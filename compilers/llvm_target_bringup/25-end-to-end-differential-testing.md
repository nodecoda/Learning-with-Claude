# Ch 25 — 端到端与差分测试

> **目标**：用 qemu、`start.s`、17 个程序与静态指令数基线，验证 MiniRV “不只是能生成指令”，而是和 RISC-V 参考后端跑出同样的可观察结果。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`（commit `6e7e7abeb2b7`）。clang 的实测使用 `/home/ubuntu/llvm/builds/pr-lane/bin/clang`，原因见 Ch 26。下文 `$B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`。

## 为什么需要这个？

lit + FileCheck 只能证明“生成的汇编文本里有某条指令”。它证明不了：

- 这条指令的寄存器分配在真实调用约定下是否正确；
- 栈帧大小、callee-saved 保存位置、栈上传参偏移是否与 ABI 一致；
- 链接器把重定位修好之后，机器码还能不能跑；
- 变长数组、varargs、跳转表这些“LLVM 语义 + ABI + 帧布局”共同作用的路径，最终是否给出同一个结果。

MiniRV 没有自己的 libc、没有自己的操作系统、也没有自己的 C 前端。要证明“跑对了”，最便宜、最可信的办法不是写一个 golden 输出，而是**差分测试**：同一份 LLVM IR，分别用 MiniRV 和成熟的 RISC-V 后端编译，链接同一个启动桩，在同一个模拟器里执行，比较退出码。

这个方法的本质是：

> 我们不需要先证明 RISC-V 后端绝对正确。我们只需要证明 MiniRV 在 17 组被选中的行为上，与 RISC-V 后端**没有分歧**。

它抓不到“两边一起错”的 bug，但它能抓住绝大多数 bring-up 阶段的真实错误：ABI 参数错位、栈帧算错、callee-saved 漏存、跳转表项宽度不对、varargs 保存区偏移错。

## 先建立直觉

把差分测试想成两个学生做同一张卷子，老师不公布标准答案，只检查两人答案是否一致。卷子是 `.ll`，两个学生是 `llc` 的两个 target，判卷方式是 qemu 的退出码。

```
                    dev/e2e/*.ll  (17 份同一 IR)
                       /                    \
                      /                      \
      llc -mtriple=minirv64          llc -mtriple=riscv64 -mattr=+m
             |                                |
        prog.m.o                          prog.r.o
             |                                |
             +---------> start.o <------------+
                       (llvm-mc -triple=riscv64)
                          |            |
                    ld.lld --no-relax  |
                          |            |
                    prog.m.elf     prog.r.elf
                          \            /
                    qemu-riscv64-static
                          |            |
                     exit code m   exit code r
                          \            /
                       m == r ?  OK / FAIL
```

注意几个刻意的选择：

- **同一个 `start.o`** 链两次，排除启动代码差异。
- **同一个模拟器** 跑两个 ELF，排除宿主差异。
- **只比较退出码**，不比较 stdout、不比较指令条数、不比较 ELF 大小。
- **不比较反汇编**：反汇编不同是正常的（跳转表项宽度、`ret` 伪指令、PC-relative 序列都可能不同），只有行为必须相同。

## 主线：为什么是这些参数

### 为什么比较退出码，而不是 stdout？

`start.s` 只做一件事：调用 `main`，然后把 `main` 的返回值当作进程退出码：

```
jal  ra, main      # ra = 返回地址
addi a7, zero, 93  # a7 = SYS_exit
ecall              # exit(a0)
```

没有 libc，没有 `printf`，没有 `write` 系统调用。要比较 stdout，我们得先实现格式化输出、缓冲区、`write`，那测的是运行时库，不是 MiniRV 的代码生成。退出码是这条最小运行时里唯一现成的、确定性的可观察量。

代价是：退出码只有 8 位。`qemu` 把 guest 的 `exit(status)` 传给宿主 shell，shell 看到的是 `status & 0xff`。所以：

- 300 和 44 会碰撞；
- 负数和它的低 8 位补码会碰撞；
- 崩溃也可能产生一个非零状态，和某个合法返回值碰撞。

因此每个程序都应该主动把结果折到 0–255，并且尽量让不同程序使用不同的返回值。`t150.ll` 就是这条通道的锚点：它只返回 150，证明“main 返回值 → a0 → exit → qemu → shell”整条链是通的。

### 为什么 `--no-relax`？

RISC-V 的链接器默认会做 relaxation：把 `auipc + addi` 合成更短的序列、把 `auipc + jalr` 换成 `jal`、把对齐填充删掉，等等。这些改写依赖 `R_RISCV_RELAX` 之类重定位，也可能假设 `gp`（global pointer）已经初始化。

MiniRV 的目标是 RV64IM 的一个最小子集：它没有压缩指令，也没有实现标准 RISC-V 的完整 relaxation 模型。如果让 `ld.lld` 自由改写，最终执行的字节就不再是 `llc` 生成的那段代码，差分测试会变成“测链接器 + 测 MiniRV 的混合体”。更糟的是，`start.s` 没有初始化 `gp`，任何 GP-relative relaxation 都可能直接跑飞。

所以两边都用：

```
ld.lld -m elf64lriscv --no-relax start.o prog.o -o prog.elf
```

这是一个**控制变量**：关掉链接期优化，让 MiniRV 的 codegen 成为唯一变量。

### 为什么 `start.s` 用 riscv64 汇编？

因为 MiniRV 的汇编器是故意裁剪的：没有 `ecall`，没有 `ra`/`a7`/`zero` 这些寄存器别名，也没有完整的伪指令支持。`start.s` 是测试脚手架，不是被测目标。为了让脚手架可读、可移植，用参考后端的汇编器：

```
$B/llvm-mc -triple=riscv64 -filetype=obj start.s -o "$OUT/start.o"
```

这也保证两个链接用的是**逐字节相同**的启动目标文件。如果为了测试去给 MiniRV 汇编器加 `ecall`，那是把“运行时需要”错误地变成“codegen 需要”，会污染 target 的范围。

### 为什么参考端要 `-mattr=+m`？

MiniRV 是 RV64IM，乘除法是硬件指令。`llc` 的 `riscv64` 默认却只有 RV64I：默认 CPU `generic-rv64` 不带 M。实测：

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ cd llvm/lib/Target/MiniRV/dev/bench
$ $B/llc -mtriple=riscv64 -O2 mul.ll -o - | grep -E '\.attribute|call|^[[:space:]]*mul[[:space:]]'
	.attribute	4, 16
	.attribute	5, "rv64i2p1"
	call	__muldi3
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 mul.ll -o - | grep -E '\.attribute|^[[:space:]]*mul[[:space:]]'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
	mul	a5, a5, a6
```

没有 `+m`，参考端会把 32 位乘法降级成 `__muldi3` libcall。e2e 没有链接 compiler-rt，所以要么链接失败，要么比较的是“libcall vs 硬件 mul”这两种完全不同的实现。加上 `+m`，两边才在同一个 ISA 假设下比较。

这里有一个很容易踩的坑：**clang driver 的 `--target=riscv64` 默认带 `+m`**（Ch 26 会看到 `-###` 里的 `-target-feature +m`），但 `llc` 不走 driver，默认不带。不要用 clang 的默认去推断 llc 的默认。

## 主线：逐行读 `dev/e2e/run.sh`

```bash
 1  #!/usr/bin/env bash
 2  # End-to-end differential test for MiniRV.
 3  #
 4  # Every .ll defines main and returns a small exit code.  The same IR is
 5  # compiled for MiniRV and for the RISC-V reference backend, linked against a
 6  # tiny start.s with ld.lld, run under qemu-riscv64-static, and the exit codes
 7  # are compared.
 8  #
 9  #   LLVM_BUILD_DIR=<build> llvm/lib/Target/MiniRV/dev/e2e/run.sh
10  set -euo pipefail
11  TREE=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
12  BUILD=${LLVM_BUILD_DIR:-/home/ubuntu/llvm/builds/dsl-tablegen}
13  B="$BUILD/bin"
14  LDLLD=${LDLLD:-ld.lld}
15  QEMU=${QEMU:-qemu-riscv64-static}
16  cd "$(dirname "$0")"
```

- 第 10 行：`set -euo pipefail`。任何未捕获的失败都会终止；`pipefail` 保证管道中间失败也算失败。
- 第 11 行：从脚本位置反推仓库根。这样脚本不依赖调用者的 cwd。
- 第 12–15 行：构建目录、`llc`/`llvm-mc` 所在目录、链接器、模拟器全部可覆盖。默认值只是方便本地跑，CI 可以注入自己的工具链。
- 第 16 行：切到 `e2e/`，后面 `*.ll` 才是相对这个目录的。

```bash
18  for tool in "$B/llc" "$B/llvm-mc" "$LDLLD" "$QEMU"; do
19    if ! command -v "$tool" >/dev/null 2>&1; then
20      echo "skip: $tool not found" >&2
21      exit 77
22    fi
23  done
```

- 第 18–23 行：预检。缺工具时退出 77，这是 lit 约定俗成的 “skip” 码，外层测试框架可以据此跳过而不是报失败。
- 注意 `command -v "$B/llc"` 对绝对路径也有效；它检查的是“可执行”，不是“在 PATH 里”。

```bash
25  OUT=$(mktemp -d)
26  echo "artifacts: $OUT"
```

- 第 25 行：每次运行一个临时目录，失败后可以保留现场手工分析。脚本不清理，方便复现。
- 第 26 行：把目录打印出来；这也是我们后面手工跑 ELF 的入口。

```bash
28  # start.s is plain RISC-V; the MiniRV assembler intentionally has no ecall or
29  # register aliases, so assemble it with the reference target.
30  "$B/llvm-mc" -triple=riscv64 -filetype=obj start.s -o "$OUT/start.o"
```

- 第 30 行：只汇编一次 `start.o`，两个链接共用。注释已经解释了为什么不用 MiniRV 汇编器。

```bash
32  fail=0
33  for f in *.ll; do
34    base=${f%.ll}
35    "$B/llc" -mtriple=minirv64 -O2 -filetype=obj "$f" -o "$OUT/$base.m.o" \
36      || { echo "FAIL $base: MiniRV compile"; fail=1; continue; }
37    "$B/llc" -mtriple=riscv64 -mattr=+m -O2 -filetype=obj "$f" \
38      -o "$OUT/$base.r.o" \
39      || { echo "FAIL $base: riscv64 compile"; fail=1; continue; }
```

- 第 33 行：shell glob 按文件名字典序展开，所以输出顺序稳定（`bf10` 在 `bf2` 前，因为字符 `1` < `2`）。
- 第 35–36 行：MiniRV 编译。失败就记 `fail=1` 并 `continue`，不中断其余程序——一次运行能看到所有编译失败，而不是只看到第一个。
- 第 37–39 行：参考端编译。`-filetype=obj` 直接出目标文件；`-O2` 两边一致，避免优化级别成为变量。

```bash
40    "$LDLLD" -m elf64lriscv --no-relax "$OUT/start.o" "$OUT/$base.m.o" \
41      -o "$OUT/$base.m.elf" \
42      || { echo "FAIL $base: MiniRV link"; fail=1; continue; }
43    "$LDLLD" -m elf64lriscv --no-relax "$OUT/start.o" "$OUT/$base.r.o" \
44      -o "$OUT/$base.r.elf" \
45      || { echo "FAIL $base: riscv64 link"; fail=1; continue; }
```

- 第 40–45 行：同一个链接器、同一个 emulation、同一个 `--no-relax`、同一个 `start.o`。唯一变量是 `.m.o` / `.r.o`。
- `-m elf64lriscv` 是显式的，不依赖 ld.lld 从输入文件猜。MiniRV 目标文件虽然是 `EM_RISCV`，但显式指定更稳。

```bash
47    "$QEMU" "$OUT/$base.m.elf" >/dev/null 2>&1 && m=0 || m=$?
48    "$QEMU" "$OUT/$base.r.elf" >/dev/null 2>&1 && r=0 || r=$?
49    if [ "$m" = "$r" ]; then
50      printf 'OK   %-16s exit=%s\n' "$base" "$m"
51    else
52      printf 'FAIL %-16s minirv=%s riscv64=%s\n' "$base" "$m" "$r"
53      fail=1
54    fi
55  done
56
57  exit "$fail"
```

- 第 47–48 行：`cmd && m=0 || m=$?`。`cmd` 成功时 `$?` 是 0；失败时 `$?` 是 qemu 报告的退出状态。`set -e` 不会在这里炸，因为整个 `&& ||` 列表的最后一个赋值总是成功。
- 第 49–54 行：只有退出码相等才 OK。注意脚本把 qemu 的 stdout/stderr 全部丢弃：诊断信息不属于比较对象。
- 第 57 行：只要有任一程序失败，脚本以 1 退出；全部成功是 0。

## 主线：逐行读 `dev/e2e/start.s`

```
1		.text
2		.globl _start
3	_start:
4		jal	ra, main
5		addi	a7, zero, 93
6		ecall
```

- 第 1 行：放进 `.text`。
- 第 2–3 行：导出 `_start`。`ld.lld` 在没有 `-e` 时默认入口就是 `_start`，所以链接命令不需要额外参数。
- 第 4 行：`jal ra, main`，把返回地址放进 `x1`。`main` 是普通 C ABI 函数，返回时执行 `jalr x0, 0(x1)`。
- 第 5 行：`a7 = 93`，Linux 的 `exit` 系统调用号。
- 第 6 行：`ecall`。内核/模拟器取 `a0` 作为退出码，进程结束，不返回。

这段桩没有：

- 初始化 `gp`；
- 初始化线程指针 `tp`；
- 保存/恢复任何寄存器；
- 设置栈（栈由内核在进程入口给好，`qemu-user` 会按 ABI 放置）。

所以它只适合“无 libc、无 TLS、无动态链接”的裸程序。也正因为没有 `gp`，`--no-relax` 不是可选项，而是正确性条件。

用 MiniRV 汇编器直接汇编它会失败，这不是 bug，而是范围声明：

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen/llvm/lib/Target/MiniRV/dev/e2e
$ $B/llvm-mc -triple=minirv64 -filetype=obj start.s -o /tmp/start_mini.o
start.s:4:6: error: invalid operand for instruction
        jal     ra, main
                ^
start.s:5:7: error: invalid operand for instruction
        addi    a7, zero, 93
                ^
start.s:6:2: error: invalid instruction mnemonic
        ecall
        ^
```

三条错误分别对应：`ra` 别名、`zero` 别名、`ecall` 指令。MiniRV 不实现它们，因为 `llc` 永远不会生成它们；运行时桩用参考汇编器是合理的边界。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 启动桩 | `dev/e2e/start.s` | `_start`、`a7=93`、`ecall` | 裸运行时唯一可观察量 |
| 驱动器 | `dev/e2e/run.sh` | `B`、`LDLLD`、`QEMU`、`fail` | 编译/链接/执行/比较四段 |
| 用例 | `dev/e2e/*.ll` | `main` | 每个文件一个 feature |
| 代码尺寸 | `dev/bench/run.sh` | `grep -cE`、`ratio` | 汇编行数，不是字节 |
| 参考测试 | `llvm/test/CodeGen/RISCV` | `RUN:` / `CHECK:` | 文本断言 |
| 执行测试 | `llvm/test/ExecutionEngine` | `%lli` | JIT/解释器模型 |
| MC 工具 | `llvm/test/tools/llvm-mc` | `RUN:` | 汇编器 CLI |
| 冒烟入口 | `dev/minirv-smoke.sh` | unittest、lit、e2e、bench | 一站式检查 |

## 17 个程序各覆盖什么

| # | 程序 | 覆盖点 | 实测 exit |
|---|---|---|---|
| 1 | `bf10` | 10×i64 栈数组，fill + sum 两个循环；小帧、基本寻址 | 108 |
| 2 | `bf2` | 300×i64（约 2400 B）大帧；超过 2047 的立即数拆分 | 21 |
| 3 | `bigframe` | 300×i64 大帧 + 调用 `helper`；大帧与 callee-saved spill 共存 | 42 |
| 4 | `bigframe2` | 与 `bigframe` **逐字节相同**（`diff` 返回 0）；当前不增加覆盖 | 42 |
| 5 | `edge` | i32 边界：`signext`、最高位、signed/unsigned 比较、移位、trunc、i8/i16 窄化 | 237 |
| 6 | `jt` | 密集 switch 与稀疏 switch 的跳转表；PC-relative 取表地址 | 129 |
| 7 | `misc` | `stacksave`/`stackrestore` 循环、`frameaddress`、`returnaddress` | 114 |
| 8 | `narrowstack` | 9 个 i8 / 9 个 i16 参数；第 9 个走栈，窄栈槽与符号扩展 | 223 |
| 9 | `selftest` | 算术混合、`sdiv`/`srem`、9 个 i32 参数（1 个栈参数）、平方和循环 | 66 |
| 10 | `t1` | 递归 + VLA；每层自己的动态 alloca | 20 |
| 11 | `t150` | 只返回 150；验证退出码通道本身 | 150 |
| 12 | `tc` | 尾调用；百万层递归不涨栈；带帧的间接尾调用 | 105 |
| 13 | `twoarr` | 两个 300×i64 数组（约 4800 B 帧）；两个大 alloca | 129 |
| 14 | `va` | varargs：寄存器保存区、栈上 varargs、i32 varargs、动态 alloca 下的 `va_start` | 184 |
| 15 | `vla` | VLA 求和；VLA 跨调用存活；VLA 与大固定帧共存 | 17 |
| 16 | `vla2` | 递归 VLA；16 字节对齐 VLA；VLA 跨多次调用存活 | 39 |
| 17 | `vla3` | 10 个参数 + VLA；栈参数与动态 alloca 同时存在 | 45 |

按主题归类：

- **varargs**：`va`。
- **跳转表**：`jt`。
- **递归**：`t1`、`tc`、`vla2`。
- **VLA / 动态 alloca**：`t1`、`vla`、`vla2`、`vla3`、`misc`。
- **frame address / return address / stacksave**：`misc`。
- **大帧**：`bf2`、`bigframe`、`twoarr`。
- **窄栈参数**：`narrowstack`、`selftest`、`vla3`。
- **i32 边界**：`edge`、`selftest`。

`bigframe2` 的重复值得单独说：它和 `bigframe.ll` 的内容完全一样，所以“17 个程序”实际只有 16 份不同 IR。它不有害，但会让人误以为多测了一种帧布局。读测试套件时要敢于做这种核对，而不是相信文件名。

## 实测：e2e 全绿

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
  QEMU=/usr/bin/qemu-riscv64-static \
  LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
  llvm/lib/Target/MiniRV/dev/e2e/run.sh
artifacts: /tmp/tmp.nQjW78JL5B
OK   bf10             exit=108
OK   bf2              exit=21
OK   bigframe         exit=42
OK   bigframe2        exit=42
OK   edge             exit=237
OK   jt               exit=129
OK   misc             exit=114
OK   narrowstack      exit=223
OK   selftest         exit=66
OK   t1               exit=20
OK   t150             exit=150
OK   tc               exit=105
OK   twoarr           exit=129
OK   va               exit=184
OK   vla              exit=17
OK   vla2             exit=39
OK   vla3             exit=45
```

这 17 行说明：在这 17 组 IR 上，MiniRV 和 RISC-V 参考后端的“机器码 + 链接 + ABI + 帧布局”组合给出了相同的 8 位结果。它不说明 MiniRV 的所有指令都正确，也不说明这些退出码是“程序的正确答案”。

一个具体例子：`bf10` 返回 108，而不是 `3*(0+…+9)=135`。看 IR 就知道，`done` 块用的是 `%s`，不是循环最后一次更新的 `%s1`，所以 108 是 IR 语义下的值。差分测试只保证两边一致；要判断“这个值是否符合预期”，得另设 golden 检查。这也是为什么 `t150` 这种常量锚点有价值。

## 实测：静态指令数基线

```console
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
  llvm/lib/Target/MiniRV/dev/bench/run.sh
| kernel | MiniRV insns | riscv64 insns | ratio |
|---|---|---|---|
| add | 12 | 12 | 1.00 |
| mul | 12 | 12 | 1.00 |
| reduction | 10 | 12 | 0.83 |
| memcpy | 9 | 9 | 1.00 |
| axpy | 12 | 12 | 1.00 |
```

`bench/run.sh` 做的事很朴素：对每个 kernel，用 `llc -O2` 输出汇编，然后 `grep -cE $'^\t[a-z]'` 数“以 tab 开头、后接小写字母”的行。这基本就是指令行，但会把标签、伪指令、注释排除掉。

这个基线证明：

- MiniRV 的标量循环能生成与参考后端**数量级相同**的代码；
- `add`/`mul`/`memcpy`/`axpy` 的指令数完全一致，说明基本 lowering 没有明显退化；
- `reduction` 是 10 比 12，MiniRV 反而少两条，这通常是因为参考端多了 `ret`/地址物化序列，而不是 MiniRV “更优”。

它**不**证明：

- 运行更快。指令数不是周期数，没算 cache、分支预测、流水线、访存延迟。
- 代码尺寸更小。数的是汇编行，不是 `.text` 字节数；伪指令展开、对齐、跳转表数据都不在里面。
- 任何性能结论。`bench/run.sh` 的注释自己写了 “not a performance claim”。

如果你要真的做性能比较，需要：固定 qemu 版本、用 `-icount` 或硬件周期计数器、区分 warmup、报告多次运行的分布。那是另一个章节的事。

## 实测：手工复现一个程序

下面的 `/tmp/tmp.nQjW78JL5B` 是本次运行的 artifacts 目录；你复现时替换成自己的。

```console
$ /usr/bin/qemu-riscv64-static /tmp/tmp.nQjW78JL5B/t150.m.elf; echo "minirv exit=$?"
minirv exit=150
$ /usr/bin/qemu-riscv64-static /tmp/tmp.nQjW78JL5B/t150.r.elf; echo "riscv64 exit=$?"
riscv64 exit=150
$ $B/llvm-objdump -d --triple=minirv64 /tmp/tmp.nQjW78JL5B/t150.m.elf
000000000001114c <_start>:
   1114c: ef 00 c0 00  	jal	x1, 12
   11150: 93 08 d0 05  	addi	x17, x0, 93
   11154: 73 00 00 00  	<unknown>

0000000000011158 <main>:
   11158: 13 05 60 09  	addi	x10, x0, 150
   1115c: 67 80 00 00  	jalr	x0, 0(x1)
```

这里有两个细节：

- `llvm-objdump` 对 `ecall` 显示 `<unknown>`，因为 MiniRV 的 disassembler 没有实现 `ecall`。链接后的 ELF 里它是标准 RISC-V 编码，qemu 能执行；disassembler 不认识不代表代码错。
- `jal x1, 12` 的目标是 `main`，偏移 12 字节；`main` 用 `addi x10, x0, 150` 把返回值放进 `a0`，再用 `jalr x0, 0(x1)` 返回。

再看一个“反汇编不同但行为相同”的例子。`jt.ll` 的跳转表：

```console
$ diff -u <($B/llc -mtriple=minirv64 -O2 -filetype=asm jt.ll -o -) \
          <($B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=asm jt.ll -o -) \
  | grep -E '^[-+][[:space:]]*(ld|lw|auipc|lui)' | head -4
-	auipc	x4, .LJTI0_0
-	ld	x3, 0(x3)
+	lui	a1, %hi(.LJTI0_0)
+	lw	a0, 0(a0)
$ diff -u <($B/llc -mtriple=minirv64 -O2 -filetype=asm jt.ll -o -) \
          <($B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=asm jt.ll -o -) \
  | grep -E '^-[[:space:]]*\.quad' | head -2
-	.quad	.LBB0_2
-	.quad	.LBB0_7
$ diff -u <($B/llc -mtriple=minirv64 -O2 -filetype=asm jt.ll -o -) \
          <($B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=asm jt.ll -o -) \
  | grep -E '^\+[[:space:]]*\.word' | head -2
+	.word	.LBB0_2
+	.word	.LBB0_7
```

`head` 只是为了截短输出；完整 diff 里两类表项成对出现。

MiniRV 的跳转表项是 8 字节，用 `ld` 取；参考端在 medlow 代码模型下用 4 字节项和 `lw`。两者都正确，因为表项宽度是 target lowering 的一部分。差分测试比较的是 `jt` 的 exit=129，不是这段反汇编。看到反汇编不同就喊 bug，是 bring-up 期最常见的误判之一。

## 怎么加一个新的 e2e 程序

1. 在 `dev/e2e/` 放一个 `foo.ll`。`main` 必须有，返回值最好是 `i32`；文件头写 `target triple = "minirv64"` 只是文档，`llc -mtriple` 会覆盖它。
2. 只测一个主题。比如“结构体按值传参”就单独一个文件，不要和 varargs 混在一起。失败时你能立刻定位。
3. 不要依赖 libc、`printf`、`malloc`、浮点格式化。e2e 只链接 `start.o` 和被测对象，没有 compiler-rt，也没有 libm。
4. 不要返回地址、指针、时间、随机数。qemu-user 的加载地址和 ASLR 策略会让结果不稳定。要测指针，返回“指针减基址”的偏移。
5. 把结果折到 8 位，例如：

   ```llvm
   %t = and i64 %result, 255
   %r = trunc i64 %t to i32
   ret i32 %r
   ```

6. 选一个非零、和其他程序不冲突的返回值。0 不是不能用，但“0”同时是“成功”的默认含义，失败时不容易区分“真的算出 0”和“提前正常退出”。
7. 跑一遍 harness，确认两边 OK；再手工跑一次 ELF，确认 exit 和预期一致。

### 返回码怎么选

- **优先携带信息**：`(a*31 + b*17 + c) & 255` 比 `ret i32 1` 好，因为不同的错误组合更容易得到不同结果。
- **避免碰撞**：8 位只有 256 个值。程序多了以后，两个不同结果折到同一个值是可能的。 collision 不一定致命（差分测试只要求两边相等），但会让你在调试时误判。
- **避免 0 和常见的崩溃码**：崩溃通常表现为非零（例如 139），但具体值依赖宿主和 qemu。选一个“有语义”的值，比选 0/1 更容易读日志。
- **显式截断**：不要依赖 C 的 `int` 到 `exit` 的隐式转换；在 IR 里就 `and` + `trunc`，让意图可见。
- **为锚点保留常量**：像 `t150` 这样只返回常量的程序，应该至少有一个。它不测试复杂逻辑，但能证明退出通道没坏。

## 怎么调试一个 mismatch

假设 `foo` 在 MiniRV 是 37，在 riscv64 是 41。按这个顺序查：

1. **先确认不是 harness 的问题**。手工跑两个 ELF，`echo $?`；确认 `+m` 在、`--no-relax` 在、`start.o` 是同一个。把 `qemu` 的 stderr 放开，看看有没有 “Illegal instruction”。
2. **分开看编译和链接**。重新执行 `llc` 和 `ld.lld`，确认失败发生在哪一步。链接错误通常是重定位或缺失符号（例如忘了 `+m` 导致 `__muldi3`）。
3. **对比反汇编，但只对比语义相关部分**：

   ```console
   $ $B/llvm-objdump -d --triple=minirv64 foo.m.elf > /tmp/m.dis
   $ $B/llvm-objdump -d --triple=riscv64 foo.r.elf > /tmp/r.dis
   $ diff -u /tmp/m.dis /tmp/r.dis | head -80
   ```

   重点看：prologue/epilogue 的 `sp` 调整、callee-saved 的存取偏移、栈参数的 `0(sp)` 偏移、varargs 保存区、跳转表项宽度、32 位值的符号扩展。
4. **看目标文件头和重定位**：

   ```console
   $ $B/llvm-readobj -h -r foo.m.o
   ```

   `Format: elf64-littleriscv`、`Machine: EM_RISCV` 应该和参考端一致；重定位类型应该是标准 RISC-V 的，而不是自定义的。
5. **缩小优化级别**。`-O0` 能排除调度/合并带来的干扰；如果 `-O0` 对、`-O2` 错，问题更可能在 RA、帧合并、尾调用或 branch folding。
6. **停下来看中间表示**。assertions 构建可以用：

   ```console
   $ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel foo.ll -o -
   $ $B/llc -mtriple=minirv64 -O2 -debug-only=isel,regalloc foo.ll -o /dev/null
   ```

   先确认 SelectionDAG 选出的指令是对的，再确认 RA 没有把值放到错误的寄存器。
7. **怀疑 ABI，不要怀疑“指令本身”**。bring-up 期最常见的错是：栈参数偏移少了一个槽、callee-saved 漏存、varargs 保存区按 4 字节而不是 8 字节对齐、VLA 把 `sp` 恢复早了。这些在单条指令层面都“合法”，只有跑起来才暴露。
8. **最后才怀疑 qemu 或参考端**。参考端漏 `+m`、`start.s` 被 MiniRV 汇编器错误处理、`--no-relax` 被去掉，都会制造假 mismatch。

## 和 RISC-V 自己的测试怎么分工

| 测试面 | 位置 | 规模（本树） | 证明什么 | 不证明什么 |
|---|---|---|---|---|
| CodeGen lit | `llvm/test/CodeGen/RISCV/*.ll` | 2247 个 `.ll` | ISel/ABI/帧布局的**文本形状**符合预期 | 机器码能链接、能执行 |
| MC lit | `llvm/test/MC/RISCV/*` | 622 个文件 | 汇编/反汇编/编码往返 | 运行时语义 |
| ExecutionEngine | `llvm/test/ExecutionEngine/*` | 约 500 个文件 | JIT/解释器执行 IR 的结果 | 交叉目标的真实链接与启动 |
| llvm-mc 工具测试 | `llvm/test/tools/llvm-mc/*` | 10 个文件 | 汇编器 CLI 行为 | target 的 ABI |
| MiniRV e2e | `dev/e2e/run.sh` | 17 个程序 | MiniRV 与参考后端在真实链接/执行下行为一致 | 绝对正确性、性能 |

`llvm/test/ExecutionEngine` 的 `MCJIT`/`Orc` 测试典型形式是：

```
; RUN: %lli -jit-kind=mcjit %s > /dev/null
; RUN: %lli %s > /dev/null
```

它要求“宿主能执行目标代码”。MiniRV 是交叉目标，lit 里没有 qemu 这一层，所以不能直接复用这个模型。e2e 补的正是“交叉 + 真实链接 + 真实执行”这一格。

RISC-V 上游的 CodeGen 测试是**模式断言**：`CHECK` 某条 `mul`、某个 `sd ra, 8(sp)`、某个 `addi sp, sp, -16`。它非常擅长防止“某次改动把指令序列改坏”，但它不会告诉你“这段序列在 qemu 里跑出来是不是同一个值”。两者不是替代关系，是金字塔的两层。lit/FileCheck 的细节见 [`../llvm_lit/README.md`](../llvm_lit/README.md)。

## 反直觉与踩雷

1. **错误直觉**：17 个程序都 OK，说明 MiniRV 的代码生成是对的。 → **正确认识**：只说明它和 RISC-V 参考后端在这 17 组行为上**没有分歧**。两边可能共享同一个 ABI 误解；退出码只有 8 位，也可能碰撞。`t1` 甚至把 `a[1]` 写进只有 `n` 个元素的 alloca，`n=1` 时越界；两边仍然同为 20，但这不是语义正确性。差分测试是回归网，不是正确性证明。
2. **错误直觉**：`bigframe2` 是第二个大帧测试，覆盖了不同的帧布局。 → **正确认识**：它和 `bigframe.ll` 逐字节相同（`diff` 退出 0），只是跑了两次。读测试套件要核对内容，不要只看文件名。
3. **错误直觉**：`-mattr=+m` 只是“让参考端也开 M”，可有可无。 → **正确认识**：`llc` 的 `riscv64` 默认是 `rv64i2p1`，没有 M 会生成 `__muldi3`；e2e 不链接 compiler-rt，直接链接失败。而 clang driver 的默认又带 `+m`，两个默认值不要混。
4. **错误直觉**：`--no-relax` 是保守选项，去掉也能跑。 → **正确认识**：`start.s` 没有初始化 `gp`，链接期 relaxation 可能把代码改写成依赖 `gp` 或压缩指令的序列；那样执行的就不是 `llc` 的输出。关掉它是为了让 MiniRV codegen 成为唯一变量。
5. **错误直觉**：`exit=108` 说明 `bf10` 算出了 108。 → **正确认识**：108 是 IR 里 `%s` 的值（前 9 个元素的和），不是 `3*(0+…+9)=135`。差分测试只比较两边；具体值要读 IR 或另设 golden。
6. **错误直觉**：反汇编里 MiniRV 用 `ld`、参考端用 `lw`，肯定有一个错。 → **正确认识**：跳转表项宽度是 target lowering 的选择，8 字节项 + `ld` 和 4 字节项 + `lw` 都可以正确。比较行为，不要比较指令形状。

## 进阶：再往深一层

- **加第三个 oracle**：如果构建了 `lli`，可以在宿主上直接解释同一份 IR，得到一个与 target 无关的结果。它对 `va.ll` 这种依赖 ABI 的程序不完全等价（`lli` 用宿主 ABI），但对纯算术/循环程序是一个独立检查。当前构建没有 `lli`，属于 guided。
- **`-O0` 对照**：把 e2e 跑成 `-O0` 和 `-O2` 两套。`-O0` 错、`-O2` 对，通常是 ISel/帧；反过来通常是 RA/调度/尾调用。
- **故障注入**：故意把 `--no-relax` 去掉、把 `+m` 去掉、把 `start.o` 换成 MiniRV 汇编，观察失败模式。知道“坏的时候长什么样”，调试时才不会误判。
- **CI 集成**：把 `exit 77` 当成 skip，把 `artifacts:` 目录作为失败附件保留；只在有 `ld.lld` 和 `qemu-riscv64-static` 的 job 里跑。
- **随机 IR**：用 `llvm-stress` 或自定义生成器产出大量小函数，做批量差分。前提是先把 ABI 固定下来，否则随机 IR 会把大量时间花在“未定义行为”上。

## 本章重点整理

- 差分测试的 oracle 是“另一个后端”，不是“标准答案”；它证明一致性，不证明绝对正确。
- 同一份 IR、同一个 `start.o`、同一个链接器参数、同一个模拟器，只让 target 成为变量。
- 比较退出码是因为最小运行时只有 `exit` 这一条可观察通道；代价是 8 位截断和碰撞。
- `--no-relax` 防止链接器改写 MiniRV 的输出；`start.s` 用 riscv64 汇编是因为 MiniRV 汇编器故意没有 `ecall` 和别名。
- `-mattr=+m` 让参考端从 `rv64i2p1` 提升到 RV64IM，否则会生成 `__muldi3` libcall。
- 17 个程序覆盖 varargs、跳转表、递归、VLA、frame address、大帧、窄栈参数、i32 边界；`bigframe2` 目前是重复的。
- `bench/run.sh` 是代码尺寸基线，不是性能结论。
- 调试 mismatch 的顺序：harness → 编译/链接 → 反汇编 → 重定位 → 优化级别 → 中间表示 → ABI/帧。

## 自我检核

- [ ] 不看笔记，解释为什么 e2e 比较 exit code 而不是 stdout；说出至少两个代价。
- [ ] 解释 `--no-relax` 与 `start.s` 不初始化 `gp` 的关系。
- [ ] 解释为什么参考端需要 `-mattr=+m`，以及为什么 clang 的默认不能拿来推断 llc。
- [ ] 说出 `bf10` 的 108 是怎么从 IR 得来的；为什么它不是 135。
- [ ] 说出 `bigframe2` 和 `bigframe` 的关系；这对“17 个程序”的说法有什么影响。
- [ ] 给出一个新增 e2e 程序的检查清单：main、返回值、无 libc、无地址、8 位截断、非零。
- [ ] 描述一次 mismatch 的排查顺序，至少包含反汇编、重定位、`-O0`、ABI 四个检查点。
- [ ] 说出 CodeGen lit、MC lit、ExecutionEngine、e2e 各自证明什么、不证明什么。

## 延伸阅读

- **[LLVM Testing Guide](https://llvm.org/docs/TestingGuide.html)** — LLVM 项目
  - **读哪里**：`lit` 与 `FileCheck` 的基本用法、`RUN:` 行、测试目录布局。
  - **能学到什么**：为什么 CodeGen 测试是模式断言，以及如何写一个不会过度约束的 `CHECK`。
  - **前提**：能读懂 `llc` 的输出和基本 IR。
- **[LLVM lit 文档](https://llvm.org/docs/CommandGuide/lit.html)** — LLVM 项目
  - **读哪里**：`exit 77` 的 skip 语义、`REQUIRES:`/`UNSUPPORTED:`、并行执行。
  - **能学到什么**：`run.sh` 为什么用 77 表示“工具缺失”，以及如何把它接进 CI。
  - **前提**：跑过一次 `llvm-lit`。
- **[QEMU User Mode 文档](https://qemu-project.gitlab.io/qemu/user/main.html)** — QEMU 项目
  - **读哪里**：linux-user 的启动流程、`-d` 调试选项、guest 退出码如何传回宿主。
  - **能学到什么**：`qemu-riscv64-static` 为什么能直接跑静态 ELF，以及 `exit code` 的边界。
  - **前提**：知道 ELF 入口和系统调用约定。
- **[LLD 文档](https://lld.llvm.org/)** — LLVM 项目
  - **读哪里**：ELF 链接、`--no-relax`、RISC-V relocation 相关章节。
  - **能学到什么**：链接期 relaxation 会改写什么，为什么 bring-up 阶段要关掉它。
  - **前提**：读过 Ch 22 的对象文件与链接。
- **[../llvm_lit/README.md](../llvm_lit/README.md)** — 本仓库课程
  - **读哪里**：lit 配置、FileCheck 模式、`update_llc_test_checks.py`。
  - **能学到什么**：如何把“文本断言”和“执行断言”组合成测试金字塔。
  - **前提**：本章的 e2e 已经跑通。

→ [Ch 26 Clang 前端接入](./26-clang-frontend-integration.md)
