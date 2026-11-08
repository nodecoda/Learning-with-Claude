# Ch 28 — 调试 CodeGen：从症状到最小复现

> **目标**：拿到“汇编不对 / `llc` 崩了 / 链接失败 / 程序跑错”，能在半小时内判断根因落在 legalization、ISel、机器 pass 还是 MC 与链接层，并产出别人能原样重跑的最小复现（IR + 命令 + 期望 vs 实际）。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON，`llc --version` 打印 `Optimized build with assertions.`）；源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（`analysis/riscv-maintenance`，HEAD `0fc5afa04c23`）。`llvm-reduce`/`llvm-mc`/`llvm-readobj`/`llvm-mca`/`llvm-lit`/`FileCheck`/`not`/`ld.lld` 齐全；qemu 为 `/usr/bin/qemu-riscv64-static`（6.2，user-mode，RVV 1.0 会 SIGILL）。实验只写 `/tmp`，源码树只读。

> **前置**：[Ch 03 — 流水线与 MIR](./03-pipeline-and-mir.md)（`-stop-after`/`-run-pass`/`isSSA`）、[练习 D — 用 MIR 驱动机器 pass](./practice-d-machine-passes.md)（`-run-pass` 输入契约、verifier）、[Ch 27 — lit 测试与 update 脚本](./27-lit-testing-and-update-scripts.md)（`RUN` 行、`FileCheck`、`update_llc_test_checks.py`）。本章不重复 pass 算法，只讲“怎么定位”。

---

## 28.1 为什么“调试 CodeGen”是独立技能

RISC-V 的报障几乎从不指向根因。三句真实的话：

1. **“这里应该有一条 `addw`，怎么变成 `add` 了？”** 根因不在 ISel，而在 `riscv-opt-w-instrs` 的 `hasAllWUsers` 判定；ISel 阶段 `ADDW` 明明选对了。
2. **“换了个 `-march` 就崩。”** 崩在 MC 编码，根因可能是 TableGen 里某个 `Predicates` 太宽，让不存在的指令通过了汇编器。
3. **“`-O2` 结果不对，`-O0` 对。”** 经常根本不是后端 bug，而是 C 代码的 UB（28.12 实测：`-O0` 退出码 0，`-O2` 退出码 1）。

三次“看代码猜”能烧掉几天。分层定位法的价值是：**每一层只回答一个问题**——输入、输出、差异在哪一层首次出现；差异首次出现处，根因就在该层与上一层之间。

另一个反直觉的事实：`llc` 的报错位置不是根因位置。`LLVM ERROR: Don't know how to legalize this scalable vector type` 由 ISel 抛出（28.8 实测），真正的问题可能是“忘了开 `+v`”；`Cannot select` 由生成的 matcher 抛出，根因往往是少了一条 `Pat<>`。

---

## 28.2 先建立直觉：七种症状 → 七个第一动作

```text
       你看到的                   最近的层                  根因常在
  ┌──────────────────┐      ┌──────────────────┐      ┌───────────────────────┐
  │ 汇编少一条指令    │ ───► │ 汇编文本          │ ───► │ 机器 pass / ISel / DAG │
  │ llc 崩           │ ───► │ 栈回溯里的 pass   │ ───► │ 该 pass 的输入契约     │
  │ 汇编器拒绝        │ ───► │ llvm-mc 诊断      │ ───► │ .td Predicates/feature │
  │ 链接失败          │ ───► │ lld + 重定位      │ ───► │ code/reloc model、符号 │
  │ 运行结果错        │ ───► │ qemu 退出码       │ ───► │ UB / ABI / 编码        │
  │ 性能差            │ ───► │ 指令序列          │ ───► │ 调度模型 / 机器 pass   │
  │ 测试挂            │ ───► │ CHECK 行          │ ───► │ 行为变了，还是测试旧了 │
  └──────────────────┘      └──────────────────┘      └───────────────────────┘
```

| 症状 | 第一动作 |
|---|---|
| 汇编少了一条指令 | `-print-after=<pass>` 二分（28.3、28.5） |
| `llc` 崩 | `llvm-reduce` + `-debug-only`（28.4、28.8） |
| 汇编器拒绝 | `llvm-mc -show-encoding` + 源码 `Predicates`（实验 5） |
| 链接失败 | `llvm-readobj -r` + `lld` 报错（实验 6） |
| 运行结果错 | qemu + 最小 C + `-O0` 对照（实验 7） |
| 性能差 | `llvm-mca` + `-print-after=machine-scheduler`（实验 4） |
| 测试挂 | `llvm-lit -v` + update 脚本 diff（实验 3） |

七个动作的共同点：**先把“哪一层”钉死，再读那一层的源码**。跳过这一步，就会在 `RISCVISelLowering.cpp` 的两万行里找一个其实在 `RISCVOptWInstrs.cpp` 的问题。

---

## 28.3 分层定位法：四个快照，逐层比对

方法一句话：**固定输入，改变停点，只 diff 相邻两层**。

### 28.3.1 案例一：`ADDW` 消失

```bash
mkdir -p /tmp/ch28
cat > /tmp/ch28/store.ll <<'EOF'
define void @f(ptr %p, i64 %a, i64 %b) {
  %r = add i64 %a, %b
  %t = trunc i64 %r to i32
  store i32 %t, ptr %p
  ret void
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk
for p in finalize-isel riscv-opt-w-instrs prolog-epilog; do
  echo "== $p =="
  $T/bin/llc -mtriple=riscv64 -O2 -stop-after=$p /tmp/ch28/store.ll -o - | sed -n '/body:/,$p'
done
$T/bin/llc -mtriple=riscv64 -O2 /tmp/ch28/store.ll -o -
```

| 层 | 实测 body 关键行 | 结论 |
|---|---|---|
| `finalize-isel` | `%3:gpr = ADDW %1, %2` + `SW %3, %0, 0` | ISel 选对了 `ADDW` |
| `riscv-opt-w-instrs` | `%3:gpr = ADD %1, %2` + `SW %3, %0, 0` | **差异首次出现在这一层** |
| `prolog-epilog` | `renamable $x11 = ADD killed renamable $x11, killed renamable $x12` | RA 已换物理寄存器，指令不变 |
| 最终汇编 | `add a1, a1, a2` / `sw a1, 0(a0)` | MC 只是照抄 |

钉死中间层后再问“这个 pass 该不该做这件事”。判据在 `RISCVOptWInstrs.cpp:663` 的 `hasAllWUsers(*MI, ST, MRI)`（`:374` → `hasAllNBitUsers(..., 32)`，`:119`）：唯一使用者 `SW` 只读低 32 位，`ADDW` 的符号扩展冗余，换成 `ADD` 合法。**这不是 bug，是优化。**

同一条链还能用 `-opt-bisect-limit` 反向确认（28.7）：`N=24` 输出 `addw`，`N=25` 输出 `add`，而 `(25)` 正是 `riscv-opt-w-instrs`。

### 28.3.2 案例二：`%hi/%lo` 从哪来

```bash
printf '@g = global i32 7\ndefine i32 @ldg() {\n  %%v = load i32, ptr @g\n  ret i32 %%v\n}\n' > /tmp/ch28/g.ll
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=finalize-isel /tmp/ch28/g.ll -o -
$T/bin/llc -mtriple=riscv64 -O2 /tmp/ch28/g.ll -o -
```

| 层 | 实测 | 谁做的 |
|---|---|---|
| `finalize-isel` | `%0:gpr = LUI target-flags(riscv-hi) @g`；`%1:gpr = LW %0, target-flags(riscv-lo) @g` | ISel 选出 `LUI` + 折叠的 `LW` |
| `prolog-epilog` | `renamable $x10 = LUI target-flags(riscv-hi) @g`；`LW killed ... target-flags(riscv-lo)` | RA 换物理寄存器 |
| 最终汇编 | `lui a0, %hi(g)` / `lw a0, %lo(g)(a0)` | **AsmPrinter / MC 把 target-flags 变成修饰符** |

MIR 里根本没有 `%hi`，只有 `target-flags(riscv-hi)`。查这类问题时，最后一层要看 `RISCVAsmPrinter.cpp` 与 MC 的 `RISCVMCExpr`，不是 ISel。

### 28.3.3 操作纪律

- **一次只 diff 相邻两层**；从 `finalize-isel` 直接跳到汇编，RA、调度、伪指令展开、压缩会混在一起。差异若落在 `prolog-epilog` 与汇编之间，插入 `-stop-after=riscv-expand-pseudo-pre-emit`（实测可用）把伪指令展开单独隔出来。
- **停点必须是 pass 名**。`-stop-after=dagcombine` 直接 `LLVM ERROR: "dagcombine" pass is not registered.`（实测 rc=1）。
- **`-print-after` 观察，`-stop-after` 取输入**；两者都跑 `-O0` 与 `-O2`，因为 `-O2` 独有的 15 个 RISC-V pass（Ch 03）是“只在优化档出现”的 bug 高发区。

---

## 28.4 `-debug-only`：本树真实可用的 `DEBUG_TYPE`

`LLVM_DEBUG` 在 `NDEBUG` 下被编译掉（`llvm/include/llvm/Support/Debug.h:37` 的 `#ifndef NDEBUG`），所以 **`-debug-only` 只对 assertions 构建有意义**。本构建可用。

匹配对象是 `#define DEBUG_TYPE` / `INITIALIZE_PASS` 的注册名，**不是**文件名、不是 pass 显示名。逐个实测：

| `-debug-only=` | 来源（本树） | 实测 | 说明 |
|---|---|---|---|
| `isel` | `SelectionDAGISel.cpp:118` | ✅ 334 行 | ISel 主流程 + DAG dump |
| `riscv-isel` | `RISCVISelDAGToDAG.cpp:29` | ✅ 条件触发 | 仅 `PreprocessISelDAG` 改写时打印（`:196-204`） |
| `legalize-types` | `LegalizeTypes.cpp:23` 等 5 处 | ✅ 86 行 | 逐节点的类型合法化决策 |
| `dagcombine` | `DAGCombiner.cpp:88` | ✅ 48 行 | `Combining:` / `... into:` |
| `riscv-opt-w-instrs` | `RISCVOptWInstrs.cpp:45` | ✅ 2 行 | `Replacing ... with ...`（`:771,870,881`） |
| `riscv-vl-optimizer` | `RISCVVLOptimizer.cpp` | ✅ 需向量输入 | `Found a candidate for VL reduction:`（`:1089`） |
| `riscv-insert-vsetvli` | `RISCVInsertVSETVLI.cpp` | ✅ 3 行 | `vsetvli` 插入决策 |
| `regalloc` / `machine-scheduler` | `RegAllocGreedy.cpp:81` / `ScheduleDAGInstrs.cpp` | ✅ 260 / 365 行 | 分配与 spill、调度队列 |
| `riscv-vector-peephole` | `RISCVVectorPeephole.cpp:40` | ❌ 0 行 | 有 `DEBUG_TYPE`，全文件 **0 个** `LLVM_DEBUG` |
| `asm-printer` | `RISCVAsmPrinter.cpp` | ❌ 0 行 | 同上：0 个 `LLVM_DEBUG` |
| `global-isel` | — | ❌ 不存在 | `grep -rn 'DEBUG_TYPE "global-isel"' llvm/lib` 无结果 |

`-debug-only=isel` 在 `store.ll` 上输出 334 行；下面这段来自 `mask.ll`（DAG dump 形状一致）：

```text
	FastISel is disabled
=== mask

Initial selection DAG: %bb.0 'mask:'
SelectionDAG has 10 nodes:
          t2: i64,ch = CopyFromReg t0, Register:i64 %0
        t3: i32 = truncate t2
      t5: i32 = and t3, Constant:i32<65535>
```

它能让你确认 **legalization 之后 DAG 长什么样**，而不是猜。`riscv-isel` 在普通标量代码上一行不出（`store.ll`、`sh1.ll` 实测均 0 行），稳定触发点可伸缩向量的 `splat`：

```bash
cat > /tmp/ch28/splat2.ll <<'EOF'
define <vscale x 4 x i32> @splat_s(i32 %x) {
  %s = insertelement <vscale x 4 x i32> poison, i32 %x, i32 0
  %r = shufflevector <vscale x 4 x i32> %s, <vscale x 4 x i32> poison, <vscale x 4 x i32> zeroinitializer
  ret <vscale x 4 x i32> %r
}
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -debug-only=riscv-isel /tmp/ch28/splat2.ll -o /dev/null
```

```text
RISC-V DAG preprocessing replacing:
Old:    t8: nxv4i32 = splat_vector t2

New: t14: nxv4i32 = RISCVISD::VMV_V_X_VL undef:nxv4i32, t2, Register:i64 $x0
```

另一个可复现的短样本（`store.ll`）：

```text
$ llc -mtriple=riscv64 -O2 -debug-only=riscv-opt-w-instrs store.ll
Replacing %3:gpr = ADDW %1:gpr, %2:gpr
     with %3:gpr = ADD %1:gpr, %2:gpr
```

`riscv-vl-optimizer` 打印的是 `Found a candidate for VL reduction: <MI>`；本树对一个 4 元素 `vadd` 实测 3 条，分别对应 `PseudoVADD_VV_M1` 与两条 `PseudoVLE32_V_M1`。

> **判据**：`-debug-only=dagcombine` 有没有 `... into:`，决定改 combine 还是改 pattern（Ch 16）。本树只有 `Combining:` 没有 `into:`，说明 DAG 被看过但没有被改写。

---

## 28.5 `-print-after` / `-print-before` / `-print-after-all` / `-debug-pass=Structure`

pass 名只有三个可靠来源：

1. **`-debug-pass=Structure` 的第一行**（`Pass Arguments:`），带前导 `-`，用于 `-print-after`/`-stop-after` 时要去掉。
2. **`INITIALIZE_PASS` 的第二个参数**：`RISCVOptWInstrs.cpp:87` 的 `INITIALIZE_PASS(RISCVOptWInstrsLegacy, DEBUG_TYPE, ...)`，注册名是 `DEBUG_TYPE`，显示名是 `RISC-V Optimize W Instructions`。
3. **`-run-pass` 的报错**：写错时报 `run-pass <name> is not registered.`，是最快的探测器。

| 命令 | 结果 |
|---|---|
| `-print-after=finalize-isel` | ✅ `# *** IR Dump After Finalize ISel ... (finalize-isel) ***:` |
| `-print-before=prolog-epilog` | ✅ `# *** IR Dump Before Prologue/Epilogue Insertion ...` |
| `-print-after=riscv-opt-w-instrs` | ✅ 打印该 pass 后的 MIR |
| `-print-after=dagcombine` / `=ADDW` | ❌ 静默，**rc=0**，0 行（都不是 pass 名） |
| `-stop-after=dagcombine` | ❌ `LLVM ERROR: "dagcombine" pass is not registered.`，**rc=1** |
| `-print-after-all` | ✅ 一次 115 个 dump（实测 `store.ll`） |

**这个不对称是本章最贵的坑**：`-print-after` 拼错不报错，你会以为“这个 pass 没跑”；`-stop-after` 拼错才报警。先 `-stop-after` 验证名字，再拿去 `-print-after` 二分。

`-print-after=dagcombine` 无效的根因：DAG combine 不是 pass，跑在 `SelectionDAGISel` 内部，`DAGCombiner.cpp` 的 `DEBUG_TYPE` 只影响 `-debug-only`。Ch 16 给过同一结论，这里补上退出码证据。

---

## 28.6 `-stats` 与 `-time-passes`

`llc -help-hidden` 里 `--stats` 的描述是 `Enable statistics output from program (available with Asserts)`；本构建满足条件（本机无 release 构建可比，故以选项描述与 `--version` 字符串为据）。

```text
$ llc -mtriple=riscv64 -O2 -stats store.ll -o /dev/null 2>&1 \
    | grep -E 'asm-printer|dagcombine| 1 isel|128 isel|riscv-opt-w-instrs'
  3 asm-printer        - Number of machine instrs printed
  1 dagcombine         - Number of dag nodes combined
  1 isel               - Number of blocks selected using DAG
128 isel               - Number of times dag isel has to try another path
  1 isel               - Number of entry blocks encountered
  1 riscv-opt-w-instrs - Number of instructions transformed to non-W-ops
```

两个数字值得记住：`riscv-opt-w-instrs - Number of instructions transformed to non-W-ops` 在 `store.ll` 上是 `1`（就是那次 `ADDW→ADD`）；`isel - Number of times dag isel has to try another path` 是 `128`，TableGen matcher 回退很多次，通常是 pattern 缺失的信号。

`-time-passes` 给时间分布而非次数（数值随机器负载浮动）：

```text
$ llc -mtriple=riscv64 -O2 -time-passes store.ll -o /dev/null 2>&1 | grep -A4 -- '---User Time---'
   ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
   0.0005 ( 21.8%)   0.0002 ( 37.0%)   0.0006 ( 24.3%)   0.0006 ( 24.4%)  RISC-V DAG->DAG Pattern Instruction Selection
   0.0002 (  7.4%)   0.0000 (  0.0%)   0.0002 (  6.2%)   0.0002 (  6.2%)  RISC-V Assembly Printer
```

内部还有细分的 `Instruction Selection` / `DAG Combining 1`（调“ISel 慢”看那一段）。

---

## 28.7 `-opt-bisect-limit`：二分到第 25 个 pass

`-opt-bisect-limit=N` 让前 N 个可选 pass 执行、其余跳过，退出时用 `BISECT:` 行说明每个 pass 跑没跑。它同时作用于 IR pass 与机器 pass，编号就是调度顺序。

```bash
for N in 10 24 25 51; do
  echo "== N=$N =="
  $T/bin/llc -mtriple=riscv64 -O2 -opt-bisect-limit=$N /tmp/ch28/store.ll -o - 2>/dev/null \
    | sed -n '/^f:/,/func_end/p' | grep -E 'add|sw'
done
```

| N | `f` 的汇编 |
|---|---|
| 10 | `addw a1, a1, a2` / `sw a1, 0(a0)` |
| 24 | `addw a1, a1, a2` / `sw a1, 0(a0)` |
| 25 | `add a1, a1, a2` / `sw a1, 0(a0)` |
| 51 | `add a1, a1, a2` / `sw a1, 0(a0)` |

翻转点是 24→25。确认第 25 个是谁：

```bash
$T/bin/llc -mtriple=riscv64 -O2 -opt-bisect-limit=25 /tmp/ch28/store.ll -o /dev/null 2>&1 \
  | grep 'pass (2[45])'
```

```text
BISECT: running pass (24) dead-mi-elimination on function (f)
BISECT: running pass (25) riscv-opt-w-instrs on function (f)
```

注意事项：

- **编号不等于“前 N 个 pass 都执行了”**。实测 `N=10` 时 `riscv-isel` 显示 `NOT running`，汇编照样生成——ISel、AsmPrinter 这类必需 pass 不受 bisect 控制，所以 `N=10` 与 `N=24` 输出相同。**翻转点只说明“某个 pass 改变了行为”**，不说明它错了；流程是 bisect 缩小 → `-print-after=<该 pass>` 看差异 → 读源码。
- 文档 `llvm/docs/OptBisect.md`；`-opt-bisect=1-N` 等价，还支持区间 `1-10,20-30,45`。

---

## 28.8 `llvm-reduce`：11 个函数 → 1 个

`llvm-reduce` 的输入是“仍有问题的东西 + 判定脚本”；脚本返回 0 表示仍复现。

### 28.8.1 构造一个真的会崩的 IR

```bash
printf 'define <vscale x 4 x i32> @vp(<vscale x 4 x i32> %%a, <vscale x 4 x i32> %%b) {\n  %%r = add <vscale x 4 x i32> %%a, %%b\n  ret <vscale x 4 x i32> %%r\n}\n' > /tmp/ch28/crash.ll
$T/bin/llc -mtriple=riscv64 /tmp/ch28/crash.ll -o /dev/null 2>&1 | head -1
```

```text
LLVM ERROR: Don't know how to legalize this scalable vector type
```

这是“没开 `+v` 却用了可伸缩向量”的真实崩溃路径（fatal error，不是断言），适合作减缩靶子。

### 28.8.2 判定脚本与完整流程

先造一个 22 行、含 20 个无关函数和一个 `@vp` 的输入（一条 `define` 写一行，IR parser 接受同一行多条指令）：

```bash
{ echo 'target triple = "riscv64-unknown-linux-gnu"'
  for i in $(seq 1 20); do
    echo "define i32 @f$i(i32 %a) { %s = mul i32 %a, $i  %t = add i32 %s, 1  ret i32 %t }"
  done
  echo 'define <vscale x 4 x i32> @vp(<vscale x 4 x i32> %a) { %r = add <vscale x 4 x i32> %a, %a  ret <vscale x 4 x i32> %r }'
} > /tmp/ch28/bigcrash.ll
```

```bash
cat > /tmp/ch28/interesting.sh <<'EOF'
#!/bin/sh
/home/ubuntu/llvm/builds/ct-instr-trunk/bin/llc -mtriple=riscv64 "$1" -o /dev/null 2>&1 \
  | grep -q 'scalable vector type'
EOF
chmod +x /tmp/ch28/interesting.sh
/tmp/ch28/interesting.sh /tmp/ch28/bigcrash.ll && echo "interesting: yes"
$T/bin/llvm-reduce --test=/tmp/ch28/interesting.sh /tmp/ch28/bigcrash.ll -o /tmp/ch28/reduced.ll
```

`bigcrash.ll` 22 行；reducer 逐轮打印 `*** Reducing ...`，2.4 s 后 `Done reducing! Reduced testcase: reduced.ll`。缩减结果 5 行、1 个函数，用同一条 `llc` 命令跑仍是同一句 `LLVM ERROR`：

```llvm
target triple = "riscv64-unknown-linux-gnu"

define <vscale x 4 x i32> @vp(<vscale x 4 x i32> %a) {
  ret <vscale x 4 x i32> %a
}
```

**“缩减后仍能复现”是硬要求**：脚本用 `grep -q` 只看关键行；若它匹配到的是别的诊断，缩减会跑偏。

### 28.8.3 保属性缩减：让“汇编形状”成为判定条件

维护中更常见的是“不崩，但汇编不对”。造 10 个无关函数 + 1 个会选出 `ADDW` 的函数，把判定条件换成对汇编的检查：

```bash
{ echo 'target triple = "riscv64-unknown-linux-gnu"'
  for i in $(seq 1 10); do
    echo "define i64 @j$i(i64 %x) { %m = mul i64 %x, $i  %n = xor i64 %m, 17  ret i64 %n }"
  done
  echo 'define i64 @a(i64 %x, i64 %y) { %s = add i64 %x, %y  %t = trunc i64 %s to i32  %z = zext i32 %t to i64  ret i64 %z }'
} > /tmp/ch28/prop.ll
```

```bash
cat > /tmp/ch28/prop.sh <<'EOF'
#!/bin/sh
/home/ubuntu/llvm/builds/ct-instr-trunk/bin/llc -mtriple=riscv64 -O2 \
  -stop-after=finalize-isel "$1" -o - 2>/dev/null | grep -q 'ADDW'
EOF
chmod +x /tmp/ch28/prop.sh
$T/bin/llvm-reduce --test=/tmp/ch28/prop.sh /tmp/ch28/prop.ll -o /tmp/ch28/prop-reduced.ll
```

实测 11 个函数 → 1 个（3.9 s），条件仍成立（`%2:gpr = ADDW %0, %1`）。注意 reducer 顺手把 `trunc`+`zext` 改成了 `and i64 %s, 1`——语义等价但**形状已变**；写回归测试时用缩减后的 IR，不要用原 IR 去解释新形状。

### 28.8.4 `--delta-passes` 的取舍

```text
$ llvm-reduce --print-delta-passes | head -4
Delta passes (pass to `--delta-passes=` as a comma separated list):
 IR:
  strip-debug-info
  functions
```

- 默认跑全部 delta pass：`prop.ll` 用 3.9 s，11 个函数 → 1 个。
- `--delta-passes=instructions`：同输入 0.5 s，11 个函数壳全留下（行数反而涨到 50，因为打印会展开成多行）——**行数不是好指标，函数数才是**。
- 本树**没有** `--delta-elements`（`llvm-reduce --help | grep -c delta-elements` 实测 `0`）；粒度控制用 `--starting-granularity-level` 与 `--max-pass-iterations`。

取舍：**先快后全**。窄 delta pass 快速看出“哪类结构必要”，再用全集收敛成可提交的最小用例。

---

## 28.9 崩溃复现器：`llc` 没有 `-gen-reproducer`

```text
$ llc -help-hidden 2>&1 | grep -i reproducer
  --constraint-elimination-dump-reproducers   - Dump IR to reproduce successful transformations
```

**结论：本构建的 `llc` 没有 `-gen-reproducer`**；唯一带 “reproducer” 的选项是 GISel 的约束消除调试开关，与崩溃复现无关。本项标 **未实测**，原因是工具不存在。

`clang` 有等价物，可演示 reproducer 长什么样：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64 -O2 -c -gen-reproducer=always \
  -fcrash-diagnostics-dir=/tmp/ch28/repro /tmp/ch28/t.c -o /tmp/ch28/repro/t.o
# clang: note: diagnostic msg: /tmp/ch28/repro/t-<hash>.sh
```

生成的 `.sh` 含完整 `-cc1` 参数（几百个 `-target-feature`）与预处理后的 `.c`，换机器也能重跑。`llc` 场景要手工拼三件套：① 最小 IR（`llvm-reduce` 产出）；② 完整命令行（**`-mattr` 必须写全**）；③ 期望 vs 实际（`LLVM ERROR` 原文，或 `diff -u expected.s actual.s`）。栈回溯里的 `Running pass '...' on function '@...'` 就是分层入口。

---

## 28.10 源码落点

| 位置 | 读什么 | 为什么 |
|---|---|---|
| `llvm/lib/Support/Debug.h:37` | `#ifndef NDEBUG` | 解释为什么 release 构建里 `-debug-only` 全哑 |
| `llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:118,960,980` | `DEBUG_TYPE "isel"`、DAG dump | ISel 日志开关与两个 dump 点 |
| `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp:29,34,196-204` | `DEBUG_TYPE "riscv-isel"`、`PreprocessISelDAG` | 解释为什么标量输入 0 行 |
| `llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp:88,1923` | `DEBUG_TYPE "dagcombine"`、`Combining:` | `-print-after=dagcombine` 无效的根因 |
| `llvm/lib/Target/RISCV/RISCVOptWInstrs.cpp:45,87,374,663,771` | 注册名、`hasAllWUsers`、`Replacing` | `ADDW→ADD` 的完整判据链 |
| `llvm/lib/Target/RISCV/RISCVVectorPeephole.cpp:40,99` | `DEBUG_TYPE` + `INITIALIZE_PASS`，全文件 0 个 `LLVM_DEBUG` | 为什么该类型无输出 |
| `llvm/lib/Target/RISCV/RISCVVLOptimizer.cpp:173-176,1089` | `INITIALIZE_PASS_BEGIN/END`、候选日志 | 带 analysis 依赖的 pass 怎么注册 |
| `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:265-272` | `Predicates = [HasStdExtZbbOrZbkb]` + `ANDN` | 汇编器拒绝 `andn` 时改哪里 |
| `llvm/lib/Target/RISCV/RISCVInstrInfoV.td:1103` | `Predicates = [HasVInstructions]` | `vadd.vv` 的 feature 门槛 |

---

## 28.11 可跑实验

以下 7 个实验均在本树实测通过。假设 `/tmp/ch28` 已按 28.3.1 创建且 `T` 已设置：

```bash
source /home/ubuntu/llvm/toolset/env.sh; T=/home/ubuntu/llvm/builds/ct-instr-trunk; mkdir -p /tmp/ch28
```

### 实验 1：四层快照抓 `ADDW`

```bash
for p in finalize-isel riscv-opt-w-instrs prolog-epilog; do
  $T/bin/llc -mtriple=riscv64 -O2 -stop-after=$p /tmp/ch28/store.ll -o - | grep -E 'ADDW|ADD |SW '
done
```

期望：`finalize-isel` 见 `ADDW`；从 `riscv-opt-w-instrs` 起变成 `ADD`。

### 实验 2：`-debug-only` 校准

```bash
$T/bin/llc -mtriple=riscv64 -O2 -debug-only=riscv-opt-w-instrs /tmp/ch28/store.ll -o /dev/null
$T/bin/llc -mtriple=riscv64 -O2 -debug-only=no-such-type /tmp/ch28/store.ll -o /dev/null; echo "rc=$?"
```

期望：第一条两行 `Replacing ...`；第二条 `rc=0` 无输出——拼错类型不报错。

### 实验 3：lit 失败 → update 脚本 → 通过

```bash
mkdir -p /tmp/ch28/lit && cat > /tmp/ch28/lit/lit.cfg <<'EOF'
import os, lit.formats
config.name = 'ch28'
config.test_format = lit.formats.ShTest(False)
config.suffixes = ['.ll']
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, 'Output')
EOF
cat > /tmp/ch28/lit/store.ll <<'EOF'
; RUN: llc < %s -mtriple=riscv64 -O2 | FileCheck %s

define void @f(ptr %p, i64 %a, i64 %b) {
; CHECK-LABEL: f:
; CHECK:       addw a1, a1, a2
  %r = add i64 %a, %b
  %t = trunc i64 %r to i32
  store i32 %t, ptr %p
  ret void
}
EOF
PATH="$T/bin:$PATH" $T/bin/llvm-lit -v /tmp/ch28/lit/store.ll | tail -3
python3 /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/utils/update_llc_test_checks.py \
  --llc-binary $T/bin/llc /tmp/ch28/lit/store.ll
PATH="$T/bin:$PATH" $T/bin/llvm-lit -v /tmp/ch28/lit/store.ll | tail -2
```

期望：先 `Failed: 1 (100.00%)`，错误是 `CHECK: expected string not found in input`，并提示 `possible intended match here: add a1, a1, a2`；脚本把 `addw` 改成 `add` 并补 `# %bb.0:` 与 `ret`；最后 `Passed: 1`。

> 更新脚本只重写**函数体内部**的 inline check。把 `CHECK-LABEL` 放到文件末尾，脚本会另插一组 inline check，旧块原样保留，测试继续失败（实测）。

### 实验 4：`llvm-mca` 看调度质量

```bash
printf '.text\nloop:\n  ld a0, 0(a1)\n  addi a1, a1, 8\n  add a2, a2, a0\n  bne a1, a3, loop\n' > /tmp/ch28/loop.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -iterations=100 /tmp/ch28/loop.s | head -9
```

期望：`Total Cycles: 302`、`IPC: 1.32`、`Block RThroughput: 2.0`。再配 `-print-after=machine-scheduler` 看调度后的顺序。

### 实验 5：汇编器拒绝 → 找 predicate

```bash
printf 'andn a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
printf 'andn a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zbb -show-encoding
printf 'vadd.vv v8, v9, v10\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding 2>&1 | head -1
```

期望：`error: instruction requires the following: 'Zbb' ...`；`# encoding: [0x33,0xf5,0xc5,0x40]`；`error: instruction requires the following: 'V' ...`。然后看 `RISCVInstrInfoZb.td:265`。

### 实验 6：重定位与链接错误

```bash
printf 'declare i32 @ext(i32)\ndefine i32 @caller(i32 %%x) {\n  %%r = call i32 @ext(i32 %%x)\n  %%s = add i32 %%r, 1\n  ret i32 %%s\n}\n' > /tmp/ch28/call.ll
$T/bin/llc -mtriple=riscv64 -mattr=+c -relocation-model=pic -filetype=obj /tmp/ch28/call.ll -o /tmp/ch28/call.o
$T/bin/llvm-readobj -r /tmp/ch28/call.o | head -10
$T/bin/ld.lld -o /tmp/ch28/call.elf /tmp/ch28/call.o 2>&1 | head -3
```

期望：`0x4 R_RISCV_CALL_PLT ext`；链接报 `ld.lld: error: undefined symbol: ext` 并指出 `call.o:(caller)`。

### 实验 7：qemu 跑最小 C，`-O0`/`-O2` 对照

```bash
cat > /tmp/ch28/alias2.c <<'EOF'
__attribute__((noinline)) int f(int *p, float *q) { *p = 1; *q = 2.0f; return *p; }
void _start(void) {
  int x; register long a0 asm("a0") = f(&x, (float *)&x); register long a7 asm("a7") = 93;
  asm volatile("ecall" : "+r"(a0) : "r"(a7) : "memory");
}
EOF
for O in 0 2; do
  $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64 -nostdlib -static \
    -O$O -fuse-ld=lld /tmp/ch28/alias2.c -o /tmp/ch28/alias2-$O.elf
  /usr/bin/qemu-riscv64-static /tmp/ch28/alias2-$O.elf; echo "O$O exit=$?"
done
```

期望：`O0 exit=0`、`O2 exit=1`。不是 RISC-V 后端 bug，是 strict aliasing 的 UB：`-O2` 把 `*p` 的读 store-forward 成 `li a0, 1`。RVV 的 SIGILL 见 28.12 第 8 条。

---

## 28.12 踩雷集锦

1. **`-debug-only` 只对 assertions 构建有效。** `LLVM_DEBUG` 在 `NDEBUG` 下整体编译掉（`Debug.h:37`）；release 构建里它安静得像“功能没跑”，其实是“代码不存在”。
2. **`-print-after` 拼错静默，`-stop-after` 拼错致命。** 实测前者 rc=0、0 行；后者 `LLVM ERROR: "dagcombine" pass is not registered.`、rc=1。先用后者验证名字。
3. **pass 名 ≠ DAG combine 名 ≠ opcode。** `dagcombine` 是 `DEBUG_TYPE` 不是 pass；`ADDW` 是 opcode 不是 pass。`-print-after=ADDW` 实测 0 行。
4. **`-stats` 走 stderr，按 pass 名字母序。** `2>/dev/null` 会丢掉整段统计；`| head` 只留前几行，要找的 pass 常在截断区外。用 `grep <pass名>`。
5. **`-opt-bisect-limit` 的编号不等于“每个 pass 都被跳过”。** 必需 pass（ISel、AsmPrinter）照跑，所以 `N=10` 与 `N=24` 输出可以完全相同（实测）。
6. **`llvm-reduce` 不会凭空补 `target triple`。** 本树纠正半个流行说法：带 `target triple`/`datalayout` 的输入，缩减后两者都保留（实测 3 次）；不带 triple 的手写 IR，缩减后也不会多出来。复现脚本必须自带 `-mtriple`。
7. **缩减后不再崩，通常是判定脚本太宽。** 用 `grep -q 'scalable vector type'` 这类关键行，而不是 `llc ...; test $? -ne 0`——后者会被任何非零退出满足，包括用法错误。改完脚本先跑“应该 interesting”与“应该 not interesting”各一个文件。
8. **`-O0`/`-O2` 行为不同，先怀疑 UB；qemu SIGILL 也不等于编译器 bug。** 实测 `alias2.c` 退出码 0 vs 1（`-O2` 汇编合法），而 `-march=rv64gcv -mrvv-vector-bits=128` 编出的 `vadd.vv` 在 qemu 6.2 下 `Illegal instruction`（exit 132，6.2 实现的是 RVV 0.7）。先排除 C 代码 UB 与 qemu 版本，再开 issue。

---

## 28.13 本章重点整理

- 核心不是“读更多代码”，而是**先钉层**（legalization / ISel / 机器 pass / MC / 链接），再用四层快照法找差异首次出现处：`finalize-isel` → 目标 pass → `prolog-epilog` → 最终汇编，一次只 diff 相邻两层。
- `-debug-only` 匹配 `DEBUG_TYPE`：`isel`、`riscv-isel`、`legalize-types`、`dagcombine`、`riscv-opt-w-instrs`、`riscv-vl-optimizer` 可用；`riscv-vector-peephole` 有 `DEBUG_TYPE` 但无日志；`global-isel` 本树不存在。`-print-after` 拼错静默、`-stop-after` 拼错致命，pass 名只从 `-debug-pass=Structure`、`INITIALIZE_PASS`、`-run-pass` 报错三处取。
- `-stats` 给次数（`riscv-opt-w-instrs: 1`、`isel` 回退 128 次），`-time-passes` 给时间；`-opt-bisect-limit` 把“哪一变”精确到第 25 个 pass，但判断对错仍需读源码或对照测试。
- `llvm-reduce` 两种用法：崩溃减缩（22 行 → 5 行）与保属性减缩（11 → 1 个函数，仍产出 `ADDW`），判定脚本必须窄且可验证。
- `llc` 没有 `-gen-reproducer`；等价物是最小 IR + 完整命令行 + 期望/实际三件套。

---

## 28.14 自我检核

- [ ] 能不看笔记说出七种症状各自的第一动作，并解释为什么先钉层再读源码
- [ ] 能用四个 `-stop-after` 快照定位“某条指令在哪一层消失/出现”
- [ ] 能解释 `ADDW→ADD` 是 `hasAllWUsers` 的合法优化，而不是 ISel 选错
- [ ] 能列出本树 5 个以上可用的 `-debug-only` 类型，并说明 `riscv-vector-peephole` 为什么无输出；能解释 `-print-after=dagcombine` 为什么静默、`-stop-after=dagcombine` 为什么 rc=1
- [ ] 能用 `-opt-bisect-limit` 扫出翻转点，指出该编号对应的 pass
- [ ] 能写返回 0/1 的 interesting 脚本，把 11 个函数的 IR 缩到 1 个函数且仍复现；能说出 `--delta-passes` 的取舍与本树没有 `--delta-elements`
- [ ] 遇到 qemu SIGILL 或 `-O0`/`-O2` 差异时，先排除环境与 UB，再怀疑后端

---

## 28.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/OptBisect.md`**：1–30 行讲清 `-opt-bisect-limit` 的语义与区间写法；照着它对 28.7 的扫描做一遍。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/HowToSubmitABug.md`**：108–124 行是 codegen bug 的减缩流程，要求“先写能复现的脚本，再 `llvm-reduce --test=`”。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/CommandGuide/llvm-reduce.md`**：全部 delta pass 与 `--test` 契约；配合 `--print-delta-passes` 对照。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVOptWInstrs.cpp`**：119、374、663、771 行，`ADDW→ADD` 的判据与日志就在这条链上。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/utils/update_llc_test_checks.py`**：`--llc-binary` 与 inline check 的生成规则；实验 3 的行为由它决定。

---

→ 下一章：[Ch 29 — 上游贡献流程](./29-contribution-workflow.md)
