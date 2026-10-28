# Ch 22 内存规划：边缘设备没有 malloc 的自由

> **目标**：搞清楚一个模型从「每层都申请一块新 buffer」变成「一整块 arena 反复复用」的过程中，编译器做了哪些决定；能判断哪两个张量可以共享地址，能算出 arena 要多大，能说清对齐和 stride 为什么会改变生成的指令。
>
> **环境**：Ubuntu 22.04 / x86_64 AVX2 / gcc 11 / `aarch64-linux-gnu-gcc` 11.4 / LLVM 14 / torch 2.9.1+cpu

## 为什么需要这个？

Ch 21 结束在调用约定上：一个 memref 变成 7 个参数，指针只是第一个。指针指向的内存从哪来？上一章里它像是凭空出现的。

服务器上的答案是 `malloc`。边缘设备上它经常不成立：MCU 的 RAM 可能只有 64 KB，链接脚本在编译期就分完每一字节，运行期没有堆；就算有堆，碎片会让「今天能跑、跑一夜后崩」成为常态；`free` 的耗时不确定，每 10 ms 一帧的实时性没法保证。更根本的是，模型的内存需求在**编译期就能算出来**，算得出来就没理由留到运行期猜。

所以内存规划不是「优化」，而是**把运行期的分配问题提前到编译期解决**。这一章讲这个提前的过程，以及它会踩到的坑。

## 先建立直觉

把「每层申请新 buffer」想成每次做饭都买一套新锅：

```
第 1 层：买锅 A（16 MB），用完不洗，放着
第 2 层：买锅 B（64 MB），用完放着
第 3 层：买锅 C（16 MB），用完放着
...
第 20 层：厨房放不下了
```

正确做法是先看哪两口锅不会同时用，再让它们共用一个位置：

```
第 1 层用位置 0（16 MB）      → 第 3 层也用位置 0
第 2 层用位置 16 MB（64 MB）  → 第 5 层也用位置 16 MB
```

这里有两个动作，很多人混成一个：

| 动作 | 省的是什么 | 本章实测 |
|---|---|---|
| **复用**（liveness 分析） | 峰值内存 | 192 MB 总和 → 80 MB arena |
| **arena**（一次分配、偏移量切割） | 分配开销、碎片、确定性 | 纯分配 24.1 ns → 1.2 ns |

复用省「总量」，arena 省「开销」。两者经常一起出现，但**不能互相替代**。这个区别是本章的主线，后面所有实测都在给它做注脚。

## 底层机制

### 1. 张量生命周期与 liveness 区间

liveness 区间是「第一次被写」到「最后一次被读」之间的那段执行顺序。

```
x   [0,1)
w1  [1,4)
h1  [1,2)
w2  [4,7)      ← 和 w1 的区间 [1,4) 不重叠
h2  [4,5)
out [5,6)
```

判断两个张量能否共享地址，条件只有一条：**区间不重叠**。「写后读」和「读后写」的边界都要算进去：如果 `w2` 在第 4 步被写，而 `w1` 在第 4 步还被读，那它们就重叠了。区间用半开写法 `[start, end)` 就是为了让「第 4 步」只属于一边。

真实的调度器用的就是这一条，加上若干启发式：优先复用大小接近的块、把大块放低地址、避免把同一块反复分给对齐要求不同的张量。Inductor 的实现可以直接在 `torch/_inductor/memory.py` 里读到（`compute_memory_timeline`、`reorder_for_peak_memory`，本机 torch 2.9.1 路径可查）；XLA 叫 buffer assignment，TVM 叫 `StorageRewrite`。

### 2. 原地复用的合法性条件

Ch 9 实测的那个 kernel 只有一个指针：

```cpp
extern "C" void kernel(float* in_out_ptr0) {
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_out_ptr0 + x0, 8);
    auto tmp3 = tmp0 + tmp2;
    auto tmp4 = at::vec::clamp_min(tmp3, decltype(tmp3)(0));
    tmp4.store(in_out_ptr0 + x0);
}
```

`in_out_ptr0` 既读又写。这在 elementwise 上合法，因为**第 i 个输出只依赖第 i 个输入**：写进去的值不会被后面任何一次读用到。

换成别的算子就不成立：

| 算子 | 原地安全？ | 原因 |
|---|---|---|
| `relu(x + 1)` | 安全 | 输出 i 只读输入 i |
| `x.transpose(0,1)` | 不安全 | 输出 (i,j) 读输入 (j,i)，会覆盖尚未读的元素 |
| `y = x.sum(dim=0)` | 不安全 | 多个输入汇聚到一个输出，覆盖会污染后续累加 |
| `C = A @ B`，且 `C` 与 `A` 同址 | 不安全 | 内层循环还在读 A 的行 |

判定原地的三个条件，缺一不可：**形状与 stride 完全一致、逐元素映射（没有跨元素依赖）、输入在此之后没有别的读者**。第三条才是难点，它需要准确的 liveness。

Inductor 生成的代码把这件事写得很直白（本章实测，`TORCH_LOGS=output_code`）：

```python
buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
extern_kernels.mm(arg0_1, arg1_1, out=buf0)
del arg0_1
del arg1_1
buf1 = buf0; del buf0  # reuse
cpp_fused_add_relu_0(buf1)
```

`buf1 = buf0` 就是复用：`add+relu` 直接写在 matmul 的输出上，不再申请第二块 64×64。`del arg0_1` 的时机说明了为什么矩阵乘的输出不能和输入同址——`mm` 还在读 `arg0_1`。

### 3. arena：一次 malloc，之后只用偏移量

arena（也叫 memory pool、bump allocator）只有三个字段：

```c
typedef struct {
  unsigned char *base;   /* 一次 malloc 拿到的大块 */
  size_t cap;            /* 总容量 */
  size_t off;            /* 当前水位，只增不减 */
} Arena;

void *arena_alloc(Arena *a, size_t n, size_t align) {
  size_t p = (a->off + align - 1) & ~(align - 1);   /* 向上对齐 */
  if (p + n > a->cap) return NULL;                  /* 失败要显式处理 */
  void *q = a->base + p;
  a->off = p + n;
  return q;
}

void arena_reset(Arena *a) { a->off = 0; }          /* 整块回收，O(1) */
```

三个性质决定了它适合边缘：

1. **分配是几条整数运算**，没有锁、没有链表遍历、没有系统调用。
2. **`reset` 是 O(1)**。一帧推理结束，水位归零，整块立刻可重用，不存在碎片。
3. **地址可预测**。同样的输入序列永远得到同样的偏移，实时性和调试都需要这一点。

代价也很明确：**不能单独释放中间某一块**。推理不是问题（一帧内中间张量同时死亡），训练就是问题。

### 4. 对齐、stride 与 padding

AArch64 上 16 字节对齐不是审美问题，它决定编译器能选哪条指令。

实测（`aarch64-linux-gnu-gcc -O2 -S`）：

```c
struct S4  { float v[4]; };                          /* 16 B，默认 4 字节对齐 */
struct S4a { float v[4]; } __attribute__((aligned(16)));
void c4 (struct S4  *d, const struct S4  *s) { *d = *s; }
void c4a(struct S4a *d, const struct S4a *s) { *d = *s; }
```

```asm
c4:                              c4a:
    ldp  x2, x3, [x1]                ldp  x2, x3, [x1]
    stp  x2, x3, [x0]                stp  x2, x3, [x0]
    ret                              ret
```

两者都用了 `ldp`/`stp`（一次搬 16 字节）。不是因为结构体本身对齐，而是因为 AArch64 默认允许非对齐访问。把对齐检查打开，差别立刻出现：

```bash
aarch64-linux-gnu-gcc -O2 -mstrict-align -S al2.c
```

```asm
c4:                              c4a:
    ldr  w2, [x1]                    ldp  x2, x3, [x1]
    str  w2, [x0]                    stp  x2, x3, [x0]
    ldr  w2, [x1, 4]                 ret
    str  w2, [x0, 4]
    ldr  w2, [x1, 8]
    str  w2, [x0, 8]
    ldr  w1, [x1, 12]
    str  w1, [x0, 12]
    ret
```

`c4` 退化成 4 组 32 位搬运（8 条指令），`c4a` 仍是 2 条。**`aligned(16)` 给了编译器用 `ldp` 的资格**，在 `-mstrict-align` 下这个资格就是全部差别。

32 字节的结构体：不加 `-mstrict-align` 时是 `ldp q0, q1` / `stp q0, q1`（一次 32 字节）；加上之后 GCC 不再内联，改成 `bl memcpy`。这对裸机是致命的，`memcpy` 得你自己提供（Ch 24 踩这个坑）。

NEON 的情况要分清。写 `vld1q_f32` / `vst1q_f32`，GCC 和 clang 都生成：

```asm
vadd4:
    ldr  q0, [x1]
    ldr  q1, [x2]
    fadd v0.4s, v0.4s, v1.4s
    str  q0, [x0]
    ret
```

不是 `ld1 {v0.4s}`，而是 `ldr q0`。两者都是 16 字节搬运，区别在非对齐时的行为：`ld1`/`st1` 在架构上不要求对齐；`ldr q` 受 `SCTLR_ELx.A` 控制，A=1 时非对齐访问触发对齐异常。**`ld1` 是「保证能用」，`ldr q` 是「默认能用，取决于系统配置」**。打开严格对齐检查的裸机项目，同一份 NEON 代码会在换编译器后突然崩溃。

对齐的第二个后果在 stride 上。1001 列 float 矩阵的行宽是 4004 字节：

```
row_bytes=4004   row[0] % 16 = 0   row[1] % 16 = 4
padded_row_bytes=4016   row[0] % 16 = 0   row[1] % 16 = 0
```

第 1 行开始就偏 4 字节。补到 1004 列后行行对齐，代价是每行多 3 个 float（12 字节），1000 行就是 12000 字节。这就是「padding 换对齐」的账。

memref 描述符里只有 sizes / strides / offset，**没有 alignment 字段**。padding 必须写进 stride：

```
memref<1x1004xf32, strided<[1004, 1]>>   /* 行间跨 4016 B */
```

只改分配尺寸却忘了改 stride，第 1 行之后全部错位，而且错得有规律（每行偏 12 字节），看起来像「数值精度问题」。

## 实测：体积里的固定成本

先看 Ch 4 最简单的 AArch64 静态链接产物：

```
   text	   data	    bss	    dec	    hex	filename
 512834	  22520	  22256	 557610	  8822a	add_a64
```

`add.c` 一行 `int add(int a,int b){return a*b+1;}`，`drv.c` 只做一次 `printf`。`size` 的 `text` 列 512834 B（含代码与只读数据，单看 `.text` 是 355300 B），静态数据 22520 + 22256 = **44776 B**。

一个 64×64 的 float 矩阵是 16384 B。这个「什么都不做」的程序，静态数据是一个 64×64 权重矩阵的 2.7 倍。很多教学用的手写小模型，全部权重还不如这里 glibc 的全局状态大。

这就是静态链接边缘程序的第一课：**你的 libc 比你的模型重**。

同一份 `add.c` + `drv.c`，换架构（`-static -O2`）：

| 目标 | 文件大小 |
|---|---|
| x86_64-linux-gnu | 900328 B |
| aarch64-linux-gnu | 641056 B |
| aarch64 当前目录里的 `add_a64` | 641112 B |

aarch64 那一格在本机重复三次稳定得到 641112，没复现出 641056，差 56 字节；x86_64 那一格重跑得到 900360，与 900328 差 32 字节。两处都在元数据级别（`.comment` / build-id 一类），不是代码差异——`.text` 本身完全一致：x86_64 是 609784 B，aarch64 是 355300 B。aarch64 比 x86_64 小 28.8%，主因是 x86_64 的静态 glibc 带更多 IFUNC 与多版本实现。

被拉进来的源归档是：

```
/usr/aarch64-linux-gnu/lib/libc.a   4939918 B
```

4.9 MB 的归档，取几个成员就得到 641 KB。链接器按需取成员，但**一个成员内部是整体拉入的**。`printf` 一进来，`vfprintf`、locale、`malloc` 的依赖链跟着进来。边缘程序最后都在想办法绕开 `printf`，原因就在这里。

## 例子一：malloc 版与 arena 版的分配开销

两个版本做的事完全一样：申请 3 块 4096 B，memset，读一个字节，释放，跑 500000 轮。

```c
/* A 用 malloc/free，B 用 arena_alloc + 每轮 reset；其余完全相同 */
for (int i = 0; i < ITERS; i++) {          /* ITERS = 500000，SMALL = 4096 */
  void *p[3];
  for (int j = 0; j < 3; j++) {
    p[j] = version == 'A' ? malloc(SMALL) : arena_alloc(&a, SMALL, 16);
    memset(p[j], i + j, SMALL);
  }
  for (int j = 0; j < 3; j++) { sink += *(unsigned char *)p[j]; if (version == 'A') free(p[j]); }
  if (version == 'B') arena_reset(&a);
}
```

计时用 `clock_gettime(CLOCK_MONOTONIC)`，去掉 memset 再看纯分配：

```bash
gcc -O2 -Wall -Wextra arena_bench.c -o arena_bench
./arena_bench time_malloc_raw
./arena_bench time_arena_raw
```

实测（`taskset` 绑单核跑 8 轮，取第 1 轮）：

| 模式 | 总耗时 | 每次分配 |
|---|---|---|
| malloc/free，纯分配 | 36.183 ms | 24.1 ns |
| arena，纯分配 | 1.847 ms | 1.2 ns |
| malloc/free + memset | 131.400 ms | 87.6 ns |
| arena + memset | 98.801 ms | 65.9 ns |

**纯分配 20 倍，带 memset 只剩 1.34 倍。**

这是本章最该记住的落差点：arena 把分配器从 profile 里抹掉了（1.2 ns 已接近测量噪声），可一旦开始碰 4 KiB 内存，`memset` 的代价盖过一切。**arena 优化的是分配开销，不是访存开销。** 八轮波动：纯分配 24.1–24.8 ns / 1.2 ns；带 memset 87.3–87.7 ns / 65.6–65.9 ns，都在 1% 以内。**同一条命令在 load average 11.9 的机器上跑出过 50.8 / 2.1 / 142.9 / 98.0，整体慢一倍——不绑核、不报负载的基准数字只能用来比方向，不能用来比绝对值。**

**把块改小，结论会变。** `SMALL` 改成 64（同样 500000 轮 × 3 块，3 轮）：

```
mode=time_malloc ... bytes=64 total_ms=17.760 ns_per_alloc=11.8
mode=time_arena  ... bytes=64 total_ms=4.796  ns_per_alloc=3.2
mode=time_malloc ... bytes=64 total_ms=17.497 ns_per_alloc=11.7
mode=time_arena  ... bytes=64 total_ms=3.940  ns_per_alloc=2.6
```

**3.7–4.5 倍**，比 4 KiB 时的 1.34 倍明显。原因变了：64 字节的 memset 只剩几纳秒，分配器的 11.8 ns 重新成为主导。真实模型两种尺寸都有（大激活 + 小的元数据），收益要按尺寸分布加权。

### 一个必须先说的坑：不写 `sink`，实验测的是空气

第一版脚本是这样：

```c
void *p[3];
for (int j = 0; j < 3; j++) p[j] = malloc(SMALL);
for (int j = 0; j < 3; j++) free(p[j]);
```

实测两次都是 `total_ms=0.1555` / `per_alloc_ns=0.10`。反汇编 `main`，循环体只剩：

```asm
10c0:  sub  $0x1,%eax
10c3:  jne  10c0
```

**没有一次 `call malloc`。** `p` 只被写、从没被读，GCC 判定这对 `malloc`/`free` 没有可观测副作用，把整段删了。修法是让指针值逃逸：`sink += (uintptr_t)p[j];`，数字才跳到 50 ns 量级。**优化器会删掉你的基准测试，而且删得悄无声息。** 判断方法就是看汇编里有没有 `call malloc`。Ch 26 会把这类陷阱系统化。

## 例子二：从 liveness 到 arena 布局

把 6 个张量真的排一遍。算法是「按顺序放置，每个张量找最低的、不与任何存活张量重叠的偏移」：

```c
static int clash(Tensor *base, int idx, size_t off, size_t bytes) {
  Tensor *me = &base[idx];
  for (int i = 0; i < idx; i++) {
    if (!base[i].placed) continue;
    int live_overlap  = base[i].start < me->end && me->start < base[i].end;
    int range_overlap = off < base[i].offset + base[i].bytes
                     && base[i].offset < off + bytes;
    if (live_overlap && range_overlap) return 1;   /* 真正的冲突 */
  }
  return 0;
}
```

实测输出：

```
mode=liveness sum_mb=192 max_live_mb=80 arena_mb=80
  x    [0,1)   16 MB -> offset 0 MB
  w1   [1,4)   64 MB -> offset 0 MB
  h1   [1,2)   16 MB -> offset 64 MB
  w2   [4,7)   64 MB -> offset 0 MB
  h2   [4,5)   16 MB -> offset 64 MB
  out  [5,6)   16 MB -> offset 64 MB
```

读法：

- `x` 用完（第 1 步结束）后 `w1` 拿走位置 0，因为区间不重叠。
- `h1` 和 `w1` 都活在 `[1,2)`，必须错开，所以 `h1` 挪到 64 MB。
- `w2` 接在 `w1` 的 `[1,4)` 之后，重新拿回位置 0。
- 总和 192 MB，峰值 80 MB，arena 也是 80 MB。

最后一行要盯：**arena 大小等于峰值，不是总和，也不是峰值的某种折扣**。`w1` 是 64 MB、`h1` 是 16 MB，最大存活集合正好 80 MB，布局再巧也压不下去。峰值是下界，不是优化目标。

顺带一个实现细节：判定 `clash` 时如果把「当前张量」也放进循环里比较，就会自己和自己重叠，导致所有偏移都判为冲突、最后全落在 0。我第一版就是这么错的（少传了一个 `bytes`、把 `&t[i]` 当成「已有张量」传进去），输出是 `arena_mb=64`、6 个张量全在 0——看似「极致复用」，实际是每层都覆盖别人的数据。

## 峰值内存的对照实验

第三组实验回答「arena 到底省不省峰值」。128 层，每层 3 块 1 MB，三种做法：

| 做法 | 峰值 RSS |
|---|---|
| 全留（每层都申请新 buffer 且不释放） | 395856 KB |
| malloc/free，逐层释放 | 4208 KB |
| arena，每层 reset | 4088 KB |

（`getrusage(RUSAGE_SELF).ru_maxrss` 与 `/usr/bin/time -v` 的 `Maximum resident set size` 在这几组上一致。）第一个数字说明复用有多重要：384 MB 的申请量，峰值 RSS 395856 KB，接近全量。它和 arena 无关——**只做 liveness 复用、不做 arena，也能从 395 MB 掉到 4 MB**。

后两个说明 arena 不省峰值：4208 KB 对 4088 KB。另两轮分别是 4256 对 4060、4140 对 4012，方向三次都是 arena 略低。差异都在 200 KB 以内，属于页级噪声。arena 水位 3072 KB，但 glibc 的 `malloc` 本来就按页向内核要，这个规模上碎片不显著。

所以正确的因果是：

```
liveness 复用  → 峰值从 395 MB 降到 4 MB
arena          → 峰值不变，分配开销从 24.1 ns 降到 1.2 ns
```

把两件事混着讲，是本章最常见的误读。

## 踩雷集锦

1. **以为 `torch.empty` 不占内存，`torch.zeros` 才占。**
   `empty` 只是不初始化内容，虚拟地址照样映射。实测（`/proc/self/status` 的 `VmRSS`）：

   ```
   baseline rss_kb 211240
   after torch.empty(1GiB) rss_kb 211240
   after fill_(1) rss_kb 1257260
   after del rss_kb 208936
   ```

   `empty` 之后 RSS 没涨，`fill_(1)` 之后涨了约 1 GB。「`empty` 不占内存」在半数场景下成立——**只要你不碰它**。推理要碰每一页，碰的瞬间 RSS 就上去了。
   **纠正**：用峰值 RSS 或 `/usr/bin/time -v` 量，不要用「我调用的是 empty」推断。虚拟内存和常驻集是两本账。

2. **以为 arena 能省总内存。**
   它省的是碎片、分配开销和不确定性。峰值由最大存活集合决定，arena 改变不了这个下界。实测 4088 KB 对 4208 KB，没有收益。
   **纠正**：要降峰值，先做 liveness 复用和 in-place；arena 是这之后的事。

3. **忘了对齐，然后怀疑编译器。**
   行宽 4004 字节时第 1 行 `% 16 == 4`；`-mstrict-align` 下 16 字节拷贝从 `ldp/stp` 退化成 4 组 `ldr/str`，32 字节拷贝直接变成 `bl memcpy`（裸机上就是未定义符号）。
   **纠正**：所有张量缓冲至少 16 字节对齐，行宽向上补到 16 的倍数，并把补后的值写进 memref 的 stride。改完看汇编，确认 `ldp`/`ldr q` 回来了。

4. **以为 in-place 只跟形状有关。**
   `relu(x+1)` 能原地，`transpose` 不能，`sum(dim=0)` 不能，`A@B` 输出和 A 同址不能。形状一致只是三个条件里的第一个。
   **纠正**：判定原地时同时检查「逐元素映射」和「输入此后无读者」。不确定就退回分配新 buffer，正确性优先。

5. **用「申请了多少」当内存预算。**
   384 MB 申请量对应 395856 KB RSS 是巧合（每块都写满）。真实模型里没被碰过的页不占 RSS，一旦某层触发就跳上去。
   **纠正**：预算按峰值 RSS 算，并用真实输入跑一遍，不要用静态 shape 推。

## 本章重点整理

- liveness 区间不重叠是共享地址的充分条件；区间是半开的 `[start, end)`，写和读的边界各算一步。
- in-place 需要三个条件同时成立：形状与 stride 一致、逐元素映射、输入此后无读者。Ch 9 的 `in_out_ptr0` 满足全部三条。
- arena 是「一次 malloc + 偏移量切割 + O(1) reset」。它消掉分配开销和碎片，不改变峰值。
- 实测：纯分配 24.1 ns → 1.2 ns（20 倍）；带 4 KiB memset 后 87.6 ns → 65.9 ns（1.33 倍）。
- 实测：全留 395856 KB，逐层释放 4208 KB，arena 4088 KB。峰值靠复用，不靠 arena。
- AArch64 上 16 字节对齐让 `ldp/stp` 有资格被选中；`-mstrict-align` 下不对齐的结构体退化成 4 组 32 位搬运，32 字节结构体直接变成 `memcpy` 调用。
- 静态链接的体积里 libc 是主角：`text` 512834 B、静态数据 44776 B，源归档 `libc.a` 4939918 B。

## 自我检核

- [ ] 能画出 6 个张量的 liveness 区间，并手算出哪个可以复用哪个的地址
- [ ] 能说出 in-place 的三个条件，并判断 `transpose` / `sum(dim=0)` / `A@B` 各自为什么不满足
- [ ] 能解释 arena 的 `reset` 为什么是 O(1)，以及它为什么不能单独释放中间块
- [ ] 能复述 24.1 ns → 1.2 ns 与 87.6 ns → 65.9 ns 这两个数字，并说明差异来自哪里
- [ ] 能解释 `-mstrict-align` 为什么让 `ldp` 变成 4 组 `ldr`，以及裸机上 `bl memcpy` 意味着什么
- [ ] 能说明 `torch.empty` 之后 RSS 为什么可能不涨，以及什么时候一定会涨

## 延伸阅读

- **[glibc wiki — Malloc Internals](https://sourceware.org/glibc/wiki/MallocInternals)**
  - **读哪里**：`Chunks`、`Bins`、`Free` 三节，重点看 chunk header 与 size 对齐规则
  - **学什么**：一次 `malloc(4096)` 背后做了什么，为什么会有 16 字节对齐保证
  - **和本章关联**：24.1 ns 的来源，也是「arena 为什么能快 20 倍」的对照面

- **[What Every Programmer Should Know About Memory](https://people.freebsd.org/~lstewart/articles/cpumemory.pdf)** — Ulrich Drepper, 2007
  - **读哪里**：第 3 章（Virtual Memory）、第 6 章（NUMA 与 page fault）
  - **学什么**：虚拟地址、页表、RSS 与 page fault 的关系
  - **和本章关联**：`torch.empty` 占不占内存这个坑的完整原理

- **[MLIR Builtin Dialect — MemRef](https://mlir.llvm.org/docs/Dialects/Builtin/)**
  - **读哪里**：`memref` 类型定义，尤其是 strided layout 与 descriptor 字段
  - **学什么**：sizes / strides / offset 各管什么，为什么没有 alignment
  - **和本章关联**：padding 必须写进 stride 的规范依据

- **[ARM Architecture Reference Manual (Armv7-M)](https://developer.arm.com/documentation/ddi0403/latest/)**
  - **读哪里**：A3.3（Alignment）、A4.6（`LDM`/`STM` 对齐要求）、B3.2（`SCTLR.A`）
  - **学什么**：非对齐访问在什么条件下合法、什么条件下触发异常
  - **和本章关联**：`ldr q` 与 `ld1` 行为差异的架构依据

- **[LLVM Passes 文档](https://llvm.org/docs/Passes.html)**
  - **读哪里**：`-mem2reg`、`-licm`、`-gvn` 条目里的前提假设
  - **学什么**：编译器在什么前提下才敢把两块内存判为「不重叠」
  - **和本章关联**：内存规划靠的正是这类「不重叠」证明；证明失败只能保守分配

内存的账算清了，下一个问题更实际：这些张量上的算子，是自己生成循环，还是调一个别人写好的库？

→ [Ch 23 算子库还是代码生成](./23-libraries-vs-codegen.md)
