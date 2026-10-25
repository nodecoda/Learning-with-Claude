# Ch 17 — Fast vs Greedy 实验设计

> **目标**：在同一份 legalized MIR 上公平比较 Fast 与 Greedy：数清跨 bank COPY、解释每一条 COPY 的来源、测编译时间，并知道 Greedy 什么时候会赢、什么时候反而更差。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（`llc --version` 报 Host CPU skylake），AArch64。编译时间数字是这台机器上的实测，只代表数量级。
>
> **证据约定**：`or.ll`、`greedy_worse.ll`、`optnone`、3000 函数编译时间均为实测；源码结论标“源码依据”；未跑的标 `guided`。不伪造 benchmark 或 lit 输出。

## 为什么需要这个？

“Greedy 比 Fast 好”这句话只在特定前提下成立。常见的错误比较是：

- 拿 `-O0` 的 IR 和 `-O2` 的 IR 比，legalizer 输出已经不同。
- 拿两份不同的 MIR 比，指令数、块布局、寄存器编号都不同。
- 只数 `COPY` 总数，把 ABI 边界的 `COPY $s0` 也算成 RBS 的修复。
- 只跑一次，把进程启动和 MIR 解析时间当成 pass 时间。

公平实验要固定输入，只改一个变量：`-regbankselect-greedy`。然后同时看两个维度：

1. **输出质量**：跨 bank COPY 数、指令数、后续寄存器压力。
2. **编译时间**：RBS 自身耗时，以及整条 `llc` 的耗时。

这两个维度经常冲突：Greedy 可能减少 COPY，也可能增加 COPY；它一定比 Fast 做更多候选 mapping 的代价计算。

## 先建立直觉

把每条指令想成一次转机：

- **Fast**：只看默认航站楼。到了就走，不管下一段。
- **Greedy**：对当前航班，把“默认航站楼”和“备选航站楼”都报价：机票本身 + 把当前乘客搬到该航站楼的摆渡车成本。它选当前最便宜的那个，但仍然不看后续航班的全部行程。

```text
当前指令 I，候选 mapping M1/M2：
  cost(I, M1) = cost(I 在 M1 的 bank) + Σ 每个操作数搬到 M1 bank 的 copy 成本
  cost(I, M2) = ...
  选 min(cost) —— 只对 I 局部最小
```

所以 Greedy 的“赢”通常发生在**定义侧**：操作数已经在 FPR，备选 mapping 也在 FPR，省掉搬入成本。它的“输”通常发生在**使用侧**：当前指令省了搬入，但结果被很多 GPR 指令使用，每个 use 都要一条 COPY。

## 核心概念

### 公平实验的五个不变量

| 不变量 | 做法 | 违反后的假象 |
| --- | --- | --- |
| 同一份输入 | 先 `-stop-after=legalizer`，再对同一 MIR 跑两次 RBS | IR/legalizer 差异被算成 RBS 差异 |
| 同一 target/CPU | 固定 `-mtriple`、`-mcpu`、`mattr` | 不同 subtarget 的 mapping 不同 |
| 只改一个 flag | Fast 用默认，Greedy 加 `-regbankselect-greedy` | 多变量混淆 |
| 同一验证 | 两边都加 `-verify-machineinstrs`、`-global-isel-abort=1` | 一边失败、一边成功 |
| 同一计数口径 | 只数跨 bank COPY，排除同 bank ABI COPY | COPY 总数虚高 |

### 三个指标

| 指标 | 怎么取 | 说明 |
| --- | --- | --- |
| 跨 bank COPY | `-stop-after` MIR 里逐条看两侧 bank | 最接近 RBS 的代价 |
| 总 COPY | `Select-String '= COPY '` | 包含 ABI 边界，不能直接比较 |
| 编译时间 | `-time-passes` 或重复计时取中位数 | 注意进程启动/解析占比 |

### 跨 bank COPY 的计数口径

以 `or.ll` Fast 为例：

```yaml
%0:fpr(f32) = COPY $s0          # 同 bank：ABI 边界，不计
%1:fpr(f32) = COPY $s1          # 同 bank：ABI 边界，不计
%6:gpr(i32) = COPY %2(i32)      # FPR→GPR：计 1
%7:gpr(i32) = COPY %3(i32)      # FPR→GPR：计 1
$s0 = COPY %5(f32)              # GPR→FPR：计 1（返回值修复）
```

总数是 **3**。如果把 `%0 = COPY $s0` 也算进去，会得到 5，结论就错了。

### `optnone` 是隐藏变量

`RegBankSelect::runOnMachineFunction` 里有（源码依据）：

```cpp
if (F.hasOptNone())
  OptMode = Mode::Fast;
```

它覆盖命令行。`clang -O0` 默认给函数加 `optnone`，所以“clang -O0 下 greedy 无效”现象上成立；但 `llc -O0` 读的 IR 没有 `optnone` 时，`-regbankselect-greedy` 仍然生效。Ch 0 已实测过，本章把它纳入实验表。

## 底层机制

### Fast 与 Greedy 的分派点

`assignInstr` 里（源码依据）：

```cpp
if (OptMode == RegBankSelect::Mode::Fast) {
  BestMapping = &RBI->getInstrMapping(MI);
  MappingCost DefaultCost = computeMapping(MI, *BestMapping, RepairPts);
  if (DefaultCost == MappingCost::ImpossibleCost())
    return false;
} else {
  RegisterBankInfo::InstructionMappings PossibleMappings =
      RBI->getInstrPossibleMappings(MI);
  if (PossibleMappings.empty())
    return false;
  BestMapping = &findBestMapping(MI, PossibleMappings, RepairPts);
}
```

- **Fast**：只取 `getInstrMapping` 的默认 mapping，然后仍然计算 repair 成本；如果默认 mapping 本身不可行，直接失败。
- **Greedy**：取默认 + alternative mappings，逐个 `computeMapping`，选最小。

`init` 里还有一个重要差异（源码依据）：

```cpp
if (OptMode != Mode::Fast) {
  MBFI = &getAnalysis<MachineBlockFrequencyInfoWrapperPass>().getMBFI();
  MBPI = &getAnalysis<MachineBranchProbabilityInfoWrapperPass>().getMBPI();
}
```

Fast 不需要 MBFI/MBPI；Greedy 需要块频率来分析非局部 repair（PHI/terminator 的 split）。

### 代价公式

`RegBankSelect.h` 的注释给出的模型：

```text
cost(I, RegBank) = cost(I.Opcode, RegBank)
                  + Σ costCrossCopy(arg.RegBank, RegBank)
```

`MappingCost` 分成 `LocalCost` 和 `NonLocalCost`：大多数 repair 是局部的，不带块频率；PHI 等需要 split 的 repair 用块频率缩放。`computeMapping` 还会在候选代价已经超过当前最优时提前退出。

### AArch64 的 alternative 很少

23.1.4 的 `AArch64RegisterBankInfo::getInstrAlternativeMappings` 只对三种情况返回备选（源码依据）：

| Opcode | 条件 | 备选 |
| --- | --- | --- |
| `G_OR` | 32/64 位，正好 3 个操作数 | GPR / FPR |
| `G_BITCAST` | 32/64 位，正好 2 个操作数 | GPR / FPR / 双向 copy |
| `G_LOAD` | 64 位，正好 2 个操作数 | GPR / FPR |

其他 opcode 走默认空备选。这解释了为什么 `G_ADD`、`G_FADD` 上 Fast/Greedy 往往完全一样：没有可选的第二套 mapping。

### 每条 use 一条 COPY

`repairReg` 在操作数 bank 不匹配时插入 `COPY`，并且是按 operand 做的（源码依据）：

```cpp
MI = MIRBuilder.buildInstrNoInsert(TargetOpcode::COPY)
  .addDef(Dst)
  .addUse(Src);
```

RBS 不做 CSE，也不合并多个 use 的 repair。一个 FPR 结果被 4 条 GPR 指令使用，就会产生 4 条 `COPY`，每条对应一个 use。这是下一节 `greedy_worse` 的核心。

## 实验一：`or.ll`，Fast 3 条、Greedy 0 条

先固定输入：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -stop-after=legalizer or.ll -o or-legal.mir
```

同一份 `or-legal.mir`，只改模式：

```powershell
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  or-legal.mir -o or-fast.mir
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -regbankselect-greedy or-legal.mir -o or-greedy.mir
```

Fast 的函数体（实测）：

```yaml
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%6:gpr(i32) = COPY %2(i32)
%7:gpr(i32) = COPY %3(i32)
%4:gpr(i32) = G_OR %6, %7
%5:gpr(f32) = G_BITCAST %4(i32)
$s0 = COPY %5(f32)
```

Greedy 的函数体（实测）：

```yaml
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%4:fpr(i32) = G_OR %2, %3
%5:fpr(f32) = G_BITCAST %4(i32)
$s0 = COPY %5(f32)
```

| 模式 | `G_OR` bank | 操作数修复 | 返回值修复 | 跨 bank COPY |
| --- | --- | --- | --- | --- |
| Fast | `gpr` | 2（`%6`、`%7`） | 1（`$s0 = COPY %5`） | **3** |
| Greedy | `fpr` | 0 | 0 | **0** |

为什么 Greedy 能赢：`G_OR` 的两个操作数来自 FPR 的 `G_BITCAST`，结果又被 FPR 的 `G_BITCAST` 使用。备选 FPR mapping 的局部代价是 0；默认 GPR mapping 要搬入 2 个操作数，还要为返回值搬出 1 个。

## 实验二：Greedy 反而更差

```llvm
define i32 @greedy_worse(float %a, float %b, i32 %x) {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %u1 = add i32 %r, %x
  %u2 = xor i32 %u1, %r
  %u3 = sub i32 %u2, %r
  %u4 = mul i32 %u3, %r
  ret i32 %u4
}
```

同样先 legalize，再对同一 MIR 跑两种模式。Fast（实测）：

```yaml
%3:fpr(i32) = G_BITCAST %0(f32)
%4:fpr(i32) = G_BITCAST %1(f32)
%10:gpr(i32) = COPY %3(i32)
%11:gpr(i32) = COPY %4(i32)
%5:gpr(i32) = G_OR %10, %11
%6:gpr(i32) = G_ADD %5, %2
%7:gpr(i32) = G_XOR %6, %5
%8:gpr(i32) = G_SUB %7, %5
%9:gpr(i32) = G_MUL %8, %5
```

Greedy（实测）：

```yaml
%3:fpr(i32) = G_BITCAST %0(f32)
%4:fpr(i32) = G_BITCAST %1(f32)
%5:fpr(i32) = G_OR %3, %4
%10:gpr(i32) = COPY %5(i32)
%6:gpr(i32) = G_ADD %10, %2
%11:gpr(i32) = COPY %5(i32)
%7:gpr(i32) = G_XOR %6, %11
%12:gpr(i32) = COPY %5(i32)
%8:gpr(i32) = G_SUB %7, %12
%13:gpr(i32) = COPY %5(i32)
%9:gpr(i32) = G_MUL %8, %13
```

| 模式 | `G_OR` bank | 搬入成本 | 搬出/use 成本 | 跨 bank COPY |
| --- | --- | --- | --- | --- |
| Fast | `gpr` | 2 | 0 | **2** |
| Greedy | `fpr` | 0 | 4（`%10`–`%13`） | **4** |

Greedy 在 `G_OR` 这一条上确实更便宜：操作数都在 FPR，省了 2 条搬入。但它没有看 `%r` 后面被 4 条 GPR 指令使用；每个 use 各插一条 COPY，总共 4 条。Fast 虽然搬入 2 条，但结果直接留在 GPR，后续 4 个 use 都不需要修复。

这就是“局部贪心”的边界：**它优化当前指令，不优化当前值的所有使用。**

## 实验三：`optnone`、无 alternative 与编译时间

### `optnone` 与“没有 alternative”

同一份 `or.ll` 加上 `optnone`：

```llvm
define float @or_f(float %a, float %b) #0 {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
attributes #0 = { optnone noinline }
```

| 场景 | Fast | Greedy | 说明 |
| --- | --- | --- | --- |
| `or.ll`（无 `optnone`） | 3 | 0 | alternative 生效 |
| `greedy_worse.ll` | 2 | 4 | 局部贪心盲区 |
| `or.ll` + `optnone` | 3 | 3 | `F.hasOptNone()` 强制 Fast |
| `add.ll`（`G_ADD`） | 相同 | 相同 | 没有 alternative |
| `G_OR` s16 / >64 位 | 相同 | 相同 | 源码 `Size != 32 && Size != 64` 时无备选（源码依据） |

`optnone` 的两行输出逐行相同；`-regbankselect-greedy` 没有报错，只是被函数属性覆盖了。

### 编译时间：从全 pipeline 到 pass-only

构造 3000 个独立的 `or_f` 函数，先 legalize 成一份 MIR：

```powershell
# 生成 big3k.ll 后
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -stop-after=legalizer big3k.ll -o big3k-legal.mir
```

实测输入是 **6,751,060 字节**。在同一台机器上交替跑 10 轮 Fast/Greedy，取中位数：

| 输入 | 函数数 | legal MIR | Fast 中位 | Greedy 中位 | 比值 |
| --- | --- | --- | --- | --- | --- |
| `big3k` | 3000 | 6.75 MB | 1,678 ms | 1,708 ms | 1.02 |

这个数字**不能**读成“Greedy 只慢 2%”。它包含 MIR 解析、pass 运行、MIR 打印，解析/打印占了大头。用 `-time-passes` 单独看 RBS：

| 模式 | 8 次中位数 | 观察到的范围 |
| --- | --- | --- |
| Fast | 0.026 s | 0.020–0.041 s |
| Greedy | 0.040 s | 0.022–0.052 s |

样本小、计时器分辨率粗，只能说明“Greedy 做了更多工作”，不能当成稳定的 1.5×。要得到可信数字，用更大的输入、更多轮次、`-o NUL` 去掉打印，并报告中位数和分布。

```powershell
$t=[Diagnostics.Stopwatch]::StartNew()
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -o NUL big3k-legal.mir
$t.Stop(); $t.Elapsed.TotalMilliseconds
```

## 对比与取舍

| 维度 | Fast | Greedy |
| --- | --- | --- |
| 候选 mapping | 默认 1 个 | 默认 + alternatives |
| 依赖分析 | 无 MBFI/MBPI | MBFI + MBPI |
| 输出质量 | 可预测，可能多 COPY | 局部更优，可能更差 |
| 编译时间 | 更低 | 更高 |
| 默认 | AArch64 `new RegBankSelect()` | 需 `-regbankselect-greedy` 或自定义 pass config |
| 适合 | 默认管线、调试构建 | 有明确 alternative 的热点、实验对比 |

选择建议：

- **先测 Fast 的 COPY 数**，再决定是否值得试 Greedy。没有 alternative 的代码，Greedy 不会改变 bank。
- **不要全局打开 Greedy** 作为“优化开关”。它可能把结果推到 FPR，增加大量 GPR use 的 COPY。
- **用 MIR 计数，不要用汇编猜**。汇编里的 `fmov` 可能来自 ABI、copy coalescing 或 RBS，来源不同。

## 踩雷集锦

1. **两份不同 IR 对比**：legalizer 已经改变了指令和块。改正：先 `-stop-after=legalizer`，只对同一 MIR 跑两次 RBS。
2. **数所有 COPY**：ABI 的 `%0:fpr = COPY $s0` 会把 Fast 的 3 条虚增到 5 条。改正：只数两侧 bank 不同的 COPY。
3. **假设 Greedy 永不更差**：`greedy_worse` 实测 2→4。改正：同时看 def 侧和 use 侧。
4. **把 `-O0` 当成原因**：强制 Fast 的是 `optnone`。改正：先看 IR 有没有 `attributes #0 = { optnone ... }`。
5. **只跑一次计时**：PowerShell 启动、磁盘缓存、MIR 解析都会淹没 pass 时间。改正：交替多轮、取中位数、用 `-time-passes`。

## 进阶

- **`-global-isel-abort=2 -pass-remarks-missed=gisel-regbankselect`**：Greedy 候选全部不可行时，先看 remark 指向哪条指令，再回到 Ch 18 的诊断树。
- **`-debug-only=regbankselect`（assertions build）**：会打印每个候选 mapping 的代价和 `New best:`；这是验证“为什么选 FPR”最直接的证据（`guided`）。
- **PHI/循环**：给 `greedy_worse` 加一个循环，把 `%r` 的使用放到高频块，观察 `NonLocalCost` 与块频率的影响（`guided`）。
- **基准方法**：`hyperfine`、`perf stat`、CTMark；固定 CPU、关闭 turbo、报告分布而不是单点。

## 动手练习

1. 复现 `or.ll` 的 3 vs 0，并逐条指出 3 条 COPY 的来源。
2. 把 `greedy_worse` 的 GPR use 改成 1、2、4、8 个，先预测 Greedy 的 COPY 数，再实测。
3. 给 `or.ll` 加 `optnone`，确认 Fast/Greedy 都是 3 条。
4. 用 300 或 3000 个函数测 Fast/Greedy，报告 median、min、max，并解释为什么总时间比 `-time-passes` 平稳。
5. （`guided`）把 `or.ll` 改成 `and`/`xor`，先查 AArch64 alternative 表，预测 Greedy 是否仍然 0 条。

## 本章重点整理

- 公平比较 = 同一份 legalized MIR + 同一 target/flag，只改 `-regbankselect-greedy`。
- `or.ll`：Fast 3 条跨 bank COPY，Greedy 0 条；第 3 条是返回值修复。
- `greedy_worse`：Fast 2 条，Greedy 4 条；Greedy 局部省搬入，却为 4 个 use 各插一条 COPY。
- AArch64 的 alternative 只有 `G_OR`、`G_BITCAST`、`G_LOAD` 的特定形式；没有 alternative 时两种模式相同。
- `optnone` 强制 Fast；`-O0` 本身不强制。
- 编译时间要用中位数和分布，不要把进程启动/解析时间当成 pass 时间。

## 自我检核

- [ ] 能写出公平比较的三条命令（legalize、Fast、Greedy）
- [ ] 能解释 `or.ll` 的 3 条 COPY 分别来自哪里
- [ ] 能解释为什么 Greedy 在 `greedy_worse` 上更差
- [ ] 能说出 AArch64 23.1.4 的三类 alternative
- [ ] 能区分 `optnone` 与 `-O0`
- [ ] 能设计一个报告 median/min/max 的计时实验

## 延伸阅读

- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：文件顶部的 cost 公式、`MappingCost`、`RepairingPlacement`。
  - **学什么**：local/non-local cost 与块频率的关系。
  - **和本章的关联**：解释 Fast/Greedy 的代价模型与 `greedy_worse`。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`assignInstr`、`findBestMapping`、`computeMapping`、`repairReg`。
  - **学什么**：Greedy 如何遍历候选、如何插入 COPY。
  - **和本章的关联**：实验二 4 条 COPY 的源码路径。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrAlternativeMappings` 与 `copyCost`。
  - **学什么**：哪些 opcode/位宽有备选，GPR↔FPR 的 5/4 成本。
  - **和本章的关联**：`or.ll` 能选 FPR 的前提。
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Fast/Greedy 两段的定义。
  - **学什么**：官方对两种模式的描述；注意文档说 Fast 是 `-O0` 默认，源码里 AArch64 pass config 始终 `new RegBankSelect()`。
  - **和本章的关联**：实验设计的背景。
- **[LLVM Benchmarking Tips](https://llvm.org/docs/Benchmarking.html)**
  - **读哪里**：测量、重复、报告分布的部分。
  - **学什么**：为什么单次计时不可信。
  - **和本章的关联**：实验三编译时间部分的方法论。

下一章把“结果不对”拆成可执行的诊断树：`unable to map instruction`、缺 bank、COPY 爆炸、verifier 报错，以及 fallback 到 SelectionDAG。

→ [Ch 18 常见失败与诊断树](./18-failure-diagnosis.md)
