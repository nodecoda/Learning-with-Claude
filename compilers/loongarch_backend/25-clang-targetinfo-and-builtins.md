# Ch 25 — Clang 里的 LoongArch：TargetInfo、宏与 builtin 数据库

> 本章目标：知道 `clang` 侧 LoongArch 的代码分布；能读懂 `TARGET_BUILTIN` 的四个字段；能解释 `__builtin_lsx_vadd_w` 为什么不需要手写 CodeGen 代码就能变成 `llvm.loongarch.lsx.vadd.w`。

---

## 25.1 clang 侧只有五个文件

| 文件 | 行数量级 | 职责 |
|---|---:|---|
| `clang/lib/Basic/Targets/LoongArch.cpp` | ~500 | `TargetInfo`：宏、feature map、寄存器、builtin 注册 |
| `clang/lib/Basic/Targets/LoongArch.h` | — | `LoongArchTargetInfo` 声明 |
| `clang/include/clang/Basic/BuiltinsLoongArchBase.def` | 58 | 基础 builtin（CSR、屏障、cache、CRC…） |
| `clang/include/clang/Basic/BuiltinsLoongArchLSX.def` | 959 | LSX（128 位） |
| `clang/include/clang/Basic/BuiltinsLoongArchLASX.def` | 1,007 | LASX（256 位） |
| `clang/lib/Sema/SemaLoongArch.cpp` | ~200 | 立即数范围等语义检查 |
| `clang/lib/CodeGen/Targets/LoongArch.cpp` | 469 | **ABI**（参数分类、varargs、sret），不含 builtin lowering |

注意最后一行：`clang/lib/CodeGen/Targets/LoongArch.cpp` 里**没有** `EmitLoongArchBuiltinExpr`。LoongArch 的 builtin 走的是通用自动映射路径（25.4）。

---

## 25.2 一个 builtin 条目的四个字段

```tablegen
// BuiltinsLoongArchLSX.def:17
TARGET_BUILTIN(__builtin_lsx_vadd_w, "V4iV4iV4i", "nc", "lsx")
//              ①名字                ②类型字符串   ③属性  ④所需 feature
```

- **① 名字**：`__builtin_` 前缀 + ISA 家族前缀（`loongarch_` / `lsx_` / `lasx_`）。
- **② 类型字符串**：LLVM 的 builtin 类型编码。`V4i` = 4×i32 向量；`UWi` = unsigned long（64 位）、`Ui` = unsigned int、`Wi` = long。
- **③ 属性**：`n` = nothrow，`c` = const。`nc` 是绝大多数纯计算 builtin 的写法。
- **④ feature**：空串表示无条件可用；`"lsx"` 表示需要 `+lsx`。这一列就是 `-mno-lsx` 时报错的依据：

```text
$ clang --target=loongarch64-unknown-linux-gnu -mno-lsx -O2 -c b.c
b.c:2:36: error: '__builtin_lsx_vadd_w' needs target feature lsx
```

基础 builtin 里能看到 architecture 级别的门控：

```tablegen
TARGET_BUILTIN(__builtin_loongarch_cacop_d, "vWiUWiWi", "nc", "64bit")
TARGET_BUILTIN(__builtin_loongarch_cacop_w, "viUii",     "nc", "32bit")
TARGET_BUILTIN(__builtin_loongarch_dbar,    "vIUi",      "nc", "")
```

`dbar` 的 feature 列是空的——它在 LA32/LA64 上都能用；`cacop_d` 只在 64 位可用。

---

## 25.3 builtin 是怎么被“注册”的

`clang/lib/Basic/Targets/LoongArch.cpp:300-345` 把同一个 `.def` 文件 **include 两遍**：一遍生成字符串表，一遍生成 `Builtin::Info` 数组：

```cpp
static constexpr llvm::StringTable BuiltinLSXStrings =
    CLANG_BUILTIN_STR_TABLE_START
#define TARGET_BUILTIN CLANG_TARGET_BUILTIN_STR_TABLE
#include "clang/Basic/BuiltinsLoongArchLSX.def"
#undef TARGET_BUILTIN
    ;

static constexpr auto BuiltinLSXInfos = Builtin::MakeInfos<NumLSXBuiltins>({
#define TARGET_BUILTIN CLANG_TARGET_BUILTIN_ENTRY
#include "clang/Basic/BuiltinsLoongArchLSX.def"
#undef TARGET_BUILTIN
});
```

builtin ID 是连续编号的，`clang/include/clang/Basic/TargetBuiltins.h:234-250` 用哨兵值把三段切开：

```cpp
namespace LoongArch {
enum {
  LastTIBuiltin = clang::Builtin::FirstTSBuiltin - 1,
#define TARGET_BUILTIN(ID, TYPE, ATTRS, FEATURE) BI##ID,
#include "clang/Basic/BuiltinsLoongArchBase.def"
  FirstLSXBuiltin,
  LastBaseBuiltin = FirstLSXBuiltin - 1,
#include "clang/Basic/BuiltinsLoongArchLSX.def"
  FirstLASXBuiltin,
  LastLSXBuiltin = FirstLASXBuiltin - 1,
#include "clang/Basic/BuiltinsLoongArchLASX.def"
  LastTSBuiltin
};
}
```

还有一个 `static_assert(NumBuiltins == NumBase + NumLSX + NumLASX)`：**在 `.def` 里加一行，三段计数必须同时自洽**，否则编译期直接失败。这是加 builtin 时第一个会撞上的门。

---

## 25.4 builtin → intrinsic 的自动映射

关键在 `clang/lib/CodeGen/CGBuiltin.cpp:6929-6946`：

```cpp
std::string Name = getContext().BuiltinInfo.getName(BuiltinID);
Intrinsic::ID IntrinsicID = Intrinsic::not_intrinsic;
StringRef Prefix =
    llvm::Triple::getArchTypePrefix(getTarget().getTriple().getArch());
if (!Prefix.empty())
  IntrinsicID = Intrinsic::getIntrinsicForClangBuiltin(Prefix.data(), Name);
```

对 LoongArch，`Prefix` 是 `"loongarch"`。`getIntrinsicForClangBuiltin` 是 TableGen 根据 **builtin 名字 + intrinsic 名字**生成的查表函数：

- builtin `__builtin_lsx_vadd_w` → 去掉 `__builtin_` 后的 `lsx_vadd_w`
- 拼上 target 前缀 → `llvm.loongarch.lsx.vadd.w`

实测（`-march=la464`，注意必须开 LSX 才能用这个 builtin）：

```llvm
define dso_local i128 @t(i128 noundef %a.coerce, i128 noundef %b.coerce) {
  %2 = tail call <4 x i32> @llvm.loongarch.lsx.vadd.w(<4 x i32> %0, <4 x i32> %1)
}
declare <4 x i32> @llvm.loongarch.lsx.vadd.w(<4 x i32>, <4 x i32>) #1
```

汇编端：

```asm
vinsgr2vr.d	$vr0, $a0, 0
vinsgr2vr.d	$vr0, $a1, 1
vadd.w	$vr0, $vr0, $vr1
vpickve2gr.d	$a0, $vr0, 0
vpickve2gr.d	$a1, $vr0, 1
```

两个值得记住的结论：

1. **没有 LoongArch 专属 builtin lowering 代码**：`__builtin_lsx_*` 全部靠命名约定落到 intrinsic，再由后端的 `LoongArchISelLowering` 处理。
2. **`v4i32` 在 ABI 上被拆成两个 GPR**（`i128`），不是走向量寄存器。这是 LoongArch ABI 的规定，也解释了为什么函数边界上有一串 `vinsgr2vr.d`/`vpickve2gr.d`。

intrinsic 定义在 `llvm/include/llvm/IR/IntrinsicsLoongArch.td`，按元素宽度分组：

```tablegen
// :203
foreach inst = ["vadd_w", "vsub_w", ...] in
  def int_loongarch_lsx_#inst : VecInt<[llvm_v4i32_ty],
                                       [llvm_v4i32_ty, llvm_v4i32_ty],
                                       [IntrNoMem]>;
```

---

## 25.5 TargetInfo 的宏：`__loongarch_arch` 不是 `-march` 的回显

实测 `clang -dM -E -x c /dev/null`：

| 配置 | 关键宏 |
|---|---|
| 默认 LA64 | `__loongarch_arch "la64v1.0"`、`__loongarch_sx 1`、`__loongarch_simd_width 128`、`__loongarch_frlen 64` |
| `-march=la664` | `__loongarch_arch "la664"`、`__loongarch_asx 1`、`__loongarch_simd_width 256`、`+div32/frecipe/lam_bh/lamcas/ld_seq_sa/scq` 对应宏 |
| `-mno-lsx` | `__loongarch_arch "loongarch64"`，**没有** `__loongarch_sx` / `__loongarch_simd_width` |

源码（`Targets/LoongArch.cpp:213-240`）显示它是**从 feature 反推**的：

```cpp
StringRef ArchName = getCPU();
if (ArchName == "loongarch64") {
  if (HasFeatureLSX) {
    if (HasFeatureFrecipe && HasFeatureLAM_BH && HasFeatureLAMCAS &&
        HasFeatureLD_SEQ_SA && HasFeatureDiv32 && HasFeatureSCQ)
      Builder.defineMacro("__loongarch_arch", "\"la64v1.1\"");
    else
      Builder.defineMacro("__loongarch_arch", "\"la64v1.0\"");
  } else
    Builder.defineMacro("__loongarch_arch", Twine('"') + ArchName + Twine('"'));
}
```

所以：

- 默认 `target-cpu=loongarch64` + 默认 `+lsx` → 宏是 `la64v1.0`；
- 加 `-mno-lsx` → 宏退回 `loongarch64`；
- `-march=la664` → CPU 是 `la664`，走最后一条分支，宏就是 `la664`。

**宏不保证等于 `-march=` 的拼写**，写 `#if` 时要按“feature 组合”思考。

---

## 25.6 立即数检查在 Sema，不在 CodeGen

```text
$ clang --target=loongarch64-unknown-linux-gnu -c dr.c
dr.c:1:15: error: argument value 32768 is outside the valid range [0, 32767]

$ clang --target=loongarch64-unknown-linux-gnu -c cs.c
cs.c:1:31: error: argument value 16384 is outside the valid range [0, 16383]
```

对应 `SemaLoongArch.cpp:44-60`：

```cpp
case LoongArch::BI__builtin_loongarch_dbar:
  return SemaRef.BuiltinConstantArgRange(TheCall, 0, 0, 32767);   // 15 位
case LoongArch::BI__builtin_loongarch_csrrd_d:
  return SemaRef.BuiltinConstantArgRange(TheCall, 0, 0, 16383);   // 14 位
```

这些范围直接对应指令编码的立即数位宽（dbar 的 15 位掩码、CSR 的 14 位编号）。**加新 builtin 时忘了加 Sema 检查，非法立即数会一路走到汇编器才报错**，错误信息也更难懂。

---

## 25.7 反直觉点

1. **`__builtin_lsx_*` 没有专属 lowering**，全靠名字自动映射到 intrinsic。
2. **`__loongarch_arch` 是 feature 推导值**，不是 `-march` 字符串。
3. **默认 LA64 的 `__loongarch_arch` 是 `la64v1.0`**，尽管 `-###` 里 CPU 显示 `loongarch64`。
4. **`v4i32` 形参在 ABI 上是 `i128`**，会占用两个 GPR。
5. **builtin 的 feature 列是 Sema 错误来源**，不是后端的 `-mattr` 检查。
6. **`BuiltinsLoongArch*.def` 三段的 ID 是连续的**，插入一行会让后面所有 ID 平移（和 Ch 04 的 opcode 平移是同一类问题）。

---

## 25.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| 三个 builtin 数据库行数 | `wc -l`：58 / 959 / 1,007 |
| builtin 四字段 | `BuiltinsLoongArchLSX.def:17` |
| 双 include 注册 | `Targets/LoongArch.cpp:300-345` |
| ID 分段与 static_assert | `TargetBuiltins.h:234-250` |
| 自动 intrinsic 映射 | `CGBuiltin.cpp:6929-6946` |
| intrinsic 定义 | `IntrinsicsLoongArch.td:203` |
| 宏推导 | `Targets/LoongArch.cpp:213-246` + `-dM` 实测 |
| Sema 立即数范围 | `SemaLoongArch.cpp:44-60` + 两条实测报错 |
| ABI lowering | `clang/lib/CodeGen/Targets/LoongArch.cpp` |

---

## 25.9 练习

1. 在 `BuiltinsLoongArchLSX.def` 里加一个 `TARGET_BUILTIN(__builtin_lsx_vadd_w_test, "V4iV4iV4i", "nc", "lsx")`，重编后确认 `TargetBuiltins.h` 的 `static_assert` 是否需要改（提示：三段是自动计数的，但 `NumLSXBuiltins` 依赖哨兵值，试着解释为什么仍然能过）。
2. 用 `-emit-llvm` 验证 `__builtin_loongarch_movfcsr2gr(0)` 落到哪个 intrinsic。
3. 给 `__builtin_loongarch_cpucfg` 写一个 Sema 检查，要求参数是常量（模仿 `dbar` 的写法）。
4. 对比 `-march=la464` 与 `-march=la664` 的 `-dM` 输出，列出 `la664` 多出的 6 个 feature 宏。
5. 解释为什么 `-mno-lsx` 之后 `__loongarch_simd_width` 完全消失，而不是变成 0。

---

## 25.10 下一步

编译器和 builtin 都看完了，下一章看“程序真正跑起来还需要什么”：`compiler-rt` 的 builtins、crt 对象与 runtimes。重点是一个容易被忽略的事实——本课这份构建**没有**编译 `libclang_rt`。
