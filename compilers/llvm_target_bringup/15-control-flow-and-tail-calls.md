# Ch 15 — 控制流与尾调用

> **目标**：把 LLVM IR 的 `br`/`brcond`/`switch`/`indirectbr`/`return` 一路追到 MiniRV 的 `b*`/`jal`/`jalr`，并解释分支范围、跳表、尾调用为什么不能写 `x1`。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

前面的章节把“一条指令怎么选出来”解决了。控制流是另一类问题：它不只是选一条指令，而是要让机器基本块之间的边、分支可达范围、以及函数出口的 ABI 语义同时成立。

如果这一层做错，编译器不会在 IR 层报错。它会安静地生成一条 displacements 已经回绕的 `beq`，程序跳到别处；或者把 `switch` 的默认分支算错；或者尾调用写了 `x1`，函数返回时跳到调用者的返回地址而不是调用者的调用者。控制流 lowering 的质量，直接决定“生成的代码能不能跑”，而不只是“跑得快不快”。

这一章回答四个问题：

1. SelectionDAG 用什么节点表示五种终结指令？
2. MiniRV 的 `analyzeBranch` 系列为什么必须知道“条件 + 两个寄存器 + 目标”？
3. 跳表和尾调用在汇编里到底长什么样？
4. 分支太远时，谁负责修，修不了会怎样？

## 先建立直觉

先看一个 8 分支的 `switch`。MiniRV 的 lowering 不是“八个 `beq` 串起来”，而是“一次范围检查 + 一次表查找 + 一次间接跳转”：

```
                      x10 = switch 值
                           |
                           v
             addi  x3, x0, 7
             bltu  x3, x10, .LBB0_10      ; 无符号比较：x10 >u 7 -> default
                           | 落在 0..7
                           v
             slli  x3, x10, 3             ; index * 8
             auipc x4, .LJTI0_0
             addi  x4, x4, .Lpcrel_hi0
             add   x3, x4, x3
             ld    x3, 0(x3)              ; 取块地址
             jalr  x0, 0(x3)              ; 间接跳转；x0 表示不写返回地址
                           |
          +----------------+----------------+----- ...
          v                v                v
       .LBB0_2          .LBB0_7          .LBB0_4
        case 0           case 1           case 2
```

表在 `.rodata` 里，按 switch 的 case 顺序排列：

```
.LJTI0_0:
    .quad .LBB0_2     # case 0
    .quad .LBB0_7     # case 1
    .quad .LBB0_4     # case 2
    ...
```

注意一个反直觉点：基本块在函数里的物理顺序（`.LBB0_2`、`.LBB0_3`……）和 case 顺序不一致，表项负责把“case 顺序”映射到“块地址”。这就是跳表 lowering 的本质。

再看控制流在 SelectionDAG 里的形状：

```
IR terminator            SelectionDAG                       MiniRV
-----------------------------------------------------------------------
br label %dest      ->   ISD::BR             ----------->   PseudoBR  -> jal x0, dest
br i1 %c, ...       ->   ISD::BRCOND         ----------->   BRCC      -> beq/bne/blt/...
switch              ->   BRCOND 树 或 BR_JT  ----------->   Bcc / PseudoLA + 通用展开
indirectbr          ->   ISD::BRIND          ----------->   PseudoBRIND -> jalr x0, 0(rs)
ret                 ->   ISD::RET            ----------->   PseudoRET -> jalr x0, 0(x1)
tail call           ->   TAIL / TAIL_REG     ----------->   PseudoTAIL -> jal x0 / jalr x0
```

## 主线：从 IR 终结指令到机器分支

### `br`：无条件边

`br label %dest` 在 SelectionDAG 里是 `ISD::BR`。MiniRV 在 `MiniRVInstrInfo.td` 里用 `PseudoBR` 匹配它：

```tablegen
def PseudoBR : MiniRVInst<(outs), (ins simm21_lsb0:$imm20), "j", "$imm20",
                          [(br bb:$imm20)], InstFormatJ>,
               PseudoInstExpansion<(JAL X0, simm21_lsb0:$imm20)> { ... }
```

`PseudoBR` 打印成 `j`，但 `PseudoInstExpansion` 把它展开成 `JAL X0, target`。所以你在汇编里看到的通常是 `jal x0, .LBB...`，而不是单独的 `j`。

### `brcond`：条件边

`br i1 %c, label %t, label %f` 先变成 `ISD::BRCOND`。MiniRV 把 `BRCOND` 标成 `Custom`：

```cpp
setOperationAction(ISD::BRCOND, MVT::Other, Custom);
// BR_CC is expanded into BRCOND plus a comparison, which is then fused back
// into one branch by LowerBRCOND.
setOperationAction(ISD::BR_CC, MVT::i64, Expand);
setOperationAction(ISD::BR_CC, MVT::i32, Expand);
```

`LowerOperation` 把 `BRCOND` 交给 `LowerBRCOND`。如果条件本身是 `SETCC`，就进入 `LowerBranchCC`；否则把条件和 0 比较，用 `SETNE` 进入同一个函数。

`LowerBranchCC` 做两件关键的事：

1. **把没有记录的谓词换方向**。有序比较只有 `BLT`/`BGE` 和 `BLTU`/`BGEU`，没有 `BGT`/`BLE`，所以 `SETGT`/`SETLE` 通过交换左右操作数变成 `SETLT`/`SETGE`。
2. **把 DAG builder 规范化过的零比较改回来**。`x > 0` 在 DAG builder 里会变成 `x >= 1`，`x <= 0` 变成 `x < 1`；不改回来，`x0` 形式永远匹配不上。这一段就是 commit `dcd9fdb` 的核心。

最后它构造目标节点：

```cpp
return DAG.getNode(MiniRVISD::BRCC, DL, MVT::Other, Chain, LHS, RHS,
                   DAG.getCondCode(CC), Dest);
```

`MiniRVISD::BRCC` 的定义在 `MiniRVInstrInfo.td`：

```tablegen
def SDT_MiniRVBrCC : SDTypeProfile<0, 4, [SDTCisSameAs<0, 1>,
                                          SDTCisVT<2, OtherVT>,
                                          SDTCisVT<3, OtherVT>]>;
def MiniRVbrcc : SDNode<"MiniRVISD::BRCC", SDT_MiniRVBrCC, [SDNPHasChain]>;
```

四个操作数是 `LHS, RHS, 条件码, 目标块`，并且带 chain。`BccPat` 把每个条件码直接映射到一条 B 型指令，同时提供两个 `x0` 变体：

```tablegen
multiclass BccPat<CondCode Cond, MiniRVInst Inst> {
  def : Pat<(MiniRVbrcc GPR:$lhs, GPR:$rhs, Cond, bb:$dst),
            (Inst GPR:$lhs, GPR:$rhs, simm13_lsb0_bb:$dst)>;
  def : Pat<(MiniRVbrcc GPR:$lhs, 0, Cond, bb:$dst),
            (Inst GPR:$lhs, (i64 X0), simm13_lsb0_bb:$dst)>;
  def : Pat<(MiniRVbrcc 0, GPR:$rhs, Cond, bb:$dst),
            (Inst (i64 X0), GPR:$rhs, simm13_lsb0_bb:$dst)>;
}
defm : BccPat<SETEQ,  BEQ>;
defm : BccPat<SETNE,  BNE>;
defm : BccPat<SETLT,  BLT>;
defm : BccPat<SETGE,  BGE>;
defm : BccPat<SETULT, BLTU>;
defm : BccPat<SETUGE, BGEU>;
```

### `switch`：比较树或跳表

`SelectionDAGBuilder::visitSwitch` 先把 case 聚成 cluster，然后 `findJumpTables` 决定哪些 cluster 适合跳表。之后有两种形态：

- **稀疏 switch**：生成 `BRCOND` 比较树，每个 case 一条条件分支。
- **密集 switch**：生成跳表。`visitJumpTableHeader` 先做范围检查，`visitJumpTable` 生成 `ISD::BR_JT` 节点。

MiniRV 没有 `LowerBR_JT`。它在构造 `TargetLowering` 时这样分工：

```cpp
setOperationAction(ISD::JumpTable, MVT::i64, Custom);
setOperationAction(ISD::BR_JT, MVT::Other, Expand);
```

`ISD::JumpTable` 进 `LowerJumpTable`，把表地址变成一个 `PseudoLA`：

```cpp
SDValue Sym = DAG.getTargetJumpTable(N->getIndex(), PtrVT);
return SDValue(DAG.getMachineNode(MiniRV::PseudoLA, DL, PtrVT, Sym), 0);
```

`ISD::BR_JT` 则走 `LegalizeDAG` 的通用展开：`index * EntrySize`、表地址加偏移、`load`、`BRIND`。最后 `BRIND` 由 `PseudoBRIND` 匹配成 `jalr x0, 0(rs)`。所以 MiniRV 只需要提供“表地址怎么物化”和“间接跳转怎么编码”，不需要自己写跳表展开。

### `indirectbr`：`BRIND`

`indirectbr` 直接变成 `ISD::BRIND`。MiniRV 的 `PseudoBRIND` 匹配 `(brind GPR:$rs)`，并展开为 `JALR X0, GPR:$rs, 0`：

```tablegen
def PseudoBRIND : MiniRVInst<(outs), (ins GPR:$rs), "jr", "$rs",
                             [(brind GPR:$rs)], InstFormatI>,
                  PseudoInstExpansion<(JALR X0, GPR:$rs, 0)> { ... }
```

### `return`：`RET` 到 `PseudoRET`

`LowerReturn` 把返回值复制到返回寄存器（`RetCC_MiniRV` 的 `X10`/`X11`），然后构造 `MiniRVISD::RET`。`PseudoRET` 匹配这个节点：

```tablegen
def PseudoRET : MiniRVInst<(outs), (ins), "ret", "", [(MiniRVret)], InstFormatI>,
                PseudoInstExpansion<(JALR X0, X1, 0)> {
  let isCodeGenOnly = 1;
  let isReturn = 1;
  let isTerminator = 1;
  let isBarrier = 1;
}
```

`isReturn`/`isBarrier`/`isTerminator` 这三个标志很重要：它告诉机器层“这个块在这里结束，后面没有 fallthrough”，也告诉 verifier 这是一个合法出口。

### `MiniRVInstrInfo` 的分支分析协议

现在看 `MiniRVInstrInfo.cpp`。这一组函数不是“打印分支”，而是让机器层能**读、删、改**基本块末尾的分支。寄存器分配后的块放置、tail merging、if-conversion、BranchRelaxation 都依赖它。

`analyzeBranch` 的约定：

- 最后一条是条件分支：`Cond = [opcode, lhs, rhs]`，`TBB = 目标`，`FBB = nullptr`（false 边就是 fallthrough）。
- 最后一条是 `PseudoBR`：`TBB = 目标`；如果它前面还有一条条件分支，则 `FBB = TBB`，`TBB` 改成条件分支的目标，`Cond` 填条件。
- 其他情况返回 `true`，表示“这个块的结构我看不懂”。

代码里最重要的细节是 `Cond` 的编码：它存的是**指令 opcode + 两个比较寄存器**，不是 LLVM 的 `CondCode`。这样 `insertBranch` 可以直接 `BuildMI` 出同一条 B 型指令，`reverseBranchCondition` 也只需要把 opcode 换成相反谓词。

`removeBranch` 从块尾往前删 `PseudoBR` 和六种条件分支，跳过 debug 指令，遇到别的指令就停。`insertBranch` 要求 `TBB` 非空，`Cond` 要么空要么正好 3 个操作数；空时插 `PseudoBR`，非空时插条件分支，如果还有 `FBB` 再补一条 `PseudoBR`。

`reverseBranchCondition` 只认六种条件分支，按谓词取反：`BEQ`↔`BNE`、`BLT`↔`BGE`、`BLTU`↔`BGEU`；其他 opcode 返回 `true`，表示不能反转。

### 分支范围与 `getInstSizeInBytes`

`isBranchOffsetInRange` 是 BranchRelaxation 的判据：

```cpp
case MiniRV::BEQ:
case MiniRV::BNE:
case MiniRV::BLT:
case MiniRV::BGE:
case MiniRV::BLTU:
case MiniRV::BGEU:
  return isInt<13>(BrOffset);
case MiniRV::JAL:
case MiniRV::PseudoBR:
  return isInt<21>(BrOffset);
```

B 型指令的立即数是 13 位有符号字节偏移（低 bit 隐含为 0），所以范围是 `[-4096, 4094]`；`jal` 是 21 位，范围约 `[-1 MiB, 1 MiB - 2]`。这两个数字不是“大概”，是编码位宽决定的。

`getInstSizeInBytes` 是配套的另一半。BranchRelaxation 要累加每个块的大小，才能算出偏移：

```cpp
case MiniRV::PseudoLA:            return 8;  // auipc + addi
case MiniRV::ADJCALLSTACKDOWN:
case MiniRV::ADJCALLSTACKUP:      return 0;  // erased by PEI
default:                          return get(MI.getOpcode()).getSize();
```

`PseudoLA` 展开成 `auipc` + `addi`，所以是 8 字节；调用序列伪指令在最终代码里被删掉，所以是 0；其余伪指令都展开成一条 32 位指令。这个函数如果返回 `~0U`（基类默认），块大小会溢出成 `0xffffffff`，BranchRelaxation 就会把所有分支都当成“超范围”。

### 太远时怎么办：`insertIndirectBranch`

条件分支太远时，BranchRelaxation 反转条件，让短条件分支落到近块（原 fallthrough），再在它后面插一条 `jal` 跳到远块。无条件 `jal` 超过 ±1 MiB 时，`insertIndirectBranch` 用保留的 `x5` 物化目标地址，再 `jr x5`：

```cpp
BuildMI(MBB, MBB.end(), DL, get(MiniRV::PseudoLA), MiniRV::X5)
    .addMBB(&DestBB);
BuildMI(MBB, MBB.end(), DL, get(MiniRV::PseudoBRIND)).addReg(MiniRV::X5);
```

`x5` 在 `MiniRVRegisterInfo::getReservedRegs` 里被保留，所以这里不会破坏任何活跃值，`RestoreBB` 可以是空的。`PseudoLA` 原本只接受符号；commit `f597d37` 让 `MiniRVAsmPrinter::emitSymbolAddress` 也接受 `MO.isMBB()`，所以它可以直接把目标块标签变成 `auipc`/`addi` 的符号。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 机器分支分析 | `MiniRVInstrInfo.cpp` | `analyzeBranch`、`removeBranch`、`insertBranch`、`reverseBranchCondition` | `Cond` 的三元组协议 |
| 分支范围 | `MiniRVInstrInfo.cpp` | `isBranchOffsetInRange`、`getInstSizeInBytes`、`getBranchDestBlock` | 13/21 位范围、伪指令大小 |
| 超远跳转 | `MiniRVInstrInfo.cpp` | `insertIndirectBranch` | `la x5` + `jr x5` |
| 条件 lowering | `MiniRVISelLowering.cpp` | `LowerBRCOND`、`LowerBranchCC` | 换方向、零比较重写、`BRCC` |
| 跳表 lowering | `MiniRVISelLowering.cpp` | `LowerJumpTable`；`ISD::BR_JT` 为 `Expand` | 表地址是 `PseudoLA`，展开是通用的 |
| 返回 | `MiniRVISelLowering.cpp` | `LowerReturn`、`CanLowerReturn` | `RetCC_MiniRV`、`RET` |
| 模式和伪指令 | `MiniRVInstrInfo.td` | `MiniRVbrcc`、`BccPat`、`PseudoBR`、`PseudoBRIND`、`PseudoRET` | 条件码到指令的映射 |
| 跳表输出 | `MiniRVAsmPrinter.cpp` | `emitSymbolAddress`、`MiniRVGenMCPseudoLowering.inc` | `.LJTI` 符号、`auipc`+`addi` |
| Pass 接线 | `MiniRVCodeGenPassBuilder.cpp` | `addPreEmitPass`、`BranchRelaxationPass` | 何时跑 relaxation |

## 实测：8 分支 switch 的真实汇编

`sw.ll` 里是 8 个 case（0..7）分别返回 10..17，default 返回 0。用 `-O2` 编译，并抽取入口和跳表两段：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 sw.ll -o - | sed -n '1,17p;51,60p'
	.file	"sw.ll"
	.text
	.globl	sw8                             # -- Begin function sw8
	.type	sw8,@function
sw8:                                    # @sw8
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x3, x0, 7
	bltu	x3, x10, .LBB0_10
# %bb.1:                                # %entry
	slli	x3, x10, 3
.Lpcrel_hi0:
	auipc	x4, .LJTI0_0
	addi	x4, x4, .Lpcrel_hi0
	add	x3, x4, x3
	ld	x3, 0(x3)
	jalr	x0, 0(x3)
.LJTI0_0:
	.quad	.LBB0_2
	.quad	.LBB0_7
	.quad	.LBB0_4
	.quad	.LBB0_5
	.quad	.LBB0_3
	.quad	.LBB0_8
	.quad	.LBB0_9
	.quad	.LBB0_6
	.size	.LJTI0_0, 64
```

中间 8 个 case 块被 `sed` 省掉了，它们都只是 `addi x10, x0, imm` 加 `jalr x0, 0(x1)`。逐段解释：

- `addi x3, x0, 7` + `bltu x3, x10, .LBB0_10` 是范围检查。它不是 `blt`，因为 `x - 0 > 7` 必须用无符号比较，才能把负数也归到 default。`bltu 7, x` 等价于 `x >u 7`。
- `slli x3, x10, 3` 把索引乘 8；`auipc` + `addi` 是 `PseudoLA` 的 PC 相对地址，`.Lpcrel_hi0` 把低 12 位绑定到高 20 位。
- `ld x3, 0(x3)` 取块地址，`jalr x0, 0(x3)` 间接跳转。真实输出带 `0(`；不要写成不存在的 `jalr x0, x3`。
- `.LJTI0_0` 的 8 个 `.quad` 按 case 0..7 排列，指向的块标签却是乱序的。这不是 bug，是 block layout 的结果。

## 实测：尾调用为什么不写 `x1`

同样的 callee，分别用 `tail call` 和普通 `call`：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 tail.ll -o - | sed -n '1,27p'
	.file	"tail.ll"
	.text
	.globl	tail_direct                     # -- Begin function tail_direct
	.type	tail_direct,@function
tail_direct:                            # @tail_direct
	.cfi_startproc
# %bb.0:
	jal	x0, sw8
.Lfunc_end0:
	.size	tail_direct, .Lfunc_end0-tail_direct
	.cfi_endproc
                                        # -- End function
	.globl	plain_direct                    # -- Begin function plain_direct
	.type	plain_direct,@function
plain_direct:                           # @plain_direct
	.cfi_startproc
# %bb.0:
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	.cfi_offset x1, -8
	jal	x1, sw8
	ld	x1, 8(x2)
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

尾调用只生成一条 `jal x0, sw8`。为什么不是 `jal x1, sw8`？因为 `jal` 的 `rd` 字段决定“返回地址写到哪里”。写 `x0` 表示丢弃返回地址，`x1` 保持原值。

语义上：

```
caller 的 caller
      |  返回地址 R
      v
   tail_direct  -- jal x0, sw8 -->  sw8
                                      |
                                      | sw8 的 ret 用 x1
                                      v
                              直接回到 R（caller 的 caller）
```

如果尾调用写 `x1`，`sw8` 的 `jalr x0, 0(x1)` 会回到 `tail_direct` 里 `jal` 的下一条指令，而那条指令已经不存在（尾调用是块的终结），程序就错了。所以尾调用的本质是“把自己的返回地址原封不动借给 callee”。

如果函数自己有栈帧，PEI 会在 `PseudoTAIL` 前放 epilogue，先释放帧再跳：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/tail-call.ll -o - | sed -n '/^with_frame:/,/jal x0/p'
with_frame:                             # @with_frame
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -32
	.cfi_def_cfa_offset 32
	addi	x3, x2, 0
	sd	x10, 0(x3)
	addi	x2, x2, 32
	.cfi_def_cfa_offset 0
	jal	x0, callee
```

`addi x2, x2, 32` 必须在 `jal` 之前，因为 callee 要复用 `tail_direct` 进入时的栈顶；如果帧还占着，callee 的栈帧会叠在错误的位置。

## 实测：分支太远时 relaxation 做什么

`branch-relaxation.ll` 用 `.space` 把近块撑大，强制目标超出范围：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/branch-relaxation.ll -o - | sed -n '1,41p'
	.file	"branch-relaxation.ll"
	.text
	.globl	cond_far                        # -- Begin function cond_far
	.type	cond_far,@function
cond_far:                               # @cond_far
	.cfi_startproc
# %bb.0:                                # %entry
	bne	x10, x0, .LBB0_1
	jal	x0, .LBB0_2
.LBB0_1:                                # %near
	#APP
	.zero	4200
	#NO_APP
.LBB0_2:                                # %exit
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	cond_far, .Lfunc_end0-cond_far
	.cfi_endproc
                                        # -- End function
	.globl	uncond_far                      # -- Begin function uncond_far
	.type	uncond_far,@function
uncond_far:                             # @uncond_far
	.cfi_startproc
# %bb.0:                                # %entry
	bne	x10, x0, .LBB1_1
# %bb.3:                                # %entry
.Lpcrel_hi0:
	auipc	x5, .LBB1_2
	addi	x5, x5, .Lpcrel_hi0
	jalr	x0, 0(x5)
.LBB1_1:                                # %near
	#APP
	.zero	1100000
	#NO_APP
	addi	x10, x0, 1
	jalr	x0, 0(x1)
.LBB1_2:                                # %far
	addi	x10, x0, 0
	jalr	x0, 0(x1)
.Lfunc_end1:
	.size	uncond_far, .Lfunc_end1-uncond_far
```

`cond_far` 的处理是“条件反转 + 跳过近块”：

- 原来的 `beq x10, x0, far` 变成 `bne x10, x0, near`。
- fallthrough 直接落到 `jal x0, far`，这条 `jal` 的范围是 ±1 MiB，足以跨过 4200 字节。
- 近块执行完自然落到 `exit`。

`uncond_far` 的近块有 1,100,000 字节，超过 `jal` 的 ±1 MiB，所以 `insertIndirectBranch` 介入：

```
auipc x5, .LBB1_2
addi  x5, x5, .Lpcrel_hi0
jalr  x0, 0(x5)
```

`x5` 是保留寄存器，所以不需要 restore block。这里也能看到 `PseudoLA` 的 `MO.isMBB()` 路径：目标不是全局符号，而是一个机器基本块标签。

如果连 32 位偏移都放不下，`insertIndirectBranch` 会直接拒绝：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 huge2.ll -o /dev/null 2>&1 | head -2
LLVM ERROR: branch offsets outside the signed 32-bit range are not supported
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce the bug.
```

这个错误不是“汇编器报错”，而是编译器在 `insertIndirectBranch` 里主动 `report_fatal_error`。MiniRV 没有链接器 relaxation 这一层；它能做的只有在 codegen 里把目标物化到寄存器。

## 实测：`x0` 分支形式和编码

commit `dcd9fdb` 之前，`x > 0` 会先物化常量 1：

```console
$ git -C /home/ubuntu/llvm/trees/dsl-tablegen show --stat --oneline dcd9fdb
dcd9fdbac3b4 [MiniRV] Compare against zero with the x0 branch forms
 llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp |  50 ++++++++++
 llvm/lib/Target/MiniRV/MiniRVInstrInfo.td     |   5 +-
 llvm/test/CodeGen/MiniRV/compare-zero.ll      | 138 ++++++++++++++++++++++++++
 3 files changed, 192 insertions(+), 1 deletion(-)
```

之后 `LowerBranchCC` 把 `x >= 1` 改写成 `0 < x`，直接匹配 `x0` 形式。当前树的真实输出：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/compare-zero.ll -o - | sed -n '1,16p'
	.file	"compare-zero.ll"
	.text
	.globl	sgt_zero                        # -- Begin function sgt_zero
	.type	sgt_zero,@function
sgt_zero:                               # @sgt_zero
	.cfi_startproc
# %bb.0:                                # %entry
	bge	x0, x10, .LBB0_2
# %bb.1:                                # %t
	jalr	x0, 0(x1)
.LBB0_2:                                # %f
	addi	x10, x0, 0
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	sgt_zero, .Lfunc_end0-sgt_zero
	.cfi_endproc
```

`bge x0, x10` 就是 `0 >= x`，也就是 `!(x > 0)`。这里没有 `addi x3, x0, 1`。

分支指令的编码和条件对照如下（编码由 `llvm-mc -show-encoding` 实测）：

| 指令 | LLVM 条件码 | funct3 | 可达范围 | MiniRV 节点 |
|---|---|---|---|---|
| `beq` | `SETEQ` | `000` | ±4 KiB | `BRCC` |
| `bne` | `SETNE` | `001` | ±4 KiB | `BRCC` |
| `blt` | `SETLT` | `100` | ±4 KiB | `BRCC` |
| `bge` | `SETGE` | `101` | ±4 KiB | `BRCC` |
| `bltu` | `SETULT` | `110` | ±4 KiB | `BRCC` |
| `bgeu` | `SETUGE` | `111` | ±4 KiB | `BRCC` |
| `jal` | — | — | ±1 MiB | `PseudoBR` / `PseudoCALL` / `PseudoTAIL` |
| `jalr` | — | — | 任意（寄存器） | `PseudoBRIND` / `PseudoRET` / `PseudoCALLReg` |

## 实测：loop term folding 不是 IR 优化

commit `c0b45b1` 只改了一个开关：

```console
$ git -C /home/ubuntu/llvm/trees/dsl-tablegen show --stat --oneline c0b45b1
c0b45b1bb9f9 [MiniRV] Enable loop term folding
 llvm/lib/Target/MiniRV/MiniRVCodeGenPassBuilder.cpp | 8 +++++++-
 1 file changed, 7 insertions(+), 1 deletion(-)
```

它不是 IR pass，而是 codegen pipeline 在 LSR 之后运行的 `LoopTermFoldPass`。打开后，`bf10.ll` 的 fill 循环用指针比较终止：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/lib/Target/MiniRV/dev/e2e/bf10.ll -o - | sed -n '1,33p'
	.file	"bf10.ll"
	.text
	.globl	main                            # -- Begin function main
	.type	main,@function
main:                                   # @main
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -80
	.cfi_def_cfa_offset 80
	addi	x3, x0, 0
	addi	x4, x2, 0
	addi	x7, x4, 80
.LBB0_1:                                # %fill
                                        # =>This Inner Loop Header: Depth=1
	sd	x3, 0(x4)
	addi	x3, x3, 3
	addi	x4, x4, 8
	bne	x4, x7, .LBB0_1
# %bb.2:                                # %sum
	addi	x7, x0, 0
	addi	x3, x2, 0
	addi	x4, x3, 80
.LBB0_3:                                # %loop
                                        # =>This Inner Loop Header: Depth=1
	addi	x10, x7, 0
	ld	x7, 0(x3)
	add	x7, x10, x7
	addi	x3, x3, 8
	bne	x3, x4, .LBB0_3
# %bb.4:                                # %done
	addi	x2, x2, 80
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

循环尾部是 `bne x4, x7`（指针和尾指针比较），不是“计数器 + 立即数比较再单独维护指针”。commit message 记录的改动前/后指令数是 `bf10 21 -> 18`；当前树用 `grep -cP '^\t(?!\.|#)' bf10.s` 数出来也是 18。

## 反直觉与踩雷

1. **错误直觉**：`switch` 一定会生成跳表。→ **正确认识**：只有密集 case 才走 `BR_JT`；稀疏 case 走 `BRCOND` 比较树。`jump-table.ll` 里的 `sparse` 就明确 `CHECK-NOT: .LJTI`。
2. **错误直觉**：范围检查用 `blt` 就够了。→ **正确认识**：必须用无符号 `bltu`。`x = -1` 有符号小于 7，但它应该落到 default；无符号比较才能把负数排除在表外。
3. **错误直觉**：尾调用就是“普通调用 + 立刻返回”，所以也要写 `x1`。→ **正确认识**：尾调用是 `jal x0` / `jalr x0`，`x1` 保持调用者的返回地址；写 `x1` 会让 callee 返回到一个已经不存在的续点。
4. **错误直觉**：分支超出范围，汇编器或链接器会自动修。→ **正确认识**：MiniRV 的 `jal` 立即数在 codegen 里编码，链接器不做 relaxation；必须由 `BranchRelaxationPass` 在 emit 前处理。超过 32 位还会 fatal。
5. **错误直觉**：`x > 0` 自然会用 `x0` 形式。→ **正确认识**：DAG builder 会把它规范化成 `x >= 1`，需要在 `LowerBranchCC` 里显式改回 `0 < x`；否则会多一条物化常量 1 的指令。
6. **错误直觉**：`LoopTermFold` 是 IR 层的循环优化。→ **正确认识**：它是 codegen pass，位置在 LSR 之后，通过 `CGPassBuilderOption::EnableLoopTermFold` 打开。

## 进阶：再往深一层

**BranchRelaxation 的代价模型**。它先给每个块算 size 和 offset，再逐条检查分支；条件分支超范围时，它插入一个新块放 `jal`，自己保留反向条件分支；无条件超范围时，它用 `insertIndirectBranch`。MiniRV 的 `la x5` + `jr x5` 是 12 字节，比一条 `jal` 贵，但只在超过 1 MiB 时发生。`x5` 被保留的代价是寄存器分配少一个可用寄存器，这是 bring-up 阶段用空间换正确性的典型取舍。

**为什么 `getInstSizeInBytes` 必须先实现**。BranchRelaxation 用块大小累加 offset；基类默认返回 `~0U`。如果直接跑 pass，`0xffffffff` 会让所有块偏移溢出，relaxation 要么误判、要么算出负偏移。commit `f597d37` 把它和 `getBranchDestBlock`、`reverseBranchCondition`、`insertIndirectBranch` 一起补上，才是完整的一套。

**尾调用和 ABI 的边界**。尾调用只能转发寄存器参数。栈参数要写到“当前函数接收自己栈参数的那块区域”，而 epilogue 马上就要把这块区域还给调用者；写进去的内容会被 callee 当成自己的参数读，语义就错了。所以 `LowerCall` 在 `CCInfo.getStackSize() != 0` 时把 `musttail` 直接拒绝，普通 `tail` 降级成普通调用。这个决策在 Ch 16 会从调用序列角度再讲一次。

## 本章重点整理

- `br` → `ISD::BR` → `PseudoBR` → `jal x0`；`brcond` → `ISD::BRCOND` → `MiniRVISD::BRCC` → 六种 B 型指令。
- `switch` 密集时走 `ISD::BR_JT` + `JumpTable`：MiniRV 只 custom-lower 表地址，`BR_JT` 本身用通用展开。
- `analyzeBranch` 的 `Cond` 是 `[opcode, lhs, rhs]`，不是 LLVM 条件码；这让删/插/反转分支不需要重新做选择。
- 分支范围由 `isBranchOffsetInRange`（13/21 位）和 `getInstSizeInBytes` 共同决定；条件太远反转 + `jal`，无条件太远 `la x5` + `jr x5`。
- 尾调用用 `x0` 目的寄存器，保留 `x1`；有帧时先释放帧再跳。
- `x0` 分支形式和 `LoopTermFold` 都是“少一条指令”的局部优化，但前提是 lowering 里把规范化形式改回来。

## 自我检核

- 不看源码，写出 `analyzeBranch` 在“条件分支 + 无条件分支”连续出现时如何填 `TBB`/`FBB`/`Cond`。
- 解释 `isInt<13>` 为什么对应 ±4 KiB，而不是 ±8 KiB；低 bit 去哪了？
- 画出 `switch` 从 `visitSwitch` 到 `jalr x0, 0(x3)` 的节点链，标出 `PseudoLA` 在哪一步出现。
- 为什么 `tail call` 生成 `jal x0, callee` 而不是 `jal x1, callee`？如果写 `x1`，运行时会发生什么？
- 给定一个 `bge` 目标距离 5000 字节，说明 BranchRelaxation 会怎样改；如果距离 1.5 MiB 呢？
- `x > 0` 在 DAG builder 里被改写成什么？MiniRV 又把它改回什么？为什么需要两边都改？

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 项目
  - **读哪里**：SelectionDAG 的 “SelectionDAG Overview” 和 “The SelectionDAG” 两节，重点看 `ISD::BR`、`BRCOND`、`BR_JT`、`BRIND`。
  - **能学到什么**：控制流节点在 DAG 里为什么带 chain，`BR_JT` 为什么需要 `JumpTable` 节点。
  - **前提**：先读完本课程 Ch 13（SelectionDAG patterns），否则会被 legalize 的术语打断。

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 社区
  - **读哪里**：整数调用约定、栈帧布局、`ra`/`sp` 的角色。
  - **能学到什么**：`x1` 为什么是链接寄存器，栈参数为什么在进入函数时位于 `sp` 之上。
  - **前提**：能读基本的 RISC-V 汇编；不要求先读完整份 psABI。

- **[LLVM `BranchRelaxation.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/BranchRelaxation.cpp)** — LLVM 项目
  - **读哪里**：`isBlockInRange`、`fixupConditionalBranch`、`fixupUnconditionalBranch`、`insertIndirectBranch` 的调用点。
  - **能学到什么**：目标只需要提供范围判断、块目标、反转和间接跳转四个 hook，通用算法就能工作。
  - **前提**：读过本章的 `isBranchOffsetInRange` 和 `getInstSizeInBytes`。
→ [Ch 16 调用序列](./16-call-sequence.md)
