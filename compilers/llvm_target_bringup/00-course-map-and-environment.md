# Ch 00 — 课程地图与环境基线

> **目标**：把“接入一个 target”拆成 M0–M6 七个可验收阶段（M0 起点 + 六个递进能力），并钉死本课使用的 LLVM 源码树、构建、MiniRV 与 RISC-V 双基线。学完之后，你每读一章都能回答三件事：现在在哪一层、产物是什么、用哪条命令验收。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

Bring-up 最常见的失败不是“代码写不出来”，而是**不知道现在算不算完成**。

你会遇到一串看起来都像“能用了”的状态：

- `llvm-mc` 能把一条 `add` 汇编成 4 个字节，但 `llc` 一跑就报找不到 TargetMachine。
- `llc` 能从 IR 生成汇编，但 `-filetype=obj` 没有 object writer。
- 对象文件能被 `llvm-readobj` 读出来，但链接器不认识 machine type。
- 链接能过，qemu 一跑就跳飞，因为调用约定或栈帧不对。

如果只有“能编译吗”一个判断标准，上面每一步都会被误判成“快好了”。真实 bring-up 是**分层交付**：每一层有自己的接口、自己的产物、自己的验收命令。没有这张地图，你会把 frame lowering 的 bug 当成 ISel 的 bug，把链接器的问题当成编码器的问题，最后在错误的层里改三天。

所以本章先做两件事：

1. 用 M0–M6 七个阶段给“接入”下定义：M0 是起点，M1–M6 是六个递进能力。
2. 把所有环境变量钉死，保证你看到的输出和本课一致。

## 先建立直觉

把 bring-up 想成一段楼梯，而不是一个开关：

```
能力
 ^
 |                                                   M6 硬化 / 上游化
 |                                         M5 工具链（clang + lld + runtime）
 |                                 M4 对象 / 链接 / 运行
 |                       M3 ABI 完整的标量代码
 |               M2 控制流 / 调用 / 栈
 |       M1 IR -> asm 直线代码
 | M0 MC-only
 +------------------------------------------------------------------> 时间 / commit
   33d9ec8      ad74868       ...          e186e2a          6e7e7ab
```

每一级只承诺一个**可验证能力**。楼梯的比喻有两个关键含义：

- **下一级依赖上一级**：没有 MC 编码器，CodeGen 生成的 MCInst 没法变成字节；没有正确的 object writer，e2e 根本跑不起来。
- **上一级不能替代下一级**：`llc` 能出汇编，不代表 `llvm-mc` 能反汇编同一条指令；能跑 qemu，不代表 clang 能编 C。

从“谁在用”的角度看，三层用户对应三种 target 完成度：

```
手写汇编 / 固件用户  ->  llvm-mc / llvm-objdump / llvm-readobj   -> MC-only 就够
编译器实验用户        ->  llc（IR -> asm/obj）                    -> 需要 CodeGen
C/C++ 应用用户        ->  clang / lld / libc / compiler-rt        -> 需要完整工具链
```

本课的 MiniRV 已经走到 M4（能编译、能链接、能在 qemu 跑 17 个程序），但**没有走到 M5**：clang 不认识 `minirv64`。RISC-V 则是 M5/M6 的成熟参照。这个“不完整但有用”的状态，正是本章和下一章要解释的核心。

## 主线：M0–M6 七个阶段

在进入里程碑之前，先把本课的环境钉死。下面每一行都是会影响输出的变量；换一个版本、换一个 linker，你看到的 e2e 结果就可能不同。

| 项 | 值 | 用途 |
|---|---|---|
| 版本 | LLVM 24.0.0git | TableGen 生成物、CodeGen pipeline 的基线 |
| 源码树 | `/home/ubuntu/llvm/trees/dsl-tablegen` | 所有源码引用以它为准 |
| commit | `6e7e7abeb2b7`（2026-11-14） | 复现的最小锚点 |
| 分支 | `feat/riscv-trim` | 本课写作时的分支 |
| 构建 | `/home/ubuntu/llvm/builds/dsl-tablegen` | 所有 `llc`/`llvm-mc` 二进制来自这里 |
| 构建目标 | `X86;AArch64;RISCV;RVTrim;MiniRV` | `LLVM_TARGETS_TO_BUILD` 的实际值 |
| assertions | ON | 断言打开，接口用错会更早暴露 |
| 工具目录 | `B=/home/ubuntu/llvm/builds/dsl-tablegen/bin` | `llc`、`llvm-mc`、`llvm-readobj`、`llvm-objdump`、`llvm-lit` |
| 链接器 | `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld` | e2e 链接 MiniRV/RISC-V 对象 |
| 模拟器 | `/usr/bin/qemu-riscv64-static` | 运行 ELF，比较退出码 |
| MiniRV triple | `minirv64` | 本课的主角 |
| MiniRV 源码 | `$TREE/llvm/lib/Target/MiniRV` | MC、CodeGen、e2e 都在这里 |
| RISC-V 源码 | `$TREE/llvm/lib/Target/RISCV` | 每章的参照实现 |
| MiniRV 测试 | `$TREE/llvm/test/CodeGen/MiniRV`（49 `.ll`）、`$TREE/llvm/test/MC/MiniRV`（4 `.s`） | lit 回归网 |
| e2e | `$TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh`（17 `.ll`） | 端到端差分测试 |

### M0 — MC-only：先让汇编器和反汇编器认识你

**承诺**：给定 `.s`，能解析、编码、打印、反汇编、写出 ELF object；不承诺从 IR 生成代码。

**产物**：`llvm-mc -filetype=obj` 得到的 `.o`，以及 `llvm-objdump -d` 能读回的指令。

**验收**：

```console
$B/llvm-mc -triple=minirv64 -show-encoding m.s
$B/llvm-mc -triple=minirv64 -filetype=obj m.s -o m.o
$B/llvm-objdump -d --triple=minirv64 m.o
$B/llvm-readobj -h m.o
```

MiniRV 的 M0 是第一个 commit `33d9ec8`：只有 Triple、寄存器/格式/指令的 TableGen、AsmParser、Disassembler、InstPrinter、CodeEmitter、AsmBackend 和 ELF writer。commit message 明确写了“This target has no TargetMachine”，所以 `LLVMInitializeMiniRVTarget` 当时只是 MC 入口。RISC-V 的 M0 早就被 CodeGen 覆盖了，读它时要反过来找“哪些 MC 组件是 CodeGen 也复用的”。

### M1 — IR → asm：直线代码

**承诺**：给定不含控制流的 IR（常量、加减乘、`ret`），`llc` 能生成正确汇编。

**产物**：`.s` 文本。

**验收**：

```console
$B/llc -mtriple=minirv64 -O2 f.ll -o -
```

这一级需要 TargetMachine、Subtarget、TargetLowering、RegisterInfo、InstrInfo、SelectionDAGISel 和 AsmPrinter。MiniRV 的 `ad74868` 是这一步的起点：把 auto-td 生成的指令记录接进 SelectionDAG，并补上 `MiniRVTargetMachine.cpp` 里的 `LLVMInitializeMiniRVTarget`。注意这个符号从 MC 入口变成了 CodeGen 入口——同一个 `Targets.def` 条目，背后换了实现。

### M2 — 控制流、调用与栈

**承诺**：分支、循环、直接/间接调用、spill/reload、prologue/epilogue 都能工作。

**产物**：能链接、能在 qemu 上跑简单函数的对象。

**验收**：`llvm-lit` 的 CodeGen 用例 + e2e 的子集。

MiniRV 的 `75182ea`（选分支、spill 到栈）、`751291d`（直接调用、保存返回地址）、`89d0af0`（间接调用、符号地址）都在这一层。RISC-V 对照点：`RISCVFrameLowering::emitPrologue`、`RISCVInstrInfo::storeRegToStackSlot`。

### M3 — ABI 完整的标量代码

**承诺**：参数/返回值、varargs、i128、软浮点、栈对齐、动态 alloca、栈保护、TLS 显式拒绝等标量 ABI 路径可用。

**产物**：通过 ABI 回归测试的 `.o`，以及“不支持就报错”的诊断。

**验收**：`llvm-lit` 全量 + e2e 中涉及 varargs/i128/软浮点的程序。

这一级最容易被低估。MiniRV 用了 `9f5ca73`（RV64M/32-bit/窄整数）、`a356518`（varargs）、`a61c0cb`（i128 寄存器对）、`92b98a5`（软浮点）、`af98624`（atomics 转 libatomic、拒绝 TLS）等一长串 commit。RISC-V 的 `RISCVCallingConv.td` 和 `RISCVISelLowering::LowerFormalArguments` 是主要参照。

### M4 — 对象、链接、运行

**承诺**：`-filetype=obj` 产出可被 `ld.lld` 链接的 ELF，链接后的程序能在 qemu 上得到与 RISC-V 参照相同的退出码。

**产物**：ELF 可执行文件 + e2e 报告。

**验收**：

```console
$B/llvm-readobj -h f.o
LLVM_BUILD_DIR=$BUILD LDLLD=.../ld.lld QEMU=.../qemu-riscv64-static \
  $TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh
```

MiniRV 的 `e186e2a` 加入 e2e 与 code-size harness；`MiniRVELFObjectWriter` 复用 `ELF::EM_RISCV`，所以 `ld.lld -m elf64lriscv` 不需要改一行就能链接。这是“复用成熟 ELF machine type”的收益，也是下一章要讲的边界代价。

### M5 — 工具链

**承诺**：`clang --target=<triple>` 能编 C/C++，driver 能找到 assembler/linker，runtime（compiler-rt、libc、libm）可用，ABI 与目标平台一致。

**产物**：完整的 `clang -c` / `clang` 链接流程。

**验收**：

```console
clang --target=riscv64 -### -c f.c
clang --target=riscv64 -c f.c -o f.o
```

MiniRV **没有** M5。`llc -mtriple=minirv64` 能工作，但 `clang --target=minirv64 -c` 会直接报 `unknown target triple 'minirv64'`。RISC-V 是完整的 M5 参照：Clang 有 `RISCVTargetInfo`、driver 有 RISC-V toolchain、ABI 有 `lp64`/`lp64d` 等。

### M6 — 硬化与上游化

**承诺**：诊断完整、边界条件有测试、代码尺寸/性能有基线、补丁能拆成可评审的系列、上游 reviewer 能接受。

**产物**：测试覆盖、诊断信息、上游补丁系列。

**验收**：全量 `llvm-lit`、e2e 全绿、`git log` 能讲出每一步的理由，补丁按“MC → CodeGen → ABI → 对象 → 测试”拆分。

MiniRV 的 42 个 commit 是 M6 的素材：`dcd9fdb`（与零比较用 x0 分支形式）、`90e179c`（大栈调整拆成两条 addi）、`6e7e7ab`（大栈帧围绕 callee-saved spill 拆分）都是“先正确，再边界，再代码质量”的例子。

### Part / 章节 → 里程碑 → 验收命令

| 范围 | 里程碑 | 验收命令 |
|---|---|---|
| Ch 00–01 | M0–M6 地图 | `$B/llc --version`；`$B/llc -mtriple=minirv64 -O2 f.ll -o -` |
| Ch 02–03 | M0 | `$B/llvm-mc -triple=minirv64 -show-encoding m.s`；`$B/llvm-readobj -h m.o` |
| Ch 04–06 | M0 骨架 | `$B/llvm-lit $TREE/llvm/test/MC/MiniRV` |
| Ch 07–08 | M0 完整 MC | `$B/llvm-mc -filetype=obj`；`$B/llvm-objdump -d` |
| Ch 09–11 | M1 入口/寄存器/frame | `$B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel f.ll -o -` |
| Ch 12–14 | M1 合法化/选指/内存 | `$B/llc -mtriple=minirv64 -O2 f.ll -o -` |
| Ch 15 | M2 控制流 | `$B/llvm-lit $TREE/llvm/test/CodeGen/MiniRV` |
| Ch 16–18 | M2 调用/参数/栈 | e2e 子集（`t1.ll`、`tc.ll`、`vla*.ll`） |
| Ch 19–21 | M3 常量/软浮点/异常 | `$B/llvm-lit` + e2e 全量 |
| Ch 22 | M4 对象/链接 | `$B/llvm-readobj -h f.o`；e2e 全量 |
| Ch 23–25 | M4/M6 诊断/测试 | `$B/llvm-lit` 53 通过；e2e 17/17 |
| Ch 26 | M5 Clang（MiniRV 为 guided） | `clang --target=riscv64 -### -c f.c` |
| Ch 27–28 + Final | M6 上游 | 补丁系列 + 全量验收 |

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| Triple | `llvm/include/llvm/TargetParser/Triple.h` | `Triple::minirv64`、`Triple::isMiniRV64` | 一个 arch 名字如何进入枚举和判定 |
| DataLayout | `llvm/lib/TargetParser/TargetDataLayout.cpp` | `Triple::computeDataLayout`、`computeRISCVDataLayout` | `minirv64` 复用 RISC-V 的 64 位布局 |
| 注册 | `llvm/lib/Target/MiniRV/TargetInfo/MiniRVTargetInfo.cpp` | `RegisterTarget<Triple::minirv64>` | target 名字、描述、组件名 |
| MC | `llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCTargetDesc.cpp` | `LLVMInitializeMiniRVTargetMC` | 8 个 `RegisterMC*` 调用 |
| CodeGen | `llvm/lib/Target/MiniRV/MiniRVTargetMachine.cpp` | `LLVMInitializeMiniRVTarget`、`MiniRVTargetMachine` | M1 之后才存在的入口 |
| Asm 输出 | `llvm/lib/Target/MiniRV/MiniRVAsmPrinter.cpp` | `MiniRVAsmPrinter::emitInstruction` | MachineInstr → MCInst → streamer |
| 对象 | `llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVELFObjectWriter.cpp` | `MiniRVELFObjectWriter` | `ELF::EM_RISCV` 与 relocation 映射 |
| 测试 | `llvm/test/CodeGen/MiniRV`、`llvm/test/MC/MiniRV` | 49 `.ll` + 4 `.s` | 每层的最小回归 |
| e2e | `llvm/lib/Target/MiniRV/dev/e2e/run.sh` | 17 个 `.ll` | 双编译、链接、qemu、比较退出码 |

## 实测：环境自检

### 1. 源码树指纹

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen && git log -1 --format='%H %h %ad %s' --date=iso
6e7e7abeb2b772e6ebac14241eab1ad94f28950b 6e7e7abeb2b7 2026-11-14 03:32:02 +0800 [MiniRV] Split large frames around the callee-saved spills
```

`6e7e7abeb2b7` 是本课钉死的 commit。看到不同的 commit，先不要继续，TableGen 生成物和 CodeGen pass pipeline 都可能已经变了。

### 2. `llc --version`：注册了哪些 target

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc --version
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
  Default target: x86_64-unknown-linux-gnu
  Host CPU: znver2

  Registered Targets:
    aarch64    - AArch64 (little endian)
    aarch64_32 - AArch64 (little endian ILP32)
    aarch64_be - AArch64 (big endian)
    arm64      - ARM64 (little endian)
    arm64_32   - ARM64 (little endian ILP32)
    minirv64   - MiniRV (auto-td demo)
    riscv32    - 32-bit RISC-V
    riscv32be  - 32-bit big endian RISC-V
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
    rvtrim64   - RVTrim: trimmed RV64IMA
    x86        - 32-bit X86: Pentium-Pro and above
    x86-64     - 64-bit X86: EM64T and AMD64
```

关键点：

- `minirv64` 在列表里，说明 `TargetRegistry` 里有它，`llc` 能按名字找到。
- `rvtrim64` 也在，说明本课树里还有另一个裁剪 target；不要把它和 MiniRV 混为一谈。
- 列表里有名字**不等于** CodeGen 完整，更不等于 clang 认识它。注册只是第一层。

### 3. 一条最小 IR 的编译

```console
$ cat add3.ll
define i64 @add3(i64 %a, i64 %b, i64 %c) {
entry:
  %t = add i64 %a, %b
  %r = mul i64 %t, %c
  ret i64 %r
}

$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 add3.ll -o -
	.file	"add3.ll"
	.text
	.globl	add3                            # -- Begin function add3
	.type	add3,@function
add3:                                   # @add3
	.cfi_startproc
# %bb.0:                                # %entry
	add	x3, x10, x11
	mul	x10, x3, x12
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	add3, .Lfunc_end0-add3
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

这条输出同时证明了三件事：

- CodeGen 入口存在：`llc` 没有报“target does not support codegen”。
- 调用约定是 `x10/x11/x12` 传参、`x10` 返回、`x1` 是返回地址。
- `%t` 被分配到 `x3`，说明寄存器分配和 spill 机制已经在工作。

### 4. `llvm-readobj -h`：对象文件长什么样

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 -filetype=obj add3.ll -o add3.o
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -h add3.o

File: add3.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
ElfHeader {
  Ident {
    Magic: (7F 45 4C 46)
    Class: 64-bit (0x2)
    DataEncoding: LittleEndian (0x1)
    FileVersion: 1
    OS/ABI: SystemV (0x0)
    ABIVersion: 0
    Unused: (00 00 00 00 00 00 00)
  }
  Type: Relocatable (0x1)
  Machine: EM_RISCV (0xF3)
  Version: 1
  Entry: 0x0
  ProgramHeaderOffset: 0x0
  SectionHeaderOffset: 0x138
  Flags [ (0x0)
  ]
  HeaderSize: 64
  ProgramHeaderEntrySize: 0
  ProgramHeaderCount: 0
  SectionHeaderEntrySize: 64
  SectionHeaderCount: 7
  StringTableSectionIndex: 1
}
```

注意 `Format: elf64-littleriscv`、`Arch: riscv64`、`Machine: EM_RISCV`。MiniRV 没有自己的 ELF machine ID，它复用 RISC-V 的。这是 M4 能直接用 `ld.lld -m elf64lriscv` 的原因，也是它不能在 ELF 层面被当作“独立平台”的原因。

### 5. `llvm-lit`：回归测试计数

```console
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ $B/llvm-lit -s -j 16 $TREE/llvm/test/CodeGen/MiniRV $TREE/llvm/test/MC/MiniRV
-- Testing: 53 tests, 16 workers --
Testing:  0.. 10.. 20.. 30.. 40.. 50.. 60.. 70.. 80.. 90..

Testing Time: 0.19s

Total Discovered Tests: 53
  Passed: 53 (100.00%)
```

53 = 49 个 CodeGen `.ll` + 4 个 MC `.s`。这 53 个用例是**当前语料**的回归网，不是“ABI 全覆盖”的证明；新增指令或 ABI 路径时必须补测试。

### 6. e2e harness：17 个程序

```console
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
    LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
    QEMU=/usr/bin/qemu-riscv64-static \
    $TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh
artifacts: /tmp/tmp.HLq5jrnuui
OK   bf10             exit=108
OK   bf2              exit=21
OK   bigframe         exit=42
OK   bigframe2        exit=42
OK   edge             exit=237
OK   jt               exit=129
OK   misc             exit=114
OK   narrowstack      exit=223
OK   selftest         exit=66
OK   t1               exit=20
OK   t150             exit=150
OK   tc               exit=105
OK   twoarr           exit=129
OK   va               exit=184
OK   vla              exit=17
OK   vla2             exit=39
OK   vla3             exit=45
```

17/17 全绿。这个 harness 不是“只跑 MiniRV”：它对每个 `.ll` **同时**用 `minirv64` 和 `riscv64 -mattr=+m` 编译，分别链接、分别 qemu 运行，然后比较退出码。MiniRV 的退出码必须和成熟 RISC-V 后端一致。

## 如何复现 e2e

脚本在 `$TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh`，关键环境变量只有四个：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLVM_BUILD_DIR` | `/home/ubuntu/llvm/builds/dsl-tablegen` | 找 `bin/llc`、`bin/llvm-mc` |
| `LDLLD` | `ld.lld` | 链接 MiniRV/RISC-V 对象 |
| `QEMU` | `qemu-riscv64-static` | 运行链接后的 ELF |
| `TREE` | 由脚本自己 `git rev-parse` 得到 | 定位 `start.s` 和 `.ll` |

完整命令：

```console
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
    LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
    QEMU=/usr/bin/qemu-riscv64-static \
    $TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh
```

脚本逻辑（读一遍比跑十遍有用）：

1. 检查 `llc`、`llvm-mc`、`LDLLD`、`QEMU` 是否存在；缺一个就 `exit 77`（lit 里表示 skip）。
2. 用 `llvm-mc -triple=riscv64` 汇编 `start.s`。MiniRV 的汇编器故意没有 `ecall` 和寄存器别名，启动代码必须用参照 target 汇编。
3. 对每个 `.ll` 跑两次 `llc`：`-mtriple=minirv64` 和 `-mtriple=riscv64 -mattr=+m`。
4. 用 `ld.lld -m elf64lriscv --no-relax` 分别链接。
5. 用 qemu 运行，比较两个退出码；相同打印 `OK`，不同打印 `FAIL` 并置失败位。

这里的 `--no-relax` 很重要：MiniRV 复用了 RISC-V 的重定位类型，e2e 用 `--no-relax` 把链接行为限制在它验证过的范围内，避免把“链接器 relaxation 的差异”混进 target 的失败里。

## 证据约定：实测 / 源码依据 / guided

本课所有结论分三类，读的时候要能区分：

| 标记 | 含义 | 例子 |
|---|---|---|
| **实测** | 我在本机跑过命令，贴的是原样输出 | `llc --version` 的 target 列表、e2e 的 17 行 `OK` |
| **源码依据** | 对照 `6e7e7abeb2b7` 的文件与符号，不靠记忆 | `MiniRVELFObjectWriter` 构造时传 `ELF::EM_RISCV` |
| **guided** | 需要改源码/重新构建，课程给命令、差异思路和预期现象，不伪造构建日志 | 给 MiniRV 接 Clang driver、补一个新的 ELF machine ID |

行号会漂移，所以本课优先写“文件 + 符号名”。只有写作时用 `grep -n` 现场核对过的行号才会出现。

## 反直觉与踩雷

1. **错误直觉**：`llc --version` 里有 `minirv64`，说明 target 已经接好了。 → **正确认识**：注册只证明 `TargetRegistry` 知道这个名字。`llc` 能出汇编还需要 CodeGen，`clang` 能编 C 还需要 driver 和 runtime。MiniRV 有前者，没有后者。

2. **错误直觉**：53 个 lit 测试全过，ABI 就完整了。 → **正确认识**：lit 测的是当前语料。MiniRV 的 49 个 CodeGen 用例覆盖了它实现过的路径，但 TLS 是显式拒绝、scalable vector 是显式拒绝、Clang 前端根本没接。测试全绿只说明“已实现的部分没退化”。

3. **错误直觉**：`llvm-config --targets-built` 是注册 target 的权威列表。 → **正确认识**：它读的是构建期写入的字符串，本机输出只有 `X86 AArch64 RISCV`，但 `llc --version` 明确列出 `minirv64` 和 `rvtrim64`。验收运行时注册要用 `llc --version` 或 `TargetRegistry::lookupTarget`，不要用 `llvm-config`。

4. **错误直觉**：e2e 全绿说明输出也全对。 → **正确认识**：`run.sh` 只比较**退出码**，不比较 stdout。一个程序如果打印错但退出码碰巧相同，harness 会放过。Ch 25 会讲怎么加输出/指令数基线。

5. **错误直觉**：本课所有命令都能在 `dsl-tablegen` 构建里跑。 → **正确认识**：本树的 `builds/dsl-tablegen/bin/clang` 目前有 `symbol lookup error: libLLVMLTO.so.24.0git: undefined symbol ...SampleProfileFile...`。这是本地构建产物的问题，不是 target 的问题。Ch 01 的 clang driver 证据用 `builds/pr-lane/bin/clang`（同为 24.0.0git，commit `1bfdad8`），并在正文标明。

## 进阶：再往深一层

- **按 commit 读，而不是按文件读**。`git log --reverse --format='%h %ad %s' --date=short -- llvm/lib/Target/MiniRV` 会给你 42 步的真实顺序。挑三个 commit 做 `git show`：`33d9ec8`（MC-only）、`ad74868`（CodeGen 入口）、`e186e2a`（e2e harness）。你会看到“接口是在哪一步被迫长出来的”。
- **给 e2e 加一个程序**。复制 `t1.ll`，改成返回一个不同的常量，跑 harness，确认 MiniRV 和 RISC-V 退出码一致。再故意改错一个返回值，看 harness 怎么报 `FAIL`。
- **对照 RISC-V 的同一条路径**。MiniRV 的 `add3` 输出是 `add x3,x10,x11; mul x10,x3,x12; jalr x0,0(x1)`；RISC-V 在 `-O2` 下可能生成不同的寄存器分配和 frame 布局。差异本身就是“最小实现”和“成熟后端”的距离。

## 本章重点整理

- Bring-up 通常说“六个里程碑”，实际编号是 M0–M6：M0 是 MC-only 起点，M1–M6 是六个递进能力（M1 直线代码、M2 控制流/调用/栈、M3 ABI 标量、M4 对象/链接/运行、M5 工具链、M6 硬化/上游）。
- 每个里程碑都有独立产物和独立验收命令；`llvm-mc`、`llc`、`llvm-readobj`、`llvm-lit`、e2e 分别守不同的门。
- 环境必须钉死：树 `dsl-tablegen`、commit `6e7e7abeb2b7`、构建 `builds/dsl-tablegen`、linker `pr-lane/bin/ld.lld`、qemu `qemu-riscv64-static`。
- MiniRV 走到 M4，没走到 M5；RISC-V 是 M5/M6 参照。这个差距是后面 27 章的内容。
- 证据分实测、源码依据、guided；不要把“guided 的预期”写成“实测的输出”。

## 自我检核

- [ ] 不看笔记，按顺序说出 M0–M6，并给每个里程碑一条验收命令。
- [ ] 解释为什么 `llc --version` 里有 `minirv64`，但 `clang --target=minirv64 -c` 会失败。
- [ ] 解释 53 个 lit 测试和 17 个 e2e 程序分别覆盖什么、不覆盖什么。
- [ ] 说出 e2e 脚本里的四个环境变量，以及 `--no-relax` 存在的原因。
- [ ] 区分“实测”“源码依据”“guided”，并各举一个本课例子。

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方
  - **读哪里**：先读 “The LLVM Target-Independent Code Generator” 和 “Target Machine” 两节。
  - **能学到什么**：TargetMachine、Subtarget、ISel、AsmPrinter 在官方架构里的位置，和本章的 M1/M2 对应。
  - **前提**：能读基本 LLVM IR，知道 `llc` 的输入输出。

- **[LLVM TableGen](https://llvm.org/docs/TableGen/)** — LLVM 官方
  - **读哪里**：`-gen-instr-info`、`-gen-asm-matcher`、`-gen-dag-isel` 的说明。
  - **能学到什么**：M0 的指令记录如何变成 MC 编码器和 CodeGen 选指表。
  - **前提**：读过至少一个 `.td` 文件；否则先看 Ch 06。

- **[llvm-mc Command Guide](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — LLVM 官方
  - **读哪里**：`-show-encoding`、`-disassemble`、`-filetype=obj`。
  - **能学到什么**：M0 的验收命令到底在做什么，以及为什么它不经过 CodeGen。
  - **前提**：能在命令行跑 `llvm-mc`。

- **[本课 README](./README.md)** — 本课程
  - **读哪里**：课程地图、学习方式、证据约定。
  - **能学到什么**：29 章 + 4 个练习 + Final 的整体编排，以及和 `riscv_backend` 等课程的分工。
  - **前提**：无。

- **[RISC-V 后端课程](../riscv_backend/README.md)** — 本仓库
  - **读哪里**：ABI、MC、ISel、frame lowering 章节。
  - **能学到什么**：本课只给“参照点”的地方，那里有完整实现和 ISA 细节。
  - **前提**：先读完本章的六里程碑，知道自己要找哪一层。

→ [Ch 01 什么算“接入一个 target”](./01-what-is-target-bringup.md)
