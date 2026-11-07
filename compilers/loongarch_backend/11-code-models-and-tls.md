# Ch 11 — 代码模型与 TLS：`la.pcrel`、`la.got`、`la.tls.*`

> 本章目标：搞清楚 `small`/`medium`/`large` 到底差在哪（答案可能和你想的不一样）；能把一个 TLS 访问拆成 `%le_hi20_r` / `%ie_pc_hi20` / `%gd_pc_hi20` 三种序列；并知道 `_r` 后缀和 `R_LARCH_RELAX` 的关系——也就是 Ch 12 与 #239812 的入口。

---

## 11.1 默认代码模型

`LoongArchTargetMachine.cpp:81`：

```cpp
if (!CM)
  return TT.isArch64Bit() ? CodeModel::Medium : CodeModel::Small;
...
case CodeModel::Large:
  if (!TT.isArch64Bit())
    report_fatal_error("Large code model requires LA64");
```

| triple | 默认 |
|---|---|
| `loongarch64-*` | **Medium** |
| `loongarch32-*` | Small |
| Large | 仅 LA64 |

**注意**：LA64 默认不是 Small。这解释了你会在 `llc` 输出里看到 `pcaddu18i + jirl` 而不是 `bl`（见 11.3）。

---

## 11.2 数据寻址：Small 与 Medium 完全相同

`getAddr()`（`LoongArchISelLowering.cpp:4606`）：

```cpp
case CodeModel::Small:
case CodeModel::Medium:
  if (IsLocal) {
    // PseudoLA_PCREL -> (pcalau12i %pc_hi20) + (addi.d %pc_lo12)
  } else {
    // PseudoLA_GOT   -> (pcalau12i %got_pc_hi20) + (ld.d %got_pc_lo12)
  }
```

也就是说，**数据访问上 Small 和 Medium 是同一套代码**。实测（`-relocation-model=static`）：

```llvm
@x = dso_local global i64 0
define ptr @g() { ret ptr @x }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -code-model=small
g:
	pcalau12i	$a0, %pc_hi20(x)
	addi.d	$a0, $a0, %pc_lo12(x)
	ret
```

两条指令，PC 相对 ±2GB，这就是 `la.pcrel`。

把 `dso_local` 去掉（变成可能被 preempt 的全局），走 GOT：

```text
g:
	pcalau12i	$a0, %got_pc_hi20(x)
	ld.d	$a0, $a0, %got_pc_lo12(x)
	ret
```

`ld.d` 从 GOT 里取地址——因为符号可能被动态库抢占，必须走 GOT。

**判定入口**是 `lowerGlobalAddress()`（`:4698`）：

```cpp
if (GV->isDSOLocal() && isa<GlobalVariable>(GV)) {
  if (auto GCM = dyn_cast<GlobalVariable>(GV)->getCodeModel())
    CM = *GCM;
}
return getAddr(N, DAG, CM, GV->isDSOLocal());
```

注意 `IsLocal` 就是 `GV->isDSOLocal()`，与代码模型正交。**“用 pcrel 还是 got”看的是 `dso_local`，“能不能寻址”看的才是代码模型**。

---

## 11.3 Small vs Medium：差别在“调用”，不在“取地址”

`LowerCall`（`:10818`）：

```cpp
switch (DAG.getTarget().getCodeModel()) {
case CodeModel::Small:  Op = IsTailCall ? TAIL : CALL;               break;
case CodeModel::Medium: Op = IsTailCall ? TAIL_MEDIUM : CALL_MEDIUM; break;
case CodeModel::Large:  Op = IsTailCall ? TAIL_LARGE : CALL_LARGE;   break;
}
```

对应的伪指令：

```tablegen
def PseudoCALL_SMALL  : Pseudo<(outs), (ins bare_symbol:$func)>;
let isCall = 1, Defs = [R1, R20], Size = 8 in
def PseudoCALL_MEDIUM : Pseudo<(outs), (ins bare_symbol:$func)>;
```

实测同一个调用：

```console
$ llc -code-model=small
	bl	use
$ llc -code-model=medium
	pcaddu18i	$ra, %call36(use)
	jirl	$ra, $ra, 0
```

- **Small**：`bl` 一条（26 位、±128MB）。
- **Medium**：`pcaddu18i + jirl` 两条（call36，±128GB？36 位），并且 `Defs=[R1,R20]`——展开时会占用 `$r20` 做临时寄存器。
- `PseudoCALL_MEDIUM` 的展开在 `LoongArchExpandPseudoInsts.cpp:811` 的 `expandFunctionCALL`。

所以“medium 比 small 更通用”的代价是**每次调用多一条指令 + 多一个被 clobber 的寄存器**。这就是为什么 `-O2` 下 LA64 的程序比“纯 `bl`”版本略大。

> 如果你的目标是“减小代码体积”，把默认 medium 改成 small 会是一个诱人但危险的补丁：small 只保证 ±128MB 的代码距离，超过就链接失败。上游不会接受无条件改默认值；可行方向是**在确定范围内自动选择 small**（类似链接器的 `--relax`），或者给 `-mcmodel=small` 用户更好的诊断。

---

## 11.4 Large：四指令构造 64 位偏移

```console
$ llc -code-model=large   # @x 仍是 dso_local
g:
	pcalau12i	$a0, %pc_hi20(x)
	addi.d	$a1, $zero, %pc_lo12(x)
	lu32i.d	$a1, %pc64_lo20(x)
	lu52i.d	$a1, $a1, %pc64_hi12(x)
	add.d	$a0, $a1, $a0
	ret
```

四个重定位拼出一个 64 位偏移，再用 `add.d` 加上 PC 高位。GOT 版本用 `ldx.d $a0, $a1, $a0`（基址+索引）。

Large 的约束：

- 仅 LA64；
- 所有符号都用 64 位偏移，代码体积明显变大；
- `PseudoCALL_LARGE` 的展开也走 `expandFunctionCALL`，但走 64 位序列。

---

## 11.5 TLS 三模型

`lowerGlobalTLSAddress()`（`:4801`）按 IR 的 TLS model 分派：

| IR | 伪指令 | 典型序列 |
|---|---|---|
| `thread_local(localexec)` | `PseudoLA_TLS_LE` | `lu12i.w %le_hi20_r` + `add.d %le_add_r,$tp` + `ld.d %le_lo12_r` |
| `thread_local(initialexec)` | `PseudoLA_TLS_IE` | `pcalau12i %ie_pc_hi20` + `ld.d %ie_pc_lo12` + `ldx.d $tp` |
| 默认（general dynamic） | `PseudoLA_TLS_GD` | `pcalau12i %gd_pc_hi20` + `addi.d %got_pc_lo12` + `bl __tls_get_addr` |

### localexec（实测）

```asm
lu12i.w	$a0, %le_hi20_r(t)
add.d	$a0, $a0, $tp, %le_add_r(t)
ld.d	$a0, $a0, %le_lo12_r(t)
```

这就是 #239812 里那条序列。`_r` 后缀表示“**relaxable**”：开启 `+relax` 时，汇编器会在每条指令后面追加一个 `R_LARCH_RELAX` 标记，链接器可以把三条收缩成 `addi.d $a0, $tp, 0`。

**注意**：`llvm-mc`/`llc` 默认**不带** `+relax`，此时 `llvm-readobj -r` 只能看到 3 条 `R_LARCH_TLS_LE_*_R`，看不到 `R_LARCH_RELAX`；加 `-mattr=+relax` 才会成对出现。`clang` 驱动默认传 `+relax`（`-mno-relax` 才关）。这一点在 Ch 12 有实测对照。

**关键点**：`lu12i.w/add.d/addi.d` 用的是 `%le_hi20_r`/`%le_add_r`/`%le_lo12_r`，而 `%le_hi20`（无 `_r`）是**不参与 relaxation** 的版本。两者生成的指令相同、重定位不同。写测试或改 lowering 时搞混这两个，就会出现“能链接但没收缩”的诡异现象。

### initialexec（实测）

```asm
pcalau12i	$a0, %ie_pc_hi20(t2)
ld.d	$a0, $a0, %ie_pc_lo12(t2)
ldx.d	$a0, $a0, $tp
```

从 GOT 取“TLS 变量相对 `$tp` 的偏移”，再用 `ldx.d` 加上线程指针。不需要调用 `__tls_get_addr`。

### general dynamic（PIC，实测）

```asm
pcalau12i	$a0, %gd_pc_hi20(t3)
addi.d	$a0, $a0, %got_pc_lo12(t3)
bl	__tls_get_addr
ld.d	$a0, $a0, 0
```

`__tls_get_addr` 返回变量的真实地址，再 `ld.d` 取值。这条路径最贵，也是唯一需要 runtime 调用的。

### 一个反直觉的实测结果

**静态链接（非 PIC）下，general dynamic 会被降级成 initialexec**：

```console
$ llc -relocation-model=static   # @t3 = thread_local（默认 GD）
tlsgd:
	pcalau12i	$a0, %ie_pc_hi20(t3)
	ld.d	$a0, $a0, %ie_pc_lo12(t3)
	ldx.d	$a0, $a0, $tp
	ret
```

原因：静态可执行文件里没有“动态加载”这回事，GD 的 `__tls_get_addr` 路径没有必要，直接按 IE 处理。链接器仍可进一步 relax。

---

## 11.6 `MO_*` 标志与 `PseudoLA_*`

这些伪指令不是“汇编文本”，而是 `MachineInstr`，在 `ExpandPseudoInsts` 或 MC 层展开。它们携带的标志决定后续优化：

- `MO_RELAX`：这条指令后面要跟 `R_LARCH_RELAX`；
- `MO_PCREL`/GOT 相关：影响 `MachineLICM` 能否提升；
- GOT load 被标成 `MOLoad | MODereferenceable | MOInvariant`（`getAddr()` 末尾），所以可以被 LICM 提出循环——**这是“为什么循环里的全局变量只取一次地址”的源码依据**。

---

## 11.7 反直觉点

1. **LA64 默认是 Medium，不是 Small**；所以默认调用是 `pcaddu18i + jirl`，不是 `bl`。
2. **Small 和 Medium 的数据寻址完全一样**；差别只在调用（`bl` vs call36）。
3. **`dso_local` 决定 pcrel 还是 got**，与代码模型正交。
4. **`_r` 后缀才有 relaxation**；没有 `_r` 的同名修饰符不生成 `R_LARCH_RELAX`。
5. **静态非 PIC 下 GD 被降级成 IE**；你在 `llc` 输出里看不到 `__tls_get_addr`，不代表 GD 路径不存在（PIC 下才有）。
6. **Large 的数据序列是 5 条指令**（4 条构造偏移 + 1 条 add/ldx），不是 2 条。
7. **`PseudoCALL_MEDIUM` 声明了 `Defs=[R1,R20]`**：普通调用把目标放 `$ra`，**尾调用**放 `$r20`（`$t8`），见 `expandFunctionCALL`。别把它当成“每次调用都用 `$r20`”。

---

## 11.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| 默认代码模型 | `LoongArchTargetMachine.cpp:81-99` |
| `getAddr` 分支 | `LoongArchISelLowering.cpp:4606-4665` |
| `dso_local` 判定 | `:4698-4707` |
| CALL 分支 | `:10818-10832` |
| `PseudoCALL_MEDIUM` 定义 | `LoongArchInstrInfo.td:1793-1800` |
| `expandFunctionCALL` | `LoongArchExpandPseudoInsts.cpp:811` |
| TLS 分派 | `LoongArchISelLowering.cpp:4801-4860` |
| small/medium 调用实测 | 本章 11.3 |
| large 序列实测 | 本章 11.4 |
| TLS 三模型实测 | 本章 11.5 |

---

## 11.9 练习

1. 用 `-code-model=small` 和 `medium` 各编译一个含 100 次调用的文件，比较 `.text` 大小，算出每次调用多几字节。
2. 给 `@x` 加 per-global 代码模型：`@x = dso_local global i64 0, code_model "small"`，在 `-code-model=large` 下验证它仍走 2 条指令（**必须同时有 `dso_local`**，否则属性不生效——这是实测出来的坑）。
3. 把 `thread_local(localexec)` 改成 `thread_local(initialexec)`，用 `llvm-readobj -r` 对比 `.rela.text` 中的重定位类型。
4. 用 `-relocation-model=pic` 编译 GD 变量，找出 `__tls_get_addr` 的调用序列，并解释为什么它不能 relax。
5. 找出 `%le_hi20` 与 `%le_hi20_r` 在 `LoongArchMCExpr` 里的 `VariantKind` 枚举值，确认它们生成的重定位不同。

---

## 11.10 延伸阅读

1. **IR 层的 TLS 模型语义** — `llvm/docs/LangRef.md`
   - 读什么：`### Thread Local Storage Models`（598 行起），四种模型（`localdynamic`/`initialexec`/`localexec`/默认 general dynamic）的定义，以及“target 可以改选模型”的那段。
   - 学到什么：本章 11.5 从 `lowerGlobalTLSAddress()` 的分派反推模型；这一节是规范侧的定义，能解释为什么静态非 PIC 下 GD 可以被降级成 IE（“target may choose a different TLS model”）。
   - 前提：知道 ELF TLS 的四种模型名字。

2. **`getAddr()` 与 `lowerGlobalTLSAddress()` 的分支** — `llvm/lib/Target/LoongArch/LoongArchISelLowering.cpp`
   - 读什么：`getAddr()`（4605 行起）里 Small/Medium/Large 的 `switch`、`lowerGlobalAddress()`（4696 行起）的 `isDSOLocal()` 判定、`lowerGlobalTLSAddress()`（4801 行起）的三种 `PseudoLA_TLS_*` 分派，以及 `getAddr()` 末尾 `!IsLocal` 分支设置的 `MOInvariant` 内存操作数。
   - 学到什么：本章 11.2/11.4 只贴了关键片段；源码里 GOT load 被标成 `MOLoad | MODereferenceable | MOInvariant` 的位置，能解释 11.6 说的 LICM 为什么能把循环里的全局变量地址提到循环外。
   - 前提：读过本章 11.2–11.5。

3. **TLS 与代码模型的 lit 矩阵** — `llvm/test/CodeGen/LoongArch/tls-models.ll`
   - 读什么：开头 9 条 `RUN` 行（LA32/LA64、PIC/非 PIC、`--code-model=large`、`--enable-tlsdesc`），以及每个模型对应的 `CHECK` 序列；文件注释还特别要求符号保持 external。
   - 学到什么：本章 11.5 的“静态非 PIC 下 GD 降级成 IE”在测试里用不同 triple/relocation-model 组合固定下来；注释里“避免所有变量都被优化成 Local Exec”正是 11.5 那个反直觉结果的测试侧前提。
   - 前提：读过本章 11.5 的三种序列。

4. **PIC 与 TLS 的链接器视角** — `compilers/elf_linking/README.md`（相对 `/home/ubuntu/Learning-with-Claude`）
   - 读什么：Part 4 的 Ch 11《PIC / PIE 與 code model》与 Ch 12《TLS Model：LE / IE / GD / LD》。
   - 学到什么：本章从编译器 lowering 角度看 TLS；这门课从链接器和动态加载角度看同一组模型，能补上 `GOT`/`PLT`/`__tls_get_addr` 在链接后如何落地。
   - 前提：读过本章 11.2 的 pcrel/GOT 区分。

## 11.11 下一步

下一章进链接器：`lld/ELF/Arch/LoongArch.cpp` 怎么把 `R_LARCH_RELAX` 和它标注的重定位配对、`relax()` 状态机怎么写、以及 #239812 的 hint-first 输入为什么会让它只收缩一半。
