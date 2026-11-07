# Ch 12 — lld 重定位与 relaxation：`R_LARCH_RELAX` 的配对与 #239812

> 本章目标：读懂 `lld/ELF/Arch/LoongArch.cpp` 的 relaxation 主循环；理解 `R_LARCH_RELAX` 为什么是“同 offset 的标记”而不是“下一条”；并完整走一遍 #239812：hint-first 输入如何让 TLS LE 序列只收缩一半，以及修复为什么是 `tlsLeRelaxedAt`。

---

## 12.1 lld 的 LoongArch 支持长什么样

`lld/ELF/Arch/LoongArch.cpp` 约 1854 行，核心是四个入口：

| 函数 | 作用 |
|---|---|
| `scanSectionImpl()`（`:497`） | 把 ELF 的 `Rel`/`Rela` 读成 lld 的 `Relocation`，算 `RelExpr` |
| `relocate()` | 真正写出重定位值（`write32le`/`write64le`） |
| `relaxOnce(pass)`（`:1738`） | 一轮 relaxation，遍历所有可执行 section |
| `relax()`（`:1283`） | 单个 section 的状态机：决定每条重定位“删几条指令/改成什么” |

驱动在 `lld/ELF/Writer.cpp:1525`：**固定点迭代**，最多 30 轮：

```cpp
while (!ctx.arg.relocatable) {
  bool changed = ctx.target->needsThunks ? tc.createThunks(pass, ...)
                                         : ctx.target->relaxOnce(pass);
  ...
  if (changed && pass >= 30) { Err(ctx) << "address assignment did not converge"; break; }
}
```

为什么要多轮：删掉指令会改变地址，地址变了可能让别的分支/对齐又变得可 relax。每轮只允许“单调收缩”，并用 `pass < 4 ? UINT32_MAX : cur - delta` 防止来回震荡。

---

## 12.2 `R_LARCH_RELAX` 到底是什么

它不是“给下一个字节打标记”，而是：

> **在某个 offset 上放一条 `R_LARCH_RELAX`，表示“这个 offset 上的那条重定位所标注的指令可以被链接器删掉/改写”。**

所以一条可 relax 的指令会产生**两条同 offset 的重定位**：

```text
offset 0: R_LARCH_TLS_LE_HI20_R  (tvar)
offset 0: R_LARCH_RELAX
offset 4: R_LARCH_TLS_LE_ADD_R   (tvar)
offset 4: R_LARCH_RELAX
offset 8: R_LARCH_TLS_LE_LO12_R  (tvar)
offset 8: R_LARCH_RELAX
```

**ABI 只要求同 offset，不要求先后**。这一点是本章所有问题的根源。

**实测前提**：`llc` 默认不生成这些标记，必须开 `+relax`：

```console
$ llc ... -filetype=obj tls.ll && llvm-readobj -r tls.o
  0x0 R_LARCH_TLS_LE_HI20_R t
  0x4 R_LARCH_TLS_LE_ADD_R t
  0x8 R_LARCH_TLS_LE_LO12_R t

$ llc ... -mattr=+relax -filetype=obj tls.ll && llvm-readobj -r tls.o
  0x0 R_LARCH_TLS_LE_HI20_R t
  0x0 R_LARCH_RELAX -
  0x4 R_LARCH_TLS_LE_ADD_R t
  0x4 R_LARCH_RELAX -
  0x8 R_LARCH_TLS_LE_LO12_R t
  0x8 R_LARCH_RELAX -
```

源码依据：`LoongArchMCCodeEmitter.cpp:220` 的 `if (EnableRelax && RelaxCandidate) Fixups.back().setLinkerRelaxable();`，以及 `LoongArchAsmBackend.cpp:461` 的 `Fixup.isLinkerRelaxable()` 分支。`clang` 驱动默认传 `+relax`，`llc` 不会——写复现命令时很容易漏掉。

`relaxable()`（`:1105`）的实现却假设了“标记在后”：

```cpp
static bool relaxable(ArrayRef<Relocation> relocs, size_t i) {
  return i + 1 < relocs.size() && relocs[i + 1].type == R_LARCH_RELAX;
}
```

而 `Relocation` 的顺序来自 `scanSectionImpl()` 末尾：

```cpp
llvm::stable_sort(sec.relocs(), [](const Relocation &lhs, const Relocation &rhs) {
  return lhs.offset < rhs.offset;
});
```

`stable_sort` 只按 offset 排；**同 offset 的相对顺序保持输入顺序**。于是：

- 输入是“重定位在前、标记在后”（canonical）→ `relocs[i+1]` 是标记 → `relaxable` 为真。
- 输入是“标记在前、重定位在后”（hint-first）→ `relocs[i+1]` 是下一条重定位 → `relaxable` 为假。

---

## 12.3 TLS LE 序列与 relaxation 的目标

`localexec` TLS 的三条指令：

```asm
lu12i.w $a0, %le_hi20_r(t)     # offset 0
add.d   $a0, $a0, $tp, %le_add_r(t)   # offset 4
addi.d  $a0, $a0, %le_lo12_r(t)       # offset 8
```

链接器知道 `t` 相对 `$tp` 的偏移后，可以把整段收缩成一条：

```asm
addi.d $a0, $tp, 0
```

`relaxTlsLe()`（`:1265` 附近）做的事：

```cpp
case R_LARCH_TLS_LE_HI20_R:
case R_LARCH_TLS_LE_ADD_R:
  sec.relaxAux->relocTypes[i] = R_LARCH_RELAX;
  remove = 4;                     // 删掉这条 4 字节指令
  break;
case R_LARCH_TLS_LE_LO12_R:
  sec.relaxAux->writes.push_back(setJ5(currInsn, R_TP));   // 把基址寄存器改成 $tp
  sec.relaxAux->relocTypes[i] = R_LARCH_TLS_LE_LO12_R;
  break;
```

- 前两条：`remove = 4`，直接从输出里删掉；
- 第三条：**不能删**，要把 `addi.d $a0,$a0,imm` 的 `rj` 字段改成 `$tp`，并且立即数改成 0。

如果只删前两条、不改第三条，就得到 `addi.d $a0, $a0, 0`——读一个已经被删掉初始化的寄存器。**这就是 #239812 的“half relax”**。

---

## 12.4 hint-first 输入如何触发

测试 `lld/test/ELF/loongarch-tls-le-relax-order.s` 手工构造了两个 section，指令字节完全相同，只有重定位顺序不同：

```yaml
## canonical：标记跟在重定位后面
- Offset: 8
  Symbol: tvar
  Type: R_LARCH_TLS_LE_LO12_R
- Offset: 8
  Type: R_LARCH_RELAX

## hint_first：标记在重定位前面
- Offset: 8
  Type: R_LARCH_RELAX
- Offset: 8
  Symbol: tvar
  Type: R_LARCH_TLS_LE_LO12_R
```

前两条（offset 0、4）在两个 section 里都是 canonical 顺序，所以 `HI20_R`/`ADD_R` 仍然满足 `relaxable()`，会被删掉。

只有 offset 8 的消费者：

- canonical：`relaxable(relocs, i)` 为真 → 改写成 `$tp`；
- hint-first：`relocs[i+1]` 是 `LO12_R` 而不是 `R_LARCH_RELAX` → 为假 → **跳过**，指令原样保留。

结果：

```text
canonical : addi.d $a0, $tp, 0      ✅
hint-first: addi.d $a0, $a0, 0      ❌（$a0 已经没人初始化）
```

**为什么用 yaml 而不用 `.s`**：GNU as / LLVM 的汇编器都只产生 canonical 顺序。hint-first 只能由其它生产者（手写 ELF、yaml2obj、某些链接器的输出）产生。所以这个 bug 在“正常工具链”下很难复现，但 ABI 允许，lld 必须处理。

---

## 12.5 修复：记住“生产者已经被删”

修复没有去改 `relaxable()` 的语义（那会影响所有序列），而是在 `relax()` 里加一个**局部状态**：

```cpp
// Offset of the `lu12i.w` of the last TLS LE sequence whose address
// producing instructions were removed.
uint64_t tlsLeRelaxedAt = UINT64_MAX;
```

在处理 TLS LE 三条重定位时：

```cpp
bool relaxConsumer = r.type == R_LARCH_TLS_LE_LO12_R &&
                     tlsLeRelaxedAt != UINT64_MAX &&
                     r.offset == tlsLeRelaxedAt + 8;
if (!relaxable(relocs, i) && !relaxConsumer)
  break;
relaxTlsLe(ctx, sec, i, loc, r, remove);
if (remove == 4)
  tlsLeRelaxedAt = r.type == R_LARCH_TLS_LE_ADD_R ? r.offset - 4 : r.offset;
```

三个设计点：

1. **只在 `HI20_R`/`ADD_R` 被真正删掉时记录**（`remove == 4`），而不是看到 `_R` 就记录。这样“生产者没删、消费者也不改”的一致性仍然成立。
2. **`ADD_R` 的 offset 是 `HI20_R` + 4**，所以记录时回退 4，统一记 `lu12i.w` 的 offset。
3. **消费者判定用 `r.offset == tlsLeRelaxedAt + 8`**，即 `lu12i.w(0) / add.d(4) / addi.d(8)` 的固定布局。TLS LE 序列的形状是固定的，这个常量是安全的；但它也意味着**如果将来 ISA 扩展改变了序列长度，这里要同步改**。

修复后，hint-first 的消费者即使 `relaxable()` 为假，也会因为 `relaxConsumer` 为真而被改写。

---

## 12.6 验证

```console
$ llvm-lit -sv lld/test/ELF/loongarch-tls-le-relax-order.s
Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

测试对两个 section 用同一组 `CHECK`：

```asm
# CANONICAL-LABEL: <canonical>:
# CANONICAL-NEXT:    addi.d  $a0, $tp, 0
# HINT-FIRST-LABEL: <hint_first>:
# HINT-FIRST-NEXT:    addi.d  $a0, $tp, 0
```

也就是说：**两种顺序必须产出同一条指令**。这是这类 patch 最该写的测试形态——不是“断言修复后的行为”，而是“断言两种合法输入等价”。

回归面（本树实测）：

```console
$ llvm-lit -s -j 24 lld/test/ELF --filter loongarch
48 passed
```

---

## 12.7 为什么不是“直接改 relaxable()”

一个自然的想法：让 `relaxable()` 同时看前一条。但它是**所有** relaxation 序列共用的：

```cpp
static bool isPairRelaxable(ArrayRef<Relocation> relocs, size_t i) {
  return relaxable(relocs, i) && relaxable(relocs, i + 2) &&
         relocs[i].offset + 4 == relocs[i + 2].offset;
}
```

`PCALA_HI20`/`GOT_PC_HI20`/`CALL36` 等序列都依赖“标记紧跟其后”的位置假设。改 `relaxable()` 会让这些路径同时接受 hint-first，但它们的处理逻辑（`relaxPCHi20Lo12`、`relaxMediumCall`）没有相应的状态检查，可能引入新的 half-relax。

**局部修复 + 局部状态**是这里更安全的选择：影响面只有 TLS LE，reviewer 也容易验证。

---

## 12.8 反直觉点

1. **`R_LARCH_RELAX` 与目标重定位同 offset，顺序未定义**；`relaxable()` 的“看后一条”是位置假设，不是 ABI 保证。
2. **`stable_sort` 同 offset 保序**——所以输入顺序会一路传到 `relax()`，这是 bug 能存在的前提。
3. **half relax 比不 relax 更糟**：不 relax 只是慢，half relax 会产生读未初始化寄存器的代码。
4. **GNU as 永远产生 canonical 顺序**，所以这个 bug 只能靠 yaml/手写 ELF 复现；上游测试必须用 `yaml2obj`。
5. **relaxation 是多轮固定点**（最多 30 轮），不是一遍扫描。
6. **消费者不能删**，只能改寄存器字段（`setJ5(currInsn, R_TP)`）——删了就没有指令承接结果。
7. **`tlsLeRelaxedAt + 8` 是硬编码的序列长度**；这是修复的隐含契约，值得在 review 里点出来。

---

## 12.9 本章证据

| 结论 | 源码/命令 |
|---|---|
| `scanSectionImpl` + `stable_sort` | `lld/ELF/Arch/LoongArch.cpp:497,752` |
| `relaxable()` | `:1105` |
| `isPairRelaxable()` | `:1109` |
| `relaxTlsLe()` | `:1263-1280` |
| `relax()` 状态 | `:1283-1305` |
| 修复逻辑 | `:1355-1370` |
| 多轮驱动 | `lld/ELF/Writer.cpp:1525-1545` |
| 测试 | `lld/test/ELF/loongarch-tls-le-relax-order.s` |
| 实测通过 | 本章 12.6 |

---

## 12.10 练习

1. 把测试里的 `hint_first` section 的 offset 8 改成 4（只让 `ADD_R` 变 hint-first），预测会发生什么，再实际跑一遍。
2. 读 `relaxPCHi20Lo12()`，判断它是否也有“标记顺序”假设；如果有，构造一个 hint-first 的 `PCALA_HI20` 用例验证。
3. 在 `relax()` 里加一条 `LLVM_DEBUG`，打印每次 `remove` 和 `tlsLeRelaxedAt`，用 `-debug-only` 观察 canonical 与 hint-first 的差异。
4. 解释为什么 `pass < 4 ? UINT32_MAX : cur - delta` 能防止震荡；如果去掉会怎样？
5. 给 `tlsLeRelaxedAt + 8` 换成一个具名常量（如 `TLSLEAddressProducersSize`），评估这个改动的风险与收益。

---

## 12.11 延伸阅读

1. **relaxation 主循环与 `relaxable()`** — `lld/ELF/Arch/LoongArch.cpp`
   - 读什么：`scanSectionImpl()`（497 行）、`stable_sort`（752 行）、`relaxable()`（1105 行）、`isPairRelaxable()`（1109 行）、`relaxTlsLe()`（1263 行）、`relax()`（1283 行起，含 `tlsLeRelaxedAt` 在 1297 行与 1359–1365 行）。
   - 学到什么：本章 12.2/12.5 贴了核心片段；完整读 `relax()` 能看到 TLS LE 只是众多序列之一，`PCALA_HI20`/`CALL36`/`TLS_IE` 各自也有“标记在后”的位置假设。
   - 前提：读过本章 12.1–12.5。

2. **hint-first 的最小复现** — `lld/test/ELF/loongarch-tls-le-relax-order.s`
   - 读什么：文件头 2–5 行的注释、两条 `RUN`、`CANONICAL`/`HINT-FIRST` 的 `CHECK-NEXT`，以及 yaml 里两个 `.rela.text.*` 段的 offset 顺序（76 行起的 `hint_first` 说明）。
   - 学到什么：本章 12.4 解释了“只有 consumer 是 hint-first”；测试把 canonical 与 hint-first 放在同一个文件里用同一组 `CHECK` 断言等价，这是修这类 bug 最该模仿的测试形态。
   - 前提：知道 `yaml2obj` 和 `llvm-objdump -d` 的基本用法。

3. **`-r` 输出怎么读** — `llvm/docs/CommandGuide/llvm-readobj.md`
   - 读什么：`## ELF SPECIFIC OPTIONS` 里的 `--relocations, --relocs, -r`（101 行起），以及紧随其后的 `--section-relocations, --sr`（110 行起）。
   - 学到什么：本章多处用 `llvm-readobj -r` 观察同 offset 的 `R_LARCH_RELAX`；这一节说明 `-r` 是全局列出重定位，`--section-relocations` 是配合 `--sections` 按节列出，避免在混合输入里看错作用域。
   - 前提：跑过本章 12.2 的 `llvm-readobj -r`。

4. **链接器 relaxation 的通用模型** — `compilers/elf_linking/README.md`（相对 `/home/ubuntu/Learning-with-Claude`）
   - 读什么：Part 2 的 Ch 5《Relocation type 總論》、Ch 6《RISC-V 專屬 relocation 與 linker relaxation》与 Ch 7《R_RISCV_ALIGN 與 relaxation 的副作用》。
   - 学到什么：本章聚焦 LoongArch 的 `R_LARCH_RELAX` 配对；这门课用 RISC-V 的 `R_RISCV_RELAX`/`R_RISCV_ALIGN` 讲同一套“RELAX 标记 + 多轮收缩”机制，便于横向比较哪些是 LoongArch 特有问题。
   - 前提：读过本章 12.1 的固定点迭代。

## 12.12 下一步

Part 2 结束。**练习 B** 会把 ABI、TLS、链接串起来：用六种函数签名 + 三种 TLS 模型，追到 `.rela.text` 和最终 ELF。之后进入 Part 3（SelectionDAG）。
