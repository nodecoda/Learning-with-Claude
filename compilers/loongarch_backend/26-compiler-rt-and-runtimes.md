# Ch 26 — compiler-rt、libgcc 与 runtimes：程序怎么真正跑起来

> 本章目标：知道 LoongArch 在 `compiler-rt` 里有哪些专属代码；能说清 `libclang_rt.builtins`、`libgcc`、crt 对象三者的分工；能解释为什么本课这份构建**不能**自己链接 LoongArch 程序，以及怎么绕过。

---

## 26.1 先看一个不舒服的事实：这份构建没有 `libclang_rt`

```text
$ ls $T/lib/clang/24
include

$ find $T -name 'libclang_rt*'
（无输出）
```

也就是说：本课用的 `clang`+`llc`+`lld` 全都能生成 LoongArch 目标文件，但 **clang 自己的运行时库一个都没编**。直接链接会这样：

```text
$ clang --target=loongarch64-unknown-linux-gnu x.c -o x
ld: cannot find Scrt1.o
ld: cannot find crti.o
ld: cannot find crtbeginS.o
ld: cannot find -lgcc
```

这不是 LoongArch 后端的 bug，而是构建配置的选择：`LLVM_ENABLE_PROJECTS` 含 `clang;lld`，但没有 `compiler-rt`，也没有做 runtimes 的二次构建。读源码时要知道：**“能生成 .o”和“能链接出可执行文件”是两条独立的流水线**。

---

## 26.2 `compiler-rt/lib/builtins` 里的 LoongArch

整个目录里 LoongArch 专属的源文件只有一个：

```text
compiler-rt/lib/builtins/loongarch/fp_mode.c
```

CMake 里的组装（`compiler-rt/lib/builtins/CMakeLists.txt:964-972`）：

```cmake
set(loongarch_SOURCES
  loongarch/fp_mode.c
  ${GENERIC_SOURCES}
  ${GENERIC_TF_SOURCES}
)
set(loongarch64_SOURCES
  ${loongarch_SOURCES}
)
```

三部分含义：

| 集合 | 内容 | 为什么需要 |
|---|---|---|
| `loongarch/fp_mode.c` | `__fe_getround` / `__fe_raise_inexact` | 读写 FCSR，实现 `fenv` 的舍入模式与 inexact 标志 |
| `GENERIC_SOURCES` | `__addsf3`、`__divdi3`、`__ashldi3` 等 | 目标无关的整数/单双精度软实现 |
| `GENERIC_TF_SOURCES` | `addtf3.c`、`multf3.c`、`divtf3.c`、`subtf3.c`、`fixtfdi.c`… 共 26 个 | `fp128`（四精度）软实现；LoongArch 没有硬件四精度 |

`GENERIC_TF_SOURCES` 的完整清单在 `CMakeLists.txt:217-243`，包括 `addtf3/comparetf2/divtc3/divtf3/extenddftf2/…/trunctfsf2`。这解释了为什么一个 `long double`（LA64 上是 `fp128`）的乘法会生成 `__multf3` 调用，而不是一条指令。

注意 `loongarch32` **没有**单独的 source 集合，只有 `loongarch64`：`loongarch_SOURCES` 直接复用，因为 `fp_mode.c` 里用 `#if __loongarch_frlen != 0` 区分软/硬浮点。

---

## 26.3 `fp_mode.c`：两个函数，三条指令

源码（节选）：

```c
CRT_FE_ROUND_MODE __fe_getround(void) {
#if __loongarch_frlen != 0
  int fcsr;
  __asm__ __volatile__("movfcsr2gr %0, $fcsr0" : "=r" (fcsr));
  fcsr &= LOONGARCH_RMODE_MASK;                  // 0x0300
  switch (fcsr) { ... }
#endif
}

int __fe_raise_inexact(void) {
#if __loongarch_frlen != 0
  int fcsr;
  __asm__ __volatile__("movfcsr2gr %0, $fcsr0" : "=r" (fcsr));
  __asm__ __volatile__("movgr2fcsr $fcsr0, %0" :: "r" (fcsr | 0x10000));
#endif
  return 0;
}
```

实测编译 + 反汇编（本章命令见 26.7）：

```text
$ clang --target=loongarch64-unknown-linux-gnu -O2 -c \
    compiler-rt/lib/builtins/loongarch/fp_mode.c -o fp_mode.o

$ llvm-objdump -d fp_mode.o
0000000000000000 <__fe_getround>:
       0: 04 c8 14 01  	movfcsr2gr	$a0, $fcsr0
       4: 84 20 c9 00  	bstrpick.d	$a0, $a0, 0x9, 0x8
       8: 05 00 00 1a  	pcalau12i	$a1, 0x0
       c: a5 00 c0 02  	addi.d	$a1, $a1, 0x0
      10: a4 10 20 38  	ldx.bu	$a0, $a1, $a0
      14: 20 00 00 4c  	ret

0000000000000020 <__fe_raise_inexact>:
      20: 04 c8 14 01  	movfcsr2gr	$a0, $fcsr0
      24: 05 02 00 14  	lu12i.w	$a1, 0x10        # 0x10000 = inexact
      28: 84 14 15 00  	or	$a0, $a0, $a1
      2c: 80 c0 14 01  	movgr2fcsr	$fcsr0, $a0
```

三个可以拿来讲课的细节：

1. **`bstrpick.d $a0, $a0, 0x9, 0x8`** 就是源码里的 `fcsr &= 0x0300`：只保留 bit 9:8 的舍入模式。
2. **`ldx.bu` 是编译器把 `switch` 变成查表**：`CRT_FE_*` 只有 4 个值，Clang 生成了 4 字节表 + 寄存器索引加载。
3. **`lu12i.w $a1, 0x10`** 是 Ch 08 的常量物化：`0x10000` 用一条 `lu12i.w` 就够（低位全 0）。

这段代码是测试“后端 + 内联汇编 + 常量物化 + 分支折叠”的好材料，而且它属于真正的运行时库，不是玩具。

---

## 26.4 其它 runtime 里的 LoongArch

| 位置 | 内容 |
|---|---|
| `compiler-rt/lib/builtins/clear_cache.c:92` | `#elif defined(__linux__) && defined(__loongarch__)` → `ibar 0` |
| `compiler-rt/lib/sanitizer_common/CMakeLists.txt:214` | `sanitizer_syscall_linux_loongarch64.inc` |
| `compiler-rt/lib/xray/CMakeLists.txt:63-65` | `xray_loongarch64.cpp` + `xray_trampoline_loongarch64.S` |
| `compiler-rt/cmake/crt-config-ix.cmake:26,36` | `LOONGARCH64` 在 `CRT_SUPPORTED_ARCH` 里 |
| `compiler-rt/cmake/builtin-config-ix.cmake:87,114` | `LOONGARCH64` 在 builtins 支持列表 |

`AllSupportedArchDefs.cmake` 里 LoongArch64 的支持面（本树实测 grep）：

```text
ASAN  DFSAN  LSAN  MSAN  PROFILE  TSAN  UBSAN
SAFESTACK  CFI  SCUDO  XRAY  ORC  SANITIZER_COMMON
```

**不支持**的（同一份文件里没有 `LOONGARCH64`）：`GWP_ASAN`、`HWASAN`、`MEMPROF`、`NSAN`、`CSAN`、`TYSAN`、`CTX_PROFILE`、`RTSAN`。

这条清单对 triage 很有用：看到 “LoongArch + hwasan” 的 issue，第一反应应该是“这个组合本来就没实现”，而不是“后端生成错了”。

---

## 26.5 crt 对象与 libgcc：谁提供 `_start`

一个动态链接的 LoongArch 程序需要：

| 对象/库 | 提供者 | 作用 |
|---|---|---|
| `Scrt1.o` | glibc | PIE 的 `_start`，调用 `__libc_start_main` |
| `crti.o` / `crtn.o` | glibc | `.init`/`.fini` 序言与尾声 |
| `crtbeginS.o` / `crtendS.o` | **GCC**（`libgcc` 目录） | `.init_array` 注册、`__dso_handle` |
| `libgcc.a` / `libgcc_s.so` | GCC | `__addsf3`、`__multf3`、unwind |
| `libclang_rt.builtins-loongarch64.a` | compiler-rt | 上面那些的替代品 |

关键点：**`crtbeginS.o` 属于 GCC 的目录，不在 sysroot 的 `lib64` 里**。这解释了为什么只给 `--sysroot` 还不够。

---

## 26.6 实测：用交叉 GCC 的 sysroot 链接并运行

本机交叉工具链：

```text
$ loongarch64-unknown-linux-gnu-gcc --version
loongarch64-unknown-linux-gnu-gcc (GCC) 16.2.0

$ loongarch64-unknown-linux-gnu-gcc -print-sysroot
/opt/loongarch64-cross/bin/../target
```

直接给 `--sysroot` 仍然找不到 `crtbeginS.o`（因为它按 `<sysroot>/lib64/gcc/...` 找，实际在 `<sysroot>/usr/lib64/gcc/...`）。用 `-B` 指向 GCC 目录后才通：

```text
$ SYS=/opt/loongarch64-cross/target
$ G=$SYS/usr/lib64/gcc/loongarch64-unknown-linux-gnu/16.2.0

$ clang --target=loongarch64-unknown-linux-gnu --sysroot=$SYS -B$G -L$G \
    hello.c -o hello

$ file hello
hello: ELF 64-bit LSB pie executable, LoongArch, version 1 (SYSV),
       dynamically linked, interpreter /lib64/ld-linux-loongarch-lp64d.so.1

$ qemu-loongarch64 -L $SYS ./hello
hello loongarch
exit=0
```

`main` 的反汇编（`llvm-objdump -d hello`）：

```asm
7c0: addi.d  $sp, $sp, -0x20
7c4: st.d    $ra, $sp, 0x18
7c8: st.d    $fp, $sp, 0x10
7dc: pcaddi  $a0, 0x8
7e0: bl      -0x260 <puts@plt>
```

注意 `pcaddi $a0`：字符串地址是 **PC 相对**取的（PIE 默认），不是 `la.pcrel` 伪指令——伪指令在这一步已经被展开（Ch 18）。

---

## 26.7 怎么自己编 compiler-rt（未实测）

本课没有编 runtimes，下面命令按 LLVM 标准布局给出，**未在本树执行**：

```bash
cmake -G Ninja -S /home/ubuntu/llvm/trees/ct-instr-trunk/llvm \
  -B /home/ubuntu/llvm/builds/ct-instr-trunk-runtimes \
  -DCMAKE_C_COMPILER=$T/bin/clang \
  -DCMAKE_CXX_COMPILER=$T/bin/clang++ \
  -DCMAKE_ASM_COMPILER=$T/bin/clang \
  -DCMAKE_C_COMPILER_TARGET=loongarch64-unknown-linux-gnu \
  -DCMAKE_CXX_COMPILER_TARGET=loongarch64-unknown-linux-gnu \
  -DCMAKE_ASM_COMPILER_TARGET=loongarch64-unknown-linux-gnu \
  -DLLVM_ENABLE_RUNTIMES="compiler-rt" \
  -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON \
  -DCMAKE_SYSROOT=/opt/loongarch64-cross/target
ninja -C /home/ubuntu/llvm/builds/ct-instr-trunk-runtimes
```

预期产物是 `libclang_rt.builtins-loongarch64.a` 与 `crtbeginS.o`/`crtendS.o`；之后再链接就不需要 `-B$G`。**这条路径未实测**，因为编 runtimes 需要另一份完整构建，超出本课范围。

---

## 26.8 反直觉点

1. **“能生成 .o” ≠ “能链接”**：本课构建完全没有 `libclang_rt`。
2. **`crtbeginS.o` 在 GCC 目录，不在 sysroot 的 `lib64`**。
3. **`loongarch32` 没有单独 builtins source 集合**，与 `loongarch64` 共用。
4. **`fp128` 的四则运算全部是软实现**（26 个 `GENERIC_TF_SOURCES` 文件）。
5. **`clear_cache` 在 LoongArch 上就是一条 `ibar 0`**，参数被忽略。
6. **sanitizer 支持面是不均匀的**：ASAN/UBSAN/TSAN 有，HWASAN/MEMPROF 没有。
7. **`__fe_getround` 会生成查表**（`ldx.bu`），不是 4 个分支。

---

## 26.9 本章证据

| 结论 | 源码/命令 |
|---|---|
| 构建里没有 libclang_rt | `ls $T/lib/clang/24`、`find $T -name 'libclang_rt*'` |
| 链接缺 crt | 26.1 的四条 `ld: cannot find` |
| `loongarch_SOURCES` | `compiler-rt/lib/builtins/CMakeLists.txt:964-972` |
| TF 源文件清单 | 同文件 `:217-243` |
| `fp_mode.c` 源码 | `compiler-rt/lib/builtins/loongarch/fp_mode.c` |
| `fp_mode.o` 反汇编 | 26.3 实测 |
| `clear_cache` → `ibar 0` | `compiler-rt/lib/builtins/clear_cache.c:92` |
| sanitizer 支持面 | `compiler-rt/cmake/Modules/AllSupportedArchDefs.cmake` |
| 端到端链接 + qemu | 26.6 实测（`exit=0`） |
| runtimes 构建命令 | 26.7（**未实测**） |

---

## 26.10 练习

1. 把 `fp_mode.c` 用 `-mabi=lp64s` 编译，确认 `__fe_getround` 变成直接返回常量（`#if __loongarch_frlen != 0` 的分支被裁掉）。
2. 写一个使用 `long double` 乘法的程序，用 `-emit-llvm` 确认调用的是 `__multf3`，再链接交叉 `libgcc.a` 找到该符号。
3. 用 `llvm-nm` 在交叉 GCC 的 `libgcc.a` 里找 `__addtf3`，解释为什么 LoongArch 需要它。
4. 用 `llvm-readobj -h` 对比 `hello` 与 `hello.o` 的 `Flags`/`Type`，确认 PIE。
5. 给 `clear_cache.c` 写一个 LoongArch 的 lit 测试思路：为什么这个函数在 LoongArch 上无法用“检查参数被使用”来测试？

---

## 26.11 下一步

运行时看完了，下一章进入“怎么证明你改对了”：lit 测试布局、`update_llc_test_checks.py` 的实际用法与本树的测试基线。
