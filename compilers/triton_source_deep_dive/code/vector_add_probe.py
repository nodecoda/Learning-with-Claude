import torch, triton, triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    tl.store(out_ptr + offs, x + y, mask=mask)

n = 1000
x = torch.randn(n, device="cuda")
y = torch.randn(n, device="cuda")
out = torch.empty_like(x)
h = add_kernel[(triton.cdiv(n, 128),)](x, y, out, n, BLOCK=128)
torch.cuda.synchronize()
print("triton", triton.__version__)
print("max_err", (out - (x + y)).abs().max().item())
print("asm_keys", sorted(h.asm.keys()))
for k in ("ttir", "ttgir", "llir", "ptx"):
    print("====", k, "====")
    print(h.asm[k][:3000])
print("==== sass_head ====")
print(h.asm["sass"][:800] if "sass" in h.asm else "no sass")
