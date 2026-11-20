# Ch 2 — 一次编译的完整旅程：driver → cc1 → opt → llc → MC → lld

> **目标**：把「`clang min.c` 发生了什么」拆成可验证的阶段：driver 怎么生成 cc1 命令，cc1 怎么把 AST 变成 LLVM IR，`-O2` 的 pipeline 从哪里来，`llc` 和 MC 怎么把 IR 变成机器码，`lld` 在哪一步才出现。学完这章，你能从一条命令的输出判断它停在了链路哪一段，并能把每个阶段指到真实源码入口。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`，LLVM 24.0.0git，assertions ON。构建自带的版本串是 `a5eaf194d8b6`，与源码 checkout 不是同一个提交；本章命令输出都来自该构建，源码行号来自 `7c7a9072f736`。示例输入是下面这个 `min.c`，所有 IR、汇编和 pipeline 文本都真实运行过。

## 先看全链：一条命令背后的阶段

```text
min.c
  │
  │  clang driver
  │  Driver::BuildCompilation → BuildActions → BuildJobs
  ▼
cc1 命令（clang -cc1 ...）
  │
  │  CompilerInvocation::CreateFromArgs
  │  ExecuteCompilerInvocation
  ▼
Clang AST
  │
  │  CodeGenModule::EmitGlobal / EmitGlobalFunctionDefinition
  ▼
LLVM IR（内存中的 Module）
  │
  │  PassBuilder::buildPerModuleDefaultPipeline
  │  （clang 在进程内跑，不启动 opt 可执行文件）
  ▼
优化后的 LLVM IR
  │
  │  CodeGenTargetMachineImpl::addPassesToEmitFile
  │  TargetPassConfig::addISelPasses → addMachinePasses
  ▼
Machine IR（SelectionDAG/GlobalISel、寄存器分配、机器 pass）
  │
  │  MCStreamer / MCObjectStreamer / MCELFStreamer
  │  MCAssembler::Finish
  ▼
汇编 .s 或目标文件 .o
  │
  │  lld::elf::link → LinkerDriver::linkerMain
  ▼
可执行文件
```

这张图有一个容易误解的地方：图中的 `opt` 和 `llc` 是两个**独立可执行文件**，但 `clang` 编译时不会为每个源文件启动它们。`clang` 的 cc1 进程内部调用同一套 `PassBuilder` 和 `TargetMachine` 代码；`opt`/`llc` 只是让你单独观察这两个阶段的工具。`clang -###` 最能说明这一点：`-c` 时它只打印一条 cc1 命令，没有 `opt` 或 `llc`。

本章用这个输入：

```c
int sum(const int *a, int n) {
  int s = 0;
  for (int i = 0; i < n; ++i)
    s += a[i];
  return s;
}
```

## 阶段 0：driver 把命令行拆成 jobs

入口在 `clang/lib/Driver/Driver.cpp`：

- `Driver::BuildCompilation`（`:1552`）解析参数、建立 `Compilation`。
- `Driver::BuildActions`（`:3632`）根据 `-c`、`-S`、`-emit-llvm` 等决定 action 类型。
- `Driver::BuildJobs`（`:4663`）把 action 变成具体命令。
- `Driver::ExecuteCompilation`（`:2446`）执行 job；带 `-###` 时只打印，不执行。

具体到 C 语言编译，`clang/lib/Driver/ToolChains/Clang.cpp:5274` 的 `Clang::ConstructJob` 组装 cc1 参数，`:8713` 附近把命令包装成 `CC1Command`。所以「driver 做了什么」可以概括成：**选择工具链、选择 action、生成 cc1 参数、决定是否再生成链接 job**。

实测：

```bash
$ B=/home/ubuntu/llvm/builds/pr-lane/bin
$ $B/clang -### -O2 -c min.c -o min.o
clang version 24.0.0git (https://gh-proxy.com/https://github.com/llvm/llvm-project.git a5eaf194d8b6b5f4903b67d297e66a5de7c79a5c)
Target: x86_64-unknown-linux-gnu
Thread model: posix
InstalledDir: /home/ubuntu/llvm/builds/pr-lane/bin
Build config: +assertions
 (in-process)
 "/home/ubuntu/llvm/builds/pr-lane/bin/clang-24" "-cc1" "-triple" "x86_64-unknown-linux-gnu" "-O2" "-emit-obj" ... "-o" "min.o" "-x" "c" "min.c"
```

（中间的系统 include 路径和大量 `-f...` 参数用 `...` 截断；关键字段是 `-cc1`、`-O2`、`-emit-obj`、`-o min.o`。）

`-###` 只打印 job，不执行编译。它证明了两件事：

1. 这一步的可执行文件是 `clang-24`，参数里带 `-cc1`。
2. `-c` 的 action 是 `-emit-obj`，没有链接 job；因此这条命令不会调用 `lld`。

## 阶段 1：cc1 解析参数并进入前端

cc1 进程拿到的是 driver 生成的一长串参数。解析入口是 `clang/lib/Frontend/CompilerInvocation.cpp`：

- `CompilerInvocation::CreateFromArgs`（`:5295`）是公开入口。
- `CreateFromArgsImpl`（`:5151`）按 `CC1Option` 可见性解析参数。
- `ParseLangArgs`（`:4080`）处理语言选项。
- `ParseCodeGenArgs`（`:1853`）处理代码生成选项。

解析完成后，`clang/lib/FrontendTool/ExecuteCompilerInvocation.cpp:241` 的 `ExecuteCompilerInvocation` 根据 frontend action 进入编译。对 `min.c` 来说，action 是「生成目标文件」，于是前端做语义分析，再由 `clang/lib/CodeGen/CodeGenModule.cpp` 把 AST 降到 LLVM IR：

- `CodeGenModule::EmitGlobal`（`:4874`）处理全局声明/定义。
- `CodeGenModule::EmitGlobalFunctionDefinition`（`:7109`）生成函数体。
- `CodeGenModule::EmitTopLevelDecl`（`:8076`）遍历顶层声明。

这里的 `CodeGenModule` 不是 LLVM 的 `CodeGen` 目录，而是 Clang 的 AST→IR 层。命名相似，职责完全不同：Clang 的 `CodeGenModule` 产出 LLVM IR；LLVM 的 `llvm/lib/CodeGen` 把 IR 降到 Machine IR。

## 阶段 2：得到 LLVM IR

先说明一个实际踩到的坑：题目要求的原始命令

```bash
$ $B/clang -cc1 -emit-llvm -O1 -S min.c -o -
```

在这个构建上失败：

```text
error: '-emit-llvm' action ignored; '-S' action specified previously
```

原因是 raw cc1 把 `-emit-llvm` 和 `-S` 都当作 action，二者不能这样叠加。driver 层的 `clang -S -emit-llvm` 可以做组合翻译，但 `-cc1` 是更底层的接口。去掉 `-S` 后，`-emit-llvm` 本身就输出文本 IR：

```bash
$ $B/clang -cc1 -emit-llvm -O1 min.c -o -
```

实测输出 45 行：

```llvm
; ModuleID = 'min.c'
source_filename = "min.c"
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-unknown-linux-gnu"

; Function Attrs: nofree norecurse nosync nounwind memory(argmem: read)
define dso_local i32 @sum(ptr nofree noundef readonly captures(none) %a, i32 noundef %n) local_unnamed_addr #0 {
entry:
  %cmp4 = icmp sgt i32 %n, 0
  br i1 %cmp4, label %for.body.preheader, label %for.cond.cleanup

for.body.preheader:                               ; preds = %entry
  %wide.trip.count = zext nneg i32 %n to i64
  br label %for.body

for.cond.cleanup:                                 ; preds = %for.body, %entry
  %s.0.lcssa = phi i32 [ 0, %entry ], [ %add, %for.body ]
  ret i32 %s.0.lcssa

for.body:                                         ; preds = %for.body.preheader, %for.body
  %indvars.iv = phi i64 [ 0, %for.body.preheader ], [ %indvars.iv.next, %for.body ]
  %s.05 = phi i32 [ 0, %for.body.preheader ], [ %add, %for.body ]
  %arrayidx = getelementptr inbounds nuw [4 x i8], ptr %a, i64 %indvars.iv
  %0 = load i32, ptr %arrayidx, align 4, !tbaa !6
  %add = add nsw i32 %0, %s.05
  %indvars.iv.next = add nuw nsw i64 %indvars.iv, 1
  %exitcond.not = icmp eq i64 %indvars.iv.next, %wide.trip.count
  br i1 %exitcond.not, label %for.cond.cleanup, label %for.body, !llvm.loop !7
}

attributes #0 = { nofree norecurse nosync nounwind memory(argmem: read) "min-legal-vector-width"="0" "no-trapping-math"="true" "stack-protector-buffer-size"="8" "target-features"="+cx8,+mmx,+sse,+sse2,+x87" }

!llvm.ident = !{!0}
!llvm.errno.tbaa = !{!1}

!0 = !{!"clang version 24.0.0git (https://gh-proxy.com/https://github.com/llvm/llvm-project.git a5eaf194d8b6b5f4903b67d297e66a5de7c79a5c)"}
!1 = !{!2, !3, i64 0}
!2 = !{!"__libc_errno", !3, i64 0}
!3 = !{!"int", !4, i64 0}
!4 = !{!"omnipotent char", !5, i64 0}
!5 = !{!"Simple C/C++ TBAA"}
!6 = !{!3, !3, i64 0}
!7 = distinct !{!7, !8, !9}
!8 = !{!"llvm.loop.mustprogress"}
!9 = !{!"llvm.loop.unroll.disable"}
```

对照 `-O0`，区别不在「有没有 `add`」，而在抽象层级：

```llvm
; -O0 的函数开头
define dso_local i32 @sum(ptr noundef %a, i32 noundef %n) #0 {
entry:
  %a.addr = alloca ptr, align 8
  %n.addr = alloca i32, align 4
  %s = alloca i32, align 4
  %i = alloca i32, align 4
  store ptr %a, ptr %a.addr, align 8
  store i32 %n, ptr %n.addr, align 4
  store i32 0, ptr %s, align 4
  store i32 0, ptr %i, align 4
  br label %for.cond
```

`-O1` 已经把 `alloca`/`load`/`store` 提升成 SSA 值（`mem2reg`），并引入 `phi`；`-O0` 保留内存槽和 `optnone` 属性。这个差异是后面所有中端分析的起点。

## 阶段 3：opt 与默认 pipeline

`opt` 的 pipeline 可以用一条命令直接打印：

```bash
$ $B/opt -passes='default<O1>' -print-pipeline-passes -disable-output < /dev/null
```

实测在这个构建上，stdin 为空时命令以 0 退出并打印 pipeline；`< /dev/null` 让非交互执行不必等待输入。输出是一行很长的文本，下面是开头部分，`...` 处为排版截断：

```text
memprof-remove-attributes,annotation2metadata,forceattrs,inferattrs,coro-early,function<eager-inv>(ee-instrument<>,lower-expect,simplifycfg<...>,sroa<modify-cfg>,early-cse<>),openmp-opt,ipsccp,called-value-propagation,globalopt,function<eager-inv>(mem2reg,instcombine<max-iterations=1;no-verify-fixpoint>,simplifycfg<...>),always-inline,require<globals-aa>,function(invalidate<aa>),require<profile-summary>,cgscc(devirt<4>(inline,...,function<eager-inv;no-rerun>(sroa<modify-cfg>,early-cse<memssa>,...,loop-mssa(loop-instsimplify,loop-simplifycfg,licm<no-allowspeculation>,loop-rotate<...>,licm<allowspeculation>,simple-loop-unswitch<...>),...,instcombine<...>),...)),...verify
```

把 `O0` 到 `O3` 各打印一次，比较字节数（`wc -c`，包含末尾换行）：

| 级别 | pipeline 文本字节数 | 观察到的关键标记 |
| --- | ---: | --- |
| `default<O0>` | 172 | 只有 `ee-instrument`、`always-inline`、coro 相关和 `verify` |
| `default<O1>` | 4393 | `loop-vectorize<no-interleave-forced-only;vectorize-forced-only;>`、`loop-unroll<O1>`，没有 `slp-vectorizer` |
| `default<O2>` | 4783 | `loop-vectorize<...;no-vectorize-forced-only;>`、`slp-vectorizer`、`loop-unroll<O2>` |
| `default<O3>` | 4816 | 与 O2 相同的 vectorize 标记，`loop-unroll<O3>` |

`O0` 的完整输出只有一行：

```text
function(ee-instrument<>),always-inline,coro-cond(coro-early,cgscc(coro-split),coro-cleanup,globaldce),alloc-token,lower-comment-string,function(annotation-remarks),verify
```

这组数字说明 `-O0/-O1/-O2/-O3` 不是「线性加 pass」。O1 到 O3 的顶层条目数都是 32（用顶层逗号切分统计），但嵌套的 pipeline 内容不同：O2 打开了 SLP 向量化并取消 `vectorize-forced-only`，O3 又把 `loop-unroll` 的级别改成 `O3`。只看顶层 pass 数量会得出错误结论。

clang 和 opt 共享同一套构造：

- `llvm/lib/Passes/PassRegistry.def:268` 的 `"default"` 别名调用 `buildPerModuleDefaultPipeline(L)`。
- `clang/lib/CodeGen/BackendUtil.cpp:1163` 在 clang 的优化阶段调用 `PB.buildPerModuleDefaultPipeline(Level)`。
- `llvm/lib/Passes/PassBuilderPipelines.cpp:1659` 是 `buildPerModuleDefaultPipeline` 的实现。
- `PassBuilderPipelines.cpp:1000` 构造 simplification pipeline，`:1417` 构造 optimization pipeline。

所以 `opt -passes='default<O2>'` 和 clang 的 `-O2` 在**中端默认 pipeline** 上走同一套代码。差异在 clang 后面还会进入 codegen，并可能插入 target 相关的 IR pass；`opt` 默认停在 IR 层。

## 阶段 4：llc 把 IR 降到 Machine IR

把上面的 `o1.ll` 喂给 `llc`：

```bash
$ $B/llc -O2 -filetype=asm o1.ll -o -
```

实测汇编（完整输出）：

```asm
	.att_syntax
	.file	"min.c"
	.text
	.globl	sum                             # -- Begin function sum
	.prefalign	4, .Lfunc_end0, nop
	.type	sum,@function
sum:                                    # @sum
# %bb.0:                                # %entry
	testl	%esi, %esi
	jle	.LBB0_1
# %bb.3:                                # %for.body.preheader
	movl	%esi, %ecx
	xorl	%eax, %eax
	.p2align	4
.LBB0_4:                                # %for.body
                                        # =>This Inner Loop Header: Depth=1
	addl	(%rdi), %eax
	addq	$4, %rdi
	decq	%rcx
	jne	.LBB0_4
# %bb.2:                                # %for.cond.cleanup
	retq
.LBB0_1:
	xorl	%eax, %eax
	retq
.Lfunc_end0:
	.size	sum, .Lfunc_end0-sum
                                        # -- End function
	.ident	"clang version 24.0.0git (https://gh-proxy.com/https://github.com/llvm/llvm-project.git a5eaf194d8b6b5f4903b67d297e66a5de7c79a5c)"
	.section	".note.GNU-stack","",@progbits
```

`llc` 的入口是 `llvm/tools/llc/llc.cpp:13` 的 `main`，它调用 `llvm/tools/llc/lib/llcdriver.cpp:370` 的 `llcMain`。真正的代码生成调度在：

- `llvm/lib/CodeGen/TargetPassConfig.cpp:692` 的 `addIRPasses`：进入 ISel 前的 IR 变换。
- `TargetPassConfig.cpp:819` 的 `addCodeGenPrepare`：IR 级 codegen 准备。
- `TargetPassConfig.cpp:937` 的 `addISelPasses`：Instruction Selection。
- `TargetPassConfig.cpp:985` 的 `addMachinePasses`：机器 IR 优化、寄存器分配、epilogue 等。
- `llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:109` 的 `addPassesToGenerateCode` 串起 `TargetPassConfig`。
- `CodeGenTargetMachineImpl.cpp:222` 的 `addPassesToEmitFile` 选择输出文件类型。

一个容易踩的边界：`llc` 读的是 LLVM IR，不是 C 源文件。把 `min.c` 直接给它，会得到：

```text
llc: error: min.c:1:1: error: expected top-level entity
int sum(const int *a, int n) {
^
```

这不是 `llc` 的 bug，而是输入层级错了。`clang -cc1 -emit-llvm` 先把 C 变成 IR，才有资格进入 `llc`。

## 阶段 5：MC 层把 Machine IR 变成字节或汇编

`llc -filetype=asm` 走的是汇编文本路径；`clang -c` 走的是目标文件路径。两者共用 MC 的抽象：

- `llvm/include/llvm/MC/MCStreamer.h:1137` 声明 `emitInstruction`，它是 MC 层的指令发射接口。
- `llvm/lib/MC/MCAsmStreamer.cpp:2643` 实现汇编文本输出。
- `llvm/lib/MC/MCObjectStreamer.cpp:402` 实现目标文件路径的指令发射。
- `llvm/lib/MC/MCELFStreamer.cpp:687` 的 `createELFStreamer` 选择 ELF 格式。
- `llvm/lib/MC/MCAssembler.cpp:804` 的 `MCAssembler::Finish` 做布局并写出对象文件。

用 `clang -c` 生成对象文件并检查类型：

```bash
$ $B/clang -c -O2 min.c -o min.o
$ file min.o
min.o: ELF 64-bit LSB relocatable, x86-64, version 1 (SYSV), not stripped
```

用 `clang -S` 则停在汇编文本：

```bash
$ $B/clang -S -O2 min.c -o min.s
$ file min.s
min.s: assembler source, ASCII text
```

`clang -### -c` 显示 `-emit-obj`，说明驱动选择的是目标文件 action；这个构建默认使用集成汇编器，所以 `-###` 里没有单独的 `as` 进程。`-S` 则让 MC 选择汇编文本 streamer，输出 `.s`。

## 阶段 6：lld 只在链接时出现

`lld` 的 ELF 入口在 `lld/ELF/Driver.cpp`：

- `lld::elf::link`（`:131`）是库接口，接受参数、创建 `Ctx`，调用 `ctx.driver.linkerMain`。
- `LinkerDriver::linkerMain`（`:642`）解析链接器参数、处理输入文件。
- `LinkerDriver::link`（`:3271`）是模板化的实际链接流程。

driver 默认不一定用 lld。实测 `clang -### -O2 min.c -o min` 的链接命令是 `/usr/bin/ld`；要显式走 lld，需要：

```bash
$ $B/clang -### -fuse-ld=lld -O2 link.c -o link
```

输出里会出现：

```text
 "/home/ubuntu/llvm/builds/pr-lane/bin/clang-24" "-cc1" ... "-emit-obj" ... "-o" "/tmp/link-....o" "-x" "c" "link.c"
 "/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld" "-z" "relro" "--hash-style=gnu" ... "-o" "link" ... "/tmp/link-....o" ...
```

这里的 `/tmp/link-....o` 是实际临时文件名，用 `....` 代替了每次变化的哈希部分。看到第二条命令，才说明链路进入了 lld。

本环境的边界：这个构建自带的 `ld.lld` 链接失败，报错是共享库符号不匹配：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld: symbol lookup error:
/home/ubuntu/llvm/builds/pr-lane/bin/../lib/liblldELF.so.24.0git:
undefined symbol: _ZN4llvm18StringTableBuilder3addENS_19CachedHashStringRefEh
```

这不是 `lld/ELF/Driver.cpp` 的源码逻辑问题，而是构建产物之间的 ABI 不一致。用系统 `/usr/bin/ld.lld`（Ubuntu LLD 14.0.0）走同一条 driver 路径可以完成链接：

```bash
$ $B/clang --ld-path=/usr/bin/ld.lld -O2 link.c -o link-system
$ ./link-system; echo $?
0
```

所以本章把「构建自带 lld 的完整链接」标为**失败实测**，把「系统 lld 的链接」标为**替代验证**。源码入口和 driver 选择逻辑仍然由 `-###` 与 `lld/ELF/Driver.cpp` 的证据支持；不要因为共享库坏了就推断源码链接算法有错。

## `-emit-llvm`、`-S`、`-c` 各停在哪

| 命令 | 产物 | 停在哪一步 | 是否经过 codegen |
| --- | --- | --- | --- |
| `clang -emit-llvm -S min.c` | `.ll` 文本 IR | 中端优化后、codegen 前 | 否 |
| `clang -emit-llvm -c min.c` | `.bc` bitcode | 中端优化后、codegen 前 | 否 |
| `clang -S min.c` | `.s` 汇编 | MC 汇编文本输出 | 是 |
| `clang -c min.c` | `.o` 目标文件 | MC 目标文件输出 | 是 |
| `clang min.c` | 可执行文件 | lld（或默认链接器）之后 | 是 |

实测类型：

```bash
$ $B/clang -emit-llvm -c -O1 min.c -o min.bc
$ $B/clang -S -O2 min.c -o min.s
$ $B/clang -c -O2 min.c -o min.o
$ file min.bc min.s min.o
min.bc: LLVM IR bitcode
min.s:  assembler source, ASCII text
min.o:  ELF 64-bit LSB relocatable, x86-64, version 1 (SYSV), not stripped
```

`-S` 单独使用时是汇编，和 `-emit-llvm` 组合时才是 IR；这是 driver 层的语义。raw cc1 不接受这种组合，原因在上一节已经实测。

## 踩雷集锦

1. **以为 `clang` 会启动 `opt` 和 `llc`**。`clang -### -c` 只显示一条 cc1 命令；优化和 codegen 都在 cc1 进程内通过库调用完成。`opt`/`llc` 是独立观察工具，不是 clang 的子进程。
2. **把 `-S` 一律当成汇编**。`clang -S -emit-llvm` 产出 IR；`clang -S` 才产出汇编。到 raw `-cc1` 层，`-emit-llvm` 与 `-S` 还是互斥 action，组合会报错。
3. **以为 `-O2` 是 `-O1` 加几个 pass**。O1 到 O3 的顶层条目数都是 32，但嵌套内容不同：O2 打开 SLP 向量化，O3 改变 `loop-unroll` 级别。用「线性叠加」理解会错判 pipeline 行为。
4. **把 `-c` 当成完整编译**。`-c` 停在目标文件，不会调用 lld；要观察链接阶段，得去掉 `-c` 或显式检查 driver 的链接 job。
5. **以为默认链接器就是 lld**。这个构建的 `clang -###` 默认调用 `/usr/bin/ld`；只有 `-fuse-ld=lld` 才出现 `ld.lld`。
6. **把构建自带的 lld 失败当成源码 bug**。本次失败是 `liblldELF.so` 的符号不匹配，属于构建产物问题；源码入口和链接逻辑要分开判断。

## 本章重点整理

- driver 负责 action 选择和 job 生成；cc1 负责参数解析、前端和 AST→IR。
- clang 的优化阶段在进程内调用 `PassBuilder::buildPerModuleDefaultPipeline`，与 `opt` 的 `default<O2>` 共享同一套构造代码。
- `-O0/-O1/-O2/-O3` 不是线性关系；要看 pipeline 文本的嵌套差异，不要只数顶层 pass。
- `llc` 把 IR 降到 Machine IR；`TargetPassConfig` 组织 ISel 和机器 pass；MC 负责汇编文本或目标文件字节。
- lld 只在链接阶段出现；默认链接器不一定是 lld，`-fuse-ld=lld` 才会改变 driver 的选择。
- `-emit-llvm` 停在 IR，`-S` 停在汇编，`-c` 停在目标文件；三者对应的证据分别是 `.ll`、`.s`、`.o`。

## 自我检核

- [ ] 能不看笔记画出 driver → cc1 → IR → 中端 pipeline → Machine IR → MC → lld 的链路，并标出 `-c` 停在哪
- [ ] 能解释为什么 `clang -### -c` 里没有 `opt` 或 `llc`，以及 `opt`/`llc` 在什么时候才有独立进程
- [ ] 能说出 `Driver::BuildCompilation`、`CompilerInvocation::CreateFromArgs`、`CodeGenModule::EmitGlobal` 各自负责哪一段
- [ ] 能解释 `clang` 的 `-O2` 与 `opt -passes='default<O2>'` 为什么共享 pipeline 构造，并指出共享的源码入口
- [ ] 能根据 pipeline 文本里的 `loop-vectorize`、`slp-vectorizer`、`loop-unroll<O...>` 判断 O1/O2/O3 的差异
- [ ] 能说出 `TargetPassConfig::addISelPasses`、`addMachinePasses` 和 MC 的 `emitInstruction` 各在链路哪一段
- [ ] 能解释 `-emit-llvm`、`-S`、`-c` 的产物类型和停止点，并说明 `-S -emit-llvm` 的组合语义
- [ ] 能复现 raw cc1 的 `-emit-llvm`/`-S` 冲突，并给出可工作的替代命令
- [ ] 能说明本环境 lld 失败的边界，以及为什么不能据此判断 `lld/ELF/Driver.cpp` 的逻辑错误

## 延伸阅读

- **[Using the New Pass Manager](https://llvm.org/docs/NewPassManager.html)**
  - **读哪里**：`Overview`、`Inserting Passes into Default Pipelines`、`Invoking opt`
  - **能学到什么**：`default<O2>` 的语义、pipeline 文本的解析方式，以及 clang/opt 共享默认 pipeline 的背景
  - **前提知识**：读过本章的阶段 3；知道 `opt -passes=` 的基本用法
- **[The LLVM Target-Independent Code Generator](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：`The high-level design of the code generator`、`The "MC" Layer`
  - **能学到什么**：ISel、Machine IR、寄存器分配和 MC 的分层关系，补上 `TargetPassConfig` 之外的全局视角
  - **前提知识**：能读 LLVM IR；知道基本块和虚拟寄存器
- **[The ELF, COFF and Wasm Linkers](https://lld.llvm.org/NewLLD.html)**
  - **读哪里**：`The ELF Linker as a Library`、`Design` 下的 `Key Concepts`
  - **能学到什么**：`lld::elf::link` 为什么是库接口、`LinkerDriver` 如何组织输入和符号解析
  - **前提知识**：知道 ELF 目标文件、符号表和重定位的基本概念
- **[Clang Driver Internals](https://clang.llvm.org/docs/DriverInternals.html)**
  - **读哪里**：`Driver Design`、`Phases`、`Actions` 与 `Jobs`
  - **能学到什么**：`BuildActions`/`BuildJobs` 的设计，以及 `-###` 输出为什么长成那样
  - **前提知识**：读过本章的阶段 0；用过 `clang -###`

→ [Ch 3 — 读 LLVM 源码的方法论：类层次、`dyn_cast`、visitor、测试反推](./03-how-to-read-llvm-source.md)
