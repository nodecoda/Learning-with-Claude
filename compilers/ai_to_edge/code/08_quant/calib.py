import torch
torch.manual_seed(0)
# 模拟校准：用不同数量的样本估计 min/max，看 scale 抖动
act = torch.randn(4096) * 2.0 + 0.5      # 真实分布
for n in [8, 32, 128, 1024, 4096]:
    s = act[:n]
    scale = (float(s.max()) - float(s.min())) / 255.0
    # 用全部数据测试这个 scale 造成的裁剪比例
    lo, hi = float(s.min()), float(s.max())
    clipped = ((act < lo) | (act > hi)).float().mean().item()
    print(f"n={n:5d}  scale={scale:.6f}  裁剪比例={clipped*100:5.2f}%  "
          f"quant_err_max={((act.clamp(lo,hi) - (torch.round((act.clamp(lo,hi)-lo)/scale)*scale+lo)).abs().max()):.4f}")
