# Ch 04 — Triple 与 DataLayout

> **目标**：追踪一个 arch 名字在 `Triple.h` / `Triple.cpp` / `TargetDataLayout.cpp` 里的全部落点，并解释每个 switch 漏掉时会在哪一层、以什么方式炸。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`（`X86;AArch64;RISCV;RVTrim;MiniRV`，assertions on）。本章命令约定 `T=/home/ubuntu/llvm/trees/dsl-tablegen`、`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`。

## 为什么需要这个？

在 LLVM 里，arch 名字不是字符串，是**编译期枚举** `Triple::ArchType`。这带来一个很硬的约束：新加一个 arch，不是“在表里加一行”，而是“把所有对这张表的 `switch` 都补一遍”。漏一个的后果分三档：

- **解析不到**：`llc` 直接说 `unable to get target`，你立刻知道。
- **assert 崩**：`llvm_unreachable("Invalid ArchType!")`，assertions 构建下至少能定位。
- **静默算错**：字节序默认成 big-endian、`-m64` 不变、`long double` 变成 64 位——编译器不报错，生成的对象是错的。

MiniRV 的第一次提交 `33d9ec8c1aed` 在 Triple 这一层只动了两个文件，却写了 12 个 hunk。这个数字就是本章的主题：**加 arch 本身不贵，贵在穷举**。而 DataLayout 是第二个轴：Triple 回答“我是谁”，DataLayout 回答“我的指针多大、怎么对齐、字节序如何”。MC-only 阶段可以只答第一问，一旦有了 `TargetMachine`，第二问就躲不掉了。

## 先建立直觉

```
"minirv64-unknown-unknown-elf"
        │
        │ ① Triple::parseArch()              字符串 → 枚举
        ▼
Triple::minirv64   (Triple.h: enum ArchType)
        │
        ├─ ② getArchTypeName()      枚举 → 字符串（打印、规范化、诊断）
        ├─ ③ getArchTypePrefix()    "minirv"（RuntimeDyld / 下游约定）
        ├─ ④ getArchPointerBitWidth() → 64   （isArch64Bit）
        ├─ ⑤ isLittleEndian() → true         （决定 DataLayout 的 e/E）
        ├─ ⑥ getDefaultFormat() → ELF
        ├─ ⑦ getDefaultExceptionHandling() → DwarfCFI
        └─ ⑧ getDefaultLongDoubleFormat() → IEEEquad
        │
        │ ⑨ RegisterTarget<Triple::minirv64>    TargetInfo 里注册 ArchMatchFn
        ▼
TargetRegistry（单链表） ── lookupTarget(Triple) ──► MiniRV Target
        │
        │ ⑩ Triple::computeDataLayout()       只有 CodeGen 会调用
        ▼
"e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
        │
        ├─ MC 层：MCAsmInfo / MCSubtargetInfo（llvm-mc 能用）
        └─ CodeGen 层：TargetMachine 构造 DataLayout（llc 能用）
```

记住两个轴：

- **身份轴**：`ArchType` 枚举 + `TargetRegistry`，回答“这是谁”。
- **形状轴**：DataLayout 字符串，回答“对象长什么样”。

多数 bring-up 教程只讲身份轴，然后在第一次 `-filetype=obj`、第一次 spill、第一次传 `long double` 时被形状轴打脸。

## 主线：33d9ec8 到底改了什么

### 4.1 12 个 hunk 的逐项清单

`git show --stat 33d9ec8c1aed` 的相关行：

```console
$ git show --stat --format='' 33d9ec8c1aed | grep -E 'Triple|files changed'
 llvm/include/llvm/TargetParser/Triple.h            |   5 +
 llvm/lib/TargetParser/Triple.cpp                   |  14 +-
 48 files changed, 1484 insertions(+), 6 deletions(-)
```

逐个落点：

| # | 位置 | 作用 | 漏掉会怎样 |
|---|---|---|---|
| 1 | `Triple.h` `enum ArchType` 的 `minirv64` | 枚举值本身 | 后面所有 case 都没有可写的符号 |
| 2 | `Triple.h` `isMiniRV()` / `isMiniRV64()` | 谓词，`getDefaultExceptionHandling` 用它分派 | 异常模型静默变成 `None` |
| 3 | `Triple.cpp` `getArchTypeName` | 枚举 → `"minirv64"` | `llvm_unreachable("Invalid ArchType!")`（`Triple.cpp:172`） |
| 4 | `Triple.cpp` `getArchTypePrefix` | `"minirv"` | 不崩；但下游按前缀分派时会拿到空串，契约被破坏 |
| 5 | `Triple.cpp` `getArchTypeForLLVMName` | 名字 → 枚举，供 `lookupTarget(ArchName, ...)` | `-march=minirv64` 找不到 target |
| 6 | `Triple.cpp` `parseArch` | triple 第一段 → 枚举 | triple 解析成 `UnknownArch`，`llc` 说找不到 target |
| 7 | `Triple.cpp` `getDefaultFormat` | 默认 `ELF` | 第一次调用 `getObjectFormat()` 时 `llvm_unreachable("unknown architecture")` |
| 8 | `Triple.cpp` `getArchPointerBitWidth` | 返回 64 | `isArch64Bit()` 触发 `llvm_unreachable("Invalid architecture value")` |
| 9 | `Triple.cpp` `get64BitArchVariant` | 已是 64 位，原样返回 | **静默**返回自身（clang `-m64` 路径） |
| 10 | `Triple.cpp` `isLittleEndian` | 返回 `true` | **静默**返回 `false`，DataLayout 变 `E-m:e-...` |
| 11 | `Triple.cpp` `getDefaultExceptionHandling` | 走 `isMiniRV()` 谓词，返回 `DwarfCFI` | **静默**落到 `None`，`TargetMachine` 不声明支持 DWARF EH |
| 12 | `Triple.cpp` `getDefaultLongDoubleFormat` | `IEEEquad` | **静默**落到默认 `IEEEdouble`，Clang 的 `long double` ABI 错 |

注意第 11 项不是 `switch (getArch())`，而是 `if (... || isRISCV() || isRVTrim() || isMiniRV() || ...)`。这类**谓词分派**最容易漏：你补了 10 个 switch，编译器一个警告都不会给。

### 4.2 三种失败模式

```cpp
// 1) assert 型：漏了就是未定义行为/崩溃
StringRef Triple::getArchTypeName(ArchType Kind) {
  switch (Kind) { ... }
  llvm_unreachable("Invalid ArchType!");   // Triple.cpp:172
}

// 2) 静默默认型：不崩，但答案错
bool Triple::isLittleEndian() const {
  switch (getArch()) { ... }
  default:
    return false;                          // 漏了 MiniRV 就是“大端”
}

// 3) 解析型：枚举变成 UnknownArch，registry 找不到
static Triple::ArchType parseArch(StringRef ArchName) {
  ... .Default(Triple::UnknownArch)
}
```

这也是为什么 bring-up 时**先开 assertions**。同一份代码在 Release 构建里，第 1 类（`llvm_unreachable`）会退化成 `__builtin_unreachable()`，表现是随机的。

### 4.3 为什么加 ArchType 是最侵入的一层

因为它是**枚举 + 无默认语义的穷举**，而不是可注册的字符串表：

1. `enum ArchType` 的取值顺序被 `UnknownArch = 0` 钉死，新增值只能插在中间或末尾，但 `getArchTypeName` 与 `parseArch` 必须**双向一致**。写反了不会报错，只会打印出另一个 arch 的名字。
2. 四个函数没有安全的默认值：`getArchTypeName`、`getDefaultFormat`、`getArchPointerBitWidth` 直接 `llvm_unreachable`；`getArchTypePrefix` 默认返回空串，属于“不崩但语义缺失”。
3. 四个函数有**错误的**默认值：`isLittleEndian`（false）、`get64BitArchVariant`（原样返回）、`getDefaultExceptionHandling`（None）、`getDefaultLongDoubleFormat`（IEEEdouble）。它们是最危险的，因为测试可能全绿，直到你跑跨架构 ABI 对比。
4. 它不在 target 自己的目录里。一个后端开发者可以只读 `llvm/lib/Target/MiniRV`，但 Triple 的改动在 `llvm/lib/TargetParser`，评审时最容易被问“你为什么改公共代码”。

RISC-V 的参照意义在这里：`riscv64` 是上游 arch，所有 switch 早就补全；MiniRV 抄的是**改动模式**，不是改动内容。你在自己的 target 上可以照抄这张表。

#### 新 arch 的最小 diff 清单

按这个顺序改，能把“漏一个”的概率降到最低：

1. `Triple.h`：加 `enum ArchType` 值，再加 `isXxx()` / `isXxx64()` 谓词。
2. `Triple.cpp` 名字相关：`getArchTypeName`、`getArchTypePrefix`、`getArchTypeForLLVMName`、`parseArch`。
3. `Triple.cpp` 语义相关：`getDefaultFormat`、`getArchPointerBitWidth`、`get64BitArchVariant`、`isLittleEndian`。
4. `Triple.cpp` 两个“看起来不急”的：`getDefaultExceptionHandling`（谓词分支）、`getDefaultLongDoubleFormat`。
5. `TargetDataLayout.cpp`：`computeDataLayout` 加 case；没有 `TargetMachine` 时可以后补，但别忘。
6. `TargetInfo/MiniRVTargetInfo.cpp`：`RegisterTarget<Triple::minirv64>`，让 registry 能按 arch 匹配。
7. 验收三连：`llc --version`（注册成功）、`llvm-mc -triple=...`（MC 链路）、`llc -filetype=obj`（格式与 DataLayout）。

### 4.4 DataLayout：33d9ec8 没改，ad74868 才补

这是本章最值得记的一个事实：**MC-only 提交没有碰 `TargetDataLayout.cpp`**。用 `git log -S` 可以证明：

```console
$ git log --oneline -S minirv64 -- llvm/lib/TargetParser/TargetDataLayout.cpp
ad7486897b30 [MiniRV] Feed the auto-td records into SelectionDAG
```

原因在调用点：

```cpp
// llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:90
CodeGenTargetMachineImpl::CodeGenTargetMachineImpl(...)
    : TargetMachine(T, TT.computeDataLayout(Options.MCOptions.getABIName()), TT, ...)
```

只有构造 `TargetMachine` 时才会调用 `Triple::computeDataLayout()`。33d9ec8 的 MiniRV 没有 `TargetMachine`，`llvm-mc` 只走 MC 层，所以当时确实不需要。ad74868 加了 `MiniRVTargetMachine`，同一个提交就必须补：

```cpp
// llvm/lib/TargetParser/TargetDataLayout.cpp
  case Triple::riscv32:
  case Triple::riscv64:
  case Triple::riscv32be:
  case Triple::riscv64be:
  case Triple::rvtrim64:
  case Triple::minirv64:
    return computeRISCVDataLayout(*this, ABIName);
```

而 `computeRISCVDataLayout` 里也明确加了 MiniRV 分支：

```cpp
if (TT.isRISCV64() || TT.isRVTrim64() || TT.isMiniRV64()) {
  Ret += "-p:64:64";
  ...
  Ret += "-i64:64-i128:128-n32:64";
}
```

`computeDataLayout` 的 switch 末尾是 `llvm_unreachable("Invalid arch")`（`TargetDataLayout.cpp:584`）。所以漏掉这一条的后果不是“layout 差一点”，而是**第一次 `llc` 就崩**。

### 4.5 逐字段读 DataLayout

`llc -stop-after=finalize-isel` 打出来的真实字符串：

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel $T/llvm/test/CodeGen/MiniRV/alu.ll -o - | head -5
--- |
  ; ModuleID = '.../llvm/test/CodeGen/MiniRV/alu.ll'
  source_filename = ".../llvm/test/CodeGen/MiniRV/alu.ll"
  target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
  target triple = "minirv64"
```

（ModuleID 的绝对路径已截断为 `...`。）

| 字段 | 含义 | 对 MiniRV 的意义 | 写错的后果 |
|---|---|---|---|
| `e` | little-endian | 与 `Triple::isLittleEndian()` 一致 | 常量、load/store 字节序全反 |
| `m:e` | ELF name mangling | 符号名按 ELF 规则 | 符号修饰/汇编名错 |
| `p:64:64` | 指针 64 位、ABI 对齐 64 位 | RV64 的 XLEN=64 | GEP/指针宽度错，栈槽大小错 |
| `i64:64` | `i64` ABI 对齐 8 字节 | RV64 自然对齐 | 结构体布局、原子操作对齐错 |
| `i128:128` | `i128` ABI 对齐 16 字节 | 为后续 128 位整数 lowering 铺路（commit `97ed4d6`） | i128 spill/参数传递错位 |
| `n32:64` | native integer 宽度为 32/64 | 告诉优化器 i64 是“原生”的 | 原子/整数合法化判断退化 |
| `S128` | 栈自然对齐 128 位 = 16 字节 | RISC-V psABI 要求 16 字节栈对齐 | 调用约定错、`i128`/向量 spill 错位 |

字符串里**没有**的字段同样是信息：没有 `f32`/`f64`（MiniRV 没有浮点寄存器）、没有 `v*`（没有向量）、没有 `a:`（聚合用默认对齐）、没有 `ni:`（没有 non-integral pointer）、没有 `p0`/`p270`（没有自定义地址空间）。

这串和 `llc -mtriple=riscv64` 打出来的一模一样，因为两者共用 `computeRISCVDataLayout`。**共用是特性，不是巧合**：MiniRV 的 ABI 就是 RV64 lp64，没有理由自己发明一套。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| Triple 枚举 | `llvm/include/llvm/TargetParser/Triple.h` | `ArchType::minirv64`、`isMiniRV()`、`isMiniRV64()` | 枚举位置；谓词被谁调用 |
| Triple 解析/格式化 | `llvm/lib/TargetParser/Triple.cpp` | `parseArch`、`getArchTypeForLLVMName`、`getArchTypeName`、`getArchTypePrefix` | 名字 ↔ 枚举必须双向一致 |
| Triple 语义 | `llvm/lib/TargetParser/Triple.cpp` | `getDefaultFormat`、`getArchPointerBitWidth`、`get64BitArchVariant`、`isLittleEndian`、`getDefaultExceptionHandling`、`getDefaultLongDoubleFormat` | 哪些有错误默认值 |
| DataLayout | `llvm/lib/TargetParser/TargetDataLayout.cpp` | `Triple::computeDataLayout`、`computeRISCVDataLayout` | `minirv64` 走 RISC-V 分支 |
| DataLayout 消费方 | `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp` | 构造函数里的 `TT.computeDataLayout(...)` | 为什么 MC-only 不需要 |
| Registry 查找 | `llvm/lib/MC/TargetRegistry.cpp` | `TargetRegistry::lookupTarget` | `ArchMatchFn` 与 `getArchTypeForLLVMName` 两条路径 |
| 对象文件回读 | `llvm/include/llvm/Object/ELFObjectFile.h` | `getArch()` 由 `EM_RISCV` 映射回 `riscv64` | 为什么对象里没有 `minirv64` |

## 实测：五条命令看穿身份轴与形状轴

### 1. `llc -mtriple=minirv64`

```console
$ $B/llc -mtriple=minirv64 -O2 $T/llvm/test/CodeGen/MiniRV/alu.ll -o - | sed -n '1,11p'
	.file	"alu.ll"
	.text
	.globl	add                             # -- Begin function add
	.type	add,@function
add:                                    # @add
	.cfi_startproc
# %bb.0:
	add	x10, x10, x11
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	add, .Lfunc_end0-add
```

Triple 解析成功 → registry 找到 MiniRV → `MiniRVTargetMachine` 构造 → DataLayout 生效 → SelectionDAG 选出 `ADD` → AsmPrinter 打印 `x10` 风格寄存器名。注意返回是 `jalr x0, 0(x1)` 而不是 RISC-V 的 `ret` 别名：MiniRV 的 `PseudoRET` 会被展开成 `JALR X0, X1, 0`（Ch 07 会讲这种 pseudo expansion）。

### 2. `llc -mtriple=riscv64 -mattr=+m`

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 $T/llvm/test/CodeGen/MiniRV/alu.ll -o - | sed -n '1,14p'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
	.file	"alu.ll"
	.text
	.globl	add                             # -- Begin function add
	.p2align	2
	.type	add,@function
add:                                    # @add
	.cfi_startproc
# %bb.0:
	add	a0, a0, a1
	ret
.Lfunc_end0:
	.size	add, .Lfunc_end0-add
```

同一个 IR，RISC-V 打印 ABI 寄存器名并输出 `.attribute`；MiniRV 打印 `x` 名字且没有 `.attribute`。这不是 Triple 的差异，是 **MC 层注册内容**的差异（Ch 07/08）。但两者 DataLayout 相同——这正是“身份可以不同，形状可以共用”的例子。

### 3. `llvm-mc -triple=minirv64`

```console
$ echo 'add x10, x11, x12' | $B/llvm-mc -triple=minirv64 -show-encoding
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]

$ echo 'add x10, x11, x12' | $B/llvm-mc -triple=riscv64 -mattr=+m -show-encoding
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
```

编码字节完全相同，寄存器名不同。`llvm-mc` 只需要 TargetInfo + TargetMC + AsmParser + Disassembler，不需要 DataLayout——所以 33d9ec8 可以不碰 `TargetDataLayout.cpp`。

### 4. `llc -march=minirv64`（另一条查找路径）

```console
$ $B/llc -march=minirv64 -mtriple=x86_64-unknown-linux-gnu -O2 $T/llvm/test/CodeGen/MiniRV/alu.ll -o - | sed -n '1,9p'
	.file	"alu.ll"
	.text
	.globl	add                             # -- Begin function add
	.type	add,@function
add:                                    # @add
	.cfi_startproc
# %bb.0:
	add	x10, x10, x11
	jalr	x0, 0(x1)
```

`-march` 走的是 `TargetRegistry::lookupTarget(ArchName, ...)`：先按 `Target::Name` 找，再用 `getArchTypeForLLVMName()` 把 triple 的 arch 改掉（`llvm/lib/MC/TargetRegistry.cpp`）。所以即使 `-mtriple` 写的是 x86_64，最终也变成 MiniRV。这条路径漏掉第 5 项就会失败，而 `-mtriple` 路径仍然正常——这是“只测一种调用方式”的典型盲区。

### 5. `llvm-readobj -h` 与回读的不对称

下面是节选输出：

```console
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj $T/llvm/test/CodeGen/MiniRV/alu.ll -o /tmp/minirv-alu.o
$ $B/llvm-readobj -h /tmp/minirv-alu.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
...
  Machine: EM_RISCV (0xF3)
```

对象文件里**没有** `minirv64` 这个身份。ELF header 只有 `EM_RISCV`，回读时 `riscv64` 是唯一能映射出来的 arch。后果很实际：

```console
$ $B/llvm-objdump -d /tmp/minirv-alu.o | sed -n '6,12p'
0000000000000000 <add>:
       0: 00b50533     	add	a0, a0, a1
       4: 00008067     	ret

0000000000000008 <sub>:
       8: 40b50533     	sub	a0, a0, a1
       c: 00008067     	ret

$ $B/llvm-objdump -d --triple=minirv64 /tmp/minirv-alu.o | sed -n '6,12p'
0000000000000000 <add>:
       0: 33 05 b5 00  	add	x10, x10, x11
       4: 67 80 00 00  	jalr	x0, 0(x1)

0000000000000008 <sub>:
       8: 33 05 b5 40  	sub	x10, x10, x11
       c: 67 80 00 00  	jalr	x0, 0(x1)
```

同一份字节，默认按 RISC-V 反汇编，只有显式 `--triple=minirv64` 才用 MiniRV 的 printer。MiniRV 的对象能被 RISC-V 工具链“看懂”，是因为它故意复用了 RV64I 编码。这是便利，也是陷阱：**你能链接不等于别人知道你是谁**。

## 反直觉与踩雷

1. **错误直觉**：Triple 里加了 `minirv64`，对象文件就会记录 `minirv64`。
   → **正确认识**：ELF 只记录 `EM_RISCV` 和 class/endianness。`llvm-readobj` 回读成 `riscv64`，`llvm-objdump` 默认也用 RISC-V 反汇编。要让 MiniRV 的 printer 生效，必须显式给 `--triple=minirv64`。

2. **错误直觉**：漏了 `isLittleEndian` 会编译报错。
   → **正确认识**：不会。`isLittleEndian` 的 `default` 返回 `false`，于是 `computeRISCVDataLayout` 会拼出 `E-m:e-...`，DataLayout 与三地址代码的常量布局全部反了。这是最典型的“测试全绿但 ABI 错”。

3. **错误直觉**：Triple 改完就能 `-filetype=obj`，MC-only 阶段与 `getDefaultFormat` 无关。
   → **正确认识**：`MCContext` 构造时就会调用 `TheTriple.getObjectFormat()`（`llvm/lib/MC/MCContext.cpp:85`），漏掉 `getDefaultFormat` 连 `llvm-mc` 都起不来；它在 `getObjectFormat()` 第一次被调用时触发 `llvm_unreachable("unknown architecture")`。不是只有 `-filetype=obj` 才炸。

4. **错误直觉**：MC-only target 不需要 DataLayout，所以 DataLayout 永远不用改。
   → **正确认识**：33d9ec8 时确实不需要；ad74868 一加 `TargetMachine`，`CodeGenTargetMachineImpl` 构造函数就无条件调用 `computeDataLayout()`。DataLayout 的“需要时刻”和 TargetMachine 的“出现时刻”是同一个提交。

5. **错误直觉**：`isMiniRV()` 只是给业务代码用的方便谓词。
   → **正确认识**：它是 `getDefaultExceptionHandling()` 的分派条件。漏了谓词，arch 仍然能解析、能出汇编，但 `TargetMachine` 不声明支持 `DwarfCFI`，带 invoke 的 IR 会被拒绝或走错异常模型。

6. **错误直觉**：`-mtriple` 和 `-march` 是同一条查找路径。
   → **正确认识**：`-mtriple` 走 `parseArch`；`-march` 走 `getArchTypeForLLVMName`。只补一个，另一个仍然坏。上面第 4 条实测就是专门验证后者的。

## 进阶：再往深一层

### 什么时候不该加 ArchType

加 arch 的代价不只是这 12 个 hunk：所有下游（Clang driver、LLD、`llvm-objdump`、调试器、binutils 兼容层）都要知道这个名字。很多团队因此选择**复用已有 triple**，用 vendor / subarch / ABI / environment 区分自己：

| 复用方式 | 真实例子 | 怎么区分 | 代价 |
|---|---|---|---|
| environment | `x86_64-unknown-linux-gnux32` | `Triple::isX32()`，arch 仍是 `x86_64` | 不能改指针宽度以外的布局；工具链要认 environment |
| ABI name | `riscv64` + `-mabi=l64pc128`（CHERI） | `computeRISCVDataLayout` 里 `IsPureCapABI` 拼 `pe200:128:128:128:64` | 需要 ABI 层配合，不能新增 ELF machine |
| vendor | `riscv64-sifive-elf` 一类 | vendor 字段 + `-mcpu`/features | 只能影响选择与调优，不能改变 ISA 身份 |
| OS | `aarch64-apple-darwin` vs `aarch64-linux-gnu` | `isOSBinFormatMachO()` 选对象格式 | 只影响对象格式/ABI，不新增 arch |

MiniRV 选择了“新 arch”，原因是教学上要一个**完全独立**的注册链：自己的 `Triple::minirv64`、自己的 `RegisterTarget`、自己的 printer。代价就是本章这 12 个 hunk，以及对象仍带 `EM_RISCV` 的不对称。你要在自己的产品里做同样决定时，先问一句：**我是要一个新的 ISA 身份，还是只是现有 ISA 的一个变体？** 后者用 vendor/ABI 往往便宜一个数量级。

### DataLayout 与 Triple 是两个真值源

`Triple::isLittleEndian()` 和 DataLayout 里的 `e` 是两份独立信息。IR 的布局以 module 的 `target datalayout` 为准；Triple 的端序只影响“生成默认 layout 时用哪个字母”。如果 IR 自带一个和 triple 矛盾的 datalayout，LLVM **不会**报错。写测试时值得加一条：`llc -mtriple=minirv64` 的默认输出必须以 `e-m:e-` 开头。

## 本章重点整理

- 一个 arch 名字在 LLVM 里是 `Triple::ArchType` 枚举，不是可注册字符串；新增它必须穷举 `Triple.cpp` 的 10 个函数（9 个 switch + `getDefaultExceptionHandling` 的谓词 if 链）。
- 12 个 hunk 里，3 个是 assert 型失败（`getArchTypeName`/`getDefaultFormat`/`getArchPointerBitWidth`），4 个是**静默错误默认值**（`isLittleEndian`、`get64BitArchVariant`、异常模型、`long double`），后者最危险。
- `getDefaultExceptionHandling` 用谓词 `isMiniRV()` 分派，不补谓词不会编译失败。
- DataLayout 不是 Triple 提交的一部分：33d9ec8 没改，ad74868 加 `TargetMachine` 时才必须补 `computeDataLayout` 的 case。
- MiniRV 的 layout `e-m:e-p:64:64-i64:64-i128:128-n32:64-S128` 与 `riscv64` 完全相同，因为共用 `computeRISCVDataLayout`；`S128` 对应 psABI 的 16 字节栈对齐。
- 对象文件只记录 `EM_RISCV`；`llvm-readobj` 回读成 `riscv64`，`llvm-objdump` 必须显式 `--triple=minirv64` 才用 MiniRV printer。
- 加新 arch 的替代方案是复用 triple + vendor/subarch/ABI；先判断你需要的是“新身份”还是“现有 ISA 的变体”。

## 自我检核

- [ ] 不看源码，说出 `Triple.cpp` 里至少 8 个必须补 `case` 的函数，并指出哪几个漏了是静默错误。
- [ ] 解释为什么 `33d9ec8` 可以不改 `TargetDataLayout.cpp`，而 `ad74868` 必须改；说出触发调用的那个构造函数。
- [ ] 给定 DataLayout `e-m:e-p:64:64-i64:64-i128:128-n32:64-S128`，逐字段解释；并说明如果漏掉 `S128`，哪类代码最先出问题。
- [ ] 解释 `llc -mtriple=minirv64` 与 `llc -march=minirv64` 分别走哪条查找路径，各自依赖哪个函数。
- [ ] 为什么 `llvm-readobj -h` 显示 `Arch: riscv64` 而不是 `minirv64`？这对 `llvm-objdump` 的使用方式有什么影响？
- [ ] 用一个具体例子说明“复用 triple + vendor/ABI”比新增 arch 便宜在哪里。

## 延伸阅读

- **[LLVM Language Reference: Data Layout](https://llvm.org/docs/LangRef.html#data-layout)** — LLVM 官方
  - **读哪里**：`Data Layout` 一节，特别是 `p`、`i`、`n`、`S`、`m` 的语法与“未指定时的默认规则”。
  - **能学到什么**：本章 4.5 只讲了 MiniRV 用到的 7 个字段；LangRef 能补齐 `a`、`f`、`v`、`ni`、地址空间指针的完整语义。
  - **前提**：读完本章 4.5，最好手边有一份 `llc -stop-after=finalize-isel` 的输出对照。

- **[llvm/include/llvm/TargetParser/Triple.h](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/TargetParser/Triple.h)** — LLVM 上游源码
  - **读哪里**：`enum ArchType` 全体，以及 `isRISCV()` / `isMiniRV()` 这类谓词的命名规律。
  - **能学到什么**：为什么 Triple 的接口是“一堆谓词 + 一堆 getter”，以及新增 arch 时哪些谓词是“必须成对”的。
  - **前提**：能读 C++ 枚举与 `switch`；不需要读懂整个 Triple 的构造流程。

- **[Writing an LLVM Backend](https://llvm.org/docs/WritingAnLLVMBackend.html)** — LLVM 官方（内容偏旧）
  - **读哪里**：`Target Machine` 与 `Register a Target` 两节；把 Triple 相关段落当“历史背景”读，不要照抄。
  - **能学到什么**：官方最小 target 的骨架，以及它**省略了**本章列的哪些 switch——这正好训练你补全的能力。
  - **前提**：已读 Ch 03，知道 MC-only 与 CodeGen 的分界。

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 社区
  - **读哪里**：`Data Layout` 与 `ELF Object Files` 两节，重点看栈对齐、`long double`、`e_flags`。
  - **能学到什么**：为什么是 `S128`、`i128:128`、`IEEEquad`；这些不是 LLVM 拍脑袋，是 ABI 文档规定的。
  - **前提**：无；Ch 02 的 RV64 ABI 小结可以当速查。

→ [Ch 05 构建系统与 TargetRegistry](./05-build-and-target-registry.md)
