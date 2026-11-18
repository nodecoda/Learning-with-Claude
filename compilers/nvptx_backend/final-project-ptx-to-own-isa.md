# Final Project — PTX → 自研 ISA 的最小闭环

> **目标**：把本课 70% 的概念拼起来：解析 `llc` 生成的 PTX、翻译到一个玩具 SIMT ISA、用模拟器执行，并与真实 T4 的结果逐位对拍。

## 背景与动机

自研 GPU 做 CUDA 兼容，最现实的技术路线是「吃 PTX」。这个项目就是那条路线的最小可运行版本：不做优化、不做完整 ISA，只做**语义闭环**——能跑对，能和 NVIDIA 硬件对拍。

完成后你会有：

- 一份可扩展的 PTX 子集前端；
- 一个能执行 warp 语义的模拟器；
- 一套差分测试方法（这是兼容性工作的核心方法）。

## 任务规格

### 输入 PTX 子集

只要求支持以下指令（其余遇到就报 `unsupported`，不要静默跳过）：

| 类别 | 指令 |
|---|---|
| 声明 | `.version` `.target` `.address_size` `.entry` `.param` `.reg` |
| 移动 | `mov.u32` `mov.b32` `mov.b64` `cvta.to.global` `cvta.local` |
| 参数 | `ld.param.b32` `ld.param.b64` |
| 访存 | `ld.global.b32` `ld.global.b64` `st.global.b32` `st.global.b64` |
| shared | `ld.shared.b32` `st.shared.b32` `bar.sync` |
| 算术 | `add.s32` `add.u32` `sub.s32` `mul.lo.s32` `mad.lo.s32` `mul.wide.s32` |
| 浮点 | `add.rn.f32` `mul.rn.f32` `fma.rn.f32` |
| 比较/控制 | `setp.ge.s32` `setp.lt.s32` `selp.b32` `@%p bra` `bra` `bra.uni` `ret` |

### 目标 ISA（玩具）

- 16 个 32 位通用寄存器 + 16 个 64 位地址寄存器（或统一 64 位，自己定）。
- 每线程独立 PC、谓词、寄存器；以 32 线程为一个 warp 执行。
- shared memory：每 block 64KB 字节数组，按 word 寻址。
- 指令：`LDI` `MOV` `ADD` `SUB` `MUL` `MAD` `LD` `ST` `SETP` `SELP` `BRA` `BARP` `EXIT`。
- 分支用「活跃掩码」实现 divergence：`@p bra` 只改变掩码内线程的 PC。

### 验收标准

1. `vadd`：1024 个元素，结果与 T4 逐位一致。
2. `tile`（shared memory + barrier）：32 个元素反转，结果与 T4 一致。
3. 不支持指令必须报错并指出行号。
4. 模拟器输出执行统计：指令数、分支数、shared 访问数。

## 期望输出范例

T4 的 golden 结果（本机实测，`vadd`）：

```
c[0]=0 c[1]=3 c[511]=1533 c[1023]=3069
mismatches=0/1024
```

你的模拟器应输出同样的值；`tile` 的期望是 `out[i] = in[31-i]`。

边界输入：

- `n` 不是 32 的倍数时，最后一批线程要被谓词屏蔽；
- `bar.sync` 出现在分歧路径里时，应报错（真实硬件行为是未定义/死锁）。

## 如果你卡住了

1. **解析不了 `%r<9>`**：先展开声明，建立「虚拟寄存器名 → 索引」表，再做指令解析。
2. **`cvta` 不知道怎么做**：模拟器里可以把它当恒等映射（地址空间用高位标记区分）。
3. **warp 语义写错**：先实现「单线程顺序执行」跑通，再加 32 lane 掩码。
4. **shared 对不上**：确认 shared 是 per-block，不是 per-thread。
5. **浮点对不上**：先只做整数；浮点用 Python float（f32 需要 `struct` 截断）。

## 实作步骤建议

### Step 1：解析器
按行解析，先处理声明与标签，再处理指令。输出一个 `Function{params, regs, blocks}`。

### Step 2：IR 化
把每条 PTX 指令变成 `(opcode, dst, srcs, pred)` 元组；建基本块与 CFG。

### Step 3：翻译
PTX 虚拟寄存器 → 玩具 ISA 寄存器；超出 16 个时先 spill 到内存（正好练 Ch 11 的「分配」）。

### Step 4：单线程模拟器
实现指令语义，跑 `vadd`，与 T4 对拍。

### Step 5：warp 化
32 lane + 活跃掩码；实现 `bar.sync` 的 block 级同步。

### Step 6：差分测试
用 driver API 跑 T4 的 cubin，读回结果；与模拟器逐位比较，输出 diff。

### Step 7：报告
写 `REPORT.md`：支持的子集、已知差异、每条指令的语义来源（PTX ISA 章节）。

## 参考骨架

```python
# ptxparse.py（示意）
import re
RE_REG = re.compile(r'^\s*\.reg\s+\.(\w+)\s+%(\w+)<(\d+)>;')
RE_PARAM = re.compile(r'^\s*\.param\s+\.(\w+)(?:\s+\.ptr)?(?:\s+\.align\s+(\d+))?\s+(\w+),?')
RE_INST = re.compile(r'^\s*(?:@%(\w+)\s+)?([\w.]+)\s*(.*);')

def parse(text):
    funcs, cur = [], None
    for line in text.splitlines():
        line = line.split('//')[0].rstrip()
        if not line.strip():
            continue
        if m := RE_REG.match(line):
            cur.regs[m.group(2)] = (m.group(1), int(m.group(3)))
        elif m := RE_INST.match(line):
            cur.insts.append((m.group(1), m.group(2), m.group(3)))
    return funcs
```

```python
# sim.py（示意）
def run_warp(insts, lanes=32):
    active = set(range(lanes))
    for pc, (pred, op, args) in enumerate(insts):
        if pred and not pred_true(pred):
            continue
        exec_op(op, args, active)
```

## 评分标准

| 项目 | 权重 | 要求 |
|---|---|---|
| 解析器 | 20% | 覆盖子集，错误行号准确 |
| 翻译正确性 | 25% | 寄存器/地址/宽度正确 |
| 模拟器语义 | 25% | warp、barrier、divergence 正确 |
| 差分测试 | 20% | 与 T4 逐位对拍，报告差异 |
| 可扩展性 | 10% | 加一条指令只需改三处 |

## 进阶挑战

1. 支持 `ld.global.v4.b32`，验证向量访存。
2. 加一个简单的寄存器分配器（线性扫描 + spill），对比 `ptxas` 的 REG。
3. 支持 `atom.add.u32`，实现跨线程原子。
4. 把 `bar.sync` 换成 `barrier.cta.sync` 语义，支持多 barrier。
5. 用 C++ 重写模拟器，跑 1M 元素的 benchmark。

## 本章重点整理

- PTX 兼容层的最小闭环 = 解析 + 语义建模 + 差分验证。
- warp 语义（掩码、barrier）是 GPU 与 CPU 模拟器的分水岭。
- 差分测试是兼容性工作的核心方法：T4 是最好的 golden model。

→ [回到课程首页](./README.md)
