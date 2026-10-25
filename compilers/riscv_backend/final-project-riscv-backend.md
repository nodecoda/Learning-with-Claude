# Final Project：把一个 C 函数追到 ELF，再给它加一条指令

> 目标：用一个完整项目把 24 章串起来。你要交的不是“能跑的命令记录”，而是一份可以被人复现的证据链：同一个函数，从 C 源码到最终机器码，每一层都能指出“这一步做了什么、为什么这样做、证据在哪”。最后再加一条自定义指令，证明你理解 TableGen 与后端的分界线。

> 环境：LLVM 23.1.4 预编译（host x86_64-pc-windows-msvc）；源码用 `.scratch/llvm-23.1.4-riscv-td` 的 RISC-V 后端。重编译 `llc` 的部分标 `guided`，本机磁盘不足，不要伪造构建输出。

## 为什么

前面每章解决一个局部问题，但真实的后端工作从来不是“只改一个 pass”。一次改动会同时影响：

- 指令选择（pattern 是否匹配）
- 寄存器分配（新的寄存器压力）
- 栈帧（spill 数量变化）
- 汇编输出（新助记符、新的 `.option`）
- 目标文件（新的重定位或 `e_flags`）
- 链接（relax 是否还能生效）

只验证其中一层，就会得到“测试过了但线上错了”的结果。这个 Final 要求你在同一份代码上同时给出六层证据，就是为了逼出这种跨层思维。

项目选的是一个很普通的函数：数组求和并写入全局变量，再被另一个函数调用。它同时包含循环、访存、全局变量、函数调用和栈帧，正好覆盖后半程的主线；又足够小，能在一页纸内读完。

## 主线

### 0. 题目与约束

固定输入（不要改，否则无法对照）：

```c
#include <stdint.h>
int64_t g_sum;

__attribute__((noinline))
int64_t accumulate(const int32_t *a, int n) {
    int64_t s = 0;
    for (int i = 0; i < n; i++) s += a[i];
    g_sum = s;
    return s;
}

int64_t call_acc(const int32_t *a, int n) { return accumulate(a, n) + 1; }
```

约束：

- 目标 `riscv64-unknown-elf`，`-march=rv64gc -mabi=lp64d -O2`。
- `noinline` 必须保留。去掉它，`accumulate` 会被内联进 `call_acc`，你就看不到 `call` 与栈帧，项目的一半证据会消失。
- 全局变量 `g_sum` 必须保留。它是 `%hi/%lo` 重定位的唯一来源。

### 1. 阶段一：C → 汇编，逐段标注

本机真实输出（节选，`accumulate`）：

```asm
accumulate:
	blez	a1, .LBB0_3
	mv	a2, a0
	li	a0, 0
	slli	a1, a1, 2
	add	a1, a1, a2
.LBB0_2:
	lw	a3, 0(a2)
	addi	a2, a2, 4
	add	a0, a0, a3
	bne	a2, a1, .LBB0_2
	j	.LBB0_4
.LBB0_3:
	li	a0, 0
.LBB0_4:
	lui	a1, %hi(g_sum)
	sd	a0, %lo(g_sum)(a1)
	ret
```

`call_acc`：

```asm
call_acc:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	call	accumulate
	addi	a0, a0, 1
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

报告里必须回答：

1. 循环上界为什么是 `slli a1, a1, 2` 而不是每次比较 `i < n`？（提示：把 `n` 换算成字节偏移，省掉循环内的乘法与计数器）
2. `blez a1, .LBB0_3` 与 `.LBB0_4` 的 `j` 分别处理什么边界？`n <= 0` 时 `a0` 的值从哪来？
3. `g_sum` 为什么用 `lui` + `sd %lo`，而不是 `auipc` + `sd`？（提示：默认 `small` 代码模型）
4. `call_acc` 的 16 字节栈帧里只有 `ra`，为什么还要 16 字节而不是 8？（提示：psABI 要求 16 字节对齐）
5. `call accumulate` 是伪指令，展开后是几条指令？在哪一层展开？

这五问对应第 12、13、17、21 章的内容；答不上来就回去补对应章节。

### 2. 阶段二：三个 MIR 停点

用 `llc` 对同一段 IR 取三次快照：

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -stop-after=finalize-isel final.ll -o isel.mir
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -stop-after=greedy final.ll -o ra.mir
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -stop-after=riscv-expand-pseudo final.ll -o post.mir
```

`finalize-isel` 的快照里，你应该看到：

- 虚拟寄存器（`%0`、`%1`…）与寄存器类（`:gpr`、`:gpr64`）
- `PseudoRET`、`PseudoBR` 这类伪指令
- `ADDI`/`LW`/`SW` 已经选出，但还没有物理寄存器

`greedy`（寄存器分配）之后：

- 虚拟寄存器消失，换成 `$x10`、`$x11` 这类物理寄存器（或 `$a0`、`$a1` 别名）
- spill/reload 出现（如果有）
- `PseudoRET` 还在

`riscv-expand-pseudo` 之后：

- `PseudoRET` 变成 `PseudoBR` + `RET`
- `PseudoLI` 展开成 `LUI` + `ADDI` 或 `ADDI`
- `call` 的 `AUIPC` + `JALR` 结构显式出现

报告要求：对每个停点给出「新增/消失的指令」列表，并解释是谁做的。不要贴整份 MIR，只贴差异。

### 3. 阶段三：目标文件与重定位

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -filetype=obj final.ll -o final.o
& "$bin\llvm-readobj.exe" --relocations final.o
& "$bin\llvm-readobj.exe" --file-headers final.o
& "$bin\llvm-objdump.exe" -d final.o
```

预期证据：

- `.rela.text` 里有 `R_RISCV_HI20` 与 `R_RISCV_LO12_S`，都指向 `g_sum`
- `e_flags` 含 `EF_RISCV_FLOAT_ABI_DOUBLE (0x4)` 与 `EF_RISCV_RVC (0x1)`，即 `0x5`
- `objdump -d` 里 `lui` 与 `sd` 的立即数是 0（占位），真正值在链接期填

这三个现象必须能互相解释：`e_flags` 说这是硬浮点 + RVC 目标文件；重定位说 `g_sum` 的地址还没定；反汇编说指令里的立即数现在是 0。

### 4. 阶段四：加一条自定义指令

设计一条 `xadd`：`rd = rs1 + rs2 + 1`，编码放在自定义 opcode 空间（例如 `0b0001011`，仅用于教学，不构成任何规范）。

需要交的东西：

1. **TableGen 定义**：复用 `RVInstR` 或自定义格式，写出 `def XADD : ...`。
2. **生成物证据**：在 `.td` 副本上跑 `llvm-tblgen -gen-instr-info`，截图或粘贴 `XADD` 的枚举行（真实输出会带 `.td` 行号注释）。
3. **AsmMatcher 证据**：跑 `-gen-asm-matcher`，在生成物里找到 `xadd` 的匹配项。
4. **pattern 设计**：说明你会用哪条 DAG pattern 匹配它（例如 `(add (add GPR:$rs1, GPR:$rs2), 1)`），以及为什么这个 pattern 可能干扰现有规则。
5. **lit 测试草案**：给出 `llvm/test/CodeGen/RISCV/xadd.ll` 的完整内容，包含 `RUN:` 行与 `CHECK:` 行。
6. **构建与验证步骤**（`guided`）：给出 `cmake` 配置、`ninja llc llvm-mc FileCheck` 命令、预期 `llc -mattr=+xadd` 的输出。

第 6 步本机无法完成（磁盘不足）。允许写成“计划 + 预期现象”，但不允许编造构建日志。

### 5. 报告结构

建议按以下顺序写，每节不超过两页：

1. 环境指纹（`llc --version` 前 4 行 + 磁盘剩余）
2. C 源码与编译命令
3. 汇编逐段解释（五个问题）
4. 三个 MIR 停点的差异表
5. 目标文件证据（重定位、`e_flags`、反汇编）
6. 自定义指令设计（TableGen、生成物、pattern、测试）
7. 失败与边界（至少两个你亲自遇到的坑）
8. 复盘：如果重来一次，你会先验证什么

## 可跑实验

### 实验 1：生成 IR 与汇编

```powershell
$bin = $env:LLVM_BIN
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S final.c -o final.s
& "$bin\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -emit-llvm -S final.c -o final.ll
```

预期：`final.s` 与本章给出的片段一致；`final.ll` 里能看到 `accumulate` 的循环与 `noinline` 属性。

### 实验 2：三个 MIR 停点

```powershell
$bin = $env:LLVM_BIN
foreach ($stop in 'finalize-isel','greedy','riscv-expand-pseudo') {
  & "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -stop-after=$stop final.ll -o "$stop.mir"
}
Select-String -Path finalize-isel.mir -Pattern 'PseudoRET|:gpr'
Select-String -Path greedy.mir -Pattern 'PseudoRET|\$x1[0-9]'
Select-String -Path riscv-expand-pseudo.mir -Pattern 'PseudoBR|RET'
```

预期：三份文件的指令形态依次变化；如果 `greedy.mir` 里还有 `%0`，说明 `-stop-after` 写错了。

### 实验 3：目标文件证据

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -O2 -filetype=obj final.ll -o final.o
& "$bin\llvm-readobj.exe" --relocations final.o
& "$bin\llvm-readobj.exe" --file-headers final.o | Select-String -Context 0,3 'Flags'
```

预期：两条 `g_sum` 重定位；`Flags (0x5)`。

### 实验 4：TableGen 验证自定义指令

```powershell
$bin = $env:LLVM_BIN
$work = Join-Path $env:TEMP 'xadd-final'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Copy-Item "$env:LLVM_SRC_TD\*.td" $work
# 按你的设计修改 $work\RISCVInstrInfo.td，然后：
& "$bin\llvm-tblgen.exe" -I "$($bin)\..\include" -I $work -gen-instr-info "$work\RISCV.td" -o "$work\GenInstrInfo.inc"
Select-String -Path "$work\GenInstrInfo.inc" -Pattern 'XADD'
```

预期：能找到 `XADD` 的枚举与名字表项；找不到说明 `def` 名拼错或没被 `RISCV.td` 间接 include。

## 源码落点

| 阶段 | 位置 |
|---|---|
| 调用约定与参数 | `RISCVCallingConv.td`、`RISCVISelLowering.cpp` 的 `LowerFormalArguments` |
| 栈帧 | `RISCVFrameLowering.cpp` 的 `determineFrameLayout` / `emitPrologue` |
| 指令选择 | `RISCVISelDAGToDAG.cpp`、`RISCVInstrInfo.td` 的 pattern |
| 寄存器分配 | `RISCVRegisterInfo.cpp` 的 `getCalleeSavedRegs` / `getReservedRegs` |
| 伪指令 | `RISCVExpandPseudoInsts.cpp`、`RISCVPostRAExpandPseudoInsts.cpp` |
| 汇编输出 | `RISCVAsmPrinter.cpp`、`RISCVMCCodeEmitter.cpp` |
| 重定位 | `RISCVELFObjectWriter.cpp`、`RISCVMCExpr.cpp` |
| 新增指令 | `RISCVInstrInfo.td`、`RISCVFeatures.td`、`RISCVInstrFormats.td` |

## 常见坑

1. **去掉 `noinline`**。`call_acc` 会把 `accumulate` 内联，`call` 与栈帧证据全部消失，项目变成另一个题。
2. **用 `-O0` 生成汇编**。`-O0` 的循环与栈帧形态完全不同（大量 spill），与本章给出的对照片段对不上。
3. **忘记 `-mattr=+c`**。不加 `+c`，`e_flags` 只有 `0x4`，`sd ra, 8(sp)` 也不会压成 `c.sd`。
4. **把 `--relocations` 跑在可执行文件上**。链接后重定位为空是正常的；要看重定位必须看 `.o`。
5. **只在 `llvm-tblgen` 成功后就宣布指令可用**。tblgen 成功只证明描述合法；`llc` 与 `llvm-mc` 需要重新构建。
6. **报告只贴命令不贴输出**。没有输出的命令等于没跑；评审无法判断你是复现的还是抄的。

## 自我检查

- [ ] 我的 `final.s` 与本章片段一致（函数名、指令顺序、栈帧大小）。
- [ ] 我能解释 `slli a1, a1, 2` 与 `blez` 两个优化点。
- [ ] 三份 MIR 的差异我能逐条说出“是谁做的”。
- [ ] `e_flags` 是 `0x5`，并且我能拆开这两位。
- [ ] 我的 `xadd` 能在 `GenInstrInfo.inc` 里被检索到，且注明了定义行。
- [ ] 我明确标出了哪些结论是 `guided`，没有伪造构建输出。

## 评分表（100 分）

| 项目 | 分值 | 合格标准 |
|---|---:|---|
| 汇编逐段解释 | 20 | 五个问题全部回答，且与真实输出一致 |
| MIR 三停点 | 20 | 每份快照的形态正确，差异归因到具体 pass |
| 目标文件与重定位 | 15 | 重定位类型、`e_flags`、反汇编三者互相印证 |
| 自定义指令 TableGen | 20 | 定义合法，生成物可检索，pattern 设计有理有据 |
| lit 测试草案 | 10 | `RUN:` 与 `CHECK:` 可执行，覆盖正例与反例 |
| 失败与边界 | 10 | 至少两个亲自遇到的坑，含报错原文 |
| 复盘与表达 | 5 | 结构清楚，命令可复制，结论有条件 |

低于 60 分建议重做阶段二与阶段三；这两部分是“读懂后端”的核心，其他部分可以补。

## 时间预算

| 阶段 | 预计时间 |
|---|---|
| 环境与汇编 | 1 小时 |
| 三个 MIR 停点 | 2 小时 |
| 目标文件与重定位 | 1 小时 |
| 自定义指令（TableGen 部分） | 3 小时 |
| 自定义指令（构建部分，guided） | 2 小时（不含构建等待） |
| 报告写作 | 2 小时 |

如果 TableGen 阶段超过 5 小时，先停下来检查 `RVInstR` 的参数顺序与 `OPC_OP` 的名字，这两个是最常见的卡点。

## 延伸阅读

- LLVM 官方 “TableGen Backends” 文档：`-gen-instr-info`、`-gen-dag-isel`、`-gen-asm-matcher` 各自的输入输出契约。
- `llvm/test/CodeGen/RISCV/` 与 `llvm/test/MC/RISCV/`：前者测选指与汇编输出，后者测编码与解析，两者不能互相替代。
- RISC-V psABI：栈对齐、参数传递、`e_flags` 的正式规定。
- 本课程第 19 章的伪指令展开与第 20 章的 AsmPrinter：Final 的阶段二、三会反复用到。

[上一章：RVV 向量后端与新增扩展](./23-vector-and-custom-extension.md) | [返回目录](./README.md)