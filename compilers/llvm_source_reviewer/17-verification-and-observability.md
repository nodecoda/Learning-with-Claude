# Ch 17 — 验证与观测：`Verifier`、`Debugify`、`-print-after`、statistics

> **目标**：把「这段 IR 合法吗」「这个 pass 改了什么」「为什么改」拆成三个分别可回答的问题，并知道每种观测手段的证据强度与盲区。学完你能在 review 里写清楚「用哪条命令、看到什么、因此判断哪里会坏」。
>
> **环境**：LLVM 24.0.0git，源码 `main` @ `7c7a9072f736`（2026-11-20）；断言构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo`，assertions ON，targets `X86;AArch64;RISCV`）。对照的 release 构建是 Ubuntu 的 `opt-15`（LLVM 15.0.7，`Optimized build`）。除标注「未实测」的段落外，本章输出都在前者上真实运行。

## 为什么需要这个

Review 里最常见的空话是「这个改动应该没问题」。它听起来像判断，其实没有证据。LLVM 提供三类证据通道：验证器回答「结构合法吗」；打印通道回答「这一轮 IR 长什么样」；统计、计时、remark、debug 日志回答「改了多少、为什么、花了多久」。混用会产生两种典型误判：拿 `-print-after` 的空输出证明「pass 没跑」，拿 `-stats` 的空输出证明「没有优化发生」。

## 先建立直觉

```
            输入 IR
               │
        [输入 Verifier]         ← 默认开；-disable-verify 可关
               │
        ┌──────┴───────┐
        │  Pass A      │
        │  Pass B      │─── -print-before/-print-after：每站产物
        │  Pass C      │─── -print-changed：只看变了的站
        └──────┬───────┘
               │  每个 pass 之后
        [Per-pass Verifier]     ← 只有 -verify-each 才装
               │
        [输出 Verifier]         ← 默认开
               │
            输出 IR

  旁路：-debug-pass-manager（调度）/ -stats、-time-passes（规模与成本）
        -pass-remarks*（pass 主动报告的决策）/ -debug-only（LLVM_DEBUG，断言构建）
```

关键点：**验证器只回答「合法」，不回答「正确」**。合法 IR 可以包含 UB、可以算出错误结果、也可以让某个 target 崩掉。Ch 9 讲判据本身，本章讲「什么时候、以什么代价运行它」。

## 三道检查的边界

| 检查 | 入口 | 检查对象 | 何时运行 | 不检查 |
| --- | --- | --- | --- | --- |
| IR `Verifier` | `llvm/lib/IR/Verifier.cpp` | 模块/函数结构不变量 | 默认查输入与输出 | UB、poison 滥用、语义等价、target 约束 |
| per-pass `Verifier` | `StandardInstrumentations.cpp` | 同上，按 pass 归因 | 仅 `-verify-each` | 同上 |
| `MachineVerifier` | `llvm/lib/CodeGen/MachineVerifier.cpp` | 虚拟寄存器、live range、CFG、tied operand | 默认 asm 输出前一次；`-verify-machineinstrs` 改成每个 machine pass 后 | IR 合法性、分配质量、编码正确性 |

`opt` 的默认行为在 `llvm/tools/opt/optdriver.cpp:812-822`：`VerifierKind` 起初是 `InputOutput`，`-disable-verify` 改成 `None`，`-verify-each` 改成 `EachPass`。分支顺序决定：**`-disable-verify` 优先于 `-verify-each`**，两个都给，验证器全关。

`VerifierPass::run(Module &, ...)` 在 `llvm/lib/IR/Verifier.cpp:8549`，函数版在 `:8557`，逐条检查在 `Verifier::visit(Instruction &)`（`:565`）。Ch 0 见过的 `Instruction does not dominate all uses!` 来自 `:5637` 的 `Check(...)`。

## `-verify-each`：每个 pass 之后的哨兵

只加 `-verify-each` 时屏幕上不新增内容。同时打开 pass manager 日志才看得见：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin; mkdir -p ~/llvm-lab/ch17 && cd ~/llvm-lab/ch17
cat > add.ll <<'IR'
define i32 @f(i32 %x) {
  %y = add i32 %x, 0
  ret i32 %y
}
IR
$B/opt -passes='instcombine' -debug-pass-manager -verify-each -disable-output add.ll
# 实测（节选，省略 analysis 行）：
Running pass: InstCombinePass on f (2 instructions)
Verifying function f
Running pass: VerifierPass on [module]
```

去掉 `-verify-each` 重跑，`Verifying function f` 消失，只剩最后的模块级 `VerifierPass`。这两行差异就是证据：`-verify-each` 装的是 `VerifyInstrumentation`（注册点 `llvm/lib/Passes/StandardInstrumentations.cpp:2510-2511`，回调在 `:1427`），每个 pass 结束后按 IR unit 调 `verifyFunction` / `verifyModule`，失败时用 `report_fatal_error` 报出 pass 名（`:1444`）：

```text
Broken function found after pass "<PassName>", compilation aborted!
```

> 这行失败文本**未实测**：触发它需要一个真的产出非法 IR 的 pass。字符串来自源码；练习 D 破坏的是 analysis 缓存，不是 IR 合法性，触发不了。`VerifyInstrumentation` 还会跳过 `VerifierPass` 自身（回调开头的 `P == "VerifierPass"`）。

## Verifier 检查什么、不检查什么

```bash
cat > ub.ll <<'IR'
define i32 @ub() {
  %r = sdiv i32 1, 0
  ret i32 %r
}
IR
$B/opt -passes=verify -disable-output ub.ll; echo "EXIT=$?"
$B/opt -passes=instcombine -S ub.ll -o -
# 实测：
EXIT=0
define i32 @ub() {
  ret i32 poison
}
```

除以零是 UB，但结构合法，验证器放行，InstCombine 把它折成 poison。**Verifier 通过不等于程序有意义**：它保证后续 pass 可以安全假设支配关系、类型、属性成立，不保证真实机器上的行为。poison 与 UB 见 Ch 30。

反过来，结构错误在输入检查就抓住，不给 `-verify-each` 也一样：

```bash
# bad-dom.ll 就是 Ch 0 里那段支配关系错误的 IR
$B/opt -passes=instcombine -disable-output bad-dom.ll
# 实测（退出码 1）：
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
.../bin/opt: bad-dom.ll: error: input module is broken!
```

## 静默通过的坏例子：`-disable-verify` 压过 `-verify-each`

```bash
$B/opt -passes=instcombine -verify-each -disable-verify -disable-output bad-dom.ll; echo "EXIT=$?"
# 实测：
EXIT=0
```

没有输出，退出码 0。`NoVerify` 命中后直接设 `VerifierKind::None`，`-verify-each` 没机会生效。为了加速关验证、后来又加 `-verify-each` 想抓问题，两个开关互相抵消，**证据强度归零，但命令看起来像开了检查**。

## `MachineVerifier`：换了一层 IR

`llc` 的开关是 `-verify-machineinstrs`，链路是 `CodeGenOptions.td:680` → `TargetPassConfig.cpp:322-327` 的 `computeMachineVerifierMode`：给开关返回 `Each`，否则 `End`（除非 `disable_mir_output_verify`）。`Each` 让 `TargetPassConfig::addVerifyPass`（`:654-658`）在每个 `printAndVerify` 点插入 `createMachineVerifierPass`。

```bash
cat > m.ll <<'IR'
define i32 @f(i32 %x) {
  %y = add i32 %x, 1
  ret i32 %y
}
IR
$B/llc -mtriple=x86_64-unknown-linux-gnu -verify-machineinstrs -o /dev/null m.ll; echo "EXIT=$?"
# 实测：
EXIT=0
```

再造一个机器层错误：导出 MIR，让一条指令读只声明、没定义的虚拟寄存器。

```bash
$B/llc -mtriple=x86_64-unknown-linux-gnu -stop-after=finalize-isel -o mir0.mir m.ll
sed -i "/id: 1, class: gr32/a\  - { id: 2, class: gr32, preferred-register: \"\", flags: [  ] }" mir0.mir
sed -i 's/INC32r %0/INC32r %2/' mir0.mir
$B/llc -mtriple=x86_64-unknown-linux-gnu -verify-machineinstrs -o /dev/null mir0.mir
# 实测（前面是 MIR dump；0x... 是对象地址，每次不同）：
*** Bad machine code: Reading virtual register without a def ***
- function:    f
- basic block: %bb.0  (0x559759a32fe0)
- instruction: %1:gr32 = INC32r %2:gr32(tied-def 0), implicit-def dead $eflags
- operand 1:   %2:gr32(tied-def 0)
LLVM ERROR: Found 1 machine code errors.
```

两个细节：错误里的 `tied-def`、`implicit-def dead $eflags` 是 IR 层没有的概念，`MachineVerifierPass::run` 在 `MachineVerifier.cpp:404`；这段 MIR 不加 flag 也报错，因为解析完会直接调 `MF.verify(...)`（`MIRParser.cpp:697`）。`-verify-machineinstrs` 增加的是 **pass 之间**的检查，不是「有没有检查」。同理，`llc` 输出汇编时默认在末尾插一次检查（`CodeGenPassBuilder.cpp:279`），NVPTX / AMDGPU 会关掉这个默认（`TargetOptions.h:110` 默认 `true`）。「不加 flag 就没有机器验证」取决于 target 与输出类型。

## `Debugify`：把「调试信息有没有掉」变成可计数的事

```bash
$B/opt --print-passes | rg -i debugify
# 实测：
check-debugify
debugify
mir-debugify
mir-check-debugify
```

`debugify` 给每条指令造合成 `!dbg` 位置（第 1 行、第 2 行……），给每个有结果的指令造 `#dbg_value`，并写 `!llvm.debugify = !{行数, 变量数}` 作为账本：

```bash
cat > dbg.ll <<'IR'
define i32 @f(i32 %x) {
entry:
  %a = add i32 %x, 1
  %b = mul i32 %a, 2
  ret i32 %b
}
IR
$B/opt -passes=debugify -S dbg.ll -o -
# 实测（节选）：
define i32 @f(i32 %x) !dbg !5 {
entry:
  %a = add i32 %x, 1, !dbg !12
    #dbg_value(i32 %a, !9, !DIExpression(), !12)
  %b = mul i32 %a, 2, !dbg !13
    #dbg_value(i32 %b, !11, !DIExpression(), !13)
  ret i32 %b, !dbg !14
}
!llvm.debugify = !{!2, !3}
!2 = !{i32 3}
!3 = !{i32 2}
!9 = !DILocalVariable(name: "1", scope: !5, file: !1, line: 1, type: !10)
!10 = !DIBasicType(name: "ty32", size: 32, encoding: DW_ATE_unsigned)
```

`check-debugify` 拿着账本检查三件事（`llvm/lib/Transforms/Utils/Debugify.cpp:812-860`）：每个原始行号是否还出现在某条指令的 `DebugLoc` 里；每个变量是否还有 `#dbg_value`；`dbg.value` 的值宽度是否小于变量宽度（`diagnoseMisSizedDbgValue`，`:745`）。只有第三项会把结果变成 `FAIL`。

```bash
$B/opt -passes='debugify,instcombine,check-debugify' -disable-output dbg.ll
# 实测：
WARNING: Missing line 1
CheckModuleDebugify: PASS
$B/opt -passes='debugify,instcombine' -S dbg.ll -o -
# 实测：
    #dbg_value(i32 %x, !9, !DIExpression(DW_OP_plus_uconst, 1, DW_OP_stack_value), !12)
  %a = shl i32 %x, 1, !dbg !13
  %b = add i32 %a, 2, !dbg !13
  ret i32 %b, !dbg !14
```

`add (x,1)` 被分配律重写成 `shl` 与 `add`，都挂第 2 行；第 1 行没有指令了，所以报 missing line。变量 1 通过 `DW_OP_plus_uconst, 1` 救回来，`PASS`。**check 的粒度是「覆盖」不是「语义」**：没有检查保证这条表达式等于原 `%a` 在该点的值。

真的 `FAIL` 长这样。把变量类型改成 64 位有符号、值仍是 `i32`：

```bash
$B/opt -passes=debugify -S dbg.ll -o dbgif.ll
sed -i 's/name: "ty32", size: 32/name: "ty64", size: 64/; s/DW_ATE_unsigned/DW_ATE_signed/' dbgif.ll
$B/opt -passes=check-debugify -disable-output dbgif.ll; echo "EXIT=$?"
# 实测（节选）：
ERROR: dbg.value operand has size 32, but its variable has size 64: #dbg_value(i32 %a, !25, ...)
ERROR: dbg.value operand has size 32, but its variable has size 64: #dbg_value(i32 %b, !27, ...)
CheckModuleDebugify: FAIL
EXIT=0
```

两个坑：**退出码是 0**，`FAIL` 不让进程失败，lit 必须用 `FileCheck` 锁文本；同一份错配若 `encoding` 保持 `DW_ATE_unsigned`，结果仍是 `PASS`——源码 `:767-774` 只在有符号整数上做 `<` 比较，无符号整数直接 `HasBadSize = false`，非整数类型才做 `!=`。

| 检查项 | 会检查 | 不会检查 |
| --- | --- | --- |
| 行号 | 原始行号是否还出现在某条指令上 | 是否对应真实源码、是否指对语句 |
| 变量 | 每个变量是否还有 `#dbg_value` | 值是否语义正确、位置是否在同一条路径 |
| 宽度 | 有符号整数偏小、非整数类型不等宽 | 无符号整数的任何宽度错配 |
| 结果 | 打印 `PASS` / `FAIL` | 不设退出码，`FAIL` 也返回 0 |

`-debugify-each` 把同一套检查挂到每个 pass 前后：实测 `CheckFunctionDebugify [InstCombinePass]: PASS`，前面仍可能带 `WARNING: Missing line 1`。

## 打印通道：`-print-before` / `-print-after` / `-print-changed`

开关定义在 `llvm/lib/IR/PrintPasses.cpp`：`-print-before` 在 `:35`，`-print-after` 在 `:40`，`-print-changed` 在 `:76`，`-filter-print-funcs` 在 `:121`。

```bash
$B/opt -passes='instcombine' -print-after=instcombine -S add.ll -o /dev/null
# 实测：
; *** IR Dump After InstCombinePass on f ***
define i32 @f(i32 %x) {
  ret i32 %x
}
$B/opt -passes='instcombine' -print-changed -disable-output add.ll
# 实测（节选）：
define i32 @f(i32 %x) {
  %y = add i32 %x, 0
  ret i32 %y
}
*** IR Dump After InstCombinePass on f ***
define i32 @f(i32 %x) {
  ret i32 %x
}
```

`-print-before` 的 banner 是 `; *** IR Dump Before InstCombinePass on f ***`，内容就是未优化的 `%y = add i32 %x, 0`。`ignored` 表示这一站没改（节选省略了这类行）；`filtered out` 表示有变化但被过滤。多函数模块用 `-filter-print-funcs` 只盯一个函数：

```bash
cat > two.ll <<'IR'
define i32 @keep(i32 %x) {
  %a = add i32 %x, 0
  ret i32 %a
}
define i32 @other(i32 %x) {
  %b = add i32 %x, 0
  ret i32 %b
}
IR
$B/opt -passes='instcombine' -print-changed -filter-print-funcs=keep -disable-output two.ll
# 实测（节选）：
*** IR Dump After InstCombinePass on keep ***
define i32 @keep(i32 %x) {
  ret i32 %x
}
*** IR Dump After InstCombinePass on other filtered out ***
```

`filtered out`（`StandardInstrumentations.cpp:428` 的 `handleFiltered`）不是错误，是「这里有变化，但按过滤条件不打印内容」。

**静默陷阱**：pass 名字打错不报错，只给一份普通 `-S` 输出。实测 `-print-after=instcombineXX` 没有任何 banner、退出码 0，输出就是优化后的完整模块。看到「有 IR 输出但没 banner」，先查名字；名字用 `--print-passes` 核实。

## `-debug-pass-manager`：谁在跑、跑在哪一层

定义在 `llvm/tools/opt/NewPMDriver.cpp:76`，不打印 IR，只打印调度。实测节选：

```text
Running pass: InstCombinePass on f (2 instructions)
Running analysis: AssumptionAnalysis on f
Running analysis: DominatorTreeAnalysis on f
Running pass: VerifierPass on [module]
```

它回答三件事：pass 跑在 `Function` 还是 `Module` 层（`on f` / `on [module]`）；拉起了哪些 analysis（Ch 16 里 `PreservedAnalyses` 要处理的缓存）；有没有 adaptor 包一层。`(2 instructions)` 是进入前数出的规模。

## `-stats` 与 `STATISTIC`

`STATISTIC` 宏在 `llvm/include/llvm/ADT/Statistic.h:168-174`，按 `DEBUG_TYPE` 分组；开关在同文件 `:37-42`：

```cpp
#if !defined(NDEBUG) || LLVM_FORCE_ENABLE_STATS
#define LLVM_ENABLE_STATS 1
#else
#define LLVM_ENABLE_STATS 0
#endif
```

**统计是否可用由构建决定，不由 `-stats` 决定**。`LLVM_ENABLE_STATS=0` 时 `Statistic` 是 `NoopStatistic`，`PrintStatistics()` 直接返回（`llvm/lib/Support/Statistic.cpp:229-243`）。InstCombine 的计数器在 `InstructionCombining.cpp:119-133`：

```bash
$B/opt -passes='instcombine' -stats -disable-output add.ll
# 实测：
===-------------------------------------------------------------------------===
                          ... Statistics Collected ...
===-------------------------------------------------------------------------===

1 instcombine       - Number of insts combined
1 instcombine       - Number of functions with two iterations
2 instcombine       - Number of instruction combining iterations performed
1 last-run-tracking - Number of LastRunTracking queries
```

`insts combined` 是真正发生的替换次数（这里 1 次，`add x,0` 被折掉）；`iterations performed` 是 worklist 轮数，这里是 2 而不是 1，因为 `opt -passes=instcombine` 默认带 `verify-fixpoint`，第一轮改完后还要再跑一轮确认收敛（Ch 18 展开）。`-stats` 是**进程级累计**，不按函数、不按调用点分组；数字大时先数 pipeline 里这个 pass 出现了几次。

## `-time-passes`

```bash
$B/opt -passes='instcombine' -time-passes -disable-output add.ll
# 实测（节选）：
===-------------------------------------------------------------------------===
                        Analysis execution timing report
===-------------------------------------------------------------------------===
   ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
   0.0000 ( 14.3%)   0.0000 ( 16.7%)   0.0000 ( 14.9%)   0.0000 ( 14.6%)  TargetLibraryAnalysis
```

报告分「analysis 执行」与「pass 执行」两段。`0.0000` 只表示低于计时器分辨率，不表示零成本。比较两版实现的编译时间时，输入、构建、cache 状态必须相同，否则数字不可比。

## Optimization remarks

`-pass-remarks` 系列在 `llvm/lib/IR/DiagnosticHandler.cpp:46-66`，分「做了」（passed）、「没做」（missed）、「分析结论」（analysis），参数是匹配 pass 名的正则。remark 不是自动的：pass 必须构造 `OptimizationRemark` 并交给 `OptimizationRemarkEmitter`。

```bash
cat > inl.ll <<'IR'
define internal i32 @callee(i32 %x) {
  %y = add i32 %x, 1
  ret i32 %y
}
define i32 @caller(i32 %x) {
  %r = call i32 @callee(i32 %x)
  ret i32 %r
}
IR
$B/opt -passes='cgscc(inline)' -pass-remarks=inline -disable-output inl.ll
# 实测：
remark: <unknown>:0:0: 'callee' inlined into 'caller' with (cost=-15030, threshold=337)
```

missed remark 用 `-pass-remarks-missed`。对一个 `n` 未知的循环跑 `loop-vectorize`，本构建的实测输出是 `remark: <unknown>:0:0: vectorization is not possible`；这段输入没有放进本章（按「`%i` 从 0 递增、逐元素 `store`、`icmp slt %i.next, %n`」即可复现）。

`<unknown>:0:0` 是因为输入 `.ll` 没有 `!dbg`。引用 remark 时建议加 `-pass-remarks-output=remarks.yaml`，把 pass 名、函数名、原因一起留下。

盲区：**没有 remark 不等于没有决策**。InstCombine 在本构建里取了 `OptimizationRemarkEmitter`（`InstructionCombining.cpp:6338`）却没有任何 `emit` 调用：

```bash
$B/opt -passes=instcombine -pass-remarks=instcombine -disable-output add.ll
$B/opt -passes=instcombine -pass-remarks-missed=instcombine -disable-output add.ll
# 实测：两条都没有输出
```

但 `-stats` 记了 1 次 combine。**remark 是 pass 作者选择暴露的视角，不是运行时的事实记录。**

## 观测手段回答什么问题

| 手段 | 回答什么问题 | 不能回答什么 | 前置条件 |
| --- | --- | --- | --- |
| `-passes=verify` | IR 结构合法吗 | 有没有 UB、结果对不对 | 无 |
| `-verify-each` | 哪个 pass 之后 IR 变非法 | 哪个 pass 语义错了 | 需要能产出非法 IR 的 pass |
| `llc -verify-machineinstrs` | 哪个 machine pass 之后不变量坏了 | IR 层是否合法 | 只对 codegen 生效 |
| `-print-before/-after` | 这一站前后的 IR 文本 | 为什么改、改了几次 | 准确的 pass 名 |
| `-print-changed` | 哪些站真的改了 IR | 改动的语义合法性 | 无 |
| `-filter-print-funcs` | 某个函数的打印 | 模块级 pass 的全貌 | 配合打印选项 |
| `-debug-pass-manager` | pass 与 analysis 的调度顺序 | pass 内部做了什么 | 无 |
| `-stats` | 计数器累计值 | 单函数、单次调用的分布 | 断言构建或 `LLVM_FORCE_ENABLE_STATS` |
| `-time-passes` | analysis / pass 的耗时分布 | 大规模输入下的稳定性 | 输入与构建一致才可比 |
| `-pass-remarks*` | pass 主动报告的决策 | pass 没报告的决策 | pass 必须实现 ORE |
| `debugify` / `check-debugify` | 调试信息覆盖与宽度 | 调试值的语义正确性 | 需要 `llvm.debugify` 账本 |

## 踩雷集锦

1. **用 release 构建跑 `-debug-only`，以为 pass 没有日志。**
   错误直觉：「没输出就说明这个 pass 没有 debug 信息。」
   正确认识：release 构建里 `LLVM_DEBUG` 被编译掉，选项本身都不存在。实测（节选）：

   ```text
   $ /usr/bin/opt-15 -passes=instcombine -debug-only=instcombine -disable-output add.ll
   opt-15: Unknown command line argument '-debug-only=instcombine'.  Try: '/usr/bin/opt-15 --help'
   EXIT=1
   ```

2. **用 release 构建跑 `-stats`，把空输出当成「没有优化」。**
   同一条命令在 `opt-15` 上实测 0 字节输出、退出码 0；断言构建上才有一整段计数。先查 `llvm-config --assertion-mode`，或在 `--help-hidden` 里确认 `-stats` 的说明带 `(available with Asserts)`。

3. **同时给 `-disable-verify` 与 `-verify-each`。**
   错误直觉：「两个都写，检查更严格。」
   正确认识：`optdriver.cpp:820-822` 的顺序让 `-disable-verify` 短路成 `VerifierKind::None`；实测坏 IR 也以退出码 0 通过。

4. **把 `check-debugify` 的 `FAIL` 当成进程失败。**
   实测 `FAIL` 之后 `EXIT=0`。要 FileCheck 锁文本，或在外层脚本里 `grep -q 'CheckModuleDebugify: FAIL'`。

## 动手练习

1. 用 `-debug-pass-manager` 跑 `default<O2>`，数 `InstCombinePass` 出现几次，再和 `-stats` 的 `iterations performed` 对照，解释两者为什么不是同一个量。
2. 在 `-passes='debugify,instcombine,check-debugify'` 中间插入 `strip-debug`，观察 `WARNING` 与 `PASS` 的组合，解释为什么「调试信息全没了」仍然可能 `PASS`。
3. 给 `bad-dom.ll` 分别加 `-verify-each`、`-verify-each -disable-verify`、`-print-changed`，记录三组输出。哪一组能区分「坏在输入」与「坏在某个 pass」？

## 本章重点整理

- 验证器回答「合法」，不回答「正确」；`sdiv 1, 0` 合法，但它是 UB。
- `-verify-each` 把错误归因到具体 pass；没有它，只有输入与输出两次检查。
- `-disable-verify` 压过 `-verify-each`，组合使用时检查整段消失。

## 自我检核

- [ ] 不看笔记，说出 `Verifier`、`MachineVerifier`、`debugify` 各自检查的对象与盲区。
- [ ] 解释为什么 `-verify-each` 单独使用时屏幕上没有额外输出，以及怎样证明它真的在跑。
- [ ] 给出一个「合法但有 UB」的 IR 片段，说明为什么 `-passes=verify` 不报错。
- [ ] 解释 `-disable-verify` 与 `-verify-each` 同时出现时发生了什么，依据是哪几行源码。
- [ ] 说出 `check-debugify` 的三种检查里哪一种导致 `FAIL`，以及无符号变量的宽度错配为什么抓不到。
- [ ] 说明 `-print-changed` 输出里 `ignored` 与 `filtered out` 的区别。
- [ ] 解释 `-stats` 为空的两个可能原因，以及各自的验证办法。
- [ ] 同事说「跑了 `-print-after=instcombine` 但没看到 dump」，给出三个排查方向。

## 延伸阅读

- **[LLVM Command Guide: opt](https://llvm.org/docs/CommandGuide/opt.html)**
  - **读什么**：`-passes`、`-print-after`、`-print-changed`、`-verify-each`、`-stats`、`-time-passes` 各节
  - **能学到什么**：本章每个开关的官方语义与默认值；`-print-changed` 的 `diff` / `quiet` 变体有完整列表
  - **前提**：读过 Ch 13 的 pipeline 文本语法，知道 `function(...)` 与 `cgscc(...)` 的区别
- **[HowToUpdateDebugInfo — The `debugify` utility pass](https://llvm.org/docs/HowToUpdateDebugInfo.html#irdebugify)**
  - **读什么**：`IRDebugify` 一节，特别是 `debugify` / `check-debugify` 示例与 `!llvm.debugify` 元数据
  - **能学到什么**：`-debugify-each`、`-debugify-export` 统计格式，以及上游期望 pass 作者怎样自证调试信息保留
  - **前提**：知道 `DILocation`、`DILocalVariable`、`DIExpression` 的基本结构（Ch 8）
- **[LLVM Remarks](https://llvm.org/docs/Remarks.html)**
  - **读什么**：`Enabling optimization remarks` 与 `YAML remarks` 两节
  - **能学到什么**：`-pass-remarks-output` 的 YAML 结构、三类 remark 的序列化方式，便于把 remark 变成可 diff 的证据
  - **前提**：读过本章 remarks 小节，知道 ORE 不是自动的
- **[LLVM Language Reference — Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读什么**：`Poison Values` 整节，尤其 `select` 只传播被选中操作数的 poison
  - **能学到什么**：为什么验证器放行 `sdiv 1, 0`，以及 Ch 18 / Ch 30 里那些 fold 的合法性边界
  - **前提**：能区分 `undef` 与 `poison`

→ [Ch 18 — `InstCombine` 与 `InstSimplify`：canonicalization、worklist、fixpoint](./18-instcombine-and-instsimplify.md)
