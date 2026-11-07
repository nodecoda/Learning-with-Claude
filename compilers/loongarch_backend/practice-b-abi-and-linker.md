# 练习 B — ABI 与链接审计：从签名到 `.rela.text` 到最终 ELF

> 目标：用同一套工具链，把“参数怎么传 / TLS 怎么访问 / 链接器怎么收缩”三件事连成一条可验证的链。做完你应该能对着一个 `.o` 说出它的 ABI 假设，并预测链接后的指令。

---

## B.1 六种签名：谁进哪个寄存器

把下面五个函数（第六个 varargs 见 Ch 09）编译成汇编：

```llvm
define i64 @s1(i64 %a, i64 %b) { %r = add i64 %a, %b
  ret i64 %r }
define double @s2(double %a, double %b) { %r = fadd double %a, %b
  ret double %r }
define {i64, i64} @s3(i64 %a) { ... }
define void @s4(ptr sret({i64,i64,i64}) %p) { ret void }
define i128 @s6(i128 %a) { ret i128 %a }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2 sig.ll -o -
```

实测：

```text
s1:
	add.d	$a0, $a0, $a1
	ret
s2:
	fadd.d	$fa0, $fa0, $fa1
	ret
s3:
	move	$a1, $a0
	ret
s4:
	ret
s6:
	ret
```

审计表：

| 签名 | 实参位置 | 返回值位置 | 结论 |
|---|---|---|---|
| `i64(i64,i64)` | `$a0,$a1` | `$a0` | 整数寄存器对 |
| `double(double,double)` | `$fa0,$fa1` | `$fa0` | LP64D 走 FPR |
| `{i64,i64}(i64)` | `$a0` | `$a0,$a1` | 两个 GPR 返回 |
| `sret` | 指针在 `$a0` | — | 隐藏参数 0 |
| `i128(i128)` | `$a0,$a1` | `$a0,$a1` | 2×GRLen 拆分 |

**`s6` 为什么是空函数**：`i128` 参数原样返回，寄存器对已经在 `$a0/$a1`，一条指令都不用生成。这是“ABI 恰好对齐”的结果，不是优化。

**审计方法**：不要只读 ABI 文档；用 `llc -stop-after=loongarch-isel` 看 `COPY_FROM_REG`，或直接看汇编里哪些寄存器没被初始化就被使用。

---

## B.2 TLS 三种模型的重定位

```llvm
@t = thread_local(localexec) global i64 0
@t2 = thread_local(initialexec) global i64 0
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+relax -filetype=obj tls.ll -o tls.o
$ llvm-readobj -r tls.o
```

实测（`-mattr=+relax`）：

```text
Section (3) .rela.text {
  0x0  R_LARCH_TLS_LE_HI20_R t
  0x0  R_LARCH_RELAX -
  0x4  R_LARCH_TLS_LE_ADD_R  t
  0x4  R_LARCH_RELAX -
  0x8  R_LARCH_TLS_LE_LO12_R t
  0x8  R_LARCH_RELAX -
  0x10 R_LARCH_TLS_IE_PC_HI20 t2
  0x10 R_LARCH_RELAX -
  0x14 R_LARCH_TLS_IE_PC_LO12 t2
  0x14 R_LARCH_RELAX -
}
```

不带 `+relax` 时，`R_LARCH_RELAX` 全部消失——**这是最容易误判的一条**（Ch 12 有源码解释）。

审计要点：

1. **LE 有三条**，每条都带标记，且 offset 是 0/4/8。
2. **IE 有两条**，offset 0x10/0x14（前面 LE 占了 12 字节，对齐到 16）。
3. **重定位类型带 `_R`** 才可能 relax；IE 的 `PC_HI20`/`PC_LO12` 没有 `_R`，但同样带 `R_LARCH_RELAX`——因为 IE→LE 的放松是另一套逻辑（`tlsIeToLe`）。
4. **`llvm-readobj -r` 的顺序就是 `stable_sort` 后的顺序**；同 offset 的先后取决于输入。

---

## B.3 端到端：从汇编到 qemu 退出码

写一个最小静态程序，只计算 TLS 地址、不 dereference。实测：带 `ld.d` dereference 的版本在 `qemu-loongarch64` 下 SIGSEGV——裸 `_start` 没有 TLS runtime 初始化，`$tp` 不可用（这是推断，现象是实测的）：

```asm
    .text
    .globl _start
_start:
    lu12i.w $a0, %le_hi20_r(tvar)
    add.d   $a0, $a0, $tp, %le_add_r(tvar)
    addi.d  $a0, $a0, %le_lo12_r(tvar)
    li.d    $a0, 0
    li.d    $a7, 94
    syscall 0
    .section .tbss,"awT",@nobits
    .globl tvar
    .p2align 3
tvar:
    .dword 0
```

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
clang --target=loongarch64-unknown-linux-gnu -c pB.s -o pB.o
llvm-readobj -r pB.o          # 看到 3 条 LE + 3 条 RELAX
ld.lld -o pB pB.o
llvm-objdump -d pB | sed -n '/<_start>/,+5p'
qemu-loongarch64 ./pB; echo $?
```

实测（默认 relax）：

```text
0000000000020158 <_start>:
   20158: 44 00 c0 02  	addi.d	$a0, $tp, 0
   2015c: 04 00 80 03  	ori	$a0, $zero, 0x0
   20160: 0b 78 81 03  	ori	$a7, $zero, 0x5e
   20164: 00 00 2b 00  	syscall	0x0
exit=0
```

三条 TLS 指令被收缩成**一条** `addi.d $a0, $tp, 0`。

对照 `-mno-relax`：

```text
   20158: 04 00 00 14  	lu12i.w	$a0, 0x0
   2015c: 84 88 10 00  	add.d	$a0, $a0, $tp
   20160: 84 00 c0 02  	addi.d	$a0, $a0, 0x0
```

**同一个 `.s`，只改汇编选项，链接结果差 2 条指令。** 这就是为什么“测试通过”不等于“relaxation 被覆盖”：你的测试可能根本没开 `+relax`。

---

## B.4 hint-first 的构造

GNU as / clang 只会产生 canonical 顺序。要验证 #239812，必须手工构造：

- 用 `yaml2obj`（Ch 12 的测试就是这么做）；
- 或者用 `llvm-objcopy --update-section` 改写 `.rela.text` 的条目顺序（不推荐，容易错）。

最小做法：复制 `lld/test/ELF/loongarch-tls-le-relax-order.s`，把 `hint_first` section 里 offset 8 的两条重定位对调，然后：

```bash
llvm-lit -sv lld/test/ELF/loongarch-tls-le-relax-order.s
```

如果你改的是**输入顺序但期望不变**，测试应该仍然通过——这正是修复的含义。

---

## B.5 审计清单

拿到一个 LoongArch `.o`，按顺序问：

1. **它用什么 ABI？** 看 `EF_LOONGARCH_ABI_*` flags 和 `.o` 的 `e_flags`。
2. **浮点参数走 FPR 还是 GPR？** `lp64s` 走 GPR；`lp64d` 走 FPR；varargs 一律 GPR。
3. **返回值超过两个 GPR 吗？** 超过就必须 `sret`。
4. **TLS 是哪种模型？** 看重定位类型：`TLS_LE_*` / `TLS_IE_*` / `TLS_GD_*` / `TLS_DESC_*`。
5. **带 `R_LARCH_RELAX` 吗？** 不带的话，链接器不会收缩；先确认编译时有没有 `+relax`。
6. **同 offset 的重定位顺序是什么？** 这是 #239812 类 bug 的入口。
7. **链接后指令数变了吗？** 用 `llvm-objdump -d` 对比 `.o` 和最终 ELF。

---

## B.6 常见失败模式

| 现象 | 原因 |
|---|---|
| `llvm-readobj -r` 看不到 `R_LARCH_RELAX` | 没开 `+relax`（llc 默认关） |
| 链接后没收缩 | 同上，或 `ld.lld --no-relax` |
| `qemu` SIGSEGV | 裸 `_start` 里 `$tp` 未初始化，dereference TLS 变量 |
| `li.d` 报 “requires LA64” | 用了 `loongarch32` triple |
| `sret` 函数参数错位 | 忘了 `sret` 指针是参数 0 |
| hint-first 测试无法用 `.s` 复现 | 汇编器只产生 canonical 顺序，必须 `yaml2obj` |

---

## B.7 证据表

| 结论 | 命令/源码 |
|---|---|
| 六种签名 | B.1 的 `llc` 实测 |
| TLS 重定位 | B.2 的 `llvm-readobj -r` 实测 |
| relax 开关 | `LoongArchMCCodeEmitter.cpp:220`；B.2 对照 |
| 端到端收缩 | B.3 的 `ld.lld` + `llvm-objdump` + `qemu` 实测 |
| hint-first 测试 | `lld/test/ELF/loongarch-tls-le-relax-order.s` |

---

## B.8 下一步

Part 3 进入 SelectionDAG：先看合法化（Ch 13），再看选指（Ch 14）、自定义 lowering（Ch 15）、DAG combine（Ch 16），最后是 LSX/LASX 向量 lowering（Ch 17）。
