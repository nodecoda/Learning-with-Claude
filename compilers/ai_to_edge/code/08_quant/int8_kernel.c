/* int8 矩阵乘的完整整数路径：int8 × int8 → int32 累加 → 重新缩放回 int8
 * 环境：gcc 11.4 / aarch64-linux-gnu-gcc 11.4
 * 运行：gcc -O2 int8_kernel.c -o int8_kernel && ./int8_kernel
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

#define M 4
#define N 4
#define K 8

/* 量化参数：scale 是实数步长，zp 是零点 */
typedef struct { float scale; int32_t zp; } qparam;

/* 整数矩阵乘：
 *   累加器必须是 int32 —— int8*int8 最大 127*127=16129，
 *   累加 K 次，K=256 时上界 4.1e6，仍在 int32 内；用 int16 早就溢出。
 *   零点修正：sum((a-za)(b-zb)) = sum(a*b) - za*sum(b) - zb*sum(a) + K*za*zb
 */
void matmul_i8(const int8_t *A, const int8_t *B, int32_t *C,
               int m, int n, int k,
               qparam qa, qparam qb) {
  for (int i = 0; i < m; i++) {
    for (int j = 0; j < n; j++) {
      int32_t acc = 0;
      for (int t = 0; t < k; t++) {
        int32_t a = (int32_t)A[i * k + t] - qa.zp;
        int32_t b = (int32_t)B[t * n + j] - qb.zp;
        acc += a * b;
      }
      C[i * n + j] = acc;
    }
  }
}

/* 重新缩放：int32 -> int8，输出 scale = qa.scale * qb.scale
 * 真实硬件这里做定点乘法 + 右移（乘数用 int32 表示的小数），
 * 这里用浮点演示同一件事，便于和 PyTorch 对齐。 */
void requant_i32_i8(const int32_t *C, int8_t *out, int len,
                    float out_scale, int32_t out_zp) {
  for (int i = 0; i < len; i++) {
    int32_t v = (int32_t)lroundf((float)C[i] * out_scale) + out_zp;
    if (v < -128) v = -128;
    if (v > 127)  v = 127;
    out[i] = (int8_t)v;
  }
}

/* 参考实现：浮点 */
void matmul_f32(const float *A, const float *B, float *C, int m, int n, int k) {
  for (int i = 0; i < m; i++)
    for (int j = 0; j < n; j++) {
      float acc = 0;
      for (int t = 0; t < k; t++) acc += A[i * k + t] * B[t * n + j];
      C[i * n + j] = acc;
    }
}

static float max_abs_diff(const float *a, const float *b, int n) {
  float mx = 0;
  for (int i = 0; i < n; i++) {
    float d = a[i] - b[i];
    if (d < 0) d = -d;
    if (d > mx) mx = d;
  }
  return mx;
}

int main(void) {
  /* 固定数据，便于复现 */
  int8_t A[M * K], B[K * N];
  float Af[M * K], Bf[K * N];
  srand(12345);
  for (int i = 0; i < M * K; i++) { A[i] = (int8_t)(rand() % 21 - 10); Af[i] = A[i]; }
  for (int i = 0; i < K * N; i++) { B[i] = (int8_t)(rand() % 21 - 10); Bf[i] = B[i]; }

  qparam qa = {1.0f, 0}, qb = {1.0f, 0};   /* 本例 A/B 已是精确整数，scale=1 */

  int32_t C_i32[M * N];
  int8_t  C_i8[M * N];
  float   C_f32[M * N], C_deq[M * N];

  matmul_i8(A, B, C_i32, M, N, K, qa, qb);
  matmul_f32(Af, Bf, C_f32, M, N, K);

  /* int32 累加结果与浮点结果应完全一致（本 case 数值小，无舍入） */
  int exact = 1;
  for (int i = 0; i < M * N; i++) if ((float)C_i32[i] != C_f32[i]) exact = 0;
  printf("int32 accum == float ref : %s\n", exact ? "yes" : "no");

  /* 输出量化到 int8：这里 scale=1 会让累加结果溢出 int8，正好演示饱和 */
  int32_t mx = 0;
  for (int i = 0; i < M * N; i++) if (C_i32[i] > mx) mx = C_i32[i];
  float out_scale = 127.0f / (float)mx;   /* 映射到 int8 满量程 */
  requant_i32_i8(C_i32, C_i8, M * N, out_scale, 0);

  printf("int32 max = %d\n", mx);
  printf("out_scale = %.8f  (1/out_scale = %.4f)\n", out_scale, 1.0f / out_scale);

  /* 饱和计数：不做 scale 直接截断会溢出多少 */
  int sat = 0;
  for (int i = 0; i < M * N; i++) if (C_i32[i] > 127 || C_i32[i] < -128) sat++;
  printf("elements needing saturation if scale=1 : %d / %d\n", sat, M * N);

  for (int i = 0; i < M * N; i++) C_deq[i] = (float)C_i8[i] / out_scale;
  printf("int8 round-trip max|err| = %.6f  (相对 max %.6f)\n",
         max_abs_diff(C_deq, C_f32, M * N),
         max_abs_diff(C_deq, C_f32, M * N) / mx);

  printf("\nfirst row, int32 -> int8 -> dequant\n");
  for (int j = 0; j < N; j++)
    printf("  %6d -> %5d -> %8.3f  (ref %8.3f)\n", C_i32[j], C_i8[j], C_deq[j], C_f32[j]);
  return 0;
}
