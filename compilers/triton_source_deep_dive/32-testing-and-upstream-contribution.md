# Ch 32 — 测试体系与上游贡献

> 测试不是 PR 的附件，而是你对编译器行为写下的可执行声明。

## 目标

读完本章，你应该能：

- 说清 Triton 的四层测试各自锁住什么：lit、pytest、C++ 单测、interpreter；
- 读懂一个 lit 测试的 `RUN:` 行、`CHECK:` 行和 `-split-input-file`，并写出最小可复现的 IR 断言；
- 写一个带 `@pytest.mark.parametrize` 的 GPU 测试，以及一个能在无 GPU 环境跑的 interpreter 测试；
- 用 `pytest -k`、`-m interpreter`、`--device=cpu` 精准跑单测，而不是每次跑全量；
- 理解上游从 issue 到 merge 的流程、PR 模板、pre-commit、CI 矩阵和 review 关注点；
- 判断一个改动应该补哪一层测试，以及 reviewer 要求补 AMD 路径时该怎么拆。

本章不教你“如何让 CI 变绿”这种技巧。重点是：**测试要能在正确的层上失败**。一个只在 GPU 上跑、只比较最终数值的测试，无法告诉你 layout 或 pass 哪一步坏了；一个只检查 IR 的 lit 测试，也无法证明 kernel 在真实硬件上算得对。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码目录 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0` |
| GPU | Tesla T4，compute capability 7.5（`sm_75`） |
| 驱动 | 535.216.01 |
| CUDA toolkit | 12.4 |
| 系统 Python | 3.10.12 |
| 实验用 venv | 3.12.16，`torch 2.5.1+cu124`，`triton 3.9.0` wheel |
| lit 测试文件数 | 307 个 `.mlir` |
| pytest 测试文件数 | 77 个 `test_*.py` |
| C++ 单测文件数 | 10 个 `.cpp` |

**源码依据**：

- `test/lit.cfg.py:15-20` 设置 suite 名、`ShTest` 格式和 `.mlir/.ll` 后缀；
- `test/lit.cfg.py:45-64` 把 `triton-opt`、`triton-llvm-opt`、`mlir-translate`、`llc` 注册成 lit 的可用工具；
- `test/CMakeLists.txt:24-32` 定义 `check-triton-lit-tests`，并依赖 `triton-opt`、`triton-tensor-layout`、`triton-llvm-opt`；
- `Makefile:30-40` 把 `test-lit`、`test-cpp`、`test-unit` 映射到 ninja/pytest；
- `Makefile:66-71` 是 CI 的 interpreter 测试入口，显式设置 `TRITON_INTERPRET=1` 和 `--device=cpu`。

**guided**：本机源码树只有配置，没有构建出 `triton-opt`。`find build -name triton-opt` 没有结果，所以 `ninja check-triton-lit-tests` 和 `check-triton-unit-tests` 在本机没有实际运行。运行时实验用的是 `.venv` 里的 3.9.0 wheel；源码树没有改动。lit 和 C++ 单测的命令行按源码与 Makefile 给出，标 `guided`。

## 为什么需要这个

Triton 的 bug 会以完全不同的形态出现在不同层：

- 前端语义错：`mask` 广播错、`other` 没给、类型提升不符合预期；
- TTIR/TTGIR 错：`convert_layout` 多了一次、`tt.dot` 没被加速、pipeline 把 load 挪过了 barrier；
- lowering 错：地址空间错、`ldmatrix` 的 lane 映射错、`mbarrier` 少了一次 arrive；
- 数值错：fp8/bf16 的舍入、TF32 的精度、原子操作的顺序；
- 运行时错：launch 参数顺序、shared memory 大小、stream 同步。

如果你只有一个端到端数值测试，以上任何一层坏了，你都只看到“结果不对”。如果你只有一个 lit 测试，你可能锁住了一个错误但“看起来合法”的 IR。上游要求 lit + pytest 双测试，本质上是要求：**IR 层的行为和真实执行的行为都被钉住**。

## 先建立直觉

把四层测试想成一个金字塔：

```text
                 pytest (端到端、数值、runtime)
              C++ unittest (算法/数据结构)
           lit (IR、pass、verifier、lowering 形态)
        interpreter (Python 语义、无 GPU 快速回归)
```

| 层 | 目录 | 跑什么 | 锁住什么 | 不锁什么 |
|---|---|---|---|---|
| lit | `test/` | `triton-opt` + `FileCheck` | IR 形态、pass 效果、verifier 报错 | 真实 GPU 数值、launch |
| pytest | `python/test/` | `pytest` + 真实 GPU 或 interpreter | 端到端数值、API、runtime | 单条 IR 的细节 |
| C++ | `unittest/` | gtest/ctest | LinearLayout、swizzle、analysis 等纯算法 | Python API、GPU 执行 |
| interpreter | `python/triton/runtime/interpreter.py` | numpy 语义 | 前端语义、索引、mask | IR、PTX、性能、并发 |

一个典型改动应该这样分配测试：

- 改 `Coalesce` pass：lit 测试锁 layout，pytest 测一个真实 transpose/add 的数值；
- 改 `LinearLayout` 的某个 basis：C++ 单测锁矩阵性质，lit 锁 TTGIR 输出；
- 改 `tl.load` 的语义：pytest 的 interpreter 测试锁前端行为，GPU 测试锁真实 lowering；
- 改 `do_bench`：pytest 锁统计行为，不需要 lit。

## 核心概念

### 1. lit：把 IR 当成可执行规格

lit 使用 `ShTest` 格式。一个 `.mlir` 文件就是一个 shell 脚本的变体：`RUN:` 行描述命令，`CHECK:` 行描述期望输出。Triton 的 lit 配置在 `test/lit.cfg.py`：

- `config.name = 'TRITON'`（`15`）；
- `config.test_format = lit.formats.ShTest()`（`17`）；
- `config.suffixes = ['.mlir', '.ll']`（`20`）；
- `config.excludes` 排除 `Inputs`、`CMakeLists.txt` 等（`38`）；
- `tool_dirs` 包含构建目录的 `bin` 和 LLVM 的 `bin`（`45-53`）；
- `tools` 注册 `triton-opt`、`triton-llvm-opt`、`mlir-translate`、`llc`（`58-64`）；
- `FILECHECK_OPTS = "--enable-var-scope"`（`51`），避免跨 `CHECK-LABEL` 复用变量。

最小例子是 `test/Triton/ops.mlir:1-11`：

```mlir
// RUN: triton-opt %s | FileCheck %s

// CHECK-LABEL: @cast_ops
tt.func @cast_ops(%scalar_ptr: !tt.ptr<f32>, %scalar_f32: f32, %scalar_i64: i64) {
  // scalar -> scalar
  // CHECK:  i64 -> !tt.ptr<f32>
  %0 = tt.int_to_ptr %scalar_i64 : i64 -> !tt.ptr<f32>
  // CHECK: !tt.ptr<f32> -> i64
  %1 = tt.ptr_to_int %scalar_ptr : !tt.ptr<f32> -> i64
  // CHECK: f32 to f16
  %2 = arith.truncf %scalar_f32 : f32 to f16
```

这个测试没有 pass，只是 `triton-opt %s` 解析并打印 IR，然后 `FileCheck` 按顺序找三行。它锁住的是：parser 能接受这些 op，printer 能打印这些类型转换。

更有代表性的是 `test/TritonGPU/coalesce.mlir:1-20`：

```mlir
// RUN: triton-opt %s -split-input-file -tritongpu-coalesce | FileCheck %s

// CHECK: [[row_layout:#.*]] = #ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>
// CHECK: [[col_layout:#.*]] = #ttg.blocked<{sizePerThread = [4, 1], threadsPerWarp = [16, 2], warpsPerCTA = [1, 4], order = [0, 1]}>
// CHECK: [[load_ptr:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64x!tt.ptr<f32>, [[row_layout]]>
// CHECK: [[load_mask:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64xi1, [[row_layout]]>
// CHECK: [[load_other:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64xf32, [[row_layout]]>
// CHECK: [[load_val:%.*]] = tt.load [[load_ptr]], [[load_mask]], [[load_other]] : tensor<64x64x!tt.ptr<f32>, [[row_layout]]>
// CHECK: [[store_ptr:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64x!tt.ptr<f32>, [[col_layout]]>
// CHECK: [[store_val:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64xf32, [[col_layout]]>
// CHECK: [[store_mask:%.*]] = ttg.convert_layout {{.*}} -> tensor<64x64xi1, [[col_layout]]>
// CHECK: tt.store [[store_ptr]], [[store_val]], [[store_mask]]
```

它锁住的是：`-tritongpu-coalesce` 之后，load 用 row-major layout，store 用 column-major layout，并且两者之间插入了 `ttg.convert_layout`。这是“pass 的可执行规格”，比读 `Coalesce.cpp` 更直接。

### 2. pytest：端到端行为和 API

pytest 测试在 `python/test/unit/` 下，按目录分：

- `language/`：`test_core.py`、`test_matmul.py`、`test_standard.py`、`test_random.py`；
- `runtime/`：launch、autotuner、cache；
- `backend/`、`cuda/`、`plugins/`、`tools/`、`instrumentation/`；
- `test_debug.py`、`test_filecheck.py`、`test_knobs.py`。

`python/conftest.py:7-13` 定义 `--device` 选项和 `device` fixture。默认是 `cuda`，所以 CI 的 GPU 测试直接跑；interpreter 测试用 `--device=cpu`。

一个 GPU 测试的典型形状是 `python/test/unit/language/test_matmul.py:130-197`：

```python
@pytest.mark.parametrize("dtype_src_str", ["float32", "tensorfloat32", "float16", "float8e5", "float64"])
@pytest.mark.parametrize("dtype_dst_str", ["float32", "float16", "float64"])
@pytest.mark.parametrize("BLOCK_M, BLOCK_N, BLOCK_K, NUM_STAGES",
                         [(128, 128, 16, 4), (64, 128, 32, 4), (32, 32, 32, 4)])
@pytest.mark.parametrize("NUM_CTAS", [1, 2])
@pytest.mark.parametrize("NUM_WARPS", [4, 8])
def test_simple_matmul(dtype_src_str, dtype_dst_str, BLOCK_M, BLOCK_N, BLOCK_K,
                       NUM_STAGES, NUM_WARPS, NUM_CTAS, device, EPILOGUE_SUBTILE):
    if NUM_CTAS > 1 and (not is_cuda() or torch.cuda.get_device_capability()[0] < 9):
        pytest.skip("Clusters requires nvidia compute capability >= 9")
    ...
    k = matmul_kernel[grid](a, b, output, M, N, K, ...,
                            BLOCK_M, BLOCK_N, BLOCK_K, NUM_STAGES=NUM_STAGES,
                            num_warps=NUM_WARPS, num_ctas=NUM_CTAS)
    ref_out = torch.matmul(A, B).to(torch.float32)
    torch.testing.assert_close(ref_out, output, atol=atol, rtol=rtol)
```

它做了三件事：

1. 用 `pytest.skip` 把不支持的组合（多 CTA、fp8、shared memory 不够）排除；
2. 用真实 GPU 跑 kernel；
3. 用 `torch.testing.assert_close` 比较数值，而不是只看“没崩”。

一个 interpreter 测试的典型形状是 `python/test/unit/language/test_core.py:153-163`：

```python
@pytest.mark.interpreter
def test_scalar_overflow(device):
    @triton.jit
    def kernel():
        huge_int: tl.constexpr = 0xFFFFFFFFFFFFFF
        x = tl.full((), 32, dtype=tl.int32)
        y = x + huge_int

    with pytest.raises(triton.TritonError, match="out of range"):
        kernel[(1, )]()
```

`@pytest.mark.interpreter` 让这个测试可以被 `-m interpreter` 选中；它不需要 GPU，因为它只走前端语义检查。

### 3. C++ 单测：纯算法用 gtest

`unittest/` 下的测试用 GoogleTest。`cmake/AddTritonUnitTest.cmake:6-37` 的 `add_triton_ut` 会注册 `add_test`、链接 `GTest::gtest_main`/`gmock`、用 `gtest_discover_tests` 接入 ctest。`CMakeLists.txt:539-546` 在 `TRITON_BUILD_UT` 打开时创建 `check-triton-unit-tests`，`Makefile:34-36` 把它包成 `make test-cpp`。

`unittest/Dialect/TritonGPU/SwizzleTest.cpp:307-333` 是一个典型：

```cpp
TEST_F(SwizzleTest, AdditiveStridesRejectsTransitiveOverlap) {
  LinearLayout reps(...), addrLayout(...);
  auto result = actionAdditiveStrides(reps, addrLayout, 0, 0, 8);
  EXPECT_EQ(result.first, 8);
}
```

它不启动 GPU，也不解析 MLIR 文件；它直接构造 `LinearLayout`，调用算法，断言 stride。这类测试适合锁“算法不变量”，不适合锁“pass 在 pipeline 里的行为”。

### 4. interpreter：无 GPU 的前端回归

`TRITON_INTERPRET=1` 时，`@triton.jit` 不会返回 `JITFunction`，而是返回 `InterpretedFunction`。源码依据：

- `python/triton/knobs.py:471` 定义 `interpret`；
- `python/triton/runtime/jit.py:1025-1030` 在 `knobs.runtime.interpret` 为真时导入并返回 `InterpretedFunction`；
- `python/triton/runtime/interpreter.py:1674-1694` 是 `InterpretedFunction.run`，它不编译，而是重写 AST 后交给 `GridExecutor`；
- `python/triton/runtime/interpreter.py:1349-1361` 的 `_patch_lang` 把 `tl.*` 替换成 numpy 语义的实现。

**实测**：下面这个脚本在 `CUDA_VISIBLE_DEVICES=''` 下运行成功：

```python
import os, torch, triton, triton.language as tl
print('TRITON_INTERPRET =', os.environ.get('TRITON_INTERPRET'))
print('cuda available =', torch.cuda.is_available())

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    offs = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    tl.store(out_ptr + offs, tl.load(x_ptr + offs, mask=mask) + tl.load(y_ptr + offs, mask=mask), mask=mask)

n = 1000
x = torch.randn(n, dtype=torch.float32)
y = torch.randn(n, dtype=torch.float32)
out = torch.empty_like(x)
add_kernel[(triton.cdiv(n, 256),)](x, y, out, n, BLOCK=256)
torch.testing.assert_close(out, x + y)
print('max error =', (out - (x + y)).abs().max().item())
print('kernel type =', type(add_kernel).__name__)
```

输出：

```text
TRITON_INTERPRET = 1
cuda available = False
max error = 0.0
kernel type = InterpretedFunction
```

这说明 interpreter 确实不需要 GPU。但它也不生成 TTIR/TTGIR/PTX，不能用来验证 layout、lowering 或性能。

### 5. CI：四层测试怎么被串起来

`.github/workflows/ci.yml:1-36` 在 `pull_request`、`merge_group`、`push main` 上触发，把 `pre-commit`、NVIDIA、AMD、macOS 分成独立 job。NVIDIA 的步骤在 `.github/workflows/integration-tests-nvidia.yml:113-129`：`test-unit` → `test-gsan`/`test-interpret` → `test-regression` → `test-microbenchmark` → `test-cpp`。lit 在更早的 `make test-lit`。CI 不是“跑一个 pytest”，而是按层跑完整矩阵。

## 底层机制

### lit 的执行链

lit 不是直接读源码目录，而是读构建目录里的 `lit.site.cfg.py`。`test/lit.site.cfg.py.in:5-24` 把 CMake 变量填进去：

- `config.triton_obj_root = "@triton_BINARY_DIR@"`；
- `config.llvm_tools_dir`、`config.llvm_lib_dir`；
- `config.python_executable`；
- 最后 `lit_config.load_config(..., "@triton_SOURCE_DIR@/test/lit.cfg.py")`。

所以 `ninja check-triton-lit-tests` 实际是：用构建目录的配置，找到 `triton-opt`，对每个 `.mlir` 执行 `RUN:` 行，再用 `FileCheck` 比对。

一个最小但完整的 lit 测试应该：

1. 只包含触发问题的 op；
2. `RUN:` 只跑必要的 pass，不要跑整个 pipeline；
3. `CHECK:` 用 `CHECK-LABEL` 分段，用变量捕获 SSA 名；
4. 不依赖未定义的顺序；必要时用 `CHECK-NEXT` 或 `CHECK-SAME`；
5. 对“应该失败”的输入用 `not triton-opt ... 2>&1 | FileCheck`。

### pytest 的收集与跳过

pytest 的收集顺序是：`conftest.py` 提供 `--device`，测试模块导入 `triton`，`@pytest.mark.parametrize` 展开参数，`pytest.skip` 在运行时决定是否跳过。

这带来两个常见现象：

- 一个 `test_simple_matmul` 可能展开出几百个 case；用 `-k` 过滤比跑全量快得多；
- 同一个测试在 T4 上会跳过 `NUM_CTAS=2`、fp8、shared memory 不够的 case（`test_matmul.py:140-161`），所以“本地全绿”不等于“所有架构全绿”。

### C++ 单测的构建边界

`check-triton-unit-tests` 只在 `TRITON_BUILD_UT` 打开时存在（`CMakeLists.txt:539-546`）。如果你用 wheel 或默认构建，可能根本没有这个 target。先确认：

```bash
cd /home/ubuntu/aicomp/triton
grep -n "TRITON_BUILD_UT" CMakeLists.txt
ninja -C build/cmake.linux-x86_64-cpython-3.12 -t targets | grep check-triton-unit-tests
```

如果第二条没有输出，就不要在 PR 里写“C++ 测试已跑”；要么打开 `TRITON_BUILD_UT` 重新构建，要么明确说明未跑。

### 上游流程

Triton 的贡献流程可以拆成七步：

1. **issue**：先搜已有 issue；bug report 用 `.github/ISSUE_TEMPLATE/bug.yml`，performance 用 `performance.yml`。
2. **fork/branch**：从 `main` 切一个描述性的分支，不要在 `main` 上直接改。
3. **小 commit**：一个 commit 只做一件事；格式修正和逻辑修正分开。
4. **本地测试**：至少跑受影响的 lit/pytest；改 C++ 就加 C++ 单测；改前端就加 interpreter 测试。
5. **PR 描述**：按 `.github/PULL_REQUEST_TEMPLATE.md:11-30` 填“为什么”和测试清单，不要只写“fix bug”。
6. **CI**：pre-commit、lit、pytest、C++、regression、microbenchmark、AMD/NVIDIA 矩阵。
7. **review/merge**：reviewer 关注正确性、测试层、兼容性、性能取舍；merge 由 maintainer 做。

**源码依据**：`CONTRIBUTING.md:9-11` 明确说 maintainer 没有及时 review 的义务，贡献者要在发 PR 前自己把质量门槛拉高；`CONTRIBUTING.md:68` 说功能 bug fix 必须有最小化单测，性能改进如果“复杂度/收益”不划算会被拒。

**关于 DCO**：本仓库的 PR 模板和 CI 配置里没有 DCO/CLA 检查，`.github/PULL_REQUEST_TEMPLATE.md` 也没有要求 `Signed-off-by`。不要把其他开源项目的 DCO 规则照搬过来。真正被 CI 检查的是 `.pre-commit-config.yaml:1-45` 里的 hooks 和 `.github/workflows/pre-commit.yml:30-33` 的 `pre-commit run --all-files`。

### code style

`.pre-commit-config.yaml` 的关键 hooks：

- `trailing-whitespace`、`end-of-file-fixer`、`check-yaml`、`check-toml`、`check-ast`；
- `ruff`（`20-25`）；
- `yapf`（`31-35`）；
- `clang-format` v19.1.6（`37-40`）；
- `mypy`（`42-45`）。

C++ 的格式基线是 `.clang-format` 的 `BasedOnStyle: LLVM`。Python 侧 ruff + yapf 可能同时改格式，所以本地先跑：

```bash
pre-commit run --from-ref origin/main --to-ref HEAD
```

不要手动调格式再让 pre-commit 改一遍；这会让 diff 里混入无关行。

## 进一步用法/重要变体

### 只跑一个 lit 测试

**guided**（需要构建出 `triton-opt`）：

```bash
cd /home/ubuntu/aicomp/triton
build/cmake.linux-x86_64-cpython-3.12/bin/triton-opt \
  test/TritonGPU/coalesce.mlir -split-input-file -tritongpu-coalesce \
  | build/cmake.linux-x86_64-cpython-3.12/bin/FileCheck test/TritonGPU/coalesce.mlir
```

更常用的是让 lit 只跑一个文件：

```bash
python -m lit -v test/TritonGPU/coalesce.mlir
```

`lit` 会读构建目录的 site config；如果直接对源码目录跑，可能找不到 `triton-opt`。

### 只跑一个 pytest case

**实测**：下面这个自包含测试文件放在 `/tmp/triton_tests/test_demo.py`，用 wheel 的 Triton 和 CPU torch tensor 跑 interpreter：

```python
import pytest, torch, triton, triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    offs = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    tl.store(out_ptr + offs, tl.load(x_ptr + offs, mask=mask) + tl.load(y_ptr + offs, mask=mask), mask=mask)

@pytest.mark.parametrize("n", [1000, 4096])
def test_add(n):
    x = torch.randn(n); y = torch.randn(n); out = torch.empty_like(x)
    add_kernel[(triton.cdiv(n, 256),)](x, y, out, n, BLOCK=256)
    torch.testing.assert_close(out, x + y)
```

运行：

```bash
cd /tmp/triton_tests
CUDA_VISIBLE_DEVICES='' TRITON_INTERPRET=1 \
  /home/ubuntu/aicomp/triton/.venv/bin/python -m pytest test_demo.py \
  -k "add and not wrong" -q
```

**实测输出**：

```text
..                                                                       [100%]
2 passed, 1 deselected in 1.56s
```

注意 `-k "add and not wrong"`：实际文件里还有一个 `test_add_wrong_shape`（上面为了篇幅省略），只写 `-k add` 会把它也选中。我第一次跑时就遇到了这个：`1 failed, 2 passed`，因为 interpreter 对那个错误 shape 没有抛异常。`-k` 是子串匹配，不是精确匹配。

### 在仓库里跑单测

**guided**（需要可 import 的源码构建）：

```bash
cd /home/ubuntu/aicomp/triton
python -m pytest python/test/unit/language/test_core.py -k "test_bin_op" -q
python -m pytest python/test/unit/language/test_matmul.py -k "simple_matmul" -q
python -m pytest python/test/unit -m interpreter --device=cpu -q
```

`-m interpreter` 只选带 `@pytest.mark.interpreter` 的测试；`--device=cpu` 让 fixture 走 CPU 分支。**实测踩坑**：直接在源码树里跑 `pytest python/test/unit/language/test_core.py` 时，pytest 会把 `python/` 加入 `sys.path`，导入未构建的 `python/triton`，报 `cannot import name 'getenv' from 'triton._C.libtriton'`。这是源码包和 wheel 混用，不是测试坏了。

### interpreter 的正确用法

```bash
TRITON_INTERPRET=1 python your_kernel.py
```

适合验证索引、mask、广播、控制流，以及无 GPU 的快速回归；不适合验证 layout、shared memory、barrier、TMA、PTX/SASS、性能或多 CTA 竞争。

### PR 的最小完整形态

```text
标题： [Frontend] Fix mask broadcast for scalar pointer + block mask
问题：  tl.load(scalar_ptr, mask=block_mask) 没有报错，生成了错误 IR
改动：  semantic.py 增加 scalar pointer + block mask 的检查
测试：  pytest 新增 case；verifier 也应拒绝时补 lit case
验证：  pytest -k ...；check-triton-lit-tests；pre-commit
风险：  只影响前端检查；不改变合法输入的 IR
```

不要在一个 PR 里同时做“重构 + 修 bug + 调格式 + 加新功能”。reviewer 无法判断哪一行是修复，CI 失败时也无法 bisect。

### reviewer 要求补 AMD 路径

先判断改动是否真的跨后端：只改 `third_party/nvidia` 时 AMD 不需要跑；改 `lib/`、`include/triton`、`python/triton/language` 时 AMD 很可能受影响。共享层改动优先在 backend 层做能力检查，不要用 `#ifdef NVIDIA` 切开。补一个 `test/TritonGPU/amd/` 或 `test/Conversion/amd/` 的 lit 测试，跑 `.github/workflows/ci.yml:31-36` 的 AMD job；没有 ROCm 就标 `guided` 并说明。**源码依据**：AMD stage 表在 `third_party/amd/backend/compiler.py:948-957`。

## 对比与取舍

| 维度 | lit | pytest | C++ unittest | interpreter |
|---|---|---|---|---|
| 运行速度 | 快（毫秒级） | 慢（编译 + launch） | 快（纯 CPU） | 中（numpy 逐元素） |
| 需要 GPU | 否 | 通常是 | 否 | 否 |
| 锁住 IR | 是 | 间接 | 否 | 否 |
| 锁住数值 | 否 | 是 | 部分 | 是（前端语义） |
| 锁住 layout/pass | 是 | 间接 | 部分 | 否 |
| 维护成本 | 中（CHECK 易碎） | 高（参数爆炸） | 低 | 中（语义模拟不全） |
| 适合的改动 | pass/verifier/lowering | API/runtime/数值 | 算法/数据结构 | 前端语义 |

取舍原则：

- **能在一层锁住，就不要在两层重复锁**；但“IR 形态”和“数值结果”是不同维度，通常需要两层。
- **lit 测试要最小**：一个测试只验证一个 pass 的一个行为；不要把一个完整 kernel 的 pipeline 输出全部 CHECK 住，否则任何无关 pass 改动都会让它失败。
- **pytest 要控制参数规模**：`test_simple_matmul` 的组合是指数级的；新增参数时先想清楚是否真的需要全组合。
- **C++ 单测不要依赖 GPU**：它应该能在没有 CUDA 的机器上跑，否则就失去了“纯算法快速回归”的意义。
- **interpreter 不是 GPU 的替代品**：它能抓前端错，但抓不到 layout/lowering 错；不要用它给 GPU 正确性背书。

## 踩雷集锦

1. **`-k` 是子串匹配，不是精确匹配。**
   我跑 `pytest test_demo.py -k add` 时，`test_add_wrong_shape` 也被选中，结果错误 shape 的 case 失败。用 `-k "add and not wrong"` 或直接写 node id。

2. **lit 测试在本地过、CI 不过。**
   常见原因是 `CHECK` 依赖了未捕获的 SSA 名、`CHECK-NEXT` 中间多了一行、或者 `FILECHECK_OPTS` 的变量作用域不同。用 `[[name:%.*]]` 捕获，用 `CHECK-LABEL` 分段。

3. **pytest 测试只写 GPU 路径。**
   没有 GPU 的 CI 会直接 skip，等于没测。需要无 GPU 覆盖时，加 `@pytest.mark.interpreter` 并放到 `test-interpret` 的列表里（`Makefile:66-71`）。

4. **interpreter 通过就以为 GPU 也对。**
   interpreter 不生成 layout，也不执行 barrier/TMA。一个 `tt.dot` 的 layout 错、一个 shared memory 越界，interpreter 完全看不到。

5. **在源码树里直接跑 pytest，导入到未构建的 `python/triton`。**
   报 `cannot import name 'getenv' from 'triton._C.libtriton'`。先确认 `python -c "import triton; print(triton.__file__)"` 指向的是 editable 构建还是 wheel。

6. **C++ 测试没开 `TRITON_BUILD_UT`。**
   `check-triton-unit-tests` target 不存在，却在 PR 里写“C++ tests pass”。先 `ninja -t targets | grep check-triton-unit-tests` 确认。

7. **PR 只加 pytest，不加 lit。**
   如果改动影响 pass/IR 形态，reviewer 通常会要求 lit 测试。pytest 只能告诉你“数值对了”，不能告诉你“IR 里没有多一次 convert_layout”。

8. **把 DCO 当成 Triton 的硬门槛。**
   本仓库的 PR 模板和 CI 配置里没有 DCO 检查；PR 模板要求的是 pre-commit 和测试。不要因为缺少 `Signed-off-by` 而阻塞自己，也不要以为加上它就万事大吉。

9. **AMD reviewer 要求补测试时，只改 NVIDIA 代码。**
   如果改动在共享层，AMD 的 lit 测试是必须的；如果只在 `third_party/nvidia`，在 PR 描述里说明“AMD 不受影响”并给出理由。

## 进阶

- **用 `git bisect run` 自动定位回归**：写一个只跑单个 lit/pytest 的脚本，返回 0/1，然后 `git bisect run ./check.sh`。lit 测试特别适合，因为它不依赖 GPU。
- **用 `triton-reduce` 缩小失败用例**：源码树有 `bin/triton-reduce.cpp`，它可以把一个失败的 MLIR reproducer 缩到更小；具体用法取决于构建，标 `guided`。
- **给新 op 写三层测试**：TTIR verifier 的 lit 测试、TTGIR lowering 的 lit 测试、端到端 pytest。三层都过，才说明 op 从定义到执行是通的。
- **模拟 review**：把 PR 当成别人的代码，问四个问题：这个测试会在哪一层失败？它覆盖了哪个边界？它会不会在 AMD 上挂？它的维护成本是否值得？
- **读 CI 的 cache/warmup 逻辑**：`.github/workflows/integration-tests-nvidia.yml:103-112` 先 warm compile cache，再跑 `test-unit`；这不是优化，而是让测试在稳定状态下跑，避免把编译时间算进超时。
- **flaky test 的处理**：GPU 测试的随机数、时钟、并发都可能让结果不稳定；先固定 seed、减少参数、加 `torch.cuda.synchronize()`，再考虑 skip。

## 动手练习

1. **写一个 lit 测试**：给 `tt.addptr` 的 canonicalization 写一个最小 `.mlir`，只包含一个 `tt.addptr`，`RUN:` 跑 `triton-opt -canonicalize`，`CHECK:` 断言常量折叠后的结果。不要复制 `coalesce.mlir` 的整段 layout。
2. **写一个 pytest GPU 测试**：参数化 `n in [1, 255, 256, 257, 1000]`，测试带 mask 的 vector add；对 `n` 不被 `BLOCK` 整除的 case 重点检查尾部。
3. **跑一次 `-k`**：把本章的 `/tmp/triton_tests/test_demo.py` 跑起来，先用 `-k add`，再用 `-k "add and not wrong"`，解释两次选择的差异。
4. **跑一次 interpreter**：设置 `CUDA_VISIBLE_DEVICES=''` 和 `TRITON_INTERPRET=1`，确认 `torch.cuda.is_available()` 是 `False`，但 kernel 仍然能跑。
5. **triage**：给你三个失败现象——lit 的 CHECK 不匹配、pytest 数值错、C++ 单测断言失败——分别说出第一层应该看什么、需要什么证据。
6. **写 PR 描述**：选一个你写过的小 bug fix，按“问题/改动/测试/验证/风险”五段写一版，控制在 15 行以内。
7. **guided**：如果有 ROCm 机器，给一个共享层的改动补 `test/TritonGPU/amd/` 的 lit 测试，并跑 `make test-lit`；没有机器就写出测试文件和预期 CHECK，不要伪造运行结果。

## 本章重点

- Triton 的测试是四层的：lit 锁 IR，pytest 锁端到端，C++ 单测锁算法，interpreter 锁前端语义。
- lit 的 `RUN:` 是命令，`CHECK:` 是断言；`triton-opt` 和 `FileCheck` 由 `test/lit.cfg.py` 注册，`check-triton-lit-tests` 由 `test/CMakeLists.txt` 定义。
- pytest 用 `--device`、`@pytest.mark.interpreter`、`pytest.skip` 管理 GPU/CPU/架构差异；`-k` 是子串匹配。
- C++ 单测走 gtest/ctest，`add_triton_ut` 注册，`check-triton-unit-tests` 只在 `TRITON_BUILD_UT` 打开时存在。
- `TRITON_INTERPRET=1` 返回 `InterpretedFunction`，不生成 IR/PTX；它能抓前端错，不能替代 GPU 测试。
- 上游要求 lit + pytest 双测试，是因为它们锁的是不同维度；一个 pass 改动通常两层都要。
- 最小完整 PR = 问题 + 小改动 + 正确层的测试 + 本地验证 + 风险说明。
- reviewer 要求补 AMD 时，先判断是否跨后端；共享层改动要加 AMD lit 测试，NVIDIA-only 改动要说明理由。

## 自我检核

1. 为什么一个 `Coalesce` pass 的改动通常需要 lit + pytest 两层测试？各锁住什么？
2. `test/lit.cfg.py` 里的 `tools` 列表和 `tool_dirs` 分别解决什么问题？
3. `pytest -k add` 和 `pytest -k "add and not wrong"` 的选择差异是什么？为什么会这样？
4. `TRITON_INTERPRET=1` 时，`@triton.jit` 返回什么？它为什么不生成 TTIR？
5. 为什么 C++ 单测不应该依赖 GPU？举一个适合放在 `unittest/` 的例子。
6. 一个 PR 只改了 `third_party/nvidia/backend/compiler.py`，reviewer 要求补 AMD 测试，你应该怎么回应？
7. 本仓库的 PR 模板/CI 配置里有 DCO 检查吗？PR 模板真正要求的是什么？
8. 如果 lit 在本地过、CI 不过，你会先检查哪三件事？

## 延伸阅读

1. **MLIR Testing Guide — FileCheck Best Practices**
   - **读哪里**：`https://mlir.llvm.org/getting_started/TestingGuide/` 的 “FileCheck Best Practices” 和 “tests should be minimal”。
   - **学什么**：如何写最小、稳定、不依赖无关 pass 的 `CHECK`；为什么“跑 Python 生成 IR 再 CHECK”通常不是最小测试。
   - **与本章关系**：直接对应 `.github/PULL_REQUEST_TEMPLATE.md:26-30` 对 lit 测试的要求。

2. **`test/lit.cfg.py`、`test/lit.site.cfg.py.in`、`test/CMakeLists.txt`**
   - **读哪里**：`15-75` 的 suite/tool 配置，`24-32` 的 `check-triton-lit-tests`。
   - **学什么**：lit 如何找到 `triton-opt`、`FileCheck`，以及构建目录和源码目录的分工。
   - **与本章关系**：本章所有 lit 命令行的依据；换 commit 后要重新核对行号。

3. **`python/test/unit/language/test_core.py`、`test_matmul.py`、`python/conftest.py`**
   - **读哪里**：`test_core.py:153-163` 的 interpreter 测试，`test_matmul.py:130-197` 的 GPU 参数化测试，`conftest.py:7-13` 的 `--device`。
   - **学什么**：Triton 如何组织参数化、skip、数值比较和 CPU/GPU 分流。
   - **与本章关系**：pytest 层的最佳范本；写新测试时先找同目录的同类测试。

4. **`unittest/Dialect/TritonGPU/SwizzleTest.cpp`、`cmake/AddTritonUnitTest.cmake`**
   - **读哪里**：`SwizzleTest.cpp:307-333` 的 `TEST_F`，`AddTritonUnitTest.cmake:6-37` 的 `add_triton_ut`。
   - **学什么**：C++ 单测如何构造 MLIR 对象、如何注册到 ctest、如何链接 Triton 库。
   - **与本章关系**：C++ 层的具体写法；理解为什么它不需要 GPU。

5. **`.github/workflows/ci.yml`、`integration-tests-nvidia.yml`、`pre-commit.yml`、`.pre-commit-config.yaml`**
   - **读哪里**：`ci.yml:1-36` 的触发器和 job，`integration-tests-nvidia.yml:113-129` 的测试步骤，`pre-commit.yml:30-33` 的格式检查。
   - **学什么**：上游 CI 实际跑什么、顺序是什么、哪些检查会阻塞 PR。
   - **与本章关系**：把“本地测试”映射到“上游门槛”；避免在 PR 里声称跑了没跑的东西。

## 下一章

下一章从“正确”转向“快”：`do_bench` 怎么测、`ncu` 看什么、occupancy 和 roofline 怎么算、`num_warps`/`num_stages` 在 T4 上为什么不按直觉工作，以及维护一个编译器后端要背哪些长期负担。

→ [Ch 33 性能工程与长期维护](./33-performance-engineering-and-maintenance.md)
