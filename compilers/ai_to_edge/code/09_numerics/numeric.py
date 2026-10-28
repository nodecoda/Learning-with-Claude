import torch, numpy as np
torch.manual_seed(0)

# 1) float32 vs float64 累加顺序对点积的影响
n = 100000
a = torch.randn(n, dtype=torch.float32)
b = torch.randn(n, dtype=torch.float32)
exact = torch.dot(a.double(), b.double()).item()
f32 = torch.dot(a, b).item()
rev = torch.dot(a.flip(0).contiguous().float(), b.flip(0).contiguous().float()).item()
print(f"dot  n={n}")
print(f"  float64 ref   = {exact:.10f}")
print(f"  float32 顺序  = {f32:.10f}  rel_err={abs(f32-exact)/abs(exact):.3e}")
print(f"  float32 逆序  = {rev:.10f}  rel_err={abs(rev-exact)/abs(exact):.3e}")
print(f"  顺序 vs 逆序差 = {abs(f32-rev):.6e}  (= {abs(f32-rev)/abs(exact):.3e} 相对)")

# 2) 大小数相消
big = torch.tensor([1e8], dtype=torch.float32)
small = torch.tensor([1e-8], dtype=torch.float32)
print("\nfloat32: 1e8 + 1e-8 - 1e8 =", (big + small - big).item())
print("float64: 1e8 + 1e-8 - 1e8 =", (big.double() + small.double() - big.double()).item())

# 3) 融合 vs 分离 的位级差异（同一个 relu(x*2+y*3-z)）
x = torch.randn(256); y = torch.randn(256); z = torch.randn(256)
step = torch.relu(torch.relu(torch.relu(x*2) + y*3) - z)
fused = torch.relu(x*2 + y*3 - z)
print("\nfused vs stepwise max|diff| =", (fused-step).abs().max().item())
print("bitwise equal:", torch.equal(fused, step))

# 4) fast-math 类重排：sum 顺序
v = torch.randn(1000)
s1 = v.sum().item()
s2 = v.double().sum().item()
print(f"sum 1000: float32={s1:.8f} float64={s2:.8f} rel={abs(s1-s2)/abs(s2):.3e}")
