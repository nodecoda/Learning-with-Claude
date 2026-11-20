# Ch 30 — 正确性 review 二：poison、undef、freeze、溢出、UB、fast-math

> **目标**：建立「合法 IR 但语义错误」这一层的判断力。学完你能在 diff 里识别出：这个变换是否把一个 defined 值变成了 poison、是否在未证明的情况下加了 `nsw`/`inbounds`/fast-math flag、是否把 UB 当成了普通值来处理。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON）。构建缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；行号以 7c7a 为准。

> **依据**：`llvm/docs/LangRef.md` 的 `Undef Values`、`Poison Values`、`Well-Defined Values`、`Fast-Math Flags`、`getelementptr` 各节，源码 `7c7a9072f736`。本章的 `opt` 输出都来自 `/home/ubuntu/llvm/builds/pr-lane`。

## 为什么需要这个？

Ch 29 讲的结构性错误，Verifier 会抓。本章讲的东西，Verifier **抓不到**：

- 一段 IR 可以完全合法，但它的优化结果改变了程序语义。
- 一条 `add nsw` 溢出会产生 poison；poison 在大多数指令里传播，直到遇到 `freeze` 或被用在 UB 上下文。
- 一个 `select` 的两个 arm 里，未被选中的那个即使含 poison，也不一定让结果变成 poison；把它改写成算术，就可能把 poison 引入结果。
- 加一个 `nnan` flag 可以让 `select` 在遇到 NaN 时直接变成 poison。

这些是 LLVM miscompile 的主要来源，也是 reviewer 最值钱的地方。一个 reviewer 能指出「你这个 fold 在 arm 是 poison 时不成立」，比指出十个命名问题更有价值。

## 先建立直觉

LLVM 的「未定义」不是一个概念，而是三层：

```
undef   ：一个值，每次使用可以是任意 bit pattern，甚至每次不同
          例：%x = undef; %a = xor %x, %x  不一定是 0

poison  ：一个值，由错误操作产生；大多数指令会传播它
          例：%p = add nsw i32 INT_MAX, 1; %q = and i32 %p, 0  ; 仍是 poison

UB      ：一个执行，没有语义；优化器可以假设它不发生
          例：br i1 poison, ...；udiv i32 1, 0；load ptr poison
```

三者的关系可以压成一句话：**poison 是「值层面的错误」，UB 是「执行层面的错误」；undef 是「值层面的不确定」，不是错误。** `freeze` 是唯一能把 poison/undef 变成「确定的任意值」的指令。

```
        undef ──freeze──► 确定的任意值（每次 freeze 固定）
          │
          │ 参与大多数指令
          ▼
        poison ──freeze──► 确定的任意值
          │
          │ 用在 UB 上下文（br 条件、除数、load/store 指针…）
          ▼
          UB ──► 优化器可以删除整条路径
```

## 30.1 undef：每次使用都可以不同

LangRef 的例子很反直觉：

```llvm
  %A = undef
  %B = xor %A, %A
  %C = xor %B, %B
```

`%B` 不一定是 0。因为 `undef` 的每次使用可以是不同的 bit pattern，`%A` 在第一个 `xor` 的两个 operand 位置可以是不同的值。LangRef 明确说：

> This example points out that two `undef` operands are not necessarily the same. ... an `undef` "variable" can arbitrarily change its value over its "live range".

这解释了一个常见误区：**不要用代数恒等式去折叠含 `undef` 的表达式**，除非你能证明该恒等式对「每次使用独立取值」仍然成立。

`undef` 和 UB 的区别也很关键：

```llvm
  %A = sdiv undef, %X
  %B = sdiv %X, undef
```

第一个可以折叠成 0（因为 undef 可以取 0，0 除以任何非零值都是 0）；第二个可以假设 undef 取 0，于是触发除零 UB，整条路径可以删掉。LangRef 用这个例子说明「未定义值」和「未定义行为」不是一回事。

## 30.2 poison：错误操作的值，会传播

`poison` 是「错误操作」的结果，设计目的是允许投机执行：指令遇到非法操作数时，不一定立刻 UB，而是返回 poison，让优化器有机会把这条路径删掉。

LangRef 的关键句：

> Most instructions return `poison` when one of their arguments is `poison`. A notable exception is the `select` instruction.

以及：

> It is correct to replace a `poison` value with an `undef` value or any value of the type.

最后这句非常重要，它解释了一个常见困惑：为什么 `opt` 有时会把一条明显会溢出/除零的指令折叠成一个具体数字？因为结果是 poison/UB，而 poison 可以被替换成任意值，UB 允许任意行为。**看到具体数字，不代表原指令没有 poison 语义。**

### select 的例外

`select` 不会因为「未选中的 arm 是 poison」而变成 poison。LangRef 的 select 一节说：

> poison in the non-selected arm does not cause the result to be poison if the value is on the non-selected arm.

但**条件**是另一回事：条件为 poison 时，结果是 poison。本机实测：

```llvm
define i32 @sel(i1 %c) {
  %s = select i1 poison, i32 1, i32 2
  ret i32 %s
}
```

`opt -passes=instcombine -S` 的输出：

```llvm
define i32 @sel(i1 %c) {
  ret i32 poison
}
```

这正是 select 的经典 review 点：如果你把一个 `select` 改写成「先算两个 arm，再用算术/逻辑合并」，你可能把未选中 arm 的 poison 引入了结果。反过来，如果你能让某个 arm 变成 dead，poison 可能不再传播——这类推理必须以 LangRef 的传播规则为准，不能靠直觉。

### poison 什么时候变成 UB

LangRef 列出的「immediate UB」上下文包括：

- `load`/`store` 或其他解引用指令的指针操作数；
- `udiv`/`sdiv`/`urem`/`srem` 的除数；
- `br` 的条件；
- `call`/`invoke` 的 callee；
- 带 `noundef` 属性的参数/返回值位置。

这就是为什么 `br i1 poison, ...` 是 UB，而 `select i1 poison, ...` 只是 poison。Review 时看到「把 poison 值喂给分支/内存/除数」，要立刻标 blocker。

## 30.3 UB：优化器可以假设它不发生

UB 不是「返回一个奇怪的值」，而是「这条执行没有语义」。因此优化器可以：

- 删除整条 UB 路径；
- 把 UB 之前的代码也删掉（如果它只影响 UB 路径）；
- 把 UB 指令替换成任意值。

本机实测：`udiv i32 1, 0` 被 InstCombine 折叠成 `ret i32 poison`：

```llvm
define i32 @div() {
  %z = udiv i32 1, 0
  ret i32 %z
}
```

输出：

```llvm
define i32 @div() {
  ret i32 poison
}
```

这**不**意味着 LangRef 定义「除零 = poison」。除零是 immediate UB；UB 允许任意 refinement，poison 只是其中一种合法选择。Review 时不要把「优化器输出了 poison」反推成「这条指令的语义是 poison」。

### 常见 UB 来源

| 来源 | 例子 | review 时看什么 |
| --- | --- | --- |
| 除零 | `udiv %x, 0` | 是否证明了除数非零？ |
| 移位越界 | `shl i32 %x, 32` | 是否检查了 shift amount < bitwidth？ |
| `nsw`/`nuw` 溢出 | `add nsw i32 INT_MAX, 1` | 加 flag 时是否证明了不溢出？ |
| `inbounds` 越界 | `getelementptr inbounds i32, ptr %p, i64 -1` | 加 `inbounds` 时是否证明在对象内？ |
| null/悬垂解引用 | `load i32, ptr null` | 是否假设了非空/已分配？ |
| `br` 条件 poison/undef | `br i1 poison, ...` | 条件是否可能未定义？ |
| `unreachable` 误用 | 把可达路径标成 unreachable | 支配/可达性证明了吗？ |

## 30.4 freeze：停止传播，给出确定的任意值

`freeze` 的语义：

> If the argument is `undef` or `poison`, `freeze` returns an arbitrary but fixed value. All uses of a value returned by the same `freeze` instruction are guaranteed to always observe the same value.

本机实测：

```llvm
define i32 @frz() {
  %y = freeze i32 poison
  ret i32 %y
}
```

InstCombine 输出：

```llvm
define i32 @frz() {
  ret i32 0
}
```

选择 0 是合法的：`freeze poison` 可以是任意值，InstCombine 选了一个具体值。关键是**同一个 freeze 的所有 uses 看到同一个值**，不同 freeze 可以不同。

Review 时，`freeze` 常出现在两个地方：

1. **修复 UB**：把可能 poison 的条件 freeze 后再 branch，把「未定义分支」变成「非确定性但确定的分支」。
2. **阻止优化**：freeze 会阻断 poison 传播，可能让原本能删的代码保留下来。看到有人加 freeze，要问「这是修复正确性，还是掩盖了上游的 bug？」

## 30.5 flags：加是承诺，删是安全

LLVM 的很多 flag 本质上是「我证明了这个性质」：

| flag | 含义 | 加 flag 需要证明 | 删 flag |
| --- | --- | --- | --- |
| `nsw` | 有符号运算不溢出 | 对所有输入不溢出 | 总是安全（弱化） |
| `nuw` | 无符号运算不溢出 | 对所有输入不溢出 | 总是安全 |
| `exact` | 除法/右移无余数 | 余数为 0 | 总是安全 |
| `inbounds` | GEP 结果在对象内 | 所有下标在界内 | 总是安全 |
| `nnan` | 无 NaN | 输入/结果都不是 NaN | 总是安全 |
| `ninf` | 无 Inf | 输入/结果都不是 Inf | 总是安全 |
| `reassoc` | 允许重结合 | 浮点语义可重排 | 总是安全 |
| `contract` | 允许 FMA 收缩 | 精度语义可接受 | 总是安全 |

Review 规则可以压成一句：**加 flag 是引入新前提，必须给证明；删 flag 是弱化承诺，通常安全。** 一个 diff 如果「顺手」给一条 `add` 加了 `nsw`，却没有说明为什么不会溢出，就是 blocker。

`nnan` 的例子特别值得记：LangRef 明确写

```llvm
%Z = select nnan i1 false, float 0.0, float NaN     ; yields float:poison
```

即使选中的是 `0.0`，`nnan` + 未选中 arm 的 NaN 也能让结果变成 poison。这类 flag 的交互是 review 的高频盲区。

## 30.6 真实案例：SCEV 的 `impliesPoison`

`9ac5159b3c46` 是一个把 poison 语义写进 analysis 的修复：

```text
[SCEV] Account for use flags in impliesPoison. (#244268)

Use flags only hold where the expression is used, so an exit count with
use flags may be poison when expanded in the preheader, even if the
earlier exit is taken first. Conservatively return false from
impliesPoison for non-canonical uses, keeping them in the umin_seq.
```

实现只改了 11 行：

```cpp
-static bool impliesPoison(const SCEV *AssumedPoison, const SCEV *S) {
+static bool impliesPoison(SCEVUse AssumedPoison, const SCEV *S) {
+  // AssumedPoison may be poison due to its use flags alone.
+  if (!AssumedPoison.isCanonical())
+    return false;
+
   // First collect all SCEVs that might result in AssumedPoison to be poison.
```

拆开看：

- `SCEVUse`（`llvm/include/llvm/Analysis/ScalarEvolution.h:124`）把 SCEV 指针和 use-specific no-wrap flags 打包在一起。
- `isCanonical()` 的定义是「所有 operand 都没有 use flags」。
- 如果 `AssumedPoison` 带 use flags，那么「它是 poison」这个前提只在原来的使用点成立；把它提升到 preheader 后，前提不成立，所以不能拿它证明 `S` 是 poison。
- 保守返回 false，代价是保留 `umin_seq`，收益是避免错误地删除 poison 检查。

这个案例的教学价值在于：**poison 语义不只影响指令 fold，也影响 analysis 的推理前提。** Review 一个 analysis 改动时，要问「这个结论在 operand 被移动到另一个位置后还成立吗？」

## 30.7 Review checklist：poison / UB

| 检查项 | 触发信号 | 该问什么 |
| --- | --- | --- |
| poison 引入 | 把 select 改写成算术、合并两条路径 | 未选中 arm 的 poison 会不会传播？ |
| poison 消除 | 把 poison 值替换成常量/另一个值 | 这是 refinement 还是改变了 defined 行为？ |
| UB 上下文 | 分支条件、除数、load/store 指针 | 是否证明了非 poison/非零/非空？ |
| flag 添加 | `nsw`、`nuw`、`exact`、`inbounds`、fast-math | 证明在哪？测试覆盖了溢出/NaN/越界吗？ |
| flag 删除 | 去掉 `nsw` 等 | 是否只是为了绕过失败，而不是修复根因？ |
| freeze | 新增 freeze | 是修复 UB，还是掩盖上游 bug？ |
| assume | 新增 `llvm.assume` | 条件为 false 时是否 UB？调用者能保证吗？ |
| noundef | 新增 `noundef` | 所有调用者都保证非 poison 吗？ |

## 踩雷集锦

1. **把 poison 当成「一个特殊值」而不是「传播的错误」**：`and i32 poison, 0` 仍然是 poison，不是 0。LangRef 专门用这个例子提醒。
2. **以为 `select` 会传播两个 arm 的 poison**：未选中 arm 的 poison 不影响结果；把 select 改成算术才会引入传播。
3. **看到优化器输出具体数字就以为没有 poison**：poison 可以被 refinement 成任意值；`add nsw` 溢出折叠成回绕值就是例子。
4. **把 UB 当成「返回 poison」**：除零是 immediate UB，不是 LangRef 定义的 poison 指令；优化器输出 poison 只是合法 refinement 之一。
5. **加 flag 不证明**：`nsw`/`inbounds`/`nnan` 都是承诺，不是注释。加错就是 miscompile。
6. **用 freeze 掩盖上游 bug**：freeze 能让程序「不 UB」，但可能保留错误语义；review 时要找到真正的 poison 来源。
7. **忽略 `noundef`**：给参数加 `noundef` 等于要求所有调用者保证非 poison；漏掉一个调用者就是 UB。

## 动手练习

1. 把本章的 `poison.ll` 跑一遍 `opt -passes=instcombine -S`，逐条解释四个函数为什么得到那个结果；再用 `opt -passes=verify` 证明它们都合法。
2. 构造一个 `select`，未选中 arm 是 poison，选中 arm 是常量；用 `opt` 观察结果，验证「未选中 arm 的 poison 不传播」。
3. 构造一个 `add nsw` 溢出、再 `and 0` 的 IR，观察 InstCombine 是否保留 poison 语义；解释为什么不能把它折叠成 0。
4. 找一个把 `select` 改写成算术的 InstCombine 代码路径，检查它是否处理了 arm 的 poison。把你的分析写成一条 review comment。
5. 读 `9ac5159b3c46` 的两个测试文件，指出它们分别覆盖了 `isCanonical()` 的哪条路径；如果缺一条，补一个测试草案。
6. 给一条 `getelementptr` 加 `inbounds`，构造一个越界用例，观察优化器/verifier 的行为；解释为什么 `inbounds` 是承诺而不是提示。

## 本章重点整理

- undef 是「每次使用可不同的任意值」；poison 是「错误操作的值，会传播」；UB 是「执行没有语义」。
- `select` 不传播未选中 arm 的 poison，但条件为 poison 时结果是 poison。
- poison 可以被 refinement 成任意值；看到具体数字不代表没有 poison。
- UB 上下文包括分支条件、除数、load/store 指针、callee、`noundef` 位置。
- `freeze` 停止传播并给出确定的任意值；它修复 UB，但可能掩盖上游 bug。
- 加 flag 是承诺，删 flag 是弱化；`nsw`/`inbounds`/`nnan` 加错就是 miscompile。
- analysis 的推理也有 poison 前提；SCEV 的 `impliesPoison` 是真实案例。

## 自我检核

- [ ] 能区分 undef、poison、UB，并各举一个例子
- [ ] 能解释为什么 `xor undef, undef` 不一定是 0
- [ ] 能说出 select 对 poison 的传播规则，以及条件为 poison 时的结果
- [ ] 知道哪些上下文会让 poison 变成 immediate UB
- [ ] 能解释为什么 `freeze poison` 可以被折叠成任意具体值
- [ ] 能判断一个 diff 里加 `nsw`/`inbounds`/`nnan` 是否需要证明
- [ ] 能复述 `impliesPoison` 案例中「use flags 只在使用的点成立」的推理
- [ ] 知道 Verifier 为什么抓不到本章的所有问题

## 延伸阅读

- **[LLVM Language Reference: Undef Values](https://llvm.org/docs/LangRef.html#undef-values)**
  - **读哪里**：整节，尤其是 `xor undef, undef` 和 `sdiv undef, %X` 的例子
  - **为什么值得读**：undef 的「每次使用可不同」是很多反直觉优化的根源
- **[LLVM Language Reference: Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读哪里**：整节，包括 immediate UB 的列表和 `and poison, 0` 的例子
  - **和本章的关联**：30.2/30.3 的全部判据都来自这里
- **[LLVM Language Reference: Fast-Math Flags](https://llvm.org/docs/LangRef.html#fast-math-flags)**
  - **读哪里**：`nnan`、`ninf`、`nsz`、`reassoc`、`contract` 各小节
  - **为什么值得读**：flag 的语义是「承诺」，不是「提示」；Ch 30.5 的表格以此为准
- **[LLVM Language Reference: getelementptr](https://llvm.org/docs/LangRef.html#getelementptr-instruction)**
  - **读哪里**：`inbounds` 的规则，以及 `nusw`/`nuw` 的交互
  - **和本章的关联**：`inbounds` 是最常被误加的 flag 之一
- **[Alive2](https://github.com/AliveToolkit/alive2)**
  - **读哪里**：README 的 `alive-tv` 用法；可以用它验证一个 fold 是否保持 refinement
  - **为什么值得读**：LLVM reviewer 常用它检查 InstCombine 改动的语义正确性；本机未安装，属于可选工具

→ [Ch 31 — 测试 review：这条 lit 测试真的会在改动前失败吗？](./31-test-review.md)
