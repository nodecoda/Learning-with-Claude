# Ch 24 — Clang driver：`-march`、`-mabi`、`-mfpu` 与默认值

> 本章目标：能说清 `clang --target=loongarch64` 到底给 `cc1` 传了什么；知道 ABI 与 feature 的优先级链；能解释本树里三个真实反直觉现象（`generic-la64` 被前端拒绝、`-march=native` 在 x86 主机上解析成 `znver2`、LA32 默认走软浮点）。

---

## 24.1 driver 只做三件事

LoongArch 的 driver 逻辑几乎全在 `clang/lib/Driver/ToolChains/Arch/LoongArch.cpp`（本树 325 行），一共四个函数：

| 函数 | 作用 | 输出到 cc1 的参数 |
|---|---|---|
| `getLoongArchABI` | 决定 ABI 名字 | `-target-abi lp64d` |
| `getLoongArchTargetCPU` | 决定 CPU 名字 | `-target-cpu la664` |
| `getLoongArchTargetFeatures` | 决定 feature 集合 | `-target-feature +lsx` … |
| `postProcessTargetCPUString` | `native`/空值 → 具体 CPU | 同上 |

它**不生成指令、不检查编码**。真正的 ABI 合法性检查在 MC 层的 `llvm/lib/Target/LoongArch/MCTargetDesc/LoongArchBaseInfo.cpp`；两个位置都可能打印 warning，这是本章后半段几个“奇怪输出”的来源。

---

## 24.2 ABI 的优先级链

`getLoongArchABI`（`LoongArch.cpp:23`）的顺序是硬编码的，从上到下依次抢占：

1. `-mdouble-float` / `-msingle-float` / `-msoft-float`（最高优先，且会**覆盖**并警告冲突的 `-mabi=`/`-mfpu=`）
2. `-mabi=`
3. `-mfpu=64|32|0|none`
4. triple 的 environment 后缀（`gnusf` / `gnuf32` / `gnuf64` / `gnu`）
5. 兜底：`{ilp32,lp64}d`

实测（`clang --target=… -### -c x.c`，抓 `-target-abi`）：

| 命令行 | 实测 `-target-abi` |
|---|---|
| `--target=loongarch64-unknown-linux-gnu` | `lp64d` |
| `--target=loongarch64-unknown-linux-gnusf` | `lp64s` |
| `--target=loongarch64-unknown-linux-gnuf32` | `lp64f` |
| `--target=loongarch32-unknown-linux-gnu` | `ilp32d` |
| `-mfpu=32` | `lp64f`（同时 `+f -d -lsx`） |

冲突时是 **warning 而不是 error**：

```text
$ clang --target=loongarch64-unknown-linux-gnu -mabi=lp64d -msoft-float -### -c x.c
clang: warning: ignoring '-mabi=lp64d' as it conflicts with that implied by
'-msoft-float' (lp64s) [-Woption-ignored]
```

注意后一行最终是 `-target-feature -f -d -lsx`、`-target-abi lp64s`：软浮点会顺手把 LSX 关掉，因为 LSX 依赖 64 位 FPU（源码里三处 `err_drv_loongarch_wrong_fpu_width` 就是干这个的）。

---

## 24.3 默认 feature 集合长什么样

`getLoongArchTargetFeatures`（`LoongArch.cpp:144`）先塞两个默认值：

```cpp
// Enable the `lsx` feature on 64-bit LoongArch by default.
if (Triple.isLoongArch64() && (!Args.hasArgNoClaim(options::OPT_march_EQ)))
  Features.push_back("+lsx");

// -mrelax is default, unless -mno-relax is specified.
if (Args.hasFlag(options::OPT_mrelax, options::OPT_mno_relax,
                 Triple.isLoongArch64() ? true : false))
  Features.push_back("+relax");
else if (Args.getLastArg(options::OPT_mno_relax))
  Features.push_back("-relax");
```

实测 LA64 默认：

```text
-target-cpu loongarch64
-target-feature +lsx  +relax  +64bit  +f  +d  +ual
-target-abi lp64d
```

两个容易忽略的点：

- **`+lsx` 只在没有 `-march=` 时默认打开**。一旦写了 `-march=loongarch64`，这一行不再执行，`+lsx` 改由 `LoongArchTargetParser` 的 arch feature 表决定（`loongarch64` 只给 `+64bit +f +d +ual`，**没有 LSX**）。
- `-mno-relax` 不是“不传参数”，而是显式传 `-target-feature -relax`；这个 feature 最终决定汇编器要不要发 `R_LARCH_RELAX`（见 24.6）。

---

## 24.4 `-march=`：三个不同的东西共用一个选项

这是本章最容易踩坑的地方。`-march=` 在这里可以接受三类值，但它们的处理路径不同：

| 输入 | `getLoongArchTargetCPU` 结果 | 实测 `-target-cpu` | 说明 |
|---|---|---|---|
| `loongarch64` | 原样 | `loongarch64` | 在 `LoongArchTargetParser.def` 的 `AllArchs` 里 |
| `la64v1.0` | 换成默认 CPU | `loongarch64` | 命中硬编码分支，features 另算 |
| `la64v1.1` | 换成默认 CPU | `loongarch64` | 额外 `+frecipe +lam-bh +lamcas +ld-seq-sa +div32 +scq` |
| `la664` | 原样 | `la664` | CPU 名，features 由 arch 表给全 |
| `generic-la64` | 原样 | `generic-la64` | **前端随后拒绝** |

实测 `-march=la64v1.0` vs `-march=la64v1.1` 的 feature 差异：

```text
la64v1.0: +relax +64bit +d +lsx +ual
la64v1.1: 上面全部 + +frecipe +lam-bh +lamcas +ld-seq-sa +div32 +scq
```

### 坑 1：`generic-la64` 是后端 CPU，不是 `-march` 值

```text
$ clang --target=loongarch64-unknown-linux-gnu -march=generic-la64 -O2 -S v.c
error: unknown target CPU 'generic-la64'
note: valid target CPU values are: loongarch32, loongarch64, la464, la664

$ llc -mtriple=loongarch64 -mcpu=generic-la64 v.ll     # 成功
$ clang --target=loongarch64-unknown-linux-gnu --print-supported-cpus
	generic
	generic-la32
	generic-la64
	la464
	la664
	loongarch32
	loongarch64
```

三处不一致：

- `--print-supported-cpus` 列的是 **TableGen `ProcessorModel`**（`LoongArch.td:185-200`），包含 `generic*`。
- `clang/lib/Basic/Targets/LoongArch.cpp:487` 的 `isValidCPUName` 却调 `llvm::LoongArch::isValidCPUName`，后者 `return isValidArchName(Name)`，只认 `LoongArchTargetParser.def` 里的 4 个名字。
- driver 又直接把 `-march=` 的值当 `-target-cpu` 透传，于是“driver 接受、前端报错”。

**这是可以做成 patch 的一致性问题**：要么让 `isValidCPUName` 用 CPU 列表，要么让 driver 拒绝 `generic-la64`。

### 坑 2：`-march=native` 会解析成主机 CPU

```text
$ clang --target=loongarch64-unknown-linux-gnu -march=native -### -c x.c
-target-cpu znver2
-target-feature +relax +prfchw … +avx2 …   # 全是 x86 feature
```

因为 `postProcessTargetCPUString` 对 `native` 调的是 `llvm::sys::getHostCPUName()`，**不看 target triple**。在 x86 上交叉编译 LoongArch 时写 `-march=native` 是纯粹的灾难（后端的 CPU 校验会先报错，但那已经是下一层了）。

---

## 24.5 `-mabi=` 的警告为什么会出现好几次

实测 `-mabi=lp64f`（本树）：

```text
$ clang --target=loongarch64-unknown-linux-gnu -mabi=lp64f -S x.c
2 warning: 'lp64f' has not been standardized
2 warning: triple-implied ABI conflicts with provided target-abi 'lp64f', using target-abi

$ … -c x.c
3 warning: 'lp64f' has not been standardized
3 warning: triple-implied ABI conflicts with provided target-abi 'lp64f', using target-abi
```

次数随编译阶段变化：`-fsyntax-only` 0 次、`-S` 2 次、`-c` 3 次。两条字符串分别来自 `LoongArchBaseInfo.cpp:43` 和 `:100`，即 **MC/TargetMachine 初始化路径每走一次就打一遍**；`-fsyntax-only` 不建后端所以一条都没有。这不是“重复 bug”，而是分层校验的可观测后果——但写 lit 测试时要小心 `CHECK` 的次数。

非法值则是硬错误：

```text
$ clang --target=loongarch64-unknown-linux-gnu -mabi=bogus -c x.c
error: unknown target ABI 'bogus'
```

---

## 24.6 `-mrelax` 到底改变了什么

`+relax` / `-relax` 只影响汇编器是否在每条可松弛重定位后追加 `R_LARCH_RELAX`。实测同一段代码：

```text
$ clang --target=loongarch64-unknown-linux-gnu -O2 -c g.c        # 默认 +relax
$ llvm-readobj -r g_relax.o
    0x0 R_LARCH_GOT_PC_HI20 ext 0x0
    0x0 R_LARCH_RELAX - 0x0
    0x4 R_LARCH_GOT_PC_LO12 ext 0x0
    0x4 R_LARCH_RELAX - 0x0

$ clang --target=loongarch64-unknown-linux-gnu -O2 -mno-relax -c g.c
$ llvm-readobj -r g_norelax.o
    0x0 R_LARCH_GOT_PC_HI20 ext 0x0
    0x4 R_LARCH_GOT_PC_LO12 ext 0x0
```

这就是 Ch 12 里 lld 状态机读的那对“普通重定位 + 同 offset 标记”。`LoongArchMCCodeEmitter.cpp:141` 的 `EnableRelax = STI.hasFeature(LoongArch::FeatureRelax)` 是这条链的起点。

---

## 24.7 反直觉：LA32 默认是软浮点

`--target=loongarch32-unknown-linux-gnu` 的 driver 默认给出 `-target-abi ilp32d`，但 feature 只有 `+32bit`（没有 `+f`/`+d`）。后端于是认为 `ilp32d` 对当前 feature 非法：

```text
$ clang --target=loongarch32-unknown-linux-gnu -O2 -S fp.c
warning: both target-abi and the triple-implied ABI are invalid,
         ignoring and using feature-implied ABI
	addi.w	$sp, $sp, -16
	st.w	$ra, $sp, 12
	bl	__addsf3
```

原因在 `computeTargetABI`（`LoongArchBaseInfo.cpp:71`）：

```cpp
case ABI_ILP32D:
  return !Is64Bit && FeatureBits[LoongArch::FeatureBasicD];
```

默认 CPU `loongarch32` 在 `LoongArchTargetParser.def` 里只映射 `FK_32BIT`，没有 `FK_FP64`，所以 `ilp32d` 被判非法，回退到 feature-implied ABI（软浮点）。加 `-mfpu=64` 后立刻变成 `fadd.s $fa0, $fa0, $fa1`。

**这是“driver 默认值与后端 feature 表不匹配”的一个真实缺口**，也是很好的第一个 upstream issue 素材：要么让默认 CPU 带 `+d`，要么让 driver 不要在 LA32 上默认 `ilp32d`。

---

## 24.8 multilib 与 sysroot

本树构建没有 multilib 配置：

```text
$ clang --target=loongarch64-unknown-linux-gnu -print-multi-lib
$ clang --target=loongarch64-unknown-linux-gnu -print-multi-directory
.
```

交叉链接需要显式给 `--sysroot`/`--gcc-toolchain`（练习 E 会用到本机的 LoongArch 交叉 gcc）。driver 的 `Gnu.cpp`/`Linux.cpp` 里 LoongArch 走的是通用 GNU 工具链探测，没有 LoongArch 专属 multilib 逻辑。

---

## 24.9 反直觉点汇总

1. **`-march=generic-la64` 被前端拒绝**，但 `llc -mcpu=generic-la64` 正常——三份 CPU 名单不一致。
2. **`-march=native` 不看 triple**，在 x86 上交叉编译会拿到 `znver2` + AVX2。
3. **`-march=loongarch64` 会关掉默认的 `+lsx`**（因为默认 `+lsx` 只在“没有 `-march`”时添加）。
4. **`-mno-relax` 不是省略参数**，而是显式 `-relax`，会改变 `.o` 里的重定位条数。
5. **ABI 警告次数取决于编译阶段**（0/2/3 次），写 FileCheck 不能假设只有一条。
6. **LA32 默认 ABI 名义上是 `ilp32d`，实际落到软浮点**并打两条 warning。
7. **`-msoft-float` 会连带关掉 LSX**，不是只关 FP 指令。

---

## 24.10 本章证据

| 结论 | 源码/命令 |
|---|---|
| ABI 优先级链 | `clang/lib/Driver/ToolChains/Arch/LoongArch.cpp:23-140` |
| 默认 `+lsx` / `+relax` | 同文件 `:144-160` |
| CPU 名解析 | 同文件 `:290-325` |
| CPU 名单不一致 | `Targets/LoongArch.cpp:487`、`LoongArchTargetParser.def`、`LoongArch.td:185-216` |
| ABI 合法性检查 | `MCTargetDesc/LoongArchBaseInfo.cpp:71-120` |
| relax → 重定位 | 本章 24.6 的 `llvm-readobj -r` 实测 |
| LA32 软浮点 | 本章 24.7 实测 |
| driver 测试文件 | `clang/test/Driver/loongarch-abi.c`、`loongarch-march.c`、`loongarch-relax-features.c` 等 29 个 |

---

## 24.11 练习

1. 用 `-###` 抓出 `--target=loongarch64-unknown-linux-gnu -march=la464 -mno-lasx` 的完整 feature 列表，解释为什么 `-lasx` 出现在最后。
2. 写一个 `clang/test/Driver/loongarch-march.c` 风格的 lit 测试，覆盖 `-march=loongarch64` 不带 `+lsx` 的行为。
3. 给 `generic-la64` 的不一致写一份最小修复方案（改 `isValidCPUName` 还是改 driver？），并列出会影响的测试。
4. 验证 `-mabi=ilp32d --target=loongarch32` 加上 `-mattr=+d` 后 warning 是否消失（提示：driver 没有 `-mattr`，要用 `-Xclang -target-feature -Xclang +d`）。
5. 用 `-print-multi-lib` 和 `--gcc-toolchain` 组合，找出本机交叉 gcc 的 sysroot 路径。

---

## 24.12 延伸阅读

1. **Target triple 与环境后缀** — `llvm/docs/LangRef.md`
   - 读什么：`### Target Triple` 一节，重点看 `ARCHITECTURE-VENDOR-OPERATING_SYSTEM-ENVIRONMENT` 的规范形式。
   - 学到什么：triple 的第四段 environment 如何承载 ABI 默认值；本章 24.2 的 `gnusf`/`gnuf32`/`gnuf64` 是这一机制在 LoongArch 上的实例，读完能把“只写 `--target=` 就得到 `lp64s`”放回 triple 解析的上下文。
   - 前提：读过 24.1–24.2，知道 `clang --target=` 与 `-target-abi` 的区别。

2. **Subtarget feature 的生成机制** — `llvm/docs/WritingAnLLVMBackend.md`
   - 读什么：`## Subtarget Support` 一节，重点是 `SubtargetFeature` 的五个字段、`Proc`/`Processor` 示例，以及 `ParseSubtargetFeatures` 的调用顺序。
   - 学到什么：`-mcpu=`/`-mattr=` 如何变成 feature 位；本章的默认 `+lsx`、`-mno-relax` 的显式 `-relax`、以及 `-msoft-float` 连带关掉 LSX，都能在这套“feature 声明 + 依赖列表”的机制里找到位置。
   - 前提：读过 24.3–24.4，知道 `-target-feature` 是 driver 传给 cc1 的参数。

3. **后端 CPU 表与 driver 名单的错位** — `llvm/lib/Target/LoongArch/LoongArch.td`
   - 读什么：文件末尾 `// LoongArch processors supported.` 之后的 `ProcessorModel` 定义，逐个对照 `generic-la64`、`loongarch64`、`la464`、`la664` 的 feature 列表。
   - 学到什么：后端侧 CPU→feature 的权威表。`generic-la64` 在后端有定义且带 `FeatureExtLSX`，而 `loongarch64` 只带 `Feature64Bit`/`FeatureUAL`/`FeatureBasicD`、没有 LSX；这正是本章“三份 CPU 名单不一致”中后端那一份，能帮助判断该改 driver、TargetParser 还是 `.td`。
   - 前提：能读基本的 TableGen `def`/`ProcessorModel` 语法（Ch 04）。

4. **relax 在链接器侧的另一半** — `compilers/elf_linking/README.md`（相对 `/home/ubuntu/Learning-with-Claude`）
   - 读什么：`compilers/elf_linking/README.md` 的课程地图中，Part 2（Static Linking）的 Ch 6「RISC-V 专属 relocation 与 linker relaxation」、Ch 7「R_RISCV_ALIGN 与 relaxation 的副作用」，以及 Part 5（进阶议题）的练习 B。
   - 学到什么：本章 24.6 只讲到汇编器在可松弛重定位后追加 `R_LARCH_RELAX`；沿着这两章可以补上链接器侧的两个问题：relaxation 如何被消费、以及对齐与 relaxation 的相互影响。用 RISC-V 对照，能更清楚地看 LoongArch 的同类状态机。
   - 前提：读过本章 24.6 和 Ch 12 的重定位/relaxation；接受用 RISC-V 做类比。

## 24.13 下一步

driver 只决定“传什么”。下一章看这些开关在编译器内部落到哪：`TargetInfo` 的宏、三个 builtin 数据库，以及 builtin 名字如何自动映射成 `llvm.loongarch.*` intrinsic。
