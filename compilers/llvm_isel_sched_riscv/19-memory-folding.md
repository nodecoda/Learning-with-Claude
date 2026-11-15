# Ch 19 — 内存操作数与 folding：RISC-V 没有 indexed addressing 意味着什么

> **目标**：理解 RISC-V 只有「基址寄存器 + 12-bit 立即数」这一种内存寻址，因而地址计算无法藏进 load/store；理解 folding 的边界（2047/2048、-2048）、全局地址的 hi/lo 合成，以及这些选择如何改变调度器看到的依赖链。学完你能对着一段 GEP 预测它会选成几条地址指令、哪一条能折进 `ld/sd`。
>
> **环境**：LLVM 24.0.0git；`llc -mtriple=riscv64 -mattr=+m`；`-print-after=finalize-isel`；`riscv-merge-base-offset`、`riscv-fold-mem-offset` pass；对照 `-O0` 与 `-O2`。所有汇编与 MIR 均为本机实跑。

## 为什么内存折叠属于「选择 × 调度」

x86 可以用 `mov rax, [rdi + rsi*8 + 16]` 一条指令完成「基址 + 变址 + 比例 + 偏移」。RISC-V 的 load 只有：

```text
ld  rd, imm(rs1)        # 有效地址 = rs1 + sign_extend(imm[11:0])
```

没有第二个寄存器，没有 scale，没有「基址 + 变址」。这意味着：

- `a[i]` 必须先算出 `a + i*8`，再 `ld 0(addr)`。
- 地址计算是一条真实的指令，进入 DAG，参与调度。
- 只有「基址 + 常数」且常数落在 `[-2048, 2047]` 时，才能把加法折进内存操作数。

所以「内存折叠」不是单纯的 peephole，它直接决定：

1. DAG 里有没有一条 `ADD` 节点。
2. load 的地址依赖链有多长。
3. 寄存器压力：地址寄存器是否需要一个额外的 live range。
4. 调度器能不能把地址计算提前，掩盖 load 延迟。

## 先建立直觉：两种寻址的 DAG 形状

同一段 `a[i]`：

```llvm
define i64 @ld_idx(ptr %p, i64 %i) {
  %q = getelementptr i64, ptr %p, i64 %i
  %v = load i64, ptr %q
  ret i64 %v
}
```

RISC-V 的 DAG（简化）：

```text
  i ──> SLLI(3) ──┐
                   ADD ──> LD(0) ──> ret
  p ──────────────┘
```

x86 的 DAG（简化）：

```text
  i ──> scale*8 ──┐
                   LOAD(base=p, index=i, scale=8, disp=0) ──> ret
  p ──────────────┘
```

差别不在「几条机器指令」，而在 **load 的地址是不是一个独立节点**。RISC-V 的 `LD` 有两个输入（地址寄存器、立即数），x86 的 load 可以有四个。调度器在 RISC-V 上必须先把 `ADD` 排进某个 cycle，才能让 `LD` 发射。

实跑结果：

```asm
ld_idx:
        slli    a1, a1, 3
        add     a0, a0, a1
        ld      a0, 0(a0)
        ret
```

MIR（`-print-after=finalize-isel`，`rocket-rv64`）：

```text
%2:gpr = SLLI %1:gpr, 3
%3:gpr = ADD %0:gpr, %2:gpr
%4:gpr = LD %3:gpr, 0 :: (load (s64) from %ir.q)
```

三条指令、两条依赖边。`SLLI` 和 `ADD` 都可以被调度器移动，但 `LD` 必须等 `ADD`。

## 主线一：常数偏移的 folding 边界

把索引换成常数，情况立刻不同。下面四个函数只差 GEP 的字节偏移：

```llvm
define i64 @ld_off8(ptr %p)    { %q = gep i8, ptr %p, i64 8;    load i64, ptr %q }
define i64 @ld_off2047(ptr %p) { %q = gep i8, ptr %p, i64 2047; load i64, ptr %q }
define i64 @ld_off2048(ptr %p) { %q = gep i8, ptr %p, i64 2048; load i64, ptr %q }
define i64 @ld_off_neg2048(ptr %p) { %q = gep i8, ptr %p, i64 -2048; load i64, ptr %q }
```

实跑汇编：

```asm
ld_off8:
        ld      a0, 8(a0)
        ret

ld_off2047:
        ld      a0, 2047(a0)
        ret

ld_off2048:
        addi    a0, a0, 2047
        ld      a0, 1(a0)
        ret

ld_off_neg2048:
        ld      a0, -2048(a0)
        ret
```

`2047` 能折，`2048` 不能。`2048` 的 lowering 很值得注意：它不是「加 2048 再 `ld 0`」，而是「加 2047 再 `ld 1`」。因为 `2047` 和 `1` 都在 12-bit 有符号范围内，这样只需一条 `addi`，且 load 的立即数非零。MIR 是：

```text
%1:gpr = ADDI %0:gpr, 2047
%2:gpr = LD %1:gpr, 1
```

如果写成 `addi 2048` 会直接非法（超出 12-bit），所以 legalize 把大偏移拆成「能折进内存的最大块 + 余数」。`-2048` 恰好是下界，能一条折进去。

再看更大的偏移：

| 偏移 | 汇编 | 地址指令数 |
|---:|---|---:|
| 8 | `ld a0, 8(a0)` | 0 |
| 2047 | `ld a0, 2047(a0)` | 0 |
| 2048 | `addi a0,a0,2047; ld a0,1(a0)` | 1 |
| 4095 | `lui a1,1; add a0,a0,a1; ld a0,-1(a0)` | 2 |
| 4096 | `lui a1,1; add a0,a0,a1; ld a0,0(a0)` | 2 |
| -4096 | `addi a0,a0,-2048; ld a0,-2048(a0)` | 1（两条 12-bit） |
| 8191 | `lui a1,2; add a0,a0,a1; ld a0,-1(a0)` | 2 |

这张表说明 folding 不是「有偏移就折」，而是 **legalize 先构造一个可表示的地址序列，最后一块尽量折进内存操作数**。`-4096` 用两条 `addi` 而不是 `lui+add`，因为 `-2048 + -2048` 恰好拼出 `-4096`，少一条指令且都合法。

还有一个与 folding 相关但更晚的机制：压缩指令。`c.ld` 的偏移范围比 `ld` 小（8-bit 无符号、按 8 字节缩放），且基址必须是 `x8`–`x15`。`riscv-make-compressible` 在 post-RA 调度之后运行，会尝试重写 `addi` 或调整寄存器分配，使某条 `ld` 能变成 `c.ld`。它不改变语义，但会改变最终指令的字节数与编码；分析「为什么这条 load 没有压缩」时，不要把它和本章的 DAG folding 混为一谈。

## 主线二：全局地址与 `PseudoMovAddr`

全局变量的地址在 medlow 模型下是 `lui %hi(sym) + addi %lo(sym)`。ISel 可能先选成一条伪指令 `PseudoMovAddr`，再由 post-RA 的 `riscv-expand-pseudo-post-ra` 展开成 `LUI+ADDI`。第 20 章会展开伪指令；这里只看它对内存折叠的影响。

取 LLVM 自带测试里的形状（外部 hidden 全局，偏移 8）：

```llvm
@values = external hidden local_unnamed_addr global [2 x i64], align 8

define i64 @test() {
  %value = load i64, ptr getelementptr inbounds (i8, ptr @values, i64 8), align 8
  ret i64 %value
}
```

`riscv-merge-base-offset` 之前：

```text
%0:gpr = PseudoMovAddr target-flags(riscv-hi) @values, target-flags(riscv-lo) @values
%1:gpr = LD %0:gpr, 8
```

之后：

```text
%0:gpr = LUI target-flags(riscv-hi) @values + 8
%1:gpr = LD %0:gpr, target-flags(riscv-lo) @values + 8
```

这个 pass 做的事情不是「删一条 addi」，而是**把偏移从 load 的立即数搬到 hi/lo 重定位对里**，让 `LUI` 直接算 `@values+8`，`LD` 的 `%lo` 也指向 `@values+8`。这样地址仍然只有一条 `LUI`，load 的偏移变成 0。源码里的约束很明确（`RISCVMergeBaseOffset.cpp`）：

- `lui`/`auipc` 的结果只能有一个 use，就是那条 `addi`；
- 地址操作数必须是 `%hi`/`%lo` 对；
- 原地址的 offset 必须是 0。

如果全局地址先被别的东西用了（比如取地址传给函数），就不能合并。这也是「选择影响调度」的一个侧面：`PseudoMovAddr` 是一条节点，合并后变成 `LUI` 一条节点，load 的地址依赖从「两条地址指令」缩成「一条」。

另一个 pass `riscv-fold-mem-offset` 处理的是普通 `addi` 链：

```text
%a = ADDI %base, 8
%b = ADDI %a, 16
%v = LD %b, 0
```

如果 `%b` 只被 load/store 使用，且总偏移仍在 12-bit 内，就把 `24` 折进 `LD`，删掉两条 `addi`。源码注释写得很清楚：中间可以夹杂其他算术指令，只要最终结果只用于地址。这个 pass 在 `addMachineSSAOptimization()` 里运行，位于 pre-RA scheduler 之前，所以它改变的是 scheduler 看到的 DAG。

## 主线三：折叠如何改变调度

考虑一个循环：

```llvm
for (i = 0; i < n; i++)
  sum += a[i];
```

如果没有 strength reduction，每轮都要 `slli+add+ld`；LLVM 的 LSR 会把索引变成指针增量，生成：

```asm
ld   a2, 0(a0)
addi a0, a0, 8
...
```

地址计算从「两条指令」变成「一条 `addi`」，而且这条 `addi` 与 load 的地址是**同一条链**。调度器可以把 `addi` 提前到上一轮，但无法把它删掉。若循环展开，多个 `ld` 可以共享一个基址、用不同偏移：

```asm
ld a2, 0(a0)
ld a3, 8(a0)
ld a4, 16(a0)
addi a0, a0, 24
```

这时三条 load 共享一个地址寄存器，folding 让每条 load 只付一个立即数，地址链只有一条 `addi`。**折叠的直接收益是减少地址节点，间接收益是让多个 load 可以并行发射。**

反过来，`ld_off2048` 的例子说明 folding 失败会多一条 `ADDI`，而这条 `ADDI` 在关键路径上：

```text
base ──> ADDI(2047) ──> LD(1) ──> use
```

如果这个 load 是循环里的，`ADDI` 每轮都要算一次（除非 LSR 把它变成指针增量）。面试里问「为什么这里多了一条 addi」，答案往往就是「偏移超出 12-bit，folding 失败」。

## 主线四：栈槽地址同样受 12-bit 约束

栈上的对象不是「免费」的。小对象可以直接用 `sp + 立即数`：

```llvm
define void @small(i64 %v) {
  %buf = alloca [2 x i64], align 8
  %q = getelementptr [2 x i64], ptr %buf, i64 0, i64 1
  store i64 %v, ptr %q
  ret void
}
```

```asm
small:
        addi    sp, sp, -16
        sd      a0, 8(sp)
        addi    sp, sp, 16
        ret
```

`sd a0, 8(sp)` 是一条指令，偏移 8 折进 store。把对象放大到 600 个 `i64`（4800 字节）后，帧大小本身超过 12-bit：

```asm
large:
        lui     a1, 1
        addi    a1, a1, 720        # a1 = 4816
        sub     sp, sp, a1
        lui     a1, 1
        addi    a2, sp, 16
        add     a1, a2, a1          # a1 = sp + 16 + 4096
        sd      a0, 696(a1)         # 4792 = 4096 + 696
        lui     a0, 1
        addi    a0, a0, 720
        add     sp, sp, a0
        ret
```

这里出现了三条地址相关指令（`lui`、`addi`、`add`）才拼出 `sp+4792`。原因和第 2048 的例子一样：`4792` 超出 12-bit，必须拆成 `4096 + 696`，`4096` 用 `lui 1` 造出来，`696` 折进 `sd`。栈帧的分配/回收也要用 `lui+addi` 构造 4816，因为 `addi sp, sp, -4816` 非法。

对调度器来说，这些地址指令都在 prologue/epilogue 或 store 之前，属于真实依赖。大栈帧函数的 prologue 会明显变长，这也是「减少栈对象、复用栈槽」在 RISC-V 上比在 x86 上更值钱的原因之一。

## 踩雷

1. **GEP 的索引会按元素大小缩放**。`getelementptr i64, ptr %p, i64 %i` 的字节偏移是 `i*8`，所以先 `slli 3`；`getelementptr i8, ptr %p, i64 %i` 不缩放，直接 `add`。看到 `slli` 不要以为多余。
2. **`2048` 不是「加 2048 再 ld 0」**。legalize 会拆成 `addi 2047; ld 1`，因为两条立即数都必须落在 `[-2048,2047]`。自己写汇编测试时不要照直觉写 `addi 2048`，那是非法指令。
3. **`-2048` 能折，`-2049` 不能**。下界是包含的；`-2049` 会变成 `addi -2048; ld -1` 或 `lui+add`，取决于具体序列。
4. **`RISCVMergeBaseOffset` 要求 `lui` 单 use**。如果全局地址还被用于比较、传参或调试信息，pass 会放弃合并；`-print-before/after=riscv-merge-base-offset` 能直接看到它有没有动。
5. **`PseudoMovAddr` 在 `finalize-isel` 里不是最终形态**。不要把它当成一条真实指令去数周期；它会在 post-RA 展开成 `LUI+ADDI`，而这两条在 pre-RA scheduler 眼里是**一条**伪指令。
6. **`-O0` 的折叠行为不同**。FastISel 不跑，SelectionDAG 仍然会做 legalize，但缺少 LSR、GVN 等 IR pass，地址形状可能更差；比较优化效果必须固定 `-O2`。

## 动手练习

1. 对 `ld_off_4095`、`ld_off_4096`、`ld_off_8191` 分别写出 MIR，标出每条指令的立即数，验证「最后一块折进 load」的规律。
2. 写一个 `struct { char pad[2048]; long x; }` 的 GEP，访问 `x`，观察是否生成 `addi 2047; ld 1`。再把 `pad` 改成 2040，观察是否一条 `ld` 解决。
3. 用 `-print-before=riscv-merge-base-offset` 和 `-print-after=riscv-merge-base-offset` 对比一个外部全局的 load，记录 `PseudoMovAddr` 与 `LUI` 的差别。
4. 写一个循环，手动做「指针增量」与「索引缩放」两个版本，用 `llvm-mca` 比较地址链对周期数的影响。

## 重点整理

- RISC-V 只有 `base + 12-bit imm`，没有 indexed addressing；变址必须显式算成一条 `ADD`，进入 DAG。
- folding 的边界是 `[-2048, 2047]`；大偏移会被拆成「LUI/ADDI 序列 + 最后一个可折块」，不是简单的一条 `addi`。
- `RISCVMergeBaseOffset` 把偏移搬进 `%hi/%lo` 重定位对；`RISCVFoldMemOffset` 把 `addi` 链折进 load/store 的立即数。
- 折叠减少地址节点、缩短 load 的依赖链；折叠失败会多一条 `ADDI`，直接落在关键路径上。
- 看到地址指令，先问「它是变址缩放、超范围拆分，还是全局地址合成」，再谈调度。

## 自我检核

- [ ] 我能解释为什么 `ld a0, 0(a0)` 前面必须有一条 `add`，而 x86 不需要。
- [ ] 我能预测偏移 2047、2048、4095、-4096 的汇编形状。
- [ ] 我知道 `PseudoMovAddr` 与 `LUI+ADDI` 的关系，以及 `riscv-merge-base-offset` 改了什么。
- [ ] 我能用 `-print-after=finalize-isel` 找到 load 的地址依赖链。
- [ ] 我能说出 folding 失败对调度和寄存器压力的两个影响。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVMergeBaseOffset.cpp`：读文件头的两种 pattern（medlow / medany）与三条接受条件；和本章 `@values+8` 的例子对照。
- `llvm/lib/Target/RISCV/RISCVFoldMemOffset.cpp`：读 `foldOffset` 如何累积偏移、如何检查「只用于地址」；理解为什么它能在 `addMachineSSAOptimization` 阶段改变 DAG。
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td` 中 `AddrRegImm` / `AddrStack` 的 pattern：看 `LD`/`SD` 的 `simm12` 操作数如何被 TableGen 约束。
- `llvm/test/CodeGen/RISCV/fold-addi-loadstore.ll`：官方测试覆盖了哪些 folding 边界；挑两个 case 用本章命令复现。
- `llvm/docs/CommandGuide/llc.rst` 的 `-print-before` / `-print-after`：确认 pass 名与输出位置。

→ [下一章：伪指令、宏融合与调度可见性](./20-pseudo-expansion.md)
