# 第 16 章 用 MIR 与 lit 把 InstructionSelect 固定下来

> **本章目标**：写出能长期留在上游的 GlobalISel 测试：知道该测 IR 还是测 MIR、RUN 行每个参数在防什么、怎么检查虚拟寄存器的寄存器类约束、以及 `update_llc_test_checks.py` 什么时候会帮你、什么时候会害你。
>
> **前置**：第 2、6、7 章与练习 A。**环境**：LLVM 23.1.4，AArch64，官方 x86_64 Windows 预编译包（Optimized build）。

> **环境说明（必读）**：官方 Windows 包**不含 `FileCheck.exe`、`llvm-lit`、`not`、`bugpoint`**。本章的 `llc` 命令与输出都是本机真实跑出来的；`CHECK` 行由 `update_llc_test_checks.py` 对着这些真实输出生成，或逐行手工对齐。但本章**不会伪造 `PASS: ...` 这样的 lit 结果行**——本机没有 FileCheck，跑不了。要跑 lit，请用第 0 章的源码构建（`ninja check-llvm-codegen-aarch64`）或 Linux 官方包。

---

## 16.1 为什么不能只测最终汇编

GlobalISel 的 bug 有三种面貌：

- **选错指令**：`G_ADD` 被选成 `ADDXri` 而不是 `ADDXrr`，汇编仍然正确。
- **静默 fallback**：InstructionSelect 返回 false，函数交给 SelectionDAG，汇编完全正确。
- **契约破坏**：指令选出来了，但虚拟寄存器没有寄存器类，到汇编阶段才炸。

只看 `llc foo.ll -o -` 的汇编，前两种都抓不到。所以测试分两层：

- **行为层**（IR → asm）：确认整体输出没变，防集成回归。
- **单元层**（MIR → MIR）：只跑 `instruction-select`，直接断言 `G_*` 变成了哪条目标指令。

两层都要 `-global-isel-abort=1`，否则 fallback 会让测试"通过得毫无意义"。

---

## 16.2 最小 RUN 行模板

上游 `llvm/test/CodeGen/AArch64/GlobalISel/` 里最常见的 MIR 测试头：

```text
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 -run-pass=instruction-select %s -o - | FileCheck %s
```

逐参数拆开：

| 参数 | 作用 | 去掉会怎样 |
| --- | --- | --- |
| `-mtriple=aarch64-linux-gnu` | 固定 target，不依赖 runner 的 host | 在 x86 host 上会跑错后端 |
| `-global-isel` | 走 GlobalISel 管线 | AArch64 `-O0` 虽默认开，显式写死更稳 |
| `-global-isel-abort=1` | 选择失败 → 致命错误 | 失败会静默交给 SelectionDAG |
| `-O0` | 固定优化级别 | 不同 `-O` 会改变进入 ISel 的 MIR 形状 |
| `-run-pass=instruction-select` | 只跑这一个 pass | 跑完整管线，测不到单点 |
| `%s` / `-o -` | 输入文件 / 结果打到 stdout | 默认写 `.s`，管道拿不到 |
| `FileCheck %s` | 用文件里的 `CHECK` 行比对 | — |

两个容易忽略的细节：

1. **`-run-pass` 只接受 `.mir`**。真实报错：

```text
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select add.ll -o NUL
llc.exe: error: run-pass is for .mir file only.
```

2. **注释前缀不同**：`.ll` 用 `; RUN:`，`.mir` 用 `# RUN:`。在 `.mir` 顶部写 `; RUN:` 会破坏 YAML 解析：

```text
llc.exe: error: YAML:1:1: missing required key 'name'
; RUN: llc -mtriple=aarch64-linux-gnu ...
^
```

测整条 IR → asm 时不需要 `-run-pass`：

```text
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 < %s | FileCheck %s
```

`< %s` 把文件当 IR 输入；MIR 测试则把 `%s` 作为位置参数，因为解析器要从文件里读 `--- |` 段。

**为什么要显式 `-O0`**：`-O1` 以上会插入 pre/post-legalizer combiner 与 post-select optimize，同一段 IR 进入 InstructionSelect 的 MIR 完全不同。测试要么固定 `-O0`（测选择器本身），要么固定更高优化级别（测整条流水线），不要混着写。

---

## 16.3 MIR 文件到底长什么样

一份 `-stop-after=regbankselect -simplify-mir` 导出的 MIR：

```yaml
--- |
  define i64 @add(i64 %a, i64 %b) {
  entry:
    %r = add i64 %a, %b
    ret i64 %r
  }
...
---
name:            add
alignment:       4
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
isSSA:           true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
liveins:
  - { reg: '$x0' }
  - { reg: '$x1' }
body:             |
  bb.1.entry:
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
...
```


| 字段 | 含义 | 写错的后果 |
| --- | --- | --- |
| `--- \|` … `...` | 内嵌 LLVM IR，提供签名、DataLayout、triple | 报 `expected top-level entity` 或找不到函数 |
| `name` | 机器函数名，必须与内嵌 IR 的 `define` 一致 | `isn't defined in the provided LLVM IR` |
| `legalized` / `regBankSelected` | Legalizer / RegBankSelect 已完成 | assertions build 属性检查失败 |
| `selected` | InstructionSelect 已跑过 | 影响 MachineVerifier 是否检查 generic opcode |
| `tracksRegLiveness` | 带 liveness | 依赖 liveness 的 pass 读不到 |
| `registers` | 每个 vreg 的 bank 或寄存器类 | `Cannot determine class/bank of virtual register N` |
| `liveins` | 函数入口的物理寄存器 | `Using an undefined physical register` |
| `body` | 基本块与机器指令 | — |

### 16.3.1 bank 与寄存器类在文本上的区别

- 选择前：`%0:gpr(i64)` —— `gpr` 是 **bank**，括号里是 LLT。
- 选择后：`%0:gpr64` —— `gpr64` 是 **寄存器类**，括号消失。

`%N` 始终是**虚拟寄存器**。选择完成不等于寄存器分配完成；`$x0` 只出现在 live-in、返回值和显式 COPY 上。写 CHECK 时不要把 `%0` 和 `$x0` 混为一谈。



---

## 16.4 CHECK 家族

| 指令 | 语义 | 典型用途 |
| --- | --- | --- |
| `CHECK` | 从上次匹配位置往后找，允许隔行 | 断言"出现了某条指令" |
| `CHECK-NEXT` | 必须紧接下一行 | 断言指令相邻、顺序固定 |
| `CHECK-LABEL` | 匹配并重置扫描范围 | 多函数文件里分函数 |
| `CHECK-NOT` | 前后两个匹配之间不得出现 | 断言"没有 fallback、没有残留 G_*" |
| `CHECK-DAG` | 一组无序匹配 | 断言两条互不依赖的指令都在 |

### 16.4.1 真实汇编上的例子

`add.ll` 的实际输出（节选，保留原始 tab）：

```asm
add:                                    // @add
	.cfi_startproc
// %bb.0:                               // %entry
	add	x0, x0, x1
	ret
```

对应 CHECK：

```text
; CHECK-LABEL: add:
; CHECK:       // %bb.0:
; CHECK-NEXT:    add x0, x0, x1
; CHECK-NEXT:    ret
```

`CHECK-NEXT` 后写的是一条空格，汇编里是 tab；FileCheck 把连续空白视为等价。但 `CHECK-NEXT` 对行数严格，中间多一条注释就失败。

### 16.4.2 真实 MIR 上的例子

`add` 在 `instruction-select` 之后：

```yaml
body:             |
  bb.0.entry:
    liveins: $x0, $x1

    %0:gpr64 = COPY $x0
    %1:gpr64 = COPY $x1
    %2:gpr64 = ADDXrr %0, %1
    $x0 = COPY %2
    RET_ReallyLR implicit $x0
```

检查"虚拟寄存器被约束到 GPR64"：

```text
# CHECK: %[[ARG0:[0-9]+]]:gpr64 = COPY $x0
# CHECK: %[[ARG1:[0-9]+]]:gpr64 = COPY $x1
# CHECK: %[[SUM:[0-9]+]]:gpr64 = ADDXrr %[[ARG0]], %[[ARG1]]
# CHECK: $x0 = COPY %[[SUM]]
```

这里同时断言：寄存器类是 `gpr64`（不是 `gpr`/`gpr32`）、两个源操作数是同一条 `ADDXrr` 的操作数、返回值走 `$x0`。

**不要写死 `%2`**。vreg 编号会随输入 MIR 和 pass 插入的临时寄存器变化，上游测试几乎都用 `[[NAME:...]]` 捕获。

### 16.4.3 CHECK-NOT 与 CHECK-DAG 的陷阱

```text
# CHECK: selected:        true
# CHECK-NOT: G_ADD
# CHECK: ADDXrr
```

`CHECK-NOT` 只覆盖"上一个匹配到下一个匹配之间"。放文件末尾且后面没有 `CHECK` 时，它检查到文件结束——通常正是你想要的，但要意识到范围。想在整份输出里禁止 `G_`，更稳的是 `--implicit-check-not="G_"`。

`CHECK-DAG` 适合语义无序的指令，比如两条 live-in COPY（`# CHECK-DAG: %[[A:[0-9]+]]:gpr64 = COPY $x0` 与 `$x1` 那条）。但不要用它绕开你其实想断言的顺序：若先后决定正确性（`ANDS` 必须先设 `$nzcv` 再 `CSEL`），用 `CHECK-NEXT`。

---

## 16.5 四种测试层次

| 层次 | 输入 | 命令 | 能抓什么 | 抓不到什么 |
| --- | --- | --- | --- | --- |
| IR → asm | `.ll` | `llc ... < %s \| FileCheck` | 整条流水线、ABI、prologue/epilogue | 中间阶段是谁出的错 |
| MIR → MIR | `.mir` | `-run-pass=instruction-select` | 单条 generic 指令的选择结果、寄存器类 | pass 顺序、combiners |
| 阶段快照 | `.ll`/`.mir` | `-stop-before=` / `-stop-after=` | 任意阶段的 MIR 形状 | 不会自动断言 |
| 契约检查 | `.mir` | `-verify-machineinstrs` | 属性、寄存器类、generic 残留 | 指令选得对不对 |

选择原则：

- 新增/修改 ISel pattern → **MIR → MIR**，最快最稳，失败信息最直接。
- 改 Legalizer / RegBankSelect，或怀疑问题在上游 → **阶段快照**，先看 MIR 再决定写哪层测试。
- 回归用户可见行为、ABI、调用约定 → **IR → asm**。
- 任何"选择成功"的断言都加 **`-verify-machineinstrs`**。

把 `-stop-after=regbankselect` 换成 `instruction-select`，就得到 16.4.2 的 MIR。

### 16.5.1 `-verify-machineinstrs` 会跳过 failedISel

源码 `MachineVerifier.cpp`：

```cpp
  if (isFunctionFailedISel)
    return true;
```

所以"选择失败、函数被标 failedISel"时 verifier 什么都不报。抓 fallback 只能靠 `-global-isel-abort=1` 或直接检查 `failedISel: true`。

---

## 16.6 `update_llc_test_checks.py`：好用，但会掩盖回归

脚本在源码树 `llvm/utils/update_llc_test_checks.py`，预编译包里没有：

```text
$ python update_llc_test_checks.py --llc-binary /path/to/llc test.ll
```

本机实测。输入：

```llvm
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 < %s | FileCheck %s

define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}

define i64 @add4096(i64 %x) {
entry:
  %r = add i64 %x, 4096
  ret i64 %r
}
```

脚本真实改写后的文件：

```llvm
; NOTE: Assertions have been autogenerated by utils/update_llc_test_checks.py UTC_ARGS: --version 6
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 < %s | FileCheck %s

define i64 @add(i64 %a, i64 %b) {
; CHECK-LABEL: add:
; CHECK:       // %bb.0: // %entry
; CHECK-NEXT:    add x0, x0, x1
; CHECK-NEXT:    ret
entry:
  %r = add i64 %a, %b
  ret i64 %r
}

define i64 @add4096(i64 %x) {
; CHECK-LABEL: add4096:
; CHECK:       // %bb.0: // %entry
; CHECK-NEXT:    add x0, x0, #1, lsl #12 // =4096
; CHECK-NEXT:    ret
entry:
  %r = add i64 %x, 4096
  ret i64 %r
}
```

常用参数：`--llc-binary`（必给）、`--function`（只更新一个函数）、`--filter`/`--filter-out`、`-v`。

### 16.6.1 四个风险

1. **整段重写**。它会删掉你手写的窄断言，换成"每条指令都 CHECK"的宽断言。
2. **把错误固化成"预期"**。练习 D 的三条 `mov` 就是例子：直接跑脚本，错误的 codegen 会变成官方断言，之后没人再怀疑。
3. **跨配置不稳定**。不同 build、不同 `-mattr` 的注释/空白可能不同，全量 CHECK 很容易碎。
4. **对 `-run-pass` 的 MIR 支持不可靠**。本机实测对合法 MIR 得到：

```text
WARNING: Couldn't match any function. Possibly the wrong target triple has been provided
```

只在文件末尾插入 `# CHECK: {{.*}}`，没生成任何有用断言。**MIR 的 CHECK 建议手写。**

正确姿势：脚本生成初稿 → 人工删到只保留有意义的断言 → 再跑一次确认仍然过。

---

## 16.7 三个可跑的测试例子

### 16.7.1 正常路径：IR → asm

```text
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 < %s | FileCheck %s

define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}

; CHECK-LABEL: add:
; CHECK:       // %bb.0: // %entry
; CHECK-NEXT:    add x0, x0, x1
; CHECK-NEXT:    ret
```

真实输出与 16.4.1 相同（`add x0, x0, x1` / `ret`）。能抓到：选成 `ADDWrr`、多出常量 materialization、返回值走错寄存器。抓不到：`G_CONSTANT` 是否被折进立即数（汇编层看不出来）。

### 16.7.2 边界：4096 要走 `ADDXri` 的移位编码

`add i64 %x, 4096` 的汇编是 `add x0, x0, #1, lsl #12`。要在 MIR 层断言"折进立即数且用了 12 位左移"。

`add4096-rbs.mir`（由 `-stop-after=regbankselect -simplify-mir` 生成，节选）：

```yaml
--- |
  define i64 @add4096(i64 %x) {
  entry:
    %r = add i64 %x, 4096
    ret i64 %r
  }
...
---
name:            add4096
legalized:       true
regBankSelected: true
tracksRegLiveness: true
isSSA:           true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
liveins:
  - { reg: '$x0' }
body:             |
  bb.1.entry:
    liveins: $x0

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = G_CONSTANT i64 4096
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
...
```

测试：

```text
# RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 -run-pass=instruction-select %s -o - | FileCheck %s

# CHECK:       selected:        true
# CHECK:       failedISel:      false
# CHECK:       %[[SRC:[0-9]+]]:gpr64sp = COPY $x0
# CHECK:       %[[DST:[0-9]+]]:gpr64sp = ADDXri %[[SRC]], 1, 12
# CHECK-NOT:   G_CONSTANT
```

真实输出（关键三行）：

```yaml
    %0:gpr64sp = COPY $x0
    %2:gpr64sp = ADDXri %0, 1, 12
    $x0 = COPY %2
```

断言了三件事：常量被折叠（`%1` 消失、`G_CONSTANT` 不在）、移位量是 `12`、寄存器类是 `gpr64sp`（`ADDXri` 允许 SP 作基址）。

边界对照（同一份 IR 改常量，真实汇编）：

| 常量 | 汇编 |
| --- | --- |
| 4095 | `add x0, x0, #4095` |
| 4096 | `add x0, x0, #1, lsl #12` |
| 5000 | `mov w8, #5000` + `add x0, x0, x8` |

### 16.7.3 fallback：让失败显形

完整文件（把 `G_ADD` 换成 `G_FADD`，bank 仍是 `gpr`）：

```yaml
--- |
  define i64 @bad(i64 %a, i64 %b) {
  entry:
    %r = add i64 %a, %b
    ret i64 %r
  }
...
---
name:            bad
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
isSSA:           true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
liveins:
  - { reg: '$x0' }
  - { reg: '$x1' }
body:             |
  bb.0.entry:
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_FADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
...
```

**写法 A：断言 fallback 发生了**（能进 CI）：

```text
# RUN: llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select %s -o - | FileCheck %s

# CHECK:       selected:        true
# CHECK:       failedISel:      true
# CHECK:       %2:gpr(i64) = G_FADD %0, %1
```

真实输出：

```yaml
regBankSelected: true
selected:        true
failedISel:      true
    %2:gpr(i64) = G_FADD %0, %1
```

**写法 B：断言它必须报错**：

```text
# RUN: not --crash llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -global-isel-abort=1 %s -o /dev/null 2>&1 | FileCheck %s
# CHECK: LLVM ERROR: cannot select: %2:gpr(i64) = G_FADD %0:gpr, %1:gpr (in function: bad)
```

真实错误（退出码 1）：

```text
LLVM ERROR: cannot select: %2:gpr(i64) = G_FADD %0:gpr, %1:gpr (in function: bad)
```

`not` 在官方 Windows 包里没有。没有它时只能靠退出码肉眼确认，不要写成"测试通过"。


---

## 16.8 踩雷清单

1. **`.mir` 用 `; RUN:`** → `YAML:1:1: missing required key 'name'`。
2. **对 `.ll` 用 `-run-pass`** → `run-pass is for .mir file only.`，先导出 MIR。
3. **只写 `CHECK: ADDXrr` 却不加 `-global-isel-abort=1`** → fallback 时可能假通过。
4. **写死 vreg 编号** → 插一条 COPY 就全变；用 `[[NAME:[0-9]+]]`。
5. **把 `selected: true` 当成功** → 失败时它同样是 true，必须一起看 `failedISel`。


---

## 16.9 进阶

**用 `-mattr` 固定 subtarget**。本机实测：`atomicrmw add` 默认生成 `LDXR/STXR` 循环，`-mattr=+lse` 生成单条 `ldaddal x0, x0, [x1]`。测 feature 相关 pattern 时必须写死，否则 CI 默认 CPU 一变就碎。



---

## 16.10 练习

**16.1** 把练习 A 的 `add-hand.mir` 改成带 `# RUN:` 的测试：断言 `ADDXrr` 的两个操作数都是 `gpr64`、没有 `G_ADD` 残留，并且用 capture 变量而非固定编号。

**16.2** 把 `CHECK-NEXT: ADDXrr` 改成 `CHECK-NEXT: ADDWrr`，在源码构建里观察 FileCheck 的报错；再改成 `CHECK: ADDXrr`，解释为什么它可能仍然失败。





---

## 重点整理

- RUN 行的核心是 `-run-pass=instruction-select` + `-global-isel-abort=1` + `-O0`。
- `.ll` 用 `;` 注释，`.mir` 用 `#` 注释；`-run-pass` 只吃 `.mir`。
- MIR 测试断言"哪条指令 + 哪个寄存器类"，不是物理寄存器。
- 阶段快照用于定位，断言要落到 CHECK 行。
- `update_llc_test_checks.py` 生成的是"当前行为"，不是"正确行为"。
- `-verify-machineinstrs` 不检查 failedISel 函数。

---

## 自我检核

1. 为什么 MIR 测试要显式写 `-O0`？
2. `%0:gpr(i64)`、`%0:gpr64`、`$x0` 分别是什么阶段的什么对象？
3. `CHECK-NOT` 的作用范围由什么决定？
4. 为什么 `-verify-machineinstrs` 抓不到 fallback？
5. `-global-isel-abort=0/1/2` 的退出码与输出分别是什么？

---

## 延伸阅读

- `llvm/test/CodeGen/AArch64/GlobalISel/`：按 `instruction-select` 前缀筛选，看上游如何组织 MIR 测试。
- `llvm/utils/update_llc_test_checks.py` 与 `llvm/utils/UpdateTestChecks/`：脚本实现，尤其 `mir.py` 的支持边界。
- `llvm/lib/CodeGen/MachineVerifier.cpp`：`isFunctionFailedISel` 的提前返回。
- `llvm/docs/CommandGuide/FileCheck.rst`：CHECK-DAG / CHECK-NOT 的精确语义。
- `llvm/docs/TestingGuide.rst`：lit 的目录约定与 `REQUIRES`/`XFAIL`。

---

## 下一章

测试能证明"现在是这样"，但不能告诉你"为什么不是那样"。第 17 章把这些工具反过来用：在只有 Optimized build 的机器上，用 `-print-after`、`-global-isel-abort=1`、`llvm-reduce` 把一次选错逼到最小复现。

[第 17 章 调试工具链](./17-debugging-toolchain.md)
