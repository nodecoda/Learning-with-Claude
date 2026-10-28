import torch
torch.manual_seed(0)
W = (torch.randn(64, 128) * 3.0)   # out=64, in=128
x = torch.randn(4, 128)

def per_channel(t, dim):
    n = t.shape[dim]
    scales, zps = [], []
    for i in range(n):
        v = t.select(dim, i)
        s = (float(v.max()) - float(v.min())) / 255.0
        scales.append(s)
        zps.append(max(0, min(255, int(round(-float(v.min())/s)))))
    q = torch.quantize_per_channel(t, torch.tensor(scales), torch.tensor(zps, dtype=torch.int64), dim, torch.quint8)
    return q.dequantize()

def per_tensor(t):
    s = (float(t.max())-float(t.min()))/255.0
    z = max(0, min(255, int(round(-float(t.min())/s))))
    return torch.quantize_per_tensor(t, s, z, torch.quint8).dequantize()

y = x @ W.T
for name, Wd in [("per-tensor      ", per_tensor(W)),
                 ("per-channel dim0", per_channel(W, 0)),
                 ("per-channel dim1", per_channel(W, 1))]:
    rel = ((x @ Wd.T - y).norm()/y.norm()).item()
    print(f"{name}  max_err={ (Wd-W).abs().max().item():8.4f}  matmul_rel_err={rel:.5f}")
print("W 形状", tuple(W.shape), " 每行范围示例 row0:", round(float(W[0].max()-W[0].min()),3),
      " 每列范围示例 col0:", round(float(W[:,0].max()-W[:,0].min()),3))
