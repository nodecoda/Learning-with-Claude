# Ch 26 — MIR/lit 测试：同时锁定选择和调度

> **目标**：把「这条指令是谁选的、这个顺序是谁排的」变成可重复执行的断言。学完你能写出三层测试：用 `.ll` + `-stop-after=finalize-isel` 锁住选择结果，用 `.mir` + `-run-pass=machine-scheduler` 锁住调度顺序，用 `.s`/`llvm-mca` 锁住最终形态和模型数字；并且知道每种断言过强、过弱时分别会怎么坏。
>
> **环境**：LLVM 24.0.0git；`llc`、`FileCheck`、`llvm-lit`、`split-file`、`update_mir_test_checks.py`；源码树 `/home/ubuntu/llvm/trees/explore`，构建 `/home/ubuntu/llvm/builds/explore`。本章所有 MIR、报错、lit 输出均为本机实跑；临时目录 `/tmp/ch26/`。

## 为什么测试是「选择 × 调度」的第三只手

面试里有一类问题专门筛掉「只会看汇编」的人：

> 「你改了这个 sched model，怎么知道没有把别的 CPU 弄坏？」
> 「你怎么证明这条 `czero.eqz` 是 instruction selection 选的，而不是后面某个 pass 换的？」
> 「调度顺序变了，你怎么把它固化下来？」

这三个问题的共同答案是：**在正确的阶段截断管线，并对该阶段的产物做断言**。LLVM 的测试基础设施正好为这件事设计：

- `llc -stop-after=<pass>`：从 IR 跑到指定 pass 停下，输出该阶段的 MIR。
- `llc -run-pass=<pass>`：从 MIR 出发，只跑一个 pass。
- `FileCheck`：对输出做模式匹配；`CHECK-NEXT` 能表达「相邻且有序」。
- `llvm-lit`：把上面这些命令变成可批量执行的回归测试。

本章不教 lit 的全部语法；只教「选择」和「调度」这两条线各自该在哪一刀切、断言该写多强。

## 先建立直觉：一条测试管一件事

```text
                 ┌──────────────────────────────┐
   foo.ll  ──────►│ llc -stop-after=finalize-isel│──► MIR：选择已定，顺序未排
                 └──────────────────────────────┘
                 ┌──────────────────────────────┐
   foo.mir ──────►│ llc -run-pass=machine-scheduler│──► MIR：pre-RA 顺序已定
                 └──────────────────────────────┘
                 ┌──────────────────────────────┐
   foo.mir ──────►│ llc -run-pass=prologepilog   │──► MIR：寄存器/栈已定
                 └──────────────────────────────┘
                 ┌──────────────────────────────┐
   foo.ll  ──────►│ llc -O2 -mcpu=...            │──► .s：端到端形态
                 └──────────────────────────────┘
```

断言强度从弱到强大致是：

| 测试形态 | 锁住什么 | 坏了会怎样 | 适合谁 |
|---|---|---|---|
| `.ll` + `-stop-after=finalize-isel` | 选出的 opcode、pseudo 形状 | 改 pattern 立刻红 | instruction selection |
| `.mir` + `-run-pass=machine-scheduler` | 指令相对顺序 | 改 sched model / 策略立刻红 | scheduling |
| `.mir` + `-run-pass=prologepilog` | 物理寄存器、栈偏移 | 改 RA/PEI 立刻红 | regalloc、frame |
| `.ll` → `.s` + FileCheck | 端到端 | 任何一层变了都可能红（定位差） | 用户可见行为 |
| `llvm-mca` 的 `.test` | 模型数字（latency/吞吐） | 改 `WriteRes` 立刻红 | sched model |

关键判断：**测试要红得有意义**。端到端 `.s` 测试最省事，但它把四层的变化混在一个断言里，红了之后你还得二分定位。分层测试的价值不是「更严格」，而是「红了就知道该看哪一层」。

## 主线一：把 MIR 读成「选择的证据」

先用第 18 章的 `select` 例子。IR 是：

```llvm
define i64 @sel(i64 %a, i64 %b, i1 zeroext %c) {
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

用 `-stop-after=finalize-isel` 停在选择刚结束的位置：

```bash
$ /home/ubuntu/llvm/builds/explore/bin/llc \
    -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
    -stop-after=finalize-isel sel.ll -o -
```

本机输出里，`@sel` 的 body 是：

```yaml
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11, $x12

    %2:gpr = COPY $x12
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %3:gpr = COPY $x0
    %4:gpr = PseudoCCMOVGPR %1, %0, 14180, %2, %3
    $x10 = COPY %4
    PseudoRET implicit $x10
```

这段输出同时回答了两个问题：

1. **选择已经发生了**：`select` 变成了 `PseudoCCMOVGPR`，不是 `czero.eqz`。
2. **展开还没发生**：`PseudoCCMOVGPR` 要等到 pre-emit 才展开成真实分支（第 20 章）。所以「最终汇编里没有 `PseudoCCMOVGPR`」不代表它没被选过。

`14180` 不是魔法数字。它是 TableGen 生成的 opcode 枚举值；在本机构建里：

```text
BEQ  = 14180
BNE  = 14190
```

（`RISCVGenInstrInfo.inc` 中的 `BEQ = 14180`、`BNE = 14190`。）也就是说这个 pseudo 的第 3 个操作数是一个**分支 opcode 操作数**，不是普通立即数。MIR 打印器把它打成数字，是因为它属于 `bcc_opcode` 这个自定义操作数类型（`RISCVInstrInfoSFB.td`）。

这件事对写测试很重要：**不要把 `14180` 当业务常量去断言**。它是生成枚举，LLVM 加减指令就会变。要断言「选了什么」，应该匹配指令名和操作数结构，而不是 opcode 数值。

`finalize-isel` 之后还有 `%3:gpr = COPY $x0`：`sfb_rhs` 操作数要求一个寄存器，常量 0 先被 materialize 成 `$x0` 的 COPY。这也是选择阶段的产物，能被测试锁住。

## 主线二：post-RA 的 MIR 是调度后的现场

同一个函数，跑到 `machine-scheduler`（这里指 post-RA 那次；`machine-scheduler` 在 pre-RA 和 post-RA 各出现一次，`-stop-after` 停在最后一次）：

```bash
$ /home/ubuntu/llvm/builds/explore/bin/llc \
    -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
    -stop-after=machine-scheduler sel.ll -o -
```

本机 body：

```yaml
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11, $x12

    %2:gpr = COPY $x12
    %1:gpr = COPY $x11
    %4:gpr = COPY $x10
    %4:gpr = PseudoCCMOVGPR %4, %1, 14190, %2, $x0
    $x10 = COPY %4
    PseudoRET implicit $x10
```

和上一节对比，三个变化都发生了：

| 变化 | 从 | 到 | 谁做的 |
|---|---|---|---|
| 条件码 | `BEQ`（14180） | `BNE`（14190） | `PseudoCCMOVGPR` 可交换，commute 时取反 |
| 操作数 | `%1, %0` | `%4, %1` | 同上，falsev/truev 交换 |
| `sfb_rhs` | `%3`（值为 0） | `$x0` | 寄存器分配后 vreg 消失 |

这正是「同一份选择、不同阶段现场」的典型。你要锁调度，就断言这一层；要锁选择，就断言上一层。混在一起，红了无法归因。

再看第 18 章的 `sel0`（`select %c, %a, 0`）：在 `-O2` 的最终汇编里它变成了 `czero.eqz`，但在 `finalize-isel` 的 MIR 里你只会看到 `PseudoCCMOVGPR`。如果你写了一个 `.s` 测试断言 `czero.eqz`，然后有人调了 pseudo 展开或 if-conversion，测试红了，你会误以为是 selection 的问题。分层测试就是防这个。

## 主线三：只跑一个 pass，把顺序钉死

`-run-pass` 只接受 `.mir`。用 `.ll` 会得到：

```text
$ llc -run-pass=machine-scheduler sel.ll -o -
/home/ubuntu/llvm/builds/explore/bin/llc: error: run-pass is for .mir file only.
```

正确做法是先把 MIR 存下来，再反复只跑调度器。第 25 章用过的 `one.mir`（`@f` 做三次 load、三次 mul、三次 add）就是为此准备的。它的 body 是：

```yaml
body:             |
  bb.0.entry:
    liveins: $x10, $x11, $x12, $x13, $x14

    %4:gpr = COPY $x14
    %3:gpr = COPY $x13
    %2:gpr = COPY $x12
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %5:gpr = LD %0, 0 :: (load (s64) from %ir.p)
    %6:gpr = LD %0, 8 :: (load (s64) from %ir.p1)
    %7:gpr = LD %0, 16 :: (load (s64) from %ir.p2)
    %8:gpr = MUL %5, %1
    %9:gpr = MUL %6, %2
    %10:gpr = MUL %7, %3
    %11:gpr = ADD %8, %9
    %12:gpr = ADD %11, %10
    %13:gpr = ADD %12, %4
    $x10 = COPY %13
    PseudoRET implicit $x10
```

跑 top-down：

```bash
$ llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
      -run-pass=machine-scheduler -misched-prera-direction=topdown \
      one_test2.mir -o -
```

body 与输入同序（`LD` 三条连续，`MUL` 三条连续）。换 bottom-up：

```bash
$ llc ... -run-pass=machine-scheduler -misched-prera-direction=bottomup one_test2.mir -o -
```

本机输出变成：

```yaml
    %0:gpr = COPY $x10
    %1:gpr = COPY $x11
    %5:gpr = LD %0, 0 :: (load (s64) from %ir.p)
    %2:gpr = COPY $x12
    %6:gpr = LD %0, 8 :: (load (s64) from %ir.p1)
    %3:gpr = COPY $x13
    %7:gpr = LD %0, 16 :: (load (s64) from %ir.p2)
    %8:gpr = MUL %5, %1
    ...
    %4:gpr = COPY $x14
    %12:gpr = ADD %11, %10
```

注意 `COPY $x12` 被塞到两条 `LD` 中间。这不是调度器乱来：bottom-up 从依赖链尾部往上建，COPY 是零延迟节点，能填延迟槽就填。**这类「顺带被调进来的 COPY」正是强的 `CHECK-NEXT` 最容易踩的坑**：你的语义断言是 `LD, LD, LD`，测试却因为 COPY 插进来而红。

所以写调度顺序测试时，先决定你要锁的粒度：

- 锁「三条 load 连续」：用 `CHECK-DAG` 断言存在一个窗口，或者用 `CHECK-NEXT` 但先把 COPY 用 `CHECK` 跳过（`CHECK` 不要求相邻）。
- 锁「LD→MUL 的依赖顺序」：用 `CHECK: ...` 加 `CHECK: ...`，顺序敏感但不要求相邻。
- 锁「调度器就是按 top-down 排的」：用 `CHECK-NEXT` 严格断言，同时固定 `-misched-prera-direction`，否则换个默认方向就红。

## 主线四：lit 把命令变成回归

一个最小可跑的 lit 目录（本机实测）：

```python
# /tmp/ch26/litdir/lit.cfg.py
import lit.formats, os
LLVM_BIN = '/home/ubuntu/llvm/builds/explore/bin'
config.name = 'ch26'
config.test_format = lit.formats.ShTest(False)
config.suffixes = ['.mir', '.ll']
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, 'Output')
config.environment['PATH'] = LLVM_BIN + os.pathsep + os.environ['PATH']
config.substitutions.append(('%llc', os.path.join(LLVM_BIN, 'llc')))
config.substitutions.append(('%FileCheck', os.path.join(LLVM_BIN, 'FileCheck')))
```

测试文件开头：

```text
# RUN: %llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
# RUN:   -run-pass=machine-scheduler -misched-prera-direction=topdown %s -o - \
# RUN:   | FileCheck %s
```

跑：

```text
$ llvm-lit -v t1.mir
-- Testing: 1 tests, 1 workers --
PASS: ch26 :: t1.mir (1 of 1)
```

三个本机踩出来的真实错误，按出现顺序：

**(1) `ShTest(True)` 在 LLVM 24 已经不能用了。**

```text
ValueError: execute_external=True is deprected as of LLVM-23 and the option will
be removed in LLVM-24. Please move to using the internal shell
(execute_external=False).
```

`lit.formats.ShTest()` 不带参数即可。自定义 lit 配置不要照抄旧博客。

**(2) RUN 行里写 `llc` 会 127，而不是 PASS/FAIL。**

```text
# executed command: llc -mtriple=riscv64 ... t1.mir -o -
# | 'llc': command not found
# error: command failed with exit status: 127
```

原因：`llc` 不在 `PATH` 里，而且 LLVM 官方测试套件的 `%llc` 替换是 `llvm/test/lit.cfg.py` 提供的，不是 lit 内建的。独立目录要么把 `bin` 加进 `PATH`，要么定义 `%llc` 替换（上面的 `lit.cfg.py` 两件事都做了）。**看到 127 先查环境，不要改测试语义。**

**(3) 断言错了，FileCheck 的报错要会读。**

把 `CHECK-NEXT: %8:gpr = MUL` 故意改成 `ADD` 之后：

```text
# | t2.mir:6:15: error: CHECK-NEXT: expected string not found in input
# | # CHECK-NEXT: %8:gpr = ADD
# |               ^
# | <stdin>:110:13: note: scanning from here
# |  %7:gpr = LD %0, 16 :: (load (s64) from %ir.p2)
# |             ^
# | <stdin>:114:3: note: possible intended match here
# |  %11:gpr = ADD %8, %9
```

读法固定：`error` 是断言失败，`scanning from here` 是上一个匹配结束的位置，`possible intended match` 是 FileCheck 猜你可能想匹配的位置。调度顺序测试里，这两个 note 通常直接告诉你「调度器把谁提前了」。

## 主线五：`-verify-machineinstrs` 与自动生成断言

`-verify-machineinstrs` 在每个 pass 后插 `MachineVerifier`。它不是默认开的，写 MIR 测试时建议显式加。两个本机真实错误：

**SSA 被破坏**（删掉 `%8` 的定义、保留使用）：

```text
error: bad.mir: f has explicit property IsSSA, but is not valid SSA
```

**用了未定义的物理寄存器**（把 `LD %0, 0` 改成 `LD $x20, 0`，而 `$x20` 不在 liveins）：

```text
*** Bad machine code: Using an undefined physical register ***
LLVM ERROR: Found 1 machine code errors.
```

第二条尤其常出现在手写 MIR：你从汇编里抄了一条 `$x20`，但没在 `liveins` 里声明。verifier 的报错比 FileCheck 的 diff 更接近根因。

自动生成断言：

```bash
$ python3 llvm/utils/update_mir_test_checks.py \
    --llc-binary /home/ubuntu/llvm/builds/explore/bin/llc t3.mir
```

本机效果（文件头多一行，body 前插入 CHECK）：

```text
# NOTE: Assertions have been autogenerated by utils/update_mir_test_checks.py UTC_ARGS: --version 6
...
    ; CHECK: [[COPY:%[0-9]+]]:gpr = COPY $x14
    ; CHECK-NEXT: [[COPY1:%[0-9]+]]:gpr = COPY $x13
    ; CHECK-NEXT: [[ADD2:%[0-9]+]]:gpr = ADD [[ADD1]], [[COPY]]
```

它的价值是「快速把现状固化」，代价是**它锁的是实现细节**：vreg 编号、COPY 位置、寄存器分配结果全都被写进断言。上游 review 常要求作者把自动生成的断言裁到「语义相关的那几条」；直接把 200 行 `CHECK-NEXT` 提交上去，下一个改 pass 顺序的人会先来骂你。

一个可操作的裁剪原则：

1. 自动生成。
2. 删掉所有 `COPY`、`PseudoRET`、frame 设置相关的断言。
3. 保留「选择结果」（opcode 与操作数关系）。
4. 调度测试只保留你要锁的那 3–6 条相对顺序。
5. 重跑 `llvm-lit`，确认裁剪后仍然能红。

## 底层机制：三个开关分别截在哪

- **`-stop-after=<pass>`**：`llc` 的 codegen pipeline 由 `TargetPassConfig` 构建；`-stop-after` 按 pass 名在 pipeline 里插入停止点，并把当前 MIR 打印出来。名字必须与 `-debug-pass=Structure` 里看到的一致（`finalize-isel`、`machine-scheduler`、`prologepilog`）。
- **`-run-pass=<pass>`**：走 MIR parser，只构建该 pass 及其依赖的 analysis；因此输入必须是合法 MIR（`--- |` 的 IR 段 + `---` 的 machine function 段）。`.ll` 没有 machine function，直接报错。
- **`-verify-machineinstrs`**：把 `MachineVerifier` 插到每个 pass 之后；失败是 `LLVM ERROR`，不是普通诊断。CI 里它比断言更早发现「MIR 本身不合法」。

还有三个影响确定性的开关，写测试时必须固定：

| 开关 | 不固定的后果 |
|---|---|
| `-mcpu=` | 不同 CPU 的 sched model 不同，顺序直接变 |
| `-mattr=` | 少了 `+m`，`MUL` 可能变成 libcall |
| `-misched-prera-direction=` | 默认策略可能随 CPU 变；top-down/bottom-up 顺序完全不同 |
| `-enable-pipeliner` | optional bool，默认关；开了会重排整个循环 |

## 踩雷清单

1. **注释符分两套，位置写错就解析失败**。文件头（`--- |` 之前）和 YAML 元数据区是 YAML，用 `#`；`body: |` 里是 MIR 文本，用 `;`。本机两个真实报错：

   ```text
   ; RUN: llc %s          ← 文件头用了 ;
   error: YAML:1:1: missing required key 'name'
   ```

   ```text
       # CHECK: hello     ← body 内用了 #
   error: c_hash.mir:120:5: unexpected character '#'
   ```

   所以 `update_mir_test_checks.py` 把 `; CHECK:` 写进 body 是对的；而测试文件开头的 `# RUN:` 也是对的。两套语法各有各的地盘。

   这个错误还会和 `not` 组合成**假通过**。把注释写错、再用 `not llc ... 2>&1 | FileCheck` 做负向测试，llc 的报错信息会把源码里的 `# CHECK:` 原样回显到 stdout，FileCheck 于是匹配到了自己的源码行：

   ```text
   $ llvm-lit -v t_falsepass5.mir
   PASS: ch26 :: t_falsepass5.mir (1 of 1)
   ```

   但 llc 根本没有执行成功，它只是解析失败。`pre-pr-check.sh` 专门扫 `.mir` 里的 `# CHECK` 就是为了堵这个洞（第 28 章）。**负向测试里永远不要让被测程序的报错进入 FileCheck 的输入流**，除非你显式匹配 `error:` 并接受它。
2. **`-run-pass` 不适用于 `.ll`**。报错是 `run-pass is for .mir file only.`，不是「找不到 pass」。
3. **`CHECK-NEXT` 会把 COPY 也算进顺序**。bottom-up 调度把 `COPY $x12` 插进两条 `LD` 之间，是合法行为；断言前先想清楚你要锁的是数据依赖还是字面相邻。
4. **`update_mir_test_checks.py` 会锁死 vreg 编号**。`[[ADD2:%[0-9]+]]` 这类模式在 pass 顺序变化时会大面积失效；它适合起步，不适合直接提交。
5. **`-verify-machineinstrs` 默认不在所有配置下开**。手写 MIR 时不加，等到 RA 才炸，根因已经被埋到几百行之外。
6. **`-stop-after` 的名字写错不会报「未知 pass」那么直观**。先跑 `-debug-pass=Structure` 对名字，别猜。

## 动手练习

1. 用本章的 `one.mir`，写一个 `t_topdown.mir`：断言三条 `LD` 连续、随后三条 `MUL`。分别用 topdown/bottomup 跑 `llvm-lit`，记录哪个 PASS、哪个 FAIL，并解释 FAIL 的第一条 note。
2. 把 `sel.ll` 改成 `-stop-after=finalize-isel` 的 lit 测试，用 `update_mir_test_checks.py` 生成断言，然后裁到只剩 `PseudoCCMOVGPR` 那一行。思考：为什么不能断言 `14180`？
3. 在 `one_test2.mir` 里制造两个错误：(a) 删掉 `%8` 定义；(b) 把某个 `%0` 换成未声明的 `$x20`。分别跑 `-verify-machineinstrs`，记录两条报错的第一行。
4. 给 `t1.mir` 加 `-verify-machineinstrs`，确认 topdown 仍然 PASS；再加 `-misched-postra=true`，观察顺序是否变化。
5. **guided**：在 `llvm/test/CodeGen/RISCV/` 找一个用 `-stop-after=finalize-isel` 的现有测试，对照它的 CHECK 粒度，说明作者为什么没有用 `-run-pass`。

## 重点整理

| 你想锁的东西 | 用什么输入 | 用什么开关 | 断言怎么写 |
|---|---|---|---|
| 选了哪条指令 | `.ll` | `-stop-after=finalize-isel` | `CHECK: PseudoCCMOVGPR` / opcode 名 |
| pre-RA 调度顺序 | `.mir` | `-run-pass=machine-scheduler` | `CHECK-NEXT` 锁相邻，或 `CHECK` 锁相对序 |
| post-RA 顺序 | `.mir` | `-run-pass=postmisched` | 同上，注意物理寄存器已出现 |
| 最终寄存器/栈 | `.mir` | `-run-pass=prologepilog` | `CHECK: $x10`、`CHECK: -8` |
| 端到端形态 | `.ll` | `-O2 -mcpu=` | `CHECK` 指令序列 |
| 模型数字 | `.s` | `llvm-mca -mcpu=` | `CHECK: Latency` / `RThroughput` |

- 分层测试的意义是**归因**，不是更严格；端到端测试红了，先分层复现再改。
- `-run-pass` 只吃 MIR；`.ll` 要用 `-stop-after`。
- `PseudoCCMOVGPR` 的 `14180/14190` 是生成枚举（BEQ/BNE），不是稳定契约。
- lit 的 `%llc` 不是内建的；自定义目录要自己提供替换或 PATH。
- `-verify-machineinstrs` 抓「MIR 不合法」，FileCheck 抓「行为变了」，两者不能互相替代。

## 自我检核

- [ ] 我能说出 `-stop-after` 与 `-run-pass` 各自接受什么输入、截在管线哪里。
- [ ] 我能解释为什么 `select` 在 `finalize-isel` 阶段是 `PseudoCCMOVGPR`，而最终汇编是 `czero.eqz` 或分支。
- [ ] 我能读懂 FileCheck 的 `scanning from here` 与 `possible intended match here`。
- [ ] 我知道 `CHECK-NEXT` 在调度测试里什么时候过强，并给出两种替代写法。
- [ ] 我能用 `-verify-machineinstrs` 复现「undefined physical register」并修好 liveins。
- [ ] 我知道 `update_mir_test_checks.py` 的输出为什么不能直接提交。

## 延伸阅读

- `llvm/docs/MIRLangRef.rst`：MIR 的 YAML 结构、`body`、`liveins`、`frameInfo` 的官方定义；写测试前先查它，不要从别人的 MIR 猜字段。
- `llvm/docs/TestingGuide.rst`：lit 的目录结构、`RUN` 行、`%s`/`%t` 替换、`REQUIRES`/`XFAIL` 的语义；本章的 lit.cfg 只是最小子集。
- `llvm/test/CodeGen/RISCV/`：搜 `-stop-after=finalize-isel`，看 upstream 如何把「选择」单独锁住；再搜 `-run-pass=machine-scheduler` 看调度测试的断言粒度。
- `llvm/utils/update_mir_test_checks.py`：自动生成断言的实现；读它能理解 `[[NAME:%[0-9]+]]` 的命名规则和裁剪边界。
- `llvm/lib/CodeGen/MachineVerifier.cpp`：`-verify-machineinstrs` 的检查项；遇到「Bad machine code」先在这里找判定条件。

→ [下一章：性能归因](./27-performance-attribution.md)
