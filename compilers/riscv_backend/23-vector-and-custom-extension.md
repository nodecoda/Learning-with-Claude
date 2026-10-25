# 23 RVV 向量后端与新增扩展：从 `PseudoVADD` 到自己的指令

> 目标：把 RVV（RISC-V Vector）后端与标量后端的差异讲清楚，并把这门课的所有线索收束成一条可执行的扩展流程。读完后你能解释 `vsetivli` 为什么不在 isel 阶段出现、`vtype` 的 208 是怎么算出来的、`v0` 为什么特殊，能照着清单给 LLVM 加一条自定义指令，并明确哪些步骤在本机能验证、哪些必须重编译 `llc`。

> 环境：LLVM 23.1.4 预编译（host x86_64-pc-windows-msvc）。RVV 实验用 `-mattr=+v -riscv-v-vector-bits-min=128`；本章所有汇编与 MIR 均为本机真实输出。新增指令的完整链路需要重编译 `llc`，相关步骤标 `guided`。

## 为什么

标量后端的核心矛盾是“指令怎么选”；RVV 后端多了一个更麻烦的矛盾：**向量指令的语义依赖一个隐式状态 `vtype` 和 `vl`**。

同一条 `vadd.vv v8, v8, v9`，在 `e32,m1` 下是 4 个 32 位元素相加，在 `e8,m4` 下是 16 个 8 位元素相加。指令编码里没有 `e32`、`m1` 这些信息，它们全在 `vtype` 寄存器里。于是后端必须回答三个新问题：

1. 谁负责在正确的时机写 `vtype` 与 `vl`？
2. 两条相邻的向量指令能否共享同一次 `vsetvli`？
3. 寄存器分配时，`v8` 是一个寄存器还是一组寄存器？

这三个问题的答案决定了 RVV 后端的结构：**isel 只生成伪指令，`vsetvli` 由后面独立的 pass 插入，寄存器分配按 LMUL 分组处理**。不理解这一点，读 RVV 的 MIR 会觉得“怎么全是 Pseudo”。

## 主线

### 23.1 寄存器模型：`v0`–`v31`、LMUL 与掩码

RVV 有 32 个向量寄存器 `v0`–`v31`，但它们的“宽度”不是固定的：

- 物理上每个寄存器是 VLEN 位（本机 `-riscv-v-vector-bits-min=128`，即至少 128 位）。
- LMUL（`m1`/`m2`/`m4`/`m8`）决定一条指令用几个寄存器组成一个逻辑向量。`m2` 表示两条指令寄存器拼在一起，`m8` 表示八条。
- `v0` 被保留给掩码（mask），不能作为普通向量操作数参与 `m1` 以上的分组。这就是 `RISCVVMV0Elimination` 这类 pass 存在的原因：生成 `v0` 的 copy 很贵，能消就消。

汇编里的 `v8` 在 `m2` 下代表 `v8` 与 `v9` 两个物理寄存器。寄存器分配器必须把这对寄存器一起分配，不能拆开。`RISCVRegisterInfo.td` 里的 `VR`、`VRM2`、`VRM4`、`VRM8` 寄存器类就是为此存在的。

### 23.2 `vtype` 编码：208 是怎么来的

先看 isel 之后的 MIR（真实输出，`-stop-after=riscv-insert-vsetvli`）：

```text
dead $x0 = PseudoVSETIVLI 4, 208 /* e32, m1, ta, ma */, implicit-def $vl, implicit-def $vtype
renamable $v8 = PseudoVLE32_V_M1 undef renamable $v8, %0, 4 /* vl */, 5 /* e32 */, 2 /* tu, ma */, implicit $vl, implicit $vtype
renamable $v8 = PseudoVADD_VV_M1 undef renamable $v8, killed renamable $v8, killed renamable $v9, 4 /* vl */, 5 /* e32 */, 1 /* ta, mu */, implicit $vl, implicit $vtype
```

`208` 不是魔法数，它是 `vtype` 的位编码：

| 位 | 字段 | 本指令取值 | 贡献 |
|---|---|---|---|
| `[2:0]` | `vlmul` | `0`（m1） | `0` |
| `[5:3]` | `vsew` | `2`（e32） | `2 << 3 = 16` |
| `[6]` | `vta` | `1`（tail agnostic） | `64` |
| `[7]` | `vma` | `1`（mask agnostic） | `128` |

`16 + 64 + 128 = 208`。注释里的 `ta, ma` 就是 `vta=1, vma=1`。

这里有一个容易读错的细节：`PseudoVLE32_V_M1` 后面的 `5 /* e32 */` 是另一个编号（`VSEW_32` 的枚举值），不是 `vtype` 的位域。前者是 TableGen 层的立即数操作数，后者是运行时写进 CSR 的值。

### 23.3 isel 只生成伪指令

`-stop-after=finalize-isel` 看到的 MIR 更早：

```text
%3:vr = PseudoVLE32_V_M1 $noreg, %0, 4 /* vl */, 5 /* e32 */, 2 /* tu, ma */ :: (load (s128) from %ir.a)
%5:vr = PseudoVADD_VV_M1 $noreg, killed %3, killed %4, 4 /* vl */, 5 /* e32 */, 1 /* ta, mu */
```

注意三点：

1. 没有任何 `vsetvli`。
2. 操作数是虚拟寄存器 `%3:vr`，寄存器类已经是 `vr`。
3. 伪指令带了 `vl`、`e32`、`tu/ma` 这些**立即数形式的策略参数**。

为什么不在 isel 里直接生成 `vsetivli`？因为 isel 是逐节点匹配的，它看不到“接下来五条指令都用同一个 `vtype`”这种全局信息。如果每条向量指令都配一条 `vsetvli`，代码会膨胀得无法接受。把这件事推迟到一个有数据流分析的机器 pass，才能做公共子表达式消除。

### 23.4 `RISCVInsertVSETVLI`：用数据流求出最小 `vsetvli` 集合

这个 pass 做的是前向数据流分析：在每个基本块入口，记录“当前 `vl` 与 `vtype` 是什么”；遇到需要不同 `vtype` 的伪指令，就插入一条 `vsetivli`/`vsetvli`；遇到已经匹配的状态，就复用。

插入后（仍然是 MIR，但伪指令已被标记为使用 `implicit $vl, $vtype`）：

```text
dead $x0 = PseudoVSETIVLI 4, 208 /* e32, m1, ta, ma */, implicit-def $vl, implicit-def $vtype
renamable $v8 = PseudoVLE32_V_M1 ... implicit $vl, implicit $vtype
```

最后汇编阶段，`PseudoVSETIVLI` 展开成真正的指令：

```asm
vsetivli	zero, 4, e32, m1, ta, ma
vle32.v	v8, (a0)
vle32.v	v9, (a1)
vadd.vv	v8, v8, v9
vse32.v	v8, (a2)
ret
```

一条 `vsetivli` 服务四条向量指令。这就是“延迟插入”的收益。如果这四条指令的 `vtype` 不一致，pass 会插入多条；如果 `vl` 在循环里变化，pass 会退化成 `vsetvli`（寄存器形式）而不是 `vsetivli`（立即数形式）。

### 23.5 RVV 专属的机器 pass

RVV 的 pass 比标量多，按管线顺序大致是：

| pass | 作用 |
|---|---|
| `riscv-vl-optimizer` | 优化 `vl` 的计算，去掉冗余的 `vsetvli` 候选 |
| `riscv-vector-peephole` | 向量指令窥孔（合并 `vmerge`/`vmv` 等） |
| `riscv-insert-vsetvli` | 插入 `vsetvli`/`vsetivli`（核心） |
| `riscv-vmv0-elimination` | 消除围绕 `v0` 的冗余 copy |
| `riscv-insert-read-write-csr` | 处理需要显式读写 CSR 的指令 |
| `riscv-insert-write-vxrm` | 定点舍入模式（`vxrm`）的写入 |

这些 pass 的存在说明一件事：RVV 的“指令选择”只完成了一半，剩下的一半在机器层。读 RVV 后端时，如果只看 `RISCVInstrInfoV.td` 的 pattern，会完全错过 `vsetvli` 这条主线。

### 23.6 一条 RVV 指令要写几层 `.td`

以 `vadd.vv` 为例，TableGen 里至少有三层：

1. **格式层**（`RISCVInstrFormatsV.td`）：定义 `RVInstV` 之类的基类，声明 `opcode`、`vd`、`vs1`、`vs2`、`vm` 字段。
2. **伪指令层**（`RISCVInstrInfoVPseudos.td`）：定义 `PseudoVADD_VV_M1` 等按 LMUL 展开的一组伪指令，带 `vl`/`vtype` 立即数操作数。
3. **真实指令层**（`RISCVInstrInfoV.td`）：定义 `VADD_VV` 的真实编码与汇编格式。

中间那层伪指令是关键设计：它让 isel 不必关心 LMUL 展开，也让 `vsetvli` 插入 pass 有一个统一的中间表示。

### 23.7 新增一条自定义指令：完整清单

这是本课程的收尾动作。假设要加一条整数指令 `xadd`（`rd = rs1 + rs2 + 1`），需要改这些地方：

| 步骤 | 文件 | 本机能否验证 |
|---|---|---|
| 1. 定义 Feature | `RISCVFeatures.td` | 能（tblgen） |
| 2. 定义指令格式或复用 `RVInstR` | `RISCVInstrFormats.td` | 能（tblgen） |
| 3. 定义指令与编码 | `RISCVInstrInfo.td` | 能（tblgen） |
| 4. 加 DAG pattern | `RISCVInstrInfo.td` | 能（tblgen 生成 DAGISel） |
| 5. 汇编/反汇编支持 | 由 tblgen 的 AsmMatcher 自动生成 | 能（生成物里能看到） |
| 6. 汇编别名或伪指令 | `AsmParser/RISCVAsmParser.cpp` | 需重编译（guided） |
| 7. 选指验证 | `llc -mattr=+xadd` | 需重编译（guided） |
| 8. lit 测试 | `llvm/test/CodeGen/RISCV/` | 需重编译（guided） |

第 1–5 步是纯 TableGen，能在本机跑通并看到 `xadd` 出现在 `RISCVGenInstrInfo.inc` 的枚举里、出现在 `RISCVGenAsmMatcher.inc` 的匹配表里。第 6–8 步需要构建 `llc`，本机磁盘不够，只能给命令与预期。

这个划分很重要：**TableGen 能验证“描述正确”，不能验证“后端行为正确”**。很多教程把这两件事混为一谈，导致读者以为改完 `.td` 就大功告成。

## 可跑实验

### 实验 1：向量加法的完整链路

目的：从 IR 一路看到 `vsetivli` 被插入。

```powershell
$bin = $env:LLVM_BIN
$lines = @(
  'target triple = "riscv64-unknown-elf"'
  'define void @addv4(ptr %a, ptr %b, ptr %c) {'
  '  %va = load <4 x i32>, ptr %a, align 16'
  '  %vb = load <4 x i32>, ptr %b, align 16'
  '  %s = add <4 x i32> %va, %vb'
  '  store <4 x i32> %s, ptr %c, align 16'
  '  ret void'
  '}'
)
[IO.File]::WriteAllText("$env:TEMP\v.ll", ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))

& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-vector-bits-min=128 "$env:TEMP\v.ll" -o -
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-vector-bits-min=128 -stop-after=finalize-isel "$env:TEMP\v.ll" -o - | Select-String 'Pseudo'
```

预期：第一条命令输出 `vsetivli` + 四条向量指令；第二条只输出 `PseudoVLE32_V_M1` 等伪指令，没有 `vsetivli`。

### 实验 2：验证 `vtype` 的 208

目的：把 MIR 里的 `208` 拆成位域。

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-vector-bits-min=128 -stop-after=riscv-insert-vsetvli "$env:TEMP\v.ll" -o - | Select-String 'PseudoVSETIVLI'
```

预期：`PseudoVSETIVLI 4, 208 /* e32, m1, ta, ma */`。手算 `16 + 64 + 128` 核对。

### 实验 3：改变 LMUL 与向量宽度

目的：观察 `-riscv-v-vector-bits-min` 与 LMUL 上限如何改变 `vtype` 与寄存器分组。

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-vector-bits-min=256 "$env:TEMP\v.ll" -o -
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-fixed-length-vector-lmul-max=1 "$env:TEMP\v.ll" -o -
```

预期：`vsetivli` 的 sew/lmul 字段与后续 `vle32.v` 的寄存器编号会变化；`lmul-max=1` 会强制 `m1`，限制一次处理的元素数。

### 实验 4：在 `.td` 副本里加一条自定义指令

目的：验证“TableGen 层可改、可生成、可检索”。

```powershell
$bin = $env:LLVM_BIN
$src = $env:LLVM_SRC_TD
$work = Join-Path $env:TEMP 'xadd'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Copy-Item "$src\*.td" $work

# 在 RISCVInstrInfo.td 末尾追加一条 R 型指令（示例，具体字段按 RVInstR 的定义写）
$add = @(
  ''
  'def XADD : RVInstR<0b0000000, 0b000, OPC_OP, (outs GPR:$rd),'
  '                  (ins GPR:$rs1, GPR:$rs2), "xadd", "$rd, $rs1, $rs2">,'
  '          Sched<[]>;'
) -join "`n"
[IO.File]::AppendAllText("$work\RISCVInstrInfo.td", $add, (New-Object Text.UTF8Encoding($false)))

& "$bin\llvm-tblgen.exe" -I "$($bin)\..\include" -I $work -gen-instr-info "$work\RISCV.td" -o "$work\x.inc"
Select-String -Path "$work\x.inc" -Pattern 'XADD'
```

预期：`llvm-tblgen` 成功退出，`Select-String` 能在生成的 `.inc` 里找到 `XADD` 的枚举与名字表项。如果 tblgen 报格式字段错误，说明 `RVInstR` 的参数顺序或 `OPC_OP` 名字写错了——这本身就是一次真实的 TableGen 反馈循环。

这一步**不会**让 `llvm-mc` 认识 `xadd`；要让它真正可汇编，必须重编译 `llc`（`guided`）。

### 23.8 掩码与 `v0`：一次真实的向量 select

标量 `select` 在 RVV 下不是分支，而是比较加掩码合并。输入：

```llvm
define <4 x i32> @vmax(<4 x i32> %a, <4 x i32> %b) {
  %c = icmp sgt <4 x i32> %a, %b
  %s = select <4 x i1> %c, <4 x i32> %a, <4 x i32> %b
  ret <4 x i32> %s
}
```

本机 `llc -mattr=+v -riscv-v-vector-bits-min=128` 的真实输出：

```asm
vmax:
	vsetivli	zero, 4, e32, m1, ta, ma
	vmax.vv	v8, v8, v9
	ret
```

LLVM 把「比较 + select」识别成了 `vmax.vv`，没有生成 `vmslt.vv` + `vmerge.vvm`。这是一个真实的 pattern 合并例子：源码层面的两个操作，在 DAG 层被折叠成一个节点，再匹配到一条指令。

如果你把条件改成 `icmp sgt` 之外、无法折叠成单条指令的形式（例如比较后做算术），就会看到 `vmslt.vv` 写 `v0`，再跟一条带 `v0.t` 掩码的指令。此时 `v0` 的生命周期很短，`RISCVVMV0Elimination` 会尝试把相邻的 `vmv0` 合并掉。

## 可跑实验（补充）

把上面的 `vmax` 与一个不能折叠的 select 并排跑：

```powershell
$bin = $env:LLVM_BIN
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+v -O2 -riscv-v-vector-bits-min=128 "$env:TEMP\vmax.ll" -o -
```

对比两次输出里是否出现 `v0.t` 与 `vmerge`，就能看出「pattern 折叠」与「掩码合并」是两条不同的路径。
## 源码落点

| 位置 | 作用 |
|---|---|
| `RISCVFeatures.td` | `+v`、`+zve32x`、`+zvfh` 等 Feature 定义 |
| `RISCVInstrFormatsV.td` | RVV 指令格式基类（`vm`、`vd`、`vs1`、`vs2`） |
| `RISCVInstrInfoV.td` | RVV 真实指令与编码 |
| `RISCVInstrInfoVPseudos.td` | 按 LMUL/SEW 展开的伪指令族 |
| `RISCVInstrInfoVVLPatterns.td` | 向量 DAG pattern |
| `RISCVInsertVSETVLI.cpp` | `vtype`/`vl` 数据流分析（核心） |
| `RISCVVLOptimizer.cpp` | `vl` 计算优化 |
| `RISCVVectorPeephole.cpp` | 向量窥孔 |
| `RISCVVMV0Elimination.cpp` | `v0` 冗余 copy 消除 |
| `RISCVInsertReadWriteCSR.cpp` | CSR 显式读写 |
| `RISCVInsertWriteVXRM.cpp` | `vxrm` 写入 |
| `RISCVRegisterInfo.td` | `VR`/`VRM2`/`VRM4`/`VRM8` 寄存器类 |
| `RISCVSubtarget.h` | `hasVInstructions()`、`getMinRVVVectorSizeInBits()` |

## 常见坑

1. **在 isel 阶段找 `vsetivli`**。isel 只生成 `PseudoV*`；`vsetivli` 由 `riscv-insert-vsetvli` 插入。用 `-stop-after=finalize-isel` 看不到它是正常的。
2. **把 `208` 当成 SEW 编号**。`208` 是整个 `vtype` 的位编码；`5 /* e32 */` 才是 TableGen 层的 SEW 枚举。
3. **忘记 `v0` 不是普通寄存器**。掩码只能用 `v0`，所以涉及 mask 的代码会看到 `vmv0` 或 `vmerge.vvm` 这类指令；把 `v0` 当普通 `m1` 寄存器分配会得到错误代码。
4. **以为 LMUL 只影响性能**。LMUL 改变寄存器分组，`m8` 会占满 8 个寄存器，寄存器压力骤增，可能触发 spill；这不是“调个参数”那么简单。
5. **改完 `.td` 就以为指令可用**。TableGen 只生成描述；`llvm-mc` 与 `llc` 需要重新编译才会认识新指令。
6. **忽略 `.variant_cc` 之类的变体调用约定**。返回向量类型的函数会带上 `vcc` 相关标记，ABI 细节与标量函数不同，跨语言调用时要单独确认。

## 自我检查

- [ ] 能解释为什么 isel 阶段只有 `PseudoV*`，没有 `vsetivli`。
- [ ] 能独立把 `208` 拆成 `vlmul`/`vsew`/`vta`/`vma` 四个字段。
- [ ] 能说出 `v0` 的特殊性，以及 `RISCVVMV0Elimination` 在解决什么问题。
- [ ] 能列出新增一条指令需要改的 8 个步骤，并区分哪些能在本机验证。
- [ ] 能解释 LMUL 如何改变寄存器分配与寄存器压力。
- [ ] 能用 `-stop-after=riscv-insert-vsetvli` 与 `-stop-after=finalize-isel` 对照，说明多出来的 `vsetivli` 从哪来。

## 延伸阅读

- RISC-V 向量扩展规范（RVV 1.0）第 3–4 章：`vtype` 位域、LMUL、tail/mask agnostic 语义的正式定义。
- LLVM 源码 `llvm/lib/Target/RISCV/RISCVInsertVSETVLI.cpp`：前向数据流分析与 `vsetvli` 合并策略，是本课最难也最值得读的一个 pass。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td`：伪指令如何按 SEW/LMUL 展开成族，读它能理解 TableGen 的 `multiclass` 在真实项目里的用法。
- `llvm/test/CodeGen/RISCV/rvv/`：RVV 的 lit 测试目录，几乎每个 pass 都有最小用例，适合配合 `-print-after` 逐个复现。

[上一章：常量池、GOT/PLT 与 TLS](./22-constant-pool-got-tls.md) | [下一章：Final 端到端项目](./final-project-riscv-backend.md)