# 01 后端全景：从 IR 到 ELF 的十二个站位

> 目标：把“LLVM 后端”拆成十二个可以分别观察的站位，再建立一张“站位 → 产物 → 责任对象 → 源码文件”的对照表。读完本章，你看到一条 MIR、一条 MCInst 或一条重定位，都能说出它处在哪一站、由谁产生、下一站会拿它做什么。

> 环境：LLVM 23.1.4 预编译版；同版本 RISC-V 源码在 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。

## 为什么

“后端”是一个过度打包的词。有人用它指指令选择，有人用它指寄存器分配，有人用它指汇编输出。三种用法都合理，却对应完全不同的输入、输出和失败模式。把它们混在一起，就会出现典型的误判：

- 在 SelectionDAG 里找物理寄存器，找不到，以为后端没做寄存器分配。
- 在 MIR 里找 `%hi/%lo` 重定位，找不到，以为重定位丢了。
- 在 `.s` 里找 `PseudoCALL`，找不到，以为伪指令没有展开。
- 在 `.o` 里找 `auipc` 的目标地址，发现是 0，以为编码错了。

这些都不是 bug，而是把不同站位的词汇混在了一起。本章先画出十二个站位，再说明每一站由哪个对象负责。后面的章节会逐站展开；本章只要求你建立一张不会串线的地图。

地图的判断标准很朴素：**每一站都有明确的输入类型和输出类型**。输入是 IR，输出是 DAG；输入是 DAG，输出是 MIR；输入是 MIR，输出是 MCInst；输入是 MCInst，输出是字节和重定位。只要接口说清楚，读源码时就知道该在哪个文件里找什么。

## 主线

### 1.1 十二个站位：谁产生什么

一次 `llc` 的完整链路可以拆成十二站。方框里是表示，箭头旁是责任对象：

```text
  1  LLVM IR（Module / Function）
      |  IR passes + target IR passes
      v
  2  IR（仍是 IR，但已做原子展开、gather/scatter、交错访存等准备）
      |  SelectionDAGBuilder
      v
  3  SelectionDAG（节点是 ISD 操作，类型已按目标合法化）
      |  DAG combine + legalize + isel
      v
  4  SelectionDAG（合法化后、选指前的 DAG）
      |  RISCVISelDAGToDAG
      v
  5  机器指令序列（MIR 的雏形，虚拟寄存器 + 目标指令）
      |  finalize-isel
      v
  6  MIR（SSA 形式，selected，虚拟寄存器）
      |  pre-RA machine passes
      v
  7  MIR（仍含虚拟寄存器，已做 machine combine/CSE/sink 等）
      |  register coalescer + scheduler + greedy + virtregrewriter
      v
  8  MIR（物理寄存器；liveness 与 stack slot 已具体化）
      |  post-RA passes
      v
  9  MIR（物理寄存器，分支/栈帧/压缩候选已处理）
      |  pseudo expansion
      v
  10 MIR（伪指令已展开为真实指令或 MC 层可处理的形式）
      |  AsmPrinter + MCInstLower
      v
  11 MCInst（目标无关的 MC 表示，带操作数与 fixup）
      |  MCCodeEmitter + ELFStreamer
      v
  12 目标文件（.o：字节、重定位、符号表、ELF 属性）
      |  ld.lld
      v
     ELF 可执行文件 / 共享对象
```

十二站的对照表：

| 站 | 表示 | 主要责任对象 | 观察手段 | 典型产物 |
| --- | --- | --- | --- | --- |
| 1 | LLVM IR | `LLVMTargetMachine` + IR passes | `opt`、`llc -print-after=...` | `.ll` |
| 2 | 目标相关 IR | `RISCVPassConfig::addIRPasses` | `-debug-pass=Structure` | 改写后的 IR |
| 3 | SelectionDAG | `SelectionDAGBuilder` | `-debug-only=isel`（release 不可用） | DAG |
| 4 | 合法化 DAG | `RISCVTargetLowering` | `-print-after=...` | 合法 DAG |
| 5 | 机器指令 | `RISCVISelDAGToDAG` | `-stop-after=riscv-isel` | 初始 MIR |
| 6 | finalize 后 MIR | `FinalizeISel` + `RISCVPassConfig` | `-stop-after=finalize-isel` | SSA MIR |
| 7 | pre-RA MIR | machine passes | `-stop-after=...` | 优化后 MIR |
| 8 | post-RA MIR | `RegAllocGreedy` + `VirtRegRewriter` | `-stop-before=riscv-copyelim` | 物理寄存器 MIR |
| 9 | post-RA 优化 MIR | `RISCVPassConfig` 的 post-RA pass | `-stop-after=prolog-epilog` | 栈帧完整 MIR |
| 10 | 展开后 MIR | `RISCVExpandPseudoInsts` | `-stop-after=riscv-expand-pseudo` | 真实指令 MIR |
| 11 | MCInst | `RISCVAsmPrinter` / `RISCVMCInstLower` | `-show-mc-inst`（如可用） | MCInst 流 |
| 12 | 目标文件 | `RISCVMCCodeEmitter` / `RISCVELFStreamer` | `llvm-readobj`、`llvm-objdump` | `.o`、重定位、ELF 属性 |

这张表有一个容易漏掉的事实：**第 2 站不改变表示层**。输入是 IR，输出还是 IR，只是做了目标相关的准备。真正的表示切换发生三次：IR 到 DAG（第 3 站），DAG 到 MIR（第 5 站），MIR 到 MCInst（第 11 站）。把三次切换记住，后面就不会在同一层里找不属于它的东西。

### 1.2 六个对象家族：谁拥有谁

RISC-V 后端里最常打交道的六个对象是：

| 对象 | 生命周期 | 回答什么问题 | RISC-V 实现 |
| --- | --- | --- | --- |
| `TargetMachine` | 每个目标一个，进程级 | 数据布局、代码模型、重定位模型、目标文件格式 | `RISCVTargetMachine` |
| `TargetSubtarget` | 每个函数/特性组合一个，按 key 缓存 | 这个函数启用了哪些扩展、CPU 是什么、ABI 是什么 | `RISCVSubtarget` |
| `TargetLowering` | 每个 subtarget 一个 | 某类 ISD 操作怎么合法化、怎么 lowering、调用约定怎么处理 | `RISCVTargetLowering` |
| `TargetRegisterInfo` | 每个 subtarget 一个 | 寄存器类、别名、spill/restore、callee-saved | `RISCVRegisterInfo` |
| `TargetInstrInfo` | 每个 subtarget 一个 | 指令属性、copyPhysReg、store/load 到栈槽、分支分析 | `RISCVInstrInfo` |
| `TargetFrameLowering` | 每个 subtarget 一个 | 栈帧布局、prologue/epilogue、callee-saved 位置 | `RISCVFrameLowering` |

所有权关系可以压成一张图：

```text
RISCVTargetMachine
  ├─ TLOF: RISCVELFTargetObjectFile（TargetLoweringObjectFile）
  ├─ MRI: MCRegisterInfo
  ├─ MII: MCInstrInfo
  ├─ STI: MCSubtargetInfo（模块级，供 AsmPrinter 写 .attribute）
  ├─ MCAsmInfo
  └─ SubtargetMap: key -> RISCVSubtarget
                              ├─ FrameLowering: RISCVFrameLowering
                              ├─ InstrInfo: RISCVInstrInfo
                              │     └─ RegInfo: RISCVRegisterInfo
                              ├─ TLInfo: RISCVTargetLowering
                              └─ TSInfo: RISCVSelectionDAGInfo
```

两个细节值得停一下。

第一，`TargetRegisterInfo` 不是 `RISCVSubtarget` 的直接成员，而是由 `RISCVInstrInfo` 持有的 `const RISCVRegisterInfo RegInfo`。`RISCVSubtarget::getRegisterInfo()` 的实现是 `return &InstrInfo.getRegisterInfo();`。所以“寄存器信息属于指令信息”在这棵实现里是字面意义上的所有权关系。

第二，`TargetLowering` 不是 pass。它是一个查询/降低接口，被 SelectionDAG 的 legalizer、DAG combiner、ISel 以及调用约定 lowering 调用。它的生命周期跟着 subtarget，不跟着某个函数。把 `TargetLowering` 当成“一个 pass”会导致在 pass 列表里找不到它，然后误以为它不存在。

### 1.3 创建顺序：为什么顺序不能乱

创建顺序可以从 `RISCVTargetMachine` 的构造函数和 `RISCVSubtarget` 的构造函数读出来。真实顺序如下：

```text
llc
  -> TargetRegistry::createTargetMachine(...)
  -> RISCVTargetMachine 构造函数
       1. CodeGenTargetMachineImpl 基类构造
          - 计算 DataLayout（TT.computeDataLayout(ABIName)）
          - 保存 Triple、CPU、FS、Options、RelocModel、CodeModel、OptLevel
       2. 成员初始化 TLOF(createTLOF(TT))
          - ELF 目标 -> RISCVELFTargetObjectFile
       3. 构造函数体 initAsmInfo()
          - MRI  = TheTarget.createMCRegInfo(Triple)
          - MII  = TheTarget.createMCInstrInfo()
          - STI  = TheTarget.createMCSubtargetInfo(Triple, CPU, FS)
          - AsmInfo = TheTarget.createMCAsmInfo(*MRI, Triple, MCOptions)
       4. setMachineOutliner(true)、setSupportsDebugEntryValues(true)、setCFIFixup(...)
  -> 第一个函数进入 getSubtargetImpl(F)
       5. 读取函数属性 target-cpu / tune-cpu / target-features
       6. 读取模块 flag target-abi，必要时校验 -target-abi 一致性
       7. 以 RVVMin/RVVMax + CPU + TuneCPU + FS 为 key 查 SubtargetMap
       8. 未命中则 new RISCVSubtarget(...)
  -> RISCVSubtarget 构造函数
       9. 基类 RISCVGenSubtargetInfo 构造
      10. 成员初始化 FrameLowering(initializeSubtargetDependencies(...))
          - ParseSubtargetFeatures(CPU, TuneCPU, FS)
          - RISCV::updateCZceFeatureImplications
          - TargetABI = RISCVABI::computeTargetABI(...)
          - RISCVFeatures::validate(Triple, featureBits)
      11. InstrInfo(*this)（内部持有 RegInfo）
      12. TLInfo(TM, *this)
      13. 构造函数体 TSInfo = make_unique<RISCVSelectionDAGInfo>()
```

顺序里有三个“必须先发生”的依赖：

1. **特性先于 ABI**。`initializeSubtargetDependencies` 先 `ParseSubtargetFeatures`，再 `computeTargetABI`。ABI 的 F/D 变体依赖特性位，顺序反了就会算出错误的 ABI。
2. **ABI 先于 TargetLowering**。`TLInfo(TM, *this)` 在成员初始化列表里位于 `InstrInfo` 之后，此时 `TargetABI` 已经算好，`TargetLowering` 构造时可以直接读。
3. **FrameLowering 先于 InstrInfo/TLInfo**。`FrameLowering` 的初始化表达式调用 `initializeSubtargetDependencies`，它返回 `*this`，因此特性、ABI、校验都在 `InstrInfo` 和 `TLInfo` 构造前完成。

这也解释了一个常见困惑：为什么 `RISCVSubtarget` 的构造函数看起来只有三行成员初始化，却能保证 `TLInfo` 拿到完整的特性信息。答案在 `FrameLowering` 的初始化表达式里。

### 1.4 RISCV.td 的 include 图

`RISCV.td` 是总入口。真实的 include 顺序（节选，保留语义分组）如下：

```tablegen
include "llvm/Target/Target.td"

include "RISCVFeatures.td"
include "RISCVProfiles.td"
include "RISCVSystemOperands.td"

include "RISCVRegisterInfo.td"
include "RISCVSchedule.td"
include "RISCVCallingConv.td"
include "RISCVInstrInfo.td"
include "GISel/RISCVRegisterBanks.td"

include "RISCVInstrPredicates.td"
include "RISCVMacroFusion.td"

include "RISCVSchedAndes45.td"
include "RISCVSchedGenericOOO.td"
include "RISCVSchedMIPSP8700.td"
include "RISCVSchedRocket.td"
include "RISCVSchedSiFive7.td"
include "RISCVSchedSiFiveP400.td"
include "RISCVSchedSiFiveP500.td"
include "RISCVSchedSiFiveP600.td"
include "RISCVSchedSiFiveP800.td"
include "RISCVSchedSpacemitX60.td"
include "RISCVSchedSpacemitX100.td"
include "RISCVSchedSyntacoreSCR1.td"
include "RISCVSchedSyntacoreSCR345.td"
include "RISCVSchedSyntacoreSCR7.td"
include "RISCVSchedTTAscalonX.td"
include "RISCVSchedXiangShanNanHu.td"

include "RISCVProcessors.td"
include "RISCVPfmCounters.td"
```

末尾定义 `RISCVInstrInfo : InstrInfo`、`RISCVAsmParser : AsmParser`、`RISCVAsmWriter : AsmWriter` 和 `RISCV : Target`。`RISCVInstrInfo` 里 `guessInstructionProperties = false`，这意味着指令属性不会被 TableGen 自动猜测，而是在 `.td` 里显式写出。

`RISCVInstrInfo.td` 再引入各扩展。真实的 include 开头是：

```tablegen
include "RISCVInstrFormats.td"
include "RISCVInstrFormatsC.td"
include "RISCVInstrFormatsV.td"
include "RISCVInstrFormatsXAIF.td"
include "RISCVInstrInfoM.td"
include "RISCVInstrInfoA.td"
include "RISCVInstrInfoZa.td"
include "RISCVInstrInfoZalasr.td"
include "RISCVInstrInfoZimop.td"
include "RISCVInstrInfoZicbo.td"
include "RISCVInstrInfoZicond.td"
include "RISCVInstrInfoZilsd.td"
include "RISCVInstrInfoZibi.td"
include "RISCVInstrInfoF.td"
include "RISCVInstrInfoD.td"
include "RISCVInstrInfoQ.td"
include "RISCVInstrInfoZfh.td"
include "RISCVInstrInfoZfbfmin.td"
include "RISCVInstrInfoZfa.td"
include "RISCVInstrInfoZb.td"
include "RISCVInstrInfoZk.td"
include "RISCVInstrInfoV.td"
...
include "RISCVInstrInfoC.td"
include "RISCVInstrInfoZc.td"
...
include "RISCVInstrGISel.td"
```

这个顺序传达三条信息：

1. **格式先于指令**。`RISCVInstrFormats.td` 定义 `InstFormat` 与 `RVInstR/I/S/B/U/J` 等基类；后面的扩展文件才能继承这些格式。
2. **基础指令先于扩展**。`RISCVInstrInfo.td` 先定义基础整数指令（`ADD` 在 23.1.4 的第 818 行），再通过 include 引入 M/A/F/D/V 等扩展。
3. **调度资源先于具体处理器模型**。`RISCVSchedule.td` 在 `RISCVInstrInfo.td` 之前定义资源与 itinerary，具体处理器的 `RISCVSched*.td` 在 `RISCV.td` 里稍后引入，最后才是 `RISCVProcessors.td`。

`RISCVInstrFormats.td` 的核心是 `InstFormat`：

```tablegen
class InstFormat<bits<5> val> {
  bits<5> Value = val;
}
def InstFormatPseudo : InstFormat<0>;
def InstFormatR      : InstFormat<1>;
def InstFormatI      : InstFormat<3>;
def InstFormatS      : InstFormat<4>;
def InstFormatB      : InstFormat<5>;
def InstFormatU      : InstFormat<6>;
def InstFormatJ      : InstFormat<7>;
```

`RVInstCommon` 把 `Format.Value` 写进 `TSFlags{4-0}`。这个字段会在 MC 层被 `RISCVMCCodeEmitter` 读取，用来决定 fixup 形式。于是“格式”不只是文档分类，它是一条从 `.td` 到编码器的数据通路。
### 1.5 每章对应哪一站

下面这张表把课程的 30 篇材料钉到十二个站位上。“主站”是这一章的主战场，“相关站”是会用到的上下游。

| 篇目 | 主站 | 相关站 | 一句话职责 |
| --- | --- | --- | --- |
| `00-environment-and-baseline.md` | 工具层 | 全部 | 钉版本、目标注册、证据约定 |
| `01-backend-map.md` | 全部 | 全部 | 建立十二站地图与对象家族 |
| `02-isa-modeling.md` | 输入约束 | 3、4、5、8、12 | XLEN、扩展、寄存器、格式、端序 |
| `03-pipeline-and-mir.md` | 1-11 | 全部 | pass 管线与 MIR 观察点 |
| `practice-a-pipeline-dissection.md` | 1-12 | 全部 | 六段证据链 |
| `04-tablegen-language.md` | 5、6 | 4、7、12 | TableGen 语法与记录 |
| `05-generated-artifacts.md` | 5、11 | 6、12 | 六种 generator 的产物 |
| `06-instruction-encoding.md` | 12 | 5、9 | R/I/S/B/U/J 与 RVC 编码 |
| `07-operands-and-patterns.md` | 4、5 | 3、6 | Operand、ComplexPattern、DAG pattern |
| `practice-b-custom-instruction.md` | 4-12 | 全部 | 新增一条指令的全链路 |
| `08-asm-parser.md` | 12 | 11 | AsmParser 与匹配表 |
| `09-disassembler.md` | 12 | 11 | 反汇编与 llvm-mc 工作流 |
| `10-mc-layer-and-objects.md` | 11、12 | 6、9 | MC 层与目标文件 |
| `11-registers-and-classes.md` | 7、8 | 5、9 | 寄存器类、别名、spill |
| `12-calling-convention.md` | 3、5、6 | 7、8 | 调用约定与参数传递 |
| `13-frame-lowering.md` | 9、10 | 8、11 | 栈帧、prologue/epilogue |
| `practice-c-abi-audit.md` | 7-10 | 5、6 | ABI 与栈帧审计 |
| `14-legalization.md` | 4 | 3、5 | 类型/操作合法化 |
| `15-selection-dag.md` | 3、4、5 | 6 | DAG 构建、combine、isel |
| `16-custom-lowering-and-intrinsics.md` | 4、5 | 3、6 | 自定义 lowering 与 intrinsic |
| `17-globalisel.md` | 3-5 的替代路径 | 7、8 | GlobalISel 的 IRTranslator/Legalizer/Selector |
| `18-scheduling-and-mca.md` | 8、9 | 18 的 mca | 调度模型与 llvm-mca |
| `19-pseudo-expansion.md` | 10 | 9、11 | 伪指令展开的 pre/post-RA 两套 |
| `20-asm-printer.md` | 11 | 10、12 | MIR 到 MCInst 与汇编输出 |
| `practice-d-scheduling-and-linking.md` | 8-12 | 全部 | 调度与链接审计 |
| `21-relocations-and-linking.md` | 12 | 9、11 | 重定位、代码模型、ld.lld |
| `22-constant-pool-got-tls.md` | 2、11、12 | 9 | 常量池、GOT/PLT、TLS |
| `23-vector-and-custom-extension.md` | 3-12 | 全部 | RVV 与自定义扩展 |
| `final-project-riscv-backend.md` | 全部 | 全部 | 端到端综合 |
| `README.md` | 地图 | 全部 | 课程入口与导航 |

用法：读某一章时，先看“主站”，再看“相关站”。例如读第 12 章调用约定，主站是 3/5/6，相关站是 7/8；这意味着你会在 DAG lowering、isel 和 MIR 里看到调用约定，而不只是在 ABI 文档里看到它。

## 可跑实验

### 实验 1：把 pass 管线打印成站位清单

目的：用真实的 pass 名验证十二站不是纸面分类。

命令：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 -debug-pass=Structure first.ll -o NUL 2>&1 |
  Select-String -Pattern 'riscv-|greedy|prolog-epilog|register-coalescer|machine-scheduler'
```

真实输出里会看到这些名字（节选）：

```text
riscv-gather-scatter-lowering
interleaved-access
riscv-codegenprepare
riscv-promote-const
riscv-isel
finalize-isel
riscv-vl-optimizer
riscv-vector-peephole
riscv-fold-mem-offset
machine-combiner
machine-cse
machine-sink
riscv-opt-w-instrs
riscv-prera-expand-pseudo
riscv-merge-base-offset
riscv-insert-read-write-csr
riscv-insert-write-vxrm
riscv-vmv0-elimination
register-coalescer
machine-scheduler
greedy
riscv-insert-vsetvli
riscv-dead-defs
riscv-copyelim
prolog-epilog
postrapseudos
riscv-post-ra-expand-pseudo
riscv-load-store-opt
postmisched
block-placement
riscv-late-branch-opt
branch-relaxation
riscv-make-compressible
stack-frame-layout
riscv-move-merge
riscv-push-pop-opt
riscv-expand-pseudo
riscv-expand-atomic-pseudo
riscv-asm-printer
```

解读：这些名字可以按站位分组。`riscv-isel`/`finalize-isel` 属于第 5/6 站；`machine-*` 与 `riscv-opt-w-instrs` 属于第 7 站；`register-coalescer`/`greedy` 属于第 8 站；`prolog-epilog`/`riscv-copyelim` 属于第 9 站；`riscv-expand-pseudo` 属于第 10 站；`riscv-asm-printer` 属于第 11 站。

失败排查：

- 只看到 `Pass Arguments:` 一行、看不到下面缩进的 pass 列表：`-debug-pass=Structure` 的输出走 stderr，PowerShell 里必须加 `2>&1`，否则 `Select-String` 可能只拿到 stdout。
- 完全没有任何 pass 名：确认 `llc` 是 23.1.4，且 `-O2` 没有写成 `-O 2`。

### 实验 2：打印 RISCV.td 的 include 图

目的：确认“格式先于指令、指令先于调度模型”的层次。

命令：

```powershell
$td = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
Select-String -LiteralPath "$td\RISCV.td" -Pattern '^include' | ForEach-Object { $_.Line }
Select-String -LiteralPath "$td\RISCVInstrInfo.td" -Pattern '^include' |
  Select-Object -First 12 | ForEach-Object { $_.Line }
```

真实输出开头：

```text
include "llvm/Target/Target.td"
include "RISCVFeatures.td"
include "RISCVProfiles.td"
include "RISCVSystemOperands.td"
include "RISCVRegisterInfo.td"
include "RISCVSchedule.td"
include "RISCVCallingConv.td"
include "RISCVInstrInfo.td"
include "GISel/RISCVRegisterBanks.td"
```

`RISCVInstrInfo.td` 的开头：

```text
include "RISCVInstrFormats.td"
include "RISCVInstrFormatsC.td"
include "RISCVInstrFormatsV.td"
include "RISCVInstrFormatsXAIF.td"
include "RISCVInstrInfoM.td"
include "RISCVInstrInfoA.td"
include "RISCVInstrInfoZa.td"
include "RISCVInstrInfoZalasr.td"
include "RISCVInstrInfoZimop.td"
include "RISCVInstrInfoZicbo.td"
include "RISCVInstrInfoZicond.td"
include "RISCVInstrInfoZilsd.td"
```

解读：`RISCVInstrFormats.td` 是格式基类，必须最早；M/A/... 扩展文件继承这些格式。`RISCVSchedule.td` 在 `RISCVInstrInfo.td` 之前定义调度资源，具体处理器的 `RISCVSched*.td` 在 `RISCV.td` 里稍后引入。

失败排查：`Select-String` 找不到文件，说明 `$td` 拼错。用 `Test-Path "$td\RISCV.td"` 先确认。

### 实验 3：在第 6 站停一下

目的：看到 DAG 选指后的 MIR 雏形，确认“虚拟寄存器 + 目标指令”这一层。

命令：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=riscv-isel first.ll -o -
```

真实的 `mul_add` 函数体：

```text
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11

    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %2:gpr = MUL %0, %1
    %3:gpr = ADDI killed %2, 7
    $x10 = COPY %3
    PseudoRET implicit $x10
```

解读：`%0`、`%1`、`%2`、`%3` 是虚拟寄存器，`gpr` 是寄存器类；`$x10`、`$x11` 是物理寄存器；`PseudoRET` 是还没展开的伪指令；`killed` 是操作数标志。这一站还没有物理寄存器分配，也没有栈帧。

失败排查：如果输出 `selected: false`，不要立刻认为没选指。这个头部字段在 `-stop-after` 生成的 MIR 里可能是陈旧值；看函数体里是否已经是 `MUL`/`ADDI` 这类目标指令。第 03 章会专门讲这个坑。

### 实验 4（边界）：`.attribute 5` 与函数级 `.option arch`

目的：看清“文件级属性”与“函数级特性”不是一回事。

命令：

```powershell
$ll = Join-Path $env:TEMP "fnattr.ll"
$lines = @(
  'target triple = "riscv64-unknown-elf"'
  'define i64 @f(i64 %a, i64 %b) #0 {'
  '  %m = mul i64 %a, %b'
  '  ret i64 %m'
  '}'
  'attributes #0 = { "target-features"="+m" }'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
llc -mtriple=riscv64 -O2 $ll -o -
```

真实输出节选：

```asm
	.attribute	4, 16
	.attribute	5, "rv64i2p1"
	.file	"fnattr.ll"
	.option	push
	.option	arch, +m, +zmmul
	.text
	.globl	f
f:
	mul	a0, a0, a1
	ret
	.option	pop
```

解读：函数级 `target-features="+m"` 让函数体选出了 `mul`，汇编器用 `.option push` / `.option arch, +m, +zmmul` / `.option pop` 把这段代码包起来；文件级 `.attribute 5` 仍然是 `rv64i2p1`。所以不能用 `.attribute 5` 反推“这个模块里没有任何函数用 M”。

失败排查：如果函数体没有 `mul`，检查 `define` 末尾是否真的带 `#0`。属性组定义了但没被引用，是 `.ll` 里常见的静默错误。

## 源码落点

- `RISCVTargetMachine.cpp`：`RISCVTargetMachine::RISCVTargetMachine` 的成员初始化与 `initAsmInfo()`；`createTLOF`；`getSubtargetImpl`；`LLVMInitializeRISCVTarget`。
- `RISCVTargetMachine.cpp`：`RISCVPassConfig::addIRPasses`、`addPreISel`、`addInstSelector`、`addPreRegAlloc`、`addPostRegAlloc`、`addPreEmitPass`、`addPreEmitPass2`。
- `RISCVSubtarget.h` / `RISCVSubtarget.cpp`：成员声明顺序 `FrameLowering`、`InstrInfo`、`TLInfo`；构造函数里的 `initializeSubtargetDependencies`、`TLInfo(TM, *this)`、`TSInfo`；`getRegisterInfo()` 返回 `InstrInfo.getRegisterInfo()`。
- `RISCVInstrInfo.h`：`const RISCVRegisterInfo RegInfo;` 与 `getRegisterInfo()`。
- `CodeGenTargetMachineImpl.cpp`：`initAsmInfo()` 创建 `MRI`、`MII`、`STI`、`MCAsmInfo`。
- `RISCV.td`：总 include 图与 `def RISCV : Target`。
- `RISCVInstrInfo.td`：基础整数指令与扩展 include；`def ADD` 在 23.1.4 第 818 行。
- `RISCVInstrFormats.td`：`InstFormat`、`RVInstCommon`、`RVInstR/I/S/B/U/J`；`TSFlags{4-0}` 写入格式值。
- `RISCVAsmPrinter.cpp`：`emitAttributes`、`emitTargetFeaturePush`、`emitTargetFeaturePop`、`runOnMachineFunction`。

## 常见坑

1. **把 `TargetLowering` 当成一个 pass**。它不在 pass 列表里，而是被 DAG legalizer、combiner、ISel 和调用约定 lowering 调用的接口。找不到它是正常的。
2. **以为 `TargetSubtarget` 是全局唯一**。`getSubtargetImpl(F)` 按函数属性与 RVV 参数构造 key，缓存到 `SubtargetMap`。不同函数可以拿到不同的 subtarget。
3. **以为 `TargetRegisterInfo` 是独立成员**。在这棵实现里它由 `RISCVInstrInfo` 持有，`RISCVSubtarget::getRegisterInfo()` 只是转发。
4. **用 `.attribute 5` 判断函数级特性**。文件级属性和函数级 `.option arch` 是两套东西。实验 4 的输出就是反例。
5. **把 `.td` 的 include 顺序当成无关紧要**。格式基类必须先于指令，指令必须先于调度模型。顺序错了，TableGen 会报未定义记录，或者生成错误的 opcode 编号。
6. **拿 `D:\llvm-project` 的行号解释 23.1.4 的生成物**。两棵树版本不同，opcode 编号和记录顺序都可能变。生成物只和同版本 `.td` 对齐。
7. **把 `-debug-pass=Structure` 当成调试日志**。它打印的是 pass 结构，不是每条指令的变换。要看指令级变化，用 `-print-after` 或 `-stop-after`。

## 自我检查

- [ ] 我能按顺序说出 IR → DAG → MIR → MCInst → ELF 的十二个站位。
- [ ] 我能把 `riscv-isel`、`finalize-isel`、`greedy`、`prolog-epilog`、`riscv-expand-pseudo`、`riscv-asm-printer` 分别归到正确的站。
- [ ] 我能解释 `TargetMachine`、`TargetSubtarget`、`TargetLowering`、`TargetRegisterInfo`、`TargetInstrInfo`、`TargetFrameLowering` 各自回答什么问题。
- [ ] 我能画出 `RISCVTargetMachine` → `RISCVSubtarget` → `FrameLowering`/`InstrInfo`/`TLInfo` 的创建顺序，并说出三个“必须先发生”的依赖。
- [ ] 我能解释 `RISCVInstrFormats.td` 为什么必须先于 `RISCVInstrInfoM.td` 等扩展文件。
- [ ] 我能解释 `.attribute 5` 与函数级 `.option arch` 的区别，并给出实验 4 的证据。
- [ ] 我能把课程任意一篇材料映射到十二站中的至少一个主站。

## 延伸阅读

- LLVM 官方文档：`llvm.org/docs/CodeGenerator.html`，重点看 “The LLVM Target-Independent Code Generator” 的分层图。
- LLVM 官方文档：`llvm.org/docs/TableGen/BackEnds.html`，理解 `.td` 记录如何变成 `.inc`。
- LLVM 官方文档：`llvm.org/docs/MIRLangRef.html`，为第 03 章的 MIR 语法做准备。
- 同版本源码：`RISCVTargetMachine.cpp`、`RISCVSubtarget.cpp`、`RISCVSubtarget.h`、`RISCVInstrInfo.h`、`RISCV.td`、`RISCVInstrInfo.td`、`RISCVInstrFormats.td`。
- 同版本核心源码：`.scratch/llvm-23.1.4-src/llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp` 的 `initAsmInfo()`。

[上一章：环境与基线](./00-environment-and-baseline.md) | [下一章：ISA 建模](./02-isa-modeling.md)