# 练习 B：给后端加一条自定义指令

> 目标：在 RISCV `.td` 的**副本**里加入一条 `addi.imm`，用 `llvm-tblgen` 验证枚举和汇编匹配表的变化；再给出重新构建 `llvm-mc` 后能汇编这条指令的 guided 步骤与预期。练习的重点不是“改一行字符串”，而是分清 MC 层接受一条指令、反汇编器识别一条指令、llc 主动选择一条指令，是三件不同的事。

> 环境：LLVM 23.1.4 预编译版，Windows x86_64；同版本 RISCV `.td` 在 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。磁盘只剩不足 6 GB，不能在本机构建 llc；所有需要重新编译的步骤都标 `guided`。可跑的 tblgen 验证全部来自本机 2026-10-25 的重跑。

## 为什么

给后端加一条指令，最容易被误解成“在 `.td` 里写一个 `def`，然后 llc 就会用它”。实际链路有四层：

1. **TableGen 记录层**：`def ADDI_IMM : ALU_ri<...>` 产生一条指令记录，带操作数、编码位、汇编字符串。
2. **MC 层**：`llvm-mc` 通过 `-gen-asm-matcher` 的表识别 `addi.imm`，通过 `-gen-emitter` 的表编码，通过 `-gen-disassembler` 的表解码。
3. **选择层**：`llc` 的 SelectionDAG 需要一条 `Pat` 或自定义 lowering 才会主动生成这条指令；只有记录、没有 pattern，llc 不会选它。
4. **打印层**：`AsmPrinter` 和 `InstAlias` 决定输出 `addi`、`addi.imm` 还是别的别名。

这四层分开，练习才有意义。你可以在不构建 llc 的情况下完成第 1 层和第 2 层的表验证；第 3 层需要重新构建 llc，且要额外写 pattern；第 4 层的行为还可能受别名和反汇编顺序影响。

本练习选择 `addi.imm` 而不是随便造一个 opcode，原因是它复用 `ALU_ri`：`funct3 = 0b000`、opcode `OPC_OP_IMM`、操作数 `GPR, simm12_lo`，和 `ADDI` 编码完全相同，改的只是 mnemonic（记录层面还有 `ADDI` 自己的重物化标志，见 B.1）。这样能把变量降到最少：如果枚举和匹配表没变，先查副本/include 路径和输出文件，而不是编码格式。

练习的验收标准分三级：

- **L1（可跑）**：副本重新生成 `-gen-instr-info` 后出现 `ADDI_IMM`；重新生成 `-gen-asm-matcher` 后出现 `addi.imm` 的 mnemonic 和 `MatchEntry`。
- **L2（guided）**：用完整 LLVM 23.1.4 源码重新构建 `llvm-mc`，`llvm-mc -show-encoding` 接受 `addi.imm a0, a1, 42`，字节与 `addi` 相同。
- **L3（guided，可选）**：让 `llc` 主动选择 `ADDI_IMM`。这需要新增 pattern 或自定义 lowering，并且会和现有 `ADDI` pattern 竞争；本练习不作为必做项。

## 主线

### B.1 基线：先确认 ADDI 的真实位置和产物

在改任何东西之前，先确认基线。`ADDI` 在 `RISCVInstrInfo.td` 第 797 行，`ADD` 在第 818 行：

```tablegen
def ADDI  : ALU_ri<0b000, "addi">;
...
def ADD  : ALU_rr<0b0000000, 0b000, "add", Commutable=1>,
           Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

`ALU_ri` 在第 686 行：

```tablegen
class ALU_ri<bits<3> funct3, string opcodestr>
    : RVInstI<funct3, OPC_OP_IMM, (outs GPR:$rd), (ins GPR:$rs1, simm12_lo:$imm12),
              opcodestr, "$rd, $rs1, $imm12">,
      Sched<[WriteIALU, ReadIALU]>;
```

这告诉我们：`ADDI_IMM` 只要复用 `ALU_ri<0b000, "addi.imm">`，操作数、编码位、调度信息就和 `ADDI` 一样。差别有两处：`opcodestr`/`AsmString` 变了；`ADDI` 前面那两行 `let isReMaterializable = 1, isAsCheapAsAMove = 1` 只作用于 `ADDI`，不会传给 `ADDI_IMM`。本机副本记录里 `ADDI_IMM` 的这两个字段都是 `0`。

基线生成物：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$out = ".scratch/riscv-tblgen-ev"
New-Item -ItemType Directory -Force -Path $out | Out-Null
& "$bin/llvm-tblgen.exe" -I $inc -I $td -gen-instr-info "$td/RISCV.td" -o "$out/RISCVGenInstrInfo.inc"
Select-String -LiteralPath "$out/RISCVGenInstrInfo.inc" -Pattern '^\s*ADD(I)?\s+=|^\s*ADDW\s+='
```

本机真实输出：

```text
ADD                                        = 13725, // RISCVInstrInfo.td:818
ADDI                                       = 13727, // RISCVInstrInfo.td:797
ADDW                                       = 13729, // RISCVInstrInfo.td:923
```

注意 `ADDI` 和 `ADD` 之间还有 `ADDD = 13726`（`RISCVInstrInfoP.td`），所以 `ADDI` 不是 `13726`。枚举按记录集合分配，不按源码行号连续。

### B.2 复制整个 RISCV `.td` 目录

不要直接改源码树。复制整个目录，原因是 `RISCVInstrInfo.td` 里有大量 `include`：

```text
include "RISCVInstrFormats.td"
include "RISCVInstrInfoM.td"
include "RISCVInstrInfoC.td"
...
```

如果只复制 `RISCVInstrInfo.td` 到临时目录，`include` 会回退到 `-I` 指向的原目录，副本会混用原文件。复制整个目录最稳。

```powershell
$src  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$copy = ".scratch/riscv-td-copy-04"
if (-not (Test-Path $copy)) { Copy-Item -LiteralPath $src -Destination $copy -Recurse }
(Get-ChildItem -LiteralPath $copy -Recurse -File | Measure-Object Length -Sum).Sum
```

本机副本大小 `5,964,313` 字节；RISCV 根目录有 92 个 `.td`，`GISel/` 子目录里另有 1 个，共 93 个。`-I $copy` 要排在原源码目录之前；本实验只传 `-I $copy -I $inc`，`$inc` 里没有 RISCV 的 `.td`，不会抢。

检查副本和原文件是否相同：

```powershell
(Get-FileHash "$src/RISCVInstrInfo.td").Hash
(Get-FileHash "$copy/RISCVInstrInfo.td").Hash
```

两个哈希相同，说明复制成功、还没修改。

### B.3 在副本里插入 ADDI_IMM

目标行：

```text
def ADDI  : ALU_ri<0b000, "addi">;
```

插入后：

```text
def ADDI  : ALU_ri<0b000, "addi">;
def ADDI_IMM : ALU_ri<0b000, "addi.imm">;
```

用 PowerShell 按行插入，不破坏原文件编码：

```powershell
$f = Join-Path $copy 'RISCVInstrInfo.td'
$lines = [IO.File]::ReadAllLines($f)
$idx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -eq 'def ADDI  : ALU_ri<0b000, "addi">;') { $idx = $i; break }
}
if ($idx -lt 0) { throw 'ADDI anchor not found' }
$new = New-Object System.Collections.Generic.List[string]
$new.AddRange([string[]]$lines[0..$idx])
$new.Add('def ADDI_IMM : ALU_ri<0b000, "addi.imm">;')
if ($idx + 1 -lt $lines.Count) { $new.AddRange([string[]]$lines[($idx+1)..($lines.Count-1)]) }
[IO.File]::WriteAllText($f, (($new -join "`n")+"`n"), (New-Object Text.UTF8Encoding($false)))
Select-String -LiteralPath $f -Pattern '^def ADDI' | ForEach-Object { "$($_.LineNumber): $($_.Line)" }
```

本机真实输出：

```text
797: def ADDI  : ALU_ri<0b000, "addi">;
798: def ADDI_IMM : ALU_ri<0b000, "addi.imm">;
```

确认源码树没有被改：

```powershell
Select-String -LiteralPath "$src/RISCVInstrInfo.td" -Pattern 'ADDI_IMM'
```

预期没有输出。副本里有，原文件没有。

### B.4 生成并验证：枚举、名字表、汇编匹配

重新生成两个最关键的产物：

```powershell
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/copy-InstrInfo.inc"
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-asm-matcher "$copy/RISCV.td" -o "$out/copy-AsmMatcher.inc"
if ($LASTEXITCODE -ne 0) { throw 'tblgen failed' }
```

枚举真实输出：

```text
13742: ADDI                                       = 13727, // RISCVInstrInfo.td:797
13743: ADDIW                                      = 13728, // RISCVInstrInfo.td:912
13744: ADDI_IMM                                   = 13729, // RISCVInstrInfo.td:798
13745: ADDW                                       = 13730, // RISCVInstrInfo.td:924
13746: ADD_UW                                     = 13731, // RISCVInstrInfoZb.td:285
```

对照基线：

```text
base ADDW   = 13729, // RISCVInstrInfo.td:923
base ADD_UW = 13730, // RISCVInstrInfoZb.td:285
```

`ADDI_IMM` 拿到 `13729`，原来的 `ADDW` 从 `13729` 变成 `13730`，`ADD_UW` 从 `13730` 变成 `13731`。这不是错误，而是枚举插入后的正常位移。它也是“不要在外部硬编码 opcode 数字”的直接证据。

名字表真实输出：

```text
/* 355275 */ "ADDI_IMM\000"
```

汇编匹配真实输出：

```text
6326: "dd\005aaddu\003abs\004absw\003add\006add.uw\004addd\004addi\010addi.imm"
7406: { 260 /* addi.imm */, RISCV::ADDI_IMM, Convert__Reg1_0__Reg1_1__SImm12LO1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
```

这三行分别证明：

- `ADDI_IMM` 进入了名字表，`MCInstrInfo::getName` 能返回它。
- `addi.imm` 进入了 mnemonic 字符串表，汇编器能按文本找到它。
- `MatchEntry` 把 `addi.imm` 映射到 `RISCV::ADDI_IMM`，转换函数是 `Reg1, Reg1, SImm12LO1`。

注意 `\010` 是长度前缀，表示 `addi.imm` 有 10 个字符。字符串表不是按分隔符切分，而是“长度字节 + 字符”的紧凑格式。

### B.5 看 diff：一行改动，几千行变化

```powershell
git diff --no-index --numstat -- "$out/RISCVGenInstrInfo.inc" "$out/copy-InstrInfo.inc"
git diff --no-index --numstat -- "$out/RISCVGenAsmMatcher.inc" "$out/copy-AsmMatcher.inc"
```

本机真实结果（省略了目录和 git 的 `{旧 => 新}` 花括号）：

```text
9228    9224    RISCVGenInstrInfo.inc => copy-InstrInfo.inc
6227    6226    RISCVGenAsmMatcher.inc => copy-AsmMatcher.inc
```

一行 `.td`，`InstrInfo` 变 9,228 行，`AsmMatcher` 变 6,227 行。原因是：

- 枚举插入导致后续编号整体加一。
- 指令描述数组、名字表偏移、名字索引表跟着变。
- asm matcher 的 `MatchTable0` 偏移、mnemonic 字符串表、操作数匹配表都变。

这也是“生成物不进版本库”的原因。你不可能审查 9,000 行生成物 diff；正确做法是审查 `.td` 和测试。

### B.6 guided：重新构建 llvm-mc，让 `addi.imm` 能汇编

这一节需要完整 LLVM 23.1.4 源码和构建空间。本机的 `.scratch/llvm-23.1.4-riscv-td` 只是 RISCV 目录的 sparse checkout，**不能**单独构建 llc；`D:\llvm-project` 是 main/24-dev，版本不同，不能拿来验证 23.1.4 的行为。磁盘只剩不足 6 GB，也不够一次 Release 构建。因此以下步骤标 `guided`，只给命令、预期和判定标准，不在本机执行。

准备一份完整的 `llvm-project`，切到 `llvmorg-23.1.4`，把副本里的 `RISCVInstrInfo.td` 替换进去（或者把整个副本目录覆盖到源码树的 RISCV 目录）。然后：

```powershell
cmake -G Ninja -S llvm -B build `
  -DLLVM_TARGETS_TO_BUILD=RISCV `
  -DCMAKE_BUILD_TYPE=Release `
  -DLLVM_ENABLE_ASSERTIONS=ON
cmake --build build --target llvm-mc llc -j 8
```

`-DLLVM_ENABLE_ASSERTIONS=ON` 不是必需的，但它能让编码/解码表的断言更早暴露。构建目标先选 `llvm-mc` 和 `llc`，不要一上来构建全部工具。

构建完成后，测试汇编：

```powershell
$mc = "build/bin/llvm-mc.exe"
$asm = @(
  'addi.imm a0, a1, 42'
  'addi a0, a1, 42'
)
[IO.File]::WriteAllText("$env:TEMP\addi-imm.s", ($asm -join "`n")+"`n", (New-Object Text.UTF8Encoding($false)))
& $mc -triple=riscv64 -mattr=+c -show-encoding "$env:TEMP\addi-imm.s"
```

预期（guided）：

```text
addi.imm a0, a1, 42   # encoding: [0x13,0x85,0xa5,0x02]
addi     a0, a1, 42   # encoding: [0x13,0x85,0xa5,0x02]
```

字节必须相同，因为 `ADDI_IMM` 复用 `ALU_ri<0b000, ...>`，opcode/funct3/操作数都和 `ADDI` 一样。回显的 mnemonic 可能是 `addi.imm`，也可能被别名/打印器规范成 `addi`；以字节为准。若第一条直接报 `unknown instruction`，说明新构建的 `llvm-mc` 没有加载到修改后的 `RISCVGenAsmMatcher.inc`，先检查构建目录和源码路径。

再验证对象文件：

```powershell
& $mc -triple=riscv64 -mattr=+c -filetype=obj "$env:TEMP\addi-imm.s" -o "$env:TEMP\addi-imm.o"
& "build/bin/llvm-objdump.exe" -d "$env:TEMP\addi-imm.o"
```

预期（guided）：两条指令都编码成 `02a58513`，反汇编可能显示 `addi` 或 `addi.imm`。如果显示 `addi`，不是失败；反汇编器在两条同编码记录之间选择哪一条，取决于生成的解码表顺序。这也是为什么练习的 L2 验收标准是“能汇编且字节正确”，不是“反汇编必须回显 `addi.imm`”。

### B.7 guided 可选：让 llc 主动选择 ADDI_IMM

只加 `def ADDI_IMM` 不会让 llc 选它。llc 的选择来自 pattern，而 `ADDI` 已经有：

```tablegen
def : PatGprSimm12<add, ADDI>;
```

如果你再加：

```tablegen
def : PatGprSimm12<add, ADDI_IMM>;
```

两条规则竞争同一个 `(add GPR, simm12_lo)` 模式，最终选哪条由 TableGen 的排序和 `AddedComplexity` 决定，不保证选 `ADDI_IMM`。要让 llc 稳定选新指令，需要满足以下之一：

1. 移除或 guard 现有的 `ADDI` pattern（会改变整个后端的默认输出，不建议在练习里做）。
2. 给 `ADDI_IMM` 加更高的 `AddedComplexity`，让它在特定条件下优先；仍然会和 `ADDI` 竞争，需要测试。
3. 新建一个自定义 SDNode（例如 `riscv_addi_imm`），在 `RISCVISelLowering.cpp` 里把某个 intrinsic 或特定模式 lowering 成它，再写 pattern。这是真正的“新增选择能力”，不是“新增 mnemonic”。
4. 只在 MC 层使用它（本练习的 L2），不要求 llc 选择。

这也是本练习的核心边界：**新增指令记录 ≠ 新增选择规则**。MC 层能汇编，不代表 llc 会生成；llc 会生成，不代表反汇编器会优先回显它。

### B.8 失败与边界：真实错误

**错误 1：重名定义。**

```tablegen
def D : C;
def D : C;
```

真实报错：

```text
error: def already exists: D
def D : C;
    ^
note: location of previous definition
```

在 RISC-V 副本里把 `ADDI_IMM` 写成 `ADDI`，会得到同类的 `def already exists: ADDI`。tblgen 在解析阶段退出，不会生成半个 `.inc`。

**错误 2：改错文件。** 如果只改了原目录，副本没变，生成物不会有 `ADDI_IMM`。用 `Select-String` 确认副本第 798 行有插入，原文件没有。

**错误 3：把原目录也放进 `-I` 且排在前面。** 如果写成 `-I $td -I $copy -I $inc`，TableGen 会先用原 `$td` 的 `RISCVInstrInfo.td`，生成物里没有 `ADDI_IMM`。本机实测这种组合得到 0 条 `ADDI_IMM`；只留 `-I $copy -I $inc` 则得到 1 条。

**错误 4：以为 llc 会选。** 重新构建 llc 后，`addi.imm` 仍然不会出现在输出里，因为没有 pattern 指向它。这不是构建失败，是选择层没有规则。

**错误 5：反汇编回显不是 `addi.imm`（guided 预期）。** 两条同编码记录可能在解码表里竞争；回显哪条取决于表顺序。验收看字节，不看回显。

**错误 6：用错源码版本。** 用 `D:\llvm-project` 的 main/24-dev 构建，再拿 23.1.4 的 `.td` 对比，行号和生成物都会漂移。练习必须锁定 `llvmorg-23.1.4`。

## 可跑实验

### 实验 1：基线枚举

目标：确认 `ADDI`、`ADD`、`ADDW` 的基线编号。

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$out = ".scratch/riscv-tblgen-ev"
& "$bin/llvm-tblgen.exe" -I $inc -I $td -gen-instr-info "$td/RISCV.td" -o "$out/RISCVGenInstrInfo.inc"
Select-String -LiteralPath "$out/RISCVGenInstrInfo.inc" -Pattern '^\s*ADD(I)?\s+=|^\s*ADDW\s+='
```

预期：`ADD=13725`、`ADDI=13727`、`ADDW=13729`。

### 实验 2：复制、插入、重生成

目标：完成 L1 验收。

```powershell
$src  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$copy = ".scratch/riscv-td-copy-04"
if (-not (Test-Path $copy)) { Copy-Item -LiteralPath $src -Destination $copy -Recurse }
$f = Join-Path $copy 'RISCVInstrInfo.td'
$lines = [IO.File]::ReadAllLines($f)
$idx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -eq 'def ADDI  : ALU_ri<0b000, "addi">;') { $idx = $i; break }
}
if ($idx -lt 0) { throw 'ADDI anchor not found' }
$new = New-Object System.Collections.Generic.List[string]
$new.AddRange([string[]]$lines[0..$idx])
$new.Add('def ADDI_IMM : ALU_ri<0b000, "addi.imm">;')
if ($idx + 1 -lt $lines.Count) { $new.AddRange([string[]]$lines[($idx+1)..($lines.Count-1)]) }
[IO.File]::WriteAllText($f, (($new -join "`n")+"`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/copy-InstrInfo.inc"
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-asm-matcher "$copy/RISCV.td" -o "$out/copy-AsmMatcher.inc"
Select-String -LiteralPath "$out/copy-InstrInfo.inc" -Pattern 'ADDI_IMM|^\s*ADDW\s+='
Select-String -LiteralPath "$out/copy-AsmMatcher.inc" -Pattern 'addi\.imm'
```

预期：`ADDI_IMM=13729`、`ADDW=13730`；`addi.imm` 出现在 mnemonic 和 `MatchEntry`。

### 实验 3：确认原文件未被污染

```powershell
Select-String -LiteralPath "$src/RISCVInstrInfo.td" -Pattern 'ADDI_IMM'
Select-String -LiteralPath "$copy/RISCVInstrInfo.td" -Pattern 'ADDI_IMM'
```

预期：第一条无输出，第二条命中第 798 行。

### 实验 4：失败例子

在副本里临时把 `ADDI_IMM` 改成 `ADDI`（或者再写一条 `def ADDI`），运行：

```powershell
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/dup.inc"
```

预期：`error: def already exists: ADDI`，返回非零。改回 `ADDI_IMM` 后恢复。

### 实验 5：guided 重建与汇编

```powershell
cmake -G Ninja -S llvm -B build -DLLVM_TARGETS_TO_BUILD=RISCV -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON
cmake --build build --target llvm-mc llc -j 8
build/bin/llvm-mc.exe -triple=riscv64 -mattr=+c -show-encoding "$env:TEMP\addi-imm.s"
```

预期（guided）：`addi.imm a0, a1, 42` 输出 `[0x13,0x85,0xa5,0x02]`，与 `addi` 相同；若报 unknown instruction，检查构建产物和 `-I`/源码路径。

### 参考答案

<details><summary>参考答案</summary>

### 补丁

在 `RISCVInstrInfo.td` 的 `ADDI` 后加一行：

```tablegen
def ADDI  : ALU_ri<0b000, "addi">;
def ADDI_IMM : ALU_ri<0b000, "addi.imm">;
```

不需要改 `ALU_ri`。`ADDI_IMM` 复用 `funct3=0b000`、`OPC_OP_IMM`、`(outs GPR:$rd)`、`(ins GPR:$rs1, simm12_lo:$imm12)` 和 `"$rd, $rs1, $imm12"`。

### 完整命令

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$src = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$copy = ".scratch/riscv-td-copy-04"
$out = ".scratch/riscv-tblgen-ev"
if (-not (Test-Path $copy)) { Copy-Item -LiteralPath $src -Destination $copy -Recurse }
$f = Join-Path $copy 'RISCVInstrInfo.td'
$lines = [IO.File]::ReadAllLines($f)
$idx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -eq 'def ADDI  : ALU_ri<0b000, "addi">;') { $idx = $i; break }
}
if ($idx -lt 0) { throw 'ADDI anchor not found' }
$new = New-Object System.Collections.Generic.List[string]
$new.AddRange([string[]]$lines[0..$idx])
$new.Add('def ADDI_IMM : ALU_ri<0b000, "addi.imm">;')
if ($idx + 1 -lt $lines.Count) { $new.AddRange([string[]]$lines[($idx+1)..($lines.Count-1)]) }
[IO.File]::WriteAllText($f, (($new -join "`n")+"`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/copy-InstrInfo.inc"
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-asm-matcher "$copy/RISCV.td" -o "$out/copy-AsmMatcher.inc"
Select-String -LiteralPath "$out/copy-InstrInfo.inc" -Pattern 'ADDI_IMM|^\s*ADDW\s+='
Select-String -LiteralPath "$out/copy-AsmMatcher.inc" -Pattern 'addi\.imm'
```

### 预期输出

```text
13744: ADDI_IMM                                   = 13729, // RISCVInstrInfo.td:798
13745: ADDW                                       = 13730, // RISCVInstrInfo.td:924
6326: "dd\005aaddu\003abs\004absw\003add\006add.uw\004addd\004addi\010addi.imm"
7406: { 260 /* addi.imm */, RISCV::ADDI_IMM, Convert__Reg1_0__Reg1_1__SImm12LO1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
```

### 判定

- 出现 `ADDI_IMM = 13729`：L1 的枚举部分通过。
- 出现 `addi.imm` 的 mnemonic 行和 `MatchEntry`：L1 的汇编匹配部分通过。
- 原文件 `Select-String` 无输出：副本隔离通过。
- 重新构建 `llvm-mc` 后能汇编且字节为 `[0x13,0x85,0xa5,0x02]`：L2 通过。
- `llc` 输出里没有 `addi.imm`：正常，L3 未做；需要 pattern 或自定义 lowering。

### 常见追问

**Q：为什么 `ADDI_IMM` 是 13729，不是 13728？** `ADDIW` 占了 13728；枚举按名字/记录顺序分配，不按源码插入位置连续。

**Q：为什么 `ADDW` 变成 13730？** 新记录插在 `ADDW` 前面，TableGen 重新编号，后续整体位移。

**Q：能不能只加 `def` 不加 pattern，让 llc 用？** 不能。llc 的 SelectionDAG 只按 pattern/lowering 生成指令；MC 层的记录不会自动进入选择器。

**Q：反汇编显示 `addi` 而不是 `addi.imm`，算失败吗？** 不算。两条记录同编码，解码表选哪条取决于顺序；L2 验收看字节。

**Q：能不能在源码树里直接改？** 不建议。副本可回滚，也不会污染唯一的 23.1.4 源码。练习结束保留或删除副本都可以，但不要提交。

</details>

## 源码落点

| 位置 | 符号 | 说明 |
| --- | --- | --- |
| `RISCVInstrInfo.td:686` | `class ALU_ri` | `ADDI_IMM` 复用的模板 |
| `RISCVInstrInfo.td:797` | `def ADDI` | 插入锚点 |
| `RISCVInstrInfo.td:798` | `def ADDI_IMM` | 练习新增记录 |
| `RISCVInstrInfo.td:818` | `def ADD` | 对照用 R 型记录 |
| `RISCVInstrInfo.td:1394` | `PatGprSimm12` | 若要 llc 选择，需要 pattern |
| `RISCVInstrInfo.td:1471` | `PatGprSimm12<add, ADDI>` | 现有 `ADDI` 选择规则，新指令的竞争者 |
| `RISCVGenInstrInfo.inc` | `ADDI_IMM = 13729` | 枚举与名字表 |
| `RISCVGenAsmMatcher.inc` | `addi.imm` / `MatchEntry` | MC 汇编匹配 |
| `RISCVGenMCCodeEmitter.inc` | `InstBits[]` / `case RISCV::ADDI_IMM` | 编码，和 `ADDI` 同固定位（重跑 `-gen-emitter` 后预期出现） |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp:559` | `encodeInstruction` | 对象文件编码入口 |
| `AsmParser/RISCVAsmParser.cpp:3711` | `emitToStreamer` | 汇编器发指令与压缩入口 |
| `RISCVAsmPrinter.cpp:380` | `emitInstruction` | 文本打印路径 |
| `RISCV.td` | `include "RISCVInstrInfo.td"` | 入口 include 链 |

## 常见坑

1. **改源码树原文件。** 会污染唯一的 23.1.4 源码，也会让后续章节的基线漂移。复制整个 RISCV 目录到 `.scratch`。

2. **只复制一个 `.td`。** `include` 会回退到原目录，副本混用原文件，实验结论不可信。

3. **把原目录放在 `-I` 前面。** `-I $td -I $copy` 会先命中原始 `.td`，副本的改动被忽略；`-I $copy` 要排在原目录前。

4. **以为加 `def` 就会让 llc 选。** 选择层需要 pattern 或自定义 lowering；MC 层记录只影响汇编/编码/解码。

5. **用枚举号做外部接口。** 插入一条记录会让后续编号整体位移；`ADD=13725` 只是本次构建的值。

6. **期待反汇编一定回显新 mnemonic。** 同编码记录会在解码表里竞争，回显哪条取决于顺序；以字节为准。

7. **在 sparse checkout 上构建。** `.scratch/llvm-23.1.4-riscv-td` 只有 RISCV 目录，不能单独构建 llc；需要完整 `llvm-project` 的 `llvmorg-23.1.4`。

8. **用 main/24-dev 的源码验证 23.1.4。** 行号、生成物、记录名都会漂移；练习必须锁定版本。

9. **忽略磁盘。** 本机剩余空间不足 6 GB，不够 Release 构建；guided 步骤需要另一台机器或清理空间。

10. **把 L1 当成 L3。** tblgen 出现 `ADDI_IMM` 只说明记录和 MC 表生成了；llc 是否选择是另一个问题。

## 自我检查

- [ ] 我能在副本里插入 `ADDI_IMM`，并确认原文件没有被修改。
- [ ] 我能解释为什么 `ADDI_IMM` 复用 `ALU_ri<0b000, ...>` 后编码和 `ADDI` 相同。
- [ ] 我能跑 `-gen-instr-info` 和 `-gen-asm-matcher`，并读出 `ADDI_IMM` 的枚举、名字和 `MatchEntry`。
- [ ] 我能解释 `ADDW` 为什么从 13729 变成 13730。
- [ ] 我能区分 L1（tblgen 表验证）、L2（重建 llvm-mc 能汇编）、L3（llc 主动选择）。
- [ ] 我知道只加 `def` 不会让 llc 选择，需要 pattern 或自定义 lowering。
- [ ] 我能说出 guided 构建需要完整 LLVM 23.1.4 源码，不能用 sparse checkout 或 24-dev 代替。
- [ ] 我能处理重名定义、`-I` 顺序、反汇编回显不确定这三个边界。

## 延伸阅读

- LLVM TableGen Backends：<https://llvm.org/docs/TableGen/BackEnds.html>
- LLVM 后端开发指南：<https://llvm.org/docs/CodeGenerator.html>
- LLVM MC 层文档：<https://llvm.org/docs/CodeGenerator.html#the-mc-layer>
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`
- `llvm/lib/Target/RISCV/RISCV.td`
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`
- `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`
- `llvm/test/MC/RISCV/`（汇编/编码测试）
- `llvm/test/CodeGen/RISCV/`（选择器测试）

[上一章：操作数与 Pattern](./07-operands-and-patterns.md) | [下一章：汇编器](./08-asm-parser.md)