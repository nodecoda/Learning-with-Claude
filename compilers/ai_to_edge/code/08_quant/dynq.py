import torch
torch.manual_seed(0)
lin = torch.nn.Linear(128, 64).eval()
x = torch.randn(4, 128)
with torch.no_grad():
    out = lin(x)
qlin = torch.ao.quantization.quantize_dynamic(lin, {torch.nn.Linear}, dtype=torch.qint8).eval()
with torch.no_grad():
    outq = qlin(x)
print("bitwise equal:", torch.equal(out, outq))
print("max|diff|:", (out-outq).abs().max().item())
print("rel_err:", ((outq-out).norm()/out.norm()).item())
print("qlin type:", type(qlin).__name__, "| weight dtype:", qlin.weight.dtype)
print("weight shape:", tuple(qlin.weight.shape), "| int8 qscheme:", qlin.weight.qscheme())
print("scales shape:", tuple(qlin.weight.q_per_channel_scales().shape), "axis:", qlin.weight.q_per_channel_axis())
