# Ch 25 — TableGen 质量与 sched model 维护

> **目标**：把「sched model 写得好不好」变成可检查的工程问题。学完你能判断一条指令的 `Sched` 声明是否完整、为什么新增一个 `SchedWrite` 会让所有 `CompleteModel` 的 CPU 报错、`Sched<[]>` 与「没有模型」有什么区别，以及一个可合并的 sched model patch 应该带哪些测试。
>
> **环境**：LLVM 24.0.0git；`llvm-tblgen`；`llc`；`llvm-mca`；源码树 `/home/ubuntu/llvm/trees/explore`；本机临时改过 `RISCVInstrInfoZicbo.td` 验证 patch，验证后已 `git checkout` 还原。所有报错与 mca 数字均为本机实跑。

## 为什么 TableGen 质量值得单独一章

sched model 的 bug 很少表现为崩溃，更多是**静默的错误定价**：

- `Sched<[]>` 让 scheduler 按默认值处理，编译能过，性能不对。
- 新增 `SchedWrite` 没在所有 `CompleteModel` 的 CPU 里映射，`llvm-tblgen` 直接报错，整棵构建挂掉。
- 映射了但 latency/资源写错，`llvm-mca` 能跑，数字不可信。
- 指令的 `mayLoad`/`mayStore` 与 `WriteRes` 的资源不匹配，mca 的 `MayStore` 列与调度行为矛盾。

这些问题的共同点是：**编译器不会告诉你「模型错了」，只会给你一个看似合理的周期数**。所以 sched model 的维护需要一套显式的检查清单，而不是「能编译就算过」。

## 先建立直觉：三层声明

RISC-V 的 sched model 分三层：

```text
第一层：抽象读写类（RISCVSchedule.td）
  def WriteIALU : SchedWrite;
  def ReadIALU  : SchedRead;

第二层：每个 CPU 的资源映射（RISCVSchedSiFive7.td 等）
  def : WriteRes<WriteIALU, [SiFive7PipeAB]> { let Latency = 3; }
  def : ReadAdvance<ReadIALU, 0>;

第三层：指令绑定（RISCVInstrInfo*.td）
  def ADD : ... , Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

三层必须同时成立：

- 第一层定义「有哪些抽象类」。
- 第二层给每个 CPU 定价；`CompleteModel = true` 的模型必须覆盖所有被指令引用的类。
- 第三层把具体指令绑到类上；绑错或漏绑，scheduler 就拿到错误信息。

`Sched<[]>` 是第三层的空绑定：它不引用任何类，所以不会触发第二层的覆盖检查，也不会在 mca 里产生资源压力。**它不是「默认模型」，是「没有模型」。**

## 主线一：`Sched<[]>` 的库存

本机统计（`grep -c 'Sched<\[\]>'`）：

| 文件 | `Sched<[]>` 数量 |
|---|---:|
| `RISCVInstrInfo.td` | 36 |
| `RISCVInstrInfoXCV.td` | 51 |
| `RISCVInstrInfoXSf.td` | 17 |
| `RISCVInstrInfoXSpacemiT.td` | 10 |
| `RISCVInstrInfoZicbo.td` | 7 |
| 全部 `*.td` 合计 | 135 |

这些不全是 bug。有些是：

- **伪指令**：展开后才知道真实指令，例如 `PseudoCCMOVGPRNoX0`。
- **调试/标记指令**：不占真实资源。
- **厂商扩展**：模型作者还没补，先用空声明保证能编译。
- **真正遗漏**：例如 `CBO_ZERO`、`PREFETCH_*`，它们有真实内存副作用，却没有任何 sched class。

判断标准不是「数量」，而是「这条指令在 scheduler/mca 眼里有没有意义」：

- 会发射、占功能单元、有延迟的指令 → 应该有 `Sched`。
- 只存在于 MIR、展开后才有真实指令的伪指令 → 可以空，但要在注释里说明。
- 纯标记/调试 → 可以空。

`CBO_ZERO` 属于第一类。它的 `mayStore = 1`，会写 cache block，但在 `RISCVInstrInfoZicbo.td` 里是：

```tablegen
def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[]>;
```

结果就是第 13 章遇到的：

```text
$ llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+zicboz cbo.s
error: unable to resolve scheduling class for write variant.
note: instruction: cbo.zero (a0)
```

## 主线二：新增 `SchedWrite` 会触发全模型覆盖检查

假设你不想复用 `WriteIALU`，而是给 CBO 定义专用类：

```tablegen
// RISCVSchedule.td
def WriteCBO : SchedWrite;
def ReadCBO  : SchedRead;
```

然后在 SiFive7 模型里映射：

```tablegen
// RISCVSchedSiFive7.td
def : WriteRes<WriteCBO, [SiFive7PipeAB]> { let Latency = 1; }
```

编译会失败。本机真实报错：

```text
Included from RISCV.td:54:
RISCVSchedAndes45.td:226:5: error: Processor does not define resources for WriteCBO
def Andes45Model : SchedMachineModel {
    ^
```

原因不是 `CompleteModel`。`Andes45Model` 在源码里明确写了 `CompleteModel = 0`，但 TableGen 仍然报错：只要该 CPU 能生成引用了 `WriteCBO` 的指令，模型就必须给出 `WriteRes`。`CompleteModel` 只控制「未建模的 opcode 是否报错」，不控制「已声明的 `SchedWrite` 是否有资源」。

这条规则的好处是防止「某个 CPU 悄悄用默认值」；代价是新增类的维护成本高。换句话说，新增 `SchedWrite` 时要更新的是**所有能生成该指令的 CPU 模型**，不只是 `CompleteModel = 1` 的那些。两种策略：

1. **复用现有类**：CBO 复用 `WriteIALU/ReadIALU`，零成本，但语义不精确（CBO 不是 ALU）。
2. **新增专用类**：语义清晰，但要更新所有 complete model，并补 mca 测试。

对上游 patch 来说，策略 2 更常见；对教学/原型，策略 1 足够。

### 为什么 `CompleteModel = 0` 也救不了

`CompleteModel` 的语义容易误解。源码注释说得很清楚：

> Subtargets that define a model for only a subset of instructions ... must clear this bit. Otherwise, the scheduler considers an **unmodelled opcode** to be an error.

它管的是「opcode 没有 sched class」；不管「sched class 有名字但模型没给资源」。后者是 TableGen 的硬错误，和 `CompleteModel` 无关。

用命令确认哪些模型会受影响：

```bash
grep -rn "CompleteModel" llvm/lib/Target/RISCV/RISCVSched*.td
```

本机输出里 14 个模型全部是 `CompleteModel = 0` 或 `false`，但 Andes45 仍然报错。这说明：

- 不要用「我设了 `CompleteModel = 0`」作为不补 `WriteRes` 的理由。
- 新增 `SchedWrite` 前，先 `grep` 哪些 CPU 的 `UnsupportedFeatures` 没有排除该扩展；这些模型都要补。
- 如果只是想修 `lack-sched` 而不想改一圈模型，复用已有类（如 `WriteIALU`）是更小的 diff。

## 主线三：一个最小可验证 patch（guided）

下面的 patch 只改一行，复用 `WriteIALU/ReadIALU`：

```diff
--- a/llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td
+++ b/llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td
@@ -55,7 +55,8 @@ def CBO_INVAL : CBO_r<0b000000000000, "cbo.inval">, Sched<[]>;
 } // Predicates = [HasStdExtZicbom]
 
 let Predicates = [HasStdExtZicboz] in {
-def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[]>;
+def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">,
+               Sched<[WriteIALU, ReadIALU]>;
 } // Predicates = [HasStdExtZicboz]
```

重建：

```bash
source /home/ubuntu/llvm/toolset/env.sh
ninja -C /home/ubuntu/llvm/builds/explore llc llvm-mca
```

验证：

```bash
$MCA -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+zicboz \
     -instruction-info cbo.s
```

本机结果（改后）：

```text
[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      3     0.50           *            cbo.zero (a0)
```

`Latency = 3` 来自 SiFive7 的 `WriteIALU`，`RThroughput = 0.50` 来自 `PipeAB`。Rocket 上（改后）：

```text
 1      1     1.00           *            cbo.zero (a0)
```

`Latency = 1` 是 Rocket 的 `WriteIALU`。而在 `andes-ax25`（`NoSchedModel`）上仍然是：

```text
error: unable to find instruction-level scheduling information
for target triple 'riscv64' and cpu 'andes-ax25'.
```

这三组结果说明：

- `Sched<[...]>` 只解决「指令绑定到哪个类」。
- 具体的 latency/吞吐由 CPU 模型决定。
- `NoSchedModel` 下没有任何模型可用，绑了类也没用。

改完后已还原源码并重建，基线仍保持 `Sched<[]>` 的报错，方便你复现前后对比。

## 主线四：`UnsupportedFeatures` 与 `lack-sched`

有些指令不是「忘了写」，而是模型作者明确标记「这个扩展我不支持」：

```tablegen
let UnsupportedFeatures = [HasVInstructions, HasStdExtZicond, ...];
```

`UnsupportedFeatures` 的作用是让 TableGen 在检查时把相关指令当成「不需要建模」，从而允许 `CompleteModel` 通过。它和 `Sched<[]>` 的区别：

| 机制 | 语义 | mca 行为 |
|---|---|---|
| `Sched<[]>` | 指令没有绑定任何类 | 报 `unable to resolve scheduling class` |
| `UnsupportedFeatures` | 该 CPU 不支持这个扩展 | 相关指令不应出现在该 CPU 的代码里 |
| `Sched<[WriteX]>` + 模型缺失 | 绑定了类，但模型没映射 | TableGen 报错（即使 `CompleteModel = 0`） |

`-skip-unsupported-instructions=lack-sched` 只是让 mca **跳过**无法建模的指令，不是修复模型。跳过之后周期数会偏小，不能当作性能结论。

## 主线五：一个可合并的 sched model patch 应包含什么

以本机 `git log` 里的真实提交为参照：

```text
1d9762a07765 [RISCV] Add scheduling models for sifive-x160 and sifive-x180
63c052008276 [RISCV] Use BufferSize = 0 for ProcResGroup in SiFive7
78b651a2cba9 [RISCV] Fix the pipe used by fmv.x.<fp>/<fp>.x in SiFive7
360fab623d3f [RISCV] Fix IDiv/IRem scheduling data for RV32 cores
```

这些提交的共同点：

- **只改模型/表，不改代码生成逻辑**；commit message 直接说清改哪条指令/哪个资源。
- **带 mca 测试**：`llvm/test/tools/llvm-mca/RISCV/...` 下有完整的 `.test` 文件，覆盖该 CPU 的每类指令。
- **带 lit 回归**：`llvm/test/CodeGen/RISCV/...` 里更新受影响的调度顺序。
- **NFC 拆分**：纯重命名/重构单独提交（如 `77efa4af205f [RISCV][NFC] Simplify the vector pipe names`），不和新模型混在一起。

一个合格的 patch 至少要有：

1. **语义正确**：`WriteRes` 的资源、`Latency`、`NumMicroOps` 与该指令的真实行为一致。
2. **覆盖完整**：所有引用该 `SchedWrite` 的 complete model 都映射了。
3. **测试可复现**：mca 测试覆盖新指令；lit 测试覆盖调度顺序变化。
4. **不破坏旧 CPU**：新增类不能让 `-mcpu=rocket` 等旧模型报错或改变无关输出。

## 踩雷

1. **`Sched<[]>` 能编译，所以最容易被忽略**。它不会触发 complete model 检查，也不会在 mca 里报错（直到你真正去建模它）。
2. **新增 `SchedWrite` 必须更新所有能生成该指令的模型**。本机的 `Processor does not define resources for WriteCBO` 就是活例子；`Andes45Model` 的 `CompleteModel = 0` 也救不了它，只改 SiFive7 不够。
3. **复用 `WriteIALU` 是权宜之计**。CBO 不是 ALU，latency/资源语义都不对；上游 patch 应该定义专用类。
4. **`UnsupportedFeatures` 不是「免费通过」**。它表示该 CPU 不支持这个扩展；如果指令真的在该 CPU 上生成，模型仍然是错的。
5. **`-skip-unsupported-instructions=lack-sched` 会让周期数偏小**。它跳过整条指令，不是给它一个默认延迟。
6. **`NoSchedModel` 下绑类也没用**。`andes-ax25` 的报错是 `unable to find instruction-level scheduling information`，不是 `Sched<[]>` 的问题。
7. **mca 能跑不等于模型正确**。本机 patch 后 u74 的 `cbo.zero` 是 `Latency 3 / RThroughput 0.5`，这只是 `WriteIALU` 的借用值，不是 CBO 的真实成本。

## 动手练习

1. 用 `grep -c 'Sched<\[\]>'` 统计本机 `RISCVInstrInfo*.td`，挑出三个「有真实副作用但空绑定」的指令，说明为什么应该补。
2. **guided**：按主线三的 patch 改 `CBO_ZERO`，重建 `llc llvm-mca`，记录 u74/rocket 的 `-instruction-info`；再在 `andes-ax25` 上验证仍然报错。
3. **guided**：尝试新增 `WriteCBO/ReadCBO`，只改 SiFive7，观察 TableGen 的 `Processor does not define resources for WriteCBO`；然后列出还需要改哪些模型（用 `grep -l 'CompleteModel' RISCVSched*.td` 找）。
4. 给 `CBO_CLEAN`/`CBO_FLUSH`/`CBO_INVAL` 也补 `Sched`，比较 mca 的 `MayStore` 列与指令语义是否一致。
5. 找一个 `Sched<[]>` 的伪指令，解释它为什么可以空绑定；再从 `RISCVExpandPseudo*.cpp` 找到它的展开点。

## 重点整理

- sched model 分三层：抽象读写类、每 CPU 的 `WriteRes/ReadAdvance`、指令的 `Sched` 绑定。
- `Sched<[]>` 是「没有模型」，不是「默认模型」；它绕过 complete model 检查，也绕过 mca 的资源压力。
- 任何能生成该指令的 CPU 模型都必须覆盖它引用的 `SchedWrite`；`CompleteModel = 0` 只放过未建模 opcode，不放过「有类但无资源」。
- `UnsupportedFeatures` 表示 CPU 不支持该扩展，不是模型缺口的遮羞布。
- 复用现有类可以快速修 `lack-sched`，但语义不精确；上游 patch 应定义专用类并补 mca/lit 测试。
- mca 能跑只是第一步；latency/吞吐是否真实，需要对照硬件或厂商文档。

## 自我检核

- [ ] 我能说出 `Sched<[]>`、`UnsupportedFeatures`、`NoSchedModel` 三者的区别。
- [ ] 我能解释为什么新增 `WriteCBO` 会让 `CompleteModel = 0` 的 Andes45 模型也报错。
- [ ] 我能用 `-instruction-info` 验证一条指令的 latency/吞吐，并判断它是否借用了别的类。
- [ ] 我知道一个 sched model patch 至少要带哪些测试。
- [ ] 我能区分「mca 跳过指令」与「模型修好了」。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVSchedule.td`：第一层抽象类；看 `WriteIALU`/`ReadIALU` 的命名约定。
- `llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`：第二层完整模型；看 `WriteRes`/`ReadAdvance`/`UnsupportedFeatures` 的组织方式。
- `llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td`：本章 patch 的位置；对照 `CBO_ZERO` 与 `PREFETCH_*` 的 `Sched<[]>`。
- `llvm/include/llvm/Target/TargetSchedule.td`：`ProcWriteResources`/`ProcReadAdvance` 的字段语义。
- `llvm/test/tools/llvm-mca/RISCV/`：官方 mca 测试的写法；挑一个 `SiFiveX100/integer.test` 看覆盖粒度。
- `llvm/test/CodeGen/RISCV/` 下与 sched model 相关的 lit 测试：看模型改动如何影响调度顺序。

→ [下一章：MIR 与 lit 测试](./26-mir-lit-testing.md)
