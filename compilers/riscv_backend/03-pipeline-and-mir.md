# 03 流水线与 MIR：把 pass 名单变成可停靠的站点

> 目标：把 `llc` 从“一条命令”变成一条可以停靠的流水线。你会看到真实的 pass 顺序，学会用 `-stop-after`、`-stop-before`、`-start-before`、`-run-pass`、`-print-after-all`、`-simplify-mir` 在任意一站取快照，并能手写一段 MIR 只跑一个 pass。读完本章，MIR 不再是“看不懂的中间文件”，而是你调试后端的标准接口。

> 环境：LLVM 23.1.4 预编译版（Optimized build，无 `-debug-only` 日志）；同版本源码在 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。命令在仓库根目录 `D:\Learning-with-Claude` 下执行。

## 为什么

后端调试最常见的困境是“输出不对，但不知道在哪一站变坏”。你看到最终汇编少了一条 `mul`，可能是：

- IR 层的某个 pass 把乘法改写了。
- Legalizer 把 `MUL` 标成了 `Expand`。
- ISel 选了 libcall，而不是 `MUL`。
- 寄存器分配把结果 spill 了。
- 伪指令展开把 `PseudoCALL` 变成了 `auipc` + `jalr`。
- AsmPrinter 把某条指令折叠成了别名。

这六种原因对应六个不同的站位。没有停靠能力，你只能在最终汇编上猜。`llc` 的 `-stop-after`、`-stop-before`、`-run-pass` 就是为此存在的：它们让你在任意两站之间取一份 MIR，比较前后差异，把“猜”变成“看”。

MIR 是这条流水线的通用快照格式。它不是 LLVM IR 的另一种写法，而是 `MachineFunction` 的序列化：虚拟寄存器、物理寄存器、机器指令、栈槽、liveness、CFI 都在里面。读懂 MIR，你就能在不重新编译 LLVM、不打开调试日志的情况下，定位大多数后端问题。

本章的主线是三步：先看 pass 顺序，再学 MIR 语法，最后用停靠开关做实验。每一步都给真实输出。

## 主线

### 3.1 pass 管线：真实的顺序

`llc -debug-pass=Structure` 会打印完整的 pass 结构。真实输出很长，下面按站位分组，只保留与 RISC-V 相关的名字（名字来自本机 23.1.4 实测）：

```text
IR / 目标相关 IR：
  atomic-expand
  riscv-gather-scatter-lowering
  interleaved-access
  riscv-codegenprepare
  riscv-promote-const
  barrier
  global-merge

选指：
  riscv-isel
  finalize-isel

pre-RA 机器层：
  riscv-vl-optimizer
  riscv-vector-peephole
  riscv-fold-mem-offset
  machine-combiner
  machine-cse
  machine-sink
  riscv-opt-w-instrs
  riscv-prera-expand-pseudo
  riscv-merge-base-offset
  riscv-prera-zilsd-opt
  riscv-insert-read-write-csr
  riscv-insert-write-vxrm
  riscv-vmv0-elimination

寄存器分配：
  register-coalescer
  machine-scheduler
  greedy
  virtregrewriter
  riscv-insert-vsetvli
  riscv-dead-defs
  greedy
  virtregrewriter

post-RA：
  machine-cp
  machinelicm
  riscv-copyelim
  postra-machine-sink
  shrink-wrap
  prolog-epilog
  machine-latecleanup
  branch-folder
  tailduplication
  machine-cp
  postrapseudos
  riscv-post-ra-expand-pseudo
  kcfi
  riscv-load-store-opt
  postmisched
  block-placement
  riscv-late-branch-opt
  riscv-indirect-branch-tracking
  branch-relaxation
  riscv-make-compressible
  stack-frame-layout
  riscv-move-merge
  riscv-push-pop-opt

伪指令展开与输出：
  riscv-expand-pseudo
  riscv-expand-atomic-pseudo
  riscv-asm-printer
```

这张表有三个容易漏掉的事实。

第一，**`greedy` 出现了两次**。第一次是主分配，后面跟着 `virtregrewriter`；第二次出现在 spill/rewrite 之后。`-stop-after=greedy` 停的是第一次 `greedy` 之后、`virtregrewriter` 之前，所以 MIR 里仍然有虚拟寄存器。要拿物理寄存器快照，用 `-stop-before=riscv-copyelim` 或 `-stop-after=prolog-epilog`。

第二，**`riscv-insert-vsetvli` 在 RA 之后**。向量指令在 isel 阶段只有 `PseudoVLE32_V_M1` 这类伪指令，`vsetvli` 是后插入的。第 23 章会展开这条链。

第三，**`riscv-expand-pseudo` 不是唯一的伪指令展开点**。pre-RA 有 `riscv-prera-expand-pseudo`，post-RA 有 `riscv-post-ra-expand-pseudo`，最后还有 `riscv-expand-pseudo` 和 `riscv-expand-atomic-pseudo`。哪条伪指令在哪一站展开，取决于它是否依赖物理寄存器、栈帧或最终布局。

这些 pass 的装配点在 `RISCVTargetMachine.cpp` 的 `RISCVPassConfig`：

- `addIRPasses()`：原子展开、gather/scatter、交错访存、CodeGenPrepare。
- `addPreISel()`：常量提升、barrier、global merge。
- `addInstSelector()`：`createRISCVISelDag(...)`。
- `addMachineSSAOptimization()`：VL optimizer、vector peephole、fold mem offset、通用 machine SSA 优化、`riscv-opt-w-instrs`。
- `addPreRegAlloc()`：pre-RA 伪指令展开、base offset 合并、CSR/VXRM 插入、VMV0 消除。
- `addPostRegAlloc()`：冗余 copy 消除。
- `addPreEmitPass()`：copy propagation、late branch opt、indirect branch tracking、branch relaxation、make compressible。
- `addPreEmitPass2()`：move merge、push/pop 优化、`riscv-expand-pseudo`、原子伪指令展开。

### 3.2 MIR 语法：一份 MachineFunction 的序列化

MIR 文件由 YAML 头和若干函数块组成。真实结构如下（节选）：

```text
--- |
  target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
  target triple = "riscv64"
  define i64 @mul_add(i64 %a, i64 %b) {
    %m = mul i64 %a, %b
    %r = add i64 %m, 7
    ret i64 %r
  }
...
---
name:            mul_add
alignment:       4
tracksRegLiveness: true
isSSA:           true
noVRegs:         false
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 3, class: gpr, preferred-register: '', flags: [  ] }
liveins:
  - { reg: '$x10', virtual-reg: '%0' }
  - { reg: '$x11', virtual-reg: '%1' }
frameInfo:
  stackSize:       0
  adjustsStack:    false
  hasCalls:        false
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11

    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %2:gpr = MUL %0, %1
    %3:gpr = ADDI killed %2, 7
    $x10 = COPY %3
    PseudoRET implicit $x10
```

逐项读：

- **`--- |` 与 `...`**：YAML 文档分隔符。第一个 `--- |` 包含模块头（datalayout、triple、函数签名）；每个函数块以 `---` 开始，以 `...` 结束。
- **`name`**：函数名，必须与模块里的 `define` 对应。
- **`alignment`**：函数对齐，单位是字节。
- **`tracksRegLiveness`**：是否跟踪寄存器活性。为 `true` 时，操作数上的 `killed`、`dead` 才有意义。
- **`isSSA` / `noVRegs`**：是否仍是 SSA 形式、是否已经没有虚拟寄存器。RA 前通常是 `isSSA: true, noVRegs: false`；RA 后是 `isSSA: false, noVRegs: true`。
- **`registers`**：虚拟寄存器列表，每项有 `id`、`class`、可选 `preferred-register`、`flags`。
- **`liveins`**：函数入口的活跃寄存器；`virtual-reg` 把物理寄存器映射到虚拟寄存器。
- **`frameInfo`**：栈大小、是否调整栈、是否有调用等。
- **`stack`**：栈槽列表，包括 spill slot、callee-saved 位置。
- **`body`**：基本块与机器指令。

函数体里，虚拟寄存器写成 `%0:gpr`，物理寄存器写成 `$x10`。操作数标志是 MIR 的重要信息：

| 标志 | 含义 | 真实例子 |
| --- | --- | --- |
| `killed` | 这是该寄存器最后一次使用 | `%3:gpr = ADDI killed %2, 7` |
| `implicit` | 指令隐式读取该寄存器 | `PseudoRET implicit $x10` |
| `implicit-def` | 指令隐式定义该寄存器 | `implicit-def dead $x1` |
| `dead` | 定义后不再使用 | `implicit-def dead $x1` |
| `renamable` | 允许重命名的物理寄存器 | `renamable $x12 = ANDI killed renamable $x12, 1` |
| `frame-setup` | prologue 中的指令 | `$x2 = frame-setup ADDI $x2, -16` |
| `frame-destroy` | epilogue 中的指令 | `$x1 = frame-destroy LD $x2, 8` |
| `target-flags(...)` | 目标相关标志 | `PseudoCALL target-flags(riscv-call) @g` |

`::` 后面是机器内存操作数。两个真实例子：

```text
SD %1, %0, 0 :: (store (s64) into %ir.p)
frame-setup SD killed $x1, $x2, 8 :: (store (s64) into %stack.0)
```

第一行来自 isel 后的 `ldst`：`%ir.p` 指向 IR 里的指针参数，说明这条 store 对应 IR 的 `store i64 %v, ptr %p`。第二行来自 prologue：`%stack.0` 是栈槽，`(store (s64) into %stack.0)` 说明这是把 `ra` spill 到栈槽。

`::` 不是注释，也不是装饰。它是机器内存操作数的语法：括号里描述访问类型（`load`/`store`）、宽度（`s64`）、地址来源（`%ir.p` 或 `%stack.0`）。去掉它，MIR 解析器会把操作数当成普通寄存器/立即数，验证器会报错。

### 3.3 停靠开关：六个常用选项

| 选项 | 作用 | 输入 | 典型用途 |
| --- | --- | --- | --- |
| `-stop-after=<pass>` | 在指定 pass 之后停止 | IR 或 MIR | 取某一站之后的快照 |
| `-stop-before=<pass>` | 在指定 pass 之前停止 | IR 或 MIR | 取某一站之前的快照 |
| `-start-before=<pass>` | 从指定 pass 开始跑剩余管线 | IR 或 MIR | 跳过前面的 pass，复用已有 MIR |
| `-run-pass=<pass>` | 只跑一个 pass | MIR | 单 pass 实验 |
| `-print-after-all` | 每个 pass 后打印 IR/MIR | IR 或 MIR | 追全程变化 |
| `-simplify-mir` | 省略默认字段 | MIR 输出 | 让快照更短 |

真实行为要用实验确认，不能只看帮助文本。三个已经实测的细节：

1. `-stop-after=riscv-isel` 的 MIR 头部可能仍写 `selected: false`，但函数体里已经是 `MUL`/`ADDI`。这个字段在 `-stop-after` 生成的 MIR 里可能是陈旧值，判断是否选指要看函数体。
2. `-stop-after=greedy` 的 MIR 仍然有虚拟寄存器。原因是 `greedy` 之后还有 `virtregrewriter`；要物理寄存器快照，用 `-stop-before=riscv-copyelim` 或 `-stop-after=prolog-epilog`。
3. `-start-before=riscv-isel` 可以直接对 `.ll` 使用，`llc` 会从选指开始跑剩余管线并输出汇编。它跳过了前面的 IR pass，适合验证“同样的 IR 在选指之后是否还出问题”。

`-print-after-all` 的输出非常大。真实第一行是：

```text
*** IR Dump After ObjC ARC contraction (objc-arc-contract) ***
```

它会在每个 pass 后打印当前 IR/MIR。调试单个函数时，优先用 `-print-after=<pass>` 或 `-filter-print-funcs=<name>` 缩小范围；`-print-after-all` 适合确认 pass 顺序，不适合逐行阅读。

`-simplify-mir` 会省略默认字段。真实头部从：

```text
name:            mul_add
alignment:       4
exposesReturnsTwice: false
legalized:       false
regBankSelected: false
selected:        false
failedISel:      false
tracksRegLiveness: true
...
```

变成（节选，后面还有 `noVRegs`、`hasFakeUses` 与 `registers` 等字段）：

```text
name:            mul_add
alignment:       4
tracksRegLiveness: true
noPhis:          false
isSSA:           true
```

省略的字段不是“变成了 false”，而是“使用默认值”。读简化 MIR 时不要因为字段缺失就推断某个状态。
### 3.4 手写 MIR 跑单 pass

`-run-pass` 是本章最重要的实验工具。它让你在一段手写 MIR 上只跑一个 pass，观察这个 pass 到底改了什么，而不被前后管线干扰。

下面是一段最小 MIR。它的函数体只有两条 `ADDI`，其中 `$x11` 的结果没有被使用：

```text
--- |
  target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
  target triple = "riscv64-unknown-elf"
  define i64 @f(i64 %a) {
    ret i64 %a
  }
...
---
name:            f
alignment:       4
tracksRegLiveness: true
liveins:
  - { reg: '$x10' }
body:             |
  bb.0:
    liveins: $x10

    $x11 = ADDI $x10, 1
    $x10 = ADDI $x10, 2
    PseudoRET implicit $x10
...
```

命令：

```powershell
llc -mtriple=riscv64 -run-pass=dead-mi-elimination hand.mir -o -
```

真实的函数体会从：

```text
body:             |
  bb.0:
    liveins: $x10

    $x11 = ADDI $x10, 1
    $x10 = ADDI $x10, 2
    PseudoRET implicit $x10
```

变成：

```text
body:             |
  bb.0:
    liveins: $x10

    $x10 = ADDI $x10, 2
    PseudoRET implicit $x10
```

`dead-mi-elimination` 删掉了 `$x11 = ADDI $x10, 1`，因为它定义的物理寄存器没有任何使用，也没有隐式副作用。这个实验说明两件事：

1. **MIR 是可执行的输入**。只要函数签名、`liveins`、`body` 对齐，`llc` 就能把它当成一个 `MachineFunction` 读进来。
2. **单 pass 的作用域很窄**。`dead-mi-elimination` 不会帮你做寄存器分配、不会补 prologue，也不会展开 `PseudoRET`。它只做死指令消除。

`-run-pass` 也有明确的失败模式。把 pass 名写错：

```powershell
llc -mtriple=riscv64 -run-pass=does-not-exist hand.mir -o -
```

真实输出：

```text
llc.exe: error: run-pass does-not-exist is not registered.
EXIT=1
```

这里的错误是“pass 名未注册”，不是 MIR 语法错误。排查时先确认 pass 名来自 `-debug-pass=Structure` 的真实输出，再确认它是否可以被单独运行。

### 3.5 MIR 与 IR 的语义差异

MIR 不是“另一种写法的 IR”。它和 IR 至少有六处语义差异：

| 维度 | LLVM IR | MIR |
| --- | --- | --- |
| 值 | SSA 值 `%m`、`%r` | 虚拟寄存器 `%2:gpr`、物理寄存器 `$x10` |
| 类型 | 每个值有明确类型（`i64`、`double`） | 类型主要落在虚拟寄存器的寄存器类上；物理寄存器没有 IR 类型 |
| 活跃性 | 由 SSA 支配关系隐含 | 显式 `liveins`、`killed`、`implicit`、`dead` |
| 指令 | 目标无关的 IR 指令 | 目标机器指令与伪指令（`PseudoCALL`、`PseudoRET`） |
| 内存 | `load`/`store` + `align` | 机器指令 + `:: (load/store ...)` 机器内存操作数 |
| 栈 | 没有栈槽概念 | `stack:` 列表、`%stack.0`、spill slot、callee-saved 位置 |

一个具体对照。IR 里：

```llvm
%m = mul i64 %a, %b
%r = add i64 %m, 7
ret i64 %r
```

isel 后的 MIR 里：

```text
%1:gpr = COPY $x11
%0:gpr = COPY $x10
%2:gpr = MUL %0, %1
%3:gpr = ADDI killed %2, 7
$x10 = COPY %3
PseudoRET implicit $x10
```

IR 的 `%a`/`%b` 变成 `COPY $x10`/`COPY $x11` 之后的虚拟寄存器；`mul` 变成 `MUL`；`add` 变成 `ADDI`；`ret` 变成 `PseudoRET`。`killed` 标出 `%2` 的最后一次使用，`implicit $x10` 表示返回指令隐式读取返回值。

RA 后的 MIR 再变一次：

```text
renamable $x10 = MUL killed renamable $x10, killed renamable $x11
renamable $x10 = ADDI killed renamable $x10, 7
PseudoRET implicit $x10
```

虚拟寄存器消失了，`%0`/`%1`/`%2`/`%3` 被合并/分配到 `$x10`/`$x11`；`renamable` 表示这些物理寄存器可以被重命名；`killed` 仍然保留，供后续 pass 做 liveness 分析。

最后是带栈帧的 `caller`：

```text
bb.0 (%ir-block.0):
  liveins: $x10, $x1

  $x2 = frame-setup ADDI $x2, -16
  frame-setup CFI_INSTRUCTION def_cfa_offset 16
  frame-setup SD killed $x1, $x2, 8 :: (store (s64) into %stack.0)
  frame-setup CFI_INSTRUCTION offset $x1, -8
  $x11 = ADDI $x0, 3
  PseudoCALL target-flags(riscv-call) @mul_add, csr_ilp32d_lp64d, implicit-def dead $x1, implicit $x10, implicit killed $x11, implicit-def $x2, implicit-def $x10
  $x1 = frame-destroy LD $x2, 8 :: (load (s64) from %stack.0)
  frame-destroy CFI_INSTRUCTION restore $x1
  $x2 = frame-destroy ADDI $x2, 16
  frame-destroy CFI_INSTRUCTION def_cfa_offset 0
  PseudoRET implicit $x10
```

这一段把 MIR 的“机器层语义”暴露得很完整：`frame-setup`/`frame-destroy` 标记 prologue/epilogue；`%stack.0` 是 spill slot；`csr_ilp32d_lp64d` 是调用约定；`implicit-def dead $x1` 表示调用破坏了 `ra`；`target-flags(riscv-call)` 告诉 MC 层这条调用要用 RISC-V call 的重定位/fixup 规则处理。

## 可跑实验

### 实验 1：打印并分组 pass 管线

目的：确认 pass 顺序与站位分组，尤其是 `greedy` 出现两次、`riscv-insert-vsetvli` 在 RA 之后。

命令：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 -debug-pass=Structure first.ll -o NUL 2>&1 |
  Select-String -Pattern 'riscv-|greedy|virtregrewriter|prolog-epilog|register-coalescer|machine-scheduler|branch-relaxation'
```

预期输出：包含 3.1 节列出的名字。重点核对：

- `riscv-isel` 在 `finalize-isel` 之前。
- `riscv-insert-vsetvli` 在 `greedy`/`virtregrewriter` 之后。
- `prolog-epilog` 在 `riscv-copyelim` 之后、`riscv-expand-pseudo` 之前。
- `riscv-asm-printer` 是最后一个 RISC-V pass。

解读：这些名字的先后就是站位顺序。两个 `greedy` 是同一个 pass 在管线里出现两次，不能拿第一次的位置当 RA 后的快照。

失败排查：`-debug-pass=Structure` 的输出走 stderr。PowerShell 里必须加 `2>&1`，否则 `Select-String` 可能看不到内容。

### 实验 2：比较 isel 后与 RA 后的 MIR

目的：看到虚拟寄存器到物理寄存器的切换，以及栈帧的出现。

命令：

```powershell
llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -O2 -stop-after=riscv-isel first.ll -o isel.mir
llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -O2 -stop-after=prolog-epilog first.ll -o ra.mir
```

预期输出：`isel.mir` 的 `mul_add` 函数体是 3.5 节第一段（虚拟寄存器 + `MUL`/`ADDI`）；`ra.mir` 的 `caller` 函数体是 3.5 节最后一段（物理寄存器 + 栈帧 + `PseudoCALL`）。

也可以看头部字段：

```text
isel.mir:
  isSSA:        true
  noVRegs:      false
  registers:    [ %0:gpr, %1:gpr, %2:gpr, %3:gpr ]

ra.mir:
  isSSA:        false
  noVRegs:      true
  registers:    []
```

解读：RA 之后 `registers: []`，说明没有虚拟寄存器了；`liveins` 里的 `virtual-reg` 变成空字符串。栈槽 `%stack.0` 出现，`frameInfo.stackSize` 变成 16。

失败排查：如果 `ra.mir` 仍然有 `%0:gpr`，说明停错了站。`-stop-after=greedy` 不是 RA 后快照；用 `-stop-after=prolog-epilog` 或 `-stop-before=riscv-copyelim`。

### 实验 3：手写 MIR，只跑一个 pass

目的：把“单 pass 输入/输出”跑通。

命令：

```powershell
$mir = Join-Path $env:TEMP "hand.mir"
$lines = @(
  '--- |'
  '  target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
  '  target triple = "riscv64-unknown-elf"'
  '  define i64 @f(i64 %a) {'
  '    ret i64 %a'
  '  }'
  '...'
  '---'
  'name:            f'
  'alignment:       4'
  'tracksRegLiveness: true'
  'liveins:'
  "  - { reg: '$x10' }"
  'body:             |'
  '  bb.0:'
  '    liveins: $x10'
  ''
  '    $x11 = ADDI $x10, 1'
  '    $x10 = ADDI $x10, 2'
  '    PseudoRET implicit $x10'
  '...'
)
[IO.File]::WriteAllText($mir, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
llc -mtriple=riscv64 -run-pass=dead-mi-elimination $mir -o -
```

预期输出：`$x11 = ADDI $x10, 1` 被删除，剩下 `$x10 = ADDI $x10, 2` 和 `PseudoRET implicit $x10`。

解读：`dead-mi-elimination` 只看指令的 def/use，不改控制流；`$x11` 的结果没有使用者，所以整条 `ADDI` 被删除。

失败排查：

- 报 YAML 解析错误：检查 `--- |` 和 `...` 是否成对，缩进是否被 PowerShell 的字符串拼接破坏。
- 报 `liveins` 不匹配：检查模块里的 `define` 参数个数与 `liveins` 是否一致。`define i64 @f(i64 %a)` 对应一个 `$x10`。
- 报 pass 未注册：确认 pass 名是 `dead-mi-elimination`，不是 `dead-mi-elim` 之类的缩写。

### 实验 4（边界）：`-stop-after=greedy` 不是 RA 后

目的：亲手撞一次“停错站”的坑。

命令：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=greedy first.ll -o greedy.mir
Select-String -LiteralPath greedy.mir -Pattern 'noVRegs|registers:'
```

真实结果：

```text
noVRegs:         false
registers:
  - { id: 0, class: gpr, ... }
  - { id: 1, class: gpr, ... }
```

解读：`greedy` 只做分配决策，真正的虚拟寄存器重写由后面的 `virtregrewriter` 完成。`-stop-after=greedy` 停在两者之间，所以仍然是虚拟寄存器。要物理寄存器快照，用：

```powershell
llc -mtriple=riscv64 -mattr=+m -O2 -stop-before=riscv-copyelim first.ll -o postra.mir
```

失败排查：如果 `postra.mir` 仍然有虚拟寄存器，检查 `riscv-copyelim` 是否真的在这条管线里；用 `-debug-pass=Structure` 确认名字拼写。

## 源码落点

- `RISCVTargetMachine.cpp`：`RISCVPassConfig::addIRPasses`、`addPreISel`、`addInstSelector`、`addMachineSSAOptimization`、`addPreRegAlloc`、`addPostRegAlloc`、`addPreEmitPass`、`addPreEmitPass2`。
- `RISCVSubtarget.h`：`enableMachineScheduler()` 返回 `true`；`enablePostRAScheduler()` 读取 `UsePostRAScheduler`。
- `RISCVInstrInfo.cpp`：`copyPhysReg`、`storeRegToStackSlot`、`loadRegFromStackSlot`、分支分析。
- `RISCVRegisterInfo.cpp`：spill/restore、callee-saved 处理。
- `RISCVExpandPseudoInsts.cpp`：`riscv-expand-pseudo` 的主体，展开 `PseudoLI`、`PseudoLA`、`PseudoTAIL` 等。
- `RISCVPostRAExpandPseudoInsts.cpp`：post-RA 伪指令展开。
- `RISCVExpandAtomicPseudoInsts.cpp`：原子伪指令展开。
- 核心源码：`llvm/include/llvm/CodeGen/MIRParser/MIRParser.h`（解析入口）、`.scratch/llvm-23.1.4-src/llvm/lib/CodeGen/MIRPrinter.cpp`、`MachineVerifier.cpp`。本机的 `llvm-23.1.4-src` 是裁剪副本，`lib/CodeGen/MIRParser/` 目录不在其中；需要读解析实现时，按 `lib/CodeGen/CMakeLists.txt` 的 `add_subdirectory(MIRParser)` 到同版本上游取。
- `RISCVAsmPrinter.cpp`：`runOnMachineFunction`、`emitFunctionBody`、`emitTargetFeaturePush/Pop`。

## 常见坑

1. **把 `selected: false` 当成没选指**。`-stop-after` 生成的 MIR 头部可能保留陈旧字段；看函数体里是否已经是 `MUL`/`ADDI`。
2. **用 `-stop-after=greedy` 取 RA 后快照**。`greedy` 后面还有 `virtregrewriter`；用 `-stop-before=riscv-copyelim` 或 `-stop-after=prolog-epilog`。
3. **把 pass 名写成 IR pass 名**。`-stop-after=riscv-isel` 是真实 pass 名；`-stop-after=isel` 不是。名字以 `-debug-pass=Structure` 的输出为准。
4. **以为 `-run-pass` 会跑完整管线**。它只跑一个 pass；需要的分析会重算，但前序 pass 产生的状态不会自动补齐。依赖前序状态的 pass 可能报错或给出无意义结果。
5. **手写 MIR 时 `liveins` 与函数签名不一致**。`define i64 @f(i64 %a)` 对应一个 `$x10`；多写或少写都会被验证器抓住。
6. **把 `-print-after-all` 当成逐行阅读工具**。输出量很大，先按 pass 名或函数名过滤；确认顺序时再用全量输出。
7. **把 `-simplify-mir` 省略的字段当成 false**。省略表示默认值，不表示状态改变。
8. **MIR 文件缺少 `--- |` 或 `...`**。YAML 解析器会直接报错，错误信息通常指向第一行，不要被误导到函数体。

## 自我检查

- [ ] 我能按顺序说出 `riscv-isel`、`finalize-isel`、`greedy`、`virtregrewriter`、`riscv-insert-vsetvli`、`prolog-epilog`、`riscv-expand-pseudo`、`riscv-asm-printer` 的相对位置。
- [ ] 我能解释为什么 `greedy` 在管线里出现两次，以及 `-stop-after=greedy` 为什么仍含虚拟寄存器。
- [ ] 我能读懂 MIR 的 `name`、`alignment`、`tracksRegLiveness`、`isSSA`、`noVRegs`、`liveins`、`body`。
- [ ] 我能解释 `%0:gpr`、`$x10`、`killed`、`implicit`、`dead`、`renamable`、`frame-setup` 的含义。
- [ ] 我能解释 `:: (store (s64) into %ir.p)` 与 `:: (store (s64) into %stack.0)` 的区别。
- [ ] 我能用 `-stop-after`、`-stop-before`、`-start-before`、`-run-pass` 各做一次快照或单 pass 实验。
- [ ] 我能手写一段 MIR 并用 `dead-mi-elimination` 删除一条死指令。
- [ ] 我能说出 MIR 与 IR 在值、类型、liveness、内存操作数、栈槽五个维度上的差异。

## 延伸阅读

- LLVM 官方文档：`llvm.org/docs/MIRLangRef.html`，MIR 的权威语法参考。
- LLVM 官方文档：`llvm.org/docs/CodeGenerator.html`，MachineFunction、MachineInstr、liveness 的背景。
- LLVM 官方文档：`llvm.org/docs/CommandGuide/llc.html`，`-stop-after`、`-stop-before`、`-run-pass`、`-print-after-all` 的选项说明。
- 同版本源码：`RISCVTargetMachine.cpp` 的 `RISCVPassConfig`、`RISCVExpandPseudoInsts.cpp`、`RISCVPostRAExpandPseudoInsts.cpp`。
- 同版本核心源码：`.scratch/llvm-23.1.4-src/llvm/lib/CodeGen/MIRPrinter.cpp`、`MachineVerifier.cpp`，以及 `llvm/include/llvm/CodeGen/MIRParser/MIRParser.h`。

[上一章：ISA 建模](./02-isa-modeling.md) | [下一章：练习 A：一次编译的六段解剖](./practice-a-pipeline-dissection.md)