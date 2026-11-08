# Ch 13 — 调试、复现与最小化

> **目标**：把“我这里崩了”变成一条可交给别人的证据链：`诊断 → IR dump → crash reproducer → mlir-reduce`。重点是知道每一层各自能证明什么、代价是什么，以及为什么 reduce 出来的用例必须先过 verifier。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，assertions ON（`report_fatal_error` 与 backtrace 可用）。
>
> **前置**：Ch 07（诊断与 verifier）、Ch 09（`-mlir-print-ir-*`）。

---

## 13.1 为什么维护者要读调试链路

一个 MLIR 崩溃报告通常长这样：

```text
Assertion `isa<X>(op)' failed.
...
0.  Program arguments: mlir-opt huge.mlir --pass-pipeline='...'
```

这种报告**没有价值**：没人知道是哪条 patch 引入的，也没人能从中间某处重建状态。可用的报告需要四件东西：

1. **出错点**：哪条诊断、哪个 op、哪次 pass 运行。
2. **出错时的 IR**：不是最初的输入，而是“最后一个成功 pass 之后”的 IR。
3. **完整的 pipeline**：包括选项、是否 verify、是否多线程。
4. **最小输入**：几百行以内，能被人读完。

MLIR 恰好为这四件事各给了一个工具，而且顺序是强制的：先有诊断，再有 dump，再有 reproducer，最后才 reduce。反过来做（先 reduce）会把错误的最小复现条件丢掉。

## 13.2 先建立直觉：四层证据阶梯

```text
  ① 诊断层  ──  mlir-opt 的 error/remark + 位置
     │            --mlir-print-stacktrace-on-diagnostic   ← 加 C++ 调用栈
     │            --mlir-print-op-on-diagnostic           ← 附上当前 op
     ▼
  ② dump 层 ──  --mlir-print-ir-before/after/after-all
     │            --mlir-print-ir-after-change / -after-failure
     │            --mlir-print-ir-tree-dir=<dir>          ← 每个 pass 一个文件
     ▼
  ③ 复现层 ──  --mlir-pass-pipeline-crash-reproducer=<file>
     │            --mlir-pass-pipeline-local-reproducer   ← 缩到“最小 pipeline”
     │            --run-reproducer <file>                 ← 原样重放
     ▼
  ④ 最小化 ──  mlir-reduce <file> -reduction-tree='test=<script>'
                  └─ 每步都过 verifier + 你的 interestingness 脚本
```

代价递增、信息递减：

| 层 | 代价 | 产出 | 不能回答什么 |
|---|---|---|---|
| 诊断 | 无 | 位置 + 消息 | IR 长什么样 |
| dump | 输出体积 | 每个 pass 前后的 IR | 完整 pipeline 参数 |
| reproducer | 一次运行 | 输入 + `mlir_reproducer` 元数据 | 为什么崩 |
| reduce | 分钟级、反复跑脚本 | 最小输入 | 根因（只保留“还崩”这个事实） |

## 13.3 诊断层：让错误自己带上上下文

`--mlir-print-stacktrace-on-diagnostic` 在每条诊断后附 C++ 调用栈（开关定义在 `MLIRContext.cpp:68-69`）。实测：

```bash
printf 'func.func @bad() {\n  %%0 = arith.addi %%x, %%x : i32\n  return\n}\n' > /tmp/mw/bad.mlir
$T/bin/mlir-opt /tmp/mw/bad.mlir --mlir-print-stacktrace-on-diagnostic 2>&1 | head -20
```

```text
/tmp/mw/bad.mlir:2:19: error: use of undeclared SSA value name
  %0 = arith.addi %x, %x : i32
                  ^
/tmp/mw/bad.mlir:2:19: note: diagnostic emitted with trace:
 #0 0x00007f6301fc8a52 llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Unix/Signals.inc:931:22
 #1 0x00007f63021858de llvm::raw_string_ostream::~raw_string_ostream() /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/include/llvm/Support/raw_ostream.h:664:16
 #2 0x00007f63021858de emitDiag(mlir::Location, mlir::DiagnosticSeverity, llvm::Twine const&) /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Diagnostics.cpp:373:5
 #3 0x00007f6302185a5a mlir::emitError(mlir::Location, llvm::Twine const&) /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Diagnostics.cpp:385:1
 #4 0x00007f62f70357f5 mlir::Token::is(mlir::Token::Kind) const /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/AsmParser/Token.h:38:34
 #5 0x00007f62f70357f5 mlir::detail::Parser::emitError(llvm::SMLoc, llvm::Twine const&) /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/AsmParser/Parser.cpp:246:20
```

两个使用要点：

- **它只对“有诊断”的失败有用**。segfault/assert 不会走这条路径，要走 crash reproducer。
- **栈里大量行号相同是正常的**（内联、模板实例化）。真正的信息是函数名：看到 `Parser::emitError` 就知道是解析期，而不是 pass 期。

`--mlir-print-op-on-diagnostic` 更轻量：不打栈，只把当前 op 打成 note。前面 11.5 里 `-test-cfg-loop-info` 的调度错误就带出了整个 module（那是 `see current operation` 的效果）。

## 13.4 dump 层：`before` / `after` / `change` / `failure`

四个开关的语义差别（`PassManagerOptions.cpp:36-62`）：

| 开关 | 触发条件 | 何时用 |
|---|---|---|
| `--mlir-print-ir-before=<pass>` | 指定 pass 之前 | 已知嫌疑 pass |
| `--mlir-print-ir-after-all` | 每个 pass 之后 | 不知道谁改的 |
| `--mlir-print-ir-after-change` | 只有 IR 变了才打 | 缩小输出量（Ch 10 实验 4） |
| `--mlir-print-ir-after-failure` | 只有 pass 失败才打 | 崩/失败现场定位 |

`-after-failure` 的实测（配合必然失败的 `-test-pass-failure`）：

```bash
$T/bin/mlir-opt /tmp/mw/crash.mlir \
  -pass-pipeline='builtin.module(func.func(test-pass-failure))' --mlir-print-ir-after-failure
```

```text
// -----// IR Dump After {anonymous}::TestFailurePass Failed: test-pass-failure{gen-diagnostics=false} //----- //
func.func @f(%arg0: i32) -> i32 {
  %0 = arith.addi %arg0, %arg0 : i32
  return %0 : i32
}
```

头部多了 `Failed` 一词，这是它和 `-after-all` 的唯一区别——但正是这一点让你不必滚动几万行。

### 13.4.1 输出太大时：`--mlir-print-ir-tree-dir`

当 pipeline 有几十个 pass、每个 pass 打整个 module，terminal 一定滚没。`--mlir-print-ir-tree-dir=<dir>` 把每次 dump 写成 `<dir>` 下的独立文件（开关在 `PassManagerOptions.cpp:61-65`，需与 `-print-ir-*` 联用）。维护上的用法是“跑一次、事后 diff 两个目录”：

```bash
mkdir -p /tmp/mw/ir-a /tmp/mw/ir-b
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(cse))' \
  --mlir-print-ir-after-all --mlir-print-ir-tree-dir=/tmp/mw/ir-a --mlir-disable-threading >/dev/null
```

我没有在本机核对文件命名规则（**未实测**产物布局），但开关语义确定：它只影响 dump 的落盘方式，不改变 dump 的触发条件。要对比两次运行，用同名目录 + `diff -ru`。

一个实用惯例：把 `-tree-dir` 和 `-after-change` 组合，目录里就只剩“真正改了 IR 的那些 pass”，比逐条读 terminal 快一个数量级。

## 13.5 复现层：crash reproducer 的文件格式

打开开关：

```bash
$T/bin/mlir-opt /tmp/mw/crash.mlir \
  -pass-pipeline='builtin.module(func.func(test-pass-crash))' \
  --mlir-pass-pipeline-crash-reproducer=/tmp/mw/repro.mlir
```

```text
/tmp/mw/crash.mlir:0:0: error: Failures have been detected while processing an MLIR pass pipeline
/tmp/mw/crash.mlir:0:0: note: Pipeline failed while executing [`{anonymous}::TestCrashRecoveryPass` on 'func.func' operation: @f]: reproducer generated at `/tmp/mw/repro.mlir`
```

生成的 `/tmp/mw/repro.mlir`：

```text
module {
  func.func @f(%arg0: i32) -> i32 {
    %0 = arith.addi %arg0, %arg0 : i32
    return %0 : i32
  }
}

{-#
  external_resources: {
    mlir_reproducer: {
      pipeline: "builtin.module(func.func(test-pass-crash))",
      disable_threading: false,
      verify_each: true
    }
  }
#-}
```

三个字段缺一不可：

- `pipeline`：完整 textual pipeline，包含 pass 选项。
- `disable_threading`：崩在多线程下时通常需要置 `true` 才能稳定复现。
- `verify_each`：对应 `PassManager::enableVerifier`（`PassCrashRecovery.cpp:529-530`）。

重放：

```bash
$T/bin/mlir-opt --run-reproducer /tmp/mw/repro.mlir
```

```text
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce the bug.
Stack dump:
0.	Program arguments: /home/ubuntu/llvm/builds/ct-instr-trunk/bin/mlir-opt --run-reproducer /tmp/mw/repro.mlir
 #0 0x00007f01fb39da52 llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Unix/Signals.inc:931:22
 #1 0x00007f01fb39aadc llvm::sys::RunSignalHandlers() /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Signals.cpp:108:20
 #2 0x00007f01fb39b1b5 SignalHandler(int, siginfo_t*, void*) /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Unix/Signals.inc:474:14
 #3 0x00007f01faca7520 (/lib/x86_64-linux-gnu/libc.so.6+0x42520)
 #4 0x00007f01facfb9bc __pthread_kill_implementation ./nptl/pthread_kill.c:44:76
```

这就是可以贴进 issue 的东西：**一条命令 + 一个文件 + 一个栈**。

### `local-reproducer`：把 pipeline 缩到“还能崩”的最小集合

```bash
$T/bin/mlir-opt /tmp/mw/crash.mlir \
  -pass-pipeline='builtin.module(func.func(canonicalize,test-pass-crash))' \
  --mlir-pass-pipeline-crash-reproducer=/tmp/mw/repro2.mlir \
  --mlir-pass-pipeline-local-reproducer
```

```text
<unknown>:0: error: Local crash reproduction may not be used without disabling mutli-threading first.
```

（原文如此，`mutli` 是上游拼写。）加 `--mlir-disable-threading` 后：

```text
      pipeline: "builtin.module(func.func(test-pass-crash))",
      disable_threading: true,
      verify_each: true
```

`canonicalize` 被剥掉了——`local-reproducer` 会为每个 pass 单独维护恢复点（`PassCrashRecovery.cpp:184-192`、`:339`），只保留触发崩溃的那一段。维护含义：**报 issue 时优先用 local reproducer**，reviewer 能直接看出是哪个 pass 的问题。

## 13.6 最小化：`mlir-reduce` 的实际契约

`mlir-reduce` 不是“删到不崩”，而是“删到**你的脚本还认为有趣**”：

```text
mlir-reduce 输入的 module
      │
      ▼  ReductionTreePass::runOnOperation            [ReductionTreePass.cpp:268]
  逐层遍历 region（嵌套 region 也遍历，`:283-294`）
      │
      ▼  reduceOp → findOptimal                       [:82 / :297]
  对每个 region：
      ├─ 把所有 op 编号成 [0..N)
      ├─ 按 reduction tree 的 ranges 生成候选
      ├─ 对不在 range 的 op：applyPatterns（改写）或 erase
      ▼  Tester::isInteresting(candidate)              [Tester.cpp:27]
   ├─ 先 verify(candidate)，失败 → 不感兴趣（`:32`）
   ├─ 写临时文件，跑你的脚本（`:40-54`）
   └─ 脚本退出码非 0 → 感兴趣
```

三个必须知道的契约：

1. **退出码：非 0 = 感兴趣**。这与 `grep` 的直觉相反（`grep` 找到返回 0）。官方文档写得很清楚（`mlir/docs/Tools/mlir-reduce.md:58-59`、`:75`），测试脚本要写 `if grep ...; then exit 1; else exit 0; fi`。
2. **每个候选必须先过 verifier**。`Tester.cpp:32` 的 `verify(topOp)` 失败会直接判不感兴趣，并把校验错误打到 stderr（所以 reduce 过程会刷一堆 `error: null operand found`，那是被拒的候选，不是最终结果）。
3. **只实现了 SinglePath**。`reduceOp` 的 `switch` 只有 `case TraversalMode::SinglePath`，其它值走 `default: emitError("unsupported traversal mode detected")`（`ReductionTreePass.cpp:297-305`）。传 `traversal-mode=1` 会失败。

### 13.6.1 三类缩减策略

`mlir-reduce` 的缩减不是“删 op”一种（`mlir-reduce.md:107-160`）：

| 策略 | 手段 | 风险 | 本树支持 |
|---|---|---|---|
| Operation elimination | 直接 erase 不在 range 的 op | 可能产生非法 IR，被 verifier 拒绝 | ✅ `applyPatterns(..., eraseOpNotInRange=true)` |
| Rewrite into simpler forms | 用 dialect 提供的 reduction pattern 改写（如未知 rank → 常量 rank） | 改变语义细节，可能丢失触发条件 | ✅ `DialectReductionPatternInterface` |
| Built-in optimization passes | `-opt-reduction-pass{opt-pass=...}`，只有更小且仍感兴趣才替换 | 依赖优化 pass 本身的行为 | ✅ `OptReductionPass.cpp:44-90` |

维护含义：**erase 是最强的**，也最容易被 verifier 挡住；如果 reduce 卡住不动，先看是不是所有候选都非法（stderr 全是 `null operand found` 之类），再考虑补一个 dialect reduction pattern。

## 13.7 源码落点

| 位置 | 内容 | 维护相关性 |
|---|---|---|
| `mlir/lib/IR/MLIRContext.cpp:68-69` | `mlir-print-stacktrace-on-diagnostic` | 诊断附栈开关 |
| `mlir/lib/Pass/PassManagerOptions.cpp:24/:28` | crash / local reproducer 开关 | CLI 入口 |
| `mlir/lib/Pass/PassManagerOptions.cpp:47/:50/:57` | after-change / after-failure / module-scope | dump 语义 |
| `mlir/lib/Pass/PassCrashRecovery.cpp:413` | `PassManager::runWithCrashRecovery` | 恢复点主循环 |
| `mlir/lib/Pass/PassCrashRecovery.cpp:61/:94/:136` | `pipelineElements` 与 pipeline 字符串 | 写进 `mlir_reproducer.pipeline` |
| `mlir/lib/Pass/PassCrashRecovery.cpp:184-192/:339` | `localReproducer` 分支 | “最小 pipeline”从哪来 |
| `mlir/lib/Pass/PassCrashRecovery.cpp:503-530` | 读取 `disable_threading`/`verify_each` | `--run-reproducer` 的重放逻辑 |
| `mlir/lib/Reducer/ReductionTreePass.cpp:38` | `applyPatterns` | 改写 or 删除的判定 |
| `mlir/lib/Reducer/ReductionTreePass.cpp:82` | `findOptimal` | 树遍历 + 每节点测试 |
| `mlir/lib/Reducer/ReductionTreePass.cpp:297` | `reduceOp` 的 traversal switch | 只支持 SinglePath |
| `mlir/lib/Reducer/Tester.cpp:27/:32/:60-80` | 有趣性判定 | verify 先行 + 退出码语义 |
| `mlir/include/mlir/Reducer/Passes.td:20/:27/:37` | `test`/`traversal-mode`/`opt-pass` 选项 | reduce 的 CLI 面 |

核对命令：

```bash
grep -n 'runWithCrashRecovery\|localReproducer\|pipelineElements' mlir/lib/Pass/PassCrashRecovery.cpp | head
grep -n 'findOptimal\|TraversalMode::SinglePath\|unsupported traversal mode' mlir/lib/Reducer/ReductionTreePass.cpp
grep -n 'verify(topOp)\|if (!result)' mlir/lib/Reducer/Tester.cpp
```

## 13.8 可跑实验

### 实验 1：失败 pass 的现场（实测）

```bash
cat > /tmp/mw/crash.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  %0 = arith.addi %a, %a : i32
  return %0 : i32
}
EOF
$T/bin/mlir-opt /tmp/mw/crash.mlir \
  -pass-pipeline='builtin.module(func.func(test-pass-failure))' --mlir-print-ir-after-failure
```

```text
// -----// IR Dump After {anonymous}::TestFailurePass Failed: test-pass-failure{gen-diagnostics=false} //----- //
func.func @f(%arg0: i32) -> i32 {
  %0 = arith.addi %arg0, %arg0 : i32
  return %0 : i32
}
```

### 实验 2：不加 reproducer 时你只有一份烂报告（实测）

```bash
$T/bin/mlir-opt /tmp/mw/crash.mlir -pass-pipeline='builtin.module(func.func(test-pass-crash))'
```

```text
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce the bug.
Stack dump:
0.	Program arguments: /home/ubuntu/llvm/builds/ct-instr-trunk/bin/mlir-opt /tmp/mw/crash.mlir -pass-pipeline=builtin.module(func.func(test-pass-crash))
 #0 0x00007fd8e0d92a52 llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Unix/Signals.inc:931:22
 #1 0x00007fd8e0d8fadc llvm::sys::RunSignalHandlers() /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Signals.cpp:108:20
 #2 0x00007fd8e0d901b5 SignalHandler(int, siginfo_t*, void*) /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/Unix/Signals.inc:474:14
```

退出码实测：

```bash
$T/bin/mlir-opt /tmp/mw/crash.mlir -pass-pipeline='builtin.module(func.func(test-pass-crash))' >/dev/null 2>&1; echo $?
```

```text
134
```

`134 = 128 + 6 (SIGABRT)`。报告里只有 argv 和栈，**没有 IR、没有选项默认值、没有 `verify_each`**。这就是为什么必须在复现命令里一开始就带 `--mlir-pass-pipeline-crash-reproducer`：崩了之后再想加，状态已经没了。

### 实验 3：crash reproducer 与重放（实测）

命令与输出见 13.5；`cat /tmp/mw/repro.mlir` 得到的 `mlir_reproducer` 块就是最终产物。`--run-reproducer` 会原样重放并打出栈。

### 实验 4：local reproducer 需要关线程（实测）

```text
<unknown>:0: error: Local crash reproduction may not be used without disabling mutli-threading first.
```

关掉之后 pipeline 从 `func.func(canonicalize,test-pass-crash)` 缩成 `func.func(test-pass-crash)`（见 13.5 的产物）。

### 实验 5：用官方文档的样例跑 reduce（实测）

```bash
export PATH="$T/bin:$PATH"        # 关键：tester 脚本里调用了 mlir-opt
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-reduce mlir/test/mlir-reduce/reduction-tree/doc-example.mlir \
  -reduction-tree='traversal-mode=0 test=mlir/test/mlir-reduce/script/grep-select.sh' 2>/dev/null
```

```text
module {
  func.func @func2(%arg0: i1) -> f32 {
    %cst = arith.constant 2.200000e+00 : f32
    %cst_0 = arith.constant 5.300000e+00 : f32
    %0 = arith.addf %cst, %cst_0 : f32
    %1 = arith.select %arg0, %0, %cst : f32
    %2 = arith.addf %cst, %1 : f32
    return %2 : f32
  }
}
```

输入有 2 个函数、11 条 op，输出只剩 1 个函数、5 条 op。`@func1`（无关函数）和整条 i32 链都被删掉了。

### 实验 6：PATH 没配时的假“不感兴趣”（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-reduce mlir/test/mlir-reduce/reduction-tree/doc-example.mlir \
  -reduction-tree='traversal-mode=0 test=mlir/test/mlir-reduce/script/grep-select.sh' 2>&1 | head -2
```

```text
loc("mlir/test/mlir-reduce/reduction-tree/doc-example.mlir":0:0): error: uninterested module will not be reduced
loc("mlir/test/mlir-reduce/reduction-tree/doc-example.mlir":0:0): error: uninterested module will not be reduced
```

原因不是 reduce 坏了，而是 `grep-select.sh` 里第一行是 `mlir-opt $1`，而 `mlir-opt` 不在 `PATH` 里。文档在 `mlir-reduce.md:75` 专门提醒过这一点。**“uninterested” 永远先怀疑 tester 脚本本身。**

### 实验 7：自己写 tester，16 行缩到 7 行（实测）

```bash
cat > /tmp/mw/sel.sh <<EOF
#!/bin/sh
# mlir-reduce 约定：0 = 不感兴趣，非 0 = 感兴趣
$T/bin/mlir-opt "\$1" 2>/dev/null | grep -q "arith.subi"
if [ \$? -eq 0 ]; then exit 1; else exit 0; fi
EOF
chmod +x /tmp/mw/sel.sh
cat > /tmp/mw/ch13-demo.mlir <<'EOF'
func.func @noise(%a: f32, %b: f32) -> f32 {
  %0 = arith.addf %a, %b : f32
  %1 = arith.mulf %0, %b : f32
  %2 = arith.subf %1, %a : f32
  return %2 : f32
}
func.func @keep(%a: i32, %b: i32) -> i32 {
  %c1 = arith.constant 1 : i32
  %c2 = arith.constant 2 : i32
  %unused = arith.muli %a, %c2 : i32
  %0 = arith.addi %a, %c1 : i32
  %1 = arith.subi %0, %c1 : i32
  %2 = arith.muli %1, %b : i32
  %3 = arith.subi %2, %c2 : i32
  return %3 : i32
}
EOF
$T/bin/mlir-reduce /tmp/mw/ch13-demo.mlir -reduction-tree='traversal-mode=0 test=/tmp/mw/sel.sh' 2>/dev/null
```

```text
module {
  func.func @keep(%arg0: i32, %arg1: i32) -> i32 {
    %c2_i32 = arith.constant 2 : i32
    %0 = arith.muli %arg0, %arg1 : i32
    %1 = arith.subi %0, %c2_i32 : i32
    return %1 : i32
  }
}
```

16 行输入 → 7 行输出：`@noise` 整块消失，`@keep` 里与 `subi` 无关的常量折叠链被删。注意 tester 里用了 **绝对路径**，避免了实验 5 的坑。

### 实验 8：未实测项

- **`--mlir-print-ir-tree-dir` 的产物布局**：需要先建目录并跑一个多 pass pipeline；本机未实测其文件命名规则，**未实测**，只给开关位置（`PassManagerOptions.cpp:61-65`）。
- **`-opt-reduction-pass` 配 `opt-pass` 的缩减效果**：它的语义是“跑一个优化 pipeline，只在结果更小且仍感兴趣时替换”（`OptReductionPass.cpp:44-90`）；我未实测具体 pipeline 组合，**未实测**。

## 13.9 踩雷集锦

1. **错误直觉：reduce 之后的用例一定能复现原崩溃。** → 正确认识：reduce 只保留“脚本仍感兴趣 + verifier 通过”；如果 crash 依赖被删掉的上下文，最小用例可能不再崩。脚本要盯**错误信息**，不是“能不能跑”。
2. **错误直觉：tester 脚本返回 0 表示“找到了”。** → 正确认识：`mlir-reduce` 约定**非 0 = 感兴趣**（`Tester.cpp:78-82`）。
3. **错误直觉：`uninterested module will not be reduced` 说明输入有问题。** → 正确认识：先怀疑 tester 脚本（PATH、绝对路径、`2>&1` 重定向）。
4. **错误直觉：reduce 过程中刷屏的 verifier error 是 bug。** → 正确认识：那是被 `Tester::isInteresting` 拒绝的候选（`Tester.cpp:32`）。
5. **错误直觉：`--mlir-pass-pipeline-local-reproducer` 可以直接用。** → 正确认识：必须同时 `--mlir-disable-threading`，否则报 `Local crash reproduction may not be used without disabling mutli-threading first`。
6. **错误直觉：`--mlir-print-ir-after-all` 就够了。** → 正确认识：多线程 + 大量 pass 会把输出冲掉；`-after-change`、`-after-failure`、`-tree-dir` 是三个减噪器。
7. **错误直觉：`traversal-mode` 有多个算法可选。** → 正确认识：本树只实现 `SinglePath`，其它值报 `unsupported traversal mode detected`。
8. **错误直觉：assertions 构建和 release 构建的崩溃可以互相对照。** → 正确认识：assertion 失败在 release 下可能变成静默错误结果；报 issue 必须写清构建类型（本课程环境是 assertions ON）。

## 13.10 本章重点整理

- 四层证据：诊断 → IR dump → reproducer → reduce；顺序不能反。
- `--mlir-print-stacktrace-on-diagnostic` 只对“有诊断”的失败有效；segfault 要靠 reproducer。
- reproducer 文件 = IR + `mlir_reproducer{pipeline, disable_threading, verify_each}`，`--run-reproducer` 可原样重放。
- `local-reproducer` 剥离无关 pass，但要求 `--mlir-disable-threading`。
- `mlir-reduce` 的有趣性 = verifier 通过 + 脚本返回非 0；只实现 SinglePath。
- reduce 刷屏的 verifier error 是被拒候选，不是最终产物。
- 最小用例的脚本要用绝对路径；`PATH` 缺失会伪装成“输入不感兴趣”。
- 不加 reproducer 的崩溃只剩 argv 与栈（exit 134），事后无法补；复现命令必须一开始就带开关。

## 13.11 自我检核

- [ ] 能说出四层证据各自能回答什么问题、不能回答什么
- [ ] 能用一个失败 pass 生成 reproducer，并用 `--run-reproducer` 重放
- [ ] 能解释 `mlir_reproducer` 三个字段的作用
- [ ] 能解释 `local-reproducer` 为什么要关线程，并复现那条错误
- [ ] 能写出符合 `mlir-reduce` 约定的 tester 脚本（含绝对路径与 `2>&1`）
- [ ] 能解释 reduce 过程中 verifier error 的来源
- [ ] 能说明为什么 `traversal-mode` 只有 0 可用
- [ ] 能在报告里区分 assertions ON/OFF 的崩溃行为

## 13.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Tools/mlir-reduce.md`**
  - 读哪里：`## How to Use it`（21 行）、`### Write the script for testing interestingness`（53 行）、`## Available reduction strategies`（107 行）
  - 能学到什么：tester 契约与三种缩减策略；和本章关联：13.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Pass/PassCrashRecovery.cpp`**
  - 读哪里：`60-200`（pipeline 字符串 + 恢复点）、`400-540`（runWithCrashRecovery 与重放）
  - 能学到什么：reproducer 的确切生成/消费逻辑；和本章关联：13.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Reducer/ReductionTreePass.cpp`**
  - 读哪里：`38-110`（applyPatterns/findOptimal）、`268-310`（遍历与 traversal switch）
  - 能学到什么：reduction tree 的真实边界；和本章关联：13.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Reducer/Tester.cpp`**
  - 读哪里：全文 80 行
  - 能学到什么：verify 先行 + 退出码语义；和本章关联：13.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Pass/PassManagerOptions.cpp`**
  - 读哪里：`20-70`（crash 与 print-ir 全部选项）
  - 能学到什么：每个开关的真实名字与默认值；和本章关联：13.3/13.4

调试是“把一个已知失败压小”。下一章换个方向：从**定义**出发——ODS 与 `mlir-tblgen` 生成的 op 类、parser、verifier、builder 是怎么来的，以及为什么改 `.td` 的行为常常超出预期。

→ 下一章：[Ch 14 ODS 与生成代码](./14-ods-and-generated-code.md)
