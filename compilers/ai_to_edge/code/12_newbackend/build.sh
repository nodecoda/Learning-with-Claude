#!/usr/bin/env bash
# 练习 D：同一份 C 在三个后端上的差异
set -euo pipefail
cd "$(dirname "$0")"

echo "===== AArch64 ====="
aarch64-linux-gnu-gcc -O2 -S softmax.c -o softmax_aarch64.s
aarch64-linux-gnu-gcc -O2 -c softmax.c -o softmax_aarch64.o
echo "除法指令:"; grep -E 'sdiv' softmax_aarch64.s
echo "库调用:"; grep -cE '\bbl\s+__' softmax_aarch64.s || echo 0
aarch64-linux-gnu-size softmax_aarch64.o

echo
echo "===== Cortex-M3 (Thumb-2) ====="
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -S softmax.c -o softmax_thumb.s
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -c softmax.c -o softmax_thumb.o
echo "除法指令:"; grep -E 'sdiv' softmax_thumb.s
echo "库调用:"; grep -E '\bbl\s+__' softmax_thumb.s
arm-none-eabi-size softmax_thumb.o

echo
echo "===== RISC-V 64 ====="
riscv64-linux-gnu-gcc -O2 -S softmax.c -o softmax_rv.s
riscv64-linux-gnu-gcc -O2 -c softmax.c -o softmax_rv.o
echo "除法指令:"; grep -E '^\s+(div|divw|divu|rem)' softmax_rv.s
echo "库调用:"; grep -cE '\bcall\s' softmax_rv.s || echo 0
riscv64-linux-gnu-size softmax_rv.o

echo
echo "===== 三后端 .text 体积 ====="
printf "%-14s %s bytes\n" "AArch64" "$(aarch64-linux-gnu-size softmax_aarch64.o | tail -1 | awk '{print $1}')"
printf "%-14s %s bytes\n" "Cortex-M3" "$(arm-none-eabi-size softmax_thumb.o | tail -1 | awk '{print $1}')"
printf "%-14s %s bytes\n" "RISC-V 64" "$(riscv64-linux-gnu-size softmax_rv.o | tail -1 | awk '{print $1}')"
