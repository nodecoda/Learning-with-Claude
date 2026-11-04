# Final Project — 全栈 Triton 扩展：`saturating_add` 从 Python API 到 PTX

> 目标：独立完成一个**可以按上游标准提交**的 Triton 特性。它必须同时穿过 Python API、AST/语义、TTIR、TTGIR、LLVM lowering、PTX、interpreter、lit、pytest、benchmark 和文档。做完这个项目，你就具备“改 Triton”而不只是“读 Triton”的完整闭环。

> **环境**：Triton 3.9.0（commit `a972119e92a27b445cd160017ea461e56148590c`），T4（sm_75）。Hopper/Blackwell 专属路径标 `guided`。构建未完成时，所有需要运行的输出标 `guided`，不要伪造。

## 项目目标

实现一个用户可见的 Triton 语言特性：

```python
import triton.language as tl

y = tl.saturating_add(x, z)
```

语义：逐元素有符号饱和加法，支持 `int8/int16/int32/int64`（scalar 与 tensor），溢出时钳到该类型的 `INT_MIN`/`INT_MAX`，不回绕。

最终交付：

| 层 | 交付物 | 验收 |
|---|---|---|
| Python API | `tl.saturating_add` | 可 import、可调用、有 docstring |
| AST/语义 | `math.py` 的 builtin + 类型合法化 | 混合 dtype 报错或按规则提升 |
| Python binding | `python/src/ir.cc` 的 `create_sadd_sat` | 前端能发射 TTIR op |
| TTIR | `tt.sadd_sat` + verifier | round-trip、非法类型报错 |
| Lowering | `SaddSatOpToLLVM.cpp` | 生成 `llvm.add/icmp/select` |
| Interpreter | `create_sadd_sat` | `TRITON_INTERPRET=1` 可跑 |
| 测试 | lit + pytest + interpreter test | 全绿 |
| 性能 | benchmark vs `tl.clamp(x + y, ...)` | 有数据、有结论 |
| 文档 | docstring + 设计说明 | 上游 PR 可读 |
| 回滚 | 单一 commit / 可 revert | 不破坏现有 API |

## 为什么选 `saturating_add`

1. **有真实需求**：int8/fp8 量化、图像处理、定点运算都需要饱和加法；手写 `tl.clamp(x + y, INT_MIN, INT_MAX)` 在溢出时已经回绕，语义是错的。
2. **足够小**：不涉及 layout、不涉及新硬件指令，能在 T4 上完整验证。
3. **足够全**：它必须穿过前端、IR、lowering、interpreter 四层，正好覆盖本课 70% 的核心概念。
4. **可对比**：可以和 `tl.clamp` 版本做性能对比，训练“优化是否值得”的判断。

**注意**：上游可能已经有类似讨论（比如 `tl.saturating_cast` 或量化相关 op）。提交前先搜 issue/PR，确认命名与语义不冲突；如果已有实现，改成“补测试/补文档/补 lowering”同样算完成，但要在设计说明里写清楚差异。

## 架构：一次改动穿过五层

```text
┌─────────────────────────────────────────────────────────────┐
│ L0  Python API      python/triton/language/math.py          │
│                     tl.saturating_add(x, y)                 │
├─────────────────────────────────────────────────────────────┤
│ L1  Binding         python/src/ir.cc                        │
│                     builder.create_sadd_sat(lhs, rhs)       │
├─────────────────────────────────────────────────────────────┤
│ L2  TTIR            TritonOps.td + Ops.cpp                  │
│                     tt.sadd_sat : (int, int) -> int         │
├─────────────────────────────────────────────────────────────┤
│ L3  Lowering        SaddSatOpToLLVM.cpp                     │
│                     llvm.add + icmp + select                │
├─────────────────────────────────────────────────────────────┤
│ L4  Backend         LLVM -> NVPTX -> ptxas                  │
│                     add.s32 + setp + selp                   │
├─────────────────────────────────────────────────────────────┤
│ L5  Interpreter     runtime/interpreter.py                  │
│                     numpy clip                              │
└─────────────────────────────────────────────────────────────┘
```

每一层都要有独立的验证手段；不要等最后跑端到端才发现某一层错了。

## 里程碑

### M0 — 调研与设计（0.5 天）

任务：

1. 在 Triton 仓库搜 `saturat`、`clamp`、`quantiz`，确认没有同名/同义 op；
2. 读 `python/triton/language/math.py:87-92`（`umulhi` 的写法），确定 API 风格；
3. 读 `python/src/ir.cc:1384-1387`（`create_umulhi` 的 binding），确定 builder 写法；
4. 读 `include/triton/Dialect/Triton/IR/TritonOps.td:220-233`（`TT_MulhiUIOp`），确定 op 定义；
5. 读 `lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:151-168`，确定 lowering 基类；
6. 写一页设计说明：语义、类型规则、边界值、失败模式、测试计划。

验收：设计说明能回答“溢出时返回什么”“i8 和 i32 是否同一套规则”“和 `tl.clamp` 的差异”。

### M1 — TTIR op 与 verifier（0.5 天）

改动：

- `include/triton/Dialect/Triton/IR/TritonOps.td`：新增 `TT_SaddSatOp`；
- `lib/Dialect/Triton/IR/Ops.cpp`：实现 `verify()`。

验收：

```bash
triton-opt roundtrip.mlir          # 能解析/打印
triton-opt bad-f32.mlir            # 报 "expects i8/i16/i32/i64"
```

### M2 — Lowering（1 天）

改动：

- `lib/Conversion/TritonGPUToLLVM/SaddSatOpToLLVM.cpp`；
- `include/triton/Conversion/TritonGPUToLLVM/PatternTritonGPUOpToLLVM.h`（声明）；
- `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp`（调用）；
- `lib/Conversion/TritonGPUToLLVM/CMakeLists.txt`（加文件）。

验收：

```bash
lit -v test/Conversion/sadd-sat-to-llvm.mlir
```

LLVM IR 里必须出现 `llvm.add`、`llvm.icmp slt`、`llvm.select`；scalar 与 tensor 各一个用例。

### M3 — Python API 与 binding（0.5 天）

改动：

- `python/src/ir.cc`：`create_sadd_sat`；
- `python/triton/language/math.py`：`saturating_add` builtin；
- `python/triton/language/__init__.py`：导出 `saturating_add`。

实现参考：

```python
@core.builtin
@_check_dtype(dtypes=["int8", "int16", "int32", "int64"])
def saturating_add(x, y, _semantic=None):
    x = core._unwrap_if_constexpr(x)
    y = core._unwrap_if_constexpr(y)
    x, y = core.binary_op_type_legalization(x, y, _semantic)
    return core.tensor(_semantic.builder.create_sadd_sat(x.handle, y.handle),
                       x.type)
```

注意：

- `_check_dtype` 的列表要和 verifier 一致；不要一边允许 `uint32` 一边拒绝；
- `binary_op_type_legalization` 会处理 scalar/tensor 混合与 dtype 提升；确认提升后的类型仍在允许集合里；
- docstring 要写清楚“饱和”而不是“回绕”，并给边界例子。

验收：

```python
import triton.language as tl
print(tl.saturating_add)          # 可 import
```

并用 `TRITON_KERNEL_DUMP=1` 确认 TTIR 里出现 `tt.sadd_sat`。

### M4 — Interpreter（0.5 天）

改动：`python/triton/runtime/interpreter.py` 的 builder 加：

```python
def create_sadd_sat(self, lhs, rhs):
    dtype = lhs.data.dtype
    info = np.iinfo(dtype)
    ret = np.clip(lhs.data.astype(np.int64) + rhs.data.astype(np.int64),
                  info.min, info.max).astype(dtype)
    return TensorHandle(ret, lhs.dtype.scalar)
```

验收：

```bash
TRITON_INTERPRET=1 python test_saturating_add.py
```

边界：`INT8_MAX + 1 -> 127`，`INT8_MIN + (-1) -> -128`。

### M5 — 测试矩阵（1 天）

| 测试 | 文件 | 覆盖 |
|---|---|---|
| op round-trip | `test/Triton/sadd-sat.mlir`（或 `test/Conversion/`） | 解析/打印/verifier |
| lowering scalar | `test/Conversion/sadd-sat-to-llvm.mlir` | i8/i16/i32/i64 |
| lowering tensor | 同上 | blocked layout |
| 非法类型 | 同上 | f32/i1 报错 |
| 数值 | `python/test/unit/language/test_saturating_add.py` | 边界 + 随机 |
| interpreter | 同上，`TRITON_INTERPRET=1` | numpy 路径 |
| 组合 | 同上 | 与 `tl.load/store`、mask 一起用 |

数值测试模板：

```python
import numpy as np
import torch
import triton
import triton.language as tl

@triton.jit
def kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask, other=0)
    y = tl.load(y_ptr + offs, mask=mask, other=0)
    tl.store(out_ptr + offs, tl.saturating_add(x, y), mask=mask)

def test_boundaries(dtype):
    info = np.iinfo(dtype)
    vals = np.array([info.min, info.min + 1, -1, 0, 1, info.max - 1, info.max],
                    dtype=dtype)
    # 两两组合，覆盖正溢出/负溢出/普通
    x = torch.tensor(np.repeat(vals, len(vals)), device="cuda", dtype=getattr(torch, f"int{info.bits}"))
    y = torch.tensor(np.tile(vals, len(vals)), device="cuda", dtype=getattr(torch, f"int{info.bits}"))
    out = torch.empty_like(x)
    kernel[(triton.cdiv(x.numel(), 128),)](x, y, out, x.numel(), BLOCK=128)
    ref = np.clip(x.cpu().numpy().astype(np.int64) + y.cpu().numpy().astype(np.int64),
                  info.min, info.max).astype(dtype)
    np.testing.assert_array_equal(out.cpu().numpy(), ref)
```

注意：`torch` 的 int8/int16 加法会回绕，参考值必须用 `int64` 计算再 clip；不要直接 `x + y`。

### M6 — Benchmark（0.5 天）

对比三种实现：

1. `tl.saturating_add(x, y)`；
2. `tl.clamp(x + y, INT_MIN, INT_MAX)`（注意：这个实现语义是错的，只用于性能对照）；
3. 手写 `tl.where` 溢出检测。

指标：

- `triton.testing.do_bench` 中位数；
- PTX 指令数（`grep -c` 或 `wc -l`）；
- SASS 是否有 spill（`LDL/STL`）；
- 寄存器数（`ptxas -v` 或 `ncu`）。

结论模板：

> 在 `int32`、`BLOCK=1024`、1M 元素下，`saturating_add` 的中位耗时是 X ms，`clamp` 版本是 Y ms，差异 Z%。PTX 指令数从 A 变成 B；SASS 无 spill。结论：该 op 的额外 `icmp/select` 在 memory-bound 场景下被隐藏，性能可接受。

如果没有实测数据，写“未测量”，不要编数字。

### M7 — 上游 PR 包（0.5 天）

一个可提交的 PR 应该包含：

- **commit 拆分**：op 定义 / lowering / 前端 / interpreter / 测试 / 文档，每步可独立 review；
- **PR 描述**：动机、语义、边界、测试、性能、兼容性、回滚；
- **测试证据**：`lit` 和 `pytest` 的输出摘要；
- **风险**：新 op 是否影响 canonicalizer、是否需要在 AMD 后端实现、是否影响 `tt.divisibility` 分析。

PR 描述模板：

```markdown
## Motivation
Quantized kernels need saturating integer add; `x + y` wraps and `clamp(x + y)`
cannot recover the overflowed value.

## Semantics
`tt.sadd_sat` performs signed saturating add for i8/i16/i32/i64, scalar and
tensor. Overflow clamps to INT_MIN/INT_MAX.

## Implementation
- TTIR op + verifier
- Elementwise lowering to add/icmp/select
- Python API `tl.saturating_add`
- Interpreter support

## Tests
- lit: round-trip, lowering, invalid dtype
- pytest: boundary matrix + random + interpreter

## Performance
Measured on T4, int32, 1M elements: ...

## Compatibility
New op; no existing IR changes. AMD backend falls back to generic lowering
(or: not yet implemented, tracked in ...).

## Rollback
Revert this commit; no persistent format changes.
```

## 验收 rubric

| 维度 | 权重 | 满分标准 |
|---|---|---|
| 正确性 | 30% | 边界/随机/interpreter 全过；非法输入有清晰报错 |
| 分层完整性 | 20% | 五层都有改动且各自可验证 |
| 测试质量 | 20% | lit 锁 IR、pytest 锁数值、覆盖 scalar/tensor/边界 |
| 性能证据 | 15% | 有 benchmark、有 PTX/SASS 证据、结论不过度 |
| 上游可读性 | 10% | commit 拆分、PR 描述、docstring、命名一致 |
| 回滚与兼容 | 5% | 可 revert、不破坏现有 API、AMD 路径有说明 |

## 参考骨架

<details>
<summary>关键 diff（点击展开）</summary>

### 1. `TritonOps.td`

```tablegen
def TT_SaddSatOp : TT_Op<"sadd_sat", [Elementwise,
                                      SameOperandsAndResultType,
                                      Pure]> {
  let summary = "Saturating signed integer addition";
  let arguments = (ins TT_IntLike:$x, TT_IntLike:$y);
  let results = (outs TT_IntLike:$result);
  let assemblyFormat = "$x `,` $y attr-dict `:` type($x)";
  let hasVerifier = 1;
}
```

### 2. `Ops.cpp`

```cpp
LogicalResult SaddSatOp::verify() {
  Type ty = getX().getType();
  if (auto tensorTy = dyn_cast<RankedTensorType>(ty))
    ty = tensorTy.getElementType();
  if (!ty.isInteger(8) && !ty.isInteger(16) && !ty.isInteger(32) &&
      !ty.isInteger(64))
    return emitError() << "tt.sadd_sat expects i8/i16/i32/i64, got " << ty;
  return success();
}
```

### 3. `python/src/ir.cc`

```cpp
      .def("create_sadd_sat",
           [](TritonOpBuilder &self, Value &lhs, Value &rhs) -> Value {
             return self.create<triton::SaddSatOp>(lhs, rhs);
           })
```

### 4. `math.py`

```python
@core.builtin
@_check_dtype(dtypes=["int8", "int16", "int32", "int64"])
def saturating_add(x, y, _semantic=None):
    x = core._unwrap_if_constexpr(x)
    y = core._unwrap_if_constexpr(y)
    x, y = core.binary_op_type_legalization(x, y, _semantic)
    return core.tensor(_semantic.builder.create_sadd_sat(x.handle, y.handle),
                       x.type)
```

### 5. `interpreter.py`

```python
def create_sadd_sat(self, lhs, rhs):
    dtype = lhs.data.dtype
    info = np.iinfo(dtype)
    ret = np.clip(lhs.data.astype(np.int64) + rhs.data.astype(np.int64),
                  info.min, info.max).astype(dtype)
    return TensorHandle(ret, lhs.dtype.scalar)
```

### 6. lowering

见 [练习 D](./practice-d-custom-lowering.md) 的 `SaddSatOpToLLVM.cpp`。

</details>

## 六周执行计划（可按自己的节奏压缩）

| 周 | 目标 | 产出 | 检查点 |
|---|---|---|---|
| 1 | 调研 + 设计 | 一页设计说明、API 草案、测试计划 | 能说清语义与边界 |
| 2 | TTIR op + verifier | `TritonOps.td`、`Ops.cpp`、round-trip lit | `triton-opt` 能解析/拒绝 |
| 3 | Lowering | `SaddSatOpToLLVM.cpp`、NVIDIA 注册、lit | LLVM IR 出现 add/icmp/select |
| 4 | 前端 + interpreter | `ir.cc`、`math.py`、`interpreter.py` | `tl.saturating_add` 可调用，解释器可跑 |
| 5 | 测试 + 性能 | pytest 矩阵、benchmark、PTX/SASS 证据 | 全绿 + 有数据 |
| 6 | 上游包 | commit 拆分、PR 描述、文档 | 能按上游标准提交 |

如果只有两天：优先做 M1 + M2 + lit（IR 层闭环），前端和 interpreter 标为“未完成”，不要假装做了。

## 设计说明模板

提交前把下面这份填完。它也是你未来回看时唯一能解释“为什么这么设计”的东西。

```markdown
# Saturating Add: Design Note

## 1. Motivation
- 使用场景：量化 int8/int16、图像处理、定点运算
- 现有替代：`x + y` 回绕；`clamp(x + y)` 无法恢复溢出值
- 目标用户：Triton kernel 作者

## 2. Semantics
- 输入：i8/i16/i32/i64，scalar 或 tensor，x/y 类型一致
- 输出：同类型、同 shape
- 溢出：正溢出 -> INT_MAX；负溢出 -> INT_MIN
- 非溢出：与普通加法一致
- 未定义：NaN/float 不适用（verifier 拒绝）

## 3. Type Rules
- 混合 dtype：按 `binary_op_type_legalization` 提升；提升后必须仍在允许集合
- scalar + tensor：广播到 tensor
- i1：拒绝（不是有意义的饱和加法）

## 4. Lowering
- 逐元素：add + icmp(slt) + icmp(eq) + select
- 溢出条件：sameSign && !signOk
- 备选：LLVM `sadd.sat` intrinsic（若可用）

## 5. Testing
- lit：round-trip、lowering、非法类型
- pytest：边界矩阵、随机、interpreter
- 组合：mask/load/store、不同 num_warps

## 6. Performance
- 对比 clamp 版本与 where 版本
- 指标：do_bench 中位数、PTX 指令数、SASS spill、occupancy

## 7. Compatibility & Rollback
- 新 op，不改变现有 IR
- AMD 后端：fallback / 未实现（写明）
- 回滚：revert 单个 commit，无持久化格式变化

## 8. Open Questions
- 是否需要 `uadd_sat`？
- 是否需要在 canonicalizer 里做 `sadd_sat(x, 0) -> x`？
```

## 代码审查关注点

把 PR 交给别人之前，自己先按下面的清单过一遍。这些也是上游 reviewer 最常问的问题。

**语义与类型**

- [ ] `INT_MIN + (-1)` 和 `INT_MAX + 1` 都有测试；
- [ ] 混合 dtype 的行为和 `binary_op_type_legalization` 一致；
- [ ] `i1`/`f32` 被 verifier 拒绝，错误信息可读；
- [ ] scalar 和 tensor 走同一套语义，没有隐式回绕。

**IR 与 lowering**

- [ ] op 有 `Pure`/`Elementwise` trait，不会阻止优化；
- [ ] `assemblyFormat` 能 round-trip；
- [ ] lowering 不依赖具体 layout；
- [ ] pattern 注册在 NVIDIA（和 AMD，如果支持）都完整；
- [ ] 没有引入 `UnrealizedConversionCastOp` 之类的类型欺骗。

**前端**

- [ ] `_check_dtype` 与 verifier 一致；
- [ ] docstring 有边界例子；
- [ ] `__init__.py` 导出，`__all__` 更新；
- [ ] interpreter 路径可用。

**测试**

- [ ] lit 覆盖 scalar/tensor/非法类型；
- [ ] pytest 覆盖边界/随机/mask 组合；
- [ ] 测试不依赖固定 GPU 型号（除 Hopper/Blackwell 专属）；
- [ ] 没有把“未测量”写成“更快”。

**工程**

- [ ] commit 按层拆分；
- [ ] PR 描述有动机/语义/测试/性能/兼容/回滚；
- [ ] 没有顺手改无关文件；
- [ ] 构建和测试命令可复制。

## 测试矩阵扩展

除了 M5 的最小矩阵，建议再加下面这些组合。它们能抓到“边界值对了但组合场景错”的问题。

| 维度 | 取值 | 目的 |
|---|---|---|
| dtype | i8, i16, i32, i64 | 位宽边界 |
| 容器 | scalar, `tensor<128x>`, `tensor<32x64>` | 广播/多维 |
| layout | blocked（不同 `sizePerThread`/`order`） | lowering 与 layout 解耦 |
| 值 | min, min+1, -1, 0, 1, max-1, max | 溢出边界 |
| 组合 | `tl.load` + mask + `saturating_add` | 真实 kernel 形态 |
| launch | `num_warps` = 1/4/8 | 线程映射 |
| 解释器 | `TRITON_INTERPRET=1` | numpy 路径 |

一个容易漏的用例：**masked load 的 `other` 是 `INT_MIN`**。这时 `saturating_add(other, -1)` 必须钳到 `INT_MIN`，而不是回绕到 `INT_MAX`。把它写进 pytest。


## 常见失败模式

1. **只做了 TTIR + lowering，没做前端**：`tl.saturating_add` 不存在，用户用不了；这个项目不算完成。
2. **前端 dtype 规则和 verifier 不一致**：`_check_dtype` 允许 `uint32`，verifier 拒绝，端到端才炸。
3. **interpreter 没实现**：`TRITON_INTERPRET=1` 时 `create_sadd_sat` 不存在，测试直接 AttributeError。
4. **数值参考值用错**：用 `torch` 的 int8 加法做参考，得到回绕结果，测试“通过”但语义错。
5. **benchmark 不公平**：`clamp` 版本和 `saturating_add` 用了不同的 `BLOCK`/`num_warps`；结论无效。
6. **忘记 AMD**：上游 review 会问“AMD 后端怎么办”；至少要在 PR 里说明是 fallback 还是未实现。
7. **commit 混在一起**：op、lowering、前端、测试全在一个 commit，review 无法分层。

## 延伸方向

1. **`tl.saturating_cast`**：float → int 的饱和转换，涉及 rounding mode 和 NaN 处理。
2. **向量化 lowering**：如果 LLVM 有 `llvm.sadd.sat` 向量 intrinsic，比较逐元素 vs 向量版本的 PTX。
3. **AMD 后端**：CDNA 的 `v_add_sat` 是否可用；如果没有，fallback 到 `v_min/v_max`。
4. **Gluon 版本**：在 `python/triton/experimental/gluon/` 里暴露显式 layout 的 `saturating_add`。
5. **上游跟进**：提交后读 review 意见，把它变成 Ch 32 的案例研究。

## 提交前检查清单

- [ ] 设计说明一页，语义/类型/边界/失败模式齐全
- [ ] TTIR op + verifier + round-trip 测试
- [ ] lowering + lit（scalar/tensor/非法类型）
- [ ] Python API + binding + docstring
- [ ] interpreter 支持
- [ ] pytest 边界矩阵 + 随机 + interpreter
- [ ] benchmark（含 PTX/SASS 证据，或明确写“未测量”）
- [ ] AMD 路径说明
- [ ] commit 拆分 + PR 描述
- [ ] 能 revert，且不改变现有 IR 格式

## 结课标准

完成这个项目后，你应该能：

- 给 Triton 新增一个用户可见 op，并解释它在每一层的表示；
- 用 lit 锁 IR、用 pytest 锁数值、用 benchmark 锁性能；
- 按上游标准拆分 commit、写 PR 描述、回应 review；
- 在一个不熟悉的 pass/lowering 里定位问题，而不是靠猜。

如果这四件事你都能独立做到，本课的目标就达成了。
