# 练习 D — 用 MIR 驱动机器层 pass

> 目标：不依赖完整流水线，直接给一个机器 pass 喂 MIR，观察它改了什么。完成后你能独立验证 Ch 18–22 里的任何一条断言。
>
> 预计时间：90 分钟。所有输出都在本树实测过；你的结果应当逐字一致（除了 IR 里的临时名）。

---

## D.0 准备

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
R=/home/ubuntu/llvm/trees/ct-instr-trunk
mkdir -p /tmp/mir-lab && cd /tmp/mir-lab
```

---

## D.1 从 IR 生成一份真实 MIR

```bash
cat > if.ll <<'EOF'
define i32 @sel(i32 %a, i32 %b, i32 %c) {
entry:
  %cmp = icmp eq i32 %a, 0
  br i1 %cmp, label %then, label %else
then:
  ret i32 %b
else:
  ret i32 %c
}
EOF

$T/bin/llc -mtriple=loongarch64 -O2 -stop-after=loongarch-prera-expand-pseudo if.ll -o pre.mir
```

打开 `pre.mir`，找到：

```yaml
  bb.0.entry:
    successors: %bb.2(0x30000000), %bb.1(0x50000000)
    liveins: $r4, $r5, $r6
    %2:gpr = COPY $r6
    %1:gpr = COPY $r5
    %0:gpr = COPY $r4
    %5:gpr = ADDI_W %0:gpr, 0
    BNEZ %5:gpr, %bb.2
    PseudoBR %bb.1
```

**检查点 1**：`PseudoBR` 是未展开的伪指令；`$r4/$r5/$r6` 是 ABI 参数寄存器。

---

## D.2 手写 MIR：先踩四个坑

按顺序运行下面四个错误版本，记录报错。它们都是**真实报错**。

### 坑 1：寄存器名用 `$zero`

```bash
cat > bad1.mir <<'EOF'
--- |
  define void @f(i64 %a) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4
    BEQZ $zero, %bb.1
    PseudoRET

  bb.1:
    PseudoRET
EOF
$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt bad1.mir -o /dev/null
```

```text
error: bad1.mir:10:10: unknown register name 'zero'
```

### 坑 2：指令名用最终汇编

注意要先把 `$zero` 改成 `$r0`，否则 parser 会在第一处错误就停下：

```bash
cat > bad2.mir <<'EOF'
--- |
  define void @f(i64 %a) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4
    BEQZ $r0, %bb.1
    RET

  bb.1:
    RET
EOF
$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt bad2.mir -o /dev/null
```

```text
error: bad2.mir:11:5: unknown machine instruction name 'RET'
```

### 坑 3：小写汇编名

```bash
sed 's/RET/ret/g' bad2.mir > bad3.mir
$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt bad3.mir -o /dev/null
```

```text
error: bad3.mir:11:5: unknown machine instruction name 'ret'
```

### 坑 4：删掉 `name:`

```bash
sed '/^name:/d' bad2.mir > bad4.mir
$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt bad4.mir -o /dev/null
```

```text
error: YAML:5:1: missing required key 'name'
```

### 一个反直觉的“不是坑”

IR 段其实**可以省略**：把 `--- |` 到 `...` 四行删掉，`-run-pass` 仍然能跑（parser 会合成空 IR，ModuleID 变成 `.mir` 文件名）。但保留 IR 段能让 MIR 里的 `%ir.a`、`(load (s64))` 注释更可读，所以 `-stop-after` 生成的 MIR 建议原样保留。

**结论**：MIR 里用 `$r0`、`PseudoRET`、`PseudoBR`；`name:` 必填。

---

## D.3 `loongarch-late-branch-opt`：什么会折，什么不会

### 会折：静态可知的真比较

```bash
cat > lb_true.mir <<'EOF'
--- |
  define void @f(i64 %a) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4
    BEQ $r4, $r4, %bb.2
    PseudoBR %bb.1

  bb.1:
    PseudoRET

  bb.2:
    PseudoRET
EOF

$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt lb_true.mir -o -
```

输出：

```yaml
  bb.0:
    successors: %bb.2(0x80000000)
    liveins: $r4
    PseudoBR %bb.2
```

`BEQZ $r0, %bb.2` 也能折（把 `BEQ $r4, $r4` 换成 `BEQZ $r0`）。

### 不会折：普通的 `x == 0`

```bash
sed 's/BEQ \$r4, \$r4/BEQ $r4, $r0/' lb_true.mir > lb_zero.mir
$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-late-branch-opt lb_zero.mir -o -
```

输出保持：

```yaml
    BEQ $r4, $r0, %bb.2
    PseudoBR %bb.1
```

**为什么**：`LoongArchLateBranchOpt.cpp:63-78` 对 `BEQ/BNE` 只接受“同一个寄存器跟自己比”（`Cond[1].getReg() == Cond[2].getReg()`）；对 `BEQZ/BNEZ` 只接受“跟 `$r0` 比”。普通 `x == 0` 在更早的 MachineCombiner 已经变成 `BEQZ`。

**检查点 2**：用 `-stop-after=loongarch-opt-w-instrs` 看原始 IR 的 `icmp eq` 是否已经被换掉。

---

## D.4 `loongarch-dead-defs`：名字骗人，它只管 load/store

### 计算指令不会被改写

```bash
cat > dd_add.mir <<'EOF'
--- |
  define void @f(i64 %a) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4, $r5
    %0:gpr = ADD_W $r4, $r5
    PseudoRET
EOF

$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-dead-defs dd_add.mir -o -
```

输出：

```yaml
    dead %0:gpr = ADD_W $r4, $r5
```

只多了 `dead` 标记，**没有改成 `$r0`**。源码 `LoongArchDeadRegisterDefinitions.cpp:70-75`：

```cpp
// We only handle non-computational instructions.
const MCInstrDesc &Desc = MI.getDesc();
if (!Desc.mayLoad() && !Desc.mayStore() && !Desc.hasUnmodeledSideEffects())
  continue;
```

### 访存指令会被改写

```bash
cat > dd_load.mir <<'EOF'
--- |
  define void @f(ptr %p) { ret void }
...
---
name:            f
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $r4
    %0:gpr = LD_D $r4, 0 :: (load (s64))
    PseudoRET
EOF

$T/bin/llc -mtriple=loongarch64 -run-pass=loongarch-dead-defs dd_load.mir -o -
```

输出：

```yaml
    dead $r0 = LD_D $r4, 0 :: (load (s64))
```

**检查点 3**：解释为什么把目的寄存器改成 `$r0` 仍然保留这条 load（提示：异常语义/内存副作用）。

---

## D.5 `loongarch-merge-base-offset`：把 ADDI 折进访存

**必须同时满足**：非 PIC（`-relocation-model=static`）+ 符号是 `dso_local`。

```bash
cat > mb.ll <<'EOF'
@g = dso_local global i64 42
define i64 @h() {
  %v = load i64, ptr @g
  ret i64 %v
}
EOF
```

折叠前：

```bash
$T/bin/llc -mtriple=loongarch64 -O2 -relocation-model=static \
  -stop-after=loongarch-prera-expand-pseudo mb.ll -o -
```

```yaml
%2:gpr = PCALAU12I target-flags(loongarch-pcrel-hi) @g
%0:gpr = ADDI_D %2, target-flags(loongarch-pcrel-lo) @g
%1:gpr = LD_D %0, 0 :: (dereferenceable load (s64) from @g)
```

折叠后：

```bash
$T/bin/llc -mtriple=loongarch64 -O2 -relocation-model=static \
  -stop-after=loongarch-merge-base-offset mb.ll -o -
```

```yaml
%2:gpr = PCALAU12I target-flags(loongarch-pcrel-hi) @g
%1:gpr = LD_D %2, target-flags(loongarch-pcrel-lo) @g :: (dereferenceable load (s64) from @g)
```

`ADDI_D` 消失，`%pc_lo12` 直接落在 `LD_D` 的立即数位上。

**检查点 4**：把 `dso_local` 去掉，或去掉 `-relocation-model=static`，确认 pass 不再触发（会走 GOT）。

---

## D.6 观察工具

```bash
# 某个 pass 之后长什么样（不中断编译）
$T/bin/llc -mtriple=loongarch64 -O2 -print-after=loongarch-opt-w-instrs if.ll -o /dev/null

# 统计
$T/bin/llc -mtriple=loongarch64 -O2 -stats -o /dev/null if.ll 2>&1 | grep -E 'isel|dagcombine'

# SelectionDAG 细节
$T/bin/llc -mtriple=loongarch64 -O2 -debug-only=isel -o /dev/null if.ll 2>&1 | head -20
```

实测 `-stats` 参考值（`bittest.ll`，978 个测试之一）：

```text
380 dagcombine      - Number of dag nodes combined
226 isel            - Number of blocks selected using DAG
713 isel            - Number of times dag isel has to try another path
120 isel            - Number of entry blocks encountered
```

---

## D.7 验收标准

完成下面全部，才算做完：

- [ ] 能不看笔记写出一个合法 MIR（含 IR 段、`$r0`、`PseudoRET`）
- [ ] 能解释 `BEQ $r4,$r4` 会被折、`BEQ $r4,$r0` 不会
- [ ] 能解释 `dead-defs` 为什么跳过 `ADD_W`
- [ ] 能复现 `merge-base-offset` 的折叠，并说明 `dso_local` 的作用
- [ ] 能用 `-print-after` 与 `-stats` 给出“某 pass 改了/没改”的证据

---

## D.8 常见失败

| 现象 | 原因 |
|---|---|
| `unknown pass name` | pass 名写错；用 `-debug-pass=Structure` 查 |
| `missing required key 'name'` | MIR 段被删掉了 `name:` |
| `unknown register name 'zero'` | 用了 `$zero`，要写 `$r0` |
| `merge-base-offset` 不触发 | 用了默认 PIC 或符号不是 `dso_local` |
| `dead-defs` 不触发 | 目标是计算指令（pass 只处理 load/store/side-effect） |

---

## D.9 延伸

把 D.5 的 `mb.ll` 改成 `%v = load i64, ptr @g, align 8` 之外的形状（例如 `getelementptr` + `load`），观察 `ADDI_D` 是否还能被折进访存；再试 `-mcmodel=large`（Ch 11），记录序列从 2 条变成 5 条时 pass 的行为。
