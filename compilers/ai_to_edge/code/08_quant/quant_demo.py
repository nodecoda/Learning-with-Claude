import torch
torch.manual_seed(0)
W = (torch.randn(64, 128) * 3.0)
x = torch.randn(4, 128)

def per_tensor(t, lo, hi):
    tmin, tmax = float(t.min()), float(t.max())
    scale = (tmax - tmin) / (hi - lo)
    zp = max(lo, min(hi, int(round(lo - tmin / scale))))
    q = torch.quantize_per_tensor(t, scale, zp, torch.qint8 if lo < 0 else torch.quint8)
    return q.dequantize(), scale, zp

# 1) 激活/数据向量，int8 与 uint8 两种取值域
for lo, hi, name in [(-128, 127, "int8"), (0, 255, "uint8")]:
    dq, s, zp = per_tensor(x, lo, hi)
    err = (dq - x).abs()
    print(f"[{name}] scale={s:.6f} zp={zp} levels={hi-lo+1} max_err={err.max():.5f} mean_err={err.mean():.5f}")

# 2) 权重点阵：逐通道 vs 逐张量
scales = []
for i in range(W.shape[1]):
    r = W[:, i]
    scales.append((float(r.max()) - float(r.min())) / 255.0)
qW = torch.quantize_per_channel(W, torch.tensor(scales), torch.zeros(W.shape[1], dtype=torch.int64), 1, torch.quint8)
dqW = qW.dequantize()
qT, s_all, z_all = per_tensor(W, 0, 255)
print("per-channel  max_err %.5f mean_err %.5f" % ((dqW-W).abs().max().item(), (dqW-W).abs().mean().item()))
print("per-tensor   max_err %.5f mean_err %.5f  (scale=%.6f zp=%d)" % ((qT-W).abs().max().item(), (qT-W).abs().mean().item(), s_all, z_all))

# 3) 端到端矩阵乘误差传播
y = x @ W.T
for name, Wd in [("per-channel", dqW), ("per-tensor", qT)]:
    yq = x @ Wd.T
    print(f"matmul rel_err {name:12s} = {((yq-y).norm()/y.norm()).item():.5f}")

# 4) 动态量化 Linear
lin = torch.nn.Linear(128, 64)
qlin = torch.ao.quantization.quantize_dynamic(lin, {torch.nn.Linear}, dtype=torch.qint8)
out, outq = lin(x), qlin(x)
print("dynamic-quant Linear rel_err = %.5f" % ((outq-out).norm()/out.norm()).item())

# 5) 存储
print("fp32 4 B/weight, int8 1 B/weight -> 4x shrink; 64x128 = %d params, %d B vs %d B" % (64*128, 64*128, 64*128*4))
