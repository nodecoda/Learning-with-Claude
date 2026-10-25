# Ch 04 TableGen 语言：把指令写成可生成的数据

> 目标：把 `RISCVInstrInfo.td` 从“看不懂的 DSL”降成四件事：记录、字段、继承、覆盖。读完本章，你看到 `def ADD : ALU_rr<...>` 时，能说出它最终变成哪些记录、哪些字段、哪些生成物，以及 `let` 改的到底是哪一层。

> 环境：LLVM 23.1.4 预编译版，Windows x86_64；源码是同版本 sparse checkout：`.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行。`llvm-tblgen` 的 include 根是预编译包里的 `include/`，里面有 `llvm/Target/Target.td`。

## 为什么

后端里最容易被误读的文件就是 `.td`。它看起来像 C++ 的类定义，实际不是：`.td` 没有函数体、没有运行时、没有控制流。它只做一件事：把一组**记录**（record）写进内存，然后交给 TableGen 的某个 backend 生成 C++ 或表格。

不理解这一层，会遇到三类典型问题。

第一类是把 `def` 当成函数。`def ADD : ALU_rr<...>` 不是“定义一条叫 ADD 的指令”，而是“创建一个记录，名字叫 ADD，父类是 ALU_rr 的某个实例”。记录里的字段来自父类、模板参数和 `let` 覆盖，最终是一个扁平的字段集合。你在 `.td` 里看到的继承链，到了记录里已经展开。

第二类是把 `Pattern = []` 当成“这条指令没有匹配规则”。`ADD` 的记录里确实是空的 `Pattern`，但 `llvm-tblgen -gen-dag-isel` 仍然为它生成了 matcher。原因是匹配规则写在**匿名 Pattern 记录**里，例如 `def : PatGprGpr<add, ADD>;`。指令记录和 Pattern 记录是两类对象，靠操作数里的 `ADD` 这个名字连起来。

第三类是低估 `let` 的影响。`let isReMaterializable = 1` 看起来只是改一个布尔字段，实际会改变寄存器分配器的重物化决策；`let Predicates = [IsRV64]` 会改变指令在哪些 subtarget 上可见；`let Inst{31-25} = funct7` 会改变编码位。改一行 `.td`，生成物可能变动几千行，这不是工具“抽风”，而是记录字段的连锁反应。

本章的抓手是真实记录。我们会用 `llvm-tblgen -print-records` 把 `ADD` 展开，再对照 `-gen-instr-info` 和 `-gen-dag-isel` 的产物。只要你能在记录里指出一个字段，就能判断它会影响枚举、编码、汇编器还是选择器。

## 主线

### 4.1 一次 TableGen 运行：解析、实例化、backend

`llvm-tblgen` 的输入是一个入口 `.td`，RISC-V 的入口是 `RISCV.td`。它先处理 `include`，把 `Target.td`、特性、寄存器、指令格式、扩展指令、调度模型全部读进来；然后解析 `class` 和 `def`，把每个 `def` 实例化成记录；最后按命令行指定的 backend 输出。

`-print-records` 是最直接的观察方式。它不生成 C++，只把内存里的记录按文本打出来。真实命令：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
& "$bin/llvm-tblgen.exe" -I $inc -I $td -print-records $td/RISCV.td -o out/RISCV.records.td
```

本机实跑结果：耗时约 5.3 秒，输出 `145,074,924` 字节。这个数字本身说明 RISC-V 的记录量已经不适合人眼通读；正确的读法是按名字定位，而不是从头看。

`ADD` 的记录在输出第 67710 行开始，真实片段如下（为了让一行放得下，`Inst` 字段保留原样）：

```text
def ADD {	// InstructionEncoding Instruction RVInstCommon RVInst RVInstRBase RVInstR ALU_rr Sched
  field bits<32> Inst = { 0, 0, 0, 0, 0, 0, 0, rs2{4}, rs2{3}, rs2{2}, rs2{1}, rs2{0}, rs1{4}, rs1{3}, rs1{2}, rs1{1}, rs1{0}, 0, 0, 0, rd{4}, rd{3}, rd{2}, rd{1}, rd{0}, 0, 1, 1, 0, 0, 1, 1 };
  int Size = 4;
  string DecoderNamespace = "";
  list<Predicate> Predicates = [];
  string DecoderMethod = "";
  bit hasCompleteDecoder = 1;
  string Namespace = "RISCV";
  dag OutOperandList = (outs GPR:$rd);
  dag InOperandList = (ins GPR:$rs1, GPR:$rs2);
  string AsmString = "add	$rd, $rs1, $rs2";
  list<dag> Pattern = [];
  bit isCommutable = 1;
  list<SchedReadWrite> SchedRW = [WriteIALU, ReadIALU, ReadIALU];
  InstFormat Format = InstFormatR;
}
```

这里有几个关键点，后面会逐个展开：

- `Inst` 是一个 `bits<32>`，每个位置要么是常量，要么引用另一个字段的某一位。
- `OutOperandList` 和 `InOperandList` 是 `dag`，不是字符串。
- `Pattern` 是空的。`ADD` 的匹配规则不在这条记录里。
- `isCommutable = 1` 来自 `ALU_rr` 的 `Commutable` 参数，不是 `ADD` 自己写的。
- `Predicates` 是空的，说明基础 `ADD` 不依赖 `+m`、`+v` 这类扩展。

记录头的注释 `InstructionEncoding Instruction RVInstCommon RVInst RVInstRBase RVInstR ALU_rr Sched` 是继承链。它告诉你 `ADD` 的字段是从哪些类一路叠上来的。读 `.td` 时先看这条链，比在文件里上下翻更快。

### 4.2 值类型：bit、int、bits、list、dag、string

TableGen 的值类型不多，但每一种都对应生成物的不同部位。把它们和记录字段对上，基本就不会混。

| 类型 | 写法 | 在 RISC-V 后端里的例子 | 影响的产物 |
| --- | --- | --- | --- |
| `bit` | `1` / `0` | `isCommutable = 1`、`mayLoad = 1` | 指令属性、调度、编码选择 |
| `int` | `42`、`-1` | `Size = 4`、`AddedComplexity = 0` | 记录字段、模式优先级 |
| `bits<N>` | `bits<32> Inst` | `Inst{31-25} = funct7` | 机器码位、解码器、编码器 |
| `list<T>` | `[IsRV64]`、`[WriteIALU, ReadIALU]` | `Predicates`、`SchedRW` | 谓词检查、调度资源 |
| `dag` | `(outs GPR:$rd)`、`(ins GPR:$rs1, GPR:$rs2)` | 操作数列表、Pattern | 选择器、汇编字符串、MCInst |
| `string` | `"add"`、`"$rd, $rs1, $rs2"` | `AsmString`、`DecoderNamespace` | 汇编器、打印器、反汇编器 |
| `code` | `[{ return isInt<12>(Imm); }]` | `MCOperandPredicate`、`PredicateCode` | 生成的 C++ 片段 |
| `list<dag>` | `[(ADD GPR:$rs1, GPR:$rs2)]` | `ResultInstrs` | 选择器的替换结果 |

`bits<N>` 的位序容易出错。`bits<8> Code = 0x12` 在 `-print-records` 里打印成：

```text
bits<8> Code = { 0, 0, 0, 1, 0, 0, 1, 0 };
```

也就是从高位到低位。`0x12` 是 `0001 0010`。当你在 `.td` 里写 `Inst{31-25} = funct7` 时，TableGen 把 `funct7` 的 7 位按同样的方向塞进 31 到 25 位。位序写反，编码就会错，但 `.td` 本身仍然能通过解析；这类错误通常要到 `llvm-mc -show-encoding` 或反汇编才暴露。

`dag` 是“带操作符的列表”。`(outs GPR:$rd)` 的操作符是 `outs`，参数是一个带名字的 `GPR`；`(ins GPR:$rs1, GPR:$rs2)` 的操作符是 `ins`，参数有两个。`$rd`、`$rs1` 这些名字会进入 `AsmString` 的占位符，也会进入选择器的操作数绑定。

`string` 不是随便的注释。`AsmString = "add\t$rd, $rs1, $rs2"` 决定汇编打印；`"add"` 作为 `opcodestr` 进入 `InstAlias` 和汇编匹配表。把 `AsmString` 写错，`llvm-mc` 能汇编的文本和 `llc` 打印的文本就会不一致。

`code` 是嵌入的 C++ 片段。它不在 TableGen 里执行，而是被复制到生成的 C++ 里。`simm12` 的 `MCOperandPredicate` 就是一个例子：

```text
code MCOperandPredicate = [{
  int64_t Imm;
  if (!MCOp.evaluateAsConstantImm(Imm))
    return false;
  return isInt<12>(Imm);
}];
```

这段代码最终出现在汇编器/反汇编器的谓词检查里，不是 TableGen 解释器里。把它当成“生成代码的模板”比当成“TableGen 函数”更准确。

### 4.3 class、def、multiclass：模板、实例、批量实例

`class` 是模板，`def` 是实例，`multiclass` 是“一次生成多个 def 的模板”。这三者的区别可以用一个最小实验看清。

新建一个临时 `.td`（不要改源码树）：

```tablegen
class Enc<int n> {
  int Width = n;
  bit Valid = 1;
  bits<8> Code = 0x00;
  string Tag = "base";
  list<int> Fields = [1, 2, 3];
}
def EncA : Enc<4> {
  let Code = 0x12;
  let Tag = "a";
}
multiclass Pair<int n> {
  def _Lo : Enc<n> { let Tag = "lo"; }
  def _Hi : Enc<n> { let Tag = "hi"; }
}
defm P : Pair<8>;
def : Enc<16>;
```

运行：

```powershell
& "$bin/llvm-tblgen.exe" -print-records .\td-semantics.td
```

真实输出（节选）：

```text
def EncA {	// Enc
  int Width = 4;
  bit Valid = 1;
  bits<8> Code = { 0, 0, 0, 1, 0, 0, 1, 0 };
  string Tag = "a";
  list<int> Fields = [1, 2, 3];
def P_Hi {	// Enc
  int Width = 8;
  string Tag = "hi";
def P_Lo {	// Enc
  int Width = 8;
  string Tag = "lo";
def anonymous_0 {	// Enc
  int Width = 16;
  string Tag = "base";
```

这张输出说明四件事：

1. `class Enc<int n>` 本身不是记录；只有 `def` 才产生记录。
2. `EncA` 继承了 `Valid`、`Fields`，覆盖了 `Code` 和 `Tag`。
3. `multiclass Pair` 加上 `defm P` 生成了 `P_Lo` 和 `P_Hi` 两个记录；后缀来自 multiclass 内部的 `def _Lo` / `def _Hi`。
4. `def : Enc<16>;` 生成匿名记录 `anonymous_0`；名字由 TableGen 自动分配。

RISC-V 后端大量使用这三种形式。`class ALU_rr<...>` 是模板；`def ADD : ALU_rr<...>` 是实例；`defm : BccPat<SETEQ, BEQ>` 是匿名 multiclass 实例，会生成若干个 Pattern 记录。`defm Select_GPR : SelectCC_GPR_rrirr<GPR, XLenVT>` 则生成一族带后缀的伪指令。

匿名记录是读生成物时的一个坎。`def : PatGprGpr<add, ADD>;` 没有名字，`-print-records` 里叫 `anonymous_15183`。这个名字在不同版本、不同 include 顺序下可能变化，所以不要把它当稳定 API。稳定的定位方式是搜内容，例如：

```powershell
Select-String -Path out/RISCV.records.td -Pattern 'PatternToMatch = \(XLenVT \(add \(XLenVT GPR:\$rs1\), \(XLenVT GPR:\$rs2\)\)\)' -Context 0,4
```

本机真实命中的记录是：

```text
def anonymous_15183 {	// Pattern Pat PatGprGpr
  dag PatternToMatch = (XLenVT (add (XLenVT GPR:$rs1), (XLenVT GPR:$rs2)));
  list<dag> ResultInstrs = [(ADD GPR:$rs1, GPR:$rs2)];
  list<Predicate> Predicates = [];
  int AddedComplexity = 0;
}
```

这条记录才是 `ADD` 的选择规则。`ADD` 记录里的 `Pattern = []` 不是遗漏，而是分工。

### 4.4 let：覆盖字段的三种位置

`let` 的作用是“在这个作用域内，把某个字段设成新值”。它出现的位置决定作用域。

第一种，写在 `def` 或 `class` 的花括号里，只影响当前记录：

```tablegen
def FENCE : RVInstI<...> {
  bits<4> pred;
  bits<4> succ;
  let rs1 = 0;
  let rd = 0;
  let imm12 = {0b0000,pred,succ};
}
```

第二种，写在 `def` 前面，影响紧接着的那个定义：

```tablegen
let isReMaterializable = 1, isAsCheapAsAMove = 1 in
def ADDI  : ALU_ri<0b000, "addi">;
```

第三种，写在 `let ... in { ... }` 块里，影响块内所有定义：

```tablegen
let IsSignExtendingOpW = 1 in {
def SLTI  : ALU_ri<0b010, "slti">;
def SLTIU : ALU_ri<0b011, "sltiu">;
}
```

这三种写法最终都落到记录的同一个字段上。区别只在作用域。读代码时，先找最近的 `let`，再找 `class` 默认值，最后找父类。

本机的 `ADDI` 记录可以直接验证覆盖结果：

```text
def ADDI {	// InstructionEncoding Instruction RVInstCommon RVInst RVInstIBase RVInstI ALU_ri Sched
  field bits<32> Inst = { imm12{11}, ..., 0, 0, 1, 0, 0, 1, 1 };
  list<Predicate> Predicates = [];
  bit isReMaterializable = 1;
  bit isAsCheapAsAMove = 1;
}
```

`isReMaterializable` 和 `isAsCheapAsAMove` 在 `ALU_ri` 里没有默认成 1，是 `let` 给 `ADDI` 加的。`ADDIW` 则是另一种情况：

```text
def ADDIW {	// 记录头见完整输出
  field bits<32> Inst = { imm12{11}, ..., 0, 0, 1, 1, 0, 1, 1 };
  list<Predicate> Predicates = [IsRV64];
}
```

`Predicates = [IsRV64]` 来自 `let Predicates = [IsRV64] in { ... }` 块。它让 `ADDIW` 只在 RV64 的 subtarget 上可见。注意这不是“运行时 if”，而是生成到 matcher 里的谓词检查；RV32 上这条指令根本不会进入候选集。

`let` 也能改编码位。`ADD` 的 `Inst` 里，31 到 25 位是 `funct7`，24 到 20 位是 `rs2`，19 到 15 位是 `rs1`，14 到 12 位是 `funct3`，11 到 7 位是 `rd`，6 到 0 位是 `opcode`。这些赋值分散在 `RVInstRBase`、`RVInstR` 和 `ALU_rr` 里，`ADD` 只提供模板参数。读编码时不要只盯着 `def ADD` 那一行。

`let` 的常见错误是写了一个不存在的字段：

```tablegen
class C { int X = 1; }
def D : C { let Y = 2; }
```

真实报错：

```text
error: Value 'Y' unknown!
def D : C { let Y = 2; }
                ^
```

`bits<N>` 溢出也会报错。把 `bits<3>` 赋成 `0x1F`：

```text
error: Initializer of 'B' in 'D' could not be fully resolved:
{ !cast<bits<3>>(31){2}, !cast<bits<3>>(31){1}, !cast<bits<3>>(31){0} }
```

这两个错误都在解析/实例化阶段发生，不会等到生成 C++ 才失败。写 `.td` 时先跑 `-print-records`，比直接跑大 backend 更快定位。

### 4.5 Instruction、Operand、Pattern：三条基类链

RISC-V 的指令记录最终都继承 `Instruction`。`Instruction` 在 `include/llvm/Target/Target.td` 第 663 行，字段很多，和本章相关的主要有：

| 字段 | 作用 | `ADD` 的值 |
| --- | --- | --- |
| `OutOperandList` | 定义操作数 | `(outs GPR:$rd)` |
| `InOperandList` | 使用操作数 | `(ins GPR:$rs1, GPR:$rs2)` |
| `AsmString` | 汇编打印格式 | `"add\t$rd, $rs1, $rs2"` |
| `Pattern` | 指令自带的匹配规则 | `[]` |
| `Predicates` | subtarget 可见性 | `[]` |
| `Size` | 编码字节数 | `4` |
| `isCommutable` | 是否可交换操作数 | `1` |
| `mayLoad` / `mayStore` | 访存属性 | `0` / `0` |
| `hasSideEffects` | 是否有副作用 | `0` |
| `isCodeGenOnly` | 是否只用于代码生成 | `0` |

RISC-V 在 `Instruction` 之上又叠了几层。`RISCVInstrFormats.td` 第 306 行定义 `RVInst`，第 387 行定义 `RVInstR`，第 467 行定义 `RVInstI`，第 500 行定义 `RVInstS`，第 515 行定义 `RVInstB`，第 548 行定义 `RVInstU`，第 559 行定义 `RVInstJ`。`RVInstR` 负责把 `funct7`、`rs2`、`rs1`、`funct3`、`rd`、`opcode` 放进 `Inst` 的固定位置；`ALU_rr` 接上 `opcodestr` 和 `Commutable`，`ADD` 定义末尾的 `Sched<...>` 再挂上调度信息。

`ADD` 的完整链是：

```text
InstructionEncoding
  -> Instruction
    -> RVInstCommon
      -> RVInst
        -> RVInstRBase
          -> RVInstR
            -> ALU_rr
              -> ADD
```

这条链可以在 `-print-records` 的记录头注释里直接看到。你不需要记住每一层的所有字段，但要记住“越靠下的类越具体，越靠下的 `let` 优先级越高”。

`Operand` 和 `RegisterClass` 是另一条链。`Operand` 在 `Target.td` 第 1115 行，`RegisterClass` 在第 292 行。RISC-V 的 `GPR` 记录头是：

```text
def GPR {	// DAGOperand RegisterClassLike RegisterClass RISCVRegisterClass GPRRegisterClass
  RegInfoByHwMode RegInfos = XLenRI;
  list<ValueType> RegTypes = [XLenVT, XLenFVT, XLenVecI8VT, XLenVecI16VT, XLenVecI32VT];
  dag MemberList = (add (sequence "X%u", 10, 17), ...);
  bit isAllocatable = 1;
}
```

`GPR` 不是 `Operand` 的子类，而是 `RegisterClass`。它能出现在 `(ins GPR:$rs1)` 里，是因为 `RegisterClass` 也实现了 `DAGOperand` 接口。相反，`simm12` 的记录头是：

```text
def simm12 {	// DAGOperand Operand RISCVOp RISCVSImmOp SDPatternOperator PatFrags PatFrag ImmLeaf RISCVSImmLeafOp
  string DecoderMethod = "decodeSImmOperand<12>";
  string EncoderMethod = "getImmOpValue";
  string OperandType = "OPERAND_SIMM12";
  dag Fragments = [(XLenVT imm)];
  string ImmediateCode = "return isInt<12>(Imm);";
}
```

`simm12` 是 `Operand`，同时是 `ImmLeaf`。前者让汇编器认识它，后者让选择器能对它做立即数匹配。`uimm5`、`uimmlog2xlen`、`simm12_lo` 都是同一套路：一个操作数记录，加上一个立即数谓词。

`Pattern` 在 `TargetSelectionDAG.td` 第 2450 行，`Pat` 在第 2460 行，`PatFrag` 在第 1195 行，`PatLeaf` 在第 1207 行，`ImmLeaf` 在第 1240 行，`ComplexPattern` 在第 2468 行。`PatGprGpr` 是 RISC-V 自己定义的便利类：

```tablegen
class PatGprGpr<SDPatternOperator OpNode, RVInst Inst, ValueType vt = XLenVT>
    : Pat<(vt (OpNode (vt GPR:$rs1), (vt GPR:$rs2))), (Inst GPR:$rs1, GPR:$rs2)>;
```

`Pat` 只是 `Pattern` 的语法糖：`Pat<pattern, result>` 等价于 `Pattern<pattern, [result]>`。`PatFrag` 用来给子图起名字并附加谓词，`PatLeaf` 用来匹配叶子节点并附加 C++ 谓词，`ComplexPattern` 用来调用 C++ 选择函数并返回多个结果。第 07 章会展开这四者的差别，本章先记住它们都属于 Pattern 家族。

`Requires` 和 `Predicates` 也容易混。`Requires` 在 `Target.td` 第 958 行，定义只有三行：

```tablegen
class Requires<list<Predicate> preds> {
  list<Predicate> Predicates = preds;
}
```

它给 Pattern 记录加谓词。`let Predicates = [...]` 给指令记录加谓词。两者最终都会进入生成的 matcher，但作用的记录不同。`Requires<[IsRV32]>` 写在 Pattern 后面，限制的是“这条规则在 RV32 上才匹配”；`let Predicates = [IsRV64]` 写在指令块里，限制的是“这条指令在 RV64 上才存在”。

`Predicate` 在 `Target.td` 第 911 行，RISC-V 的 `HasStdExtM` 在 `RISCVFeatures.td` 第 220 行：

```tablegen
def HasStdExtM : Predicate<"Subtarget->hasStdExtM()">, ...
```

这不是字符串标签，而是会变成 C++ 表达式 `Subtarget->hasStdExtM()`。`IsRV32`、`IsRV64`、`HasStdExtZca`、`OptForMinSize` 都是同一机制。`OptForMinSize` 的定义在 `RISCVInstrInfo.td` 第 1702 行：

```tablegen
def OptForMinSize : Predicate<"MF ? MF->getFunction().hasMinSize() : false">;
```

它依赖当前 MachineFunction，所以不能在指令记录里随便用；它通常出现在 Pattern 或带 `Predicates` 的指令块里。

### 4.6 RISCVInstrInfo.td 的组织方式与 include 链

`RISCV.td` 是总入口。它先 `include "llvm/Target/Target.td"`，再按类别 include：

```text
RISCVFeatures.td
RISCVProfiles.td
RISCVSystemOperands.td
RISCVRegisterInfo.td
RISCVSchedule.td
RISCVCallingConv.td
RISCVInstrInfo.td
GISel/RISCVRegisterBanks.td
RISCVInstrPredicates.td
RISCVMacroFusion.td
RISCVSched*.td
RISCVProcessors.td
RISCVPfmCounters.td
```

最后定义 `RISCVInstrInfo : InstrInfo`、`RISCVAsmParser : AsmParser`、`RISCVAsmWriter : AsmWriter` 和 `RISCV : Target`。这三个对象是后端注册的锚点：`InstructionSet` 指向 `RISCVInstrInfo`，`AssemblyParsers` 指向 `RISCVAsmParser`，`AssemblyWriters` 指向 `RISCVAsmWriter`。

`RISCVInstrInfo.td` 自己也是一个 include 中心。它在第 635 到 638 行 include 四个格式文件：

```text
RISCVInstrFormats.td
RISCVInstrFormatsC.td
RISCVInstrFormatsV.td
RISCVInstrFormatsXAIF.td
```

在 2364 行之后按扩展 include：

```text
RISCVInstrInfoM.td
RISCVInstrInfoA.td
RISCVInstrInfoZa.td
RISCVInstrInfoZalasr.td
RISCVInstrInfoZimop.td
RISCVInstrInfoZicbo.td
RISCVInstrInfoZicond.td
...
RISCVInstrInfoV.td
RISCVInstrInfoC.td
...
```

最后在 2441 行 include `RISCVInstrGISel.td`。这个顺序不是随便排的：格式基类要先有，寄存器类和调度类要先有，扩展指令才能用；C 和 V 的格式、伪指令、pattern 又依赖前面的整数基础。TableGen 允许多轮解析，但把依赖顺序写清楚仍然是可维护性的基础。

`ADD` 在 `RISCVInstrInfo.td` 第 818 行：

```tablegen
def ADD  : ALU_rr<0b0000000, 0b000, "add", Commutable=1>,
           Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

它没有花括号，没有 `let`，没有 Pattern。所有信息都在模板参数和父类里。`ALU_rr` 在第 698 行：

```tablegen
class ALU_rr<bits<7> funct7, bits<3> funct3, string opcodestr,
             bit Commutable = 0>
    : RVInstR<funct7, funct3, OPC_OP, (outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2),
              opcodestr, "$rd, $rs1, $rs2"> {
  let isCommutable = Commutable;
}
```

`ADDI` 在第 797 行，`ALU_ri` 在第 686 行：

```tablegen
class ALU_ri<bits<3> funct3, string opcodestr>
    : RVInstI<funct3, OPC_OP_IMM, (outs GPR:$rd), (ins GPR:$rs1, simm12_lo:$imm12),
              opcodestr, "$rd, $rs1, $imm12">,
      Sched<[WriteIALU, ReadIALU]>;
```

真正的匹配规则在第 1470 行附近：

```tablegen
def : PatGprGpr<add, ADD>;
def : PatGprSimm12<add, ADDI>;
def : PatGprGpr<sub, SUB>;
def : PatGprGpr<or, OR>;
def : PatGprSimm12<or, ORI>;
def : PatGprGpr<and, AND>;
def : PatGprSimm12<and, ANDI>;
def : PatGprGpr<xor, XOR>;
def : PatGprSimm12<xor, XORI>;
```

这就是“指令记录”和“pattern 记录”分离的证据。`ADD` 只描述自己；`PatGprGpr<add, ADD>` 描述“看到 `add` 节点时，生成 `ADD` 指令”。读 RISC-V 后端时，找指令去 `def`，找选择规则去 `Pat`/`PatGpr*`/`Pattern`，两边不要混。

## 可跑实验

先准备本章统一的路径变量和输出目录（命令都在仓库根目录执行）：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
New-Item -ItemType Directory -Force -Path out | Out-Null
```

### 实验 1：最小 TableGen 记录

目标：看清 `class`、`def`、`multiclass`、`let`、匿名 def 的实际记录。

```powershell
$tdText = @"
class Enc<int n> {
  int Width = n;
  bit Valid = 1;
  bits<8> Code = 0x00;
  string Tag = "base";
  list<int> Fields = [1, 2, 3];
}
def EncA : Enc<4> {
  let Code = 0x12;
  let Tag = "a";
}
multiclass Pair<int n> {
  def _Lo : Enc<n> { let Tag = "lo"; }
  def _Hi : Enc<n> { let Tag = "hi"; }
}
defm P : Pair<8>;
def : Enc<16>;
"@
[IO.File]::WriteAllText("$env:TEMP\td-semantics.td", $tdText, (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-tblgen.exe" -print-records "$env:TEMP\td-semantics.td"
```

预期：看到 `EncA`、`P_Hi`、`P_Lo`、`anonymous_0` 四个记录；`EncA` 的 `Code` 是 `0x12` 的位串，`P_Hi`/`P_Lo` 的 `Width` 都是 8，`Tag` 分别是 `hi`/`lo`。

### 实验 2：展开真实的 ADD 记录

目标：在 145 MB 的记录文件里定位 `ADD`，确认 `Pattern = []`、`isCommutable = 1`、`Inst` 位布局。

```powershell
& "$bin/llvm-tblgen.exe" -I $inc -I $td -print-records $td/RISCV.td -o out/RISCV.records.td
Select-String -Path out/RISCV.records.td -Pattern '^def ADD\s*\{' -Context 0,24
```

预期：记录头是 `InstructionEncoding Instruction RVInstCommon RVInst RVInstRBase RVInstR ALU_rr Sched`；`OutOperandList = (outs GPR:$rd)`；`InOperandList = (ins GPR:$rs1, GPR:$rs2)`；`Pattern = []`；`isCommutable = 1`。

### 实验 3：让 TableGen 报错

目标：确认 `let` 的字段名和 `bits<N>` 宽度都在实例化阶段检查。

```powershell
$bad = @"
class C { int X = 1; }
def D : C { let Y = 2; }
"@
[IO.File]::WriteAllText("$env:TEMP\td-bad.td", $bad, (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-tblgen.exe" -print-records "$env:TEMP\td-bad.td"
```

预期：`error: Value 'Y' unknown!`，进程返回 1。把 `Y` 改成 `X` 后错误消失；把 `bits<3>` 赋成 `0x1F` 会得到 `could not be fully resolved`。

### 实验 4：定位匿名 Pattern

目标：不靠 `anonymous_15183` 这种不稳定名字，靠内容找到 `ADD` 的匹配记录。

```powershell
Select-String -Path out/RISCV.records.td -Pattern 'PatternToMatch = \(XLenVT \(add \(XLenVT GPR:\$rs1\), \(XLenVT GPR:\$rs2\)\)\)' -Context 0,4
```

预期：命中 `def anonymous_15183 { // Pattern Pat PatGprGpr`，其 `ResultInstrs` 是 `[(ADD GPR:$rs1, GPR:$rs2)]`。这证明 `ADD` 的选择规则不在指令记录里。

## 源码落点

| 位置 | 符号 | 说明 |
| --- | --- | --- |
| `llvm/lib/Target/RISCV/RISCV.td` | `RISCV : Target` | 总入口，绑定 `InstructionSet`、`AssemblyParsers`、`AssemblyWriters` |
| `RISCV.td` | `include "RISCVInstrInfo.td"` | 指令定义入口 |
| `RISCVInstrInfo.td:635-638` | 四个 `RISCVInstrFormats*.td` | R/I/S/B/U/J、C、V、XAIF 格式基类 |
| `RISCVInstrInfo.td:686` | `class ALU_ri` | I 型算术指令模板 |
| `RISCVInstrInfo.td:698` | `class ALU_rr` | R 型算术指令模板，`Commutable` 参数 |
| `RISCVInstrInfo.td:797` | `def ADDI` | `addi` 指令记录 |
| `RISCVInstrInfo.td:818` | `def ADD` | `add` 指令记录 |
| `RISCVInstrInfo.td:1470-1481` | `PatGprGpr` / `PatGprSimm12` 实例 | 基础算术的 DAG pattern |
| `RISCVInstrInfo.td:1603` | `addr_hi_lo` | 地址高低位拼接的 PatFrag |
| `RISCVInstrInfo.td:1669` | `riscv_cond` | 条件码转换的 PatLeaf |
| `RISCVInstrInfo.td:2014` | `class LdPat` | 访存 pattern，使用 `AddrRegImm` |
| `RISCVInstrInfo.td:2029` | `class StPat` | 存储 pattern |
| `RISCVInstrFormats.td:306` | `class RVInst` | 指令基类，`Inst` 的起点 |
| `RISCVInstrFormats.td:387` | `class RVInstR` | R 型字段布局 |
| `RISCVInstrFormats.td:467` | `class RVInstI` | I 型字段布局 |
| `RISCVRegisterInfo.td:317` | `def GPR` | 通用寄存器类 |
| `RISCVFeatures.td:220` | `def HasStdExtM` | `+m` 的 Predicate |
| `include/llvm/Target/Target.td:663` | `class Instruction` | 指令记录基类 |
| `include/llvm/Target/Target.td:911` | `class Predicate` | 谓词基类 |
| `include/llvm/Target/Target.td:958` | `class Requires` | 给 Pattern 加谓词 |
| `include/llvm/Target/Target.td:1115` | `class Operand` | 操作数基类 |
| `include/llvm/Target/TargetSelectionDAG.td:2450` | `class Pattern` | Pattern 基类 |
| `include/llvm/Target/TargetSelectionDAG.td:2460` | `class Pat` | `Pattern` 的语法糖 |
| `include/llvm/Target/TargetSelectionDAG.td:2468` | `class ComplexPattern` | C++ 选择函数入口 |

读这些文件时，建议按“入口 -> 格式 -> 指令 -> pattern”的顺序，而不是从 145 MB 的记录文件硬翻。

## 常见坑

1. **把 `def` 当函数调用。** `def ADD : ALU_rr<...>` 创建的是记录，不是函数；它没有返回值，也没有运行时行为。要观察结果，用 `-print-records`。

2. **把 `Pattern = []` 当成没有匹配。** `ADD` 的指令记录确实没有 Pattern，但 `def : PatGprGpr<add, ADD>;` 生成了独立的匿名记录。找匹配规则要去 `-gen-dag-isel` 产物或 `Pat*` 定义里找。

3. **把 `GPR` 和 `simm12` 当成同一种 Operand。** `GPR` 是 `RegisterClass`，`simm12` 是 `Operand` + `ImmLeaf`。它们都能出现在 `ins` 里，但一个约束寄存器，一个约束立即数。

4. **以为 `let` 只影响下一行。** `let ... in { ... }` 影响整个块；写在 `def` 花括号里只影响该记录；写在 `class` 里会影响所有实例，除非实例再覆盖。

5. **混用 `Requires` 和 `Predicates`。** `Requires<[IsRV64]>` 给 Pattern 加谓词，`let Predicates = [IsRV64]` 给指令加谓词。两者都进 matcher，但限制的对象不同。

6. **把 `bits<N>` 当整数随便赋值。** `bits<3>` 只能放 0 到 7；`0x1F` 会在实例化阶段报 `could not be fully resolved`。位序从高到低打印，赋值时不要按小端直觉写。

7. **用匿名记录名做稳定引用。** `anonymous_15183` 会随 include 顺序和版本变化。要定位匿名 Pattern，搜 `PatternToMatch`、`ResultInstrs` 或生成的 `Src:`/`Dst:` 注释。

8. **改 `.td` 后只看源码 diff，不看生成物。** 一个 `let` 可能让枚举整体位移，生成物 diff 上千行。第 05 章会实跑这条闭环；判断影响面要看 `-gen-instr-info` 和 `-gen-dag-isel`，不要只看 `.td`。

9. **在源码树里直接改 `.td` 做实验。** 这会污染唯一的同版本源码。复制整个 RISCV 目录到 `.scratch`，用 `-I` 指向副本，是更安全的做法。

## 自我检查

- [ ] 我能说出 `class`、`def`、`multiclass` 三者分别产生什么，以及 `defm` 的后缀规则。
- [ ] 我能解释 `bits<32> Inst` 里每一段位对应的字段，并指出 `ADD` 的 `funct7`、`rs2`、`rs1`、`funct3`、`rd`、`opcode` 各在哪几位。
- [ ] 我能用 `-print-records` 找到 `ADD` 记录，并指出 `Pattern = []`、`isCommutable = 1`、`Predicates = []`。
- [ ] 我能找到 `ADD` 对应的匿名 Pattern 记录，并说明它为什么不在 `ADD` 记录里。
- [ ] 我能区分 `GPR`（RegisterClass）和 `simm12`（Operand + ImmLeaf）。
- [ ] 我能解释 `let isReMaterializable = 1` 和 `let Predicates = [IsRV64]` 分别改变了什么。
- [ ] 我知道 `Requires` 和 `Predicates` 各自作用在哪种记录上。
- [ ] 我能按 `RISCV.td` -> `RISCVInstrInfo.td` -> `RISCVInstrFormats.td` 的顺序说明 include 链。

## 延伸阅读

- LLVM TableGen 官方文档：<https://llvm.org/docs/TableGen/>
- LLVM TableGen Backends：<https://llvm.org/docs/TableGen/BackEnds.html>
- LLVM 后端开发指南：<https://llvm.org/docs/CodeGenerator.html>
- `include/llvm/Target/Target.td`：`Instruction`、`Operand`、`Predicate`、`Requires`
- `include/llvm/Target/TargetSelectionDAG.td`：`Pattern`、`Pat`、`PatFrag`、`PatLeaf`、`ImmLeaf`、`ComplexPattern`
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`：`ALU_ri`、`ALU_rr`、`ADDI`、`ADD`、`PatGpr*`
- `llvm/lib/Target/RISCV/RISCVInstrFormats.td`：`RVInst`、`RVInstR`、`RVInstI`
- `llvm/lib/Target/RISCV/RISCVRegisterInfo.td`：`GPR`
- `llvm/lib/Target/RISCV/RISCVFeatures.td`：`HasStdExtM` 等 Predicate

[上一章：流水线与 MIR](./03-pipeline-and-mir.md) | [下一章：生成物解剖](./05-generated-artifacts.md)