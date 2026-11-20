# Ch 23 — CodeGen 流水线源码入口：ISel → Machine IR → RA → MC

> **目标**：把「`llc` 把 IR 变成汇编」拆成可定位的源码台阶。学完你能回答：这条机器指令是哪个阶段生成的？`-print-after` 为什么没输出？这段测试跑的是 legacy 还是 new PM 的 CodeGen 流水线？以及一个 diff 改的是 ISel、机器 pass，还是 AsmPrinter/MC。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo` + assertions ON，targets `X86;AArch64;RISCV`）。本章的命令和输出都在这个构建上实际运行。行号会漂，先按符号用 `rg` 重新定位。

## 为什么需要这个？

中端 review 的失败通常是「这个变换不合法」；后端 review 的失败更常见的是「你把 pass 放错了位置」。同一个 `MachineFunctionPass`，放在寄存器分配前还是后，能用的不变量完全不同：

- 寄存器分配前，虚拟寄存器还在，`MachineFunctionProperties::IsSSA` 可能成立，`LiveIntervals` 可以算。
- 寄存器分配后，虚拟寄存器被 `VirtRegMap` 重写成物理寄存器，`NoVRegs` 成立，很多 SSA 假设失效。
- `AsmPrinter` 之后已经没有「改机器指令」这回事，只剩 MC 层的编码与重定位。

如果 review 时只看 diff 里的 C++，不看它挂在流水线的哪个钩子上，你就会漏掉「这个 pass 在 RA 之后却读取虚拟寄存器」「这个测试用 `-stop-after` 停在了错误的阶段」这类问题。本章给一张能落到 `file:line` 的地图。

## 先建立直觉

把后端想成一条装配线，每个工位只接受特定形态的半成品：

```
min.ll
  │  llc driver：选 target、建 TargetMachine
  ▼
TargetMachine::addPassesToEmitFile
  │  建 TargetPassConfig，按节目单 addPass
  ▼
IR 级准备（CodeGenPrepare、异常 lowering、ISel prepare）
  │
  ▼
ISel：SelectionDAG 或 GlobalISel        ← IR 指令变成 MachineInstr
  │
  ▼
FinalizeISel                            ← 展开 ISel 伪指令，补 frame 信息
  │
  ▼
MachineFunction passes（SSA 优化 → RA → post-RA）
  │
  ▼
AsmPrinter（MachineInstr → MCInst）      ← 仍能看到 MI，但不再改 MI 语义
  │
  ▼
MCStreamer / MCCodeEmitter / object writer
  │
  ▼
min.o 或 .s
```

关键分界线只有两条：**ISel 之前是 IR，ISel 之后是 Machine IR；AsmPrinter 之后是 MC**。Review 时先确定 diff 落在哪一段，再谈不变量。

## 23.1 从 `llc` 到 `TargetMachine`

当前树里 `llvm/tools/llc/llc.cpp` 只剩一个入口壳：

```cpp
// llvm/tools/llc/llc.cpp:15
extern "C" int llcMain(int argc, char **argv);
int main(int argc, char **argv) {
  llvm::InitLLVM X(argc, argv);
  return llcMain(argc, argv);
}
```

真正的 driver 在 `llvm/tools/llc/lib/llcdriver.cpp:370` 的 `llcMain`。它负责解析 `.ll`/`.mir`、建 `TargetMachine`、决定 legacy 还是 new PM，最后把模块交给 `addPassesToEmitFile`（`llcdriver.cpp:846`）。旧资料里的 `LLVMTargetMachine` 在当前树已不存在：`rg -n 'LLVMTargetMachine' llvm/include/llvm/Target llvm/lib/CodeGen` 无匹配；通用实现改名为 `CodeGenTargetMachineImpl`（`CodeGenTargetMachineImpl.h:24`，commit `bb3f5e1fed7c`）。

目标无关的基类在 `llvm/include/llvm/Target/TargetMachine.h:85`，虚接口 `addPassesToEmitFile` 在 `TargetMachine.h:474`。真正的通用实现落在 `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:222` 的 `CodeGenTargetMachineImpl::addPassesToEmitFile`。它做三件事：

1. 调 `addPassesToGenerateCode`（同文件 `:109`）建立 `TargetPassConfig`。
2. 让 `TargetPassConfig` 安装 ISel 与机器 pass。
3. 如果流水线会完整跑完，追加 `AsmPrinter`；否则追加 MachineVerifier 或 MIR printer。

`addPassesToGenerateCode` 的核心是这几行（`CodeGenTargetMachineImpl.cpp:109` 起）：

```cpp
TargetPassConfig *PassConfig = TM.createPassConfig(PM);
PM.add(PassConfig);
...
if (PassConfig->addISelPasses()) return nullptr;
PassConfig->addMachinePasses();
```

注意 `PM.add(PassConfig)` 的顺序：`TargetPassConfig` 自己也是一个 pass，它在 `doInitialization` 阶段把后续 pass 插进 `PassManager`。所以你在 `-debug-pass=Structure` 里看到的那一长串，是 `TargetPassConfig` 构造出来的，不是 driver 手写的。

### legacy 与 new PM 的 CodeGen 是两套东西

`llc` 对 X86 默认走 legacy CodeGen pass manager。判断依据在 `llcdriver.cpp:753`：只有显式 `-passes`、显式 `-enable-new-pm`，或 target 的 `shouldDefaultToNewPM()` 为真时才切 new PM。当前树里只有 MSP430 和 Lanai 覆盖 `shouldDefaultToNewPM()` 返回 `true`（`llvm/lib/Target/MSP430/MSP430TargetMachine.h:62`、`llvm/lib/Target/Lanai/LanaiTargetMachine.h:66`），X86 没有覆盖，所以默认是 legacy。

可以用两次不同的调试输出确认：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/llc -O2 -debug-pass=Structure min.ll -o /dev/null 2>&1 | head -1
$B/llc -O2 -enable-new-pm -debug-pass-manager min.ll -o /dev/null 2>&1 | head -3
```

实测第一行会打印 legacy 的 pass 参数列表，里面能直接看到 `-x86-isel -finalize-isel ... -greedy ... -x86-asm-printer`。第二条会打印 `Running pass: ... on [module]` 形式的新 PM 事件。对 `min.ll`，两条路径生成的汇编相同，但**它们的 pass 注册与分析管理不同**；测试到底覆盖哪条，是 review 时要问的第一件事。

### `CodeGenPassBuilder` 是什么关系

当前树里没有 `llvm/lib/CodeGen/CodeGenPassBuilder.cpp`。实际位置是：

- 声明：`llvm/include/llvm/Passes/CodeGenPassBuilder.h:86`
- 实现：`llvm/lib/Passes/CodeGenPassBuilder.cpp:232`（`buildPipeline`）
- X86 的派生：`llvm/lib/Target/X86/X86CodeGenPassBuilder.cpp:48`

它不是 legacy `TargetPassConfig` 的替代品，而是 **new PM 版本的同一张节目单**。以 X86 为例：

```
llc -enable-new-pm
  → compileModuleWithNewPM                  (llvm/tools/llc/lib/NewPMDriver.cpp:88)
  → TargetMachine::buildCodeGenPipeline     (TargetMachine.h:531)
  → X86TargetMachine::buildCodeGenPipeline  (X86CodeGenPassBuilder.cpp:358)
  → X86CodeGenPassBuilder::buildPipeline    (CodeGenPassBuilder.cpp:232)
```

`llvm/lib/Passes/RunCodeGen.cpp:105` 的 `runCodeGenPipeline` 是库级别入口（clang、flang、LTO 用），内部在 `:59` 的 `runCodeGenPipelineNewPM` 和 legacy 之间选；`llc` 自己走上面的 `NewPMDriver`。new PM 分支最终也调用 `TargetMachine::buildCodeGenPipeline`。

`X86CodeGenPassBuilder` 覆盖的钩子和 `X86PassConfig` 几乎一一对应：`addIRPasses`、`addInstSelector`、`addMachineSSAOptimization`、`addOptimizedRegAlloc`、`addAsmPrinter` 等。两套代码目前并存，迁移还没完成。`CodeGenPassBuilder.h:33` 的 FIXME 写着：

```cpp
// FIXME: Dummy target independent passes definitions that have not yet been
// ported to new pass manager. Once they do, remove these.
#define DUMMY_MACHINE_FUNCTION_PASS(NAME, PASS_NAME) ...
```

`llvm/include/llvm/Passes/MachinePassRegistry.def:245` 起的 `DUMMY_MACHINE_FUNCTION_PASS` 列表会把这些尚未迁移的 pass 变成「什么都不做、返回 `PreservedAnalyses::all()`」的占位符。这意味着：**同一条测试在 `-enable-new-pm` 下可能少跑若干优化**。Review 一个 CodeGen 改动时，如果测试只跑默认 pipeline，不能推断 new PM 路径也正确；反过来，new PM 下的输出差异也不能直接当成 legacy 的 bug。

## 23.2 `TargetPassConfig`：后端的节目单

`TargetPassConfig` 在 `llvm/include/llvm/CodeGen/TargetPassConfig.h:85`，继承 `ImmutablePass`。它不做变换，只负责按 target 和命令行选项把 pass 排好。

### 两个总入口

```cpp
// llvm/lib/CodeGen/TargetPassConfig.cpp:937
bool TargetPassConfig::addISelPasses() {
  ...
  addIRPasses();
  addCodeGenPrepare();
  addPassesToHandleExceptions();
  addISelPrepare();
  return addCoreISelPasses();
}

// llvm/lib/CodeGen/TargetPassConfig.cpp:985
void TargetPassConfig::addMachinePasses() {
  ...
  addMachineSSAOptimization();
  ...
  addPreRegAlloc();
  ...
  addOptimizedRegAlloc();   // 或 addFastRegAlloc()
  addPostRegAlloc();
  ...
  addMachineLateOptimization();
  ...
  addPreEmitPass();
}
```

`addMachinePasses` 的注释写得很直白：它「可以当作 LLVM CodeGen 主要阶段的标准顺序来读」。如果你想知道一个机器 pass 的合法上下文，先在这个函数里找它被插在哪个 `add*` 钩子之间。

### `addPass` 不只是 `PM->add`

`TargetPassConfig::addPass`（`TargetPassConfig.cpp:578`）同时处理三件事：

- `-start-before` / `-start-after` / `-stop-before` / `-stop-after` 的截断。
- target 对 pass 的替换（`getPassSubstitution`）。
- 机器 pass 前后的 debugify 与 MachineVerifier（`addMachinePrePasses` / `addMachinePostPasses`）。

所以 `-stop-after=finalize-isel` 不是 driver 在输出阶段截断，而是 `addPass` 在看到 `FinalizeISelID` 之后把 `Stopped` 置位，后面的 pass 被 `delete` 掉。名字写错时 `getPassIDFromName` 返回空，`addPass` 不会命中任何阶段，最终表现为「什么都没停住」或直接报错；具体行为取决于选项路径，下一节的失败例子会展示。

### 一张实际节目单

```bash
$B/llc -O2 -debug-pass=Structure min.ll -o /dev/null 2>&1 | head -1
```

从实测输出里截一段（省略号是原文没有的）：

```text
... -x86-isel ... -finalize-isel ... -slot-indexes -liveintervals
... -two-address-instruction -register-coalescer -machine-scheduler
... -greedy -virt-reg-rewriter ... -post-RA-sched ... -x86-asm-printer
```

这张表比任何架构图都可靠。要注意 `-slot-indexes`、`-liveintervals` 出现在 `-greedy` 之前，`-virt-reg-rewriter` 出现在之后；第 24、25 章会解释这条分界线为什么重要。

## 23.3 ISel 两条线：reviewer 需要知道的边界

`addCoreISelPasses`（`TargetPassConfig.cpp:850`）先在 `SelectionDAG`、`FastISel`、`GlobalISel` 三者中选一个。选择顺序大致是：显式 `-fast-isel` → 显式/默认 GlobalISel → `-O0` 且 target 想要 FastISel → 否则 SelectionDAG。两条主线的入口不同，但终点相同。

### SelectionDAG

Target 在 `addInstSelector()` 里安装自己的 selector。X86 的实现是 `X86PassConfig::addInstSelector`（`llvm/lib/Target/X86/X86TargetMachine.cpp:442`）：

```cpp
addPass(createX86ISelDag(getX86TargetMachine(), getOptLevel()));
```

往上一层是 `SelectionDAGISelLegacy::runOnMachineFunction`（`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:375`），它把工作交给 `SelectionDAGISel::runOnMachineFunction`（同文件 `:600`）。真正的逐块选择在 `SelectAllBasicBlocks` 里，核心循环会调用 `SelectBasicBlock`（`:873`）和 `DoInstructionSelection`（`:1276`）。

Reviewer 需要知道的边界：SelectionDAG 的合法性由 `TargetLowering` 和 `.td` pattern 共同决定；`SelectionDAGISel` 负责把 IR 基本块变成 DAG、做合法化、再匹配 pattern。**pattern 本身不是本章主题**，但你要知道失败通常表现为 `Cannot select`，而不是 verifier 错误。

### GlobalISel

GlobalISel 的四个钩子在 `TargetPassConfig.h:294` 起，默认实现返回 `true`（表示 target 不支持）。X86 的实现（`X86TargetMachine.cpp:456` 起）主线依次是：

```text
IRTranslator → Legalizer → RegBankSelect → InstructionSelect
```

最后的 `InstructionSelectLegacy` 包住 `InstructionSelectImpl::runOnMachineFunction`（`llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp:150`）。它按 post-order 遍历基本块，对每条指令调用 `selectInstr`（同文件 `:366`）。

Reviewer 需要知道的边界：GlobalISel 的失败通常先体现在 `MachineFunctionProperties`（`Legalized`、`RegBankSelected`、`Selected`）或 `machineFunctionIsIllegal` 检查，而不是 IR verifier。GlobalISel 失败后是否回退到 SelectionDAG，由 `isGlobalISelAbortEnabled()` 决定（`TargetPassConfig.cpp:923`）。这条回退路径会让「本应失败的测试」悄悄通过，所以测试里经常显式加 `-global-isel-abort=1`。

### 两条线共同的后处理

不管走哪条线，`addCoreISelPasses` 最后都会加入 `FinalizeISel`：

```cpp
// llvm/lib/CodeGen/TargetPassConfig.cpp:929
addPass(&FinalizeISelID);
printAndVerify("After Instruction Selection");
```

`MachineFunctionPass`（`llvm/include/llvm/CodeGen/MachineFunctionPass.h:31`）是这些 pass 的基类：子类实现 `runOnMachineFunction(MachineFunction &)`，`SelectionDAGISelLegacy`、`FinalizeISel`、`AsmPrinter` 都走这个接口。

`FinalizeISel::runOnMachineFunction`（`llvm/lib/CodeGen/FinalizeISel.cpp:89`）展开 ISel 产生的伪指令；SelectionDAG 路径还会调用 target 的 `finalizeLowering`，GlobalISel 在 `InstructionSelect` 里已经做过收尾。它也是「ISel 结束」这个观察点的定义：`-stop-after=finalize-isel` 之后，你看到的已经是 target-specific `MachineInstr`。

## 23.4 IR → MI → MC 的源码入口表

| 阶段 | 谁触发 | 源码位置 | 关键类/函数 |
| --- | --- | --- | --- |
| driver 入口 | `llc` main | `llvm/tools/llc/llc.cpp:15` → `llvm/tools/llc/lib/llcdriver.cpp:370` | `llcMain` |
| 选择输出路径 | `compileModule` | `llvm/tools/llc/lib/llcdriver.cpp:846` | `Target->addPassesToEmitFile` |
| target 虚接口 | legacy 管线 | `llvm/include/llvm/Target/TargetMachine.h:474` | `TargetMachine::addPassesToEmitFile` |
| 通用实现 | legacy 管线 | `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:222` | `CodeGenTargetMachineImpl` |
| pass 节目单 | `addPassesToGenerateCode` | `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:109` | `TargetPassConfig` |
| IR 准备 | `addISelPasses` | `llvm/lib/CodeGen/TargetPassConfig.cpp:937` | `addIRPasses` / `addCodeGenPrepare` |
| SelectionDAG ISel | target `addInstSelector` | `llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:600` | `SelectionDAGISel::runOnMachineFunction` |
| GlobalISel ISel | target `addGlobalInstructionSelect` | `llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp:150` | `InstructionSelectImpl` |
| ISel 收尾 | `addCoreISelPasses` | `llvm/lib/CodeGen/FinalizeISel.cpp:89` | `FinalizeISel` |
| 机器 SSA 优化 | `addMachinePasses` | `llvm/lib/CodeGen/TargetPassConfig.cpp:1181` | `addMachineSSAOptimization` |
| 寄存器分配 | `addMachinePasses` | `llvm/lib/CodeGen/TargetPassConfig.cpp:1335` | `addOptimizedRegAlloc` |
| post-RA 收尾 | `addMachinePasses` | `llvm/lib/CodeGen/TargetPassConfig.cpp:1394` | `addMachineLateOptimization` |
| MI → MC | `addAsmPrinter` | `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:135` | `AsmPrinter`（`AsmPrinter.h:95`） |
| MC 输出 | `createMCStreamer` | `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:158` | `MCStreamer`（`MCStreamer.h:222`） |

这张表可以直接当 review 的定位工具：diff 改的是 `SelectionDAGISel.cpp`，问题就落在「IR → MI」；改的是 `TargetPassConfig::addMachinePasses` 的顺序，问题就落在「谁在 RA 前后」；改的是 `AsmPrinter`，问题就落在「MI → MC」。

## 23.5 实测：让流水线自己说话

先准备最小输入：

```llvm
; min.ll
define i32 @addmul(i32 %a, i32 %b) {
entry:
  %t = add i32 %a, 1
  %r = mul i32 %t, %b
  ret i32 %r
}
```

### 生成汇编

```bash
$B/llc -O2 min.ll -o -
```

实测输出（节选，`...` 表示省略）：

```text
addmul:
	...
	leal	1(%rdi), %eax
	imull	%esi, %eax
	retq
```

`leal` 和 `imull` 已经是 target-specific 指令；如果你在 `-stop-after=finalize-isel` 里看到 `INC32r` / `IMUL32rr`，那是 MC 之前的 MI opcode，名字不同是正常的。

### 在 `finalize-isel` 之后打印 Machine IR

```bash
$B/llc -O2 -print-after=finalize-isel min.ll -o /dev/null
```

实测输出走 **stderr**（空行有删节）：

```text
# *** IR Dump After Finalize ISel and expand pseudo-instructions (finalize-isel) ***:
# Machine code for function addmul: IsSSA, TracksLiveness
Function Live Ins: $edi in %0, $esi in %1

bb.0.entry:
  liveins: $edi, $esi
  %1:gr32 = COPY $esi
  %0:gr32 = COPY $edi
  %2:gr32 = INC32r %0:gr32(tied-def 0), implicit-def dead $eflags
  %3:gr32 = IMUL32rr %2:gr32(tied-def 0), %1:gr32, implicit-def dead $eflags
  $eax = COPY %3:gr32
  RET 0, $eax
```

这段输出里有三件事值得记住：`%0`/`%1` 是虚拟寄存器，`$edi`/`$eax` 是物理寄存器，`implicit-def dead $eflags` 是隐式 def + dead flag。第 24 章会把它们逐个拆开。

### 停在 `finalize-isel`，输出 MIR

```bash
$B/llc -O2 -stop-after=finalize-isel min.ll -o -
```

实测输出的 `body` 部分（空行有删节）：

```yaml
body:             |
  bb.0.entry:
    liveins: $edi, $esi
    %1:gr32 = COPY $esi
    %0:gr32 = COPY $edi
    %2:gr32 = INC32r %0, implicit-def dead $eflags
    %3:gr32 = IMUL32rr %2, %1, implicit-def dead $eflags
    $eax = COPY %3
    RET 0, $eax
```

同一份内容，`-print-after` 是给人看的宽度，`-stop-after` 是可被 `llc` 重新读入的 MIR。写测试时优先用后者，因为它能直接作为下一条 `RUN:` 的输入。

### 生成 object 并反汇编

```bash
$B/llc -O2 -filetype=obj min.ll -o min.o
$B/llvm-objdump -d min.o
```

实测：

```text
0000000000000000 <addmul>:
       0: 8d 47 01                      leal	0x1(%rdi), %eax
       3: 0f af c6                      imull	%esi, %eax
       6: c3                            retq
```

到这里，`leal` 已经被编码成 `8d 47 01`。如果 review 涉及编码、重定位或 `.o` 布局，观察点已经越过 `AsmPrinter`，应该去 Ch 26 的 MC 层找，而不是继续在 `MachineInstr` 里找。

## 23.6 失败例子：三种「看起来没跑」的假象

### 失败一：`-print-after` 名字写错，空输出且退出码为 0

```bash
$B/llc -O2 -print-after=finalize-isell min.ll -o /dev/null
echo $?
```

实测没有任何输出，退出码 `0`。原因是 `-print-after` 用 pass 的 argument（`finalize-isel`）去匹配，不匹配时只是「没有 pass 命中」，不会报错。把它和 `-stop-after` 对比：

```bash
$B/llc -O2 -stop-after=finalize-isell min.ll -o /dev/null
```

实测：

```text
LLVM ERROR: "finalize-isell" pass is not registered.
```

同一个拼写错误，一个静默，一个致命。写实验记录时，凡是看到「命令成功但没有输出」，先怀疑名字，不要先怀疑 pass 没跑。

### 失败二：只把输出重定向到文件，以为 `-print-after` 没工作

```bash
$B/llc -O2 -print-after=finalize-isel min.ll -o /dev/null > dump.txt
wc -c dump.txt
```

实测 `dump.txt` 是 0 字节，因为 IR dump 写到 `dbgs()`，也就是 stderr。正确写法是 `2> dump.txt` 或 `2>&1`。这不是 LLVM 的怪癖，而是所有 `-print-after`/`-print-before` 的共同行为；review 里看到别人贴「没有输出」的日志，先问重定向。

### 失败三：漏掉 `-mattr`，目标指令选不出来

```llvm
; vec.ll
define <4 x i32> @vadd(<4 x i32> %a, <4 x i32> %b) {
  %r = add <4 x i32> %a, %b
  ret <4 x i32> %r
}
```

```bash
$B/llc -O2 -mattr=-sse2 vec.ll -o -
```

实测输出是一串标量 `addl`，没有 `paddd`：

```text
	movq	%rdi, %rax
	addl	%r9d, %esi
	addl	8(%rsp), %edx
	addl	16(%rsp), %ecx
	addl	24(%rsp), %r8d
	...
```

这不是 ISel 漏了 pattern，而是 subtarget 明确禁用了 SSE2。Review 里看到「这段代码没有向量化」时，先确认 `RUN:` 行的 `-mattr`/`-mcpu` 是否真的开启了对应 feature；把 feature 关掉再指责 pattern，是最常见的假 bug。

## 踩雷集锦

1. **错误直觉：`-print-after` 写错会报错。** 正确认识：它按 pass argument 匹配，写错只是没有命中，输出为空、退出码 0；`-stop-after` 写错才会 `pass is not registered`。
2. **错误直觉：`llc` 默认跑 new PM CodeGen。** 正确认识：X86 默认 legacy；`-enable-new-pm` 或 `-passes` 才切。两套节目单由 `TargetPassConfig` 和 `CodeGenPassBuilder` 分别构造。
3. **错误直觉：`-enable-new-pm` 只是换个 pass manager，行为等价。** 正确认识：`MachinePassRegistry.def` 里还有 `DUMMY_*` 占位 pass，迁移未完成时 new PM 可能少跑优化。
4. **错误直觉：`finalize-isel` 之后就没有伪指令了。** 正确认识：`FinalizeISel` 展开的是 ISel 伪指令；后续 target pass 仍可能生成新的伪指令（例如 frame 相关），直到 `ExpandPostRAPseudos`。
5. **错误直觉：看到 `selected: false` 说明 ISel 没跑。** 正确认识：`Selected` 是 GlobalISel 的属性；SelectionDAG 路径不设置它，MIR 里显示 `false` 是正常的（第 24 章详述）。

## 动手练习

1. 用 `-debug-pass=Structure` 找出 `-greedy` 前后的三个 pass，并说明它们各自需要什么 `MachineFunctionProperties`。
2. 把 `min.ll` 改成带一个 `alloca` 的函数，分别跑 `-print-after=finalize-isel` 和 `-print-after=prolog-epilog`，找出 `$rsp` 第一次出现的位置。
3. 用 `-stop-after=finalize-isel` 生成 MIR，再用 `-run-pass=none` 读回去，确认 round-trip 不丢 `liveins`。
4. 跑 `-enable-new-pm -debug-pass-manager`，找出一个 `DUMMY_*` pass，说明它为什么会让两条路径的输出不同。

## 本章重点整理

- `llvm/tools/llc/llc.cpp` 只是壳，driver 在 `llvm/tools/llc/lib/llcdriver.cpp`。
- legacy CodeGen 的主线是 `TargetMachine::addPassesToEmitFile` → `CodeGenTargetMachineImpl` → `TargetPassConfig`。
- `TargetPassConfig::addPass` 同时处理 start/stop、target 替换、debugify 和 MachineVerifier，不只是 `PM->add`。
- `addISelPasses` 与 `addMachinePasses` 是两个总入口；RA 之前和之后的合法上下文不同。
- SelectionDAG 与 GlobalISel 的入口不同，但都以 `FinalizeISel` 作为「ISel 结束」的观察点。
- new PM CodeGen 由 `CodeGenPassBuilder` 构造，和 legacy 并存；迁移未完成，`DUMMY_*` pass 会改变实际执行的优化集合。
- `-print-after` 走 stderr，名字按 pass argument 匹配；写错不会报错。

## 自我检核

- [ ] 不看笔记，能画出 `llc` → `addPassesToEmitFile` → `TargetPassConfig` → ISel → machine passes → AsmPrinter 的调用链
- [ ] 能说出 `llvm/tools/llc/llc.cpp` 和 `llvm/tools/llc/lib/llcdriver.cpp` 各自负责什么
- [ ] 能解释 `TargetPassConfig::addPass` 为什么不能直接看成 `PM->add`
- [ ] 能说出 `-print-after` 与 `-stop-after` 在名字写错时的不同行为，并解释原因
- [ ] 能判断一条 `llc` 命令走的是 legacy 还是 new PM，并给出验证命令
- [ ] 能说出 `CodeGenPassBuilder` 与 `TargetPassConfig` 的对应关系，以及 `DUMMY_*` 的风险
- [ ] 能指出 SelectionDAG 和 GlobalISel 各自在哪个文件、哪个函数进入逐指令选择
- [ ] 拿到一个 CodeGen diff，能先判断它落在 IR、MI 还是 MC 段

## 延伸阅读

- **[LLVM Code Generator Design](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：`The LLVM Target-Independent Code Generator` 的 pipeline 小节，特别是 `Instruction Selection`、`Register Allocation`、`Code Emission`
  - **能学到什么**：本章表格的官方版本，以及每个阶段的输入/输出契约；对照 `TargetPassConfig.cpp` 读，能把文档里的阶段名落到函数名
  - **前提**：读完本章 23.1–23.3，知道 `MachineFunction` 是什么
- **[LLVM Command Guide: llc](https://llvm.org/docs/CommandGuide/llc.html)**
  - **读哪里**：`-print-after`、`-stop-after`、`-start-before`、`-filetype`、`-mattr`、`-mcpu` 各节
  - **能学到什么**：这些 flag 的官方语义和边界；尤其是 `-run-pass` 只吃 `.mir`，`-stop-*` 用的是 pass argument 名
  - **前提**：能读懂 MIR 的 `body:` 段（第 24 章）
- **[GlobalISel — LLVM documentation](https://llvm.org/docs/GlobalISel.html)**
  - **读哪里**：`Pipeline` 与 `Instruction Select` 两节
  - **能学到什么**：IRTranslator → Legalizer → RegBankSelect → InstructionSelect 的设计动机，以及它和 SelectionDAG 的分工
  - **前提**：读过本章 23.3；不要求读 pattern 细节

→ [Ch 24 — Machine IR 与 MIR：`MachineFunction`、`MachineInstr`、`LiveIntervals`](./24-machine-ir-and-mir.md)
