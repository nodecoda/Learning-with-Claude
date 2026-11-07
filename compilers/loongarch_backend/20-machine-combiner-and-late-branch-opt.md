# Ch 20 — `MachineCombiner` 与 `LateBranchOpt`：RA 前后的两种“重组”

> 本章目标：搞清楚 `fmadd` 是谁形成的（不是 `MachineCombiner`）；知道 `MachineCombiner` 在 LoongArch 上目前其实“空转”；并能用 MIR 直接驱动 `LoongArchLateBranchOpt`，看它把静态可知的条件分支折成无条件跳转。

---

## 20.1 `fmadd` 从哪来：不是 MachineCombiner

```llvm
define double @fma(double %a, double %b, double %c) {
  %m = fmul contract double %a, %b
  %r = fadd contract double %m, %c
  ret double %r
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
fma:
	fmadd.d	$fa0, $fa0, $fa1, $fa2
	ret
```

融合发生在 **SelectionDAG 阶段**：`contract` 标志允许 `fmul`+`fadd` 合成 `fmadd`，pattern 在 `LoongArchFloat64InstrInfo.td` 里。也就是说：

- **不是** `MachineCombiner` 做的；
- **不是** RA 之后做的；
- 如果你去掉 `contract`，会看到 `fmul.d` + `fadd.d` 两条。

```llvm
%m = fmul double %a, %b      ; 没有 contract
%r = fadd double %m, %c
```

```text
	fmul.d	$fa0, $fa0, $fa1
	fadd.d	$fa0, $fa0, $fa2
```

---

## 20.2 `MachineCombiner` 在 LoongArch 上是“空转”

`LoongArchTargetMachine.cpp:73`：

```cpp
static cl::opt<bool>
    EnableMachineCombiner("loongarch-enable-machine-combiner", ..., cl::init(false));

bool LoongArchPassConfig::addILPOpts() {
  if (EnableMachineCombiner)
    addPass(&MachineCombinerID);
  ...
}
```

但 `LoongArchInstrInfo` **没有实现** `getMachineCombinerPatterns()`（本树 `grep` 无结果）。generic `MachineCombiner` 默认返回“没有 pattern”，所以这个 pass 加上去也不改变代码。

**这是一个可以贡献的方向**：MachineCombiner 的典型用途是 RA 之后的重结合（如 `(a+b)+c` 的树形重组、`fmul`+`fadd` 的迟到融合）。LoongArch 目前把融合放在 DAG 阶段，RA 后的重组是空白。

要加 pattern，需要：

1. `LoongArchInstrInfo::getMachineCombinerPatterns(MachineInstr &Root, SmallVectorImpl<unsigned> &Patterns) const override;`
2. 在 `LoongArchInstrInfo.cpp` 里实现模式匹配（通常配合 `MachineCombinerPattern::MULADD` 之类的枚举）；
3. 处理 `Root` 的 operand 交换、`isReassociationProfitable` 检查；
4. 加 `llvm/test/CodeGen/LoongArch/machine-combiner-*.ll`。

**风险**：RA 后重组会改变寄存器压力，必须用 `-stats`/`llvm-mca` 和真实基准验证，不能只看指令数变少。

---

## 20.3 `LateBranchOpt`：静态可知的条件分支

`LoongArchLateBranchOpt.cpp` 的注释写得很清楚：

```cpp
/// This file provides LoongArch specific target optimizations, currently it's
/// limited to convert conditional branches into unconditional branches when
/// the condition can be statically evaluated.
```

它处理四种情况：

| 分支 | 条件 | 折叠为 |
|---|---|---|
| `BEQZ` | 寄存器是 `$r0` | `TBB`（条件真） |
| `BNEZ` | 寄存器是 `$r0` | `FBB`（条件假） |
| `BEQ` | 两个寄存器相同 | `TBB` |
| `BNE` | 两个寄存器相同 | `FBB` |

**为什么放到“late”**：这些条件在 IR 层看不出来，只有经过 RA/伪指令展开后，才会出现 `$r0` 这样的物理寄存器事实。`SimplifyCFG` 和 `BranchFolding` 都看不到这一层。

### 用 MIR 直接驱动

```mir
--- |
  define void @f() { ret void }
...
---
name:            f
alignment:       4
tracksRegLiveness: true
body:             |
  bb.0:
    BEQZ $r0, %bb.1
    B %bb.2
  bb.1:
    PseudoRET
  bb.2:
    PseudoRET
...
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu \
  -run-pass=loongarch-late-branch-opt lb.mir -o -
```

实测输出：

```text
body:             |
  bb.0:
    successors: %bb.1(0x80000000)

    PseudoBR %bb.1

  bb.1:
    PseudoRET

  bb.2:
    PseudoRET
...
```

`BEQZ $r0, %bb.1` + `B %bb.2` 被折成 `PseudoBR %bb.1`；`bb.2` 变成不可达（但还在，等后续 `BranchFolding`/`MachineBlockPlacement` 清理）。

### MIR 的两个坑

1. **寄存器名用数字**：`$r0`，不是 `$zero`。MIR 解析走的是 `getRegisterByName`，不认 ABI alt name。
2. **opcode 名大写且用伪指令名**：`PseudoRET`/`PseudoBR`，不是 `RET`/`B`。先用 `llc -stop-after=...` 生成一份真实的 MIR，照着改最稳。

---

## 20.4 别把 `LateBranchOpt` 和分支放松搞混

| pass | 做什么 |
|---|---|
| `LoongArchLateBranchOpt` | 把**条件已知**的分支折成无条件 |
| `BranchRelaxationPass` | 把**超出范围**的分支换成远跳序列（`b` → `pcaddu18i+jirl` 之类） |

两者都在 late 阶段，但一个问题关于“条件”，一个关于“距离”。`LoongArchPassConfig::addPreEmitPass()` 里加的是 `BranchRelaxationPassID`，不是 `LateBranchOpt`。

`LateBranchOpt` 注册在 `LoongArchPassConfig::addMachineLateOptimization()`（`LoongArchTargetMachine.cpp:251-255`），且只在 `-O1+` 添加：

```cpp
void LoongArchPassConfig::addMachineLateOptimization() {
  if (TM->getOptLevel() != CodeGenOptLevel::None)
    addPass(createLoongArchLateBranchOptPass());
  TargetPassConfig::addMachineLateOptimization();
}
```

---

## 20.5 反直觉点

1. **`fmadd` 不是 MachineCombiner 形成的**，是 DAG 阶段 + `contract`。
2. **`MachineCombiner` 在 LoongArch 上启用了但没 pattern**，等于空转。
3. **`LateBranchOpt` 处理的是“条件静态可知”**，不是分支距离。
4. **`BEQ r, r` 恒真、`BNE r, r` 恒假**，这是它最有用的两条规则。
5. **MIR 里寄存器必须写 `$r0`**，写 `$zero` 会报 unknown register。
6. **`-run-pass` 只跑指定 pass**，不会自动跑前置 pass；MIR 必须已经是该 pass 期望的形态。
7. **`LateBranchOpt` 不删除不可达块**，只改分支；清理交给后面的 pass。

---

## 20.6 本章证据

| 结论 | 源码/命令 |
|---|---|
| `fmadd` 实测 | 本章 20.1 |
| MachineCombiner 开关 | `LoongArchTargetMachine.cpp:73,231` |
| 无 `getMachineCombinerPatterns` | 全目录 grep 无结果 |
| LateBranchOpt 规则 | `LoongArchLateBranchOpt.cpp:53-80` |
| pass 名 | `loongarch-late-branch-opt` |
| MIR 实测 | 本章 20.3 |
| BranchRelaxation 区分 | `LoongArchTargetMachine.cpp:203` |

---

## 20.7 练习

1. 去掉 `contract`，确认 `fmadd` 不再生成。
2. 用 `-loongarch-enable-machine-combiner` 打开 MachineCombiner，对比输出是否变化（预期：不变）。
3. 把 MIR 里的 `BEQZ $r0` 改成 `BEQZ $r4`，确认不再折叠。
4. 加一个 `BEQ $r4, $r4, %bb.1` 的用例，验证折叠。
5. 解释为什么 `LateBranchOpt` 不放在 IR 层：IR 层能看到 `$r0` 吗？

---

## 20.8 延伸阅读

1. **用 `-run-pass` 驱动 LateBranchOpt 的 MIR 流程** — `llvm/docs/MIRLangRef.md`
   - 读什么：`Testing Individual Code Generation Passes` 小节：`-stop-after` 生成输入、`-run-pass` 只跑一个 pass、`-simplify-mir` 精简，以及 MIR 里 `tracksRegLiveness`/`liveins` 的去留
   - 学到什么：本章 20.3 给了一段手写 MIR；手册补上这段 MIR 应该怎么从真实函数生成、哪些字段可以删、什么时候必须保留 liveins。这样手写 `BEQZ $r0` 的用例不会因为缺 successors 或 liveins 被 verifier 拒绝。
   - 前提：读过本章 20.3，知道 `PseudoRET`/`PseudoBR` 与 `$r0` 的写法。

2. **本树的 MachineCombiner 测试与本章说法要对读** — `llvm/test/CodeGen/LoongArch/machine-combiner.ll`
   - 读什么：文件头两条 `RUN`（`-O1`、`-loongarch-enable-machine-combiner=true`）与 `test_reassoc_fadd*`、`test_fmin_nnan_*`、`test_reassoc_add_multiuse` 的正反例
   - 学到什么：本章 20.2 的判断是“没有 `getMachineCombinerPatterns()`，所以 pass 空转”。这个测试说明判据不止一条：本树在 `-O1` 下用开关直接检查 fadd/fmul 重结合，以及 `fmin/fmax` 必须带 `nnan` 才能重结合。读它和 `LoongArchInstrInfo.h` 对照，能自己确认“没有 target pattern”是否等于“没有效果”，而不是只凭一个 grep 结论。
   - 前提：知道 `reassoc`/`nnan`/`nsz` 的 fast-math 含义；读过本章 20.2。

3. **`useMachineCombiner` 与结合性钩子** — `llvm/lib/Target/LoongArch/LoongArchInstrInfo.h`
   - 读什么：`LoongArchInstrInfo` 类尾部的 `useMachineCombiner()`、`isAssociativeAndCommutative()`，以及同处声明的 `getInverseOpcode()`
   - 学到什么：本章 20.2 把“加 pattern”写成实现 `getMachineCombinerPatterns()` 一条路；头文件显示本树走的是另一组钩子：先用 `useMachineCombiner()` 声明本目标要跑，再由 `isAssociativeAndCommutative()` 告诉 generic 算法哪些指令可重结合。补上“generic 算法 + 目标谓词”与“目标自带 pattern”两种接入方式的区别。
   - 前提：读过本章 20.2；能读 C++ 的 override 声明。

4. **LateBranchOpt 的完整判定与 fallthrough 处理** — `llvm/lib/Target/LoongArch/LoongArchLateBranchOpt.cpp`
   - 读什么：`runOnBasicBlock()` 里的 `analyzeBranch`、`BEQZ/BNEZ/BEQ/BNE` 四个 case、`removeBranch`/`insertBranch` 与 successors 更新
   - 学到什么：本章 20.3 的表格只写“折成 TBB/FBB”；源码还有两条没展开的细节：`Cond.size()` 的下限检查（拿不到寄存器操作数就直接放弃），以及 `Folded == nullptr` 时只删分支、把 fallthrough 块设为后继。补上“为什么输出里有时是 `PseudoBR`、有时根本没有分支”。
   - 前提：知道 MachineBasicBlock 的 successors 与 `analyzeBranch` 的 TBB/FBB 约定。

## 20.9 下一步

下一章看原子与内存屏障：`ExpandAtomicPseudoInsts` 的 LL/SC 展开、`MemoryBarrierOpt` 的优化，以及 `dbar`/`ibar` 的语义。
