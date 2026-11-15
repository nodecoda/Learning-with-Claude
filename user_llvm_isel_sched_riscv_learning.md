---
name: llvm-isel-sched-riscv-course-scope
description: LLVM 24 指令选择 × 调度课程 — 30 章 + 4 练习 + 自定义 RISC-V 指令 final；SelectionDAG 主线，GlobalISel 对照，全部实验在本机构建上真跑。
metadata:
  type: project
---

`compilers/llvm_isel_sched_riscv/` 是一门只讲「instruction selection 与 scheduling 的接缝」的 LLVM 后端课程。它不重复 `compiler_backend` 的 RISC-V 全貌、`globalisel_instruction_select` 的 matcher 细节，也不重复 `riscv_backend` 的 MC/ABI；它把 SelectionDAG、`ScheduleDAGSDNodes`、MachineScheduler、sched model 放在同一条函数生命周期上，回答「这条指令是谁选的、这个顺序是谁排的」。

**Why:** 用户在 CPU 编译器岗位面试中反复被问调度和指令选择，想把两者打通。课程按 RISC-V 为主线（load/store、无 indexed addressing、12-bit immediate、Zba/Zbb/Zicond 扩展），用同一段 IR 在 `generic`、`rocket-rv64`、`sifive-u74` 上做可复现对比；GlobalISel 作为对照，不展开 matcher table。

**How to apply:**
- 文件结构：README + Ch 0–29（30 章）+ 练习 A–D + Final，共 36 篇。
- Part 0（Ch 0–3 + 练习 A）：环境基线、两条调度主线、三种 selector、RISC-V ISA 约束。
- Part 1（Ch 4–10 + 练习 B）：SelectionDAG 数据结构、combine/legalize、pattern matching、多指令 pattern、`ScheduleDAGSDNodes`、调度边界、O0/FastISel。
- Part 2（Ch 11–17 + 练习 C）：MachineScheduler、sched model 三层、RISC-V 模型走读、`llvm-mca`、调度策略、RA 拉扯、post-RA。
- Part 3（Ch 18–24 + 练习 D）：selection × scheduling 交互面（`select`/`setcc`/`Zicond`/`Zba`、memory folding、pseudo、if-conversion、软件流水、RVV、诊断树）。
- Part 4（Ch 25–29 + Final）：TableGen 质量、MIR/lit、性能归因、上游 review、设计空间；final 给 RISC-V 加一条带 sched model 的自定义指令。
- 刻意涵盖：可跑 `llc`/`llvm-mca` 实验、`-print-after-isel`、`-print-before/after=machine-scheduler`、`-debug-only=machine-scheduler`、`-global-isel`、`-O0` 对照。
- 刻意不涵盖：LLVM 完整构建教学、TableGen 源码内部、GlobalISel matcher table、RVV 全量、链接器细节。
- 环境：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，源码 commit `700e551714cc`（2026-11-10）。
- 教学信条：先直觉后形式化；每个「谁做的」都给命令和证据；版本特定行为标注；不把 mca 模型周期当硬件实测；不伪造构建/测试输出。

当前状态：Part 0（Ch 0–3 + 练习 A）+ Part 1（Ch 4–10 + 练习 B）+ Part 2（Ch 11–17 + 练习 C）已完成（2026-11-15）；Part 3 已完成（2026-11-15）；Part 4（Ch 25–29 + Final）已完成（2026-11-16），全课完成。Part 0 的关键实测：同一段 `min.ll` 在 `generic`/`rocket-rv64`/`sifive-u74` 下指令种类相同、顺序不同；`sifive-u74` 把 `add` 提前；`llvm-mca` 同一段汇编在三个模型上为 1201/1001/901 周期。Part 1 的关键实测：`-enable-misched=false` 下 `source` 保持 `L1 M1 L2 M2 A0 A1 A2`，`list-burr` 变成 `A0 L1 M1 A1 L2 M2 A2`；MachineScheduler 开启时所有 `-pre-RA-sched` 最终顺序收敛；`vliw-td` 在 RISC-V 上断言失败。

Part 2 的关键实测（2026-11-15）：`-mcpu=generic` 在 RV64 映射 `generic-rv64` → `SpacemitX60Model`；`-mtune` 覆盖 sched model（`MCSubtargetInfo.cpp:245`），`-mtune=generic` 仍是 `generic-rv64`，真正的 `NoSchedModel` 用 `-mcpu=andes-ax25`（mca 报 `unable to resolve scheduling class`）。同一段 `min.ll` 的 `LD→MUL` 边延迟：rocket 2 / sifive-u74 3 / generic 4 / xiangshan 5；XiangShan 的 `LD→ADD` 是 4（`XS2LoadToALUBypass` 减 1）。`ldmul.ll`（40 load + 40 mul）只改 pre-RA 方向：topdown 0 条栈访问、704 周期；bidirectional 60/1424；bottomup 64/1604；关 misched + source 84/2014。post-RA 只在 `sifive-u74` 默认运行（`TunePostRAScheduler`），默认方向 TopDown；开 1424 / 关 1514，栈访问都是 60。`cbo.zero` 的 `Sched<[]>` 使 mca 报 `unable to resolve scheduling class for write variant`，加 `-skip-unsupported-instructions=lack-sched` 会把指令整条丢掉。
Part 3 的关键实测（2026-11-15）：`select` 在基线下是 `BNE+PHI`（3 个基本块），`+zicond` 后是 `czero.nez+czero.eqz+or`（1 个块）；`select c,a,0` 基线就用 `neg+and`。`sh3` 在 `+zba` 下从 `slli+add` 变 `sh3add`；同一依赖链 100 iter 的 mca 周期：rocket 601（`sh3add` lack-sched）、sifive-u74 504→503、xiangshan 404→403、spacemit-x60 702→801（`sh3add` 延迟 2，`add`/`slli` 延迟 1）。内存折叠边界：off 2047 一条 `ld`，2048 变 `addi 2047; ld 1`，4095 变 `lui 1; add; ld -1`，-4096 变两条 `addi -2048`。大栈帧（600×i64）需要 `lui+addi+add` 才拼出 `sp+4792`。`PseudoMovImm 3968` 在 pre-RA 调度里是 1 条、post-RA 展开为 `ADDI 31; SLLI 7`；`PseudoCALL` 到汇编才变 `call`。常量 3968 在 rocket/u74/x60 是 `li 31; slli 7`，在 sifive-p450（`TuneLUIADDIFusion`）是 `lui 1; addi -128`。`-riscv-sched-mispredict-penalty=10000` 时 `SelectOptimize` 保留 `select`，=0 时转成 `br+phi`（sifive-p550）。MachineCombiner 在 `ldmul.ll` 上把栈帧从 80 推到 240。`loop2.ll` 软件流水：rocket `II=9, MaxStageCount=0`（不变），sifive-u74 `II=6, MaxStageCount=1`（prologue/epilogue）。RVV `<4 x i64>` 生成 `vsetivli 4,e64,m1`；`<8 x i64>` 在 min=256 是 `m2`、min=512 是 `m1`；混合 e32/e64 的 `vmix` 有 6 条 `vsetivli`；向量延迟 P670 `vle64=8/vadd=2`，X60 `vle64=3/vadd=4`。

Part 4 的关键实测（2026-11-16）：`Sched<[]>` 全树 135 处（InstrInfo 36、XCV 51、XSf 17、XSpacemiT 10、Zicbo 7）；新增 `WriteCBO` 只改 SiFive7 时，TableGen 在 `RISCVSchedAndes45.td:226` 报 `Processor does not define resources for WriteCBO`，补完写资源还会报读资源。`CBO_ZERO` 临时补 `Sched<[WriteIALU,ReadIALU]>` 后：u74 1 uOp/3 latency/0.50 RThroughput/MayStore，rocket 1/1/1.00。MIR 注释的真实规则是**分区**：文件头与 YAML 元数据区用 `#`，`body: |` 内用 `;`；body 内写 `# CHECK` 报 `unexpected character '#'`，文件头写 `;` 报 YAML `missing required key 'name'`；`not llc ... 2>&1 | FileCheck` 会把报错回显的 `# CHECK` 源码行当成匹配而假 PASS（`pre-pr-check.sh` 第 7 项专扫此坑）。`-save-stats=obj` 在输出旁写 `<output>.stats` JSON（如 `sel.stats`）；`-pass-remarks-output` 产出 `StackSize`/`InstructionMix`（按基本块数 opcode）/`InstructionCount`/`EnabledFeatures`；`-debug-only=machine-scheduler` 显示 `RegionInstrs` 与 `ShouldTrackPressure`；in-order CPU 上 mca 的 `-bottleneck-analysis` 明确 warning 不支持，但仍给出依赖/资源分解（本机循环：依赖 76.65%、资源 7.68%）。归因实测：`machine-combiner` 把 `(m0+m1)+m2+d` 重结合成 `(m0+m1)+(m2+d)`（`%16`）；pre-RA topdown/bottomup 的 MIR 顺序不同，但 `one.ll`/`big.ll` 最终汇编逐字节相同。`sh3add` 单条 mca 4 周期 vs `slli+add` 5 周期（RThroughput 都是 1.0）。GlobalISel：`sel0` 在 SDAG 是 `czero.eqz`，GISel 退回 `bnez+li`；`mem3.ll` 两者逐字节相同；`rvv.ll` 在 abort=2 时逐函数回退（`fallback path for vadd/vmul`），abort=1 直接 `unable to legalize %5:_(<4 x s64>) = G_ADD`；`-global-isel-abort` 0/1/2 = 静默回退/abort/回退+诊断。Final 的自定义指令 `xdemo.sadd`（`rd = rs1 + (rs2 << uimm2)`，CUSTOM-0，编码 `0b 85 c5 c0` 对 `a0,a1,a2,3`）：patch 19 文件 44 行插入（15 个模型各 2 行占位）；u74 mca latency 2 / PipeA / 单条 Total Cycles 3，rocket latency 1 / RocketUnitALU / 2；两条依赖的 timeline 宽度 5 vs 3；选择测试与 mca 测试 `llvm-lit` 2/2 PASS；验证后已 `git checkout` 还原并重建，`-mattr=help` 不再含 xdemo。

