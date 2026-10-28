#include <stdio.h>
#include <stdint.h>
int main(void){
  /* K=256 个 127*127 累加：int32 的上界是 2.1e9，这里 4.1e6，安全 */
  int32_t acc32=0; int16_t acc16=0;
  for(int i=0;i<256;i++){ acc32 += 127*127; }
  for(int i=0;i<256;i++){ acc16 = (int16_t)(acc16 + 127*127); }  /* 显式截断，模拟 overflow */
  printf("K=256, all 127*127:\n");
  printf("  int32 = %d   (true = %d)\n", acc32, 127*127*256);
  printf("  int16 = %d   (溢出了，差 %d)\n", acc16, acc32 - acc16);
  printf("  int32 上限 2147483647 / (127*127) = %d 次乘加才溢出\n", 2147483647/(127*127));
  /* int8*int8 的乘积上界 */
  printf("  (-128)*(-128) = %d  <- 超出 int8 上界 127\n", 128*128);
  printf("  127*127 = %d\n", 127*127);
  return 0;
}
