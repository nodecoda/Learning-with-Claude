# 22 常量池、GOT/PLT 与 TLS：那些“没有立刻变成指令”的数据

> 目标：搞清楚一个常量、一个全局变量、一个函数、一个 `__thread` 变量分别怎样从源码走到最终地址。读完后你能判断 LLVM 什么时候把常量放进 `.rodata.cstN`、什么时候直接用 `li` 拼出来，能读懂 `%hi/%lo`、`%got_pcrel_hi`、`%tprel_hi`、`%tls_gd_pcrel_hi` 四套不同的地址写法，并知道 RISC-V 的 `gp` 寄存器在 LLVM 里并不承担小数据寻址。

> 环境：LLVM 23.1.4 预编译（host x86_64-pc-windows-msvc）。本章命令通过 `compilers/riscv_backend/code/tools/run.ps1` 执行，脚本会把 LLVM `bin` 放到 `PATH` 最前面。所有汇编片段均为本机 `clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S` 的真实输出。需要重编译 llc 才能验证的部分标 `guided`。

## 为什么

第 21 章解决的是“地址怎么填”。这一章解决一个更靠前的问题：**哪些东西需要地址**。

一个 `double` 常量看起来只是一条 `fmv.d.x`，但同一个 1.5 和一个 π，LLVM 会给出两套完全不同的代码：

```asm
onehalf:
	li	a0, 2047
	slli	a0, a0, 51
	fmv.d.x	fa0, a0
	ret

pi:
	lui	a0, %hi(.LCPI0_0)
	fld	fa0, %lo(.LCPI0_0)(a0)
	ret
```

1.5 的 IEEE754 位模式是 `0x3FF0000000000000`，正好是 `2047 << 51`，两条整数指令就能拼出来；π 的位模式是 `0x400921fb54442d18`，没有这种捷径，于是 LLVM 把它放进 `.rodata.cst8`，用 `lui` + `fld` 从常量池读。

这背后是一个成本模型，不是一个固定规则。理解这个模型，你才能解释“为什么我加了一个常量，汇编里突然多出一个 `.LCPI`”，也才能判断某个性能回归是不是常量池布局变化引起的。

同样的分层出现在四个地方：

| 对象 | 需要什么 | 用哪套写法 |
|---|---|---|
| 立即数常量 | 位模式，不需要地址 | `li` / `lui`+`addi` / 常量池 |
| 全局变量 | 数据地址 | `%hi/%lo`、`%pcrel_hi/%pcrel_lo`、`%got_pcrel_hi` |
| 外部函数 | 代码地址 | `call`（`CALL_PLT`）、`tail` |
| `__thread` 变量 | TLS 偏移 + `tp` | `%tprel_*`、`%tls_ie_pcrel_hi`、`%tls_gd_pcrel_hi` |

四套写法不能混用，混用的后果通常不是编译错误，而是链接期重定位找不到、或者运行期取到错误地址。

## 主线

### 22.1 常量池的触发条件：位模式的“构造成本”

LLVM 的 RISC-V 后端在 `RISCVISelLowering.cpp` 里为常量选择物化策略：能用一个 `lui` + `addi`（或 `li` 伪指令）便宜构造的，直接构造；构造成本超过从内存加载的，就走常量池。

本机三个真实例子：

```asm
# 1.5：两条整数指令
	li	a0, 2047
	slli	a0, a0, 51
	fmv.d.x	fa0, a0

# π：常量池 + fld
.LCPI0_0:
	.quad	0x400921fb54442d18              # double 3.1415926535897931
	...
	lui	a0, %hi(.LCPI0_0)
	fld	fa0, %lo(.LCPI0_0)(a0)

# 0x123456789abcdef0：常量池 + ld
.LCPI1_0:
	.quad	1311768467463790320             # 0x123456789abcdef0
	...
	lui	a0, %hi(.LCPI1_0)
	ld	a0, %lo(.LCPI1_0)(a0)
```

判断依据可以粗略写成：如果构造需要超过 2 条指令（`lui` + `addi` 算两条），并且加载仅需要 `lui` + `ld`/`fld` 两条，那么常量池更划算。注意 `fld` 仍然要一次数据访存，所以“常量池更快”从来不成立；它省的是**指令字节数**，代价是一次 load。

### 22.2 `.LCPI` 标签与 `.rodata.cstN` 段

常量池条目不是随便塞在 `.text` 里。真实的段声明是：

```asm
	.section	.rodata.cst8,"aM",@progbits,8
	.p2align	3, 0x0
.LCPI1_0:
	.quad	1311768467463790320
```

三个细节：

1. `cst8` 的 `8` 是条目大小（字节）。同一条常量如果在多个函数里出现，LLVM 可能合并；大小不同的常量会落在 `cst4` / `cst8` / `cst16` 等不同段。
2. `"aM"` 里的 `M` 表示可合并（mergeable），链接器可以把相同内容的常量池条目合并成一份。
3. `.p2align 3` 保证 8 字节对齐。`fld` 不要求对齐，但 `ld` 在某些平台上对非对齐访问有额外代价，LLVM 选择保守对齐。

`RISCVTargetObjectFile.cpp` 决定这些条目放在哪个段、用多大对齐；`RISCVConstantPoolValue.cpp` 负责把“一个需要重定位的符号 + 加数”表示成常量池值，供后续 `%hi/%lo` 拆分使用。

### 22.3 `%hi` / `%lo`：为什么不是简单的高 20 位

取全局变量地址的最短序列是：

```asm
addr:
	lui	a0, %hi(g)
	addi	a0, a0, %lo(g)
	ret
```

`lui` 把 20 位立即数放到目标寄存器的高 20 位，低 12 位补 0。`addi` 的立即数是 **有符号** 12 位，范围 `[-2048, 2047]`。如果 `g` 的地址低 12 位是 `0x800` 以上，直接截断会得到负数，必须先把高 20 位加一。

所以 `%hi` 的真实语义是 `(addr + 0x800) >> 12`，`%lo` 是 `addr - (%hi << 12)`。这个 `0x800` 修正写在 `RISCVMCExpr.cpp` 的 `evaluatePCRelHi` / `evaluateNonPCRelHi` 里，不在汇编器文本里。

这也是为什么 `%hi` 与 `%lo` 必须成对出现：单独一条 `lui %hi(g)` 得到的不是 `g` 的地址，而是“对齐到 4 KiB 后可能差一个 4 KiB”的近似值。

### 22.4 PIC：GOT 让取址多一次解引用

`-relocation-model=pic` 时，编译器不能假设 `g` 在链接期有固定地址（共享库可能被加载到任意位置）。于是它改成：

```asm
.Lpcrel_hi0:
	auipc	a0, %got_pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)   # a0 = GOT[g]
	ld	a0, 0(a0)                        # a0 = *g
```

第一步算出 GOT 槽的 PC 相对地址，第二步从 GOT 槽取出 `g` 的真实地址，第三步才访存。对应重定位是 `R_RISCV_GOT_HI20` + `R_RISCV_PCREL_LO12_I`（第 21 章有完整输出）。

GOT 槽本身由链接器生成，落在 `.got` 段，运行时由动态加载器填。对同一个全局变量反复读取，LLVM 可以把中间那次 `ld`（GOT 槽的值）提到循环外，但第三次访存省不掉。

### 22.5 PLT 与 `call`：为什么默认走 PLT

RISC-V 的 `call` 伪指令展开成 `auipc ra, %pcrel_hi(f)` + `jalr ra, %pcrel_lo(...)(ra)`。在 PIC 下，如果 `f` 是外部函数，链接器需要经过 PLT 才能跳过去，因此重定位类型是 `R_RISCV_CALL_PLT` 而不是 `R_RISCV_CALL`。

本机实测（第 21 章的 `link.o`）：

```text
0x0 R_RISCV_CALL_PLT foo
0x0 R_RISCV_RELAX    -
```

两个后果：

- 同一条 `call`，静态链接时可能被 relax 成 `jal`（第 21 章实测），动态链接时通常保留 `auipc` + `jalr`，由 PLT 完成间接跳转。
- `-fno-plt` 之类的优化会改变重定位类型，但不会改变“取地址 + 间接跳转”的基本结构。

### 22.6 TLS 三种模型：同一行 C 代码，三套指令

`__thread int tls_g;` 在三种 TLS 模型下的真实输出：

**local-exec（默认，非 PIC）**

```asm
tls_get:
	lui	a0, %tprel_hi(tls_g)
	add	a0, a0, tp, %tprel_add(tls_g)
	lw	a0, %tprel_lo(tls_g)(a0)
	ret
```

`tp` 是线程指针寄存器。`lui` 构造 TLS 块内偏移的高位，`add` 把 `tp` 加上去，`lw` 用低 12 位偏移访存。这里 `add` 有四个操作数，第四个 `%tprel_add(...)` 是给链接器 relax 用的标记：如果偏移足够小，链接器可以把三条指令收缩成更短的形式。

**initial-exec（`-fPIC -ftls-model=initial-exec`）**

```asm
.Lpcrel_hi0:
	auipc	a0, %tls_ie_pcrel_hi(tls_g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)   # a0 = GOT 里的 TLS 偏移
	add	a0, a0, tp
	lw	a0, 0(a0)
	ret
```

TLS 偏移放在 GOT 里，运行时仅需要一次额外 load，不需要调用 `__tls_get_addr`。适合主程序或启动时就加载的共享库。

**general-dynamic（`-fPIC` 默认）**

```asm
	auipc	a0, %tls_gd_pcrel_hi(tls_g)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)
	call	__tls_get_addr
```

把 TLS 描述符的地址传给 `__tls_get_addr`，由运行时算出当前线程的变量地址。最通用，也最慢：一次函数调用。

三种模型的取舍不是 LLVM 决定的，而是「这个模块会不会被 `dlopen`」「是不是主可执行文件」这些部署问题决定的。后端只负责把选定的模型翻译成正确的指令序列。

### 22.7 `.sbss` / `.sdata` 与 `gp`：LLVM 不做 gp 相对寻址

加上 `-msmall-data-limit=8` 后，`int g;` 会落到 `.sbss`：

```asm
	.section	.sbss,"aw",@nobits
	.globl	g
g:
	.word	0
```

但访问它的代码仍然是：

```asm
get:
	lui	a0, %hi(g)
	lw	a0, %lo(g)(a0)
	ret
```

也就是说，LLVM 的 RISC-V 后端**没有**把 `.sdata`/`.sbss` 里的对象改成 `gp` 相对寻址。`gp` 相对寻址在 GNU 工具链里依赖链接器 relaxation（把 `%hi/%lo` 改写成 `gp` 相对），LLVM 的实现选择不生成这类重定位，因此小数据段只影响布局，不影响指令序列。

如果你从 GNU 汇编迁移过来，期待看到 `lw a0, %gottprel_g(gp)` 之类的写法，在这里会落空。

## 可跑实验

### 实验 1：观察常量池的触发阈值

目的：找到“什么时候 LLVM 决定用常量池”的经验边界。

```powershell
$bin = $env:LLVM_BIN
$lines = @(
  'double a(void) { return 1.5; }'
  'double b(void) { return 3.141592653589793; }'
  'long   c(void) { return 0x123456789abcdef0L; }'
  'long   d(void) { return 0x12345L; }'
)
[IO.File]::WriteAllText("$env:TEMP\cp.c", ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S "$env:TEMP\cp.c" -o -
```

预期：`a` 与 `d` 直接物化（`li`/`lui`+`addi`），`b` 与 `c` 走 `.LCPI`。把 `d` 的常量改成 `0x123456789abcdef0`，它会立刻变成常量池。

### 实验 2：PIC 下的 GOT 三连

目的：确认 `-fPIC` 让取全局变量从两条指令变成三条。

```powershell
$bin = $env:LLVM_BIN
$c = 'int g; int get(void){ return g; } void set(int v){ g = v; }'
[IO.File]::WriteAllText("$env:TEMP\got.c", $c, (New-Object Text.UTF8Encoding($false)))
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S "$env:TEMP\got.c" -o -
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -fPIC -S "$env:TEMP\got.c" -o -
```

预期：非 PIC 是 `lui` + `lw`；PIC 是 `auipc` + `ld` + `lw`，且 `auipc` 的操作数是 `%got_pcrel_hi(g)`。

### 实验 3：三种 TLS 模型并排比较

目的：把 `__thread` 的三套指令序列放在一起看。

```powershell
$bin = $env:LLVM_BIN
$c = '__thread int tls_g; int get(void){ return tls_g; }'
[IO.File]::WriteAllText("$env:TEMP\tls.c", $c, (New-Object Text.UTF8Encoding($false)))

& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S "$env:TEMP\tls.c" -o -
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -fPIC -ftls-model=initial-exec -S "$env:TEMP\tls.c" -o -
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -fPIC -S "$env:TEMP\tls.c" -o -
```

预期：依次看到 `%tprel_hi` 三连、`%tls_ie_pcrel_hi` + `add tp`、`%tls_gd_pcrel_hi` + `call __tls_get_addr`。

### 实验 4：确认小数据段不改变寻址方式

目的：验证 `.sbss` 与 `gp` 相对寻址在 LLVM 里是两件事。

```powershell
$bin = $env:LLVM_BIN
$c = 'int g; int get(void){ return g; }'
[IO.File]::WriteAllText("$env:TEMP\sd.c", $c, (New-Object Text.UTF8Encoding($false)))
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -msmall-data-limit=8 -S "$env:TEMP\sd.c" -o -
```

预期：`g` 落在 `.sbss`，但 `get` 仍是 `lui` + `lw`。

## 源码落点

| 位置 | 作用 |
|---|---|
| `RISCVConstantPoolValue.cpp/.h` | 需要重定位的常量池值（符号 + 加数）的表示 |
| `RISCVTargetObjectFile.cpp/.h` | 常量池段落选择、对齐、small data 段 |
| `RISCVISelLowering.cpp` | `LowerConstant` / 常量物化策略、全局地址选择 |
| `RISCVPromoteConstant.cpp` | 把可提升的常量从内存提到寄存器 |
| `RISCVAsmPrinter.cpp` | 打印 `.LCPI` 标签与 `%hi/%lo` 操作数 |
| `RISCVMCExpr.cpp` | `%hi` / `%lo` / `%got_pcrel_hi` / `%tprel_hi` 的求值 |
| `RISCVInstrInfo.td` | `LUI` / `AUIPC` / `ADDI` / `LD` / `FLD` 的 pattern |
| `RISCVTargetMachine.cpp` | 根据 relocation model 选择全局地址 lowering 路径 |

### 22.8 一个常量池条目的完整生命周期

把 π 的例子从 IR 追到 ELF，可以看清四层各自的职责：

1. **IR 层**：`ret double 0x400921FB54442D18`，常量是 `ConstantFP`，还没有地址。
2. **SelectionDAG 层**：`ConstantFP` 变成 `ISD::ConstantFP`。`RISCVTargetLowering::LowerOperation` 判断构造成本，决定走常量池，生成一个目标常量池节点。
3. **机器指令层**：出现 `PseudoLI`（物化地址）与 `FLD`，常量池条目记在 `MachineConstantPool` 里，此时只有序号，没有最终偏移。
4. **汇编/目标文件层**：`RISCVAsmPrinter` 分配 `.LCPI0_0` 标签，输出 `.section .rodata.cst8` 与 `.quad`，指令里写 `%hi(.LCPI0_0)`；汇编器再把它变成 `R_RISCV_HI20` 重定位。

四个阶段的“常量”形态完全不同：位模式 → DAG 节点 → 机器常量池索引 → 带重定位的符号。任何一层判断错，最终看到的现象都是“地址不对”或“值不对”，但根因在完全不同的地方。

用 `llc -stop-after=finalize-isel` 能在第 3 层停下来，看到 `PseudoLI` 与常量池引用；用 `llvm-readobj --relocations` 能在第 4 层看到 `R_RISCV_HI20` 指向 `.LCPI0_0`。把两份输出并排放，是排查常量池问题最快的方法。

### 22.9 失败案例：TLS 与链接模型的错配

`__thread` 变量在两种情况下会直接失败，而且报错信息不在 C 源码层面：

**情况一：把 general-dynamic 的目标文件静态链接进一个没有 TLS 运行时的镜像。**

链接器会报 `undefined symbol: __tls_get_addr`。原因是 general-dynamic 模型假定运行时提供 `__tls_get_addr`，而裸机镜像或 `-nostdlib` 链接没有这个符号。解决办法不是“加个空实现”，而是换 TLS 模型或干脆不用 TLS。

**情况二：在共享库里用 local-exec。**

`local-exec` 假定变量与代码在同一个模块、且模块加载地址固定。把这样的目标文件打进 `.so`，动态加载器无法为每个线程算出正确偏移，链接期通常报 `relocation R_RISCV_TPREL_HI20 against ... can not be used when making a shared object`。这类错误在编译单个 `.c` 时不会出现，只在链接共享库时暴露。

两个案例的共同点：**TLS 模型是链接期契约，不是单文件属性**。后端只能按你给的模型生成指令，无法在链接期替你改主意。
## 常见坑

1. **以为 1.5 也走常量池**。`0x3FF0000000000000 = 2047 << 51`，两条整数指令就能构造，比一次 `fld` 更省。常量池不是“浮点常量默认路径”。
2. **把 `%hi` 当成简单右移 12 位**。少了 `+0x800` 修正，低 12 位为负时地址会偏 4 KiB，且只在特定地址上出错。
3. **在 PIC 下数错指令条数**。取全局变量是三条（`auipc`、`ld`、`lw`），不是两条；漏掉的那条是 GOT 解引用。
4. **以为 `-msmall-data-limit` 会生成 `gp` 相对寻址**。LLVM 只改段布局，不生成 `gp` 相对重定位。
5. **把 `__tls_get_addr` 的调用当成普通函数调用**。它是运行时约定的 TLS 解析入口，参数是 TLS 描述符地址，返回值是当前线程的变量地址；ABI 由 psABI 的 TLS 章节规定。
6. **混淆 `R_RISCV_CALL` 与 `R_RISCV_CALL_PLT`**。前者用于本地已知符号，后者用于可能经过 PLT 的外部符号；链接器对两者的 relax 策略不同。

## 自我检查

- [ ] 能说出常量池触发的经验阈值，并用 1.5 / π / `0x123456789abcdef0` 三个例子解释。
- [ ] 能写出 `.rodata.cst8` 的段声明，并解释 `"aM"` 里 `M` 的含义。
- [ ] 能解释 `%hi` 为什么需要 `+0x800` 修正。
- [ ] 能画出 PIC 下取全局变量的三条指令，并指出哪一条读 GOT。
- [ ] 能默写 local-exec、initial-exec、general-dynamic 三种 TLS 序列的关键指令。
- [ ] 能解释为什么 `-msmall-data-limit` 不会改变 `lui` + `lw` 的寻址方式。

## 延伸阅读

- RISC-V psABI 的 “TLS” 与 “Code Model” 两节：三种 TLS 模型的正式定义，以及 `tp` 的约定。
- LLVM 源码 `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` 的全局地址与常量池 lowering 路径。
- `llvm/lib/Target/RISCV/RISCVTargetObjectFile.cpp`：`.rodata.cstN` 与 `.sdata`/`.sbss` 的选择逻辑。
- `llvm/test/CodeGen/RISCV/` 下的 `global-address.ll`、`tls.ll`、`constant-pool.ll`：每个结论都有最小 lit 用例。

[上一章：重定位、代码模型与 ld.lld](./21-relocations-and-linking.md) | [下一章：RVV 向量后端与新增扩展](./23-vector-and-custom-extension.md)