# Ch 17 — 参数与返回值

> **目标**：能把一条 `call`/`ret` 从 IR 追到 `CCState` 的 `CCValAssign`，再到 `LowerFormalArguments`/`LowerCall`/`LowerReturn` 生成的 DAG，并解释 i128、varargs、sret/byval 各自把值放在哪里、为什么。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

Clang 生成 `call i64 @f(i64 %a, ..., i64 %i)` 时，它不知道也不应该知道 `x10..x17` 这套约定；SelectionDAG 的通用代码同样不知道。中间必须有“ABI 描述”和“lowering 实现”两层：

- `CallingConv.td` 声明位置；
- `CCState` 把声明应用到每个值部分，产出 `CCValAssign`；
- `LowerFormalArguments`/`LowerCall`/`LowerReturn` 把值真正搬进/搬出这些位置。

## 先建立直觉

### 一条 call 的流水线

```
IR: call i64 @args9(i64 %x, ..., i64 9)
      │ ComputeValueVTs 先把非法类型拆成合法部分
      ▼
CCState::AnalyzeCallOperands(Outs, CC_MiniRV)
      │ 产出 CCValAssign：x10..x17，第 9 个是 MemLoc offset=0
      ▼
LowerCall
      ├─ 栈参数 store 到 x2+offset；寄存器参数 CopyToReg
      ├─ CALL + RegisterMask；CALLSEQ_END
      └─ 返回值 CopyFromReg x10（i128 再加 x11）
      ▼
PseudoCALL → jal x1, args9
```

入口侧对称：`LowerFormalArguments` 对 RegLoc 做 `addLiveIn` + `CopyFromReg`，对 MemLoc 做 `CreateFixedObject` + load，varargs 则把剩余参数寄存器存到入口 SP 下方。返回侧是 `AnalyzeReturn` → `CopyToReg x10/x11` → `PseudoRET`；`CanLowerReturn` 为 false 时静默改成 sret。

### 寄存器与栈槽布局

```
参数序号   1     2     3     4     5     6     7     8      9       10
寄存器   x10   x11   x12   x13   x14   x15   x16   x17    —       —
栈偏移    —     —     —     —     —     —     —     —    0(x2)   8(x2)
返回值   x10 = 低 64 位；x11 = 高 64 位（仅 i128 这类两个 i64 部分）
```

与 RISC-V lp64d 的对照：

| 项目 | MiniRV | RISC-V lp64d |
|---|---|---|
| 整数参数 / 返回 | `x10..x17` / `x10,x11` | `a0..a7` / `a0,a1` |
| 第 9 个参数 | `0(x2)` | `0(sp)` |
| 浮点 | 无 FPU，soft-float libcall | `fa0..fa7` |
| 栈槽 | 8 字节，8 对齐 | 按类型对齐 |
| varargs | `x11..x17` 存入口 SP 下 | `a0..a7` 存保存区 |
| sret / byval | 普通指针 / 直接传指针 | 普通指针 / 调用点 `memcpy` |
| callee-saved | `x1,x8,x9,x18..x27` | 同左 + `fs0..fs11` |
| 栈对齐 | 16 | 16 |

这里的 `0(x2)` 是 **callee 入口时的 x2**。如果 callee 有自己的栈帧，prologue 之后 x2 已经下移，第 9 个参数就变成 `StackSize+0(x2)`；MiniRV 用 fixed object 记住“相对入口 SP”的偏移，`getFrameIndexReference` 负责补上 `StackSize`。

```
高地址
┌──────────────────────────────┐
│ caller 的 locals / spills     │
├──────────────────────────────┤
│ 第 9/10 个参数                │ 0/8(入口 x2)  ← fixed object
├──────────────────────────────┤ ← 入口 x2
│ callee 的 ra spill            │ 8(当前 x2)    ← addi x2,x2,-16
│ callee 的 outgoing arg        │ 0(当前 x2)
└──────────────────────────────┘
低地址
```

## 主线 1：`CC_MiniRV` 与 `CCState`

MiniRV 的约定只有三条规则（`MiniRVCallingConv.td`）：

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

TableGen 为这两个 record 生成两个 `CCAssignFn`（`MiniRVGenCallingConv.inc`，在 `MiniRVISelLowering.cpp` 里通过 `#define GET_CALLING_CONV_IMPL` 引入）。关键在于：**td 不搬任何值，它只回答“第 N 个值部分放哪”**。

`CCState` 是这件事的运行时状态机：

```cpp
SmallVector<CCValAssign, 16> ArgLocs;
CCState CCInfo(CallConv, IsVarArg, MF, ArgLocs, *DAG.getContext());
CCInfo.AnalyzeCallOperands(Outs, CC_MiniRV);
```

`CCValAssign` 里最常用的字段：

| 字段 | 含义 | MiniRV 里的例子 |
|---|---|---|
| `ValNo` | 第几个 IR 值（或拆分后的第几部分） | `args9` 的 `%i` 是 8 |
| `ValVT` | IR 侧的值类型 | `i64` |
| `LocVT` | ABI 位置类型（可能是提升后的） | `i32` → `i64` |
| `LocInfo` | 扩展方式 `SExt/ZExt/AExt` | `CCIfType<[i32],...>` 记录 |
| `Loc` | 寄存器号或栈偏移 | `X10` / `0` |

四个入口分别对应四个方向：

| 方向 | CCState 调用 | 结果 |
|---|---|---|
| callee 入口 | `AnalyzeFormalArguments(Ins, CC_MiniRV)` | 每个形参的位置 |
| caller 出口 | `AnalyzeCallOperands(Outs, CC_MiniRV)` | 每个实参的位置 |
| return | `AnalyzeReturn(Outs, RetCC_MiniRV)` | 返回值位置 |
| 能力检查 | `CheckReturn(Outs, RetCC_MiniRV)` | 能不能不用 sret |

## 主线 2：入口、出口、返回三个 lowering

### 入口：`LowerFormalArguments`

```cpp
CCInfo.AnalyzeFormalArguments(Ins, CC_MiniRV);
for (CCValAssign &VA : ArgLocs) {
  if (VA.isRegLoc()) {
    Register VReg = RegInfo.createVirtualRegister(&MiniRV::GPRRegClass);
    RegInfo.addLiveIn(VA.getLocReg(), VReg);
    Arg = DAG.getCopyFromReg(Chain, DL, VReg, VA.getLocVT());
  } else {
    unsigned Offset = VA.getLocMemOffset();
    int FI = MF.getFrameInfo().CreateFixedObject(ValVT.getStoreSize(), Offset, true);
    Arg = DAG.getLoad(VA.getLocVT(), DL, Chain, FIN, ...);
  }
  if (Arg.getValueType() != VA.getValVT())
    Arg = DAG.getNode(ISD::TRUNCATE, DL, VA.getValVT(), Arg);
}
```

三个容易忽略的点：

1. `addLiveIn` 是必须的。没有它，寄存器分配不知道 `$x10` 在函数入口就等于这个虚拟寄存器，会把它当成未定义值。
2. 栈参数用 `CreateFixedObject`，不是 `CreateStackObject`。fixed object 的偏移永远相对**入口 SP**（以及 CFA），不会被 prologue 的 `addi x2,x2,-N` 影响。
3. 对窄类型，`ValVT.getStoreSize()` 只读 4/2/1 字节，而槽本身仍是 8 字节（`CCAssignToStack<8,8>`）。所以 `read_nine32` 是 `lw x10,0(x2)`，不是 `ld`。

### 出口：`LowerCall`

```cpp
CCInfo.AnalyzeCallOperands(Outs, CC_MiniRV);
...
if (VA.isRegLoc())
  RegsToPass.emplace_back(VA.getLocReg(), Val, VA.getLocVT());
else
  MemOpChains.push_back(DAG.getStore(Chain, DL, Val, Address, ...));
...
for (auto &[Reg, Val, VT] : RegsToPass) {
  Chain = DAG.getCopyToReg(Chain, DL, Reg, Val, Glue);
  ArgRegs.push_back(DAG.getRegister(Reg, VT));
}
```

顺序很重要：**先写栈参数，再写寄存器参数**。栈参数存在 `x2 + offset`，寄存器参数用 `CopyToReg`；把 `getRegister` 加进 `CALL` 的操作数，才能让这些寄存器成为 call 的隐式使用（否则寄存器分配器可能在 call 前把它们重用掉）。

`CALL` 还带 `RegisterMask`（`getCallPreservedMask`，MiniRV 是 `CSR_MiniRV_RegMask`）。这是寄存器分配器判断“调用后哪些寄存器还活着”的唯一依据。

`LowerCall` 里还有一条规则：**有栈参数的 tail call 会退化成普通调用**；`musttail` 则直接报错。理由是自己和 callee 要共用同一块出参区，而 epilogue 马上就要把它还给 caller。

### 返回：`LowerReturn` 与 `CanLowerReturn`

```cpp
CCInfo.AnalyzeReturn(Outs, RetCC_MiniRV);
for (unsigned I = 0; I != RVLocs.size(); ++I) {
  CCValAssign &VA = RVLocs[I];
  assert(VA.isRegLoc() && "MiniRV returns in registers only");
  SDValue Val = OutVals[I];
  if (Val.getValueType() != VA.getLocVT())
    Val = DAG.getNode(ISD::SIGN_EXTEND, DL, VA.getLocVT(), Val);
  Chain = DAG.getCopyToReg(Chain, DL, VA.getLocReg(), Val, Glue);
}
return DAG.getNode(MiniRVISD::RET, DL, MVT::Other, RetOps);
```

`CanLowerReturn` 是“预算检查”，在 lowering 之前由 `FunctionLoweringInfo` 调用：

```cpp
bool MiniRVTargetLowering::CanLowerReturn(...) const {
  SmallVector<CCValAssign, 4> RVLocs;
  CCState CCInfo(CallConv, IsVarArg, MF, RVLocs, Context);
  return CCInfo.CheckReturn(Outs, RetCC_MiniRV);
}
```

`CheckReturn` 为 false 时，`FunctionLoweringInfo::CanLowerReturn` 为 false，`SelectionDAGBuilder::visitRet` 走“把返回值 store 到隐藏指针”的路径（`DemoteRegister`），也就是**静默降级成 sret**。这不是错误，也不会打印诊断；它只是改变了 ABI。i128 在 `a61c0cb` 之前就踩了这个坑。

## 主线 3：i128——先拆类型，再分配寄存器对

IR 的 `i128` 在 SelectionDAG 里不是一个合法寄存器类型：MiniRV 只 `addRegisterClass(MVT::i64, ...)`。所以参数/返回值在进入调用约定分析前，已经被 `ComputeValueVTs` 拆成两个 `i64` 部分（`InputArg`/`OutputArg` 各一个）。这带来两个直接后果：

- `CC_MiniRV` 看到的是 `i64`、`i64`，不是 `i128`；`CCAssignToReg` 依次分配 `x10`、`x11`。
- `RetCC_MiniRV` 必须允许 **两个** 寄存器，否则第二个部分无家可归。

`a61c0cb` 就是这一行：

```diff
 def RetCC_MiniRV : CallingConv<[
   CCIfType<[i32], CCPromoteToType<i64>>,
-  CCIfType<[i64], CCAssignToReg<[X10]>>
+  CCIfType<[i64], CCAssignToReg<[X10, X11]>>
 ]>;
```

commit message 写得很直白：之前 128 位返回“被降级成隐藏的 sret 指针，即使参数已经在寄存器里”。改完之后 `sub128` 的低半在 `x10`、高半在 `x11`，`use128` 直接 `jal`，没有 sret 指针。

`10b7b4a` 补的是另一面：i128 的除法/取余没有指令，类型合法化会生成 `__divti3`/`__udivti3`/`__modti3`/`__umodti3` 调用。参数按普通调用约定落在 `x10..x13`，返回值落在 `x10/x11`，所以 lowering 不需要任何特殊代码，只需要在 `MiniRVSubtarget::initLibcallLoweringInfo` 里给四个操作命名实现：

```cpp
Info.setLibcallImpl(RTLIB::SDIV_I128, RTLIB::impl___divti3);
Info.setLibcallImpl(RTLIB::UDIV_I128, RTLIB::impl___udivti3);
Info.setLibcallImpl(RTLIB::SREM_I128, RTLIB::impl___modti3);
Info.setLibcallImpl(RTLIB::UREM_I128, RTLIB::impl___umodti3);
```

如果只改返回寄存器、不改 libcall 表，`sdiv i128` 会在选择阶段报 “unsupported library call operation”；如果只改 libcall 表、不改返回寄存器，helper 的返回值又会走 sret，调用点对不上。两个 commit 必须成对看。

## 主线 4：varargs——保存区就是 `va_list` 的数组

varargs 的难点不在 call site（未命名参数就是普通出参），而在 callee：`va_start` 需要给 `va_arg` 一个**可寻址的槽数组**，而前 8 个未命名参数可能还在寄存器里。

`a356518` 的做法：

```cpp
static const MCPhysReg ArgRegs[] = {X10, X11, X12, X13, X14, X15, X16, X17};
unsigned Idx = CCInfo.getFirstUnallocated(ArgRegs);
int SaveSize = 8 * (NumArgRegs - Idx);
...
FI = MFI.CreateFixedObject(SaveSize, -SaveSize, /*IsImmutable=*/true);
for (unsigned I = Idx; I != NumArgRegs; ++I) {
  ... store ArgRegs[I] at FIN + (I - Idx) * 8 ...
}
RVFI->setVarArgsFrameIndex(FI);
```

布局的选择是本章最值得记住的设计：

```
入口 SP = x2_entry
        ┌──────────────────────────────┐
        │ 未命名栈参数（第 9、10... 个）│ 0(x2_entry)
        ├──────────────────────────────┤
        │ x17 的保存槽                 │ -8(x2_entry)
        │ x16 的保存槽                 │ -16
        │ ...                          │
        │ x11 的保存槽                 │ -(SaveSize)
        ├──────────────────────────────┤ ← 保存区起点 = va_list 的初始值
        │ va_list 对象（alloca）        │ -(SaveSize+8)
        └──────────────────────────────┘
```

保存区选在 `[-SaveSize, 0)`，正好和入口栈参数 `[0, ...)` 拼成一个连续的、每槽 8 字节的数组。于是：

- `va_list` 就是“指向下一个槽的指针”（`LowerVASTART` 把 `getVarArgsFrameIndex()` 的地址 store 进 alloca）；
- `va_arg` 是“load 指针 → load 值 → 指针 +8 → store 回去”（`LowerVAARG`）；
- 窄类型也占一整槽，因为约定已经把 `i8/i16/i32` 提升到 `i64`，调用者写的是整寄存器；`va_arg i32` 只用 `lw` 读低 32 位。

这套表示和 RISC-V 的 `RISCVMachineFunctionInfo`/`LowerVASTART` 几乎同构：RISC-V 也是把 `a0-a7` 中未使用的部分存到 `-VarArgsSaveSize`，再让 `va_list` 指向它。MiniRV 只是把 `RISCV::getArgGPRs(Subtarget)` 换成了固定的 8 个 `X10..X17`。

**为什么 varargs 强制有栈帧？** 因为 `IsVarArg` 会触发保存区，而保存区是一个有地址的 fixed object，必须有真实的栈空间；一个本来可以“无帧、只用寄存器”的叶子函数，只要声明成 variadic 就必须 `addi x2,x2,-N`。注意：**有帧 ≠ 有 frame pointer**。MiniRV 的 `sum` 有 64 字节帧，但没有 `sd x8`，也没有用 `x8`，因为 `hasFP` 只看 VLA/frameaddress/realign，不看 varargs。

## 主线 5：sret 与 byval

### sret

`sret` 在 IR 层是“第一个参数是一个指向返回对象的指针”。MiniRV 没有为它写任何特殊规则，所以它就是普通的 `ptr` 参数，落在 `x10`；第二个参数 `%x` 落在 `x11`：

```
define void @make_s(ptr sret(%S) %out, i64 %x)
   → x10 = %out, x11 = %x
   → sd x11, 0(x10)
```

这与 RISC-V lp64d 一致（psABI 也把 sret 指针放在 `a0`）。调用者只需保证对象地址跨过 `jal` 仍可用：`call_sret` 把它放进 `x8`（callee-saved）。

### byval

`byval` 的语义是“callee 拿到一份**副本**，修改不影响 caller”。LLVM 的通用 lowering 只在 target 显式处理时才会做拷贝；MiniRV 的 `LowerFormalArguments`/`LowerCall` 没有检查 `Flags.isByVal()`，于是 `byval` 被当作普通指针传递：

```
take_byval:  ld x10, 8(x10)       # 直接读 caller 的对象
call_byval:  addi x10, x2, 0      # 直接把 caller 的地址传过去
```

这与语义不符：如果 callee 写 `%s`，caller 会看到修改。RISC-V 的做法是在调用点 `CreateStackObject` + `memcpy` 出本地副本（`RISCVTargetLowering::LowerCall` 里的 “Create local copies for byval args”），再把副本地址传过去。MiniRV 测试集没有覆盖 byval 写入，所以这个简化没有被暴露；但你的 target 如果声称支持 byval，必须自己补这一步，或者明确拒绝 `byval` 参数。**“能编译”不等于“语义正确”**，这是本章最重要的边界。

## 主线 6：ABI 与 lowering 不一致会发生什么

把责任分清楚：

| 层 | 负责 | 不负责 |
|---|---|---|
| `CallingConv.td` | 位置、`LocVT`、`LocInfo` | 搬值、栈帧、varargs 保存区、byval 拷贝 |
| `LowerFormalArguments` | 把入口位置变成 DAG 值（live-in / fixed object load） | 决定位置 |
| `LowerCall` | 写栈参数、`CopyToReg`、`CALL`、取返回值 | 决定位置 |
| `LowerReturn` | `CopyToReg` + `RET` | 决定位置 |
| `CanLowerReturn` | 判断“能否不用 sret” | 生成代码 |

不一致的四种表现，从轻到重：

1. **td 分配了寄存器，lowering 忘了 copy**：值来自未定义的寄存器。机器验证器不会报错（live-in 是合法的），运行结果随机。
2. **lowering 写了别的寄存器**：返回值/参数错位。同样静默。
3. **td 给出 `LowerReturn` 不支持的 mem loc**：`assert(VA.isRegLoc())` 在 assertions 构建里直接 abort；release 构建会把内存偏移当寄存器用。
4. **`CanLowerReturn` 为 false**：不报错，SelectionDAG 自动改成 sret 隐藏指针。这是最隐蔽的一种，因为它“能跑”，只是 ABI 变了；和外部 C 代码链接时才炸。

所以验收不能只看“汇编长得像”。至少要跑三类 IR 级测试：9 个参数（栈边界）、i128（多部分返回）、varargs（保存区）。MiniRV 的 `stack-args.ll`、`i128.ll`、`varargs.ll` 就是这三类。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| ABI 声明 | `MiniRVCallingConv.td` | `CC_MiniRV`、`RetCC_MiniRV` | 8 个寄存器、栈槽、返回寄存器对 |
| 调用约定生成 | `MiniRVGenCallingConv.inc` | `CC_MiniRV` 的 `CCAssignFn` | `CCIfType`/`CCAssignToReg` 展开 |
| 入口 lowering | `MiniRVISelLowering.cpp` | `LowerFormalArguments` | live-in、fixed object、varargs 保存区 |
| 出口 lowering | `MiniRVISelLowering.cpp` | `LowerCall` | 栈参数先写、寄存器 copy、`RegisterMask`、tail call 限制 |
| 返回 lowering | `MiniRVISelLowering.cpp` | `LowerReturn`、`CanLowerReturn` | `RetCC_MiniRV`、sret demotion 入口 |
| varargs | `MiniRVISelLowering.cpp` | `LowerVASTART`、`LowerVAARG` | `va_list` = 槽指针 |
| 每函数信息 | `MiniRVMachineFunctionInfo.h` | `VarArgsFrameIndex` | 保存区首槽的 frame index |
| 库调用 | `MiniRVSubtarget.cpp` | `initLibcallLoweringInfo` | `__divti3` 等 i128 helper |
| RISC-V 参照 | `RISCVCallingConv.cpp`、`RISCVISelLowering.cpp` | `CC_RISCV`、`LowerCall` 的 byval local copies | lp64d 的完整规则 |

## 实测：从 `args9` 到 `call9`

环境：

```console
$ export TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
```

### 1. callee：前 8 个在 `x10..x17`，第 9 个在 `0(x2)`

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/abi.ll -o - | sed -n '/^args9:/,/jalr/p'
args9:                                  # @args9
	.cfi_startproc
# %bb.0:                                # %entry
	add	x3, x10, x11
	add	x3, x3, x12
	add	x3, x3, x13
	add	x3, x3, x14
	add	x3, x3, x15
	add	x3, x3, x16
	add	x3, x3, x17
	ld	x4, 0(x2)
	add	x10, x3, x4
	jalr	x0, 0(x1)
```

`ld x4,0(x2)` 就是第 9 个参数：`LowerFormalArguments` 为它建了一个 offset 0 的 fixed object。注意 callee 没有 prologue——它没有帧，也没有调用，`x2` 仍是入口值。

### 2. caller：先写栈参数，再填寄存器

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/abi.ll -o - | sed -n '/^call9:/,/jalr/p'
call9:                                  # @call9
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	.cfi_offset x1, -8
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
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

- `addi x2,x2,-16; sd x1,8(x2)`：caller 的帧，16 字节 = 8 字节 ra + 8 字节出参槽（对齐 16）。
- `addi x3,x0,9; sd x3,0(x2)`：第 9 个参数写进出参区。**先写栈，再填寄存器**，和 `LowerCall` 的顺序一致。
- `addi x11,x0,2 ... addi x17,x0,8`：`%x` 本来就在 `x10`，其余 7 个常数直接 materialize 到 `x11..x17`。
- `jal x1,args9`：`PseudoCALL` 展开。MiniRV 没有 `call` 伪指令，AsmPrinter 直接打印 `jal`。
- `ld x1,8(x2); addi x2,x2,16; jalr x0,0(x1)`：epilogue。

### 3. 同一份 IR 的 RISC-V lp64d

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/abi.ll -o - | sed -n '/^args9:/,/ret/p'
args9:                                  # @args9
	.cfi_startproc
# %bb.0:                                # %entry
	ld	t0, 0(sp)
	add	a0, a0, a1
	add	a2, a2, a3
	add	a4, a4, a5
	add	a0, a0, a2
	add	a4, a4, a6
	add	a0, a0, a4
	add	a0, a0, a7
	add	a0, a0, t0
	ret
```

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/abi.ll -o - | sed -n '/^call9:/,/ret/p' \
    | grep -v '\.cfi_'
call9:                                  # @call9
# %bb.0:                                # %entry
	addi	sp, sp, -16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	li	t0, 9
	li	a1, 2
	li	a2, 3
	li	a3, 4
	li	a4, 5
	li	a5, 6
	li	a6, 7
	li	a7, 8
	sd	t0, 0(sp)
	call	args9
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	addi	sp, sp, 16
	ret
```

对照点见表：`a0..a7` = `x10..x17`，第 9 个参数同样在 `0(sp)`；RISC-V 用 `ret`/`call` 伪指令，MiniRV 直接打印 `jalr x0,0(x1)`/`jal x1,...`。`call9` 里 RISC-V 先 `li` 再写栈，MiniRV 先写栈，这是调度差异。

### 4. MIR：`CCValAssign` 落到 `fixedStack`

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel /tmp/abi.ll -o - \
    | sed -n '/^name:            args9/,/^\.\.\./p' \
    | grep -E '^fixedStack:|offset: 0|%15:gpr = LD'
fixedStack:
  - { id: 0, type: default, offset: 0, size: 8, alignment: 16, stack-id: default, 
    %15:gpr = LD %fixed-stack.0, 0 :: (load (s64) from %fixed-stack.0, align 16)
```

`offset: 0, size: 8, alignment: 16` 就是第 9 个参数；`%15 = LD %fixed-stack.0` 是 `LowerFormalArguments` 生成的 load。

### 5. varargs：保存区 + `va_list`

```console
$ $B/llc -mtriple=minirv64 -O2 $TREE/llvm/test/CodeGen/MiniRV/varargs.ll -o - \
    | sed -n '/^sum:/,/jalr/p'
sum:                                    # @sum
# %bb.0:                                # %entry
	addi	x2, x2, -64
	addi	x4, x2, 0
	addi	x3, x2, 8
	sd	x3, 0(x4)
	sd	x11, 0(x3)
	sd	x12, 8(x3)
	sd	x13, 16(x3)
	sd	x14, 24(x3)
	sd	x15, 32(x3)
	sd	x16, 40(x3)
	sd	x17, 48(x3)
	addi	x3, x0, 0
.LBB0_1:                                # %loop
                                        # =>This Inner Loop Header: Depth=1
	ld	x7, 0(x4)
	addi	x11, x7, 8
	sd	x11, 0(x4)
	ld	x7, 0(x7)
	add	x3, x3, x7
	addi	x10, x10, -1
	bne	x10, x0, .LBB0_1
# %bb.2:                                # %exit
	addi	x10, x3, 0
	addi	x2, x2, 64
	jalr	x0, 0(x1)
```

`%n` 占 `x10`，所以 `x11..x17` 共 7 个寄存器被保存到 `x2+8 .. x2+56`；`va_list` 对象在 `x2+0`，`va_start` 把 `x2+8` 存进去。循环里 `ld x7,0(x4); addi x11,x7,8; sd x11,0(x4); ld x7,0(x7)` 就是 `va_arg` 的四步。

### 6. i128：`x10/x11` 返回

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch17-i128.ll -o - | sed -n '/^sub128:/,/jalr/p'
sub128:                                 # @sub128
	.cfi_startproc
# %bb.0:
	sub	x3, x11, x13
	sltu	x4, x10, x12
	sub	x11, x3, x4
	sub	x10, x10, x12
	jalr	x0, 0(x1)
```

`sub128(i128 %a, i128 %b)` 的参数是 `x10/x11` 和 `x12/x13`；`sub x11,x3,x4` 写高半，`sub x10,x10,x12` 写低半。`use128` 没有 sret 指针，直接 `jal`。

### 7. sret / byval

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch17-sret.ll -o - | sed -n '/^make_s:/,/jalr/p'
make_s:                                 # @make_s
	.cfi_startproc
# %bb.0:
	sd	x11, 0(x10)
	jalr	x0, 0(x1)
```

`sd x11,0(x10)` 说明 `sret` 指针就是第一个普通参数。调用者把对象地址放进 `x8` 跨过 `jal`：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch17-sret.ll -o - \
    | sed -n '/^call_sret:/,/jalr/p' \
    | grep -P 'addi\tx2|sd\tx1|sd\tx8|addi\tx11|addi\tx8|addi\tx10|jal\t|ld\tx10'
	addi	x2, x2, -48
	sd	x1, 40(x2)
	sd	x8, 32(x2)
	addi	x11, x10, 0
	addi	x8, x2, 8
	addi	x10, x8, 0
	jal	x1, make_s
	ld	x10, 0(x8)
	addi	x2, x2, 48
```

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch17-sret.ll -o - | sed -n '/^take_byval:/,/jalr/p'
take_byval:                             # @take_byval
	.cfi_startproc
# %bb.0:
	ld	x10, 8(x10)
	jalr	x0, 0(x1)
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch17-sret.ll -o - \
    | sed -n '/^call_byval:/,/jalr/p' \
    | grep -P 'addi\tx2|sd\tx1|addi\tx10|sd\tx3|jal\t|ld\tx1'
	addi	x2, x2, -32
	sd	x1, 24(x2)
	addi	x10, x2, 0
	sd	x3, 8(x10)
	jal	x1, take_byval
	ld	x1, 24(x2)
	addi	x2, x2, 32
```

`call_byval` 把 `x2+0` 直接传给 `take_byval`，没有 `memcpy`——这就是前面说的 byval 语义缺口。

## 反直觉与踩雷

1. **错误直觉**：`MiniRVCallingConv.td` 开头的注释说“MiniRV 没有 call 指令，所以 caller 根本生成不出来”，那 `LowerCall` 应该没用。
   **正确认识**：这条注释停留在 `751291d` 加入 call 支持之前的状态；当前 `LowerCall` 会生成 `PseudoCALL`/`PseudoCALLReg`，实测 `call9` 里有 `jal x1,args9`。读 target 代码时以当前 lowering 和测试为准，不要相信过期注释。
2. **错误直觉**：`i128` 是一个参数，应该占一个“128 位寄存器”或至少一次分配。
   **正确认识**：类型合法化先把它拆成两个 `i64` 部分，调用约定看到的是两个独立的 `CCValAssign`；`RetCC_MiniRV` 少写一个寄存器就会触发 sret demotion（`a61c0cb` 之前的行为）。
3. **错误直觉**：varargs 一定要 frame pointer，因为 `va_list` 要访问帧。
   **正确认识**：varargs 只需要**栈帧**来放寄存器保存区；`hasFP` 不看 varargs。MiniRV 的 `sum` 有 64 字节帧但没有 `x8`。只有 VLA、`llvm.frameaddress`、stack realignment 才强制 `x8`。
4. **错误直觉**：`byval` 是 IR 属性，LLVM 会自动拷贝。
   **正确认识**：覆写 `LowerCall` 后，byval 的拷贝不会自动发生；MiniRV 把 `byval` 当普通指针（`call_byval` 直接把 `x2+0` 传过去），callee 的写入会穿透到 caller（我们另写了一个 callee 修改 byval 参数的用例验证过）。RISC-V 才会在调用点 `memcpy`。如果你的 target 要实现 byval，必须自己写；否则应当拒绝。

## 进阶：再往深一层

- **`CCValAssign::LocInfo` 被 MiniRV 忽略了一部分。** `LowerCall` 对窄类型统一 `SIGN_EXTEND`，不区分 `SExt/ZExt/AExt`；`LowerFormalArguments` 对寄存器参数统一 `TRUNCATE`。这在“callee 只使用低位”的前提下自洽，但严格的 psABI 会要求按 signedness 扩展。RISC-V 的 `CC_RISCV` 用 `LocInfo` 选择扩展方式。你的 target 如果和外部 ABI 对接，应该补上。
- **`MaxCallFrameSize` 与 reserved call frame。** MiniRV 的 `hasReservedCallFrame` 在无 VLA 时返回 true，出参区在 prologue 一次性预留；MIR 里的 `ADJCALLSTACKDOWN 8` 在 PEI 阶段被 `eliminateCallFramePseudoInstr` 消掉，不会在调用点出现 `addi x2,x2,-8`。有 VLA 时则相反（Ch 18 的 `vla_call`）。

## 本章重点整理

- `CallingConv.td` 只分配位置；`CCState` 把规则套到每个值部分，产出 `CCValAssign`；lowering 负责搬值。
- i128 在 DAG 里是两个 `i64` 部分，返回需要 `x10/x11` 两个寄存器；`a61c0cb` 修返回，`10b7b4a` 修除法 libcall。
- varargs 把剩余参数寄存器存到入口 SP 下方，与入口栈参数拼成连续槽数组，`va_list` 就是槽指针；varargs 强制栈帧但不强制 frame pointer。
- sret 是普通指针参数；byval 在 MiniRV 里没有拷贝，是已知的语义缺口。
- ABI 与 lowering 不一致时，最危险的不是断言，而是静默 sret demotion 和“能跑但 ABI 变了”。

## 自我检核

- [ ] 不看源码，画出 `args9` 的寄存器/栈布局，并说明第 9 个参数在 callee 有帧和无帧时分别怎么寻址。
- [ ] 解释 `CanLowerReturn` 返回 false 后，`SelectionDAGBuilder::visitRet` 做了什么；为什么这不会打印诊断。
- [ ] 说出 `a61c0cb` 和 `10b7b4a` 各自修了什么，为什么必须成对。

## 延伸阅读

- **[LLVM Code Generator — Implementing a Calling Convention](https://llvm.org/docs/CodeGenerator.html#implementing-a-calling-convention)** — LLVM Project
  - **读哪里**：`CallingConv.td` 的 `CCIfType`/`CCAssignToReg`/`CCAssignToStack` 语义，以及 `LowerFormalArguments`/`LowerCall`/`LowerReturn` 的职责划分。
  - **能学到什么**：为什么 td 只描述位置，而搬值必须在 C++ 里做；`CCState` 的四个分析入口。
  - **前提**：先读 Ch 10 和 Ch 13（SelectionDAG 基本节点）。
- **[LLVM Language Reference — call instruction](https://llvm.org/docs/LangRef.html#call-instruction)** — LLVM Project
  - **读哪里**：`sret`、`byval`、`byref`、`signext`/`zeroext` 参数属性，以及 calling convention 的语义。
  - **能学到什么**：IR 层的 ABI 契约；为什么 `byval` 要求副本，而 `sret` 只是指针。
  - **前提**：知道 LLVM IR 的基本类型和函数定义。
- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：整数调用约定、`a0-a7`、栈参数对齐、2×XLEN 规则、variadic 的寄存器保存。
  - **能学到什么**：MiniRV 的 `CC_MiniRV` 是这套规则的“最小子集”；也能解释为什么 lp64d 还有 `fa0-fa7`。
  - **前提**：RV64 基础；ISA 细节看 `../riscv_backend/README.md`。

→ [Ch 18 栈帧进阶](./18-stack-frames-advanced.md)
