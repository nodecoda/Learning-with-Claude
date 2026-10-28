/* 看编译器给 int8 与 float32 的向量宽度差多少 */
#include <stdint.h>
#define N 4096
void add_i8(int8_t *restrict a, const int8_t *restrict b) {
  for (int i = 0; i < N; i++) a[i] = (int8_t)(a[i] + b[i]);
}
void add_f32(float *restrict a, const float *restrict b) {
  for (int i = 0; i < N; i++) a[i] = a[i] + b[i];
}
int32_t dot_i8(const int8_t *a, const int8_t *b, int n) {
  int32_t s = 0;
  for (int i = 0; i < n; i++) s += (int32_t)a[i] * (int32_t)b[i];
  return s;
}
