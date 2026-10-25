# Ch 28 读 TVM 源码：从 Python wrapper 到 FFI 边界

> **目标**：建立一套能复用的读源码方法：先用 Python wrapper 定位入口，再用 `_ffi_api` 找到边界，最后决定要不要下到 C++。用本机 TVM 0.27.0.post1 真查三条路线——`tvm.build`、`te.create_prim_func → s_tir.Schedule`、`inspect_source()`——每条都给文件与行号。

## 为什么需要这个？

学 AI 编译器到后期，最容易卡在三种问题上：

- **API 为什么长这样？** 比如 0.27 为什么把 `tvm.tir` 拆成 `s_tir` 和 `tirx`？为什么 `te.create_schedule` 没了？
- **这个报错从哪来？** 栈里一半是 Python，一半是 `function.pxi`，最后一行却是 C++ 文件。
- **IR 为什么变了？** `split` 之后多出 `k_0 * 4 + k_1`，是哪个函数改的？

这些问题，文档只能回答一半。但“读源码”不等于从 `src/` 第一行读起：TVM 的 Python 包有 200 多个文件、几万行，硬读会迷路。正确姿势是**带着一条调用链去读**，读到需要的边界就停。

本章有一个现实限制：本机只装了 wheel，没有 C++ 源码。

```text
D:\Learning-with-Claude\.scratch\aic-env\Lib\site-packages\tvm\
  ├── *.py            ← Python wrapper（可读）
  ├── lib/            ← libtvm.dll（二进制）
  └── include/        ← 头文件（部分）
```

`src/` 不在本机。所以本章的 C++ 结论分两种：能从 Python 侧观察到的（`_ffi` 边界、global func、错误栈里的文件名/行号）标 `源码依据`；需要打开 `src/` 才能确认的标 `guided`。**不伪造 C++ 源码内容。**

下文所有 `文件:行号` 都是本机 wheel 的实测命中（`源码依据`），不是凭记忆。

## 先建立直觉：源码是三层洋葱

把 TVM 想成三层，读源码的难度和收益都随层数上升：

| 层 | 你看到的东西 | 能回答的问题 | 不能回答的问题 |
|---|---|---|---|
| Python wrapper | `driver/build_module.py`、`s_tir/schedule/schedule.py` | API 的参数、默认值、类型检查、对象关系 | pass 具体怎么改写 IR |
| FFI 边界 | `_ffi_api.py`、`tvm_ffi`、`get_global_func` | Python 把什么交给了 C++、函数叫什么名字 | C++ 内部算法 |
| C++ 实现 | `src/tirx/...`、`src/target/llvm/...` | 调度合法性、lowering、codegen 细节 | ——（本机没有） |

读源码的第一原则：**先确定你停在哪一层**。如果你只是想知道“`sch.mod` 是什么类型”，停在 Python 就够了；如果你想问“`split` 为什么拒绝这个 loop”，那就必须下到 C++ 的 schedule 实现（`guided`）。

第二原则：**让 IR 自己说话**。很多“源码问题”其实用 `script()`、`inspect_source()`、错误诊断就能回答，不需要读实现。Ch 12 已经演示过：打印 `sch.mod.script()` 比猜 `split` 干了什么有效得多。

## 核心概念一：目录地图

先给一张按 wheel 实际内容统计的地图。统计命令：

```powershell
$p = "D:\Learning-with-Claude\.scratch\aic-env\Lib\site-packages\tvm"
foreach ($d in @("relax","s_tir","tirx","target","runtime","te","driver","ir")) {
  $files = Get-ChildItem "$p\$d" -Filter *.py -Recurse
  "{0,-10} py_files={1,3}  lines={2,7}" -f $d, $files.Count, (($files | Get-Content | Measure-Object -Line).Lines)
}
```

本机输出（2026-10，tvm 0.27.0.post1）：

```text
relax      py_files=212  lines=  63330
s_tir      py_files=179  lines=  28502
tirx       py_files= 52  lines=  17235
target     py_files= 12  lines=   926
runtime    py_files= 15  lines=  3042
te         py_files=  6  lines=   917
driver     py_files=  3  lines=   135
ir         py_files= 24  lines=  3595
```

几个立刻能看出来的事实：

- **`relax` 是最大的包**：它包含图 IR、算子、类型、VM、训练、分布式、前端。读 Relax 要按子目录读，不要从头读。
- **`s_tir` 比 `tirx` 大**：`s_tir` 里还有 `meta_schedule`、`dlight`、`tensor_intrin`；`tirx` 是新 TIR 的表达式/语句/构建入口。
- **`driver` 只有 135 行**：`tvm.build` 和 `tvm.compile` 都只是薄薄一层转发。真正的 build 逻辑在 `tirx/build.py`。
- **`target` 只有 926 行**：target 的解析、tag、intrinsic 查询在 Python；具体 codegen 在 C++。

### 五个必须认识的目录

| 目录 | 关键文件 | 负责什么 | 读它的时机 |
|---|---|---|---|
| `python/tvm/relax` | `expr.py`、`pipeline.py`、`vm_build.py`、`op/`、`transform/` | 图级 IR、pass、VM 执行 | 想知道“图优化在哪做” |
| `python/tvm/s_tir` | `schedule/schedule.py`、`dlight/`、`meta_schedule/` | 调度原语、自动调度、自动调优 | 想知道“循环怎么被改写” |
| `python/tvm/tirx` | `build.py`、`compilation_pipeline.py`、`function.py`、`stmt.py` | 新 TIR、lowering 管线、build 入口 | 想知道“TIR 怎么变成 module” |
| `python/tvm/target` | `target.py`、`codegen.py`、`tag_registry/` | target 解析、tag、codegen 查询 | 想知道“target 字符串怎么生效” |
| `python/tvm/runtime` | `module.py`、`executable.py`、`vm.py`、`_tensor.py` | module 加载、tensor、VM | 想知道“编译产物怎么跑” |

## 核心概念二：路线 1——`tvm.build` 到 codegen

先看入口。`tvm/__init__.py:95-96` 把 `build` 从 `driver` 里导出：

```python
# python/tvm/__init__.py:95-96
# tvm.driver
from .driver import build, compile
```

`driver/build_module.py` 本身非常薄：

```python
# python/tvm/driver/build_module.py:31-60
def build(mod, target=None, pipeline="default"):
    ...
    warnings.warn("build is deprecated. Use `tvm.compile` or `tvm.tirx.build` instead.", DeprecationWarning)
    return tvm.tirx.build(mod, target, pipeline)
```

所以 `tvm.build` 只是**兼容层**。真正的工作从 `tvm/tirx/build.py:157` 开始：

| 行号 | 做什么 |
|---|---|
| `tirx/build.py:180-183` | `PrimFunc` 包成 `IRModule` |
| `tirx/build.py:215` | `BindTarget(target_to_bind)`，把 target 写进函数属性 |
| `tirx/build.py:221` | 按名字取 TIR pipeline（默认 `"default"`） |
| `tirx/build.py:227` | 执行 pipeline，得到 lowering 后的 IR |
| `tirx/build.py:230` | `split_host_device_mods`，拆 host/device |
| `tirx/build.py:233` | `finalize_host_passes()` |
| `tirx/build.py:240` | `tir_to_runtime(...)` |
| `tirx/build.py:243` | 把整个函数注册成 global func `"tirx.build"` |

再往下是 `codegen_build`：

```python
# python/tvm/tirx/build.py:124-132
def codegen_build(mod, target):
    if tvm.ir.transform.PassContext.current().config.get("tirx.disable_assert", False):
        mod = tvm.tirx.transform.SkipAssert()(mod)
    build_f_name = "target.build." + target.kind.name
    bf = tvm.get_global_func(build_f_name)
    if bf is None:
        raise ValueError(f"{build_f_name} is not enabled")
    return bf(mod, target)
```

关键点：`target.build.llvm` 不是 Python 函数，而是 C++ 注册的 global func。Python 侧只负责拼名字、查表、调用；真正的 codegen 在 C++（本机没有源码，`guided`）。

调用链可以画成：

```text
tvm.build                          driver/build_module.py:31
  └─ tvm.tirx.build                tirx/build.py:157
       ├─ BindTarget               tirx/build.py:215
       ├─ pipeline(mod)            tirx/build.py:227
       ├─ split_host_device_mods   tirx/build.py:230
       ├─ finalize_host_passes     tirx/build.py:233
       └─ tir_to_runtime           tirx/build.py:135
            └─ codegen_build       tirx/build.py:124
                 └─ get_global_func("target.build.llvm")
                      └─ C++ codegen（guided）
```

## 核心概念三：路线 2——TE → PrimFunc → Schedule

`te.create_prim_func` 在 `python/tvm/te/operation.py:538`。它的函数体只有三行有效代码：

```python
# python/tvm/te/operation.py:590-592
if not isinstance(ops, list | tuple | Array):
    ops = [ops]
return _ffi_api.CreatePrimFunc(ops, index_dtype_override)
```

也就是说，TE 的 `placeholder/compute/reduce_axis` 只是**建图的前端**；`create_prim_func` 把这张 TE 图交给 C++ 的 `CreatePrimFunc`，返回一个 `tvm.tirx.function.PrimFunc`。

本机实测类型：

```text
create_prim_func -> tvm.tirx.function.PrimFunc
Schedule -> tvm.s_tir.schedule.schedule.Schedule
sch.mod -> tvm.ir.module.IRModule
SBlockRV -> SBlockRV
loops -> ['s_tir.LoopRV', 's_tir.LoopRV', 's_tir.LoopRV']
after split loops -> ['s_tir.LoopRV', 's_tir.LoopRV', 's_tir.LoopRV', 's_tir.LoopRV']
```

`Schedule` 的构造函数在 `python/tvm/s_tir/schedule/schedule.py:130`，它把工作交给 `_ffi_api.TracedSchedule`（`:178-185`）。`sch.mod` 是一个 property：

```python
# python/tvm/s_tir/schedule/schedule.py:207-210
@property
def mod(self) -> IRModule:
    """Returns the AST of the module being scheduled"""
    return _ffi_api.ScheduleGetMod(self)
```

`get_sblock` 在 `:483`，`get_loops` 在 `:508`，`split` 在 `:739`。所以“schedule 改的是 `sch.mod`”这句话在源码里是字面成立的：每个原语都通过 FFI 改 C++ 里持有的 IR，再通过 `ScheduleGetMod` 取回来。

本机实测：对 `16×16` matmul 的 `k` 轴做 `split(8)` 后，`sch.mod.script()` 的循环头从：

```python
for i, j, k in T.grid(16, 16, 16):
```

变成：

```python
for i, j, k_0, k_1 in T.grid(16, 16, 8, 2):
```

注意 `k_0` 的范围是 8、`k_1` 是 2，乘积仍是 16。这就是 `split` 的真实语义，不需要读 C++ 也能确认。

## 核心概念四：路线 3——`inspect_source()` 到底调了什么

很多人以为 `inspect_source` 在 `python/tvm/runtime/module.py`。**0.27 不是。** `tvm.runtime.Module` 确实在 `runtime/module.py:109` 定义：

```python
# python/tvm/runtime/module.py:107-110
# override the Module class in ffi.Module
@_register_object("ffi.Module")
class Module(_Module):
    """Runtime Module."""
```

但它继承自 `tvm_ffi.module.Module`，而 `inspect_source` 的方法体在 `tvm_ffi/module.py:301-314`：

```python
# site-packages/tvm_ffi/module.py:301-314
def inspect_source(self, fmt: str = "") -> str:
    """Get source code from module, if available."""
    return _ffi_api.ModuleInspectSource(self, fmt)
```

本机 MRO 实测：

```text
<class 'tvm.runtime.module.Module'> (
  <class 'tvm.runtime.module.Module'>,
  <class 'tvm_ffi.module.Module'>,
  <class 'tvm_ffi.core.Object'>,
  <class 'tvm_ffi.core.CObject'>,
  <class 'object'>
)
```

`tvm/runtime/_ffi_api.py` 的全文核心只有两行：

```python
# python/tvm/runtime/_ffi_api.py:19-22
import tvm_ffi
# Exports functions registered in runtime namespace.
tvm_ffi.init_ffi_api("runtime", __name__)
```

所以调用链是：

```text
mod.inspect_source(fmt)
  └─ tvm_ffi.module.Module.inspect_source      tvm_ffi/module.py:301
       └─ _ffi_api.ModuleInspectSource          runtime namespace
            └─ C++ ModuleNode::InspectSource    guided
```

这也是本章要反复用的一条判断：**看到 `_ffi_api`，Python 侧就到头了**。

## 底层机制：FFI 边界的四个信号

在 wheel 里读源码，最重要的是识别“这里已经到边界了”。四个信号：

1. **函数名以 `_ffi_api.` 开头**：如 `_ffi_api.TracedSchedule`、`_ffi_api.ScheduleGetMod`。
2. **`tvm.get_global_func(...)`**：按字符串查 C++ 注册函数。
3. **对象没有 Python `__module__`**：本机 `target.build.llvm` 的 `__module__` 是 `None`。
4. **栈里出现 `function.pxi` 或 C++ 路径**：如 `python/tvm_ffi/cython/function.pxi`、`D:\a\tvm\tvm\src\...`。

本机实测：

```text
tirx.build -> Function
target.build.llvm -> Function
target.build.c -> Function
relax.build -> None
--- attrs of target.build.llvm ---
__module__ None
__name__ None
__doc__ Callable wrapper around a TVM FFI function. ...
```

`relax.build` 查不到，是因为 Relax 的入口不在 global func 表里，而在 `python/tvm/relax/vm_build.py:172` 的 Python 函数 `build`。这又是一个“不要假设所有入口都同构”的例子。

## 示例一：真跑一次并记录调用链

下面的脚本用 `sys.settrace` 只记录 TVM 内部几个文件的函数调用，不修改源码：

```python
import sys, os, tvm
from tvm import te, s_tir

TRACE_FILES = ("build_module.py", "tirx\\build.py", "tirx/build.py",
               "compilation_pipeline.py", "tvm_ffi\\module.py", "tvm_ffi/module.py")
events = []

def tracer(frame, event, arg):
    if event == "call":
        fn = frame.f_code.co_filename
        if any(fn.endswith(f) for f in TRACE_FILES):
            events.append((os.path.basename(fn), frame.f_code.co_name, frame.f_lineno))
    return tracer

N = 16
A = te.placeholder((N, N), name="A")
B = te.placeholder((N, N), name="B")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
func = te.create_prim_func([A, B, C])
sch = s_tir.Schedule(func)
ko, ki = sch.split(sch.get_loops(sch.get_sblock("C"))[2], factors=[8, None])

sys.settrace(tracer)
mod = tvm.build(sch.mod, target="llvm")
src = mod.inspect_source()
sys.settrace(None)

seen = []
for e in events:
    if not seen or seen[-1] != e:
        seen.append(e)
print("--- call chain (%d unique frames) ---" % len(seen))
for f, name, line in seen:
    print(f"{f}:{line}: {name}")
print("source bytes =", len(src))
print("first line =", src.splitlines()[0])
```

本机真实输出（去重后的连续帧）：

```text
--- call chain (11 unique frames) ---
build_module.py:31: build
build.py:157: build
compilation_pipeline.py:140: get_tir_pipeline
build.py:30: split_host_device_mods
build.py:103: is_host_func
build.py:108: <lambda>
build.py:103: is_host_func
build.py:234: <dictcomp>
build.py:135: tir_to_runtime
build.py:124: codegen_build
module.py:301: inspect_source
source bytes = 71521
first line = ; ModuleID = 'TVMMod'
```

这张表比任何架构图都可靠：

- `build_module.py:31` 是兼容层；
- `build.py:157` 是真正的 build；
- `compilation_pipeline.py:140` 说明默认 pipeline 是**按名字查表**拿到的；
- `build.py:124` 之后没有 Python 帧了——下一跳就是 C++；
- `module.py:301` 是 `inspect_source`，它在 `tvm_ffi`，不在 `runtime/module.py`。

注意：这条链里**没有** `get_default_tir_pipeline`，因为 `tvm.build` 的 `pipeline` 默认是字符串 `"default"`，走 `get_tir_pipeline` 分支（`tirx/build.py:218-221`）；传 `pipeline=None` 才会走默认管线。读源码时要对“默认参数改变分支”敏感。

## 示例二：用 Select-String 找入口

在 Windows 上，`Select-String` 是最快的源码搜索工具。查 `build` 的转发：

```powershell
$p = "D:\Learning-with-Claude\.scratch\aic-env\Lib\site-packages\tvm"
Select-String -Path "$p\driver\build_module.py" -Pattern '^def build|return tvm\.tirx\.build|^def compile'
```

真实命中：

```text
build_module.py:31: def build(
build_module.py:60: return tvm.tirx.build(mod, target, pipeline)
build_module.py:72: def compile(  # pylint: disable=redefined-builtin
```

再查 build 的各个阶段：

```powershell
Select-String -Path "$p\tirx\build.py" -Pattern 'get_tir_pipeline|split_host_device_mods|codegen_build|tir_to_runtime|register_global_func'
```

真实命中：

```text
build.py:30: def split_host_device_mods(mod: IRModule) -> tuple[IRModule, dict[Target, IRModule]]:
build.py:124: def codegen_build(mod: IRModule, target: Target) -> tvm.runtime.Module:
build.py:135: def tir_to_runtime(
build.py:148: device_modules.append(codegen_build(device_mod, target))
build.py:150: mhost = codegen_build(mhost_all, target_host)
build.py:221: pipeline, finalize_host_passes, finalize_device_passes = tvm.tirx.get_tir_pipeline(pipeline)
build.py:230: host_mod, device_mod_dict = split_host_device_mods(mod)
build.py:240: return tir_to_runtime(host_mod, device_mod_dict, target_host)
build.py:243: tvm.register_global_func("tirx.build", build)
```

这就是“文件 + 行号”的证据。不要凭记忆写 `build.py:xxx`；版本一换，行号就漂。

## 示例三：`inspect_source` 的格式与一个真实报错

`inspect_source()` 不传参数时返回 LLVM IR：

```python
print(mod.inspect_source()[:120])
```

本机输出：

```text
; ModuleID = 'TVMMod'
source_filename = "TVMMod"
target datalayout = "e-m:w-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
```

支持的格式不是 target 名字。本机实测：

| `fmt` | 结果 | 首行 |
|---|---|---|
| `""` | 成功，71521 字节 | `; ModuleID = 'TVMMod'` |
| `"ll"` | 成功，71521 字节 | `; ModuleID = 'TVMMod'` |
| `"asm"` | 成功，32154 字节 | `.def @feat.00;` |
| `"s"` | 成功，32154 字节 | `.def @feat.00;` |
| `"llvm"` | **报错** | `InternalError: Do not know how to get source code with format: llvm'` |
| `"c"` | **报错** | `InternalError: Do not know how to get source code with format: c'` |

`"llvm"` 的完整报错还带了一条 C++ 路径：

```text
File "D:\a\tvm\tvm\src\target\llvm\llvm_module.cc", line 289, in
class tvm::ffi::String __cdecl tvm::codegen::LLVMModuleNode::InspectSource(...)
tvm.error.InternalError: Do not know how to get source code with format: llvm'
```

这条路径是**编译 wheel 时的源码路径**，不是本机文件。它证明了两件事：`inspect_source` 最终落到 `LLVMModuleNode::InspectSource`；C++ 不在本机，想读实现只能看 GitHub 对应 tag（`guided`）。

## 对比与取舍：Python 读到哪，C++ 读到哪

| 你想回答 | 停在哪 | 具体做法 | 代价 |
|---|---|---|---|
| API 参数/默认值/类型 | Python wrapper | `inspect.signature`、`Get-Content`、`Select-String` | 低 |
| Python 把什么交给 C++ | `_ffi_api` | 看 `_ffi_api.Xxx` 和 `get_global_func` 名字 | 低 |
| IR 被改成了什么 | IR 文本 | `script()`、`inspect_source()` | 低 |
| 调度为什么非法 | 错误诊断 + C++ | 先读 `ScheduleError` 里的 IR，再查 C++ | 中 |
| codegen 具体指令 | C++ | GitHub `src/target/...`（`guided`） | 高 |

## 踩雷集锦

1. **把 `tvm.tir` 当 0.27 的调度入口**：0.27 里调度在 `tvm.s_tir`，新 TIR 在 `tvm.tirx`。旧教程的 `te.create_schedule` 不存在。
2. **以为 wheel 里有 `src/`**：只有 `libtvm.dll` 和 Python。C++ 行号只能从错误栈/头文件/上游仓库获得。
3. **`inspect_source("llvm")`**：格式名不是 target 名。用默认、`"ll"` 或 `"asm"`。
4. **`sys.settrace` 看不到 C++**：trace 停在 `codegen_build` 是正常现象，不是 trace 写错了。
5. **把行号写死进笔记**：版本升级会漂。记录“文件 + 函数名 + 特征字符串”，行号只作当时证据。

## 进阶：再往深一层

### 1. 用 `script()` 反推 pass

先保存 lowering 前后的 `script()`，再做 diff。比读 pass 源码更快定位“哪一步改了什么”。Ch 12 的 `split/reorder` 例子就是这么做的。

### 2. 用 `get_global_func` 确认边界

```python
for name in ["tirx.build", "target.build.llvm", "target.build.c", "relax.build"]:
    print(name, tvm.get_global_func(name, allow_missing=True))
```

本机能查到前三个，查不到 `relax.build`。这能帮你判断“这个入口是 Python 还是 C++”。

## 动手练习

### 练习 1：对比 `tvm.build` 与 `tvm.compile`

用本章的 trace 脚本，把 `tvm.build(...)` 换成 `tvm.compile(...)`，记录调用链。回答：多/少了哪些帧？`tvm.compile` 在 `driver/build_module.py:72` 里如何判断走 Relax 还是 TIR？

<details>
<summary>参考思路</summary>

`compile` 先调用 `_contains_relax(mod)`（`build_module.py:63-69`）：如果 module 里有 `tvm.relax.Function`，走 `tvm.relax.build`；否则走 `tvm.tirx.build`，再包一层 `Executable`（`:104-112`）。所以 TIR 路径会多一个 `Executable` 包装，但核心 build 帧相同。
</details>

### 练习 2：找到 `Schedule.split` 的 FFI 调用

用 `Select-String` 在 `s_tir/schedule/schedule.py` 里找 `def split`，读出它调用的 `_ffi_api` 名字。再用 `tvm.get_global_func` 查这个名字是否存在。

<details>
<summary>参考思路</summary>

`split` 在 `:739`，函数体里调用 `_ffi_api.ScheduleSplit(...)`。用 `tvm.get_global_func("s_tir.schedule.ScheduleSplit", allow_missing=True)` 可以确认它是 C++ 注册函数。具体名字以你本机文件为准。
</details>

### 练习 3：解释 `inspect_source("llvm")` 的报错

跑一次 `mod.inspect_source("llvm")`，把完整栈保存下来。指出哪一行是 Python 边界，哪一行是 C++，为什么 `"ll"` 可以而 `"llvm"` 不行。

<details>
<summary>参考思路</summary>

Python 边界是 `tvm_ffi/module.py:314` 的 `_ffi_api.ModuleInspectSource(self, fmt)`；下一帧是 `function.pxi`，再下一帧是 `llvm_module.cc:289`。格式是 `LLVMModuleNode::InspectSource` 自己定义的字符串，不是 target kind。
</details>

## 本章重点整理

- 读 TVM 源码分三层：Python wrapper → FFI 边界 → C++。先确定停在哪层。
- `tvm.build` 是兼容层（`driver/build_module.py:31`），真正入口是 `tvm.tirx.build`（`tirx/build.py:157`）。
- build 的五步：BindTarget → pipeline → split host/device → finalize → `tir_to_runtime`。
- `target.build.llvm` 是 C++ global func；Python 侧只能看到 `Function`，看不到实现。
- `te.create_prim_func` 在 `te/operation.py:538`，最终调 `_ffi_api.CreatePrimFunc`。
- `s_tir.Schedule` 的 `mod` 属性是 `_ffi_api.ScheduleGetMod`；每个原语都改这份 IR。
- `inspect_source` 在 0.27 由 `tvm_ffi.module.Module` 提供（`tvm_ffi/module.py:301`），不在 `runtime/module.py`。
- 格式名是 `""`/`"ll"`/`"asm"`，不是 `"llvm"`；`"llvm"` 会抛 `InternalError`。
- 行号会漂，笔记里记“文件 + 函数名 + 特征字符串”。

## 自我检核

- [ ] 我能说出 `tvm.build` 到 `codegen_build` 的至少 5 个函数名。
- [ ] 我能解释 `target.build.llvm` 为什么在 Python 里找不到实现。
- [ ] 我能用 `Select-String` 在 30 秒内定位一个函数的定义行。
- [ ] 我知道 `sch.mod` 是 property，不是普通字段。
- [ ] 我知道 `inspect_source("llvm")` 会失败，并知道该用什么格式。
- [ ] 我能区分“错误诊断里的 C++ 路径”和“本机存在的文件”。

## 延伸阅读

- **TVM 源码（GitHub）**：读 `python/tvm/tirx/build.py`、`python/tvm/s_tir/schedule/schedule.py`、`src/target/llvm/llvm_module.cc`。学什么：build 的真实顺序与 codegen 入口。与本课关联：Ch 14 的 codegen 三条路径。
- **TVM 文档：Pass Infrastructure**：读 `tvm.transform` 与 `PassContext`。学什么：pass 怎么组合、config 怎么传。与本课关联：本章的 `tir.disable_vectorize` 例子。
- **tvm-ffi 仓库**：读 `python/tvm_ffi/module.py` 与 `function.pxi`。学什么：Python 对象如何持有 C++ 对象、global func 如何注册。与本课关联：本章的 FFI 边界。
- **Ch 12《TVM TIR 与 schedule》**：读 `split/reorder` 的 IR 变化。学什么：调度原语的语义。与本课关联：路线 2 的实测。
- **Ch 14《后端代码生成》**：读 LLVM/C/CUDA 三条路径。学什么：`target.build.*` 的差异。与本课关联：路线 1 的终点。

---

→ [Ch 29 设计取舍：TVM、TorchInductor、XLA 与 MLIR/IREE](./29-design-tradeoffs.md)
