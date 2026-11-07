# Ch 10 — 栈帧 lowering：`$sp`、`$fp` 与那条 `-2032`

> 本章目标：解释 prologue 里每一行 `addi.d $sp, $sp, ...` 的来历；知道 `$fp` 什么时候才出现；理解为什么 4KB 的栈帧会被拆成 `-2032 / -2048 / -32` 三段；并能判断一次改动会不会破坏 CFI。

---

## 10.1 什么时候有 prologue，什么时候没有

实测：

```llvm
define i64 @leaf(i64 %x) { ret i64 %x }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O0
leaf:
	ret
```

叶函数、无栈对象、无 callee-saved → **一行 prologue 都没有**。帧布局不是“每个函数都有”，而是“需要才分配”。

判定顺序（`emitPrologue`，`LoongArchFrameLowering.cpp:298`）：

```cpp
determineFrameLayout(MF);
uint64_t StackSize = MFI.getStackSize();
if (StackSize == 0 && !MFI.adjustsStack())
  return;
```

`adjustsStack()` 为真通常意味着有调用（需要 `$ra` 溢出）或动态栈调整。

---

## 10.2 `$fp` 的四个触发条件

`hasFPImpl()`（`:36`）：

```cpp
return MF.disableFramePointerElim() || RegInfo->hasStackRealignment(MF) ||
       MFI.hasVarSizedObjects() || MFI.isFrameAddressTaken();
```

| 条件 | 典型来源 |
|---|---|
| `disableFramePointerElim()` | `-fno-omit-frame-pointer`（函数属性 `"frame-pointer"="all"`） |
| `hasStackRealignment()` | 栈上有超过默认对齐的对象（如 `align 64` 的 `alloca`） |
| `hasVarSizedObjects()` | `alloca i8, i64 %n`、VLA |
| `isFrameAddressTaken()` | `llvm.frameaddress`、`__builtin_frame_address` |

实测动态 `alloca`：

```llvm
define void @dyn(i64 %n) {
  %a = alloca i8, i64 %n
  call void @use(ptr %a)
  ret void
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O0
dyn:
	addi.d	$sp, $sp, -16
	.cfi_def_cfa_offset 16
	st.d	$ra, $sp, 8
	st.d	$fp, $sp, 0
	.cfi_offset 1, -8
	.cfi_offset 22, -16
	addi.d	$fp, $sp, 16
	.cfi_def_cfa 22, 0
	addi.d	$a1, $a0, 15
	bstrins.d	$a1, $zero, 3, 0
	move	$a0, $sp
	sub.d	$a0, $a0, $a1
	move	$sp, $a0
	pcaddu18i	$ra, %call36(use)
	jirl	$ra, $ra, 0
	addi.d	$sp, $fp, -16
	ld.d	$fp, $sp, 0
	ld.d	$ra, $sp, 8
	addi.d	$sp, $sp, 16
	ret
```

逐行读：

- `addi.d $sp,$sp,-16`：固定帧，存 `$ra`/`$fp`。
- `st.d $fp, $sp, 0`、`addi.d $fp, $sp, 16`：**先保存旧 `$fp`，再建立新 `$fp`**；`$fp` 指向固定帧顶（`$sp+16`），这样动态分配后仍能用 `$fp` 找回固定区。
- `addi.d $a1, $a0, 15; bstrins.d $a1, $zero, 3, 0`：`%n` 向上取整到 16 字节——`(n+15) & ~15`。`bstrins.d` 清零低 4 位，比 `andi` 更省（无需构造掩码）。
- `sub.d $a0, $sp, $a1; move $sp, $a0`：真正的动态分配。
- epilogue 用 `addi.d $sp, $fp, -16` 恢复，而不是“加回 `$n`”——这就是 `$fp` 存在的意义。
- `.cfi_def_cfa 22, 0`：CFA 改为 `$fp`（DWARF 寄存器号 22），偏移 0。**改 prologue 时必须同步改 CFI，否则 backtrace 会错**。

---

## 10.3 那条 `-2032`：`FirstSPAdjustAmount`

实测 4KB 栈帧：

```llvm
define void @big() {
  %a = alloca [4096 x i8]
  call void @use(ptr %a)
  ret void
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O0
big:
	addi.d	$sp, $sp, -2032
	.cfi_def_cfa_offset 2032
	st.d	$ra, $sp, 2024
	.cfi_offset 1, -8
	addi.d	$sp, $sp, -2048
	addi.d	$sp, $sp, -32
	.cfi_def_cfa_offset 4112
```

`-2032` 不是随手取的数，`getFirstSPAdjustAmount()`（`:552`）写得很明确：

```cpp
// FirstSPAdjustAmount is chosen as (2048 - StackAlign) because 2048 will
// cause sp = sp + 2048 in the epilogue to be split into multiple
// instructions. Offsets smaller than 2048 can fit in a single load/store
// instruction, and we have to stick with the stack alignment.
return 2048 - getStackAlign().value();   // 2048 - 16 = 2032
```

触发条件：

```cpp
if (!isInt<12>(MFI.getStackSize()) && (CSI.size() > 0))
```

即“栈帧超过 12 位有符号立即数（−2048..2047）**且**有 callee-saved 要溢出”。栈对齐是 16，所以取 `2048-16 = 2032`。

为什么要**先**调 2032、保存 `$ra`，**再**调剩下的 2080：

- `st.d $ra, $sp, 2024` 的偏移必须是 12 位可编码的（3072 不行）；
- 先调 2032 后，`$ra` 落在 `sp+2024`，一条 `st.d` 就能存；
- 剩下的 2080 再分两次 `addi`（`-2048` + `-32`）。

如果一次性调 `-4112`，`adjustReg` 会因为超出 12 位而生成 `lu12i.w + sub.d`，并且 callee-saved 的偏移也会超出 12 位，需要额外的临时寄存器——更慢、更难调度。

`adjustReg` 的三条路径（`:51`）：

| 条件 | 生成 |
|---|---|
| `isInt<12>(Val)` | 一条 `addi.d` |
| `-4096 < Val <= 2*(2048-StackAlign)` | 两条 `addi.d`（每步都保持对齐） |
| 其它 | `lu12i.w + ori` 构造常数，再 `sub.d`/`add.d` |

实测 20016 字节帧：

```text
	addi.d	$sp, $sp, -2032
	st.d	$ra, $sp, 2024
	lu12i.w	$a0, 4
	ori	$a0, $a0, 1600        # 4*4096+1600 = 17984
	sub.d	$sp, $sp, $a0         # 17984 + 2032 = 20016
```

这里 `adjustReg` 走到第三条路径，用 `$a0` 当临时寄存器——所以**大栈帧会多吃一个 GPR**，这也是寄存器压力分析里容易漏的一点。

---

## 10.4 栈探测（stack probe）

先纠正一个常见误解：**默认并不探测**。

```cpp
bool LoongArchTargetLowering::hasInlineStackProbe(const MachineFunction &MF) const {
  // If the function specifically requests inline stack probes, emit them.
  if (MF.getFunction().hasFnAttribute("probe-stack"))
    return MF.getFunction().getFnAttribute("probe-stack").getValueAsString() ==
           "inline-asm";
  return false;
}
```

必须有 `"probe-stack"="inline-asm"` 属性才会探测。探测粒度由 `stack-probe-size` 决定，默认 4096（`LoongArchISelLowering.cpp:12178`）：

```cpp
unsigned StackProbeSize = Fn.getFnAttributeAsParsedInteger("stack-probe-size", 4096);
```

`allocateStack()`（`LoongArchFrameLowering.cpp:188`）分三种情况：

1. **不需要探测，或 `Offset <= ProbeSize`**：直接 `adjustReg`；只有动态分配时才补一条 `st.d $zero, $sp, 0`。
2. **`Offset < ProbeSize * 5`**：展开循环——每次 `-ProbeSize` 后 `st.d $zero, $sp, 0`。
3. **更大**：生成 `PROBED_STACKALLOC` 伪指令，由 `inlineStackProbe()`（`:523`）展开成循环，用 `$r13` 当目标寄存器。

实测（强制小粒度，65 字节帧 → 走第 3 种，生成循环）：

```llvm
define void @probe() "probe-stack"="inline-asm" "stack-probe-size"="16" {
  %a = alloca [64 x i8]
  call void @use(ptr %a)
  ret void
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O0
probe:
	addi.d	$t1, $sp, -80
	.cfi_def_cfa 13, 80
	ori	$t2, $zero, 16
.LBB0_1:
	sub.d	$sp, $sp, $t2
	st.d	$zero, $sp, 0
	bne	$sp, $t1, .LBB0_1
# %bb.2:
	.cfi_def_cfa_register 3
	.cfi_def_cfa_offset 80
	st.d	$ra, $sp, 72
	...
```

三个细节：

- `$t1`（`$r13`）保存目标栈顶，`$t2` 保存步长，循环用 `bne` 判断——这是 `PROBED_STACKALLOC` 的标准展开。
- `.cfi_def_cfa 13, 80` 在循环期间把 CFA 暂时指向 `$t1`，循环结束后 `.cfi_def_cfa_register 3` 切回 `$sp`。**这是 CFI 随伪指令展开变形的典型例子。**
- 循环里每条 `st.d $zero, $sp, 0` 就是“碰一下页”，触发 guard page。

**LoongArch 用的是 inline probe，不是调用 `__chkstk`**。读代码时别去找 runtime 调用。

趣味点：10.3 里 `big` 的 `Offset`（2032）恰好 `<= ProbeSize`，而且没有 `probe-stack` 属性，所以**没有**探测存储。

## 10.5 callee-saved 与 epilogue

`determineCalleeSaves()`（`:570`）在通用逻辑之上补两条：

```cpp
// Unconditionally spill RA and FP only if the function uses a frame pointer.
if (hasFP(MF)) {
  SavedRegs.set(LoongArch::R1);   // $ra
  SavedRegs.set(LoongArch::R22);  // $fp
}
```

注意措辞：**只有 `hasFP` 时才强制存 `$ra`/`$fp`**。普通函数里 `$ra` 是否溢出由 `TargetFrameLowering` 的通用规则决定（有调用就需要）；`$fp` 不用就不存。

epilogue（`:416`）的顺序与 prologue 相反：

```text
（若有 FirstSPAdjustAmount）先加回 SecondSPAdjustAmount
adjustReg(sp, sp, StackSize)
恢复 callee-saved（$fp 先于 $ra，取决于 spill 顺序）
ret
```

实测 `big` 的 epilogue：

```text
	addi.d	$sp, $sp, 2032
	addi.d	$sp, $sp, 48
	ld.d	$ra, $sp, 2024
	addi.d	$sp, $sp, 2032
	ret
```

`2032 + 48 = 2080` 是第二次调整的逆操作（`-2048-32` 拆成 `+2032+48`，同样是为了每步对齐、且避免 2048 这个会被拆分的值），最后 `+2032`。**prologue 怎么拆，epilogue 就怎么拆回去**——两者必须严格配对，否则 `$sp` 会漂移。

---

## 10.6 远分支的应急槽

`processFunctionBeforeFrameFinalized()`（`:142`）里有一段容易被忽略的逻辑：

```cpp
// Far branches beyond 27-bit offset require a spill slot for scratch register.
bool IsLargeFunction = !isInt<27>(estimateFunctionSizeInBytes(TII, MF));
```

函数体超过 ±64MB（27 位分支范围）时，`b` 可能够不到目标，需要 `pcaddu18i + jirl`，而这需要临时寄存器。帧定型前预留 scavenging slot，寄存器分配器才有地方腾寄存器。

这类“为 relaxation 预留资源”的代码，是 LoongArch 后端里比较有贡献空间的地方：阈值、估计方法、以及和 `-mcmodel=large` 的交互都可能有 bug。

---

## 10.7 shrink-wrapping

```cpp
bool LoongArchFrameLowering::enableShrinkWrapping(const MachineFunction &MF) const {
  if (MF.getFunction().hasOptNone())
    return false;
  return true;
}
```

即：**优化开启时启用**，`-O0` 关闭。效果是 prologue 可能被放到某个基本块里（而不是函数入口），只有真正需要栈的路径才付代价。

验证方法：

```bash
llc -mtriple=loongarch64-unknown-linux-gnu -O2 -print-after=prologepilog fn.ll -o /dev/null
```

看 `$sp` 调整是否落在非 entry 块。如果你改了 `hasFP`/`determineCalleeSaves`，记得同时跑 `-O0` 和 `-O2`：两者的 shrink-wrapping 行为不同，很容易只测一边。

---

## 10.8 反直觉点

1. **叶函数没有 prologue**，`StackSize == 0 && !adjustsStack()` 直接 return。
2. **`-2032` 是 2048 减栈对齐**，不是“半个页”；`2048` 本身在 epilogue 会被拆成多条，所以避开。
3. **大栈帧会占用一个临时 GPR**（`adjustReg` 的 `lu12i.w + sub.d` 路径）。
4. **栈探测是 inline 的**，没有 `__chkstk` 调用；`PROBED_STACKALLOC` 是伪指令。
5. **`$fp` 不是“有栈帧就有”**，只有四个条件之一成立才有。
6. **`$fp` 建立顺序是先存后改**：`st.d $fp` 在 `addi.d $fp, $sp, ...` 之前，源码里还专门 `std::advance(MBBI, CSI.size())` 跳过 callee-saved 存储。
7. **CFI 必须跟着 prologue 改**：`.cfi_def_cfa_offset`、`.cfi_offset`、`.cfi_def_cfa` 三件套错一个，`gdb`/unwinder 就会给你错误的调用栈。
8. **`-O0` 关闭 shrink-wrapping**，所以“我在 -O0 看到 prologue 在入口”不能推广到 `-O2`。

---

## 10.9 本章证据

| 结论 | 源码/命令 |
|---|---|
| prologue 早退 | `LoongArchFrameLowering.cpp:324-326` |
| `hasFP` 四条件 | `:36-42` |
| 动态 alloca 实测 | 本章 10.2 |
| `FirstSPAdjustAmount` | `:552-568` |
| `adjustReg` 三路径 | `:51-100` |
| 20016 帧实测 | 本章 10.3 |
| stack probe 默认 4096 | `LoongArchISelLowering.cpp:12178-12187` |
| probe 三分支 | `LoongArchFrameLowering.cpp:188-295` |
| callee-saved 规则 | `:570-580` |
| epilogue | `:416-460` |
| 远分支应急槽 | `:142-160` |
| shrink-wrapping | `:709-716` |

---

## 10.10 练习

1. 用 `"probe-stack"="inline-asm" "stack-probe-size"="16"` 强制探测，观察循环展开与 CFI 的变化；再改成 `"stack-probe-size"="4096"`，看它退化成单条 `adjustReg`。
2. 写一个 `align 64` 的 `alloca`，确认 `$fp` 出现、并且 prologue 里有 `bstrins.d $sp, $zero, 5, 0` 做重对齐。
3. 用 `-fno-omit-frame-pointer` 等价的函数属性（`"frame-pointer"="all"`）编译叶函数，观察 `$fp` 被强行引入。
4. 比较 `-O0` 与 `-O2` 的 prologue 位置，找出 shrink-wrapping 的证据。
5. 改一处 prologue（比如把 `-2032` 写成 `-2048`），跑 `llvm-lit llvm/test/CodeGen/LoongArch`，看哪些测试会抓到（**改完记得还原**）。

---

## 10.11 下一步

下一章看代码模型与 TLS：`small`/`medium`/`large` 怎么影响 `la.pcrel`/`la.got` 的展开，`R_LARCH_*` 重定位怎么产生，以及 `localexec`/`initialexec`/`globaldynamic` 三种 TLS 模型的指令序列。
