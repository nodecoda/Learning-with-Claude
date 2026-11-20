# Ch 0 — 环境与证据基线

> **目标**：把本课的源码、构建、工具和「一条结论怎么被证明」钉死。学完这章，你手上有一套可重复的最小实验台：能在不读代码的情况下先判断问题属于 IR 合法性、中端变换还是代码生成，然后再去对应源码验证。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON，targets `X86;AArch64;RISCV`。本章所有输出都在这个构建上真实运行。

## 为什么需要这个？

读 LLVM 源码最常见的失败不是「看不懂 C++」，而是**证据链断了**：

- 看到一个现象，凭记忆说「应该是 InstCombine 做的」，却没跑过 `-print-after=instcombine`。
- 看到一个 PR，觉得「这个改动应该没问题」，却没验证它依赖的 IR 不变量。
- 看到一个测试，觉得 `CHECK` 行都过了，却没想过这些断言在改动前是不是也会过。

Reviewer 和普通读者的区别，不在于谁记得更多 pass 名字，而在于**谁能把一句判断落到可复现的证据上**。这一章先把证据台搭好；后面每一章都会重复同一套动作：

```
源码位置（file:line / 符号）
        +
可复现命令（opt / llc / lit）
        +
真实输出（不是回忆）
        =
一条可以写进 review comment 的结论
```

## 先建立直觉

把 LLVM 想成一条有多道安检的流水线。每一道安检都有自己的「合法输入」定义：

```
源代码 .c
   │  clang driver / cc1
   ▼
LLVM IR (.ll)  ──[Verifier]──  合法性检查：支配关系、类型、属性
   │  opt -passes=...
   ▼
优化后 IR      ──[Verifier]── 每个 pass 之后都可以复查
   │  llc
   ▼
Machine IR     ──[MachineVerifier]── 虚拟寄存器、live range、CFG
   │  regalloc / machine passes
   ▼
MC / 汇编      ──[MC 层断言]── 编码、重定位、立即数范围
   │  as / lld
   ▼
可执行文件
```

一条「为什么这里选错了/优化没了/程序崩了」的问题，先定位它发生在哪道安检之前或之后，再去那道安检的源码里找判据。**不要从最熟悉的目录开始翻**，那是碰运气。

## 钉死环境

先确认你手上的构建和源码是同一棵树、同一个模式。`llvm-config` 一次只能可靠地吃一个查询，分开跑：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/llvm-config --version
$B/llvm-config --assertion-mode
$B/llvm-config --build-mode
```

实测输出：

```text
24.0.0git
ON
RelWithDebInfo
```

`assertion-mode ON` 不是可选项。本课大量依赖 `-debug-only=`、`assert()`、`LLVM_DEBUG`，release-only 构建会把它们全部编译掉，你会得到「命令没报错但也没有输出」的假象。

源码基线单独记一次：

```bash
cd /home/ubuntu/llvm/llvm-project
git log -1 --format='%H %ad %s' --date=short
```

实测：

```text
7c7a9072f736aaa57facedfe23240ce08166ba8c 2026-11-20 [libc++] Remove redundant `_LIBCPP_HIDE_FROM_ABI` from detail headers j-O (#244539)
```

> **行号会漂**。本课引用 `file:line` 是为了让你快速跳过去，不是让你背。升级源码后先 `rg '<符号>' <文件>` 重新定位；函数名、类名、文件名比行号稳定。

工具清单也确认一遍：

```bash
for t in clang opt llc llvm-mca llvm-reduce llvm-lit FileCheck; do
  printf '%-14s %s\n' "$t" "$([ -x $B/$t ] && echo yes || echo no)"
done
```

实测全部 `yes`。其中四个工具构成后面所有实验的骨架：

| 工具 | 回答什么问题 | 典型用法 |
| --- | --- | --- |
| `opt` | 中端发生了什么、IR 是否合法 | `-passes=`、`-print-after=`、`-verify-each` |
| `llc` | 后端把 IR 变成什么机器码 | `-O2`、`-mtriple=`、`-print-after=` |
| `llvm-reduce` | 一个 bug 的最小复现是什么 | `--test=...`、`-o reduced.ll` |
| `llvm-lit` | 一条测试到底在锁什么 | `-v`、`--filter=` |

## 最小实验一：IR 是否合法

先写一段故意违反支配关系（dominance）的 IR。`%x` 只在 `a` 块定义，却在 `b` 块被使用；如果控制流从 `entry` 直接到 `b`，`%x` 根本没有定义：

```llvm
define i32 @g(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  %x = add i32 1, 2
  br label %b
b:
  %y = add i32 %x, 3
  ret i32 %y
}
```

跑 `verify`：

```bash
$B/opt -passes=verify -disable-output bad-dom.ll
```

实测输出：

```text
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
/home/ubuntu/llvm/builds/pr-lane/bin/opt: bad-dom.ll: error: input module is broken!
```

这句话对应 `llvm/lib/IR/Verifier.cpp:5637` 的 `Check(DT.dominates(Op, U), "Instruction does not dominate all uses!", Op, &I);`。记住这个模式：**错误文本就是源码里的字符串字面量**。以后看到任何 verifier 报错，直接 `rg '<报错文本>' llvm/lib/IR/Verifier.cpp`，比读十页文档快。

注意 `verify` 不会「修好」IR，它只负责拒绝。如果你的实验输入本身就非法，后面所有 pass 的行为都没有意义。**先 verify，再谈优化**，这是 review 时也一样的原则。

## 最小实验二：谁改了我的 IR

拿一段常量折叠能吃掉一半的 IR：

```llvm
define i32 @f(i32 %a, i32 %b) {
entry:
  %x = add i32 %a, 0
  %y = mul i32 %x, 1
  %z = add i32 %y, %b
  ret i32 %z
}
```

只跑 InstCombine：

```bash
$B/opt -passes=instcombine -S min.ll
```

实测：

```llvm
; ModuleID = 'min.ll'
source_filename = "min.ll"

define i32 @f(i32 %a, i32 %b) {
entry:
  %z = add i32 %a, %b
  ret i32 %z
}
```

两条指令消失。但「消失了」不等于「知道是谁删的」。加上 `-print-after`：

```bash
$B/opt -passes='default<O1>' -print-after=instcombine -disable-output min.ll 2>&1 | head -40
```

你会看到同一个 `InstCombinePass` 被 pipeline 跑了多轮，每轮打印一次 IR：

```text
; *** IR Dump After InstCombinePass on f ***
define i32 @f(i32 %a, i32 %b) local_unnamed_addr {
entry:
  %z = add i32 %a, %b
  ret i32 %z
}
...
; *** IR Dump After InstCombinePass on f ***
; Function Attrs: mustprogress nofree norecurse nosync nounwind willreturn memory(none)
define i32 @f(i32 %a, i32 %b) local_unnamed_addr #0 {
entry:
  %z = add i32 %b, %a
  ret i32 %z
}
```

这里有两个容易被忽略的事实：

1. **InstCombine 不是「一个 pass 跑一次」**。`default<O1>` 的 pipeline 文本里有多个 `instcombine`，而且 `InstCombinePass` 内部还有 worklist 迭代。
2. **它不只删指令，还改操作数顺序、加函数属性**。你看到 `%z = add i32 %b, %a` 和 `#0` 属性，都不是「原始 IR 里有的东西」。

想知道内部为什么这么改，开 `-debug-only`：

```bash
$B/opt -passes=instcombine -debug-only=instcombine -disable-output min.ll 2>&1 | head -25
```

实测（节选）：

```text
INSTCOMBINE ITERATION #1 on f
ADD:   ret i32 %z
ADD:   %z = add i32 %y, %b
ADD:   %y = mul i32 %x, 1
ADD:   %x = add i32 %a, 0
IC: Visiting:   %x = add i32 %a, 0
IC: Replacing   %x = add i32 %a, 0
    with i32 %a
IC: Mod =   %x = add i32 %a, 0
    New =   %x = add i32 %a, 0
IC: ERASE   %x = add i32 %a, 0
```

这组输出直接对应 `llvm/lib/Transforms/InstCombine/InstructionCombining.cpp` 里的 `LLVM_DEBUG` 与 `STATISTIC`。读源码前先看这些调试输出，你会知道该在哪个函数下断点。

## 最小实验三：后端与 MC

```bash
$B/llc -O2 min.ll -o -
```

实测（x86-64）：

```asm
	.text
	.globl	f
	.prefalign	4, .Lfunc_end0, nop
	.type	f,@function
f:
	.cfi_startproc
# %bb.0:                                # %entry
                                        # kill: def $esi killed $esi def $rsi
                                        # kill: def $edi killed $edi def $rdi
	leal	(%rdi,%rsi), %eax
	retq
```

从 IR 的 `add i32 %a, %b` 到一条 `leal`，中间经过 SelectionDAG/GlobalISel、寄存器分配、MC 编码。`llc` 的每一层都能单独打印：

```bash
$B/llc -O2 -print-after=finalize-isel min.ll -o /dev/null 2>&1 | head -30
```

> 上面的命令在本机可以跑；不同版本 `-print-after` 的阶段名可能不同，先 `llc --help-hidden | rg 'print-after'` 确认。**未实测的阶段名不要写进结论。**

## 断言构建与调试信息

`pr-lane` 构建是 `RelWithDebInfo` + assertions。这意味着：

- `assert()` 生效，pass 的契约被违反时会在现场炸，而不是悄悄产生错 IR。
- `LLVM_DEBUG(...)` 生效，`-debug-only=<tag>` 有输出。
- 有调试符号，`gdb`/`lldb` 能落到源码行。

反过来，如果你拿到的是发行版 `clang`/`opt`，很多本课实验会「看起来通过」。检查方式只有一个：`llvm-config --assertion-mode`。

一个常见误区是「assertions 构建慢，所以用 release 跑测试」。LLVM 自己的 lit 测试大量依赖 `REQUIRES: asserts`；用 release 构建跑，会得到大量 `UNSUPPORTED`，而不是通过。

## 证据分级：写 review 前先标记

本课要求你给每条结论标一个证据等级。不是形式主义，而是防止把猜测写成 blocker：

| 等级 | 含义 | 例 |
| --- | --- | --- |
| E1 | 本机跑出来的真实输出 | `opt -passes=verify` 报 `Instruction does not dominate all uses!` |
| E2 | 源码可查的符号/字符串/调用链 | `Verifier.cpp:5637` 有对应的 `Check(...)` |
| E3 | 官方文档明文 | LangRef 说 `poison` 参与 `select` 的条件会传播 |
| E4 | 有依据的推断，未逐条验证 | 「这个 pattern 可能漏了 scalable vector」 |
| E5 | 纯猜测 | 「我觉得这里应该有问题」 |

Review comment 里，E1/E2/E3 可以直接下判断；E4 要写成问题（「Does this handle ...?」）；E5 不该发出去。后面 Ch 32 会把这条规则变成评论模板。

## 把实验台固化成脚本

每章都手敲命令会漏参数。建一个最小脚本，放在课程目录外，避免污染教材：

```bash
mkdir -p ~/llvm-lab && cd ~/llvm-lab
cat > env.sh <<'SH'
export B=/home/ubuntu/llvm/builds/pr-lane/bin
export SRC=/home/ubuntu/llvm/llvm-project
export PATH="$B:$PATH"
SH
```

之后每个实验目录只放 `.ll` 和输出：

```
~/llvm-lab/
├── env.sh
├── 00-min/ min.ll bad-dom.ll
├── 01-pass/ ...
└── 02-codegen/ ...
```

这样做的好处是，写章节或写 review 时可以把「命令 + 输入 + 输出」一起贴出来，别人能复现。**不能复现的结论，在 LLVM review 里价值接近于零。**

## 踩雷集锦

1. **以为 `opt -passes=instcombine` 只跑一遍**：`InstCombinePass` 内部有 worklist，外层 pipeline 还可能多次调用。看到 IR 变化，先数清楚是第几轮，再决定看哪个函数。
2. **用 release 构建跑 `-debug-only`**：没有输出不是「这个 pass 没有调试信息」，而是 `LLVM_DEBUG` 被编译掉了。先查 `--assertion-mode`。
3. **把 `verify` 当成优化开关**：`-passes=verify` 只检查，不修复。非法 IR 在 verify 之后仍然是非法 IR；不要拿它「清理」输入。
4. **直接信 `-print-after` 的阶段名**：不同版本、不同 target 的阶段名会变。用 `--help-hidden` 查当前构建的真实名字，否则你会得到空输出并误以为 pass 没跑。
5. **拿发行版 clang 的汇编解释源码行为**：发行版可能关 assertions、开不同默认值、带 downstream patch。源码结论必须用你正在读的那棵树构建出来的工具验证。

## 动手练习

1. 把 `bad-dom.ll` 改成合法版本（把 `%x` 移到 `entry`，或让 `b` 只能从 `a` 到达），再跑 `verify`，确认没有输出。
2. 用 `-print-after=instcombine` 跑 `default<O2>`，数一数 `InstCombinePass` 在这段 3 行 IR 上被调用了多少次。解释为什么不是 1 次。
3. 用 `-debug-only=instcombine` 找出 `%y = mul i32 %x, 1` 被替换成什么，以及替换发生在第几轮。
4. 用 `llc -print-after=finalize-isel` 打印 `min.ll` 的 Machine IR，找到 `leal` 对应的 `MachineInstr` opcode。
5. 故意把 `-passes=verify` 去掉，跑一个会产生非法 IR 的实验（后面 Ch 16 会给例子），观察 `-verify-each` 如何在第一个违规 pass 处停下。

## 本章重点整理

- 证据链是「源码位置 + 可复现命令 + 真实输出」，三者缺一，结论就降级。
- 先 `verify` 再优化；非法 IR 上的所有行为都不构成证据。
- `-print-after` 看「哪一轮改了什么」，`-debug-only` 看「函数内部为什么改」，两者不能互相替代。
- assertions 构建是本课的前提；`llvm-config --assertion-mode` 是唯一可靠的检查方式。
- 行号会漂，符号和文件路径才是稳定锚点。

## 自我检核

- [ ] 能说出 `verify`、`-print-after`、`-debug-only` 各自回答什么问题，以及不能回答什么
- [ ] 能在不查文档的情况下解释为什么 assertions 构建是必须的
- [ ] 能给出一个 E1 证据和一个 E4 证据的例子，并说明为什么 review 里要区别对待
- [ ] 知道 `opt` 的 pipeline 可能多次调用同一个 pass，并会用 `-print-after` 验证
- [ ] 知道行号漂移时该怎么重新定位（`rg '<符号>'`）
- [ ] 能解释为什么「本机能跑」不算证据，以及 E1/E4 分级怎么用在 review comment 里
- [ ] 能区分「构建产物可信」与「源码行号可信」这两种不同的基线声明
- [ ] 能在换 build 或换 commit 后，判断本章哪类结论仍然成立、哪类需要重跑

## 延伸阅读

- **[LLVM Command Guide: opt](https://llvm.org/docs/CommandGuide/opt.html)**
  - **读哪里**：`-passes`、`-print-after`、`-verify-each`、`-debug-only` 四节
  - **为什么值得读**：本课所有中端实验的入口；官方对 pipeline 语法的定义比任何博客准确
- **[LLVM Command Guide: llc](https://llvm.org/docs/CommandGuide/llc.html)**
  - **读哪里**：`-print-after`、`-stop-after`、`-mtriple`、`-mcpu`
  - **和本章的关联**：Ch 23–26 的后端实验都从这里开始
- **[LLVM Developer Policy: Test Cases](https://llvm.org/docs/DeveloperPolicy.html#test-cases)**
  - **读哪里**：整节；重点看「每个 bug fix 都要有回归测试」的措辞
  - **为什么值得读**：Ch 31 的测试 review 标准来自这里，不是自创
- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：开头的 `Value`/`User`/`Type` 导览，以及 `dyn_cast` 一节
  - **和本章的关联**：Ch 3/4 的源码阅读方法在这里有官方版本

→ [Ch 1 — 源码树地图：从 3184 个 `.cpp` 找到该看的那一个](./01-source-tree-map.md)
