# 练习 D — 用 MIR 驱动机器 pass

> **目标**：把“读 pass 源码”变成“用 MIR 单独驱动一个 pass”。能从真实函数导出 MIR、改一条指令、用 `-run-pass` 观察 pass 是否纠正、用 `-verify-machineinstrs` 证明输入合法，并把结果写成上游风格的 `llvm/test/CodeGen/RISCV/*.mir` 测试。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（LLVM `24.0.0git`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`）；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）。本章“实测”均来自该构建；跑不了的标“未实测”。

> **前置**：读完 [Ch 03 — 流水线与 MIR](./03-pipeline-and-mir.md)（`-stop-after`/`-run-pass`、MIR 字段）与 [Ch 19 — 机器层优化 pass](./19-machine-optimizations.md)（`riscv-opt-w-instrs`、`riscv-dead-defs`、`riscv-make-compressible` 的行为）。本章不重复 pass 算法，只练“怎么把 pass 单独拎出来”。

---

## D.1 为什么 MIR 比 IR 更适合测机器 pass

IR 到 MIR 之间还隔着 ISel、RA 和一串前置优化：

```text
IR → IR passes → Legalize/Combine/ISel → MachineSSA 优化
   → RegAlloc → PostRA/PreEmit（伪指令展开、压缩） → MIR
```

用 IR 测机器 pass，等于让整条流水线一起参与：ISel 可能选出不同 pseudo，RA 可能换寄存器编号，前面的优化可能把待测指令吃掉。测试失败时，分不清是 pass 错了还是上游先改了形状。同一份 IR 在 `prolog-epilog` 的实测差异：

```bash
cat > /tmp/pd/store.ll <<'EOF'
define void @f(ptr %p, i32 %a, i32 %b) {
  %r = add i32 %a, %b
  store i32 %r, ptr %p
  ret void
}
EOF
for O in 0 2; do $T/bin/llc -mtriple=riscv64 -O$O -stop-after=prolog-epilog \
  /tmp/pd/store.ll -o - | sed -n '/body:/,$p' | head -12; done
```

`-O0` 有栈槽和 `ADDW`；`-O2` 没有栈槽，`ADDW` 也变成了 `ADD`：

```yaml
# -O0: SD $x11, $x2, 8 ... ; renamable $x10 = ADDW ...
# -O2: renamable $x11 = ADD ... ; SW killed renamable $x11, ...
```

| 维度 | IR 测试 | MIR + `-run-pass` |
|---|---|---|
| 输入 | 还要经过 ISel/RA | 直接锁定 `MachineInstr` |
| 跑的 pass | 整条 pipeline | 只跑 `-run-pass` 列出的 pass |
| 失败定位 | 可能是上游改了形状 | 输入固定，差异就是该 pass 的行为 |
| 寄存器 | vreg 编号不稳定 | 可写 `%0:gpr`，也可写 `$x10` |
| 上游用法 | 少 | 本树 `llvm/test/CodeGen/RISCV/*.mir` 有 101 个 |

MIR 测不了 IR 合法性、ABI lowering、ISel pattern；要测“IR 到机器指令选得对不对”，仍然用 `.ll` + `-stop-after=finalize-isel` 或完整 `llc`。

---

## D.2 先建立直觉：MIR 文件的解剖

本树真实文件 `llvm/test/CodeGen/RISCV/opt-w-instrs.mir` 的第一个函数（字段与真实文件一致，长 RUN 行为排版折行；该文件没有 IR 段，带 IR 段的例子见 D.7 与参考答案 1）：

```text
# RUN: llc -mtriple=riscv64 -mattr='+d,+zfa,+v,+xtheadmempair' \
#      -run-pass=riscv-opt-w-instrs %s -o - | FileCheck %s   ← 测试入口
---
name:            fcvtmod_w_d                                  ← 函数符号
tracksRegLiveness: true                                       ← 维护 liveness
body:             |
  bb.0.entry:                                                 ← 基本块
    liveins: $x10                                             ← 块入口活跃物理寄存器
    ; CHECK-LABEL ...                                         ← FileCheck 期望
    %0:fpr64 = COPY $x10                                      ← 虚拟寄存器 + 类
    %1:gpr = nofpexcept FCVTMOD_W_D %0, 1
    %2:gpr = ADDIW %1, 0
    $x10 = COPY %2                                            ← 物理寄存器
    PseudoRET                                                 ← 伪指令收尾
...                                                           ← 函数结束
```

| 字段 | 含义 | 什么时候重要 |
|---|---|---|
| `# RUN:` | lit 的测试命令，`llc` 不解析注释 | 决定 CI 怎么跑 |
| `--- \|` | 一个 LLVM IR 模块（YAML block scalar） | 需要 `@global`、外部函数、属性、`alloca` 时才写 |
| `---` | 一个 `MachineFunction` | 每个函数一段，可多段 |
| `name:` | 函数符号名，要与 IR 的 `define` 对上 | 对不上会建空函数，pass 看到的东西不同 |
| `tracksRegLiveness:` | 是否维护块内 `liveins` 与 `killed/dead` | 依赖 liveness 的 pass 与 verifier 都看它 |
| 全局/块内 `liveins:` | 函数入口 / 基本块入口的物理寄存器活跃值 | 块内列表在 `tracksRegLiveness: true` 时必需 |
| `%0:gpr` / `$x10` | 虚拟寄存器 + 类 / 物理寄存器（`a0`） | MachineSSA / PostRA 阶段的区别 |
| `:: (store (s32))` | 内存操作数描述，供 AA/别名分析 | load/store 相关 pass 会读 |
| `implicit-def dead $x1` | 隐含定义 `ra`，值不再使用 | call/return 的 ABI 副作用；漏写会被 verifier 抓 |
| `PseudoRET` | 后端伪指令，展开前保留 | 机器层测试常以 pseudo 收尾 |

`implicit-def` 的真实例子（`branch-opt.mir:123`）：

```yaml
PseudoCALL target-flags(riscv-call) @bar, csr_ilp32_lp64, implicit-def dead $x1, implicit $x10, implicit-def $x2
```

`implicit-def dead $x1` 定义 `ra` 但不用；`implicit $x10` 读第一个参数；`implicit-def $x2` 破坏 `sp`。这些不是注释，verifier 会据此检查寄存器是否真的活跃。`registers:` 段不是必需的：本练习手写的 MIR 都省略它，只要每个 vreg 首次出现带 `:gpr`/`:fpr64` 类名，parser 就能建表。

> **关键区别**：全局 `liveins:` 是函数入口的事实，块内 `liveins:` 是每个基本块入口的事实。`tracksRegLiveness: true` 时块内列表必须准确；设成 `false` 后 parser 不再要求它，verifier 也跳过 liveness 检查。

---

## D.3 三个必会命令

### D.3.1 `-run-pass`：只跑一个 pass

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs -simplify-mir \
  /tmp/pd/addw.mir -o -
```
`-simplify-mir` 只影响打印，不改变 pass 行为。它只接受 `.mir`；喂 `.ll` 会报 `llc: error: run-pass is for .mir file only.`。它按 `getAnalysisUsage` 补必要 analyses，但不会补 ISel/RA 的“建场”。输入必须是该 pass 所在阶段的 MIR：PreRA pass 给 SSA MIR，PostRA pass 给 `noVRegs: true` 的 MIR。

### D.3.2 `-start-before` / `-stop-after`：截取一段 pipeline

```bash
# 从 IR 导出 MIR
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=prolog-epilog -simplify-mir \
  /tmp/pd/add.ll -o /tmp/pd/add-prolog.mir
# 在 MIR 上从某个 pass 继续
$T/bin/llc -mtriple=riscv64 \
  -start-before=riscv-opt-w-instrs -stop-after=riscv-opt-w-instrs \
  /tmp/pd/addw.mir -o -
```

第二条本树实测有效，输入 `ADDW`、输出 `ADD`。但 `-start-before` 是“从流水线这个位置继续”，**不是“先做完前置再从这个 pass 开始”**。在 IR 上直接 `-start-before=riscv-opt-w-instrs` 会跳过 ISel，实测触发 `Assertion !NodePtr->isKnownSentinel() failed`；在 IR 上 `-start-before=finalize-isel -stop-after=finalize-isel` 更隐蔽：不崩溃，但 `body:` 为空，因为 ISel 根本没跑。正确姿势是先用 `-stop-after` 导出 MIR，再在 MIR 上用 `-run-pass` 或 `-start-before`。

### D.3.3 `-print-after` / `-print-after-all`：确认 pass 名与输出

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v \
  -run-pass=riscv-vl-optimizer -print-after=riscv-vl-optimizer \
  /tmp/pd/vl.mir -o /dev/null
```

实测头两行：

```text
# *** IR Dump After RISC-V VL Optimizer (riscv-vl-optimizer) ***:
# Machine code for function f: IsSSA, NoPHIs, TracksLiveness
```

括号外是 `getPassName()` 显示名，括号内是 `INITIALIZE_PASS` 注册名。`-print-after` 按注册名匹配；写错不报错，只静默无输出（实测 `-print-after=riscv-opt-w-instr` 退出 0）。`-run-pass` 写错则硬报错。`-print-after-all` 打印每个 pass 后的 MIR：

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs \
  -print-after-all /tmp/pd/addw.mir -o /dev/null 2>&1 | grep '^# \*\*\*'
```

```text
# *** IR Dump After RISC-V Optimize W Instructions (riscv-opt-w-instrs) ***:
# *** IR Dump After Verify generated machine code (machineverifier) ***:
```

| 命令 | 输入 | 用途 | 典型误用 |
|---|---|---|---|
| `-run-pass=<p>` | `.mir` | 单跑一个或一组 pass | 给 `.ll`，报 `run-pass is for .mir file only` |
| `-stop-after=<p>` | `.ll`/`.mir` | 导出某阶段 MIR | 以为它只跑一个 pass |
| `-start-before=<p> -stop-after=<q>` | `.mir` 最稳 | 跑一段 pipeline | 在 `.ll` 上从机器 pass 硬起，空函数或崩溃 |
| `-print-after=<p>` | 任意 | 看某 pass 后的 MIR | 名字拼错，静默无输出 |

---

## D.4 pass 名从哪来

**一、`-debug-pass=Structure`**：打印实际跑过的 pass 参数名。

```bash
$T/bin/llc -mtriple=riscv64 -O2 -debug-pass=Structure /tmp/pd/add.ll -o /dev/null 2>&1 \
  | head -1 | tr ' ' '\n' | grep riscv
```

本树 `-O2` 实测（节选，按 pipeline 顺序）：`riscv-vl-optimizer`、`riscv-vector-peephole`、`riscv-fold-mem-offset`、`riscv-opt-w-instrs`、`riscv-expand-pseudo-pre-ra`、`riscv-merge-base-offset`、`riscv-insert-vsetvli`、`riscv-dead-defs`、`riscv-expand-pseudo-post-ra`、`riscv-make-compressible`、`riscv-expand-pseudo-pre-emit`、`riscv-expand-pseudo-atomics`、`riscv-asm-printer`。

**二、源码里的 `INITIALIZE_PASS`**：注册名是第二个参数，显示名是第三个。`RISCVOptWInstrs.cpp:45,87`：

```cpp
#define DEBUG_TYPE "riscv-opt-w-instrs"
INITIALIZE_PASS(RISCVOptWInstrsLegacy, DEBUG_TYPE, RISCV_OPT_W_INSTRS_NAME,
                false, false)
```

`riscv-vl-optimizer` 还演示了带 analysis 依赖的写法（`RISCVVLOptimizer.cpp:173-176`）：`INITIALIZE_PASS_BEGIN` + `INITIALIZE_PASS_DEPENDENCY(MachineDominatorTreeWrapperPass)` + `INITIALIZE_PASS_END`。

**三、`-run-pass` 的报错**：写错名字时 llc 会直接说没注册。

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-dead-register-definitions /tmp/pd/min.mir -o -
```

```text
llc: error: run-pass riscv-dead-register-definitions is not registered.
```

> **本树纠正**：`riscv-dead-register-definitions` 不存在；`RISCVDeadRegisterDefinitions.cpp:26` 的 `DEBUG_TYPE` 是 `riscv-dead-defs`。文件名 ≠ pass 名。

本树存在且注册名核对过的 9 个名字（本章直接跑过其中 5 个）：

| pass 名（`-run-pass`） | 注册文件 | 职责 |
|---|---|---|
| `riscv-opt-w-instrs` | `RISCVOptWInstrs.cpp:87` | 删冗余 `sext.w`，规范化 W/非 W |
| `riscv-dead-defs` | `RISCVDeadRegisterDefinitions.cpp:53` | dead virtual def 改成 `x0` |
| `riscv-merge-base-offset` | `RISCVMergeBaseOffset.cpp:56` | 尾部偏移折进 `lui/auipc+addi` |
| `riscv-make-compressible` | `RISCVMakeCompressible.cpp:104` | 为 load/store 换压缩寄存器/偏移 |
| `riscv-expand-pseudo-pre-ra` | `RISCVExpandPseudoPreRA.cpp:198` | PreRA 伪指令展开 |
| `riscv-expand-pseudo-post-ra` | `RISCVExpandPseudoPostRA.cpp:215` | PostRA 常量/地址伪指令展开 |
| `riscv-insert-vsetvli` | `RISCVInsertVSETVLI.cpp:157` | 插入/合并 `vsetvli` |
| `riscv-vl-optimizer` | `RISCVVLOptimizer.cpp:173` | 向量 VL 传播与化简 |
| `riscv-vector-peephole` | `RISCVVectorPeephole.cpp:99` | 向量 mask/指令 peephole |

`-help-hidden` **不列** pass 名；它列的是 command-line options，实测 `grep -c 'riscv-opt-w-instrs'` 为 0。

---

## D.5 手写 MIR 的完整流程

### D.5.1 从真实函数导出

```bash
cat > /tmp/pd/add.ll <<'EOF'
define i64 @f(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
EOF
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=prolog-epilog -simplify-mir \
  /tmp/pd/add.ll -o /tmp/pd/add-prolog.mir
sed -n '/body:/,$p' /tmp/pd/add-prolog.mir
```

实测 body：`liveins: $x10, $x11`；`renamable $x10 = ADD killed ...`；`PseudoRET implicit $x10`。

`prolog-epilog` 之后已是物理寄存器，适合测 PostRA pass；要测 MachineSSA pass，改用 `-stop-after=finalize-isel`。

### D.5.2 改一处，跑 pass

把 `ADD` 改成 `ADDW`，保留 ABI liveins。完整文件见参考答案 1；关键片段：

```yaml
bb.0:
  liveins: $x10, $x11, $x12
  %0:gpr = COPY $x10
  %1:gpr = COPY $x11
  %2:gpr = COPY $x12
  %3:gpr = ADDW %1, %2
  SW %3, %0, 0 :: (store (s32))
  PseudoRET
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/pd/addw.mir -o -
```

输出：`%3:gpr = ADD %1, %2`；下一条 `SW %3, %0, 0` 不变。

`ADDW` 变 `ADD`，因为唯一使用者 `SW` 只要求低 32 位；判定链在 `hasAllNBitUsers` / `hasAllWUsers`（Ch 19）。

### D.5.3 verifier：故意制造一个错误

```bash
$T/bin/llc -mtriple=riscv64 -verify-machineinstrs -run-pass=none /tmp/pd/fixed.mir -o -
```

`-run-pass=none` 的语义是“不跑任何 pass，只加载、验证、打印 MIR”。MIR parser 本身会验证一次；`-verify-machineinstrs` 额外在每个 pass 后插 verifier。删掉 `liveins: $x10`，实测报：

```text
*** Bad machine code: Using an undefined physical register ***
- function:    f
- basic block: %bb.0  (0x5613ef115e00)
- instruction: %0:gpr = COPY $x10
- operand 1:   $x10
```

修法是补回块内 `liveins: $x10`。没有 `-verify-machineinstrs` 时，加载阶段仍会抓到这个错误；但“pass 之后才坏”的问题只有 `-verify-machineinstrs` 能在 pass 边界抓到。

<details>
<summary>参考答案 1：完整可跑的 `opt-w-instrs` MIR 文件</summary>

```yaml
# RUN: llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs %s -o - | FileCheck %s

--- |
  define void @f(ptr %p, i64 %a, i64 %b) {
    %r = add i64 %a, %b
    %t = trunc i64 %r to i32
    store i32 %t, ptr %p
    ret void
  }
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $x10, $x11, $x12
    ; CHECK-LABEL: name: f
    ; CHECK: %3:gpr = ADD %1, %2
    ; CHECK-NEXT: SW %3, %0, 0
    %0:gpr = COPY $x10
    %1:gpr = COPY $x11
    %2:gpr = COPY $x12
    %3:gpr = ADDW %1, %2
    SW %3, %0, 0 :: (store (s32))
    PseudoRET
...
```

用 `llc -run-pass=riscv-opt-w-instrs /tmp/pd/fc.mir -o - | FileCheck /tmp/pd/fc.mir` 实测通过。
</details>

<details>
<summary>参考答案 2：verifier 报错与修复</summary>

错误输入（缺块内 liveins）：

```yaml
--- |
  define i64 @f(i64 %a) { ret i64 %a }
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    %0:gpr = COPY $x10
    $x10 = COPY %0
    PseudoRET
...
```

报错原文 `*** Bad machine code: Using an undefined physical register ***`（上下文见 D.5.3）。修复：`bb.0:` 下加 `liveins: $x10`。反例：`tracksRegLiveness: false` + 无 `liveins` 时 verifier 不报错（实测退出码 0）——检查被关掉了。
</details>

<details>
<summary>参考答案 3：把上面的 MIR 写成 FileCheck 测试</summary>

三行 check 放在 body 内；`CHECK-NEXT` 要求 `SW` 紧接 `ADD`，中间多一条 `COPY` 就会失败：

```yaml
; CHECK-LABEL: name: f
; CHECK: %3:gpr = ADD %1, %2
; CHECK-NEXT: SW %3, %0, 0
```

实测通过；把第三行改成 `CHECK-NEXT: NOPE`，FileCheck 报 `expected string not found in input`，并打印扫描起点。上游脚本 `python3 llvm/utils/update_mir_test_checks.py --llc-binary $T/bin/llc /tmp/pd/fc.mir` 能自动生成覆盖整个 body 的 checks。
</details>

---

## D.6 案例一：`riscv-opt-w-instrs` 删冗余 `sext.w`

D.5.2 已给出 `ADDW→ADD` 的输入、命令与输出。第二个实测：删 `ADDIW rd, rs1, 0`。

```yaml
# 输入
%2:gpr = ADDW %0, %1
%3:gpr = ADDIW %2, 0
$x10 = COPY %3
PseudoRET implicit $x10
# 输出（同一条 -run-pass 命令）
%2:gpr = ADDW %0, %1
$x10 = COPY %2
PseudoRET implicit $x10
```

`ADDIW %2, 0` 被删，`%3` 合并回 `%2`。`ADDW` 本身已保证 bit 31 符号扩展到 bit 63，所以这条 `sext.w` 冗余。反例：把使用者换成需要完整 64 位的 `SD`，`ADDW` 不会被换成 `ADD`，因为高 32 位不能随便丢。

---

## D.7 案例二：`riscv-make-compressible` 把 `sw zero` 换成可压缩寄存器

硬门槛：函数必须 `optsize`，subtarget 必须有 `Zca`（`RISCVMakeCompressible.cpp:483,490`）。`+c` 本树隐含 `+zca`，测试里显式写更清楚。

```yaml
--- |
  define void @f(ptr %p) optsize {
    store i32 0, ptr %p, align 4
    ret void
  }
---
name:            f
tracksRegLiveness: true
noVRegs:         true
body:             |
  bb.0:
    liveins: $x10
    SW $x0, $x10, 0
    SW $x0, $x10, 4
    SW $x0, $x10, 8
    SW $x0, $x10, 12
    PseudoRET
...
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+c,+zca \
  -run-pass=riscv-make-compressible /tmp/pd/comp.mir -o -
```

真实输出：`$x11 = ADDI $x0, 0`，紧接着 `SW $x11, $x10, 0`、`SW $x11, $x10, 4`、`SW $x11, $x10, 8`、`SW $x11, $x10, 12`，最后 `PseudoRET`。

`zero` 换成 `$x11`（`a1`，属于 `x8–x15`），并插一条 `ADDI $x11, $x0, 0` 补值。四条 store 共用一次拷贝；use 数太少时不值得插 `c.mv`/`c.li`。编码证据：`sw zero, 0(a0)` 是 `[0x23,0x20,0x05,0x00]`（4 字节），`sw a1, 0(a0)` 是 `[0x0c,0xc1]`（2 字节）；`zero` 进不了 `c.sw` 的 3 位 `rs2'` 字段。注意这个 pass 只处理 load/store；`addi → c.addi` 在 AsmPrinter 的 `RISCVRVC::compress`（Ch 19 / Ch 22）。

---

## D.8 案例三：`riscv-expand-pseudo-post-ra` 的真正主角，以及 `PseudoCALL` 的纠正

### D.8.1 `PseudoCALL` 不归 post-RA pass 管

常见说法是“`riscv-expand-pseudo-post-ra` 把 `PseudoCALL` 展开成 `auipc+jalr`”。**本树不是这样。** `PseudoCALL` 一路活到 MC 编码期，由 `RISCVMCCodeEmitter::expandFunctionCall`（`RISCVMCCodeEmitter.cpp:169`）展开；`RISCVExpandPseudoPostRA.cpp:68` 的 `expandMI` 只处理 `PseudoMovImm`、`PseudoMovAddr`、`PseudoAddUpperImm`、`PseudoMERGE`。TD 注释也写明（`RISCVInstrInfo.td:1892-1899`）：`PseudoCALL is a pseudo instruction which will eventually expand to auipc and jalr while encoding.`

实测：`PseudoCALL` 喂给 post-RA pass，输出原样保留：

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-expand-pseudo-post-ra /tmp/pd/call.mir -o -
```

```yaml
PseudoCALL target-flags(riscv-call) @foo, csr_ilp32_lp64, implicit-def dead $x1, implicit-def $x2
PseudoRET
```

真正展开在目标文件里：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+c -filetype=obj /tmp/pd/call.ll -o /tmp/pd/call.o
$T/bin/llvm-objdump -d -r /tmp/pd/call.o
```

```text
       4: 00000097      auipc   ra, 0x0
                0000000000000004:  R_RISCV_CALL_PLT  foo
       8: 000080e7      jalr    ra <f+0x4>
```

`auipc ra` + `jalr ra` + `R_RISCV_CALL_PLT` 就是最终形态；`+relax` 时还带 `R_RISCV_RELAX`，链接器可缩成 `jal`。

### D.8.2 post-RA 的真实案例：`PseudoAddUpperImm → LUI+ADD`

```yaml
name:            test
noVRegs:         true
body:             |
  bb.0:
    liveins: $x2
    $x10 = PseudoAddUpperImm $x2, 4
    PseudoRET
...
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-expand-pseudo-post-ra /tmp/pd/postra.mir -o -
```

```yaml
body:             |
  bb.0:
    liveins: $x2

    $x10 = LUI 4
    $x10 = ADD $x2, killed $x10
    PseudoRET
...
```

目的寄存器已经是物理寄存器，`Hi` 立即数已就绪，所以只机械生成 `LUI` + `ADD`；这和 `PseudoMovAddr → LUI+ADDI` 同类。

---

## D.9 案例四：`riscv-vl-optimizer` 把 `VLMAX` 收紧到实际需求

`-1` 表示 `VLMAX`；pass 沿 use 链传播“实际最多需要多少元素”。这里只看最小片段，不重复 Ch 17 的 vsetvli 插入。

```yaml
--- |
  define void @f() { ret void }
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    %0:vr = PseudoVADD_VV_M1 $noreg, $noreg, $noreg, -1, 3 /* e8 */, 0
    %1:vr = PseudoVADD_VV_M1 $noreg, %0, $noreg, 1, 3 /* e8 */, 0
    %2:vr = PseudoVADD_VV_M1 $noreg, %0, $noreg, 2, 3 /* e8 */, 0
    $v8 = COPY %1
    $v9 = COPY %2
    PseudoRET
...
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -run-pass=riscv-vl-optimizer /tmp/pd/vl.mir -o -
```

```yaml
%0:vr = PseudoVADD_VV_M1 $noreg, $noreg, $noreg, 2 /* vl */, 3 /* e8 */, 0
%1:vr = PseudoVADD_VV_M1 $noreg, %0, $noreg, 1 /* vl */, 3 /* e8 */, 0
%2:vr = PseudoVADD_VV_M1 $noreg, %0, $noreg, 2 /* vl */, 3 /* e8 */, 0
```

`%0` 的两个使用者只要 1 和 2 个元素，所以 `VLMAX` 收紧到 2。变换不改指令条数，但让后面的 `riscv-insert-vsetvli` 更容易共用 `vsetvli`。

---

## D.10 源码落点

| 位置 | 读什么 | 为什么 |
|---|---|---|
| `RISCVTargetMachine.cpp:387,396,489,513,555,564` | 六个 `createRISCV*Pass()` 调用 | 每个 pass 的流水线位置 |
| `RISCVOptWInstrs.cpp:45,87,740,802,905` | 注册名、删 `sext.w`、规范化 W 后缀 | `riscv-opt-w-instrs` 全链 |
| `RISCVVLOptimizer.cpp:40,173,1453` | 注册名、analysis 依赖、`runOnMachineFunction` | `riscv-vl-optimizer` 全链 |
| `RISCVMakeCompressible.cpp:82,104,483,490` | 注册名、`optsize`/`Zca` 门槛 | 为什么你的输入不触发 |
| `RISCVExpandPseudoPostRA.cpp:68,215` | `expandMI` 四个 case、注册名 | 纠正 `PseudoCALL` 归属 |
| `RISCVMCCodeEmitter.cpp:169,578-583` | `expandFunctionCall`、`PseudoCALL` switch | `PseudoCALL → auipc+jalr` 真正位置 |
| `RISCVInstrInfo.td:1892-1899` | `PseudoCALL` 注释与 `Size = 8` | 目标文件里为什么是 8 字节 |
| `llvm/docs/MIRLangRef.md:70-100,294-305` | `-simplify-mir` 能删什么、块内 `liveins` | 手写 MIR 的字段契约 |

---

## D.11 可跑实验

```bash
$T/bin/llc --version | head -3
$T/bin/llc -mtriple=riscv64 -O2 -debug-pass=Structure /tmp/pd/add.ll -o /dev/null 2>&1 \
  | head -1 | tr ' ' '\n' | grep riscv | head

# 1. 导出 MIR 并单跑；第三条在 IR 上从机器 pass 硬起，应触发断言
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=finalize-isel -simplify-mir \
  /tmp/pd/store.ll -o /tmp/pd/store-isel.mir
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/pd/store-isel.mir -o - | grep -E 'ADDW|ADD '
$T/bin/llc -mtriple=riscv64 -O2 -start-before=riscv-opt-w-instrs /tmp/pd/add.ll -o - 2>&1 | head -4

# 2. verifier：错误、修复、静默放行
$T/bin/llc -mtriple=riscv64 -run-pass=none /tmp/pd/nolive.mir -o - 2>&1 | grep -A5 'Bad machine code'
$T/bin/llc -mtriple=riscv64 -verify-machineinstrs -run-pass=none /tmp/pd/fixed.mir -o /dev/null && echo VERIFIER_OK
$T/bin/llc -mtriple=riscv64 -verify-machineinstrs -run-pass=none /tmp/pd/nolive-notrack.mir -o /dev/null && echo SILENT_PASS
# 3. -simplify-mir 删了什么；FileCheck 的 -NEXT 失败
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=prolog-epilog /tmp/pd/add.ll -o /tmp/pd/full.mir
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=prolog-epilog -simplify-mir /tmp/pd/add.ll -o /tmp/pd/simp.mir
diff -u /tmp/pd/full.mir /tmp/pd/simp.mir | grep -E '^[-+](tracksRegLiveness|  - \{ reg|liveins)' | head
sed 's/; CHECK-NEXT: SW %3, %0, 0/; CHECK-NEXT: NOPE/' /tmp/pd/fc.mir > /tmp/pd/fc-bad.mir
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/pd/fc-bad.mir -o - | $T/bin/FileCheck /tmp/pd/fc-bad.mir
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/llvm-lit llvm/test/CodeGen/RISCV/opt-w-instrs.mir llvm/test/CodeGen/RISCV/postra-expand-pseudo.mir llvm/test/CodeGen/RISCV/make-compressible.mir llvm/test/CodeGen/RISCV/rvv/vl-opt.mir
```
本树实测：最后一组 4 个测试全部 `PASS`（`Testing Time: 0.24s`）。qemu 6.2 只支持 draft V 0.7，RVV 不要用它跑；标量静态程序的冒烟可用 `clang --target=riscv64-linux-gnu -static -nostdlib ... -fuse-ld=lld` 后交给 `/usr/bin/qemu-riscv64-static`，本练习已验证一个返回 42 的自由链接程序。

---

## D.12 踩雷集锦

1. **`tracksRegLiveness` 与实际输入不一致，verifier 会替你“放行”。** 设成 `true` 却漏写块内 `liveins`，报 `Using an undefined physical register`；设成 `false` 再漏写，实测退出码 0，但依赖 liveness 的 pass 看到的是不完整信息。

2. **`-simplify-mir` 删的是“默认值 + 冗余注解”，不是无差别删 `implicit-def`。** 实测会去掉 `tracksRegLiveness: true`（默认 false）、`virtual-reg: ''`、空 `registers: []`、大量默认 `frameInfo` 字段；显式写的 `implicit-def dead $x1` 仍保留。危险在于 `tracksRegLiveness` 被省略后 liveness 被当关闭，pass 行为随之改变。

3. **pass 名的大小写、版本、拼写都可能是坑。** `riscv-dead-register-definitions` 在本树不存在，真名是 `riscv-dead-defs`；`-run-pass` 拼错硬报 `not registered`，`-print-after` 拼错却静默无输出。脚本里的名字要用 `-debug-pass=Structure` 或 `INITIALIZE_PASS` 核过。

4. **`-run-pass` 与 `-start-before` 不是同义词。** 前者“只跑这些 pass”，输入必须是 `.mir`；后者“从 pipeline 这个位置继续”，仍会跑后面的 pass。在 IR 上从机器 pass 开始会跳过 ISel：轻则 `body:` 为空，重则断言崩溃。

5. **手写 MIR 缺 `stack:`，frame index 直接解析失败。** 实测报 `error: ... use of undefined stack object '%stack.0'`。只要出现 `%stack.N`，就必须在 YAML 顶层写 `stack:`；只测寄存器运算的 MIR 不要引入 frame index。

6. **`-verify-machineinstrs` 只在 assertions 构建里可靠。** 本构建 `llc --version` 写明 `Optimized build with assertions.`；没有 assertions 的 release 构建里，verifier 的断言检查会失效，通过不代表 MIR 合法。

7. **`FileCheck` 的 `-NEXT` 太严。** 它要求匹配行紧挨上一行；pass 多打印一条 `COPY`、一条注释或一个空行都会失败。实测把 `CHECK-NEXT: SW ...` 改成不存在的字符串，报 `expected string not found`。只在“两条必须相邻”时用 `-NEXT`，否则用 `CHECK` 或 `CHECK-SAME`。

8. **MIR 的 IR 段与 `name:` 对不上，pass 会看到空函数。** `--- |` 里的 `define` 名、`name:`、`PseudoRET` 要属于同一个函数；只改一处会让 llc 建一个没有 body 的函数，`-run-pass` 看起来“什么都没做”。

---

## D.13 本章重点整理

- MIR 测试的价值是**锁定机器层输入**：IR 到 MIR 之间还有 ISel/RA/前置优化；`-run-pass` 只跑指定 pass，输出差异才是该 pass 的行为。
- 核心字段：`name`、`tracksRegLiveness`、块内 `liveins`、`%0:gpr`、`$x10`、`implicit-def`；IR 段可选，但引用全局/符号/属性时必需。
- 三个命令的边界：`-run-pass` 只吃 `.mir`；`-stop-after` 导出阶段快照；`-start-before`/`-stop-after` 是 pipeline 区间，不能在 IR 上从机器 pass 硬起。
- pass 名只从 `-debug-pass=Structure`、`INITIALIZE_PASS`/`getPassName`、`-run-pass` 报错三个来源拿；`-help-hidden` 不列 pass 名。
- 本树 `PseudoCALL` **不**由 `riscv-expand-pseudo-post-ra` 展开，而在 `RISCVMCCodeEmitter::expandFunctionCall` 里变成 `auipc+jalr`；post-RA 的真实案例是 `PseudoAddUpperImm → LUI+ADD`。
- 四个可复用案例：`ADDW→ADD`、`sw zero→sw a1`、`PseudoAddUpperImm→LUI+ADD`、`VLMAX→实际 VL`。
- verifier 是输入契约的裁判：缺 `liveins` 报 `Using an undefined physical register`；关掉 `tracksRegLiveness` 会把这条检查一起关掉，**通过 ≠ 正确**。

---

## D.14 自我检核

- [ ] 能说出至少两个“IR 测试机器 pass 会引入噪声”的来源，并用 `-O0`/`-O2` 的 `prolog-epilog` 差异举例
- [ ] 能逐字段解释 `name`、`tracksRegLiveness`、块内 `liveins`、`%0:gpr`、`$x10`、`implicit-def dead $x1`
- [ ] 能从 `.ll` 用 `-stop-after=prolog-epilog -simplify-mir` 导出 MIR，并改成可被 `-run-pass` 接受的输入
- [ ] 能解释 `-run-pass`、`-stop-after`、`-start-before` 三者的输入类型与语义差异
- [ ] 能用 `-debug-pass=Structure` 或 `INITIALIZE_PASS` 查出 `riscv-opt-w-instrs`、`riscv-dead-defs`、`riscv-vl-optimizer` 的注册名
- [ ] 能独立跑通 `ADDW→ADD` 与 `sw zero→sw a1`
- [ ] 能解释为什么 `PseudoCALL` 不属于 `riscv-expand-pseudo-post-ra`，并指出真正展开位置
- [ ] 能故意制造缺 `liveins` 的 MIR，读出 `Using an undefined physical register` 并修复
- [ ] 能写带 `RUN`/`CHECK-LABEL`/`CHECK-NEXT` 的 MIR 测试，并解释 `-NEXT` 过严会导致什么失败

---

## D.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/MIRLangRef.md`**：70–100（`-simplify-mir`）、294–305（块内 `liveins`）、500–540（`implicit-def`/`dead`）。MIR 字段的权威定义。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/opt-w-instrs.mir`**：`RUN` 行与 `fcvtmod_w_d`/`physreg`，对照 `-run-pass` 与 `-passes` 两种写法。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/postra-expand-pseudo.mir`** 与 **`llvm/test/CodeGen/RISCV/make-compressible.mir`**：前者是 `PseudoAddUpperImm → LUI+ADD` 最小样本，后者是 `optsize` + `+c/+zca` 完整案例。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/vl-opt.mir`** 与 **`rvv/vsetvli-insert.mir`**：VL 传播与 `vsetvli` 插入；后者 `RUN` 先跑 `liveintervals` 再跑目标 pass。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**：383–600 的 `RISCVPassConfig`，把每个 pass 对回 `addMachineSSAOptimization`/`addPreRegAlloc`/`addPreSched2`/`addPreEmitPass`。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/utils/update_mir_test_checks.py`**：`--help` 与 `--llc-binary`；上游自动生成 MIR checks 的官方脚本。

→ 下一章：[Ch 24 — Clang Driver：-march/-mabi/-mcpu 的优先级](./24-clang-driver.md)
