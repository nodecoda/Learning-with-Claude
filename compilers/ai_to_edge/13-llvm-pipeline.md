# Ch 13 — LLVM 优化管线：pass 顺序为什么重要

> **目标**：搞清 `llc -O2` 背后跑了什么、为什么 pass 顺序不能随便换，以及一个让无数人困惑的现象——为什么有的 IR 加了 `opt` 也没变化。学完这章，你能手工控制优化管线并预测它的效果。

> **环境**：LLVM 14.0.0（Ubuntu）、`/usr/lib/llvm-14/bin/{clang,opt,llc}`、x86_64 与 aarch64 target

## 为什么需要这个？

直觉上，优化是「越多越好」：跑一次 `-O2` 应该比 `-O1` 更优。但实际工程里的问题不一样：

```
「我改了 IR，为什么产物没变？」
「我加了一个 pass，为什么性能反而下降？」
「-O0 的 IR 和 -O2 的 IR 差这么多，到底哪一步起的作用？」
```

这些问题的答案都在**pass 顺序**和**pass 之间的前提条件**里。

## 先建立直觉

把 LLVM 的 pass 管线想成一条**装配线上的多道加工工序**：

```
原料 IR
  │  ① 规范化（mem2reg：把栈上的变量提到寄存器）
  ▼
SSA 形式（每个变量只赋值一次，分支处用 phi）
  │  ② 局部优化（instcombine：算术化简）
  ▼
简化后的 SSA
  │  ③ 循环优化（loop-vectorize：把循环改成向量操作）
  ▼
向量化的循环
  │  ④ 全局优化（inline、GVN、SCCP）
  ▼
优化后的 IR
  │  ⑤ 下降（SelectionDAG → 机器指令）
  ▼
机器指令
```

关键：**每一道工序都假设前一道已经做完。**

举例：`loop-vectorize` 要求循环是**规范形式**（单入口、单出口、可数迭代）。这个前提是前面的 `loop-simplify` 保证的。你跳过 `loop-simplify` 直接 `loop-vectorize`，它很可能什么都不做。

## 实测：`optnone` 阻止优化

这是本章最有价值的一个发现，也是最常见的困惑来源。

先造一份 `-O0` 的 IR：

```bash
clang --target=aarch64-linux-gnu -O0 -S -emit-llvm t.c -o t_o0.ll
```

`t.c`：

```c
int sum(const int* a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }
```

看它的函数体（实测，节选）：

```llvm
define dso_local i32 @sum(i32* noundef %0, i32 noundef %1) #0 {
  %3 = alloca i32*, align 8
  %4 = alloca i32, align 4
  %5 = alloca i32, align 4
  %6 = alloca i32, align 4
  store i32* %0, i32** %3, align 8
  store i32 %1, i32* %4, align 4
  store i32 0, i32* %5, align 4
  store i32 0, i32* %6, align 4
  br label %7

7:                                                ; preds = %19, %2
  %8 = load i32, i32* %6, align 4
  ...
```

典型 `-O0` 特征：**每个变量都在栈上开一个 `alloca`**，每次读写都 `load`/`store`。

现在跑 `mem2reg`（把栈变量提升为 SSA 值）：

```bash
opt -passes=mem2reg -S t_o0.ll -o t_m2r.ll
grep -c alloca t_m2r.ll
```

**实测结果：4**

一个都没减少。

### 原因：`optnone`

看 `t_o0.ll` 末尾的属性行：

```llvm
attributes #0 = { noinline nounwind optnone uwtable "frame-pointer"="non-leaf"
  "min-legal-vector-width"="0" "no-trapping-math"="true" ...
  "target-features"="+neon,+outline-atomics,+v8a" }
```

**`optnone`**——这个属性告诉 `opt`：**跳过这个函数的全部优化**。

它是 `-O0` 的产物，作用是保证调试体验（变量位置与源码对应）。但如果你把 `-O0` 的 IR 拿去手工 `opt`，你会以为「优化没效果」。

### 去掉它

```bash
sed 's/ optnone//' t_o0.ll > t_o0_nonone.ll
opt -passes=mem2reg -S t_o0_nonone.ll -o t_m2r.ll
echo "alloca: $(grep -c alloca t_m2r.ll)  phi: $(grep -c phi t_m2r.ll)"
```

实测：

```
alloca: 0  phi: 2
```

**4 个 alloca → 0 个，同时生成 2 个 phi**。

phi 是 SSA 的核心：循环回边处，`s` 和 `i` 各自需要一个「来自初始值或上一轮」的合并节点。

> **这条实测解释了一个非常常见的困惑**：为什么 `clang -O0 -emit-llvm` 出来的 `.ll` 文件手工跑 `opt` 没反应。不是你命令写错了，是 IR 里带着 `optnone`。

## 实测：`-O2` 到底做了什么

同一段源码，`-O2`：

```bash
clang --target=aarch64-linux-gnu -O2 -S -emit-llvm t.c -o t_o2.ll
```

对比规模：`-O0` 62 行，`-O2` 89 行。

**`-O2` 的行数更多**。为什么？

看 `-O2` 的产物（实测，节选）：

```llvm
define dso_local i32 @sum(i32* nocapture noundef readonly %0, i32 noundef %1) local_unnamed_addr #0 {
  %3 = icmp sgt i32 %1, 0
  br i1 %3, label %4, label %30

4:                                                ; preds = %2
  %5 = zext i32 %1 to i64
  %6 = icmp ult i32 %1, 8
  br i1 %6, label %27, label %7

7:                                                ; preds = %4
  %8 = and i64 %5, 4294967288
  br label %9

9:                                                ; preds = %9, %7
  %10 = phi i64 [ 0, %7 ], [ %21, %9 ]
  %11 = phi <4 x i32> [ zeroinitializer, %7 ], [ %19, %9 ]
  %12 = phi <4 x i32> [ zeroinitializer, %7 ], [ %20, %9 ]
  %13 = getelementptr inbounds i32, i32* %0, i64 %10
  %14 = bitcast i32* %13 to <4 x i32>*
  %15 = load <4 x i32>, <4 x i32>* %14, align 4, !tbaa !10
  %16 = getelementptr inbounds i32, i32* %13, i64 4
  %17 = bitcast i32* %16 to <4 x i32>*
  %18 = load <4 x i32>, <4 x i32>* %17, align 4, !tbaa !10
  %19 = add <4 x i32> %15, %11
  %20 = add <4 x i32> %18, %12
  %21 = add nuw i64 %10, 8
  %22 = icmp eq i64 %21, %8
  br i1 %22, label %23, label %9
```

读出来的信息：

1. **`%11`、`%12` 是 `<4 x i32>` 向量**——循环被向量化了
2. **一次迭代累加 8 个元素**（两个 4 宽向量，`%19` 和 `%20` 各 4 个）
3. **`%6 = icmp ult i32 %1, 8`**——预处理：元素少于 8 个时跳过向量循环
4. **`%8 = and i64 %5, 4294967288`**——`4294967288 = 0xFFFFFFF8 = ~7`，即「向下取整到 8 的倍数」
5. **`nocapture` / `readonly`** 属性被推导出来

行数增加是因为：**向量化 + 尾部处理 + 前置条件检查**，三样都比原始循环代码长。

> **反直觉但正确**：优化的目标不是「更短的 IR」，是「更快的机器码」。IR 变长是向量化的正常代价。

## pass 顺序为什么重要

用真实数据演示。同一份 `-O1` 的 IR（`loop.c` 里的 `dot`，1024 次乘加）：

```bash
clang --target=aarch64-linux-gnu -O1 -S -emit-llvm loop.c -o loop_o1.ll
grep -c 'vector' loop_o1.ll      # 实测：2
```

只有 2 处含 `vector`（向量类型几乎没用）。

单独跑 `loop-vectorize`：

```bash
opt -passes='loop-vectorize' -S loop_o1.ll -o loop_vec.ll
grep -c 'vector' loop_vec.ll     # 实测：21
```

**从 2 增加到 21**——真的向量化了。

那如果先跑别的呢？

```bash
# 试试：先 mem2reg 再 vectorize
opt -passes='mem2reg,loop-vectorize' -S loop_o1.ll | grep -c vector
```

实测四种组合（数 `grep -c vector` 的命中数）：

| 命令 | `vector` 命中 | 说明 |
|---|---|---|
| `-passes='mem2reg'` | 2 | 只在优化栈变量，没向量化 |
| `-passes='loop-vectorize'` | 21 | 仅向量化 |
| `-passes='mem2reg,loop-vectorize'` | 21 | 和单独向量化结果一样 |
| `-O2`（完整管线） | **19** | **比只做向量化还少** |

最后一行是本节的精华：**完整优化的产物"看起来更少"**。

看 `-O2` 后 `dot` 的循环体（实测）：

```llvm
vector.ph:
  br label %vector.body

vector.body:                                      ; preds = %vector.body, %vector.ph
  %index = phi i64 [ 0, %vector.ph ], [ %index.next, %vector.body ]
  %vec.phi = phi <4 x i32> [ zeroinitializer, %vector.ph ], [ %7, %vector.body ]
  %2 = getelementptr inbounds i32, i32* %0, i64 %index
  %3 = bitcast i32* %2 to <4 x i32>*
  %wide.load = load <4 x i32>, <4 x i32>* %3, align 4, !tbaa !10
  %4 = getelementptr inbounds i32, i32* %1, i64 %index
  %5 = bitcast i32* %4 to <4 x i32>*
  %wide.load1 = load <4 x i32>, <4 x i32>* %5, align 4, !tbaa !10
  %6 = mul nsw <4 x i32> %wide.load1, %wide.load
  %7 = add <4 x i32> %6, %vec.phi
  %index.next = add nuw i64 %index, 4
  %8 = icmp eq i64 %index.next, 1024
  br i1 %8, label %middle.block, label %vector.body, !llvm.loop !18

middle.block:                                     ; preds = %vector.body
  %9 = call i32 @llvm.vector.reduce.add.v4i32(<4 x i32> %7)
  ret i32 %9
}
```

两个关键变化：

1. **尾部处理完全消失**：`icmp eq i64 %index.next, 1024` 是精确比较，因为 1024 是 4 的倍数，而且 `dot` 的边界是编译期常量。只做 `loop-vectorize` 时生成的运行时检查（`%n < 4` 之类）被后续 pass 消掉了。
2. **归约变成了内建函数**：`llvm.vector.reduce.add.v4i32` 替代了手写循环，后续会映射到 `addv`（AArch64）之类的向量归约指令。

> **所以「pass 越多 IR 越长」是错的**。前面几个 pass 生成辅助代码，后面几个 pass 又把它消掉。**真正要看的是最终机器码**，不是中间 IR 的行数或关键词命中数。这正好呼应本章开头那个问题：「为什么我加了一个 pass，产物没变/变差」——单看 IR 判断优化效果，很容易得出反直觉的结论。

### 官方的顺序长什么样

```bash
opt --print-passes 2>&1 | head -20
```

这只是列出可用 pass。真正的默认管线在源码 `PassBuilderPipelines.cpp` 里。你可以让它打印实际跑的 pass：

```bash
opt -O2 -debug-pass-manager -S t_o0_nonone.ll -o /dev/null 2>&1 | grep -oE 'Running pass: [A-Za-z]+' | head -30
```

实测能看到类似 `Running pass: PromotePass`、`Running pass: InstCombinePass`、`Running pass: LoopVectorizePass` 的序列。

> **注意**：如果输入带了 `optnone`，这个序列只会跳过函数——你会看到 pass 运行了但没产生任何变化。这就是「假优化」。

## 三个关键 pass 的作用

| pass | 输入要求 | 做什么 | 不做会怎样 |
|---|---|---|---|
| `mem2reg` | alloca 只被 load/store 直接使用 | 栈变量 → SSA 值 + phi | 后续所有优化都失效（IR 不是 SSA） |
| `instcombine` | 任意 | 算术恒等式化简、常量折叠 | 多余的 `mul 0`、`add 0` 留着 |
| `loop-vectorize` | 循环已 `loop-simplify` | 标量循环 → 向量循环 | 只能用标量指令，慢 4–8 倍 |

`mem2reg` 排在最前面不是偶然：**LLVM 绝大多数优化都要求 SSA 形式**。跳过它，后面全部空转。

## 在 AI 编译流程里的位置

回想 Ch 6：Inductor 生成的是 **C++ 源码**，然后交给 `g++/clang`。所以：

```
Inductor 的"调度"决定  →  生成的 C++ 循环结构
                                  ↓
                    clang -O2 的 pass 管线
                                  ↓
                    向量化、展开、指令选择
```

**两处都在做向量化**。区别：

- Inductor 的向量化是**显式的**（`at::vec::Vectorized<float>`，宽度 8）
- clang 的向量化是**推导的**（`loop-vectorize` 自己决定能否向量化）

两者叠加的效果是：Inductor 已经把循环写成向量友好的形式，clang 再做指令级确认。这也解释了为什么 Inductor 生成的 kernel 通常能被编译得很好——它没有给 clang 出难题。

> **边缘部署的推论**：如果你用交叉编译器（`aarch64-linux-gnu-gcc`）编 MLIR 生成的 IR，**记得给 `-O2`**。不给的话，`llc`/`gcc` 默认的优化级别可能不足以向量化你的循环，性能会差一个数量级。

## 踩雷集锦

1. **拿 `-O0` 的 IR 手工 `opt` 却没变化**：IR 里有 `optnone`。实测：4 个 alloca 一个都没减少；去掉属性后全部消失并生成 2 个 phi。
2. **以为 `-O2` 的 IR 一定更短**：实测 89 行 vs 62 行，因为向量化 + 尾部处理增加了代码。
3. **跳过 `mem2reg` 直接跑优化**：IR 不是 SSA，绝大多数 pass 空转。
4. **以为 `loop-vectorize` 总能成功**：需要规范化循环、可计算的迭代次数、无循环携带依赖。不满足就静默跳过。
5. **混淆「Inductor 的向量化」与「clang 的向量化」**：前者写在生成的 C++ 里（显式 `Vectorized<>`），后者是 IR 层面的自动推导。

## 本章重点整理

- LLVM 的 pass 是**有前提条件**的装配线，顺序不能随意调换。
- `optnone` 属性会让 `opt` 跳过函数——这是「改了 IR 没效果」的头号原因。
- `mem2reg` 是几乎所有优化的前置：栈 → SSA。
- 向量化会让 IR 变长（主体 + 尾部 + 前置检查），但机器码更快。
- AI 编译器（Inductor）和传统编译器（clang）都会向量化，分工不同。

## 自我检核

- [ ] 不看笔记，解释为什么 `-O0` 的 IR 手工 `opt` 没效果
- [ ] 能说出 `-O2` 的行数为什么可能比 `-O0` 多
- [ ] 知道 `mem2reg` 为什么必须排在最前面
- [ ] 看到 `<4 x i32>` 和 `and i64 %x, 4294967288`，能解释向量化与尾部取整的关系
- [ ] 能解释为什么完整 `-O2` 的 `vector` 命中数可能比只跑 `loop-vectorize` 更少
- [ ] 能说出 Inductor 的向量化与 clang 的向量化的区别

## 延伸阅读

### 官方文档

- **[LLVM — Pass Infrastructure](https://llvm.org/docs/Passes.html)**
  - **读哪里**：`Transform Passes` 里的 `mem2reg`、`instcombine`、`loop-vectorize` 说明
  - **为什么值得读**：每个 pass 的前提条件都写在里面，是排查「为什么没生效」的第一手资料
- **[LLVM — Auto-Vectorization](https://llvm.org/docs/Vectorizers.html)**
  - **读哪里**：「Induction Variables」「Runtime Checks」两节
  - **和本章的关联**：解释为什么 `-O2` 那个例子要生成 `icmp ult i32 %1, 8` 的前置检查

### 源码依据

- **[PassBuilderPipelines.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Passes/PassBuilderPipelines.cpp)**
  - **读哪里**：`buildO2Pipeline` / `buildPerModuleDefaultPipeline`
  - **为什么值得读**：官方默认管线顺序是这里的代码，不是文档
  - **注意**：主干与 LLVM 14 可能有差异，按你本机版本对照

### 论文

- **[LLVM: A Compilation Framework for Lifelong Program Analysis & Transformation](https://llvm.org/pubs/2004-01-LLVM-IR.html)** — Lattner & Adve, CGO 2004
  - **核心贡献**：LLVM IR 与 pass 架构的原始论文
  - **读哪里**：Section 4（Pass Manager）
  - **和本章的关联**：pass 之间依赖关系的设计源头

- **[Auto-Vectorization in LLVM](https://llvm.org/devmtg/2013-11/slides/Rotem-Vectorization.pdf)** — Rotem, LLVM Dev Meeting 2013
  - **核心贡献**：向量化器如何判断循环可否向量化
  - **读哪里**：整份 slides（不长）
  - **和本章的关联**：本章 `-O2` 那个例子背后的判定逻辑

### 博客 / 工程文章

- **[Eli Bendersky — Working with LLVM passes](https://eli.thegreenplace.net/2013/02/25/a-crash-course-in-llvm-ir/)**
  - **这系列说什么**：LLVM IR 与 pass 的实操入门
  - **读哪里**：`LLVM IR crash course` 与 `Writing an LLVM pass` 两篇
  - **为什么值得读**：比官方文档更适合第一次动手

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **读哪几章**：第 5 章（SSA）、第 8 章（循环优化）
  - **和本章的关联**：`mem2reg` 与 `loop-vectorize` 的理论基础

IR 优化完了，接下来要交给后端。而后端第一件事是：这段代码要生成给哪个机器？答案写在 triple 和 data layout 里。

→ [Ch 14 Triple、DataLayout 与 TargetMachine](./14-target-description.md)
