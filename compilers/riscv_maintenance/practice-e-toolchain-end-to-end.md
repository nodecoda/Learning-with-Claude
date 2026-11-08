# 练习 E — 端到端工具链：clang → .o → ld.lld → qemu

> **目标**：把一次 RISC-V 构建拆成七个可检查的产物，用 `-###`、`llvm-readelf`、`llvm-objdump`、`qemu-riscv64-static` 留下证据，最终得到“一条命令就能复现”的 bug report 模板。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（LLVM `24.0.0git`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`）；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）；`/usr/bin/qemu-riscv64-static`（QEMU 6.2，user-mode）。本机无 riscv sysroot/libc；实验只写 `/tmp`，不改源码树。

> **前置**：读完 [Ch 24 — Clang Driver](./24-clang-driver.md)、[Ch 25 — TargetInfo 与 builtins](./25-clang-targetinfo-and-builtins.md)、[Ch 26 — compiler-rt、libc 与 qemu](./26-compiler-rt-libc-qemu.md)。本练习不重复参数优先级，只把三层串起来验证。

---
## E.1 为什么必须“端到端”

单看一层，都会把错误留给下一层：

| 只验证这一层 | 看不到什么 | 错误实际在哪一层暴露 |
|---|---|---|
| `clang -S` | `.o` 的 `e_flags`、重定位、`.attribute` | 链接期：`different floating-point ABI` |
| `clang -c` | relaxation、最终地址、入口 | 链接期或运行期：`call` 没变成 `jal`、入口为 0 |
| `llvm-readelf` | 程序行为 | 运行期：输出错、SIGILL、退出码不对 |
| `qemu` 单独跑 | 哪一层写错了 | 只能看到“坏了”，不能定位 |

维护者收到的 bug report 经常是“RISC-V 上这段代码跑不对”。合格的报告必须把它变成一条命令：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
bash e2e.sh repro.c /tmp/repro     # 产出全部证据
```
并附上构建 hash、`-###` 的两行、`.o` 的 `Flags`/`Tag_arch`/关键重定位、最终的 `llvm-objdump -d` 与 qemu 的 stdout + `exit=$?`。下面每一步都围绕这个目标。

---
## E.2 先建立直觉：一次构建的七个产物

```text
hello.c
  ├─ clang -S -emit-llvm  → hello.ll        (IR)
  ├─ clang -S             → hello.s         (.option/.attribute 可见)
  ├─ clang -c             → hello.o         (e_flags/重定位)
  ├─ ld.lld --relax       → hello.elf       (地址已定、call→jal)
  ├─ llvm-readelf -h/-A   → 元数据证据
  ├─ llvm-objdump -d      → 反汇编证据
  └─ qemu-riscv64-static  → 运行输出
```

| 产物 | 生成命令 | 回答的问题 | 证据命令 |
|---|---|---|---|
| `hello.ll` | `clang -S -emit-llvm` | 前端把 C 变成了什么 | `grep`、`llc -stop-after` |
| `hello.s` | `clang -S` | 选了哪些指令、带了什么属性 | 读 `.attribute`/`.option` |
| `hello.o` | `clang -c` | ABI、ISA、重定位是什么 | `llvm-readelf -h -A -r` |
| `hello.elf` | `ld.lld -e _start -static --relax` | 地址、入口、段、relax 结果 | `llvm-readelf -h -S -l` |
| 元数据 | `llvm-readelf` | `e_flags`/`.attribute`/reloc 是否符合预期 | 与 `.o` 对照 |
| 反汇编 | `llvm-objdump -d --no-show-raw-insn` | 最终指令与重定位落点 | `-dr` 看 `.o` |
| 运行 | `qemu-riscv64-static` | 行为与退出码 | stdout + `$?` |

本练习的 `hello.c` 不依赖 libc：自写 `_start`，用两个 Linux syscall（`write` a7=64、`exit` a7=93）。这样可以同时验证“入口、ABI、relax、运行”四件事。

```c
static inline long sys_write(long fd, const char *buf, long n) {
  register long a0 asm("a0") = fd;
  register long a1 asm("a1") = (long)buf;
  register long a2 asm("a2") = n;
  register long a7 asm("a7") = 64;
  asm volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
  return a0;
}
static inline void sys_exit(long code) {
  register long a0 asm("a0") = code;
  register long a7 asm("a7") = 93;
  asm volatile("ecall" : : "r"(a0), "r"(a7) : "memory");
  __builtin_unreachable();
}
static volatile long N = 10;
__attribute__((noinline)) static long sum(long n) {
  long s = 0;
  for (long i = 1; i <= n; i++) s += i;
  return s;
}
void _start(void) {
  long s = sum(N);
  const char *msg = s == 55 ? "toolchain ok\n" : "toolchain BAD\n";
  sys_write(1, msg, s == 55 ? 13 : 14);
  sys_exit(s == 55 ? 0 : 1);
}
```

---
## E.3 实验 1：用 `-###` 看 driver 编排的两步

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d \
  -O2 -mrelax -static -nostdlib -fuse-ld=lld -### -o hello.elf hello.c
```
**实测**（节选；`...` 只省略重复的 `-target-feature`，`$T` 替换绝对前缀）：
```text
"$T/bin/clang-24" "-cc1" "-triple" "riscv64-unknown-unknown-elf" "-O2" \
  "-emit-obj" ... "-static-define" "-mrelocation-model" "static" ... \
  "-target-feature" "+relax" "-target-abi" "lp64d" ... \
  "-o" "/tmp/hello-xxxx.o" "-x" "c" "hello.c"
"$T/bin/ld.lld" "-Bstatic" "-m" "elf64lriscv" "-X" \
  "-L.../lib/clang-runtimes/riscv64-unknown-elf/rv64imafdc/lp64d/lib" \
  "/tmp/hello-xxxx.o" "-o" "hello.elf"
```

三个要点：

- **只有两步**：`cc1` 生成临时 `.o`，`ld.lld` 链接。`-###` 不执行，只打印。
- `-target-abi lp64d`、`+relax`、`-mrelocation-model static` 都进了 cc1；它们分别决定 ABI、重定位标记和链接方式。
- 因为 `-nostdlib`，ld 行里**没有** `crt0.o`、`-lc`、`libclang_rt.builtins.a`；加上这些输入就是 E.11 的 F1。`-###` 只走到 driver 拼完命令，后端的 ABI note 不在这一步出现（Ch 24 §24.2）。

---
## E.4 实验 2：`hello.ll` 与 `hello.s` 的对应

```bash
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax"
$CC -S -emit-llvm -o hello.ll hello.c
$CC -S -o hello.s hello.c
```
**实测**（`hello.ll` 的 `_start` 正文）：
```llvm
define dso_local void @_start() local_unnamed_addr #0 {
entry:
  %0 = load volatile i64, ptr @N, align 8, !tbaa !11
  %call = tail call fastcc i64 @sum(i64 noundef %0)
  %cmp = icmp ne i64 %call, 55
  %conv = select i1 %cmp, i64 14, i64 13
  %1 = select i1 %cmp, i64 ptrtoint (ptr @.str.1 to i64), i64 ptrtoint (ptr @.str to i64)
  %2 = tail call i64 asm sideeffect "ecall", "={x10},{x11},{x12},{x17},{x10},~{memory}"(i64 %1, i64 range(i64 13, 15) %conv, i64 64, i64 1)
  %conv7 = zext i1 %cmp to i64
  tail call void asm sideeffect "ecall", "{x10},{x17},~{memory}"(i64 range(i64 0, 2) %conv7, i64 93)
  unreachable
}
```
**实测**（`hello.s` 的同一函数，节选；`...` 为省略）：
```asm
_start:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	lui	a0, %hi(N)
	ld	a0, %lo(N)(a0)
	call	sum
	li	a1, 55
	addi	a2, a0, -55
	snez	a3, a2
	addi	a2, a3, 13
	bne	a0, a1, .LBB0_2
	...
.LBB0_3:
	li	a7, 64
	li	a0, 1
	#APP
	ecall
	#NO_APP
	li	a7, 93
	mv	a0, a3
	#APP
	ecall
	#NO_APP
```

| IR | 汇编 | 说明 |
|---|---|---|
| `load volatile i64, ptr @N` | `lui %hi(N)` + `ld %lo(N)` | 全局地址拆成 HI20/LO12，带 `R_RISCV_RELAX` |
| `tail call fastcc @sum` | `call sum` | `call` 是伪指令，`.o` 里是 `auipc+jalr` + `R_RISCV_CALL_PLT` |
| 两个 `select` | `bne` + 两个 `lui/addi` | 分支选消息指针；长度用 `addi/snez/addi` 算 |
| `asm sideeffect "ecall"` | `#APP ecall #NO_APP` | 内联汇编原样保留 |

`hello.s` 开头的 `.attribute 4, 16` 与 `.attribute 5, "rv64..."` 就是 `.o` 的 `e_flags`/`Tag_arch` 来源；完整两行见 E.5。

编译器的 `.option push/arch` 只在函数的 `target` 与模块 ISA 不一致时出现；`__attribute__((target("arch=+zba")))` **实测**会发 `.option push` / `.option arch, +zba` / `.option pop`（`RISCVAsmPrinter.cpp:550-596`）。`.option norelax` 是另一条路，见 E.10。

---
## E.5 实验 3：`hello.o` 的 `e_flags`、`.attribute` 与重定位

沿用 E.4 的 `$CC`：

```bash
$CC -c -o hello.o hello.c
$T/bin/llvm-readelf -h -A -r hello.o
```
**实测**（`-h` 节选）：
```text
Type:                              REL (Relocatable file)
Machine:                           RISC-V
Entry point address:               0x0
Flags:                             0x5, RVC, double-float ABI
```
**实测**（`llvm-readelf -A hello.o | sed -n '9,19p'`，两条属性）：
```text
      Attribute {
        Tag: 4
        Value: 16
        TagName: stack_align
        Description: Stack alignment is 16-bytes
      }
      Attribute {
        Tag: 5
        TagName: arch
        Value: rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0
      }
```
**实测**（`-r` 的关键条目，`grep` 过滤）：
```text
    Offset             Info             Type               Symbol's Value  Symbol's Name + Addend
0000000000000004  000000030000001a R_RISCV_HI20           0000000000000000 N + 0
0000000000000004  0000000000000033 R_RISCV_RELAX                     0
0000000000000008  000000030000001b R_RISCV_LO12_I         0000000000000000 N + 0
0000000000000008  0000000000000033 R_RISCV_RELAX                     0
000000000000000c  0000000400000013 R_RISCV_CALL_PLT       000000000000004e sum + 0
000000000000000c  0000000000000033 R_RISCV_RELAX                     0
0000000000000024  0000000500000010 R_RISCV_BRANCH         0000000000000032 .L0  + 0
0000000000000028  000000060000001a R_RISCV_HI20           0000000000000000 .L.str + 0
0000000000000028  0000000000000033 R_RISCV_RELAX                     0
```

- `Flags: 0x5` = `EF_RISCV_RVC (0x1)` | `EF_RISCV_FLOAT_ABI_DOUBLE (0x4)`。`e_flags` 是**模块级**的，由 `-mabi=lp64d` 和 `+c` 决定（`RISCVELFStreamer.cpp:111-134`）。
- `Tag 4 = 16` 是 psABI 的栈对齐；`Tag 5 = arch` 是规范化后的 ISA 字符串，含 `zca/zcd`。`.attribute` 比 `e_flags` 细，`+zba` 只在这里出现。
- 每条 `HI20`/`LO12`/`CALL_PLT` 后都跟 `R_RISCV_RELAX`。它不代表“已经 relax”，只表示“链接器可以尝试”；没有它，`--relax` 不会动这条。
- `R_RISCV_CALL_PLT` 来自 `call` 伪指令；`R_RISCV_BRANCH`/`R_RISCV_RVC_JUMP` 是分支与压缩跳转的 fixup（`RISCVELFObjectWriter.cpp:95-101`）。

---
## E.6 实验 4：`ld.lld` 链接，入口与段

```bash
$T/bin/ld.lld -e _start -static --relax -o hello.elf hello.o
$T/bin/llvm-readelf -h -S -l hello.elf
```
**实测**（`-h -l` 节选）：
```text
Type:                              EXEC (Executable file)
Entry point address:               0x111ae
Flags:                             0x5, RVC, double-float ABI

Program Headers:
  Type           Offset   VirtAddr           PhysAddr           FileSiz  MemSiz   Flg Align
  LOAD           0x000000 0x0000000000010000 0x0000000000010000 0x0001ad 0x0001ad R   0x1000
  LOAD           0x0001ae 0x00000000000111ae 0x00000000000111ae 0x000070 0x000070 R E 0x1000
  LOAD           0x000220 0x0000000000012220 0x0000000000012220 0x000008 0x000008 RW  0x1000
```

四个检查点：

1. **入口**：`0x111ae` 就是 `_start` 的地址（`llvm-readelf -s hello.elf | grep _start` 可对照）。`-e _start` 显式指定，不依赖 lld 的默认值。
2. **类型**：`EXEC` 且没有 `PT_INTERP`，说明是静态可执行文件；`-static` 负责这一点。
3. **段**：三个 `LOAD` 分别是 R、R E、RW；`.text` 在第二个段，起始 `0x111ae`。
4. **大小**：`.text` 的 `FileSiz` 是 `0x70`；下一节会看到 `--no-relax` 是 `0x74`。

---
## E.7 实验 5：`--relax` vs `--no-relax`：`call` → `jal`

```bash
for opt in --no-relax --relax; do
  $T/bin/ld.lld -e _start -static $opt -o "hello${opt}.elf" hello.o
done
$T/bin/llvm-readelf -S hello--no-relax.elf | grep ' .text '
$T/bin/llvm-readelf -S hello--relax.elf   | grep ' .text '
```

**实测**（`.text` 大小；第一行 `--no-relax`，第二行 `--relax`）：
```text
  [ 2] .text             PROGBITS        00000000000111ae 0001ae 000074 00  AX  0   0  2
  [ 2] .text             PROGBITS        00000000000111ae 0001ae 000070 00  AX  0   0  2
```

**实测**（同一条 `call sum` 的前后）：
```asm
# --no-relax
   111ba: auipc ra, 0x0
   111be: jalr  0x42(ra) <sum>
# --relax
   111ba: jal   0x111f8 <sum>
```

`call` 是 8 字节的 `auipc+jalr`，`jal` 是 4 字节；只省 4 字节，后面的地址整体前移。`--relax` 是 lld 的默认值，`--no-relax` 显式关闭；driver 在 `-mno-relax` 时会追加 `-X --no-relax`（`BareMetal.cpp:558-561`）。

**实测**：两个可执行文件都输出 `toolchain ok`，`exit=0`。练习 B 已从行为侧验证过 relax 前后输出一致；这里用 `.text` 大小与反汇编再看一次，原则是“改指令、也改所有引用地址，迭代到不动点”（Ch 12 §12.6）。

---
## E.8 实验 6：`llvm-objdump -d` 与 `-r` 联合看

```bash
$T/bin/llvm-objdump -dr --no-show-raw-insn hello.o
```

**实测**（`_start` 节选）：
```text
0000000000000000 <_start>:
       0:      	addi	sp, sp, -0x10
       2:      	sd	ra, 0x8(sp)
       4:      	lui	a0, 0x0
		0000000000000004:  R_RISCV_HI20	N
		0000000000000004:  R_RISCV_RELAX	*ABS*
       8:      	ld	a0, 0x0(a0)
		0000000000000008:  R_RISCV_LO12_I	N
		0000000000000008:  R_RISCV_RELAX	*ABS*
       c:      	auipc	ra, 0x0
		000000000000000c:  R_RISCV_CALL_PLT	sum
		000000000000000c:  R_RISCV_RELAX	*ABS*
      10:      	jalr	ra <_start+0xc>
      14:      	li	a1, 0x37
      18:      	addi	a2, a0, -0x37
      1c:      	snez	a3, a2
      20:      	addi	a2, a3, 0xd
      24:      	bne	a0, a1, 0x24 <_start+0x24>
		0000000000000024:  R_RISCV_BRANCH	.L0
```

三个“别被骗”：

- `.o` 里的 `lui a0, 0x0`、`ld a0, 0x0(a0)` 都是**占位**；地址要等 lld 填，显示 0 不代表生成错了。
- `mv a1, a1` 是 `addi a1, a1, 0` 的别名；`R_RISCV_LO12_I` 会改写它的立即数（见完整输出）。
- `jalr ra <_start+0xc>` 是占位目标，因为 `R_RISCV_CALL_PLT` 尚未解析；链接后变成 `jal <sum>`（E.7）。

`-d` 看指令，`-r` 看重定位；两者必须一起看，否则会把占位当最终结果。

---
## E.9 实验 7：qemu 运行与退出码

```bash
/usr/bin/qemu-riscv64-static ./hello.elf; echo "exit=$?"
/usr/bin/qemu-riscv64-static -strace ./hello.elf
```

**实测**：
```text
toolchain ok
exit=0

toolchain ok
270532 write(1,0x10190,13) = 13
270532 exit(0)
```

`write` 的参数是 `(fd=1, buf=0x10190, n=13)`，返回 13（PID 每次不同）；`exit(0)` 对应 `a7=93`、`a0=0`。`n=13` 正好是 `"toolchain ok\n"` 的长度；传 14 会把末尾 NUL 也写出去。

<details>
<summary>展开：qemu 输出校验的写法与退出码语义</summary>
```bash
set -euo pipefail
out=$(/usr/bin/qemu-riscv64-static ./hello.elf)   # 命令替换去掉末尾换行
test "$out" = "toolchain ok"                      # 精确匹配 stdout
```

退出码：`0` 正常；`1` 是程序自己 `exit(1)`；`132 = 128+4` 是 SIGILL（入口错或 ISA 不支持）；`139 = 128+11` 是 SIGSEGV（地址错）。F2 会看到 132，F4 会看到 1。
</details>

**RVV 说明**：本练习只跑 scalar。`qemu-riscv64-static` 6.2 的 user-mode 实现 draft V 0.7.1，LLVM 24 生成 RVV 1.0；带 `vadd.vv` 的程序会 SIGILL。Ch 26 实验 7 与练习 C 已记录，本练习的向量端到端**未实测**。

---
## E.10 实验 8（可选）：linker script、`__global_pointer$` 与 gp relax

```bash
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax"
sed 's/^void _start(void)/void _start_c(void)/' hello.c > hello_c.c
cat > start.S <<'EOF'
	.text
	.globl _start
_start:
	.option push
	.option norelax
	la gp, __global_pointer$
	.option pop
	call _start_c
EOF
$CC -c -o start.o start.S
$CC -msmall-data-limit=8 -c -o hello_sd.o hello_c.c    # N 进 .sdata
cat > layout.ld <<'EOF'
SECTIONS {
  . = 0x10000; .text   : { *(.text*) }
  . = 0x20000; .rodata : { *(.rodata*) }
  . = 0x30000; .sdata  : { *(.sdata*) }
               .data   : { *(.data*) }
               .bss    : { *(.bss*) *(COMMON) }
}
EOF
$T/bin/ld.lld -T layout.ld -e _start -static --relax --relax-gp -o layout.elf start.o hello_sd.o
$T/bin/llvm-readelf -S -sW layout.elf
$T/bin/llvm-objdump -d --no-show-raw-insn layout.elf
```

`start.S` 在 `.option norelax` 保护下初始化 `gp`，再跳到 C 的 `_start_c`。**实测**（布局与符号）：
```text
  [ 1] .text             PROGBITS        0000000000010000 001000 000078 00  AX  0   0  2
  [ 2] .rodata           PROGBITS        0000000000020000 002000 00001d 01 AMS  0   0  1
  [ 3] .sdata            PROGBITS        0000000000030000 003000 000008 00  WA  0   0  8
     6: 0000000000030000     8 OBJECT  LOCAL  DEFAULT     3 N
    16: 0000000000030800     0 NOTYPE  GLOBAL DEFAULT     3 __global_pointer$
```
**实测**（gp relax 前后）：
```asm
# --relax（无 --relax-gp）
   10010: lui  a0, 0x30
   10014: ld   a0, 0x0(a0)
# --relax --relax-gp
   10010: ld   a0, -0x800(gp)
```

两条路径都输出 `toolchain ok`、`exit=0`。`__global_pointer$` 由 lld 定义为 `.sdata + 0x800`（`Writer.cpp:1852-1860`）；本例 `.sdata = 0x30000`，`gp = 0x30800`，`N = 0x30000`，位移 `-0x800` 正好在 12 位立即数范围内。gp relaxation 需要三条件：`--relax-gp`、`__global_pointer$` 被引用（`la gp` 会引用）、目标在 `gp ± 2 KiB`。少一个就退回 `lui/ld`。

---
## E.11 失败注入：四个真实报错

统一前缀：`CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax"`。

### F1 忘了 `-nostdlib`

```bash
$CC -static -fuse-ld=lld -o bad1.elf hello.c
```

**实测**（路径用 `...` 省略）：
```text
ld.lld: error: cannot open crt0.o: No such file or directory
ld.lld: error: cannot open .../lib/clang/24/lib/riscv64-unknown-unknown-elf/libclang_rt.builtins.a: No such file or directory
ld.lld: error: unable to find library -lc
clang: error: linker command failed with exit code 1 (use -v to see invocation)
```

`-static` 只选静态链接，不负责去掉启动文件和库。裸机 triple 的 driver 会加 `crt0.o`、`libclang_rt.builtins.a`、`-lc`（`BareMetal.cpp:572-580`），而本机没有 riscv sysroot。

### F2 忘了 `-e _start`

```bash
sed 's/^void _start(void)/void user_entry(void)/' hello.c > hello_ne.c
$CC -c -o hello_ne.o hello_ne.c
$T/bin/ld.lld -static --relax -o noentry.elf hello_ne.o
/usr/bin/qemu-riscv64-static ./noentry.elf; echo "qemu exit=$?"
```

**实测**：
```text
ld.lld: warning: cannot find entry symbol _start; not setting start address
  Type:                              EXEC (Executable file)
  Entry point address:               0x0
/bin/bash: ... Illegal instruction (core dumped)
qemu exit=132
```

lld 的默认入口是 `_start`；找不到只警告，入口留 0。qemu 从 0 开始执行，那里是 ELF header，不是代码，于是 SIGILL。

### F3 `-mabi` 不一致

```bash
$CC -mabi=lp64  -c -o hello_lp64.o hello_c.c
$CC -mabi=lp64d -c -o start_d.o   start.S
$T/bin/ld.lld -e _start -static -o bad3.elf start_d.o hello_lp64.o
```

**实测**：
```text
  Flags:                             0x1, RVC
  Flags:                             0x5, RVC, double-float ABI
ld.lld: error: hello_lp64.o: cannot link object files with different floating-point ABI from start_d.o
```

两条 `Flags` 分别来自 `hello_lp64.o` 与 `start_d.o`；`calcEFlags` 只比较 `EF_RISCV_FLOAT_ABI` 这两位（`RISCV.cpp:167-193`）；RVC 位是 OR 进去的。两个 `.o` 各自 lowering 都可能正确，契约却在链接期对不上。

### F4 漏了 `.option norelax`，又开了 `--relax-gp`

```asm
# start_bad.S
	lui	gp, %hi(__global_pointer$)
	addi	gp, gp, %lo(__global_pointer$)
	call	_start_c
```

```bash
$CC -c -o start_bad.o start_bad.S
$T/bin/ld.lld -e _start -static --relax --relax-gp -o badgp.elf start_bad.o hello_sd.o
/usr/bin/qemu-riscv64-static ./badgp.elf; echo "exit=$?"
```

**实测**（上面的链接命令）：
```text
00000000000111ae <_start>:
   111ae: mv   gp, gp
   111b2: jal  0x111b6 <_start_c>
toolchain BAD
exit=1
```

gp relaxation 发现符号就是 `gp` 自己，位移为 0，于是删掉 `lui`、把 `addi gp, gp, 0` 改写成 `mv gp, gp`。`gp` 仍是 0，后面的 `ld -0x800(gp)` 读到错误地址，程序走 `BAD` 分支。修复：`.option push/.option norelax/.option pop` 包住初始化（E.10 的 `start.S`）。

<details>
<summary>展开：四个失败的根因、最小修复与“为什么晚一层才报”</summary>

| 注入 | 第一处报错 | 根因 | 最小修复 |
|---|---|---|---|
| 无 `-nostdlib` | lld `cannot open crt0.o` | driver 追加启动文件与 `-lc` | 加 `-nostdlib` |
| 无 `-e _start` | lld warning + entry 0 | 默认入口符号缺失 | 加 `-e _start` 或改名 `_start` |
| `lp64` vs `lp64d` | lld `different floating-point ABI` | `e_flags` 的浮点 ABI 位不同 | 两边统一 `-mabi` |
| 无 `.option norelax` | qemu 输出 BAD | gp relax 把 `lui/addi` 改成 `mv gp,gp` | 用 `.option norelax` 保护 gp 初始化 |

F1/F3 在链接期才报，因为单文件编译不知道另一个 `.o` 的 `e_flags`；F2/F4 在运行期才报，因为 lld 只保证 ELF 结构合法，不保证入口地址上是代码、也不保证 `gp` 的值。端到端证据链的价值就是把这四类错误各自钉在最小复现上。
</details>

---
## E.12 源码落点

| 行为 | 文件 | 行号（本树 `0fc5afa04c23`） |
|---|---|---|
| `-mrelax` → `+relax` feature | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp` | 69、141-145 |
| `-mno-relax` → `-X --no-relax` | `clang/lib/Driver/ToolChains/BareMetal.cpp` | 558-561 |
| 默认 `crt0.o`、`-lc` | `clang/lib/Driver/ToolChains/BareMetal.cpp` | 572-580 |
| `e_flags` 的 RVC/浮点 ABI | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp` | 111-134 |
| `.attribute stack_align` / `arch` | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVTargetStreamer.cpp` | 65-89 |
| `call` → `R_RISCV_CALL_PLT` | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFObjectWriter.cpp` | 95-101 |
| `e_flags` 链接期比较 | `lld/ELF/Arch/RISCV.cpp` | 167-193 |
| `call` → `jal` / `c.jal` | `lld/ELF/Arch/RISCV.cpp` | 933-990 |
| `HI20/LO12` → `x0` / `gp` | `lld/ELF/Arch/RISCV.cpp` | 992-1035 |
| relax 不动点循环 | `lld/ELF/Writer.cpp` | 1500-1545 |
| `__global_pointer$ = .sdata + 0x800` | `lld/ELF/Writer.cpp` | 1852-1868 |

读源码按“谁写元数据、谁消费元数据”：driver 拼参数（`RISCV.cpp`、`BareMetal.cpp`）→ 后端写 `.o`（`RISCVELFStreamer`、`emitTargetAttributes`）→ lld 校验 `e_flags` 并改写 `.text`（`calcEFlags`、`relaxOnce`）→ qemu 只消费最终 ELF。

---
## E.13 可跑实验：证据链脚本模板

脚本把每一步落盘，最后用 qemu 的 stdout 做硬校验；只写 `$OUT`，不动源码树。
<details>
<summary>展开：完整的 e2e.sh（已在本机对 hello.c 实测通过）</summary>
```bash
#!/usr/bin/env bash
set -euo pipefail
T=${T:-/home/ubuntu/llvm/builds/ct-instr-trunk}
SRC=${1:-hello.c}
OUT=${2:-out}
mkdir -p "$OUT"
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax"

$CC -S -emit-llvm -o "$OUT/hello.ll" "$SRC"
$CC -S             -o "$OUT/hello.s"  "$SRC"
$CC -c             -o "$OUT/hello.o"  "$SRC"
$T/bin/llvm-readelf -h -A -r "$OUT/hello.o" > "$OUT/obj.meta.txt"
$T/bin/ld.lld -e _start -static --relax -o "$OUT/hello.elf" "$OUT/hello.o"
$T/bin/llvm-readelf -h -S -l "$OUT/hello.elf" > "$OUT/elf.meta.txt"
$T/bin/llvm-objdump -d --no-show-raw-insn "$OUT/hello.elf" > "$OUT/hello.dis"

out=$(/usr/bin/qemu-riscv64-static "$OUT/hello.elf")
printf '%s\n' "$out"
test "$out" = "toolchain ok"
```

运行 `bash e2e.sh hello.c /tmp/pe-out`，**实测**输出 `toolchain ok`。产物为 `hello.ll`、`hello.s`、`hello.o`、`obj.meta.txt`、`hello.elf`、`elf.meta.txt`、`hello.dis`。失败时 `set -e` 在第一个非零命令处停下，`$OUT` 里保留中间物，正好定位是哪一步坏了。
</details>

**PR 描述里贴哪几段**：

1. **复现命令**：`bash e2e.sh repro.c /tmp/repro`，加上 `T` 的构建 hash。
2. **driver 证据**：`clang -###` 的 cc1 行与 ld 行（标出 `-target-abi`、`+relax`、`-nostdlib`）。
3. **`.o` 证据**：`llvm-readelf -h` 的 `Flags`、`-A` 的 `Tag_arch`、`-r` 的关键重定位（改前/改后各一组）。
4. **`.elf` 证据**：`llvm-readelf -h -S -l` 的 `Entry`/`.text` 大小；`llvm-objdump -d` 的改前/改后（`call` vs `jal`、`lui/ld` vs `ld gp`）。
5. **运行证据**：qemu 的 stdout 原文 + `exit=$?`；失败时附 `-strace` 的 `write`/`exit` 行。

只贴“修复后能跑”不够；reviewer 要看的是**同一份输入在改前/改后产生不同证据**，以及这条命令能在他的机器上重跑。

---
## E.14 踩雷集锦

1. **`-static` 不等于 `-nostdlib`。** `-static` 选静态链接方式；`-nostdlib` 去掉 `crt0.o`、`-lc` 和默认库。只写 `-static` 会得到 F1 的三个 error。裸机 triple 的启动文件叫 `crt0.o`，不是 `crt1.o`。
2. **`_start` 的栈对齐要自己保证。** psABI 要求函数入口 `sp` 16 字节对齐；qemu 初始 `sp` 满足，但压了奇数个 8 字节槽再调 C 函数就会破坏对齐。编译器用 `addi sp, sp, -16`；手写时至少 `andi sp, sp, -16`。
3. **`gp` 初始化必须 `.option norelax`。** `lui gp,%hi(__global_pointer$)` + `addi gp,gp,%lo(...)` 在 `--relax-gp` 下会被改成 `mv gp, gp`（F4）。这是 relaxation 的正确行为，保护手段是汇编器指令。
4. **三个“关 relax”的开关作用点不同。** `-mno-relax` 改编译器 feature 并让 driver 加 `--no-relax`；`--no-relax` 只关链接期；`.option norelax` 只关汇编器为一段代码发 `R_RISCV_RELAX` 标记。改错层，症状会出现在另一层。
5. **qemu-riscv64-static 只跑静态程序。** 动态 ELF 需要 riscv 的 `ld.so` 和 sysroot，本机没有（动态链接**未实测**）。`-static -nostdlib` 是最可靠的端到端路径；`-L /nonexistent` 对静态 ELF 无影响（实测），可用来确认“确实是静态”。
6. **`-fuse-ld=lld` 不能省。** 不加时 driver 可能调用 PATH 里的 GNU `ld`。`-###` 的第二个 `"..."` 行就是实际链接器路径；先看这一行，再怀疑参数。
7. **`llvm-readelf` 与 GNU `readelf` 的 `-A` 格式不同。** `-h` 的 `Flags: 0x5, RVC, double-float ABI` 两边一致；`-A` 下 LLVM 输出 `TagName: arch` / `Value:`，GNU 输出 `Tag_RISCV_arch: "..."`。脚本解析 `-h` 的数值，不要 grep 本地化文本。
8. **RVV 版本错配会 SIGILL。** qemu 6.2 user-mode 是 draft V 0.7.1，LLVM 24 是 RVV 1.0。scalar 可以端到端；带 `vsetvli`/`vadd.vv` 的程序在本机**未实测通过**，不要写进“已验证”的 PR 证据。

---
## E.15 本章重点整理

- **七个产物各回答一个问题**：IR、`.s`、`.o`、`.elf`、元数据、反汇编、运行输出；缺一个就把错误留给下一层。
- **`-###` 只看两步**：cc1 与 ld。`-nostdlib` 决定 ld 行里有没有 `crt0.o`/`-lc`；`-target-abi`、`+relax` 决定 `.o` 的内容。
- **`.o` 三件套**：`e_flags=0x5`（RVC + double-float）、`.attribute 5=arch`（细到 `zba`/`zca`）、`R_RISCV_RELAX` 配对（relax 候选标记）。
- **lld 两件事**：按 `e_flags` 拒绝混链；按 `R_RISCV_RELAX` 把 `call` 改成 `jal`、把 `lui/ld` 改成 `ld gp`，并同步改所有地址。
- **gp 三条件**：`--relax-gp`、`__global_pointer$` 被引用、目标在 `gp ± 2 KiB`；`gp = .sdata + 0x800`。
- **qemu 证据**：stdout + `exit=$?` + 可选 `-strace`；`132` SIGILL、`139` SIGSEGV、`1` 可能是程序自己的 `exit(1)`。

---
## E.16 自我检核

- [ ] 能画出 E.2 的七个产物，并说出每个能证明/不能证明什么
- [ ] 能从 `clang -###` 指出 cc1 的 `-target-abi`、`+relax`、`-mrelocation-model` 和 ld 的输入
- [ ] 能解释 `e_flags=0x5` 的两位，以及为什么 `+zba` 不在 `e_flags` 里
- [ ] 能对照 `hello.ll` 与 `hello.s`，指出 `load`、`call`、`select`、`ecall` 的落点
- [ ] 能解释 `R_RISCV_RELAX` 为什么成对出现，以及 `--no-relax` 后 `.text` 为什么大 4 字节
- [ ] 能说出 `__global_pointer$` 的定义、gp relax 三条件，以及 `.option norelax` 保护什么
- [ ] 能独立复现 F1–F4，并说出每个错误第一次出现的层
- [ ] 能用 `e2e.sh` 对一份新源码产出全部证据，并在 PR 描述里只贴关键五行

---
## E.17 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChains/BareMetal.cpp`**：558-561（`-X --no-relax`）、572-580（`crt0.o` 与 `-lc` 的追加条件）。F1/F2 的根因。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Driver/ToolChains/Arch/RISCV.cpp`**：69（`getRISCVTargetFeatures`）、141-145（`-mrelax` → `+relax`）。`-###` 里 feature 的来源。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp`**：111-134（`e_flags` 的 `RVC`/`FLOAT_ABI`/`RVE`/`TSO` 位）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVTargetStreamer.cpp`**：65-89（`stack_align`/`arch`）、138-155（`.option arch` 文本形式）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/ELF/Arch/RISCV.cpp`**：167-193（`calcEFlags`）、933-990（`relaxCall`）、992-1035（`relaxHi20Lo12`，含 gp 路径）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/ELF/Writer.cpp`**：1500-1545（relax 不动点）、1852-1868（`__global_pointer$` 与 `riscvGlobalPointer`）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/test/ELF/riscv-relax-*.s`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/`**：改 driver、streamer 或 relaxation 时的第一道回归网。

→ 下一章：[Ch 27 — lit 测试与 update 脚本](./27-lit-testing-and-update-scripts.md)
