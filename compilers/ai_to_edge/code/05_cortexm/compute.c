/* 被编译成 Cortex-M3 代码的「算子」。没有 libc、没有堆。 */
#include <stdint.h>

int32_t compute(int32_t a, int32_t b) {
  int32_t acc = 0;
  for (int32_t i = 0; i < b; i++) acc += a;
  return acc;
}
