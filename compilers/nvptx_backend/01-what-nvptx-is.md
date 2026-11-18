# Ch 1 — NVPTX 的定位：PTX 发射器，不是机器码后端

> **目标**：搞清 NVPTX 在 LLVM 里的职责边界——它做了哪些机器层工作、刻意不做哪些、为什么。学完你能判断一个「GPU 后端」缺什么。

## 为什么需要这个？

你熟悉的后端（X86、RISC-V）最终要产出可执行的目标文件：选指令、分配物理寄存器、排指令、编码成字节、写 ELF。NVPTX **只做到「打印 PTX 文字」为止**。如果你用 CPU 后端的框架去读它，会不断产生「这里怎么少了」「这算不算 bug」的错觉。

更实际的是：自研 GPU 的编译器团队迟早要回答「我们做 LLVM target，还是做 PTX 消费者」。这个选择的分水岭，就在 NVPTX 做了/没做哪些事。

## 先建立直觉

把 NVPTX 想成一位**翻译官**，不是工厂厂长：

```
LLVM IR  ──►  [ SelectionDAG / ISel ]  ──►  MachineInstr  ──►  PTX 文字
                     ↑                          ↑                  │
              这层做语义合法化            这层只保留"虚拟寄存器"     │
                                                                    ▼
                                                              ptxas（厂长）
                                                         分配物理寄存器、排程、
                                                         选真正指令、编码 SASS
```

翻译官负责「意思不能错、结构要合理」；厂长负责「怎么在具体产线上高效生产」。翻译官不碰产线参数，所以同一份稿子能交给不同代的工厂。

## 一、证据：三处「刻意不做」

### 1. 没有寄存器分配器

`NVPTXTargetMachine.cpp` 里，`createTargetRegisterAllocator` 直接返回空：

```cpp
FunctionPass *NVPTXPassConfig::createTargetRegisterAllocator(bool) {
  return nullptr; // No reg alloc
}
```

`addOptimizedRegAlloc()` 里加的也只是 PHI 消除、TwoAddress、coalescing、pre-RA 调度、栈槽着色——**没有任何 allocator**。原因很简单：PTX 的虚拟寄存器数量不设上限，分配这件事在 PTX 层没有意义（Ch 11 详述）。

### 2. 寄存器类只是「占位」

`NVPTXRegisterInfo.td` 里每个类只定义了 5 个物理寄存器，注释写得很坦白：

```tablegen
// We use virtual registers, but define a few physical registers here to keep
// SDAG and the MachineInstr layers happy.
foreach i = 0...4 in {
  def R#i  : NVPTXReg<"%r"#i>;  // 32-bit
}
```

这些 `%r0..%r4` 不是硬件资源，只是让 SelectionDAG 和 MachineInstr 的数据结构能成立。

### 3. 没有 object emission

试一下：

```bash
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -O2 -filetype=obj add.ll -o add.o
# llc: error: createMCCodeEmitter failed
```

没有 MC code emitter，所以产不出 `.o`。PTX 是文本，由 `ptxas` 接手。

## 二、它还刻意省掉了什么

| 机器层工作 | X86 / RISC-V | NVPTX | 谁补上 |
|---|---|---|---|
| 物理寄存器分配 | 后端 | **无** | ptxas |
| 指令调度（真实时序） | 后端 | 仅 pre-RA，无 SchedModel | ptxas |
| 指令编码 / ELF | 后端 | **无** | ptxas |
| 栈帧 | 后端 | 有，但只是 `.local` 声明 | ptxas 分配地址 |
| 分支放松 / 延迟槽 | 后端 | 无 | ptxas |
| occupancy / 寄存器压力 | 部分后端有 | **无** | 无人（需编译器上层补） |
| divergence 分析 | 无通用框架 | **无** | 需自建 |
| shared memory 分配/着色 | N/A | **无** | 需自建 |

最后三行是关键：**它们在 LLVM 里根本不存在，也没有下家**。Triton、CUTLASS 这些项目在 LLVM 之上自己补，正是因为这个空白。

## 三、那 NVPTX 到底做了什么？

别误会成「它什么也没干」。它的工作量集中在前半段和「契约翻译」：

- **IR 语义修正**：把 generic 指针、聚合参数、alloca、不可达块改写成 PTX 能表达的形式（Ch 7）。
- **ABI**：kernel 参数放 `.param` 空间，device 函数用 `.func` + 返回参数槽（Ch 4、8）。
- **类型合法化**：PTX 没有 i8 算术、i1 只能是 predicate（Ch 9）。
- **指令选择**：`mad.lo`、`mul.wide`、`cvta`、`setp`、`selp` 等模式（Ch 10）。
- **文本发射**：`.version`、`.target`、`.entry`、`.reg` 编号、`.shared` 提升（Ch 15）。

一句话：**它把「LLVM 能表达的语义」压成「PTX 能表达的语义」**。这是一次真正的翻译，不是简单的 `printf`。

## 四、三种「CUDA 兼容」路线

| 路线 | 接入点 | 优点 | 代价 | 代表 |
|---|---|---|---|---|
| A. 吃 PTX | 自家汇编器/后端解析 `.ptx` | 兼容面最稳，nvcc 前端全部复用 | 要完整实现 PTX 语义 + 优化 | ZLUDA、SCALE、Vortex |
| B. 接 NVVM IR | 在 LLVM 里加自家 target | 复用 LLVM 优化器，起步快 | 要跟 NVVM 方言/内建函数同步 | 多数自研 GPU 早期 |
| C. 自己写前端 | 直接解析 CUDA C++ | 完全可控 | 工作量最大，生态兼容最难 | 少见 |

NVPTX 是路线 B 的「参照实现」：它证明 LLVM 能产出语义正确的 PTX。你如果做路线 B，把 `NVPTXISelLowering` 当作「GPU 语义清单」来读；做路线 A，则把它当作「PTX 语义的生产者」来读，反推消费者需要支持什么。

## 五、这意味着 `-O2` 优化了什么？

`llc -O2` 的优化几乎都发生在 **IR 层和 DAG 层**：

- IR：EarlyCSE/GVN、LoadStoreVectorizer、SROA、FMA 折叠、参数前送（Ch 7）。
- DAG：合法化、combine、模式匹配（Ch 9、10）。
- 机器层：pre-RA 调度、coalescing、栈槽着色（Ch 13）。

它**不会**替你做的：寄存器压力控制、occupancy 调优、shared memory swizzle、warp 级流水。这些正是 `ptxas`（部分）和上层框架（部分）的工作。

## 踩雷集锦

1. **以为 `llc -O2` 的 PTX 越短越好**：PTX 短不代表 SASS 快；`ptxas` 还会重排、合并、展开。判断性能必须看 SASS 和 profiling（Ch 16）。
2. **以为 `.reg` 少就是寄存器压力小**：`.reg` 是虚拟寄存器计数，物理压力看 `ptxas -v` / `cuobjdump -res-usage`。
3. **在 NVPTX 上找 `-regalloc=greedy` 的效果**：没有 allocator，这个开关对它没有意义。
4. **拿 `-filetype=obj` 产出的空文件当成功**：命令会直接失败并返回非零；不要被脚本里的 `|| true` 骗了。

## 进阶：如果自研 GPU 要「完整后端」

你需要在 NVPTX 的基础上补四类东西，这也是 Ch 17 的清单：

1. **真实 ISA 编码**（MC + object writer）。
2. **occupancy 感知的寄存器分配**：寄存器数直接决定能驻留多少 warp，目标函数不是「spill 最少」而是「吞吐最大」。
3. **GPU 专属优化**：divergence/reconvergence、shared memory 分配与 swizzle、异步拷贝流水、MMA fragment layout。
4. **调度模型**：in-order + scoreboard + 双发射，和 CPU 的 OoO 模型完全不同。

## 动手练习

1. 用 `grep -rn "createTargetRegisterAllocator" llvm/lib/Target/` 对比 NVPTX 与 X86/RISC-V 的实现差异。
2. 跑一次 `-filetype=obj`，确认失败信息；再跑 `-filetype=asm`，确认成功。
3. 在 `NVPTXInstrInfo.td` 里搜 `SchedMachineModel`，确认它不存在；想想为什么 pre-RA 调度仍然能跑。

## 本章重点整理

- NVPTX 是「PTX 发射器」：语义翻译完整，机器层工作大量留白。
- 没有寄存器分配、没有 SchedModel、没有 object emission，是设计选择，不是缺陷。
- `ptxas` 是真正的机器码后端；occupancy/divergence/shared 分配则无人负责。
- 三种 CUDA 兼容路线的分水岭，就在你选择复用哪一层。

## 自我检核

- [ ] 能说出 NVPTX 刻意不做的三件事，以及各自的下家
- [ ] 能解释「虚拟寄存器无限」为什么让 regalloc 失去意义
- [ ] 能对比三条 CUDA 兼容路线的代价
- [ ] 能判断一个 GPU 后端缺的是「翻译」还是「优化」

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方
  - 读「The SelectionDAG class」与「Machine code description classes」两节，把 NVPTX 的留白对应到标准后端阶段。
- **[PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)** — NVIDIA
  - 读「Program State Spaces」与「Registers」两节；理解为什么 PTX 允许无限虚拟寄存器。
- **[ZLUDA](https://github.com/vosen/ZLUDA)** — Andrzej Janik
  - 看它如何消费 PTX/cubin；对照本章的三条路线，判断它属于哪一类、代价落在哪。

接下来进入 PTX 契约本身。先看清 PTX 有哪些类型和状态空间，才能理解后端为什么要做那些「奇怪」的合法化。

→ [Ch 2 PTX 契约（一）：虚拟 ISA、类型与状态空间](./02-ptx-contract-isa.md)
