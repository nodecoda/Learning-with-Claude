/* 驱动：调用 MLIR 降级后的 matmul。
   关键点：memref 降级后不再是一个结构体指针，而是拆成 7 个标量参数。
   顺序：allocated, aligned, offset, size0, size1, stride0, stride1 */
#include <stdio.h>

extern void matmul(
    float*, float*, long, long, long, long, long,
    float*, float*, long, long, long, long, long,
    float*, float*, long, long, long, long, long);

int main(void) {
  float A[4*8], B[8*4], C[4*4] = {0};
  for (int i = 0; i < 32; i++) A[i] = (float)(i % 5);
  for (int i = 0; i < 32; i++) B[i] = (float)(i % 3);

  matmul(A,A,0, 4,8, 8,1,
         B,B,0, 8,4, 4,1,
         C,C,0, 4,4, 4,1);

  for (int i = 0; i < 4; i++) {
    for (int j = 0; j < 4; j++) printf("%.1f ", C[i*4+j]);
    printf("\n");
  }
  return 0;
}
