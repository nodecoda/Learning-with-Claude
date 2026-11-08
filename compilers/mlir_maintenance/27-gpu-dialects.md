# Ch 27 — GPU 方言：outlining、三套后端与“跑不通”的诊断

> **目标**：能完整说出 `gpu.launch` → `gpu.launch_func` + `gpu.module`/`gpu.func` → NVVM/ROCDL/SPIR-V 这条链每一跳的输入输出；能在本树里区分“真的能跑”（NVVM/ROCDL 文本 lowering）与“需要后端才能跑”（PTX/HSACO 二进制、SPIR-V 序列化）；遇到“failed to legalize”能读懂 match failure remark。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，构建 target 只有 `X86;AArch64;RISCV`（没有 NVPTX/AMDGPU/SPIRV 后端），源 @ `e44fff5decc7`。
>
> **前置**：Ch 19/21（legality 与动态 legality）、Ch 26（OpenACC 如何落到 `gpu.launch`）。

---

## 27.1 为什么 GPU 方言是“分层最明显”的子系统

`lib/Dialect/GPU` 一年 94 次提交，只有 Linalg 的一半；但它的**结构复杂度**是最高的：同一个 `gpu` 方言里同时住着三层抽象。

```text
高  ┌────────────────────────────────────────────────────────────┐
层  │ gpu.launch / gpu.launch_func      "我有个三维网格要跑"       │
    ├────────────────────────────────────────────────────────────┤
    │ gpu.module / gpu.func (kernel)    "这是设备代码，单独编译"   │
    ├────────────────────────────────────────────────────────────┤
    │ gpu.block_id / thread_id /         "我在网格的哪个位置"      │
    │ gpu.barrier / gpu.shuffle ...                                │
低  └────────────────────────────────────────────────────────────┘
        │                    │                     │
        ▼                    ▼                     ▼
     NVVM                 ROCDL                 SPIR-V
   (NVIDIA)              (AMD)                 (Vulkan/OpenCL)
```

这三层的分界线就是 **kernel outlining**。它是本章的核心：outlining 之前，GPU 代码只是“函数里的一个 region”；outlining 之后，它是“一个独立的模块，有自己的编译单元和 ABI”。

维护者最常遇到的三个问题，正好对应三层：

- “我的 pass 在 `gpu.launch` 里改代码，为什么 outlining 之后找不到了？” → region 被搬进了 `gpu.module`，符号作用域变了。
- “`gpu.func` 的参数为什么变成了 5 个 `!llvm.ptr/i64`？” → memref 描述符被拆成 base/aligned/offset/sizes/strides。
- “`--gpu-module-to-binary` 报 `no targets are registered`。” → 这是**构建配置**问题，不是 IR 问题（27.7 实测）。

## 27.2 先建立直觉：outlining 是分水岭

```text
【outlining 之前】一个 module，一个 func
┌──────────────────────────────────────────────┐
│ module {                                     │
│   func.func @fill(%A: memref<64xf32>) {      │
│     gpu.launch blocks(1,1,1) threads(64,1,1) │
│     {                                        │
│       %tid = gpu.thread_id x                 │
│       memref.store %v, %A[%tid]              │
│       gpu.terminator                         │
│     }                                        │
│   }                                          │
│ }                                            │
└──────────────────────────────────────────────┘
                    │  --gpu-kernel-outlining
                    ▼
【outlining 之后】host module + 嵌套的 device module
┌──────────────────────────────────────────────┐
│ module attributes {gpu.container_module} {    │
│   func.func @fill(%A) {                       │
│     gpu.launch_func @fill_kernel::@fill_kernel│  ← host 侧只剩"启动"
│       blocks(1,1,1) threads(64,1,1)           │
│       args(%A : memref<64xf32>)               │
│   }                                           │
│   gpu.module @fill_kernel {                   │  ← device 侧独立编译单元
│     gpu.func @fill_kernel(%A) kernel          │
│       attributes {known_block_size = [64,1,1],│
│                   known_grid_size  = [1,1,1]} │
│     { %tid = gpu.thread_id x ... }            │
│   }                                           │
│ }                                             │
└──────────────────────────────────────────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
     NVVM        ROCDL       SPIR-V
   nvvm.*       rocdl.*     spirv.*
        │           │           │
        ▼           ▼           ▼
     PTX/cubin   HSACO        SPIR-V binary
   （需要 NVPTX）（需要 AMDGPU）（需要 SPIRV 后端）
```

图里最后一行是关键：**MLIR 能把 IR 降到 `nvvm.*`/`rocdl.*`/`spirv.*`（纯 dialect 转换），但“变成二进制”需要 LLVM 注册了对应 target**。本树没有，所以本章会明确标出哪些实验“只到文本层”。

## 27.3 `gpu.launch` 的三维契约

`gpu.launch` 的 block/thread 都是**三元组**，即使你只用一维也要写满：

```bash
cd /tmp/mlir_p5
cat > gpu.mlir <<'EOF'
module attributes {gpu.container_module} {
  func.func @fill(%A: memref<64xf32>) {
    %c1 = arith.constant 1 : index
    %c64 = arith.constant 64 : index
    gpu.launch blocks(%bx, %by, %bz) in (%nbx = %c1, %nby = %c1, %nbz = %c1)
               threads(%tx, %ty, %tz) in (%ntx = %c64, %nty = %c1, %ntz = %c1) {
      %v = arith.constant 1.0 : f32
      memref.store %v, %A[%tx] : memref<64xf32>
      gpu.terminator
    }
    return
  }
}
EOF
```

如果你按老教程写一维形式：

```text
gpu.mlir:5:5: error: custom op 'gpu.launch' blocks expects 3 arguments, but got 1
    gpu.launch blocks(%bx) in (%nbx = %c1) threads(%tx) in (%ntx = %c64) {
    ^
```

这条错误信息说明 verifier 在**解析阶段**就要求三个 block 参数，不是“可选维度”。旧语法（只写 x 维）在某个版本被删掉了，这类改动是 GPU 方言常见的 breaking change。

## 27.4 outlining：region 变 kernel

```bash
$T/bin/mlir-opt gpu.mlir --gpu-kernel-outlining
```

真实输出：

```text
module attributes {gpu.container_module} {
  func.func @fill(%arg0: memref<64xf32>) {
    %c1 = arith.constant 1 : index
    %c64 = arith.constant 64 : index
    gpu.launch_func  @fill_kernel::@fill_kernel blocks in (%c1, %c1, %c1) threads in (%c64, %c1, %c1)  args(%arg0 : memref<64xf32>)
    return
  }
  gpu.module @fill_kernel {
    gpu.func @fill_kernel(%arg0: memref<64xf32>) kernel attributes {known_block_size = array<i32: 64, 1, 1>, known_grid_size = array<i32: 1, 1, 1>} {
      %block_id_x = gpu.block_id x
      %block_id_y = gpu.block_id y
      %block_id_z = gpu.block_id z
      %thread_id_x = gpu.thread_id x
      %thread_id_y = gpu.thread_id y
      %thread_id_z = gpu.thread_id z
      %grid_dim_x = gpu.grid_dim x
      ...
      %cst = arith.constant 1.000000e+00 : f32
      memref.store %cst, %arg0[%thread_id_x] : memref<64xf32>
      gpu.return
    }
  }
}
```

五个必须注意的细节：

1. **`gpu.container_module` 属性被加到外层 module**：它是“这个 module 里有 device module”的标记，后续 pass 靠它判断。
2. **kernel 名字**：`@fill_kernel::@fill_kernel` 是 `module::func` 的完全限定名。默认名是 `<父符号名>_kernel`，所以函数叫 `fill` 就得到 `fill_kernel`。
3. **`known_block_size` / `known_grid_size`**：把 launch 时的常量维度“固化”成 kernel 的属性。这两个属性是后续优化（如 `nvvm.maxntid`）的依据，也是你改 launch 维度时最容易忘记同步的东西。
4. **`gpu.block_id`/`thread_id`/`grid_dim`/`block_dim` 全部被显式注入**：即使 kernel 里没用到 y/z 维，也会插入（`createForAllDimensions`，`KernelOutlining.cpp:40`）。
5. **`gpu.terminator` 变成 `gpu.return`**：region 的终结符换了——这是“从 region 变成函数”的标志。

源码：`outlineKernelFuncImpl`（`KernelOutlining.cpp:177`）负责克隆 region 并建 `gpu.func`；`convertToLaunchFuncOp`（`:284`）负责把 `gpu.launch` 换成 `gpu.launch_func`；`createKernelModule`（`:405`）负责建 `gpu.module`。pass 本体在 `:355`。

## 27.5 NVVM：outline 之后的 kernel 怎么变 `nvvm.*`

```bash
$T/bin/mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-nvvm
```

真实输出（节选）：

```text
  gpu.module @fill_kernel {
    llvm.func @fill_kernel(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64) attributes {gpu.kernel, gpu.known_block_size = array<i32: 64, 1, 1>, gpu.known_grid_size = array<i32: 1, 1, 1>, nvvm.kernel, nvvm.maxntid = array<i32: 64, 1, 1>} {
      %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
      %1 = llvm.insertvalue %arg0, %0[0] : ...
      ...
      %6 = llvm.mlir.constant(1.000000e+00 : f32) : f32
      %7 = nvvm.read.ptx.sreg.tid.x range <i32, 0, 64> : i32
      %8 = llvm.sext %7 : i32 to i64
      %9 = llvm.extractvalue %5[1] : ...
      %10 = llvm.getelementptr inbounds|nuw %9[%8] : (!llvm.ptr, i64) -> !llvm.ptr, f32
      llvm.store %6, %10 : f32, !llvm.ptr
```

四个转换同时发生：

1. **`gpu.func` → `llvm.func`**，且 `memref` 参数被拆成 **5 个 LLVM 值**（`ptr, ptr, i64, i64, i64`）= base/aligned/offset/sizes/strides。这就是 27.1 里“参数为什么变了”的答案。
2. **`gpu.thread_id x` → `nvvm.read.ptx.sreg.tid.x`**，而且带了 **`range <i32, 0, 64>`**。这个 range 来自 `known_block_size`——如果 outlining 时维度是动态的，range 就不会出现，后续的除零/越界优化也会失效。
3. **属性翻译**：`gpu.kernel` 保留，同时加 `nvvm.kernel` 与 `nvvm.maxntid = [64,1,1]`。
4. **`memref.store` 变成 `llvm.getelementptr + llvm.store`**：`--convert-gpu-to-nvvm` 内部会调用 memref/arith 的 LLVM pattern（`LowerGpuOpsToNVVMOps.cpp:554` 的 `populateGpuToNVVMConversionPatterns` 调用点）。

**维护者视角**：`range` 属性丢失是常见 bug。它由 `nvvm.read.ptx.sreg.tid.x` 的 builder 在 lowering 时从 `known_block_size` 推导。如果你改了 outlining 的维度传播逻辑，一定要跑 `test/Conversion/GPUToNVVM/` 里带 `range` CHECK 的用例。

## 27.6 ROCDL：同一个 kernel，换 target 要显式给 chipset

```bash
$T/bin/mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-rocdl
```

真实输出：

```text
<unknown>:0: error: 'invalid' is not a valid AMDGPU architecture: expected a GPU name, a triple, or a target ID
```

`--convert-gpu-to-rocdl` 需要知道目标架构，否则连 data layout 都算不出来。给上 chipset：

```bash
$T/bin/mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-rocdl='chipset=gfx942'
```

真实输出（节选）：

```text
  gpu.module @fill_kernel attributes {llvm.data_layout = "e-p:64:64-p1:64:64-p2:32:32-p3:32:32-p4:64:64-p5:32:32-p6:32:32-p7:160:256:256:32-p8:128:128:128:48-p9:192:256:256:32-i64:64-v16:16-v24:32-v32:32-v48:64-v96:128-v192:256-v256:256-v512:512-v1024:1024-v2048:2048-n32:64-S32-A5-G1-ni:7:8:9"} {
    llvm.func @fill_kernel(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64) attributes {gpu.kernel, gpu.known_block_size = array<i32: 64, 1, 1>, gpu.known_grid_size = array<i32: 1, 1, 1>, rocdl.flat_work_group_size = "64,64", rocdl.kernel, rocdl.reqd_work_group_size = array<i32: 64, 1, 1>} {
      %7 = rocdl.workitem.id.x range <i32, 0, 64> : i32
```

和 NVVM 对比，三处差异特别值得记：

| | NVVM | ROCDL |
|---|---|---|
| thread id | `nvvm.read.ptx.sreg.tid.x` | `rocdl.workitem.id.x` |
| kernel 属性 | `nvvm.kernel`, `nvvm.maxntid` | `rocdl.kernel`, `rocdl.reqd_work_group_size`, `rocdl.flat_work_group_size` |
| data layout | 由 `#nvvm.target` 决定 | 由 `chipset=gfx942` 决定，写成 `llvm.data_layout` |

同一个 `gpu.func`，两个后端产出的属性名、intrinsic 名、甚至是“工作组大小怎么表达”都不一样。**这就是为什么 GPU 后端 bug 极少能“改一处修两边”**。

## 27.7 二进制：本树跑不通，但错误信息很有教学价值

先试最朴素的：

```bash
$T/bin/mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-nvvm --gpu-module-to-binary
```

真实输出：

```text
gpu.mlir:5:5: error: the module has no target attributes
    gpu.launch blocks(%bx, %by, %bz) in (%nbx = %c1, %nby = %c1, %nbz = %c1)
```

`gpu.module` 需要 `gpu.targets` 属性（例如 `[#nvvm.target<chip = "sm_70">]`）。加上：

```bash
sed 's/gpu.module @fill_kernel {/gpu.module @fill_kernel [#nvvm.target<chip = "sm_70">] {/' outlined.mlir > outlined_tgt.mlir
$T/bin/mlir-opt outlined_tgt.mlir --convert-gpu-to-nvvm --gpu-module-to-binary='format=isa'
```

真实输出：

```text
outlined_tgt.mlir:8:3: error: Failed to lookup target for triple 'nvptx64-nvidia-cuda' Unable to find target for triple "nvptx64-nvidia-cuda" (no targets are registered)
```

**这不是 IR 写错了，是构建里没有 NVPTX 后端。** 证据：

```bash
$T/bin/llc --version | sed -n '/Registered Targets/,$p'
```

真实输出：

```text
  Registered Targets:
    aarch64    - AArch64 (little endian)
    aarch64_32 - AArch64 (little endian ILP32)
    aarch64_be - AArch64 (big endian)
    arm64      - ARM64 (little endian)
    arm64_32   - ARM64 (little endian ILP32)
    riscv32    - 32-bit RISC-V
    riscv32be  - 32-bit big endian RISC-V
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
    x86        - 32-bit X86: Pentium-Pro and above
    x86-64     - 64-bit X86: EM64T and AMD64
```

没有 `nvptx64`、没有 `amdgcn`、没有 `spirv`。所以本章的二进制路径**明确标为未实测**（原因：构建 target 只开了 `X86;AArch64;RISCV`，`-DLLVM_TARGETS_TO_BUILD` 不含 NVPTX/AMDGPU/SPIRV）。要把这条链跑通，需要重配构建并安装 CUDA/ROCm 工具链；`ptxas` 本身在机器上是有的（`/usr/local/cuda/bin/ptxas`），但 LLVM 的 NVPTX target 没编进去，`gpu-module-to-binary` 走不到调用它的那一步。

### 27.7.1 高层 pipeline 也绕不过构建限制

`mlir-opt` 提供了“一条命令到底”的 pipeline：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -A 2 'gpu-lower-to-nvvm-pipeline'
```

真实输出：

```text
      --gpu-lower-to-nvvm-pipeline                           -   The default pipeline lowers main dialects (arith, memref, scf, vector, gpu, and nvgpu) to NVVM. It starts by lowering GPU code to the specified compilation target (default is fatbin) then lowers the host code.
```

直接用它跑 `gpu.mlir`：

```bash
$T/bin/mlir-opt gpu.mlir --gpu-lower-to-nvvm-pipeline
```

真实输出：

```text
gpu.mlir:5:5: error: Failed to lookup target for triple 'nvptx64-nvidia-cuda' Unable to find target for triple "nvptx64-nvidia-cuda" (no targets are registered)
    gpu.launch blocks(%bx, %by, %bz) in (%nbx = %c1, %nby = %c1, %nbz = %c1)
    ^
gpu.mlir:5:5: note: see current operation: 
"gpu.module"() <{sym_name = "fill_kernel", targets = [#nvvm.target]}> ({
  "llvm.func"() <{CConv = #llvm.cconv<ccc>, function_type = !llvm.func<void (ptr, ptr, i64, i64, i64)>, linkage = #llvm.linkage<external>, sym_name = "fill_kernel", visibility_ = 0 : i64}> ({
  ^bb0(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64):
```

这条错误信息比单纯 “no targets are registered” 更有信息量，因为它暴露了 pipeline 的内部状态：

1. `gpu.module` 已经被自动加上 `targets = [#nvvm.target]`；
2. kernel 已经降成 `llvm.func`，memref 参数已经是 5 个标量——**说明 NVVM lowering 全部成功了**；
3. 失败发生在最后“把 LLVM IR 编成 cubin/PTX”这一步，因为本树没有 NVPTX target。

换句话说，**用这条 pipeline 时，“IR 是否正确”和“后端是否可用”是两件事**。诊断顺序应该是：先看 `note: see current operation` 里的 IR 是否已降到 `llvm.func`。降到了 → 问题在构建配置；没降到 → 问题在你的 IR（通常是某个 dialect 没有对应 pattern）。

顺带记一个 option 名陷阱：这条 pipeline 的选项是 `cubin-chip`/`cubin-format`/`cubin-triple`/`cubin-features`，**没有** `compilation-target`。实测：

```bash
$T/bin/mlir-opt gpu.mlir --gpu-lower-to-nvvm-pipeline='cubin-chip=sm_70 compilation-target=isa'
```

真实输出：

```text
<Pass-Options-Parser>: no such option compilation-target
<unknown>:0: error: failed to add `gpu-lower-to-nvvm-pipeline` with options `cubin-chip=sm_70 compilation-target=isa`
```

“文档/直觉与真实 option 名不符”时，一律以 `--help` 的输出为准。

## 27.8 SPIR-V：一个真实的 “failed to legalize + match failure remark” 案例

SPIR-V 路径不需要 LLVM 后端就能做 **dialect 转换**（序列化才需要后端）。试：

```bash
$T/bin/mlir-opt gpusv.mlir --gpu-kernel-outlining --convert-gpu-to-spirv
```

真实输出（节选）：

```text
gpusv.mlir:5:5: remark: match failure: missing 'spirv.entry_point_abi' attribute
    gpu.launch blocks(%bx, %by, %bz) in (%nbx = %c1, %nby = %c1, %nbz = %c1)
    ^
gpusv.mlir:5:5: note: see current operation: 
"gpu.func"() <{function_type: (memref<64xf32, #spirv.storage_class<StorageBuffer>>) -> (), kernel, ...>
gpusv.mlir:5:5: error: failed to legalize operation 'gpu.func': ...
```

这个诊断是 **DialectConversion 的标准形态**，值得逐段读：

1. `remark: match failure: missing 'spirv.entry_point_abi' attribute` —— 某个 pattern 尝试匹配 `gpu.func` 但前置条件不满足。注意它是 **remark** 不是 error，默认可能被过滤掉，需要 `--mlir-print-ir-after-all` 或提高诊断级别才容易看到。
2. `note: see current operation` —— 打印失败时的 op 状态（这里已经能看到 pattern 把 memref 的 storage class 改成了 `StorageBuffer`）。
3. `error: failed to legalize operation 'gpu.func'` —— 最终 legality 检查失败。

修法是把 ABI 属性加到 `gpu.func` 上（不是 `gpu.module` 上）：

```bash
sed 's/kernel attributes {known_block_size/kernel attributes {spirv.entry_point_abi = #spirv.entry_point_abi<workgroup_size = [64, 1, 1]>, known_block_size/' outlined.mlir > outlined_sv3.mlir
$T/bin/mlir-opt outlined_sv3.mlir --convert-gpu-to-spirv
```

真实输出（换了一个错误，说明第一关过了）：

```text
outlined_sv3.mlir:23:7: error: failed to legalize operation 'memref.store': "memref.store"(%43, <<UNKNOWN SSA VALUE>>, %15) : (f32, memref<64xf32, #spirv.storage_class<StorageBuffer>>, index) -> ()
```

到这一步需要 memref→SPIR-V 的转换。本树提供 `--convert-memref-to-spirv`，但直接接在后面仍然留下 `memref.store`：

```bash
$T/bin/mlir-opt outlined_sv3.mlir --convert-gpu-to-spirv --convert-memref-to-spirv
# 仍然是: error: failed to legalize operation 'memref.store'
```

所以 SPIR-V 全链路在本章标为**部分实测**：能验证“缺 `entry_point_abi` 会被拒”和“memref 需要单独处理”两个结论，但没有走通到 `spirv.module`。完整路径通常还要 `--map-memref-spirv-storage-class`、`--spirv-lower-abi-attrs` 等（`--help` 里有，但组合顺序依赖具体 op set）。

## 27.9 host 侧 `gpu-to-llvm` 的顺序陷阱

`gpu-to-llvm` 把 host 侧的 `gpu.launch_func` 变成 runtime 调用。直接接在 `--convert-to-llvm` 后面会撞：

```bash
$T/bin/mlir-opt outlined.mlir --convert-to-llvm --gpu-to-llvm
```

真实输出：

```text
outlined.mlir:5:5: error: 'gpu.launch_func' op got 5 kernel operands but expected 1
    gpu.launch_func  @fill_kernel::@fill_kernel blocks in (%c1, %c1, %c1) threads in (%c64, %c1, %c1)  args(%arg0 : memref<64xf32>)
```

原因：`--convert-to-llvm` 把 `memref<64xf32>` 参数**拆成了 5 个 LLVM 值**，但 `gpu.launch_func` 的 `operandSegmentSizes` 仍然记录“1 个 kernel operand”，verifier 立刻发现不一致。

结论：**`gpu.launch_func` 的 operand 必须在 GPU 侧统一处理**——要么先 `--gpu-module-to-binary` 把 kernel 变成 binary（不再需要逐个 operand），要么用带 memref 支持的模式（`--gpu-to-llvm='use-bare-ptr-memref-call-conv=1'` 之类的选项，视版本而定）。这类“两个 pass 各自正确、组合起来非法”的问题，是 GPU pipeline 最常见的一类 bug。

## 27.10 顺带一提：NVGPU 与 XeGPU 是“补丁层”

`mlir/include/mlir/Dialect/LLVMIR/NVVMOps.td`（105 次改动）与 `NVVMDialect.cpp`（90 次）长期占据 churn 前列，`lib/Dialect/XeGPU/Transforms/XeGPUPropagateLayout.cpp`（63 次）也很热。它们的存在说明一件事：

- **NVGPU** 不是“另一个后端”，而是 NVVM 之上的**高层模式层**（`nvgpu.ldmatrix`、`nvgpu.mma.sync` 等），最终仍降到 `nvvm.*`；
- **XeGPU** 是 Intel GPU 的类似层，核心难点是 **layout 传播**（`XeGPUPropagateLayout.cpp`），因为 Intel 的 DPAS/2D block load 对数据布局有强约束。

维护者经验：修“某个 GPU intrinsic 生成不对”时，先确认它属于哪一层——`gpu.*`（通用）、`nvgpu.*`/`xegpu.*`（厂商高层）、`nvvm.*`/`rocdl.*`（厂商底层）。三层的测试目录分别是 `test/Dialect/GPU`（47 个文件）、`test/Dialect/NVGPU`（13 个）、`test/Dialect/XeGPU`。

## 27.11 源码落点

| 主题 | 文件:行 |
|---|---|
| `gpu.launch` 的 verifier / 解析 | `mlir/lib/Dialect/GPU/IR/GPUDialect.cpp`；`mlir/include/mlir/Dialect/GPU/IR/GPUOps.td` |
| kernel outlining 主体 | `mlir/lib/Dialect/GPU/Transforms/KernelOutlining.cpp:355` |
| 克隆 region 建 `gpu.func` | 同上 `:177`（`outlineKernelFuncImpl`） |
| `gpu.launch` → `gpu.launch_func` | 同上 `:284`（`convertToLaunchFuncOp`） |
| 建 `gpu.module` | 同上 `:405`（`createKernelModule`） |
| 注入 block/thread id | 同上 `:40`（`createForAllDimensions`） |
| NVVM pattern 注册 | `mlir/lib/Conversion/GPUToNVVM/LowerGpuOpsToNVVMOps.cpp:636`、legality `:600` |
| ROCDL pattern 注册 | `mlir/lib/Conversion/GPUToROCDL/LowerGpuOpsToROCDLOps.cpp:879` |
| GPU → binary | `mlir/lib/Dialect/GPU/Transforms/ModuleToBinary.cpp` |
| NVVM op 定义（churn 105） | `mlir/include/mlir/Dialect/LLVMIR/NVVMOps.td` |
| NVVM 方言实现（churn 90） | `mlir/lib/Dialect/LLVMIR/IR/NVVMDialect.cpp` |
| XeGPU layout 传播（churn 63） | `mlir/lib/Dialect/XeGPU/Transforms/XeGPUPropagateLayout.cpp` |

## 27.12 可跑实验清单

| # | 命令 | 结果 |
|---|---|---|
| 1 | `mlir-opt gpu.mlir --gpu-kernel-outlining` | ✅ `launch_func` + `gpu.module` + `known_block_size` |
| 2 | `mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-nvvm` | ✅ `nvvm.read.ptx.sreg.tid.x range <i32,0,64>` |
| 3 | `mlir-opt gpu.mlir --gpu-kernel-outlining --convert-gpu-to-rocdl` | ❌ `'invalid' is not a valid AMDGPU architecture` |
| 4 | `... --convert-gpu-to-rocdl='chipset=gfx942'` | ✅ `rocdl.workitem.id.x` + `llvm.data_layout` |
| 5 | `... --gpu-module-to-binary` | ❌ `the module has no target attributes` |
| 6 | `... --gpu-module-to-binary`（加 `#nvvm.target`） | ❌ `no targets are registered`（构建无 NVPTX）**未实测** |
| 7 | `mlir-opt gpusv.mlir --convert-gpu-to-spirv` | ❌ `missing 'spirv.entry_point_abi'`（+ match failure remark） |
| 8 | 加 `entry_point_abi` 后重跑 | ❌ 换错为 `memref.store`（部分走通） |
| 9 | `mlir-opt outlined.mlir --convert-to-llvm --gpu-to-llvm` | ❌ `got 5 kernel operands but expected 1` |

“未实测”的唯一一条是第 6 行——不是命令写错，而是本树没有 NVPTX target，任何需要 `llc`/PTX 的路径都跑不到。其余失败都是**真实诊断**，可以直接当“读错误信息”的练习。

## 27.13 踩雷集锦

1. **错误直觉：`gpu.launch` 可以只写一维。** → 正确认识：verifier 要求 3 个 block 参数与 3 个 thread 参数，旧语法已删。
2. **错误直觉：outlining 只是“把 region 搬走”。** → 正确认识：它同时改符号作用域、加 `container_module`、固化 `known_block_size/grid_size`、换终结符。
3. **错误直觉：`gpu.func` 的 memref 参数还是 memref。** → 正确认识：NVVM/ROCDL lowering 后变成 5 个标量（base/aligned/offset/sizes/strides）。
4. **错误直觉：`nvvm.read.ptx.sreg.tid.x` 的 `range` 是装饰。** → 正确认识：它来自 `known_block_size`，丢了会让后续优化失效；改 outlining 必须验证。
5. **错误直觉：`--convert-gpu-to-rocdl` 不需要目标。** → 正确认识：没有 `chipset=` 连 data layout 都算不出来，直接报 `'invalid' is not a valid AMDGPU architecture`。
6. **错误直觉：`--gpu-module-to-binary` 失败说明 IR 错。** → 正确认识：先看 `no targets are registered`——那是构建的 `LLVM_TARGETS_TO_BUILD` 问题。
7. **错误直觉：`failed to legalize` 只有 error 信息。** → 正确认识：上面的 `remark: match failure: ...` 才是根因（本例是缺 `spirv.entry_point_abi`）。
8. **错误直觉：`--convert-to-llvm` 可以在 `--gpu-to-llvm` 之前随便跑。** → 正确认识：它把 kernel operand 拆成 5 个值，和 `gpu.launch_func` 的 operand 段计数冲突。

## 27.14 本章重点整理

- GPU 方言分三层：launch（网格）、module/func（编译单元）、id/barrier（线程语义）；分界线是 kernel outlining。
- outlining 的五个产物：`container_module`、`launch_func`、`gpu.module`、`gpu.func` + `known_*_size`、注入的 id/dim op。
- NVVM 与 ROCDL 是同一层的两个后端，intrinsic 名与属性名完全不同；`range` 属性是二者共有的、来自 `known_block_size` 的优化依据。
- 二进制路径依赖 LLVM target；本树只有 X86/AArch64/RISCV，所以 PTX/HSACO/SPIR-V 序列化**不可实测**。
- SPIR-V 的入口属性 `spirv.entry_point_abi` 必须加在 `gpu.func` 上；缺了会得到 remark + failed to legalize。
- `gpu-to-llvm` 与 `convert-to-llvm` 有顺序/operand 表示的耦合，混合使用时要先确认 operand 段计数。
- NVGPU/XeGPU 是厂商高层模式层，最终仍落到 NVVM/ROCDL，不要把三者当同一层。

## 27.15 自我检核

- [ ] 能画出 outlining 前后的 IR 结构差异，并说出五个产物
- [ ] 能解释 `known_block_size` 如何变成 `nvvm.maxntid` 和 `range`
- [ ] 能说出 `gpu.func` 的 memref 参数为什么变成 5 个值
- [ ] 能复现 `'invalid' is not a valid AMDGPU architecture` 并说出修法
- [ ] 能解释 `no targets are registered` 是构建问题而非 IR 问题
- [ ] 能读懂 SPIR-V 的 match failure remark，并知道 `entry_point_abi` 该加在哪
- [ ] 能解释 `got 5 kernel operands but expected 1` 的成因
- [ ] 能说出 NVGPU/XeGPU/NVVM 三层的分工

## 27.16 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/GPU.md`**
  - 读哪里：`gpu.launch`/`gpu.func` 的语义与 memory model 部分
  - 能学到什么：三维网格与 workgroup 的正式定义；和本章关联：27.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/GPU/Transforms/KernelOutlining.cpp`**
  - 读哪里：`177-300`（`outlineKernelFuncImpl`）、`:284-310`（`convertToLaunchFuncOp`）
  - 能学到什么：region→function 的具体克隆步骤与符号处理；和本章关联：27.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/GPUToNVVM/LowerGpuOpsToNVVMOps.cpp`**
  - 读哪里：`540-640`（pattern 组装与 legality）、搜 `read.ptx.sreg`
  - 能学到什么：`range` 与 `maxntid` 从哪来；和本章关联：27.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/GPUToROCDL/LowerGpuOpsToROCDLOps.cpp`**
  - 读哪里：`820-900`（legality、chipset 与 data layout）
  - 能学到什么：为什么需要显式 chipset；和本章关联：27.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Dialect/GPU/module-to-binary-nvvm.mlir`**
  - 读哪里：`1-40`（`#nvvm.target` 的写法与 `#gpu.object` 结果）
  - 能学到什么：本树缺 NVPTX 时哪一步开始失败；和本章关联：27.7

GPU 的后端链路是“编译器内部”的自动化。下一章换一个完全不同的思路：把“怎么改 IR”写成一段可解释的 IR——Transform dialect，以及它的宿主语言 PDLL。

→ 下一章：[Ch 28 Transform dialect 与 PDLL](./28-transform-dialect-pdll.md)
