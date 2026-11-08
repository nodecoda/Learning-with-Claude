# Ch 26 — compiler-rt、libc 与 qemu：让代码真的跑起来

> **目标**：把编译器生成的目标文件真正执行起来，用运行结果验证 ABI、libcall、relaxation 与浮点约定；同时钉死**哪一层在本机存在、哪一层不存在**，不把"能编译"当成"能运行"。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON），源码树 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`）；`/usr/bin/qemu-riscv64-static`（QEMU 6.2，user-mode）。实验只写 `/tmp`。

> **前置**：读完 Ch 13（libcall 从 legalization 的哪条出路产生）与练习 B（`_start`、`ecall`、relaxation）。本章的命令都作为完整链路出现，推导不回锅。

---

## 26.1 为什么维护者要关心"跑起来"

静态检查能证明"编码合法""pattern 匹配上了"，但不能证明**两个翻译单元对同一份 ABI 的理解一致**。下面四类 bug 只在运行期暴露：

| 类别 | 编译期 | 运行期 | 章节 |
|---|---|---|---|
| ABI 参数分类 | 两个 `.o` 各自都对 | 返回值是垃圾、第 9 个参数读错槽 | Ch 09、练习 B |
| relaxation | 反汇编"每条都合法" | 输出错、`gp` 读到 0 | Ch 12、练习 B |
| TLS | 重定位能解析 | `tp` 相对寻址读到错位置 | Ch 11、练习 B |
| 原子/fence | 指令能编码 | 多 hart 下丢更新（qemu-user 难复现） | Ch 20 |

RISC-V 的麻烦在于：**运行时环境不像 x86 那样现成**。交叉时你同时缺目标 `libc`、`crt0.o`、compiler-rt 预编译库。第一项工作不是写代码，而是把"有什么/没什么"钉死：

```bash
$ ls $T/lib/clang/24/
include
$ find $T -name 'libclang_rt.builtins*'
（无输出）
$ ls /usr/riscv64-linux-gnu
ls: cannot access '/usr/riscv64-linux-gnu': No such file or directory
$ /usr/bin/qemu-riscv64-static --version
qemu-riscv64 version 6.2.0 (Debian 1:6.2+dfsg-2ubuntu6.31)
```

`lib/clang/24/` 只有 `include/`，没有 `lib/`——本构建只编了编译器、没编 runtimes（26.4 解释）。所以本章所有"跑通"都走同一条路：**`-nostdlib` + 自己写 `_start` + 手动补 builtins 源文件**。

reviewer 问"会不会改变运行行为"时，`llc` 输出只是**主张**，qemu 输出才是**证据**——Ch 20 与练习 B 的"多版本输出一致"都是这个逻辑。

---

## 26.2 先建立直觉：三层运行时栈

一个 RISC-V 程序从"能执行"到"能 `printf`"，依赖三层互相独立的东西：

```
                        你的程序 (main / _start)
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
│ compiler-rt       │   │ libc              │   │ qemu-user         │
│ builtins          │   │ printf/malloc/... │   │ syscall 转发到 host│
│ __muldi3          │   │ 依赖 builtins 与  │   │ 提供进程/内存/文件 │
│ __divdi3          │   │ 内核 syscall      │   │ 语义，不模拟内核   │
│ __adddf3 ...      │   │                   │   │                   │
└───────────────────┘   └───────────────────┘   └───────────────────┘
   源码有，预编译库无        本机无 riscv sysroot      本机有，scalar only
```

三层各自的**本机实际状态**：

| 层 | 本机状态 | 证据 | 缺了会怎样 |
|---|---|---|---|
| compiler-rt builtins | 源码在 `compiler-rt/lib/builtins/`；预编译 `.a` **不存在** | 上面的 `ls`/`find` | `undefined symbol: __muldi3` |
| libc | **没有** riscv64 sysroot | `/usr/riscv64-linux-gnu` 不存在 | `stdio.h` 找不到；`-lc` 找不到 |
| qemu-user | **有**，6.2，user-mode；不模拟内核 | `--version`、26.6.3 的 `ENOSYS` | 只能验证 scalar 用户态行为 |

关键认识：**三层不是一条必须齐全的链**。builtins 可以在没有 libc 时单独存在（它就是为裸机准备的）；qemu-user 可以在没有 libc 时运行静态 ELF（只转发 syscall）。本章实验正是靠"抽掉中间层"单独验证每一层。

一个容易混的符号归属：`__riscv_flush_icache` 在 Linux 链接里由 **glibc** 提供；compiler-rt 的对应实现叫 `__clear_cache`（`clear_cache.c:56`），RISC-V Linux 分支发的也是 syscall 259（`:182-196`）。Ch 20 §20.7 展示了两者的汇编差异。

---

## 26.3 compiler-rt builtins：libcall 从哪来

### 26.3.1 源码位置

```bash
$ cd /home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins
$ ls -la muldi3.c divdi3.c adddf3.c fixdfdi.c
-rw-rw-r-- 1 ubuntu ubuntu  859 Nov  1 14:13 adddf3.c
-rw-rw-r-- 1 ubuntu ubuntu  780 Nov  1 14:13 divdi3.c
-rw-rw-r-- 1 ubuntu ubuntu 1343 Nov  1 14:13 fixdfdi.c
-rw-rw-r-- 1 ubuntu ubuntu 1583 Nov  1 14:13 muldi3.c
```

| 文件 | 符号 | 定义行 | 备注 |
|---|---|---|---|
| `muldi3.c` | `__muldi3` | 38 | 先算 32×32→64（`__muldsi3`），再补高位 |
| `divdi3.c` | `__divdi3` | 22 | 只有一行，实体在 `int_div_impl.inc`，依赖 `__udivmoddi4` |
| `adddf3.c` | `__adddf3` | 16 | 依赖 `__fe_getround`/`__fe_raise_inexact` |
| `fixdfdi.c` | `__fixdfdi` | 18 | 负数转正后走 `__fixunsdfdi` |

`COMPILER_RT_ABI` 在 `int_lib.h:25/27/30` 展开：RISC-V 上为空，只有 ARM 才变成 `__pcs__("aapcs")`——所以这些 `.c` 能直接用 clang 编到任意 target。

### 26.3.2 什么时候后端会发 libcall

用 `llc` 看，不猜：

```bash
$ cat > lib.ll <<'EOF'
define i64 @m64(i64 %a, i64 %b) { %r = mul i64 %a, %b  ret i64 %r }
define i64 @d64(i64 %a, i64 %b) { %r = sdiv i64 %a, %b ret i64 %r }
EOF
$ $T/bin/llc -mtriple=riscv32 -mattr=+m -O2 lib.ll -o - | sed -n '/^d64:/,/ret/p'
d64:
	addi	sp, sp, -16
	sw	ra, 12(sp)
	call	__divdi3
	lw	ra, 12(sp)
	addi	sp, sp, 16
	ret
```

| 组合 | 产物 | 结论 |
|---|---|---|
| `riscv32 +m`, `mul i64` | `mul` + `mulhu` 内联序列 | RV32 的 i64 乘法是 **Custom**，不是 libcall |
| `riscv32 +m`, `sdiv i64` | `call __divdi3` | 除法没有可接受的内联序列 |
| `riscv32 -m`, `mul i64` | `call __muldi3` | 缺 M 时连 32×32 都没有 |
| `riscv32 -m`, `mul i32` | `tail __mulsi3` | 32 位乘法也变 libcall |
| `riscv64 +m`, `mul i64` | `mul a0, a0, a1` | 一条指令，**不该**出现 `__muldi3` |
| `riscv64 -m`, `mul i64` | `tail __muldi3` | 缺 M 才需要 |

纠正一个常见口误："RV32 的 i64 乘法一定调 `__muldi3`"**只在缺 M 时成立**；`+m` 下 legalizer 走 `ReplaceNodeResults` 拆成 `mul`/`mulhu`（Ch 13 §13.5，`13-legalization.md:441` 的示例是 `-mattr=-m`）。

### 26.3.3 实测：手动把 `muldi3.c` 编成 `.o` 并运行

```bash
$ RT=/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64 -O2 \
    -I$RT -I$RT/.. -c $RT/muldi3.c -o muldi3.o
$ $T/bin/llvm-nm muldi3.o
0000000000000000 T __muldi3
```

`-I$RT -I$RT/..` 覆盖 `builtins/` 与上层 `lib/`；本章这几个文件只用前者也够（实测单独 `-I$RT` 可编过），多给一个不会错。测试程序（`rt.h` 见 26.5.2）：

```c
extern i64 __muldi3(i64, i64);
void _start(void) {
  puts_("muldi3="); put_u64((u64)__muldi3(123456789L, 987654321L));
  puts_("neg=");    put_u64((u64)__muldi3(-123456789L, 987654321L));
  sys_exit(0);
}
```

```bash
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64 -O2 \
    -static -nostdlib -fuse-ld=lld -o mul_test.elf mul_test.c muldi3.o
$ /usr/bin/qemu-riscv64-static ./mul_test.elf; echo "exit=$?"
muldi3=121932631112635269
neg=18324811442596916347
exit=0
$ python3 -c "print(123456789*987654321)"
121932631112635269
```

**不链接**时的原始报错：

```
ld.lld: error: undefined symbol: __muldi3
>>> referenced by mul_test.c
>>>               /tmp/mul_test-d4b451.o:(_start)
clang: error: linker command failed with exit code 1 (use -v to see invocation)
```

依赖是链式的：只链 `divdi3.o` 会报 `undefined symbol: __udivmoddi4`（`divdi3.o:(__divdi3)`），补 `udivmoddi4.c`（`:27`）才行；`adddf3.o` 缺 `fp_mode.o` 时一次报 `__fe_getround` 和 `__fe_raise_inexact` 两个符号。后两者在 `fp_mode.c:18`/`:20`，实现分别是"返回 round-to-nearest"和"空操作"——软浮点不维护 FP 状态寄存器。**一个 builtin 源文件不等于一个完整闭包**，这是手工链接最容易低估的成本。

---

## 26.4 `-rtlib=compiler-rt` 的真实失败

```bash
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d \
    -rtlib=compiler-rt -fuse-ld=lld -O2 -o tiny.elf tiny.c
ld.lld: error: cannot open crt0.o: No such file or directory
ld.lld: error: cannot open /home/ubuntu/llvm/builds/ct-instr-trunk/lib/clang/24/lib/riscv64-unknown-unknown-elf/libclang_rt.builtins.a: No such file or directory
ld.lld: error: unable to find library -lc
clang: error: linker command failed with exit code 1 (use -v to see invocation)
```

三条错误各管一层：`crt0.o`（sysroot）、`libclang_rt.builtins.a`（compiler-rt）、`-lc`（libc）。**`-rtlib` 只影响第二行**，不会凭空生成库。更隐藏的坑：同时加 `-nostdlib` 时它根本不参与链接：

```
clang: warning: argument unused during compilation: '-rtlib=compiler-rt' [-Wunused-command-line-argument]
```

想看 driver 到底在找什么，用 `-###`：

```bash
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -### tiny.c 2>&1 | tail -1
 "/usr/bin/ld" "-Bstatic" "-m" "elf64lriscv" "-X" "crt0.o" \
   "-L.../lib/clang/24/lib/riscv64-unknown-unknown-elf" \
   "tiny.o" "--start-group" ".../libclang_rt.builtins.a" "-lc" "--end-group"
```

这些搜索路径都不是本机的真实目录。`ToolChain::getCompilerRT()`（`ToolChain.cpp:938`）先按"无架构子目录"的新布局找，再退回"带架构"的旧布局；都不存在时返回那个不存在的路径，错误由 linker 报出。BareMetal 的 `getCompilerRTPath()`（`BareMetal.cpp:205`）还会优先用 `clang-runtimes` 目录。

### 为什么 runtimes 要单独构建

LLVM 把"编译器"和"它要链接的库"分开：

`LLVM_ENABLE_PROJECTS` 编工具（clang、lld、llvm-*）；`LLVM_ENABLE_RUNTIMES` 编**给 target 用的库**（compiler-rt、libcxx、libunwind、libc），见 `llvm/CMakeLists.txt:179` 与 `:177` 的默认列表。用 `LLVM_ENABLE_PROJECTS=compiler-rt` 仍能编，但 CMake 警告已废弃（`:240`）。

runtimes 必须用**目标编译器**重编，因为它们是 target 代码：x86 主机上的 `libclang_rt.builtins.a` 对 riscv64 无用。所以 `X86;AArch64;RISCV` 的构建完全可能没有任何 target 的 runtimes——本章正是如此。`compiler-rt/lib/builtins/CMakeLists.txt:1197` 是产出 `.a` 的地方；`:1192-1194` 对 `riscv32` 强制 `-fforce-enable-int128`（128 位软浮点需要 `__int128`）。

修好这条链需要单独一个 runtimes 构建：

```bash
cmake -S llvm -B build-rt -G Ninja \
  -DCMAKE_C_COMPILER=$T/bin/clang -DCMAKE_CXX_COMPILER=$T/bin/clang++ \
  -DLLVM_ENABLE_RUNTIMES=compiler-rt -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON \
  -DCMAKE_C_FLAGS="--target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d"
```

这一步**未实测**（会写新 build 目录，超出"只读源码 + 只写 `/tmp`"边界）；"必须单独构建"的结论由上面的错误信息与 CMake 变量语义支撑。

---

## 26.5 libc 与 sysroot：没有 glibc 时怎么办

### 26.5.1 三个开关的组合

| 命令行 | 结果（本机实测） |
|---|---|
| `-static` 单独 | 失败：`cannot open crt0.o`、`builtins.a`、`-lc` |
| `-static -nostdlib` | **成功**，生成静态 EXEC，可直接跑 |
| `-nostdlib` 单独 | 也生成 `Type: EXEC` 并跑通；但不要依赖（默认可能变成 PIE） |
| `-mabi=lp64d` + `#include <stdio.h>` | 失败：`'stdio.h' file not found` |

`--sysroot` 给 driver 一个根：头文件在 `<sysroot>/usr/include`，库和 `crt0.o` 在 `<sysroot>/usr/lib`。本机没有 riscv sysroot，所以它没有可指向的目标。`-static` 是"不要动态加载器"，**不提供** libc；`-nostdlib` 是"不要启动文件和默认库"，**不阻止**你自己补 `.o`。两者组合才得到"纯我的代码 + 我选的 builtins"。

### 26.5.2 自己写 `_start`：两个 syscall 就够

```c
static long sys_write(int fd, const void *buf, unsigned long n) {
  register long a0 __asm__("a0") = fd;
  register const void *a1 __asm__("a1") = buf;
  register unsigned long a2 __asm__("a2") = n;
  register long a7 __asm__("a7") = 64;      /* __NR_write */
  __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
  return a0;
}
static void sys_exit(int code) {
  register long a0 __asm__("a0") = code;
  register long a7 __asm__("a7") = 93;      /* __NR_exit */
  __asm__ volatile("ecall" : : "r"(a0), "r"(a7) : "memory");
  __builtin_unreachable();
}
```

Linux 的 RISC-V syscall 号与 asm-generic 一致：`write=64`、`exit=93`。`register ... __asm__("aN")` 让编译器把值放进指定参数寄存器；`"+r"(a0)` 表示返回值也写回 `a0`。qemu-user 捕获 `ecall`，按 host 的 `write(2)` 执行——所以 host 终端能看到输出。

### 26.5.3 `__riscv_flush_icache` 的归属

Ch 20 §20.7 的结论在这里补全证据链：

Linux target 上 clang 为 `__builtin___clear_cache` 发 `call __riscv_flush_icache`；该符号由 **glibc** 提供，`compiler-rt` 里 grep 不到。compiler-rt 的实现叫 `__clear_cache`（`clear_cache.c:56`），RISC-V Linux 分支在 `:182-196` 发 syscall 259；qemu 6.2 确实实现 259（实测 `flush259=0`）。

裸机（`riscv64-unknown-elf`）没有内核替你做 I-cache 同步，只发 `fence.i`；这是 Ch 20 里两个 target 汇编不同的原因。

---

## 26.6 qemu-user 实操

### 26.6.1 `-L <sysroot>` 的意义

`-L` 设置 **guest 的根目录**：qemu 用它解析 ELF 的 `PT_INTERP`（动态加载器）和 `DT_NEEDED`（共享库）。对**静态** ELF 它什么都不做：

```bash
$ /usr/bin/qemu-riscv64-static -L /nonexistent ./hello.elf
hello riscv
exit=0
```

这也是判断"我的程序是不是静态"的快速实验。动态 ELF + 缺 `-L` 的 `Could not open '/lib/ld-linux-riscv64-lp64d.so.1'` 本章**未实测**——本机没有 riscv 动态加载器。

### 26.6.2 `-cpu`：能选什么

```bash
$ /usr/bin/qemu-riscv64-static -cpu help
any
rv64
shakti-c          # 另有 sifive-e51 / sifive-u54
```

尝试打开 V：

```bash
$ /usr/bin/qemu-riscv64-static -cpu rv64,v=true ./rvv.elf
qemu-riscv64-static: can't apply global rv64-riscv-cpu.v=true: Property 'rv64-riscv-cpu.v' not found
exit=1
```

QEMU 6.2 的 CPU 模型没有 `v` property；V 是编译期特性，且实现的是 **draft V 0.7.1**，不是 LLVM 24 的 RVV 1.0。26.7 实验 7 给出真实 SIGILL。

### 26.6.3 `-strace` 与 `-d in_asm`

```bash
$ /usr/bin/qemu-riscv64-static -strace ./hello.elf
hello riscv
217135 write(1,0x7ffa10,12) = 12
217135 exit(0)
```

第一列是 guest PID，之后是 syscall 名、参数、返回值。排查"没输出"先看 `write`：没有就是没走到，返回 `-1` 就是 fd/缓冲区问题。qemu-user 只实现 syscall 表，不实现特权架构：不存在的 syscall 返回 `-ENOSYS`（实测 `syscall1000=18446744073709551578`，即 `2^64-38`），依赖 CSR、`mret`、页表遍历的程序不能跑。

`-d in_asm` 打印翻译块（qemu 的 TB，不是 objdump）：

```bash
$ /usr/bin/qemu-riscv64-static -d in_asm ./hello.elf 2>&1 | head -5
----------------
IN: _start
0x000000000001116e:  1141              addi            sp,sp,-16
0x0000000000011170:  00010537          lui             a0,65536
```

`-d in_asm,nochain` 禁用 TB 链接，便于逐块观察。

## 26.7 端到端实验（9 个，全部实测）

统一前缀：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
RT=/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc"
cd /tmp/ch26
```

### 实验 1：裸 `_start` + write/exit

```bash
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 \
    -static -nostdlib -fuse-ld=lld -o hello.elf hello.c
$ /usr/bin/qemu-riscv64-static ./hello.elf; echo "exit=$?"
hello riscv
exit=0
$ $T/bin/llvm-readelf -h hello.elf | grep -E 'Type|Entry|Flags'
  Type:                              EXEC (Executable file)
  Entry point address:               0x1116e
  Flags:                             0x5, RVC, double-float ABI
```

`e_flags=0x5` = RVC + double-float ABI；入口是 `.text` 开头的 `_start`，不是 `main`。

### 实验 2–3：`__muldi3` 与 `__divdi3`

`__muldi3` 见 26.3.3：`muldi3=121932631112635269`，与 Python 一致。`__divdi3` 多一层依赖：

```bash
$ $CC -mabi=lp64 -O2 -c $RT/divdi3.c -o divdi3.o && $CC -mabi=lp64 -O2 -c $RT/udivmoddi4.c -o udivmoddi4.o
$ $CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o div_test.elf div_test.c divdi3.o udivmoddi4.o
$ /usr/bin/qemu-riscv64-static ./div_test.elf
divdi3=18446744073376218281
```

`18446744073376218281 = 2^64 - 333333335`，即 `-333333335`。C 的整数除法**向零截断**，所以不是 Python `//` 的 `-333333336`；打印按无符号，读时自己换算。

### 实验 4：`__adddf3`（软浮点，`-mabi=lp64`）

```bash
$ $CC -mabi=lp64 -O2 -c $RT/adddf3.c -o adddf3.o && $CC -mabi=lp64 -O2 -c $RT/fp_mode.c -o fp_mode.o
$ $CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o add_soft.elf add_soft.c adddf3.o fp_mode.o
$ /usr/bin/qemu-riscv64-static ./add_soft.elf
soft 1.5+2.25 bits=0x400e000000000000
```

`0x400e000000000000` 是 `3.75` 的 IEEE-754 位模式。用**位模式**而不是十进制打印，是为了避免再引入 `__fixdfsi`、`__floatunsidf` 一长串依赖，把实验变成"实现 printf"。`llvm-objdump -d add_soft.elf` 里 `__adddf3`（`0x111f2` 起）是纯整数序列，没有一条浮点指令。

### 实验 5：同一程序走硬件 `fadd.d`（`-mabi=lp64d`）

```bash
$ $CC -mabi=lp64d -O2 -static -nostdlib -fuse-ld=lld -o add_hw.elf add_hw.c
$ /usr/bin/qemu-riscv64-static ./add_hw.elf
hard 1.5+2.25 bits=0x400e000000000000
$ $T/bin/llvm-objdump -d --no-show-raw-insn add_hw.elf | sed -n '/<_start>:/,+12p'
   1117e:      	fld	fa5, 0x10(sp)
   11180:      	fld	fa4, 0x8(sp)
   11182:      	fadd.d	fa5, fa5, fa4
   1119e:      	fmv.x.d	a0, fa5
```

`a`、`b` 必须加 `volatile`，否则 `-O2` 常量折叠，你永远看不到 `fadd.d`。**`-mabi` 直接决定硬件浮点还是软浮点**，不是优化级别的事。

### 实验 6：relax / GP relaxation 在 qemu 下行为一致

```bash
$ $CC -mabi=lp64d -O2 -mrelax -c -o relax.o relax.c
$ $T/bin/ld.lld --no-relax         -o norelax.elf relax.o
$ $T/bin/ld.lld --relax            -o relax.elf   relax.o
$ $T/bin/ld.lld --relax --relax-gp -o relaxgp.elf relax.o
$ for f in norelax.elf relax.elf relaxgp.elf; do printf '%-14s ' $f; /usr/bin/qemu-riscv64-static ./$f; done
norelax.elf    sum=242
relax.elf      sum=242
relaxgp.elf    sum=242
$ $T/bin/llvm-size norelax.elf relax.elf relaxgp.elf
   text	   data	    bss	    dec	    hex	filename
    189	      8	      0	    197	     c5	norelax.elf
    181	      8	      0	    189	     bd	relax.elf
    177	      8	      0	    185	     b9	relaxgp.elf
```

`--no-relax` 到 `--relax` 省 8 字节（两个 `auipc+jalr` → `jal`），`--relax-gp` 再省 4 字节：

```
# --no-relax                        # --relax --relax-gp
auipc  ra, 0x0                      jal    0x11196 <set_gp>
jalr   -0x16(ra) <set_gp>           ld     s0, -0x800(gp)
lui    a0, 0x12
ld     s0, 0x250(a0)
```

本例 `__global_pointer$ = 0x12a48`、`g = 0x12248`，差正好 `-0x800`。三个 ELF 输出相同，说明"改指令也改地址"是自洽的——Ch 12 不动点循环的运行期证据。

### 实验 7：RVV 程序在 qemu 6.2 下 SIGILL

```bash
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -mabi=lp64d -O2 -static -nostdlib -fuse-ld=lld -o rvv.elf rvv.c
$ $T/bin/llvm-objdump -d --no-show-raw-insn rvv.elf | grep vsetvli | head -1
   111d0:      	vsetvli	t0, zero, e64, m1, ta, ma
$ /usr/bin/qemu-riscv64-static ./rvv.elf; echo "exit=$?"
Illegal instruction (core dumped)
exit=132
```

132 = 128 + 4（SIGILL）。不是程序写错，而是**版本错配**：LLVM 24 按 RVV 1.0 编码，qemu 6.2 只认 draft 0.7.1，第一条 `vsetvli` 就对不上。**任何向量运行期结论在本机都必须标「未实测」**；可做替代验证：`llc`/`llvm-mc` 编码、`llvm-objdump` 反汇编、Ch 17 的 `-debug-only=isel`。要真跑需 QEMU ≥ 7.1 或硬件。

### 实验 8：浮点 ABI 不一致导致运行期错值

```bash
$ $CC -mabi=lp64d -O2 -c abi_caller.c -o abi_caller.o
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -O2 -I$RT -I$RT/.. -c abi_callee.c -o abi_callee.o
$ $T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -O2 -I$RT -I$RT/.. -c $RT/adddf3.c -o adddf3_d.o && $T/bin/clang --target=riscv64-unknown-elf -march=rv64imac -mabi=lp64d -O2 -I$RT -I$RT/.. -c $RT/fp_mode.c -o fp_mode_d.o
note: hard-float 'd' ABI can't be used for a target that doesn't support the D instruction set extension (ignoring target-abi)   # 三条 clang 命令各打一次
$ $T/bin/llvm-readelf -h abi_callee.o | grep Flags
  Flags:                             0x5, RVC, double-float ABI
$ $T/bin/ld.lld -o abi_mix.elf abi_caller.o abi_callee.o adddf3_d.o fp_mode_d.o && /usr/bin/qemu-riscv64-static ./abi_mix.elf
bits=0x3ff8000000000000
```

调用方按 `lp64d` 把 `1.5` 放进 `fa0`、从 `fa0` 读返回值；被调用方因 `rv64imac` 没有 D，实际用 `a0` 收参数和返回。两边 `e_flags` 都是 `0x5`，链接器无话可说，结果读回 `0x3ff8000000000000`（1.5）而不是 `0x4004000000000000`（2.5）——`e_flags` 只记录"声明"，不记录"实际用了哪些寄存器"。

### 实验 9：`_start` 入口的 `sp` 对齐

```bash
$ /usr/bin/qemu-riscv64-static ./sp.elf
sp_mod16=0
```

qemu 进入 `_start` 时 `sp` 已按 psABI 16 字节对齐；手写汇编切栈或换 loader 后要自己 `andi sp, sp, -16`。`gp` 则**从来没人为你初始化**：裸机必须自己 `la gp, __global_pointer$` 且放进 `.option norelax`（练习 B §B.12 第 4 条的 `mv gp, gp` 翻车记录）。

## 26.8 源码落点

| 文件 | 行 | 内容 |
|---|---|---|
| `compiler-rt/lib/builtins/muldi3.c` | 38 | `__muldi3`；`:17` 是 32×32→64 的 `__muldsi3` |
| `compiler-rt/lib/builtins/divdi3.c` | 22 | `__divdi3` → `int_div_impl.inc` |
| `compiler-rt/lib/builtins/udivmoddi4.c` | 27 | `__udivmoddi4`，`__divdi3` 的实际依赖 |
| `compiler-rt/lib/builtins/adddf3.c` | 16 | `__adddf3` → `fp_add_impl.inc` |
| `compiler-rt/lib/builtins/fp_mode.c` | 18 / 20 | `__fe_getround` / `__fe_raise_inexact` |
| `compiler-rt/lib/builtins/fixdfdi.c` | 18 | `__fixdfdi`；负数走 `__fixunsdfdi` |
| `compiler-rt/lib/builtins/clear_cache.c` | 56 / 182-196 | `__clear_cache`；RISC-V Linux 发 syscall 259 |
| `compiler-rt/lib/builtins/int_lib.h` | 25-30 | `COMPILER_RT_ABI` 的平台分支（ARM 专用） |
| `compiler-rt/lib/builtins/CMakeLists.txt` | 1197 | `add_compiler_rt_runtime(clang_rt.builtins ...)` |
| `llvm/CMakeLists.txt` | 177 / 179 / 240 | 默认 runtimes、`LLVM_ENABLE_RUNTIMES`、废弃警告 |
| `clang/lib/Driver/ToolChain.cpp` | 938 | `getCompilerRT()` 的两套布局搜索；BareMetal 覆写在 `BareMetal.cpp:205` |
| `lld/ELF/Arch/RISCV.cpp` | 167 | `calcEFlags()`；`e_flags` 只在链接期比较 |
| `llvm/docs/RISCVUsage.md` | — | 用户可见的 ABI/relax 承诺 |

---

## 26.9 可跑实验

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
RT=/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc"
mkdir -p /tmp/ch26 && cd /tmp/ch26

$CC -mabi=lp64d -O2 -static -nostdlib -fuse-ld=lld -o hello.elf hello.c
/usr/bin/qemu-riscv64-static ./hello.elf

for f in muldi3 divdi3 udivmoddi4 adddf3 fp_mode fixdfdi fixunsdfdi; do
  $CC -mabi=lp64 -O2 -I$RT -I$RT/.. -c $RT/$f.c -o $f.o
done
$CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o mul_test.elf mul_test.c muldi3.o
$CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o div_test.elf div_test.c divdi3.o udivmoddi4.o
$CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o add_soft.elf add_soft.c adddf3.o fp_mode.o
$CC -mabi=lp64 -O2 -static -nostdlib -fuse-ld=lld -o fix_test.elf fix_test.c fixdfdi.o fixunsdfdi.o
for f in mul_test div_test add_soft fix_test; do /usr/bin/qemu-riscv64-static ./$f.elf; done

$CC -mabi=lp64d -O2 -static -nostdlib -fuse-ld=lld -o add_hw.elf add_hw.c
/usr/bin/qemu-riscv64-static ./add_hw.elf
$T/bin/llvm-objdump -d --no-show-raw-insn add_hw.elf | grep fadd.d

$CC -mabi=lp64d -O2 -mrelax -c -o relax.o relax.c
$T/bin/ld.lld --no-relax -o norelax.elf relax.o
$T/bin/ld.lld --relax    -o relax.elf   relax.o
$T/bin/ld.lld --relax --relax-gp -o relaxgp.elf relax.o
for f in norelax.elf relax.elf relaxgp.elf; do /usr/bin/qemu-riscv64-static ./$f; done

$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -mabi=lp64d -O2 \
  -static -nostdlib -fuse-ld=lld -o rvv.elf rvv.c
/usr/bin/qemu-riscv64-static ./rvv.elf || echo "SIGILL rc=$?"

# abi_mix.elf / sp.elf 需先按实验 8、9 编译
/usr/bin/qemu-riscv64-static ./abi_mix.elf && /usr/bin/qemu-riscv64-static ./sp.elf
```

源码骨架：`mul_test.c`/`div_test.c`/`fix_test.c` 各写一行 `extern` 声明并调用；`add_soft.c` 调 `__adddf3`；`add_hw.c` 用 `volatile double a = 1.5, b = 2.25;`；`abi_callee.c` 定义 `double addone(double x) { return x + 1.0; }`。`rt.h` 提供 `sys_write`/`sys_exit`/`put_u64`/`put_hex64`（见 26.5.2）。

两个必须自己控制的点：`add_hw.c` 的 `volatile`（防常量折叠）；`mul_test.c` 的乘积要留在 `i64` 范围内，否则测到的是溢出回绕而不是乘法本身（第一次实验就踩了：`1234567890123 * 987654321` 超出 2^63，输出被截断）。

## 26.10 踩雷集锦

1. **`-static` 与 `-nostdlib` 必须成对。** `-static` 单独会去找 `crt0.o`、`builtins.a`、`-lc`；`-nostdlib` 单独在本工具链侥幸生成静态 EXEC，但换 driver/版本可能变成 PIE，运行期再报 `PT_INTERP` 找不到。
2. **`_start` 的 `sp` 与 `gp` 不是自动的。** `sp` 由 qemu 按 16 字节对齐传入（实测 `sp_mod16=0`）；`gp` 没有任何人初始化，必须自己 `la gp, __global_pointer$`，且放在 `.option norelax` 里，否则会被 relax 成 `mv gp, gp`。
3. **RVV 版本错配是"第一条指令就死"。** qemu 6.2 = draft V 0.7.1，LLVM 24 = RVV 1.0；`-cpu rv64,v=true` 连 property 都不存在。向量结论只能来自编码/反汇编，运行期一律标「未实测」。
4. **`-rtlib` 与 `--unwindlib` 互相约束，baremetal 目标却直接忽略。** `riscv64-linux-gnu` 上 `--unwindlib=libunwind` + 默认 `--rtlib=libgcc` 报 `--rtlib=libgcc requires --unwindlib=libgcc`；`riscv64-unknown-elf` 上两者都是 `-Wunused-command-line-argument`（没有 C++ 运行时）。
5. **`__muldi3` 在 RV64 不该出现。** `riscv64 +m` 的 `mul i64` 是一条 `mul`，只有 `-m` 才退回 `tail __muldi3`；看到它先查 `-mattr`。反过来 RV32 + `+m` 的 i64 乘法是内联 `mul`/`mulhu`，也不是 libcall。
6. **浮点 ABI 与 `-mabi` 不一致是"链接通过、结果错"。** `-march=rv64imac -mabi=lp64d` 只给 note，`e_flags` 仍写 `0x5`，代码实际用整数寄存器传 double。混链后 `1.5 + 1.0` 读出 `1.5`，没有任何诊断。
7. **qemu-user 不模拟完整内核。** 未知 syscall 返回 `-ENOSYS`（`syscall1000=18446744073709551578`），不是崩溃；依赖 CSR/特权态的程序无法运行。别把 qemu 的宽容当 ABI 保证。
8. **builtins 不是一个文件，且必须用同一 `-mabi` 编。** `__divdi3` 要 `__udivmoddi4`，`__adddf3` 要 `fp_mode.c`，`__fixdfdi` 要 `__fixunsdfdi`；`lp64` 的 `adddf3.o`（`e_flags=0x1`）与 `lp64d` 调用方链接会报 `cannot link object files with different floating-point ABI`——这是好事，链接器替你拦住了实验 8 那类错误的一半。

## 26.11 本章重点整理

- 三层互相独立：builtins 可无 libc 存在，qemu-user 可无 libc 运行静态 ELF，libc 需要 sysroot。本机：builtins 只有源码、无 riscv sysroot、qemu 6.2 只做 scalar + draft V 0.7.1。
- libcall 由 legalization 的 `Expand` 终点或 `LibCall` 动作产生；RV64 `+m` 的 i64 乘除是一条指令。
- 手工链接 builtins：同一 `-mabi` 编译、按符号闭包补文件、`-static -nostdlib -fuse-ld=lld`；runtimes 必须用 `LLVM_ENABLE_RUNTIMES` 单独构建。
- `--sysroot` 解决头文件与 `crt0.o`/`-lc`；没有 sysroot 时自己写 `_start` + `ecall`（write=64、exit=93）。运行期证据不可替代：relax 三版输出一致、软/硬浮点位模式一致、ABI 混用静默错值，只有跑起来才看得见。

## 26.12 自我检核

- [ ] 能画出三层运行时栈，并说出本机每一层"有/无"的具体证据
- [ ] 给定 `(triple, +m/-m, 操作)`，能用 `llc` 判断是内联序列还是 `__muldi3`/`__divdi3`/`__mulsi3`
- [ ] 能说出 `LLVM_ENABLE_RUNTIMES` 与 `LLVM_ENABLE_PROJECTS` 的分工，以及 runtimes 为什么必须按 target 重编
- [ ] 能写出 `_start` 的 write/exit 两个 syscall，并说明 `"+r"(a0)` 的作用
- [ ] 能用 `-mabi=lp64` / `-mabi=lp64d` 构造"软浮点 vs `fadd.d`"的对照，并给出 objdump 证据
- [ ] 能说出 qemu 6.2 跑 RVV 1.0 的结果（SIGILL/132）与 `-cpu rv64,v=true` 的报错
- [ ] 能解释 `-march=rv64imac -mabi=lp64d` 为什么链接通过但运行错值

---

## 26.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins/README.txt`**：builtins 的定位（"libgcc 的替代品"）与"每个函数一个文件"的组织原则，解释了为什么手工链接要补一串依赖。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins/muldi3.c`**、**`divdi3.c`**、**`adddf3.c`**、**`fixdfdi.c`**：本章四个实验对象的完整实现；配 **`fp_mode.c`** 看软浮点 FP 状态策略。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins/clear_cache.c`**：56 行是入口，182-196 是 RISC-V Linux 的 syscall 259 实现，对照 Ch 20 §20.7。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/CMakeLists.txt`**：177-179 默认 runtimes、232-246 废弃警告；配 `builtins/CMakeLists.txt:1197` 看 `.a` 怎么产出。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChain.cpp`**：938-960 的 `getCompilerRT()`，解释 `-###` 里那条 `-L` 从哪来。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/ELF/Arch/RISCV.cpp`**：167 的 `calcEFlags()`；`e_flags` 的检查范围就是实验 8 能骗过链接器的原因。

---

→ 下一章：[练习 E — 端到端工具链](./practice-e-toolchain-end-to-end.md)
