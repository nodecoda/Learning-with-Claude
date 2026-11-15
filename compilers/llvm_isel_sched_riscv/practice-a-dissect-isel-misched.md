# 练习 A — 解剖一个函数的 ISel → MISched

> **目标**：把 Ch 0–3 的工具拼起来，对一个函数做一次完整的归因：哪些指令是 SelectionDAG 选的、哪些顺序是 `ScheduleDAGSDNodes` 排的、哪些顺序是 `MachineScheduler` 排的、哪些差异是 feature 造成的。完成后你应该能拿出一张「指令 → MIR → 最终汇编 → 谁做的」的表，而不是一句「LLVM 优化了」。

## 背景与动机

面试里常见的题型是：

> 「这段 C/IR 在 `sifive-u74` 上生成这样的汇编，为什么 `add` 排在两个 `ld` 前面？如果换成 `generic` 呢？如果加 `-mattr=+zba` 呢？」

这类题不是考你背汇编，而是考你能不能**分层归因**。本练习模拟一次真实 code review：有人给你一段 IR 和三份输出，你要在 30 分钟内指出差异来源，并给出可复现的命令。

## 任务规格

### 输入

把下面的 IR 保存成 `work.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @work(ptr %p, i64 %a, i64 %b, i64 %c, i64 %d) {
  %x0 = load i64, ptr %p
  %x1 = mul i64 %x0, %a
  %p1 = getelementptr i64, ptr %p, i64 1
  %y0 = load i64, ptr %p1
  %y1 = mul i64 %y0, %c
  %s0 = add i64 %d, %b
  %s1 = add i64 %s0, %x1
  %s2 = add i64 %s1, %y1
  ret i64 %s2
}
```

### 输出（三张表 + 一段结论）

1. **指令选择表**：每条 IR 操作对应哪条 MIR opcode，为什么。至少覆盖 `load`、`mul`、`getelementptr`、`add`、`ret`。
2. **调度变化表**：`-print-before=machine-scheduler` 和 `-print-after=machine-scheduler` 之间，哪些指令的位置变了，为什么。至少找出两条。
3. **归因表**：比较 `generic`、`rocket-rv64`、`sifive-u74`、`-global-isel`、`-O0` 五种设置，指出每种差异是 selection、scheduling、feature 还是 register allocation。
4. **一段结论**：用 5–8 句话回答「为什么 `sifive-u74` 把 `add` 提前了，而 `generic` 没有」。

### 约束

- 必须用 `-mattr=+m` 固定 `M` 扩展，否则 `generic`/`rocket-rv64` 会把 `mul` 降成 `__muldi3`，比较的不是同一件事。
- 至少用一次 `-print-after-isel`、一次 `-print-before/after=machine-scheduler`、一次 `-debug-only=machine-scheduler`。
- 至少用一次 `llvm-mca`，并记录 `-mcpu`、`-mattr`、`-iterations`。
- 不允许只看最终汇编就下结论；每个「谁做的」都要有对应命令的证据。

## 期望输出示例

### 指令选择表（示例）

| IR 操作 | MIR（`-print-after-isel`） | 选择理由 |
|---|---|---|
| `load i64, ptr %p` | `%5:gpr = LD %0:gpr, 0` | `i64` load → `LD`；offset 0 |
| `mul i64 %x0, %a` | `%6:gpr = MUL %5:gpr, %1:gpr` | 有 `M` 扩展 → `MUL`；否则 `__muldi3` |
| `getelementptr i64, ptr %p, i64 1` | 没有单独指令；folded 成 `LD %0:gpr, 8` | `SelectAddrRegImm` 把常量偏移塞进 load |
| `add i64 %d, %b` | `%9:gpr = ADD %4:gpr, %2:gpr` | 简单 ALU；没有 condition code |
| `ret i64 %s2` | `$x10 = COPY %11:gpr; PseudoRET` | 返回值放 `a0`；`PseudoRET` 后面展开 |

### 调度变化表（示例）

| 指令 | before 位置 | after 位置 | 为什么移动 |
|---|---|---|---|
| `%9 = ADD %4, %2` | `%8` 之后 | `%5` 之前 | 不依赖 load/mul；提前不增加压力；填充 load 延迟 |
| `%6 = MUL %5, %1` | `%5` 之后 | 两个 `LD` 之后 | 依赖 `%5`；scheduler 把两个 load 放前面 |
| `%0..%2 = COPY` | 源码顺序 | 重新排序 | COPY 之间无依赖；为减少活区间 |

### 归因表（示例）

| 设置 | 指令种类 | 顺序 | 结论 |
|---|---|---|---|
| `generic -mattr=+m` | 2 `ld` + 2 `mul` + 3 `add` + `ret` | `add` 在后面 | SelectionDAG + generic-rv64 的 model |
| `rocket-rv64 -mattr=+m` | 同上 | 同上 | Rocket 单发射模型 |
| `sifive-u74 -mattr=+m` | 同上 | `add` 提前 | MachineScheduler + SiFive7 模型 |
| `-global-isel` | 同上 | `ld a5; add a2; ld a0; ...` | selector 改变 MIR 形状 |
| `-O0` | 有 spill | 入口 `sd a2, 8(sp)` | O0 pipeline + 寄存器分配，不是 FastISel |

## 如果你卡住了

1. **先确认你看的是哪一层**：`-print-after-isel` 是「选完但还没排」，`-print-after=machine-scheduler` 是「pre-RA 排完」。两者不要混。
2. **先固定 feature**：`-mcpu` 会改 feature。`generic`/`rocket-rv64` 默认没有 `M`；加 `-mattr=+m` 再比较。
3. **用 `diff` 找位置变化**：把 before/after MIR 存成文件，`diff -u` 比肉眼快。重点看 `%9`、`%6`、`%8` 这些编号。
4. **看 `-debug-only=machine-scheduler` 的 `RegionPolicy` 和 `Cluster ld/st`**：它们解释「为什么两个 load 被放一起」「为什么调度器敢提前 `add`」。
5. **不要把 `llvm-mca` 的周期数当成硬件实测**：它是模型模拟；同一段汇编换 `-mcpu`，周期数就变。

## 实作步骤建议

### Step 1：建立基线

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
MCA=/home/ubuntu/llvm/builds/explore/bin/llvm-mca
mkdir -p /tmp/isel-sched-a && cd /tmp/isel-sched-a
# 保存 work.ll
```

先跑 `sifive-u74` 的最终汇编：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 work.ll -o -
```

你应该看到 `add` 在两个 `ld` 前面。把输出保存成 `sifive.s`。

### Step 2：抓 SelectionDAG 的结果

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after-isel work.ll -o /dev/null > isel.mir 2>&1
```

在 `isel.mir` 里找 `%9:gpr = ADD`。它此时还在 `%8` 后面。记录这一点：**ISel 没有把 `add` 提前。**

### Step 3：抓 MachineScheduler 的 before/after

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-before=machine-scheduler work.ll -o /dev/null > before.mir 2>&1

"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after=machine-scheduler work.ll -o /dev/null > after.mir 2>&1

diff -u <(sed -n '/^0B/,/^$/p' before.mir) \
         <(sed -n '/^0B/,/^$/p' after.mir)
```

参考 diff：

```diff
--- before.mir
+++ after.mir
@@ -2,14 +2,14 @@
 	  liveins: $x10, $x11, $x12, $x13, $x14
 16B	  %4:gpr = COPY $x14
 32B	  %3:gpr = COPY $x13
+40B	  %0:gpr = COPY $x10
+44B	  %1:gpr = COPY $x11
 48B	  %2:gpr = COPY $x12
-64B	  %1:gpr = COPY $x11
-80B	  %0:gpr = COPY $x10
+56B	  %9:gpr = ADD %4:gpr, %2:gpr
 96B	  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
-112B	  %6:gpr = MUL %5:gpr, %1:gpr
 128B	  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
+136B	  %6:gpr = MUL %5:gpr, %1:gpr
 144B	  %8:gpr = MUL %7:gpr, %3:gpr
-160B	  %9:gpr = ADD %4:gpr, %2:gpr
 176B	  %10:gpr = ADD %9:gpr, %6:gpr
 192B	  %11:gpr = ADD %10:gpr, %8:gpr
```

从这里能读出：

- `%9 = ADD` 从 `%8` 后面移到了 `%5` 前面。
- `%6 = MUL` 从 `%5` 后面移到了两个 `LD` 后面。
- `COPY` 也被重排，目的是缩短活区间。

### Step 4：看调度器为什么这么做

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -debug-only=machine-scheduler work.ll -o /dev/null 2>&1 | head -60
```

重点找：

```text
GenericScheduler RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=0 OnlyBottomUp=0
Cluster ld/st SU(5) - SU(7)
```

解释：

- `ShouldTrackPressure=1`：调度器不是只看延迟，还在控制寄存器压力。
- `Cluster ld/st`：两个 load 被当成一个 cluster，倾向放在一起。
- `%9` 不依赖 load/mul，提前它不会增加压力，还能填充 load 的 latency。

### Step 5：换 CPU、换 selector、换 opt level

```bash
for cpu in generic rocket-rv64 sifive-u74; do
  echo "===== $cpu ====="
  "$LLC" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -O2 work.ll -o - \
    | sed -n '/^work:/,/^\.Lfunc_end/p'
done

echo "===== globalisel ====="
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -global-isel -global-isel-abort=1 work.ll -o - \
  | sed -n '/^work:/,/^\.Lfunc_end/p'

echo "===== O0 ====="
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O0 work.ll -o - \
  | sed -n '/^work:/,/^\.Lfunc_end/p'
```

把结果填进归因表。注意：`-O0` 有 spill 不代表 FastISel；RISC-V 没有 FastISel，这是 SelectionDAG + O0 寄存器分配。

### Step 6：用 mca 量化同一段汇编

```bash
cat > work.s <<'ASM'
add a2, a2, a4
ld a4, 0(a0)
ld a0, 8(a0)
mul a1, a4, a1
mul a0, a0, a3
add a1, a1, a2
add a0, a0, a1
ret
ASM

for cpu in generic rocket-rv64 sifive-u74; do
  echo "--- $cpu ---"
  "$MCA" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -iterations=100 work.s \
    | grep -E "Iterations|Instructions|Total Cycles|Total uOps|Dispatch Width|IPC|Block RThroughput"
done
```

如果你得到 901 / 1001 / 1201 附近的数字，说明你在同一段汇编上只换了模型。把 `-mcpu` 写进表格，不要只写「mca 说 X 周期」。

## 完整参考解答

**先自己做完再看。** 下面给出关键输出和解释，不是唯一答案；如果你的数字略有不同，先检查 LLVM 版本、`-mattr` 和 `-mcpu`。

<details>
<summary>点开参考解答</summary>

### 1. 指令选择表

以 `-mcpu=sifive-u74 -mattr=+m -O2 -print-after-isel` 为准：

```text
%5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
%6:gpr = MUL %5:gpr, %1:gpr
%7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
%8:gpr = MUL %7:gpr, %3:gpr
%9:gpr = ADD %4:gpr, %2:gpr
%10:gpr = ADD %9:gpr, %6:gpr
%11:gpr = ADD %10:gpr, %8:gpr
```

| IR 操作 | MIR | 理由 |
|---|---|---|
| `load i64, ptr %p` | `%5 = LD %0, 0` | `i64` → `LD`；offset 0 |
| `mul i64 %x0, %a` | `%6 = MUL %5, %1` | `+m`；否则 `__muldi3` |
| `getelementptr i64, ptr %p, i64 1` | folded 进 `%7 = LD %0, 8` | 常量偏移 8 在 `simm12` 内 |
| `mul i64 %y0, %c` | `%8 = MUL %7, %3` | 同上 |
| `add i64 %d, %b` | `%9 = ADD %4, %2` | 普通 ALU；无 condition code |
| `add i64 %s0, %x1` | `%10 = ADD %9, %6` | 依赖 `%9`、`%6` |
| `add i64 %s1, %y1` | `%11 = ADD %10, %8` | 依赖 `%10`、`%8` |
| `ret i64 %s2` | `COPY $x10, %11; PseudoRET` | 返回值放 `a0`；pseudo 后展开 |

### 2. 调度变化表

`diff` 的关键行：

```diff
+56B	  %9:gpr = ADD %4:gpr, %2:gpr
 96B	  %5:gpr = LD %0:gpr, 0
-112B	  %6:gpr = MUL %5:gpr, %1:gpr
 128B	  %7:gpr = LD %0:gpr, 8
+136B	  %6:gpr = MUL %5:gpr, %1:gpr
```

| 指令 | before | after | 原因 |
|---|---|---|---|
| `%9 = ADD %4, %2` | `%8` 后 | `%5` 前 | 无依赖；不增加压力；填充 load latency |
| `%6 = MUL %5, %1` | `%5` 后 | 两个 `LD` 后 | 依赖 `%5`；scheduler 把两个 load 先发 |
| `%0..%2 = COPY` | 源码顺序 | 重排 | 缩短活区间；COPY 无依赖 |

### 3. 归因表

| 设置 | 指令种类 | 顺序 | 归因 |
|---|---|---|---|
| `generic -mattr=+m` | 2 `ld` + 2 `mul` + 3 `add` + `ret` | `add` 在后面 | SelectionDAG + `generic-rv64`（`SpacemitX60Model`） |
| `rocket-rv64 -mattr=+m` | 同上 | 同上 | SelectionDAG + `RocketModel`（单发射） |
| `sifive-u74 -mattr=+m` | 同上 | `add` 提前 | MachineScheduler + `SiFive7Model` |
| `-global-isel` | 同上 | `ld a5; add a2; ld a0; ...` | selector 改变 MIR 形状；scheduler 输入不同 |
| `-O0` | 多出 spill | 入口 `sd a2, 8(sp)` | O0 pipeline + 寄存器分配；不是 FastISel |

### 4. 结论（示例）

1. `sifive-u74` 和 `generic` 的指令种类完全一样，所以差异不在 instruction selection。
2. `sifive-u74` 的 `add` 被提前到两个 `ld` 之前，这是 `MachineScheduler` 做的；`-print-before/after=machine-scheduler` 可以直接证明。
3. `add` 不依赖任何 load/mul，提前它不会增加寄存器压力，还能填充 load 的 latency。
4. `generic` 在 RV64 下映射到 `generic-rv64`，使用 `SpacemitX60Model`（双发射、顺序核）；`rocket-rv64` 是单发射。模型不同，调度策略不同。
5. 如果换成 `-global-isel`，指令种类仍然一样，但 MIR 的虚拟寄存器/COPY 形状不同，scheduler 的输出也不同；这说明「换 selector」会改变 scheduling 的输入。
6. `-O0` 的 spill 不是 FastISel 的锅；RISC-V 没有 FastISel，`-O0` 仍然是 SelectionDAG + O0 寄存器分配。
7. 判断这类问题的最短路径：先看指令种类（selection/feature），再看顺序（scheduling），最后看 spill（RA）。

</details>

## 测试用例

| 输入 / 设置 | 预期观察 | 说明 |
|---|---|---|
| `sifive-u74 -mattr=+m -O2` | `add` 在两个 `ld` 前 | MachineScheduler 提前 |
| `generic -mattr=+m -O2` | `add` 在后面 | 模型不同 |
| `rocket-rv64 -mattr=+m -O2` | 和 `generic` 同序 | 单发射模型 |
| `-enable-misched=false` | 顺序接近 source | 只剩 `ScheduleDAGSDNodes` |
| `-pre-RA-sched=source` + `-enable-misched=false` | `ld; add; ld; ...` | DAG scheduler 保持源码序 |
| `-global-isel -global-isel-abort=1` | 顺序和 SelectionDAG 不同 | selector 改变 MIR |
| `-O0` | 有 `sd`/`ld` spill | O0 pipeline/RA |
| 去掉 `-mattr=+m` | `__muldi3` 调用 | feature 差异，不是 scheduling |

## 常见错误答案

下面这些回答听起来像那么回事，但都缺一层证据。面试时说出其中一句，基本会被追问倒。

1. **「`sifive-u74` 的指令选择更激进，所以把 `add` 提前了。」**
   - 错在哪：指令种类完全一样。`-print-after-isel` 里两边都是 `LD/LD/MUL/MUL/ADD/ADD/ADD`。提前发生在 `MachineScheduler`。
   - 正确说法：selection 没变；`MachineScheduler` 按 `SiFive7Model` 重排。

2. **「`generic` 没有调度模型，所以不排。」**
   - 错在哪：RV64 下 `-mcpu=generic` 映射到 `generic-rv64`，使用 `SpacemitX60Model`；`-mtune=generic` 同样被规范化成 `generic-rv64`。真正的 `NoSchedModel` 要用 `-mcpu=andes-ax25` / `-mtune=andes-ax25` 才看得到。
   - 正确说法：`generic` 有模型，只是和 `sifive-u74` 不同。

3. **「`-O0` 用了 FastISel，所以有 spill。」**
   - 错在哪：RISC-V 没有 FastISel 实现。`-O0` 仍然走 SelectionDAG；spill 来自 O0 的寄存器分配和栈槽选择。
   - 正确说法：`-O0` 是 SelectionDAG + O0 pipeline；要看 RA，用 `-print-after=regalloc`。

4. **「`-global-isel` 的输出顺序不同，因为 GlobalISel 的调度器更好。」**
   - 错在哪：两者最后都经过 `MachineScheduler`；顺序不同的第一原因是 MIR 形状不同（虚拟寄存器、COPY、初始顺序）。
   - 正确说法：selector 改变了 scheduling 的输入，不是 scheduler 本身变了。

5. **「`add` 提前是因为它不依赖 load。」**
   - 不完整：不依赖只是「可以提前」的必要条件；还要看寄存器压力、调度策略、模型里的 `LoadLatency`、以及是否被 cluster 挡住。
   - 正确说法：无依赖 + 不增加压力 + 能填充 load latency，三个条件同时成立。

## 面试表达模板

把答案压缩成 30 秒，可以按这个顺序说：

```text
先分层：这段差异不是 instruction selection，因为指令种类一样（2 ld / 2 mul / 3 add）。
再看顺序：-print-before/after=machine-scheduler 显示 %9 ADD 从 %8 后移到 %5 前。
再解释：%9 不依赖 load/mul，提前不增加寄存器压力，还能填充 load latency；
        MachineScheduler 在 SiFive7Model 下选择提前，generic/rocket 的模型没有这么做。
最后给证据：-debug-only=machine-scheduler 里 ShouldTrackPressure=1、Cluster ld/st SU(5)-SU(7)。
```

这个模板的好处是：每一句都能被一条命令验证。面试官继续追问，你就把命令和输出说出来。

## 如果输出和参考不一致

| 现象 | 第一嫌疑 | 检查 |
|---|---|---|
| `mul` 变成 `__muldi3` | 没有 `M` | 加 `-mattr=+m`；看 `-print-after-isel` |
| `sifive-u74` 也没有提前 `add` | 版本/模型不同 | 看 `llc --version`、`RISCVSchedSiFive7.td` 的 `IssueWidth`/`LoadLatency` |
| `-global-isel` 直接报错 | Legalizer 不支持 | 加 `-global-isel-abort=1`；看错误里的 `G_*` op |
| `-O0` 没有 spill | 参数/ABI 不同 | 检查函数签名、`-mattr`、`target triple` |
| mca 周期数和参考差很多 | `-mcpu`/`-mattr`/`-iterations` 不同 | 把三个参数写全；确认 `llvm-mca --version` |

## 参考输出：三份最终汇编

### `generic -mattr=+m -O2`

```asm
f:                                      # @f
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

### `rocket-rv64 -mattr=+m -O2`

```asm
f:                                      # @f
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

### `sifive-u74 -mattr=+m -O2`

```asm
f:                                      # @f
	add	a2, a2, a4
	ld	a4, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a4, a1
	mul	a0, a0, a3
	add	a1, a1, a2
	add	a0, a0, a1
	ret
```

### `llvm-mca` 汇总

| CPU | Total Cycles | Dispatch Width | IPC | Block RThroughput |
|---|---:|---:|---:|---:|
| `generic` | 1201 | 2 | 0.67 | 4.0 |
| `rocket-rv64` | 1001 | 1 | 0.80 | 8.0 |
| `sifive-u74` | 901 | 2 | 0.89 | 4.0 |

注意：这张表是**同一段汇编**在三个模型上的模拟，不是三个 `llc` 输出的性能对比。要做后者，必须把三份汇编分别喂给同一个 `-mcpu`。

## 评分标准

| 项目 | 及格 | 良好 | 优秀 |
|---|---|---|---|
| 指令选择表 | 能列出 opcode | 能解释 `LD`/`MUL`/`ADD` 的理由 | 能指出 `getelementptr` folded 进 offset，并给出源码函数 |
| 调度变化表 | 能看出 `add` 提前 | 能用 before/after MIR 证明 | 能结合 `ShouldTrackPressure`、`Cluster ld/st` 解释为什么 |
| 归因表 | 能区分 selection/order | 能区分 feature/RA | 能指出 `generic` 映射到 `generic-rv64`，`-O0` 没有 FastISel |
| 证据 | 用了 `llc` | 用了 `-print-after-isel` 和 scheduler before/after | 用了 `-debug-only=machine-scheduler` 和 `llvm-mca`，参数写全 |
| 表达 | 能说结论 | 能按「先分层、再解释、后证据」说 | 能主动说出反例和边界，不把模型周期当硬件性能 |

## 延伸挑战（加分）

1. 把 `work.ll` 的 `%s0 = add %d, %b` 改成依赖 `%x1`，再跑 `sifive-u74`。`add` 还能提前吗？解释依赖和调度自由度的关系。
2. 给 `work.ll` 加 `-mattr=+zba`，把 `getelementptr` 改成变量索引，观察 `sh3add`。用 `llvm-mca` 比较 `sh3add` 版本和 `slli+add` 版本的关键路径。
3. 用 `-mcpu=generic-ooo -mattr=+64bit,+m` 跑一次。它和 `sifive-u74` 的顺序一样吗？如果不一样，查 `RISCVSchedGenericOOO.td` 的 `IssueWidth`、`MicroOpBufferSize`、`LoadLatency`。
4. 用 `-global-isel` 跑 `work.ll`，再用 `-print-after=instruction-select` 和 `-print-after=machine-scheduler` 对比。找出一个「SelectionDAG 有、GlobalISel 没有」的 `COPY`。
5. 写一个 `i32` 版本的 `work`，用 `-mattr=+zba` 看它选 `sh1add` 还是 `sh3add`。解释 scale 和类型的关系。

## 自我检核

- [ ] 能独立跑出 `-print-after-isel`、`-print-before/after=machine-scheduler`、`-debug-only=machine-scheduler` 三份证据
- [ ] 能指出至少两条位置变化的指令，并解释依赖/压力原因
- [ ] 能区分「指令种类变」和「顺序变」分别归因给哪一层
- [ ] 能解释 `-mcpu` 同时改 feature 和 model，以及如何用 `-mattr` 控制变量
- [ ] 能解释 `-O0` 的 spill 不是 FastISel
- [ ] 能用 `llvm-mca` 在同一段汇编上比较不同 `-mcpu`，并说明这是模型差异

下一章进入 SelectionDAG 的数据结构。你会看到 `chain`、`glue`、`TokenFactor` 如何把「不能随便移动」的约束写进 DAG，以及为什么 `add` 可以提前而 `ld` 不能随便越过 `sd`。

→ [Ch 4 SelectionDAG 的数据结构：chain、glue、token](./04-selectiondag-data-structures.md)
