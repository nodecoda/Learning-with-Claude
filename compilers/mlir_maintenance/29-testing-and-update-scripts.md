# Ch 29 — 测试基础设施：lit、诊断校验与更新脚本

> **目标**：把“我改了代码，跑了一下没崩”升级成“我能写出一条会在出错时真的失败的测试”。读完你要能：读懂任意 MLIR 测试的 `RUN` 行在做什么；知道 `CHECK` / `expected-error` / `UNSUPPORTED` 各自在哪一层生效；用 `generate-test-checks.py` 生成草稿并知道它为什么只是草稿；判断一次改动**必须**配哪种测试。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`（分支 `analysis/mlir-maintenance`）。构建含 assertions、`mlir-opt`、`FileCheck`、`llvm-lit`。
>
> **前置**：Ch 02（构建与测试矩阵）、Ch 07（verifier 与诊断）、Ch 13（复现与 reduce）。本章只讲“测试本身怎么工作”。

---

## 29.1 为什么维护者要先读测试设施

MLIR 的 PR 里，测试常常是 diff 的一半。本树近 6 个月 1,302 条 `[mlir]` 提交中，606 条（46.5%）在 3 个文件、80 行以内，典型形状是「1 个实现文件 + 1 个测试文件」。**这个项目一半的吞吐，是“改几行 + 改几条 CHECK”。**

不会写测试，会发生三件事：修复无法证明“修好了”（reviewer 不会凭读代码相信你）；无法证明“没修坏别的”；读不懂别人的 PR——`canonicalize.mlir` 里 12 行 CHECK 的变化，往往比 2 行 C++ 更能说明行为差异。

更现实的是：**测试写错的方式比代码多**——`CHECK-NEXT` 少一行、`expected-error` 放错行、`UNSUPPORTED` 拼错 feature 名，都会让测试“永远绿”。本章的目标就是识别这类**假绿**。

## 29.2 先建立直觉：一次 lit 运行的五个层次

```text
                  $ llvm-lit -sv <build>/tools/mlir/test/Dialect/Arith
                                   │
   ┌───────────────────────────────▼────────────────────────────────────┐
   │ ① suite 配置  mlir/test/lit.cfg.py                                   │
   │    suffixes(11 种) / available_features / ToolSubst(mlir-opt, ...)   │
   └───────────────────────────────┬────────────────────────────────────┘
                                   │ 递归进入目录
   ┌───────────────────────────────▼────────────────────────────────────┐
   │ ② lit.local.cfg（可选）：unsupported / 追加 feature                  │
   └───────────────────────────────┬────────────────────────────────────┘
                                   │ 每个文件
   ┌───────────────────────────────▼────────────────────────────────────┐
   │ ③ RUN 行：一个文件 = 一段微型脚本                                    │
   │    // RUN: mlir-opt %s -canonicalize | FileCheck %s                  │
   └───────────────┬────────────────────────────────┬───────────────────┘
                   │ stdout                          │ stderr / 诊断
   ┌───────────────▼───────────────┐  ┌─────────────▼───────────────────┐
   │ ④ FileCheck                   │  │ ⑤ DiagnosticVerifier            │
   │    CHECK / CHECK-NEXT / ...   │  │    expected-error / expected-note│
   └───────────────────────────────┘  └─────────────────────────────────┘
```

关键点：**④ 和 ⑤ 是两条互斥的验证路线**。正向测试把 IR 打到 stdout 用 FileCheck 比对；负向测试让诊断进 verifier，用注释里的 `expected-*` 比对。一个文件可以同时用两者，但你要清楚每一行受谁保护。

## 29.3 三层配置：suite、目录、文件

### 29.3.1 suite 级：`mlir/test/lit.cfg.py`

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
grep -n "config.suffixes\|config.test_format\|ToolSubst(\"mlir-opt\"\|add(\"asserts\")" mlir/test/lit.cfg.py
```

```text
24:config.test_format = lit.formats.ShTest()
26:config.suffixes = [
304:            ToolSubst("mlir-opt", "mlir-opt --verify-roundtrip", unresolved="fatal"),
364:    config.available_features.add("asserts")
```

`config.suffixes`（26–39 行）是 11 种扩展名：`.td .mlir .toy .ll .tc .py .yaml .test .pdll .c .spvasm`——所以 `.td` 也可以是测试（TableGen 输入同样能有 `RUN:` 行）。

304 行是隐藏开关：设置 `MLIR_OPT_CHECK_IR_ROUNDTRIP=1` 后，所有测试里的 `mlir-opt` 自动变成 `mlir-opt --verify-roundtrip`，每条管道顺带验证“打印再解析得到同一份 IR”。写 parser/printer 补丁时可以开着跑整套。默认不加。

### 29.3.2 目录级：`lit.local.cfg`

`mlir/test` 下有 58 个 `lit.local.cfg`。最短的一个解释了 `REQUIRES: x86-native-target` 从哪来：

```bash
cat mlir/test/mlir-runner/lit.local.cfg
```

```text
# MSAN does not work with JIT.
if "msan" in config.available_features:
    config.unsupported = True

# Requires native execution.
if "host-supports-jit" not in config.available_features:
    config.unsupported = True

config.available_features.add(config.root.native_target.lower() + "-native-target")
```

三行信息量很大：`config.unsupported = True` 是**整目录**跳过；`host-supports-jit` 由 `mlir-runner --host-supports-jit` 实测得到（`lit.cfg.py:374-399`）；目录配置可以**新增** feature——`x86-native-target` 就是这么来的。

### 29.3.3 文件级：只有 `RUN:` 和注释

单个测试文件不参与配置。没有 `RUN:` 会怎样？实测：

```bash
printf 'func.func @nop() { return }\n' > /tmp/mlir-lit/norun.mlir
$T/bin/llvm-lit -sv /tmp/mlir-lit/norun.mlir
```

```text
Test has no 'RUN:' line
********************
Unresolved Tests (1):
  mlir-demo :: norun.mlir

Total Discovered Tests: 1
  Unresolved: 1 (100.00%)
```

状态词是 **Unresolved**，不是 Failed：意思是“不知道怎么跑”，不是“跑错了”。看到它先查 `RUN:`，别查代码。

## 29.4 RUN 行：一份被误认为 shell 的脚本

`llvm/docs/TestingGuide.md:193` 明确说：`RUN` 行**看起来像 shell，但不是 shell**，由 lit 自己解释。实践记住五条：

| 写法 | 含义 | 常见误用 |
|---|---|---|
| `%s` | 当前测试源文件 | 写成 `$s`（shell 展开为空） |
| `%t` | 本测试专属临时文件 | 写死 `/tmp/foo`，并行时互相踩 |
| `\|` | 管道 | 以为支持 `&&`、`2>&1` 的全部 shell 语义 |
| `not` | 反转退出码 | 用 `! cmd`（内置 shell 不认） |
| 行尾 `\` | 续行 | 断行不带 `\`，后半行被当新命令 |

`not` 由 lit.cfg 显式注册（`mlir/test/lit.cfg.py:207`），负向测试常用 `// RUN: not mlir-opt %s --some-pass 2>&1 | FileCheck %s`。

### 29.4.1 `-split-input-file`：一个文件，多个独立世界

`-split-input-file` 用 `// -----` 把文件切成块，**每块独立解析、独立跑 pipeline、独立验证**：

```bash
cat > /tmp/mlir-lit/split-demo.mlir <<'MLIR'
// RUN: mlir-opt %s -split-input-file -canonicalize

func.func @a(%x: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %r = arith.addi %x, %c0 : i32
  return %r : i32
}

// -----

func.func @b(%y: i32) -> i32 {
  %c1 = arith.constant 1 : i32
  %r = arith.muli %y, %c1 : i32
  return %r : i32
}
MLIR
$T/bin/mlir-opt /tmp/mlir-lit/split-demo.mlir -split-input-file -canonicalize
```

```text
module {
  func.func @a(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}

// -----
module {
  func.func @b(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

两个要点：输出也带 `// -----`（由 `--output-split-marker` 控制），所以 CHECK 可以按块写；每块**独立编号**，两块都用 `%arg0` 不冲突。去掉 split，同名函数会报重定义。

### 29.4.2 为什么 `invalid-ops.mlir` 的 RUN 行那么长

```bash
sed -n 1p mlir/test/IR/invalid-ops.mlir
# // RUN: mlir-opt -allow-unregistered-dialect %s -split-input-file -verify-diagnostics
```

四个参数各司其职，少一个就红。最容易被删的是 `-allow-unregistered-dialect`，因为它在**大多数块**里都没用。实测删掉：

```bash
$T/bin/mlir-opt mlir/test/IR/invalid-ops.mlir -split-input-file -verify-diagnostics 2>&1 | head -3; echo "exit=$?"
```

```text
mlir/test/IR/invalid-ops.mlir:100:15: error: unexpected error: operation being parsed with an unregistered dialect. If this is intended, please use -allow-unregistered-dialect with the MLIR tool used
  "foo.region"() ({
exit=1
```

100 行、120 行用了 `"foo.region"` / `"g"` 这种未注册方言的 generic 形式；没有这个 flag，解析器直接拒绝，后面的块根本没机会跑。**RUN 行里每个 flag 都服务某个具体块，删之前先 `grep` 它服务哪一行。**

## 29.5 CHECK 语义：FileCheck 到底在比什么

一条 `CHECK` 是“从这里往后找，找到第一处匹配”，不是“整行相等”。六个指令的差别必须背：

| 指令 | 语义 | 用错会怎样 |
|---|---|---|
| `CHECK` | 从上次匹配位置向后找 | 顺序变了仍通过 → 漏掉顺序类 bug |
| `CHECK-NEXT` | 必须是**紧邻下一行** | 多一个空行就红，脆 |
| `CHECK-SAME` | 与上一条在**同一行** | 换行位置变了就红 |
| `CHECK-LABEL` | 划分区块，重置搜索起点 | 少了它，跨函数匹配会串 |
| `CHECK-NOT` | 两条 CHECK 之间不得出现 | 放最后时作用域到文件尾 |
| `CHECK-DAG` | 一组无序匹配 | 用于顺序无关的场景 |

### 29.5.1 一个会真的失败的测试

先看通过的样子（`/tmp/mlir-lit` 的配置见 29.11 实验 1）：

```bash
$T/bin/llvm-lit -sv /tmp/mlir-lit
```

```text
-- Testing: 6 tests, 6 workers --
FAIL: mlir-demo :: bad-check.mlir (5 of 6)
...
Testing Time: 0.32s

Total Discovered Tests: 6
  Unsupported      : 1 (16.67%)
  Passed           : 3 (50.00%)
  Expectedly Failed: 1 (16.67%)
  Failed           : 1 (16.67%)
```

失败信息是本章最该会读的：

```text
# RUN: at line 1
mlir-opt /tmp/mlir-lit/bad-check.mlir -canonicalize | FileCheck /tmp/mlir-lit/bad-check.mlir
# | /tmp/mlir-lit/bad-check.mlir:4:16: error: CHECK-NEXT: expected string not found in input
# | // CHECK-NEXT: return %c0
# |                ^
# | <stdin>:3:2: note: possible intended match here
# |  return %arg0 : i32
```

三行定位法：第一行告诉你哪个前缀；`scanning from here` 是搜索起点；`possible intended match` 是实际输出。这里的测试写错了（`%c0` 已被 `-canonicalize` 折掉），但它演示了正确行为：断言必须写实际输出。

### 29.5.2 变量是断言的载体

```text
// CHECK-LABEL: func.func @f(
// CHECK-SAME:    %[[ARG0:.*]]: i32,
// CHECK-SAME:    %[[ARG1:.*]]: i32) -> i32 {
// CHECK:           %[[ADDI_0:.*]] = arith.addi %[[ARG0]], %[[ARG1]] : i32
// CHECK:           return %[[ADDI_0]] : i32
```

`%[[NAME:regex]]` 是“捕获 + 复用”。它能挡住**操作数接错**：若实现写成 `addi %b, %a`，绑定到具体 SSA 名的 `%[[ARG0]]`/`%[[ARG1]]` 就不匹配。变量不是装饰，它是断言的载体。

## 29.6 负向测试：`--verify-diagnostics` 的三个档位

`--verify-diagnostics` 不是布尔开关，而是三级枚举（`MlirOptMain.cpp:182`；枚举在 `Diagnostics.h:689`）：

```text
enum class Level { None = 0, All, OnlyExpected };
```

实测三档（同一个“有错误但没有 expected 注释”的文件）：

```bash
$T/bin/mlir-opt /tmp/mlir-lit/unexpected.mlir                        ; echo "plain exit=$?"
$T/bin/mlir-opt --verify-diagnostics /tmp/mlir-lit/unexpected.mlir   ; echo "all exit=$?"
$T/bin/mlir-opt --verify-diagnostics=only-expected /tmp/mlir-lit/unexpected.mlir ; echo "only-expected exit=$?"
```

```text
unexpected.mlir:2:23: error: use of undeclared SSA value name
plain exit=1
unexpected.mlir:2:23: error: unexpected error: use of undeclared SSA value name
all exit=1
only-expected exit=0
```

**本章最重要的三条事实**：不加 flag 时普通错误照常 exit=1；`--verify-diagnostics` 把没被 expected 覆盖的诊断标成 `unexpected error:`；`only-expected` 下没人期待的错误**直接放行，exit=0**。

第三档意味着：负向测试写成 `only-expected` 后，就从“非法输入必须报错”退化成“如果报错，必须是这一条”。**错误完全没报，测试照样绿。** 除非验证某个 note 必须出现，否则别用。

### 29.6.1 反直觉：不加 flag 会“报错”，但不会失败

```bash
cat > /tmp/mlir-lit/exp.mlir <<'MLIR'
func.func @ok(%a: i32) -> i32 {
  // expected-error@+1 {{boom}}
  return %a : i32
}
MLIR
$T/bin/mlir-opt /tmp/mlir-lit/exp.mlir ; echo "exit=$?"
```

```text
exp.mlir:2:6: error: expected error "boom" was not produced
  // expected-error@+1 {{boom}}
     ^~~~
（此处省略随后打印的 module 输出）
exit=0
```

消息出来了，**exit 却是 0**。原因在 `MlirOptMain.cpp:816-824`：verifier handler 无条件创建，所以它会打印；但只有 `config.shouldVerifyDiagnostics()` 为真时，`verify()` 的失败才被并进返回状态。**结果：忘记加 flag 的负向测试会“带着错误信息通过”**——CI 看退出码，不会因此变红。这就是每条负向测试的 RUN 行都必须写 `--verify-diagnostics` 的真正原因。

### 29.6.2 标准形状

```text
// RUN: mlir-opt %s -split-input-file -verify-diagnostics

func.func @extract_wrong_number_of_indices(%arg0: tensor<?xf32>) {
  // expected-error@+1 {{expected 1 index, got 0}}
  %0 = tensor.extract %arg0[] : tensor<?xf32>
  return
}
```

四个约定：`@+1` 指向下一条语句；`{{...}}` 里是**正则**（`(` `[` 要转义）；一个 split 块可以有多条 `expected-*`，每条都必须被产生一次；`expected-note`/`expected-remark` 同理。

## 29.7 平台与特性门控：`REQUIRES` / `UNSUPPORTED` / `XFAIL`

这三个词写在注释里，由 lit 在任何命令执行前读取：

```text
// REQUIRES: asserts                     → 没有 asserts feature 就 Unsupported
// UNSUPPORTED: system-windows           → 命中就 Unsupported
// XFAIL: target={{(s390x|sparc.*)-.*}}  → 命中则“预期失败”
```

本树真实统计：`grep -rl "UNSUPPORTED:" mlir/test | wc -l` = 30；`REQUIRES` = 56；`XFAIL` = 51。

### 29.7.1 语义差别比看起来大

- **REQUIRES**：feature **不在** → Unsupported（跳过）。
- **UNSUPPORTED**：feature **在** → Unsupported。两者是反义写法。
- **XFAIL**：命中 → 失败算 Expectedly Failed，**通过反而算 Unexpectedly Passed（失败）**。唯一“绿了会报警”的指令。

实测（`/tmp/mlir-lit` 里两个文件）：

```bash
printf '// REQUIRES: asserts\n// RUN: mlir-opt %%s\nfunc.func @nop() { return }\n' > /tmp/mlir-lit/unsup.mlir
printf '// UNSUPPORTED: asserts\n// RUN: mlir-opt %%s\nfunc.func @nop2() { return }\n' > /tmp/mlir-lit/unsup2.mlir
$T/bin/llvm-lit -sv /tmp/mlir-lit 2>&1 | tail -5
```

```text
Total Discovered Tests: 6
  Unsupported      : 1 (16.67%)     ← REQUIRES: asserts
  Passed           : 3 (50.00%)     ← UNSUPPORTED: asserts 照跑
  Expectedly Failed: 1 (16.67%)
  Failed           : 1 (16.67%)
```

（`←` 是旁注）注意 `/tmp/mlir-lit/lit.cfg.py` 并没有声明 `asserts`，所以 `REQUIRES` 命中“没有→跳过”，`UNSUPPORTED` 命中“没有→不跳过”。**feature 名拼错时两个指令都静默失效**：`REQUIRES: assert`（少 s）会让测试永远 Unsupported，你却在本地以为它在跑。

本构建真实可用的 feature（`lit.cfg.py:361-414`）：`host-byteorder-little-endian`、`asserts`、`expensive_checks`、`host-supports-jit`、`python-ge-311`、`<target>-native-target`。

### 29.7.2 XFAIL 的正确用法

```bash
head -5 mlir/test/IR/elements-attr-interface.mlir
```

```text
// RUN: mlir-opt %s -test-elements-attr-interface -verify-diagnostics

// Parsing external resources does not work on big-endian platforms currently
// XFAIL: target={{(s390x|sparc.*)-.*}}
```

这不是“测试坏了”，而是“在这些平台当前能力不足，我们**记录**已知失败，等能力补上后 XFAIL 自动变成 Unexpectedly Passed 提醒你删掉”。**XFAIL 必须带原因注释**，否则半年后没人知道它为什么在那儿。

## 29.8 更新脚本：让机器写草稿，让人写断言

MLIR 没有 `update_test_checks.py` 的等价物。三个脚本的分工：

| 脚本 | 归属 | 能做什么 | 不能做什么 |
|---|---|---|---|
| `mlir/utils/generate-test-checks.py` | MLIR | 从输出生成 `CHECK` 草稿 | 不理解语义，会生成冗余 CHECK |
| `llvm/utils/update_test_checks.py` | LLVM IR | 原地更新 `opt`/`llc` 测试 | **只认 LLVM IR 工具链** |
| `llvm/utils/update_any_test_checks.py` | 调度器 | 按文件类型分发 | 源码里没有 MLIR 分支（`grep -c -i mlir` = 0） |

### 29.8.1 `generate-test-checks.py` 实测

```bash
cd /tmp/mlir-lit
$T/bin/mlir-opt gen.mlir -canonicalize | python3 /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/utils/generate-test-checks.py --source gen.mlir
```

```text
// NOTE: Assertions have been autogenerated by utils/generate-test-checks.py
// It is *not* authoritative about what constitutes a good test.

// CHECK-LABEL:   func.func @f(
// CHECK-SAME:                 %[[ARG0:.*]]: i32,
// CHECK-SAME:                 %[[ARG1:.*]]: i32) -> i32 {
// CHECK:           %[[ADDI_0:.*]] = arith.addi %[[ARG0]], %[[ARG1]] : i32
// CHECK:           return %[[ADDI_0]] : i32
```

两个细节：脚本自己打印免责声明——**它生成“能过的断言”，不是“该断的言”**；它引用的 `https://mlir.llvm.org/getting_started/TestingGuide/` 在本树里没有对应文件（MLIR 的测试约定写在 `llvm/docs/TestingGuide.md`）。这类文档漂移本身可以提交补丁，但先确认没人已经在修（见 Ch 31）。

### 29.8.2 先 dry-run，再落盘

`generate-test-checks.py` 需要 `-i` 才原地改；`update_test_checks.py` 默认写回。维护者习惯：

```bash
python3 mlir/utils/generate-test-checks.py --source foo.mlir out.txt
diff -u foo.mlir out.txt | head -40     # 先看差异
```

原因：**更新脚本会把“实现变了”和“测试松了”混在一次 diff 里**。一次 canonicalize 顺序变化可能让 40 行 CHECK 全变，其中 39 行是重排、1 行是真行为变化。人工 review 这 40 行才是测试的价值。

## 29.9 什么时候必须加测试

| 改动 | 必须加 | 落在哪 |
|---|---|---|
| verifier / trait 校验 | 负向用例 | `mlir/test/Dialect/<X>/invalid.mlir` |
| parser / printer（`assemblyFormat`） | 正向 roundtrip | 同方言 `*.mlir`，可加 `--verify-roundtrip` |
| fold / canonicalization | 正向 CHECK | `canonicalize.mlir` |
| conversion pattern | 正向 + legality | `mlir/test/Conversion/<X>To<Y>/` |
| pass 行为 | 端到端 | `mlir/test/<Area>/`，固定 pipeline |
| 诊断措辞 | 负向用例**跟着改** | 已有 `invalid*.mlir` |
| 纯 NFC 重构 | 不新增 | 跑现有套件 |

最后一行值得展开：**NFC 补丁不应带新测试**。新测试引入“行为约定”，而 NFC 承诺“行为不变”；两者放一个 PR，reviewer 无法用“测试全绿”证明行为不变——他会要求拆。

## 29.10 源码落点

```bash
grep -n "config.suffixes\|use_default_substitutions" mlir/test/lit.cfg.py
grep -n "add_lit_testsuite\|add_lit_testsuites" mlir/test/CMakeLists.txt
grep -n "Best practices\|Precommit workflow" llvm/docs/TestingGuide.md
```

- `mlir/test/lit.cfg.py:24/26-39/160` — `ShTest()`、11 种后缀、`use_default_substitutions()`（`%s`/`%t` 来源）。
- `mlir/test/lit.cfg.py:302-319/361-414` — `mlir-opt` 的三种替换模式；feature 全集（`REQUIRES` 只能引用这里出现过的名字）。
- `mlir/test/CMakeLists.txt:282` — `add_lit_testsuite(check-mlir ...)`。
- `llvm/docs/TestingGuide.md:193/324` — RUN 行不是 shell；“Precommit workflow”要求先提交 baseline CHECK、再提交实现。

## 29.11 可跑实验

### 实验 1：从零建一个 lit 套件

```bash
mkdir -p /tmp/mlir-lit && cd /tmp/mlir-lit
cat > lit.cfg.py <<'PY'
import os
import lit.formats

config.name = "mlir-demo"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".mlir"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.environment["PATH"] = (
    "/home/ubuntu/llvm/builds/ct-instr-trunk/bin:" + os.environ["PATH"]
)
PY
$T/bin/llvm-lit -sv /tmp/mlir-lit
```

```text
Testing Time: 0.15s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

### 实验 2：让测试故意失败

```bash
cd /tmp/mlir-lit && sed -i 's/return %arg0/return %c0/' good.mlir
$T/bin/llvm-lit -sv /tmp/mlir-lit 2>&1 | sed -n 1,14p
```

```text
FAIL: mlir-demo :: good.mlir (1 of 1)
# | /tmp/mlir-lit/good.mlir:4:16: error: CHECK-NEXT: expected string not found in input
# | // CHECK-NEXT: return %c0
# | <stdin>:3:2: note: possible intended match here:  return %arg0 : i32
```

`error:` 那行是**你的断言**，`possible intended match` 是**实际输出**。对齐看，一眼能分出是断言写错还是行为变了。

### 实验 3：三档 `--verify-diagnostics`

```bash
for f in "" "--verify-diagnostics" "--verify-diagnostics=only-expected"; do
  $T/bin/mlir-opt $f /tmp/mlir-lit/unexpected.mlir >/dev/null 2>&1; printf "%-36s exit=%s\n" "${f:-<none>}" "$?"
done
```

```text
<none>                               exit=1
--verify-diagnostics                 exit=1
--verify-diagnostics=only-expected   exit=0
```

**exit=0 不代表输入合法**，只代表“没有 unexpected 诊断”。

### 实验 4：单目录跑与并行度

```bash
time $T/bin/llvm-lit -j1 -q $T/tools/mlir/test/Dialect/Arith
time $T/bin/llvm-lit    -q $T/tools/mlir/test/Dialect/Arith
```

```text
-- Testing: 31 tests, 1 workers --     real 0m6.749s
-- Testing: 31 tests, 31 workers --    real 0m1.550s
```

31 个测试、4.4 倍加速；MLIR 测试是**进程级并行**的，所以 `%t` 才那么重要。

### 实验 5：全套基线（本树，2026-11-08）

```bash
cd $T && ninja check-mlir 2>&1 | tail -7
```

```text
Testing Time: 56.36s

Total Discovered Tests: 4947
  Unsupported      :  643 (13.00%)
  Passed           : 4303 (86.98%)
  Expectedly Failed:    1 (0.02%)
```

`Discovered 4947` vs `Testing 4352` 的差值是 Unsupported 与部分不参与本次运行的组。**“0 failed”才是基线**，其余数字随构建配置变（这台机器没有 GPU runner，所以 643 个 unsupported）。

## 29.12 踩雷集锦

1. **测试绿了说明行为对。** → `only-expected` 下预期错误完全没产生也能 exit=0；`CHECK` 顺序写松了，接错操作数也能过。**测试的强度 = 它能否在 bug 存在时失败**。
2. **`expected-error` 不加 flag 也会生效。** → 它会打印 “expected error ... was not produced”，但 exit 仍是 0；失败并入 status 需要 `--verify-diagnostics`（`MlirOptMain.cpp:824`）。
3. **`UNSUPPORTED` 拼错 feature 会报错。** → 拼错 = 条件永不成立 = 测试永远跑。`REQUIRES: assert`（少 s）能让整个文件静默 Unsupported。
4. **跳过等于通过。** → Unsupported 不计失败，**换台没有该 feature 的机器，你的测试可能从没跑过**。
5. **`CHECK` 越少越稳 / 更新脚本可以无脑 `-i`。** → 只剩 `CHECK-LABEL` 时任何错误都测不出；脚本会把实现变化和测试松弛混在一次 diff 里。
7. **`-split-input-file` 只是省行数。** → 它改变语义：每块独立 context、独立 pipeline、独立编号；`--filter` 也只匹配测试名，不匹配内容。

## 29.13 本章重点整理

- 五个层次：suite 配置 → `lit.local.cfg` → `RUN` 行 → 工具输出 → FileCheck / DiagnosticVerifier；`RUN` 行不是 shell（`%s`/`%t` 是替换，`not` 反转退出码）。
- `CHECK` 家族六个指令的差别在**搜索起点与行约束**；`CHECK-LABEL` 分区，`CHECK-NOT` 区间断言。
- 负向测试用 `--verify-diagnostics`（All）；`only-expected` 让 unexpected 静默；不加 flag 时 `expected-*` 只打印不失败。
- `REQUIRES`/`UNSUPPORTED` 是反义门控，`XFAIL` 是“预期失败，绿了反而报警”；CHECK 草稿用 `generate-test-checks.py`（`update_test_checks.py` 只服务 LLVM IR）。
- 测试落点由改动类型决定：verifier → `invalid.mlir`；fold → `canonicalize.mlir`；conversion → `test/Conversion/`。

## 29.14 自我检核

- [ ] 能说出 `CHECK`、`CHECK-NEXT`、`CHECK-SAME`、`CHECK-LABEL` 的搜索起点差异吗？
- [ ] 能解释 `only-expected` 为什么危险，以及 `REQUIRES`/`UNSUPPORTED` 哪个是“默认跑、缺 feature 才跳”吗？
- [ ] 能给一个 verifier 改动写出完整负向测试（RUN 行 + `expected-error@+1`）吗？
- [ ] 知道本构建的 `asserts` feature 从 `lit.cfg.py` 哪一行来吗？
- [ ] 能解释 `-split-input-file` 除了省行数还改变了什么语义吗？
- [ ] 知道 MLIR 没有 `update_test_checks.py` 支持，该用哪个脚本替代吗？能一眼看出“绿了但没测到东西”的三种写法吗？

## 29.15 延伸阅读

1. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/TestingGuide.md` — 读 193–270（Writing new regression tests）与 340–375（Best practices）；学到 RUN 行不是 shell、测试要最小化；对应 29.4/29.9。
2. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lit.cfg.py` — 读 24–60、296–340、355–415；学到“一个套件的语义全在这 400 行里”；对应 29.3。
3. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/Diagnostics.h` — 读 680–712（`Level` 与 handler 声明）；学到三档语义的类型来源；对应 29.6。
4. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/utils/generate-test-checks.py` — 读 20–60（免责声明与用法）；学到“脚本只写草稿”；对应 29.8。
5. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp` — 读 160–200 与 790–835；学到为什么“不加 flag 也校验”；对应 29.6。

测试是“证明你知道自己在做什么”的唯一方式。下一章换个视角：**实现和测试都写好了，怎么把它包装成上游愿意读、愿意批的 PR**。

→ 下一章：[Ch 30 上游 PR：形状、评审与关单](./30-upstream-pr-review.md)
