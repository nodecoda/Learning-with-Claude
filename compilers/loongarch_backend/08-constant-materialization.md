# Ch 08 — 常量物化：一个 `i64` 怎么变成 1–5 条指令

> 本章目标：把 `LoongArchMatInt::generateInstSeq()` 这张“贪心表”读透。你会知道为什么 `1` 是 `ori`、`-1` 是 `addi.w`、`2^52` 是一条 `lu52i.d`，以及为什么 `2^52-1` 会多出一条看着没用的 `lu52i.d $a0,$a0,0`。

---

## 8.1 为什么单独有“常量物化”这一层

LoongArch 没有“把 64 位立即数放进寄存器”的指令。最小的装载粒度是：

| 指令 | 作用 | 立即数位宽 |
|---|---|---|
| `ori rd, rj, imm12` | `rd = rj | imm`（`rj=$zero` 时就是装 12 位） | 12（零扩展） |
| `addi.w rd, rj, imm12` | `rd = sext32(rj + imm)` | 12（符号扩展，结果截 32 位再符号扩展） |
| `lu12i.w rd, imm20` | `rd = sext64(imm20 << 12)` | 20（低 12 位清零） |
| `lu32i.d rd, imm20` | `rd[51:32] = imm20`，保留其它位 | 20（只改中间 20 位） |
| `lu52i.d rd, rj, imm12` | `rd[63:52] = imm12`，需要 `rj` | 12（只改最高 12 位） |
| `bstrins.d rd, rj, msb, lsb` | 位段插入，用来合并两条指令 | — |

于是“装一个常量”本质是**在 64 位里分四段拼**：

```text
| Highest12 |  Higher20  |   Hi20    |   Lo12   |
63        52 51        32 31        12 11       0
```

`generateInstSeq()` 开头的注释就是这张图（`MCTargetDesc/LoongArchMatInt.cpp:15`）。

---

## 8.2 一份代码，三个调用者

`LoongArchMatInt` 放在 `MCTargetDesc/`，不是 `Utils/`，这是有意的：它同时被 MC 层和 CodeGen 层使用。

| 调用者 | 位置 | 场景 |
|---|---|---|
| SelectionDAG 选指 | `LoongArchISelDAGToDAG.cpp:64` | `ISD::Constant` → 机器指令序列 |
| 机器层 `movImm` | `LoongArchInstrInfo.cpp:280` | 寄存器分配后插入常量（如 `COPY` 物化、spill 填充） |
| MC 层伪指令 | `AsmParser/LoongArchAsmParser.cpp:1539` | `li.w` / `li.d` 展开 |
| Inline asm / 特殊 lowering | `LoongArchISelLowering.cpp:11095` | 需要立即数序列的 lowering 场景 |

**这解释了一个常见困惑**：`li.d 0x123456789` 在 `llvm-mc` 里能展开，在 `llc` 输出里却看不到 `li.d`——因为两条路径调的是同一个 `generateInstSeq()`，只是 `llc` 在选指阶段就已经展开了。

---

## 8.3 算法逐段读

```cpp
const int64_t Highest12 = Val >> 52 & 0xFFF;
const int64_t Higher20  = Val >> 32 & 0xFFFFF;
const int64_t Hi20      = Val >> 12 & 0xFFFFF;
const int64_t Lo12      = Val & 0xFFF;
```

### 第一优先级：`LU52I_D` 单条早退

```cpp
if (Highest12 != 0 && SignExtend64<52>(Val) == 0) {
  Insts.push_back(Inst(LoongArch::LU52I_D, SignExtend64<12>(Highest12)));
  return Insts;
}
```

条件含义：**低 52 位全 0**（`Val` 只有最高 12 位非零）。这时 `lu52i.d rd, $zero, Highest12` 一条就够。

实测 `2^52`：

```llvm
define i64 @p52() { ret i64 4503599627370496 }
```

```text
p52:
	lu52i.d	$a0, $zero, 1
	ret
```

### 第二优先级：低 32 位

```cpp
if (Hi20 == 0)
  Insts.push_back(Inst(LoongArch::ORI, Lo12));
else if (SignExtend32<1>(Lo12 >> 11) == SignExtend32<20>(Hi20))
  Insts.push_back(Inst(LoongArch::ADDI_W, SignExtend64<12>(Lo12)));
else {
  Insts.push_back(Inst(LoongArch::LU12I_W, SignExtend64<20>(Hi20)));
  if (Lo12 != 0)
    Insts.push_back(Inst(LoongArch::ORI, Lo12));
}
```

三个分支对应三种形态：

1. **`Hi20 == 0`**：值小于 4096（或低 32 位高位全 0）→ `ori rd, $zero, Lo12`。
2. **`ADDI_W` 能覆盖**：`Lo12` 的符号扩展恰好等于 `Hi20`——即 `Hi20` 是 `Lo12` 的符号扩展，说明 `Hi20<<12 | Lo12` 可以由一个 12 位有符号数经 `addi.w` 得到（`addi.w` 会做 32 位符号扩展）。
3. **否则**：`lu12i.w` 装高 20 位，再按需 `ori` 补低 12 位。

实测：

| 常量 | 输出 | 走的分支 |
|---|---|---|
| `1` | `ori $a0, $zero, 1` | `Hi20==0` |
| `-1` | `addi.w $a0, $zero, -1` | `ADDI_W` |
| `0x1234567890` | `lu12i.w $a0, 214375` + `ori $a0, $a0, 2192` | 第三分支 |
| `0` | `move $a0, $zero` | 选指层特判（见 8.5） |

`0x1234567890` 的拆解：`Hi20 = 0x34567 = 214375`，`Lo12 = 0x890 = 2192`。编码上 `lu12i.w` 的立即数就是 `0x34567`，左移 12 位得到 `0x34567000`，`ori` 补 `0x890`。

### 第三优先级：高 32 位

```cpp
if (SignExtend32<1>(Hi20 >> 19) != SignExtend32<20>(Higher20))
  Insts.push_back(Inst(LoongArch::LU32I_D, SignExtend64<20>(Higher20)));

if (SignExtend32<1>(Higher20 >> 19) != SignExtend32<12>(Highest12))
  Insts.push_back(Inst(LoongArch::LU52I_D, SignExtend64<12>(Highest12)));
```

这两个 `if` 的判断逻辑一样：**已经算出来的高位是否已经“隐含”了下一段应有的符号扩展**。如果隐含了，就不用再发指令；否则补一条。

这里有一个必须理解的点：`lu12i.w` 会把结果**符号扩展到 64 位**，`addi.w` 也会把 32 位结果符号扩展。所以“低 32 位算完”之后，高 32 位并不一定是 0——它等于 `Hi20` 的符号位复制。`Higher20` 的比较就是在检查这一点。

---

## 8.4 反直觉点：会生成“多余”的指令

实测 `2^52 - 1`：

```llvm
define i64 @p52m1() { ret i64 4503599627370495 }
```

```text
p52m1:
	addi.w	$a0, $zero, -1
	lu52i.d	$a0, $a0, 0
	ret
```

第二条 `lu52i.d $a0, $a0, 0` 把最高 12 位设成 0——而 `addi.w -1` 的结果已经是 `0xFFFFFFFFFFFFFFFF`，所以这条指令**在语义上是多余的**。

为什么还发？因为判断条件是：

```cpp
SignExtend32<1>(Higher20 >> 19) != SignExtend32<12>(Highest12)
```

`Higher20 = 0xFFFFF`，`Higher20 >> 19 = 1`，`SignExtend32<1>(1) = -1`；`Highest12 = 0`，`SignExtend32<12>(0) = 0`。`-1 != 0`，于是发 `LU52I_D 0`。

这里做的是**局部符号扩展一致性检查**，不是全局最优。它保证正确性，但不保证最少指令数。后续的 `MachineCombiner`/`Peephole` 可能删掉它（取决于 pass 和上下文），但在 `-O2` 的这段输出里它还在。

**这正是可以贡献的地方**：如果你能证明“当 `Highest12 == 0` 且 `Higher20` 的符号位为 1 时，`LU52I_D 0` 一定冗余”，就可以省一条指令。但要非常小心 LA32/LA64 和 `sext` 语义，并补上 `llvm/test/CodeGen/LoongArch/` 的回归。这类“少一条指令”的补丁在上游很受欢迎，也很容易被 reviewer 要求给出基准数据。

---

## 8.5 `BSTRINS_D`：三条并两条

当序列长度 ≥ 3 时，`generateInstSeq()` 尝试用一条 `bstrins.d` 合并掉两条：

```cpp
// N of Insts = 3
// 1. ORI + LU32I_D + LU52I_D     =>     ORI + BSTRINS_D
// 2. ADDI_W + LU32I_D + LU52I_D  =>  ADDI_W + BSTRINS_D
// 3. LU12I_W + ORI + LU32I_D     =>     ORI + BSTRINS_D
// 4. LU12I_W + LU32I_D + LU52I_D => LU12I_W + BSTRINS_D
```

思路：先把已经能算出的部分（`TmpVal1`/`TmpVal2`）当作“底”，再枚举 `[Msb:Lsb]` 这段，看能不能用一条位段插入把剩下的位补齐。`Inst` 里把 `Msb << 32 | Lsb` 打包成一个立即数，展开时再拆：

```cpp
case LoongArch::BSTRINS_D:
  BuildMI(...).addReg(SrcReg).addReg(SrcReg)
      .addImm(Inst.Imm >> 32).addImm(Inst.Imm & 0xFF);
```

注意 `Msb`/`Lsb` 在 LoongArch 的 `bstrins.d` 里是 6 位字段（0–63），所以打包/解包用 `>>32` 和 `&0xFF` 是安全的。

`bstrins.d` 也大量出现在其它 lowering 里（比如 `atomicrmw` 的 clamp 序列），但那不是常量物化，别混。

---

## 8.6 `0` 的特判：不是 `ori`，是 `move $a0, $zero`

`LoongArchISelDAGToDAG::Select()`：

```cpp
case ISD::Constant: {
  int64_t Imm = cast<ConstantSDNode>(Node)->getSExtValue();
  if (Imm == 0 && VT == GRLenVT) {
    SDValue New = CurDAG->getCopyFromReg(CurDAG->getEntryNode(), DL,
                                         LoongArch::R0, GRLenVT);
    ReplaceNode(Node, New.getNode());
    return;
  }
```

实测：

```text
zero:
	move	$a0, $zero
	ret
```

为什么特判？因为 `$r0` 恒为 0，直接 `COPY` 比发 `ori rd,$zero,0` 更省事，也给后续寄存器分配/拷贝合并留了空间。**这也说明 `generateInstSeq(0)` 本身会返回 `ORI 0`**，只是选指层不会走到它。

---

## 8.7 LA32 与 LA64 的差别

`LoongArchInstrInfo::movImm()` 里有一句硬检查：

```cpp
if (!STI.is64Bit() && !isInt<32>(Val))
  report_fatal_error("Should only materialize 32-bit constants for LA32");
```

`getSExtValue()` 之后：

- **LA64**：走完整四段。
- **LA32**：只允许 32 位有符号范围内的值；`lu32i.d`/`lu52i.d` 不存在（`Feature64Bit` 才提供）。
- **`li.w`**：`emitLoadImm()` 里先 `Imm = SignExtend64<32>(Imm)`，再走同一个生成器。所以 `li.w 0xffffffff` 等价于 `li.d -1`，两者都打印成 `addi.w $a0, $zero, -1`。

实测 `li.w` 与 `li.d` 的差别（这一点非常容易误判）：

```console
$ llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding
li.w $a0, 0xffffffff
li.d $a0, -1
li.w $a0, 0x80000000
li.d $a0, 0x80000000
```

```text
	addi.w	$a0, $zero, -1                  # encoding: [0x04,0xfc,0xbf,0x02]
	addi.w	$a0, $zero, -1                  # encoding: [0x04,0xfc,0xbf,0x02]
	lu12i.w	$a0, -524288                    # encoding: [0x04,0x00,0x00,0x15]
	lu12i.w	$a0, -524288                    # encoding: [0x04,0x00,0x00,0x15]
	lu32i.d	$a0, 0                          # encoding: [0x04,0x00,0x00,0x16]
```

注意最后一行：`li.d 0x80000000` 需要 `lu32i.d 0` 把高 32 位清零，而 `li.w 0x80000000` 只需要 `lu12i.w`。这是 `li.w` 先做 32 位符号扩展的直接后果，也是 8.4 那个“多余指令”问题的另一个实例。

LA32 上的 `i64` 更特殊：`i64` 返回值会被拆成两个 GPR（`$a0`/`$a1`）。实测：

```llvm
define i64 @x() { ret i64 4294967296 }
```

```console
$ llc -mtriple=loongarch32-unknown-linux-gnu -O2
	ori	$a1, $zero, 1
	move	$a0, $zero
	ret
```

高 32 位进 `$a1`，低 32 位进 `$a0`。这部分在 Ch 09 展开。

实测 `i32` 常量：

```llvm
define i32 @wbig() { ret i32 305419896 }
```

```text
wbig:
	lu12i.w	$a0, 74565
	ori	$a0, $a0, 1656
	ret
```

`74565 = 0x12345`，`1656 = 0x678`——`0x12345678` 的拆法。注意即使结果是 `i32`，`lu12i.w` 也会把它符号扩展到 64 位，但在 `i32` 返回约定下高 32 位被忽略。

---

## 8.8 从 IR 到指令的完整追踪

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cat > /tmp/c08.ll <<'EOF'
define i64 @cbig() { ret i64 78187493520 }
define i64 @p52()  { ret i64 4503599627370496 }
define i64 @p52m1(){ ret i64 4503599627370495 }
EOF

# 1) 看 DAG 里的 Constant
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -print-before=loongarch-isel /tmp/c08.ll -o /dev/null 2>&1 | grep -E 'Constant|ret i64'

# 2) 看选指后的 MIR（已经是展开好的序列）
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -stop-after=loongarch-isel /tmp/c08.ll -o - 2>/dev/null | grep -E 'LU12I_W|ORI|LU32I_D|LU52I_D|ADDI_W|BSTRINS_D'

# 3) 看最终汇编
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 /tmp/c08.ll -o -
```

实测第 3 步：

```text
cbig:
	lu12i.w	$a0, 214375
	ori	$a0, $a0, 2192
	lu32i.d	$a0, 18
	ret
p52:
	lu52i.d	$a0, $zero, 1
	ret
p52m1:
	addi.w	$a0, $zero, -1
	lu52i.d	$a0, $a0, 0
	ret
```

`78187493520 = 0x1234567890`：`Hi20=0x34567`，`Lo12=0x890`，`Higher20=0x12=18`，`Highest12=0`。

---

## 8.9 本章证据

| 结论 | 命令/源码 |
|---|---|
| 四段划分与注释 | `MCTargetDesc/LoongArchMatInt.cpp:15-22` |
| `LU52I_D` 早退 | 同上 `:24-27` |
| 低 32 位三分支 | 同上 `:29-40` |
| 高 32 位两道检查 | 同上 `:42-50` |
| `BSTRINS_D` 合并 | 同上 `:56-140` |
| 三个调用者 | `ISelDAGToDAG.cpp:64`、`InstrInfo.cpp:280`、`AsmParser.cpp:1539` |
| `0` 特判 | `LoongArchISelDAGToDAG.cpp:52-60` |
| LA32 限制 | `LoongArchInstrInfo.cpp:277-279` |
| `li.w` 符号扩展 | `LoongArchAsmParser.cpp:1534-1536` |
| 实测序列 | 本章 8.8 的 `llc` 输出 |

---

## 8.10 练习

1. 手工推导 `0x7fffffffffffffff`、`0x8000000000000000`、`0xdeadbeef` 的指令序列，再用 `llvm-mc`/`llc` 验证。
2. 找出一个会触发 `BSTRINS_D` 的常量（提示：三条序列且能合并），给出 `-stop-after=loongarch-isel` 的证据。
3. 解释 `p52m1` 里 `lu52i.d $a0, $a0, 0` 为什么不是 bug；再尝试构造一个能证明它冗余的补丁思路（先别改树）。
4. `li.w 0x80000000` 和 `li.d 0x80000000` 生成的序列一样吗？为什么？
5. 在 LA32 triple（`loongarch32-unknown-linux-gnu`）上试 `ret i64 0x1_0000_0000`，记录 `llc` 的行为。

---

## 8.11 延伸阅读

1. **`generateInstSeq()` 的完整分支** — `llvm/lib/Target/LoongArch/MCTargetDesc/LoongArchMatInt.cpp`
   - 读什么：15–22 行的四段注释、24–27 行的 `LU52I_D` 早退、29–40 行的低 32 位三分支、42–50 行的高 32 位两道检查，以及 56–140 行的 `BSTRINS_D` 合并枚举（注释 60–72 行列了 5 种场景：N=3 的 4 种加 N=4 的 1 种）。
   - 学到什么：本章 8.3 只逐段贴了主分支；源码里 `BSTRINS_D` 的完整场景表和 `Inst` 里 `Msb << 32 | Lsb` 的打包方式值得完整读一遍，能补上 8.5 没展开的 N=4 情形以及 `TmpVal1`/`TmpVal2` 的构造。
   - 前提：读过本章 8.3 与 8.5。

2. **MC 层伪指令的对照输出** — `llvm/test/MC/LoongArch/Macros/macros-li.s`
   - 读什么：`li.w` / `li.d` 成对出现的 `CHECK` 行，重点看 `0x80000000` 那一组（`lu12i.w` + `lu32i.d 0`）和 `0xfffff800` 那一组（`addi.w`）。
   - 学到什么：本章 8.7 说“`li.w` 先做 32 位符号扩展”；这份测试把同一条 `generateInstSeq()` 在两个伪指令下的差异逐条固定下来，可以直接当反例表用。
   - 前提：读过本章 8.2 的“一份代码，三个调用者”。

3. **SelectionDAG 侧的常量选择** — `llvm/test/CodeGen/LoongArch/imm.ll`
   - 读什么：`imm0`、`imm7ff0000000000000` 等函数的 `CHECK-NEXT` 序列，重点看 `0` 的 `move $a0, $zero` 和 `2^52` 的 `lu52i.d`。
   - 学到什么：本章 8.6 的 `0` 特判和 8.3 的单条早退在 `llc` 输出里长什么样；这份测试由 `update_llc_test_checks.py` 生成，可以对照本章 8.8 的手工命令看同一批常量。
   - 前提：会读 FileCheck 的 `CHECK-LABEL`/`CHECK-NEXT`。

4. **立即数字段宽度与位域语义** — `compilers/tablegen_internals/README.md`（相对 `/home/ubuntu/Learning-with-Claude`）
   - 读什么：Part 2 的 Ch 10《位域、`bits<>` 与字段偏移的语义》。
   - 学到什么：本章 8.5 提到 `bstrins.d` 的 `Msb`/`Lsb` 是 6 位字段、`lu52i.d` 的立即数是 12 位；这门课解释 TableGen 的 `bits<N>` 和字段偏移如何决定这些范围，避免把“指令语义”和“编码宽度”混为一谈。
   - 前提：读过本章 8.1 的字段图和 8.5 的打包代码。

## 8.12 下一步

Part 1 的正文到这里结束。接下来是**练习 A**：真的往 `.td` 里加一条指令，重编 `llc`/`llvm-mc`，跑通“汇编 → 编码 → 反汇编”的往返，并补一条 MC 测试。做完练习 A，你就有能力独立处理 `[LoongArch]` 的 MC 层补丁了。
