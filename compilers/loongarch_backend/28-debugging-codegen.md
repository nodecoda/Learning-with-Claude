# Ch 28 — 调试 CodeGen：停 pass、跑单 pass、打印 MIR 与手写 MIR

> 本章目标：拿到一个“LoongArch 生成错了”的 bug 时，能在 5 分钟内定位到 pass；能用 `-run-pass` 在 MIR 上做单步实验；知道手写 MIR 的四个坑。

---

## 28.1 先选工具

| 现象 | 第一条命令 |
|---|---|
| 某条指令选错了 | `llc -debug-only=isel,legalize-types,dagcombine` |
| 怀疑某个机器 pass 改坏了 | `-stop-before/-stop-after=<pass>` 对比前后 MIR |
| 只想跑一个 pass | `-run-pass=<pass>` + 手写/截取的 `.mir` |
| 想知道某个 pass 改了多少 | `-stats`（需要 assertions 构建） |
| 想看 pass 顺序 | `-debug-pass=Structure` |
| 想验证寄存器/指令合法性 | `-verify-machineinstrs` |

本课构建是 `+assertions`（`clang --version` 的 `Build config: +assertions`），所以 `-stats` 可用。

---

## 28.2 真实 pass 名字（不要猜）

`-debug-pass=Structure` 在 LA64 `-O2` 下打印出的 LoongArch 专属 pass：

```text
loongarch-isel
loongarch-opt-w-instrs
loongarch-prera-expand-pseudo
loongarch-merge-base-offset
loongarch-dead-defs
loongarch-late-branch-opt
loongarch-expand-pseudo
loongarch-expand-atomic-pseudo
loongarch-memory-barrier-opt
```

对应的类名/文件：

| pass 名 | 文件 |
|---|---|
| `loongarch-isel` | `LoongArchISelDAGToDAG.cpp` |
| `loongarch-opt-w-instrs` | `LoongArchOptWInstrs.cpp` |
| `loongarch-prera-expand-pseudo` | `LoongArchPreRAExpandPseudo.cpp` |
| `loongarch-merge-base-offset` | `LoongArchMergeBaseOffset.cpp` |
| `loongarch-dead-defs` | `LoongArchDeadRegisterDefinitions.cpp` |
| `loongarch-late-branch-opt` | `LoongArchLateBranchOpt.cpp` |
| `loongarch-expand-pseudo` | `LoongArchExpandPseudoInsts.cpp` |
| `loongarch-expand-atomic-pseudo` | `LoongArchExpandAtomicPseudoInsts.cpp` |
| `loongarch-memory-barrier-opt` | `LoongArchMemoryBarrierOpt.cpp` |

顺序注意：**两个 expand-pseudo 不是同一个**。`prera` 在寄存器分配前，`expand-pseudo` 在栈帧布局之后（Ch 18 讲过这条分界）。

---

## 28.3 停 pass：`-stop-before` / `-stop-after`

```text
$ llc -mtriple=loongarch64 -O2 -stop-after=loongarch-isel t2.ll -o -
--- |
  ; ModuleID = 't2.ll'
  target triple = "loongarch64"
  ...
  name:            bittest_7_i32
  body:             |
    bb.0.entry:
      liveins: $r4
      %0:gpr = COPY $r4
      %1:gpr = ANDI ...

```

实用技巧：

- **`-stop-after=<pass>` 输出的东西是合法 MIR**，可以直接喂回 `-run-pass`。
- 名字写错会报 `unknown pass name`，并列出候选；比读源码快。
- 想比较“某 pass 前后”，分别停在该 pass 的**前一个**和**自己**，然后 `diff`。

```bash
llc -mtriple=loongarch64 -O2 -stop-after=shrink-wrap        f.ll -o a.mir
llc -mtriple=loongarch64 -O2 -stop-after=loongarch-late-branch-opt f.ll -o b.mir
diff a.mir b.mir
```

---

## 28.4 跑单 pass：`-run-pass`

`-run-pass` 只跑指定的机器 pass，输入必须是 MIR。实测一个能真正触发 `LoongArchLateBranchOpt` 的最小例子：

```yaml
--- |
  define void @f(i64 %a) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4
    BEQ $r4, $r4, %bb.2
    PseudoBR %bb.1

  bb.1:
    PseudoRET

  bb.2:
    PseudoRET
```

```text
$ llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt lb2.mir -o -
  bb.0:
    successors: %bb.2(0x80000000)
    liveins: $r4
    PseudoBR %bb.2
```

`BEQ $r4, $r4` + `PseudoBR` 被折成单条 `PseudoBR %bb.2`，successor 也只剩下 `%bb.2`。

`BEQZ $r0, %bb.2` 得到同样结果。**但 `BEQ $r4, $r0` 不会变**：

```text
    BEQ $r4, $r0, %bb.2      # 原样保留
    PseudoBR %bb.1
```

因为 pass 的判定条件（`LoongArchLateBranchOpt.cpp:63-78`）是：

```cpp
case LoongArch::BEQZ:
case LoongArch::BNEZ:
  if (Cond.size() < 2 || !Cond[1].isReg() || Cond[1].getReg() != LoongArch::R0)
    return false;                        // 只认 "跟 $r0 比较"
  Folded = (Opc == LoongArch::BEQZ) ? TBB : FBB;
  break;
case LoongArch::BEQ:
case LoongArch::BNE:
  if (... || Cond[1].getReg() != Cond[2].getReg())
    return false;                        // 只认 "同一个寄存器跟自己比"
  Folded = (Opc == LoongArch::BEQ) ? TBB : FBB;
```

一句话：它折叠的是**静态可知真/假**的比较，不是普通的 `x == 0`。`x == 0` 在更早的 MachineCombiner 里已经被换成 `BEQZ`。

---

## 28.5 `-print-after`：不改流程地看 MIR

```text
$ llc -mtriple=loongarch64 -O2 -print-after=loongarch-opt-w-instrs if.ll -o /dev/null

# *** IR Dump After LoongArch Optimize W Instructions (loongarch-opt-w-instrs) ***:
# Machine code for function sel: IsSSA, TracksLiveness
Function Live Ins: $r4 in %0, $r5 in %1, $r6 in %2

bb.0.entry:
  successors: %bb.1(0x30000000), %bb.2(0x50000000)
  liveins: $r4, $r5, $r6
  %2:gpr = COPY $r6
  %1:gpr = COPY $r5
  %0:gpr = COPY $r4
  %5:gpr = ADDI_W %0:gpr, 0
  BNEZ %5:gpr, %bb.2
  PseudoBR %bb.1
```

对比 `-stop-after`：`-print-after` 只是**打印**，编译继续；适合一次看多个 pass（可重复传参），不适合当输入。

---

## 28.6 `-stats`：量化某个 pass 干了多少

```text
$ llc -mtriple=loongarch64 -O2 -stats -o /dev/null bittest.ll | grep -E 'isel|dagcombine'
380 dagcombine          - Number of dag nodes combined
225 isel                - Number of blocks selected using DAG
708 isel                - Number of times dag isel has to try another path
119 isel                - Number of entry blocks encountered
 53 loongarch-isel-lowering - Number of tail calls
```

读法：

- `708 次 try another path` 相对 `225 个块` 说明模式匹配回退很频繁——通常是 `ComplexPattern` 没匹配上，值得看 `-debug-only=isel`。
- `-stats` 是**全局累计**，不是单函数；要按函数看就加 `-filter-print-funcs=<name>` 或拆小输入。

---

## 28.7 `-debug-only`：SelectionDAG 的放大镜

```text
$ llc -mtriple=loongarch64 -O2 -debug-only=isel -o /dev/null if.ll
	FastISel is disabled

*** sel
Initial selection DAG: %bb.0 'sel:entry'
SelectionDAG has 24 nodes:
  t0: ch,glue = EntryToken
  t4: i64,ch = CopyFromReg t0, Register:i64 %1
```

可用的名字（本树实测有效）：`isel`、`legalize-types`、`dagcombine`。它们对应 `LLVM_DEBUG` 的标签，不是 pass 名。

**必须 assertions 构建**；release 构建里 `-debug-only` 是空操作。

---

## 28.8 手写 MIR 的四个坑

实测报错（全部真实）：

| 坑 | 实测错误 |
|---|---|
| 用 `$zero` | `error: bad1.mir:10:10: unknown register name 'zero'` |
| 用 `RET` | `error: b2.mir:11:5: unknown machine instruction name 'RET'` |
| 用 `ret`（小写汇编名） | 同上，MIR 里用大写 opcode |
| 删掉 MIR 段的 `name:` | `error: YAML:5:1: missing required key 'name'` |

正确写法：

1. **物理寄存器用数字名**：`$r0`（不是 `$zero`）、`$r4`、`$sp`、`$ra`、`$tp`。
2. **伪指令用大写**：`PseudoRET`、`PseudoBR`、`PseudoLA_PCREL`。
3. **最稳的生成方式**：先 `llc -stop-after=<pass>` 生成真实 MIR，再改。
4. **IR 段其实可以省略**：本树实测，去掉 `--- |` 段后 `-run-pass` 仍能工作（parser 会合成空 IR）；但 `-stop-after` 生成的 MIR 带 IR 段，保留它才能让 `%ir.a` 注释和类型信息可读。

```bash
llc -mtriple=loongarch64 -O2 -stop-after=loongarch-prera-expand-pseudo f.ll -o f.mir
# 编辑 f.mir 后：
llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt f.mir -o -
```

---

## 28.9 一个完整的定位流程

以“`PseudoBR` 没被删掉”为例：

1. `-debug-pass=Structure` 确认 `loongarch-late-branch-opt` 在 `shrink-wrap` 之后、`machine-latecleanup` 之前。
2. `-stop-after=shrink-wrap` 拿输入 MIR，`-stop-after=loongarch-late-branch-opt` 拿输出 MIR。
3. `diff` 两份 MIR，确认 pass 到底有没有跑、有没有改。
4. 没改 → 读 `LoongArchLateBranchOpt.cpp:50-105` 的判定条件，对照输入里的 opcode/操作数。
5. 手工构造最小 MIR（28.4），确认“应该改”的输入能改。
6. 回到真实 IR，`-print-after=loongarch-isel` 看 `BEQZ` 是不是在更早的 pass 就被换掉了。

---

## 28.10 反直觉点

1. **`-run-pass` 需要 MIR，不吃 `.ll`**；`-stop-after` 的输出正好可以喂回去。
2. **MIR 里没有 `$zero`**，只有 `$r0`。
3. **MIR 的 opcode 是伪指令名**（`PseudoRET`），不是最终汇编名（`ret`）。
4. **`-print-after` 不改变编译流程**，`-stop-after` 会。
5. **`-debug-only` 在 release 构建里静默失效**。
6. **`-stats` 是全局累计**，不是 per-function。
7. **`LateBranchOpt` 不折叠 `x == 0`**，只折叠静态真/假比较。

---

## 28.11 本章证据

| 结论 | 命令/源码 |
|---|---|
| 9 个 LoongArch pass 名 | `-debug-pass=Structure` 实测 |
| pass 顺序 | 同上（`shrink-wrap` → `late-branch-opt` → `machine-latecleanup`） |
| `-stop-after` 输出 MIR | 28.3 实测 |
| `BEQ r4,r4` 折叠 | 28.4 实测 |
| `BEQ r4,r0` 不折叠 | 28.4 实测 + `LoongArchLateBranchOpt.cpp:63-78` |
| `-print-after` 输出 | 28.5 实测 |
| `-stats` 数字 | 28.6 实测 |
| MIR 四个坑 | 28.8 四条实测报错（含精确文件名:行号） |

---

## 28.12 练习

1. 用 `-stop-after=loongarch-merge-base-offset` 和 `-stop-after=loongarch-prera-expand-pseudo` 对比同一个函数，找出 `MergeBaseOffset` 删掉的指令。
2. 手写一个 MIR，让 `loongarch-dead-defs` 真正删掉一条 `ADD_W`（提示：结果未被使用且没有 side effect）。
3. 用 `-stats` 比较 `-O2` 与 `-O3` 的 `dagcombine` 次数，解释差异。
4. 用 `-debug-only=isel` 找出一条走了 “try another path” 的指令，说明它为什么没匹配上。
5. 写一个 `-verify-machineinstrs` 能抓到的畸形 MIR（例如用了未定义的虚拟寄存器）。

---

## 28.13 延伸阅读

1. **从真实 clang 命令定位 pass** — `llvm/docs/DebuggingLLVM.md`
   - 读什么：`## Extract and rerun the compile command`、`## Understanding the source of the issue`、`## Understanding where an instruction came from`。
   - 学到什么：本章从 `llc` 出发；这份文档补上如何从构建系统抓到真实 clang 命令、用 pass-by-pass 打印定位、以及用 `-print-mi-addrs` 配合调试器回溯到创建 `MachineInstr` 的 C++ 位置。28.9 只走到“读 pass 判定条件”，这里补上“定位到具体构造点”。
   - 前提：读过 28.3–28.7；有 debug/assertions 构建。

2. **MIR 的测试指南与语法** — `llvm/docs/MIRLangRef.md`
   - 读什么：`## MIR Testing Guide` 的 `### Testing Individual Code Generation Passes`、`#### Simplifying MIR files`，以及 `## Machine Instructions Format Reference` 的 `### Registers`、`### Machine Instructions`、`### Machine Operands`。
   - 学到什么：解释 28.8 的四个坑为什么成立（`$r0` 不是 `$zero`、opcode 用大写伪指令名、`name:` 必填），并学会用 `-run-pass` 所需的最小 MIR 写法。这里还补上“先简化真实 MIR，再手写”的方法。
   - 前提：读过 28.3–28.4、28.8。

3. **MIR 背后的对象模型** — `llvm/docs/CodeGenerator.md`
   - 读什么：`## Machine code description classes` 下的 MachineInstr、MachineBasicBlock、MachineFunction 三个类小节，以及 `#### Machine code in SSA form`。
   - 学到什么：读 MIR 时知道 `liveins`、successors、虚拟寄存器、SSA 属性分别对应哪个 C++ 对象；也能解释为什么 `-stop-after` 的输出可以直接作为 `-run-pass` 输入。28.3 只讲了命令，这里补上数据模型。
   - 前提：读过 28.3–28.5；对 `MachineFunction` 有基本印象（Ch 03）。

4. **`-debug-only` 为什么在 release 里失效** — `llvm/docs/CommandLine.md`
   - 读什么：`### Internal vs External Storage` 中 `LLVM_DEBUG` 宏的定义，以及 `DebugFlag` 通过 `cl::location` 绑定的例子；再扫一眼 `### Option Attributes` 里的 `cl::Hidden`。
   - 学到什么：`-debug-only` 的输出依赖 `LLVM_DEBUG`，而该宏在 `NDEBUG` 下被编译成空；这解释了 28.7 的“必须 assertions 构建”。读完能区分“选项存在”和“输出被编译掉”两件事。
   - 前提：读过 28.7；知道 release 与 assertions 构建的区别。

## 28.14 下一步

工具齐了，下一章用它们复盘本树唯一的那个提交：从 issue #239812 到 `lld/ELF/Arch/LoongArch.cpp` 的 24 行修复，以及 commit message / PR 该怎么写。
