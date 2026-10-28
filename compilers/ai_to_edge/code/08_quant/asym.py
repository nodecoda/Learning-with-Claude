import torch
torch.manual_seed(0)
act = torch.clamp(torch.randn(4096)*1.5 + 2.0, 0.0, 6.0)   # ReLU 之后的典型激活
lo, hi = 0.0, 6.0

def report(name, qmin, qmax, sym):
    if sym:
        m = max(abs(lo), abs(hi))
        s = 2*m/(qmax-qmin)
        zp = 0
    else:
        s = (hi-lo)/(qmax-qmin)
        zp = int(round(qmin - lo/s))
    q = torch.round(act/s) + zp
    q = q.clamp(qmin, qmax)
    dq = (q - zp) * s
    # 实际用到的刻度数
    used = int(q.max().item() - q.min().item()) + 1
    print(f"{name:22s} scale={s:.6f} zp={zp:4d} 用到刻度={used:3d}/256  "
          f"max_err={(dq-act).abs().max().item():.6f}  mean_err={(dq-act).abs().mean().item():.6f}")

report("int8 对称(zp=0)", -128, 127, True)
report("int8 非对称", -128, 127, False)
report("uint8 非对称", 0, 255, False)
