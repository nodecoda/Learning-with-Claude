# Final Project — 从零做一个可发布的 lit 测试框架

> **目标**：把全课 70% 以上的核心概念压进一个可交付项目：一个能被别人安装、运行、读报告、接 CI 的 lit 测试框架 `minitest`。不是「再写几个测试」，而是交付一套**测试基础设施**：suite 配置、自定义 format、directive 扩展、报告、CI、上游 patch。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。项目本身跨平台，但验收命令以 Linux 为准；Windows/macOS 差异要显式处理或标注。

---

## 项目定位

你要为一个虚构的工具链 `mytool` 做测试框架。`mytool` 有两个子命令：

```bash
mytool compile input.toy -o output.txt   # 把 .toy 源文件编译成三地址码
mytool check input.toy                   # 只做语义检查，输出 OK 或错误
```

`minitest` 要满足：

- 用 lit 作为 runner，但**不是**只会写 `RUN: echo`。
- 支持两种测试格式：
  1. **ShTest**：LLVM 风格的 `RUN:` + FileCheck/`grep`/`diff`。
  2. **JsonCase**：自定义 `TestFormat`，每个 `.case` 文件是一个 JSON 测试描述。
- 支持按 feature 跳过（`REQUIRES`/`UNSUPPORTED`）、预期失败（`XFAIL`）、重试（`ALLOW_RETRIES`）。
- 有 `lit.cfg.py` + `lit.site.cfg.py.in` + CMake，构建树与源码树分离。
- 生成 xUnit 与 JSON 报告；CI 能门禁。
- 给上游 lit 提交一个**小但完整**的 patch（实现 + 自测 + 文档 + Release Notes）。

## 交付物

| ID | 交付物 | 对应章节 |
|---|---|---|
| D1 | `minitest/` 目录 + `lit.cfg.py` + `lit.site.cfg.py.in` | Ch 3, 10–12 |
| D2 | `mytool` 参考实现（Python，约 80–150 行） | 自备 |
| D3 | 8–10 个 ShTest 测试（正常/边界/失败/`REQUIRES`/`XFAIL`） | Ch 4–9 |
| D4 | `JsonCase` 自定义 format + 5 个 `.case` 测试 | Ch 19–23, Practice D |
| D5 | 报告：xUnit + JSON + `--time-tests` 输出 | Ch 17 |
| D6 | CI：GitHub Actions（Linux/macOS/Windows 矩阵） | Ch 24 |
| D7 | 上游 lit patch：一个 CLI 选项 / directive / bugfix，带自测与文档 | Ch 25 |

> **允许简化**：`mytool` 不需要是真正的编译器。一个确定性的 Python 脚本就够：读 `.toy`，做几项检查，输出固定格式的三地址码。项目的重点是**测试框架**，不是编译器。

## 目录布局

```
minitest/
├── CMakeLists.txt
├── pyproject.toml
├── requirements.txt
├── README.md
├── mytool/
│   ├── __init__.py
│   └── cli.py
├── formats/
│   ├── __init__.py
│   └── jsoncase.py
├── lit.cfg.py
├── lit.site.cfg.py.in
└── tests/
    ├── smoke/
    │   ├── compile-basic.test
    │   ├── compile-error.test
    │   └── check-basic.test
    ├── features/
    │   ├── fast-path.test
    │   └── xfail-known-bug.test
    ├── jsoncase/
    │   ├── add.case
    │   ├── div-zero.case
    │   └── parse-error.case
    └── Inputs/
        ├── basic.toy
        ├── error.toy
        └── expected-basic.txt
```

`test_exec_root` 必须在构建树或 `Output/`，不能在源码树。`.gitignore` 加 `Output/`、`lit-results.*`。

## 里程碑

### M0 — 基线：先让 1 个测试跑起来

**目标**：`lit tests` 能发现并跑一个 ShTest。

最小 `lit.cfg.py`：

```python
import os
import lit.formats

config.name = "minitest"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.excludes = ["Inputs"]
```

最小测试 `tests/smoke/compile-basic.test`：

```text
# RUN: %{mytool} compile %S/Inputs/basic.toy -o %t.out
# RUN: FileCheck %s < %t.out

; CHECK: t0 = 1 + 2
; CHECK: ret t0
```

**验收**：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -v tests
# PASS: minitest :: smoke/compile-basic.test
```

**卡点提示**：`%{mytool}` 还没定义；先用绝对路径或 `python3 -m mytool`。Ch 14 会讲 `ToolSubst`。

### M1 — 配置与工具替换：让 suite 可移植

**目标**：不再硬编码路径；用 `lit.site.cfg.py.in` + `ToolSubst`。

`lit.cfg.py` 增加：

```python
import lit.llvm
from lit.llvm.subst import ToolSubst

lit.llvm.initialize(lit_config, config)
lit.llvm.llvm_config.add_tool_substitutions([
    ToolSubst("%{mytool}", command=config.mytool_path, unresolved="fatal"),
])
config.available_features.add("mytool")
```

`lit.site.cfg.py.in`：

```python
@LIT_SITE_CFG_IN_HEADER@

config.mytool_path = path(r"@MYTOOL_PATH@")
config.test_exec_root = path(r"@CMAKE_BINARY_DIR@/tests")

import lit.llvm
lit.llvm.initialize(lit_config, config)
lit_config.load_config(config, os.path.join(r"@CMAKE_SOURCE_DIR@", "lit.cfg.py"))
```

`CMakeLists.txt`：

```cmake
configure_lit_site_cfg(
  ${CMAKE_CURRENT_SOURCE_DIR}/lit.site.cfg.py.in
  ${CMAKE_CURRENT_BINARY_DIR}/lit.site.cfg.py
  MAIN_CONFIG ${CMAKE_CURRENT_SOURCE_DIR}/lit.cfg.py)

add_lit_testsuite(check-minitest "Running minitest"
  ${CMAKE_CURRENT_BINARY_DIR}
  PARAMS "mytool=$<TARGET_FILE:mytool>"
  DEPENDS mytool)
```

**验收**：

```bash
cmake -S . -B build -G Ninja
ninja -C build check-minitest
```

**卡点提示**：`configure_lit_site_cfg` 的 `PATHS`/`MAIN_CONFIG` 参数、`@VAR@` 替换、`lit_config.load_config` 的回加载，Ch 12 有逐段解释。

### M2 — 写「会失败」的测试：ShTest 全覆盖

**目标**：至少 8 个 ShTest，覆盖：

1. 正常编译输出（FileCheck）。
2. 语法错误：退出码非 0 + 错误信息。
3. `-o` 输出文件：`%t` + `diff`。
4. `REQUIRES: fast` 的条件测试。
5. `XFAIL: *` 的已知 bug。
6. `UNSUPPORTED: windows`（或平台 feature）。
7. `ALLOW_RETRIES: 1` 的 flaky 演示（用确定性方式模拟）。
8. 一个故意写错的测试，用来验证「测试真的会红」（验证完删掉或标 `XFAIL`）。
9. `%{readfile:...}` 或 `%{s:stem}` 的替换示例。
10. `DEFINE`/`REDEFINE` 的参数化示例（可选）。

**验收**：

```bash
lit -v -a tests
# 期望：PASS 数 >= 8；FAIL 0；至少 1 个 XFAIL；至少 1 个 UNSUPPORTED
```

**卡点提示**：

- `%t` 是文件名，不是目录；要目录用 `%t.d` 或 `rm -rf %t && mkdir %t`。
- `XFAIL` 通过会变 `XPASS`（failure），不要用它来「跳过」。
- `REQUIRES` 拼错是 `UNRESOLVED`，不是 `FAIL`。

### M3 — 自定义 format：JsonCase

**目标**：实现 `JsonCase` format，支持 `.case` 文件，并让 lit 发现/执行/报告。

`.case` 格式：

```json
{
  "name": "add",
  "command": ["%{mytool}", "compile", "Inputs/add.toy"],
  "exit": 0,
  "stdout_contains": ["t0 = 1 + 2"],
  "requires": ["fast"],
  "xfail": false
}
```

`formats/jsoncase.py` 骨架：

```python
import json
import os

import lit.Test
import lit.TestRunner
import lit.util


class JsonCaseFormat(lit.formats.TestFormat):
    def getTestsInDirectory(self, testSuite, path_in_suite, litConfig, localConfig):
        source_path = testSuite.getSourcePath(path_in_suite)
        for filename in sorted(os.listdir(source_path)):
            if filename.endswith(".case"):
                yield lit.Test.Test(
                    testSuite, path_in_suite + (filename,), localConfig
                )

    def execute(self, test, litConfig):
        if test.config.unsupported:
            return lit.Test.Result(lit.Test.UNSUPPORTED, "Test is unsupported")

        path = test.getSourcePath()
        try:
            with open(path, encoding="utf-8") as f:
                spec = json.load(f)
        except Exception as e:
            return lit.Test.Result(lit.Test.UNRESOLVED, f"bad JSON: {e}")

        # 把 JSON 里的 requires/xfail 接到 Test 对象上
        test.requires.extend(spec.get("requires", []))
        if spec.get("xfail"):
            test.xfails.append("*")

        if litConfig.noExecute:
            return lit.Test.Result(lit.Test.PASS)

        cmd = [os.path.expandvars(x) for x in spec["command"]]
        cwd = os.path.dirname(path)
        try:
            out, err, code = lit.util.executeCommand(
                cmd,
                cwd=cwd,
                env=test.config.environment,
                timeout=test.config.maxIndividualTestTime,
            )
        except lit.util.ExecuteCommandTimeoutException as e:
            return lit.Test.Result(lit.Test.TIMEOUT, str(e))

        expected_exit = spec.get("exit", 0)
        if code != expected_exit:
            return lit.Test.Result(
                lit.Test.FAIL,
                f"exit {code}, expected {expected_exit}\nstdout:\n{out}\nstderr:\n{err}",
            )

        for needle in spec.get("stdout_contains", []):
            if needle not in out:
                return lit.Test.Result(
                    lit.Test.FAIL, f"missing {needle!r} in stdout:\n{out}"
                )

        return lit.Test.Result(lit.Test.PASS)
```

`lit.cfg.py` 里注册：

```python
config.test_format = lit.formats.ShTest()
config.suffixes = [".test", ".case"]
# 关键：给 .case 用自定义 format
from formats.jsoncase import JsonCaseFormat
config.test_format = lit.formats.ShTest()
config.case_format = JsonCaseFormat()
```

但 `TestingConfig` 只有一个 `test_format`，不能按后缀自动分流。两种做法：

**做法 A（推荐）**：在 `getTestsInDirectory` 里同时处理两种后缀，或者用 `lit.local.cfg` 在不同目录切 format：

```
tests/smoke/lit.local.cfg.py       → ShTest, suffixes [".test"]
tests/jsoncase/lit.local.cfg.py    → JsonCaseFormat, suffixes [".case"]
```

**做法 B**：写一个 `DispatchingFormat`，在 `getTestsInDirectory` 里按后缀选择子 format，在 `execute` 里委托。这是更通用的设计，也是加分项。

**验收**：

```bash
lit --show-tests tests/jsoncase
# minitest :: jsoncase/add.case
# ...
lit -v tests/jsoncase
# PASS/FAIL/XFAIL 与 spec 一致
```

**卡点提示**：

- `JsonCaseFormat` 不调用 `parseIntegratedTestScript`，所以 `XFAIL`/`REQUIRES` 不会自动解析；要手动接到 `test.requires`/`test.xfails`，或者调用 `parseIntegratedTestScript(test, require_script=False)`。
- `lit.util.executeCommand` 的 `timeout` 是**单命令**超时；`test.config.maxIndividualTestTime` 来自 `--timeout` 或 config。
- `Test.Result` 的 `output` 会进 xUnit/JSON 报告；写清失败原因。

### M4 — 报告与诊断：让失败可检索

**目标**：CI 能消费结果。

```bash
lit -v -j 4 \
  --xunit-xml-output lit-results.xml \
  -o lit-results.json \
  --time-tests=20 \
  tests
```

验收：

- `lit-results.xml` 能在 CI 里展开失败用例。
- `lit-results.json` 含 `__version__`、`elapsed`、`tests[].code/name/output`。
- `--time-tests=20` 打印最慢 20 个测试。
- 故意让一个测试 FAIL，确认 xUnit/JSON 都有它，且退出码为 1。
- 把 suite 路径写错，确认退出码为 2（不是 1）。

### M5 — 工程化：CI、跨平台、性能

**目标**：别人 clone 下来能跑。

- `pyproject.toml` / `requirements.txt` pin lit 版本（git commit 或 `lit==...`）。
- GitHub Actions 矩阵：Ubuntu/macOS/Windows；`set -o pipefail`；`if: always()` 上传报告。
- `--timeout=60`、`--max-time=1800`、`-j 4`。
- `parallelism_group` 限制会抢资源的测试（例如写同一临时目录的测试）。
- Windows 路径：`%{pathsep}`、`%{fs-sep}`、`.exe`。
- `README.md` 写清：怎么跑一个测试、怎么加测试、失败怎么看、CI 怎么复现。

**验收**：

```bash
# 干净环境
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
lit -v tests
```

### M6 — 上游 patch：给 lit 留一个改动

**目标**：选一个 `tools:llvm-lit` 的小 issue，或自己发现一个可复现的小问题，提交 patch。

最低要求：

- 一个最小复现（放 `/tmp` 或 issue 评论，不进 repo）。
- 一个实现改动（CLI 选项 / directive / 错误信息 / bugfix）。
- 一个 lit 自测（`llvm/utils/lit/tests` 下的 `.py`，或现有测试加 `RUN:` 段）。
- 文档更新（`lit.md` / `TestingGuide.md`）。
- 如果用户可见，`ReleaseNotes.md` 的 `Changes to the LLVM tools` 加一条。
- 本地自测通过：

```bash
cp -a llvm/utils/lit /tmp/lit-selftest-src
(cd /tmp/lit-selftest-src && python3 lit.py --path <build>/bin -j 8 tests)
```

**可选的 patch 方向**（挑一个，不要贪多）：

1. 给 `lit` 加一个 `--max-warnings` 选项（warning 超过 N 就失败）。
2. 给 `SHTest` 加一个 `TIMEOUT:` directive（单测试超时，覆盖 CLI）。
3. 改一个错误信息，让它包含文件/行/期望/实际。
4. 修一个 Windows 路径/大小写/`.exe` 的小 bug。
5. 给 `JsonReport` 加一个字段（例如 `attempts`）。

**不要选**：

- 重构整个 `TestRunner`。
- 改 `PASS: ...` 输出格式（会破坏 CI 解析）。
- 加一个需要新依赖的功能（reviewer 会问「能不能不用」）。
- 只改文档不改代码的「大改」。

## 里程碑 ↔ 课程章节对照

做 final 时最怕「卡住不知道回哪一章」。下表把每个里程碑映射到本课的具体章节；只在你真的卡住时回读，不要先把 26 章重看一遍。

| 里程碑 | 主要依赖章节 | 卡住时的第一诊断动作 |
|---|---|---|
| M0 基线 | Ch 0、Ch 4、Ch 11 | `lit -v tests` 看 `# RUN: at line N`；0 tests 先查 `suffixes` 与 `test_source_root` |
| M1 配置与工具替换 | Ch 10、Ch 12、Ch 13、Ch 14 | `lit --show-suites` 看 suite 是否加载；`%{mytool}` 未定义按 Ch 14 的 `unresolved` 四态排查 |
| M2 会失败的测试 | Ch 5、Ch 6、Ch 7、Ch 9 | 先故意改坏一条 `CHECK`，确认它会 FAIL；不失败说明断言写空了 |
| M3 自定义 format | Ch 19、Ch 21、Ch 23、练习 D | `--show-tests` 看 case 是否被发现；`-v` 看 `execute()` 返回的 `Result` 码 |
| M4 报告与诊断 | Ch 17、Ch 18、练习 C | 先用 `--xunit-xml-output` 生成报告，再从 `FAIL` 条目反查 `# executed command` |
| M5 工程化 | Ch 15、Ch 24 | `-j 1` 能过而 `-j 8` 失败，按 Ch 15 查共享临时文件与 `parallelism_group` |
| M6 上游 patch | Ch 25 | 先跑 `llvm/utils/lit/tests` 自测；再按 Ch 25 的四件套（实现/测试/文档/Release Notes）自查 |

两个使用建议：

1. **按里程碑回读，不按章节顺序回读**。M3 卡在 `TestFormat` 接口时，Ch 19 讲解析、Ch 21 讲结果、Ch 23 讲 GoogleTest 的注册方式，三处各取所需即可。
2. **每完成一个里程碑就冻结一次验收命令**。把当次真实输出贴进 `NOTES.md`，后面重构时用同一条命令对比；这比「感觉没坏」可靠。

## 验收标准

### 功能验收

- [ ] `lit --show-suites` 能看到 `minitest`，`Source Root`/`Exec Root` 正确。
- [ ] `lit --show-tests` 能发现 `.test` 与 `.case` 两类测试。
- [ ] 至少 8 个 ShTest + 5 个 JsonCase。
- [ ] 至少 1 个 `XFAIL`、1 个 `UNSUPPORTED`、1 个 `REQUIRES` 条件测试。
- [ ] 至少 1 个测试在故意改坏后变红（验证过）。
- [ ] `-v` 失败输出包含 `RUN: at line N`、`# executed command`、`# error`。
- [ ] `--xunit-xml-output` 与 `-o` JSON 都能生成且包含失败用例。
- [ ] `--time-tests=20` 有输出。
- [ ] suite 路径写错时退出码为 2；测试失败时退出码为 1；全过为 0。
- [ ] 源码树没有 `Output/` 或测试产物（`git status` 干净）。
- [ ] 在干净 venv 里 `pip install -r requirements.txt && lit tests` 能跑。
- [ ] CI 配置能跑（至少一个平台真跑过；其他平台标注未实测）。

### 上游 patch 验收

- [ ] 最小复现能说明问题。
- [ ] 改动是 lit 层，不是某个项目的 `lit.cfg`。
- [ ] 自测在干净副本上通过。
- [ ] 文档/Release Notes 更新。
- [ ] commit message 用 `[lit] ...`，正文解释「为什么」。
- [ ] PR 描述包含问题、方案、测试、兼容性、范围。

## 评分 rubric（100 分）

| 维度 | 分值 | 标准 |
|---|---|---|
| Suite 配置 | 15 | `lit.cfg`/site cfg/CMake 正确，source/exec 分离，无硬编码路径 |
| ShTest 覆盖 | 20 | 正常/边界/失败/条件/预期失败齐全，失败可诊断 |
| JsonCase format | 20 | 发现/执行/超时/错误处理/报告正确，代码可读 |
| 报告与诊断 | 15 | xUnit/JSON/时间直方图/退出码/失败现场齐全 |
| 工程化 | 15 | 版本 pin、CI、跨平台、并行、超时、README |
| 上游 patch | 15 | 最小复现、实现、自测、文档、Release Notes、commit 风格 |

## 参考时间线

| 阶段 | 时间 | 产出 |
|---|---|---|
| M0 | 0.5 天 | 1 个 ShTest 跑通 |
| M1 | 1 天 | site cfg + ToolSubst + CMake |
| M2 | 1.5 天 | 8+ ShTest，覆盖 directives |
| M3 | 2 天 | JsonCase format + 5 个 case |
| M4 | 0.5 天 | xUnit/JSON/time-tests |
| M5 | 1 天 | CI + 跨平台 + 文档 |
| M6 | 1–2 天 | lit 上游 patch + 自测 |
| 总计 | 7.5–8.5 天 | 可发布框架 + PR |

## 加分挑战

1. **DispatchingFormat**：一个 format 按后缀/内容分派到 ShTest/JsonCase/GoogleTest。
2. **`--json-shard`**：把 JsonCase 测试分片，和 lit 的 `--num-shards` 对齐。
3. **失败最小化**：写一个脚本，从失败 JsonCase 自动生成最小 `.case`。
4. **报告对比**：比较两次 `lit-results.json`，输出「新增失败/修复/变慢」。
5. **上游 patch 被合并**：这是终极加分项；即使没合并，拿到 reviewer 的实质性 review 也算。

## 自我检核

- [ ] 你的项目里，哪一部分是「lit 提供的」，哪一部分是「你写的」？能画出边界吗？
- [ ] 如果 `lit --show-tests` 看不到 `.case`，你会按什么顺序排查（suffixes / format / local cfg / 发现缓存）？
- [ ] JsonCase 的 `xfail` 字段和 lit 原生 `XFAIL:` 是同一套语义吗？如果不是，你如何让它们一致？
- [ ] 你的 CI 在测试失败时真的会红吗？`set -o pipefail`、`if: always()`、退出码 2 都处理了吗？
- [ ] 你的上游 patch 如果被 reviewer 要求「拆成两个 PR」，你会怎么拆？
- [ ] 这门课 26 章里，哪三章对你的项目影响最大？为什么？

## 延伸阅读

- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读什么：`LIT INFRASTRUCTURE` 与所有 OPTIONS。
  - 能学到：你项目里用到的每个 CLI/format 的官方语义；写 README 时可直接引用。
  - 前提：完成 M0–M4。
- **[LLVM Testing Infrastructure Guide](https://llvm.org/docs/TestingGuide.html)**
  - 读什么：`Regression test structure`、`Extra files`、`Substitutions`、`Constraining test execution`。
  - 能学到：怎么把 `minitest` 的测试写得像 LLVM 一样可维护；`Inputs/`、`split-file`、`REQUIRES` 的惯例。
  - 前提：完成 M2。
- **[`llvm/utils/lit/tests`](https://github.com/llvm/llvm-project/tree/main/llvm/utils/lit/tests)**
  - 读什么：`shtest-shell.py`、`lit-opts.py`、`xfail-cl.py`、`parallelism-groups.py`。
  - 能学到：lit 官方怎么测 shell/CLI/XFAIL/并行；你的 format 自测可以照这个结构写。
  - 前提：完成 M3。
- **[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html)**
  - 读什么：`Review process` 与 `Patch reviews`。
  - 能学到：M6 的 PR 怎么写得让 reviewer 愿意看；怎么回应「请拆小」。
  - 前提：完成 M5。

> 做完这个项目，你不再只是「会写 lit 测试」；你能为一个团队搭测试基础设施，也能改 lit 本身。回到课程地图，挑一个你最想深挖的方向继续：`llvm/utils/lit` 源码、某个项目的测试套件，或 LLVM 上游的 `tools:llvm-lit` issue。

→ [返回课程地图](./README.md)
