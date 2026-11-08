# Ch 24 — 自建 suite 的工程化

> **目标**：把前 23 章的零件组装成一个别人敢用的测试套件：目录布局、版本 pin、CI、跨平台、并行、报告、失败策略都能落地，而不是只在自己机器上 `lit tests` 能跑。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本章的 suite/报告输出在本机实跑；GitHub Actions 与 CMake 片段是模板，标注为「未在本机 CI 实跑」。

---

## 24.1 demo 与 production 的差距

前 23 章你已经能让一个 suite 跑起来。但「能跑」和「能维护」之间隔着这些问题：

| demo | production |
|---|---|
| `lit tests` 手动跑 | CI 每次 PR 自动跑，退出码可门禁 |
| 测试只在一台 Linux 上过 | Linux/macOS/Windows 至少各一套 |
| 失败后自己看终端 | xUnit/JSON 报告进 CI artifact，能点开失败用例 |
| 临时文件写在源码树 | `test_exec_root` 隔离，源码树永远干净 |
| 测试偶尔挂，重跑就好 | 能区分真失败、flaky、超时、环境缺失 |
| `lit` 版本随系统 | pin 版本，升级有测试 |
| 并行一开就互相踩 | `%t` 隔离 + `parallelism_group` 限流 |
| 失败信息只有 `exit 1` | 日志里有 `RUN: at line`、真实命令、stdout/stderr |

本章按「一个团队要接管这个 suite」的标准来收口。

## 24.2 目录布局：源码与执行分离

推荐布局：

```
myproject/
├── CMakeLists.txt
├── lit.cfg.py                  # suite 定义（源码树）
├── lit.site.cfg.py.in          # 构建参数模板
├── tests/
│   ├── smoke/
│   │   ├── basic.test
│   │   └── lit.local.cfg.py    # 可选：目录级覆盖
│   ├── errors/
│   │   ├── bad-input.test
│   │   └── Inputs/             # 辅助输入，不发现
│   └── Inputs/
├── tools/
│   └── mytool
└── .github/workflows/lit.yml
```

三个原则：

1. **`test_exec_root` 不在源码树**。CMake 构建时设成 `${CMAKE_BINARY_DIR}/tests`；独立运行时设成 `source_root/Output` 并加进 `.gitignore`。
2. **辅助输入放 `Inputs/`**，并在 `config.excludes` 里排除。LLVM 的 `llvm/test` 就是这么做的。
3. **不要让测试写源码目录**。一旦测试 `cd` 到源码目录写文件，并行、缓存、只读 checkout 全都会出问题。

一个能跑的最小 `lit.cfg.py`：

```python
import os
import lit.formats

config.name = "MyProject"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.excludes = ["Inputs", "CMakeLists.txt", "README.md"]
```

生产版再加 `substitutions`、`available_features`、`environment`、`parallelism_group`。Ch 11 已经逐项讲过。

## 24.3 安装与版本 pin

自建项目不要把「系统里碰巧有一个 lit」当依赖。三种做法，按推荐顺序：

### 做法一：从源码 vendor 一份（最可控）

```bash
git subtree add --prefix third_party/llvm-lit \
  https://github.com/llvm/llvm-project.git main --squash
```

或者只复制 `llvm/utils/lit`。优点是版本跟代码一起 review；缺点是升级要手动同步。

### 做法二：pip 安装并 pin 版本

```bash
python3 -m pip install "lit==24.0.0"
```

但 PyPI 的 `lit` 发布节奏和 LLVM trunk 不完全同步。依赖新特性（`%{s:stem}`、`%if`、`%{readfile:}`）时，PyPI 版本可能没有。更稳的是从 git 装：

```bash
python3 -m pip install \
  "git+https://github.com/llvm/llvm-project.git@<commit>#subdirectory=llvm/utils/lit"
```

`requirements.txt` 里写 commit，不要写 `main`。

### 做法三：用构建目录的 `llvm-lit`（已经在构建 LLVM 时）

```bash
/path/to/llvm-build/bin/llvm-lit --version
```

适合「项目本身就是 LLVM 子项目」的场景。CI 里要确保 lit 和被测工具来自同一次构建，否则会出现「工具新、lit 旧」或反过来。

> **版本检查要进 CI**：在 suite 的 `lit.cfg.py` 里加一个最小版本断言，比在 README 里写「需要 lit 24」可靠。

```python
import lit
major = lit.__versioninfo__[0]
if major < 24:
    lit_config.fatal("this suite requires lit >= 24, got %s" % lit.__version__)
```

## 24.4 CI：退出码、报告、超时

lit 的退出码语义（Ch 0/Ch 16）：

- `0`：所有选中测试通过。
- `1`：至少一个测试失败（`FAIL/XPASS/TIMEOUT/UNRESOLVED`）。
- `2`：lit 内部错误、没发现测试、filter 为空。

CI 里要区分 `1` 和 `2`：

```yaml
# .github/workflows/lit.yml —— 模板，未在本机 CI 实跑
name: lit
on: [push, pull_request]
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-22.04, macos-14, windows-2022]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - run: python -m pip install -r requirements.txt
      - run: python -m pip install -e .
      - name: Run lit
        shell: bash
        run: |
          set -o pipefail
          lit -v -j 4 \
            --xunit-xml-output lit-results.xml \
            -o lit-results.json \
            tests | tee lit.log
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: lit-results-${{ matrix.os }}
          path: |
            lit-results.xml
            lit-results.json
            lit.log
```

几个工程细节：

- **`set -o pipefail`**：否则 `lit ... | tee` 的退出码是 `tee` 的，CI 永远绿。这是最常见的 CI 假绿。
- **`if: always()`**：测试失败时也要上传报告，否则你只能看终端。
- **`fail-fast: false`**：一个平台失败不要取消其他平台，否则你不知道是不是跨平台问题。
- **`-j` 不要写死 CPU 数**：用固定值（如 4）保证 CI 可复现；本机可以用默认值。
- **超时**：CI 层面加 job timeout，lit 层面加 `--timeout`，两层都要有。

## 24.5 并行与资源：别让测试互相踩

### `%t` 隔离

每个测试的 `%t` 是唯一的：

```
<test_exec_root>/<path_in_suite>/Output/<basename>.tmp
```

所以两个测试同时写 `%t` 不会冲突。但**同一测试的多次运行**（`ALLOW_RETRIES`）会复用同一个 `%t`，测试要自己清理：

```text
# RUN: rm -rf %t.d && mkdir %t.d
# RUN: cp %S/Inputs/data.txt %t.d/
# RUN: mytool %t.d/data.txt > %t.d/out.txt
# RUN: FileCheck %s < %t.d/out.txt
```

### `parallelism_group`

有些测试会抢同一个端口、同一块 GPU、同一个全局缓存。用 `parallelism_group` 限流：

```python
# lit.cfg.py
lit_config.parallelism_groups["gpu"] = 1
config.parallelism_group = "gpu"
```

`config.parallelism_group` 也可以是 callable，按测试动态分组（源码 `worker._get_parallelism_semaphore()` 支持）。Ch 15 已讲实现。

### 时间与 flaky

- 不要写 `sleep` 等异步。用轮询 + 超时。
- 不要依赖系统时间/时区。需要时用固定 `SOURCE_DATE_EPOCH` 或注入时钟。
- 不要依赖网络。CI 可能没网，或者网慢导致超时。
- 对已知 flaky 测试用 `ALLOW_RETRIES: 1`，但要在 issue 里跟踪；不要让 `ALLOW_RETRIES` 变成掩盖 bug 的永久膏药。

## 24.6 跨平台：路径、shell、扩展名

### 路径

- 用 `%{pathsep}` 而不是硬编码 `:`。
- 用 `%{fs-sep}` 或 `%/s` 归一化斜杠。
- 需要比较路径时，用 `%{s:real}` / `%{t:real}` 展开符号链接。
- Windows 上避免用 `:` 拼接绝对路径；用 `%:s` 去掉盘符冒号。

### shell

- LLVM 24 默认内置 shell；不要写 bash 专有语法（`[[ ]]`、`set -e`、数组、函数）。
- 需要外部命令时，确认它在 PATH 里；找不到会 `127`。
- `echo -e`、`printf`、`test` 的行为在 lit 内置 shell 与 bash 下不完全一样，Ch 8 有实测。
- 需要外部 shell 时，优先通过 `LIT_USE_INTERNAL_SHELL=0` 做临时诊断，不要把它作为长期方案（`execute_external` 已废弃）。

### 扩展名

- Windows 可执行文件有 `.exe`；`GoogleTest` format 会自动补 `.exe`，`ShTest` 不会。
- 用 `%{exeext}`（LLVM 配置提供）或 `sys.platform` 判断。
- 脚本测试（`.py`）在 Windows 上没有 shebang 执行能力，`prepareCmd()` 会插入 `sys.executable`；你自己的 format 要注意这一点。

## 24.7 测试设计规范

一个 production suite 的测试应该满足：

1. **一个测试只验证一个行为**。失败时名字就能定位。
2. **测试名描述行为，不描述实现**。`add-zero-folds.ll` 比 `instcombine-test-17.ll` 好。
3. **失败信息能自解释**。至少包含：输入、命令、期望、实际。
4. **不依赖执行顺序**。并行 + `--order=random` 下仍要过。
5. **不依赖外部网络/时间/随机数**。需要随机时固定 seed。
6. **不写源码树**。所有输出进 `%t` 或 `test_exec_root`。
7. **每个 `RUN` 都能失败**。写完故意改坏一次，确认它会红（练习 A 的核心）。
8. **辅助文件放 `Inputs/`**，并在 `excludes` 里排除。

一个反例：

```text
# RUN: mytool input.txt > output.txt
# RUN: grep -q "ok" output.txt
```

问题：`input.txt` 在哪？`output.txt` 写到当前工作目录（可能是源码树）；并行时两个测试抢同一个 `output.txt`。正确写法：

```text
# RUN: mytool %S/Inputs/input.txt > %t.out
# RUN: grep -q "ok" %t.out
```

## 24.8 性能：让 suite 跑得快且稳

- **用 `--time-tests` 找慢测试**：`lit --time-tests=20 tests` 打印最慢 20 个。
- **用 `.lit_test_times.txt` + `--order=smart`**：先跑历史失败/慢的测试，早暴露问题。
- **用 sharding 拆 CI**：`--num-shards=4 --run-shard=N`，四个 job 并行；空 shard 退出 0。
- **限制单测试超时**：`--timeout=60` 或 `config.maxIndividualTestTime`；不要让一个挂死测试拖住整个 CI。
- **限制总时间**：`--max-time=1800`；超时后剩余测试标 `SKIPPED`。
- **谨慎开 `-j`**：I/O 重、内存大的 suite，`-j` 太高反而慢；用 `parallelism_group` 限流。

## 24.9 报告：让失败可检索

最小组合：

```bash
lit -v -j 4 \
  --xunit-xml-output results.xml \
  -o results.json \
  --time-tests=20 \
  tests
```

- `results.xml`：CI 原生支持，失败用例可点开。
- `results.json`：自己写脚本分析（最慢测试、失败分布、历史对比）。
- `--time-tests=20`：终端直方图，快速看性能回归。
- `-v`：失败时打印命令与输出；CI 日志里保留现场。

不要只上传 `lit.log`。日志是给人看的，XML/JSON 是给机器看的；两者都要。

## 24.10 发布与维护

- **版本化 suite**：suite 的配置/测试和被测项目一起打 tag；不要出现「测试是新的、工具是旧的」。
- **升级 lit 要有测试**：升级前跑一遍 `check-lit` 或自建 suite，重点看 CLI 输出、报告 schema、shell 行为。
- **废弃策略**：自定义 directive/format 的删除要走 deprecation 周期；至少一个 release 的 warning。
- **文档**：在 README 里写清「怎么跑一个测试」「怎么加一个测试」「失败怎么看」「CI 怎么复现」。
- **所有权**：指定一个 owner 维护 `lit.cfg.py` 和 CI；否则配置会腐烂。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「CI 里 `lit | tee log` 失败会红」 | 没有 `set -o pipefail` 时退出码是 `tee` 的，永远 0。 |
| 「测试写当前目录没关系」 | 并行会互相覆盖，源码树会被弄脏；一律写 `%t`。 |
| 「`ALLOW_RETRIES` 能治 flaky」 | 它只是让 flaky 变成 `FLAKYPASS`，不消除根因；要跟踪 issue。 |
| 「Windows 上路径用 `/` 就行」 | 多数工具接受 `/`，但 shell 重定向、盘符、`%:s` 仍有差异；要实测。 |
| 「`-j` 越大越快」 | I/O/内存/端口竞争会让它更慢；用 `parallelism_group` 和实测调。 |
| 「lit 退出 2 是测试失败」 | 是 lit 自己出错（配置/路径/过滤为空）；CI 要单独报警。 |

## 重点整理

- production suite 的六件事：目录隔离、版本 pin、CI 门禁、并行限流、跨平台、报告。
- `test_exec_root` 不在源码树；辅助输入进 `Inputs/` 并 `excludes`。
- CI 必须 `set -o pipefail`，否则 `tee` 会吞掉失败。
- `%t` 隔离单测试；`parallelism_group` 隔离共享资源；`--timeout`/`--max-time` 防挂死。
- 测试设计：一个行为、可失败、无顺序依赖、无网络/时间/随机、只写 `%t`。
- 报告组合：`-v` + xUnit + JSON + `--time-tests`；日志与机器报告都要。
- 升级 lit 要跑 suite；自定义扩展要有 deprecation 周期。

## 自我检核

- [ ] 你的 suite 在 CI 里 `lit ... | tee`，测试失败但 job 绿了，最可能是什么原因？怎么修？
- [ ] 为什么 `%t` 能保证并行安全，而 `output.txt` 不能？`%t` 的路径由哪几段拼成？
- [ ] 一个测试依赖网络，另一个依赖系统时间；分别会在什么环境下变成 flaky？怎么改成确定性？
- [ ] `--timeout`、`--max-time`、CI job timeout 三层分别防什么？只留一层会怎样？
- [ ] 为什么 `ALLOW_RETRIES` 不是 flaky 的解决方案？它会把结果标成什么？
- [ ] 你要给团队写「怎么加一个测试」的 README，最少要包含哪 5 条？

## 延伸阅读

- **[lit Command Guide — EXECUTION OPTIONS / SELECTION OPTIONS](https://llvm.org/docs/CommandGuide/lit.html#execution-options)**
  - 读什么：`-j`、`--timeout`、`--max-failures`、`--filter`、shards 的官方说明。
  - 能学到：本章 CLI 的权威语义；对照 Ch 15–17 的实现。
  - 前提：跑过至少一个 suite。
- **[LLVM Test Suite Guide — Quickstart](https://llvm.org/docs/TestSuiteGuide.html#quickstart)**
  - 读什么：前 5 步与 `llvm-lit -v -j 1 -o results.json .` 示例。
  - 能学到：一个真实大型 suite 的 CI/报告用法；`-o` JSON 的实际消费方式。
  - 前提：知道 CMake 基本用法。
- **[`llvm/utils/lit/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/CMakeLists.txt)**
  - 读什么：全文（约 40 行）。
  - 能学到：`configure_lit_site_cfg` + `add_lit_testsuite` + `prepare-check-lit` 的真实工程写法；为什么要复制测试目录。
  - 前提：读过 Ch 12。
- **[GitHub Actions — Workflow syntax](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions)**
  - 读什么：`jobs.<job_id>.strategy.matrix`、`steps[*].if`、`actions/upload-artifact`。
  - 能学到：本章 YAML 模板里每个字段的含义；怎么改成 GitLab CI/Jenkins。
  - 前提：用过任意 CI。

> 最后一章把视角转到上游：你发现 lit 本身有个问题，怎么从复现走到一个能被 reviewer 接受的 patch。

→ [Ch 25 给上游 lit 提交改动](./25-upstream-lit.md)
