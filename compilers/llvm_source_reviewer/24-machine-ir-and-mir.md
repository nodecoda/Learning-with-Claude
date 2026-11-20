# Ch 24 — Machine IR 与 MIR：`MachineFunction`、`MachineInstr`、`LiveIntervals`

> **目标**：把 Machine IR 从「汇编之前的中间态」变成可检查的对象模型：`MachineFunction` / `MachineBasicBlock` / `MachineInstr` / `MachineOperand` 各自拥有什么，虚拟寄存器和物理寄存器在文本里怎么表示，MIR 怎么被读回、怎么用 `-run-pass` 和 `-verify-machineinstrs` 做最小实验，以及 `LiveIntervals` / `SlotIndexes` 在寄存器分配前后扮演什么角色。

> **环境**：LLVM 24.0.0git，源码 `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（assertions ON）。本章的 MIR、命令和报错都来自这棵树的实际运行。MIR 文本格式和 pass 名会随版本变化，引用前先 `rg` 确认。命令里的 `$B` 指向 `/home/ubuntu/llvm/builds/pr-lane/bin`。

## 为什么需要这个？

LLVM IR 是 target-independent 的：`add i32` 不关心寄存器，也不关心栈帧。到了后端，问题变成：

- 这个值放在哪个虚拟寄存器里？它的 register class 是什么？
- 这条指令有几个 def/use？哪些是隐式的？哪些是 tied？
- 这个基本块的 live-in 是哪些物理寄存器？
- 寄存器分配之后，虚拟寄存器被换成物理寄存器，哪些 liveness 信息还能信？

Machine IR 就是为这些问题设计的一层对象模型。它比 IR 更接近机器，但还没有被编码成字节；它保留了基本块、指令和操作数结构，所以既能被优化，也能被打印、解析和单测。

MIR 文本格式的价值在于**可复现**。一个后端 bug 常常只涉及「某条指令、某个虚拟寄存器、某个 live interval」，不需要整个 clang driver。把 `-stop-after` 的输出存成 `.mir`，就能在不再跑前端和中端的情况下复现问题。Review 时看到测试用 `.mir`，你要能判断它锁住的到底是哪条不变量。

## 先建立直觉

```
LLVM IR
  │  ISel（第 23 章）
  ▼
MachineFunction
  ├── MachineBasicBlock 1
  │     ├── MachineInstr: %2:gr32 = INC32r %0, implicit-def dead $eflags
  │     │     ├── MachineOperand: %2   (def, vreg)
  │     │     ├── MachineOperand: %0   (use, vreg)
  │     │     └── MachineOperand: $eflags (implicit-def, dead, physreg)
  │     └── liveins: $edi
  └── MachineBasicBlock 2 ...
  │
  │  MIRPrinter
  ▼
.mir 文本  ──MIRParser──▶  同一个 MachineFunction
  │
  │  AsmPrinter（第 26 章）
  ▼
MCInst / 汇编 / object
```

对象的所有权是单向的：`MachineFunction` 拥有基本块和寄存器信息，`MachineBasicBlock` 拥有指令列表，`MachineInstr` 拥有操作数数组。删除指令要走 `MachineFunction::deleteMachineInstr`（`MachineFunction.h:1106`），不要直接 `delete`；分配要走 `CreateMachineInstr`（`MachineFunction.h:1083`）。这条规则和 IR 里「指令由 `Function` 的 allocator 管理」是同一个设计。

## 24.1 四个核心对象

| 对象 | 头文件:行 | 负责什么 | 不该负责什么 |
| --- | --- | --- | --- |
| `MachineFunction` | `llvm/include/llvm/CodeGen/MachineFunction.h:286` | 一个函数的 MI 容器、`MachineRegisterInfo`、frame info、属性 | 不做指令选择，不做寄存器分配 |
| `MachineBasicBlock` | `llvm/include/llvm/CodeGen/MachineBasicBlock.h:115` | 指令列表、前驱/后继、live-in 列表 | 不拥有函数级分析 |
| `MachineInstr` | `llvm/include/llvm/CodeGen/MachineInstr.h:72` | opcode、操作数数组、MI flags、debug loc | 不解释操作数语义 |
| `MachineOperand` | `llvm/include/llvm/CodeGen/MachineOperand.h:49` | 寄存器/立即数/符号/帧索引等具体操作数 | 不持有指令 |

`MachineRegisterInfo`（`llvm/include/llvm/CodeGen/MachineRegisterInfo.h:55`）是函数级的寄存器表：虚拟寄存器的 class、def/use 链都在这里。`createVirtualRegister` 在 `MachineRegisterInfo.h:779`，`getVRegDef` 在 `:647`。如果你在 review 里看到有人手写 `Register(999)` 而不走 `createVirtualRegister`，先怀疑虚拟寄存器编号越界。

### `MachineFunctionProperties`：pass 之间的契约

`MachineFunction` 里有一组属性（`MachineFunction.h:138`），pass 可以声明「我要求什么」和「我保证什么」：

```cpp
// llvm/include/llvm/CodeGen/MachineFunction.h:187 附近
enum class Property : unsigned {
  IsSSA, NoPHIs, TracksLiveness, NoVRegs,
  FailedISel, Legalized, RegBankSelected, Selected,
  TiedOpsRewritten, FailsVerification, FailedRegAlloc,
  TracksDebugUserValues,
};
```

常见组合：

| 属性 | 含义 | 典型出现位置 |
| --- | --- | --- |
| `IsSSA` | 每个虚拟寄存器只有一个 def | ISel 之后、PHI elimination 之前 |
| `NoPHIs` | 没有 PHI 指令 | PHI elimination 之后 |
| `TracksLiveness` | live-in / kill / dead 信息可信 | 大多数机器 pass |
| `NoVRegs` | 没有虚拟寄存器 | 寄存器分配之后 |
| `Legalized` / `RegBankSelected` / `Selected` | GlobalISel 的三个阶段 | GlobalISel 流水线 |

这不是文档装饰。`TargetPassConfig::addPass` 会在机器 pass 前后调用 `addMachinePrePasses` / `addMachinePostPasses`；`MachineFunctionPass::runOnFunction` 在调用你的 `runOnMachineFunction` 之前检查 `RequiredProperties`（`MachineFunctionPass.cpp:65`）。一个 pass 声明 `IsSSA` 却运行在 PHI elimination 之后，你会看到：

```text
MachineFunctionProperties required by ... pass are not met by function f.
Required properties: IsSSA
Current properties: NoPHIs, TracksLiveness, TiedOpsRewritten
LLVM ERROR: MachineFunctionProperties check failed
```

这段报错来自属性检查，不是 IR verifier，也不是 MachineVerifier 的指令检查。Review 时看到它，先找 pass 的 `getRequiredProperties()`，再对照它在 `addMachinePasses` 里的位置。

### 一个容易误读的字段：`selected: false`

第 23 章的 `-stop-after=finalize-isel` 输出里有：

```yaml
legalized:       false
regBankSelected: false
selected:        false
isSSA:           true
```

这里的 `selected` 是 GlobalISel 的属性：`InstructionSelect` 跑完才置位。SelectionDAG 路径不会设置它，所以 `selected: false` **不代表 ISel 没跑**。判断 SDAG 是否已经选择，要看指令本身是不是 target-specific（`INC32r`、`IMUL32rr`），而不是看这个字段。把 `selected` 当成通用「ISel 已运行」标志，是读 MIR 时最常见的误判。

## 24.2 `MachineOperand` 与 flags

`MachineOperand::MachineOperandType` 在 `MachineOperand.h:51`，常用类型：

| 类型 | 表示什么 | 例子 |
| --- | --- | --- |
| `MO_Register` | 虚拟或物理寄存器 | `%0`、`$edi` |
| `MO_Immediate` | 整数立即数 | `1`、`-8` |
| `MO_FrameIndex` | 抽象栈帧索引 | `%stack.0` |
| `MO_GlobalAddress` | 全局符号地址 | `@g` |
| `MO_MachineBasicBlock` | 基本块引用 | `%bb.1` |
| `MO_RegisterMask` | 调用破坏的寄存器集合 | call 的 implicit operand |
| `MO_CFIIndex` | CFI 指令索引 | `.cfi_*` |
| `MO_Metadata` / `MO_MCSymbol` | 调试或符号引用 | `!0`、`.Lfoo` |

操作数上的 flag 决定它在这条指令里的角色：

- `isDef()`（`MachineOperand.h:390`）/ `isUse()`（`:385`）：这是 def 还是 use。
- `isKill()`（`:405`）：这是该寄存器 live range 的最后一次 use。
- `isDead()`（`:400`）：这个 def 的结果没有被使用。
- `isUndef()`（`:410`）：这个 use 的值不确定。
- `isEarlyClobber()`、`isRenamable()`、`isInternalRead()`、`isDebug()`：约束、可重命名性、内部读、调试用途。

把第 23 章的 MIR 拆开看：

```text
%2:gr32 = INC32r %0:gr32(tied-def 0), implicit-def dead $eflags
```

- `%2:gr32` 是虚拟寄存器 def，class 为 `gr32`。
- `%0:gr32(tied-def 0)` 是 use，同时 tied 到第 0 个操作数；tied 意味着 def 和 use 必须分配同一个物理寄存器。
- `implicit-def dead $eflags` 是隐式 def：指令文本里没有写 `$eflags`，但语义上它被修改了；`dead` 表示结果没人用。

`MachineInstr::MIFlag`（`MachineInstr.h:89`）是**指令级**的 flag，和操作数 flag 是两套东西：

```cpp
enum MIFlag {
  NoFlags = 0,
  FrameSetup = 1 << 0,     // 函数序言的一部分
  FrameDestroy = 1 << 1,   // 函数尾声的一部分
  BundledPred = 1 << 2,
  BundledSucc = 1 << 3,
  FmNoNans = 1 << 4,       // fast-math: 不允许 NaN
  FmNoInfs = 1 << 5,
  FmNsz = 1 << 6,
  FmArcp = 1 << 7,
  FmContract = 1 << 8,
  ...
};
```

`FrameSetup` / `FrameDestroy` 会影响 PEI 和栈帧布局；`Fm*` 会影响浮点优化的合法性。Review 一个改 MI flag 的 diff 时，先问「谁会读这个 flag」，再问「清掉它会不会让后面的 pass 做出错误假设」。

## 24.3 MIR 文本、`-run-pass` 与 verifier

MIR 文件由三段组成：

```text
--- |
  ; 可选的 LLVM IR 模块
...
---
name:            addmul
tracksRegLiveness: true
registers:
  - { id: 0, class: gr32 }
body:             |
  bb.0.entry:
    liveins: $edi
    %0:gr32 = COPY $edi
```

- `--- |` 到 `...` 是嵌入的 IR；没有它时，`llc` 会为每个 machine function 建一个空 IR 函数。
- 第二个 `---` 开始是函数级 YAML：`name`、`registers`、`liveins`、`frameInfo`、`body` 等。
- `body` 里是基本块和指令；`%0` 是虚拟寄存器，`$edi` 是物理寄存器。

打印侧的实现是 `llvm/lib/CodeGen/MIRPrinter.cpp:178` 的 `printMF`，对外入口是 `llvm/include/llvm/CodeGen/MIRPrinter.h:43` 的 `printMIR`。解析侧是 `llvm/include/llvm/CodeGen/MIRParser/MIRParser.h:46` 的 `MIRParser`，`parseMachineFunctions` 在 `llvm/lib/CodeGen/MIRParser/MIRParser.cpp:1360`。

### 用 `-stop-after` 得到 MIR

```bash
$B/llc -O2 -stop-after=finalize-isel min.ll -o min.mir
```

实测 `min.mir` 的关键字段（寄存器列表有删节）：

```yaml
name:            addmul
tracksRegLiveness: true
isSSA:           true
noVRegs:         false
registers:
  - { id: 0, class: gr32 }   # 另有 id 1..3
liveins:
  - { reg: '$edi', virtual-reg: '%0' }
  - { reg: '$esi', virtual-reg: '%1' }
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

`liveins` 同时给出物理寄存器和对应的虚拟寄存器：`$edi` 从入口就是 `%0` 的值。`registers` 里的 `id` 只用于 MIR 文件内部引用；实测 parser 加载后会重新编号，打印出的 `%N` 不一定等于 `id`。24.6 会看到 `%7` 变成 `%1`。

### `-run-pass` 只吃 `.mir`

```bash
$B/llc -run-pass=dead-mi-elimination -verify-machineinstrs min.mir -o -
```

实测输出会重新打印一份 MIR。把 `.ll` 直接喂给它：

```bash
$B/llc -run-pass=dead-mi-elimination min.ll -o /dev/null
```

实测：

```text
llc: error: run-pass is for .mir file only.
```

源码位置是 `llvm/tools/llc/lib/llcdriver.cpp:806`。`-run-pass` 的实现（`llcdriver.cpp:469` 的 `addPass`）只接受已经注册的 pass argument；写错会报 `run-pass ... is not registered`，和 `-print-after` 的静默失败不同。

`-run-pass=none` 是合法值（`llcdriver.cpp:470` 起），表示不加任何 pass，但仍然解析、验证并打印 MIR。做 round-trip 测试时它比“没有 pass”更明确：

```bash
$B/llc -run-pass=none -verify-machineinstrs tiny.mir -o -
```

### `-verify-machineinstrs` 到底验证什么

`-verify-machineinstrs` 定义在 `llvm/lib/CodeGen/CodeGenOptions.td:680`，类型是 optional bool。它在 `TargetPassConfig.cpp:471` 被用来设置 `VerifyMode`：

- 不设：默认 `MachineVerifierMode::End`，流水线末尾跑一次 verifier。
- 设了：`MachineVerifierMode::Each`，每个机器 pass 之后都跑。
- `-disable-mir-output-verify`：`MachineVerifierMode::None`。

另外，MIR parser 在加载函数时**总是**调用一次 `MF.verify`（`MIRParser.cpp:696`），不依赖这个 flag。所以：

- 一个未定义虚拟寄存器，在解析阶段就会被抓，不需要 `-verify-machineinstrs`。
- 一个 pass 把 MIR 改坏，只有 `-verify-machineinstrs` 才会在 pass 之后立刻抓；否则可能要等到流水线末尾。

这个区别在写测试时很重要：`.mir` 输入里明显的结构错误，不加 flag 也会失败；pass 之间的错误，必须加 flag 才能定位到第一个坏 pass。

### `-stop-before` 与 `-stop-after`

`-stop-before=<pass>` 在 pass 运行前停，`-stop-after=<pass>` 在运行后停。名字都是 pass argument（`stop-after` 定义在 `CodeGenOptions.td:675`），不是打印出来的 banner。对 `min.ll` 来说，这两个命令的 `body` 实测相同：

```bash
$B/llc -O2 -stop-before=finalize-isel min.ll -o - | sed -n '/^body:/,$p'
$B/llc -O2 -stop-after=finalize-isel min.ll -o - | sed -n '/^body:/,$p'
```

两次都输出同一段 `body`（`liveins: $edi, $esi`、`%2:gr32 = INC32r ...`、`%3:gr32 = IMUL32rr ...`、`RET 0, $eax`）。这说明 `FinalizeISel` 对这个函数没有产生可见变化。不要因为两次输出一样就认为 `-stop-before` 没生效；换一个带伪指令的函数再试，差异才会出现。

## 24.4 `LiveIntervals`、`SlotIndexes` 与已消失的 `LiveVariables`

寄存器分配不是「随便挑个物理寄存器」；它要知道每个虚拟寄存器在哪些程序点活着。这套信息由两个分析提供：

| 分析 | 头文件:行 | 作用 |
| --- | --- | --- |
| `SlotIndexes` | `llvm/include/llvm/CodeGen/SlotIndexes.h:307` | 给每条指令和每个基本块边界分配单调递增的 slot 编号 |
| `LiveIntervals` | `llvm/include/llvm/CodeGen/LiveIntervals.h:55` | 为每个虚拟寄存器维护 live interval（段集合） |

`LiveIntervals` 内部持有 `SlotIndexes *Indexes`（`LiveIntervals.h:66` 附近），所有 segment 的端点都是 `SlotIndex`。没有 `SlotIndexes`，live interval 无法比较先后，也无法在指令之间插入 spill/reload。

它们的位置在 `TargetPassConfig::addOptimizedRegAlloc`（`TargetPassConfig.cpp:1335`）：

```cpp
// LiveIntervals requires pure SSA form and no unreachable blocks.
addPass(&UnreachableMachineBlockElimID);
...
addPass(&LiveIntervalsID);
addPass(&MachineLoopInfoID);
addPass(&PHIEliminationID);
addPass(&TwoAddressInstructionPassID);
addPass(&RegisterCoalescerID);
...
addPass(&MachineSchedulerID);
```

这段顺序解释了两件事：

1. `LiveIntervals` 需要纯 SSA 且没有不可达块，所以 `UnreachableMachineBlockElim` 先跑。
2. `PHIElimination` 会破坏 SSA，所以 `LiveIntervals` 必须在它之前算；但后续 pass 仍然可以查已经算好的 interval。

### `LiveVariables` 在当前树里不存在

老教材和旧博客常把 `LiveVariables` 当作寄存器分配前的标准分析。这个树里要先确认。在源码树里跑 `rg -n "class LiveVariables" llvm/include llvm/lib`，实测无匹配；`ls llvm/lib/CodeGen/LiveVariables.cpp` 报 `No such file or directory`。也就是说，在当前 `7c7a9072f736` 上，**不要去找 `LiveVariables.h`，也不要假设某个 pass 依赖它**。虚拟寄存器的 liveness 由 `LiveIntervals` 提供；物理寄存器分配后的栈槽 liveness 由 `LiveStacks` 等分析处理。Review 时看到 diff 还在引用 `LiveVariables`，先确认它是不是从旧分支 cherry-pick 过来的。

## 24.5 最小 `.mir` 实验

下面这个文件不依赖 `-stop-after`，可以手写（空行有删节）：

```yaml
--- |
  define i32 @f(i32 %a) {
  entry:
    %r = add i32 %a, 1
    ret i32 %r
  }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0.entry:
    liveins: $edi
    %0:gr32 = COPY $edi
    %1:gr32 = INC32r %0, implicit-def dead $eflags
    $eax = COPY %1
    RET 0, $eax
```

跑一个不会改变结果的 pass：

```bash
$B/llc -run-pass=dead-mi-elimination -verify-machineinstrs tiny.mir -o -
```

实测 `body` 原样输出（空行有删节）：

```text
body:             |
  bb.0.entry:
    liveins: $edi
    %0:gr32 = COPY $edi
    %1:gr32 = INC32r %0, implicit-def dead $eflags
    $eax = COPY %1
    RET 0, $eax
```

把 `-run-pass` 换成 `none`，再跑一次，输出相同。这个对照说明：MIR 的解析/打印是稳定的，pass 没有改动时不会凭空产生差异。写回归测试时，这个基线能帮你区分「pass 改了」和「打印格式变了」。

## 24.6 失败例子：未定义虚拟寄存器

把 `%0` 换成一个没有 def 的 `%7`，并在 `registers` 里声明它：

```yaml
registers:
  - { id: 1, class: gr32 }
  - { id: 7, class: gr32 }
body:             |
  bb.0.entry:
    liveins: $edi

    %1:gr32 = INC32r %7, implicit-def dead $eflags
    $eax = COPY %1
    RET 0, $eax
```

```bash
$B/llc -run-pass=none -verify-machineinstrs bad-undef.mir -o -
```

实测报错：

```text
*** Bad machine code: Reading virtual register without a def ***
- function:    f
- basic block: %bb.0 entry (地址省略)
- instruction: %0:gr32 = INC32r %1:gr32(tied-def 0), implicit-def dead $eflags
- operand 1:   %1:gr32(tied-def 0)
LLVM ERROR: Found 1 machine code errors.
```

报错文本在 `llvm/lib/CodeGen/MachineVerifier.cpp:3187`：

```cpp
report("Reading virtual register without a def", MO, MONum);
```

注意两个细节。第一，输出里的 `%1` 不是你写的 `%7`：MIR parser 重新编号了虚拟寄存器，所以**不要把 `%N` 当作稳定 ID**；要定位就回到源文件，按 `registers` 的 `id` 和指令顺序核对。第二，即使不加 `-verify-machineinstrs`，这条错误也会在解析阶段出现，因为 `MIRParser.cpp:696` 会先验证一次。

### 重复定义：parser 在 verifier 之前就拒绝

如果同一个虚拟寄存器在 `IsSSA` 函数里被定义两次：

```yaml
isSSA:           true
body:             |
  bb.0.entry:
    liveins: $edi
    %0:gr32 = COPY $edi
    %0:gr32 = INC32r %0, implicit-def dead $eflags
    $eax = COPY %0
```

实测：

```text
error: bad-redef-ssa.mir: f has explicit property IsSSA, but is not valid SSA
```

这是 parser 的属性一致性检查，不是 `MachineVerifier` 的指令检查。它说明 MIR 里的 `isSSA: true` 是一个**承诺**：parser 会核对它，pass 也会依赖它。不要在 MIR 里随手写 `isSSA: true` 来绕过属性检查。

## 踩雷集锦

1. **错误直觉：`selected: false` 说明 ISel 没跑。** 正确认识：`Selected` 是 GlobalISel 属性；SelectionDAG 不设置它，target-specific opcode 才是判断依据。
2. **错误直觉：MIR 里的 `%0`、`%1` 是稳定编号。** 正确认识：parser 会重新编号；`registers` 的 `id` 和打印出的 `%N` 不是一回事，报错里的 `%N` 可能和你写的不一样。
3. **错误直觉：不加 `-verify-machineinstrs` 就不会验证。** 正确认识：MIR parser 在加载时总会 `MF.verify`；这个 flag 的作用是把流水线末尾的一次验证升级为每个 pass 之后都验证。
4. **错误直觉：`-run-pass` 可以直接跑 `.ll`。** 正确认识：它只接受 `.mir`，`.ll` 会报 `run-pass is for .mir file only`。
5. **错误直觉：`LiveVariables` 还是寄存器分配的标准分析。** 正确认识：当前树里没有这个类；虚拟寄存器 liveness 看 `LiveIntervals`，物理寄存器/栈槽看 `LiveStacks` 等。

## 动手练习

1. 用 `-stop-after=finalize-isel` 生成 `min.mir`，删掉 `registers:` 里的一个条目，观察 parser 的报错，并解释它为什么不是 verifier 报错。
2. 把 `tiny.mir` 的 `%0` 改成 `%5`（同时在 `registers` 里声明 id 5），跑 `-run-pass=none`，对比输出里的编号变化。
3. 给 `tiny.mir` 加一个没有 `liveins` 的第二个基本块，用 `-verify-machineinstrs` 检查是否出现 live-in 相关错误。
4. 跑 `-run-pass=dead-mi-elimination` 和 `-run-pass=none`，解释为什么两者的输出相同。

## 本章重点整理

- `MachineFunction` 拥有 `MachineBasicBlock`，`MachineBasicBlock` 拥有 `MachineInstr`，`MachineInstr` 拥有 `MachineOperand`。
- `MachineFunctionProperties` 是 pass 之间的契约；`IsSSA`、`NoVRegs`、`Selected` 的语义不同，不能混用。
- `selected: false` 不是「ISel 没跑」，它只描述 GlobalISel 的 `InstructionSelect`。
- `MO_*` 是操作数类型，`isDef`/`isUse`/`isKill`/`isDead` 是操作数 flag，`MIFlag` 是指令级 flag。
- MIR 由 IR 段、函数 YAML、`body` 三段组成；`-stop-after` 生成，`-run-pass` 消费。
- MIR parser 加载时总会验证；`-verify-machineinstrs` 把验证频率提升到每个 pass 之后。
- `SlotIndexes` 提供位置，`LiveIntervals` 提供虚拟寄存器的 live range；`LiveVariables` 在当前树里已不存在。

## 自我检核

- [ ] 能画出 `MachineFunction` → `MachineBasicBlock` → `MachineInstr` → `MachineOperand` 的所有权链
- [ ] 能解释 `IsSSA`、`NoPHIs`、`TracksLiveness`、`NoVRegs` 分别在什么时候成立
- [ ] 能说出为什么 `selected: false` 不能用来判断 SelectionDAG 是否跑过
- [ ] 能区分 `MO_*`、操作数 flag、`MachineInstr::MIFlag` 三层概念
- [ ] 能解释 MIR parser 的加载时验证和 `-verify-machineinstrs` 的差别
- [ ] 能写出一个最小 `.mir` 并用 `-run-pass=none` round-trip
- [ ] 能根据报错定位是 parser 错误、属性错误还是 `MachineVerifier` 错误
- [ ] 知道当前树里没有 `LiveVariables`，并能用 `rg` 证明

## 延伸阅读

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`Machine Function`、`Registers`、`Body`、`Machine Operands` 各节
  - **能学到什么**：MIR 每个字段的官方语义；写 `.mir` 测试时遇到格式错误，先查这里
  - **前提**：能读懂 `-stop-after` 的输出
- **[LLVM Code Generator Design: Register Allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)**
  - **读哪里**：`Register Allocation` 与 `Live Intervals` 小节
  - **能学到什么**：`LiveIntervals` / `SlotIndexes` 在 RA 中的角色，以及为什么 SSA 是前置条件
  - **前提**：本章 24.4；第 25 章会展开 greedy allocator
- **[LLVM Command Guide: llc](https://llvm.org/docs/CommandGuide/llc.html)**
  - **读哪里**：`-run-pass`、`-stop-before`、`-stop-after`、`-verify-machineinstrs` 各节
  - **能学到什么**：这些 flag 的官方语义和限制；尤其是 `-run-pass` 只对 `.mir` 生效
  - **前提**：本章 24.3

→ [Ch 25 — 寄存器分配与机器 pass：从虚拟寄存器到物理寄存器](./25-regalloc-and-machine-passes.md)
