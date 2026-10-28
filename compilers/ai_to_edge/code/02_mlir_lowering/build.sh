#!/usr/bin/env bash
# 环境：Ubuntu 22.04 / LLVM 14 / MLIR 14 / aarch64-linux-gnu-gcc / qemu-aarch64-static
# 运行：./build.sh
set -euo pipefail
LLVM_BIN=${LLVM_BIN:-/usr/lib/llvm-14/bin}
cd "$(dirname "$0")"

echo "== 1) linalg -> loops -> LLVM dialect =="
"$LLVM_BIN/mlir-opt" matmul.mlir \
  --convert-linalg-to-loops \
  --lower-affine \
  --convert-scf-to-std \
  --convert-std-to-llvm \
  --convert-arith-to-llvm \
  --convert-memref-to-llvm \
  --reconcile-unrealized-casts > matmul.llvm.mlir

echo "== 2) LLVM dialect -> LLVM IR =="
"$LLVM_BIN/mlir-translate" --mlir-to-llvmir matmul.llvm.mlir > matmul.ll

echo "== 3) LLVM IR -> AArch64 object =="
"$LLVM_BIN/llc" -march=aarch64 -filetype=obj -O2 matmul.ll -o matmul_a64.o

echo "== 4) link (static) =="
aarch64-linux-gnu-gcc -static -O1 mdrv.c matmul_a64.o -o matmul_a64

echo "== 5) run under qemu =="
qemu-aarch64-static ./matmul_a64
