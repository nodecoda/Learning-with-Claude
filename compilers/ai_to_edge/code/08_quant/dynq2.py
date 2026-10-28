import torch, torch.nn as nn
torch.manual_seed(0)
lin = nn.Linear(128, 64).eval()
x = torch.randn(4, 128)
q = torch.ao.quantization.quantize_dynamic(lin, {nn.Linear}, dtype=torch.qint8)
print("class:", type(q), "| same obj:", q is lin)
print("weight type:", type(q.weight).__name__, "dtype:", q.weight.dtype, "device:", q.weight.device)
print("has _packed_params:", hasattr(q, "_packed_params"))
if hasattr(q, "_packed_params") and q._packed_params is not None:
    print("packed:", q._packed_params.dtype, q._packed_params._weight_bias()[0].dtype)
print("forward equal:", torch.equal(lin(x), q(x)))
