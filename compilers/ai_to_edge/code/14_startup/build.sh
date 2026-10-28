#!/usr/bin/env bash
# 环境：arm-none-eabi-gcc 10.3 / qemu-system-arm 6.2
set -euo pipefail
cd "$(dirname "$0")"

CFLAGS="-mcpu=cortex-m3 -mthumb -nostdlib -fno-builtin -Wl,-T,link.ld -Wl,--gc-sections -O2"

echo "== 1) 正确版本：带 .data 拷贝与 .bss 清零 =="
arm-none-eabi-gcc $CFLAGS startup_full.c -o startup_full.elf
arm-none-eabi-size startup_full.elf
arm-none-eabi-objcopy -O binary startup_full.elf startup_full.bin
ls -l startup_full.bin | awk '{print "bin size:", $5, "bytes"}'
timeout 3 qemu-system-arm -M mps2-an385 -cpu cortex-m3 \
  -kernel startup_full.elf -nographic -serial mon:stdio 2>&1 | head -4 || true

echo
echo "== 2) 段布局：注意 .data 的 VMA 在 RAM、LMA 在 FLASH =="
arm-none-eabi-objdump -h startup_full.elf | grep -E 'text|data|bss'

echo
echo "== 3) 去掉 .data 拷贝会怎样（见 startup_noinit.c）=="
arm-none-eabi-gcc $CFLAGS startup_noinit.c -o startup_noinit.elf
timeout 3 qemu-system-arm -M mps2-an385 -cpu cortex-m3 \
  -kernel startup_noinit.elf -nographic -serial mon:stdio 2>&1 | head -4 || true
