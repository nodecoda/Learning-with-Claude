# Ch 23 — 调度模型与 `llvm-mca`：`la464` vs `la664`

> 本章目标：知道 `SchedMachineModel` 里的 `IssueWidth`/`MicroOpBufferSize` 怎么影响调度；能用 `llvm-mca` 对同一段循环给出 `la464`/`la664` 的 IPC 对比；并知道为什么 `generic-la64` 上 `llvm-mca` 直接报错。

---

## 23.1 三个调度文件

| 文件 | 行数 | 内容 |
|---|---:|---|
| `LoongArchSchedule.td` | — | 公共 `SchedWrite`/`SchedRead` 定义 |
| `LoongArchSchedLA464.td` | 1,104 | LA464 的 `ProcResource` 与 `SchedWriteRes` |
| `LoongArchSchedLA664.td` | 1,142 | LA664 的同上 |
| `LoongArchScheduleV.td` | 1,291 | LSX/LASX 向量调度 |

公共读写类（`LoongArchSchedule.td:12`）：

```tablegen
def WriteIALU   : SchedWrite;  // 32 或 64 位整数 ALU
def WriteIALU32 : SchedWrite;  // LA64 上的 32 位整数 ALU
def ReadIALU    : SchedRead;
def ReadIALU32  : SchedRead;

// ADD.W  SUB.W  Sched<[WriteIALU, ReadIALU32, ReadIALU32]>
// ADD.D  SUB.D  Sched<[WriteIALU, ReadIALU, ReadIALU]>
```

指令定义里：

```tablegen
def ADD_D : ALU_3R<0x00108000>, Sched<[WriteIALU, ReadIALU, ReadIALU]>;
def ADD_W : ALU_3R<0x00100000>, Sched<[WriteIALU, ReadIALU32, ReadIALU32]>;
```

`.w` 和 `.d` 的差别体现在 **Read** 类上：`.w` 读的是“32 位操作数”，`.d` 读 64 位。调度模型据此分配端口。

---

## 23.2 两个 CPU 的模型参数

```tablegen
def LA464Model : SchedMachineModel {
  let IssueWidth = 4;             // 4-way decode and dispatch
  let MicroOpBufferSize = 128;    // Reorder Buffer size
  let LoadLatency = 4;
  let MispredictPenalty = 14;
  let CompleteModel = 0;
}

def LA664Model : SchedMachineModel {
  let IssueWidth = 6;             // 6-way decode and dispatch
  let MicroOpBufferSize = 256;
  ...
}
```

差异：

| 参数 | LA464 | LA664 |
|---|---:|---:|
| 发射宽度 | 4 | **6** |
| ROB | 128 | **256** |
| 整数 ALU 端口 | 4（ALU0/1 带 MUL/Branch） | 4 |
| AGU | 2（1 load + 1 load/store） | 4（2 load + 2 store） |

LA664 更宽、更深的 ROB，所以同一段循环的 IPC 更高。

---

## 23.3 实测：同一段循环，两个 CPU

```asm
.LBB0_1:
    ld.d    $a0, $a1, 0
    add.d   $a2, $a2, $a0
    addi.d  $a1, $a1, 8
    bne     $a1, $a3, .LBB0_1
    ret
```

```console
$ llvm-mca -mtriple=loongarch64-unknown-linux-gnu -mcpu=la464 -iterations=100 loop.s
```

```text
Iterations:        100
Instructions:      500
Total Cycles:      132
Total uOps:        500

Dispatch Width:    4
uOps Per Cycle:    3.79
IPC:               3.79
Block RThroughput: 1.3
```

```console
$ llvm-mca ... -mcpu=la664 -iterations=100 loop.s
```

```text
Iterations:        100
Instructions:      500
Total Cycles:      107
Total uOps:        500

Dispatch Width:    6
uOps Per Cycle:    4.67
IPC:               4.67
Block RThroughput: 1.0
```

读法：

- **`Block RThroughput`**：这段代码的“吞吐下界”（每轮迭代最少多少周期）。1.3 → 每 1.3 周期一次迭代；1.0 → 每周期一次。
- **`IPC`**：每周期退休指令数。4 条指令/迭代，LA464 达到 3.79，接近发射宽度 4；LA664 4.67，受限于 4 条指令的循环体本身，而不是宽度。
- **`Total Cycles`**：100 次迭代的总周期，包含启动/排空开销。

**同一个循环，LA664 快 23%**——但这是因为模型参数不同，不代表真实硬件一定如此。`llvm-mca` 是“按模型推演”，不是实测硬件。

---

## 23.4 `generic-la64` 为什么不能用 `llvm-mca`

```tablegen
def : ProcessorModel<"generic-la64", NoSchedModel, [Feature64Bit, FeatureUAL, FeatureExtLSX]>;
```

`NoSchedModel` 表示“没有指令级调度信息”。`llvm-mca` 需要每条指令的端口/延迟，所以直接报错：

```text
error: unable to find instruction-level scheduling information for target triple
       'loongarch64-unknown-linux-gnu' and cpu 'generic-la64'.
```

这不是 bug，是设计：generic CPU 不承诺具体微架构。

---

## 23.5 `-mcpu` 同时改 feature

`la664` 的定义（`LoongArch.td:209`）：

```tablegen
def : ProcessorModel<"la664", LA664Model, [Feature64Bit, FeatureUAL,
                                           FeatureExtLASX, FeatureExtLVZ,
                                           FeatureExtLBT, FeatureFrecipe,
                                           FeatureLAM_BH, FeatureLAMCAS, ...]>;
```

所以 `-mcpu=la664` 不只是“换调度模型”，还会**打开 LASX/LBT/LVZ/frecipe/LAM**。这会导致：

- 指令选择变化（能用 `xv*`、`crc`、`frecipe` 等）；
- 测试里如果只想要调度模型，应该用 `-mcpu=la664 -mattr=-lasx,...` 精确控制，而不是假设 `-mcpu` 只影响调度。

**这是写 lit 测试最容易踩的坑之一**：`-mcpu=la664` 的输出和 `-mcpu=generic-la64 -mattr=+lasx` 可能不同。

---

## 23.6 `CompleteModel = 0` 的含义

`CompleteModel = 0` 表示“这个模型不覆盖所有指令”。遇到没有调度信息的指令，`llvm-mca` 会警告或按默认处理；调度器则可能把它当“未知延迟”。

如果你新增一条指令却忘了加 `Sched<[...]>`，在 `CompleteModel = 1` 的模型上会直接报错，在 `0` 上则静默降级——**后者更危险**，因为性能问题不会在测试里暴露。

---

## 23.7 反直觉点

1. **`generic-la64` 没有调度模型**，`llvm-mca` 直接报错。
2. **`-mcpu=la664` 会打开一堆扩展**，不只是换调度。
3. **`Block RThroughput` 是下界**，不是预测的实际周期。
4. **`IssueWidth` 是发射宽度，不是执行端口数**；ALU 端口数看 `ProcResource`。
5. **`.w` 与 `.d` 的调度差异在 Read 类**，不在 Write 类。
6. **`CompleteModel = 0` 会掩盖缺失的调度信息**。
7. **`llvm-mca` 的输入是汇编**，不是 IR；它不关心前端。

---

## 23.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| 公共读写类 | `LoongArchSchedule.td:12-18` |
| LA464 参数 | `LoongArchSchedLA464.td:19-25` |
| LA664 参数 | `LoongArchSchedLA664.td:20-26` |
| CPU feature 列表 | `LoongArch.td:202-216` |
| mca 实测 | 本章 23.3 |
| generic 报错 | 本章 23.4 |

---

## 23.9 练习

1. 把循环体改成 8 条独立 `add.d`，比较两个 CPU 的 IPC（应该接近发射宽度）。
2. 加入一条 `div.d`，观察 `Block RThroughput` 如何被非流水线单元拉高。
3. 用 `-mcpu=la664 -mattr=-lasx` 编译一个 `<8 x i32>` 函数，确认 LASX 被关闭。
4. 给一条新指令加 `Sched<>`，分别在 `la464`/`la664` 上跑 `llvm-mca`，观察差异。
5. 解释为什么 `llvm-mca` 对 `ret` 给出 “found a return instruction” 警告。

---

## 23.10 下一步

Part 4 结束。**练习 D** 会让你用 `-run-pass` 驱动一个机器层 pass，并写一个 MIR 测试。之后进入 Part 5：clang 驱动、builtin、runtime。
