# Ch 2 — pass 契约、MIR 属性与失败模式

> **目标**：把 RegBankSelect 当成一个有明确输入输出契约的 pass 来用。学完你能读懂 MIR 头部的 `legalized` / `regBankSelected` / `selected` / `failedISel` 等字段，能说清 `-stop-after` 与 `-run-pass` 的区别，能区分“失败”、“静默 fallback”和“看起来成功但其实失败了”三种结局。
>
> **环境**：LLVM 23.1.4 官方预编译包（Optimized build，实测）；源码行来自 `llvm-23.1.4-src`（源码依据）。
>
> **证据约定**：本章所有 MIR 属性表、命令输出、错误文本都在本机 23.1.4 上跑过（实测）。涉及 assertions build 的结论明确标注，不假装跑过。

## 为什么需要这个？

写 GlobalISel 相关代码时，有三个问题会反复出现：

1. **“我这份 MIR 能喂给 RegBankSelect 吗？”** 能，但可能不是你想象的那种“能”。
2. **“这份 MIR 是 RBS 真的跑过，还是我拿错了阶段？”** 属性字段能回答。
3. **“RBS 失败了会怎样？”** 在你没有显式要求 abort 的情况下，答案是“几乎看不出”。

第 3 点尤其危险。RegBankSelect 失败会置 `failedISel`，然后：

- 在 `-run-pass` 里：进程 exit 0，MIR 照常写出，只是多了一个 `failedISel: true`；
- 在全管线里：`ResetMachineFunction` 把函数重置，SelectionDAG 接手，汇编照样生成。

也就是说，“编译成功”完全不能证明 RegBankSelect 成功。本章的实测会把这三条路都走一遍。

## 先建立直觉

把 pass 想成过关：

- **`getRequiredProperties()`** 是入境要求（“必须已经合法化”）。
- **`getClearedProperties()`** 是出境时作废的章（“我不再保证没有 PHI”）。
- **`getSetProperties()`** 是出境时新盖的章（“bank 已选”）。
- **`failedISel`** 是“本次通关失败”的红章，盖上去就不会自己消失。

```text
    irtranslator            legalizer             regbankselect          instruction-select
  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐       ┌──────────────┐
  │ legalized  = F│  →   │ legalized  = T│  →   │ legalized  = T│   →   │ legalized  = T│
  │ regBankSel = F│      │ regBankSel = F│      │ regBankSel = T│       │ regBankSel = T│
  │ selected   = F│      │ selected   = F│      │ selected   = F│       │ selected   = T│
  └──────────────┘      └──────────────┘      └──────────────┘       └──────────────┘
                                  ↑                    ↑
                            要求 IsSSA+Legalized   置 RegBankSelected / 清 NoPHIs
```

一个容易踩的细节：这些属性是**正向表述**的（`MachineFunction.h:145-146` 原文：“The properties are stated in "positive" form”）。`noPhis: false` 不代表“有 PHI”，只代表“没有人断言过它没有 PHI”。属性和事实不是一回事。

## 核心概念

### RBS 的三件套

```cpp
// llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h:624-634
MachineFunctionProperties getRequiredProperties() const override {
  return MachineFunctionProperties().setIsSSA().setLegalized();
}

MachineFunctionProperties getSetProperties() const override {
  return MachineFunctionProperties().setRegBankSelected();
}

MachineFunctionProperties getClearedProperties() const override {
  return MachineFunctionProperties().setNoPHIs();
}
```

| 钩子 | 内容 | 含义 |
| --- | --- | --- |
| required | `IsSSA`, `Legalized` | 输入必须是 SSA 形式、且已合法化 |
| set | `RegBankSelected` | 输出保证所有 generic vreg 都有 bank |
| cleared | `NoPHIs` | 不承诺输出没有 PHI（repair 可能拆边） |

“repair 可能拆边”不是猜测：`RegBankSelect.cpp:952-963` 的 `EdgeInsertPoint::materialize()` 直接调用 `Src.SplitCriticalEdge(DstOrSplit, P)`。拆边会新建基本块，也就可能引入新的 PHI，所以它必须撤掉 `NoPHIs` 这个保证。

### MIR 头部的字段

| 字段 | MIR 名 | 谁写 | 怎么读 |
| --- | --- | --- | --- |
| `Legalized` | `legalized:` | Legalizer | true 表示“每条指令都合法”，不代表“MIR 变过” |
| `RegBankSelected` | `regBankSelected:` | RegBankSelect | true 表示“跑过”，**不表示“成功”** |
| `Selected` | `selected:` | InstructionSelect | true 表示 generic 指令已消掉、vreg 已约束成 regclass |
| `FailedISel` | `failedISel:` | 任何 `reportGISelFailure` | true 表示本函数已放弃 GlobalISel |
| `NoPHIs` | `noPhis:` | 各 pass 的 cleared 集合 | false 只表示“没有断言”，不表示“有 PHI” |
| `IsSSA` | `isSSA:` | IRTranslator 等 | RBS 的硬要求之一 |

`MachineFunction.h:163-176` 对前三个的定义值得抄下来：

- `Legalized`：MachineLegalizer 跑过，所有 pre-isel generic 指令都合法（要么 generic 且永远合法如 `COPY`，要么 target-specific，要么是合法的 pre-isel generic）。
- `RegBankSelected`：RegBankSelect 跑过，所有 generic vreg 都被分配了 bank。
- `Selected`：InstructionSelect 跑过，所有 pre-isel generic 指令都被消掉；因此所有 generic vreg 都被约束成 regclass，附加的 size 也被清掉。

## 底层机制

### 属性是在 pass 框架里统一处理的

pass 自己不写 `MF.getProperties().setRegBankSelected()`，而是由 `MachineFunctionPass` 在调用前后统一处理：

```cpp
// llvm/include/llvm/CodeGen/MachineFunctionPass.h:36-38
RequiredProperties = getRequiredProperties();
SetProperties = getSetProperties();
ClearedProperties = getClearedProperties();
```

```cpp
// llvm/lib/CodeGen/MachineFunctionPass.cpp:61-72（节选）
#ifndef NDEBUG
  if (!MFProps.verifyRequiredProperties(RequiredProperties)) {
    errs() << "MachineFunctionProperties required by " << getPassName()
           << " pass are not met by function " << F.getName() << ".\n"
           << "Required properties: ";
    RequiredProperties.print(errs());
    errs() << "\nCurrent properties: ";
    MFProps.print(errs());
    errs() << "\n";
    reportFatalUsageError("MachineFunctionProperties check failed");
  }
#endif
```

```cpp
// llvm/lib/CodeGen/MachineFunctionPass.cpp:85 与 :121
MFProps.reset(ClearedProperties);   // 进入 pass 前清除
...
MFProps.set(SetProperties);         // 跑完后置位
```

**注意 `#ifndef NDEBUG`**。官方预编译包是 Optimized build，`NDEBUG` 已定义，这段检查被整个编译掉。所以在官方包上，`legalized: false` 的 MIR 喂给 `-run-pass=regbankselect` 不会报“属性不满足”，它会直接跑。

### runOnMachineFunction 做了什么

```cpp
// llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:750-771
bool RegBankSelect::runOnMachineFunction(MachineFunction &MF) {
  // If the ISel pipeline failed, do not bother running that pass.
  if (MF.getProperties().hasFailedISel())
    return false;

  LLVM_DEBUG(dbgs() << "Assign register banks for: " << MF.getName() << '\n');
  const Function &F = MF.getFunction();
  Mode SaveOptMode = OptMode;
  if (F.hasOptNone())
    OptMode = Mode::Fast;
  init(MF);

#ifndef NDEBUG
  if (!checkFunctionIsLegal(MF))
    return false;
#endif

  assignRegisterBanks(MF);

  OptMode = SaveOptMode;
  return false;
}
```

四个要点：

1. **`failedISel` 是粘性的**：上游任何一个 pass 失败过，RBS 直接 no-op。这是 Ch 18 诊断树的起点——如果你看到 `failedISel: true` 而 RBS 什么都没做，问题在上游。
2. **`optnone` 强制 Fast**：注意它改的是成员 `OptMode`，跑完再恢复，所以只影响当前函数。
3. **`checkFunctionIsLegal` 只在 assertions build 生效**（`737-748` 整个函数体在 `#ifndef NDEBUG` 里），而且还能被 `-disable-gisel-legality-check` 关掉（`LegalizerInfo.cpp:32-35`）。
4. **永远返回 `false`**。这是 `MachineFunctionPass` 的“是否修改了 IR”返回值，RBS 明明改了 MIR 却返回 false。不要用它判断“有没有干活”。

### 失败路径

一条指令映射失败时：

```cpp
// llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:726-730
if (!assignInstr(MI)) {
  reportGISelFailure(MF, *MORE, "gisel-regbankselect",
                     "unable to map instruction", MI);
  return false;
}
```

`reportGISelFailure` 的第一件事是打红章：

```cpp
// llvm/lib/CodeGen/GlobalISel/Utils.cpp:261-266
void llvm::reportGISelFailure(MachineFunction &MF,
                              MachineOptimizationRemarkEmitter &MORE,
                              MachineOptimizationRemarkMissed &R) {
  MF.getProperties().setFailedISel();
  reportGISelDiagnostic(DS_Error, MF, MORE, R);
}
```

然后 `reportGISelDiagnostic` 根据 `MF.getTarget().Options.GlobalISelAbort` 决定是致命错误还是只发一条 remark（`Utils.cpp:237-253`）。

### 三种 abort 模式

```cpp
// llvm/lib/CodeGen/TargetPassConfig.cpp:180-188（节选）
static cl::opt<GlobalISelAbortMode> EnableGlobalISelAbort(
    "global-isel-abort", cl::Hidden,
    cl::desc("Enable abort calls when \"global\" instruction selection "
             "fails to lower/select an instruction"),
    cl::values(
        clEnumValN(GlobalISelAbortMode::Disable, "0", "Disable the abort"),
        clEnumValN(GlobalISelAbortMode::Enable, "1", "Enable the abort"),
        clEnumValN(GlobalISelAbortMode::DisableWithDiag, "2",
                   "Disable the abort but emit a diagnostic on failure")));
```

| 取值 | 名字 | 行为 | 有没有 fallback |
| --- | --- | --- | --- |
| `0` | Disable | 不 abort，默认不打印诊断 | 有（全管线静默退回 SelectionDAG） |
| `1` | Enable | 失败即 `reportFatalUsageError` | **没有**，SDAG 都不挂 |
| `2` | DisableWithDiag | 不 abort，但发 remark | 有 |

### 全管线里的 fallback

```cpp
// llvm/lib/CodeGen/TargetPassConfig.cpp:1061-1071（节选）
// Pass to reset the MachineFunction if the ISel failed. Outside of the above
// if so that the verifier is not added to it.
if (Selector == SelectorType::GlobalISel)
  addPass(createResetMachineFunctionPass(
      reportDiagnosticWhenGlobalISelFallback(), isGlobalISelAbortEnabled()));

// Run the SDAG InstSelector, providing a fallback path when we do not want to
// abort on not-yet-supported input.
if (Selector != SelectorType::GlobalISel || !isGlobalISelAbortEnabled())
  if (addInstSelector())
    return true;
```

`ResetMachineFunctionPass` 对失败函数做的是“整份重来”：`MF.reset()` + 重新初始化（`ResetMachineFunctionPass.cpp:63-85`），把 `failedISel` 清掉，让 SelectionDAG 从 IR 重新生成 MIR。所以 fallback 之后你看到的是**完全不同的 MIR**，不是“RBS 处理了一半的结果”。

## 示例一：四个阶段的属性（实测）

同一条 `mixp.ll`，四个 `-stop-after` 的头部字段：

| 阶段 | legalized | regBankSelected | selected | failedISel | noPhis | isSSA |
| --- | --- | --- | --- | --- | --- | --- |
| irtranslator | false | false | false | false | false | true |
| legalizer | **true** | false | false | false | false | true |
| regbankselect | true | **true** | false | false | false | true |
| instruction-select | true | true | **true** | false | false | true |

两个观察：

- **`noPhis` 全程是 false**，即使 `mixp` 里根本没有 PHI。这再次说明它是“断言”而非“事实”。想看 `noPhis` 为 true 的效果，得找真实的 PHI 消除 pass。
- **`legalized` 只跳一次**：从 false 到 true 发生在 legalizer 之后，此后不再变。这条单调性是判断“我是不是拿错了阶段”的最快方法。

## 示例二：`-stop-after` 与 `-run-pass`（实测）

```powershell
# 路线 A：一次编译，停在 RBS 之后
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -stop-after=regbankselect mixp.ll -o mixp-rbs-full.mir

# 路线 B：先把 legalized MIR 存下来，只重跑 RBS
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -stop-after=legalizer mixp.ll -o mixp-legal.mir
& $llc -mtriple=aarch64-linux-gnu -O0 `
  -run-pass=regbankselect mixp-legal.mir -o mixp-rbs-only.mir
```

直接 `diff` 两个文件，会看到三处差异：

| 差异 | 路线 A | 路线 B | 原因 |
| --- | --- | --- | --- |
| `; ModuleID` | `mixp.ll` | `mixp-legal.mir` | MIR 里嵌了输入文件路径 |
| 基本块名 | `bb.1.entry` | `bb.0.entry` | MIR 往返重新编号 |
| `maxCallFrameSize` | `4294967295` | `0` | YAML 默认值的打印差异 |

但**指令体逐字节相同**：

```text
%0:fpr(f32) = COPY $s0
%1:gpr(i32) = COPY $w0
%2:gpr(i32) = G_CONSTANT i32 1
%3:gpr(i32) = G_ADD %1, %2
%4:fpr(f32) = G_SITOFP %3(i32)
%5:fpr(f32) = G_FADD %4, %0
$s0 = COPY %5(f32)
```

结论：比对时不要整文件 `diff`，要么只比 `body:` 之后的行，要么先把 Metadata 归一化。这一点在 CI 里做 MIR 回归时有实际影响。

还有一个容易忽略的差别：**`-run-pass` 只跑你点名的 pass**，不会有 `addPreRegBankSelect()` 里的 combiner/lowering，也不会有 `-O0` 的默认管线。所以路线 B 的输入必须是“已经过 target 预处理”的 MIR，否则你会得到一个和真实管线不同的结果。两种路线结果一致，恰恰说明本例中那几段组合器没有改变任何东西。

## 示例三：失败长什么样（实测）

构造一份“合法 MIR 但有一条无法映射的指令”：在 legalized MIR 里插入一行 `i768` 的 `G_ANYEXT`（AArch64 的 GPR 最多到 128 位、FPR 到 512 位，768 位没有映射）。

```text
; badmap.mir（节选）
%9:_(i768) = G_ANYEXT %1(i32)
```

**情况 1：不加任何 flag**

```console
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=regbankselect badmap.mir -o out.mir
$ echo $?
0
```

stderr 一个字都没有，输出的 MIR 头部是：

```yaml
legalized:       true
regBankSelected: true
failedISel:      true
```

注意 `regBankSelected: true` 与 `failedISel: true` **同时存在**。原因在 `MachineFunctionPass.cpp:121`：`SetProperties` 是在 `runOnMachineFunction` 返回之后无条件置位的，不管它成功与否。

**情况 2：`-global-isel-abort=2` + 打开 remark**

```console
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=regbankselect \
      -global-isel-abort=2 -pass-remarks-missed=gisel-regbankselect \
      badmap.mir -o NUL
remark: <unknown>:0:0: unable to map instruction: %6:_(i768) = G_ANYEXT %1:gpr(i32) (in function: mixp)
```

exit code 仍是 0。

**情况 3：`-global-isel-abort=1`**

```console
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=regbankselect \
      -global-isel-abort=1 badmap.mir -o NUL
LLVM ERROR: unable to map instruction: %6:_(i768) = G_ANYEXT %1:gpr(i32) (in function: mixp)
```

exit code 1，后面跟栈回溯。

三者对比：

| 配置 | 退出码 | stderr | MIR 里的证据 | 适合 |
| --- | --- | --- | --- | --- |
| 无 flag | 0 | 空 | `failedISel: true` | 复现“静默失败” |
| `-global-isel-abort=2` + remarks | 0 | remark | 同上 | 批量扫描哪些函数失败 |
| `-global-isel-abort=1` | 1 | `LLVM ERROR` | 不产出 | 调试、CI 门禁 |

## 对比与取舍

### `-stop-after` vs `-run-pass`

| 维度 | `-stop-after=regbankselect` | `-run-pass=regbankselect` |
| --- | --- | --- |
| 输入 | LLVM IR | MIR |
| 跑多少 pass | 整条管线直到 RBS | 只有 RBS |
| 是否含 target 预处理 | 是（combiner/lowering） | 否 |
| 适合 | 看真实管线结果 | 固定输入、反复实验、单变量对比 |
| 输出稳定性 | 受前面 pass 影响 | 只受这份 MIR 影响 |

正确的用法是**两条都用**：用 `-stop-after` 得到“真实结果”，用 `-run-pass` 做“可重复实验”。Ch 17 的 Fast/Greedy 公平对比就依赖后者。

### abort 模式怎么选

- 日常开发：默认（`0`）。你不想每次未支持指令都崩。
- CI 门禁：`1`。让“悄悄 fallback”变成硬失败，否则 GlobalISel 覆盖率会慢慢退化。
- 大规模排查：`2` + `-pass-remarks-missed=gisel-regbankselect`，收集所有失败函数而不中断编译。

## 踩雷集锦

**1. 在 Optimized build 里指望 RBS 帮你检查 `legalized`**

`MachineFunctionPass.cpp:61-72` 整段在 `#ifndef NDEBUG` 里。实测：把 `-stop-after=irtranslator` 的 MIR（`legalized: false`）直接喂给 `-run-pass=regbankselect`，exit 0，输出 `regBankSelected: true`。它会照常给未合法化的指令贴 bank。想有这层保护，必须自建 assertions 版本。

**2. 把 `regBankSelected: true` 当成“成功”**

示例三已经证明它可以和 `failedISel: true` 并存。判据应该是 `regBankSelected && !failedISel`，而不是只看前者。

**3. 以为 `-global-isel-abort=1` 是“打印更多日志”**

它是 **fatal**：`reportFatalUsageError` 直接结束进程，全管线里连 SelectionDAG fallback 都不挂（`TargetPassConfig.cpp:1069`）。想要“多日志但不中断”，用 `=2` 加 remark。

**4. 整文件 diff `-run-pass` 与 `-stop-after` 的输出**

`ModuleID`、`bb.0` / `bb.1`、`maxCallFrameSize` 都会不同，但指令体相同。只比 `body:` 之后的内容，否则你会误判成“两条路线结果不一致”。

**5. 把 `noPhis: false` 读成“有 PHI”**

属性是正向断言。`noPhis: false` 只是“没有 pass 断言过它没有 PHI”。`mixp` 这种完全没有 PHI 的函数，四个阶段也都是 `noPhis: false`。

## 进阶

- **Fast 模式不请求 MBFI/MBPI**。`RegBankSelect.cpp:94-100` 与 `105-114` 只在非 Fast 时 `addRequired<MachineBlockFrequencyInfoWrapperPass>()` 等。这意味着 `-run-pass=regbankselect` 在 Fast 下不需要块频率分析就能跑，而在 Greedy 下缺了会 assert（`RegBankSelect.cpp:454`）。
- **`getSelectionDAGFallbackAnalysisUsage`**：`RegBankSelect::getAnalysisUsage` 末尾调用它（`RegBankSelect.cpp:113`），实现在 `Utils.cpp:1150-1152`，目前只保留 `StackProtector`。这是 fallback 路径能安全接管的分析前提。
- **`machineFunctionIsIllegal` 的成本**：`checkFunctionIsLegal` 会对整个函数重跑合法性检查（`737-748`），只在 assertions build 里有，所以 release 版 RBS 的编译时间不含这块。
- **属性也会被 `-run-pass` 之外的路径改写**：`ResetMachineFunctionPass` 用 `Props.resetToInitial()` 或 `MF.reset()` 清掉全部状态（`ResetMachineFunctionPass.cpp:72-85`）。调试 fallback 时看到属性“突然全没了”，多半是这个 pass 干的。

## 动手练习

1. 用 `-stop-after=irtranslator` 生成 MIR，直接把 `legalized: false` 改成 `true`，再跑 `-run-pass=regbankselect`。记录：输出里 `legalized` 是什么？这能说明属性字段和“事实”的什么关系？
2. 给一个含循环（有 `phi`）的函数跑四个 `-stop-after`，把属性表填完整。再故意把 `-run-pass=regbankselect` 的输出与 `-stop-after=regbankselect` 的 `body:` 做 diff，验证“只有基本块编号不同”。
3. 用本章的 `badmap` 思路（挑一个超出 bank 尺寸的类型）在你自己机器上复现三种 abort 行为，把退出码与 stderr 记成表。然后回答：如果 CI 只检查退出码，哪一种失败会被漏掉？

## 本章重点整理

- RBS 的契约是 `IsSSA` + `Legalized` 进、`RegBankSelected` 出、`NoPHIs` 清。
- 属性检查在 `#ifndef NDEBUG` 里，Optimized build 完全不检查；`-run-pass` 因此可以跑未合法化的 MIR。
- `failedISel` 是粘性的：上游失败会让 RBS 直接 no-op。
- 失败时 `regBankSelected` 仍会被置 true，判据必须是 `regBankSelected && !failedISel`。
- `-stop-after` 看真实管线，`-run-pass` 做单变量实验；两者输出只有 bookkeeping 差异。
- abort 三档：0 静默 fallback、1 硬失败、2 带诊断的 fallback。

## 自我检核

- [ ] 能说出 RBS 的 required / set / cleared 各是什么，并解释为什么清 `NoPHIs`
- [ ] 能解释为什么 Optimized build 里 `-run-pass=regbankselect` 不检查 `legalized`
- [ ] 能列出 `regBankSelected: true` 仍可能失败的原因
- [ ] 能说清 `-global-isel-abort=0/1/2` 的行为差异
- [ ] 能解释 `-stop-after` 与 `-run-pass` 输出为何不是逐字节相同
- [ ] 知道 fallback 之后函数的 MIR 是重建的，而不是“半成品”

## 延伸阅读

- **[LLVM GlobalISel — Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - **读哪里**：pass 顺序与各阶段的属性说明。
  - **学什么**：官方对 `Legalized` / `RegBankSelected` / `Selected` 的定义。
  - **和本章的关联**：本章的属性表就是这些定义的实测版本。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：Machine Function 的 YAML 字段表，特别是 `legalized`、`regBankSelected`、`selected`、`failedISel`。
  - **学什么**：每个字段的合法取值与默认值；`-run-pass` 的语义。
  - **和本章的关联**：不会读这些字段，示例一的表就无从填起。
- **[MachineFunctionPass.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/MachineFunctionPass.cpp)**
  - **读哪里**：`runOnFunction` 里的属性检查（59-72）、`reset`（85）、`set`（121）。
  - **学什么**：属性是由框架统一处理的，pass 只声明；检查被 `NDEBUG` 包住。
  - **和本章的关联**：踩雷 1 与踩雷 2 的根因都在这里。
- **[TargetPassConfig.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/TargetPassConfig.cpp)**
  - **读哪里**：`-global-isel-abort` 的定义（180-188）、`addCoreISelPasses` 的 fallback 段（1038-1071）。
  - **学什么**：三档 abort 的语义，以及 SelectionDAG 在什么条件下被挂上。
  - **和本章的关联**：示例三与“对比与取舍”的直接依据。
- **[Utils.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/Utils.cpp)**
  - **读哪里**：`reportGISelDiagnostic` 与 `reportGISelFailure`（237-280）。
  - **学什么**：失败时“置 `failedISel` + 发诊断”的两步流程，以及 fatal 的判定条件。
  - **和本章的关联**：解释为什么失败可能完全静默。

下一章往下一层：LLT、regclass、bank 和物理寄存器到底各是什么，为什么同一个 `i32` 能在两个 bank 里出现。

→ [Ch 3 寄存器、寄存器类、bank 与 LLT](./03-registers-banks-and-llt.md)