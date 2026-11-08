# 练习 A — 写一组能诊断的 LLVM IR 测试

> **目标**：为一个真实的 `opt` pass 搭出 6 个用例的 lit suite，覆盖正常路径、边界输入、`REQUIRES`、`XFAIL` 和错误路径；再亲手制造一次失败，确认这套测试不是「永远不会红的注释」。

> **背景**：LLVM 代码评审里最常见的拒绝理由不是「测试没覆盖」，而是「测试通过得不对」。一个 `CHECK` 写得太松、一个 `XFAIL` 标错、一个 `pipefail` 被关掉，都会让测试在 bug 面前保持绿色。本练习练的是**让失败发生在正确的层**：pass 变了就红在 FileCheck，工具找不到就红在 127，预期失败就红在 XFAIL→XPASS。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，`opt`/`FileCheck`/`not`/`count` 来自 `/home/ubuntu/llvm/builds/pr-lane/bin`。本练习的参考解答在 `/tmp` 实跑过，下面所有输出都是真实输出。

---

## A.1 任务规格

为 `opt -passes=instcombine` 写一个 suite，目录结构如下：

```text
practice-a/
├── lit.cfg.py
├── tests/
│   ├── 01-add-zero.test
│   ├── 02-constant-fold.test
│   ├── 03-already-canonical.test
│   ├── 04-requires-asserts.test
│   ├── 05-xfail.test
│   └── 06-error-path.test
├── diagnostics/
│   └── broken-check.test
└── Output/                 ← lit 生成，加进 .gitignore
```

要求：

| 文件 | 必须覆盖 | 期望状态 |
|---|---|---|
| `01-add-zero.test` | 正常路径：`add i32 %x, 0` 被消掉 | PASS |
| `02-constant-fold.test` | 边界输入：常量折叠 `1 + 2 → 3` | PASS |
| `03-already-canonical.test` | 边界输入：已经规范的 IR 不被改坏 | PASS |
| `04-requires-asserts.test` | `REQUIRES: asserts` 条件 | PASS（有 asserts）/ UNSUPPORTED（无 asserts） |
| `05-xfail.test` | `XFAIL: *` 预期失败 | XFAIL |
| `06-error-path.test` | `not opt ... 2>&1 \| FileCheck` 错误路径 | PASS |
| `diagnostics/broken-check.test` | 故意写错的 `CHECK`，用来验证诊断 | FAIL（单独运行） |

硬性约束：

1. `.ll` 文件里的 directive 必须用 `;` 注释，不能用 `#`。
2. 每个测试至少一个 `CHECK`，且至少有一个测试用 `CHECK-NEXT` 或 `CHECK-NOT`。
3. `05-xfail.test` 的失败必须是**真实的文本不匹配**，不能靠 `false` 之类的命令伪造。
4. `diagnostics/broken-check.test` 不能放进 `tests/`，否则整套 suite 永远是红的。

## A.2 期望输出

运行绿色部分：

```bash
cd /tmp/practice-a
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests
```

期望：

```text
-- Testing: 6 tests, 6 workers --
PASS: PracticeA :: tests/01-add-zero.test (1 of 6)
PASS: PracticeA :: tests/02-constant-fold.test (2 of 6)
PASS: PracticeA :: tests/03-already-canonical.test (3 of 6)
PASS: PracticeA :: tests/04-requires-asserts.test (4 of 6)
XFAIL: PracticeA :: tests/05-xfail.test (5 of 6)
PASS: PracticeA :: tests/06-error-path.test (6 of 6)

Testing Time: 0.05s

Total Discovered Tests: 6
  Passed           : 5 (83.33%)
  Expectedly Failed: 1 (16.67%)
```

退出码是 **0**。`XFAIL` 不是失败。

运行诊断部分：

```bash
python3 .../lit.py diagnostics -v
```

期望：**1 个 FAIL，退出码 1**，并出现 `CHECK-NEXT: expected string not found in input`、`possible intended match here` 和 `Input was:` 转储。

验证 `REQUIRES`：

```bash
PRACTICE_A_NO_ASSERTS=1 python3 .../lit.py tests
```

期望：`04-requires-asserts.test` 变成 `UNSUPPORTED`，其余 4 PASS + 1 XFAIL，退出码仍是 0。

## A.3 卡点提示

1. **注释符**：`.ll` 里写 `# RUN:`，`opt` 会把 `#` 当成非法 IR，报 `expected top-level entity`。必须写 `; RUN:`。
2. **工具替换**：`%opt`、`%FileCheck` 不是 lit 内置的，必须在 `lit.cfg.py` 里 `config.substitutions.append(...)`；否则会变成字面命令名，127。
3. **工具路径**：默认用 `/home/ubuntu/llvm/builds/pr-lane/bin`；换机器时设 `LLVM_BIN=/path/to/llvm/bin`，`lit.cfg.py` 会读这个环境变量。
4. **默认 cwd 与 `%t`**：cwd 是 `test_exec_root/tests`，`%t` 是 `cwd/Output/<name>.tmp`。本练习不需要临时文件，但别把 `Output/` 提交进仓库。
5. **`CHECK-LABEL` 会吃掉 `(`**：`CHECK-LABEL: define i32 @f(` 之后的 `CHECK-SAME` 要从 `i32` 开始写，不要重复 `(`。
6. **行首空格**：FileCheck 默认忽略行首空白（除非 `--strict-whitespace`），所以 `; CHECK-NEXT:    ret i32 %x` 里的缩进不影响匹配。
7. **XFAIL 与 XPASS**：`XFAIL: *` 的测试如果意外通过，lit 报 `XPASS`，而 `XPASS` 是 failure，不是「更好」。
8. **`not` 的位置**：`not %opt ... 2>&1 | %FileCheck` 保护的是 `opt` 的失败；写成 `not %FileCheck` 就变成「FileCheck 必须失败」，语义完全不同。

## A.4 实作建议（5 步）

1. **先让一个测试跑起来**：只写 `lit.cfg.py`、`01-add-zero.test`，确认 `PASS` 和 `Exit Code: 0`。不要一次写 6 个。
2. **加边界输入**：`02` 用常量折叠，`03` 用已经规范的函数。边界测试的价值是「pass 没乱改」，不是「pass 又优化了什么」。
3. **加 `REQUIRES`**：在 `lit.cfg.py` 里用 `opt --version` 检测 `assertions`，再给 `04` 加 `REQUIRES: asserts`。用环境变量关掉 feature，确认它变成 UNSUPPORTED。
4. **加 `XFAIL`**：写一个当前构建确实不满足的严格 CHECK，确认状态是 `XFAIL` 而不是 `FAIL`；再想一遍「它什么时候会 XPASS」。
5. **加诊断与 mutation test**：把 `diagnostics/broken-check.test` 跑红，读 FileCheck 的三段信息（找不到、扫描起点、可能匹配）；然后复制 `01`、只改一个 CHECK，确认它也会红，最后删掉副本。

## A.5 完整参考解答

<details>
<summary>点开查看完整 suite（可直接复制运行）</summary>

### `lit.cfg.py`

```python
import os
import subprocess

import lit.formats

config.name = "PracticeA"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")

LLVM_BIN = os.environ.get("LLVM_BIN", "/home/ubuntu/llvm/builds/pr-lane/bin")

config.substitutions.append(("%opt", os.path.join(LLVM_BIN, "opt")))
config.substitutions.append(("%FileCheck", os.path.join(LLVM_BIN, "FileCheck")))
config.substitutions.append(("%count", os.path.join(LLVM_BIN, "count")))
config.substitutions.append(("%not", os.path.join(LLVM_BIN, "not")))

# Gate assertion-dependent tests on a real build property.
version = subprocess.run(
    [os.path.join(LLVM_BIN, "opt"), "--version"],
    capture_output=True,
    text=True,
).stdout
if "assertions" in version:
    config.available_features.add("asserts")
if os.environ.get("PRACTICE_A_NO_ASSERTS"):
    config.available_features.discard("asserts")
```

### `tests/01-add-zero.test`

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @add_zero(i32 %x) {
  %r = add i32 %x, 0
  ret i32 %r
}

; CHECK-LABEL: define i32 @add_zero(
; CHECK-NEXT:    ret i32 %x
; CHECK-NOT:     add i32
```

### `tests/02-constant-fold.test`

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @fold() {
  %r = add i32 1, 2
  ret i32 %r
}

; CHECK-LABEL: define i32 @fold(
; CHECK-NEXT:    ret i32 3
```

### `tests/03-already-canonical.test`

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @id(i32 %x) {
  ret i32 %x
}

; CHECK-LABEL: define i32 @id(
; CHECK-NEXT:    ret i32 %x
; CHECK-NEXT:  }
```

### `tests/04-requires-asserts.test`

```text
; REQUIRES: asserts
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @with_nsw(i32 %x) {
  %r = add nsw i32 %x, 0
  ret i32 %r
}

; CHECK-LABEL: define i32 @with_nsw(
; CHECK-NEXT:    ret i32 %x
```

### `tests/05-xfail.test`

```text
; XFAIL: *
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

; Known gap: this build splits `udiv %x, 2` into `lshr` but renames the
; result from `%r` to `%r1`. We keep this strict expectation so that the
; day the name is preserved, lit reports XPASS and we update the test.
define i32 @udiv_two(i32 %x) {
  %r = udiv i32 %x, 2
  ret i32 %r
}

; CHECK-LABEL: define i32 @udiv_two(
; CHECK-NEXT:    %r = lshr i32 %x, 1
; CHECK-NEXT:    ret i32 %r
```

### `tests/06-error-path.test`

```text
; RUN: %not %opt -S -passes=no-such-pass %s 2>&1 | %FileCheck %s --check-prefix=ERR

define void @f() {
  ret void
}

; ERR: unknown pass
```

### `diagnostics/broken-check.test`

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @add_zero(i32 %x) {
  %r = add i32 %x, 0
  ret i32 %r
}

; This CHECK is deliberately wrong: instcombine rewrites `ret %r` to
; `ret %x`, so CHECK-NEXT cannot find `ret i32 %r`.
; CHECK-LABEL: define i32 @add_zero(
; CHECK-NEXT:    ret i32 %r
```

### 运行

```bash
mkdir -p /tmp/practice-a/tests /tmp/practice-a/diagnostics
# 把上面的文件分别写入对应路径
cd /tmp/practice-a
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests
```

真实输出：

```text
-- Testing: 6 tests, 6 workers --
PASS: PracticeA :: tests/01-add-zero.test (1 of 6)
PASS: PracticeA :: tests/04-requires-asserts.test (2 of 6)
XFAIL: PracticeA :: tests/05-xfail.test (3 of 6)
PASS: PracticeA :: tests/03-already-canonical.test (4 of 6)
PASS: PracticeA :: tests/02-constant-fold.test (5 of 6)
PASS: PracticeA :: tests/06-error-path.test (6 of 6)

Testing Time: 0.05s

Total Discovered Tests: 6
  Passed           : 5 (83.33%)
  Expectedly Failed: 1 (16.67%)
```

顺序会因并行而变，状态和统计不变；退出码 0。

### 验证它真的会失败

第一步，直接跑诊断目录：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py diagnostics -v
```

真实输出（截取）：

```text
FAIL: PracticeA :: diagnostics/broken-check.test (1 of 1)
Exit Code: 1

# | .../broken-check.test:11:15: error: CHECK-NEXT: expected string not found in input
# | ; CHECK-NEXT: ret i32 %r
# |               ^
# | <stdin>:4:22: note: scanning from here
# | define i32 @add_zero(i32 %x) {
# |                      ^
# | <stdin>:5:2: note: possible intended match here
# |  ret i32 %x
# |  ^
# error: command failed with exit status: 1
```

退出码 1。三段信息分别告诉你：哪个 CHECK 失败、从哪里开始找、最可能想匹配哪一行。

第二步，做一次 mutation test（改一个字符，看测试是否变红）：

```bash
cd /tmp/practice-a
cp tests/01-add-zero.test tests/01-mutated.test
sed -i 's/; CHECK-NEXT:    ret i32 %x/; CHECK-NEXT:    ret i32 %r/' tests/01-mutated.test
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests/01-mutated.test -v
rm tests/01-mutated.test
```

真实结果：

```text
FAIL: PracticeA :: tests/01-mutated.test (1 of 1)
Exit Code: 1
# | ...:9:15: error: CHECK-NEXT: expected string not found in input
# | ; CHECK-NEXT: ret i32 %r
# | <stdin>:5:2: note: possible intended match here
# |  ret i32 %x
```

如果改坏了 CHECK 它仍然是绿的，说明这个测试没有真正约束 pass 的输出。

第三步，验证 `REQUIRES`：

```bash
PRACTICE_A_NO_ASSERTS=1 python3 .../lit.py tests
```

真实输出：

```text
UNSUPPORTED: PracticeA :: tests/04-requires-asserts.test (2 of 6)
...
Total Discovered Tests: 6
  Unsupported      : 1 (16.67%)
  Passed           : 4 (66.67%)
  Expectedly Failed: 1 (16.67%)
```

退出码仍是 0：`UNSUPPORTED` 不是失败，但它证明 feature 门控生效了。

</details>

## A.6 测试用例表

| 文件 | 覆盖点 | 期望状态 | 失败时先看什么 |
|---|---|---|---|
| `01-add-zero.test` | 正常路径：add-zero 消除 | PASS | `CHECK-NEXT` 的扫描起点与 `possible intended match` |
| `02-constant-fold.test` | 边界：常量折叠 | PASS | `ret` 的常量是否变成 3 |
| `03-already-canonical.test` | 边界：已规范 IR 不被改坏 | PASS | 是否多出/少了行，`CHECK-NEXT` 是否错位 |
| `04-requires-asserts.test` | `REQUIRES: asserts` | PASS / UNSUPPORTED | `--show-used-features`、`opt --version` 是否含 assertions |
| `05-xfail.test` | `XFAIL` 预期失败 | XFAIL | 是否变成 `XPASS`（那是 failure） |
| `06-error-path.test` | `not` + 错误文本 | PASS | `unknown pass` 是否在 stderr 且被 `2>&1` 接住 |
| `diagnostics/broken-check.test` | 故意失败的诊断样例 | FAIL | FileCheck 的三段定位信息 |

## A.7 加分挑战

1. **把 `02` 改成 `CHECK-DAG` 版本**：让 `define` 与 `ret` 的顺序无关，观察 `CHECK-DAG` 的匹配区间和失败信息。
2. **加一个 `split-file` 用例**：一个 `.test` 里放两份 IR，分别跑 instcombine，再用 `CHECK-A`/`CHECK-B` 检查。
3. **制造一次 `XPASS`**：把 `05` 的 `udiv` 改成已经是 `%r = lshr i32 %x, 1`，让 CHECK 通过；观察 lit 报 `XPASS`，单条 `Exit Code: 0`，但 lit 整体退出码是 1。
4. **用 `count` 替换一条 CHECK**：`%opt ... | %count 2`，确认行数不符时输出 `Expected 2 lines, got 1.`。
5. **加 `UNSUPPORTED: system-windows`**：给依赖 `sh` 的用例加平台门控，并在 `--show-used-features` 里确认它被列出。
6. **压日志**：用 `FILECHECK_OPTS=--dump-input=never` 跑诊断用例，确认 `Input was:` 消失但失败信息仍在。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`.ll` 里也能写 `# RUN:`」 | `opt` 会把 `#` 当非法 IR；`.ll` 必须用 `; RUN:`，`.test` 才用 `#`。 |
| 「`XFAIL` 的测试不会执行」 | 它照常执行；只有真的失败才记 `XFAIL`，意外通过是 `XPASS`（failure）。`XPASS` 单条可能显示 `Exit Code: 0`，但 lit 整体退出码是 1。 |
| 「`UNSUPPORTED` 会让 lit 返回 1」 | 不会；`UNSUPPORTED`/`XFAIL`/`EXCLUDED` 都不算失败。只有 `FAIL`/`XPASS` 等让 lit 返回 1。 |
| 「`CHECK-LABEL` 之后的 `CHECK-SAME` 可以重复 `(`」 | `CHECK-LABEL` 已经把 `(` 匹配掉了，`CHECK-SAME` 从它结束的位置继续。 |
| 「`CHECK-NEXT` 会因为缩进对不上」 | 默认忽略行首空白；真正对不上的是「中间多了一行」。 |
| 「`not %FileCheck` 能测编译器错误」 | 它的语义是「FileCheck 必须失败」；测编译器错误要写 `not %opt ... \| %FileCheck`。 |
| 「测试绿了就不用管 `--filter`」 | `--filter` 会改变执行集合，摘要里出现 `Excluded` 是正常的；别把「没跑」当成「通过」。 |

## 重点整理

- 一套可诊断的 suite 至少要有：正常路径、边界输入、条件门控、预期失败、错误路径、故意失败的诊断样例。
- `.ll` 的 directive 用 `;`；`%opt`/`%FileCheck` 必须由 `lit.cfg.py` 注入。
- `REQUIRES` 用真实构建属性（如 `assertions`）门控，不要写死。
- `XFAIL` 是「已知失败」的正式记录；`XPASS` 是 failure，提醒你更新测试。
- mutation test 是验证测试有效性的最快方法：改一个 CHECK，确认它变红，再改回来。
- 退出码要分开读：0 全过（含 XFAIL/UNSUPPORTED）、1 有 FAIL/XPASS、2 lit 内部错误。

## 自我检核

- [ ] 你的 suite 里哪一个测试能在「pass 完全不优化」时仍然通过？如果都能通过，说明约束不够。
- [ ] `05-xfail.test` 在什么情况下会变成 `XPASS`？那时你应该改什么？
- [ ] 如果把 `06` 的 `2>&1` 去掉，FileCheck 会看到什么？测试会变成 PASS 还是 FAIL？为什么？
- [ ] `PRACTICE_A_NO_ASSERTS=1` 时，`04` 的状态是什么？lit 的退出码为什么仍是 0？
- [ ] 你能用一条命令证明 `tests/01-add-zero.test` 不是「永远绿」的吗？
- [ ] `%t` 在你的 suite 里指向哪？为什么本练习不需要它？

## 延伸阅读

- **[LLVM Testing Infrastructure Guide](https://llvm.org/docs/TestingGuide.html)**
  - 读什么：`Writing a test`、`Extra tools`、`REQUIRES`/`XFAIL` 的官方说明。
  - 能学到：LLVM 官方对「什么是一个好回归测试」的标准；对照本练习的 6 个用例。
  - 前提：读过 Ch 5 的 directive。
- **[FileCheck Command Guide](https://llvm.org/docs/CommandGuide/FileCheck.html)**
  - 读什么：`CHECK-LABEL`、`CHECK-NEXT`、`CHECK-DAG`、`--dump-input`。
  - 能学到：如何把「输出对不对」写成可定位的断言，而不是模糊的 grep。
  - 前提：跑过本练习的 `01` 和 `diagnostics`。
- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读什么：`--filter`、`--show-tests`、`--show-used-features`、退出码。
  - 能学到：如何只跑一个用例、如何确认 `REQUIRES` 用到的 feature 拼写正确。
  - 前提：有一个能跑的 suite。
- **[`llvm/test/Transforms/InstCombine/`](https://github.com/llvm/llvm-project/tree/main/llvm/test/Transforms/InstCombine)**
  - 读什么：任选 3 个 `.ll`，只看 `RUN:` 和 `CHECK` 的组织方式。
  - 能学到：真实上游测试如何平衡「覆盖充分」与「不过度约束」。
  - 前提：能读基本 LLVM IR。

> 下一章回到配置层：`lit_config` 与 `config` 为什么是两个对象，它们各自在哪一刻被创建、被复制、被修改。

→ [Ch 10 lit_config 与 config：两个对象的一生](./10-config-objects.md)
