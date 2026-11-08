# Ch 31 — 高价值贡献地图：按 churn 与缺口选题

> **目标**：把“我想给 RISC-V 上游做点事”变成一份可执行的选题清单。学完你能用 10 分钟判断一个方向值不值得做：它热不热、测试重不重、缺口是否可复现、reviewer 会卡在哪、最后能不能落成一个几行源码加一组 lit 测试的 PR。

> **环境**：`TR=/home/ubuntu/llvm/trees/ct-instr-trunk`（分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`，完整历史 607,142 个提交），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。本章对 `TR` 只读；所有数字都在 2026-11-08 重跑过。

> **前置**：读完 [Ch 23 — GlobalISel 现状](./23-globalisel-status.md)、[Ch 29 — 上游贡献流程](./29-contribution-workflow.md)、[Ch 30 — 维护者视角](./30-maintainership-and-triage.md)。本章不重复“怎么修”，只回答“修哪里、为什么值得修”。

---

## 31.1 为什么用“数据”选题，而不是凭感觉

随机修 bug 的最大问题不是难度，而是**没有筛选器**：你可能花两天复现一个没人关心的边角，最后发现上游已经有两个 PR；也可能碰了最热的文件，review 排在三周后。churn、测试密度和缺口清单合起来，正好提供三个筛选维度：

| 信号 | 怎么量 | 它告诉你什么 | 误读风险 |
|---|---|---|---|
| **churn 高** | `git log --since --oneline -- <path> \| wc -l` | 代码在快速演化，维护需求真实存在；reviewer 熟悉这块 | 冲突也多；一个 NFC 清理可能被要求拆成三个 PR |
| **churn 低、测试多** | 同窗口提交数 + 测试目录文件数 | 接口稳定，回归保护强；改动少但每次都要重验证 | 不是“没事做”，而是“改动门槛高”，ABI/编码尤其如此 |
| **缺口明确** | 现有测试的 `unsupported`/fallback/`TODO`，或某个 IR 形状选不中 | 有 before/after，可写成 lit；不跟热点文件抢行 | “缺口”必须能复现，不能只是读代码觉得缺 |

三个直接结论：

1. **高 churn 不等于高价值。** `RISCVISelLowering.cpp` 一年 498 个提交，说明它是主战场，也说明任何改动都要和几十个在途 PR 抢同一片 hunk。适合有明确 bug 的修复，不适合“顺手重构”。
2. **低 churn 不等于低价值。** `RISCVInstrFormats.td` 一年只有 10 个提交，但 `MC/RISCV` 有 621 个 lit 用例；改一个编码字段，验证成本远高于改一个 pattern。稳定区的贡献通常不是“新功能”，而是“修一个长期没人敢碰的错”。
3. **缺口比随机 bug 更容易被接受。** GISel 的 TLS、P-ext 的缺失 pattern、RVV 的 VL 优化、MC 的别名/诊断，都有清楚的“现在不行、之后行”的对比；reviewer 不需要先相信你的价值判断，只需要看 diff 和测试。

churn 统计也有噪声：重命名、批量 `update_llc_test_checks.py`、NFC 都会抬高计数。所以本章的用法是“**先定位，再用 `--numstat` 和 `git log -S` 看实质**”，而不是拿一个数字当结论。

---

## 31.2 先建立直觉：贡献机会的四象限

把“churn”放在纵轴，把“改动对象”放在横轴：左边偏现有实现，右边偏接口/数据模型。四个象限不是分类学，而是四种完全不同的工作方式。

```
            高 churn
               │  ① 热点重构/性能（ISelLowering、P-ext）
               │  ② 新扩展/新指令（Features.td + InstrInfo*.td）
───────────────┼───────────────
               │  ③ 缺口补齐（GISel、测试、文档）
               │  ④ 稳定区（编码、ABI，改动需谨慎）
            低 churn
             左：实现细节                     右：接口/数据模型
```

| 象限 | 代表文件（12 个月提交数） | 典型 PR | 上手难度 | 主要风险 |
|---|---|---|---|---|
| ① 热点重构/性能 | `RISCVISelLowering.cpp` 498、`RISCVTargetTransformInfo.cpp` 89、`RISCVInstrInfo.cpp` 92 | 修一个 codegen 回归；把一个 `Custom` lowering 改小；补一个 DAG combine | 4/5 | 冲突最多；测试形状复杂；容易把 NFC 做成行为变化 |
| ② 新扩展/新指令 | `RISCVInstrInfoP.td` 184、`RISCVFeatures.td` 80、`RISCVInstrInfo.td` 65 | P-ext 一条 `Pat<>`；新 CPU；新 `SchedRW` | 3/5 | 七道门（Feature/predicate/pattern/编码/解析/打印/测试）；`experimental` 闸门 |
| ③ 缺口补齐 | `GISel/RISCVLegalizerInfo.cpp` 46、`RISCVVLOptimizer.cpp` 24、`RISCVVectorPeephole.cpp` 30、测试/文档 | GISel TLS/byval；MC 别名；release note；补 lit 用例 | 2/5 | 缺口可能“看起来缺、其实有更好方案”；GISel 不影响默认后端 |
| ④ 稳定区 | `RISCVInstrFormats.td` 10、`RISCVRegisterInfo.td` 28、ABI/`e_flags` | 修编码字段；修 ABI 边角；改 `.attribute` 规范化 | 5/5 | 一动就是 ABI/ELF 兼容性；必须 release note；review 极慢 |

`AsmParser/RISCVAsmParser.cpp` 的 59 次是一个例外：接口本身稳定，但别名、`.option`、诊断的修补频率高，它更像④里的“接口税”，不要因为数字高就当成热点重构。

一个实用判断：**如果你说不清“before 和 after 的机器码/诊断差在哪”，就不在①/②；如果你说不清“谁会感知到这个变化”，就不在④。** ③ 是最适合第一次上游贡献的象限，因为它天然带 before/after，而且通常不碰 ABI。

---

## 31.3 churn 地图：真实数据

先跑总量。12 个月 1,545 个提交，6 个月 817 个；按 183 天算，最近约每天 4.5 个。这个密度意味着“排队”是常态，不是你的 PR 特别慢。

```bash
$ cd /home/ubuntu/llvm/trees/ct-instr-trunk
$ git rev-list --count HEAD                                                   # 607142
$ git log --since='12 months ago' --oneline -- llvm/lib/Target/RISCV | wc -l  # 1545
$ git log --since='6 months ago'  --oneline -- llvm/lib/Target/RISCV | wc -l  # 817
```

top 15 的真实输出（`--pretty=format:` 去掉提交行，`sed '/^$/d'` 去掉空行）：

```bash
$ git log --since='12 months ago' --name-only --pretty=format: -- llvm/lib/Target/RISCV \
    | sed '/^$/d' | sort | uniq -c | sort -rn | head -15
    498 llvm/lib/Target/RISCV/RISCVISelLowering.cpp
    184 llvm/lib/Target/RISCV/RISCVInstrInfoP.td
     92 llvm/lib/Target/RISCV/RISCVInstrInfo.cpp
     89 llvm/lib/Target/RISCV/RISCVTargetTransformInfo.cpp
     80 llvm/lib/Target/RISCV/RISCVFeatures.td
     71 llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp
     68 llvm/lib/Target/RISCV/RISCVTargetTransformInfo.h
     68 llvm/lib/Target/RISCV/RISCVTargetMachine.cpp
     65 llvm/lib/Target/RISCV/RISCVInstrInfo.td
     59 llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp
     46 llvm/lib/Target/RISCV/RISCVFrameLowering.cpp
     46 llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp
     40 llvm/lib/Target/RISCV/RISCV.h
     38 llvm/lib/Target/RISCV/RISCVProcessors.td
     38 llvm/lib/Target/RISCV/RISCVISelLowering.h
```

只看提交数会漏掉“改了多少”。前五名的增删行数：

| 文件 | 提交 | 新增 | 删除 | 读法 |
|---|---:|---:|---:|---|
| `RISCVISelLowering.cpp` | 498 | 10,562 | 4,578 | 主战场；平均每个提交净增 12 行，但分布极不均 |
| `RISCVInstrInfoP.td` | 184 | 4,018 | 1,629 | P-ext 在持续填 pattern，单条 pattern 很小，测试很大 |
| `RISCVInstrInfo.cpp` | 92 | 1,354 | 836 | copyPhysReg、分支/尺寸分析、RVV spill 都从这里过 |
| `RISCVTargetTransformInfo.cpp` | 89 | 1,218 | 434 | 成本模型随 RVV/P-ext 一起长 |
| `RISCVFeatures.td` | 80 | 805 | 339 | 新扩展/新 implies；一改就牵动 `features-info.ll` 与 `attributes.ll` |

两个最容易被问“为什么这么多”的文件：

**`RISCVInstrInfoP.td` 的 184 次。** 其中 108 个提交消息也带 `P-ext`；整个后端 171 个 `P-ext` 提交，差额落在 clang/intrinsic/测试侧。P 规范从 0.18 走到 0.21，指令名、编码、intrinsic 一起变；每个 PR 通常只加 1–4 行 `Pat<>`，但要在 `rvp-scalar-rv64.ll`、`rvp-simd-32.ll`、`rvp-simd-64.ll` 里补几十行 CHECK。测试 churn 也印证了这一点：`rvp-simd-64.ll` 一年 79 次，`rvp-simd-32.ll` 59 次，`rv32p.ll` 54 次。P 还是 `RISCVExperimentalExtension<0, 21, ...>`（`RISCVFeatures.td:1313`），所以每个测试都要写 `+experimental-p`，不能写 `+p`。

**`AsmParser/RISCVAsmParser.cpp` 的 59 次。** 它不是“汇编器小修小补”，而是所有语法面的唯一入口：`.option arch`、`.attribute arch`、别名、操作数谓词、错误诊断都在这里汇合。12 个月里的样本形状很分散：`fe67f34107fa` 修 C/Zce 在 `resetToArch` 里的蕴含（31 行源码、16 行测试）；`12336bb0e16d` 用可选 offset 操作数替掉零偏移 `InstAlias`；`8b4e74e02ede` 给多余操作数补 “unexpected extra operand”；`8b66604bb8e6` 把 `mop.rr.7` 重映射到 `sspush`。共同点是**一行 parser 改动会扫到整片 MC 测试**。

稳定区则相反：`RISCVInstrFormats.td` 10 次、`RISCVRegisterInfo.td` 28 次、`RISCVRegisterInfo.cpp` 33 次。它们不是没价值，而是“改动门槛高”：编码字段错了是 silent miscompile，ABI 错了是链接期/运行期灾难。第一次贡献不建议从这里开始。

---

## 31.4 测试密度地图：保护强，迭代也贵

“测试多”有两个含义：回归保护强；你改一行，可能要等一整轮 CHECK 更新。下表全部在本机重跑，`lit` 使用 `-j16`：

| 目录 | 文件数 | lit 用例 | 12 个月提交 | 16 线程 wall | 单线程 CPU 时间 |
|---|---:|---:|---:|---:|---:|
| `llvm/test/CodeGen/RISCV` | 2,742 | 2,741 | 1,140 | 30.9s | 7m01s |
| `llvm/test/MC/RISCV` | 622 | 621 | 140 | 1.9s | 17.7s |
| `clang/test/CodeGen/RISCV` | 2,824 | 2,824 | 117 | 40.9s | 9m39s |
| `llvm/test/CodeGen/RISCV/GlobalISel` | 500 | 500 | 118 | 2.65s | 29.8s |
| `llvm/test/tools/llvm-mca/RISCV` | 386 | 357 | 38 | 0.59s | 5.8s |

注意“文件数”和“用例数”不是一回事：`CodeGen/RISCV` 有 2,742 个文件但 2,741 个 lit 用例（有一个 `lit.local.cfg` 之类的非测试文件）；`llvm-mca/RISCV` 有 386 个文件、357 个用例，其中 137 个是 `.s`。基线的“357”指的是用例数，不是文件数。

读法：

- **CodeGen 的 30.9s 是并行后的墙钟时间**；单线程 CPU 时间 7 分钟。你改 `RISCVISelLowering.cpp` 的一个 combine，实际要等的不止 31 秒：CI 还会跑 clang、MC、GlobalISel、mca 和别的 target。
- **clang 的 40.9s / 9m39s CPU** 说明“后端一行，前端一片”不是夸张。`RISCVFeatures.td` 加一个扩展，`features-info.ll`（一年 51 次）和 `attributes.ll`（37 次）经常一起动。
- **MC 只有 1.9s，但 621 个用例**，所以 MC 改动适合“快速反复”：`llvm-mc -show-encoding` → `llvm-objdump -d` → 单文件 lit，反馈循环最短。
- **GlobalISel 500 个用例 2.65s** 是最舒服的缺口区；但默认 CI 不跑 GISel，必须显式 `-global-isel`。这也意味着“默认后端绿”不代表 GISel 没坏。

测试密度本身就是选题信号：`rvp-simd-64.ll` 一年 79 次、`rvp-simd-32.ll` 59 次，说明 P-ext 的测试面还在快速膨胀；`O3-pipeline.ll` 33 次，说明 pass 管线改动会牵动管线契约测试。**先看测试 churn，再决定要不要碰这个文件。**

---

## 31.5 七类可上手题目

下面每一类都给“代表文件、第一步命令、可验证的完成标准、难度（1–5）”。难度按“第一次做需要几天”估：1 = 半天，3 = 2–3 天，5 = 一周以上且需要 maintainer 预沟通。

### ① 一个 `[RISCV] Fix ...` 的 codegen bug

- **代表文件**：`RISCVFrameLowering.cpp`、`RISCVISelLowering.cpp`、`RISCVInstrInfo.cpp`。
- **真实形状**：`a44f88533543` “Correctly restore libcall CSRs with SCS”，源码 +16/−3，两个测试文件合计 +1,122/−20（总计 +1,138/−23）。bug fix 的典型比例是“十几行源码 + 上千行 CHECK”。
- **第一步**：
  ```bash
  git -C $TR show --stat a44f88533543
  git -C $TR log --since='12 months ago' --oneline --grep='Fix' -- llvm/lib/Target/RISCV | head
  ```
  12 个月有 **179** 个提交消息含 `Fix` 的后端提交，足够你找到“同类形状”。
- **完成标准**：最小 `.ll`/`.mir` 在父提交上 `llc -verify-machineinstrs` 失败，在修复后通过；新增 lit 用例只覆盖这个不变量；没有顺带改无关 CHECK。
- **难度**：3/5。

### ② 一个缺失的 `Pat<>` / `ComplexPattern`

- **代表文件**：`RISCVInstrInfoP.td`（P-ext）、`RISCVInstrInfo.td`（基础整数）、`RISCVInstrInfoZb.td`（Zb*）。
- **真实形状**：`51530e3179d6` “Add isel pattern for mul.w01/mul.w11”，**+4 行源码、+48 行测试**。之前 `(sra X, 32)` 被单独选成 `srai`，再喂给 `mul`；加两条 `Pat` 后折成一条 `mul.w01`/`mul.w11`。
- **第一步**：
  ```bash
  git -C $TR show 51530e3179d6 -- llvm/lib/Target/RISCV/RISCVInstrInfoP.td
  ```
- **完成标准**：同一段 IR 从“两条指令”变成“一条指令”；`llc` 输出可肉眼核对；lit 用 `-mattr=+experimental-p`（P-ext）或对应扩展；RV32/RV64 的 `IsRV64` 条件写对。
- **难度**：2/5。

### ③ 一个 GISel legalizer/selector 缺口

- **代表文件**：`GISel/RISCVLegalizerInfo.cpp`、`GISel/RISCVInstructionSelector.cpp`、`GISel/RISCVCallLowering.cpp`。
- **Ch 23 的清单**：byval 参数/调用（`RISCVCallLowering.cpp:596`）、TLS `G_GLOBAL_VALUE`（`RISCVInstructionSelector.cpp:1480`）、尾调用/`musttail`（615–616）、定长向量、RVV tuple。
- **实测缺口**：TLS 全局地址在 `-global-isel -global-isel-abort=1` 下直接失败：
  ```text
  LLVM ERROR: cannot select: %0:gprb(p0) = G_GLOBAL_VALUE @tls (in function: addr_tls)
  ```
- **第一步**：
  ```bash
  $T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 tls.ll -o -
  ```
- **完成标准**：`abort=1` 不再报错；输出里有正确的 TLS 地址序列；新增 `GlobalISel/tls.ll`；`llvm-lit -sv llvm/test/CodeGen/RISCV/GlobalISel` 500/500 通过。
- **难度**：3/5（TLS）；4/5（byval/tail，涉及 ABI 或栈布局）。

### ④ 一个 MC 编码/别名/反汇编问题

- **代表文件**：`AsmParser/RISCVAsmParser.cpp`、`MCTargetDesc/RISCVMCCodeEmitter.cpp`、`Disassembler/RISCVDisassembler.cpp`。
- **真实形状**：`fe67f34107fa` 修 `resetToArch` 的 C/Zce 蕴含，31 行源码、16 行测试；这类改动的反馈循环最短。
- **第一步**：
  ```bash
  $T/bin/llvm-mc -triple=riscv64 -mattr=+experimental-p -show-encoding one.s
  ```
  例：`padd.b a0, a1, a2` → `encoding: [0x3b,0x85,0xc5,0x84]`。
- **完成标准**：编码字节与 `llvm-objdump -d` 往返一致；非法形式有明确诊断；新增/更新 `llvm/test/MC/RISCV/*.s`；不改变已有别名的打印形式。
- **难度**：2/5。

### ⑤ 一个 RVV VL/SEW 优化

- **代表文件**：`RISCVVLOptimizer.cpp`、`RISCVVectorPeephole.cpp`、`RISCVInsertVSETVLI.cpp`；测试看 `llvm/test/CodeGen/RISCV/rvv/vl-opt.mir`、`vsetvli-insert*.mir`、`rvv-peephole-*.ll`。
- **第一步**（看优化器为什么放弃）：
  ```bash
  $T/bin/llc -mtriple=riscv64 -mattr=+v -debug-only=riscv-vl-optimizer rvv.ll -o /dev/null
  ```
  实测会打印 `Found a candidate for VL reduction` 与 `Abort due to CommonVL == VLOp`，这就是选题入口。
- **完成标准**：同一函数少一条 `vsetvli` 或少一条 `vmerge`；`vl-opt.mir`/端到端 `.ll` 的 CHECK 更新；`llvm-lit -sv llvm/test/CodeGen/RISCV/rvv` 通过。
- **难度**：4/5（要懂 VLMAX、SEW/LMUL、policy 与 `$noreg`）。

### ⑥ 一个调度模型补充

- **代表文件**：`RISCVSchedSiFive7.td`、`RISCVSchedule.td`、`RISCVScheduleV.td`，以及各扩展的 `RISCVInstrInfo*.td`。
- **实测缺口**：`RISCVInstrInfoP.td` 里 `Sched<...>` 的数量是 **0**；所以 P 指令在 mca 里无法解析调度类：
  ```text
  error: unable to resolve scheduling class for write variant.
  note: instruction: 	padd.b	a0, a1, a2
  ```
- **第一步**：
  ```bash
  $T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+experimental-p padd.s
  ```
- **完成标准**：mca 不再报 `unable to resolve scheduling class`；给指令补 `Sched<[Write...]>` 或在目标模型里补 `WriteRes`；`llvm/test/tools/llvm-mca/RISCV` 357 个用例通过。
- **难度**：3/5（补一个类容易，证明数字合理难）。

### ⑦ 一个文档/测试改进

- **代表文件**：`llvm/docs/RISCVUsage.md`、`llvm/docs/ReleaseNotes.md:378`、`llvm/test/MC/RISCV/rvy-invalid-attributes.s` 这类纯测试文件。
- **第一步**：
  ```bash
  grep -n "Changes to the RISC-V Backend" llvm/docs/ReleaseNotes.md
  git -C $TR diff --check
  ```
- **完成标准**：release note 的 bullet 与现有格式一致（“Added experimental MC support for ...”、“Adds experimental assembler/CodeGen support for ...”）；测试只改 CHECK/新增负例；`git diff --check` 干净。**本机没有配置 Sphinx，文档构建未实测**，这是本章唯一不能机器验证的完成标准。
- **难度**：1/5。

---

## 31.6 选题检查清单

在开 branch 之前，按顺序问自己八个问题。任何一项答不上来，就先别写代码。

| # | 检查 | 具体动作 | 红旗 |
|---|---|---|---|
| 1 | 5 分钟内能复现吗？ | 一条 `llc` 或 `llvm-mc`，输入是自包含的最小 `.ll`/`.s` | 需要完整项目、需要 qemu、需要特定 linker script |
| 2 | 能写成 lit 吗？ | 能写 `RUN:` + `CHECK:`；有负例 | 只能“看汇编觉得不对”，没有稳定断言 |
| 3 | 会改 ABI/`e_flags` 吗？ | `grep -rn "e_flags\|ELFHeaderFlags\|ABI"` 改到的文件 | 改了 `.attribute arch`、CC 规则、`e_flags` 却当普通 bug |
| 4 | 要 release note 吗？ | 用户能否感知：新扩展、新 CPU、新选项、行为变化 | 用户可见却没写；纯 NFC 却硬加一条 |
| 5 | reviewer 是谁？ | `llvm/Maintainers.md:357-366`：Alex Bradbury、Craig Topper、Philip Reames、Sam Elliott | 选了 P-ext/RVV 却不知道谁看；PR 描述没有 why |
| 6 | 影响默认后端吗？ | 不带 `-global-isel` 跑一遍；不带 `-mattr` 跑一遍 | GISel-only 改动却宣称“修了默认 codegen” |
| 7 | 冲突面多大？ | 对要改的文件跑 12 个月 churn；看是否 top 5 | 同时改 `RISCVISelLowering.cpp` 的三个不相关函数 |
| 8 | diff 够小吗？ | `git diff --stat`；源码尽量 < 30 行；一个行为一个 PR | 顺手格式化、顺手改注释、顺手加无关测试 |

第 5 条不是“去找关系”，而是“去找对的问题”。`Maintainers.md` 的名单告诉你谁最可能指出 ABI/编码问题；PR 描述里先写 Problem/Root cause/Fix/Tests，比 @ 谁更有用。

---

## 31.7 一个完整的“从选题到 PR”示例

选 ②：P-ext 的 `mul.w01`/`mul.w11`。这不是虚构，是 `51530e3179d6` 的真实形状。

**问题**：RV64 上 `(sext i32 a) * (b >> 32)` 先选 `srai`，再选 `mul`；P-ext 有 `mul.w01` 可以一条完成。

**落点**：`llvm/lib/Target/RISCV/RISCVInstrInfoP.td:3409` 附近，已有的 `mul.w00` pattern 旁边。

**预期 diff 形状**（源码只有 4 行）：

```diff
   def : Pat<(i64 (mul sexti32:$rs1, (sext_inreg GPR:$rs2, i32))),
             (MUL_W00 sexti32:$rs1, GPR:$rs2)>;
+  def : Pat<(i64 (mul sexti32:$rs1, (sra GPR:$rs2, (XLenVT 32)))),
+            (MUL_W01 sexti32:$rs1, GPR:$rs2)>;
+  def : Pat<(i64 (mul (sra GPR:$rs1, (XLenVT 32)), (sra GPR:$rs2, (XLenVT 32)))),
+            (MUL_W11 GPR:$rs1, GPR:$rs2)>;
   def : Pat<(i64 (mul zexti32:$rs1, (and GPR:$rs2, 0xffffffff))),
             (MULU_W00 zexti32:$rs1, GPR:$rs2)>;
```

**测试**：`llvm/test/CodeGen/RISCV/rvp-scalar-rv64.ll` 加 4 个函数（正序/交换操作数各两条），共 +48/−2；`−2` 是因为新增函数让后面的 `.LCPI` 编号整体后移，`update_llc_test_checks.py` 自动改了两行。

**验证命令**（全部在本机跑过；`/tmp/mulw01.ll` 的内容见 E6）：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+experimental-p /tmp/mulw01.ll -o - | sed -n '/^mul_w01:/,/Lfunc_end/p'
# mul.w01 a0, a0, a1

$T/bin/llvm-lit -sv llvm/test/CodeGen/RISCV/rvp-scalar-rv64.ll
# 1/1 PASS
```

**PR 标题**：`[RISCV][P-ext] Add isel pattern for mul.w01/mul.w11`

**PR 正文骨架**：

```text
Problem: (sext i32 a) * (b >> 32) emits srai + mul on RV64.
Root cause: no isel pattern folds the shift into mul.w01/mul.w11.
Fix: add two Pat<> entries next to mul.w00; no ABI/e_flags change.
Tests: four functions in rvp-scalar-rv64.ll, both operand orders.
Release note: not needed (codegen quality only).
```

**完成标准**：`mul.w01` 单条出现；lit 通过；`git diff --check` 干净；`git diff --stat` 是 2 文件、约 50 行；没有改 `ReleaseNotes.md`。这就是一个“几行源码 + 一组测试”的标准可提交形状。

---

## 31.8 源码落点

| 内容 | 路径 | 关键行/符号 | 为什么是入口 |
|---|---|---|---|
| P-ext Feature 与实验闸门 | `llvm/lib/Target/RISCV/RISCVFeatures.td` | `FeatureStdExtP` 1313、`HasStdExtP` 1318 | 决定 `+experimental-p`、predicate 与 `.attribute arch` |
| P 指令与 pattern | `llvm/lib/Target/RISCV/RISCVInstrInfoP.td` | `PADD_B` 723、`MUL_W01` 822、`MUL_W01` pattern 3410、`PNSRLI_H` pattern 2943 | P-ext 选题的第一落点 |
| 基础整数 pattern | `llvm/lib/Target/RISCV/RISCVInstrInfo.td` | `Pat<>`/`Pseudo` 定义 | 非扩展类缺失 pattern 通常在这里 |
| Zb* pattern | `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | `ctpop` pattern 590 | 位运算/人口计数类选题 |
| 自定义 lowering | `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` | `LowerOperation` 8374、`PerformDAGCombine` 25386 | 热点 bug 与性能问题的主入口 |
| 手写选指 | `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp` | `Select` 1178、`SelectAddrRegImm` 3630 | `ComplexPattern`/特殊寻址 |
| 指令信息 | `llvm/lib/Target/RISCV/RISCVInstrInfo.cpp` | `getInstSizeInBytes` 2090、`copyPhysReg` 490 | 分支距离、RVV spill、复制 |
| 成本模型 | `llvm/lib/Target/RISCV/RISCVTargetTransformInfo.cpp` | `getArithmeticInstrCost` 3091 | 向量化/循环决策 |
| 帧布局 | `llvm/lib/Target/RISCV/RISCVFrameLowering.cpp` | `determineCalleeSaves` 1850 | SCS、stack probe、RVV 帧 |
| pass 管线 | `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` | `addInstSelector` 449 | GISel/SDAG 选择与 pass 顺序 |
| 汇编解析 | `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp` | `parseInstruction` 3186、`resetToArch` 3261 | 别名、`.option arch`、诊断 |
| 编码 | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp` | `getBinaryCodeForInstr` 85 | 指令编码/固定位 |
| 反汇编 | `llvm/lib/Target/RISCV/Disassembler/RISCVDisassembler.cpp` | `getInstruction` 44 | MC 往返验证 |
| GISel 合法化 | `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp` | `G_MUL` builder 506/519 | GISel 缺口第一站 |
| GISel 选指/ABI | `llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`、`RISCVCallLowering.cpp` | `G_GLOBAL_VALUE` 1480；byval 596；tail 615 | TLS/byval/musttail |
| RVV VL | `llvm/lib/Target/RISCV/RISCVVLOptimizer.cpp` | `runOnMachineFunction` 1453 | `riscv-vl-optimizer` 调试入口 |
| 调度模型 | `llvm/lib/Target/RISCV/RISCVSchedSiFive7.td` | `WriteRes<WriteCPOP>` 393 | 补 `SchedRW`/`WriteRes` 的参照 |
| 文档 | `llvm/docs/ReleaseNotes.md`、`llvm/docs/RISCVUsage.md` | RISC-V 小节 378 | release note 与用户文档 |
| reviewer 名单 | `llvm/Maintainers.md` | 357–366 | RISC-V backend maintainers |

---

## 31.9 可跑实验

以下命令默认在 `$TR` 根目录执行（`$T` 已 `source` 环境）；输出是 2026-11-08 的实测，不是示意。

**E1 — 总量与 churn**

```bash
$ cd /home/ubuntu/llvm/trees/ct-instr-trunk
$ git rev-list --count HEAD                                                   # 607142
$ git log --since='12 months ago' --oneline -- llvm/lib/Target/RISCV | wc -l  # 1545
$ git log --since='6 months ago'  --oneline -- llvm/lib/Target/RISCV | wc -l  # 817
```

读法：12 个月 1,545、6 个月 817，最近半年占 53%；P-ext 与 RVV 是主要增量。

**E2 — top 15 文件**

```bash
$ git log --since='12 months ago' --name-only --pretty=format: -- llvm/lib/Target/RISCV \
    | sed '/^$/d' | sort | uniq -c | sort -rn | head -15
```

输出见 31.3。读法：`RISCVISelLowering.cpp` 498 是绝对热点；P td 184 是“新扩展”象限；`AsmParser` 59 是“稳定接口但高频修补”的典型。

**E3 — 测试计数与耗时**

```bash
$ for d in llvm/test/CodeGen/RISCV llvm/test/MC/RISCV clang/test/CodeGen/RISCV \
           llvm/test/CodeGen/RISCV/GlobalISel llvm/test/tools/llvm-mca/RISCV; do
>   printf '%-46s %s\n' "$d" "$(find "$d" -type f | wc -l)"; done
$ $T/bin/llvm-lit -q -j16 llvm/test/CodeGen/RISCV
$ $T/bin/llvm-lit -q -j16 llvm/test/MC/RISCV
$ $T/bin/llvm-lit -q -j16 clang/test/CodeGen/RISCV
$ $T/bin/llvm-lit -q -j16 llvm/test/CodeGen/RISCV/GlobalISel
$ $T/bin/llvm-lit -q -j16 llvm/test/tools/llvm-mca/RISCV
```

读法：`CodeGen/RISCV` 2,741 个用例、30.9s wall / 7m01s CPU；`MC` 621 个用例、1.9s；`clang` 2,824 个、40.9s；`GlobalISel` 500 个、2.65s；`mca` 357 个、0.59s。

**E4 — `Fix` 提交的形状**

```bash
$ git log --since='12 months ago' --oneline --grep='Fix' -- llvm/lib/Target/RISCV | wc -l
179
$ git log --since='12 months ago' --oneline --grep='Fix' -- llvm/lib/Target/RISCV | head -5
843ae8e14efc [NFC][RISCV] Add curly braces around case statement (#239653)
27a2da4d2716 [RISCV] Remove X5 from CSR_RT_MostRegs/CSR_RT_MostRegs_RVE (#238090)
3ebd8b97a55f [RISCV][GISel] Drop nsw and nuw flags when promoting s32 add, sub, and shl on RV64. (#234782)
38f8b88d236c [AArch64][PowerPC][RISCV] Ignore debug instructions when trimming stackmap shadows (#219756)
bf9172ec3752 [RISCV][GlobalISel] Fix call lowering for Zfinx and Zhinx (#231981)
```

读法：`Fix` 不等于“后端 bug”，有 NFC、跨 target、GISel；必须再加路径过滤并读 `--stat`。

**E5 — 用 `-S` 追溯一个 pattern 的来路**

```bash
$ git log -S'PNSRLI_H' --oneline -- llvm/lib/Target/RISCV/RISCVInstrInfoP.td | wc -l
8
$ git log -S'PNSRLI_H' --oneline -- llvm/lib/Target/RISCV/RISCVInstrInfoP.td | head -3
cad1eecaac83 [Clang][RISCV] Add packed narrowing shift intrinsics (#228726)
97bceede1059 [RISCV][P-ext] Improve the codegen for unzip-like shuffle and buildvector (#208763)
a71ca081f6f7 [RISCV][P-ext] Replace v4i8/v2i16 mul and v4i8 mulh patterns on RV32 with custom lowering. (#204382)
```

读法：`-S` 数的是“字符串出现次数变化”，能告诉你某个 pattern/指令是何时、以什么动机进来的；再配合 `git show` 看测试形状。

**E6 — 一个真实 `Pat<>` 的 before/after**

```bash
$ cat > /tmp/mulw01.ll <<'LL'
define i64 @mul_w01(i32 signext %a, i64 %b) nounwind {
  %a_sext = sext i32 %a to i64
  %b_hi = ashr i64 %b, 32
  %mul = mul i64 %a_sext, %b_hi
  ret i64 %mul
}
LL
$ $T/bin/llc -mtriple=riscv64 -mattr=+experimental-p /tmp/mulw01.ll -o - | sed -n '/^mul_w01:/,/Lfunc_end/p'
	mul.w01	a0, a0, a1
	ret
```

读法：没有这条 pattern 时是 `srai` + `mul`；有了之后是一条。这就是 ② 类题目的完整验收。

**E7 — GISel 缺口：TLS 直接 abort**

```bash
$ cat > /tmp/tls.ll <<'LL'
@tls = thread_local global i32 0
define ptr @addr_tls() { ret ptr @tls }
LL
$ $T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 /tmp/tls.ll -o -
LLVM ERROR: cannot select: %0:gprb(p0) = G_GLOBAL_VALUE @tls (in function: addr_tls)
```

读法：这是 `-global-isel-abort=1` 的默认行为，不是“没开 GISel”。修好后这条命令应输出 TLS 地址序列并退出 0。

**E8 — MC 实验：P-ext 的实验闸门与编码往返**

```bash
$ printf 'padd.b a0, a1, a2\n' > /tmp/one.s
$ $T/bin/llvm-mc -triple=riscv64 -mattr=+p -show-encoding /tmp/one.s
'+p' is not a recognized feature for this target (ignoring feature)
error: instruction requires the following: 'Base P' (Packed SIMD)
$ $T/bin/llvm-mc -triple=riscv64 -mattr=+experimental-p -show-encoding /tmp/one.s
	padd.b	a0, a1, a2                      # encoding: [0x3b,0x85,0xc5,0x84]
$ $T/bin/llvm-mc -triple=riscv64 -mattr=+experimental-p -filetype=obj /tmp/one.s -o /tmp/one.o
$ $T/bin/llvm-objdump -d --mattr=+experimental-p /tmp/one.o
       0: 84c5853b     	padd.b	a0, a1, a2
```

读法：`+p` 会被静默忽略，只剩“缺少扩展”的错误；正确写法是 `+experimental-p`。编码 `0x84c5853b` 与反汇编往返一致。

**E9 — 调度模型缺口：P 指令没有 scheduling class**

```bash
$ printf 'padd.b a0, a1, a2\n' > /tmp/padd.s
$ $T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+experimental-p /tmp/padd.s
error: unable to resolve scheduling class for write variant., use -skip-unsupported-instructions=lack-sched to ignore these on the input.
note: instruction: 	padd.b	a0, a1, a2
```

读法：`RISCVInstrInfoP.td` 里 `Sched<...>` 数量为 0，所以 mca 无法给 P 指令建模。补 `Sched<[Write...]>` 或目标模型 `WriteRes` 是一个低冲突、可验证的 ⑥ 类题目。

---

## 31.10 踩雷集锦

1. **只看 churn，不看测试。** `RISCVISelLowering.cpp` 498 次很诱人，但 `CodeGen/RISCV` 有 2,741 个用例；改一个 combine 可能让几十个 CHECK 变红。先跑目标文件相关测试，再决定是否碰热点。
2. **选 P-ext 却不加 `experimental`。** `+p` 不是已识别 feature，会被静默忽略；正确是 `+experimental-p`。测试里写错，错误会变成“instruction requires Base P”，浪费半天。
3. **改 ABI/`e_flags` 不写 release note。** `.attribute arch`、`e_flags`、CC 规则都是用户可见契约。Ch 22 的 `e_flags` 六位与 ABI 校验必须一起看；release note 在 `ReleaseNotes.md:378`。
4. **热点文件冲突多。** 498 次/年的文件，同时可能有多个在途 PR。把 NFC 拆出去、把行为改动压到一个 hunk，比“我改得很干净”更能减少 review 时间。
5. **GISel 改动不影响默认后端。** 默认 `llc` 走 SDAG；GISel 测试要显式 `-global-isel`。修了 GISel 却在 PR 里写“fix RISC-V codegen”，reviewer 第一句就会问“默认路径呢？”
6. **RVV 无法在本机运行验证。** qemu 版本可能 SIGILL，`vsetvli`/LMUL 行为也可能与硬件不同。诚实做法是：llc/MIR/lit 证明代码形状，运行验证标注“未在本机执行”。
7. **把 NFC 做成行为变化。** 改了 `if` 顺序、枚举覆盖顺序、`unsigned`→`int`，CHECK 就会动；NFC 的承诺是逐位等价，不是“我觉得一样”。
8. **忽略 RV32/RV64 的 predicate。** 很多 P-ext pattern 只在 RV32 或只在 RV64 合法（`IsRV64`、`GPRPairRV32`）。只测一个 triple 就提交，另一个 triple 会直接选指失败或生成非法指令。

---

## 31.11 本章重点整理

- **churn 是需求信号，不是价值判断。** 12 个月 1,545 个提交；`ISelLowering` 498、P td 184、`InstrInfo` 92、TTI 89、`Features.td` 80。
- **P-ext 的高 churn 有明确原因**：171 个 `P-ext` 提交、规范 0.18→0.21、pattern 与测试一起长；测试热点是 `rvp-simd-64.ll` 79、`rvp-simd-32.ll` 59。
- **AsmParser 59 次是“接口税”**：别名、`.option arch`、诊断、操作数谓词都从这里过；一行 parser 改动会扫到整片 MC 测试。
- **稳定区不是冷门区**：`InstrFormats.td` 10 次但 MC 621 个用例；ABI/编码改动少、验证重、必须 release note。
- **缺口优先选 ③**：GISel TLS/byval、P-ext 缺失 pattern、MC 别名、RVV VL、调度模型、文档测试；都有 before/after，冲突小。
- **验收标准固定**：`llc`/`llvm-mc` 可复现 → lit 可断言 → 不碰 ABI/`e_flags`（或明确写 note）→ diff 小于 30 行源码。
- **一个标准 PR 的形状**：`51530e3179d6` 的 +4 源码 / +48 测试；标题、正文、测试、release note 四件事说清楚。

---

## 31.12 自我检核

- [ ] 我能用一条命令重跑 12 个月/6 个月的 churn，并解释为什么 6 个月占了一半以上。
- [ ] 我能说出 top 5 热点文件，以及 `RISCVInstrInfoP.td` 184 次中的 171 次来自哪里。
- [ ] 我能解释 `AsmParser` 59 次为什么不是“小修小补”，并各举一个别名/`.option`/诊断例子。
- [ ] 我能区分“文件数”和“lit 用例数”，并说出 `CodeGen/RISCV`、`MC/RISCV`、`mca/RISCV` 的三组数字。
- [ ] 我能用四象限判断一个题目属于①–④，并说出对应难度与主要风险。
- [ ] 我能为七类题目各写出一条第一步命令，并给出可机器验证的完成标准。
- [ ] 我能复现 `mul.w01` 的 before/after，并说明为什么这个 PR 不需要 release note。
- [ ] 我能复现 GISel TLS 的 `LLVM ERROR`，并解释 `-global-isel-abort=1` 与 `=0` 的区别。
- [ ] 我能说出 P-ext 为什么要写 `+experimental-p`，以及 `+p` 会发生什么。
- [ ] 我能在 `Maintainers.md:357-366` 找到 RISC-V reviewer，并按 31.6 的八项检查过一遍选题。

---

## 31.13 延伸阅读

- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/23-globalisel-status.md`（缺口清单 23.9、500 个测试 23.8）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/29-contribution-workflow.md`（PR 形状、commit message、release note 边界）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/30-maintainership-and-triage.md`（triage/review 视角）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/27-lit-testing-and-update-scripts.md`（lit 与 `update_llc_test_checks.py`）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/21-scheduling-models-and-mca.md`（`SchedRW`、`llvm-mca`）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/17-rvv-lowering.md`（VL/SEW、`riscv-vl-optimizer`）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoP.td`（P-ext 定义与 pattern）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`（合法化 builder）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`（TLS 选指）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVCallLowering.cpp`（byval/tail）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvp-scalar-rv64.ll`（P-ext 标量测试）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/GlobalISel/`（500 个 GISel 测试）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/tools/llvm-mca/RISCV/`（357 个 mca 测试）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/`（621 个 MC 测试）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/ReleaseNotes.md`（RISC-V 小节 378）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/RISCVUsage.md`（扩展用户文档）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/Maintainers.md`（RISC-V backend 357–366）

→ 下一章：[Final Project — 交出一个可提交上游的 RISC-V 修复](./final-project-riscv-contribution.md)
