# Ch 19 — SASS 编码与调度控制位

> **目标**：看懂 cubin 里一条 SASS 指令的 128 位编码，知道哪一半承载「指令+操作数」、哪一半承载「调度/依赖与语义修饰」，并能用 `nvdisasm -hex` 做编码级对照。

> **环境**：Tesla T4（sm_75，Turing）、CUDA 12.4、`cuobjdump` / `nvdisasm`。

## 为什么需要这个？

PTX 是契约，SASS 才是硬件真正执行的东西。两个 kernel 的 PTX 可以一模一样，SASS 却可能因为调度不同而差出数十个百分点（经验值，不是本机实测）。如果你只读 PTX，就永远看不到最后一层。

更现实的是：自研 GPU 的 ISA 也要定义自己的编码与控制位。看 NVIDIA 怎么做（以及哪些信息不公开），能帮你判断「哪些必须自己设计、哪些可以留给工具链」。

## 先建立直觉

从 Volta（sm_70）开始，一条 SASS 指令是 **128 位 = 16 字节**：

```
 低 64 位                          高 64 位
┌──────────────────────┐   ┌──────────────────────────┐
│ opcode + 操作数 + 修饰 │   │ 调度/依赖控制（stall、    │
│ （LDG.E.SYS R0,[R2]） │   │ yield、barrier、部分修饰） │
└──────────────────────┘   └──────────────────────────┘
```

`cuobjdump -sass` 把高 64 位隐藏了，只给你反汇编文本；`nvdisasm -hex` 会把它打出来。**同一个指令、同样的操作数，高 64 位可以不同**——这就是调度。

## 一、先确认指令尺寸

`add.cubin` 的 `.text.add` 段大小与指令数：

```bash
cuobjdump -elf add.cubin | grep -E "\.text|\.nv\.info"
```

```
5     318     30  0  4         CUDA_INFO       0    3        0 .nv.info
6     348     70  0  4         CUDA_INFO      40    3        b .nv.info.add
b     580    100  0 80          PROGBITS       6    3  c000006 .text.add
```

`.text.add` 是 `0x100` 字节，函数里 16 条指令（`MOV/S2R/.../EXIT/BRA`）→ **每条 16 字节**。反汇编地址也是 `0x0, 0x10, 0x20...`，步长 16。

## 二、`nvdisasm -hex`：看到高 64 位

```bash
nvdisasm -hex -c add.cubin | sed -n '/add:/,/EXIT/p' | head -12
```

真实输出（节选）：

```
        /*0000*/                   MOV R1, c[0x0][0x28] ;      /* 0x00000a0000017a02 */
                                                                 /* 0x000fe40000000f00 */
        /*0010*/                   S2R R2, SR_CTAID.X ;          /* 0x0000000000027919 */
                                                                 /* 0x000e280000002500 */
        /*0020*/                   S2R R3, SR_TID.X ;            /* 0x0000000000037919 */
                                                                 /* 0x000e240000002100 */
        /*0030*/                   IMAD R2, R2, c[0x0][0x0], R3 ;/* 0x0000000002027a24 */
                                                                 /* 0x001fca00078e0203 */
```

每行两个 64 位字：第一个是指令编码，第二个主要是控制/依赖信息（也含部分语义修饰）。cubin 是小端、低字在前：地址 `N` 处是低 8 字节，`N+8` 处是高 8 字节，`nvdisasm` 的打印顺序与文件布局一致。写解析器时按这个顺序配对。

注意：

- `MOV` 的高字 `0x000fe40000000f00` 与 `S2R` 的 `0x000e280000002500` 明显不同。
- `IMAD` 的高字 `0x001fca00078e0203` 里也包含操作数扩展位与调度信息；**不要假设高字全是控制位**，字段是交错的。

## 三、关键实验：同一条指令，不同高 64 位

在 `memk.cubin` 里，`STG.E.SYS [UR4], R0` 出现在四个不同 kernel 中：

```
/*0110*/  STG.E.SYS [UR4], R0 ;   /* 0x00000000ff007986 */
                                   /* 0x000fe2000c10e904 */
/*0080*/  STG.E.SYS [UR4], R0 ;   /* 0x00000000ff007986 */
                                   /* 0x000fe2000c10e904 */
/*0050*/  STG.E.SYS [UR4], R0 ;   /* 0x00000000ff007986 */
                                   /* 0x000fe2000c10e904 */
/*0040*/  STG.E.SYS [UR4], R0 ;   /* 0x00000000ff007986 */
                                   /* 0x004fe2000c10e904 */
```

**低 64 位完全相同**（同一条指令、同样的寄存器），**高 64 位有一处不同**（`0x000f` vs `0x004f`）。这条差异来自调度上下文：前三条的 STG 不依赖紧邻的 load 结果，最后一条所在的 load→store 序列里 STG 要等 `LDG.E.SYS` 的数据（见 `plain.cubin`），scoreboard/stall 配置随之不同。

再看 `ptxas -O0` 与 `-O3` 的 `FADD`：

```
-O0:  FADD R0, R0, R2 ;   /* 0x0000000200007221 */  /* 0x003fde0000000000 */
-O3:  FADD R9, R2, R5 ;   /* 0x0000000502097221 */  /* 0x004fd00000000000 */
```

低字不同是因为寄存器不同；高字 `0x003fde...` vs `0x004fd0...` 的差异则反映了不同的调度配置。**不要急着把它翻译成「O0 等得更久」**：高字里 stall、yield、scoreboard 字段是交错的，单看位型不能断言具体等待周期；要确认，得用微基准（见本章「进阶」）。能确定的是：`-O` 级别改变了 `ptxas` 写入的调度字段，而不是 PTX 语义。

## 四、高 64 位里有什么（以及公开到什么程度）

公开资料能确认的：

- 每条指令带 **调度信息**：发射前要等多少个周期（stall）、是否让出（yield）、以及内存依赖的 **scoreboard 屏障编号**（read/write barrier）。
- 这些信息由 `ptxas` 在调度后写入，硬件按它发射。
- NVIDIA 的《CUDA Binary Utilities》文档只描述反汇编工具，**不公开位域布局**。社区工具（maxas 一类的反汇编器/汇编器）通过差分与微基准反推字段，但不同架构会变。

因此本课只做「可验证的观察」，不给你一张可能过时的位表：

| 观察 | 证据 |
|---|---|
| 指令 16 字节 | `.text` 大小 / 地址步长 |
| 低字含 opcode+操作数 | 同指令跨 kernel 低字相同 |
| 高字含调度状态 | 同指令跨 kernel 高字不同 |
| `ptxas -O` 改变高字 | `-O0` vs `-O3` 的 FADD |
| 高字字段非纯控制 | IMAD 的高字含操作数扩展 |

再补一组更干净的对照（同一组 sm_75 cubin：`plain.cubin` / `mem.cubin`；操作数完全相同，只改 PTX 的 strength/scope）：

```
LDG.E.SYS        R0, [UR4]   低字 0x00000004ff007981   高字 0x000ea2000c1ee900
LDG.E.STRONG.GPU R0, [UR4]   低字 0x00000004ff007981   高字 0x000ea2000c1f4900
STG.E.SYS        [UR4], R0   低字 0x00000000ff007986   高字 0x000fe2000c10e904
STG.E.STRONG.GPU [UR4], R0   低字 0x00000000ff007986   高字 0x000fe2000c114904
```

低字逐位相同，高字不同（`0x...ee900` vs `0x...f4900`、`0x...0c10...` vs `0x...0c11...`）。这比「跨 kernel 找不同」更能说明问题：**同一条指令的语义修饰（strength/scope）至少是差异来源之一**；当然，调度字段也可能同时变化——这正说明高字是交错的，不能把它简单叫作「控制位」。

如果你要自己解字段，正确方法是微基准：构造两条有已知依赖距离的指令，扫描 stall 值，观察高字哪几位变化，再做交叉验证。**不要相信没有实验支撑的位表。**

## 五、cubin 里还有什么

`cuobjdump -elf add.cubin` 的段表（实测）：

| 段 | 作用 |
|---|---|
| `.text.add` | SASS 指令（`PROGBITS`，align 0x80） |
| `.nv.info` / `.nv.info.add` | kernel 元数据（参数、寄存器、barrier 等） |
| `.nv.constant0.add` | kernel 参数常量 bank |
| `.nv.callgraph` | 调用图 |
| `.nv.rel.action` | 重定位动作 |
| `.debug_frame` | CFI |

`ELF header` 里的 `sm=75` 是硬约束：**SASS 不做跨 major 兼容**。sm_75 的 cubin 不能在 sm_80 上跑；只有同一 major 内的 minor 版本（如 sm_80 → sm_86）才谈得上二进制兼容。跨代要么重新编译 PTX，要么带多份 cubin（fatbin）。

cubin 是标准 ELF64，所以 LLVM 自带的 `llvm-readelf` 也能读：

```bash
llvm-readelf -h mem.cubin | grep -E "OS/ABI|Machine|Flags"
# OS/ABI: NVIDIA - CUDA
# Machine: NVIDIA CUDA architecture
# Flags:   0x4b054b, sm_75
llvm-readelf -S mem.cubin | grep -E "\.text|\.nv\.info|\.nv\.constant0"
```

但 `llvm-objdump -d` 会明确拒绝：

```
error: 'mem.cubin': no disassembler for target nvptx64-nvidia-cuda
```

这不是配置问题，而是 LLVM 根本没有 SASS 反汇编器。**PTX 是 LLVM 的边界，SASS 是 ptxas/驱动/`nvdisasm` 的边界**——这条分工在工具层也成立。

## 六、SASS 里的实用模式

| 模式 | 含义 |
|---|---|
| `LDG.E.SYS` | 普通 global load（weak） |
| `LDG.E.STRONG.GPU` | `.gpu` scope 的强 load |
| `LDG.E.CONSTANT.SYS` | `ld.global.nc`（只读缓存） |
| `STG.E.STRONG.SYS` | `volatile` store（强，但非原子） |
| `ATOMG.E.ADD.STRONG.GPU` | `.gpu` scope 的 atomic add |
| `MEMBAR.SC.GPU` / `.SYS` / `.CTA` | fence |
| `IMAD.WIDE` | 32×32→64 的乘加（地址计算） |
| `S2R` / `ULDC.64` | 读特殊寄存器 / uniform 常量加载 |
| `UR4` | Turing 的 uniform 寄存器（每 warp 一份，省寄存器） |

`UR4` 是 Turing 引入的 uniform datapath：kernel 参数、block 坐标这类「整个 warp 相同」的值放 uniform 寄存器，不占每线程寄存器。这也是为什么 `ptxas` 能把 12 个寄存器压得这么低。

## 踩雷集锦

1. **以为 `cuobjdump -sass` 就是全部**：它不显示控制位；要 `nvdisasm -hex`。
2. **以为高 64 位全是控制位**：IMAD 等指令的高字含操作数扩展，字段交错。
3. **照抄网上的位表**：不同架构不同，且多数没有可复现实验。
4. **拿 SASS 做跨架构比较**：SASS 跨 major 不兼容；跨代比较要先统一 PTX/编译参数。
5. **以为 `-O0` 只是「不优化」**：它同时改变调度与 stall，性能差异可能很大。
6. **忽略 uniform 寄存器**：统计寄存器压力时，`UR` 不计入每线程 `REG`，但会影响常量加载策略。

## 进阶：写一个最小的 SASS 注解器

不需要完整反汇编器。你可以：

1. 用 `nvdisasm -hex` 导出 `(地址, 低字, 高字)`。
2. 对同一 opcode 聚类高字，统计哪些位随「前一条指令的延迟」变化。
3. 用微基准验证：在两条指令间插入不同数量的 `NOP`/独立指令，观察高字与实测周期。

这是理解控制位最可靠的路，也是做自研 ISA 调度模型时该有的方法论。

一个可以直接用的解析骨架（Python，解析 `nvdisasm -hex -c` 的三元组）：

```python
import re, subprocess, sys

out = subprocess.run(["nvdisasm", "-hex", "-c", sys.argv[1]],
                     capture_output=True, text=True).stdout
pat = re.compile(
    r"/\*([0-9a-f]{4})\*/\s+(.*?);\s*"
    r"/\*\s*(0x[0-9a-f]{16})\s*\*/\s*"
    r"/\*\s*(0x[0-9a-f]{16})\s*\*/", re.S)

for m in pat.finditer(out):
    addr, text, lo, hi = m.groups()
    print(addr, text.strip(), lo, hi)
```

把输出导成 CSV 后，就可以按 opcode 分组、按 `hi` 聚类，找「低字相同、高字不同」的样本。注意：`nvdisasm` 的输出里函数边界不在每条指令上，跨函数比较前要先按 `Function :` 切段。

## 动手练习

1. 用 `cuobjdump -elf` 确认 `.text` 大小与指令数，算出指令字节数。
2. 用 `nvdisasm -hex` 导出 `add.cubin`，找出所有低字相同的指令对，比较高字。
3. 用 `ptxas -O0/-O3` 编同一份 PTX，diff `FADD` 的高字。
4. 把 `__launch_bounds__` 加到 kernel，看 `REG` 与高字是否变化。
5. 用 `nvdisasm -hex` 看 `LDG.E.STRONG.GPU` 与 `LDG.E.SYS` 的高字差异。

## 本章重点整理

- sm_70+ 的 SASS 指令是 128 位：低字指令/操作数，高字含调度/依赖与部分语义修饰。
- 同指令同操作数可以有不同的高字，差异来自 `ptxas` 的调度，以及 strength/scope 等修饰。
- 控制位包含 stall、yield、scoreboard 屏障；公开文档不给位域。
- cubin 是架构绑定的 ELF；SASS 跨 major 不兼容。
- Turing 的 uniform 寄存器让「warp 内相同」的值不占每线程寄存器。

## 自我检核

- [ ] 能说出 SASS 指令的字节数与低/高字分工
- [ ] 能解释「同指令不同高字」说明什么
- [ ] 知道 cubin 里 `.text` / `.nv.info` / `.nv.constant0` 的作用
- [ ] 能说出至少两个「高字不全是控制位」的理由
- [ ] 知道为什么不能照抄网上的位表

## 延伸阅读

- **[CUDA Binary Utilities](https://docs.nvidia.com/cuda/cuda-binary-utilities/)** — NVIDIA
  - 读 `cuobjdump` 与 `nvdisasm` 两章；`-hex`、`-c`、`-elf` 的官方说明。
- **[PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)** — NVIDIA
  - 读「Instruction Set」的指令修饰符，理解 PTX 与 SASS 修饰的对应关系。
- **[Dissecting the NVIDIA Turing T4 GPU via Microbenchmarking](https://arxiv.org/abs/1903.07486)** — Zhe Jia et al.（2019）
  - 用微基准拆解 Turing 的调度与缓存；做控制位实验时的参考方法论。
- **[maxas](https://github.com/NervanaSystems/maxas)** — Scott Gray
  - Maxwell 世代的 SASS 汇编器；看「控制位如何被显式编码」的早期实践（注意架构不同）。

→ [练习 E：内存序与编码对照](./practice-e-memory-and-sass.md)
