# Ch 10 — 指令选择：DAG 到 PTX 指令

> **目标**：能看懂 NVPTX 的 TableGen 模式，知道 `mad.lo`、`mul.wide`、`cvta`、`setp` 这些指令是怎么被「选」出来的，并能解释 `-O0` 与 `-O2` 的差异。

## 为什么需要这个？

指令选择是后端最像「编译器」的部分：同一段 IR 可以选出完全不同的指令序列。NVPTX 的 ISel 有两个特殊之处：

1. **不用 GlobalISel**：整个 target 只有 SelectionDAG。
2. **模式数量大**：`NVPTXIntrinsics.td` 有 588 个 `def`，`NVPTXInstrInfo.td` 有 397 个，绝大多数是 intrinsic 与特殊指令。

## 先建立直觉

```
IR:  %i = mul i32 %a, %b
     %j = add i32 %i, %c
          │
          ▼  DAG combine
     (add (mul a b) c)
          │
          ▼  模式匹配（TableGen 生成）
     mad.lo.s32 %r, a, b, c
```

模式匹配就是「找形状」：`(add (mul x y), z)` 这个形状对应 `mad`。如果形状不匹配（例如 `mul` 的结果还有别的用户），就只能选 `mul` + `add`。

## 一、ISel 的入口

`NVPTXISelDAGToDAG.cpp`（2455 行）继承 `SelectionDAGISel`，核心是：

- `Select(SDNode *N)`：按 opcode 分派。
- TableGen 生成的 matcher：处理绝大多数算术/访存模式。
- 手写 `SelectADDR`：把地址操作数拆成 base + offset（第 1195 行）。
- 手写 `SelectSETP_F16X2` / `SelectSETP_BF16X2`：半精度成对比较的特殊处理。

## 二、TableGen 模式长什么样

以 `mul.wide` 为例（`NVPTXInstrInfo.td` 原文节选）：

```tablegen
multiclass MULWIDEInst<string suffix, SDPatternOperator op, RegTyInfo big_t, RegTyInfo small_t> {
  def suffix :
    BasicNVPTXInst<(outs big_t.RC:$dst), (ins small_t.RegOrImm:$a, small_t.RegOrImm:$b),
                   "mul.wide." # suffix>;
  def : Pat<(big_t.Ty (op small_t.Ty:$a, small_t.Ty:$b)),
            (!cast<Instruction>(NAME # suffix) small_t.ImmNode:$a, small_t.ImmNode:$b)>;
}
defm MUL_WIDE : MULWIDEInst<"s32", smul_wide, I64RT, I32RT>;
```

读法：

- `MUL_WIDE_S32` 的输出是 64 位（`I64RT`），输入是两个 32 位（`I32RT`）。
- 模式 `(op a, b)` 里的 `op` 是自定义 DAG 节点 `NVPTXISD::MUL_WIDE_SIGNED`，不是普通 `mul`。
- 这个节点由 DAG combine 从「sext + mul」或「GEP 索引」生成。

`mad` 的模式更有意思：

```tablegen
def : Pat<(big_t.Ty (add (OneUse2<op> small_t.Ty:$a, small_t.Ty:$b), big_t.Ty:$c)),
          (!cast<Instruction>(NAME) ...)>;
```

`OneUse2` 是关键：只有当 `mul` 的结果**只被这个 add 使用**时，才能折成 `mad`。否则 `mul` 还要留给别的用户。

## 三、`-O0` 与 `-O2` 的四组实测差异

同一份 `add.ll`：

| 形状 | `-O0` | `-O2` |
|---|---|---|
| 索引计算 | `mul.lo.s32` + `add.s32` | `mad.lo.s32` |
| 地址计算 | `cvt.s64.s32` + `shl.b64` | `mul.wide.s32` |
| 访存 | `ld.b32`（generic） | `cvta.to.global` + `ld.global.b32` |
| 分支 | `bra.uni` + 额外块 | 直接 `@%p bra` |

`-O0` 的原始片段：

```ptx
mul.lo.s32 	%r6, %r3, %r5;
add.s32 	%r1, %r6, %r2;
```

`-O2`：

```ptx
mad.lo.s32 	%r5, %r3, %r4, %r2;
```

这不是「两条变一条」那么简单：`mad` 在 GPU 上是原生融合指令，吞吐和延迟都更好。**但前提是形状匹配**。

## 四、特殊寄存器的选择

```tablegen
defm INT_PTX_SREG_TID : PTX_READ_SREG_R32V4<"tid">;
defm INT_PTX_SREG_NTID : PTX_READ_SREG_R32V4<"ntid">;
defm INT_PTX_SREG_CTAID : PTX_READ_SREG_R32V4<"ctaid">;
```

`PTX_READ_SREG_R32V4` 是一个 multiclass，一次生成 `.x/.y/.z/.w` 四个变体。所以：

```llvm
%t = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
```

直接匹配成：

```ptx
mov.u32 	%r2, %tid.x;
```

这类 intrinsic 的模式匹配是「一对一」的：IR intrinsic → PTX 指令，没有额外 lowering。

## 五、地址选择：`SelectADDR`

`SelectADDR(SDValue Addr, SDValue &Base, SDValue &Offset)` 把地址拆成：

- **Base**：寄存器或符号。
- **Offset**：立即数偏移。

于是 `[%rd9+124]`、`[add_param_0+4]` 这种形式才可能出现。拆不出来时，会退回「先算地址、再访存」的两条指令。

## 六、为什么 NVPTX 没有 GlobalISel

GlobalISel 需要大量 legalizer/selector 规则，收益主要在编译速度和可维护性。NVPTX 的指令集是虚拟的、模式又高度依赖 intrinsic，维护两套 ISel 不划算。源码里 `grep -ri globalisel llvm/lib/Target/NVPTX` 为空，可以作为事实核对。

## 踩雷集锦

1. **以为 `mul`+`add` 一定变 `mad`**：`OneUse2` 不满足时不会折。
2. **以为 `mad.wide` 总是更好**：源码注释明确说它会带来回归，只在用户显式开启时才生成（`MAD_WIDE_*` 带 `doMADWideOpt` 谓词）。
3. **在 `-O0` 下期待 `cvta.to.global`**：地址空间特化是优化，不是正确性要求。
4. **手写 IR 用普通 `mul` 却期待 `mul.wide`**：wide 变体来自 `NVPTXISD` 自定义节点，需要 DAG combine 触发。
5. **忽略 immediate 与寄存器的区别**：`SelectADDR` 能折进立即数时，指令数会少一条。

## 进阶：一条 `mad.wide` 的诞生

`MAD_WIDE_S32` 的形状是 `(add (mul_wide a, b), c)`：

- `mul_wide` 来自 `sext i32 → i64` 后的乘法（常见于 GEP 索引）。
- 只有当乘法的 64 位结果只被这个加法使用时，才折成 `mad.wide.s32`。
- 源码注释提到它曾在某些场景造成回归，所以默认关闭。

这说明一件事：**指令选择不只是「能匹配就匹配」，还要有性能判断**。自研 GPU 后端必须为每条融合指令准备这样的开关与回归测试。

## 动手练习

1. 把 `add.ll` 里的 `mul` 结果再多用一个（例如同时存到另一个指针），观察 `mad` 是否消失。
2. 用 `grep -n "OneUse2" NVPTXInstrInfo.td` 找出所有依赖单用的模式。
3. 写一个 GEP 索引来自 `sext i32` 的例子，确认 `mul.wide.s32`。
4. 用 `-nvptx-mad-wide-opt=true` 对比 `mad.wide` 的生成（选项名来自 `NVPTXOptions.td`）。

## 本章重点整理

- NVPTX 只有 SelectionDAG，没有 GlobalISel。
- TableGen 模式 + 自定义 DAG 节点 + 手写 `SelectADDR` 构成 ISel。
- `mad`/`mul.wide` 依赖形状与单用条件；不是无条件融合。
- `-O0`/`-O2` 的差异主要来自 DAG combine 与地址空间特化。

## 自我检核

- [ ] 能读懂一个 `Pat<...>` 并说出它的匹配形状
- [ ] 能解释 `OneUse2` 为什么存在
- [ ] 知道 `mul.wide` 由哪种 IR 形状产生
- [ ] 能说出 `SelectADDR` 拆出的两个部分

## 延伸阅读

- **[LLVM TableGen Backends](https://llvm.org/docs/TableGen/BackEnds.html)** — LLVM 官方
  - 读「Instruction Selection Patterns」一节；本章的 `Pat` 语法在这里有完整说明。
- **[LLVM SelectionDAG](https://llvm.org/docs/CodeGenerator.html#selectiondag)** — LLVM 官方
  - 读「DAG Combining」与「Instruction Selection」两节。
- **[PTX ISA: Integer Instructions](https://docs.nvidia.com/cuda/parallel-thread-execution/#integer-instructions)** — NVIDIA
  - 查 `mad`、`mul.wide` 的语义与溢出规则。

下面是练习 B：把三条产生 PTX 的路径放在一起对拍，训练你「看到差异就能定位原因」的能力。

→ [练习 B：三路对拍（手写 IR / clang / nvcc）](./practice-b-three-way-diff.md)
