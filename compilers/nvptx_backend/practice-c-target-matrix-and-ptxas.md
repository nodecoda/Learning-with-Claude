# 练习 C — 版本矩阵、ptxas 资源与 SASS 对照

> **目标**：把 Ch 3、Ch 11、Ch 16 串起来：同一份 PTX 在不同目标/版本/ptxas 选项下的资源与 SASS 变化。做完你能独立做「工具链升级回归」。

## 背景与动机

GPU 编译器团队最常做的回归不是「功能对不对」，而是「换个 ptxas / 换个 arch，寄存器怎么变了、SASS 怎么变了」。这个练习模拟一次小型回归。

## 任务规格

以 `add.ptx`（Ch 0）为输入，完成：

1. 用 `llc` 为 `sm_75`、`sm_80`、`sm_90a` 各生成一份 PTX，记录 `.version` 与 `.target`。
2. 用 `ptxas -arch=sm_75` 编译 sm_75 版本，记录 `REG/STACK/SHARED`。
3. 用 `ptxas -arch=sm_80` 编译「`.target sm_75`」的 PTX，记录 `headerflags`。
4. 用 `--maxrregcount=8` 编一次，记录 REG 与 spill。
5. 用 `cuobjdump -sass` 导出 sm_75 版本，数出 `LDG` 与 `IMAD.WIDE` 各几条。

验收：把结果填成表，并对每一处差异写一句解释。

## 期望输出范例

本机实测：

| 项目 | 结果 |
|---|---|
| `.version`（sm_75 / sm_80 / sm_90a） | `6.3` / `7.0` / `8.0` |
| `.target` | `sm_75` / `sm_80` / `sm_90a` |
| REG（sm_75） | `12` |
| `ptxas -arch=sm_80` 的 headerflags | `EF_CUDA_SM80 EF_CUDA_VIRTUAL_SM(EF_CUDA_SM75)` |
| `--maxrregcount=8` 的 REG | 仍为 `12`（ABI 下限） |
| SASS `LDG` / `IMAD.WIDE` | 各 `2` / `3` |

边界输入：

- `-mcpu=sm_90a` 的 PTX 不能拿到 T4 上跑（T4 是 sm_75）；只比较文本，不执行。
- `--maxrregcount` 低于下限时不会强制压到 8。

## 如果你卡住了

1. **`ptxas` 报 arch 不匹配**：确认 `-arch` 不低于 `.target`；`ptxas` 允许更高。
2. **SASS 里有 `BRA` 到自身**：那是函数尾部的填充，不影响计数。
3. **REG 一直是 12**：这个小 kernel 的 ABI 下限就是 12，换大 kernel 才能看到变化。
4. **sm_90a 的 PTX 编不过**：检查 `ptxas` 版本是否支持 8.0；CUDA 12.4 支持。
5. **headerflags 看不到**：用 `cuobjdump -sass` 的前几行，不是 `-res-usage`。

## 实作步骤建议

### Step 1：生成三份 PTX
只改 `-mcpu`，其余参数一致。

### Step 2：记录头部
把 `.version`/`.target` 抄表。

### Step 3：编 sm_75 并记录资源
`ptxas -v` + `cuobjdump -res-usage` 交叉验证。

### Step 4：跨 arch 编译
`ptxas -arch=sm_80` 编 sm_75 的 PTX，记录 `VIRTUAL_SM`。

### Step 5：SASS 计数
`cuobjdump -sass add.cubin | grep -c "LDG"`，再数 `IMAD.WIDE`。

### Step 6：回归结论
写三句话：哪项随 arch 变、哪项随 ptxas 选项变、哪项不变。

## 完整参考解答

**写完再看！**

<details>
<summary>点开参考结论</summary>

```bash
BIN=/home/ubuntu/llvm/builds/ct-instr-trunk/bin
for cpu in sm_75 sm_80 sm_90a; do
  $BIN/llc -mtriple=nvptx64 -mcpu=$cpu -O2 add.ll -o add-$cpu.ptx
  head -4 add-$cpu.ptx | tail -2
done
```

```
.version 6.3 / .target sm_75
.version 7.0 / .target sm_80
.version 8.0 / .target sm_90a
```

```bash
ptxas -arch=sm_75 add-sm_75.ptx -o a75.cubin -v
# Used 12 registers, 0 bytes spill
ptxas -arch=sm_80 add-sm_75.ptx -o a80.cubin
cuobjdump -sass a80.cubin | head -4
# code for sm_80
# .headerflags "... EF_CUDA_SM80 EF_CUDA_VIRTUAL_SM(EF_CUDA_SM75)"
```

结论：

1. `.version` 随架构变（最低 PTX 版本），`.target` 随 `-mcpu` 变。
2. `ptxas -arch` 只改物理目标，不改 PTX 的虚拟目标（`VIRTUAL_SM` 会保留）。
3. 小 kernel 的 REG 由 ABI 下限决定，`--maxrregcount=8` 压不下去。
4. SASS 的 `LDG` 数量对应 PTX 的两个 `ld.global`；`IMAD.WIDE` 是三条地址计算的融合结果。

</details>

## 卡点提示

1. 先确认 `ptxas --version` 支持你选的 arch。
2. 不要把 `BRA 0xf0` 这类填充算进指令数。
3. `-v` 的输出在 stderr，脚本里要 `2>&1`。
4. 资源统计以 cubin 为准，不要用 PTX 的 `.reg`。

## 加分挑战

1. 把 kernel 改成 shared memory 版本，观察 `SHARED:128` 与 REG 的变化。
2. 用 `-O0` 生成 PTX 再 ptxas，比较 REG 与 SASS 长度。
3. 用 `nvcc -Xptxas -v` 编同一 kernel，比较与 llc 路径的 REG 差异。
4. 写一个脚本，自动对 3 个 arch × 2 个 `-maxrregcount` 做矩阵并输出 CSV。

→ [Ch 15 AsmPrinter 与 MC：PTX 文字是怎么印出来的](./15-asmprinter-and-mc.md)
