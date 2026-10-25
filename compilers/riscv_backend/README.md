# LLVM RISC-V 后端全解：从 TableGen 到 ELF

> 给已经会写 C、读过一点 LLVM IR、想搞清楚“一条 `addi` 到底是谁生成的”的工程师。全课以本机 LLVM 23.1.4、RISC-V 源码 `llvmorg-23.1.4`、Windows x86_64 为基线，每个关键结论都配可复现命令。

这门课不教你“怎么用 `clang --target=riscv64` 编译一个程序”，而是把 RISC-V 后端拆成十二个站位：TableGen 描述、编码、汇编器、反汇编器、寄存器类、调用约定、栈帧、合法化、指令选择、机器 pass、汇编输出、链接。每一站都落到 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV` 的具体文件和具体符号上，再用 `llc`、`llvm-mc`、`llvm-tblgen`、`llvm-readobj`、`ld.lld` 产出可对照的证据。

全课 **24 章 + 4 个练习 + 1 个 Final Project + 本 README，共 30 篇**。配套脚本在 [`code/`](./code/)，统一入口是 `code/tools/run.ps1`。

> 证据约定：`实测` = 本机 23.1.4 的真实输出；`源码依据` = 对照 `llvmorg-23.1.4` 的文件与符号；`guided` = 需要重编译 `llc`，课程只给命令、差异思路与预期现象，不伪造构建日志。

## 为什么

RISC-V 后端是一个很适合“读完整后端”的目标，原因有三个：

1. **ISA 本身小且规整**。基础整数指令只有 R/I/S/B/U/J 六种格式，寄存器堆就是 `x0`–`x31`、`f0`–`f31`、`v0`–`v31`。你不需要先花两周记指令集，就能开始读选指代码。
2. **扩展机制清晰**。`+m`、`+a`、`+c`、`+v` 这些 Feature 在 `RISCVFeatures.td` 里是数据，不是散落在 C++ 里的 `if`。改一个 Feature 会影响哪些 pattern、哪些 ABI 检查，可以顺着 TableGen 追下去。
3. **工具链自洽**。`llc`、`llvm-mc`、`llvm-tblgen`、`ld.lld` 都来自同一版本，你可以在本机把“描述 → 编码 → 汇编 → 链接”整条链跑通，而不只是读代码。

反过来，只读教程不跑实验，最容易产生三种错觉：

- 以为 `vsetvli` 是指令选择阶段生成的（实际由 `riscv-insert-vsetvli` 后插入）。
- 以为改了 `.td` 指令就能用（实际要重编译 `llc` 与 `llvm-mc`）。
- 以为 `+d` 就等于 `lp64d`（实际是“特性”与“ABI”两件事，`e_flags` 会拆穿）。

本课的设计就是针对这三种错觉：每一章至少有一个“反直觉输出”，并且要求你解释它。

## 主线

### 1. 课程定位

| 维度 | 本课的选择 |
|---|---|
| 对象 | LLVM 23.1.4 的 RISC-V 后端（RV32/RV64，含 RVV 概览） |
| 方法 | 读同版本源码 + 跑预编译工具 + 用 TableGen 重建生成物 |
| 产物 | 汇编、MIR、目标文件、ELF、`.inc` 生成物、编译器 pass 列表 |
| 深度 | 从 `.td` 的一条 `def` 到 ELF 的一条重定位 |
| 边界 | 不构建完整 LLVM（磁盘不足）；不覆盖私有扩展与 GPU/加速器后端 |

### 2. 先修要求

| 领域 | 需要到什么程度 | 不要求 |
|---|---|---|
| C/C++ | 能读模板、继承、`switch` 分派、指针 | 写过 LLVM Pass |
| 编译原理 | IR、SSA、指令选择、寄存器分配的基本概念 | 手写寄存器分配器 |
| 汇编 | 能读 `addi`/`lw`/`beq` 这类指令 | 写过 RISC-V 裸机程序 |
| PowerShell | 能跑脚本、设环境变量、重定向输出 | 写复杂模块 |
| 体系结构 | 寄存器、栈、调用约定的直觉 | 流水线与缓存微架构 |

不要求读过 LLVM 源码。第 01 章会给你对象家族的全局图，第 03 章会教你怎么把编译过程停下来。

### 3. 课程地图：7 个 Part，30 篇

#### Part 0 — 全景与基线（Ch 00–03 + 练习 A）

- [Ch 00 环境与基线](./00-environment-and-baseline.md)：23.1.4 指纹、四个 RISC-V target、`run.ps1`、第一条函数
- [Ch 01 后端全景](./01-backend-map.md)：IR → DAG → MIR → MC → ELF 的十二个站位与对象家族
- [Ch 02 ISA 建模](./02-isa-modeling.md)：XLEN、扩展、寄存器堆、R/I/S/B/U/J 六种格式
- [Ch 03 流水线与 MIR](./03-pipeline-and-mir.md)：真实 pass 名单、MIR 语法、`-stop-after`/`-run-pass`
- [练习 A：一次编译的六段解剖](./practice-a-pipeline-dissection.md)：给一段 IR，产出六层证据

#### Part 1 — TableGen 与指令描述（Ch 04–07 + 练习 B）

- [Ch 04 TableGen 语言](./04-tablegen-language.md)：class/multiclass/def/let、Instruction/Operand/Pattern
- [Ch 05 生成物解剖](./05-generated-artifacts.md)：六个 generator、`ADD = 13725`、58 MB 的 DAGISel 表
- [Ch 06 指令编码](./06-instruction-encoding.md)：字段位序、`-show-encoding`、RVC 压缩
- [Ch 07 操作数与 Pattern](./07-operands-and-patterns.md)：Operand、ComplexPattern、DAG 匹配语法
- [练习 B：加一条自定义指令](./practice-b-custom-instruction.md)：在 `.td` 副本上改、生成、diff

#### Part 2 — MC 层（Ch 08–10）

- [Ch 08 汇编器](./08-asm-parser.md)：AsmParser、MatchInstructionImpl、别名与伪指令
- [Ch 09 反汇编器](./09-disassembler.md)：decode 表、往返实验、RVC 与别名打印
- [Ch 10 MC 层与目标文件](./10-mc-layer-and-objects.md)：MCInst/MCStreamer/MCCodeEmitter、ELF 节与 `.option`

#### Part 3 — 寄存器、ABI 与栈帧（Ch 11–13 + 练习 C）

- [Ch 11 寄存器与寄存器类](./11-registers-and-classes.md)：GPR/FPR/VR、spill slot、allocation order
- [Ch 12 调用约定](./12-calling-convention.md)：ilp32/lp64/lp64d、参数寄存器、sret/byval/varargs
- [Ch 13 栈帧](./13-frame-lowering.md)：prologue/epilogue、callee-saved、frame index elimination
- [练习 C：ABI 与栈帧审计](./practice-c-abi-audit.md)：六种函数签名的落地位置

#### Part 4 — 指令选择（Ch 14–17）

- [Ch 14 合法化](./14-legalization.md)：Legal/Widen/Narrow/Expand/Libcall、RV32 上的 i64
- [Ch 15 SelectionDAG 选指](./15-selection-dag.md)：DAG 构建、pattern 匹配、C++ 选指入口
- [Ch 16 自定义 lowering 与 intrinsic](./16-custom-lowering-and-intrinsics.md)：`LowerOperation`、`llvm.riscv.*`
- [Ch 17 GlobalISel](./17-globalisel.md)：四段流水、与 SelectionDAG 的真实对比

#### Part 5 — 机器层（Ch 18–20 + 练习 D）

- [Ch 18 调度模型与 llvm-mca](./18-scheduling-and-mca.md)：`SchedRW`、资源压力、模型误差
- [Ch 19 伪指令展开与机器 pass](./19-pseudo-expansion.md)：`PseudoLI`/`PseudoLA`、`vsetvli` 插入
- [Ch 20 AsmPrinter](./20-asm-printer.md)：`MCInstLower`、`.attribute`、`.cfi`、符号修饰
- [练习 D：调度与链接审计](./practice-d-scheduling-and-linking.md)：mca 对比 + 链接重定位落地

#### Part 6 — 链接与扩展（Ch 21–23 + Final）

- [Ch 21 重定位、代码模型与 ld.lld](./21-relocations-and-linking.md)：`HI20/LO12`、`PCREL`、`RELAX`、`jal` 收缩
- [Ch 22 常量池、GOT/PLT 与 TLS](./22-constant-pool-got-tls.md)：`.LCPI`、GOT 三连、三种 TLS 模型
- [Ch 23 RVV 与新增扩展](./23-vector-and-custom-extension.md)：`vtype` 的 208、`vsetvli` 插入、扩展清单
- [Final Project](./final-project-riscv-backend.md)：C 函数追到 ELF，再加一条自定义指令

### 4. 运行方式

所有命令都在仓库根目录 `D:\Learning-with-Claude` 执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 labs/00-version.ps1
```

`run.ps1` 做四件事：

1. 从脚本位置反推仓库根目录，拼出 `.scratch/llvm-23.1.4-root/.../bin`。
2. 把这个 `bin` 放到 `PATH` 最前面，保证 `llc`/`llvm-mc` 解析到 23.1.4。
3. 导出 `LLVM_BIN`、`LLVM_SRC_TD`、`PYTHONIOENCODING=utf-8`。
4. 按扩展名交给 `powershell -File` 或 `python`，并把子进程退出码透传出去。

脚本路径是相对 `code/` 的。也就是说 `labs/00-version.ps1` 会被解析成 `code/labs/00-version.ps1`。

### 5. 证据约定

每章都会出现三种标记，含义固定：

| 标记 | 含义 | 你能做什么 |
|---|---|---|
| `实测` | 本机 23.1.4 的真实输出 | 复制命令，得到同样的结果 |
| `源码依据` | 对同版本源码的描述 | 打开对应文件核对 |
| `guided` | 需要重编译 `llc` | 按步骤执行，核对预期现象 |

看到 `guided` 就意味着“本机没有跑过”，课程不会给你伪造的输出。这是这门课最重要的诚实性约束。

### 6. 环境与版本

| 项 | 值 |
|---|---|
| LLVM | 23.1.4 预编译，Optimized build |
| Host | x86_64-pc-windows-msvc |
| 目标 | riscv32 / riscv32be / riscv64 / riscv64be |
| 源码 | `.scratch/llvm-23.1.4-riscv-td`（sparse clone，`llvmorg-23.1.4`） |
| 工具 | `llc` `llvm-mc` `llvm-objdump` `llvm-readobj` `llvm-tblgen` `llvm-mca` `ld.lld` |
| 缺失 | `llvm-lit`、`FileCheck`（预编译包不带） |
| 磁盘 | 约 5 GiB 可用，不足以构建 `llc` |

`D:\llvm-project` 是 main / 24-dev，只能用来读结构，不能用来引用行号。这一点在第 00 章会用 `git describe` 再确认一次。

### 7. 学完能做什么

- 拿到任意一段 RISC-V 汇编，能说出它由哪条 IR、哪类 pattern、哪个 pass 生成。
- 能读 MIR，知道在 `finalize-isel`、`greedy`、`riscv-expand-pseudo` 三个停点各发生了什么。
- 能用 `llvm-readobj` 解释重定位、`e_flags` 与段布局，判断 small/medium/large 代码模型是否选错。
- 能解释 RVV 的 `vtype`、LMUL 与 `vsetvli` 插入，读懂向量 MIR。
- 能给 LLVM 加一条指令：写 `.td`、跑 tblgen、设计 pattern、写 lit 测试，并知道哪一步必须重编译。

### 8. 与其他课程的关系

| 课程 | 关系 |
|---|---|
| [`compilers/compiler_backend`](../compiler_backend/README.md) | 后端全景入门，RISC-V 只是其中一节；本课是它的深挖版 |
| [`compilers/globalisel_instruction_select`](../globalisel_instruction_select/README.md) | 以 AArch64 为主讲 GlobalISel；本课第 17 章从 RISC-V 视角对照 |
| [`compilers/globalisel_regbankselect`](../globalisel_regbankselect/README.md) | 寄存器银行选择专题，可与本课第 11、17 章对读 |
| [`compilers/elf_linking`](../elf_linking/README.md) | 链接器与重定位专题，可与本课第 21、22 章对读 |
| [`compilers/mlir_core`](../mlir_core/README.md) | MLIR 全貌；本课停在 LLVM IR 之后，不涉及 MLIR lowering |

## 常见坑

1. **拿 `D:\llvm-project` 当 23.1.4 用**。它是 main 分支，`.td` 结构与生成物都可能不同；本课所有源码引用都指向 `llvmorg-23.1.4` 的 sparse clone。
2. **以 PATH 里的 `llc` 为准**。系统里可能还有别的 LLVM；先用 `run.ps1` 或 `llc --version` 确认 23.1.4。
3. **把 `guided` 当 `实测` 写进报告**。没有构建日志就不要声称构建成功，评审一眼能看出来。
4. **在 `-O0` 下对照本章汇编**。`-O0` 的栈帧与 spill 形态完全不同，对不上不是教材错。
5. **用 PowerShell 5.1 运行含中文的 `.ps1`**。无 BOM 的 UTF-8 会被当成 ANSI；本课所有实验脚本都是纯 ASCII。
6. **以为 `llvm-tblgen` 成功就等于指令可用**。它只验证描述合法，`llc`/`llvm-mc` 需要重新构建。

## 自我检查

- [ ] 我能说出这门课覆盖的十二个站位，以及每一站对应哪个源文件。
- [ ] 我知道 `实测`、`源码依据`、`guided` 三种标记的区别。
- [ ] 我能用 `run.ps1` 跑通一个实验，并看到 23.1.4 的版本指纹。
- [ ] 我知道本机为什么不能构建 `llc`，以及这会影响哪些章节。
- [ ] 我能说出这门课与 `compiler_backend`、`globalisel_*` 两门课的分工。

## 延伸阅读

- LLVM 官方 “Writing an LLVM Backend” 与 “TableGen” 文档：后端对象家族与 TableGen 语法的基础，可与第 01、04 章对读。
- RISC-V 非特权规范与 psABI：指令格式、ABI、重定位、TLS 的正式定义，是本课所有“规范依据”的来源。
- `llvm/test/CodeGen/RISCV/` 与 `llvm/test/MC/RISCV/`：前者测选指与汇编输出，后者测编码与解析，是最大的题库。
- `lld/ELF/Arch/RISCV.cpp`：relax 的实现，读完第 21 章再来看，能解释“为什么链接器要迭代”。