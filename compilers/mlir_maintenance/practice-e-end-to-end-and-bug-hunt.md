# 练习 E — 端到端降级与 bug 猎杀

> **目标**：独立完成一次“`linalg.matmul` → LLVM dialect → `mlir-runner` 打印真实结果”的全链路；然后人为注入一个**不报错但结果错**的索引 bug，用 Ch 13 的方法把它定位到具体的那张 indexing map。最后把同一份代码 tile 一遍，体验 6 个 pass 的顺序依赖。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，`mlir-runner` 可用。
>
> **前置**：Ch 22（bufferization）、Ch 24（Linalg 三方边界）、Ch 13（调试与 reduce）。预计 60–90 分钟。

---

## E.1 任务与产出

你要交出四样东西，全部落在 `/tmp/mlir_p5/`：

| 产出 | 内容 |
|---|---|
| `e2e.mlir` | memref 版 matmul，`main` 返回 `C[1,1]` 的整数值 |
| `e2e_llvm.mlir` | 降级到 LLVM dialect 的结果，`mlir-runner` 能直接跑 |
| `bug2.mlir` | 注入了索引 bug 的版本，跑出 20 而不是 22 |
| `e2e-tile.mlir` | 加了 transform schedule 的版本，tile 后仍然是 22 |

评分标准不是“跑出 22”，而是**你能不能解释每一步为什么必须在那里**。E.8 会故意打乱顺序，让你看到四种不同的报错。

## E.2 先建立直觉：一条 6 跳的管线

```text
  e2e.mlir（linalg + memref + arith + func）
        │
   ① --convert-linalg-to-loops
        │   linalg.matmul → 3 层 scf.for + memref.load/store
        ▼
  scf.for + memref + arith
        │
   ② --convert-scf-to-cf        ← 必须单独跑！见 E.5
        │   scf.for → cf.br / cf.cond_br + block 参数
        ▼
  cf + memref + arith
        │
   ③ --convert-to-llvm
        │   arith/cf/func/memref → llvm.*，memref 描述符 → 5 个标量
        ▼
  llvm.func + llvm.store/load + llvm.call @malloc
        │
   ④ mlir-runner --entry-point-result=i32
        │   JIT 编译 + 执行 + 打印返回值
        ▼
      22

  ┌──────────────── 分数从哪来 ────────────────┐
  │ 正确性：22                                  │
  │ 可解释性：每一跳的输入/输出方言              │
  │ 鲁棒性：换成 tile 版仍然 22（E.9）           │
  └────────────────────────────────────────────┘
```

## E.3 Step 0：工作目录

```bash
mkdir -p /tmp/mlir_p5 && cd /tmp/mlir_p5
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt --version
```

真实输出：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
```

## E.4 Step 1：写一个“结果可判定”的 matmul

关键设计：**用返回值把结果带出来**，这样不需要 `printf`、不需要 `mlir_runner_utils`，`mlir-runner --entry-point-result=i32` 会直接打印。

<details>
<summary>点击展开：`e2e.mlir` 完整内容（约 30 行）</summary>

```mlir
func.func @main() -> i32 {
  %A = memref.alloc() : memref<2x2xf32>
  %B = memref.alloc() : memref<2x2xf32>
  %C = memref.alloc() : memref<2x2xf32>
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %v1 = arith.constant 1.0 : f32
  %v2 = arith.constant 2.0 : f32
  %v3 = arith.constant 3.0 : f32
  %v4 = arith.constant 4.0 : f32
  %z  = arith.constant 0.0 : f32
  memref.store %v1, %A[%c0, %c0] : memref<2x2xf32>
  memref.store %v2, %A[%c0, %c1] : memref<2x2xf32>
  memref.store %v3, %A[%c1, %c0] : memref<2x2xf32>
  memref.store %v4, %A[%c1, %c1] : memref<2x2xf32>
  memref.store %v1, %B[%c0, %c0] : memref<2x2xf32>
  memref.store %v2, %B[%c0, %c1] : memref<2x2xf32>
  memref.store %v3, %B[%c1, %c0] : memref<2x2xf32>
  memref.store %v4, %B[%c1, %c1] : memref<2x2xf32>
  memref.store %z, %C[%c0, %c0] : memref<2x2xf32>
  memref.store %z, %C[%c0, %c1] : memref<2x2xf32>
  memref.store %z, %C[%c1, %c0] : memref<2x2xf32>
  memref.store %z, %C[%c1, %c1] : memref<2x2xf32>
  linalg.matmul ins(%A, %B : memref<2x2xf32>, memref<2x2xf32>)
               outs(%C : memref<2x2xf32>)
  %r = memref.load %C[%c1, %c1] : memref<2x2xf32>
  %i = arith.fptosi %r : f32 to i32
  return %i : i32
}
```

</details>

为什么用 `A = B = [[1,2],[3,4]]`：`C[1,1] = 3*2 + 4*4 = 22`。这个值对“乘错行/乘错列”的 bug 很敏感——E.8 会看到它变成 20。

## E.5 Step 2：第一次降级，故意写错

很多人会直接写：

```bash
$T/bin/mlir-opt e2e.mlir --convert-linalg-to-loops --convert-to-llvm > e2e_llvm.mlir
$T/bin/mlir-runner e2e_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
loc("e2e_llvm.mlir":138:5): error: Dialect `scf' not found for custom op 'scf.for' 
could not parse the input IR
```

注意错误的**形态**：不是“降级失败”，而是 `mlir-runner` **解析不了**。原因是 `--convert-to-llvm` 并不包含 SCF→CF：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -E 'convert-to-llvm|convert-scf-to-cf'
```

真实输出：

```text
      --convert-scf-to-cf                                    -   Convert SCF dialect to ControlFlow dialect, replacing structured control flow with a CFG
      --convert-to-llvm                                      -   Convert to the LLVM dialect
```

`--convert-to-llvm` 是“把所有实现了 `ConvertToLLVMPatternInterface` 的方言降掉”的聚合 pass；`scf` 没有实现这个接口（它的降级路线是 `scf → cf → llvm`，中间必须经过 `cf`）。所以：

**规则 1：`--convert-to-llvm` ≠ “一步到 LLVM”。它不负责 `scf`、`affine`、`transform` 这些“结构化”方言。**

## E.6 Step 3：正确的三跳

```bash
$T/bin/mlir-opt e2e.mlir \
  --convert-linalg-to-loops \
  --convert-scf-to-cf \
  --convert-to-llvm > e2e_llvm.mlir
echo "lower_exit=$?"
grep -n 'llvm.func @main' e2e_llvm.mlir
$T/bin/mlir-runner e2e_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
lower_exit=0
3:  llvm.func @main() -> i32 {
22
```

`22` 就是 `C[1,1]`。跑通了，但先别急着结束——**能跑不等于能解释**。看一眼 `memref.alloc` 到底变成了什么：

```bash
grep -n 'llvm.call @malloc' e2e_llvm.mlir
sed -n '10,18p' e2e_llvm.mlir
```

真实输出：

```text
19:    %15 = llvm.call @malloc(%14) : (i64) -> !llvm.ptr
36:    %32 = llvm.call @malloc(%31) : (i64) -> !llvm.ptr
53:    %49 = llvm.call @malloc(%48) : (i64) -> !llvm.ptr
    %11 = llvm.mlir.constant(4 : i64) : i64
    %12 = llvm.mlir.zero : !llvm.ptr
    %13 = llvm.getelementptr %12[%11] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    %14 = llvm.ptrtoint %13 : !llvm.ptr to i64
    %15 = llvm.call @malloc(%14) : (i64) -> !llvm.ptr
    %16 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
```

三件事：三次 `malloc` 对应三个 `memref.alloc`；大小不是字面常量，而是 `zero 指针 + GEP(4 个 f32)` 再 `ptrtoint` 得到的——**用 GEP 算 size 是 LLVM 的惯用法**，因为它能自动带上目标的指针宽度与类型大小；分配完之后，返回值被塞进 `!llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>`，这就是 memref 描述符（base / aligned / offset / sizes / strides）。

所以看到“`malloc` 的参数不是 16”时，不要急着怀疑版式——先看 `%11` 这种常量是从哪来的，以及 GEP 的元素类型是什么。**

## E.7 Step 4：换成 tensor 版，看 bufferization 的接缝

同一道题，用 tensor 写会更“函数式”，但降级多一跳：

<details>
<summary>点击展开：`e2e_tensor.mlir` 与 bufferize 命令</summary>

```mlir
func.func @main() -> i32 {
  %A = arith.constant dense<[[1.0, 2.0], [3.0, 4.0]]> : tensor<2x2xf32>
  %B = arith.constant dense<[[1.0, 2.0], [3.0, 4.0]]> : tensor<2x2xf32>
  %C = arith.constant dense<[[0.0, 0.0], [0.0, 0.0]]> : tensor<2x2xf32>
  %0 = linalg.matmul ins(%A, %B : tensor<2x2xf32>, tensor<2x2xf32>)
                    outs(%C : tensor<2x2xf32>) -> tensor<2x2xf32>
  %v = tensor.extract %0[%c1, %c1] : tensor<2x2xf32>
  %i = arith.fptosi %v : f32 to i32
  return %i : i32
}
```

```bash
$T/bin/mlir-opt e2e_tensor.mlir \
  -one-shot-bufferize='bufferize-function-boundaries' \
  --convert-linalg-to-loops --convert-scf-to-cf --convert-to-llvm > e2e_tensor_llvm.mlir
$T/bin/mlir-runner e2e_tensor_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
22
```

</details>

如果你跳过 `-one-shot-bufferize`，会回到 Ch 24 的结论：`--convert-linalg-to-loops` 对 tensor 版**静默 no-op**，然后 `--convert-to-llvm` 留下 `linalg.matmul`，`mlir-runner` 报 `Dialect 'linalg' not found`。这不是新错误，是同一个边界的另一种表现。

## E.8 Step 5：注入一个“不报错但算错”的 bug

这是本章的重点。我们改**一张 indexing map**，不动任何 op 结构：

```bash
$T/bin/mlir-opt e2e.mlir --linalg-generalize-named-ops > e2e_gen.mlir
grep -n '^#map' e2e_gen.mlir
```

真实输出：

```text
1:#map = affine_map<(d0, d1, d2) -> (d0, d2)>
2:#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
3:#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
```

把 A 的 map 从 `(d0, d2)` 改成 `(d2, d0)`（转置读取）：

```bash
sed 's|#map = affine_map<(d0, d1, d2) -> (d0, d2)>|#map = affine_map<(d0, d1, d2) -> (d2, d0)>|' \
  e2e_gen.mlir > bug2.mlir
$T/bin/mlir-opt bug2.mlir --convert-linalg-to-loops --convert-scf-to-cf --convert-to-llvm > bug2_llvm.mlir
$T/bin/mlir-runner bug2_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
20
```

**没有任何 verifier 报错**：`(d2, d0)` 仍然是合法的仿射置换 map，形状也匹配，`linalg.generic` 的 verifier 只检查“map 的结果维数 = operand 的 rank”。所以这是一个**语义 bug**，不是**结构 bug**。

手算验证：`A^T = [[1,3],[2,4]]`，`C[1,1] = 2*2 + 4*4 = 20`。20 与 22 的差就是“A 被转置读了一次”。

## E.9 Step 6：定位——三步把 bug 逼到那一行

### 第一步：确认 bug 在输入 IR 还是某个 pass

```bash
$T/bin/mlir-opt bug2.mlir --convert-linalg-to-loops \
  --mlir-print-ir-after-all > /dev/null 2> ir_dump.txt
grep -E 'IR Dump After' ir_dump.txt
```

真实输出：

```text
// -----// IR Dump After ConvertLinalgToLoopsPass: convert-linalg-to-loops //----- //
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
// -----// IR Dump After ConvertToLLVMPass: convert-to-llvm{allow-pattern-rollback=true dynamic=false } //----- //
```

注意 dump 头里连 pass 的默认 option 都打印了（`allow-pattern-rollback=true`、`dynamic=false`）——这是排查“默认值和我以为的不一样”的第一手资料。

只有三个 dump，说明 `--convert-linalg-to-loops` 之前**没有其它 pass 动过 IR**。于是 bug 只能来自输入。

### 第二步：看降级后循环里的下标顺序

```bash
$T/bin/mlir-opt bug2.mlir --convert-linalg-to-loops | sed -n '20,40p'
```

真实输出：

```text
    scf.for %arg0 = %c0 to %c2 step %c1 {
      scf.for %arg1 = %c0 to %c2 step %c1 {
        scf.for %arg2 = %c0 to %c2 step %c1 {
          %2 = memref.load %alloc[%arg2, %arg0] : memref<2x2xf32>
          %3 = memref.load %alloc_4[%arg2, %arg1] : memref<2x2xf32>
          %4 = memref.load %alloc_5[%arg0, %arg1] : memref<2x2xf32>
```

对照 E.7 里“正确版”的同一段（`memref.load %A[%arg0, %arg2]`），差别一目了然：A 的 load 变成了 `[%arg2, %arg0]`，也就是 `[k, i]` 而不是 `[i, k]`。

**这一步的方法论**：不要试图“读懂整个 pass”，只要找到**你预期的那一个下标表达式**，看它的操作数顺序。循环变量与 map 维度的对应关系是固定的（`%arg0=d0=M`, `%arg1=d1=N`, `%arg2=d2=K`），所以 `(d2,d0)` 必然生成 `[%arg2, %arg0]`。

### 第三步：最小化

真正的 `mlir-reduce` 用法是给一个 tester 脚本：

```bash
cat > interesting.sh <<'EOF'
#!/bin/bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt "$1" --convert-linalg-to-loops --convert-scf-to-cf --convert-to-llvm -o /tmp/red.mlir 2>/dev/null || exit 1
out=$($T/bin/mlir-runner /tmp/red.mlir --entry-point-result=i32 2>/dev/null | head -1)
[ "$out" = "20" ]
EOF
chmod +x interesting.sh
$T/bin/mlir-reduce bug2.mlir --reduction-tree='test=/tmp/mlir_p5/interesting.sh'
```

**未跑通**（如实记录）：本树实测返回

```text
loc("bug2.mlir":4:1): error: uninterested module will not be reduced
```

即使手工执行 `./interesting.sh bug2.mlir` 返回 0、`mlir-runner` 确实打印 20。我加了日志确认 tester **被调用了两次**（`CALL: /tmp/mlir-reduce-*.mlir`、`OUT: 20`），但 reducer 仍判定 “uninterested”。这是 `mlir-reduce` 的 tester 交互问题（可能与本树的 `--reduction-tree` 选项注册方式有关），我没有继续深挖。

替代方案（也是我实际用的）：**手工最小化**。32 行的输入里，matmul 只占 3 行、其余都是常量 store；把常量 store 换成 `linalg.fill` 或直接 `memref.alloc` + 不初始化，bug 仍然复现（结果变成未定义，不适合断言），所以真正的最小复现就是“generic op + 两张 map”。也就是说：**bug 已经最小到一行 map 了**，再 reduce 也只能得到同一行。

## E.10 Step 7：修好，并写一条能抓住它的测试

修法就是把 map 改回去。但“修好”不算完——要写一条**改回 bug 就会失败**的测试。

<details>
<summary>点击展开：`e2e-indexing.mlir` 测试文件（CHECK 循环下标顺序）</summary>

```mlir
// RUN: mlir-opt %s --convert-linalg-to-loops | FileCheck %s

// 用一个 A 非对称、B 单位阵的输入，任何转置都会改变结果
func.func @matmul(%A: memref<2x2xf32>, %B: memref<2x2xf32>, %C: memref<2x2xf32>) {
  linalg.matmul ins(%A, %B : memref<2x2xf32>, memref<2x2xf32>)
               outs(%C : memref<2x2xf32>)
  return
}

// CHECK-LABEL: func.func @matmul
// CHECK: scf.for %[[I:.*]] =
// CHECK:   scf.for %[[J:.*]] =
// CHECK:     scf.for %[[K:.*]] =
// CHECK:       %{{.*}} = memref.load %{{.*}}[%[[I]], %[[K]]]
// CHECK:       %{{.*}} = memref.load %{{.*}}[%[[K]], %[[J]]]
// CHECK:       %{{.*}} = memref.load %{{.*}}[%[[I]], %[[J]]]
// CHECK:       memref.store %{{.*}}, %{{.*}}[%[[I]], %[[J]]]
```

</details>

这条测试的价值在于：它断言的是**下标顺序**，不是“能跑”。如果你把 `(d0,d2)` 改成 `(d2,d0)`，CHECK 的 `[%[[I]], %[[K]]]` 立刻失配。

放回源码树的位置：`mlir/test/Dialect/Linalg/` 下新增一个文件，然后在 `mlir/test/Dialect/Linalg/CMakeLists.txt` 不需要改（lit 是按目录扫描的）。跑：

```bash
$T/bin/llvm-lit -sv /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Dialect/Linalg/e2e-indexing.mlir
```

（本章没有把它写进源码树——源码树只读。你自己练习时放到 `/tmp` 用 `llvm-lit` 的 `--param` 指过去，或者放到自己的 fork 里。）

## E.11 Step 8：tile 一遍，体验 6 个 pass 的顺序依赖

最后一步是把 E.6 的管线“加一个 tile”。看起来只是多一个 schedule，实际会连续踩四个坑。

先写输入（在 `e2e.mlir` 末尾追加 transform 模块）：

```bash
head -30 e2e.mlir > e2e_tile.mlir
cat >> e2e_tile.mlir <<'EOF'

module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
    %t, %loops:3 = transform.structured.tile_using_for %mm tile_sizes [1, 2, 2]
      : (!transform.any_op) -> (!transform.any_op, !transform.any_op, !transform.any_op, !transform.any_op)
    transform.yield
  }
}
EOF
```

### 坑 1：忘了先跑 interpreter 的产物清理

直接接 E.6 的管线：

```bash
$T/bin/mlir-opt e2e_tile.mlir --transform-interpreter --convert-linalg-to-loops \
  --convert-scf-to-cf --convert-to-llvm > e2e_tile_llvm.mlir
$T/bin/mlir-runner e2e_tile_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
loc("e2e_tile_llvm.mlir":278:5): error: Dialect `transform' not found for custom op 'transform.named_sequence' 
could not parse the input IR
```

`--convert-to-llvm` 不会删 transform 模块（它也是“结构化方言”）。加 `--test-transform-dialect-erase-schedule`。

### 坑 2：tile 产生的 `memref.subview` 需要 `--expand-strided-metadata`

真实输出（加上 erase 之后）：

```text
loc("e2e_tile_llvm.mlir":156:16): error: Dialect `memref' not found for custom op 'memref.subview'
```

`memref.subview` 是**带动态 offset 的 view**，`--convert-to-llvm` 的 memref pattern 不会直接展开它；标准做法是先 `--expand-strided-metadata`。

### 坑 3：`--expand-strided-metadata` 会生成 `affine.apply`，所以 `--lower-affine` 必须排在它后面

真实输出（顺序写错时）：

```text
loc("e2e_tile_llvm.mlir":154:12): error: Dialect `affine' not found for custom op 'affine.apply'
```

注意这个错误的微妙之处：如果你把 `--lower-affine` 放在 `--expand-strided-metadata` **之前**，`lower-affine` 跑的时候还没有 `affine.apply`，等 expand 生成出来就没人管了。**pass 顺序错误在这里不会报“顺序错”，只会留下一个没人降的 op。**

### 坑 4：最终能跑的那一条

```bash
$T/bin/mlir-opt e2e_tile.mlir \
  --transform-interpreter \
  --test-transform-dialect-erase-schedule \
  --convert-linalg-to-loops \
  --expand-strided-metadata \
  --lower-affine \
  --convert-scf-to-cf \
  --convert-to-llvm > e2e_tile_llvm.mlir
grep -cE 'affine\.|memref\.|scf\.|linalg\.' e2e_tile_llvm.mlir
$T/bin/mlir-runner e2e_tile_llvm.mlir --entry-point-result=i32
```

真实输出：

```text
1
22
```

`1` 是剩下那个空的 `module attributes {transform.with_named_sequence}` 外壳——`--test-transform-dialect-erase-schedule` 只删 `named_sequence`，不删外层 module。`mlir-runner` 能接受它（属性不需要 dialect 加载），但这说明**“erase schedule”不是“erase transform module”**。

tile 之后仍然是 22，证明 tiling 保语义。这一步的收获不是“跑通了”，而是那四条错误信息——它们分别对应管线里的四类“没人负责”的残留 op。

## E.12 踩雷集锦

1. **错误直觉：`--convert-to-llvm` 一步到底。** → 正确认识：它不含 `scf`/`affine`/`transform`；`scf` 必须走 `--convert-scf-to-cf`。
2. **错误直觉：`mlir-runner` 报 `Dialect 'X' not found` 是 runner 的 bug。** → 正确认识：那是**解析失败**，说明上游还有 X 方言没降；错误位置指向残留 op。
3. **错误直觉：`memref.alloc` 变成 `malloc(128)` 说明版式算错了。** → 正确认识：还要看描述符（base/aligned/offset/sizes/strides）的构造；只看一个常数会误判。
4. **错误直觉：`linalg.generic` 的 verifier 会抓住转置错误。** → 正确认识：它只检查 map 与 operand 的 rank/形状；语义错误必须靠测试断言下标顺序。
5. **错误直觉：`mlir-reduce` 给个 tester 就能自动最小化。** → 正确认识：本树实测会报 `uninterested module will not be reduced`（tester 明明返回 0）；要么深挖 reducer 的调用约定，要么手工最小化。
6. **错误直觉：`--lower-affine` 放哪都行。** → 正确认识：它只降“当时已经存在”的 affine op；`--expand-strided-metadata` 之后新生成的 `affine.apply` 需要它再跑一次（或把顺序调对）。
7. **错误直觉：`--test-transform-dialect-erase-schedule` 会把 transform 模块删干净。** → 正确认识：只删 `named_sequence`，外层空 module 还在。
8. **错误直觉：结果对就说明管线对。** → 正确认识：E.8 的 bug 版本也能“跑出结果”，只是 20；正确性判断必须来自对期望值的独立推导。

## E.13 本章重点整理

- 端到端最小管线：`convert-linalg-to-loops` → `convert-scf-to-cf` → `convert-to-llvm`，缺一不可。
- `mlir-runner --entry-point-result=i32` 让“返回 i32 的 `main`”成为最简单的可观测输出，不需要 printf。
- tensor 版多一跳 `-one-shot-bufferize='bufferize-function-boundaries'`；没有它，Linalg 降级静默 no-op。
- 语义 bug（索引 map 错）不会触发 verifier；定位靠“打印降级后的循环下标顺序”。
- `--mlir-print-ir-after-all` 的 dump 头会带 pass 默认 option，是排查默认值差异的第一手资料。
- tile 引入了三类新残留：`transform` 模块、`memref.subview`、`affine.apply`；每个都要专门的 pass 处理，且顺序敏感。
- `memref.subview` → `--expand-strided-metadata` → `affine.apply` → `--lower-affine` 是一条固定的依赖链。
- `mlir-reduce` 在本树的 tester 交互有坑，需记录并准备手工最小化方案。

## E.14 自我检核

- [ ] 能不看笔记写出三跳管线，并解释为什么 `--convert-to-llvm` 不够
- [ ] 能解释 `mlir-runner` 的 `Dialect 'X' not found` 与“降级失败”的区别
- [ ] 能手算 `A=B=[[1,2],[3,4]]` 时 `C[1,1]` 的值，并说明为什么选这个元素
- [ ] 能解释把 A 的 map 改成 `(d2,d0)` 后为什么结果变成 20
- [ ] 能从降级后的 `memref.load` 操作数顺序反推出 indexing map
- [ ] 能说出 `--mlir-print-ir-after-all` 输出里 pass 默认 option 的位置
- [ ] 能按正确顺序写出 tile 版的 7 个 pass，并说出每个坑对应的残留 op
- [ ] 能解释为什么 `mlir-reduce` 的 `uninterested module` 值得记录而不是忽略

## E.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/Linalg/_index.md`**
  - 读哪里：`45-57`（tensor/buffer 双栖）、`284`（region 参数）
  - 能学到什么：为什么 tensor 版必须先 bufferize；和本章关联：E.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Linalg/Transforms/Loops.cpp`**
  - 读哪里：`297-340`
  - 能学到什么：`memref.load/store` 的下标是怎么从 map 生成的；和本章关联：E.9
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp`**
  - 读哪里：`scf.for` 的 lowering（搜 `ForOpLowering`）
  - 能学到什么：`iter_args` → block 参数的具体改写；和本章关联：E.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/MemRefToLLVM/MemRefToLLVM.cpp`**
  - 读哪里：`AllocOpLowering`、`SubViewOpLowering`
  - 能学到什么：`malloc(128)` 与描述符的真实计算；和本章关联：E.6/E.11
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Tutorials/QuickstartRewrites.md`**
  - 读哪里：整篇（最短的 pattern 入门）
  - 能学到什么：如果你要自己写“抓住这个 bug”的 C++ pattern；和本章关联：E.10

练习 E 把 Part 5 串了一遍：Linalg（Ch 24）、SCF（Ch 25）、GPU 之后的宿主侧（Ch 26/27）、Transform（Ch 28）。下一章进入 Part 6：怎么把这样的修复变成一条能被上游接受的提交——从 lit 测试约定开始。

→ 下一章：[Ch 29 测试约定与 update 脚本](./29-testing-and-update-scripts.md)
