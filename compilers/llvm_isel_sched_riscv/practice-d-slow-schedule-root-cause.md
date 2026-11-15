# 练习 D — 给「选对了但排得慢」做根因分析

> **目标**：在不改一行 LLVM 源码的前提下，用 `llc`、`llvm-mca`、`-print-after`、`-stats` 对四个真实症状做根因分析，并写出可复现的诊断报告。重点是**排除法**：每一步只回答「这一层有没有问题」，不跳过层次直接猜结论。
>
> **环境**：LLVM 24.0.0git；`LLC=/home/ubuntu/llvm/builds/explore/bin/llc`；`MCA=/home/ubuntu/llvm/builds/explore/bin/llvm-mca`；`-mtriple=riscv64`；基线 `-mattr=+m`；CPU 至少覆盖 `rocket-rv64`、`sifive-u74`、`spacemit-x60`。本练习的所有数字都来自这套环境，换版本/换 CPU 后绝对值会变，但诊断路径不变。

## 背景与动机

面试里最容易被追问的场景不是「这个 pass 怎么工作」，而是：

> 「这段代码在 A 核上跑得比 B 核快，但你看汇编发现 A 核多了一条指令。为什么？」

或者：

> 「你开了调度，spill 从 0 变成 60。是调度器坏了吗？」

这类问题的共同点是：**最终汇编看起来「不对」，但不对的原因可能在选择、调度、RA、伪指令展开或成本模型中的任意一层**。本练习要求你按第 24 章的诊断树，对四个案例逐一给出「第一次出现差异的层」与「下一步动作」。

## 任务规格

四个案例相互独立，建议按顺序做。每个案例交付一份不超过一页的报告，包含：

1. **症状**：你观察到的汇编/数字。
2. **复现命令**：完整、可直接粘贴。
3. **分层排查**：ISel / pre-RA / post-RA / 模型，各一句结论。
4. **根因**：落在哪一层，为什么。
5. **修复或接受**：改什么参数/源码，或者为什么不该改。

### 案例 A：`ldmul.ll` 的 60 次栈访问

材料：`/tmp/isel_sched_probe/ldmul.ll`（40 个 load + mul 的归约链；若文件不存在，按下面的代码重建）。

```llvm
target triple = "riscv64-unknown-linux-gnu"

define i64 @ldmul(ptr %p, i64 %acc) {
entry:
  %p1 = getelementptr i64, ptr %p, i64 1
  %p2 = getelementptr i64, ptr %p, i64 2
  ; ... 共 40 组 load + mul
  %l0 = load i64, ptr %p
  %m0 = mul i64 %l0, %acc
  %l1 = load i64, ptr %p1
  %m1 = mul i64 %l1, %m0
  ; ...
  ret i64 %m39
}
```

实测基线（`sifive-u74`，`-O2`）：

| 配置 | 栈访问次数 | mca 周期（100 iter） |
|---|---:|---:|
| 默认（bidirectional） | 60 | 1424 |
| `-misched-prera-direction=topdown` | 0 | 704 |
| `-misched-prera-direction=bottomup` | 64 | 1604 |
| `-enable-misched=false` + source order | 84 | 2014 |
| `-enable-misched=false` + `-enable-post-RA-sched=list-ilp` | 40 | 1274 |

问题：

- 第一次出现「60 次栈访问」的 pass 是哪一个？给出命令。
- 为什么 `topdown` 能消掉 spill？这和 `tryCandidate` 的哪一级优先级有关？
- 如果只能改一个命令行参数，你选哪个？如果允许改源码，你会动 `RISCVSubtarget::overrideSchedPolicy` 还是 `tryCandidate`？

### 案例 B：`chain` 在 `spacemit-x60` 上变慢

材料：

```llvm
target triple = "riscv64-unknown-linux-gnu"
define i64 @chain(i64 %a, i64 %b, i64 %c) {
  %s = shl i64 %b, 3
  %x = add i64 %a, %s
  %y = mul i64 %x, %c
  ret i64 %y
}
```

实测：

```text
基线 slli+add+mul：702 cycles / 100 iter
+zba sh3add+mul：  801 cycles / 100 iter
```

问题：

- 用 `-print-after=finalize-isel` 证明选择层没有问题。
- 用 `-instruction-info` 找出 `sh3add` 与 `add`/`slli` 的延迟差异。
- 这是 scheduler 的错吗？如果不是，应该改哪一层的什么？

### 案例 C：`caller` 里的 `PseudoCALL`

材料：

```llvm
declare i64 @callee(i64)
define i64 @caller(i64 %x) {
  %r = call i64 @callee(i64 %x)
  %s = add i64 %r, 1
  ret i64 %s
}
```

问题：

- 用 `-print-after=postmisched` 确认 `PseudoCALL` 仍在。
- 在汇编里找到 `call callee`。
- 解释为什么这不是「伪指令展开失败」，以及 `Sched<[WriteIALU, WriteJalr, ReadJalr]>` 在调度器眼里是什么。

### 案例 D：`vmix` 的六条 `vsetivli`

材料：

```llvm
target triple = "riscv64-unknown-linux-gnu"
define void @vmix(ptr %a, ptr %b, ptr %c) {
  %x = load <4 x i64>, ptr %a, align 32
  %y = load <8 x i32>, ptr %b, align 32
  %z = add <4 x i64> %x, %x
  %w = add <8 x i32> %y, %y
  store <4 x i64> %z, ptr %c, align 32
  %pc = getelementptr i32, ptr %c, i64 16
  store <8 x i32> %w, ptr %pc, align 32
  ret void
}
```

编译：

```bash
$LLC -O2 -mtriple=riscv64 -mattr=+m,+v -riscv-v-vector-bits-min=512 -o - rvv2.ll
```

问题：

- 数出 `vsetivli` 的条数，并写出两种 VTYPE。
- 用 `-print-after=finalize-isel` 证明 ISel 阶段没有 `vsetvli`。
- 用 `-print-after=riscv-insert-vsetvli` 确认插入点。
- 提出一个不改变语义的 IR 重排方案，并预测 `vsetivli` 会降到几条。

## 提供的材料与建议流程

建议把四个案例分别放在 `/tmp/practice-d/{a,b,c,d}/`，每个目录保留：

```text
input.ll          # 你写的或复制的 IR
cmd.txt           # 复现命令
isel.mir          # -print-after=finalize-isel 的输出
sched.mir         # -print-after=machine-scheduler
postra.mir        # -print-after=postmisched
out.s             # 最终汇编
notes.md          # 你的报告
```

统一的环境变量：

```bash
export LLC=/home/ubuntu/llvm/builds/explore/bin/llc
export MCA=/home/ubuntu/llvm/builds/explore/bin/llvm-mca
```

## 报告模板

把下面这段直接复制到 `notes.md`，每格填一句，不要写成长文：

```markdown
# 案例 X 诊断报告

## 1. 症状
（一句话 + 两个数字/汇编片段）

## 2. 复现
```bash
（完整命令，含版本、triple、cpu、mattr）
```

## 3. 分层排查
- ISel（finalize-isel）：
- pre-RA（machine-scheduler）：
- post-RA（postmisched）：
- 模型（mca -instruction-info）：

## 4. 根因
（第一次出现差异的 pass + 机制）

## 5. 修复/接受
（参数或源码位置；副作用）

## 6. 证据
（MIR 片段、stats、mca 表格各一条）
```

要求：

- 每一层都要有「结论」，不能只贴输出。
- 「根因」必须指名 pass，不能写「调度器优化过度」这种模糊表述。
- 「修复」必须写副作用；例如 `topdown` 消 spill 但可能降低 ILP。

## 如何构造最小复现

四个案例的 IR 都可以再缩小。缩小的方法：

1. **保留触发结构**：案例 A 保留「多条独立 load + 一条长归约链」；案例 B 保留「两条指令融合成一条」；案例 C 保留一个 `call`；案例 D 保留两种 SEW。
2. **删掉无关函数**：只留一个 `define`，避免 IR pass 跨函数优化。
3. **固定对齐与属性**：`align 8`、`noalias`、`zeroext` 都会影响选择，缩小前后保持一致。
4. **用 `-stop-after` 冻结阶段**：`-stop-after=finalize-isel` 生成 MIR，之后可以用 `-run-pass` 单独重跑 scheduler，排除 IR pass 干扰。

最小复现的标准：**换一台机器、换一个构建，仍然能复现同一层的差异**。如果只有你的 `/tmp` 文件能复现，说明还有隐藏依赖（环境变量、随机种子、profile）。

## 判定标准

| 维度 | 合格 | 优秀 |
|---|---|---|
| 复现命令 | 能跑出同样现象 | 命令可粘贴、含版本与 CPU |
| 分层 | 四层都检查 | 能指出「第一次出现差异」的 pass |
| 根因 | 说对层 | 能引用源码/表格/优先级 |
| 修复 | 给出一个参数 | 能解释参数为什么有效、副作用是什么 |
| 表达 | 结论清楚 | 有数字、有前后对比、有「不修」的理由 |

## 参考答案

### 案例 A

**第一次出现差异的层**：原因在 pre-RA scheduler，表现在 greedy RA。

```bash
# 冻结 ISel
$LLC ... -stop-after=finalize-isel -o ldmul.mir ldmul.ll

# 只看调度后的 MIR：没有栈访问，但虚拟寄存器 live range 被拉开
$LLC ... -print-after=machine-scheduler -o /dev/null ldmul.ll | grep -c "stack\."
# 0

# 再看 RA 后：栈访问出现
$LLC ... -print-after=greedy -o /dev/null ldmul.ll | grep -c "stack\."
# 34
```

`finalize-isel` 与 `machine-scheduler` 都没有 `stack.`；`greedy` 之后出现 34 处 `stack.`，最终汇编里 17 个 spill slot、60 次栈访问（100 条 `ld/sd` 减去 40 条数据访问）。根因是 bidirectional 调度同时从 top 和 bottom 生长，为了填延迟把 40 条 load 的 live range 拉长，超过 32 个整数寄存器后 greedy allocator 开始 spill。

精确的 `-stats` 对比（`sifive-u74`）：

| 配置 | 栈帧 | spilled live ranges | asm 中 `ld/sd` 总数 |
|---|---:|---:|---:|
| 默认（bidirectional） | 240 B | 17 | 100（40 数据 + 60 栈） |
| `topdown` | 无栈帧 | 0 | 40（全是数据） |
| `bottomup` | 240 B | 19 | 104（40 数据 + 64 栈） |

注意「第一次出现差异」有两层含义：**顺序/活跃区间**在 `machine-scheduler` 就变了，**栈访问**在 `greedy` 才出现。报告里要把这两句分开写，否则会把 RA 的现象错误归因给 scheduler。

`topdown` 只从顶部生长，优先把 load 立刻喂给 mul，live range 短，spill 消失。对应 `tryCandidate` 的 `RegExcess`/`RegCritical` 压力优先级：当压力超过阈值时，scheduler 应该停止追求 ILP；bidirectional 的两个边界各自看到一半压力，容易低估全局压力。

**修复**：

- 命令行：`-misched-prera-direction=topdown`（本案例 704 vs 1424）。
- 源码：在 `RISCVSubtarget::overrideSchedPolicy` 里对高压力 region 切 topdown，或调 `tryCandidate` 的 `RegExcess` 权重；不要直接删 bidirectional，因为它对其它循环更好。

### 案例 B

**第一次出现差异的层**：选择层（但选择是「正确」的），根因在成本模型。

```bash
$LLC -O2 -mtriple=riscv64 -mattr=+m,+zba -mcpu=spacemit-x60 \
     -print-after=finalize-isel -o /dev/null chain.ll
# MIR: SH3ADD + MUL，没有 SLLI/ADD
```

```bash
$MCA -mtriple=riscv64 -mcpu=spacemit-x60 -mattr=+m,+zba \
     -instruction-info chain.s
# add 1, slli 1, sh3add 2, mul 6
```

不是 scheduler 的错：顺序已经是 `SH3ADD -> MUL`，没有可重排空间。根因是 `SH3ADD` 的延迟（2）高于 `slli+add` 的串行链（1+1），在这条纯依赖链上融合反而更慢。

**修复**：在 pattern 的收益判断里比较 `sh3add` 与 `slli+add` 的 latency；或者接受它，因为在吞吐场景（多条独立链）里少一条指令可能更好。关键是**不要用「指令数」作为唯一收益指标**。

### 案例 C

`-print-after=postmisched` 仍有：

```text
PseudoCALL target-flags(riscv-call) @callee, <regmask ...>
```

汇编：

```asm
call    callee
```

`PseudoCALL` 由汇编打印机/MC 层展开成 `auipc+jalr`，目的是保留 `R_RISCV_CALL`/`R_RISCV_RELAX` 重定位对。`Sched<[WriteIALU, WriteJalr, ReadJalr]>` 描述的是这两条指令的资源，但 MachineScheduler 把它当成一个 SUnit；它不能在 `auipc` 与 `jalr` 之间插指令。

结论：不是 bug。诊断时看到 `PseudoCALL` 应检查汇编里是否有 `call`，而不是继续找展开 pass。

### 案例 D

`vsetivli` 六条，两种 VTYPE：

```asm
vsetivli zero, 8, e32, mf2, ta, ma
vsetivli zero, 4, e64, m1, ta, ma
```

`finalize-isel` 只有 `PseudoVLE32_V_MF2` / `PseudoVLE64_V_M1` 等，没有 `vsetvli`；`riscv-insert-vsetvli` 之后才出现六条。

重排方案：把 e32 的 load/add/store 放一起，e64 的 load/add/store 放一起。预测：每种 VTYPE 一条 `vsetivli`，共两条（如果两个 store 的地址计算不引入额外切换）。

注意：重排后 load/store 的别名关系可能变化，必须先确认 `%a`/`%b`/`%c` 不重叠（本案例可以加 `noalias`）。

## 时间预算与评分权重

建议总时长 90 分钟：

| 阶段 | 时间 | 产出 |
|---|---:|---|
| 案例 A | 30 min | 方向消融表 + 根因一段 |
| 案例 B | 15 min | mca 延迟表 + 结论 |
| 案例 C | 10 min | pass 顺序证据 |
| 案例 D | 20 min | `vsetivli` 计数 + 重排方案 |
| 写报告 | 15 min | 四份一页报告 |

评分权重建议：

- 复现与分层（40%）：命令能不能跑、四层有没有都检查。
- 根因（30%）：有没有指名 pass、有没有引用优先级/源码。
- 修复（20%）：参数是否有效、副作用是否说清。
- 表达（10%）：数字、前后对比、结论是否可复述。

如果只能完成两个案例，优先做 A 与 D：A 覆盖「调度 → 压力 → RA」的主线，D 覆盖「IR 顺序 → `vsetvli`」的 RVV 主线。

## 把诊断变成回归测试

诊断结论如果能落成一条 MIR/lit 测试，才算真正可复现。以案例 A 为例：

1. 冻结 ISel：

```bash
$LLC -O2 -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
     -stop-after=finalize-isel -o ldmul.mir ldmul.ll
```

2. 用 `-run-pass=machine-scheduler` 单独跑调度，对比两个方向：

```bash
$LLC -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 -run-pass=machine-scheduler \
     -misched-prera-direction=topdown -o - ldmul.mir
```

3. 用 FileCheck 锁定「前几条 load 之后立刻跟 mul」的顺序：

```llvm
; RUN: llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 ; RUN:   -run-pass=machine-scheduler -misched-prera-direction=topdown %s -o - ; RUN:   | FileCheck %s
; CHECK: %{{[0-9]+}}:gpr = LD
; CHECK-NEXT: %{{[0-9]+}}:gpr = MUL
```

注意这个 CHECK 只对当前 MIR 与当前 LLVM 版本稳定；上游测试通常用 `CHECK-DAG` 或只检查「spill 数」而不是逐条顺序。写回归测试时优先锁定**不变量**（例如「topdown 版本没有 `stack.`」），不要把整个指令序列写死。

案例 D 的回归测试更适合锁 `vsetvli` 条数：

```llvm
; RUN: llc -mtriple=riscv64 -mattr=+m,+v -riscv-v-vector-bits-min=512 %s -o - \
; RUN:   | FileCheck %s --check-prefix=VTYPE
; VTYPE-COUNT-2: vsetivli
```

`FileCheck` 没有直接的 `COUNT` 语法，实际写法是用 `grep -c` 包一层脚本，或者用 `-verify-machineinstrs` + 自定义 lit shell。这里只要求你理解「把诊断结论变成可自动检查的断言」这个方向。

## 面试版三句话答案

把报告压缩成面试可复述的三句话：

- **案例 A**：ISel 没问题；bidirectional 调度把 40 条 load 的 live range 拉长，greedy RA 产生 17 个 spill；换 `topdown` 后栈访问从 60 降到 0，代价是可能牺牲部分 ILP。
- **案例 B**：选择层确实选了 `sh3add`，但 `spacemit-x60` 给它的延迟是 2，而 `slli+add` 是两条 1-cycle；这是成本模型问题，不是 scheduler 问题。
- **案例 C**：`PseudoCALL` 在 MIR 里保留是链接器 relaxation 的要求；它按一个 SUnit 调度，汇编阶段才变成 `call`。
- **案例 D**：`vsetivli` 不在 ISel 阶段；它由 RA 之后的 `riscv-insert-vsetvli` 插入，六条来自 e32/e64 交错；重排 IR 比调 scheduler 有效。

## 常见错误

1. **直接看最终汇编就归因给 scheduler**。要先看 `finalize-isel`，确认选择层是否已经不对。
2. **把 `-mcpu=generic` 当成「无模型」**。它是 `generic-rv64`，用 SpacemitX60 模型；真正无模型用 `andes-ax25`。
3. **用 `-stats` 的 combined 计数判断收益**。MachineCombiner 的 30 次合并可能带来 240 字节栈帧（案例 A 的相邻现象）。
4. **在 `-O2` 下测 `SelectOptimize`**。它只在 `-O3` 加入流水线。
5. **忘记 `+m`**。对比 `mul` 时基线必须带 `-mattr=+m`，否则选成 libcall。
6. **用 `llvm-mca` 验证软件流水的 II**。mca 不模拟跨迭代 stage 结构；看 pipeliner 的 YAML。

## 提交前自查

- [ ] 每条复现命令都写了 `-mtriple`、`-mcpu`、`-mattr`、`-O` 级别。
- [ ] 四层（ISel / pre-RA / post-RA / 模型）都有结论，没有跳层。
- [ ] 「根因」指名了 pass，并解释了第一次出现差异的位置。
- [ ] 「修复」写了副作用，或明确说明「不该修」的理由。
- [ ] 至少有一个数字或 MIR 片段支撑结论，不只有文字。
- [ ] 报告能被另一个人按命令复现，不需要口头补充。

## 延伸

- 把案例 A 的 `ldmul.ll` 换成 20 组、60 组 load，画出「spill 数 vs load 数」曲线，找出 `sifive-u74` 的拐点。
- 把案例 B 的 `chain` 复制成 4 条独立链，再比较 `sh3add` 与 `slli+add`；解释为什么结论可能反转。
- 给案例 D 加 `noalias`，验证重排是否安全；再用 `-print-after=riscv-insert-vsetvli` 数 `vsetivli`。
- 用第 24 章的四层漏斗，给一个你实际遇到的 RISC-V 代码生成问题写一份报告。

→ [下一章：TableGen 质量与 sched model 维护](./25-tablegen-quality.md)
