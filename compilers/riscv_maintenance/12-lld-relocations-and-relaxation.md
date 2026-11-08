# Ch 12 — LLD 的 RISC-V 重定位与 relaxation

> **目标**：能读一份 `.rela.text` 说出每一条 `R_RISCV_*` 在做什么；能解释 `%hi/%lo` 的 `0x800` 进位；能说清 LLD 的 relaxation 状态机为什么需要"多趟收敛"，以及 GP relaxation 为什么需要两个额外前提。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，HEAD `0fc5afa04c23`，`ld.lld` 版本含同一 commit。

> **前置**：Ch 06（MC 层的 fixup 与 `MCExpr`）、Ch 11（code model、TLS、`gp`）。本章讲链接器侧：同一个 `.o` 在链接时还能被改多少。

---

## 12.1 为什么维护者要读链接器

RISC-V 是少数"链接器会重写指令"的架构。原因很实际：

- 函数调用默认 `auipc+jalr`（8 字节），但如果目标在 ±1 MiB 内，可以缩成 `jal`（4 字节）；
- `lui+addi` 访存（8 字节），如果符号在 `gp` ±2 KiB 内，可以缩成一条 `addi/ld`（4 字节）；
- `.p2align` 产生的填充，在压缩指令混排后可能多出无用的字节，链接器要回收。

这些改写发生在**重定位已经解析、地址已经分配**之后，所以链接器必须在"改指令 → 地址变化 → 又可能有新的可改指令"之间迭代到不动点。这就是 relaxation。

`lld/ELF/Arch/RISCV.cpp` 是本课里唯一一个"必须读状态机"的文件（1,678 行），其中：

| 部分 | 行数区间 | 作用 |
|---|---|---|
| 位操作助手 | 105–130 | `hi20`/`lo12`/`setLO12_I`/`setLO12_S` |
| `RISCV` 构造与 `calcEFlags` | 131–191 | `e_flags` 合并 |
| `getRelExpr` | 319–352 | 重定位 → 表达式 |
| `scanSectionImpl` | 354–515 | 第一遍扫描，建 `Relocation` 表 |
| `relocate` / `relocateAlloc` | 523–869 | 把值写回指令 |
| `relaxCall`/`relaxTlsLe`/`relaxHi20Lo12`/`relax` | 933–1142 | 每一趟的改写规则 |
| `relaxOnce` | 1143–1161 | 一趟的入口与收敛判断 |
| `synthesizeAlign*`/`finalizeRelax` | 1163–1300 | `.p2align` 的填充回收 |

维护者遇到的 bug 大多落在两处：`relocate` 的位拼接写错（某个 `R_RISCV_*` 的 imm 位置不对），或者 `relax` 的状态判断漏了一种组合（比如 `R_RISCV_ALIGN` 与 `R_RISCV_CALL` 相邻时）。

---

## 12.2 先建立直觉：三个"地址还没定"的时刻

```
  编译/汇编期               链接期（第一遍）            链接期（relax 多趟）
  ─────────────            ────────────────            ────────────────────
  auipc ra, %pcrel_hi(f)   R_RISCV_CALL_PLT f          f 在 ±1MiB?
  jalr  ra, %pcrel_lo(p)   R_RISCV_RELAX              ├─ 是 → 改写成 jal（省 4B）
                                                     └─ 否 → 保留 8B
        │                        │                          │
        ▼                        ▼                          ▼
   写进 .o 的                解析符号 → 填值           重算地址 → 再检查
   .rela.text               （relocate）              （relaxOnce 循环）
```

三个关键事实：

1. **`.o` 里的指令是"半成品"**：`auipc` 的 imm 是 0，真正的值在 `.rela.text` 里。
2. **`R_RISCV_RELAX` 是一个"许可证"**：只有紧跟其后的重定位才允许被改写。没有它，链接器不能碰这条指令。
3. **relaxation 会改变地址，因此必须迭代**：省掉 4 字节会让后面所有符号前移，可能让另一条 `call` 刚好进入范围（也可能让已经改好的又超出范围）。

---

## 12.3 实测：一份 `.rela.text` 长什么样

```asm
.text
.globl _start
_start:
  call ext
  lui a0, %hi(g)
  ld a0, %lo(g)(a0)
  ret
.section .sdata,"aw"
.globl g
g: .word 7
```

```bash
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -mattr=+m,+a,+c -o a.o a.s
$T/bin/llvm-readobj -r a.o
```

```
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_CALL_PLT ext 0x0
    0x8 R_RISCV_HI20 g 0x0
    0xC R_RISCV_LO12_I g 0x0
  }
]
```

三条重定位，三种语义：

| 重定位 | 偏移 | 含义 |
|---|---|---|
| `R_RISCV_CALL_PLT` | 0x0 | 覆盖 8 字节的 `auipc+jalr` 对，目标走 PLT |
| `R_RISCV_HI20` | 0x8 | `lui` 的 20 位高位（带 `0x800` 进位） |
| `R_RISCV_LO12_I` | 0xC | I 型指令的 12 位低位（`ld` 的 imm） |

注意 `R_RISCV_CALL_PLT` 只占一个表项却管两条指令——这是 RISC-V 特有的"成对重定位"，也是 `writePlt`/`relaxCall` 都要按 8 字节读的原因（`relaxCall` 里 `read64le`）。

加上 `+relax` 后，每条重定位后面会多一条许可证：

```bash
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -mattr=+m,+a,+c,+relax -o ar.o a.s
$T/bin/llvm-readobj -r ar.o
```

```
    0x0 R_RISCV_CALL_PLT ext 0x0
    0x0 R_RISCV_RELAX - 0x0
    0x8 R_RISCV_HI20 g 0x0
    0x8 R_RISCV_RELAX - 0x0
    0xC R_RISCV_LO12_I g 0x0
    0xC R_RISCV_RELAX - 0x0
```

`+relax` 是 Subtarget feature（`RISCVFeatures.td:2031`），clang 的 `-mrelax` 打开它，`-mno-relax` 关掉。**它只影响是否发出 `R_RISCV_RELAX`，不影响指令本身**。

---

## 12.4 `getRelExpr`：重定位先变成"表达式"

`RISCV::getRelExpr`（319）把 ELF 的重定位类型映射成 LLD 内部的 `RelExpr`：

```cpp
RelExpr RISCV::getRelExpr(const RelType type, const Symbol &s,
                          const uint8_t *loc) const {
  switch (type) {
  case R_RISCV_32:
  case R_RISCV_64:
    return R_ABS;
  case R_RISCV_ADD8: ... case R_RISCV_SUB64:
    return RE_RISCV_ADD;
  case R_RISCV_32_PCREL:
    return R_PC;
  case R_RISCV_SET_ULEB128:
  case R_RISCV_SUB_ULEB128:
    return RE_RISCV_LEB128;
  default:
    Err(ctx) << getErrorLoc(ctx, loc) << "unknown relocation (" << type.v
             << ") against symbol " << &s;
    return R_NONE;
  }
}
```

只有四类：绝对、加减、PC 相对、ULEB128。真正的分流在 `scanSectionImpl`（354）：那里按类型决定走 `R_ABS`、`R_GOT_PC`、`R_TPLREL`，还是走快捷路径 `processR_PC`/`processR_PLT_PC`。

`scanSectionImpl` 的注释值得抄下来：

> Relocation types that only need a RelExpr set `expr` and break out of the switch to reach `rs.process()`. Types that need special handling (fast-path helpers, TLS) call a handler and use `continue`.

翻译：**能复用通用机制的只设表达式；需要特殊处理的（PC 相对、PLT、TLS LE 检查）直接调用 handler 然后 `continue`**。读这段代码时，先看哪些 `continue` 了，剩下的才是"标准路径"。

---

## 12.5 `relocate`：`%hi/%lo` 的 `0x800` 是怎么来的

`hi20`/`lo12` 只有两行（105–106）：

```cpp
static uint32_t hi20(uint32_t val) { return (val + 0x800) >> 12; }
static uint32_t lo12(uint32_t val) { return val & 4095; }
```

`%lo` 是**有符号** 12 位（−2048..2047），而 `val & 4095` 得到的是 0..4095 的无符号值。两者靠 `+0x800` 的进位对齐：如果低 12 位 ≥ 0x800，就向上进一位，让 `lo` 变成负数。手算一遍真实例子：

```asm
_start:
  lui a0, %hi(g)
  lw  a0, %lo(g)(a0)
```

链接后 `g = 0x10ef8`：

```asm
   10000: 00011537     	lui	a0, 0x11
   10004: ef852503     	lw	a0, -0x108(a0)
```

验证：

- `hi20(0x10ef8) = (0x10ef8 + 0x800) >> 12 = 0x116f8 >> 12 = 0x11`
- `lo12(0x10ef8) = 0xef8`，作为有符号 12 位是 `0xef8 - 0x1000 = -0x108`
- `0x11 << 12 = 0x11000`，`0x11000 - 0x108 = 0x10ef8` ✓

如果 `relocate` 里少加那个 `0x800`，`lui` 会变成 `0x10`、`lo` 变成 `0xef8`（正数 3832）——地址就差了 4096。这类 bug 在 `lui`/`addi` 组合、`%pcrel_hi`/`%pcrel_lo` 组合里都会出现，且只在"低 12 位 ≥ 0x800"的符号上复现。**写测试时必须覆盖这个边界**。

位拼接本身由 `setLO12_I`/`setLO12_S`（123–129）完成：

```cpp
static uint32_t setLO12_I(uint32_t insn, uint32_t imm) {
  return (insn & 0xfffff) | ((imm & 0xfff) << 20);
}
static uint32_t setLO12_S(uint32_t insn, uint32_t imm) {
  return (insn & 0x1fff07f) | (((imm >> 5) & 0x7f) << 25) | ((imm & 0x1f) << 7);
}
```

I 型是高 12 位；S 型拆成 `imm[11:5]` 和 `imm[4:0]` 两段。写错掩码是"编码正确但地址错"的经典来源。

---

## 12.6 relaxation 状态机

`relaxOnce`（1143）是入口，内部对每个可 relax 的 section 调 `relax`（1035）。`relax` 的骨架：

```cpp
std::fill_n(aux.relocTypes.get(), relocs.size(), R_RISCV_NONE);
aux.writes.clear();
for (auto [i, r] : llvm::enumerate(relocs)) {
  const uint64_t loc = secAddr + r.offset - delta;   // 已累计删除的字节
  uint32_t &cur = aux.relocDeltas[i], remove = 0;
  switch (r.type) {
  case R_RISCV_ALIGN:            { /* 回收对齐填充 */ }
  case R_RISCV_CALL: case R_RISCV_CALL_PLT: { /* 8B → 4B */ }
  case R_RISCV_TPREL_*:          { /* TLS LE 缩短 */ }
  case R_RISCV_HI20: case R_RISCV_LO12_*: { /* gp/x0 折叠 */ }
  ...
  }
  delta += remove;
  if (delta != cur) { cur = delta; changed = true; }
}
sec.bytesDropped = delta;
return changed;
```

四个数据结构要分清：

| 名字 | 含义 |
|---|---|
| `relocDeltas[i]` | 到第 i 条重定位为止，累计删掉多少字节 |
| `relocTypes[i]` | 这一条重定位改写后的新类型（`R_RISCV_NONE` 表示不动） |
| `aux.writes` | 要直接写进 section 的字节（如 `jal` 的编码） |
| `SymbolAnchor` | 符号在 section 内的位置，relax 后要跟着前移 |

`initSymbolAnchors`（870）在 relax 之前把符号位置记录下来，`relax` 里边扫边更新 `value`/`size`，最后 `finalizeRelax`（1278）收尾。**这套机制存在的唯一理由**：relax 改的是"已经分配好地址的 section 内容"，符号和调试信息必须同步。

收敛判断在 `Writer.cpp` 的 `finalizeAddressDependentContent`：

```cpp
while (!ctx.arg.relocatable) {
  bool changed = ctx.target->needsThunks
                     ? tc.createThunks(pass, ctx.outputSections)
                     : ctx.target->relaxOnce(pass);
  ...
  if (changed && pass >= 30) {
    Err(ctx) << "address assignment did not converge";
    break;
  }
}
```

`pass` 会一路涨。`relax` 里那句 `remove = pass < 4 ? 6 : cur - delta;` 是为了防止"删了又加"的振荡——前几趟最多删 6 字节（`c.j` 的情况），之后沿用上一趟的值。

---

## 12.7 三类 relaxation 的实测

### 12.7.1 `call` → `jal`

```asm
_start:
  call near
  ret
near:
  ret
```

```bash
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -mattr=+c,+relax -o b.o b.s
$T/bin/ld.lld -T link.ld --relax    -o b.relax.elf   b.o
$T/bin/ld.lld -T link.ld --no-relax -o b.norelax.elf b.o
```

```asm
# --relax
   10000: 00e000ef     	jal	0x1000e <near>
   10004: 8082         	ret

# --no-relax
   10000: 00000097     	auipc	ra, 0x0
   10004: 012080e7     	jalr	0x12(ra) <near>
   10008: 8082         	ret
```

判定逻辑在 `relaxCall`（933）：

```cpp
if (remove >= 6 && rvc && isInt<12>(displace) && rd == X_X0) {
  ... writes.push_back(0xa001);  // c.j
} else if (remove >= 4 && isInt<21>(displace)) {
  ... writes.push_back(0x6f | rd << 7);  // jal
  remove = 4;
} else {
  remove = 0;
}
```

三种目标形态：`c.j`（2 字节，仅 `rd == x0` 且范围 ±2 KiB）、`jal`（4 字节，±1 MiB）、`c.jal`（RV32C 专用）、否则不动。注意 `rd` 是从 `auipc` 那半个字里抠出来的（`extractBits(insnPair, 32+11, 32+7)`）——`auipc` 的 `rd` 就是 `jalr` 的 `rd`，也是 `jal` 的 `rd`。

### 12.7.2 `HI20/LO12` → `gp` 相对

`relaxHi20Lo12`（992）有两条路径：

```cpp
// 1) 地址本身能放进 12 位 → 折叠成 x0 相对
if (isInt<12>(r.sym->getVA(ctx, r.addend))) { ... INTERNAL_R_RISCV_X0REL_I ... }

// 2) 没有 gp → 放弃
const Defined *gp = ctx.sym.riscvGlobalPointer;
if (!gp) return;

// 3) 相对 gp 在 ±2 KiB → 折叠成 gp 相对
if (!isInt<12>(r.sym->getVA(ctx, r.addend) - gp->getVA(ctx))) return;
```

实测（`.sdata` 里放一个 `g`，并让对象引用 `__global_pointer$`）：

```asm
_start:
  call near
  lui a0, %hi(g)
  ld  a0, %lo(g)(a0)
  la  a2, __global_pointer$
  ret
```

```asm
# --relax --relax-gp
   10000: 012000ef     	jal	0x10012 <near>
   10004: 8001b503     	ld	a0, -0x800(gp)

# --relax（不给 --relax-gp）
   10000: 016000ef     	jal	0x10016 <near>
   10004: 00010537     	lui	a0, 0x10
   10008: 0d853503     	ld	a0, 0xd8(a0)
```

链接后 `g = 0x100d8`、`__global_pointer$ = 0x108d8`，`g - gp = -0x800`，正好卡在 `isInt<12>` 的下边界——这也解释了为什么 `gp` 被定义成 `.sdata + 0x800`（`Writer.cpp:1856-1860` 的注释："RISC-V's gp can address +/- 2 KiB, set it to .sdata + 0x800"）。

### 12.7.3 `R_RISCV_ALIGN`

`.p2align` 在汇编期会插入 `R_RISCV_ALIGN`，告诉链接器"这里需要对齐，但填充字节数取决于前面的指令有没有被压缩/缩短"。`relax` 里的处理是：

```cpp
case R_RISCV_ALIGN: {
  const uint64_t nextLoc = loc + r.addend;
  const uint64_t align = PowerOf2Ceil(r.addend + 2);
  // All bytes beyond the alignment boundary should be removed.
  remove = nextLoc - ((loc + align - 1) & -align);
  if (LLVM_UNLIKELY(static_cast<int32_t>(remove) < 0)) {
    Err(ctx) << ... << "insufficient padding bytes for " << r.type << ": "
             << r.addend << " bytes available for requested alignment of "
             << align << " bytes";
    remove = 0;
  }
  break;
}
```

`remove` 为负说明汇编器给的填充不够——这通常意味着前一条指令被手工改短了却没留余量。报错文本就是这个 bug 的指纹。

---

## 12.8 PLT 与 GOT

`call ext`（`ext` 未定义、要跨模块）会走 PLT。`writePltHeader`(242) 与 `writePlt`(282) 生成 RISC-V 的 PLT 桩，`writeGotPlt`(226) 填 GOT 项。

实测一个共享库：

```bash
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -mattr=+c,+relax -o f.o f.s
$T/bin/ld.lld -shared --relax -soname libf.so -o libf.so f.o
$T/bin/llvm-objdump -d --section=.plt libf.so
```

```
0000000000001300 <.plt>:
    1300: 00002397     	auipc	t2, 0x2
    1304: 41c30333     	sub	t1, t1, t3
    1308: 1383be03     	ld	t3, 0x138(t2)
    130c: fd430313     	addi	t1, t1, -0x2c
    1310: 13838293     	addi	t0, t2, 0x138
    1314: 00135313     	srli	t1, t1, 0x1
    1318: 0082b283     	ld	t0, 0x8(t0)
    131c: 000e0067     	jr	t3
```

配套的重定位：

```
Relocation section '.rela.plt' ... R_RISCV_JUMP_SLOT  ext + 0
Relocation section '.rela.dyn' ... R_RISCV_64         gd  + 0
```

要点：

- `.plt` 每项 16 字节（`writePlt` 里 `ebreak` 相关布局），header 32 字节；
- `R_RISCV_JUMP_SLOT` 是 `ext` 的 GOT 项，第一次调用后由动态链接器填充；
- `lga a0, gd`（全局数据）生成 `R_RISCV_GOT_HI20` + `R_RISCV_PCREL_LO12_I` 一对，落到 `.rela.dyn` 的 `R_RISCV_64`。

顺带一个**易错点**：`%got_pcrel_hi` 只能配 `auipc`，不能配 `lui`。直接写 `lui a0, %got_pcrel_hi(x)` 会被汇编器拒绝：

```
error: operand must be a symbol with %hi/%tprel_hi specifier or an integer in the range [0, 1048575]
```

这也是为什么 MC 层提供了 `lga` 伪指令——它负责生成正确的一对。

---

## 12.9 源码落点

- **`lld/ELF/Arch/RISCV.cpp`**（1,678 行）
  - 105–130：`hi20`/`lo12`/`setLO12_I`/`setLO12_S`，全章最该背下来的 25 行。
  - 131–191：`calcEFlags`，输入文件的 `e_flags` 如何合并、冲突如何报错。
  - 319–352：`getRelExpr`，四类表达式。
  - 354–515：`scanSectionImpl`，第一遍扫描；注意哪些分支 `continue`。
  - 523–734：`relocate`，把值写回指令的主 switch。
  - 783–869：`relocateAlloc`，`alloc` 段里的重定位。
  - 933–1034：`relaxCall`/`relaxTlsLe`/`relaxHi20Lo12`，三类改写。
  - 1035–1142：`relax`，状态机主体。
  - 1143–1161：`relaxOnce`，一趟的入口。
  - 1163–1300：`synthesizeAlign*`/`finalizeRelax`，`.p2align` 与符号锚点收尾。

- **`lld/ELF/Writer.cpp`**
  - 1500–1545：`finalizeAddressDependentContent`，relax 迭代循环与 30 趟上限。
  - 1850–1870：`__global_pointer$` 的定义与 `riscvGlobalPointer` 的赋值。

- **`lld/ELF/Config.h`**
  - 403 附近：`relaxGP`；`--relax-gp` 默认关闭（`Driver.cpp:1563`）。

- **测试**
  - `lld/test/ELF/riscv-relax-call.s`、`riscv-relax-call2.s`：`call → jal` 的正面用例。
  - `lld/test/ELF/riscv-relax-hi20-lo12.s`、`riscv-relax-hi20-lo12-pie.s`：GP relaxation（`--relax-gp` 出现在 RUN 行里）。
  - `lld/test/ELF/riscv-relax-align.s`、`riscv-relax-align-rvc.s`：`R_RISCV_ALIGN` 的填充回收。
  - `lld/test/ELF/riscv-tls-le.s`/`-ie.s`/`-ld.s`/`-gd.s`：四种 TLS 模型。
  - `lld/test/ELF/riscv-plt.s`、`riscv-reloc-got.s`：PLT/GOT 布局。

---

## 12.10 可跑实验

### 实验 1：看清 `+relax` 只加许可证

```bash
cd /tmp && printf '.text\n.globl _start\n_start:\n  call ext\n  ret\n' > r.s
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -o r0.o r.s
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -mattr=+relax -o r1.o r.s
$T/bin/llvm-objdump -d r0.o | tail -5
$T/bin/llvm-objdump -d r1.o | tail -5
$T/bin/llvm-readobj -r r0.o; $T/bin/llvm-readobj -r r1.o
```

两次反汇编应完全一致；差别只在 `.rela.text` 里多出的 `R_RISCV_RELAX`。

### 实验 2：`call` 收缩

用 12.7.1 的 `b.s` 与两份 ELF，`llvm-objdump -d` 对比，再看 `.text` 的 sh_size：

```bash
$T/bin/llvm-readelf -S b.relax.elf   | grep '\.text'
$T/bin/llvm-readelf -S b.norelax.elf | grep '\.text'
```

实测：

```text
  [ 1] .text   PROGBITS  0000000000010000 001000 000008 00  AX  0   0  2    # --relax
  [ 1] .text   PROGBITS  0000000000010000 001000 00000c 00  AX  0   0  2    # --no-relax
```

`0x0c → 0x08`，正好是 `auipc+jalr`（8 字节）换成 `jal`（4 字节）省下的 **4 字节**。注意 `ls -l` 看文件总大小通常**看不出来**（两份都是 4792 字节）——段对齐与页填充会把 4 字节吃掉。判断 relaxation 是否生效要看 `.text` 的 `sh_size` 或反汇编，不要看文件大小。

### 实验 3：`0x800` 边界

```bash
cd /tmp && cat > h.s <<'EOF'
.text
.globl _start
_start:
  lui a0, %hi(g)
  lw a0, %lo(g)(a0)
  ret
.section .sdata,"aw"
.space 0xee8
.globl g
g: .word 7
EOF
$T/bin/llvm-mc -triple=riscv64 -filetype=obj -o h.o h.s
$T/bin/ld.lld -T <你的脚本> --no-relax -o h.elf h.o
$T/bin/llvm-objdump -d h.elf | sed -n '/<_start>/,/^$/p'
$T/bin/llvm-readelf -s h.elf | grep ' g$'
```

把 `.space 0xee8` 改成 `0xce8`，观察 `lui` 的立即数与 `lo` 的符号是否翻转。

### 实验 4：GP relaxation 的两个前提

分别用 `--relax` 与 `--relax --relax-gp` 链接 12.7.2 的 `e.s`，再把这个文件里的 `la a2, __global_pointer$` 删掉重跑——你会看到即使给了 `--relax-gp` 也不会收缩，因为 `addOptionalRegular` 只解析"已经存在的"符号引用（`Writer.cpp:126-137`）。

---

## 12.11 踩雷集锦

1. **`%hi/%lo` 忘了 `0x800`。** 只在低 12 位 ≥ 0x800 的符号上复现，测试必须专门构造这个边界。
2. **`%got_pcrel_hi` 配了 `lui`。** 汇编器直接报错；GOT 访问必须是 `auipc` + `%pcrel_lo`。
3. **`R_RISCV_CALL` 当成 4 字节处理。** 它覆盖 `auipc+jalr` 两条指令；`relaxCall` 用 `read64le` 读 8 字节，写回时也要成对处理。
4. **`relaxable` 检查漏了。** 只有紧跟 `R_RISCV_RELAX` 的重定位才能改；否则会改到用户明确要求"精确汇编"的代码。
5. **`--relax-gp` 以为默认开。** 默认是 `false`（`Driver.cpp:1563`），且还需要 `__global_pointer$` 被引用。
6. **`.p2align` 的填充不够。** 会报 `insufficient padding bytes for R_RISCV_ALIGN`；手工改短指令时要留余量。
7. **relax 不收敛。** `pass >= 30` 会报 `address assignment did not converge`；通常是两类改写互相触发。
8. **忘了 `SymbolAnchor` 的更新。** relax 改变 section 大小后，符号 `st_value`/`st_size` 必须同步，否则调试信息和 `--gc-sections` 都会错。

---

## 12.12 本章重点整理

- `.o` 里的指令是半成品，真正的值在 `.rela.text`；`R_RISCV_CALL_PLT` 一个表项管 8 字节。
- `R_RISCV_RELAX` 是"允许改写"的许可证，由 `+relax` 生成，`--no-relax` 可全局关掉。
- `hi20(val) = (val + 0x800) >> 12`、`lo12(val) = val & 4095`；`+0x800` 是为了让 `%lo` 成为有符号 12 位。
- relaxation 三类：`call → jal/c.j`、`HI20/LO12 → gp/x0 相对`、`R_RISCV_ALIGN` 回收填充。
- 状态机由 `relocDeltas`/`relocTypes`/`writes`/`SymbolAnchor` 四个结构驱动，迭代到不动点（上限 30 趟）。
- GP relaxation 需要 `--relax-gp` **且** `__global_pointer$` 被引用；`gp = .sdata + 0x800`。
- PLT 由 `writePltHeader`/`writePlt` 生成，动态重定位是 `R_RISCV_JUMP_SLOT`；GOT 数据访问是 `R_RISCV_GOT_HI20` + `R_RISCV_PCREL_LO12_I`。

---

## 12.13 自我检核

- [ ] 能背出 `hi20`/`lo12` 两个公式，并手算 `0x10ef8`
- [ ] 能解释 `R_RISCV_CALL_PLT` 为什么覆盖 8 字节
- [ ] 能说出 `R_RISCV_RELAX` 的作用，以及如何关闭它
- [ ] 能列出三类 relaxation 各自的判定条件
- [ ] 能解释 GP relaxation 的两个前提，以及 `gp` 为什么是 `.sdata + 0x800`
- [ ] 能说出 relaxation 需要多趟收敛的原因
- [ ] 能解释 `R_RISCV_ALIGN` 的 `remove` 为什么可能为负
- [ ] 能画出 `.plt` header 的前 4 条指令并说明各自作用

---

## 12.14 延伸阅读

- **`lld/ELF/Arch/RISCV.cpp`**（本树，1,678 行）
  - 读哪里：105–130、319–352、933–1142。
  - 为什么读：RISC-V 链接期行为的全部实现都在这里，且注释解释了不少"为什么"。
- **`lld/ELF/Writer.cpp`**
  - 读哪里：1500–1545（relax 循环）、1850–1870（`__global_pointer$`）。
  - 为什么读：relax 不是孤立模块，它嵌在地址分配的不动点循环里。
- **`llvm/docs/RISCVUsage.md`**（本树，808 行）
  - 读哪里：706 行起 "Global Pointer (GP) Relaxation and the Small Data Limit"。
  - 为什么读：用户视角的开关与默认值。
- **`lld/test/ELF/riscv-relax-*.s`**
  - 读哪里：每个 RUN 行的 `--relax`/`--no-relax` 对照与 CHECK。
  - 为什么读：改 relaxation 时的第一道回归网。
- **`llvm/test/MC/RISCV/linker-relaxation.s`**
  - 读哪里：汇编器发出的 `R_RISCV_RELAX` 组合。
  - 为什么读：MC 与 LLD 的契约在这里显式写出。

---

→ 下一章：[Ch 13 — 合法化](./13-legalization.md)
