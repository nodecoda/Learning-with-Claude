# Ch 17 — LSX/LASX 向量 lowering：`v*` 与 `xv*` 的同一套逻辑

> 本章目标：理解 LSX（128 位）与 LASX（256 位）共享一套 lowering；能解释一个常量向量的三种生成方式（`vrepli` / 常量池 `vld` / 插入链）；并知道 shuffle 为什么经常要先从内存取 mask。

---

## 17.1 特性与类型

`LoongArch.td:59`：

```tablegen
def FeatureExtLSX : SubtargetFeature<"lsx", "HasExtLSX", "true",
                                     "'LSX' (Loongson SIMD Extension)", [FeatureBasicD]>;
def FeatureExtLASX : SubtargetFeature<"lasx", "HasExtLASX", "true",
                                      "'LASX' (Loongson Advanced SIMD Extension)",
                                      [FeatureExtLSX]>;
```

依赖链：**LASX → LSX → BasicD**。所以 `-mattr=+lasx` 自动带 `+lsx`；`-mattr=-lsx` 会同时关掉 LASX（feature 依赖是单向的，关父不关子——实际上 TableGen 会报不一致，所以别这么写）。

寄存器类（`LoongArchRegisterInfo.td:204`）：

| 类 | 位宽 | 类型 |
|---|---|---|
| `LSX128` | 128 | `v16i8` / `v8i16` / `v4i32` / `v2i64` / `v4f32` / `v2f64` |
| `LASX256` | 256 | `v32i8` / `v16i16` / `v8i32` / `v4i64` / `v8f32` / `v4f64` |

LASX 的 lowering **大量复用 LSX**：同一个 `lowerVECTOR_SHUFFLE` 里按类型宽度分支，指令名从 `v*` 换成 `xv*`。这就是为什么 `LSXInstrInfo.td`（3500 行）和 `LASXInstrInfo.td`（3457 行）体量几乎一样——它们是镜像的。

---

## 17.2 入口函数

`LowerOperation` 的向量分支：

| 操作 | 函数 | 行号 |
|---|---|---|
| `BUILD_VECTOR` | `lowerBUILD_VECTOR` | `:652` |
| `VECTOR_SHUFFLE` | `lowerVECTOR_SHUFFLE` | `:3596` |
| `INSERT_VECTOR_ELT` | `lowerINSERT_VECTOR_ELT` | `:648` |
| `EXTRACT_VECTOR_ELT` | `lowerEXTRACT_VECTOR_ELT` | `:650` |
| `CONCAT_VECTORS` | `lowerCONCAT_VECTORS` | `:654` |
| `SCALAR_TO_VECTOR` | `lowerSCALAR_TO_VECTOR` | `:1584` |
| `VECREDUCE_ADD` | `lowerVECREDUCE_ADD` | `:1083` |

注意 `VECREDUCE_AND/OR/XOR/SMAX/...` 也在 `LowerOperation` 里（`:680-686`），但只有 `ADD` 有独立的 `lowerVECREDUCE_ADD`，其余走通用展开或 `VECREDUCE_*` 的组合。

---

## 17.3 `BUILD_VECTOR` 的三种生成方式

### 全零 → `vrepli.b 0`

```llvm
ret <4 x i32> zeroinitializer
```

```text
	vrepli.b	$vr0, 0
```

### splat → `vrepli.<elt>`

```llvm
ret <4 x i32> <i32 7, i32 7, i32 7, i32 7>
```

```text
	vrepli.w	$vr0, 7
```

### 任意常量 → 常量池 + `vld`

```llvm
ret <4 x i32> <i32 1, i32 2, i32 3, i32 4>
```

```text
	.section	.rodata.cst16,"aM",@progbits,16
.LCPI0_0:
	.word	1
	.word	2
	.word	3
	.word	4
	.text
bv:
	pcalau12i	$a0, %pc_hi20(.LCPI0_0)
	vld	$vr0, $a0, %pc_lo12(.LCPI0_0)
	ret
```

**为什么不用 `vldi`**：`vldi` 的 13 位立即数只能编码有限的常量模式（类似 `vldi` 的“位模式 + 移位”），`1,2,3,4` 这种每个 lane 不同的值不在其中。`vrepli` 要求所有 lane 相同。两者都不满足时，只能走常量池。

**这也是一个优化机会**：如果某个常量向量能被 `vldi` 编码却没被识别，就会多一次内存访问。`lowerBUILD_VECTOR` 里对 `vldi` 的匹配逻辑是值得读的部分。

---

## 17.4 shuffle：mask 常常要先从内存取

```llvm
%r = shufflevector <4 x i32> %a, <4 x i32> %b, <4 x i32> <i32 0, i32 5, i32 2, i32 7>
```

```text
	.section	.rodata.cst16,"aM",@progbits,16
.LCPI1_0:
	.word	0
	.word	5
	.word	2
	.word	7
	.text
shuf:
	pcalau12i	$a0, %pc_hi20(.LCPI1_0)
	vld	$vr2, $a0, %pc_lo12(.LCPI1_0)
	vshuf.w	$vr2, $vr1, $vr0
	vori.b	$vr0, $vr2, 0
	ret
```

三个观察：

1. **mask 是内存里的常量向量**，不是立即数——`vshuf.w` 的三个操作数是 `vd, vj, vk`，其中 `vk` 是 mask 寄存器。
2. **`vori.b $vr0, $vr2, 0` 是一条“寄存器复制”**：`vshuf.w` 的结果写在 `$vr2`（因为 `$vr2` 被 mask 占用），需要搬到返回值 `$vr0`。LoongArch 没有向量 `move`，用 `vori.b x, y, 0` 代替。
3. **`vshuf.w` 的第三个操作数是索引向量**（运行时值），不是立即数；`shufflevector` 的 mask 是编译期常量，所以必须先从常量池 `vld` 进寄存器。

`lowerVECTOR_SHUFFLE()` 里有一长串特化：`lowerVECTOR_SHUFFLEAsShift`、`AsByteRotate`、`AsZeroOrAnyExtend`、`VPACKEV/VPACKOD`、`VILVH/VILVL`、`VPICKEV/VPICKOD`、`VPERMI`……每一种都对应一类能被单条指令表达的 mask 模式。**这就是 3500 行 `.td` + 上千行 lowering 的主要来源。**

---

## 17.5 reduction：`vhaddw` 链

```llvm
%r = call i32 @llvm.vector.reduce.add.v4i32(<4 x i32> %a)
```

```text
red:
	vhaddw.d.w	$vr0, $vr0, $vr0
	vhaddw.q.d	$vr0, $vr0, $vr0
	vpickve2gr.d	$a0, $vr0, 0
	ret
```

`vhaddw` 是“水平加宽”：把相邻两 lane 相加并加宽到下一宽度。

- `vhaddw.d.w`：4×i32 → 2×i64（两两相加）
- `vhaddw.q.d`：2×i64 → 1×i128（再相加）
- `vpickve2gr.d`：取 lane 0 到 GPR

三次操作完成 4 元素归约。`lowerVECREDUCE_ADD()`（`:1083`）会根据元素类型和向量宽度选不同的加宽链。

---

## 17.6 LASX：同一套逻辑，`x` 前缀

```llvm
define <8 x i32> @xv(<8 x i32> %a) { %r = add <8 x i32> %a, %a
  ret <8 x i32> %r }
```

```console
$ llc -mattr=+lasx
xv:
	xvadd.w	$xr0, $xr0, $xr0
	ret
```

`vadd.w` → `xvadd.w`，操作数从 `$vr` 变 `$xr`。**没有第二套 lowering**；`lowerVECTOR_SHUFFLE` 等函数里用 `VT.getSizeInBits() == 256` 分支，或者用 `Subtarget.hasExtLASX()` 选 opcode。

这也意味着：**给 LSX 修 bug 时，要同时检查 LASX 分支**。很多上游补丁的标题是 `[LoongArch] Fix LSX/LASX ...`，原因就在这里。

---

## 17.7 反直觉点

1. **LASX 依赖 LSX 依赖 BasicD**，不是三个独立 feature。
2. **`vshuf.w` 的 mask 必须是寄存器**，常量 mask 要从内存 `vld`。
3. **`vori.b x, y, 0` 是向量 move**，不是“或 0”那么简单（它复制整个向量）。
4. **`vrepli` 和 `vldi` 能覆盖的常量模式不同**，都不满足才走常量池。
5. **reduction 不是一条指令**，是 `vhaddw` 链 + `vpickve2gr`。
6. **LSX/LASX 的 `.td` 几乎镜像**，改一处要同步另一处；但 lowering 是共享的，改 lowering 要测两种宽度。
7. **`-mattr=+lasx` 会自动带 `+lsx`**，但 `-mattr=-lsx,+lasx` 是非法组合，TableGen 会报错。

---

## 17.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| feature 依赖 | `LoongArch.td:59-68` |
| 寄存器类 | `LoongArchRegisterInfo.td:204,214` |
| 向量 lowering 入口 | `LoongArchISelLowering.cpp:648-686` |
| `lowerVECTOR_SHUFFLE` | `:3596` |
| `lowerVECREDUCE_ADD` | `:1083` |
| 常量向量三种方式 | 本章 17.3 实测 |
| shuffle 实测 | 本章 17.4 |
| reduction 实测 | 本章 17.5 |
| LASX 实测 | 本章 17.6 |
| `.td` 规模 | Ch 01 的文件统计 |

---

## 17.9 练习

1. 找一个能用 `vldi` 编码的常量向量（提示：看 `LoongArchLSXInstrInfo.td` 里 `vldi` 的 pattern），验证它没有走常量池。
2. 构造一个 `shufflevector` 的 mask，让 `lowerVECTOR_SHUFFLEAsShift` 命中，观察生成的 `vslli`/`vsrli`。
3. 用 `<8 x i32>` 重复 17.5 的 reduction，确认走 `xv*` 指令。
4. 读 `lowerVECREDUCE_ADD`，解释 `v8i16` 和 `v4i32` 的加宽链有什么不同。
5. 把 17.6 的 `+lasx` 改成 `+lsx`，观察 256 位向量如何被拆成两条 128 位指令。

---

## 17.10 下一步

Part 3 结束。**练习 C** 会让你写一个向量选指/combine 的小补丁并验证。之后进入 Part 4：机器层 pass（伪指令展开、`MergeBaseOffset`、`OptWInstrs`、分支优化、原子、调度）。
