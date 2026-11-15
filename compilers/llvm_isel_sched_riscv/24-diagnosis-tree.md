# Ch 24 — 诊断树：选错、排错、spill、pseudo 未展开

> **目标**：把前 23 章的知识压缩成一张可执行的诊断树。给你一段「看起来不对」的 RISC-V 汇编，你能在五分钟内判断问题发生在选择层、调度层、寄存器分配层还是伪指令展开层，并给出下一步命令，而不是盲目试 `-O3` 或 `-mattr`。
>
> **环境**：LLVM 24.0.0git；`llc`、`llvm-mca`、`opt`；常用 pass 名 `finalize-isel`、`machine-scheduler`、`postmisched`、`riscv-expand-pseudo-post-ra`、`riscv-insert-vsetvli`；`-print-after`、`-print-before`、`-stats`、`-debug-pass=Structure`。本章引用的数字都来自前几章的本机实测。

## 为什么需要诊断树

后端问题最难的部分不是「修」，而是「定位」。同一段汇编变慢，可能是：

- 指令选错了（`sh3add` vs `slli+add`）。
- 指令选对了，但 pre-RA 调度为了 ILP 把寄存器压力拉爆，spill 变多。
- 调度没问题，但 post-RA 被反依赖锁死。
- 伪指令在 pre-emit 才展开，调度器根本没看到真实指令。
- sched model 缺一条指令的 class，`llvm-mca` 直接拒绝建模。

这些问题的「症状」可能都是「这段代码变慢了」。诊断树的作用是**用最少的命令把范围砍半**，而不是一上来就读 scheduler 源码。

## 先建立直觉：四层漏斗

```text
症状：汇编不对 / 变慢 / spill 多 / mca 报错
  │
  ├─ 0. 冻结环境：版本、triple、cpu、mattr、O 级别
  │
  ├─ 1. ISel 选了什么？        -print-after=finalize-isel
  │      └─ 指令不对 -> 选择层（pattern / 扩展 / tune / IR 形状）
  │
  ├─ 2. pre-RA 排成什么？      -print-after=machine-scheduler
  │      └─ 顺序不对 / spill -> 调度层（方向 / 压力 / region）
  │
  ├─ 3. post-RA 改了什么？     -print-after=postmisched
  │      └─ 顺序又变了 -> 反依赖 / hazard / pseudo 展开
  │
  └─ 4. 汇编/模型对得上吗？    llvm-mca -instruction-info
         └─ lack-sched -> 模型缺口，不是代码问题
```

关键原则：**先确定「哪一层第一次出现差异」，再解释原因**。不要拿最终汇编直接归因给 scheduler。

## 第 0 步：冻结环境

在跑任何对比之前，固定这五项：

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
$LLC --version | head -3
$LLC -O2 -mtriple=riscv64 -mattr=+m -mcpu=<cpu> ...
```

| 项 | 为什么必须固定 | 常见坑 |
|---|---|---|
| LLVM 版本 | sched model 与 pass 顺序随版本变 | 24.0.0git 与 19 的 `PseudoVSETVLI` 行为不同 |
| triple | RV32/RV64 的寄存器与 ABI 不同 | 忘写 `-mtriple=riscv64`，默认 x86 |
| `-mcpu` | 同时决定 ISA 与 tune | `-mcpu=generic` 会被规范成 `generic-rv64`，用 SpacemitX60 模型 |
| `-mattr` | 只改 ISA，不改 tune | 对比 `mul` 必须带 `+m`；`+zba` 不会自动带 `+m` |
| `-O` 级别 | 决定 IR pass 与 FastISel | `-O0` 不走 DAG ISel 的同一路径 |

`llc` 没有 `-S`；默认输出到 `<input>.s`，要 stdout 必须 `-o -`。这是第 0 章就踩过的坑，诊断时最容易浪费五分钟。

## 第 1 步：ISel 选了什么

命令：

```bash
$LLC -O2 -mtriple=riscv64 -mattr=+m -mcpu=<cpu> \
     -print-after=finalize-isel -o /dev/null input.ll
```

看三件事：

1. **指令 opcode 是否符合预期**。`SH3ADD` 还是 `SLLI+ADD`？`CZERO_EQZ` 还是 `BNE+PHI`？
2. **有没有意外的伪指令**。`PseudoMovImm`、`PseudoMovAddr`、`PseudoCCMOVGPRNoX0` 都意味着「真实指令还没出现」。
3. **基本块数量**。`select` 是否被 lowered 成分支？分支数量决定调度区域。

如果这一步就不对，问题在**选择层**，继续看：

| 现象 | 可能原因 | 下一步 |
|---|---|---|
| 该用 `sh3add` 却用了 `slli+add` | 没开 `+zba`；或 pattern 被别的 combine 挡住 | 查 `-mattr`；用 `-debug-only=isel` 看匹配 |
| 该用 `czero` 却用了分支 | 没开 `+zicond`；或 CPU 走 `PseudoCCMOVGPR` | 查 `-mattr=+zicond` 与 `-mcpu` 的 tune feature |
| 常量物化序列与别的 CPU 不同 | `TuneLUIADDIFusion` 改变 `RISCVMatInt` | 对比 `-mcpu`，查 `RISCVMatInt.cpp` |
| 多了一条 `addi` | GEP 偏移超出 12-bit；folding 失败 | 看偏移是否在 `[-2048,2047]` |
| 全局地址多一条 `addi` | `PseudoMovAddr` 未合并 | 看 `riscv-merge-base-offset` 前后 |

### 例 1：`sh3add` 反而更慢

第 18 章的 `chain` 在 `spacemit-x60` 上：

```text
基线 slli+add+mul：702 cycles / 100 iter
+zba sh3add+mul：  801 cycles / 100 iter
```

`-print-after=finalize-isel` 确认选择没错（确实选了 `SH3ADD`）。继续用 `llvm-mca -instruction-info`：

```text
spacemit-x60: add latency 1, sh3add latency 2
```

结论：**选择层正确，问题在成本模型**。`sh3add` 在这颗核上延迟更高，融合反而拉长关键路径。修复方向不是改 scheduler，而是改 pattern 的收益判断（或接受它，因为吞吐场景可能不同）。

## 第 2 步：pre-RA 排成什么

命令：

```bash
$LLC ... -print-after=machine-scheduler -o /dev/null input.ll
```

看：

- 指令顺序是否与 `finalize-isel` 不同；不同说明 scheduler 动了。
- 有没有 spill/reload 被插入（此时还没有 RA，但 scheduler 会插入 COPY）。
- 基本块是否被合并/拆分（region 边界）。

如果顺序不对，先问三个问题：

1. **方向对不对**？RISC-V pre-RA 默认 bidirectional；用 `-misched-prera-direction=topdown|bottomup|bidirectional` 对比。
2. **压力是不是被拉爆**？看 `-stats` 的 `regalloc - Number of spills inserted` 与栈帧大小。
3. **region 是否被切碎**？`select`/分支/调用点都是边界；看 MIR 的 `successors`。

### 例 2：`ldmul.ll` 的方向消融

第 15 章的实测（`sifive-u74`）：

| 配置 | 栈访问 | 周期（mca） |
|---|---:|---:|
| default（bidirectional） | 60 | 1424 |
| `topdown` | 0 | 704 |
| `bottomup` | 64 | 1604 |
| misched off + source order | 84 | 2014 |
| misched off + list-ilp | 40 | 1274 |

如果只看最终汇编「有 60 次栈访问」，可能会说「scheduler 很差」。正确的诊断是：

- `finalize-isel` 的指令数正常 → 选择没问题。
- `machine-scheduler` 后 live range 变长 → 压力问题。
- 换 `topdown` 后 spill 消失 → 方向启发式与压力模型的交互。

修复方向：调 `-misched-prera-direction`、`-misched-regpressure`，或减小 region（例如把长链拆成多个基本块）。

## 第 3 步：post-RA 改了什么

命令：

```bash
$LLC ... -print-after=postmisched -o /dev/null input.ll
```

post-RA 的合法动作比 pre-RA 少：

- 填延迟槽（load-use、乘法器）。
- 打散反依赖（如果 target 支持；RISC-V 默认不支持 `-break-anti-dependencies` 的完整效果）。
- 为 macro fusion / load-store pair 创造相邻关系。

如果 post-RA 后顺序又变了，重点看：

- 有没有 `PseudoCALL` / `PseudoRET` 还在（正常，它们在 MC 层展开）。
- 有没有 `PseudoMovImm` / `PseudoMovAddr` 被展开成多条（`riscv-expand-pseudo-post-ra` 在 `postmisched` 之前）。
- hazard recognizer 是否报 `Disabled scoreboard hazard recognizer`（第 17 章）。

### 例 3：`caller` 里的 `PseudoCALL` 不是 bug

`-print-after=postmisched` 仍然看到：

```text
PseudoCALL target-flags(riscv-call) @callee, <regmask ...>
```

最终汇编是：

```asm
call    callee
```

这不是「伪指令没展开」，而是设计：`auipc+jalr` 必须成对保留给链接器 relaxation。诊断时看到 `PseudoCALL` 不要继续找 pass；去汇编里确认 `call` 存在即可。

## 第 4 步：模型对得上吗

```bash
$MCA -mtriple=riscv64 -mcpu=<cpu> -mattr=+m -instruction-info input.s
```

如果报：

```text
error: found an unsupported instruction ... use -skip-unsupported-instructions=lack-sched
```

说明该指令在 CPU 的 sched model 里没有 class。常见原因：

- 新扩展（`Zba`、`Zicond`、`Zicboz`）尚未被所有 CPU 模型覆盖。
- 指令用 `Sched<[]>` 声明，或完全没写 `Sched`。
- `UnsupportedFeatures` 把整个扩展标成不支持。

### 例 4：`cbo.zero` 的 `lack-sched`

第 13 章的实测：

```text
cbo.zero 报 unable to resolve scheduling class for write variant
```

`cbo.zero` 是 cache block zero，写内存但没有普通 store 的 sched class；`+lack-sched` 会把整条指令从 mca 输入里丢掉，周期数因此失真。诊断结论是**模型缺口**，不是代码生成错误。

## 第 5 步：RVV 专项

RVV 的问题优先看三处：

1. `-print-after=finalize-isel`：有没有 `PseudoV*`，LMUL 编码是多少。
2. `-print-after=riscv-insert-vsetvli`：插入了多少条 `vsetvli`，位置是否在循环内。
3. `-print-after=postmisched`：post-RA 是否把 `vsetvli` 与向量指令排在一起。

### 例 5：`vmix` 的六条 `vsetivli`

第 23 章的 `vmix` 在 `-riscv-v-vector-bits-min=512` 下有六条 `vsetivli`。`finalize-isel` 里没有 `vsetvli`，`riscv-insert-vsetvli` 之后才出现六条。结论：**不是 scheduler 插入的，是 IR 的 SEW 交错导致的**。修复方向是重排 IR（先做完全部 e32，再做 e64），不是调 `-misched-*`。

## 一张可打印的检查表

| 症状 | 第一条命令 | 若命中，下一步 |
|---|---|---|
| 指令选错 | `-print-after=finalize-isel` | 查 `-mattr`、pattern、tune feature |
| 顺序不对 | `-print-after=machine-scheduler` | 对比 `-misched-prera-direction` |
| spill 多 | `-stats` + 栈帧 | 查压力、region、MachineCombiner |
| post-RA 又变了 | `-print-after=postmisched` | 查反依赖、hazard、pseudo 展开 |
| `Pseudo*` 还在 | `-print-after=riscv-expand-pseudo-*` | 确认展开点；`PseudoCALL` 属正常 |
| mca 报 lack-sched | `-instruction-info` | 换 CPU 或承认模型缺口 |
| 向量慢 | `-print-after=riscv-insert-vsetvli` | 数 `vsetvli`，检查 SEW 分组 |
| 常量序列怪 | 对比不同 `-mcpu` | 查 `TuneLUIADDIFusion` |

## 一次完整的诊断记录

把上面的步骤串成一个真实案例：`chain` 在 `spacemit-x60` 上开启 `+zba` 后变慢。

**第 0 步：冻结环境**

```bash
$LLC --version | head -1
# LLVM version 24.0.0git
$LLC -O2 -mtriple=riscv64 -mattr=+m -mcpu=spacemit-x60 -o - chain.ll
```

**第 1 步：确认选择**

```bash
$LLC ... -print-after=finalize-isel -o /dev/null chain.ll
```

MIR 里是 `SH3ADD` + `MUL`，没有 `SLLI`/`ADD`。选择层符合预期，排除「pattern 没匹配」。

**第 2 步：确认调度**

```bash
$LLC ... -print-after=machine-scheduler -o /dev/null chain.ll
```

顺序仍是 `SH3ADD` → `MUL`，scheduler 没有制造额外问题。排除方向/压力。

**第 3 步：确认 post-RA**

```bash
$LLC ... -print-after=postmisched -o /dev/null chain.ll
```

顺序不变，没有 spill。排除 post-RA。

**第 4 步：查模型**

```bash
$MCA -mtriple=riscv64 -mcpu=spacemit-x60 -mattr=+m,+zba      -instruction-info chain.s
```

```text
add      latency 1
sh3add   latency 2
mul      latency 6
```

**结论**：选择正确、调度正确、post-RA 正确；问题在 `spacemit-x60` 的 sched model 给 `sh3add` 的延迟（2）高于 `slli+add` 链（1+1，但可并行？此处是串行链）。修复方向是成本模型/pattern 收益判断，不是 scheduler。

这个记录的价值在于：**每一步都排除一个层**，而不是反复猜 `-mattr`。真实诊断时把每一步的输出贴进 issue/PR，reviewer 能直接复现。

## 踩雷

1. **`-mcpu=generic` 不是「没有模型」**。它被规范成 `generic-rv64`，用 SpacemitX60 模型；真正无模型用 `-mcpu=andes-ax25` 或 `-mtune=andes-ax25`。
2. **`-print-after` 的 pass 名必须准确**。`postmisched`、`machine-scheduler`、`finalize-isel`、`riscv-expand-pseudo-post-ra` 都是注册名；写错会静默无输出。
3. **`-stats` 不是所有构建都有**。需要 assertions 或统计开启的构建；本课使用的探索构建有。
4. **`-misched-prera-direction` 没有 `default` 取值**。合法值是 `topdown`、`bottomup`、`bidirectional`；写 `default` 会报错。
5. **`llvm-mca` 的 `-mcpu` 与 `llc` 的 `-mcpu` 语义不完全相同**。mca 只关心 sched model，不跑代码生成；不要用 mca 的 CPU 名称去推断 `llc` 的 ISA 扩展。
6. **不要把「更少的指令」当成「更快的代码」**。第 18 章的 `sh3add`、第 21 章的 MachineCombiner 都是反例。

## 动手练习

1. 给一个函数，故意把 `-mattr=+zba` 去掉，用诊断树判断「为什么没有 `sh3add`」，并写出每一步命令。
2. 用 `ldmul.ll` 复现 60 spill 与 0 spill 两个版本，指出第一次出现差异的 pass。
3. 找一个带 `call` 的函数，证明 `PseudoCALL` 在 `postmisched` 仍在、在汇编里变成 `call`；解释为什么这不是 bug。
4. 用 `-mcpu=andes-ax25` 编译一个含 `mul` 的函数，观察 `NoSchedModel` 下的调度与 mca 行为。
5. 把 `vmix` 的 IR 重排，数 `vsetivli` 的变化，并用 `-print-after=riscv-insert-vsetvli` 验证。

## 重点整理

- 诊断顺序：冻结环境 → ISel → pre-RA → post-RA → 汇编/模型。
- 选择层的症状：opcode 不对、伪指令意外、基本块数量异常。
- 调度层的症状：顺序变了、spill 多了、region 被切碎。
- post-RA 层的症状：反依赖、hazard、伪指令展开。
- 模型层的症状：`lack-sched`、`UnsupportedFeatures`、固定延迟不随 VL 变。
- 先定位「第一次出现差异的 pass」，再解释原因；不要从最终汇编倒推。

## 自我检核

- [ ] 我能用四条 `-print-after` 命令把问题定位到选择/调度/RA/展开层。
- [ ] 我能解释 `ldmul.ll` 的 60 spill 是方向启发式造成的，而不是 ISel 选错。
- [ ] 我知道 `PseudoCALL` 在 post-RA 仍在是正常现象。
- [ ] 我能区分 `lack-sched` 与「模型延迟不准」。
- [ ] 我能用 `riscv-insert-vsetvli` 判断 `vsetvli` 是 IR 顺序问题还是调度问题。

## 延伸阅读

- 第 0 章的「环境与基线」：所有命令的前提与 `llc` 的输出约定。
- 第 15 章的 `tryCandidate` 优先级表：诊断 spill 时对照方向启发式。
- 第 17 章的 post-RA hazard 日志：区分 `Disabled scoreboard hazard recognizer` 与真正的调度失败。
- 第 20 章的三个 pseudo 展开 pass：确认「伪指令还在」是否正常。
- 第 23 章的 `RISCVPreRAMachineSchedStrategy`：RVV 专项诊断的源码入口。
- `llvm/docs/CommandGuide/llc.rst` 与 `llvm-mca.rst`：`-print-after`、`-stats`、`-instruction-info` 的官方说明。

→ [下一章：TableGen 质量与 sched model 维护](./25-tablegen-quality.md)
