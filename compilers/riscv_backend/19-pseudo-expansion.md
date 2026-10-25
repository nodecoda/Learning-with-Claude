# 19 伪指令展开与机器 pass：从 PseudoLLA 到 auipc/addi

> 目标：把"伪指令"从一个模糊的称呼变成一条可追踪的生命周期。读完本章，你能说出 `PseudoRET` / `PseudoLA` / `PseudoTAIL` / 原子伪指令各自在哪一段被展开、为什么不能在选指阶段就展开、以及 `riscv-prera-expand-pseudo` 与 `riscv-expand-pseudo` 的分工。

> 环境：LLVM 23.1.4 预编译。命令通过 `compilers/riscv_backend/code/tools/run.ps1` 执行。本章的 MIR 片段来自 `-stop-after=` / `-print-before=` / `-print-after=`，反汇编来自 `llvm-objdump`。

## 为什么

伪指令不是"还没实现的指令"，而是"现在还不能决定怎么实现的指令"。决定不了的原因有三类：

1. **物理寄存器未定**。`PseudoLLA @g` 要展开成 `auipc a0, %pcrel_hi(g)` 加 `addi a0, a0, %pcrel_lo(...)`。两条指令必须写同一个物理寄存器，而寄存器分配之前这个寄存器还不存在。如果在选指阶段就拆成两条 `ADDI`/`AUIPC` 虚拟寄存器指令，寄存器分配器没有理由把两个虚拟寄存器分到同一个物理寄存器，展开就失效了。
2. **立即数范围未定**。`li` 的目标值能否用一条 `addi` 表示、要不要 `lui`+`addi`、要不要 8 条指令拼 64 位，取决于立即数的实际值和重定位后的偏移。链接器 relax 之后偏移还会变。
3. **寄存器掩码与 ABI 信息未定**。`PseudoTAIL` 带一整个 regmask（哪些寄存器被调用破坏），`PseudoAtomicLoadAdd64` 需要 `early-clobber` 的 scratch 寄存器，`PseudoVLE32_V_M1` 隐式读写 `vl` / `vtype`。这些都是寄存器分配和向量配置分析之后才稳定的信息。

于是 RISC-V 后端把"展开"拆到多个时间点。每个时间点只做当时能确定的那部分，剩下的继续留给后面的 pass。理解这套时间表，比记住伪指令名字更有用：同样叫 `riscv-expand-pseudo`，pre-RA 版本和 pre-emit 版本做的事情并不相同。

## 主线

### 19.1 伪指令在 TableGen 里长什么样

RISC-V 的伪指令都继承 `Pseudo` 基类（定义在 `Target.td`），但它们的"出口"不同，分成三类。

第一类：**编码期展开**。`PseudoRET` 的定义是

```tablegen
def PseudoRET : Pseudo<(outs), (ins), [(riscv_ret_glue)]>,
                PseudoInstExpansion<(JALR X0, X1, 0)>;
```

`PseudoInstExpansion` 告诉 TableGen："到了 MC 层，把它换成 `JALR X0, X1, 0`"。所以它在 MIR 里可以一直活到 pre-emit 之后，AsmPrinter 调 `lowerPseudoInstExpansion` 时才变成真正的 `JALR`。

第二类：**汇编期展开**。`PseudoTAIL` 的定义带 `AsmString`：

```tablegen
// Define AsmString to print "tail" when compile with -S flag.
let isCall = 1, isTerminator = 1, isReturn = 1, isBarrier = 1, Uses = [X2],
    ...
def PseudoTAIL : Pseudo<(outs), (ins call_symbol:$dst), [], "tail", "$dst">;
```

注释写得很直白：`-S` 时打印 `tail`，真正的 `auipc` + `jalr` 在编码时展开。这也解释了为什么汇编输出里能看到 `tail foo`，而目标文件里看不到。

第三类：**汇编器伪指令**。`PseudoLI` 的注释是

```tablegen
// Note that the size is 32 because up to 8 32-bit instructions are needed to
// generate an arbitrary 64-bit immediate. However, the size does not really
// matter since PseudoLI is currently only used in the AsmParser where it gets
// expanded to real instructions immediately.
```

也就是说 `li` 在 llc 的代码生成路径里并不以 `PseudoLI` 形态存在；它是汇编器接受的写法，进 `AsmParser` 就展开成 `lui`/`addi`/`slli` 序列。llc 自己物化立即数走的是 `PseudoLLA`/`PseudoLAImm` 等另一组伪指令。

这三类的共同点是：**伪指令携带了后面的阶段才能满足的约束**。`PseudoRET` 携带 `riscv_ret_glue`，`PseudoTAIL` 携带 regmask，`PseudoLI` 携带"任意 64 位立即数"这个承诺。

### 19.2 五个展开/插入 pass 与它们在流水线上的位置

`llc -debug-pass=Structure` 的真实 pass 序列里，与伪指令相关的有五处：

```text
... -riscv-opt-w-instrs -riscv-prera-expand-pseudo -riscv-merge-base-offset
    ... -register-coalescer ... -machine-scheduler ... -greedy ...
    -riscv-insert-vsetvli ... -prolog-epilog ... -postrapseudos
    -riscv-post-ra-expand-pseudo -kcfi -riscv-load-store-opt -postmisched
    ... -riscv-make-compressible ... -stack-frame-layout -riscv-move-merge
    -riscv-push-pop-opt -riscv-expand-pseudo -riscv-expand-atomic-pseudo
    -riscv-asm-printer
```

按时间顺序：

| pass（`-debug-pass` 名） | 实现文件 | 展开什么 | 为什么在这个位置 |
|---|---|---|---|
| `riscv-prera-expand-pseudo` | `RISCVExpandPseudoInsts.cpp`（`RISCVPreRAExpandPseudo`） | `PseudoLLA`、`PseudoLGA`、`PseudoLA_TLS_*` | 需要虚拟寄存器成对约束，必须在 RA 前；展开后 RA 能把 auipc/addi 分到同一寄存器 |
| `riscv-insert-vsetvli` | `RISCVInsertVSETVLI.cpp` | 插入 `PseudoVSETIVLI` / `PseudoVSETVL`，给向量指令加隐式 `vl`/`vtype` | 需要 RA 后的物理寄存器与活跃性信息，且在第一次 RA 附近 |
| `riscv-post-ra-expand-pseudo` | `RISCVPostRAExpandPseudoInsts.cpp` | `PseudoMovImm`、`PseudoMovAddr`、`PseudoMERGE` | RA 之后才能确定 scratch 寄存器 |
| `riscv-expand-pseudo` | `RISCVExpandPseudoInsts.cpp`（`RISCVExpandPseudo`） | 预发射阶段的收尾展开 | 最靠近 AsmPrinter，此时所有虚拟寄存器已消失 |
| `riscv-expand-atomic-pseudo` | `RISCVExpandAtomicPseudoInsts.cpp` | `PseudoAtomicLoadAdd*`、`PseudoAtomicSwap*` 等 | 需要物理寄存器做 LR/SC 循环，必须 post-RA |

一个容易混淆的点：`riscv-prera-expand-pseudo` 与 `riscv-expand-pseudo` 都在 `RISCVExpandPseudoInsts.cpp` 里，但它们是两个不同的 `MachineFunctionPass`，注册名不同，位置差了一整轮寄存器分配。看代码时先看类名（`RISCVPreRAExpandPseudo` vs `RISCVExpandPseudo`），不要只看文件名。

### 19.3 真实前后对比：PseudoLLA 变成 AUIPC + ADDI

输入是一段取全局变量地址的 IR：

```llvm
target triple = "riscv64-unknown-elf"
@g = global i64 7
define ptr @addr() {
  ret ptr @g
}
```

`-O2 -code-model=medium -stop-after=finalize-isel` 的 MIR 里，选指结果是：

```text
body:             |
    %0:gpr = PseudoLLA @g
    PseudoRET implicit $x10
```

整个地址计算只有一条伪指令，`PseudoRET` 也还在。再跑 `-print-after=riscv-prera-expand-pseudo`：

```text
# *** IR Dump After RISC-V Pre-RA pseudo instruction expansion pass (riscv-prera-expand-pseudo) ***:
  %1:gpr = AUIPC target-flags(riscv-pcrel-hi) @g, pre-instr-symbol <mcsymbol .Lpcrel_hi0>
  %0:gpr = ADDI %1:gpr, target-flags(riscv-pcrel-lo) <mcsymbol .Lpcrel_hi0>
  PseudoRET implicit $x10
```

三处信息值得逐字读：

- `target-flags(riscv-pcrel-hi)` / `riscv-pcrel-lo` 是两条指令各自的重定位标记，最终对应 `R_RISCV_PCREL_HI20` 与 `R_RISCV_PCREL_LO12_I`。
- `pre-instr-symbol <mcsymbol .Lpcrel_hi0>` 把两条指令绑在同一个标签上。这个标签就是后面汇编里看到的 `.Lpcrel_hi0`，链接器靠它把 hi/lo 配对。
- `PseudoRET` 没有被这个 pass 处理。它的展开在更后面。

最终汇编（同一输入，`-code-model=medium`）：

```asm
addr:
	.cfi_startproc
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(g)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)
	ret
	.cfi_endproc
```

`.Lpcrel_hi0` 出现在 `auipc` 之前，`%pcrel_lo` 引用它而不是直接引用 `g`。这不是可选的写法，而是 RISC-V psABI 的规定：`%pcrel_lo` 的符号必须是配对的 `%pcrel_hi` 所在的标签，链接器才能反推 hi 的偏移。

### 19.4 真实前后对比：PseudoVLE32_V_M1 到 vsetivli + vle32.v

向量是"伪指令必须存在"最有说服力的例子。`<4 x i32>` 的逐元素加法在 `finalize-isel` 之后是四条伪指令：

```text
body:             |
    %3:vr = PseudoVLE32_V_M1 $noreg, %0, 4 /* vl */, 5 /* e32 */, 2 /* tu, ma */ :: (load (s128) from %ir.a)
    %4:vr = PseudoVLE32_V_M1 $noreg, %1, 4 /* vl */, 5 /* e32 */, 2 /* tu, ma */ :: (load (s128) from %ir.b)
    %5:vr = PseudoVADD_VV_M1 $noreg, killed %3, killed %4, 4 /* vl */, 5 /* e32 */, 1 /* ta, mu */
    PseudoVSE32_V_M1 killed %5, %2, 4 /* vl */, 5 /* e32 */ :: (store (s128) into %ir.c)
```

注意 `$noreg` 这个操作数。它占位的是 `vtype`（或者更准确地说，是 vsetvli 产生的配置），此刻还没有被赋值。`-print-before=riscv-insert-vsetvli` 显示的是同四条伪指令，但寄存器已被分配：

```text
64B	  renamable $v8 = PseudoVLE32_V_M1 undef renamable $v8(tied-def 0), %0:gpr, 4, 5, 2 :: (load (s128) from %ir.a)
80B	  renamable $v9 = PseudoVLE32_V_M1 undef renamable $v9(tied-def 0), %1:gpr, 4, 5, 2 :: (load (s128) from %ir.b)
96B	  renamable $v8 = PseudoVADD_VV_M1 undef renamable $v8(tied-def 0), killed renamable $v8, killed renamable $v9, 4, 5, 1
128B	  PseudoVSE32_V_M1 killed renamable $v8, %2:gpr, 4, 5 :: (store (s128) into %ir.c)
```

`-print-after=riscv-insert-vsetvli` 多出一条配置指令，并给每条向量指令补了两个隐式操作数：

```text
56B	  dead $x0 = PseudoVSETIVLI 4, 208, implicit-def $vl, implicit-def $vtype
64B	  renamable $v8 = PseudoVLE32_V_M1 undef renamable $v8(tied-def 0), %0:gpr, 4, 5, 2, implicit $vl, implicit $vtype :: ...
...
```

`208` 是 `vtype` 的编码值：`e32`、`m1`、`ta`、`ma` 组合出来的位模式。最终汇编把它打印成人读形式：

```asm
vsetivli	zero, 4, e32, m1, ta, ma
vle32.v	v8, (a0)
vle32.v	v9, (a1)
vadd.vv	v8, v8, v9
vse32.v	v8, (a2)
ret
```

为什么不能在第 19.1 步（选指）就插入 `vsetivli`？因为它要满足"同一个基本块内配置不重复"的优化：如果连续四条向量指令都用同样的 `e32,m1,ta,ma`，只应插一条。这个判断需要看到整块指令、知道 `vl`、`vtype` 的活跃区间，还要处理控制流汇合点。`RISCVInsertVSETVLI.cpp` 里的 `needVSETVLI`、`computeVLVTYPEChanges`、`needVSETVLIPHI` 就是为这三件事服务的。

### 19.5 PseudoTAIL 与 PseudoRET：一直活到编码期

把尾调用编译成 medium 代码模型：

```llvm
define void @tcall() {
  tail call void @foo()
  ret void
}
```

`-print-after=riscv-expand-pseudo` 的输出里，`PseudoTAIL` 仍然存在：

```text
  PseudoTAIL target-flags(riscv-call) @foo, <regmask $vlenb $x0 $x1 $x8 ...>, implicit $x2
```

这说明它不归这个 pre-emit pass 管。汇编输出打印的是 `tail foo`，而目标文件反汇编里是：

```text
000000000000000c <tcall>:
       c: 00000317     	auipc	t1, 0x0
      10: 00030067     	jr	t1
```

`auipc` + `jalr x0, 0(t1)` 是 `tail` 的真实编码。`jr` 是 `jalr x0, 0(rs)` 的别名。`PseudoRET` 同理，编码后是 `jalr x0, 0(ra)`，反汇编打印成 `ret`。

这段还有一个小细节：`addr` 的目标文件反汇编里有

```text
       0: 00000517     	auipc	a0, 0x0
       4: 00050513     	mv	a0, a0
```

`mv a0, a0` 看起来像空操作，实际是 `addi a0, a0, 0` 被汇编打印机规范化成 `mv` 别名。重定位填的是 0，所以 `addi` 的立即数是 0。这个现象在阅读未链接目标文件时非常常见，不要误判为编译器生成了冗余指令。

### 19.6 原子伪指令：没有 Zaamo 时的 LR/SC 循环

新版本的 `+a` 会同时打开 `zaamo` 与 `zalrsc`，此时 `atomicrmw add` 直接选成 `amoadd.d.aqrl`。但 `RISCVInstrInfoA.td` 里那组 `PseudoAtomic*` 的谓词是

```tablegen
let Predicates = [HasStdExtZalrsc, NoStdExtZaamo] in {
  ...
  def PseudoAtomicLoadAdd32 : PseudoAMO;
  ...
}

let Predicates = [HasStdExtZalrsc, NoStdExtZaamo, IsRV64] in {
  ...
  def PseudoAtomicLoadAdd64 : PseudoAMO;
  ...
}
```

也就是说只有"有 LR/SC、没有 AMO"的配置才会用到它们。用 `-mattr=+zalrsc,-zaamo` 编同一个函数，MIR 里出现：

```text
  early-clobber renamable $x12, dead early-clobber renamable $x13 = PseudoAtomicLoadAdd64 killed renamable $x10, killed renamable ...
```

`early-clobber` 是这里的关键：LR/SC 循环需要一个临时寄存器保存 `lr` 的结果、另一个保存 `sc` 的返回值，而且不能与地址/增量寄存器重合。这种约束只有寄存器分配之后才能满足，所以 `riscv-expand-atomic-pseudo` 是 post-RA pass。展开后是：

```text
  $x12 = LR_D_AQRL $x10
  $x13 = SC_D_RL $x13, $x10
  BNE $x13, $x0, %bb.1
```

最终汇编：

```asm
.LBB0_1:
	lr.d.aqrl	a2, (a0)
	add	a3, a2, a1
	sc.d.rl	a3, a3, (a0)
	bnez	a3, .LBB0_1
```

如果把 `PseudoAtomicLoadAdd64` 提前到选指阶段展开，`early-clobber` 约束就落不到物理寄存器上，RA 可能把 scratch 分配到与地址相同的寄存器，循环立刻出错。这是"伪指令必须存在"最硬的一条证据。

### 19.7 收尾 pass：伪指令展开之后的优化

伪指令展开完，机器层还有一批 pass 会把展开出来的指令再优化回去。它们的源码注释说明了各自的边界：

- `RISCVOptWInstrs.cpp`："This pass does some optimizations for *W instructions at the MI level. First it removes unneeded sext.w instructions." 这解释了为什么 GISel 留下的 `sext.w` 有时会消失。
- `RISCVMakeCompressible.cpp`："This pass searches for instructions that are prevented from being compressed by ... the use of a single uncompressed register." 它通过改寄存器编号让 `add` 能变成 `c.add`。
- `RISCVMoveMerger.cpp`："performs move related peephole optimizations as Zcmp has specified. This pass should be run after register allocation." 对应 `riscv-move-merge`，在 `stack-frame-layout` 之后。
- `RISCVLoadStoreOpt.cpp`：`riscv-load-store-opt`，在 post-RA 伪指令展开之后，处理访存合并。
- `RISCVVMV0Elimination.cpp`：消除多余的 `vmv0`，属于向量收尾。

这些 pass 的顺序不是随意的。举例：`riscv-make-compressible` 在 `branch-relaxation` 之后、`stack-frame-layout` 之前；它要改寄存器编号，所以必须在 RA 之后；它又要影响最终指令长度，所以必须在分支放松之后。改 pass 顺序会同时影响压缩率和分支偏移，这是 RISC-V 后端里最容易踩的顺序坑之一。

## 可跑实验

### 实验 1：四个阶段看同一段代码

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\19-pseudo.ps1
```

脚本会在 `%TEMP%\rvlab19` 写入 `pseudo.ll`、`vec.ll`、`atomic.ll`，并依次打印：

- `-stop-after=finalize-isel` 的 `PseudoLLA` / `PseudoTAIL` / `PseudoRET`。
- `-print-after=riscv-prera-expand-pseudo` 的 `AUIPC` + `ADDI` 对。
- 最终汇编里的 `.Lpcrel_hi0`、`%pcrel_hi(g)`、`%pcrel_lo(.Lpcrel_hi0)`。
- `-filetype=obj` 后 `llvm-objdump -d` 的 `auipc t1` + `jr t1`。

### 实验 2：向量配置的插入点

同一脚本对 `vec.ll` 跑三次：`-stop-after=finalize-isel`、`-print-before=riscv-insert-vsetvli`、`-print-after=riscv-insert-vsetvli`。预期看到：

```text
before:  PseudoVLE32_V_M1 ... 4, 5, 2
after:   PseudoVSETIVLI 4, 208, implicit-def $vl, implicit-def $vtype
         PseudoVLE32_V_M1 ... 4, 5, 2, implicit $vl, implicit $vtype
final:   vsetivli zero, 4, e32, m1, ta, ma
```

### 实验 3（失败/边界）：在错误的 pass 上等 PseudoTAIL 消失

把 `-print-after=riscv-expand-pseudo` 的输出过滤 `PseudoTAIL`，本机会看到它仍然在。掉进的坑是"名字里有 expand，就以为它展开所有伪指令"。正确的判据是源码里的 `case`：`RISCVExpandPseudo::expandMI` 处理 `PseudoLA_TLS_*`、`PseudoCC*`、RV32 的 Zdinx 等，`PseudoTAIL` 不在其中；`PseudoTAIL` 的展开点在 MC 层，由编码器完成。

### 实验 4：原子伪指令的两种世界

```powershell
$bin = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin"
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+a -O2 atomic.ll -o -
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+zalrsc,-zaamo" -O2 atomic.ll -o -
```

第一条输出 `amoadd.d.aqrl`（单条 AMO），第二条输出 `lr.d.aqrl` / `sc.d.rl` / `bnez` 循环。同一个 IR、同一个优化级别，差别只来自 feature 谓词 `NoStdExtZaamo`。

## 源码落点

路径相对 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。

| 位置 | 符号 | 读它回答什么问题 |
|---|---|---|
| `RISCVInstrInfo.td` | `PseudoLI`（约 1035 行）、`PseudoRET`（约 1876 行）、`PseudoTAIL`（约 1884 行）、`PseudoLA`（约 1943 行） | 伪指令的定义、`PseudoInstExpansion`、`AsmString`、Size |
| `RISCVExpandPseudoInsts.cpp` | `RISCVPreRAExpandPseudo::expandMI`、`expandAuipcInstPair`、`expandLoadLocalAddress`、`RISCVExpandPseudo::expandMI`、两个 `INITIALIZE_PASS` | pre-RA 与 pre-emit 两个展开器各处理哪些 opcode |
| `RISCVPostRAExpandPseudoInsts.cpp` | `expandMovImm`、`expandMovAddr`、`expandMERGE` | post-RA 展开的三种伪指令 |
| `RISCVExpandAtomicPseudoInsts.cpp` | `PseudoAtomicLoadAdd32/64`、`PseudoAtomicSwap32/64` 等 case | LR/SC 循环怎么生成 |
| `RISCVInsertVSETVLI.cpp` | `needVSETVLI`、`computeVLVTYPEChanges`、`needVSETVLIPHI`、`runOnMachineFunction` | vsetvli 何时插入、何时可复用 |
| `RISCVInstrInfoA.td` | `PseudoAMOPat`、`PseudoAtomicLoadAdd64`、谓词 `HasStdExtZalrsc, NoStdExtZaamo` | 原子伪指令的触发条件 |
| `RISCVTargetMachine.cpp` | `addPreSched2`、`addPreEmitPass` 里的 `createRISCVPostRAExpandPseudoPass`、`createRISCVExpandPseudoPass`、`createRISCVExpandAtomicPseudoPass` | 五个 pass 的装配顺序 |

## 常见坑

1. **以为 `riscv-expand-pseudo` 展开所有伪指令**。它不处理 `PseudoTAIL`、`PseudoRET`，这两个在 MC 层展开；它也不处理原子伪指令。
2. **把 pre-RA 与 pre-emit 混为一谈**。两个类都在 `RISCVExpandPseudoInsts.cpp`，注册名分别是 `riscv-prera-expand-pseudo` 与 `riscv-expand-pseudo`，中间隔着一整轮寄存器分配。
3. **在选指阶段手动展开 `PseudoLLA`**。两条指令必须写同一个物理寄存器，提前展开会让 RA 失去这个约束。
4. **忽略 `%pcrel_lo` 必须引用 `.Lpcrel_hi*` 标签**。直接写 `%pcrel_lo(g)` 会被汇编器或链接器拒绝，因为 hi/lo 配对信息丢失。
5. **把 `mv a0, a0` 当成冗余指令**。它常常是 `addi a0, a0, 0` 的别名打印，立即数由重定位填 0。
6. **以为 `+a` 一定生成 `amoadd`**。`+zalrsc,-zaamo` 会走 LR/SC 循环，这是 feature 谓词决定的，不是优化级别。
7. **在 RA 前插 `vsetivli`**。配置复用需要整块视野与 `vl`/`vtype` 活跃信息，`riscv-insert-vsetvli` 放在 RA 附近正是为此。
8. **调换 `riscv-make-compressible` 与 `branch-relaxation` 的顺序**。前者改变指令长度，后者依赖长度计算；顺序错了会出现分支偏移错误或压缩率下降。

## 自我检查

- [ ] 能说出伪指令存在的三类原因（物理寄存器未定、立即数范围未定、regmask/ABI 信息未定），并各举一条指令。
- [ ] 能列出五个与伪指令相关的 pass 及其在 pass 序列中的位置。
- [ ] 能画出 `PseudoLLA` → `AUIPC` + `ADDI` → `.Lpcrel_hi0` 汇编的真实链路。
- [ ] 能解释 `PseudoVSETIVLI 4, 208` 里的 `4` 和 `208` 分别是什么，以及为什么 `vl`/`vtype` 是隐式操作数。
- [ ] 能解释 `PseudoTAIL` 为什么在 `riscv-expand-pseudo` 之后仍然存在，以及它在哪一层变成 `auipc` + `jalr`。
- [ ] 能复现 `+a` 与 `+zalrsc,-zaamo` 两种原子序列，并说明 `early-clobber` 的作用。
- [ ] 能说出 `riscv-opt-w-instrs`、`riscv-make-compressible`、`riscv-move-merge` 各自消掉什么。
- [ ] 能用 `-print-before=` / `-print-after=` 定位一个 pass 的输入输出，而不是只看最终汇编。

## 延伸阅读

- RISC-V psABI 中 `%pcrel_hi` / `%pcrel_lo` 的配对规则，以及 `.Lpcrel_hi*` 标签的约定。
- `llvm/test/CodeGen/RISCV/` 下与 `pseudo`、`vsetvli`、`atomic` 相关的 lit 测试：每个都是某一 pass 前后的 MIR 对。
- 本课程第 20 章（AsmPrinter）：`lowerPseudoInstExpansion` 在 `emitInstruction` 里的位置，正好接上本章 19.5。
- 本课程第 18 章（调度模型）：`riscv-insert-vsetvli` 与 `postmisched` 的相对顺序。
- `RISCVInsertVSETVLI.cpp` 顶部的注释：VSETVLI 插入与 `vl` 输出展开的设计说明。

[上一章：调度模型与 llvm-mca](./18-scheduling-and-mca.md) | [下一章：AsmPrinter](./20-asm-printer.md)