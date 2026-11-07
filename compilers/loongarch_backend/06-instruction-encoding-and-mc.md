# Ch 06 — 指令编码与 MC 层

> 状态：待写。本章将覆盖：把 `add.d $a0,$a1,$a2` 手算成 `0x00108c84`，再看 `MCCodeEmitter`/`AsmBackend` 怎么生成 fixup 与 `.rela`。

## 本章计划

- `Fmt3R` 的位域
- `MCCodeEmitter::encodeInstruction`
- `AsmBackend` 与 `LoongArchFixupKinds.h`

---

（待填：正文、实测命令、证据表、练习、下一步。）
