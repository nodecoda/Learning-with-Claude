# Ch 14 — SelectionDAG 选指：`Pat<>`、`ComplexPattern` 与手写 `Select`

> 本章目标：知道一条合法化后的 DAG 节点会先被 TableGen 生成的匹配器尝试，再落到 `LoongArchISelDAGToDAG::Select` 的手写分支；能解释 `ld.d` / `ldptr.d` / `ldx.d` 三种寻址方式各自被谁选中。

---

## 14.1 两条选指路径

```text
Legalized DAG
   │
   ├─ 1. TableGen 生成的 matcher（LoongArchGenDAGISel.inc）
   │     按 pattern 的“复杂度”排序，逐条尝试
   │
   └─ 2. 手写 Select(SDNode*)（LoongArchISelDAGToDAG.cpp:35）
         处理 matcher 表达不了的情况
```

`Select()` 开头：

```cpp
void LoongArchDAGToDAGISel::Select(SDNode *Node) {
  if (Node->isMachineOpcode()) { Node->setNodeId(-1); return; }
  unsigned Opcode = Node->getOpcode();
  ...
  switch (Opcode) {
  default: break;
  case ISD::Constant: ...
  case ISD::FrameIndex: ...
  case ISD::BITCAST: ...
  case ISD::BUILD_VECTOR: ...
  }
  SelectCode(Node);   // 落到生成的 matcher
}
```

**顺序很重要**：手写分支先跑，没处理的才 `SelectCode()`。所以你在 `.td` 里加了 pattern 却没生效，先确认它没被 `Select()` 提前截胡。

---

## 14.2 pattern 的三种写法

### 直接 `Pat`

```tablegen
def : PatGprGpr<add, ADD_D>;                 // add -> add.d
def : PatGprImm<add, ADDI_D, simm12>;        // add x, imm12 -> addi.d
```

`PatGprGpr` 展开就是：

```tablegen
class PatGprGpr<SDPatternOperator OpNode, LAInst Inst>
    : Pat<(OpNode GPR:$rj, GPR:$rk), (Inst GPR:$rj, GPR:$rk)>;
```

**pattern 顺序**：TableGen 会按“越具体越先”排序（常量优先于寄存器），所以你不需要手写 `if` 判断 `simm12`——`PatGprImm` 用 `simm12` 这个 `Operand` 做了约束。

### `ComplexPattern`

```tablegen
def BaseAddr      : ComplexPattern<iPTR, 1, "SelectBaseAddr">;
def AddrConstant  : ComplexPattern<iPTR, 2, "SelectAddrConstant">;
def NonFIBaseAddr : ComplexPattern<iPTR, 1, "selectNonFIBaseAddr">;
def AddrRegImm    : ComplexPattern<iPTR, 2, "SelectAddrRegImm12">;
```

`ComplexPattern<iPTR, 2, "...">` 的意思是：匹配一个指针值，**拆出 2 个结果**（基址 + 立即数），由 C++ 函数 `SelectAddrRegImm12` 决定怎么拆。这类 pattern 用于：

- 基址 + 偏移（`ld.d` 的 `[rj, imm12]`）；
- 需要判断偏移是否在范围内、是否 2 字节对齐（`ldptr` 的 imm14 lsl2）；
- 需要拆 `GEP` 的常量部分。

### 自定义 `SDNode`

```tablegen
def loongarch_call : SDNode<"LoongArchISD::CALL", ...>;
```

用于“一条指令 + 特殊 ABI/链”的场景，必须在 C++ 里构造和 lower（Ch 15）。

---

## 14.3 实测：三种寻址方式

```llvm
define i64 @off12(ptr %p)   { %q = gep i8, ptr %p, i64 100;   load }
define i64 @offbig(ptr %p)  { %q = gep i8, ptr %p, i64 5000;  load }
define i64 @o40k(ptr %p)    { %q = gep i8, ptr %p, i64 40000; load }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
```

| 偏移 | 生成 | 依据 |
|---|---|---|
| 100 | `ld.d $a0, $a0, 100` | `LD_D : LOAD_2RI12`，12 位立即数 |
| 5000 | `ldptr.d $a0, $a0, 5000` | `LDPTR_D : LOAD_2RI14`，14 位、左移 2（±32KB） |
| 40000 | `lu12i.w $a1, 9; ori $a1,$a1,3136; ldx.d $a0,$a0,$a1` | 偏移超出 imm14，物化到寄存器，用 `ldx.d` |

`5000` 之所以能用 `ldptr.d`：`5000/4 = 1250`，落在 14 位有符号范围（±8191）。`40000/4 = 10000` 超了。

对应 `.td`：

```tablegen
def LD_D    : LOAD_2RI12<0x28c00000>;
def LDPTR_D : LOAD_2RI14<0x26000000>;

let Predicates = [IsLA64] in {
defm : LdPat<load, LD_D, i64>;                       // 12 位偏移
def : Pat<(i64 (load (AddLike BaseAddr:$rj, simm14_lsl2:$imm14))),
          (LDPTR_D BaseAddr:$rj, simm14_lsl2:$imm14)>;   // 14 位、左移 2
}
```

注意 `LDPTR_D` 的 pattern 用的是 `AddLike`（`add` 或 `or`，见 `LoongArchInstrInfo.td:1357`），不是裸 `add`——因为 GEP 的地址计算可能被合并成 `or`。

**`ldptr.d` 是「大偏移」优化**：它把偏移左移 2 位编码，用 14 位表示 ±32KB 的字节偏移，省掉一次地址物化。这个设计在 RISC 里不常见，值得记住。

---

## 14.4 手写 `Select` 的四个分支

### `ISD::Constant`

Ch 08 讲过：`Imm == 0` 直接 `COPY $r0`，否则调 `generateInstSeq()` 展开。这里再强调一次：**常量物化不走 pattern**，因为一条 `ISD::Constant` 可能对应 1–5 条指令，pattern 表达不了。

### `ISD::FrameIndex`

把栈对象索引改成 `$sp/$fp` + 偏移。这是为什么 `FrameIndex` 不进 matcher：偏移可能超出 12 位，需要 `adjustReg` 之类的处理。

### `ISD::BITCAST`

`i64 <-> f64`、`v2i64 <-> v2f64` 这类位cast，在 LoongArch 上通常是一条 `movfr2gr.d`/`movgr2fr.d` 或零指令（寄存器类别名）。手写是因为 TableGen 的 `BITCAST` pattern 很难覆盖所有类型对。

### `ISD::BUILD_VECTOR`

构造向量：全零、全常量、`splat` 等有专门优化（`vldi`/`vrepli.b`）。这部分和 LSX/LASX 强相关（Ch 17）。

---

## 14.5 怎么调试“为什么选了这条”

1. **看 matcher 的决策**（assertions 构建）：

   ```bash
   llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
     -debug-only=isel fn.ll -o /dev/null 2>&1 | grep -A3 'Selecting'
   ```

2. **看生成的 matcher 表**：

   ```bash
   llvm-tblgen -gen-dag-isel -I llvm/include -I llvm/lib/Target/LoongArch \
     llvm/lib/Target/LoongArch/LoongArch.td -o /tmp/GenDAGISel.inc
   grep -n 'ADD_D' /tmp/GenDAGISel.inc | head
   ```

   本树生成物约 **56,559 行 / 4.1MB**（Ch 04）。

3. **看 `-print-after=isel` 的 MIR**，确认 opcode。

**常见误判**：`.td` 里加了 pattern，但 `Select()` 提前处理了该 opcode；或者 pattern 的 `Predicates` 不满足（feature 没开）。先看 `-debug-only=isel` 的 “Selecting” 行。

---

## 14.6 `PatGprGpr_32` 与 `assertsexti32`

```tablegen
class PatGprGpr_32<SDPatternOperator OpNode, LAInst Inst>
    : Pat<(sext_inreg (OpNode (assertsexti32 GPR:$rj), (assertsexti32 GPR:$rk)), i32),
          (Inst GPR:$rj, GPR:$rk)>;
```

这是给 **LA32 的 32 位运算**用的：LA32 的寄存器是 64 位宽，`add.w` 只保证低 32 位；要匹配“结果被符号扩展到 32 位”的 DAG，必须用 `sext_inreg` + `assertsexti32` 约束操作数。

如果你在 LA32 上发现某个 pattern 不匹配，先检查是不是少了 `sext_inreg` 包装——这是 LA32/LA64 双模式最常见的 pattern 问题。

---

## 14.7 反直觉点

1. **`Select()` 先于 matcher**；手写分支能“截胡” `.td` 的 pattern。
2. **`ldptr.d` 的偏移是左移 2 编码的**，范围 ±32KB，不是 ±2GB。
3. **`ldx.d` 需要先把偏移物化到寄存器**，代价是 2–3 条额外指令。
4. **`ISD::Constant` 不走 pattern**，因为一条常量可能展开成多条指令。
5. **`ComplexPattern` 的 C++ 函数返回 bool**，匹配失败会回退到下一个 pattern；不要在函数里 `report_fatal_error`。
6. **LA32 的 pattern 常带 `sext_inreg`**，LA64 不带；同一份 `.td` 里两套 pattern 并存是正常的。
7. **matcher 表有 5 万多行**，不要试图手读；用 `-debug-only=isel` 和 `grep` 定位。

---

## 14.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| `Select()` 入口 | `LoongArchISelDAGToDAG.cpp:35-52` |
| 四个手写分支 | `:53,92,101,109` |
| `ComplexPattern` 列表 | `LoongArchInstrInfo.td:625-628` |
| `PatGprGpr`/`PatGprImm` | `:1343-1355` |
| `ADDI_D` pattern | `:1417` |
| `LD_D`/`LDPTR_D` | `:1183,1208` |
| 三种寻址实测 | 本章 14.3 |
| matcher 规模 | Ch 04 的 tblgen 实测 |

---

## 14.9 练习

1. 把 `offbig` 的偏移改成 32768，确认 `ldptr.d` 失效并观察回退序列。
2. 用 `-debug-only=isel` 观察 `off12` 的 “Selecting” 行，确认是 `LD_D` 而不是 `LDX_D`。
3. 在 `LoongArchInstrInfo.td` 里找到 `LdPat` 的定义，解释它为什么需要 `i64` 参数。
4. 解释 `ISD::FrameIndex` 为什么不能直接用 `addi.d` 的 pattern 处理。
5. 找一个 `ComplexPattern` 的 C++ 实现，说明它匹配失败时返回什么。

---

## 14.10 下一步

下一章看“pattern 表达不了”的部分：`LowerOperation` 的自定义 lowering、`LoongArchISD::*` 节点、以及 intrinsic 的 lowering 路径。
