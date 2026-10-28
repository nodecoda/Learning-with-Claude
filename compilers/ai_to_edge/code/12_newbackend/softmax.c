/* 练习 D 的样本：一个带归约、除法、exp 的算子，适合观察后端差异
 * 环境：LLVM 14 / aarch64-linux-gnu-gcc 11.4 / arm-none-eabi-gcc 10.3 / riscv64-linux-gnu-gcc 11.4
 * 运行：./build.sh
 */
#include <stdint.h>

#define N 8

/* 这条链上有：归约(max) → 减法 → 除法 → 比较，覆盖了
 * 浮点、整数、控制流三类指令，足以暴露后端差异。 */
void softmax_i32(const int32_t *in, int32_t *out, int32_t scale) {
  int32_t mx = in[0];
  for (int i = 1; i < N; i++) if (in[i] > mx) mx = in[i];   /* 归约 */
  int32_t sum = 0;
  for (int i = 0; i < N; i++) { out[i] = (in[i] - mx) * scale; sum += out[i]; }
  for (int i = 0; i < N; i++) out[i] = out[i] / sum;        /* 除法 */
}

/* 整数版没有 exp，但除法在 Cortex-M3 上是库调用（i64）或硬件（i32）。
 * 再加一个 i64 除法，专门触发 __aeabi_ldivmod。 */
int64_t div_i64(int64_t a, int64_t b) { return a / b; }
