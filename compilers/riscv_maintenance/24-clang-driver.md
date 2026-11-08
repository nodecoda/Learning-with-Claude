# Ch 24 — Clang Driver：-march / -mabi / -mcpu 的优先级

> **目标**：把 `-march` / `-mabi` / `-mcpu` / `-mtune` 从命令行到 cc1 的优先级讲清；能解释一次 `clang -march=rv64gc_zba -mabi=lp64d` 在 driver、TargetInfo、后端、ELF 四处分别发生了什么；能用 `-###` 与真实编译区分「driver 已拒绝」和「后端才警告」。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。本章「实测」均来自该构建。

> **前置**：读完 Ch 02（`-mattr`/`-march` 如何变成 `hasStdExt*()`）与 Ch 22（`.attribute arch`、`e_flags`、链接期 ABI 冲突）。本章不重复 feature 配电盘与 ELF 编码，只讲 driver 如何决定「送哪组 feature 给谁」。

---

## 24.1 为什么 driver 是「扩展改动的第一道关」

后端只看见扁平的 feature 列表：`+i +m +a +f +d +c ... +zba`。它不知道输入来自 `-march=rv64gc_zba`、`-mcpu=sifive-u74` 还是后端测试里的 `-mattr=+zba`。把这些输入翻译成同一份列表并决定谁覆盖谁的地方，只有 driver。

一个 `-march` 语义改动会同时影响五处：

| # | 站位 | 关键位置 | 失败形态 |
|---|---|---|---|
| 1 | Driver 解析 | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:27` | `invalid arch name '...'` |
| 2 | cc1 参数 | `clang/lib/Driver/ToolChains/Clang.cpp:1600`、`:2095`、`:6523` | 这一层不校验 `-mabi` |
| 3 | TargetInfo | `clang/lib/Basic/Targets/RISCV.cpp:342`、`:414` | `unknown target ABI`、`invalid feature combination` |
| 4 | 后端 subtarget | `llvm/lib/Target/RISCV/RISCVSubtarget.cpp:75`、`:88` | `note: hard-float 'd' ABI ...`（非致命） |
| 5 | ELF 输出 | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp:101`、`:110` | 链接期 ABI 不一致（Ch 22） |

漏一处就会出现「能编译、但产物不可链接」或「driver 接受、后端忽略」：

- 只在后端加 feature/pattern、没让 `RISCVISAInfo` 的解析表认识这个扩展名：`clang -march=...` 直接报 `invalid arch name`。
- 只改后端、只用 `llc -mattr=+xfoo` 测试：driver 这一关被绕过，`clang` 用户仍用不了；上游 PR 必须补 `clang/test/Driver/riscv-arch.c`。
- 只改 `-march` 解析、没改 `.attribute` 规范化：链接器和 QEMU 看到的仍是旧 ISA。

**driver 的代码顺序就是优先级。**

---

## 24.2 先建立直觉：一次 `clang -march=... -mabi=...` 的参数旅程

```text
$ clang --target=riscv64-unknown-elf -march=rv64gc_zba -mabi=lp64d -c x.c
        │
        ▼
[1] Driver：clang/lib/Driver/ToolChains/Arch/RISCV.cpp
    ├─ getRISCVArch      :305   -march > -mcpu 默认 > -mabi 推导 > triple
    ├─ getRISCVABI       :222   -mabi > arch 推导 > triple
    ├─ getRISCVTargetCPU :409   -mcpu > -march=native > generic-rv{32,64}
    └─ getRISCVTargetFeatures :69
         └─ getArchFeatures :27 → parseArchString :33
              ├─ 成功 → toFeatures(AddAllExtensions=true) → 正项 + 负项
              └─ 失败 → "clang: error: invalid arch name 'rv64gc_xfoo',
                         unsupported non-standard user-level extension 'xfoo'"
        │
        ▼
[2] cc1 命令行：Clang.cpp:1600 / :2095 / :6523
    -triple riscv64-unknown-unknown-elf   -target-cpu generic-rv64
    -target-feature +i +m +a +f +d +c ... +zba ... -xfoo ...   (286 个)
    -target-abi lp64d                    -tune-cpu <only if -mtune>
        │  ← 只拼字符串；-mabi=bogus 也原样送下去
        ▼
[3] TargetInfo：Targets/RISCV.cpp:342 / :414
    initFeatureMap → parseFeatures；handleTargetFeatures → ISAInfo、宏、builtin
        │  ← "error: unknown target ABI 'bogus'" / "invalid feature combination"
        ▼
[4] CodeGen / Subtarget：RISCVSubtarget.cpp:75 / :88
    ParseSubtargetFeatures → hasStdExt*；computeTargetABI
        │  ← 非致命："note: hard-float 'd' ABI ... (ignoring target-abi)"
        ▼
[5] AsmPrinter / ELFStreamer：RISCVELFStreamer.cpp:101 / :110
    .attribute 5 = "rv64i2p1_..._zba1p0"；e_flags = RVC | FLOAT_ABI | RVE | TSO
        │  ← lld："cannot link object files with different floating-point ABI"
        ▼
      x.o
```

三个容易看错的点：

1. **driver 只校验 `-march` 与 `-mcpu`，不校验 `-mabi` 的内容。** `getRISCVABI` 对显式 `-mabi=` 直接 `return A->getValue()`（`RISCV.cpp:247`）。
2. **`-###` 只走到第 2 步。** 它打印 cc1 命令就退出，看不到后端 note。
3. **同一份 feature 列表会被解释两次。** TargetInfo 用它决定宏/builtin，后端用它决定选指/ABI；两次不一致就是 bug。

---

## 24.3 `-march` 的解析：base + 单字母 + `_z*`

```text
rv64   gc        _zba
│      │          │
XLEN   baseline + 单字母扩展   多字母扩展（必须有下划线）
```

- `rv64` / `rv32` 决定 XLEN；首字母必须是 `i`、`e`、`g` 或 `y`（`RISCVISAInfo.cpp:632`）。
- 单字母扩展可连写，`g` 是 `imafd_zicsr_zifencei` 的缩写；多字母用 `_` 分隔，可带版本号。
- 实测 `rv64gc_zba` 规范化为 `rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0_zba1p0`，这个串会进 `.attribute 5`。

按 psABI，`g = imafd_zicsr_zifencei`；本树还会物化新规范里的隐含扩展。实测 `-march=rv64gc` 的正项：

```text
+i +m +a +f +d +c +zicsr +zifencei +zmmul +zaamo +zalrsc +zca +zcd
```

其中 `m→zmmul`、`a→zaamo/zalrsc`、`c→zca`（有 `d` 时 `zcd`，RV32 有 `f` 时 `zcf`）。`g` 自带 `zicsr`/`zifencei`，所以 `rv64gc` 有它们，而裸机默认的 `rv64imac` 没有。`v` 更长：实测 `-march=rv64gcv` 多出 `+v +zve32x +zve32f +zve64x +zve64f +zve64d +zvl32b +zvl64b +zvl128b`；漏 `zvl128b`，后端的向量合法性判断就缺前提（Ch 02、Ch 17）。

`getRISCVArch`（`RISCV.cpp:305`）四步：显式 `-march`（`native` 等价 `-mcpu=native`，走 `:343-350`）→ `-mcpu` 的 `DefaultMarch` → `-mabi` 反推 → triple。实测：

| triple | 默认 `-march` | 默认 `-mabi` | 关键正项 |
|---|---|---|---|
| `riscv64-unknown-elf` | `rv64imac` | `lp64` | `+i +m +a +c +zmmul +zaamo +zalrsc +zca` |
| `riscv64-linux-gnu` | `rv64imafdc`（不含 `zifencei`） | `lp64d` | `+i +m +a +f +d +c +zicsr ... +zcd` |
| `riscv32-unknown-elf` | `rv32imac` | `ilp32` | `+i +m +a +c ... +zca` |
| `riscv32-linux-gnu` | `rv32imafdc` | `ilp32d` | `+i +m +a +f +d +c ... +zcf` |

**裸机默认不是 `rv64gc`，而是 `rv64imac`。** `RISCV.cpp:393-407` 对 `UnknownOS` 返回 `rv64imac`/`rv32imac`，其它 OS 返回 `rv64imafdc`/`rv32imafdc`；Android 与 Fuchsia 另有分支（`:402`、`:404`），分别默认 `rv64imafdcv_zba_zbb_zbs` 与 `rva22u64_v`。

实验扩展必须同时有 flag 与精确版本号：

```bash
$T/bin/clang --target=riscv32-unknown-elf -march=rv32izibi0p1 -c x.c
# clang: error: invalid arch name 'rv32izibi0p1',
# requires '-menable-experimental-extensions' for experimental extension 'zibi'
```

加上 flag 后 cc1 收到 `+experimental-zibi` 与 `+experimental`；版本号不能省、不能写错：`rv32izibi` → `requires explicit version number`，`rv32izibi0p7` → `unsupported version number 0.7 ... (this compiler supports 0.1)`。实现是 `RISCVISAInfo.cpp:375-395`；driver 把 flag 直接传给 `parseArchString`（`RISCV.cpp:31-34`），所以**没有 flag，错误在 driver 就发生**。

---

## 24.4 `-mabi` 的默认与校验：driver 只做「选」，不做「验」

`getRISCVABI`（`RISCV.cpp:222`）三步：显式 `-mabi=` 直接返回；否则用 `getRISCVArch` 的结果做 `computeDefaultABI`；解析失败再按 triple 兜底（裸机 `lp64`/`ilp32`，其它 `lp64d`/`ilp32d`）。第 2 步的表在 `RISCVISAInfo.cpp:1102`：

| ISA 里有 | RV32 | RV64 |
|---|---|---|
| `e` / `d` / `f` / 都没有 | `ilp32e` / `ilp32d` / `ilp32f` / `ilp32` | `lp64e` / `lp64d` / `lp64f` / `lp64` |

所以 `-march=rv64gc` 不写 `-mabi` 得 `lp64d`；`-mabi=lp64d` 不写 `-march` 走 `getRISCVArch` 第 3 步得 `rv64imafdc`。两者互为默认，形成小循环但结果稳定。实测：

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -c x.c | grep -o '"-target-abi" "[^"]*"'
# "-target-abi" "lp64"

$T/bin/clang -### --target=riscv64-unknown-elf -march=rv64gc -c x.c | grep -o '"-target-abi" "[^"]*"'
# "-target-abi" "lp64d"

$T/bin/clang -### --target=riscv64-unknown-elf -mabi=lp64d -c x.c \
  | grep -o '"-target-feature" "+[^"]*"' | tr '\n' ' '
# +i +m +a +f +d +c +zicsr +zmmul +zaamo +zalrsc +zca +zcd
```

最后一条说明 **`-mabi` 也会反过来改默认 `-march`**，这是 `getRISCVArch` 第 3 步的直接结果。

`-mabi` 的合法性不在 driver 检查：

```bash
$T/bin/clang --target=riscv64-unknown-elf -mabi=bogus -c x.c
# error: unknown target ABI 'bogus'

$T/bin/clang --target=riscv64-unknown-elf -mabi=ilp32 -c x.c
# error: unknown target ABI 'ilp32'
```

`ilp32` 是合法 ABI 名，只是不能用于 RV64；错误仍叫 `unknown target ABI`，因为 cc1 的 `TargetInfo` 在 `setABI` 阶段拒绝（`DiagnosticCommonKinds.td:338`）。`clang -###` 会把 `-target-abi ilp32` 原样打印并正常退出——**`-###` 通过不代表能编译**。

真正「driver 放行、后端才提示」的是硬浮点 ABI 与缺少 F/D 的组合：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -c x.c
# note: hard-float 'd' ABI can't be used for a target that doesn't support
#       the D instruction set extension (ignoring target-abi)
```

这条 note 来自 `RISCVSubtarget.cpp:88-94`（`computeTargetABI` 失败后 `errs()` 打印并回落），不是 driver，也不是 TargetInfo。实际行为：driver 原样返回 `lp64d`；cc1 收到 `+i +m +a +c` 与 `-target-abi lp64d`；后端回落到 `lp64`；代码生成按软 ABI 用 `a0` 传 `double`；ELFStreamer 仍按 `TargetOptions.ABIName` 写 `lp64d`。实测 `double id(double)`：

```asm
# imac + lp64d：a0 传 double              # rv64gc + lp64d：fa0 传 double
sd a0, 8(sp) / ld a0, 8(sp)              fsd fa0, 8(sp) / fld fa0, 8(sp)
```

前者 `e_flags` 仍是 `Flags: 0x5, RVC, double-float ABI`。这就是 Ch 22「`e_flags 0x5` + `__adddf3`」的来源：**note 只说忽略 target-abi，没有把 ELF 的 ABI 标记也改掉**。链接器看到 `lp64d`，代码生成用 `lp64`，driver 层无法修复，只能在命令行避免。

---

## 24.5 `-mcpu` / `-mtune`：CPU 名、默认 ISA 与调度

`getRISCVTargetCPU`（`RISCV.cpp:409`）只决定 `-target-cpu`：`-mcpu=X` > `-march=native` > `generic-rv64`/`generic-rv32`。但 `-mcpu` 还通过 `getRISCVArch` 第 2 步提供默认 ISA：`getMArchFromMcpu` 查 `RISCVTargetParserDef.inc` 的 `DefaultMarch`，再走 `getArchFeatures` 变成 `-target-feature`。所以它至少影响三样：默认 ISA、`-target-cpu`、CPU 相关的非对齐访存 feature（`RISCV.cpp:99-102` 判定、`:149-179` 落成 feature）。

实测 `sifive-u74` 与 `rocket-rv64`：

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -mcpu=sifive-u74 -c x.c \
  | grep -o '"-target-feature" "+[^"]*"' | tr '\n' ' '
# +i +m +a +f +d +c +zicsr +zifencei +zmmul +zaamo +zalrsc +zca +zcd +relax

$T/bin/clang -### --target=riscv64-unknown-elf -mcpu=rocket-rv64 -c x.c \
  | grep -o '"-target-feature" "+[^"]*"' | tr '\n' ' '
# +i +zicsr +zifencei +relax
```

差异来自 `DefaultMarch`：`sifive-u74` 接近 `rv64imafdc_zicsr_zifencei_zmmul_zaamo_zalrsc_zca_zcd`；`rocket-rv64` 只有 `rv64i_zicsr_zifencei`。`-mcpu=help` 在本树列出 **60 个** CPU：

```bash
$T/bin/clang --target=riscv64-unknown-elf -mcpu=help -c x.c 2>&1 | grep -c $'^\t'
# 60
```

开头是 `an-erbium`、`andes-45-series`…，末尾是 `xiangshan-kunminghu`、`xiangshan-nanhu`、`xt-c910v2`、`xt-c920v2`。Ch 21 已用这份列表做调度实验；**列表长度随 trunk 变化，不要写死进测试**。

`-march` 与 `-mcpu` 同时出现时各管一半：实测 `-march=rv64gc -mcpu=rocket-rv64 -mabi=lp64d` 得到 `-target-cpu rocket-rv64`，feature 仍是 `+i +m +a +f +d +c +zicsr +zifencei +zmmul +zaamo +zalrsc +zca +zcd`，`-target-abi lp64d`。**ISA 字符串由 `-march` 赢，`-target-cpu` 由 `-mcpu` 赢**；所以既有 `m/a/f/d` 指令，又是 rocket 的 CPU 模型。

feature 的 append 顺序（`getRISCVTargetFeatures`，`RISCV.cpp:69`）：arch feature → CPU 非对齐 feature → `-ffixed-X` → `±relax` → strict-align → tune feature → 用户显式 `-m...`（`handleTargetFeaturesGroup`，`:218`）。`-target-feature` 后者覆盖前者，所以显式选项优先级最高。driver 没有 `-mattr`；它是 `llc` 等工具的选项（cc1 用 `-target-feature`）。在 `llc` 里显式 `-mattr` 同样在 CPU 之后应用：

```bash
$T/bin/llc -mtriple=riscv64 -mcpu=sifive-u74 -mattr=-c -O2 tune.ll -o - | grep 'attribute.*5'
# .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0"
```

（`tune.ll` 见 24.11 实验 6）`c` 与 `zcd` 都消失了；不加 `-mattr=-c` 时两者都在。**显式 feature 永远最后生效。**

`getRISCVTuneCPU`（`RISCV.cpp:432`）把 `-mtune=X` 变成 `-tune-cpu X`，不碰 `-target-cpu`：

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -mcpu=sifive-u74 -mtune=rocket-rv64 -c x.c \
  | grep -o '"-target-cpu" "[^"]*"\|"-tune-cpu" "[^"]*"'
# "-target-cpu" "sifive-u74"
# "-tune-cpu" "rocket-rv64"
```

对普通 CPU 名，`-mtune` 只换调度模型。实测同一段 IR，`u74.s` 与 `rocket.s` 的差异只有：

```diff
 	ld	a3, 0(a0)
 	ld	a0, 8(a0)
-	add	a0, a0, a2
 	mul	a1, a3, a1
+	add	a0, a0, a2
 	add	a0, a0, a1
 	ret
```

指令条数不变，只是 `add a0, a0, a2` 的位置变了；`.attribute` 也不变。例外是 `-mexperimental-mtune-syntax` 的 `-mtune=sifive-x390:full-vec-fp64`，它会插入 `-target-feature -single-element-vec-fp64`（`riscv-mtune-tune-features.c:3-12`）。

校验不对称：`-mcpu=nope` 在 driver 立刻报 `clang: error: unsupported argument 'nope' to option '-mcpu='`（`isValidRISCVCPU`，`RISCV.cpp:53`）；`-mtune=nope` 照样通过 `-###`，真编译时 cc1 才报 `error: unknown target CPU 'nope'`。`-mcpu` 的负例可只跑 `-###`，`-mtune` 的负例必须真编译。

---

## 24.6 `--target` 与 multilib/ABI：triple 决定默认值

| 维度 | `riscv64-unknown-elf` | `riscv64-linux-gnu` |
|---|---|---|
| `-triple` | `riscv64-unknown-unknown-elf` | `riscv64-unknown-linux-gnu` |
| 默认 `-march` / `-mabi` | `rv64imac` / `lp64` | `rv64imafdc` / `lp64d` |
| relocation model | `static` | `pic`（`-pic-level 2`，PIE） |
| include | `-nostdsysteminc` + `lib/clang-runtimes/riscv64-unknown-elf/include` | `/usr/local/include`、`/usr/include` |
| 链接 | `crt0.o`、`-Bstatic`、`/usr/bin/ld -m elf64lriscv` | `crti.o`/`crtbeginS.o`、`-dynamic-linker /lib/ld-linux-riscv64-lp64d.so.1` |

`-dynamic-linker` 的名字里就带 `lp64d`：triple + ABI 共同决定运行时加载器。`--sysroot` 只整体换前缀：

```bash
$T/bin/clang -### --target=riscv64-linux-gnu --sysroot=/tmp/sysroot24 x.c \
  | grep -o '\-\-sysroot=[^"]*\|"-internal-isystem" "[^"]*"'
# "-internal-isystem" ".../lib/clang/24/include"
# "-internal-isystem" "/tmp/sysroot24/usr/local/include"
# --sysroot=/tmp/sysroot24
```

driver 把 `-march` 规范化后再拼 multilib 目录名：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d --print-multi-directory
# rv64imafdc/lp64d

$T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64 --print-multi-directory
# .
```

`rv64gc` 被展开成 `rv64imafdc`，不是原字符串。multilib 目录名、`.attribute arch`、cc1 feature 列表三者来自同一个规范化结果；只改一处就会漂移。冲突分三层：cc1 的 `-mabi=ilp32` + `riscv64` → `error: unknown target ABI 'ilp32'`；后端的 `-march=rv64imac -mabi=lp64d` → `note: hard-float ...`，`-march=rv64e -mabi=lp64` → `note: only the lp64e ABI is supported for RV64E (ignoring target-abi)`；都不报错；链接期两个 `.o` 的 ABI 不同（Ch 22 实测）：

```text
error: f2_lp64.o: cannot link object files with different floating-point ABI from f1_lp64d.o
```

定位时先看错误是谁打的。

---

## 24.7 `-mcmodel` / `-mrelax` / `-mno-relax`：driver 怎么转发

`-mcmodel` 在 `Options.td:5671` 定义，映射在 `CommonArgs.cpp:3093`，RISC-V 分支在 `:3122-3134`：`medlow→small`、`medany→medium`、`large` 仅 RV64 且要求非 PIC。实测：

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -mcmodel=medlow -c x.c | grep -o '"-mcmodel=[^"]*"'
# "-mcmodel=small"

$T/bin/clang -### --target=riscv64-unknown-elf -mcmodel=medany -c x.c | grep -o '"-mcmodel=[^"]*"'
# "-mcmodel=medium"

$T/bin/clang --target=riscv64-linux-gnu -mcmodel=large -fpic -c x.c
# clang: error: invalid argument '-mcmodel=large' not allowed with '-fpic'
```

它是 cc1 的 `-mcmodel=`，不是 `-mllvm -riscv-...`。`small`/`medium`/`large` 对 `la.*`、GOT、TLS 的影响见 Ch 11；链接期配合 relaxation 见 Ch 12。

`-mrelax`/`-mno-relax` 在 `Options.td:5864-5866` 定义，driver 在 `getRISCVTargetFeatures` 里把它变成 feature（`RISCV.cpp:142`）：默认 `+relax`，`-mno-relax` 时 `-relax`。实测：

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -c x.c | grep -o '"[+-]relax"'
# "+relax"

$T/bin/clang -### --target=riscv64-unknown-elf -mno-relax -c x.c | grep -o '"[+-]relax"'
# "-relax"
```

- **不改 `.s` 文本**：实测 `-mrelax` 与 `-mno-relax` 的 `-S` 输出逐字节相同（`cmp` 返回 0）。
- **改 `.o` 重定位**：`-relax` 时不发 `R_RISCV_RELAX`（Ch 12）。
- **也传给链接器**：裸机加 `-X --no-relax`（`BareMetal.cpp:560`），GNU 加 `--no-relax`（`Gnu.cpp:344`）；实测链接行里能看到 `"--no-relax"`。

另一条独立通道是汇编器 `.option norelax`，属于 MC 层状态，不是 driver feature；两者都能关 relaxation，但作用点不同（Ch 22 有实验）。

---

## 24.8 优先级矩阵

| 要决定的东西 | 优先级（高 → 低） | 代码位置 |
|---|---|---|
| ISA 字符串 | `-march` > `-mcpu` 的 `DefaultMarch` > `-mabi` 反推 > triple | `RISCV.cpp:305-407` |
| ABI | 显式 `-mabi` > `computeDefaultABI` > triple | `RISCV.cpp:222-281` |
| `-target-cpu` | `-mcpu` > `-march=native` > `generic-rv{32,64}` | `RISCV.cpp:409-430` |
| `-tune-cpu` | `-mtune` > 空（后端回落到 CPU） | `RISCV.cpp:432-476` |
| code model | `-mcmodel` > 默认 `small` | `CommonArgs.cpp:3093` |
| relax | `-mno-relax` > `-mrelax` > 默认 `+relax` | `RISCV.cpp:142` |
| feature 列表 | 显式 `-m...` > tune feature > arch feature | `RISCV.cpp:211-220` |

两条规则：**`-march` 与 `-mcpu` 不互斥**（ISA 归 `-march`，CPU 模型归 `-mcpu`）；**重复 `-target-feature` 后者覆盖前者**，driver 的 append 顺序就是优先级。

---

## 24.9 改一个扩展时，按这五步验收

1. **driver 解析**：`clang -march=rv64gc_zfoo -### -c x.c` 不再报 `invalid arch name`。
2. **cc1 参数**：`-###` 里有 `+zfoo`，且没有误写成 `+experimental-zfoo`（除非它确实是实验扩展）。
3. **TargetInfo**：`__riscv_zfoo` 宏、相关 builtin 可用（Ch 25）；用 `clang -dM -E -x c /dev/null` 检查。
4. **后端**：`llc -mattr=+zfoo` 能选到指令，`hasStdExtZfoo()` 为真（Ch 02）。只跑 `llc` 会绕过 driver，不算完。
5. **ELF**：`.attribute 5` 有 `_zfoo...`，`e_flags` 没被意外改掉（Ch 22）；用 `llvm-readobj -A` 和 `llvm-readelf -h` 各看一遍。

---

## 24.10 源码落点

| 主题 | 文件:行 | 说明 |
|---|---|---|
| `-march` 解析入口 | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:27` | `getArchFeatures` → `parseArchString` |
| `-march` 默认顺序 | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:305` | `getRISCVArch`，四步优先级 |
| `-mabi` 默认顺序 | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:222` | `getRISCVABI`，显式 > arch > triple |
| `-mcpu` / `-mtune` | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:409`、`:432` | `getRISCVTargetCPU`、`getRISCVTuneCPU` |
| feature append 顺序 | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp:69-220` | arch → CPU → relax → tune → 显式 |
| cc1 参数拼接 | `clang/lib/Driver/ToolChains/Clang.cpp:1600`、`:2090`、`:6523` | `getTargetFeatures`、`AddRISCVTargetArgs`、`-target-cpu` |
| code model 映射 | `clang/lib/Driver/ToolChains/CommonArgs.cpp:3093` | `medlow→small`、`medany→medium` |
| TargetInfo feature map | `clang/lib/Basic/Targets/RISCV.cpp:342`、`:414` | `initFeatureMap`、`handleTargetFeatures` |
| ABI 推导表 | `llvm/lib/TargetParser/RISCVISAInfo.cpp:1102` | `computeDefaultABI` |
| ISA 字符串解析 | `llvm/lib/TargetParser/RISCVISAInfo.cpp:569` | 实验扩展在 `:375` |
| CPU 默认 ISA | `llvm/lib/TargetParser/RISCVTargetParser.cpp:108` | `getMArchFromMcpu` |
| 后端 ABI 校验 | `llvm/lib/Target/RISCV/RISCVSubtarget.cpp:88` | note + 回落 |
| `.attribute` / `e_flags` | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp:101`、`:110` | `finishAttributeSection`、`finish` |
| 选项定义 | `clang/include/clang/Options/Options.td:5612`、`:5662`、`:5878`、`:6663`、`:7263` | `mabi`、`march`、`menable...`、`mtune`、`mcpu` |

测试入口：`clang/test/Driver/riscv-arch.c`（`invalid arch name`、实验扩展）、`riscv-abi.c`（ABI 默认值与 `unknown target ABI`）、`riscv-cpus.c`（`-mcpu`）、`riscv-mtune-tune-features.c`（`:3-12`）、`riscv-mcmodel.c`。

---

## 24.11 可跑实验

先准备 `printf 'int f(int a,int b){return a+b;}\n' > x.c`；命令在 `/tmp` 执行，feature 输出为可读性省略重复的 `"-target-feature"` 前缀，计数与内容实测。

### 实验 1：完整 cc1 命令行

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -c x.c
```

实测片段（省略其余 270 多个负向 `-target-feature`）：

```text
".../clang-24" "-cc1" "-triple" "riscv64-unknown-unknown-elf" "-emit-obj" \
  "-mrelocation-model" "static" "-target-cpu" "generic-rv64" \
  "-target-feature" "+i" "-target-feature" "+m" "-target-feature" "+a" \
  ... "-target-feature" "+zcd" "-target-feature" "+relax" \
  "-target-abi" "lp64d" ... "-x" "c" "x.c"
```

正项 14 个，总 `-target-feature` 286 个（其余是显式负项，用来把 CPU 默认 feature 全关掉）。`-target-abi lp64d` 是 driver 从 `-mabi` 直接抄来的。

### 实验 2：换 triple，对比默认 `-march` / `-mabi`

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -c x.c 2>&1 | grep -o '"-target-abi" "[^"]*"\|"-mrelocation-model" "[^"]*"' | tr '\n' ' '
$T/bin/clang -### --target=riscv64-linux-gnu -c x.c 2>&1 | grep -o '"-target-abi" "[^"]*"\|"-mrelocation-model" "[^"]*"' | tr '\n' ' '
```

```text
"-mrelocation-model" "static" "-target-abi" "lp64"
"-mrelocation-model" "pic" "-target-abi" "lp64d"
```

上为 elf、下为 linux。裸机默认 `rv64imac`（只有 `+i +m +a +c`），Linux 默认 `rv64imafdc`（多 `+f +d +zicsr +zcd`）。**同一份源码，什么也没写，ABI 已经不同。**

### 实验 3：`-march=rv64gc_zba` 的 feature 列表

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -march=rv64gc_zba -mabi=lp64d -c x.c 2>&1 \
  | grep -o '"-target-feature" "+[^"]*"' | tr '\n' ' '
# +i +m +a +f +d +c +zicsr +zifencei +zmmul +zaamo +zalrsc +zca +zcd +zba +relax
```

`+zba` 是唯一新增正项；`+relax` 不是 ISA 扩展，是 `-mrelax` 的默认值。

### 实验 4：`rv64imac + lp64d` 的 note、代码生成与 `e_flags`

```bash
printf 'double id(double x){return x;}\n' > id.c
$T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -S id.c -o -
# note: hard-float 'd' ABI can't be used for a target that doesn't support
#       the D instruction set extension (ignoring target-abi)
# 汇编：sd a0, 8(sp) / ld a0, 8(sp)
```

对比 `-march=rv64gc` 的 `fsd fa0, 8(sp)` / `fld fa0, 8(sp)`。再看 ELF：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -c id.c -o id.o
$T/bin/llvm-readelf -h id.o | grep Flags
#   Flags: 0x5, RVC, double-float ABI
```

note 说「忽略 target-abi」，`e_flags` 仍是 `double-float ABI`。**编译成功，不代表 ABI 一致。**

### 实验 5：`-mcpu=sifive-u74` vs `-mcpu=rocket-rv64`

```bash
$T/bin/clang -### --target=riscv64-unknown-elf -mcpu=sifive-u74 -c x.c 2>&1 | grep -o '"-target-cpu" "[^"]*"\|"-target-feature" "+[^"]*"' | tr '\n' ' '
$T/bin/clang -### --target=riscv64-unknown-elf -mcpu=rocket-rv64 -c x.c 2>&1 | grep -o '"-target-cpu" "[^"]*"\|"-target-feature" "+[^"]*"' | tr '\n' ' '
```

```text
"-target-cpu" "sifive-u74" +i +m +a +f +d +c +zicsr +zifencei +zmmul +zaamo +zalrsc +zca +zcd +relax
"-target-cpu" "rocket-rv64" +i +zicsr +zifencei +relax
```

`rocket-rv64` 的 `DefaultMarch` 只有 `rv64i`，所以连 `m/a/c` 都没有；不是 driver 丢了 feature。

### 实验 6：`-mtune` 只改调度（普通 CPU 名）

```bash
cat > tune.ll <<'IR_EOF'
define i64 @f(ptr %p, i64 %a, i64 %b) {
  %x = load i64, ptr %p
  %q = getelementptr i64, ptr %p, i64 1
  %y = load i64, ptr %q
  %m = mul i64 %x, %a
  %n = add i64 %m, %y
  %r = add i64 %n, %b
  ret i64 %r
}
IR_EOF
$T/bin/llc -mtriple=riscv64 -mcpu=sifive-u74 -mtune=sifive-u74 -O2 tune.ll -o u74.s
$T/bin/llc -mtriple=riscv64 -mcpu=sifive-u74 -mtune=rocket-rv64 -O2 tune.ll -o rocket.s
diff -u u74.s rocket.s
```

实测 diff（只有指令顺序变化）：

```diff
 	ld	a4, 0(a0)
-	addi	a0, a0, 8
 	slli	a5, a4, 1
 	add	a2, a2, a4
 	add	a2, a2, a5
 	addi	a3, a3, 1
+	addi	a0, a0, 8
 	blt	a3, a1, .LBB0_2
```

`-tune-cpu rocket-rv64` 没有改 `-target-cpu`，也没有改 `.attribute`。例外是 `-mexperimental-mtune-syntax` 的 `-mtune=sifive-x390:full-vec-fp64`，它会插入 `-target-feature -single-element-vec-fp64`（`riscv-mtune-tune-features.c:3-12`）。

### 实验 7：非法 `-march`（以及 `rv64gq` 其实合法）

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gqq -c x.c
# clang: error: invalid arch name 'rv64gqq', duplicated standard user-level extension 'q'

$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc_xfoo -c x.c
# clang: error: invalid arch name 'rv64gc_xfoo',
# unsupported non-standard user-level extension 'xfoo'

$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc_zba9 -c x.c
# clang: error: invalid arch name 'rv64gc_zba9',
# unsupported version number 9 for extension 'zba'
```

`rv64gq` **不是**非法值：`q` 是标准单字母扩展，实测被接受并产生 `+q`。写负例不要照抄「`rv64gq` 应该报错」；用 `rv64gqq`（重复）或 `rv64gc_xfoo`（未知厂商扩展）。

### 实验 8：实验扩展 `zibi` 的前后差异

```bash
$T/bin/clang --target=riscv32-unknown-elf -march=rv32izibi0p1 -c x.c
# clang: error: invalid arch name 'rv32izibi0p1',
# requires '-menable-experimental-extensions' for experimental extension 'zibi'

$T/bin/clang -### --target=riscv32-unknown-elf -march=rv32izibi0p1 \
  -menable-experimental-extensions -c x.c 2>&1 \
  | grep -o '"-target-feature" "+[^"]*"' | tr '\n' ' '
# +i +experimental-zibi +experimental +relax
```

`+experimental` 是全局开关，`+experimental-zibi` 是具体扩展；两者缺一，后端都不会启用实验扩展。

`-mcmodel`/`-mno-relax` 的转发已在 24.7 实测；本章唯一没有重跑的是 lld 的 ABI 冲突，它已在 Ch 22 实测，这里只引用结论。

## 24.12 踩雷集锦

1. **裸机默认 `-march` 不是 `rv64gc`。** `riscv64-unknown-elf` 走 `UnknownOS`，默认 `rv64imac` + `lp64`；`riscv64-linux-gnu` 才是 `rv64imafdc` + `lp64d`。看到「为什么没有 `f`/`d`」先查 triple。
2. **`-march` 与 `-mabi` 互为默认。** 不写 `-mabi` 由 `-march` 推；不写 `-march` 由 `-mabi` 推。只改一个，另一个可能跟着变。
3. **`-march` 与 `-mcpu` 同时出现时各管一半。** `-march` 决定 ISA feature，`-mcpu` 决定 `-target-cpu`；不要以为后写的 `-mcpu` 会覆盖 `-march`。
4. **feature 列表有隐含，也有覆盖。** `g = imafd_zicsr_zifencei`，本树还物化 `m→zmmul`、`a→zaamo/zalrsc`、`c→zca`（有 `d` 时 `zcd`），写 `.attribute` 检查不要只比对四个字母；显式 feature 最后 append（driver 的 `-m...` 在 tune feature 之后，llc 的 `-mattr` 在 CPU 之后），`-mcpu=sifive-u74 -mattr=-c` 真的会关掉 `c` 与 `zcd`。
5. **实验扩展必须「flag + 精确版本号」。** 只写 `rv32izibi` 报缺版本号；写 `0p7` 报版本不支持；只有 `-menable-experimental-extensions` + `0p1` 才通过。错误都在 driver。
6. **note 不阻止编译，但会留下 ABI 不一致。** `rv64imac + lp64d` 只打 note，代码生成按 `lp64`（`a0` 传 double），`e_flags` 却写 `0x5`（`lp64d`）。这是 Ch 22 链接问题的源头。
7. **`-###` 不执行后端，也不校验 `-mabi`/`-mtune`；别把 `rv64gq` 当非法值。** `-mabi=bogus` 在 `-###` 里原样出现；`-mtune=nope` 也能打印 `-tune-cpu nope`；`-mcpu=nope` 反而在 driver 报错。`q` 是标准扩展，要测非法值用 `rv64gqq` 或 `rv64gc_xfoo`。
8. **`-mno-relax` 与 `.option norelax` 是两条独立通道。** 前者在 driver 变成 `-target-feature -relax` 并给链接器加 `--no-relax`；后者是汇编器 MC 层状态，绕过 driver。只改 driver 不会影响手写汇编里的 `.option norelax`（Ch 22）。

---

## 24.13 本章重点整理

- **driver 是唯一同时看到 triple、`-march`、`-mabi`、`-mcpu`、`-mtune` 的地方。** 它把输入压成一个 `-target-feature` 列表 + `-target-cpu` + `-target-abi`；后端只认这三样。
- **`getRISCVArch` 四步**：显式 `-march` > `-mcpu` 默认 ISA > `-mabi` 反推 > triple。**`getRISCVABI` 三步**：显式 `-mabi` > `computeDefaultABI` > triple。两者互为默认。
- **裸机默认 `rv64imac`/`lp64`，Linux 默认 `rv64imafdc`/`lp64d`。** `rv64gc` 通常由用户显式写出。
- **`g` 展开为 `imafd_zicsr_zifencei`，并带出 `zmmul`/`zaamo`/`zalrsc`/`zca`/`zcd`；`v` 带出整组 `zve*` 与 `zvl*`。**
- **`-mcpu` 既提供默认 ISA，又决定 `-target-cpu`；`-march` 与 `-mcpu` 同时给出时，ISA 归 `-march`，CPU 模型归 `-mcpu`。**
- **`-mtune` 对普通 CPU 名只换调度模型；`-mexperimental-mtune-syntax` 的 tune feature 会以 `-target-feature` 进入 cc1。**
- **`-mabi` 在 driver 不被校验。** `bogus`/`ilp32` 由 cc1 报 `unknown target ABI`；`rv64imac + lp64d` 由后端打 note 并回落，但 `e_flags` 仍写 `lp64d`。
- **`-mcmodel` 映射为 `medlow→small`、`medany→medium`；`-mrelax`/`-mno-relax` 映射为 `±relax` feature，并同时影响链接行。**
- **验收扩展改动要跑五步**：driver 解析、cc1 参数、TargetInfo 宏/builtin、后端选指、`.attribute`/`e_flags`。只跑 `llc -mattr` 会漏掉 driver。

---

## 24.14 自我检核

- [ ] 能画出 `clang -march=... -mabi=...` 的五段旅程，并说出每段在哪个文件、失败时报什么错
- [ ] 能背出 `getRISCVArch` 的四步与 `getRISCVABI` 的三步，并解释为什么二者会互相推导
- [ ] 能说出 `riscv64-unknown-elf` 与 `riscv64-linux-gnu` 的默认 `-march`/`-mabi`/relocation model 差异
- [ ] 能解释 `rv64gc_zba` 的三段与 `g`/`v` 隐含扩展，以及 `-march=rv64gc -mcpu=rocket-rv64` 为什么既有 `m/a/f/d` 又是 rocket 的 CPU 模型
- [ ] 能复现 `rv64imac + lp64d` 的 note、`a0` vs `fa0` 的汇编差异，以及 `e_flags 0x5` 的矛盾
- [ ] 能用 `-###`、`llvm-readobj -A`、`llvm-readelf -h`、`--print-multi-directory` 各取一份证据
- [ ] 能说出 `-mtune` 与 `-mexperimental-mtune-syntax` 的区别，以及 `-mno-relax` 的两条作用通道
- [ ] 能写出至少三个非法 `-march` 负例，并知道 `rv64gq` 不是其中之一

---

## 24.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChains/Arch/RISCV.cpp`**：读 27–57、69–220、222–281、305–407、409–476。本章主线；注释保留了 GCC 默认值的历史原因。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChains/Clang.cpp`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChains/CommonArgs.cpp`**：读 1600、2090–2135、6502、6523 与 3093–3134；配合 **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/include/clang/Options/Options.td`** 的 5612/5662/5878/6663/7263 看选项定义与 Group。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/TargetParser/RISCVISAInfo.cpp`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/TargetParser/RISCVTargetParser.cpp`**：读 286–315（正负项生成）、375–395（实验扩展）、569–650（`parseArchString`）、1102–1128（`computeDefaultABI`）、108–140（`getMArchFromMcpu`）。配套测试在 **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/test/Driver/riscv-arch.c`、`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/test/Driver/riscv-abi.c`、`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/test/Driver/riscv-cpus.c`、`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/test/Driver/riscv-mtune-tune-features.c`、`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/test/Driver/riscv-mcmodel.c`**；`riscv-arch.c:377-411` 是实验扩展负例的模板。

→ 下一章：[Ch 25 — Clang TargetInfo 与 builtins](./25-clang-targetinfo-and-builtins.md)
