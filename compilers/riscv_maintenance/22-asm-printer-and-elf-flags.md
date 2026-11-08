# Ch 22 — AsmPrinter 与 ELF flags：.option、.attribute 与 e_flags

> **目标**：把 `MachineFunction` 到 `.o` 的最后一段路走完；能说出 `RISCVAsmPrinter` 五个 hook 的职责；能读懂 `.option` 与 `.attribute` 在 `.s`/`.o` 里的两种形态；能手算 `e_flags` 的六位；能在 `e_flags` 与 `Tag_RISCV_arch` 不一致时判断是代码生成 bug 还是用户输入问题。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。「实测」均来自该构建；跑不了的标「未实测」。

> **前置**：Ch 07（`.s` 如何被重新解析、`MCInst` 与 `InstPrinter`）、Ch 12（`R_RISCV_RELAX`、`call→jal`、`e_flags` 与 lld）。

---

## 22.1 为什么维护者要看输出层

前面各章回答「生成哪条指令」，这一章回答「生成的东西别人能不能用」。

1. **`e_flags` 不匹配，链接直接失败。** 实测把 `-mabi=lp64d` 与 `-mabi=lp64` 的两个 `.o` 交给 `ld.lld`，得到 `error: f2_lp64.o: cannot link object files with different floating-point ABI from f1_lp64d.o`。这是硬错误，不是警告（`lld/ELF/Arch/RISCV.cpp:167-190`）。

2. **`.attribute arch` 是别人判断「这文件能用什么指令」的唯一依据。** binutils 链接时按 `Tag_RISCV_arch` 合并；QEMU 据此决定给 guest 开哪些扩展；内核 alternatives 也读它。少一个 `zca`，下游就可能拒绝或误判。

3. **`.option` 决定 `.s` 能否被重新汇编。** 后端给每个函数插 `push/pop`，本质是把「per-function target-features」翻译成汇编器能理解的状态栈。翻译漏了，`.s` 就只对当前输入有效。

**指令选错是性能问题，输出写错是兼容性问题。**

---

## 22.2 先建立直觉：从 MI 到 ELF 的最后一公里

```
  MachineFunction（已调度、伪指令已展开）
        │  RISCVAsmPrinter::runOnMachineFunction                     :584
        │    ├─ emitTargetFeaturePush()  → .option push / .option arch  :550
        │    ├─ SetupMachineFunction()
        │    ├─ emitFunctionBody()        ← AsmPrinter 基类
        │    └─ emitTargetFeaturePop()    → .option pop                 :578
        ▼
  RISCVAsmPrinter::emitInstruction                                    :390
        │  verifyInstructionPredicates / emitNTLHint
        │  lowerPseudoInstExpansion / lowerToMCInst
        ▼
     MCInst（opcode + MCOperand，仍未定长）
        │  EmitToStreamer()：RISCVRVC::compress() 尝试压成 C_*         :276
        ├─────────────────────────────┬──────────────────────────────
        ▼                             ▼
  MCAsmStreamer                  RISCVELFStreamer
  + RISCVTargetAsmStreamer       + RISCVTargetELFStreamer
        │                             │
  RISCVInstPrinter              RISCVMCCodeEmitter
  （MCInst → 文本 + 别名）       （MCInst → 字节 + MCFixup）
        │                             │
        ▼                             ▼
     .s 文本                      .o（.text / .rela / .symtab）
   .option push                       │  RISCVTargetELFStreamer::finish()  :110
   .option arch, +zba                 │    e_flags = RVC|TSO|RVE|FLOAT_ABI
   .attribute 5, "..."                │  finishAttributeSection()          :101
                                      ▼
                                  .riscv.attributes（Tag_RISCV_arch 等）
```

找错现场时看这张表：

| 层 | 输入 | 输出 | 典型症状 |
|---|---|---|---|
| `RISCVAsmPrinter` | `MachineInstr` | `MCInst` 流 | 少一条指令、伪指令未展开 |
| `RISCVInstPrinter` | `MCInst` | 文本 | 别名/寄存器名不对（Ch 07） |
| `RISCVMCCodeEmitter` | `MCInst` | 字节 + fixup | 编码错、重定位类型错（Ch 06） |
| `RISCVELFStreamer` / `RISCVTargetELFStreamer` | 字节 + 符号 + features | `.text`/`.rela`/mapping symbol、`e_flags`、attributes | 重定位多/少、`$x` 缺失、lld 拒链 |

---

## 22.3 AsmPrinter 的结构

`RISCVAsmPrinter.cpp` 只有 1,410 行，骨架是五个入口。

### 22.3.1 `runOnMachineFunction`：一个函数 = 一个 `.option` 作用域

```cpp
// RISCVAsmPrinter.cpp:584
bool RISCVAsmPrinter::runOnMachineFunction(MachineFunction &MF) {
  STI = &MF.getSubtarget<RISCVSubtarget>();
  bool EmittedOptionArch = emitTargetFeaturePush(*STI);
  SetupMachineFunction(MF);
  emitFunctionBody();
  emitXRayTable();
  emitTargetFeaturePop(*STI, EmittedOptionArch);
  return false;
}
```

**push 在 `SetupMachineFunction` 之前，pop 在 `emitXRayTable` 之后**。`.option` 作用域因此包住整个函数体（含 XRay sled），sled 里的 `jal` 也需要正确的压缩/relax 设置。`EmittedOptionArch` 是「有没有真的 push」：无差异时不发 pop，避免孤立的 `.option pop` 报错。

### 22.3.2 `emitTargetFeaturePush`：feature 差 → `.option arch`

```cpp
// RISCVAsmPrinter.cpp:550-577
const MCSubtargetInfo &MCSTI = TM.getMCSubtargetInfo();
for (const auto &Feature : MCSTI.getAllProcessorFeatures()) {
  if (STI.hasFeature(Feature.Value) == MCSTI.hasFeature(Feature.Value))
    continue;
  if (!RISCVISAInfo::isSupportedExtensionFeature(Feature.key()))
    continue;
  auto Delta = STI.hasFeature(Feature.Value) ? RISCVOptionArchArgType::Plus
                                             : RISCVOptionArchArgType::Minus;
  StringRef ExtName = Feature.key();
  ExtName.consume_front("experimental-");
  NeedEmitStdOptionArgs.emplace_back(Delta, ExtName.str());
}
if (!NeedEmitStdOptionArgs.empty()) {
  RTS.emitDirectiveOptionPush();                      // :568
  RTS.emitDirectiveOptionArch(NeedEmitStdOptionArgs); // :569
  RTS.setArchString(cantFail(RISCVFeatures::parseFeatureBits(STI))->toString());
  return true;
}
return false;
```

三个细节决定输出文本：基准是**模块级 `TM.getMCSubtargetInfo()`**，每个函数都相对模块基线做差、不叠加；只处理 `isSupportedExtensionFeature` 且剥掉 `experimental-` 前缀，所以 `+relax`/`+64bit` 不会出现；遍历的是已排序 feature 表，所以参数是**字母序**。

### 22.3.3 `emitInstruction` 与 `EmitToStreamer`

`emitInstruction`（`:390`）第一件事是 `verifyInstructionPredicates`——selected instruction 与 feature 不匹配的第一道拦网。之后：`emitNTLHint`（`nontemporal` 访存的 hint 编码）→ `lowerPseudoInstExpansion`（TableGen 生成的 `RISCVGenMCPseudoLowering.inc`）→ `switch` 处理 `PseudoTAILX7`/`KCFI_CHECK`/`STACKMAP`/`PATCHPOINT`/`STATEPOINT`/XRay/`PseudoCALL*LpadAlign` → 兜底 `lowerToMCInst` + `EmitToStreamer`。

```cpp
// RISCVAsmPrinter.cpp:276
MCInst CInst;
bool Res = RISCVRVC::compress(CInst, Inst, SubtargetInfo);
if (Res) ++RISCVNumInstrsCompressed;
S.emitInstruction(Res ? CInst : Inst, SubtargetInfo);
```

**压缩发生在 AsmPrinter 层，不在 CodeEmitter 层**（Ch 06 已述）。所以 `.s` 与 `.o` 看到同一批 `C_*`，一致性由这里保证。

### 22.3.4 `emitStartOfAsmFile`：ABI 与模块级 attribute

`:652` 起做三件事：① 从模块 metadata `getTargetABIFromMD()` 取 ABI，取不到就 `computeTargetABI(MCSubtargetInfo, "")`，取到非法值则 `emitError`；② 读模块 flag **`riscv-isa`**，对每个字符串 `parseArchString` 后 `ToggleFeature`（该 flag 由 clang 在 `CodeGenModule.cpp:1575-1592` 用 `AppendUnique` 写入，LTO 时合并多个 TU）；③ `RTS.emitTargetAttributes(SubtargetInfo, /*EmitStackAlign=*/true)`（`:694`）——传的是 `TM.getMCSubtargetInfo()`，**不是**任何函数的 `STI`。这就是「`.attribute arch` 只描述模块基线」的根源：这里写一次，之后所有 per-function `.option arch` 都不回写它。

### 22.3.5 `emitFunctionEntryLabel`：唯一的逐函数 attribute

`:707` 只有四行：若 `RISCVMachineFunctionInfo::isVectorCall()` 为真，调 `RTS.emitDirectiveVariantCC(*CurrentFnSym)`，然后交给基类。`variant_cc` 是**符号级**信息，落在 `.symtab` 的 `st_other`（`STO_RISCV_VARIANT_CC`，`RISCVELFStreamer.cpp:148-151`），不进 `.riscv.attributes`。

---

## 22.4 `RISCVTargetStreamer`：一次调用，两种落点

`RISCVTargetStreamer` 是纯虚接口（`RISCVTargetStreamer.h:35-72`），基类所有 `emitDirective*` 都是空实现（`RISCVTargetStreamer.cpp:34-53`）。两个子类各写一半：

| 方法 | `RISCVTargetAsmStreamer`（`.s`） | `RISCVTargetELFStreamer`（`.o`） |
|---|---|---|
| `emitDirectiveOptionPush` | 打印 `\t.option\tpush`（`:98`） | `ArchStringStack.push_back`（`:67`） |
| `emitDirectiveOptionPop` | 打印 `\t.option\tpop`（`:102`） | 恢复 `ArchString`，可能换 `$x` 符号（`:71`） |
| `emitDirectiveOptionArch` | 打印 `.option arch, ±ext`（`:138`） | **空实现**（继承基类） |
| `emitDirectiveOptionRVC/NoRVC` | 打印 `.option rvc/norvc`（`:114`） | **空实现**（`:82-83`） |
| `emitDirectiveOptionRelax/NoRelax` | 打印 `.option relax/norelax`（`:130`） | **空实现**（`:80-81`） |
| `emitAttribute` / `emitTextAttribute` | 打印 `.attribute tag, value`（`:162`/`:166`） | `setAttributeItem(..., Overwrite=true)`（`:85`/`:89`） |
| `finishAttributeSection` | 空（`:175`） | 写出 `.riscv.attributes`（`:101`） |

这张表解释了一个常见困惑：**`.option arch` 在 ELF 路径下什么都不做**。它不需要做事——ELF 路径没有「汇编器状态」，指令已是字节。这里只跟踪 `ArchString`，用于生成 `$x<ISA>` mapping symbol（22.8）。

基类的 `setFlagsFromFeatures`（`:60`）两个子类共用：

```cpp
HasRVC = STI.hasFeature(RISCV::FeatureStdExtZca);
HasTSO = STI.hasFeature(RISCV::FeatureStdExtZtso);
```

判据是 **`Zca` 而非 `C`**。本树里 `c` 隐含 `zca`（`updateCZceFeatureImplications`），通常等价；若只开 `zca` 不开 `c`，`EF_RISCV_RVC` 仍会置位——这符合 psABI（「可能含压缩指令」）。

---

## 22.5 `.option` 家族

### 22.5.1 七个成员

| 指令 | 汇编器效果（`RISCVAsmParser.cpp`） | 后端是否输出 |
|---|---|---|
| `.option push` / `.option pop` | feature bits + parser options 入栈（`:3317`）/ 出栈，空栈报错（`:3326`） | 是，成对出现 |
| `.option arch, ...` | 改 feature set（`:3338`） | 是，per-function 差异 |
| `.option rvc` / `.option norvc` | `setFeatureBits(C)`（`:3456`）/ 清 `C` 与 `Zca`（`:3467`） | 否 |
| `.option relax` / `.option norelax` | `setFeatureBits(Relax)`（`:3497`）/ 清 `Relax`（`:3506`） | 否（实测） |

另有 `.option exact` / `.option noexact`（`:3436`）：切 `ExactAssembly` 并清/置 `Relax`，AsmPrinter 只在 `emitLpadAlignedCall`（`:310`）用它把 `call` 包起来，禁止链接期改写。

### 22.5.2 为什么每个函数都要 `push/pop`

per-function `target` attribute（如 `__attribute__((target("arch=+zba")))`）在 IR 里是函数级 `target-features`，输出层没有对应概念——汇编器只有「当前状态」。`push/pop` 就是把函数级状态映射成汇编器状态栈：

```
	.option	push
	.option	arch, +zba
	...                     ← g 的函数体
	.option	pop
```

没有这一对，`+zba` 会泄漏到下一个函数，`.s` 重新汇编后语义就变了。这也解释了 `emitTargetFeaturePop(STI, DidPush)` 为何要检查 `DidPush`：无差异时多一个 `pop`，汇编器会报 `.option pop with no .option push`（实测错误文本，来自 `popFeatureBits()` 返回 `true`）。

### 22.5.3 `-mattr=+c` 与 `.option rvc` 不是一回事

- `-mattr=+c` 是**命令行 feature**：影响 `RISCVSubtarget`、指令选择、压缩、`HasRVC`、`e_flags`。
- `.option rvc` 是**汇编器指令**：只影响「接下来能不能接受/生成压缩指令」，不改 `e_flags`，也不改已生成的 `.attribute arch`。

唯一交集：`.s` 重新汇编时，`.option rvc` 决定汇编器是否接受 `c.*`。手写 `.option rvc` 而命令行无 `+c`，会得到「有压缩指令、无 RVC 位」的 `.o`（22.11 实验 10）。

---

## 22.6 `.attribute` 家族

### 22.6.1 常用 tag

| Tag | 名字 | 值类型 | 谁写 | 实测 |
|---:|---|---|---|---|
| 4 | `Tag_RISCV_stack_align` | ULEB128 | `emitTargetAttributes`（ABI 已知） | `stack_align: 16-bytes` |
| 5 | `Tag_RISCV_arch` | 字符串 | `emitTargetAttributes` + `.attribute 5` | `Tag_RISCV_arch: "rv64i2p1_..."` |
| 6 | `Tag_RISCV_unaligned_access` | ULEB128 | 只有手写 `.attribute` | `Unaligned access` |
| 8/10/12 | `Tag_RISCV_priv_spec[_minor/_revision]` | ULEB128 | 只有手写 `.attribute` | `Tag_RISCV_priv_spec: 2` |

定义在 `llvm/include/llvm/Support/RISCVAttributes.h:28-37`。**奇数是字符串、偶数是整数**（`RISCVAsmParser.cpp:3561` 注释），这是解析器判断值类型的唯一依据。

另有 `Tag_RISCV_atomic_abi`（14），只在 `-mabi-attributes` 且 `+a` 时由 `emitTargetAttributes` 写出，本章未实测（需要该开关）。`stack_align` 来自 `RISCVABI::getStackAlignment`（`RISCVBaseInfo.h:816-841`）：`ILP32E` → 4，`LP64E` → 8，其余 → 16。所以 RVE 目标的 `.attribute 4` 是 4 或 8，不是 16。

### 22.6.2 ISA 字符串规范化

`emitTargetAttributes`（`RISCVTargetStreamer.cpp:65-91`）走 `RISCVFeatures::parseFeatureBits(STI)` → `RISCVISAInfo::toString()`。两条规则：

1. 前缀 `rv32`/`rv64`；单字母按 `i/e/y` → `mafdqlcbkjtvph` 固定顺序（`RISCVISAUtils.cpp:16-70`）。
2. 每项带版本 `<major>p<minor>`（`i2p1` = 2.1）；多字母扩展 `z*` 先，按**第二字母**套同一张单字母 rank 表（`zicsr`/`zifencei` 最前，`zmmul` 次之，`zca`/`zcd` 靠后），然后 `s*`，最后 `x*`。

实测把 `.attribute arch, "rv64gc"` 交给 `llvm-mc`，输出被展开为：

```
rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0
```

`c` 展开出 `zca`/`zcd`，`m` 展开出 `zmmul`，`a` 展开出 `zaamo`/`zalrsc`。**`.attribute` 里永远是展开后的集合**，不是用户写的简写。

### 22.6.3 `.attribute arch` 会重置 feature set

`parseDirectiveAttribute` 对 tag 5 走 `resetToArch(..., /*FromOptionDirective=*/false)`（`:3593`），而 `resetToArch`（`:3261-3302`）是**整体替换**：对每个 supported extension，按 `ISAInfo->hasExtension` 与当前 `hasFeature` 的差异 `ToggleFeature`。

实测：`.attribute arch, "rv64gc"` + `-mattr=+v`，再写 `vadd.vv` → 报 `instruction requires ... 'V'`；删掉那行 `.attribute` 则通过。**它覆盖命令行，不与命令行取交集。**

---

## 22.7 `e_flags` 六位

### 22.7.1 位定义

| 位 | 值 | 常量（`ELF.h:520-527`） | 来源 |
|---|---:|---|---|
| 0 | `0x1` | `EF_RISCV_RVC` | `HasRVC`（= `+zca`） |
| 1–2 | `0x6` | `EF_RISCV_FLOAT_ABI`（掩码） | ABI |
| | `0x0`/`0x2`/`0x4`/`0x6` | `..._SOFT`/`..._SINGLE`/`..._DOUBLE`/`..._QUAD` | `ilp32(d)`/`lp64(d)`；`QUAD` 无 LLVM ABI 映射，走不到 |
| 3 | `0x8` | `EF_RISCV_RVE` | `isRVE(ABI)` |
| 4 | `0x10` | `EF_RISCV_TSO` | `HasTSO`（= `+ztso`） |

### 22.7.2 唯一的赋值点

全树只有一处写 `e_flags`：`RISCVTargetELFStreamer::finish()`（`RISCVELFStreamer.cpp:110-135`）。

```cpp
unsigned EFlags = W.getELFHeaderEFlags();
if (hasRVC())  EFlags |= ELF::EF_RISCV_RVC;              // :118
if (hasTSO())  EFlags |= ELF::EF_RISCV_TSO;              // :120
if (RISCVABI::isRVE(ABI)) EFlags |= ELF::EF_RISCV_RVE;   // :123
if (RISCVABI::isSoftFP(ABI))        EFlags |= ELF::EF_RISCV_FLOAT_ABI_SOFT;   // :126
else if (RISCVABI::isSingleFP(ABI)) EFlags |= ELF::EF_RISCV_FLOAT_ABI_SINGLE; // :128
else if (RISCVABI::isDoubleFP(ABI)) EFlags |= ELF::EF_RISCV_FLOAT_ABI_DOUBLE; // :130
else assert(ABI == RISCVABI::ABI::Unknown);
```

三个推论：**`HasRVC`/`HasTSO` 在构造时固定**（构造函数 `:27-31` 调 `setFlagsFromFeatures`），之后任何 `.option rvc`/`.option arch, +c` 都不改它；**`ABI` 是单值**，来自 `emitStartOfAsmFile` 的 `setTargetABI`，一个 `.o` 只有一个 float ABI；**没有 `QUAD` 分支**，要支持 `lp64q` 得先扩 ABI 枚举。

### 22.7.3 它不记录什么

`e_flags` 只有 5 个有效位。`zba`、`v`、`zicond` **统统不在里面**，只出现在 `Tag_RISCV_arch`。所以：判断「能否在只支持 `rv64gc` 的核上跑」看 attributes；判断「能否与另一个 `.o` 链接」看 `e_flags`。

`lld` 的策略（`lld/ELF/Arch/RISCV.cpp:167-190`）：

```cpp
uint32_t target = getEFlags(ctx, ctx.objectFiles.front());
for (InputFile *f : ctx.objectFiles) {
  uint32_t eflags = getEFlags(ctx, f);
  if (eflags & EF_RISCV_RVC) target |= EF_RISCV_RVC;
  if ((eflags & EF_RISCV_FLOAT_ABI) != (target & EF_RISCV_FLOAT_ABI)) Err(...);
  if ((eflags & EF_RISCV_RVE) != (target & EF_RISCV_RVE)) Err(...);
}
```

RVC 是「或」，float ABI 与 RVE 要求「相等」，**TSO 根本不比较**（取第一个文件的）。TSO 混链在 lld 这里不报错。

---

## 22.8 mapping symbol：第三个被忽略的输出

`.o` 里除了 `.text` 与 `.riscv.attributes`，还有 `$x`/`$d` 符号，回答「这段字节是代码还是数据、用什么 ISA 编的」。

```cpp
// RISCVELFStreamer.cpp:180-189
bool NeedSymbol =
    LastEMS != EMS_Instructions || LastEmittedArch != MappingSymbolArch;
if (NeedSymbol) {
  if (MappingSymbolArch.empty())
    emitMappingSymbol("$x");
  else
    emitMappingSymbol("$x" + MappingSymbolArch);
  LastEmittedArch = MappingSymbolArch;
}
```

`MappingSymbolArch` 由 `RISCVTargetELFStreamer::setArchString`（`:60-65`）维护，去重逻辑保证同一 ISA 连续出现时不重复发符号。实测一个模块里三个函数分别用 `rv64gc`、`+zba`、`+v`：

```
2: 0x00 ... $xrv64i2p1_..._zca1p0_zcd1p0        7: 0x00 4 FUNC f
3: 0x04 ... $xrv64i2p1_..._zca1p0_zcd1p0_zba1p0 8: 0x04 4 FUNC g
4: 0x08 ... $xrv64i2p1_..._c2p0_v1p0_..._zvl64b  9: 0x08 8 FUNC h
```

偏移 0/4/8 与 `f`/`g`/`h` 一一对应。**per-function `.option arch` 在 `.o` 里的最终形态不是 directive，而是符号**；反汇编器与调试器靠它把地址映射回正确的 ISA。

---

## 22.9 与 lld / binutils / QEMU 的契约

| 消费者 | 读什么 | 读错会怎样 |
|---|---|---|
| `ld.lld` / GNU `ld` | `e_flags`（float ABI、RVE、RVC、TSO 部分使用）；GNU ld 还读 `Tag_RISCV_arch` | ABI/RVE 不一致 → 硬错误；RVC 影响 relax 对齐；GNU ld 另做 attribute 兼容性检查 |
| `llvm-objdump` / `gdb` | `$x<ISA>` mapping symbol | 用错扩展，显示 `unknown` |
| QEMU / 内核 alternatives | `Tag_RISCV_arch`（内核还读 `.note.gnu.property`） | QEMU 报「ELF 要求扩展 X，当前 CPU 不支持」；补丁点选错则运行非法指令 |

`.note.gnu.property` 由 `emitNoteGnuProperty`（`RISCVAsmPrinter.cpp:1051`）在 `emitEndOfAsmFile` 写出，内容为 `GNU_PROPERTY_RISCV_FEATURE_1_AND`；CFI/Zicfilp 范畴，本章不展开。

---

## 22.10 源码落点

| 位置 | 行 | 读什么 |
|---|---:|---|
| `lib/Target/RISCV/RISCVAsmPrinter.cpp` | 276–284 / 295–359 | `EmitToStreamer`：压缩 + 计数；`emitLpadAlignedCall`：唯一手工 `push/exact/pop`（309/310/329） |
| 同上 | 390–452 | `emitInstruction`：谓词校验、NTL hint、伪指令分流 |
| 同上 | 550–597 | `emitTargetFeaturePush/Pop` + `runOnMachineFunction`（568/569/581/584） |
| 同上 | 652–714 | `emitStartOfAsmFile`（694）/ `emitEndOfAsmFile` / `emitFunctionEntryLabel` |
| `MCTargetDesc/RISCVTargetStreamer.cpp` | 34–53 / 60–91 | 基类空实现；`setFlagsFromFeatures`（`HasRVC=+zca`、`HasTSO=+ztso`）+ `emitTargetAttributes` |
| 同上 | 98–175 | `RISCVTargetAsmStreamer`：每条 directive 的文本形态 |
| `MCTargetDesc/RISCVELFStreamer.cpp` | 27–74 | 构造时固定 flags；`setArchString`；push-pop 栈 |
| 同上 | 85–108 / **110–135** / 165–202 | attribute 三项写入 + `finishAttributeSection`；`finish`：`e_flags` 唯一赋值点（118/120/123/126/128/130）；`$x`/`$d` mapping symbol |
| `MCTargetDesc/RISCVAsmBackend.cpp` + `AsmParser/RISCVAsmParser.cpp` | 968–970；3261–3302 | `createObjectTargetWriter`（ELF vs MachO）；`resetToArch`（整体替换 feature set） |
| 同上 | 3306–3520 / 3527–3605 | `parseDirectiveOption` 七个分支；`parseDirectiveAttribute`（奇偶定值类型，tag 5 特殊） |
| `lib/Support/RISCVISAUtils.cpp` + `lib/TargetParser/RISCVISAInfo.cpp` | 16–120；1065–1089 | 规范顺序 rank 表（`mafdqlcbkjtvph`）；`toString` 拼规范串 |
| `lib/MC/MCELFStreamer.cpp` | 478–530 / 563–610 | `setAttributeItem`；section 的 0x41/vendor/`Tag_File` 布局 |
| `include/llvm/BinaryFormat/ELF.h` + `lld/ELF/Arch/RISCV.cpp` | 520–527；161–190 / 935 | 六个 `EF_RISCV_*` 常量；`calcEFlags`；relax 读 `EF_RISCV_RVC` |
| `clang/lib/CodeGen/CodeGenModule.cpp` | 1575–1592 | 写 `riscv-isa` module flag（LTO） |

---

## 22.11 可跑实验

均在 `/tmp/ch22` 执行，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

### 实验 1：`clang -S` 的两种 `.attribute arch`

```bash
printf 'int add(int a, int b) { return a + b; }\n' > add.c
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc  -mabi=lp64d -O2 -S add.c -o add_gc.s
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gcv -mabi=lp64d -O2 -S add.c -o add_v.s
```

```
add_gc.s:  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
add_v.s :  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_v1p0_zicsr2p0_..._zca1p0_zcd1p0_zve32f1p0_zve32x1p0_zve64d1p0_zve64f1p0_zve64x1p0_zvl128b1p0_zvl32b1p0_zvl64b1p0"
```
两份都**没有** `.option`——模块基线与函数一致时不该有。

### 实验 2：per-function target attribute 触发 `.option push/arch/pop`

```bash
cat > feat.c <<'EOF'
int f(int a, int b) { return a + b; }
__attribute__((target("arch=+zba"))) int g(int *p) { return p[3]; }
__attribute__((target("arch=+v"))) int h(int a) { return a * 3; }
EOF
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S feat.c -o feat.s
```

```
	.option	push
	.option	arch, +zba
g:	...
	.option	pop
	.option	push
	.option	arch, +v, +zve32f, +zve32x, +zve64d, +zve64f, +zve64x, +zvl128b, +zvl32b, +zvl64b
h:	...
	.option	pop
```

（上为节选）`+v` 带出 9 个隐含扩展，顺序是 feature 表字母序——22.3.2 那段循环的直接结果。

### 实验 3–6：`e_flags` 六种输入

```bash
cat > add.ll <<'EOF'
target triple = "riscv64-unknown-elf"
define i32 @add(i32 %a, i32 %b) { %r = add i32 %a, %b  ret i32 %r }
EOF
$T/bin/llc -mtriple=riscv64 -filetype=obj -mattr=+m,+a,+f,+d,+c -target-abi=lp64d add.ll -o c_on.o
$T/bin/llc -mtriple=riscv64 -filetype=obj -mattr=+m,+a,+f,+d,-c -target-abi=lp64d add.ll -o c_off.o
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64  -O2 -c add.c -o a_lp64.o
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64f -O2 -c add.c -o a_lp64f.o
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc_ztso -mabi=lp64d -O2 -c add.c -o a_ztso.o
$T/bin/clang --target=riscv32-unknown-elf -march=rv32e -mabi=ilp32e -c add.c -o e32.o
for f in c_on.o c_off.o a_lp64.o a_lp64f.o a_ztso.o e32.o; do $T/bin/llvm-readelf -h $f | grep Flags; done
```

```
  Flags:  0x5, RVC, double-float ABI        ← llc -mattr=+c（或 clang rv64gc/lp64d）
  Flags:  0x4, double-float ABI             ← llc -mattr=-c
  Flags:  0x1, RVC                          ← lp64
  Flags:  0x3, RVC, single-float ABI        ← lp64f
  Flags:  0x15, RVC, double-float ABI, TSO  ← rv64gc_ztso
  Flags:  0x8, RVE                          ← rv32e + ilp32e
```

混链（`f1_lp64d.o` + `f2_lp64.o`）实测：`ld.lld: error: f2_lp64.o: cannot link object files with different floating-point ABI from f1_lp64d.o`，`exit=1`。

### 实验 7：`rv64gc` vs `rv64gc_zba` 只差 attribute

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc_zba -mabi=lp64d -O2 -c add.c -o add_zba.o
$T/bin/llvm-readelf -A add_zba.o | grep "Value: rv64"
```

```
rv64..._zcd1p0  ← rv64gc        rv64..._zcd1p0_zba1p0  ← rv64gc_zba
```

`e_flags` 两者都是 `0x5`——`zba` 不进 `e_flags`。

### 实验 8：`.option norelax` 抑制 `R_RISCV_RELAX`

```bash
cat > relax.s <<'EOF'
	.text
bar:
	.option push
	.option norelax
	call	foo
	.option pop
	call	baz
EOF
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+f,+d,+c,+relax -filetype=obj relax.s -o relax.o
$T/bin/llvm-readelf -r relax.o
```

```
0000000000000000 ... R_RISCV_CALL_PLT   foo + 0
0000000000000008 ... R_RISCV_CALL_PLT   baz + 0
0000000000000008 ... R_RISCV_RELAX
```

`foo` 那条**没有** `R_RISCV_RELAX`——`.option norelax` 在汇编期就清掉 `+relax`。对照 Ch 12（`relaxCall`）与 Ch 18（`PseudoCALL` 展开）：没有 `R_RISCV_RELAX` 的地方链接器不动作。另有一条对照：`clang -mrelax` 与 `-mno-relax` 的 `-S` 输出逐字节相同（实测 `diff` 为空），但 `.o` 里前者多一条 `R_RISCV_RELAX`。

### 实验 9：`llvm-mc` 手写 `.attribute` / `.option` 往返

```bash
cat > allattr.s <<'EOF'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zba1p0"
	.attribute	6, 1
	.attribute	8, 2
	.text
f:
	add	a0, a0, a1
	ret
EOF
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+f,+d,+c,+zba -target-abi=lp64d \
  -filetype=obj allattr.s -o allattr.o
$T/bin/llvm-readobj -A allattr.o
```

```
      Attribute { Tag: 4   Value: 16  TagName: stack_align  Description: Stack alignment is 16-bytes }
      Attribute { Tag: 5   TagName: arch  Value: rv64i2p1_..._zca1p0_zcd1p0_zba1p0 }
      Attribute { Tag: 6   Value: 1   TagName: unaligned_access  Description: Unaligned access }
      Attribute { Tag: 8   TagName: priv_spec  Value: 2 }
```

tag 5 被**重新规范化**（`a` 带出 `zaamo`/`zalrsc`，`c` 带出 `zca`/`zcd`，另补 `zicsr`/`zmmul`），4/6/8 原样保留；同一份 `.o` 的 `Flags` 仍是 `0x5`——`.attribute` 不参与 `e_flags`。GNU `readelf -A` 对同一文件给出另一种格式：

```
Attribute Section: riscv
  Tag_RISCV_stack_align: 16-bytes   Tag_RISCV_arch: "rv64i2p1_..._zba1p0"
  Tag_RISCV_unaligned_access: Unaligned access   Tag_RISCV_priv_spec: 2
```

### 实验 10：`.option rvc` / per-function `+c` 与 `e_flags` 错位

```bash
printf '\t.text\n\t.option\trvc\n\tc.addi\ta0, 1\n\tret\n' > rvc_only.s
$T/bin/llvm-mc -triple=riscv64 -mattr=-c -filetype=obj rvc_only.s -o rvc_off.o
$T/bin/llvm-readelf -h rvc_off.o | grep Flags   # 0x0
$T/bin/llvm-objdump -d rvc_off.o
#   0: 0505   addi a0, a0, 0x1     ← 2 字节，压缩指令
#   2: 8082   ret
```

per-function `+c` 同样漏位（`withc` 的 `9d2d` 是 `c.addw`，而 `Flags: 0x4`）：

```bash
cat > perfn.ll <<'EOF'
target triple = "riscv64-unknown-elf"
define i32 @withc(i32 %a, i32 %b) #0 { %r = add i32 %a, %b  ret i32 %r }
define i32 @without(i32 %a, i32 %b) { %r = sub i32 %a, %b  ret i32 %r }
attributes #0 = { "target-features"="+m,+a,+f,+d,+c" }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -target-abi=lp64d -filetype=obj perfn.ll -o perfn.o
$T/bin/llvm-readelf -h perfn.o | grep Flags   # 0x4
```

`.s` 里明明有 `.option push` + `.option arch, +c, +zca, +zcd`，`e_flags` 却没有 RVC 位——`HasRVC` 在构造流式器时就定死了。

---

## 22.12 踩雷集锦

1. **`.option arch` 的完整形式可以「悄悄扩大」。** 实测 `-mattr=+m,+a,+f,+d,+c` 下写 `.option arch, rv64gcv`，`vadd.vv` 被接受（`exit=0`）；不写则报 `instruction requires ... 'V'`。psABI 期望「只能收窄」，但 `resetToArch` 只检查 rv32/rv64 位宽，不检查子集关系（`RISCVAsmParser.cpp:3278-3282`）。**别依赖它做特性隔离。**
2. **per-function target-features 与全局 `-mattr` 的冲突有两个出口。** `.attribute arch, "rv64gc"` + `-mattr=+v` 会让 `vadd.vv` 失败（整体替换）；反过来模块级无 `+c` 而函数带 `+c` 时，`emitTargetFeaturePush` 只发 `.option arch, +c`，`.attribute arch` 与 `e_flags` 仍是模块基线（实验 10）。两边都要看。
3. **`e_flags` 不记录 `zba`/`v`**（只看构造时的 `+zca` 决定 RVC 位）。判断扩展支持看 `Tag_RISCV_arch`，判断链接兼容看 `e_flags`；per-function `+c` 与 `.option rvc` 都能产出压缩指令而不置位（实验 10）。lld 若用 RVC 位做对齐假设（`RISCV.cpp:935`），结果就是「压缩指令被当成 4 字节对齐」。
4. **`Tag_RISCV_arch` 的顺序是硬约束，不是字典序。** `i` 打头，再 `mafdqlcbkjtvph`，`z*` 按第二字母套同一张表（所以 `zicsr` 在 `zaamo` 之前），最后 `s*`、`x*`。手写顺序错会被规范化掉，diff 测试会莫名失败。
5. **内联汇编里的 `.option` 会泄漏。** CodeGen 的 `push/pop` 只包「函数级 feature 差异」，包不住用户在 `asm()` 里写的 `.option rvc`。在 `-S` 输出里它会改掉后续函数的汇编状态；在 `.o` 路径下可能产出与 `e_flags` 不符的字节。
6. **`-mno-relax` 与 `.option norelax` 是两条独立通道。** 前者 driver → `-mattr=-relax`，只影响 `.o` 是否发 `R_RISCV_RELAX`；后者是汇编器状态。两者都**不改 `.s` 文本**（实测 `-mrelax`/`-mno-relax` 的 `-S` 逐字节相同）。
7. **`llvm-readelf` 与 GNU `readelf` 输出不同；本树没有 `llvm-readobj --attributes`。** 正确开关是 `-A` / `--arch-specific`（`--attributes` 报 `unknown argument`）。GNU 打 `Tag_RISCV_*` 名字，LLVM 打 `Tag:`/`TagName:`/`Value:` 三段——写 FileCheck 时选一边，别混。

---

## 22.13 本章重点整理

- 输出层是后端对外唯一承诺（`e_flags` 错 → 链接错误，`.attribute` 错 → 兼容性错误）；一个函数 = 一个 `.option` 作用域（push 在 `SetupMachineFunction` 前，pop 在 `emitXRayTable` 后）；`emitTargetFeaturePush` 相对**模块级** STI 做差，只处理 `isSupportedExtensionFeature`，输出字母序且不叠加。
- 压缩在 `RISCVAsmPrinter::EmitToStreamer`（`RISCVRVC::compress`）完成，`.s` 与 `.o` 因此看到同一批 `C_*`；`RISCVTargetStreamer` 是虚接口，文本路径打印 directive，ELF 路径改状态或写 section，`.option arch` 在 ELF 路径是空实现。
- `.option` 七个成员中 `arch`/`rvc`/`norvc`/`relax`/`norelax` 改汇编器 feature，`push`/`pop` 管状态栈；AsmPrinter 只主动输出 `push`/`pop`/`arch`（`exact` 仅见 lpad 路径），`rvc`/`norvc`/`relax`/`norelax` 在 ELF 路径下是空实现。
- `.attribute` 奇数 tag 是字符串、偶数是整数；tag 5 先 `resetToArch` 再规范化，所以覆盖命令行；规范顺序：`rv64` → `i`/`e` → `mafdqlcbkjtvph` → `z*`（按第二字母）→ `s*` → `x*`，每项带 `major p minor`。
- `e_flags` 只有五位有效：RVC(0x1)、FLOAT_ABI(0x2/0x4/0x6)、RVE(0x8)、TSO(0x10)，唯一赋值点是 `RISCVTargetELFStreamer::finish()`；lld 对 RVC 取并集，对 float ABI 与 RVE 要求相等，不比较 TSO；per-function ISA 在 `.o` 里是 `$x<ISA>` mapping symbol 而非 directive，`.attribute` 不参与 `e_flags`。

---

## 22.14 自我检核

- [ ] 能画出 `MachineFunction → MCInst → (.s | .o) → e_flags/attributes` 的完整链路，并说出每层职责
- [ ] 能解释 `emitTargetFeaturePush` 为什么相对 `TM.getMCSubtargetInfo()` 而不是上一个函数做差
- [ ] 能说出 `EmittedOptionArch` 为 `false` 时为什么不能发 `.option pop`
- [ ] 能列出 `.option` 七个成员，并指出哪几个在 ELF 路径下是空实现
- [ ] 能背出 `.attribute` 的四个常用 tag（4/5/6/8）与奇偶值类型规则，并手工推出 `rv64gc + lp64d` 的 `e_flags = 0x5`、说明每位来自哪个函数；能解释为什么 per-function `+c` 会产出「有压缩指令、无 RVC 位」的 `.o`
- [ ] 能写出 `Tag_RISCV_arch` 的规范顺序规则，并解释 `zicsr` 为何排在 `zaamo` 前
- [ ] 能用 `llvm-mc` + `llvm-readobj -A` 独立复现一份带四个 tag 的 `.o`

---

## 22.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVAsmPrinter.cpp`**：读 276–359、390–452、550–597、652–714。`.option` 生成的唯一来源；`emitLpadAlignedCall` 是唯一手工拼 `push/exact/pop` 的地方。配套读 **`MCTargetDesc/RISCVTargetStreamer.cpp`**（34–91、98–175）与 **`RISCVELFStreamer.cpp`**（43–74、101–135、165–202）：前者是「某条 directive 打印成什么」的全部答案，后者装着 `e_flags` 与 mapping symbol。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`** 与 **`llvm/lib/Support/RISCVISAUtils.cpp`**：读 3261–3302、3306–3520、3527–3605 与 16–120。前者是 `.s` 语义的另一半（「我的 .s 为什么汇编失败」多半落在 `resetToArch`），后者是规范顺序 rank 表；配合 **`llvm/test/MC/RISCV/attribute.s`**（tag ↔ 文本对照）与 **`attribute-with-option.s`**（`.attribute arch` 覆盖 `-mattr` 的标准测试形状）一起读。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/mapping-isa-attribute.s`** 与 **`lld/test/ELF/riscv-flags.s`**：前者读文件头注释与 `CHECK: Name: $x...`（`.attribute 5` 如何「替换」而非「追加」初始 mapping symbol 的官方口径），后者读 `ERR:` 与 `CHECK: Flags`（改 `finish()` 前先读它）。

→ 下一章：[Ch 23 — GlobalISel 现状](./23-globalisel-status.md)
