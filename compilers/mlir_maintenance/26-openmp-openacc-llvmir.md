# Ch 26 — OpenMP / OpenACC 到 LLVM IR：两条完全不同的路径

> **目标**：能说出 OpenMP 与 OpenACC 在 MLIR 里走的是**两套架构**（translation interface vs DialectConversion），知道 `__kmpc_*` 与 `__tgt_acc_*` 分别由谁生成，能用 `mlir-translate` 把两条路径都跑出真实 LLVM IR。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`。
>
> **前置**：Ch 19/21（DialectConversion 与 legality）、Ch 23（`mlir-translate` 与 target lowering 的分工）。

---

## 26.1 为什么这一章是“全仓第一热”

按 12 个月改动次数排，MLIR 里最热的文件不是任何一个 pass，而是：

```text
mlir/lib/Target/LLVMIR/Dialect/OpenMP/OpenMPToLLVMIRTranslation.cpp   161 次
```

它一个文件 11,263 行，比 `mlir/lib/Dialect/OpenMP` 整个 IR 目录（`OpenMPDialect.cpp` 6,211 行）还长。第二名 `NVVMOps.td`（105 次）、第三名 `NVVMDialect.cpp`（90 次）也都是“翻译/后端”类文件，而不是“优化”类文件。

这告诉你一个事实：**MLIR 里最容易被改的地方不是算法，而是“语义到 LLVM 的映射”**。原因是多方面的：

- 上层（Clang/Flang）不断引入新的 clause 与语义组合；
- LLVM 侧的 `OpenMPIRBuilder` 在不断演进；
- OpenMP 规范每几年加一批新构造；
- 设备端（`omp.target`）与主机端的 codegen 差异巨大。

而 OpenACC 走的是另一条路：`acc` 方言**没有** LLVM IR translation interface，它通过 `mlir/lib/Conversion/OpenACCToLLVM/` 用普通的 DialectConversion 降到 LLVM dialect，再由 `mlir-translate` 统一翻译。

**这两条路必须分清**，否则你会去错误的地方找 bug。

## 26.2 先建立直觉：同一个目标，两条流水线

```text
            OpenMP 路径                              OpenACC 路径
   ┌──────────────────────────────┐        ┌──────────────────────────────┐
   │ func.func + omp.parallel ... │        │ func.func + acc.parallel ... │
   └──────────────┬───────────────┘        └──────────────┬───────────────┘
                  │                                       │
       ① --convert-to-llvm                      ① acc 专用 lowering
          （arith/memref/func → llvm）             acc-compute-lowering
                  │                                 acc-cg-to-gpu
                  ▼                                       │
   ┌──────────────────────────────┐                       ▼
   │ llvm.func + omp.parallel     │        ┌──────────────────────────────┐
   │   （omp op 原样保留！）       │        │ func.func + gpu.launch ...   │
   └──────────────┬───────────────┘        └──────────────┬───────────────┘
                  │                                       │
       ② mlir-translate --mlir-to-llvmir        ② -acc-to-llvm
          （LLVMTranslationDialectInterface）      （普通 partial conversion）
                  │                                       │
                  ▼                                       ▼
   ┌──────────────────────────────┐        ┌──────────────────────────────┐
   │ LLVM IR:                     │        │ llvm.func + __tgt_acc_* 调用 │
   │   call @__kmpc_fork_call     │        └──────────────┬───────────────┘
   │   outlined @f..omp_par       │                       │
   │   call @__kmpc_for_static_*  │              ③ mlir-translate
   └──────────────────────────────┘                       ▼
                                            ┌──────────────────────────────┐
                                            │ LLVM IR:                     │
                                            │   call @__tgt_acc_kernel     │
                                            │   call @__tgt_acc_data_begin │
                                            └──────────────────────────────┘
```

两条路的**分界线**在“`omp`/`acc` op 什么时候消失”：

- OpenMP：`omp.*` 一直活到 `mlir-translate` 内部，被 `LLVMTranslationDialectInterface::convertOperation` 吃掉。
- OpenACC：`acc.*` 在 `-acc-to-llvm` 这一步就被 DialectConversion 换成 `llvm.call @__tgt_acc_*`。

## 26.3 OpenMP：translation interface 的三个钩子

`omp` 方言在 LLVM 侧注册的是一个 `LLVMTranslationDialectInterface`（`OpenMPToLLVMIRTranslation.cpp:10462`）：

```cpp
class OpenMPDialectLLVMIRTranslationInterface
    : public LLVMTranslationDialectInterface {
```

这个接口只有三个真正有用的钩子（定义在 `mlir/include/mlir/Target/LLVMIR/LLVMTranslationInterface.h:40-65`）：

| 钩子 | 调用时机 | 典型用途 |
|---|---|---|
| `convertOperation` | 遍历每个 op 时 | 把 `omp.parallel` 变成 `__kmpc_fork_call` |
| `amendOperation` | op 已被通用逻辑处理之后 | 给 `llvm.func` 补 `omp` 相关属性/元数据 |
| `initModuleTranslation` | 模块翻译开始前 | 注册设备端信息 |

`convertOperation` 是一个巨大的 `switch`（`:10965` 开始），对 `omp.parallel` 分派到 `convertOmpParallel`（`:5191`）：

```cpp
convertOmpParallel(omp::ParallelOp opInst, llvm::IRBuilderBase &builder,
```

但**真正生成 LLVM IR 的不是它**。MLIR 侧只负责把 op 的语义翻译成 `llvm::OpenMPIRBuilder` 的调用，代码生成在 LLVM 里：

```bash
wc -l llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp
```

真实输出：

```text
13800 llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp
```

**维护者最重要的认知**：`omp.parallel` 生成的三重嵌套函数/`__kmpc_*` 调用有 bug 时，一半概率根因在 `llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp`，而不在 `mlir/`。先看 MLIR 侧传进去的参数对不对，再去 LLVM 侧看 IRBuilder 怎么用这些参数。

## 26.4 第一个坑：`mlir-translate` 只注册了一部分方言

直接翻译一个含 `arith` 的 `omp` 模块：

```bash
cd /tmp/mlir_p5
cat > omp.mlir <<'EOF'
module {
  func.func @omp_test(%lb: i64, %ub: i64, %step: i64, %data: memref<100xf32>) {
    omp.parallel {
      omp.wsloop {
        omp.loop_nest (%i) : i64 = (%lb) to (%ub) step (%step) {
          %v = arith.constant 1.0 : f32
          %idx = arith.index_cast %i : i64 to index
          memref.store %v, %data[%idx] : memref<100xf32>
          omp.yield
        }
      }
      omp.terminator
    }
    return
  }
}
EOF
$T/bin/mlir-translate --mlir-to-llvmir omp.mlir
```

真实输出：

```text
omp.mlir:6:16: error: Dialect `arith' not found for custom op 'arith.constant' 
          %v = arith.constant 1.0 : f32
               ^
omp.mlir:6:16: note: Available dialects: arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu, llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm ; ...
```

注意 `Available dialects` 这一行——这是**权威列表**。`mlir-translate` 的工具级注册只包含这些方言，没有 `arith`、没有 `memref`、没有 `scf`。所以 `mlir-translate` 从来不是“任意 MLIR 到 LLVM IR”的万能工具，它要求输入**已经是 LLVM dialect + 少数带 translation interface 的方言**。

正确做法是先用 `--convert-to-llvm` 把其它方言降掉：

```bash
$T/bin/mlir-opt omp.mlir --convert-to-llvm
```

真实输出（节选，注意 `omp` 原样保留）：

```text
module {
  llvm.func @omp_test(%arg0: i64, %arg1: i64, %arg2: i64, %arg3: !llvm.ptr, %arg4: !llvm.ptr, %arg5: i64, %arg6: i64, %arg7: i64) {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    ...
    omp.parallel {
      omp.wsloop {
        omp.loop_nest (%arg8) : i64 = (%arg0) to (%arg1) step (%arg2) {
          %6 = llvm.mlir.constant(1.000000e+00 : f32) : f32
          %7 = llvm.extractvalue %5[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)> 
          %8 = llvm.getelementptr inbounds|nuw %7[%arg8] : (!llvm.ptr, i64) -> !llvm.ptr, f32
          llvm.store %6, %8 : f32, !llvm.ptr
          omp.yield
        }
      }
      omp.terminator
    }
```

`--convert-to-llvm` 是“把实现了 `ConvertToLLVMPatternInterface` 的方言逐个降掉”的聚合 pass。`omp` **没有**实现这个接口（它实现的是 translation interface），所以它被完整保留。这不是 bug，是设计。

## 26.5 完整翻译：`__kmpc_fork_call` 与 outlined 函数

把两步接起来：

```bash
$T/bin/mlir-opt omp.mlir --convert-to-llvm | $T/bin/mlir-translate --mlir-to-llvmir > omp.ll
```

真实输出（节选，`omp.ll` 共 188 行）：

```text
%struct.ident_t = type { i32, i32, i32, i32, ptr }

@0 = private unnamed_addr constant [23 x i8] c";unknown;unknown;0;0;;\00", align 1
@1 = private unnamed_addr constant %struct.ident_t { i32 0, i32 2, i32 0, i32 22, ptr @0 }, align 8
@2 = private unnamed_addr constant %struct.ident_t { i32 0, i32 514, i32 0, i32 22, ptr @0 }, align 8
@3 = private unnamed_addr constant %struct.ident_t { i32 0, i32 66, i32 0, i32 22, ptr @0 }, align 8

define void @omp_test(i64 %0, i64 %1, i64 %2, ptr %3, ptr %4, i64 %5, i64 %6, i64 %7) {
  %structArg = alloca { ptr, ptr, ptr, ptr }, align 8
  %.reloaded = alloca i64, align 8
  ...
omp_parallel:                                     ; preds = %entry
  %gep_.reloaded = getelementptr { ptr, ptr, ptr, ptr }, ptr %structArg, i32 0, i32 0
  store ptr %.reloaded, ptr %gep_.reloaded, align 8
  ...
  call void (ptr, i32, ptr, ...) @__kmpc_fork_call(ptr @1, i32 1, ptr @omp_test..omp_par, ptr %structArg)
  br label %omp.par.exit
```

四件事可以逐条对上：

1. **`%struct.ident_t` 与三个 `@N` 全局**：OpenMP 的 location 描述符（源码位置/版本），每个 `omp.parallel`/`wsloop` 都有自己的 ident。
2. **`@omp_test..omp_par`**：`omp.parallel` 的 region 被 **outline** 成一个内部函数，`__kmpc_fork_call` 把它当回调传进去。名字里的 `..omp_par` 是 OpenMPIRBuilder 的命名约定。
3. **`%structArg` + 一串 `%.reloaded`**：region 捕获的值（`%lb/%ub/%step` 和 memref 描述符）被**打包成结构体指针**传进 outline 函数——这是 OpenMP 的 ABI（参数通过 void* 数组传递）。
4. **`call void (ptr, i32, ptr, ...)`**：`__kmpc_fork_call` 是**可变参数**函数，第三个参数是函数指针，之后是捕获参数。

再往里的 `omp.wsloop`：

```text
omp.wsloop.region:                                ; preds = %omp.par.region1
  %6 = icmp slt i64 %2, 0
  %7 = sub i64 0, %2
  %8 = select i1 %6, i64 %7, i64 %2
  %9 = select i1 %6, i64 %3, i64 %4
  %10 = select i1 %6, i64 %4, i64 %3
  %11 = sub nsw i64 %10, %9
  ...
  %omp_loop.tripcount = select i1 %12, i64 0, i64 %17
  br label %omp_loop.preheader

omp_loop.preheader:                               ; preds = %omp.wsloop.region
  store i64 0, ptr %p.lowerbound, align 4
  %18 = sub i64 %omp_loop.tripcount, 1
  store i64 %18, ptr %p.upperbound, align 4
  store i64 1, ptr %p.stride, align 4
  %omp_global_thread_num = call i32 @__kmpc_global_thread_num(ptr @1)
  call void @__kmpc_for_static_init_8u(ptr @2, i32 %omp_global_thread_num, i32 34, ptr %p.lastiter, ptr %p.lowerbound, ptr %p.upperbound, ptr %p.stride, i64 1, i64 0)
```

这段是理解“MLIR 到底做了多少”的最好证据：

- **步长为负时的边界交换**（`select i1 %6, i64 %3, i64 %4`）在 MLIR 侧就展开了；
- **trip count 的手工计算**（`udiv`、`add 1`、`select`）也在 MLIR 侧；
- 但**真正的“哪个线程拿哪一段迭代”**交给 `__kmpc_for_static_init_8u`——也就是 OpenMP runtime。

所以 `omp.loop_nest` 的 lowering 分三块：MLIR 算 trip count、OpenMPIRBuilder 生成循环骨架、runtime 决定调度。

### 26.5.1 元数据

`omp.ll` 的尾部有三个值得注意的元数据：

```text
attributes #0 = { nounwind }
attributes #1 = { convergent nounwind }

!llvm.module.flags = !{!0}

!0 = !{i32 2, !"Debug Info Version", i32 3}
!1 = !{i64 8}
!2 = !{!3}
!3 = !{i64 2, i64 -1, i64 -1, i1 true}
```

- `convergent`：`__kmpc_barrier` 必须标 convergent，否则优化器可能把 barrier 移出分支导致死锁。这是**语义级**属性，不是优化提示。
- `!callback`（`!2`/`!3`）：给 `__kmpc_fork_call` 的间接调用回调约定，允许优化器在 outline 函数内联时保持语义。
- `!align !1`：`!{i64 8}`，描述捕获指针的对齐，用于证明 load/store 的 align。

如果你在做“omp lowering 后对齐信息丢失”的 bug，查的就是 `!align` 这条链：MLIR 侧的 `memref` 对齐 → 描述符 → `amendOperation` 补的属性 → LLVM 的 `align`。

## 26.6 OpenACC：没有 translation interface

先验证一个常见误解：

```bash
ls mlir/lib/Target/LLVMIR/Dialect/
```

真实输出：

```text
ArmNeon  ArmSME  ArmSVE  Builtin  CMakeLists.txt  GPU  LLVMIR  NVVM  OpenMP  Ptr  ROCDL  SPIRV  VCIX  XeVM
```

**没有 `OpenACC` 目录**。`mlir/include/mlir/Target/LLVMIR/Dialect/` 下也没有 `OpenACC`。也就是说：`acc.*` op 不可能通过 translation interface 变成 LLVM IR。

OpenACC 的路在 `mlir/lib/Conversion/OpenACCToLLVM/`，一共 8 个文件、3,285 行：

```bash
wc -l mlir/lib/Conversion/OpenACCToLLVM/*.cpp
```

真实输出：

```text
   896 lib/Conversion/OpenACCToLLVM/ACCAtomicPatterns.cpp
   460 lib/Conversion/OpenACCToLLVM/ACCDataDirectivePatterns.cpp
   578 lib/Conversion/OpenACCToLLVM/ACCDataRuntime.cpp
   253 lib/Conversion/OpenACCToLLVM/ACCExecutableDirectivePatterns.cpp
   115 lib/Conversion/OpenACCToLLVM/ACCHostDataPatterns.cpp
   490 lib/Conversion/OpenACCToLLVM/ACCKernelLaunchPatterns.cpp
    80 lib/Conversion/OpenACCToLLVM/ACCToLLVM.cpp
   413 lib/Conversion/OpenACCToLLVM/ACCToLLVMUtils.cpp
```

入口 `ACCToLLVM.cpp` 短得惊人（80 行），核心就是注册 pattern + 一次 partial conversion（`ACCToLLVM.cpp:72-78`）：

```cpp
  LLVMConversionTarget target(getContext());
  configureACCExecutableDirectiveConversionLegality(target);
  configureACCDataDirectiveConversionLegality(target);
  configureACCHostDataConversionLegality(target);
  configureACCKernelLaunchConversionLegality(target);
  configureACCAtomicConversionLegality(target);
  if (failed(applyPartialConversion(module, target, std::move(patterns))))
    signalPassFailure();
```

对比一下：OpenMP 是 11,263 行的 translation interface，OpenACC 是 80 行的 pass 注册 + 3,205 行的 pattern。**架构差异直接决定了你该去哪里找 bug**：

- `omp.*` 生成错了 → 先看 `OpenMPToLLVMIRTranslation.cpp`，再看 `OMPIRBuilder.cpp`；
- `acc.*` 生成错了 → 先看对应 `ACC*Patterns.cpp` 的 pattern，是标准的 `OpConversionPattern` 调试流程（Ch 19 的 `-debug-only=dialect-conversion` 直接可用）。

## 26.7 OpenACC 的三级流水线

`acc.parallel` 不是一步到 LLVM 的。实测：

```bash
# 用官方测试的第一段作为输入
sed -n '5,27p' /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Dialect/OpenACC/acc-compute-lowering-compute.mlir > acc_par.mlir
$T/bin/mlir-opt acc_par.mlir --acc-compute-lowering
```

真实输出：

```text
module {
  func.func @parallel_gang_loop(%arg0: memref<1xi32>) {
    %c10 = arith.constant 10 : index
    %0 = acc.copyin varPtr(%arg0 : memref<1xi32>) -> memref<1xi32>
    acc.kernel_environment dataOperands(%0 : memref<1xi32>) {
      %1 = acc.par_width %c10 par_dim(#acc.par_dim<block_x>)
      acc.compute_region launch(%arg1 = %1) ins(%arg2 = %0) : (memref<1xi32>) {
        %c1 = arith.constant 1 : index
        %c0 = arith.constant 0 : index
        %c99 = arith.constant 99 : index
        scf.parallel (%arg3) = (%c0) to (%c99) step (%c1) {
          %2 = arith.addi %arg3, %c1 overflow<nsw> : index
          %3 = arith.index_cast %2 : index to i32
          memref.store %3, %arg2[%c0] : memref<1xi32>
          scf.reduce 
        } {acc.collapse_count = 1 : i64, acc.par_dims = #acc<par_dims[block_x]>}
        acc.yield
      } <{origin = "acc.parallel"}>
    }
    acc.copyout accPtr(%0 : memref<1xi32>) to varPtr(%arg0 : memref<1xi32>)
    return
  }
}
```

第一步（`acc-compute-lowering`）：`acc.parallel` → `acc.kernel_environment` + `acc.compute_region`，循环体变成 `scf.parallel`，并带上 `acc.par_dims`（映射到 `block_x`）。`lib/Dialect/OpenACC/Transforms/ACCComputeLowering.cpp` 的注释（`:17-29`）把这一步的目标写得很清楚：把“计算构造”和“数据环境”分开。

第二步（`acc-cg-to-gpu`）：`acc.compute_region` → `gpu.launch`。这个 pass 是 **func-level** 的，直接写会报错：

```bash
$T/bin/mlir-opt acc_par.mlir --acc-compute-lowering --acc-cg-to-gpu
```

真实输出：

```text
<unknown>:0: error: unable to schedule pass 'ACCCGToGPU' on a PassManager intended to run on 'builtin.module'!
```

正确写法：

```bash
$T/bin/mlir-opt acc_par.mlir --pass-pipeline='builtin.module(func.func(acc-compute-lowering,acc-cg-to-gpu))'
```

真实输出（节选）：

```text
      gpu.launch blocks(%arg1, %arg2, %arg3) in (%arg7 = %c10, %arg8 = %c1, %arg9 = %c1) threads(%arg4, %arg5, %arg6) in (%arg10 = %c1, %arg11 = %c1, %arg12 = %c1) {
        %grid_dim_x = gpu.grid_dim x
        ...
        %1 = arith.addi %arg1, %c1_0 overflow<nsw> : index
        %2 = arith.index_cast %1 : index to i32
        memref.store %2, %0[%c0] : memref<1xi32>
        gpu.terminator
      }
```

到这里 OpenACC 已经变成 GPU 方言了——后面就接 Ch 27 的 NVVM/ROCDL/SPIR-V。**这就是 OpenACC 的真正架构：它不是“翻译到 LLVM IR”，而是“降级到 GPU 方言，再走 GPU 后端”**。

第三步（`-acc-to-llvm`）：对**主机侧**的 `acc.*`（`acc.copyin`/`acc.copyout`/`acc.kernel_environment`/`gpu.launch_func`）生成 runtime 调用。用官方 kernel-launch 测试的第一段：

```bash
awk 'NR>=28 && /^\/\/ -----/{exit} NR>=28' \
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Conversion/OpenACCToLLVM/kernel-launch.mlir > acc_kernel.mlir
$T/bin/mlir-opt acc_kernel.mlir --acc-to-llvm | grep -n '__tgt_acc\|llvm.func'
```

真实输出：

```text
22:  llvm.func @loop_0()
38:  llvm.func @parallel_copy(%arg0: !llvm.ptr, %arg1: i64) {
84:    llvm.call @__tgt_acc_data_begin(%3, %4, %5, %7, %16, %20, %24, %28, %32, %6, %36, %2) : (!llvm.ptr, i64, i64, i32, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, i64) -> ()
186:    %123 = llvm.call @__tgt_acc_kernel(%119, %42, %120, %121, %115, %2, %118, %122) : (!llvm.ptr, !llvm.ptr, i64, i64, !llvm.ptr, i64, !llvm.ptr, !llvm.ptr) -> i32
244:    llvm.call @__tgt_acc_data_end(%132, %133, %134, %136, %145, %149, %153, %157, %161, %135, %165, %2) : (!llvm.ptr, i64, i64, i32, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, i64) -> ()
247:  llvm.func @__tgt_acc_data_begin(!llvm.ptr, i64, i64, i32, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, i64)
248:  llvm.func @__tgt_acc_kernel(!llvm.ptr, !llvm.ptr, i64, i64, !llvm.ptr, i64, !llvm.ptr, !llvm.ptr) -> i32
251:  llvm.func @abort()
252:  llvm.func @__tgt_acc_data_end(!llvm.ptr, i64, i64, i32, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, !llvm.ptr, i64)
```

三个 runtime 函数就是 OpenACC 的全部 ABI 表面：`__tgt_acc_data_begin`（进入 data region）、`__tgt_acc_kernel`（启动 kernel）、`__tgt_acc_data_end`（退出 data region）。

再走一步 `mlir-translate`，得到纯 LLVM IR（140 行）：

```bash
$T/bin/mlir-opt acc_kernel.mlir --acc-to-llvm | $T/bin/mlir-translate --mlir-to-llvmir > acc.ll
```

真实输出（节选）：

```text
  call void @__tgt_acc_data_begin(ptr @acc.ident.12.13.13360826627721765758, i64 1, i64 0, i32 1, ptr %3, ptr %5, ptr %7, ptr %9, ptr %11, ptr null, ptr %13, i64 -1)
  %52 = call i32 @__tgt_acc_kernel(ptr @acc.ident.15.7.11625014803744216033, ptr @loop_0, i64 0, i64 0, ptr %51, i64 -1, ptr @acc.kernel_name.loop_0, ptr null)
```

注意 `@acc.ident.*` 里的数字串（`13360826627721765758`）——它是**内容哈希**，由 `ACCToLLVMUtils.cpp` 生成，用来保证同一个 ident 在不同编译单元里稳定。这也是一个常被忽略的维护点：改 ident 的构造方式会改变二进制里的符号名。

## 26.8 host/device 的分工

两条路径都区分 host 与 device 代码，但机制不同。

**OpenMP**：靠 `omp.target` / `omp.is_target_device` 属性 + `OpenMPIRBuilder::Config.isTargetDevice()`。`OpenMPToLLVMIRTranslation.cpp:10973` 有一段明确的拒绝逻辑：

```cpp
  if (ompBuilder->Config.isTargetDevice() &&
      !isa<omp::TargetOp, omp::MapInfoOp, omp::TerminatorOp, omp::YieldOp>(
          op) &&
      isHostDeviceOp(op))
    return op->emitOpError() << "unsupported host op found in device";
```

意思是：设备端编译时，只允许 target/map/terminator/yield 这几个“外壳”op；其它 host op 混进来就报错。这条错误信息在真实 bug 报告里出现频率很高，通常意味着 pass pipeline 把 host 代码错误地留在了 device region 里。

**OpenACC**：靠 `acc-specialize-for-host` / `acc-specialize-for-device` 两个 pass 显式分叉：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -E 'acc-specialize|acc-routine-lowering|acc-implicit'
```

真实输出（节选）：

```text
      --acc-implicit-data                                    -   Generate implicit data attributes for OpenACC compute constructs
      --acc-implicit-declare                                 -   Applies implicit acc declare to globals referenced in compute and routine acc regions
      --acc-implicit-routine                                 -   Generate implicit acc routine for functions in acc regions
      --acc-routine-lowering                                 -   Specialize `acc routine` functions for device
      --acc-specialize-for-device                            -   Strip OpenACC constructs inside device code
      --acc-specialize-for-host                              -   Convert OpenACC operations for host execution
```

所以 OpenACC 的 host/device 是**两个独立的编译单元**：host 版本里 `acc.compute_region` 被替换成 kernel launch，device 版本里 `acc.*` 被剥掉只留计算体。这是为什么 `acc` 需要 22 个 transform pass（`ls mlir/lib/Dialect/OpenACC/Transforms/*.cpp | wc -l` 实测），而 `omp` 只需要 translation 里的分支。

## 26.9 源码落点

| 主题 | 文件:行 |
|---|---|
| OpenMP translation interface 注册 | `mlir/lib/Target/LLVMIR/Dialect/OpenMP/OpenMPToLLVMIRTranslation.cpp:10462`、`:11252` |
| `convertOperation` 分派 | 同上 `:10965`；`omp.parallel` → `:11055` |
| `convertOmpParallel` | 同上 `:5191` |
| `omp.wsloop` / `omp.loop_nest` | 同上（按 `LoopWrapperInterface` 分支，`:10986` 起） |
| `amendOperation`（属性/元数据） | 同上 `:10503` |
| 设备端 host op 检查 | 同上 `:10973` |
| LLVM 侧真正生成 | `llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp`（13,800 行） |
| module 翻译入口 | `mlir/lib/Target/LLVMIR/ModuleTranslation.cpp:2694`（`translateModuleToLLVMIR`） |
| 接口三个钩子 | `mlir/include/mlir/Target/LLVMIR/LLVMTranslationInterface.h:40-65` |
| OpenACC → LLVM 注册 | `mlir/lib/Conversion/OpenACCToLLVM/ACCToLLVM.cpp:72-78` |
| OpenACC compute 降级 | `mlir/lib/Dialect/OpenACC/Transforms/ACCComputeLowering.cpp`（`:17-29` 注释、`:483` pass 类） |
| OpenACC kernel launch pattern | `mlir/lib/Conversion/OpenACCToLLVM/ACCKernelLaunchPatterns.cpp` |
| OpenACC 方言本体（12 个月 62 次改动） | `mlir/lib/Dialect/OpenACC/IR/OpenACC.cpp` |

## 26.10 可跑实验清单

| # | 命令 | 观察什么 |
|---|---|---|
| 1 | `mlir-translate --mlir-to-llvmir omp.mlir` | 工具级方言子集：`arith` 不在列表里 |
| 2 | `mlir-opt omp.mlir --convert-to-llvm` | `omp.*` 原样保留（没有 `ConvertToLLVMPatternInterface`） |
| 3 | `mlir-opt omp.mlir --convert-to-llvm \| mlir-translate --mlir-to-llvmir` | `__kmpc_fork_call` + outlined `..omp_par` |
| 4 | `grep -n '__kmpc_for_static_init_8u\|__kmpc_barrier' omp.ll` | runtime 调度点 |
| 5 | `ls mlir/lib/Target/LLVMIR/Dialect/` | 没有 `OpenACC` 目录 |
| 6 | `mlir-opt acc_par.mlir --acc-compute-lowering` | `acc.parallel` → `acc.compute_region` |
| 7 | `mlir-opt acc_par.mlir --pass-pipeline='builtin.module(func.func(acc-compute-lowering,acc-cg-to-gpu))'` | `acc.compute_region` → `gpu.launch` |
| 8 | `mlir-opt acc_kernel.mlir --acc-to-llvm` | `__tgt_acc_*` 三个 runtime 调用 |
| 9 | `mlir-opt acc_kernel.mlir --acc-to-llvm \| mlir-translate --mlir-to-llvmir` | 纯 LLVM IR（140 行） |

## 26.11 踩雷集锦

1. **错误直觉：`mlir-translate --mlir-to-llvmir` 能翻译任意 MLIR。** → 正确认识：它只注册了十几个方言；`arith`/`memref`/`scf` 都不在，必须先 `--convert-to-llvm`。
2. **错误直觉：`--convert-to-llvm` 会把 `omp.*` 也降掉。** → 正确认识：`omp` 实现的是 translation interface，不是 `ConvertToLLVMPatternInterface`；它一直活到 `mlir-translate` 内部。
3. **错误直觉：`omp.parallel` 的 lowering 全在 MLIR 里。** → 正确认识：MLIR 侧 11k 行只负责“翻译成 OpenMPIRBuilder 调用”；真正的 IR 生成在 `llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp`（13.8k 行）。
4. **错误直觉：OpenACC 也是 translation interface。** → 正确认识：`mlir/lib/Target/LLVMIR/Dialect/` 下根本没有 `OpenACC`；它走 `lib/Conversion/OpenACCToLLVM` 的 partial conversion。
5. **错误直觉：`acc.parallel` 直接变 LLVM IR。** → 正确认识：先 `acc.compute_region`，再 `gpu.launch`，再 GPU 后端；`-acc-to-llvm` 只处理 host 侧的 data/kernel 调用。
6. **错误直觉：`acc-cg-to-gpu` 可以像普通 pass 一样写在 `mlir-opt` 后面。** → 正确认识：它是 func-level pass，必须 `builtin.module(func.func(...))`。
7. **错误直觉：`omp.parallel` 结构写错（比如在 `omp.wsloop` 里多放一个 `omp.terminator`）会被忽略。** → 正确认识：`loop wrapper` 要求恰好一个嵌套 op，否则报 `does not contain exactly one nested op`。
8. **错误直觉：`convergent` 属性只是优化提示。** → 正确认识：它是 barrier 的语义约束；去掉会导致合法的重排变成死锁。

## 26.12 本章重点整理

- 同一个目标（LLVM IR），两套架构：OpenMP 用 `LLVMTranslationDialectInterface`，OpenACC 用 `DialectConversion`。
- `mlir-translate` 的方言注册是**工具级子集**；`Available dialects` 那行是权威答案。
- `omp.parallel` → `__kmpc_fork_call` + outlined 函数 + `structArg` 打包，这是 OpenMP 的 ABI；`omp.wsloop` → `__kmpc_for_static_init_*`，调度交给 runtime。
- MLIR 侧算 trip count、OpenMPIRBuilder 生成骨架、runtime 决定线程分配——三层分工。
- OpenACC 的路线是 `acc.parallel` → `acc.compute_region` → `gpu.launch` → GPU 后端；host 侧 `-acc-to-llvm` 生成 `__tgt_acc_data_begin/kernel/data_end`。
- `convergent`、`!callback`、`!align`、`!llvm.module.flags` 是这条链上的关键元数据；丢一个就可能是语义 bug。
- host/device 分叉：OpenMP 靠 `isTargetDevice` 运行时判断，OpenACC 靠 `acc-specialize-for-{host,device}` 两个 pass。

## 26.13 自我检核

- [ ] 能画出两条路径的分叉点（`omp.*` 何时消失 vs `acc.*` 何时消失）
- [ ] 能说出 `mlir-translate` 为什么拒绝 `arith`，以及正确的前置 pass
- [ ] 能解释 `__kmpc_fork_call` 的四个参数各是什么
- [ ] 能解释 `structArg`/`%.reloaded` 的作用（region 捕获打包）
- [ ] 能说出 `omp.wsloop` 的 trip count 计算发生在哪一侧
- [ ] 能解释 `convergent` 缺失会导致什么
- [ ] 能说出 OpenACC 的三级流水线与三个 `__tgt_acc_*` 调用
- [ ] 能解释为什么 `acc-cg-to-gpu` 需要 `func.func` 锚点

## 26.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Target/LLVMIR/LLVMTranslationInterface.h`**
  - 读哪里：`35-70`（三个钩子的声明与注释）
  - 能学到什么：translation interface 与 conversion 的本质区别；和本章关联：26.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Target/LLVMIR/Dialect/OpenMP/OpenMPToLLVMIRTranslation.cpp`**
  - 读哪里：`10462-10560`（接口类与 `amendOperation`）、`10965-11070`（`convertOperation` 分派）
  - 能学到什么：11k 行文件的组织方式与分派入口；和本章关联：26.3/26.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Frontend/OpenMP/OMPIRBuilder.cpp`**
  - 读哪里：搜 `createParallel`、`createStaticWorkshareLoop`
  - 能学到什么：`__kmpc_*` 调用与 outlined 函数的真正生成逻辑；和本章关联：26.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/OpenACCToLLVM/ACCToLLVM.cpp`**
  - 读哪里：`35-80`（pattern 注册与 `applyPartialConversion`）
  - 能学到什么：80 行就能接起一整套 conversion；和本章关联：26.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/OpenMPDialect/ODS.md`**
  - 读哪里：clause 与 op 的对应关系、`loop wrapper` 的结构约束
  - 能学到什么：为什么 `omp.wsloop` 里只能有一个嵌套 op；和本章关联：26.11 第 7 条

OpenMP 与 OpenACC 最终都汇入 GPU/NVVM/ROCDL 这一层。下一章专门看 GPU 方言：kernel outlining 为什么是分水岭，以及本树里哪些后端路径能真跑、哪些只能停在诊断。

→ 下一章：[Ch 27 GPU 方言与后端链路](./27-gpu-dialects.md)
