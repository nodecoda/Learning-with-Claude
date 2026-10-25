# Ch 10 调试、诊断与测试

> **目标**：建立一条可重复的调试链路：先用诊断定位“谁在报错”，再用 IR dump 定位“哪个 pass 改坏了”，然后用 split-input-file 把问题隔离到单个用例，最后用 `mlir-reduce` 把输入缩到最小。学完本章，你应该能在一个陌生的 MLIR pipeline 里，独立完成“复现 → 定位 → 最小化 → 回归测试”。

## 为什么需要这个？

MLIR 的 pipeline 通常有几十个 pass，IR 又在不断变形。出问题时，最没用的做法是“把整个 pipeline 跑一遍，然后盯着最终 IR 猜”。有效的做法是把问题切成四层：

1. **诊断层**：错误是谁发的？位置在哪？有没有 note 指向 prior use？
2. **pass 层**：哪一步之后 IR 开始不合法？`--mlir-print-ir-before/after-all` 能回答。
3. **用例层**：一个文件里有多个函数/多个测试，哪个 chunk 触发？`--split-input-file` 能回答。
4. **最小化层**：能不能把输入缩到几十行？`mlir-reduce` 能回答。

这四层分别对应不同的工具。本章按这个顺序讲。

## 先建立直觉：三类工具，不要混用

| 工具 | 回答的问题 | 典型命令 |
|---|---|---|
| Diagnostics | 错误是什么、在哪、有没有关联位置 | `mlir-opt bad.mlir` |
| IR dump | 哪个 pass 前后 IR 变了 | `--mlir-print-ir-before-all` / `--mlir-print-ir-after-all` |
| 测试/缩减 | 怎么隔离、怎么最小化、怎么防回归 | `--split-input-file`、`mlir-reduce`、lit/FileCheck |

一个常见误区是拿 `printf` 当诊断。MLIR 的诊断是**结构化对象**：有 severity、location、notes，能被工具捕获、被测试匹配、被 IDE 显示。自己 `llvm::errs()` 打印一行，既没有位置，也不能被 `--verify-diagnostics` 检查。

## 核心概念一：Diagnostics、InFlightDiagnostic、note/remark

严重级别定义在 `mlir/include/mlir/IR/Diagnostics.h:41-46`：

```cpp
enum class DiagnosticSeverity {
  Note,
  Warning,
  Error,
  Remark,
};
```

`Note` 不能直接发射，只能通过 `attachNote` 挂在别的诊断上；`DiagnosticEngine::emit` 里有断言（`Diagnostics.h:473-476`）。`Diagnostic::attachNote` 在 `Diagnostics.h:261`，`InFlightDiagnostic::attachNote` 在 `Diagnostics.h:365-368`。

典型用法：

```cpp
op->emitError("operand type mismatch");
op->emitWarning("this may be slow");
emitRemark(op->getLoc()) << "candidate rejected";

InFlightDiagnostic diag = op->emitError("bad shape");
diag.attachNote(otherOp->getLoc()) << "defined here";
```

`InFlightDiagnostic` 是“正在构造的诊断”。你可以用 `<<` 追加内容；它离开作用域时析构，诊断才真正发射。这就是为什么 `emitError(...) << "x"` 能工作：临时对象活到整条表达式结束。

note 的打印顺序在 `Diagnostics.cpp:519-523`：主诊断先打，然后逐条打印 notes；如果 note 的位置和上一条不同，会额外打印源码行。你看到的“prior use here”就是这么来的。

`remark` 是低优先级诊断，默认也会打印，但可以被过滤器关掉。它适合“我建议你看看这里”，不适合错误路径。

## 核心概念二：IR dump 的开关

`mlir-opt --help` 里和 IR dump 相关的选项（实测存在）：

```text
--mlir-print-ir-before=<pass-arg>       - Print IR before specified passes
--mlir-print-ir-before-all              - Print IR before each pass
--mlir-print-ir-after=<pass-arg>        - Print IR after specified passes
--mlir-print-ir-after-all               - Print IR after each pass
--mlir-print-ir-after-change            - When printing the IR after a pass, only print if the IR changed
--mlir-print-ir-after-failure           - When printing the IR after a pass, only print if the pass failed
--mlir-print-ir-module-scope            - Always print the top-level operation
--mlir-print-ir-tree-dir=<string>       - Print file tree rooted at this directory
```

最常用的组合是 `--mlir-print-ir-after-all`。它会为每个 pass 打印一段：

```text
// -----// IR Dump After CanonicalizerPass: canonicalize{...} //----- //
module { ... }
```

注意两点：

- dump 走 **stderr**。PowerShell 里要 `2>&1` 才能进管道；`| Out-File` 不加 `2>&1` 会得到空文件。
- pass 名字后面的 `{...}` 是 pass 选项。`canonicalize{max-iterations=10 ...}` 这种信息对判断“为什么它没收敛”很有用。

如果输出太大，用 `--mlir-print-ir-after-change` 只看真正改变 IR 的 pass；或者用 `--mlir-print-ir-after=<pass-arg>` 只盯一个 pass。

## 核心概念三：split-input-file

`--split-input-file` 把一个文件按 `// -----` 切成多个 chunk，每个 chunk 独立 parse、verify、跑 pipeline、打印。默认 marker 在 `mlir/include/mlir/Support/ToolUtilities.h:36`：

```cpp
extern inline const char *const kDefaultSplitMarker = "// -----";
```

`mlir-opt` 的选项注册在 `mlir/lib/Tools/mlir-opt/MlirOptMain.cpp:150-161`；切分和重新拼接在 `ToolUtilities.cpp:22-30` 与 `97-101`。输出里的分隔符是 `// -----`，与默认输入 marker 相同；`--output-split-marker` 可以改它。

为什么测试都用它？因为一个文件里可以放多个互不影响的用例：前面的用例失败不会阻止后面的用例解析；每个用例的 `loc` 仍然指向原文件的行号，报错位置不会漂。

## 核心概念四：verify-each 的真实默认值

很多人以为要显式加 `--verify-each` 才会在每个 pass 后验证。**在 MLIR 23.1.4 的 `mlir-opt` 里，默认就是开的。** 看 `MlirOptMain.cpp:189-192`：

```cpp
static cl::opt<bool, /*ExternalStorage=*/true> verifyPasses(
    "verify-each",
    cl::desc("Run the verifier after each transformation pass"),
    cl::location(verifyPassesFlag), cl::init(true));
```

`cl::init(true)` 是关键。`--verify-each` 只是把已经是 true 的值再设一遍；真正有用的是 `--verify-each=false`（关掉）和知道它默认开着。

verifier 检查什么？SSA 支配关系、类型、trait 约束、region/block 的 terminator、op 自己的 `verify()`。它不检查“你的算法对不对”，但能抓住绝大多数结构错误。

注意：`--verify-each=false` 不会跳过**输入解析后的第一次 verify**。它只影响 pass 之间的验证。所以你不能用它来“让非法输入通过”。

## 核心概念五：mlir-reduce

`mlir-reduce` 的目标是“在保持 interesting 的前提下，把输入缩到最小”。它的模型是：

1. 你提供一个 tester 命令；
2. tester 对每个候选文件返回“是否 interesting”；
3. `mlir-reduce` 用 reduction-tree 算法删除/简化 IR，直到不能再小。

文档在 `mlir/docs/Tools/mlir-reduce.md:21-27`，tester 的语义在 `53-58`：**返回 1 表示 interesting，0 表示不 interesting**。源码 `mlir/lib/Reducer/Tester.cpp:69-81` 也是这个逻辑：`if (!result) return False; return True;`。

在 Windows 上有一个坑：`Tester` 用 `llvm::sys::ExecuteAndWait` 直接启动 tester（`Tester.cpp:70-72`），它不能直接执行 `.bat`。所以要用 `powershell.exe -File tester.ps1` 或者 `cmd.exe /c tester.bat` 包一层。本章的实测用的是 PowerShell 包装。

## 核心概念六：lit 与 FileCheck 的角色（guided）

上游 MLIR 的回归测试标准组合是 **lit + FileCheck**：

- `lit` 负责发现测试、解析 `RUN:` 行、准备 substitutions（`%s`、`%mlir-opt` 等）、比较退出码；
- `FileCheck` 负责在输出里按顺序匹配 `CHECK:`、`CHECK-NEXT:`、`CHECK-NOT:`、`CHECK-LABEL:`，并支持变量捕获。

一个典型测试：

```mlir
// RUN: mlir-opt %s --canonicalize | FileCheck %s

// CHECK-LABEL: func.func @fold
// CHECK: arith.constant 42 : i32
func.func @fold() -> i32 {
  %0 = arith.constant 20 : i32
  %1 = arith.constant 22 : i32
  %2 = arith.addi %0, %1 : i32
  return %2 : i32
}
```

**本机状态**：`lit 23.1.4` 存在（在一个 Python venv 里），但 `FileCheck` 不在 PATH，也没有 LLVM 测试套件的 lit 配置。所以这一段标 `guided`：命令和写法是上游标准，本章不伪造 FileCheck 输出。

如果你只想验证“诊断是否按预期发出”，有一个不依赖 FileCheck 的替代：`--verify-diagnostics`。它读取源码里的 `expected-error` / `expected-note` / `expected-remark` 注释，逐条比对。本章示例四会实测它。

## 示例一：错误定位（实测）

输入：

```mlir
func.func @bad(%a: i32, %b: i64) -> i32 {
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
```

命令：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\bad.mlir
```

实测输出：

```text
bad.mlir:2:23: error: use of value '%b' expects different type than prior uses: 'i32' vs 'i64'
  %0 = arith.addi %a, %b : i32
                      ^
bad.mlir:1:25: note: prior use here
func.func @bad(%a: i32, %b: i64) -> i32 {
                        ^
```

这条诊断的信息量很大：

- 主错误位置 `2:23` 指向 `%b` 的使用点；
- note 位置 `1:25` 指向 `%b` 的定义（函数参数）；
- 错误文本直接告诉你期望类型和实际类型。

`--mlir-print-op-on-diagnostic` 默认是 true（`MLIRContext.cpp:62-66`），所以 verifier 还会附一条 `see current operation` 的 note。把它关掉：

```powershell
& "$mlir\mlir-opt.exe" .\bad-return.mlir --mlir-print-op-on-diagnostic=false
```

实测输出会少掉 `note: see current operation` 这一行。这个开关在“诊断已经够清楚、不想看 op 文本”时有用。

## 示例二：split-input-file（实测）

输入 `split.mlir`：

```mlir
func.func @good(%a: i32) -> i32 {
  %0 = arith.addi %a, %a : i32
  return %0 : i32
}

// -----

func.func @bad(%a: i32, %b: i64) -> i32 {
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
```

命令：

```powershell
& "$mlir\mlir-opt.exe" .\split.mlir --split-input-file
```

实测输出：

```mlir
module {
  func.func @good(%arg0: i32) -> i32 {
    %0 = arith.addi %arg0, %arg0 : i32
    return %0 : i32
  }
}

// -----
split.mlir:9:23: error: use of value '%b' expects different type than prior uses: 'i32' vs 'i64'
  %0 = arith.addi %a, %b : i32
                      ^
split.mlir:8:25: note: prior use here
func.func @bad(%a: i32, %b: i64) -> i32 {
                        ^
```

关键观察：

- 第一个 chunk 正常打印，说明它被独立处理了；
- 第二个 chunk 的错误行号是 `9:23`，对应**原文件**的行号，不是 chunk 内行号；
- 输出里的 `// -----` 是工具重新插入的分隔符。

这就是为什么一个测试文件可以放很多用例：失败只影响当前 chunk 的退出码，定位仍然准确。

## 示例三：pass 边界 dump（实测）

命令：

```powershell
& "$mlir\mlir-opt.exe" .\rauw.mlir --canonicalize --cse --mlir-print-ir-after-all
```

实测输出（截取前两个 pass）：

```text
// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
module {
  func.func @f(%arg0: i32) -> i32 {
    %0 = arith.muli %arg0, %arg0 : i32
    return %0 : i32
  }
}
// -----// IR Dump After CSEPass: cse //----- //
module {
  func.func @f(%arg0: i32) -> i32 {
    %0 = arith.muli %arg0, %arg0 : i32
    return %0 : i32
  }
}
module {
  func.func @f(%arg0: i32) -> i32 {
    %0 = arith.muli %arg0, %arg0 : i32
    return %0 : i32
  }
}
```

最后一段没有 `IR Dump` 头，是 `mlir-opt` 的最终输出。前面的 dump 都在 stderr；如果你只重定向 stdout，会看不到它们。

`--mlir-print-ir-before-all` 同理，只是打印 pass 运行前的状态。两者一起用，可以精确看到“某个 pass 的输入和输出”。

## 示例四：verify-diagnostics（实测）

`--verify-diagnostics` 让你把期望的诊断写进源码，不需要 FileCheck。输入：

```mlir
func.func @bad(%a: i32) -> i64 {
  // expected-error @+1 {{type of return operand 0 ('i32') doesn't match function result type ('i64') in function @bad}}
  return %a : i32
}
```

命令：

```powershell
& "$mlir\mlir-opt.exe" .\verify-diag.mlir --verify-diagnostics
```

实测退出码为 0，没有输出。这说明诊断按预期发出了。

如果把期望文本改错：

```mlir
// expected-error @+1 {{this message is intentionally wrong}}
```

实测输出：

```text
verify-diag-wrong.mlir:3:3: error: unexpected error: type of return operand 0 ('i32') doesn't match function result type ('i64') in function @bad
  return %a : i32
  ^
verify-diag-wrong.mlir:2:6: error: expected error "this message is intentionally wrong" was not produced
  // expected-error @+1 {{this message is intentionally wrong}}
     ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
```

这个机制适合“诊断文本就是契约”的测试；它不能替代 FileCheck 对 IR 形状的检查，但能覆盖错误路径，而且在本机没有 FileCheck 的情况下仍然可用。

## 示例五：mlir-reduce（实测）

准备一个“输入合法、但某个 pass 会失败”的例子：`arith.addf` 的 tensor 形式没有 BufferizableOpInterface，`one-shot-bufferize` 会失败。输入里放三个函数，其中只有 `@tensor_addf` 触发问题。

tester 脚本 `tester-bufferize.ps1`：

```powershell
param([string]$file)
& "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin\mlir-opt.exe" $file `
    --one-shot-bufferize=bufferize-function-boundaries *> $null
if ($LASTEXITCODE -ne 0) { exit 1 }
exit 0
```

命令：

```powershell
& "$mlir\mlir-reduce.exe" .\reduce.mlir `
  "--reduction-tree=test=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe test-arg=-NoProfile test-arg=-File test-arg=$PWD\tester-bufferize.ps1"
```

实测 stdout：

```mlir
module {
  func.func @tensor_addf(%arg0: tensor<4xf32>, %arg1: tensor<4xf32>) -> tensor<4xf32> {
    %0 = arith.addf %arg0, %arg1 : tensor<4xf32>
    return %0 : tensor<4xf32>
  }
}
```

两个无关函数被删掉了，只留下触发失败的最小函数。stderr 里会看到一些 verifier 诊断，那是 `mlir-reduce` 在试错时淘汰的候选，不是最终结果。

注意 tester 的退出码语义：**非 0 = interesting**。这和大多数 Unix 工具“0 表示成功”的直觉相反，写 tester 时最容易搞反。

## 对比与取舍

| 需求 | 首选工具 | 备注 |
|---|---|---|
| 看错误文本和位置 | 直接跑 `mlir-opt` | 先读主诊断，再读 notes |
| 看哪个 pass 改坏了 IR | `--mlir-print-ir-after-all` | 输出大，配合 `--mlir-print-ir-after-change` |
| 隔离多个用例 | `--split-input-file` | 默认 marker `// -----` |
| 验证诊断文本 | `--verify-diagnostics` | 不需要 FileCheck |
| 验证 IR 形状 | lit + FileCheck | 本机缺 FileCheck，标 guided |
| 最小化失败输入 | `mlir-reduce` | tester 返回非 0 表示 interesting |
| 看 pass 选项 | `--mlir-print-ir-after-all` 的 header | pass 名后的 `{...}` |
| 看 op 的通用形式 | `--mlir-print-op-generic` | 去掉 custom 语法糖 |

## 踩雷集锦

1. **把 IR dump 重定向到文件却是空的。** dump 走 stderr；PowerShell 用 `2>&1`，不要只写 `>`。

2. **写成 `-print-after-all`。** 那是 LLVM `opt` 的拼写；MLIR 的前缀是 `mlir-`。写错不会报错，只会静默不打印。

3. **以为 `--verify-each` 默认关闭。** 23.1.4 的 `mlir-opt` 默认 true（`MlirOptMain.cpp:189-192`）；真正需要知道的是 `--verify-each=false` 会关掉它。

4. **用 `--verify-each=false` 让非法输入通过。** 它只跳过 pass 之间的验证，输入解析后的第一次 verify 仍然执行。

5. **split 后按 chunk 内行号找错误。** 报错行号是原文件行号；`// -----` 之前的行也要算进去。

6. **tester 退出码写反。** `mlir-reduce` 要“非 0 = interesting”；如果你按“0 = 成功”写，第一次就会被判定为 uninteresting。

7. **在 Windows 上直接让 `mlir-reduce` 跑 `.bat`。** `ExecuteAndWait` 不能直接执行批处理；用 `powershell.exe -File` 或 `cmd.exe /c` 包一层。

8. **在 Optimized build 上找 `-debug-only`。** 本机包没有 assertions/debug 日志；用 IR dump、diagnostics 和 `mlir-reduce` 代替。

## 进阶：再往深一层

- **crash reproducer**：`--mlir-pass-pipeline-crash-reproducer=<file>` 和 `--mlir-pass-pipeline-local-reproducer` 可以在 pass 崩溃/失败时生成最小 pipeline 的复现文件。比手工记 pass 顺序可靠。
- **按 pass 打印**：`--mlir-print-ir-before=<pass-arg>` / `--mlir-print-ir-after=<pass-arg>` 只盯一个 pass，适合大 pipeline。
- **IR tree**：`--mlir-print-ir-tree-dir=<dir>` 把每个 pass 的 IR 写成文件树，适合 diff。
- **`--mlir-print-op-generic`**：看 op 的真实结构（属性字典、operand/result 类型），排查 custom parser/printer 的问题。
- **`mlir-query`**：结构化查询 IR，比 grep 文本可靠；适合写“找出所有满足某模式的 op”的脚本。
- **`--verify-diagnostics` 的局限**：它只匹配诊断，不检查 IR；真正回归测试仍然推荐 lit + FileCheck。

## 动手练习

1. **复现错误定位**：把 `bad.mlir` 的 `%b` 改成 `i32`，确认错误消失；再把返回类型改成 `i64`，观察新的错误和 note。

2. **split 练习**：把三个用例放进一个文件，其中一个故意写错；用 `--split-input-file` 确认前两个仍然输出，第三个报错行号正确。

3. **dump 练习**：用 `--canonicalize --cse` 跑一个含重复 `addi` 的函数，分别加 `--mlir-print-ir-before-all` 和 `--mlir-print-ir-after-all`，指出哪个 pass 删掉了重复 op。

4. **verify-diagnostics**：给 `bad-return.mlir` 写一条正确的 `expected-error`，跑 `--verify-diagnostics` 确认退出码 0；再故意改错文本，确认它报“expected error ... was not produced”。

5. **mlir-reduce**：构造一个包含三个函数的合法输入，让其中一个函数触发 `one-shot-bufferize` 失败；用 tester 把它缩到最小，记录删掉了哪些函数。

6. **FileCheck（guided）**：如果你能拿到 FileCheck，给本章的 `--canonicalize` 例子写一个 `RUN:` + `CHECK:` 测试；本机没有 FileCheck，写清楚你缺什么。

## 本章重点整理

- 诊断是结构化对象：`emitError` / `emitWarning` / `emitRemark` / `attachNote`；note 不能直接发射。
- `--mlir-print-op-on-diagnostic` 默认 true；`=false` 可以去掉 `see current operation`。
- IR dump 走 stderr；`--mlir-print-ir-before-all` / `after-all` 是定位 pass 的主工具。
- `--split-input-file` 默认 marker 是 `// -----`；每个 chunk 独立处理，行号仍是原文件行号。
- `--verify-each` 在 23.1.4 默认 true；`--verify-each=false` 才会关。
- `mlir-reduce` 的 tester 返回非 0 表示 interesting；Windows 上要包一层 PowerShell/cmd。
- `--verify-diagnostics` 可以在没有 FileCheck 的情况下测诊断文本。
- lit + FileCheck 仍是上游标准；本机缺 FileCheck，相关段落标 guided。

## 自我检核

- [ ] 我能写出一个带 `attachNote` 的诊断，并解释 note 的位置
- [ ] 我知道 IR dump 走 stderr，并会用 `2>&1`
- [ ] 我能用 `--mlir-print-ir-after-all` 找到第一个改坏 IR 的 pass
- [ ] 我能解释 `--split-input-file` 的行号为什么是原文件行号
- [ ] 我知道 `--verify-each` 在 23.1.4 默认是开的
- [ ] 我能写出一个 `mlir-reduce` 的 tester，并说清退出码语义
- [ ] 我能用 `--verify-diagnostics` 验证一条 `expected-error`
- [ ] 我知道本机为什么不能跑 FileCheck，以及 lit 和 FileCheck 各自负责什么

## 延伸阅读

- **[MLIR Diagnostics 文档](https://mlir.llvm.org/docs/Diagnostics/)**
  - **读哪里**：诊断严重级别、`InFlightDiagnostic`、`attachNote`、诊断 handler。
  - **学什么**：如何写出可被工具消费的诊断。
  - **和本章的关联**：示例一的错误定位。

- **[mlir/include/mlir/IR/Diagnostics.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Diagnostics.h)**
  - **读哪里**：`DiagnosticSeverity`、`attachNote`、`emitRemark`。
  - **学什么**：诊断对象的 API 与生命周期。
  - **和本章的关联**：note/remark 的行为。

- **[mlir/docs/Tools/mlir-reduce.md](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/docs/Tools/mlir-reduce.md)**
  - **读哪里**：How to Use it、tester 脚本、reduction-tree。
  - **学什么**：interestingness 测试与缩减策略。
  - **和本章的关联**：示例五的最小化流程。

- **[MLIR Testing 文档](https://mlir.llvm.org/docs/Testing/)**
  - **读哪里**：lit、FileCheck、`--split-input-file`、`--verify-diagnostics`。
  - **学什么**：上游测试的组织方式。
  - **和本章的关联**：本章的测试链路与上游标准的对应关系。

- **[LLVM FileCheck 文档](https://llvm.org/docs/CommandGuide/FileCheck.html)**
  - **读哪里**：`CHECK`、`CHECK-NEXT`、`CHECK-NOT`、变量捕获。
  - **学什么**：如何写稳定的 IR 形状断言。
  - **和本章的关联**：本机缺 FileCheck，这部分标 guided。

---

下一章开始定义自己的方言：从 ODS 写第一个 Op，把本章的测试习惯带进新方言。

→ [练习 B：写一个 pass 与 pattern](./practice-b-pass-and-pattern.md)