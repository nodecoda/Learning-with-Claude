# 练习 E — 端到端：clang → .o → ld.lld → qemu

> 目标：把 Ch 24–26 的东西串起来，用本树构建的 clang 生成 LoongArch 可执行文件，用本树的 `ld.lld` 链接，用 `qemu-loongarch64` 跑出正确结果，并用 `llvm-objdump` 证明 relaxation 真的发生了。
>
> 预计时间：60 分钟。所有输出均为本树实测。

---

## E.0 本机有什么

```text
$ loongarch64-unknown-linux-gnu-gcc --version | head -1
loongarch64-unknown-linux-gnu-gcc (GCC) 16.2.0

$ loongarch64-unknown-linux-gnu-gcc -print-sysroot
/opt/loongarch64-cross/bin/../target

$ qemu-loongarch64 --version | head -1
qemu-loongarch64 version 8.2.2
```

本树构建的 clang **没有** `libclang_rt`（Ch 26），所以 crt 和 `libgcc` 都借用交叉 GCC 的 sysroot。

固定变量：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
SYS=/opt/loongarch64-cross/target
G=$SYS/usr/lib64/gcc/loongarch64-unknown-linux-gnu/16.2.0
```

---

## E.1 最小可执行文件

```bash
cat > hello.c <<'EOF'
#include <stdio.h>
int main(void) { puts("hello loongarch"); return 0; }
EOF

$T/bin/clang --target=loongarch64-unknown-linux-gnu \
  --sysroot=$SYS -B$G -L$G hello.c -o hello

$ file hello
hello: ELF 64-bit LSB pie executable, LoongArch, version 1 (SYSV),
       dynamically linked, interpreter /lib64/ld-linux-loongarch-lp64d.so.1

$ qemu-loongarch64 -L $SYS ./hello
hello loongarch
exit=0
```

三个 flag 缺一不可：

| flag | 作用 | 缺了会怎样 |
|---|---|---|
| `--target=loongarch64-unknown-linux-gnu` | 选目标 | 生成 x86 代码 |
| `--sysroot=$SYS` | 找 `Scrt1.o`、`libc.so` | `cannot find Scrt1.o` |
| `-B$G` | 找 `crtbeginS.o`、`libgcc.a` | `cannot find crtbeginS.o` / `-lgcc` |

`-L$G` 是给 `-lgcc` 用的；只给 `-B` 在部分版本上仍会找不到库。

`main` 的反汇编（`llvm-objdump -d hello`）：

```asm
7c0: addi.d  $sp, $sp, -0x20
7c4: st.d    $ra, $sp, 0x18
7c8: st.d    $fp, $sp, 0x10
7dc: pcaddi  $a0, 0x8
7e0: bl      -0x260 <puts@plt>
```

注意 `pcaddi`：PIE 下字符串地址是 PC 相对取的。

---

## E.2 用本树的 `ld.lld` 链接

```bash
$T/bin/clang --target=loongarch64-unknown-linux-gnu \
  --sysroot=$SYS -B$G -L$G -fuse-ld=lld hello.c -o hello_lld

$ qemu-loongarch64 -L $SYS ./hello_lld
hello loongarch
exit=0
```

`-fuse-ld=lld` 让 driver 调 `$T/bin/ld.lld`。这一步验证了 Ch 12 的 lld 在真实链接任务里可用，而不只是跑 lit。

---

## E.3 TLS LE：从 6 条重定位到 1 条指令

这是本课的核心证据链，也是 #239812 的现场。

### 第一步：编译到 `.o`，看汇编

```bash
cat > tls.c <<'EOF'
__thread int tv = 7;
int *addr(void) { return &tv; }
EOF

$T/bin/clang --target=loongarch64-unknown-linux-gnu -O2 \
  -fPIC -ftls-model=local-exec -c tls.c -o tls_le.o

$T/bin/llvm-objdump -d --no-show-raw-insn tls_le.o
0000000000000000 <addr>:
       0: lu12i.w  $a0, 0x0
       4: add.d    $a0, $a0, $tp
       8: addi.d   $a0, $a0, 0x0
       c: ret
```

### 第二步：看重定位

```bash
$T/bin/llvm-readobj -r tls_le.o | grep R_LARCH
    0x0 R_LARCH_TLS_LE_HI20_R tv 0x0
    0x0 R_LARCH_RELAX - 0x0
    0x4 R_LARCH_TLS_LE_ADD_R tv 0x0
    0x4 R_LARCH_RELAX - 0x0
    0x8 R_LARCH_TLS_LE_LO12_R tv 0x0
    0x8 R_LARCH_RELAX - 0x0
```

**6 条重定位，3 对**。每对的第二条是同 offset 的 `R_LARCH_RELAX` —— 这就是 Ch 12/29 里 lld 状态机读的标记。

### 第三步：链接后看最终 ELF

```bash
cat > tls2.c <<'EOF'
#include <stdio.h>
__thread int tv = 7;
__attribute__((noinline)) int *addr(void) { return &tv; }
int main(void) { int *p = addr(); printf("%d\n", *p); return 0; }
EOF

$T/bin/clang --target=loongarch64-unknown-linux-gnu \
  --sysroot=$SYS -B$G -L$G -O2 -ftls-model=local-exec tls2.c -o tls2

$T/bin/llvm-objdump -d --no-show-raw-insn tls2 | sed -n '/<addr>:/,+3p'
0000000000000820 <addr>:
     820: addi.d  $a0, $tp, 0x0
     824: ret

$ qemu-loongarch64 -L $SYS ./tls2
7
exit=0
```

**三条指令被收缩成一条**，且结果正确（`7`）。

### 第四步：验证“坏顺序”的后果

这正是 #239812 的 bug：把 offset 8 的那对重定位顺序换成 marker 在前，lld 只删掉前两条、不改 `addi.d`，程序会读到一个未初始化的 `$a0`。回归测试 `lld/test/ELF/loongarch-tls-le-relax-order.s` 用 `yaml2obj` 精确构造了这个输入（Ch 27/29）。

**检查点**：用 `llvm-readobj -r tls2` 确认最终 ELF 里已经没有任何 `R_LARCH` 重定位（全部解析完）。

---

## E.4 LSX：向量代码真的能跑

```bash
cat > vec.c <<'EOF'
#include <stdio.h>
typedef int v4i32 __attribute__((vector_size(16)));
__attribute__((noinline)) v4i32 addv(v4i32 a, v4i32 b) { return a + b; }
int main(void) {
  v4i32 a = {1,2,3,4}, b = {10,20,30,40};
  v4i32 r = addv(a,b);
  printf("%d %d %d %d\n", r[0], r[1], r[2], r[3]);
  return 0;
}
EOF

$T/bin/clang --target=loongarch64-unknown-linux-gnu \
  --sysroot=$SYS -B$G -L$G -march=la464 -O2 vec.c -o vec

$ qemu-loongarch64 -L $SYS ./vec
11 22 33 44
exit=0
```

`addv` 的汇编里会出现 LSX（`-march=la464` 带 `+lsx +lasx`）：

```asm
vinsgr2vr.d  $vr0, $a0, 0
vinsgr2vr.d  $vr0, $a1, 1
vadd.w       $vr0, $vr0, $vr1
vpickve2gr.d $a0, $vr0, 0
vpickve2gr.d $a1, $vr0, 1
```

**检查点**：换成 `-march=loongarch64`（无 LSX），确认 `vadd.w` 消失、变成标量 `add.w` 序列。

---

## E.5 LA32 / LA64 的 i64 差异

```bash
cat > i64.c <<'EOF'
long long add64(long long a, long long b) { return a + b; }
EOF

$T/bin/clang --target=loongarch32-unknown-linux-gnu -O2 -S i64.c -o - | grep -E 'add\.w|sltu|add\.d'
$T/bin/clang --target=loongarch64-unknown-linux-gnu -O2 -S i64.c -o - | grep -E 'add\.w|sltu|add\.d'
```

LA32 需要 `add.w` + `sltu` 进位链；LA64 一条 `add.d`。这与 Ch 13 的 legalization 和 Ch 27 的自动生成断言完全对应。

---

## E.6 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `cannot find Scrt1.o` | 没给 `--sysroot` | 加 `--sysroot=$SYS` |
| `cannot find crtbeginS.o` | 没给 `-B$G` | 加 `-B$G`（注意目录是 `usr/lib64/gcc/...`） |
| `cannot find -lgcc` | 没给 `-L$G` | 加 `-L$G` |
| `undefined reference to main` | 输入文件里没有 `main` | 用可执行程序而不是库函数 |
| qemu 报 `Could not open '/lib64/ld-linux-loongarch-lp64d.so.1'` | 没给 `-L $SYS` | `qemu-loongarch64 -L $SYS ./prog` |
| `-march=native` 报 unknown CPU | 主机是 x86，Ch 24 | 用 `-march=la464`/`la664` |

---

## E.7 验收标准

- [ ] `hello` 能打印 `hello loongarch` 且 `exit=0`
- [ ] `hello_lld` 用本树 `ld.lld` 链接成功
- [ ] `tls_le.o` 里能看到 3 对 `R_LARCH_RELAX`
- [ ] `tls2` 的 `addr` 反汇编只有 `addi.d $a0, $tp, 0x0; ret`
- [ ] `vec` 在 qemu 下输出 `11 22 33 44`
- [ ] 能解释 LA32/LA64 的 i64 加法差异

---

## E.8 延伸

1. 用 `-mcmodel=large` 重新编译 `tls2`，观察 `addr` 从 3 条变成 5 条（Ch 11）。
2. 给 `tls2` 加 `-Wl,--no-relax`（如果 lld 支持）或 `-mno-relax`，看最终 ELF 是否保留三条指令。
3. 用 `llvm-readobj -S tls2` 找 `.tdata`/`.tbss`，确认 TLS 段布局。
4. 用 `qemu-loongarch64 -strace ./tls2` 看 syscall（若 qemu 版本支持）。
5. 把 `vec.c` 的 `v4i32` 改成 `v8i32`（需要 LASX），对比 `-march=la464` 与 `-march=la664` 的指令数。

---

## E.9 收尾

到这里，你已经有一条完整的证据链：

```text
C 源码
  → clang（driver/builtin/ABI，Ch 24/25）
  → IR（ABI 属性、intrinsic，Ch 09/15）
  → .o（指令选择/MC/重定位，Ch 04–08/13–22）
  → 最终 ELF（lld relaxation，Ch 12/29）
  → qemu 运行结果（exit=0）
```

**每一段都能用命令复现**——这就是参与维护的底气。
