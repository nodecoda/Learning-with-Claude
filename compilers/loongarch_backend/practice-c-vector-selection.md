# 练习 C — 向量选指与测试固化：`+lsx` vs `+lasx`

> 目标：用同一段 IR 观察 128 位/256 位的分界，找到“谁把 `<8 x i32>` 拆成两条”的源码位置，并用 `update_llc_test_checks.py` 把行为固化成测试。做完你应该能独立判断一个向量代码生成问题是“类型合法化拆的”还是“选指选的”。

---

## C.1 同一段 IR，两种结果

```llvm
define <8 x i32> @xv(<8 x i32> %a) {
  %r = add <8 x i32> %a, %a
  ret <8 x i32> %r
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx -O2
xv:
	xvadd.w	$xr0, $xr0, $xr0
	ret

$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx -O2
xv:
	vadd.w	$vr0, $vr0, $vr0
	vadd.w	$vr1, $vr1, $vr1
	ret
```

**同一个函数，`+lsx` 下变成两条 128 位指令。** 这不是选指“没找到 `xvadd.w`”，而是 **`<8 x i32>` 在只有 LSX 时不是合法类型**，被类型合法化拆成了两个 `<4 x i32>`。

判断方法：

```bash
llc ... -mattr=+lsx -O2 -print-before=legalize-types -print-after=legalize-types ...
```

如果拆分发生在 `legalize-types` 之前，那就是类型合法化；如果拆分在 `isel` 之后，那才是选指/展开的问题。

---

## C.2 找到“谁决定 128/256”

源码里有两个层次：

1. **类型合法化**：`addRegisterClass(MVT::v8i32, &LASX256RegClass)` 只在 `hasExtLASX()` 时执行（Ch 13）。没有注册 → 类型不合法 → 拆。
2. **lowering**：`lowerVECTOR_SHUFFLE` 等函数里按 `VT.getSizeInBits()` 或 `Subtarget.hasExtLASX()` 选 `v*`/`xv*`。

任务：打开 `LoongArchISelLowering.cpp`，找出 `lowerVECTOR_SHUFFLE` 里第一处 `hasExtLASX` 或 `256` 的判断，记录行号与它处理的 mask 模式。

---

## C.3 用 update 脚本固化测试

新建 `llvm/test/CodeGen/LoongArch/lsx-vs-lasx-add.ll`：

```llvm
; RUN: llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx < %s \
; RUN:   | FileCheck %s --check-prefix=LSX
; RUN: llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx < %s \
; RUN:   | FileCheck %s --check-prefix=LASX

define <8 x i32> @xv(<8 x i32> %a) {
  %r = add <8 x i32> %a, %a
  ret <8 x i32> %r
}
```

然后：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
python3 llvm/utils/update_llc_test_checks.py \
  --llc-binary /home/ubuntu/llvm/builds/ct-instr-trunk/bin/llc \
  llvm/test/CodeGen/LoongArch/lsx-vs-lasx-add.ll
```

脚本会自动生成 `LSX-LABEL`/`LASX-LABEL` 和指令 `CHECK` 行。

**注意**：

- 默认 `update_llc_test_checks.py` 只生成一个 prefix；双 prefix 需要手动拆分 RUN 行或跑两次。
- 生成的 `CHECK` 依赖本树的指令选择结果；如果上游改了优化，测试会失败——这正是它的价值。
- 不要用 `CHECK-NEXT` 锁死所有指令；只锁关键指令（`vadd.w`/`xvadd.w`），否则测试很脆。

---

## C.4 一个“看似选指、实则合法化”的陷阱

如果你看到：

```text
vadd.w	$vr0, $vr0, $vr0
vadd.w	$vr1, $vr1, $vr1
```

第一反应可能是“`xvadd.w` 的 pattern 没匹配上”。但 `+lsx` 下根本没有 `xvadd.w` 这条指令（LASX feature 没开），pattern 不可能匹配。

**排查顺序**：

1. `llc -mattr=+lasx` 是否生成 `xvadd.w`？是 → 说明 lowering/pattern 都没问题，问题在 feature 或类型注册。
2. `-mattr=+lasx` 仍然拆 → 再看 `legalize-types`，确认 `v8i32` 是否被注册。
3. 都没问题 → 才是 pattern/combine 的锅。

---

## C.5 任务清单

1. 记录 `+lsx` 与 `+lasx` 下 `<8 x i32> add` 的指令数差异（2 vs 1）。
2. 找出 `addRegisterClass` 里注册 `v8i32` 的那一行（Ch 13 有）。
3. 找出 `lowerVECTOR_SHUFFLE` 里第一处 256 位分支。
4. 写一个双 prefix 测试并跑通：

   ```bash
   llvm-lit -sv llvm/test/CodeGen/LoongArch/lsx-vs-lasx-add.ll
   ```

5. 把测试里的 `add` 改成 `sub`/`mul`，确认 `xvsub.w`/`xvmul.w` 也符合预期。

---

## C.6 常见失败模式

| 现象 | 原因 |
|---|---|
| `+lasx` 仍生成两条 `vadd.w` | `-mattr=+lasx` 被后面的 `-mattr=-lasx` 覆盖，或函数有 `target-features` 属性 |
| 测试在别的机器上失败 | 用了 `CHECK-NEXT` 锁死寄存器分配/调度顺序 |
| `update_llc_test_checks.py` 不生成 prefix | 脚本默认 prefix 是 `CHECK`，需要在 RUN 行写 `--check-prefix` 后手动调整 |
| `xvadd.w` 报 “instruction requires LASX” | 只开了 `+lsx` |

---

## C.7 证据表

| 结论 | 命令/源码 |
|---|---|
| `<8 x i32>` 在 `+lsx` 下拆两条 | C.1 实测 |
| `+lasx` 一条 | C.1 实测 |
| 类型注册条件 | `LoongArchISelLowering.cpp:95-103` |
| update 脚本 | `llvm/utils/update_llc_test_checks.py` |
| 双 prefix 写法 | C.3 |

---

## C.8 参考解答

> 复核环境：2026-11-08，`llc` 来自 `/home/ubuntu/llvm/builds/ct-instr-trunk/bin`，源码行号来自本树 `ct-instr-trunk`。

<details>
<summary>C.1：`<8 x i32> add` 在 `+lsx` / `+lasx` 下差在哪？</summary>

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx -O2 x.ll -o -
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx  -O2 x.ll -o -
```

实测：

```text
# +lasx
xv:
	xvadd.w	$xr0, $xr0, $xr0
	ret

# +lsx
xv:
	vadd.w	$vr0, $vr0, $vr0
	vadd.w	$vr1, $vr1, $vr1
	ret
```

`+lsx` 下不是“`xvadd.w` 没匹配上”，而是 `<8 x i32>` 这个类型根本没有被注册成合法类型，被类型合法化拆成两个 `<4 x i32>`，然后各自选 `vadd.w`。判断位置：`-print-before=legalize-types` / `-print-after=legalize-types`，看拆分发生在 isel 之前还是之后。

</details>

<details>
<summary>C.2：谁决定 128 位还是 256 位？</summary>

`LoongArchISelLowering.cpp` 里注册寄存器类的代码（实测行号）：

```text
 95:      MVT::v32i8, MVT::v16i16, MVT::v8i32, MVT::v4i64, MVT::v8f32, MVT::v4f64};
101:  if (Subtarget.hasExtLASX())
103:      addRegisterClass(VT, &LoongArch::LASX256RegClass);
```

完整逻辑是：

```cpp
static const MVT::SimpleValueType LASXVTs[] = {
    MVT::v32i8, MVT::v16i16, MVT::v8i32, MVT::v4i64, MVT::v8f32, MVT::v4f64};
if (Subtarget.hasExtLASX())
  for (MVT VT : LASXVTs)
    addRegisterClass(VT, &LoongArch::LASX256RegClass);
```

所以第一层是 **feature 决定类型合法性**：没有 LASX 就没有 `v8i32` 的寄存器类，类型必须被拆。第二层才是 lowering 里按 `VT.is256BitVector()` / `hasExtLASX()` 选具体指令。

</details>

<details>
<summary>C.3：`lowerVECTOR_SHUFFLE` 里第一处 256 位分支在哪？</summary>

实测源码位置：

```text
3596: SDValue LoongArchTargetLowering::lowerVECTOR_SHUFFLE(SDValue Op, ...
3640:   if (VT.is256BitVector())
3641:     return lower256BitShuffle(DL, OrigMask, VT, V1, V2, DAG, Subtarget);
3492: static SDValue lower256BitShuffle(const SDLoc &DL, ArrayRef<int> Mask, ...
```

`lowerVECTOR_SHUFFLE` 先处理 undef mask、非法 mask，然后按 128/256 位分派；256 位走 `lower256BitShuffle`。`lower256BitShuffle` 内部再按 mask 模式选 `xvshuf.*`、`xvpermi.*` 等。

理解这一点后，排查顺序是：feature/类型注册 → `lower256BitShuffle` 的具体模式 → 最后才是 DAG pattern。

</details>

<details>
<summary>C.4：双 prefix 测试怎么写？`update_llc_test_checks.py` 能直接生成吗？</summary>

RUN 行写两个 prefix：

```asm
; RUN: llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx  < %s | FileCheck %s --check-prefix=LSX
; RUN: llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx < %s | FileCheck %s --check-prefix=LASX

define <8 x i32> @xv(<8 x i32> %a) {
  %r = add <8 x i32> %a, %a
  ret <8 x i32> %r
}

; LSX-LABEL: xv:
; LSX: vadd.w $vr0, $vr0, $vr0
; LSX: vadd.w $vr1, $vr1, $vr1
; LASX-LABEL: xv:
; LASX: xvadd.w $xr0, $xr0, $xr0
```

`update_llc_test_checks.py` 默认只生成一个 `CHECK` prefix；双 prefix 要么手动写，要么跑两次再合并。验证时不必进 lit 测试套件，直接跑 RUN 行即可：

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx  < t.ll | $T/bin/FileCheck t.ll --check-prefix=LSX
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx < t.ll | $T/bin/FileCheck t.ll --check-prefix=LASX
```

实测两条都返回 0（打印 `LSX-OK` / `LASX-OK`）。注意：`llvm-lit` 只发现带测试套件配置的路径，单独把 `.ll` 放在 `/tmp` 下跑 `llvm-lit` 会报 “unable to find test suite”。

</details>

<details>
<summary>C.5：把 `add` 换成 `sub` / `mul` 会怎样？</summary>

`mul`：

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx -O2 s.ll -o - | grep xv
```

实测：

```text
	xvmul.w	$xr0, $xr0, $xr0
```

`sub` 则**看不到 `xvsub.w`**，因为 `sub %a, %a` 被 DAG combine 折成零向量。实测 `xs` 的函数体是：

```text
	xvrepli.b	$xr0, 0
	ret
```

这是练习里最容易误判的一条：不是选指失败，而是运算本身被优化掉了。想观察 `xvsub.w`，要换成两个不同操作数：

```llvm
define <8 x i32> @xs(<8 x i32> %a, <8 x i32> %b) {
  %r = sub <8 x i32> %a, %b
  ret <8 x i32> %r
}
```

这时实测输出：

```text
	xvsub.w	$xr0, $xr0, $xr1
	ret
```

结论：验证某条指令“有没有被选上”时，先确认测试 IR 没有被常量折叠/恒等式化简掉。

</details>

---

## C.9 下一步

Part 4 进入机器层：伪指令展开（Ch 18）、`MergeBaseOffset` 与 `OptWInstrs`（Ch 19）、`MachineCombiner` 与 `LateBranchOpt`（Ch 20）、原子与内存屏障（Ch 21）、死定义与寄存器信息（Ch 22）、调度模型与 `llvm-mca`（Ch 23）。
