# Ch 21 — 原子与内存屏障：AMO、LL/SC 与 `dbar`

> 本章目标：知道 `atomicrmw` 走 AMO、`cmpxchg` 走 LL/SC、release store 居然是一条 `amswap`；理解 `MemoryBarrierOpt` 的七条合并规则；并知道为什么原子伪指令要拖到最后一个 pass 展开。

---

## 21.1 实测：四种原子操作，四种实现

```llvm
define i32 @add_atomic(ptr %p) {
  %old = atomicrmw add ptr %p, i32 1 seq_cst
  ret i32 %old
}
define i32 @load_acq(ptr %p) {
  %v = load atomic i32, ptr %p acquire, align 4
  ret i32 %v
}
define void @store_rel(ptr %p, i32 %v) {
  store atomic i32 %v, ptr %p release, align 4
}
define i1 @cmpxchg32(ptr %p, i32 %c, i32 %n) {
  %r = cmpxchg ptr %p, i32 %c, i32 %n seq_cst seq_cst
  %ok = extractvalue { i32, i1 } %r, 1
  ret i1 %ok
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
```

### `atomicrmw add` → AMO

```text
	ori	$a2, $zero, 1
	amadd_db.w	$a1, $a2, $a0
	move	$a0, $a1
	ret
```

LoongArch 有 AMO（atomic memory operation）指令，`amadd.w` 直接完成“读-加-写”，不需要 LL/SC 循环。`_db` 后缀表示“自带 dbar 语义”（由 `MemoryBarrierOpt` 合并而来）。

### acquire load → `ld.w` + `dbar`

```text
	ld.w	$a0, $a0, 0
	dbar	20
	ret
```

普通 `ld.w` 没有 acquire 语义，所以后面补一条 `dbar`。`dbar` 的立即数是**屏障掩码**，不是“第 20 号屏障”。

### release store → `amswap_db.w`

```text
	amswap_db.w	$zero, $a1, $a0
	ret
```

**release store 不是 `st.w`**。LoongArch 的普通 store 没有 release 语义，编译器用“原子交换 0”实现：`amswap_db.w $zero, val, addr` 把 `val` 写进去、把旧值丢到 `$zero`，同时带 `db` 屏障。

这是一个非常反直觉的点：**看起来是 store，生成的是一条 AMO**。

### `cmpxchg` → LL/SC 循环

```text
	addi.w	$a1, $a1, 0
.LLBB3_1:
	ll.w	$a3, $a0, 0
	bne	$a3, $a1, .LBB3_3
	move	$a4, $a2
	sc.w	$a4, $a0, 0
	beq	$a4, $zero, .LBB3_1
.LBB3_3:
	dbar	20
	ret
```

LoongArch **没有原生 CAS**，所以 `cmpxchg` 必须用 `ll.w`/`sc.w` 循环：

1. `ll.w` 读出当前值；
2. 与期望值比较，不等就退出；
3. 相等则 `sc.w` 尝试写回，失败（`$a4 == 0`）就重试。

`sc.w` 的“成功/失败”通过目标寄存器返回 0/1，这就是为什么需要 `beq ... .LBB3_1` 重试。

---

## 21.2 `MemoryBarrierOpt` 的七条规则

文件头注释（`LoongArchMemoryBarrierOpt.cpp:12-18`）：

```text
- DBAR x + DBAR y -> DBAR (x & y)
- DBAR x + AMO_DB -> AMO_DB
- DBAR x + AMO    -> AMO_DB
- DBAR x + LL     -> LL
- AMO_DB + DBAR x -> AMO_DB
- AMO    + DBAR x -> AMO_DB
- SC     + DBAR x -> SC
```

含义：

| 规则 | 解释 |
|---|---|
| 两个 `dbar` 合并 | 掩码按位与（更弱的屏障被吸收） |
| `dbar` + `AMO` | 合并成 `AMO_DB`（AMO 自带屏障） |
| `dbar` + `LL` | `LL` 本身有 acquire 语义，`dbar` 可删 |
| `SC` + `dbar` | `SC` 本身有 release 语义，`dbar` 可删 |

这正是 21.1 里 `amadd_db.w`/`amswap_db.w` 的来源：源码先发出 `amadd.w` + `dbar`，这个 pass 再合并。

**两个开关**：

```cpp
RequireNoPathBypass("loongarch-require-no-path-bypass", cl::init(true));
MergeAMOWithMB("loongarch-merge-amo-with-dbar", ...);
```

- `RequireNoPathBypass`：只有“没有路径绕过屏障”时才合并（保守，默认开）。
- `MergeAMOWithMB`：控制 AMO 与 `dbar` 的合并。

调试时可以关掉它们，看原始序列。

---

## 21.3 `ExpandAtomicPseudoInsts`：为什么拖到最后

`LoongArchTargetMachine.cpp:209`：

```cpp
// Schedule the expansion of AtomicPseudos at the last possible moment,
// avoiding the possibility for other passes to break the requirements for
// forward progress in the LL/SC block.
addPass(createLoongArchExpandAtomicPseudoPass());
```

LL/SC 循环有一个硬要求：**`ll` 和 `sc` 之间不能有让循环无法前进的指令**（比如另一个 `sc`、或者改变地址的指令）。如果展开得太早，后续 pass（调度、RA、分支优化）可能在循环里插入指令，破坏这个性质。

`LoongArchExpandAtomicPseudoInsts.cpp`（755 行）里的 pseudo 覆盖：

| 类别 | 例子 |
|---|---|
| 交换 | `PseudoAtomicSwap32`、`PseudoMaskedAtomicSwap32` |
| 读-改-写 | `PseudoAtomicLoadAdd32/Sub32/And32/Or32/Xor32` |
| 最值 | `PseudoAtomicLoadUMax32/UMin32/Max32/Min32` |
| 掩码最值 | `PseudoMaskedAtomicLoadUMax32/UMin32` |
| 比较交换 | `PseudoCmpXchg32/64`、`expandAtomicCmpXchg128` |

**注意**：`amadd` 这类 AMO 路径不走这些 pseudo；pseudo 是给“没有 AMO 的原子操作”（如 CAS、掩码操作）用的。

---

## 21.4 `dbar` 的立即数

`dbar 20` 里的 `20` 是**屏障掩码**（哪些 load/store 组合需要排序），不是“屏障等级”。不同内存序会生成不同的掩码：

- `seq_cst`：最强；
- `acquire`/`release`：更弱；
- `acq_rel`：介于两者。

**不要**在测试里硬编码 `dbar 20` 而不解释；写 `CHECK: dbar` 更稳，或者用 `--check-prefix` 分开不同内存序。

---

## 21.5 反直觉点

1. **`atomicrmw` 走 AMO，`cmpxchg` 走 LL/SC**——因为 LoongArch 有 AMO 但没有 CAS。
2. **release store 是一条 `amswap`**，不是 `st`。
3. **`_db` 后缀是 `MemoryBarrierOpt` 合并出来的**，不是源码直接写的。
4. **`dbar` 立即数是掩码**，不是等级。
5. **原子伪指令最后展开**，否则 LL/SC 会被其它 pass 破坏。
6. **`sc.w` 失败返回 0**，成功返回 1；重试条件是“返回 0”。
7. **`ll.w` 的 acquire 语义**让前面的 `dbar` 可以被删（规则 4）。

---

## 21.6 本章证据

| 结论 | 源码/命令 |
|---|---|
| 四种原子实测 | 本章 21.1 |
| 屏障合并规则 | `LoongArchMemoryBarrierOpt.cpp:12-18` |
| 两个开关 | `:34,39` |
| 原子 pass 时机 | `LoongArchTargetMachine.cpp:207-212` |
| pseudo 清单 | `LoongArchExpandAtomicPseudoInsts.cpp:92-145` |
| LL/SC 展开 | `:499`（cmpxchg）、`:639`（128 位） |

---

## 21.7 练习

1. 把 `atomicrmw add` 改成 `fetch_sub`、`fetch_and`，观察 AMO 指令变化。
2. 把 `cmpxchg` 的 `seq_cst` 改成 `monotonic`，看 `dbar` 是否消失。
3. 用 `-loongarch-merge-amo-with-dbar=false` 关闭合并，对比 `amadd.w` + `dbar` 的原始序列。
4. 解释 `PseudoCmpXchg128` 为什么需要特殊的展开（提示：LL/SC 没有 128 位版本）。
5. 找一个“掩码原子操作”（如 `atomicrmw umax` 配合掩码），看它展开成几条指令。

---

## 21.8 下一步

下一章看 `DeadRegisterDefinitions` 与寄存器信息：哪些定义是“死”的、`getReservedRegs` 保留了什么、以及 `CalleeSavedRegs` 如何与 ABI 对齐。
