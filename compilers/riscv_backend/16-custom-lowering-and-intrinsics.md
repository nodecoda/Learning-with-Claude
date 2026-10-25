# 第 16 章 自定义 lowering 与 intrinsic

有些操作无法用一条 IR 指令表达，或者表达了也不该由通用合法化器处理。RISC-V 后端用两条路接住它们：一是 `RISCVTargetLowering` 里的自定义 lowering（`LowerOperation`、`ReplaceNodeResults`），二是 `llvm.riscv.*` intrinsic 加上 TableGen pattern。前者把 `ISD` 节点改写成目标自己的 `RISCVISD::` 节点，后者把 intrinsic 直接匹配成伪指令。

这一章用三个真实例子把链路走完：

- `vfirst`：从 `llvm.riscv.vfirst.i64.nxv4i1` 到 `PseudoVFIRST_M_B16`，再到 `vsetvli` + `vfirst.m`。
- `sha256sig0`：正确声明时，没有 `Zknh` 会报 `Cannot select: ... RISCVISD::SHA256SIG0`，有 `Zknh` 时是一条 `sha256sig0 a0, a0`。
- 新增 intrinsic 的 TableGen 验证：一个 `llvm.riscv.demo.absdiff` 的枚举生成，以及缺少 `TargetPrefix` 时的真实报错。

最后给一个 guided 的“新增 intrinsic + lowering + pattern”流程。它需要重新编译 llc，本机不能执行，所以只给 diff 思路、tblgen 命令和 lit 写法。

## 为什么

先看 `vfirst` 的最终汇编：

```asm
vfirst:
	vsetvli	zero, a0, e8, mf2, ta, ma
	vfirst.m	a0, v0
	ret
```

IR 里只有一句：

```llvm
%r = call i64 @llvm.riscv.vfirst.i64.nxv4i1(<vscale x 4 x i1> %m, i64 %vl)
```

中间发生了什么？`vfirst` 的返回类型是标量 `i64`，输入是 mask 向量和 VL。RISC-V 的 `vfirst.m` 只接受 mask 寄存器 `v0` 和标量目标寄存器，VL 由 `vsetvli` 提供。这条链要同时处理：

- intrinsic 的 overloaded 类型（`i64` 和 `nxv4i1` 的匹配）；
- mask 必须落在 `v0`；
- VL 是第二个操作数，不是普通的向量长度；
- `vsetvli` 是后插入的，选指阶段只有 `PseudoVFIRST_M_B16`。

这些信息不可能靠一条通用的 `Pat` 表达完，所以 RISC-V 用了“intrinsic 定义 + TableGen pattern + 伪指令 + 后插入 vsetvli”的组合。

`sha256sig0` 更直接。它没有向量寄存器，也没有 VL，理论上一条 `PatGpr` 就够。但前提是 intrinsic 名字正确、扩展开启、lowering 把它变成 `RISCVISD::SHA256SIG0`。三者缺一，结果完全不同：名字错了会变成普通外部调用，扩展没开会报 `Cannot select`，lowering 没写会报 `Unimplemented ... LowerOperation Case`。

这就是本章要解决的问题：知道一个 `llvm.riscv.*` 调用在哪个文件里被接住，失败时该看哪一层。

## 主线

### 16.1 两条自定义路径：lowering 与 intrinsic

`RISCVTargetLowering::LowerOperation` 是 SelectionDAG 的“自定义动作”入口。当某个节点被 `setOperationAction(..., Custom)` 标记，合法化器不会展开它，而是调用 `LowerOperation`：

```cpp
SDValue RISCVTargetLowering::LowerOperation(SDValue Op,
                                            SelectionDAG &DAG) const {
  switch (Op.getOpcode()) {
  default:
    reportFatalInternalError(
        "Unimplemented RISCVTargetLowering::LowerOperation Case");
  case ISD::PREFETCH:
    return LowerPREFETCH(Op, Subtarget, DAG);
  case ISD::GlobalAddress:
    return lowerGlobalAddress(Op, DAG);
  ...
  case ISD::INTRINSIC_WO_CHAIN:
    return LowerINTRINSIC_WO_CHAIN(Op, DAG);
  case ISD::INTRINSIC_W_CHAIN:
    return LowerINTRINSIC_W_CHAIN(Op, DAG);
  case ISD::INTRINSIC_VOID:
    return LowerINTRINSIC_VOID(Op, DAG);
  }
}
```

`LowerOperation` 的返回值仍然是一个 DAG 节点，通常是 `RISCVISD::` 自定义节点。它不直接生成机器指令，而是把“目标相关的语义”编码进 DAG，等选指阶段再匹配。

`ReplaceNodeResults` 处理的是另一种情况：结果类型本身不合法。典型例子是 RV64 上的 `FP_TO_SINT i32`：

```cpp
void RISCVTargetLowering::ReplaceNodeResults(SDNode *N,
                                             SmallVectorImpl<SDValue> &Results,
                                             SelectionDAG &DAG) const {
  ...
  case ISD::FP_TO_SINT:
  case ISD::FP_TO_UINT: {
    assert(N->getValueType(0) == MVT::i32 && Subtarget.is64Bit() &&
           "Unexpected custom legalisation");
    ...
    unsigned Opc = IsSigned ? RISCVISD::FCVT_W_RV64 : RISCVISD::FCVT_WU_RV64;
    SDValue Res = DAG.getNode(Opc, DL, MVT::i64, Op0,
                              DAG.getTargetConstant(RISCVFPRndMode::RTZ, DL,
                                                    MVT::i64));
    Results.push_back(DAG.getNode(ISD::TRUNCATE, DL, MVT::i32, Res));
    return;
  }
  }
}
```

这里 `FP_TO_SINT` 的结果是 `i32`，但 RISC-V 的 `fcvt.w.s` 在 RV64 上产生 64 位结果。`ReplaceNodeResults` 生成一个 `RISCVISD::FCVT_W_RV64`（结果 `i64`），再 `TRUNCATE` 到 `i32`，把两个结果都放进 `Results`。如果浮点类型需要软化，代码会走 `RTLIB::getFPTOSINT` 的 libcall 分支。这个函数和 `LowerOperation` 的分工是：

- `LowerOperation`：结果类型合法，但操作没有合法指令。
- `ReplaceNodeResults`：结果类型不合法，需要改变结果的数量或类型。

两者都可能生成 `RISCVISD::` 节点，区别在触发时机。

### 16.2 `RISCVISD::` 自定义节点

`RISCVISD::` 节点在 TableGen 里定义。`RISCVInstrInfo.td` 顶部有一个 `RVSDNode`：

```tablegen
class RVSDNode<string opcode, SDTypeProfile type_profile,
               list<SDNodeProperty> properties = []>
    : SDNode<"RISCVISD::" # opcode, type_profile, properties> {
}
```

具体节点散落在各个 `.td` 里。例如 `SHA256SIG0`：

```tablegen
def riscv_sha256sig0 : RVSDNode<"SHA256SIG0", SDTIntUnaryOp>;
```

`riscv_vfirst_vl` 在 `RISCVInstrInfoVVLPatterns.td`：

```tablegen
def riscv_vfirst_vl : RVSDNode<"VFIRST_VL",
                               SDTypeProfile<1, 3, [SDTCisVT<0, XLenVT>,
                                                   SDTCisVec<1>, SDTCisInt<1>,
                                                   SDTCVecEltisVT<2, i1>,
                                                   SDTCisSameNumEltsAs<1, 2>,
                                                   SDTCisVT<3, XLenVT>]>>;
```

`RVSDNode` 的第一个参数是 C++ 枚举名，第二个是类型约束。生成后，C++ 里可以直接写 `RISCVISD::SHA256SIG0`、`RISCVISD::VFIRST_VL`。这些节点不是 IR 指令，也不会出现在 `.ll` 文件里；它们只存在于 SelectionDAG 的中间表示。

自定义节点有三个好处：

- 把多个 IR 操作合并成一个语义单元，例如 `vfirst` 的 mask + VL + 结果类型。
- 把目标约束（哪个寄存器、哪个立即数范围）留到选指阶段处理。
- 让 DAGCombine 能识别“这是一个整体”，而不是零散的 `load`/`and`/`icmp`。

代价是：每增加一个 `RISCVISD::` 节点，就要同时增加 lowering、pattern、可能的 DAGCombine 和测试。只加枚举不加 pattern，最后会在选指阶段报 `Cannot select`。

### 16.3 `vfirst` 的完整链路

先看 intrinsic 定义。`llvm/include/llvm/IR/IntrinsicsRISCV.td` 里有一个 `RISCVMaskedUnarySOutUnMasked`：

```tablegen
class RISCVMaskedUnarySOutUnMasked
      : DefaultAttrsIntrinsic<[LLVMMatchType<1>],
                  [llvm_anyvector_ty, llvm_anyint_ty],
                  [IntrNoMem]>, RISCVVIntrinsic {
  let VLOperand = 1;
}
```

逐项读：

- `[LLVMMatchType<1>]`：返回值类型和第二个参数（索引 1）相同。第二个参数是 `llvm_anyint_ty`，所以 `vfirst` 的返回类型会跟着 VL 的类型走：VL 是 `i64`，返回就是 `i64`。
- `[llvm_anyvector_ty, llvm_anyint_ty]`：输入是一个任意向量和一个任意整数。对 `vfirst` 来说，向量是 mask 类型 `nxv4i1`。
- `VLOperand = 1`：第二个参数是 VL，后端插入 `vsetvli` 时要读它。

`multiclass RISCVMaskedUnarySOut` 生成两个名字：

```tablegen
multiclass RISCVMaskedUnarySOut {
  def "int_riscv_" # NAME : RISCVMaskedUnarySOutUnMasked;
  def "int_riscv_" # NAME # "_mask" : RISCVMaskedUnarySOutMasked;
}
```

所以：

```tablegen
defm vfirst : RISCVMaskedUnarySOut;
```

会生成 `int_riscv_vfirst` 和 `int_riscv_vfirst_mask`。生成的头文件 `IntrinsicsRISCV.h` 里有：

```text
riscv_vfirst,       // llvm.riscv.vfirst (IntrinsicsRISCV.td:1325)
riscv_vfirst_mask,  // llvm.riscv.vfirst.mask (IntrinsicsRISCV.td:1326)
```

IR 里的名字 `llvm.riscv.vfirst.i64.nxv4i1` 是重载后的形式：`.i64` 对应返回值/VL，`.nxv4i1` 对应 mask 向量。名字不是手写的，是 TableGen 按类型约束生成的。

然后看匹配。`RISCVInstrInfoVPseudos.td` 里有：

```tablegen
defm : VPatUnaryS_M<"int_riscv_vfirst", "PseudoVFIRST">;
```

`VPatUnaryS_M` 对每个 mask 类型生成一条 pattern：

```tablegen
multiclass VPatUnaryS_M<string intrinsic_name, string inst> {
  foreach mti = AllMasks in {
    def : Pat<(XLenVT (!cast<Intrinsic>(intrinsic_name)
                       (mti.Mask VR:$rs1), VLOpFrag)),
              (!cast<Instruction>(inst#"_M_"#mti.BX) $rs1,
               GPR:$vl, mti.Log2SEW)>;
    def : Pat<(XLenVT (!cast<Intrinsic>(intrinsic_name # "_mask")
                       (mti.Mask VR:$rs1), (mti.Mask VMV0:$vm), VLOpFrag)),
              (!cast<Instruction>(inst#"_M_"#mti.BX#"_MASK") $rs1,
               (mti.Mask VMV0:$vm), GPR:$vl, mti.Log2SEW)>;
  }
}
```

`AllMasks` 包含不同的 mask 元素宽度，`mti.BX` 决定伪指令后缀，`mti.Log2SEW` 是 `vsetvli` 的 SEW 编码。对 `nxv4i1`，匹配到的是 `PseudoVFIRST_M_B16`（`B16` 是 `AllMasks` 里对应这个 mask 布局的后缀）。

选指后的 MIR：

```text
%1:gprnox0 = COPY $x10
%0:vr = COPY $v0
%2:gpr = PseudoVFIRST_M_B16 %0:vr, %1:gprnox0, 0
$x10 = COPY %2:gpr
PseudoRET implicit $x10
```

三个操作数是 `rs1`、`vl`、`log2sew`。`%1:gprnox0` 表示 VL 不能是 `x0`（`vfirst` 的 VL 为 0 时结果有特殊语义）。`0` 是 `log2(8)=0`，对应 `e8`。

`vsetvli` 不在选指阶段生成。`riscv-insert-vsetvli` pass 在后面根据 `PseudoVFIRST_M_B16` 和 VL 插入：

```asm
vsetvli	zero, a0, e8, mf2, ta, ma
vfirst.m	a0, v0
```

`mf2` 是 LMUL 的选择，由 mask 类型和 VLEN 约束决定。这一层不在本章展开，第 18 章会讲调度和 `vsetvli` 的插入。

失败路径：没有 `+v` 时，`nxv4i1` 不是合法类型，llc 报：

```text
LLVM ERROR: Don't know how to legalize this scalable vector type
```

退出码是 `-1073741795`。这不是 intrinsic 的问题，而是 mask 向量类型没有合法表示。把 `-mattr` 改成 `+v` 后就能走到 pattern。

### 16.4 `sha256sig0`：正确名字、扩展与 `Cannot select`

`sha256sig0` 的定义很短：

```tablegen
class ScalarCryptoGprIntrinsic32
  : DefaultAttrsIntrinsic<[llvm_i32_ty],
                          [llvm_i32_ty],
                          [IntrNoMem, IntrSpeculatable]>;

def int_riscv_sha256sig0 : ScalarCryptoGprIntrinsic32;
```

它没有重载，所以 IR 里的正确名字是 `llvm.riscv.sha256sig0`，**不带** `.i32` 后缀。写错名字的后果很隐蔽：

```llvm
declare i32 @llvm.riscv.sha256sig0.i32(i32)
define i32 @sha(i32 %x) {
  %r = call i32 @llvm.riscv.sha256sig0.i32(i32 %x)
  ret i32 %r
}
```

llc 不报错，输出是：

```asm
sha:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	call	llvm.riscv.sha256sig0.i32
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

它被当成普通外部函数调用了。原因是没有匹配到已知 intrinsic，LLVM 把它保留成普通 `call`；这既不会触发 lowering，也不会触发 pattern。这个例子说明：intrinsic 名字的后缀不是装饰，写错会静默退化。

正确名字、没有 `Zknh`：

```llvm
declare i32 @llvm.riscv.sha256sig0(i32)
```

输出是 fatal error：

```text
LLVM ERROR: Cannot select: ...: i64 = RISCVISD::SHA256SIG0 ...
```

`RISCVISD::SHA256SIG0` 已经由 `LowerINTRINSIC_WO_CHAIN` 生成：

```cpp
case Intrinsic::riscv_sha256sig0:
case Intrinsic::riscv_sha256sig1:
case Intrinsic::riscv_sha256sum0:
case Intrinsic::riscv_sha256sum1: {
  unsigned Opc;
  switch (IntNo) {
  case Intrinsic::riscv_sha256sig0: Opc = RISCVISD::SHA256SIG0; break;
  ...
  }
  return DAG.getNode(Opc, DL, XLenVT, Op.getOperand(1));
}
```

但 `RISCVInstrInfoZk.td` 里的 pattern 带扩展 predicate：

```tablegen
let Predicates = [HasStdExtZknh] in {
def SHA256SIG0 : Unary_r<0b000100000010, 0b001, "sha256sig0">;
def : PatGpr<riscv_sha256sig0, SHA256SIG0>;
}
```

没有 `Zknh` 时 predicate 不成立，节点没有可选的指令，于是 `Cannot select`。开 `+zknh` 后：

```asm
sha:
	sha256sig0	a0, a0
	ret
```

这条链的诊断顺序很清晰：先看 intrinsic 名字，再看扩展，再看 pattern。三者都对，才有一条指令。

### 16.5 `setOperationAction` 与 `setTargetDAGCombine`

`LowerOperation` 不是自动调用的。一个节点要先被标成 `Custom`：

```cpp
setOperationAction(ISD::BRCOND, MVT::Other, Custom);
setOperationAction(ISD::SELECT, XLenVT, Custom);
setOperationAction(ISD::DYNAMIC_STACKALLOC, XLenVT, Custom);
```

`setOperationAction(Op, VT, Custom)` 告诉合法化器：“这个 opcode 在 VT 上不要展开，交给目标。”合法化器随后调用 `LowerOperation`。如果没有这条标记，节点会被通用合法化器处理，可能展开、可能 libcall，也可能直接留给选指。

`setTargetDAGCombine` 是另一张表：

```cpp
setTargetDAGCombine({ISD::INTRINSIC_VOID, ISD::INTRINSIC_W_CHAIN,
                     ISD::INTRINSIC_WO_CHAIN, ISD::ADD, ISD::SUB, ISD::MUL,
                     ISD::AND, ISD::OR, ISD::XOR, ISD::SETCC, ISD::SELECT,
                     ISD::SRA});
```

它告诉 DAGCombiner：“这些 opcode 变化时，调用 `RISCVTargetLowering::PerformDAGCombine`。”`PerformDAGCombine` 里对 `vfirst` 的处理是一个好例子：

```cpp
case Intrinsic::riscv_vcpop:
case Intrinsic::riscv_vcpop_mask:
case Intrinsic::riscv_vfirst:
case Intrinsic::riscv_vfirst_mask: {
  SDValue VL = N->getOperand(2);
  if (IntNo == Intrinsic::riscv_vcpop_mask ||
      IntNo == Intrinsic::riscv_vfirst_mask)
    VL = N->getOperand(3);
  if (!isNullConstant(VL))
    return SDValue();
  // If VL is 0, vcpop -> li 0, vfirst -> li -1.
  SDLoc DL(N);
  EVT VT = N->getValueType(0);
  if (IntNo == Intrinsic::riscv_vfirst ||
      IntNo == Intrinsic::riscv_vfirst_mask)
    return DAG.getAllOnesConstant(DL, VT);
  return DAG.getConstant(0, DL, VT);
}
```

当 VL 是常量 0 时，`vfirst` 的结果一定是 `-1`，不需要生成向量指令，直接返回全 1 常量。这个 combine 只有在 `ISD::INTRINSIC_WO_CHAIN` 被 `setTargetDAGCombine` 注册后才会被调用。

`setOperationAction` 和 `setTargetDAGCombine` 的区别可以这样记：

- `setOperationAction` 决定“合法化时怎么处理”，对象是 opcode + 类型。
- `setTargetDAGCombine` 决定“DAGCombine 时是否叫目标”，对象是 opcode。
- `LowerOperation` 是 `Custom` 动作的落地函数；`PerformDAGCombine` 是 combine 动作的落地函数。

### 16.6 为什么多数情况下先找现有 intrinsic

在新增 `llvm.riscv.*` 之前，先搜三处：

1. `llvm/include/llvm/IR/IntrinsicsRISCV.td`：看有没有语义相同或相近的定义。`vfirst`、`vcpop`、`vmsbf` 都在同一个 `RISCVMaskedUnarySOut`/`RISCVMaskedUnaryMOut` 家族里。
2. `llvm/lib/Target/RISCV/RISCVInstrInfo*.td`：看有没有 `Pat` 或 `VPat*` 已经把这个 intrinsic 接到伪指令。
3. `llvm/test/CodeGen/RISCV/`：看有没有同名或同模式的 lit 测试；测试里通常包含 intrinsic 的完整声明和 `CHECK` 行。

先找现有的理由很实际：

- 现有 intrinsic 已经处理了 overload 类型、mask、VL、立即数约束（`ImmArg`）和内存属性（`IntrNoMem`）。自己新写一个，容易漏掉其中一项。
- 现有 intrinsic 通常已经有 Clang builtin、成本模型和测试。新增一个要同步改 Clang、成本模型和测试，否则只能手写 IR 调用。
- intrinsic 名字和编号进入 bitcode/ABI。新增或改名会影响跨版本 bitcode；已有名字更稳。
- 新 intrinsic 需要重新生成 `IntrinsicsRISCV.h`、`RISCVGenInstrInfo.inc`、`RISCVGenDAGISel.inc` 并重新编译 llc。没有构建环境时，只能写 guided 方案。

如果现有 intrinsic 的语义只差一点，优先考虑在 lowering 里组合现有节点，而不是新增一个名字。`vfirst` 的 VL=0 优化就是在 `PerformDAGCombine` 里做的，没有新增 intrinsic。

## 可跑实验

统一入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\Learning-with-Claude\compilers\riscv_backend\code\tools\run.ps1' 'labs\16-custom-lowering-intrinsics.ps1'
```

脚本会生成 `vfirst.ll`、`DemoIntrinsics.td`，输出 `vfirst-print.txt`、`vfirst.s`、`DemoEnums.inc`。下面是拆开的命令。

### 实验 1：`vfirst` 的 intrinsic → 伪指令 → 汇编

```powershell
$LLVM = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
$w = Join-Path $env:TEMP 'riscv-course-lab16'
New-Item -ItemType Directory -Force -Path $w | Out-Null
Set-Location $w

@"
target triple = "riscv64-unknown-elf"
declare i64 @llvm.riscv.vfirst.i64.nxv4i1(<vscale x 4 x i1>, i64)
define i64 @vfirst(<vscale x 4 x i1> %m, i64 %vl) {
  %r = call i64 @llvm.riscv.vfirst.i64.nxv4i1(<vscale x 4 x i1> %m, i64 %vl)
  ret i64 %r
}
"@ | Set-Content -Encoding ascii vfirst.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+v' -O2 `
  -print-after=riscv-isel vfirst.ll -o vfirst.s 2> vfirst-print.txt

Get-Content vfirst-print.txt | Select-String 'Machine code for function vfirst' -Context 0,10
Get-Content vfirst.s | Select-String '^vfirst:' -Context 0,8
```

预期 MIR：

```text
%1:gprnox0 = COPY $x10
%0:vr = COPY $v0
%2:gpr = PseudoVFIRST_M_B16 %0:vr, %1:gprnox0, 0
$x10 = COPY %2:gpr
PseudoRET implicit $x10
```

预期汇编：

```asm
vsetvli	zero, a0, e8, mf2, ta, ma
vfirst.m	a0, v0
ret
```

边界：把 `-mattr=+v` 改成 `-mattr=`，会得到 `Don't know how to legalize this scalable vector type`，退出码 `-1073741795`。

### 实验 2：`sha256sig0` 的名字与扩展

先验证正确名字、没有 `Zknh`：

```powershell
@"
target triple = "riscv64-unknown-elf"
declare i32 @llvm.riscv.sha256sig0(i32)
define i32 @sha(i32 %x) {
  %r = call i32 @llvm.riscv.sha256sig0(i32 %x)
  ret i32 %r
}
"@ | Set-Content -Encoding ascii sha.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 sha.ll -o sha-nok.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+zknh' -O2 sha.ll -o sha-zknh.s
Get-Content sha-zknh.s | Select-String '^sha:' -Context 0,5
```

预期：`-mattr=+m` 的那条命令会失败，不生成 `sha-nok.s`，错误形如：

```text
LLVM ERROR: Cannot select: ...: i64 = RISCVISD::SHA256SIG0 ...
```

`sha-zknh.s` 是：

```asm
sha:
	sha256sig0	a0, a0
	ret
```

再验证错误名字的静默退化：

```powershell
@"
target triple = "riscv64-unknown-elf"
declare i32 @llvm.riscv.sha256sig0.i32(i32)
define i32 @sha(i32 %x) {
  %r = call i32 @llvm.riscv.sha256sig0.i32(i32 %x)
  ret i32 %r
}
"@ | Set-Content -Encoding ascii sha-wrong.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+zknh' -O2 sha-wrong.ll -o sha-wrong.s
Get-Content sha-wrong.s | Select-String '^sha:' -Context 0,8
```

预期：汇编里是 `call llvm.riscv.sha256sig0.i32`，没有 `sha256sig0` 指令。这个名字没有被识别为 intrinsic，所以既不 lowering 也不选指。

### 实验 3：TableGen 新增 intrinsic 枚举

```powershell
@"
include "llvm/IR/Intrinsics.td"

let TargetPrefix = "riscv" in {
  def int_riscv_demo_absdiff : DefaultAttrsIntrinsic<[llvm_i64_ty],
                                                     [llvm_i64_ty, llvm_i64_ty],
                                                     [IntrNoMem, IntrSpeculatable]>;
}
"@ | Set-Content -Encoding ascii DemoIntrinsics.td

$inc = Join-Path (Split-Path -Parent $LLVM) 'include'
& "$LLVM\llvm-tblgen.exe" -I $inc -gen-intrinsic-enums `
  --intrinsic-prefix=riscv DemoIntrinsics.td -o DemoEnums.inc
Get-Content DemoEnums.inc | Select-String 'demo_absdiff'
```

预期：

```text
riscv_demo_absdiff,                        // llvm.riscv.demo.absdiff (DemoIntrinsics.td:4)
```

边界：去掉 `let TargetPrefix = "riscv" in`：

```powershell
@"
include "llvm/IR/Intrinsics.td"

def int_riscv_demo_absdiff : DefaultAttrsIntrinsic<[llvm_i64_ty],
                                                   [llvm_i64_ty, llvm_i64_ty],
                                                   [IntrNoMem, IntrSpeculatable]>;
"@ | Set-Content -Encoding ascii DemoBad.td

& "$LLVM\llvm-tblgen.exe" -I $inc -gen-intrinsic-enums `
  --intrinsic-prefix=riscv DemoBad.td -o DemoBad.inc
```

预期退出码 1，错误信息包含：

```text
target independent intrinsic 'llvm.riscv.demo.absdiff' has prefix 'llvm.riscv'
that conflicts with intrinsics for target 'riscv'
```

这说明 `TargetPrefix` 不是可选的：它告诉 TableGen 这个 intrinsic 属于 riscv 目标，名字前缀不能和目标前缀冲突。

### 实验 4（guided）：新增 intrinsic + lowering + pattern

本机不能重新编译 llc，所以下面是 guided 流程。假设新增一条并不存在的 `demo.absdiff` 指令，只用于演示文件之间的关系。

1. 在 `llvm/include/llvm/IR/IntrinsicsRISCV.td` 增加：

```tablegen
let TargetPrefix = "riscv" in {
  def int_riscv_demo_absdiff : DefaultAttrsIntrinsic<[llvm_i64_ty],
                                                     [llvm_i64_ty, llvm_i64_ty],
                                                     [IntrNoMem, IntrSpeculatable]>;
}
```

2. 在 `RISCVInstrInfo.td` 或新建的 `RISCVInstrInfoDemo.td` 增加 DAG 节点：

```tablegen
def riscv_demo_absdiff : RVSDNode<"DEMO_ABSDIFF", SDTIntBinOp>;
```

3. 在 `RISCVISelLowering.cpp` 的 `LowerINTRINSIC_WO_CHAIN` 增加：

```cpp
case Intrinsic::riscv_demo_absdiff:
  return DAG.getNode(RISCVISD::DEMO_ABSDIFF, DL, XLenVT,
                     Op.getOperand(1), Op.getOperand(2));
```

4. 在指令定义文件里增加一条（假设的）机器指令和 pattern：

```tablegen
def DEMO_ABSDIFF : ALU_rr<0b0000000, 0b000, "demo.absdiff">;
def : PatGprGpr<riscv_demo_absdiff, DEMO_ABSDIFF>;
```

真实工作里要补上编码字段、predicate、调度信息和 `hasSideEffects`；这里的字段只是占位。

5. 重新生成并编译：

```powershell
$td = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-riscv-td\llvm\lib\Target\RISCV'
New-Item -ItemType Directory -Force -Path out | Out-Null
& "$LLVM\llvm-tblgen.exe" -I $inc -I $td -gen-instr-info $td/RISCV.td -o out/RISCVGenInstrInfo.inc
& "$LLVM\llvm-tblgen.exe" -I $inc -I $td -gen-dag-isel  $td/RISCV.td -o out/RISCVGenDAGISel.inc
cmake --build build --target llc
```

6. 写 lit 测试 `llvm/test/CodeGen/RISCV/demo-absdiff.ll`：

```llvm
; RUN: llc -mtriple=riscv64 -mattr=+m < %s | FileCheck %s
; CHECK-LABEL: demo_absdiff:
; CHECK:       demo.absdiff a0, a0, a1
; CHECK-NEXT:  ret

declare i64 @llvm.riscv.demo.absdiff(i64, i64)

define i64 @demo_absdiff(i64 %a, i64 %b) {
  %r = call i64 @llvm.riscv.demo.absdiff(i64 %a, i64 %b)
  ret i64 %r
}
```

跑法：

```powershell
llvm-lit -v llvm/test/CodeGen/RISCV/demo-absdiff.ll
```

本机没有 `llvm-lit`，也缺少构建产物，所以这一步只能标 guided。验证顺序建议是：先 `llvm-tblgen -gen-intrinsic-enums` 确认名字和枚举，再 `-gen-instr-info` 确认 opcode，再 `-gen-dag-isel` 确认 pattern，最后才编译 llc 跑 lit。任何一步失败，都不要继续往下猜。

## 源码落点

- `llvm/include/llvm/IR/IntrinsicsRISCV.td`
  - `class RISCVVIntrinsic`：`VLOperand` 字段，默认 `NoVLOperand`。
  - `class RISCVMaskedUnarySOutUnMasked`：`[LLVMMatchType<1>]` + `[llvm_anyvector_ty, llvm_anyint_ty]`，`VLOperand = 1`。
  - `multiclass RISCVMaskedUnarySOut`：生成 `int_riscv_<name>` 和 `int_riscv_<name>_mask`。
  - `defm vfirst : RISCVMaskedUnarySOut;`：`vfirst` 的名字来源。
  - `class ScalarCryptoGprIntrinsic32` 与 `def int_riscv_sha256sig0`：无重载 intrinsic 的例子。
- `llvm/include/llvm/IR/IntrinsicsRISCV.h`（生成物）
  - `riscv_vfirst`、`riscv_vfirst_mask` 的枚举和注释里的 `.td` 行号。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td`
  - `PseudoVFIRST` 的伪指令定义。
  - `defm : VPatUnaryS_M<"int_riscv_vfirst", "PseudoVFIRST">;`。
  - `multiclass VPatUnaryS_M`：intrinsic 到 `PseudoVFIRST_M_*` 的 pattern。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVVLPatterns.td`
  - `riscv_vfirst_vl` 的 `RVSDNode` 定义。
  - 带 mask 的 `PseudoVFIRST_M_*_MASK` pattern。
- `llvm/lib/Target/RISCV/RISCVInstrInfoZk.td`
  - `riscv_sha256sig0` 的 `RVSDNode`。
  - `SHA256SIG0` 指令定义。
  - `def : PatGpr<riscv_sha256sig0, SHA256SIG0>;`。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
  - `RISCVTargetLowering::LowerOperation`：`Custom` 动作的总入口。
  - `LowerINTRINSIC_WO_CHAIN`、`LowerINTRINSIC_W_CHAIN`、`LowerINTRINSIC_VOID`：intrinsic 的三类 lowering。
  - `riscv_sha256sig0` 到 `RISCVISD::SHA256SIG0` 的分支。
  - `ReplaceNodeResults`：`FP_TO_SINT`/`FP_TO_UINT` 的 `i32` 结果处理。
  - `PerformDAGCombine`：`vfirst`/`vcpop` 在 VL=0 时的常量折叠。
  - `setTargetDAGCombine`：注册 `ISD::INTRINSIC_*` 等 opcode。
- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`
  - `Select`：`RISCVISD::` 自定义节点的选指入口。
  - `selectVSETVLI`：`vsetvli` 相关伪指令的选择。
  - `selectImm`：常量物化，和 `RISCVMatInt` 配合。
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`
  - `class RVSDNode`：`RISCVISD::` 节点的命名规则。

## 常见坑

1. **intrinsic 名字多写一个后缀。** `llvm.riscv.sha256sig0` 是无重载 intrinsic；写成 `llvm.riscv.sha256sig0.i32` 会被当成普通外部函数，llc 不报错，只生成 `call`。看到“intrinsic 没生效”时先核对名字。

2. **只加 `RISCVISD::` 节点，不加 pattern。** `LowerOperation` 可以成功生成节点，但选指阶段找不到指令，最终报 `Cannot select: ... RISCVISD::XXX`。节点、pattern、指令定义要一起加。

3. **忘记 `TargetPrefix = "riscv"`。** TableGen 会报 `target independent intrinsic ... has prefix ... that conflicts with intrinsics for target 'riscv'`，退出码 1。这个 `let` 不是装饰。

4. **把 `VLOperand` 的索引写错。** `RISCVMaskedUnarySOutUnMasked` 的 `VLOperand = 1` 指第二个参数；带 mask 的类通常把 VL 放到更后的位置。索引写错，`vsetvli` 会读错操作数，表现为选指成功但汇编的 VL 不对。

5. **把 `LowerOperation` 和 `ReplaceNodeResults` 混用。** 前者处理“结果类型合法、操作没有指令”，后者处理“结果类型本身不合法”。在 `ReplaceNodeResults` 里只返回一个值，可能让调用方拿到错误类型。

6. **以为 `setOperationAction(..., Custom)` 会自动生成代码。** 它只是把节点交给 `LowerOperation`；如果 `LowerOperation` 没有对应 case，会报 `Unimplemented RISCVTargetLowering::LowerOperation Case`。

7. **在 Optimized build 里用 `-debug-only`。** 和 15 章一样，`-debug-only=isel` 不可用；用 `-print-after=riscv-isel` 或 `-print-after=finalize-isel`。

8. **先写新 intrinsic，后搜现有实现。** 很多语义已经有 `llvm.riscv.*` 和 pattern，重复添加会增加 ABI、Clang builtin、成本模型和测试的维护面。先搜 `IntrinsicsRISCV.td`、`RISCVInstrInfo*.td` 和测试目录。

## 自我检查

- [ ] 能说出 `LowerOperation` 与 `ReplaceNodeResults` 的触发条件和返回值差异。
- [ ] 能解释 `RISCVISD::` 节点为什么不是 IR 指令，以及它在哪个阶段被匹配成机器指令。
- [ ] 能画出 `llvm.riscv.vfirst.i64.nxv4i1` → `VPatUnaryS_M` → `PseudoVFIRST_M_B16` → `vsetvli` + `vfirst.m` 的链路。
- [ ] 能解释 `RISCVMaskedUnarySOutUnMasked` 里 `LLVMMatchType<1>` 和 `VLOperand = 1` 的含义。
- [ ] 能复现 `sha256sig0` 在无 `Zknh` 时的 `Cannot select`，并说出加上 `+zknh` 后的汇编。
- [ ] 能解释为什么 `llvm.riscv.sha256sig0.i32` 会静默变成普通 `call`。
- [ ] 能用 `llvm-tblgen -gen-intrinsic-enums` 验证新 intrinsic，并复现缺少 `TargetPrefix` 的报错。
- [ ] 能说出新增 intrinsic 至少要改哪些文件，以及为什么本机只能标 guided。

## 延伸阅读

- `llvm/include/llvm/IR/IntrinsicsRISCV.td`：所有 `llvm.riscv.*` 的声明，重点看 `RISCVVIntrinsic`、`RISCVMaskedUnarySOut*`、`ScalarCryptoGprIntrinsic32`。
- `llvm/include/llvm/IR/IntrinsicsRISCV.h`：生成的枚举，注释里的 `.td` 行号是定位定义的最快入口。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td`：向量伪指令和 `VPat*` pattern。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVVLPatterns.td`：带 VL 的 `RISCVISD::` 节点和 pattern。
- `llvm/lib/Target/RISCV/RISCVInstrInfoZk.td`：`sha256sig0` 等标量加密指令。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`：`LowerOperation`、`LowerINTRINSIC_*`、`ReplaceNodeResults`、`PerformDAGCombine`。
- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`：`RISCVISD::` 节点的选指实现。
- `llvm/test/CodeGen/RISCV/`：同版本的 lit 测试；本机 sparse clone 不含测试目录，需要完整源码树。

[上一章：SelectionDAG 选指](./15-selection-dag.md) | [下一章：GlobalISel](./17-globalisel.md)