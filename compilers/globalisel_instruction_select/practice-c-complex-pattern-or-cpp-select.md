# 练习 C：ComplexPattern 迁移与 AArch64 寻址固定点

> **目标**：亲手走一遍“SelectionDAG ComplexPattern → GlobalISel ComplexOperandMatcher”的迁移，并用 AArch64 的 `am_indexed32` 把边界、非对齐、多 use 与 feature 缺失四类情况全部测到。
>
> **产出**：一份可复现的测试脚本 + 一张“预测 vs 实际”表 + 一份 `am_indexed32` 迁移说明。
> **时间**：3–5 小时。**环境**：第 0 章的 LLVM 23.1.4 `llc`，AArch64 目标。

---

## C.0 为什么拿 `am_indexed32` 做练习

第 11 章讲过 ComplexPattern 的迁移，但那里偏重“概念上要补什么”。这个练习把它压到一个具体、可验收的点上：**AArch64 的 `LDRWui` 如何把 `base + constant` 折进 12 位 scaled immediate**。

这条路径同时具备四个优点：

1. 它是真实存在的 upstream 代码，不需要编造指令；
2. 它有明确的数值边界（4095 / 4096），可以精确预测；
3. 它有非 scaled 的兜底指令（`LDURWi`），能观察“匹配失败但不是错误”；
4. 它和 feature predicate（`HasRCPC_IMMO`、`HasLSE`）相邻，能顺手验证 feature 缺失时的选路。

练习的核心不是“背一条 pattern”，而是建立下面这条因果链：

```text
.td 里的 ComplexPattern
  → gi_ 版本的 GIComplexOperandMatcher + GIComplexPatternEquiv
  → GlobalISelEmitter 生成 GIM_CheckComplexPattern
  → C++ selectAddrModeIndexed<32> 返回两个 renderer
  → LDRWui 的 base/offset 操作数被填上
```

任何一环缺失，最终表现都不是“报一个清楚的错”，而是“pattern 没导入”或“悄悄选了另一条指令”。这正是本练习要训练的判断力。

---

## C.1 任务规格

### C.1.1 输入

工作目录建议为 `isel-practice-c/`，放入三个 IR 文件。

**`gep.ll`**：

```llvm
define i32 @ld0(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 0
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @ld4095(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 4095
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @ld4096(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 4096
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @ldneg1(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 -1
  %v = load i32, ptr %q, align 4
  ret i32 %v
}
```

**`gep_extra.ll`**（边界补充）：

```llvm
define i32 @ld1(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 1
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @ld1024(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 1024
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @ld4097(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 4097
  %v = load i32, ptr %q, align 4
  ret i32 %v
}
```

**`unal.ll`**：

```llvm
define i32 @ld_off2(ptr %p) {
entry:
  %q = getelementptr i8, ptr %p, i64 2
  %v = load i32, ptr %q, align 1
  ret i32 %v
}

define i32 @ld_two_uses(ptr %p) {
entry:
  %q = getelementptr i32, ptr %p, i64 8
  %a = load i32, ptr %q, align 4
  %b = load i32, ptr %q, align 4
  %r = add i32 %a, %b
  ret i32 %r
}
```

**`acq.ll`**：

```llvm
define i32 @aload_acq(ptr %p) {
entry:
  %v = load atomic i32, ptr %p acquire, align 4
  ret i32 %v
}
```

**`cas.ll`**（feature 对照，沿用第 14 章）：

```llvm
define i32 @cas_i32(ptr %p, i32 %cmp, i32 %new) {
entry:
  %pair = cmpxchg ptr %p, i32 %cmp, i32 %new seq_cst seq_cst
  %old = extractvalue { i32, i1 } %pair, 0
  ret i32 %old
}
```

另外需要 23.1.4 的源码树，至少包含：

- `llvm/lib/Target/AArch64/AArch64InstrFormats.td`
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`
- `llvm/utils/TableGen/GlobalISelEmitter.cpp`
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h`

### C.1.2 输出

1. 一份 `isel-addr.patch`：如果目标树里没有 `gi_am_indexed32`，补上它；如果有，写一段注释说明它为何足够。
2. 一份 `run-tests.ps1`：对五个测试组分别跑 `llc`，把汇编写入 `out/`。
3. 一份 `results.md`：每组记录“预测 → 实际 → 结论”，至少包含本练习 C.5 中的那张表。
4. 一段 5–10 行的说明：`selectAddrModeIndexed<32>` 的哪个分支决定了 4095 与 4096 的差异。

### C.1.3 验收标准

- 每条测试都用 `-global-isel -global-isel-abort=1`，不允许靠 fallback 蒙混；
- 每个函数在 `-stop-after=instruction-select` 里都是 `selected: true`、`failedISel: false`；
- 加 `-verify-machineinstrs` 不报错；
- 4095 与 4096 的选择结果不同，且你能指出源码里的比较条件；
- 非对齐 offset 2 走 `LDURWi`，不是 `LDRWui`，也不是崩溃；
- 多 use 场景下两条 load 各自折叠，不要求 GEP 单 use；
- feature 缺失/存在时，`ldar` 与 `ldapr`（或 `CMP_SWAP_32` 与 `CASALW`）符合预期；
- 不修改与练习无关的文件。

---

## C.2 背景：ComplexPattern 的三段映射

### C.2.1 SelectionDAG 侧

`AArch64InstrFormats.td:3744`：

```tablegen
def am_indexed32 : ComplexPattern<iPTR, 2, "SelectAddrModeIndexed32", []>;
```

`ComplexPattern` 的第二个模板参数 `2` 是**它覆盖两个操作数**（base、offset），不是“匹配两个输入”。第三个参数是 SelectionDAG 侧的 C++ 函数名。`am_indexed32` 被 `LDRWui` 等指令的 pattern 使用。

### C.2.2 GlobalISel 侧

`AArch64InstrFormats.td:3761`：

```tablegen
def gi_am_indexed32 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<32>">,
    GIComplexPatternEquiv<am_indexed32>;
```

三个信息：

- `s64`：指针类型在 gMIR 里是 `s64`；
- `"selectAddrModeIndexed<32>"`：GlobalISel 侧要调用的 C++ 模板实例；
- `GIComplexPatternEquiv<am_indexed32>`：把 SelectionDAG 的 `am_indexed32` 与这条 GI 记录绑定。

### C.2.3 导入器

`GlobalISelEmitter.cpp:1035` 起：

```cpp
  const auto &ComplexPattern = ComplexPatternEquivs.find(R);
  if (ComplexPattern == ComplexPatternEquivs.end())
    return failedImport("SelectionDAG ComplexPattern (" + R->getName() +
                        ") not mapped to GlobalISel");
```

`ComplexPatternEquivs` 的填充来自所有 `GIComplexPatternEquiv` 派生记录（同文件约 522 行）。所以：

> 只写 `ComplexPattern` 不写 `GIComplexPatternEquiv`，pattern 一定导不进来。

### C.2.4 执行器

`GIMatchTableExecutor.h:273` 有 `GIM_CheckComplexPattern`。匹配成功后，C++ 函数返回 `ComplexRendererFns`（一组 renderer lambda），渲染器再用它们填目标指令的操作数。

`selectAddrModeIndexed` 的签名（`AArch64InstructionSelector.cpp:7665`）：

```cpp
InstructionSelector::ComplexRendererFns
AArch64InstructionSelector::selectAddrModeIndexed(MachineOperand &Root,
                                                  unsigned Size) const;
```

`Size` 是内存访问的字节数，决定 scale：`Size=4` 时，12 位 immediate 的单位是 4 字节。

---

## C.3 五组必做测试（另加一个静态检查）

### 测试组 1：正常路径

| 函数 | 预测 | 验收 |
| --- | --- | --- |
| `ld0` | `LDRWui %0, 0` / `ldr w0, [x0]` | 无 `G_PTR_ADD` 残留 |
| `ld1` | `LDRWui %0, 1` / `ldr w0, [x0, #4]` | offset 以元素为单位 |

命令：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -verify-machineinstrs gep_extra.ll -o -
```

### 测试组 2：边界（4095 / 4096 / 4097 / -1）

| 函数 | 预测 | 验收 |
| --- | --- | --- |
| `ld1024` | `LDRWui %0, 1024` / `[x0, #4096]` | 仍可编码 |
| `ld4095` | `LDRWui %0, 4095` / `[x0, #16380]` | 最大 scaled immediate |
| `ld4096` | `MOVi32imm 16384` + `LDRWroX` | 超出 12 位，改寄存器 |
| `ld4097` | `MOVi32imm 16388` + `LDRWroX` | 同样走寄存器 |
| `ldneg1` | `LDURWi %0, -4` / `ldur` | 负偏移走 unscaled |

验收重点：4096 不是“选不出来”，而是**换了一条寻址形态**。这说明 ComplexPattern 失败会继续尝试其它 pattern，而不是直接 abort。

### 测试组 3：非法/不匹配 scaled 的偏移

`ld_off2` 的 GEP 偏移是 2 字节，`load i32` 的 scale 是 4：

```llvm
%q = getelementptr i8, ptr %p, i64 2
%v = load i32, ptr %q, align 1
```

预测：scaled 12 位模式要求 `RHSC % Size == 0`，2 不是 4 的倍数，所以失败；`selectAddrModeUnscaled` 接受 +2，选择 `LDURWi %0, 2`。

验收：汇编是 `ldur w0, [x0, #2]`，不是 `ldr`，也不是运行时错误。

### 测试组 4：多个 use

`ld_two_uses` 的 GEP 被两条 load 共用。预测：ComplexPattern 对每条 load 各跑一次，不要求单 use；`-O0` 不做 CSE，因此出现两条相同的 `LDRWui %0, 8`。

验收：MIR 两条 `LDRWui %0, 8`，汇编两条 `ldr w8, [x0, #32]`（`LDRWui` 的立即数是元素数 8，汇编显示字节偏移 32）。

### 测试组 5：feature 缺失与存在

两个子场景：

1. `aload_acq` 默认 `ldar w0, [x0]`；加 `-mattr=+rcpc-immo` 后 `ldapr w0, [x0]`（MIR 从 `LDARW` 变 `LDAPRW`）。
2. `cas.ll` 默认 `CMP_SWAP_32`（`ldaxr`/`stlxr` 循环）；加 `-mattr=+lse` 后 `CASALW`（`casal`）。

验收：feature 只改变子目标可选指令集，不改变 `am_indexed32` 的折叠逻辑；两者要分开解释。

### 测试组 6（可选，静态检查）：`_6b` 变体

`AArch64InstrFormats.td:3752` 有：

```tablegen
def am_indexed32_6b : ComplexPattern<iPTR, 2, "SelectAddrModeIndexedUImm<4,63>", []>;
```

在 23.1.4 里搜索 `gi_am_indexed32_6b`，你会**找不到**对应的 GI 记录。这不是笔误，而是一个很好的迁移练习：如果某条 6 位 scaled pattern 要在 GlobalISel 下工作，必须补一条 `GIComplexOperandMatcher<s64, "selectAddrModeIndexedUImm<4,63>">` + `GIComplexPatternEquiv<am_indexed32_6b>`。本练习只要求你指出这一点，不要求修改上游。

---

## C.4 五步实现建议

### 第 1 步：找到 pattern 与目标指令

在 `AArch64InstrFormats.td` 搜 `LDRWui`，确认它使用的 ComplexPattern 是 `am_indexed32`。在 `AArch64InstrGISel.td` / 生成的 `.inc` 里确认 GlobalISel 是否已有对应规则。

**检查点**：如果 `gi_am_indexed32` 不存在，`-gen-global-isel` 会在导入 `LDRWui` 的相关 pattern 时报 `ComplexPattern (...) not mapped to GlobalISel`。

### 第 2 步：确认 SelectionDAG 侧语义

读 `ComplexPattern<iPTR, 2, ...>`，确认：

- 输入是一个指针（`iPTR`）；
- 输出两个操作数（base、offset）；
- 目标指令的 operand list 必须能接住这两个操作数。

**检查点**：如果目标指令只声明了一个地址操作数，renderer 会多填一个，MachineVerifier 或 MC 层才会炸。

### 第 3 步：补 `GIComplexOperandMatcher` 与 `GIComplexPatternEquiv`

最小 patch 就是 C.2.2 的两行（若树里已有则跳过）。类型用 `s64`，C++ 函数名用 `selectAddrModeIndexed<32>`。

**检查点**：重新生成/编译后，`-gen-global-isel` 不再报 “not mapped”；`GIM_CheckComplexPattern` 出现在 matcher table 里。

### 第 4 步：核对 C++ 实现的分支顺序

按 `selectAddrModeIndexed` 的顺序检查：

1. `Root` 不是寄存器 → 失败；
2. `G_FRAME_INDEX` → `(fi, 0)`；
3. small code model → `tryFoldAddLowIntoImm`；
4. `isBaseWithConstantOffset` → 检查 `(RHSC & (Size-1)) == 0 && RHSC >= 0 && RHSC < (0x1000 << Scale)`；
5. `selectAddrModeUnscaled` 成功 → 返回 `nullopt`，让 unscaled pattern 接手；
6. 否则 → `(Root, 0)`。

**检查点**：4095 走第 4 步，4096 落到第 6 步，-1 走第 5 步。三者必须能在源码里一一对应。

### 第 5 步：跑五组测试并核对 `selected`

每条命令都加 `-verify-machineinstrs`，并检查：

```yaml
selected:        true
failedISel:      false
```

只看 `selected` 不够；第 2 章已经解释过 `failedISel` 的意义。

---

## C.5 参考解答

<details>
<summary>展开：完整源码依据、patch 与实测输出</summary>

### C.5.1 源码事实

**SelectionDAG 侧**（`AArch64InstrFormats.td:3744`）：

```tablegen
def am_indexed32 : ComplexPattern<iPTR, 2, "SelectAddrModeIndexed32", []>;
```

**GlobalISel 侧**（同文件 `:3761`）：

```tablegen
def gi_am_indexed32 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<32>">,
    GIComplexPatternEquiv<am_indexed32>;
```

**C++ 实现**（`AArch64InstructionSelector.cpp:7665`，节选；省略 frame index 与 small code model 分支）：

```cpp
  if (isBaseWithConstantOffset(Root, MRI)) {
    MachineOperand &LHS = RootDef->getOperand(1);
    MachineOperand &RHS = RootDef->getOperand(2);
    MachineInstr *LHSDef = MRI.getVRegDef(LHS.getReg());
    MachineInstr *RHSDef = MRI.getVRegDef(RHS.getReg());

    int64_t RHSC = (int64_t)RHSDef->getOperand(1).getCImm()->getZExtValue();
    unsigned Scale = Log2_32(Size);
    if ((RHSC & (Size - 1)) == 0 && RHSC >= 0 && RHSC < (0x1000 << Scale)) {
      if (LHSDef->getOpcode() == TargetOpcode::G_FRAME_INDEX)
        return {{
            [=](MachineInstrBuilder &MIB) { MIB.add(LHSDef->getOperand(1)); },
            [=](MachineInstrBuilder &MIB) { MIB.addImm(RHSC >> Scale); },
        }};

      return {{
          [=](MachineInstrBuilder &MIB) { MIB.add(LHS); },
          [=](MachineInstrBuilder &MIB) { MIB.addImm(RHSC >> Scale); },
      }};
    }
  }

  // Before falling back to our general case, check if the unscaled
  // instructions can handle this. If so, that's preferable.
  if (selectAddrModeUnscaled(Root, Size))
    return std::nullopt;

  return {{
      [=](MachineInstrBuilder &MIB) { MIB.add(Root); },
      [=](MachineInstrBuilder &MIB) { MIB.addImm(0); },
  }};
```

**Unscaled 兜底**（同文件 `:7592`）负责 `LDURWi` 的 `[-256, 255]` 范围；它对 `+2`、`-4` 都成立。

**导入失败点**（`GlobalISelEmitter.cpp:1035`）：

```cpp
  if (ComplexPattern == ComplexPatternEquivs.end())
    return failedImport("SelectionDAG ComplexPattern (" + R->getName() +
                        ") not mapped to GlobalISel");
```

### C.5.2 patch（若目标树没有 `gi_am_indexed32`）

```tablegen
// AArch64InstrFormats.td，紧邻 am_indexed32 的 gi_ 记录
def gi_am_indexed32 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<32>">,
    GIComplexPatternEquiv<am_indexed32>;
```

如果树里已经有（23.1.4 upstream 就有），不要重复定义；在 `isel-addr.patch` 里写一行注释：

```text
23.1.4 already provides gi_am_indexed32 at AArch64InstrFormats.td:3761;
the exercise is to reproduce/verify it, not to duplicate it.
```

### C.5.3 实测输出

所有命令都带 `-O0 -global-isel -global-isel-abort=1`。

**正常路径**：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -stop-after=instruction-select gep.ll -o -
```

```yaml
name:            ld0
    %1:gpr32 = LDRWui %0, 0 :: (load (i32) from %ir.q)

name:            ld4095
    %3:gpr32 = LDRWui %0, 4095 :: (load (i32) from %ir.q)
```

汇编：

```asm
ld0:
	ldr	w0, [x0]
	ret

ld4095:
	ldr	w0, [x0, #16380]
	ret
```

**边界 4096 / 4097**：

```yaml
name:            ld4096
    %4:gpr32 = MOVi32imm 16384
    %3:gpr32 = LDRWroX %0, %1, 0, 0 :: (load (i32) from %ir.q)
```

```asm
ld4096:
	mov	w8, #16384
	ldr	w0, [x0, x8]
	ret
```

```asm
ld4097:
	mov	w8, #16388
	ldr	w0, [x0, x8]
	ret
```

**1024 / -1**：

```asm
ld1024:
	ldr	w0, [x0, #4096]
	ret

ldneg1:
	ldur	w0, [x0, #-4]
	ret
```

`ld1024` 的 `LDRWui` 立即数是 1024，汇编字节偏移是 4096；`ldneg1` 是 `LDURWi -4`，因为负偏移不满足 `RHSC >= 0`。

**非对齐 +2**：

```yaml
name:            ld_off2
    %3:gpr32 = LDURWi %0, 2 :: (load (i32) from %ir.q, align 1)
```

```asm
ld_off2:
	ldur	w0, [x0, #2]
	ret
```

**多 use**：

```yaml
name:            ld_two_uses
    %3:gpr32 = LDRWui %0, 8 :: (load (i32) from %ir.q)
    %4:gpr32 = LDRWui %0, 8 :: (load (i32) from %ir.q)
```

```asm
ld_two_uses:
	ldr	w8, [x0, #32]
	ldr	w9, [x0, #32]
	add	w0, w8, w9
	ret
```

注意 MIR 里的 `8` 是“元素数”，汇编里的 `32` 是“字节偏移”；`simm12` 编码的是 `8`，MC 层按 scale 4 还原成 32。

**feature 缺失/存在**：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -stop-after=instruction-select acq.ll -o -
```

```yaml
%1:gpr32 = LDARW %0 :: (load acquire (i32) from %ir.p)
```

加 `-mattr=+rcpc-immo`：

```yaml
%1:gpr32 = LDAPRW %0 :: (load acquire (i32) from %ir.p)
```

汇编分别是 `ldar w0, [x0]` 与 `ldapr w0, [x0]`。

`cas.ll` 的对照：

```yaml
# 默认
early-clobber %5:gpr32, dead early-clobber %6:gpr32 = CMP_SWAP_32 ...
# +lse
%5:gpr32 = CASALW %1, %2, %0 :: (load store seq_cst seq_cst (i32) ...)
```

这两组不是 `am_indexed32` 的职责，但它们证明 feature predicate 与寻址折叠是两条独立的选路。

### C.5.4 结论表

| 场景 | 选中指令 | 关键分支 |
| --- | --- | --- |
| `ld0` | `LDRWui 0` | `Root` 直接使用 |
| `ld1` | `LDRWui 1` | `RHSC >> Scale` |
| `ld1024` | `LDRWui 1024` | 边界内 |
| `ld4095` | `LDRWui 4095` | `RHSC < 0x1000 << 2` |
| `ld4096` | `MOVi32imm` + `LDRWroX` | 超出 12 位 |
| `ld4097` | `MOVi32imm` + `LDRWroX` | 超出 12 位 |
| `ldneg1` | `LDURWi -4` | 负偏移走 unscaled |
| `ld_off2` | `LDURWi 2` | 非 4 倍数走 unscaled |
| `ld_two_uses` | 两条 `LDRWui 8` | 每条 use 独立折叠 |
| `aload_acq` | `LDARW` / `LDAPRW` | feature 决定 |
| `cas.ll` | `CMP_SWAP_32` / `CASALW` | feature 决定 |

### C.5.5 验收核对

- [ ] `-stop-after=instruction-select` 里所有函数 `selected: true`；
- [ ] `failedISel: false`；
- [ ] `-verify-machineinstrs` 无输出；
- [ ] 4095/4096 的差异能对应到 `RHSC < (0x1000 << Scale)`；
- [ ] `ld_off2` 是 `LDURWi`；
- [ ] 多 use 有两条 `LDRWui`；
- [ ] `ldar`/`ldapr` 与 `CMP_SWAP_32`/`CASALW` 的 feature 对照成立；
- [ ] patch 只动目标 `.td`（若需要）与练习目录。

</details>

---

## C.6 常见卡点

1. **只写 `ComplexPattern`，忘了 `GIComplexPatternEquiv`**。导入器直接 `failedImport("... not mapped to GlobalISel")`，pattern 静默消失。
2. **把 `ComplexPattern<iPTR, 2, ...>` 的 `2` 当成输入个数**。它是覆盖的操作数个数；目标指令 operand list 必须能接住 base + offset。
3. **用有符号数直接做 12 位范围判断**。源码先用 `getZExtValue()` 再转 `int64_t`，然后要求 `RHSC >= 0`；这样才能把负数留给 unscaled 路径。
4. **误解 `selectAddrModeUnscaled` 成功后的 `return std::nullopt`**。这不是失败，而是“让 unscaled pattern 接手”的信号；把它改成成功返回会抢掉 `LDURWi`。
5. **忘了 feature predicate**。`HasRCPC_IMMO` / `HasLSE` 不满足时，选出的指令在目标上不可用；测试必须显式跑“缺失”和“存在”两边。


---

## C.7 延伸挑战

1. **补 `gi_am_indexed32_6b`**：用 `GIComplexOperandMatcher<s64, "selectAddrModeIndexedUImm<4,63>">`，并找一条真正使用 6 位 scaled 的指令验证 63/64 边界。注意：这需要改源码并重新构建 `llc`，不能只用预编译二进制。
2. **扩展到 8/16 位访问**：用 `am_indexed8` / `am_indexed16` 重跑 `ldrb`/`ldrh` 的 4095/4096 边界，观察 scale 变化如何改变最大字节偏移。
3. **写一个 lit 测试**：把 C.3 的五组输出写成 `llvm/test/CodeGen/AArch64/GlobalISel/` 风格的 `RUN` + `FileCheck`，并加 `-verify-machineinstrs`。
4. **对照 RISC-V**：RISC-V 的 load/store 立即数直接编码在指令里，没有 AArch64 这种 `ComplexPattern` 抽象；把同一段 `gep.ll` 跑到 `riscv64` 上，比较“谁负责折叠 offset”。
5. **手写 select case**：如果不用 ComplexPattern，在 C++ 里直接匹配 `G_PTR_ADD` + `G_CONSTANT` 并改写 `G_LOAD`，再和 `selectAddrModeIndexed` 的实现对比。哪种更容易处理 frame index、small code model 与 unscaled 兜底？

---

## C.8 提交清单

- `isel-addr.patch`：只包含 `gi_am_indexed32`（若需要）与必要注释；
- `run-tests.ps1`：五组命令，输出到 `out/`；
- `results.md`：C.5.4 的表 + 每组一条“为什么”；
- 一段说明：4095 与 4096 的分界条件，以及 `ld_off2` 为什么走 `LDURWi`；
- 不修改 `README.md`、`memory/` 或其他章节。

---

## 下一章

[第 16 章 MIR/lit 测试方法](./16-mir-lit-testing.md) —— 本练习已经有可跑的 `llc` 命令与边界值；下一章把这些命令固化成 `RUN` 行、`CHECK` 和最小 MIR，让“地址折对了”变成可回归的测试，而不是一次性的手工作业。
