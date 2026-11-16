# Ch 23 — 诊断与显式拒绝

> **目标**：理解 bring-up 阶段“先显式拒绝、再逐步实现”的工程策略；能说清 MiniRV 的 TLS、scalable vector、patchpoint 等拒绝路径分别落在哪一层，并为自己的 target 选择 `report_fatal_error`、`reportFatalUsageError`、`LLVMContext::emitError`、`DiagnosticInfoUnsupported` 或 `Custom` lowering。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

一个新 target 最危险的失败不是“编译不过”，而是“编译过了，但生成的代码是错的”。bring-up 早期常见的三种坏结局：

1. **深层断言**：比如参数是 scalable vector，参数 lowering 却去问它的固定字节数，直接在 `TypeSize` 里 assert。
2. **选择器漏底**：DAG 节点没人认领，最后打印 `Cannot select: GlobalTLSAddress` 或 `Cannot select: intrinsic %llvm.thread.pointer`。这类信息对用户没有意义，还常常伴随一大段 stack dump。
3. **静默错误**：generic expansion 把 `llvm.readcyclecounter` 折成 0；`llvm.experimental.deoptimize` 被当成 libcall，找不到实现就打印一条 error 然后把调用丢掉，`llc` 仍然以 0 退出。这是最坏的：程序能链接、能跑，结果却是错的。

所以 bring-up 要建立一个不变量：

> **编译器接受的程序，语义必须正确；做不到就明确拒绝。**

“显式拒绝”不是偷懒，而是把“不支持”变成一个可测试、可排期、可替换的契约。每个 `*-error.ll` 都是一份 scope 文档：它告诉用户缺什么，也告诉未来的自己该从哪里开始实现。

什么时候实现、什么时候诊断？我们用的判据不是“难不难”，而是“语义能不能用现有手段表达”：

| 情况 | 选择 | MiniRV 的例子 |
|---|---|---|
| 语义可用现有指令、libcall 或 generic expansion 正确表达 | **实现** | 原子操作走 libatomic（`af98624`）、i128 除法走 compiler-rt、soft-float 走 helpers |
| 需要缺失的硬件状态或 ABI 约定 | **先诊断** | TLS 需要 thread pointer 和 TLS model；cycle counter 需要 CSR；scalable vector 需要向量寄存器和向量 ABI |
| 需要运行时/对象格式配合 | **先诊断** | stackmap/patchpoint 需要 `.llvm_stackmaps` 和运行时栈扫描 |
| 属于核心语言语义，静默错误不可接受 | **必须实现** | 超出范围的 branch 必须做 branch relaxation（`f597d37`），不能编码成回绕的位移 |
| 罕见、无测试、实现成本远高于收益 | **先诊断，等真实需求** | `llvm.localrecover` |

一句话：**能便宜地做对就做对；做不对就大声说“不做”。**

## 先建立直觉

一条 IR 特性从进入后端到变成机器码，会经过好几道关口。拒绝可以发生在任何一道，但越早越容易给出准确信息，越晚越容易变成断言：

```
IR:  thread_local global / <vscale x 4 x i64> / patchpoint / rdcycle
                 |
                 v
        IR verifier（不知道 target 能力，放行）
                 |
                 v
   SelectionDAGBuilder  ->  ISD::GlobalTLSAddress / INTRINSIC_WO_CHAIN / ...
                 |
                 v
   TargetLowering 合法化：这个 opcode 的 action 是什么？
          |                                   |
          | Custom                            | Legal / Expand
          v                                   v
   LowerOperation()                     generic 代码
          |                                   |
          | 明确 report / diagnose            | 可能静默地做“差不多”的事
          v                                   v
   MachineInstr（Pseudo / 真实指令）
                 |
                 v
        AsmPrinter / MC 编码
          |
          | 没有编码的 pseudo -> 最后一道防线
          v
        .s / .o
```

MiniRV 的策略很直接：**在能判断“这个特性不属于本 target”的最早一层，把它标成 `Custom`，然后在 `LowerOperation` 里 `report_fatal_error`。** 对没有编码的 pseudo（stackmap/patchpoint/statepoint），则在 AsmPrinter 里兜底。

三层防线可以这样记：

| 层 | 你在这里知道什么 | 适合拒绝什么 |
|---|---|---|
| DAG lowering / 参数 lowering | 这个操作、这个类型的语义 | TLS、intrinsic、scalable vector、cycle counter |
| Frame / InstrInfo | 栈布局、分支距离、寄存器约束 | VLA（变长数组）+realign、超大帧、超远分支、musttail 栈参数 |
| AsmPrinter / MC | 这条 MachineInstr 有没有编码 | stackmap、patchpoint、statepoint、非法 inline asm |

## 主线：MiniRV 的拒绝清单

MiniRV 没有为了“看起来支持”而硬塞实现。下面按代码路径走一遍，每条都对应一个 commit 和一个 `*-error.ll`。

### 1. TLS 全局：`GlobalTLSAddress` + `Custom`

`MiniRVISelLowering` 的构造函数里：

```cpp
// llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp:84-97
// There is no TLS model, so reject thread-local globals explicitly rather
// than letting the selector report "Cannot select: GlobalTLSAddress".
setOperationAction(ISD::GlobalTLSAddress, MVT::i64, Custom);
// Route the intrinsics through LowerOperation so that llvm.thread.pointer
// can be rejected; see there.
setOperationAction(ISD::INTRINSIC_WO_CHAIN, MVT::Other, Custom);
// There is no cycle counter CSR, and the generic expansion would silently
// produce zero; reject the intrinsic instead of returning a wrong value.
setOperationAction({ISD::READCYCLECOUNTER, ISD::READSTEADYCOUNTER}, MVT::i64,
                   Custom);
// llvm.localrecover has no selection pattern; diagnose the pair with
// llvm.localescape instead of failing in the selector.
setOperationAction(ISD::LOCAL_RECOVER, MVT::i64, Custom);
```

然后：

```cpp
// llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp:206-229
SDValue MiniRVTargetLowering::LowerOperation(SDValue Op,
                                             SelectionDAG &DAG) const {
  switch (Op.getOpcode()) {
  case ISD::GlobalAddress:
    return LowerGlobalAddress(Op, DAG);
  case ISD::GlobalTLSAddress:
    report_fatal_error("MiniRV does not support thread-local storage");
  case ISD::INTRINSIC_WO_CHAIN: {
    unsigned IID = cast<ConstantSDNode>(Op.getOperand(0))->getZExtValue();
    if (IID == Intrinsic::thread_pointer)
      report_fatal_error("MiniRV does not support thread-local storage");
    return SDValue();
  }
  case ISD::READCYCLECOUNTER:
    report_fatal_error("MiniRV has no cycle counter");
  case ISD::READSTEADYCOUNTER:
    report_fatal_error("MiniRV has no steady counter");
  case ISD::LOCAL_RECOVER:
    report_fatal_error(
        "MiniRV does not support llvm.localescape/llvm.localrecover");
```

`af98624` 的提交信息把动机写得很清楚：没有 TLS model，与其让选择器报 `Cannot select: GlobalTLSAddress`，不如报“这个 target 不支持 thread-local storage”。测试是 `tls-error.ll`：

```llvm
@t = thread_local global i64 7
define i64 @get_tls() {
  %v = load i64, ptr @t
  ret i64 %v
}
```

实现路径不是“加一条指令”那么简单：需要 `tp` 寄存器、TLS model（LocalExec / InitialExec / LocalDynamic / GeneralDynamic）、以及 `getTLSModel` 和 `lowerGlobalTLSAddress` 的完整逻辑。RISC-V 的参考实现在 `RISCVISelLowering::lowerGlobalTLSAddress`；如果只是裸机 bring-up，另一条更便宜的路是 `LowerToTLSEmulatedModel`（emulated TLS），不需要 thread pointer 寄存器。选哪条取决于你的 ABI 和运行时，而不是取决于编译器内部好不好写。

### 2. `llvm.thread.pointer`：同一个诊断，另一条入口

`188665d` 修的是一个很典型的“同一语义、两个入口”问题：`thread_local` 全局走 `GlobalTLSAddress`，而 `llvm.thread.pointer` 走 `INTRINSIC_WO_CHAIN`。如果只处理前者，后者仍然会在选择器里报：

```
Cannot select: intrinsic %llvm.thread.pointer
```

MiniRV 的做法是把 `INTRINSIC_WO_CHAIN` 全部标成 `Custom`，在 `LowerOperation` 里只拦截 `Intrinsic::thread_pointer`，其他 intrinsic 返回空 `SDValue()`，让选择器继续按原有 pattern 处理。这样诊断和 TLS 全局完全一致，用户不需要理解 intrinsic 的内部编号。

实现路径：给 target 一个 thread pointer 寄存器（RISC-V 是 `X4`），然后 `return DAG.getRegister(RISCV::X4, PtrVT);`。RISC-V 在 `RISCVISelLowering.cpp` 的 `Intrinsic::thread_pointer` case 里就是这么做的。前提是你的 ABI 真的定义了 `tp`，并且运行时/链接器会把 TLS 块准备好；否则“读一个寄存器”只会把错误推迟到运行期。

### 3. cycle counter / steady counter：拒绝“折成 0”

`READCYCLECOUNTER` 和 `READSTEADYCOUNTER` 在 MiniRV 里也被标成 `Custom`。原因写在注释里：没有 CSR 的情况下，generic expansion 会把 intrinsic 折成 0。一个永远返回 0 的 cycle counter 比编译失败更糟，因为它会让基准测试、超时逻辑、随机种子全部“看起来正常”。

`5b8f602` 做了一件小事但很重要：把两个 intrinsic 的诊断分开。

```cpp
case ISD::READCYCLECOUNTER:
  report_fatal_error("MiniRV has no cycle counter");
case ISD::READSTEADYCOUNTER:
  report_fatal_error("MiniRV has no steady counter");
```

测试也分成 `cycle-counter-error.ll` 和 `steady-counter-error.ll`。如果两者共用一个 “cycle counter” 消息，用户用 `llvm.readsteadycounter` 时会一头雾水；诊断应该命名用户实际使用的东西。

实现路径：RISC-V 在 RV64 上把 `READCYCLECOUNTER` 标成 `Legal`，pattern 是 `CSRRS CYCLE.Encoding, X0`（`RISCVInstrInfo.td` 的 `readcyclecounter` Pat）；RV32 上则用 `READ_COUNTER_WIDE` 组合 `cycle`/`cycleh`。MiniRV 连 CSR 指令都没有，所以要先加 `CSRRS` 的编码、CSR 编号和 pattern，或者退而求其次走 libcall。两条路都要先决定“这个 target 是否真的承诺提供 cycle/time”。

### 4. `llvm.localescape` / `llvm.localrecover`：成对诊断

`LOCAL_RECOVER` 被标成 `Custom`，但 MiniRV 只拒绝了 `localrecover`，并没有拒绝 `localescape`。`localescape` 在 SelectionDAGBuilder 里会直接生成 `LOCAL_ESCAPE` 伪指令，frame index 会被当成相对 frame address 的偏移；真正缺的是 `localrecover` 的选择 pattern。所以诊断把一对 intrinsic 都点名：

```
MiniRV does not support llvm.localescape/llvm.localrecover
```

测试 `localescape-error.ll` 同时定义了两个函数：一个调用 `llvm.localescape`，一个调用 `llvm.localrecover`。这样读者能看懂“不是 escape 本身不能用，而是这对机制不完整”。

实现路径比 TLS 窄：需要为 `ISD::LOCAL_RECOVER` 定义一个 SDNode 和 pattern，把 frame alloc symbol 物化成偏移再加到 frame pointer 上。AArch64 的 `AArch64LocalRecover` 和 X86 的 `X86RecoverFrameAlloc` 是现成参考。它不需要新的寄存器类，但需要 MC/汇编层支持 frame alloc symbol，所以仍然不是十分钟能做完的事。

### 5. scalable vector 参数：在 `TypeSize` 之前拦住

`48b9276` 的提交信息说得很直白：函数接收 scalable vector 时，参数 lowering 去问固定字节数，崩在 `TypeSize` 里。MiniRV 没有向量单元，也没有向量 ABI，所以在 `LowerFormalArguments` 和 `LowerCall` 两处检查：

```cpp
// llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp:583-585
for (const ISD::InputArg &IA : Ins)
  if (IA.ArgVT.isScalableVector())
    report_fatal_error("MiniRV does not support scalable vectors");
```

调用侧同理（`Outs`）。测试 `scalable-vector-error.ll` 用一个 `<vscale x 4 x i64>` 的 add 函数同时覆盖形参和返回值；诊断会在形参 lowering 阶段触发。

实现路径是“另一个世界”：需要向量寄存器类、向量调用约定、`vscale` 的运行时值、spill/reload 规则。对一个标量 bring-up target，正确选择就是拒绝，而不是把 scalable vector 当成固定长度向量糊弄过去。

### 6. stackmap / patchpoint / statepoint：最后一道防线

`ac86a8a` 在 `MiniRVAsmPrinter::emitInstruction` 里拦截没有编码的 pseudo：

```cpp
// llvm/lib/Target/MiniRV/MiniRVAsmPrinter.cpp:211-219
void MiniRVAsmPrinter::emitInstruction(const MachineInstr *MI) {
  if (MI->getOpcode() == TargetOpcode::STACKMAP ||
      MI->getOpcode() == TargetOpcode::PATCHPOINT ||
      MI->getOpcode() == TargetOpcode::STATEPOINT)
    report_fatal_error("MiniRV does not support stack maps");
```

为什么放在 AsmPrinter？因为 `STACKMAP`/`PATCHPOINT` 是伪指令，真正的问题是“没有 target-specific lowering 去记录 map、padding 代码、生成 `.llvm_stackmaps` 段”。在指令打印机里拦截是最后一道防线；它能保证不会走到 generated printer 的 `llvm_unreachable`。`patchpoint-error.ll` 和 `stackmap-error.ll` 都期望同一条消息。

`5b8f602` 又补了 `STATEPOINT`，并处理了 `llvm.experimental.deoptimize` 的一个隐蔽问题：generic 代码把 deoptimize 当 libcall，找不到 `DEOPTIMIZE` 实现时只 `emitError("no deoptimize libcall available")` 然后丢掉调用，`llc` 退出码还是 0，输出一个空函数。修法是：

```cpp
// llvm/lib/Target/MiniRV/MiniRVSubtarget.cpp:47
Info.setLibcallImpl(RTLIB::DEOPTIMIZE, RTLIB::impl___llvm_deoptimize);
```

这样 lowering 会走到 statepoint，最后被 AsmPrinter 拒绝，得到一个硬错误。这个案例值得反复看：**“有 diagnostic”不等于“编译失败”**；如果诊断之后继续生成代码，用户可能拿到一个看似成功的坏结果。

实现路径：移植 RISC-V 的 `LowerSTACKMAP`、`LowerPATCHPOINT`、`LowerSTATEPOINT` 和 `emitPatchPoint`，并支持 `StackMaps` 段。RISC-V 也只在 64 位 target 上支持，32 位会 `reportFatalUsageError`。这是一整块运行时契约，不适合在 bring-up 早期硬做。

### 7. frame chain、VLA+realign、musttail 栈参数

还有几条不在 `LowerOperation` 里的拒绝，同样遵循“说清楚”的原则：

- `llvm.frameaddress(1)` / `llvm.returnaddress(1)`：MiniRV 的 frame pointer 是 incoming SP，但没有保存 previous FP 的链，所以 `LowerFRAMEADDR`/`LowerRETURNADDR` 在 depth != 0 时 `report_fatal_error("MiniRV cannot walk the frame chain")`。RISC-V 的实现是从 `[FP - 2*XLEN]` 逐层 load，前提是 prologue 真的保存了 previous FP。
- VLA + 超额对齐：`MiniRVFrameLowering::emitPrologue` 在 `hasVarSizedObjects()` 且需要 realign 时拒绝，因为没有第二个 base register 能同时指向 realign 后的 SP 和动态分配的 locals。RISC-V 通过 `RVFI` 的栈管理（reserved spill、base register 等）处理这类组合。
- `musttail` + 栈参数：`LowerCall` 发现尾调用需要转发栈参数时拒绝，因为 epilogue 马上要释放 caller 的 incoming argument area，写进去的栈参数会失效。非 musttail 的普通尾调用可以降级成普通调用，所以只有 `musttail` 才硬失败。
- 超远分支 / 超大帧：`MiniRVInstrInfo::insertIndirectBranch` 拒绝超过 signed 32-bit 的分支偏移；`MiniRVFrameLowering::emitPrologue` 拒绝超过 32-bit 的帧。它们是“当前实现的上限”，不是 ISA 的永久限制，但也不能静默截断。

这些拒绝路径对应的 commit 可以这样核对：

```console
$ for c in af98624 188665d 48b9276 ac86a8a6d884 5b8f602; do
    git show -s --format='%h %s' $c
  done
af9862449a00 [MiniRV] Lower atomics to libatomic and reject TLS explicitly
188665dc4cf6 [MiniRV] Reject llvm.thread.pointer with the TLS diagnostic
48b927646a77 [MiniRV] Reject scalable vector arguments with a diagnostic
ac86a8a6d884 [MiniRV] Reject stack maps and patchpoints
5b8f6027c83a [MiniRV] Diagnose deoptimize and steady-counter calls
```

### 拒绝清单总表

| 特性 | 当前 MiniRV 行为 | 错误消息 | 实现路径 | 测试文件 |
|---|---|---|---|---|
| `thread_local` 全局 | `GlobalTLSAddress` → `Custom` → `LowerOperation` | `MiniRV does not support thread-local storage` | `tp` 寄存器 + TLS model；裸机可用 emulated TLS | `tls-error.ll` |
| `llvm.thread.pointer` | `INTRINSIC_WO_CHAIN` → `Custom`，只拦截该 intrinsic | 同上 | `DAG.getRegister(tp, PtrVT)` | `thread-pointer-error.ll` |
| `llvm.readcyclecounter` | `READCYCLECOUNTER` → `Custom` | `MiniRV has no cycle counter` | `CSRRS CYCLE` + pattern，或 libcall | `cycle-counter-error.ll` |
| `llvm.readsteadycounter` | `READSTEADYCOUNTER` → `Custom` | `MiniRV has no steady counter` | `CSRRS TIME` + pattern，或 libcall | `steady-counter-error.ll` |
| `localescape`/`localrecover` | `LOCAL_RECOVER` → `Custom` | `MiniRV does not support llvm.localescape/llvm.localrecover` | `LOCAL_RECOVER` SDNode + frame alloc symbol | `localescape-error.ll` |
| scalable vector 参数 | `LowerFormalArguments`/`LowerCall` 检查 `Ins`/`Outs` | `MiniRV does not support scalable vectors` | 向量寄存器类 + 向量调用约定 | `scalable-vector-error.ll` |
| stackmap | AsmPrinter 拦截 `STACKMAP` | `MiniRV does not support stack maps` | `LowerSTACKMAP` + `.llvm_stackmaps` | `stackmap-error.ll` |
| patchpoint | AsmPrinter 拦截 `PATCHPOINT` | 同上 | `LowerPATCHPOINT` + `emitPatchPoint` | `patchpoint-error.ll` |
| statepoint / deoptimize | AsmPrinter 拦截 `STATEPOINT`；`DEOPTIMIZE` 命名 libcall | 同上 | `LowerSTATEPOINT` + 运行时 `__llvm_deoptimize` | `deoptimize-error.ll` |
| `frameaddress`/`returnaddress` depth>0 | `LowerFRAMEADDR`/`LowerRETURNADDR` 检查 depth | `MiniRV cannot walk the frame chain` | prologue 保存 previous FP，逐层 load | `frame-info-error.ll` |
| VLA + 超额对齐 | `emitPrologue` 检查 `hasVarSizedObjects()` | `MiniRV cannot realign the stack with variable-sized allocations` | 第二个 base register / reserved spill | `stack-align-error.ll` |
| `musttail` + 栈参数 | `LowerCall` 检查 `IsTailCall` 与栈大小 | `MiniRV cannot tail call a function with stack arguments` | 转发栈参数，或换用支持该 ABI 的调用序列 | `musttail-error.ll` |
| 分支超出 ±32-bit | `insertIndirectBranch` 检查 `isInt<32>` | `branch offsets outside the signed 32-bit range are not supported` | branch relaxation / 长跳转序列 | `branch-relaxation.ll`（正向） |

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| DAG 合法化 | `llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp` | `MiniRVTargetLowering::MiniRVTargetLowering` | `setOperationAction(..., Custom)` 把哪些 opcode 路由到 `LowerOperation` |
| DAG lowering | 同上 | `MiniRVTargetLowering::LowerOperation` | TLS、thread.pointer、两个 counter、localrecover 的分支 |
| 参数 lowering | 同上 | `LowerFormalArguments` / `LowerCall` | scalable vector 的 `Ins` / `Outs` 检查 |
| 帧 lowering | `llvm/lib/Target/MiniRV/MiniRVFrameLowering.cpp` | `emitPrologue` | VLA+realign、32-bit 帧上限 |
| 指令信息 | `llvm/lib/Target/MiniRV/MiniRVInstrInfo.cpp` | `insertIndirectBranch` | 超远分支的诊断 |
| 汇编打印 | `llvm/lib/Target/MiniRV/MiniRVAsmPrinter.cpp` | `emitInstruction` | `STACKMAP`/`PATCHPOINT`/`STATEPOINT` 兜底 |
| libcall | `llvm/lib/Target/MiniRV/MiniRVSubtarget.cpp` | `initLibcallLoweringInfo` | `RTLIB::DEOPTIMIZE` 为什么要命名 |
| 参考实现 | `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` | `lowerGlobalTLSAddress`、`lowerFRAMEADDR`、`READCYCLECOUNTER` case | 成熟 target 如何真正实现这些路径 |
| 参考实现 | `llvm/lib/Target/RISCV/RISCVAsmPrinter.cpp` | `LowerSTACKMAP` / `LowerPATCHPOINT` / `LowerSTATEPOINT` | stackmap 的实现长什么样 |

## 实测：每条拒绝路径的真实输出

先设好变量：

```console
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ T=$TREE/llvm/test/CodeGen/MiniRV
```

下面每条命令都直接跑 `llc`。为了不让 stack dump 淹没正文，只保留 stderr 的第一行；完整输出还会包含 `PLEASE submit a bug report ...` 和几十行 backtrace，退出码是 134（`report_fatal_error` 默认 `GenCrashDiag=true`，最终 `abort()`）。测试里的 `not --crash` 正是为了匹配这个行为。

```console
$ $B/llc -mtriple=minirv64 $T/scalable-vector-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support scalable vectors
$ $B/llc -mtriple=minirv64 $T/patchpoint-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support stack maps
$ $B/llc -mtriple=minirv64 $T/deoptimize-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support stack maps
```

```console
$ $B/llc -mtriple=minirv64 $T/frame-info-error.ll -o /dev/null
LLVM ERROR: MiniRV cannot walk the frame chain
$ $B/llc -mtriple=minirv64 $T/localescape-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support llvm.localescape/llvm.localrecover
$ $B/llc -mtriple=minirv64 $T/cycle-counter-error.ll -o /dev/null
LLVM ERROR: MiniRV has no cycle counter
$ $B/llc -mtriple=minirv64 $T/tls-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support thread-local storage
```

再看三个“同族”的入口，确认它们没有漏到选择器：

```console
$ $B/llc -mtriple=minirv64 $T/thread-pointer-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support thread-local storage
$ $B/llc -mtriple=minirv64 $T/steady-counter-error.ll -o /dev/null
LLVM ERROR: MiniRV has no steady counter
$ $B/llc -mtriple=minirv64 $T/stackmap-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support stack maps
```

对照一下测试指令的语义。`not` 和 `not --crash` 不能互换：

```console
$ $B/not --crash $B/llc -mtriple=minirv64 $T/tls-error.ll -o /dev/null >/dev/null 2>&1
$ echo $?
0
$ $B/not $B/llc -mtriple=minirv64 $T/tls-error.ll -o /dev/null >/dev/null 2>&1
$ echo $?
1
```

`not --crash` 成功，因为 `report_fatal_error` 让进程 abort；普通 `not` 只接受“非零退出但不崩溃”，所以它失败了。如果你以后把某条诊断改成 `reportFatalUsageError`（exit 1），对应测试必须从 `not --crash` 改成 `not`，否则 CI 会红。

## 四种报错机制怎么选

### `report_fatal_error`

- 签名：`[[noreturn]] void report_fatal_error(const Twine &, bool GenCrashDiag = true)`。
- 行为：打印 `LLVM ERROR: ...`，默认 `GenCrashDiag=true` 时 `abort()`；本构建里 `llc` 退出码 134，并打印 stack dump。
- 适合：bring-up 早期的“明确不支持”，尤其是你希望测试用 `not --crash` 锁死行为的时候。
- 代价：对最终用户不友好，会提示“提交 LLVM bug”，但这不是编译器的 bug。该 API 在 `ErrorHandling.h` 里已经标了 `@deprecated`，新代码应优先考虑下面两个。

### `reportFatalUsageError`

- 行为：内部调用 `report_fatal_error(reason, /*GenCrashDiag=*/false)`，打印 `LLVM ERROR: ...` 后 `exit(1)`，没有 crash trace，也不会要求用户报 bug。
- 适合：已知的“不支持/未实现”功能，是 bring-up 诊断最语义正确的选择。
- 例子：RISC-V 在 32 位 target 上对 `STACKMAP`/`PATCHPOINT`/`STATEPOINT` 用它；GHC calling convention 下的 TLS 也用它。
- 测试：用 `not`，不是 `not --crash`。

MiniRV 目前统一用 `report_fatal_error`，好处是测试一致、行为最硬；如果要把 MiniRV 的诊断打磨成“用户可读”，第一步就是把这些调用换成 `reportFatalUsageError`，同时把 12 个 `not --crash` 改成 `not`。这是一次独立的、机械的改造，适合在 bring-up 稳定后做。

### `LLVMContext::emitError`

- 头文件注释写得很清楚：它**会返回**，调用者要准备好“把出错的构造丢掉，且不要崩溃”；生成的代码**不保证正确**，消息会自动加 `error: ` 前缀。
- 适合：还能继续分析、希望一次报告多个错误，或者某个构造可以安全丢弃的场景。
- 风险：如果你只是 `emitError` 然后继续，可能得到空函数或错误代码。`deoptimize` 就是活例子：generic 代码 `emitError("no deoptimize libcall available")`，丢掉调用，`llc` 仍然退出 0。MiniRV 的修法不是“再 emit 一次”，而是把 libcall 命名，让流程走到一个会硬失败的 patchpoint。

### `DiagnosticInfoUnsupported`

- 定义在 `llvm/include/llvm/IR/DiagnosticInfo.h`，携带 `Function` 和可选 `DiagnosticLocation`，默认 severity 是 `DS_Error`。
- 通过 `MF.getFunction().getContext().diagnose(DiagnosticInfoUnsupported{...})` 发出；它**不会立刻终止**，而是记录一条带函数/源码位置的诊断。
- 适合：成熟 target 的“用户可见的不支持”，尤其是能定位到 IR 指令、希望错误信息带文件名/行号的场景。RISC-V 用它在保留寄存器冲突、栈指针被用户保留等情况下报错；X86 在“返回 double 但没有 SSE2”这类 ABI 不支持时也用它。
- 注意：它和 `emitError` 一样是“记录后返回”，所以 lowering 仍然要返回一个安全的占位值，不能让后续 pass 拿着非法节点继续跑。真要硬停，用 `reportFatalUsageError`。

### `setOperationAction(..., Custom)` + `fail`

这不是一种“报错 API”，而是一种**路由机制**：`Custom` 告诉合法化器“别自己猜，去调用 `LowerOperation`”。如果你标了 `Custom` 却没有处理某个 opcode，`LowerOperation` 返回空 `SDValue()`，节点留在 DAG 里，最后仍然会变成 `Cannot select`。所以正确姿势是：

1. 用 `Custom` 把 opcode 精确路由到你的代码；
2. 在 `LowerOperation` 的 `case` 里给出实现，或者给出明确诊断；
3. 不要用“返回空”假装处理了。

MiniRV 的 `INTRINSIC_WO_CHAIN` 是个例外：它对 `thread_pointer` 硬失败，对其他 intrinsic 返回空，让选择器继续走 pattern。这是有意的“只拦截一个”，不是漏处理。

一张表总结：

| 机制 | 是否立即终止 | 退出码/行为 | 有源码位置 | 适合 |
|---|---|---|---|---|
| `report_fatal_error` | 是 | abort，134 + crash dump | 否 | bring-up 早期硬拒绝 |
| `reportFatalUsageError` | 是 | exit 1，无 crash dump | 否 | 已知不支持/未实现 |
| `LLVMContext::emitError` | 否 | 继续，最终由 driver 决定 | 否 | 可恢复、可丢弃的构造 |
| `DiagnosticInfoUnsupported` | 否 | 继续，记录 error 诊断 | 是（函数 + DebugLoc） | 成熟 target 的用户诊断 |
| `Custom` + `fail` | 取决于 fail | 由 `report_*` 决定 | 否 | 把 opcode 路由到自己的拒绝逻辑 |

## 反直觉与踩雷

1. **错误直觉**：“不支持的路径不用管，反正选择器会报错。”
   **正确认识**：选择器报的是内部节点名（`GlobalTLSAddress`、`%llvm.thread.pointer`），用户看不懂，而且可能在报错前先 assert。必须用 `Custom` 把它路由到自己的诊断。

2. **错误直觉**：“只要 `emitError` 了，编译就会失败。”
   **正确认识**：`emitError` 只记录诊断并返回。`deoptimize` 的 generic 路径就是 emit 完继续走，最后 `llc` 退出 0、输出空函数。**诊断之后必须保证不再生成错误代码**，否则要用 `reportFatalUsageError` 之类的硬停。

3. **错误直觉**：“`not` 和 `not --crash` 差不多，随便写。”
   **正确认识**：`report_fatal_error` 走 abort，测试必须 `not --crash`；`reportFatalUsageError` 走 exit 1，测试必须 `not`。我们上面的实测已经证明两者会给出相反的结论。

4. **错误直觉**：“拒绝得越早越好，能拦就拦。”
   **正确认识**：太早会挡住本来可以便宜实现的特性。原子操作在 ISA 里没有 A 扩展，但 MiniRV 选择走 libatomic，而不是拒绝；如果一开始就在 DAG 里拒绝，就失去了“用 libcall 补齐语义”的机会。判断标准是语义能否正确表达，不是“有没有对应指令”。

5. **错误直觉**：“`LLVM ERROR` 就是给用户看的错误信息。”
   **正确认识**：默认的 `report_fatal_error` 会附带“请提交 LLVM bug”的提示和 stack dump，这对“本 target 不支持”是不合适的。面向用户的诊断应该用 `reportFatalUsageError` 或 `DiagnosticInfoUnsupported`，并给出可行动的信息。

6. **错误直觉**：“`DiagnosticInfoUnsupported` 会像异常一样中止 lowering。”
   **正确认识**：它是 `diagnose()`，记录后返回。你仍然要决定返回什么值、是否继续。对“继续就会生成错代码”的场景，硬停更安全。

7. **错误直觉**：“堆栈里的 `LLVM ERROR` 越详细越好，最好把 DAG 也打出来。”
   **正确认识**：测试只需要稳定的第一行；stack dump 是调试辅助，不是契约。诊断消息要短、稳定、点名特性，测试用 `CHECK:` 锁住它。

## 进阶：把一条诊断变成实现

诊断不是终点，而是一张带测试的 TODO。把它变成实现的顺序，我们建议固定成五步：

1. **保留 error 测试**：它就是验收标准。实现完成后，这个文件要么改名成正向测试，要么保留为“该特性关闭时仍然拒绝”的负向测试。
2. **定位层**：TLS/参数/intrinsic 在 `LowerOperation`；栈布局在 FrameLowering；pseudo 编码在 AsmPrinter。层选错了，实现会变成打补丁。
3. **选实现策略**：能 libcall 就 libcall（atomics、i128 div、soft-float）；能 generic expand 就 expand（bit intrinsics）；需要新指令就加 encoding + pattern（cycle counter）；需要 ABI/运行时就评估是否值得（TLS、stackmap）。
4. **先做最小语义**：例如 `llvm.thread.pointer` 只需要返回 `tp` 寄存器；`READCYCLECOUNTER` 只需要一条 `CSRRS`。不要顺手把整个 TLS 模型做完。
5. **补正向测试 + e2e**：CodeGen 测试锁指令，e2e 锁运行结果；两者都过，才删诊断。

两个具体的轨迹：

- **cycle counter**：现在 `READCYCLECOUNTER` → 诊断。实现时加 `CSRRS` 的 MC 编码、`CYCLE`/`TIME` 的 CSR 常量、`Pat<(i64 (readcyclecounter)), (CSRRS CYCLE, X0)>`，把 `setOperationAction` 从 `Custom` 改成 `Legal`，把 `cycle-counter-error.ll` 改成检查 `csrrs` 的正向测试，最后在 e2e 里读一次 counter 确认不是常量 0。
- **TLS**：如果只是裸机单线程，最便宜的是 emulated TLS（`LowerToTLSEmulatedModel`），不需要 `tp`；如果要走 ELF TLS，则需要 `tp`、TLS model、`__tls_get_addr` 或 TLSDESC。两条路的测试完全不同，先决定运行时契约再写代码。

反例也要记住：**不是所有“不支持”都应该变成诊断**。`f597d37` 之前，超远分支会被编码成回绕的位移，静默跳到别处；这不是“未实现”，而是“核心语义错误”。正确做法是实现 branch relaxation（`getInstSizeInBytes`、`getBranchDestBlock`、`reverseBranchCondition`、`insertIndirectBranch`），而不是加一条“分支太远不支持”的诊断。判据还是那句：**能正确表达就必须正确表达；不能正确表达才拒绝。**

## 本章重点整理

- bring-up 的目标不是“尽可能多接受程序”，而是“接受的一定对”。显式拒绝是质量保证，不是功能缺失。
- MiniRV 用 `setOperationAction(..., Custom)` 把 TLS、`thread.pointer`、两个 counter、`localrecover` 路由到 `LowerOperation`，在那里 `report_fatal_error`。
- scalable vector 在参数 lowering 的 `Ins`/`Outs` 上检查；stackmap/patchpoint/statepoint 在 AsmPrinter 兜底；VLA+realign、musttail 栈参数、超远分支、超大帧在 frame/instr 层拒绝。
- `report_fatal_error` 立即 abort（测试用 `not --crash`）；`reportFatalUsageError` exit 1（测试用 `not`）；`emitError`/`DiagnosticInfoUnsupported` 只记录、会返回，必须保证后续不生成错代码。
- 诊断要命名用户用的特性，不要暴露 DAG 节点名；诊断消息是测试契约，要稳定。
- 把诊断变成实现的固定路径：保留 error 测试 → 定位层 → 选策略 → 最小实现 → 正向测试 + e2e。
- 核心语义（分支、调用、返回）不能靠诊断逃避；静默错误比编译失败更糟。

## 自我检核

- [ ] 不看源码，说出 `GlobalTLSAddress`、`thread_pointer`、`READCYCLECOUNTER`、`LOCAL_RECOVER` 分别被哪一行 `setOperationAction` 标成 `Custom`，以及它们在 `LowerOperation` 的哪个 case 里失败。
- [ ] 解释为什么 `llvm.readcyclecounter` 不能交给 generic expansion：它最终会变成什么？为什么那比报错更糟？
- [ ] `report_fatal_error` 和 `reportFatalUsageError` 的退出行为差在哪？对应的 lit 指令分别是什么？如果写反了会发生什么？
- [ ] 为什么 `emitError` 之后还要 `return` 一个值？举出 MiniRV 里“诊断了但没失败”的真实案例。
- [ ] 给你自己的 target 选一个当前不支持的 IR 特性，写出：它应该在哪一层拒绝、消息是什么、未来实现的第一步是什么、测试文件叫什么。
- [ ] 判断下面哪些应该实现而不是诊断：原子操作、TLS、超远分支、scalable vector、i128 除法。给出理由。

## 延伸阅读

- **[LLVM `ErrorHandling.h` / `ErrorHandling.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/Support/ErrorHandling.h)** — LLVM 项目
  - **读哪里**：`report_fatal_error`、`reportFatalInternalError`、`reportFatalUsageError` 的声明和注释，以及 `ErrorHandling.cpp` 里 `GenCrashDiag` 如何决定 `abort()` 还是 `exit(1)`。
  - **能学到什么**：为什么“不支持的功能”不该走 internal error；测试指令 `not` / `not --crash` 的差异从哪来。
  - **前提**：读过基本的 C++ 异常/终止语义，知道 `abort` 和 `exit` 的区别。

- **[LLVM `DiagnosticInfo.h` 与 `LLVMContext.h`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/DiagnosticInfo.h)** — LLVM 项目
  - **读哪里**：`DiagnosticInfoUnsupported`、`DiagnosticInfoUnsupportedTargetIntrinsic` 的构造参数；`LLVMContext::emitError` 的注释（“returns, code need not be correct”）。
  - **能学到什么**：带函数/源码位置的诊断怎么构造；为什么 `diagnose` 不是 `noreturn`；什么时候该用它而不是硬停。
  - **前提**：知道 `Function`、`DebugLoc`、`DiagnosticSeverity` 的基本概念。

- **[RISC-V `RISCVAsmPrinter::LowerSTACKMAP` / `LowerPATCHPOINT` / `LowerSTATEPOINT`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/RISCV/RISCVAsmPrinter.cpp)** — LLVM 项目
  - **读哪里**：三个 `Lower*` 函数如何调用 `StackMaps`、如何 `emitPatchPoint`、如何处理 64 位限制。
  - **能学到什么**：把 MiniRV 的“stack maps 不支持”变成实现，需要补哪些对象格式和运行时契约。
  - **前提**：读过 Ch 22 的对象文件/重定位，知道 `.llvm_stackmaps` 是给运行时看的元数据。

- **[LLVM LangRef：Thread-Local Storage、`llvm.thread.pointer`、Stack Map Intrinsics](https://llvm.org/docs/LangRef.html)** — LLVM 项目
  - **读哪里**：`thread_local` 的 TLS model、`llvm.thread.pointer` 的语义、`llvm.experimental.stackmap` / `patchpoint` 的操作数。
  - **能学到什么**：诊断消息背后的语言级契约；为什么这些特性不是“加一条指令”就能支持。
  - **前提**：能读懂 LLVM IR 的全局变量和 intrinsic 声明。

- **[`../riscv_backend/README.md`](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：TLS、调用约定、stackmap/statepoint 相关章节。
  - **能学到什么**：成熟 target 如何把这些路径真正实现，而不是停在诊断。
  - **前提**：先读完本章和 Ch 17（参数与返回）。

→ [Ch 24 测试一个新 target](./24-testing-a-new-target.md)
