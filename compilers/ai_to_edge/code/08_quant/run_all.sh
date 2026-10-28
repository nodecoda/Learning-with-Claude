#!/usr/bin/env bash
# Ch 11 全部实验；环境：gcc 11.4 / aarch64-linux-gnu-gcc 11.4 / torch 2.9.1+cpu
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-/home/ubuntu/.venvs/learn/bin/python}
echo "===== 1) 仿射量化：对称 vs 非对称 ====="
$PY asym.py
echo
echo "===== 2) 逐通道 vs 逐张量 ====="
$PY quant_axis.py
echo
echo "===== 3) 累加器宽度 ====="
gcc -O2 acc_width.c -o acc_width && ./acc_width
echo
echo "===== 4) int8 矩阵乘全链路 ====="
gcc -O2 int8_kernel.c -o int8_kernel -lm && ./int8_kernel
echo
echo "===== 5) 位宽对向量化的影响 ====="
gcc -O3 -mavx2 -S vec_width.c -o vec_x86.s
echo "x86 add_i8 循环步长:"; sed -n '/^add_i8:/,/\.size.*add_i8/p' vec_x86.s | grep -E 'addq|vpaddb'
echo "x86 add_f32 循环步长:"; sed -n '/^add_f32:/,/\.size.*add_f32/p' vec_x86.s | grep -E 'addq|vaddps'
echo
echo "===== 6) 校准样本量的影响 ====="
$PY calib.py
echo
echo "===== 7) 动态量化是否真的生效 ====="
$PY dynq2.py 2>&1 | grep -vi 'deprecat\|migrat\|eager mode\|fx graph\|pt2e\|see https'
