# Ch 03 — 维护者、CODEOWNERS 与 churn

> **目标**：能从一个改动路径判断“谁应该 review、测试放哪、最近谁在这块动得最多”，并用 churn 与 TODO 数据挑出高价值、低冲突的贡献点。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`，HEAD `e44fff5decc7`；统计窗口为 2026-11-08 往前 12 个月。

## 03.1 为什么维护者要先看“人”和“频率”

一个补丁被拒，常见原因不是代码错，而是：

- 改错了层，找错了 reviewer；
- 和最近别人的改动冲突；
- 修的是一个已经有人在做的方向；
- 没有覆盖 owner 最在意的边界条件。

MLIR 的 ownership 是公开的，churn 也是可查的。把这两件事放在写代码之前，能省掉大量 review 往返。

本章回答四个问题：

1. **`mlir/Maintainers.md` 的分区怎么读？**
2. **`.github/CODEOWNERS` 比 Maintainers 细在哪？**
3. **过去一年哪些文件/目录最热？热意味着什么？**
4. **怎么用 churn + TODO 挑一个可上手的贡献点？**

## 03.2 先建立直觉：一个补丁的“人际路径”

```
你改的文件
   │
   ├─ 路径匹配 .github/CODEOWNERS？
   │      │ 是 → 对应 @owner（最具体）
   │      └ 否 → 落到 mlir/Maintainers.md 的大类
   │
   ├─ 最近 12 个月谁改得最多？
   │      └→ 历史上下文最多的人（但不一定是 owner）
   │
   ├─ 测试目录在哪？
   │      └→ mlir/test/<对应层>；owner 会先看测试形状
   │
   └→ PR review：owner + 相关子系统维护者 + 社区
```

三层信息叠在一起，才是“该找谁”的完整答案：

- **CODEOWNERS**：最具体的路径所有权，优先。
- **Maintainers.md**：大类/方言的负责人，CODEOWNERS 没覆盖时用。
- **churn**：谁最近在这块动得多，能提供历史背景；但 churn 高不等于 owner。

## 03.3 `mlir/Maintainers.md`：三个大类

文件开头就说明了分区逻辑：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
sed -n '1,35p' mlir/Maintainers.md
```

本机实测（节选）：

```text
## Core
Core components of MLIR, including core IR, analyses and rewriters, fundamental
dialects, build system and language bindings.

- Alex Zinenko
- Jacques Pienaar
- Mehdi Amini

## Egress
MLIR components pertaining to egress flows from MLIR, in particular to LLVM IR.

- Matthias Springer
- Andrzej Warzynski
- Tobias Gysi

## Tensor Compiler
MLIR components specific to construction of compilers for tensor algebra, in
particular for machine learning compilers.

- Renato Golin
- Jacques Pienaar
- Andrzej Warzynski
```

三个大类的边界：

| 大类 | 管什么 | 典型改动 |
|---|---|---|
| Core | IR、analysis、rewriter、基础方言、构建、bindings | `lib/IR`、`lib/Pass`、`lib/Rewrite`、`arith/scf/cf/func` |
| Egress | 从 MLIR 流向 LLVM IR / SPIR-V / GPU / CPU | `Conversion/*ToLLVM`、`Target/LLVMIR`、`gpu/nvvm/rocdl` |
| Tensor Compiler | 张量代数、ML 编译器相关 | `linalg`、`tensor`、`memref`、`vector`、`tosa`、`sparse_tensor` |

行号核对：

```bash
grep -n '^## Core\|^## Egress\|^## Tensor Compiler' mlir/Maintainers.md
```

本机实测：

```text
12:## Core
67:## Egress
117:## Tensor Compiler
```

### 方言级负责人

大类下面还有方言清单，例如：

```text
* ‘arith’ Dialect (core + kuhar + krzysz00)
* ‘linalg’ Dialect (tensor)
* ‘vector’ Dialect (tensor + dcaballe + Groverkss)
* ‘llvm’ Dialect (egress)
* ‘gpu’ Dialect (fabianmcg, krzysz00)
* ‘omp’ Dialect (tblah, skatrak)
```

读法：

- 括号里的 `core` / `egress` / `tensor` 表示“归哪个大类的负责人管”；
- 后面跟 GitHub handle 的，是该方言的具名负责人；
- `unmaintained` 的方言（如 `async`、`quant`）不是“不能改”，而是“没有默认 reviewer，需要自己找愿意看的人”。

维护含义：改 `arith` 的 canonicalization，先找 `kuhar`/`krzysz00` 或 core；改 `linalg` 的 tiling，找 tensor 大类；改 `llvm` dialect 的 translation，找 egress。

## 03.4 `.github/CODEOWNERS`：比 Maintainers 更细

CODEOWNERS 是 GitHub 的自动 review 路由。MLIR 相关段落在文件中部：

```bash
sed -n '54,95p' .github/CODEOWNERS
```

本机实测（节选）：

```text
# MLIR core tablegen.
/mlir/lib/TableGen @krzysz00
/mlir/tools/mlir-tblgen @krzysz00

# MLIR properties.
/mlir/include/mlir/IR/Properties.td @krzysz00

# MLIR Interfaces.
/mlir/include/mlir/Interfaces/TilingInterface.* @MaheshRavishankar @nicolasvasilache
/mlir/lib/Interfaces/TilingInterface.* @MaheshRavishankar @nicolasvasilache
/mlir/include/mlir/Interfaces/ValueBoundsOpInterface.* @matthias-springer
/mlir/lib/Interfaces/ValueBoundsOpInterface.* @matthias-springer
/mlir/**/ValueBoundsOpInterfaceImpl.* @matthias-springer
...
/mlir/**/Interfaces/InferIntRangeInterface.* @krzysz00
/mlir/**/Interfaces/InferIntDivisibilityOpInterface.* @krzysz00
```

CODEOWNERS 的特点：

- **路径 glob 很具体**：`/mlir/**/ValueBoundsOpInterfaceImpl.*` 能覆盖所有 dialect 的实现文件。
- **一个路径可以多个 owner**：TilingInterface 同时有 `@MaheshRavishankar` 和 `@nicolasvasilache`。
- **不是所有目录都有**：没匹配到的路径回落到 Maintainers 的大类。
- **它只影响 review 请求，不是合并权限**：最终仍要按 LLVM 的 review 规则。

维护含义：改 `ValueBoundsOpInterface` 的实现，PR 会自动 request `@matthias-springer`；改 `TilingInterface`，会 request 两位。你不需要在 PR 里手动 @，但要在描述里写清影响面。

## 03.5 churn：过去一年哪里最热

统计 12 个月和 6 个月的提交数：

```bash
git log --since='12 months ago' --oneline -- mlir | wc -l
git log --since='6 months ago' --oneline -- mlir | wc -l
```

本机实测：

```text
4060
1973
```

12 个月 4,060 条，6 个月 1,973 条。平均每月约 330 条；最近半年没有明显减速。

带 `[mlir]` 前缀的比例：

```bash
git log --since='12 months ago' --pretty=%s -- mlir | grep -c '^\[mlir\]'
```

本机实测：

```text
2280
```

2,280 / 4,060 ≈ 56%。前缀是惯例，不是强制；不写前缀不会导致 CI 失败，但会从 `git log --grep='\[mlir\]'` 里消失。

### 文件级 churn

```bash
git log --since='12 months ago' --name-only --pretty=format: -- mlir \
  | grep -v '^$' | sort | uniq -c | sort -nr | head -12
```

本机实测：

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

三个观察：

1. **最热的是“出口”和 GPU/OpenMP。** `OpenMPToLLVMIRTranslation.cpp` 161 次，说明 OpenMP 的 MLIR→LLVM IR 翻译是最活跃的维护面。
2. **ODS 文件也热。** `NVVMOps.td` 105 次、`LLVMDialect.cpp` 55 次；改 op 定义是高频工作。
3. **核心 IR 没有进前 12。** `Operation.cpp` 这类文件改动少但影响大；churn 低不等于不重要。

### 目录级 churn

```bash
git log --since='12 months ago' --name-only --pretty=format: -- mlir \
  | grep -v '^$' | cut -d/ -f1-4 | sort | uniq -c | sort -nr | head -10
```

本机实测：

```text
   2472 mlir/include/mlir/Dialect
   1541 mlir/test/Target/LLVMIR
    495 mlir/test/Dialect/Tosa
    438 mlir/lib/Dialect/XeGPU
    421 mlir/test/Dialect/XeGPU
    416 mlir/lib/Target/LLVMIR
    416 mlir/lib/Dialect/OpenACC
    360 mlir/test/lib/Dialect
    354 mlir/test/Dialect/OpenACC
    340 mlir/test/Dialect/Linalg
```

`include/mlir/Dialect` 的 2,472 是“文件被改次数”的累加，不是文件数；`test/Target/LLVMIR` 高是因为生成的 `.ll` check 文件行数多、更新频繁。

## 03.6 谁在改：review 负载与历史上下文

```bash
git log --since='12 months ago' --pretty=%an -- mlir | sort | uniq -c | sort -nr | head -12
```

本机实测：

```text
    464 Mehdi Amini
    157 Arseniy Obolenskiy
     95 Razvan Lupusoru
     95 Matthias Springer
     93 Jianhui Li
     83 Luke Hutton
     78 Maksim Levental
     68 Krzysztof Drewniak
     67 Sang Ik Lee
     60 lonely eagle
     55 Twice
     53 Andrzej Warzyński
```

读法：

- 这是**提交作者**，不是 reviewer；不能直接等同 owner。
- 高频作者通常对某块的历史最熟，可以在 PR 里引用他们的相关 commit 作为上下文。
- 不要因为某人提交多就私信要求 review；按 CODEOWNERS/Maintainers 路由，PR 描述里写清影响面即可。

## 03.7 TODO/FIXME：1,324 个可上手线索

```bash
grep -rn 'TODO\|FIXME' mlir/lib mlir/include --include='*.cpp' --include='*.h' | wc -l
```

本机实测：

```text
1324
```

这些不是“1324 个 bug”，而是 1,324 个**作者留下的意图标记**。筛选方法：

1. 限定到你熟悉的目录，例如 `mlir/lib/Dialect/Arith`；
2. 排除 `TODO: Remove this`（临时兼容）和 `TODO: Upstream`（等下游）；
3. 找带 issue 编号或明确条件的 TODO；
4. 先写复现，再决定是否值得修。

示例：

```bash
grep -rn 'TODO' mlir/lib/Dialect/Arith --include='*.cpp' | head -10
```

本机实测（节选）：

```text
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:110:  // TODO: Handle index separately; its storage width is not its target width.
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:279:/// TODO: disallow arith.constant to return anything other than signless integer
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:457:  // TODO: Incorporate this check to `FloatAttr::get*`.
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:691:  // TODO: Handle the overflow case.
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:849:  // TODO: divui (x, 0) -> poison. Division by zero is undefined behaviour and
mlir/lib/Dialect/Arith/IR/ArithOps.cpp:901:  // TODO: divsi (x, 0) -> poison. Division by zero is undefined behaviour and
```

这些 TODO 分三类：类型/宽度边界（110、279）、属性校验（457）、溢出/除零语义（691、849、901）。前两类通常可以独立修，第三类牵涉 poison 语义，要拉 core reviewer。

## 03.8 按改动路径选 reviewer

| 改动路径 | 第一 reviewer | 依据 |
|---|---|---|
| `mlir/lib/IR/`、`mlir/include/mlir/IR/` | Core（ftynse/jpienaar/joker-eph） | Maintainers.md Core |
| `mlir/lib/TableGen/`、`mlir/tools/mlir-tblgen/` | `@krzysz00` | CODEOWNERS |
| `mlir/**/TilingInterface.*` | `@MaheshRavishankar`、`@nicolasvasilache` | CODEOWNERS |
| `mlir/**/ValueBoundsOpInterface*` | `@matthias-springer` | CODEOWNERS |
| `mlir/lib/Conversion/*ToLLVM/` | Egress | Maintainers.md Egress |
| `mlir/lib/Target/LLVMIR/` | Egress | Maintainers.md Egress |
| `mlir/lib/Dialect/Linalg/` | Tensor Compiler | Maintainers.md Tensor |
| `mlir/lib/Dialect/GPU/` | `@fabianmcg`、`@krzysz00` | Maintainers.md |
| `mlir/lib/Dialect/OpenMP/` | `@tblah`、`@skatrak` | Maintainers.md |
| `mlir/lib/Dialect/OpenACC/` | `@clementval`、`@razvanlupusoru` | Maintainers.md |

这张表是起点，不是终点：如果 PR 同时改了 IR 和 Conversion，两位 owner 都要看；如果改了接口默认实现，CODEOWNERS 的接口 owner 优先。

## 03.9 可跑实验

### 实验 1：读 Maintainers 分区

```bash
grep -n '^## Core\|^## Egress\|^## Tensor Compiler' mlir/Maintainers.md
```

本机实测：

```text
12:## Core
67:## Egress
117:## Tensor Compiler
```

### 实验 2：看 CODEOWNERS 的 MLIR 段

```bash
sed -n '54,80p' .github/CODEOWNERS
```

本机实测：

```text
# MLIR core tablegen.
/mlir/lib/TableGen @krzysz00
/mlir/tools/mlir-tblgen @krzysz00

# MLIR properties.
/mlir/include/mlir/IR/Properties.td @krzysz00

# MLIR Interfaces.
/mlir/include/mlir/Interfaces/TilingInterface.* @MaheshRavishankar @nicolasvasilache
...
/mlir/**/Interfaces/InferIntRangeInterface.* @krzysz00
```

### 实验 3：12 个月 churn

```bash
git log --since='12 months ago' --oneline -- mlir | wc -l
git log --since='6 months ago' --oneline -- mlir | wc -l
```

本机实测：

```text
4060
1973
```

### 实验 4：前缀比例

```bash
git log --since='12 months ago' --pretty=%s -- mlir | grep -c '^\[mlir\]'
```

本机实测：

```text
2280
```

### 实验 5：热点文件

```bash
git log --since='12 months ago' --name-only --pretty=format: -- mlir \
  | grep -v '^$' | sort | uniq -c | sort -nr | head -8
```

本机实测：

```text
    161 mlir/lib/Target/LLVMIR/Dialect/OpenMP/OpenMPToLLVMIRTranslation.cpp
    105 mlir/include/mlir/Dialect/LLVMIR/NVVMOps.td
     90 mlir/lib/Dialect/LLVMIR/IR/NVVMDialect.cpp
     78 mlir/lib/Dialect/OpenMP/IR/OpenMPDialect.cpp
     66 mlir/lib/Dialect/Vector/IR/VectorOps.cpp
     63 mlir/lib/Dialect/XeGPU/Transforms/XeGPUPropagateLayout.cpp
     62 mlir/lib/Dialect/OpenACC/IR/OpenACC.cpp
     61 mlir/lib/Conversion/AMDGPUToROCDL/AMDGPUToROCDL.cpp
```

### 实验 6：TODO 规模

```bash
grep -rn 'TODO\|FIXME' mlir/lib mlir/include --include='*.cpp' --include='*.h' | wc -l
```

本机实测：

```text
1324
```

## 03.10 源码落点

| 文件 | 行号 | 作用 |
|---|---|---|
| `mlir/Maintainers.md` | 12 / 67 / 117 | Core / Egress / Tensor Compiler |
| `mlir/Maintainers.md` | 30–165 | 方言级负责人清单 |
| `.github/CODEOWNERS` | 57–95 | MLIR tablegen/properties/interfaces 的精细路由 |
| `mlir/docs/ReleaseNotes.md` | 全文件 | 版本行为变更，PR 可能要补 |
| `mlir/README.md` | 全文件 | 官方入口（很短，指向 mlir.llvm.org） |

核对：

```bash
grep -n '^## ' mlir/Maintainers.md | head
grep -n 'MLIR' .github/CODEOWNERS | head
```

## 03.11 踩雷集锦

1. **把 churn 高的人当 owner。** churn 是提交量，不是 review 职责；owner 看 Maintainers/CODEOWNERS。
2. **以为 CODEOWNERS 覆盖所有 MLIR。** 它只覆盖部分精细路径；没覆盖的回落到大类。
3. **在 PR 里手动 @ 一堆人。** GitHub 会按 CODEOWNERS 自动 request；手动 @ 过多会被视为噪音。
4. **只改实现，不改测试目录。** owner 第一句通常问“测试呢”；测试位置本身也是路由信号。
5. **把 `unmaintained` 当“不能碰”。** 它表示没有默认 reviewer，不是禁止修改；但你要自己找到愿意看的人。
6. **用 12 个月 churn 判断“最重要”。** 核心 IR 的 churn 低，但影响面最大；热度只代表变化频率。
7. **把 `[mlir]` 前缀当强制规范。** 它是惯例；不写不会挂 CI，但会降低可检索性。
8. **忽略 release note。** 行为变更（尤其 dialect/ODS）通常要在 `mlir/docs/ReleaseNotes.md` 记一笔；漏了会在 review 被打回。

## 03.12 本章重点整理

- `mlir/Maintainers.md` 分 Core / Egress / Tensor Compiler 三大类，方言条目再给具名 owner。
- `.github/CODEOWNERS` 对 TableGen、Properties、TilingInterface、ValueBounds 等路径做精细路由。
- 12 个月 4,060 条 MLIR 提交，6 个月 1,973 条；`[mlir]` 前缀占 56%。
- churn 热点是 OpenMP→LLVM IR、NVVM、OpenACC、XeGPU、Vector；核心 IR churn 低但影响大。
- TODO/FIXME 1,324 处，是贡献线索，不是 bug 数。
- 选 reviewer 的顺序：CODEOWNERS → Maintainers 大类 → 方言具名 owner → churn 作者提供上下文。

## 03.13 自我检核

- [ ] 能说出 Core / Egress / Tensor Compiler 各自覆盖哪些目录
- [ ] 能给定一个路径，判断 CODEOWNERS 是否会匹配，以及回落到哪个大类
- [ ] 能复现 4,060 / 1,973 / 2,280 三个数字，并解释它们的统计窗口
- [ ] 能解释为什么 `Operation.cpp` churn 低但仍然是最重要的文件之一
- [ ] 能从一个 TODO 判断它是否值得做成 PR
- [ ] 能在 PR 描述里写清“影响面 + 测试位置 + 需要的 reviewer 类型”

## 03.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/Maintainers.md`**
  - 读哪里：12–165 行
  - 能学到什么：MLIR 的所有权分区与方言负责人
  - 和本章关联：03.3 的三张表
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/.github/CODEOWNERS`**
  - 读哪里：57–95 行
  - 能学到什么：GitHub review 路由的具体 glob
  - 和本章关联：03.4 的路径匹配
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/DeveloperPolicy.md`**
  - 读哪里：Maintainers、Review、Commit 三节
  - 能学到什么：LLVM 的 review/合并规则；MLIR 沿用同一套
  - 和本章关联：03.8 的路由只是起点，最终按 DeveloperPolicy
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/ReleaseNotes.md`**
  - 读哪里：当前版本段落
  - 能学到什么：哪些行为变更需要写 release note
  - 和本章关联：Ch 30 会展开
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/README.txt`**
  - 读哪里：整份（很短）
  - 能学到什么：官方文档入口与阅读顺序
  - 和本章关联：后续章节的延伸阅读都从这里挑

地图、环境、所有权都有了。下一章进入 IR 核心：`Operation`/`Block`/`Region`/`Value` 的对象模型，以及为什么一个 `erase()` 的顺序就能让 `mlir-opt` 崩。

→ 下一章：[Ch 04 Operation、Block、Region、Value](./04-ir-object-model.md)
