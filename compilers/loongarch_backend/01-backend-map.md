# Ch 01 — 后端全景：一条 LoongArch 指令经过的十二个站位

> 本章目标：建立一张“从 IR 到 ELF”的地图。之后每章只深入一个站位，但你始终知道它在整条链的哪里。

---

## 1.1 先看一条最小的函数

```llvm
define i64 @add(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
```

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 /tmp/la-smoke.ll -o -
```

实测输出（节选）：

```asm
	.text
	.globl	add
	.p2align	2
	.prefalign	5, .Lfunc_end0, nop
	.type	add,@function
add:
	.cfi_startproc
# %bb.0:
	add.d	$a0, $a0, $a1
	ret
.Lfunc_end0:
	.size	add, .Lfunc_end0-add
	.cfi_endproc
```

这条 `add.d` 不是“IR 里写了 `add`，后端就选 `ADD_D`”这么简单。它经过了下面十二个站位。

---

## 1.2 十二个站位

| # | 站位 | 输入 → 输出 | 主要文件 | 本课章节 |
|---|---|---|---|---|
| 1 | IR passes | LLVM IR → 更规范的 IR | `llvm/lib/Transforms/*` + target hooks | Ch 03 |
| 2 | 合法化 | IR/DAG 类型 → 目标合法类型 | `LoongArchISelLowering.cpp` 构造函数 | Ch 13 |
| 3 | DAG 构建 | IR → `SelectionDAG` | `SelectionDAGBuilder` + target lowering | Ch 13–15 |
| 4 | 自定义 lowering | 非法 DAG 节点 → 目标节点 | `LowerOperation`、`lower*` | Ch 15 |
| 5 | DAG→DAG 选指 | 目标节点 → `MachineSDNode` | `LoongArchISelDAGToDAG.cpp` + `.td` pattern | Ch 14 |
| 6 | 指令调度/机器 SSA | DAG → `MachineFunction` | `LoongArchInstrInfo.cpp`、`MachineScheduler` | Ch 23 |
| 7 | Pre-RA 伪指令展开 | `PseudoLA_*` → 真实指令 | `LoongArchExpandPseudoInsts.cpp` | Ch 18 |
| 8 | 寄存器分配 | 虚拟寄存器 → 物理寄存器 | `LoongArchRegisterInfo.cpp`、`InstrInfo.cpp` | Ch 22 |
| 9 | Post-RA 展开/优化 | 机器指令 → 机器指令 | `LoongArchExpandPseudoInsts.cpp`、`LateBranchOpt.cpp`、`MemoryBarrierOpt.cpp` | Ch 18–21 |
| 10 | AsmPrinter / MC | `MachineInstr` → `MCInst` → 字节 | `LoongArchAsmPrinter.cpp`、`MCTargetDesc/*` | Ch 06–07 |
| 11 | 汇编/对象 | `.s` / `.o` | `AsmParser`、`MCCodeEmitter`、`AsmBackend` | Ch 06–07 |
| 12 | 链接 | `.o` → ELF | `lld/ELF/Arch/LoongArch.cpp` | Ch 12 |

记住一句话：**前 9 站在 `llvm/lib/Target/LoongArch`，第 10–11 站在 `MCTargetDesc`/`AsmParser`，第 12 站在 `lld`。** 跨站调试时，先问“我现在在哪一站”，再去对应目录。

---

## 1.3 对象家族

```text
Module
  └─ Function
       └─ BasicBlock
            └─ Instruction
                 │  SelectionDAGBuilder
                 ▼
            SelectionDAG
              ├─ SDNode（ISD::ADD、ISD::LOAD、LoongArchISD::CALL...）
              └─ 类型 EVT/MVT（i64、f64、v4i32...）
                 │  DAGToDAGISel + pattern
                 ▼
            MachineFunction
              └─ MachineBasicBlock
                   └─ MachineInstr（ADD_D、PseudoLA_TLS_LE...）
                        │  AsmPrinter / MCInstLower
                        ▼
                   MCInst（opcode + MCOperand）
                        │  MCCodeEmitter + fixup
                        ▼
                   4 字节编码 + relocation
                        │  ELFObjectWriter
                        ▼
                   .o（.text + .rela.text + .symtab）
                        │  ld.lld
                        ▼
                   ELF executable / shared object
```

每一层都有自己的“指令表示”：

| 层 | 指令表示 | 例子 |
|---|---|---|
| IR | `Instruction` | `%r = add i64 %a, %b` |
| DAG | `SDNode` | `ISD::ADD` / `LoongArchISD::CALL` |
| 机器 | `MachineInstr` | `ADD_D $a0, $a0, $a1` |
| MC | `MCInst` | `ADD_D` + 3 个 `MCOperand` |
| 对象 | 32 位字 + 重定位 | `0x00108c84` + `R_LARCH_*` |

---

## 1.4 文件地图：按“你改什么”来分

### 4.1 描述层（TableGen）

| 文件 | 你会在什么时候改它 |
|---|---|
| `LoongArch.td` | 加 Feature、CPU、Schedule 引用 |
| `LoongArchInstrFormats.td` | 加一种指令格式（`Fmt2R`/`Fmt3R`/...） |
| `LoongArchInstrInfo.td` | 加标量/浮点指令、pattern、SDNode |
| `LoongArchFloat32InstrInfo.td` / `Float64InstrInfo.td` | 加 FP 指令 |
| `LoongArchLSXInstrInfo.td` / `LASXInstrInfo.td` | 加 128/256 位向量指令 |
| `LoongArchLBTInstrInfo.td` / `LVZInstrInfo.td` | 加 LBT/LVZ 指令 |
| `LoongArchRegisterInfo.td` | 加寄存器/寄存器类 |
| `LoongArchCallingConv.td` | 改 callee-saved 集合 |
| `LoongArchSchedule.td` + `SchedLA*.td` | 改调度资源/延迟 |

### 4.2 代码生成层（C++）

| 文件 | 职责 | 典型符号 |
|---|---|---|
| `LoongArchTargetMachine.cpp` | 注册 target、pass pipeline | `LoongArchPassConfig` |
| `LoongArchSubtarget.cpp` | CPU/Feature/ABI 初始化 | `initializeSubtargetDependencies` |
| `LoongArchISelLowering.cpp` | 合法化、调用约定、全局地址/TLS、intrinsic、combine | `LowerOperation`、`lowerGlobalTLSAddress` |
| `LoongArchISelDAGToDAG.cpp` | DAG pattern 匹配的 C++ 补充 | `Select`、`SelectAddr` |
| `LoongArchInstrInfo.cpp` | 分支分析、spill/restore、`movImm` | `analyzeBranch`、`copyPhysReg` |
| `LoongArchRegisterInfo.cpp` | frame index 消除、spill slot | `eliminateFrameIndex` |
| `LoongArchFrameLowering.cpp` | prologue/epilogue、栈探测、shrink-wrap | `emitPrologue`、`adjustReg` |
| `LoongArchExpandPseudoInsts.cpp` | 伪指令展开（Pre-RA/Post-RA） | `expandLoadAddressTLSLE` |
| `LoongArchMergeBaseOffset.cpp` | `pcalau12i + addi` 折叠 | `detectFoldable` |
| `LoongArchOptWInstrs.cpp` | `W` 后缀优化 | `convertToDSuffixes` |
| `LoongArchLateBranchOpt.cpp` | 分支反转/跳转消除 | `runOnMachineFunction` |
| `LoongArchMemoryBarrierOpt.cpp` | `dbar` 优化 | `runOnMachineFunction` |
| `LoongArchDeadRegisterDefinitions.cpp` | 死定义 → `$r0` | `runOnMachineFunction` |
| `LoongArchAsmPrinter.cpp` | 汇编输出、`.option`/`.attribute` | `emitInstruction` |
| `LoongArchTargetTransformInfo.cpp` | 成本模型 | `getArithmeticInstrCost` |
| `LoongArchSelectionDAGInfo.cpp` | memcpy/memcmp 的 DAG 钩子 | `getMemcmp` |

### 4.3 MC 层

| 文件 | 职责 |
|---|---|
| `MCTargetDesc/LoongArchMCCodeEmitter.cpp` | `MCInst` → 32 位编码 + fixup |
| `MCTargetDesc/LoongArchAsmBackend.cpp` | fixup 解析、重定位生成 |
| `MCTargetDesc/LoongArchELFObjectWriter.cpp` | `.o` 的重定位类型/符号 |
| `MCTargetDesc/LoongArchELFStreamer.cpp` | `.option`/`.attribute` 等指令 |
| `MCTargetDesc/LoongArchInstPrinter.cpp` | `MCInst` → 汇编文本 |
| `MCTargetDesc/LoongArchMatInt.cpp` | 常量物化（`lu12i.w`/`ori`/...） |
| `AsmParser/LoongArchAsmParser.cpp` | 汇编文本 → `MCInst` |
| `Disassembler/LoongArchDisassembler.cpp` | 字节 → `MCInst` |

### 4.4 Clang / LLD / 运行时

| 文件 | 职责 |
|---|---|
| `clang/lib/Basic/Targets/LoongArch.cpp` | 宏、类型、ABI、feature 校验 |
| `clang/lib/Driver/ToolChains/Arch/LoongArch.cpp` | `-march`/`-mabi`/`-mfpu`/multilib |
| `clang/lib/CodeGen/Targets/LoongArch.cpp` | builtin → IR intrinsic |
| `clang/lib/Sema/SemaLoongArch.cpp` | builtin 参数/feature 检查 |
| `lld/ELF/Arch/LoongArch.cpp` | 重定位、relaxation、PLT/GOT |
| `compiler-rt/lib/builtins/loongarch/` | `__*tf3` 等运行时 |
| `compiler-rt/lib/xray/xray_loongarch64.*` | XRay |

---

## 1.5 走一遍真实链路

### 5.1 IR → 汇编

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 /tmp/la-smoke.ll -o /tmp/la-smoke.s
```

### 5.2 汇编 → 对象

```bash
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -filetype=obj /tmp/la-smoke.s -o /tmp/la-smoke.o
$T/bin/llvm-readobj -h /tmp/la-smoke.o
```

实测（关键字段）：

```text
Format: elf64-loongarch
Arch: loongarch64
Type: Relocatable
Machine: EM_LOONGARCH (258)
Flags: EF_LOONGARCH_ABI_DOUBLE_FLOAT | EF_LOONGARCH_OBJABI_V1
```

`Flags` 里的 `EF_LOONGARCH_ABI_*` 就是 ABI；`EM_LOONGARCH = 258` 是 machine ID。Ch 02 会把这张表拆开。

### 5.3 对象 → ELF

```bash
$T/bin/ld.lld -m elf64loongarch -static -e add /tmp/la-smoke.o -o /tmp/la-smoke.elf
$T/bin/llvm-objdump -d --no-show-raw-insn /tmp/la-smoke.elf | sed -n '/<add>:/,+3p'
```

实测：

```text
0000000000020190 <add>:
   20190: 00108c84  add.d  $a0, $a0, $a1
   20194: 4c000020  ret
```

### 5.4 带重定位的例子

```llvm
@tvar = thread_local(localexec) global i64 7, align 8
define ptr @tls_addr() { ret ptr @tvar }
```

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+relax -filetype=obj /tmp/tlsle.ll -o /tmp/tlsle.o
$T/bin/llvm-readobj -r /tmp/tlsle.o
```

实测：

```text
0x0 R_LARCH_TLS_LE_HI20_R tvar
0x0 R_LARCH_RELAX
0x4 R_LARCH_TLS_LE_ADD_R tvar
0x4 R_LARCH_RELAX
0x8 R_LARCH_TLS_LE_LO12_R tvar
0x8 R_LARCH_RELAX
```

这就是 Ch 00 里那条被 lld 收缩的序列。**同一条指令在 `.s` 里是 `%le_hi20_r`，在 `.o` 里是 `R_LARCH_TLS_LE_HI20_R`，在 `ld.lld` 里是 `relaxTlsLe()` 的一个 case。** 三者的对应关系是读 LoongArch 工具链的主线。

---

## 1.6 LoongArch 后端的五个设计决策

### 6.1 只有 SelectionDAG，没有 GlobalISel

`grep -ril globalisel llvm/lib/Target/LoongArch` 为空；`addInstSelector()` 只注册 `createLoongArchISelDag`。这意味着：

- 你不需要同时维护 Legalizer/RegBankSelect/InstructionSelect 三套规则；
- 但新指令的 pattern 必须写在 `.td` 或 `LoongArchISelDAGToDAG.cpp`；
- 迁移 GlobalISel 是一个真实但尚未启动的长期议题（Ch 17 会讨论难点）。

### 6.2 伪指令展开分成 Pre-RA 和 Post-RA

`LoongArchPassConfig` 注册了两个 pass：

- `createLoongArchPreRAExpandPseudoPass()`：在寄存器分配前展开地址加载（`PseudoLA_*`），因为需要虚拟寄存器；
- `createLoongArchExpandPseudoPass()`：在 Post-RA 展开必须靠近指令编码的伪指令（如 `PseudoAddTPRel`）。

这解释了为什么 `LoongArchExpandPseudoInsts.cpp` 里有 `LoongArchPreRAExpandPseudo` 和 `LoongArchExpandPseudo` 两个类。

### 6.3 成对重定位与 `MO_RELAX`

LoongArch 的 linker relaxation 不靠“指令序列模式匹配”，而靠 **同 offset 的 `R_LARCH_RELAX` 标记**。后端在 `LoongArchBaseInfo.h` 里用 `MO_RELAX = 0x40` 表示“这条机器操作数要带 marker”：

```cpp
enum {
  MO_DIRECT_FLAG_MASK = 0x3f,
  MO_RELAX = 0x40
};
```

`addTargetFlags`/`getDirectFlags` 负责把 `MO_*`（如 `MO_LE_HI_R`）和 `MO_RELAX` 组合起来。到 MC 层，`LoongArchAsmBackend` 再把它变成 `.rela` 里的两条记录。

### 6.4 LA32/LA64 共用一套指令描述，用 `HwMode` 和 `W` 指令区分

`LoongArch.td` 里：

```tablegen
defvar LA32 = DefaultMode;
def LA64 : HwMode<[IsLA64]>;
```

`IsLA64`/`IsLA32` 是 `Predicate`，指令用 `Requires<[IsLA64]>` 或 pattern 里的 `GRLenVT` 区分。`ADD_W`/`ADDI_W`/`LD_W` 这些 32 位操作在 LA64 上存在，但语义是“读 32 位、写 64 位（符号扩展）”；`LoongArchOptWInstrs` 专门优化这些 `W` 指令。

### 6.5 向量扩展是“两套寄存器类、一套 lowering”

- `LSX128`：`v16i8/v8i16/v4i32/v2i64/v4f32/v2f64`
- `LASX256`：`v32i8/v16i16/v8i32/v4i64/v8f32/v4f64`

`LoongArchISelLowering` 用 `if (Subtarget.hasExtLSX())` / `hasExtLASX()` 注册寄存器类，然后大量 lowering 代码用 `VT.is128BitVector()` / `is256BitVector()` 分支。Ch 17 会专门讲这个复用模式。

---

## 1.7 建议的读代码顺序

不要从 12k 行的 `LoongArchISelLowering.cpp` 第 1 行开始读。推荐五条路径：

1. **“一条 add 怎么来的”**：`LoongArchInstrInfo.td` 的 `ADD_D` → `LoongArchISelLowering` 的合法化 → `llc -debug-pass=Structure`。
2. **“一个全局变量怎么变成重定位”**：`lowerGlobalAddress` → `getAddr` → `PseudoLA_PCREL` → `expandLoadAddressPcrel` → `MO_PCREL_HI/LO` → `AsmBackend`。
3. **“一个 TLS 变量怎么被 relax”**：`lowerGlobalTLSAddress` → `PseudoLA_TLS_LE` → `expandLoadAddressTLSLE` → `MO_LE_*_R` → lld `relaxTlsLe`。
4. **“一个 prologue 怎么来的”**：`LoongArchFrameLowering::emitPrologue` → `determineCalleeSaves` → `adjustReg`。
5. **“一个向量 shuffle 怎么选”**：`lowerVECTOR_SHUFFLE` → `LSX/LASX` pattern → `llc -mattr=+lasx`。

每条路径都对应后面的一个 Part。

---

## 1.8 反直觉点

1. **LoongArch 只有 SelectionDAG，没有 GlobalISel。** 读代码时不要找 `LoongArchGlobalISel`，它不存在（1.6 的 6.1 节）。
2. **伪指令展开不是一处，而是三处**：Pre-RA、Post-RA、原子伪指令各有一个 pass（1.6 的 6.2 节）。
3. **成对重定位 + `MO_RELAX` 是 LoongArch 特有的设计**，不能直接套 RISC-V 的 relaxation 经验（1.6 的 6.3 节）。
4. **LA32/LA64 共用一套指令描述**，靠 `HwMode` 和 `.w`/`.d` 区分，而不是两份 `InstrInfo.td`（1.6 的 6.4 节）。
5. **LSX/LASX 是两套寄存器类、一套 lowering 逻辑**：`LSX128`/`LASX256` 共享同一个 `HwEncoding`（1.6 的 6.5 节）。
6. **指令选择不是一条 pass**：`loongarch-isel` 之后还有 `finalize-isel` 和两个 `expand-pseudo`，只盯 isel 会漏掉一半的“指令变形”。

---

## 1.9 本章证据

```bash
# 1. target 注册
$T/bin/llc --version | grep -A2 loongarch

# 2. pass 结构
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 -debug-pass=Structure /tmp/la-smoke.ll -o /dev/null 2>&1 | head -5

# 3. 编码
printf 'add.d $a0, $a1, $a2\n' > /tmp/one.s
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding /tmp/one.s

# 4. 重定位
$T/bin/llvm-readobj -r /tmp/tlsle.o
```

实测编码：

```text
add.d $a0, $a1, $a2   # encoding: [0xa4,0x98,0x10,0x00]
```

`0x001098a4` 小端写成 `a4 98 10 00`；`ADD_D` 的 opcode 是 `0x00108000`，`rk=5 (a1)`、`rj=4 (a0)`、`rd=4 (a0)`。Ch 06 会手算这个字。

---

## 1.10 练习

1. 用 `llc -stop-after=loongarch-isel` 输出 MIR，找出 `ADD_D` 出现的位置，确认它前面是否还有 `Pseudo*`。
2. 把 `-O2` 换成 `-O0`，对比 `-debug-pass=Structure` 的差异；哪些 pass 消失了？
3. 用 `llvm-mc -show-encoding` 验证 `add.d`、`add.w`、`addi.d` 的 opcode 位域，画出 32 位布局。
4. 找一个 `PseudoLA_*`，跟到它的 `expandLoadAddress*`，再跟到最终的重定位类型。

---

## 1.11 延伸阅读

1. **官方七阶段与 target description 类** — `llvm/docs/CodeGenerator.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`### The high-level design of the code generator` 的七阶段编号列表，以及 `## Target description classes` 下 `TargetMachine`、`DataLayout`、`TargetLowering`、`TargetRegisterInfo`、`TargetInstrInfo`、`TargetFrameLowering`、`TargetSubtarget`、`TargetJITInfo` 八个小节（JIT 可跳过）。
   - 学到什么：把 1.2 的十二个站位映射回 LLVM 的官方七阶段，分清哪些是 target-independent 框架、哪些是 LoongArch 自己插入的 pass。
   - 前提：能读懂 `llc -debug-pass=Structure` 的一行输出（本章 1.9 有命令）。

2. **从零写后端的标准顺序** — `llvm/docs/WritingAnLLVMBackend.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`### Basic Steps` 的七步清单，再读 `## Register Set and Register Classes`、`## Instruction Set`、`## Instruction Selector` 三章的导言。
   - 学到什么：补 1.4 文件地图背后的依赖顺序：为什么先有 `TargetMachine`，再补寄存器/指令描述，最后才是 ISel 与 AsmPrinter。
   - 前提：知道 `TargetMachine`、`RegisterInfo.td`、`InstrInfo.td` 各自负责什么（本章 1.4）。

3. **LoongArch 没有 GlobalISel 意味着多少工作量** — `llvm/docs/GlobalISel/Porting.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：开头列出的四个必须实现的类（`CallLowering`、`RegisterBankInfo`、`LegalizerInfo`、`InstructionSelector`）和 `TargetPassConfig` 那一段。
   - 学到什么：把 1.6 的“只有 SelectionDAG”从结论变成可估算的迁移面：不是加一个 pass，而是补四套规则并接进 pipeline。
   - 前提：知道 SelectionDAG 的 legalize 与 ISel 大致流程（本章 1.2 的站位 2–5）。

4. **pass 到底在哪一行注册** — `llvm/lib/Target/LoongArch/LoongArchTargetMachine.cpp`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`LoongArchPassConfig` 的 `addIRPasses()`、`addInstSelector()`、`addMachineSSAOptimization()`、`addPreRegAlloc()`、`addMachineLateOptimization()`、`addPreEmitPass()`、`addPreEmitPass2()` 七个函数体。
   - 学到什么：把 1.5 的真实链路和 1.6 的 Pre-RA/Post-RA 分工落到具体注册点，读 Ch 03 时不会只背 pass 名。
   - 前提：知道 `TargetPassConfig` 的 `add*` 钩子会在固定阶段被调用（可先读 `CodeGenerator.md` 的 high-level design）。

## 1.12 下一步

下一章把地图上的“类型/Feature/ABI”这一层拆开：LA32/LA64 到底差在哪、`-mabi=lp64d` 如何变成 `e_flags`、寄存器名和调用约定怎么对应。
