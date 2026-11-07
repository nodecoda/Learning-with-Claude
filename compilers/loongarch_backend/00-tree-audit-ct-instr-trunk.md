# Ch 00 — 深入分析 `ct-instr-trunk`：一棵正在修 LoongArch 链接器 bug 的 LLVM 树

> 本章目标：把这棵树的“身份、库存、当前改动、构建状态、验证证据、上游位置”一次讲清楚。读完你应该能回答：这棵树为什么存在、`01df7c365d7c` 改了什么、为什么这个修复是对的、以及它离一个可提交的 PR 还差什么。

本章是“树审计”，不是“LoongArch 入门”。LoongArch 的 ISA 和文件地图从 Ch 01 开始。

---

## 0.1 一句话结论

`ct-instr-trunk` 是 LLVM monorepo 的一个 **git worktree**，当前停在分支 `fix/issue-239812-relax-marker-order` 上，只比 `upstream/main` 多一条本地提交：

> `[lld][LoongArch] Don't half-relax TLS LE sequences`

它修的是 **LLD 的 LoongArch TLS local-exec relaxation**：当 `R_LARCH_RELAX` 标记出现在它标注的重定位**之前**时，LLD 只删掉了地址生产指令，却没有把消费者改写成 `$tp` 相对，于是生成了一条读“未初始化寄存器”的错误序列。修复本身只有 24 行（外加 95 行测试），但它把“LLVM 后端 → 汇编器 → ELF 重定位 → 链接器 relaxation”整条链都串了起来，是极好的第一个贡献案例。

---

## 0.2 树的身份

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb
git log --oneline -3
git rev-parse HEAD
git merge-base HEAD upstream/main
```

实测（2026-11-07）：

| 项目 | 值 |
|---|---|
| 路径 | `/home/ubuntu/llvm/trees/ct-instr-trunk` |
| 类型 | worktree（不是 hub，不在 `llvm-project` 里直接改） |
| 分支 | `fix/issue-239812-relax-marker-order` |
| HEAD | `01df7c365d7c5420a2dd66ab941e3a0afb7293c9` |
| HEAD 时间 | 2026-11-07 14:13:14 +0800 |
| upstream/main 尖端（本机 fetch 到的） | `8f639a8fa35dd4f3a79c8b9588f0cf59326fabd0`（2026-11-07 15:37 +0100） |
| merge-base | `5fe5e53cff518bb81e2debaff4747c8b9bac130b` |
| 相对 upstream | 领先 1，落后 19 |
| `origin` | `https://github.com/nodecoda/llvm-project.git` |
| `upstream` | `https://gh-proxy.com/https://github.com/llvm/llvm-project.git`（`no_push`） |

注意两件事：

1. **`origin` 是个人 fork，`upstream` 是只读代理。** 这是本机工作区的固定布局：改动推到 `origin`，PR 从 `nodecoda:fix/...` 发到 `llvm/llvm-project`。
2. **分支已经 push 到 `origin`，但还没有 PR。** `gh pr list --head nodecoda:fix/issue-239812-relax-marker-order` 返回空列表。所以当前状态是“代码和测试都在，PR 还没开”。

### 为什么这棵树叫 `ct-instr-trunk`

`ct` 是工作区里沿用下来的任务前缀（不是 LLVM 概念）；`trunk` 表示它跟的是 `upstream/main` 而不是某个 release 分支。真正有意义的是**分支名**：`fix/issue-239812-relax-marker-order`。看到分支名就应该先去看 issue，而不是从 diff 猜。

---

## 0.3 代码库存：这棵树里 LoongArch 到底有多少东西

先给规模，避免“以为 LoongArch 只是一个小 target”。

### 后端核心 `llvm/lib/Target/LoongArch`

实测 `wc -l`（只统计 `.cpp/.h/.td`，含 `AsmParser/MCTargetDesc/...` 子目录）：

| 文件 | 行数 | 作用 |
|---|---:|---|
| `LoongArchISelLowering.cpp` | 12,283 | 合法化、调用约定、全局地址/TLS、intrinsic、DAG combine |
| `LoongArchLASXInstrInfo.td` | 3,457 | 256 位向量指令 |
| `LoongArchLSXInstrInfo.td` | 3,500 | 128 位向量指令 |
| `LoongArchInstrInfo.td` | 2,807 | 标量/浮点指令、pattern、SDNode 定义 |
| `LoongArchInstrInfo.cpp` | 1,347 | 分支分析、spill/restore、`movImm`、地址模式折叠 |
| `LoongArchScheduleV.td` | 1,291 | 向量调度资源 |
| `LoongArchSchedLA664.td` | 1,142 | LA664 调度模型 |
| `LoongArchSchedLA464.td` | 1,104 | LA464 调度模型 |
| `LoongArchExpandPseudoInsts.cpp` | 989 | Pre-RA/Post-RA 伪指令展开 |
| `LoongArchMergeBaseOffset.cpp` | 878 | `pcalau12i + addi` 地址折叠 |
| `LoongArchOptWInstrs.cpp` | 865 | 32 位 `W` 指令优化 |
| `LoongArchExpandAtomicPseudoInsts.cpp` | 755 | LL/SC 原子序列展开 |
| `LoongArchFrameLowering.cpp` | 716 | prologue/epilogue、栈探测 |
| `LoongArchSchedule.td` | 534 | `SchedWrite`/`SchedRead` 定义 |
| `LoongArchTargetTransformInfo.cpp` | 508 | 成本模型 |
| `LoongArchISelDAGToDAG.cpp` | 501 | DAG→DAG 选指 |
| `LoongArchAsmPrinter.cpp` | 351 | 汇编输出、`.option`、`.attribute` |
| `LoongArchMemoryBarrierOpt.cpp` | 421 | `dbar` 优化 |
| `LoongArchRegisterInfo.cpp` | 377 | frame index 消除、spill |
| `LoongArchSubtarget.cpp` | 120 | CPU/Feature/ABI 初始化 |
| **合计** | **44,723** | 不含测试 |

### 测试库存

| 目录 | 文件数 | 说明 |
|---|---:|---|
| `llvm/test/CodeGen/LoongArch` | 978 | 标量/浮点/ABI/机器 pass，含 `lsx/` 337、`lasx/` 353、`ir-instruction/` 47 |
| `llvm/test/MC/LoongArch` | 335 | 汇编/反汇编/重定位/fixup |
| `lld/test/ELF/loongarch-*` | 48 | 重定位、relaxation、TLS、PLT |
| `clang/test/CodeGen/LoongArch` | 48 | builtins、ABI、向量 |
| `clang/test/CodeGenCXX/LoongArch` | 1 | C++ ABI 相关 |

### Clang / LLD / 其它

| 文件 | 行数 |
|---|---:|
| `clang/lib/Basic/Targets/LoongArch.cpp` | 498 |
| `clang/lib/CodeGen/Targets/LoongArch.cpp` | 469 |
| `clang/lib/Driver/ToolChains/Arch/LoongArch.cpp` | 325 |
| `clang/lib/Sema/SemaLoongArch.cpp` | 530 |
| `clang/include/clang/Basic/BuiltinsLoongArchLSX.def` | 959 |
| `clang/include/clang/Basic/BuiltinsLoongArchLASX.def` | 1,007 |
| `lld/ELF/Arch/LoongArch.cpp` | 1,854 |
| `llvm/include/llvm/IR/IntrinsicsLoongArch.td` | 1,233 |
| `llvm/lib/TargetParser/LoongArchTargetParser.cpp` | 96 |

`1711` 个 tracked 文件路径里带 `loongarch`（含测试输入/期望文件）。这个量级说明：**LoongArch 已经是一个“全栈”target，不是一个实验性后端。**

---

## 0.4 当前分支在修什么：issue #239812

### 4.1 现象

LLD 在链接 LoongArch 的 TLS local-exec 代码时，会把下面这条三指令序列：

```asm
lu12i.w $a0, %le_hi20_r(tvar)
add.d   $a0, $a0, $tp, %le_add_r(tvar)
addi.d  $a0, $a0, %le_lo12_r(tvar)
```

收缩成一条：

```asm
addi.d  $a0, $tp, %le_lo12_r(tvar)
```

这是 `relaxTlsLe()` 的职责。问题出在**它怎么判断“这条重定位可以被 relax”**。

### 4.2 复现的最小对象

issue 里的两个对象**指令、符号、重定位类型、offset、addend 完全相同**，唯一区别是 offset 8 的两条重定位谁先谁后：

```text
canonical（GNU as 的普通输出）:
  0x8  R_LARCH_TLS_LE_LO12_R  tvar
  0x8  R_LARCH_RELAX

hint-first（用 .reloc 手工构造）:
  0x8  R_LARCH_RELAX
  0x8  R_LARCH_TLS_LE_LO12_R  tvar
```

本机用 cross `as` 实测，顺序确实如 issue 所述：

```text
$ loongarch64-unknown-linux-gnu-readelf -rW canonical.o
0000000000000008 R_LARCH_TLS_LE_LO12_R  tvar + 0
0000000000000008 R_LARCH_RELAX

$ loongarch64-unknown-linux-gnu-readelf -rW hint-first.o
0000000000000008 R_LARCH_RELAX
0000000000000008 R_LARCH_TLS_LE_LO12_R  tvar + 0
```

### 4.3 根因：`relaxable()` 只会往后看一条

`lld/ELF/Arch/LoongArch.cpp`：

```cpp
static bool relaxable(ArrayRef<Relocation> relocs, size_t i) {
  return i + 1 < relocs.size() && relocs[i + 1].type == R_LARCH_RELAX;
}
```

它假设“`R_LARCH_RELAX` 一定紧跟在被标注的重定位后面”。但 LoongArch ELF ABI 只规定两者**同 offset**，没有规定谁先谁后。`scanSectionImpl()` 在最后做的是**按 offset 的稳定排序**：

```cpp
llvm::stable_sort(sec.relocs(),
                  [](const Relocation &lhs, const Relocation &rhs) {
                    return lhs.offset < rhs.offset;
                  });
```

同 offset 的相对顺序被保留。于是 hint-first 对象进入 `relax()` 时：

- `HI20_R`（offset 0）后面跟着 `RELAX` → `relaxable()` 为真 → 删掉 `lu12i.w`；
- `ADD_R`（offset 4）后面跟着 `RELAX` → 为真 → 删掉 `add.d`；
- `LO12_R`（offset 8）后面是**同 offset 的另一条重定位**，不是 `RELAX` → 为假 → **不改写消费者**。

结果：

```asm
# 两条生产者被删，消费者仍在读 $a0
addi.d $a0, $a0, 0
ret
```

`$a0` 是调用者传进来的值（复现里是 0），不是 `$tp`。程序随后通过地址 0 取数，SIGSEGV。

### 4.4 修复：记住“生产者已被删”的位置

当前提交在 `relax()` 里加了一个局部状态：

```cpp
// Offset of the `lu12i.w` of the last TLS LE sequence whose address
// producing instructions were removed. ...
uint64_t tlsLeRelaxedAt = UINT64_MAX;
```

在 `R_LARCH_TLS_LE_HI20_R / ADD_R / LO12_R` 的 case 里：

```cpp
bool relaxConsumer = r.type == R_LARCH_TLS_LE_LO12_R &&
                     tlsLeRelaxedAt != UINT64_MAX &&
                     r.offset == tlsLeRelaxedAt + 8;
if (!relaxable(relocs, i) && !relaxConsumer)
  break;
relaxTlsLe(ctx, sec, i, loc, r, remove);
if (remove == 4)
  tlsLeRelaxedAt =
      r.type == R_LARCH_TLS_LE_ADD_R ? r.offset - 4 : r.offset;
```

翻译成状态机：

| 遇到 | 条件 | 动作 | `tlsLeRelaxedAt` |
|---|---|---|---|
| `HI20_R` | `relaxable()` 真 | 删 `lu12i.w`（`remove=4`） | `r.offset`（即序列起点） |
| `ADD_R` | `relaxable()` 真 | 删 `add.d`（`remove=4`） | `r.offset - 4`（仍是序列起点） |
| `LO12_R` | `relaxable()` 真 **或** `r.offset == 起点+8` | 把消费者改写成 `$tp` | 不变 |
| `LO12_R` | 两者都假 | 不动 | 不变 |

关键点：**只要生产者被删了，消费者就必须被改写**，不管 `RELAX` 标记在它前面还是后面。修复把“是否 relax”从“看下一条”扩展成“看下一条 **或** 看同一序列的生产者状态”。

### 4.5 为什么这个修复是安全的

逐种边界情况推演（源码依据 + 手工构造）：

1. **canonical 顺序**：`relaxable()` 仍为真，走原路径，行为不变。
2. **hint-first 顺序**：生产者先被删，`tlsLeRelaxedAt` 被记下；消费者在 `起点+8`，被改写。修复目标。
3. **只有 `LO12_R` 的 `RELAX` 在前，生产者都没删**：`tlsLeRelaxedAt == UINT64_MAX`，`relaxConsumer` 为假，序列原样保留。正确。
4. **`HI20` 删了、`ADD` 没删**：消费者仍会被改写；`ADD` 写 `$a0` 后又被 `addi.d $a0, $tp, ...` 覆盖，结果正确（只是少优化了一条）。
5. **值超出 12 位范围**：`relaxTlsLe()` 一开始就 `return`，生产者不删，`tlsLeRelaxedAt` 不更新，消费者不会被错误改写。
6. **两条相邻 TLS LE 序列**：`LO12` 的 offset 只可能等于“本序列起点 + 8”；上一条序列的 `tlsLeRelaxedAt` 不会误匹配下一条的 `LO12`（除非地址恰好重叠，那不是合法输入）。

代价是：`tlsLeRelaxedAt` 是一个“最近一次生产者被删”的单调状态，不是完整的配对表。对合法输入足够；对恶意/畸形输入，最坏情况是少 relax 或多 relax 一条，而不会静默删掉整段代码。review 时可以接受这个折中，但它值得在 PR 描述里主动说明。

---

## 0.5 验证记录：从“跑不了”到“48/48 + QEMU 通过”

### 5.1 第一个发现：这棵树的构建根本跑不了自己的测试

拿到树时 `toolset/status.sh` 显示 `builds/ct-instr-trunk` 有 28G、`PENDING=1`，看起来一切正常。但：

```text
$ grep LLVM_TARGETS_TO_BUILD builds/ct-instr-trunk/CMakeCache.txt
LLVM_TARGETS_TO_BUILD:STRING=X86;AArch64
$ grep LLVM_ENABLE_PROJECTS builds/ct-instr-trunk/CMakeCache.txt
LLVM_ENABLE_PROJECTS:STRING=clang
$ ls builds/ct-instr-trunk/bin | grep lld
（无）
```

也就是说：**分支改的是 `lld/ELF/Arch/LoongArch.cpp`，但构建里既没有 `LoongArch` target，也没有 `lld`。** 这时跑任何 LoongArch lit 测试都会得到 `UNSUPPORTED`，而不是通过或失败——这是最危险的“假绿”。

修复构建配置（不改变源码）：

```bash
LLVM_TARGETS_TO_BUILD='X86;AArch64;LoongArch' \
LLVM_ENABLE_PROJECTS='clang;lld' \
./toolset/configure.sh ct-instr-trunk
JOBS=24 ./toolset/build.sh ct-instr-trunk \
  llc llvm-mc llvm-objdump lld llvm-readobj yaml2obj FileCheck count not \
  split-file llvm-as llvm-dis opt llvm-mca llvm-config
```

重配后：

```text
$ builds/ct-instr-trunk/bin/llc --version | grep loongarch
    loongarch32 - 32-bit LoongArch
    loongarch64 - 64-bit LoongArch
$ builds/ct-instr-trunk/bin/llvm-config --targets-built
X86 AArch64 LoongArch
$ ls -l builds/ct-instr-trunk/bin/ld.lld
... ld.lld -> lld
```

> 教训：**`llvm-config` 也要重编。** 第一次重配后 lit 仍把 LLD 测试标成 `UNSUPPORTED`，因为旧的 `bin/llvm-config` 还报告 `X86 AArch64`，而 `lld/test/lit.cfg.py` 的 `--targets-built` feature 正是问它。`ninja llvm-config` 之后才恢复。

### 5.2 目标测试

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llvm-lit -sv \
  lld/test/ELF/loongarch-tls-le-relax-order.s \
  lld/test/ELF/loongarch-relax-tls-le.s \
  lld/test/ELF/loongarch-tls-le.s
```

实测：

```text
-- Testing: 3 tests, 3 workers --
Testing:  0.. 10.. ... 90..
Testing Time: 0.33s

Total Discovered Tests: 3
  Passed: 3 (100.00%)
```

### 5.3 整个 LoongArch LLD 面

```bash
$T/bin/llvm-lit -s -j 24 lld/test/ELF --filter 'loongarch'
```

实测：

```text
-- Testing: 48 of 2049 tests, 24 workers --
Testing Time: 1.22s
Total Discovered Tests: 2049
  Excluded: 2001 (97.66%)
  Passed  :   48 (2.34%)
```

### 5.4 端到端：canonical vs hint-first，QEMU 都退出 0

用本机 cross `as` 生成两个对象，用**这棵树构建出的 `ld.lld`** 链接，再用 `qemu-loongarch64` 跑：

```text
$ ld.lld -m elf64loongarch -static -e runtime_entry canonical.o -o canonical.elf
$ ld.lld -m elf64loongarch -static -e runtime_entry hint-first.o -o hint-first.elf

$ objdump -d canonical.elf | sed -n '/<tls_addr>:/,+2p'
0000000000020190 <tls_addr>:
   20190: 02c00044  addi.d  $a0, $tp, 0
   20194: 4c000020  ret

$ objdump -d hint-first.elf | sed -n '/<tls_addr>:/,+2p'
0000000000020190 <tls_addr>:
   20190: 02c00044  addi.d  $a0, $tp, 0
   20194: 4c000020  ret

$ qemu-loongarch64 ./canonical.elf;   echo $?   # 0
$ qemu-loongarch64 ./hint-first.elf;  echo $?   # 0
```

修复前 hint-first 会得到 `addi.d $a0, $a0, 0` 并在取数时 SIGSEGV；修复后两者字节级一致。

### 5.5 一个重要的反证：LLVM 自己不会生成 hint-first

用本树的 `llc` 生成一个 `localexec` TLS 变量：

```llvm
@tvar = thread_local(localexec) global i64 7, align 8
define ptr @tls_addr() { ret ptr @tvar }
```

```text
$ llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+relax -filetype=obj tlsle.ll -o tlsle-llvm.o
$ llvm-readobj -r tlsle-llvm.o
0x0 R_LARCH_TLS_LE_HI20_R tvar
0x0 R_LARCH_RELAX
0x4 R_LARCH_TLS_LE_ADD_R tvar
0x4 R_LARCH_RELAX
0x8 R_LARCH_TLS_LE_LO12_R tvar
0x8 R_LARCH_RELAX
```

LLVM 后端永远输出 canonical 顺序。**这意味着 #239812 不是“LLVM 生成错对象”，而是“LLD 对 ABI 允许的输入不够健壮”。** 这类问题在 review 时最容易被问：“真实编译器会这样生成吗？”答案必须是：“不会，但 ABI 允许，且 GNU ld/mold 都能正确处理。”issue 里已经用 GNU ld 2.46 和 mold 2.40.4 做了对照，这正是修复能被接受的关键证据。

---

## 0.6 这棵树还暴露了哪些工作流问题

1. **构建配置与任务不匹配。** 任务改 lld，构建却没有 lld；任务改 LoongArch，构建却没有 LoongArch。以后开新任务，先跑：
   ```bash
   grep -E 'LLVM_TARGETS_TO_BUILD|LLVM_ENABLE_PROJECTS' builds/<task>/CMakeCache.txt
   ```
2. **`PENDING=0` 不等于“能测”。** `status.sh` 只比较源码与构建时间，不检查 target/project 是否覆盖当前任务。
3. **`UNSUPPORTED` 不是通过。** LoongArch lit 测试在缺少 target 时会整片 `UNSUPPORTED`；如果只看“0 failed”会误判。
4. **`llvm-config` 是 lit feature 的隐形依赖。** 重配 target 后必须重编它，否则 `--targets-built` 仍是旧值。
5. **本地提交还没有 PR。** 代码、测试、issue 都在，但 `gh pr list --head ...` 为空。下一个动作不是继续改代码，而是走 Ch 29 的 PR 流程。

---

## 0.7 上游最近在做什么：40 条 LoongArch 提交的主题分布

`git log --oneline -40 -- llvm/lib/Target/LoongArch clang/lib/Driver/ToolChains/Arch/LoongArch.cpp lld/ELF/Arch/LoongArch.cpp` 的实测结果，可以分成六类：

| 主题 | 代表提交 | 说明 |
|---|---|---|
| 链接/relaxation | `01df7c365d7c`、`c98ead286790`、`ad9a555c9c03` | TLS LE、TLS IE extreme code model、`llvm-objdump` PLT 符号 |
| 向量 lowering/成本 | `7f2cda891828`、`3b1f7fda9e10`、`7a37be2989ef`、`9e5165bde019`、`28b09aae629c` | LDPTR、FLD/VLDREPL、VSLLWIL/VEXTH、XVPERMI.Q、子 128 位向量成本 |
| 调度/机器 pass | `59cdc9a5e41f`、`566a9bf4f7d5`、`378916e7f7c7`、`d1404bfb91a1` | MachineCombiner、LA464/LA664 调度、kill flags 清理 |
| 分支/控制流 | `fb1a976c1b45`、`6f903bc61e0c`、`0c2baee4686d` | LoopDataPrefetch、`$FCC` 分支、FP 立即数物化 |
| ABI/驱动 | `80542d2d0068`、`aed9799e2f13`、`6b2b66dc23aa` | environment-less triple 的默认 ABI、`--print-imm-hex`、`dbar` 注解 |
| known bits/peephole | `ad13d6f017a2`、`fb3ef8699477`、`7c648c812d93` | BSTRINS/BSTRPICK、`V{ALL,ANY}_*`、`MOVCF2GR` |

这告诉你两件事：

- **活跃面很宽。** 从链接器到向量成本都有近期改动，不是“维护模式”。
- **入门切口很多。** 如果你不想一上来碰 12k 行的 `ISelLowering.cpp`，可以从 `llvm/test/MC/LoongArch` 的 fixup、`lld/test/ELF/loongarch-*` 的 relaxation、或 `LoongArchLateBranchOpt.cpp`（121 行）这种小文件开始。

### 谁在 review

按本树历史提交的作者统计（`git log --format='%an' -- llvm/lib/Target/LoongArch lld/ELF/Arch/LoongArch.cpp clang/lib/Basic/Targets/LoongArch.cpp`）：

```text
145 wanglei        141 Fangrui Song   109 hev
 78 Weining Lu     66 ZhaoQi         64 gonglingqin
 37 Matt Arsenault 34 Zhaoxin Yang   31 tangaac
 26 WANG Xuerui    21 lrzlin         17 Sergei Barannikov
```

这不是“reviewer 名单”，但能看出谁熟悉哪块：`wanglei`/`Weining Lu`/`hev` 在 target 侧出现最多，`Fangrui Song` 在 lld 侧出现最多。PR 描述里用 `[LoongArch]`/`[lld][LoongArch]` 前缀，能让对应的人更容易被路由到。

---

## 0.8 这棵树的贡献地图

| 方向 | 入口文件 | 难度 | 需要的证据 |
|---|---|---|---|
| LLD relaxation/重定位 | `lld/ELF/Arch/LoongArch.cpp` | 中 | `.s` + `yaml2obj` + `llvm-objdump` + QEMU |
| MC fixup/编码 | `MCTargetDesc/LoongArchAsmBackend.cpp`、`MCCodeEmitter.cpp` | 中 | `llvm-mc -show-encoding`、`llvm-readobj -r` |
| 机器 peephole | `LoongArchLateBranchOpt.cpp`（121 行）、`DeadRegisterDefinitions.cpp`（104 行） | 低–中 | MIR + `-run-pass` |
| 调度模型 | `LoongArchSchedLA464.td`/`LA664.td` | 中 | `llvm-mca` + 资源压力 |
| 向量 lowering | `LoongArchISelLowering.cpp` + `LSX/LASX*.td` | 高 | `llc` + `update_llc_test_checks.py` |
| Clang builtins | `clang/lib/CodeGen/Targets/LoongArch.cpp`、`BuiltinsLoongArch*.def` | 中 | `clang -cc1 -emit-llvm` + `llc` |
| 运行时 | `compiler-rt/lib/builtins/loongarch/`、`xray_loongarch64.*` | 中–高 | 交叉编译 + QEMU |

对当前分支，最短的下一步是：

1. 用 `git rebase upstream/main` 把分支移到最新（当前落后 19 条）。
2. 跑 `lld/test/ELF` 全量（不只 LoongArch），确认没有跨架构回归。
3. 补一条 PR 描述：现象、根因、修复、为什么 ABI 允许 hint-first、GNU ld/mold 对照、测试命令。
4. 开 PR，标题建议 `[lld][LoongArch] Don't half-relax TLS LE sequences`，正文引用 `Fixes #239812`。

---

## 0.9 Review checklist：如果我是 reviewer，我会问什么

1. **`tlsLeRelaxedAt` 为什么是单个 `uint64_t` 而不是按 symbol/序列的 map？** 答：合法输入里 TLS LE 序列是固定 3 指令布局；单个“最近起点”足够覆盖当前循环顺序，且避免为每个 section 分配容器。要在 PR 里主动说明这个假设。
2. **如果 `R_LARCH_RELAX` 出现在 `HI20_R` 之前呢？** 答：`HI20` 不会被删，`ADD` 可能被删；`tlsLeRelaxedAt` 只会在 `remove==4` 时更新，消费者仍会被正确改写或保持原样。当前测试没有覆盖这个组合，可以加一条。
3. **如果 `LO12_R` 的 offset 恰好等于上一条序列起点 + 8，但它不属于 TLS LE 呢？** 答：`LO12_R` 类型本身就限定了 TLS LE 语义；非 TLS LE 不会用这个 relocation type。可以在注释里写清楚。
4. **多 pass relaxation 会不会振荡？** 答：`tlsLeRelaxedAt` 在每次 `relax()` 调用内重置；`relaxOnce()` 的 `changed` 仍由 `relocDeltas` 驱动，新增的 consumer 改写只在 producer 已删时发生，不会引入新的 `remove`，因此不改变收敛性。
5. **测试为什么用 `yaml2obj` 而不是 `.s`？** 答：GNU as 的普通 TLS 语法只生成 canonical 顺序，要构造 hint-first 必须用 `.reloc` 或对象变换；`yaml2obj` 能精确控制同 offset 的重定位顺序，是最小、最稳定的复现。
6. **要不要加 release note？** 这是 bug fix，通常不需要；但如果 PR 描述里提到“ABI 允许的输入”，可以在 `lld/docs/ReleaseNotes.rst` 加一句（如果该版本有 LoongArch 段落）。

---

## 0.10 本章证据清单

| 结论 | 命令 | 结果 |
|---|---|---|
| 树身份 | `git status -sb` / `git log -1` | 分支、HEAD、1 ahead / 19 behind |
| 构建缺 target/project | `grep ... CMakeCache.txt` | `X86;AArch64`、`clang` |
| 重配后可用 | `llc --version` / `llvm-config --targets-built` | `loongarch32/64`、`X86 AArch64 LoongArch` |
| 修复测试通过 | `llvm-lit -sv` 3 个 TLS 测试 | 3 passed |
| LoongArch LLD 全绿 | `llvm-lit --filter loongarch` | 48 passed |
| 端到端正确 | cross as + `ld.lld` + `qemu-loongarch64` | 两个 ELF 都是 `addi.d $a0,$tp,0`，exit 0 |
| LLVM 只生成 canonical | `llc` + `llvm-readobj -r` | `LO12_R` 后跟 `RELAX` |
| 上游活跃面 | `git log --oneline -40 -- ...` | 六类主题 |

---

## 0.11 练习

1. **复现修复前的错误。** 在不修改当前分支的前提下，用 `git worktree` 或 `git stash` 造一个“去掉 `relaxConsumer` 逻辑”的临时版本，重编 `lld`，链接 hint-first 对象，确认得到 `addi.d $a0,$a0,0` 且 QEMU SIGSEGV。记录你改的是哪几行。
2. **补一条边界测试。** 构造“`HI20` 的 `RELAX` 在前、`ADD` 的 `RELAX` 在后、`LO12` 的 `RELAX` 在前”的对象，预测输出并验证。
3. **审一遍 `relaxTlsLe()`。** 如果 `val` 超出 12 位，为什么生产者不能被删？把 `isInt<12>(val)` 改成 `isInt<13>` 会发生什么？
4. **看 `relaxOnce()` 的收敛。** 在 `lld` 上加 `-mllvm -debug-only=...` 或读 `Writer.cpp` 的 pass 循环，解释为什么最多 30 pass。

---

## 0.12 下一步

- 想知道这棵树里每个文件在整条编译链的哪一站：读 [Ch 01 后端全景](./01-backend-map.md)。
- 想直接看这个修复涉及的 ISA/ABI 背景：跳到 [Ch 02](./02-isa-subtarget-abi.md) 的 TLS/代码模型小节和 [Ch 12](./12-lld-relocations-and-relaxation.md)。
- 想把这条分支变成 PR：读 [Ch 29](./29-contribution-workflow.md)。
