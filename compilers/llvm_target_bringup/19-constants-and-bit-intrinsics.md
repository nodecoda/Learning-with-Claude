# Ch 19 — 常量与位运算

> **目标**：能解释 RV64 上任意 64 位常量如何变成 `lui/addi/slli/or`，能读懂 MiniRV 的常量选指与 bit intrinsic 展开，并能判断一个操作该做成 target 指令、DAG combine 还是 libcall。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。MiniRV triple 是 `minirv64`。

## 为什么需要这个？

常量是后端最不起眼、也最容易埋 bug 的一层。一个 `ret i64 0x123456789abcdef0` 在 IR 里只是一个 `Constant` 节点，但 RV64I 没有“把 64 位立即数装进寄存器”的指令：`addi` 只有 12 位立即数，`lui` 只有 20 位，而且 RV64 的 `lui` 还会把 32 位结果符号扩展到 64 位。后端必须自己把常量拆成一条或几条真实指令。

这一层解决三个问题：
1. **常量物化（materialization）**：任意 `i64` 如何用有限立即数拼出来，代码尺寸和寄存器压力如何权衡。
2. **位运算 intrinsic**：`ctpop`、`ctlz`、`bswap`、`fshl` 这些 IR 操作没有对应的 RV64I 单指令，谁负责展开？展开成什么？
3. **i128 的合法化**：一个 128 位值在 64 位机器上就是两个寄存器，加减乘除分别怎么落到寄存器对、进位链和运行时调用上？

这三个问题刚好对应三种实现层级：**target instruction**、**DAG combine / 通用展开**、**libcall**。本章的最终目的不是背指令序列，而是拿到一张“该在哪一层实现”的判断表。

## 先建立直觉

先看 64 位常量合成的全景。核心思想只有一句：**把 64 位切成两个 32 位字，每个字再用 `lui` 的高 20 位加 `addi` 的低 12 位拼；低位字要清零高半，高位字要左移 32 位，最后用 `or` 合并。**

```
0x123456789abcdef0
        │
        ├── high word 0x12345678 ──► lui 74565 (0x12345)
        │                            addi 1656 (0x678)
        │                            slli 32
        │
        └── low  word 0x9abcdef0 ──► lui -414770 (0x9abce, 符号扩展)
                                     addi -272 (0xfffffef0)
                                     slli 32; srli 32   ← 清掉高 32 位
                                             │
                              high << 32 ────┼──── low (zero-extended)
                                             ▼
                                          or  x10, high, low
```

为什么低位字要 `slli 32; srli 32`？因为 `lui` 在 RV64 上先把 32 位结果符号扩展到 64 位。`0x9abcdef0` 的 bit 31 是 1，`lui+addi` 得到的是 `0xffffffff9abcdef0`；左移再右移 32 位就把高 32 位清零，得到 `0x000000009abcdef0`。高位字则相反：它本来就要落在 bit 32..63，所以 `slli 32` 之后不再清高半。

再看立即数边界：

| 编码 | 字段 | 可表示范围 | 关键点 |
|---|---|---|---|
| `addi rd, rs1, imm` | 12 位有符号 | `[-2048, 2047]` | 超出就要先物化 |
| `lui rd, imm20` | 20 位，左移 12 | 32 位结果 `imm20 << 12`，RV64 再符号扩展 | `imm20=0x80000` 会得到 `0xffffffff80000000` |
| `slli/srli/srai` | 6 位无符号 | `[0, 63]` | RV64 的 shift amount |
| `addiw` | 12 位有符号 | `[-2048, 2047]` | 结果截断到 32 位再符号扩展 |

一个 32 位常量 `V` 的拆分公式是：

```
hi = (V + 0x800) >> 12      // 先加 0x800 再右移，相当于四舍五入到 4KB
lo = V - (hi << 12)         // lo 落在 [-2048, 2047]
```

`+0x800` 是必须的。比如 `V=2048`，不 rounding 会得到 `hi=0, lo=2048`，`addi` 放不下；rounding 后 `hi=1, lo=-2048`，`lui 1; addi -2048` 正好是 2048。

## 主线：MiniRV 的常量物化

### 没有 `li`，也没有 `PseudoMovImm`

MiniRV 的常量物化不在某个叫 `materializeConstant` 的函数里，也没有 `selectImm` 或 `PseudoMovImm`。它藏在 `MiniRVDAGToDAGISel::Select` 的最前面：

```cpp
// llvm/lib/Target/MiniRV/MiniRVISelDAGToDAG.cpp
void MiniRVDAGToDAGISel::Select(SDNode *N) {
  if (N->isMachineOpcode()) { ... return; }

  if (N->getOpcode() == ISD::ADD) {
    // 常量加数放不进 simm12，但在 [-4096, 4094] 内：拆成两条 addi
  }

  if (N->getOpcode() == ISD::Constant) {
    // 没有 load-immediate 记录：用 LUI + ADDI 拼
  }

  if (N->getOpcode() == ISD::FrameIndex) { ... }

  SelectCode(N);   // 其余交给 TableGen 生成的 matcher
}
```

这个顺序很重要：`Select` 是 target 的“先手”，只处理 pattern 表达不了、或表达出来不划算的形状；其余一律落回 `SelectCode`。MiniRV 的 corpus 里没有 `LI` 记录，所以常量只能走手写路径。

`ISD::Constant` 分支内部有一个 `BuildWord` lambda，负责把一个 32 位字变成 `lui+addi`（必要时加 `slli/srli`）：

```cpp
auto BuildWord = [&](uint32_t Word) -> SDValue {
  int64_t V = Word;
  bool ClearUpperWord = Word >= 0x80000000u;
  if (ClearUpperWord)
    V -= int64_t(1) << 32;

  int64_t Hi = (V + 0x800) >> 12;
  int64_t Lo = V - (Hi << 12);
  SDValue Res = LUI(Hi);
  if (Lo) Res = ADDI(Res, Lo);
  if (ClearUpperWord) Res = SRLI(SLLI(Res, 32), 32);
  return Res;
};
```

注意 `BuildWord` 的语义是“生成一个 **零扩展的 32 位模式**”。这个语义对 `[0, 2^32)` 的常量成立，对负数不成立——本章“踩雷”会展开。

### 8 个真实常量

下面这段是 `llc -mtriple=minirv64 -O2` 的原始输出，只过滤掉了 `.cfi` 和注释行：

```console
$ $B/llc -mtriple=minirv64 -O2 consts.ll -o - | grep -E '^[a-zA-Z_][a-zA-Z0-9_]*:|^[[:space:]]+(addi|lui|slli|srli|or|jalr)'
c0:                                     # @c0
	addi	x10, x0, 0
	jalr	x0, 0(x1)
c1:                                     # @c1
	addi	x10, x0, 1
	jalr	x0, 0(x1)
cm1:                                    # @cm1
	addi	x10, x0, -1
	jalr	x0, 0(x1)
c2047:                                  # @c2047
	addi	x10, x0, 2047
	jalr	x0, 0(x1)
c2048:                                  # @c2048
	lui	x3, 1
	addi	x10, x3, -2048
	jalr	x0, 0(x1)
c4095:                                  # @c4095
	lui	x3, 1
	addi	x10, x3, -1
	jalr	x0, 0(x1)
c3968:                                  # @c3968
	lui	x3, 1
	addi	x10, x3, -128
	jalr	x0, 0(x1)
cbig:                                   # @cbig
	lui	x3, 74565
	addi	x3, x3, 1656
	slli	x3, x3, 32
	lui	x4, -414770
	addi	x4, x4, -272
	slli	x4, x4, 32
	srli	x4, x4, 32
	or	x10, x3, x4
	jalr	x0, 0(x1)
```

逐个解释：

- `0`、`1`、`-1`、`2047` 都在 `simm12` 内，直接 `addi rd, x0, imm`。`x0` 恒为 0，所以这就是 RV64 的“装小立即数”。
- `2048 = 0x800`：`hi = (2048 + 2048) >> 12 = 1`，`lo = 2048 - 4096 = -2048`。`lui x3,1` 得到 `0x1000`，`addi -2048` 得到 `0x800`。
- `4095 = 0xfff`：`hi=1`，`lo = 4095 - 4096 = -1`。这是“低位接近 4KB 边界”的典型形态。
- `3968 = 0xf80`：`hi=1`，`lo = 3968 - 4096 = -128`。同样是因为 `0xf80 + 0x800 = 0x1180`，右移 12 位进 1。
- `0x123456789abcdef0`：低字 `0x9abcdef0 = 2596069104`，有 `V = 2596069104 - 2^32 = -1698898192`；`hi = (-1698898192 + 2048) >> 12 = -414770`，`lo = -1698898192 - (-414770 << 12) = -272`。高字 `0x12345678 = 305419896`；`hi=74565`，`lo=1656`。高字 `slli 32`，低字 `slli 32; srli 32`，最后 `or`。
这里出现的 `x3`、`x4` 是 caller-saved 临时寄存器，`x10` 是返回值寄存器。常量物化不占用 `ra`，所以不需要栈帧。

### 当常量加数放不进 `simm12`

MiniRV 在 `ISD::ADD` 分支还做了一个小优化：如果常量在 `[-4096, 4094]` 内但不在 `simm12` 内，就拆成两条 `addi`，而不是先物化常量再 `add`：

```console
$ $B/llc -mtriple=minirv64 -O2 addimm.ll -o -
f:
	addi	x3, x10, 2047
	addi	x10, x3, 953
	jalr	x0, 0(x1)
```

`3000 = 2047 + 953`，两条 `addi` 比“`lui+addi` 物化 3000 再 `add`”少一条指令；`1000000` 超出范围，走正常物化再 `add`。

### `PseudoLA` 与 AsmPrinter 的分工

MiniRV 确实有伪指令，但只用于**符号地址**，不是常量：

- `MiniRVInstrInfo.td` 定义 `PseudoLA`（`la rd, symbol`），`MiniRVAsmPrinter::emitSymbolAddress` 把它展开成 `auipc+addi`。
- `MiniRVAsmPrinter::emitInstruction` 先处理 `PseudoLA`，再尝试 `lowerPseudoInstExpansion`（`PseudoRET`/`PseudoBR`/`PseudoCALL` 等），最后走 `MiniRVMCInstLower`。
- 常量在选指阶段已经变成真实的 `LUI/ADDI/SLLI/OR`，所以 `MiniRVMCInstLower::Lower` 只需要把 `MO_Immediate` 复制成 `MCOperand::createImm`，`MiniRVInstPrinter` 负责打印十进制/十六进制。

对比 RISC-V 参考实现：`RISCVISelDAGToDAG.cpp` 有 `selectImm`，它调用 `RISCVMatInt::generateInstSeq`；当序列长度为 2 且开了 rematerializable movimm 时，会生成 `PseudoMovImm`，否则 `selectImmSeq` 直接展开。RISC-V 还有 assembler 层的 `PseudoLI`（`RISCVInstrInfo.td`），只用于汇编器解析 `li` 并立即展开。MiniRV 把这三层都省掉了，代价是常量逻辑写死在 `Select` 里，且没有 rematerialization 的收益。

### RISC-V 的 `li` 展开长什么样

`li` 不是 RV64 指令，是汇编器伪指令。用 `llvm-mc -show-encoding` 可以看到它如何把任意常量拆成 `lui/addi/slli`：

```console
$ $B/llvm-mc -triple=riscv64 -show-encoding li.s
	lui	a0, 583                         # encoding: [0x37,0x75,0x24,0x00]
	addi	a0, a0, -1875                   # encoding: [0x13,0x05,0xd5,0x8a]
	slli	a0, a0, 14                      # encoding: [0x13,0x15,0xe5,0x00]
	addi	a0, a0, -947                    # encoding: [0x13,0x05,0xd5,0xc4]
	slli	a0, a0, 12                      # encoding: [0x13,0x15,0xc5,0x00]
	addi	a0, a0, 1511                    # encoding: [0x13,0x05,0x75,0x5e]
	slli	a0, a0, 13                      # encoding: [0x13,0x15,0xd5,0x00]
	addi	a0, a0, -272                    # encoding: [0x13,0x05,0x05,0xef]
	li	a1, 1                           # encoding: [0x93,0x05,0x10,0x00]
	slli	a1, a1, 11                     # encoding: [0x93,0x95,0xb5,0x00]
	li	a2, 1                           # encoding: [0x13,0x06,0x10,0x00]
	slli	a2, a2, 31                     # encoding: [0x13,0x16,0xf6,0x01]
```

这是 `li a0, 0x123456789abcdef0` 的展开：它没有按“两个 32 位字 + `or`”来拆，而是用连续的 `slli/addi` 逐段构造。RISC-V 的 `RISCVMatInt` 会按指令数和可压缩性做代价比较；MiniRV 的实现更朴素，但足以覆盖 64 位常量。

RISC-V codegen 对大到不划算的常量还会放常量池（`.rodata.cst8` + `lui/ld`）；MiniRV 没有这条路径，所以选择内联物化。

MiniRV 没有常量池 lowering，也没有重定位到 `.rodata` 的路径，所以它选择内联物化。这是“最小可用”与“成熟后端”的典型差异。

## 主线：位运算 intrinsic

### 谁负责展开

IR 层的 `llvm.ctpop/ctlz/cttz/bswap/bitreverse` 在 SelectionDAG 里对应 `ISD::CTPOP/CTLZ/CTTZ/BSWAP/BITREVERSE`。MiniRV 在 `MiniRVISelLowering` 里显式把它们标成 `Expand`：

```cpp
setOperationAction(
    {ISD::CTPOP, ISD::CTLZ, ISD::CTTZ, ISD::BSWAP, ISD::BITREVERSE},
    MVT::i64, Expand);
```

`ISD::ABS`、`ISD::SMAX`、`ISD::SMIN`、`ISD::FSHL`、`ISD::FSHR` 在 `TargetLoweringBase` 里默认就是 `Expand`，所以 MiniRV 不用再写。`Expand` 的意思是“合法化器用已有节点拼出来”，不是“调用运行时”。

### `ctpop`：SWAR + 一次乘法

```console
$ $B/llc -mtriple=minirv64 -O2 bits.ll -o -
t_pop:
	lui	x3, 209715
	addi	x3, x3, 819
	slli	x4, x3, 32
	or	x3, x4, x3
	lui	x4, 349525
	addi	x4, x4, 1365
	slli	x7, x4, 32
	or	x4, x7, x4
	srli	x7, x10, 1
	and	x4, x7, x4
	sub	x4, x10, x4
	and	x7, x4, x3
	srli	x4, x4, 2
	and	x3, x4, x3
	add	x3, x7, x3
	lui	x4, 4112
	addi	x4, x4, 257
	slli	x7, x4, 32
	or	x4, x7, x4
	lui	x7, 61681
	addi	x7, x7, -241
	slli	x10, x7, 32
	or	x7, x10, x7
	srli	x10, x3, 4
	add	x3, x3, x10
	and	x3, x3, x7
	mul	x3, x3, x4
	srli	x10, x3, 56
	jalr	x0, 0(x1)
```

这段看着长，其实是标准的 64 位 SWAR popcount：

1. `0x3333...3333` 由 `lui 209715 (0x33333); addi 819 (0x333); slli 32; or` 复制成 64 位。
2. `0x5555...5555` 由 `lui 349525 (0x55555); addi 1365 (0x555); slli 32; or` 得到。
3. `x = x - ((x >> 1) & 0x5555...)`。
4. `x = (x & 0x3333...) + ((x >> 2) & 0x3333...)`。
5. `x = (x + (x >> 4)) & 0x0f0f...`，其中 `0x0f0f...` 由 `lui 61681 (0xf0f1); addi -241 (-0xf1); slli 32; or` 得到 `0x0f0f0f0f0f0f0f0f`。
6. `pop = (x * 0x0101...0101) >> 56`，乘法常量为 `lui 4112 (0x1010); addi 257 (0x101); slli 32; or`。

最后用 `mul` 而不是一串加法，是因为 MiniRV 有 RV64M；RISC-V 无 M 时这个常量乘法会被展开成 `slli/add` 序列（不是单条 `mul`），代码会明显变长。

### `ctlz/cttz`：先处理零，再数 1

`ctlz` 的非零路径先做“位扩散”，把最高位右侧全部填 1，再取反做 popcount：

```console
t_lz:
	beq	x10, x0, .LBB1_3
	srli	x3, x10, 1
	or	x3, x10, x3
	...                 # 位扩散到最低位
	xori	x3, x3, -1
	...                 # 对取反后的值做 popcount
	addi	x10, x0, 64
	jalr	x0, 0(x1)
.LBB1_3:
	addi	x10, x0, 64
	jalr	x0, 0(x1)
```

`beq x10, x0` 是 `is_zero_poison=false` 时的零输入分支，返回 64。`cttz` 则先算 `x & -x` 取出最低位的 1，再对这个单比特值做 popcount；零输入同样返回 64。

### `bswap` 与 `bitreverse`：掩码 + 移位

`bswap` 是字节交换，输出里的 `srli 24`、`slli 24`、`srli 8`、`slli 8` 以及 `0x00ff00ff...` 掩码就是按字节重排。`bitreverse` 先做一次 `bswap`，再做 4 位、2 位、1 位的成对交换（汇编里是连续的 `srli/and/slli/or`）。

`0x0f0f...`、`0x3333...`、`0x5555...` 这些常量又回到本章开头的物化逻辑：每出现一次 64 位掩码，选指器就要再拼一次。

### `fshl/fshr`：可变循环移位

`fshl` 的展开是：

```console
t_fshl:
	andi	x3, x12, 63
	sll	x3, x10, x3
	srli	x4, x11, 1
	xori	x7, x12, -1
	andi	x7, x7, 63
	srl	x4, x4, x7
	or	x10, x3, x4
```

它计算 `(a << s) | (b >> (64-s))`。注意 `64-s` 没有直接算，而是用 `srli 1; srl (~s & 63)`：先右移 1 位，再右移 `~s & 63` 位，合起来就是 `64-s`，同时天然处理 `s=0`（`~0 & 63 = 63`，`b >> 1 >> 63 = b >> 64` 在 RV64 上是 0）。`fshr` 对称，只是先 `srl b, s` 再 `sll a, 64-s`。

### `abs/smax/smin`：条件选择变成掩码算术

MiniRV 没有 `cmov`，`select` 被 lower 成掩码：

```console
t_abs:
	srai	x3, x10, 63
	xor	x4, x10, x3
	sub	x10, x4, x3

t_smax:
	xor	x3, x10, x11
	slt	x4, x11, x10
	sub	x4, x0, x4
	and	x3, x3, x4
	xor	x10, x11, x3
```

`abs` 的 `mask = x >> 63`（全 0 或全 1），然后 `(x ^ mask) - mask`。`smax` 的 `slt x4, b, a` 产生 0/1，`sub x4,x0,x4` 变成全 0/全 1，再用 `(a^b)&mask ^ b` 选择。`smin` 只是把 `slt` 的操作数反过来。这套序列不需要分支，适合没有条件传送的 target。

### 有 `+zbb` 时

RISC-V 的 Zbb 扩展提供 `cpop/clz/ctz/rev8/min/max`。同一份 IR 加 `-mattr=+m,+zbb`：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m,+zbb -O2 bits.ll -o -
t_pop:
	cpop	a0, a0
	ret
t_lz:
	clz	a0, a0
	ret
t_tz:
	ctz	a0, a0
	ret
t_bswap:
	rev8	a0, a0
	ret
t_abs:
	neg	a1, a0
	max	a0, a0, a1
	ret
t_smax:
	max	a0, a0, a1
	ret
```

`bitreverse` 没有变成单指令：Zbb 的 `rev8` 只是按字节反转，不是 64 位全反转，所以仍然走通用展开。`fshl/fshr` 在这个构建里也仍然展开成移位（没有选到 `rol/ror`），这说明“有扩展”不等于“每条 pattern 都已经写好”。Zba 主要提供 `sh1add/sh2add/sh3add` 这类地址缩放，和本章的 bit intrinsic 关系不大。

MiniRV 没有 Zbb feature，也没有 `cpop` 等记录，所以只能 `Expand`。如果给 MiniRV 加 Zbb，正确做法是：

1. 在 `MiniRVInstrInfo.td` 增加 `CPOP/CLZ/CTZ/REV8` 记录和 `Pat`；
2. 把对应 `setOperationAction` 从 `Expand` 改成 `Legal`；
3. 保留 `Expand` 作为无 Zbb 时的 fallback（通过 `Predicates` 区分）。

这就是“target instruction vs 通用展开”的分界线：**有单指令就加 pattern；没有就让它 Expand，不要在 C++ 里手写 SWAR。**

## 主线：i128 的合法化

### 寄存器对与 ABI

MiniRV 只声明了 `i64` 寄存器类，所以 `i128` 在类型合法化时被拆成两个 `i64`。调用约定里只写了 `i64` 的分配规则，`i128` 拆分后自然落到连续的 GPR：

```
i128 a          i128 b
x10 = a.lo      x12 = b.lo
x11 = a.hi      x13 = b.hi
返回：x10 = r.lo, x11 = r.hi
```

这也解释了为什么 i128 的 libcall 不需要搬参数：`__divti3` 的 ABI 正好也是“两个 i64 参数在 x10..x13，结果在 x10/x11”。

### 加法和减法：`sltu` 进位/借位链

```console
$ $B/llc -mtriple=minirv64 -O2 i128.ll -o -
add128:
	add	x4, x11, x13
	add	x3, x10, x12
	sltu	x7, x3, x10
	add	x11, x4, x7
	addi	x10, x3, 0
	jalr	x0, 0(x1)
sub128:
	sub	x3, x11, x13
	sltu	x4, x10, x12
	sub	x11, x3, x4
	sub	x10, x10, x12
	jalr	x0, 0(x1)
```

加法：

- `x3 = a.lo + b.lo`，这是结果的低 64 位。
- `sltu x7, x3, x10`：无符号比较“低位的和是否小于 a.lo”。小于说明发生了 64 位回绕，即进位 1。这里必须用 `sltu`，不能用 `slt`，因为进位是无符号语义。
- `x4 = a.hi + b.hi`，`x11 = x4 + carry`，这是结果的高 64 位。

减法：

- `x3 = a.hi - b.hi`，`x10 - x12` 是低 64 位。
- `sltu x4, x10, x12`：`a.lo < b.lo` 说明低位不够减，需要从高位借 1。
- `x11 = (a.hi - b.hi) - borrow`。

这两条链就是“用 64 位指令实现 128 位加减”的全部秘密。没有 `addc/subc` 指令，只能靠 `sltu` 把进位/借位变成 0/1。

### 乘法：内联的 64×64 交叉积

```console
mul128:
	mul	x3, x10, x13
	mulhu	x4, x10, x12
	add	x3, x4, x3
	mul	x4, x11, x12
	add	x11, x3, x4
	mul	x10, x10, x12
	jalr	x0, 0(x1)
```

把 128 位乘法写成四个 64 位乘积：

```
a = a.hi*2^64 + a.lo
b = b.hi*2^64 + b.lo

r.lo = low64(a.lo * b.lo)
r.hi = high64(a.lo * b.lo) + a.lo*b.hi + a.hi*b.lo   (mod 2^64)
```

- `mul x3, x10, x13`：`a.lo * b.hi`。
- `mulhu x4, x10, x12`：`a.lo * b.lo` 的高 64 位。
- `mul x4, x11, x12`：`a.hi * b.lo`。
- `mul x10, x10, x12`：`a.lo * b.lo` 的低 64 位。

MiniRV 没有调用 `__multi3`，因为 `setOperationAction({UMUL_LOHI, SMUL_LOHI}, i64, Expand)` 让类型合法化器用 `MUL/MULHU` 拼出 i128 乘法。对比 RISC-V 关闭 M 扩展时，同一个 `mul i128` 会变成：

```console
$ $B/llc -mtriple=riscv64 -O2 i128.ll -o -
mul128:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	call	__multi3
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

`__multi3` 是 compiler-rt/libgcc 的 128×128→128 乘法。RISC-V 有 M 时也会内联成 `mul/mulhu`；MiniRV 永远有 M，所以永远内联。这个对比说明：**同一个 IR 操作，是否变成 libcall 取决于 target 的合法操作集合，而不是 IR 本身。**

### 除法与取余：`__divti3` 家族

```console
sdiv128:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, __divti3
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)

urem128:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, __umodti3
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

RV64 没有 128 位除法，软件实现太长，内联不划算，所以 `SDIV_I128/UDIV_I128/SREM_I128/UREM_I128` 在 `MiniRVSubtarget::initLibcallLoweringInfo` 里映射到：

| RTLIB | 符号 |
|---|---|
| `SDIV_I128` | `__divti3` |
| `UDIV_I128` | `__udivti3` |
| `SREM_I128` | `__modti3` |
| `UREM_I128` | `__umodti3` |

调用点只 spill `ra`，因为参数已经在 x10..x13，返回值在 x10/x11，不需要额外搬移。对象文件里的重定位也证明它不是“调用地址 0”：

```console
$ $B/llvm-readobj -r i128.o
    0x50 R_RISCV_JAL __divti3 0x0
    0x68 R_RISCV_JAL __umodti3 0x0
```

i128 的变量移位走 `SHL_PARTS/SRL_PARTS/SRA_PARTS`，MiniRV 把它们标成 `Expand`；以 `lshr4` 为例，低半右移 4、高半左移 60 再 `or`，和加减乘除一样由类型合法化器拼出来。

## 三类实现：target 指令、combine、libcall

这是本章最该带走的一张表：

| 层级 | 是什么 | 谁来做 | MiniRV 例子 | 什么时候选 |
|---|---|---|---|---|
| target instruction | ISA 里真实存在的单条指令 | TableGen `Pat` + `SelectCode` | `add → ADDI`、`+zbb → CPOP` | 有指令，且语义/立即数/寄存器约束能表达 |
| DAG combine / 通用展开 | 用已有节点拼出的等价序列 | `LegalizeDAG` 的 `Expand`，或 target 的 `performDAGCombine`/`LowerOperation` | `ctpop`、`bswap`、`abs`、i128 add | 没有单指令，但序列短、通用展开够好 |
| libcall | 调用运行时函数 | `LibcallLoweringInfo` + `RTLIB` | `__divti3`、`__multi3`（无 M）、`__addsf3` | 软件实现大、代码尺寸敏感、ABI 已标准化 |

判断顺序可以固定成四问：

1. **ISA 有没有一条指令？** 有就加 pattern，标 `Legal`；不要把指令能做的事写成 C++。
2. **没有指令，但能用已有节点拼出短序列？** 标 `Expand`，让合法化器做；如果通用展开质量差，再标 `Custom` 并在 `LowerOperation` 里写 target 版本。
3. **序列很长，或需要跨函数共享？** 选 libcall；在 `initLibcallLoweringInfo` 里注册 `RTLIB` 名字，否则会得到 “no libcall available”。
4. **这个操作是否值得 target 特殊优化？** 用 combine 改写成更便宜的形状（例如 `add(x, C1), C2` 合并），但 combine 必须保证语义等价，不能改变 trapping/poison 行为。

常量物化比较特殊：它没有对应的 IR 操作，也不是 combine，而是**选指期的叶子节点处理**。MiniRV 把它放在 `Select` 的 `ISD::Constant` 分支；如果 target 有 `PseudoMovImm`，更合适的做法是生成可重物化的伪指令，让寄存器分配器在需要时再展开。
## 实测：llvm-lit

```console
$ $B/llvm-lit -v llvm/test/CodeGen/MiniRV/{constants,bit-intrinsics,i128,i128-div}.ll
  Passed: 4 (100.00%)
```

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 常量选指 | `MiniRVISelDAGToDAG.cpp` | `MiniRVDAGToDAGISel::Select`、`BuildWord` | `ISD::Constant` 分支 |
| 指令记录 | `MiniRVInstrInfo.td` | `simm12`、`uimm20`、`ADDI/LUI/SLLI/OR` | pattern 与立即数约束 |
| 汇编输出 | `MiniRVAsmPrinter.cpp` / `MiniRVMCInstLower.cpp` | `emitInstruction`、`Lower` | 真实指令与 `MO_Immediate` |
| 位运算 | `MiniRVISelLowering.cpp` | `setOperationAction` | `CTPOP/CTLZ/... = Expand` |
| i128 | `MiniRVSubtarget.cpp` | `initLibcallLoweringInfo` | `__divti3` 注册 |

## 反直觉与踩雷

1. **错误直觉**：`lui` 只是把 20 位立即数放到高 20 位，不会影响低 32 位以上。  
   **正确认识**：RV64 的 `lui` 把 32 位结果**符号扩展**到 64 位。`lui x3, 0x80000` 得到的是 `0xffffffff80000000`，不是 `0x0000000080000000`。MiniRV 对 `0x9abcdef0` 这类 bit31 为 1 的字，必须用 `slli 32; srli 32` 清高半；对正数也不能想当然。

2. **错误直觉**：`li` 是一条 RV64 指令，后端直接选它就行。  
   **正确认识**：`li` 是汇编器伪指令，编码层不存在。RISC-V 的 `PseudoLI` 只在 AsmParser 里展开；MiniRV 连这个伪指令都没有，常量必须在 `Select` 里拆成 `LUI/ADDI/SLLI/OR`。判断一个名字是不是指令，看它有没有 4 字节编码，而不是看汇编能不能写。

3. **错误直觉**：`ctpop` 这种 intrinsic 一定调用 `__popcountdi2`。  
   **正确认识**：LLVM 默认用 SWAR + 乘法展开；只有 target 明确把它 lower 成 libcall，才会出现运行时调用。MiniRV 标 `Expand`，所以生成的是二十多条整数指令；`+zbb` 才有 `cpop` 单指令。

4. **错误直觉**：i128 的加法和乘法都应该调用 `__addti3`/`__multi3`。  
   **正确认识**：加法只是 `add + sltu + add`，乘法是四个 64 位 `mul/mulhu` 加两次 `add`；只有除法/取余这种“软件实现太长”的操作才走 `__divti3` 家族。是否 libcall 由 target 的合法操作和代价决定。

5. **错误直觉**：MiniRV 的常量合成对所有 64 位值都正确。  
   **正确认识**：当前树（`6e7e7ab`）的 `BuildWord` 语义是“零扩展 32 位”，但 `Select` 用 `isInt<32>(Val) || isUInt<32>(Val)` 把负的 32 位值也送进了这条路径。实测 `ret i64 -2049` 生成：

   ```console
   lui	x3, -1
   addi	x3, x3, 2047
   slli	x3, x3, 32
   srli	x10, x3, 32
   ```

   它返回 `0x00000000fffff7ff`（4294965247），而不是 `0xfffffffffffff7ff`（-2049）。同样，`0x7fffffff` 因为 `lui 0x80000` 的符号扩展、又没有清零，会得到 `0xffffffff7fffffff`。`constants.ll` 只钉住了 `-2048` 和 `-2147483649`，这两个恰好一个走 `simm12`、一个靠高字 `or` 修正，所以测试是绿的。复现方式：写两个 `noinline` 函数，一个 `ret i64 -2049`，另一个用运行时参数算 `x - 2049`，在 qemu 上比较，MiniRV 退出码 1，RISC-V 参考退出码 0。修法不是改测试，而是让 `BuildWord` 明确区分“零扩展 32 位”和“符号扩展 64 位”，或者对 `isInt<32>` 的负值走 64 位双字路径。

## 进阶：再往深一层

- **重物化（rematerialization）**：RISC-V 的 `PseudoMovImm` 让常量在寄存器分配后还能重新展开，减少 spill。MiniRV 直接在选指期展开，常量值一旦被 spill 就要占栈槽。给 MiniRV 加一个带 `isReMaterializable` 的伪指令，是下一步很自然的优化。
- **常量池**：RISC-V 对大到不划算的常量用 `.rodata.cst8` + `lui/ld` 换代码尺寸；MiniRV 没有这条路径，可在 `Select` 里按序列长度决定是否引用常量池。
- **代价模型**：`RISCVMatInt` 还看 RVC 压缩后的字节数和执行代价；MiniRV 的 `BuildWord` 只看“能不能放下”。加入 `addiw`、`sh1add` 或 Zbb 指令后，应把序列长度/字节数/延迟做成显式 cost 函数。

## 本章重点整理

- RV64 没有 `li` 指令；32 位常量用 `lui + addi`，拆分时 `hi=(V+0x800)>>12`、`lo=V-(hi<<12)`。
- RV64 的 `lui` 符号扩展 32 位结果；零扩展的 32 位模式需要 `slli 32; srli 32`。
- 64 位常量 = 低字 + 高字 `slli 32` + `or`；两个字都可能各自需要 `lui/addi` 和清零。
- MiniRV 没有 `selectImm/materializeConstant/PseudoMovImm`；常量物化在 `MiniRVDAGToDAGISel::Select` 的 `ISD::Constant` 分支，`PseudoLA` 只用于符号地址。
- `ctpop/ctlz/cttz/bswap/bitreverse` 在 MiniRV 上是 `Expand`；`+zbb` 才能选到 `cpop/clz/ctz/rev8`。
- `fshl/fshr` 用两次移位 + `or`，`64-s` 通过 `~s & 63` 避免额外减法。
- `abs/smax/smin` 用掩码算术，不依赖条件传送。
- i128 加减用 `sltu` 进位/借位链；乘法用四个 64 位交叉积；除法/取余走 `__divti3` 家族。
- 选择实现层级：有指令用 pattern，短序列用 Expand/combine，长实现用 libcall；不要把所有东西都塞进 C++。

## 自我检核
- [ ] 不看笔记，写出 `V=3968` 的 `hi/lo` 计算，并解释为什么 `lo` 是负数。
- [ ] 说出 MiniRV 的常量物化在哪个函数、哪个 `ISD` 分支；为什么它没有 `PseudoMovImm`。
- [ ] 解释 `ctpop` 展开中三个 64 位掩码分别是什么，最后为什么用 `mul` 和 `srli 56`。
- [ ] 解释 `fshl` 如何用 `srli 1; srl (~s & 63)` 计算 `b >> (64-s)`。

## 延伸阅读

- **[LLVM Language Reference — 位运算与饱和 intrinsic](https://llvm.org/docs/LangRef.html)** — LLVM Project
  - **读哪里**：`llvm.ctpop`、`llvm.ctlz`、`llvm.cttz`、`llvm.bswap`、`llvm.bitreverse`、`llvm.fshl`、`llvm.fshr`、`llvm.abs`、`llvm.smax/smin` 的语义、poison 规则和参数。
  - **能学到什么**：为什么 `ctlz(x, false)` 的零输入结果有定义，而 `true` 会产生 poison；这些规则决定了后端能不能安全地做零分支优化。
  - **前提**：读过 Ch 13 的 SelectionDAG 节点和 Ch 12 的合法化；否则会把 IR 语义和后端展开混在一起。

- **[LLVM Code Generator — TableGen patterns and instruction selection](https://llvm.org/docs/CodeGenerator.html)** — LLVM Project
  - **读哪里**：Instruction Selection、Pattern Matching、Immediate operands 相关章节。
  - **能学到什么**：`Pat`、`ImmLeaf`、`ComplexPattern` 各自能表达什么；为什么 `simm12` 既可以是 operand 又可以是 matcher。
  - **前提**：能读懂 `MiniRVInstrInfo.td` 里的 `def simm12` 和 `def : Pat<(i64 (add ...))>`。

- **[RISC-V ELF psABI — Integer Calling Convention](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：integer calling convention、aggregate 传递、soft-float ABI 的寄存器分配。
  - **能学到什么**：为什么 i128 会拆成两个 GPR、为什么 soft-float 的 f32/f64 走整数寄存器；这些不是 LLVM 随意决定的，而是 ABI 合同。
  - **前提**：知道 RV64 的 x10..x17 是参数寄存器，x10/x11 是返回值寄存器。

→ [Ch 20 软浮点与 libcall](./20-soft-float-and-libcalls.md)
