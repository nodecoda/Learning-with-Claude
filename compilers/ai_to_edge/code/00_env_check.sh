#!/usr/bin/env bash
# 环境自检：本课所有章节依赖这里列出的能力
set -uo pipefail
fail=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "  OK   $1"; else echo "  FAIL $1"; fail=1; fi; }

echo "== toolchain =="
chk "llc has aarch64"   "/usr/lib/llvm-14/bin/llc --version | grep -q aarch64"
chk "llc has riscv64"   "/usr/lib/llvm-14/bin/llc --version | grep -q riscv64"
chk "mlir-opt present"  "/usr/lib/llvm-14/bin/mlir-opt --version"
chk "mlir-translate"    "/usr/lib/llvm-14/bin/mlir-translate --version"
chk "aarch64 gcc"       "aarch64-linux-gnu-gcc --version"
chk "arm-none-eabi gcc" "arm-none-eabi-gcc --version"
chk "riscv64 gcc"       "riscv64-linux-gnu-gcc --version"
chk "qemu aarch64"      "qemu-aarch64-static --version"
chk "qemu system arm"   "qemu-system-arm --version"

echo "== python =="
PY=${PY:-$HOME/.venvs/learn/bin/python}
chk "torch"  "$PY -c 'import torch'"
chk "numpy"  "$PY -c 'import numpy'"

echo
if [ $fail -eq 0 ]; then echo "环境就绪"; else echo "有项目失败，按上面 FAIL 逐项修"; fi
exit $fail
