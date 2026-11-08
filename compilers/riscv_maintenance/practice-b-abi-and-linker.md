# 练习 B — ABI 与链接器：让两个目标文件对得上

> **目标**：把两个 `.o` 链接成能在 qemu 上真跑的 RV64 程序，用运行结果验证 psABI 的参数分类、`e_flags` 的链接期检查、relaxation 的“改写但不改行为”，以及 static TLS 的 local-exec 序列。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON），源码树 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`）；`/usr/bin/qemu-riscv64-static`（QEMU 6.2）。实验只在 `/tmp` 写文件，不改源码树。

> **前置**：读完 Ch 09（调用约定 lowering）、Ch 10（栈帧）、Ch 11（code model / TLS / GP）、Ch 12（LLD 重定位与 relaxation）。本练习不重复推导，只把四章串成一次端到端实验。

---

## B.1 为什么 ABI 练习比读文档有效

psABI 的每条规则都短，组合起来却有一百多个分支。文档给你规则，不给你**规则在本工具链里落成的指令**：

| psABI 规则 | 只读文档的直觉 | 链接后真跑的真相 |
|---|---|---|
| 整型参数用 `a0`–`a7` | 第 9 个进栈 | 对；但 RV32 的 `i64`、RV64 的 `i128` 各占两个槽 |
| 浮点参数用 `fa0`–`fa7` | 一律走 FPR | 对；但 **vararg 的浮点一律走整型寄存器** |
| 聚合体 ≤2×XLEN 拆寄存器 | `{long,long,long}` 拆成 `a0/a1` + 栈 | 错：>2×XLEN 是 **by reference**，只传一个指针（B.5） |
| 返回值 >2×XLEN 用 `sret` | `sret` 指针占 `a0` | 对；但 clang 生成 `%byval-temp` + `memcpy`，不是 IR `byval` |
| vararg 的偶数寄存器规则 | 16 字节对齐参数跳过奇数寄存器 | 对；但 ILP32E 例外，且只在“拆分后仍对齐”时触发 |

更关键的是：**两个翻译单元各自 lowering 正确，契约仍可能对不上**。前端把 `-mabi` 写进模块，后端写进 `.o` 的 `e_flags`，链接器只比较那一位。症状要么在链接期，要么在运行期，不会在单文件编译期出现。本练习要一次拿全三类证据：链接器**拒绝**不匹配（B.3）、**改写**指令但行为不变（B.7）、程序**真跑**出预期值（B.5–B.9）。

---

## B.2 先建立直觉：一次链接的四层契约

```
  C 源码 (a.c, b.c)
        │  clang -cc1
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ ① 前端 ABI 分类 (clang/lib/CodeGen + RISC-V ABIInfo)        │
  │    ≤2×XLEN 聚合体 → 拆; >2×XLEN → 指针; 大返回 → sret       │
  └──────────────────────────────┬──────────────────────────────┘
                                 ▼
  IR: byval / sret / zeroext / signext / "target-abi"="lp64d"
        │  llc (SelectionDAG)
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ ② 后端 CC_RISCV (RISCVCallingConv.cpp, Ch 09)               │
  │    a0–a7 / fa0–fa7 / 栈槽 / vararg save area                │
  └──────────────────────────────┬──────────────────────────────┘
                                 ▼
  .o: e_flags (RVC | 浮点 ABI), .riscv.attributes, .rela.text (+RELAX)
        │  ld.lld (Ch 12)
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ ③ 链接器: 合并 e_flags、解析重定位、relaxation               │
  │    call → jal; lui/ld → ld gp; TPREL 落值; gp = .sdata+0x800 │
  └──────────────────────────────┬──────────────────────────────┘
                                 ▼
  可执行 ELF ── qemu-riscv64-static ──▶ ④ 运行时: 看寄存器与栈
```

| 层 | 典型错误 | 报错时机 | 症状 |
|---|---|---|---|
| ① 前端分类 | 大聚合体当成寄存器参数 | 编译期不报 | 两个 TU 各编各的，运行时栈被写坏 |
| ② 后端 CC | `ret`/`call` 两侧规则不一致 | 编译期不报 | 返回值是垃圾，或第 9 个参数读错槽 |
| ③ `e_flags` | 一个 `lp64`、一个 `lp64d` | **链接期** | `different floating-point ABI` |
| ③ relaxation | 位移算错 / `gp` 未初始化 | 链接期不报 | 运行崩溃或输出错误，反汇编却“都合法” |
| ④ 运行时 | 入口 `sp` 未对齐、TLS 模型用错 | 运行期 | qemu 段错误或 TLS 读到 0 |

---

## B.3 实验 1：ABI 不匹配必须在链接期被抓

> 约定：从本节起，`sys_write`/`sys_exit` 直接用 B.11 的裸 syscall 骨架；先读一遍 B.11，后面的命令才能原样复制。

```c
/* abi_int.c */  long add(long a, long b) { return a + b; }
/* abi_main.c */ extern long add(long, long); long _start(void) { return add(2, 3); }
```

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/abi && cd /tmp/abi
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64  -O2 -c -o int_lp64.o  abi_int.c
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -c -o main_lp64d.o abi_main.c
$T/bin/llvm-readelf -h int_lp64.o | grep Flags; $T/bin/llvm-readelf -h main_lp64d.o | grep Flags
$T/bin/ld.lld -static -o mismatch.elf int_lp64.o main_lp64d.o; echo "ld exit=$?"
```

**实测**（LLVM 24 的 lld，报错原文与 GNU ld 不同）：

```
  Flags: 0x1, RVC
  Flags: 0x5, RVC, double-float ABI
ld.lld: error: main_lp64d.o: cannot link object files with different floating-point ABI from int_lp64.o
ld exit=1
```

`0x1` 与 `0x5` 的差别只在 `EF_RISCV_FLOAT_ABI`（mask `0x6`）：soft = 0，single = 2，double = 4；`EF_RISCV_RVC` 是 `0x1`，两边都有。为什么必须检查？看 `double` 在两种 ABI 下的形状（`f3.c` 见 B.4 的 `add3d`）：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64 -O2 -S -o - f3.c
```

**实测**（`lp64`：参数在 `a0`–`a2`，进出各搬一次）：

```asm
add3d:
	fmv.d.x	fa5, a1         # 三个参数在 a0–a2
	fmv.d.x	fa4, a0
	fadd.d	fa5, fa4, fa5   # 后续 fadd、fmv.x.d 略
	ret
```

`lp64d` 下三个参数直接在 `fa0/fa1/fa2`，没有 `fmv`。混链时被调用者会去 `fa0` 读一个从未写过的寄存器。

<details>
<summary>解答：报错原文、修复方式，以及“只用整型为什么也能链”</summary>

修复一：两边都用同一个 `-mabi`。修复二：两边都用 `lp64d`——即使被调函数**一个浮点都没有**：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -c -o int_lp64d.o abi_int.c
$T/bin/llvm-readelf -h int_lp64d.o | grep Flags    # 0x5, RVC, double-float ABI
$T/bin/ld.lld -static -o ok.elf int_lp64d.o main_lp64d.o && echo link-ok
```

原因：`e_flags` 是**模块级**的，由 `-mabi` 决定，不由“这个函数用了什么类型”决定。`RISCVELFStreamer::finish()`（`RISCVELFStreamer.cpp:112`）只看 `getTargetABI()`；`RISCV::calcEFlags()`（`lld/ELF/Arch/RISCV.cpp:167`）只比较该字段。

反过来的坑：`-march=rv64imac`（无 D）+ `-mabi=lp64d` 不报错，只打 note。**实测**：

```
note: hard-float 'd' ABI can't be used for a target that doesn't support the D instruction set extension (ignoring target-abi)
  Flags: 0x5, RVC, double-float ABI
    0x8 R_RISCV_CALL_PLT __adddf3 0x0
```

代码走 soft-float（`__adddf3`，参数在 `a0`–`a2`），`e_flags` 却写着 double-float。链接器不会替你发现，只有 `__adddf3` 未定义时才顺带失败。
</details>

---

## B.4 实验 2：浮点参数走哪

```c
/* f3.c */ double add3d(double a, double b, double c) { return a + b + c; }
           float  add3f(float a, float b, float c)  { return a + b + c; }
```

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -o - f3.c
```

**实测**：

```asm
add3d:
	fadd.d	fa5, fa0, fa1
	fadd.d	fa0, fa5, fa2
	ret
add3f:
	fadd.s	fa5, fa0, fa1
	fadd.s	fa0, fa5, fa2
	ret
```

ABI 名 `fa0/fa1/fa2` 对应物理寄存器 `f10/f11/f12`；MIR 里看到的是物理名。先确认映射，再看 liveins：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -emit-llvm -o f3.ll f3.c
$T/bin/llc -mtriple=riscv64-unknown-elf -mattr=+m,+a,+c,+d \
    -stop-after=finalize-isel -simplify-mir -o - f3.ll
```

**实测**（`add3d` 入口）：

```
name:            add3d
liveins:
  - { reg: '$f10_d', virtual-reg: '%0' }
  - { reg: '$f11_d', virtual-reg: '%1' }
  - { reg: '$f12_d', virtual-reg: '%2' }
```

`$f10_d` 就是 `fa0`（`d` 后缀表示 64 位子寄存器）。`-stop-after=finalize-isel` 停在寄存器分配之前，参数还带 ABI 寄存器的 COPY，正好验证“值被放进了谁”。

`llc` 没收到 `-target-abi` 仍按 `lp64d` 分配，因为 IR 带了 `!1 = !{i32 1, !"target-abi", !"lp64d"}`。**ABI 是 IR 的一部分**；手写 `.ll` 时要么补这个标志，要么显式传 `-target-abi=lp64d`。

---

## B.5 实验 3：结构体传参

```c
/* st.c */
struct S2 { int a, b; };       /* 8 B  */
struct S3 { long a, b, c; };   /* 24 B */
struct FF { float x, y; };     /* 8 B, 两个浮点字段 */
long take2(struct S2 s)  { return s.a + s.b; }
long take3(struct S3 s)  { return s.a + s.b + s.c; }
float takeff(struct FF s) { return s.x + s.y; }
```

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -emit-llvm -o st.ll st.c
grep -E '^define' st.ll
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -o - st.c
```

**实测**（前三行来自 `st.c`，最后一行来自另写的 `make3` 定义）：

```llvm
define dso_local … i64 @take2(i64 %s.coerce)
define dso_local … @take3(ptr … dereferenceable(24) %s)
define dso_local float @takeff(float %0, float %1)
define dso_local void @make3(ptr … sret(%struct.S3) align 8 %agg.result, i64 %a, i64 %b, i64 %c)
```

**实测**（汇编对照）：

```asm
take2:                       # 8 字节打包进 a0，取出时再拆
	slli	a1, a0, 32
	add	a0, a0, a1          # 收尾 srai a0, a0, 32
	ret
take3:                       # 24 字节：a0 是一个指针，不是 a0/a1
	ld	a1, 0(a0)
	ld	a2, 8(a0)
	ld	a0, 16(a0)
	add	a1, a1, a2
	add	a0, a0, a1
	ret
takeff:                      # 两个 float 走 FPR
	fadd.s	fa0, fa0, fa1
	ret
```

另写调用者（`struct S3 s = {1,2,3}; take3(s);` 或 `struct S3 s = make3(7,8,9);`）时，`call` 前都会 `mv a0, sp`：前者传临时对象地址，后者传 sret 缓冲区。被调用者的 `sret` 形状是 `sd a1,0(a0); sd a2,8(a0); sd a3,16(a0)`。

MIR 把“谁在哪个寄存器”钉死。**实测**（`take2`/`take3` 都只有 `{ reg: '$x10', ... }`，即 `a0`）：

```
name:            takeff
liveins:
  - { reg: '$f10_f', virtual-reg: '%0' }
  - { reg: '$f11_f', virtual-reg: '%1' }
```

<details>
<summary>解答：三个结构体到底落在哪，以及“24 字节 = a0/a1 + 栈”为什么是错的</summary>

| 类型 | 大小 vs 2×XLEN（16 B） | 分类 | 汇编证据 |
|---|---|---|---|
| `struct {int a,b;}` | 8 ≤ 16 | 拆成 1 个 XLEN，打包进 `a0` | `slli/add/srai` 拆包 |
| `struct {long a,b,c;}` | 24 > 16 | **by reference**，`a0` 是地址 | `ld a1,0(a0)`；调用者 `mv a0,sp` |
| `struct {float x,y;}` | 8 ≤ 16，字段是 FP | 拆成两个 FPR：`fa0/fa1` | `fadd.s fa0,fa0,fa1` |

psABI 原文是 “Aggregates larger than 2×XLEN bits are passed by reference and replaced by a pointer in the argument list.” 所以 24 字节结构体既不会占 `a0/a1`，也不会有一部分落到栈上——它只占**一个**参数寄存器。“寄存器 + 栈”属于 2×XLEN 拆分在寄存器耗尽时的情形（RV32 的 `i64`、RV64 的 `i128`）。

另一个误解：以为 clang 会给大结构体发 IR 的 `byval`。`-O0` 和 `-O2` 都不发：

```llvm
%byval-temp = alloca %struct.BIG, align 8
call void @llvm.memcpy.p0.p0.i64(ptr align 8 %byval-temp, ptr align 8 %v, i64 48, i1 false)
%call = call i64 @use(ptr ... dereferenceable(48) %byval-temp)
```

clang 用显式 `alloca + memcpy` 实现“按值”语义，IR 参数仍是裸 `ptr`。`byval` 只在手写 IR 或其它前端里出现；后端确实支持（`LowerCall` 的 `ByValArgs` 做本地副本，`RISCVISelLowering.cpp:29332`）。手写 `sret + byval` 时顺序是 `a0` = sret、`a1` = byval 指针（实测生成 `ld a1,0(a1); sd a1,0(a0)`）。
</details>

---

## B.6 实验 4：可变参数

```c
typedef __builtin_va_list va_list;
long vsum(int n, ...) {
  va_list ap; __builtin_va_start(ap, n);
  long s = 0;
  for (int i = 0; i < n; i++) s += __builtin_va_arg(ap, long);
  __builtin_va_end(ap); return s;
}
```

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -o - var.c
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S -emit-llvm -o var.ll var.c
$T/bin/llc -mtriple=riscv64-unknown-elf -mattr=+m,+a,+c,+d \
    -stop-after=finalize-isel -simplify-mir -o - var.ll
```

**实测**（prologue 把 `a1`–`a7` 倒进 save area）：

```asm
vsum:
	addi	sp, sp, -80
	mv	t0, a0
	sd	a5, 56(sp)        # a5–a7 → 56/64/72(sp)
	sd	a6, 64(sp)
	sd	a7, 72(sp)
	addi	a0, sp, 24
	sd	a1, 24(sp)        # a1–a4 → 24/32/40/48(sp)，后三条略
	sd	a0, 8(sp)         # va_list 指向 save area
```

**实测**（`frameInfo` 节选）：

```
frameInfo:
  hasVAStart:      true
machineFunctionInfo:
  varArgsSaveSize: 64
```

`varArgsSaveSize = 64` 是 8 个整型参数寄存器 × 8 字节；`a0`（具名参数 `n`）也留了槽，尽管这里只显式存了 `a1`–`a7`。推导见 Ch 09 §9.7，结论是：**被调用者不知道调用者传了多少个可变参数，只能把整组参数寄存器先倒进内存**。vararg 的 `double` 同样走整型寄存器，调用点用 `li/slli` 拼出 `0x3ff8000000000000` 这类位模式，不经过 `fa*`。

<details>
<summary>解答：vararg 的偶数寄存器规则长什么样</summary>

规则：vararg 中“原始大小 = 2×XLEN 且原始对齐 = 2×XLEN”的参数，必须从**偶数号**参数寄存器开始。实现只有几行（`RISCVCallingConv.cpp:527`）：

```cpp
if (ArgFlags.isVarArg() && ArgFlags.getNonZeroOrigAlign() == TwoXLenInBytes &&
    DL.getTypeAllocSize(OrigTy) == TwoXLenInBytes && ABI != RISCVABI::ABI::ILP32E) {
  unsigned RegIdx = State.getFirstUnallocated(ArgGPRs);
  if (RegIdx != std::size(ArgGPRs) && RegIdx % 2 == 1) State.AllocateReg(ArgGPRs);
}
```

**实测**：`vcallee(1, a, b, (__int128)1)`，`a`/`b` 占 `a1`/`a2`，`__int128` 从 `a4` 开始：

```asm
	li	a0, 1
	li	a4, 1          # a3 被跳过
	li	a5, 0
	tail	vcallee
```

ILP32E 被显式排除是为了兼容 GCC；改这段前先对比两边行为（Ch 09 踩雷第 4 条）。
</details>

---

## B.7 实验 5：链接器 relaxation 前后行为一致

```c
/* relax_leaf.c */
long g __attribute__((section(".sdata"))) = 1;
long leaf(long x) { return x + 100; }

/* relax_main.c：sys_write/sys_exit 见 B.11 */
extern long leaf(long); extern long g; void set_gp(void);
void _start(void) {
  set_gp(); long v = leaf(41) + g;
  sys_write(1, v == 142 ? "sum=142\n" : "BAD\n", v == 142 ? 8 : 4);
  sys_exit(0);
}
```

`set_gp` 的内联汇编（为什么必须 `.option norelax` 见 B.12 第 4 条）：

```c
__asm__(".option push\n.option norelax\n"
        ".globl set_gp\nset_gp:\n la gp, __global_pointer$\n ret\n"
        ".option pop\n");
```

```bash
CC="$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax"
$CC -c -o relax_main.o relax_main.c && $CC -c -o relax_leaf.o relax_leaf.c
$T/bin/ld.lld --relax            -o relax.elf   relax_main.o relax_leaf.o
$T/bin/ld.lld --no-relax         -o norelax.elf relax_main.o relax_leaf.o
$T/bin/ld.lld --relax --relax-gp -o relaxgp.elf relax_main.o relax_leaf.o
for f in norelax.elf relax.elf relaxgp.elf; do printf '%-14s ' $f; /usr/bin/qemu-riscv64-static ./$f; done
```

**实测**（三个可执行文件输出完全一致）：

```
norelax.elf    sum=142
relax.elf      sum=142
relaxgp.elf    sum=142
```

**实测**（`call` 收缩，`--no-relax` 是 8 字节的 `auipc+jalr`）：

```
# --no-relax                          # --relax
111ac: auipc ra, 0x0                  111ac: jal 0x1119e <set_gp>
111b0: jalr -0xe(ra) <set_gp>         111b4: jal 0x111f4 <leaf>
111b8: auipc ra, 0x0 / 111bc: jalr 0x44(ra) <leaf>
```

**实测**：`llvm-readelf -S` 的 `.text` 大小依次是 `0x64`、`0x5c`、`0x58`（relax 省 8 字节，GP relax 再省 4 字节）。

GP relaxation 要三个条件同时成立：`--relax-gp`、`__global_pointer$` 被引用、符号在 `gp ± 2 KiB` 内。本例 `gp = 0x12a00`、`g = 0x12200`，差正好 `-0x800`：

```
# --relax（无 --relax-gp）            # --relax --relax-gp
111b8: lui a1, 0x12                   111b8: ld a1, -0x800(gp)
111bc: ld a1, 0x200(a1)
```

<details>
<summary>解答：relax 前后完整反汇编对照（_start 开头）</summary>

```asm
# ld.lld --no-relax                    # ld.lld --relax
00000000000111a8 <_start>:             00000000000111a8 <_start>:
   111a8: addi  sp, sp, -0x10             111a8: addi  sp, sp, -0x10
   111aa: sd    ra, 0x8(sp)               111aa: sd    ra, 0x8(sp)
   111ac: auipc ra, 0x0                   111ac: jal   0x1119e <set_gp>
   111b0: jalr  -0xe(ra) <set_gp>         111b0: li    a0, 0x29
   111b4: li    a0, 0x29                  111b4: jal   0x111f4 <leaf>
   111b8: auipc ra, 0x0                   111b8: lui   a1, 0x12
   111bc: jalr  0x44(ra) <leaf>
```

三件事：`call` 的 8 字节对变成 4 字节；后面的地址整体前移；GP relaxation 再合并 `lui/ld`。**行为不变**，因为链接器同时改了指令和所有引用地址，并迭代到不动点（Ch 12 §12.6）。
</details>

---

## B.8 实验 6：TLS 真跑

```c
__thread long tls_var = 7;
__thread long tls_two = 9;
void _start(void) {
  tls_var = 7; tls_two = 9;                      /* qemu 6.2 不拷贝 .tdata 初值 */
  long a = tls_var + tls_two;                    /* 第一次取值: 16 */
  tls_var += 35; tls_two += 1;
  long ok = a == 16 && tls_var == 42 && tls_two == 10
            && &tls_two - &tls_var == 1;         /* 相邻 8 字节 */
  sys_write(1, ok ? "tls ok\n" : "tls bad\n", ok ? 7 : 8);
  sys_exit(0);
}
```

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 \
    -ftls-model=local-exec -static -nostdlib -fuse-ld=lld -o tls.elf tls.c
/usr/bin/qemu-riscv64-static ./tls.elf
```

**实测**（local-exec 的访存序列，四个操作数缺一不可）：

```asm
	lui	a0, %tprel_hi(tls_var)
	add	a0, a0, tp, %tprel_add(tls_var)
	addi	a0, a0, %tprel_lo(tls_var)
	ld	a2, 0(a0)          # 等价于读 tls_var
```

**实测**（`tls_var` 的三条重定位；`tls_two` 另有三条同型）：

```
0x2 R_RISCV_TPREL_HI20   tls_var 0x0
0x6 R_RISCV_TPREL_ADD    tls_var 0x0
0xA R_RISCV_TPREL_LO12_I tls_var 0x0
```

`R_RISCV_TPREL_ADD` 表示“这条 `add` 已经把 `tp` 加进来了”，防止多趟 relaxation 重复处理；最终地址 = `tp + st_value`。**实测**（qemu 输出）：

```
tls ok
```

`llvm-readelf -s` 里两个都是 `TLS` 符号，`st_value` 为 0 和 8，所以 `&tls_two - &tls_var == 1`（按 `long` 计）。**qemu 6.2 的 static 路径不拷贝 `.tdata` 初值**：不先写入直接读，`tls_var` 是 0（实测），所以代码显式赋值；TLS 的**寻址**（TPREL、`tp` 相对）正常，**初值镜像未实测**。另外 `tp` 实测为 0、TLS 块映射在 0 起始——绝对地址是 qemu 的分配细节，不是 ABI 契约。

---

## B.9 实验 7（可选）：GP 与 small data 的边界

`sd.c` 只有两行：`long g = 1;` 与 `long read_g(void) { return g; }`。

```bash
for lim in 0 8; do
  $T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -mrelax \
      -msmall-data-limit=$lim -c -o sd_$lim.o sd.c
  $T/bin/llvm-readelf -S sd_$lim.o | grep -E '\.sdata|\.data'
  $T/bin/llvm-readobj -r sd_$lim.o | grep -E 'HI20|LO12|GPREL'
done
```

**实测**（`-msmall-data-limit` 只改**放哪里**，不改指令和重定位）：

```
# limit=0                          # limit=8
  [ 4] .data   PROGBITS 000008       [ 4] .sdata  PROGBITS 000008
    0x0 R_RISCV_HI20   g               0x0 R_RISCV_HI20   g
    0x4 R_RISCV_LO12_I g               0x4 R_RISCV_LO12_I g
```

两个 `.o` 的代码都是 `lui %hi(g)` + `ld %lo(g)`。**GP relaxation 不是编译器发出的重定位**：链接器在 `--relax-gp` 下把 `HI20 + LO12_I` 这一对改写掉；只有 `g` 在 `.sdata`（离 `gp` 够近）时才发生——这就是 B.7 里 `ld -0x800(gp)` 的来源。

“`%gprel` / `R_RISCV_GPREL_I` 是否出现”在本树的答案是：

- `llvm-mc` 不接受 `%gprel`。**实测**：`gp.s:4:11: error: invalid relocation specifier` / `ld a0, %gprel(g)(gp)`。`R_RISCV_GPREL_I`/`_S` 是 **lld 内部伪类型**（`lld/ELF/Arch/RISCV.cpp:73` 的 `256`/`257`），不会出现在 `.o` 的 `.rela.text`，只能在 `relaxAux->relocTypes` 里看到。

---

## B.10 源码落点

| 位置 | 行号 | 看什么 |
|---|---|---|
| `llvm/lib/Target/RISCV/RISCVCallingConv.cpp` | 392 | `CC_RISCV_Impl`：FPR/GPR/栈槽的判定顺序 |
| 同上 | 442–470 / 527–536 | `AllowFPRForF64 = !isVarArg()`；vararg 偶数规则 |
| 同上 | 620–670 | 聚合体走 `CCValAssign::Indirect` |
| `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` | 28701 / 28865–28906 | `LowerFormalArguments`；`VarArgsSaveSize` |
| 同上 | 29013 / 29332 | `LowerCall`；`byval` 的本地副本 |
| 同上 | 28979–28988 | `sret` 必须是第 0 个参数 |
| `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp` | 112–133 | `e_flags`：RVC / RVE / float ABI |
| `lld/ELF/Arch/RISCV.cpp` | 167–193 / 933 / 992 | `calcEFlags`；`relaxCall` / `relaxHi20Lo12` |
| `lld/ELF/Writer.cpp` | 1856 | `__global_pointer$ = .sdata + 0x800` |
| `clang/lib/Driver/ToolChains/Arch/RISCV.cpp` | 222 | `-mabi` 的解析与默认值 |
| `llvm/lib/Target/RISCV/RISCVSubtarget.cpp` | 86–94 | “ignoring target-abi” 的 note |

---

## B.11 可跑实验：无 libc 的 syscall 骨架

下面这份可以直接复制。用 `ecall` 直接调 Linux 的 `write`（`a7=64`）与 `exit`（`a7=93`），不链接 libc：

```c
static long sys_write(long fd, const void *buf, long n) {
  register long a0 __asm__("a0") = fd;
  register long a1 __asm__("a1") = (long)buf;
  register long a2 __asm__("a2") = n;
  register long a7 __asm__("a7") = 64;
  __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
  return a0;                       /* 可能是 short write，见 B.12 第 6 条 */
}
static void sys_exit(long code) {
  register long a0 __asm__("a0") = code;
  register long a7 __asm__("a7") = 93;
  __asm__ volatile("ecall" : : "r"(a0), "r"(a7) : "memory");
  __builtin_unreachable();
}
void _start(void) {
  const char msg[] = "hello riscv\n";
  sys_write(1, msg, sizeof msg - 1);
  sys_exit(0);
}
```

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 \
    -static -nostdlib -fuse-ld=lld -o hello.elf hello.c
/usr/bin/qemu-riscv64-static ./hello.elf; echo "exit=$?"
```

**实测**：

```
hello riscv
exit=0
```

---

## B.12 踩雷集锦

1. **`-mabi`/`-march` 的交叉约束不是硬错误，`e_flags` 也只记录 RVC、float ABI、RVE、TSO。** `-march=rv64imac -mabi=lp64d` 只打 note，代码走 `__adddf3`（参数在 `a0`–`a2`），`e_flags` 仍是 `0x5`；而 `rv64gc` 与 `rv64imac` 在 `lp64` 下都是 `0x1, RVC`，可正常链接。别指望它拦住 `+v`/`+zba` 混用。
2. **`sret` 与 `byval` 同时出现时，`a0` = sret，`a1` = byval 指针。** 手写 IR 实测生成 `ld a1,0(a1); sd a1,0(a0)`。但 clang 对 C 不发 `byval`，而是 `alloca + memcpy`。
3. **`-static` 只保证 local-exec 能解析。** 本例只实测 local-exec；`global-dynamic` 需要 TLS descriptor 与动态加载器（未实测）。把 `local-exec` 抄进共享库，`ld.lld -shared` 会报 `relocation R_RISCV_TPREL_HI20 ... cannot be used with -shared`（实测）。
4. **给 `gp` 赋值必须 `.option norelax`。** 用 `lui gp,%hi(__global_pointer$)` + `addi gp,gp,%lo(...)` 且开 `--relax-gp`，这对指令会被改写成 `mv gp, gp`。**实测**：`1119e: mv gp, gp`，程序输出 `BAD`；加回 `.option norelax` 后恢复 `sum=142`。
5. **qemu-riscv64-static 只跑静态可执行文件；`-nostdlib` 下 `_start` 的栈对齐要自己保证。** 动态 ELF 需要 `-L` 指定 sysroot 与 `ld.so`（未实测）；psABI 要求进入函数时 `sp` 16 字节对齐，qemu 入口满足，但换 loader 或手改 `_start` 后要自己 `andi sp, sp, -16`。
6. **`write` 可能 short write。** 骨架为短小忽略了返回值；真实代码要循环到 `n` 字节写完。同一 `ecall` 在信号、管道、非阻塞 fd 上都可能提前返回。

---

## B.13 本章重点整理

- 一次链接四层契约：前端分类 → IR 属性 → `CC_RISCV` → `.o` 的 `e_flags` → lld → 运行时；聚合体规则三条：≤2×XLEN 拆寄存器、>2×XLEN by reference、返回 >2×XLEN 用 `sret`（`a0`）。
- 浮点参数走 `fa0`–`fa7`，vararg 一律走整型寄存器；vararg 的 save area 是整组参数寄存器的大小（本例 64）。
- `e_flags` 是模块级的，`lp64` 与 `lp64d` 混链会被 `calcEFlags` 拒绝；`-march`/`-mabi` 的内部矛盾不会被检查。
- relaxation 改指令也改地址：`call→jal`、`HI20/LO12→gp`；三个可执行文件输出必须一致。
- GP relaxation 需要 `--relax-gp` + `__global_pointer$` 被引用 + 符号在 `gp ± 2 KiB`；`R_RISCV_GPREL_I` 是 lld 内部类型。local-exec TLS 的地址 = `tp + st_value`，qemu 的 `tp` 与 TLS 映射位置只是运行环境细节。

---

## B.14 自我检核

- [ ] 能画出 B.2 的四层图，并对每层说出一个“错了会怎样”的症状
- [ ] 能解释 `0x1` 与 `0x5` 两个 `e_flags` 的每一位，以及链接器比较哪几位
- [ ] 能说出 `{int,int}`、`{long,long,long}`、`{float,float}` 的落点，并给出汇编证据
- [ ] 能解释为什么 clang 的 RISC-V IR 里看不到 `byval`，而后端仍支持 `byval`
- [ ] 能说出 vararg 偶数规则的触发条件、跳过哪个寄存器、ILP32E 为什么例外
- [ ] 能解释 `--relax` 与 `--relax-gp` 各改写哪类重定位、为什么需要多趟收敛，以及 `.option norelax` 保护什么
- [ ] 能写出 local-exec TLS 的四条指令序列，并说出 `R_RISCV_TPREL_ADD` 的作用

---

## B.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVCallingConv.cpp`**：392–686（`CC_RISCV_Impl`）、167（`getArgGPRs`）。所有“参数落在哪”的答案都在这里，注释里的 psABI 摘要比文档更贴近实现。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**：28701–28920、29013–29528、29539–29640。`CC_RISCV` 只是规则，这三段才是把规则变成 COPY/栈槽的手。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/ELF/Arch/RISCV.cpp`**：73–74（内部 GPREL 类型）、167–193（`calcEFlags`）、933–1142（三类 relaxation）。链接期“改写但不改行为”的全部实现。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/ELF/Writer.cpp`**：1500–1545（relax 循环）、1850–1870（`__global_pointer$`）。relax 嵌在地址分配的不动点循环里，单看 `relaxOnce` 会漏上下文。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/RISCVUsage.md`**：ABI 选项、TLS 模型、GP relaxation 与 small data limit；用户可见的承诺与本章实测逐条对照。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/vararg.ll`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/lld/test/ELF/riscv-relax-*.s`**：CHECK 行里的寄存器、偏移与 `--relax`/`--no-relax` 对照；改 lowering 或 relaxation 时的第一道回归网。

→ 下一章：[Ch 13 — 合法化](./13-legalization.md)
