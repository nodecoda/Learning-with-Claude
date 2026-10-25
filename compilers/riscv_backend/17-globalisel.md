# 17 GlobalISel 在 RISC-V：四段流水与一次真实的对比实验

> 目标：把 `-global-isel` 从"另一个开关"变成可观察的流水线。读完本章，你能说出 RISC-V 的 GISel 由哪五个文件拼起来、四个阶段各自的 MIR 长什么样、同一段 IR 交给两种选指器会差在哪几条指令，以及什么时候必须让 llc 退回 SelectionDAG。

> 环境：LLVM 23.1.4 预编译（Optimized build，host x86_64-pc-windows-msvc），RISC-V 目标已注册 `riscv32` / `riscv64` / `riscv32be` / `riscv64be`。命令都在仓库根目录 `D:\Learning-with-Claude` 下通过 `compilers/riscv_backend/code/tools/run.ps1` 执行；它把 LLVM `bin` 放到 `PATH` 前面，并导出 `LLVM_BIN`。

## 为什么

SelectionDAG 把"合法化"和"选指"压在同一个 DAG 上：DAG 既是中间表示，也是匹配模板的输入。这种设计的代价在调试时暴露得很直接——当一条指令选错，你面对的是几千个节点的 DAG，而不是一条可以单独打印的指令。GlobalISel 的出发点是把这件事拆成四段，每段都有独立的 MIR 快照：

```text
  LLVM IR
     |  IRTranslator        IR -> 泛型 MIR（G_ADD/G_MUL/...）
     v
  泛型 MIR
     |  Legalizer           把不支持的泛型操作拆小或降级
     v
  合法 MIR
     |  RegBankSelect       给每个虚拟寄存器挑寄存器Bank（gprb/fprb/vrb）
     v
  bank 已选 MIR
     |  InstructionSelect   泛型操作 -> 真实机器指令
     v
  机器指令 MIR
```

这条流水线在 RISC-V 上确实可用，`-global-isel` 能编出和 SelectionDAG 等价的代码。但它不是"更好的 SelectionDAG"：RISC-V 的 GISel 仍有明确的覆盖缺口，遇到缺口会整函数退回 SelectionDAG（fallback）。默认的 fallback 策略还会直接报 `LLVM ERROR`。所以本章的重点不是背四个阶段的名字，而是建立三条判断：

1. 打开 GISel 之后，究竟哪几个 pass 被换掉了、哪几个没换。
2. 同一段 IR 的两种输出差异，哪些是设计差异，哪些是暂时的覆盖差异。
3. 什么时候应该用 GISel，什么时候不该。

## 主线

### 17.1 打开开关：被替换的只是 isel 这一段

`-global-isel` 并不会重写整条后端流水线。看 `RISCVTargetMachine.cpp` 里 `RISCVPassConfig` 的覆写点，顺序是固定的：

- `addIRTranslator()` 装入 `IRTranslator`。
- `addPreLegalizeMachineIR()` 在 `-O0` 时装 `RISCVO0PreLegalizerCombiner`，否则装 `RISCVPreLegalizerCombiner`。
- `addLegalizeMachineIR()` 装入 `Legalizer`。
- `addPreRegBankSelect()` 在非 `-O0` 时装 `RISCVPostLegalizerCombiner`。
- `addRegBankSelect()` 装入 `RegBankSelect`。
- `addGlobalInstructionSelect()` 装入 `InstructionSelect`。

这六个函数只在 `addInstSelector()` 返回 `false` 的路径上生效。也就是说，GISel 只接管"IR 到机器指令"这一段；后面的 machine-combiner、machine-sink、寄存器分配、prologue/epilogue、伪指令展开、压缩指令优化全部复用同一套 pass。这一点决定了后面所有的比较都只该盯住选指输出，而不是整份汇编。

四段流水的 MIR 可以直接打印出来。命令模式统一为 `llc -global-isel -stop-after=<stage>`，下面是本机 `-O2`、`-mattr=+m` 下 `mul_add` 的真实输出（只截取关键行）：

```text
--- irtranslator ---
legalized:       false
regBankSelected: false
selected:        false
    %0:_(s64) = COPY $x10
    %1:_(s64) = COPY $x11
    %3:_(s64) = G_CONSTANT i64 7
    %2:_(s64) = G_MUL %0, %1
    %4:_(s64) = G_ADD %2, %3

--- legalizer ---
legalized:       true
    %0:_(s64) = COPY $x10
    ...

--- regbankselect ---
legalized:       true
regBankSelected: true
    %0:gprb(s64) = COPY $x10
    %1:gprb(s64) = COPY $x11
    %3:gprb(s64) = G_CONSTANT i64 7
    %2:gprb(s64) = G_MUL %0, %1
    %4:gprb(s64) = G_ADD %2, %3

--- instruction-select ---
selected:        true
    %0:gpr = COPY $x10
    %1:gpr = COPY $x11
    %2:gpr = MUL %0, %1
    %4:gpr = ADDI %2, 7
```

四行元数据比指令本身更值得记：`legalized` 在第二段翻成 `true`，`regBankSelected` 在第三段翻成 `true`，`selected` 在第四段翻成 `true`。这三个布尔量是判断"流水线走到哪"的最快方式；它们和寄存器类型 `_(s64)` → `gprb(s64)` → `gpr` 一一对应。

还有一个容易被忽略的细节：`G_CONSTANT i64 7` 在第四段消失了，`G_ADD` 变成了带立即数的 `ADDI`。这不是选指器"合并"了两条指令，而是 `InstructionSelector` 在匹配 `G_ADD` 时把常量操作数折叠进了 `ADDI` 的立即数槽。所以数指令条数时，泛型 MIR 比最终机器指令多一条是正常的。

### 17.2 GISel 目录里的五个角色

`llvm/lib/Target/RISCV/GISel/` 下有五个必须认识的文件，外加一个内联汇编入口。它们的职责不是"各管一段"，而是"各管一类失败"：

| 文件 | 关键符号 | 负责什么 | 失败时你会看到 |
|---|---|---|---|
| `RISCVCallLowering.cpp` | `RISCVCallLowering::lowerReturn`、`lowerFormalArguments`、`lowerCall`、`saveVarArgRegisters` | 把 IR 的函数签名、返回值、实参、可变参数落到 ABI 寄存器与栈槽 | 调用约定相关断言，或整函数 fallback |
| `RISCVLegalizerInfo.cpp` | `RISCVLegalizerInfo::RISCVLegalizerInfo`、`legalizeCustom`、`legalizeVAStart`、`legalizeExt`、`legalizeLoadStore` | 用 `getActionDefinitionsBuilder` 描述每种泛型操作在 RV32/RV64 上是否合法、要 `lower` 还是 `clampScalar` | `LLVM ERROR: unable to legalize instruction: ... G_VECREDUCE_ADD` |
| `RISCVRegisterBankInfo.cpp` | `RISCVRegisterBankInfo::getInstrMapping`、`getRegBankFromRegClass`、`getFPValueMapping` | 给每个泛型指令的每个操作数挑 gprb / fprb / vrb，并给出 copy 代价 | `RegBankSelect` 阶段的 "Unable to map" 类错误 |
| `RISCVInstructionSelector.cpp` | `RISCVInstructionSelector::select`、`selectImpl`（TableGen 生成）、`selectAddrRegImm`、`selectSelect`、`materializeImm` | 把 bank 已选的泛型指令匹配成真实指令；复杂寻址、立即数、select 都有手写兜底 | 选指失败，整函数 fallback |
| `RISCVO0PreLegalizerCombiner.cpp` / `RISCVPreLegalizerCombiner.cpp` / `RISCVPostLegalizerCombiner.cpp` | `RISCVPostLegalizerCombinerImpl`、`runOnMachineFunction` | 在合法化前后做 GISel 侧的 combine（常量折叠、load/store 合并等） | combine 不生效，输出比 SDAG 啰嗦 |

这条分工解释了一个常见困惑：为什么同一个 `select`，改 `RISCVInstructionSelector.cpp` 和改 `RISCVLegalizerInfo.cpp` 都能"修好"。前者决定能不能匹配成 `PseudoCCMOVGPR` 或分支，后者决定 `G_SELECT` 这个泛型操作在 RISC-V 上是否合法。判断该改哪边，看失败发生在第几段：legalizer 的报错带 `unable to legalize instruction`，selector 的失败通常只留下 fallback 警告。

组合器（combiner）是第五个角色，也是最容易被忽略的。`-O0` 走 `RISCVO0PreLegalizerCombiner`，`-O2` 走 `RISCVPreLegalizerCombiner` 加 `RISCVPostLegalizerCombiner`。这不是可有可无的优化：泛型 MIR 里大量的 `G_SEXT`/`G_ZEXT`/`G_TRUNC` 需要在这些 combine 里被消掉，否则 selector 会为它们各选一条真实指令。

### 17.3 同一段 IR，两种选指器的真实差异

下面这段 IR 有三处典型形态：一条 `mul`+`addi`、一个 `select`、一次 store 后 load。把它分别交给两种选指器：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @mul_add(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %r = add i64 %m, 7
  ret i64 %r
}

define i32 @sel(i1 %c, i32 %a, i32 %b) {
  %s = select i1 %c, i32 %a, i32 %b
  ret i32 %s
}

define i64 @ldst(ptr %p, i64 %v) {
  store i64 %v, ptr %p, align 8
  %x = load i64, ptr %p, align 8
  ret i64 %x
}
```

命令是 `llc -mtriple=riscv64 -mattr=+m -O2`，加不加 `-global-isel`。本机实测的差异只有 8 行：

```text
InputObject     SideIndicator
-----------     -------------
	mv	a3, a0      =>            (仅 GlobalISel)
	andi	a3, a3, 1   =>
	sd	a1, 0(a0)   =>
	ld	a0, 0(a0)   =>
	andi	a3, a0, 1   <=            (仅 SelectionDAG)
	mv	a2, a0      <=
	mv	a0, a1      <=
	sd	a1, 0(a2)   <=
```

`mul_add` 两边完全一样，都是 `mul` + `addi` + `ret`。差异全部集中在另外两个函数：

1. `sel`：SelectionDAG 先把条件 `andi a3, a0, 1` 算好，再 `mv a0, a1`；GISel 先做两个 `mv`（把返回值预置成 `a1` 的副本，并把条件搬进 `a3`），再 `andi`。两条路径的语义等价，但指令顺序不同，因为在 GISel 里 `COPY` 是显式的、要在选指阶段消化掉，而 SDAG 的复制是在寄存器分配附近才出现。
2. `ldst`：SelectionDAG 保留了两条 `mv`（把指针和值搬到新寄存器），GISel 直接用 `a0` 既做地址又做结果：`sd a1, 0(a0)` 后 `ld a0, 0(a0)`。这不是 GISel "更聪明"，而是它没有插入那两条复制；对 `ldst` 这种 store 后立即 load 同一地址的形态，两种结果都正确。

再用 `.scratch/riscv-ev/basic.ll`（前面的实验样本）对比，会多出一处更值得记的差异：GISel 在 `sel` 里多出一条 `sext.w a0, a0`，SelectionDAG 没有。`sext.w` 本身是一条真实指令（`ADDIW` 的别名），它的出现说明 GISel 在 32 位结果到 64 位返回值的扩展上，把符号扩展留到了选指之后；SDAG 则把这个扩展吸收了。这类差异不是 bug，也不该拿来做逐字节回归。

把这两组差异合起来看，可以得到一条实用结论：**两种选指器的输出在语义上等价，但在指令条数、指令顺序、以及 32/64 位边界上的扩展指令上不保证一致**。任何"把 SDAG 的 CHECK 行直接套到 GISel 上"的测试写法都会失败，正确做法是给 GISel 单独一份测试或单独一组 CHECK 前缀。

### 17.4 与 SelectionDAG 的取舍：编译时间、覆盖度、调试难度

**编译时间**。在 2000 个只含 `mul`+`addi` 的小函数上，本机重复三次的结果是：

| 选指器 | 第 1 次 | 第 2 次 | 第 3 次 |
|---|---|---|---|
| SelectionDAG | 840 ms | 811 ms | 1047 ms |
| GlobalISel | 1141 ms | 1397 ms | 993 ms |

三次的抖动比两者之间的差距还大。诚实的结论是：在这个负载上两者同量级，GISel 没有体现出"更快"；把编译时间当作选 GISel 的理由不成立。真正影响编译时间的是函数规模、combiner 的迭代次数和 fallback 的比例——一个整函数 fallback 意味着先跑完 GISel 再跑一遍 SDAG。

**覆盖度**。RISC-V 的 GISel 在整数、浮点、调用、基本访存上已经可用；缺口集中在向量与部分聚合操作。本章 17.5 的向量归约例子会直接触发 legalizer 失败。实践中的判断方法是：拿你的真实 IR 跑一遍 `-global-isel -global-isel-abort=2`，看有没有 `warning: Instruction selection used fallback path for <函数名>`。有警告就说明这个函数没走 GISel，收益为零。反过来，标量浮点这条路径已经闭环：本章实测 `llc -mtriple=riscv64 "-mattr=+m,+f,+d" -global-isel -global-isel-abort=2` 编译 `fadd`、`fmul`、`fptosi` 三个函数，退出码 0、没有 fallback 警告，汇编里直接是 `fadd.d`、`fmul.d`、`fcvt.w.d`。

**调试难度**。GISel 的优势在这里最明显：`-stop-after=irtranslator|legalizer|regbankselect|instruction-select` 给出四个可 diff 的快照，`-print-before=` / `-print-after=` 能卡在单个 pass 前后。SelectionDAG 的对应手段是 `-view-dag-combine1-dags` 之类的 GraphViz 导出，需要额外工具才能看。代价是 GISel 的失败信息更分散：legalizer 报一条，selector 只给 fallback 警告，regbankselect 可能什么都不说就把函数退回去。

一个可操作的取舍规则：

- 你在改 RISC-V 的 isel 本身、或要给新扩展加选指：优先 GISel，因为四段快照让定位快得多。
- 你在编生产代码、目标里有向量或复杂聚合：先测 fallback 比例，再决定是否默认打开。
- 你在写 lit 测试：两种选指器分开写，不要共用 CHECK 行。

### 17.5 fallback 与 -global-isel-abort 的真实三档

`-global-isel-abort` 的语义在 `TargetOptions.h` 里由 `GlobalISelAbortMode` 定义，三个枚举值是 `Disable`、`Enable`、`DisableWithDiag`，分别对应 `0`、`1`、`2`。默认值是 `Enable`。实测行为如下（输入是一个含 `llvm.vector.reduce.add` 的函数，`-mattr=+m,+v`）：

```text
--- -global-isel-abort=0 ---
exit=0

--- -global-isel-abort=1 ---
exit=1
LLVM ERROR: unable to legalize instruction: %2:_(s32) = G_VECREDUCE_ADD %1:_(<4 x s32>) (in function: vsum)

--- -global-isel-abort=2 ---
exit=0
warning: Instruction selection used fallback path for vsum
```

三档的含义可以直接从输出读出来：

| 值 | 名称 | 行为 | 适用场景 |
|---|---|---|---|
| 0 | Disable | 静默 fallback 到 SelectionDAG | 批量实验，只关心最终代码 |
| 1 | Enable（默认） | 直接 `LLVM ERROR` 终止 | 开发 GISel 本身，失败必须暴露 |
| 2 | DisableWithDiag | fallback 并打印 warning | 评估覆盖率，想知道哪些函数没走 GISel |

这里有个与直觉相反的点：**默认值不是"温和地退回"，而是"直接报错"**。所以第一次给一个陌生目标打开 `-global-isel` 时，若不加 `-global-isel-abort=2`，你很可能看到的是 `LLVM ERROR` 而不是一份能跑的汇编。做覆盖度评估时应该显式写 `2`。

向量的缺口比想象中宽。不仅归约失败，连一个最普通的 `<4 x i32>` 逐元素加法也会 fallback：

```text
warning: Instruction selection used fallback path for addv4
```

而 `-global-isel-abort=0` 时同样的输入能正常产出 `vsetivli` / `vle32.v` / `vadd.vv` / `vse32.v`——因为那些指令是 SelectionDAG 选的，不是 GISel。把这份汇编当成"GISel 支持向量"的证据是常见误判。

### 17.6 把一次 fallback 定位到具体阶段

前面几节给的是结论，这一节给流程。假设你拿到一份含向量归约的 IR，`-global-isel` 下直接报 `LLVM ERROR`。合理的排查顺序是四步，每一步都对应一节前面的知识：

1. 先用 `-global-isel-abort=2` 复跑，确认是哪个函数 fallback：

```text
warning: Instruction selection used fallback path for vsum
```

2. 用 `-stop-after=legalizer` 看这个函数停在哪条泛型指令上。本例的报错已经点名：

```text
LLVM ERROR: unable to legalize instruction: %2:_(s32) = G_VECREDUCE_ADD %1:_(<4 x s32>) (in function: vsum)
```

3. 回到 `RISCVLegalizerInfo.cpp`，在构造函数里找 `G_VECREDUCE_ADD` 对应的 `getActionDefinitionsBuilder`。如果它不在任何 builder 里，或 builder 的动作是 `unsupported`，就说明这条泛型操作在该 subtarget 上被判为不合法。修法不是"在 selector 里加一个 pattern"，而是先给 legalizer 一个 `lower` 或 `custom` 规则。

4. 只有在 legalizer 通过之后，才轮到 `RISCVInstructionSelector.cpp` 的 `selectImpl` 或手写 `select`。这一步失败通常不会报 `unable to legalize`，只会让整函数 fallback。

这套流程有一个反向用途：当你想知道某个新扩展"GISel 支持到哪一步"，不需要读全部代码，按 `-stop-after` 四档各跑一次，哪一档开始出现 `LLVM ERROR`，缺口就在那一段。

### 17.7 同一份 IR 的三种观测方式

同一个函数可以用三种粒度观察，选错粒度会让问题看起来像"玄学"：

| 观测方式 | 命令 | 能看到什么 | 看不到什么 |
|---|---|---|---|
| 最终汇编 | `llc -global-isel -o -` | 指令条数、寄存器分配结果、压缩指令 | 哪一段选指器产生的 |
| 阶段 MIR | `llc -global-isel -stop-after=<stage>` | 泛型指令、bank、选中标志 | pass 之间的中间变化 |
| pass 前后 | `llc -global-isel -print-before=<pass> -print-after=<pass>` | 单个 pass 的输入输出 | 其他 pass 的副作用 |

第三种方式在 GISel 里尤其有用，因为 combiner 会在 legalize 前后改 IR。举例：`-print-after=legalizer` 与 `-print-before=regbankselect` 之间还夹着 `RISCVPostLegalizerCombiner`（`-O2` 下），两段输出不一定相同。如果你把这两个快照当成同一步，就会误以为 legalizer 做了 combine。正确的写法是显式打印 combiner 的名字，或者用 `-stop-after` 卡在确定的 pass 边界上。

再补一个与 SelectionDAG 对照的观测差异：SDAG 的 `-debug-pass=Structure` 里只有 `riscv-isel` 与 `finalize-isel` 两段，GISel 会多出 `irtranslator`、`legalizer`、`regbankselect`、`instruction-select` 四段。用 `-debug-pass=Structure` 数一下段数，比读文档更快确认当前走的是哪条路径。
## 可跑实验

### 实验 1：一次跑完两种选指器与四段 MIR

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\17-gisel.ps1
```

脚本会在 `%TEMP%\rvlab17` 下写入 `basic.ll`，依次产出 `sdag.s`、`gisel.s`，打印 `Compare-Object` 的差异表，再打印四个阶段的 `legalized` / `regBankSelected` / `selected` 与寄存器类型。预期能看到的三个关键现象：

- `irtranslator` 阶段是 `%0:_(s64)`，`legalized: false`。
- `regbankselect` 阶段变成 `%0:gprb(s64)`，`regBankSelected: true`。
- 差异表里 `ldst` 在 GISel 侧只剩 `sd a1, 0(a0)` / `ld a0, 0(a0)`，SDAG 侧有两条 `mv`。`Compare-Object` 的完整输出形如：

```text
InputObject     SideIndicator
-----------     -------------
	mv	a3, a0      =>
	andi	a3, a3, 1   =>
	sd	a1, 0(a0)   =>
	ld	a0, 0(a0)   =>
	andi	a3, a0, 1   <=
	mv	a2, a0      <=
	mv	a0, a1      <=
	sd	a1, 0(a2)   <=
```

`=>` 表示只在 GISel 的汇编里出现，`<=` 表示只在 SDAG 的汇编里出现。读这张表时不要按行配对，两边指令数本来就不等。


### 实验 2：把 fallback 打出来

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\17-gisel.ps1
```

同一个脚本的后半段会写 `gap.ll`（含 `llvm.vector.reduce.add`），并对三档 `-global-isel-abort` 各跑一次，打印 `exit=` 与匹配到的 `LLVM ERROR` / `warning`。预期输出就是 17.5 节那张表：`0` 静默成功、`1` 报错退出、`2` 打印 fallback 警告。

### 实验 3（失败/边界）：向量加法也会 fallback

```powershell
$bin = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin"
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+v" -O2 -global-isel -global-isel-abort=2 `
    "D:\Learning-with-Claude\.scratch\riscv-ev\vec2.ll" -o -
```

本机实测第一行输出是 `warning: Instruction selection used fallback path for addv4`，随后打印的汇编仍然包含 `vsetivli` 与 `vle32.v`。这份汇编由 SelectionDAG 产生。这个实验的价值是让你亲眼看到"GISel 打开 + 向量能编"并不等于"GISel 支持向量"。

### 实验 4：不要用 SDAG 的 CHECK 行回归 GISel

把实验 1 的两份汇编各存一份，再用 `git diff --no-index` 对比。你会看到差异行数远小于总行数，但差异集中在 `sel` 与 `ldst`，而且 GISel 侧的 `mv` 位置不同。若把 SDAG 版本的 `update_llc_test_checks.py` 结果直接给 GISel 用，测试会红，但代码是对的。这个实验没有"通过/失败"的判定，它训练的是读 diff 的能力。

### 17.8 一句话的取舍结论

如果只记一句：GISel 值得用在「你要改选指器」和「你需要四段可 diff 的快照」的场景；它不值得用在「想靠它加速编译」或「目标里有向量、想少写测试」的场景。前者是它的设计收益，后者是它当前的覆盖边界。把这句话和第 18 章的调度模型放在一起看，会发现后端优化的收益通常来自机器层，而不是换一个 isel 框架。

## 源码落点

路径均相对 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`（llvmorg-23.1.4 sparse clone）。

| 位置 | 符号 | 读它回答什么问题 |
|---|---|---|
| `RISCVTargetMachine.cpp` | `RISCVPassConfig::addIRTranslator`、`addPreLegalizeMachineIR`、`addLegalizeMachineIR`、`addPreRegBankSelect`、`addRegBankSelect`、`addGlobalInstructionSelect` | GISel 到底替换了哪几个 pass，`-O0` 与 `-O2` 的 combiner 差异 |
| `GISel/RISCVCallLowering.cpp` | `lowerReturn`、`lowerFormalArguments`、`lowerCall`、`saveVarArgRegisters` | 调用约定在 GISel 里怎么落地，可变参数从哪来 |
| `GISel/RISCVLegalizerInfo.cpp` | `RISCVLegalizerInfo::RISCVLegalizerInfo`、`legalizeCustom`、`legalizeVAStart`、`legalizeExt`、`legalizeLoadStore` | 哪些泛型操作合法、哪些要 lower、失败信息从哪抛 |
| `GISel/RISCVRegisterBankInfo.cpp` | `getInstrMapping`、`getRegBankFromRegClass`、`getFPValueMapping`、`hasFPConstraints` | gprb / fprb / vrb 怎么选，copy 代价怎么算 |
| `GISel/RISCVInstructionSelector.cpp` | `select`、`selectImpl`、`selectAddrRegImm`、`selectSelect`、`materializeImm`、`preISelLower` | 泛型指令怎么变真实指令，立即数与寻址怎么兜底 |
| `GISel/RISCVPreLegalizerCombiner.cpp`、`RISCVPostLegalizerCombiner.cpp`、`RISCVO0PreLegalizerCombiner.cpp` | `runOnMachineFunction`、`RISCVPostLegalizerCombinerImpl` | 合法化前后的 combine 做了什么 |
| `GISel/RISCVRegisterBanks.td` | 寄存器 Bank 定义 | bank 的名字与包含的寄存器类 |

核心 GISel 头文件（`IRTranslator.h`、`Legalizer.h`、`RegBankSelect.h`、`InstructionSelect.h`）在预编译包的 `include/llvm/CodeGen/GlobalISel/` 下，可以直接对照类名读。`GlobalISelAbortMode` 的枚举在 `include/llvm/Target/TargetOptions.h`。

## 常见坑

1. **把 `-global-isel` 当成全局替换**。它只替换 isel 这一段，后面的 machine pass 完全一样。看到调度或 prologue 的差异，先怀疑输入 MIR 不同，而不是 GISel 改了调度。
2. **忘了默认 `-global-isel-abort=1` 会直接终止**。第一次在陌生 IR 上试 GISel 却没写 `=2`，得到 `LLVM ERROR` 就以为工具坏了。评估阶段统一加 `-global-isel-abort=2`。
3. **把 fallback 产物当成 GISel 的产物**。`addv4` 的向量汇编是 SDAG 选的；唯一的判据是 warning，不是汇编里有没有 `vsetivli`。
4. **拿 SDAG 的 CHECK 行回归 GISel**。`sel` 的 `mv` 位置、`ldst` 的复制、`sext.w` 的有无都可能不同，逐字节比对必然失败。
5. **只看最终汇编判断四段流水**。`legalized` / `regBankSelected` / `selected` 三个布尔量和 `_(s64)` → `gprb` → `gpr` 的类型变化才是流水线的证据。
6. **在 PowerShell 里写 `-mattr=+m,+v` 不加引号**。PowerShell 会把逗号当成数组分隔符，报 `Missing argument in parameter list`；写成 `"-mattr=+m,+v"`。
7. **以为 fallback 是"没有成本"的**。一个函数 fallback 意味着 GISel 的四段先跑完，再跑一遍 SelectionDAG；覆盖率低时编译时间会明显变差。

8. **把 `-global-isel` 和 `-O0` 混着比较**。`-O0` 走 `RISCVO0PreLegalizerCombiner`，`-O2` 走两个 combiner，指令条数差异可能来自 combiner 而不是选指器。比较时固定优化级别。
9. **忽略 `G_CONSTANT` 的折叠**。泛型 MIR 里 `G_CONSTANT` 是独立一条，选指后常量进了 `ADDI` 的立即数槽；用泛型 MIR 数指令条数会多算。
## 自我检查

- [ ] 能说出 RISC-V GISel 的五个文件（CallLowering、LegalizerInfo、RegisterBankInfo、InstructionSelector、combiner）各自负责哪类失败。
- [ ] 能不看笔记写出 `-stop-after=irtranslator|legalizer|regbankselect|instruction-select`，并解释每一步 `legalized` / `regBankSelected` / `selected` 的变化。
- [ ] 能解释为什么 `-global-isel-abort=1` 是默认值，以及为什么评估覆盖率要用 `=2`。
- [ ] 能举出一个"GISel 打开但向量仍由 SDAG 选"的真实例子，并说明判据是 warning 而不是汇编内容。
- [ ] 能说出同一段 IR 在两种选指器下至少两处差异（`sel` 的 `mv` 位置、`ldst` 的复制、`sext.w`）。
- [ ] 能说明为什么 GISel 的编译时间在本机微基准上没有优势，以及这个结论的适用范围。
- [ ] 能用 `Compare-Object` 生成两份汇编的差异表，并判断哪些差异是设计差异、哪些是覆盖差异。

## 延伸阅读

- LLVM 官方文档 `GlobalISel`：四段流水的设计目标与 `-global-isel-abort` 的语义。
- `llvm/docs/GlobalISel/Porting.rst`（同版本源码树）：“先让 legalizer 通过，再补 selector”的移植顺序。
- RISC-V 后端的 GISel 测试：`llvm/test/CodeGen/RISCV/GlobalISel/` 下的 `.mir` 文件，每个都是某一阶段的输入输出对。
- 本课程第 14 章（合法化）与第 15 章（SelectionDAG 选指）：把本章的四段与 SDAG 的两段对照读。
- `llvm/utils/update_llc_test_checks.py`：理解 CHECK 行是怎么生成的，才能理解为什么不能跨选指器复用。

- `llvm/test/CodeGen/RISCV/GlobalISel/legalizer/*.mir`：每个文件只测一条泛型指令的合法化动作，改 legalizer 时先跑这一组。
- `llvm/test/CodeGen/RISCV/GlobalISel/regbankselect/*.mir`：观察 `gprb` / `fprb` / `vrb` 的分配结果。
- `llvm/test/CodeGen/RISCV/GlobalISel/instruction-select/*.mir`：选指前后的 MIR 对，最适合对照本章 17.1 的四段输出。
- `RISCVRegisterBanks.td`：bank 的名字与包含的寄存器类，读 `RISCVRegisterBankInfo.cpp` 前先看它。

- `llvm/lib/CodeGen/GlobalISel/Legalizer.cpp`：合法的驱动逻辑，`legalizeMachineFunction` 的迭代与失败上报路径。
- `llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp`：`applyMapping` 的代价比较，解释为什么同一张表会选出不同 bank。
- `llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`：`InstructionSelect` 如何调用目标 `select()` 并处理失败。
- `llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp`：`translate` 的入口，`G_*` 泛型指令在这里第一次出现。
- `llvm/lib/CodeGen/GlobalISel/Combiner.cpp`：combine 的 worklist 机制，理解 `-O0` 与 `-O2` 组合器差异。
- `llvm/lib/Target/RISCV/GISel/RISCVRegisterBanks.td`：bank 定义与寄存器类的对应关系。
- `llvm/include/llvm/Target/TargetOptions.h`：`GlobalISelAbortMode` 三个枚举值的权威定义。
[上一章：自定义 lowering 与 intrinsic](./16-custom-lowering-and-intrinsics.md) | [下一章：调度模型与 llvm-mca](./18-scheduling-and-mca.md)
