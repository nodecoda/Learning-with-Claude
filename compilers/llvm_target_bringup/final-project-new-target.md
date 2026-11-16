# Final Project — 从零接入 MiniRV+

> **目标**：把 Ch 00–27 的分层知识合成一次可验收的 bring-up：在现有 MiniRV 上增加一条真实指令 `xdemo.sadd`，走完编码 → MC → DAG pattern → lit → e2e 全链路；再可选地把 Clang 的 `TargetInfo` 接上，让 `clang --target=minirv64` 能编译一部分 C。

> **环境**：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`，链接器 `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`，模拟器 `/usr/bin/qemu-riscv64-static`。前端实测使用 `/home/ubuntu/llvm/builds/pr-lane/bin/clang`（本树的 `bin/clang` 当前有共享库符号问题，见 Ch 26）。

> **性质**：本章是项目规格与参考实现，不是新的机制章节。所有改源码并重新构建的步骤都标 **guided**；只有“现有 MiniRV 的基线输出”标 **实测**。我们不会伪造 `xdemo.sadd` 的构建日志。

## 为什么把 Final 做成“再加一条指令”？

给 target 加一条指令是 bring-up 的最小完整切片：它足够小，一周能做完；又足够深，会同时碰到编码、MC、选指、测试和运行边界。

```
命令行/feature → TableGen 记录 → MC 编码 → 汇编器/反汇编器
      → SelectionDAG 选指 → 寄存器分配/帧 → AsmPrinter → ELF → 链接/运行
```

`xdemo.sadd` 同时具备三个教学属性：非标准编码（custom-0）、非平凡操作数（`rd, rs1, rs2, uimm2`）、可融合的 DAG 形状（`shl(add(a,b), n)`）。每一层都有可观察的字节、汇编行或退出码；任何一层偷懒都会在下一层暴露。

## 项目定义：MiniRV+ = MiniRV + XDemo

### 指令语义

```
xdemo.sadd rd, rs1, rs2, uimm2
  rd = (rs1 + rs2) << uimm2      // 64 位回绕，uimm2 ∈ [0,3]
```

名字里的 `sadd` 读作 “shifted add”。它不是 RISC-V 标准指令，也不是饱和加法；我们刻意避开 `sadd` 在 RISC-V P 扩展里的已有含义，只在 `xdemo.` 命名空间里使用。

### 编码

采用 R 型布局，opcode 用 custom-0（`0b0001011`），`uimm2` 放在 `Inst{26-25}`，`funct3=0`，`Inst{31-27}=0`：

```
 31      27 26  25 24    20 19    15 14  12 11     7 6        0
+----------+------+--------+--------+------+--------+----------+
| 00000    |uimm2 |  rs2   |  rs1   | 000  |   rd   | 0001011  |
+----------+------+--------+--------+------+--------+----------+
```

| 字段 | 位 | 值/来源 |
|---|---|---|
| opcode | 6..0 | `0x0b`（custom-0） |
| rd | 11..7 | 目标寄存器 |
| funct3 | 14..12 | `0` |
| rs1 | 19..15 | 左加数 |
| rs2 | 24..20 | 右加数 |
| uimm2 | 26..25 | 移位量 0..3 |
| funct5 | 31..27 | `0` |

RISC-V 保留了四个 custom opcode 空间；`0x0b` 不会和 RV64IM 的标准指令冲突。我们**不**新增 ELF machine、不新增 relocation、不改 `Triple`：MiniRV 的对象仍然是 `elf64-littleriscv` / `EM_RISCV`，这条指令只是一个普通 32 位字。

### 为什么默认关闭

`xdemo` 是扩展，不是 MiniRV 的默认 ISA。用子目标特性 `+xdemo` 控制：

- `llc -mtriple=minirv64`：默认 RV64IM，不选 `xdemo.sadd`，现有 17 个 e2e 程序行为不变。
- `llc -mtriple=minirv64 -mattr=+xdemo`：允许选 `xdemo.sadd`。
- `llvm-mc -triple=minirv64`：默认拒绝 `xdemo.sadd`；加 `-mattr=+xdemo` 才接受。

默认开启会让 stock qemu 在遇到 custom-0 时直接 trap，e2e 全红；默认关闭才能让差分 harness 继续当回归网。

## 先建立直觉：一条指令穿过哪些层

```
xdemo.sadd 文本
   │
   ├─ 1. 编码真值：auto-td/opcodes-ysx 的 opcode 行 + instructions/rv-xdemo/*.yaml
   ├─ 2. TableGen：XDEMO_SADD + AsmMatcher + AsmWriter + Disassembler + MCCodeEmitter
   ├─ 3. MC：parser → MCInst → 4 字节 → .o → objdump 反向
   ├─ 4. CodeGen：feature/predicate + Pat (shl (add GPR, GPR), uimm2)
   ├─ 5. e2e：MiniRV vs riscv64 差分（默认 fallback 路径）
   └─ 6. xdemo 路径：静态指令数 + MC 往返（stock qemu 不执行 custom-0）
```

关键直觉：**编码不是从 `.td` 里“写”出来的，是从 opcode 数据库“读”出来的**。MiniRV 的指令记录由 `llvm/utils/auto-td` 生成；YAML 只写“我是谁、用哪个 taxonomy、哪个 predicate”，不写 `bits<32> Inst`。你在 Final 里要遵守同一条规则，否则 `check-autotd` 会拒绝你的 YAML。

## 课程概念覆盖：70% 不是口号

| 课程 | 落点 |
|---|---|
| Ch 00/01/04/05 | 基线、七层、Triple、TargetRegistry；`llc --version` 能列出 `minirv64` |
| Ch 02/03/06/07/08 | 参照、MC-only、TableGen、MC 五件套、encoder；opcode 行 + YAML + `llvm-mc` 往返 |
| Ch 09/10/12/13/19 | Subtarget/feature、寄存器、合法化、Pat、`uimm2`；`XDEMO_SADD` |
| Ch 11/14/15/16/17/18 | 帧、内存、控制流、调用、参数、栈帧；17 个 e2e 程序 |
| Ch 20/21/22/23 | 软浮点/CFI 回归、ELF/链接、显式拒绝；e2e + `llvm-readobj` |
| Ch 24/25/26/27/28 | 测试、差分、可选 Clang、补丁系列、复盘；56 lit + 18 e2e |

除 Ch 26（可选）和 Ch 28（复盘）外，其余 27/29 章都被直接用到（约 93%）；Track B 再补 Ch 26。**没有覆盖的是** GPU/JIT/完整 LLD/compiler-rt 运行时——这些本来就不在本课范围。

## 任务规格

### 功能需求

1. `llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding` 能把 `xdemo.sadd` 编成 4 字节，且字节与规格一致。
2. `llvm-mc ... -filetype=obj` + `llvm-objdump -d --triple=minirv64` 能往返，打印同样的汇编。
3. 不加 `+xdemo` 时，汇编器必须拒绝，错误信息包含 `instruction requires`。
4. `uimm2` 只接受 0..3；`4`、`-1`、`8` 必须报 invalid operand，不能静默截断。
5. `llc -mtriple=minirv64 -mattr=+xdemo -O2` 对 `shl(add(a,b), 2)` 选出一条 `xdemo.sadd`，而不是 `add` + `slli`。
6. `llc -mtriple=minirv64 -O2`（无 `+xdemo`）仍生成 `add` + `slli`，现有测试不变。
7. 新增 MC lit、CodeGen lit、负向 lit；全量 lit 从 53 增加到 56。
8. e2e harness 从 17/17 变成 18/18（新增的 `xdemo_shadow.ll` 走默认 fallback）。
9. 静态 benchmark 证明 `+xdemo` 相对 `-xdemo` 的指令数减少量等于 `xdemo.sadd` 的站点数。
10. 文档写清“stock qemu 不能执行 custom-0”，不把静态基准伪装成运行时性能。

### 非目标：明确不做

| 不做 | 原因 |
|---|---|
| RVV / 向量 / scalable vector | 会把项目从“一条指令”变成“一个扩展” |
| TLS / 原子 / 浮点 / 完整 psABI | MiniRV 已有显式拒绝或 libcall 回归；Track B 只做整数标量 |
| 新 ELF machine / relocation / LLD / compiler-rt | `xdemo.sadd` 没有符号操作数，复用 `EM_RISCV` 和现有 `ld.lld` |
| 调度模型 / 成本模型 / GlobalISel / 第二个新指令 | 一条指令不值得引入 itinerary；优秀路径是加深测试，不是加宽 ISA |

**范围守卫**：如果你发现自己在改 `Triple.cpp`、`RISCV*`、`ELFObjectWriter`、`BareMetal.cpp`（Track A），停下来。Track A 的 diff 应该只在 `llvm/lib/Target/MiniRV` 和 `llvm/test/{MC,CodeGen}/MiniRV` 里。

### 交付物与文件清单

| 文件 | 作用 |
|---|---|
| `auto-td/opcodes-ysx/extensions/rv_xdemo`、`instructions/rv-xdemo/xdemo_sadd.yaml`、`taxonomy/xdemo.yaml`、`config.yaml`、`CMakeLists.txt` | 编码真值 + 生成器接线 |
| `MiniRVInstrInfo.td`、`MiniRVISelDAGToDAG.cpp` | feature/predicate、`uimm2`、`Pat`、ISel 的 `Subtarget` 成员 |
| `AsmParser/MiniRVAsmParser.cpp`、`Disassembler/MiniRVDisassembler.cpp` | `isUImm2()`、`DecodeUImm2()` |
| `llvm/test/MC/MiniRV/xdemo-sadd{,-invalid}.s`、`llvm/test/CodeGen/MiniRV/xdemo-sadd.ll` | MC/CodeGen/负向测试 |
| `dev/e2e/xdemo_shadow.ll`、`dev/xdemo/{check.sh,bench.sh,README.md}` | e2e、静态检查、基准、限制 |
| `clang/lib/Basic/Targets/MiniRV.{h,cpp}`、`Targets.cpp`、`CMakeLists.txt` | 可选 TargetInfo + 注册 |
| `clang/lib/Driver/ToolChains/Arch/MiniRV.cpp`、`Clang.cpp`、`Driver.cpp` | 可选 driver 默认值 + BareMetal |
| `clang/lib/CodeGen/Targets/MiniRV.cpp`、`CodeGenModule.cpp` | 优秀：整数 ABIInfo |

## 里程碑：6 步，每步一个可验证 checkpoint

### M0 — 冻结基线（Ch 00/24/25）

改任何文件前，记录三组数：lit、e2e、bench。把它们贴进 `dev/xdemo/README.md` 的 “baseline” 一节。

```console
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ $B/llvm-lit -s -j 16 $TREE/llvm/test/CodeGen/MiniRV $TREE/llvm/test/MC/MiniRV 2>/dev/null
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld QEMU=/usr/bin/qemu-riscv64-static \
  LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen $TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen $TREE/llvm/lib/Target/MiniRV/dev/bench/run.sh
```

**Checkpoint**：`53 passed`、`17/17 OK`、bench 表与 Ch 25 一致。任何一项不一致，先修环境，不要开始改代码。

### M1 — 编码真值与 MC 记录（Ch 03/06/07/08）

加 opcode overlay、YAML、taxonomy、config、CMake 的 `YSX_OPCODES`；先用 `check-autotd` 验证记录，不要急着编译。

```console
$ ninja -C /home/ubuntu/llvm/builds/dsl-tablegen check-autotd
$ ninja -C /home/ubuntu/llvm/builds/dsl-tablegen lib/Target/MiniRV/auto-td/AutoTdInstrInfo.inc
$ grep -A12 'def XDEMO_SADD' /home/ubuntu/llvm/builds/dsl-tablegen/lib/Target/MiniRV/auto-td/AutoTdInstrInfo.inc
```

**Checkpoint**：`check-autotd` 通过；`grep` 显示 `bits<2> imm;` 和 `Inst{26-25} = imm`。此时先不要 `ninja llc`：`uimm2`/`HasXdDemo` 还没定义，M2 补。

### M2 — Parser/printer/decoder 的边界（Ch 08/23/24）

加 `UImm2AsmOperand`/`uimm2`、`isUImm2()`、`DecodeUImm2()`，以及 `FeatureXdDemo`/`HasXdDemo`；构建后跑 `$B/llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding`（输入用 `llvm/test/MC/MiniRV/xdemo-sadd.s`），并补负向测试：`4`、`-1`、缺操作数、无 feature。

**Checkpoint**：`xdemo.sadd x10,x11,x12,2` 输出 `[0x0b,0x85,0xc5,0x04]`；不加 `-mattr` 报 `instruction requires`；`llvm-lit -v` 两个 MC 文件全过；`llvm-objdump` 往返一致。

### M3 — Feature、Subtarget 与 DAG pattern（Ch 09/10/12/13）

在 `MiniRVInstrInfo.td` 加 `Pat`；在 `MiniRVISelDAGToDAG.cpp` 提供 `Subtarget` 成员（`FeatureXdDemo`/`uimm2` 已在 M2）。先只跑 `-mattr=+xdemo`，确认选指。

```console
$ $B/llc -mtriple=minirv64 -mattr=+xdemo -O2 xdemo.ll -o -
$ $B/llc -mtriple=minirv64 -mattr=-xdemo -O2 xdemo.ll -o -
$ $B/llc -mtriple=minirv64 -mattr=+xdemo -O2 -stop-after=finalize-isel xdemo.ll -o -
```

**Checkpoint**：`+xdemo` 输出一条 `xdemo.sadd`；`-xdemo` 输出 `add` + `slli`；MIR 里能看到 `XDEMO_SADD`。如果 `+xdemo` 仍输出两条，先看 `-debug-only=isel`，不要改 pattern 之外的层。

### M4 — lit 测试与 auto-td 回归（Ch 24/06）

写 `xdemo-sadd.ll`，覆盖 `uimm2=0..3`、`uimm2=4` 不融合、fallback、objdump 路径；把 `xdemo-sadd.s` 的三条 `RUN:` 写全。

**Checkpoint**：全量 lit `56 passed`；`check-autotd` 仍通过；`git diff --check` 干净。

### M5 — e2e 与静态 benchmark（Ch 22/25）

加 `dev/e2e/xdemo_shadow.ll`，让默认路径跑 18/18；写 `dev/xdemo/check.sh` 与 `dev/xdemo/bench.sh`，验证自定义路径的编码/选指/指令数差。

**Checkpoint**：e2e `18/18 OK`；`check.sh` 断言 `xdemo.sadd` 站点数 > 0 且编码字节正确；`bench.sh` 断言 `off - on == sites`。**不**要求 stock qemu 执行 custom-0。

### M6 — 可选 Clang track + patch series（Ch 26/27）

先加 `TargetInfo` 让 `clang --target=minirv64 -c` 能过；再加 driver 默认 `-target-abi lp64`、`-target-feature +m`；最后按 Ch 27 拆 commit。

**Checkpoint**：`clang --target=minirv64 -### -c t.c` 出现 `-target-abi lp64`；`clang --target=minirv64 -c t.c` 产出对象；`llvm-readobj -h` 仍是 `EM_RISCV`。Track A 的 lit/e2e 不被 Track B 影响。

## 参考实现（guided）

以下是“最小正确形状”，不是完整 patch。编码、字段宽度、predicate 和测试意图不要改。

### 1. 编码真值：opcode overlay
`llvm/lib/Target/MiniRV/auto-td/opcodes-ysx/extensions/rv_xdemo`：

```
xdemo.sadd rd rs1 rs2 funct2 31..27=0 14..12=0 6..0=0x0b
```

`funct2` 在 `arg_lut.csv` 里是 `26..25`，正好是 `uimm2` 的位置。不要把 `uimm2` 放进 `imm2`（`21..20`），那会和 `rs2` 重叠。

`.../auto-td/instructions/rv-xdemo/xdemo_sadd.yaml`：

```yaml
mnemonic: xdemo.sadd
opcode: {repo: ysx-opcodes, extension: rv_xdemo, key: xdemo_sadd}
spec: minirv.xdemo.r_type_imm2
domain: rv-xdemo
status: auto_full
```

`.../auto-td/taxonomy/xdemo.yaml`：

```yaml
categories:
  r_type_imm2:
    operands:
      outs: [{role: dest, field: rd, reg_class: GPR}]
      ins:
        - {role: lhs, field: rs1, reg_class: GPR}
        - {role: rhs, field: rs2, reg_class: GPR}
        - {role: imm, field: funct2, operand: uimm2, var: imm}
    effects: {}
```

`var: imm` 让生成的记录用 `$imm`，而不是 `$funct2`；这是 auto-td 里 `zimm5 -> imm` 的同一种写法。

`.../auto-td/config.yaml` 的两处改动：

```yaml
domains: {rv-i: {predicate: null}, rv-m: {predicate: null}, rv-xdemo: {predicate: HasXdDemo}}
coverage: {extensions: [rv_i, rv_m, ysx-opcodes/rv_xdemo]}
```

`.../MiniRV/CMakeLists.txt`：

```cmake
llvm_autotd_generate(MiniRV
  TARGET_ROOT ${CMAKE_CURRENT_SOURCE_DIR}
  RISCV_OPCODES ${CMAKE_CURRENT_SOURCE_DIR}/auto-td/opcodes
  YSX_OPCODES ${CMAKE_CURRENT_SOURCE_DIR}/auto-td/opcodes-ysx)
```

生成物 `AutoTdInstrInfo.inc` 的关键部分应是（**不要手写**，只核对）：

```tablegen
def XDEMO_SADD : MiniRVInst<(outs GPR:$rd),
                           (ins GPR:$rs1, GPR:$rs2, uimm2:$imm),
                           "xdemo.sadd", "$rd, $rs1, $rs2, $imm", [],
                           InstFormatR> {
  let Predicates = [HasXdDemo];
  bits<5> rd; bits<5> rs1; bits<5> rs2; bits<2> imm;
  let Inst{31-27}=0b00000; let Inst{26-25}=imm; let Inst{24-20}=rs2;
  let Inst{19-15}=rs1; let Inst{14-12}=0b000; let Inst{11-7}=rd;
  let Inst{6-0}=0b0001011;
}
```

### 2. Feature、predicate 与 Subtarget
`MiniRVInstrInfo.td`（放在 `include "auto-td/AutoTdInstrInfo.inc"` 之前）：

```tablegen
def FeatureXdDemo : SubtargetFeature<"xdemo", "HasXdDemo", "true",
                                    "Enable the XDemo extension">;
def HasXdDemo : Predicate<"Subtarget->hasXdDemo()">,
                AssemblerPredicate<(all_of FeatureXdDemo), "'XDemo' extension">;
```

`FeatureXdDemo` 的第二个模板参数是生成的字段名；`GET_SUBTARGETINFO_HEADER` 会生成 `HasXdDemo` 字段和 `hasXdDemo()` 访问器，**不要**再手写成员。

`MiniRVISelDAGToDAG.cpp` 还要给 generated predicate 提供 `Subtarget`：

```cpp
const MiniRVSubtarget *Subtarget = nullptr;
bool runOnMachineFunction(MachineFunction &MF) override {
  Subtarget = &MF.getSubtarget<MiniRVSubtarget>();
  return SelectionDAGISel::runOnMachineFunction(MF);
}
```

`AssemblerPredicate` 决定 `llvm-mc` 的诊断文本；没有它，`-mattr=-xdemo` 不会拒绝。

### 3. `uimm2` 操作数、parser 与 decoder
`MiniRVInstrInfo.td`（放在 `include "auto-td/AutoTdInstrInfo.inc"` 之前）：

```tablegen
def UImm2AsmOperand : MiniRVImmAsmOperand<"UImm2", "isUImm2">;
def uimm2 : Operand<i64>, ImmLeaf<i64, [{ return isUInt<2>(Imm); }]> {
  let ParserMatchClass = UImm2AsmOperand;
  let DecoderMethod = "DecodeUImm2";
}
```

`MiniRVAsmParser.cpp`：

```cpp
bool isUImm2() const {
  if (Kind != k_Imm) return false;
  int64_t Val;
  return Imm->evaluateAsAbsolute(Val) && isUInt<2>(Val);
}
```

`MiniRVDisassembler.cpp`：

```cpp
static DecodeStatus DecodeUImm2(MCInst &MI, uint64_t Imm, uint64_t Address,
                                const MCDisassembler *Decoder) {
  MI.addOperand(MCOperand::createImm(static_cast<int64_t>(Imm)));
  return MCDisassembler::Success;
}
```

不要复用 `DecodeUImm5`/`DecodeUImm6`：它们不会替你检查范围，而且名字会误导下一个读者。打印器不用手写：`MiniRVInstPrinter::printOperand` 已能打印立即数，`AsmWriter` 从 `AsmString` 生成 `$imm`。

### 4. DAG pattern
`MiniRVInstrInfo.td`，放在现有 `add`/`shl` 规则之后：

```tablegen
let Predicates = [HasXdDemo] in
def : Pat<(i64 (shl (add GPR:$rs1, GPR:$rs2), uimm2:$imm)),
          (XDEMO_SADD GPR:$rs1, GPR:$rs2, uimm2:$imm)>;
```

这条 `Pat` 比 `(shl GPR, uimm6)` 更具体：`uimm2` 是 `uimm6` 的子集，内层还要求 `add`。TableGen 会按复杂度排序，正常情况下它先匹配。若 `+xdemo` 仍出 `add` + `slli`，按这个顺序查：

1. `-debug-only=isel` 看 `shl` 是否仍是 `shl`、内层是否仍是 `ISD::ADD`（DAGCombiner 可能重结合）。
2. 确认 `HasXdDemo` 真的生效，且移位量是 i64 常量；再看 `uimm2` 的 `ImmLeaf` 是否接受该常量。
3. 仍然失败，再考虑用 target intrinsic（优秀路径），不要直接把 pattern 改成 `ComplexPattern` 大锤。

### 5. 测试文件
`llvm/test/MC/MiniRV/xdemo-sadd.s`：

```asm
# RUN: llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding %s | FileCheck %s
# RUN: llvm-mc -triple=minirv64 -mattr=+xdemo -filetype=obj %s -o %t.o
# RUN: llvm-objdump -d --triple=minirv64 %t.o | FileCheck --check-prefix=DIS %s

# CHECK: xdemo.sadd x10, x11, x12, 2
# CHECK-SAME: encoding: [0x0b,0x85,0xc5,0x04]
xdemo.sadd x10, x11, x12, 2

# CHECK: xdemo.sadd x1, x2, x3, 1
# CHECK-SAME: encoding: [0x8b,0x00,0x31,0x02]
xdemo.sadd x1, x2, x3, 1

# DIS: xdemo.sadd x10, x11, x12, 2
# DIS: xdemo.sadd x1, x2, x3, 1
```

`llvm/test/MC/MiniRV/xdemo-sadd-invalid.s`：

```asm
# RUN: not llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding %s 2>&1 | FileCheck %s --check-prefix=BAD
# RUN: not llvm-mc -triple=minirv64 -show-encoding %s 2>&1 | FileCheck %s --check-prefix=NOEXT

# BAD: error: invalid operand for instruction
xdemo.sadd x10, x11, x12, 4

# NOEXT: instruction requires
xdemo.sadd x10, x11, x12, 0
```

`llvm-mc` 会继续报告后续行，但一个文件混两类错误容易让 FileCheck 互相掩盖；更稳的是 `split-file`，或把两类错误拆成两个文件。

`llvm/test/CodeGen/MiniRV/xdemo-sadd.ll`：

```llvm
; RUN: llc -mtriple=minirv64 -mattr=+xdemo -O2 < %s | FileCheck %s
; RUN: llc -mtriple=minirv64 -mattr=-xdemo -O2 < %s | FileCheck %s --check-prefix=OFF
; RUN: llc -mtriple=minirv64 -mattr=+xdemo -O2 -filetype=obj < %s \
; RUN:   | llvm-objdump -d --triple=minirv64 - | FileCheck %s --check-prefix=OBJ

define i64 @sadd2(i64 %a, i64 %b) {
  %s = add i64 %a, %b
  %r = shl i64 %s, 2
  ret i64 %r
}
define i64 @sadd4(i64 %a, i64 %b) {
  %s = add i64 %a, %b
  %r = shl i64 %s, 4
  ret i64 %r
}

; CHECK-LABEL: sadd2:
; CHECK:         xdemo.sadd      x10, x10, x11, 2
; CHECK-LABEL: sadd4:
; CHECK:         slli    x10, x3, 4
; OFF-LABEL: sadd2:
; OFF:           add     x3, x10, x11
; OFF-NEXT:      slli    x10, x3, 2
; OBJ: xdemo.sadd x10, x10, x11, 2
```

`sadd4` 是范围守卫：`uimm2` 装不下 4，必须退回 `slli`，而不是把 4 截成 0。

### 6. e2e 程序与脚本
`dev/e2e/xdemo_shadow.ll` 用 i64 循环走默认 fallback（结果应为 112；i32 会被 custom-lower 成 ADDW/SLLIW，匹配不到 i64 pattern）：

```llvm
define i32 @main() {
entry: br label %loop
loop:
  %i = phi i64 [ 0, %entry ], [ %i1, %loop ]
  %acc = phi i64 [ 0, %entry ], [ %acc1, %loop ]
  %x = add i64 %acc, %i
  %y = shl i64 %x, 2
  %acc1 = add i64 %acc, %y
  %i1 = add i64 %i, 1
  %c = icmp slt i64 %i1, 8
  br i1 %c, label %loop, label %done
done:
  %t = trunc i64 %acc1 to i32
  %r = and i32 %t, 255
  ret i32 %r
}
```

`dev/xdemo/check.sh` 的核心断言：

```bash
"$B/llc" -mtriple=minirv64 -mattr=+xdemo -O2 -filetype=obj "$f" -o "$OUT/x.o"
"$B/llvm-objdump" -d --triple=minirv64 "$OUT/x.o" > "$OUT/x.s"
test "$(grep -c 'xdemo.sadd' "$OUT/x.s")" -gt 0
"$B/llvm-mc" -triple=minirv64 -mattr=+xdemo -show-encoding "$OUT/one.s" \
  | grep -q '0x0b,0x85,0xc5,0x04'
"$B/llc" -mtriple=minirv64 -mattr=-xdemo -O2 -filetype=obj "$f" -o "$OUT/off.o"
# off.o 链接 start.o，qemu 退出码 112；x.o 只做静态检查
```

`dev/xdemo/bench.sh` 只数静态指令：

```bash
off=$("$B/llc" -mtriple=minirv64 -mattr=-xdemo -O2 "$f" -o - | grep -cE $'^\t[a-z]')
on=$("$B/llc" -mtriple=minirv64 -mattr=+xdemo -O2 "$f" -o - | grep -cE $'^\t[a-z]')
sites=$("$B/llc" -mtriple=minirv64 -mattr=+xdemo -O2 "$f" -o - | grep -c 'xdemo.sadd')
test $((off - on)) -eq "$sites"
```

这是**代码尺寸**基准，不是性能结论。Ch 25 已经说过：指令数不是周期数。

## 预期输出（guided）

以下输出是按当前生成器规则应得的结果；编码字节已用按位计算核对。

### `llvm-mc -show-encoding`

```console
$ $B/llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding xdemo.s
	xdemo.sadd	x10, x11, x12, 2       # encoding: [0x0b,0x85,0xc5,0x04]
	xdemo.sadd	x1, x2, x3, 1          # encoding: [0x8b,0x00,0x31,0x02]
```

四个移位量的低 3 字节不变，最高字节按 `imm << 1` 变化：

| 源 | 编码 |
|---|---|
| `xdemo.sadd x10, x11, x12, 0` | `[0x0b,0x85,0xc5,0x00]` |
| `xdemo.sadd x10, x11, x12, 1` | `[0x0b,0x85,0xc5,0x02]` |
| `xdemo.sadd x10, x11, x12, 2` | `[0x0b,0x85,0xc5,0x04]` |
| `xdemo.sadd x10, x11, x12, 3` | `[0x0b,0x85,0xc5,0x06]` |

### `llc` 汇编

```console
$ $B/llc -mtriple=minirv64 -mattr=+xdemo -O2 xdemo.ll -o -
...
sadd2:
	.cfi_startproc
# %bb.0:
	xdemo.sadd	x10, x10, x11, 2
	jalr	x0, 0(x1)
```

无 feature 时：

```console
$ $B/llc -mtriple=minirv64 -mattr=-xdemo -O2 xdemo.ll -o -
...
sadd2:
	.cfi_startproc
# %bb.0:
	add	x3, x10, x11
	slli	x10, x3, 2
	jalr	x0, 0(x1)
```

### MIR

```console
$ $B/llc -mtriple=minirv64 -mattr=+xdemo -O2 -stop-after=finalize-isel xdemo.ll -o -
...
    %2:gpr = XDEMO_SADD %0, %1, 2
```

### lit

```console
$ $B/llvm-lit -v $TREE/llvm/test/MC/MiniRV/xdemo-sadd.s
-- Testing: 1 tests, 1 workers --
PASS: LLVM :: MC/MiniRV/xdemo-sadd.s (1 of 1)

$ $B/llvm-lit -s -j 16 $TREE/llvm/test/CodeGen/MiniRV $TREE/llvm/test/MC/MiniRV 2>/dev/null
-- Testing: 56 tests, 16 workers --
...
Total Discovered Tests: 56
  Passed: 56 (100.00%)
```

## 实测基线：现有 MiniRV 的真实输出

这些输出是本章写作时在本机跑出来的，用来锚定“加指令之前应该长什么样”。

### 1. `llc` 的 fallback：`add` + `slli`

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/minirv_sadd_base.ll -o -
	.file	"minirv_sadd_base.ll"
	.text
	.globl	sadd_base                       # -- Begin function sadd_base
sadd_base:                              # @sadd_base
	.cfi_startproc
	add	x3, x10, x11
	slli	x10, x3, 2
	jalr	x0, 0(x1)
```

这就是 `xdemo.sadd` 要替换的两条指令。注意 `add` 的 `rd` 是 `x3`，因为结果还要被 `slli` 使用；融合后目标寄存器直接是 `x10`，少一次中间值。

### 2. `llvm-mc` 的现有编码

```console
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/minirv_enc.s
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
	slli	x10, x10, 2                     # encoding: [0x13,0x15,0x25,0x00]
```

`add` 的低字节是 `0x33`，`xdemo.sadd` 的低字节是 `0x0b`；这就是 custom-0 与 standard opcode 的差别。两者的 `0x85,0xc5` 相同，因为 `rd/rs1/rs2` 的位置相同。

### 3. MIR：选指后的两条机器指令

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel /tmp/minirv_sadd_base.ll -o -
...
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11
    %2:gpr = ADD %0, %1
    %3:gpr = SLLI %2, 2
    $x10 = COPY %3
    PseudoRET implicit $x10
```

`XDEMO_SADD` 成功时，`%2:gpr = ADD` 和 `%3:gpr = SLLI` 应该变成一条 `%2:gpr = XDEMO_SADD %0, %1, 2`。如果 MIR 里还有 `ADD`，说明 `Pat` 没匹配，不要去看 AsmPrinter。

### 4. e2e 全绿（17/17）

```console
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
  QEMU=/usr/bin/qemu-riscv64-static \
  LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
  llvm/lib/Target/MiniRV/dev/e2e/run.sh
artifacts: /tmp/tmp.H82Cq9J9cW
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

加 `xdemo_shadow.ll` 后应该是 18 行，最后一行 `OK xdemo_shadow exit=112`。`112` 是循环 `acc = acc + ((acc+i)<<2)` 在 `i=0..7` 后的低 8 位，已用 Python 手算核对。

### 5. 静态指令数基线

```console
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
  llvm/lib/Target/MiniRV/dev/bench/run.sh
| kernel | MiniRV insns | riscv64 insns | ratio |
|---|---|---|---|
| add | 12 | 12 | 1.00 |
| mul | 12 | 12 | 1.00 |
| reduction | 10 | 12 | 0.83 |
| memcpy | 9 | 9 | 1.00 |
| axpy | 12 | 12 | 1.00 |
```

`xdemo` 的 benchmark 不是再造一张“性能表”，而是证明 `off - on == sites`。如果你的 kernel 有 5 个可融合站点，`+xdemo` 的汇编行数应该正好少 5。

### 6. 可选 Clang track 的起点：现在会失败

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -c /tmp/minirv_t.c -o /tmp/minirv_t.o
error: unknown target triple 'minirv64'
```

这不是后端问题：`llc -mtriple=minirv64` 已经能编译同一段 IR。Track B 要补的是 Clang 的 `TargetInfo` 工厂，不是 MiniRV 的指令选择。

## 可选 Track B：Clang `TargetInfo`（guided）

最小目标：让下面这段 C 通过 `clang --target=minirv64 -c`，且 `llc -mtriple=minirv64` 能产出 `addw x10, x11, x10`：

```c
int add(int a, int b) { return a + b; }
```

### 文件
| 文件 | 改动 |
|---|---|
| `clang/lib/Basic/Targets/MiniRV.{h,cpp}` | `MiniRVTargetInfo : TargetInfo`、64 位模型、LP64、`__minirv*` 宏 |
| `clang/lib/Basic/Targets.cpp`、`CMakeLists.txt` | `AllocateTarget` case、加源文件 |
| `clang/lib/Driver/ToolChains/Arch/MiniRV.cpp`、`Clang.cpp`、`Driver.cpp` | `getMiniRVArch`/ABI、`AddMiniRVTargetArgs`、`BareMetal.cpp` |
| `clang/lib/CodeGen/Targets/MiniRV.cpp`、`CodeGenModule.cpp` | 整数子集 ABIInfo（优秀） |

`TargetInfo` 的关键形状：

```cpp
class MiniRVTargetInfo : public TargetInfo {
protected:
  std::string ABI;
public:
  MiniRVTargetInfo(const llvm::Triple &T, const TargetOptions &O)
      : TargetInfo(T) {
    LongWidth = LongAlign = PointerWidth = PointerAlign = 64;
    IntMaxType = Int64Type = SignedLong;
    resetDataLayout("e-m:e-p:64:64-i64:64-i128:128-n32:64-S128");
  }
  bool setABI(const std::string &N) override {
    if (N != "lp64") return false;
    ABI = N; return true;
  }
  void getTargetDefines(const LangOptions &, MacroBuilder &) const override;
  BuiltinVaListKind getBuiltinVaListKind() const override {
    return TargetInfo::VoidPtrBuiltinVaList;
  }
};
```

`getTargetDefines` 至少定义 `__minirv`、`__minirv_xlen 64`、`__minirv_mul`、`__minirv_div`。**不要**定义 `__riscv`：那会让大量 C 代码走 RISC-V 分支，假设 A/C/F/D 存在。

driver 最小版本：

```cpp
void Clang::AddMiniRVTargetArgs(const ArgList &Args,
                                ArgStringList &CmdArgs) const {
  CmdArgs.push_back("-target-abi");     CmdArgs.push_back("lp64");
  CmdArgs.push_back("-target-cpu");     CmdArgs.push_back("generic");
  CmdArgs.push_back("-target-feature"); CmdArgs.push_back("+m");
}
```

验收：

```console
$ clang --target=minirv64 -### -c t.c 2>&1 | grep -E 'target-abi|target-feature|target-cpu'
$ clang --target=minirv64 -c t.c -o t.o
$ $B/llvm-readobj -h t.o | grep -E 'Format|Machine'
```

预期 `Format: elf64-littleriscv`、`Machine: EM_RISCV`；这与 Track A 的对象格式一致，不需要新 ELF machine。

## 最小可行提交 vs 优秀提交

**最小可行提交（及格线）**：一条指令、一个 feature、一个 `Pat`；两个 MC 测试（正向 + 负向）、一个 CodeGen 测试；全量 lit 56/56；e2e 18/18（`xdemo_shadow.ll` 走 fallback）；`check.sh` 证明 custom 路径能编码/选指；`bench.sh` 证明静态指令数差等于站点数；`dev/xdemo/README.md` 写清 qemu 限制；不做 Clang。

**优秀提交**：在最小提交之上，补 `uimm2=0..3` 的参数化测试、`uimm2=4` 的拒绝测试、`-stop-after=finalize-isel` 的 MIR 断言、`llvm-mc -disassemble` 的反向测试；Track B 的 `TargetInfo` + driver 让整数标量 C 工作；整数 `ABIInfo`（前 8 个整数参数 `x10..x17`，返回值 `x10/x11`，小聚合体 coerce）；6–8 个可独立 revert 的 commit；有 patched qemu/FPGA 就再加 runtime 断言，没有就明确写“未验证”。

**优秀的定义是“证据更硬”，不是“指令更多”。** 第二条指令、RVV、TLS 都不会让评分更高，只会让 review 更难。

## 评分 rubric

| 维度 | 权重 | 及格 | 优秀 |
|---|---:|---|---|
| MC | 20% | 能汇编/反汇编，字节正确；无 feature 时拒绝 | 正反向测试、`uimm2` 边界、decoder 往返、诊断清楚 |
| CodeGen | 30% | `+xdemo` 出一条 `xdemo.sadd`，`-xdemo` 回退 | MIR 断言、单/多 use 分析、`sadd4` 范围守卫、无回归 |
| 测试 | 20% | MC + CodeGen lit 通过，全量 56/56 | 负向测试、`check-autotd`、`update_llc_test_checks` 兼容性说明 |
| e2e | 20% | 18/18；custom 路径静态检查 | 语义 oracle、站点数不变量、与 riscv64 的差分解释 |
| 文档/评审 | 10% | README 写清限制；diff 聚焦 | patch series、review checklist、commit message、Ch 27 合规 |

**一票否决**：改了 `Triple.cpp`、`RISCV*` 或 ELF writer 来实现一条 ALU 指令；默认开启 `+xdemo` 导致现有 e2e 变红；把 `uimm2=4` 静默截断成 0；声称“qemu 跑通了 xdemo”但没有 patched 模拟器；把生成物 `AutoTdInstrInfo.inc` 手改后提交。

## 验收命令清单

```console
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ BUILD=/home/ubuntu/llvm/builds/dsl-tablegen

$ ninja -C $BUILD llc llvm-mc llvm-objdump llvm-readobj
$ ninja -C $BUILD check-autotd
$ $B/llvm-mc -triple=minirv64 -mattr=+xdemo -show-encoding $TREE/llvm/test/MC/MiniRV/xdemo-sadd.s
$ $B/llvm-mc -triple=minirv64 -mattr=+xdemo -filetype=obj $TREE/llvm/test/MC/MiniRV/xdemo-sadd.s -o /tmp/x.o
$ $B/llvm-objdump -d --triple=minirv64 /tmp/x.o
$ $B/llc -mtriple=minirv64 -mattr=+xdemo -O2 $TREE/llvm/test/CodeGen/MiniRV/xdemo-sadd.ll -o -
$ $B/llc -mtriple=minirv64 -mattr=-xdemo -O2 $TREE/llvm/test/CodeGen/MiniRV/xdemo-sadd.ll -o -
$ $B/llvm-lit -s -j 16 $TREE/llvm/test/CodeGen/MiniRV $TREE/llvm/test/MC/MiniRV 2>/dev/null
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld QEMU=/usr/bin/qemu-riscv64-static \
  LLVM_BUILD_DIR=$BUILD $TREE/llvm/lib/Target/MiniRV/dev/e2e/run.sh
$ $TREE/llvm/lib/Target/MiniRV/dev/xdemo/check.sh
$ $TREE/llvm/lib/Target/MiniRV/dev/xdemo/bench.sh
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -### -c /tmp/minirv_t.c
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -c /tmp/minirv_t.c -o /tmp/minirv_t.o
$ $B/llvm-readobj -h /tmp/minirv_t.o | grep -E 'Format|Machine'
```

预期汇总：`check-autotd` OK；MC 字节为 `[0x0b,0x85,0xc5,0x04]`；`+xdemo` 一条、`-xdemo` 两条；lit 56/56；e2e 18/18；`bench.sh` 的 `off - on == sites`；可选 Clang 产出 `EM_RISCV` 对象。

## 反直觉与踩雷

1. **错误直觉**：加新指令就是手写 `bits<32> Inst` 和 `let Inst{...}`。 → **正确认识**：这个仓库的 MiniRV 记录由 auto-td 从 opcode 数据库生成；YAML 里写 `encoding`/`bits<` 会被 `check-autotd` 直接拒绝。你写的是“编码真值的一行”和“操作数形状”，不是编码本身。
2. **错误直觉**：新指令应该默认开启，否则怎么算“加了指令”。 → **正确认识**：默认开启会让 stock qemu 在 custom-0 上 trap，现有 e2e 全红。用 `+xdemo` 门控，默认路径保持 RV64IM，才能同时证明“新功能”和“零回归”。
3. **错误直觉**：e2e 应该直接跑 `xdemo.sadd`，否则不算端到端。 → **正确认识**：`qemu-riscv64` 不认识 custom-0。e2e 证明的是默认 fallback 的语义和 17 个旧程序不回归；custom 路径由 MC 往返 + CodeGen + 静态基准证明。没有 patched 模拟器就诚实写“未 runtime 验证”。
4. **错误直觉**：`Pat` 更具体就一定会赢。 → **正确认识**：如果 `add` 有多个 use，DAG 会先把它选成 `ADD`，内层 `add` 就不再是 `ISD::ADD`，融合 pattern 不会触发。用单 use 测试；失败时先看 `-debug-only=isel`，不要怪 TableGen。
5. **错误直觉**：`uimm2` 可以复用 `uimm5`，反正编码只取低两位。 → **正确认识**：parser 会接受 `4`/`5`，encoder 静默截断，生成错误代码。必须新增 2 位 operand class，并用 `4`/`-1` 的负向测试锁住。
6. **错误直觉**：`xdemo.sadd` 需要新 relocation 或新 ELF machine。 → **正确认识**：它没有符号操作数，就是一条普通 32 位指令；对象仍是 `elf64-littleriscv`/`EM_RISCV`。需要新 relocation 的是 `auipc+addi` 那类 PC-relative 序列，不是这条。

## Review checklist（按 Ch 27）

- [ ] `llvm-mc -show-encoding` 的字节与规格逐位一致；`llvm-objdump` 能反向打印。
- [ ] 无 `+xdemo` 时诊断包含 `instruction requires`，且不是 crash。
- [ ] `uimm2` 的 `0..3` 全过，`4`/`-1` 被拒绝。
- [ ] `llc -mattr=+xdemo` 一条；`-mattr=-xdemo` 两条；MIR 能证明是选指。
- [ ] `sadd4`（shift=4）不融合，证明范围守卫有效。
- [ ] 全量 lit 56/56；`check-autotd` 通过；`git diff --check` 干净。
- [ ] e2e 18/18；`xdemo_shadow` 的退出码是 112。
- [ ] `bench.sh` 的 `off - on == sites`，没有把静态数字写成性能结论。
- [ ] diff 只在 `llvm/lib/Target/MiniRV` 和 `llvm/test/{MC,CodeGen}/MiniRV`（Track A）。
- [ ] 生成物没有被手改；`AutoTdInstrInfo.inc` 只由构建生成。
- [ ] commit message 说清“行为变化 + 测试”，AI 辅助按 `Assisted-by:` 标注。
- [ ] 文档明确写“stock qemu 不执行 custom-0”。

## Patch series：commit-by-commit

按 Ch 27 的原则：**一个 commit = 一个可独立验证、可独立 revert 的行为**。

| # | Commit | 内容 | 验证 |
|---|---|---|---|
| P0 | `[MiniRV] NFC: wire vendor opcode overlay` | CMake 加 `YSX_OPCODES` + 空 `opcodes-ysx/extensions/.gitkeep`；config 不动 | `check-autotd` 仍通过 |
| P1 | `[MiniRV] Add XDemo feature and uimm2 operand` | `FeatureXdDemo`、`HasXdDemo`、`uimm2`、parser/decoder | 无指令可汇编；feature 可解析 |
| P2 | `[MiniRV] Add xdemo.sadd MC record` | opcode 行、YAML、taxonomy、MC 正向/负向测试 | `llvm-mc` 字节正确；lit 55/55 |
| P3 | `[MiniRV] Select xdemo.sadd from shl(add)` | `Pat` + ISel `Subtarget` 成员 + CodeGen 测试 + `sadd4` 守卫 | `llc` 一条；lit 56/56 |
| P4 | `[MiniRV] Add XDemo e2e shadow and static bench` | `xdemo_shadow.ll`、`check.sh`、`bench.sh` | e2e 18/18；`off-on==sites` |
| P5 | `[Clang] Add MiniRV TargetInfo` | `Targets/MiniRV.*`、`AllocateTarget`、driver 默认 | `clang --target=minirv64 -c` 过 |
| P6 | `[Clang] Add MiniRV integer ABIInfo` | `CodeGen/Targets/MiniRV.cpp`、`createTargetCodeGenInfo` | 整数标量/小聚合体测试 |
| P7 | `[Docs] Record MiniRV+ bring-up notes` | `dev/xdemo/README.md`、限制、baseline | 文档链接可点，命令可复现 |

每个 commit 的提交前检查：

```console
$ git diff --check
$ clang-format -i <changed .cpp/.h>
$ ninja -C $BUILD llc llvm-mc llvm-objdump
$ $B/llvm-lit -s -j 16 $TREE/llvm/test/CodeGen/MiniRV $TREE/llvm/test/MC/MiniRV 2>/dev/null
```

不要把所有改动塞进一个 “add xdemo” commit。reviewer 无法在半小时内判断“编码、pattern、e2e、Clang”四件事是否各自正确；拆成 P0–P4，任何一块坏了都能单独 revert。

## 本章重点整理

- **Final 的产物不是“一条指令”，而是一条可验收的分层链路**：opcode 行 → 生成记录 → MC 往返 → 选指 → lit → e2e → 基准。
- **编码真值必须来自 opcode 数据库**；YAML 不写 encoding，生成物不手改。
- **扩展必须默认关闭**，用 `+xdemo` 门控；e2e 证明默认路径不回归，custom-0 的 runtime 不能假装。
- **最小提交做窄，优秀提交做深**：更多测试、更硬的证据、可拆的 commit；Ch 27 的标准从第一天就适用。

## 自我检核

- [ ] 不看笔记，画出 `xdemo.sadd` 从汇编文本到 4 字节的字段布局，并说出 `uimm2` 为什么在 26..25。
- [ ] 解释为什么 YAML 里不能写 `encoding`，以及 `check-autotd` 会怎么拒绝它。
- [ ] 解释 `+xdemo` 默认关闭的两个理由（qemu、回归）。
- [ ] 如果 `llc -mattr=+xdemo` 仍输出 `add; slli`，按什么顺序定位？说出至少三个检查点。
- [ ] 为什么 `uimm2=4` 必须回退到 `slli`，而不是截断？
- [ ] e2e 18/18 证明了什么、没证明什么？用一句话分别回答。
- [ ] 说出 P2 和 P3 的边界：为什么 MC 记录和 DAG pattern 要分开提交？

## 延伸阅读
- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方。读 `TargetMachine`、`TargetLowering`、`SelectionDAGISel` 三节；能解释 `Pat` 属于选指层而不是 MC 层。前提：Ch 09/13，能看 `-stop-after=finalize-isel` 的 MIR。
- **[LLVM TableGen](https://llvm.org/docs/TableGen/)** — LLVM 官方。读 `Instruction`、`Pat`、`ImmLeaf`、`AsmWriter`、`AsmMatcher`；能解释 `uimm2` 为什么同时是 `Operand` 和 `ImmLeaf`。前提：Ch 06 的 `-gen-instr-info`/`-gen-dag-isel`。
- **[llvm-mc Command Guide](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — LLVM 官方。读 `-show-encoding`、`-disassemble`、`-filetype=obj`；这是编码层最便宜的验收方式。前提：跑过 Ch 08 的 `basic.s`。
- **[RISC-V ISA Manual](https://riscv.org/technical/specifications/)** — RISC-V International。读 custom opcode 空间和 R/I 型编码；能解释为什么 `0x0b` 安全、为什么 `uimm2` 不能塞进已有字段。前提：Ch 02，不需要读完整手册。
- **[`llvm/utils/auto-td/README.md`](/home/ubuntu/llvm/trees/dsl-tablegen/llvm/utils/auto-td/README.md)** — 本树。读 `Instruction YAML`、`Taxonomy`、`Validation`、`Vendor extensions`；这是本章生成器契约的权威来源。前提：能读 YAML/CMake。

→ [Ch 28 复盘：42 个 commit 的 bring-up 时间线](./28-bringup-retrospective.md)
