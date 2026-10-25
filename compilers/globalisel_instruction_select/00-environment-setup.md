# Ch 0 — 环境搭建与最小实验

> **目标**：在 WSL2 上构建一份带 assertions 的 LLVM 23.1.4，验证 `llc` / `llvm-tblgen` / `llvm-lit`，并用一个 `add` 函数亲手看到 GlobalISel 四个阶段的 MIR 变化；最后制造一次 `InstructionSelect` 失败，确认 fallback 不是“看起来成功”。

> **环境**：LLVM 23.1.4，WSL2 Ubuntu 24.04 x86_64，Ninja + ccache，主 target AArch64，RISC-V 对照。

> 本章的 `llc` 输出在 LLVM 23.1.4 官方预编译包上实际跑过；build 命令针对 WSL2 源码构建。不同构建方式、是否加 `-simplify-mir` 会让 MIR 文本略有差异，但阶段与关键 opcode 不变。

## 为什么需要这个？

GlobalISel 最危险的失败方式是**安静地 fallback 到 SelectionDAG**。你运行 `llc -O0 -mtriple=aarch64-unknown-linux-gnu foo.ll`，看到正确的 AArch64 汇编，以为 GlobalISel 工作正常；实际上可能是 Legalizer 或 InstructionSelect 失败，function 被标记为 `failedISel`，后面的 SelectionDAG 接手产出了“正确但根本没经过 GlobalISel”的代码。

不打开 abort/diagnostic，这个切换通常不会出现在最终输出里。于是你会花几个小时调一个不存在的 ISel pattern，或者误以为某条指令已经能被 GlobalISel 选择。

这门课后面所有实验都依赖三种能力，必须在 Ch 0 建立：

- **能停在任何阶段看 MIR**：`-stop-after=irtranslator/legalizer/regbankselect/instruction-select`。
- **能让失败显形**：`-global-isel-abort=1` 或 `=2`。
- **能只看一个 pass**：把 MIR 存下来，用 `-run-pass=instruction-select` 重跑。

## 先建立直觉

把 GlobalISel 想成一条冲洗照片的流水线。LLVM IR 是底片，最终汇编是成品；中间四个阶段各产出一张“半成品照片”：

```text
LLVM IR
  │ IRTranslator
  ▼
gMIR（G_* opcode，虚拟寄存器还没有 register bank）
  │ Legalizer
  ▼
legal gMIR（操作/类型都是 target 能处理的形态）
  │ RegBankSelect
  ▼
banked gMIR（每个虚拟寄存器有 bank，但还没有具体寄存器类）
  │ InstructionSelect
  ▼
MIR（target-specific opcode，例如 ADDXrr）
  │ RA / Scheduler / MC
  ▼
AArch64 assembly
```

三个契约点后面会反复引用：

- `Legalizer` 之后不能残留 illegal operation。
- `RegBankSelect` 之后每个虚拟寄存器必须有 register bank。
- `InstructionSelect` 之后不能残留 pre-ISel generic opcode（`G_*`）。

`InstructionSelect` 只负责最后一段：把 banked gMIR 变成 target MIR。它不该再处理“类型太大”“target 不支持这个操作”之类的问题——那些应该已经被上游解决。判断失败该怪 legalizer、regbankselect 还是 selector，靠的就是这条边界。

## 选环境

**优先 WSL2 Ubuntu 24.04**：与 LLVM 官方文档/CI 最接近，CMake/Ninja/lit 路径顺。原生 Windows + MSVC 可以构建，但源码构建摩擦大、官方预编译包通常不是 assertions build；Docker 适合团队复现，但调试器配置更麻烦。把源码放在 WSL 的 `~/src`，不要放 `/mnt/c/...`，否则链接和 lit 会因跨文件系统 I/O 明显变慢。

硬件建议：4 核能跑，8 核以上更好；16GB RAM 是舒适线，32GB 更稳；源码约 1.5GB，build 目录通常十几 GB，**预留 30GB**；内存不足时用 `-DLLVM_PARALLEL_LINK_JOBS=2` 限制并行链接。

## 安装依赖

Ubuntu 24.04 自带 GCC 13 / Clang 18，构建 LLVM 23 都够用。下面用 Clang 作为 host compiler；换 GCC 只需改 `CMAKE_C_COMPILER` / `CMAKE_CXX_COMPILER`。

```bash
sudo apt update
sudo apt install -y \
  build-essential cmake ninja-build ccache git python3 \
  zlib1g-dev libzstd-dev libxml2-dev libedit-dev libncurses-dev \
  lld clang
```

```bash
cmake --version   # 需要 >= 3.20；Ubuntu 24.04 通常是 3.28
ninja --version && clang --version && ccache --version
```

> LLVM 23 的 `CMakeLists.txt` 仍写着 `cmake_minimum_required(VERSION 3.20.0)`，但提示未来会提高到 3.31。Ubuntu 24.04 的 3.28 目前可用；更老的发行版先升级 CMake。

## 取得源码

```bash
mkdir -p ~/src && cd ~/src
git clone --depth 1 --branch llvmorg-23.1.4 \
  https://github.com/llvm/llvm-project.git
cd llvm-project
```

浅克隆足够构建；要追 `git log` / `git blame` 时改用 `--filter=blob:none` 或下载 `llvm-project-23.1.4.src.tar.xz`。本课最常看三处：`llvm/lib/CodeGen/GlobalISel/`（四个 pass）、`llvm/lib/Target/AArch64/GISel/`（selector）、`llvm/lib/Target/AArch64/AArch64InstrGISel.td` 与 `llvm/test/CodeGen/AArch64/GlobalISel/`（GI pseudo 与测试）。

## 配置 build

```bash
cd ~/src/llvm-project

cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64;RISCV;X86" \
  -DLLVM_CCACHE_BUILD=ON \
  -DLLVM_USE_LINKER=lld \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++
```

| 选项 | 为什么需要 |
|---|---|
| `CMAKE_BUILD_TYPE=Release` | Debug 太慢、太大；Release + assertions 是本课性价比最高的组合 |
| `LLVM_ENABLE_ASSERTIONS=ON` | 打开 assert、`-debug-only` 和诊断能力；没有它很多调试手段不可用 |
| `LLVM_TARGETS_TO_BUILD="AArch64;RISCV;X86"` | AArch64 主線、RISC-V 对照、X86 作为 host/测试补充 |
| `LLVM_CCACHE_BUILD=ON` | 增量重建省时间；CMake 会检查 `ccache` |
| `LLVM_USE_LINKER=lld` | 链接更快；需要先装 `lld` |
| `CMAKE_C_COMPILER=clang` / `CXX=clang++` | 统一 host compiler；用 GCC 也可以 |

需要 C/C++ → IR 时再加 Clang：

```bash
cmake -G Ninja -S llvm -B build -DLLVM_ENABLE_PROJECTS=clang
```

可选：`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` 生成 `compile_commands.json`；`-DLLVM_OPTIMIZED_TABLEGEN=ON` 用 host tablegen 加速，但第一次学不建议开。

## 编译与验证

```bash
ninja -C build llc llvm-tblgen FileCheck not count split-file
```

- `llc`：停 pass、跑 MIR、产汇编；`llvm-tblgen`：生成 `*GenGlobalISel.inc`、`--warn-on-skipped-patterns`。
- `FileCheck` / `not` / `count` / `split-file`：跑 lit 测试所需。
- `llvm-lit` **不是 Ninja target**；它是 configure 阶段生成的脚本，通常在 `build/bin/llvm-lit`。

```bash
build/bin/llc --version
build/bin/llvm-tblgen --help | grep -E 'gen-global-isel|warn-on-skipped'
build/bin/FileCheck --version
build/bin/llvm-lit --version
```

`llc --version` 的 `Registered Targets` 必须包含 `aarch64` 和 `riscv64`；`llvm-tblgen --help` 必须能看到 `--gen-global-isel`。否则先回去检查 `LLVM_TARGETS_TO_BUILD` 和 build 是否成功。

## 实验 1：一个 `add` 的四个阶段

新建 `add.ll`：

```llvm
target triple = "aarch64-unknown-linux-gnu"

define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}
```

先看最终汇编：

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 -o - add.ll
```

```asm
add:                                    // @add
	.cfi_startproc
// %bb.0:                               // %entry
	add	x0, x0, x1
	ret
```

这条 `add x0, x0, x1` 经过了四个阶段。逐个 dump：

### 阶段 1：IRTranslator

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 \
  -stop-after=irtranslator -simplify-mir -o - add.ll
```

```mir
body:             |
  bb.1.entry:
    liveins: $x0, $x1

    %0:_(i64) = COPY $x0
    %1:_(i64) = COPY $x1
    %2:_(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

此时 `%0/%1/%2` 是虚拟寄存器，类型是 LLT `i64`；寄存器类是 `_`（未分配）；操作是 generic `G_ADD`，不是 `ADDXrr`。`RET_ReallyLR` 是 AArch64 的返回 pseudo，Ch 20 会解释。

### 阶段 2：Legalizer

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 \
  -stop-after=legalizer -simplify-mir -o - add.ll
```

body 几乎一样，但多了：

```mir
legalized:       true
```

Legalizer 的契约不是“把每条指令改写成 target 指令”，而是“确保剩下的 generic 操作/类型都是 target 能合法处理的”。简单 i64 `G_ADD` 在 AArch64 上本来就 legal，所以 body 不变。

### 阶段 3：RegBankSelect

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 \
  -stop-after=regbankselect -simplify-mir -o - add.ll
```

关键变化：

```mir
regBankSelected: true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }

%0:gpr(i64) = COPY $x0
%1:gpr(i64) = COPY $x1
%2:gpr(i64) = G_ADD %0, %1
```

`gpr` 是 **register bank**，不是最终寄存器类。RegBankSelect 只决定“走通用寄存器 bank 还是浮点/向量 bank”，以及 operand mapping；`gpr32`/`gpr64` 的具体类要等 InstructionSelect/后续阶段。

### 阶段 4：InstructionSelect

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 \
  -stop-after=instruction-select -simplify-mir -o - add.ll
```

关键变化：

```mir
regBankSelected: true
selected:        true
registers:
  - { id: 0, class: gpr64 }
  - { id: 1, class: gpr64 }
  - { id: 2, class: gpr64 }

%0:gpr64 = COPY $x0
%1:gpr64 = COPY $x1
%2:gpr64 = ADDXrr %0, %1
```

三件事同时发生：`G_ADD` → `ADDXrr`；虚拟寄存器类从 `gpr` 收紧为 `gpr64`；`selected: true`，表示不应再有 pre-ISel generic opcode。

| 阶段 | `-stop-after=` | 关键属性 | `%2` 的形态 |
|---|---|---|---|
| IRTranslator | `irtranslator` | `isSSA: true` | `%2:_(i64) = G_ADD` |
| Legalizer | `legalizer` | `legalized: true` | `%2:_(i64) = G_ADD` |
| RegBankSelect | `regbankselect` | `regBankSelected: true` | `%2:gpr(i64) = G_ADD` |
| InstructionSelect | `instruction-select` | `selected: true` | `%2:gpr64 = ADDXrr` |

之后读每一章源码时，都先做这个对照实验：看 MIR 在哪一阶段变了，再去看对应的 pass/selector。

## 实验 2：把 MIR 存下来，只跑 InstructionSelect

```bash
build/bin/llc -global-isel -mtriple=aarch64-unknown-linux-gnu -O0 \
  -stop-after=regbankselect -simplify-mir \
  -o add.regbank.mir add.ll

build/bin/llc -run-pass=instruction-select -simplify-mir \
  -o - add.regbank.mir
```

输出应包含：

```mir
regBankSelected: true
selected:        true

...
%2:gpr64 = ADDXrr %0, %1
```

这是本课最常用的工作流：

```text
IR → 停在某阶段 → 改 MIR / 改源码 → 只重跑一个 pass → 看差异
```

它比“改 C++ → 全量编译 → 看最终汇编”快一个数量级。Ch 16 会把它变成 lit 测试，Ch 17 会加上 debug 输出。

## 实验 3：制造一次 InstructionSelect 失败

`+strict-align` 下的 unaligned i64 load 是一个现成例子：

```llvm
target triple = "aarch64-unknown-linux-gnu"

define i64 @strict_align(ptr %p) #0 {
  %x = load i64, ptr %p, align 1
  ret i64 %x
}

attributes #0 = { "target-features"="+strict-align" }
```

默认行为（`abort=0`）会安静 fallback：

```bash
build/bin/llc -global-isel -global-isel-abort=0 \
  -mtriple=aarch64-unknown-linux-gnu -O0 -o - strict.ll
```

它能产出正确汇编（用一串 `ldrb` 拼出 i64），但你看不到“GlobalISel 失败了”。成功的是 SelectionDAG，不是 GlobalISel。

让失败变成硬错误：

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=aarch64-unknown-linux-gnu -O0 -o - strict.ll
```

实际输出（节选）：

```text
LLVM ERROR: cannot select: RET_ReallyLR implicit $x0 (in function: strict_align)
...
2.	Running pass 'InstructionSelect' on function '@strict_align'
```

失败发生在 **InstructionSelect**。错误说 `cannot select: RET_ReallyLR`，而不是直接说“load 不支持”；因为 unaligned load 被 lower 成一串操作后，最终在返回/选择阶段暴露。Ch 18 会教你从这种间接错误倒推根因。

Fallback 但留下诊断：

```bash
build/bin/llc -global-isel -global-isel-abort=2 \
  -pass-remarks-missed='gisel*' \
  -mtriple=aarch64-unknown-linux-gnu -O0 -o - strict.ll 2>&1 | head
```

```text
remark: <unknown>:0:0: cannot select: RET_ReallyLR implicit $x0 (in function: strict_align)
warning: Instruction selection used fallback path for strict_align
```

| `-global-isel-abort=` | 行为 | 用途 |
|---|---|---|
| `0` | 静默 fallback | 生产/兼容性优先；**不要用它做 GlobalISel 验证** |
| `1` | 失败即 `report_fatal_error` | 写 lit 测试、确认“是否真的全走 GlobalISel” |
| `2` | 不 abort，但输出 remark/warning | 日常开发、扫大范围测试、找 fallback 原因 |

> 这三个值来自 `TargetPassConfig.cpp` 的 `EnableGlobalISelAbort`；`isGlobalISelAbortEnabled()` 只有在 `=1` 时为真。Ch 10 会回到这段源码。

## 版本与默认 selector 的检查表

LLVM 23.1.4 的 AArch64 由 `-aarch64-enable-global-isel-at-O` 控制，默认值是 `0`：

- `-O0`：默认走 GlobalISel（但 `GlobalISelAbort` 默认是 `Disable`，失败会静默 fallback）。
- `-O1` 及以上：默认走 SelectionDAG；要显式加 `-global-isel`，或把 `-aarch64-enable-global-isel-at-O` 调高。

```bash
# O0 默认就会跑 InstructionSelect
build/bin/llc -O0 -mtriple=aarch64-unknown-linux-gnu \
  -stop-after=instruction-select -simplify-mir -o - add.ll

# 不显式 -global-isel 的 O1：不要用它判断 GISel 默认开启
build/bin/llc -O1 -mtriple=aarch64-unknown-linux-gnu \
  -stop-after=instruction-select -simplify-mir -o - add.ll
```

第二种情况你会看到 `failedISel: true` 或没有 body。做本课实验时统一显式写 `-global-isel`，避免默认策略变化干扰。

`-stop-after=` / `-run-pass=` 使用的是 pass 注册名，不是文件名。当前常用：`irtranslator`、`legalizer`、`regbankselect`、`instruction-select`。报 `pass is not registered` 时先跑 `build/bin/llc --help-hidden | grep -E 'stop-after|run-pass'`。LLVM 23.1.4 的四个关键路径：AArch64 selector 在 `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`；GI pseudo 在 `AArch64InstrGISel.td`；生成表是 build 目录的 `AArch64GenGlobalISel.inc`；matcher 执行器在 `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h`。

换版本先 `rg --files | rg 'InstructionSelector|GenGlobalISel'`，不要照抄旧博客路径。

RISC-V 的基本 i64 `add` 在 LLVM 23.1.4 上可以跑通：

```bash
build/bin/llc -global-isel -global-isel-abort=1 \
  -mtriple=riscv64-unknown-linux-gnu -O0 -o - add.ll
```

输出是 `add a0, a0, a1` / `ret`。但 RISC-V 覆盖范围比 AArch64 小，很多 case 会 fallback；Ch 22 会系统对照。

选型原则：**先保证失败可见（assertions + abort=1），再追求构建速度**。Release + assertions 适合本课日常开发；RelWithDebInfo 适合源码级断点；Debug 适合深挖 LLVM 内部但极慢；官方预编译包只适合快速跑 `llc`/看汇编。

## 踩雷集锦

1. **“汇编对了，所以 GlobalISel 对了”**：默认 `abort=0` 会静默 fallback。验证时至少加 `-global-isel-abort=1`，或 `=2` 看 warning。
2. **只编 `llc`，不编 `llvm-tblgen`/`FileCheck`**：Ch 5 要读生成表，Ch 16 要跑 lit；建议一开始就编 `llc llvm-tblgen FileCheck not count split-file`。
3. **用 Release 包找 `-debug-only`**：该选项依赖 assertions；报 `Unknown command line argument` 不是命令拼错。换源码 assertions build。
4. **以为 `-O1` 也默认 GlobalISel**：LLVM 23.1.4 的 AArch64 默认只在 `-O0` 开；`-O1` 要显式 `-global-isel`。实验时统一显式写。
5. **把 `-stop-after=instruction-select` 当成“一定会跑 ISel”**：如果 pipeline 没走 GlobalISel，这个 stop point 可能只留下 `failedISel: true` 或空 body。先确认 `-global-isel` 和 `failedISel`。
6. **在 `/mnt/c` 下 build 或忽略 `-simplify-mir`**：前者让链接/lit 变慢；后者让 MIR 带大量调试信息。源码放 `~/src`，对照时加 `-simplify-mir`（只影响打印，不改变 pass 行为）。

## 进阶：让环境更好用

```bash
ccache -s                                  # 看命中率
cmake -G Ninja -S llvm -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake -G Ninja -S llvm -B build -DLLVM_PARALLEL_LINK_JOBS=2  # 内存不足时
build/bin/llvm-lit -v llvm/test/CodeGen/AArch64/GlobalISel/arm64-fallback.ll
```

## 动手练习

1. **跑通四阶段**：用 `add.ll` 生成 `irtranslator` / `legalizer` / `regbankselect` / `instruction-select` 四份 MIR，找出 `G_ADD` 在哪一步变成 `ADDXrr`、`gpr` 在哪一步变成 `gpr64`。
2. **只跑 ISel**：把 `regbankselect` 后的 MIR 存成文件，用 `-run-pass=instruction-select` 重跑；确认输出和 `-stop-after=instruction-select` 一致。
3. **制造失败**：把 `add.ll` 的 `add` 改成 `sub`，观察 MIR 与汇编如何变化；再用 `strict.ll` 跑 `abort=0/1/2`，记录三种行为的差异。
4. **换 target**：把 `add.ll` 的 triple 改成 `riscv64-unknown-linux-gnu`，对比 AArch64 与 RISC-V 的 `selected` MIR。

## 本章重点整理

- GlobalISel 的失败常常是**静默 fallback**；验证时必须用 `-global-isel-abort=1` 或 `=2`。
- 四个阶段各有硬契约：legalized → regBankSelected → selected；`InstructionSelect` 之后不能有 `G_*`。
- `-stop-after` 看阶段状态，`-run-pass` 隔离单个 pass；这是后续所有实验的基本循环。
- AArch64 在 LLVM 23.1.4 的 `-O0` 默认走 GlobalISel，但 fallback 默认关闭 abort；`-O1` 要显式 `-global-isel`。
- `-debug-only` / `-stats` 依赖 assertions build；官方预编译包通常不满足。

## 自我检核

- [ ] 能解释为什么“汇编正确”不等于“GlobalISel 选择成功”
- [ ] 能不看笔记说出 `-global-isel-abort=0/1/2` 的差异
- [ ] 能从 MIR 的 `legalized` / `regBankSelected` / `selected` 判断当前阶段
- [ ] 知道 `gpr`（bank）与 `gpr64`（register class）不是同一个东西
- [ ] 知道 AArch64 在 `-O0` 与 `-O1` 的默认 selector 差异
- [ ] 能说出至少三个源码路径：`InstructionSelect.cpp`、`GIMatchTableExecutor.h`、`AArch64InstructionSelector.cpp`

## 延伸阅读

- **[LLVM Getting Started](https://llvm.org/docs/GettingStarted.html)**
  - 读 “Requirements” 与 “Configure and build LLVM and Clang” 两节；本章的 CMake 选项都能在这里找到官方解释。遇到 build 失败先回这里。
- **[LLVM CMake 文档](https://llvm.org/docs/CMake.html)**
  - 读 `LLVM_ENABLE_ASSERTIONS`、`LLVM_TARGETS_TO_BUILD`、`LLVM_USE_LINKER`、`LLVM_CCACHE_BUILD` 的说明；本章选项表的权威来源。
- **[GlobalISel Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - 读 “Core Pipeline” 与 “Testing/Debugging” 两节；本章的四阶段 MIR 对照就是这条 pipeline 的最小实例。Ch 2 会逐条展开契约。
- **[llvm-lit 文档](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读 “RUN lines” 与 “FileCheck” 相关部分；Ch 16 写测试前先熟悉 `-v`、`--filter` 和 `%s` 的用法。
- **[LLVM 源码：InstructionSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp)**
  - 读 `selectMachineFunction()` 的遍历顺序与 `failedISel` 设置；本章的 `failedISel: true` 就来自这里。

## 下一章

[Ch 1 — GlobalISel 全景与 ISel 的位置](./01-globalisel-overview.md)
