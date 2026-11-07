# Final Project — 从 issue 到可提交的上游 PR

> 这是本课的毕业作业。目标不是“写出很多代码”，而是产出一份**可提交、可 review、可合并**的 LoongArch 修复。
>
> 建议投入：2–4 周。最终交付物是一个分支 + 一份 PR 描述 + 一份证据记录。

---

## F.0 选题

从下面三类里选一个，**不要选 LLDB 支持、不要选需要新 ABI 的 RFC**：

| 类型 | 特征 | 起始动作 |
|---|---|---|
| A. correctness / miscompile | 有 reduced testcase，期望输出明确 | 复现 → 定位 → 最小修复 |
| B. crash / assertion | assertions 构建能打出来，栈可读 | `-debug-only` + `-stop-after` 二分 |
| C. missed optimization | 有 IR 对照，能算指令数/周期 | `llvm-mca` + `-stats` 给证据 |

选题时先回答三个问题（写进 `notes.md`）：

1. 错的产物是哪一层？（IR / asm / .o 重定位 / 最终 ELF）
2. 最小复现命令是什么？
3. “正确”的期望输出是什么？谁规定的（ABI 文档 / 既有测试 / 另一架构对照）？

---

## F.1 里程碑 1：复现（第 1 周）

交付：

- [ ] 一个最小输入（`.ll` / `.s` / `.mir` / `yaml2obj` 的 `.s`）
- [ ] 一条可复制粘贴的命令
- [ ] 实际输出 vs 期望输出（贴原文）

验收：**别人不看你的解释，照着命令就能看到同样的差异。**

模板：

```markdown
## Repro
$ llc -mtriple=loongarch64 -O2 repro.ll -o - | grep ...

Actual:
  ...

Expected:
  ...
```

---

## F.2 里程碑 2：定位（第 1–2 周）

要求写出“证据链”，不是“我觉得”：

- [ ] `-debug-pass=Structure` 确认相关 pass 在流水线里的位置
- [ ] `-stop-before/-stop-after` 找到**第一个**产生差异的 pass
- [ ] 指出具体文件:行号（例如 `LoongArchLateBranchOpt.cpp:63-78`）
- [ ] 说明“为什么这里会这样”，并给出一个反例证明你的理解

模板：

```markdown
## Root cause
Pass X 在 A 条件下把 ... 变成 ...，但 B 条件下 ...
源码：llvm/lib/Target/LoongArch/X.cpp:NN-MM
反例：把输入改成 ...，pass 不再触发，证明条件是 ...
```

---

## F.3 里程碑 3：修复（第 2–3 周）

硬约束：

- [ ] 只改必要的文件；能 20 行解决就不要 200 行
- [ ] 不改变无关语义；`relaxable()` 这类共享函数慎改
- [ ] 新逻辑要有注释说明**不变量**（一句话）
- [ ] `git clang-format` 干净

自检问题（写进 notes）：

1. 这个修复依赖“输入总是 X”的隐含假设吗？
2. 另一条路径（LA32 / large code model / PIC / `-mno-relax`）会不会被误伤？
3. 如果 reviewer 要求“改成更通用的写法”，你有备选方案吗？

---

## F.4 里程碑 4：测试（第 3 周）

- [ ] 回归测试能表达 bug 的触发条件（必要时用 `yaml2obj`）
- [ ] 测试同时覆盖“修复前会错、修复后正确”的输入
- [ ] 如果修的是顺序相关 bug，测试必须有两种顺序
- [ ] 跑过相邻子集：`llvm/test/CodeGen/LoongArch`、`llvm/test/MC/LoongArch`、`lld/test/ELF --filter=loongarch`

命令：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llvm-lit -q <你的测试文件>
$T/bin/llvm-lit -q llvm/test/CodeGen/LoongArch
$T/bin/llvm-lit -q llvm/test/MC/LoongArch
$T/bin/llvm-lit -q lld/test/ELF --filter=loongarch
```

---

## F.5 里程碑 5：提交（第 4 周）

### commit message 模板

```text
[组件][LoongArch] 祈使句，72 列内

问题：ABI/规范怎么说，现状错在哪，可观测后果是什么。
修复：一句话策略，强调不变量。
测试：为什么这样测，为什么不能用更简单的工具。

Fixes #NNNNN
Assisted-by: <工具名>（如果用 AI 辅助）
```

### PR 描述模板

```markdown
### Problem
...

### Fix
...

### Test
...

Fixes #NNNNN
```

### 提交前 checklist

- [ ] `git fetch upstream && git rebase upstream/main`
- [ ] rebase 后重跑目标测试与相邻子集
- [ ] `git log -1 --format=%B` 检查尾注
- [ ] `git diff --stat` 只含预期文件
- [ ] 没有把格式改动混进功能改动（NFC 另开一个提交）

---

## F.6 评分标准（自评）

| 维度 | 不合格 | 合格 | 优秀 |
|---|---|---|---|
| 复现 | 只有描述 | 有命令和输入 | 输入最小化，能区分正确/错误 |
| 定位 | “可能在这个文件” | 指到 pass 与行号 | 有反例验证理解 |
| 修复 | 改动大、依赖假设 | 小且局部 | 不变量清晰，有备选方案 |
| 测试 | 只测 happy path | 覆盖触发条件 | 测试能独立证明 bug 存在 |
| 提交 | 标题含糊 | 四段结构完整 | reviewer 一轮过 |

---

## F.7 交付物清单

```text
notes.md                 # 复现命令、根因、证据
repro/                   # 最小输入（可含 yaml）
patch/                   # git format-patch 产物（可选）
pr-description.md        # 可直接粘贴到 GitHub
evidence.txt             # 关键命令输出（diff、lit 结果）
```

把这五份东西放进一个目录，你就有了一个**可复用的贡献记录**。申请 commit access 时，`DeveloperPolicy.md:536` 要的就是这类证据。

---

## F.8 参考案例

本课的 #239812 就是一个完整样例：

- 复现：#239812 的 YAML（marker 在前）
- 定位：`relaxable()` 只看 `relocs[i+1]`
- 修复：`tlsLeRelaxedAt` 游标 + `offset + 8`
- 测试：`lld/test/ELF/loongarch-tls-le-relax-order.s`（95 行，`yaml2obj`）
- 提交：Ch 29 的四段 message + PR 草稿

照着这个骨架做你的第一个 PR。做完之后，回到 Ch 30 的半年节奏表，开始第二个。
