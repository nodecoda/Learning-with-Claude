# Ch 00 — 树审计：`ct-instr-trunk`、`analysis/riscv-maintenance` 与这份构建

> **目标**：把这棵树的身份、库存、分支、构建状态、上游位置和验证证据一次讲清楚。读完你应该能回答：这门课到底在读哪份代码、为什么必须自己重配构建、以及当前哪些测试是真正跑过的。

> **环境**：LLVM `24.0.0git`，`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`（2026-11-08）；构建 `/home/ubuntu/llvm/builds/ct-instr-trunk`，`RelWithDebInfo` + assertions ON + `BUILD_SHARED_LIBS=ON`，targets `X86;AArch64;RISCV`，projects `clang;lld`。

本章是“树审计”，不是“RISC-V 入门”。ISA、扩展和文件地图从 Ch 01–02 开始。但如果你跳过这一章，后面每一次“实测输出”你都只能选择相信，而不能复现。

---

## 0.1 为什么维护者先做树审计

读 LLVM 的 RISC-V 代码，最常见的三种“看起来学会了、实际上没学会”：

1. **读的是 release 分支，改的是 trunk。** 网上大量教程基于 LLVM 17/18/23，`RISCVISelLowering.cpp` 的行为、pass 顺序、甚至文件是否存在都不同。拿旧行号去 trunk 里 grep，会得到“这函数不存在”的假结论。
2. **代码看对了，构建是旧的。** `llc` 能跑，但跑的是三周前的 `.so`；`llvm-lit` 全绿，但 RISC-V 测试全部被标记 `UNSUPPORTED`，根本没执行。
3. **测试全绿，但测的不是你改的那棵树。** LLVM 的 lit 配置在 **configure 时**把目标列表写进 `lit.site.cfg.py`。换 worktree、换分支、换 target 之后不重配，测试会安静地跳过。

树审计就是提前把这三种坑排掉。它不是形式主义：本章最后给出的 3 组 lit 结果，每组都带命令、耗时和通过数，后续章节引用同一套基线。

---

## 0.2 先建立直觉：一棵树、一个分支、一份构建

```
/home/ubuntu/llvm
├── llvm-project/                 hub：唯一完整历史 clone，永远 main，不构建
├── trees/
│   └── ct-instr-trunk/           ← 本课唯一源码入口（worktree）
│       ├── .git                  （指向 hub 的 worktree 元数据）
│       ├── llvm/                 LLVM 本体
│       ├── clang/                Clang 前端与 driver
│       └── lld/                  链接器
└── builds/
    └── ct-instr-trunk/           ← 与 worktree 一一对应的 out-of-tree 构建
        ├── CMakeCache.txt        目标列表、项目列表、编译器都在这里
        ├── bin/llc               RISC-V 代码生成的入口
        ├── bin/llvm-mc           汇编/编码/反汇编
        ├── bin/clang             driver + 前端
        ├── bin/ld.lld            RISC-V 链接
        └── bin/llvm-lit          测试驱动
```

三条硬规则（来自 `/home/ubuntu/admin/AGENTS.md`）：

- **hub 只 fetch/rebase/log，不改代码、不构建。**
- **源码改动只在 `trees/<task>`；构建只在 `builds/<task>`。**
- **默认只操作本任务自己的 worktree 与构建。** 本课只读 `ct-instr-trunk`。

> 本课的 `analysis/riscv-maintenance` 是从 `upstream/main` 开出的**分析分支**，不是要合入的上游分支。原来的 `fix/issue-239812-relax-marker-order` 仍在本地，切回去只需 `git switch`。

---

## 0.3 这棵树的身份

先跑四条命令，不要凭记忆：

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb
git log --oneline -3
git rev-parse --short HEAD
git rev-list --left-right --count upstream/main...HEAD
```

实测（2026-11-08）：

```text
## analysis/riscv-maintenance...upstream/main
?? llvm/test/CodeGen/LoongArch/bittest.s
0fc5afa04c23 [NVPTX] Select scalar_to_vector for bf16x2 (#236706)
319bd32d5276 [BFI] Scale block frequencies with native 128-bit integers (NFC) (#239953)
0fc5afa04c23
0	0
```

几个容易误读的点：

| 项目 | 值 | 说明 |
|---|---|---|
| 路径 | `/home/ubuntu/llvm/trees/ct-instr-trunk` | git worktree，不是独立 clone |
| 分支 | `analysis/riscv-maintenance` | 从 `upstream/main` 开出，跟踪 `upstream/main` |
| HEAD | `0fc5afa04c23` | 2026-11-08 的上游尖端 |
| 相对 `upstream/main` | 领先 0、落后 0 | 刚 fetch 并直接以它为基础 |
| `origin` | `https://github.com/nodecoda/llvm-project.git` | 个人 fork，推送用 |
| `upstream` | `https://gh-proxy.com/https://github.com/llvm/llvm-project.git` | 只读代理（`no_push`） |

`?? llvm/test/CodeGen/LoongArch/bittest.s` 是前一个任务留下的未跟踪文件。它不影响 RISC-V，也不应该被提交；本课不碰它。

### 为什么从 `upstream/main` 开，而不是从旧分支开

旧分支 `fix/issue-239812-relax-marker-order` 比 `upstream/main` 多一条 LoongArch 提交。如果基于它开分析分支：

- 你会把一条与本课无关的 lld 修复带进每个 `git diff`；
- 行号、pass 顺序、测试基线都会偏离真正的上游；
- 将来想对照 `git log upstream/main` 时会不断被这条提交干扰。

分析分支的唯一职责是“当前上游 RISC-V 代码 + 本课的实验记录”。所以从 `upstream/main` 开，保持 0 领先 0 落后。

---

## 0.4 代码库存：RISC-V 在这个 trunk 里到底有多大

先给规模，避免“以为 RISC-V 只是个小 target”。

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
find llvm/lib/Target/RISCV -type f \( -name '*.cpp' -o -name '*.h' -o -name '*.td' \) | wc -l
find llvm/lib/Target/RISCV -type f \( -name '*.cpp' -o -name '*.h' -o -name '*.td' \) -print0 \
  | xargs -0 cat | wc -l
```

实测：

```text
210
157290
```

也就是说：**210 个源文件、157,290 行**。这比很多人想象的大。它有明确的分层：

| 层 | 目录 | 规模（行） | 代表文件 |
|---|---|---:|---|
| 目标机与 SelectionDAG | `RISCV/` | ~137,900 | `RISCVISelLowering.cpp` 31,346；`RISCVInstrInfo.cpp` 5,629；`RISCVISelDAGToDAG.cpp` 5,295 |
| TableGen 描述 | `RISCV/*.td` | 合计约 5 万 | `RISCVInstrInfoVPseudos.td` 7,392；`RISCVInstrInfoP.td` 3,859；`RISCVFeatures.td` 2,407 |
| MC 描述 | `RISCV/MCTargetDesc/` | 6,880 | `RISCVBaseInfo.h` 1,059；`RISCVAsmBackend.cpp` 1,032；`RISCVMCCodeEmitter.cpp` 1,003 |
| 汇编器 | `RISCV/AsmParser/` | 4,720 | `RISCVAsmParser.cpp` |
| 反汇编器 | `RISCV/Disassembler/` | 770 | `RISCVDisassembler.cpp` |
| GlobalISel | `RISCV/GISel/` | 6,529 | `RISCVInstructionSelector.cpp` 2,376；`RISCVLegalizerInfo.cpp` 1,925 |
| MCA | `RISCV/MCA/` | 437 | `RISCVCustomBehaviour.cpp` 361 |

### 测试库存

```bash
for d in llvm/test/CodeGen/RISCV llvm/test/MC/RISCV clang/test/CodeGen/RISCV \
         llvm/test/tools/llvm-mca/RISCV lld/test/ELF; do
  printf '%-38s %s\n' "$d" "$(find $d -type f | wc -l)"
done
```

实测（`lld/test/ELF` 只统计 `riscv*` 相关文件时为 55）：

| 测试根 | 文件数 | 主要内容 |
|---|---:|---|
| `llvm/test/CodeGen/RISCV` | 2,742 | `.ll` 代码生成、`.mir` 机器 pass；其中 `rvv/` 1,267、`GlobalISel/` 500 |
| `llvm/test/MC/RISCV` | 622 | 汇编、编码、反汇编、重定位；`rvv/` 96 |
| `clang/test/CodeGen/RISCV` | 2,824 | ABI、builtins、attributes、vector bits |
| `clang/test/Driver/riscv*` | 22 | `-march`/`-mabi`/`-mcpu`/`-mtune` 解析 |
| `lld/test/ELF/riscv*` | 55 | 重定位、`e_flags`、relaxation |
| `llvm/test/tools/llvm-mca/RISCV` | 386 | 各 CPU 调度模型 |

这个比例很重要：**源码 15.7 万行，RISC-V 相关测试文件超过 6,600 个**。维护工作的一大半不是“写代码”，而是“找到该改哪一条测试、怎么让它仍然通过”。

### 活跃度：哪里在动

```bash
git log --since='12 months ago' --oneline -- llvm/lib/Target/RISCV | wc -l
git log --since='6 months ago'  --oneline -- llvm/lib/Target/RISCV | wc -l
```

实测：**12 个月 1,545 条，6 个月 820 条**。文件热度（按提交触碰次数，12 个月）：

| 次数 | 文件 |
|---:|---|
| 498 | `RISCVISelLowering.cpp` |
| 184 | `RISCVInstrInfoP.td` |
| 92 | `RISCVInstrInfo.cpp` |
| 89 | `RISCVTargetTransformInfo.cpp` |
| 80 | `RISCVFeatures.td` |
| 71 | `RISCVISelDAGToDAG.cpp` |
| 68 | `RISCVTargetMachine.cpp` |
| 65 | `RISCVInstrInfo.td` |

读法建议：

- **只想修一个具体 bug**：先看 Ch 31 的贡献地图，再回到对应热点文件。
- **想系统读后端**：按 Ch 01 的十二个站位走，不要从 31,346 行的 `RISCVISelLowering.cpp` 第一行开始硬啃。
- **想加扩展**：`RISCVFeatures.td` + `RISCVInstrInfo*.td` + `RISCVInstrPredicates.td` 是主线，P-ext 是这个 trunk 最活跃的样本。

---

## 0.5 构建：从“没有 RISCV”到“真的能跑”

这是本章最重要的一节。因为它是一个**会静默失败**的坑。

### 0.5.1 第一次检查：`llc` 根本不认识 RISC-V

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc --version | grep -i riscv
grep -E '^LLVM_TARGETS_TO_BUILD:' $T/CMakeCache.txt
```

重配前的实际输出是：

```text
LLVM_TARGETS_TO_BUILD:STRING=X86;AArch64;LoongArch
```

`llc --version | grep -i riscv` **什么都没有**。这不是“RISC-V 代码坏了”，而是这份构建目录被上一个 LoongArch 任务重配过，RISC-V target 根本不在图里。

更隐蔽的是测试：

```python
# llvm/test/CodeGen/RISCV/lit.local.cfg
if not "RISCV" in config.root.targets:
    config.unsupported = True
```

`config.root.targets` 来自 **configure 时生成的** `build/test/lit.site.cfg.py`：

```python
config.targets_to_build = " X86 AArch64 LoongArch"
```

所以：

- `llvm-lit` 不会报错，它会把 2,742 个 CodeGen 测试全部标成 `UNSUPPORTED`；
- `llvm-lit -Dtargets=RISCV` 也救不了，因为 `targets_to_build` 不读 `lit_config.params`；
- 唯一的修法是**重新 configure**。

### 0.5.2 重配命令

用 toolset，不要手敲 cmake 参数：

```bash
export LLVM_HOME=/home/ubuntu/llvm
LLVM_TARGETS_TO_BUILD='X86;AArch64;RISCV' \
LLVM_ENABLE_PROJECTS='clang;lld' \
"$LLVM_HOME"/toolset/configure.sh ct-instr-trunk
```

关键输出：

```text
-- Targeting X86
-- Targeting AArch64
-- Targeting RISCV
-- Clang version: 24.0.0git
-- Configuring done (8.1s)
-- Generating done (8.7s)
```

确认：

```bash
grep -E '^LLVM_TARGETS_TO_BUILD:|^LLVM_ENABLE_PROJECTS:|^LLVM_SOURCE_DIR:' \
  "$LLVM_HOME/builds/ct-instr-trunk/CMakeCache.txt"
```

```text
LLVM_SOURCE_DIR:STATIC=/home/ubuntu/llvm/trees/ct-instr-trunk/llvm
LLVM_TARGETS_TO_BUILD:STRING=X86;AArch64;RISCV
LLVM_ENABLE_PROJECTS:STRING=clang;lld
```

### 0.5.3 为什么必须要 `lld`

本课 Ch 12、练习 B、练习 E 都要看 `.rela.text`、`e_flags` 和 relaxation。重配前 `builds/ct-instr-trunk/bin/lld` 是**上一个 LoongArch 构建残留的旧二进制**：

```text
LLD 24.0.0 (... 01df7c365d7c5420a2dd66ab941e3a0afb7293c9)
```

而当前源码 HEAD 是 `0fc5afa04c23`。版本字符串里那串 commit 就是证据：**工具和源码不是同一棵树**。这种“能跑但跑的是旧代码”的情况，比“跑不起来”更危险。

重新构建后：

```text
LLD 24.0.0 (https://gh-proxy.com/https://github.com/llvm/llvm-project.git 0fc5afa04c23cbf6a606b7aea7b47d54dc2784de)
```

### 0.5.4 构建规模

| 步骤 | 命令 | 边数 | 时间（本机 24 并发） |
|---|---|---:|---:|
| 主构建 | `ninja -j24` | 6,266 | 约 29 分钟（11:28–11:56） |
| lld | `ninja -j24 bin/lld` | 2,358 | 约 2 分钟 |

构建目录约 39G；根分区剩余约 28G。ccache 生效时，单文件改动后的增量构建通常是秒级到分钟级。

> 注意：`ninja -j24 bin/lld` 触发 2,358 条边，是因为把 `lld` 加入 `LLVM_ENABLE_PROJECTS` 会重新生成构建图，并连带重建部分共享库；这不是 lld 本身有 2,358 个文件。

---

## 0.6 验证：不只“编过了”

### 0.6.1 目标与工具

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc --version | grep -i riscv
$T/bin/llvm-config --targets-built
```

实测：

```text
    riscv32    - 32-bit RISC-V
    riscv32be  - 32-bit big endian RISC-V
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
X86 AArch64 RISCV
```

### 0.6.2 最小代码生成

```llvm
define i64 @add(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}

define signext i32 @mul_add(i32 %a, i32 %b, i32 %c) {
  %m = mul i32 %a, %b
  %r = add i32 %m, %c
  ret i32 %r
}
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/riscv-smoke.ll -o -
```

实测（节选）：

```asm
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
add:
	add	a0, a0, a1
	ret
mul_add:
	mul	a0, a0, a1
	addw	a0, a0, a2
	ret
```

两个细节值得注意：

- `+m` 直接体现在 `.attribute 5, "rv64i2p1_m2p0_zmmul1p0"` 里；
- `mul_add` 的 `add` 变成了 `addw`，因为 RV64 上 i32 结果需要 W 后缀做符号扩展。这就是 Ch 13 合法化与 Ch 19 `OptWInstrs` 的入口。

### 0.6.3 汇编与编码

```bash
printf 'addi a0, a0, 1\nadd a1, a2, a3\nmul a0, a1, a2\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+m -show-encoding
```

实测：

```text
	addi	a0, a0, 1                       # encoding: [0x13,0x05,0x15,0x00]
	add	a1, a2, a3                      # encoding: [0xb3,0x05,0xd6,0x00]
	mul	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x02]
```

### 0.6.4 ELF flags

```bash
printf 'addi a0, a0, 1\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+c -filetype=obj -o /tmp/smoke.o -
$T/bin/llvm-readobj --file-headers /tmp/smoke.o | grep -i -A3 Flags
```

实测：

```text
  Flags [ (0x1)
    EF_RISCV_RVC (0x1)
```

`+c` 不是“允许压缩”这么简单，它会写进 ELF header。Ch 22 会完整拆 `e_flags`。

### 0.6.5 Clang 端到端

```bash
printf 'int f(int a, int b) { return a * b + 1; }\n' > /tmp/t.c
$T/bin/clang -target riscv64-unknown-linux-gnu -march=rv64gc -mabi=lp64d -O2 -S /tmp/t.c -o -
```

实测（节选）：

```asm
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
f:
	mul	a0, a1, a0
	addiw	a0, a0, 1
	ret
```

注意 `-march=rv64gc` 被规范化成了很长的 `.attribute` 串，并且 `g` 被展开成 `imafd_zicsr_zifencei` 再加 `zca/zcd`。这就是 Ch 02 和 Ch 24 的主题。

### 0.6.6 lit 基线

三组命令，全部带耗时：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk

$T/bin/llvm-lit -s -j24 llvm/test/CodeGen/RISCV llvm/test/MC/RISCV
$T/bin/llvm-lit -s -j24 clang/test/Driver/riscv-arch.c clang/test/Driver/riscv-abi.c \
  clang/test/Driver/riscv-cpus.c clang/test/Driver/riscv-features.c \
  llvm/test/tools/llvm-mca/RISCV
$T/bin/llvm-lit -s -j24 clang/test/CodeGen/RISCV \
  lld/test/ELF/riscv-flags.s lld/test/ELF/riscv-pcrel-hilo.s lld/test/ELF/riscv-branch.s
```

实测（2026-11-08）：

| 测试集 | 发现数 | 通过 | 失败 | 耗时 |
|---|---:|---:|---:|---:|
| LLVM CodeGen/RISCV + MC/RISCV | 3,362 | 3,362 | 0 | 25.2–25.9s（两次） |
| Clang Driver RISC-V + llvm-mca/RISCV | 361 | 361 | 0 | 10.02s |
| Clang CodeGen/RISCV + 3 个 LLD RISC-V | 2,827 | 2,827 | 0 | 32.38s |

这三组数字是后续章节的“健康基线”。以后你改任何东西，至少先跑对应的一组；如果这组本来就有失败，先记下来，别把已有失败算到自己头上。

---

## 0.7 这棵树的上游位置

`upstream/main` 的最新提交：

```bash
git log -1 --format='%h %cI %s' upstream/main
```

实测：

```text
0fc5afa04c23 2026-11-08T08:40:52+05:30 [NVPTX] Select scalar_to_vector for bf16x2 (#236706)
```

维护相关的三个事实：

1. **文档是 `.md`，不是 `.rst`。** 本快照 `llvm/docs/RISCVUsage.md` 有 808 行；`llvm/docs/RISCV/RISCVVectorExtension.md` 320 行；`llvm/docs/RISCV/RISCVVCIX.md` 250 行。网上指向 `.rst` 的链接在这里已经过时。
2. **RISC-V 没有 CODEOWNERS 条目。** `.github/CODEOWNERS` 只用于自动分配，`grep -i riscv` 为空。真正的 reviewer 名单在 `llvm/Maintainers.md:357-366`：Alex Bradbury (`asb`)、Craig Topper (`topperc`)、Philip Reames (`preames`)、Sam Elliott (`lenary`)。
3. **`[RISCV]` 是惯例，不是政策。** `DeveloperPolicy.md:345-349` 只说“改动局限在某个组件时，习惯上加方括号标签”。最近 6 个月 820 条提交里，约 80% 用 `[RISCV]`；但把 `[RISCV]` 说成“必须”是不准确的。

---

## 0.8 踩雷集锦

1. **`llc --version | grep riscv` 没输出，不等于代码没编。** 先看 `CMakeCache.txt` 的 `LLVM_TARGETS_TO_BUILD`；再看 `lit.site.cfg.py` 的 `targets_to_build`。两者都是 configure 时写死的。
2. **lit 全绿可能是假绿。** RISC-V 目录的 `lit.local.cfg` 会在 target 缺失时把整目录标 `UNSUPPORTED`。`-s` 模式下它只显示 `Unsupported`，不显示失败。
3. **`llvm-lit -Dtargets=RISCV` 没用。** `targets_to_build` 不通过 `lit_config.params` 读取；必须重新 configure。
4. **旧二进制比没有二进制更危险。** 本树重配前 `bin/lld` 还在，版本字符串是 `01df7c365d7c`。只要不打印 `--version`，你会以为链接测试测的是 `0fc5afa04c23`。
5. **切换 worktree 分支后不重编，测的是旧 `.so`。** `BUILD_SHARED_LIBS=ON` 时，`bin/llc` 只是薄壳，真正代码在 `lib/libLLVM*.so`。先跑 `ninja -n` 看有没有 pending。
6. **`ninja` 的 “Re-running CMake” 不是错误。** 它是正常的重新生成步骤；但如果它反复出现，说明某个 `CMakeLists.txt` 时间戳在循环触发，应该查 `ninja -d explain`。
7. **不要用别的 worktree 的 build 来“省时间”。** 源码/构建对不上时，行号、pass 顺序、测试结果全部不可信；本课的证据约定不允许这样做。

---

## 0.9 可跑实验

### 实验 1：复现完整审计（约 1 分钟）

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
T="$LLVM_HOME/builds/ct-instr-trunk"

git status -sb
git rev-parse --short HEAD
grep -E '^LLVM_TARGETS_TO_BUILD:|^LLVM_ENABLE_PROJECTS:' "$T/CMakeCache.txt"
$T/bin/llvm-config --targets-built
$T/bin/llc --version | grep -i riscv
$T/bin/ld.lld --version
$T/bin/llvm-lit -s -j24 llvm/test/CodeGen/RISCV/addcarry.ll
```

预期：分支是 `analysis/riscv-maintenance`，targets 含 `RISCV`，`llc` 列出 `riscv32/riscv64`，`ld.lld` 版本里出现 `0fc5afa04c23`，单测 `PASS`。

### 实验 2：亲手制造一次“假绿”

把 `llvm/test/CodeGen/RISCV/lit.local.cfg` 临时改成：

```python
if True:
    config.unsupported = True
```

再跑：

```bash
$T/bin/llvm-lit -s llvm/test/CodeGen/RISCV/addcarry.ll
```

你会看到 `Unsupported` 而不是 `FAIL`。改回去再跑一次，恢复 `PASS`。这个实验的目的是让你以后看到 “Unsupported” 时立刻想到 target 配置，而不是以为测试被跳过是正常的。

> 这是本课唯一建议你改 `lit.local.cfg` 的地方；做完务必 `git checkout -- llvm/test/CodeGen/RISCV/lit.local.cfg`。

### 实验 3：确认增量构建是否干净

```bash
cd "$T"
ninja -n | tail -3
```

如果输出只有 `[0/1] Re-running CMake...` 或为空，说明没有待编边；如果有成百上千条，先构建再跑测试。

---

## 0.10 源码与配置落点

| 内容 | 路径 | 关键行/符号 |
|---|---|---|
| 目标机与 pass 配置 | `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` | `RISCVPassConfig` 318；`createPassConfig` 360；`addIRPasses` 400；`addPreISel` 419；`addInstSelector` 449；`addIRTranslator` 455；`addPreSched2` 488；`addPreEmitPass` 497；`addPreEmitPass2` 516；`addMachineSSAOptimization` 544；`addPreRegAlloc` 568 |
| Subtarget/Feature 入口 | `llvm/lib/Target/RISCV/RISCVSubtarget.cpp` | `initializeSubtargetDependencies` 56；`ParseSubtargetFeatures` 75 |
| 扩展描述 | `llvm/lib/Target/RISCV/RISCVFeatures.td` | `RISCVExtension` 29；`FeatureStdExtZba` 495；`HasStdExtZba` 498 |
| 测试 gate | `llvm/test/CodeGen/RISCV/lit.local.cfg` | `if not "RISCV" in config.root.targets` |
| 构建配置 | `builds/ct-instr-trunk/CMakeCache.txt` | `LLVM_TARGETS_TO_BUILD`、`LLVM_ENABLE_PROJECTS`、`LLVM_SOURCE_DIR` |
| 生成的目标列表 | `builds/ct-instr-trunk/test/lit.site.cfg.py` | `config.targets_to_build`（第 44 行附近） |
| 维护者 | `llvm/Maintainers.md` | 357–366 行，RISCV backend |
| 使用文档 | `llvm/docs/RISCVUsage.md` | 808 行；扩展状态表在 97 行起 |

---

## 0.11 本章重点整理

- 本课的源码基线是 `trees/ct-instr-trunk` 的 `analysis/riscv-maintenance` 分支，HEAD `0fc5afa04c23`，与 `upstream/main` 0 领先 0 落后。
- RISC-V 后端在本 trunk 是 **210 个文件、157,290 行**；测试面超过 6,600 个文件；过去一年 1,545 条提交改到 `lib/Target/RISCV`。
- 构建不是“有就行”：必须 `X86;AArch64;RISCV` + `clang;lld`，否则 RISC-V 测试会静默 `UNSUPPORTED`，lld 测试会跑旧二进制。
- 当前验证基线：3,362 + 361 + 2,827 个测试全部通过，耗时分别约 25–26s / 10s / 32s。
- 维护者入口是 `llvm/Maintainers.md:357-366`，不是 CODEOWNERS；`[RISCV]` 是惯例而非强制。

## 0.12 自我检核

- [ ] 能说清 `trees/<task>`、`builds/<task>`、hub 三者的分工
- [ ] 知道为什么分析分支要从 `upstream/main` 开，而不是从旧修复分支开
- [ ] 能解释 `lit.local.cfg` + `lit.site.cfg.py` 如何让测试静默变 `UNSUPPORTED`
- [ ] 会检查 `CMakeCache.txt` 的 `LLVM_TARGETS_TO_BUILD` 与 `LLVM_ENABLE_PROJECTS`
- [ ] 会用 `--version` 里的 commit 判断工具与源码是否同源
- [ ] 记得住三组 lit 基线数字，并能复现其中任意一组

---

## 0.13 延伸阅读

- **`llvm/docs/RISCVUsage.md`**（本树 `/home/ubuntu/llvm/trees/ct-instr-trunk`，808 行）
  - **读哪里**：先读 `## Base ISAs`、`## Ratified Extensions`、`## Experimental Extensions`、`## Vendor Extensions` 四节；扩展状态表从 97 行开始。
  - **为什么读**：这是上游对“哪些扩展支持、哪些只是汇编支持”的权威口径。判断一个 issue 是“实现 bug”还是“本来就不支持”，先查这里。
- **`llvm/Maintainers.md`**（本树，357–366 行）
  - **读哪里**：只读 `#### RISCV backend` 这一节。
  - **为什么读**：PR 该 @ 谁、review 卡住该 ping 谁，这里比 CODEOWNERS 可靠。
- **[LLVM GitHub 文档](https://llvm.org/docs/GitHub.html)**（`llvm/docs/GitHub.md`，本树 568 行）
  - **读哪里**：`### Creating Pull Requests`、`### Approvals`、`### Landing your change`、`### Rebasing Pull Requests and Force Pushes`。
  - **为什么读**：Ch 29 会把这里的规则落到具体命令；先读一遍能避免“PR 标题写成 commit 标题”这类低级错误。
- **[LLVM lit 文档](https://llvm.org/docs/CommandGuide/lit.html)**
  - **读哪里**：`--param` / `-D`、`--filter`、`-s` 与 `-v` 的差异。
  - **为什么读**：本章的“假绿”实验就是 lit 配置语义的直接后果；Ch 27 会大量使用这些参数。
- **`/home/ubuntu/llvm/toolset/README.md`**（本机，非上游）
  - **读哪里**：`configure.sh`、`build.sh`、`status.sh` 三段。
  - **为什么读**：本课的构建命令都从这里来；手敲 cmake 参数会漏掉 `assertions`、`ccache`、`shared libs` 等关键设置。

---

→ 下一章：[Ch 01 — 后端全景：一条 RISC-V 指令经过的十二个站位](./01-backend-map.md)
