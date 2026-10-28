import time, torch
torch.manual_seed(0)
torch.set_num_threads(1)
def bench(fn, warmup=3, iters=20):
    for _ in range(warmup): fn()
    t = time.perf_counter()
    for _ in range(iters): fn()
    return (time.perf_counter()-t)/iters*1e3

for M,N,K in [(64,64,64),(256,256,256),(512,512,512),(1024,1024,1024)]:
    a = torch.randn(M,K); b = torch.randn(K,N)
    ms = bench(lambda: a @ b)
    gflops = 2*M*N*K/(ms/1e3)/1e9
    print(f"matmul {M}x{N}x{K}: {ms:8.2f} ms  {gflops:7.2f} GFLOP/s  (1 thread)")

# elementwise 带宽
n = 1<<24
x = torch.randn(n)
ms = bench(lambda: torch.relu(x*2+1))
gb = n*4*2/(ms/1e3)/1e9
print(f"elementwise n={n}: {ms:.2f} ms  {gb:.2f} GB/s (读+写)")
