# Ch 02 — ISA、扩展与 Subtarget：从 `+zba` 到 `hasStdExtZba()`

> **目标**：追清一条 feature 字符串的完整生命：`-mattr=+zba` / `-march=rv64gc_zba` 如何变成 TableGen 的 `Feature` 记录、`HasStdExtZba` predicate、C++ 的 `hasStdExtZba()`、指令 pattern 的可用性，以及 ELF 里的 `.attribute arch`。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM `24.0.0git`，HEAD `0fc5afa04c23`。

---

## 2.1 为什么扩展模型值得单独一章

RISC-V 的扩展不是散落在 C++ 里的 `if`，而是一张**数据表**：

```text
RISCVFeatures.td 里的 def
        │
        ├──→ -mattr / -march 的字符串
        ├──→ TableGen 生成 SubtargetFeature 解析
        ├──→ HasStdExtZba predicate（给汇编器和 pattern 用）
        ├──→ RISCVSubtarget::hasStdExtZba()（给 C++ 用）
        └──→ ELF .attribute arch（给链接器和运行时用）
```

这条链的任何一环漏掉，都会产生一种“看起来能跑”的 bug：

- 只加了指令 def，没加 predicate：`-mattr=-zba` 时也能汇编出 `sh3add`；
- 只加了 C++ 检查，没加 pattern：C++ 认为该用，选指却选不出来；
- 只改了 `-march` 解析，没改 ELF 属性：链接器看到的 ABI/扩展和编译器不一致；
- 只加了 feature，没加 implication：`+v` 不自动带上 `zvl128b`，向量代码生成会缺前提。

上游最近 12 个月有 **80 条提交**改到 `RISCVFeatures.td`，占后端热点第 5 位。这不是“配置文件的杂活”，而是 RISC-V 维护的主战场之一。

---

## 2.2 先建立直觉：feature 是一个配电盘

把 `RISCVSubtarget` 想成一个配电盘：

```text
        -mattr=+zba,+m
              │
              ▼
   ┌──────────────────────┐
   │  ParseSubtargetFeatures │  ← TableGen 生成
   └──────────────────────┘
              │
      ┌───────┼────────┬─────────┐
      ▼       ▼        ▼         ▼
 HasStdExtZba HasStdExtM ...  HasStdExtV
      │       │                  │
      ▼       ▼                  ▼
   sh3add   mul/div          vadd.vv
   pattern  pattern          pattern
```

每个 `Feature` 记录有两个关键属性：

- **Name**：`-mattr` 认识的名字，比如 `zba`；
- **fieldname**：生成到 `RISCVSubtarget` 里的布尔字段，默认是 `Feature` 换成 `Has`，也就是 `HasStdExtZba`。

`RISCVFeatures.td:29-52` 的 `RISCVExtension` 类就是干这个的：

```tablegen
class RISCVExtension<int major, int minor, string desc,
                     list<SubtargetFeature> implies = [],
                     string fieldname = !subst("Feature", "Has", NAME),
                     string value = "true", bit IsExperimental = false>
    : SubtargetFeature<"", fieldname, value, "", implies> {
  defvar extName = !subst("FeatureVendor", "", !subst("FeatureStdExt", "", NAME));

  let Name = !if(IsExperimental, "experimental-", "") # !tolower(extName);
  ...
}
```

所以 `FeatureStdExtZba` 自动得到：

- `Name = "zba"`（`FeatureStdExt` 被剥掉，转小写）；
- `fieldname = "HasStdExtZba"`；
- `implies = []`。

---

## 2.3 一个真实例子：`+zba` 打开 `sh3add`

### 2.3.1 源码

```llvm
define i64 @sh3add(i64 %x, i64 %y) {
  %s = shl i64 %x, 3
  %r = add i64 %s, %y
  ret i64 %r
}
```

### 2.3.2 没有 Zba

```bash
$T/bin/llc -mtriple=riscv64 -O2 /tmp/zba.ll -o -
```

实测：

```asm
sh3add:
	slli	a0, a0, 3
	add	a0, a0, a1
	ret
```

### 2.3.3 有 Zba

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+zba -O2 /tmp/zba.ll -o -
```

实测：

```asm
sh3add:
	sh3add	a0, a0, a1
	ret
```

`.attribute` 也变了：

```asm
	.attribute	5, "rv64i2p1_zba1p0"
```

### 2.3.4 这条链在源码里长什么样

**第 1 环：feature 定义**（`RISCVFeatures.td:495-501`）

```tablegen
def FeatureStdExtZba
    : RISCVExtension<1, 0, "Address Generation Instructions">,
      RISCVExtensionBitmask<0, 27>;
def HasStdExtZba : Predicate<"Subtarget->hasStdExtZba()">,
                   AssemblerPredicate<(all_of FeatureStdExtZba),
                                      "'Zba' (Address Generation Instructions)">;
def NoStdExtZba : Predicate<"!Subtarget->hasStdExtZba()">;
```

注意 `HasStdExtZba` 同时服务两个消费者：

- `Predicate<"Subtarget->hasStdExtZba()">`：给 SelectionDAG pattern 做 C++ 条件；
- `AssemblerPredicate<...>`：给汇编器做“这条指令在当前 `-mattr` 下是否合法”。

**第 2 环：指令与 pattern**（`RISCVInstrInfoZb.td:274-294`）

```tablegen
let Predicates = [HasStdExtZba] in {
def SH1ADD : ALU_rr<0b0010000, 0b010, "sh1add">,
             Sched<[WriteSHXADD,...]>;
def SH2ADD : ALU_rr<0b0010000, 0b100, "sh2add">,
             Sched<[WriteSHXADD,...]>;
def SH3ADD : ALU_rr<0b0010000, 0b110, "sh3add">,
             Sched<[WriteSHXADD,...]>;

let append Predicates = [IsRV64] in {
def SLLI_UW : RVBShift_ri<...>;
def ADD_UW  : ALUW_rr<...>;
...
} // append Predicates = [IsRV64]
} // Predicates = [HasStdExtZba]
```

**第 3 环：C++ 检查**（例如 `RISCVISelLowering.cpp` 里的 `hasStdExtZba()`）。这层通常用于“要不要做某个 lowering 决策”，而不是直接选指令。

**第 4 环：ELF 属性**（`RISCVELFStreamer.cpp` / `RISCVAsmBackend.cpp`）。`-mattr=+zba` 会让 `e_flags` 之外的 `.attribute 5` 字符串多出 `_zba1p0`。

> 维护提示：如果你新增一个扩展，最小闭环是“feature + predicate + 指令/pattern + 测试（汇编器、CodeGen、属性）”。只做前两项，`llc` 能过，但 `llvm-mc` 的 negative test 会挂。

---

## 2.4 没有扩展时，编译器退化成什么

这是判断“这个扩展到底省了什么”的最快方法。

### 2.4.1 Zbb 的 `andn`

```llvm
define i64 @andn(i64 %a, i64 %b) {
  %n = xor i64 %b, -1
  %r = and i64 %a, %n
  ret i64 %r
}
```

无 `+zbb`：

```asm
andn:
	not	a1, a1
	and	a0, a0, a1
	ret
```

有 `+zbb`：

```asm
andn:
	andn	a0, a0, a1
	ret
```

### 2.4.2 Zbb 的 `cpop`

```llvm
define i32 @cpop(i32 %a) {
  %c = call i32 @llvm.ctpop.i32(i32 %a)
  ret i32 %c
}
```

无 `+zbb`（实测，节选）：

```asm
cpop:
	lui	a1, 349525
	srli	a2, a0, 1
	addi	a1, a1, 1365
	and	a1, a2, a1
	sub	a0, a0, a1
	...
	srliw	a0, a0, 24
	ret
```

有 `+zbb`：

```asm
cpop:
	cpopw	a0, a0
	ret
```

一条变十七条。这就是为什么“扩展没打开”的报错往往表现为“性能问题”，而不是“编译错误”。

> 实验建议：拿任何一条扩展指令，在 `-mattr` 关掉它的情况下编译等价 IR，观察退化序列。这比读 spec 更能建立“为什么需要这个扩展”的直觉。

---

## 2.5 feature 的隐含关系：`+v` 不只是 `+v`

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 /tmp/zba.ll -o - | grep attribute
```

实测：

```asm
	.attribute	5, "rv64i2p1_f2p2_d2p2_v1p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0_zve32f1p0_zve32x1p0_zve64d1p0_zve64f1p0_zve64x1p0_zvl32b1p0_zvl64b1p0_zvl128b1p0"
```

一个 `+v` 展开了：

- `zve32x`、`zve32f`、`zve64x`、`zve64f`、`zve64d`；
- `zvl32b`、`zvl64b`、`zvl128b`；
- 以及 `f`/`d`（因为 `zve64d` 依赖 `d`）。

源头在 `RISCVFeatures.td:698-701`：

```tablegen
def FeatureStdExtV
    : RISCVExtension<1, 0,
                     "Vector Extension for Application Processors",
                     [FeatureStdExtZvl128b, FeatureStdExtZve64d]>,
      RISCVExtensionBitmask<0, 21>;
```

`implies` 是递归的：`Zve64d` 又 imply `Zve64f` 和 `D`，一路展开。

### 2.5.1 负面 feature 不会撤销隐含扩展

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m,-m -O2 /tmp/riscv-smoke.ll -o - | grep attribute
```

实测：

```asm
	.attribute	5, "rv64i2p1_zmmul1p0"
```

`-m` 关掉了 `FeatureStdExtM`，但 `FeatureStdExtM` 隐含的 `FeatureStdExtZmmul` 还在。这是一个非常常见的误解：**`+m,-m` 不等于“没有 M 相关的一切”**。

如果你真的想确认某个 feature 是否关闭，用 `llvm-mc` 汇编一条该扩展的指令，看汇编器是否报错；不要只看 `-mattr` 字符串。

### 2.5.2 实验

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 /tmp/zba.ll -o - | grep '\.attribute'
$T/bin/llc -mtriple=riscv64 -mattr=+v,-d -O2 /tmp/zba.ll -o - | grep '\.attribute'
```

实测 `-mattr=+v,-d` 的属性串是：

```asm
	.attribute	5, "rv64i2p1_f2p2_zicsr2p0_zve32f1p0_zve32x1p0_zve64f1p0_zve64x1p0_zvl128b1p0_zvl32b1p0_zvl64b1p0"
```

注意 `v1p0` 和 `zve64d` 都不见了。负面 feature 与 `implies` 的交互不是简单的集合减法：`v` 依赖 `zve64d`，`zve64d` 又依赖 `d`；`-d` 会一路把这条链裁掉。所以**不要凭直觉组合 `+v,-d`**，永远用 `.attribute` 验证最终 feature 集。

---

## 2.6 Subtarget 的解析链

### 2.6.1 从函数属性到 Subtarget

```text
TargetMachine::getSubtargetImpl
  └─ 读函数属性 "target-features" / "target-cpu"
       └─ new RISCVSubtarget(TT, CPU, TuneCPU, FS, ABIName, ...)
            └─ initializeSubtargetDependencies(TT, CPU, TuneCPU, FS, ABIName)
                 └─ ParseSubtargetFeatures(CPU, TuneCPU, FS)   ← TableGen 生成
                      └─ 设置 HasStdExtZba 等布尔字段
```

源码锚点：

| 步骤 | 位置 |
|---|---|
| 读函数属性、构造 Subtarget | `RISCVTargetMachine.cpp:129`（`getSubtargetImpl`） |
| 构造函数 | `RISCVSubtarget.cpp:97-116` |
| 依赖初始化 | `RISCVSubtarget.cpp:56`（`initializeSubtargetDependencies`） |
| feature 解析 | `RISCVSubtarget.cpp:75`（`ParseSubtargetFeatures`） |
| 声明 | `RISCVSubtarget.h:138`、`:154` |

### 2.6.2 生成的 getter

`RISCVSubtarget.h:117-121` 用宏展开布尔字段，`:189-191` 展开 getter。所以你可以在 C++ 里直接写：

```cpp
if (Subtarget.hasStdExtZba())
  ...
```

这些 getter 是 `inline` 的，编译器能折叠；不要担心“每个 pattern 一次函数调用”的开销。

### 2.6.3 复合 helper

`RISCVSubtarget.h:193-345` 有一批手写的复合判断：

- `hasStdExtFOrZfinx()`（197）
- `hasVInstructions()`（327）
- `hasVInstructionsI64()`（328）
- `hasVInstructionsF64()`（335）
- `hasShlAdd()`（229）

维护建议：**新增复合条件时，优先加到这里，而不是在 31k 行的 `RISCVISelLowering.cpp` 里重复写 `hasStdExtA() && hasStdExtB()`**。否则两个地方的一致性迟早会漂移。

---

## 2.7 CPU、tune 与 profile

### 2.7.1 `-mcpu` 不是“只影响调度”

```bash
$T/bin/llc -mtriple=riscv64 -mcpu=help | head -18
```

实测（节选）：

```text
  andes-45-series     - Select the andes-45-series processor.
  et-soc1             - Select the et-soc1 processor.
  generic             - Select the generic processor.
  generic-ooo         - Select the generic-ooo processor.
  generic-rv32        - Select the generic-rv32 processor.
  generic-rv64        - Select the generic-rv64 processor.
  mips-p8700          - Select the mips-p8700 processor.
```

`-mcpu` 会：

1. 打开该 CPU 的默认扩展集合；
2. 选择调度模型（`RISCVSched*.td`）；
3. 影响 `RISCVProcessors.td` 里的 `TuneFeatures`。

`-mtune` 只影响第 2、3 项，不改变可用指令集。维护时区分这两者很重要：一个“性能回归”可能只是 `-mtune` 选错了模型，而不是代码生成错了。

### 2.7.2 profile

`RISCVProfiles.td`（165 行）定义了 `rva23u64` / `rvb23u64` 这类 profile。profile 是“一组扩展的命名集合”，CLI 里可以写：

```bash
clang --target=riscv64 -march=rva23u64 ...
```

它会在 `RISCVISAInfo::parseArchString` 里展开成具体扩展。Ch 24 会讲 driver 侧的优先级。

---

## 2.8 架构字符串的校验与规范化

`llvm/lib/TargetParser/RISCVISAInfo.cpp` 是唯一负责“字符串语法”的地方：

| 函数 | 行 | 职责 |
|---|---:|---|
| `parseNormalizedArchString` | 474 | 解析已经规范化的字符串 |
| `parseArchString` | 569 | 解析用户输入的 `-march` |
| `toFeatures` | 286 | 转成 `+`/`-` feature 列表 |

`parseArchString` 会检查：

- 字符集必须是 `[a-z0-9_]`（572-574）；
- 必须以 `rv32` / `rv64` 或受支持 profile 开头（577-616）；
- 基础 ISA 必须是 `e`/`i`/`g`/`y`（619-630）；
- 扩展的版本、顺序、依赖关系。

规范化后的字符串会写进 `.attribute 5`。例如：

```bash
$T/bin/clang -target riscv64-unknown-linux-gnu -march=rv64gc_zba -mabi=lp64d -O2 -S /tmp/zba.ll -o - | grep attribute
```

实测：

```asm
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0_zba1p0"
```

注意 `g` 被展开成 `imafd_zicsr_zifencei`，`c` 又派生出 `zca/zcd`，`m` 派生出 `zmmul`，`a` 派生出 `zaamo/zalrsc`。**你写的 `-march` 和 ELF 里的字符串不是一回事**；调试 ABI/链接问题时，永远看 `.attribute`，不要看命令行。

---

## 2.9 ABI 与扩展的交叉校验

`-mabi=lp64d` 需要 D 扩展。如果你强行组合：

```bash
$T/bin/clang -target riscv64-unknown-linux-gnu -march=rv64i -mabi=lp64d -O2 -S /tmp/args.c -o /dev/null
```

实测：

```text
note: hard-float 'd' ABI can't be used for a target that doesn't support the D instruction set extension (ignoring target-abi)
```

注意它是 **note + 忽略**，不是错误。这意味着“命令行看起来是 lp64d，实际生成的是软浮点 ABI”。这类“静默降级”是 ABI bug 的高发区。

校验代码在：

- `clang/lib/Basic/Targets/RISCV.cpp:414-448`（`handleTargetFeatures`）；
- `RISCV32TargetInfo::setABI`（`RISCV.h:174-190`）；
- `RISCV64TargetInfo::setABI`（`RISCV.h:205-221`）。

---

## 2.10 踩雷集锦

1. **`+m,-m` 不会清掉 `zmmul`。** implication 是单向的；负面 feature 只关掉自己。
2. **`-mattr` 和 `-march` 不是同一层。** `-mattr` 直接给 backend；`-march` 先经过 `RISCVISAInfo::parseArchString` 规范化，再变成 feature。调试时优先用 `-mattr` 隔离变量。
3. **`-mcpu` 会改扩展集合。** “我只想换调度模型”应该用 `-mtune`。
4. **`.attribute` 才是链接器看到的真相。** 命令行字符串会被展开、排序、加版本号。
5. **experimental 扩展需要 `-menable-experimental-extensions`。** 名字带 `experimental-` 前缀；没有这个 flag，`-march` 会报错。
6. **新增扩展忘加 `AssemblerPredicate`。** 结果是 CodeGen 能生成，`llvm-mc` 却拒绝汇编；或者反过来。
7. **新增扩展忘加测试。** 上游至少期望：`llvm/test/MC/RISCV/<ext>-valid.s`、`<ext>-invalid.s`、`llvm/test/CodeGen/RISCV/<ext>.ll`、`attributes.ll` 更新。
8. **`hasStdExt*()` 不是线程安全的懒初始化。** 它是普通布尔字段，Subtarget 构造后只读；不要在 pass 里缓存跨 Subtarget 的指针。

---

## 2.11 可跑实验

### 实验 1：Zba 的三层证据（约 10 秒）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /tmp

# 1) CodeGen
$T/bin/llc -mtriple=riscv64 -mattr=+zba -O2 zba.ll -o - | sed -n '/sh3add:/,/ret/p'

# 2) Assembler：没有 zba 时应该报错
printf 'sh3add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
printf 'sh3add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zba -show-encoding

# 3) ELF attribute
$T/bin/llc -mtriple=riscv64 -mattr=+zba -O2 zba.ll -o - | grep '\.attribute 5'
```

预期：第一条 `llvm-mc` 报 `invalid instruction`；第二条输出 `encoding: [0x33,0x65,0xb5,0x20]`；`llc` 的属性串含 `zba1p0`。

### 实验 2：`+v` 的展开

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 zba.ll -o - | grep '\.attribute 5'
```

数一数 `zve*`、`zvl*` 各出现了几次。然后对照 `RISCVFeatures.td:698` 的 `implies` 列表，确认你能手算出来。

### 实验 3：CPU 的隐含扩展

```bash
for cpu in generic generic-ooo sifive-p670; do
  echo "== $cpu =="
  $T/bin/llc -mtriple=riscv64 -mcpu=$cpu -O2 zba.ll -o - 2>/dev/null | grep '\.attribute 5'
done
```

如果某个 CPU 不存在，`llc` 会报错；这本身也是一条证据：CPU 名称来自 `RISCVProcessors.td`，不是任意字符串。

---

## 2.12 源码落点

| 内容 | 路径 | 关键行/符号 |
|---|---|---|
| 扩展基类 | `llvm/lib/Target/RISCV/RISCVFeatures.td` | `RISCVExtension` 29；`Name` 44-46 |
| Zba/Zbb/Zbs | 同上 | `FeatureStdExtZba` 495；`HasStdExtZba` 498；`FeatureStdExtZbb` 503；`FeatureStdExtZbs` 512 |
| V 的 implies | 同上 | `FeatureStdExtV` 698 |
| Zba 指令/pattern | `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | `Predicates = [HasStdExtZba]` 274；`SH3ADD` 279 |
| Subtarget 解析 | `llvm/lib/Target/RISCV/RISCVSubtarget.cpp` | 56、75、97-116 |
| Subtarget getter | `llvm/lib/Target/RISCV/RISCVSubtarget.h` | 117-121、138、154、189-191、193-345 |
| 架构字符串 | `llvm/lib/TargetParser/RISCVISAInfo.cpp` | `parseArchString` 569；`parseNormalizedArchString` 474；`toFeatures` 286 |
| CPU/tune | `llvm/lib/Target/RISCV/RISCVProcessors.td` | 1,159 行 |
| profile | `llvm/lib/Target/RISCV/RISCVProfiles.td` | `FeatureStdExtV` 附近的 profile 定义 |
| Clang ABI 校验 | `clang/lib/Basic/Targets/RISCV.cpp` | `handleTargetFeatures` 414 |
| 使用文档 | `llvm/docs/RISCVUsage.md` | `## Ratified Extensions` 97；`## Experimental Extensions` 336 |

---

## 2.13 本章重点整理

- 扩展是数据：`RISCVExtension` 自动派生 `-mattr` 名与 `HasStdExt*` 字段。
- 一条扩展至少要在四个地方闭环：feature、predicate、指令/pattern、测试（含 ELF 属性）。
- `+v` 会展开成一长串 `zve*` / `zvl*`；`+m,-m` 不会撤销 `zmmul`。
- `-mattr`、`-march`、`-mcpu`、`-mtune` 是四层不同的东西；调试时一次只动一层。
- `.attribute 5` 是链接器看到的真相；命令行字符串会被规范化。

## 2.14 自我检核

- [ ] 能解释 `RISCVExtension` 如何从 `FeatureStdExtZba` 派生 `-mattr=+zba` 和 `hasStdExtZba()`
- [ ] 能说出 `HasStdExtZba` 同时服务哪两个消费者
- [ ] 会用“关掉扩展看退化序列”的方法判断一个扩展省了什么
- [ ] 知道 `+v` 隐含哪些扩展，并能从 `.attribute` 里指出来
- [ ] 能解释为什么 `-march=rv64gc_zba` 的属性串比命令行长得多
- [ ] 新增一个扩展时，能列出至少四类必须补的测试

---

## 2.15 延伸阅读

- **`llvm/docs/RISCVUsage.md`**（本树，808 行）
  - **读哪里**：`## Base ISAs`（43）、`## Profiles`（62）、`## Ratified Extensions`（97）、`## Experimental Extensions`（336）、`## Vendor Extensions`（416）。
  - **为什么读**：它给出“哪个扩展处于什么状态”的上游口径；判断“这是 bug 还是未支持”的第一站。
- **`llvm/lib/Target/RISCV/RISCVFeatures.td`**（本树，2,407 行）
  - **读哪里**：29-70 行的基类；然后按需跳到 `FeatureStdExtZba`（495）、`FeatureStdExtV`（698）。
  - **为什么读**：这是扩展模型的唯一源头；任何 `hasStdExt*` 的语义都在这里。
- **`llvm/lib/TargetParser/RISCVISAInfo.cpp`**（本树，1,181 行）
  - **读哪里**：`parseArchString`（569）与 `toFeatures`（286）。
  - **为什么读**：解释为什么命令行 `-march` 和 `.attribute` 不是同一字符串。
- **`llvm/docs/RISCV/RISCVVectorExtension.md`**（本树，320 行）
  - **读哪里**：`## Mapping to LLVM IR types`（6）、`## Representation in LLVM IR`（45）。
  - **为什么读**：`+v` 展开后的 `zve*`/`zvl*` 如何影响 IR 类型与 lowering，这份文档讲得最清楚。

---

→ 下一章：[Ch 03 — 流水线与 MIR：`RISCVPassConfig` 的真实 pass 名单](./03-pipeline-and-mir.md)
