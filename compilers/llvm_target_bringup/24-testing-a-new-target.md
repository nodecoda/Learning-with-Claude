# Ch 24 — 测试一个新 target

> **目标**：为一个新 target 建立可维护的测试金字塔：MC 编码、CodeGen 汇编、MIR pass 级、lit driver、端到端差分与代码尺寸基线；能读懂 `RUN:`/`FileCheck` 约定，能判断什么时候该手写 CHECK、什么时候该用 `update_*_test_checks.py`。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

“能编译”不是正确性证明。一个新 target 的 bug 会以完全不同的粒度出现：

- MC 层：编码错一个 bit，汇编器和反汇编器互相“自洽”，但真实硬件不认。
- CodeGen 层：指令选对了，寄存器分配或栈帧布局错了，函数返回时才炸。
- pass 层：branch relaxation、PEI（Prologue/Epilogue Insertion）、regalloc 的某个不变量被破坏，汇编看不出来，运行才错。
- 端到端：前面全对，链接/重定位/ABI 错，程序在 qemu 里返回错误退出码。

如果只有端到端测试，任何一个失败都要从 IR 一路查到机器码；如果只有单元测试，你又不知道这些指令拼起来能不能跑。测试金字塔的作用就是：**把失败尽量定位在最小的一层，同时保留少量高成本的端到端测试兜底。**

MiniRV 目前的规模是 53 个 lit 测试（49 个 CodeGen `.ll` + 4 个 MC `.s`），其中 12 个是 `-error` 负向测试。对比 RISC-V 的 980 个顶层 CodeGen 条目、2247 个递归 `.ll`、498 个 MC 条目，53 只是一个 bring-up 的地板，不是成熟 target 的终点。这一章要建立的不是“再多写几个测试”，而是“每一层测什么、用什么工具、失败了说明什么”。

## 先建立直觉

测试金字塔从下到上，越往下越快、越具体、越便宜；越往上越慢、越真实、越难定位：

```
                         /\
                        /  \        e2e differential
                       /    \       17 programs, qemu exit code
                      /------\      (最真实，最难定位)
                     /        \
                    / code-size \   static instruction counts
                   /   baseline   \  5 kernels
                  /--------------\
                 /   lit driver   \  llvm-lit: discovery + RUN lines
                /    53 tests      \ (把下面各层组织起来)
               /------------------\
              /   MIR pass-level    \  llc -run-pass / -stop-after
             /     MiniRV: 0 .mir    \ (当前缺口)
            /------------------------\
           /    CodeGen asm FileCheck \  37 positive .ll
          /      instruction selection   \ ABI / frame / reloc
         /--------------------------------\
        /        MC encoding / parsing      \  4 .s
       /   llvm-mc -show-encoding / objdump  \ (最便宜)
      /--------------------------------------\
```

每一层回答不同的问题：

| 层 | 问题 | 工具 | MiniRV 现状 | 失败定位 |
|---|---|---|---|---|
| MC 单元 | 这条汇编的字节对吗？能反汇编回来吗？ | `llvm-mc`、`llvm-objdump` | 4 个 `.s` | 编码器/解析器/打印机 |
| CodeGen 汇编 | 这段 IR 选出的指令/顺序对吗？ | `llc` + `FileCheck` | 49 个 `.ll`（37 正向 + 12 负向） | ISel/合法化/ABI/帧 |
| MIR pass 级 | 某个 machine pass 前后的不变量对吗？ | `llc -run-pass` / `-stop-after` | **0 个 `.mir`** | regalloc/PEI/branch relaxation |
| lit driver | 测试被发现、配置、并行执行对吗？ | `llvm-lit` | 53 个测试 | 测试基础设施 |
| e2e 差分 | 整个编译器 + 链接 + 运行时语义对吗？ | `llc` + `ld.lld` + qemu | 17 个程序 | ABI/重定位/运行时 |
| 代码尺寸 | 代码生成是否出现尺寸回归？ | 静态指令计数 | 5 个 kernel | 代码生成策略 |

注意 MIR 那一行是 0。这不是“MIR 不重要”，而是 MiniRV 还没走到需要大量调 pass 的阶段。只要开始改 branch relaxation、PEI、regalloc，MIR 测试就应该补上；否则一个 pass 级 bug 只能靠汇编或 e2e 间接暴露。

## 主线：MiniRV 的六层测试

### 1. MC 单元测试：`llvm/test/MC/MiniRV/basic.s`

MC 测试是最便宜的一层。它不跑指令选择，只验证“汇编文本 ↔ 字节”的往返。MiniRV 的 `basic.s` 开头是三条 `RUN:`：

```asm
# RUN: llvm-mc -triple=minirv64 -show-encoding %s | FileCheck %s
# RUN: llvm-mc -triple=minirv64 -filetype=obj %s -o %t.o
# RUN: llvm-objdump -d --triple=minirv64 %t.o | FileCheck --check-prefix=DIS %s
```

三条 `RUN:` 分别验证：

1. **编码**：`llvm-mc -show-encoding` 输出每条指令的字节，`FileCheck` 用默认 `CHECK` 前缀匹配。
2. **对象生成**：`-filetype=obj` 生成 `%t.o`（`%t` 是 lit 分配的临时路径）。
3. **反汇编**：`llvm-objdump -d` 把对象反汇编回来，用 `DIS` 前缀匹配，确认编码器和打印机没有各说各话。

对应的 CHECK 是手写的，而且用了 `CHECK-SAME` 把助记符和 encoding 绑在同一行：

```asm
# CHECK: add x1, x2, x3
# CHECK-SAME: encoding: [0xb3,0x00,0x31,0x00]
add x1, x2, x3
```

实测：

```console
$ $B/llvm-mc -triple=minirv64 -show-encoding llvm/test/MC/MiniRV/basic.s
	add	x1, x2, x3                      # encoding: [0xb3,0x00,0x31,0x00]

	sub	x5, x6, x7                      # encoding: [0xb3,0x02,0x73,0x40]

	sra	x10, x11, x12                   # encoding: [0x33,0xd5,0xc5,0x40]

	addi	x1, x2, -1                      # encoding: [0x93,0x00,0xf1,0xff]

	xori	x28, x29, 2047                  # encoding: [0x13,0xce,0xfe,0x7f]

	lui	x1, 74565                       # encoding: [0xb7,0x50,0x34,0x12]

	auipc	x2, 1048575                     # encoding: [0x17,0xf1,0xff,0xff]
```

再走一遍对象路径：

```console
$ $B/llvm-mc -triple=minirv64 -filetype=obj llvm/test/MC/MiniRV/basic.s -o /tmp/basic.o
$ $B/llvm-objdump -d --triple=minirv64 /tmp/basic.o
/tmp/basic.o:	file format elf64-littleriscv

Disassembly of section .text:

0000000000000000 <.text>:
       0: b3 00 31 00  	add	x1, x2, x3
       4: b3 02 73 40  	sub	x5, x6, x7
       8: 33 d5 c5 40  	sra	x10, x11, x12
       c: 93 00 f1 ff  	addi	x1, x2, -1
      10: 13 ce fe 7f  	xori	x28, x29, 2047
      14: b7 50 34 12  	lui	x1, 74565
      18: 17 f1 ff ff  	auipc	x2, 1048575
```

MC 测试的边界也要清楚：它只证明“这条指令的编码/解析正确”，不证明“指令选择会选它”，更不证明“选出来能跑”。后者是 CodeGen 和 e2e 的事。

### 2. CodeGen 汇编测试：`llvm/test/CodeGen/MiniRV/alu.ll`

`alu.ll` 是最典型的 CodeGen 测试。开头是两条 `RUN:`，第二条跨行：

```llvm
; RUN: llc -mtriple=minirv64 < %s | FileCheck %s
; RUN: llc -mtriple=minirv64 -filetype=obj < %s \
; RUN:   | llvm-objdump -d --triple=minirv64 - \
; RUN:   | FileCheck %s --check-prefix=OBJ
```

第一条验证汇编文本；第二条把同一个文件编译成对象，再反汇编，用 `OBJ` 前缀验证“汇编文本和对象里的指令一致”。这能抓住“AsmPrinter 打印对了，但 MC 编码错了”的情况。

每条指令的 CHECK 都很短：

```llvm
; CHECK-LABEL: add:
; CHECK:         add     x10, x10, x11
define i64 @add(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
```

这里有几个约定：

- **`CHECK-LABEL: add:`** 把匹配范围锚定到 `add` 这个函数标签。没有它，后面的 `CHECK: add x10, x10, x11` 可能匹配到别的函数，测试会“假通过”。
- **`CHECK:`** 只要求“在当前位置之后出现”，不要求紧邻；适合不关心中间指令的场景。
- **`CHECK-NEXT:`** 要求紧邻下一行；`mixed:` 里用它锁住三条指令的相对顺序。
- **`CHECK-NOT:`** 在两段匹配之间断言某模式不出现；写“不能出现某条指令”时很有用。
- **`--check-prefix=OBJ`** 让同一个文件支持多套断言；`--check-prefixes=A,B` 可以一次启用多套。

实测 `llc` 的输出（节选）：

```console
$ $B/llc -mtriple=minirv64 < llvm/test/CodeGen/MiniRV/alu.ll
	.file	"<stdin>"
	.text
	.globl	add                             # -- Begin function add
	.type	add,@function
add:                                    # @add
	.cfi_startproc
# %bb.0:
	add	x10, x10, x11
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	add, .Lfunc_end0-add
	.cfi_endproc
```

注意 `alu.ll` 没有写 `-O2`，用的是 `llc` 的默认优化级别；对这个文件，实测默认和 `-O2` 的输出没有差别。但但我们仍然建议新测试显式写 `-O2` 或 `-O0`。默认值可能随 LLVM 版本变化，而且“这个测试到底想锁哪个优化级别”应该一眼可见。

### 3. MIR pass 级测试：MiniRV 的缺口

MIR 测试直接喂 `MachineFunction`，跳过 IR 和指令选择，专门测某个 machine pass。MiniRV 目前一个 `.mir` 都没有：

```console
$ find llvm/test/CodeGen/MiniRV -name '*.mir' | wc -l
0
$ find llvm/test/CodeGen/RISCV -name '*.mir' | wc -l
498
```

这不是说 MiniRV 不需要 MIR，而是它的 pass 还没复杂到必须用 MIR 锁。只要开始动 branch relaxation、PEI、regalloc、tail duplication，MIR 就会变成最有效的定位工具。

MIR 从哪来？用 `-stop-after` 把某个 pass 之后的 MachineFunction 打出来：

```console
$ $B/llc -mtriple=minirv64 -stop-after=finalize-isel < llvm/test/CodeGen/MiniRV/alu.ll
...
name:            add
alignment:       1
...
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11

    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %2:gpr = ADD %0, %1
    $x10 = COPY %2
    PseudoRET implicit $x10
```

这段 MIR 的 `isSSA: true`，直接丢给 PEI 会触发 `hasNoVRegs()` 断言。MIR 测试的输入必须和 pass 的前置条件匹配，这本身就是一条重要规则。

最小的解析测试只需要 `-run-pass=none`：把 `-stop-after=finalize-isel` 的输出存成 `.mir`，加一行 `# RUN: llc -mtriple=minirv64 -run-pass=none %s -o -`，就能验证 MIR 本身能被读入。

要测 PEI，先取 `virt-reg-rewriter` 之后的 MIR（此时 `noVRegs: true`），再跑 `-run-pass=prolog-epilog`：

```console
$ $B/llc -mtriple=minirv64 -stop-after=virt-reg-rewriter \
    < llvm/test/CodeGen/MiniRV/alu.ll > /tmp/alu-vrr.mir
$ $B/llc -mtriple=minirv64 -run-pass=prolog-epilog /tmp/alu-vrr.mir -o /dev/null
$ echo $?
0
```

反过来，拿 `finalize-isel` 之后的 IsSSA MIR 直接跑 PEI，会得到 `Assertion 'MF.getProperties().hasNoVRegs()' failed`。这不是 MiniRV 的 bug，是测试输入选错了阶段。`-simplify-mir` 会去掉可选字段，让 diff 更稳定。参考实现可以看 `llvm/test/CodeGen/RISCV/verify-instr.mir` 和 `llvm/test/CodeGen/MIR/RISCV/machine-function-info.mir`。

为什么值得补 MIR：汇编测试只能看到最终指令，regalloc 选错寄存器、PEI 多调整一次 SP、branch relaxation 少插一条跳转，都可能被后面的 pass 掩盖。MIR 测试把这些 pass 的不变量单独钉住，失败时直接指向 pass，而不是“某段汇编不对”。

### 4. lit driver：把测试组织起来

`llvm-lit` 是 driver，它负责发现测试、展开 `RUN:`、并行执行、汇总结果。单文件跑：

```console
$ $B/llvm-lit -v llvm/test/CodeGen/MiniRV/alu.ll
-- Testing: 1 tests, 1 workers --
PASS: LLVM :: CodeGen/MiniRV/alu.ll (1 of 1)

Testing Time: 0.07s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

全量跑 MiniRV 的 53 个测试：

```console
$ $B/llvm-lit llvm/test/CodeGen/MiniRV llvm/test/MC/MiniRV
PASS: LLVM :: CodeGen/MiniRV/global-offset.ll (49 of 53)
PASS: LLVM :: CodeGen/MiniRV/i128.ll (50 of 53)
PASS: LLVM :: CodeGen/MiniRV/i128-div.ll (51 of 53)
PASS: LLVM :: CodeGen/MiniRV/soft-float.ll (52 of 53)
PASS: LLVM :: CodeGen/MiniRV/cfi.ll (53 of 53)

Testing Time: 0.19s

Total Discovered Tests: 53
  Passed: 53 (100.00%)
```

`-v` 会打印每个测试的 PASS/FAIL 和失败时的完整命令；排查 CI 时先看 FAIL 行下面的 `Command Output`，里面通常直接有 `llc` 的 stderr 和 FileCheck 的 diff。

### 5. 端到端差分：`dev/e2e/selftest.ll`

`selftest.ll` 没有 `RUN:`，因为它不是 lit 测试，而是 e2e harness 的输入。它定义了一个 `main`，返回一个小整数：

```llvm
target triple = "minirv64"

define signext i32 @mix(i32 signext %a, i32 signext %b) { ... }
define signext i32 @divs(i32 signext %a, i32 signext %b) { ... }
define signext i32 @sum9(...) { ... }
define signext i32 @sumsq(i32 signext %n) { ... }

define i32 @main() {
  %a = call signext i32 @mix(i32 7, i32 5)
  %b = call signext i32 @divs(i32 -100, i32 7)
  %c = call signext i32 @sum9(i32 1, i32 2, i32 3, i32 4, i32 5, i32 6, i32 7,
                              i32 8, i32 9)
  %d = call signext i32 @sumsq(i32 10)
  %s1 = add i32 %a, %b
  %s2 = add i32 %s1, %c
  %s3 = add i32 %s2, %d
  %r = and i32 %s3, 255
  ret i32 %r
}
```

`dev/e2e/run.sh` 对每个 `.ll` 做同样的事：分别用 `minirv64` 和 `riscv64 -mattr=+m` 编译成对象，链接 `start.s`，在 `qemu-riscv64-static` 里运行，比较退出码。实测：

```console
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
    QEMU=/usr/bin/qemu-riscv64-static \
    llvm/lib/Target/MiniRV/dev/e2e/run.sh
artifacts: /tmp/tmp.ojCk3lORs0
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

`selftest` 的退出码 66 不是随便写的：它是 `main` 里那串运算的结果，RISC-V 参考后端也返回 66。差分测试的 oracle 就是“参考后端 + 真实执行”，这比任何汇编 CHECK 都强，但定位能力最弱：失败时你只知道“两边不一样”，不知道是哪条指令。

这里有个真实插曲：第一次跑这个 harness 时，`t1` 在 RISC-V 参考侧 qemu 段错误（`riscv64=139`），MiniRV 侧是 20；重跑后 17/17 全过。差分测试的失败要先分辨是编译器 bug、测试程序 bug，还是 qemu/环境抖动。不要看到一次 FAIL 就改代码，也不要看到一次 PASS 就认为稳定。

### 6. 代码尺寸基线：`dev/bench/run.sh`

`bench/run.sh` 不做正确性，它只数静态指令条数：

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

这个 ratio 是“静态指令条数比”，不是运行时性能。`reduction` 的 0.83 说明 MiniRV 生成的指令比 RISC-V 少，可能是因为参考后端多了几条防御性指令，也可能是因为 MiniRV 少做了什么；不要拿它当性能结论。它的价值是**回归基线**：某个改动让 ratio 从 1.00 跳到 1.40，你就知道代码生成策略变了，应该去看 diff。

## 源码落点

| 层 | 文件 | 关键内容 | 看什么 |
|---|---|---|---|
| MC | `llvm/test/MC/MiniRV/basic.s` | 3 条 `RUN:`、`CHECK`/`CHECK-SAME`、`DIS` | `-show-encoding` 与 `objdump` 双路径 |
| MC | `llvm/test/MC/MiniRV/invalid-operands.s` | `not llvm-mc` | 负向解析测试 |
| CodeGen | `llvm/test/CodeGen/MiniRV/alu.ll` | `RUN:`、`CHECK-LABEL`/`CHECK`/`CHECK-NEXT`、`OBJ` | ISel 与 MC 的一致性 |
| CodeGen | `llvm/test/CodeGen/MiniRV/*-error.ll` | `not --crash` + `CHECK:` | 诊断契约 |
| e2e | `llvm/lib/Target/MiniRV/dev/e2e/selftest.ll` | `main` 返回小整数 | 差分 oracle 的输入 |
| e2e | `llvm/lib/Target/MiniRV/dev/e2e/run.sh` | 双编译、链接、qemu、比较退出码 | 17 个程序的 harness |
| 尺寸 | `llvm/lib/Target/MiniRV/dev/bench/run.sh` | `grep -cE '^\t[a-z]'` | 静态指令计数 |
| 总入口 | `llvm/lib/Target/MiniRV/dev/minirv-smoke.sh` | auto-td、lit、e2e、size | 一条命令跑全套 |

## 实测：测试基础设施本身也要验证

### `-verify-machineinstrs`

改 machine pass 时，先让机器指令验证器过一遍：

```console
$ $B/llc -mtriple=minirv64 -verify-machineinstrs \
    < llvm/test/CodeGen/MiniRV/alu.ll -o /dev/null
$ echo $?
0
```

它会在每个 machine pass 后检查寄存器/操作数/CFG 的不变量。很多“汇编看起来对，但 MIR 已经坏了”的 bug 会在这里提前暴露。建议所有涉及 machine pass 的测试都加 `-verify-machineinstrs`，成本很低。

### 对比 RISC-V 的测试规模

```console
$ ls llvm/test/CodeGen/RISCV | wc -l
980
$ ls llvm/test/CodeGen/RISCV/*.ll | wc -l
873
$ find llvm/test/CodeGen/RISCV -name '*.ll' | wc -l
2247
$ ls llvm/test/MC/RISCV | wc -l
498
```

MiniRV 的对应数字是：

```console
$ ls llvm/test/CodeGen/MiniRV/*.ll | wc -l
49
$ ls llvm/test/MC/MiniRV/*.s | wc -l
4
```

差距不只是数量。成熟 target 多出来的测试通常覆盖：

- **扩展矩阵**：RV32/RV64、M/A/F/D/C/V、Z* 扩展的组合。
- **ABI 变体**：ilp32/lp64、hard/soft float、RVV ABI、varargs、sret、byval。
- **代码模型/重定位**：medlow/medany/large、GOT/PLT、TLS model、relaxation。
- **pass 级不变量**：regalloc、PEI、branch relaxation、machine outliner、scheduling。
- **调试/异常**：DWARF、CFI、unwind、EH frame。
- **负向测试**：非法操作数、非法寄存器、诊断消息。

53 个测试能证明“MiniRV 的核心路径是活的”，不能证明“MiniRV 是一个成熟后端”。这不是贬低 bring-up，而是提醒你：测试要按风险排序，不要被数字麻痹。

## `update_llc_test_checks.py` / `update_mc_test_checks.py`

两个脚本都在 `llvm/utils/` 下：

- `llvm/utils/update_llc_test_checks.py`：根据 `RUN:` 里的 `llc` 命令重新生成汇编 CHECK。
- `llvm/utils/update_mc_test_checks.py`：根据 `RUN:` 里的 `llvm-mc` 命令重新生成 MC CHECK。

常用选项：`--llc-binary` 指定 `llc`（新 target 必须指定）、`--function` 只更新一个函数、`-u` 只更新已 autogenerated 的测试、`--force-update` 强制更新。

但对 MiniRV，这个脚本现在**不能直接用**。拿 `alu.ll` 的副本跑：

```console
$ cp llvm/test/CodeGen/MiniRV/alu.ll /tmp/alu.ll
$ python3 llvm/utils/update_llc_test_checks.py \
    --llc-binary /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc /tmp/alu.ll
...
  File ".../UpdateTestChecks/asm.py", line 630, in get_run_handler
    raise KeyError("Triple %r is not supported" % (triple))
KeyError: "Triple 'minirv64' is not supported"
```

原因在 `llvm/utils/UpdateTestChecks/asm.py::get_run_handler`：它维护了一张 triple → scrubber 的表，里面有 `riscv32`/`riscv64`，没有 `minirv64`。要支持 MiniRV，需要在这张表里加一项，例如复用 RISC-V 的 scrubber：

```python
"minirv64": (scrub_asm_riscv, ASM_FUNCTION_RISCV_RE),
```

或者为 MiniRV 写一个专门的 scrubber（处理 `x0`、`la`、`PseudoLA` 等）。这本身就是一条 bring-up TODO：**测试工具链也要跟着 target 一起长。**

`update_mc_test_checks.py` 能跑，但它的行为和手写测试差别很大。拿 `basic.s` 的副本跑：

```console
$ cp llvm/test/MC/MiniRV/basic.s /tmp/basic.s
$ python3 llvm/utils/update_mc_test_checks.py \
    --llvm-mc-binary /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-mc /tmp/basic.s
WARNING: Skipping unparsable RUN line: llvm-mc -triple=minirv64 -filetype=obj %s -o %t.o
WARNING: Skipping non-mc RUN line: llvm-objdump -d --triple=minirv64 %t.o | FileCheck --check-prefix=DIS %s
```

它只处理 `llvm-mc` 那一行，跳过 `-filetype=obj` 和 `objdump` 的 RUN。生成出来的 diff（节选）：

```diff
+// NOTE: Assertions have been autogenerated by utils/update_mc_test_checks.py UTC_ARGS: --version 6
 # RUN: llvm-mc -triple=minirv64 -show-encoding %s | FileCheck %s
...
-# CHECK: add x1, x2, x3
-# CHECK-SAME: encoding: [0xb3,0x00,0x31,0x00]
 add x1, x2, x3
+// CHECK: add	x1, x2, x3                      # encoding: [0xb3,0x00,0x31,0x00]
```

它会把 CHECK 放到每条汇编后面、把 `CHECK-SAME` 合并成一行，还会在注释块里插入空行。对“批量生成新指令编码”很方便，但对已有测试会造成大量无关 diff。**永远在副本上跑，先看 diff，再决定要不要合入。**

什么时候用这些脚本？

| 场景 | 建议 |
|---|---|
| 新增一批指令，需要生成基础编码 CHECK | 用 `update_mc_test_checks.py`，然后人工整理 |
| 大规模 CodeGen 输出因一次合法化改动而漂移 | 用 `update_llc_test_checks.py` 生成候选，但逐段 review |
| 语义测试（ABI、调用约定、诊断、栈布局） | **手写** CHECK，不要自动生成 |
| 负向测试（`not --crash`） | **手写**，脚本不会替你判断诊断是否正确 |
| 新 target 还没有 scrubber | 先补 `get_run_handler` 映射，或继续手写 |

为什么手写 CHECK 对语义测试更好？

1. **它表达意图**：`CHECK: add x10, x10, x11` 说的是“这里要做一次加法”，不是“这里必须出现这 12 条指令”。
2. **它稳定**：自动生成的 CHECK 会锁住寄存器分配、栈偏移、指令调度；换个 `-O` 级别或改一个无关 pass 就全红。
3. **它可读**：失败时你一眼能看出“期望 add，实际没有”，而不是在几百行 autogenerated CHECK 里找第一处不匹配。
4. **它允许否定断言**：`CHECK-NOT`、`CHECK-COUNT`、`CHECK-DAG` 这些语义，自动生成器不会替你想。
5. **它不会掩盖回归**：自动生成器会把“当前错误输出”固化成“期望输出”；如果你不看 diff 就接受，回归就变成了新基线。

## 怎么写一个回归测试

以一个真实 bug 为例：`f597d37` 之前，MiniRV 的 branch 超出 ±4 KiB（条件分支）或 ±1 MiB（`jal`）时，会把位移截断/回绕，静默跳到别处。修复后加了测试 `branch-relaxation.ll`。它的写法很值得学：

```llvm
; RUN: llc -mtriple=minirv64 < %s -o - | FileCheck %s

; CHECK-LABEL: cond_far:
; CHECK:         bne     {{x[0-9]+}}, x0, .LBB
; CHECK-NEXT:    jal     x0, .LBB
; CHECK-NOT:     beq     {{x[0-9]+}}, x0,
define i64 @cond_far(i64 %x) {
entry:
  %c = icmp eq i64 %x, 0
  br i1 %c, label %far, label %near
near:
  ; 4200 bytes pushes %far past the 13-bit branch displacement.
  call void asm sideeffect ".space 4200", ""()
  br label %exit
far:
  br label %exit
exit:
  ret i64 %x
}
```

固定套路：

1. **先复现**：用和 bug report 一样的 `llc` 参数（这里是默认 `-O2` 等价级别），确认能看到错误输出。
2. **最小化 IR**：删掉无关函数、参数、metadata；只留下触发 bug 的结构。这个例子里用 inline asm 的 `.space 4200` 精确制造“刚好超出范围”的距离，比写 4200 条指令干净。
3. **选 oracle**：如果要锁“汇编长什么样”，用 `llc | FileCheck`；如果要锁“某个 pass 之后的不变量”，用 MIR；如果要锁“运行结果”，用 e2e。
4. **加 `-verify-machineinstrs`**：只要测试涉及 machine pass，就加；它能在 CHECK 之前先抓坏 MIR。
5. **写最小 CHECK**：`CHECK-LABEL` 锚定函数，`CHECK-NEXT` 锁关键顺序，`CHECK-NOT` 锁“不能回到旧行为”。不要把所有指令都写进去。
6. **负向测试用 `not`/`not --crash`**：诊断测试要匹配消息，不要匹配 stack dump。
7. **单文件跑，再全量跑**：`llvm-lit -v <file>`，然后 `llvm-lit llvm/test/CodeGen/MiniRV llvm/test/MC/MiniRV`。

对于“静默错误”类 bug，回归测试最好同时有汇编断言和 e2e：汇编断言定位“生成策略变了”，e2e 断言“运行结果对了”。`f597d37` 的提交信息就提到，条件分支路径用 qemu 跑过（近块返回 42，远块返回 7），无条件分支路径用汇编检查 `auipc x5 + addi + jalr x0, 0(x5)`，因为 `.space` 填充出来的代码不可执行。

对于诊断类 bug，回归测试是负向的：

```llvm
; RUN: not --crash llc -mtriple=minirv64 < %s -o /dev/null 2>&1 | FileCheck %s
; CHECK: LLVM ERROR: MiniRV does not support thread-local storage
@t = thread_local global i64 7
```

注意 `2>&1`：`report_fatal_error` 把消息写到 stderr，不重定向的话 FileCheck 看不到。`-o /dev/null` 避免产生无意义的输出文件。

## 反直觉与踩雷

1. **错误直觉**：“53 个测试全过，说明 target 已经很稳了。”
   **正确认识**：53 个测试只覆盖了 MiniRV 当前实现的核心路径；MIR 是 0，扩展/ABI/代码模型/调试的矩阵也远没有 RISC-V 那么厚。测试数量要和风险匹配，不是和“感觉”匹配。

2. **错误直觉**：“自动生成的 CHECK 更全面，应该优先用。”
   **正确认识**：自动生成的 CHECK 锁的是“当前代码长什么样”，不是“语义应该是什么”。它会把寄存器分配、栈偏移、调度顺序全部固化；一次无关改动就全红，而真正的回归可能被“更新基线”掩盖。语义测试手写，批量编码测试才用生成器。

3. **错误直觉**：“所有错误测试都写 `not --crash` 最保险。”
   **正确认识**：`not --crash` 只匹配“崩溃”（abort/信号）。如果实现改用 `reportFatalUsageError`（exit 1），测试必须改成 `not`；否则测试会因为“没有崩溃”而失败。测试指令要和实现的终止方式一致。

4. **错误直觉**：“`CHECK:` 写在函数前面就够了，不需要 `CHECK-LABEL`。”
   **正确认识**：没有 `CHECK-LABEL`，FileCheck 会在整个文件里找第一个匹配，可能匹配到另一个函数。`CHECK-LABEL` 把作用域切开，是 CodeGen 测试的基本纪律。

5. **错误直觉**：“e2e 挂了就是编译器 bug。”
   **正确认识**：e2e 依赖 `llc`、`ld.lld`、`qemu`、`start.s` 和测试程序本身。我们实测时第一次 `t1` 在 RISC-V 参考侧 qemu 段错误，重跑就 17/17；这类抖动要先复现、再定位，不要直接改后端。

6. **错误直觉**：“MIR 测试是高级玩法，bring-up 不需要。”
   **正确认识**：只要你改 machine pass，MIR 就是最便宜的定位工具。汇编测试只能看到最终结果，MIR 能看到 pass 的输入/输出；没有它，branch relaxation/PEI/regalloc 的 bug 会反复横跳。

7. **错误直觉**：“`llc` 默认就是 `-O2`，测试里不用写。”
   **正确认识**：默认值可能变，而且测试意图不明确。新测试显式写 `-O2` 或 `-O0`；如果测试依赖某个 pass，再加 `-stop-after`/`-run-pass` 把范围钉死。

## 进阶：让测试金字塔真正运转

- **先补工具链**：`update_llc_test_checks.py` 需要 `get_run_handler` 认识 `minirv64`，否则你只能手写。把这条映射加进 `UpdateTestChecks/asm.py`，本身就是一个可 review 的小 patch。
- **用 `split-file` 组织多场景**：一个 `.ll` 里放多个 `--check-prefix` 场景会让文件变长；`split-file` 可以把 IR 和期望输出拆开，适合 ABI/异常这类复杂测试。
- **用 `REQUIRES`/`XFAIL` 管理平台差异**：需要 qemu 的测试、需要特定 host 的测试，应该显式标记，而不是在 CI 里随机失败。
- **把 smoke 脚本接进 CI**：`dev/minirv-smoke.sh` 已经串起 auto-td、lit、e2e、size；CI 里跑它，比单独跑 lit 更能防止“测试全绿但程序跑不起来”。
- **给每个 bug 留一个最小测试**：修 bug 的 commit 必须带回归测试；测试名用 `*-error.ll`（诊断）、`*-relaxation.ll`（pass）、`*-abi.ll`（ABI）这样的语义命名，而不是 `pr12345.ll`。
- **定期看测试分布**：`ls llvm/test/CodeGen/MiniRV | wc -l`、`find ... -name '*.mir' | wc -l`、错误测试数量；如果 MIR 一直是 0，而你在改 PEI，这就是明显的覆盖缺口。

## 本章重点整理

- 测试金字塔：MC 编码 → CodeGen 汇编 → MIR pass → lit driver → e2e 差分 → 代码尺寸；越往下越便宜、越具体，越往上越真实、越难定位。
- MC 测试用 `llvm-mc -show-encoding` + `llvm-objdump` 双路径；CodeGen 测试用 `llc | FileCheck`，`CHECK-LABEL` 锚定函数，`CHECK-NEXT`/`CHECK-NOT` 锁顺序和否定行为。
- MiniRV 有 49 个 CodeGen `.ll`（37 正向 + 12 负向）和 4 个 MC `.s`，共 53 个 lit 测试；MIR 是 0，这是当前最大的覆盖缺口。
- `llvm-lit -v` 单文件跑，`llvm-lit llvm/test/CodeGen/MiniRV llvm/test/MC/MiniRV` 全量跑；53/53 通过。
- e2e 用 `selftest.ll` 这类返回小整数的程序，比较 MiniRV 与 RISC-V 的 qemu 退出码；17/17 是 smoke，不是证明。
- 代码尺寸基线只数静态指令，不是性能；它的用途是发现回归，不是排名。
- `update_llc_test_checks.py` 对 MiniRV 目前会 `KeyError: "Triple 'minirv64' is not supported"`，需要补 `UpdateTestChecks/asm.py` 的 triple 映射；`update_mc_test_checks.py` 能用但会重写注释/跳过非 MC 的 RUN，必须看 diff。
- 语义测试手写 CHECK；自动生成适合批量编码和机械漂移，不适合诊断/ABI/栈布局。
- 回归测试固定套路：复现 → 最小化 → 选 oracle → `-verify-machineinstrs` → 最小 CHECK → 单文件/全量跑。

## 自我检核

- [ ] 不看文件，画出 MiniRV 的测试金字塔，并说出每层各有多少测试、失败时能定位到什么。
- [ ] 解释 `alu.ll` 两条 `RUN:` 分别在验证什么；第二条为什么要经过 `llvm-objdump`。
- [ ] 为什么 `CHECK-LABEL` 是必需的？如果没有它，`CHECK: add x10, x10, x11` 可能匹配到什么？
- [ ] MiniRV 的 MIR 测试数量是多少？如果要把 `-stop-after=finalize-isel` 的输出变成 `.mir` 测试，第一步命令是什么？
- [ ] `update_llc_test_checks.py` 为什么对 `minirv64` 失败？修复点在哪张表？
- [ ] 手写 CHECK 和自动生成 CHECK 各适合什么场景？各举一个 MiniRV 测试为例。
- [ ] 给一个“分支超出范围”的 bug，写出最小回归测试的步骤和至少两条 CHECK。
- [ ] e2e 失败时，如何区分编译器 bug、测试程序 bug 和环境抖动？

## 延伸阅读

- **[LLVM Testing Guide](https://llvm.org/docs/TestingGuide.html)** — LLVM 项目
  - **读哪里**：lit/FileCheck 的基本用法、`RUN:` 行、测试组织、`REQUIRES`/`XFAIL`。
  - **能学到什么**：官方推荐的测试分层和命名习惯；为什么回归测试要最小化。
  - **前提**：能运行 `llvm-lit`，读过本章的 `alu.ll`/`basic.s`。

- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)** — LLVM 项目
  - **读哪里**：`-v`、`--filter`、`--time-tests`、`%s`/`%t` 替换、测试发现规则。
  - **能学到什么**：怎么只跑一个测试、怎么看失败命令、怎么在 CI 里定位 flaky test。
  - **前提**：知道 lit 是一个 Python 测试 driver，不是编译器的一部分。

- **[FileCheck Command Guide](https://llvm.org/docs/CommandGuide/FileCheck.html)** — LLVM 项目
  - **读哪里**：`CHECK`/`CHECK-NEXT`/`CHECK-NOT`/`CHECK-DAG`/`CHECK-LABEL`、`--check-prefix`、正则和变量。
  - **能学到什么**：怎么写“表达意图”的断言，而不是把当前输出全部抄一遍。
  - **前提**：读过本章 `alu.ll` 的 CHECK 段落。

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)** — LLVM 项目
  - **读哪里**：MIR 的 IR 部分、machine function、`-run-pass`、`-simplify-mir`。
  - **能学到什么**：怎么把 `-stop-after` 的输出变成可维护的 pass 级测试。
  - **前提**：知道 MachineFunction/MachineInstr 的基本结构（Ch 11–13）。

→ [Ch 25 端到端与差分测试](./25-end-to-end-differential-testing.md)
