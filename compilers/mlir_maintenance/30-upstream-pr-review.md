# Ch 30 — 上游 PR：形状、评审与关单

> **目标**：把“代码写对了”变成“补丁进得去”。读完你要能：把一次改动切成上游接受的形状；写出 reviewer 三分钟能判断对错的 commit message 与 PR 描述；预判 reviewer 会问什么并提前回答；知道什么时候该 rebase、什么时候该自己关掉 PR。
>
> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，HEAD `e44fff5decc7`。`gh` 已认证，本章**只用只读命令**（`gh pr view` / `gh api .../comments`），不发评论、不写任何远端状态。
>
> **前置**：Ch 03（所有权与 churn）、Ch 29（测试）。本章假定你已经有一个“能过 `check-mlir`”的补丁。

---

## 30.1 为什么“形状”是独立技能

本树近 6 个月 1,306 条带 `[mlir]` 的提交，平均 **5.4 个文件 / +150.5 / -39.8 行**。但平均值被少数大改动拉高了：其中 **606 条（46.5%）在 3 个文件、80 行以内**，**196 条（15%）只碰 1 个文件**。

也就是说，这个项目的日常吞吐是“小补丁”，而小补丁的接受率取决于三件与算法无关的事：

1. **形状**：diff 有没有混入无关改动、测试占多少、要不要拆。
2. **叙事**：标题是否一行说清、body 是否解释了“为什么现在改”。
3. **路由**：reviewer 选对了吗、有没有先例可引用。

一个正确但形状糟糕的补丁，典型命运是被要求拆成三个 PR——拆分本身要花两天。

## 30.2 先建立直觉：一个 PR 的十道闸门

```text
  想法 ─► ① 复现 ─► ② 单 commit ─► ③ 自查形状 ─► ④ 本地全绿
          (Ch 13)     (no merges)    (≤3 文件/≤80 行)   (check-mlir 子集)
                                                              │
  ┌───────────────────────────────────────────────────────────┘
  ▼
 ⑤ 描述 ─► ⑥ 选 reviewer ─► ⑦ review ─► ⑧ rebase/squash ─► ⑨ merge
 (why/how tested) (CODEOWNERS)   (改 or 说服)   (keep linear)     (squash)
                                                                  │
                                                                  ▼
                                                    ⑩ post-commit review
                                                        └─ 有异议 → revert
```

三个反直觉之处：
- **② 只有一个 commit**。不是“一个 PR 一个 commit”这么简单——review 期间你 amend/force-push，历史永远只有一条。
- **⑦ 不是辩论赛**。reviewer 说“这个 API 名字不好”时，正确回应是改或给出反证，不是解释你为什么这么想。
- **⑩ 是流程的一部分**。`llvm/docs/CodeReview.md:36-50` 明确写：merge 后发现设计问题，社区成员可以要求作者**立刻 revert**，讨论在不阻塞别人的前提下继续。

## 30.3 三个真实提交解剖

选这三个是因为它们代表三种最常见的“小补丁”，而且都在本树里可以直接 `git show`。

### 30.3.1 行为修复：`bc0d038ff6a0`

```bash
git log -1 --format='%h %s%n%n%b' bc0d038ff6a0 | head -12
git show --stat --format='' bc0d038ff6a0
```

```text
bc0d038ff6a0 [mlir][vector] Fix foldInsertUseChain to return getDest() (#239714)

Fixes a regression introduced in #238070, which changed `return
insertOp.getResult()` to `return insertOp.getDest()` in
`foldInsertUseChain`. `dest` is `InsertOp`'s destination vector rather
than its result. Because of this change, folding `vector.insert` ops at
the same position replaced `insertOp` with its destination operand
instead of signaling an in-place fold, dropping the final insertion.

 mlir/lib/Dialect/Vector/IR/VectorOps.cpp   |  2 +-
 mlir/test/Dialect/Vector/canonicalize.mlir | 12 ++++++------
 2 files changed, 7 insertions(+), 7 deletions(-)
```

这个 PR 值得逐句读：

- 标题三件事：子系统 tag `[mlir][vector]`、动作 `Fix`、对象 `foldInsertUseChain`。**一行就是一条 changelog**。
- body 第一句给出**回归来源**（`#238070`），第二句给出**机制**（`dest` 是目的向量不是结果），第三句给出**用户可见后果**（丢掉最后一次 insert）。这是“为什么”三段式，不是“我改了什么”。
- 测试配比 1:1（2 个文件里 1 个是测试），而且测试不是新增文件，是**改已有用例的操作数**——见 30.3.4 的深挖。

### 30.3.2 文档 NFC：`fff83e84f4df`

```bash
git log -1 --format='%h %s%n%n%b' fff83e84f4df | head -10
git show --stat --format='' fff83e84f4df
```

```text
fff83e84f4df [mlir][vector] Update doc for vector.broadcast (nfc) (#239707)

Follow-up for #228727:

* Simplify the formula for computing the source index (no need for a
  special case when source_dim == 1)
* Add more examples.

The examples were created by @yueshengys in #227574, thank you!
 mlir/include/mlir/Dialect/Vector/IR/VectorOps.td | 44 +++++++++++++++++-------
 1 file changed, 32 insertions(+), 12 deletions(-)
```

三个细节：标题里的 `(nfc)` 是**承诺**（no functional change），reviewer 因此只看表述是否正确，写错这个词的代价是信任；“Follow-up for #228727”给出来龙去脉，`@yueshengys` 的致谢说明**归属是社区规范的一部分**；1 个文件 +32/-12 说明文档补丁可以“行数大”，因为它不改变语义。

### 30.3.3 可移植性修复：`99e9d841a8b5`

```bash
git log -1 --format='%h %s%n%n%b' 99e9d841a8b5 | head -8
git show --stat --format='' 99e9d841a8b5
```

```text
99e9d841a8b5 [mlir][wasm] Avoid MSVC parse error in byte sequence helper (#239101)

castIndexSequenceToBytes built its result with std::byte{IS}... as a
non-type template argument pack. 2019 16.11 (MSVC 19.29.30156) fails to
parse a pack expansion of a braced conversion there, which breaks the
whole translation unit.

Use a constexpr variable template byteAt<I> and expand byteAt<IS>...
instead. The resulting sequence is unchanged: one std::byte for each
value 0 through 255.

 mlir/lib/Target/Wasm/TranslateFromWasm.cpp | 9 +++++++--
 1 file changed, 7 insertions(+), 2 deletions(-)
```

关键证据是**“结果不变”**（body 最后一句 `The resulting sequence is unchanged`）。reviewer 无法在 Linux 上复现 MSVC 的解析错误，唯一能接受的理由是“论证了等价性 + 精确到编译器版本”。

### 30.3.4 深挖：那个测试在修好之前为什么是绿的

`bc0d038ff6a0` 的测试改动值得单独看，因为它是“假绿测试”的活标本：

```bash
git show --format='' bc0d038ff6a0 -- mlir/test/Dialect/Vector/canonicalize.mlir | head -32
```

```diff
-//  CHECK-SAME:   %[[VAL:.*]]: f32,
+//  CHECK-SAME:   %[[VAL0:.*]]: f32, %[[VAL1:.*]]: f32, %[[VAL2:.*]]: f32,
-  %v_0 = vector.insert %val, %arg[%pos, 0] : f32 into vector<4x4xf32>
+  %v_0 = vector.insert %val0, %arg[%pos, 0] : f32 into vector<4x4xf32>
+  %v_1 = vector.insert %val1, %v_0[%pos, 0] : f32 into vector<4x4xf32>
+  %v_2 = vector.insert %val2, %v_1[%pos, 0] : f32 into vector<4x4xf32>
```

旧测试里三次 insert **用的是同一个 `%val`**。回归的后果是“丢掉最后一次 insert”，但当三次插入的值相同时，丢不丢在输出上**看不出来**——所以测试一直绿。修复把三个操作数改成不同的值，测试才能区分“保留 v2”和“退回 dest”。

这条经验可以直接迁移：写测试时问自己“**如果实现错了，输出会在哪一行不同？**”答不上来，说明断言不够。

## 30.4 提交形状的统计特征（命中 1,306 条，其中 4 条 diff 为空）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git log --since=6.months --grep='\[mlir\]' --shortstat --pretty=format:'%h' \
  | grep 'changed' | awk '{f+=$1; a+=$4; d+=$6; n++} END {printf "commits=%d avg_files=%.1f avg_add=%.1f avg_del=%.1f\n", n, f/n, a/n, d/n}'
```

```text
commits=1302 avg_files=5.4 avg_add=150.5 avg_del=39.8
```

再切一刀（同一份数据）：`1-file: 196 small: 606`——1 个文件的 196 条，3 文件 / 80 行内的 606 条。

标题形状（同一区间，1,306 条里 1,144 条带二级 tag，87.6%）：

```bash
git log --since=6.months --grep='\[mlir\]' --format='%s' \
  | grep -o '^\[mlir\]\[[a-zA-Z0-9_-]*\]' | sort | uniq -c | sort -rn | head -6
```

```text
    120 [mlir][SPIR-V]
    102 [mlir][tosa]
     66 [mlir][acc]
     63 [mlir][vector]
     38 [mlir][linalg]
     35 [mlir][arith]
```

怎么用这些数字：

- 第一个 PR 应落在 **1–2 文件、几十行**；超过 400 行先想“能不能拆成 NFC + 行为”。
- 标题的二级 tag 决定 reviewer 的过滤器，**tag 写错等于没发给对的人**。
- `avg_add 150.5` 与 `1-file 15%` 同时成立，说明分布是长尾；别用平均值给大补丁找理由。

## 30.5 commit message 与 PR 描述的分工

| 位置 | 长度 | 回答什么 | 谁读 |
|---|---|---|---|
| 标题 | ≤ 80 字符 | 改了什么（一行 changelog） | 所有人、release note 作者 |
| commit body | 3–10 行 | **为什么**要改、机制是什么 | reviewer、六个月后的 `git blame` |
| PR 描述 | 10–40 行 | 上下文、测试怎么跑、替代方案 | reviewer、CI |
| trailers | 1–2 行 | `Assisted-by:`、`Fixes:` | 政策合规、issue 关联 |

把 `bc0d038ff6a0` 的 body 按这个表拆开，就是标准模板：

```text
<一句话：修复了什么回归，来源是哪个 PR>      ← 事实
<一句话：机制层面错在哪>                       ← 诊断
<一句话：用户可见后果是什么>                   ← 影响
<可选：测试怎么覆盖的>                         ← 证据
```

PR 描述则补三件 body 里放不下的东西：

1. **怎么验证的**：贴 `ninja check-mlir` 的失败数、或某个目录的 lit 输出（Ch 29）。
2. **为什么不选另一个方案**：一句“我考虑过在 X 层修，但那会影响 Y”。
3. **不确定性**：把你没把握的地方主动写出来。reviewer 最怕的是“看起来都懂、其实有坑”。

## 30.6 reviewer 真正会说什么

这一节全部是**真实的 inline review comment**（只读 `gh api` 取的，见 30.12 实验 4）。按类型分，因为回应方式不同。

### 30.6.1 命名与 API 一致性

```text
lexi-nadia @mlir/lib/IR/MLIRContext.cpp:299:
  Optional: Should we change `StorageUniquer::disableMultithreading`
  to `StorageUniquer::setThreadSafeLocking`?     joker-eph: Good point, done.
```

reviewer 看到“函数名描述的行为已经变了”，第一反应是改名而不是改注释。注意开头 `Optional:`——这是“建议而非阻塞”，但作者选择了接受。

### 30.6.2 文档缺口

```text
nigham @mlir/include/mlir/IR/MLIRContext.h:50:
  Should we add documentation about enabling locking independently
  of enabling multi-threading here?              joker-eph: Done
```

新增能力必须在**声明处**有文档。MLIR 的 header 同时是用户文档，reviewer 会按“下游 include 这个头能看到什么”来要求。

### 30.6.3 格式与 nit

```text
adam-smnk @mlir/include/mlir/IR/DialectBase.td:60:
  nit: I'd move "Deprecated" to a new line for better readability
joker-eph @mlir/include/mlir/Dialect/AMDGPU/Transforms/Passes.td:32:
  This looks like a very long line that could be wrapped around.
joker-eph @mlir/lib/Conversion/AMDGPUToROCDL/AMDGPUToROCDL.cpp:112:
  Nit: remove braces.
```

`nit:` 前缀的意思是“不阻塞，但改了更好”。正确做法是**顺手改掉并回一句 done**，不要在评论里讨论风格——LLVM 有 `clang-format` 和 CodingStandards，争论没有产出。

### 30.6.4 一致性：代码与 release note 对不上

```text
Muzammiluddin-Syed-ECE @mlir/docs/ReleaseNotes.md:53:
  We say `arch` is added alongside `triple` but then drop `triple`
  as a pass option below.
Muzammiluddin-Syed-ECE @mlir/include/mlir/Dialect/GPU/Pipelines/Passes.h:78:
  IIUC this does not align with the release notes which imply triple
  is kept as a pass option.
```

这是最贵的一类 review comment：**release note 被当作代码的一部分审**。一项改动同时触及 CLI 选项、header 注释和 ReleaseNotes 时，三处描述必须一致（见 30.7）。

### 30.6.5 承认自己记错

```text
joker-eph @mlir/unittests/Dialect/AMDGPU/AMDGPUUtilsTest.cpp:18:
  Isn't there an LLVM macro to handle this? I remember something like this.
krzysz00: Turns out there is
```

reviewer 也会记错。作者的回应是给出事实（有/没有这个宏），而不是“我觉得没有”。**用证据回应，而不是用立场回应**——这是 MLIR review 的基本礼仪。

### 30.6.6 分类与回应策略

| 类型 | 典型措辞 | 你的回应 |
|---|---|---|
| 正确性 | “this drops the last insertion” | 补测试或改实现，别解释 |
| API/命名 | “should we rename …” | 改，或给出“下游已在用”的反证 |
| 文档 | “should we document …” | 改，header 也是文档 |
| 测试 | “can you add a test for …” | 加；加不了就说明为什么 |
| 范围 | “this seems unrelated” | 拆 PR，别在原 PR 里辩解 |
| 风格 | “nit: …” | 改完回 done |

## 30.7 release note 的时机

一个容易误判的事实：**release note 不是每个 PR 都要写**。本树近 12 个月只有 **5 条提交**改过 `mlir/docs/ReleaseNotes.md`：

```bash
git log --since=12.months --oneline -n 5 -- mlir/docs/ReleaseNotes.md
```

```text
8dbdbd3b146d [mlir] Support thread-safe context access without parallel execution (#236551)
55bc2b607674 [mlir][gpu] Split target-specific passes out of MLIRGPUTransforms (#234903)
155462f440ff [mlir] Migrate AMDGPU/ROCDL to targets, not chipset versions (#223563)
c495a9b796c4 [mlir] Enable strict properties in assembly formats by default (#225742)
243f01157719 [mlir][GPU|NVVM] Update the default SM to 7.5  (#177469)
```

对比同期 1,306 条 `[mlir]` 提交：命中率 **0.4%**。判据是“**下游是否会被迫改代码**”：

| 该写 | 不该写 |
|---|---|
| 公共 API 新增/重命名/删除 | fold / canonicalization 结果变化 |
| IR 语法、`assemblyFormat`、bytecode 格式 | 诊断措辞改进 |
| CLI 选项改名（`chipset` → `arch`） | 内部重构、NFC |
| 默认行为变化（strict properties 默认开启） | 测试补充 |
| 依赖最低版本（Python/编译器） | 性能微调 |

看一个“该写”的例子：`8dbdbd3b146d` 的 diff 是 7 个文件、+212/-15，其中 `mlir/docs/ReleaseNotes.md` 占 +23 行，`mlir/unittests/IR/MLIRContextTest.cpp` 占 +126 行。**release note 与单测在同一个 PR 里**，因为它们证明的是同一件事：新 API 存在且行为如描述。

写法建议：release note 给**迁移前后的代码片段**，而不是形容词——上面那条就贴了三行 API 调用。

## 30.8 rebase、squash 与格式化

`llvm/docs/Contributing.md` 的要求是硬的：

```text
47:* have a single commit, up-to-date with the upstream `origin/main` branch, and don't have merges.
48:* include a small unit test
50:* not contain any unrelated changes
51:* be an isolated change. Independent changes should be submitted as separate patches ...
```

三条实操：

### 30.8.1 用 fixup + autosquash 维持单 commit

review 期间改代码，不要新增 “address review comments” 提交。正确做法：

```bash
git commit --fixup=<被修的 commit>
GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash HEAD~3
```

实测（`/tmp/rbdemo2` 里演练）：

```text
--- before ---
04a0f4b fixup! add g
bb0e5cf add g
0960141 tweak f
cfb6591 add f
--- after ---
0ebb4a9 add g
0960141 tweak f
cfb6591 add f
```

`GIT_SEQUENCE_EDITOR=:` 用 `true` 命令代替编辑器，让 `--autosquash` 自动执行——这是脚本里安全 squash 的标准写法。

### 30.8.2 只格式化你改的行

```bash
git clang-format HEAD~1
```

这个脚本只格式化**与 `HEAD~1` 相比有差异的行**，不会重排整个文件（需 `clang-format` 在 PATH，或 `git config clangFormat.binary <path>`）。实测 `--diff`（只看不改）：

```bash
cd /tmp/cfdemo1 && git clang-format --diff HEAD~1
```

```diff
@@ -1,2 +1,2 @@
 int  addOne( int a ){return a+1;}
-int  addTwo( int a ){return a+2;}
+int addTwo(int a) { return a + 2; }
```

第一行没被动——**它不在本次 diff 里**。这正是你要的行为；反之，如果你手动对整文件跑 `clang-format`，会得到几百行无关改动，reviewer 只会要求你撤回。

### 30.8.3 线性历史

merge  commit 在 LLVM 是被拒绝的（`Contributing.md:145` 的 linear-history policy）。所以：

- 从 `upstream/main` rebase，而不是 `merge upstream/main`；
- PR 合并用 “Squash and merge”（`Contributing.md:141`）；
- 不要 rebase 别人的分支，也不要在 review 中途把 main merge 进来。

## 30.9 什么时候该关掉自己的 PR

关 PR 不是失败，是维护者技能的一部分。五种情形，判断标准各不相同：

| 情形 | 识别信号 | 处理 |
|---|---|---|
| 上游已修 | `git log -S '<你的符号>'` 找到同义提交 | 关，贴链接，说“已由 #NNN 解决” |
| 设计被否 | reviewer 说“this needs an RFC” | 关（或转 draft），去开 RFC/issue |
| 要求拆分 | “please split the NFC part out” | 拆完重开两个 PR，别在原 PR 里硬改 |
| 自己的前提错了 | 复现脚本在新 HEAD 上不失败 | 关，附“我误判了 X”的一句 |
| 三周无 reviewer | 无 owner、无回复 | 先 ping 一次（一周一次，`Contributing.md:120`），再无响应就关 |

**关的方式**：写一句结论 + 替代品链接。静默关闭会让后来 `git log` 搜到这条记录的人重复你的工作。

顺带一个数据：本树近 6 个月有 **16 条** `Revert ... [mlir]`。revert 在上游是正常流程，`llvm/docs/CodeReview.md:36-50` 的原文是“erring on the side of the developer asking for more review prevents any lingering disagreement over code in the tree”。**被 revert 不等于你错了**，等于“设计讨论还没结束”。

## 30.10 AI 工具政策：能做什么、必须声明什么

`llvm/docs/AIToolPolicy.md` 很短（193 行），但每条都影响你能不能提交：

```text
6:  ... there must be a **human in the loop**.
7:  **Contributors must read and review all LLM-generated code or text before they
8:  ask other project members to review it.**
15: ... **start with small contributions** ...
26: ... use a commit message trailer like Assisted-by: <name of code assistant>.
51: ... automated review tools that publish comments without human review are not allowed.
56: AI tools must not be used to fix GitHub issues labelled good first issue.
```

四条对本章的直接约束：

1. **你必须能回答 review 里的每个问题**——包括“这行为什么这么写”。答不上来，说明你还没准备好提交。
2. **PR 描述自己写**（政策原文建议用工具只做翻译/润色）。描述是 reviewer 判断你理解程度的第一手材料。
3. **声明工具使用**：substantial 的生成内容要加 `Assisted-by:` trailer。
4. **不要自动发评论**：任何“agent 自动回复 review”的做法都违反政策。

## 30.11 源码落点

```bash
grep -n "single commit\|include a small unit test\|isolated change\|Squash and merge\|linear-history" llvm/docs/Contributing.md
grep -n "post_commit_review\|erring on the side" llvm/docs/CodeReview.md
grep -n "human in the loop\|Assisted-by\|good first issue" llvm/docs/AIToolPolicy.md
grep -c "mlir" .github/CODEOWNERS
```

- `llvm/docs/Contributing.md:44-51` — 五条硬要求（单 commit、带测试、无无关改动、可独立）。
- `llvm/docs/Contributing.md:120/141-145` — ping 的频率（一周一次）、squash merge 与线性历史。
- `llvm/docs/CodeReview.md:36-50` — post-commit review 与 revert 的正当性。
- `llvm/docs/CodeReview.md:86` — 需要 RFC 的情形（跨项目的接口改动）。
- `llvm/docs/AIToolPolicy.md:6/7/15/26/51/56` — 本章 30.10 的五条。
- `.github/CODEOWNERS:58-83` — MLIR 的组件级路由（TableGen、TilingInterface、ValueBounds、DataFlow…）。
- `mlir/Maintainers.md:12/67/117` — Core / Egress / Tensor Compiler 三节（Ch 31 详用）；`mlir/docs/ReleaseNotes.md` 全文 163 行，`## LLVM 24` 是当前段。

## 30.12 可跑实验

### 实验 1：量一下自己的 diff 形状

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git apply --stat /tmp/fp2/fix.patch
git apply --numstat /tmp/fp2/fix.patch
```

```text
 2 files changed, 16 insertions(+), 8 deletions(-)
12	4	mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
4	4	mlir/test/Dialect/Tensor/invalid.mlir
```

`--numstat` 的 `12/4` 比柱状图更有用：**实现 12 行、测试 4 行**，比例健康；若实现 200 行、测试 0 行，先补测试。

### 实验 2：读三个真实提交的完整 diff

```bash
for c in bc0d038ff6a0 fff83e84f4df 99e9d841a8b5; do
  git show --stat --format='%h %s' $c | head -5
done
```

```text
bc0d038ff6a0 VectorOps.cpp 2 +-  /  canonicalize.mlir 12 +-
fff83e84f4df VectorOps.td 32 +, 12 -
99e9d841a8b5 TranslateFromWasm.cpp 7 +, 2 -
```

### 实验 3：看一个真实 PR 的描述（只读）

```bash
gh pr view 239714 --repo llvm/llvm-project --json title,state,mergedAt,files,body
```

```json
{"body":"Fixes a regression introduced in #238070, ... We also add a test to prevent this regression from happening again. \n\ncc @banach-space","files":[{"path":"mlir/lib/Dialect/Vector/IR/VectorOps.cpp","additions":1,"deletions":1,"changeType":"MODIFIED"},{"path":"mlir/test/Dialect/Vector/canonicalize.mlir","additions":6,"deletions":6,"changeType":"MODIFIED"}],"mergedAt":"2026-11-07T19:30:23Z","state":"MERGED","title":"[mlir][vector] Fix foldInsertUseChain to return getDest()"}
```

注意 `cc @banach-space`：PR 描述里的 `cc` 是**手动路由**，在 CODEOWNERS 没覆盖到、或你想让某个上下文专家看一眼时使用。

### 实验 4：读真实 review comment（只读）

```bash
gh api repos/llvm/llvm-project/pulls/236551/comments \
  --jq '.[] | "\(.user.login) @\(.path):\(.line): \(.body|gsub("\n";" ")|.[0:160])"'
```

```text
lexi-nadia @mlir/lib/IR/MLIRContext.cpp:299: Optional: Should we change `StorageUniquer::disableMultithreading` to `StorageUniquer::setThreadSafeLocking`?
joker-eph @mlir/lib/IR/MLIRContext.cpp:299: Good point, done.
nigham @mlir/include/mlir/IR/MLIRContext.h:50: Should we add documentation about enabling locking independently of enabling multi-threading here?
joker-eph @mlir/include/mlir/IR/MLIRContext.h:50: Done
```

再看一个“release note 与代码对不上”的：

```bash
gh api repos/llvm/llvm-project/pulls/223563/comments \
  --jq '.[] | select(.path|test("ReleaseNotes|Passes.h")) | "\(.user.login) @\(.path):\(.line): \(.body|gsub("\n";" ")|.[0:200])"'
```

```text
Muzammiluddin-Syed-ECE @mlir/docs/ReleaseNotes.md:53: We say `arch` is added alongside `triple` but then drop `triple` as a pass option below.
Muzammiluddin-Syed-ECE @mlir/include/mlir/Dialect/GPU/Pipelines/Passes.h:78: IIUC this does not align with the release notes which imply triple is kept as a pass option.
```

这两个 API 都是只读；**不要**在这条命令后面接 `gh pr comment`。

### 实验 5：fixup + autosquash 演练

```bash
mkdir -p /tmp/rbdemo2 && cd /tmp/rbdemo2 && git init -q . \
  && git config user.email a@b.c && git config user.name demo \
  && printf 'a\n' > f.txt && git add f.txt && git commit -qm "add f" \
  && printf 'b\n' >> f.txt && git commit -qam "tweak f" \
  && printf 'typo\n' > g.txt && git add g.txt && git commit -qm "add g" \
  && printf 'typo fixed\n' > g.txt && git commit -qam "fixup! add g" \
  && GIT_SEQUENCE_EDITOR=: git rebase -q -i --autosquash HEAD~3 \
  && git log --oneline
```

```text
0ebb4a9 add g
0960141 tweak f
cfb6591 add f
```

四个提交变三个，`fixup!` 被吸收进 `add g`，历史保持一条线。

## 30.13 踩雷集锦

1. **标题里写 “Fix bug” 就够。** → 上游按 tag 过滤：没有 `[mlir][subsystem]` 的标题，很多 reviewer 根本看不到（1,144/1,306 带二级 tag）。
2. **commit body 写“改了什么”。** → reviewer 从 diff 就能看出改了什么；body 要写**为什么现在改**、**机制错在哪**（看 30.3.1 的三段式）。
3. **review 期间新增 “address comments” 提交。** → 违反单 commit 要求；用 `--fixup` + `--autosquash`。
4. **`git merge upstream/main` 保持最新。** → LLVM 拒绝 merge commit（linear-history policy），必须 rebase。
5. **对整文件跑 `clang-format`。** → 几百行无关 diff；用 `git clang-format HEAD~1` 只格式化改动行。
6. **每个 PR 都写 release note。** → 12 个月只有 5 条提交改 ReleaseNotes（0.4%）；判据是“下游是否被迫改代码”。
7. **reviewer 提意见就解释自己的思路。** → 先判断类型：正确性给证据、命名直接改、范围问题拆 PR。解释只对“我考虑过但…”，不对“你错了”。
8. **PR 没人理就一直等。** → 一周 ping 一次是上限；三周无 owner 的补丁应当关闭或转 draft，而不是挂着占位。

## 30.14 本章重点整理

- MLIR 的默认补丁形状：1–2 文件、几十行、标题带子系统 tag；46.5% 的提交在 3 文件/80 行内。
- commit body 回答“为什么”，PR 描述回答“怎么验证 + 为什么不选别的”，标题是一行 changelog。
- 测试配比是形状的一部分：行为修复通常“1 实现 + 1 测试”；NFC 不应带新测试；reviewer 的 comment 分六类，回应策略各不相同。
- release note 只在“下游被迫改代码”时写；代码、header 注释、release note 三处必须一致。
- 单 commit + rebase + squash merge 是硬要求；`--fixup`/`--autosquash` 是维持它的工具。
- `git clang-format HEAD~1` 只格式化改动行；整文件格式化会制造无关 diff。
- AI 政策：human in the loop、自己写描述、`Assisted-by:` 声明、不自动发评论、不碰 `good first issue`。

## 30.15 自我检核

- [ ] 我能给一个补丁写出“事实 / 诊断 / 影响”三段式 body 吗？
- [ ] 我知道 400 行以上的补丁应该怎么拆（NFC + 行为）吗？
- [ ] 我能把一条 reviewer 的 “nit:” 和一条 “this is wrong” 区分开并分别回应吗？
- [ ] 我知道什么改动必须写 release note 吗？能用 `--fixup` + `--autosquash` 把 4 个提交压成 1 个吗？
- [ ] 我知道 `git clang-format HEAD~1` 与 `clang-format -i *.cpp` 的差别吗？
- [ ] 我能说出三个“应该关掉自己 PR”的信号吗？
- [ ] 我知道 LLVM 对 AI 生成内容的四条硬约束吗？

## 30.16 延伸阅读

1. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/Contributing.md` — 读 44–133（How to Submit a Patch）与 134–175（commit/merge）；学到五条硬要求与线性历史；对应 30.8。
2. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/CodeReview.md` — 读 20–60（pre/post-commit review 与 revert）；学到“revert 是正常流程”；对应 30.9。
3. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/AIToolPolicy.md` — 全文 193 行，重点 1–70；学到 human-in-the-loop 与 `Assisted-by:`；对应 30.10。
4. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/ReleaseNotes.md` 与 `.github/CODEOWNERS`（58–95）— 学到 release note 粒度与“谁会被自动拉进来”；对应 30.7/30.6。

PR 是“把改动翻译成社区能接受的形式”。下一章把视角拉远：**在 8,000 个文件里，怎么挑出值得你花两周的那一个**。

→ 下一章：[Ch 31 贡献地图：你的第一个补丁该选哪里](./31-contribution-map.md)
