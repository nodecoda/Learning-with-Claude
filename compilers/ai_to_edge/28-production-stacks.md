# Ch 28 生产工具链对照：ExecuTorch / TVM / IREE / ORT / TFLite

> **目标**：把前 27 章拆开讲的每一层，重新装回五个工业框架里。学完这章，你看到一个部署需求，能先判断该选哪条栈、它替你做了哪几步、又跳过了哪一步，以及这一步被跳过之后风险落在哪里。

> **环境**：本章的操作实测只用到本机已有的 PyTorch 2.9.1+cpu、LLVM 14、`aarch64-linux-gnu-gcc` 11.4、`arm-none-eabi-gcc` 10.3、QEMU 6.2。ExecuTorch、TVM、IREE、ONNX Runtime、TFLite **都没有安装在本机**，凡涉及它们的内部行为、包内目录、命令行输出，一律标「源码/文档依据」或「未在本机实测」。表格里不出现假装跑过的终端记录。

## 先建立直觉

前 27 章的链路可以压成六个格子：

```
捕获 → 图优化 → 调度 → 降级(IR) → 代码生成/选 kernel → 打包+运行时
 │        │        │        │              │                │
ATen   融合/常量    tiling   MLIR/LLVM     生成 or 调库       .pte/.so/.tflite
```

五个生产栈不是五套完全不同的科学，而是**在这六个格子里各自填了不同的答案**。有的格子它们干脆不填，交给别的东西。你做选型时真正要问的是三件事：

1. 我的模型以什么形式进入这条栈？（PyTorch 程序、ONNX 图、TFLite 模型、还是手写 IR）
2. 这条栈在哪一格停止「通用」，开始「针对算子手写」？
3. 它跳过的格子，代价由谁承担——编译器作者、算子库作者，还是你？

下面先逐个拆，最后用一张大表贯穿。

## ExecuTorch：PyTorch 官方的边缘出口

### 它是什么

ExecuTorch 是 PyTorch 官方为边缘设备做的端到端导出与运行时方案。定位很明确：**让 `nn.Module` 不经过第三方格式，直接变成一个可在手机、MCU、嵌入式 Linux 上加载的包**。

它和本课 Part 1–2 的对应关系最直接，因为入口就是你已经学过的 `torch.export`。

### 架构分层

```
PyTorch nn.Module
      │  torch.export（Dynamo 追踪 + ATen 图）
      ▼
ExportedProgram（ATen dialect）
      │  Edge dialect 转换
      ▼
Edge dialect 图（去掉训练语义、autograd、部分动态能力）
      │  分区 / delegate（XNNPACK、CoreML、Vulkan、Qualcomm……）
      ▼
若干「可在目标后端执行」的子图 + 剩余 CPU 子图
      │  序列化
      ▼
.pte 平面缓冲区（flatbuffer）包
      │
      ▼
极简 C++ 运行时（无 Python、可选无 libc 依赖）
```

这里有两个名词值得钉住，因为面试和排障都会遇到：

- **Edge dialect**：`torch.export` 出来的图仍然是 ATen 算子集，里面允许训练相关语义、允许一部分动态性。Edge dialect 是它的受限子集，目标是「算子集合小而稳定、shape 约束明确、可以直接映射到嵌入式 kernel」。这一步不是简单过滤，它会把一些算子拆成更基础的组合。
- **`.pte`**：ExecuTorch 的部署包格式，基于 FlatBuffers。选择 FlatBuffers 而不是 pickle 或 zip 的原因和 TFLite 一样：**反序列化不需要动态分配大量对象，也不需要完整的运行时类型系统**，适合没有 OS 的设备。

「源码/文档依据」的边界：上述分层、Edge dialect 与 `.pte` 的存在，是公开文档与仓库结构里的稳定事实；但**具体某版本 `.pte` 的字段布局、delegate 分区算法的细节，本章未在本机实测**，不应据此写解析器。

### 它填了哪些格子

| 格子 | ExecuTorch 的做法 | 本课对应 |
| --- | --- | --- |
| 捕获 | `torch.export`，直接复用 ATen 图 | Ch 3–4 |
| 图优化 | 一部分在导出前后做，一部分交给 delegate | Ch 7 |
| 调度 | 主要交给后端（XNNPACK 等）或手写 kernel | Ch 8 |
| 降级 | Edge dialect → 后端专用表示 | Ch 10、12 |
| 代码生成 | **不生成通用循环**；调库或调 delegate | Ch 23 |
| 打包/运行时 | `.pte` + 极简 C++ runtime | Ch 24 |

它跳过的关键一步是**通用循环级代码生成**。ExecuTorch 不自带一个「像 LLVM 那样把任意张量表达式变成循环再优化」的后端；它的性能来自 delegate（预编译 kernel 库）和少量手写 kernel。这正好是 Ch 23 的结论：算子数量有限、形状相对固定时，手写/调库比通用代码生成更划算。

### 什么时候选它

- 团队已经在 PyTorch 生态里，模型是 `nn.Module`，不想维护第二套图定义。
- 目标是手机或带 OS 的边缘设备，能接受包体积换开发速度。
- 需要官方支持的量化与 delegate 路线（XNNPACK/CoreML 等）。

### 什么时候不选它

- 目标是没有 OS、Flash 只有几百 KB 的 MCU：运行时与 delegate 的体积、以及算子覆盖都要重新评估（**未在本机实测**，需要按目标版本核对支持的算子集）。它的 `backends/cortex_m/ops/` 里确实是手写的量化算子（如 `op_quantized_add.cpp`、`op_quantized_batch_matmul.cpp`，GitHub API 列目录实测），这再次印证「MCU 路线靠手写 kernel」。
- 你需要对调度做非常规实验（自定义 tiling、自定义内存布局）：通用栈的扩展点通常不够深，本课的 MLIR/LLVM 路线更合适。

## TVM：计算与调度分离的元老

### 它是什么

TVM 的核心贡献不是「又一个编译器」，而是把**算什么（compute）**和**怎么算（schedule）**拆成两个可独立表达、可独立搜索的东西。这个思想在 Ch 8 已经用过：同一个 matmul 语义，配不同的 tiling/vectorize 调度，生成的循环结构完全不同。

### 架构分层

```
前端（Relax / 旧版 Relay 图，或框架导入）
      │  图级优化：算子融合、常量折叠、layout 变换
      ▼
图级 IR（Relax；旧版为 Relay）→ 每个算子展开为 TE（Tensor Expression）
      │  te 调度原语：split / reorder / tile / vectorize / parallel
      ▼
TIR（张量级循环 IR，显式循环与 buffer）
      │  AutoTVM / Ansor 搜索调度参数
      ▼
后端：LLVM（CPU）、CUDA/ROCm（GPU）、BYOC（自研加速器）
      │
      ▼
目标代码 + TVM runtime（Graph Executor / AOT Executor）
```

三个名词要分清：

- **Relay**：图级 IR。算子之间是数据流关系，适合做融合和常量折叠。对应本课 Ch 7 的图优化层。
- **TIR**：张量级 IR。到了这一层，循环、buffer、索引都显式写出来了。对应本课 Ch 10 的 `linalg → scf.for` 降级结果。
- **TE + schedule**：TVM 独有的「先描述计算，再描述循环变换」的 DSL。`te.compute` 只说「输出每个元素等于什么」，`te.create_schedule` 再说怎么切分循环。

版本提示（源码依据，实测方式为 GitHub API 列目录，未编译）：TVM 主分支现在的目录是 `src/relax/`、`src/s_tir/`、`src/te/`，**没有 `src/relay/` 与 `src/tir/`**。Relax 是新一代图级 IR，`s_tir` 是可调度的 TIR。也就是说：本章讲的 Relay → TIR 是**架构思想**，在新代码里对应 Relax → s_tir；读源码时按新目录找，别照旧路径翻。AutoTVM / Ansor / MetaSchedule 的具体 API 同样随版本变化。

AutoTVM / Ansor 是**调度参数搜索**：给定一组候选 tile 大小、向量化因子、并行策略，在目标硬件上实测，选最快的一组。注意这不是「自动写算子」，搜索空间仍由人给出的 schedule 模板（AutoTVM）或搜索规则（Ansor）界定。

「源码/文档依据」的边界：Relay/TIR/te 的分层与职责是公开且稳定的；**AutoTVM 与 Ansor 在当前版本中的具体 API 名称和默认搜索成本，本章未在本机实测**，不同版本差异较大。

### 它填了哪些格子

| 格子 | TVM 的做法 | 本课对应 |
| --- | --- | --- |
| 捕获 | 多前端导入；不绑定 PyTorch | Ch 3 |
| 图优化 | Relay 上做融合/折叠/layout | Ch 7 |
| 调度 | **核心卖点**：schedule + 自动搜索 | Ch 8 |
| 降级 | Relax/Relay → TE → TIR → 目标 IR | Ch 10、12 |
| 代码生成 | 通用：LLVM/CUDA 后端真的生成代码 | Ch 9、13 |
| 运行时 | Graph Executor 或 AOT | Ch 24 |

TVM 是五个里**唯一把「调度搜索」当作一等公民**的。代价是：搜索需要目标硬件在环，编译时间可能从秒级涨到小时级；而且搜索出来的 schedule 是硬件相关的，换芯片要重搜。

### 什么时候选它

- 你要在非主流硬件（自研 NPU、特殊 DSP）上榨性能，且愿意写 BYOC 后端。
- 你有明确的调度实验需求：不同 tiling、不同向量化宽度、不同并行策略。
- 你能接受「编译期换运行期性能」的长编译。

### 什么时候不选它

- 团队没有编译器工程师，目标只是「把模型跑起来」。
- 模型算子极多且更新极快：每换一次模型都要重新调 schedule，维护成本高。

## IREE：全 MLIR 路线的工业版

### 它是什么

IREE（Intermediate Representation Execution Environment）把整条编译链都放在 MLIR 里，从高层图方言一路降到硬件抽象，再由 LLVM/SPIR-V 出机器码。它是本课 Ch 10、Ch 12 的「工业级完整版」：你在课上手工敲的 `linalg → scf → llvm`，IREE 用一套方言和 pass pipeline 自动化了。

### 架构分层

```
输入（StableHLO / TOSA / linalg 等）
      │
      ▼
flow 方言        ← 图级：tensor 上的数据流、dispatch 划分
      │
      ▼
stream 方言      ← 调度级：异步执行、依赖、内存空间分配
      │
      ▼
hal 方言         ← 硬件抽象：buffer、executable、device 接口
      │
      ▼
LLVM IR / SPIR-V
      │
      ▼
目标代码（CPU：LLVM；GPU：SPIR-V/Vulkan；其他后端）
```

三个方言的职责，用一句话各说清：

- **`flow`**：还是「张量」的世界。它决定哪些算子合成一个 dispatch（可以理解为一次 kernel 启动），以及数据在 dispatch 之间怎么流动。
- **`stream`**：开始关心「什么时候执行」。异步、依赖边、以及 buffer 在哪个内存空间（host/device）——这是本课 Ch 22 内存规划的工业化表达。
- **`hal`**：只关心「硬件能做什么」。分配 buffer、提交 executable、同步。到了这一层，张量的语义已经不重要了。

这个分层的好处是**每一层都可以单独测试**：你可以只跑 `flow` 的 pass 看 dispatch 划分对不对，不必等整条链跑完。「源码/文档依据」的边界：方言名称与职责是公开文档中的稳定内容；**具体 pass 名称、pipeline 顺序、以及某版本是否默认启用某后端，本章未在本机实测**。

### 它填了哪些格子

| 格子 | IREE 的做法 | 本课对应 |
| --- | --- | --- |
| 捕获 | 接受多种高层方言，不绑定单一框架 | Ch 3 |
| 图优化 | `flow` 层做 dispatch 划分与融合 | Ch 7 |
| 调度 | `stream` 层做异步与依赖调度 | Ch 8 |
| 降级 | flow → stream → hal → LLVM | Ch 10、12 |
| 代码生成 | LLVM/SPIR-V 通用生成 | Ch 9、13 |
| 运行时 | HAL runtime + 模块 | Ch 24 |

IREE 几乎不跳过任何格子，代价是**栈很深**。出问题时你要判断是 `flow` 划分错了、`stream` 调度错了、还是 `hal` 后端不支持——这也是它调试工具（IR dump、pass timing）存在的原因。

### 什么时候选它

- 目标是「一个编译器覆盖 CPU + GPU + 多种加速器」，不想为每个后端重写一套。
- 团队熟悉 MLIR，愿意读 IR 定位问题。
- 需要异步执行与显式内存空间管理（移动端 GPU、多设备）。

### 什么时候不选它

- 目标是最小 MCU：完整 HAL runtime 的体积与依赖通常超出预算（**未在本机实测**，需按目标配置裁剪）。
- 你的场景只是一个固定模型跑在一个固定 CPU 上：栈越深，构建与调试成本越高。

## ONNX Runtime：图优化 + 插件化 Execution Provider

### 它是什么

ONNX Runtime（ORT）的定位和前三个不同：它**主要不是编译器**，而是一个「以 ONNX 图为输入、用预编译 kernel 执行」的推理引擎。图优化在 ORT 层做，算子实现以 kernel 库为主，硬件适配通过 Execution Provider（EP）插件化。

### 架构分层

```
ONNX 模型（protobuf）
      │  图优化：常量折叠、算子融合、layout 转换、量化重写
      ▼
优化后的 ONNX 图
      │  分区：按 EP 能力把子图分配给不同后端
      ▼
Execution Provider（CPU / CUDA / TensorRT / OpenVINO / NNAPI / QNN ……）
      │  每个 EP 自带预编译 kernel 或调用厂商库
      ▼
执行
```

关键设计是 **EP 的能力声明 + 分区**：每个 EP 说「这些算子我能跑」，ORT 把图切成若干段，不能跑的段落回 CPU。这和 ExecuTorch 的 delegate 分区是同一类思想，只是 ORT 的边界更偏「算子级」而不是「子图级重写」。

「源码/文档依据」的边界：EP 机制、图优化 pass 的存在是公开事实；**具体某个 EP 支持哪些算子、某版本的融合规则，本章未在本机实测**，以目标版本的 operator support 文档为准。

### 它填了哪些格子

| 格子 | ORT 的做法 | 本课对应 |
| --- | --- | --- |
| 捕获 | 不捕获；输入就是 ONNX 图 | Ch 3（格式对照） |
| 图优化 | ORT 自己的 pass | Ch 7 |
| 调度 | **基本不做循环级调度**；交给 kernel 作者 | Ch 8（缺位） |
| 降级 | 不降级到通用 IR；直接映射到 kernel | Ch 10（缺位） |
| 代码生成 | **不生成**；调预编译 kernel 库 | Ch 23 |
| 运行时 | 完整运行时 + EP 插件 | Ch 24 |

ORT 跳过的是**通用循环生成与调度**。它把这一格的答案写成「kernel 库 + 厂商库」。这在算子稳定的推理场景非常划算（kernel 可以针对每种 shape 手调），但遇到新算子或奇怪 shape，性能会掉到 fallback kernel。

### 什么时候选它

- 模型已经导出成 ONNX，或者团队本来就用 ONNX 作为交换格式。
- 目标平台有成熟 EP（x86 CPU、CUDA、OpenVINO、NNAPI）。
- 你要的是「稳定跑起来 + 可预测」，不是极限性能。

### 什么时候不选它

- 需要极致的算子融合或自定义调度：ORT 的扩展点是 EP 和自定义算子，不是通用 IR 变换。
- 极小设备：完整运行时与 ONNX protobuf 解析的体积不适合几百 KB 的 MCU。

## TFLite：FlatBuffer + 解释器 + delegate

### 它是什么

TensorFlow Lite 的部署形态很朴素：**一个 FlatBuffer 模型文件 + 一个解释器 + 可选 delegate**。它的历史位置是「把 TensorFlow 图压到手机」，所以设计上极其重视两件事：包体积、量化。

### 架构分层

```
TensorFlow / Keras 模型
      │  转换器：图冻结、常量折叠、量化、算子融合
      ▼
.tflite（FlatBuffer）
      │  解释器加载：直接 mmap，无需反序列化成对象图
      ▼
解释器按算子逐个 dispatch
      │  可选 delegate：
      │    XNNPACK（CPU 浮点/量化）
      │    GPU delegate
      │    NNAPI（Android）
      │    CMSIS-NN（Cortex-M）
      ▼
执行
```

三个设计点值得记住：

1. **FlatBuffer 而不是 protobuf 对象树**：模型可以直接映射进内存，字段按 offset 访问，不需要解析阶段的大量分配。这对启动时间和内存受限设备都关键。
2. **解释器模型**：算子不是编译成一条融合后的循环，而是运行时按图节点依次调用 kernel。融合主要发生在转换期（把能合的算子合成一个复合算子），运行期不再做循环级优化。
3. **量化是一等公民**：`int8` 量化不是附加功能，而是转换器的核心路径之一；大量 kernel 直接以 `int8` 为主类型实现。

「源码/文档依据」的边界：FlatBuffer 格式、解释器 + delegate 的结构、量化路径都是公开事实；**某个 `.tflite` 文件的 schema 版本、以及 CMSIS-NN delegate 在某 Cortex-M 型号上的算子覆盖，本章未在本机实测**。

### 它填了哪些格子

| 格子 | TFLite 的做法 | 本课对应 |
| --- | --- | --- |
| 捕获 | TensorFlow/Keras 转换器 | Ch 3 |
| 图优化 | 转换期融合 + 量化重写 | Ch 7、11 |
| 调度 | **不做通用调度**；kernel 内部已手调 | Ch 8（缺位） |
| 降级 | 不降级到通用 IR | Ch 10（缺位） |
| 代码生成 | **不生成**；手写 kernel + delegate | Ch 23 |
| 运行时 | 解释器 + FlatBuffer | Ch 24 |

TFLite 跳过的格子最多：它几乎完全放弃了通用循环生成与调度。这正是它在小设备上体积可控的原因，也是它遇到新算子时必须等官方或自己写 kernel 的原因。

### 什么时候选它

- 目标 Android/嵌入式 Linux，且模型以卷积类为主（kernel 成熟）。
- 量化是硬需求，希望走最成熟的 `int8` 路径。
- 需要极小的运行时与快速冷启动。

### 什么时候不选它

- 模型有大量新算子或动态控制流：解释器 + 固定 kernel 的组合不擅长。
- 需要深度自定义调度：扩展点是自定义算子/delegate，不是 IR pass。

## 一张表贯穿五个栈

下表把六个格子逐列填满。**「跳过」表示该栈没有通用实现，而是用别的手段替代**，不是「没做」。

| 维度 | ExecuTorch | TVM | IREE | ONNX Runtime | TFLite |
| --- | --- | --- | --- | --- | --- |
| 前端捕获 | `torch.export`（ATen 图） | 多前端导入（Relay/Relax） | 多方言输入（StableHLO/TOSA/linalg） | 直接吃 ONNX 图 | TF/Keras 转换器 |
| 中间表示层级 | ATen → Edge dialect → 后端 | Relax/Relay → TE → TIR（新版 `s_tir`） | flow → stream → hal | ONNX 图（优化后） | TFLite FlatBuffer 图 |
| 是否有 MLIR | 否（以 ATen / Edge dialect 为主） | 否（自有 Relax/s_tir） | **全栈 MLIR** | 否（自有图 IR） | 否（FlatBuffer 图） |
| 代码生成策略 | 调库 / delegate 为主 | 生成（LLVM/CUDA）+ 可调库 | 生成（LLVM/SPIR-V） | 调预编译 kernel | 调手写 kernel / delegate |
| 内存规划 | 运行时 + delegate 管理 | Graph/AOT executor，TIR 显式 buffer | `stream`/`hal` 显式分配 | EP 与 arena 管理 | 解释器 arena + tensor 复用 |
| 目标后端覆盖 | 手机 CPU/GPU/NPU + 部分 MCU | CPU/GPU/自研（BYOC） | CPU/GPU/多加速器 | 依赖 EP 生态 | Android/iOS/MCU（CMSIS-NN） |
| 包格式 | `.pte`（FlatBuffers） | 目标相关（so/obj/源码） | VM 模块（`.vmfb`） | `.onnx` + EP 库 | `.tflite`（FlatBuffers） |
| 运行时体积量级 | 小（可裁剪，**未实测**） | 中（Graph Executor 偏大，AOT 可小） | 中到大（HAL runtime，**未实测**） | 中大（完整引擎，**未实测**） | 小（解释器，**未实测**） |
| 调试工具 | 导出诊断、delegate 分区日志 | IR dump、schedule 可视化、profiler | pass timing、IR dump、profiler | EP 分区日志、profiler | 转换日志、delegate 回退报告 |
| 许可证 | BSD 三条款（`LICENSE` 逐条核对，GitHub API 报 `NOASSERTION`） | Apache-2.0（GitHub API） | Apache-2.0（GitHub API） | MIT（GitHub API） | Apache-2.0（覆盖 `tensorflow/lite`） |

体积那一行刻意只写量级、并全部标「未实测」：运行时体积高度依赖裁剪配置和链接方式，任何具体 KB 数字离开构建命令都没有意义。本课能给你的是**测量方法**（Ch 26 的 `size`、`nm --size-sort`），不是别人的数字。

许可证一行的「GitHub API」指 `api.github.com/repos/<owner>/<repo>` 返回的 `license.spdx_id`（实测查询）。ExecuTorch 返回 `NOASSERTION`，所以我改用 `LICENSE` 原文核对：它的正文是「Redistribution and use in source and binary forms」加三条款，属 BSD 三条款；但多机构版权声明让自动识别失败——**自动工具报不出来时，回原文读**，这是同类问题的一般解法。

## 把每个框架映射回本课链路

```
                    Ch3-5      Ch7       Ch8      Ch10/12    Ch23       Ch24
捕获 ────────────────┼─────────┼─────────┼─────────┼─────────┼─────────┼──►
                     │         │         │         │         │         │
ExecuTorch      torch.export  部分     delegate   Edge→后端   调库     .pte+runtime
TVM             多前端       Relay     schedule   Relay→TIR   生成     Graph/AOT
IREE            多方言       flow      stream     flow→hal    生成     HAL runtime
ORT             ONNX 输入    ORT pass   ✗         ✗          调库     引擎+EP
TFLite          转换器       转换期     ✗         ✗          手写kernel 解释器
```

「✗」不是批评，是设计选择：**它们把复杂度从编译器移到了 kernel 库**。你要评估的是这笔交易在你的场景划不划算。

一个具体的判断例子：本课 Final 的 MLP 只有 `linear/relu/linear` 三个算子，形状完全固定。按 Ch 23 的判据（算子数量少 × 形状稳定 × 体积预算紧），**手写循环 + 手写 kernel 是最优解**，五个生产栈都不如你自己写的 176 字节 `mlp_forward`（Cortex-M3 `-O2` 实测 `.text` 176 B，见 Final）。反过来，如果你的模型有 200 个算子、每周更新，手写就是灾难。

## 本机能做与不能做的实测

本机没有安装五个框架，这一点必须说清楚。能做的只有环境与工具链证明：

```bash
/home/ubuntu/.venvs/learn/bin/python -c "import torch; print(torch.__version__)"
```

实测输出：

```
2.9.1+cpu
```

这条命令**只证明 PyTorch 环境存在**，不证明任何框架行为。下面这些我没有跑，也不会贴输出：

- `executorch`、`tvm`、`iree`、`onnxruntime`、`tflite_runtime`、`tensorflow`：`ModuleNotFoundError`（实测，见下方命令）。这只能说明本机没装，不能说明它们的 API。

```bash
for m in executorch tvm iree onnxruntime tflite_runtime tensorflow; do
  /home/ubuntu/.venvs/learn/bin/python -c "import $m" 2>&1 | tail -1
done
```

实测输出（每条都是 `ModuleNotFoundError`）：

```
ModuleNotFoundError: No module named 'executorch'
ModuleNotFoundError: No module named 'tvm'
ModuleNotFoundError: No module named 'iree'
ModuleNotFoundError: No module named 'onnxruntime'
ModuleNotFoundError: No module named 'tflite_runtime'
ModuleNotFoundError: No module named 'tensorflow'
```

想真正验证某个栈，最短路径是：

1. 在独立 venv 里装官方包（不要把五个装进同一个环境，依赖会打架）。
2. 用同一个 784→64→10 MLP 导出，记录包体积。
3. 在目标设备或 QEMU 上跑同一个输入，和 PyTorch 参考输出比 `max_abs`。
4. 用 Ch 26 的方法量延迟，用 `size` 量体积。

这套对照实验是本课 Final 的延伸；Final 里你会在**自己写的**两条后端上完成它，不依赖任何框架。

## 踩雷集锦

1. **把「有 MLIR」当成「更好」**：ORT 和 TFLite 没有全栈 MLIR，但在手机推理上非常能打。IR 只是手段。
2. **以为 ExecuTorch 会替你生成 kernel**：它的性能来自 delegate 与手写 kernel；Edge dialect 只保证「能表达」。
3. **以为 TVM 的 AutoTVM 是万能自动调优**：搜索空间由模板/规则界定，换硬件要重搜，编译成本真实存在。
4. **把 ONNX 当运行时**：ONNX 是格式，ORT 才是执行引擎；「导出成功」不等于「目标 EP 支持」。
5. **以为 TFLite 的 int8 一定更快**：在支持 `int8` SIMD 的 CPU 上通常更快；在没有相应指令的目标上，反量化开销可能吃掉收益（**未实测**，必须按目标实测）。
6. **跨栈比体积不看裁剪配置**：一个「1 MB 运行时」可能开了全部 delegate，另一个「200 KB」可能裁到只剩 CPU。数字必须带构建命令。

## 本章重点整理

- 五个栈的差异不在「谁更先进」，而在**六个格子里各自填了什么、跳过了什么**。
- ExecuTorch 复用 `torch.export`，靠 Edge dialect + delegate + `.pte`；跳过了通用循环生成。
- TVM 的核心是 compute/schedule 分离与调度搜索；是唯一把调度当一等公民的。
- IREE 是全 MLIR 路线，`flow → stream → hal`，几乎不跳格子，代价是栈深。
- ORT 是「ONNX 图 + EP 插件 + kernel 库」，跳过循环级调度与通用代码生成。
- TFLite 是「FlatBuffer + 解释器 + delegate」，跳过最多，换来的小体积与量化成熟度。
- 选型先问：输入形式、算子规模与稳定性、体积预算、团队是否读得动 IR。

## 自我检核

- [ ] 不看表，能说出 ExecuTorch 的 Edge dialect 和 `.pte` 各解决什么问题
- [ ] 能解释 TVM 的 Relay、TIR、te 三者分别在哪一层
- [ ] 能说出 IREE 的 `flow`、`stream`、`hal` 各自关心的对象
- [ ] 能解释 ORT 的 Execution Provider 为什么是「插件」而不是「后端编译器」
- [ ] 能说出 TFLite 为什么用 FlatBuffer，以及解释器模型放弃了什么
- [ ] 给定「3 个算子、形状固定、Flash 128 KB」的需求，能选出手写路线并说明理由
- [ ] 能指出五个栈中哪些跳过通用循环生成，以及它们用什么替代

## 延伸阅读

### 官方文档

- **[ExecuTorch 文档](https://docs.pytorch.org/executorch/stable/index.html)**
  - **读哪里**：`Exporting to ExecuTorch` 与 `Runtime` 两节
  - **为什么值得读**：Edge dialect 的算子集、delegate 分区、`.pte` 的官方定义；读完再回看本章的分层图
- **[Apache TVM 文档](https://tvm.apache.org/docs/)**
  - **读哪里**：`Relay`、`TensorIR`、`Schedule` 三部分，以及 `AutoTVM`/`Ansor` 的教程
  - **和本章的关联**：compute/schedule 分离的第一手说明，对应 Ch 8
- **[IREE 文档](https://iree.dev/)**
  - **读哪里**：`Guides` 里的编译流程，以及 `Reference` 中的方言说明
  - **为什么值得读**：`flow`/`stream`/`hal` 的职责边界，本章只给了骨架
- **[ONNX Runtime 文档](https://onnxruntime.ai/docs/)**
  - **读哪里**：`Execution Providers` 与 `Graph Optimizations`
  - **和本章的关联**：EP 分区机制与 ORT 的图优化清单
- **[TensorFlow Lite 源码目录](https://github.com/tensorflow/tensorflow/tree/master/tensorflow/lite)**
  - **读哪里**：`schema.fbs`（FlatBuffer 定义）、`delegates/`、`kernels/`
  - **和本章的关联**：解释器模型与量化一等公民的设计；官方文档站从本机网络不可达（实测 `curl` 返回 000），故以仓库为准

### 源码依据

- **[ExecuTorch 仓库](https://github.com/pytorch/executorch)**
  - **读哪里**：`backends/` 下的 delegate 目录、`runtime/` 的 executor
  - **为什么值得读**：想看「一个 delegate 到底注册了什么」时，这里比文档具体
- **[TVM 仓库](https://github.com/apache/tvm)**
  - **读哪里**：`src/relax/`（图级）、`src/s_tir/`（可调度 TIR）、`src/te/`、`python/tvm/te/`
  - **为什么值得读**：当前主分支已无 `src/relay/` 与 `src/tir/`（GitHub API 实测）；这一条本身就是「版本会漂移」的例子
- **[IREE 仓库](https://github.com/iree-org/iree)**
  - **读哪里**：`compiler/src/iree/compiler/Dialect/` 下的 `Flow`、`Stream`、`HAL`
  - **为什么值得读**：方言定义与 pass 目录就是本章分层图的展开

### 论文

- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning](https://www.usenix.org/conference/osdi18/presentation/chen)** — Chen et al., OSDI 2018
  - **核心贡献**：compute/schedule 分离与自动调优的系统化论述
  - **读哪里**：第 4 节（调度）与第 5 节（自动调优）
- **[Glow: Graph Lowering Compiler Techniques for Neural Networks](https://arxiv.org/abs/1805.00907)** — Rotem et al., 2018
  - **核心贡献**：图级 lowering 与内存规划
  - **和本章的关联**：理解 ORT/TFLite 这类「图优化 + kernel」路线的理论背景
- **[MLIR: Scaling Compiler Infrastructure for Domain Specific Computation](https://arxiv.org/abs/2002.11054)** — Lattner et al., 2021
  - **读哪里**：方言分层与 progressive lowering 的动机
  - **和本章的关联**：IREE 全栈 MLIR 路线的方法论依据

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon
  - **读哪几章**：第 8–10 章（指令选择、寄存器分配、调度）
  - **注意**：书里没有 MLIR 与深度学习，但「为什么分层」的底层逻辑仍适用

五个栈看完了。下一章不再问「它们怎么做」，而是问「你的约束下该选哪一种做法」——把选择写成可查的决策表。

→ [Ch 29 取舍与决策表](./29-tradeoffs.md)
