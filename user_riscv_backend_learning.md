---
name: riscv-backend-course-scope
description: LLVM RISC-V backend course (2026) — 24 chapters + 4 practices + end-to-end final, LLVM 23.1.4 prebuilt tools plus llvmorg-23.1.4 RISC-V sources, TableGen regenerated locally.
metadata:
  type: project
---

`compilers/riscv_backend/` 是一門以「完整後端主線」組織的 LLVM RISC-V 教材：從 TableGen 的 `def` 與指令編碼，一路走到 MC 層、暫存器與 ABI、Legalizer/SelectionDAG/GlobalISel、偽指令與機器 pass、AsmPrinter、重定位與 RVV，最後用一個端到端 Final 收束。

**Why:** 使用者想學「LLVM 裡的 RISC-V 完整後端實作」。本機有 LLVM 23.1.4 預編譯工具與同版本 RISC-V 源碼，但磁碟不足以建置 `llc`。因此課程採「讀同版本源碼 + 跑預編譯工具 + 用 `llvm-tblgen` 重建生成物」的主線，只有「新增指令後重新建置」標為 guided。

**How to apply:**

- 檔案結構：`README.md` + Ch 00–23（24 章）+ 練習 A–D + Final，共 30 篇，約 14,100 行。
- Part 0（00–03 + 練習 A）：環境基線、後端全景、ISA 建模、流水線與 MIR。
- Part 1（04–07 + 練習 B）：TableGen 語言、生成物解剖、指令編碼（含 RVC）、Operand 與 DAG pattern。
- Part 2（08–10）：AsmParser、Disassembler、MC 層與 ELF 輸出。
- Part 3（11–13 + 練習 C）：暫存器類、呼叫約定（ilp32/lp64/lp64d）、棧框。
- Part 4（14–17）：合法化、SelectionDAG 選指、自訂 lowering 與 intrinsic、GlobalISel。
- Part 5（18–20 + 練習 D）：排程模型與 llvm-mca、偽指令展開、AsmPrinter。
- Part 6（21–23 + Final）：重定位與 ld.lld、常量池/GOT/PLT/TLS、RVV 與新增擴展。
- 環境：LLVM 23.1.4 預編譯（`.scratch/llvm-23.1.4-root/.../bin`）；同版本源碼 `.scratch/llvm-23.1.4-riscv-td`（`llvmorg-23.1.4` sparse clone）。`D:\llvm-project` 是 main/24-dev，只讀結構、不引用行號。
- 統一入口：`compilers/riscv_backend/code/tools/run.ps1 <labs/xxx.ps1>`；腳本純 ASCII（PowerShell 5.1 對無 BOM UTF-8 會誤判為 ANSI），並透傳子進程退出碼。
- 關鍵實測（可重跑）：`llc -mtriple=riscv64 -mattr=+m -O2` → `mul/addi/ret`；`llvm-mc -show-encoding` 的 R/I/S/B/U/J 編碼與 RVC 自動壓縮；`-code-model=small|medium|large` 對應 `HI20`/`PCREL_HI20`/常量池；`-relocation-model=pic` → `GOT_HI20`；`llvm-mc -mattr=+relax` + `ld.lld` 把 `call` 收成 `jal`；`llvm-tblgen` 六種 generator 全可跑（DAGISel 約 58 MB）。
- 刻意標 guided：新增指令後重新建置 `llc`/`llvm-mc`、lit/FileCheck 驗證、`update_llc_test_checks`。課程不偽造建置日誌。
- 刻意不涵蓋：私有擴展實作細節、GPU/加速器後端、完整 LLVM 建置流程。
- 教學紀律：每章固定 `## 為什麼` / `## 主線` / `## 可跑實驗` / `## 源碼落點` / `## 常見坑` / `## 自我檢查` / `## 延伸閱讀`；每章至少一個失敗或邊界例子；輸出必附命令與真實結果。

✅ 全部完成（2026-10-25）：30 篇、約 14,124 行；`python .scratch/validate-riscv-course.py` 回報 `problems=0`；根 README 索引與 MEMORY 已同步。