# Ch 16 — 调用序列

> **目标**：把一次函数调用从 `LowerCall` 追到 `jal`/`jalr`，解释 `ra` 的保存、`ADJCALLSTACKDOWN/UP`、栈参数、尾调用与 `musttail` 的边界。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

控制流解决“块之间怎么走”，调用序列解决“函数之间怎么交接”。这两件事经常被混在一起：`jal` 既是跳转又是写返回地址，`ret` 既是跳转又是读返回地址，栈参数既属于调用者又属于被调用者。任何一处 ABI 约定错位，程序都能编译、链接，然后在运行时以一个很难查的栈错结束。

MiniRV 的调用序列尤其值得看，因为它没有“调用指令”这个概念上的特殊编码：direct call 就是 `jal x1, target`，indirect call 就是 `jalr x1, 0(rs)`，return 就是 `jalr x0, 0(x1)`，tail call 就是 `jal x0, target`。所有复杂性都在**谁保存 `x1`、参数放哪、栈什么时候调整、伪指令什么时候消失**。

这一章回答：

1. caller 和 callee 各自负责什么？
2. `LowerCall` 的每一步在 DAG 里长什么样？
3. `PseudoCALL`/`PseudoCALLReg`/`PseudoTAIL` 和 `ADJCALLSTACKDOWN/UP` 分别做什么？
4. `musttail` 为什么在 MiniRV 上会被拒绝？
5. `callsEHReturn`/`callsUnwindInit` 与“MiniRV 禁用异常模型”是什么关系？

## 先建立直觉

先看 caller 和 callee 的栈与寄存器：

```
高地址
        +-------------------------------+  <- caller 的 caller 的帧
        | caller 进入时的栈参数          |
        +-------------------------------+  <- caller 的 CFA（entry sp）
        | caller 保存的 x1 (ra)          |  sd x1, 8(x2)
        | 填充 / 局部对象                |
        +-------------------------------+  <- caller 当前 sp = CFA - 16
        | 第 9 个及以后的 outgoing 参数  |  sd x3, 0(x2)
        +-------------------------------+
低地址
                  |
                  |  jal x1, args9   （x1 = 返回地址）
                  v
        +-------------------------------+  <- callee entry sp（就是 caller 当前 sp）
        | 第 9 个参数                    |  ld x4, 0(x2)
        | callee 的 ra 保存区（若它再调用）|
        | callee 的局部对象              |
        +-------------------------------+  <- callee 当前 sp
```

寄存器约定：

```
x1  = ra      返回地址；MiniRV 把它列进 CSR 集合，函数只要会写 x1 就保存它
x2  = sp      栈指针；16 字节对齐
x5  = scratch 保留，BranchRelaxation / 大偏移寻址使用
x6  = scratch 保留，栈调整同时读写 sp 时使用
x8  = s0/fp   可选帧指针；有动态 alloca / frameaddress / 栈 realignment 时才保留
x10..x17      8 个整型参数；x10 同时是第一个返回值
x18..x27      callee-saved
```

一次调用的时间线：

```
caller                              callee
------                              ------
sp -= 16
sd x1, 8(sp)          ; 保存自己的 ra
sd arg9, 0(sp)        ; 栈参数
x10..x17 = args       ; 寄存器参数
jal x1, callee   ---> x1 = 返回地址
                              sp -= frame
                              sd x1, ...       ; 若 callee 还会调用
                              ... body ...
                              ld x1, ...
                              sp += frame
                              jalr x0, 0(x1) --+
ld x1, 8(sp)                                  |
sp += 16                                      |
jalr x0, 0(x1)  <-----------------------------+
```

注意最后两条 `jalr x0, 0(x1)` 是同一个模式，但属于两个不同的函数：callee 的 `x1` 指向 caller 的 `jal` 下一条，caller 的 `x1` 指向它自己的 caller。

## 主线：一次调用从 IR 到汇编

### ABI：`CC_MiniRV` 和 `RetCC_MiniRV`

`MiniRVCallingConv.td` 的约定很小：

```tablegen
def CC_MiniRV : CallingConv<[
  CCIfType<[i32], CCPromoteToType<i64>>,
  CCIfType<[i64], CCAssignToReg<[X10, X11, X12, X13, X14, X15, X16, X17]>>,
  CCIfType<[i64], CCAssignToStack<8, 8>>
]>;

def RetCC_MiniRV : CallingConv<[
  CCIfType<[i32], CCPromoteToType<i64>>,
  CCIfType<[i64], CCAssignToReg<[X10, X11]>>
]>;
```

八个参数寄存器，超过八个就按 8 字节对齐落到栈上；返回值最多两个 XLEN（`i128` 用 `x10`/`x11`，Ch 17 详细讲）。`i32` 先提升到 `i64`，所以 ABI 上所有整型槽都是 8 字节。

`CSR_MiniRV` 决定哪些寄存器要被 prologue/epilogue 保存：

```tablegen
def CSR_MiniRV : CalleeSavedRegs<(add X1, (sequence "X%u", 8, 9),
                                     (sequence "X%u", 18, 27))>;
```

`x1` 在 psABI 里不是 callee-saved，但把它放进这个集合，PEI 就会在函数**写 x1**（也就是发起调用）时自动保存进入时的 ra。效果就是“caller 保存 ra”，只是保存点被放在 prologue，而不是每条 call 前后。RISC-V 参考后端也把 `X1` 放进 `CSR_ILP32_LP64`，是同一个技巧。

### `LowerCall`：从参数分析到结果复制

`MiniRVTargetLowering::LowerCall` 的顺序可以拆成八步：

1. 拒绝 scalable vector 参数。
2. `CCInfo.AnalyzeCallOperands(Outs, CC_MiniRV)` 得到每个参数的位置。
3. 如果 `IsTailCall` 且有栈参数，检查 `musttail`；不能转发就拒绝或降级。
4. `DAG.getCALLSEQ_START(Chain, NumBytes, 0, DL)`。
5. 把直接 callee 变成 `TargetGlobalAddress`/`TargetExternalSymbol`；否则标记 `IsIndirect`。
6. 先写栈参数（`getStore`），再把寄存器参数 `CopyToReg`，形成 `CALL`/`CALL_REG` 节点。
7. 尾调用直接返回 `TAIL`/`TAIL_REG`；否则 `CALLSEQ_END` 并从 `x10` 复制结果。
8. 结果按 `RetCC_MiniRV` 截断/扩展后放进 `InVals`。

关键代码：

```cpp
unsigned NumBytes = CCInfo.getStackSize();
Chain = DAG.getCALLSEQ_START(Chain, NumBytes, 0, DL);
if (GlobalAddressSDNode *G = dyn_cast<GlobalAddressSDNode>(Callee))
  Callee = DAG.getTargetGlobalAddress(G->getGlobal(), DL, PtrVT, G->getOffset());
else if (ExternalSymbolSDNode *S = dyn_cast<ExternalSymbolSDNode>(Callee))
  Callee = DAG.getTargetExternalSymbol(S->getSymbol(), PtrVT);
else
  IsIndirect = true;
```

栈参数的地址是“当前 sp + 偏移”：

```cpp
SDValue Address =
    DAG.getNode(ISD::ADD, DL, PtrVT, StackPtr,
                DAG.getIntPtrConstant(VA.getLocMemOffset(), DL));
MemOpChains.push_back(
    DAG.getStore(Chain, DL, Val, Address,
                 MachinePointerInfo::getStack(MF, VA.getLocMemOffset())));
```

寄存器参数则收集成 `RegsToPass`，稍后 `CopyToReg`。这些寄存器还会作为 call 节点的隐式 use 加进去，保证填参数的 copy 不会被调度到 call 之后。

call 节点带有调用约定保留寄存器掩码：

```cpp
const uint32_t *Mask =
    MF.getSubtarget().getRegisterInfo()->getCallPreservedMask(MF, CallConv);
Ops.push_back(DAG.getRegisterMask(Mask));
```

这就是寄存器分配器判断“哪些值能跨过 call 继续活着”的依据。

尾调用分支：

```cpp
if (CLI.IsTailCall) {
  MF.getFrameInfo().setHasTailCall();
  return DAG.getNode(IsIndirect ? MiniRVISD::TAIL_REG : MiniRVISD::TAIL, DL,
                     MVT::Other, Ops);
}
```

注意它在 `CALLSEQ_END` 之前返回。尾调用不需要“关闭调用序列”，因为不存在一个要返回的 call 点；callee 的返回直接走 caller 的 caller。

### `PseudoCALL` / `PseudoCALLReg` / `PseudoTAIL`

四个伪指令是调用 lowering 的落点：

```tablegen
let isCall = 1, Defs = [X1], hasNoSchedulingInfo = 1 in
def PseudoCALL : MiniRVInst<(outs), (ins bare_symbol:$dst), "jal", "x1, $dst",
                            [(MiniRVcall tglobaladdr:$dst)], InstFormatJ>,
                 PseudoInstExpansion<(JAL X1, simm21_lsb0:$dst)>;

let isCall = 1, Defs = [X1], hasNoSchedulingInfo = 1 in
def PseudoCALLReg : MiniRVInst<(outs), (ins GPR:$rs), "jalr", "x1, $rs, 0",
                               [(MiniRVcall_reg GPR:$rs)], InstFormatI>,
                    PseudoInstExpansion<(JALR X1, GPR:$rs, 0)>;

let isCall = 1, isTerminator = 1, isReturn = 1, isBarrier = 1,
    hasNoSchedulingInfo = 1 in
def PseudoTAIL : MiniRVInst<(outs), (ins bare_symbol:$dst), "jal", "x0, $dst",
                            [(MiniRVtail tglobaladdr:$dst)], InstFormatJ>,
                 PseudoInstExpansion<(JAL X0, simm21_lsb0:$dst)>;
```

- `PseudoCALL`：直接符号，展开 `jal x1, symbol`；`Defs=[X1]`，所以它 clobber ra。
- `PseudoCALLReg`：callee 地址已在寄存器，展开 `jalr x1, rs, 0`。
- `PseudoTAIL`：直接符号，展开 `jal x0, symbol`；没有 `Defs=[X1]`，不写 ra。
- `PseudoTAILReg`：寄存器 callee，展开 `jalr x0, rs, 0`。

`PseudoInstExpansion` 让 AsmPrinter 在最后一步把伪指令换成真实编码；MIR 里看到的仍然是 `PseudoCALL`/`PseudoCALLReg`，这方便 verifier 识别“这是一次调用”，而不是一条普通 `jal`。

### `ADJCALLSTACKDOWN/UP` 到底做什么

`LowerCall` 对普通调用无条件发 `CALLSEQ_START/END`（commit `11d2d448a7d8`“Always emit the call sequence”）；尾调用在 START 之后直接返回 TAIL，不发 END。ISel 把它们匹配成：

```tablegen
let isCodeGenOnly = 1, Defs = [X2], Uses = [X2] in {
def ADJCALLSTACKDOWN : MiniRVInst<(outs), (ins i64imm:$amt1, i64imm:$amt2),
                                  "#ADJCALLSTACKDOWN", "$amt1, $amt2",
                                  [(callseq_start timm:$amt1, timm:$amt2)]>;
def ADJCALLSTACKUP : MiniRVInst<(outs), (ins i64imm:$amt1, i64imm:$amt2),
                                "#ADJCALLSTACKUP", "$amt1, $amt2",
                                [(callseq_end timm:$amt1, timm:$amt2)]>;
}
```

它们的 `Defs/Uses` 都是 `x2`，但打印成 `#ADJCALLSTACKDOWN`，说明它们不是真实指令。PEI 调 `MiniRVFrameLowering::eliminateCallFramePseudoInstr`：

- 如果 `hasReservedCallFrame(MF)` 为真，prologue 已经把最大的 outgoing 参数区算进 frame，伪指令直接删掉。
- 如果函数有动态 alloca，`hasReservedCallFrame` 为假，伪指令变成 `addi x2, x2, ±amount`，amount 还要 `alignTo(..., 16)`。

所以“有 `ADJCALLSTACKDOWN`”不等于“汇编里会出现 `addi sp`”。MiniRV 的普通函数都是 reserved call frame，最终汇编里看不到这两条。

### `LowerFormalArguments`：callee 侧

callee 的参数分析用 `CCInfo.AnalyzeFormalArguments(Ins, CC_MiniRV)`。寄存器参数通过虚拟寄存器变成 live-in：

```cpp
Register VReg = RegInfo.createVirtualRegister(&MiniRV::GPRRegClass);
RegInfo.addLiveIn(VA.getLocReg(), VReg);
Arg = DAG.getCopyFromReg(Chain, DL, VReg, VA.getLocVT());
```

栈参数则创建一个 fixed object：

```cpp
FI = MF.getFrameInfo().CreateFixedObject(VA.getLocVT().getStoreSize(),
                                         Offset, /*IsImmutable=*/true);
SDValue FIN = DAG.getTargetFrameIndex(FI, PtrVT);
Arg = DAG.getLoad(VA.getLocVT(), DL, Chain, FIN,
                  MachinePointerInfo::getFixedStack(MF, FI));
```

`Offset` 是相对**进入时 sp** 的偏移。fixed object 不占本地 frame 的布局，它只是告诉 PEI“这个地址在 incoming argument area 里”。窄参数（`i8`/`i16`/`i32`）会用 `getExtLoad` 读，并按调用方给的 `SExt`/`ZExt` 标志扩展；这就是 ABI 里“窄整型按 XLEN 传递”的实现。

### `LowerReturn`：callee 出口

`LowerReturn` 先用 `RetCC_MiniRV` 分析返回值，把每个值复制到 `x10`/`x11`，再发 `MiniRVISD::RET`：

```cpp
Chain = DAG.getCopyToReg(Chain, DL, VA.getLocReg(), Val, Glue);
...
return DAG.getNode(MiniRVISD::RET, DL, MVT::Other, RetOps);
```

`PseudoRET` 展开成 `JALR X0, X1, 0`。`isReturn=1`、`isBarrier=1`、`isTerminator=1` 让机器层知道这是函数出口，不会把它当成可 fallthrough 的普通跳转。

### prologue/epilogue 与 spill hooks

`MiniRVFrameLowering::emitPrologue` 先调整 sp。小 frame 一条 `addi`；超过 12 位就 `adjustReg` 拆成两条 `addi`，再大就 `lui`+`addi`+`add`。`getFirstSPAdjustAmount` 在大 frame 里先调一半，让 callee-saved spill 的偏移仍在 12 位内。

callee-saved 的保存/恢复走 `MiniRVInstrInfo` 的两个 hook：

```cpp
// store: SD SrcReg, FrameIndex, 0
BuildMI(MBB, MI, ..., get(MiniRV::SD))
    .addReg(SrcReg, getKillRegState(IsKill))
    .addFrameIndex(FrameIndex).addImm(0);
// load: LD DestReg, FrameIndex, 0
BuildMI(MBB, MI, ..., get(MiniRV::LD), DestReg)
    .addFrameIndex(FrameIndex).addImm(0);
```

所有 GPR 都是 64 位，所以一个 `SD`/`LD` 覆盖全部溢出；没有 FPR，也就不需要按寄存器类分支。`FrameIndex` 在 `MiniRVRegisterInfo::eliminateFrameIndex` 里变成 `offset(x2)` 或 `offset(x8)`。

`copyPhysReg` 也很小：

```cpp
BuildMI(MBB, MI, DL, get(MiniRV::ADDI), DestReg)
    .addReg(SrcReg, getKillRegState(KillSrc))
    .addImm(0);
```

corpus 没有 `mv`，`addi rd, rs, 0` 就是 move。间接调用前的 `addi x3, x10, 0` 就是它生成的。

### 尾调用与 `musttail`
普通 `tail call` 在 `LowerCall` 里走 `TAIL` 分支，生成 `jal x0`/`jalr x0`。但如果 callee 需要栈参数，代码会先检查：

```cpp
if (CLI.IsTailCall && CCInfo.getStackSize() != 0) {
  if (CLI.CB && CLI.CB->isMustTailCall())
    report_fatal_error(
        "MiniRV cannot tail call a function with stack arguments");
  CLI.IsTailCall = false;
}
```

原因在栈布局：栈参数要写到“当前函数接收自己栈参数的那块区域”，而 epilogue 马上就要把这块区域还给调用者。写进去的内容会被 callee 当成自己的参数读，语义就错了。

- 普通 `tail`：可以降级成普通 call，所以 `with_stack_arg` 生成 `jal x1, nine`，然后正常 `jalr x0, 0(x1)` 返回。
- `musttail`：LLVM 要求它必须保持尾调用语义，不能降级；MiniRV 只能 fatal。

`musttail` 本身还有 LangRef 约束：call 后必须紧跟 `ret`、调用约定和签名要兼容、不能是 variadic 等。MiniRV 的额外限制只有一条：**不能有栈参数**。

### `callsEHReturn` 与 `callsUnwindInit`

这两个不是 ABI 参数，而是 `MachineFunction` 上的异常处理标志：

- `SelectionDAGBuilder` 看到 `llvm.eh.return.i32/i64` 时 `setCallsEHReturn(true)`，并构造 `ISD::EH_RETURN`。
- 看到 `llvm.eh.unwind.init` 时 `setCallsUnwindInit(true)`；`TargetFrameLowering::determineUncondPrologCalleeSaves` 会因此保存**所有** callee-saved 寄存器，因为 `longjmp` 可能从任意点恢复。

MiniRV 在 `MiniRVTargetMachine` 构造函数里关掉两个异常模型：

```cpp
// There is no unwinder, no personality routine and no exception register.
setSupportsExceptionModel(ExceptionHandling::DwarfCFI, false);
setSupportsExceptionModel(ExceptionHandling::SjLj, false);
```

关掉之后，codegen pipeline 跑 `LowerInvoke`：`invoke` 变成普通 `call`，landing pad 不可达并被删除，personality routine 不再被引用。这是 commit `9eeed8d`“Lower invokes for an unwindless target”做的事。没有这一步，`resume` 会缺 `_Unwind_Resume` libcall，最后掉进 `SelectionDAGBuilder` 的 `llvm_unreachable`。

但“禁用异常模型”不等于“实现了 EH 返回”。`callsEHReturn` 仍可能被 intrinsic 置位，MiniRV 没有对应的 EH return lowering；`llvm.eh.return.i64` 在 MiniRV 上会生成一个空函数体（实测见下）。这不是可用的异常路径，只是“不要用它”的边界。
## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 调用约定 | `MiniRVCallingConv.td` | `CC_MiniRV`、`RetCC_MiniRV` | 8 个参数寄存器、栈参数、`x10/x11` 返回 |
| caller lowering | `MiniRVISelLowering.cpp` | `LowerCall` | 参数分析、CALLSEQ、栈参数、TAIL |
| callee 入口 | `MiniRVISelLowering.cpp` | `LowerFormalArguments` | live-in、fixed object、窄参数扩展 |
| 返回 | `MiniRVISelLowering.cpp` | `LowerReturn`、`CanLowerReturn` | `RET`、`x10/x11` |
| 调用伪指令 | `MiniRVInstrInfo.td` | `PseudoCALL`、`PseudoCALLReg`、`PseudoTAIL`、`PseudoTAILReg` | `Defs=[X1]` 与 `jal x0` |
| 调用序列伪指令 | `MiniRVInstrInfo.td` | `ADJCALLSTACKDOWN/UP`、`callseq_start/end` | `Defs/Uses=[X2]`、PEI 删除 |
| 寄存器保存 | `MiniRVInstrInfo.cpp` | `copyPhysReg`、`storeRegToStackSlot`、`loadRegFromStackSlot` | `addi rd,rs,0`、`SD`/`LD` |
| 栈帧 | `MiniRVFrameLowering.cpp` | `emitPrologue`、`emitEpilogue`、`spillCalleeSavedRegisters`、`eliminateCallFramePseudoInstr` | 大 frame 拆分、reserved call frame |
| 寄存器集合 | `MiniRVRegisterInfo.td`、`.cpp` | `CSR_MiniRV`、`getReservedRegs`、`getCallPreservedMask` | `x1` 为什么在 CSR 里、`x5/x6` 保留 |
| 异常模型 | `MiniRVTargetMachine.cpp` | `setSupportsExceptionModel` | DwarfCFI/SjLj 关闭 |

## 实测：direct / indirect / 9 参数调用

先看 direct 和 indirect（过滤掉 `.cfi` 和声明行，只保留指令，下同）：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 calls.ll -o - | grep -vE '^\s*\.(file|text|globl|type|size|cfi)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | sed -n '1,18p'
direct:                                 # @direct
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, callee
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
indirect:                               # @indirect
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	addi	x3, x10, 0
	addi	x10, x11, 0
	jalr	x1, 0(x3)
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

`direct` 的四件事：`addi x2, x2, -16` 分配 frame；`sd x1, 8(x2)` 保存 caller 自己的 ra（`x1` 在 `CSR_MiniRV` 里，而 `jal x1, callee` 会写它）；`jal x1, callee` 发起调用；`ld x1` + `addi x2` + `jalr x0, 0(x1)` 恢复并返回。

`indirect` 多了两条 `addi ..., 0`：`addi x3, x10, 0` 是 `copyPhysReg`（函数指针从 `x10` 挪到 `x3`），`addi x10, x11, 0` 是把第一个实参放到 `x10`；然后 `jalr x1, 0(x3)`。

9 个参数的 caller：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 call_args9.ll -o - | grep -vE '^\s*\.(file|text|globl|type|size|cfi)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | head -17
call_args9:                             # @call_args9
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	addi	x3, x0, 9
	sd	x3, 0(x2)
	addi	x11, x0, 2
	addi	x12, x0, 3
	addi	x13, x0, 4
	addi	x14, x0, 5
	addi	x15, x0, 6
	addi	x16, x0, 7
	addi	x17, x0, 8
	jal	x1, args9
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

第 9 个参数是 `sd x3, 0(x2)`，不是 `8(x2)`：frame 是 16 字节，ra 在 `8(x2)`，outgoing 参数区从 `0(x2)` 开始。`x10` 是第一个参数（`%a`），已经在 `x10` 里，所以没有 copy；`x11..x17` 是常量 2..8，用 `addi` 立即数。

MIR 层能看到伪指令和 `ADJCALLSTACKDOWN`：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel calls.ll -o - | grep -E "ADJCALLSTACK|PseudoCALL|PseudoRET|SD %"
    ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
    PseudoCALL @callee, csr_minirv, implicit-def dead $x1, implicit $x10, implicit-def $x2, implicit-def $x10
    ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
    PseudoRET implicit $x10
    ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
    PseudoCALLReg %0, csr_minirv, implicit-def dead $x1, implicit $x10, implicit-def $x2, implicit-def $x10
    ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
    PseudoRET implicit $x10
    ADJCALLSTACKDOWN 8, 0, implicit-def dead $x2, implicit $x2
    SD %2, %1, 0 :: (store (s64) into stack)
    PseudoCALL @nine, csr_minirv, implicit-def dead $x1, implicit $x10, implicit $x11, implicit $x12, implicit $x13, implicit $x14, implicit $x15, implicit $x16, implicit $x17, implicit-def $x2, implicit-def $x10
    ADJCALLSTACKUP 8, 0, implicit-def dead $x2, implicit $x2
    PseudoRET implicit $x10
```

`PseudoCALL @nine` 把 `x10..x17` 全部列成隐式 use；`ADJCALLSTACKDOWN 8` 表示这次调用需要 8 字节 outgoing 区。最终汇编没有 `addi x2, x2, -8`，因为 PEI 把它算进了 prologue 的 16 字节 frame。

## 实测：和 riscv64 的 `call`/`jalr` 对比

同一份 `calls.ll` 用参考后端编译，过滤掉声明和 `.cfi`：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=riscv64 -mattr=+m -O2 calls.ll -o - | grep -vE '^\s*\.(attribute|file|text|globl|type|size|cfi|p2align)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | sed -n '1,16p'
direct:                                 # @direct
# %bb.0:
	addi	sp, sp, -16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	call	callee
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	addi	sp, sp, 16
	ret
indirect:                               # @indirect
# %bb.0:
	addi	sp, sp, -16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	mv	a2, a0
	mv	a0, a1
	jalr	a2
	ld	ra, 8(sp)                       # 8-byte Folded Reload
```

差异不是“谁对谁错”，而是抽象层次不同：

- RISC-V 的 `call` 是汇编器伪指令，通常展开成 `auipc ra, %pcrel_hi` + `jalr ra, %pcrel_lo(ra)`，链接器可在 `+relax` 下缩成 `jal ra`。MiniRV 直接发 `jal x1, callee`，没有链接器 relaxation，只有 codegen 里的 BranchRelaxation。
- RISC-V 打印别名（`ra`、`sp`、`a0`），`jalr a2` 省略默认的 `ra` 目的寄存器；MiniRV 打印 `x` 编号，并显式写 `jalr x1, 0(x3)`。
- RISC-V 用 `ret`；MiniRV 用 `jalr x0, 0(x1)`。RISC-V 的 move 是 `mv`；MiniRV 是 `addi rd, rs, 0`。
- 两边都把 `ra` 溢出到 `8(sp)`/`8(x2)`，因为调用会写返回地址寄存器。

## 实测：callee 侧怎么读第 9 个参数

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 args9def.ll -o - | sed -n '1,11p'
	.file	"args9def.ll"
	.text
	.globl	args9                           # -- Begin function args9
	.type	args9,@function
args9:                                  # @args9
	.cfi_startproc
# %bb.0:
	add	x3, x10, x11
	ld	x4, 0(x2)
	add	x10, x3, x4
	jalr	x0, 0(x1)
```

`args9` 是 leaf，不需要 frame，所以 `x2` 还是进入时的 sp；第 9 个参数在 `0(x2)`。如果它再调用别的函数，prologue 会先 `addi x2, x2, -N`，fixed object 的地址由 `getFrameIndexReference` 重算（无 FP 时 `x2+StackSize`，有 FP 时 `x8`），读到的仍是同一个 incoming slot。

## 实测：`musttail` 被拒绝

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/musttail-error.ll -o /dev/null 2>&1 | head -2
LLVM ERROR: MiniRV cannot tail call a function with stack arguments
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce the bug.
```

`musttail-error.ll` 的调用有 9 个参数，第 9 个必须走栈；MiniRV 不能把栈参数转发给 callee，也不能把 `musttail` 降级，所以只能报错。普通 `tail` 没有这个约束：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 tailstack.ll -o - | grep -vE '^\s*\.(file|text|globl|type|size|cfi)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | head -17
with_stack_arg:                         # @with_stack_arg
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	sd	x10, 0(x2)
	addi	x10, x0, 1
	addi	x11, x0, 2
	addi	x12, x0, 3
	addi	x13, x0, 4
	addi	x14, x0, 5
	addi	x15, x0, 6
	addi	x16, x0, 7
	addi	x17, x0, 8
	jal	x1, nine
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

它没有生成 `jal x0, nine`，而是 `jal x1, nine` 加正常返回。`tail` 只是优化提示，不能保持时就可以放弃；`musttail` 是语义要求，不能放弃。

## 实测：异常模型关闭后的 `invoke` 和 `unwind.init`

`eh.ll` 里的 `invoke` 被降成普通 call：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/eh.ll -o - | grep -vE '^\s*\.(file|text|globl|type|size|cfi)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | head -9
invoke_test:                            # @invoke_test
# %bb.0:                                # %entry
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, may_throw
# %bb.1:                                # %ok
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

没有 landing pad，没有 `__gxx_personality`，只有一条普通 `jal x1, may_throw`。

`llvm.eh.unwind.init` 会置 `callsUnwindInit`，于是所有 callee-saved 都被保存：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 unwindinit.ll -o - | grep -vE '^\s*\.(file|text|globl|type|size|cfi)|^\.Lfunc_end|^\s*# -- End function|^\s*$' | head -16
f:                                      # @f
# %bb.0:                                # %entry
	addi	x2, x2, -112
	sd	x1, 104(x2)
	sd	x8, 96(x2)
	sd	x9, 88(x2)
	sd	x18, 80(x2)
	sd	x19, 72(x2)
	sd	x20, 64(x2)
	sd	x21, 56(x2)
	sd	x22, 48(x2)
	sd	x23, 40(x2)
	sd	x24, 32(x2)
	sd	x25, 24(x2)
	sd	x26, 16(x2)
	sd	x27, 8(x2)
```

这个函数根本没有调用，却保存了 13 个寄存器。原因不是 MiniRV 的调用约定，而是 `determineUncondPrologCalleeSaves` 对 `callsUnwindInit` 的通用处理：`longjmp` 可能从任意点恢复，所以所有 callee-saved 都要有 home。

`llvm.eh.return.i64` 则暴露了 MiniRV 的边界：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 ehret.ll -o - | sed -n '5,10p'
f:                                      # @f
	.cfi_startproc
# %bb.0:                                # %entry
.Lfunc_end0:
	.size	f, .Lfunc_end0-f
	.cfi_endproc
```

函数体是空的。`callsEHReturn` 在 MIR 里是 `true`，但 MiniRV 没有 EH return lowering；这条路径不属于支持范围。不要把“关闭异常模型”误当成“实现了异常返回”。
## 反直觉与踩雷

1. **错误直觉**：caller 必须保存所有参数寄存器。→ **正确认识**：参数寄存器是 caller-saved，只有跨 call 还活着的值才需要保存；MiniRV 只保存 `x1` 和真正用到的 callee-saved 寄存器。
2. **错误直觉**：看到 `ADJCALLSTACKDOWN` 就一定会看到 `addi sp`。→ **正确认识**：普通函数用 reserved call frame，PEI 直接删除伪指令；只有动态 alloca 才在 call site 调整 sp。
3. **错误直觉**：`sd x1, 8(x2)` 是“每条 call 前后的保存”。→ **正确认识**：它在 prologue 里保存进入时的 ra；因为 `x1` 在 CSR 集合里，PEI 在函数会写 `x1` 时统一保存。
4. **错误直觉**：第 9 个参数在 `8(x2)`，因为 ra 在 `8(x2)`。→ **正确认识**：ra 占 `8(x2)`，outgoing 参数区从 `0(x2)` 开始；callee 进入时读 `0(x2)`。
5. **错误直觉**：`musttail` 和 `tail` 一样，不能保持就降级。→ **正确认识**：`musttail` 是语义要求；MiniRV 对有栈参数的 `musttail` 直接 fatal，普通 `tail` 才降级。

## 进阶：再往深一层

**`PseudoCALL` 为什么需要 `Defs=[X1]`**。如果只写 `isCall=1`，寄存器分配器不知道 `x1` 被写，可能把一个活值放在 `x1` 里跨过 call。`Defs=[X1]` 让 `x1` 成为 call 的 def，同时 `CSR_MiniRV` 让 PEI 在 prologue 保存进入值；两者配合，才能既保护 caller 的 ra，又允许 callee 写自己的 ra。

**reserved call frame 的收益**。`hasReservedCallFrame` 为真时，prologue 一次性分配“局部对象 + 最大 outgoing 参数区”，call site 不需要 `addi sp`。代价是 frame 稍微大一点，收益是 call site 更短、栈指针在整个函数体里稳定，fixed object 和 frame index 的偏移也更容易算。动态 alloca 打破这个前提，所以 `eliminateCallFramePseudoInstr` 必须保留 call-site 调整。

**EH 模型是“能力声明”，不是“代码生成开关”**。`setSupportsExceptionModel(ExceptionHandling::DwarfCFI, false)` 告诉通用 pipeline“不要为这个 target 生成 DWARF EH 准备代码”，于是 `LowerInvoke` 接管。它不会删除 `llvm.eh.return`/`llvm.eh.unwind.init` 这些 intrinsic；这些 intrinsic 仍然会设置 `MachineFunction` 标志。target 要么实现它们，要么明确拒绝。MiniRV 目前是“没有实现，但也没有诊断”的灰区。
## 本章重点整理

- 调用序列 = caller 保存 ra + 参数搬运 + `PseudoCALL`/`PseudoCALLReg` + callee prologue/epilogue + `PseudoRET`。
- `CC_MiniRV` 用 `x10..x17` 传 8 个参数，其余按 8 字节对齐入栈；返回值在 `x10`/`x11`。
- `ADJCALLSTACKDOWN/UP` 是 `CALLSEQ_START/END` 的机器层表示；reserved call frame 下会被 PEI 删除。
- `PseudoCALL`/`PseudoCALLReg` 定义 `x1`；`PseudoTAIL`/`PseudoTAILReg` 不定义 `x1`，用 `x0` 保留返回地址。
- `musttail` 不能降级；MiniRV 只允许寄存器参数，栈参数直接 fatal。
- MiniRV 关闭 DwarfCFI/SjLj，`invoke` 降为 call；`callsUnwindInit` 仍会触发全 callee-saved 保存，`callsEHReturn` 没有可用实现。

## 自我检核

- 不看源码，按顺序写出 `LowerCall` 的八步，并标出 `CALLSEQ_START` 和 `CALLSEQ_END` 的位置。
- 解释 `sd x1, 8(x2)` 为什么在 prologue，而不是每条 `jal` 前后；如果去掉 `CSR_MiniRV` 里的 `X1` 会发生什么？
- 9 个参数时，caller 的 `sd x3, 0(x2)` 和 callee 的 `ld x4, 0(x2)` 为什么能对上？如果 callee 有 frame 呢？
- `PseudoCALL` 和 `PseudoTAIL` 的 TableGen 标志有什么不同？哪个标志保证 `x1` 不被写？
- 为什么普通 `tail` 有栈参数时可以降级，而 `musttail` 不行？

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 项目
  - **读哪里**：SelectionDAG 的 call lowering、`CALLSEQ_START/END`、`LowerCall`/`LowerFormalArguments` 的说明。
  - **能学到什么**：为什么调用需要 chain 和 glue，为什么参数 copy 要作为 call 的隐式 use。
  - **前提**：先读本课程 Ch 13 和 Ch 15。

- **[LLVM LangRef: `musttail`](https://llvm.org/docs/LangRef.html#musttail)** — LLVM 项目
  - **读哪里**：`musttail` 的合法性约束和“call 后必须紧跟 ret”的规则。
  - **能学到什么**：为什么 `musttail` 不能像 `tail` 一样降级，以及签名/调用约定一致性的要求。
  - **前提**：知道 `tail` 与 `musttail` 的区别。

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 社区
  - **读哪里**：整数调用约定、参数寄存器、栈帧布局、`ra`/`sp` 的角色。
  - **能学到什么**：`x10..x17` 为什么是参数寄存器，栈参数为什么在 entry sp 之上，`ra` 为什么需要 caller 保存。
  - **前提**：能读基本 RISC-V 汇编。
→ [Ch 17 参数与返回值](./17-arguments-and-returns.md)
