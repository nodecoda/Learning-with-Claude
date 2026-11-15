# Final Project — 给 RISC-V 加一条带调度模型的自定义指令

> **目标**：从零走完一条指令的完整生命周期：定义 vendor 扩展、写 TableGen 指令、接上 SelectionDAG pattern、绑定 `SchedWrite/SchedRead`、给所有 CPU 模型补 `WriteRes`、用 `llvm-mca` 验证延迟、用 lit 锁住选择与调度。做完你会得到一条能编译、能汇编、能被 mca 分析、能回归测试的 `xdemo.sadd`。
>
> **环境**：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/explore`，构建 `/home/ubuntu/llvm/builds/explore`（RelWithDebInfo + assertions，`BUILD_SHARED_LIBS=ON`）。本章的 patch 在本机实际编译、运行、测试过；验证后已 `git checkout` 还原并重建基线。所有输出为真实终端记录。
>
> **前置**：第 6、7 章（pattern）、第 12、25 章（sched model 三层）、第 26 章（MIR/lit）、第 28 章（PR 规矩）。如果只想知道结论，读「验收标准」和「常见失败模式」两节。

## 项目定义：为什么是 `xdemo.sadd`

我们要加的指令：

```text
xdemo.sadd rd, rs1, rs2, uimm2     # rd = rs1 + (rs2 << uimm2)
```

选择它的理由：

1. **有真实原型**：T-Head 的 `th.addsl` 就是这个形状（`RISCVInstrInfoXTHead.td`），Zba 的 `sh1add/sh2add/sh3add` 也是同一类。你不是在发明怪东西，而是在复刻一条真实存在的指令。
2. **能被普通 IR 触发**：`add a, (shl b, 3)` 不需要 intrinsic、不需要内联汇编；SelectionDAG 里就有对应节点。
3. **能体现 CPU 差异**：同一条指令在 `sifive-u74` 上延迟 2、在 `rocket-rv64` 上延迟 1，正好演示「选择相同、调度不同」。
4. **编码简单**：CUSTOM-0 opcode + 2-bit immediate，不会陷入 RISC-V 编码的细节泥潭。

它不是真扩展。命名用 `XDemo`，目的是让 `-mattr=help` 里一眼看出这是教学用。

## 交付物清单

```text
llvm/lib/Target/RISCV/
  RISCVFeatures.td            +7    FeatureVendorXDemo / HasVendorXDemo
  RISCVInstrInfoXDemo.td      新文件  指令 + pattern
  RISCVInstrInfo.td           +1    include 新文件
  RISCVSchedule.td            +2    WriteXDemoSAdd / ReadXDemoSAdd
  RISCVSched*.td              +2×16 每个模型的 WriteRes + ReadAdvance
  RISCVSchedSiFive7.td        +4    latency 2 / PipeA 的专用定价
llvm/test/CodeGen/RISCV/
  xdemo-sadd.ll               新文件  选择测试（有/无 +xdemo）
llvm/test/tools/llvm-mca/RISCV/
  xdemo-sadd.test             新文件  mca 延迟测试
```

本机 `git diff --stat`（不含两个新测试文件和未跟踪的新 .td）：

```text
 19 files changed, 44 insertions(+)
```

44 行里，真正「有信息量」的只有 feature、指令、pattern、SiFive7 定价；其余 15 个文件各两行，是 sched model 的**覆盖税**。这个比例本身就是本项目的教学点。

## 先建立直觉：一条指令要过五关

```text
① feature      -mattr=+xdemo 让 Subtarget 认识它
       │
② 指令定义     opcode/操作数/编码/asm 字符串
       │
③ pattern      DAG 节点 → 这条指令
       │
④ sched class  Sched<[Write..., Read...]>，给调度器一个抽象类
       │
⑤ 每 CPU 定价  WriteRes/ReadAdvance，把抽象类变成 latency/资源
       │
       ▼
   llc 生成  →  llvm-mc 编码  →  llvm-mca 周期  →  lit 回归
```

五关任何一关断了，现象都不同：

| 断在哪 | 现象 |
|---|---|
| feature | `-mattr=+xdemo` 报 unknown feature |
| 指令定义 | TableGen 报编码字段冲突，或 asm 字符串对不上 |
| pattern | `llc` 仍然生成 `slli+add`，不报错 |
| sched class | TableGen 报 `Processor does not define resources` |
| 每 CPU 定价 | 能编译，但 mca 的 latency 是全模型默认值 |

## Step 0：基线——先确认「没有它」是什么样

```bash
$ cat > /tmp/ch30.ll <<'IR'
define i64 @sadd3(i64 %a, i64 %b) {
  %s = shl i64 %b, 3
  %r = add i64 %a, %s
  ret i64 %r
}
IR

$ llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 /tmp/ch30.ll -o -
```

本机输出：

```asm
sadd3:
	.cfi_startproc
# %bb.0:
	slli	a1, a1, 3
	add	a0, a0, a1
	ret
```

记住这个形状。加完扩展后，它应该变成一条 `xdemo.sadd`；如果没变，问题一定在 feature 或 pattern，不在调度。

## Step 1：注册 vendor 扩展

`RISCVFeatures.td` 的 vendor 区，在 `// T-Head Extensions` 之前插入：

```tablegen
// XDemo (tutorial-only vendor extension)
def FeatureVendorXDemo
    : RISCVExtension<1, 0, "XDemo shift-add instruction">;
def HasVendorXDemo : Predicate<"Subtarget->hasVendorXDemo()">,
                     AssemblerPredicate<(all_of FeatureVendorXDemo),
                         "'XDemo' (XDemo shift-add instruction)">;
```

`RISCVExtension` 的命名规则（同文件顶部）：

- record 名 `FeatureVendorXDemo` → 去掉 `FeatureVendor` 前缀 → `XDemo` → 小写 → feature 名 `xdemo`。
- 字段名默认把 `Feature` 换成 `Has`：`Subtarget->hasVendorXDemo()`。
- `RISCVExtension<1, 0, ...>` 的 1/0 是版本号；vendor 扩展通常写 1.0。

验证：

```bash
$ llc -mattr=help -mtriple=riscv64 | grep xdemo
  xdemo    - 'XDemo' (XDemo shift-add instruction).
```

**检查点 1**：`-mattr=help` 能看到 `xdemo`。看不到就说明 feature 没进 `RISCVFeatures.td` 的 include 链，或者名字拼错（TableGen 不会报错，只会静默不生成）。

## Step 2：定义指令与编码

新建 `llvm/lib/Target/RISCV/RISCVInstrInfoXDemo.td`：

```tablegen
//===-- RISCVInstrInfoXDemo.td - XDemo vendor extension ----*- tablegen -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// This file describes the XDemo vendor extension, used as the worked example
// in the LLVM ISel/scheduling course. It is not a real RISC-V extension.
//
//===----------------------------------------------------------------------===//

/// rd = rs1 + (rs2 << uimm2)
class XDemoShiftALU_rri<bits<3> funct3, string opcodestr>
    : RVInstRBase<funct3, OPC_CUSTOM_0, (outs GPR:$rd),
                  (ins GPR:$rs1, GPR:$rs2, uimm2:$uimm2),
                  opcodestr, "$rd, $rs1, $rs2, $uimm2"> {
  bits<2> uimm2;
  let Inst{31-30} = uimm2;
  let Inst{29-25} = 0b00000;
}

let Predicates = [HasVendorXDemo] in {
def XDEMO_SADD : XDemoShiftALU_rri<0b000, "xdemo.sadd">,
                 Sched<[WriteIALU, ReadIALU, ReadIALU]>;

def : Pat<(riscv_add_like_non_imm12 (shl GPR:$rs2, uimm2:$uimm2),
                                    (XLenVT GPR:$rs1)),
          (XDEMO_SADD GPR:$rs1, GPR:$rs2, uimm2:$uimm2)>;
} // Predicates = [HasVendorXDemo]
```

然后在 `RISCVInstrInfo.td` 的 vendor include 区加一行：

```tablegen
include "RISCVInstrInfoXDemo.td"
```

### 编码是怎么来的

`RVInstRBase` 已经填好 R-type 的固定字段：

```text
31-25 funct7 | 24-20 rs2 | 19-15 rs1 | 14-12 funct3 | 11-7 rd | 6-0 opcode
```

我们只覆盖 `31-30`（2-bit imm）和 `29-25`（补 0），其余由基类填。所以：

```text
xdemo.sadd a0, a1, a2, 3
  opcode = 0b0001011 (CUSTOM-0)
  funct3 = 000
  imm    = 11
  rs2    = a2 = x12 = 01100
  rs1    = a1 = x11 = 01011
  rd     = a0 = x10 = 01010
```

验证：

```bash
$ printf 'xdemo.sadd a0, a1, a2, 3\n' | llvm-mc -triple=riscv64 -mattr=+xdemo -show-encoding
	xdemo.sadd	a0, a1, a2, 3           # encoding: [0x0b,0x85,0xc5,0xc0]
```

**检查点 2**：`llvm-mc -show-encoding` 能编码，`llvm-mc -disassemble` 能解回来。如果 TableGen 报字段冲突，通常是 `Inst{31-25}` 被基类和子类同时设置。

### 为什么 pattern 用 `riscv_add_like_non_imm12`

直接写 `(add GPR:$rs1, (shl GPR:$rs2, uimm2:$imm))` 经常匹配不到，因为 RISC-V 的 DAG combine 会把 `add` 规范化成目标特定的 `riscv_add_like_non_imm12` 节点（Zba 的 `sh*add` pattern 也用它）。T-Head 的 `TH_ADDSL` 就是同一个写法：

```tablegen
def : Pat<(riscv_add_like_non_imm12 (shl GPR:$rs2, uimm2:$uimm2), (XLenVT GPR:$rs1)),
          (TH_ADDSL GPR:$rs1, GPR:$rs2, uimm2:$uimm2)>;
```

照抄这个形状，比自己猜 `add` 节点可靠。

## Step 3：第一次编译——选择成功

```bash
$ llc -mtriple=riscv64 -mattr=+m,+xdemo -mcpu=sifive-u74 /tmp/ch30.ll -o -
```

本机输出：

```asm
sadd3:
	.cfi_startproc
# %bb.0:
	xdemo.sadd	a0, a0, a1, 3
	ret
```

**检查点 3**：`+xdemo` 时是一条 `xdemo.sadd`，`-xdemo` 时退回 `slli+add`。两个方向都要测；只测「有」不测「无」，你无法知道 pattern 是否在默认 CPU 上误触发。

此时 `xdemo.sadd` 还没有自己的调度类——`Sched<[WriteIALU, ...]>` 只是借用整数 ALU 的定价。所以 mca 会显示 u74 的 IALU latency 3：

```text
1      3     1.00    xdemo.sadd
```

这不是我们想要的「2 周期」。下一步给它独立定价。

## Step 4：定义调度类

在 `RISCVSchedule.td` 加两行：

```tablegen
def WriteXDemoSAdd  : SchedWrite;    // XDemo shift-add
def ReadXDemoSAdd   : SchedRead;
```

把指令的 `Sched` 改成：

```tablegen
Sched<[WriteXDemoSAdd, ReadXDemoSAdd, ReadXDemoSAdd]>
```

这三个位置的含义：一个 def（写） + 两个 use（读）。如果写成 `Sched<[WriteXDemoSAdd, ReadXDemoSAdd]>`，TableGen 不会报错，但第二个源操作数的读依赖会丢，调度器可能把它提前。**操作数个数必须和 `ins` 一致**，这是最容易静默错的地方。

### 为什么先给所有模型一个「基线」定价

`SchedWrite` 是抽象类，必须有 CPU 模型把它映射到资源和延迟。最小可行做法是：每个模型先按 `WriteIALU` 的资源复制一份，保证能编译；再在目标 CPU 上覆盖成真实值。

这一步会撞上第 25 章的「覆盖税」。本机实际报错（只改了 SiFive7 时）：

```text
RISCVSchedAndes45.td:226:5: error: Processor does not define resources for WriteXDemoSAdd
```

补完写资源后，下一轮又报读资源：

```text
RISCVSchedAndes45.td:226:5: error: Processor does not define resources for ReadXDemoSAdd
```

也就是说：**`SchedWrite` 和 `SchedRead` 都要覆盖**。最终每个模型文件加两行（以 Rocket 为例，本机 diff）：

```diff
 def : WriteRes<WriteIALU32, [RocketUnitALU]>;
 def : WriteRes<WriteIALU, [RocketUnitALU]>;
+def : WriteRes<WriteXDemoSAdd, [RocketUnitALU]>;
 ...
 def : ReadAdvance<ReadIALU, 0>;
+def : ReadAdvance<ReadXDemoSAdd, 0>;
```

为什么不能只改一个模型？因为指令的 sched class 是全局的；任何能选到这条指令的 CPU，其模型都必须能解释这个类。`CompleteModel = 0` 只放过「完全没有 sched class 的指令」，不放过「有类但缺资源」。

### 一个 TableGen 作用域坑

本机在 XiangShanNanHu 上踩到：

```text
RISCVSchedXiangShanNanHu.td:52:5: error: Value 'Latency' unknown!
```

原因：`ReadAdvance` 被插进了 `let Latency = 1 in { ... }` 块里，而 `ReadAdvance` 没有 `Latency` 字段。`WriteRes` 可以继承 `let Latency`，`ReadAdvance` 不行。修法是把 `ReadAdvance` 移到 `let` 块外。

### SiFive7 的专用定价

在 `SiFive7WriteResBase` multiclass 的整数区加：

```tablegen
  // XDemo: example vendor instruction with its own latency on PipeA.
  def : WriteRes<WriteXDemoSAdd, [PipeA]> { let Latency = 2; }
  def : ReadAdvance<ReadXDemoSAdd, 0>;
```

注意它写在 multiclass 里，会被三个 VLEN 变体各实例化一次；不是重复定义，而是每个模型一份。

## Step 5：用 mca 验证「同指令、不同 CPU」

```bash
$ printf 'xdemo.sadd a0, a0, a1, 3\n' > /tmp/xd1.s
$ llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m,+xdemo -instruction-info /tmp/xd1.s
```

本机（u74）：

```text
[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      2     1.00                        xdemo.sadd	a0, a0, a1, 3
```

资源表里它占 `VLEN512SiFive7PipeA`：

```text
[0]    [1]    [2]    [3]    [4]    [5]    [6]    [7]    Instructions:
 -      -     1.00    -      -      -      -      -     xdemo.sadd	a0, a0, a1, 3
```

换 `rocket-rv64`：

```text
[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      1     1.00                        xdemo.sadd	a0, a0, a1, 3
```

资源是 `RocketUnitALU`，延迟 1。单条指令的 `Total Cycles`：

| CPU | latency | 资源 | `-iterations=1` 的 Total Cycles |
|---|---:|---|---:|
| `sifive-u74` | 2 | PipeA | 3 |
| `rocket-rv64` | 1 | ALU | 2 |

两条有依赖的 `xdemo.sadd`，timeline 直接画出差距：

```text
# sifive-u74
[0,0]     DeE .   xdemo.sadd	a0, a0, a1, 3
[0,1]     . DeE   xdemo.sadd	a0, a0, a2, 2

# rocket-rv64
[0,0]     DE.   xdemo.sadd	a0, a0, a1, 3
[0,1]     .DE   xdemo.sadd	a0, a0, a2, 2
```

**检查点 4**：u74 的 mca latency 必须是 2、资源必须是 PipeA；rocket 必须是 1、ALU。如果 u74 显示 3，说明你的 `WriteRes` 没生效，或者被别的 `InstRW` 覆盖了。

## Step 6：写测试——选择与调度各一条

### 选择测试（`.ll`）

`llvm/test/CodeGen/RISCV/xdemo-sadd.ll`：

```llvm
; RUN: llc -mtriple=riscv64 -mattr=+m,+xdemo -mcpu=sifive-u74 %s -o - \
; RUN:   | FileCheck %s
; RUN: llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 %s -o - \
; RUN:   | FileCheck %s --check-prefix=NOXD

define i64 @sadd3(i64 %a, i64 %b) {
  %s = shl i64 %b, 3
  %r = add i64 %a, %s
  ret i64 %r
}

; CHECK-LABEL: sadd3:
; CHECK:       xdemo.sadd a0, a0, a1, 3
; NOXD-LABEL:  sadd3:
; NOXD:        slli a1, a1, 3
; NOXD-NEXT:   add a0, a0, a1
```

两个 RUN 行分别锁「有扩展」和「无扩展」。`NOXD-NEXT` 是安全的，因为 `slli` 和 `add` 是紧邻的两条，不会被 COPY 插开。

### mca 测试（`.test`）

`llvm/test/tools/llvm-mca/RISCV/xdemo-sadd.test`：

```asm
# RUN: llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m,+xdemo \
# RUN:   -instruction-info < %s | FileCheck %s

xdemo.sadd a0, a0, a1, 3

# CHECK: {{^}} 1      2     1.00{{.*}}xdemo.sadd
```

这里有一个本机踩到的坑：最初写

```text
# CHECK:      Instructions:
# CHECK-NEXT:  1      2     1.00                        xdemo.sadd
```

测试失败，因为 `CHECK: Instructions:` 先匹配到了汇总行的 `Instructions:      100`，`CHECK-NEXT` 的下一行是 `Total Cycles`，不是指令表：

```text
error: CHECK-NEXT: is not on the line after the previous match
note: previous match ended here
  Instructions: 100
```

修法是不要匹配 `Instructions:` 这个宽泛的词，直接用 `{{^}}` 锚定行首的数字列。

运行：

```bash
$ llvm-lit -sv llvm/test/CodeGen/RISCV/xdemo-sadd.ll \
              llvm/test/tools/llvm-mca/RISCV/xdemo-sadd.test
Testing:  0.. 10.. 20.. 30.. 40.. 50.. 60.. 70.. 80.. 90..
Testing Time: 0.09s

Total Discovered Tests: 2
  Passed: 2 (100.00%)
```

**检查点 5**：两个测试都 PASS。把 `Sched<[...]>` 改回 `WriteIALU`，mca 测试应该红（延迟 3），选择测试仍然 PASS——这证明两条测试锁的是不同层。

## Step 7：还原源码，重建基线

教学 patch 验证完必须还原，否则后续章节的实验数据都不可信：

```bash
$ cd /home/ubuntu/llvm/trees/explore
$ git checkout -- llvm/lib/Target/RISCV/
$ rm llvm/lib/Target/RISCV/RISCVInstrInfoXDemo.td \
     llvm/test/CodeGen/RISCV/xdemo-sadd.ll \
     llvm/test/tools/llvm-mca/RISCV/xdemo-sadd.test
$ ninja -C /home/ubuntu/llvm/builds/explore llc
```

本机还原后重建耗时约 15 秒（增量，258 条边），验证：

```bash
$ llc -mattr=help -mtriple=riscv64 | grep -c xdemo
0
```

**检查点 6**：`xdemo` 从 `-mattr=help` 消失，`git status` 干净。这一步不做，后面所有「基线」数字都不可信。

## 验收标准

- [ ] `-mattr=help` 出现 `xdemo`。
- [ ] `llvm-mc -show-encoding` 能编码 `xdemo.sadd`，字节为 `0b 85 c5 c0`（对 `a0, a1, a2, 3`）。
- [ ] `llc -mattr=+m,+xdemo` 生成 `xdemo.sadd`；去掉 `+xdemo` 生成 `slli+add`。
- [ ] TableGen 全量构建无 `Processor does not define resources`。
- [ ] mca：u74 latency 2 / PipeA；rocket latency 1 / ALU。
- [ ] 选择测试与 mca 测试都 PASS。
- [ ] 源码还原、基线重建、`git status` 干净。

## 常见失败模式

1. **pattern 写了裸 `add`，匹配不到**。RISC-V 会把 add 规范化成 `riscv_add_like_non_imm12`；照 `TH_ADDSL` 的写法。
2. **只给一个 CPU 加 `WriteRes`**。TableGen 会点名第一个缺资源的处理器（本机是 Andes45），补完写还要补读。
3. **`ReadAdvance` 放进 `let Latency` 块**。报 `Value 'Latency' unknown!`；`ReadAdvance` 没有 Latency 字段。
4. **`Sched<[...]>` 的读操作数少一个**。不报错，但第二个源的依赖丢失，调度器可能提前。
5. **mca 测试用 `CHECK: Instructions:` 起手**。会匹配汇总行，`CHECK-NEXT` 必失败；用 `{{^}}` 锚定。
6. **忘记 `Predicates = [HasVendorXDemo]`**。指令会在所有 CPU 上可选，默认 `-mattr` 也能生成，破坏其他测试。
7. **和 `+zba` 同时开**。`sh3add` 的 pattern 与 `xdemo.sadd` 竞争，TableGen 不报冲突，选中哪条取决于 pattern 顺序；教学测试里不要同时开。
8. **改完不还原**。这是最隐蔽的：后续所有「基线」实验都会被这条幽灵指令污染。

## 如果要做成真正的 upstream patch

本项目的 patch 是「能工作」，不是「能合并」。要变成上游 patch，至少还要：

1. **真实来源**：latency/吞吐的厂商文档或实测；不能写「2 看起来合理」。
2. **16 个模型的真实取值**：本机机械复制 `WriteIALU` 只是占位；每个 CPU 要有自己的数。
3. **完整的 mca 覆盖**：参考 `llvm/test/tools/llvm-mca/RISCV/SiFiveX100/` 的粒度，按指令类别建 `.test`。
4. **release notes**：用户可见的新指令要写 `llvm/docs/ReleaseNotes.rst`。
5. **commit message**：`[RISCV] Add XDemo shift-add instruction`，body 写来源、影响面、测试；带 `Assisted-by`（如果用 AI 辅助）。
6. **review 预检**：第 28 章的 `pre-pr-check.sh` + `$llvm-reviewer-patterns`。

## 重点整理

| 层 | 文件 | 关键点 | 验证 |
|---|---|---|---|
| feature | `RISCVFeatures.td` | `RISCVExtension` 命名规则 | `-mattr=help` |
| 指令 | `RISCVInstrInfoXDemo.td` | `RVInstRBase` + 字段覆盖 | `llvm-mc -show-encoding` |
| pattern | 同上 | 用 `riscv_add_like_non_imm12` | `llc` 有/无 `+xdemo` |
| sched 类 | `RISCVSchedule.td` | 写 1 个、读 N 个 | TableGen 构建 |
| 每 CPU 定价 | `RISCVSched*.td` | 写和读都要覆盖 | `WriteRes`/`ReadAdvance` |
| 目标 CPU 覆盖 | `RISCVSchedSiFive7.td` | multiclass 内覆盖 latency | mca latency 2 |
| 回归 | `llvm/test/...` | `.ll` 锁选择、`.test` 锁模型 | `llvm-lit` |

- 一条指令的价值不在「能生成」，而在「选择、调度、模型、测试」四件事都能自证。
- sched model 的覆盖税是真实成本：19 个文件里 15 个只是两行占位。
- 同一条指令在不同 CPU 上延迟不同，这不是 bug，是模型的本职。
- 教学 patch 验证后必须还原；否则后续实验全部污染。

## 自我检核

- [ ] 我能从零写出 `xdemo.sadd` 的 feature、指令、pattern、sched 类四段代码。
- [ ] 我能解释 `RVInstRBase` 的位域，以及 `uimm2` 放在 `31-30` 的理由。
- [ ] 我知道为什么 pattern 用 `riscv_add_like_non_imm12` 而不是 `add`。
- [ ] 我能解释「写资源」和「读资源」都要覆盖，以及漏掉的报错长什么样。
- [ ] 我能用 mca 证明同一条指令在 u74/rocket 上的延迟差异。
- [ ] 我能写出选择测试和 mca 测试，并知道它们各自锁哪一层。
- [ ] 我知道教学 patch 与可合并 patch 的差距在哪。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVInstrInfoXTHead.td`：`TH_ADDSL` 的完整定义与 pattern；本章的指令就是照它写的。
- `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`：`SH1ADD/SH2ADD/SH3ADD` 与 `Sh1AddPat`；对照「复杂 pattern」与「简单 pattern」的写法。
- `llvm/lib/Target/RISCV/RISCVSchedule.td`：`WriteIALU`/`ReadIALU` 等抽象类的组织；新增类时放这里。
- `llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`：multiclass 如何给三个 VLEN 变体定价；本章的 `WriteXDemoSAdd` 覆盖点。
- `llvm/test/tools/llvm-mca/RISCV/SiFiveX100/integer.test`：上游 mca 测试的标准结构；把本章的单条测试扩展成完整覆盖时参考它。
- `llvm/docs/TableGen/`：`Instruction`/`Operand`/`SchedWrite` 的记录模型；写自定义编码前先读。

---

## 课程收尾：三句话带走

1. **选择决定图，调度决定序，模型决定价**。看到汇编差异，先问「图变了吗」，再问「序变了吗」，最后问「定价对吗」。
2. **每一层都有可截断的验证点**：`-stop-after=finalize-isel` 看选择，`-run-pass=machine-scheduler` 看调度，`llvm-mca` 看模型，`perf` 看硅。
3. **RISC-V 是最透明的放大镜**：load/store、12-bit immediate、无 indexed addressing，让「为什么选这条、为什么排这样」都能落到具体机制上。

面试里被问到「调度和指令选择怎么打通」，可以这样答：

> 「我用一条 IR 走完 SelectionDAG 的 combine/legalize/select，看它变成哪条 MachineInstr；再用 `-stop-after` 和 `-run-pass` 把选择与调度分开验证；最后用 sched model 和 mca 把顺序翻译成周期，用 perf 在板上对账。这样任何一次变慢，我都能定位到是图、序、还是定价的问题。」
