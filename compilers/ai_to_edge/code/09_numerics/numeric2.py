import torch
torch.manual_seed(0)
x = torch.randn(256); y = torch.randn(256); z = torch.randn(256)

# A) 同序分离 vs 融合：语义等价时差异应为 0（说明「融合本身不改数值」）
t1 = x*2; t2 = t1 + y*3; t3 = t2 - z
step = torch.relu(t3)
fused = torch.relu(x*2 + y*3 - z)
print("A 同序 stepwise vs fused: bitwise_equal =", torch.equal(step, fused),
      " max|diff| =", (step-fused).abs().max().item())

# B) 重结合律：先加 y*3 和 z 再减，与从左到右不同
reassoc = torch.relu(x*2 + (y*3 - z))
print("B 重结合 (x*2 + (y*3-z)): bitwise_equal =", torch.equal(step, reassoc),
      " max|diff| =", (step-reassoc).abs().max().item(),
      " 不同元素数 =", (step != reassoc).sum().item(), "/256")

# C) FMA 的效果：a*b+c 一步 vs 两步
import numpy as np
a = torch.randn(100000, dtype=torch.float32); b = torch.randn(100000, dtype=torch.float32)
c = torch.randn(100000, dtype=torch.float32)
two_step = (a.double()*b.double() + c.double())   # 模拟「乘完再舍入再加」
print("C FMA 残差演示（torch 在 CPU 上是否用 FMA 不可直接观测）:")
print("   两步 f32 结果与 f64 参考的相对误差中位数 =",
      float(((a*b+c).double() - two_step).abs().div(two_step.abs().clamp_min(1e-9)).median()))

# D) 归约顺序
v = torch.randn(4096)
s_seq = v.double().sum()            # 参考
s_f32 = v.sum()
s_pair = v.reshape(-1, 2).sum(1).sum()  # 两两归约
print("D 归约: f64ref=%.9f  f32=%.9f (rel %.2e)  两两=%.9f (rel %.2e)" %
      (s_seq, s_f32, abs(s_f32-s_seq)/abs(s_seq), s_pair, abs(s_pair-s_seq)/abs(s_seq)))
