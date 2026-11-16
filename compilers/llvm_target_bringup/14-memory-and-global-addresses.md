# Ch 14 — 内存与全局地址

> **目标**：说清 load/store 架构只有 base+12 位位移时，`getelementptr`、栈帧对象、全局变量分别怎么变成地址；能从 `auipc+addi` 的每一分位移定位到 `R_RISCV_PCREL_HI20`/`R_RISCV_PCREL_LO12_I`，并解释 2047 边界和 RISC-V code model 的取舍。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。

## 为什么需要这个？

RISC-V 和 MiniRV 都是 load/store 架构：算术只对寄存器做，访存只有 `ld rt, imm(rs1)` 一种形状，`imm` 是 12 位有符号数。没有 `[base + index*8 + disp]`，也没有 pre/post-index 写回。

于是两件事必须在编译器里显式做：**地址计算是普通算术**（`a[i]` 的乘 8 加基址变成 `slli`+`add`），**符号地址不是常量**（`@g` 的地址链接期才知道，必须 `auipc+addi` 现算并留重定位）。前者错了运行时段错误，后者错了通常先在链接期报 relocation，或者更糟——链接通过但共享库加载后跳飞。因此这一章的证据要一路看到 ELF 重定位。

Ch 13 讲“DAG 形状怎么选成指令”，这一章讲“地址这个形状从哪来”。pattern 能匹配 `(add base, simm12)`，但 base 是谁、offset 从哪来、放不下怎么办，都不在 pattern 里。

## 先建立直觉

```
C:   x = a[i]                 (a 是全局数组，i 是变量)
          │
LLVM IR:  %p = getelementptr [N x i64], ptr @a, i64 0, i64 %i
          %v = load i64, ptr %p
          │
          ├── 常量下标 i=k ──────────────┐
          ▼                              ▼
   GlobalAddress @a + k*8          slli t, i, 3        ← 变量下标：先算字节偏移
          │                              │
          │   LowerGlobalAddress         │
          ▼                              ▼
      PseudoLA @a  ────────────────►  add t, base, t   ← 基址 + 偏移
          │                              │
          └──────────────┬───────────────┘
                         ▼
                 ld rt, off(base)         off ∈ [-2048, 2047]
                         │
                  off 放不下？→ 先把 off 物化到寄存器，add，再 ld 0(base)
```

全局地址那一支再展开：

```
auipc rd, %pcrel_hi(sym)       rd = PC + (hi20(sym - PC) << 12)
   ▲
   │  .Lpcrel_hi0 标签打在这条指令上
   ▼
addi  rd, rd, %pcrel_lo(.Lpcrel_hi0)   rd += lo12(sym - PC)
```

`%pcrel_lo` 为什么引用标签而不是 `sym`？低 12 位必须和“上一条 auipc 的 PC”配对；链接器通过标签找到那条 auipc，算出同一个 `sym - PC` 再取低 12 位。只写 `sym`，链接器不知道配哪一条。

## 主线 1：寻址模式只有 base + 12 位

```cpp
bool MiniRVTargetLowering::isLegalAddressingMode(const DataLayout &DL,
                                                const AddrMode &AM, Type *Ty,
                                                unsigned AS,
                                                Instruction *I) const {
  // Loads and stores take a base register and a 12-bit signed displacement.
  // An index without a base is just an address; a base plus an index would
  // need a separate add, so leave it to LSR to decide.
  if (AM.BaseGV || AM.ScalableOffset || !isInt<12>(AM.BaseOffs))
    return false;
  switch (AM.Scale) {
  case 0:
    break;
  case 1:
    if (!AM.HasBaseReg)
      break;
    return false;
  default:
    return false;
  }
  return true;
}
```

三条拒绝理由对应三个现实：`AM.BaseGV` 说明全局符号不能当寻址模式的一部分（它要么物化成寄存器，要么进重定位）；`!isInt<12>(AM.BaseOffs)` 说明位移超界；`scale != 0/1` 或 `scale==1 && HasBaseReg` 说明 `base + index` 需要额外一条 add，不能算免费。

这是给 LSR 和 DAG combine 的信号：**返回 false 不是“代码不合法”，而是“别折进来，折了我还得拆回去”**；真正兜底在选指和 `eliminateFrameIndex`。

变量下标的 GEP 在 DAG 里就是移位加加法：

```console
$ $B/llc -mtriple=minirv64 -O2 big.ll -o - | sed -n '/^dyn:/,/Lfunc_end1/p'
dyn:                                    # @dyn
	.cfi_startproc
# %bb.0:
	slli	x3, x10, 3
.Lpcrel_hi1:
	auipc	x4, arr
	addi	x4, x4, .Lpcrel_hi1
	add	x3, x4, x3
	ld	x10, 0(x3)
	jalr	x0, 0(x1)
.Lfunc_end1:
```

`slli x3, x10, 3` 是 `i*8`，`add x3, x4, x3` 是基址加偏移，`ld` 位移 0。如果 ISA 有 `[base + index*8]`，这三条会合成一条；MiniRV 没有。**`isLegalAddressingMode` 对 `scale==8` 返回 false 的理由就在这里：与其让 DAG 形成匹配不了的形状再拆，不如一开始就不折。**

| 源 | 合法化后的地址形状 | 选指结果 | 备注 |
|---|---|---|---|
| `a[0]`（全局） | `PseudoLA @a` | `auipc+addi`，`ld rt,0(base)` | 位移 0 |
| `a[255]`（i64） | `add PseudoLA, 2040` | `ld rt,2040(base)` | 2040 在 `[-2048,2047]` |
| `a[256]` | `add PseudoLA, 2048` | `addi 2047; addi 1; ld 0` | 2048 超界，两条 ADDI |
| `a[1000]` | `add PseudoLA, 8000` | `lui; addi; add; ld 0` | 超两条 ADDI 范围 |
| `a[i]` | `add base, (shl i,3)` | `slli; add; ld 0` | 无索引寻址 |
| `s.f`（栈对象） | `tframeindex` | `ld fi,off` → PEI 解析 | 位移由 `eliminateFrameIndex` 填 |
| `@g` 取地址 | `PseudoLA @g` | `auipc+addi`，重定位 | 不访存也要物化 |

## 主线 2：栈帧地址——`tframeindex` + `eliminateFrameIndex`

栈对象地址不是 `PseudoLA`，而是 frame index。pattern 直接把它当基址：

```tablegen
def : Pat<(i64 (load tframeindex:$fi)), (LD tframeindex:$fi, 0)>;
def : Pat<(store GPR:$val, tframeindex:$fi),
          (SD GPR:$val, tframeindex:$fi, 0)>;
```

`tframeindex` 是 TableGen 认识的特殊叶子，匹配 `TargetFrameIndexSDNode`。选指后 MIR 里还是 `%stack.0.buf`，真正的数字要等 PEI。frame index 作为**值**（取地址）时，`MiniRVDAGToDAGISel::Select` 生成 `ADDI <fi>, 0`，同样靠 PEI 解析。**MiniRV 没有 RISC-V 的 `SelectAddr`/`SelectAddrRegImm`**：普通地址由 `(add GPR, simm12)` pattern 匹配，frame index 由 `tframeindex` 匹配，真正“选地址”的 C++ 代码只有 `eliminateFrameIndex`。

```cpp
StackOffset
MiniRVFrameLowering::getFrameIndexReference(const MachineFunction &MF, int FI,
                                            Register &FrameReg) const {
  const MachineFrameInfo &MFI = MF.getFrameInfo();
  if (MFI.isCalleeSavedObjectIndex(FI)) {
    uint64_t Adjust = getFirstSPAdjustAmount(MF);
    FrameReg = MiniRV::X2;
    return StackOffset::getFixed(MFI.getObjectOffset(FI) +
                                 (Adjust ? Adjust : MFI.getStackSize()));
  }
  if (!hasFP(MF)) {
    FrameReg = MiniRV::X2;
    return StackOffset::getFixed(MFI.getObjectOffset(FI) + MFI.getStackSize());
  }
  ...
  FrameReg = MiniRV::X8;
  return StackOffset::getFixed(MFI.getObjectOffset(FI));
}
```

`MFI.getObjectOffset(FI)` 是相对“进入函数时 SP”的**有符号偏移**，对象在栈顶下方，通常是负数。没有 frame pointer 时，当前 SP 已经往下走了 `StackSize`，要加回来才是从当前 SP 起的正偏移；有 FP 时 `x8` 停在进入时的 SP，直接用对象偏移。

特殊情况：callee-saved 槽在大栈帧里相对“中间 SP”（spill 在第一段调整后），用 `getFirstSPAdjustAmount`；有 FP 且栈重对齐时局部对象仍从 SP 算。

`eliminateFrameIndex` 的常见路径：

```cpp
  int64_t Offset =
      TFI->getFrameIndexReference(MF, FrameIndex, FrameReg).getFixed();
  ...
  Offset += MI.getOperand(FIOperandNum + 1).getImm();

  if (isInt<12>(Offset)) {
    MI.getOperand(FIOperandNum).ChangeToRegister(FrameReg, /*isDef=*/false);
    MI.getOperand(FIOperandNum + 1).ChangeToImmediate(Offset);
    return false;
  }
```

塞得进 12 位：把 `<fi>` 换成物理基址寄存器，把占位位移换成真实 offset，load/store 的 `(base, disp)` 形状不变。塞不进走两条兜底：`ADDI <fi>, 0`（取地址）直接改写成“把 `FrameReg + Offset` 算到目标寄存器”并删除自己；load/store 则先用保留的 scratch `x5` 物化地址，再改成 `ld rt, 0(x5)`。`x5`/`x6` 在 `MiniRVRegisterInfo::getReservedRegs` 里保留，注释分别是“大偏移物化地址”和“SP 既是源又是目的时的 scratch”。

普通栈帧的两级快照（`frame.ll`：`alloca [4 x i64]`，对 `%buf[2]` store、读 `%buf[0]`）：

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel -simplify-mir frame.ll -o - 2>&1 | grep -E "ADDI %stack|SD |LD "
    %1:gpr = ADDI %stack.0.buf, 0
    SD %0, %1, 16 :: (store (s64) into %ir.p)
    %2:gpr = LD %1, 0 :: (dereferenceable load (s64) from %ir.buf)

$ $B/llc -mtriple=minirv64 -O2 -stop-after=prolog-epilog -simplify-mir frame.ll -o - 2>&1 | sed -n '/^body:/,$p'
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10
  
    $x2 = frame-setup ADDI $x2, -32
    frame-setup CFI_INSTRUCTION def_cfa_offset 32
    $x3 = ADDI $x2, 0
    SD $x10, $x3, 16 :: (store (s64) into %ir.p)
    $x3 = LD killed $x3, 0 :: (dereferenceable load (s64) from %ir.buf)
    $x10 = ADD killed $x3, killed $x10
    $x2 = frame-destroy ADDI $x2, 32
    frame-destroy CFI_INSTRUCTION def_cfa_offset 0
    PseudoRET implicit $x10
...
```

`%1 = ADDI %stack.0.buf, 0` 变成 `$x3 = ADDI $x2, 0`：frame index 解析成 `sp+0`（栈大小 32，对象偏移 -32），`SD` 的 16 保持。对象超过 2047 时同一路径分两段：`bigframe.ll` 里对象在 `sp+2500`，输出 `addi x3, x2, 2047; addi x3, x3, 453; sb x4, 0(x3)`。**prologue 拆栈调整**是 FrameLowering 的事，**拆对象偏移**是 `eliminateFrameIndex` 的事。

## 主线 3：全局地址——`LowerGlobalAddress` → `PseudoLA`

```cpp
  setOperationAction(ISD::GlobalAddress, MVT::i64, Custom);
```

`Custom` 让 operation legalization 调 `LowerOperation`，再到：

```cpp
SDValue MiniRVTargetLowering::LowerGlobalAddress(SDValue Op,
                                                 SelectionDAG &DAG) const {
  const auto *GV = cast<GlobalAddressSDNode>(Op);
  SDLoc DL(Op);
  EVT PtrVT = getPointerTy(DAG.getDataLayout());
  SDValue Sym =
      DAG.getTargetGlobalAddress(GV->getGlobal(), DL, PtrVT, GV->getOffset());
  return SDValue(DAG.getMachineNode(MiniRV::PseudoLA, DL, PtrVT, Sym), 0);
}
```

两个细节：`PseudoLA` 是**机器节点**，没有 `Pat`——lowering 直接造出来，不需要匹配任何形状，存在只是把“符号 + 偏移”保留到 AsmPrinter；`GV->getOffset()` 原样带进 `TargetGlobalAddress`，这是 commit `378ffed` 的关键。

选指阶段已经能看到它：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel g.ll -o - 2>&1 | sed -n "/Legalized selection DAG: %bb.0 .loadg/,/^$/p"
Legalized selection DAG: %bb.0 'loadg:'
SelectionDAG has 8 nodes:
  t0: ch,glue = EntryToken
      t9: i64 = PseudoLA TargetGlobalAddress:i64<ptr @g> 0
    t4: i64,ch = load<(dereferenceable load (s64) from @g)> t0, t9, poison:i64
  t6: ch,glue = CopyToReg t0, Register:i64 $x10, t4
  t7: ch = MiniRVISD::RET t6, Register:i64 $x10, t6:1
```

`load` 还是 `ISD::load`，base 已经是 `PseudoLA`；选指时 `(i64 (load GPR:$base))` 匹配成 `LD`。

`PseudoLA` 没有编码，由 `MiniRVAsmPrinter::emitSymbolAddress` 手工拼：

```cpp
  MCSymbol *HiLabel = OutContext.createTempSymbol("pcrel_hi", true);
  OutStreamer->emitLabel(HiLabel);

  MCInst AUIPC;
  AUIPC.setOpcode(MiniRV::AUIPC);
  AUIPC.addOperand(MCOperand::createReg(Dst));
  AUIPC.addOperand(MCOperand::createExpr(Target));
  EmitToStreamer(*OutStreamer, AUIPC);

  MCInst ADDI;
  ADDI.setOpcode(MiniRV::ADDI);
  ADDI.addOperand(MCOperand::createReg(Dst));
  ADDI.addOperand(MCOperand::createReg(Dst));
  ADDI.addOperand(
      MCOperand::createExpr(MCSymbolRefExpr::create(HiLabel, OutContext)));
  EmitToStreamer(*OutStreamer, ADDI);
```

顺序是：在 auipc **之前**打临时标签 `.Lpcrel_hiN`；发 `auipc dst, sym(+off)`；发 `addi dst, dst, .Lpcrel_hiN`（第三个操作数是标签，不是 `sym`）。RISC-V 走 `RISCVAsmPrinter` + `MCSymbolRefExpr::VariantKind`，文本里打印 `%pcrel_hi(g)`；MiniRV 没实现 specifier 打印，所以文本是裸的 `auipc x3, g`，**语义完全靠重定位**。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 寻址模式 | `MiniRVISelLowering.cpp` | `isLegalAddressingMode`、`isLegalAddImmediate` | 哪些形状被认为“免费” |
| 全局地址 | 同上 | `LowerGlobalAddress`、`LowerJumpTable` | `PseudoLA` 在哪造出来 |
| 栈帧布局 | `MiniRVFrameLowering.cpp` | `getFrameIndexReference`、`getFirstSPAdjustAmount` | 对象偏移、FP、大栈帧两段调整 |
| frame index 消除 | `MiniRVRegisterInfo.cpp` | `eliminateFrameIndex`、`needsFrameBaseReg`、`materializeFrameBaseRegister` | 12 位内改写、超界用 x5、ADDI 兜底 |
| 选指 | `MiniRVISelDAGToDAG.cpp` | `Select` 的 `ISD::FrameIndex`/`ISD::ADD` 分支 | 地址作为值时的两条 ADDI |
| 符号物化 | `MiniRVAsmPrinter.cpp` | `emitSymbolAddress` | 标签、auipc、addi、sym+offset |
| 编码/重定位 | `MCTargetDesc/MiniRVMCCodeEmitter.cpp`、`MiniRVELFObjectWriter.cpp` | `getMachineOpValue`、`getRelocType` | AUIPC→HI20，ADDI→LO12_I，标签保留 |
| 汇编器配对 | `MCTargetDesc/MiniRVAsmBackend.cpp` | `evaluateFixup`、`getPCRelHiFixup` | pcrel_lo 如何找到对应 auipc |
| RISC-V 参照 | `RISCVISelLowering.cpp`、`RISCVAsmPrinter.cpp` | `lowerGlobalAddress`、`RISCVMatInt` | code model、GOT、常量池 |

## 实测：全局 load/store 与重定位

```console
$ $B/llc -mtriple=minirv64 -O2 g.ll -o - | sed -n '1,15p'
	.file	"g.ll"
	.text
	.globl	loadg                           # -- Begin function loadg
	.type	loadg,@function
loadg:                                  # @loadg
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	x3, g
	addi	x3, x3, .Lpcrel_hi0
	ld	x10, 0(x3)
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	loadg, .Lfunc_end0-loadg
	.cfi_endproc
```

（`g.ll`：`@g = global i64 0`；`loadg` 是 `load i64, ptr @g` + `ret`，`storeg` 是 `store i64 %v, ptr @g` + `ret void`；store 版本只是把 `ld` 换成 `sd`。）逐条：

- `.Lpcrel_hi0` 是 `emitSymbolAddress` 打的临时标签，位置在 auipc 上；不占字节，只给重定位当锚点。
- `auipc x3, g` 把 `g` 相对当前 PC 的高 20 位加到 `x3`；`x3` 是寄存器分配器选的 scratch（MiniRV 固定保留 `x0/x2/x5/x6`，`x8` 按需保留，`x3` 可分配）。
- `addi x3, x3, .Lpcrel_hi0` 加低 12 位；文本写标签是因为必须和标签处那条 auipc 配对。
- `ld x10, 0(x3)`：全局对象在 `g+0`，位移 0。**这里没有第二次重定位**，地址已经在 x3 里。

```console
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj g.ll -o g.o
$ $B/llvm-readobj -r g.o

File: g.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_PCREL_HI20 g 0x0
    0x4 R_RISCV_PCREL_LO12_I .L0  0x0
    0x10 R_RISCV_PCREL_HI20 g 0x0
    0x14 R_RISCV_PCREL_LO12_I .L0  0x0
  }
  Section (7) .rela.eh_frame {
    0x1C R_RISCV_32_PCREL .L0  0x0
    0x30 R_RISCV_32_PCREL .L0  0x0
  }
]
```

- `0x0`/`0x10` 是重定位的**字节偏移**：`loadg` 的 auipc 在 0、addi 在 4；`storeg` 从 0x10 开始，auipc 在 0x10、addi 在 0x14。
- `R_RISCV_PCREL_HI20`：把 `g - P` 的高 20 位（四舍五入到 4KB）填进 U 型立即数，`P` 是 auipc 的地址。
- `R_RISCV_PCREL_LO12_I`：低 12 位填进 I 型立即数；它引用 `.L0`（文本里的 `.Lpcrel_hi0`），链接器通过这个标签找到对应 auipc，保证高低位来自同一个 `g - P`。
- `g 0x0`/`.L0 0x0` 是符号和 addend，这里都是 0；`.rela.eh_frame` 的 `R_RISCV_32_PCREL` 是 CFI/FDE 初始位置，与全局地址无关。

`.Lpcrel_hi0` 到 object 里叫 `.L0`：`createTempSymbol(..., true)` 造的私有临时符号，MC 层写 ELF 时编号成 `.L0`。同一个符号，两个名字。

## 实测：2047 边界

600 个 i64 的全局数组，取下标 255/256/257：

```console
$ $B/llc -mtriple=minirv64 -O2 idx.ll -o - | sed -n '/^i255:/,/Lfunc_end2/p'
i255:                                   # @i255
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	x3, arr
	addi	x3, x3, .Lpcrel_hi0
	ld	x10, 2040(x3)
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	i255, .Lfunc_end0-i255
	.cfi_endproc
                                        # -- End function
	.globl	i256                            # -- Begin function i256
	.type	i256,@function
i256:                                   # @i256
	.cfi_startproc
# %bb.0:
.Lpcrel_hi1:
	auipc	x3, arr
	addi	x3, x3, .Lpcrel_hi1
	addi	x3, x3, 2047
	addi	x3, x3, 1
	ld	x10, 0(x3)
	jalr	x0, 0(x1)
.Lfunc_end1:
	.size	i256, .Lfunc_end1-i256
	.cfi_endproc
                                        # -- End function
	.globl	i257                            # -- Begin function i257
	.type	i257,@function
i257:                                   # @i257
	.cfi_startproc
# %bb.0:
.Lpcrel_hi2:
	auipc	x3, arr
	addi	x3, x3, .Lpcrel_hi2
	addi	x3, x3, 2047
	addi	x3, x3, 9
	ld	x10, 0(x3)
	jalr	x0, 0(x1)
```

- `i255`：偏移 255×8 = **2040**，在 `[-2048,2047]` 内，直接折进 `ld` 的 imm12。
- `i256`：**2048** 超界，拆成 `2047 + 1`，两条 `addi`，`ld 0`。
- `i257`：**2056**，拆成 `2047 + 9`。

为什么不是 `2048 + 0`？`2048` 自己放不进 12 位；第一条必须取能放进去的值，`2047` 是正数侧最大值，负数侧对称取 `-2048`。

更大的偏移（下标 1000 = 8000 字节）超出“两条 ADDI”的 `4094` 上限，走常量物化：

```console
$ $B/llc -mtriple=minirv64 -O2 big.ll -o - | sed -n '/^i1000:/,/Lfunc_end0/p'
i1000:                                  # @i1000
	.cfi_startproc
# %bb.0:
	lui	x3, 2
	addi	x3, x3, -192
.Lpcrel_hi0:
	auipc	x4, arr
	addi	x4, x4, .Lpcrel_hi0
	add	x3, x4, x3
	ld	x10, 0(x3)
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	i1000, .Lfunc_end0-i1000
	.cfi_endproc
```

`lui x3, 2` 得 `0x2000`（8192），`addi x3, x3, -192` 得 `8000 = 0x1F40`，再 `add` 到基址，`ld` 位移 0。三条路径：

| 偏移范围 | 处理 | 指令数 |
|---|---|---|
| `[-2048, 2047]` | 折进 load/store 的 imm12 | 1 |
| `[-4096, 4094]` | 两条 ADDI | 2 + 访存 |
| 其他 | LUI+ADDI 物化，再 ADD | 2 + 1 + 访存 |

上界推导：`First=2047`，剩余 `Val-2047 ≤ 2047`，所以 `Val ≤ 4094`；下界 `First=-2048`，剩余 `≥ -2048`，所以 `Val ≥ -4096`。

## 实测：全局地址带偏移（commit 378ffed）

`memcpy` 展开会分别读常量的前后两半，后半段的地址是 `@c + 8`：

```console
$ $B/llc -mtriple=minirv64 -O2 off.ll -o - | sed -n '/^f:/,/Lfunc_end/p'
f:                                      # @f
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	x3, .Lc
	addi	x3, x3, .Lpcrel_hi0
	ld	x3, 0(x3)
	sd	x3, 0(x10)
.Lpcrel_hi1:
	auipc	x3, .Lc+8
	addi	x3, x3, .Lpcrel_hi1
	ld	x3, 0(x3)
	sd	x3, 8(x10)
	jalr	x0, 0(x1)
.Lfunc_end0:
```

```console
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj off.ll -o off.o
$ $B/llvm-readobj -r off.o | sed -n '1,14p'

File: off.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_PCREL_HI20 .Lc 0x0
    0x4 R_RISCV_PCREL_LO12_I .L0  0x0
    0x10 R_RISCV_PCREL_HI20 .Lc 0x8
    0x14 R_RISCV_PCREL_LO12_I .L0  0x0
  }
]
```

第二个 `PCREL_HI20` 的 addend 是 `0x8`，`PCREL_LO12_I` 仍是 0。**偏移只放在 HI20 上**，这是 RISC-V 的约定：低位只指向 auipc 标签，addend 由高位移位承载。

修复前的写法是 `MCSymbolRefExpr::create(getSymbol(...), MO.getOffset(), OutContext)`。在当前树里第三个参数是 **assembler specifier**，不是 offset；非零 offset 被当成未初始化的 specifier，触发 `MCAsmInfo::getSpecifierName` 断言。修法是用 `MCBinaryExpr::createAdd(sym, MCConstantExpr::create(offset))`，让汇编器和链接器都看到 `sym+offset`。这个 bug 能编译通过，因为 `unsigned` 和 `VariantKind` 都是整数——**类型相同、语义不同，编译器不会提醒你**。

## 实测：RISC-V code model 与 GOT

同样一份 `g.ll`，换 RISC-V 后端和 code model：

```console
$ for m in small medium large; do echo "=== $m"; \
    $B/llc -mtriple=riscv64 -mattr=+m -code-model=$m -O2 g.ll -o - \
    | sed -n '/^loadg:/,/^\.Lfunc_end0/p'; done
=== small
loadg:                                  # @loadg
	.cfi_startproc
# %bb.0:
	lui	a0, %hi(g)
	ld	a0, %lo(g)(a0)
	ret
.Lfunc_end0:
=== medium
loadg:                                  # @loadg
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ret
.Lfunc_end0:
=== large
loadg:                                  # @loadg
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(.LCPI0_0)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ld	a0, 0(a0)
	ret
.Lfunc_end0:
```

- **small（medlow）**：假设符号落在 `±2GB` 绝对地址窗口，`lui %hi` + `ld %lo`。指令最少，要求加载地址可预测。
- **medium（medany）**：假设代码和数据在同一 `±2GB` 模块内，`auipc %pcrel_hi` + `%pcrel_lo`。可放任意地址，模块不能超 2GB。
- **large**：不假设距离。64 位地址放常量池（`.LCPI0_0`），PC 相对取常量池地址，再 `ld` 出地址访存。

small 的重定位是绝对高低位：auipc 上是 `R_RISCV_HI20`，load 是 `R_RISCV_LO12_I`，store 是 `R_RISCV_LO12_S`（S 型立即数）。medium 则和 MiniRV 一样是 `PCREL_HI20`/`PCREL_LO12_I`。

开 PIC 后可抢占全局必须走 GOT：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -relocation-model=pic -code-model=medium -O2 g.ll -o - \
    | sed -n '/^loadg:/,/^\.Lfunc_end0/p'
loadg:                                  # @loadg
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	a0, %got_pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ld	a0, 0(a0)
	ret
.Lfunc_end0:
$ $B/llc -mtriple=riscv64 -mattr=+m -relocation-model=pic -code-model=medium -O2 -filetype=obj g.ll -o gp.o
$ $B/llvm-readobj -r gp.o | grep -A5 "rela.text"
  Section (3) .rela.text {
    0x0 R_RISCV_GOT_HI20 g 0x0
    0x4 R_RISCV_PCREL_LO12_I .Lpcrel_hi0 0x0
    0x10 R_RISCV_GOT_HI20 g 0x0
    0x14 R_RISCV_PCREL_LO12_I .Lpcrel_hi1 0x0
  }
```

`GOT_HI20` 取的是 **GOT 表项**的地址，不是 `g` 的地址，所以多一条 `ld a0, 0(a0)` 把真地址读出来。这是 PIC 的必然成本。

MiniRV 只有一条路：`LowerGlobalAddress` 永远发 `PseudoLA`，不看 relocation model。开 PIC 输出完全不变：

```console
$ $B/llc -mtriple=minirv64 -relocation-model=pic -O2 -filetype=obj g.ll -o gpic.o
$ $B/llvm-readobj -r gpic.o | grep -A5 "rela.text"
  Section (3) .rela.text {
    0x0 R_RISCV_PCREL_HI20 g 0x0
    0x4 R_RISCV_PCREL_LO12_I .L0  0x0
    0x10 R_RISCV_PCREL_HI20 g 0x0
    0x14 R_RISCV_PCREL_LO12_I .L0  0x0
  }
```

这不是“PIC 优化得好”，而是 **MiniRV 没有实现 GOT**：它假设符号链接期静态解析、彼此在 `±2GB` 内。对 bare-metal/静态链接是合理的最小实现，对共享库不能用。TLS 更直接：

```console
$ $B/llc -mtriple=minirv64 -O2 /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/tls-error.ll -o -
LLVM ERROR: MiniRV does not support thread-local storage
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce it.
```

`LowerOperation` 对 `ISD::GlobalTLSAddress` 和 `llvm.thread.pointer` 都 `report_fatal_error`，不让选指器报 `Cannot select`。**能明确拒绝的，不要留到选指器**（Ch 23）。

## 反直觉与踩雷

1. **错误直觉**：`auipc x3, g` 里的 `g` 就是最终地址。→ **正确认识**：它只是“符号 + 重定位”的文本形式：未链接的 `.o` 里编码是 `auipc x3, 0`，链接器才填 `g - P` 的高 20 位。
2. **错误直觉**：`PCREL_LO12_I` 的 addend 应该等于 `g` 的偏移（比如 8）。→ **正确认识**：低位引用 auipc 标签，addend 是 0；偏移全部放在 `PCREL_HI20` 上。两处都写会算错两次。
3. **错误直觉**：`ld` 偏移上限 2047，所以下标 255（2040 字节）会溢出。→ **正确认识**：2040 ≤ 2047，能折；边界是字节偏移 2047/2048，不是元素下标。i64 数组里 255/256 正好跨过。
4. **错误直觉**：`-relocation-model=pic` 会让所有 target 走 GOT。→ **正确认识**：这是 target 实现决定的。MiniRV 不看 relocation model，PIC 和 non-PIC 产出同一份 `PCREL_HI20`；对共享库而言这是错的，只是 bring-up 阶段没暴露。
5. **错误直觉**：大栈帧只要 prologue 拆 SP 调整就够了。→ **正确认识**：SP 调整和对象偏移是两件事。`bigframe` 里 SP 分 `-2048/-960`，对象偏移 2500 又在 `eliminateFrameIndex` 里分 `2047/453`。
6. **错误直觉**：`MCSymbolRefExpr::create(sym, offset, ctx)` 的第二个参数是偏移。→ **正确认识**：当前树里它是 assembler specifier，这是 commit `378ffed` 的根因。想加偏移必须构造 `MCBinaryExpr`。

## 进阶：再往深一层

- **大栈帧的两段调整**：`getFirstSPAdjustAmount` 在 `StackSize` 超 12 位且有 callee-saved 时返回 `2048 - StackAlign`，让 spill 落在第一段内，剩下的在 spill 之后再调；commit `6e7e7ab` 补完整“围绕 callee-saved spill 拆分”。Ch 11/18 展开。
- **`needsFrameBaseReg` 与 LocalStackSlotAllocation**：PEI 之前 `LocalStackSlotAllocation` 会为超界对象尝试建 base 寄存器（`materializeFrameBaseRegister` 造 `ADDI <fi>, offset`），`needsFrameBaseReg` 判断“是否必须走 base 寄存器”。MiniRV 在没有 var-sized object 时启用；有动态 alloca 时改用 frame pointer，避免和 SP 相对布局打架。
- **RISC-V 的 `lowerGlobalAddress`**：按 code model 分支（small `%hi/%lo`、medium `%pcrel`、large 常量池），PIC 用 GOT；`RISCVMatInt::generateInstSeq` 把任意 64 位常量变成最短 LUI/ADDI/SLLI/ADD 序列。MiniRV 的 `BuildWord` 是它的极简版。
- **`needsRelocateWithSymbol` 为什么要 true**：`MiniRVELFObjectWriter` 明确返回 true，注释是“低位的 PC 相对对必须命名 auipc 标签，而不是它所在的 section”。折成 section+addend，`PCREL_LO12` 就找不到对应的 hi。

## 本章重点整理

- load/store 架构只有 base + 12 位有符号位移；变量下标必须显式 `slli + add`，常量下标能折就折。
- `isLegalAddressingMode` 是给优化器的“别折”信号，不是合法性检查；兜底在选指和 frame index 消除。
- 栈对象用 `tframeindex` pattern；`getFrameIndexReference` 把负的对象偏移换算成当前 SP/FP 的正偏移；`eliminateFrameIndex` 在 12 位内直接改操作数，超界用 `x5` 或把 `ADDI <fi>,0` 改写成地址计算。
- 全局地址走 `LowerGlobalAddress` → `PseudoLA` → `emitSymbolAddress`，展开成 `auipc rd, sym(+off)` + `addi rd, rd, .Lpcrel_hiN`。
- 偏移分级：`[-2048,2047]` 折进 imm12；`[-4096,4094]` 两条 ADDI；更大用 LUI+ADDI+ADD。
- RISC-V code model：small=`lui/%hi`，medium=`auipc/%pcrel_hi`，large=常量池 + `R_RISCV_64`；PIC 用 `GOT_HI20` + 额外一次 `ld`。
- MiniRV 只有静态 PC 相对：没有 GOT、没有 TLS、忽略 relocation model；这是 bring-up 的边界，不是通用实现。

## 自我检核

- [ ] 画出 `getelementptr @arr, 0, %i` 在 MiniRV 上的 DAG 形状和最终三条指令，并说明每条对应 IR 的哪一部分。
- [ ] 为什么 `isLegalAddressingMode` 对 `scale==1 && HasBaseReg` 返回 false？不返回 false 会生成什么形状，选指时怎样？
- [ ] `getFrameIndexReference` 的四个分支各对应什么函数形态？`MFI.getObjectOffset(FI)` 为什么通常是负数？
- [ ] 偏移 2048 时为什么是 `addi 2047; addi 1`？把 `4094` 这个上界推一遍。
- [ ] 逐字段解释 `R_RISCV_PCREL_HI20 g 0x8` 和 `R_RISCV_PCREL_LO12_I .L0 0x0`：哪个是偏移、哪个是锚点、链接器怎么配对。
- [ ] small/medium/large 各假设了什么距离？large 为什么多一条 `ld` 和一个 `.quad`？

## 延伸阅读

- **[RISC-V ELF psABI Specification](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：`Relocation` 一章的 `PCREL_HI20`/`PCREL_LO12_I`/`LO12_S`/`GOT_HI20`，以及 `Code Models` 一节。
  - **能学到什么**：本章每个重定位的规范定义、addend 规则、code model 的地址窗口假设。
  - **前提**：知道 ELF relocation 的基本结构；不要求读过链接器源码。
- **[`../elf_linking/README.md`](../elf_linking/README.md)** — 本仓库课程
  - **读哪里**：重定位处理、PC 相对配对、GOT/PLT 部分。
  - **能学到什么**：链接器视角看 `PCREL_HI20`/`PCREL_LO12` 怎么变成最终立即数；为什么 `needsRelocateWithSymbol` 重要。
  - **前提**：读完本章的 `llvm-readobj -r` 实测。
- **[`../riscv_backend/README.md`](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：`RISCVISelLowering::lowerGlobalAddress`、`RISCVAsmPrinter`、`RISCVMatInt` 相关章节。
  - **能学到什么**：完整 code model/GOT/常量池实现，以及 `%pcrel_hi` specifier 的打印路径。
  - **前提**：知道 RV64 的 LUI/AUIPC/ADDI 编码；读过本章的 MiniRV 版本更容易对照。
- **[LLVM 源码：`RISCVISelLowering.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/RISCV/RISCVISelLowering.cpp)** — LLVM Project
  - **读哪里**：`lowerGlobalAddress`、`lowerBlockAddress`、`lowerJumpTable`。
  - **能学到什么**：code model 分支怎么写、`RISCVMatInt::InstSeq` 怎么被复用、PIC 判断在哪。
  - **前提**：能读 `TargetLowering` 的 `LowerOperation` 分派；配合本章的 MiniRV `LowerGlobalAddress` 对比。

→ [Ch 15 控制流与尾调用](./15-control-flow-and-tail-calls.md)
