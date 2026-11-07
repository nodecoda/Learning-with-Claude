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

## C.8 下一步

Part 4 进入机器层：伪指令展开（Ch 18）、`MergeBaseOffset` 与 `OptWInstrs`（Ch 19）、`MachineCombiner` 与 `LateBranchOpt`（Ch 20）、原子与内存屏障（Ch 21）、死定义与寄存器信息（Ch 22）、调度模型与 `llvm-mca`（Ch 23）。
