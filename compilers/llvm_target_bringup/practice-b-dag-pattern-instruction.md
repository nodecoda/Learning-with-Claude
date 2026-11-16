# 练习 B — 加一条带 DAG pattern 的指令

> **目标**：让 `llvm.trap()` 在 MiniRV 上真正生成 `ebreak`，并沿着 IR → SelectionDAG → MachineInstr → MC 编码一路验证，理解合法化动作和 pattern 缺一不可。

> **环境**：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/dsl-tablegen`，构建目录 `/home/ubuntu/llvm/builds/dsl-tablegen`，二进制目录 `B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`。

> **前置**：完成 [练习 A](./practice-a-mc-instruction.md)，读过 [Ch 9 TargetMachine、Subtarget、Lowering](./09-targetmachine-subtarget-lowering.md)、[Ch 12 Legalization](./12-legalization.md) 和 [Ch 13 SelectionDAG patterns](./13-selectiondag-patterns.md)。

> **产出**：一个 `EBREAK` MC 记录、一个 `ISD::TRAP` 合法化动作、一个 `Pat<(trap), (EBREAK)>`、一个 `/tmp` 下的 CodeGen lit 测试、一次真实 `llc` 前后对照，以及一次 17/17 的 e2e 回归。

## 背景：为什么 MC 记录还不够

练习 A 证明了 `llvm-mc` 能认识一条指令，但这只说明 MC 层闭环通了。`llc` 走的是另一条路：IR 先被 SelectionDAG 化，经过类型合法化和操作合法化，再由指令选择把 DAG 节点映射到机器指令。**同一条 `.td` 记录会被两条路径消费，但两条路径的入口完全不同**。

`llvm.trap()` 是一个很好的例子。它进入 SelectionDAG 后变成 `ISD::TRAP` 节点：有 chain、没有结果。默认情况下，MiniRV 没有为它设置合法化动作，DAG legalizer 把它展开成一个 `abort()` 库调用，于是 `llc` 输出 `addi x2, x2, -16; sd x1, 8(x2); jal x1, abort; ...; jalr x0, 0(x1)`。这段代码没有错，但不是“MiniRV 有一条 trap 指令”。要让 `llc` 生成 `ebreak`，必须同时做两件事：

1. 告诉合法化器：`ISD::TRAP` 在 MiniRV 上是 **Legal** 的，不要展开成 `abort`。
2. 告诉指令选择器：`(trap)` 这个 DAG 节点对应 `EBREAK`。

只做第一件事，合法化器会把 `TRAP` 留在 DAG 里，但 ISel 找不到 pattern，直接报 `Cannot select`。只做第二件事，`TRAP` 在合法化阶段已经变成 `abort` 调用，pattern 根本没有机会匹配。**这是一条“合法化动作 + pattern”必须成对出现的链路**。

### 三列对照

| 列 | RISC-V 参考实现 | MiniRV 的最小实现 | 你自己 target 的决策 |
|---|---|---|---|
| 合法化 | `RISCVISelLowering.cpp`：`setOperationAction({ISD::TRAP, ISD::DEBUGTRAP}, MVT::Other, Legal);` | `MiniRVISelLowering.cpp`：只把 `ISD::TRAP` 设为 `Legal` | 你的 ISA 有没有 trap/debugtrap 两条指令？有就分别设 |
| trap 指令 | `RISCVInstrInfo.td`：`Pat<(trap), (UNIMP)>` | `MiniRVInstrInfo.td`：`Pat<(trap), (EBREAK)>` | `trap` 不一定是 `ebreak`；RISC-V 选 `unimp`，MiniRV 选 `ebreak` |
| debugtrap | `Pat<(debugtrap), (EBREAK)>` | 本练习不处理 `ISD::DEBUGTRAP` | 如果调试器需要断点语义，再单独接 `debugtrap` |
| 指令标志 | `EBREAK` 带 `isTrap = 1`、`hasSideEffects = 1` | `EBREAK` 带 `hasSideEffects = 1`、`hasNoSchedulingInfo = 1` | 生产后端应补 `isTrap` 并测试依赖它的路径 |
| 测试 | RISC-V CodeGen 测试覆盖 `unimp`/`ebreak` | `/tmp` 下检查 `ebreak` 和 MIR `EBREAK` | 至少一个 asm 检查 + 一个 MIR 检查 |

RISC-V 把 `llvm.trap()` 降成 `unimp`，把 `llvm.debugtrap()` 降成 `ebreak`。MiniRV 的题目要求把 `trap` 降成 `ebreak`。这不是“谁对谁错”，而是**目标平台对 trap 语义的约定**。你在自己的 target 上要先回答：`llvm.trap()` 应该让 CPU 进入哪种异常？调试器断点和致命 trap 是不是同一条指令？

### 为什么 `EBREAK` 还是一条 MC 记录

`EBREAK` 既要从汇编器进入，也要从 CodeGen 进入，所以不能加 `isCodeGenOnly = 1`；加上它，`llvm-mc` 就不再认识 `ebreak`。`EBREAK` 的编码是 `0x00100073`（`0x001 << 20 | 0b11100 << 2 | 0b11`），小端字节 `73 00 10 00`，和 RISC-V 的 `ebreak` 完全一致。

## 先建立直觉

整条链可以画成一条“合法化分岔”：

```
define void @t() {
  call void @llvm.trap()
  ret void
}
        │
        ▼
   ISD::TRAP  (chain, no result)
        │
        │ DAG legalizer 查 MiniRVTargetLowering 的 action
        ├──────────────────────────────┐
        │ Expand（默认）                │ Legal（本练习设置）
        ▼                              ▼
   abort() 库调用                  ISD::TRAP 留在 DAG
   + 栈帧 + jal x1, abort              │
                                       │ Instruction selection
                                       │ 匹配 Pat<(trap), (EBREAK)>
                                       ▼
                                    EBREAK
                                       │ MC encoder
                                       ▼
                                  73 00 10 00
```

关键点：**合法化发生在指令选择之前**。你在 `.td` 里写的 `Pat` 只影响 instruction selection；它管不到 legalizer 已经把 `TRAP` 展开成 `abort` 的情况。

`-stop-after=finalize-isel` 停在指令选择之后、寄存器分配之前，能直接看到 `EBREAK` 有没有被选中。如果这里没有 `EBREAK`，问题在合法化或 pattern；如果这里有但最终 asm 没有，问题在 asm printer 或后续 pass。

### `ISD::TRAP` 是什么形状

`ISD::TRAP` 是有 chain、没有结果、没有操作数的节点。它在 DAG 里长这样：

```
t1: ch = trap t0
```

`t0` 是输入 chain，`t1` 是输出 chain。`Pat<(trap), (EBREAK)>` 不写操作数，因为节点没有值操作数；chain 由 TableGen 生成的 `OPC_EmitMergeInputChains1_0` 自动合并。**pattern 只描述“哪个 opcode 变成哪个指令”，chain 接线由生成器处理**。

### 为什么 `ret` 还在

练习 IR 是 `define void @t() { call void @llvm.trap(); ret void }`。`llvm.trap()` 是 `noreturn`，但 `ret` 仍然写在 IR 里。`llc` 的 codegen 级别不会替你做 IR 级死代码消除，所以最终 asm 会在 `ebreak` 后面再放一条 `jalr x0, 0(x1)`。把 IR 改成 `unreachable`，`jalr` 就会消失。这不是后端 bug，而是 IR 里有没有显式返回指令的区别。

## 任务规格

### 必做

1. 在 `MiniRVInstrInfo.td` 的 include 行后加 `EBREAK` 记录。
2. 在 `MiniRVISelLowering.cpp` 的构造函数里加 `setOperationAction(ISD::TRAP, MVT::Other, Legal);`。
3. 在 `MiniRVInstrInfo.td` 里加 `def : Pat<(trap), (EBREAK)>;`。
4. 重新构建 `llc` 和 `llvm-mc`。
5. 用 `llc -mtriple=minirv64 -O2` 编译 `llvm.trap()` IR，确认输出 `ebreak`。
6. 用 `-stop-after=finalize-isel` 确认 MIR 里有 `EBREAK`；用 `llvm-mc` 确认字节和反汇编。
7. 在 `/tmp` 下写 CodeGen lit 测试；跑现有套件、e2e；保存 patch、回滚、重新构建、复验基线。

### `EBREAK` 的编码

| 位 | 字段 | 值 | 含义 |
|---|---|---|---|
| `31-20` | `imm12` | `0x001` | `SYSTEM` 指令的立即数 1；RISC-V 用这个值表示 `ebreak` |
| `19-15` | `rs1` | `0` | 不使用源寄存器 |
| `14-12` | `funct3` | `0` | `SYSTEM` 的 `funct3` |
| `11-7` | `rd` | `0` | 不写目标寄存器 |
| `6-2` | opcode 低位 | `0b11100` | `SYSTEM` |
| `1-0` | 长度 | `0b11` | 32 位指令 |

拼接结果 `0x00100073`，小端字节 `73 00 10 00`。

### 约束与验收

- 不要加 `isCodeGenOnly = 1`：`EBREAK` 既要被 `llvm-mc` 认识，也要被 CodeGen 选中。
- `hasSideEffects = 1` 必须保留：`EBREAK` 不能被当成无副作用指令删除或重排。
- `hasNoSchedulingInfo = 1` 必须保留：MiniRV 没有完整调度模型；RISC-V 对应 `Sched<[]>`。
- `setOperationAction` 的第二个参数必须是 `MVT::Other`，不是 `MVT::i64`。`TRAP` 是 chain 节点。
- pattern 必须写在 `EBREAK` 记录之后，否则 TableGen 找不到 `EBREAK`。
- 不要改仓库里的 `libm-calls.ll`；它在 `/tmp` 副本里更新期望后验证。

验收清单：

- [ ] `llc` 输出 `ebreak`，不再输出 `jal x1, abort`。
- [ ] `-stop-after=finalize-isel` 的 MIR 里有 `EBREAK`。
- [ ] `llvm-mc -show-encoding` 输出 `[0x73,0x00,0x10,0x00]`；`llvm-objdump -d` 显示 `00100073 ebreak`。
- [ ] `/tmp` 下的 CodeGen lit 测试通过。
- [ ] e2e 17/17；回滚后 `llc` 恢复 `abort`，`git status` 只剩 `?? ana-docs/`。

## 期望输出

| 观察点 | 命令 | 期望 |
|---|---|---|
| 合法化前 | `llc -O2` | `jal x1, abort` + 栈帧 |
| 合法化后 | `llc -O2` | `ebreak`，不再有 `abort` |
| 指令选择 | `llc -stop-after=finalize-isel` | `EBREAK` + `PseudoRET` |
| MC 编码 | `llvm-mc -show-encoding` | `[0x73,0x00,0x10,0x00]` |
| 反汇编 | `llvm-objdump -d` | `00100073 ebreak` |
| lit | `/tmp` CodeGen 测试 | `Passed: 1` |
| e2e | `dev/e2e/run.sh` | 17 行 `OK`，退出码 0 |
| 回滚 | `git status --short` | 只有 `?? ana-docs/` |

## 卡住提示

1. **先看基线**：不建 pattern 时，`llc` 已经能把 `llvm.trap()` 变成 `abort`。这说明 `TRAP` 默认走 `Expand`，不是“无法处理”。你的任务是把默认库调用替换成一条指令。
2. **合法化动作放哪里**：`MiniRVTargetLowering` 构造函数里，`setOperationAction(ISD::ATOMIC_FENCE, ...)` 附近，`computeRegisterProperties` 之前。
3. **类型写 `MVT::Other`**：`ISD::TRAP` 没有值类型，只有 chain。写成 `MVT::i64` 不会生效。
4. **pattern 的名字**：`(trap)` 不是 `(ISD::TRAP)`。TableGen 里的 SDNode 名字是 `trap`，`ISD::TRAP` 是 C++ 枚举名。
5. **只加 Legal 会崩**：合法化器保留 `TRAP`，但 ISel 没有 pattern，实测得到 `LLVM ERROR: Cannot select: t1: ch = trap t0`，退出码 134。
6. **只加 pattern 不够**：`TRAP` 在合法化阶段已经变成 `abort`，pattern 没机会匹配。你会看到 `llc` 仍然输出 `jal x1, abort`，但 `.td` 里明明有 `Pat`。
7. **`ret` 为什么还在**：`llvm.trap()` 是 `noreturn`，但 IR 里显式写了 `ret void`。codegen 不做 IR 级死代码消除；换成 `unreachable` 就能去掉 `jalr`。
8. **`libm-calls.ll` 为什么失败**：它在 `trap` 函数上钉了 `CHECK: jal x1, abort`。把 `trap` 改成 `ebreak` 后，这一行必然过期。这不是回归，而是测试需要同步更新。
9. **`isTrap` 要不要加**：RISC-V 的 `EBREAK` 带 `isTrap = 1`；题目只要求 `hasSideEffects`/`hasNoSchedulingInfo`。本练习按题目做，生产后端应补 `isTrap` 并测试依赖它的路径（guided）。

## 分段步骤

### Step 0：保存现场并记录基线

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ git status --short
?? ana-docs/
```

准备 IR：

```llvm
; /tmp/practice-b-trap.ll
define void @t() {
  call void @llvm.trap()
  ret void
}
declare void @llvm.trap() noreturn nounwind
```

基线 `llc`：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o - | sed -n '7,16p'
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, abort
	...
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

这段基线有完整的栈帧保存/恢复，因为 `abort` 是一个普通调用。改成 `ebreak` 后，这些栈帧代码会消失，因为 `EBREAK` 不是调用。

### Step 1：加 `EBREAK` 记录

在 include 行后插入：

```tablegen
def EBREAK : MiniRVInst<(outs), (ins), "ebreak", "", [], InstFormatI> {
  let AsmString = "ebreak";
  let Inst{31-20} = 0x001;
  let Inst{19-15} = 0;
  let Inst{14-12} = 0;
  let Inst{11-7} = 0;
  let Inst{6-2} = 0b11100;
  let Inst{1-0} = 0b11;
  let hasSideEffects = 1;
  let hasNoSchedulingInfo = 1;
}
```

这条记录本身只让 `llvm-mc` 和 MC 层认识 `ebreak`，不会改变 `llc` 的 `llvm.trap()` 输出。先只加记录、rebuild、跑一次 `llc`，确认仍然是 `abort`，能把“MC 记录”和“CodeGen pattern”分开。

### Step 2：把 `ISD::TRAP` 标成 Legal

在 `MiniRVTargetLowering` 构造函数里，`setOperationAction(ISD::ATOMIC_FENCE, ...)` 之后加：

```cpp
  // llvm.trap() is a real instruction on MiniRV.  Marking ISD::TRAP legal
  // stops the legalizer from expanding it into an abort() libcall, and lets
  // the Pat<(trap), (EBREAK)> in MiniRVInstrInfo.td select it.
  setOperationAction(ISD::TRAP, MVT::Other, Legal);
```

`Legal` 的含义是：**DAG legalizer 不要动这个节点，把它原样留给 instruction selection**。这也是为什么下一步必须有 pattern。

### Step 3：加 pattern

在 `EBREAK` 记录后面加：

```tablegen
// llvm.trap() reaches SelectionDAG as ISD::TRAP.  The lowering marks that node
// legal, so the only way it can become code is this pattern.
def : Pat<(trap), (EBREAK)>;
```

`(trap)` 没有操作数，`(EBREAK)` 也没有操作数。chain 的接线由 TableGen 生成的 `OPC_EmitMergeInputChains1_0` 完成。

### Step 4：重新构建

```console
$ cd /home/ubuntu/llvm/builds/dsl-tablegen
$ /usr/bin/time -f 'ELAPSED=%e s' ninja llc llvm-mc
[0/2] Re-checking globbed directories...
[1/41] Building MiniRVGenDisassemblerTables.inc...
...
[40/41] Linking CXX executable bin/llc
ELAPSED=10.41 s
```

真实耗时约 **10.4 秒**。构建会重新生成 `MiniRVGenDAGISel.inc`，里面新增：

```text
/*   1297*/ /*SwitchOpcode*/ 6, TARGET_VAL(ISD::TRAP), // ->1306
/*   1300*/  OPC_RecordNode, // #0 = 'trap' chained node
/*   1301*/  OPC_EmitMergeInputChains1_0,
/*   1302*/  OPC_MorphNodeTo0Chain, TARGET_VAL(MiniRV::EBREAK),
                  0/*#Ops*/, // Ops = None
             // Src: (trap) - Complexity = 3
             // Dst: (EBREAK)
```

这就是 pattern 的全部实现。`OPC_MorphNodeTo0Chain` 说明它产生一个带 chain 的零操作数指令。

### Step 5：验证 `llc` 汇编

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o - | sed -n '7,9p'
# %bb.0:
	ebreak
	jalr	x0, 0(x1)
```

`jalr x0, 0(x1)` 还在，因为 IR 里显式写了 `ret void`。把 IR 改成 `unreachable`：

```console
$ cat > /tmp/practice-b-trap-unreachable.ll <<'LL'
define void @t() {
  call void @llvm.trap()
  unreachable
}
declare void @llvm.trap() noreturn nounwind
LL
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap-unreachable.ll -o - | sed -n '7,10p'
# %bb.0:
	ebreak
.Lfunc_end0:
	.size	t, .Lfunc_end0-t
```

两种输出都正确。练习要求的是 `ret` 版本，所以报告里以 `ebreak` + `jalr` 为准；`unreachable` 版本用来解释 `jalr` 的来源。

### Step 6：验证 MIR

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel /tmp/practice-b-trap.ll -o - | grep -A3 'body:'
body:             |
  bb.0 (%ir-block.0):
    EBREAK
    PseudoRET
```

如果这里还是 `ADJCALLSTACKDOWN`/`PseudoCALL abort`，说明 `TRAP` 仍然被 `Expand`；如果这里直接崩，说明 `Legal` 生效了但 pattern 缺失。

### Step 7：验证 MC 编码

```console
$ printf 'ebreak\n' > /tmp/practice-b-ebreak.s
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/practice-b-ebreak.s
	ebreak                                  # encoding: [0x73,0x00,0x10,0x00]
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/practice-b-ebreak.s -o /tmp/practice-b-ebreak.o
$ $B/llvm-objdump -d /tmp/practice-b-ebreak.o
0000000000000000 <.text>:
       0: 00100073     	ebreak
```

如果 `llc` 输出 `ebreak` 但 `llvm-mc` 不认识，说明你加了 `isCodeGenOnly`；如果 `llvm-mc` 认识但 `llc` 不选，说明合法化或 pattern 有问题。

### Step 8：在 `/tmp` 写 CodeGen lit 测试

沿用练习 A 的 `/tmp/practice-lit/` 配置，新增：

```llvm
; /tmp/practice-lit/practice-b.ll
; RUN: %llc -mtriple=minirv64 -O2 < %s | %FileCheck %s
; RUN: %llc -mtriple=minirv64 -O2 -stop-after=finalize-isel < %s | %FileCheck %s --check-prefix=MIR

define void @t() {
  call void @llvm.trap()
  ret void
}

declare void @llvm.trap() noreturn nounwind

; CHECK-LABEL: t:
; CHECK: ebreak
; CHECK-NOT: jal x1, abort
; MIR: EBREAK
```

```console
$ $B/llvm-lit -v /tmp/practice-lit/practice-b.ll
-- Testing: 1 tests, 1 workers --
PASS: practice-ab :: practice-b.ll (1 of 1)

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

`CHECK-NOT: jal x1, abort` 是核心。它不只检查“有 ebreak”，还检查“旧的库调用没有回来”。

### Step 9：跑现有套件，处理 `libm-calls.ll`

```console
$ T=/home/ubuntu/llvm/trees/dsl-tablegen/llvm/test
$ $B/llvm-lit -q $T/CodeGen/MiniRV $T/MC/MiniRV
-- Testing: 53 tests, 32 workers --
FAIL: LLVM :: CodeGen/MiniRV/libm-calls.ll (14 of 53)
Total Discovered Tests: 53
  Failed: 1 (1.89%)
```

失败原因不是编码错误，而是测试期望过期。`libm-calls.ll` 里有一段：

```llvm
; llvm.trap must not fall through.
; CHECK-LABEL: trap:
; CHECK:         jal     x1, abort
define void @trap() {
  call void @llvm.trap()
  unreachable
}
```

它在 MiniRV 还没有 trap 指令时，锁的是 `abort` 行为。现在 `llvm.trap()` 合法地变成 `ebreak`，这一行必须更新。`llvm-lit` 会指出：

```text
llvm/test/CodeGen/MiniRV/libm-calls.ll:96:10: error: CHECK: expected string not found in input
; CHECK: jal x1, abort
```

在 `/tmp` 复制一份并更新期望：

```console
$ sed -e 's/^; RUN: llc /; RUN: %llc /' \
      -e 's/| FileCheck /| %FileCheck /' \
      -e 's/^; RUN: llvm-objdump /; RUN: %llvm-objdump /' \
      -e 's/^; CHECK:         jal     x1, abort/; CHECK:         ebreak/' \
      /home/ubuntu/llvm/trees/dsl-tablegen/llvm/test/CodeGen/MiniRV/libm-calls.ll \
      > /tmp/practice-lit/libm-calls-updated.ll
$ $B/llvm-lit -v /tmp/practice-lit/libm-calls-updated.ll
PASS: practice-ab :: libm-calls-updated.ll (1 of 1)
  Passed: 1 (100.00%)
```

其余 52 个测试（48 个 CodeGen + 4 个 MC）全部通过：

```console
$ $B/llvm-lit -q $(cat /tmp/practice-lit/existing.txt)
-- Testing: 52 tests, 32 workers --

Total Discovered Tests: 52
exit=0
```

结论要写清楚：**这不是未预期的回归，而是一个需要同步更新的行为断言**。仓库里的测试文件在本练习中不允许修改；提交时应该把 `jal x1, abort` 改成 `ebreak`。

### Step 10：跑 e2e harness

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
  QEMU=/usr/bin/qemu-riscv64-static \
  llvm/lib/Target/MiniRV/dev/e2e/run.sh > /tmp/practice-ab-e2e.log 2>&1; echo $?
0
$ grep -c '^OK' /tmp/practice-ab-e2e.log
17
```

### Step 11：保存 patch、回滚、重新构建、复验

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen
$ git diff -- llvm/lib/Target/MiniRV/MiniRVInstrInfo.td \
    llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp > /tmp/bringup/practice-ab.patch
$ git checkout -- llvm/lib/Target/MiniRV/MiniRVInstrInfo.td \
    llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp
$ git status --short
?? ana-docs/
$ cd /home/ubuntu/llvm/builds/dsl-tablegen && ninja llc llvm-mc
[40/41] Linking CXX executable bin/llc
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o - | grep -E 'ebreak|abort'
	jal	x1, abort
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/practice-b-ebreak.s
/tmp/practice-b-ebreak.s:1:1: error: invalid instruction mnemonic
ebreak
^
```

`llc` 回到 `abort`，`llvm-mc` 回到拒绝，`git status` 只剩 `ana-docs/`。构建产物和源码都回到基线。

## 参考解：完整补丁与真实验证

<details>
<summary>展开：EBREAK 记录、TRAP 合法化、pattern、测试、失败对照与 RISC-V 对照</summary>

### 1. 拆分后的精确 patch

完整实验补丁在 `/tmp/bringup/practice-ab.patch`；练习 B 的独立 patch 在 `/tmp/bringup/practice-b-ebreak.patch`。下面是后者（从完整补丁中去掉 `FENCE`，注释略作调整以便单独阅读）：

```diff
diff --git a/llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp b/llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp
index 56d66ff0c356..19e7cc952f3e 100644
--- a/llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp
+++ b/llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp
@@ -150,6 +150,10 @@ MiniRVTargetLowering::MiniRVTargetLowering(const TargetMachine &TM,
   // the DAG legalizer emit its generic __sync_synchronize() call.
   setMaxAtomicSizeInBitsSupported(0);
   setOperationAction(ISD::ATOMIC_FENCE, MVT::Other, Expand);
+  // llvm.trap() is a real instruction on MiniRV.  Marking ISD::TRAP legal
+  // stops the legalizer from expanding it into an abort() libcall, and lets
+  // the Pat<(trap), (EBREAK)> in MiniRVInstrInfo.td select it.
+  setOperationAction(ISD::TRAP, MVT::Other, Legal);
 
   computeRegisterProperties(STI.getRegisterInfo());
 }
diff --git a/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td b/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
index 0dd9138cc108..3f8832fe05db 100644
--- a/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
+++ b/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
@@ -112,6 +112,29 @@ def simm21_lsb0_bb : MiniRVTargetOperand {
 
 include "auto-td/AutoTdInstrInfo.inc"
 
+//===----------------------------------------------------------------------===//
+// Hand-written system instructions
+//===----------------------------------------------------------------------===//
+//
+// The auto-td corpus does not contain EBREAK.  It is a fixed encoding with no
+// operands, so every bit is written out explicitly.
+
+def EBREAK : MiniRVInst<(outs), (ins), "ebreak", "", [], InstFormatI> {
+  let AsmString = "ebreak";
+  let Inst{31-20} = 0x001;
+  let Inst{19-15} = 0;
+  let Inst{14-12} = 0;
+  let Inst{11-7} = 0;
+  let Inst{6-2} = 0b11100;
+  let Inst{1-0} = 0b11;
+  let hasSideEffects = 1;
+  let hasNoSchedulingInfo = 1;
+}
+
+// llvm.trap() reaches SelectionDAG as ISD::TRAP.  The lowering marks that node
+// legal, so the only way it can become code is this pattern.
+def : Pat<(trap), (EBREAK)>;
+
 //===----------------------------------------------------------------------===//
 // Call frame pseudos
 //===----------------------------------------------------------------------===//
```

### 2. 生成物与真实前后对照

`MiniRVGenDAGISel.inc` 里的匹配分支：

```text
/*   1297*/ /*SwitchOpcode*/ 6, TARGET_VAL(ISD::TRAP), // ->1306
/*   1300*/  OPC_RecordNode, // #0 = 'trap' chained node
/*   1301*/  OPC_EmitMergeInputChains1_0,
/*   1302*/  OPC_MorphNodeTo0Chain, TARGET_VAL(MiniRV::EBREAK),
                  0/*#Ops*/, // Ops = None
             // Src: (trap) - Complexity = 3
             // Dst: (EBREAK)
```

`MiniRVGenInstrInfo.inc` 里的 opcode 和标志：

```text
    EBREAK                                = 361, // MiniRVInstrInfo.td:134
    { 361,	0,	0,	4,	0,	0,	0,	MiniRVOpInfoBase + 1,	0,	0|(1ULL<<MCID::UnmodeledSideEffects)|... },  // EBREAK
```

`MiniRVGenAsmMatcher.inc` / `MiniRVGenAsmWriter.inc` / `MiniRVGenDisassemblerTables.inc`：

```text
  { 81 /* ebreak */, MiniRV::EBREAK, Convert_NoOperands, AMFBS_None, {  }, },
  /* 680 */ "ebreak\000"
  OPC_Decode, 233, 2, 1,                     // 518:  decode to EBREAK using decoder 1
```

基线见 Step 0（`jal x1, abort` + 栈帧）。改动后：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o - | sed -n '7,9p'
# %bb.0:
	ebreak
	jalr	x0, 0(x1)
```

### 3. 只加 Legal、不加 pattern 的真实失败

把 pattern 删掉、只保留 `setOperationAction(ISD::TRAP, MVT::Other, Legal)`，重新构建后运行：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o -
LLVM ERROR: Cannot select: t1: ch = trap t0
In function: t
...
$ echo $?
134
```

`t1: ch = trap t0` 就是 DAG 里的节点：`t0` 是输入 chain，`t1` 是输出 chain。合法化器把它留下了，但 ISel 找不到 `(trap)` 的 pattern。**`Legal` 和 `Pat` 必须同时存在**。反过来，只加 pattern、不加 Legal，`llc` 仍然输出 `abort`，因为 `TRAP` 在合法化阶段已经被展开。

### 4. `/tmp` CodeGen 测试与 RISC-V 对照

测试文件见 Step 8；真实输出：

```console
$ $B/llvm-lit -v /tmp/practice-lit/practice-b.ll
PASS: practice-ab :: practice-b.ll (1 of 1)
  Passed: 1 (100.00%)
```

RISC-V 的合法化与 pattern：

```cpp
setOperationAction({ISD::TRAP, ISD::DEBUGTRAP}, MVT::Other, Legal);
```

```tablegen
def : Pat<(trap), (UNIMP)>;
def : Pat<(debugtrap), (EBREAK)>;
```

真实输出：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/practice-b-trap-unreachable.ll -o - | sed -n '10,12p'
# %bb.0:
	unimp
.Lfunc_end0:
```

RISC-V 的 `EBREAK` 还带 `isTrap = 1`；MiniRV 的题目只要求 `hasSideEffects`/`hasNoSchedulingInfo`。生产后端应补 `isTrap` 并测试依赖 `MCInstrDesc::isTrap()` / `MCID::Trap` 的路径。标为 **guided**，因为本练习没有覆盖这些路径。

### 5. 清理与最终状态

```console
$ cd /home/ubuntu/llvm/trees/dsl-tablegen && git status --short
?? ana-docs/
$ $B/llc -mtriple=minirv64 -O2 /tmp/practice-b-trap.ll -o - | grep -E 'ebreak|abort'
	jal	x1, abort
```

源码、构建产物、测试行为都回到基线；patch 在 `/tmp/bringup/practice-ab.patch`。

</details>

## 常见坑（错误直觉 → 正确认识）

1. **错误直觉**：加了 `EBREAK` 记录，`llc` 就会把 `llvm.trap()` 变成 `ebreak`。  
   **正确认识**：记录只让 MC 层认识 `ebreak`。CodeGen 还缺两件事：`ISD::TRAP` 的合法化动作和 `(trap)` 的 pattern。基线 `llc` 输出 `jal x1, abort`，就是这两件事都没做的结果。

2. **错误直觉**：把 `ISD::TRAP` 设成 `Legal` 就够了。  
   **正确认识**：合法化器会把 `TRAP` 留在 DAG 里，但 ISel 找不到 pattern，直接报 `Cannot select: t1: ch = trap t0`，退出码 134。`Legal` 只是“不要展开”，不是“知道怎么选”。

3. **错误直觉**：只加 `Pat<(trap), (EBREAK)>` 就够了。  
   **正确认识**：`TRAP` 在合法化阶段已经被展开成 `abort()` 调用，pattern 根本没有机会匹配。你会看到 `llc` 仍然输出 `jal x1, abort`，但 `.td` 里明明有 pattern。必须同时加 `Legal`。

4. **错误直觉**：`setOperationAction` 的第二个参数写成 `MVT::i64` 更自然。  
   **正确认识**：`ISD::TRAP` 是 chain 节点，没有值类型，必须写 `MVT::Other`。写错类型，action 不会作用在 `TRAP` 上。

5. **错误直觉**：`llvm.trap()` 是 `noreturn`，所以 `ret void` 会被自动删掉。  
   **正确认识**：`llc` 的 codegen 级别不做 IR 级死代码消除。IR 里写了 `ret void`，最终 asm 就会有 `jalr x0, 0(x1)`。把 IR 改成 `unreachable`，`jalr` 才会消失。

6. **错误直觉**：`libm-calls.ll` 失败说明后端有回归。  
   **正确认识**：它钉的是旧行为 `jal x1, abort`。`llvm.trap()` 现在合法地变成 `ebreak`，这一行必须同步更新。在 `/tmp` 副本上改成 `ebreak` 后通过，其余 52 个测试也通过。

7. **错误直觉**：`isTrap = 1` 只是装饰，`isCodeGenOnly = 1` 只是“给 CodeGen 用”。  
   **正确认识**：RISC-V 给 `EBREAK` 加 `isTrap`，它影响依赖 `MCInstrDesc::isTrap()` / `MCID::Trap` 的路径；本练习按题目没加，生产后端应补（guided）。而 `isCodeGenOnly = 1` 会把记录从汇编器 matcher 里藏起来，`llvm-mc` 就不认识 `ebreak`；`EBREAK` 必须同时出现在 MC 和 CodeGen 两条路径里。

## 自我检核

- [ ] 不看笔记，画出 `llvm.trap()` 从 IR 到 `ebreak` 的路径，并标出合法化和指令选择的分界点。
- [ ] 解释为什么“只加 Legal”会得到 `Cannot select`，而“只加 pattern”会得到 `abort`。
- [ ] 说出 `setOperationAction` 的第二个参数为什么是 `MVT::Other`，写成 `MVT::i64` 会发生什么。
- [ ] 解释 `Pat<(trap), (EBREAK)>` 里没有写 chain，但生成的代码为什么有 `OPC_EmitMergeInputChains1_0`。
- [ ] 比较 RISC-V 的 `trap → UNIMP` 和 MiniRV 的 `trap → EBREAK`：各自的设计理由是什么？
- [ ] 解释 `ret void` 和 `unreachable` 对最终 asm 的差别，以及这个差别发生在哪一层。
- [ ] 如果 `libm-calls.ll` 失败，你如何判断它是“测试过期”还是“真的回归”？给出至少两条证据。

## 延伸阅读

- **[LLVM SelectionDAG](https://llvm.org/docs/CodeGenerator.html#selectiondag)** — LLVM 官方文档  
  - **读哪里**：`SelectionDAG` 一节，重点看 legalization 和 instruction selection 的先后关系。  
  - **能学到什么**：为什么 `setOperationAction` 在 ISel 之前生效，以及 `Legal`/`Expand`/`Custom` 的区别。  
  - **前提**：读过本课程 Ch 12 和 Ch 13。

- **[LLVM TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)** — LLVM 官方文档  
  - **读哪里**：`Pattern`、`Pat`、`SDNode` 和 `Instruction` 的交互部分。  
  - **能学到什么**：`(trap)` 为什么能匹配 `ISD::TRAP`，以及 chain 是如何由生成器接线的。  
  - **前提**：能看懂基本的 TableGen 记录和 dag 类型。

- **[LLVM LangRef: llvm.trap](https://llvm.org/docs/LangRef.html#llvm-trap-intrinsic)** — LLVM 官方文档  
  - **读哪里**：`llvm.trap` 的语义、`noreturn` 属性和它与 `unreachable` 的区别。  
  - **能学到什么**：为什么 IR 里可以同时有 `call @llvm.trap()` 和 `ret`，以及为什么最终 asm 会保留 `jalr`。  
  - **前提**：了解基本的 LLVM IR 语法。

- **[RISC-V backend 课程](../riscv_backend/README.md)** — 本仓库  
  - **读哪里**：合法化、指令选择和 trap/debugtrap 相关章节。  
  - **能学到什么**：成熟后端如何组织 `setOperationAction`、`Pat` 和 `isTrap` 标志。  
  - **前提**：先完成本练习，带着 MiniRV 的最小实现去对照。

→ [练习 C — 端到端跑通一个程序](./practice-c-end-to-end.md)
