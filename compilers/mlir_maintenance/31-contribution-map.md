# Ch 31 — 贡献地图：你的第一个补丁该选哪里

> **目标**：把前 30 章的知识折算成一张“可执行的机会清单”。读完你要能做到三件事：用数据而不是感觉挑出一个子系统；判断一个候选点是 1 天、1 周还是 1 个月的活；把它写成上游接受得了的形状（bug fix / 诊断改进 / 规范化 / ODS / 文档）。本章不教你“随便找个 TODO 改掉”，而是教你**为什么某个 TODO 现在值得改**。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，HEAD `e44fff5decc7`。所有统计命令都在这个树上跑，数字会随 rebase 变化——**方法可复用，数字要重算**。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

> **前置**：Ch 03（所有权与 churn）、Ch 13（调试与 reduce）、Ch 29（测试）、Ch 30（PR 流程）。本章假定你已经能跑通 `check-mlir` 的一个子目录。

## 31.1 为什么“贡献地图”这件事要单独一章

新手最容易死在三件事上，而且全都不是编程能力问题：

1. **选了一个没人维护的地方。** 你改得很开心，但没人 review。MLIR 是 `CODEOWNERS` + `Maintainers.md` 驱动的项目，一个方言的 owner 就在 `mlir/Maintainers.md` 里写名字。改一个 owner 长期不在线的方言，你的 PR 会挂在 `waiting-for-author` 里三周。
2. **选了一个“看起来简单、其实不可能”的地方。** 最典型的是 TODO：`grep TODO` 出来的 1,324 条里，绝大部分作者写下它的时候就知道不该现在做——要么缺基础设施（“等 ODS 支持 captures”），要么是设计层面的取舍（“不要在这里做，应该在 bufferization 之前做”）。**TODO 是设计注记，不是任务票。**
3. **选了一个“形状不对”的补丁。** MLIR reviewer 反复看的是四类东西：正确性（verifier + 测试）、IR 兼容性（bytecode 版本、assembly 格式）、API 破坏面、以及“这属于这个方言还是属于通用基础设施”。一个 800 行的“顺手重构”在 MLIR 几乎必然被拆成 6 个 PR 或者直接劝退。

所以“选哪里”不是软技能，它是**对仓库状态的读取能力**：谁在动、谁有 owner、哪里有测试缺口、哪种改动形态被这个社区接受。

## 31.2 先建立直觉：三层漏斗

把“找活干”想成一个三层漏斗：上面是海量信号，中间被结构性条件筛掉，最后只剩几十个你会真的去做的候选。

```text
        ┌──────────────────────────────────────────────────────────┐
 L1     │ 原始信号：TODO(1324) / 失败测试(0) / churn(4060 commits)  │
 信号    │ 未注册方言 / 测试缺口 / 文档错字 / 构建警告                │
        └───────────────────────────┬──────────────────────────────┘
                                    │ 筛：有人在动吗？
        ┌───────────────────────────▼──────────────────────────────┐
 L2     │ 结构性条件                                               │
 过滤    │  · 有 owner（Maintainers.md / CODEOWNERS）                │
        │  · 近 12 个月有 ≥20 次提交（活着）                         │
        │  · 有独立 test 目录（可验证）                              │
        │  · 你不必先改公共 API 才能动手                             │
        └───────────────────────────┬──────────────────────────────┘
                                    │ 筛：值得做吗？
        ┌───────────────────────────▼──────────────────────────────┐
 L3     │ 候选（10~20 个）                                          │
 执行    │  形态: bugfix │ diagnostic │ canonicalize │ ODS │ docs    │
        │  成本: 半天   │ 2 天      │ 3 天        │ 1 周│ 2 小时    │
        │  产出: test + release note + PR 描述                      │
        └──────────────────────────────────────────────────────────┘
```

关键的不对称：**L1 到 L2 会砍掉 90% 的信号，L2 到 L3 又砍掉 80%。** 也就是说，`grep TODO` 出来的 1,324 条里，真正“现在就能动、且有人 review”的可能是 20 条。反过来，最快被合并的补丁往往根本不在 TODO 里——它是你在跑测试、读诊断、试 assembly 格式时撞上的不一致。

## 31.3 用数据找入口：四条必跑查询

以下四条是本章的“体检表”。每次准备开工前重跑一遍，因为它们直接决定你的时间投在哪。

### 31.3.1 churn：谁在动

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git log --since=12.months --pretty=format: --name-only -- mlir/ \
  | grep -v '^$' | sort | uniq -c | sort -rn | head -12
```

实测输出（本树，2025-11 至 2026-11）：

```text
    161 mlir/lib/Target/LLVMIR/Dialect/OpenMP/OpenMPToLLVMIRTranslation.cpp
    105 mlir/include/mlir/Dialect/LLVMIR/NVVMOps.td
     90 mlir/lib/Dialect/LLVMIR/IR/NVVMDialect.cpp
     78 mlir/lib/Dialect/OpenMP/IR/OpenMPDialect.cpp
     66 mlir/lib/Dialect/Vector/IR/VectorOps.cpp
     63 mlir/lib/Dialect/XeGPU/Transforms/XeGPUPropagateLayout.cpp
     62 mlir/lib/Dialect/OpenACC/IR/OpenACC.cpp
     61 mlir/lib/Conversion/AMDGPUToROCDL/AMDGPUToROCDL.cpp
     57 mlir/lib/Dialect/Tosa/IR/TosaOps.cpp
     55 mlir/lib/Dialect/XeGPU/Transforms/XeGPULayoutImpl.cpp
     55 mlir/lib/Dialect/LLVMIR/IR/LLVMDialect.cpp
     55 mlir/lib/Bindings/Python/IRCore.cpp
```

怎么读这张表：

- **前 4 名全是“边界翻译”层**（OpenMP→LLVMIR、NVVM ODS、OpenMP dialect、NVVM dialect）。这告诉你 MLIR 当前的主要增长在 GPU/并行后端，不在核心 IR。
- **churn 高 ≠ 好入口**。`OpenMPToLLVMIRTranslation.cpp` 一年 161 次改动意味着它每天都在被不同的人动，你的补丁会和别人的 rebase 打架，而且 reviewer 对这块的上下文极深。**它是“热点”，不是“缺口”。**
- **真正的缺口在“低 churn + 有 owner + 有测试”的地方**：改了没人抢，reviewer 也熟。

### 31.3.2 子系统活跃度：谁值得投时间

```bash
for p in lib/Conversion lib/Target/LLVMIR include/mlir/Dialect/LLVMIR \
         lib/Dialect/Linalg lib/Dialect/Vector lib/IR lib/Transforms \
         lib/Dialect/OpenMP lib/Dialect/GPU lib/Dialect/Affine \
         lib/Analysis lib/Dialect/SparseTensor lib/Dialect/Transform python; do
  printf "%5d %s\n" "$(git log --since=12.months --oneline -- "mlir/$p" | wc -l)" "$p"
done | sort -rn
```

实测：

```text
  550 lib/Conversion
  317 lib/Target/LLVMIR
  298 include/mlir/Dialect/LLVMIR
  183 lib/Dialect/Linalg
  170 lib/Dialect/Vector
  126 lib/IR
  114 lib/Transforms
   97 python
   95 lib/Dialect/OpenMP
   94 lib/Dialect/GPU
   77 lib/Dialect/Affine
   63 lib/Analysis
   62 lib/Dialect/SparseTensor
   36 lib/Dialect/Transform
```

`lib/Conversion` 一年 550 次提交，是第二名 `lib/Target/LLVMIR`（317）的 1.7 倍。这意味着：**如果你只会一个方向，选 Conversion 的期望回报最高**——它同时满足“有人 review”“有大量 `mlir/test/Conversion` 用例”“改动通常是局部的 pattern”。

反过来 `lib/Dialect/Transform`（36）和 `lib/Analysis`（63）是“小而稳”的区域：改动少、概念密度高、reviewer 少但要求高。适合已经读懂 Ch 11/Ch 28 的人去做“领域内一次到位”的补丁。

### 31.3.3 TODO：1324 条里怎么筛

先确认口径（**`--include` 会显著改变数字，引用时必须写清口径**）：

```bash
grep -rn --include=*.h --include=*.cpp -E 'TODO|FIXME' mlir/lib mlir/include | wc -l
# 1324
grep -rn --include=*.h --include=*.cpp --include=*.td -E 'TODO|FIXME' mlir/lib mlir/include | wc -l
# 1453
```

再按方言分布：

```bash
grep -rn --include=*.h --include=*.cpp --include=*.td -E 'TODO|FIXME' mlir/lib/Dialect \
  | awk -F: '{split($1,a,"/"); print a[4]}' | sort | uniq -c | sort -rn | head -8
```

实测：

```text
    162 Linalg
    141 Vector
    114 Affine
     98 SparseTensor
     42 SPIRV
     41 MemRef
     31 LLVMIR
     27 SCF
```

这张表和 churn 表**几乎不重叠**，这正是机会所在：Linalg/Vector/Affine/SparseTensor 有大量“作者自己标注的、还没做的设计点”，而它们不是每天被抢的热点文件。

但注意筛选顺序：先看 TODO 的**语气**，再看内容。三类语气对应三种命运：

| 语气 | 例子（本树真实行号） | 能不能做 |
|---|---|---|
| “这里先保守，等 X 就放开” | `mlir/lib/Dialect/Vector/IR/VectorOps.cpp:197` `TODO: replace once we have 0-d vectors.` | 不能——0-d vector 支持是跨子系统工程 |
| “这个信息现在丢掉了” | `mlir/lib/Dialect/Linalg/IR/LinalgOps.cpp:156` `TODO: Pass in a proper location here.` | **能**——局部、可测、reviewer 秒懂 |
| “应该在别处做” | `mlir/lib/Dialect/Linalg/IR/LinalgOps.cpp:444` `TODO: Move this to a utility library.` | 谨慎——属于“重构”，要单独立 PR 且先说清搬迁边界 |

**经验法则**：只做那些“改完之后，一个 `.mlir` 测试能证明行为变了”的 TODO。做不到这一点的，说明它不是一个可验证的缺陷。

### 31.3.4 测试缺口：谁没有测试

```bash
for d in mlir/test/Dialect/*/; do
  printf "%5d %s\n" "$(find "$d" -name '*.mlir' | wc -l)" "$(basename "$d")"
done | sort -n | head -12
```

实测：

```text
    2 MPI
    2 PDLInterp
    3 UB
    4 ArmNeon
    4 Func
    4 LLVM
    4 PDL
    5 ArmSVE
    5 Complex
    5 DLTI
    5 Index
    6 MLProgram
```

再对照“哪些 `lib/Dialect` 子目录根本没有对应测试目录”：

```bash
for d in mlir/lib/Dialect/*/; do b=$(basename "$d");
  [ -d "mlir/test/Dialect/$b" ] || echo -n "$b "; done; echo
# NVVM OpenACCMPCommon Utils
```

`lib/Dialect` 46 个目录、`test/Dialect` 45 个目录，差的 4 个里 `NVVM` 的测试其实在 `mlir/test/Dialect/LLVMIR/`（因为它和 LLVMIR 方言同族），`Utils` 不是方言。**所以“没有测试目录”通常不意味着“没测试”，而是“测试挂在别处”**——这个误解会让新手报“XX 方言零测试”这种错结论。

真正可用的测试缺口信号是**比例**：某方言 `lib` 侧 1.2 万行，`test` 侧只有 5 个文件（`Complex`、`Index`、`DLTI` 都是这种形状）。这类地方补一个“边界行为测试”成本低、价值真实，且 reviewer 不会因为“你在动我的实现”而紧张。

### 31.3.5 过期 TODO：最便宜的一类真实缺陷

上面三条查询给出的是“候选”，不是“结论”。动手前必须再验一件事：**这条 TODO 是不是已经过时了。** 本树有一个教科书级的例子。

```bash
sed -n '279,291p' mlir/lib/Dialect/Arith/IR/ArithOps.cpp
```

实测：

```text
/// TODO: disallow arith.constant to return anything other than signless integer
/// or float like.
LogicalResult arith::ConstantOp::verify() {
  auto type = getType();
  // Integer values must be signless.
  if (auto intType = dyn_cast<IntegerType>(getElementTypeOrSelf(type));
      intType && !intType.isSignless())
    return emitOpError("integer return type must be signless");
  // Any float or elements attribute are acceptable.
  if (!llvm::isa<IntegerAttr, FloatAttr, ElementsAttr>(getValue())) {
```

TODO 写在 `279`，而 `285–290` 已经**把它要求的两件事都实现了**；对应的负向测试也早就在：

```bash
sed -n '135,146p' mlir/test/IR/invalid-ops.mlir
```

```text
func.func @verify_fail_1() {
  // expected-error@+1 {{'arith.constant' op integer return type must be signless}}
  %r = "arith.constant"() {value = -1 : si32} : () -> si32
```

**结论：这条 TODO 是陈旧的，删掉它就是一个合法的 NFC 补丁**（`ArithOps.cpp` 减 2 行、无测试改动）。要不要做？它符合“可复现、有 owner、可验证（`check-mlir` 仍全绿）、不动 API、不动 IR 格式、reviewer 3 分钟能判断”全部六问——但价值也仅止于“减少一条误导性注释”。

真正有价值的是它的**反面**：`invalid-ops.mlir:137` 这条测试之所以存在，是因为有人真的实现了那个 verifier 并顺手留下了 TODO。这说明**这个社区里“验证器 + 负向测试”是最容易成形的贡献形态**——你去 `mlir/test/IR/invalid-*.mlir` 翻一遍，凡是“该报错但沉默”的地方，都是一个形态 B 的候选。

**方法论**：把 `grep TODO` 的结果和 `grep -c` 的测试结果对照一遍，先删掉已经实现的，再看剩下的。这一步通常能砍掉 10–20% 的“假候选”，也顺手避免你在 PR 里写出“我实现了 X”而 X 早已存在的尴尬。

## 31.4 五种可交付形态，成本与接受率

MLIR 的补丁不是同质的。下表把 Ch 04–30 的知识映射到具体形态，这是本章最该背下来的一张表。

| 形态 | 典型改动 | 需要先读 | 成本 | 被接受的关键 |
|---|---|---|---|---|
| **A. bug fix** | 一个 fold/canonicalize 返回了错的 value；一个转换 pattern 漏了 operand | Ch 04, 10, 19 | 半天–2 天 | 有最小复现 `.mlir`，且修复不改变其它测试的 CHECK |
| **B. 诊断改进** | `emitOpError` 加上实际值；verifier 提前拦截非法 IR | Ch 07, 16 | 1–2 天 | 用 `--verify-diagnostics` 写负向测试；不引入“假阳性”拒绝合法 IR |
| **C. 规范化 / fold** | 新增 `fold`、`canonicalize` pattern、`ConstantLike` 支持 | Ch 10, 25 | 3–5 天 | 证明不会无限重写；`-test-greedy-patterns` 风格用例；语义等价论证 |
| **D. ODS / assembly** | 新增 op、改 `assemblyFormat`、加 trait | Ch 14, 15, 18 | 1 周 | bytecode/IR 兼容性说明；`test/Dialect/X/ops.mlir` roundtrip |
| **E. 文档 / 工具** | 修 `mlir/docs` 错例、补 `mlir/utils` 脚本、`-verify-diagnostics` 增强 | Ch 01, 29 | 2 小时–1 天 | 与原行为逐字对照；不夹带代码改动 |

实测支撑“小补丁是主流”这一判断：

```bash
git log --since=6.months --grep='\[mlir\]' --shortstat --pretty=format:'%h' \
  | grep -E 'file.*changed' \
  | awk '{f+=$1; i+=$4; d+=$6; n++}
         END {printf "commits=%d avg_files=%.1f avg_ins=%.1f avg_del=%.1f\n", n,f/n,i/n,d/n}'
# commits=1302 avg_files=5.4 avg_ins=150.5 avg_del=39.8
```

```bash
git log --since=6.months --grep='\[mlir\]' --shortstat --pretty=format:'%h' \
  | grep -E 'file.*changed' \
  | awk '{if ($1<=3 && $4+$6<=80) s++} END {print "<=3 files & <=80 lines:", s}'
# <=3 files & <=80 lines: 606
```

**606 / 1302 ≈ 46.5%** 的 MLIR 提交在 3 个文件、80 行以内。也就是说这个项目**将近一半的吞吐来自小补丁**——不是“大重构才是贡献”。你的第一个 PR 应该落在这个 46.5% 里，才符合社区的实际节奏。

再看几个真实的小提交形状（`git log --since=4.months --grep='\[mlir\]' --shortstat`）：

```text
bc0d038ff6a0 [mlir][vector] Fix foldInsertUseChain to return getDest() (#239714)  2 files changed, 7 insertions(+), 7 deletions(-)
fff83e84f4df [mlir][vector] Update doc for vector.broadcast (nfc) (#239707)       1 file changed, 32 insertions(+), 12 deletions(-)
702bd6c1954e [mlir][docs] Fix minor typos in mlir/docs (#239256)                  2 files changed, 2 insertions(+), 2 deletions(-)
99e9d841a8b5 [mlir][wasm] Avoid MSVC parse error in byte sequence helper (#239101) 1 file changed, 7 insertions(+), 2 deletions(-)
d28096faf279 [mlir][OpenACC][NFC] Fix declare operation examples (#234523)        1 file changed, 8 insertions(+), 6 deletions(-)
```

读这五个的形状：**1–2 个文件、个位数到几十行、标题带子系统 tag**。这就是"被接受的补丁"的默认长相。

## 31.5 六问筛选：把一个候选点变成计划

拿到候选（TODO、测试缺口、你撞上的不一致）之后，逐条问。**任何一问不过就换一个**，不要试图“克服”。

1. **可复现吗？** 你能给出一段 30 行内的 `.mlir` + 一条 `mlir-opt` 命令，稳定复现错误行为？做不到 → 先去做复现（Ch 13 的 reduce 就是干这个的）。
2. **有 owner 吗？** `grep -n` 一次 `mlir/Maintainers.md`，确认这个方言/组件在 Core / Egress / Tensor Compiler 三节里有名字。没有 → 你需要先在 PR 描述里说服“为什么这个改动不破坏别人的设计”，成本翻倍。
3. **测试落在哪？** 明确写出你会改哪个已有文件、或新建哪个 `test/Dialect/<X>/<what>.mlir`。写不出来 → 这个改动不可验证。
4. **会不会动公共 API？** 要不要改 `include/mlir/...` 里被下游广泛 include 的头？要的话，考虑能否用一个 `LLVM_DEPRECATED` 过渡或纯内部改动替代。
5. **会不会动 IR 格式？** 改 `assemblyFormat` 或 op 定义 → 需要 bytecode 兼容性说明（Ch 18）；纯 fold/诊断通常不需要。
6. **reviewer 要读多少？** 如果你的 diff 超过 400 行，先拆。MLIR 的 review 带宽是按“一个 reviewer 20 分钟能判断对错”设计的。

## 31.6 源码落点（先看这几处，再决定动手）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
grep -n '^#' mlir/Maintainers.md
grep -n 'MLIR' .github/CODEOWNERS | head
ls mlir/lib/Dialect | wc -l ; ls mlir/test/Dialect | wc -l
```

实测关键行号（本树）：

- `mlir/Maintainers.md:12` — `## Core`：语言核心、IR、Pass、通用转换的所有者。
- `mlir/Maintainers.md:67` — `## Egress`：往 LLVM IR / SPIR-V / GPU 后端走的方言。
- `mlir/Maintainers.md:117` — `## Tensor Compiler`：Linalg / TOSA / bufferization 这一层。
- `mlir/Maintainers.md:52` — Transform Dialect 的三个 owner（`martin-luecke`、`ftynse`、`rolfmorel`），这是“小而稳”区域的典型：owner 明确、改动量小、要求高。
- `mlir/Maintainers.md:142` / `:143` / `:152` — `linalg`(tensor)、TOSA、`bufferization` 的 owner 落在 Tensor Compiler 节。
- `.github/CODEOWNERS` 里 MLIR 段落用的是**组件级**规则（ODS、Properties、Interfaces、TilingInterface、DataFlow），比 `Maintainers.md` 的“方言级”更细；两者冲突时以 CODEOWNERS 的 review 路由为准。

一个常被忽略的事实：`lib/Dialect` 有 46 个目录，而 `mlir-opt --show-dialects` 报告 50 个方言——**方言数和目录数不一一对应**（有些方言共享目录，例如 LLVMIR 目录下挂着 NVVM/ROCDL 等子方言，有些在 `mlir/lib/Conversion` 里只作为 target）。数错这个会让你在 PR 里写出“MLIR 有 46 个方言”这种被秒拒的句子。

## 31.7 可跑实验

以下六个实验按“从读到写”排序。每个都能在 10 分钟内跑完，产出的东西可以直接贴进你的笔记或 PR 描述草稿。

### 实验 1：给你的候选算“活跃度 / 缺口比”

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
D=Linalg
echo "commits12mo: $(git log --since=12.months --oneline -- mlir/lib/Dialect/$D | wc -l)"
echo "todo:        $(grep -rn --include=*.cpp --include=*.h --include=*.td -E 'TODO|FIXME' mlir/lib/Dialect/$D | wc -l)"
echo "tests:       $(find mlir/test/Dialect/$D -name '*.mlir' | wc -l)"
```

实测（Linalg）：

```text
commits12mo: 183
todo:        162
tests:       179
```

同样口径下 SparseTensor：

```text
commits12mo: 62
todo:        98
tests:       121
```

解读：SparseTensor 的 **TODO/commits ≈ 1.58**，Linalg 是 0.89。前者“待做点密度”更高、竞争更少，是更舒服的入口——但前提是你能读懂 sparse 的 lowering 链路（`SparseTensor/Transforms/`）。

### 实验 2：验证一个 TODO 是不是“可验证缺陷”

拿 `LinalgOps.cpp:444` 的“Move this to a utility library”做反例：

```bash
sed -n '440,450p' mlir/lib/Dialect/Linalg/IR/LinalgOps.cpp
```

实测输出显示它后面跟着一组 `getXXX` 辅助函数。结论：**这是重构类 TODO，改完没有任何 `.mlir` 测试能证明行为变化** → 不选。

再拿一个反例——**看起来最像“可做”的 TODO**，`ArithOps.cpp:279`：

```bash
sed -n '279,286p' mlir/lib/Dialect/Arith/IR/ArithOps.cpp
```

它写着“disallow `arith.constant` to return anything other than signless integer or float like”，像是“加个 verifier”的活。但 31.3.5 会实测证明：**下面 285–290 行已经实现了它，负向测试也早就有了。** 所以“读起来最像任务”的那条 TODO，恰恰是最容易让你写出重复补丁的那条。

### 实验 3：用现有负向测试学会“诊断补丁”的形状

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
ls mlir/test/IR/invalid-*.mlir | head -5
sed -n '1,20p' mlir/test/IR/invalid-ops.mlir
$T/bin/mlir-opt --verify-diagnostics mlir/test/IR/invalid-ops.mlir; echo "exit=$?"
```

这里有个**实测出来的坑**：直接跑 `mlir-opt --verify-diagnostics` 会失败，因为 `invalid-ops.mlir` 里混用了 `"affine.apply"` 这类通用形式，需要文件头 `RUN` 行里的全部参数：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
head -1 mlir/test/IR/invalid-ops.mlir
```

```text
// RUN: mlir-opt -allow-unregistered-dialect %s -split-input-file -verify-diagnostics
```

按 `RUN` 行原样跑：

```bash
$T/bin/mlir-opt -allow-unregistered-dialect -split-input-file \
  --verify-diagnostics mlir/test/IR/invalid-ops.mlir; echo "exit=$?"
```

实测输出（每个 `// -----` 分隔的 chunk 打印一行，最后 `exit=0`）：

```text
// -----
// -----
...（共 17 行）
// -----
exit=0
```

而漏掉 `-allow-unregistered-dialect` 时，实测会得到 `exit=1` 和 18 条 `expected error "..." was not produced`——**这正是形态 B 最容易踩的坑**：你以为自己在测诊断，其实在测“unregistered op 不允许”。**这就是形态 B 的模板**：改一句 `emitOpError`，在这类文件里加 3 行断言，PR 就完整了。

### 实验 4：找一个“测试比例失衡”的方言

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
for D in Complex Index DLTI MemRef SCF; do
  lib=$(find mlir/lib/Dialect/$D -name '*.cpp' | xargs cat 2>/dev/null | wc -l)
  t=$(find mlir/test/Dialect/$D -name '*.mlir' 2>/dev/null | wc -l)
  printf "%-10s lib_lines=%-7s tests=%s\n" "$D" "$lib" "$t"
done
```

实测：

```text
Complex    lib_lines=522     tests=5
Index      lib_lines=1118    tests=5
DLTI       lib_lines=857     tests=5
MemRef     lib_lines=12756   tests=34
SCF        lib_lines=15634   tests=47
```

注意这张表的**两个维度要一起看**：`Complex` 只有 522 行实现、5 个测试，比例 1:104；`MemRef` 12,756 行、34 个测试，比例 1:375。后者才是“测试覆盖相对更薄”的那个——**绝对文件数会骗人，比例不会**。顺手也能看出：`tests=` 统计的是 `.mlir` 文件数而不是用例数，一个 `-split-input-file` 文件里可能有 9 个用例（`invalid-ops.mlir` 就是），所以这个指标只用作粗筛。

### 实验 5：确认你的改动不会踩 IR 格式

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git diff --stat
git diff | grep -E '^\+\+\+|assemblyFormat|\.td$' | head
```

如果你看到 `.td` 里的 `assemblyFormat` 被改，就必须在 PR 描述里写清楚“旧 IR 是否仍可解析”。实测一个纯 fold 改动：

```bash
git log -1 --stat bc0d038ff6a0 | tail -6
```

```text
 mlir/lib/Dialect/Vector/IR/VectorOps.cpp   |  2 +-
 mlir/test/Dialect/Vector/canonicalize.mlir | 12 ++++++------
 2 files changed, 7 insertions(+), 7 deletions(-)
```

它只动实现 + canonicalize 测试，**不碰 `.td`、不碰 bytecode** → 这类补丁的兼容性负担最低。注意 `2 files` 里有 1 个是测试文件：**测试不是附赠品，它占 diff 的一半**，这就是形态 A 的“标准配比”。

### 实验 6：写一张自己的“候选表”

```bash
cd /home/ubuntu/Learning-with-Claude/compilers/mlir_maintenance
cat > /tmp/candidates.md <<'TBL'
| 候选 | 形态 | 子系统 | owner? | 测试落点 | 预估 |
|---|---|---|---|---|---|
| Complex 边界 roundtrip 测试 | E | Tensor Compiler | 有 | test/Dialect/Complex/ | 0.5d |
| arith.constant 非 signless 拒绝 | B | Core | 有 | test/Dialect/Arith/invalid.mlir | 1d |
| VectorOps.cpp:197 0-d vector fold | A | Tensor Compiler | 有 | test/Dialect/Vector/ | 3d(风险高) |
TBL
column -t -s'|' /tmp/candidates.md 2>/dev/null || cat /tmp/candidates.md
```

这张表的作用不是“计划”，而是**逼你为每一行填满 5 个字段**。填不满的那一行，就是你还不能动手的那一行。

## 31.8 踩雷集锦

1. **“TODO 数是工作量指标” → 不是。** 1,324 条里大部分是设计注记（“等基础设施”“应该在别处做”）。可执行 TODO 的判据是“一个 `.mlir` 测试能证明行为变化”，不是“代码里写了 TODO”。
2. **“churn 高 = 机会多” → 反了。** `OpenMPToLLVMIRTranslation.cpp` 一年 161 次改动，意味着你每次 rebase 都要重解冲突，且 reviewer 对这段有极强的既有心智模型。热点给的是“学习材料”，不是“入口”。
3. **“没测试目录 = 没测试” → 常见错结论。** `lib/Dialect` 46 个目录 vs `test/Dialect` 45 个目录，差的 `NVVM` 其实测在 `mlir/test/Dialect/LLVMIR/`。报“XX 零测试”前先 `grep -rl` 全树。
4. **“方言数 = lib/Dialect 目录数” → 不成立。** 目录 46、注册方言 50。共享目录的子方言（NVVM/ROCDL 挂在 LLVMIR 体系）会让这个数字对不上。写 PR 前用 `mlir-opt --show-dialects` 核对。
5. **“先做大重构建立威信” → MLIR 不走这条路。** 46.5% 的提交在 3 文件/80 行内。大改动在 MLIR 的默认命运是被要求拆成 NFC（no functional change）+ 行为改动两步。
6. **“文档补丁不算贡献” → 算，但要挑对文档。** `[mlir][docs] Fix minor typos` 这类 PR 存在且被合并，然而价值取决于它修的是“错例”（读者会照抄）还是“拼写”。错例 > 过时 API > 拼写。
7. **“owner 不在就自己顶着” → 会挂三周。** `Maintainers.md` 的三节（Core:12 / Egress:67 / Tensor Compiler:117）就是路由表；没有 owner 的区域，你的 PR 需要额外一位 Core reviewer 背书，预算翻倍。
8. **“PR 越大越显得努力” → 直接劝退。** reviewer 的心智带宽按“20 分钟能判断对错”设计；超过 400 行先拆，超过 800 行默认被要求重做。

## 31.9 本章重点整理

- 找活干是三层漏斗：L1 信号（TODO/churn/测试缺口）→ L2 结构条件（owner、活跃度、可验证）→ L3 候选（形态 + 成本 + 产出）。
- **churn 表告诉你“谁在被改”，TODO 表告诉你“谁在欠账”，两张表几乎不重叠**——交集之外才是你的机会。
- TODO 按语气分三类：等基础设施（不做）、信息丢失（可做）、该在别处做（谨慎，属重构）。
- 可执行性的唯一硬判据：**一个 `.mlir` 测试能证明行为变化**。
- MLIR 的默认补丁形状是“1–2 文件、几十行、标题带子系统 tag”；46.5% 的提交在 3 文件/80 行内。
- 五种形态（bugfix / diagnostic / canonicalize / ODS / docs）对应五套先行章节与接受标准，不要混用。
- 方言数（50）≠ 目录数（46）≠ 测试目录数（45）；写进 PR 前用命令核对。
- 六问筛选（可复现、有 owner、测试落点、公共 API、IR 格式、reviewer 成本）任何一问不过就换候选。

## 31.10 自我检核

- [ ] 我能不看笔记说出“churn 高 ≠ 好入口”的两个理由吗？
- [ ] 给我一段 1324 条 TODO 的 grep 输出，我能用“语气三分类”在 10 分钟内筛出 5 个候选吗？
- [ ] 我知道 `--include=*.td` 会让 TODO 计数从 1324 变成 1453，并且引用时会写清口径吗？
- [ ] 我能为一个候选补丁明确写出“测试落在 `mlir/test/.../<file>.mlir` 的哪一行”吗？
- [ ] 我能在 400 行以内把一个候选拆成 NFC + 行为两步吗？
- [ ] 我知道 `Maintainers.md` 三节的行号（12 / 67 / 117），并能按方言定位 owner 吗？
- [ ] 我知道“改 `assemblyFormat`”会让兼容性负担从 0 变成“必须写 bytecode 说明”吗？
- [ ] 我能解释为什么 `Complex`（1831 行实现 / 5 个测试）比 `Linalg`（高 churn）更适合第一个补丁吗？

## 31.11 延伸阅读

1. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/Maintainers.md` — 读 `## Core`（12）、`## Egress`（67）、`## Tensor Compiler`（117）三节的结构差异；学到“所有权是分层路由而不是名单”；本章 31.6 直接引用这些行号。
2. `/home/ubuntu/llvm/trees/ct-instr-trunk/.github/CODEOWNERS` — 只看 MLIR 段落；学到“组件级 review 路由（ODS/Properties/Interfaces/DataFlow）比方言级更细”；与 31.6 的 owner 讨论互补。
3. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/Contributing.md` — 读 “How to submit a patch” 与 review 期待；学到上游对补丁大小与拆分的要求；对应 31.4 的表。
4. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DeveloperGuide.md` — 读新增 pass/dialect 的检查清单；学到“一个新组件需要哪些文件与测试”；做形态 D 前必读。
5. `/home/ubuntu/Learning-with-Claude/compilers/mlir_maintenance/30-upstream-pr-review.md` — 回看 reviewer 关注点那一节；学到“为什么小补丁更容易过”；与本章 31.4/31.5 互相印证。

下一章是这门课的终点：把本章选出的一个候选，做成一个 PR-ready 的完整交付（problem → root cause → fix → tests → release note）。

→ 下一章：[Final Project — 交出一个 PR-ready 的 MLIR 修复](./final-project-mlir-fix.md)
