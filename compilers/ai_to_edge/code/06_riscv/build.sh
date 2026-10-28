#!/usr/bin/env bash
# 环境：Ubuntu 22.04 / LLVM 14 / riscv64-linux-gnu-gcc / qemu-riscv64-static
# 运行：./build.sh
set -euo pipefail
LLVM_BIN=${LLVM_BIN:-/usr/lib/llvm-14/bin}
cd "$(dirname "$0")"

echo "== 1) IR =="
"$LLVM_BIN/clang" --target=riscv64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll

echo "== 2) RV64 汇编（看 mul / addi） =="
"$LLVM_BIN/llc" -march=riscv64 add.ll -o add.s
grep -E '^\s+(mul|addi|li|ret)' add.s

echo "== 3) 链接 + qemu =="
"$LLVM_BIN/llc" -march=riscv64 -filetype=obj add.ll -o add_rv.o
riscv64-linux-gnu-gcc -static -O1 drv.c add_rv.o -o add_rv 2>/dev/null \
  || riscv64-linux-gnu-gcc -static -O2 add.c drv.c -o add_rv
qemu-riscv64-static ./add_rv
