# 练习 A：给 TableGen 加一条带精确 source range 的诊断

> **目标**：走完「词法 → 语法 → 语义检查」的最小改动链路，并写出符合 LLVM 风格的错误信息 + lit 测试。这是全书第一个真正改 `lib/TableGen` 的练习，也是 Final Project 的热身。

> **环境**：LLVM `24.0.0git`。需要能增量重编 `llvm-tblgen`。

---

## 任务规格

当前的 `def` 重名诊断长这样（Ch 0 实测）：

```console
$ llvm-tblgen dup.td
dup.td:2:5: error: def already exists: A
def A { int Y = 2; }
    ^
dup.td:1:5: note: location of previous definition
def A { int X = 1; }
    ^
```

**任务**：把这条诊断升级成能指出「两个定义在语义上为什么会冲突」，并给 `note` 加上**完整 source range**（高亮整个 `def A {...}` 而不只是名字），同时保证：

1. 不改变任何现有正确程序的行为（`llvm/test/TableGen/` 全绿）。
2. 新增至少 3 个 lit 测试：普通重名、`def` 与匿名 def 冲突、`defm` 展开后的重名。
3. 错误信息包含足够上下文，让用户不必回看源码就能定位。

## 起点：先读三处代码

### ① 诊断的产生点

`RecordKeeper::addDef`（`llvm/lib/TableGen/Record.h:2098`）：

```cpp
void addDef(std::unique_ptr<Record> R) {
  for (const Record *SC : make_first_range(R->getDirectSuperClasses()))
    DefsByDirectSuperClass[SC].push_back(R.get());
  bool Ins = Defs.try_emplace(R->getName(), std::move(R)).second;
  (void)Ins;
  assert(Ins && "Record already exists");
  if (!Cache.empty()) Cache.clear();
}
```

注意这里**只有 `assert`**。真正的用户可见错误在 `TGParser::addDefOne`（`TGParser.cpp:580`）：

```cpp
bool TGParser::addDefOne(std::unique_ptr<Record> Rec) {
  if (Record *Prev = Records.getDef(Rec->getName())) {
    // 这里产生 "def already exists"
  }
  ...
}
```

### ② 诊断的格式

`llvm/lib/TableGen/Error.h` 的重载（实际签名，LLVM 24）：

```cpp
void PrintNote(const Twine &Msg);
void PrintNote(function_ref<void(raw_ostream &OS)> PrintMsg);
void PrintNote(ArrayRef<SMLoc> NoteLoc, const Twine &Msg);

void PrintWarning(ArrayRef<SMLoc> WarningLoc, const Twine &Msg);

void PrintError(const Twine &Msg);
void PrintError(function_ref<void(raw_ostream &OS)> PrintMsg);
void PrintError(ArrayRef<SMLoc> ErrorLoc, const Twine &Msg);
void PrintError(const char *Loc, const Twine &Msg);
void PrintError(const Record *Rec, const Twine &Msg);
void PrintError(const RecordVal *RecVal, const Twine &Msg);
```

注意**没有 `SMLoc` 单点重载**：最小粒度是 `ArrayRef<SMLoc>`，而 `Error.cpp` 的 `PrintMessage` 对它的解释是——

```cpp
static void PrintMessage(ArrayRef<SMLoc> Locs, SourceMgr::DiagKind Kind, const Twine &Msg) {
  if (Kind == SourceMgr::DK_Error) ++ErrorsPrinted;
  SMLoc NullLoc;
  if (Locs.empty()) Locs = NullLoc;
  SrcMgr.PrintMessage(Locs.consume_front(), Kind, Msg);
  for (SMLoc Loc : Locs)
    SrcMgr.PrintMessage(Loc, SourceMgr::DK_Note, "instantiated from multiclass");
}
```

**第一个位置是主诊断，其余每个位置都会自动打印成一条 `note: instantiated from multiclass`。** 这个设计正好适合 `defm` 展开产生的记录（Ch 8 会用到），但如果你想打印「两个不同 def 的位置」，不能靠它——那会得到误导性的 multiclass note。

这就是本练习的核心难点：要给 `note` 加上**完整 source range**（高亮整个 `def A {...}`），得自己调 `SourceMgr::PrintMessage` 并传 `SMRange`。

### ③ 已有 note 的写法

现有实现在 `TGParser::addDefOne`（`TGParser.cpp:580`）：

```cpp
if (const Record *Prev = Records.getDef(Rec->getNameInitAsString())) {
  if (!Rec->isAnonymous()) {
    PrintError(Rec->getLoc(), "def already exists: " + Rec->getNameInitAsString());
    PrintNote(Prev->getLoc(), "location of previous definition");
    return true;
  }
  NewName = Records.getNewAnonymousName();
}
```

注意 `PrintError(Rec->getLoc(), ...)` 直接把 `Record::getLoc()`（`ArrayRef<SMLoc>`）传进去。如果这个 def 是从 `defm` 展开的，`getLoc()` 里会有多个元素，于是你会看到：

```console
$ llvm-tblgen dupm.td
dupm.td:4:5: error: def already exists: D_x
def D_x : C<2>;
    ^
dupm.td:2:27: note: location of previous definition
multiclass M<int n> { def _x : C<n>; }
                          ^
dupm.td:3:10: note: instantiated from multiclass
defm D : M<1>;
         ^
```

（`dupm.td` 内容：`multiclass M<int n> { def _x : C<n>; }` + `defm D : M<1>;` + `def D_x : C<2>;`。）

这就是 ② 那段 `PrintMessage` 的行为：`Prev->getLoc()` 有三个元素，第一个是 multiclass 里 `def _x` 的位置，第二、三个被自动标成 `instantiated from multiclass`。**这个自动 note 很有用，但它和「用户想看的两个 def 位置」是不同的东西**——注意它甚至没指出 `defm D : M<1>` 的哪一部分产生了 `D_x`。理解这个区别是设计新诊断的前提。

## 分段实作建议

### Step 1（30 分钟）：复现并定位

```bash
cd ~/tg-lab/scratch
cat > dup.td <<'EOF'
def A { int X = 1; }
def A { int Y = 2; }
EOF
~/tg-lab/build/bin/llvm-tblgen dup.td
```

确认输出与本章开头一致。然后在 `TGParser.cpp` 里 `grep -n "already exists"`，找到产生点，读它上下 40 行。

**验收**：能说出这条错误是在哪个函数、由哪个 `if` 触发的。

### Step 2（1 小时）：扩展诊断基础设施

在 `llvm/include/llvm/TableGen/Error.h` 加一个重载：

```cpp
void PrintError(SMRange Range, const Twine &Msg);
void PrintNote(SMRange Range, const Twine &Msg);
```

在 `Error.cpp` 里实现「打印范围」。

**关键提示**：不要自己拼 `^~~~` 字符串。`SourceMgr` 已经有能处理范围的接口：

```cpp
// llvm/include/llvm/Support/SourceMgr.h
void PrintMessage(raw_ostream &OS, SMLoc Loc, DiagKind Kind, const Twine &Msg,
                  ArrayRef<SMRange> Ranges = {}, ArrayRef<FixItHint> FixIts = {},
                  bool ShowColors = true) const;
```

传 `Ranges = {Range}` 就会得到跨行高亮、tab 对齐、颜色。自己写必然在多行 case 上出错。

**验收**：写一个 3 行的测试 `.cpp` 调用新重载，确认输出格式。

### Step 3（1 小时）：改错误信息

把 `addDefOne` 里的诊断改成：

```
error: redefinition of 'A'
note: previous definition is here
note: both definitions have the same name; TableGen requires unique def names
```

最后一条 note 是**给新手的解释**，而不是重复前两条信息。判断标准：删掉它，用户会不会少知道一件事？会 → 留；不会 → 删。

**验收**：`llvm-tblgen dup.td` 的输出包含三条信息，且 `note` 各自指向不同位置。

### Step 4（1 小时）：补 lit 测试

在 `llvm/test/TableGen/` 下新建 `def-redefinition.td`：

```td
// RUN: not llvm-tblgen %s 2>&1 | FileCheck %s

// CHECK: error: redefinition of 'A'
// CHECK-NEXT: note: previous definition is here
def A { int X = 1; }
def A { int Y = 2; }
```

三个测试场景：

1. **普通重名**（上面这个）
2. **匿名 def 冲突**：

   ```td
   def A { int X = 1; }
   def : A {}     // 匿名 def，名字由 TableGen 生成
   ```

   先实测当前行为再写断言——**不要假设**它会报什么。

3. **`defm` 展开后的重名**：

   ```td
   class C<int n> { int N = n; }
   multiclass M<int n> { def _x : C<n>; }
   defm D : M<1>;
   def D_x : C<2>;   // 与 defm 产生的 D_x 冲突
   ```

**验收**：`llvm-lit -v llvm/test/TableGen/def-redefinition.td` 全绿。

### Step 5（1 小时）：跑全量回归

```bash
ninja -C ~/tg-lab/build llvm-tblgen
~/tg-lab/build/bin/llvm-lit ~/tg-lab/src/llvm-project/llvm/test/TableGen
```

目标：`Failed: 0`。如果有 fail，看是不是你的诊断改了某个测试的期望输出——那是**合法的破坏性变更**，需要同步更新那个测试，并在 commit message 里说明。

## 参考解答

<details>
<summary>点开看完整实现（含关键取舍说明）</summary>

**`llvm/include/llvm/TableGen/Error.h`**：

```cpp
void PrintError(const SMRange &Range, const Twine &Msg);
void PrintNote(const SMRange &Range, const Twine &Msg);
```

**`llvm/lib/TableGen/Error.cpp`**：

```cpp
namespace {
void printRangedMessage(SMLoc Loc, SourceMgr::DiagKind Kind, const Twine &Msg,
                        SMRange Range) {
  if (Kind == SourceMgr::DK_Error) ++ErrorsPrinted;
  SrcMgr.PrintMessage(errs(), Loc, Kind, Msg, Range);
}
} // namespace

void llvm::PrintError(SMRange Range, const Twine &Msg) {
  printRangedMessage(Range.Start, SourceMgr::DK_Error, Msg, Range);
}
void llvm::PrintNote(SMRange Range, const Twine &Msg) {
  printRangedMessage(Range.Start, SourceMgr::DK_Note, Msg, Range);
}
```

（`errs()` 与 `WithColor` 的取舍：现有 `PrintMessage` 用的是 `SrcMgr.PrintMessage(Loc, Kind, Msg)` 那个无 `OS` 的重载，它默认写到 `errs()`。保持一致性即可。）

关键取舍：**不要自己拼字符串画 `^~~~`**。`SourceMgr::PrintMessage` 的 `Ranges` 参数会正确处理单行/多行、tab、颜色。自己写会在多行 range 上出错。

**`llvm/lib/TableGen/TGParser.cpp` 的 `addDefOne`**：

```cpp
bool TGParser::addDefOne(std::unique_ptr<Record> Rec) {
  if (Record *Prev = Records.getDef(Rec->getName())) {
    PrintError(SMRange(Rec->getLoc().front(), Rec->getLoc().back()),
               "redefinition of '" + Rec->getName() + "'");
    PrintNote(SMRange(Prev->getLoc().front(), Prev->getLoc().back()),
              "previous definition is here");
    PrintNote(Rec->getLoc().front(),
              "both definitions have the same name; TableGen requires unique "
              "def names");
    return true;
  }
  ...
}
```

**为什么用 `getLoc().front()` 到 `.back()`**：`Record::Locs` 保留实例化位置；对从 `defm` 展开的记录，它还会带上 multiclass 原型的位置。用首尾做 `SMRange` 在简单 case 上够用，但**在多行 def 上不是精确的「整个 body」**——`Locs` 里没有 body 结束位置。真正精确的做法是在 `ParseBody` 结束时把 `}` 的位置也记进 `Record`，这是加分挑战 4。

**实测提醒**：`def A` 与 `def A_x`（不同名）不会冲突；`defm` 产生的名字是 `D_x` 形式，冲突时 `getLoc()` 会有 3 个元素（见下面卡点 3）。

**测试**（`llvm/test/TableGen/def-redefinition.td`）：

```td
// RUN: not llvm-tblgen %s 2>&1 | FileCheck %s
// CHECK: error: redefinition of 'A'
// CHECK-NEXT: note: previous definition is here
// CHECK-NEXT: both definitions have the same name

def A { int X = 1; }
def A { int Y = 2; }
```

</details>

## 卡点提示

1. **`PrintError` 找不到 `SMRange` 重载**：`Error.h` 里只有 `SMLoc` 版本，你得自己加。别试图 `SMRange` 隐式转 `SMLoc`——它不会。
2. **编译报 `undefined reference`**：`Error.cpp` 在 `LLVMTableGen` 库里，改了它要重编整个库（`ninja llvm-tblgen` 会自动处理）。如果只编 `llvm-tblgen` 不重编库，会链接到旧符号。
3. **测试里 `CHECK-NEXT` 不匹配**：`SourceMgr` 会在消息前加 `file:line:col: `，且 `PrintNote(ArrayRef<SMLoc>)` 对第 2 个及以后的位置会插一条 `note: instantiated from multiclass`。写 `CHECK` 时要按真实行数留位，不要假设「一行 note」。
4. **`getLoc().back()` 是引用位置不是定义结束**：`Record::Locs` 后面还会 append 引用位置。如果你在解析完整个文件后调用，`.back()` 可能是别处引用它的位置。**在 `addDefOne` 里调用是安全的**，因为那时只有定义位置。
5. **匿名 def 的行为和你想的不一样**：`addDefOne` 里 `if (!Rec->isAnonymous())` 才报错，匿名 def 会被 `getNewAnonymousName()` 改名后插入。所以 `def : A {}` 不会和 `def A` 冲突。先实测再写断言。

## 加分挑战

- **挑战 1**：让诊断同时指出「两个定义分别在哪个文件的哪一行」，支持跨 include 文件的重名。
- **挑战 2**：给 `class` 重名也加同样的诊断（当前 `ParseClass` 有类似检查，但信息更弱）。找出它的位置并统一。
- **挑战 3**：统计 `llvm/test/TableGen` 里有多少测试依赖现有的 `def already exists: A` 文案，评估改文案的破坏面。这个数字决定你能不能直接改，还是需要保留旧文案。
- **挑战 4**：让 `Record` 记住 body 的结束位置，使 `SMRange` 能精确覆盖整个定义。需要改 `ParseBody`、`Record` 和所有构造点——评估这个改动的上游接受度。

## 完成后你应该能回答

- `Record::getLoc()` 返回的 `ArrayRef<SMLoc>` 里，元素分别是什么？为什么 `PrintError(Record*)` 会把第 2 个及以后的位置打印成 `instantiated from multiclass`？
- `SourceMgr::PrintMessage` 的 `Ranges` 参数和手动拼 `^~~~` 相比，多处理了哪些情况？
- 为什么 `addDefOne` 里检查重名，而 `RecordKeeper::addDef` 里只有 `assert`？
- 如果你要加「跨文件重名」诊断，需要动哪些数据结构？
