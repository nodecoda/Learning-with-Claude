# 21 重定位、代码模型与 ld.lld：从 `%hi/%lo` 到一个真正的 `jal`

> 目标：把「汇编里的符号地址」变成可验证的链接期事实。读完后你能解释 `lui`/`auipc` 为什么成对出现、small/medium/large 三种代码模型各自承诺了什么、`R_RISCV_HI20` 与 `R_RISCV_PCREL_HI20` 在什么条件下溢出，并能用 `llvm-readelf`、`llvm-objdump` 和 `ld.lld` 自己重放一次 `call foo` 到 `jal foo` 的收缩。

> 环境：LLVM 23.1.4 预编译（Optimized build，host x86_64-pc-windows-msvc），lld 23.1.4。命令在仓库根目录 `D:\Learning-with-Claude` 下通过 `compilers/riscv_backend/code/tools/run.ps1 labs/21-relocs.ps1` 执行；该脚本把 LLVM `bin` 放到 `PATH` 前面并导出 `LLVM_BIN`。本章出现的所有数字都来自本机实跑，标 `guided` 的部分需要重编译工具链。

## 为什么

编译器生成目标文件时，并不认识 `g` 最终住在哪个地址。它只知道三件事：这条指令能携带多少位、链接器允许做什么改写、以及当前代码模型承诺的地址范围。于是「取一个全局变量地址」被拆成一份跨指令的契约：一条指令送高位，另一条指令补低位，中间不允许插入会破坏配对的重排。

这份契约在三个地方同时出现，而且三处必须一致：

1. 指令选择阶段选出的机器指令序列（`lui` + `ld`、`auipc` + `ld`、或常量池间接）。
2. 汇编层写出的重定位类型（`R_RISCV_HI20`、`R_RISCV_PCREL_HI20`、`R_RISCV_GOT_HI20`）。
3. 链接器对这些重定位的解释与改写（填值、检查范围、执行 relax）。

只读汇编文本会漏掉第二层：你看到 `%hi(g)`，但看不到它最终变成哪一种重定位，也看不到链接器是否会把它换成更短的指令。反过来，只读最终可执行文件会漏掉第一层：`jal` 可能是源码里的 `call` 被 relax 之后的样子。把汇编、目标文件、可执行文件三层放在一起读，才是这一章的主线。

本章要回答四个问题：

- 为什么 `%hi` 不是「高 20 位」的字面截断，而要先加 `0x800`？
- small、medium、large 三种代码模型分别在什么地址范围内安全，越界时报什么错？
- `.rela.text` 里一条 24 字节的记录如何对应到一条指令，`r_info` 怎么拆？
- `R_RISCV_RELAX` 凭什么允许链接器把 8 字节的 `call` 改成 4 字节的 `jal`，代价是什么？

## 主线

### 21.1 两半地址：`%hi` 与 `%lo` 的进位契约

RISC-V 的 U 型指令把 20 位立即数放在 `inst[31:12]`，左移 12 位后参与运算；I 型和 S 型指令携带 12 位立即数，并按有符号数符号扩展到 64 位。因此「把一个 64 位地址装进寄存器」在指令层面只能写成两半：

```asm
lui  a0, %hi(g)          # a0 = hi20 << 12
ld   a0, %lo(g)(a0)      # a0 = *(a0 + sext(lo12))
```

两半要能拼回原地址，必须满足：

```text
(hi20 << 12) + sext(lo12) == address
```

`lo12` 是 12 位有符号数，取值范围 `[-2048, 2047]`。当地址的低 12 位大于等于 `0x800` 时，把它当作负数更划算——这样 `hi20` 进一位，`lo12` 变成负的偏移。RISC-V psABI 因此规定 `%hi` 取 `(address + 0x800) >> 12`，LLVM 的实现遵循同一约定。

本机实测可以把这个进位看清楚。把 `hi20.s` 汇编后，用 `--defsym` 把 `g` 放在 `0x12345abc`：

```powershell
llvm-mc -triple=riscv64 -filetype=obj hi20.s -o hi20.o
ld.lld --defsym=g=0x12345abc -o hi20-round.elf hi20.o
llvm-objdump -d hi20-round.elf
```

```text
0000000000011120 <_start>:
   11120: 12346537     	lui	a0, 0x12346
   11124: abc53503     	ld	a0, -0x544(a0)
   11128: 00008067     	ret
0000000012345abc A g
```

`0x12346 << 12 = 0x12346000`，加上 `-0x544` 正好回到 `0x12345abc`。如果 `%hi` 只是简单取高 20 位（`0x12345`），`%lo` 就会是 `0xabc`，符号扩展后变成 `-0x544`，两者相加得到 `0x12345abc - 0x1000 = 0x12344abc`，差了 4 KiB。这就是「加 `0x800` 再右移」存在的全部理由。

同一条地址构造，在目标文件里被记成两种低位重定位：`ld` 属于 I 型（`LO12_I`），`sd` 属于 S 型（`LO12_S`）。它们的位域不同，链接器必须分别处理。本机 `llc -filetype=obj` 对 `glob.ll`（`get_g`/`set_g`/`addr_g`）的实测输出：

```text
0000000000000000  0000000a0000001a R_RISCV_HI20       g + 0
0000000000000004  0000000a0000001b R_RISCV_LO12_I     g + 0
000000000000000c  0000000a0000001a R_RISCV_HI20       g + 0
0000000000000010  0000000a0000001c R_RISCV_LO12_S     g + 0
0000000000000018  0000000a0000001a R_RISCV_HI20       g + 0
000000000000001c  0000000a0000001b R_RISCV_LO12_I     g + 0
```

三条 `HI20` 分别属于三个函数。第三条配的是 `LO12_I`，因为 `addr_g` 用 `addi a0, a0, %lo(g)` 把偏移加进指针，而不是访存。

### 21.2 三种代码模型：绝对、PC 相对、常量池间接

代码模型回答的是「符号离引用点有多远」。LLVM 的 `llc` 只有三个取值：`small`、`medium`、`large`。Clang 驱动层的 `-mcmodel` 是另一套名字，两者对应关系如下：

| clang `-mcmodel=` | llc `-code-model=` | 寻址方式 | 安全范围 |
|---|---|---|---|
| `small`（默认） | `small` | `lui` + `%lo` 绝对地址 | 符号地址落在有符号 32 位内 |
| `medany` | `medium` | `auipc` + `%pcrel_lo` PC 相对 | 符号与引用点相距在 ±2 GiB 内 |
| `large` | `large` | 常量池放 64 位指针，再间接取地址 | 全 64 位地址空间 |

**small（绝对）**。`lui` 的 20 位加上 `lo12` 只能覆盖 `[-2^31, 2^31-1]`。可执行文件被链接到低地址时这没问题；一旦 `g` 被放到 `0x80000000`，`lui` 需要表示 `0x80000`，而 U 型立即数字段的有符号解释范围是 `[-0x80000, 0x7ffff]`，于是溢出。本机真实报错：

```text
ld.lld: error: hi20.o:(.text+0x0): relocation R_RISCV_HI20 out of range:
524288 is not in [-524288, 524287]; references 'g'
>>> defined in --defsym
```

注意报错里的 `524288` 是 `0x80000`——链接器检查的是「高位部分」是否落在 20 位有符号范围内，而不是完整地址。

**medium（PC 相对）**。`auipc` 取当前 PC 加 `hi20 << 12`，再配合 `%pcrel_lo` 补齐，覆盖的是「引用点到符号」的差值。`glob.ll` 在 `-code-model=medium` 下的实测重定位：

```text
0000000000000000  R_RISCV_PCREL_HI20     g + 0
0000000000000004  R_RISCV_PCREL_LO12_I   .Lpcrel_hi0 + 0
000000000000000c  R_RISCV_PCREL_HI20     g + 0
0000000000000010  R_RISCV_PCREL_LO12_S   .Lpcrel_hi1 + 0
0000000000000018  R_RISCV_PCREL_HI20     g + 0
000000000000001c  R_RISCV_PCREL_LO12_I   .Lpcrel_hi2 + 0
```

低位重定位指向的不是 `g`，而是同一个函数里那条 `auipc` 前面的本地标签 `.Lpcrel_hiN`。这是 PC 相对寻址的关键细节：补低位时必须知道「哪一条 `auipc` 提供了高位」，否则算不出同一个基准。汇编文本里能看到标签：

```asm
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
```

把 `g` 放到 `0x90000000`，差值超出 ±2 GiB，链接器对三个函数各报一次：

```text
ld.lld: error: glob-medium.o:(function get_g: .text+0x0): relocation
R_RISCV_PCREL_HI20 out of range: 589807 is not in [-524288, 524287]; references 'g'
>>> referenced by glob.ll
>>> defined in --defsym
```

**large**。这里没有「一条指令直接构造 64 位地址」的捷径。LLVM 在 `.text` 里为每个引用点放一个 8 字节的指针常量 `.LCPI0_0: .quad g`，代码先用 PC 相对方式取到这个常量的地址，再从常量里读出 `g` 的地址：

```asm
.LCPI0_0:
	.quad	g
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(.LCPI0_0)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ld	a0, 0(a0)
```

目标文件里对应的是 `R_RISCV_64`（64 位绝对重定位），不再有 `HI20`：

```text
0000000000000000  0000000e00000002 R_RISCV_64  g + 0
0000000000000018  0000000e00000002 R_RISCV_64  g + 0
0000000000000030  0000000e00000002 R_RISCV_64  g + 0
```

代价很直接：每个引用点多一次内存访问、多 8 字节数据。large 模型不是「更安全所以更好」，而是「地址空间换指令数」。

**选项名本身也是坑**。`llc` 没有 `-mcmodel`，也没有 `medany` 这个取值：

```text
llc.exe: for the --code-model option: Cannot find option named 'medany'!
```

```text
llc.exe: Unknown command line argument '-mcmodel=medany'.
llc.exe: Did you mean '--schedmodel=medany'?
```

`-mcmodel=medany` 只在 clang 驱动层合法。实测 `clang --target=riscv64 -mcmodel=medany -S` 生成的汇编与 `llc -code-model=medium` 同形（`auipc` + `%pcrel_lo`），`-mcmodel=large` 与 `llc -code-model=large` 同形（`.LCPI` + `auipc` + 两次 `ld`）。

### 21.3 PIC 与 `R_RISCV_GOT_HI20`：多一次解引用

`-relocation-model=pic` 不改变「两半地址」的结构，但改变两半指向谁：`auipc` 先算到 GOT 槽的 PC 相对地址，`ld` 从 GOT 槽里取出符号的真正地址，再第三次访存读写变量。

本机实测重定位：

```text
0000000000000000  0000000d00000014 R_RISCV_GOT_HI20     g + 0
0000000000000004  0000000300000018 R_RISCV_PCREL_LO12_I .Lpcrel_hi0 + 0
0000000000000010  0000000d00000014 R_RISCV_GOT_HI20     g + 0
0000000000000014  0000000600000018 R_RISCV_PCREL_LO12_I .Lpcrel_hi1 + 0
0000000000000020  0000000d00000014 R_RISCV_GOT_HI20     g + 0
0000000000000024  0000000800000018 R_RISCV_PCREL_LO12_I .Lpcrel_hi2 + 0
```

对应汇编：

```asm
.Lpcrel_hi0:
	auipc	a0, %got_pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)   # a0 = GOT[g]
	ld	a0, 0(a0)                        # a0 = *g
```

`%got_pcrel_hi` 是汇编层的写法，落到目标文件就是 `GOT_HI20`；低位仍然是 `PCREL_LO12_I`，仍然指向那条 `auipc` 的标签。small 模型下读一次全局变量只要两条指令，PIC 下要三条；如果循环里反复读同一个全局变量，寄存器分配器可能把中间那次 `ld`（GOT 槽的值）提到循环外——这条线索在第 22 章用 MIR 展开。

GOT 槽本身、`.rela.dyn` 与动态加载时的填充，属于第 22 章；本章只确认「编译器为 PIC 承诺了哪种重定位」。

### 21.4 `R_RISCV_CALL_PLT` 与 `R_RISCV_RELAX`：一条重定位的“许可单”

函数调用比取全局变量多一层：`call foo` 不是一条指令，而是 `auipc ra, %pcrel_hi(foo)` 加 `jalr ra, %pcrel_lo(...)` 两条，共 8 字节。目标文件里记的是：

```text
0x0 R_RISCV_CALL_PLT foo
0x0 R_RISCV_RELAX    -
```

两条重定位落在**同一个偏移** `0x0`。`CALL_PLT` 说的是「这里要跳到 `foo`，而且走 PLT 语义」；`RELAX` 说的是「我允许你把这个序列改短，改完记得把后面的地址都重算」。

`R_RISCV_RELAX` 不带符号，也不写目标地址，它是一张许可单。没有它，链接器只能老老实实填 `auipc` + `jalr`；有它，链接器才可以在 `foo` 落在 `jal` 的 ±1 MiB 范围内时，把 8 字节换成 4 字节的 `jal`。

本机实测（`llvm-mc -triple=riscv64 -mattr=+relax -filetype=obj` 产出 `link.o`，`ld.lld -m elf64lriscv` 链接）：

```asm
0000000000011120 <_start>:
   11120: 008000ef     	jal	0x11128 <foo>
   11124: 00008067     	ret

0000000000011128 <foo>:
   11128: 00008067     	ret
```

`jal` 的机器码 `0x008000ef` 里，`rd=ra`（`x1`），立即数是 `0x8`（从 `0x11120` 到 `0x11128`）。原来那条 `auipc` 已经消失，`.text` 从 16 字节缩到 12 字节。

这里有一个容易被忽略的后果：**relax 会改变地址**。链接器一边改写指令长度，一边维护所有符号的值和所有重定位的目标；已经算好的 `%pcrel_lo` 指向的 `auipc` 可能被删掉，后面的分支偏移也要重算。RISC-V 链接器的做法是迭代到不动点，而不是一次扫描。

### 21.5 `e_flags`：ABI 与 RVC 写进了 ELF 头

`llvm-readobj --file-headers` 在目标文件上会打印 `Flags`。同一份 `basic.ll`，两种编译选项的结果完全不同：

```text
# llc -mtriple=riscv64 -O2 -filetype=obj
Flags [ (0x0)
]

# llc -mtriple=riscv64 -mattr=+c,+d -target-abi=lp64d -O2 -filetype=obj
Flags [ (0x5)
  EF_RISCV_FLOAT_ABI_DOUBLE (0x4)
  EF_RISCV_RVC (0x1)
]
```

`0x5` 拆开就是 `0x4 | 0x1`。这两位的含义写在 psABI 里，不是 LLVM 自创：

| 位 | 名字 | 含义 |
|---|---|---|
| `0x1` | `EF_RISCV_RVC` | 目标文件里含有 16 位压缩指令 |
| `0x2` | `EF_RISCV_FLOAT_ABI_SOFT` | 软浮点 ABI |
| `0x4` | `EF_RISCV_FLOAT_ABI_DOUBLE` | 双精度硬浮点 ABI（`lp64d` / `ilp32d`） |
| `0x8` | `EF_RISCV_RVE` | 嵌入式 ABI（`ilp32e`） |
| `0x10` | `EF_RISCV_TSO` | 强制 TSO 内存模型 |

默认 `-mtriple=riscv64` 不带 `-target-abi` 时，llc 选的是 `lp64`（软浮点 ABI），所以即使你开了 `+d`，`e_flags` 仍然是 `0x0`。想让 `e_flags` 出现 `0x4`，必须显式给 `-target-abi=lp64d`，或者让驱动层根据 `-mabi` 推导。这是「特性（`+d`）」与「ABI（`lp64d`）」分离的直接证据：开了 D 扩展不代表你用硬浮点调用约定。

### 21.6 链接后的 ELF：段、符号与“重定位为空”

链接完成后，`llvm-readobj --relocations link.elf` 输出一个空列表：

```text
Relocations [
]
```

这不是工具坏了，而是**静态可执行文件的重定位已经在链接期全部消费掉**。`.rela.text` 只存在于可重定位目标文件（ET_REL）里；到了 ET_EXEC，地址已经定死，除非你生成 PIE 或动态库，才会在 `.rela.dyn` 里留下运行期重定位。

同一时刻的段与符号（真实输出，节选）：

```text
Entry: 0x11120
Name: .text (1)
Address: 0x11120
Size: 12
Name: $xrv64i2p1 (1)
Value: 0x11120
Name: _start (12)
Value: 0x11120
Name: foo (19)
Value: 0x11128
```

三个细节值得记住：

1. `Entry` 是 `0x11120`，不是 `0x0`。`ld.lld` 的 RISC-V 默认镜像基址是 `0x11000` 一带，具体值取决于链接脚本或默认布局；不要把它当成固定常量写进测试。
2. `.text` 的 `Size` 是 12，正好是 `jal`(4) + `ret`(4) + `ret`(4)。如果没开 relax，这里会是 16。
3. `$xrv64i2p1` 是**映射符号**（mapping symbol），值为 `0x11120`，类型通常是 `STT_NOTYPE`。它告诉反汇编器「从这以后按 RV64I 2.1 解码」，这样 objdump 才能在没有调试信息时也选对指令集。

### 21.7 large 模型：为什么要两次 `ld`

`large` 模型承诺整个地址空间都能寻址。它的代价是取址序列变成三段：

```asm
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(.LCPI0_0)   # a0 = 常量池条目的 PC 相对高位
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ld	a0, 0(a0)                  # a0 = 真正的 g
```

中间那次 `ld` 取的是常量池里存着的 64 位绝对地址，而不是 GOT 槽。这样做的原因很直接：`auipc` + 12 位立即数只能覆盖 ±2 GiB，想在全 64 位空间取址，就必须把完整地址放到内存里，再用一次访存读出来。

本机实测 `llc -code-model=large` 会生成 `.LCPI0_0:` 标签与 `.quad g` 条目，重定位落在 `.rela.text` 和 `.rela.data` 两处。前者填 `auipc` 的高位，后者是常量池条目本身的 64 位绝对地址。

判断该用哪种模型，看的是**代码与数据的相对距离**，不是代码大小：

| 模型 | 承诺 | 典型场景 |
|---|---|---|
| `small`（medlow） | 所有符号落在低 2 GiB，绝对地址可直接 `lui` 构造 | 裸机、内核低地址镜像 |
| `medium`（medany） | 代码与目标相距 ±2 GiB | 常规用户态程序、PIE |
| `large` | 任意 64 位地址 | 超大模块、JIT、特殊布局 |

选错模型的症状不是编译失败，而是链接期 `relocation R_RISCV_HI20 out of range` 或运行期取到错误地址。后者更难查，因为反汇编看起来“指令都对”。
### 21.8 读一条 `Rela` 记录：24 字节怎么拆

ELF64 的 `.rela.text` 每条记录 24 字节：

| 偏移 | 大小 | 字段 | 本例的值 |
|---|---|---|---|
| 0 | 8 | `r_offset` | `0x0`（要修补的指令在段内的偏移） |
| 8 | 8 | `r_info` | 高 32 位是符号索引，低 32 位是类型 |
| 16 | 8 | `r_addend` | `0x0`（RISC-V 用 RELA，加数显式存放） |

`r_info` 的低 32 位就是 `R_RISCV_HI20=22`、`R_RISCV_LO12_I=23`、`R_RISCV_PCREL_HI20=24` 这些编号。`llvm-readobj` 已经把 `r_info` 拆成了可读的名字，所以你在输出里看不到十六进制；想确认编号，用 `llvm-readobj --elf-output-style=GNU --relocations` 或查 `llvm/BinaryFormat/ELFRelocs/RISCV.def`。
## 可跑实验

### 实验 1：三种代码模型的重定位对照

目的：确认 `small` / `medium` / `large` 三种模型下，同一个 `@g` 的取址序列与重定位类型如何变化。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 labs/21-relocs.ps1
```

预期：脚本对 `small`、`medium`、`pic` 各生成一个 `.o`，并打印 `--relocations`。你应该看到：

- `small`：`R_RISCV_HI20` + `R_RISCV_LO12_I`（`lui` + `ld`）
- `medium`：`R_RISCV_PCREL_HI20` + `R_RISCV_PCREL_LO12_I`（`auipc` + `ld`）
- `pic`：`R_RISCV_GOT_HI20` + `R_RISCV_PCREL_LO12_I`（`auipc` + `ld` + `ld`）

如果三种输出一样，先检查是不是漏了 `-code-model` 或 `-relocation-model`，再看 `llc -mattr=help` 里 `+c` 之类的默认值有没有悄悄改变指令选择。

### 实验 2：亲手把 `call` 收缩成 `jal`

目的：观察 `R_RISCV_RELAX` 的作用，以及 relax 前后 `.text` 大小的变化。

```powershell
$bin = $env:LLVM_BIN
$asm = ".text`n.globl _start`n_start:`n  call foo`n  ret`nfoo:`n  ret`n"
[IO.File]::WriteAllText("$env:TEMP\relax.s", $asm, (New-Object Text.UTF8Encoding($false)))

& "$bin\llvm-mc.exe" -triple=riscv64 -mattr=+relax -filetype=obj "$env:TEMP\relax.s" -o "$env:TEMP\relax.o"
& "$bin\llvm-readobj.exe" --relocations "$env:TEMP\relax.o"
& "$bin\ld.lld.exe" -m elf64lriscv "$env:TEMP\relax.o" -o "$env:TEMP\relax.elf"
& "$bin\llvm-objdump.exe" -d "$env:TEMP\relax.elf"
```

预期：`llvm-readobj` 打印 `R_RISCV_CALL_PLT` 与 `R_RISCV_RELAX` 各一条，偏移都是 `0x0`；`llvm-objdump` 打印 `jal 0x... <foo>`，而不是 `auipc` + `jalr`。

把 `-mattr=+relax` 去掉再跑一遍，`jal` 会变回两条指令。这个对照比读十遍文档更直接。

### 实验 3：读 `e_flags`，验证 ABI 与 RVC 是两件事

目的：拆开「开 `+d`」「用 `lp64d`」「开 `+c`」三个开关对 `e_flags` 的影响。

```powershell
$bin = $env:LLVM_BIN
$src = "compilers/riscv_backend/code/labs/basic.ll"

& "$bin\llc.exe" -mtriple=riscv64 -mattr=+d -O2 -filetype=obj $src -o "$env:TEMP\a.o"
& "$bin\llvm-readobj.exe" --file-headers "$env:TEMP\a.o" | Select-String -Context 0,3 'Flags'

& "$bin\llc.exe" -mtriple=riscv64 -mattr=+d -target-abi=lp64d -O2 -filetype=obj $src -o "$env:TEMP\b.o"
& "$bin\llvm-readobj.exe" --file-headers "$env:TEMP\b.o" | Select-String -Context 0,3 'Flags'
```

预期：第一个 `Flags` 是 `0x0`，第二个是 `0x4`。再加 `-mattr=+c`，变成 `0x5`。

### 实验 4：确认链接后重定位为空

目的：区分 ET_REL 与 ET_EXEC 的重定位语义。

```powershell
$bin = $env:LLVM_BIN
& "$bin\llvm-readobj.exe" --relocations "$env:TEMP\relax.elf"
```

预期：输出 `Relocations [` 后立刻 `]`。如果你在这里看到非空列表，说明链接产物不是静态可执行文件（例如变成了共享库或 PIE）。

## 源码落点

| 位置 | 作用 |
|---|---|
| `MCTargetDesc/RISCVMCExpr.cpp` | `%hi` / `%lo` / `%pcrel_hi` / `%got_pcrel_hi` 的解析与求值 |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp` | 把 `MCExpr` 变成立即数字段，决定重定位类型 |
| `MCTargetDesc/RISCVELFObjectWriter.cpp` | 写 `r_type`，处理 `R_RISCV_RELAX` 的成对生成 |
| `MCTargetDesc/RISCVAsmBackend.cpp` | 汇编期的立即数范围检查与 fixup |
| `RISCVISelLowering.cpp` | 选择 `lui` / `auipc` / GOT 序列，取决于 code model 与 relocation model |
| `RISCVTargetObjectFile.cpp` | 常量池与全局对象布局 |
| `RISCVAsmPrinter.cpp` | 打印 `%pcrel_hi` / `%pcrel_lo` 与 `.Lpcrel_hiN` 标签 |
| `RISCVInstrInfo.td` | `AUIPC` / `LUI` / `JALR` / `JAL` 的 pattern 与编码 |

## 常见坑

1. **把 `-mcmodel` 当 llc 选项**。llc 只认 `-code-model=small|medium|large`；`medany` 是 clang 驱动层的名字。写成 `-code-model=medany` 会直接报 `Cannot find option named 'medany'`。
2. **以为 `+d` 就等于 `lp64d`**。特性决定「能不能用 D 指令」，ABI 决定「浮点参数走哪个寄存器」。两者不一致时，`e_flags` 不会撒谎：它只反映 ABI。
3. **用绝对地址思维读 `%pcrel_lo`**。`%pcrel_lo(.Lpcrel_hi0)` 里的标签指向那条 `auipc`，不是指向符号 `g`。把标签删掉或重排，低位重定位就失去锚点。
4. **在 `-O0` 下验证 relax**。`-O0` 的指令序列更长、栈帧更大，`jal` 的 ±1 MiB 范围更容易被撑破，relax 可能不触发；这不是 relax 失效。
5. **拿 main 版本的 lld 链接 23.1.4 的 `.o`**。ELF 本身兼容，但 relax 的实现细节和默认镜像基址可能不同，地址对不上时不要先怀疑教材。
6. **把 `R_RISCV_RELAX` 当成独立重定位去解析符号**。它的 `r_info` 里符号索引是 0（无符号），偏移与前一条重定位相同，解析时应当成对处理。

## 自我检查

- [ ] 能画出 `lui` + `ld`、`auipc` + `ld`、`auipc` + `ld` + `ld` 三种取全局变量序列，并说出各自的重定位类型。
- [ ] 能解释为什么 `R_RISCV_RELAX` 不带符号，却能让链接器改写指令长度。
- [ ] 能在 `llvm-readobj --file-headers` 的输出里读出 `EF_RISCV_RVC` 与 `EF_RISCV_FLOAT_ABI_DOUBLE`。
- [ ] 能解释 `%pcrel_lo(.Lpcrel_hi0)` 里的标签为什么必须指向 `auipc`。
- [ ] 能说出 ET_REL 与 ET_EXEC 在重定位上的差别，并知道链接后 `--relocations` 为空是正常的。
- [ ] 能复现 `call foo` → `jal foo` 的收缩，并说明 `.text` 从 16 字节变成 12 字节的原因。

## 延伸阅读

- RISC-V psABI 的 “Relocation” 一章：`R_RISCV_HI20` / `LO12` / `PCREL` / `GOT` / `RELAX` 的正式定义与计算式，是本课程所有重定位结论的规范来源。
- LLVM 源码 `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCExpr.cpp`：`evaluatePCRelHi` / `evaluatePCRelLo` 的实际实现，重点看 `0x800` 修正。
- `lld/ELF/Arch/RISCV.cpp`：relax 的迭代流程与 `R_RISCV_RELAX` 的配对检查，读完能解释“为什么链接器要跑多遍”。
- `llvm/test/MC/RISCV/` 下的 `relocation-*.s`：每条重定位都有最小可复现用例，适合当练习素材。

[上一章：AsmPrinter](./20-asm-printer.md) | [下一章：常量池、GOT/PLT 与 TLS](./22-constant-pool-got-tls.md)