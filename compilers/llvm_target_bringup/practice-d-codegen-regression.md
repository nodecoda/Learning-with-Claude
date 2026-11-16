# 练习 D — 定位一个 CodeGen 回归

> **目标**：拿到一份“大栈帧 / 帧索引”坏掉的 IR，用 `-debug-only`、`-stop-after`、MIR、`llvm-objdump` 和 lit 把故障钉到具体 pass，而不是靠读 diff 猜。

> **环境**：LLVM 24.0.0git（`6e7e7ab`），`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。本章的“修复前 / 修复后”来自同一棵 scratch worktree 分别检出 `9f5ca73`（`d93cc60^`）与 `d93cc60` 后重建，命令见 Step 0。

## 背景与动机

MiniRV 历史上有一个很典型的回归：提交 `d93cc60` "[MiniRV] Support stack frames larger than 2047 bytes"。它的父提交 `9f5ca73` 有 11 个 CodeGen lit 测试、也能给寄存器溢出分配栈槽，但没有一个测试使用 IR `alloca`；**任何真正使用 `alloca`（地址没被 IR 优化掉）的函数都过不了 ISel**：

```console
$ $OLD/llc -mtriple=minirv64 -O2 small-frame.ll -o - 2>&1 | head -2
LLVM ERROR: Cannot select: t3: i64 = FrameIndex<0>
In function: small_frame
```

注意这个函数只有 16 字节栈帧，所以不是“帧太大”，而是**帧索引本身没有被选择**。大栈帧的问题被压在第二层：只把 ISel 补好，2400 字节的帧会换一个 pass 继续报错。

这个回归的表象是编译失败，不是静默的 miscompile；但定位方法和真正的 wrong-code 回归完全一样：**找到第一个出错的层**。边界一节还会看到，如果只删掉 prologue 里的检查，后端确实会静默生成错误指令（`addi sp, sp, 1696`），所以“让它编过”不等于“修对了”。

为什么不能直接看 `git show d93cc60`？因为提交同时改了三层：ISel 选择 `FrameIndex`、PEI 物化大栈调整、`eliminateFrameIndex` 处理大位移。三层各自都能报出看似合理的错误；不按层截断，你只会看到最后一个 pass 的抱怨。

| 提交 | 做了什么 | 留下的问题 |
|---|---|---|
| `9f5ca73` | 机器级 spill、固定栈参数、调用 | IR `alloca` 地址作为值出现时，ISel 无法选择 `ISD::FrameIndex` |
| `d93cc60` | 选裸 `FrameIndex`；`adjustReg` 物化大调整；`eliminateFrameIndex` + 虚拟基址 | 三条 `lui/addi/add`，代码偏大 |
| `90e179c` | 大调整拆成两条 `addi` | 2400 字节帧变成 `-2048; -352` |
| `6e7e7ab` | 大帧在 callee-saved spill 两侧拆开 | 当前 HEAD 的形态 |

## 先建立直觉

把 `llc` 拆成五个观察点，每个观察点对应一个“谁该负责”的问题：

```text
  SelectionDAGISelPass ─► finalize-isel ─► [localstackalloc] ─► greedy
       (选指)                (MIR)             (基址寄存器)        (regalloc)
               │                                                   │
               ▼                                                   ▼
         prolog-epilog (PEI: 帧布局 + prologue/epilogue + eliminateFrameIndex)
               │
               ▼
         AsmPrinter ─► asm 文本 ─► MC 编码 ─► .o ─► ld.lld ─► qemu
```

| 观察点 | 问题 | 出问题时的典型符号 |
|---|---|---|
| ISel | `FrameIndex` 这种 DAG 节点有没有 pattern？ | `MiniRVISelDAGToDAGISel::Select` |
| `finalize-isel` | 选指后的 MIR 是否自洽？ | TableGen pattern / `Select` |
| `prolog-epilog` | 帧多大？sp 怎么调？spill 偏移是否可达？ | `MiniRVFrameLowering::emitPrologue`、`eliminateFrameIndex` |
| regalloc | 地址/基址落在哪个物理寄存器？scratch 会不会被占用？ | `getReservedRegs`、LocalStackSlotAllocation 虚寄存器 |
| MC / objdump | 汇编文本和真实编码是否一致？ | `MiniRVMCCodeEmitter`、立即数字段 |

关键直觉：**MIR 在 PEI 之前 `stackSize` 还是 0**。栈帧大小是 PEI 算出来的，不是 ISel 算的；所以在 `finalize-isel` 里找“帧为什么这么大”永远找不到。

## 任务规格

### 输入

在 `/tmp/minirv-repro` 下建三个文件（`volatile` 只是防止 `-O2` 把 alloca 优化掉，不影响后端路径）：

```llvm
; small-frame.ll：16 字节帧，但 alloca 的地址被当作值使用
define i64 @small_frame(i64 %n) {
entry:
  %a = alloca [2 x i64], align 8
  store volatile i64 %n, ptr %a, align 8
  %v = load volatile i64, ptr %a, align 8
  ret i64 %v
}
```

```llvm
; large-frame.ll：2400 字节帧，超过 addi 的 2047 上限
define i64 @large_frame(i64 %n) {
entry:
  %a = alloca [300 x i64], align 8
  store volatile i64 %n, ptr %a, align 8
  %v = load volatile i64, ptr %a, align 8
  ret i64 %v
}
```

```llvm
; far-object.ll：load 位于对象内 2392 字节处，超出 12 位位移
define i64 @far_object(i64 %n) {
entry:
  %a = alloca [300 x i64], align 8
  store volatile i64 %n, ptr %a, align 8
  %p = getelementptr inbounds [300 x i64], ptr %a, i64 0, i64 299
  %v = load volatile i64, ptr %p, align 8
  ret i64 %v
}
```

### 约束与验收

- 只改 `llvm/lib/Target/MiniRV`，不碰 RISC-V 后端和公共 CodeGen。
- 不允许用“把 `-O2` 换成 `-O0`”或“删掉检查”绕过问题；要能解释每个 pass 的输入输出。
- 修复后小帧、2400 字节帧、对象内 2392 字节位移、带 callee-saved spill 的函数都要能编过。
- 交付：三个文件在父提交上的“第一个失败 pass”+ 命令输出；根因符号；修复 diff；修复后 asm/objdump；一个 lit 测试；以及“只删 guard 会怎样”的证据。

## 期望输出

一张表回答“谁该背锅”：

| 输入 | 父提交症状 | 第一个失败 pass | 根因符号 |
|---|---|---|---|
| `small-frame.ll` | `Cannot select: FrameIndex` | `SelectionDAGISelPass` | `MiniRVISelDAGToDAGISel::Select` 缺 `ISD::FrameIndex` |
| `large-frame.ll` | 同上；补 ISel 后变成 `frames must fit` | `prolog-epilog`（PEI） | `MiniRVFrameLowering::emitPrologue` |
| `far-object.ll` | 同上；补 ISel 后同大帧；最终走 scratch | `prolog-epilog` + `eliminateFrameIndex` | `MiniRVRegisterInfo::eliminateFrameIndex` |

诊断报告固定五行，逼自己写清楚：

```text
症状：
第一个失败 pass：
证据（命令 + 关键输出）：
根因（符号 + 假设）：
修复 + 测试：
```

## 卡住了的提示

1. **`Cannot select` 先看 DAG，不要先改 TableGen。** `FrameIndex` 和 `TargetFrameIndex` 是两个节点；MiniRV 的 load/store pattern 只匹配后者（`tframeindex`）。
2. **`-stop-after=finalize-isel` 没有输出，不代表 flag 写错。** 进程在 ISel 里 abort，根本没走到 `finalize-isel`；先在报错栈里找 `SelectionDAGISel::CannotYetSelect`。
3. **PEI 之前 `stackSize: 0` 是正常的。** 看 `localFrameSize` 和 `stack:` 里的对象；最终帧大小要等 `prolog-epilog` 之后。
4. **`-debug-only` 只在 assertions 构建里可用，输出走 stderr。** 调试时配 `-o /dev/null`，否则 asm 和 debug 输出混在一起。
5. **汇编文本不校验立即数范围。** `llc -o -` 可以打印 `addi x2, x2, -2400`，编码器会把它截成 12 位；必须用 `-filetype=obj` + `llvm-objdump -d` 看真实编码。

## 分段实作步骤

### Step 0：准备一个可切换的 scratch 构建

不想重建也可以，用 `git show d93cc60` 读源码，把 before 标成 `source-derived`。要实测就建一次：

```console
$ git -C /home/ubuntu/llvm/trees/dsl-tablegen worktree add --detach /tmp/minirv-before d93cc60^
$ cmake -G Ninja /tmp/minirv-before/llvm -B /tmp/minirv-before-build \
    -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON \
    -DLLVM_TARGETS_TO_BUILD=MiniRV -DLLVM_ENABLE_PROJECTS= \
    -DLLVM_USE_LINKER=lld -DPython3_EXECUTABLE=/usr/bin/python3
$ ninja -C /tmp/minirv-before-build llc llvm-mc llvm-objdump FileCheck not llvm-config count
```

后文用到的变量：

```console
$ B=/home/ubuntu/llvm/builds/dsl-tablegen/bin     # 当前 HEAD（6e7e7ab）
$ OLD=/tmp/minirv-before-build/bin                # 检出 9f5ca73 时构建
$ INT=/tmp/minirv-before-build/bin                # 再加 ISel 补丁的中间态
$ FIX=/tmp/minirv-before-build/bin                # 切到 d93cc60 后重建
```

后三个变量指向同一路径，只是构建时的源码状态不同；作者依次重建并保存了每段输出。`-DPython3_EXECUTABLE` 不是装饰：MiniRV 的 auto-td 生成器 `import yaml`，没装 PyYAML 的 Python 会让构建在第一步失败。

### Step 1：先确认“小帧也挂”，把范围缩到帧索引

```console
$ $OLD/llc -mtriple=minirv64 -O2 small-frame.ll -o - 2>&1 | head -2
LLVM ERROR: Cannot select: t3: i64 = FrameIndex<0>
In function: small_frame
```

16 字节帧也失败，说明不是 2047 上限的问题；`large-frame.ll` 报的是同一句（`In function: large_frame`）。结论：第一层坏在 **ISel**，而且是 `FrameIndex` 这个节点本身。

### Step 2：用 `-debug-only=isel` 看节点形状

```console
$ $OLD/llc -mtriple=minirv64 -O2 -debug-only=isel large-frame.ll -o /dev/null 2>&1 \
    | sed -n '/Initial selection DAG/,/t7/p'
Initial selection DAG: %bb.0 'large_frame:entry'
SelectionDAG has 11 nodes:
    t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64 = Constant<0>
    t6: ch = store<(volatile store (s64) into %ir.a)> t2:1, t2, FrameIndex:i64<0>, poison:i64
  t7: i64,ch = load<(volatile dereferenceable load (s64) from %ir.a)> t6, FrameIndex:i64<0>, poison:i64
```

`t6`/`t7` 的地址操作数是 `FrameIndex:i64<0>`，不是 `TargetFrameIndex`。再看 `.td`：

```tablegen
def : Pat<(i64 (load tframeindex:$fi)), (LD tframeindex:$fi, 0)>;
def : Pat<(store GPR:$val, tframeindex:$fi), (SD GPR:$val, tframeindex:$fi, 0)>;
```

`tframeindex` 是 `ISD::TargetFrameIndex`（`TargetSelectionDAG.td` 里的 SDNode）。目标只在 lowering 栈参数、创建 fixed object 和机器级 spill 时自己造这个节点；IR alloca 的地址作为值出现时是普通 `ISD::FrameIndex`，没有 pattern 可匹配。

### Step 3：用 `-stop-after=finalize-isel` 证明“没走到那里”

```console
$ $OLD/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel large-frame.ll -o - 2>&1 | head -2
LLVM ERROR: Cannot select: t3: i64 = FrameIndex<0>
In function: large_frame
```

如果 `finalize-isel` 能跑完，它会打印一份 `--- |` MIR；这里没有，说明故障发生在它之前。第一个失败的 pass 是 `SelectionDAGISelPass`。

### Step 4：只补 ISel，把第二层逼出来

只把 `d93cc60` 的 `MiniRVISelDAGToDAG.cpp` 那 15 行搬过来（裸 `FrameIndex` → `ADDI fi, 0`），重建：

```console
$ git -C /tmp/minirv-before checkout d93cc60 -- \
    llvm/lib/Target/MiniRV/MiniRVISelDAGToDAG.cpp
$ ninja -C /tmp/minirv-before-build llc
$ $INT/llc -mtriple=minirv64 -O2 small-frame.ll -o - | sed -n '/addi.*-16/,/sd/p'
	addi	x2, x2, -16
	addi	x3, x2, 0
	sd	x10, 0(x3)
```

小帧通了，`addi x3, x2, 0` 就是新加的路径。大帧换了个死法：

```console
$ $INT/llc -mtriple=minirv64 -O2 large-frame.ll -o - 2>&1 | head -2
LLVM ERROR: MiniRV frames must fit in a 12-bit immediate
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ and include the crash backtrace and instructions to reproduce the bug.
```

但这次 MIR 是完整的：

```console
$ $INT/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel large-frame.ll -o - \
    | sed -n '/^body:/,$p' | sed '$d' | sed '/^[[:space:]]*$/d'
body:             |
  bb.0.entry:
    liveins: $x10
    %0:gpr = COPY $x10
    %1:gpr = ADDI %stack.0.a, 0
    SD %0, %1, 0 :: (volatile store (s64) into %ir.a)
    %2:gpr = LD %1, 0 :: (volatile dereferenceable load (s64) from %ir.a)
    $x10 = COPY %2
    PseudoRET implicit $x10
```

再用 `-stop-after=prolog-epilog` 截断：

```console
$ $INT/llc -mtriple=minirv64 -O2 -stop-after=prolog-epilog large-frame.ll -o - 2>&1 | head -1
LLVM ERROR: MiniRV frames must fit in a 12-bit immediate
```

证据链闭合：ISel 产出合法 MIR；`prolog-epilog` 一进去就 abort。第二个失败 pass 是 PEI，根因在 `MiniRVFrameLowering`。顺手确认一个常见误会：

```console
$ $INT/llc -mtriple=minirv64 -O2 -stop-before=prolog-epilog large-frame.ll -o - \
    | grep -E "stackSize|localFrameSize"
  stackSize:       0
  localFrameSize:  2400
```

PEI 之前 `stackSize` 就是 0，帧是在 PEI 里算出来的。

### Step 5：读源码 + 完整修复 + 验证

父提交的 `emitPrologue` 只有一条 `addi`，并在 `!isInt<12>(StackSize)` 时 `report_fatal_error`；`eliminateFrameIndex` 则把 `对象偏移 + StackSize + 位移` 直接写进操作数。两处都假设“栈帧和对象偏移一定塞得进 12 位”。完整修复见 `<details>`；验证用修复提交的构建：

```console
$ $FIX/llc -mtriple=minirv64 -O2 large-frame.ll -o - | sed -n '/^large_frame:/,/jalr/p'
large_frame:                            # @large_frame
	.cfi_startproc
	lui	x6, -1
	addi	x6, x6, 1696
	add	x2, x6, x2
	addi	x3, x2, 0
	sd	x10, 0(x3)
	ld	x10, 0(x3)
	lui	x6, 1
	addi	x6, x6, -1696
	add	x2, x6, x2
	jalr	x0, 0(x1)
```

对象内的大位移走 `far-object.ll`：

```console
$ $FIX/llc -mtriple=minirv64 -O2 far-object.ll -o - | sed -n '/lui.*x4/,/ld/p'
	lui	x4, 1
	addi	x4, x4, -1704
	add	x3, x3, x4
	ld	x10, 0(x3)
```

`lui x4; addi x4, x4, -1704` 是在物化 `2392 = 4096 - 1704`，再 `add x3, x3, x4`；这是 ISel 对超范围 ADD 常量的处理，不是帧调整。最后用目标文件和 lit 收口：

```console
$ $FIX/llc -mtriple=minirv64 -O2 -filetype=obj far-object.ll -o /tmp/far.o
$ $B/llvm-objdump -d /tmp/far.o | sed -n '5,12p'

0000000000000000 <far_object>:
       0: fffff337     	lui	t1, 0xfffff
       4: 6a030313     	addi	t1, t1, 0x6a0
       8: 00230133     	add	sp, t1, sp
       c: 00010193     	mv	gp, sp
      10: 00a1b023     	sd	a0, 0x0(gp)
      14: 00001237     	lui	tp, 0x1
```

`mv gp, sp` 是 `addi x3, x2, 0` 的 ABI 别名，不要以为多了一条指令；`0x6a0` 就是 1696。

## 完整参考解答

<details>
<summary>点开：症状 → pass → 根因 → 修复 → 测试</summary>

### 1. 结论先行

| 层 | 症状 | 证据 | 根因 | 修复 |
|---|---|---|---|---|
| ISel | `Cannot select: FrameIndex` | `-debug-only=isel` 的 DAG | `Select` 只认 `TargetFrameIndex` | 裸 `FrameIndex` → `ADDI fi, 0` |
| PEI / frame | `MiniRV frames must fit in a 12-bit immediate` | `-stop-after=prolog-epilog` | `emitPrologue` 用一条 `addi` | `adjustReg` 物化 `lui+addi+add` |
| PEI / frame index | 大对象地址/大位移 | `far-object.ll` 的 scratch 序列 | `eliminateFrameIndex` 直接写 12 位位移 | 大偏移走 `adjustReg`，load/store 用 `X5`、位移清零 |
| LSSA / regalloc | 基址寄存器可能被占用 | `-debug-only=regalloc` 的 `AllocationOrder` | scratch 没保留 | 保留 `X5`、`X6` |

### 2. 证据链

父提交上，两条 `-stop-after` 都死在 ISel 之前（见 Step 3/4）——`-stop-after` 不是“跳过前面的 pass”，而是“跑到这里才停”。补 ISel 后，`finalize-isel` 能打印出带 `ADDI %stack.0.a, 0` 的 MIR（见 Step 4），而 `prolog-epilog` 换成栈帧错误。修复后 PEI 之后的 MIR 是：

```console
$ $FIX/llc -mtriple=minirv64 -O2 -stop-after=prolog-epilog large-frame.ll -o - \
    | sed -n '/^body:/,$p' | sed '$d' | sed '/^[[:space:]]*$/d'
body:             |
  bb.0.entry:
    liveins: $x10
    $x6 = frame-setup LUI -1
    $x6 = frame-setup ADDI $x6, 1696
    $x2 = frame-setup ADD $x6, $x2
    $x3 = ADDI $x2, 0
    SD killed $x10, $x3, 0 :: (volatile store (s64) into %ir.a)
    $x10 = LD killed $x3, 0 :: (volatile dereferenceable load (s64) from %ir.a)
    $x6 = frame-destroy LUI 1
    $x6 = frame-destroy ADDI $x6, -1696
    $x2 = frame-destroy ADD $x6, $x2
    PseudoRET implicit $x10
```

`frame-setup` / `frame-destroy` 告诉后面的 pass 这些指令属于 prologue/epilogue，不能随便搬。

### 3. 根因：三个“12 位”假设

1. **ISel**：内存 pattern 只写了 `tframeindex`（`ISD::TargetFrameIndex`）。IR alloca 的地址作为值出现时是 `ISD::FrameIndex`，要么手动选成 `ADDI fi, 0`，要么永远 `Cannot select`。
2. **`emitPrologue` / `emitEpilogue`**：整个帧只用一条 `addi`。立即数是 `simm12`（`-2048..2047`），2400 字节直接越界。
3. **`eliminateFrameIndex`**：`Offset` 算出来后直接 `ChangeToImmediate`，默认它一定落在指令的 12 位位移字段里。对象在帧内 2392 字节处时，`sd/ld` 的位移就炸了。

三者必须同时成立。只修第 1 条，第 2 条报错；只放宽第 2 条的检查，MC 层会把 `-2400` 截成 `+1696`（见“边界”）。

### 4. 修复（`git show d93cc60` 的关键 hunk；注释与换行有压缩）

ISel 里接住裸帧索引：

```cpp
  // A frame index used as a value is the address of a stack object.  It
  // becomes "addi <fi>, 0"; PEI resolves the frame index against the stack
  // pointer and materialises a large offset in a register.
  if (N->getOpcode() == ISD::FrameIndex) {
    SDLoc DL(N);
    EVT VT = N->getValueType(0);
    SDValue FI = CurDAG->getTargetFrameIndex(
        cast<FrameIndexSDNode>(N)->getIndex(), VT);
    ReplaceNode(N, CurDAG->getMachineNode(
                       MiniRV::ADDI, DL, VT, FI,
                       CurDAG->getTargetConstant(0, DL, MVT::i64)));
    return;
  }
```

`MiniRVFrameLowering::adjustReg` 负责“任何栈调整”：

```cpp
void MiniRVFrameLowering::adjustReg(MachineBasicBlock &MBB,
                                    MachineBasicBlock::iterator II,
                                    const DebugLoc &DL, Register DestReg,
                                    Register SrcReg, int64_t Val,
                                    MachineInstr::MIFlag Flag) const {
  if (DestReg == SrcReg && Val == 0)
    return;
  if (isInt<12>(Val)) {
    BuildMI(MBB, II, DL, TII.get(MiniRV::ADDI), DestReg)
        .addReg(SrcReg).addImm(Val).setMIFlag(Flag);
    return;
  }
  assert(isInt<32>(Val) && "MiniRV cannot materialise a larger adjustment");
  // lui would clobber a source that is also the destination, so build the
  // constant in a scratch register in that case.
  int64_t Hi20 = (Val + 0x800) >> 12;
  int64_t Lo12 = Val - (Hi20 << 12);
  Register Scratch = DestReg == SrcReg ? Register(MiniRV::X6) : DestReg;
  BuildMI(MBB, II, DL, TII.get(MiniRV::LUI), Scratch)
      .addImm(Hi20).setMIFlag(Flag);
  BuildMI(MBB, II, DL, TII.get(MiniRV::ADDI), Scratch)
      .addReg(Scratch).addImm(Lo12).setMIFlag(Flag);
  BuildMI(MBB, II, DL, TII.get(MiniRV::ADD), DestReg)
      .addReg(Scratch).addReg(SrcReg).setMIFlag(Flag);
}
```

prologue / epilogue 从“一条 `addi`”变成“调用 `adjustReg`”：

```cpp
  if (!isInt<32>(StackSize))
    report_fatal_error("MiniRV frames must fit in a 32-bit immediate");
  adjustReg(MBB, MBB.begin(), DL, MiniRV::X2, MiniRV::X2,
            -static_cast<int64_t>(StackSize), MachineInstr::FrameSetup);
  // epilogue:
  adjustReg(MBB, MBBI, DL, MiniRV::X2, MiniRV::X2,
            static_cast<int64_t>(StackSize), MachineInstr::FrameDestroy);
```

`eliminateFrameIndex` 处理“过了 regalloc 才发现偏移太大”：

```cpp
  int64_t Offset = static_cast<int64_t>(MFI.getObjectOffset(FrameIndex)) +
                   static_cast<int64_t>(MFI.getStackSize()) +
                   MI.getOperand(FIOperandNum + 1).getImm();
  if (isInt<12>(Offset)) {
    MI.getOperand(FIOperandNum).ChangeToRegister(MiniRV::X2, /*isDef=*/false);
    MI.getOperand(FIOperandNum + 1).ChangeToImmediate(Offset);
    return false;
  }
  // A base register is materialised as "addi <fi>, 0"; compute in place.
  if (MI.getOpcode() == MiniRV::ADDI) {
    Register Dest = MI.getOperand(0).getReg();
    TFI->adjustReg(MBB, II, DL, Dest, MiniRV::X2, Offset, MachineInstr::NoFlags);
    MI.eraseFromParent();
    return true;
  }
  // A load or store: address it through X5 with a zero displacement.
  TFI->adjustReg(MBB, II, DL, MiniRV::X5, MiniRV::X2, Offset,
                 MachineInstr::NoFlags);
  MI.getOperand(FIOperandNum).ChangeToRegister(MiniRV::X5, /*isDef=*/false);
  MI.getOperand(FIOperandNum + 1).ChangeToImmediate(0);
  return false;
```

再让 LocalStackSlotAllocation 在 regalloc 之前给大对象建基址，并保留 scratch：

```cpp
bool MiniRVRegisterInfo::requiresVirtualBaseRegisters(
    const MachineFunction &MF) const {
  return true;  // objects more than 2047 bytes from SP need a base register
}
// getReservedRegs: X5 materialises a stack address, X6 a stack adjustment.
```

配套实现 `needsFrameBaseReg`、`isFrameOffsetLegal`、`getFrameIndexInstrOffset`、`materializeFrameBaseRegister`、`resolveFrameIndex`；`materializeFrameBaseRegister` 生成的 `ADDI %fi, Offset` 正好由上面的 `eliminateFrameIndex` 分支接住。`-debug-only=regalloc` 能看到 scratch 不在分配顺序里：

```console
$ $FIX/llc -mtriple=minirv64 -O2 -debug-only=regalloc far-object.ll -o /dev/null 2>&1 | head -1
AllocationOrder(GPR) = [ $x3 $x4 $x7 $x10 $x11 $x12 $x13 $x14 $x15 $x16 $x17 $x28 $x29 $x30 $x31 $x1 $x8 $x9 $x18 $x19 $x20 $x21 $x22 $x23 $x24 $x25 $x26 $x27 ]
```

`$x2`、`$x5`、`$x6` 都不在列表里（`$x0` 也不在），所以分配器不会把活值放进 scratch。

### 5. 诊断决策树

```text
最终输出不对
├─ llc 直接报错？
│  ├─ "Cannot select: ... FrameIndex"
│  │    → -debug-only=isel 看 Initial selection DAG
│  │    → FrameIndex 没有 pattern：ISel 层（-stop-after=finalize-isel 无输出）
│  └─ "MiniRV frames must fit ..." / 地址越界
│       → -stop-after=prolog-epilog：frame lowering / PEI
│       → -stop-before=prolog-epilog 看 stackSize/localFrameSize
├─ 有 asm，但语义不对
│  ├─ -stop-after=finalize-isel 的 MIR 已错      → ISel / 合法化 / DAG combine
│  ├─ finalize-isel 对，prolog-epilog 错        → frame lowering、spill 偏移
│  ├─ prolog-epilog 对，最终 asm 错             → regalloc / peephole / AsmPrinter
│  │    → -debug-only=regalloc 看虚拟寄存器去向、是否 spill
│  ├─ asm 对，objdump 错                        → MC 编码
│  │    → llvm-mc -show-encoding 单独验证立即数/编码
│  └─ objdump 对，运行时错                      → ABI / 链接 / 运行时
│       → e2e（qemu）与 RISC-V 参考后端对比退出码
```

顺序不能反：先证明“上一层是对的”，再往下走。否则你会同时怀疑 ISel 和 frame lowering，最后靠猜。

### 6. flag 速查表

| flag | 回答什么问题 | 注意 |
|---|---|---|
| `-print-pipeline-passes` | pass 顺序和精确名字 | 必须一字不差，如 `prolog-epilog` |
| `-debug-only=isel` | 选指前后的 DAG/MIR；`FrameIndex` 有没有被接住 | 需 assertions 构建；stderr |
| `-stop-after=finalize-isel` | ISel 是否产出合法 MIR | 不含 prologue，`stackSize` 还是 0 |
| `-stop-before=prolog-epilog` | PEI 之前的对象布局、localFrameSize | 帧还没算出来 |
| `-stop-after=prolog-epilog` | PEI 是否成功；spill/地址是否已解析 | frame 层的分水岭 |
| `-debug-only=regalloc` | 虚拟寄存器→物理寄存器、spill、保留寄存器 | 输出很大，配 `-o /dev/null` |
| `-run-pass=prolog-epilog` | 拿 MIR 单独重跑 PEI | 需要一份合法 MIR |
| `llvm-mc -show-encoding` | MC 层接不接受这个立即数/编码 | 只测 MC，不经过 CodeGen |
| `llvm-objdump -d` | 真实编码（ABI 别名、截断都在这里现形） | 汇编文本不可信 |
| `llvm-lit -v` | 回归是否被锁住 | 需要稳定 CHECK |

### 7. 回归测试

最小测试（在 `d93cc60` 上实测通过）：

```llvm
; RUN: llc < %s -mtriple=minirv64 -O2 | FileCheck %s
; CHECK-LABEL: large_frame:
; CHECK:       lui x6, -1
; CHECK-NEXT:  addi x6, x6, 1696
; CHECK-NEXT:  add x2, x6, x2
; CHECK:       lui x6, 1
; CHECK-NEXT:  addi x6, x6, -1696
; CHECK-NEXT:  add x2, x6, x2
; CHECK-NEXT:  jalr x0, 0(x1)
define i64 @large_frame(i64 %n) {
entry:
  %a = alloca [300 x i64], align 8
  store volatile i64 %n, ptr %a, align 8
  %v = load volatile i64, ptr %a, align 8
  ret i64 %v
}
```

```console
$ $FIX/llc < large-frame-test.ll -mtriple=minirv64 -O2 \
    | $FIX/FileCheck large-frame-test.ll && echo PASS
PASS
$ cd /tmp/minirv-before-build && ./bin/llvm-lit /tmp/minirv-before/llvm/test/CodeGen/MiniRV 2>&1 | tail -2
Total Discovered Tests: 12
  Passed: 12 (100.00%)
```

在父提交上，这条测试会在 RUN 行就失败（`Cannot select`），所以它确实能锁住这个回归。注意：`90e179c` 之后序列变成两条 `addi`，测试也要跟着改；锁定的是**当前行为**，不是某条指令的永恒写法。

</details>

## 边界/测试

**1. 2047 / 2048 边界。** `addi` 的 `simm12` 是 `-2048..2047`。`alloca [255 x i64]`（2040 字节）可能仍是一条 `addi`，`[256 x i64]`（2048 字节）必须物化；帧里还有对齐和 spill 槽，实际 `StackSize` 往往更大。

**2. 为什么不能只删 guard。** 用 MIR 直接造一条超范围 `addi`（`badimm.mir`）：

```yaml
--- |
  target triple = "minirv64"
  define void @badimm() { ret void }
---
name: badimm
tracksRegLiveness: true
frameInfo: { stackSize: 0, maxAlignment: 8, adjustsStack: false, hasCalls: false, maxCallFrameSize: 0, isCalleeSavedInfoValid: true }
body: |
  bb.0:
    liveins: $x10
    $x2 = ADDI $x2, -2400
    PseudoRET implicit $x10
...
```

看真实编码：

```console
$ $B/llc -mtriple=minirv64 badimm.mir -o - | sed -n '/-2400/,/jalr/p'
	addi	x2, x2, -2400
	jalr	x0, 0(x1)
$ $B/llc -mtriple=minirv64 -filetype=obj badimm.mir -o /tmp/badimm.o
$ $B/llvm-objdump -d /tmp/badimm.o | sed -n '6,8p'
0000000000000000 <badimm>:
       0: 6a010113     	addi	sp, sp, 0x6a0
       4: 00008067     	ret
```

汇编文本是 `-2400`，编码是 `0x6a0`（`+1696`）：`MiniRVMCCodeEmitter::getMachineOpValue` 把立即数原样交给 TableGen，字段只有 12 位，高位被丢掉。这条指令会把 sp **抬高** 1696 字节，后面所有 store 都写到帧外。汇编器里同一件事是显式报错，所以别拿 `llvm-mc` 代替端到端验证：

```console
$ printf 'addi x2, x2, -2400\n' | $B/llvm-mc -triple=minirv64 -show-encoding 2>&1
<stdin>:1:14: error: operand must be a 12-bit signed immediate
addi x2, x2, -2400
             ^
```

**3. 对齐。** 帧大小按 8/16 字节对齐后可能跨过 2048；用 `alloca [257 x i64]`（2056 字节）验证对齐后的路径。

**4. 带 callee-saved spill 的函数。** prologue 先调 sp，再把 `x1`/`x8` 存到新 sp 附近；顺序反了，spill 会写到调用者的帧上。用 2400 字节帧 + 一次调用检查 `sd x1, ...` 在 `add x2, x6, x2` 之后。

**5. 对象内大位移。** `far-object.ll` 的 load 在对象内 2392 字节处；修好后它走 scratch 或 ISel 的常量寄存器，不是 `x2+2392`。用 `llvm-objdump` 确认位移字段是 0 或小值。

**6. 端到端。** 单元测试通过不代表运行时对。当前 HEAD 的 e2e：

```console
$ LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld QEMU=/usr/bin/qemu-riscv64-static \
    LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
    /home/ubuntu/llvm/trees/dsl-tablegen/llvm/lib/Target/MiniRV/dev/e2e/run.sh 2>&1 | tail -2
OK   vla2             exit=39
OK   vla3             exit=45
```

17 个程序全部 `OK` 才算闭环；直接踩大帧/vla 的是 `bigframe`、`bigframe2`、`twoarr`、`vla`。

## 加分挑战

1. **用 `-run-pass` 复现“只删 guard”。** 临时把 `report_fatal_error` 换成裸 `ADDI`，重建，观察 asm 是 `-2400`、objdump 是 `+1696`；不想重建就引用本节的 `badimm.mir`。
2. **对比 RISC-V。** 用 `-mtriple=riscv64 -mattr=+m` 编 `large-frame.ll`，对照 `RISCVRegisterInfo::adjustReg` 和 `RISCVFrameLowering::getFirstSPAdjustAmount`，看它怎么拆两条 `addi`。
3. **跟到 `6e7e7ab`。** 当前 HEAD 在 callee-saved spill 两侧拆帧（`getFirstSPAdjustAmount` 返回 `2048 - align`），让 spill 用 12 位位移；对比 `caller` 在 `d93cc60` 和 HEAD 的 `sd`/`ld` 序列。

## 反直觉与踩雷

1. **错误直觉**：`Cannot select` 是 pattern 写漏了，应该去补 `.td`。  
   **正确认识**：漏的是节点种类。`tframeindex` 只匹配 `ISD::TargetFrameIndex`；IR alloca 产生的是 `ISD::FrameIndex`，要在 `Select` 里先转成 target 节点。
2. **错误直觉**：`-stop-after=finalize-isel` 没输出，说明 flag 名字不对。  
   **正确认识**：flag 没问题，是 ISel 先 abort 了；看 `SelectionDAGISel::CannotYetSelect`。
3. **错误直觉**：`stackSize: 0` 说明这个函数没有栈帧。  
   **正确认识**：PEI 之前就是 0，真正的帧大小在 `prolog-epilog` 里算；看 `localFrameSize` 和 `stack:`。
4. **错误直觉**：`llc -o -` 打出来的汇编就是最终编码。  
   **正确认识**：汇编文本可以打印超范围立即数，编码时字段只有 12 位；必须 objdump。
5. **错误直觉**：`-debug-only=regalloc` 只在“寄存器不够用”时有用。  
   **正确认识**：它还能验证保留寄存器；`AllocationOrder(GPR)` 里没有 `$x5/$x6`，scratch 才安全。

## 本章重点整理

- 定位回归的顺序是“分层截断”：`finalize-isel` → `prolog-epilog` → `regalloc` → objdump → e2e。
- MiniRV 的 `FrameIndex` 回归有两层：ISel 不认普通 `FrameIndex`；PEI 假设帧和对象偏移都塞得进 12 位。
- 大栈帧的修复是四件事：物化常量、临时寄存器、基址寄存器、`eliminateFrameIndex` 兜底。
- scratch 必须在 `getReservedRegs` 里保留；否则 regalloc 会把活值放进 `x5/x6`。
- 汇编文本不等于编码；lit 锁的是当前行为，后续优化会改变指令序列。

## 自我检核

- [ ] 不看笔记，说出 `FrameIndex` 和 `TargetFrameIndex` 的区别，以及 MiniRV 为什么只给后者写 pattern。
- [ ] 为什么 `-stop-after=finalize-isel` 失败就能把责任定在 ISel？如果它成功、`-stop-after=prolog-epilog` 失败呢？
- [ ] `-stop-before=prolog-epilog` 里 `stackSize` 是多少？帧大小是谁算的？
- [ ] 只删掉 `isInt<12>` 的 guard 会发生什么？给出汇编文本和 objdump 的差异。
- [ ] 修复后 `far-object.ll` 的 load 位移是多少？为什么不是 2392？

## 延伸阅读

- **[LLVM CodeGenerator 文档](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方
  - **读哪里**：Machine Code Framework、Prolog/Epilog、Frame Objects、Register Allocation。
  - **能学到什么**：PEI 的职责边界，为什么 `stackSize` 在 PEI 前是 0，`eliminateFrameIndex` 在哪个阶段跑。
  - **前提**：能读懂 `-stop-after` 输出的 MIR。

- **[lit 与 FileCheck](../llvm_lit/README.md)** — 本仓库的测试基础设施课程
  - **读哪里**：RUN 行、`CHECK-NEXT`/`CHECK-LABEL`、`not --crash`。
  - **能学到什么**：把本章的手工命令变成稳定回归测试，以及测试如何随行为变化更新。
  - **前提**：完成 Step 5。

- **[RISC-V 后端全貌](../riscv_backend/README.md)** — 本仓库的 RISC-V 后端课程
  - **读哪里**：`RISCVFrameLowering::getFirstSPAdjustAmount`、`RISCVRegisterInfo::adjustReg`、`eliminateFrameIndex`。
  - **能学到什么**：成熟后端怎么把“大帧 + spill + 基址寄存器”拆成可维护的几步；MiniRV 的修复是它的最小版本。
  - **前提**：知道 `simm12` 和 `lui/addi` 的拼装方式。

- **[LLVM ISel 与调度（RISC-V）](../llvm_isel_sched_riscv/README.md)** — 本仓库的选指/调度课程
  - **读哪里**：SelectionDAG 的地址模式、`ComplexPattern`、帧索引在 DAG 里的形态。
  - **能学到什么**：为什么 `FrameIndex` 必须先进 target 节点，pattern 才能匹配。
  - **前提**：能看懂 `-debug-only=isel` 的 DAG dump。

→ [Final Project — 从零接入 MiniRV+](./final-project-new-target.md)
