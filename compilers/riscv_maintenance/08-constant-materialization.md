# Ch 08 — 常量与地址物化

> **目标**：把「一个 64 位立即数怎么变成 1–8 条指令」拆到可手算的程度；能在 `RISCVMatInt` 里指出某一类常数走哪条分支、为什么 `0x800` 不是一条 `lui`、为什么最坏是 8 条；能区分「常量物化」（算出数值）与「地址物化」（算出符号地址）；知道 `getIntMatCost` 被谁用来做决策。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`。本章所有「实测」都来自这套构建；跑不了的会标「未实测」。

> **前置**：Ch 04（指令编码）、Ch 06（MC 层）、Ch 07（汇编器伪指令展开）。地址部分会用到 Ch 11 的引子。

---

## 8.1 为什么维护者要读常量物化

`lui` 的立即数是 20 位、被放到 32 位字的 `[31:12]`；`addi` 的立即数是 **12 位、有符号**，范围 −2048..2047。这两句话就是本主题全部的麻烦来源：

1. **没有「装 32 位立即数」的指令**，任何超过 12 位的值都得切成「高 20 + 低 12」；低位是符号扩展的，所以高位要向上取整（`+0x800`），低位可能变成负数。切法不唯一，边界值就多。
2. **RV64 的 `lui` 会符号扩展 bit 31**：`lui a0, 0x80000` 得到 `0xFFFFFFFF80000000`，不是 `0x80000000`。很多「RV32 对、RV64 错」的补丁死在这里（ISA 语义，本章未实测执行）。
3. **一份算法被四个消费者共用**：SelectionDAG ISel、GlobalISel、`llvm-mc`、`RISCVAsmPrinter`。改一行可能同时改到 C 代码、内联汇编、`.s`、`llvm.experimental.patchpoint` 的跳转地址。
4. **扩展让「成本」不等于「条数」**：`+zba`/`+zbs`/`+zbb` 各能省一条，但互斥关系与优先级只有读代码才知道。

| 文件 | 行数 | 内容 |
|---|---|---|
| `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.h` | 83 | `Inst`、`InstSeq`、四个公开入口 |
| `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.cpp` | 630 | 递归分解 + 十余种优化尝试 |

注意路径在 **`MCTargetDesc/`** 下：这正是为了让 `llvm-mc` 能链接它（8.9 节的前提）。

---

## 8.2 先建立直觉：64 位值 → 12 位切分 → 指令序列

```
Val = 0xdeadbeefcafebabe
        │
        ├─ 低 12 位 0xabe ──sext12──► -1346          ← 最后一条 addi 的操作数
        ├─ Val - (-1346) = 0xdeadbeefcafec000
        │      └─ countr_zero = 14 ──► slli 14       ← 把已装好的高位推上去
        └─ 剩余 ──递归──► ... 直到落在 32 位内：lui 0xffdeb + addi -577
        ▼
  [2 条：装高 32 位]   [3 组 slli+addi = 6 条：逐段补低 12 位]
lui 0xffdeb / addi -577 / slli 14 / addi -1037 / slli 12 / addi -1029 / slli 14 / addi -1346
```

三条推论，后面每一节都要用：

- **递归从 LSB 往 MSB 走，发射从 MSB 往 LSB 走**，所以 `InstSeq` 第一条永远是最高位那条、最后一条是 `addi Lo12`（源码注释 `RISCVMatInt.cpp:151-172`）。
- **每条 `addi` 只能补 12 位**，所以 64 位最多 1 条 `lui` + 1 条 `addi`（32 位）+ 3 组（`slli`+`addi`）= 8 条。`InstSeq = SmallVector<Inst, 8>` 里的 8 不是随手写的。
- **`addi` 是符号扩展加法**，所以不能「先装高 32 位再一路补 12 位非零字段」——那样只能用 11 位。要吃到完整 12 位，必须从最低位开始处理，这就是递归方向反直觉的原因。

---

## 8.3 `Inst` 与四个入口

`RISCVMatInt.h:22-42` 里 `Inst` 不是 `MachineInstr` 也不是 `MCInst`，而是「操作数形状」的描述：

```cpp
enum OpndKind {
  RegImm, // ADDI/ADDIW/XORI/SLLI/SRLI/SLLI_UW/RORI/BSETI/BCLRI/TH_SRRI
  Imm,    // LUI/QC_LI/QC_E_LI/PLI_B/PLI_H/PLI_W
  RegReg, // SH1ADD/SH2ADD/SH3ADD/PACK
  RegX0,  // ADD_UW
};
class Inst {
  unsigned Opc;
  int32_t Imm; // The largest value we need to store is 32 bits for QC_E_LI.
public:
  Inst(unsigned Opc, int64_t I) : Opc(Opc), Imm(I) { assert(I == Imm && "truncated"); }
```

`getOpndKind()`（`.cpp:596-628`）把 opcode 映射到上面四类，调用者据此填操作数。这就是同一个序列既能在 ISel 里变 DAG 节点、又能在 MC 里变 `MCInst` 的原因。

```cpp
using InstSeq = SmallVector<Inst, 8>;                                      // .h:43
InstSeq generateInstSeq(int64_t Val, const MCSubtargetInfo &STI);          // .h:50
void    generateMCInstSeq(int64_t, const MCSubtargetInfo &, MCRegister, SmallVectorImpl<MCInst> &);
InstSeq generateTwoRegInstSeq(int64_t, const MCSubtargetInfo &, unsigned &, unsigned &);   // .h:61
int     getIntMatCost(const APInt &, unsigned Size, const MCSubtargetInfo &,
                      bool CompressionCost = false, bool FreeZeroes = false);  // .h:79
```

签名里只有一个上下文依赖 `MCSubtargetInfo`：**没有 `MachineFunction`、没有 `TargetMachine`**。`.h:45-49` 把理由写死了："to allow this helper to be used from both the MC layer and during instruction selection"。

`generateInstSeq`（`.cpp:294-505`）本身只做两件事：调 `generateInstSeqImpl`，再依次尝试一串「整段序列替换」优化——**顺序就是优先级**：

| 优化 | 行号 | 触发 | 产出 |
|---|---|---|---|
| 尾部零 + `slli` | 294-318 | 低 12 位非零且为偶数 | `li c` + `slli` 取代 `lui`+`addi` |
| `0x17ff` 调整 | 328-344 | `(Val & 0x1800) == 0x1000` | 先 ±δ 再补一条 `addi` |
| 前导零 / 取反 | 346-364 | 正值 >2 条 / 负值 >3 条 | `srli` / `xori -1` |
| `pack` | 366-381 | 高 32 位 == 低 32 位 | `pack` |
| `bseti` / `bclri` | 383-431 | `+zbs`，逐位补/清 | `bseti` / `bclri` |
| `sh1/2/3add` | 433-486 | `+zba`，值可被 3/5/9 整除 | `sh*add` |
| `rori` | 488-503 | `+zbb` 或 `+xtheadbb` | `addi` + `rori` |

`generateInstSeqImpl`（`.cpp:53-223`）内部才是真分解，前面还有几道整条指令快路：`+zbs` 单比特 `bseti`（57-62）、Xqci 的 `qc.li`/`qc.e.li`（64-80）、P 扩展 `pli.*`（82-118）、`≤32` 位（120-147）、`>32` 位递归（149-223）。

---

## 8.4 32 位以内：`lui`/`addi` 的两种分解

`.cpp:120-147` 是全章最该背下来的 28 行（首行注释已压成一行）：

```cpp
// v == 0 : ADDI ; v 落在 simm12 : ADDI ; v[0,12)==0 且 v[12,32)!=0 : LUI ; 其余 : LUI+ADDI(W)
int64_t Hi20 = ((Val + 0x800) >> 12) & 0xFFFFF;
int64_t Lo12 = SignExtend64<12>(Val);
if (Hi20) Res.emplace_back(RISCV::LUI, Hi20);
if (Lo12 || Hi20 == 0) {
  unsigned AddiOpc = RISCV::ADDI;
  if (IsRV64 && Hi20) {                       // 136-143
    int64_t LuiRes = SignExtend64<32>(Hi20 << 12);
    if (!isInt<32>(LuiRes + Lo12)) AddiOpc = RISCV::ADDIW;
  }
  Res.emplace_back(AddiOpc, Lo12);
}
```

三个细节决定了很多 bug：`+0x800` 是**进位**而不是舍入误差（`Lo12` 可负，必须按 `(Val+2048)/4096` 向上取整才能保证 `Hi20<<12 + Lo12 == Val`）；`& 0xFFFFF` 防止负值算术右移后高位全 1；`ADDIW` 只在「`LUI` 符号扩展成负数、再补 `-1` 会破高半」时使用，否则保持 `ADDI` 以免和 RV32 产生无谓 diff。

实测（`llvm-mc -triple=riscv64 -mattr=+m,+c -show-encoding li.s`，`li.s` 见 8.13 实验 1）：

```asm
	li	a0, 2047                        # encoding: [0x13,0x05,0xf0,0x7f]
	li	a0, 1                           # encoding: [0x05,0x45]
	slli	a0, a0, 11                      # encoding: [0x2e,0x05]
	lui	a0, 74565                       # encoding: [0x37,0x55,0x34,0x12]
	addi	a0, a0, 1656                    # encoding: [0x13,0x05,0x85,0x67]
```

对应输入 `li a0, 0x7ff` / `0x800` / `0x12345678`。注意第二条：**`0x800` 不是一条 `lui`**，朴素分解是 `lui 1` + `addi -2048`；顶层优化（294-318）发现「低 12 位非零、偶数、`Val>>11 == 1` 能用 6 位立即数」，换成 `li 1` + `slli 11`——两条都能压成 RVC（共 4 字节）。`+zbs` 下变成一条 `bseti a0, zero, 11`（encoding `[0x13,0x15,0xb0,0x28]`）；`+zba`/`+zbb` 对这五个值**完全无影响**（实测与 `+m,+c` 逐行相同）。扩展不是越多越好，各有各的适用形状。

---

## 8.5 32 位以上：递归、移位与最坏 8 条

`.cpp:174-222` 每层做三件事：剥掉最低 12 位、按 `countr_zero` 决定一次能跳多远、把「递归结果 + `slli` + `addi`」拼起来。

```cpp
int64_t Lo12 = SignExtend64<12>(Val);
Val = (uint64_t)Val - (uint64_t)Lo12;
if (!isInt<32>(Val)) {
  ShiftAmount = llvm::countr_zero((uint64_t)Val);
  Val >>= ShiftAmount;
  if (ShiftAmount > 12 && !isInt<12>(Val)) {        // 188-202：为了能用 LUI，退 12 位
    if (isInt<32>((uint64_t)Val << 12)) { ShiftAmount -= 12; Val = (uint64_t)Val << 12; }
    else if (isUInt<32>((uint64_t)Val << 12) && STI.hasFeature(RISCV::FeatureStdExtZba)) {
      ShiftAmount -= 12; Val = SignExtend64<32>((uint64_t)Val << 12); ShiftOpc = RISCV::SLLI_UW;
    }
  }
  // 205-211：Val 是 uint32 但不是 int32 时，同样用 SLLI_UW 清高半
}
generateInstSeqImpl(Val, STI, Res);                 // 先递归（MSB 部分）
if (ShiftAmount) Res.emplace_back(ShiftOpc, ShiftAmount);
if (Lo12)        Res.emplace_back(RISCV::ADDI, Lo12);
```

`countr_zero` 是「按需跳」的关键：值越稀疏，一次 `slli` 跨得越远，序列越短。所以条数**不由位宽决定**，而由连续非零位的密度决定——8 条是上界，不是常态。`ShiftOpc` 是本章最年轻的改动：HEAD 上的 `c87a60f617dd Replace Unsigned flag in generateInstSeqImpl with ShiftOpc. NFC (#192363)` 让 `slli`/`slli.uw` 的选择显式传进递归，这种 NFC 重构正是上手该文件的切入点。

---

## 8.6 手算五例（全部实测）

**① `0x7ff`**：`Hi20 = 0`，只有 `addi 2047`。1 条，但 2047 超出 `c.li` 的 −32..31，压不了（实测仍是 4 字节 `0x7ff00513`）。

**② `0x800`**：真值边界——`2047` 是 `sext12` 上界，`2048` 已越界。三种实测物化（`;` 表示序列）：

```asm
generic       li a0, 1 ; slli a0, a0, 11
-mattr=+zbs   bseti a0, zero, 11
p670 -zbs     lui a0, 1 ; addi a0, a0, -2048
```

默认选 `li`+`slli`：`c.li`+`c.slli` 共 4 字节；`lui`+`addi` 那版虽把 `lui` 压成 `c.lui`，合计仍 6 字节。`.cpp:304-309` 解释了为什么该判决只看 `TuneLUIADDIFusion`、不看 `+c`：避免有无 C 扩展的目标产生代码差异（融合开关的另一例见 8.14 第 6 条）。

**③ `0x12345678`**：`Hi20 = 0x12345 = 74565`、`Lo12 = 0x678 = 1656`，`lui`+`addi` 2 条。

**④ `0x12345fff`**：`Lo12 = sext12(0xfff) = -1`、`Hi20 = 0x12346 = 74566`，于是 `lui 74566` + `addi -1`，**2 条**。常见误解是「低位 `0xfff` 得再借一层、要 4 条」——实测否定：`sext12` 让 `0xfff` 直接等于 `-1`。真正要 4 条的是跨过 `0x800` 又带碎片的形状，如 `0x1000017ff` → `lui 256`/`addi 1`/`slli 12`/`addi 2047`。

**⑤ `0xdeadbeefcafebabe`（RV64 最坏情况，8 条；`llvm-mc -mattr=+m,+a,+c`）**：

```asm
	lui	a0, 1048043
	addi	a0, a0, -577
	slli	a0, a0, 14
	addi	a0, a0, -1037
	slli	a0, a0, 12
	addi	a0, a0, -1029
	slli	a0, a0, 14
	addi	a0, a0, -1346
```

递归轨迹按 `.cpp:174-222` 逐层代入：

| 层 | 进入时 `Val` | `Lo12` | `countr_zero` | 移位后 `Val` |
|---|---|---|---|---|
| 0 | `0xdeadbeefcafebabe`（−2401053089206453570） | −1346 | 14 | `0xffff7ab6fbbf2bfb`（−146548650464261） |
| 1 | `0xffff7ab6fbbf2bfb` | −1029 | 12 | `0xfffffff7ab6fbbf3`（−35778479117） |
| 2 | `0xfffffff7ab6fbbf3` | −1037 | 14 | `0xffffffffffdeadbf`（−2183745，已在 32 位内） |
| 3 | `0xffffffffffdeadbf` | −577 | — | `Hi20 = 0xffdeb` → `lui 1048043` + `addi -577` |

最后一层请验证是 `ADDI` 而非 `ADDIW`：`LuiRes = SignExtend64<32>(0xffdeb << 12) = -2183168`，加 `-577` 得 `-2183745`，仍是 `isInt<32>`。

`llc` 里要看到这 8 条必须关掉常量池（默认选 `ld`，见 8.11；夹具 `c.ll` 见 8.13）：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -riscv-constant-pool-for-large-ints=false c.ll -o -
```

输出与上面 `llvm-mc` 的 8 条**逐条一致**——这是 8.9 节的主题。

---

## 8.7 「相邻值优化」到底是什么

这个词用在三个位置，先分清，否则 review 时对不上号。

**（A）同一数值的两种 `(hi, lo)` 切法**：`Val` 可写成 `(Hi20, Lo12)` 或 `(Hi20+1, Lo12-4096)`；`+0x800` 取整就是挑「低位落在 −2048..2047」的那个。`0x12345fff` 的 `(0x12346, -1)` 与 `(0x12345, 0xfff)` 是同一常数，后者非法。

**（B）把常数挪到更好切的邻居，再补一条 `addi` 修正**（`.cpp:328-344`）：

```cpp
// 节选：lower 13 bits like 0x17ff → +1, restore with ADDI -1（省去 TmpSeq 声明）
if ((Val & 0xfff) != 0 && (Val & 0x1800) == 0x1000) {
  int64_t Imm12 = -(0x800 - (Val & 0xfff));
  generateInstSeqImpl(Val - Imm12, STI, TmpSeq);
  if ((TmpSeq.size() + 1) < Res.size()) { TmpSeq.emplace_back(RISCV::ADDI, Imm12); Res = TmpSeq; }
}
```

代价固定 +1 条，收益是让下一层 `countr_zero` 变大，因此只有严格变短才接受。

**（C）DAG 层选相邻值、让用户补偿**——现在最活跃的形态：

| 函数 | 行号 | 做法 | 补偿 |
|---|---|---|---|
| `selectNegImm` | 4526-4552 | 用 `-Imm` 代替 `Imm` | `add` 变 `sub` |
| `selectInvLogicImm` | 4554-4595 | 用 `~Imm` 代替 `Imm` | `xori -1` / `andn` |
| `selectZExtImm32` | 4503-4524 | 用 `zext` 后的值代替 | 依赖用户只做 `add`/`or` |
| `selectImm64IfCheaper` | 4490-4501 | 通用成本比较器 | 调用者决定 |

`selectImm64IfCheaper` 就是「用 `getIntMatCost` 投票」的地方：

```cpp
if (getIntMatCost(APInt(64, OrigImm), 64, *Subtarget, true) <=
    getIntMatCost(APInt(64, Imm),     64, *Subtarget, true))
  return false;
```

**必须澄清**：任务清单提到的 `generateInstSeqAdjacent` 在本树**不存在**——`grep -rn "generateInstSeqAdjacent" llvm/` 无输出，`git log -S'generateInstSeqAdjacent' -- llvm/lib/Target/RISCV` 也为空。上面的 (A)(B)(C) 就是这项能力在当前源码里的全部落点。

---

## 8.8 与扩展的交互

四段代码互不覆盖，按 8.3 的表顺序尝试，**先命中先返回**或**严格更短才替换**。

**`+zbs`**：`.cpp:57-62` 有一条快路——值是 2 的幂、且不能靠单条 `LUI`/`ADDI` 表达时直接 `bseti`，这就是 `0x800` 变一条的原因。多比特场景在 383-431，用 `popcount` 判断「1 的个数是否少于现有条数」（`echo 'li a0, 0x123456789' | $T/bin/llvm-mc -triple=riscv64 -mattr=+m,+c,+zbs`）：

```asm
	lui	a0, 144470
	addi	a0, a0, 1929
	bseti	a0, a0, 32          # 0x123456789：4 条 → 3 条
```

**`+zba`**：`sh1add rd, rs1, rs2` 算 `rs2 + (rs1<<1)`，所以 `3X`/`5X`/`9X` 一条搞定（433-455），条件是「能被 3/5/9 整除、商在 32 位内、比原序列严格少一条」。实测 `-5372288229`（`echo 'li a0, -5372288229' | $T/bin/llvm-mc -triple=riscv64 -mattr=+m`，再换 `+m,+zba` 跑一次；与 `llvm/test/CodeGen/RISCV/imm.ll:1992` 的 `RV64IZBA` 检查行一致）：

```asm
=== 基线                              === -mattr=+m,+zba
	lui	a0, 1048416                    	lui	a0, 611378
	addi	a0, a0, -437                   	addi	a0, a0, 265
	slli	a0, a0, 13                     	sh1add	a0, a0, a0
	addi	a0, a0, 795
```

另有 `slli.uw` 通路（194-211）：剩余值「uint32 但不是 int32」时用它一次清掉高 32 位（如 `0x140369ce5` → `lui 655797` / `slli.uw 1` / `addi -795`）。

**`+zbb`**：只用于「值是 12 位立即数循环移位结果」这一种形状（488-503，`extractRotateInfo` 在 225-241）。实测 `0xffff800fffffffff`（同上的 `echo ... | llvm-mc`，`-mattr=+m,+c` 对比 `+m,+c,+zbb`）：基线 3 条（`li -2047` / `slli 36` / `addi -1`），`+zbb` 后 2 条：

```asm
	li	a0, -2048
	rori	a0, a0, 28
```

**`+m` 与乘常量无关**：乘一个常量完全不经过 `RISCVMatInt`——常量本身由它物化，「乘法怎么拆」是 DAG combine 的事。实测夹具里的 `@mulc`（`llc -mattr=+m` 对比 `-mattr=-m`；`123456789 = 0x75BCD15`）：

```asm
=== +m                                  === -m
	lui	a1, 30141                       	slli	a1, a0, 2
	addi	a1, a1, -747                    	slli	a2, a0, 4
	mul	a0, a0, a1                      	... 共 20 条 shift/add/sub ...
```

两次的常量部分都是 `lui 30141` + `addi -747`；`+m` 只决定有没有 `mul` 这条指令。

---

## 8.9 一层算法，两个消费者：MC 与 CodeGen

两者确实共用 `generateInstSeq`，`grep -rn "RISCVMatInt::generate" llvm/lib/Target/RISCV/` 的两条主路径：

```
llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp:3746:  RISCVMatInt::generateMCInstSeq(Value, getSTI(), DestReg, Seq);
llvm/lib/Target/RISCV/RISCVInstrInfo.cpp:1017:           RISCVMatInt::InstSeq Seq = RISCVMatInt::generateInstSeq(Val, STI);
```

**MC 侧**：`li` 伪指令 → `PseudoLI` 分支（4497-4519）→ `emitLoadImm`（3743-3751）→ `generateMCInstSeq`（`.cpp:507-542`，把 `InstSeq` 展成 `MCInst`，第一条源是 `X0`，之后是 `DestReg`）。

**CodeGen 侧**：`selectImm`（278-311）直接用 `InstSeq` 造 DAG 机器节点；`RISCVExpandPseudoPostRA::expandMovImm`（85-100）→ `RISCVInstrInfo::movImm`（1000-1063），操作数填法与 `generateMCInstSeq` 逐行对应。

`movImm` 不只服务常量表达式：`adjustReg` 调大栈（`RISCVRegisterInfo.cpp:415, 443`）、`mulImm` 装倍乘常量（`RISCVInstrInfo.cpp:5082-5184`，`movImm` 在 5146）、stack-clash probing 装步长（`RISCVFrameLowering.cpp:2821`）都用它。

**MIR 里看不到 `li`**：常数在 ISel 阶段已展开成真实指令；只有打开实验选项 `-riscv-use-rematerializable-movimm`（默认 `false`，`RISCVOptions.td:14`）才留下 `PseudoMovImm`。夹具 `c.ll` 见 8.13；下面只截两个函数。

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -stop-after=prolog-epilog c.ll -o - | grep -E '^name: |^    [A-Za-z$]'
```

```mir
name:            c_0x12345678
    renamable $x10 = LUI 74565
    renamable $x10 = ADDI killed renamable $x10, 1656
    PseudoRET implicit $x10
name:            addr
    renamable $x10 = PseudoMovAddr target-flags(riscv-hi) @g, target-flags(riscv-lo) @g
    PseudoRET implicit $x10
```

加 `-riscv-use-rematerializable-movimm=true` 后，`c_0x12345678` 变成一行 `renamable $x10 = PseudoMovImm 305419896`。这个伪指令存在的理由是**重物化**：spill 后不必从栈上 reload，重新 `li` 可能更便宜（`RISCVInstrInfo.td:2006-2014` 标了 `isReMaterializable = 1`）。它由 post-RA pass 展开，而 `-stop-after=prolog-epilog` 在其之前，所以能看到。

**内联汇编里的 `li` 走 MC**：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -filetype=obj c.ll -o c.o
$T/bin/llvm-objdump -d --no-show-raw-insn --disassemble-symbols=ia c.o
```

```
000000000000004a <ia>:
      4a:      	lui	a0, 0x92
      4e:      	addi	a0, a0, -0x5d5
      52:      	slli	a0, a0, 0xd
      54:      	addi	a0, a0, 0x789
      58:      	ret
```

`0x92=146`、`-0x5d5=-1493`、`0xd=13`、`0x789=1929`，与 `llvm-mc` 对 `li a0, 0x123456789` 一致。**推论**：验证汇编器/内联汇编的影响用 `llvm-mc` 就够；只测 `llc` 会漏 MC，反过来会漏 `PseudoMovImm` 与 DAG combine。

---

## 8.10 地址物化：同一套「hi/lo」，不同的填法

常量物化产出**数值**，地址物化产出**符号地址**。形状相同，但低位来源不同：常量自己算 `Lo12`，地址把低位留给**重定位**。

非 PIC 的 small code model 走绝对寻址（`RISCVISelLowering.cpp:10215-10230`）：

```cpp
// This generates the pattern (addi (lui %hi(sym)) %lo(sym)).
SDValue AddrHi = getTargetNode(N, DL, Ty, DAG, RISCVII::MO_HI);
SDValue AddrLo = getTargetNode(N, DL, Ty, DAG, RISCVII::MO_LO);
return DAG.getNode(RISCVISD::ADD_LO, DL, Ty,
                   DAG.getNode(RISCVISD::HI, DL, Ty, AddrHi), AddrLo);
```

这对 `HI`/`ADD_LO` 被 TableGen 折成 **`PseudoMovAddr`**（`RISCVInstrInfo.td:1662-1676`，`Size = 8, isReMaterializable = 1`），post-RA 展开成 `LUI`+`ADDI`（`RISCVExpandPseudoPostRA.cpp:102-120`）。8.9 节 MIR 里的 `PseudoMovAddr target-flags(riscv-hi) @g, ...` 就是它；到 `-stop-after=riscv-expand-pseudo-post-ra` 停点变成 `LUI target-flags(riscv-hi) @g` + `ADDI ..., target-flags(riscv-lo) @g`（`llc -mtriple=riscv64 -mattr=+m,+a,+c -stop-after=riscv-expand-pseudo-post-ra c.ll -o -` 可复现）。

PIC 与 medium code model 走 PC 相对（`10189-10195`、`10231-10253`）：`RISCVISD::LLA` → `PseudoLLA` → `AUIPC %pcrel_hi(sym)` + `ADDI/LD %pcrel_lo(label)`。展开器 `RISCVAsmParser::emitAuipcInstPair`（3753-3777）**先发一个临时 label，再让 `%pcrel_lo` 指向它**：

```cpp
MCSymbol *TmpLabel = Ctx.createNamedTempSymbol("pcrel_hi");
Out.emitLabel(TmpLabel);                            // %pcrel_lo 绑到这条 label
emitToStreamer(Out, MCInstBuilder(RISCV::AUIPC).addReg(TmpReg).addExpr(SymbolHi));
const MCExpr *Lo = MCSpecifierExpr::create(MCSymbolRefExpr::create(TmpLabel, Ctx), RISCV::S_PCREL_LO, Ctx);
```

这不是风格问题：`R_RISCV_PCREL_LO12_I` 必须绑定到**具体那条 `AUIPC`**，linker 才能算出 `hi20<<12 + lo12` 并做 relaxation（Ch 12）。同理，地址的低 12 位**不能**像常量那样自由 `+0x800` 取整——取整会让 `lo12` 脱离重定位对的语义。

四种模式实测（同一个 `ret ptr @g`）：

| 模式 | 输出 |
|---|---|
| 非 PIC small | `lui a0, %hi(g)` / `addi a0, a0, %lo(g)` |
| 非 PIC medium | `auipc a0, %pcrel_hi(g)` / `addi a0, a0, %pcrel_lo(.Lpcrel_hi2)` |
| 非 PIC large | `auipc a0, %pcrel_hi(.LCPI6_0)` / `ld a0, %pcrel_lo(...)(a0)` |
| PIC | `auipc a0, %got_pcrel_hi(g)` / `ld a0, %pcrel_lo(...)(a0)` |

汇编器层面同样分叉：`la` 在非 PIC 下就是 `lla`（`auipc %pcrel_hi` + `addi %pcrel_lo`），PIC 下是 `lga`（`auipc %got_pcrel_hi` + `ld`）。分派在 `RISCVAsmParser.cpp:3799-3827`；`la.tls.ie`/`la.tls.gd` 走同一个 `emitAuipcInstPair`，只换 specifier（3829-3856），细节留给 Ch 11。

一句话记住：**地址物化 = hi/lo 切分 + 重定位；常量物化 = hi/lo 切分 + 立即数算术**。`PseudoMovAddr` 是交界：长得像常量物化，两个操作数却是 `MO_HI`/`MO_LO` 修饰的符号。

---

## 8.11 成本模型：`getIntMatCost` 与「宁可用常量池」

`getIntMatCost`（`.cpp:577-594`）不是「数条数」，而是**按 XLen 切块累加**：

```cpp
for (unsigned S = 0; S < Size; S += PlatRegSize) {
  APInt Chunk = Val.ashr(S).sextOrTrunc(PlatRegSize);
  if (FreeZeroes && Chunk.getSExtValue() == 0) continue;   // 零寄存器的块免费
  Cost += getInstSeqCost(generateInstSeq(Chunk.getSExtValue(), STI), HasRVC);
}
return std::max(FreeZeroes ? 0 : 1, Cost);
```

`getInstSeqCost`（16-50）在 `CompressionCost=true` 且 `+zca` 时改用权重：RVI 记 100、可压缩的记 70、`QC_E_LI` 记 150（48 位 ≈ 1.5 条）。头文件注释（`.h:64-66`）提醒它**会高估**：它不判断「这个值本来能不能当立即数操作数」，拿它做 `ADDI` 可行性判断是错的。

调用者（`grep -rn "getIntMatCost" llvm/lib/Target/RISCV/` 的完整结果）：

| 位置 | 用途 |
|---|---|
| `RISCVTargetTransformInfo.cpp:123` | IR 成本模型（`getIntImmCost`/`getIntImmCostInst`）：ConstantHoisting、CGP 的 GEP 索引、LSR、SCEVExpander |
| `RISCVISelDAGToDAG.cpp:183/186、4492/4494` | `(add X,C)`→`(sub X,-C)` 的成本比较；`selectImm64IfCheaper` |
| `RISCVISelLowering.cpp:2848` | FP 立即数：`fmv` + 整数物化 ≤ 阈值才不用常量池 |
| `RISCVISelLowering.cpp:10817/10822、26520` | `select`→`czero` 的 delta 成本；向量 splat 拆标量 store 是否划算（≤2） |
| `RISCVISelLowering.cpp:27127, 27129` | `c1` 与 `c1 << c2` 谁更便宜，决定要不要 combine |

**常见误解**：machine combiner 并不调用它（`grep` 结果里没有 `RISCVInstrInfo.cpp` 的机器组合器段落），内联汇编也不走 TTI。真正关心它的是 **ISel/DAG combine**（选哪条序列）和 **IR 成本模型**（常量提升、GEP/归纳变量是否值得把常量提到循环外）。

决定「要不要干脆用常量池」的是另一对函数（`RISCVSubtarget.cpp:154-187`）：

```cpp
unsigned RISCVSubtarget::getMaxBuildIntsCost() const {
  // 从常量池取整数需要两条指令，所以最低成本是 2
  return CLOpts.max_build_ints_cost == 0 ? getLoadLatency() + 1
                                         : std::max<unsigned>(2, CLOpts.max_build_ints_cost);
}
```

规则在 `lowerConstant`（`RISCVISelLowering.cpp:7749-7770`）：`Seq.size() <= getMaxBuildIntsCost()` 才展开，否则落常量池；门是 `CLOpts.constant_pool_for_large_ints`（默认 `true`，`RISCVOptions.td:73`）。

实测这条阈值链（夹具 `c.ll` 见 8.13）：8 条常数在 `-riscv-max-build-ints-cost=7` 下走常量池（`lui %hi(.LCPI…)` + `ld`）、`=8` 才全展开；5 条常数 `c_five`（`647251618792307`）在默认下走常量池、`-riscv-sched-load-latency=4` 下展开：

复现：`llc -mtriple=riscv64 -mattr=+m,+a,+c -riscv-max-build-ints-cost=7 c.ll -o - | sed -n '/^c_worst:/,/Lfunc_end/p'` 得到常量池版，把 `7` 换成 `8` 得到 8 条；对 `c_five` 加 `-riscv-sched-load-latency=4` 则从池变成 5 条展开。

默认阈值是 4（`generic-rv64` 的 `LoadLatency = 3`），所以 5 条就输给常量池；把 `LoadLatency` 抬到 7（`-riscv-sched-load-latency=7`），阈值变 8，`0xdeadbeefcafebabe` 才会在普通 codegen 里真的展开；反过来 `-mcpu=sifive-p670`（`LoadLatency = 4`）下同一个 `c_five` 直接展开——阈值公式可以这样自证。**这解释了为什么 8 条序列在 C 代码里几乎看不到、却在内联汇编里天天见**——内联汇编没有「要不要用常量池」这个选项。

---

## 8.12 源码落点

- **`MCTargetDesc/RISCVMatInt.h`**（83 行）：22-27 `OpndKind`；29-42 `Inst` 与截断断言；43 `InstSeq`；50-80 入口与成本契约。
- **`MCTargetDesc/RISCVMatInt.cpp`**（630 行）：16-50 RVC 权重；53-118 快路（Zbs/Xqci/P）；120-147 `≤32` 位分解；151-223 `>32` 位递归；294-503 优化链；507-542 `generateMCInstSeq`；544-575 `generateTwoRegInstSeq`；577-594 `getIntMatCost`。
- **`RISCVInstrInfo.cpp`**：1000-1063 `movImm`；5082-5184 `mulImm`（5146 调 `movImm`）。**`RISCVSubtarget.cpp`**：154-156 常量池开关、178-187 `getMaxBuildIntsCost`。
- **`RISCVISelDAGToDAG.cpp`**：278-311 `selectImm`；3490-3535 `selectConstantAddr`（注释点明「不能用 `generateInstSeq`，它偏好 `LUI+ADDIW`」）；4490-4595 相邻值四件套。
- **`RISCVExpandPseudoPostRA.cpp`**：68-83 分派；85-100 `expandMovImm`；102-120 `expandMovAddr`。
- **`AsmParser/RISCVAsmParser.cpp`**：3743-3751 `emitLoadImm`；3753-3777 `emitAuipcInstPair`；3779-3827 `lla`/`lga`/`la`；4497-4519 `PseudoLI`。
- **`RISCVInstrInfo.td`**：1658-1676 `PseudoMovAddr`；2006-2014 `PseudoMovImm`。
- **测试**：`llvm/test/CodeGen/RISCV/imm.ll`（RUN 行同时覆盖 `+zba`/`+zbb`/`+zbs`/`+xtheadbb`/`-riscv-constant-pool-for-large-ints=0`/`-riscv-use-rematerializable-movimm`）、`llvm/test/MC/RISCV/rv64i-aliases-valid.s`、`rv32zbs-aliases-valid.s`。

---

## 8.13 可跑实验

先写出夹具（后面所有命令都基于它；`c_five` 是 5 条常数，`mulc` 是乘常量，`ia` 是内联汇编）：

```bash
cat > c.ll <<'EOF'
target triple = "riscv64-unknown-linux-gnu"
@g = global i64 0
define i64 @c_0x7ff() { ret i64 2047 }
define i64 @c_0x800() { ret i64 2048 }
define i64 @c_0x12345678() { ret i64 305419896 }
define i64 @c_0x12345fff() { ret i64 305422335 }
define i64 @c_worst() { ret i64 -2401053089206453570 }
define i64 @c_five() { ret i64 647251618792307 }
define ptr @addr() { ret ptr @g }
define i64 @mulc(i64 %x) {
  %r = mul i64 %x, 123456789
  ret i64 %r
}
define i64 @ia() {
  %r = call i64 asm sideeffect "li $0, 0x123456789", "=r"()
  ret i64 %r
}
EOF
```

**实验 1：五个值一次看完**

```bash
printf 'li a0, 0x7ff\nli a0, 0x800\nli a0, 0x12345678\nli a0, 0x12345fff\nli a0, 0xdeadbeefcafebabe\n' > li.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+c -show-encoding li.s
```

数条数：1 / 2 / 2 / 2 / 8。第 2、第 4 个最容易想错。

**实验 2：只改 `-mattr`**

```bash
for A in "+m,+c" "+m,+c,+zba" "+m,+c,+zbb" "+m,+c,+zbs"; do
  echo "=== $A"; $T/bin/llvm-mc -triple=riscv64 -mattr="$A" li.s; done
```

预期：只有 `+zbs` 改变输出；`+zba`/`+zbb` 对这五个形状无效——「无变化」本身就是证据。

**实验 3：code model 三连**

```bash
for M in small medium large; do echo "=== $M";
  $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -code-model=$M c.ll -o - \
  | sed -n '/^addr:/,/^\.Lfunc_end/p'; done
```

三种输出见 8.10 的表。注意 `llc` 用 `-code-model=`；`-mcmodel=` 只有 clang driver 认识，写错会得到 `Unknown command line argument`。

---

## 8.14 踩雷集锦

1. **以为 `0x800` 是一条 `lui`。** `LUI` 装的是 `[31:12]`，`0x800` 全在低 12 位。实测是 `li 1`+`slli 11`（或 `lui 1`+`addi -2048`，或 `+zbs` 的 `bseti`）。
2. **以为 `0x12345fff` 要 4 条**（`sext12(0xfff) = -1`，2 条就够）；**或在 RV64 上手写 `lui` 装 `0x80000000`**——正确物化是 `li 1`+`slli 31`，`0x7fffffff` 才需要 `lui 524288`+`addiw -1`。
3. **用 `getIntMatCost` 判断立即数能否直接进 `ADDI`。** 头文件明确说它会高估且不检查立即数可容纳性。
4. **改完只跑 `llc` 测试。** `llvm-mc` 走 `generateMCInstSeq`、内联汇编走 MC，只测一边必漏一半；反过来，**在 `-stop-after=prolog-epilog` 找 `li`** 也找不到——常量在 ISel 就展开了。
5. **以为 `-mattr=+m` 会改变常量物化。** 实测两次都是 `lui 30141`+`addi -747`。
6. **忘记 `TuneLUIADDIFusion`。** 同一条 `0x1800`，`generic` 给 `li 3`+`slli 11`，`sifive-p670` 给 `lui 2`+`addi -2048`。别拿单一 `-mcpu` 的期望值当通用行为。
7. **在地址物化里照搬常量的 `+0x800` 取整。** PC 相对的 `lo12` 由重定位对决定，取整会破坏 `%pcrel_lo` 与 `AUIPC` 的绑定（Ch 12 的 relaxation 直接依赖它）。

---

## 8.15 本章重点整理

- `lui` 20 位（`[31:12]`）+ `addi` 12 位有符号 = 全部复杂度；`Hi20 = ((Val+0x800)>>12) & 0xFFFFF` 与 `Lo12 = sext12(Val)` 是唯一分解公式。
- `≤32` 位最多 2 条；`>32` 位从 LSB 递归剥离，每层一条 `addi`+一条 `slli`，上界 8 条。
- `Inst` 只描述「opcode + 立即数 + 操作数形状」，因此同一序列可喂给 ISel 与 MC；`generateInstSeq` 的上下文只有 `MCSubtargetInfo`。
- 「相邻值优化」有三处落点：两种 `(hi,lo)` 切法、`0x17ff` 的 ±δ 修正、DAG 层的 `-V`/`~V`/`zext` 替换；`generateInstSeqAdjacent` 这个名字在本树不存在。
- 扩展分工：`+zbs` 管单比特与逐位、`+zba` 管 `sh*add`/`slli.uw`/`add.uw`、`+zbb` 只做 `rori`；`+m` 与常量物化无关。
- 地址物化 = hi/lo 切分 + 重定位：small 用 `PseudoMovAddr`，medium/PIC 用 `AUIPC %pcrel_hi` + `%pcrel_lo(label)`，large 多一次常量池取址。
- `getIntMatCost` 服务 ISel/DAG combine 与 IR 成本模型，不服务 machine combiner；是否落常量池由 `getMaxBuildIntsCost() = LoadLatency + 1` 决定。

---

## 8.16 自我检核

- [ ] 能默写 `Hi20`/`Lo12` 两行，并解释 `+0x800` 与 `& 0xFFFFF` 各防什么错
- [ ] 能解释为什么 `0x12345fff` 只要 2 条、`0x1000017ff` 要 4 条
- [ ] 能闭卷推出 `0xdeadbeefcafebabe` 的 8 条，并说明最后一层为何是 `ADDI`
- [ ] 能解释 `%pcrel_lo` 为什么要指向临时 label，而不是直接写符号
- [ ] 能在 MIR 里区分 `PseudoMovAddr`、`PseudoMovImm` 与已展开的 `LUI`/`ADDI`

---

## 8.17 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.cpp`**（630 行）：读 151-223 的递归与 294-503 的优化链——本章所有「少一条/多一条」的判决都在这里，优化链的顺序就是优先级。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.h`**（83 行）：读 45-80 的契约注释——`getIntMatCost` 的「会高估」与 `CompressionCost` 语义只在这里写清楚。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`**：读 278-311、3490-3535、4490-4595——成本模型在 ISel 侧的全部使用，也是「改序列会不会影响访存折叠」的入口。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoPostRA.cpp`**：读 68-120——两种伪指令的最终展开，以及 `Kill`/`Dead`/`Renamable` 的传播细节。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`**：读 3743-3827、4497-4519——内联汇编与 `.s` 的行为由这段决定，且它与 CodeGen 不共享代码路径。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/imm.ll`**：读 1-25 行的 RUN 矩阵，再按 `RV64IZBA`/`RV64IZBS`/`RV64IZBB` 前缀抽查——它是这个文件的行为契约。

→ 下一章：[Ch 09 — 调用约定 lowering](./09-calling-convention-lowering.md)
