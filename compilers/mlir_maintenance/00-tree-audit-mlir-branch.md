# Ch 00 — 树审计与分析分支

> **目标**：把“我要读 MLIR 源码”变成一套可复现的实验环境：知道当前 worktree 在哪个 commit、构建里有哪些 MLIR 工具、全量 lit 基线是多少，以及后面 31 章所有“实测”输出是从哪条命令跑出来的。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，LLVM/MLIR `24.0.0git`，`builds/ct-instr-trunk`（`RelWithDebInfo`、assertions ON、shared libs、`clang;lld;mlir`）。

## 00.1 为什么维护者要先做树审计

维护 MLIR 和读 MLIR 是两种不同的活动。

读 MLIR 时，你可以接受“这份代码大概是某个版本”；维护 MLIR 时，你必须回答三个可证伪的问题：

1. **我改的是哪一份代码？** 上游 `main` 每天都在动。同一个 `Operation::erase()`，在 24.0.0git 的某个 commit 和三个月后的 commit 上，前后置条件可能不同。
2. **我跑的是哪一份二进制？** 源码改了但没重编，`mlir-opt` 跑的还是旧逻辑；这时你看到的“行为没变”是假的。
3. **我用来判断对错的基线是什么？** 如果不知道全量 `mlir/test` 本来有多少 unsupported、多少 XFAIL，你无法区分“我改坏了”和“这个环境本来就不支持”。

所以本课第 0 章不写 MLIR 概念，先做一次**树审计（tree audit）**：把源码、分支、构建、工具、测试基线四件事钉死。后面每一章都引用这套基线；任何“实测”都可以回到这里复现。

> 如果你只想先建立 MLIR 心智模型，可以读完本章后跳到 [Ch 01](./01-mlir-in-llvm-map.md)；但只要你打算改代码，本章的检查项一个都不能省。

## 00.2 先建立直觉：一条证据链

维护工作不是“读文件 → 写补丁”，而是一条从源码到证据的链：

```
  upstream/main
       │  git switch -c analysis/mlir-maintenance
       ▼
  trees/ct-instr-trunk/          ← 你读的源码（只读）
       │  cmake -S llvm -B builds/ct-instr-trunk
       ▼
  builds/ct-instr-trunk/         ← 你跑的二进制（可重建）
       │  mlir-opt / mlir-translate / mlir-runner
       ▼
  /tmp/*.mlir                    ← 最小复现（可丢弃）
       │  llvm-lit mlir/test
       ▼
  基线数字 + 失败清单            ← 判断“有没有改坏”的锚点
       │  git diff / git log
       ▼
  PR 里的“问题 → 根因 → 修复 → 测试”
```

这条链上任何一环断开，后面的结论都不可信：

- 分支不对 → 你读的源码和你跑的构建不是同一份。
- 构建落后 → `ninja -n` 会告诉你还有多少条边没编。
- 没有基线 → 你不知道 9 个失败是环境问题还是回归。
- 没有最小复现 → reviewer 无法独立验证你的 bug 声明。

本章就是把这条链的每一环都跑一遍。

## 00.3 审计一：worktree 与分支

先看当前状态：

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb
git log --oneline -3
```

本机实测（2026-11-08）：

```text
## analysis/mlir-maintenance...origin/analysis/mlir-maintenance
?? llvm/test/CodeGen/LoongArch/bittest.s
e44fff5decc7 [clang-repl] Undo the enumerators of an unscoped enumeration (#228370)
3aec233fb321 [mlir][nvgpu] Expose which memrefs optimize-shared-memory can swizzle (NFC) (#237180)
bc0d038ff6a0 [mlir][vector] Fix foldInsertUseChain to return getDest() (#239714)
```

三个信息：

1. **分支**：`analysis/mlir-maintenance`，跟踪 `origin/analysis/mlir-maintenance`（你的 fork）。它不是 `upstream/main`，所以 `git push` 不会误推到 LLVM 上游。
2. **HEAD**：`e44fff5decc7`。本课所有行号都以这个 commit 为准；上游一旦前进，`grep -n` 的行号会漂移。
3. **未跟踪文件**：`llvm/test/CodeGen/LoongArch/bittest.s` 是前一个任务留下的，不属于 MLIR，也不属于本课。不要提交它，也不要删除它。

为什么分支要从 `upstream/main` 开，而不是从旧的 `analysis/riscv-maintenance` 继续？

- MLIR 的 ODS、conversion、bytecode 都在持续演化，旧分支落后 13 个提交就足以让行号和 API 对不上。
- 维护者提交 PR 时，基线必须是当前 `upstream/main`；否则 review 第一句就是“please rebase”。
- 分析分支和 RISC-V 课程分支互不影响：RISC-V 分支仍在 `0fc5afa04c23`，需要时切回去即可。

检查分支基：

```bash
git merge-base --is-ancestor upstream/main HEAD && echo "based on upstream/main"
git rev-list --count upstream/main..HEAD
```

本机实测：

```text
based on upstream/main
0
```

`0` 表示当前分支没有独有提交，就是一个干净的基线分支。后面写课程文档不会改这个 worktree；课程文件在另一个仓库 `Learning-with-Claude`。

## 00.4 审计二：构建配置

这棵树原本的构建只启用了 `clang;lld`，没有 MLIR。启用 MLIR 不需要新 worktree，但必须重配：

```bash
LLVM_ENABLE_PROJECTS="clang;lld;mlir" \
  "$LLVM_HOME"/toolset/configure.sh ct-instr-trunk
```

`toolset/configure.sh` 是工作区唯一允许的配置入口；它固定了 `RelWithDebInfo`、assertions ON、`BUILD_SHARED_LIBS=ON`、`LLVM_USE_LINKER=lld`、ccache 等参数。我们只通过 `LLVM_ENABLE_PROJECTS` 环境变量加项目，不手写一整套 cmake 参数。

配置完成后看缓存：

```bash
grep -E 'LLVM_ENABLE_PROJECTS:|CMAKE_BUILD_TYPE:|LLVM_ENABLE_ASSERTIONS:|BUILD_SHARED_LIBS:' \
  "$LLVM_HOME/builds/ct-instr-trunk/CMakeCache.txt"
```

本机实测：

```text
CMAKE_BUILD_TYPE:STRING=RelWithDebInfo
LLVM_ENABLE_ASSERTIONS:BOOL=ON
LLVM_ENABLE_PROJECTS:STRING=clang;lld;mlir
BUILD_SHARED_LIBS:BOOL=ON
```

四个选项分别意味着：

- `RelWithDebInfo`：有优化也有调试信息。跑得快，但栈回溯还能看到行号。
- `assertions ON`：`assert()` 和 `llvm_unreachable` 生效。MLIR 的很多不变量只在 assertions 构建里检查；release 构建会静默继续，反而更难查。
- `clang;lld;mlir`：MLIR 的 lit 测试需要 clang/lld 参与端到端用例；只开 `mlir` 会少掉一部分测试能力。
- `BUILD_SHARED_LIBS=ON`：库是 `.so`，增量链接快，`mlir-opt` 启动也快；代价是运行时 `LD_LIBRARY_PATH` 必须能找到 build 的 `lib/`，`llvm-lit` 已经替你设好。

构建目标先只编四个工具：

```bash
cd "$LLVM_HOME/builds/ct-instr-trunk"
time ninja -j24 mlir-opt mlir-translate mlir-runner mlir-tblgen
```

本机首次实测：

```text
[5384/5384] Linking CXX executable bin/mlir-opt

real    26m20.629s
user    592m26.905s
sys     32m40.754s
```

5,384 条边、26 分 20 秒。这个数字很重要：后面你每次改 `mlir/include/mlir/IR/OpDefinition.h` 这类核心头文件，都会触发大范围重编；先知道成本，才不会在 review 中途才发现要等半小时。

### 一个容易漏的缓存项：PDL

MLIR 的 Transform dialect 和 PDL 测试依赖 `MLIR_ENABLE_PDL_IN_PATTERNMATCH`。这个选项在“MLIR 只是依赖、不是主项目”时会默认关掉；如果 CMake cache 里残留了 `0`，即使 `LLVM_ENABLE_PROJECTS` 已经包含 `mlir`，`mlir-opt` 也不会注册 PDL 相关测试 pass。

本机第一次跑 lit 时就踩到了：9 个 Transform/PDL 测试失败，报 `PDL for rewrites when PDL is not enabled` 或 `Unknown command line argument '-test-pdll-pass'`。修正只有一行：

```bash
cmake -S "$LLVM_HOME/trees/ct-instr-trunk/llvm" \
      -B "$LLVM_HOME/builds/ct-instr-trunk" \
      -DMLIR_ENABLE_PDL_IN_PATTERNMATCH=ON
ninja -j24 mlir-opt
```

本机实测增量：

```text
[5188/5188] Linking CXX executable bin/mlir-opt

real    14m48.385s
```

为什么不是改 `toolset/configure.sh`？因为那是全工作区共享脚本，改它会影响别的任务；这里只是修一个缓存项，保留其余由 toolset 生成的配置。课程里凡是这样“补一个 cache 变量”的动作，都要写清原因和影响范围。

## 00.5 审计三：工具与方言

确认二进制真的能跑：

```bash
T="$LLVM_HOME/builds/ct-instr-trunk"
$T/bin/mlir-opt --version
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | wc -l
```

本机实测：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.

50
```

50 个方言是“这个构建注册了多少方言”，不是“MLIR 只有 50 个方言”。不同构建会因 `LLVM_TARGETS_TO_BUILD`、可选后端、测试 dialect 而变化。做对比实验时，先确认两边的方言集合一致。

再看三个工具的分工：

| 工具 | 输入 | 输出 | 维护时什么时候用 |
|---|---|---|---|
| `mlir-opt` | `.mlir` | `.mlir` | 跑 pass、验证 canonicalize/conversion、最小复现 |
| `mlir-translate` | `.mlir` | LLVM IR / SPIR-V 等 | 检查 MLIR → LLVM 的接缝 |
| `mlir-runner` | LLVM dialect / 可执行 IR | 程序输出 | 端到端验证数值语义 |

`mlir-runner` 在本树不叫 `mlir-cpu-runner`。如果你照着旧文档敲 `mlir-cpu-runner`，`ninja` 会报 `unknown target`。这是一个典型的“版本漂移”坑：先 `ninja -t targets | grep mlir-`，再决定目标名。

## 00.6 审计四：最小冒烟

写一个最小的 `func + arith`：

```bash
mkdir -p /tmp/mlir-ch00 && cd /tmp/mlir-ch00
cat > smoke.mlir <<'EOF'
func.func @add(%a: i32, %b: i32) -> i32 {
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
$T/bin/mlir-opt smoke.mlir
```

本机实测：

```text
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
}
```

注意两个细节：

- 输入参数名 `%a`/`%b` 被打印成 `%arg0`/`%arg1`。MLIR 的 printer 默认不保留 SSA 名字；调试时不要拿名字当稳定标识。
- `mlir-opt` 即使不跑 pass，也会 parse → verify → print。它能发现语法错误和 verifier 错误，但不会发现语义错误。

再走一次 lowering：

```bash
$T/bin/mlir-opt --convert-to-llvm smoke.mlir
```

本机实测：

```text
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

这一步只证明“arith/func 能降到 LLVM dialect”，不证明能生成机器码。真正的端到端在 Ch 23 和练习 E。

## 00.7 审计五：lit 基线

MLIR 的测试入口是 `mlir/test`。先建测试依赖：

```bash
cd "$LLVM_HOME/builds/ct-instr-trunk"
ninja -j24 check-mlir-build-only
```

本机实测：

```text
[404/404] Linking CXX executable tools/mlir/unittests/Target/LLVM/MLIRTargetLLVMTests

real    1m39.054s
```

然后跑全量：

```bash
cd "$LLVM_HOME/trees/ct-instr-trunk"
"$LLVM_HOME/builds/ct-instr-trunk/bin/llvm-lit" -sv -j16 mlir/test
```

本机最终基线：

```text
Total Discovered Tests: 4947
  Unsupported      :  643 (13.00%)
  Passed           : 4303 (86.98%)
  Expectedly Failed:    1 (0.02%)
  Failed           :    0

Testing Time: 43.42s
```

四个数字要一起看：

- **Discovered 4,947**：lit 认为存在的测试总数。它包含 unsupported，不等于“会跑 4,947 个”。
- **Unsupported 643**：当前构建不支持的后端/特性（CUDA、ROCm、部分 GPU、可选工具）。这是环境的已知边界，不是回归。
- **Passed 4,303**：真正通过的数量。
- **XFAIL 1**：预期失败，通常是上游已知问题；不要把它当失败。
- **Failed 0**：这才是“基线干净”的判据。

首轮如果不建 `check-mlir-build-only`，会看到 105 个 `Exit Code: 127`。那不是代码坏了，而是 CAPI 测试、LSP server、`mlir-reduce`、`mlir-pdll` 等工具没编。判断方法：失败日志里出现 `command not found`，且路径在 `builds/ct-instr-trunk/bin/` 下。先补依赖，再谈回归。

## 00.8 可跑实验：把基线变成可重复的证据

### 实验 1：确认构建没有落后

```bash
cd "$LLVM_HOME/builds/ct-instr-trunk"
ninja -n mlir-opt mlir-translate mlir-runner | tail -3
```

本机实测：

```text
ninja: no work to do.
```

如果这里不是 `no work to do`，先 `ninja -j24 mlir-opt mlir-translate mlir-runner`，再跑任何 lit。否则你测的是旧二进制。

### 实验 2：确认 PDL 测试 pass 真的注册了

```bash
"$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" --help \
  | grep -E 'test-pdll-pass|test-pdl-bytecode-pass'
```

本机实测：

```text
      --test-pdl-bytecode-pass   -   Test PDL ByteCode functionality
      --test-pdll-pass           -   Test PDLL functionality
```

这两个开关存在，说明 `MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON` 生效。如果缺失，Transform/PDL 的 lit 会以“unknown argument”失败。

### 实验 3：量一次全量 lit 的真实耗时

```bash
cd "$LLVM_HOME/trees/ct-instr-trunk"
time "$LLVM_HOME/builds/ct-instr-trunk/bin/llvm-lit" -q -j16 mlir/test
```

本机实测：

```text
real    0m43.975s
```

`-q` 只打印失败和汇总。日常改一个 dialect 时不要跑全量，跑对应目录即可；但提交前至少跑一次全量或 CI 等价集合。

### 实验 4：确认源码树没有被课程污染

```bash
cd "$LLVM_HOME/trees/ct-instr-trunk"
git status --porcelain
```

本机实测：

```text
?? llvm/test/CodeGen/LoongArch/bittest.s
```

除这个前序任务遗留文件外，没有课程产生的改动。课程文件全部写在 `Learning-with-Claude` 仓库；LLVM worktree 只读。

### 实验 5：记录构建体积

```bash
du -sh "$LLVM_HOME/builds/ct-instr-trunk"
```

本机实测：

```text
51G
```

启用 MLIR 后构建从 39G 涨到 51G。磁盘紧张时，先清 ccache 和旧 run 日志，不要删 `builds/`；重编一次 26 分钟起。

## 00.9 源码落点

本章不涉及 MLIR 语义，但要记住四个入口文件：

| 文件 | 作用 | 为什么维护者会看 |
|---|---|---|
| `mlir/CMakeLists.txt` | MLIR 顶层配置 | 选项默认值、依赖、测试开关 |
| `mlir/test/lit.cfg.py` | lit 测试发现与替换 | 为什么某个测试没跑/被 skip |
| `mlir/tools/mlir-opt/mlir-opt.cpp` | `mlir-opt` 注册入口 | 哪些 pass/dialect 在测试二进制里可见 |
| `mlir/Maintainers.md` | 维护者分区 | 找对 reviewer |

用 `grep -n` 核对本章引用的关键行：

```bash
grep -n 'MLIR_ENABLE_PDL_IN_PATTERNMATCH' mlir/CMakeLists.txt
grep -n 'MLIR_INCLUDE_TESTS' mlir/CMakeLists.txt
grep -n 'registerTestPasses' mlir/tools/mlir-opt/mlir-opt.cpp
```

本机实测（行号以 `e44fff5decc7` 为准）：

```text
mlir/CMakeLists.txt:150:if("mlir" IN_LIST LLVM_DEPENDENCY_ONLY_PROJECTS)
mlir/CMakeLists.txt:151:  set(MLIR_ENABLE_PDL_IN_PATTERNMATCH_default 0)
mlir/CMakeLists.txt:153:  set(MLIR_ENABLE_PDL_IN_PATTERNMATCH_default 1)
mlir/tools/mlir-opt/mlir-opt.cpp:328:  registerTestPasses();
```

## 00.10 踩雷集锦

1. **以为 `LLVM_ENABLE_PROJECTS` 改了就会重配。** CMake cache 会保留旧值；改环境变量后要重跑 `toolset/configure.sh`，并 `grep CMakeCache.txt` 确认。只看命令行输出不算。
2. **把 `mlir-cpu-runner` 当成本树的目标名。** 本树是 `mlir-runner`；旧文档、旧脚本里的名字会直接让 `ninja` 报 unknown target。
3. **不建测试依赖就跑全量 lit。** 105 个 `Exit Code: 127` 会让你以为 MLIR 坏了；其实只是 CAPI/LSP/reduce 工具没编。
4. **把 unsupported 当失败。** 643 个 unsupported 是环境边界；判断回归只看 `Failed`，再对照失败目录是否与你的改动相关。
5. **在 stale build 上跑实验。** `ninja -n` 不是 `no work to do` 时，`mlir-opt` 的行为可能还是旧代码；先重编再测。
6. **用 `git status` 里的未跟踪文件判断“树是干净的”。** 本树有一个前序任务遗留的 `bittest.s`；它不属于本课，不要顺手提交。
7. **把 `mlir-opt` 的打印名当稳定标识。** SSA 名字会被重写成 `%arg0`；测试要用结构匹配（`FileCheck` 的 `CHECK-SAME`/`CHECK-NEXT`），不要依赖参数名。
8. **在 upstream 分支上直接改。** 本课的分析分支跟踪 fork；上游 `upstream` 是 `no_push`。提交前确认 `git remote -v`，避免推到错误的地方。

## 00.11 本章重点整理

- 维护 MLIR 的第一件事不是读 `Operation.cpp`，而是把**源码 commit、构建二进制、测试基线**三者对齐。
- `analysis/mlir-maintenance` 从 `upstream/main` 的 `e44fff5decc7` 开出，`0` 个独有提交；课程文档不写回 LLVM worktree。
- 构建用 toolset 的 `LLVM_ENABLE_PROJECTS="clang;lld;mlir"`；首编 5,384 边 / 26 分 20 秒。
- `MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON` 是 MLIR 测试的隐藏开关；缓存为 0 会造成 9 个假失败。
- 干净基线：4,947 discovered / 4,303 passed / 643 unsupported / 1 XFAIL / **0 failed**，43.4 秒。
- 日常判断回归：先 `ninja -n`，再跑对应目录 lit，最后才跑全量。

## 00.12 自我检核

- [ ] 能说出当前 worktree 的 HEAD、分支、跟踪的远端，以及为什么不能用 `upstream/main` 直接改
- [ ] 能解释 `RelWithDebInfo`、assertions、shared libs、`clang;lld;mlir` 四个配置各自影响什么
- [ ] 能复现 `mlir-opt --version` / `--show-dialects` / 最小 `arith` lowering，并解释输出里的 `%arg0`
- [ ] 能说出 4,947 / 4,303 / 643 / 1 / 0 五个数字的含义
- [ ] 遇到 `Exit Code: 127` 时，知道先查 `command not found` 和缺失的构建目标
- [ ] 能在不删除 `builds/` 的前提下判断“构建是否落后”

## 00.13 延伸阅读

- **`/home/ubuntu/llvm/toolset/README.md`**
  - 读哪里：`configure.sh`、`build.sh`、`status.sh` 三段
  - 能学到什么：本机唯一的构建入口，以及为什么不允许手敲整套 cmake
  - 和本章关联：00.4 的配置就是这套脚本生成的
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/CMakeLists.txt`**
  - 读哪里：第 120–215 行（execution engine、PDL、tests、Python bindings）
  - 能学到什么：MLIR 的默认选项如何随构建形态变化
  - 和本章关联：00.4 的 PDL 缓存坑就在第 150–156 行
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lit.cfg.py`**
  - 读哪里：`config.suffixes` 和 `available_features` 两段
  - 能学到什么：为什么 `.td`/`.py`/`.pdll` 也算测试，以及 unsupported 是怎么来的
  - 和本章关联：00.7 的 4,947 个 discovered 由这里决定
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/PassManagement.md`**
  - 读哪里：先读 “Operation Pass” 与 “Pass Manager” 两节
  - 能学到什么：下一章之后所有 pass 实验的语法基础
  - 和本章关联：00.7 的 `--pass-pipeline` 就来自这套语法
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/Maintainers.md`**
  - 读哪里：Core / Egress / Tensor Compiler 三个分区
  - 能学到什么：谁负责哪块，review 该找谁
  - 和本章关联：Ch 03 会展开

环境已经钉死。下一章先不看具体代码，而是把 MLIR 放回 LLVM 的整体结构里：它有哪些库、哪些工具、和 LLVM IR 的边界在哪。

→ 下一章：[Ch 01 MLIR 在 LLVM 中的位置](./01-mlir-in-llvm-map.md)
