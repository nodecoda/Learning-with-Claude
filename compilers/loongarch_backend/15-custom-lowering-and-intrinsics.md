# Ch 15 — 自定义 lowering 与 intrinsic：pattern 表达不了的部分

> 本章目标：看懂 `LowerOperation` 的分派表；知道 intrinsic 在 LoongArch 上大多**不是** C++ lowering，而是 `.td` 里用字符串拼出来的 pattern；并能说出加一个 intrinsic 要改哪四个地方。

---

## 15.1 `LowerOperation`：一张分派表

`LoongArchISelLowering.cpp:590` 起：

```cpp
SDValue LoongArchTargetLowering::LowerOperation(SDValue Op, SelectionDAG &DAG) const {
  switch (Op.getOpcode()) {
  case ISD::ATOMIC_FENCE:      return lowerATOMIC_FENCE(Op, DAG);
  case ISD::EH_DWARF_CFA:      return lowerEH_DWARF_CFA(Op, DAG);
  case ISD::GlobalAddress:     return lowerGlobalAddress(Op, DAG);
  case ISD::GlobalTLSAddress:  return lowerGlobalTLSAddress(Op, DAG);
  case ISD::INTRINSIC_WO_CHAIN: return lowerINTRINSIC_WO_CHAIN(Op, DAG);
  ...
  }
}
```

完整的 case 列表有 50 多项，覆盖：

| 类别 | 例子 |
|---|---|
| 地址 | `GlobalAddress`、`GlobalTLSAddress`、`JumpTable`、`ConstantPool` |
| 128 位拆分 | `SHL_PARTS`、`SRA_PARTS`、`SRL_PARTS` |
| 浮点转换 | `FP_TO_SINT`、`FP_TO_UINT`、`UINT_TO_FP`、`FP_TO_FP16` |
| 向量 | `BUILD_VECTOR`、`VECTOR_SHUFFLE`、`INSERT_VECTOR_ELT`、`VECREDUCE_*` |
| 控制流 | `BRCOND`、`SELECT`、`SETCC` |
| 栈/异常 | `DYNAMIC_STACKALLOC`、`FRAMEADDR`、`RETURNADDR` |
| intrinsic | `INTRINSIC_WO_CHAIN`、`INTRINSIC_W_CHAIN`、`INTRINSIC_VOID` |

**判定规则**：只有当合法化阶段把某个操作标成 `Custom`（Ch 13）时，才会走到这里。`Expand`/`Legal` 不会。

---

## 15.2 intrinsic 的三条入口

```cpp
case ISD::INTRINSIC_WO_CHAIN: return lowerINTRINSIC_WO_CHAIN(Op, DAG);
case ISD::INTRINSIC_W_CHAIN:  return lowerINTRINSIC_W_CHAIN(Op, DAG);
case ISD::INTRINSIC_VOID:     return lowerINTRINSIC_VOID(Op, DAG);
```

`lowerINTRINSIC_WO_CHAIN()`（`:4874`）第一行：

```cpp
switch (Op.getConstantOperandVal(0)) {
default:
  return SDValue();   // Don't custom lower most intrinsics.
```

**“大多数 intrinsic 不做自定义 lowering”**——它们直接由 `.td` 的 pattern 匹配成指令。C++ 这里主要做两件事：

1. **立即数范围检查**：`checkIntrinsicImmArg<N>(Op, ...)` 检查第 N 个立即数是否在合法范围（如 `vldi` 的 13 位、`vrepli` 的 10 位）。越界会报 `error: argument out of range`。
2. **特殊 lowering**：如 `Intrinsic::thread_pointer` → `DAG.getRegister(LoongArch::R2, PtrVT)`，直接返回 `$tp`。

`lowerINTRINSIC_W_CHAIN()`（`:5162`）和 `lowerINTRINSIC_VOID()`（`:5283`）处理带链的（load/store 类、原子、cache 操作），错误处理统一走 `emitIntrinsicWithChainErrorMessage()`。

---

## 15.3 `.td` 里的 intrinsic pattern 是“拼”出来的

以 LSX 为例（`LoongArchLSXInstrInfo.td:3119`）：

```tablegen
class deriveLSXIntrinsic<string Inst> {
  Intrinsic ret = !cast<Intrinsic>(!tolower("int_loongarch_lsx_"#Inst));
}

foreach Inst = ["VADD_W", "VSUB_W", ...] in
  def : Pat<(deriveLSXIntrinsic<Inst>.ret
               (v4i32 LSX128:$vj), (v4i32 LSX128:$vk)),
            (!cast<LAInst>(Inst) LSX128:$vj, LSX128:$vk)>;
```

这段代码的含义：

- `"int_loongarch_lsx_" # Inst` 然后 `!tolower` → `int_loongarch_lsx_vadd_w`；
- `!cast<Intrinsic>` 把它变成 TableGen 的 intrinsic 记录；
- `!cast<LAInst>(Inst)` 把字符串 `"VADD_W"` 变成指令定义；
- 于是 **intrinsic 名与指令名必须能通过“小写 + 前缀”对上**。

实测：

```llvm
declare <4 x i32> @llvm.loongarch.lsx.vadd.w(<4 x i32>, <4 x i32>)
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx -O2
vadd:
	vadd.w	$vr0, $vr0, $vr1
	ret
```

一条指令，没有 C++ lowering 参与。

非向量 intrinsic 也类似。实测 `crc.w.d.w`：

```llvm
declare i32 @llvm.loongarch.crc.w.d.w(i64, i32)
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lbt -O2
crc:
	crc.w.d.w	$a0, $a0, $a1
	ret
```

---

## 15.4 加一个 intrinsic 要改哪四处

以“给 LSX 加一个 `vfoo.w`”为例：

1. **`llvm/include/llvm/IR/IntrinsicsLoongArch.td`**
   ```tablegen
   def int_loongarch_lsx_vfoo_w : VecInt<[llvm_v4i32_ty],
                                          [llvm_v4i32_ty, llvm_v4i32_ty]>;
   ```
   `VecInt` 会带上 `IntrNoMem` 等属性。

2. **`clang/include/clang/Basic/BuiltinsLoongArchLSX.def`**（如果要暴露 builtin）
   ```c
   TARGET_BUILTIN(__builtin_lsx_vfoo_w, "V4iV4iV4i", "nc", "lsx")
   ```

3. **`llvm/lib/Target/LoongArch/LoongArchLSXInstrInfo.td`**
   - 指令 `def VFOO_W : LSX3R_VVV<0x...>, Sched<[...]>;`
   - pattern：如果命名对得上，把 `"VFOO_W"` 加进 `foreach` 列表即可；否则手写 `Pat<(int_loongarch_lsx_vfoo_w ...)>`。

4. **测试**
   - `llvm/test/CodeGen/LoongArch/lsx/intrinsic-vfoo.ll`
   - `clang/test/CodeGen/LoongArch/builtin-lsx-vfoo.c`

**顺序很重要**：先加 intrinsic 定义（否则 TableGen 里 `!cast<Intrinsic>` 找不到），再加指令/pattern，最后加 builtin 和测试。

---

## 15.5 立即数 intrinsic 的坑

`checkIntrinsicImmArg<N>(Op, ArgIdx, DAG, IsSigned)` 检查的是 **IR 里的常量**。如果立即数不是编译期常量，会在 lowering 阶段报错：

```text
error: argument out of range
```

这类 intrinsic 包括 `vldi`（13 位）、`vrepli`（10 位）、`vpickve2gr`（lane 索引）等。

**反直觉**：指令本身可能有更大的编码空间，但 intrinsic 的立即数范围由 `.td` 的 `ImmLeaf`/`timm` 约束和 C++ 检查共同决定。改范围要同时改两处。

---

## 15.6 自定义 `SDNode`

```tablegen
def loongarch_call : SDNode<"LoongArchISD::CALL", SDT_LoongArchCall,
                            [SDNPHasChain, SDNPOptInGlue, SDNPOutGlue, SDNPVariadic]>;
```

这些节点：

- 在 `LowerOperation`/`LowerCall` 里创建；
- 在 `PerformDAGCombine` 里可能被优化（Ch 16）；
- 最后由 `.td` 的 pattern 或 `Select()` 选中。

什么时候该用自定义节点而不是普通 pattern：

| 情况 | 方式 |
|---|---|
| 一条指令能表达，操作数是普通寄存器/立即数 | `Pat<...>` |
| 需要 C++ 计算地址、拆立即数、查 feature | `ComplexPattern` |
| 需要多指令序列、伪指令、特殊 ABI/链 | `LoongArchISD::*` + `LowerOperation` |
| 只影响编码/调度 | 改 `Fmt*`/`Sched<>` |

---

## 15.7 反直觉点

1. **大多数 intrinsic 没有 C++ lowering**；`LowerOperation` 里的 `default: return SDValue()` 就是证据。
2. **intrinsic 名与指令名靠字符串约定绑定**；改名字会静默地让 pattern 失效（TableGen 会报 `!cast` 失败，但如果是手写 pattern 就不会）。
3. **立即数检查在 lowering，不在 verifier**；所以同一个 intrinsic 在 `-O0` 可能通过、在常量折叠后又报错。
4. **`INTRINSIC_VOID` 不一定真的“无返回值”**，它可能带链（`SDNPHasChain`）。
5. **`thread_pointer` 是唯一直接返回物理寄存器的 intrinsic**（`$tp`）。
6. **`crc.w.d.w` 的第一个参数是 `i64`**（不是 `i32`），因为它是“64 位数据 + 32 位初值”；签名错了 verifier 直接拒绝。

---

## 15.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| `LowerOperation` 分派 | `LoongArchISelLowering.cpp:590-700` |
| intrinsic 三入口 | `:605-610` |
| “不 custom lower” | `:4874-4881` |
| 立即数检查 | `:4883-4900` |
| `W_CHAIN`/`VOID` | `:5162`、`:5283` |
| LSX intrinsic 生成 | `LoongArchLSXInstrInfo.td:3119-3145` |
| `crc` 实测 | 本章 15.3 |
| `vadd.w` 实测 | 本章 15.3 |
| intrinsic 定义规模 | `IntrinsicsLoongArch.td` 1233 行 |

---

## 15.9 练习

1. 找出 `int_loongarch_lsx_vldi` 的立即数范围检查，确认越界时报什么错。
2. 给 `vadd.w` 换一个 intrinsic 名（只改 `.td`），观察 TableGen 报什么错。
3. 读 `lowerINTRINSIC_W_CHAIN` 的一个 case，说明它为什么要保留链。
4. 找一个“没有对应指令、必须 C++ lowering”的 intrinsic，解释它展开成什么。
5. 统计 `IntrinsicsLoongArch.td` 里 LSX/LASX/LBT/LVZ 各占多少行。

---

## 15.10 延伸阅读

1. **`immarg` 的规范约束** — `llvm/docs/LangRef.md`
   - 读什么：`### Parameter Attributes` 里的 `immarg`（1718 行附近），以及 `## Intrinsic Functions`（14637 行起）关于 intrinsic 必须是外部函数、可重载的规则。
   - 学到什么：本章 15.5 说“立即数检查在 lowering”；这一节解释 `immarg` 在 IR 层的语义，能说明为什么非编译期常量会在 verifier 或 lowering 阶段被拒。
   - 前提：读过本章 15.2 和 15.5。

2. **intrinsic 与指令名的字符串绑定** — `llvm/lib/Target/LoongArch/LoongArchLSXInstrInfo.td`
   - 读什么：3119–3143 行的 `deriveLSXIntrinsic` 类与第一组 `foreach Inst = [...]`，注意 `!tolower("int_loongarch_lsx_"#Inst)` 和 `!cast<LAInst>(Inst)`。
   - 学到什么：本章 15.3 用简化版展示了“名字对上就能匹配”；源码里还有按元素宽度分组的多个 `foreach`，能看出命名约定失效时会在哪一步报错。
   - 前提：读过本章 15.3。

3. **立即数越界的诊断** — `llvm/test/CodeGen/LoongArch/intrinsic-error.ll`
   - 读什么：文件开头的 `RUN: not llc`，以及 `dbar_imm_out_of_hi_range` 等函数和对应的 `argument out of range` `CHECK`。
   - 学到什么：本章 15.5 只说了报错文本；这份测试列出 `dbar`/`ibar`/`break`/`csrrd` 等多个 intrinsic 的边界值，能帮你判断改 `checkIntrinsicImmArg` 时该补哪些用例。
   - 前提：读过本章 15.5。

4. **GlobalISel 的 target intrinsic 路径** — `llvm/docs/GlobalISel/IRTranslator.md`
   - 读什么：`## Target Intrinsics`（34 行起）。
   - 学到什么：这一节明确说 target intrinsic 的可定制 lowering 还没有实现；读它能避免误以为 GlobalISel 有与 SelectionDAG `.td` pattern 对等的 intrinsic 机制，也能解释本章为什么把 intrinsic 主要交给 pattern。
   - 前提：知道本章 15.1 的 `LowerOperation` 分派和 GlobalISel 的四段流水。

## 15.11 下一步

下一章看 DAG combine：`PerformDAGCombine` 如何在选指前把 `(add x, 0)`、`(sext_inreg ...)` 这类节点消掉，以及 `computeKnownBitsForTargetNode` 怎么帮助 combine。
