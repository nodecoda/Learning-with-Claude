# Ch 8 — 函数 lowering：参数、返回与调用

> **目标**：能追踪一个函数从 LLVM IR 签名到 PTX `.entry`/`.func` 的完整 lowering 路径，并解释每个 `.param` 槽的来源。

## 为什么需要这个？

ABI 是「约定」；lowering 是「把约定实现出来」。Ch 4 看的是产物，本章看的是 `NVPTXISelLowering.cpp` 里那 8600 行如何把 `LowerFormalArguments`、`LowerReturn`、`LowerCall` 拼起来。读懂它，你才能在自研 GPU 上复刻一套自己的 ABI。

## 先建立直觉

```
IR: define ptx_kernel void @k(ptr %p, i32 %n)
                     │
                     ▼
LowerFormalArguments: 为每个参数建 "参数符号"（param symbol）
                      ┌─ 标量：ld.param.bXX
                      ├─ byval：可能 ld.param / grid constant / local 拷贝
                      └─ 指针：可带 .global 限定
                     │
                     ▼
SelectionDAG 用这些值构建函数体
                     │
                     ▼
AsmPrinter 把参数符号打印成 .param 列表（Ch 4、15）
```

关键概念：**参数符号（param symbol）是一个 DAG 节点**，不是内存地址。`ld.param.b64 %rd1, [k_param_0]` 里的 `k_param_0` 是符号，不是寄存器。

## 一、`LowerFormalArguments`

`NVPTXTargetLowering::LowerFormalArguments`（`NVPTXISelLowering.cpp`）做四件事：

1. **建立参数符号**：`getParamSymbolNode(DAG, i)` 为第 i 个参数生成 `NVPTXISD::Wrapper` 节点，指向 `k_param_i`。
2. **按类型选择装载方式**：
   - 标量：`ld.param.bXX`（宽度按 Ch 4 的 ABI 规则）。
   - 指针：`ld.param.b64` + 可选 `.global` 限定。
   - byval：根据 Ch 7 的三条策略决定直接 `ld.param`、grid constant、还是 local 拷贝。
3. **处理对齐**：参数符号带 `.align N`，由 `PromoteParamAlign` 与 IR 属性共同决定。
4. **把值写回 `InVals`**：后续 DAG 构建从这里取值。

一个容易忽略的点：kernel 的 byval 参数在 `LowerArgs` 之后已经是 `ptr addrspace(101)`（entry param 空间）。ISel 看到这个空间，就知道「这是参数符号，不是普通指针」，不需要再插 `cvta`。

## 二、`LowerReturn`

规则很短：

- kernel 返回 void，直接 `ret`。
- device 函数有返回值时，ABI 在签名里放一个返回参数槽 `func_retval0`，`LowerReturn` 生成 `st.param`。

```ptx
.visible .func (.param .b32 func_retval0) scale(...)
...
st.param.b32 	[func_retval0], %r3;
ret;
```

聚合返回值按数组传：

```llvm
%S = type { float, i32 }
define ptx_device %S @mk(i32 %x)
```

```ptx
.visible .func (.param .align 4 .b8 func_retval0[8]) mk(
	.param .b32 mk_param_0
)
...
st.param.b32 	[func_retval0+4], %r1;
st.param.b32 	[func_retval0], 1065353216;
```

调用者则声明一个 `retval0[8]`，用 `ld.param` 按字段读回：

```ptx
.param .align 4 .b8 	retval0[8];
call.uni (retval0), mk, (param0);
ld.param.b32 	%r1, [retval0];
```

## 三、`LowerCall`

`LowerCall` 生成 `CALLSEQ_START` / `CALLSEQ_END` 包围的调用序列，步骤是：

1. 为每个实参分配一个调用参数符号（`param0`、`param1`…）。
2. 生成 `st.param` 把值写进去。
3. 若有返回值，再分配 `retval0`。
4. 生成 `call.uni`。
5. 用 `ld.param` 取回返回值。

真实产物（Ch 4 的 `scale` 调用）：

```ptx
{ // callseq 0, 0
	.param .b32 	param0;
	.param .b32 	param1;
	.param .b32 	retval0;
	st.param.b32 	[param1], %r1;
	st.param.b32 	[param0], %r3;
	call.uni (retval0), scale, (param0, param1);
	ld.param.b32 	%r4, [retval0];
} // callseq 0
```

`NVPTXProxyRegErasure` 会在寄存器分配前清理为保活 `callseq_end` 插入的伪寄存器（Ch 7）。

## 四、调用约定与 kernel 判定

LLVM 侧：

| 调用约定 | PTX | 说明 |
|---|---|---|
| `ptx_kernel` | `.entry` | 驱动启动，无返回值 |
| `ptx_device` | `.func` | device 内调用，可返回值 |
| C | 不支持 | device 代码不用 C ABI |

`isKernelFunction(F)`（`NVVMProperties.h`）只查调用约定；`AsmPrinter::emitFunctionEntryLabel` 据此决定打印 `.entry` 还是 `.func` 加返回子句。

## 五、可变参数与全局构造

- **可变参数**：PTX 没有 C 的 varargs ABI，IR 层由 `ExpandVariadics` 先降级成固定参数（Ch 7）。所以 `printf` 在 device 侧要么被特殊处理，要么不可用。
- **全局构造/析构**：`NVPTXCtorDtorLowering` 把它们改写成特殊 kernel，由驱动在模块加载时调度。

## 踩雷集锦

1. **以为参数符号是地址**：`k_param_0` 是符号；`[k_param_0+8]` 是「符号 + 常量偏移」，不是指针算术。
2. **以为聚合返回值走 sret**：NVPTX 用返回参数槽，不是隐藏指针参数（和 x86-64 不同）。
3. **在 kernel 里返回值**：`.entry` 不能有返回值；IR 里写了 `ptx_kernel` 又 `ret i32` 会被拒绝或忽略。
4. **忽略 byval 的三种策略**：同样的 IR 签名，可能生成 `ld.param`，也可能生成 local 拷贝。
5. **手写 IR 时调用约定写错**：`define void @k` 默认是 C 调用约定，会变成 `.func` 而不是 `.entry`。

## 进阶：ABI 设计与自研 GPU

如果你的 GPU 想兼容 PTX，kernel ABI 基本没有选择余地：必须实现 `.param`、参数提升、返回槽。真正有设计空间的是 device 函数 ABI（寄存器传参 vs 参数空间）和调用约定（是否支持间接调用、递归）。NVPTX 选择「一切走参数空间」，实现简单，代价是每次调用都有 `st.param`/`ld.param` 开销。

## 动手练习

1. 写一个返回 `{float,i32}` 的 device 函数，标出 `func_retval0` 的偏移。
2. 写一个 byval 参数被写入的 kernel，确认出现 local 拷贝，并解释为什么不能直接写 `.param`。
3. 把 kernel 的调用约定改成默认 C，观察 PTX 从 `.entry` 变成 `.func`。
4. 用 `-O0` 编一次，统计 `callseq` 里参数符号的数量与顺序。

## 本章重点整理

- `LowerFormalArguments` 建参数符号，按 ABI 选择 `ld.param` / grid constant / local 拷贝。
- device 返回值走 `func_retval0` 参数槽，聚合返回按数组传。
- `LowerCall` 生成 `callseq` + `st.param` + `call.uni` + `ld.param`。
- 调用约定决定 `.entry` / `.func`，没有 C ABI。

## 自我检核

- [ ] 能解释参数符号与寄存器操作数的区别
- [ ] 能画出 device 函数调用的五步序列
- [ ] 知道聚合返回值在 PTX 里长什么样
- [ ] 能说出 kernel 与 device 函数 ABI 的三处差异

## 延伸阅读

- **[PTX ISA: Function Declarations](https://docs.nvidia.com/cuda/parallel-thread-execution/#function-declarations)** — NVIDIA
  - 读 `.param`、`call`、`st.param` 三节；本章产物逐条对应。
- **[LLVM SelectionDAG](https://llvm.org/docs/CodeGenerator.html#selectiondag)** — LLVM 官方
  - 读「Call lowering」相关段落，理解 `LowerCall` 在框架里的位置。
- **[CUDA ABI 文档](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#device-function-calls)** — NVIDIA
  - 看 device 函数调用的限制（递归、函数指针），对照 PTX 的 `call.uni`。

函数边界清楚了，接下来进 DAG：类型怎么合法化、指令怎么选。

→ [Ch 9 类型合法化：i1/i8/i16 与 PTX 的类型规则](./09-type-legalization.md)
