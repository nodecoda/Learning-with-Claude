# Ch 22 — 循环、软件流水与调度边界

> **目标**：理解 MachinePipeliner 在 RISC-V 上的真实状态：默认不跑、`-enable-pipeliner=true` 才加入流水线；同一个循环在 `rocket-rv64` 上得到 `II=9, MaxStageCount=0`（不变换），在 `sifive-u74` 上得到 `II=6, MaxStageCount=1`（真正流水）。学完你能解释「为什么资源模型决定了能不能流水」，以及流水后 prologue/epilogue 的形态。
>
> **环境**：LLVM 24.0.0git；`llc`；`-mtriple=riscv64 -mattr=+m`；`-enable-pipeliner=true`；`-pass-remarks-output` 读取 pipeliner 的 YAML 诊断；CPU 对照 `rocket-rv64` 与 `sifive-u74`。所有汇编与 YAML 均为本机实跑。

## 为什么软件流水值得单独一章

前面几章的调度都是**基本块内**的重排：scheduler 在不改变迭代次数的前提下，把指令挪到更合适的 cycle。软件流水（software pipelining，SWP）跨的是**迭代边界**：

```text
普通循环：   iter i 的 load -> iter i 的 compute -> iter i 的 store
软件流水：   iter i 的 load -> iter i+1 的 load
                            -> iter i 的 compute
                            -> iter i+1 的 compute
                            -> iter i 的 store
```

它把「下一轮的 load」提前到「这一轮的 compute」之前，用重叠掩盖 load 延迟。代价是：

- 需要 prologue（填充流水线）和 epilogue（排空）。
- 活跃区间变长，寄存器压力上升。
- 需要硬件有足够的 issue 宽度与功能单元；单发射顺序核很难受益。

这三条都与 **sched model** 直接相关，所以它是「调度模型 → 代码形状」的一个极端例子。

## 先建立直觉：RISC-V 默认不跑 MachinePipeliner

`TargetPassConfig::addMachinePasses` 的条件是：

```cpp
if (getOptLevel() != CodeGenOptLevel::None &&
    valueOr(getOpts(*TM).enable_pipeliner, isMachinePipelinerEnabled()))
  addPass(&MachinePipelinerID);
```

`isMachinePipelinerEnabled()` 的默认实现返回 `false`；RISC-V 没有覆盖它。`getOpts(*TM).enable_pipeliner` 来自 `-enable-pipeliner` 这个 optional bool。因此：

```bash
# 默认：没有 -pipeliner pass
llc -O2 -mtriple=riscv64 -mattr=+m -mcpu=rocket-rv64 -o - loop.ll

# 显式打开：pass 才会出现
llc -O2 -mtriple=riscv64 -mattr=+m -mcpu=rocket-rv64 \
    -enable-pipeliner=true -o - loop.ll
```

用 `-debug-pass=Structure` 验证：

```bash
llc ... -enable-pipeliner=true -debug-pass=Structure -o /dev/null loop.ll 2>&1 \
  | tr ' ' '\n' | grep -n pipeliner
```

输出里会出现 `-pipeliner`，位置在 `-riscv-expand-pseudo-pre-ra` 之前、`-machine-scheduler` 之前。也就是说，SWP 在 **pre-RA scheduler 之前** 重塑循环，之后的 RA 与 post-RA 调度都在新形状上工作。

注意 `-enable-pipeliner` 是 optional bool：单独写 `-enable-pipeliner` 不带值，可能被解析成默认/未设置。本课统一写 `-enable-pipeliner=true`，避免歧义。

## 主线一：一个能流水的循环

```llvm
define void @f(ptr noalias %a, ptr noalias %b) {
entry: br label %for
for:
  %i = phi i64 [0,%entry],[%i1,%for]
  %pa = getelementptr i64, ptr %a, i64 %i
  %pb = getelementptr i64, ptr %b, i64 %i
  %x = load i64, ptr %pa
  %y = load i64, ptr %pb
  %s1 = add i64 %x, %y
  %s2 = mul i64 %s1, 5
  %s3 = xor i64 %s2, %x
  store i64 %s3, ptr %pa
  %i1 = add nuw nsw i64 %i, 1
  %c = icmp ult i64 %i1, 100
  br i1 %c, label %for, label %exit
exit: ret void
}
```

默认（不开 pipeliner）在 `sifive-u74`：

```asm
f:
        addi    a2, a1, 800
.LBB0_1:
        ld      a3, 0(a0)
        ld      a4, 0(a1)
        addi    a1, a1, 8
        add     a4, a4, a3
        slli    a5, a4, 2
        add     a4, a4, a5
        xor     a3, a3, a4
        sd      a3, 0(a0)
        addi    a0, a0, 8
        bne     a1, a2, .LBB0_1
        ret
```

每次迭代顺序执行：`ld, ld, add, slli, add, xor, sd, addi, addi, bne`。load 的延迟没有被掩盖，下一轮要等这一轮的 `sd` 完成（地址 `a0` 更新后）。

## 主线二：`rocket-rv64` 为什么不流水

打开 pipeliner：

```bash
llc -O2 -mtriple=riscv64 -mattr=+m -mcpu=rocket-rv64 \
    -enable-pipeliner=true -pass-remarks-output=rem.yaml -o - loop.ll
```

汇编与默认**完全相同**。`rem.yaml` 里：

```yaml
--- !Analysis
Pass:            pipeliner
Name:            schedule
Function:        f
Args:
  - String:          'Schedule found with Initiation Interval: '
  - II:              '9'
  - String:          ', MaxStageCount: '
  - MaxStageCount:   '0'
...
--- !Analysis
Pass:            pipeliner
Name:            schedule
Function:        f
Args:
  - String:          'Unable to find schedule'
```

`II=9` 是 initiation interval，即「每隔 9 个 cycle 启动一轮迭代」。`MaxStageCount=0` 表示只找到单 stage 的调度，没有真正重叠。原因在资源模型：

- Rocket 的 sched model 是单发射（`IssueWidth = 1`）。
- 循环体约 10 条机器指令（含分支），每条都要占一个 issue slot。
- 单发射下 MII 接近指令数；pipeliner 算出 `II=9`，没有空余 slot 让下一轮提前启动，stage count 自然是 0。

`Unable to find schedule` 是 pipeliner 在尝试更大 stage count 时的失败诊断，不是「完全没跑」。它先找到一个 stage-0 的合法调度，再尝试重叠，失败后保留原样。

## 主线三：`sifive-u74` 上真正流水

同样的命令，`-mcpu=sifive-u74`：

```yaml
--- !Analysis
Pass:            pipeliner
Name:            schedule
Function:        f
Args:
  - String:          'Schedule found with Initiation Interval: '
  - II:              '6'
  - String:          ', MaxStageCount: '
  - MaxStageCount:   '1'
...
--- !Passed
Pass:            pipeliner
Name:            schedule
Function:        f
Args:
  - String:          'Pipelined succesfully!'
```

`II=6`，`MaxStageCount=1`：两 stage 重叠，每 6 个 cycle 启动一轮。生成的汇编：

```asm
f:
        ld      a2, 0(a0)          # prologue: 预取第 0 轮 a
        addi    a3, a1, 800
        ld      a4, 0(a1)          # prologue: 预取第 0 轮 b
        add     a4, a4, a2
        slli    a5, a4, 2
        add     a4, a4, a5
        addi    a5, a0, 8
        addi    a1, a1, 8
        beq     a1, a3, .LBB0_2
.LBB0_1:                            # steady state
        mv      a6, a0
        xor     a2, a2, a4          # 第 i 轮 compute
        ld      a4, 0(a1)           # 第 i+1 轮 load b
        mv      a0, a5
        sd      a2, 0(a6)           # 第 i 轮 store
        ld      a2, 0(a5)           # 第 i+1 轮 load a
        addi    a5, a5, 8
        add     a4, a4, a2
        slli    a6, a4, 2
        add     a4, a4, a6
        addi    a1, a1, 8
        bne     a1, a3, .LBB0_1
.LBB0_2:
        xor     a2, a2, a4          # epilogue: 最后一轮 compute
        sd      a2, 0(a0)           # epilogue: 最后一轮 store
        ret
```

结构非常清楚：

```text
prologue   : 预取第 0 轮的 load，算到一半
steady     : 每轮「用上一轮的数据 compute + store」同时「为下一轮 load」
epilogue   : 把最后一轮的 compute/store 收尾
```

`mv` 是寄存器旋转的产物：软件流水需要把「这一轮用的寄存器」和「下一轮要写的寄存器」错开，RA 之后就是这些 `mv`。它们不是冗余，是流水线阶段的边界。

## 主线四：为什么 II 从 9 变成 6

两个 CPU 的 sched model 差异：

| 参数 | `rocket-rv64` | `sifive-u74` |
|---|---|---|
| IssueWidth | 1 | 2 |
| `LD` latency | 2 | 3 |
| `ADD`/`XOR` latency | 1 | 3 |
| `MUL` latency | 4 | 3 |
| 循环体指令数 | 约 10 | 约 10 |

初看 u74 的延迟更大，为什么反而能流水？因为 SWP 的目标不是「单轮更快」，而是「稳态下每轮启动间隔更小」：

- Rocket 单发射，9 条指令至少 9 个 issue slot，`II >= 9`；没有空余 slot 做重叠。
- u74 双发射，约 10 条指令理论上 5 个 slot 就能发完；`II=6` 受限于某条依赖链（load latency 3 + ALU 链），仍有空间让下一轮提前进入。
- u74 的 ALU 延迟 3 让单轮更慢，但双发射让**吞吐**更高；SWP 利用的正是吞吐余量。

这也是「延迟大的机器不一定慢」的一个具体例子：单看一条 `add` 的 latency，u74 是 3、rocket 是 1；但循环稳态由 issue 宽度和依赖结构共同决定。

## 用 `llvm-mca` 看稳态块：能看吞吐，不能看 II

把默认循环体和流水后的 steady-state 循环体分别抽成汇编，用 `llvm-mca -iterations=100` 比较：

```text
=== swp_default.s ===
Iterations: 100   Instructions: 1000   Total Cycles: 1003
Dispatch Width: 2   Block RThroughput: 5.0

=== swp_steady.s ===
Iterations: 100   Instructions: 1200   Total Cycles: 903
Dispatch Width: 2   Block RThroughput: 6.0
```

两个数字看起来矛盾：稳态块的 `Block RThroughput` 从 5.0 升到 6.0（更差），但 100 次迭代的总周期从 1003 降到 903（更好）。原因是：

- `Block RThroughput` 只看「这个块要占用多少资源」，稳态块多了 `mv`、多了一次 load，资源占用当然更高。
- SWP 的收益不在单块吞吐，而在**跨迭代重叠**；mca 把稳态块当成线性指令序列，不会模拟「下一轮提前启动」的 stage 结构，所以它给出的 903 只是近似。

结论：mca 可以用来看「流水后的块有多重、有没有 spill 风险」，但**不能用来验证 II 是否真的达到 6**。验证 II 要看 pipeliner 的 YAML 与真实硬件性能计数器。

## 主线五：SWP 的准入条件

`MachinePipelinerImpl::canPipelineLoop` 的检查（源码）：

1. 循环必须是**单基本块**（`L.getNumBlocks() == 1`）；多块循环直接拒绝。
2. PHI 不能形成环（`hasPHICycle`）。
3. 分支必须能被 `analyzeBranch` 理解；RISC-V 的 `RISCVInstrInfo::analyzeLoopForPipelining` 还要求：
   - 不是无限循环（`TBB == LoopBB && FBB == LoopBB`）；
   - 必须有条件分支（`FBB != nullptr`）；
   - 条件的 lhs/rhs 不能直接是 PHI。
4. 必须有 preheader。
5. store 数量不能超过 `-pipeliner-max-num-stores`（默认 1）。

第 5 条很关键：一个循环里如果有两条 store，默认就不会被流水。`loop2.ll` 只有一条 store，所以通过了；如果再加一条 `store`，即使资源足够也会被拒。

## 踩雷

1. **`-enable-pipeliner` 不带值可能不生效**。它是 optional bool；本课统一写 `-enable-pipeliner=true`。判断 pass 是否加入，用 `-debug-pass=Structure`，不要靠汇编猜。
2. **`-pass-remarks` 不写 output 文件时可能看不到分析 remark**。pipeliner 的 `II`/`MaxStageCount` 是 `Analysis` 级 remark，用 `-pass-remarks-output=rem.yaml` 最稳。
3. **`MaxStageCount: 0` 不等于「pass 没跑」**。它表示找到了合法调度但没有重叠；汇编不变是正常结果。
4. **`II` 不是「每轮执行的 cycle 数」**。它是「每 II 个 cycle 启动一轮」；稳态吞吐是 `1/II` 轮每 cycle，单轮延迟由 stage 数决定。
5. **SWP 会显著增加寄存器压力**。`loop2` 在 u74 上只多了几个 `mv`，但复杂循环可能出现 spill；`-pipeliner-register-pressure` 是相关旋钮。
6. **SWP 与 unroll 不是一回事**。unroll 复制循环体、减少分支开销；SWP 重叠不同迭代的阶段，不复制整个循环体（但会生成 prologue/epilogue）。
7. **多块循环不会被流水**。先把循环简化成单块（`-loop-simplify`、`-simplifycfg`），否则 `canPipelineLoop` 直接拒绝。

## 动手练习

1. 用 `-debug-pass=Structure` 对比 `-enable-pipeliner=true` 与默认，确认 `-pipeliner` 的位置；再用 `-print-after=pipeliner` 看它是否改写了 MIR。
2. 在 `loop2.ll` 里加第二条 `store`，重新在 u74 上打开 pipeliner，观察 `-pipeliner-max-num-stores` 的影响；把上限调大再试。
3. 把循环的 trip count 从 100 改成 4，观察 prologue/epilogue 是否还值得；解释 `MaxStageCount` 与 trip count 的关系。
4. 在 u74 上分别用 `-pipeliner-force-ii=4` 与 `-pipeliner-force-ii=6`，比较生成的代码与 `-stats` 的 spill 数。
5. 用 `llvm-mca` 比较流水前后稳态循环的吞吐（把 loop body 单独抽成汇编），验证 `II` 与实际周期数的关系。

## 重点整理

- RISC-V 默认不跑 MachinePipeliner；`-enable-pipeliner=true` 才把 `-pipeliner` 加入流水线，位置在 pre-RA scheduler 之前。
- SWP 的准入：单基本块、可分析分支、无 PHI 环、有 preheader、store 数不超限。
- `II` 由资源模型与依赖链共同决定；`MaxStageCount=0` 表示没有重叠，汇编不变。
- `rocket-rv64` 单发射 → `II=9` 无法重叠；`sifive-u74` 双发射 → `II=6` 成功重叠。
- 流水后代码分为 prologue / steady state / epilogue，`mv` 是寄存器旋转的产物。
- SWP 用寄存器压力换吞吐；不要只看单轮延迟。

## 自我检核

- [ ] 我能用 `-debug-pass=Structure` 证明默认没有 `-pipeliner`。
- [ ] 我能解释 `II=9, MaxStageCount=0` 与 `II=6, MaxStageCount=1` 的差别。
- [ ] 我能指出流水后汇编里的 prologue、steady state、epilogue。
- [ ] 我知道 `canPipelineLoop` 的五条准入条件。
- [ ] 我能解释为什么 u74 的延迟更大却更適合流水。

## 延伸阅读

- `llvm/lib/CodeGen/MachinePipeliner.cpp`：读 `canPipelineLoop` 与 `scheduleLoop`，重点看 `II`/`MaxStageCount` 的计算与 `MachineOptimizationRemarkAnalysis` 的输出。
- `llvm/lib/Target/RISCV/RISCVInstrInfo.cpp` 的 `analyzeLoopForPipelining`：看 RISC-V 对分支、PHI、trip count 的额外限制。
- `llvm/lib/CodeGen/TargetPassConfig.cpp` 的 `addMachinePasses`：确认 `-enable-pipeliner` 的 optional bool 语义。
- `llvm/docs/CodeGenerator.rst` 中关于 MachinePipeliner 的章节：补 modulo scheduling 的背景。
- `llvm/test/CodeGen/RISCV/` 下与 `pipeliner` 相关的测试：看官方如何用 `-pass-remarks-output` 做 FileCheck。

→ [下一章：RVV、`vsetvli` 与向量调度](./23-rvv-selection-scheduling.md)
