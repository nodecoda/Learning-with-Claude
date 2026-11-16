# Ch 09 — TargetMachine、Subtarget 与 TargetLowering

> **目标**：能从 `llc` 的入口一路追到 `SelectionDAGISel` 和 `AsmPrinter`，说清 `TargetMachine`、`Subtarget`、`TargetLowering` 各拥有什么、在什么时候被谁调用，并能解释“异常模型关了，为什么还有 `.cfi_*`”。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。

## 为什么需要这个？

到 Ch 08，`llvm-mc` 已能把 `add x10, x11, x12` 编成四个字节。但 `llc` 的输入是 IR：它不知道你的机器有几个寄存器、`i32` 合不合法、`mul` 有没有指令、参数放哪、栈怎么对齐、符号地址怎么物化。这些答案在 CodeGen 层的三个对象里：

- `TargetMachine`：module 的“目标身份”。拥有 `TargetLoweringObjectFile`（TLOF）、数据布局、重定位/代码模型、异常模型，并创建 `Subtarget`。
- `TargetSubtargetInfo`：函数的“目标能力”。拥有 `InstrInfo`、`RegisterInfo`、`FrameLowering`、`TargetLowering`、`SelectionDAGInfo`。RISC-V 按函数属性切 subtarget；MiniRV 只有一个。
- `TargetLowering`：把“LLVM 的合法世界”翻译成“这个 target 的合法世界”的策略表：合法类型、操作动作、调用约定、寻址模式、libcall。

没有这一层，`llc` 只好像 Ch 03 那样停在 MC-only；有了它，IR 才能变成 MachineInstr，再变成汇编。

## 先建立直觉

把一次 `llc` 想成装配线：`TargetMachine` 是工厂，`Subtarget` 是工具柜，`TargetLowering` 是工艺卡，`SelectionDAGISel` 是选机床的工人，`AsmPrinter` 是贴标签出厂的包装工。

```text
                         llc -mtriple=minirv64
                                  │
                    TargetRegistry::createTargetMachine
                                  │
                     ┌────────────▼─────────────┐
                     │ MiniRVTargetMachine      │
                     │  ├─ TLOF (ELF)           │
                     │  ├─ Reloc::Static        │
                     │  ├─ ExceptionModel       │
                     │  └─ MiniRVSubtarget      │
                     └────────────┬─────────────┘
                                  │ getSubtargetImpl(F)
                     ┌────────────▼─────────────┐
                     │ MiniRVSubtarget          │
                     │  ├─ MiniRVInstrInfo      │
                     │  │    └─ MiniRVRegisterInfo
                     │  ├─ MiniRVFrameLowering  │
                     │  ├─ MiniRVTargetLowering │ ← 合法类型/动作/CC/寻址/libcall
                     │  └─ MiniRVSelectionDAGInfo
                     └────────────┬─────────────┘
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
   SelectionDAGISelPass   PrologEpilogInserter   MiniRVAsmPrinterPass
   (DAG 构建/合法化/选指)  (栈帧/CSR/CFI)         (MCInst/伪指令/汇编)
             └──────────────► MachineInstr ────────────┘
```

关键点：`TargetLowering` 不是选指器。它回答“这个操作在这个 target 上应该是什么形态”；真正把 DAG 节点匹配成 `ADD`/`MUL` 的，是 `MiniRVDAGToDAGISel::Select` 和 TableGen 生成的 pattern。把这两个角色混在一起，是读后端时最常见的第一个误解。

## 主线：从 `llc` 到一条 `add`

### 1. `MiniRVTargetMachine`：module 的目标身份

```cpp
MiniRVTargetMachine::MiniRVTargetMachine(const Target &T, const Triple &TT,
                                         StringRef CPU, StringRef FS,
                                         const TargetOptions &Options,
                                         std::optional<Reloc::Model> RM,
                                         std::optional<CodeModel::Model> CM,
                                         CodeGenOptLevel OL, bool JIT)
    : CodeGenTargetMachineImpl(T, TT, CPU, FS, Options,
                               getEffectiveRelocModel(RM),
                               getEffectiveCodeModel(CM, CodeModel::Small), OL),
      TLOF(std::make_unique<TargetLoweringObjectFileELF>()),
      Subtarget(TT, CPU, FS, *this) {
  // There is no unwinder, no personality routine and no exception register.
  // The ELF default would enable DwarfCFI; disabling it makes the codegen
  // pipeline run LowerInvoke, which turns an invoke into a call and leaves the
  // landing pad unreachable.
  setSupportsExceptionModel(ExceptionHandling::DwarfCFI, false);
  setSupportsExceptionModel(ExceptionHandling::SjLj, false);
  initAsmInfo();
}
```

- `getEffectiveRelocModel(RM)` 默认 `Reloc::Static`；MiniRV 没有完整 PIC/GOT 实现。
- `getEffectiveCodeModel(CM, CodeModel::Small)` 默认 small；`auipc+addi` 的 PC-relative 对正好适配。
- `TLOF` 用通用 `TargetLoweringObjectFileELF`，不自己写 `MiniRVELFTargetObjectFile`。它决定 section、常量池、EH frame 放哪；ELF header 的 `EM_RISCV` 是 MC 层的事，不在这里。
- `Subtarget(TT, CPU, FS, *this)` 直接在 TM 里放一个成员并立刻构造。所有 MiniRV 函数共享同一个 subtarget，`target-cpu`/`target-features` 属性不会切出不同配置。
- `setSupportsExceptionModel` 是本章重点，后面单独讲。
- `initAsmInfo()` 从 target 取 `MCAsmInfo`，定下汇编语法、注释符、指令对齐。

`getSubtargetImpl` 直接返回 `&Subtarget`，没有查表。`createMachineFunctionInfo` 给每个函数建 `MiniRVMachineFunctionInfo`，目前只存 `VarArgsFrameIndex`：可变参数函数把 `x10..x17` 中未被命名参数吃掉的寄存器保存到栈上，`va_start` 拿到的就是这个保存区地址。这类“每个函数一份、TM 不关心、lowering 和 frame 都要用”的状态放在 `MachineFunctionInfo` 里。

最后两个 override 决定流水线形态：

- `buildCodeGenPipeline(...)` 换成 `MiniRVCodeGenPassBuilder`。
- `shouldDefaultToNewPM() { return true; }` 让 `llc` 默认走新 PM。`TargetMachine` 默认返回 `false`，所以 RISC-V 在这个构建里默认还是 legacy codegen pipeline。

### 2. `MiniRVSubtarget`：工具柜里有什么

```cpp
MiniRVSubtarget::MiniRVSubtarget(const Triple &TT, StringRef CPU, StringRef FS,
                                 const MiniRVTargetMachine &TM)
    : MiniRVGenSubtargetInfo(TT, CPU, /*TuneCPU=*/CPU, FS), InstrInfo(*this),
      TLInfo(TM, *this), FrameLowering(*this) {
  TSInfo = std::make_unique<MiniRVSelectionDAGInfo>();
}
```

- `MiniRVGenSubtargetInfo(TT, CPU, TuneCPU, FS)` 是 TableGen 生成的基类，负责解析 `-mcpu`/`-mattr`。但 `MiniRV.td` 只有 `def : Proc<"generic", []>;`，没有任何 `SubtargetFeature`。所以 `-mattr` 是空操作，`-mcpu` 只认 `generic`。
- `InstrInfo(*this)` 内部拥有 `MiniRVRegisterInfo RI`，所以 `getRegisterInfo()` 返回 `&InstrInfo.getRegisterInfo()`，不是单独 new 的对象。`InstrInfo` 和 `RegisterInfo` 的生命周期绑定在 subtarget 上。
- `TLInfo(TM, *this)` 需要 TM（TLOF、数据布局、选项）和 STI（寄存器类、栈指针、feature）。构造顺序很重要：`TLInfo` 在 `InstrInfo` 之后，因为 `TargetLowering` 构造时会 `computeRegisterProperties(STI.getRegisterInfo())`。
- `FrameLowering(*this)` 是 prologue/epilogue、栈对齐、CSR spill 的策略对象（Ch 11）。
- `TSInfo` 是 `SelectionDAGGenTargetInfo` 的薄封装，给 DAG 提供 target-specific SDNode 信息。

五个 getter 没有逻辑：`getInstrInfo()`/`getRegisterInfo()`/`getFrameLowering()`/`getTargetLowering()`/`getSelectionDAGInfo()` 分别转发到 `InstrInfo`、`InstrInfo.getRegisterInfo()`、`FrameLowering`、`TLInfo`、`TSInfo`；MiniRV 没有 per-function feature 查询。

MiniRV **没有** `initSubtargetDependencies`。这个名字属于 RISC-V：

```cpp
RISCVSubtarget &
RISCVSubtarget::initializeSubtargetDependencies(const Triple &TT, StringRef CPU,
                                                StringRef TuneCPU, StringRef FS,
                                                StringRef ABIName) {
  bool Is64Bit = TT.isArch64Bit();
  if (CPU.empty() || CPU == "generic")
    CPU = Is64Bit ? "generic-rv64" : "generic-rv32";
  ...
  ParseSubtargetFeatures(CPU, TuneCPU, FS);
  RISCV::updateCZceFeatureImplications(*this);
  ...
  TargetABI = *ABIOrErr;
  RISCVFeatures::validate(TT, getFeatureBits());
  return *this;
}
```

RISC-V 需要它，因为：CPU 名有默认值/别名（`generic` → `generic-rv64`）；`TuneCPU` 要查 `RISCVTuneInfoTable` 拿对齐/cache 参数；feature 之间有依赖（`+c` 隐含 `+zca`，`+d` 隐含 `+f`）；`-target-abi` 要和 feature 组合校验（`lp64d` 必须有 `+d`）。MiniRV 没有 feature、CPU 表、ABI 选择，所以不需要这一层。

### 3. RISC-V 对照：per-function subtarget 与 TTI

`RISCVTargetMachine::getSubtargetImpl` 是另一个量级：

```cpp
if (LastSubtarget.first && LastSubtarget.second == FnAttrs)
  return LastSubtarget.first;
Attribute CPUAttr = F.getFnAttribute("target-cpu");
Attribute TuneAttr = F.getFnAttribute("tune-cpu");
Attribute FSAttr = F.getFnAttribute("target-features");
StringRef CPU = CPUAttr.isValid() ? CPUAttr.getValueAsString() : TargetCPU;
...
SmallString<512> Key;
raw_svector_ostream(Key) << "RVVMin" << RVVBitsMin << "RVVMax" << RVVBitsMax
                         << CPU << TuneCPU << FS;
auto &I = SubtargetMap[Key];
if (!I)
  I = std::make_unique<RISCVSubtarget>(TargetTriple, CPU, TuneCPU, FS,
                                       getTargetABIName(*F.getParent()), ...);
```

三件 MiniRV 不做的事：

- **按函数属性切 subtarget**：`target-cpu`、`tune-cpu`、`target-features`、`vscale_range` 都进 cache key。同一 module 里可以一个函数 `+v`、另一个不带。
- **缓存**：`SubtargetMap` 按 key 复用，`LastSubtarget` 再做一层快速命中。
- **TTI**：`RISCVTargetMachine::getTargetTransformInfo` 返回 `TargetTransformInfo(std::make_unique<RISCVTTIImpl>(this, F))`。RISC-V 有完整 cost model，影响循环展开、向量化、内联阈值。MiniRV 没有 override，用基类 `TargetTransformInfo`，IR 层优化不会针对 MiniRV 的指令成本做特殊判断。

CPU 表也差得远：MiniRV 只有 `def : Proc<"generic", []>;`；RISC-V 的 `RISCVProcessors.td` 有 `RISCVProcessorModel`、`RISCVTuneInfo` 和几十个 CPU。MiniRV 的 `-mcpu=help` 没有输出；传 `-mcpu=rocket` 得到：

```console
$ $B/llc -mtriple=minirv64 -mcpu=rocket -O2 f.ll -o /dev/null
'rocket' is not a recognized processor for this target (ignoring processor)
'rocket' is not a recognized processor for this target (ignoring processor)
```

只是 warning，仍然继续编译。这是 TableGen 生成代码的默认行为：不认识的 CPU 被忽略，feature 保持默认。bring-up 阶段这是好事，生产后端则要像 `RISCVFeatures::validate` 那样把错误提前。

### 4. `MiniRVTargetLowering`：合法世界的边界

构造函数只做五类声明。

**（1）合法类型与寄存器类**

```cpp
addRegisterClass(MVT::i64, &MiniRV::GPRRegClass);
```

只有 `i64` 是合法整数类型，`i8/i16/i32` 会被类型合法化提升到 `i64`。这不是偷懒：RV64 寄存器本来就是 XLEN=64，让 DAG 里出现 `i32` 只会让每个操作都要考虑高位。MiniRV 的选择是：DAG 里只有 `i64`，窄宽度用 `*W` 指令和窄 load/store 单独建模。

**（2）操作动作**

```cpp
setOperationAction(ISD::MUL, MVT::i64, Legal);
setOperationAction(ISD::SELECT, MVT::i64, Expand);
setOperationAction(ISD::SELECT_CC, MVT::i64, Custom);
setOperationAction(ISD::GlobalAddress, MVT::i64, Custom);
setOperationAction(ISD::GlobalTLSAddress, MVT::i64, Custom);
setOperationAction(ISD::BRCOND, MVT::Other, Custom);
setOperationAction(ISD::BR_CC, MVT::i64, Expand);
```

- `Legal`：DAG 可以保留这个节点，选指器有 pattern 匹配。
- `Custom`：不走通用 legalization。operation legalization 调 `LowerOperation`，type legalization 调 `ReplaceNodeResults`（MiniRV 的 `i32` 算术就是后者）。
- `Expand`：合法化器把它拆成更基础的操作。
- `Promote`：类型合法化器提升到更宽类型。

MiniRV 的 `i32` 算术全是 `Custom`：RV64 有 `ADDW/SUBW/SLLW/SRLW/SRAW/MULW/DIVW/...`，但位运算没有 `*W` 形式。这些 `i32` 节点走 `ReplaceNodeResults`（类型合法化的 Custom hook），把 `i32 ADD` 改成 `ADDW`，把 `AND/OR/XOR` 改成 64 位操作再截断；`LowerOperation` 处理的是 GlobalAddress/BRCOND/SELECT_CC 等另一批节点（Ch 12 展开）。

**（3）调用约定**

`TargetLowering` 提供 `LowerFormalArguments`、`LowerCall`、`LowerReturn`、`CanLowerReturn` 四个钩子。它们负责把 CCState 的结果变成 DAG 里的 COPY/LOAD/STORE/CALL；`CallingConv.td` 只负责算出每个参数落在哪个寄存器或栈偏移。Ch 10 逐行读。

**（4）寻址模式**

```cpp
if (AM.BaseGV || AM.ScalableOffset || !isInt<12>(AM.BaseOffs))
  return false;
switch (AM.Scale) {
case 0: break;
case 1:
  if (!AM.HasBaseReg) break;
  return false;
default: return false;
}
return true;
```

MiniRV 的 load/store 只有 `base + 12-bit signed displacement`。有全局变量基址不行；有可变缩放不行；偏移超过 `[-2048, 2047]` 不行；`scale == 1` 且同时有 base 不行——那需要一条额外 `add`，交给 LSR 决定。`isLegalICmpImmediate`/`isLegalAddImmediate` 也返回 `isInt<12>(Imm)`。

**（5）libcall**

`MiniRVTargetLowering` 自己不写 libcall 表，而是 `MiniRVSubtarget::initLibcallLoweringInfo`：

```cpp
Info.setLibcallImpl(RTLIB::MEMCPY, RTLIB::impl_memcpy);
Info.setLibcallImpl(RTLIB::MEMMOVE, RTLIB::impl_memmove);
Info.setLibcallImpl(RTLIB::MEMSET, RTLIB::impl_memset);
Info.setLibcallImpl(RTLIB::ATOMIC_LOAD, RTLIB::impl___atomic_load);
```

`minirv64` 不在 LLVM 的 runtime libcall 表里，默认查不到实现名；不 `setLibcallImpl`，lowering 会拿空字符串建符号。MiniRV 把所有原子操作映射到 `__atomic_*`（没有 A 扩展），软浮点映射到 `__addsf3`/`__divdf3` 等 compiler-rt 入口，`llvm.experimental.deoptimize` 映射到 `__llvm_deoptimize`。这张表是 Ch 20 的主线。

RISC-V 的 `RISCVTargetLowering` 用同一组接口，但把大量操作标成 `Custom`/`Expand`，并 override `LowerOperation`、`ReplaceNodeResults`、`isLegalAddressingMode`、`LowerFormalArguments` 等；MiniRV 的 946 行是它的最小子集。

最后一行必须是 `computeRegisterProperties(STI.getRegisterInfo());`，它把 `addRegisterClass` 注册的类、保留寄存器、分配顺序、合法类型一起算成内部表，放在最后是因为前面的 `setOperationAction` 可能依赖这些属性。

### 5. `SelectionDAGISel` 与 `AsmPrinter`

`MiniRVDAGToDAGISel` 很薄：继承 `SelectionDAGISel`，`#include "MiniRVGenDAGISel.inc"` 拿到 TableGen 从 `MiniRVInstrInfo.td` pattern 生成的匹配表，只 override `Select` 处理 pattern 覆盖不了的情况。比如 `ADD` 的立即数不在 12 位内但能拆成两个 `ADDI`：

```cpp
if (N->getOpcode() == ISD::ADD) {
  if (auto *C = dyn_cast<ConstantSDNode>(N->getOperand(1))) {
    int64_t Val = C->getSExtValue();
    if (!isInt<12>(Val) && Val >= -4096 && Val <= 4094) {
      int64_t First = Val < 0 ? -2048 : 2047;
      ...
```

`SelectionDAGISelPass` 做完整流水线：建 DAG、类型/操作合法化、DAG combine、`Select`、调度。`FinalizeISelPass` 再处理选指留下的伪指令：展开 `EmitInstrWithCustomInserter`，调用 `TargetLowering::finalizeLowering`，修 frame info。MiniRV 没有 custom inserter，`FinalizeISelPass` 主要是收尾。

`MiniRVAsmPrinter::emitInstruction` 负责四件事：

1. 拒绝没实现的伪指令：`STACKMAP`、`PATCHPOINT`、`STATEPOINT` 直接 `report_fatal_error`。
2. 物化符号地址：`PseudoLA` 展开成 `auipc rd, %pcrel_hi(sym)` + `addi rd, rd, %pcrel_lo(L)`。`L` 是 AsmPrinter 现场创建的临时标签，用来绑定高低两部分。
3. 展开 `PseudoInstExpansion`：`PseudoRET` → `jalr x0, 0(x1)`，`PseudoBR` → `jal x0, target`。
4. 其余走 `MiniRVMCInstLower`，把 `MachineInstr` operand 转成 `MCInst`。

`MiniRVAsmPrinterBeginPass`/`EndPass` 是 module pass，负责 `doInitialization`/`doFinalization`；`MiniRVAsmPrinterPass` 是 machine function pass，逐函数 `runOnMachineFunction`。所以 `-debug-pass-manager` 里会看到 `MiniRVAsmPrinterPass on add3` 和 `on caller` 两次。

### 6. `MiniRVCodeGenPassBuilder`：把流水线接起来

```cpp
void MiniRVCodeGenPassBuilder::addIRPasses(PassManagerWrapper &PMW) {
  addFunctionPass(AtomicExpandPass(getTM()), PMW);
  Base::addIRPasses(PMW);
}
void MiniRVCodeGenPassBuilder::addPreEmitPass(PassManagerWrapper &PMW) {
  addMachineFunctionPass(BranchRelaxationPass(), PMW);
}
Error MiniRVCodeGenPassBuilder::addInstSelector(PassManagerWrapper &PMW) {
  addMachineFunctionPass(MiniRVISelDAGToDAGPass(getTM(), getOptLevel()), PMW);
  return Error::success();
}
```

- `AtomicExpandPass` 在 IR 层把所有原子操作展开成 libatomic 调用。没有 A 扩展，选指器没有 `ATOMIC_*` pattern；不先展开就会在 `Select` 失败。
- `BranchRelaxationPass` 在 pre-emit 阶段拆分超出范围的 `beq`（4 KiB）/`jal`（1 MiB）。
- `addInstSelector` 把自己的 `MiniRVISelDAGToDAGPass` 塞进流水线，而不是用通用 ISel 创建逻辑。

`MiniRVTargetMachine::buildCodeGenPipeline` 还额外把 `Options.EnableLoopTermFold` 设为 `true` 再交给 `MiniRVCodeGenPassBuilder`。这个 pass 把计数循环的终止条件折叠到已有归纳变量上；RISC-V 也开，新 PM 默认不开。

## `setSupportsExceptionModel`：关掉异常模型，不等于关掉 CFI

`TargetMachine` 构造函数按 triple 的默认异常处理能力初始化一组 bit：

```cpp
static bool isExceptionModelSupportedByDefault(const Triple &TT,
                                               ExceptionHandling Model) {
  switch (Model) {
  case ExceptionHandling::DwarfCFI:
  case ExceptionHandling::SjLj:
    return TT.getDefaultExceptionHandling() != ExceptionHandling::None;
  ...
```

对 ELF target，`DwarfCFI` 默认打开。MiniRV 在 TM 构造函数里把它和 `SjLj` 都清掉：

```cpp
setSupportsExceptionModel(ExceptionHandling::DwarfCFI, false);
setSupportsExceptionModel(ExceptionHandling::SjLj, false);
```

这个 bit 影响的是 **IR 层的异常 lowering**，不是汇编里的 `.cfi_*`。`CodeGenPassBuilder` 这样用：

```cpp
if (TM.supportsExceptionModel(ExceptionHandling::SjLj))
  addFunctionPass(SjLjEHPreparePass(), PMW);
...
if (TM.supportsExceptionModel(ExceptionHandling::DwarfCFI))
  addFunctionPass(DwarfEHPreparePass(TM), PMW);
...
if (!HasEHPrep)
  addFunctionPass(LowerInvokePass(), PMW);
```

MiniRV 两个都关，`HasEHPrep == false`，走 `LowerInvokePass`：`invoke` 改写成普通 `call`，landing pad 变成不可达代码，再被 `UnreachableBlockElimPass` 删掉。RISC-V 两个都开，走 `SjLjEHPreparePass` + `DwarfEHPreparePass`。`-debug-pass-manager` 一眼可见：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-pass-manager f.ll -o /dev/null 2>&1 \
    | grep -E 'LowerInvokePass|SjLjEHPreparePass|DwarfEHPreparePass'
Running pass: LowerInvokePass on add3 (3 instructions)
Running pass: LowerInvokePass on caller (2 instructions)

$ $B/llc -mtriple=riscv64 -mattr=+m -O2 -enable-new-pm=1 -debug-pass-manager \
    f.ll -o /dev/null 2>&1 \
    | grep -E 'LowerInvokePass|SjLjEHPreparePass|DwarfEHPreparePass'
Running pass: SjLjEHPreparePass on add3 (3 instructions)
Running pass: DwarfEHPreparePass on add3 (3 instructions)
Running pass: SjLjEHPreparePass on caller (2 instructions)
Running pass: DwarfEHPreparePass on caller (2 instructions)
```

那为什么 MiniRV 还能打出 `.cfi_startproc`？因为 CFI 的生成条件是另一套：

```cpp
bool MachineFunction::needsFrameMoves() const {
  return getTarget().Options.ForceDwarfFrameSection ||
         F.needsUnwindTableEntry() ||
         !F.getParent()->debug_compile_units().empty();
}
// F.needsUnwindTableEntry() = hasUWTable() || !doesNotThrow() || hasPersonalityFn()
```

我们的 `f.ll` 函数没有 `nounwind`，`!doesNotThrow()` 为真，`needsFrameMoves()` 为真，`MiniRVFrameLowering` 就会在 prologue/epilogue 插 `CFIInstBuilder` 生成的 CFI，`AsmPrinter` 再打成 `.cfi_*`。实测：

```console
$ $B/llc -mtriple=minirv64 -O2 f.ll -o - | grep -E 'cfi_startproc|cfi_def_cfa_offset|cfi_offset|cfi_restore|cfi_endproc'
	.cfi_startproc
	.cfi_endproc
	.cfi_startproc
	.cfi_def_cfa_offset 16
	.cfi_offset x1, -8
	.cfi_restore x1
	.cfi_def_cfa_offset 0
	.cfi_endproc
```

给函数加 `nounwind` 后再跑，CFI 全部消失：

```console
$ $B/llc -mtriple=minirv64 -O2 g.ll -o - | grep -E 'cfi_startproc|cfi_def_cfa_offset'
$ echo $?
1
```

结论：`setSupportsExceptionModel` 管“invoke/landingpad/personality 怎么 lower”；CFI 是 `needsFrameMoves()` 的产物，触发条件是 `uwtable`、`nounwind` 的缺失、personality、调试信息或 `ForceDwarfFrameSection`。MiniRV 关异常模型是为了避免生成它没有的 unwinder/personality 路径；普通函数的 unwind 信息仍然要生成，所以 CFI 照常出现。两者不矛盾。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| TargetMachine | `MiniRVTargetMachine.cpp/h` | `MiniRVTargetMachine::MiniRVTargetMachine`、`getSubtargetImpl`、`createMachineFunctionInfo`、`buildCodeGenPipeline` | TLOF、Reloc/CodeModel 默认、异常模型、单 subtarget、新 PM |
| Subtarget | `MiniRVSubtarget.cpp/h` | ctor、`getInstrInfo`、`getRegisterInfo`、`getFrameLowering`、`getTargetLowering`、`getSelectionDAGInfo` | 五件套拥有关系、无 feature、无 TTI |
| Subtarget libcall | `MiniRVSubtarget.cpp` | `initLibcallLoweringInfo` | memcpy/memset、`__atomic_*`、软浮点、deopt |
| Lowering | `MiniRVISelLowering.cpp/h` | ctor、`LowerOperation`、`isLegalAddressingMode`、`LowerFormalArguments`、`LowerCall` | 合法类型、操作动作、寻址、CC、libcall 触发点 |
| ISel | `MiniRVISelDAGToDAG.cpp` | `MiniRVDAGToDAGISel::Select`、`MiniRVISelDAGToDAGPass` | 生成 pattern + 少量手写特例 |
| AsmPrinter | `MiniRVAsmPrinter.cpp` | `emitInstruction`、`MiniRVAsmPrinterPass`、begin/end pass | 伪指令、`PseudoLA`、符号地址、CFI |
| Pipeline | `MiniRVCodeGenPassBuilder.cpp` | `addIRPasses`、`addPreEmitPass`、`addInstSelector`、`addAsmPrinter*` | AtomicExpand、BranchRelaxation、新 PM |
| RISC-V 参照 | `RISCVTargetMachine.cpp` | `getSubtargetImpl`、`getTargetTransformInfo` | per-function cache、TTI、CPU/feature |
| RISC-V 参照 | `RISCVSubtarget.cpp` | `initializeSubtargetDependencies` | CPU 归一化、TuneInfo、feature 依赖、ABI 校验 |
| RISC-V 参照 | `RISCVISelLowering.cpp` | `RISCVTargetLowering` | 31,441 行完整 lowering；MiniRV 只有 946 行 |

## 实测：四组命令

测试文件 `f.ll` 含 `add3` 和 `caller`；`g.ll` 是给 `add3` 加 `nounwind` 的版本。

### 实测 1：MiniRV 的 pass 列表

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-pass-manager f.ll -o /dev/null 2>&1 \
    | grep -E 'LowerInvokePass|SelectionDAGISelPass|FinalizeISelPass|PrologEpilogInserterPass|BranchRelaxationPass|MiniRVAsmPrinterPass'
Running pass: LowerInvokePass on add3 (3 instructions)
Running pass: LowerInvokePass on caller (2 instructions)
Running pass: SelectionDAGISelPass on add3
Running pass: FinalizeISelPass on add3
Running pass: PrologEpilogInserterPass on add3
Running pass: BranchRelaxationPass on add3
Running pass: MiniRVAsmPrinterPass on add3
Running pass: SelectionDAGISelPass on caller
Running pass: FinalizeISelPass on caller
Running pass: PrologEpilogInserterPass on caller
Running pass: BranchRelaxationPass on caller
Running pass: MiniRVAsmPrinterPass on caller
```

- `LowerInvokePass` 而不是 `DwarfEHPreparePass`：异常模型关了，走 invoke 降级路径。
- `SelectionDAGISelPass`：DAG 构建、合法化、combine、选指。
- `FinalizeISelPass`：展开选指留下的伪指令，调用 `finalizeLowering`。
- `PrologEpilogInserterPass`：插入 prologue/epilogue、分配 CSR spill 槽、确定 frame size。
- `BranchRelaxationPass`：MiniRV 自己在 pre-emit 加的，拆分超范围分支。
- `MiniRVAsmPrinterPass`：逐函数输出汇编。

### 实测 2：`-debug-only=isel` 的初始 DAG

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel f.ll -o /dev/null 2>&1 \
    | sed -n "/Initial selection DAG: %bb.0 'add3:entry'/,/^$/p"
Initial selection DAG: %bb.0 'add3:entry'
SelectionDAG has 12 nodes:
    t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t2:1, Register:i64 %1
  t6: i64,ch = CopyFromReg t4:1, Register:i64 %2
      t7: i64 = add t2, t4
    t8: i64 = mul t7, t6
  t10: ch,glue = CopyToReg t6:1, Register:i64 $x10, t8
  t11: ch = MiniRVISD::RET t10, Register:i64 $x10, t10:1

```

这张图把 `TargetLowering` 的成果展示得很清楚：

- 三个参数已经是 `i64`，没有 `i32`/`i8` 节点——类型合法化已完成。
- `add`/`mul` 还是通用 `ISD` 节点，不是 `ADD`/`MUL` 机器指令——还没选指。
- `CopyFromReg %0/%1/%2` 是 `LowerFormalArguments` 建的虚拟寄存器，`%0` 对应 `$x10`。
- `CopyToReg $x10` + `MiniRVISD::RET` 是 `LowerReturn` 建的返回序列；`MiniRVISD::RET` 是 target-specific 节点，`getTargetNodeName` 能打印它。

继续看同一份日志的 `Selected selection DAG`，节点会变成 `ADD`/`MUL` 机器指令，但 `CopyFromReg`/`CopyToReg` 还在；直到 `FinalizeISel` 之后，`-stop-after=finalize-isel` 的 MIR 才会把虚拟寄存器和机器指令放在一起。

### 实测 3：RISC-V 的 `-debug-pass-manager` 为什么是空的

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 -debug-pass-manager f.ll -o /dev/null
$ echo $?
0
```

没有输出。不是 RISC-V 没有 pass，而是这个构建里的 `llc` 对 RISC-V 默认走 legacy codegen pipeline。`TargetMachine::shouldDefaultToNewPM()` 默认返回 `false`，`llvm/lib/Passes/RunCodeGen.cpp` 用 `valueOr(Opts.force_new_pm_codegen, TM.shouldDefaultToNewPM())` 选择流水线。MiniRV 显式 override 成 `true`，所以它的 `-debug-pass-manager` 有输出；RISC-V 没有。

加 `-enable-new-pm=1`：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 -enable-new-pm=1 -debug-pass-manager \
    f.ll -o /dev/null 2>&1 \
    | grep -E 'SjLjEHPreparePass|DwarfEHPreparePass|SelectionDAGISelPass|FinalizeISelPass|PrologEpilogInserterPass|RISCVAsmPrinterPass'
Running pass: SjLjEHPreparePass on add3 (3 instructions)
Running pass: DwarfEHPreparePass on add3 (3 instructions)
Running pass: SjLjEHPreparePass on caller (2 instructions)
Running pass: DwarfEHPreparePass on caller (2 instructions)
Running pass: SelectionDAGISelPass on add3
Running pass: FinalizeISelPass on add3
Running pass: PrologEpilogInserterPass on add3
Running pass: SelectionDAGISelPass on caller
Running pass: FinalizeISelPass on caller
Running pass: PrologEpilogInserterPass on caller
Running pass: RISCVAsmPrinterPass on add3
Running pass: RISCVAsmPrinterPass on caller
```

legacy 下用 `-debug-pass=Structure`，会看到 `-riscv-isel`、`-finalize-isel`、`-prolog-epilog`、`-riscv-asm-printer`。阶段相同，只是 pass 名不同；不要因为名字不同就以为流水线不同。

### 实测 4：`-stop-after=finalize-isel` 的 MIR

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel f.ll -o - \
    | awk '/^name:            add3/{p=1} p&&/^body:/{b=1} b{print} b&&/^\.\.\.$/{exit}'
body:             |
  bb.0.entry:
    liveins: $x10, $x11, $x12

    %2:gpr = COPY $x12
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %3:gpr = ADD %0, %1
    %4:gpr = MUL %3, %2
    $x10 = COPY %4
    PseudoRET implicit $x10
...
```

怎么读：

- `name: add3` 下面是 YAML 元数据。`registers:` 列出五个虚拟寄存器，class 都是 `gpr`；`liveins:` 把物理 `$x10/$x11/$x12` 映射到虚拟 `%0/%1/%2`，这是 `LowerFormalArguments` 里 `addLiveIn` 的结果。
- `body:` 里已经是机器指令：`ADD`、`MUL` 是选指后的 opcode；`COPY` 是参数/返回值搬运；`PseudoRET` 还没展开成 `jalr`。
- `isSSA: true`、`noVRegs: false`、`tracksRegLiveness: true`：还没跑寄存器分配，虚拟寄存器还在。
- 这里没有 prologue/epilogue、CSR spill、物理寄存器分配。`PrologEpilogInserterPass` 和 `RAGreedyPass` 还没跑。`-stop-after=finalize-isel` 是“选指刚结束”的快照，不是最终代码。

`caller` 的 MIR 里，`PseudoCALL @add3, csr_minirv` 的 implicit 操作数就是调用约定：`$x10/$x11/$x12` 是参数寄存器，`$x1` 是返回地址，`csr_minirv` 是 call-preserved 掩码；`ADJCALLSTACKDOWN/UP 0` 表示出参区已在 prologue 预留（Ch 11）。

## 反直觉与踩雷

1. **错误直觉**：`setSupportsExceptionModel(DwarfCFI, false)` 会让 `llc` 不再输出 `.cfi_*`。→ **正确认识**：它关的是 IR 层的 `DwarfEHPreparePass`/异常表准备，不是 AsmPrinter 的 CFI 打印。CFI 由 `needsFrameMoves()` 触发；没有 `nounwind` 的函数仍然生成 `.cfi_startproc`。
2. **错误直觉**：MiniRV 的 `-mattr=+m`、`-mcpu=rocket` 会像 RISC-V 一样切换指令集/调优。→ **正确认识**：`MiniRV.td` 只有 `Proc<"generic", []>`，没有 `SubtargetFeature`。`-mattr` 是空操作，`-mcpu=rocket` 只打印 warning 然后忽略。
3. **错误直觉**：`TargetLowering` 负责把 DAG 节点匹配成机器指令。→ **正确认识**：它声明“这个操作应该 Legal/Custom/Expand”，真正匹配的是 `MiniRVDAGToDAGISel::Select` 和 `MiniRVGenDAGISel.inc` 里的 pattern。`LowerOperation`（operation legalization 的 `Custom` 路径）做的是改写 DAG，不是选指令。
4. **错误直觉**：`llc -debug-pass-manager` 对所有 target 都有效。→ **正确认识**：它只对走新 PM 的 target 有输出。RISC-V 在这个构建里默认 legacy，命令静默返回 0；要么加 `-enable-new-pm=1`，要么用 `-debug-pass=Structure`。
5. **错误直觉**：`-stop-after=finalize-isel` 的 MIR 就是最终机器码，只是格式不同。→ **正确认识**：它只是选指结束的快照，虚拟寄存器还没分配，prologue/epilogue、CSR spill、分支放松都还没跑。判断选指是否发生，看 `body` 里有没有机器 opcode，不要看 `selected:` 字段。

## 进阶：再往深一层

- **TM 与 STI 的边界**：`TargetMachine` 拥有全局对象（TLOF、AsmInfo、Options），`TargetSubtargetInfo` 拥有 per-function 的指令/寄存器/帧/lowering。RISC-V 的 per-function subtarget 让 `+v` 函数和标量函数共享一个 module；MiniRV 的单 subtarget 是 bring-up 阶段的合理简化。你的 target 如果要支持 `target-features`，从第一天就要把 `getSubtargetImpl` 写成带 cache 的版本。
- **新 PM 与 legacy PM**：MiniRV 走 `CodeGenPassBuilder`，RISC-V 默认走 `TargetPassConfig`。阶段名不同，顺序一致：IR passes → ISel → machine passes → AsmPrinter。写 bring-up 日志时建议同时保留两种证据。

## 本章重点整理

- `TargetMachine` 是 module 级对象：TLOF、Reloc/CodeModel 默认、异常模型、`createMachineFunctionInfo`、pass pipeline。
- `TargetSubtargetInfo` 是 function 级对象：拥有 `InstrInfo`、`RegisterInfo`、`FrameLowering`、`TargetLowering`、`SelectionDAGInfo`。MiniRV 一个 TM 一个 subtarget；RISC-V 按函数属性缓存多个。
- `TargetLowering` 的五类职责：合法类型、操作动作、调用约定、寻址模式、libcall。它不选指令。
- `SelectionDAGISelPass` 做 DAG 构建/合法化/combine/选指；`FinalizeISelPass` 展开伪指令并收尾。
- `AsmPrinter` 做 MCInst lowering、伪指令展开、符号地址物化、CFI 打印。
- `setSupportsExceptionModel(DwarfCFI, false)` 关闭的是 IR 异常 lowering，不是 CFI。CFI 由 `needsFrameMoves()` 触发；`nounwind` 会让它消失。
- RISC-V 的 `initializeSubtargetDependencies` 处理 CPU 归一化、TuneInfo、feature 依赖、ABI 校验；MiniRV 没有这些，所以 `-mattr`/`-mcpu` 基本无效。
- RISC-V 默认 legacy PM，MiniRV 显式新 PM；`-debug-pass-manager` 对前者静默。

## 自我检核

- [ ] 不看源码，说出 `MiniRVTargetMachine` 构造函数里 `TLOF`、`Subtarget`、`setSupportsExceptionModel`、`initAsmInfo` 各自解决什么问题。
- [ ] 为什么 MiniRV 的 `getSubtargetImpl` 可以直接返回成员，而 RISC-V 要建 cache key？如果 MiniRV 明天要支持 `+m`/`-m`，你需要改哪几个地方？
- [ ] `Legal`、`Custom`、`Expand`、`Promote` 四种操作动作，各举一个 MiniRV 的例子，并说明谁会处理它。
- [ ] 为什么 `isLegalAddressingMode` 对 `scale == 1 && HasBaseReg` 返回 false？如果不返回 false，DAG 会生成什么，选指器为什么接不住？
- [ ] 解释“异常模型关了但 CFI 还在”的完整因果链：`setSupportsExceptionModel` → `CodeGenPassBuilder` → `LowerInvokePass`；以及 `needsFrameMoves` → `CFIInstBuilder` → `.cfi_*`。
- [ ] 为什么 `llc -mtriple=riscv64 -debug-pass-manager` 没有输出？写出两种看到 RISC-V pass 列表的方法。

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM Project
  - **读哪里**：先读 “The LLVM Target-Independent Code Generator” 总览，再读 “Target Machine” 和 “SelectionDAG”。
  - **能学到什么**：把本章的 TM/STI/Lowering/ISel/AsmPrinter 放回官方分层图，理解哪些接口稳定、哪些是实现细节。
  - **前提**：能读 LLVM IR 和基本 C++ 继承；不需要先读 RISCV 后端。
- **[Writing an LLVM Backend](https://llvm.org/docs/WritingAnLLVMBackend.html)** — LLVM Project
  - **读哪里**：`TargetMachine`、`TargetSubtargetInfo`、`TargetLowering` 三节，以及 `RegisterInfo`/`InstrInfo` 的最小示例。
  - **能学到什么**：官方教程骨架和 MiniRV 的对应关系；它偏旧、偏概念，但能补齐 `TargetMachine` 的成员职责。
  - **前提**：读完本章，知道 `MiniRVTargetMachine.cpp` 每一行在做什么；否则会被教程里的旧 API 名字带偏。
- **[LLVM 源码：`CodeGenPassBuilder.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Passes/CodeGenPassBuilder.cpp)** — LLVM Project
  - **读哪里**：`addIRPasses` 里 `supportsExceptionModel` 的分支，以及 `addISelPasses`/`addMachinePasses` 的顺序。
  - **能学到什么**：为什么 MiniRV 关掉异常模型后出现 `LowerInvokePass`；新 PM 流水线怎么由 target 的 bit 和 override 拼出来。
  - **前提**：知道 `ExceptionHandling` 枚举和 `TargetMachine::supportsExceptionModel` 的语义；配合本章 pass 列表读。

→ [Ch 10 寄存器类与调用约定](./10-registers-and-calling-convention.md)
