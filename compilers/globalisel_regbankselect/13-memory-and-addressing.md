# Ch 13 — 内存操作与地址 bank

> **目标**：把 load/store 的两个 bank 问题拆开：地址在哪个 bank、数据在哪个 bank。学完你能解释为什么 AArch64 的地址总是 GPR、为什么 `load i64` 有时落在 FPR、`G_PTR_ADD` 和 `G_GEP` 在 23.1.4 里是什么关系、对齐和偏移为什么不会改变 bank，以及 Greedy 的 `G_LOAD` 64 位 alternative mapping 在什么条件下才有意义。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测 MIR 与汇编）；AArch64 主 target，RISC-V 对照。所有内存 MIR 来自 `mem.ll` / `load-dual.ll` 的官方包输出。
>
> **证据约定**：标“实测”的 MIR/汇编是官方 23.1.4 输出；标“源码依据”的是 `AArch64RegisterBankInfo.cpp` / `IRTranslator.cpp` / `GenericOpcodes.td` 的结论。`G_GEP` 在 23.1.4 中不存在，本章明确纠正，不伪造带 `G_GEP` 的 MIR。

## 为什么需要这个？

内存指令是 RegBankSelect 里最容易“看起来懂、实际读错”的一类，因为它同时涉及两个 bank：

- **地址操作数**：几乎总是整数指针，AArch64 上必须是 GPR64。
- **数据操作数**：可能是整数（GPR），也可能是浮点/向量（FPR）。

如果你只盯着 `G_LOAD` 的目的 vreg，很容易忽略地址；如果你只盯着地址，又会奇怪为什么同一个 `G_LOAD` 在另一个函数里跑到了 FPR。更麻烦的是，对齐、偏移、寻址模式这些“内存语义”并不改变 bank，却会改变 InstructionSelect 能折叠出什么指令。调试时把“bank 选错”和“寻址模式没匹配上”混在一起，就会浪费很多时间。

这一章要把三件事分开：

1. 地址 bank：谁产生地址，谁就是 GPR。
2. 数据 bank：由类型、MMO、use 分析共同决定。
3. 对齐/偏移：属于 MMO 和寻址模式，不属于 bank 选择。

## 先建立直觉

把一条 load 想成快递取件：

```text
       地址（门牌号）                数据（包裹）
       GPR64                        GPR 或 FPR
          │                             │
          ▼                             ▼
   G_PTR_ADD / G_FRAME_INDEX      G_LOAD / G_STORE
          │                             │
          └──────────┬──────────────────┘
                     ▼
              AArch64 寻址模式
```

门牌号一定是整数，所以地址永远走 GPR。包裹可能是文件（整数）、易碎品（浮点）或一箱货（向量），所以数据 bank 要看类型和用途。

```text
%addr:gpr(p0) = G_GLOBAL_VALUE @g_f64
%v:fpr(f64)   = G_LOAD %addr(p0)
```

这条指令里，`%addr` 是 GPR，`%v` 是 FPR。它们不是“同一条指令必须同 bank”，而是两个独立的 `ValueMapping`。

## 核心概念

### 1. `G_LOAD` / `G_STORE` 的 operand 布局

`G_LOAD` 是 `(dst, addr)`，`G_STORE` 是 `(src, addr)`；两者都额外挂一个 `MachineMemOperand`（MMO）。

- `G_LOAD`：operand 0 是数据 def，operand 1 是地址 use。
- `G_STORE`：operand 0 是数据 use，operand 1 是地址 use。
- 两者都额外挂一个 `MachineMemOperand`（MMO），里面记录地址空间、对齐、别名、是否 volatile/atomic 等信息。

RegBankSelect 的 mapping 必须给每个寄存器 operand 一个 `ValueMapping`。所以一条 `G_LOAD` 的 mapping 至少有两项：数据 bank + 地址 bank。

### 2. 地址 bank 在 AArch64 上永远是 GPR

AArch64 的 `G_LOAD` alternative mapping 写得很直白：数据可以用 `PMI_FirstGPR` 或 `PMI_FirstFPR`，但地址固定是 `getValueMapping(PMI_FirstGPR, TypeSize::getFixed(64))`。RISC-V 也一样：`G_LOAD` 的 operand 1 固定用 `GPRValueMapping`（RV64 是 64 位，RV32 是 32 位）。

这不是“地址碰巧是 GPR”，而是 target 的寻址硬件决定的。AArch64 的 load/store 地址基址寄存器是 X 寄存器；RISC-V 是 X 寄存器。地址 bank 没有 GPR/FPR 二选一。

### 3. 数据 bank 由类型、MMO 和 use 共同决定

同样是一条 64 位 load，`load i64` 默认走 GPR，`load double` 走 FPR。但事情不止类型：

- **MMO 的 IR 类型**：从 `@g_f64`（double 全局）加载，`isLoadFromFPType` 会看到 global 的值类型是 double，直接判 FPR。
- **use 分析**：如果 load 的结果被 `G_FADD` 或 `bitcast` 到浮点后使用，AArch64 会倾向于把 load 直接放 FPR，避免一次跨 bank 复制。
- **atomic**：源码明确“Atomics always use GPR destinations”，不再往 FPR 推。
- **小于 32 位**：走 `CustomMappingID`，在 `applyMapping` 里插入 `G_ANYEXT` / `G_TRUNC`，因为 AArch64 没有 8/16 位 GPR 子寄存器。

所以“64 位整数 load 一定在 GPR”是错的。实测 `load_i64_as_f64` 的 `%1` 就是 `fpr(i64)`。

### 4. `G_PTR_ADD` 与 `G_GEP`：23.1.4 只有前者

brief 里列了 `G_GEP`，但 23.1.4 的 `GenericOpcodes.td` 里没有 `G_GEP`。GEP 在 IRTranslator 的 `translateGetElementPtr` 里被翻译成 `G_PTR_ADD` 加常数/移位。

实测 `getelementptr i32, ptr %p, i64 %idx` 生成：

```text
%7:gpr(i64) = G_CONSTANT i64 2
%3:gpr(i64) = G_SHL %1, %7(i64)
%4:gpr(p0) = G_PTR_ADD %0, %3(i64)
%6:gpr(i32) = G_LOAD %4(p0)
```

- 索引先左移 2（乘 4 字节），得到字节偏移。
- `G_PTR_ADD` 把基址和偏移相加，结果仍是指针/GPR。
- selector 最后把 `G_SHL` + `G_PTR_ADD` 折叠成 `[x0, x1, lsl #2]`。

如果你在旧 MIR 里看到 `G_GEP`，那是旧版本或别的上下文；不要拿它解释 23.1.4 的输出。

### 5. `G_FRAME_INDEX` / `G_GLOBAL_VALUE` 是地址生产者

两者都只产生地址，不产生数据：

```text
%0:gpr(p0) = G_FRAME_INDEX %stack.0.a
%4:gpr(p0) = G_GLOBAL_VALUE @g_i64
```

AArch64 的 `getInstrMapping` 对 `G_FRAME_INDEX` 有专门分支：operand 0 是唯一需要 bank 的 operand，映射到 GPR；operand 1（栈对象索引）不是寄存器。`G_GLOBAL_VALUE` 没有专门分支，走默认逻辑：结果是 pointer 标量，落到 GPR。

### 6. 对齐和偏移不改变 bank

- **对齐**：IR 的 `align 8` 进入 MMO，影响 load/store 的合法性、是否允许 unaligned、以及 selector 能选什么指令；它不参与 `ValueMapping` 的 bank 选择。
- **偏移**：常量偏移在 MIR 里通常是 `G_CONSTANT` + `G_PTR_ADD`，或者直接留在 MMO/寻址模式里；它也不改变 bank。
- **寄存器压力**：把偏移物化成 `G_CONSTANT` 会多一个 GPR vreg，但这是指令选择/寻址模式的问题，不是 bank 问题。

实测 `store_both` 的汇编：

```asm
str x1, [x0]
str d0, [x0, #8]
```

MIR 里是 `G_CONSTANT i64 8` + `G_PTR_ADD`，但 selector 把偏移折进了 `[x0, #8]`。RegBankSelect 只看到两个 GPR 地址操作数，不知道最终会折成立即数偏移。

## 底层机制

### AArch64 的默认猜测

`getInstrMapping` 的通用循环先给每个操作数一个初值：

```cpp
if (Ty.isVector())
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else if (isPreISelGenericFloatingPointOpcode(Opc) ||
         (MO.isDef() && onlyDefinesFP(MI, MRI, TRI)) ||
         (MO.isUse() && onlyUsesFP(MI, MRI, TRI)) ||
         Ty.getSizeInBits() > 64)
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else
  OpRegBankIdx[Idx] = PMI_FirstGPR;
```

对 `G_LOAD`，地址是 pointer（≤64 位标量），所以初值是 GPR；数据按类型初值可能是 GPR 或 FPR。然后进入 `G_LOAD` 专属分支做微调。

### `G_LOAD` 的微调顺序

源码的顺序很重要：

1. **数据初值不是 GPR**：`Cost = 2`，直接结束。也就是“向量/浮点数据在 FPR，load 稍贵”。
2. **atomic**：强制 GPR；如果小于 32 位，走 `CustomMappingID`。
3. **`isLoadFromFPType`**：看 MMO 的 IR 值类型，如果是 FP/FP vector，数据改 FPR。
4. **use 分析**：如果结果被 FP 指令（或 `prefersFPUse`、带 FP 约束的 PHI）使用，数据改 FPR。
5. **小于 32 位**：走 `CustomMappingID`，`applyMapping` 里插 `G_TRUNC`。

这解释了三个实测现象：

- `load double` → 第 3 步直接 FPR。
- `load i64` 但 bitcast 到 double → 第 4 步 use 分析推到 FPR。
- `load i32` → 默认 GPR；如果 <32 位才走 custom。

### `G_STORE` 的微调

`G_STORE` 的顺序：

1. 如果数据初值是 GPR，看它的定义指令是否 `onlyDefinesFP`；是则改 FPR。
2. 如果数据是标量且 <32 位，走 `CustomMappingID`，`applyMapping` 里插 `G_ANYEXT`。

实测 `store_both`：

```text
%1:gpr(i64) = COPY $x1
%2:fpr(f64) = COPY $d0
G_STORE %1(i64), %0(p0)
G_STORE %2(f64), %4(p0)
```

第一个 store 的数据是 GPR，第二个是 FPR，地址都是 GPR。`G_STORE` 没有 def，mapping 只描述两个 use。

### `G_LOAD` 的 64 位 alternative mapping

AArch64 只对 64 位、恰好 2 个 operand 的 `G_LOAD` 提供 alternative：

```cpp
case TargetOpcode::G_LOAD: {
  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 64)
    break;

  // If the instruction has any implicit-defs or uses,
  // do not mess with it.
  if (MI.getNumOperands() != 2)
    break;

  InstructionMappings AltMappings;
  const InstructionMapping &GPRMapping = getInstructionMapping(
      /*ID*/ 1, /*Cost*/ 1,
      getOperandsMapping(
          {getValueMapping(PMI_FirstGPR, Size),
           // Addresses are GPR 64-bit.
           getValueMapping(PMI_FirstGPR, TypeSize::getFixed(64))}),
      /*NumOperands*/ 2);
  const InstructionMapping &FPRMapping = getInstructionMapping(
      /*ID*/ 2, /*Cost*/ 1,
      getOperandsMapping(
          {getValueMapping(PMI_FirstFPR, Size),
           // Addresses are GPR 64-bit.
           getValueMapping(PMI_FirstGPR, TypeSize::getFixed(64))}),
      /*NumOperands*/ 2);
  AltMappings.push_back(&GPRMapping);
  AltMappings.push_back(&FPRMapping);
  return AltMappings;
}
```

要点：

- 只有 64 位；32/128 位不提供。
- 只有 2 个 operand；带 implicit-def/use 的 load 直接放弃。
- 两个 mapping 的 cost 都是 1，区别在数据 bank；地址始终 GPR64。
- Greedy 会在两者之间比较 repair 代价；Fast 只用默认 mapping，不会看 alternatives。

在 `load_dual.ll` 的实测里，Fast 已经把 `%1` 放进了 FPR：

```text
%1:fpr(i64) = G_LOAD %0(p0) :: (load (i64) from %ir.p)
%7:gpr(i64) = COPY %1(i64)
%3:gpr(i64) = G_ADD %7, %2
...
%4:fpr(f64) = G_BITCAST %1(i64)
```

所以 `-regbankselect-greedy` 的输出与 Fast **逐字节相同**（本章实测 SHA-256 相同）。这不代表 alternative 没用，而是默认的 use 分析已经选中了 FPR；alternative 是给“默认分析看不准、Greedy 需要换边”的情况兜底。

## 例子一：整数/浮点 load 的两种数据 bank（实测）

```llvm
define i64 @load_i64(ptr %p) {
  %v = load i64, ptr %p, align 8
  ret i64 %v
}

define double @load_f64(ptr %p) {
  %v = load double, ptr %p, align 8
  ret double %v
}

define double @load_i64_as_f64(ptr %p) {
  %v = load i64, ptr %p, align 8
  %d = bitcast i64 %v to double
  ret double %d
}
```

MIR：

```text
name: load_i64
  %0:gpr(p0) = COPY $x0
  %1:gpr(i64) = G_LOAD %0(p0) :: (load (i64) from %ir.p)
  $x0 = COPY %1(i64)

name: load_f64
  %0:gpr(p0) = COPY $x0
  %1:fpr(f64) = G_LOAD %0(p0) :: (load (f64) from %ir.p)
  $d0 = COPY %1(f64)

name: load_i64_as_f64
  %0:gpr(p0) = COPY $x0
  %1:fpr(i64) = G_LOAD %0(p0) :: (load (i64) from %ir.p)
  %2:fpr(f64) = G_BITCAST %1(i64)
  $d0 = COPY %2(f64)
```

三个函数，地址都是 `gpr(p0)`；数据分别是 `gpr(i64)`、`fpr(f64)`、`fpr(i64)`。第三个最值得注意：它的 IR 类型是 i64，但 use 是 bitcast 到 double，AArch64 直接把 load 放 FPR，省掉一次 GPR→FPR 复制。

## 例子二：GEP 变成 G_PTR_ADD（实测）

```llvm
define i32 @gep_load(ptr %p, i64 %idx) {
  %q = getelementptr i32, ptr %p, i64 %idx
  %v = load i32, ptr %q, align 4
  ret i32 %v
}
```

MIR：

```text
%0:gpr(p0) = COPY $x0
%1:gpr(i64) = COPY $x1
%7:gpr(i64) = G_CONSTANT i64 2
%3:gpr(i64) = G_SHL %1, %7(i64)
%4:gpr(p0) = G_PTR_ADD %0, %3(i64)
%6:gpr(i32) = G_LOAD %4(p0) :: (load (i32) from %ir.q)
$w0 = COPY %6(i32)
```

没有 `G_GEP`。索引乘 4 通过 `G_SHL` 完成；`G_PTR_ADD` 的 def 和两个 use 都是 GPR。最终汇编：

```asm
ldr w0, [x0, x1, lsl #2]
```

selector 把 `G_SHL` + `G_PTR_ADD` + `G_LOAD` 折成一条带移位索引的 load。RegBankSelect 只负责保证地址链在 GPR；折叠是 ISel 的事。

## 例子三：栈、全局与 FPR 全局 load（实测）

```llvm
define i64 @stack_and_global() {
  %a = alloca i64, align 8
  store i64 7, ptr %a, align 8
  %v = load i64, ptr %a, align 8
  %g = load i64, ptr @g_i64, align 8
  %r = add i64 %v, %g
  ret i64 %r
}

define double @global_fp() {
  %g = load double, ptr @g_f64, align 8
  %r = fadd double %g, 1.0
  ret double %r
}
```

MIR：

```text
name: stack_and_global
  %0:gpr(p0) = G_FRAME_INDEX %stack.0.a
  %1:gpr(i64) = G_CONSTANT i64 7
  G_STORE %1(i64), %0(p0) :: (store (i64) into %ir.a)
  %2:gpr(i64) = G_LOAD %0(p0) :: (load (i64) from %ir.a)
  %4:gpr(p0) = G_GLOBAL_VALUE @g_i64
  %3:gpr(i64) = G_LOAD %4(p0) :: (load (i64) from @g_i64)
  %5:gpr(i64) = G_ADD %2, %3
```

`G_FRAME_INDEX` 和 `G_GLOBAL_VALUE` 都产出 GPR 指针；数据 bank 按 load 的类型走。`global_fp` 的 `G_LOAD` 是 `%0:fpr(f64) = G_LOAD %1:gpr(p0)`，因为 MMO 的 IR 值类型是 double（`isLoadFromFPType` 直接判定），不需要靠 use 分析。

## 对比与取舍

| 选择 | 优点 | 代价 | 适用 |
| --- | --- | --- | --- |
| load 数据放 GPR | 整数运算免费，地址链同 bank | 浮点 use 需要跨 bank COPY | 纯整数、位运算 |
| load 数据放 FPR | 浮点 use 免费 | 整数 use 需要跨 bank COPY | 纯浮点、bitcast 到 FP |
| 地址放 GPR | 硬件要求，selector 可直接匹配 | 无 | 所有 AArch64 load/store |
| Greedy 选 `G_LOAD` FPR alternative | 可在混合 use 下换边 | 仍是局部代价，可能增加另一侧 COPY | 64 位、无 implicit 操作数 |

RegBankSelect 的取舍是局部的：它不知道这个 load 后续会不会被 spill，也不知道 addressing mode 会不会折叠。它只根据当前 mapping 的局部代价和 repair 代价选一个 bank。

## 踩雷集锦

1. **以为指针类型会落到 FPR**  
   指针是标量整数地址，AArch64/RISC-V 都走 GPR。`p0` 不会因为“不是整数语义”而进 FPR。

2. **以为 `load i64` 一定在 GPR**  
   实测 `load_i64_as_f64` 的 `%1` 是 `fpr(i64)`。MMO 类型和 use 分析都能把数据推到 FPR。

3. **在 23.1.4 里找 `G_GEP`**  
   没有。GEP 被翻译成 `G_SHL`/`G_CONSTANT` + `G_PTR_ADD`。看到旧 MIR 里的 `G_GEP`，先确认版本。

4. **把对齐/偏移当成 bank 选择因素**  
   `align 8` 进 MMO，偏移进 `G_CONSTANT`/`G_PTR_ADD` 或寻址模式；它们不影响 `ValueMapping` 的 bank。bank 只看类型、约束和 use。

5. **以为 Greedy 一定会改变 `G_LOAD` 的 bank**  
   `load_dual.ll` 的 Fast/Greedy 输出逐字节相同，因为默认 use 分析已经选了 FPR。alternative 只在默认 mapping 不是最优时才有机会翻盘。

## 进阶

1. **Atomic load/store**  
   源码强制 atomic 数据走 GPR。构造一个 `atomic load i64`，观察 `isAtomic()` 分支如何覆盖 use 分析。

2. **Indexed load/store**  
   `G_INDEXED_LOAD` / `G_INDEXED_STORE` 有额外的 `newaddr` def/use；AArch64 对它们的 bank 处理与普通 load/store 不同。读 `getInstrMapping` 的 `G_INDEXED_*` 分支。

3. **小于 32 位的 load/store**  
   `CustomMappingID` 会在 `applyMapping` 里插 `G_ANYEXT` / `G_TRUNC`。构造 `load i8` / `store i16`，观察 RegBankSelect 之后多出来的 32 位 vreg。

## 动手练习

1. 对 `load_i64`、`load_f64`、`load_i64_as_f64` 逐条标注：地址 bank、数据 bank、MMO 类型、use 类型。
2. 把 `gep_load` 的索引类型改成 i32，观察 `G_SHL`/`G_PTR_ADD` 的 operand bank 是否变化。
3. 给 `load_dual.ll` 加一个 `atomic load i64`，预测数据 bank，再用 `-stop-after=regbankselect` 验证。
4. 用 `-regbankselect-greedy` 跑 `mem.ll`，和 Fast 输出做 `Compare-Object`；解释为什么完全相同或哪里不同。
5. 写一条 `store i8`，观察 `applyMapping` 插入的 `G_ANYEXT`，以及它是否引入新的跨 bank COPY。

## 本章重点整理

- 地址 bank 在 AArch64 上永远是 GPR64；数据 bank 由类型、MMO、use 分析决定。
- `G_LOAD` 的 64 位 alternative mapping 只有 GPR/FPR 两种数据 bank，地址固定 GPR64，且要求恰好 2 个 operand。
- `G_GEP` 在 23.1.4 不存在；GEP 变成 `G_SHL`/`G_CONSTANT` + `G_PTR_ADD`。
- `G_FRAME_INDEX` / `G_GLOBAL_VALUE` 只产生 GPR 地址。
- 对齐和偏移影响 MMO/寻址模式，不影响 bank；最终汇编可能把偏移折进 `[x0, #8]` 或 `[x0, x1, lsl #2]`。

## 自我检核

- [ ] 能说出 `G_LOAD` / `G_STORE` 每个 operand 的角色和 bank
- [ ] 能解释为什么 `load i64` 有时是 FPR
- [ ] 能说明 23.1.4 的 GEP 为什么不是 `G_GEP`
- [ ] 能描述 `G_LOAD` 64 位 alternative mapping 的三个限制
- [ ] 能解释对齐/偏移为什么不改变 bank

## 延伸阅读

### 源码

- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`G_LOAD` / `G_STORE` 的 `getInstrMapping` 分支、`isLoadFromFPType`、`getInstrAlternativeMappings` 的 `G_LOAD` case。
  - **学什么**：数据 bank 的判定顺序，以及 64 位 alternative 的限制。
  - **和本章的关联**：例子一、三和 alternative 源码都来自这里。

- **[IRTranslator.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp)**
  - **读哪里**：`translateGetElementPtr`。
  - **学什么**：GEP 如何变成常数/移位 + `G_PTR_ADD`。
  - **和本章的关联**：这是“没有 G_GEP”的直接依据。

- **[GenericOpcodes.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/Target/GenericOpcodes.td)**
  - **读哪里**：`G_LOAD`、`G_STORE`、`G_PTR_ADD`、`G_FRAME_INDEX`、`G_GLOBAL_VALUE` 的定义。
  - **学什么**：operand 顺序和类型；确认 23.1.4 没有 `G_GEP`。
  - **和本章的关联**：所有 MIR 读法的语法依据。

### 文档

- **[LLVM GlobalISel — Generic Opcode](https://llvm.org/docs/GlobalISel/GenericOpcode.html)**
  - **读哪里**：`G_LOAD` / `G_STORE` / `G_PTR_ADD` 的语义说明。
  - **学什么**：MMO 与显式 operand 的分工。
  - **和本章的关联**：对齐/偏移为什么不在显式 operand 里。

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`MachineMemOperand` 的打印形式、`:: (load ...)` 语法。
  - **学什么**：如何从 MIR 注释里读出 load/store 的 IR 类型和对齐。
  - **和本章的关联**：例子里的 `(load (f64) from @g_f64)` 就是 MMO 的可读形式。

下一章处理转换：bitcast 只是换 bank，fpext/fptosi 才是真的数值转换；同一个“看起来像转换”的 IR，在 MIR 里可能是 COPY，也可能是一条 target 指令。

→ [Ch 14 转换指令与跨 bank 语义](./14-conversions-and-cross-bank.md)