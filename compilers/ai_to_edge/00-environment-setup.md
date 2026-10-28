# Ch 0 — 环境搭建：一条能真跑的工具链

> **目标**：把「模型 → 中间表示 → 机器码 → 在设备上跑出数字」这条链路需要的工具装齐，并且每一件都验证过版本。学完这章，你能在 10 分钟内判断一台机器能不能做本课的实验。

> **环境**：Ubuntu 22.04 LTS、x86_64、LLVM 14.0.0、PyTorch 2.9.1+cpu、MLIR 14、aarch64-linux-gnu-gcc 11.4、arm-none-eabi-gcc 10.3、qemu 6.2。其他版本行为差异会在对应章节标注。

## 为什么需要这个？

这门课和大多数「AI 编译器」教程最大的不同：**每个结论都要在本机跑出来**。如果工具链装不对，你会把「我环境有问题」误判成「这个技术不成立」——这是学编译器最耗时间的坑。

更麻烦的是，这条链上每一环都挑剔版本：

```
PyTorch 2.9 ──► torch.export 的 API 每两个版本改一次
MLIR 14   ──► func.func 这个写法是 MLIR 15 才有的，14 只认 func
LLVM 14   ──► 只装 X86 target 的构建无法生成 ARM 代码
qemu      ──► user-mode 和 system-mode 是两件事
```

所以本章不是「复制粘贴安装命令」，而是**装完之后逐项验证**。

## 先建立直觉

把整条链想成一条流水线，每段有不同的「翻译官」：

```
 ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
 │ PyTorch  │──►│ AI 编译器 │──►│   MLIR   │──►│   LLVM   │──►│  设备    │
 │ 模型代码  │   │ Inductor  │   │ 多级 IR  │   │ 后端     │   │ 真跑     │
 └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
    Python        Python/C++      mlir-opt       llc/clang      qemu/板子
```

每段翻译官只认自己那套格式。装环境就是把这五个翻译官请到位，并且**确认他们说同一种方言**。

## 基础工具

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake ninja-build git curl \
  python3 python3-venv python3-pip
```

验证：

```bash
gcc --version | head -1
cmake --version | head -1
python3 --version
```

实测输出（本机）：

```
gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0
cmake version 3.22.1
Python 3.10.12
```

## LLVM 与 Clang

**这是最容易装错的一环。** Ubuntu 22.04 的 `llvm-14` 包含全部后端，但如果你自己从源码构建，默认只有 X86：

```bash
# 发行版包：包含 aarch64 / arm / riscv / x86 等全部 target
sudo apt-get install -y llvm-14 clang-14 lld-14
```

验证 target 是否齐全：

```bash
/usr/lib/llvm-14/bin/llc --version | sed -n '/Registered Targets/,$p' | head -12
```

实测输出：

```
Registered Targets:
  aarch64    - AArch64 (little endian)
  aarch64_32 - AArch64 (little endian ILP32)
  aarch64_be - AArch64 (big endian)
  amdgcn     - AMD GCN GPUs
  arm        - ARM
  arm64      - ARM64 (little endian)
  ...
  riscv32    - 32-bit RISC-V
  riscv64    - 64-bit RISC-V
```

如果这里只有 `x86` / `x86-64`，**后面 Ch 18–20 全部跑不了**。这不是"可能有问题"，是确定跑不了。

### 一个真实踩坑：自建 LLVM 只编了 X86

本机原本有一个从源码构建的 LLVM 24：

```bash
$ grep -E 'LLVM_TARGETS_TO_BUILD|LLVM_ENABLE_PROJECTS' build/CMakeCache.txt
LLVM_TARGETS_TO_BUILD:STRING=X86
LLVM_ENABLE_PROJECTS:STRING=clang;lld
```

它版本更新（24.0.0git）、带 assertions，看起来很理想。但：

- `LLVM_TARGETS_TO_BUILD=X86` → **生成不了 AArch64 汇编**
- `LLVM_ENABLE_PROJECTS` 里没有 `mlir` → **没有 `mlir-opt`**

> **错误直觉**：「版本越新越好」。  
> **正确认识**：编译器工具链是**按 target 和 project 裁剪**的。选工具链第一件事是看它编了哪些 backend、哪些 project，不是看版本号。

自建全 target 的代价（供参考）：需要 `-DLLVM_TARGETS_TO_BUILD="X86;AArch64;ARM;RISCV" -DLLVM_ENABLE_PROJECTS="clang;lld;mlir"`，Release 构建在 28 核机器上仍要数十分钟到数小时。本课用发行版包，因为**结论不依赖 LLVM 24 的新特性**。

## MLIR

```bash
sudo apt-get install -y mlir-14-tools
```

验证：

```bash
/usr/lib/llvm-14/bin/mlir-opt --version | head -1
/usr/lib/llvm-14/bin/mlir-opt --help | grep -c 'convert-'
```

实测：

```
Ubuntu LLVM version 14.0.0
45
```

### 第二个真实踩坑：`func.func` 在 MLIR 14 不存在

网上大量 MLIR 例子长这样：

```mlir
func.func @add(%a: i32) -> i32 { ... }
```

在 MLIR 14 上直接报错：

```
s.mlir:2:3: error: custom op 'func.func' is unknown
  func.func @add(%a: i32, %b: i32) -> i32 {
  ^
```

原因：`func` 独立成 dialect 是 MLIR 15 的事。MLIR 14 里函数还是 `builtin.func`，语法糖写作 `func`：

```mlir
module {
  func @add(%a: i32, %b: i32) -> i32 {
    ...
  }
}
```

试出来的过程（本机实测）：

```
--- builtin.func -> module { func @f() {        ✅ 通过
--- func.func    -> error: custom op 'func.func' is unknown   ❌
--- std.func     -> error: custom op 'std.func' is unknown    ❌
--- llvm.func    -> error: 'std.return' op expects parent op 'builtin.func'
```

> **教训**：MLIR 的方言名和语法糖在不同版本间会变。**先 `--help` 看 dialect 列表，再动手写 IR**。`mlir-opt --help` 第一段就是 `Available Dialects`。

## PyTorch

用 venv 隔离，避免污染系统 Python：

```bash
python3 -m venv --system-site-packages ~/.venvs/learn
~/.venvs/learn/bin/pip install numpy
```

`torch` 有一个体积陷阱：PyPI 上的默认 wheel 捆绑 CUDA 依赖，接近 1 GB。CPU 版只有约 176 MB：

```bash
# 国内镜像的 CPU wheel 目录（注意是网页目录，不是 pip index）
curl -s https://mirrors.aliyun.com/pytorch-wheels/cpu/ \
  | grep -oE 'torch-2\.9\.1%2Bcpu-cp310[^"]*' | head -1

# 直接下载并安装（文件名必须保留合法的 wheel 命名）
curl -O "https://mirrors.aliyun.com/pytorch-wheels/cpu/torch-2.9.1%2Bcpu-cp310-cp310-manylinux_2_28_x86_64.whl"
~/.venvs/learn/bin/pip install ./torch-2.9.1+cpu-cp310-cp310-manylinux_2_28_x86_64.whl
```

> **踩坑**：`pip install torch==2.9.1+cpu` 会失败（`No matching distribution found`），因为 `+cpu` 是本地版本标识，索引里的包名不带它。必须**下载 wheel 文件再按路径安装**。

验证（本机实测）：

```bash
~/.venvs/learn/bin/python -c "
import torch
print('torch', torch.__version__)
print('inductor:', torch._dynamo.is_inductor_supported())
"
```

```
torch 2.9.1+cpu
inductor: True
```

## 交叉工具链

边缘设备不是 x86。三条主流路线：

```bash
# AArch64（跑 Linux 的边缘盒子、树莓派、手机）
sudo apt-get install -y gcc-aarch64-linux-gnu libc6-dev-arm64-cross

# Cortex-M 裸机（MCU，没有 OS）
sudo apt-get install -y gcc-arm-none-eabi

# RISC-V（另一条开放 ISA 路线）
sudo apt-get install -y gcc-riscv64-linux-gnu libc6-dev-riscv64-cross
```

验证：

```bash
aarch64-linux-gnu-gcc --version | head -1
arm-none-eabi-gcc --version | head -1
riscv64-linux-gnu-gcc --version | head -1
```

```
aarch64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0
arm-none-eabi-gcc (15:10.3-2021.07-4) 10.3.1 20210621 (release)
riscv64-linux-gnu-gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0
```

## QEMU：两种模式，别搞混

```bash
sudo apt-get install -y qemu-user-static qemu-system-arm
```

这两个**不是一回事**：

| 工具 | 模式 | 用途 | 本课用于 |
|---|---|---|---|
| `qemu-aarch64-static` | user-mode | 直接跑单条 ARM64 程序，透明转发系统调用 | Ch 18、21、25 |
| `qemu-system-arm` | system-mode | 模拟整块板子（CPU + 内存 + 外设） | Ch 19 裸机 |

user-mode 跑用户态程序，几毫秒启动；system-mode 要模拟外设，还得自己写启动代码。

## 一次性验证脚本

把下面存成 `code/00_env_check.sh`，全绿才算环境就绪：

```bash
#!/usr/bin/env bash
set -uo pipefail
fail=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "  OK   $1"; else echo "  FAIL $1"; fail=1; fi; }

echo "== toolchain =="
chk "llc has aarch64"   "/usr/lib/llvm-14/bin/llc --version | grep -q aarch64"
chk "llc has riscv64"   "/usr/lib/llvm-14/bin/llc --version | grep -q riscv64"
chk "mlir-opt present"  "/usr/lib/llvm-14/bin/mlir-opt --version"
chk "mlir-translate"    "/usr/lib/llvm-14/bin/mlir-translate --version"
chk "aarch64 gcc"       "aarch64-linux-gnu-gcc --version"
chk "arm-none-eabi gcc" "arm-none-eabi-gcc --version"
chk "riscv64 gcc"       "riscv64-linux-gnu-gcc --version"
chk "qemu aarch64"      "qemu-aarch64-static --version"
chk "qemu system arm"   "qemu-system-arm --version"

echo "== python =="
chk "torch"  "~/.venvs/learn/bin/python -c 'import torch'"
chk "numpy"  "~/.venvs/learn/bin/python -c 'import numpy'"

echo
[ $fail -eq 0 ] && echo "环境就绪" || echo "有项目失败，按上面 FAIL 逐项修"
exit $fail
```

实测输出：

```
== toolchain ==
  OK   llc has aarch64
  OK   llc has riscv64
  OK   mlir-opt present
  OK   mlir-translate
  OK   aarch64 gcc
  OK   arm-none-eabi gcc
  OK   riscv64 gcc
  OK   qemu aarch64
  OK   qemu system arm

== python ==
  OK   torch
  OK   numpy

环境就绪
```

## 第一个端到端实验

不要等看完十章再动手。现在就跑这一条：

```bash
cd code/01_hello_llvm && make
```

实测输出：

```
/usr/lib/llvm-14/bin/clang --target=aarch64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll
/usr/lib/llvm-14/bin/llc -march=aarch64 add.ll -o add_a64.s
/usr/lib/llvm-14/bin/llc -march=aarch64 -filetype=obj add.ll -o add_a64.o
```

看生成的指令：

```bash
grep -E 'madd|orr' code/01_hello_llvm/add_a64.s
```

```
	orr	w8, wzr, #0x1
	madd	w0, w1, w0, w8
```

`add.c` 里写的是 `a * b + 1`，变成了**一条** `madd`（multiply-add）。这就是整门课要追的东西：源码意图 → 中间表示 → 目标机器上那条指令。

## 踩雷集锦

1. **自建 LLVM 只编了一个 target**：`llc --version` 里看不到 `aarch64`，后面所有 ARM 实验都会失败。检查 `Registered Targets`，不是看版本号。
2. **`func.func` 与 MLIR 14**：直接抄网上例子会报 `unknown op`。先跑 `mlir-opt --help` 看 dialect 列表。
3. **`pip install torch==X+cpu` 失败**：`+cpu` 是本地版本标识，索引里没有这个名字。下载 wheel 再按路径装。
4. **qemu user-mode 与 system-mode 混用**：`qemu-aarch64-static` 跑不了裸机固件（没有内核、没有系统调用），会直接报错或挂死。裸机必须 `qemu-system-arm -M <板子>`。
5. **交叉 gcc 找不到头文件**：只装 `gcc-aarch64-linux-gnu` 不装 `libc6-dev-arm64-cross`，编译含 `<stdio.h>` 的程序会失败。两个都要装。

## 本章重点整理

- 编译器工具链要按 **target** 和 **project** 检查，不是按版本号。
- MLIR 方言名随版本变化，`--help` 是最快的事实来源。
- `+cpu` 这类本地版本标识不能直接写进 pip 需求。
- qemu 的 user-mode 与 system-mode 解决完全不同的问题。

## 自我检核

- [ ] 不看笔记，说出为什么「LLVM 24」可能比不上「LLVM 14」做本课实验
- [ ] 能解释 `llc --version` 里 `Registered Targets` 少了 `aarch64` 意味着什么
- [ ] 知道 MLIR 14 里函数该怎么写，以及怎么自己查出来
- [ ] 能区分 `qemu-aarch64-static` 和 `qemu-system-arm` 各自能跑什么
- [ ] 能说出至少两个「装错环境会导致误判技术不成立」的例子

## 延伸阅读

### 官方文档 / Spec

- **[LLVM Getting Started](https://llvm.org/docs/GettingStarted.html)**
  - **读哪里**：`Building LLVM with CMake` 一节里的 `LLVM_TARGETS_TO_BUILD` 和 `LLVM_ENABLE_PROJECTS` 说明
  - **为什么值得读**：这是理解「为什么同一个 LLVM 版本能力不同」的权威来源，也是自建时的必查项
- **[MLIR Getting Started](https://mlir.llvm.org/getting_started/)**
  - **读哪里**：「Build MLIR」与 dialect 注册部分
  - **注意**：文档跟着主干走，14 与 15+ 的 `func` 差异文档里不一定显式写，以 `--help` 实测为准

### 工具链

- **[QEMU User Mode Emulation](https://www.qemu.org/docs/master/user/main.html)**
  - **读哪里**：开头「User Mode Emulation」与限制说明
  - **为什么值得读**：一次讲清 user-mode 到底模拟了什么、为什么不能跑裸机
- **[GNU Arm Embedded Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)**
  - **读哪里**：release notes 里的 target 支持列表
  - **前提**：想换更新的 Cortex-M 工具链时再来看，apt 版本（10.3）够本课用

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版，2011）
  - **读哪几章**：第 1 章讲清「前端/后端」分工，读完再来理解本章流水线图；第 2 章扫描与 IR 也是后面 Ch 12 的铺垫
  - **注意**：书里不涉及 AI 编译器，只补编译器基本功

装好环境之后，下一章我们把这条流水线拆成七次「形态变化」，看清一个模型到底经过了什么。

→ [Ch 1 全景：一个模型到一条指令的七次变身](./01-pipeline-overview.md)
