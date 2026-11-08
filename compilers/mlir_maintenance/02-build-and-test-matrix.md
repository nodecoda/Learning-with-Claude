# Ch 02 — 构建系统与测试矩阵

> **目标**：知道同一个 MLIR 源码在不同 CMake 选项下会变成哪种二进制，能选对测试入口、看懂 lit 的 unsupported/excluded 数字，并为不同层级的改动选择正确的回归集合。

> **环境**：`builds/ct-instr-trunk`，`RelWithDebInfo`、assertions ON、shared libs、`clang;lld;mlir`；全量 lit 基线 4,947 discovered / 4,303 passed / 0 failed。

## 02.1 为什么“构建矩阵”是维护能力的一部分

同一个 `mlir-opt`，在四个维度上可以完全不同：

| 维度 | 选项 | 行为差异 |
|---|---|---|
| 优化/调试 | `CMAKE_BUILD_TYPE=RelWithDebInfo` / `Debug` | 速度、栈回溯、变量可见性 |
| 断言 | `LLVM_ENABLE_ASSERTIONS=ON/OFF` | `assert`、`llvm_unreachable` 是否触发 |
| 链接 | `BUILD_SHARED_LIBS=ON/OFF` | `.so` vs `.a`，增量链接速度、部署方式 |
| 项目 | `LLVM_ENABLE_PROJECTS=...` | 有没有 MLIR、clang、lld；测试能不能跑端到端 |

维护者最常遇到的“灵异现象”都能在这四维里找到解释：

- 你本地断言构建崩了，CI 的 release 构建却过了：因为不变量只在 `assert` 里检查。
- 你本地 shared 构建能跑，下游 static 链接报重复符号：因为 shared 隐藏了符号冲突。
- 你只开了 `mlir`，某些 lit 测试直接 unsupported：因为它们需要 clang/lld。
- 你没开 `MLIR_ENABLE_PDL_IN_PATTERNMATCH`，Transform 测试报 unknown argument：因为测试 pass 没注册。

所以“会跑测试”不等于“会选测试矩阵”。本章把矩阵拆开，并给出按改动层选回归集合的规则。

## 02.2 先建立直觉：四个开关决定你在测什么

```
                         ┌─────────────────────────────┐
                         │  CMAKE_BUILD_TYPE           │
                         │  Debug / RelWithDebInfo     │
                         └──────────────┬──────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
   ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
   │ assertions ON/OFF│     │ shared/static    │     │ projects         │
   │ 不变量是否检查    │     │ 链接与符号边界    │     │ 工具/测试是否可用 │
   └────────┬─────────┘     └────────┬─────────┘     └────────┬─────────┘
            │                        │                        │
            └────────────┬───────────┴────────────┬───────────┘
                         ▼                        ▼
              ┌────────────────────┐   ┌─────────────────────────┐
              │ mlir-opt 的行为     │   │ lit 的 available_features│
              │ 崩溃/诊断/性能      │   │ asserts / expensive / ...│
              └────────────────────┘   └─────────────────────────┘
```

关键点：**构建选项不只影响性能，还会改变测试的“可见集合”**。同一个测试文件，在 assertions 构建里可能有一个 `REQUIRES: asserts`，在 release 构建里就直接 excluded。

## 02.3 读 CMake cache：不要猜配置

任何“为什么这个测试没跑/为什么这个工具没有”的问题，第一步都是看 cache：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
grep -E 'CMAKE_BUILD_TYPE:|LLVM_ENABLE_ASSERTIONS:|BUILD_SHARED_LIBS:|LLVM_ENABLE_PROJECTS:|MLIR_ENABLE_EXECUTION_ENGINE:|MLIR_INCLUDE_TESTS:|MLIR_ENABLE_BINDINGS_PYTHON:|MLIR_ENABLE_CUDA_RUNNER:|MLIR_ENABLE_ROCM_RUNNER:|MLIR_ENABLE_PDL_IN_PATTERNMATCH:' \
  "$T/CMakeCache.txt"
```

本机实测：

```text
BUILD_SHARED_LIBS:BOOL=ON
CMAKE_BUILD_TYPE:STRING=RelWithDebInfo
LLVM_ENABLE_ASSERTIONS:BOOL=ON
LLVM_ENABLE_PROJECTS:STRING=clang;lld;mlir
MLIR_ENABLE_BINDINGS_PYTHON:BOOL=0
MLIR_ENABLE_CUDA_RUNNER:BOOL=0
MLIR_ENABLE_EXECUTION_ENGINE:BOOL=ON
MLIR_ENABLE_PDL_IN_PATTERNMATCH:BOOL=ON
MLIR_ENABLE_ROCM_RUNNER:BOOL=0
MLIR_INCLUDE_TESTS:BOOL=ON
```

逐项解释：

- `MLIR_ENABLE_EXECUTION_ENGINE=ON`：有 JIT，`mlir-runner` 和 `ExecutionEngine` 测试可用。
- `MLIR_INCLUDE_TESTS=ON`：生成 `check-mlir`、test dialect、test pass；关掉它，`mlir-opt` 里很多 `-test-*` 会消失。
- `MLIR_ENABLE_BINDINGS_PYTHON=0`：Python binding 测试会 unsupported；本课不依赖 Python。
- `MLIR_ENABLE_CUDA_RUNNER=0` / `ROCM_RUNNER=0`：GPU 端到端测试会 unsupported；这不代表 GPU lowering 不可用。
- `MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON`：Transform/PDL 测试 pass 可用；关掉会造成 9 个假失败。

### 为什么用 `toolset/configure.sh` 而不是手写 cmake

因为手写很容易漏掉一致性：

- `LLVM_USE_LINKER=lld`：不设会用默认链接器，链接时间和内存都变差；
- `LLVM_CCACHE_BUILD=ON`：不设会全量重编；
- `LLVM_INCLUDE_EXAMPLES=OFF`：不设会把 examples 也编了；
- `CMAKE_EXPORT_COMPILE_COMMANDS=ON`：不设就没有 `compile_commands.json`，clangd/clang-tidy 不可用。

本课只通过环境变量加项目：

```bash
LLVM_ENABLE_PROJECTS="clang;lld;mlir" \
  "$LLVM_HOME"/toolset/configure.sh ct-instr-trunk
```

## 02.4 构建目标：build-only、check、单工具

MLIR 的测试目标分两类：

| 目标 | 作用 | 什么时候用 |
|---|---|---|
| `check-mlir-build-only` | 只编测试依赖，不跑 | CI 分阶段；本地先确保工具齐全 |
| `check-mlir` | 编依赖并跑全量 lit | 提交前/CI |
| `mlir-opt` 等单工具 | 只编一个工具 | 日常快速迭代 |
| `MLIR<Dialect>Tests` 等单测 | 编 C++ unit test | 改 core/analysis 时 |

验证测试依赖已齐：

```bash
cd "$T"
ninja -n check-mlir-build-only | tail -3
```

本机实测：

```text
ninja: no work to do.
```

全量测试的等价命令：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
"$T/bin/llvm-lit" -sv -j16 mlir/test
```

`check-mlir` 只是把这个命令包了一层 CMake target；直接跑 `llvm-lit` 更容易控制并发、过滤和输出。

### 为什么先 build-only 再 lit

因为 lit 不会替你编工具。缺 `mlir-capi-ir-test`、`mlir-lsp-server`、`mlir-reduce` 时，测试会以 `Exit Code: 127` 失败：

```text
'.../bin/mlir-capi-ir-test': command not found
```

这类失败不是代码回归，而是环境没准备好。判断顺序：

1. 看失败日志有没有 `command not found`；
2. 看缺失的二进制是否在 `builds/ct-instr-trunk/bin/`；
3. 跑 `ninja check-mlir-build-only`；
4. 重跑失败目录。

## 02.5 lit 的发现规则：4,353 个可运行测试从哪来

`mlir/test/lit.cfg.py` 定义了测试后缀：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
sed -n '27,39p' mlir/test/lit.cfg.py
```

本机实测：

```text
config.suffixes = [
    ".td",
    ".mlir",
    ".toy",
    ".ll",
    ".tc",
    ".py",
    ".yaml",
    ".test",
    ".pdll",
    ".c",
    ".spvasm",
]
```

所以 MLIR 的“测试”不只有 `.mlir`：

- `.td`：TableGen 定义的 roundtrip/诊断；
- `.py`：Python binding；
- `.pdll`：PDDL/PDLL；
- `.c`：C API；
- `.spvasm`：SPIR-V 汇编；
- `.yaml`：Linalg named op 等。

按后缀数一遍：

```bash
git ls-files mlir/test | sed 's/.*\.//' | sort | uniq -c | sort -nr | head -12
```

本机实测：

```text
   3299 mlir
    151 cpp
    137 py
     87 td
     75 ll
     58 cfg
     55 wasm
     50 txt
     46 test
     42 toy
     37 mlirbc
     24 pdll
```

`--show-tests` 能看到 lit 实际发现的测试名：

```bash
"$T/bin/llvm-lit" --show-tests mlir/test | wc -l
```

本机实测：

```text
4353
```

为什么不是 4,947？因为 `--show-tests` 只列出可运行的测试；4,947 包含 unsupported。两者差值 594 左右，再加上 suite 自身的差异，不必强行对齐；关键是**同一个命令在改动前后保持一致**。

## 02.6 可用特性：asserts、expensive、host-*

lit 测试用 `REQUIRES:` / `UNSUPPORTED:` 引用 `available_features`。`mlir/test/lit.cfg.py` 会按构建配置添加：

```bash
grep -n 'available_features.add' mlir/test/lit.cfg.py | head -20
```

本机实测（节选）：

```text
323:    config.available_features.add("has_llc_binary")
361:    config.available_features.add("host-byteorder-" + sys.byteorder + "-endian")
364:    config.available_features.add("asserts")
366:    config.available_features.add("noasserts")
369:    config.available_features.add("python-stable-abi")
372:    config.available_features.add("expensive_checks")
396:    config.available_features.add("host-supports-jit")
399:    config.available_features.add("host-supports-nvptx")
402:    config.available_features.add("host-supports-amdgpu")
405:    config.available_features.add("host-supports-spirv")
408:    config.available_features.add("arm-emulator")
411:    config.available_features.add("mlir-expensive-checks")
```

本机 `LLVM_ENABLE_EXPENSIVE_CHECKS=OFF`，所以 `expensive_checks` 不存在；`MLIR_ENABLE_EXPENSIVE_PATTERN_API_CHECKS` 也未开，所以 `mlir-expensive-checks` 不存在。带这两个 feature 的测试会被 excluded。

维护含义：

- **不要为了让测试通过而去改 `UNSUPPORTED`。** 先判断是环境不支持，还是行为真的坏了。
- **assertions-only 的测试在 release CI 上不会跑。** 如果你修的是断言触发的问题，必须在 assertions 构建上验证。
- **`REQUIRES: asserts` 的负控很重要。** 它保护的往往就是“release 下静默继续”的不变量。

## 02.7 跑测试：全量、目录、过滤

### 全量

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
time "$T/bin/llvm-lit" -q -j16 mlir/test
```

本机实测：

```text
Total Discovered Tests: 4947
  Unsupported      :  643
  Passed           : 4303
  Expectedly Failed:    1
  Failed           :    0
real    0m43.975s
```

### 单目录

```bash
"$T/bin/llvm-lit" -s -j16 mlir/test/Dialect/Arith
```

本机实测：

```text
-- Testing: 31 tests, 16 workers --
Testing Time: 1.43s

Total Discovered Tests: 31
  Passed: 31 (100.00%)
```

### 过滤

```bash
"$T/bin/llvm-lit" -s --filter='canonicalize' mlir/test/Dialect/Arith
```

本机实测：

```text
Total Discovered Tests: 31
  Excluded: 30 (96.77%)
  Passed  :  1 (3.23%)
```

过滤只匹配测试名，不匹配文件内容；`--filter='addi'` 可能一个都不中，因为测试名是 `arith-canonicalize` 之类的路径名。先 `--show-tests` 看名字。

### 输出模式

| 选项 | 输出 | 用途 |
|---|---|---|
| `-q` | 只报失败和汇总 | 日常快速 |
| `-s` | 汇总（passed/excluded/unsupported） | 判断测试集合 |
| `-v` | 每个测试的命令 | 复现单条 |
| `-sv` | 命令 + 汇总 | 调试失败 |
| `-j N` | 并发 | 全量 16，单目录可 8 |

## 02.8 按改动层选回归集合

维护者不需要每次跑 4,947 个测试。按改动层选：

| 改动位置 | 最小集合 | 再加跑 |
|---|---|---|
| `mlir/lib/IR` / `include/mlir/IR` | `mlir/test/IR` + `mlir/test/Bytecode` | `mlir/test/Pass`、`mlir/test/Rewrite` |
| `mlir/lib/Pass` | `mlir/test/Pass` | `mlir/test/Transforms` |
| `mlir/lib/Rewrite` | `mlir/test/Rewrite`、`mlir/test/Transforms/canonicalize.mlir` | 相关 dialect 的 canonicalize |
| `mlir/lib/Analysis` | `mlir/test/Analysis` | `mlir/test/Dialect/*` 中用到该 analysis 的 |
| `mlir/lib/Dialect/<X>` | `mlir/test/Dialect/<X>` | `mlir/test/Conversion/*<X>*` |
| `mlir/lib/Conversion/<A>To<B>` | `mlir/test/Conversion/<A>To<B>` | `mlir/test/Target`（若到 LLVM IR） |
| `mlir/lib/Target/LLVMIR` | `mlir/test/Target/LLVMIR` | `mlir/test/Conversion/...ToLLVM` |
| `mlir/tools/mlir-opt` | `mlir/test/mlir-opt` | 全量（注册面大） |

原则：**先跑最靠近改动的目录，再跑一层依赖，最后全量**。全量是提交前门槛，不是日常内循环。

## 02.9 可跑实验

### 实验 1：确认 cache 里的四个开关

```bash
grep -E 'CMAKE_BUILD_TYPE:|LLVM_ENABLE_ASSERTIONS:|BUILD_SHARED_LIBS:|LLVM_ENABLE_PROJECTS:' "$T/CMakeCache.txt"
```

本机实测：

```text
BUILD_SHARED_LIBS:BOOL=ON
CMAKE_BUILD_TYPE:STRING=RelWithDebInfo
LLVM_ENABLE_ASSERTIONS:BOOL=ON
LLVM_ENABLE_PROJECTS:STRING=clang;lld;mlir
```

### 实验 2：确认测试依赖已编

```bash
cd "$T" && ninja -n check-mlir-build-only | tail -3
```

本机实测：

```text
ninja: no work to do.
```

### 实验 3：数可运行测试

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
"$T/bin/llvm-lit" --show-tests mlir/test | wc -l
```

本机实测：

```text
4353
```

### 实验 4：单目录基线

```bash
"$T/bin/llvm-lit" -s -j16 mlir/test/Dialect/Arith
```

本机实测：

```text
Total Discovered Tests: 31
  Passed: 31 (100.00%)
```

### 实验 5：过滤与 excluded

```bash
"$T/bin/llvm-lit" -s --filter='canonicalize' mlir/test/Dialect/Arith
```

本机实测：

```text
Total Discovered Tests: 31
  Excluded: 30 (96.77%)
  Passed  :  1 (3.23%)
```

### 实验 6：看某个测试为什么被 skip

```bash
grep -R 'UNSUPPORTED\|REQUIRES' mlir/test/Dialect/Arith | head -10
```

本机实测（Arith 目录没有 gate，输出为空）：

```text
$ grep -R 'UNSUPPORTED\|REQUIRES' mlir/test/Dialect/Arith | head -5
$
```

换一个目录就能看到真实 gate：

```bash
grep -R -m1 'UNSUPPORTED:' mlir/test/python | head -3
```

本机实测：

```text
mlir/test/python/global_constructors.py:# UNSUPPORTED: target=aarch64{{.*}}, target=arm64{{.*}}
mlir/test/python/ir/auto_location.py:# UNSUPPORTED: python-stable-abi
mlir/test/Integration/Dialect/Linalg/CPU/test-tensor-matmul.mlir:// UNSUPPORTED: asan
```

## 02.10 源码落点

| 文件 | 行号 | 作用 |
|---|---|---|
| `mlir/CMakeLists.txt` | 120–215 | execution engine、PDL、tests、Python 选项 |
| `mlir/test/CMakeLists.txt` | 274–288 | `check-mlir-build-only` / `check-mlir` |
| `mlir/test/lit.cfg.py` | 27–39 | 测试后缀 |
| `mlir/test/lit.cfg.py` | 360–414 | available_features |
| `mlir/test/lit.site.cfg.py.in` | 1–30 | 构建配置注入 lit |
| `mlir/test/CMakeLists.txt` | `MLIR_TEST_DEPENDS` | 测试依赖工具清单 |

核对：

```bash
grep -n 'check-mlir-build-only\|add_lit_testsuite(check-mlir' mlir/test/CMakeLists.txt
grep -n 'config.suffixes' mlir/test/lit.cfg.py
grep -n 'available_features.add' mlir/test/lit.cfg.py | head
```

## 02.11 踩雷集锦

1. **把 `check-mlir` 当“只跑测试”。** 它会先编依赖；缺工具时失败在构建，不在测试。
2. **把 unsupported 当回归。** 643 个 unsupported 是配置边界；看 `Failed` 才判断回归。
3. **用 `--filter` 匹配文件内容。** 它匹配测试名；先 `--show-tests`。
4. **在 release 构建上验证 assertions-only 的 bug。** 断言不触发，行为可能“看起来正常”。
5. **只跑改动目录就提交。** 核心头文件的改动会波及几百个库；至少跑一层依赖 + 全量。
6. **改 `UNSUPPORTED` 让测试变绿。** 这是掩盖环境问题，不是修 bug。
7. **以为 `MLIR_INCLUDE_TESTS=OFF` 只影响测试。** 它还会让 `mlir-opt` 缺少 test pass，影响复现。
8. **同时开多个 lit 实例跑同一 build 目录。** 测试会写临时文件/共享库缓存，可能相互干扰；串行或分目录。

## 02.12 本章重点整理

- 构建矩阵四维：build type、assertions、shared/static、projects；它们共同决定二进制行为和 lit 可见集合。
- `check-mlir-build-only` 先备工具，`llvm-lit` 再跑测试；`check-mlir` 是两者的包装。
- lit 发现 4,353 个可运行测试（`--show-tests`），全量 discovered 4,947（含 643 unsupported）。
- `asserts`、`expensive_checks`、`host-supports-*` 等 feature 决定 `REQUIRES/UNSUPPORTED`。
- 回归集合按改动层选：IR → Pass/Rewrite → Dialect → Conversion → Target，最后全量。
- `-q/-s/-v/-sv` 四种输出模式用途不同；失败复现先用 `-sv`。

## 02.13 自我检核

- [ ] 能说出四个构建维度各自如何影响“测试是否运行”
- [ ] 能解释 4,353 与 4,947 的差异，以及 643 unsupported 的来源
- [ ] 能按改动位置选出最小回归集合，并说明为什么还要跑全量
- [ ] 能用 `--show-tests` / `--filter` / `-s` 定位一个测试是否被 excluded
- [ ] 能解释 `MLIR_INCLUDE_TESTS=OFF` 对 `mlir-opt` 的影响
- [ ] 遇到 `Exit Code: 127` 时，能按“缺工具 → build-only → 重跑”的顺序处理

## 02.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lit.cfg.py`**
  - 读哪里：`config.suffixes`、`available_features`、`ToolSubst`
  - 能学到什么：lit 如何发现测试、如何替换工具路径
  - 和本章关联：02.5–02.6 的所有数字都来自这里
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lit.site.cfg.py.in`**
  - 读哪里：整份（很短）
  - 能学到什么：CMake 变量如何注入 lit
  - 和本章关联：`asserts`、`expensive_checks`、`host-supports-*` 的来源
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/CMakeLists.txt`**
  - 读哪里：120–215 行
  - 能学到什么：MLIR 的构建选项与默认值
  - 和本章关联：02.3 的 cache 值对应的定义
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/CMakeLists.txt`**
  - 读哪里：250–290 行
  - 能学到什么：`check-mlir-build-only` 与 `check-mlir` 的依赖
  - 和本章关联：02.4 的目标表
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/TestingGuide.md`**
  - 读哪里：`lit` 与 `FileCheck` 两节
  - 能学到什么：LLVM 全家桶的测试约定；MLIR 的 lit 配置在此基础上扩展
  - 和本章关联：Ch 29 会展开 `split-input-file`、`-verify-diagnostics`

构建与测试的矩阵已经清楚。下一章换一个视角：谁拥有这些目录、review 会落到谁头上、哪些文件在一年内被改得最频繁。

→ 下一章：[Ch 03 维护者、CODEOWNERS 与 churn](./03-maintainers-and-churn.md)
