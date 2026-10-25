# 00 环境与基线：把 23.1.4 钉在证据上

> 目标：把本课程的运行环境变成一组可以复现的事实。读完本章，你能回答四个问题：本机到底是哪个 LLVM；`llc` 认识哪些 RISC-V 目标；一条最小函数会产出什么；`.attribute 5` 里的 ISA 字符串每一个字段从哪里来。更重要的是，你会知道哪些结论可以引用实测，哪些只能标 `guided`。

> 环境：Windows 10 x86_64；PowerShell 5.1；LLVM 23.1.4 预编译版（Optimized build，host `x86_64-pc-windows-msvc`）。所有命令都在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。

## 为什么

后端课程最容易制造一种“看起来能跑”的假象：命令有输出，汇编也能读，但版本、目标、特性、源码树四者没有对齐。等到某一行汇编和源码对不上，你无法判断是读错了版本、开错了特性，还是把另一棵源码树的行号搬了过来。

本章先把四个基线钉死：

1. **工具版本**：`llc`、`llvm-mc`、`llvm-tblgen` 都来自同一个 23.1.4 预编译包，而不是 PATH 里某个旧版 LLVM。
2. **目标注册**：本机注册了 `riscv32`、`riscv32be`、`riscv64`、`riscv64be` 四个目标。没有注册的目标，后面的 `-mtriple` 实验根本无从谈起。
3. **源码版本**：同版本 RISC-V 源码在 `.scratch/llvm-23.1.4-riscv-td`，`git describe` 是 `llvmorg-23.1.4`。`D:\llvm-project` 是 main / 24-dev，只能用于读结构，不能用于引用行号。
4. **构建能力**：D 盘剩余不足 5 GiB（实测时 4.88 GiB），无法容纳一次 `llc` 构建。于是“改 `.td` 后重新生成并重编译”只能标 `guided`，不能伪装成实测。

这四个基线决定了整门课的写作纪律：每个论断要么给命令和输出，要么给同版本源码的文件名与符号名，要么明确标 `guided`。三者不能混用。

## 主线

### 0.1 版本、构建类型与注册目标

先看 `llc --version` 的真实前几行：

```text
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
  Default target: x86_64-pc-windows-msvc
  Host CPU: skylake
```

`Optimized build.` 这一行有实际后果：release 构建通常没有 `-debug-only` 这类断言/调试开关，后面的 MIR 实验要靠 `-stop-after`、`-run-pass`、`-print-after`，而不是靠 `DEBUG_TYPE` 日志。

再看注册目标。`llc --version` 的目标列表里，RISC-V 部分真实如下：

```text
    riscv32    - 32-bit RISC-V
    riscv32be  - 32-bit big endian RISC-V
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
```

这四个名字不是装饰。它们分别对应 `RISCVTargetMachine.cpp` 里 `LLVMInitializeRISCVTarget()` 的四个 `RegisterTargetMachine<RISCVTargetMachine>` 调用，分别绑定 `getTheRISCV32Target()`、`getTheRISCV64Target()`、`getTheRISCV32beTarget()`、`getTheRISCV64beTarget()`。后面的 `-mtriple=riscv64` 之所以能工作，是因为这里先完成了注册。

工具是否齐全也要检查。本机确认存在的工具有：

```text
llc            True
llvm-mc        True
llvm-objdump   True
llvm-readobj   True
llvm-tblgen    True
llvm-mca       True
ld.lld         True
```

`llvm-lit` 不在预编译包的常用路径里，所以本课程不依赖 lit 测试套件。需要验证 TableGen 时直接调用 `llvm-tblgen`；需要验证汇编/编码时直接调用 `llvm-mc`；需要验证链接时直接调用 `ld.lld`。

### 0.2 run.ps1 的职责

统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。它做四件事：

1. 根据脚本位置反推出仓库根目录，拼出 LLVM 23.1.4 的 `bin` 目录。
2. 把这个 `bin` 放到 `PATH` 最前面，保证 `llc`、`llvm-mc` 等命令解析到 23.1.4，而不是系统里的其他 LLVM。
3. 设置 `LLVM_BIN`、`LLVM_SRC_TD`、`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。实验脚本因此可以直接使用 `$env:LLVM_BIN` 和 `$env:LLVM_SRC_TD`。
4. 如果参数以 `.ps1` 结尾，就交给 `powershell -NoProfile -ExecutionPolicy Bypass -File`；如果以 `.py` 结尾，就交给 `python`；否则直接执行。

最小用法：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 `
  labs/00-version.ps1
```

注意脚本参数是相对 `compilers/riscv_backend/code/` 的路径，不是相对仓库根目录。`labs/00-version.ps1` 会被解析成 `code/labs/00-version.ps1`。

真实输出的开头是：

```text
[run] LLVM (http://llvm.org/): |   LLVM version 23.1.4
== llc --version ==
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
  Default target: x86_64-pc-windows-msvc
  Host CPU: skylake
```

这行 `[run]` 是入口脚本打印的版本指纹。看到别的版本号，后面的实验全部作废，先修环境。

### 0.3 第一条函数：从 .ll 到 asm

`00-version.ps1` 会写一个最小 `.ll`：

```llvm
target triple = "riscv64-unknown-elf"
define i64 @mul_add(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %r = add i64 %m, 7
  ret i64 %r
}
```

然后执行：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 first.ll -o -
```

真实的汇编开头如下：

```asm
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
	.file	"riscv-ch00.ll"
	.text
	.globl	mul_add                         # -- Begin function mul_add
	.p2align	2
	.type	mul_add,@function
mul_add:                                # @mul_add
	.cfi_startproc
# %bb.0:
	mul	a0, a0, a1
	addi	a0, a0, 7
	ret
.Lfunc_end0:
	.size	mul_add, .Lfunc_end0-mul_add
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

逐行读出四类信息：

- `.attribute 4, 16`：栈对齐 16 字节。这是 RISC-V psABI 的栈对齐要求，不是这条函数自己声明的。
- `.attribute 5, "rv64i2p1_m2p0_zmmul1p0"`：文件级 ISA 属性。它来自模块级 `MCSubtargetInfo`，不是某个函数的局部特性。0.4 节会拆开读。
- `.cfi_startproc` / `.cfi_endproc`：CFI 伪指令，供调试器和异常展开使用。它们不是机器指令，不会进入 `.text` 的编码。
- `.section ".note.GNU-stack","",@progbits`：标记栈不可执行。这也是链接器输入的一部分。

`mul` 能出现，是因为命令开了 `+m`。把 `-mattr` 去掉，同一条函数会变成 libcall：

```asm
mul_add:
	.cfi_startproc
# %bb.0:
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	call	__muldi3
	addi	a0, a0, 7
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
```

这段输出把“特性开关会影响选指与 ABI”两件事同时暴露出来：没有 `+m` 就没有 `mul`，而且调用 `__muldi3` 需要保存 `ra`，于是出现了 16 字节栈帧。

### 0.4 `.attribute 5` 的 ISA 字符串怎么读

`.attribute 5` 是 RISC-V ELF 的 `Tag_RISCV_arch`。字符串的语法是“基础 ISA + 若干下划线分隔的扩展”，每个扩展带主版本和小版本。实测四组 `-mattr`：

```text
-mattr 缺省
  .attribute 5, "rv64i2p1"

-mattr=+m
  .attribute 5, "rv64i2p1_m2p0_zmmul1p0"

-mattr=+m,+a,+f,+d
  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0"

-mattr=+m,+a,+f,+d,+c
  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
```

拆开读：

| 片段 | 含义 | 为什么会出现 |
| --- | --- | --- |
| `rv64i2p1` | RV64 基础整数 ISA，I 扩展 2.1 | triple 是 `riscv64`，基础 ISA 固定 |
| `m2p0` | M 扩展 2.0 | `+m` 显式打开 |
| `a2p1` | A 扩展 2.1 | `+a` 显式打开 |
| `f2p2` / `d2p2` | F/D 扩展 2.2 | `+f,+d` 显式打开 |
| `c2p0` | C 扩展 2.0 | `+c` 显式打开 |
| `zicsr2p0` | Zicsr 2.0 | F 扩展隐含依赖 |
| `zmmul1p0` | Zmmul 1.0（只乘不除） | M 扩展隐含依赖 |
| `zaamo1p0` / `zalrsc1p0` | 原子内存操作 / LR-SC | A 扩展拆出的子扩展 |
| `zca1p0` / `zcd1p0` | 压缩基础指令 / 压缩双精度访存 | C 与 D 组合后拆出的子扩展 |

这里有三个容易读错的地方。

第一，**版本号不是 LLVM 版本**。`i2p1` 是 ISA 扩展版本，和 `23.1.4` 无关。

第二，**字符串会展开隐含依赖**。你只写了 `+m`，输出却多出 `zmmul1p0`；你只写了 `+a`，输出却多出 `zaamo1p0` 和 `zalrsc1p0`。这不是重复，而是 ISA 规范化后的完整集合。

第三，**字符串是文件级的**。`RISCVAsmPrinter::emitAttributes` 取的是 `TM.getMCSubtargetInfo()`，源码注释明确写了：不同函数可能有不同属性，但输出端无法判断哪个函数才是“正确”的文件级代表。于是 `.attribute 5` 反映的是模块/命令行的子目标，不是每个函数的局部 `target-features`。

这一点可以用对象文件确认：

```powershell
llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -filetype=obj first.ll -o first.o
llvm-readobj --arch-specific first.o
```

真实输出节选：

```text
Format: elf64-littleriscv
Arch: riscv64
BuildAttributes {
  FormatVersion: 0x41
  Section 1 {
    SectionLength: 103
    Vendor: riscv
    Tag: Tag_File (0x1)
    Size: 93
    FileAttributes {
      Attribute {
        Tag: 4
        Value: 16
        TagName: stack_align
        Description: Stack alignment is 16-bytes
      }
      Attribute {
        Tag: 5
        TagName: arch
        Value: rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0
      }
    }
  }
}
```

`Tag: 5` 和汇编里的 `.attribute 5` 是同一个东西。`llvm-readobj` 只是把数值标签翻译成了 `TagName: arch`。

### 0.5 为什么 D:\llvm-project 不能当 23.1.4 用

两棵源码树的真实版本指纹：

```text
.scratch/llvm-23.1.4-riscv-td
  git describe --tags --always
  llvmorg-23.1.4

D:\llvm-project
  git describe --tags --always
  523ce86a00bd
  git log -1 --format=%cs
  2026-10-18
```

`llvmorg-23.1.4` 是带标签的发布版本；`523ce86a00bd` 没有标签，提交日期是 2026-10-18，属于 main / 24-dev 一线。两者相差一个开发周期。

版本不一致会带来三类错误：

1. **行号漂移**。同一个 `def ADD` 在 23.1.4 的 `RISCVInstrInfo.td` 第 818 行；在 main 上可能已经插入或删除了若干行。把 main 的行号写进课程，读者用 23.1.4 对照就会对不上。
2. **符号改名**。pass 名、类名、字段名会随重构变化。23.1.4 的 `riscv-prera-expand-pseudo`、`riscv-expand-pseudo` 在别的版本可能拆分或合并。
3. **生成物编号漂移**。TableGen 分配的 opcode 编号依赖记录顺序与内容。用 main 的 `RISCVGenInstrInfo.inc` 去解释 23.1.4 的 `ADD = 13725`，结论没有意义。

因此本课程的源码锚点只用两种写法：

- 同版本文件 + 符号名，例如 `RISCVInstrInfo.td` 的 `def ADD`。
- 同版本文件 + 明确的行号，例如 `RISCVInstrInfo.td:818`。行号只在 23.1.4 树内使用。

`D:\llvm-project` 可以用来读跨版本的总体结构，例如某个类大致负责什么；不能用来引用行号，也不能用来推断 23.1.4 的生成物内容。

### 0.6 证据约定：实测、源码依据、guided

本课程每个技术论断都落在三类证据之一。

**实测**：本机 23.1.4 的真实命令与输出。写法是“命令 + 输出 + 解读”。例如：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 first.ll -o -
```

输出里出现 `mul a0, a0, a1`，于是可以写“开启 M 后，i64 乘法选成 `mul`”。这是实测。

**源码依据**：对 23.1.4 源码的描述，给文件名和符号名。例如“`RISCVTargetMachine.cpp` 的 `LLVMInitializeRISCVTarget()` 注册四个目标”。这类结论不要求本机重新编译，但要求源码树版本正确。

**guided**：需要重新构建 `llc` 才能验证的结论。典型场景是修改 `.td` 后新增一条指令，或者改 `RISCVISelLowering.cpp` 后观察新的选指结果。写法必须包含：改动文件、改动思路、构建命令、预期现象、验证命令。不能写“我编译后看到……”，因为没有构建。

判断标准很直接：如果一条结论依赖“重新生成并重编译 LLVM 才会出现的新行为”，它就是 `guided`；如果它只是读现有源码的结构，它就是源码依据；如果它能在本机预编译工具上重跑，它就是实测。

### 0.7 磁盘限制与不能构建 llc 的后果

本机 D 盘真实剩余空间（2026-10-25 实测，这个数字会随磁盘使用变化）：

```text
FreeBytes=5242949632
FreeGiB=4.88
```

一次带 RISC-V 目标的 `llc` 构建，即使只开一个目标、Release、无断言，通常也要 20-30 GB 的构建目录，外加 1-2 小时。不足 5 GiB 的剩余空间连配置阶段的对象文件都放不下。

后果有四条：

1. **不能验证新指令的端到端行为**。改 `RISCVInstrInfo.td` 新增一条指令后，`llvm-tblgen` 可以重新生成 `.inc`，但预编译 `llc` 不会加载新表。选指、汇编、反汇编都不会认识新指令。
2. **不能验证 lowering 改动**。改 `RISCVISelLowering.cpp` 后需要重编译 `LLVMRISCVCodeGen`，本机做不到。
3. **可以验证 TableGen 语法与生成物**。`llvm-tblgen -gen-instr-info`、`-gen-asm-matcher`、`-gen-emitter` 等可以在现有头文件和 `.td` 上离线跑，产物能看到记录编号、匹配表、编码函数。这部分是实测。
4. **可以验证现有二进制的行为**。`llc`、`llvm-mc`、`llvm-objdump`、`llvm-readobj`、`ld.lld`、`llvm-mca` 都能跑。只要不改 LLVM 本体，实验就是实测。

所以本课程的分工是：**读源码建立假设，用现有工具验证可验证的部分，把必须重编译的部分明确标 guided**。这条纪律比任何单条汇编都重要。
## 可跑实验

### 实验 1：一条命令确认基线

目的：确认版本、目标注册、工具齐全、首函数输出四件事。

命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 `
  labs/00-version.ps1
```

预期输出包含：

```text
LLVM version 23.1.4
Optimized build.
riscv32    - 32-bit RISC-V
riscv32be  - 32-bit big endian RISC-V
riscv64    - 64-bit RISC-V
riscv64be  - 64-bit big endian RISC-V
llc            True
llvm-mc        True
llvm-tblgen    True
```

以及首函数的 `mul` / `addi` / `ret`。

解读：如果 `[run]` 行不是 23.1.4，先检查 `run.ps1` 拼出的 `$llvmBin` 是否存在；如果 RISC-V 目标缺失，说明这个预编译包没有启用 RISC-V，后面的所有实验都不能做。

失败排查：

- `LLVM 23.1.4 not found at: ...`：目录名或层级不对，检查 `.scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin/llc.exe`。
- `Script not found: ...`：`labs/00-version.ps1` 是相对 `code/` 的路径，不要写成相对仓库根目录。
- 输出里出现别的 LLVM 版本：`PATH` 被外部环境污染，确认入口脚本先执行了 `$env:PATH = "$llvmBin;$env:PATH"`。

### 实验 2：观察 ISA 字符串随特性变化

目的：把 `-mattr` 与 `.attribute 5` 的对应关系跑出来。

命令：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$ll  = Join-Path $env:TEMP "attr.ll"
$lines = @(
  'target triple = "riscv64-unknown-elf"'
  'define i64 @f(i64 %a, i64 %b) {'
  '  %m = mul i64 %a, %b'
  '  ret i64 %m'
  '}'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))

foreach ($m in @("", "+m", "+m,+a,+f,+d", "+m,+a,+f,+d,+c")) {
  "=== $m ==="
  if ($m) {
    & "$bin\llc.exe" -mtriple=riscv64 "-mattr=$m" -O2 $ll -o - |
      Select-String -Pattern '\.attribute\s+5'
  } else {
    & "$bin\llc.exe" -mtriple=riscv64 -O2 $ll -o - |
      Select-String -Pattern '\.attribute\s+5'
  }
}
```

预期输出：

```text
===  ===
	.attribute	5, "rv64i2p1"
=== +m ===
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
=== +m,+a,+f,+d ===
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0"
=== +m,+a,+f,+d,+c ===
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
```

解读：每多一个扩展，字符串多出该扩展及其隐含依赖。`+c` 同时带出 `zca1p0` 和 `zcd1p0`，因为 D 扩展存在时压缩双精度访存属于 `Zcd`。

失败排查：

- PowerShell 把 `-mattr=+m,+a` 里的逗号当成参数分隔符时，输出会缺失或报参数错误。把整个开关写成字符串 `"-mattr=$m"`，或者写成 `'-mattr=+m,+a'`。
- 字符串仍是 `rv64i2p1`：检查 `.ll` 里是否误加了函数级 `target-features`，以及函数是否真的引用了那个属性组。本课程的 fixture 里有一个属性组没有被任何函数引用，这类细节会让“我明明写了 +m”失效。

### 实验 3（边界）：未知特性只警告，不报错

目的：确认 `-mattr` 的失败模式。很多人以为写错特性会直接失败，实际不是。

命令：

```powershell
llc -mtriple=riscv64 "-mattr=+notafeature" -O2 first.ll -o -
```

真实输出（节选）：

```text
	.attribute	5, "rv64i2p1"
	.file	"first.ll"
	.text
	.globl	f
f:
	tail	__muldi3
...
'+notafeature' is not a recognized feature for this target (ignoring feature)
EXIT=0
```

解读：`llc` 把未知特性当成警告并忽略，退出码仍是 0。于是你以为自己开了某个扩展，实际选指走了 libcall。这个边界比“报错”更危险，因为它不会打断脚本。

失败排查：把 `-mattr` 的每个字段和 `RISCVFeatures.td` 里的 `def FeatureStdExt*` 对照；看到 `tail __muldi3` 就说明 M 没有生效。不要用退出码判断特性是否真的打开。

## 源码落点

- `.scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin/`：预编译工具目录。`llc.exe`、`llvm-mc.exe`、`llvm-tblgen.exe`、`llvm-readobj.exe` 都在这里。
- `RISCVTargetMachine.cpp`：`LLVMInitializeRISCVTarget()` 注册 `riscv32`、`riscv64`、`riscv32be`、`riscv64be` 四个目标。
- `RISCVSubtarget.cpp`：`RISCVSubtarget::initializeSubtargetDependencies()` 依次做 `ParseSubtargetFeatures()`、`RISCV::updateCZceFeatureImplications()`、`TargetABI = RISCVABI::computeTargetABI()`、`RISCVFeatures::validate()`。
- `RISCVAsmPrinter.cpp`：`RISCVAsmPrinter::emitAttributes()` 调用 `RTS.emitTargetAttributes(SubtargetInfo, /*EmitStackAlign=*/true)`；源码注释说明它使用模块级 `MCSubtargetInfo`。
- `RISCVTargetStreamer.cpp`：`RISCVTargetStreamer::emitTargetAttributes()` 写入 `RISCVAttrs::STACK_ALIGN` 与 `RISCVAttrs::ARCH`，后者来自 `ISAInfo->toString()`。
- `RISCVFeatures.td`：`FeatureStdExtI`、`FeatureStdExtM`、`FeatureStdExtA`、`FeatureStdExtF`、`FeatureStdExtD`、`FeatureStdExtC`、`FeatureStdExtV` 等定义；`RISCVExtension<major, minor, ...>` 记录版本与依赖。
- `RISCVInstrInfo.td:818`：`def ADD`。这个行号只对 23.1.4 树有效。

## 常见坑

1. **把 `.attribute 5` 当成函数级特性**。它是文件级属性，来自模块级子目标。一个模块里不同函数可以有不同 `target-features`，但 `.attribute 5` 只能写一份。看到字符串和某个函数的实际指令不一致时，先查 `RISCVAsmPrinter::emitAttributes` 的注释。
2. **用 `D:\llvm-project` 的行号解释 23.1.4**。两棵树差一个开发周期。行号、符号名、生成物编号都可能漂移。源码锚点必须来自 `.scratch/llvm-23.1.4-riscv-td`。
3. **把 `-mattr` 写错后继续相信退出码**。未知特性只警告，退出码是 0，选指却已经退化成 libcall。判断特性是否生效，要看汇编里是否出现预期指令，不要看 `$LASTEXITCODE`。
4. **把 `.cfi_*` 当成机器指令**。它们是汇编层的调试/展开伪指令，不进入 `.text` 编码。用 `llvm-objdump -d` 看对象文件时不会出现 `cfi_startproc`。
5. **在 PowerShell 里直接写 `-mattr=+m,+a`**。逗号可能被参数解析吃掉，导致 `-mattr` 只收到一部分。把整个开关写成字符串。
6. **以为剩余几 GiB 可以“先试试构建”**。配置阶段就会失败或把磁盘写满。不能构建就明确标 guided，不要用“应该会”代替证据。

## 自我检查

- [ ] 我能说出本机 `llc --version` 的版本、构建类型和 host 三元组。
- [ ] 我能列出四个已注册的 RISC-V 目标，并解释 `riscv64be` 与 `riscv64` 的差别。
- [ ] 我能从 `.attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"` 里指出基础 ISA、显式扩展和隐含依赖。
- [ ] 我能解释为什么 `.attribute 5` 是文件级属性，而不是某个函数的局部属性。
- [ ] 我能用 `git describe` 证明 `D:\llvm-project` 不是 23.1.4，并说出引用它的行号会带来什么后果。
- [ ] 我能区分实测、源码依据、guided 三类证据，并给每条结论标上正确类别。
- [ ] 我能解释剩余不足 5 GiB 为什么不足以构建 `llc`，以及这对课程实验的具体限制。
- [ ] 我知道未知 `-mattr` 特性只产生警告、退出码仍为 0，并知道如何验证特性是否真的生效。

## 延伸阅读

- LLVM 官方文档：`llvm.org/docs/CommandGuide/llc.html`，重点看 `-mtriple`、`-mattr`、`-filetype`、`-stop-after`。
- LLVM 官方文档：`llvm.org/docs/TableGen/`，为第 04、05 章的 `.td` 阅读做准备。
- RISC-V psABI：`github.com/riscv-non-isa/riscv-elf-psabi-doc`，栈对齐、ABI 名称、ELF 属性的权威来源。
- RISC-V ISA 手册：扩展版本号与 `rv64i2p1` 这类字符串的语法。
- 同版本源码：`.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`、`RISCVSubtarget.cpp`、`RISCVAsmPrinter.cpp`、`MCTargetDesc/RISCVTargetStreamer.cpp`。

[返回目录](./README.md) | [下一章：后端全景与对象家族](./01-backend-map.md)