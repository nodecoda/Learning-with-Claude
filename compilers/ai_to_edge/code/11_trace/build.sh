#!/usr/bin/env bash
set -euo pipefail
B=${LLVM_BIN:-/usr/lib/llvm-14/bin}
cd "$(dirname "$0")"
"$B/clang" --target=aarch64-linux-gnu -O1 -S -emit-llvm trace.c -o trace.ll
"$B/llc" -march=aarch64 trace.ll -o trace.s
aarch64-linux-gnu-gcc -c trace.s -o trace.o
aarch64-linux-gnu-gcc -static -O1 trace.o drv.c -o trace_bin
echo "== qemu =="
qemu-aarch64-static ./trace_bin
