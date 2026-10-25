# 练习 D：调度与链接审计

> 目标：把第 18 章的 `llvm-mca` 与第 20 章的汇编/重定位知识接起来，做一次完整的"改写—度量—链接—核对"。本练习分两部分：先用 mca 比较三种循环写法并解释瓶颈，再把一个带全局变量的目标文件链接成 ELF，逐字节核对重定位如何落地。

> 环境：LLVM 23.1.4 预编译（含 `llvm-mca`、`llc`、`llvm-mc`、`llvm-readobj`、`llvm-objdump`、`ld.lld`）。命令通过 `compilers/riscv_backend/code/tools/run.ps1` 执行。所有数字都是本机实测，供你对照，不要求你复现出完全相同的毫秒级结果。

## 为什么

单独看 mca 的报告，很容易停在"IPC 0.5 好像不高"这种层面；单独看 `readobj`，也很容易停在"有一堆 R_RISCV_* 看不懂"。把两件事放在一起练，才能建立一条完整的因果链：

```text
  源码/IR
    |  llc                        -> 汇编（第 20 章的打印规则）
    |  llvm-mc / llc -filetype=obj -> 目标文件（重定位还没落地）
    |  llvm-readobj --relocations  -> 看到 HI20/LO12 这类"待填的洞"
    |  ld.lld                      -> 填洞、放松、确定地址
    |  llvm-objdump -d             -> 看到最终立即数与最终指令长度
```

mca 那部分训练的是"读模型、找瓶颈、验证改写"；链接那部分训练的是"读重定位、算地址、验证放松"。两者共同的方法是同一套：**先预测，再测量，最后解释差值**。预测错了不是失败，是发现模型边界的机会。

## 主线

### D.1 第一部分：三种循环写法的比较

给定一个逐元素做 `c[i] = a[i] + k` 的循环。基线写法（记为 A）每次处理一个元素：

```asm
.L_a:
  ld    a1, 0(a0)
  add   a1, a1, a2
  sd    a1, 0(a3)
  addi  a0, a0, 8
  addi  a3, a3, 8
  addi  a4, a4, -1
  bnez  a4, .L_a
```

这是编译器在 `-O2` 下很可能生成的形态：一个指针、一个数据指针、一个计数器，三条数据指令加三条控制/地址指令。

要求你完成四件事：

1. 用 `llvm-mca -mtriple=riscv64 -mcpu=sifive-u74` 测 A，记录 `Total Cycles`、`IPC`、`Block RThroughput`。
2. 把 A 改写成每次处理两个元素（B），要求两条数据链互不依赖，地址增量改成 16，计数器改成 -2。
3. 再把 B 改写成每次处理四个元素（C），地址增量 32，计数器 -4，用立即数偏移 `8/16/24` 而不是四条独立指针。
4. 把三次的 `Total Cycles` 除以"每轮处理的元素数 × 迭代数"，得到**每元素周期**，再判断哪个写法真正更好。

注意一个容易误判的点：mca 的 `Total Cycles` 是"整块重复 100 次"的总周期，不是"一轮循环"的周期。A 是 7 条指令，B 是 10 条，C 是 16 条；直接比总数会让 C 看起来最差。

### D.2 第二部分：链接审计

给定一段 IR，它有一个全局变量和三个函数：

```llvm
target triple = "riscv64-unknown-elf"
@g = global i64 42, align 8

define i64 @get_g() {
  %v = load i64, ptr @g, align 8
  ret i64 %v
}
define void @set_g(i64 %v) {
  store i64 %v, ptr @g, align 8
  ret void
}
define ptr @addr_g() {
  ret ptr @g
}
```

要求：

1. `llc -mtriple=riscv64 -mattr=+m -O2 -filetype=obj` 生成 `glob.o`。
2. `llvm-readobj --relocations glob.o` 记录所有 `R_RISCV_*`，并说明每一条对应哪条指令的哪个字段。
3. `llvm-objdump -d glob.o` 观察未链接时的立即数（通常是 0）。
4. `ld.lld -o glob.elf glob.o -e get_g` 链接，再用 `llvm-objdump -d --section=.text glob.elf` 观察立即数被填成什么。
5. 用 `llvm-readobj --sections glob.elf` 找到 `.data` 的地址，验证 `lui` 的高 20 位与 `ld/sd/addi` 的低 12 位拼起来正好等于该地址。
6. 再做一个放松实验：把 `call foo; ret` 写成汇编，加 `+relax` 编成对象，链接后确认 `call` 从 8 字节变成 4 字节的 `jal`。

第 6 步的判据不是"反汇编看起来像 jal"，而是"链接前后符号地址的差值"：`_start` 在 `0x11120`，`foo` 在 `0x11128`，两者相差 8；若 `call` 仍是 8 字节，`foo` 应在 `0x11128` 之后再加 4 字节的位置。实际 `ret` 落在 `0x11124`，正好证明 `call` 只占 4 字节。

### D.3 评分标准

| 项 | 合格 | 良好 | 优秀 |
|---|---|---|---|
| mca 读数 | 能抄下三个 `Total Cycles` | 能换算每元素周期 | 能解释 `Block RThroughput` 与资源压力的关系 |
| 瓶颈判断 | 说"依赖链"或"资源" | 用 `-timeline` 指出等待格 | 能说明顺序核与 OOO 模型的差异，并指出 mca 不建模的部分 |
| 改写 | 写出 B 或 C | B/C 都能跑通且数字下降 | 能说明收益递减与寄存器压力 |
| 重定位 | 能列出 `R_RISCV_HI20/LO12_I/LO12_S` | 能对应到具体指令字段 | 能算出 `0x12000 + 0x208 = 0x12208` |
| 放松 | 能跑出 `jal` | 能解释 `R_RISCV_RELAX` | 能说明为什么 `.option norelax` 会抑制它 |
| 边界 | 知道 mca 不是芯片 | 能举一个模型失真的例子 | 能说出哪些结论不能从 mca 推出 |

## 可跑实验

### 实验 1：一键跑完三组循环与链接审计

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\practice-d-sched-link.ps1
```

脚本在 `%TEMP%\rvlabd` 写入 `loop_a.s`、`loop_b.s`、`loop_c.s`、`glob.ll`、`link.s`，依次打印三组 mca 摘要、`glob.o` 的重定位与反汇编、链接后的反汇编与 `.data` 地址，以及放松前后的对比。你可以先不看下面的答案，自己抄一遍数字。

### 实验 2：先预测再测量

在跑脚本之前，先写下你的预测：A 的每迭代周期是多少？B 相对 A 能快多少？C 是否还能继续下降？跑完对照。预测偏差大的地方，通常就是你对 `IssueWidth`、`PipeA/PipeB` 或依赖间隔的理解有缺口。

### 实验 3（失败/边界）：把 mca 当芯片会得到什么

对同一个 `loop_c` 分别用 `-mcpu=sifive-u74` 和 `-mcpu=sifive-p670` 跑，记录 `Total Cycles` 与 `Dispatch Width`。两者会给出不同的周期数。这不说明哪颗核更快，只说明两份模型的 `MicroOpBufferSize`、`IssueWidth`、`LoadLatency` 不同。若你把其中任一数字写进性能报告当作"实测"，这份报告就是错的。

### 参考答案

<details><summary>参考答案</summary>

#### 第一部分：三组循环的真实数字

本机 `-mcpu=sifive-u74`、默认 100 次迭代：

| 变体 | 指令数 | Instructions | Total Cycles | IPC | Block RThroughput | 每迭代 | 每元素 |
|---|---|---|---|---|---|---|---|
| A（1 元素） | 7 | 700 | 603 | 1.16 | 3.5 | 6.03 | 6.03 |
| B（2 元素） | 10 | 1000 | 803 | 1.25 | 5.0 | 8.03 | 4.02 |
| C（4 元素） | 16 | 1600 | 1303 | 1.23 | 8.0 | 13.03 | 3.26 |

A 的资源压力是 `PipeA 3.49`、`PipeB 3.51`，两者几乎相等，说明这个块在两条整数流水线之间已经比较均衡；`Block RThroughput 3.5` 与"7 条指令 / 发射宽度 2 = 3.5"一致。它的 6.03 拍/迭代高于 3.5 的下界，差额来自 `ld → add → sd` 的依赖链与地址递增的串行性。

B 的 8.03 拍/迭代对应 4.02 拍/元素。相对 A 的 6.03，收益来自两点：两条数据链互不依赖，可以并行；循环控制指令（两条 `addi` 与一条 `bnez`）被摊到两个元素上。

C 的 13.03 拍/迭代对应 3.26 拍/元素。资源压力变为 `PipeA 10.00`、`PipeB 6.00`：四条 `ld` 与四条 `sd` 全部挤在 `PipeA`，`PipeA` 成为新的瓶颈。继续加深展开不会再有线性收益，因为访存端口已经饱和；再往下要走向量化（`vle32.v` / `vadd.vv` / `vse32.v`），而不是继续手工展开。

改写建议的优先级：

1. 先把循环展开到 4 元素，配 `8/16/24` 的立即数偏移，只保留一组指针递增——这就是 C。
2. 若 `PipeA` 已是瓶颈，检查能否把部分 `sd` 合并（例如连续元素用更宽的存储，或在允许的 ABI 下用向量）。
3. 不要继续展开到 8 元素：寄存器压力上升，`PipeA` 仍然只有一条，`Block RThroughput` 会继续线性上升，而每元素周期不再下降。

关于"哪种更好"的结论必须带条件：C 在 `sifive-u74` 的模型下每元素 3.26 拍，优于 B 的 4.02；但 C 的指令缓存占用是 A 的 2.3 倍，在真实前端受限的场景里未必更快。mca 不建模指令缓存与取指带宽，这是它的边界之一。

#### 第二部分：链接审计的真实结果

未链接的 `glob.o`，重定位是：

```text
0x0  R_RISCV_HI20    g
0x4  R_RISCV_LO12_I  g
0xC  R_RISCV_HI20    g
0x10 R_RISCV_LO12_S  g
0x18 R_RISCV_HI20    g
0x1C R_RISCV_LO12_I  g
```

六条对应三个函数的六条指令：`get_g` 的 `lui`+`ld`、`set_g` 的 `lui`+`sd`、`addr_g` 的 `lui`+`addi`。`HI20` 填 `lui` 的高 20 位，`LO12_I` 填 I 型指令（`ld`/`addi`）的低 12 位，`LO12_S` 填 S 型指令（`sd`）的低 12 位。此外 `.rela.eh_frame` 里还有三条 `R_RISCV_32_PCREL`，那是 CFI/FDE 的 PC 相对引用，与全局变量无关。

未链接反汇编里立即数全是 0：

```text
0000000000000000 <get_g>:
       0: 00000537     	lui	a0, 0x0
       4: 00053503     	ld	a0, 0x0(a0)
       8: 00008067     	ret
```

链接后：

```text
00000000000111e4 <get_g>:
   111e4: 00012537     	lui	a0, 0x12
   111e8: 20853503     	ld	a0, 0x208(a0)
   111ec: 00008067     	ret
```

`llvm-readobj --sections` 给出 `.data` 的地址是 `0x12208`。于是：

```text
  0x12 << 12 = 0x12000     （lui 的立即数 0x12，左移 12 位）
+ 0x208                    （ld 的低 12 位，符号扩展后仍是正数）
= 0x12208                  （正好是 .data，也就是 g 的地址）
```

`set_g` 用 S 型立即数，编码在指令的 `[31:25]` 与 `[11:7]` 两段里，反汇编显示成 `sd a0, 0x208(a1)`；`addr_g` 用 `addi`，对应 `LO12_I`。三者拼出的地址一致，这就是 HI20/LO12 配对的意义。

链接后 `llvm-readobj --relocations glob.elf` 是空的：所有静态重定位都已落地。动态重定位只有在 PIC/共享库场景才会保留，本章的默认代码模型不需要。

#### 第二部分附加：放松的字节证据

`link.s` 在 `+relax` 下编成对象后：

```text
0x0 R_RISCV_CALL_PLT foo 0x0
0x0 R_RISCV_RELAX    - 0x0
```

链接后反汇编：

```text
0000000000011120 <_start>:
   11120: 008000ef     	jal	0x11128 <foo>
   11124: 00008067     	ret

0000000000011128 <foo>:
   11128: 00008067     	ret
```

`call foo` 原来是 `auipc` + `jalr`（8 字节），被链接器放松成 `jal`（4 字节）。可以用两条独立证据确认：

1. `jal` 的目标是 `0x11128`，而 `_start` 在 `0x11120`，偏移 8；`jal` 的立即数编码是 `0x008000ef`，与 RISC-V J 型格式一致。
2. `_start` 的第二条指令 `ret` 在 `0x11124`，说明第一条只占 4 字节。若未放松，`ret` 应在 `0x11128`。

若在 `call foo` 外再包一层 `.option push` / `.option norelax` / `.option pop`，`R_RISCV_RELAX` 会消失（第 20 章实验 4），链接后保持 8 字节的 `auipc` + `jalr`。这就是"放松是链接器的决定，`.option norelax` 只是收回标记"的完整证据链。

#### 常见失分点

- 只看 `Total Cycles` 不看每元素周期，得出"B 比 A 慢"的错误结论。
- 把 `Block RThroughput` 当成实测周期。它只是下界，A 的 3.5 与 6.03 差了一倍多。
- 把 `lui 0x12` 当成"地址 0x12"。它左移 12 位后才是 `0x12000`。
- 以为链接后应当还有 `R_RISCV_HI20`。静态可执行文件里这些重定位会被消耗掉。
- 以为 `jal` 是 `call` 的直接替换。放松只在有 `R_RISCV_RELAX` 且目标在范围内时发生。

</details>

## 源码落点

| 位置 | 符号 | 与本练习的关系 |
|---|---|---|
| `RISCVSchedule.td` | `WriteIALU`、`WriteLDD`、`WriteSTD`、`WriteJmp` | A/B/C 三组指令的调度事件来源 |
| `RISCVSchedSiFive7.td` | `WriteRes<WriteLDD, ...>`、`WriteRes<WriteSTD, ...>`、`SiFive7SchedMachineModel` | 解释 `ld`/`sd` 的 Latency 与 `PipeA` 压力 |
| `RISCVInstrInfo.td` | `LD`、`SD`、`ADDI`、`BNE`、`CALL` 的 `Sched<[...]>` | 每条指令声明了哪些读写事件 |
| `RISCVAsmPrinter.cpp` | `lowerSymbolOperand`、`emitInstruction` | `%hi/%lo` 与重定位标志的打印路径 |
| `RISCVELFStreamer.cpp` | `.riscv.attributes`、relax 标记 | 目标文件里重定位与属性的落盘 |
| `RISCVAsmBackend.cpp` | `applyFixup`、relax 相关逻辑 | 链接器之外，汇编器如何写 fixup |
| `MCA/RISCVCustomBehaviour.cpp` | llvm-mca 的 RISC-V 自定义行为 | 为什么某些指令的 mca 读数需要特殊处理 |

## 常见坑

1. **直接比较三种写法的 `Total Cycles`**。B 是 803、C 是 1303，看起来 C 最差；换算成每元素周期后 C 最好（3.26 对 4.02 对 6.03）。
2. **把 `lui` 的立即数当地址**。`lui a0, 0x12` 表示 `0x12000`，必须左移 12 位再加低 12 位。
3. **期待链接后还有静态重定位**。默认代码模型下，`HI20/LO12` 在链接时被消耗，`readobj` 应当为空。
4. **用 `jal` 的出现判断放松**。判据是符号地址与后续指令位置；`jal` 也可能由其他原因产生。
5. **把 mca 的周期写进性能报告**。它不建模缓存、分支预测、指令缓存、内存别名；它适合比较写法，不适合预测绝对性能。
6. **在 `-mcpu` 不同时比较周期数**。`Dispatch Width` 与 `MicroOpBufferSize` 不同，数字不可比。
7. **忽略 `.rela.eh_frame` 里的 `R_RISCV_32_PCREL`**。它不属于全局变量访问，却会让"重定位总数"对不上，进而误判有遗漏。
8. **忘记 `-mattr=+relax`**。不加这个特性，`call foo` 根本不会带 `R_RISCV_RELAX`，放松实验会得出"链接器没工作"的错误结论。

9. **把 `.rela.eh_frame` 的 `R_RISCV_32_PCREL` 当成遗漏的全局访问**。它属于 CFI/FDE，不影响 `g` 的地址计算。
10. **用未链接对象的反汇编判断代码质量**。立即数全为 0，`mv a0, a0` 之类是重定位占位，不是最终形态。

## 自我检查

- [ ] 能算出 A/B/C 三种写法的每元素周期，并说明为什么不能直接比 `Total Cycles`。
- [ ] 能用 `Block RThroughput` 与资源压力解释 A 的 6.03 拍/迭代为什么高于 3.5 的下界。
- [ ] 能说明 C 的瓶颈从"依赖链"转移到"PipeA 压力"的证据。
- [ ] 能列出 `glob.o` 的六条静态重定位并对应到具体指令字段。
- [ ] 能算出 `0x12000 + 0x208 = 0x12208`，并与 `.data` 的 `readobj` 输出对上。
- [ ] 能解释链接后 `--relocations` 为空的原因。
- [ ] 能用符号地址与 `ret` 的位置证明 `call` 从 8 字节变成 4 字节。
- [ ] 能说出至少两个"mca 数字不能推出"的结论（缓存、分支预测、真实旁路、指令缓存占用）。

## 延伸阅读

- 本课程第 18 章：`SchedWrite`/`WriteRes`/`ReadAdvance` 的三层结构，以及 `-bottleneck-analysis` 对顺序核的限制。
- 本课程第 20 章：`%hi/%lo` 与 `%pcrel_hi/%pcrel_lo` 的打印路径，以及 `.option norelax` 的实验。
- 本课程第 21 章：重定位、代码模型与 `ld.lld` 的完整链路，本章只做了最小闭环。
- RISC-V psABI 的 relocation 章节：`R_RISCV_HI20` / `LO12_I` / `LO12_S` / `RELAX` 的位域定义。
- `llvm/test/CodeGen/RISCV/` 下的 `reloc`、`code-model` 测试：可以直接对照本章的六条重定位。

- LLVM 文档 `ELF Relocations`：`R_RISCV_HI20` 与 `LO12` 的位域与溢出检查。
- `lld/docs/` 中 RISC-V relaxation 的说明：哪些序列可以被放松、放松后对齐如何处理。
- `llvm/test/MC/RISCV/relocations.s`：每条重定位的最小汇编样例。
- 本课程 Final Project：把本章的审计方法用到自定义指令的端到端链路上。

[上一章：AsmPrinter](./20-asm-printer.md) | [下一章：重定位、代码模型与 ld.lld 链接](./21-relocations-and-linking.md)
