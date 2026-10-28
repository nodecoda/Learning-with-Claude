#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "== 1) 原生 x86_64 vs qemu 模拟 =="
cat > /tmp/k.c <<'C'
#include <stdio.h>
#include <time.h>
float buf[8192];
int main(){ struct timespec a,b;
  clock_gettime(CLOCK_MONOTONIC,&a);
  for(int it=0;it<20000;it++)
    for(int i=0;i<8192;i++) buf[i]=buf[i]*1.0001f+0.5f;
  clock_gettime(CLOCK_MONOTONIC,&b);
  double ms=(b.tv_sec-a.tv_sec)*1e3+(b.tv_nsec-a.tv_nsec)/1e6;
  printf("elapsed_ms=%.3f\n",ms); return 0;}
C
gcc -O2 /tmp/k.c -o /tmp/k_native
aarch64-linux-gnu-gcc -O2 -static /tmp/k.c -o /tmp/k_a64
echo -n "native x86_64 : "; /tmp/k_native
echo -n "qemu-aarch64  : "; qemu-aarch64-static /tmp/k_a64
echo
echo "== 2) binary size 对比（同一个 C 文件，静态链接） =="
for t in x86_64-linux-gnu aarch64-linux-gnu; do
  ${t}-gcc -O2 -static /tmp/k.c -o /tmp/k_$t 2>/dev/null && \
  printf "%-20s %8s bytes\n" "$t" "$(stat -c%s /tmp/k_$t)"
done
echo
echo "== 3) -O0 / -O2 / -Os 的 Cortex-M 体积 =="
for o in -O0 -O2 -Os; do
  arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb $o -nostdlib -Wl,-T,../05_cortexm/link.ld \
    ../05_cortexm/startup.c ../05_cortexm/compute.c -o /tmp/fw$o.elf 2>/dev/null
  printf "%-4s %6s bytes text\n" "$o" "$(arm-none-eabi-size /tmp/fw$o.elf | tail -1 | awk '{print $1}')"
done
