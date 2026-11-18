# 练习 D — 给 NVPTX 加一个 intrinsic

> **目标**：走完「声明 intrinsic → TableGen 映射 → 打印 → lit 测试」四层，得到一个能被 `llc` 选中并输出指定 PTX 的新 intrinsic。

## 背景与动机

真实后端工作里，「加一条新指令」比「改一个优化」更常见：新硬件有 `foo.op`，编译器要能生成它。NVPTX 的扩展路径很清晰，这个练习把它压缩成一个最小改动。

> 本练习需要重新构建 `llc`。本课没有替你执行完整构建；下面给的是可复现的路径与预期结果，实际行为以你的构建为准。

## 任务规格

新增一个 intrinsic：

```llvm
declare i32 @llvm.nvvm.my.inc(i32 %x)
```

要求 `llc` 输出：

```ptx
add.u32 	%r1, %r0, 1;
```

（等价于 `x + 1`，但必须走新 intrinsic，而不是被 IR 优化成普通 add。）

改动范围：

- `llvm/include/llvm/IR/IntrinsicsNVVM.td`：声明 intrinsic。
- `llvm/lib/Target/NVPTX/NVPTXIntrinsics.td`：定义指令与 `Pat`。
- `llvm/test/CodeGen/NVPTX/my-inc.ll`：lit 测试。

验收：

```bash
ninja -C /home/ubuntu/llvm/builds/ct-instr-trunk llc
cd /home/ubuntu/llvm/builds/ct-instr-trunk
./bin/llvm-lit -q test/CodeGen/NVPTX/my-inc.ll
```

## 期望输出范例

输入：

```llvm
define i32 @f(i32 %x) {
  %r = call i32 @llvm.nvvm.my.inc(i32 %x)
  ret i32 %r
}
```

输出（关键行）：

```ptx
add.u32 	%r1, %r0, 1;
```

边界输入：`IntrNoMem` 缺失时，优化器可能不会把调用提升/合并；`Pat` 写错时，会 fallback 成 `Cannot select`。

## 如果你卡住了

1. **`Cannot select`**：`Pat` 的 intrinsic 名字与声明不一致，或类型不匹配。
2. **被优化成 `add`**：intrinsic 没标 `IntrNoMem`/`IntrSpeculatable`，或者测试 IR 被 InstCombine 先处理了。
3. **测试找不到**：lit 要用构建目录的 `test/...` 路径。
4. **重编太慢**：只构建 `llc` 目标，不要全量。
5. **PTX 打印不合法**：检查字符串里的操作数顺序与类型后缀。

## 实作步骤建议

### Step 1：声明
在 `IntrinsicsNVVM.td` 加 `int_nvvm_my_inc`，返回 `i32`、参数 `i32`。

### Step 2：映射
在 `NVPTXIntrinsics.td` 定义：

```tablegen
def INT_MY_INC : BasicNVPTXInst<(outs Int32Regs:$d), (ins Int32Regs:$a),
                                "add.u32 $d, $a, 1;",
                                [(set i32:$d, (int_nvvm_my_inc i32:$a))]>;
```

### Step 3：构建
`ninja -C <build> llc`。

### Step 4：手测
用 `llc -mtriple=nvptx64 -mcpu=sm_75` 编测试 IR，确认输出。

### Step 5：lit
写 `RUN` 行 + `CHECK`，跑通。

### Step 6：回归
跑一遍 `test/CodeGen/NVPTX`，确认没破坏既有测试。

## 完整参考解答

**写完再看！**

<details>
<summary>点开参考 patch</summary>

```tablegen
// IntrinsicsNVVM.td
def int_nvvm_my_inc : Intrinsic<[llvm_i32_ty], [llvm_i32_ty],
                                [IntrNoMem, IntrSpeculatable]>;
```

```tablegen
// NVPTXIntrinsics.td
def INT_MY_INC : BasicNVPTXInst<(outs Int32Regs:$d), (ins Int32Regs:$a),
                                "add.u32 $d, $a, 1;",
                                [(set i32:$d, (int_nvvm_my_inc i32:$a))]>;
```

```llvm
; llvm/test/CodeGen/NVPTX/my-inc.ll
; RUN: llc < %s -mtriple=nvptx64 -mcpu=sm_75 | FileCheck %s
; CHECK-LABEL: f:
; CHECK: add.u32
define i32 @f(i32 %x) {
  %r = call i32 @llvm.nvvm.my.inc(i32 %x)
  ret i32 %r
}
declare i32 @llvm.nvvm.my.inc(i32)
```

预期：`llc` 输出 `add.u32 %r1, %r0, 1;`，lit 通过。

</details>

## 卡点提示

1. 先只加定义、不加 `Pat`，确认 intrinsic 能被识别（否则会报未知 intrinsic）。
2. `Int32Regs` 的拼写要和 `NVPTXRegisterInfo.td` 一致。
3. 测试 IR 不要加 `-O2`，避免被折叠。
4. 打印字符串末尾的分号与空格会影响 FileCheck。

## 加分挑战

1. 把 `add.u32` 改成需要两个操作数的 `mad.lo.u32`，练习多操作数 pattern。
2. 加一个带 `.pred` 返回的 intrinsic，练习谓词类型。
3. 给新指令加 `Requires<[hasPTX...]>`，练习特性门控。
4. 用 `-verify-machineinstrs` 跑一次，确认 MIR 合法。

→ [Ch 18 PTX 内存一致性模型：scope、ordering 与 fence](./18-ptx-memory-model.md)
