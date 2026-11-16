# 练习 A — 给 MiniRV 加一条 MC 指令

> **目标**：把一条指令从 `.td` 记录一路走到汇编器、编码器、打印器、反汇编器和 lit 测试，理解 MC 层的闭环，而不是只背一条“加指令”的配方。

> **环境**：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/dsl-tablegen`，构建目录 `/home/ubuntu/llvm/builds/dsl-tablegen`，二进制目录 `B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`。

> **前置**：读过 [Ch 3 MC-only milestone](./03-mc-only-milestone.md)、[Ch 7 MC 层五件套](./07-mc-layer-five-pieces.md) 和 [Ch 8 汇编器、打印器、编码器](./08-assembler-printer-encoder.md)。如果你还没写过 TableGen 记录，先看 [Ch 6 TableGen minimum](./06-tablegen-minimum.md)。

> **产出**：一条 `FENCE` 记录、一个 `/tmp` 下的 MC lit 测试、一次真实 rebuild、四组真实输出，以及一个可复用的“新指令检查表”。

## 背景：为什么先练 MC 层

后端 bring-up 最容易产生一种错觉：只要 `llc` 能跑，指令就“加好了”。事实恰好相反。`llc` 只覆盖 CodeGen 路径；一条指令还可能从汇编器进入，经过匹配、编码、写对象文件，再被反汇编器读回来。这条路径不经过 SelectionDAG，也不经过寄存器分配。你如果只加 `Pat<...>` 而没加 MC 记录，汇编器会告诉你 `invalid instruction mnemonic`；你如果只加 MC 记录而没加 pattern，`llc` 永远不会选它。两条路径必须分开验证。

MiniRV 的 `auto-td` 语料只覆盖当前后端实际用到的 RV64IM 子集。`FENCE` 和 `EBREAK` 不在语料里，所以它们必须手写。这正好是一个干净的练习：`FENCE` 没有操作数、编码固定，不需要 CodeGen 支持；`EBREAK` 则会在练习 B 里接上 `llvm.trap()`。练习 A 只做 MC，目的是把“记录 → 匹配 → 字节 → 反汇编”这条链单独锁死。

MC 层在 LLVM 里的位置可以用一句话概括：**它是唯一能把助记符变成字节、再把字节变回助记符的闭环**。CodeGen 只负责“选哪条指令”，MC 层负责“这条指令长什么样”。两条路径共享同一份 TableGen 记录，但消费记录的生成文件不同。

### 三列对照

| 列 | RISC-V 参考实现 | MiniRV 的最小实现 | 你自己 target 的决策 |
|---|---|---|---|
| 记录位置 | `llvm/lib/Target/RISCV/RISCVInstrInfo.td` 的 `FENCE`、`EBREAK` | `llvm/lib/Target/MiniRV/MiniRVInstrInfo.td`，放在 `include "auto-td/AutoTdInstrInfo.inc"` 之后 | 手写记录放在生成语料之后，避免被生成器覆盖 |
| 操作数 | `FENCE` 有 `pred`/`succ` 两个 4 位字段；`InstAlias<"fence", (FENCE 0xF, 0xF)>` 提供无参写法 | `FENCE` 没有操作数，直接固定 `31-20=0x0ff` | ISA 有多种 fence 形式就建模操作数；只有一种规范形式就固定位 |
| 编码 | 由 `RVInstI` 的 `imm12 = {0b0000,pred,succ}` 组合 | 每个字段显式写 `Inst{...}` | 字段是否连续、是否有隐式位，决定你用格式类还是逐位写 |
| 汇编器 | 生成的 matcher 认识 `fence` 和 `fence iorw, iorw` | 生成的 matcher 只认识无参 `fence` | 先决定“规范拼写”，再决定要不要 alias |
| 测试 | `llvm/test/MC/RISCV/rvi-valid.s` 同时检查汇编和对象 | 在 `/tmp` 下写一个 MC lit 测试，检查字节和反汇编 | 每条新指令至少一个精确字节检查和一个反汇编检查 |

RISC-V 的 `FENCE` 不是“更复杂所以更高级”，而是它的 ISA 允许 `pred`/`succ` 组合。MiniRV 只保留一个固定编码，所以它的记录可以更短。**决策规则不是“照抄 RISC-V”，而是“你的 ISA 到底有几个可观察的拼写/编码变体”**。

## 先建立直觉

一条 `fence` 从文本到字节，再回到文本，经过这些站点：

```
fence
  │
  │ 1. lexer：Identifier("fence")
  ▼
MiniRVAsmParser::parseInstruction
  │    OperandVector = [Token("fence")]
  ▼
MatchInstructionImpl
  │    MiniRVGenAsmMatcher.inc
  │    mnemonic = "fence"，操作数个数 = 0
  │    Match_Success，Opcode = MiniRV::FENCE
  ▼
MCInst { Opcode = FENCE, Operands = [] }
  │
  │ 2. MiniRVMCCodeEmitter::encodeInstruction
  ▼
getBinaryCodeForInstr
  │    MiniRVGenMCCodeEmitter.inc
  │    Inst{31-20}=0x0ff, Inst{6-2}=0b00011, ...
  ▼
32-bit word = 0x0ff0000f
  │
  │ 3. support::endian::write32le
  ▼
bytes = 0f 00 f0 0f
  │
  │ 4. object file / .text
  ▼
llvm-objdump / MiniRVDisassembler
  │    read32le -> 0x0ff0000f
  │    MiniRVGenDisassemblerTables.inc 匹配固定位
  ▼
MCInst { Opcode = FENCE, Operands = [] }
  │
  │ 5. MiniRVInstPrinter::printInst
  ▼
fence
```

这张图里有两个“看起来像魔法”的地方，其实都不是魔法：

1. **匹配**：`MatchInstructionImpl` 不是手写的，它从 `MiniRVGenAsmMatcher.inc` 里拿到助记符表和 `Convert_NoOperands` 转换器。你写 `(outs), (ins)`，TableGen 就知道这条记录没有操作数。
2. **编解码**：`getBinaryCodeForInstr` 和 `decodeInstruction` 也不是手写的，它们从 `Inst` 字段和 `AsmString` 生成。你不需要为 `FENCE` 写任何 C++ 编码函数。

### MCInst 是什么

`MCInst` 是 MC 层的“指令实例”：一个 opcode 加一串 `MCOperand`。对 `FENCE` 来说，opcode 是 `MiniRV::FENCE`，操作数是空。对 `addi x1, x2, -1` 来说，opcode 是 `MiniRV::ADDI`，操作数是 `X1`、`X2`、`-1`。编码器只关心 opcode 和操作数；打印器只关心 `AsmString` 和操作数；反汇编器只关心位模式和操作数解码方法。

`FENCE` 没有操作数，所以它的整条路径最简单：匹配器只需要 `Convert_NoOperands`，编码器不需要任何 `EncoderMethod`，反汇编器不需要任何 `DecoderMethod`，打印器只需要 `AsmString = "fence"`。

### TableGen 记录如何变成四个生成文件

| 生成文件 | 消费它的代码 | 对 `FENCE` 来说多了什么 |
|---|---|---|
| `MiniRVGenAsmMatcher.inc` | `MiniRVAsmParser::matchAndEmitInstruction` | 助记符表里的 `fence`，匹配项 `{ 88 /* fence */, MiniRV::FENCE, Convert_NoOperands, ... }` |
| `MiniRVGenMCCodeEmitter.inc` | `MiniRVMCCodeEmitter::encodeInstruction` | `getBinaryCodeForInstr` 里对 `FENCE` 的位拼接 |
| `MiniRVGenAsmWriter.inc` | `MiniRVInstPrinter::printInst` | 字符串 `"fence\000"` |
| `MiniRVGenDisassemblerTables.inc` | `MiniRVDisassembler::getInstruction` | 固定位匹配后 `decode to FENCE` |
| `MiniRVGenInstrInfo.inc` | `MCInstrInfo` / `MCInstrDesc` | opcode 枚举和 `UnmodeledSideEffects` 标志 |

这四个文件都是构建产物，不要手改。你改的是 `.td`，然后让 `ninja` 重新生成它们。

## 任务规格

### 必做

1. 在 `llvm/lib/Target/MiniRV/MiniRVInstrInfo.td` 的 `include "auto-td/AutoTdInstrInfo.inc"` 之后加一条 `FENCE` 记录。
2. 不改 `auto-td/` 下的 YAML，也不改任何 C++ 文件。练习 A 是纯 MC 任务。
3. 不改 `MiniRVISelLowering.cpp`，不加 `Pat<...>`。`FENCE` 不接任何 SelectionDAG 节点。
4. 重新构建 `llc` 和 `llvm-mc`。只构建 `llc` 不够，因为 MC 路径主要看 `llvm-mc`。
5. 用 `llvm-mc -show-encoding` 检查字节，用 `-filetype=obj` + `llvm-objdump -d` 检查反汇编。
6. 在 `/tmp` 下写一个 MC lit 测试，不往仓库里加测试文件。
7. 保存 patch，回滚源码，重新构建，确认 `fence` 再次被拒绝，`git status` 回到只有 `ana-docs/`。

### FENCE 的编码

题目给定的字段是：

| 位 | 字段 | 值 | 含义 |
|---|---|---|---|
| `31-20` | `imm12` / `pred+succ` | `0x0ff` | RISC-V 的 `fence iorw, iorw`；高 4 位 0，pred=1111，succ=1111 |
| `19-15` | `rs1` | `0` | 不使用基址寄存器 |
| `14-12` | `funct3` | `0` | `MISC_MEM` 的 `funct3` |
| `11-7` | `rd` | `0` | 不写目标寄存器 |
| `6-2` | opcode 低位 | `0b00011` | `MISC_MEM` |
| `1-0` | 长度 | `0b11` | 32 位指令 |

把它们拼起来：

```
0x0ff << 20   = 0x0ff00000
0b00011 << 2  = 0x0000000c
0b11          = 0x00000003
OR            = 0x0ff0000f
```

小端写进对象文件就是 `0f 00 f0 0f`。这和 RISC-V 的 `fence iorw, iorw` 完全一致，因为 MiniRV 选择的就是这个规范形式。

### 约束

- `hasSideEffects = 1`：`FENCE` 不能被当作无副作用指令删除或重排。即使练习 A 不走 CodeGen，这个标志也应该正确，因为它是 ISA 语义的一部分。
- `hasNoSchedulingInfo = 1`：MiniRV 没有完整的调度模型；这个标志告诉 TableGen 不要为它找 itinerary。RISC-V 对应的是 `Sched<[]>`，本质都是“没有调度信息”。
- 不要加 `isCodeGenOnly = 1`。这个标志会把记录从汇编器 matcher 里藏起来，`llvm-mc` 就再也认不出 `fence`。练习 A 的整条 MC 链都会断。
- 不要加 `let isTrap = 1`。`FENCE` 不是 trap；`EBREAK` 才是练习 B 的主角。
- 不要给 `FENCE` 加操作数。题目要求的是固定编码、无操作数。如果你建模 `pred`/`succ`，那就是另一条练习，测试也要跟着变。

### 验收清单

- [ ] `llvm-mc -triple=minirv64 -show-encoding` 输出 `[0x0f,0x00,0xf0,0x0f]`。
- [ ] `llvm-mc -filetype=obj` 生成对象，`llvm-objdump -d` 显示 `0ff0000f fence`。
- [ ] 反汇编回来的助记符是 `fence`，不是数字或空行。
- [ ] `/tmp` 下的 MC lit 测试通过。
- [ ] 仓库自带的 `llvm/test/MC/MiniRV` 4 个测试仍然通过。
- [ ] 回滚后 `fence` 再次报 `invalid instruction mnemonic`。
- [ ] `git status --short` 只剩 `?? ana-docs/`。

## 期望输出

| 观察点 | 命令 | 期望 |
|---|---|---|
| 汇编器认识助记符 | `llvm-mc -show-encoding` | `fence` 加 `encoding: [0x0f,0x00,0xf0,0x0f]` |
| 编码正确 | 同上 | 4 字节，低地址是 `0x0f` |
| 对象文件正确 | `llvm-mc -filetype=obj` | 成功生成 ELF64 little-riscv 对象 |
| 反汇编正确 | `llvm-objdump -d` | `0: 0ff0000f fence` |
| 无操作数 | `fence 1` | 报 `invalid operand for instruction` |
| 测试正确 | `llvm-lit /tmp/...` | `Passed: 1` 或 `Passed: 2` |
| 回滚干净 | `git status --short` | 只有 `?? ana-docs/` |

练习 A 的成功标准不是“`llc` 能编译”，而是“`llvm-mc` 能双向转换，并且测试钉住了字节”。`llc` 在这里甚至不应该知道 `FENCE` 的存在，因为它没有 pattern。

## 卡住提示

1. **先找 include 行**：`grep -n 'include "auto-td/AutoTdInstrInfo.inc"' llvm/lib/Target/MiniRV/MiniRVInstrInfo.td`。所有手写记录都放在它后面，否则会被生成语料的作用域和命名影响。
2. **记录基类签名**：`MiniRVInst<dag outs, dag ins, string opcodestr, string argstr, list<dag> pattern, InstFormat format>`。无操作数就是 `(outs), (ins)`，pattern 是空列表 `[]`。
3. **`AsmString` 的默认值**：基类会拼 `opcodestr # "\t" # argstr`。`argstr` 为空时会留下一个尾随 tab，所以练习里显式写 `let AsmString = "fence";`。这不是编码正确性的必要条件，但能让打印结果干净。
4. **编码字段不要猜**：把 `Inst{31-20}`、`Inst{19-15}`、`Inst{14-12}`、`Inst{11-7}`、`Inst{6-2}`、`Inst{1-0}` 逐行写出来。TableGen 不会替你补 `rs1=0` 或 `rd=0`。
5. **为什么不用写编码函数**：没有操作数，就没有 `EncoderMethod`。`getBinaryCodeForInstr` 直接从 `Inst` 拼接。你在 `MiniRVMCCodeEmitter.cpp` 里看不到任何 `FENCE` 分支，这是正常的。
6. **为什么不用写解码函数**：没有操作数，就没有 `DecoderMethod`。反汇编器只需要固定位匹配，然后发一个空操作数的 `MCInst`。
7. **只 rebuild `llc` 会漏掉 MC**：`llvm-mc` 链接的是 `LLVMMiniRVDesc`、`LLVMMiniRVAsmParser`、`LLVMMiniRVDisassembler`。用 `ninja llc llvm-mc`，或者直接 `ninja`。
8. **测试不要放仓库**：在 `/tmp/practice-lit/` 建一个最小 `lit.cfg.py`，把 `%llvm-mc`、`%llvm-objdump`、`%FileCheck` 替换到构建目录。仓库测试目录只用来跑回归。
9. **`fence 1` 的报错不一定是“too many operands”**：生成的 matcher 对无操作数记录只接受零个操作数，多出来的 token 会落到 `Match_InvalidOperand`，所以真实报错是 `invalid operand for instruction`。不要背错误文本，先看 matcher 的 `Convert_NoOperands`。
10. **回滚后一定要重新构建**：源码回滚了，构建产物不会自动回滚。你不 rebuild，`llvm-mc` 仍然认识 `fence`，你会误以为回滚失败。

## 分段步骤

### Step 0：保存现场并记录基线

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ git status --short
?? ana-docs/
```

`ana-docs/` 是实验前就存在的未跟踪目录，不要动它。练习结束后的目标状态就是“只剩这一行”。

先确认基线不认识 `fence`：

```console
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ printf 'fence\n' > /tmp/practice-a-fence-only.s
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/practice-a-fence-only.s
/tmp/practice-a-fence-only.s:1:1: error: invalid instruction mnemonic
fence
^
exit=1
```

这条“失败”很重要。它是你回滚后要复现的对照，不是环境坏了。

### Step 1：读一条现有记录，确认风格

```console
$ sed -n '1,130p' llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
```

你会看到 `include "auto-td/AutoTdInstrInfo.inc"` 之后是调用帧伪指令、`Pat` 和手写节点。手写记录要插在 include 之后、调用帧伪指令之前。不要改 `auto-td/`，那是生成语料的输入。

### Step 2：写 `FENCE` 记录

在 include 行后插入下面这段。练习 A 只需要 `FENCE`，不需要 `EBREAK`。

```tablegen
def FENCE : MiniRVInst<(outs), (ins), "fence", "", [], InstFormatI> {
  let AsmString = "fence";
  let Inst{31-20} = 0x0ff;
  let Inst{19-15} = 0;
  let Inst{14-12} = 0;
  let Inst{11-7} = 0;
  let Inst{6-2} = 0b00011;
  let Inst{1-0} = 0b11;
  let hasSideEffects = 1;
  let hasNoSchedulingInfo = 1;
}
```

逐行读一遍：`(outs)` 空、`(ins)` 空、助记符 `fence`、参数串空、pattern 空、格式 `InstFormatI`。格式只影响 `.td` 的可读性和后续扩展，真正的位模式由 `Inst` 决定。

### Step 3：重新构建

```console
$ cd /home/ubuntu/llvm/builds/dsl-tablegen
$ /usr/bin/time -f 'ELAPSED=%e s' ninja llc llvm-mc
[0/2] Re-checking globbed directories...
[1/41] Building MiniRVGenDisassemblerTables.inc...
...
[40/41] Linking CXX executable bin/llc
ELAPSED=10.41 s
```

真实构建耗时约 **10.4 秒**（`ninja` 只重跑了 MiniRV 相关的 41 个步骤）。中间会重新生成 `MiniRVGenAsmMatcher.inc`、`MiniRVGenAsmWriter.inc`、`MiniRVGenMCCodeEmitter.inc`、`MiniRVGenDisassemblerTables.inc` 和 `MiniRVGenDAGISel.inc`。即使练习 A 不加 pattern，`MiniRVGenDAGISel.inc` 也会因为 `.td` 变了而重生成。

### Step 4：验证汇编与编码

```console
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/practice-a-fence-only.s
	fence                                   # encoding: [0x0f,0x00,0xf0,0x0f]
```

这里有两个信息：

- 助记符被打印成 `fence`，说明 `AsmString` 和 matcher 都对了。
- 字节是 `0f 00 f0 0f`，说明 `Inst` 的字段拼接对了。

如果你看到的是 `fence` 但字节不对，问题在 `Inst` 字段；如果字节对但助记符不对，问题在 `AsmString` 或 matcher。

### Step 5：验证对象与反汇编

```console
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/practice-a-fence-only.s -o /tmp/practice-a-fence.o
$ $B/llvm-objdump -d /tmp/practice-a-fence.o

/tmp/practice-a-fence.o:	file format elf64-littleriscv

Disassembly of section .text:

0000000000000000 <.text>:
       0: 0ff0000f     	fence
```

反汇编这一行同时证明三件事：对象文件写入了 4 字节、反汇编器读出了 `0x0ff0000f`、打印器把 opcode 映射回了 `fence`。如果编码器对但反汇编器错，通常是 `Inst` 字段和 decoder 表的匹配范围不一致；如果对象文件里根本没有这条指令，检查 `.text` 段和 `-filetype=obj`。

### Step 6：在 `/tmp` 写 MC lit 测试

仓库里的 MC 测试用 `llvm-mc`、`llvm-objdump` 和 `FileCheck`。在 `/tmp` 下也可以复刻，只要给 lit 一个最小配置：

```python
# /tmp/practice-lit/lit.cfg.py
import os
import lit.formats

config.name = 'practice-ab'
config.test_format = lit.formats.ShTest()
config.suffixes = ['.s', '.ll']
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, 'out')

B = '/home/ubuntu/llvm/builds/dsl-tablegen/bin'
config.substitutions.append(('%llvm-mc', os.path.join(B, 'llvm-mc')))
config.substitutions.append(('%llvm-objdump', os.path.join(B, 'llvm-objdump')))
config.substitutions.append(('%FileCheck', os.path.join(B, 'FileCheck')))
```

测试文件：

```asm
# /tmp/practice-lit/practice-a.s
# RUN: %llvm-mc -triple=minirv64 -show-encoding %s | %FileCheck %s
# RUN: %llvm-mc -triple=minirv64 -filetype=obj %s -o %t.o
# RUN: %llvm-objdump -d --triple=minirv64 %t.o | %FileCheck --check-prefix=DIS %s

# CHECK: fence
# CHECK-SAME: encoding: [0x0f,0x00,0xf0,0x0f]
fence

# DIS: fence
```

运行：

```console
$ $B/llvm-lit -v /tmp/practice-lit/practice-a.s
-- Testing: 1 tests, 1 workers --
PASS: practice-ab :: practice-a.s (1 of 1)

Testing Time: 0.06s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

练习 B 还会在这个目录里加一个 `.ll` 测试，所以你也可以把整个目录一起跑，得到 `2 tests, 2 workers` 和 `Passed: 2`。关键是：**新测试在 `/tmp`，仓库里不新增文件**。

### Step 7：跑仓库 MC 回归

```console
$ T=/home/ubuntu/llvm/trees/dsl-tablegen/llvm/test
$ $B/llvm-lit -q $T/MC/MiniRV
-- Testing: 4 tests, 4 workers --

Total Discovered Tests: 4
exit=0
```

4 个现有 MC 测试全部通过。练习 A 只加了一条 MC 记录，不应该改变任何已有编码。如果你在这里看到失败，先检查是不是误改了 `auto-td` 或已有记录。

### Step 8：保存 patch、回滚、重新构建、复验

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ git diff -- llvm/lib/Target/MiniRV/MiniRVInstrInfo.td \
    > /tmp/bringup/practice-a-fence.patch
$ git checkout -- llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
$ git status --short
?? ana-docs/
```

重新构建：

```console
$ cd /home/ubuntu/llvm/builds/dsl-tablegen
$ ninja llc llvm-mc
[0/2] Re-checking globbed directories...
...
[40/41] Linking CXX executable bin/llc
```

复验拒绝：

```console
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/practice-a-fence-only.s
/tmp/practice-a-fence-only.s:1:1: error: invalid instruction mnemonic
fence
^
exit=1
```

如果这里仍然认识 `fence`，说明 `llvm-mc` 没有重新链接。检查 `ninja` 是否真的重跑了 `MiniRVGenAsmMatcher.inc` 和 `bin/llvm-mc`。

## 参考解：完整补丁与真实验证

<details>
<summary>展开：FENCE 记录、拆分 patch、测试文件、生成物与 RISC-V 对照</summary>

### 1. 拆分后的精确 patch

完整实验补丁在 `/tmp/bringup/practice-ab.patch`，同时包含练习 B 的 `EBREAK`。练习 A 的独立 patch 在 `/tmp/bringup/practice-a-fence.patch`；下面就是它的内容，逐字一致：

```diff
diff --git a/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td b/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
index 0dd9138cc108..bb5585f1780b 100644
--- a/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
+++ b/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
@@ -112,6 +112,25 @@ def simm21_lsb0_bb : MiniRVTargetOperand {
 
 include "auto-td/AutoTdInstrInfo.inc"
 
+//===----------------------------------------------------------------------===//
+// Hand-written system instructions
+//===----------------------------------------------------------------------===//
+//
+// The auto-td corpus is RV64IM only; FENCE is not in it.  It is a fixed
+// encoding with no operands, so every bit is written out explicitly.
+
+def FENCE : MiniRVInst<(outs), (ins), "fence", "", [], InstFormatI> {
+  let AsmString = "fence";
+  let Inst{31-20} = 0x0ff;
+  let Inst{19-15} = 0;
+  let Inst{14-12} = 0;
+  let Inst{11-7} = 0;
+  let Inst{6-2} = 0b00011;
+  let Inst{1-0} = 0b11;
+  let hasSideEffects = 1;
+  let hasNoSchedulingInfo = 1;
+}
+
 //===----------------------------------------------------------------------===//
 // Call frame pseudos
 //===----------------------------------------------------------------------===//
```

注意：这是练习 A 的独立补丁，练习 B 的 `EBREAK` 不在其中。完整实验补丁（两条指令 + TRAP 合法化）在 `/tmp/bringup/practice-ab.patch`。

### 2. 生成物到底长什么样

构建后，`lib/Target/MiniRV/` 下的生成文件会多出这些内容（行号会随 TableGen 版本变化，这里只引用可搜索的文本）：

`MiniRVGenInstrInfo.inc`：

```text
    EBREAK                                = 361, // MiniRVInstrInfo.td:134
    FENCE                                 = 362, // MiniRVInstrInfo.td:122
...
    { 362,	0,	0,	4,	0,	0,	0,	MiniRVOpInfoBase + 1,	0,	0|(1ULL<<MCID::UnmodeledSideEffects)|... },  // FENCE
```

`FENCE` 的 `MCInstrDesc` 里出现 `UnmodeledSideEffects`，这就是 `hasSideEffects = 1` 的直接结果。`MiniRVOpInfoBase + 1` 说明它没有操作数。

`MiniRVGenAsmMatcher.inc`：

```text
  { 81 /* ebreak */, MiniRV::EBREAK, Convert_NoOperands, AMFBS_None, {  }, },
  { 88 /* fence */, MiniRV::FENCE, Convert_NoOperands, AMFBS_None, {  }, },
```

`Convert_NoOperands` 和空的 `{ }` 操作数列表就是“无操作数”的全部秘密。

`MiniRVGenAsmWriter.inc`：

```text
  /* 674 */ "fence\000"
```

打印器就是执行 `printInstruction`，从这张字符串表里取 `"fence"`。

`MiniRVGenDisassemblerTables.inc`：

```text
  15, 10,                                    //  50: case 0xf: {
  OPC_CheckField, 7, 25, 128, 192, 127,      //  52:  check Inst[31:7] == 0x1fe000
  OPC_Decode, 234, 2, 1,                     //  58:  decode to FENCE using decoder 1
```

这里不需要你写 `DecodeFENCE`。固定位匹配成功后，decoder 直接产生一个空操作数的 `MCInst`。

### 3. 真实测试输出

练习 A 的测试是 `/tmp/practice-lit/practice-a.s`：

```console
$ $B/llvm-lit -v /tmp/practice-lit/practice-a.s
-- Testing: 1 tests, 1 workers --
PASS: practice-ab :: practice-a.s (1 of 1)

Testing Time: 0.06s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

如果你把练习 B 的 `.ll` 也放进同一目录：

```console
$ $B/llvm-lit -v /tmp/practice-lit
-- Testing: 2 tests, 2 workers --
PASS: practice-ab :: practice-a.s (1 of 2)
PASS: practice-ab :: practice-b.ll (2 of 2)

Testing Time: 0.07s

Total Discovered Tests: 2
  Passed: 2 (100.00%)
```

### 4. RISC-V 对照

RISC-V 的 `FENCE` 记录在 `llvm/lib/Target/RISCV/RISCVInstrInfo.td`：

```tablegen
let hasSideEffects = 1, mayLoad = 0, mayStore = 0 in {
def FENCE : RVInstI<0b000, OPC_MISC_MEM, (outs),
                    (ins fencearg:$pred, fencearg:$succ),
                    "fence", "$pred, $succ">, Sched<[]> {
  bits<4> pred;
  bits<4> succ;

  let rs1 = 0;
  let rd = 0;
  let imm12 = {0b0000,pred,succ};
}
```

它允许任意 `pred`/`succ` 组合，并用 `InstAlias<"fence", (FENCE 0xF, 0xF)>` 提供无参写法。真实输出：

```console
$ printf 'fence iorw, iorw\n' > /tmp/riscv-ref.s
$ $B/llvm-mc -triple=riscv64 -mattr=+m -show-encoding /tmp/riscv-ref.s
	fence                                   # encoding: [0x0f,0x00,0xf0,0x0f]
```

这正是 MiniRV `FENCE` 的字节。MiniRV 没有建模 `pred`/`succ`，而是把这个规范形式固定下来。两种做法都对，区别是**可表达性**：RISC-V 能汇编 `fence r, w`；MiniRV 只接受 `fence`。

### 5. 清理与最终状态

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ git status --short
?? ana-docs/
$ ls /tmp/bringup/practice-ab.patch
/tmp/bringup/practice-ab.patch
```

树是干净的，patch 在 `/tmp`。构建产物已经回滚到基线，`llvm-mc` 再次拒绝 `fence`。

</details>

## 常见坑（错误直觉 → 正确认识）

1. **错误直觉**：只要在 `.td` 里加了指令，`llc` 就会选它。  
   **正确认识**：`llc` 只走 SelectionDAG/GlobalISel 的 pattern 匹配。`FENCE` 没有 pattern，所以 `llc` 永远不会主动生成它；它能被生成只可能来自内联汇编或手写 MIR。练习 A 的验收在 `llvm-mc`，不在 `llc`。

2. **错误直觉**：`isCodeGenOnly = 1` 只是“给 CodeGen 用”，加上更安全。  
   **正确认识**：这个标志会把记录从汇编器 matcher 中排除。加上它，`llvm-mc` 就会回到 `invalid instruction mnemonic`。练习 A 的指令必须能被汇编器看到，所以不能加。

3. **错误直觉**：无操作数指令不需要 decoder。  
   **正确认识**：它不需要 `DecoderMethod`，但仍然需要 decoder 表项。反汇编器必须知道 `0x0ff0000f` 对应 `FENCE`，否则 `llvm-objdump` 会把它显示成 `.word` 或未知指令。`Inst` 字段就是 decoder 表的输入。

4. **错误直觉**：`fence 1` 应该报 “too many operands”。  
   **正确认识**：生成的 matcher 对无操作数记录走 `Convert_NoOperands`，多出来的 token 会落到 `Match_InvalidOperand`。实测输出是：

   ```console
   $ $B/llvm-mc -triple=minirv64 /tmp/fence-bad.s -o /dev/null
   /tmp/fence-bad.s:1:7: error: invalid operand for instruction
   fence 1
         ^
   ```

   错误文本由 matcher 的结果决定，不要靠猜。

5. **错误直觉**：`AsmString` 可以直接留空，基类会拼好。  
   **正确认识**：基类拼的是 `opcodestr # "\t" # argstr`。无操作数时 `argstr` 为空，会留下尾随 tab。显式写 `let AsmString = "fence";` 能避免打印尾部空白，也让生成字符串表更干净。它不是编码正确性的必要条件，但值得做。

6. **错误直觉**：只 rebuild `llc` 就够了。  
   **正确认识**：`llvm-mc` 链接的是 `LLVMMiniRVDesc`、`LLVMMiniRVAsmParser`、`LLVMMiniRVDisassembler`。只构建 `llc` 可能不会重新链接 `llvm-mc`，你会看到旧行为。用 `ninja llc llvm-mc`。

7. **错误直觉**：`hasSideEffects` 只影响优化，不影响 MC。  
   **正确认识**：它在 `MCInstrDesc` 里也会变成 `UnmodeledSideEffects`；CodeGen 的机器指令层用它判断重排/删除。`FENCE` 必须设置它，否则语义上就是错的，哪怕当前测试只看字节。

8. **错误直觉**：回滚源码后，`llvm-mc` 会自动恢复。  
   **正确认识**：源码和构建产物是两件事。`git checkout` 只改源码；你必须重新 `ninja`，否则 `llvm-mc` 仍然带着旧的生成表。回滚后的复验是练习的一部分，不是形式主义。

## 自我检核

- [ ] 不看笔记，画出 `fence` 从文本到字节再回到文本的五个站点，并说出每个站点消费哪个生成文件。
- [ ] 解释为什么 `FENCE` 没有 `EncoderMethod` 和 `DecoderMethod`，但编码/解码仍然工作。
- [ ] 说出 `isCodeGenOnly = 1` 对 MC 路径的具体影响，并解释为什么练习 A 不能用它。
- [ ] 给定 `0x0ff0000f`，反推出六个字段的值；再解释小端字节序为什么是 `0f 00 f0 0f`。
- [ ] 比较 RISC-V 的 `FENCE` 和 MiniRV 的 `FENCE`：哪一种能表达 `fence r, w`？为什么？
- [ ] 如果你把 `hasSideEffects` 改成 0，哪些层会受影响？至少说出两个。
- [ ] 回滚后 `llvm-mc` 仍认识 `fence`，你的排查顺序是什么？

## 延伸阅读

- **[LLVM MC Layer](https://llvm.org/docs/CodeGenerator.html#the-mc-layer)** — LLVM 官方文档  
  - **读哪里**：`The MC Layer` 一节，重点看 `MCInst`、`MCStreamer`、`MCCodeEmitter`、`MCAsmBackend` 的分工。  
  - **能学到什么**：为什么 MC 层要独立于 CodeGen，以及汇编器/反汇编器共享哪些抽象。  
  - **前提**：读过本课程 Ch 7；不需要先读完整 CodeGenerator 文档。

- **[TableGen BackEnds](https://llvm.org/docs/TableGen/BackEnds.html)** — LLVM 官方文档  
  - **读哪里**：`AsmMatcher`、`AsmWriter`、`Disassembler`、`InstrInfo` 四个 backend 的说明。  
  - **能学到什么**：`Convert_NoOperands`、`AsmString`、`Inst` 字段分别被哪个 backend 消费。  
  - **前提**：能看懂基本的 TableGen 记录和 `let` 覆盖。

- **[RISC-V ISA Manual](https://riscv.org/technical/specifications/)** — RISC-V International  
  - **读哪里**：Unprivileged ISA 的 `MISC-MEM` 和 `SYSTEM` 指令编码，对照 `fence`、`ebreak` 的字段。  
  - **能学到什么**：`pred`/`succ` 的语义，以及为什么 `fence iorw, iorw` 是 `0x0ff0000f`。  
  - **前提**：只需要看指令编码表，不需要读完整特权架构。

- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)** — LLVM 官方文档  
  - **读哪里**：`lit.cfg.py`、`ShTest`、`%s`/`%t` 替换和 `FileCheck` 集成。  
  - **能学到什么**：为什么 `/tmp` 下的测试需要一个最小配置，以及怎样把构建目录里的工具接进去。  
  - **前提**：跑过至少一次 `llvm-lit`。

- **[RISC-V backend 课程](../riscv_backend/README.md)** — 本仓库  
  - **读哪里**：MC 层和指令定义相关章节。  
  - **能学到什么**：成熟后端如何组织 `RISCVInstrInfo*.td`、alias、operand 类和 MC 测试。  
  - **前提**：先完成本练习，带着“MiniRV 的最小实现”去对照。

→ [练习 B — 加一条带 DAG pattern 的指令](./practice-b-dag-pattern-instruction.md)
