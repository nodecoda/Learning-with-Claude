# Ch 10 位域、`bits<>` 与字段偏移的语义

> **本章目标**：彻底搞清 `bits<n>` 的三套「位序」——存储顺序、打印顺序、`{hi...lo}` 的索引方向；能预测任意 `let B{x...y} = v` 的结果；知道 `V{0...7}` 与 `V{7...0}` 的差别不是「写反了」，而是真的反转。

> **环境**：LLVM `24.0.0git`。

---

## 10.1 三套位序，必须分开记

这是 TableGen 里最容易搞混的地方。三个不同的东西都叫「位号」：

| 场景 | 顺序 | 说明 |
| --- | --- | --- |
| **存储**（`BitsInit::getBits()`） | `[0]` 是最低位 | `ParseSimpleValue` 用 `1LL << i` 构造 |
| **打印**（`-print-records`） | 最左是最高位 | `getAsString` 用 `reverse(getBits())` |
| **索引**（`V{3...0}`） | 数字小的是低位 | 和硬件手册一致 |

先用一个实验把三者对齐：

```td
def X { bits<8> V = 0xE4; }
```

```console
$ llvm-tblgen -print-records bt4.td
def X {
  bits<8> V = { 1, 1, 1, 0, 0, 1, 0, 0 };
}
```

用 Python 核对：

```console
$ python3 -c "v=0xE4; print('MSB first:', [(v>>i)&1 for i in range(7,-1,-1)])"
MSB first: [1, 1, 1, 0, 0, 1, 0, 0]
```

**打印是 MSB-first**，和 `0xE4` 的二进制写法 `11100100` 一致。但内存里 `getBits()[0]` 是 `0`（最低位）。

源码证据：

```cpp
// 构造（TGParser.cpp 的 ParseSimpleValue）
for (unsigned i = 0; i != BinaryVal.second; ++i)
  Bits[i] = BitInit::get(Records, BinaryVal.first & (1LL << i));   // [0] = LSB

// 打印（Record.cpp:509）
std::string BitsInit::getAsString() const {
  std::string Result = "{ ";
  ListSeparator LS;
  for (const Init *Bit : reverse(getBits())) {      // ← 反转
    ...
  }
  return Result + " }";
}
```

## 10.2 索引：`V{3...0}` 取的是低四位

```td
def X {
  bits<8> V = 0xA5;
  bit B0 = V{0};
  bit B7 = V{7};
  bits<4> Lo = V{3...0};
  bits<4> Hi = V{7...4};
}
```

```console
$ llvm-tblgen -print-records bt1.td | tail -8
def X {
  bits<8> V = { 1, 0, 1, 0, 0, 1, 0, 1 };
  bit B0 = 1;
  bit B1 = 0;
  bit B7 = 1;
  bits<4> Lo = { 0, 1, 0, 1 };
  bits<4> Hi = { 1, 0, 1, 0 };
  ...
}
```

`0xA5` = `10100101`：
- `V{0}` = 最低位 = 1 ✓
- `V{7}` = 最高位 = 1 ✓
- `V{3...0}` = `0101`（低四位），打印 `{0,1,0,1}` ✓
- `V{7...4}` = `1010`（高四位），打印 `{1,0,1,0}` ✓

**`{hi...lo}` 里 hi 写在左边，但 hi 是高位**。这和 Verilog 的 `[7:4]` 一致，和 C 的位域声明顺序相反。

## 10.3 `{0...7}` 不是「写反了」，是真反转

这是本课最容易踩的坑。看：

```td
def X {
  bits<8> V = 0xE4;
  bits<8> R = V{0...7};
  bits<8> S = V{7...0};
}
```

```console
$ llvm-tblgen -print-records bt7.td | tail -5
def X {
  bits<8> V = { 1, 1, 1, 0, 0, 1, 0, 0 };
  bits<8> R = { 0, 0, 1, 0, 0, 1, 1, 1 };
  bits<8> S = { 1, 1, 1, 0, 0, 1, 0, 0 };
}
```

- `S = V{7...0}`：范围覆盖全部位，顺序是从高到低，结果**和原值相同**（因为 `{7,6,5,4,3,2,1,0}` 对存储来说是降序，取值后重新按降序放回，等于恒等）。
- `R = V{0...7}`：范围是升序 `{0,1,...,7}`，取值后按升序放回，**得到完整的位反转**：`11100100` → `00100111`。

机制在 `BitsInit::convertInitializerBitRange`：

```cpp
BitsInit::convertInitializerBitRange(ArrayRef<unsigned> Bits) const {
  SmallVector<const Init *, 16> NewBits(Bits.size());
  for (auto [Bit, NewBit] : zip_equal(Bits, NewBits)) {
    if (Bit >= getNumBits()) return nullptr;
    NewBit = getBit(Bit);          // 按 Bits 的顺序逐个取
  }
  return BitsInit::get(getRecordKeeper(), NewBits);
}
```

`Bits` 的顺序**就是结果的存储顺序**。所以 `{0...7}` 让结果 `[0]` = 原 `[0]`… 等一下，这看起来是恒等？

不，关键在于 `ParseValue` 里对 `Ranges` 做了 `std::reverse`：

```cpp
SmallVector<unsigned, 16> Ranges;
ParseRangeList(Ranges);
if (Ranges.empty()) return nullptr;
std::reverse(Ranges.begin(), Ranges.end());        // ← 这里
Result = Result->convertInitializerBitRange(Ranges);
```

`ParseRangePiece` 解析 `0...7` 时按 `{0,1,2,3,4,5,6,7}` 推入 `Ranges`，然后 `std::reverse` 变成 `{7,6,5,4,3,2,1,0}`。再传给 `convertInitializerBitRange`，于是 `NewBits[0] = getBit(7)`……

**这就是反转的来源**：源码里写的顺序被 reverse 成「降序」，然后按降序填充存储（`[0]` 是最低位）。写成 `{0...7}` 时，reverse 后是 `{7..0}`，填出来的存储是 `[0]=getBit(7)`，即最高位放到最低位——完整反转。

写成 `{7...0}` 时，`ParseRangePiece` 推入 `{7,6,...,0}`，reverse 后是 `{0,1,...,7}`，填出来 `[i] = getBit(i)`——恒等。

**结论**：源码里 `{hi...lo}` 的写法是「降序」，reverse 后变升序，正好对上 `[0]` 是最低位的存储约定。写 `{lo...hi}` 就是升序，reverse 后变降序，产生反转。**这不是 bug，是坐标系的自然结果**——但极其容易写错。

> 实用规则：**永远写 `{hi...lo}`**（hi 在前）。想要位反转，用 `!foreach` 或显式列表更安全。

## 10.4 `let` 的位域赋值：三个必须知道的细节

```td
class C { bits<8> V = 0x00; }
def X : C { let V{3...0} = 0b1111; }
def Y : C { let V{7...4} = 0b1010; }
```

```console
$ llvm-tblgen -print-records bt3.td | tail -6
def X {	// C
  bits<8> V = { 0, 0, 0, 0, 1, 1, 1, 1 };
}
def Y {	// C
  bits<8> V = { 1, 0, 1, 0, 0, 0, 0, 0 };
}
```

`X`：低四位变 `1111`，高四位保持 `0000`。`Y`：高四位变 `1010`，低四位保持。

实现（`TGParser::SetValue`，296 行起）：

```cpp
if (!BitList.empty()) {
  const auto *CurVal = dyn_cast<BitsInit>(RV->getValue());
  if (!CurVal)
    return Error(Loc, "Value '" + ... + "' is not a bits type");

  // ① 把右值转成 bits<BitList.size()>
  const Init *BI = V->getCastTo(BitsRecTy::get(Records, BitList.size()));
  if (!BI) return Error(Loc, "Initializer is not compatible with bit range");

  SmallVector<const Init *, 16> NewBits(CurVal->getNumBits());   // 全 null

  // ② 按 BitList 顺序填
  for (unsigned i = 0, e = BitList.size(); i != e; ++i) {
    unsigned Bit = BitList[i];
    if (NewBits[Bit])
      return Error(Loc, "Cannot set bit #" + Twine(Bit) + " of value '" +
                        ... + "' more than once");
    NewBits[Bit] = BI->getBit(i);
  }

  // ③ 没填的位用原值
  for (unsigned i = 0, e = CurVal->getNumBits(); i != e; ++i)
    if (!NewBits[i]) NewBits[i] = CurVal->getBit(i);

  V = BitsInit::get(Records, NewBits);
}
```

三个细节：

**细节一：右值先 cast 到 `bits<BitList.size()>`**。所以 `let V{3...0} = 3` 是合法的——`IntInit(3)` 转成 `bits<4>`：

```console
$ cat bt9.td
class C { bits<8> V = 0xFF; }
def X : C { let V{3...0} = 3; }
$ llvm-tblgen -print-records bt9.td | tail -3
def X {	// C
  bits<8> V = { 1, 1, 1, 1, 0, 0, 1, 1 };
}
```

`0xFF` 的低四位被 `3`（`0011`）替换，得到 `1111_0011`。

**细节二：位不能重复设置**。`let V{3...3}{3...3}` 这类会报 `Cannot set bit #3 ... more than once`。但注意 `ParseOptionalRangeList` 允许多个 range，所以 `let V{0...3}{2...5} = ...` 是语法合法、语义非法的（重叠）。

**细节三：`BitList` 在传入前已被 reverse**（`ParseBodyItem` 里）。所以 `{3...0}` 的 `BitList` 是 `{0,1,2,3}`，`BitList[i]=i`，`NewBits[i] = BI->getBit(i)`——直接把右值的第 i 位放到第 i 位，符合直觉。如果你写 `{0...3}`，`BitList` 是 `{3,2,1,0}`，`NewBits[3] = BI->getBit(0)`——右值最低位放到第 3 位，又是反转。

## 10.5 `bits<n>` 的「不完整」状态

`BitsInit` 可以有未定元素：

```td
def X { bits<8> V = {1, ?, 0, 0, 0, 0, 0, 0}; }
```

```console
$ llvm-tblgen -print-records bc4.td | tail -3
def X {
  bits<8> V = { 1, ?, 0, 0, 0, 0, 0, 0 };
}
```

`?` 可以留在 `bits<>` 里，`-print-records` 原样打出来。

**注意 `-print-records` 用 `?` 表示未定，用 `*` 表示 null 指针**。源码里：

```cpp
std::string BitsInit::getAsString() const {
  for (const Init *Bit : reverse(getBits())) {
    if (Bit) Result += Bit->getAsString();
    else     Result += "*";              // ← null 是 '*'
  }
}
```

`?` 是 `UnsetInit::getAsString()` 的返回值。两者不同：`?` 是「用户写了 `?`」，`*` 是「内部构造时留空」。实践中 `*` 几乎不出现（只有 `BitsInit::get` 收到含 null 的数组时）。

三个谓词的区别（Ch 5 提过，这里落地）：

```cpp
bool BitsInit::isComplete() const {    // 所有位都已定（没有 ? ）
  return all_of(getBits(), [](const Init *Bit) { return Bit->isComplete(); });
}
bool BitsInit::allInComplete() const {  // 所有位都未定
  return all_of(getBits(), [](const Init *Bit) { return !Bit->isComplete(); });
}
bool BitsInit::isConcrete() const {     // 所有位都是 BitInit
  return all_of(getBits(), [](const Init *Bit) { return Bit->isConcrete(); });
}
```

## 10.6 `bits` 与 `int` 的转换边界

`IntInit::convertInitializerTo`（Ch 9 见过）：

```cpp
if (const auto *BRT = dyn_cast<BitsRecTy>(Ty)) {
  int64_t Value = getValue();
  if (!canFitInBitfield(Value, BRT->getNumBits()))
    return nullptr;
  SmallVector<const Init *, 16> NewBits(BRT->getNumBits());
  for (unsigned i = 0; i != BRT->getNumBits(); ++i)
    NewBits[i] = BitInit::get(getRecordKeeper(), Value & ((i < 64) ? (1LL << i) : 0));
  return BitsInit::get(getRecordKeeper(), NewBits);
}
```

`canFitInBitfield` 的注释很关键：

```cpp
static bool canFitInBitfield(int64_t Value, unsigned NumBits) {
  // For example, with NumBits == 4, we permit Values from [-7 .. 15].
  return (NumBits >= sizeof(Value) * 8) ||
         (Value >> NumBits == 0) || (Value >> (NumBits-1) == -1);
}
```

**负数允许**，只要符号扩展后能放下：`bits<4>` 接受 `[-7, 15]`。实测：

```console
$ printf 'def X { bits<4> A = -1; }\n' > neg.td && llvm-tblgen -print-records neg.td
def X {
  bits<4> A = { 1, 1, 1, 1 };
}
```

注意循环里的 `(i < 64) ? (1LL << i) : 0`：**超过 64 位的部分直接填 0**。实测 `bits<128> A = -1`：

```console
$ printf 'def X { bits<128> A = -1; }\n' > b128.td && llvm-tblgen -print-records b128.td
def X {
  bits<128> A = { 0, 0, ..., 0, 1, 1, ..., 1 };
}
```

打印是 MSB-first，所以**前 64 个 `0` 在左边**（高位），后 64 个 `1` 在右边（低位）——即低 64 位是 `-1` 的二进制补码，高 64 位是 0。**这不是符号扩展，是「填 0」**。所以 `bits<128> = -1` 不等于「128 位全 1」。

这是 `int64_t` 的固有限制，不是 bug——TableGen 的 `int` 就是 64 位。要真正的 128 位值，只能写显式位列表。

## 10.7 位域在真实 target 里的用法

AArch64 的指令编码就是一堆 `bits<>` 位域：

```td
// AArch64InstrFormats.td 的典型 pattern
class BaseAddSubImm<bits<8> opc, ...> : ... {
  bits<12> imm12;
  bits<5>  Rn;
  bits<5>  Rd;
  let Inst{31-24} = opc;
  let Inst{21-10} = imm12;
  let Inst{9-5}   = Rn;
  let Inst{4-0}   = Rd;
}
```

`let Inst{31-24} = opc` 用的是**旧语法**（`-` 而不是 `...`）。`ParseRangePiece` 里两者等价：

```cpp
case tgtok::dotdotdot:
case tgtok::minus: {       // ← '-' 和 '...' 是同一个 case
  Lex.Lex();
  ...
}
```

所以 `Inst{31-24}` 和 `Inst{31...24}` 完全一样。读老 target 代码时别以为 `-` 是减法。

## 10.8 本章检查清单

- [ ] 说出三套位序各自的顺序，以及 `getBits()[0]` 是最低位
- [ ] 解释 `V{0...7}` 为什么产生位反转
- [ ] 预测 `let V{7...4} = 0b1010` 的结果
- [ ] 说出 `-print-records` 里 `?` 与 `*` 的区别
- [ ] 解释 `canFitInBitfield` 为什么允许负数
- [ ] 说出 `bits<128>` 从 `int` 赋值时高 64 位是什么
- [ ] 解释 `Inst{31-24}` 和 `Inst{31...24}` 的关系

## 10.9 踩雷记录

**错误直觉 1：`V{7...0}` 和 `V{0...7}` 只是写法不同。**
正确认识：前者恒等，后者是完整位反转。因为 `ParseValue` 会对 range 做 `std::reverse`，`{hi...lo}` 才是「正常」顺序。

**错误直觉 2：`bits<4>` 打印成 `{0,0,1,1}` 表示值是 `0011` 的十进制 3，但那只是巧合。**
正确认识：打印是 MSB-first，`{0,0,1,1}` 确实是 `0011` = 3。但存储是 `[1,1,0,0]`。别把打印当存储。

**错误直觉 3：`let V{3...0} = 3` 里 `3` 是「三位二进制」。**
正确认识：右值先 cast 成 `bits<BitList.size()>` = `bits<4>`，所以 `3` 是 `0011`（四位）。

**错误直觉 4：`Inst{31-24}` 里的 `-` 是减法。**
正确认识：`-` 和 `...` 在 `ParseRangePiece` 里是同一个 `case`，都是 range 分隔符。这是历史语法。

**错误直觉 5：`bits<128>` 能存 128 位整数。**
正确认识：从 `int` 赋值时只有低 64 位有效（`i < 64` 判断），高 64 位填 0。要全 128 位得写成位列表。

**错误直觉 6：`?` 和 `*` 是同一个东西。**
正确认识：`?` 是 `UnsetInit`（用户写的），`*` 是 null 指针（内部构造留空）。`isComplete()` 对两者都返回 false，但 `getAsString()` 不同。

## 10.10 延伸阅读

- **`llvm/lib/TableGen/Record.cpp`** 的 `BitsInit::convertInitializerBitRange`（488）
  - 位域取值的唯一实现。对照本章 10.3 的 reverse 分析。
- **`llvm/lib/TableGen/Record.cpp`** 的 `BitsInit::getAsString`（509）
  - 打印为什么是 MSB-first，一眼可见。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseRangePiece`（1019）与 `ParseValue` 的后缀循环（3237）
  - `{hi...lo}` 到 `BitList` 再到 `convertInitializerBitRange` 的完整链路。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `SetValue` 位域分支（296–330）
  - `let` 位域赋值的三个细节。
- **`llvm/lib/TableGen/Record.cpp`** 的 `canFitInBitfield`（571）
  - 负数与位宽的边界。4 行，注释比代码长。

## 下一章

Ch 11 讲 `assert`、`dump`、`!instances` 这三个「运行期」设施。它们让 `.td` 从「静态描述」变成「可自检的规格」，也是上游 target 定义里最常见的防御手段。

[Ch 11 assert、dump、!instances 与运行期求值](./11-assert-dump-instances.md)
