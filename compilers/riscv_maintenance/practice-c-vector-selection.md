# 练习 C — 诊断向量指令选择：为什么没选中我想的那条

> **目标**：给定“我期望 `vadd.vv`，编译器却给了别的”这类现象，能用一套固定流程定位它发生在哪一层，并用最小证据说明是 bug 还是预期行为；最后能指出该改哪个文件、补哪个 lit。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，只读），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）。本章每个“实测”都来自这棵树 + 这个构建，命令均可直接复现。

> **前置**：读完 Ch 13（Legalization）、Ch 14（SelectionDAG pattern）、Ch 17（RVV lowering）。本章不重复 LMUL/`vsetvli` 的定义，只练“怎么查”。

---

## C.1 为什么“没选中”是最常见的维护问题

RVV 的一条指令不是一次匹配的产物，而是**五个层次**的合取；auto-vectorization、intrinsic/pattern、LMUL/SEW、mask、VL 任何一项把条件改掉，最终症状都只是同一句话：“汇编不对”。

| 层 | 决策 | 典型“落空” | 观察点 |
|---|---|---|---|
| 前端 | auto-vectorization 是否发生、VF 多大 | 根本没向量化 | `-Rpass*` |
| 类型/DAG | 合法类型、fixed→scalable container | LMUL 不对、多出 convert | `-stop-after=finalize-isel`、`-debug-only=legalize-types` |
| 选指 | `.td` pattern 或 `llvm.riscv.*` intrinsic | 选成另一条 pseudo、`.vi`/`.vv` 形态不同 | `-debug-only=isel` |
| MI 优化 | `RISCVVLOptimizer`、`RISCVVectorPeephole` | 该无 mask 却有 mask、VL 没收敛 | `-stop-before/after=` |
| 配置 | `vsetvli` 插入、mask/policy | `v0.t` 出现、policy 不是 `ta,ma` | `-stop-after=riscv-insert-vsetvli` |

维护者最容易犯的错，是**跳过“是不是 bug”这一步**。本树的向量选择里有大量“故意不按直觉走”的策略，最典型的是 `b[i] << 1` 被选成 `vadd.vv v8, v8, v8` 而不是 `vsll.vi`——源码注释写得很直白：`// Emit shift by 1 as an add since it might be faster.`（`RISCVInstrInfoVPseudos.td:6859`、`RISCVInstrInfoVVLPatterns.td:2165`）。把这种当 bug 去“修”，reviewer 会直接打回。

所以诊断的第一步永远是两个问题：

1. **它在哪一层偏离了预期？**（用下面这棵树）
2. **偏离是策略还是缺陷？**（用 Ch 17 的 lowering 规则 + 源码注释判断）

---

## C.2 先建立直觉：一条诊断决策树

把“期望 vs 实际”按**从上游到下游**的顺序切开。越靠上的层，修复成本越低，也越不该在后端硬改。

```
期望 vadd.vv 实际得到标量循环
  ├─ 循环没向量化?  → -Rpass=loop-vectorize / -Rpass-missed=loop-vectorize
  ├─ 向量化了但类型不合法? → -debug-only=legalize-types / -stop-after=finalize-isel
  ├─ 选成了 Pseudo 但没变真实指令? → -stop-after=riscv-vector-peephole
  ├─ LMUL/SEW 不对? → -mattr=+v,-zvl128b / -riscv-v-vector-bits-min
  └─ 有 mask 却期望无 mask? → vsetvli policy / vmerge
```

三条使用规则：

- **自上而下**：上面的层没排除掉，不要看下面的层。循环都没向量化，去读 `PseudoVADD` 的 pattern 是浪费时间。
- **每层都要留下可复现证据**：一条命令 + 一段输出，而不是“我觉得”。
- **先定位“边界”再定位“行”**：先知道是 pattern 还是 peephole，再 `grep -n` 到具体行。

---

## C.3 案例一：期望 `vadd.vv`，实际标量循环

**输入**（`dep.c`，带真实循环携带依赖）：

```c
void f(int *a, const int *b, int n) {
  for (int i = 1; i < n; i++) a[i] = a[i-1] + b[i];
}
```

**诊断命令**：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/clang --target=riscv64 -march=rv64gcv -mabi=lp64d -O2 -c -o /dev/null \
  -Rpass=loop-vectorize -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize dep.c
```

**实测输出**（节选）：

```text
dep.c:2:36: remark: loop not vectorized: unsafe dependent memory operations in loop. Use #pragma clang loop distribute(enable) to allow loop distribution to attempt to isolate the offending operations into a separate loop
Backward loop carried data dependence. Memory location is the same as accessed at dep.c:2:38 [-Rpass-analysis=loop-vectorize]
dep.c:2:3: remark: loop not vectorized [-Rpass-missed=loop-vectorize]
```

**确认 IR 真的没有向量**：

```bash
$T/bin/clang --target=riscv64 -march=rv64gcv -mabi=lp64d -O2 -S -emit-llvm -o - dep.c | grep -c '<vscale x'
```

实测输出 `0`——一个 `<vscale x N x i32>` 运算都没有；`for.body` 仍是 `phi` + `load` + `add` + `store` 的标量循环。（函数属性里另有 `vscale_range(2,1024)`，但它不含 `<vscale x` 这个子串，用 `grep vscale` 会数到它，别被误导。）

**读法**：`-Rpass-missed` 给的是**结论**，`-Rpass-analysis` 给的是**原因**。这里原因是 `Backward loop carried data dependence`，属于 loop vectorizer 的正确拒绝。对照组：把循环体换成 `a[i] = b[i] + c[i]` 且三个指针都 `restrict`，同一个命令会输出：

```text
c1.c:2:3: remark: vectorized loop (vectorization width: vscale x 4, interleaved count: 1) [-Rpass=loop-vectorize]
```

（节选，省略 caret 行。）

<details>
<summary>诊断结论与修复方向（点开）</summary>

- **结论**：不是 RVV 后端问题，是循环确实有依赖。属于**预期行为**。
- **先看哪层**：前端 auto-vectorization，不进后端。
- **若这是 bug**（你确信依赖不存在，比如两个指针实际不别名）：问题在依赖分析，改 `llvm/lib/Analysis/LoopAccessAnalysis.cpp`，或先加 `#pragma clang loop distribute(enable)` 绕过。
- **lit 落点**：`llvm/test/Transforms/LoopVectorize/RISCV/`；对照用例参照 `epilogue-strided-access.ll`。
</details>

---

## C.4 案例二：期望 `m1`，实际 `m2`

**输入**（`lmul.ll`，固定长度 `<8 x i32>`，没有 `vscale`）：

```llvm
target triple = "riscv64-unknown-linux-gnu"
define void @f(ptr %a, ptr %b) {
  %va = load <8 x i32>, ptr %a, align 4
  %vb = load <8 x i32>, ptr %b, align 4
  %r = add <8 x i32> %va, %vb
  store <8 x i32> %r, ptr %a, align 4
  ret void
}
```

**两条命令，两种 LMUL**：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v lmul.ll -o - | grep -E 'vsetivli|vadd'
$T/bin/llc -mtriple=riscv64 -mattr=+v -riscv-v-vector-bits-min=256 lmul.ll -o - | grep -E 'vsetivli|vadd'
```

**实测输出**：

```text
vsetivli zero, 8, e32, m2, ta, ma          # -mattr=+v
vadd.vv  v8, v8, v10

vsetivli zero, 8, e32, m1, ta, ma          # -riscv-v-vector-bits-min=256
vadd.vv  v8, v8, v9
```

**为什么**：`+v` 隐含 `zvl128b`，可以从 `.attribute 5` 直接看到：

```text
.attribute 5, "rv64i2p1_..._v1p0_..._zvl128b1p0_zvl64b1p0_zvl32b1p0"
```

固定长度向量先被塞进一个 **container type**：`ceil(8 x 32 / VLEN_min=128) = 2` → m2。把最小 VLEN 提到 256，`ceil(8 x 32 / 256) = 1` → m1。落点是 `RISCVISelLowering.cpp:3383` 的 `getContainerForFixedLengthVector`，VLEN 来自 `RISCVSubtarget.h:271` 的 `getRealMinVLen()`。

**顺带纠正一个常见误解**：scalable 类型的 LMUL 不是 `元素数`，而是由 `RVVBitsPerBlock == 64` 决定。`<vscale x 4 x i32>` 在 VLEN=128（vscale=2）上是 m2，`<vscale x 2 x i32>` 才是 m1：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v intr.ll -o - | grep -E 'vsetvli|vadd'   # nxv4i32
$T/bin/llc -mtriple=riscv64 -mattr=+v intr2.ll -o - | grep -E 'vsetvli|vadd'  # nxv2i32
```

```text
vsetvli zero, a0, e32, m2, ta, ma     # nxv4i32
vadd.vv v8, v8, v10
vsetvli zero, a0, e32, m1, ta, ma     # nxv2i32
vadd.vv v8, v8, v9
```

<details>
<summary>诊断结论与修复方向（点开）</summary>

- **结论**：不是 bug。LMUL 由“最小 VLEN + 数据宽度”推导，`-riscv-v-vector-bits-min` 只在你知道硬件 VLEN 更大时才该加。
- **先看哪层**：类型/DAG 层的 container 选择，不进 ISel。
- **注意**：`-riscv-v-vector-bits-min` 只影响 **fixed-length** 向量；对 `<vscale x 2 x i32>` 加 `=512`，实测仍是 m1。
- **若这是 bug**（比如 VLEN 已知但 container 算错）：改 `RISCVISelLowering.cpp:3383`，或检查 `RISCVSubtarget.h:271` 的 VLEN 来源。
- **lit 落点**：`llvm/test/CodeGen/RISCV/rvv/fixed-vectors-vadd*.ll`；先加一个 `-mattr=+v,-zvl128b,+zvl256b` 的 RUN 行。
</details>

---

## C.5 案例三：期望无 mask，实际带 `v0.t`

**输入**（`mask2.c`，`if` 包住一次累加）：

```c
void condadd(int *restrict a, const int *restrict b, int n, int lim) {
  for (int i = 0; i < n; i++)
    if (b[i] > lim) a[i] = a[i] + b[i];
}
```

**实测汇编**（节选，省略循环控制）：

```text
vsetvli a5, a2, e32, m2, ta, ma
vle32.v v8, (a7)
vmsgt.vx v0, v8, a3
vle32.v v10, (a6), v0.t
vadd.vv v8, v10, v8
vse32.v v8, (a6), v0.t
```

`v0.t` 出现在 load/store 上。**为什么**：IR 里 vectorizer 生成的是 `llvm.vp.load` / `llvm.vp.store`，EVL + mask 一起来：

```llvm
%vp.op.load   = tail call <vscale x 4 x i32> @llvm.vp.load.nxv4i32.p0(ptr align 4 %1, <vscale x 4 x i1> splat (i1 true), i32 %0)
%vp.op.load17 = tail call <vscale x 4 x i32> @llvm.vp.load.nxv4i32.p0(ptr align 4 %3, <vscale x 4 x i1> %2, i32 %0)
tail call void @llvm.vp.store.nxv4i32.p0(<vscale x 4 x i32> %4, ptr align 4 %3, <vscale x 4 x i1> %2, i32 %0)
```

读法：`b[i]` 的 load 用 `splat (i1 true)`（全 1），`a[i]` 的 load/store 用 `%2`（你的 `if`）。所以 **`v0.t` 不是“凭空多出来的 mask”**：EVL 机制让每次访问都带一个 mask 操作数，全 1 时后端会消掉；只有真条件才留下 `v0.t`。实测 `-stop-after=finalize-isel` 能看到对应的 `PseudoVLE32_V_M2_MASK` / `PseudoVSE32_V_M2_MASK`，而同一条 `PseudoVADD_VV_M2` 是无 mask 的。

select 形态还能产出 `vmerge`。`mask3.c`（`x > lim ? x+1 : x-1`）实测：

```text
vmsgt.vx v0, v10, a3
vmerge.vim v12, v8, 1, v0
vadd.vv v10, v12, v10
```

配套的 `vmv.v.i v8, -1` 先造出全 `-1` 的 splat；`vmerge.vim` 在 mask 为真处改成 `1`、假处保留 `-1`，再加到 `x` 上，正好得到 `x+1` / `x-1`。这是“三元表达式 → 掩码 + select”的典型形状。

**判断“这个 mask 该不该有”**：在 `-stop-after=finalize-isel` 下看 pseudo 的 opcode 后缀。`mask2` 里 `b[i]` 是 `PseudoVLE32_V_M2`（无 mask），`a[i]` 是 `PseudoVLE32_V_M2_MASK`（带 mask 操作数）；纯 `a[i]=b[i]+c[i]` 则两条都是 `_V_M2`。若你期望无 mask，要么源头没有 `if`/`select`，要么是 peephole 没把 `vmerge` 消掉。

<details>
<summary>诊断结论与修复方向（点开）</summary>

- **结论**：预期行为。`if` 被 if-conversion 成 mask，mask 只能待在 `v0`。
- **先看哪层**：MI 优化层。`RISCVVectorPeephole` 负责“能不要 mask 就不要”（`vmerge`→`vmv`，`RISCVVectorPeephole.cpp:265` 起）；剩余 mask 是语义需要。
- **判断技巧**：`-stop-before=riscv-vector-peephole` 与 `-stop-after=riscv-vector-peephole` 对比，看 `PseudoVMERGE_*` 是否消失、`_MASK` 是否变成非 mask 形态。
- **若这是 bug**（mask 多余）：改 `RISCVVectorPeephole.cpp`，或在 `RISCVISelLowering.cpp:16310` 的 `lowerVPOp` 里处理全 1 mask。
- **lit 落点**：`llvm/test/CodeGen/RISCV/rvv/vadd-vp-mask.ll`、`fixed-vectors-vadd-vp-mask.ll`。
</details>

---

## C.6 案例四：期望 `vle32.v`，实际 `vlse32.v` / `vluxei64.v`

**输入 A**（固定步长 4）：`for (i) a[i] = b[i*4];`（汇编节选，省略地址计算）

```text
vsetvli a5, a2, e8, mf2, ta, ma
vlse32.v v8, (a6), a4
vse32.v v8, (a6)
```

`vlse32.v` 是 **strided load**，第三个操作数 `a4` 是字节步长。生成的步长来自：

```text
li a4, 16
slli a6, a3, 4      # i * 4 elements * 4 bytes
```

**别被 `e8, mf2` 骗了**：那条 `vsetvli` 的 vtype 是给“元素计数”用的，load/store 的 EEW（`.32`）编码在指令自身，不在 vtype。`e8,mf2` 与 `e32,m2` 的 `SEW/LMUL` 比同为 16，VLMAX 相同，所以 `vl` 语义一致。看到这不要误判成“SEW 选错”。

**输入 B**（gather）：`for (i) a[i] = b[idx[i]];`（节选）

```text
vsetvli a6, a3, e32, m2, ta, ma
vle32.v v12, (t0)
vluxei64.v v12, (a1), v8
vse32.v v12, (a7)
```

`vluxei64.v` 是 **indexed-unordered gather**：索引向量 `v8` 的元素宽度是 64（`idx` 被提升到 i64）。`-Rpass` 对这类循环往往只报 `vectorized loop` / `interleaving is not beneficial`，**不会告诉你用了 gather**——所以判断 memory op 形态时，**看汇编比看 remark 可靠**。

**判断“该不该是连续访问”**：只有 `a[i]`/`b[i]` 且步长为 1 才会出 `vle32.v`。`i*4` 是 stride，`idx[i]` 是 index，都合法但不同。若你确认 stride 就是 4，那 `vlse32.v` 是**正确的最优选择**，不要改成四次 `vle32.v`。

<details>
<summary>诊断结论与修复方向（点开）</summary>

- **结论**：预期行为。步长/索引表达式决定了 memory op 的形态。
- **先看哪层**：类型/DAG → 选指。`lowerVPStridedLoad`（`RISCVISelLowering.cpp:16667`）和 `isLegalStridedLoadStore`（`:30899`）决定能否直接选 strided/indexed pseudo。
- **若这是 bug**（不该 strided 却 strided，或该 gather 却标量化）：先看 TTI 的 `getMemoryOpCost`（`RISCVTargetTransformInfo.cpp:2658`）是否把 gather 判得太贵；再看 legality。
- **lit 落点**：`llvm/test/CodeGen/RISCV/rvv/vlse.ll`、`vluxei.ll`；代价侧 `llvm/test/Analysis/CostModel/RISCV/rvv-load-store.ll`。
</details>

---

## C.7 案例五：期望 `vadd.vv`，实际 `vadd.vi` / `vwadd` / `vadd.vv` 自加

三个“形态不同但语义相同”的真实输出：

| 源码 | 期望 | 实测 | 原因 |
|---|---|---|---|
| `a[i] = b[i] + 1` | `vadd.vv` + splat | `vadd.vi v8, v8, 1` | 5-bit 有符号立即数走 `.vi` 形态 |
| `a[i] = (long)b[i] + c[i]` | `vadd.vv` | `vwadd.vv v8, v11, v10` | i32+i32→i64 是 widening add |
| `a[i] = b[i] << 1` | `vsll.vi` | `vadd.vv v8, v8, v8` | 源码注释明确“移位 1 当加法可能更快” |

**实测汇编**：

```text
vadd.vi   v8, v8, 1            # b[i] + 1
vwadd.vv  v8, v11, v10         # (long)b[i] + c[i]
vadd.vv   v8, v8, v8           # b[i] << 1  ← 故意
```

落点：`.vi`/`.vx`/`.vv` 三个形态由同一 multiclass 展开，`RISCVInstrInfoVVLPatterns.td:974` 的 `VPatBinaryVL_VV_VX_VI`，立即数变体在 `:913` 的 `VPatBinaryVL_XI`（`ImmType = simm5`）。add 的实例化在 `:2035`。widening 是另一组 `VPatBinaryWVL_*`（`:2073`）。`<<1` 的替换在 `RISCVInstrInfoVPseudos.td:6859` 与 `RISCVInstrInfoVVLPatterns.td:2165`。

**关键判断**：`vadd.vi` 比 `vadd.vv` + `vmv.v.i` 少一条，是**优化**；`vwadd` 是类型决定的唯一正确形态；`<<1 → vadd` 是策略。三者都不是“没选中”，而是**“选了另一条等价/更优的”**。诊断时先问“这条更差吗”，再问“pattern 漏了吗”。

<details>
<summary>诊断结论与修复方向（点开）</summary>

- **结论**：三条都是设计选择，不是漏匹配。
- **先看哪层**：选指层（pattern 的 operand 形态）。用 `-debug-only=isel` 能看到最终 `Morphed node`，例如 `... = PseudoVADD_VV_M2 ... TargetConstant:i64<5>`（`5` 即 e32）。
- **若这是 bug**（比如 `.vi` 的立即数溢出 simm5 却没走 `.vv`）：改 `RISCVInstrInfoVVLPatterns.td:913` 的 `ImmType`/predicate；改完 `llc` 会重新生成 MatcherTable，等一次增量构建。
- **lit 落点**：`llvm/test/CodeGen/RISCV/rvv/vadd.ll`；widening 用 `vwadd-mask-sdnode.ll` 同目录系列。
</details>

---

## C.8 诊断命令清单

按“从上游到下游”排。所有命令都在本构建实测可用。

| 目的 | 命令 | 要点 |
|---|---|---|
| 是否向量化 | `clang -O2 -Rpass=loop-vectorize -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize` | 需要 `-O2`；不给 `-march=...v` 时**完全没有输出** |
| 看向量化 IR | `clang -S -emit-llvm -o - x.c` | 找 `<vscale x 4 x i32>`、`llvm.vp.load`、`splat (i1 true)` |
| 看 ISel 结果 | `llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel -simplify-mir x.ll` | 看 `PseudoV*` 的 LMUL 后缀与 `/* e32 */` |
| 看选指过程 | `llc ... -debug-only=isel` | assertions 构建；会打印 `Morphed node` |
| 看类型合法化 | `llc ... -debug-only=legalize-types` | 只报“IR 类型合不合法”，不报 container |
| VL 收敛 | `llc ... -stop-before/after=riscv-vl-optimizer` | `-1 /* vl=VLMAX */` → `%14 /* vl */` |
| pseudo 化简 | `llc ... -stop-before/after=riscv-vector-peephole` | 看 `_MASK`/`vmerge` 是否消失 |
| vsetvli 插入 | `llc ... -stop-before/after=riscv-insert-vsetvli` | 看 `PseudoVSETVLI` 的 vtype 立即数 |
| 验证指令存在 | `llvm-mc -triple=riscv64 -mattr=+v -show-encoding` | 先确认“我想的那条”真的能编码 |

两个使用细节：`-simplify-mir` 只影响打印，不改变代码生成；`-verify-machineinstrs` 可以和 `-stop-after` 一起加，提前抓非法 MIR。

---

## C.9 最小修复方向

先定性，再定文件。下表是“若判定为 bug”时的第一落点，不是猜测。

| 案例 | 层 | 真实文件:行 | 先补哪个 lit |
|---|---|---|---|
| 一 | 依赖/代价 | `llvm/lib/Analysis/LoopAccessAnalysis.cpp`；代价侧 `RISCVTargetTransformInfo.cpp:2658` | `llvm/test/Transforms/LoopVectorize/RISCV/` |
| 二 | container | `RISCVISelLowering.cpp:3383`；VLEN 来源 `RISCVSubtarget.h:271` | `llvm/test/CodeGen/RISCV/rvv/fixed-vectors-vadd*.ll` |
| 三 | mask 消除 | `RISCVVectorPeephole.cpp:265`；`RISCVISelLowering.cpp:16310` | `llvm/test/CodeGen/RISCV/rvv/vadd-vp-mask.ll` |
| 四 | strided/indexed | `RISCVISelLowering.cpp:16667`、`:30899`；代价 `RISCVTargetTransformInfo.cpp:2658` | `llvm/test/CodeGen/RISCV/rvv/vlse.ll`、`vluxei.ll` |
| 五 | pattern 形态 | `RISCVInstrInfoVVLPatterns.td:913`、`:974`、`:2035`；pseudo `RISCVInstrInfoVPseudos.td:6171` | `llvm/test/CodeGen/RISCV/rvv/vadd.ll` |

三条经验：

- **能改 TTI 就不改 pattern**：pattern 改动会重生成 61 MB 的 `RISCVGenDAGISel.inc`（Ch 14 实测），review 成本高。
- **能改 peephole 就不改 ISel**：peephole 有 `-stop-after` 可单测，MIR 测试比 DAG 测试稳定。
- **不要在 lowering 里硬编码 SEW/LMUL**：那是 container + subtarget 的职责，硬编码会在 `zvl256b` 上翻车。

---

## C.10 源码落点

以下行号均用 `grep -n` 在本树核过：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
grep -n 'VPatBinaryVL_VV_VX_VI<riscv_add_vl' llvm/lib/Target/RISCV/RISCVInstrInfoVVLPatterns.td
grep -n 'defm PseudoVADD ' llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td
grep -n 'static MVT getContainerForFixedLengthVector' llvm/lib/Target/RISCV/RISCVISelLowering.cpp
grep -n 'unsigned getRealMinVLen' llvm/lib/Target/RISCV/RISCVSubtarget.h
grep -n 'RISCVTTIImpl::getMemoryOpCost\|RISCVTTIImpl::getArithmeticInstrCost' llvm/lib/Target/RISCV/RISCVTargetTransformInfo.cpp
```

实测输出：

```text
llvm/lib/Target/RISCV/RISCVInstrInfoVVLPatterns.td:2035:defm : VPatBinaryVL_VV_VX_VI<riscv_add_vl, "PseudoVADD">;
llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td:6171:defm PseudoVADD   : VPseudoVALU_VV_VX_VI<Commutable=1>;
llvm/lib/Target/RISCV/RISCVISelLowering.cpp:3383:static MVT getContainerForFixedLengthVector(MVT VT,
llvm/lib/Target/RISCV/RISCVSubtarget.h:271:  unsigned getRealMinVLen() const {
llvm/lib/Target/RISCV/RISCVTargetTransformInfo.cpp:2658:InstructionCost RISCVTTIImpl::getMemoryOpCost(unsigned Opcode, Type *Src,
llvm/lib/Target/RISCV/RISCVTargetTransformInfo.cpp:3091:InstructionCost RISCVTTIImpl::getArithmeticInstrCost(
```

其余站位：

- `RISCVInstrInfoVVLPatterns.td:913` `class VPatBinaryVL_XI`（`.vi` 形态）、`:974` multiclass、`:1031` widening。
- `RISCVInstrInfoVPseudos.td:2718` `multiclass VPseudoVALU_VV_VX_VI`（14 个 opcode 的来源）、`:6269` `PseudoVWADD`、`:6859` shift-by-1。
- `RISCVISelLowering.cpp:9818/9820` `lowerVPOp`/`lowerVPStridedLoad` 分发、`:16310` `lowerVPOp` 本体、`:16667` `lowerVPStridedLoad`、`:30899` `isLegalStridedLoadStore`。
- `RISCVVectorPeephole.cpp:44` impl、`:265` `getVMV_V_VOpcodeForVMERGE_VVM`、`:329` mask 折叠、`:985` `runOnMachineFunction`。
- `RISCVVLOptimizer.cpp:1453` legacy pass；`RISCVInsertVSETVLI.cpp:157` `INITIALIZE_PASS`。
- `RISCVISelLowering.h:386` `computeVLMAX`、`:398` `getM1VT`（`RVVBitsPerBlock == 64` 的 `static_assert` 在 `RISCVISelLowering.cpp:8584`）。

---

## C.11 可跑实验

以下 8 个实验全部在本机构建跑过。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码里 `cd` 到 `/tmp/pc` 自建。

**E1 — 向量化成功与失败对照**

```bash
$T/bin/clang --target=riscv64 -march=rv64gcv -O2 -c -o /dev/null -Rpass=loop-vectorize c1.c
```

```text
c1.c:2:3: remark: vectorized loop (vectorization width: vscale x 4, interleaved count: 1) [-Rpass=loop-vectorize]
```

（节选，省略 caret 行。）

把 `c1.c` 换成 `dep.c`（`a[i] = a[i-1] + b[i]`）后同一命令输出 `loop not vectorized [-Rpass-missed=loop-vectorize]`，详见 C.3。

**E2 — 看向量化 IR**

```bash
$T/bin/clang --target=riscv64 -march=rv64gcv -O2 -S -emit-llvm -o - c1.c | grep -m1 'add nsw <vscale x 4 x i32>'
```

```text
%3 = add nsw <vscale x 4 x i32> %vp.op.load11, %vp.op.load
```

**E3 — ISel 之后的 pseudo（节选，省略 `:: (load ...)` 与循环控制）**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel -simplify-mir mask3.ll -o -
```

```text
%14:gprnox0 = PseudoVSETVLI %2, 199 /* e8, mf2, ta, ma */, implicit-def dead $vl, implicit-def dead $vtype
%17:vrm2 = PseudoVLE32_V_M2 $noreg, %16, %14 /* vl */, 5 /* e32 */, 2 /* tu, ma */
early-clobber %18:vr = PseudoVMSGT_VX_M2 %17, %10, -1 /* vl=VLMAX */, 5 /* e32 */
%20:vrm2nov0 = PseudoVMERGE_VIM_M2 $noreg, %19, 1, %21, -1 /* vl=VLMAX */, 5 /* e32 */
%22:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %20, %17, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
```

注意 `PseudoVSETVLI` 的 vtype 是 `e8, mf2` 而数据 pseudo 是 `e32` + `_M2`——**ISel 阶段两者不必一致**，`InsertVSETVLI` 会统一。

**E4 — VL 优化器：`-1` 变真实 `vl`**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-before=riscv-vl-optimizer -simplify-mir mask3.ll -o - | grep PseudoVADD
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=riscv-vl-optimizer  -simplify-mir mask3.ll -o - | grep PseudoVADD
```

```text
before: %22:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %20, %17, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
after:  %22:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %20, %17, %14 /* vl */, 5 /* e32 */, 3 /* ta, ma */
```

**E5 — vsetvli 插入（节选）**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=riscv-insert-vsetvli -simplify-mir mask3.ll -o - | grep -E 'VSETVL'
```

```text
dead %30:gprnox0 = PseudoVSETVLIX0 killed $x0, 209 /* e32, m2, ta, ma */, implicit-def $vl, implicit-def $vtype
%14:gprnox0 = PseudoVSETVLI %29, 209 /* e32, m2, ta, ma */, implicit-def $vl, implicit-def $vtype
```

`209 = e32,m2,ta,ma`。VMV 用 VLMAX、数据指令用 `%vl`，这正是 `vsetvli` 的两次形态。

**E6 — 选指过程**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -debug-only=isel lmul.ll -o /dev/null 2>&1 | grep -E 'Morphed node: t[0-9]+: nxv|PseudoVADD_VV_M2'
```

```text
Morphed node: t19: nxv4i32 = PseudoVADD_VV_M2_MASK undef:nxv4i32, t28, t25, t18, ...
Morphed node: t18: nxv4i1 = PseudoVMSET_M_B16 TargetConstant:i64<8>, TargetConstant:i64<0>
...
t41: nxv4i32 = PseudoVADD_VV_M2 Register:nxv4i32 $noreg, t42, t43, TargetConstant:i64<8>, TargetConstant:i64<5>, TargetConstant:i64<1>
```

匹配器**先**选中 `_MASK` + 全 1 mask，再被化简成无 mask 形式。所以“看到 `_MASK`”不等于“最终有 mask”。

**E7 — 类型合法化看什么、不看什么**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -debug-only=legalize-types lmul.ll -o /dev/null 2>&1 | grep -E 't7|v8i32'
```

```text
Legalizing node: t7: v8i32,ch = load<(load (s256) from %ir.a, align 4)> t0, t2, poison:i64
Analyzing result type: v8i32
Legally typed node: t7: v8i32,ch = load<(load (s256) from %ir.a, align 4)> t0, t2, poison:i64
```

`v8i32` 直接判 legal——**container 转换不在这层做**，所以查 LMUL 要用 E3 而不是 E7。

**E8 — 指令存在性 + 编码**

```bash
printf 'vadd.vv v8, v10, v8\nvadd.vi v8, v8, 1\nvwadd.vv v8, v11, v10\nvlse32.v v8, (a0), a4\nvluxei64.v v12, (a1), v8\nvmerge.vim v12, v8, 1, v0\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+v -show-encoding
```

```text
vadd.vv     v8, v10, v8      # encoding: [0x57,0x04,0xa4,0x02]
vadd.vi     v8, v8, 1        # encoding: [0x57,0xb4,0x80,0x02]
vwadd.vv    v8, v11, v10     # encoding: [0x57,0x24,0xb5,0xc6]
vlse32.v    v8, (a0), a4     # encoding: [0x07,0x64,0xe5,0x0a]
vluxei64.v  v12, (a1), v8    # encoding: [0x07,0xf6,0x85,0x06]
vmerge.vim  v12, v8, 1, v0   # encoding: [0x57,0xb6,0x80,0x5c]
```

**E9 — 运行期：scalar 能跑，RVV 不能**

两个程序共用同一骨架（`run.c` 用 `rv64gc`，`runv.c` 用 `rv64gcv`，只差一个自增循环）：

```c
static long sys(long n, long a, long b, long c) {
  register long a7 __asm__("a7") = n;   register long a0 __asm__("a0") = a;
  register long a1 __asm__("a1") = b;   register long a2 __asm__("a2") = c;
  __asm__ volatile("ecall" : "+r"(a0) : "r"(a7), "r"(a1), "r"(a2) : "memory");
  return a0;
}
static int buf[16];
void _start(void) {
  for (int i = 0; i < 16; i++) buf[i] = i * 3 + 1;   /* runv.c: buf[i] += 1 */
  sys(93, buf[15], 0, 0);                            /* exit(buf[15]) */
  for (;;) {}
}
```

```bash
$T/bin/clang --target=riscv64 -march=rv64gc  -O2 -static -nostdlib -fuse-ld=lld -Wl,-e,_start -o run.elf  run.c
$T/bin/clang --target=riscv64 -march=rv64gcv -O2 -static -nostdlib -fuse-ld=lld -Wl,-e,_start -o runv.elf runv.c
/usr/bin/qemu-riscv64-static ./run.elf  ; echo $?
/usr/bin/qemu-riscv64-static ./runv.elf ; echo $?
```

```text
46                    # scalar：程序自算的结果，正常退出
Illegal instruction (core dumped)
132                   # RVV：qemu 6.2 只认 draft V 0.7
```

`runv.s` 的第一条向量指令是 `vsetivli zero, 8, e32, m2, ta, ma`，SIGILL 就发生在这里。

**E10 — LMUL 对照（可重复 C.4）**

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel -simplify-mir lmul.ll -o - | grep VADD
$T/bin/llc -mtriple=riscv64 -mattr=+v -riscv-v-vector-bits-min=256 -stop-after=finalize-isel -simplify-mir lmul.ll -o - | grep VADD
```

```text
%4:vrm2 = PseudoVADD_VV_M2 ...     # +v  (zvl128b)
%4:vr   = PseudoVADD_VV_M1 ...     # bits-min=256
```

---

## C.12 踩雷集锦

1. **`-Rpass` 需要 `-O2`，且只对循环说人话。** `-O1` 实测输出 `only vectorizing loops that explicitly request it`；不加 `-march=...v` 则**零输出**（静默），容易误判成“没跑”。
2. **`-mattr=+v` 隐含 `zvl128b`。** `.attribute 5` 里直接能看到。想要更小的 LMUL 就得显式告诉编译器 VLEN 更大，而不是“我觉得 256 位”。
3. **`-riscv-v-vector-bits-min` 改 LMUL，不改正确性。** m1/m2 都算对；它只影响 container 和代价。加到 scalable 类型上无效（实测 `nxv2i32` 加 `=512` 仍是 m1）。
4. **`-debug-only` 需要 assertions 构建。** 本构建是 `Optimized build with assertions.`；release 构建下这些输出会消失，**未实测**具体是静默还是报错。
5. **`-stop-after` 拼错：不同工具行为不同。** `llc -stop-after=...`/`-stop-before=...` 拼错会直接 `LLVM ERROR: "..." pass is not registered.`（exit 1）；但 `llc -print-after=<typo>` 和 `opt -stop-after=<typo>` **静默成功**（exit 0，无输出）。所以“没输出”先怀疑拼写。
6. **intrinsic 的 SEW/LMUL 由 LLVM 类型决定，不是由参数决定。** 且 `RVVBitsPerBlock == 64`：`<vscale x 4 x i32>` 在 VLEN=128 上是 m2，不是 m1。要 m1 得写 `nxv2i32`。
7. **masked intrinsic 的 passthru 与 policy 都要管。** `llvm.riscv.vadd.mask` 的第 1 个参数是 passthru（masked-off 时保留的值），policy 传 `0` 得到 `tu, mu`，传 `3` 得到 `ta, ma`——实测 vsetvli 立即数随之变化。
8. **qemu 6.2 跑不了 RVV 1.0。** 实测 SIGILL（exit 132）。向量程序的运行期结论在本机**一律未实测**；只能做静态检查（`llc`/`llvm-mc`/`llvm-objdump`）。

---

## C.13 本章重点整理

- “没选中”先按 **前端 → 类型/DAG → 选指 → MI 优化 → 配置** 五层切，不要一上来读 pattern。
- 诊断的证据单位是**停点 + 输出**：`-stop-before/after` 给边界，`-debug-only` 给过程，`llvm-mc` 给“这条指令真的存在”。
- 本练习的五个案例在本树都**不是 bug**：两个是依赖与 container 的预期推导，三个是 mask/strided/立即数形态的策略选择。真 bug 通常表现为“同一语义只在某个 LMUL/SEW/VLEN 组合下才错”，这时才动 pattern/legalization。
- LMUL 是 `VLEN_min` 与数据宽度的函数；`RVVBitsPerBlock == 64` 是 scalable 类型的换算常数。
- 修复优先级：TTI 代价 > peephole > pattern > lowering；越往下游改，构建与 review 成本越高。
- 运行期验证在这台机器上只覆盖 scalar；向量结论必须标“未实测”。

---

## C.14 自我检核

- [ ] 能不看笔记画出 C.2 的决策树，并说出每条分支用哪条命令。
- [ ] 能解释 `-Rpass-missed` 与 `-Rpass-analysis` 的分工，并复现一次 `Backward loop carried data dependence`。
- [ ] 能说明为什么 `<8 x i32>` 在 `+v` 下是 m2，在 `-riscv-v-vector-bits-min=256` 下是 m1。
- [ ] 能说出 `RVVBitsPerBlock` 的值，并据此推出 `nxv4i32` 的 LMUL（VLEN=128）。
- [ ] 能在 `-stop-after=finalize-isel` 的输出里区分 `/* e32 */`（SEW）与 `_M2`（LMUL）。
- [ ] 能解释为什么 ISel 阶段 `PseudoVSETVLI` 的 vtype 可以和 `_M2` 数据指令不一致。
- [ ] 能判断 `vadd.vi`、`vwadd.vv`、`vadd.vv v8,v8,v8` 三种“非预期”是否算 bug。
- [ ] 能说出 `-stop-after` 拼错在 `llc` 与 `opt` 下的不同表现。
- [ ] 能说明为什么本机不能用 qemu 验证 RVV，并给出替代的静态证据。

---

## C.15 延伸阅读

- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/13-legalization.md`
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/14-selection-dag-patterns.md`
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/17-rvv-lowering.md`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoVVLPatterns.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVVectorPeephole.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/`

→ 下一章：[Ch 18 — 伪指令展开：从 Pseudo 到真实指令](./18-pseudo-expansion.md)
