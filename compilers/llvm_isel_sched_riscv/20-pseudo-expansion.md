# Ch 20 — 伪指令、宏融合与调度可见性

> **目标**：搞清 RISC-V 后端里「伪指令」从哪来、在哪一个 pass 展开、scheduler 在展开前后分别看到什么。重点回答：为什么 `PseudoMovImm` 在 pre-RA 调度里只算一条指令、`PseudoCALL` 为什么到汇编阶段才变成 `auipc+jalr`、宏融合为什么既影响顺序又影响指令选择。
>
> **环境**：LLVM 24.0.0git；`llc -mtriple=riscv64 -mattr=+m`；pass 名 `riscv-expand-pseudo-pre-ra`、`riscv-expand-pseudo-post-ra`、`riscv-expand-pseudo-pre-emit`；`-riscv-use-rematerializable-movimm=true`；CPU 对照 `rocket-rv64` 与 `sifive-p450`。所有 MIR 与汇编均为本机实跑。

## 为什么伪指令会破坏「选择 → 调度」的直觉

第 18、19 章默认「ISel 选出的每条 MachineInstr 都是一条真实指令」。RISC-V 不成立：

- 有些伪指令要到寄存器分配**之后**才展开成多条真实指令。
- 有些伪指令要到汇编打印阶段才展开，MachineInstr 层永远看不到它的真实形态。
- 有些伪指令带 `Sched<[...]>`，但只声明了一条指令的资源；注释里直接写着 `FIXME: The scheduling information does not reflect the multiple instructions.`

后果是：scheduler 可能在「一条 1-cycle 指令」的假设下做决策，而真实执行是「两条有依赖的指令」。这不是 scheduler 的 bug，而是选择层给的输入不精确。

## 先建立直觉：一条伪指令的三次生命

以常量 3968 为例。开启 `-riscv-use-rematerializable-movimm=true` 后，`finalize-isel` 只生成一条：

```text
%0:gpr = PseudoMovImm 3968
```

pre-RA scheduler 看到的也是这一条。RA 之后，`riscv-expand-pseudo-post-ra` 把它展开成两条：

```text
renamable $x10 = ADDI $x0, 31
renamable $x10 = SLLI killed renamable $x10, 7
```

post-RA scheduler 看到的是这两条真实指令。最终汇编：

```asm
li      a0, 31          # 汇编器把 addi x0,31 打印成 li
slli    a0, a0, 7
ret
```

时间线：

```text
ISel ──> finalize-isel ──> pre-RA sched ──> RA ──> expand-post-ra ──> post-RA sched ──> asm
         PseudoMovImm      PseudoMovImm      ...    ADDI + SLLI        ADDI + SLLI
         1 node, 1 cycle   1 node             真实 2 条指令
```

**pre-RA scheduler 被少算了一条指令，post-RA scheduler 才知道真相。** 但 pre-RA 阶段的调度决策已经影响了寄存器分配和 spill；post-RA 只能修顺序，不能重做 spill。

## 主线一：三个展开点

RISC-V 把伪指令展开拆成三个 pass，位置不同，目的也不同。

| pass | 插入点 | 典型伪指令 | 为什么在这里 |
|---|---|---|---|
| `riscv-expand-pseudo-pre-ra` | `addPreRegAlloc` | `PseudoLLA`、`PseudoLGA`、`PseudoLA_TLS_IE/GD/TLSDESC` | 地址序列需要参与 RA；展开后是真实 `auipc/addi` |
| `riscv-expand-pseudo-post-ra` | `addPreSched2` | `PseudoMovImm`、`PseudoMovAddr`、`PseudoAddUpperImm`、`PseudoMERGE` | 需要物理寄存器；展开后 post-RA scheduler 还能排 |
| `riscv-expand-pseudo-pre-emit` | `addPreEmitPass2` | `PseudoCCMOVGPR`、`PseudoCCADD`、`PseudoMV_FPR*`、`PseudoRV32Zdinx*` | 太晚，任何调度都已结束；只做最终 lowering |

还有一类根本不在这三个 pass 里：`PseudoCALL`、`PseudoTAIL`、`PseudoRET` 由汇编打印机或 MC 层展开。原因是链接器 relaxation：`auipc+jalr` 必须保留成一对带 `R_RISCV_CALL` / `R_RISCV_RELAX` 重定位的指令，不能在中途被拆开。

## 主线二：`PseudoMovImm` 与 `PseudoMovAddr`

源码里两条定义都带同一句 FIXME：

```tablegen
// FIXME: The scheduling information does not reflect the multiple instructions.
def PseudoMovImm : Pseudo<...>, Sched<[WriteIALU]>;
def PseudoMovAddr : Pseudo<...>, Sched<[WriteIALU]>;
```

`PseudoMovImm` 只在 `-riscv-use-rematerializable-movimm=true` 时由 `selectImm` 生成，条件是常量需要两条指令（`Seq.size() == 2`）。默认关闭，所以普通编译看到的是 `LUI+ADDIW` 或 `li+slli`，不是伪指令。

`PseudoMovAddr` 更常见：全局地址的 `%hi/%lo` 对被选成一条伪指令，post-RA 展开成：

```text
LUI  dst, %hi(sym)
ADDI dst, dst, %lo(sym)
```

这两条真实指令在 pre-RA scheduler 眼里是一条 `PseudoMovAddr`。如果这个地址被多个 load/store 使用，`RISCVMergeBaseOffset` 可能先把偏移合并进 `%hi/%lo`（第 19 章），减少后续地址指令；如果没有合并，pre-RA 会低估地址链的长度。

`PseudoAddUpperImm` 是另一个例子：它表示 `base + (imm << 12)`，展开成 `LUI+ADD`，但 `Sched<[WriteIALU, ReadIALU]>` 只描述了一条 ALU 加一条读。它存在的目的是让 `MachineLateInstrsCleanup` 能 CSE 相同的上半立即数，属于「为了优化而故意延迟展开」。

## 主线三：`PseudoCALL` 为什么永远不展开

看一个调用者：

```llvm
declare i64 @callee(i64)

define i64 @caller(i64 %x) {
  %r = call i64 @callee(i64 %x)
  %s = add i64 %r, 1
  ret i64 %s
}
```

`finalize-isel`：

```text
ADJCALLSTACKDOWN 0, 0, ...
$x10 = COPY %0:gpr
PseudoCALL target-flags(riscv-call) @callee, <regmask ...>, implicit-def dead $x1, ...
ADJCALLSTACKUP 0, 0, ...
%1:gpr = COPY $x10
%2:gpr = ADDI %1:gpr, 1
PseudoRET implicit $x10
```

post-RA scheduler 之后仍然是：

```text
PseudoCALL target-flags(riscv-call) @callee, <regmask ...>
```

最终汇编：

```asm
call    callee
```

`PseudoCALL` 的 `Sched<[WriteIALU, WriteJalr, ReadJalr]>` 描述的是「`auipc` + `jalr`」两条指令的资源，但 MachineScheduler 把它当成**一个 SUnit**。它能算出这条 SUnit 的 latency，却不能在 `auipc` 和 `jalr` 之间插入别的指令，也不能把 `jalr` 的延迟单独建模。`PseudoRET` 更彻底：`PseudoInstExpansion<(JALR X0, X1, 0)>`，MC 层才变成 `jalr x0, 0(x1)`，MIR 里连资源声明都没有。

这不是设计缺陷，而是链接器约束下的取舍：

- `auipc+jalr` 必须成对出现，且带重定位；在 MIR 层拆开会让 relaxation 失效。
- 调用点是一个调度屏障（regmask、隐式 def），scheduler 本来也不应该跨它移动指令。

所以面试里问「调用指令的延迟怎么建模」，正确回答是：**调用点按一个 SUnit 建模，内部两条指令的细节由 MC 层和链接器负责；调度器关心的是调用前后的依赖与 regmask，不是 `auipc`/`jalr` 的内部流水**。

## 主线四：宏融合——选择与顺序的交叉点

RISC-V 的宏融合定义在 `RISCVMacroFusion.td`，常见的有：

- `lui + addi` → 一条融合操作（`TuneLUIADDIFusion`）
- `auipc + addi` → 一条融合操作（`TuneAUIPCADDIFusion`）
- `slli 48 + srli 48` → `zext.h`（`TuneZExtHFusion`）
- `slli 32 + srli 32` → `zext.w`（`TuneZExtWFusion`）

这些 tune feature 由 CPU 决定，例如 `sifive-p450`、`sifive-p550`、`sifive-p670` 打开 `TuneLUIADDIFusion` / `TuneAUIPCADDIFusion`，`sifive-p670` 还打开 `TuneZExtWFusion`。

宏融合在两个层面起作用：

1. **调度层**：`RISCVPassConfig::addPreRAMachineSched` / `addPostRAMachineSched` 里调用 `createMacroFusionDAGMutation(MacroFusions)`，给 DAG 加 cluster 边，让 scheduler 尽量把两条指令排在一起。它不新增指令，只约束顺序。
2. **选择层**：`RISCVMatInt` 在物化常量时会查询 `TuneLUIADDIFusion`。如果该 CPU 能融合 `lui+addi`，就优先选 `lui+addi`；否则优先选更可压缩的 `li+slli`。

第 2 点很容易被忽略。实测常量 3968：

```asm
# rocket-rv64 / sifive-u74 / spacemit-x60（无 LUI+ADDI 融合）
li      a0, 31
slli    a0, a0, 7

# sifive-p450（有 LUI+ADDI 融合）
lui     a0, 1
addi    a0, a0, -128
```

同一个 IR、同一个 `-O2`，只换 CPU，指令序列就不同。原因是 `RISCVMatInt::generateInstSeq` 里的这一行：

```cpp
bool IsShiftedCompressible =
    isInt<6>(ShiftedVal) && !STI.hasFeature(RISCV::TuneLUIADDIFusion);
```

**tune feature 同时影响「选哪条序列」和「怎么排」。** 这也是为什么「指令选择和调度是两层」这个说法只在 pass 结构上成立；在成本模型上，它们通过 tune feature 相互渗透。

## 主线六：`PseudoLGA` 与 `PseudoMovAddr` 的时机差异

同样是「取全局地址」，静态与 PIC 两种重定位模型会选择不同伪指令，展开点也不同。

静态模型下，`ret ptr @ext` 在 `finalize-isel` 是：

```text
%0:gpr = PseudoMovAddr target-flags(riscv-hi) @ext, target-flags(riscv-lo) @ext
```

`riscv-expand-pseudo-pre-ra` 之后**仍然是** `PseudoMovAddr`，因为它要等 RA 分配物理寄存器；真正的 `LUI+ADDI` 在 post-RA 展开。

PIC 模型下，同一段 IR 是：

```text
%0:gpr = PseudoLGA @ext :: (dereferenceable invariant load (s64) from got)
```

`riscv-expand-pseudo-pre-ra` 之后变成两条真实指令：

```text
%1:gpr = AUIPC target-flags(riscv-got-hi) @ext, pre-instr-symbol <mcsymbol .Lpcrel_hi0>
%0:gpr = LD %1:gpr, target-flags(riscv-pcrel-lo) <mcsymbol .Lpcrel_hi0>
```

也就是说：

| 伪指令 | 重定位模型 | 展开点 | pre-RA scheduler 看到 |
|---|---|---|---|
| `PseudoMovAddr` | static / medlow | post-RA | 1 条伪指令 |
| `PseudoLGA` | PIC / GOT | pre-RA | 2 条真实指令（`AUIPC+LD`） |

这解释了一个常见困惑：为什么同样的「取全局地址」代码，PIC 版本的调度看起来更「准」。不是 PIC 更聪明，而是它的伪指令在 pre-RA 之前就展开了，scheduler 能看到完整的地址链。静态版本要等 post-RA，pre-RA 只能按一条 `WriteIALU` 估算。

## 怎么查 pass 顺序

不要背顺序，直接让 `llc` 打印：

```bash
llc -O2 -mtriple=riscv64 -mattr=+m -debug-pass=Structure -o /dev/null file.ll 2>&1 \
  | tr ' ' '\n' | grep -nE 'pseudo|pipeliner|machine-scheduler|postmisched'
```

本机输出里与本章相关的片段（按出现顺序）：

```text
-pipeliner
-riscv-expand-pseudo-pre-ra
...
-machine-scheduler
...
-riscv-insert-vsetvli
...
-riscv-expand-pseudo-post-ra
...
-postmisched
...
-riscv-expand-pseudo-pre-emit
```

把这条命令和 `-print-after=<pass>` 结合，就能确定「某个伪指令在 scheduler 眼里是什么形态」。

## 主线五：`PseudoVSETIVLI` 是一个正面例子

不是所有伪指令都缺调度信息。RVV 的 `PseudoVSETIVLI` 明确声明了：

```tablegen
def PseudoVSETIVLI : Pseudo<...>, Sched<[WriteVSETIVLI]>;
```

它在 post-RA 阶段由 `riscv-insert-vsetvli` 插入，post-RA scheduler 能看到它，并按 `WriteVSETIVLI` 定价；最终由 `PseudoInstExpansion` 在 MC 层变成 `vsetivli`。第 23 章会展开 RVV，这里只用它说明：**伪指令可以有正确的 sched class，关键是 TableGen 有没有写**。

## 主线七：pre-emit 展开为什么最危险

`riscv-expand-pseudo-pre-emit` 位于 `postmisched` 之后、汇编打印之前。它处理的伪指令有一个共同点：**展开后的序列完全不受任何调度器控制**。典型代表是 SFB（Short Forward Branch）系列的 `PseudoCCMOVGPR`、`PseudoCCADD`、`PseudoCCSUB`、`PseudoCCAND` 等，以及 `PseudoMV_FPR16INX`、`PseudoRV32ZdinxSD` 这类与寄存器对/NaN 语义相关的指令。

以 `PseudoCCADD` 为例，它表示「条件成立时执行一次加法，否则保持原值」，展开后是「分支 + `ADD`」或「`czero` + `ADD`」的序列。这个选择依赖具体扩展与寄存器约束，发生在所有调度之后。因此：

- 它在 pre-RA 和 post-RA 的 DAG 里都是一条节点，延迟按伪指令的 sched class 估算。
- 展开引入的分支会改变控制流，但 CFG 已经定型，不会再触发 block placement 或 if-conversion。
- 如果展开序列里有 load/store，它们的延迟对 scheduler 完全不可见。

这也解释了为什么 RISC-V 把「能早展开的就早展开」：能在 pre-RA 展开的 `PseudoLGA` 就早展开，能在 post-RA 展开的 `PseudoMovAddr` 就放在 `postmisched` 之前；只有语义上必须等到最后的（链接器 relaxation、寄存器对约束）才留给 pre-emit 或 MC 层。

## 踩雷

1. **`-riscv-use-rematerializable-movimm` 默认是 false**。不加这个 flag，`PseudoMovImm` 不会出现；不要拿默认编译的输出否认它的存在。
2. **`-print-after=postmisched` 里仍有 `PseudoCALL` / `PseudoRET`**。它们不在 MIR 层展开，看到它们不要以为 pass 没跑；去汇编里看 `call` / `ret`。
3. **`Sched<[WriteIALU]>` 不等于「一条真实指令」**。`PseudoMovAddr`、`PseudoMovImm`、`PseudoAddUpperImm` 都是多条指令共用一条 sched 声明；源码注释已经承认这一点。
4. **展开顺序不能反**。`riscv-expand-pseudo-post-ra` 在 `postmisched` 之前，所以 post-RA scheduler 能看到展开后的两条；`riscv-expand-pseudo-pre-emit` 在 `postmisched` 之后，里面的伪指令对调度完全不可见。
5. **宏融合不是「只改顺序」**。`TuneLUIADDIFusion` 会改变常量物化序列；对比不同 CPU 的汇编时，先确认是融合还是选择导致的差异。
6. **`PseudoRET` 没有 `Sched`**。它靠 `PseudoInstExpansion` 在 MC 层变成 `jalr`；不要指望在 MIR 里读到它的延迟。
7. **`Size = 8` 是给分支放松/布局用的，不是真实编码长度**。`PseudoMovAddr`、`PseudoMovImm`、`PseudoCALL` 都声明 `Size = 8`，表示展开后占 8 字节；实际编码可能是两条 4 字节，也可能被压缩成 2 字节。分析代码尺寸时以汇编为准，不要读 MIR 的 `Size`。

## 动手练习

1. 用 `-riscv-use-rematerializable-movimm=true` 编译 `ret i64 3968`，分别 dump `finalize-isel`、`machine-scheduler`、`riscv-expand-pseudo-post-ra`、`postmisched`，画出 `PseudoMovImm` 的生命周期。
2. 把常量换成 `305419896`（`0x12345678`），观察 `PseudoMovImm` 是否出现；解释 `Seq.size() == 2` 的条件。
3. 在 `sifive-p450` 与 `rocket-rv64` 上编译同一个 `ret i64 3968`，对比 `li+slli` 与 `lui+addi`，并在源码里找到 `TuneLUIADDIFusion` 的判断。
4. 写一个带 `call` 的函数，用 `-print-after=postmisched` 找到 `PseudoCALL`，再用 `-print-after=riscv-expand-pseudo-pre-emit` 确认它仍在；最后在汇编里找 `call`。
5. 给 `PseudoMovAddr` 找一个真实用例（全局 load），用 `-print-after=finalize-isel` 和 `-print-after=riscv-expand-pseudo-post-ra` 对比展开前后。

## 重点整理

- RISC-V 的伪指令在三个不同位置展开：pre-RA、post-RA、pre-emit；`PseudoCALL`/`PseudoRET` 在 MC 层。
- pre-RA scheduler 可能把多条真实指令当成一条 SUnit；post-RA scheduler 通常能看到 post-RA 展开后的真实指令。
- `PseudoMovImm` / `PseudoMovAddr` / `PseudoAddUpperImm` 的 `Sched` 声明只描述一条指令，源码有 FIXME。
- `PseudoCALL` 必须保持成对，不能拆开调度；调用点按一个 SUnit 建模。
- 宏融合既加 cluster 边约束顺序，也通过 tune feature 改变常量物化序列。
- 伪指令的 `Size` 只服务布局与分支放松，不能当作真实编码长度。
- 判断「调度器看到了什么」，先查 pass 顺序，再查伪指令的展开点；`-debug-pass=Structure` 是最快的入口。
- 静态全局地址走 `PseudoMovAddr`（post-RA 展开），PIC 全局地址走 `PseudoLGA`（pre-RA 展开），两者的调度可见性不同。
- pre-emit 展开的伪指令（SFB 系列、FPR 寄存器对）对所有调度器都是黑盒，不要试图用 `-misched-*` 调它们的顺序；要改就只能改展开策略本身。

## 自我检核

- [ ] 我能说出三个 pseudo 展开 pass 的名字与插入点。
- [ ] 我能解释为什么 `PseudoMovImm` 在 pre-RA 调度里被低估。
- [ ] 我知道 `PseudoCALL` 为什么不在 MIR 层展开，以及它对调度的影响。
- [ ] 我能用 `TuneLUIADDIFusion` 解释 3968 在不同 CPU 上的两种汇编。
- [ ] 我能区分「宏融合改顺序」与「宏融合改选择」两种效果。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVInstrInfo.td` 中 `PseudoMovImm`、`PseudoMovAddr`、`PseudoAddUpperImm`、`PseudoCALL` 的定义：重点读 FIXME 注释与 `Sched` 列表。
- `llvm/lib/Target/RISCV/RISCVExpandPseudoPostRA.cpp`：看 `expandMovImm` 如何调用 `TII->movImm`，以及 `expandMovAddr` 如何生成 `LUI+ADDI`。
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.cpp`：读 `generateInstSeq` 中 `TuneLUIADDIFusion` 的判断，理解选择如何被 tune feature 影响。
- `llvm/lib/Target/RISCV/RISCVMacroFusion.td`：读三个 `SimpleFusion` 的 `CheckOpcode` / `CheckImmOperand`，理解融合的匹配条件。
- `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` 的 `addPreSched2` / `addPreEmitPass2`：确认三个展开 pass 的准确位置。
- `llvm/test/CodeGen/RISCV/remat-movimm.mir`（若存在）：看官方如何测试 `PseudoMovImm` 的展开。

→ [下一章：分支、if-conversion、MachineCombiner](./21-branch-if-conversion.md)
