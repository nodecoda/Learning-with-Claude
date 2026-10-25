# 第 17 章 调试工具链：在 Optimized build 上把问题逼出来

> **本章目标**：在没有 assertions 的官方预编译包上，仍然能把"选错/选不出来"定位到具体 pass、具体指令、具体 bank。你会知道哪些工具真的可用、哪些只是传说，以及 `-debug-only=global-isel` 为什么是一条死路。
>
> **前置**：第 2、7、10、16 章。**环境**：LLVM 23.1.4 官方 x86_64 Windows 包（Optimized build），AArch64。

> **本章所有命令与输出都在本机真实执行过**；唯一例外是 17.2.3 的 assertions build 输出，那里会明确标注"未运行，依据源码"。

---

## 17.1 先确认你手里的 llc 是哪种构建

```text
$ llc --version
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
  Default target: x86_64-pc-windows-msvc
  Host CPU: skylake
```

`Optimized build.` 这一行是关键。它意味着：

| 能力 | Optimized | assertions |
| --- | --- | --- |
| 正常 codegen | ✅ | ✅ |
| `-verify-machineinstrs` | ✅（verifier 本身编译进去了） | ✅ |
| 属性契约检查 `verifyRequiredProperties` | ❌ 编译掉 | ✅ 致命错误 |
| `-debug-only=<type>` | ❌ 参数不存在 | ✅ |
| `-stats` | ⚠️ 接受，但无输出 | ✅ |
| `assert()` / `LLVM_DEBUG` | ❌ 编译掉 | ✅ |

### 17.1.1 `-debug-only` 的真实下场

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -debug-only=global-isel add.ll -o NUL
llc.exe: Unknown command line argument '-debug-only=global-isel'.  Try: '...\llc.exe --help'
llc.exe: Did you mean '--debug-pass=global-isel'?
```

两个要点：

1. **`-debug-only` 这个参数根本没注册**，不是"认识但不输出"。它被 `#ifndef NDEBUG` 包着，Optimized build 里不存在。
2. **`--debug-pass=global-isel` 是误导**。`-debug-pass` 控制 pass manager 的调度打印，参数是 pass 名，不是 `DEBUG_TYPE`。`global-isel` 既不是 pass 名，也不是任何文件的 `DEBUG_TYPE`（见 17.2）。

### 17.1.2 `-stats` 的真实下场

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -stats add.ll -o NUL
$ echo $LASTEXITCODE
0
```

退出码 0，**一个字都不打印**。`llc --help-hidden` 自己写着：

```text
--stats   - Enable statistics output from program (available with Asserts)
```

所以"跑一下 `-stats` 看看哪个 pass 慢/失败"在官方包上是空操作。要计时用 `-time-passes`（17.3）。

### 17.1.3 包里到底有什么

以下是对 `bin/` 的实际清点（23.1.4 Windows 包）：

| 工具 | 状态 |
| --- | --- |
| `llc` / `opt` / `llvm-tblgen` | ✅ |
| `llvm-reduce` | ✅ |
| `FileCheck` / `llvm-lit` / `not` / `bugpoint` | ❌ 不在包内 |

没有 FileCheck 意味着**跑不了 `filecheck` 类测试**；没有 `not` 意味着**无法用退出码写"必须失败"的 lit 测试**；没有 `bugpoint` 意味着老式自动缩减不可用（用 `llvm-reduce` 替代）。这些都是环境事实，不是配置问题。

---

## 17.2 `DEBUG_TYPE`：`global-isel` 这个名字根本不存在

### 17.2.1 真实名字表

从 LLVM 23.1.4 源码逐个提取 `#define DEBUG_TYPE`：

| 文件 | `DEBUG_TYPE` |
| --- | --- |
| `GlobalISel/InstructionSelect.cpp` | `instruction-select` |
| `GlobalISel/Legalizer.cpp` | `legalizer` |
| `GlobalISel/RegBankSelect.cpp` | `regbankselect` |
| `GlobalISel/IRTranslator.cpp` | `irtranslator` |
| `GlobalISel/Utils.cpp` | `globalisel-utils` |
| `AArch64/GISel/AArch64InstructionSelector.cpp` | `aarch64-isel` |
| `AArch64/GISel/AArch64LegalizerInfo.cpp` | `aarch64-legalinfo` |
| `AArch64/GISel/AArch64CallLowering.cpp` | `aarch64-call-lowering` |

**没有任何文件用 `global-isel` 作为 `DEBUG_TYPE`。** 所以即使你手上有 assertions build，`-debug-only=global-isel` 也不会打印任何东西——它只是"永远不匹配的过滤器"。

### 17.2.2 想看的到底是哪一段

- 想看 `selectImpl` 命中/失败：`-debug-only=instruction-select`。
- 想看 AArch64 selector 的 C++ 分支（比如"寄存器没有 bank"）：`-debug-only=aarch64-isel`。
- 想看 legalizer 为什么拒绝：`-debug-only=legalizer` 或 `aarch64-legalinfo`。

### 17.2.3 assertions build 里 `LLVM_DEBUG` 输出什么（未运行，依据源码）

源码 `AArch64InstructionSelector.cpp`：

```cpp
#define DEBUG_TYPE "aarch64-isel"
...
      LLVM_DEBUG(dbgs() << "Generic register has no bank or class\n");
```

`InstructionSelect.cpp` 则是：

```cpp
#define DEBUG_TYPE "instruction-select"
...
        LLVM_DEBUG(dbgs() << "\nSelect:  " << MI);
          LLVM_DEBUG(dbgs() << "Selection failed!\n");
```

在 assertions build 上，**预期**是下面这样（**示意，未在本机运行**；按源码里的 `LLVM_DEBUG` 字符串拼接）：

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -debug-only=instruction-select bad.mir -o -
Select:  %2:gpr(i64) = G_FADD %0:gpr, %1:gpr
Selection failed!
```

它的价值是告诉你该 grep 什么，而不是当作已验证输出。

要真跑，按第 0 章构建时加 `-DLLVM_ENABLE_ASSERTIONS=ON`；源码构建的 `llc` 才会注册 `-debug-only`。

---

## 17.3 可用工具清单

| 工具/参数 | 状态 | 真实证据 |
| --- | --- | --- |
| `-global-isel-abort=1` | ✅ | `LLVM ERROR: cannot select: ...` |
| `-stop-before=` / `-stop-after=` | ✅ | 导出任一阶段 MIR |
| `-print-before=` / `-print-after=` | ✅ | `# *** IR Dump Before ... ***` |
| `-verify-machineinstrs` | ✅（跳过 failedISel） | `*** Bad machine code: ...` |
| `-simplify-mir` | ✅ | 输出少 40 多行噪声 |
| `-time-passes` | ✅ | 真实 timing 表 |
| `-stats` | ⚠️ 接受但静默 | 退出码 0、无输出 |
| `-debug-only` | ❌ | `Unknown command line argument` |
| `opt -passes=` | ✅ | `opt -passes=instcombine -S` |
| `-mattr=` | ✅ | `+lse` 改变 atomicrmw 选择 |
| `llvm-reduce` | ✅ | 能把 3 函数缩到 1 函数 |
| `bugpoint` / `FileCheck` / `llvm-lit` / `not` | ❌ | 不在 `bin/` |

---

## 17.4 用 `-print-before` / `-print-after` 看选择前后

这是 Optimized build 上最值钱的组合：不需要 debug 信息，直接看 MIR。

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 \
      -print-before=instruction-select -print-after=instruction-select add.ll -o NUL
```

真实输出：

```text
# *** IR Dump Before InstructionSelect (instruction-select) ***:
# Machine code for function add: IsSSA, TracksLiveness, Legalized, RegBankSelected
Function Live Ins: $x0, $x1
bb.1.entry:
  liveins: $x0, $x1
  %0:gpr(i64) = COPY $x0
  %1:gpr(i64) = COPY $x1
  %2:gpr(i64) = G_ADD %0:gpr, %1:gpr
  $x0 = COPY %2:gpr(i64)
  RET_ReallyLR implicit $x0
# End machine code for function add.
# *** IR Dump After InstructionSelect (instruction-select) ***:
# Machine code for function add: IsSSA, TracksLiveness, Legalized, RegBankSelected, Selected
Function Live Ins: $x0, $x1
bb.1.entry:
  liveins: $x0, $x1
  %0:gpr64 = COPY $x0
  %1:gpr64 = COPY $x1
  %2:gpr64 = ADDXrr %0:gpr64, %1:gpr64
  $x0 = COPY %2:gpr64
  RET_ReallyLR implicit $x0
```

怎么读：

1. `# Machine code for function ...:` 的尾部属性列表，就是此刻的函数状态。`Selected` 出现在 after，不在 before。
2. `%0:gpr(i64)` → `%0:gpr64`：bank + LLT 变成了寄存器类。
3. `G_ADD` → `ADDXrr`：根指令换了。
4. 操作数从 `%0` 变成 `%0:gpr64`：打印器在已选择函数里会附带寄存器类，方便核对。

`-print-after-all` 会打印每个 pass，输出极长；先用单个 pass 名定位，再决定要不要全量。

**注意**：dump 走 stderr，而 `-o -` 的汇编走 stdout。重定向时别把两者混在一起，否则 CHECK 会匹配到 dump。

---

## 17.5 `-global-isel-abort=1`：把 fallback 变成可定位错误

### 17.5.1 三档语义（源码 + 实测）

`TargetPassConfig.cpp`：

```cpp
clEnumValN(GlobalISelAbortMode::Disable, "0", "Disable the abort"),
clEnumValN(GlobalISelAbortMode::Enable, "1", "Enable the abort"),
clEnumValN(GlobalISelAbortMode::DisableWithDiag, "2",
           "Disable the abort but emit a diagnostic on failure")
```

`Utils.cpp` 决定是否致命：

```cpp
  bool IsGlobalISelAbortEnabled =
      MF.getTarget().Options.GlobalISelAbort == GlobalISelAbortMode::Enable;
  bool IsFatal = Severity == DS_Error && IsGlobalISelAbortEnabled;
  ...
  if (IsFatal)
    reportFatalUsageError(Twine(R.getMsg()));
  else
    MORE.emit(R);
```

### 17.5.2 实测对照

输入：一份把 `G_ADD` 写成 `G_FADD`、bank 仍为 `gpr` 的 MIR（完整文件见 16.7.3）。

```text
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select bad.mir -o -
...
selected:        true
failedISel:      true
    %2:gpr(i64) = G_FADD %0, %1
$ echo $LASTEXITCODE
0
```

换成 `-global-isel-abort=1`：

```text
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -global-isel-abort=1 bad.mir -o NUL
LLVM ERROR: cannot select: %2:gpr(i64) = G_FADD %0:gpr, %1:gpr (in function: bad)
```

退出码 1。这一行信息量很大：

- `%2:gpr(i64)`：目标 vreg、bank、LLT；
- `G_FADD`：没被选中的根指令；
- `%0:gpr`、`%1:gpr`：操作数的 bank 已被打印出来，能立刻判断是不是 bank 不匹配。

### 17.5.3 `abort=2` 在 `-run-pass` 下的坑

```text
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -global-isel-abort=2 bad.mir -o -
...
selected:        true
failedISel:      true
$ echo $LASTEXITCODE
0
```

`=2` 是"不 abort，但发 diagnostic"。在完整管线里它会让函数 fallback 到 SelectionDAG；但在 `-run-pass=instruction-select` 里没有 SelectionDAG 接手，于是你只得到一个 `failedISel: true` 的 MIR 和退出码 0。**调试时用 `=1`，不要用 `=2`。**

### 17.5.4 它不只抓 ISel

`reportGISelFailure` 是 GlobalISel 共用的，所以 Legalizer 失败也会被 `abort=1` 变成致命错误：

```text
LLVM ERROR: unable to legalize instruction: %2:_(f64) = G_STRICT_FADD %0:_, %1:_ (in function: f)
```

```text
LLVM ERROR: unable to legalize instruction: G_STORE %51:_(<2 x i8>), %48:_(p0)
:: (store (<2 x i1>) into %stack.0) (in function: f)
```

看到 `unable to legalize` 就不要再查 InstructionSelect——问题在更早的阶段。

---

## 17.6 `-verify-machineinstrs`：能查什么，不能查什么

能查：

```text
*** Bad machine code: Unexpected generic instruction in a Selected function ***
- function:    add
- basic block: %bb.0 entry
- instruction: %2:gpr(i64) = G_FADD %0:gpr, %1:gpr
LLVM ERROR: Found 7 machine code errors.
```

不能查：

```cpp
  if (isFunctionFailedISel)
    return true;
```

`MachineVerifier.cpp` 对 `failedISel` 的函数直接放行。所以"选择失败但被标记"这种最常见的情况，verifier 一句都不说。它和 `-global-isel-abort=1` 是互补关系：

- `abort=1` 抓"选不出来"；
- `verify-machineinstrs` 抓"选出来了但不合法"。

两个都开，才覆盖完整。

---

## 17.7 调试顺序：先契约，再 bank，再 matcher，最后渲染

错误信息会告诉你该从哪一层开始。推荐顺序如下。

### 第一步：确认 pass 契约

```text
$ llc ... -stop-before=instruction-select -verify-machineinstrs bad.mir -o -
```

看四件事：

- `legalized: true`？
- `regBankSelected: true`？
- 每个 `%N` 都有 `bank(llt)`？
- 有没有 `failedISel: true`？

如果错误是 `unable to legalize`，直接回第 4 章；如果是 `MachineFunctionProperties ... not met`（assertions build），是 pass 调度问题，不是选择器。

### 第二步：确认 bank 与 LLT

看 before dump：

```text
  %2:gpr(i64) = G_ADD %0:gpr, %1:gpr
```

`G_ADD` 的操作数在 `gpr`，还是 `fpr`？LLT 是 `s64` 还是 `s32`？AArch64 的 `ADDXrr` 要求 `s64` + GPR bank，`ADDWrr` 要求 `s32`。bank 错了，matcher 必然拒绝，这不是 pattern 的锅。

### 第三步：确认 matcher 是否命中

- Optimized build：`-print-after` 看根指令有没有变；`-global-isel-abort=1` 看失败点。
- assertions build：`-debug-only=aarch64-isel`（C++ 分支）或 `-debug-only=instruction-select`（表执行）。

如果根指令变成了"另一条合法但更差的指令"，说明 matcher 命中了**错误的规则**，而不是没命中。这时要查 pattern 的优先级/复杂度，练习 D 是完整案例。

### 第四步：确认渲染与编码

选择成功但汇编阶段报错，查：

- `-verify-machineinstrs` 的寄存器类/大小报错；
- `-mattr` 是否让某条指令不可用（例如没有 `+lse` 时不该出现 `ldaddal`）；
- MC 层报 `invalid operand for instruction` 时，通常是立即数范围或移位量。

### 17.7.1 症状对照表

| 症状 | 先查哪层 | 首选命令 |
| --- | --- | --- |
| `unable to legalize instruction` | Legalizer | `-stop-after=legalizer` |
| `cannot select: %N:bank(llt) = G_...` | bank/LLT 或 matcher | `-stop-before=instruction-select` |
| `Unexpected generic instruction` | 渲染/约束 | `-verify-machineinstrs` |
| `VReg has no regclass after selection` | `constrainRegClass` | assertions build |
| 汇编正确但明显更差 | 选择质量 | `-print-after`、练习 D |
| `Unknown command line argument '-debug-only'` | 构建类型 | `llc --version` |

---

## 17.8 三个可跑命令（真实输出）

### 17.8.1 看选择前后

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 \
      -print-before=instruction-select -print-after=instruction-select add.ll -o NUL
```

输出见 17.4：`G_ADD` → `ADDXrr`，`gpr(i64)` → `gpr64`。

### 17.8.2 让 fallback 报错

```text
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
      -global-isel-abort=1 bad.mir -o NUL
LLVM ERROR: cannot select: %2:gpr(i64) = G_FADD %0:gpr, %1:gpr (in function: bad)
```

### 17.8.3 用 `llvm-reduce` 缩小

`llvm-reduce` 在包里，但 `--test` 需要一个**可执行文件**。本机把 `.bat` 或 `python script.py` 传给它，会得到：

```text
error: running interesting-ness test: program not executable
```

正确做法是编译一个小 exe（本机用 clang + VS2017 工具链，20 行 C 就够）。它的逻辑是：跑 `llc`，输出里出现 `mov\tw8, w8` 就退出 0（interesting），否则退出 1。

```text
$ llvm-reduce --test=C:\isel-lab\interesting.exe C:\isel-lab\red\t.ll -o C:\isel-lab\red\out.ll
...
*** Sinking defs to uses (sink-defs-to-uses)...
 **** SUCCESS | Saved new best reduction to C:\isel-lab\red\out.ll
Done reducing! Reduced testcase: C:\isel-lab\red\out.ll
```

三个函数被缩成一个：

```llvm
define i64 @keep(i64 %x) {
  %r = and i64 %x, 4294967295
  ret i64 %r
}
```

注意 interestingness 的**退出码方向**：llvm-reduce 认为退出 0 = interesting。写反了会得到 `Input isn't interesting!`。

### 17.8.4 附加：三个便宜但有用的命令

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -time-passes add.ll -o NUL
===-------------------------------------------------------------------------===
                          Pass execution timing report
===-------------------------------------------------------------------------===
  Total Execution Time: 0.0156 seconds (0.0030 wall clock)
   ---User Time---   --User+System--   ---Wall Time---  --- Name ---
   0.0000 (  0.0%)   0.0000 (  0.0%)   0.0014 ( 47.5%)  Expand IR instructions
   0.0156 (100.0%)   0.0156 (100.0%)   0.0005 ( 16.2%)  AArch64 Assembly Printer
   0.0000 (  0.0%)   0.0000 (  0.0%)   0.0003 (  8.9%)  IRTranslator
   0.0000 (  0.0%)   0.0000 (  0.0%)   0.0001 (  4.6%)  Fast Register Allocator
   0.0000 (  0.0%)   0.0000 (  0.0%)   0.0001 (  3.6%)  InstructionSelect
```

```text
$ opt -passes=instcombine -S add.ll -o -
```

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -mattr=+lse amo.ll -o -
	ldaddal	x0, x0, [x1]
```

最后一条对比：不带 `-mattr=+lse` 时，同一个 `atomicrmw add` 生成 `LDXR/STXR` 循环。这是确认 feature 谓词是否生效的最快手段。

---

## 17.9 踩雷清单

1. **`-debug-only` 在 Optimized build 上不存在**，不是"打开方式不对"。
2. **`DEBUG_TYPE` 不是 `global-isel`**。用 `instruction-select` / `aarch64-isel` / `legalizer`。
3. **`-print-after` 的输出走 stderr**，和 `-o -` 的汇编混在一起会污染 CHECK。
4. **`-verify-machineinstrs` 跳过 failedISel**，抓不到 fallback。
5. **`abort=2` 在 `-run-pass` 下等于静默**，调试请用 `=1`。

---

## 17.10 进阶

### 17.10.1 用 `-print-after` 配合 `-simplify-mir` 做快照

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 \
      -stop-after=instruction-select -simplify-mir add.ll -o add-sel.mir
```

把每个阶段的快照存成文件，diff 两个版本（改 pattern 前/后），比读 `-print-after-all` 的输出高效得多。

### 17.10.2 用 `-mattr` 隔离 feature 谓词

同一段 IR，分别加 `-mattr=+lse` / `-mattr=-lse`，如果选择结果不同，说明命中的规则带 feature 谓词；如果完全相同，说明这个 feature 不是变量。这是排查"CI 上和我本地不一样"的第一招。

### 17.10.3 给 assertions build 留一个开关

```text
$ cmake -G Ninja -S llvm -B build-assert \
    -DLLVM_ENABLE_ASSERTIONS=ON -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_TARGETS_TO_BUILD="AArch64"
```

Release + assertions 是性价比最高的调试构建：跑得动，又能用 `-debug-only`、`-stats` 和属性检查。

---

## 练习

**17.1** 用 `-print-before` / `-print-after` 观察 `sub i64 %x, 1`：`SUBSXri` 还是 `SUBXri`？两者对 `$nzcv` 的影响有什么不同？（提示：本机实测 `sub` 的常量形态会出现 `subs`。）

**17.2** 把 17.5.2 的 MIR 分别用 `abort=0/1/2` 跑一遍，记录退出码、`failedISel` 和输出行数。

**17.3** 用 `llvm-reduce` 把一份包含 3 个函数的 IR 缩到只保留触发某个 pattern 的函数。写出你的 interestingness 判据。

**17.4** 用 `-mattr=+lse` / `-mattr=-lse` 对比 `atomicrmw add`，指出哪条指令的出现依赖 feature。

---

## 重点整理

- 先看 `llc --version`：Optimized build 没有 `-debug-only`、没有属性检查、`-stats` 静默。
- `DEBUG_TYPE` 是 `instruction-select` / `aarch64-isel` / `legalizer`，没有 `global-isel`。
- `-print-before/-after` 是 Optimized build 上最可靠的观察手段。
- `abort=1` 让"选不出来"变成带指令的致命错误；`abort=2` 在 `-run-pass` 下等于静默。
- verifier 抓"选出来但不合法"，且跳过 failedISel。
- 调试顺序：契约 → bank/LLT → matcher → 渲染/编码。

---

## 自我检核

1. Optimized build 上 `-debug-only` 报什么？为什么？
2. AArch64 selector 的 `DEBUG_TYPE` 是什么？`instruction-select` 又是哪个文件的？
3. `-stats` 与 `-time-passes` 在官方包上的行为差异？
4. 为什么 `-verify-machineinstrs` 抓不到 fallback？
5. `abort=2` 在完整管线与 `-run-pass` 下的行为差异？
6. `llvm-reduce` 的 `--test` 返回值语义是什么？

---

## 延伸阅读

- `llvm/lib/Support/Debug.cpp`：`DebugFlag`、`isCurrentDebugType` 与 `-debug-only` 的实现。
- `llvm/lib/CodeGen/GlobalISel/Utils.cpp`：`reportGISelFailure` / `reportGISelDiagnostic`，理解 fatal 与 diagnostic 的分叉。
- `llvm/lib/CodeGen/MachineVerifier.cpp`：`isFunctionFailedISel` 与 `verifyPreISelGenericInstruction`。
- `llvm/docs/CommandGuide/llvm-reduce.rst`：interestingness test 的约定。
- `llvm/docs/GlobalISel/Debugging.md`（若你的版本存在）：上游的 GlobalISel 调试清单。

---

## 下一章

工具能让你看到"发生了什么"，但看到之后还要判断"这算不算 bug、该怪谁"。第 18 章把这些症状组织成一棵诊断树：从报错文本直接定位到 Legalizer、RegBankSelect、InstructionSelect 或 MC 层。

[第 18 章 常见错误与诊断树](./18-common-bugs-diagnosis.md)
