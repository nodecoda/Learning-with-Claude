# Ch 2 — PTX 契约（一）：虚拟 ISA、类型与状态空间

> **目标**：能逐行读懂 `llc` 生成的 PTX，并说清每个类型后缀、寄存器前缀、状态空间限定符的含义。

## 为什么需要这个？

PTX 不是「汇编的另一种写法」，它是一份**带类型系统和状态空间的虚拟 ISA 契约**。后端大量「奇怪」的 lowering（i8 变成 b16、指针要 `cvta`、kernel 参数走 `.param`）都是被这份契约逼出来的。读不懂契约，就只能背输出。

## 先建立直觉

把 PTX 想成一份**跨型号的装配说明书**：

- 它规定每个零件（值）的**尺寸**（`.b32`），但不规定用哪个工位（物理寄存器）。
- 它规定材料放在**哪个仓库**（state space：`.global` / `.shared` / `.local` / `.param`），但不规定仓库地址。
- 它规定动作的**语义**（`add.rn.f32` 四舍五入），但把「几个周期、能不能双发射」留给下家。

所以同一份 PTX 可以在完全不同的微架构上被重新排布。

## 一、类型：尺寸在前，语义在后

PTX 的类型标注有两层：

| 层 | 例子 | 含义 |
|---|---|---|
| 位宽 | `.b8` `.b16` `.b32` `.b64` `.b128` | 只讲尺寸，不讲解释 |
| 解释 | `.u32` `.s32` `.f32` `.f64` `.pred` | 无符号/有符号/浮点/谓词 |
| 向量 | `.v2` `.v4`（配 `.b32` 等） | 打包访问，要求对齐 |

关键规则：**寄存器按位宽分银行，指令按语义选后缀**。所以：

- `ld.global.b32` 读 4 字节，不管它是 int 还是 float。
- `add.rn.f32` 才是浮点加法；`add.s32` 是整数加法。
- 没有 `.b8` 的算术指令——i8 的加法在 PTX 层要提升到 16 位（Ch 9 实测）。

## 二、寄存器：无限、虚拟、按类编号

```ptx
.reg .pred 	%p<2>;
.reg .b32 	%r<9>;
.reg .b64 	%rd<11>;
```

读法：声明 2 个谓词槽（`%p0`、`%p1`）、9 个 32 位槽（`%r0..%r8`）、11 个 64 位槽（`%rd0..%rd10`）。

三个容易误解的点：

1. **`<N>` 是槽数，不是使用数**。编号 0 永远保留不用，所以实际可用是 `N-1` 个；`%r<9>` 对应 8 个虚拟寄存器。
2. **这些不是物理寄存器**。声明多少不影响硬件占用；`ptxas` 之后才变成 `REG:12` 这种数字。
3. **PTX 本身不是 SSA**。虚拟寄存器可以被重复赋值；LLVM 的输出接近 SSA，但那只是它自己的习惯。

前缀约定（来自 `NVPTXRegisterInfo.td`）：`%p` 谓词、`%rs` 16 位、`%r` 32 位、`%rd` 64 位、`%rq` 128 位。

## 三、状态空间：PTX 最独特的地方

PTX 的指针不是「一个地址」，而是「**空间 + 偏移**」：

| 空间 | 声明 | 谁分配 | 典型用途 |
|---|---|---|---|
| `.reg` | 函数内 | 编译器（虚拟） | 标量临时值 |
| `.param` | 函数签名 | ABI / 调用者 | kernel 参数、返回槽 |
| `.global` | 模块级 | 驱动 / 链接器 | 显存 |
| `.shared` | 模块/函数级 | 启动时按 block 分配 | block 内共享 |
| `.local` | 函数级 | 每线程栈 | 取地址的局部变量、spill |
| `.const` | 模块级 | 驱动 | 常量内存 |

指令必须带空间限定符，或者走 **generic** 指针：

```ptx
ld.global.b32 	%r6, [%rd1];    // 直接走 global 空间
ld.b32        	%r6, [%rd1];    // generic：运行时判断落在哪个空间
```

这就是 Ch 0 里 `-O0` 与 `-O2` 产物不同的根源：`-O0` 用 generic `ld.b32`，`-O2` 通过 `cvta.to.global` 把指针特化后再用 `ld.global.b32`。generic 访问在硬件上要走一次空间判定，能特化就该特化。

## 四、指令形状：目标在前，操作数在后

PTX 是典型的「目标在前」：

```ptx
add.rn.f32 	%r8, %r6, %r7;        // r8 = r6 + r7
mad.lo.s32 	%r5, %r3, %r4, %r2;   // r5 = r3*r4 + r2（只要低 32 位）
setp.ge.s32 	%p1, %r5, %r1;        // p1 = (r5 >= r1)
@%p1 bra 	$L__BB0_2;            // 谓词执行：p1 为真才跳
```

谓词执行是 GPU 的关键机制：`@%p1 bra` 不是「分支预测」，而是**逐线程的掩码跳转**。同一 warp 里不同线程可以走不同分支，硬件用掩码把不活跃的线程关掉——这就是 warp divergence 的指令层根源。

## 五、完整的类型/空间对照实验

看一段真实的 16 位与谓词代码（`widths.ll`，Ch 9 会完整拆）：

```ptx
ld.param.b8 	%rs1, [widths_param_1];   // i1 参数以 b8 传入
and.b16 	%rs2, %rs1, 1;            // 取出最低位
setp.ne.b16 	%p1, %rs2, 0;             // 变成谓词
add.s16 	%rs5, %rs3, %rs4;         // i8 加法提升到 16 位
cvt.u32.u16 	%r1, %rs5;                // 再扩到 32 位
and.b32 	%r2, %r1, 255;            // 截回 8 位语义
selp.b32 	%r5, %r4, 42, %p1;        // 谓词选择，不是分支
```

三个契约事实在这里同时出现：i1 不能存进通用寄存器（只能用 `.pred`）、i8 没有 `.b8` 算术（提升到 `.b16`）、条件选择优先用 `selp` 而不是分支。

## 六、特殊寄存器

GPU 的「线程身份」不是函数参数，而是特殊寄存器：

```ptx
mov.u32 %r2, %tid.x;      // 线程在 block 内的 x 坐标
mov.u32 %r3, %ctaid.x;    // block 坐标
mov.u32 %r4, %ntid.x;     // block 维度
```

对应 LLVM intrinsic：`llvm.nvvm.read.ptx.sreg.tid.x` 等。它们不是普通内存访问，因此不能被优化器随便提到分支外——除非它能证明特殊寄存器在函数内不变。

## 踩雷集锦

1. **把 `.b32` 当 int**：`.b32` 只是 4 字节；同一个槽可以装 float，取决于用它的指令。
2. **以为 `%r<9>` 表示 9 个可用寄存器**：编号 0 保留，实际 8 个。
3. **以为 generic load 和 global load 等价**：generic 多一次空间判定，性能不同（Ch 5 实测）。
4. **以为 `@%p bra` 是谓词化的普通分支**：它是逐线程掩码，语义上不是「整个 warp 一起跳或不跳」。
5. **在 PTX 层找 `i8` 算术**：没有；任何 i8 运算都会被提升，溢出语义靠后续 `and` 截断。

## 进阶：PTX 的「弱类型」边界

PTX 允许 `ld.global.b32` 读一个 float 对象，再用 `add.rn.f32` 解释它——类型检查主要在指令层，而不是对象层。这带来两个后果：

- 位cast 在 PTX 层几乎零成本（`mov.b32` 即可），所以 LLVM 的 `bitcast` 常常直接消失。
- 真正的类型错误会推迟到 `ptxas` 或运行期，调试时要回到 IR 层找原因。

## 动手练习

1. 手写一个 `.ll`，让 kernel 同时有 i1、i8、i16、float 参数，编译后标出每个参数用哪种 `.param` 类型。
2. 把同一个 `load` 分别放在 addrspace(1) 和 generic 指针上，对比 `-O0`/`-O2` 的 PTX。
3. 在 `add.ptx` 里数一数：哪些虚拟寄存器只被写一次、哪些被写多次？验证「PTX 不是 SSA」。

## 本章重点整理

- PTX 类型 = 位宽 + 语义，寄存器按位宽分银行。
- 状态空间是 PTX 的一等公民；generic 访问需要运行时判定。
- 虚拟寄存器无限，`.reg` 数量只反映 LLVM 侧用量。
- 谓词执行是 warp divergence 的指令层根源。

## 自我检核

- [ ] 能解释 `.b32`、`.u32`、`.f32` 的区别与适用场景
- [ ] 能说出四种主要状态空间及各自由谁分配
- [ ] 能解释 `%r<9>` 的 9 和实际使用数的关系
- [ ] 能读懂 `setp` + `@%p bra` 的逐线程语义

## 延伸阅读

- **[PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)** — NVIDIA
  - 读「Types」与「Program State Spaces」两章；本章的表格是它们的压缩版。
- **[PTX ISA: Registers](https://docs.nvidia.com/cuda/parallel-thread-execution/#registers)** — NVIDIA
  - 看「Register State Space」一节，确认「虚拟寄存器无上限」是规格保证，不是 LLVM 的发明。
- **[CUDA C++ Programming Guide: SIMT](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)** — NVIDIA
  - 读「SIMT Architecture」小节，把 `@%p` 的掩码语义和 warp divergence 对上。

下一章讲 PTX 的「时间维度」：`.version` 和 `.target` 如何决定哪些指令能用。

→ [Ch 3 PTX 契约（二）：版本、目标与特性门控](./03-ptx-version-and-target.md)
