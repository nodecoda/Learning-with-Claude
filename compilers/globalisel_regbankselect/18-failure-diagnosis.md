# Ch 18 — 常见失败与诊断树

> **目标**：把“RegBankSelect 结果不对”拆成可执行的诊断顺序：先分清是 mapping 失败、bank 缺失、repair 爆炸、verifier 报错，还是已经 fallback 到 SelectionDAG；再定位到具体指令和具体 mapping。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（Optimized build，实测）；assertions build 的行为标“源码依据”或 `guided`。
>
> **证据约定**：本章的错误串分三类：**实测**（官方包复现）、**源码依据**（只在源码里存在，当前构建不一定触发）、`guided`（需要 assertions build 或完整 pipeline 才能复现）。不把源码里的 assert 文本伪装成实测输出。

## 为什么需要这个？

RegBankSelect 的失败有三种“隐身方式”：

1. **abort 关闭时静默**：`-global-isel-abort=0` 只设 `failedISel`，退出码仍是 0；MIR 里留下活 `_`。
2. **fallback 到 SelectionDAG**：最终汇编可能完全正确，但已经不是 GlobalISel 的产物。
3. **没有失败，只是质量差**：bank 合法、verifier 通过，但 COPY 数量翻倍。

如果不按层排查，很容易把“ABI 边界 COPY”当成 RBS 的锅，或者把“assertions build 才有的 assert”当成 release 行为。本章先建立分类，再给诊断树。

## 先建立直觉

把 RBS 想成海关：

- **输入检查**：MIR 是否 legalized？函数是否已经 `failedISel`？
- **mapping 检查**：这条指令有没有默认/备选 mapping？类型是否被 bank 覆盖？
- **repair 检查**：操作数/定义的 bank 不匹配时，能不能插 COPY？
- **verifier 检查**：插完之后，是否满足 `regBankSelected` 的不变量？
- **fallback 检查**：失败后，pipeline 是不是悄悄换成了 SelectionDAG？

```text
症状
 ├─ 汇编不对 / 崩溃
 │   ├─ failedISel: true ──────────────► 已失败，可能 fallback
 │   ├─ LLVM ERROR: unable to map ─────► 无候选 mapping
 │   └─ Bad machine code ──────────────► verifier 不变量
 └─ 汇编对，但 COPY 多 / 性能差
     ├─ 跨 bank COPY 计数
     ├─ 是否有 alternative
     └─ 是否 use 太多（局部贪心盲区）
```

## 核心概念

### 六类问题

| 类别 | 典型信号 | 失败层 | 是否致命 |
| --- | --- | --- | --- |
| 无 mapping | `unable to map instruction` | `assignInstr` | abort=1 时致命 |
| mapping 非法 | `Invalid instruction mapping` / `Mapping is invalid` | `InstructionMapping::verify` | assertions build 断言 |
| 缺 bank | `must have a bank in a RegBankSelected function` | MachineVerifier | verifier 致命 |
| COPY 爆炸 | COPY 数远高于预期 | repair / 代价模型 | 不致命，质量 bug |
| verifier 其他错误 | 类型、PHI、bank 尺寸、hint | MachineVerifier | verifier 致命 |
| fallback | `failedISel: true` + 最终汇编正常 | pass pipeline | 不致命，但不再是 GlobalISel |

### abort 模式决定“失败长什么样”

| 模式 | 枚举 | 诊断 | 是否继续 | 是否 fallback |
| --- | --- | --- | --- | --- |
| `0` | `Disable` | 无 | 是 | 是（完整 pipeline） |
| `1` | `Enable` | fatal | 否 | 否 |
| `2` | `DisableWithDiag` | remark | 是 | 是 |

`-run-pass=regbankselect` 没有后续 pipeline，所以 `0`/`2` 只会留下失败状态，不会真的跑 SelectionDAG。要看 fallback，必须跑完整 `llc`。

## 底层机制

### `assignInstr` 的四条失败路径

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
assert(BestMapping->verify(MI) && "Invalid instruction mapping");
```

失败可能来自：

1. **Fast 的默认 mapping 不可行**：`computeMapping` 返回 `ImpossibleCost`。原因可能是 mapping 本身 invalid，或某个 repair 的 `copyCost` 是 `UINT_MAX`。
2. **Greedy 没有候选**：`getInstrPossibleMappings` 为空。默认 mapping 都无效时会发生。
3. **Greedy 所有候选都不可行**：`findBestMapping` 找不到 `BestMapping`；abort 关闭时会挑第一个 mapping + `Impossible` repair point，随后 `applyMapping` 返回 false。
4. **`applyMapping` 失败**：repair point 不能 materialize，或 `repairReg` 返回 false。

`BestMapping->verify(MI)` 的断言只在 assertions build 里生效；release 包里你可能直接看到后面的 verifier 错误或错误 MIR。

### 失败如何上报

`assignRegisterBanks`：

```cpp
if (!assignInstr(MI)) {
  reportGISelFailure(MF, *MORE, "gisel-regbankselect",
                     "unable to map instruction", MI);
  return false;
}
```

`reportGISelFailure` 设置 `FailedISel`，然后按 abort 模式决定 fatal 还是 remark。abort=1 时 `reportFatalUsageError` 输出的就是：

```text
LLVM ERROR: unable to map instruction: <MI> (in function: <name>)
```

### fallback 的真实路径

`TargetPassConfig.cpp` 在选择器阶段（源码依据）：

```cpp
if (Selector == SelectorType::GlobalISel)
  addPass(createResetMachineFunctionPass(
      reportDiagnosticWhenGlobalISelFallback(), isGlobalISelAbortEnabled()));

if (Selector != SelectorType::GlobalISel || !isGlobalISelAbortEnabled())
  if (addInstSelector())
    return true;
```

翻成行为：

- `-global-isel-abort=1`：`isGlobalISelAbortEnabled()` 为 true，不添加 SelectionDAG selector；失败就是 fatal。
- `0` / `2`：`ResetMachineFunctionPass` 丢弃失败的 gMIR，`addInstSelector()` 添加 SelectionDAG 选择器，从 IR 重新选。
- `2` 额外让 `reportDiagnosticWhenGlobalISelFallback()` 为 true，发出 fallback 诊断。

所以“最终汇编正常”不能证明 GlobalISel 成功；要看 `failedISel` 或 fallback remark。

### verifier 的错误为什么比 RBS 晚

RBS 只负责“给 bank、插 COPY”。类型不匹配、PHI 类型不一致、bank 太小，往往在 RBS 之后的 `MachineVerifier` 才暴露。典型检查（源码依据）：

```cpp
if (!RegBank && isFunctionRegBankSelected)
  report("Generic virtual register must have a bank in a "
         "RegBankSelected function", MO, MONum);

if (RegBank && Ty.isValid() && ... &&
    RBI->getMaximumSize(RegBank->getID()) < Ty.getSizeInBits())
  report("Register bank is too small for virtual register", MO, MONum);
```

用 `-verify-machineinstrs` 或 `-run-pass=machineverifier` 可以把这些错误提前到 RBS 之后立刻看到。

### `unable to map` 与 `must have a bank` 的区别

- `unable to map instruction`：RBS **尝试映射但找不到可行方案**；诊断带具体指令。
- `must have a bank`：RBS **没有给某个活 vreg 留下 bank**；可能是 RBS 提前失败、输入已经标 `regBankSelected` 但没跑过，或 MIR 是手写的。
- 两者可以同时出现：abort=0 时先 `unable to map`（无诊断），输出里再看到 `_`，verifier 再报 `must have a bank`。

## 实验一：同一条指令，abort 三档给出三种“失败”

先构造一个必然没有 bank 能覆盖的类型。AArch64 的三个 bank 最大覆盖分别是 GPR 64 位、FPR 512 位（`QQQQ`）、CC 32 位，而下面这条 `G_CONSTANT` 产生 `s1024`：

```yaml
---
name:            add_s1024
legalized:       true
isSSA:           true
tracksRegLiveness: true
body:             |
  bb.0:
    %0:_(s1024) = G_CONSTANT i1024 1
    %1:_(s1024) = G_CONSTANT i1024 2
    %2:_(s1024) = G_ADD %0, %1
    RET_ReallyLR
...
```

用 `-run-pass=regbankselect` 分别跑三档（实测）：

```powershell
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -global-isel-abort=0 add_s1024.mir -o abort0.mir
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -global-isel-abort=1 add_s1024.mir -o abort1.mir
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -global-isel-abort=2 -pass-remarks-missed=gisel-regbankselect `
  add_s1024.mir -o abort2.mir
```

`abort=0` 的退出码是 `0`，stderr 为空，但输出的 MIR 头部已经变了：

```yaml
legalized:       true
regBankSelected: true
selected:        false
failedISel:      true
...
registers:
  - { id: 0, class: _, preferred-register: '', flags: [  ] }
  - { id: 1, class: _, preferred-register: '', flags: [  ] }
  - { id: 2, class: _, preferred-register: '', flags: [  ] }
body:             |
  bb.0:
    %0:_(s1024) = G_CONSTANT i1024 1
    %1:_(s1024) = G_CONSTANT i1024 2
    %2:_(s1024) = G_ADD %0, %1
    RET_ReallyLR
```

这三个 `_` 不是“暂时还没打印 bank”，而是**RBS 没有给它们映射**：`%0`、`%1` 是 `G_ADD` 的活操作数，`%2` 在这份 fixture 中是死定义，但同样保持 `_`。`regBankSelected: true` 只说明 pass manager 执行过 RBS，`failedISel: true` 才是这次执行的结论。

`abort=1` 则立刻终止。PowerShell 记录的退出码是 `-1073741795`（即 `0xC000001D`），关键错误只有一行（实测）：

```text
LLVM ERROR: unable to map instruction: %0:_(s1024) = G_CONSTANT i1024 1 (in function: add_s1024)
```

后面还会跟 `PLEASE submit a bug report` 和 stack dump。真正可操作的是第一行：失败点是 `%0`，不是后面的 `G_ADD`。RBS 在第一条无法映射的指令处就停了。

`abort=2` 加上 `-pass-remarks-missed=gisel-regbankselect` 后，退出码回到 `0`，但 stderr 有一行 remark（实测）：

```text
remark: <unknown>:0:0: unable to map instruction: %0:_(s1024) = G_CONSTANT i1024 1 (in function: add_s1024)
```

三档并排看：

| `-global-isel-abort` | 退出码 | 诊断 | 输出 MIR | 完整 pipeline 的下一步 |
| --- | --- | --- | --- | --- |
| `0` | `0` | 无 | `failedISel: true`，活 `_` | Reset + SelectionDAG |
| `1` | `-1073741795` | `LLVM ERROR` | 不产生可用结果 | 无 |
| `2` | `0` | 上述 remark | `failedISel: true`，活 `_` | Reset + SelectionDAG，并报 fallback |

为什么 `s1024` 会没有 mapping？从源码路径看，默认 mapping 必须为每个操作数找到能覆盖其类型的 `ValueMapping`；AArch64 的 GPR/FPR/CC 都没有 1024 位的完整覆盖，`G_CONSTANT` 也不是可以逐段拆分的 copy-like 指令，因此构造不出可行 mapping。这个因果解释是**源码依据**；`unable to map` 的具体指令和三种输出是实测。

## 实验二：区分死 `_` 与未映射的活 `_`

手写一份头部已经声称 `regBankSelected: true`、但函数体全是 `_` 的 MIR：

```yaml
---
name:            unmapped_live
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $w0

    %0:_(s32) = COPY $w0
    %1:_(s32) = G_CONSTANT i32 7
    %2:_(s32) = G_ADD %0, %1
    $w0 = COPY %2(s32)
    RET_ReallyLR implicit $w0
...
```

只跑 verifier：

```powershell
& $llc -mtriple=aarch64-linux-gnu -run-pass=machineverifier `
  unmapped_live.mir -o verify.mir
```

退出码为 `-1073741795`，报 6 个错误。下面只展示其中一条（实测）：

```text
*** Bad machine code: Generic virtual register must have a bank in a RegBankSelected function ***
- function:    unmapped_live
- instruction: %0:_(s32) = COPY $w0
```

为什么是 6 个？verifier 按**使用点**报错：`%0` 在定义和 `G_ADD` 操作数各一次；`%1` 同理；`%2` 在 `G_ADD` 定义和 `$w0 = COPY` 使用各一次，共 6 次。看到一串相同错误时，先按 vreg 去重，不要以为有 6 个独立 bug。

作为对照，让 RBS 真正处理一个死定义：

```yaml
    %2:gpr(s32) = G_ADD %0, %1
    %3:gpr(s32) = G_CONSTANT i32 9   # 死定义
    $w0 = COPY %2(s32)
```

`%3` 没有任何使用，但 RBS 仍然给它 `gpr`（实测）。所以：

| 现象 | 含义 | 要不要修 |
| --- | --- | --- |
| RBS 输出里死 vreg 有 bank | RBS 正常访问了这条指令 | 否 |
| RBS 输出里活 vreg 是 `_` | 该函数映射失败或 MIR 手写不合法 | 是 |
| 输入 MIR 里本来就是 `_` | 还没跑 RBS，正常 | 否，先跑 pass |
| 只有 `DBG_VALUE` 引用的 `_` | 不构成活跃使用，verifier 不按活 vreg 要求 bank | 通常否 |

最后一行来自 MachineVerifier 的“活 vreg”判定，属于**源码依据**；不要仅凭 `DBG_VALUE` 里出现 `_` 就判定 RBS 失败。真正的判定标准是：这个 vreg 是否在非 debug 指令中被定义或使用。

## 实验三：verifier 家族——RBS 没失败，不变量却坏了

下面三份手写 MIR 都在头部声明 `regBankSelected: true`，RBS 根本不会重跑；它们故意破坏不同不变量：

```yaml
# bank_too_small.mir
%0:cc(s128) = IMPLICIT_DEF
# phi_type_mismatch.mir
%3:gpr(s32) = G_PHI %1:gpr(s32), %bb.1, %2:gpr(s64), %bb.2
# assert_sext_bank.mir
%1:fpr(s32) = G_ASSERT_SEXT %0:gpr, 16
```

统一用 `-run-pass=machineverifier` 检查，得到的首行错误（实测）：

| 输入 | 错误串 | 违反的不变量 |
| --- | --- | --- |
| `cc(s128)` | `Register bank is too small for virtual register` | bank 的最大位宽不足 |
| PHI 的入值是 `s32` 与 `s64` | `Generic Instruction G_PHI has operands with incompatible/missing types` | PHI 所有入值类型必须一致 |
| `G_ASSERT_SEXT` 从 gpr 到 fpr | `G_ASSERT_SEXT cannot change register bank` | assert 类指令只能保持 bank |
| `%0:_(s16) = COPY $w0` | `Copy Instruction is illegal with mismatching sizes` | COPY 两端位宽必须一致 |
| `selected: true` 但仍有 `G_ADD` | `Unexpected generic instruction in a Selected function` | Selected 函数不能留 generic 指令 |

`cc(s128)` 的附加信息（实测）：`Register bank CC too small(32) to fit 128-bits`。

这些错误和 `unable to map instruction` 属于不同层：

- `unable to map` 是 RBS **主动报告**“我找不到 mapping”。
- `Bank too small` 是 verifier **事后检查**“你给的 bank 装不下这个类型”。
- 前者通常需要改 mapping/类型；后者通常说明 MIR 手写错了，或者某个 pass 给了非法 bank。
- 两者都可能出现在同一个函数里，但修复顺序应先处理最早的生产者，而不是逐个消 verifier 错误。

## 对比与取舍

### 四种观察方式怎么选

| 方式 | 能看到什么 | 看不到什么 | 适用场景 |
| --- | --- | --- | --- |
| `-stop-after=regbankselect`（IR 输入） | 完整 pipeline 到 RBS 的真实状态 | 后续 ISel/RA 的问题 | 从源码复现 |
| `-run-pass=regbankselect`（MIR 输入） | 单 pass 的确定行为 | fallback、pipeline 交互 | 最小化实验 |
| `-verify-machineinstrs` | RBS 之后立刻发现不变量破坏 | mapping 为什么失败 | 写测试、改 pass |
| 完整 `llc` + abort=0/2 | 是否 fallback、最终汇编 | 中间 bank 细节 | 判断用户可见行为 |

一个常见误区是只用 `-run-pass` 做诊断。它很适合把问题缩小到 RBS，但也切断了 fallback：abort=0 时你只会看到一个 `failedISel: true` 的 MIR，不会看到 SelectionDAG 接手。要回答“为什么最终汇编还能生成”，必须跑完整 pipeline。

### 可执行的诊断顺序

把前面的树落成命令，建议固定为七步：

1. **确认 pass 真的跑了**：MIR 头部是否有 `regBankSelected: true`，函数体是否出现 bank 后缀。
2. **先看 `failedISel`**：它为 `true` 时，后面所有 bank 缺失都可能是同一个根因。
3. **抓第一处主动失败**：abort=1 看 `LLVM ERROR`；abort=2 看 `remark`；abort=0 只能靠 `_` 和 verifier 反推。
4. **对活 `_` 跑 verifier**：用 `-run-pass=machineverifier` 得到具体 vreg 和使用点。
5. **给错误分层**：`unable to map` 查 mapping；`must have a bank` 查失败传播；`too small`/PHI/COPY 查不变量。
6. **回到最早的生产者**：手写 MIR 的错误不要归咎于 RBS；从 IR 生成的错误先看 Legalizer 输出。
7. **最后才跑完整 pipeline**：确认是 fatal、remark，还是已经 fallback 到 SelectionDAG。

对应的“症状 → 下一步”表：

| 首行信号 | 下一步 | 不要做什么 |
| --- | --- | --- |
| `unable to map instruction` | 用 abort=2 拿指令，查该 opcode/类型的 mapping | 不要先改 copyCost |
| `must have a bank` | 查 `failedISel` 和输入是否真的跑过 RBS | 不要手工补 bank 掩盖根因 |
| `too small for virtual register` | 检查 bank 覆盖与 LLVM 类型 | 不要扩大 bank 来掩盖非法 MIR |
| `incompatible/missing types` | 查 PHI 入值与 CFG | 不要怪 Greedy |
| `Copy ... mismatching sizes` | 查 COPY 两端类型/物理寄存器 | 不要跳过 verifier |
| 汇编正常但 `failedISel: true` | 检查 fallback | 不要把结果算作 GlobalISel 成功 |

### fallback 的取舍

`abort=1` 适合开发和 lit 测试：失败必须显式，不能被 SelectionDAG 掩盖。`abort=0` 适合生产环境的“尽量编译”：GlobalISel 不支持时仍能出代码，但性能和覆盖数据会失真。`abort=2` 是两者的折中：继续编译，同时给出一条可搜索的 remark。

从源码看，fallback 不是 RBS 自己完成的。`TargetPassConfig.cpp` 在 abort 未启用时添加 `ResetMachineFunctionPass`，再添加 SelectionDAG 的 `addInstSelector()`；RBS 只负责把 `failedISel` 置位。因此在 `-run-pass=regbankselect` 下永远看不到 fallback；完整 pipeline 会 Reset 掉失败函数并从 IR 重新选择；最终汇编正确只证明“某个选择器成功了”，不证明 GlobalISel 成功了。

## 踩雷集锦

### 1. 把退出码 0 当成成功

`-global-isel-abort=0` 下，`unable to map` 不打印、不中止，退出码仍是 0。正确做法是同时检查 `failedISel`、活 `_`，或在 CI 中用 abort=1。

### 2. 把 `regBankSelected: true` 当成“每条 vreg 都有 bank”

这个属性由 pass 的 `getSetProperties()` 设置，失败路径也会设置。它只表示 pass manager 认为 RBS 执行过。正确做法是检查 `failedISel` 和实际 bank 后缀。

### 3. 看到 `_` 就断定 RBS 有 bug

输入 MIR 可能还没跑 RBS；死定义可能被合法地忽略；只有 debug 使用的 vreg 也不按活 vreg 检查。正确做法是先确认 `_` 对应的 vreg 是否在非 debug 指令中活跃，再跑 verifier。

### 4. 用 verifier 错误反推 mapping 设计

`Bank too small`、PHI 类型不一致、COPY 位宽不匹配，往往来自手写 MIR 或前序 pass，不是 RBS 的 mapping 表。正确做法是按“最早生产者”排序，先修输入，再看 RBS。

### 5. 在 `-run-pass` 下找 fallback

单 pass 运行没有后续 pipeline，`failedISel` 之后什么都不会发生。正确做法是跑完整 `llc`，并用 abort=0/2 区分“静默继续”和“带 remark 继续”。

## 进阶

### assertions build 能看到更多，但别把它当 release 行为

`BestMapping->verify(MI)`、`checkFunctionIsLegal` 等检查在 `NDEBUG` 下被编译掉。官方预编译包是 Optimized build，所以本章的 `Invalid instruction mapping` 只能标为**源码依据**或 `guided`：它需要 assertions build 才可能触发。写上游测试时，不要假设 release 用户也会看到同样的 assert。

### 用 remark 做批量筛查

`-pass-remarks-missed=gisel-regbankselect` 可以把 `unable to map` 变成可 grep 的 remark。对一批 IR 跑 abort=2，统计 remark 的 opcode/类型分布，比逐个 abort=1 崩溃更适合评估 GlobalISel 覆盖率。注意 remark 的 `<unknown>:0:0` 位置信息没有指令行号，仍要回到 MIR 定位。

### 缩小失败用例

从完整 IR 复现后，用 `-stop-after=legalizer` 截取 MIR，再删掉与失败指令无关的块和 vreg。保留 `legalized: true`、`isSSA: true`、`tracksRegLiveness: true` 和必要的 liveins。最小 MIR 既方便贴 bug report，也方便放进 lit 测试。

## 动手练习

1. **复现三种 abort 行为**：用 `add_s1024.mir` 跑 abort=0/1/2，记录退出码、stderr 首行、`failedISel` 和 `registers`，并解释为什么 0 与 2 的 MIR 状态相同但诊断不同。
2. **制造死 `_` 与活 `_`**：从 `unmapped_live.mir` 删除 `%2` 的所有使用，再做一个“删除 `%0` 定义但保留使用”的变体；分别记录 verifier 错误并解释差异。
3. **给错误分层**：从 verifier 家族表中选三个错误，写出错误层、最早生产者和最小修复，至少一个根因不在 RBS。

## 本章重点整理

- `-global-isel-abort` 的 0/1/2 分别对应静默继续、fatal、remark 后继续；只有完整 pipeline 才会真正 fallback。
- `unable to map instruction` 是 RBS 的主动失败，带具体指令；`must have a bank` 是 verifier 对活 vreg 的事后检查。
- `regBankSelected: true` 不代表成功，必须同时看 `failedISel`。
- 死 vreg 可以被 RBS 正常映射；输出里的活 `_` 才是失败信号。

## 自我检核

- [ ] 我能不看笔记说出 abort=0、1、2 的退出码、诊断和 pipeline 行为。
- [ ] 我能解释为什么 `abort=0` 的输出仍有 `regBankSelected: true`。
- [ ] 我能区分 `unable to map instruction` 与 `must have a bank` 的失败层。
- [ ] 我能判断一个 `_` 是死定义、未跑 pass，还是活 vreg 未映射。
- [ ] 我能把 `Bank too small` 和 PHI 类型错误归到 verifier，而不是 RBS mapping。
- [ ] 我知道在 `-run-pass` 下看不到 fallback，并知道该用哪条完整 pipeline 命令验证。

## 延伸阅读

### 1. `llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp`

**读哪里**：`assignInstr`、`assignRegisterBanks`、`reportGISelFailure` 的调用点。**学什么**：失败如何从 `assignInstr` 返回 false、如何设置 `FailedISel`、abort 模式在哪里分叉。**和本章的关联**：本章所有 `unable to map` 行为的上游实现。

### 2. `llvm/lib/CodeGen/MachineVerifier.cpp`

**读哪里**：搜索 `Generic virtual register must have a bank`、`Register bank is too small`、`G_PHI has operands`。**学什么**：`regBankSelected` 函数中的检查顺序，以及“活 vreg”的判定。**和本章的关联**：解释死 `_` 与活 `_` 的后果差异。

### 3. `llvm/lib/CodeGen/TargetPassConfig.cpp`

**读哪里**：搜索 `ResetMachineFunctionPass`、`isGlobalISelAbortEnabled`、`addInstSelector`。**学什么**：fallback 的插入点与 abort 模式对 pipeline 的影响。**和本章的关联**：解释为什么 `failedISel` 之后最终汇编仍可能正常。

### 4. `llvm/test/CodeGen/AArch64/GlobalISel/regbankselect-*.mir`

**读哪里**：带 `-verify-machineinstrs` 和 `-global-isel-abort=1` 的 RUN 行。**学什么**：上游如何把失败或不变量写成可回归的 MIR 测试。**和本章的关联**：下一章把这些模式整理成可复用的测试流程。

---

→ [Ch 19 MIR/lit 测试与上游质量标准](./19-testing-and-upstream-quality.md)
