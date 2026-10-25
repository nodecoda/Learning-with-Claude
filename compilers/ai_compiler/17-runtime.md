# Ch 17 运行时：module 加载、内存池与 kernel launch

> **目标**：搞清编译产物离开编译器之后发生的事——`export_library` 存了什么、`load_module` 怎么把它映射进进程、一次调用要过几道检查和几次分配；并亲手制造几类加载失败，学会看真实报错。

## 为什么需要这个？

编译器只负责"生成"，runtime 负责"跑起来"。这两件事的边界，比多数人想的更重要：

- 同一个 kernel，放进 DLL 再加载，和进程内直接调用，**性能应该一致**——如果不一致，说明你测的不是 kernel。
- 每次调用都新建一个 VM frame、每次输出都新 malloc，会让"kernel 很快"的图在端到端里慢几倍。
- 真实部署里最常见的失败不是 kernel 算错，而是**加载失败**：缺 DLL、ABI 不匹配、导出时链接器少了符号。

而且 0.27 的 runtime 和网上教程差得很远：

- **GraphExecutor 没了**。`from tvm.contrib import graph_executor` 在 0.27 直接 `ImportError: cannot import name 'graph_executor'`。
- 图执行的主路径变成 `relax.build(...)` → `Executable` → `relax.VirtualMachine`。
- 单 kernel 路径还是 `tvm.build(...)` → `runtime.Module` + `PackedFunc`。

本章把这两条路径都跑一遍，并把 `load_module` 的失败现场保留下来。

## 先建立直觉：一次调用要过几道关

```text
Python:  vm["main"](x, w)
           │
           ├─ FFI 层：把 Python Tensor 打包成 DLTensor（data/shape/strides/byte_offset/dtype/device）
           ├─ 查找函数：VM 字节码解释器 或 PackedFunc
           ├─ wrapper 校验：num_args、type_index、shape、stride、byte_offset、dev_id
           ├─ 内核：生成的循环（可能已向量化）
           └─ 输出：从内存池取一块 buffer，包装成 Tensor 返回
```

这五步里，只有"内核"那一步是你写的代码。其余四步都是 runtime 的固定开销，也是本章要量的东西。

## 核心概念一：编译产物是"模块"，不是"文件"

`tvm.build` 返回的不是路径，而是一个 `runtime.Module`：

```python
import tvm
from tvm import te

A = te.placeholder((8,), name="A")
C = te.compute((8,), lambda i: A[i] * 2.0, name="C2")
f = tvm.build(te.create_prim_func([A, C]), target="llvm")
print(f.kind, f.entry_name, f.imports)
```

实测：

```text
llvm main ()
```

- `kind="llvm"`：这是一个 LLVM 模块，函数已经 JIT 进当前进程。
- `entry_name="main"`：默认入口名，`time_evaluator` 和 `export_library` 都用它。
- `imports=()`：CPU 构建没有导入任何 device 模块。换成 CUDA，这里会多一个 `cuda` 模块（本机无 CUDA，属 `guided`）。

Relax 那条路径产出的是另一种东西：

```python
from tvm import relax
from tvm.script import relax as R

@R.function
def main(x: R.Tensor((4, 8), "float32"), w: R.Tensor((8, 8), "float32")):
    with R.dataflow():
        y = R.matmul(x, w)
        z = R.add(y, R.const(1.0, "float32"))
        out = R.nn.relu(z)
        R.output(out)
    return out

ex = relax.build(tvm.IRModule({"main": main}), target="llvm")
print(type(ex).__name__)          # VMExecutable
vm = relax.VirtualMachine(ex, tvm.cpu())
```

`Executable` 里装的是"图 + 字节码 + 若干 TIR kernel"，VM 负责解释执行。两者都能 `export_library`，但加载后的调用方式不同。

## 核心概念二：参数打包与 wrapper 校验

看一段真实生成的入口函数签名：

```text
define dllexport i32 @__tvm_ffi_main(ptr %self_handle, ptr %args, i32 %num_args, ptr %result)
```

这是 PackedFunc 的 ABI：`args` 是一个 `DLTensor*` 数组，`num_args` 是长度。函数体前半段全是校验：

```text
%0 = icmp eq i32 %num_args, 3          ; 参数个数
%.not = icmp eq ptr %args, null        ; 参数数组非空
%A.type_index = load i32, ptr %args    ; dtype/device 类型码
%1 = icmp sgt i32 %A.type_index.fr, 63
...                                    ; strides、shape、byte_offset、dev_id
%70 = load i64, ptr %69, align 8       ; DLTensor.byte_offset
%71 = icmp eq i64 %70, 0
```

这些检查在 Python 侧表现为具体的报错。实测：

```python
m["main"](x, bad_shape)     # 传 9 个元素，kernel 期望 8 个
m["main"](x, y, y)          # 多传一个参数
```

```text
ValueError: Invalid C2.shape[0] on argument #1 when calling:
  `main(A: Tensor([8], float32), C2: Tensor([8], float32))`, expected 8

TypeError: Expected 2 arguments when calling:
  `main(A: Tensor([8], float32), C2: Tensor([8], float32))`
```

注意报错里直接印出了函数签名——这是 0.27 的 FFI 层根据 TIR 参数自动生成的。**看到这类错误，先对签名，不要怀疑 kernel。**

## 核心概念三：内存池

Relax VM 每次调用都要为中间结果分配 buffer。`memory_cfg` 控制它用不用池：

```python
vm = relax.VirtualMachine(ex, tvm.cpu(), memory_cfg="pooled")   # 或 "naive" / None
```

同一个 `matmul + add + relu`，先用 4×8（每次只分配几十字节的中间 buffer），再用 256×256×256（每次分配 256KB 级 buffer）；两个 VM 交错跑、每档取最小：

```text
4×8:          naive 8.84 us/call   pooled 8.61 us/call     ← −3%，在噪声内
256×256×256:  naive 30.25 ms/call  pooled 28.02 ms/call    ← −7%
```

结论和"池化一定快"的直觉有出入：**池化省的是 `malloc/free`，只有当每次调用要分配的 buffer 足够大时才看得出来。** 4×8 这一档，9 us 里绝大部分是 VM 解释和 FFI 打包，池化救不了；到 256³，分配才成为可见成本。对**大图 + 小 kernel** 的推理服务，分配次数多，比例会更极端——这是机制推论，本章没有做服务化压测，标 `guided`。

静态形状时，VM 还能把 buffer 大小在编译期算死；动态形状就必须每次从输入推导（见下一节）。

## 核心概念四：动态 shape 在 runtime 怎么走

编译期把 batch 写成符号 `b`：

```python
@R.function
def dyn(x: R.Tensor(("b", 8), "float32"), w: R.Tensor((8, 8), "float32")):
    with R.dataflow():
        y = R.matmul(x, w)
        R.output(y)
    return y

ex = relax.build(tvm.IRModule({"main": dyn}), target="llvm")
vm = relax.VirtualMachine(ex, tvm.cpu())
vm["dyn"](x, w)     # 函数名来自 global_symbol，不是 main！
```

实测四种 batch：

```text
dynamic b=  1: out (1, 8)  maxerr 2.38e-07
dynamic b=  4: out (4, 8)  maxerr 2.38e-07
dynamic b= 16: out (16, 8) maxerr 2.38e-07
dynamic b= 64: out (64, 8) maxerr 2.38e-07
```

三个要点：

1. **符号在调用时被绑定**：`b` 从输入的 shape 里读出来，再传给所有用到它的地方。
2. **输出在运行时分配**：`(b, 8)` 的 buffer 大小每次都可能不同，池化也只能按"够用"来复用。
3. **函数名是 `global_symbol`**：TVMScript 里 Python 函数叫 `dyn`，VM 里就查 `"dyn"`；查 `"main"` 会得到 `AttributeError: Module has no function 'main'`。这是本章踩过的一个真实坑。

动态形状不是免费的：每次调用多几次整数比较和一次动态分配，静态形状则可以把这些都折叠掉。Ch 23 会展开控制流和更复杂的符号推导。

## 底层机制：host 与 device 的分工

CPU 构建里，`f.imports == ()`——没有独立的 device 模块，所有代码在同一个 LLVM 模块里，`kernel launch` 就是一次普通函数调用。

GPU 构建则是两段式（本机无 CUDA，以下为 `guided`，依据 TVM 的模块组织方式）：

- host 模块：负责参数打包、stream 管理、按 grid/block 启动；
- device 模块（`cuda` / `ptx`）：包含真正的 kernel，作为 host 模块的 import 存在；
- `f.imports` 会列出 device 模块；`export_library` 会把它们打进同一个 fatbin/so。

这解释了为什么"编译成功"不等于"能跑"：**codegen 成功只说明 IR 能翻译，runtime 还需要 device 驱动、正确的 ABI、以及能被加载的二进制。**

## 示例一：`export_library` → `load_module` → 调用（实测）

这段是本机真实跑通的完整流程。先看 Windows 上的三个前置条件（Linux/macOS 只要系统有 `cc`，通常不需要这些）：

```powershell
# 1) 指定 clang（TVM 在 Windows 上只会找 clang）
$env:TVM_WIN_CC = "C:\Users\Think\scoop\apps\llvm\23.1.4\bin\clang.exe"
# 2) 指定 MSVC triple，否则 clang 会去找 gcc/ld
$env:TVM_WIN_TARGET = "x86_64-pc-windows-msvc"
# 3) 让链接器找到 MSVC 运行库和 Windows SDK（用分号连接，写成一行最稳）
$env:LIB = "C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Tools\MSVC\14.16.27023\lib\x64;C:\Program Files (x86)\Windows Kits\10\Lib\10.0.22621.0\um\x64;C:\Program Files (x86)\Windows Kits\10\Lib\10.0.22621.0\ucrt\x64"
```

还差一个：TVM 生成的代码会调用 `TVMFFIErrorSetRaisedFromCStrParts`，它在 `tvm_ffi.dll` 里。链接时需要一份 import library。可以用 LLVM 自带的 `llvm-dlltool` 从 DLL 的导出表生成：

```powershell
$bin = "C:\Users\Think\scoop\apps\llvm\23.1.4\bin"
$dll = ".scratch\aic-env\Lib\site-packages\tvm_ffi\lib\tvm_ffi.dll"
$names = & "$bin\llvm-readobj.exe" --coff-exports $dll |
         Select-String '^  Name: (TVMFFI\w+)$' |
         ForEach-Object { $_.Matches[0].Groups[1].Value }
$def = "LIBRARY tvm_ffi.dll`nEXPORTS`n" + (($names | ForEach-Object { "  $_" }) -join "`n")
Set-Content -LiteralPath ".scratch\ai-compiler\ch17\tvm_ffi.def" -Value $def -Encoding ascii
& "$bin\llvm-dlltool.exe" -m i386:x86-64 -d ".scratch\ai-compiler\ch17\tvm_ffi.def" `
    -l ".scratch\ai-compiler\ch17\tvm_ffi.lib"
```

实测导出 50 个 `TVMFFI*` C 符号。然后：

```python
import tvm, os, time, numpy as np
from tvm import te

d = r"D:\Learning-with-Claude\.scratch\ai-compiler\ch17"
N = 512
A = te.placeholder((N, N), name="A")
B = te.placeholder((N, N), name="B")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
i, j, kk = sch.get_loops(sch.get_sblock("C"))
i_o, i_i = sch.split(i, factors=[None, 16])
j_o, j_i = sch.split(j, factors=[None, 16])
k_o, k_i = sch.split(kk, factors=[None, 16])
sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)
f = tvm.build(sch.mod, target="llvm")

lib = os.path.join(d, "tvm_ffi.lib")
p = os.path.join(d, "matmul512.dll")
t0 = time.perf_counter()
f.export_library(p, options=[lib])          # 关键：把 import lib 传给链接器
print("export", round((time.perf_counter() - t0) * 1e3), "ms", os.path.getsize(p), "bytes")

t0 = time.perf_counter()
m = tvm.runtime.load_module(p)
print("load", round((time.perf_counter() - t0) * 1e3, 1), "ms", m.kind)
```

实测：

```text
export 214 ms 112640 bytes
load 2.1 ms library
```

112KB 里装着 kernel 代码 + CRT，已经是一个可以**脱离编译进程**加载的独立产物——在全新进程里 `load_module` 后调用，`C[0,0]` 仍然是 `1024.0`。

## 示例二：`.ll` 路线——不需要任何 C 工具链

如果只是想把"编译产物"落盘再加载，LLVM IR 文本也能走通：

```python
ll = os.path.join(d, "matmul512.ll")
f.write_to_file(ll)                  # 75.9 KB 文本
h = tvm.runtime.load_module(ll)      # 23–31 ms：重新编译 IR
```

区别很重要：

| 路线 | 产物 | 加载时做什么 | 适合 |
|---|---|---|---|
| `.dll` / `.so` | 机器码 + CRT | 映射进地址空间 | 部署、跨进程 |
| `.ll` | LLVM IR 文本 | 重新走一遍 LLVM 后端 | 调试、看 codegen |

`.ll` 路线在加载时**重新编译**，所以慢一个数量级（2.1 ms vs 23.4 ms）。它不能替代部署产物，但非常适合排查"到底是 IR 错了还是机器码错了"。

## 示例三：`time_evaluator` 与直接 Python 调用

Python 调一次 PackedFunc 有固定开销。用两个 kernel 量它：

```python
import tvm, numpy as np, time
from tvm import te

A = te.placeholder((8,), name="A")
C = te.compute((8,), lambda i: A[i] * 2.0, name="C2")
f8 = tvm.build(te.create_prim_func([A, C]), target="llvm")

x = tvm.runtime.tensor(np.ones(8, "float32"), tvm.cpu())
y = tvm.runtime.empty((8,), "float32", tvm.cpu())

# 方式 A：time_evaluator（C++ 侧循环，不含 Python 开销）
tev = f8.time_evaluator(f8.entry_name, tvm.cpu(), number=20000, repeat=5)
print(tev(x, y).min)

# 方式 B：Python for 循环（含每次调用开销）
t0 = time.perf_counter()
for _ in range(3000):
    f8(x, y)
print((time.perf_counter() - t0) / 3000)
```

实测：

```text
8 元素 kernel:   Python 循环 0.44 us/call | time_evaluator 0.019 us/call
64K 元素 kernel: Python 循环 19.00 us/call | time_evaluator 18.46 us/call
```

结论：

- **每次 Python 调用约 0.4–0.5 us 固定开销**（FFI 打包 + 函数查找）。
- 对 8 元素的 kernel，这个开销是 kernel 本身的 20 倍——**小算子必须靠融合消灭，而不是靠优化 kernel**。
- 对 64K 元素的 kernel，开销只占 3%——此时 `time_evaluator` 和 Python 循环几乎一样。

所以：**测 kernel 用 `time_evaluator`，测端到端用 Python 调用并减去 0.4 us × 调用次数。**

## 示例四：从导出到加载的八种失败现场

这些报错都是本机真实产生的，按出现顺序排列：

**1）没装 clang**

```text
RuntimeError: Can not find the LLVM clang for Windows clang.exe).Make sure it's installed
and the installation directory is in the %PATH% environment variable.
```

**2）clang 有，但 target triple 不对**

```text
clang: error: unable to execute command: program not executable
clang: error: linker (via gcc) command failed with exit code 1
```

因为 `--target=x86_64` 让 clang 去找 gcc 的 `ld`。

**3）triple 对了，但缺 MSVC 运行库**

```text
lld-link: error: could not open 'libcmt.lib': no such file or directory
lld-link: error: could not open 'oldnames.lib': no such file or directory
```

**4）运行库有了，但缺 TVM 的 import lib**

```text
lld-link: error: undefined symbol: TVMFFIErrorSetRaisedFromCStrParts
>>> referenced by lib0.o:(__tvm_set_raised_6)
```

**5）强行忽略未解析符号（错误做法）**

```python
f.export_library("bad.dll", options=["-nostdlib", "-Wl,/FORCE:UNRESOLVED"])
```

能链接出 5KB 的 DLL，但加载时：

```text
InternalError: Check failed: (lib_handle_ != nullptr) is false:
Failed to load dynamic shared library ...\bad.dll
```

**6）文件被截断**

同样报 `Failed to load dynamic shared library`——DLL 的 PE 头不完整，加载器直接拒绝。

**7）扩展名不认识**

把 `.dll` 改名成 `.dat` 再加载：

```text
RuntimeError: Loader for `.dat` files is not registered, resolved to
(ffi.Module.load_from_file.dat) in the global registry. Ensure that you have
loaded the correct runtime component.
```

**8）文件不存在**

```text
ValueError: cannot find file D:\...\missing.dll
```

这八条按层次分三类：前四条发生在**导出/链接**阶段（1–4），后四条发生在**加载**阶段（5–8）。加载侧正好对应 `load_module` 源码的三个层次：先 `os.path.isfile` 检查（否则 `ValueError`），再按扩展名分发给注册过的 loader（否则 `Loader for ... is not registered`），最后才调用 `DSOLibrary::Load`（失败就是 `Failed to load dynamic shared library`）。

## 对比与取舍

| 需求 | 选择 | 理由 |
|---|---|---|
| 进程内快速迭代 | `tvm.build` 直接用 | 无落盘、无加载 |
| 跨进程/跨机器部署 | `export_library` → `.so`/`.dll` | 自包含，加载快（~2ms） |
| 想看 codegen 结果 | `write_to_file(".ll")` | 纯文本，可 diff |
| 图级执行、动态 shape | `relax.build` + `VirtualMachine` | 0.27 唯一主路径 |
| 小算子多、调用频繁 | 先融合，再看 VM 开销 | 每次调用 0.4–0.5 us |
| 大 buffer 频繁分配 | `memory_cfg="pooled"` | 256³ 实测省 ~7%；小 kernel 在噪声内 |
| CUDA 部署 | host + device 双模块 | 本机无 GPU，`guided` |

## 踩雷集锦

1. **以为 `export_library` 只是"存文件"。** 它在 Linux/macOS 会调用 `cc` 现场编译；Windows 上要 clang + MSVC 运行库 + import lib，缺一样都失败。
2. **用 `-Wl,/FORCE:UNRESOLVED` 糊过去。** 链接成功、加载失败，错误还很难懂。
3. **加载后查 `"main"` 却报 no function。** Relax 函数名来自 `global_symbol`；TVMScript 里叫 `dyn` 就查 `"dyn"`。
4. **用 Python `for` 循环测小 kernel。** 0.44 us 里 0.42 us 是开销。
5. **每次推理新建 VM。** VM 构造本身有成本；服务里应该复用。
6. **在 Windows 上忘了 `TVM_WIN_TARGET`。** 默认 triple 会让 clang 走 gcc 链接器，报"program not executable"。
7. **以为 `.ll` 是部署格式。** 加载时重新编译，23ms 起步。

## 进阶：再往深一层

- 用 `m.get_function("main", query_imports=True)` 检查加载后的模块是否暴露入口。
- 把 `export_library` 的目标改成 `.tar`，看 TVM 如何打包多个模块（RPC 场景）。
- 用 `relax.build(..., exec_mode="bytecode")` 之外的模式对比（本机只验证了 bytecode 默认值）。
- 给 VM 传 `memory_cfg={"cpu": "pooled"}` 的字典形式，观察多 device 场景。
- 读 `src/runtime/library_module.cc` 与 `DSOLibrary::Load`，理解"加载"到底做了什么（源码依据见延伸阅读）。

## 动手练习

### 练习 1：把 kernel 存成 DLL 再加载

用本章的 matmul，`export_library` 存成 `.dll`，在**新进程**里 `load_module` 并调用，验证结果与进程内一致。

<details>
<summary>参考思路</summary>

新进程只需 `import tvm`（它会加载 `tvm_ffi.dll`），然后 `load_module`。若报 `Failed to load dynamic shared library`，先检查是不是用了 `FORCE:UNRESOLVED`；若报 `undefined symbol`，说明导出时没把 import lib 传给链接器。

</details>

### 练习 2：量出你自己机器的调用开销

写一个 8 元素 kernel，分别用 `time_evaluator` 和 Python 循环测 1 万次，算出单次 Python 开销。再换一个 64K 元素的 kernel 重复，解释为什么开销占比变了。

<details>
<summary>参考思路</summary>

本机约 0.42–0.5 us/call。占比从 95% 降到 3%，因为 kernel 时间随数据量线性增长，而 FFI 开销是常数。这条曲线决定了"多小的算子值得融合"。

</details>

### 练习 3：制造一次 ABI 错误

把导出的 DLL 复制一份并截断到 2KB，再加载；记录报错。然后思考：这个错误在部署时最容易由什么操作引起？

<details>
<summary>参考思路</summary>

报 `Failed to load dynamic shared library`。常见诱因：CI 传输中断、磁盘配额、Docker 镜像层被裁剪、`.so` 被 strip 掉了符号表（不一定会失败，但可能缺入口）。

</details>

### 练习 4：动态 shape 的边界

用 `b` 作为 batch 编译一个 matmul，测 b=1/8/64/512 的每次调用时间，并和静态编译的 4 个版本对比。

<details>
<summary>参考思路</summary>

动态版每次多几次比较和一次动态分配，小 batch 时开销占比高；大 batch 时 kernel 主导，差距缩小。静态版可以把 shape 检查常量折叠掉，但需要为每个 batch 编译一份。

</details>

## 本章重点整理

- `tvm.build` 产出 `runtime.Module`（kind/entry_name/imports），`relax.build` 产出 `Executable`，两者都能导出。
- 0.27 移除了 GraphExecutor，图执行走 Relax VM。
- 每次调用要过：FFI 打包 → 函数查找 → wrapper 校验（num_args/type/shape/stride/byte_offset）→ kernel → 输出分配。
- `memory_cfg="pooled"` 省的是分配开销：256³ 实测 ~7%，4×8 在噪声内。
- 动态 shape 在调用时绑定符号、运行时分配输出；函数名来自 `global_symbol`。
- `export_library` 在 Windows 上需要 clang + triple + MSVC 运行库 + TVM import lib；`.ll` 路线不需要工具链但加载慢 10 倍。
- Python 每次调用固定开销约 0.4–0.5 us；小算子应融合而非优化。

## 自我检核

- [ ] 我能说出 `tvm.build` 和 `relax.build` 产物类型与调用方式的区别
- [ ] 我知道 0.27 里 GraphExecutor 发生了什么
- [ ] 我能解释 wrapper 校验了哪些字段，并能从报错定位是哪个参数不对
- [ ] 我能用 `memory_cfg` 并说出池化省在哪
- [ ] 我知道动态 shape 在 runtime 的三个动作（绑定、推导、分配）
- [ ] 我能在 Windows 上独立完成一次 `export_library` + `load_module`
- [ ] 我能区分 `time_evaluator` 与 Python 调用的测量含义
- [ ] 我看到 `Failed to load dynamic shared library` 时知道从哪几层排查

## 延伸阅读

- **TVM `runtime.Module` API 文档**（`tvm/runtime/module.py:148` 与 `:424`）：读 `export_library` 和 `load_module` 的实现。与本章的关联：加载分派与 `.o`/`.tar` 的自动编译逻辑。
- **TVM `support/cc.py`**（`tvm/support/cc.py:374` `_windows_compile`）：读 Windows 链接分支。与本章的关联：为什么必须设 `TVM_WIN_CC` 和 `TVM_WIN_TARGET`。
- **DLPack 规范**（https://github.com/dmlc/dlpack）：读 `DLTensor` 结构。与本章的关联：wrapper 校验的每个字段都来自这里。
- **Relax VM 设计文档**（https://tvm.apache.org/docs/arch/）：读 VM 指令集与寄存器分配。与本章的关联：`memory_cfg` 和字节码执行。
- **TVM 源码 `src/runtime/library_module.cc`**：读 `DSOLibrary::Load`。与本章的关联："加载失败"的最后一层就是它。

---

→ [练习 C：手写 schedule 跑赢朴素实现](./practice-c-schedule-matmul.md)
