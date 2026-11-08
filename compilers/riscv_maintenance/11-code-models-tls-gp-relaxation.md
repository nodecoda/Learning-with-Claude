# Ch 11 — 代码模型、TLS 与 GP relaxation

> **目标**：看到一个全局变量/函数地址时，能判断它为什么被编译成 `lui+addi`、`auipc+addi` 或 GOT load；能说出 RISC-V 四种 TLS 模型的指令序列与重定位；能解释 `gp` 为什么在 `getReservedRegs` 里被永久保留，以及 `-msmall-data-limit` 到底改变了什么。

> **环境**：同 Ch 00。源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`；`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。本章命令在 `/tmp/ch11` 下执行，源码树只读。

> **前置**：Ch 05（`getReservedRegs`、`gp`/`tp`）、Ch 06（MC fixup 与重定位）、Ch 12（链接器如何消费 `R_RISCV_RELAX`）。本章只讲编译器侧：**选哪种寻址模式、发哪条重定位**。

---

## 11.1 为什么维护者要读“地址从哪来”

同一句 `return &g;` 在 RISC-V 上可能生成三种序列：

- `lui a0, %hi(g); addi a0, a0, %lo(g)`——非 PIC、`medlow`；
- `auipc a0, %pcrel_hi(g); addi a0, a0, %pcrel_lo(...)`——非 PIC `medany`，或 PIC 下的局部符号；
- `auipc a0, %got_pcrel_hi(g); ld a0, %pcrel_lo(...)(a0)`——PIC 下的可抢占符号。

维护者常见的错误不是“指令写错”，而是**在错误的 code model / PIC 组合下断言了错误的序列**：改了 `getAddr` 却只跑默认 PIE 测试；把 `%got_pcrel_hi` 配到 `lui`；看到 `-msmall-data-limit` 后汇编仍是 `lui+addi` 就以为开关没生效；在 `-fno-pic` 下写 `-ftls-model=local-dynamic` 却得到 initial-exec。本章把这些拆成四层：code model、PIC、TLS、GP。

---

## 11.2 先建立直觉：一个符号的地址决策树

```
                    ┌──────────────────────────────┐
                    │ 这是 TLS 符号吗？             │
                    └──────────────┬───────────────┘
                     yes           │ no
          ┌────────────────────────┴────────────────────────┐
          ▼                                                 ▼
  TLS lowering（11.8）                          ┌──────────────────────┐
  local-exec / initial-exec /                   │ 当前是 PIC 吗？       │
  local-dynamic / general-dynamic               └──────────┬───────────┘
                                                yes        │ no
                                    ┌──────────────────────┴──────────────────────┐
                                    ▼                                             ▼
                        ┌───────────────────────┐                   ┌──────────────────────────┐
                        │ 符号是 dso_local 吗？  │                   │ CodeModel 是什么？        │
                        └──────────┬────────────┘                   └──────────┬───────────────┘
                          yes      │ no                            Small       │ Medium      Large
                           ▼       ▼                                     ▼     ▼            ▼
                   auipc pcrel  auipc got_pcrel                  lui+addi  auipc+addi   常量池+auipc/ld
                   +addi        +ld                             Pseudo    PseudoLLA    （64 位地址）
                   PseudoLLA    PseudoLGA                       MovAddr
```

三个要点：编译器只看 **PIC、dso_local、code model**；`gp` 不在这棵树里；TLS 先选模型再走独立 lowering。所有 PC 相对成对序列都遵守同一规则：`auipc` 上挂标签，`%pcrel_lo` 引用标签而不是符号。

---

## 11.3 code model：medlow/medany/large 到底改了什么

Clang 的 `-mcmodel` 在 driver 里先翻译成 LLVM 名字（`clang/lib/Driver/ToolChains/CommonArgs.cpp:3122-3132`）：

| clang 选项 | LLVM `CodeModel::Model` | 非 PIC 取址 | 约束 |
|---|---|---|---|
| `medlow` | `Small` | `lui %hi` + `addi %lo` | 绝对地址落在 0 两侧 ±2 GiB |
| `medany` | `Medium` | `auipc %pcrel_hi` + `addi %pcrel_lo` | 位移在 ±2 GiB |
| `large` | `Large` | 常量池存 64 位地址，再 `auipc+ld` | 仅 RV64；不能与 PIC 同用 |
| 不指定 | `Small`（`RISCVTargetMachine.cpp:109`） | 同 `medlow` | — |

**±2 GiB 的含义。** `medlow` 的 `lui+addi` 物化的是符号扩展的 32 位值；链接器检查 `hi20` 这个 20 位有符号域 `[-524288, 524287]`，乘 4096 即 `[-2 GiB, 2 GiB)`。`medany` 物化的是 PC 相对位移，只要符号与指令距离在 ±2 GiB 内，代码段放在哪里都行。

### 实测：同一个 `g`，medlow 与 medany
```bash
cd /tmp/ch11 && cat > addr.c <<'EOF'
int g;
int *addr_g(void) { return &g; }
EOF
for CM in medlow medany; do
  echo "===== -fno-pic -mcmodel=$CM"
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 -fno-pic -mcmodel=$CM -S -o - addr.c | sed -n '/addr_g:/,/Lfunc_end/p'
done
```

```
===== -fno-pic -mcmodel=medlow
	lui	a0, %hi(g)
	addi	a0, a0, %lo(g)
	ret
===== -fno-pic -mcmodel=medany
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(g)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)
	ret
```

**默认值会骗人。** 去掉 `-fno-pic`，两者都变成 `auipc %pcrel_hi`：`getAddr` 先判断 `isPositionIndependent()`（`RISCVISelLowering.cpp:10189`），PIC 分支在 code model 的 `switch`（10211）之前。默认 PIE 下 `medlow`/`medany` 对这个例子完全一样。

### 实测：2 GiB 边界
```bash
cat > far.ld <<'EOF'
SECTIONS { . = 0x10000; .text : { *(.text*) }
           . = 0x7ffff000; .data : { *(.data*) *(.sdata*) } }
EOF
sed 's/0x7ffff000/0x80000000/' far.ld > far2.ld
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -mcmodel=medlow -c -o low.o addr.c
$T/bin/ld.lld -T far.ld  --no-relax -o low_2g.elf low.o
$T/bin/llvm-objdump -d low_2g.elf | sed -n '/<addr_g>/,/^$/p'
$T/bin/ld.lld -T far2.ld --no-relax -o low_2g1.elf low.o
```

```
# low_2g.elf（g = 0x7ffff000）
   1002c: 7ffff537     	lui	a0, 0x7ffff
# low_2g1.elf（g = 0x80000000）：链接失败
ld.lld: error: low.o:(function addr_g: .text+0x0): relocation R_RISCV_HI20 out of range:
        524288 is not in [-524288, 524287]; references 'g'
```

`0x7ffff000` 的 `hi20` 是 `0x7ffff`，刚好是上限；`0x80000000` 得到 `0x80000`，超一位就报错。同一地址用 `medany` 能过：

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -mcmodel=medany -c -o any.o addr.c
$T/bin/ld.lld -T far2.ld --no-relax -o any_2g.elf any.o
$T/bin/llvm-objdump -d any_2g.elf | sed -n '/<addr_g>/,/^$/p'
```

```
   1002c: 7fff0517     	auipc	a0, 0x7fff0
   10030: fd450513     	addi	a0, a0, -0x2c
```

`g = 0x80000000`，指令在 `0x1002c`，位移约 2 GiB − 64 KiB；再抬到 `0x80020000`，`R_RISCV_PCREL_HI20` 同样报 `524304` 超界（实测）。

### large：64 位地址放进常量池
```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -mcmodel=large -S -o - addr.c \
  | sed -n '/^\.LCPI0_0:/,/^\.Lfunc_end/p' | grep -vE '^\s*\.(size|globl|p2align|type|cfi)|^#|^\.Lfunc'
```

```
.LCPI0_0:
	.quad	g
addr_g:
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(.LCPI0_0)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ret
```

`g` 的 64 位地址写进 `.LCPI0_0`，代码只 PC 相对地拿常量池项；large 下常量池放进 `.text`（`RISCVTargetObjectFile.cpp:169-199`），所以取池本身仍在 ±2 GiB 内。`-fPIC -mcmodel=large` 被 driver 拒绝：`invalid argument '-mcmodel=large' not allowed with '-fpic'`。

---

## 11.4 `getAddr` 的四个出口

四个 lowering 入口都汇到 `RISCVTargetLowering::getAddr`（`RISCVISelLowering.cpp:10180`）。真实函数名是小写开头的 `lowerGlobalAddress`（10266）、`lowerBlockAddress`（10275）、`lowerConstantPool`（10282）、`lowerJumpTable`（10289）；`LowerOperation` 分派在 8384-8391。

```cpp
if (isPositionIndependent() || Subtarget.allowTaggedGlobals()) {   // 10189
  if (IsLocal && !Subtarget.allowTaggedGlobals())
    return DAG.getNode(RISCVISD::LLA, ...);       // PseudoLLA: pcrel
  return PseudoLGA;                              // PseudoLGA: GOT
}
switch (getTargetMachine().getCodeModel()) {
case CodeModel::Small:  // MO_HI + MO_LO，PseudoMovAddr
case CodeModel::Medium: // RISCVISD::LLA，PseudoLLA；extern_weak 例外走 GOT
case CodeModel::Large:  // getLargeGlobalAddress：常量池 + LLA
}
```

三个容易忽略的点：`lowerGlobalAddress` 把 `shouldAssumeDSOLocal(GV)` 作为 `IsLocal` 传入（10271）；`lowerBlockAddress`/`lowerConstantPool`/`lowerJumpTable` 用默认 `IsLocal=true`（`RISCVISelLowering.h:514-515`），所以 PIC 下常量池、跳转表永远走 `%pcrel_hi`；`Medium` 下只有 `extern_weak` 走 GOT（10233-10246），因为未定义弱符号可能是 0，而 0 不一定在 PC 的 ±2 GiB 内。

---

## 11.5 `PseudoMovAddr` 与 `PseudoLA*` 家族

| 伪指令 | 定义 | 展开 | 用途 |
|---|---|---|---|
| `PseudoMovAddr` | `RISCVInstrInfo.td:1662` | post-RA `LUI+ADDI`（`RISCVExpandPseudoPostRA.cpp:74,102-115`） | 非 PIC `Small`，可重物化 |
| `PseudoLLA` | `:2026` | pre-RA `AUIPC+ADDI`（`RISCVExpandPseudoPreRA.cpp:83,129-131`） | pcrel 本地地址 |
| `PseudoLGA` | `:2041` | pre-RA `AUIPC+LD/LW`（`:85,134-137`） | GOT 间接 |
| `PseudoLA` | `:2046` | 汇编器 `la`，按 PIC 选 LLA/LGA（`RISCVAsmParser.cpp:3814-3829`） | 手写汇编 |
| `PseudoLA_TLS_IE` | `:2057` | `AUIPC+LD`，`MO_TLS_GOT_HI` | initial-exec |
| `PseudoLA_TLS_GD` | `:2062` | `AUIPC+ADDI`，`MO_TLS_GD_HI` | general/local-dynamic |
| `PseudoLA_TLSDESC` | `:2070` | `AUIPC+LW+ADDI+JALR` | TLSDESC |

`PseudoMovAddr` 是**可重物化**的（`isReMaterializable = 1`），所以 post-RA 才展开；`PseudoLLA/LGA` 需要一个临时虚拟寄存器装 `auipc` 结果，所以 pre-RA 展开（`expandAuipcInstPair`，`RISCVExpandPseudoPreRA.cpp:98-127`）。

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -mcmodel=medlow -S -emit-llvm -o low.ll addr.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c \
  -stop-after=finalize-isel -simplify-mir low.ll -o - | grep Pseudo
```

```
%0:gpr = PseudoMovAddr target-flags(riscv-hi) @g, target-flags(riscv-lo) @g
```

PIC 版本要显式 `-relocation-model=pic`（`llc` 默认 static）：

```bash
printf 'int g; static int s;\nint *addr_g(void){return &g;}\nint *addr_s(void){return &s;}\n' > pic.c
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fPIC -S -emit-llvm -o pic.ll pic.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -relocation-model=pic \
  -stop-after=finalize-isel -simplify-mir pic.ll -o - | grep -E "name:|PseudoL"
```

```
name:            addr_g
    %0:gpr = PseudoLGA @g :: (dereferenceable invariant load (s64) from got)
name:            addr_s
    %0:gpr = PseudoLLA @s
```

---

## 11.6 TargetFlags：`riscv-hi` 是 MIR 拼写，不是枚举名

真实枚举在 `RISCVBaseInfo.h:469-493`，命名空间 `RISCVII`：

| 枚举 | 值 | MIR 字符串（`RISCVInstrInfo.cpp:3706-3722`） | 含义 |
|---|---|---|---|
| `MO_LO` | 3 | `riscv-lo` | 低 12 位，I/S 型 |
| `MO_HI` | 4 | `riscv-hi` | 高 20 位，`lui` |
| `MO_PCREL_LO` | 5 | `riscv-pcrel-lo` | 配对 AUIPC 标签的低 12 位 |
| `MO_PCREL_HI` | 6 | `riscv-pcrel-hi` | `auipc` 的 PC 相对高 20 位 |
| `MO_GOT_HI` | 7 | `riscv-got-hi` | GOT 项相对 `auipc` |
| `MO_TPREL_LO/HI/ADD` | 8/9/10 | `riscv-tprel-*` | TLS local-exec |
| `MO_TLS_GOT_HI` | 11 | `riscv-tls-got-hi` | TLS initial-exec |
| `MO_TLS_GD_HI` | 12 | `riscv-tls-gd-hi` | TLS general/local-dynamic |

`MO_DIRECT_FLAG_MASK` 是 31（492 行）：一个 operand 只能有一个 direct flag，但可叠加 bitmask flags；`decomposeMachineOperandsTargetFlags`（3698-3701）按这个掩码拆。所以在 MIR 里看到 `target-flags(riscv-hi)`，对应的是 `RISCVII::MO_HI`，不要去找 `MO_riscv_hi` 这种名字。

---

## 11.7 PIC vs non-PIC：`%pcrel_hi` 与 `%got_pcrel_hi` 的使用条件

```c
int g;
static int s;
int *addr_g(void) { return &g; }
int *addr_s(void) { return &s; }
```

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fPIC -S -o - pic.c | sed -n '/^addr_g:/,/Lfunc_end/p;/^addr_s:/,/Lfunc_end/p'
```

```
addr_g:
.Lpcrel_hi0:
	auipc	a0, %got_pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	ret
addr_s:
.Lpcrel_hi1:
	auipc	a0, %pcrel_hi(s)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi1)
	ret
```

规则：**PIC + dso_local**（`static`、`hidden`）走 `%pcrel_hi`/`%pcrel_lo`；**PIC + 可抢占**（默认 visibility 全局）走 `%got_pcrel_hi` + `ld %pcrel_lo`；**非 PIC** 由 code model 决定，只有 `Medium` 的 `extern_weak` 走 GOT。

本组测试的实际差异：

```bash
for OPT in "-fno-pic" "-fno-pie" "-fPIE" "-fPIC"; do
  echo "===== $OPT"
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 $OPT -S -o - addr.c | sed -n '/addr_g:/,/Lfunc_end/p'
done
```

```
-fno-pic / -fno-pie:  lui %hi(g); addi %lo(g)
-fPIE:                auipc %pcrel_hi(g); addi %pcrel_lo(.Lpcrel_hi0)
-fPIC:                auipc %got_pcrel_hi(g); ld %pcrel_lo(.Lpcrel_hi0)(a0)
```

重定位一一对应。`-fPIC` 的 `pic.o`：

```
0x0  R_RISCV_GOT_HI20        g
0x4  R_RISCV_PCREL_LO12_I    .Lpcrel_hi0
0xA  R_RISCV_PCREL_HI20      s
0xE  R_RISCV_PCREL_LO12_I    .Lpcrel_hi1
```

非 PIC `medlow` 的 `low.o`：

```
0x0  R_RISCV_HI20     g
0x4  R_RISCV_LO12_I   g
```

`R_RISCV_PCREL_LO12_I` 的目标是 `.Lpcrel_hi0` 这个 AUIPC 标签，不是 `g`；GOT 版本也一样。把第二个操作数写成符号名，链接器就无法把 hi/lo 配成一对。

---

## 11.8 TLS 四种模型

`TargetMachine::getTLSModel`（`TargetMachine.cpp:311-336`）先按“是否共享库 + 是否 dso_local”给默认值，再允许用户模型**只向更具体方向覆盖**（`SelectedModel > Model`）：

| 模型 | clang 选项 | 典型序列 | 关键重定位 |
|---|---|---|---|
| local-exec | `-ftls-model=local-exec` | `lui %tprel_hi; add tp, %tprel_add; addi %tprel_lo` | `TPREL_HI20`/`TPREL_ADD`/`TPREL_LO12_I` |
| initial-exec | `-ftls-model=initial-exec` | `auipc %tls_ie_pcrel_hi; ld %pcrel_lo; add tp` | `TLS_GOT_HI20` + `PCREL_LO12_I` |
| local-dynamic | `-ftls-model=local-dynamic` | `auipc %tls_gd_pcrel_hi; addi %pcrel_lo; call __tls_get_addr` | `TLS_GD_HI20` + `CALL_PLT` |
| general-dynamic | `-ftls-model=global-dynamic` | 同 local-dynamic（单变量） | `TLS_GD_HI20` + `CALL_PLT` |
RISC-V 没有单独的 TLS local-dynamic 重定位；`lowerGlobalTLSAddress` 把 `LocalDynamic` 和 `GeneralDynamic` 都交给 `getDynamicTLSAddr`（`RISCVISelLowering.cpp:10411-10414`）。单变量下两者序列相同，区别在符号绑定与链接器/加载器的优化空间。TLSDESC（`-mtls-dialect=desc`）本章**未实测**：目标构建默认未启用 TLSDESC，且其序列需链接器与加载器配合，超出编译器侧范围。

### 实测：四种模型的 `&tls_ext`
```bash
cd /tmp/ch11 && cat > tls.c <<'EOF'
extern __thread int tls_ext;
int *addr_ext(void) { return &tls_ext; }
EOF
for M in local-exec initial-exec local-dynamic global-dynamic; do
  echo "===== $M"
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 -fPIC -ftls-model=$M -S -o - tls.c | sed -n '/^addr_ext:/,/Lfunc_end/p'
done
```
```
===== local-exec
	lui	a0, %tprel_hi(tls_ext)
	add	a0, a0, tp, %tprel_add(tls_ext)
	addi	a0, a0, %tprel_lo(tls_ext)
	ret
===== initial-exec
.Lpcrel_hi0:
	auipc	a0, %tls_ie_pcrel_hi(tls_ext)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	add	a0, a0, tp
	ret
===== local-dynamic / global-dynamic
	addi	sp, sp, -16
	sd	ra, 8(sp)
.Lpcrel_hi0:
	auipc	a0, %tls_gd_pcrel_hi(tls_ext)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)
	call	__tls_get_addr
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```
local-exec 的 `add tp, %tprel_add` 不是普通加法：`%tprel_add` 让链接器把“高 20 位 + 低 12 位”的进位一次性补进这条 `add`。initial-exec 先从 GOT 取出“相对 TP 的偏移”，再 `add tp`。

### 实测：LE 与 GD 的重定位
```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fPIC -ftls-model=local-exec -c -o tle.o tls.c
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fPIC -ftls-model=global-dynamic -c -o tgd.o tls.c
$T/bin/llvm-readobj -r tle.o | sed -n '/\.rela.text/,/}/p'
$T/bin/llvm-readobj -r tgd.o | sed -n '/\.rela.text/,/}/p'
```

```
# tle.o
0x0  R_RISCV_TPREL_HI20     tls_ext
0x4  R_RISCV_TPREL_ADD      tls_ext
0x8  R_RISCV_TPREL_LO12_I   tls_ext
# tgd.o
0x4  R_RISCV_TLS_GD_HI20    tls_ext
0x8  R_RISCV_PCREL_LO12_I   .Lpcrel_hi0
0xC  R_RISCV_CALL_PLT       __tls_get_addr
```

### 一个容易踩的模型提升
`-ftls-model=local-dynamic` 在 `-fno-pic` 下不会得到 `__tls_get_addr`：

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -ftls-model=local-dynamic -S -emit-llvm -o - tls.c | grep 'tls_ext ='
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fno-pic -ftls-model=local-dynamic -S -o - tls.c | sed -n '/^addr_ext:/,/Lfunc_end/p'
```

```
@tls_ext = external thread_local(localdynamic) global i32, align 4
.Lpcrel_hi0:
	auipc	a0, %tls_ie_pcrel_hi(tls_ext)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
	add	a0, a0, tp
```

IR 写着 `localdynamic`，汇编却是 initial-exec。原因：非共享库 + 非 dso_local 的默认模型是 `InitialExec`（`TargetMachine.cpp:323-327`），而 `LocalDynamic` 在枚举里排在 `InitialExec` 之前；只有 `SelectedModel > Model` 才覆盖（330-333），更“弱”的 local-dynamic 被忽略。

---

## 11.9 GP relaxation 的编译器侧

先纠正一个误解：**编译器不生成 `gp` 相对寻址**。对 `medlow` 全局变量，它照常发 `lui+addi` 和 `R_RISCV_HI20`/`R_RISCV_LO12_I`；`+relax` 打开时再补 `R_RISCV_RELAX`。真正把 `lui+addi` 改成 `addi rd, gp, offset` 的是链接器（Ch 12）。编译器侧只做三件事：

1. 把小的全局对象放进 `.sdata/.sbss/.srodata`，让链接器有机会安排在 `gp` 附近；
2. 在可 relax 的 fixup 上发 `R_RISCV_RELAX`（`RISCVAsmBackend.cpp:890-914`）；
3. 永久保留 `gp`（x3）和 `tp`（x4）。

### `-msmall-data-limit` 改的是“分节”，不是“寻址”
`-msmall-data-limit=N` 是 `-G N` 的别名（`clang/include/clang/Options/Options.td:5868-5870`），driver 传给 cc1（`Clang.cpp:2098-2101`），存进 `CodeGenOpts.SmallDataLimit`，再以 module flag `"SmallDataLimit"` 写进 IR（`CodeGenModule.cpp:2118-2123`）。LLVM 侧读取它的是 `RISCVELFTargetObjectFile`（`RISCVTargetObjectFile.cpp:49-58`），**不是 `RISCVSubtarget`**。

```bash
cat > small.c <<'EOF'
int big[64];
int tiny;
int *addr_tiny(void) { return &tiny; }
EOF
for G in 0 8; do
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 -fno-pic -msmall-data-limit=$G -c -o small$G.o small.c
  echo "===== $G"
  $T/bin/llvm-readelf -SW small$G.o | grep -E '\.sbss|\.bss'
done
```
```
===== 0
  [ 4] .bss     NOBITS  0000000000000000 000054 000104 00  WA  0  0  4
===== 8
  [ 4] .sbss    NOBITS  0000000000000000 000054 000004 00  WA  0  0  4
  [ 5] .bss     NOBITS  0000000000000000 000054 000100 00  WA  0  0  4
```
`tiny`（4 字节）从 `.bss` 移到 `.sbss`，`big`（256 字节）留下，但 `addr_tiny` 仍是 `lui %hi(tiny); addi %lo(tiny)`。这是“编译器只分节、链接器才改写”的直接证据。`isInSmallSection` 条件是 `Size > 0 && Size <= SSThreshold`（`RISCVTargetObjectFile.cpp:83-87`）；0 字节对象按 GCC 传统不算 small data。

### `+relax`：发许可证，不改指令
`+relax` 是 Subtarget feature（`RISCVFeatures.td:2031-2033`，字段 `EnableLinkerRelax`），不影响指令选择，只影响是否追加 `R_RISCV_RELAX`：
```bash
for F in "s_def:" "s_norelax:-mno-relax"; do
  N=${F%%:*}; X=${F#*:}
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 -fno-pic $X -c -o $N.o small.c
done
for f in s_def s_norelax; do
  printf "%s: " "$f"; $T/bin/llvm-readobj -r $f.o | grep -c R_RISCV_RELAX
done
```

```
s_def: 4
s_norelax: 0
```
本树 clang 默认 `target-features` 带 `+relax`（实测 IR），所以默认对象就有 4 条 `R_RISCV_RELAX`；`-mno-relax` 全部去掉。

### `gp` 为什么永远保留
`RISCVRegisterInfo::getReservedRegs`（`RISCVRegisterInfo.cpp:165-225`）无条件保留：
```cpp
markSuperRegs(Reserved, RISCV::X2_H); // sp
markSuperRegs(Reserved, RISCV::X3_H); // gp
markSuperRegs(Reserved, RISCV::X4_H); // tp
```

`gp`/`tp` 不是 callee-saved 那种保留，而是**全局不变量**：`tp` 是 TLS 线程指针，local-exec/initial-exec 直接读它；`gp` 是链接器 relaxation 的锚点，启动代码把它设成 `__global_pointer$`（通常指向 `.sdata + 0x800`），链接器才敢把 `lui+addi` 折成 `addi rd, gp, offset`。编译器若把 x3 当普通 GPR 分配，链接器的假设立刻失效。

一句话桥接 Ch 12：**编译器只负责把 `R_RISCV_*` 与 `R_RISCV_RELAX` 放对位置；是否改写、改几字节，由 lld 的 relaxation 状态机决定。**

---

## 11.10 源码落点

- **`RISCVISelLowering.cpp`**
  - 8384-8391 分派；10266-10294 四个 `lower*`；10180-10264 `getAddr`（PIC 10189、`Small` 10215、`Medium` 10231、`Large` 10255）。
  - 10296-10341 LE/IE；10343-10370 GD/LD；10372-10387 TLSDESC；10389-10419 模型分派。
- **`RISCVInstrInfo.td`** 1658-1676、2026-2072：`PseudoMovAddr` 与 `PseudoLA*` 家族。
- **`RISCVExpandPseudoPreRA.cpp` / `RISCVExpandPseudoPostRA.cpp`** 78-193 / 74-115：伪指令展开。
- **`MCTargetDesc/RISCVBaseInfo.h`** 469-493：`MO_*` 枚举；**`RISCVInstrInfo.cpp`** 3698-3725：MIR 字符串。
- **`AsmParser/RISCVAsmParser.cpp`** 3781-3829、4526-4535：`la/lla/lga` 的汇编期展开。
- **`CommonArgs.cpp`** 3122-3132：code model 映射；**`Clang.cpp`** 2098-2101 + **`CodeGenModule.cpp`** 2118-2123：`-G` → module flag。
- **`RISCVTargetObjectFile.cpp`** 49-58、83-87、96-160：small data 分节；169-199：large 常量池；**`RISCVTargetMachine.cpp`** 87-93、101-112：默认 relocation/code model。
- **`RISCVFeatures.td`** 2031-2033 + **`RISCVAsmBackend.cpp`** 889-914：`+relax` 与 `R_RISCV_RELAX`；**`TargetMachine.cpp`** 311-336：`getTLSModel`；**`RISCVRegisterInfo.cpp`** 165-225：`getReservedRegs`。

---

## 11.11 可跑实验

### 实验 1：code model 三连与 2 GiB 边界

```bash
cd /tmp/ch11
for CM in medlow medany large; do
  echo "===== -fno-pic -mcmodel=$CM"
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
    -O2 -fno-pic -mcmodel=$CM -S -o - addr.c | sed -n '/addr_g:/,/Lfunc_end/p'
done
```

预期：`lui+addi` / `auipc+addi` / `.LCPI0_0 + auipc+ld`。去掉 `-fno-pic`，前两者会变成同一个 `%pcrel_hi` 序列。

2 GiB 边界：用 11.3 的 `far.ld` 与 `far2.ld` 分别链接 `low.o`：第一条成功，第二条报 `R_RISCV_HI20 out of range: 524288`。再用 `any.o` 链接 `far2.ld`，得到 `auipc 0x7fff0`。

### 实验 2：PIC 与 GOT

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d \
  -O2 -fPIC -c -o pic.o pic.c
$T/bin/llvm-readobj -r pic.o | sed -n '/\.rela.text/,/}/p'
```

预期：`g` 是 `GOT_HI20`，`s` 是 `PCREL_HI20`。把 `g` 改成 `static` 再跑，`GOT_HI20` 消失。

### 实验 3：TLS、small data、RELAX 与 MIR
TLS 四模型与模型提升：重复 11.8 的 `for M in ...` 循环；再跑 `-fno-pic -ftls-model=local-dynamic`，对比 IR 的 `thread_local(localdynamic)` 与汇编的 initial-exec 序列。

small data、RELAX 与 MIR：重复 11.9 的 `small$G.o` 循环；预期 `tiny` 进入 `.sbss` 而 `addr_tiny` 仍为 `lui+addi`。再跑：

```bash
$T/bin/llvm-readobj -r small8.o | grep -c R_RISCV_RELAX
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c \
  -stop-after=finalize-isel -simplify-mir low.ll -o - | grep Pseudo
```

预期：RELAX 计数为 4；MIR 为 `PseudoMovAddr target-flags(riscv-hi) @g, target-flags(riscv-lo) @g`。换成 `pic.ll` 并加 `-relocation-model=pic` 会看到 `PseudoLGA`/`PseudoLLA`。

---

## 11.12 踩雷集锦

1. **按 `LowerGlobalAddress` 搜代码，什么都搜不到。** 真实拼写是小写开头的 `lowerGlobalAddress`；分派在 `LowerOperation` 的 8384 行。
2. **在默认 PIE 下比较 `medlow` 和 `medany`。** 两者都走 `%pcrel_hi`，因为 PIC 分支（10189）在 code model 之前；要加 `-fno-pic` 才看得到差异。
3. **把 `-mcmodel=large` 和 `-fPIC` 一起用。** driver 报 `invalid argument '-mcmodel=large' not allowed with '-fpic'`；large 还只支持 RV64。
4. **`%got_pcrel_hi` 配 `lui`，或 `%pcrel_lo` 后面写符号名。** GOT 必须是 `auipc %got_pcrel_hi(sym)` + `ld %pcrel_lo(label)`；`%pcrel_lo` 引用 AUIPC 标签。
5. **以为 `-msmall-data-limit` 会让编译器发 `gp` 相对寻址。** 它只把对象放进 `.sdata/.sbss`；汇编仍是 `lui+addi`，改写是链接器的事。
6. **去 `RISCVSubtarget` 里找 small-data-limit 开关。** 没有；`RISCVSubtarget` 只有 `+relax`，分节逻辑在 `RISCVELFTargetObjectFile` + module flag。
7. **TLS 模型名写成 `general-dynamic`，或以为 `-fno-pic` 下 local-dynamic 一定走 `__tls_get_addr`。** clang 只认 `global-dynamic`；`getTLSModel` 只允许更具体的模型覆盖。
8. **把 `gp` 当普通 GPR 用。** `getReservedRegs` 永久保留 x3；保存/恢复或分配 `gp` 都会破坏链接器 relaxation 的锚点。

---

## 11.13 本章重点整理

- 地址生成由三件事决定：**PIC、dso_local、code model**；TLS 独立分支。
- `medlow` → `Small` → `lui+addi`（绝对 ±2 GiB）；`medany` → `Medium` → `auipc+addi`（PC 相对 ±2 GiB）；`large` → `Large` → 常量池存 64 位地址。
- 默认 PIE 下 code model 对全局变量不起作用：PIC 下只有“dso_local 用 pcrel、可抢占用 GOT”两种结果。
- 四个 lowering 入口都走 `getAddr`；`lowerBlockAddress`/`lowerConstantPool`/`lowerJumpTable` 默认 `IsLocal=true`，PIC 下不会走 GOT。
- `PseudoMovAddr` 可重物化、post-RA 展开；`PseudoLLA/LGA` pre-RA 展开；`PseudoLA` 只用于手写汇编。
- `riscv-hi`/`riscv-lo`/`riscv-pcrel-hi`/`riscv-pcrel-lo` 是 MIR 拼写；真实枚举是 `RISCVII::MO_HI`/`MO_LO`/`MO_PCREL_HI`/`MO_PCREL_LO`。
- TLS 四模型：LE 用 `%tprel_hi/%tprel_add/%tprel_lo`；IE 用 `%tls_ie_pcrel_hi` + GOT + `add tp`；LD/GD 共用 `%tls_gd_pcrel_hi` + `__tls_get_addr`；`getTLSModel` 只允许“更具体”覆盖。
- `-msmall-data-limit` 只决定分节；`+relax` 只决定是否追加 `R_RISCV_RELAX`；`gp`（x3）和 `tp`（x4）是全局保留寄存器。

---

## 11.14 自我检核

- [ ] 能画出“TLS / PIC / dso_local / code model”决策树，并说出每层源码行号
- [ ] 能解释为什么默认 PIE 下 `medlow` 和 `medany` 生成相同汇编
- [ ] 能手算 `0x7ffff000` 与 `0x80000000` 的 `hi20`，并解释 LLD 的 `[-524288, 524287]` 检查
- [ ] 能说出 `PseudoMovAddr`、`PseudoLLA`、`PseudoLGA` 的展开时机与序列
- [ ] 能写出 local-exec、initial-exec、general-dynamic 三条 TLS 序列及关键重定位
- [ ] 能解释 `-fno-pic -ftls-model=local-dynamic` 为什么得到 initial-exec
- [ ] 能解释 `-msmall-data-limit` 与 GP relaxation 的分工，以及 `gp` 为什么必须保留

---

## 11.15 延伸阅读

- **`RISCVISelLowering.cpp`** 10180-10419：地址生成的四个出口与 TLS 分派；注释直接写出每条序列的形状。
- **`RISCVExpandPseudoPreRA.cpp` / `RISCVExpandPseudoPostRA.cpp`** 78-193 / 74-115：`PseudoLLA/LGA` 与 `PseudoMovAddr` 的展开时机不同，改调度或 RA 时必须知道。

---

→ 下一章：[Ch 12 — LLD RISC-V 重定位与 relaxation](./12-lld-relocations-and-relaxation.md)
