"""A small tile-size search for a 64x64x64 matmul."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_ai.codegen_c import compile_c
from mini_ai.runtime import benchmark, load_c_function


def kernel(ti: int, tj: int, tk: int) -> str:
    return f'''#include <stddef.h>
#if defined(_MSC_VER)
#define MINI_EXPORT __declspec(dllexport)
#else
#define MINI_EXPORT
#endif
MINI_EXPORT int mini_forward(const float* A, const float* B, float* C) {{
    for (int i = 0; i < 64; ++i) for (int j = 0; j < 64; ++j) C[i*64+j] = 0.0f;
    for (int ii = 0; ii < 64; ii += {ti}) {{
        for (int jj = 0; jj < 64; jj += {tj}) {{
            for (int kk = 0; kk < 64; kk += {tk}) {{
                int i_end = ii + {ti} < 64 ? ii + {ti} : 64;
                int j_end = jj + {tj} < 64 ? jj + {tj} : 64;
                int k_end = kk + {tk} < 64 ? kk + {tk} : 64;
                for (int i = ii; i < i_end; ++i) {{
                    for (int j = jj; j < j_end; ++j) {{
                        float acc = C[i*64+j];
                        for (int k = kk; k < k_end; ++k) acc += A[i*64+k] * B[k*64+j];
                        C[i*64+j] = acc;
                    }}
                }}
            }}
        }}
    }}
    return 0;
}}
'''


def main() -> None:
    rng = np.random.default_rng(0)
    a = rng.standard_normal((64, 64), dtype=np.float32)
    b = rng.standard_normal((64, 64), dtype=np.float32)
    ref = a @ b
    configs = [(64, 64, 64), (32, 32, 32), (16, 16, 16), (32, 64, 32), (64, 32, 32), (16, 32, 16), (32, 16, 32), (8, 8, 8)]
    results = []
    for ti, tj, tk in configs:
        build = Path(".scratch/mini-ai-autotune") / f"t{ti}_{tj}_{tk}"
        result = compile_c(kernel(ti, tj, tk), build, name="matmul")
        fn = load_c_function(result.dll_path, 2, 1)
        out = fn([a, b], [(64, 64)])[0]
        err = float(np.max(np.abs(out - ref)))
        stats = benchmark(lambda: fn([a, b], [(64, 64)]), warmup=3, repeat=20)
        results.append(((ti, tj, tk), stats["median_ms"], err))
        print(f"tile=({ti:2d},{tj:2d},{tk:2d}) median_ms={stats['median_ms']:.4f} maxerr={err:.3e}")
    best = min(results, key=lambda x: x[1])
    print("best:", best[0], "median_ms=", round(best[1], 4))


if __name__ == "__main__":
    main()