# 练习 E — 分层定位：从数值错误与性能回归反推到源码

> 目标：把“Triton 出问题了”拆成可执行的七层定位流程：Python/语义 → TTIR → TTGIR → LLIR → PTX → SASS → runtime。做完这个练习，你应该能在不猜的情况下，用最小复现和分层证据把问题钉到某一层、某一个 pass、甚至某一行 lowering。

> **环境**：Triton 3.9.0（commit `a972119e92a27b445cd160017ea461e56148590c`），T4（sm_75），CUDA 12.4。需要一颗能跑的 kernel 和 `TRITON_KERNEL_DUMP`。`ncu`/`nsys` 如果没有安装，相关步骤标 `guided`；不要伪造 profiler 输出。

## 为什么做这个练习

Triton 的问题有一个很坏的特性：**症状和原因经常不在同一层**。

- 数值错，可能是 Python 语义层把 dtype 提升错了，也可能是 TTGIR 少了一个 barrier，也可能是 PTX 的 `ld.global` 用错了 cache policy；
- 性能回归，可能是多了一个 `convert_layout`，也可能是寄存器 spill，也可能是 occupancy 从 8 掉到 4；
- 编译失败，可能是 verifier、可能是 pattern 没注册、也可能是 `ptxas` 版本不匹配。

如果没有分层流程，你会陷入“改一行、跑一次、看看有没有好”的随机搜索。这个练习给你一套固定的排查顺序：**先缩复现，再分层取证，最后才改代码**。

## 任务规格

本练习有两个 case。你可以只做一个，但建议两个都做：Case A 训练“正确性定位”，Case B 训练“性能定位”。

### Case A — 数值错误（自己构造一个）

把练习 C 的 `FoldSplatLayout` 故意改坏，制造一个**只在多 use 时出现**的数值错误：

```cpp
// 错误版本：直接删掉原 splat，不管它还有没有别的 user
rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(), splat.getSrc());
rewriter.eraseOp(splat);   // BUG: 其他 user 的 def 被删了
```

你的任务：

1. 写一颗能触发 multi-use splat 的 kernel；
2. 用分层证据证明错误发生在哪一层；
3. 修复它，并用 lit + pytest 双重锁住。

### Case B — 性能回归（可构造）

在 `make_ttgir` 里把练习 C 的 pass 插到 `remove_layout_conversions` **之后**，观察某个 matmul kernel 的耗时变化。你的任务：

1. 用 `TRITON_KERNEL_DUMP` 对比插入前后的 TTGIR；
2. 判断回归是“多了 convert”“寄存器压力”“occupancy 下降”还是“测量噪声”；
3. 给出一个有证据的结论，而不是“感觉慢了”。

### 验收标准

| 项 | 标准 |
|---|---|
| 最小复现 | 一颗 kernel、一个 shape、一组 `num_warps`/`num_stages`，能稳定复现 |
| 分层证据 | 至少覆盖 TTIR、TTGIR、LLIR、PTX 四层，每层有一句“看到了什么/排除了什么” |
| 根因 | 能指到具体 pass 或 lowering 文件，而不是“Triton 有 bug” |
| 修复 | 改动最小，附 lit/pytest |
| 回归 | 修复后原 case 通过，且没有引入新的 convert/spill |

## 七层定位地图

| 层 | 你看什么 | 工具 | 典型症状 |
|---|---|---|---|
| L0 Python/语义 | 参数、dtype、broadcast、mask | `print`、`TRITON_INTERPRET=1`、`static_print` | 形状对不上、dtype 被提升、mask 写反 |
| L1 TTIR | `tt.load/store/dot/reduce`、`scf.*` | `TRITON_KERNEL_DUMP=1`、`triton-opt` | 控制流错、mask 丢、常量折叠错 |
| L2 TTGIR | layout、`convert_layout`、`local_alloc`、barrier | dump + `triton-opt` 单 pass | 多/少 convert、layout 冲突、barrier 缺失 |
| L3 LLIR | LLVM dialect、`llvm.intr`、地址空间 | dump + `triton-llvm-opt` | 类型转换错、intrinsic 参数错 |
| L4 PTX | `ld/st`、`mma`、`bar.sync`、`cp.async` | `ptxas`、`nvdisasm`、文本 diff | cache policy、vector width、同步 |
| L5 SASS | 指令调度、spill、occupancy | `cuobjdump -sass`、`ncu` | `LDL/STL`（spill）、`BAR.SYNC` 过多 |
| L6 runtime | grid、stream、launch、cache | `CUDA_LAUNCH_BLOCKING=1`、`compute-sanitizer` | 竞态、越界、launch 参数错 |

**原则**：从 L0 往 L6 走，但**一旦某层能解释症状就停**。不要一上来就读 SASS；也不要因为 TTIR 看起来对就跳过 TTGIR。

## 分段实现

### Step 1 — 把复现缩到最小

一个合格的 reproducer 必须满足：

- 单文件、可 `python repro.py` 直接跑；
- 固定随机种子、固定输入；
- 只保留一个 kernel、一个 shape、一组 launch 参数；
- 能打印“期望 vs 实际”的第一个不一致元素。

模板：

```python
import torch
import triton
import triton.language as tl

torch.manual_seed(0)

@triton.jit
def kernel(x_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask, other=0.0)
    tl.store(out_ptr + offs, x * 2.0, mask=mask)

n = 1000
x = torch.randn(n, device="cuda")
out = torch.empty_like(x)
kernel[(triton.cdiv(n, 128),)](x, out, n, BLOCK=128)
torch.cuda.synchronize()

ref = x * 2.0
bad = (out != ref).nonzero()
print("mismatch count:", bad.numel())
if bad.numel():
    i = bad[0].item()
    print("first mismatch at", i, "got", out[i].item(), "want", ref[i].item())
```

三个细节：

- `torch.cuda.synchronize()` 必须在比较前；否则你看到的是未完成的结果；
- `CUDA_LAUNCH_BLOCKING=1` 能把异步错误变成同步报错，定位 launch/越界问题；
- 如果是间歇性错误，跑 100 次并记录第一次失败的 seed；不要用“偶尔错”当复现。

### Step 2 — L0：先用解释器排除前端语义

```bash
TRITON_INTERPRET=1 python repro.py
```

解释器用 numpy 逐 op 模拟，能快速区分：

- 解释器也错 → 大概率是 Python 语义/你的 kernel 逻辑；
- 解释器对、GPU 错 → 进入 L1/L2。

限制：解释器不支持 `bfloat16` 和间接访存（`ptr = tl.load(ptr)`），见 `docs/programming-guide/chapter-3/debugging.rst`。遇到这些就跳过这一步。

### Step 3 — L1/L2：dump IR，逐层对比

```bash
TRITON_KERNEL_DUMP=1 TRITON_ALWAYS_COMPILE=1 TRITON_DUMP_DIR=/tmp/triton-dump python repro.py
ls /tmp/triton-dump/*/
```

你会看到 `<kernel>.ttir`、`.ttgir`、`.llir`、`.ptx`、`.cubin`、`.sass`（具体后缀以 `compiler.py` 的 stage 名为准）。先做三件事：

1. **TTIR 是否已经错**：`grep -n "tt.load\|tt.store\|scf.if\|arith"`，确认 mask/other/控制流和你写的一致；
2. **TTGIR 是否多了/少了 convert**：`grep -c "ttg.convert_layout"`，和“正确版本”（比如 `num_warps=4`）对比；
3. **TTGIR 是否有 barrier**：`grep -n "ttg.barrier\|ttng.wait_barrier\|ttg.local_alloc"`，shared memory 读写之间必须有同步。

如果 TTIR 对、TTGIR 错，问题在 TTGIR pass；如果 TTGIR 对、LLIR 错，问题在 conversion；如果 LLIR 对、PTX 错，问题在 LLVM/NVPTX 后端或 `ptxas`。

### Step 4 — 用 `triton-opt` 做 pass 级二分

把 TTGIR 存成文件，然后逐个 pass 跑：

```bash
# 先确认原文件能 round-trip
triton-opt kernel.ttgir -o /dev/null

# 只跑你怀疑的 pass
triton-opt kernel.ttgir -tritongpu-remove-layout-conversions -o after.mlir
diff <(triton-opt kernel.ttgir) after.mlir
```

二分策略：

- 先跑 `make_ttgir` 里的前半段（coalesce、accelerate-matmul），看 convert 数量；
- 再跑 `remove-layout-conversions`，看它是否消掉了不该消的 convert；
- 再跑 pipeline（`-tritongpu-pipeline`），看 async copy/barrier 是否配对；
- 最后跑 `-convert-triton-gpu-to-llvm`，看是否 legalize 失败。

如果某个 pass 单独跑没问题、组合起来有问题，说明是 **pass 顺序/analysis 失效**，不是单个 pass 的逻辑。这时要回到 `third_party/nvidia/backend/compiler.py` 的 `make_ttgir` 看顺序。

### Step 5 — L4/L5：PTX 与 SASS

```bash
# PTX 在 dump 目录里；也可以从 cubin 反汇编
cuobjdump -sass kernel.cubin > kernel.sass
nvdisasm -g kernel.cubin > kernel.nvdisasm
```

看三样：

1. **同步**：`bar.sync` / `BAR.SYNC` 的数量和位置；shared memory 读写之间缺同步 → 竞态；
2. **spill**：SASS 里出现 `LDL`/`STL`（local load/store）通常意味着寄存器压力过大；对照 PTX 的 `.local` 声明；
3. **occupancy**：`ncu --set full` 或 `ncu --metrics launch__occupancy_limit_registers,launch__occupancy_limit_shared_mem`。没有 `ncu` 就标 `guided`。

**注意**：PTX 的 `ld.global` 可能被 `ptxas` 重排/合并；不要用 PTX 的行号去对 SASS 的行号，除非你开了 `-lineinfo`。

### Step 6 — L6：runtime 与竞态

```bash
CUDA_LAUNCH_BLOCKING=1 python repro.py
compute-sanitizer --tool memcheck python repro.py
compute-sanitizer --tool racecheck python repro.py
```

- `memcheck` 抓越界/未初始化；
- `racecheck` 抓 shared memory 竞态（对 barrier 问题最有效）；
- Triton 自己的 ConSan（`TRITON_INSTRUMENTATION_MODE=consan`）能在编译期插入并发检查，适合怀疑 `ttg.barrier` 缺失时使用；具体支持范围以 `third_party/nvidia/backend/compiler.py` 的 `is_enabled(options, "consan")` 分支为准（**guided**：T4 上的行为未实测）。

### Step 7 — 修复与回归

修复后至少做三件事：

1. 原 reproducer 通过；
2. 新增 lit 用例锁 IR 形状（比如“multi-use 时不能删原 splat”）；
3. 新增 pytest 锁数值（`torch.testing.assert_close`）。

如果修复涉及 pass 顺序，额外记录：修复前/后的 TTGIR convert 数量、PTX 指令数、kernel 耗时（同一 seed、同一 shape、跑 100 次取中位数）。

## Case A 参考解答

<details>
<summary>故意改坏的 pass 与定位过程（点击展开）</summary>

### 错误版本

```cpp
struct FoldConvertOfSplat : public OpRewritePattern<ConvertLayoutOp> {
  using OpRewritePattern::OpRewritePattern;

  LogicalResult matchAndRewrite(ConvertLayoutOp op,
                                PatternRewriter &rewriter) const override {
    auto splat = op.getSrc().getDefiningOp<triton::SplatOp>();
    if (!splat)
      return failure();
    rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(),
                                                 splat.getSrc());
    rewriter.eraseOp(splat);   // BUG
    return success();
  }
};
```

### 为什么 lit 能抓到

multi-use 用例：

```mlir
%0 = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked0>
%1 = ttg.convert_layout %0 : tensor<128xf32, #blocked0> -> tensor<128xf32, #blocked1>
tt.return %0, %1 : tensor<128xf32, #blocked0>, tensor<128xf32, #blocked1>
```

`eraseOp(splat)` 之后，`tt.return` 还在用 `%0`，MLIR 会报 “operation destroyed but still has uses”（debug build 直接 assert；release build 可能产生悬空值/崩溃）。这不是“数值错”，而是 **IR 非法**。所以 Case A 的第一步不是跑 GPU，而是跑 lit/verifier：

```bash
triton-opt broken.mlir -tritongpu-fold-splat-layout
```

如果这一步就炸，问题在 L2，不需要往下走。

### 如果错误更隐蔽（不崩溃但数值错）

把 bug 改成“用源 layout 重建 splat，但把结果类型硬转成目标 layout”这类类型欺骗（`UnrealizedConversionCastOp` 或手写 `RankedTensorType::get`）。这时 verifier 可能过，但 layout 和实际数据分布不一致，数值会错。定位顺序：

1. `TRITON_INTERPRET=1` → 对，排除前端；
2. dump TTGIR → 看到 `tt.splat` 的 encoding 和 consumer 期望不一致；
3. 用 `triton-opt` 单跑该 pass → 复现；
4. 对比 `#blocked0`/`#blocked1` 的 `toLinearLayout`（Ch 13）→ 确认元素映射不同；
5. 修复：始终用 `op.getType()`，不要手拼类型。

### 正确版本

```cpp
rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(), splat.getSrc());
// 不 erase 原 splat；没有其他 user 时交给 canonicalize/DCE。
```

</details>

## Case B 参考解答

<details>
<summary>性能回归的分层判断（点击展开）</summary>

### 先排除测量噪声

```python
import triton.testing
ms = triton.testing.do_bench(lambda: kernel[grid](...), warmup=25, rep=100)
```

- 同一进程、同一 GPU、同一 clock 状态下比较；
- 至少跑 3 次，取中位数；
- 差异小于 3% 时，不要下结论。

### 再看 TTGIR

```bash
grep -c "ttg.convert_layout" before.ttgir after.ttgir
```

如果 after 多了 convert，回归大概率是 layout 冲突；回到 Ch 14 的传播/remat 逻辑。

### 再看 PTX/SASS

```bash
grep -c "ld.local\|st.local" kernel.ptx        # PTX 层的 spill 迹象
cuobjdump -sass kernel.cubin | grep -c "LDL\|STL"   # SASS 层的 spill
```

- spill 增加 → 寄存器压力，可能是 pass 让更多值活得更久；
- `bar.sync` 增加 → 同步变多，可能是 pipeline/WS 变化；
- occupancy 下降 → 用 `ncu` 看 register/shared memory 限制。

### 结论模板

> 插入 pass 后，TTGIR 的 `ttg.convert_layout` 从 3 个变成 5 个；PTX 的 `ld.local` 从 0 变成 12；SASS 出现 `LDL/STL`；`do_bench` 中位数从 0.42ms 变成 0.47ms（+12%）。根因是 pass 放在 `remove_layout_conversions` 之后，导致后者无法把新 splat 的 layout 传播回去。修复：把 pass 移到 `remove_layout_conversions` 之前，convert 数回到 3，耗时回到 0.42ms。

这个模板的每一句都必须有对应命令的输出；没有输出的句子删掉。

</details>

## 症状 → 层 的快速索引

下面这张表是“看到这个症状，先查哪一层”的索引。它不是绝对规则，但能把你从随机搜索里拉出来。

| 症状 | 先查 | 为什么 |
|---|---|---|
| `CompileError` / `assert` 在 Python 层 | L0 | 参数、dtype、constexpr、grid 写错 |
| verifier 报 “expected … got …” | L1/L2 | IR 类型/encoding 不合法 |
| 结果全 0 / 全 NaN | L0/L1 | mask、`other`、初始化、dtype 提升 |
| 只有部分 block 错 | L2/L6 | layout 映射、grid/launch、越界 |
| `num_warps` 改变才错 | L2 | warp 间 reduce/barrier/layout |
| `num_stages` 改变才错 | L2 | pipeline 的 prologue/epilogue 或 async wait |
| 偶发、不可复现 | L6 | 竞态、未同步、launch 异步 |
| PTX 对但 SASS 错 | L4/L5 | `ptxas` 版本、NVPTX 后端、指令调度 |
| 性能突然掉 20% | L2/L5 | 多 convert、spill、occupancy |
| cache 清了就好 | L6 | cache key/旧 `.so`/stale dump |

## 一次完整的定位记录（教学构造）

下面这条记录演示“每一层写一句证据”的格式。它不是上游 issue 的复刻，而是把 Case A 的 bug 走一遍，方便你对照自己的记录。

**现象**：一颗使用 multi-use splat 的 kernel，在启用 `FoldSplatLayout` 后结果错；不开 pass 正确。

**L0**：

```bash
TRITON_INTERPRET=1 python repro.py
# 输出：mismatch count: 0
```

结论：前端语义正确，问题在编译期变换。

**L1**：

```bash
TRITON_KERNEL_DUMP=1 TRITON_ALWAYS_COMPILE=1 python repro.py
grep -n "tt.splat" /tmp/triton-dump/*/kernel.ttir
```

结论：TTIR 里只有一个 `tt.splat`，两个 user（`tt.return` 和 `ttg.convert_layout`），符合预期。

**L2**：

```bash
triton-opt kernel.ttgir -tritongpu-fold-splat-layout -o /tmp/after.mlir
```

结论：命令直接报 “operation destroyed but still has uses” 或 verifier 失败。问题锁定在 L2 的 pass，不需要看 PTX。

**修复**：

```cpp
// 不 erase 原 splat；没有其他 user 时交给 canonicalize
rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(), splat.getSrc());
```

**回归**：

```bash
lit -v test/TritonGPU/fold-splat-layout.mlir
python -m pytest python/test/unit/language/test_fold_splat.py -q
```

结论：lit 的 multi-use 用例 + pytest 的数值用例都通过。

这条记录的关键不是“最后改哪一行”，而是**每一层都有一句可复现的证据**。如果你写不出某一层的证据，说明你还没定位到那一层，不要跳。

## 性能回归的最小实验设计

性能问题最容易被“感觉”带偏。一个合格的最小实验至少控制这些变量：

| 变量 | 做法 |
|---|---|
| 输入 | 固定 shape、dtype、seed；不要用随机 shape |
| launch | 固定 `grid`、`num_warps`、`num_stages` |
| 测量 | `triton.testing.do_bench(warmup=25, rep=100)`，取中位数 |
| 环境 | 同一进程、同一 GPU、同一 clock；必要时 `nvidia-smi -lgc` 锁频（`guided`） |
| 对照 | 只改一个变量（pass 开关 / pass 位置 / `num_stages`） |
| 重复 | 至少 3 次独立运行；差异 < 3% 不下结论 |

记录模板：

```text
case: matmul 4096x4096x4096, fp16, BLOCK=128x128x64, warps=8, stages=3
before: 1.83 ms (median of 3 runs: 1.82, 1.84, 1.83)
after : 2.05 ms (median of 3 runs: 2.04, 2.06, 2.05)
delta : +12.0%
ttgir : convert_layout 3 -> 5
ptx   : ld.local 0 -> 12
sass  : LDL/STL 0 -> 12
ncu   : occupancy 50% -> 37.5% (registers)
```

这张表比“好像变慢了”有用一百倍，也是上游 review 愿意看的东西。


## 常见卡点

1. **dump 目录是空的**：cache hit 不会 dump；加 `TRITON_ALWAYS_COMPILE=1`。还要确认 `TRITON_DUMP_DIR` 有写权限。
2. **`triton-opt` 跑 TTGIR 报 unknown dialect**：TTGIR 依赖 `ttg`/`ttng` dialect；用 `triton-opt`（已注册全部 dialect），不要用系统 `mlir-opt`。
3. **改了 pass 但行为没变**：可能命中了 `~/.triton/cache`；清 cache 或设 `TRITON_ALWAYS_COMPILE=1`。也可能是 Python 扩展没重新构建，实际加载的是旧 `.so`。
4. **`compute-sanitizer` 太慢**：先缩 shape/launch 次数；racecheck 尤其慢，只在怀疑 barrier 时用。
5. **把 PTX 当最终真相**：`ptxas` 会做指令选择/调度；PTX 对但 SASS 错，问题可能在 `ptxas` 版本或 LLVM NVPTX 后端，不在 Triton lowering。
6. **用 `print` 调试 GPU tensor**：`print(tensor)` 在 device 上不会按你想象的方式工作；用 `tl.device_print`（需要 `TRITON_DEBUG=1` 才启用 `device_assert`，`device_print` 不受此限制，见官方 debugging 文档）。

## 延伸挑战

1. **写一个自动 bisect 脚本**：给定 before/after 两个 TTGIR，逐个 pass 跑并 diff，输出第一个产生差异的 pass。
2. **给 `tt.sadd_sat` 加 ConSan/GSan 检查**：饱和加法不会越界，但可以作为“如何给自定义 op 加 instrumentation”的练习。
3. **复现一个真实的 Triton issue**：从 GitHub issue 里挑一个 layout/barrier 相关的小 bug，按本练习的七层流程走一遍，记录每一层的证据。
4. **性能归因报告**：用 `ncu` 的 `--set full` 导出一份报告，把 kernel 的瓶颈归到 memory/compute/latency 三类，并解释为什么。

## 检核清单

- [ ] 有一个单文件、固定 seed、能稳定复现的 reproducer
- [ ] 用 `TRITON_INTERPRET=1` 排除了前端
- [ ] dump 了 TTIR/TTGIR/LLIR/PTX 四层
- [ ] 用 `triton-opt` 单 pass 复现了问题
- [ ] 能说出问题在 L1/L2/L3/L4 的哪一层
- [ ] 修复后 lit 和 pytest 都通过
- [ ] 性能 case 有 `do_bench` 中位数和 PTX/SASS 证据
- [ ] 没有把“猜测”写成“结论”

## 下一章

回到 [Ch 31](./31-debugging-correctness-and-sanitizers.md) 看 GSan/ConSan/FPSan 的设计；然后去 [Final Project](./final-project-full-stack-triton-extension.md) 把 op/pass/lowering/测试/benchmark 串成一个完整交付。
