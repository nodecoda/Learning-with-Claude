# Ch 16 — DAG combine 与 known bits：选指前的最后一次改写

> 本章目标：知道 `PerformDAGCombine` 在什么时机跑、能改什么；理解“combine 改 DAG，isel 选 opcode”的分工；并能用 `computeKnownBitsForTargetNode` 解释为什么某些序列能少一条指令。

---

## 16.1 combine 在流水线里的位置

```text
IR → DAG → 合法化 → DAG combine（多轮）→ 选指 → 调度 → 寄存器分配 → ...
```

`PerformDAGCombine()`（`LoongArchISelLowering.cpp:9084`）按 opcode 分派：

```cpp
switch (N->getOpcode()) {
default: break;
case ISD::ADD:   return performADDCombine(...);
case ISD::AND:   return performANDCombine(...);
case ISD::OR:    return performORCombine(...);
case ISD::SETCC: return performSETCCCombine(...);
case ISD::SELECT:return performSELECTCombine(...);
case ISD::SHL:   return performSHLCombine(...);
case ISD::SRL:   return performSRLCombine(...);
case ISD::SUB:   return performSUBCombine(...);
case ISD::BITCAST: return performBITCASTCombine(...);
case ISD::SIGN_EXTEND/ZERO_EXTEND/ANY_EXTEND: return performEXTENDCombine(...);
case ISD::SINT_TO_FP / UINT_TO_FP / FP_TO_SINT / FP_TO_UINT: ...
case LoongArchISD::BITREV_W / BR_CC / SELECT_CC: ...
case ISD::INTRINSIC_WO_CHAIN: return performINTRINSIC_WO_CHAINCombine(...);
case LoongArchISD::MOVGR2FR_W_LA64 / MOVFR2GR_S_LA64: ...
}
```

**关键点**：combine 在每个节点上可能被调用多次（DAGCombiner 迭代到不动点），所以每个 `perform*Combine` 必须：

1. 先做 `DCI.isBeforeLegalizeOps()` 等时机检查；
2. 只返回“确实更优”的节点，否则返回 `SDValue()`；
3. 不破坏已有节点的类型/链。

---

## 16.2 两个阶段的 combine

`performADDCombine` 开头：

```cpp
if (SDValue V = performHorizWideningCombine(N, DAG, Subtarget))
  return V;

if (DCI.isBeforeLegalizeOps())
  return SDValue();

EVT VT = N->getValueType(0);
if (!VT.isVector())
  return SDValue();
```

含义：

- **合法化前**只做与类型无关的规范化（如水平加宽）；
- **合法化后**才做“依赖合法类型”的向量优化；
- 标量 `ADD` 在这里直接返回空——标量的加法优化交给通用 DAGCombiner（如 `add x, 0`、常量折叠）。

所以“为什么 `add i64 %x, 0` 没有生成指令”不是 LoongArch 的 combine 做的，是通用 DAGCombiner 做的。**读代码时要分清 target combine 和 generic combine。**

---

## 16.3 combine 能做什么：几个实测

### `fshl` → `rotri`

```llvm
%r = call i64 @llvm.fshl.i64(i64 %x, i64 %x, i64 3)
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
	rotri.d	$a0, $a0, 61
```

左旋 3 位 = 右旋 61 位。LoongArch 只有 `rotri.d`（立即数右旋），所以 combine 把 `fshl` 规范成右旋形式。

### `mul` 常量 → `slli`

```llvm
%r = mul i64 %x, 8
```

```text
	slli.d	$a0, $a0, 3
```

这是通用 DAGCombiner 的强度削减，不是 target combine。

### `select (icmp eq x, 0), 1, 0` → `sltui`

```llvm
%c = icmp eq i64 %x, 0
%r = select i1 %c, i64 1, i64 0
```

```text
	sltui	$a0, $a0, 1
```

`x == 0` 等价于 `(unsigned)x < 1`。这是 `performSETCCCombine`/`performSELECTCombine` 一类的工作：把“比较 + 选择”折叠成一条 set 指令。

**这三条例子分别代表三种来源**：target combine、generic combine、target setcc combine。写补丁前先判断该改哪一层。

---

## 16.4 `computeKnownBitsForTargetNode`

`computeKnownBitsForTargetNode()`（`:11990`）告诉优化器“这个 target 节点的结果哪些位一定为 0/1”。它被 combine、分支折叠、位宽推断共用。

三个例子：

### `MOVCF2GR` 的结果高 63 位一定是 0

```cpp
case LoongArchISD::VANYNONZERO:
case LoongArchISD::VALLZERO:
...
  // MOVCF2GR zero-extend the i1 cond to GPR.
  Known.Zero.setBitsFrom(1);
```

向量比较结果通过 `MOVCF2GR` 搬到 GPR 时只写最低位，其余位是 0。知道这一点后，`(and x, 1)` 可以省掉。

### `VPICK_ZEXT_ELT` 的高位

```cpp
unsigned VTBits = VT.getScalarSizeInBits();
Known.Zero.setBitsFrom(VTBits);
```

取元素并零扩展到 GPR，高于元素宽度的位一定是 0。

### `BSTRINS` 的位段

```cpp
// BSTRINS X, Y, msb, lsb: replace X[msb:lsb] with Y[msb-lsb:0].
unsigned Msb = Op.getConstantOperandVal(2);
unsigned Lsb = Op.getConstantOperandVal(3);
Known = DAG.computeKnownBits(Op.getOperand(0), Depth + 1);
// ... 再用 Y 的 known bits 覆盖 [Msb:Lsb]
```

位段插入的 known bits 是“底值 + 覆盖段”的组合，必须逐位算，不能简单取交集。

---

## 16.5 什么时候必须更新 known bits

如果你新增一个 target 节点，而它的结果有“必然为 0”的位，却忘了在 `computeKnownBitsForTargetNode` 里声明：

- 下游的 `(and ...)`、`(icmp ...)` 不会简化；
- 可能生成比你预期多 1–2 条指令的代码；
- 但**不会错**——known bits 是保守的，漏报只损失优化，虚报才会出错。

反过来，如果你**虚报**了 known bits（说某位一定是 0，实际不是），会生成错误代码，而且很难用 lit 测出来（需要构造恰好触发的输入）。

---

## 16.6 反直觉点

1. **`add x, 0` 的消除不是 LoongArch 的锅**，是通用 DAGCombiner。
2. **target combine 会跑多轮**，不要在里面做“只跑一次才安全”的状态变更。
3. **`isBeforeLegalizeOps()` 之后才能碰合法类型**；在合法化前对 `v4i32` 做假设会崩。
4. **`fshl` 被规范成 `rotri`（右旋）**，因为 LoongArch 没有左旋立即数指令。
5. **known bits 漏报只损失性能，虚报会产生错误代码**；这是 review 时最该盯的地方。
6. **`MOVCF2GR` 只写最低位**，所以向量比较结果转 GPR 后可以直接当 bool 用。
7. **combine 不改变指令数上限**，它只是把 DAG 改写成更容易选指的形式；真正的“少一条指令”往往发生在 combine + isel 配合。

---

## 16.7 本章证据

| 结论 | 源码/命令 |
|---|---|
| combine 分派 | `LoongArchISelLowering.cpp:9084-9130` |
| `performADDCombine` 时机检查 | `:6426-6445` |
| known bits 实现 | `:11990+` |
| `MOVCF2GR` 零扩展 | `computeKnownBitsForTargetNode` 的 VANY* case |
| `VPICK_ZEXT_ELT` | 同上 |
| `BSTRINS` | 同上 |
| `rotri`/`slli`/`sltui` 实测 | 本章 16.3 |

---

## 16.8 练习

1. 用 `-debug-only=dagcombine`（assertions 构建）观察 `fshl` 被改写成 `rotl` 再被选成 `rotri.d` 的过程。
2. 把 `BSTRINS` 的 known bits 逻辑读完整，说明为什么不能直接取 `computeKnownBits(Op0) & computeKnownBits(Op1)`。
3. 构造一个 `(and (MOVCF2GR ...), 1)` 的例子，验证 `and` 被消掉。
4. 找 `performSELECTCombine` 里把 `select` 变成 `masknez`/`maskeqz` 的分支，解释收益。
5. 在 `computeKnownBitsForTargetNode` 里加一个 `LLVM_DEBUG`，观察它在 `-O2` 下被调用的次数。

---

## 16.9 延伸阅读

1. **DAG Combiner 的官方阶段说明** — `llvm/docs/CodeGenerator.md`
   - 读什么：`Instruction Selection` 下的 `SelectionDAG Optimization Phase: the DAG Combiner` 小节，连同前面的 `SelectionDAG Legalize Phase`
   - 学到什么：文档明确 combine 在 DAG 建好后与每次 legalize 后都会跑，并把“插入的符号/零扩展”列为专门优化对象。这补上本章 16.2 只看 LoongArch 代码时的视角：`isBeforeLegalizeOps()` 的分界不是 LoongArch 特有，而是整个 SelectionDAG 的分阶段设计；也解释了为什么 `sext` 规范化首先是 generic DAGCombiner 的职责，而不是 LoongArch combine 的专利。
   - 前提：知道 `SDNode`/`SDValue` 与 legalize types/ops 的基本分工。

2. **Known bits 作为可复用 analysis** — `llvm/docs/GlobalISel/KnownBits.md`
   - 读什么：`Examples` 与 `Usage` 两节（`G_AND` 例子、`GISelValueTracking::getKnownBits`、`Known.Zero[0]`）
   - 学到什么：这里把 known bits 做成惰性计算、可被多个 pass 声明依赖的 analysis，和本章 `computeKnownBitsForTargetNode` 的“逐节点回调”形成对照。读完能分清两种接口的代价模型：前者只在需要时算，后者由 DAG 组合器在遍历中反复问。
   - 前提：读过本章 16.4，知道 `KnownBits` 的 `Zero`/`One` 表示；不需要先学 GlobalISel 的指令选择。

3. **BSTRINS/BSTRPICK 的 known bits 回归测试** — `llvm/test/CodeGen/LoongArch/bstrins-bstrpick-known-bits.ll`
   - 读什么：`utf8_rev_col`、`bstrins_low_sext`、`bstrpick_zext` 三个函数的 IR 与 `CHECK-NEXT` 序列
   - 学到什么：本章 16.4 说 BSTRINS 的 known bits 要“底值 + 覆盖段”逐位组合；这个测试给出三个可观察后果：PHI 合并后省掉 `sext.w`、`bstrins.d` 之后仍能用 `addi.d`+`sltui` 比较、`bstrpick.d` 的高位为零。它还示范了这类优化的检查是 autogenerated 的，改 combine 后要整体更新断言。
   - 前提：会读 `llc | FileCheck` 的 `CHECK-NEXT`，知道 `zext nneg`、`or disjoint` 的语义。

4. **目标节点 known bits 与符号位推理** — `llvm/lib/Target/LoongArch/LoongArchISelLowering.cpp`
   - 读什么：`PerformDAGCombine` 的 switch 分派（`:9084`）、`performSETCCCombine`（`:7394`）/`performSELECTCombine`（`:7535`），以及 `computeKnownBitsForTargetNode`（`:11990`）里的 `VANY*`、`VPICK_ZEXT_ELT`、`BSTRINS`、`BSTRPICK` 四个 case；顺带看紧邻的 `ComputeNumSignBitsForTargetNode`
   - 学到什么：把本章 16.3 的 `sltui` 例子和 16.4 的三段 known-bits 放回同一文件，能看到两条独立路径：combine 返回新节点改图，known-bits 只声明事实。源码还多出本章没提的两点：`BSTRPICK` 的 `extractBits(...).zext()`，以及 `ComputeNumSignBitsForTargetNode` 对 BSTRINS 高位来源的分情况推理。
   - 前提：能读 `SDValue`、`DAG.getNode`、`DAG.computeKnownBits` 的基本写法。

## 16.10 下一步

下一章进入向量：LSX/LASX 的 `BUILD_VECTOR`、shuffle、reduction 怎么 lowering，以及为什么 LoongArch 的向量 lowering 比标量复杂一个数量级。
