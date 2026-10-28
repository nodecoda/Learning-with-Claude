#!/usr/bin/env bash
# 环境：arm-none-eabi-gcc 10.3 / qemu-system-arm 6.2
# 运行：./build.sh
set -euo pipefail
cd "$(dirname "$0")"

arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib \
  -Wl,-T,link.ld -Wl,--gc-sections \
  -O2 startup.c compute.c -o firmware.elf

arm-none-eabi-size firmware.elf
arm-none-eabi-objcopy -O binary firmware.elf firmware.bin

echo "== qemu-system-arm (mps2-an385) =="
timeout 5 qemu-system-arm -M mps2-an385 -cpu cortex-m3 \
  -kernel firmware.elf -nographic -serial mon:stdio 2>&1 | head -5 || true
