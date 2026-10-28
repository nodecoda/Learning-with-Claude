#!/usr/bin/env bash
# 环境：Ubuntu 22.04 / LLVM 14 / aarch64-linux-gnu-gcc / qemu-aarch64-static
# 运行：./build.sh
set -euo pipefail
LLVM_BIN=${LLVM_BIN:-/usr/lib/llvm-14/bin}
cd "$(dirname "$0")"

"$LLVM_BIN/clang" --target=aarch64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll
"$LLVM_BIN/llc" -march=aarch64 add.ll -o add.s
aarch64-linux-gnu-gcc -static -O2 add.c drv.c -o add_a64
echo "== qemu =="
qemu-aarch64-static ./add_a64
