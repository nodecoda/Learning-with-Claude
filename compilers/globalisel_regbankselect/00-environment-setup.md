# Ch 0 — 环境搭建与最小 RegBankSelect 实验

> **目标**：把 RegBankSelect 的实验环境搭起来；用三个最小例子亲眼看到 vreg 被标成 `gpr` / `fpr`、跨 bank COPY 如何出现，以及 `optnone` 如何覆盖 `-regbankselect-greedy`。学完你能独立回答“这个值为什么在这个 bank”“这几条 COPY 从哪来”。
>
> **环境**：LLVM 23.1.4，Windows 10/11 x86_64 官方预编译包（实测）；WSL2 Ubuntu 24.04 或 Windows MSVC 源码构建（guided）。主 target AArch64，RISC-V 对照。
>
> **证据约定**：本章的 MIR 片段都在官方 23.1.4 包上实际跑过；源码构建命令标 guided，不伪造 build 输出。

## 为什么需要这个？

RegBankSelect 位于 Legalizer 和 InstructionSelect 之间。它的输出不是最终汇编，而是一份“每个虚拟寄存器都带 bank”的 gMIR：

```text
LLVM IR
  │ IRTranslator
  ▼
gMIR（G_* opcode，vreg 还没有 bank）
  │ Legalizer
  ▼
legal gMIR（操作和类型都合法）
  │ RegBankSelect
  ▼
banked gMIR（vreg 有 bank，但还没有具体寄存器类）
  │ InstructionSelect
  ▼
target MIR（ADDXrr、FADDSrr……）
```

这份中间状态很容易被忽略，因为 `llc` 默认只给你最终汇编。汇编看起来正确，不代表 RegBankSelect 做了正确的选择：它可能插入了多余的 COPY，可能把值放进了错误的 bank，也可能在失败后悄悄 fallback 到 SelectionDAG。只看汇编，这三种情况都可能长得一样。

这门课后面所有实验都依赖三种能力，必须在 Ch 0 建立：

- **能停在 RegBankSelect 之后看 MIR**：`-stop-after=regbankselect`。
- **能只重跑 RegBankSelect**：把 legalized MIR 存下来，用 `-run-pass=regbankselect`。
- **能让失败显形**：`-global-isel-abort=1`、`-verify-machineinstrs`。

官方预编译包足够做这三件事。只有当你需要 `-debug-only=regbankselect`、改源码、跑 `llvm-lit` 时，才需要自己构建。

## 先建立直觉

把寄存器 bank 想成机场的航站楼：

- **vreg** 是旅客。
- **bank** 是航站楼（AArch64 有 GPR、FPR、CC 三座）。
- **指令** 是航班，只接受从特定航站楼出发的旅客。
- **跨 bank COPY** 是航站楼之间的摆渡车，要花时间。

RegBankSelect 要做的，是给每个旅客分配航站楼，让整条行程的摆渡车成本尽量低。但它只看“当前这一条指令”和它的操作数，不做全局搜索——这就是 Fast/Greedy 都只能做局部决定的原因。

```text
        ┌───────────────┐
        │  legal gMIR   │
        │  %0:_(i64)    │
        │  %1:_(i64)    │
        │  %2:_(i64)    │
        └───────┬───────┘
                │ RegBankSelect
                ▼
        ┌───────────────┐
        │  banked gMIR  │
        │  %0:gpr(i64)  │
        │  %1:gpr(i64)  │
        │  %2:gpr(i64)  │
        └───────┬───────┘
                │ InstructionSelect
                ▼
        ┌───────────────┐
        │  target MIR   │
        │  ADDXrr ...   │
        └───────────────┘
```

> 如果你对 GlobalISel 五阶段还不熟，先回看 [`globalisel_instruction_select`](../globalisel_instruction_select/README.md) 的 Ch 1–4。本课假设你知道“有这五个阶段”，但不需要你会写 selector。

## 两条实验轨道

官方预编译包和源码构建不是互相替代，而是分工不同：

| 能力 | 官方 23.1.4 预编译包 | 源码 assertions build |
| --- | --- | --- |
| `llc` / `opt` / `llvm-mc` / `llvm-tblgen` | 有 | 有 |
| `-stop-after=regbankselect` | 有 | 有 |
| `-run-pass=regbankselect` | 有 | 有 |
| `-verify-machineinstrs` | 有 | 有 |
| `-debug-only=regbankselect` | **没有** | 有 |
| `FileCheck` / `llvm-lit` | **没有** | 有 |
| 改 `RegisterBankInfo` 并重编译 | 不可能 | 可以 |
| 适合章节 | Ch 0–19 的 MIR 实验 | Ch 20–23、final、debug |

官方包是 **Optimized build**。`--version` 会直接写出来：

```console
$ $llc --version
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
  Default target: x86_64-pc-windows-msvc
  Host CPU: skylake
```

没有 `-debug-only` 不是安装坏了，而是 release 构建把 `LLVM_DEBUG` 编译掉了。Ch 20 之后需要看 pass 内部决策时，再切到源码构建。

## 验证官方包

先确认工具存在，并确认本课要用的 flag 都在：

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
& $llc --version
& $llc --help-hidden | Select-String -Pattern `
  "global-isel|regbankselect|stop-after|run-pass|verify-machineinstrs"
```

在 23.1.4 上，你应该能看到：

```text
--global-isel
--global-isel-abort=<value>
--regbankselect-fast
--regbankselect-greedy
--run-pass=<pass-name>
--stop-after=<pass-name>
--verify-machineinstrs
```

`--regbankselect-fast` 的说明是 “Run the Fast mode (default mapping)”，`--regbankselect-greedy` 是 “Use the Greedy mode (best local mapping)”。注意：**帮助文本没有说默认是哪一个**；默认值要看构造函数，Ch 8 会回到源码确认。

## 取得源码

即使 Ch 0 不编译，也建议先把源码 checkout 好。后面每一章都会引用具体文件；没有源码，很多“为什么”只能靠猜。

```powershell
git clone --depth 1 --branch llvmorg-23.1.4 `
  --filter=blob:none --sparse `
  https://github.com/llvm/llvm-project.git .scratch\llvm-23.1.4-src

git -C .scratch\llvm-23.1.4-src sparse-checkout set `
  llvm/include/llvm/CodeGen `
  llvm/lib/CodeGen/GlobalISel `
  llvm/lib/Target/AArch64 `
  llvm/lib/Target/RISCV `
  llvm/test/CodeGen/AArch64/GlobalISel `
  llvm/test/CodeGen/RISCV/GlobalISel `
  llvm/docs/GlobalISel `
  llvm/utils
```

本课最常打开这几个位置：

```text
llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp
llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h
llvm/lib/CodeGen/RegisterBankInfo.cpp
llvm/include/llvm/CodeGen/RegisterBankInfo.h
llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp
llvm/lib/Target/AArch64/AArch64RegisterBanks.td
llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def
```

`--filter=blob:none --sparse` 只下载你 checkout 的目录的 blob；比完整 clone 快很多，当前快照仍可 `git blame`。要完整历史，去掉 `--depth 1` 再 clone。源码约 1.5GB，build 目录通常十几 GB，磁盘至少预留 30GB。

## 实验一：整数与浮点落在不同 bank

先写一个最小 IR：

```llvm
; ch0/add.ll
define i64 @add(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}

define float @fadd(float %a, float %b) {
  %r = fadd float %a, %b
  ret float %r
}
```

跑：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  ch0\add.ll -o ch0\add-rbs.mir
```

打开 `add-rbs.mir`，先看 `add` 的属性：

```yaml
legalized:       true
regBankSelected: true
selected:        false
```

再看寄存器表：

```yaml
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
```

最后看指令：

```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

`fadd` 同理，但 bank 变成 `fpr`，物理寄存器变成 `$s0` / `$s1`：

```yaml
registers:
  - { id: 0, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: fpr, preferred-register: '', flags: [  ] }

body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(f32) = G_FADD %0, %1
    $s0 = COPY %2(f32)
    RET_ReallyLR implicit $s0
```

这里要区分两类 COPY：

- `%0:gpr(i64) = COPY $x0` 是 **ABI 边界**：物理寄存器 `$x0` 里的参数进入 vreg 世界。它不是 RegBankSelect 为了修复 bank 而插入的。
- `G_ADD` 的输入输出都在 `gpr`，没有跨 bank 修复，所以这里没有额外的 `%tmp:... = COPY ...`。

对 `i64 add` 和 `float fadd`，Fast 和 Greedy 会给出相同结果，因为两条指令都没有 alternative mapping，bank 是被操作/类型唯一决定的。

## 实验二：同一个 G_OR，Fast 与 Greedy 差三条 COPY

这个例子会贯穿 Ch 8、Ch 9、Ch 17。两个 float 先 bitcast 成 i32，做按位 OR，再 bitcast 回 float：

```llvm
; ch0/or.ll
define float @or_f(float %a, float %b) {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
```

先跑默认模式（在 23.1.4 里就是 Fast）：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  ch0\or.ll -o ch0\or-fast.mir
```

得到：

```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %6:gpr(i32) = COPY %2(i32)
    %7:gpr(i32) = COPY %3(i32)
    %4:gpr(i32) = G_OR %6, %7
    %5:gpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
```

再跑 Greedy：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  ch0\or.ll -o ch0\or-greedy.mir
```

得到：

```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %4:fpr(i32) = G_OR %2, %3
    %5:fpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
```

数一下跨 bank 的修复 COPY：

| 模式 | `G_OR` 所在 bank | 跨 bank COPY | 说明 |
| --- | --- | --- | --- |
| Fast | `gpr` | 3 条 | 两条 FPR→GPR 喂操作数，一条 GPR→FPR 回返回值 |
| Greedy | `fpr` | 0 条 | 整条链都在 FPR，bitcast 不需要搬 |

为什么 Greedy 能这样选？因为 AArch64 的 `getInstrAlternativeMappings` 为 `G_OR` 的 32/64 位形式提供了 GPR 和 FPR 两套 mapping。Fast 只取默认的 GPR mapping；Greedy 会把“指令本身代价 + 把操作数搬到目标 bank 的代价”都算一遍，发现留在 FPR 更便宜。

> 这里先记住结论，不要急着背算法。Ch 5 会拆 mapping 数据结构，Ch 9 会拆代价公式，Ch 10 会解释 COPY 到底插在哪里。

## 实验三：`-O0` 不等于 `optnone`

这是本课要修正的一个常见说法：“`-O0` 会强制 RegBankSelect 用 Fast，所以 `-regbankselect-greedy` 无效。”

先给 `or.ll` 加上 `optnone`：

```llvm
; ch0/or-optnone.ll
define float @or_f(float %a, float %b) #0 {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
attributes #0 = { optnone noinline }
```

跑三组：

```powershell
# A. 没有 optnone，-O0 + greedy
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  ch0\or.ll -o ch0\or-o0-greedy.mir

# B. 没有 optnone，-O2 + greedy
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  ch0\or.ll -o ch0\or-greedy.mir

# C. 有 optnone，-O2 + greedy
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  ch0\or-optnone.ll -o ch0\or-optnone-greedy.mir
```

结果是：

- A 和 B 的输出**逐字节相同**（实测 SHA-256 相同）：都是 Greedy，`G_OR` 在 `fpr`，0 条跨 bank COPY。
- C 的输出是 Fast：`G_OR` 在 `gpr`，有 3 条跨 bank COPY。

这说明：

- `-O0` 本身**不会**覆盖 `-regbankselect-greedy`。
- 真正覆盖命令行的是函数上的 `optnone`：`RegBankSelect::runOnMachineFunction` 里有 `if (F.hasOptNone()) OptMode = Mode::Fast;`。
- `clang -O0` 默认会给函数加 `optnone`，所以“clang -O0 下 greedy 无效”这句话在现象上成立；但把它归因于 `-O0` 是错的，归因于 `optnone` 才对。
- `llc -O0` 读的是你给的 IR；如果 IR 没有 `optnone`，greedy 仍然生效。

Ch 8 会从源码逐行确认这个分支，并解释为什么 `optnone` 必须强制 Fast（编译时间与调试体验）。

## 怎么读这份 MIR

后面每章都会反复看这几类信息：

| 看到什么 | 含义 |
| --- | --- |
| `regBankSelected: true` | RegBankSelect 已经跑过；vreg 的 bank 已写入 `MachineRegisterInfo` |
| `selected: false` | InstructionSelect 还没跑；仍是 `G_*` 指令 |
| `class: gpr` / `class: fpr` | 该 vreg 的 bank |
| `%0:gpr(i64)` | vreg 0 的 bank 是 GPR，类型是 `i64` |
| `%0:_(i64)` | 还没有 bank；可能是 legalized MIR，也可能是未被映射的死 vreg |
| `COPY` | 可能是 ABI 边界，也可能是 RegBankSelect 的 repair；看它连接的两个 bank |
| `G_OR` / `G_BITCAST` | 仍是 generic opcode；说明只跑到 RegBankSelect |

一个实用判断：如果一条 `COPY` 的一边是物理寄存器（`$x0`、`$s0`），它通常是 ABI/调用约定边界；如果两边都是 vreg，且 bank 不同（`%6:gpr = COPY %2:fpr`），它很可能是 repair。

## 版本与默认模式检查表

不同 LLVM 版本的行为可能变化。用 23.1.4 以外的版本时，先做这三项检查：

| 检查 | 23.1.4 的答案 | 怎么查 |
| --- | --- | --- |
| RegBankSelect 默认模式 | Fast | `RegBankSelect.h` 的 `RegBankSelect(Mode RunningMode = Fast)`；AArch64 `addRegBankSelect()` 用 `new RegBankSelect()` |
| `optnone` 是否强制 Fast | 是 | `RegBankSelect.cpp` 的 `runOnMachineFunction` |
| AArch64 是否有 `G_OR` alternative | 有（32/64 位） | `AArch64RegisterBankInfo::getInstrAlternativeMappings` |

如果你看到“默认是 Greedy”的说法，先分清它说的是 pass 构造函数、某个 target 的 pass config，还是文档里的推荐配置。Ch 8 会把这三个层面分开。

## 踩雷集锦

1. **PowerShell 写 IR 带 BOM**：Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写 UTF-8 BOM，`llc` 会报 `expected top-level entity`。用 .NET 写无 BOM：
   ```powershell
   [IO.File]::WriteAllText(
     (Join-Path (Get-Location) "ch0\add.ll"),
     $ir,
     (New-Object Text.UTF8Encoding($false)))
   ```
2. **把 `-O0` 当成 `optnone`**：`llc -O0` 不会自动给函数加 `optnone`。先看 IR 有没有 `attributes #0 = { optnone ... }`，再判断 greedy 为什么没生效。
3. **用 `$LASTEXITCODE` 判断 llc 成功**：PowerShell 下 `llc` 的退出码并不可靠，尤其是你用了管道或重定向。看输出文件是否存在、MIR 属性是否正确、有没有 `error:`。
4. **把 `-stop-after` 和 `-run-pass` 混用**：`-stop-after=regbankselect` 从 LLVM IR 开始跑整条 pipeline；`-run-pass=regbankselect` 从 MIR 开始，只跑这一个 pass，要求 MIR 有 `legalized: true`、`regBankSelected: false`。
5. **以为官方包没有 `-debug-only` 就是 pass 没跑**：Optimized build 把 `LLVM_DEBUG` 编译掉了；MIR 里有 `regBankSelected: true` 就说明跑了。
6. **学习时加 `-simplify-mir`**：它会把 `registers:`、`liveins:` 等“看起来啰嗦”的信息省掉，但 bank 细节也跟着难读。Ch 0–19 不要用它。

## 进阶：再往深一层

如果你已经能跑通上面的例子，可以做三件事：

1. **构建 assertions 版本**，打开 `-debug-only=regbankselect`。你会看到 pass 为每条指令打印候选 mapping、repair 位置和代价；这是 Ch 9–10 最直接的证据。
2. **把 legalized MIR 存成 fixture**：
   ```powershell
   & $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
     -stop-after=legalizer ch0\or.ll -o ch0\or-legal.mir
   & $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
     -run-pass=regbankselect ch0\or-legal.mir -o ch0\or-rbs-only.mir
   ```
   两条路径的输出应该一致；如果不同，先检查 MIR 属性与 `-regbankselect-greedy` 是否一致。
3. **用 `-verify-machineinstrs` 跑一遍**：它会在每个 pass 后验证 machine instruction 的合法性。bank 选错、COPY 类型不匹配时，比最终汇编更早报错。

## 动手练习

1. 把 `add.ll` 改成 `i32 sub` 和 `double fmul`，先手写预测：每个 vreg 应该在 `gpr` 还是 `fpr`？再跑 `-stop-after=regbankselect` 验证。
2. 把 `or.ll` 的 `or` 换成 `and`、`xor`，保持 Fast/Greedy 对比。Greedy 还会把结果留在 FPR 吗？为什么？（提示：看 AArch64 的 `getInstrAlternativeMappings` 覆盖了哪些 opcode。）
3. 给 `add.ll` 的 `add` 函数加 `optnone`，分别在 `-O2` 下跑默认与 `-regbankselect-greedy`，确认两者是否相同。
4. 从 `-stop-after=legalizer` 的 MIR 出发，用 `-run-pass=regbankselect` 重跑；再和 `-stop-after=regbankselect` 的输出逐行比较。
5. 数一数 `or.ll` 在 Fast 下有几条跨 bank COPY，逐条说明它是“操作数修复”还是“返回值修复”。

## 本章重点整理

- RegBankSelect 的输出是 **banked gMIR**：有 bank、没有 regclass、仍是 `G_*`。
- `regBankSelected: true` + `selected: false` 是“只跑到 RegBankSelect”的标志。
- 23.1.4 的默认模式是 **Fast**；`-regbankselect-greedy` 才启用 Greedy。
- 强制 Fast 的是 `optnone`，不是 `-O0` 本身；`llc -O0` 的 IR 没有 `optnone` 时，greedy 仍生效。
- 同一个 `G_OR` 在 Fast 下可能产生 3 条跨 bank COPY，在 Greedy 下可以是 0 条；差异来自 alternative mappings 与代价模型。

## 自我检核

- [ ] 能解释 `regBankSelected: true` 与 `selected: false` 分别代表什么
- [ ] 能看着 `%0:gpr(i64)` 说出这个 vreg 的 bank、类型和当前阶段
- [ ] 能区分 ABI 边界的 `COPY` 与 RegBankSelect 的 repair `COPY`
- [ ] 能解释为什么 `llc -O0` 下 `-regbankselect-greedy` 仍然可能生效
- [ ] 知道官方预编译包为什么没有 `-debug-only`，以及什么时候必须自建

## 延伸阅读

### 官方文档

- **[LLVM GlobalISel — Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - **读哪里**：Pipeline 一节的阶段表与 “RegBankSelect” 小节。
  - **学什么**：每个 pass 的前置属性、输出属性，以及为什么 RegBankSelect 要求 `Legalized`。
  - **和本章的关联**：本章的 `legalized` / `regBankSelected` / `selected` 就来自这里。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：Machine Function 的 YAML 字段、`registers`、`body`、`-run-pass` 的说明。
  - **学什么**：`class:`、`%0:gpr(i64)`、`liveins` 这些语法分别是什么意思。
  - **和本章的关联**：不会读 MIR，后面的 mapping 与 COPY 实验都无从下手。

### 源码

- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：文件顶部的 `RegBankSelectMode` 命令行选项、`runOnMachineFunction`、`getRepairCost`。
  - **学什么**：默认模式、`optnone` 分支、Fast/Greedy 的分派点。
  - **和本章的关联**：实验三的结论就来自 `runOnMachineFunction` 里的 `if (F.hasOptNone())`。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`copyCost` 与 `getInstrAlternativeMappings`。
  - **学什么**：`copyCost` 的方向不对称（GPR→FPR=4、FPR→GPR=5；参数顺序是 `A = COPY B`）；`G_OR` / `G_BITCAST` / `G_LOAD` 的 alternative mappings。
  - **和本章的关联**：实验二里 Greedy 能把 `G_OR` 留在 FPR，就是因为这里有 FPR mapping。

### 演讲

- **[LLVM Developers' Meeting 2016](https://llvm.org/devmtg/2016-11/)**
  - **看哪场**：GlobalISel 的早期设计演讲（会议页面里有议程与视频链接）。
  - **学什么**：为什么 LLVM 要在 SelectionDAG 之外做一套新的 instruction selector，以及 RegBankSelect 在最初设计中的位置。
  - **和本章的关联**：理解“为什么要有 bank”比背 API 更重要。

下一章把镜头拉远：RegBankSelect 在整个 GlobalISel 管线中到底负责什么、不负责什么，以及它的输出如何被 InstructionSelect 消费。

→ [Ch 1 GlobalISel 全景中的 RegBankSelect](./01-regbankselect-in-globalisel.md)