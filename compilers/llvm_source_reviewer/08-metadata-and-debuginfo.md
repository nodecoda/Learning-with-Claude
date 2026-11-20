# Ch 8 — `Metadata` 与调试信息：`MDNode`、`DILocation`、`!tbaa`、`assume`

> **目标**：把 metadata 从「IR 旁边的注释」还原成一套有类型、有存储策略、会被优化器消费的旁路对象。学完这章，你能解释 `MDNode`/`MDString`/`MDTuple` 与 `Value` 的边界，读懂 `!dbg`/`!tbaa`/`!range`/`!llvm.loop` 的真实输出，判断 `llvm.assume` 什么时候提供事实、什么时候被丢弃，以及什么时候一条 metadata 会让优化器正当地把程序判成 UB。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON。源码引用按这个 commit；命令输出来自 `pr-lane`。该构建的 `clang` 打印 revision `a5eaf1948b6b`，与源码树不是同一个提交；前端生成的 metadata 细节以实测为准，类定义和语义以 `7c7a9072f736` 为准。

## 心智模型：metadata 是 IR 的旁路表

`Instruction` 通过固定 KindID 找到 `MDNode`；`MDNode` 的 operand 是 `Metadata*`，不是 `Use`。`ValueAsMetadata` 让 metadata 能引用 `Value`，但不会把目标值变成 SSA operand。

- 从指令到 metadata：`Instruction::getMetadata(KindID)`（`llvm/include/llvm/IR/Instruction.h:464`）；`!dbg` 是特例，存在 `DebugLoc` 里（`:466`）。
- 从 metadata 到值：`ValueAsMetadata`（`llvm/include/llvm/IR/Metadata.h:472`）只是包装，不拥有值。

## `Metadata` / `MDNode` / `MDString` / `MDTuple`

`Metadata` 是整套层次结构的根。`llvm/include/llvm/IR/Metadata.h:65` 的注释说它是「typeless data in the IR」。它没有 `Value` 的类型、没有 `User` 的 operand 链，也没有 `Instruction` 的父基本块。

| 类 | 源码位置 | 作用 |
| --- | --- | --- |
| `Metadata` | `Metadata.h:65` | 所有 metadata 的根；按 `MetadataKind` 做 RTTI |
| `MDString` | `Metadata.h:734` | 唯一化的字符串，例如 `!"pair"`、`!"llvm.loop.mustprogress"` |
| `MDNode` | `Metadata.h:1087` | metadata 节点，operand 是 `Metadata*`，不是 `Use` |
| `MDTuple` | `Metadata.h:1505` | 默认按 operands 唯一化的 tuple |
| `ValueAsMetadata` | `Metadata.h:472` | 把 `Value` 包成 metadata 的句柄 |

`MDNode` 有三种存储策略：`Uniqued`、`Distinct`、`Temporary`（`Metadata.h:73`）。`isUniqued()`/`isDistinct()`/`isTemporary()` 在 `Metadata.h:1269-1271`。这解释了两个常见现象：

- 两个内容相同的 `!DILocation` 可以是同一个节点，因为它是 uniqued。
- `!DICompileUnit`、`!DISubprogram` 和循环的 `!llvm.loop` 常带 `distinct`，因为身份本身重要，不能按内容合并。

实测输出里能直接看到这两种形态：

```llvm
!0 = distinct !DICompileUnit(language: DW_LANG_C11, ...)
!15 = distinct !DISubprogram(name: "sum_pairs", ...)
!30 = !DILocation(line: 0, scope: !15)
!52 = distinct !{!52, !36, !53, !54, !55}
```

`MDString` 是唯一化的字符串，`MDTuple` 是默认唯一化的 tuple。它们都不是 `Value`，所以不会出现在 `opt -passes=print<...>` 的 def-use 图里。

## 关键 metadata 速查

固定 KindID 在 `llvm/include/llvm/IR/FixedMetadataKinds.def` 里定义。常用项：

| 文本 | KindID | 源码 | 语义 |
| --- | --- | --- | --- |
| `!dbg` | `MD_dbg` | `FixedMetadataKinds.def:13` | 调试位置/子程序信息 |
| `!tbaa` | `MD_tbaa` | `:14` | 类型别名分析标签 |
| `!prof` | `MD_prof` | `:15` | 分支权重、函数入口计数等 |
| `!range` | `MD_range` | `:17` | 值域约束；越界返回 poison |
| `!alias.scope` | `MD_alias_scope` | `:20` | 该访问所属的别名作用域 |
| `!noalias` | `MD_noalias` | `:21` | 该访问不与这些作用域别名 |
| `!nonnull` | `MD_nonnull` | `:25` | load 出的指针非空 |
| `!llvm.loop` | `MD_loop` | `:32` | 循环属性/提示的容器 |

`!dbg` 的实际结构会在后面的实测里展开。`!tbaa` 的语义在 LangRef `llvm/docs/LangRef.md:7538`：LLVM IR 的内存没有类型，TBAA 用 metadata 描述高层语言的类型系统；不同 root 的 TBAA 节点之间保守地视为 `MayAlias`，同一 root 下才按类型树细化。把 `!tbaa` 挂错，AA 就可能把两个实际别名的访问判成不别名，进而重排 load/store。

`!alias.scope`/`!noalias` 在 LangRef `llvm/docs/LangRef.md:7721`：`alias.scope` 标记访问属于哪些作用域，`noalias` 标记访问与哪些作用域不别名；同一 domain 下 scope 集合满足子集关系时，两个访问被假定不别名；如果它们实际别名，行为是 undefined。这个「实际别名就 UB」是 review 的重点，不是普通的优化提示。

`!range` 在 LangRef `llvm/docs/LangRef.md:7859`：只能挂在整数/整数向量的 load、call、invoke（以及 phi 的特定形式）上，表示返回值落在若干半开区间的并集里；如果运行时的值不在范围内，返回 poison。`!nonnull` 在 `LangRef.md:12491`：只能挂在指针类型的 load 上，load 出 null 时返回 poison。`!prof` 在 `LangRef.md:9072`：记录 `branch_weights`、`function_entry_count`、`VP` 等 profile 数据。`!llvm.loop` 在 `LangRef.md:8205`：挂在循环 latch 的 branch 上，节点是属性列表，第一个 operand 按历史原因指向自己。

本实验生成了 `!tbaa`、`!range`、`!nonnull`、`!llvm.loop`、`!dbg` 的真实输出；`!prof` 与 `!alias.scope`/`!noalias` 本章只按 LangRef 说明，没有单独展开实验。`!alias.scope`/`!noalias` 在一组 `-O3` 的 `restrict` 例子里没有生成，因为 `restrict` 已经落在参数 `noalias` 属性上，循环向量化不需要额外的 scope metadata；不要把「这组实验没看到」写成「LLVM 不会生成」。

## 实测：`-g -O1` 生成的真实 metadata

```bash
cd /tmp/llvm-ch78
cat > md.c <<'C'
struct pair { int a; int b; };
int sum_pairs(int n, struct pair *p) {
  int acc = 0;
  for (int i = 0; i < n; ++i)
    if (p[i].a != 0) acc += p[i].a + p[i].b;
  return acc;
}

C
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang -g -O1 -S -emit-llvm md.c -o md.O1.ll
rg -n '!dbg|!tbaa|!llvm.loop|#dbg_' md.O1.ll | head -20
```

实测输出（节选）：

```text
7:define dso_local i32 @sum_pairs(...) ... !dbg !15 {
9:    #dbg_value(i32 %n, !25, !DIExpression(), !30)
10:    #dbg_value(ptr %p, !26, !DIExpression(), !30)
13:  %cmp15 = icmp sgt i32 %n, 0, !dbg !32
30:  %0 = load i32, ptr %arrayidx, align 4, !dbg !40, !tbaa !41
36:  %1 = load i32, ptr %b, align 4, !dbg !45, !tbaa !46
48:  br i1 %exitcond.not, label %for.cond.cleanup, label %for.body, !dbg !51, !llvm.loop !52
```

对应的 metadata 定义：

```llvm
!0 = distinct !DICompileUnit(language: DW_LANG_C11, file: !1, ..., emissionKind: FullDebug)
!15 = distinct !DISubprogram(name: "sum_pairs", scope: !1, file: !1, line: 2, ...)
!30 = !DILocation(line: 0, scope: !15)
!41 = !{!42, !12, i64 0}
!42 = !{!"pair", !12, i64 0, !12, i64 4}
!52 = distinct !{!52, !36, !53, !54, !55}
!54 = !{!"llvm.loop.mustprogress"}
!55 = !{!"llvm.loop.unroll.disable"}
```

这里能读出三件事：

1. `!dbg` 同时出现在函数（`!15`）、指令（`!41`）和 `#dbg_value` 记录上；`#dbg_value` 不是 `call` 指令，而是附着在位置上的调试记录。
2. `!tbaa` 是 load 上的旁路标签；`!42` 的 `!"pair"` 和两个 `i64` 偏移描述了结构体字段。
3. `!llvm.loop` 挂在循环 latch 的 branch 上，第一个 operand 是自身，后面是 `mustprogress` 和 `unroll.disable`。

### `opt -passes=instcombine` 是否保留 metadata

```bash
$B/opt -passes=instcombine -S md.O1.ll -o md.ic.ll
printf 'before dbg=%s tbaa=%s loop=%s\n' \
  "$(rg -c '!dbg|#dbg_value|#dbg_declare' md.O1.ll)" \
  "$(rg -c '!tbaa' md.O1.ll)" "$(rg -c '!llvm.loop' md.O1.ll)"
printf 'after  dbg=%s tbaa=%s loop=%s\n' \
  "$(rg -c '!dbg|#dbg_value|#dbg_declare' md.ic.ll)" \
  "$(rg -c '!tbaa' md.ic.ll)" "$(rg -c '!llvm.loop' md.ic.ll)"
```

实测输出：

```text
before dbg=29 tbaa=2 loop=1
after  dbg=29 tbaa=2 loop=1
```

这段 IR 上，InstCombine 没有删 metadata。它只调整了 phi 的前驱顺序，`diff` 里能看到 `for.body` 的 predecessor 列表变化，但 `!dbg`、`!tbaa`、`!llvm.loop` 都还在。不要把「这个 pass 保留了」推广成「所有 pass 都保留」；后面的 `assume` 和错误 `!range` 会给出丢弃/利用 metadata 的例子。

同一文件用 `-g0` 编译时，实测 `!dbg`/`#dbg_*` 计数为 0，`!tbaa` 仍是 2，`!llvm.loop` 仍是 1。把两份 IR 的 metadata 去掉再对比，优化后的指令序列相同；`-g` 在这里改变的是 IR 形状，不是优化结果。写 lit 测试时，`CHECK-NEXT` 如果指望两条指令相邻，`-g` 插入的 `#dbg_value` 会让它失败。

## `!range` 与 `!nonnull` 的实测形状

### `!range`：Clang 从 `_Bool` 生成

```bash
cat > range.c <<'C'
struct flags { _Bool ok; unsigned char small; };
_Bool get_ok(struct flags *f) { return f->ok; }
int get_small(struct flags *f) { return f->small; }
C
$B/clang -O1 -S -emit-llvm range.c -o range.O1.ll
sed -n '6,14p' range.O1.ll
rg -n '!range|!noundef' range.O1.ll
```

实测输出：

```llvm
define dso_local zeroext i1 @get_ok(ptr nofree noundef readonly captures(none) %f) local_unnamed_addr #0 {
entry:
  %0 = load i8, ptr %f, align 1, !tbaa !10, !range !13, !noundef !14
  %loadedv = trunc nuw i8 %0 to i1
  ret i1 %loadedv
}
!13 = !{i8 0, i8 2}
!14 = !{}
```

`!range !{i8 0, i8 2}` 表示 load 出的 `i8` 只在 `[0,2)`，也就是 0 或 1；`!noundef` 表示这个值不含 undef/poison bit。两者组合后，`trunc nuw` 才是安全的。注意 `!range` 是对**结果值**的约束，不是对指针的约束。

### `!nonnull`：load 出的指针非空

```bash
cat > mdnonnull.ll <<'IR'
define i1 @loaded_nonnull(ptr %pp) {
entry:
  %v = load ptr, ptr %pp, align 8, !nonnull !0
  %c = icmp eq ptr %v, null
  ret i1 %c
}
!0 = !{}
IR
$B/opt -passes=instcombine -S mdnonnull.ll -o mdnonnull.ic.ll
cat mdnonnull.ic.ll
```

实测输出：

```llvm
define i1 @loaded_nonnull(ptr %pp) {
entry:
  ret i1 false
}
```

`!nonnull` 让 InstCombine 直接判定 `%v == null` 为 false。如果 load 实际返回 null，按 LangRef 它返回 poison；`icmp` 消费 poison 后结果不可预测，后续分支可能落成 UB。这条 metadata 是契约，不是注释。

## `llvm.assume` 与 `AssumptionCache`

`llvm.assume` 的语义在 LangRef `llvm/docs/LangRef.md:27273`：调用者保证条件为真；如果条件为 false 或 undef，行为是 undefined。它不是「运行时检查」，而是把事实交给优化器。

`AssumptionCache` 是函数内 `llvm.assume` 的索引。`llvm/include/llvm/Analysis/AssumptionCache.h:40-44` 说明它缓存 assume 调用，避免每次查询都扫函数；`:42` 还规定创建新 assume 的 pass 必须调用 `registerAssumption()`，删除 assume 不需要特殊处理。它用 `WeakVH` 保存句柄（`AssumptionCache.h:59`），所以 assume 被删除后句柄会变空；`assumptionsFor(V)` 在 `:170` 按值查受影响的 assume。

### 实测：InstCombine 用 assume 折叠比较

```bash
cat > assume2.ll <<'IR'
declare void @llvm.assume(i1)

define i1 @in_range(i32 %x) {
entry:
  %c = icmp ult i32 %x, 10
  call void @llvm.assume(i1 %c)
  %d = icmp ult i32 %x, 20
  ret i1 %d
}

IR
$B/opt -passes=instcombine -S assume2.ll -o assume2.ic.ll
cat assume2.ic.ll
```

实测输出：

```llvm
define i1 @in_range(i32 %x) {
entry:
  %c = icmp ult i32 %x, 10
  call void @llvm.assume(i1 %c)
  ret i1 true
}

```

`%d` 被折叠成 `true`。`InstCombineCalls.cpp:4079` 处理 range bundle：如果已知范围已经包含 assume 的范围，就删除 assume；`:4083` 则会在知道更精确范围时更新 assume。`InstCombineCalls.cpp:4006` 对 `"nonnull"` bundle 也做类似的事：能证明非空就删 assume，能折进 load 就写 `!nonnull`/`!noundef` metadata。

### 什么时候 assume 会被丢弃

```bash
cat > assume3.ll <<'IR'
declare void @llvm.assume(i1)

define void @trivial() {
entry:
  call void @llvm.assume(i1 true)
  ret void
}

define void @redundant_nonnull(ptr dereferenceable(4) %p) {
entry:
  %c = icmp ne ptr %p, null
  call void @llvm.assume(i1 %c)
  ret void
}
IR
$B/opt -passes=instcombine -S assume3.ll -o assume3.ic.ll
cat assume3.ic.ll
```

实测输出：

```llvm
define void @trivial() {
entry:
  ret void
}

define void @redundant_nonnull(ptr dereferenceable(4) %p) {
entry:
  ret void
}
```

两个 assume 都被删了：`assume(true)` 没有信息量；`dereferenceable(4)` 已经隐含非空，`%c` 恒真。源码里 `InstCombineCalls.cpp:3921` 进入 `Intrinsic::assume` 分支，后面按 operand bundle 类型逐项判断；`SimplifyCFG.cpp:9314` 还明确把 `llvm.assume(false/undef)` 视为立即 UB。review 里看到 assume 被删，先判断它是「冗余」还是「条件根本不成立」；后者不是清理，是把程序送进 UB。

## 调试信息：`DIBuilder` 与 `dbg.value` 的位置语义

调试信息的类层次在 `llvm/include/llvm/IR/DebugInfoMetadata.h`：

| 类 | 源码位置 | 作用 |
| --- | --- | --- |
| `DICompileUnit` | `DebugInfoMetadata.h:2053` | 一个编译单元；`emissionKind` 决定 FullDebug/LineTablesOnly 等 |
| `DISubprogram` | `DebugInfoMetadata.h:2301` | 函数级调试描述；带 `spFlags`、`retainedNodes` |
| `DILocation` | `DebugInfoMetadata.h:2787` | 指令位置；`getLine()`/`getColumn()` 在 `:2859-2860` |
| `DILocalVariable` | `DebugInfoMetadata.h:4520` | 局部变量/参数描述 |
| `DbgRecord` | `DebugProgramInstruction.h:140` | 附在指令位置上的调试记录基类 |
| `DbgVariableRecord` | `DebugProgramInstruction.h:279` | `#dbg_value`/`#dbg_declare` 的记录实现 |

`DILocation::getScope()` 在 `:2861`，`getInlinedAt()` 在 `:2875`。`getInlinedAt()` 形成内联链：一条指令的 `!dbg` 可以指向被内联函数的位置，再通过 `inlinedAt` 指回调用点。review 内联相关调试信息时，先看这条链有没有断，而不是只看行号。

`DIBuilder` 是构造这些节点的工厂。常用 API 的形状：

| API | 源码位置 | 用途 |
| --- | --- | --- |
| `createCompileUnit(...)` | `llvm/include/llvm/IR/DIBuilder.h:140` | 创建 `DICompileUnit` |
| `createFunction(...)` | `DIBuilder.h:1011` | 创建 `DISubprogram` |
| `createLexicalBlock(...)` | `DIBuilder.h:1119` | 创建词法块 |
| `insertDeclare(...)` | `DIBuilder.h:1170` | 插入 `#dbg_declare` 记录 |
| `insertDbgValue(...)` | `DIBuilder.h:1230` | 插入 `#dbg_value` 记录 |

`insertDbgValue(Value *Val, DILocalVariable *VarInfo, DIExpression *Expr, const DILocation *DL, InsertPosition InsertPt)` 的最后一个参数是位置。`#dbg_value` 不是「在函数里随便放一条指令」，而是「从 InsertPt 起，变量 Val 的位置是 Expr 描述的位置，源码位置是 DL」。位置错了，调试器看到的值就可能来自另一条控制流路径。

### 实测：`-g` 下 `dbg.declare` 如何变成 `dbg.value`

```bash
cat > dbgloss.c <<'C'
int fold_me(int x) {
  int y = x + 0;
  int z = y * 1;
  return z;
}
C
$B/clang -g -O0 -Xclang -disable-O0-optnone -S -emit-llvm dbgloss.c -o dbgloss.g.ll
$B/opt -passes='mem2reg,instcombine' -S dbgloss.g.ll -o dbgloss.ic.ll
sed -n '8,20p' dbgloss.g.ll
echo '--- after instcombine ---'
sed -n '8,20p' dbgloss.ic.ll
```

实测输出：

```llvm
entry:
  %x.addr = alloca i32, align 4
  %y = alloca i32, align 4
  %z = alloca i32, align 4
  store i32 %x, ptr %x.addr, align 4
    #dbg_declare(ptr %x.addr, !15, !DIExpression(), !16)
    #dbg_declare(ptr %y, !17, !DIExpression(), !18)
  %0 = load i32, ptr %x.addr, align 4, !dbg !19
  %add = add nsw i32 %0, 0, !dbg !20
  store i32 %add, ptr %y, align 4, !dbg !18
--- after instcombine ---
entry:
    #dbg_value(i32 %x, !15, !DIExpression(), !16)
    #dbg_value(i32 %x, !17, !DIExpression(), !16)
    #dbg_value(i32 %x, !18, !DIExpression(), !16)
  ret i32 %x, !dbg !19
```

mem2reg 消掉了 alloca、store、load，InstCombine 折掉了 `+0` 和 `*1`；调试记录随之从 `#dbg_declare(ptr ...)` 变成 `#dbg_value(i32 %x, ...)`。这不是「调试信息丢了」，而是 pass 在值被替换后把变量位置重写成新的 SSA 值。每个变量各保留一条记录，同一个 `%x` 被多个变量别名引用。review 调试信息 diff 时，不能只看记录条数；要看变量、表达式和位置是否仍指向正确的值。

`#dbg_value` 的位置语义还体现在 `DILocation(line: 0, scope: ...)`。实测的 `!30` 就是 `!DILocation(line: 0, scope: !15)`；line 0 常用于「变量位置」而不是某条源码语句。调试器把变量位置和源码行分开处理，测试也不应该用行号去断言变量记录。

## 失败与边界：metadata 撒谎、丢失、写错

### 错误 `!range`：优化器正当地把结果折成常量

```bash
cat > badrange.ll <<'IR'
define i32 @bad_range(ptr %p) {
entry:
  %v = load i32, ptr %p, align 4, !range !0
  %c = icmp ult i32 %v, 100
  %r = select i1 %c, i32 1, i32 0
  ret i32 %r
}
!0 = !{i32 0, i32 10}
IR
$B/opt -passes=instcombine -S badrange.ll -o badrange.ic.ll
cat badrange.ic.ll
```

实测输出：

```llvm
define i32 @bad_range(ptr %p) {
entry:
  ret i32 1
}
```

`!range !{i32 0, i32 10}` 声称 load 值小于 10，因此 `icmp ult %v, 100` 恒真，整个函数折成 `ret i32 1`。如果内存里实际是 1000，按 LangRef 这个 load 返回 poison，程序已经进入未定义行为；优化器没有 bug，写 metadata 的人制造了 UB。review 里看到新增 `!range`，必须问「这个范围是从哪里证明的，所有写入路径都满足吗」。

### 丢掉 `!range`：同一段 IR 失去折叠

```bash
sed 's/, !range !0//' badrange.ll | sed '/^!0/d' > badrange_nometa.ll
$B/opt -passes=instcombine -S badrange.ll -o badrange.ic.ll
$B/opt -passes=instcombine -S badrange_nometa.ll -o badrange_nometa.ic.ll
cat badrange.ic.ll
echo '--- without metadata ---'
cat badrange_nometa.ic.ll
```

实测输出：

```llvm
define i32 @bad_range(ptr %p) {
entry:
  ret i32 1
}
--- without metadata ---
define i32 @bad_range(ptr %p) {
entry:
  %v = load i32, ptr %p, align 4
  %c = icmp ult i32 %v, 100
  %r = zext i1 %c to i32
  ret i32 %r
}
```

保留 `!range` 时比较被折成常量，去掉后 load 和 `icmp` 都留着。这就是「丢 metadata 导致优化退化」的最小对照：没有 UB，没有错误结果，只是后续 pass 少了事实。反过来说，如果 pass 在复制/合并 load 时没有携带 `!range`，性能回退可能不会以测试失败的形式出现。
## Review 检查表

看到一个 metadata 或调试信息 diff，按这个顺序问：

1. 这条 metadata 是提示还是契约？`!prof` 写错只影响取舍，`!range`/`!nonnull`/`!alias.scope` 写错可能直接引入 UB。
2. 它挂在什么位置？`!range` 只能用于特定指令，`!llvm.loop` 必须在 latch branch 上，`!dbg` 的 scope 必须匹配。
3. 它引用的 `ValueAsMetadata` 在 RAUW/删除后有没有更新？有没有留下悬空引用？
4. `llvm.assume` 是新增还是删除？新增的 assume 是否注册到 `AssumptionCache`？
5. `-g` 测试是否因为 `#dbg_value` 插入而变得脆弱？有没有用 `-g0` 或 `-debugify` 做对照？
## 踩雷集锦

1. **把 metadata 当 SSA 值**。`MDNode` 没有类型、没有定义点，`getMetadata()` 返回的不是 `Value*`；在 def-use 图里找它永远找不到。
2. **以为所有 metadata 都是提示**。`!range`、`!nonnull`、`!alias.scope` 写错会把程序送进 poison/UB；`!prof` 写错只是让优化器判断变差。
3. **看到 `!dbg` 被删就认为调试信息坏了**。指令被折叠后，`#dbg_declare` 可能被 salvage 成 `#dbg_value`；要看变量位置是否仍指向正确的值，而不是数记录条数。

## 本章重点整理

- `Metadata` 是 IR 的旁路对象：不参与 SSA、不拥有值、operand 是 `Metadata*` 而不是 `Use`。
- `!dbg` 走 `DebugLoc` 特例；`!tbaa`/`!range`/`!nonnull`/`!prof`/`!llvm.loop` 走固定 KindID 的 metadata 表。
- `!range`/`!nonnull`/`!alias.scope` 是契约，违反会产生 poison 或 UB；`!prof` 是提示。
- `llvm.assume` 把事实交给优化器；`AssumptionCache` 负责索引，新增 assume 要注册，删除由弱句柄处理。
- 同一段 IR 去掉 `!range` 就会失去折叠，这是 metadata 丢失导致优化退化的最小证据。

## 自我检核

- [ ] 不看笔记，能解释 `Metadata`、`MDNode`、`MDString`、`MDTuple` 的关系，以及为什么 `MDNode` 不是 `Value`
- [ ] 能说出 `Uniqued`、`Distinct`、`Temporary` 三种存储策略的差别，并各举一个真实节点
- [ ] 能解释 `ValueAsMetadata` 为什么存在，以及它是否让目标值参与 SSA
- [ ] 能区分 `!prof` 这类提示和 `!range`/`!nonnull` 这类契约
- [ ] 能说出 `!range` 越界时返回什么，以及为什么优化器可以据此折叠比较
- [ ] 能解释 `llvm.assume(false)` 的后果，以及 assume 被删除的两种不同原因
- [ ] 能描述 `#dbg_value` 的位置语义，并说明 `DILocation(line: 0)` 常见于什么场景
- [ ] 能解释为什么 `-g` 可能让 `CHECK-NEXT` 测试变脆，而不一定改变优化结果

## 延伸阅读

- **[LLVM Language Reference: Metadata](https://llvm.org/docs/LangRef.html#metadata)**
  - **读哪里**：`tbaa`、`noalias`/`alias.scope`、`range`、`nonnull`、`prof`、`llvm.loop` 六节
  - **能学到什么**：每种 metadata 的附着点、编码和违反后果；这是 review metadata diff 的第一手判据
  - **前提**：读过 Ch 4 的 `Value` 与 Ch 7 的属性契约
- **[LLVM Language Reference: `llvm.assume`](https://llvm.org/docs/LangRef.html#llvm-assume-intrinsic)**
  - **读哪里**：intrinsic 语义、operand bundles 的 `"nonnull"`/`"range"`/`"align"` 形式
  - **能学到什么**：assume 如何把事实交给优化器，以及它和运行时检查的区别
  - **前提**：知道 poison 和 UB 的基本区别，读过 Ch 9 的 Verifier 边界
- **[LLVM Source: `DebugInfoMetadata.h` 与 `DIBuilder.h`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/DebugInfoMetadata.h)**
  - **读哪里**：`DICompileUnit`、`DISubprogram`、`DILocation` 的字段，以及 `DIBuilder` 的 `create*`/`insert*` 方法
  - **能学到什么**：调试节点的真实 API 形状，而不是只记 `!dbg` 文本
  - **前提**：能读 LLVM 的 `MDNode` 和 `TypedArray` 包装
→ [Ch 9 — `Verifier` 与 IR 不变量：什么样的 IR 才算合法](./09-verifier-and-ir-invariants.md)
