# Ch 19 — `MergeBaseOffset` 与 `OptWInstrs`：两个“少一条指令”的机器层 pass

> 本章目标：知道 `MergeBaseOffset` 把 `pcalau12i + addi.d` 折进访存操作数、`OptWInstrs` 把 64 位指令降成 `.w` 形式；能用 `-loongarch-enable-merge-offset=false` 和 `-stats` 观察它们的效果；并理解为什么“删掉一条 `sext.w`”可能很危险。

---

## 19.1 `MergeBaseOffset`：把基址偏移折进访存

### 它解决的问题

`la.pcrel` 展开成两条：

```text
pcalau12i $a0, %pc_hi20(g)
addi.d    $a0, $a0, %pc_lo12(g)
ld.d      $a1, $a0, 0
```

`addi.d` 只是为了把 lo12 加到基址上，紧接着的 `ld.d` 偏移是 0。如果能把 lo12 直接放进 `ld.d` 的偏移字段：

```text
pcalau12i $a0, %pc_hi20(g)
ld.d      $a1, $a0, %pc_lo12(g)
```

就少一条 `addi.d`。这就是 `LoongArchMergeBaseOffsetOpt` 做的事。

### 实测（`-loongarch-enable-merge-offset`）

```llvm
@g1 = dso_local global [8 x i64] zeroinitializer
@g2 = dso_local global [8 x i64] zeroinitializer
define i64 @two2() { %a = load i64, ptr @g1
  %b = load i64, ptr @g2
  %r = add i64 %a, %b
  ret i64 %r }
```

默认（pass 开）：

```text
two2:
	pcalau12i	$a0, %pc_hi20(g1)
	ld.d	$a0, $a0, %pc_lo12(g1)
	pcalau12i	$a1, %pc_hi20(g2)
	ld.d	$a1, $a1, %pc_lo12(g2)
	add.d	$a0, $a0, $a1
	ret
```

关闭（`-loongarch-enable-merge-offset=false`）：

```text
two2:
	pcalau12i	$a0, %pc_hi20(g1)
	addi.d	$a0, $a0, %pc_lo12(g1)
	ld.d	$a0, $a0, 0
	pcalau12i	$a1, %pc_hi20(g2)
	addi.d	$a1, $a1, %pc_lo12(g2)
	ld.d	$a1, $a1, 0
	add.d	$a0, $a0, $a1
	ret
```

**两条 `addi.d` 消失**，`%pc_lo12(g1)`/`%pc_lo12(g2)` 移到了 `ld.d` 的偏移位置。链接器仍然能处理这些重定位，所以语义不变。

### 源码结构

`LoongArchMergeBaseOffset.cpp`（878 行）的核心：

```cpp
class LoongArchMergeBaseOffsetOpt : public MachineFunctionPass {
  bool detectFoldable(MachineInstr &Hi20, MachineInstr *&Add, MachineInstr *&Lo12);
  bool detectAndFoldOffset(MachineInstr &Hi20, MachineInstr &Lo12, ...);
  bool foldIntoMemoryOps(MachineInstr &Hi20, MachineInstr &Lo12, ...);
  ...
};
```

它需要：

- `MachineRegisterInfo`（追虚拟寄存器的 def-use）；
- 识别 `LU12I_W`（hi20）+ `ADDI_D`（lo12）对；
- 判断消费者是不是访存指令、偏移是否在立即数范围内；
- 改写 `MachineOperand`，把 lo12 变成 `MO_PCREL_LO12` 之类的目标标志。

### 与 relax 的冲突

`LoongArchMergeBaseOffset.cpp:735`：

```cpp
// If optimized by this pass successfully, MO_RELAX bitmask target-flag should
// ...
```

如果原序列带 `MO_RELAX`（可被链接器收缩），折叠后必须**清掉**这个标志，否则链接器会按旧序列去删指令，产生错位。这是这类“改写指令序列”的 pass 最容易漏的地方。

---

## 19.2 `OptWInstrs`：用 `.w` 指令替换 `.d`

### 它解决的问题

LA64 的寄存器是 64 位，但很多计算只需要 32 位。如果高位已知是符号扩展的，就可以用 `add.w` 代替 `add.d`：

- `add.w` 更短？不，编码一样长；但**语义更窄**，能让后续的 `sext.w` 变成多余，从而删掉；
- `add.w` 的结果天然是 32 位符号扩展，省掉显式的 `slli.d 32; srai.d 32`。

### 实测

```llvm
define i64 @h(i32 %a) { %e = sext i32 %a to i64
  ret i64 %e }
define i64 @k(i64 %a, i64 %b) {
  %ta = trunc i64 %a to i32
  %tb = trunc i64 %b to i32
  %s = add i32 %ta, %tb
  %e = sext i32 %s to i64
  ret i64 %e
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
h:
	addi.w	$a0, $a0, 0
	ret
k:
	add.w	$a0, $a0, $a1
	ret
```

`h` 的 `addi.w $a0,$a0,0` 就是“把 `$a0` 规范成 32 位符号扩展形式”，比 `slli.d 32; srai.d 32` 少一条。

### 用 `-stats` 看它做了多少

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2 -stats w2.ll -o /dev/null
 1 loongarch-opt-w-instrs - Number of removed sign-extensions
 1 loongarch-opt-w-instrs - Number of instructions transformed to W-ops
```

两个 `STATISTIC` 就是 pass 的“工作量计数器”。跑一个真实程序，这两个数字能告诉你优化空间还有多少。

### 它只在 LA64 上跑

```cpp
void LoongArchPassConfig::addMachineSSAOptimization() {
  TargetPassConfig::addMachineSSAOptimization();
  if (TM->getTargetTriple().isLoongArch64())
    addPass(createLoongArchOptWInstrsPass());
}
```

LA32 的寄存器本来就是 32 位语义，没有“用 W 指令”这回事。

### 两个隐藏开关

```cpp
DisableSExtWRemoval("loongarch-disable-sextw-removal", ...);
DisableCvtToDSuffix("loongarch-disable-cvt-to-d-suffix", ...);
```

调试时可以用它们隔离问题：

- `-loongarch-disable-sextw-removal`：保留 `sext.w`，看是不是删除逻辑导致的错误；
- `-loongarch-disable-cvt-to-d-suffix`：禁止把 `.w` 转成 `.d` 后缀。

---

## 19.3 两个 pass 的先后

```text
pre-RA expand pseudo → MergeBaseOffset → ... → MachineSSAOptimization(含 OptWInstrs) → RA → ...
```

- **`MergeBaseOffset` 在 pre-RA 之后、RA 之前**：它要追虚拟寄存器的 def-use，RA 之后就变成物理寄存器，模式匹配会复杂得多。
- **`OptWInstrs` 在 MachineSSAOptimization 阶段**：也是 pre-RA（MachineSSA 是 SSA 形式）。

两者都要求 `MachineFunctionProperties` 里声明“保留 SSA”等属性，否则 pass manager 会拒绝运行。

---

## 19.4 反直觉点

1. **`MergeBaseOffset` 不是“合并两个 global 的基址”**，而是把 `addi.d` 的 lo12 折进访存偏移。
2. **折叠后必须清 `MO_RELAX`**，否则链接器会按旧序列删指令。
3. **`OptWInstrs` 不减少指令长度**（`.w`/`.d` 都是 4 字节），它的收益是“让后续 `sext.w` 变成多余”。
4. **`addi.w x, x, 0` 不是 nop**，它是“符号扩展到 32 位”的规范化。
5. **`-stats` 需要 assertions 构建**；release 构建看不到这些计数。
6. **`llc` 不接受 `-mllvm`**；后端选项直接写，如 `-loongarch-enable-merge-offset=false`。
7. **pass 只在 `-O1+` 启用**（`getOptLevel() != None`），`-O0` 下两个都不跑。

---

## 19.5 本章证据

| 结论 | 源码/命令 |
|---|---|
| pass 结构 | `LoongArchMergeBaseOffset.cpp:30-60` |
| `MO_RELAX` 处理 | `:735-750` |
| 开关 | `LoongArchTargetMachine.cpp:63-65` |
| `OptWInstrs` 统计 | `LoongArchOptWInstrs.cpp:45-56` |
| LA64 限定 | `LoongArchTargetMachine.cpp:216-221` |
| merge 实测 | 本章 19.1 |
| W 指令实测 | 本章 19.2 |
| `-stats` 实测 | 本章 19.2 |

---

## 19.6 练习

1. 构造一个 `ld.d` 偏移超出 12 位的例子，观察 `MergeBaseOffset` 是否仍然折叠（它应该放弃）。
2. 用 `-loongarch-disable-sextw-removal` 对比 `h`/`k` 的输出。
3. 找一个 `OptWInstrs` 反而**不能**转 `.w` 的例子（高位未知），解释原因。
4. 读 `foldIntoMemoryOps`，列出它支持的访存指令类型。
5. 用 `-stats` 跑 `llvm/test/CodeGen/LoongArch` 里的某个大测试，记录两个计数。

---

## 19.7 下一步

下一章看 `MachineCombiner` 与 `LateBranchOpt`：前者在 LoongArch 上目前没有 pattern（空转），后者把“条件静态可知”的条件分支折成无条件跳转。
