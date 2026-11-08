# Ch 17 — RVV lowering：LMUL、vsetvli 与向量优化

> **目标**：把一条 `<vscale x N x T>` 的 IR 指令跟到 RVV 汇编，理解 SEW/LMUL/VL、`v0` mask、policy 与 `vsetvli` 状态如何共同决定 lowering；能读懂 `RISCVVLOptimizer`、`RISCVVectorPeephole`、`RISCVInsertVSETVLI` 的分工。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，只读），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）。本章“实测”都来自这棵树与这个构建。

> **前置**：Ch 05（`VR`/`VRM2..8`/`VMV0` 与可伸缩栈槽）、Ch 13（Legalize 后的 DAG）、Ch 14（SelectionDAG pattern 与 `-stop-after=finalize-isel`）。

## 17.1 为什么 RVV lowering 难

RVV 的一条指令由四个维度同时决定：

| 维度 | 含义 | 本树落点 |
|---|---|---|
| **SEW** | 元素宽度 8/16/32/64 | pseudo 的 `sew` 操作数；VTYPE 的 3-bit `vsew` |
| **LMUL** | 覆盖几个寄存器 `mf8..m8` | opcode 后缀 `_M1/_M2/_MF2...` + `TSFlags` 的 `VLMul` |
| **VL** | 实际处理多少元素 | pseudo 的 `$vl`；运行时 `vl` CSR |
| **mask/policy** | `v0` mask；tail/mask 是否 undisturbed | `$vm` + 2-bit `vec_policy` |

额外三件事让 lowering 变难：**可伸缩向量**（IR 是 `<vscale x 4 x i32>`，编译期只知道 `vscale_range(2,1024)`）；**mask 固定在 `v0`**（masked 指令隐式读 `v0`，`v0` 不能当普通 `VR`）；**`vsetvli` 有状态**（它定义 `vl`/`vtype`，后续向量指令隐式读取）。

一个数字：`PseudoVADD_VV` 在生成物里有 **14** 个 opcode（7 个 LMUL × masked/unmasked），SEW 只是一份操作数：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
grep -o "PseudoVADD_VV_[A-Za-z0-9_]*" $T/lib/Target/RISCV/RISCVGenInstrInfo.inc | sort -u | wc -l
# 14
```

如果 SEW 也做成 opcode 理论上就会变成 56 个；LLVM 把 SEW 放进操作数、把 LMUL 放进 opcode/TSFlags。这个选择决定了后面所有 pseudo 的命名与匹配。

## 17.2 先建立直觉：一条向量 IR 指令经过哪四层

以 `%r = add <vscale x 4 x i32> %a, %b` 为例：

```text
IR:  %r = add <vscale x 4 x i32> %a, %b        ; vscale_range(2,1024)
             │
             ▼
[1] 类型合法化 (vscale)
    <vscale x 4 x i32> → MVT::nxv4i32
    fixed-length <4 x i32> → getContainerForFixedLengthVector → nxv4i32
             │
             ▼
[2] DAG
    ISD::ADD / BUILD_VECTOR / VECTOR_SHUFFLE / INSERT_SUBVECTOR ...
    RVV 目标节点：RISCVISD::ADD_VL、VMV_V_V_VL、VSLIDEUP_VL ...
             │
             ▼
[3] ISel 选 PseudoV*
    PseudoVADD_VV_M2 $passthru, $rs2, $rs1, $vl, sew=5, policy=3
    （LMUL 在 opcode 后缀与 TSFlags；SEW 是显式立即数）
             │
             ▼
[4a] RISCVVLOptimizer：demanded-VL 数据流，把 -1(VLMAX) 改成真正需要的 %avl
             │
             ▼
[4b] RISCVVectorPeephole：Pseudo 化简（masked→unmasked、vmerge→vmv）
             │
             ▼
[4c] RISCVInsertVSETVLI：插入/合并 vsetvli，把显式 VL 改成隐式 $vl
             │
             ▼
汇编:  vsetvli a5, a3, e32, m2, ta, ma
       vadd.vv v8, v10, v8
```

四层对应四类文件：

| 层 | 文件 | 观察点 |
|---|---|---|
| 类型/DAG | `RISCVISelLowering.cpp` | `lower*`、`getContainerForFixedLengthVector` |
| ISel | `RISCVInstrInfoVPseudos.td` | `PseudoV*` 的 opcode 与操作数 |
| MI 优化 | `RISCVVLOptimizer.cpp`、`RISCVVectorPeephole.cpp` | `-stop-before/after` |
| 配置插入 | `RISCVInsertVSETVLI.cpp` | `-stop-before/after=riscv-insert-vsetvli` |

**纠偏**：很多资料把 4b/4c 都叫 peephole，甚至说 `RISCVVectorPeephole` 把 `PseudoVADD` 和 `PseudoVSETVLI` 合成 `vadd.vv`。本树不是这样：`RISCVVectorPeephole` 跑在 `vsetvli` 插入之前，`grep -n "VSET\|vset" RISCVVectorPeephole.cpp` **无输出**；真正插入/合并 `vsetvli` 的是 `RISCVInsertVSETVLI`。

## 17.3 VLMAX 与 `vsetvli` 的真实形态

以下命令假设已 `source /home/ubuntu/llvm/toolset/env.sh`，且 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

### 17.3.1 VLMAX 不是常量

```text
VLMAX = floor(VLEN * LMUL / SEW)
```

整数 LMUL 时可写成 `VLEN/SEW*LMUL`。例如 `VLEN=128, SEW=32, LMUL=1` 时 `VLMAX=4`；`LMUL=2` 时 `VLMAX=8`。VLEN 是硬件属性，编译期通常只知道最小值（`zvl128b` = 至少 128 位）。

运行时取 VLEN 的字节数用 `vlenb`：

```bash
printf 'csrr a0, vlenb\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+v -show-encoding
# csrr a0, vlenb   # encoding: [0x73,0x25,0x20,0xc2]
```

`vlenb = VLEN/8`，所以 `VLMAX = floor(vlenb*8*LMUL/SEW)`。clang 的 `vadd` 循环里 `csrr a4, vlenb; slli a4, a4, 1` 算的是 m2 向量组的字节数，不是元素个数。

### 17.3.2 `vsetvli` 编码

```bash
printf 'vsetvli a0, zero, e32, m1, ta, ma\nvsetvli a0, a1, e32, m1, ta, ma\nvsetivli a0, 4, e32, m1, ta, ma\nvsetvli zero, zero, e8, mf2, tu, mu\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+v -show-encoding
```

实测：

```text
vsetvli  a0, zero, e32, m1, ta, ma   # [0x57,0x75,0x00,0x0d]
vsetvli  a0, a1, e32, m1, ta, ma     # [0x57,0xf5,0x05,0x0d]
vsetivli a0, 4, e32, m1, ta, ma      # [0x57,0x75,0x02,0xcd]
vsetvli  zero, zero, e8, mf2, tu, mu # [0x57,0x70,0x70,0x00]
```

`vsetvli a0, zero, e32, m1, ta, ma`：`rs1=x0` 表示 AVL=VLMAX；`rd=a0` 读回实际 `vl`；`vtypei=0xD0=208` 即 `vlmul=0`(m1)、`vsew=2`(e32)、`vta=1`、`vma=1`。`vsew` 是 `log2(SEW)-3` 的 3-bit 编码：e8=0、e16=1、e32=2、e64=3。

`vsetvli` 的 `vtypei` 有 11 位（bit30:20）；`vsetivli` 只有 10 位（bit29:20），AVL 是 5-bit `uimm`，最大 31。超过 31 必须用 `vsetvli`/`vsetvl`。

### 17.3.3 `$noreg` 的两种含义

- **`vsetvli` 插入前**：pseudo 的 VL 写 `$noreg` 表示 `x0`，即 AVL=VLMAX。DAG 里对应 `getDefaultScalableVLOps` 的 `DAG.getRegister(RISCV::X0, XLenVT)`（`RISCVISelLowering.cpp:3463`）。
- **插入后**：`RISCVInsertVSETVLI` 把 AVL 操作数 `setReg(Register())` 擦掉（`:595`），再加隐式 `$vl` use（`:621`）。此时 `$noreg` 表示“VL 由当前 `vl` CSR 决定”。

同一个打印文本，前后语义完全不同；看 MIR 必须先确认停在哪个 pass。

## 17.4 DAG 层：可伸缩类型与 RVV 目标节点

### 17.4.1 `MVT::nxv*` 与 `vscale`

`MVT::nxv4i32` 表示 `<vscale x 4 x i32>`：元素数 4 要乘运行时 `vscale`。`vscale` 最小值由 `zvl*` 决定；本树 clang 给 `rv64gcv` 的函数带 `vscale_range(2,1024)`（VLEN≥128，最大 65536，`65536/64=1024`）。

fixed-length 向量先经 `getContainerForFixedLengthVector`（`:3383`，成员版 `:3419`）映射到 scalable container。`<4 x i32>` 在 `zvl128b` 下是 `nxv4i32`；`-riscv-v-vector-bits-min=256` 时同一个 128-bit 数据只需 `LMUL=1/2`，这就是实验 6 的 `m1 → mf2` 来源。

### 17.4.2 `lower*` 的 RVV 分支

`LowerOperation` 分派表在 `RISCVISelLowering.cpp:9016` 起：

| DAG 节点 | 函数 | 行号 |
|---|---|---|
| `INSERT_SUBVECTOR` | `lowerINSERT_SUBVECTOR` | `:14691` |
| `EXTRACT_SUBVECTOR` | `lowerEXTRACT_SUBVECTOR` | `:14936` |
| `BUILD_VECTOR` | `lowerBUILD_VECTOR` | `:4786` |
| `VECTOR_SHUFFLE` | `lowerVECTOR_SHUFFLE` | `:6731` |
| `VECTOR_DEINTERLEAVE`/`INTERLEAVE` | `lowerVECTOR_DEINTERLEAVE`/`INTERLEAVE` | `:15145`/`:15359` |

`lowerBUILD_VECTOR` 在 `:4788` 先 `assert(VT.isFixedLengthVector())`：它只接 fixed-length；scalable `BUILD_VECTOR` 要从 `SPLAT_VECTOR`/`INSERT_SUBVECTOR` 路径看。`lowerVECTOR_SHUFFLE` 在 `:6810` 之后进入 RVV container/`getDefaultVLOps` 分支，splat、slide、gather 都在这里分流。

### 17.4.3 带 VL 的目标节点

目标节点在 `RISCVInstrInfoVVLPatterns.td` 用 `RVSDNode` 声明：`RISCVISD::ADD_VL`（`:137`，类型剖面 `SDT_RISCVIntBinOp_VL` 在 `:64`，操作数 `rs1, rs2, passthru, mask, vl`）、`VMV_V_V_VL`（`:96`）、`VSLIDEUP_VL`/`VSLIDEDOWN_VL`（`:3107`/`:3108`）。这些节点把“VL 从哪来”显式放进 DAG。

`getVLOperand`（`RISCVISelLowering.cpp:3294`）只服务 intrinsic：从 `RISCVVIntrinsicsTable` 查 `VLOperand` 下标再取 SDValue。**本树没有 `getVLMergeOperand`**；passthru/mask 由 `RVSDNode` 的 `HasPassthruOp`/`HasMaskOp` TSFlags 区分（`RISCVInstrInfo.td:18`）。`getDefaultScalableVLOps`（`:3463`）返回“全 1 mask + `x0` VL”；`computeVLMax`（`:3494`）用 `DAG.getElementCount` 生成 `vscale*N`。

## 17.5 Pseudo 指令与 SEW/LMUL 立即数

### 17.5.1 命名规则

`defm PseudoVADD : VPseudoVALU_VV_VX_VI<Commutable=1>;`（`RISCVInstrInfoVPseudos.td:6171`）展开出：

```text
PseudoVADD_VV_M1 / PseudoVADD_VV_M1_MASK
PseudoVADD_VV_M2 / PseudoVADD_VV_M2_MASK
...                PseudoVADD_VV_MF2 / _MASK
```

后缀来自 `LMULInfo::MX`（`:175`，`V_M1`..`V_M8`/`V_MF8`..`V_MF2` 在 `:188`–`:195`）。`VPseudoBinary`（`:2096`）给每个 LMUL 生成 unmasked/masked 两条，masked 追加 `_MASK`（`:2104`–`:2112`，`RISCVMaskedPseudo<MaskIdx=3>`）。**SEW 不在 opcode 里**：同一个 `PseudoVADD_VV_M1` 用 `sew` 操作数覆盖 e8/e16/e32/e64。

### 17.5.2 操作数顺序

`VPseudoBinaryNoMaskPolicy`（`:1225`）定义：

```text
(outs $rd)
(ins  $passthru, $rs2, $rs1, $vl, $sew, $policy)
```

`VPseudoBinaryMaskPolicy`（`:1366`）在 `$rs1` 后插入 `$vm`：

```text
(outs $rd)
(ins  $passthru, $rs2, $rs1, $vm, $vl, $sew, $policy)
```

`$rs2`/`$rs1` 跟着汇编 `vadd.vv vd, vs2, vs1`，所以源码顺序看起来是“反”的：IR 的 `%a` 通常是 `$rs2`，`%b` 是 `$rs1`。实测：

```text
%33:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %32, %30, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
%33:vrm2nov0 = PseudoVLE32_V_M2_MASK $noreg, %32, %34, %27 /* vl */, 5 /* e32 */, 1 /* ta, mu */
```

`5` 是 `log2(32)`，不是 32；`3` 是 policy，不是 SEW。masked 结果类 `vrm2nov0` 排除了 `v0`；`1` 是 `ta, mu`。

### 17.5.3 policy 位

policy 是 2-bit 立即数，常量在 `RISCVVType`（不是 `RISCVII`）：

| 值 | tail | mask | 汇编 |
|---|---|---|---|
| 0 | undisturbed | undisturbed | `tu, mu` |
| 1 | agnostic | undisturbed | `ta, mu` |
| 2 | undisturbed | agnostic | `tu, ma` |
| 3 | agnostic | agnostic | `ta, ma` |

源码：`RISCVTargetParser.h:129`–`:131` 定义 `TAIL_AGNOSTIC=1`、`MASK_AGNOSTIC=2`；`RISCVInstrInfoVPseudos.td:132`–`:135` 定义 `TAIL_AGNOSTIC=1`、`TU_MU=0`、`TA_MU=1`、`TA_MA=3`。

**tail undisturbed 不是可有可无的优化**：实验 4 最终生成 `vsetivli zero, 4, e32, m2, tu, ma`，因为 `vmv.v.v` 要保留 `v8` 的 lane 4..VLMAX（前一条 `vmv.v.i` 写的 0）；写成 `ta` 返回值尾元素就是 undef。

### 17.5.4 `PseudoVSET*` 家族

`RISCVInstrInfoVPseudos.td:6098` 起：

| 名字 | AVL | VTYPE | 说明 |
|---|---|---|---|
| `PseudoVSETIVLI` | `uimm5` | 10-bit imm | `vsetivli`，AVL≤31 |
| `PseudoVSETVLI` | `GPRNoX0` | 11-bit imm | `vsetvli rd, rs1, vtypei` |
| `PseudoVSETVLIX0` | `x0` | 11-bit imm | AVL=VLMAX |
| `PseudoVSETVLIX0X0` | `x0`/`x0` | 11-bit imm | AVL 与 VLMAX 都与上一条相同 |
| `VSETVL`（真实指令） | `rs1` | `rs2` | `vsetvl rd, rs1, rs2`，VTYPE 来自寄存器 |

本树**没有 `PseudoVSETVL`**；`VSETVL` 是 `RISCVInstrInfoV.td:1112` 的真实指令。`vsetvli`/`vsetivli`/`vsetvl` 的格式在 `RISCVInstrFormatsV.td:75`/`:61`/`:85`，三者的 `Defs` 都是 `[VL, VTYPE]`。

## 17.6 RISCVVLOptimizer：把 VL 降到真正需要的值

`RISCVVLOptimizerImpl` 在 `RISCVVLOptimizer.cpp:66`，legacy `runOnMachineFunction` 在 `:1453`，实现入口 `run` 在 `:1409`。它跑在 `vsetvli` 插入之前，做**逆向 demanded-VL 数据流**：从使用者往定义者传播“我只读这么多 lane”。

| 函数 | 行号 | 作用 |
|---|---|---|
| `isCandidate` | `:1048` | 只处理有 VL/SEW、单显式 def、无 volatile/FP 异常的安全指令 |
| `getMinimumVLForUser` | `:1128` | 按 user 的 VL、passthru、是否把向量当标量用，算最小需求 |
| `transfer` | `:1395` | 把需求传播给 `virtual_vec_uses` 的定义者 |
| `tryReduceVL` | `:1316` | 真正改 VL：立即数走 `:1352`，vreg 走 `:1385` |

两个实测变换（截取）：

```text
BEFORE  PseudoVADD_VV_M2 ... -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
AFTER   PseudoVADD_VV_M2 ... %27 /* vl */,     5 /* e32 */, 3 /* ta, ma */
DEBUG   Reduce VL from -1 to %27 for %33:vrm2 = nsw PseudoVADD_VV_M2 ...

BEFORE  %2:vr = PseudoVMSET_M_B16 -1 /* vl=VLMAX */, 0 /* e8 */
AFTER   %2:vr = PseudoVMSET_M_B16 4 /* vl */, 0 /* e8 */
```

第一条是 17.11 实验 3 的 `vadd`；第二条是实验 4 的 `vmerge`：`vmerge` 只读前 4 个 lane，所以生成 mask 的 `vmset` 也不需要 VLMAX。`getMinimumVLForUser` 还处理 `vslidedown.vx` 的“只读前 slideamt+1 个 lane”（`:1104` 起）、passthru（tied operand）、reduction 把向量当标量用（`:1014`）。

pass 顺序在 `RISCVTargetMachine.cpp:547`–`:558`：`EarlyMachineLICM` → `RISCVVLOptimizer` → `RISCVVectorPeephole`。`-O0` 不跑 VL optimizer。

## 17.7 RISCVVectorPeephole：Pseudo 层的化简

`RISCVVectorPeepholeImpl` 在 `RISCVVectorPeephole.cpp:44`，`run` 在 `:959`。它要求 SSA（`:80`），跑在寄存器分配之前；它不是“把 pseudo 变成真实指令”，而是把一种 pseudo 改写成更便宜的 pseudo：

| 变换 | 行号 | 效果 |
|---|---|---|
| `convertToVLMAX` | `:131` | 已知 AVL=VLMAX 时把 VL 改成 `-1` |
| `convertAllOnesVMergeToVMv` | `:287` | all-ones mask 的 `vmerge` → `vmv.v.v` |
| `convertSameMaskVMergeToVMv` | `:338` | 同 mask 的 `vmerge` → `vmv` |
| `convertToUnmasked` | `:413` | all-ones mask 的 masked pseudo → unmasked |
| `foldVMergeToMask` | `:654` | 把 `vmerge` 折进 masked 指令 |
| `foldVMANDToMaskedCompare` | `:816` | `vmand` + compare → masked compare |

实测（`vmerge` 的 mask 是 `splat (i1 true)`）：

```text
BEFORE  %4:vrm2nov0 = PseudoVMERGE_VVM_M2 %3, %0, %1, %5, 4 /* vl */, 5 /* e32 */
AFTER   %4:vrm2     = PseudoVMV_V_V_M2 %3, %1, 4 /* vl */, 5 /* e32 */, 0 /* tu, mu */
```

对应回归测试：`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/rvv-peephole-vmerge-to-vmv.mir`。

**再次强调**：这个 pass 不碰 `vsetvli`。`grep -n "VSET\|vset" RISCVVectorPeephole.cpp` 无输出；插入/合并是下一节。

## 17.8 RISCVInsertVSETVLI 与 mask 调度

### 17.8.1 插入与合并

`RISCVInsertVSETVLIImpl` 在 `RISCVInsertVSETVLI.cpp:111`，`insertVSETVLI` 在 `:160`。按 AVL 形态选 pseudo：

- 相同 AVL 且相同 VLMAX：`PseudoVSETVLIX0X0`（`:169`），rd 都不用。
- AVL 是立即数：`PseudoVSETIVLI`（`:207`）。
- AVL 是 VLMAX（`x0`）：`PseudoVSETVLIX0`（`:220`）。
- AVL 是寄存器：`PseudoVSETVLI`（`:235`）。

合并（coalescing）在 `coalesceVSETVLIs`（`:819`），改前一条配置由 `canMutatePriorConfig`（`:758`）判断。真正决定“SEW/LMUL/VL 是否兼容”的是 `areCompatibleVTYPEs`（`RISCVVSETVLIInfoAnalysis.cpp:114`），逐项检查：SEW（相等、≥、≥且<64）、LMUL（相等或≤M1）、`SEW/LMUL` 比值、tail policy、mask policy、`altfmt`、`twiden`。`needVSETVLI`（`:273`）在 `CurInfo` 已兼容时返回 false，不插入新配置；跨基本块还要过 `needVSETVLIPHI`（`:490`）。

### 17.8.2 实测：插入前后

对实验 7 的 masked 循环（截取）：

```text
BEFORE  %27:gprnox0 = PseudoVSETVLI %44, 199 /* e8, mf2, ta, ma */
        ... PseudoVLE32_V_M2 ... %27 /* vl */ ...
AFTER   %27:gprnox0 = PseudoVSETVLI %44, 209 /* e32, m2, ta, ma */
        ... PseudoVLE32_V_M2 ... $noreg /* vl */, ..., implicit $vl, implicit $vtype ...
```

两件事同时发生：遗留的 `e8, mf2` 配置被改写成真正需要的 `e32, m2`；每条向量指令的显式 VL 被擦掉，改成隐式 `$vl`，即 17.3.3 的 `$noreg` 语义切换。

### 17.8.3 `RISCVVectorMaskDAGMutation`

这个 110 行文件**不合并 mask 指令**。它是调度 mutation（`apply` 在 `:72`，工厂在 `:106`，注册在 `RISCVTargetMachine.cpp:233`）：在 mask producer 和 masked consumer 之间加 artificial dependency（`:100`），避免 `$v0 = COPY` 的 live range 拉太长。原因在文件头 `:14`–`:28`：RegPressureTracker 不跟踪物理寄存器，而 mask 在调度时已用物理 `v0`。真正的 mask 合并看 17.7 的 `foldVMergeToMask`/`foldVMANDToMaskedCompare`。

## 17.9 端到端：谁在什么时候改了什么

```text
IR ──Legalize──▶ DAG ──ISel──▶ Pseudo ──VLOptimizer──▶ Pseudo(VL 更小)
   ──VectorPeephole──▶ Pseudo(更简单) ──RegAlloc──▶
   ──InsertVSETVLI──▶ Pseudo + vsetvli(隐式 $vl/$vtype) ──▶ MC ──▶ 汇编
```

排错顺序：

| 现象 | 先停在哪 | 看什么 |
|---|---|---|
| Pseudo 的 VL 不对 | `-stop-after=finalize-isel` | `$vl` 是 `-1`、立即数还是 vreg |
| 配置被改/少一条 `vsetvli` | `-stop-before/after=riscv-insert-vsetvli` | `PseudoVSET*` 的 vtype 与 implicit 操作数 |
| 该省没省、VL 太大 | `-stop-before/after=riscv-vl-optimizer` | `Reduce VL` 日志、candidate 判定 |
| mask 没折掉 | `-stop-before/after=riscv-vector-peephole` | `VMERGE`/`VMSET`/`_MASK` 是否还在 |

## 17.10 源码落点

| 文件 | 行号 | 内容 |
|---|---|---|
| `RISCVVLOptimizer.cpp` | `:66`/`:1409`/`:1453` | `Impl`/`run`/legacy `runOnMachineFunction` |
| 同上 | `:1128`/`:1316`/`:1395` | `getMinimumVLForUser`/`tryReduceVL`/`transfer` |
| `RISCVVectorPeephole.cpp` | `:44`/`:959` | `Impl`/`run` |
| 同上 | `:131`/`:287`/`:413`/`:654`/`:816` | `convertToVLMAX`/`vmerge→vmv`/`convertToUnmasked`/`foldVMergeToMask`/`foldVMANDToMaskedCompare` |
| `RISCVInsertVSETVLI.cpp` | `:111`/`:160`/`:819` | `Impl`/`insertVSETVLI`/`coalesceVSETVLIs` |
| 同上 | `:169`/`:207`/`:220`/`:235` | X0X0/`VSETIVLI`/`VSETVLIX0`/`VSETVLI` 选择 |
| 同上 | `:595`/`:621` | 擦除 AVL、加隐式 `$vl` |
| `RISCVVSETVLIInfoAnalysis.cpp` | `:114` | `areCompatibleVTYPEs` |
| `RISCVVectorMaskDAGMutation.cpp` | `:72`/`:100` | `apply`/artificial edge |
| `RISCVTargetMachine.cpp` | `:233`/`:555`/`:558` | mask mutation 注册/VL optimizer/peephole |
| `RISCVISelLowering.cpp` | `:3294`/`:3463`/`:3494` | `getVLOperand`/`getDefaultScalableVLOps`/`computeVLMax` |
| 同上 | `:4786`/`:6731`/`:14691`/`:14936` | BUILD_VECTOR/SHUFFLE/INSERT/EXTRACT |
| `RISCVInstrInfoVPseudos.td` | `:175`/`:1225`/`:1366`/`:6171` | `LMULInfo`/无 mask/有 mask/`PseudoVADD` |
| 同上 | `:6098`–`:6107` | `PseudoVSET*` 家族 |
| `RISCVInstrInfoV.td` | `:1105`/`:1108`/`:1112` | `VSETVLI`/`VSETIVLI`/`VSETVL` |
| `RISCVInstrFormatsV.td` | `:61`/`:75`/`:85` | 三种配置指令的编码格式 |
| `RISCVRegisterInfo.td` | `:902`/`:910`/`:917`/`:924`/`:943` | `VR`/`VRM2`/`VRM4`/`VRM8`/`VMV0` |
| `RISCVFeatures.td` | `:698`–`:702` | `+v` 隐含 `zvl128b` |
| `RISCVInstrInfo.h` | `:413` | `VLMaxSentinel = -1` |

## 17.11 可跑实验

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /home/ubuntu/llvm/trees/ct-instr-trunk
mkdir -p /tmp/ch17
```

### 实验 1：最简单的 `a[i]+b[i]`

```bash
cat > /tmp/ch17/vadd.c <<'EOF'
void vadd(int *a, const int *b, const int *c, int n) {
  for (int i = 0; i < n; ++i) a[i] = b[i] + c[i];
}
EOF
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -O2 \
  -ffreestanding -nostdinc -isystem $T/lib/clang/24/include -S /tmp/ch17/vadd.c -o -
```

实测（截取）：`csrr a4, vlenb`、`slli a4, a4, 1`、`vsetvli a5, a3, e32, m2, ta, ma`、`vle32.v v8, (a7)`、`vadd.vv v8, v10, v8`、`vse32.v v8, (a6)`。`m2` 是向量化器选的 LMUL；AVL 是循环剩余 `a3`，不是常量。

### 实验 2：`finalize-isel` 看 Pseudo

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -O2 \
  -ffreestanding -nostdinc -isystem $T/lib/clang/24/include \
  -S -emit-llvm /tmp/ch17/vadd.c -o /tmp/ch17/vadd.ll
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel /tmp/ch17/vadd.ll -o - | grep PseudoV
```

实测（截取）：

```text
%27:gprnox0 = PseudoVSETVLI %3, 199 /* e8, mf2, ta, ma */, implicit-def dead $vl, implicit-def dead $vtype
%33:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %32, %30, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
PseudoVSE32_V_M2 %33, %34, %27 /* vl */, 5 /* e32 */
```

能直接看到：LMUL 在 opcode 后缀、SEW 是 `5`、policy 是 `3`。`PseudoVSETVLI` 的 `e8, mf2` 是前面的 intrinsic/lowering 路径留下的配置，后面会被改写。

### 实验 3：`riscv-vl-optimizer` 前后

```bash
for s in riscv-vl-optimizer; do
  echo BEFORE; $T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -stop-before=$s /tmp/ch17/vadd.ll -o - 2>/dev/null | grep PseudoVADD
  echo AFTER;  $T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -stop-after=$s  /tmp/ch17/vadd.ll -o - 2>/dev/null | grep PseudoVADD
done
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -debug-only=riscv-vl-optimizer /tmp/ch17/vadd.ll -o /dev/null 2>&1 | grep "Reduce VL"
```

实测（截取）：

```text
BEFORE
    %33:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %32, %30, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
AFTER
    %33:vrm2 = nsw PseudoVADD_VV_M2 $noreg, %32, %30, %27 /* vl */, 5 /* e32 */, 3 /* ta, ma */
```

debug 打印 `Reduce VL from -1 to %27`。

### 实验 4：`riscv-vector-peephole` 前后

```bash
cat > /tmp/ch17/vmerge.ll <<'EOF'
define <vscale x 4 x i32> @vmerge(<vscale x 4 x i32> %a, <vscale x 4 x i32> %b) {
  %r = call <vscale x 4 x i32> @llvm.riscv.vmerge.nxv4i32.nxv4i32(
      <vscale x 4 x i32> zeroinitializer, <vscale x 4 x i32> %a,
      <vscale x 4 x i32> %b, <vscale x 4 x i1> splat (i1 true), i64 4)
  ret <vscale x 4 x i32> %r
}
declare <vscale x 4 x i32> @llvm.riscv.vmerge.nxv4i32.nxv4i32(
    <vscale x 4 x i32>, <vscale x 4 x i32>, <vscale x 4 x i32>, <vscale x 4 x i1>, i64)
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-before=riscv-vector-peephole /tmp/ch17/vmerge.ll -o - 2>/dev/null | grep PseudoV
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=riscv-vector-peephole  /tmp/ch17/vmerge.ll -o - 2>/dev/null | grep PseudoV
$T/bin/llc -mtriple=riscv64 -mattr=+v /tmp/ch17/vmerge.ll -o - 2>/dev/null | sed -n '/^vmerge:/,/Lfunc_end/p'
```

实测关键行：`BEFORE %4:vrm2nov0 = PseudoVMERGE_VVM_M2 ...` → `AFTER %4:vrm2 = PseudoVMV_V_V_M2 ...`。最终汇编：

```text
vsetvli  a0, zero, e32, m2, ta, ma
vmv.v.i  v8, 0
vsetivli zero, 4, e32, m2, tu, ma
vmv.v.v  v8, v10
ret
```

最后一条 `vsetivli` 的 `tu, ma` 必须保留 `v8` 的尾元素（前一条 `vmv.v.i` 写的 0）；写成 `ta` 返回值在 lane 4.. 就是 undef。

### 实验 5：`llvm-mc` 验证 `vsetvli` 编码

```bash
printf 'vsetvli a0, zero, e32, m1, ta, ma\ncsrr a0, vlenb\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+v -show-encoding
# vsetvli a0, zero, e32, m1, ta, ma   # [0x57,0x75,0x00,0x0d]
# csrr    a0, vlenb                   # [0x73,0x25,0x20,0xc2]
```

`0x0d007557` 的 `vtypei=0xD0`：`vlmul=0`、`vsew=2`、`vta=1`、`vma=1`；`vsew=2` 对应 `log2(32)-3`。

### 实验 6：LMUL 变化（同一份 IR）

```bash
cat > /tmp/ch17/add4.ll <<'EOF'
define <4 x i32> @add4(<4 x i32> %a, <4 x i32> %b) {
  %r = add <4 x i32> %a, %b
  ret <4 x i32> %r
}
EOF
for opt in "-mattr=+v" "-mattr=+v -riscv-v-vector-bits-min=256"; do
  echo "== $opt"; $T/bin/llc -mtriple=riscv64 $opt -O2 /tmp/ch17/add4.ll -o - 2>/dev/null | grep -E "vsetivli|vadd"
done
```

实测：

```text
== -mattr=+v
vsetivli zero, 4, e32, m1, ta, ma
vadd.vv  v8, v8, v9
== -mattr=+v -riscv-v-vector-bits-min=256
vsetivli zero, 4, e32, mf2, ta, ma
vadd.vv  v8, v8, v9
```

同一个 `<4 x i32>`，最小 VLEN 从 128 提到 256 后，128-bit 数据只需 `LMUL=1/2`，于是 `m1 → mf2`。`-mattr=+v,+zvl128b` 与 `-mattr=+v` 没有差异，因为 `+v` 在 `RISCVFeatures.td:698` 已隐含 `zvl128b`。

### 实验 7：masked 操作

```bash
cat > /tmp/ch17/mask.c <<'EOF'
void madd(int *a, const int *b, const int *c, int n) {
  for (int i = 0; i < n; ++i) if (b[i] > 0) a[i] = c[i];
}
EOF
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -O2 \
  -ffreestanding -nostdinc -isystem $T/lib/clang/24/include -S /tmp/ch17/mask.c -o -
```

实测循环：`vsetvli a5, a3, e32, m2, ta, ma`、`vle32.v v8, (a7)`、`vmsgt.vi v0, v8, 0`、`vle32.v v8, (a7), v0.t`、`vse32.v v8, (a6), v0.t`。MIR 里是 `PseudoVMSGT_VI_M2` 与 `PseudoVLE32_V_M2_MASK ... $v0 ... 1 /* ta, mu */`。

masked 算术/merge 的编码也可以直接验：

```bash
printf 'vadd.vv v8, v8, v9, v0.t
vmerge.vvm v8, v9, v10, v0
' | $T/bin/llvm-mc -triple=riscv64 -mattr=+v -show-encoding
# vadd.vv v8, v8, v9, v0.t    # [0x57,0x84,0x84,0x00]
# vmerge.vvm v8, v9, v10, v0 # [0x57,0x04,0x95,0x5c]
```

### 实验 8：`riscv-insert-vsetvli` 前后

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -O2 \
  -ffreestanding -nostdinc -isystem $T/lib/clang/24/include \
  -S -emit-llvm /tmp/ch17/mask.c -o /tmp/ch17/mask.ll
for s in riscv-insert-vsetvli; do
  echo BEFORE; $T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -stop-before=$s /tmp/ch17/mask.ll -o - 2>/dev/null | grep PseudoV
  echo AFTER;  $T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -stop-after=$s  /tmp/ch17/mask.ll -o - 2>/dev/null | grep PseudoV
done
```

实测（截取）：`BEFORE %27:gprnox0 = PseudoVSETVLI %44, 199 /* e8, mf2, ta, ma */` → `AFTER %27:gprnox0 = PseudoVSETVLI %44, 209 /* e32, m2, ta, ma */`，同时向量指令的 `%27 /* vl */` 变成 `$noreg /* vl */, implicit $vl, implicit $vtype`。

**未实测**：DAG 图形化（`-view-dag-combine1-dags`）未跑，需要 graphviz/dot；QEMU/真机执行未跑，本章只验证到汇编与编码；`-debug-only=riscv-vector-peephole` 无输出（该 pass 没写 `LLVM_DEBUG`），不是命令失败。

## 17.12 踩雷集锦

1. **把 VL 当常量。** VLMAX 依赖运行时 VLEN；`vsetvli a0, zero` 能拿 VLMAX，但字节数要 `csrr vlenb`。不要在 lowering 里写死 4/8/16。
2. **policy 位漏设。** `ta` 会把 tail lane 变成 undef；`vmerge`/`vmv.v.v` 的 passthru 语义通常要求 `tu`（实验 4 的 `tu, ma`）。`mu` 只对 mask lane 有意义。
3. **把 `v0` 当普通 `VR`。** masked pseudo 用 `GetVRegNoV0`，MIR 里是 `vrm2nov0`；汇编器会拒绝 `vadd.vv v0, v8, v9, v0.t`（实测报 “destination vector register group cannot overlap the mask register”）。`VMV0`（`:943`）和 `VRNoV0`（`:908`）就是为此存在。
4. **误解 `$noreg`。** `vsetvli` 前是 AVL=VLMAX；`vsetvli` 后是“用当前 `vl`”，因为 AVL 被 `setReg(Register())` 擦掉（`:595`）并加了隐式 `$vl`（`:621`）。
5. **以为 `RISCVVectorPeephole` 合并 `vsetvli`。** 它跑在插入之前，文件里没有 `VSET`；合并看 `RISCVInsertVSETVLI` 的 `coalesceVSETVLIs`（`:819`）。
6. **以为 `SEW` 立即数就是 SEW。** MIR 里的 `5` 是 `log2(32)`；VTYPE 的 3-bit `vsew` 是 `log2(SEW)-3`。把 `5` 当宽度会直接算错 VLMAX。
7. **以为 `+v,+zvl128b` 会比 `+v` 多出什么。** `+v` 已隐含 `zvl128b`（`RISCVFeatures.td:698`–`:702`）；要观察 LMUL/container 变化，用 `-riscv-v-vector-bits-min=`。
8. **随手合并 `vsetvli`。** 合法性由 `areCompatibleVTYPEs` 决定：SEW、LMUL、`SEW/LMUL` ratio、tail/mask policy、`altfmt`、`twiden` 任何一项不兼容都不能合。合错了不报错，只会让元素被重新解释。

## 17.13 本章重点整理

- 一条 RVV 指令由 SEW、LMUL、VL、mask/policy 四维决定；LMUL 在 opcode/TSFlags，SEW 是 `log2(SEW)` 立即数，VL 是显式操作数，policy 是 2-bit 立即数。
- `VLMAX = floor(VLEN*LMUL/SEW)`；VLEN 编译期未知，运行时用 `vlenb`/`vsetvli` 获取。
- DAG 层用 `MVT::nxv*`/`vscale` 表示可伸缩向量；fixed-length 通过 `getContainerForFixedLengthVector` 映射到 container。
- `RISCVISD::ADD_VL` 把 VL 显式放进 DAG；`getVLOperand` 只服务 intrinsic，本树没有 `getVLMergeOperand`。
- `PseudoVADD_VV_M1` 的命名来自 LMUL；SEW 不在 opcode 里。操作数顺序是 passthru、rs2、rs1、[vm]、vl、sew、policy。
- `RISCVVLOptimizer` 做逆向 demanded-VL 数据流；`RISCVVectorPeephole` 做 Pseudo 化简，不合并 `vsetvli`；`RISCVInsertVSETVLI` 负责插入/合并并改用隐式 `$vl`/`$vtype`。
- `RISCVVectorMaskDAGMutation` 是调度依赖 workaround，不是 mask 指令合并。

## 17.14 自我检核

- [ ] 能写出 `VLMAX = floor(VLEN*LMUL/SEW)`，并解释 `vlenb`、`vsetvli a0, zero` 各拿到什么
- [ ] 能解释 `$noreg` 在 `riscv-insert-vsetvli` 前后的两种含义
- [ ] 能说出 `PseudoVADD_VV_M2` 的 6 个操作数顺序，以及 `5`/`3` 分别是什么
- [ ] 能解释为什么 `PseudoVADD_VV` 只有 14 个 opcode 而不是 56 个
- [ ] 能指出 `RISCVVLOptimizer` 的 `transfer`/`tryReduceVL` 行号，并复现一次 VL 降低
- [ ] 能指出 `convertAllOnesVMergeToVMv` 行号，并解释它不碰 `vsetvli`
- [ ] 能说出 `areCompatibleVTYPEs` 至少检查 5 项，并解释为什么 ratio 也要看
- [ ] 能用 `-mattr=+v` 与 `-riscv-v-vector-bits-min=256` 复现 `m1 → mf2`
- [ ] 能解释 masked 指令为什么不能用 `v0` 作为数据目标

## 17.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVVLOptimizer.cpp`**：`:46`–`:63` 的 `DemandedVL`、`:1128` 的 `getMinimumVLForUser`、`:1316` 的 `tryReduceVL`、`:1395` 的 `transfer`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVVectorPeephole.cpp`**：`:131`、`:287`、`:413`、`:654`、`:816`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInsertVSETVLI.cpp`**：`:160` 的 `insertVSETVLI`、`:758` 的 `canMutatePriorConfig`、`:819` 的 `coalesceVSETVLIs`、`:595`/`:621` 的 AVL 擦除与隐式 `$vl`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVVSETVLIInfoAnalysis.cpp`**：`:114` 的 `areCompatibleVTYPEs`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td`**：`:175` 的 `LMULInfo`、`:1225`/`:1366` 的两种 binary pseudo、`:6098` 的 `PseudoVSET*`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/rvv-peephole-vmerge-to-vmv.mir`**：实验 4 的最小回归样本。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/vl-opt.mir`** 与 **`vl-opt-instrs.ll`**：`RISCVVLOptimizer` 的 candidate 与 `Reduce VL` 预期。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/allone-masked-to-unmasked.mir`**：`convertToUnmasked` 的真实场景。

→ 下一章：[练习 C — 诊断向量指令选择](./practice-c-vector-selection.md)
