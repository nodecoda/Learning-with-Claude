# 练习 C：给一条 RISC-V 指令补 `SchedRW`，用 mca 验证

> **目标**：亲手走一遍「模型缺失 → 补 `SchedWrite` → 绑定 `WriteRes` → 挂到指令 → 重建 → mca 验证」的完整链路。对象是 `cbo.zero`（Zicboz），一个当前 `Sched<[]>`、在 `llvm-mca` 里直接报错的真实指令。
>
> **环境**：LLVM 24.0.0git；源码 worktree + 对应构建；`llc`/`llvm-mca` 来自同一次构建。**本练习需要重新构建，属于 guided 实验**：文中的基线输出（补丁前）是本机实测；补丁后的日志不伪造，只给判读标准，以你构建后的输出为准。

## 为什么做这个练习

Ch 12 讲了 sched model 的三层，Ch 13 走了五个模型，Ch 14 教了 mca。但「读」和「改」是两种能力。真正做过一次之后，你会获得三个具体经验：

1. **知道缺失长什么样**：不是编译错误，而是 mca 的 `unable to resolve scheduling class for write variant`，或者 `llc` 里悄悄用默认延迟。
2. **知道要改几个地方**：`SchedWrite` 声明、`WriteRes` 绑定、指令的 `Sched<[...]>`，三处缺一不可。
3. **知道验证的标准**：mca 不再报错只是第一步；还要确认 latency 数字符合你的设计，且没有把别的指令带歪。

这类改动也是 RISC-V backend 上游 PR 的常见形态：新指令加进来时忘了 `SchedRW`，review 里被要求补上，并附一个 mca/lit 测试。

## 第一步：复现现象（本机实测）

`cbo.zero` 是 Zicboz 扩展的「把 cache block 清零」指令。它的定义（`RISCVInstrInfoZicbo.td:58`）：

```tablegen
let Predicates = [HasStdExtZicboz] in {
def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[]>;
}
```

`Sched<[]>` 是空列表：这条指令在 sched model 里没有 write，也没有 read。

用 mca 跑：

```bash
printf 'cbo.zero (a0)\nadd a0, a1, a2\n' > cbo.s
"$MCA" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m,+zicboz -instruction-info cbo.s
```

本机输出：

```text
error: unable to resolve scheduling class for write variant., use -skip-unsupported-instructions=lack-sched to ignore these on the input.
note: instruction: 	cbo.zero	(a0)
```

注意这不是「指令不认识」——汇编器能解析它（否则会报 `instruction requires ...`）。这是**调度类解析失败**。

加上 `-skip-unsupported-instructions=lack-sched` 之后：

```text
Instructions:      100
Total Cycles:      53
Total uOps:        100

[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      3     0.50                        add	a0, a1, a2
```

`cbo.zero` 被**整条丢掉**了，只剩 `add`。这是最危险的形态：如果你不看警告，会得到一份「少了指令」的周期报告，还以为自己测的是完整代码。

### 对照：为什么 `ntl.all` 不报错

```bash
printf 'ntl.all\n' > ntl.s
"$MCA" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -instruction-info ntl.s
```

本机能跑。因为 `ntl.all` 不是独立指令，而是别名：

```tablegen
def : InstAlias<"ntl.all",    (ADD   X0, X0, X5)>;
```

它落到 `ADD` 的调度类，有完整的 `WriteIALU`/`ReadIALU`。**别名自动继承目标指令的调度信息，独立指令不会。**

### 报错来自哪里

mca 在 `InstrBuilder::createInstrDescImpl()` 里为每条指令解析调度类，遇到「variant」类会循环调用 `resolveVariantSchedClass()`：

```cpp
Expected<unsigned> InstrBuilder::getVariantSchedClassID(const MCInst &MCI,
                                                        unsigned SchedClassID) {
  const MCSchedModel &SM = STI.getSchedModel();
  unsigned CPUID = SM.getProcessorID();
  while (SchedClassID && SM.getSchedClassDesc(SchedClassID)->isVariant())
    SchedClassID =
        STI.resolveVariantSchedClass(SchedClassID, &MCI, &MCII, CPUID);

  if (!SchedClassID) {
    return make_error<InstructionError<MCInst>>(
        "unable to resolve scheduling class for write variant.", MCI);
  }
  ...
}
```

`Sched<[]>` 让这条指令的 write 列表为空，variant 解析最终回到 0，于是报错。修复的方向也就明确了：给它一个真实的 `SchedWrite`，让解析能落到一个有效的 `WriteRes` 上。

### 还有多少条这样的指令

补一条之前，先知道面有多大。本机统计（`grep -c "Sched<\[\]>"`，只列非零文件）：

| 文件 | 条数 | 典型指令 |
|---|---|---|
| `RISCVInstrInfo.td` | 36 | `fence`、`fence.i`、`fence.tso`、CSR 相关 |
| `RISCVInstrInfoXCV.td` | 51 | 芯来扩展指令 |
| `RISCVInstrInfoXSf.td` | 17 |  SiFive 自定义指令 |
| `RISCVInstrInfoXSpacemiT.td` | 10 | SpacemiT 自定义 |
| `RISCVInstrInfoZicbo.td` | 7 | 4 条 CBO + 3 条 prefetch |
| 其余 | 1–2 条/文件 | `c.flwsp` 之类的压缩指令变体 |

**不是所有 `Sched<[]>` 都是 bug**。fence 是全局屏障，本来就不该被调度器移动；prefetch 是 hint，建模成 1 拍也无意义。CBO 的特殊之处在于：它有真实的内存副作用（`mayStore=1`），又会被实际生成到代码里，缺模型会让 mca 直接丢掉它。选它做练习，是因为动机清楚、改动小、验证直接。

## 第二步：定位需要改的三个地方

在动手前，先把三层的现状查清楚：

```bash
cd "$LLVM_PROJECT"   # 或你的 task worktree
grep -n "CBO_ZERO" llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td
grep -n "def WriteIALU\|def WriteSTD\|def WriteNop" llvm/lib/Target/RISCV/RISCVSchedule.td | head
grep -n "WriteRes<WriteIALU\|WriteRes<WriteSTD" llvm/lib/Target/RISCV/RISCVSchedSiFive7.td | head
```

你会看到：

- 指令侧：`CBO_ZERO ... Sched<[]>`（空）。
- 符号表：没有 `WriteCBO` 这类现成符号；最接近的是 `WriteSTD`（store double）或 `WriteNop`，但语义都不对。
- 绑定侧：`WriteIALU` 在 SiFive7 上绑 `[PipeAB]`、latency 3；`WriteSTD` 绑 `[SMX60_LS]` 等。

**设计决策**：`cbo.zero` 对 cache block 做操作，不是 ALU，也不是普通 store。直接复用 `WriteSTD` 会让它在调度器眼里变成一条 store，可能影响内存依赖建模（`mayStore=1` 已经由指令属性处理，但 sched write 的资源占用是另一回事）。正确做法是新增一个专用 `SchedWrite`。

## 第三步：设计 `WriteCBO`

你要回答三个问题：

1. **占什么资源？** 真实核上 CBO 通常走 LSU（load/store unit）或专门的 cache 维护通道。最小可行模型：复用现有 LSU 资源。
2. **latency 多少？** CBO 是「发起后不等待完成」的指令，对后续指令的依赖通常是 1（Rocket）到 4（SpacemitX60 的 LSU 延迟）。保守起见，先按 LSU 的 store 延迟给。
3. **需要 read 吗？** `cbo.zero` 读地址寄存器 `rs1`，所以需要一个 `ReadMemBase`。

推荐的模型（以 Rocket 为例）：

```tablegen
// RISCVSchedule.td
def WriteCBO : SchedWrite;    // cache block management operations
```

```tablegen
// RISCVSchedRocket.td，放在 Memory 段附近
def : WriteRes<WriteCBO, [RocketUnitMem]> {
  let Latency = 2;            // 与 WriteLDD 一致；按你的目标核调整
}
```

```tablegen
// RISCVInstrInfoZicbo.td
def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[WriteCBO, ReadMemBase]>;
```

为什么 latency 先给 2：Rocket 的 `WriteLDD` 就是 `let Latency = 2 in { ... }`。CBO 走同一个 LSU，先对齐它，避免引入一个没有依据的新数字。

**不要**直接写 `Sched<[WriteSTD, ReadStoreData, ReadMemBase]>`：`CBO_r` 只有一个 `GPRMemZeroOffset` 操作数，没有 store data，read 列表会对不上。这种错误不会崩，但会让调度器把 base 当成 data，bypass 和依赖都会算错。

### 完整 patch 长什么样

三处改动合起来（行号以你的 worktree 为准）：

```diff
--- a/llvm/lib/Target/RISCV/RISCVSchedule.td
+++ b/llvm/lib/Target/RISCV/RISCVSchedule.td
@@
 def WriteSTD        : SchedWrite;    // Store double-word
+def WriteCBO        : SchedWrite;    // Cache block management operations
 def WriteAtomicB    : SchedWrite;    //Atomic memory operation byte size
```

```diff
--- a/llvm/lib/Target/RISCV/RISCVSchedRocket.td
+++ b/llvm/lib/Target/RISCV/RISCVSchedRocket.td
@@
 // Memory
 def : WriteRes<WriteSTB, [RocketUnitMem]>;
 def : WriteRes<WriteSTH, [RocketUnitMem]>;
 def : WriteRes<WriteSTW, [RocketUnitMem]>;
 def : WriteRes<WriteSTD, [RocketUnitMem]>;
+def : WriteRes<WriteCBO, [RocketUnitMem]> {
+  let Latency = 2;          // 与 WriteLDD 对齐；按目标核调整
+}
 def : WriteRes<WriteFST32, [RocketUnitFPALU]>;
```

```diff
--- a/llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td
+++ b/llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td
@@
 let Predicates = [HasStdExtZicbom] in {
-def CBO_CLEAN : CBO_r<0b000000000001, "cbo.clean">, Sched<[]>;
-def CBO_FLUSH : CBO_r<0b000000000010, "cbo.flush">, Sched<[]>;
-def CBO_INVAL : CBO_r<0b000000000000, "cbo.inval">, Sched<[]>;
+def CBO_CLEAN : CBO_r<0b000000000001, "cbo.clean">, Sched<[WriteCBO, ReadMemBase]>;
+def CBO_FLUSH : CBO_r<0b000000000010, "cbo.flush">, Sched<[WriteCBO, ReadMemBase]>;
+def CBO_INVAL : CBO_r<0b000000000000, "cbo.inval">, Sched<[WriteCBO, ReadMemBase]>;
 } // Predicates = [HasStdExtZicbom]

 let Predicates = [HasStdExtZicboz] in {
-def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[]>;
+def CBO_ZERO : CBO_r<0b000000000100, "cbo.zero">, Sched<[WriteCBO, ReadMemBase]>;
 }
```

注意 `CBO_r` 的 `mayStore = 1` 已经在指令属性里，不需要在 `Sched<>` 里重复；`Sched<>` 只描述「占什么资源、多久可用、读什么」。

### 其他 CBO 指令一起改

`CBO_CLEAN`、`CBO_FLUSH`、`CBO_INVAL` 同样是 `Sched<[]>`，建议一并改成 `Sched<[WriteCBO, ReadMemBase]>`。它们共享同一条 cache 维护通道，模型上没有理由分开。

`FENCE_TSO`、`FENCE_I`、`PREFETCH_*` 也是 `Sched<[]>`，但它们是另一类问题（fence 是全局屏障，prefetch 是 hint），不要在这个练习里顺手改——一次只改一类，review 才好过。

## 第四步：重建

**不要**在 hub（`/home/ubuntu/llvm/llvm-project`）里改代码。按工作区规则：

```bash
source /home/ubuntu/llvm/toolset/env.sh
"$LLVM_HOME"/toolset/status.sh                 # 确认你的 task 的 PENDING
cd "$LLVM_HOME/trees/<your-task>"
# 编辑三个 .td 文件
"$LLVM_HOME"/toolset/build.sh <your-task>
```

如果只是想快速验证后端，单目标增量：

```bash
ninja -C "$LLVM_HOME/builds/<your-task>" llc llvm-mca
```

`.td` 改动会触发 TableGen 重新生成 `RISCVGenSubtargetInfo.inc` / `RISCVGenInstrInfo.inc`，然后重编 RISCV 后端并重链 `llc`/`llvm-mca`。第一次通常几分钟，之后改数字只要几十秒。

**如果 TableGen 报错**，最可能是：

- `WriteCBO` 拼错（未定义符号）。
- `WriteRes` 放错 `let SchedModel = ... in { }` 作用域，导致「不属于任何模型」。
- `Sched<[...]>` 的 read 数量和操作数不匹配（`CBO_r` 只有一个 `$rs1`）。

## 第五步：验证

### 5.1 mca 不再报错

```bash
printf 'cbo.zero (a0)\nadd a0, a1, a2\n' > cbo.s
"$MCA" -mtriple=riscv64 -mcpu=<你改的 CPU> -mattr=+m,+zicboz -instruction-info cbo.s
```

**判读标准**（不是日志抄写）：

- 不再出现 `unable to resolve scheduling class for write variant`。
- `Instructions` 应该是 200（两条指令 × 100 次迭代），不是 100。
- `cbo.zero` 那一行的 `Latency` 等于你写进 `WriteRes` 的数字。
- `Resource pressure by instruction` 里 `cbo.zero` 占的是你指定的资源（Rocket 是 `RocketUnitMem`）。

如果 `Instructions` 还是 100，说明指令仍被跳过，检查 `Sched<[...]>` 是否真的生效（可能改错了文件，或 `Predicates` 没匹配）。

### 5.2 `llc` 的 DAG 里能看到这条边

写一段会生成 `cbo.zero` 的 IR（例如对一个 `alloca` 做 `llvm.memset`，或用内置函数 `llvm.riscv.cbozero`，取决于当前版本的 lowering），用：

```bash
"$LLC" -mtriple=riscv64 -mcpu=<CPU> -mattr=+m,+zicboz -O2 \
  -misched-print-dags cbo.ll -o /dev/null
```

**判读标准**：`SU(n): ... CBO_ZERO ...` 的 `Latency` 等于你写的值；它到后续指令的 `Data Latency` 不再是默认的 1（除非你本来就写 1）。

如果 IR 不好构造，可以退一步用 MIR 输入（`-run-pass=machine-scheduler`），或者只验证 mca——mca 已经覆盖了「模型能否解析」这一层。

### 5.3 加一个 lit 测试

RISC-V 的 mca 测试通常在 `llvm/test/tools/llvm-mca/RISCV/`。最小形态：

```asm
# NOTE: Assertions have been autogenerated by utils/update_mca_test_checks.py
# RUN: llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m,+zicboz \
# RUN:   -instruction-info < %s | FileCheck %s

cbo.zero (a0)

# CHECK:      Instruction Info:
# CHECK:      [1]    [2]    [3]    [4]    [5]    [6]    Instructions:
# CHECK-NEXT:  1      2      1.00    *                   cbo.zero	(a0)
```

用 `llvm/utils/update_mca_test_checks.py` 生成实际 CHECK 行，不要手写数字。然后：

```bash
ninja -C "$LLVM_HOME/builds/<your-task>" llvm-mca FileCheck count
"$LLVM_HOME/builds/<your-task>/bin/llvm-lit" -v \
  llvm/test/tools/llvm-mca/RISCV/<your-test>.s
```

### 5.4 确认没有带歪别的指令

改动 `RISCVSchedule.td` 会影响所有模型，但只新增符号、不改现有绑定，理论上零影响。仍然要跑一遍相关回归：

```bash
"$LLVM_HOME/builds/<your-task>/bin/llvm-lit" -v \
  llvm/test/CodeGen/RISCV/ llvm/test/tools/llvm-mca/RISCV/
```

`ninja check-llvm-codegen-riscv` 也可以，但全量较慢；先跑目录级。

## 第六步：把「一个 CPU」扩展成「所有 CPU」

你只在 Rocket 里加了 `WriteRes<WriteCBO, ...>`。其他模型呢？

- TableGen 的 `CompleteModel = false` 意味着**不会有任何警告**（见 Ch 12 踩雷 6）。没绑定的模型会让 `WriteCBO` 落到 `defaultDefLatency()`：`mayLoad` 为假（CBO 是 mayStore），所以返回 1，且没有资源占用。
- 这不一定错：一个只在 Rocket 上验证的改动，不必给所有 30 多个 CPU 编数字。
- 但如果你要给上游提 PR，review 会问「SiFive7 呢？SpacemitX60 呢？」。合理的做法是：先给 2–3 个有代表性的模型（Rocket、SiFive7、GenericOOO）绑定，其余显式用默认值，并在 commit message 里说明。

一个可复用的模式：如果某条指令在多个模型里都应该走 LSU，且延迟相同，可以在各模型文件里各写一行 `def : WriteRes<WriteCBO, [<LSU 资源>]> { let Latency = N; }`，不要试图在 `RISCVSchedule.td` 里写「全局默认」——那里只有符号声明，没有绑定。

## 时间预算与提交形态

这类改动本身很小，时间主要花在构建和验证上。一个现实的预算：

| 阶段 | 时间 | 说明 |
|---|---|---|
| 定位三处 + 设计数字 | 10–20 分钟 | 读 `.td`，确认资源和延迟的参照 |
| 首次增量构建（改 `.td`） | 3–10 分钟 | TableGen + RISCV 后端 + 链接 `llc`/`llvm-mca` |
| mca 验证 | 2 分钟 | 改数字后重建通常 < 1 分钟 |
| lit 测试生成 + 运行 | 5–10 分钟 | `update_mca_test_checks.py` + 目录级 lit |
| 全 CodeGen/RISCV 回归 | 5–15 分钟 | 视机器而定 |

如果只改 `WriteRes` 里的 `Latency` 数字，第二次构建会快很多——但仍然会重跑 TableGen，因为 `.td` 是生成器的输入。这就是为什么「改一个数字」也要等几十秒。

提交时的形态（供参考，不是本练习的必做项）：

```text
[RISCV] Add scheduling information for Zicboz/Zicbom CBO instructions

CBO instructions were defined with an empty SchedRW list, so llvm-mca
could not resolve their scheduling class and silently dropped them with
-skip-unsupported-instructions=lack-sched. Model them as LSU operations
with a dedicated WriteCBO on Rocket, and bind ReadMemBase for the address
operand.

Test: llvm/test/tools/llvm-mca/RISCV/zicbo-sched.s
```

commit message 里要写清楚「为什么选这个资源/延迟」，因为 review 一定会问数字依据。没有依据的数字，比没有数字更糟。

## 踩雷集锦

1. **只改 `Sched<[...]>`，不建 `WriteCBO` 的 `WriteRes`**。TableGen 能过（因为 `CompleteModel=false`），但运行时 `WriteCBO` 没有绑定，落默认延迟 1、无资源。mca 可能仍然报 `lack-sched`，或者给出一个「看起来正常但没资源」的结果。

2. **把 `WriteSTD` 直接拿来用**。`CBO_r` 只有一个操作数，`Sched<[WriteSTD, ReadStoreData, ReadMemBase]>` 的 read 数量和操作数不匹配，TableGen 会报 operand 相关的错，或者把 base 错绑成 data。

3. **以为改了 Rocket 就改了所有 CPU**。`-mcpu=sifive-u74` 用的是 `SiFive7Model`，看不到你的 `WriteRes`。验证时必须用你改的那个 CPU 名。

4. **忘了 `+zicboz`**。mca/llc 都会报 `instruction requires ... 'Zicboz'`，这跟调度模型无关。

5. **在 hub 里改代码**。`/home/ubuntu/llvm/llvm-project` 只做 fetch/log/rebase；改动和构建都在 `trees/<task>` + `builds/<task>`。

6. **手写 lit 的 CHECK 数字**。`update_mca_test_checks.py` 会根据你当前的模型输出生成；手写容易在别的 CPU 或别的迭代次数下失配。

7. **用 `-skip-unsupported-instructions=lack-sched` 掩盖问题**。它会把指令整条丢掉（本机实测：两条指令只剩 `add`），周期数看起来「正常」，实际少算了。它只适合诊断，不适合验证。

## 重点整理

- `Sched<[]>` 不是「延迟 1」，而是「没有调度类」；mca 会报 `unable to resolve scheduling class for write variant`，`-skip-unsupported-instructions=lack-sched` 会把指令整条丢掉。
- 补一条指令要动三处：`RISCVSchedule.td` 声明 `SchedWrite`、各 CPU 的 `.td` 写 `WriteRes`、指令定义里写 `Sched<[Write..., Read...]>`。
- `Sched<[...]>` 的 read 顺序必须和操作数对应；`CBO_r` 只有一个 `$rs1`，所以只绑定 `ReadMemBase`。
- `CompleteModel = false` 让漏绑的 `WriteRes` 静默落默认值，验证必须靠 mca/llc 的实测输出。
- 别名（如 `ntl.all` → `ADD`）自动继承调度信息；独立指令必须自己绑。
- 验证顺序：mca 能解析 → 数字符合设计 → lit 测试 → 目录级回归。

## 自我检核

做完之后，逐项确认：

- [ ] `llvm-mca` 对 `cbo.zero` 不再报 `unable to resolve scheduling class`。
- [ ] `Instructions` 计数包含 `cbo.zero`。
- [ ] `Latency` 等于你写入的 `WriteRes` 值。
- [ ] `Resource pressure by instruction` 显示预期的资源名。
- [ ] `llc -misched-print-dags` 里 `CBO_ZERO` 的 `Latency` 正确（如果能构造出 IR）。
- [ ] lit 测试通过，且是用 `update_mca_test_checks.py` 生成的。
- [ ] 跑过 `llvm/test/CodeGen/RISCV/` 与 `llvm/test/tools/llvm-mca/RISCV/`。
- [ ] 明确知道你改了哪些 CPU 的模型、哪些没改。

## 复盘问题

1. 为什么 `ntl.all` 不需要补 `SchedRW`，而 `cbo.zero` 需要？
2. 如果 `WriteCBO` 只在 Rocket 里绑定，在 `sifive-u74` 上 `cbo.zero` 的 latency 会是多少？为什么？
3. 为什么这个练习不建议顺手改 `FENCE_TSO`？它的调度语义和 CBO 有什么不同？
4. 如果要把 `WriteCBO` 的 latency 从 2 改成 4，需要重新构建哪些目标？为什么改一个数字也要重跑 TableGen？

## 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVSchedule.td`（全文）**
  - **读哪里**：`Write*` / `Read*` 的声明顺序，以及文件末尾的 `SchedReadAdvance` 辅助类。
  - **和本练习的关联**：你要新增的 `WriteCBO` 就放在这里；先确认没有语义更接近的现成符号。

- **`llvm/lib/Target/RISCV/RISCVSchedRocket.td`（Memory 段，约 90–110 行）**
  - **读哪里**：`WriteSTD`、`WriteLDD` 的 `WriteRes` 与 `let Latency` 分组。
  - **和本练习的关联**：CBO 的资源与延迟取值的参照。

- **`llvm/lib/Target/RISCV/RISCVInstrInfoZicbo.td`（全文）**
  - **读哪里**：`CBO_r` 的操作数、`Sched<[]>` 的位置。
  - **和本练习的关联**：指令侧绑定；注意它只有一个 `$rs1`。

- **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp`（`checkCompleteness`，约 1917–1945 行）**
  - **读哪里**：`CompleteModel` 为 false 时直接 `continue`。
  - **和本练习的关联**：解释为什么漏绑 `WriteRes` 不报错。

- **`llvm/test/tools/llvm-mca/RISCV/`（目录）**
  - **读哪里**：随便挑一个已有测试，看 RUN 行与 CHECK 的组织方式。
  - **和本练习的关联**：你要新增的测试的模板。

- **`llvm/docs/TableGen/` 中的 *Scheduling Models* 文档**
  - **读哪里**：`WriteRes`、`SchedWrite` 的语法。
  - **和本练习的关联**：语法查漏；具体数字仍以 RISC-V 现有模型为准。

→ [下一章：指令选择如何决定调度自由度](./18-isel-scheduling-freedom.md)
