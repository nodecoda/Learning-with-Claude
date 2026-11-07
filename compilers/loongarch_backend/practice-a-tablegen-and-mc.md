# 练习 A — 加一条指令、一个别名，并跑通往返

> 目标：亲手改一次 `LoongArchInstrInfo.td`，看它如何改变 **opcode 枚举 / 汇编匹配表 / 打印表**，然后重编 `llvm-mc` 与 `llc`，用汇编→编码→反汇编验证。做完你应该能独立处理“新增一条 MC 层指令/别名”的补丁。

**重要**：本练习改的是**你自己的临时副本或临时分支**。不要在当前 `fix/issue-239812-relax-marker-order` 分支上留下无关改动。

---

## A.1 选一个安全的实验对象

不要一上来就加真 ISA 指令——编码冲突、feature 归属、调度模型都会让你卡住。用两个对象：

| 对象 | 类型 | 目的 |
|---|---|---|
| `add.du` | 新 `def`（玩具指令） | 验证 opcode 生成、编码路径、反汇编 |
| `neg.d` | `InstAlias` | 验证汇编匹配表、打印别名、测试写法 |

`add.du` 不是 LoongArch 真实指令，只用于练习；`neg.d` 是很多 RISC 都有的别名（`sub.d rd, $zero, rj`）。

---

## A.2 改动本身

在 `llvm/lib/Target/LoongArch/LoongArchInstrInfo.td` 末尾追加：

```tablegen
def ADD_DU : ALU_3R<0x00108001>,
             Sched<[WriteIALU, ReadIALU, ReadIALU]>;
def : InstAlias<"neg.d $rd, $rj", (SUB_D GPR:$rd, R0, GPR:$rj)>;
```

三件事同时发生：

1. `ALU_3R<op>` 复用 `Fmt3R` 的位域和 `"$rd, $rj, $rk"` 汇编串；
2. `Sched<[...]>` 给它一个调度模型（不加会在 `-gen-instr-info` 阶段报 `No schedule information`，除非该指令被标成 `Unsupported`/不参与调度）；
3. `InstAlias` 让 `neg.d` 在匹配阶段直接映射到 `SUB_D`，**不产生新 opcode**。

---

## A.3 第一步验证：只跑 `llvm-tblgen`（秒级）

不用重编就能确认 `.td` 是否合法、生成了什么。在本树副本上实测（源码树未被修改）：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
R=/home/ubuntu/llvm/trees/ct-instr-trunk
D=/tmp/la_td_a1                      # 把 LoongArch 目录复制一份
cp -r $R/llvm/lib/Target/LoongArch $D
# ... 在 $D/LoongArchInstrInfo.td 末尾追加 A.2 的两行 ...

$T/bin/llvm-tblgen -I $R/llvm/include -I $D -gen-instr-info   $D/LoongArch.td -o /tmp/pA-instrinfo.inc
$T/bin/llvm-tblgen -I $R/llvm/include -I $D -gen-asm-matcher  $D/LoongArch.td -o /tmp/pA-matcher.inc
$T/bin/llvm-tblgen -I $R/llvm/include -I $D -gen-asm-writer   $D/LoongArch.td -o /tmp/pA-writer.inc
```

实测输出（三处都能看到新东西）：

```text
# pA-instrinfo.inc
472:    ADD_DU                                = 457, // LoongArchInstrInfo.td:2810

# pA-matcher.inc（助记符字符串表）
"…\005add.d\006add.du\005add.w\006add…"
"…\005neg.d\003nop\003nor\002or\003ori…"

# pA-writer.inc（别名打印表）
/* 178 */ "neg.d $\x01, $\x03\0"
```

三个可读的结论：

1. **新指令拿到了 opcode 457**。这个编号是生成的，不是 ISA 定义的；插入一条 `def` 会让它后面的所有 opcode **整体后移**。所以不要在任何脚本里写死 opcode 数字。
2. **`add.du` 进入了 AsmMatcher 的助记符表**，说明 `.td` 的 `AsmString` 已经能被汇编器识别。
3. **`neg.d` 只出现在 asm-writer 的别名表**，没有新 opcode——这正是 `InstAlias` 与 `def` 的区别。

> 这一步是“快速反馈环”。改 `.td` 时先跑 tblgen，比直接 `ninja` 快一个数量级。

---

## A.4 第二步：重编哪些目标

`llvm-mc` 和 `llc` 都静态链接了 TableGen 生成物，所以两者都要重编：

```bash
cd /home/ubuntu/llvm
JOBS=24 ./toolset/build.sh ct-instr-trunk llvm-mc llc llvm-objdump llvm-readobj
```

为什么是这四个：

| 工具 | 依赖的生成物 |
|---|---|
| `llvm-mc` | AsmMatcher、AsmWriter、Disassembler、MCCodeEmitter |
| `llc` | InstrInfo、DAGISel、AsmWriter、SubtargetInfo |
| `llvm-objdump` | Disassembler（反汇编 `-d`） |
| `llvm-readobj` | 只读 ELF 结构，重定位验证用 |

第一次重编会跑 `LoongArchCommonTableGen`，之后才是编译；增量大约几分钟。

---

## A.5 第三步：往返验证

```bash
cd /tmp
cat > pa.s <<'EOF'
add.du  $a0, $a1, $a2
neg.d   $a0, $a1
EOF

$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding pa.s
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -filetype=obj pa.s -o pa.o
$T/bin/llvm-objdump -d pa.o
```

期望（`add.du` 的编码由 `0x00108001` 的位域 + `rd/rj/rk` 覆盖得到；`neg.d` 应打印成 `sub.d`）：

```text
	add.du	$a0, $a1, $a2                   # encoding: [0xa4,0x98,0x10,0x00]
	sub.d	$a0, $zero, $a1                 # encoding: [0x04,0x94,0x11,0x00]
```

> 上面的字节是**手算**的，请以你本机输出为准：
> - `add.du`：把 `0x00108001` 的 `rd/rj/rk` 位段换成 `4/5/6` →
>   `0x00108000 | (6<<10 | 5<<5 | 4) = 0x001098a4`，小端 `a4 98 10 00`。
> - `neg.d` 展开成 `sub.d $a0, $zero, $a1` → `0x00118000 | (5<<10 | 4) = 0x00119404`，小端 `04 94 11 00`。
>
> `neg.d` 是别名，打印时优先走 `printAliasInstr`，所以反汇编可能显示 `neg.d` 也可能显示 `sub.d`——**这正是要观察的点**，测试的 `CHECK` 必须以实测为准。

本节这一步标记为 **未实测**（需要重编整套工具链）；A.3 的 tblgen 输出是实测。

---

## A.6 第四步：写 MC 测试

新增 `llvm/test/MC/LoongArch/alias-neg-d.s`：

```asm
# RUN: llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding %s \
# RUN:   | FileCheck %s

## neg.d is an alias of sub.d $rd, $zero, $rj.
neg.d $a0, $a1
# CHECK: sub.d $a0, $zero, $a1
# CHECK-NEXT: # encoding: [0xa4,0x00,0x15,0x00]
```

要点：

- **`CHECK` 行必须写打印结果，不是输入文本**。别名会被 `printAliasInstr` 改写，写 `neg.d` 大概率失败。
- 用 `-show-encoding` 同时锁住“语义（哪条真指令）+ 编码”两层。
- 如果别名打印确实保留 `neg.d`，那就把 `CHECK` 改成 `neg.d`——**以实测为准，不要猜**。

再给新指令加一条：

```asm
# RUN: llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding %s | FileCheck %s
add.du $a0, $a1, $a2
# CHECK: add.du $a0, $a1, $a2
# CHECK-NEXT: # encoding: [0xa4,0x98,0x10,0x00]
```

---

## A.7 第五步：跑测试

```bash
$T/bin/llvm-lit -sv \
  llvm/test/MC/LoongArch/alias-neg-d.s \
  llvm/test/MC/LoongArch/add-du.s
```

期望 `2 passed`。如果失败，先看 `-v` 打出的实际输出，再决定改 `CHECK` 还是改代码——**永远先读实际输出**。

再跑一遍整个 MC 目录，确认没有把已有测试搞坏：

```bash
$T/bin/llvm-lit -s -j 24 llvm/test/MC/LoongArch
```

本树基线是 **335 passed**（见 Ch 00）。加了两个测试后应是 **337 passed**。

---

## A.8 第六步：清理

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git checkout -- llvm/lib/Target/LoongArch/LoongArchInstrInfo.td
# 删除你新增的两个测试文件
git status --short          # 应该干净
cd /home/ubuntu/llvm && JOBS=24 ./toolset/build.sh ct-instr-trunk llvm-mc llc
```

**必须重编回去**，否则你的构建目录里还留着玩具指令，后面的实验会看到不存在的 `add.du`。

---

## A.9 常见失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| tblgen 报 `No schedule information for …` | 新 `def` 没给 `Sched<>` | 加 `Sched<[WriteIALU, ReadIALU, ReadIALU]>` |
| 汇编报 `unrecognized instruction mnemonic` | 只重编了 `llc`，没重编 `llvm-mc` | 两个都编 |
| 反汇编报 `invalid instruction encoding` | 编码与已有指令冲突 | 换 opcode，或加 `DecoderNamespace`/feature 区分 |
| `CHECK` 失败但输出“看起来对” | 别名被打印成真指令 | 用实测文本改 `CHECK` |
| 新指令在 `-mattr=-lasx` 下仍可用 | 用了 `Predicates` 而不是 `AssemblerPredicate` | MC 层可见性要 `AssemblerPredicate`（Ch 04/17） |
| MIR 测试大面积失败 | 插入 `def` 导致 opcode 重编号 | 正常现象；MIR 用名字不用数字，但快照类测试可能受影响 |

---

## A.10 证据表

| 结论 | 证据 |
|---|---|
| `ALU_3R` 复用 `Fmt3R` | `LoongArchInstrInfo.td:741` |
| `ADD_D` 定义 | `LoongArchInstrInfo.td:1075` |
| 新 opcode = 457 | A.3 的 `-gen-instr-info` 实测 |
| `add.du` 进入匹配表 | A.3 的 `-gen-asm-matcher` 实测 |
| `neg.d` 进入别名打印表 | A.3 的 `-gen-asm-writer` 实测 |
| 重编目标 | A.4（未实测，命令来自本树构建脚本） |
| 测试基线 335 | Ch 00 |

---

## A.11 下一步

Part 1 完成。Part 2 进入 ABI 与链接：先看调用约定 lowering（Ch 09），再看栈帧（Ch 10）、代码模型与 TLS（Ch 11），最后用 `ld.lld` 的 relaxation 收尾（Ch 12）——也就是本树正在修的 #239812 所在的层。
