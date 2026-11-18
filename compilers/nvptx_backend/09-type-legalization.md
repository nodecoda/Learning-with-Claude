# Ch 9 — 类型合法化：i1/i8/i16 与 PTX 的类型规则

> **目标**：能解释为什么 i8 加法会变成 `add.s16` 再 `and 255`、为什么 i1 存取要经过 `selp`，以及 NVPTX 到底把哪些类型当作「合法」。

## 为什么需要这个？

PTX 的寄存器银行只有 1/16/32/64/128 位，没有 8 位通用寄存器，也没有 1 位寄存器。LLVM IR 却允许任意位宽。合法化（legalization）就是把 IR 的类型压进 PTX 的银行。

这段规则直接决定生成的指令数量。不懂它，你会觉得「后端怎么这么啰嗦」；懂了之后，你能预测每条 IR 会膨胀成几条 PTX。

## 先建立直觉

```
IR 类型          PTX 银行            代价
i1      ──►    %p (predicate)        load/store 要 b8 + setp/selp
i8      ──►    %rs (b16)            运算提升 + 掩码截断
i16     ──►    %rs (b16)            直接
i32     ──►    %r  (b32)            直接
i64     ──►    %rd (b64)            直接
f16     ──►    %rs (b16)            直接（或提升到 f32，见 FTZ）
f32     ──►    %r  (b32)            直接
f64     ──►    %rd (b64)            直接
i128    ──►    %rq (b128)           仅内联汇编 / 128 位访存
```

## 一、合法类型清单（源码实测）

`NVPTXISelLowering.cpp` 的构造函数里逐个注册：

```cpp
addRegisterClass(MVT::i1, &NVPTX::B1RegClass);
addRegisterClass(MVT::i16, &NVPTX::B16RegClass);
addRegisterClass(MVT::v2i16, &NVPTX::B32RegClass);
addRegisterClass(MVT::v4i8, &NVPTX::B32RegClass);
addRegisterClass(MVT::i32, &NVPTX::B32RegClass);
addRegisterClass(MVT::i64, &NVPTX::B64RegClass);
addRegisterClass(MVT::f32, &NVPTX::B32RegClass);
addRegisterClass(MVT::f64, &NVPTX::B64RegClass);
addRegisterClass(MVT::f16, &NVPTX::B16RegClass);
addRegisterClass(MVT::v2f16, &NVPTX::B32RegClass);
addRegisterClass(MVT::bf16, &NVPTX::B16RegClass);
addRegisterClass(MVT::v2bf16, &NVPTX::B32RegClass);
```

注意 **i8 不在列表里**。`v4i8` 只是「4 个 i8 打包进 b32」的向量类型，不是 8 位标量寄存器。

## 二、i8 算术：提升 + 掩码

```llvm
define ptx_kernel void @widths(ptr %p, i1 %c, i8 %a8, i8 %b8, i16 %a16, i16 %b16)
```

真实产物（节选）：

```ptx
	ld.param.b8 	%rs3, [widths_param_2];
	ld.param.b8 	%rs4, [widths_param_3];
	add.s16 	%rs5, %rs3, %rs4;
	...
	cvt.u32.u16 	%r1, %rs5;
	and.b32 	%r2, %r1, 255;
```

四步：

1. `ld.param.b8` 读进来，放进 16 位寄存器。
2. `add.s16` 做加法（没有 `add.s8`）。
3. `cvt.u32.u16` 扩到 32 位。
4. `and.b32 ..., 255` 把高位截掉，恢复 i8 语义。

**这就是为什么 i8 运算比 i32 贵**：每条 i8 加法后面都可能跟一条掩码。优化器能消掉一部分（当它证明高位无意义时），但不能保证。

## 三、i1：只能是谓词

i1 在 NVPTX 里映射到 `%p`（B1RegClass），但谓词不能直接 load/store。实测：

```llvm
%v = load i1, ptr %p
%n = xor i1 %v, true
store i1 %n, ptr %q
```

```ptx
	ld.global.b8 	%rs1, [%rd2];
	and.b16 	%rs2, %rs1, 1;
	setp.ne.b16 	%p1, %rs2, 0;
	not.pred 	%p2, %p1;
	selp.b16 	%rs3, 1, 0, %p2;
	st.global.b8 	[%rd4], %rs3;
```

读 i1 = 读 byte + 取最低位 + `setp`；写 i1 = `selp` 成 0/1 + 写 byte。**一来一回 6 条指令**。这也是为什么 `bool` 数组在 GPU 上不划算。

对应源码：`LowerSTOREi1` 负责把 i1 store 改成 `selp.b16`，`setLoadExtAction(..., MVT::i1, ...)` 把 i1 扩展 load 展开（Ch 9 引用的 `setLoadExtAction` 那几行）。

## 四、i16 与半精度

- i16 直接用 `%rs`，`add.s16` / `mul.lo.s16` 都是原生。
- f16/bf16 也在 `%rs`，但算术语义不同：`add.rn.f16` 存在，但很多情况下会被提升到 f32。

FTZ（flush-to-zero）是一个实际的分支点。源码：

```cpp
SDValue NVPTXTargetLowering::PromoteBinOpIfF32FTZ(SDValue Op, SelectionDAG &DAG) const {
  if (useF32FTZ(DAG.getMachineFunction()))
    return PromoteBinOpToF32(Op.getNode(), DAG);
  return Op;
}
```

`PromoteBinOpToF32` 把窄浮点 binop 的双方 `fpext` 到 f32、算完再折回。**效果**：启用 FTZ 时，窄浮点 binop 的 denormal 与舍入按 f32 语义处理，再折回窄类型。代价是两条 `cvt`。

## 五、比较与选择：`setp` + `selp`

| IR | PTX | 备注 |
|---|---|---|
| `icmp`/`fcmp` | `setp.*` | 结果进谓词 |
| `select` | `selp.*` | 无分支；比 `br` 便宜 |
| `br i1` | `@%p bra` | 逐线程掩码跳转 |
| `and/or/xor i1` | `and.pred` / `or.pred` / `not.pred` | 谓词逻辑 |

如果 IR 里写 `select`，你会得到 `selp`；写成两个 `br` 再 phi，就可能得到真实分支。**在 GPU 上，能 `selp` 就不要 `br`**：分支意味着 divergence 的可能。

## 六、少数派类型

- **i128**：不对应任何普通标量运算，只用于内联汇编和 128 位 load/store（B128 类）。
- **v2f32 / v2i32**：只在支持的目标上注册，用于把两个 32 位值塞进一个 64 位寄存器（`packed_types`，Ch 14）。
- **i64 shift**：在 32 位地址目标上，`LowerShiftRightParts` / `LowerShiftLeftParts` 会把 64 位移位拆成两半；`nvptx64` 上一般直接 `shl.b64`。

## 踩雷集锦

1. **以为 i8 加法是一条指令**：通常是 `add.s16` + `and`，除非优化器证明高位无用。
2. **以为 i1 能塞进通用寄存器**：不能；只能进谓词，load/store 要转换。
3. **以为 `v4i8` 能直接做算术**：它只是打包类型，算术仍要拆开或提升。
4. **忽略 FTZ 提升**：同样的 f16 表达式，开/关 FTZ 可能生成不同数量的 `cvt`。
5. **手写 IR 用 `i8` 做大量运算**：PTX 层会膨胀；能改成 i32 就改。

## 进阶：合法化顺序与 DAG combine

类型合法化（LegalizeTypes）先跑，操作合法化（LegalizeDAG）后跑。NVPTX 用 `setOperationAction` 把很多操作标成 `Expand` / `Custom` / `Promote`，例如：

- `setTruncStoreAction(VT, MVT::i1, Expand)`：i1 截断存储要展开。
- `setLoadExtAction({SEXTLOAD, ZEXTLOAD, EXTLOAD}, VT, MVT::i1, Expand)`：从 i1 扩展的 load 要展开。

这意味着你看到的每条 `and`/`setp` 都能在源码里找到对应的 action 设置。读 PTX 时反查这些 action，是理解合法化最快的方法。

## 动手练习

1. 写一个纯 i8 的向量加法 kernel，数一数每条加法对应几条 PTX 指令。
2. 写一个 `bool` 数组的读写，确认 `selp` 与 `setp` 都出现。
3. 用 `-mattr=+ftz`（如果目标支持）对比 f16 运算的 `cvt` 数量。
4. 把 i8 改成 i32 重写同一个 kernel，diff PTX 长度并解释差异。

## 本章重点整理

- 合法银行是 1/16/32/64/128 位；i8 没有标量寄存器。
- i8 算术 = b16 运算 + 掩码；i1 存取 = b8 + setp/selp。
- 比较进谓词，选择用 `selp`，分支用 `@%p bra`。
- FTZ 会触发窄浮点到 f32 的提升。

## 自我检核

- [ ] 能说出为什么 i8 加法后面常跟 `and 255`
- [ ] 能写出 i1 load/store 的完整指令序列
- [ ] 能解释 `selp` 与 `br` 在 divergence 上的差别
- [ ] 知道哪些类型只用于特殊场景（i128、v2f32）

## 延伸阅读

- **[LLVM Legalizer](https://llvm.org/docs/CodeGenerator.html#selectiondag-legalize)** — LLVM 官方
  - 读「Type Legalization」与「Operation Legalization」两节，把本章的 action 对上。
- **[PTX ISA: Types](https://docs.nvidia.com/cuda/parallel-thread-execution/#types)** — NVIDIA
  - 看「Fundamental Types」与「Vectors」两节，确认没有 8 位算术。
- **[PTX ISA: Floating Point Instructions](https://docs.nvidia.com/cuda/parallel-thread-execution/#floating-point-instructions)** — NVIDIA
  - 读 `add`/`mul` 的舍入模式与 `.ftz` 修饰符，理解 FTZ 提升的动机。

类型合法化之后，DAG 已经只剩 PTX 能表达的东西；下一步是把它们匹配成具体指令。

→ [Ch 10 指令选择：DAG 到 PTX 指令](./10-instruction-selection.md)
