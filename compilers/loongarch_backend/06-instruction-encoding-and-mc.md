# Ch 06 — 指令编码与 MC 层：从 32 位字到 `.rela.text`

> 本章目标：把 `MachineInstr` → `MCInst` → 32 位编码 → fixup/relocation 这条链走通。你会手算 `add.d` 的编码，也会看到 `%pc_hi20(sym)` 为什么不是“立刻填一个数”，而是一条等待链接器的重定位。

---

## 6.1 MC 层负责什么

| 组件 | 职责 |
|---|---|
| `AsmPrinter` | `MachineInstr` → `MCInst`，并发出 `.cfi`/`.section`/`.option` 等 |
| `MCCodeEmitter` | `MCInst` → 32 位编码 + `MCFixup` |
| `MCAsmBackend` | fixup 的范围检查、位域写入、重定位生成 |
| `MCELFObjectWriter` | `MCFixup` → `R_LARCH_*`、符号类型、ELF 头 |
| `MCStreamer` | 汇编文本/对象文件的输出管线 |
| `AsmParser` | 文本 → `MCInst`（Ch 07） |
| `Disassembler` | 字节 → `MCInst`（Ch 07） |

在 LoongArch 里，**绝大多数指令是一条 4 字节字**；复杂地址加载被拆成多条指令 + 多条重定位。

---

## 6.2 `encodeInstruction`：4 字节的出口

`LoongArchMCCodeEmitter::encodeInstruction()`：

```cpp
const MCInstrDesc &Desc = MCII.get(MI.getOpcode());
unsigned Size = Desc.getSize();

switch (MI.getOpcode()) {
default:
  break;
case LoongArch::PseudoVREPLI_B:
...
  return expandToVectorLDI<LoongArch::VLDI>(MI, CB, Fixups, STI);
case LoongArch::PseudoAddTPRel_W:
case LoongArch::PseudoAddTPRel_D:
  return expandAddTPRel(MI, CB, Fixups, STI);
}

switch (Size) {
default:
  llvm_unreachable("Unhandled encodeInstruction length!");
case 4: {
  uint32_t Bits = getBinaryCodeForInstr(MI, Fixups, STI);
  support::endian::write(CB, Bits, llvm::endianness::little);
  break;
}
}
```

三个要点：

1. **普通指令走 `getBinaryCodeForInstr`**：这是 TableGen 生成的编码函数，按 `Fmt*` 的位域把操作数塞进去。
2. **伪指令在 MC 层展开**：`PseudoAddTPRel_D` 变成 `ADD_D` + 一条 `R_LARCH_TLS_LE_ADD_R`；`PseudoVREPLI_*` 变成 `VLDI`/`XVLDI`。
3. **小端写入**：`support::endian::write(..., little)`。

---

## 6.3 手算 `add.d`

`Fmt3R` 位域（Ch 04）：

```text
31–15: opcode 高位
14–10: rk
 9–5 : rj
 4–0 : rd
```

`add.d $a0, $a1, $a2`：

```text
op  = 0x00108000
rk  = $a2 = 6  → 6 << 10 = 0x00001800
rj  = $a1 = 5  → 5 << 5  = 0x000000a0
rd  = $a0 = 4  →            0x00000004
---------------------------------------
      0x001098a4
```

小端：

```text
a4 98 10 00
```

实测：

```text
$ llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding /tmp/enc.s
add.d $a0, $a1, $a2   # encoding: [0xa4,0x98,0x10,0x00]
```

注意：`llvm-mc` 的 `-show-encoding` 输出的是**字节序列**，不是 32 位整数。读的时候要按小端拼回 `0x001098a4`。

---

## 6.4 fixup 与 relocation 是两件事

LoongArch 的 MC 层有两类“需要链接器/汇编器后处理”的引用：

### 6.4.1 目标 fixup（汇编时就要改指令位域）

`LoongArchFixupKinds.h`：

```cpp
enum Fixups {
  fixup_loongarch_b16 = FirstTargetFixupKind,
  fixup_loongarch_b21,
  fixup_loongarch_b26,
  fixup_loongarch_abs_hi20,
  fixup_loongarch_abs_lo12,
  fixup_loongarch_abs64_lo20,
  fixup_loongarch_abs64_hi12,
  fixup_loongarch_dtprel32,
  fixup_loongarch_dtprel64,
  fixup_loongarch_invalid,
  NumTargetFixupKinds = fixup_loongarch_invalid - FirstTargetFixupKind,
};
```

`AsmBackend` 的位置表（`{name, offset, bits, flags}`）：

```cpp
{"fixup_loongarch_b16", 10, 16, 0},
{"fixup_loongarch_b21", 0, 26, 0},
{"fixup_loongarch_b26", 0, 26, 0},
{"fixup_loongarch_abs_hi20", 5, 20, 0},
{"fixup_loongarch_abs_lo12", 10, 12, 0},
{"fixup_loongarch_abs64_lo20", 5, 20, 0},
{"fixup_loongarch_abs64_hi12", 10, 12, 0},
{"fixup_loongarch_dtprel32", 0, 32, 0},
{"fixup_loongarch_dtprel64", 0, 64, 0},
```

`offset` 是“从 32 位字的第几位开始”。例如 `abs_hi20` 从 bit 5 开始、20 位宽，因为 bit 0–4 是 `rd`；`abs_lo12` 从 bit 10 开始、12 位宽，因为 bit 0–9 是 `rd + rj`。

`adjustFixupValue()` 负责把值变成位域：

```cpp
case fixup_loongarch_abs_hi20:
  return (Value >> 12) & 0xfffff;
case fixup_loongarch_abs_lo12:
  return Value & 0xfff;
```

分支 fixup 还会检查对齐和范围：

```cpp
case fixup_loongarch_b16:
  if (!isInt<18>(Value)) reportOutOfRangeError(Ctx, ..., 18);
  if (Value % 4) Ctx.reportError(..., "fixup value must be 4-byte aligned");
  return (Value >> 2) & 0xffff;
```

这解释了为什么 `b`/`bl` 的目标必须是 4 字节对齐，且范围是 ±128MB（28 位有符号，最低两位为 0）。

### 6.4.2 原始重定位（链接器处理）

`%pc_hi20`、`%got_pc_hi20`、`%le_hi20_r` 这类 specifier 不是 target fixup，而是**直接以 ELF relocation type 作为 fixup kind**。`MCCodeEmitter::getExprOpValue()`：

```cpp
if (Kind == MCExpr::Specifier) {
  const LoongArchMCExpr *LAExpr = cast<LoongArchMCExpr>(Expr);
  FixupKind = LAExpr->getSpecifier();
  RelaxCandidate = LAExpr->getRelaxHint();
  switch (uint16_t(LAExpr->getSpecifier())) {
  case ELF::R_LARCH_B16:  FixupKind = LoongArch::fixup_loongarch_b16; break;
  ...
  case ELF::R_LARCH_TLS_LE_HI20_R:
  case ELF::R_LARCH_TLS_LE_LO12_R:
    RelaxCandidate = true;
    break;
  }
}
...
addFixup(Fixups, 0, Expr, FixupKind);
if (EnableRelax && RelaxCandidate)
  Fixups.back().setLinkerRelaxable();
```

区别：

| 类别 | 例子 | 谁处理 | 结果 |
|---|---|---|---|
| target fixup | `%b16`, `%abs_hi20` | `AsmBackend` | 直接改指令位域；若不能解析，生成对应 relocation |
| 原始 relocation | `%pc_hi20`, `%got_pc_lo12`, `%le_hi20_r` | `ELFObjectWriter` + 链接器 | 指令位域填 0（或保留），生成 `R_LARCH_*` |

`setLinkerRelaxable()` 是 Ch 00 那条 `R_LARCH_RELAX` 的来源：**当 `+relax` 打开且该 relocation 是 relax 候选时，fixup 会多带一个 marker**。

---

## 6.5 特例：`PseudoAddTPRel_D`

TLS LE 序列里的 `add.d $rd, $rd, $tp` 不是普通 `ADD_D`，因为它还要带 `R_LARCH_TLS_LE_ADD_R`。MC 层的 `expandAddTPRel()`：

```cpp
const LoongArchMCExpr *Expr = dyn_cast<LoongArchMCExpr>(Symbol.getExpr());
assert(Expr && Expr->getSpecifier() == ELF::R_LARCH_TLS_LE_ADD_R && ...);

addFixup(Fixups, 0, Expr, ELF::R_LARCH_TLS_LE_ADD_R);
if (STI.hasFeature(LoongArch::FeatureRelax))
  Fixups.back().setLinkerRelaxable();

unsigned ADD = MI.getOpcode() == LoongArch::PseudoAddTPRel_D
                   ? LoongArch::ADD_D : LoongArch::ADD_W;
MCInst TmpInst =
    MCInstBuilder(ADD).addOperand(Rd).addOperand(Rj).addOperand(Rk);
```

它做了两件事：

1. 为 `$tp` 相对加法的符号生成一条 `R_LARCH_TLS_LE_ADD_R`；
2. 生成真正的 `ADD_D`/`ADD_W` 编码。

这解释了为什么 Ch 03 的 MIR 里是 `PseudoAddTPRel_D`，而最终 `.o` 里是 `add.d` + `R_LARCH_TLS_LE_ADD_R`。

---

## 6.6 特例：向量立即数 `PseudoVREPLI_*`

`expandToVectorLDI()` 把 `PseudoVREPLI_B/H/W/D` 变成 `VLDI`，并把“元素宽度”编码进立即数的高两位：

```cpp
int64_t Imm = MI.getOperand(1).getImm() & 0x3FF;
switch (MI.getOpcode()) {
case LoongArch::PseudoVREPLI_H: Imm |= 0x400; break;
case LoongArch::PseudoVREPLI_W: Imm |= 0x800; break;
case LoongArch::PseudoVREPLI_D: Imm |= 0xC00; break;
}
```

这类“伪指令在 MC 层展开”的设计，让你可以在 `.s` 里写更自然的 `vrepli.b`，而不用手动拼 `vldi` 的编码。

---

## 6.7 `AsmBackend::applyFixup`：真正写位域

```cpp
void LoongArchAsmBackend::applyFixup(const MCFragment &F, const MCFixup &Fixup,
                                     const MCValue &Target, uint8_t *Data,
                                     uint64_t Value, bool IsResolved) {
  addReloc(F, Fixup, Target, Value, IsResolved);
  if (!Value)
    return; // Doesn't change encoding.

  auto Kind = Fixup.getKind();
  if (mc::isRelocation(Kind))
    return;
  MCFixupKindInfo Info = getFixupKindInfo(Kind);
  ...
}
```

顺序很重要：

1. **先 `addReloc`**：即使 fixup 能解析，也要记录重定位（某些情况下链接器仍需要）。
2. **值为 0 时直接返回**：不改编码，但重定位已经记下。
3. **原始 relocation 直接返回**：它们不改指令位域，等链接器处理。
4. **target fixup 才写位域**：按 `Info.TargetOffset` 移位、掩码。

---

## 6.8 `ELFObjectWriter`：fixup → `R_LARCH_*`

`LoongArchELFObjectWriter::getRelocType()`：

```cpp
switch (Target.getSpecifier()) {
case ELF::R_LARCH_TLS_LE_HI20:
case ELF::R_LARCH_TLS_LE_HI20_R:
...
  if (auto *SA = const_cast<MCSymbol *>(Target.getAddSym()))
    static_cast<MCSymbolELF *>(SA)->setType(ELF::STT_TLS);
  break;
}

auto Kind = Fixup.getKind();
if (mc::isRelocation(Fixup.getKind()))
  return Kind;
switch (Kind) {
default:
  reportError(Fixup.getLoc(), "Unsupported relocation type");
  return ELF::R_LARCH_NONE;
...
}
```

两个容易忽略的点：

- **TLS 符号要标成 `STT_TLS`**。否则链接器不会把它放进 TLS 段，`R_LARCH_TLS_*` 也无法正确解析。
- **原始 relocation 直接返回 `Kind`**：`Kind` 本身就是 ELF relocation type（例如 71 = `R_LARCH_PCALA_HI20`）。

实测：

```bash
$T/bin/llvm-readobj -r /tmp/tlsle.o
```

```text
0x0 R_LARCH_TLS_LE_HI20_R tvar
0x0 R_LARCH_RELAX
0x4 R_LARCH_TLS_LE_ADD_R tvar
0x4 R_LARCH_RELAX
0x8 R_LARCH_TLS_LE_LO12_R tvar
0x8 R_LARCH_RELAX
```

---

## 6.9 从 `%pc_hi20` 到 `.rela.text`

```bash
printf 'pcalau12i $a0, %%pc_hi20(sym)\n' > /tmp/pc.s
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding /tmp/pc.s
```

实测：

```text
pcalau12i $a0, %pc_hi20(sym)   # encoding: [0x04,0x00,0x00,0x1a]
                               #   fixup A - offset: 0, value: %pc_hi20(sym),
                               #   relocation type: 71
```

`0x1a000004` 是 `PCALAU12I $a0, 0` 的编码；立即数位域暂时为 0，等待链接器填。`relocation type: 71` 就是 `R_LARCH_PCALA_HI20`。

---

## 6.10 反直觉点

1. **fixup 与 relocation 是两件事**：前者在汇编期改指令位域，后者留给链接器（6.4）。
2. **fixup 的位域位置是手写表**：`abs_hi20` 从 bit 5 开始、20 位宽；`abs_lo12` 从 bit 10 开始、12 位宽（6.4.1）。
3. **重定位类型由 `ELFObjectWriter` 选**，不是 `MCCodeEmitter`；同一个 fixup 可能映射到不同的 `R_LARCH_*`（6.8）。
4. **编码是手写 shift/or**：TableGen 只提供 `Fmt*` 的位域布局，真正的拼接在 `encodeInstruction` 里（6.2/6.3）。
5. **分支 fixup 会检查 4 字节对齐和 ±128MB 范围**，越界在汇编期就报错，不会留到链接器（6.4.1）。
6. **`PseudoAddTPRel_D`/`PseudoVREPLI_*` 是编码层特例**：只读 `.td` 的 pattern 会漏掉它们（6.5/6.6）。

---

## 6.11 本章证据

| 结论 | 命令/源码 |
|---|---|
| 编码入口 | `LoongArchMCCodeEmitter::encodeInstruction` |
| 位域 | `Fmt3R` + `getBinaryCodeForInstr` |
| target fixup 表 | `LoongArchAsmBackend::getFixupKindInfo` |
| fixup 值调整 | `adjustFixupValue` |
| 原始 relocation | `MCCodeEmitter::getExprOpValue` |
| TLS add 特例 | `expandAddTPRel` |
| 向量立即数 | `expandToVectorLDI` |
| ELF relocation | `LoongArchELFObjectWriter::getRelocType` |
| 实测 | `llvm-mc -show-encoding`、`llvm-readobj -r` |

---

## 6.12 练习

1. 手算 `addi.d $a0, $a1, -4` 的编码，再用 `llvm-mc -show-encoding` 验证（注意 12 位立即数的有符号编码）。
2. 用 `llvm-mc -show-encoding` 观察 `%b16`、`%b21`、`%b26` 的 fixup offset/bits，解释为什么 `b16` 的 offset 是 10。
3. 构造一个 `%le_add_r` 的汇编（提示：`add.d $a0, $a0, $tp, %le_add_r(tvar)` 可能不是合法语法；从 MIR 或 `.td` 的伪指令路径入手），确认它生成 `R_LARCH_TLS_LE_ADD_R`。
4. 读 `applyFixup`，解释“值为 0 时直接返回”为什么不会丢掉重定位。

---

## 6.13 延伸阅读

1. **ISA 手册里的格式图与编码表** — `https://loongson.github.io/LoongArch-Documentation/LoongArch-Vol1-EN.html`（外部链接）
   - 读什么：`1.2. Instruction formats` 的 2R/3R/2RI12 等格式图，以及 `2.2.1.1. add.w/d, sub.w/d`、`2.2.1.4. lu12i.w, lu32i.d, lu52i.d` 的编码表。
   - 学到什么：核对 6.3 手算的 `rd/rj/rk` 位序与 `0x00108000` opcode；理解 `lu12i.w` 的 20 位立即数为什么要配合 `addi.d` 的 12 位。
   - 前提：知道 LoongArch 基础指令固定 4 字节、小端（本章 6.1/6.3）。

2. **llvm-mc 的编码与 fixup 输出怎么读** — `llvm/docs/CommandGuide/llvm-mc.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`### End-user Options` 里的 `--show-encoding`、`--filetype=[asm,null,obj]`、`--triple=<string>`、`--mattr`，以及 `--assemble` 的说明。
   - 学到什么：补 6.3/6.9 的实验方法：`-show-encoding` 打印字节序列与 fixup 描述，`-filetype=obj` 才能让 `llvm-readobj -r` 看到 `.rela`。
   - 前提：跑过本章 6.3 的 `llvm-mc -show-encoding`。

3. **fixup 与 relocation 的测试语料** — `llvm/test/MC/LoongArch/Relocations/fixups.s`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：三条 RUN 行（`--show-encoding`、`--filetype=obj | llvm-objdump -d`、`--filetype=obj | llvm-readobj -r`），以及 `%abs_hi20`/`%abs_lo12`/`%b16`/`%b21` 与普通 `b`/`bl` 的 b26 各自的 `CHECK-FIXUP`/`CHECK-INSTR`/`CHECK-REL`。
   - 学到什么：把 6.4.1 的 fixup 表变成可对照输出；理解“汇编期能解析的 fixup 不产生 `R_LARCH_*`”，以及分支 fixup 的 4 字节对齐与范围检查。
   - 前提：知道 `%abs_hi20` 等 specifier 的语义（本章 6.4）。

4. **链接器怎么消费这些重定位** — `lld/ELF/Arch/LoongArch.cpp`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`relaxable()`（约 1105 行）、`relaxTlsLe()`（约 1263 行），以及 `relax()` 里 `R_LARCH_TLS_LE_HI20_R`/`ADD_R`/`LO12_R` 的 case（约 1290–1385 行）。
   - 学到什么：补本章停在 `.rela.text` 之后的半程：`R_LARCH_*` 如何被 lld 判定为可 relax 的三指令序列，以及 `tlsLeRelaxedAt` 如何修 Ch 00 的 hint-first bug。
   - 前提：读过本章 6.8 与 Ch 00 的 0.4；知道 TLS LE 三指令序列。

## 6.14 下一步

下一章看 MC 层的另外三个入口：`AsmParser`（文本 → `MCInst`）、`Disassembler`（字节 → `MCInst`）和 `InstPrinter`（`MCInst` → 文本），并用 `llvm-mc -disassemble` 做往返。
