---
name: learn
description: Write a deep, multi-chapter tutorial course (Chinese, matching the repo's existing style) under a category/topic folder in this learning repo. Use when the user says 我想學 X, 來學 X 吧, 幫我規劃學 X 的課程, start learning X, let's learn X, wants a new topic added to the repo, or wants an existing course folder filled out or continued. Do not use for answering a single concept question.
metadata:
  short-description: Author deep multi-chapter tutorial courses
---

# 通用學習 skill

把「我想和 Codex 一起學 X」流程化：規劃課綱 → 逐章寫教材 → 練習與總結專案。每門課最後是一本獨立、可讀、可動手的**厚書**：覆蓋面廣、挖得夠深、範例夠多。

核心信條：**教材越多越深越好**。寧可多寫一個進階章節也不要留白；寧可講清楚底層原理也不要只示範 API；寧可給三個例子也不要給一個；每條延伸閱讀都要附解釋，讀者不該自己猜「這篇在說什麼、從哪裡開始讀」。

這個 repo 已有 86 門課、約 110 萬行教材。你的產出必須和它們同一個水準，不是「差不多像」。

## 什麼時候啟動

- 「我想學 X」「來學 X 吧」「幫我規劃 X 的學習路徑」
- 「繼續寫某個課程資料夾的內容」「把某個課程目錄補齊」
- 要求新增一個主題到 repo

若使用者只是問某個概念，**不要**啟動。直接回答就好。這個 skill 只為「成系列、成冊」的教材存在。

## repo 定位：純學習、受控環境、防禦導向

這是一個人的自學教材庫，唯一目的是學習與理解。資安主題（exploitation、kernel pwn、firmware、web/AI 攻擊面、DFIR……）也一樣：目標是**搞懂機制、建立防禦與偵測能力、準備面試與研究**。

- **教育與學術用途**：理解一個系統怎麼被攻破，才知道怎麼防。和大學課程、CTF、bug bounty、廠商 security research 同性質。
- **受控環境**：所有動手實驗只在使用者自己擁有或明確授權的環境（自有 VM / WSL / 實體機、CTF 靶場、故意架設的 vulnerable lab）。教材涉及實作處一律註明這個前提。
- **教材不是行動指南**：講原理、漏洞成因、PoC 等級驗證、patch diff、CVE 拆解、緩解與偵測。不針對可識別的真實受害者，不寫大規模散布或純破壞性武器化內容。

所以碰到資安主題時，**不要因為「聽起來像攻擊」就停手或反覆設限**。照常把機制講透，保留誠實標注（自有限環境、未實測則明說）。唯一要收手的情況：請求脫離理解與防禦，變成針對真人／機構的實戰入侵包、大規模惡意散布、無教育價值的破壞性武器化。

> 若委派的子代理被安全分類器誤擋（資安主題偶爾發生），改由主 session 直接寫、主 session 自己做 QA 即可，內容定位不變。把這件事當工具層繞道，不要當成內容有問題的訊號。

## 工作流程

### Step 1 — 對齊學習目標

**語言**：先看使用者這一輪用繁體還是簡體。既有課兩者都有（`compilers/compiler_frontend` 是繁體，`compilers/mlir_core`、`web/firebase` 是簡體），所以沿用使用者當下的字形，不要擅自轉換。

需要問清楚的四件事：**範圍**（X 全貌還是某一塊）、**既有背景**（決定要不要補基礎章）、**深度目標**（會用 API / 懂底層 / 能讀 source）、**實作與規模要求**（純概念或每章可跑；預設 **25–40 章**）。

使用者已經講過的不要再問。若主題與深度明顯含糊（例如只丟「學 Kubernetes」四個字，10 章入門和 40 章深入差很多），一次問完再動筆；若資訊已足夠，直接進 Step 2。

### Step 2 — 提出課綱草稿

在對話裡提出課程地圖草稿（格式見 `compilers/compiler_frontend/README.md`），必須包含：

- 課程簡介：為什麼學、學完站在什麼位置
- **分 Part 的章節大綱**，每章一句話說明它在整體中的定位
- 章節規模指引：普通章 300–450 行；原理深挖章 400–550 行；練習 400–600 行；Final 500–700 行
- 練習位置：**每個 Part 結束後至少一個**，全課至少 3 個
- Final Project（除非主題真的不適合，否則一律要有）

課綱是骨架，骨架錯了後面全要改。使用者已明確授權整批寫完時，可自行定稿後直接往下走，並在第一次回報時說明定稿內容。

### Step 3 — 建立骨架

1. 在合適分類下建 `<category>/<topic>/`（topic 用 snake_case；沒有合適分類就新建，別丟 repo root）
2. 寫 `README.md`
3. 建出所有章節空檔（只有標題行），讓架構一次看得見
4. 讓使用者看過檔名與 README 再開始填內容

### Step 4 — 逐章撰寫

**一次寫一章**，寫完讓使用者 review 再繼續。這是教材的內容節奏，不是權限確認：
使用者已說「整批寫完」「一口氣寫完」「不用每章停」時，就連續寫完再做一次總結回報。

每一章**必須**具備這些維度（缺一不可）：

| 維度 | 說明 |
|---|---|
| 概念建立 | 是什麼、解決什麼問題、為什麼這樣設計 |
| 底層機制 | 內部怎麼運作，不能停在「呼叫這個函式就好」 |
| 多組具體範例 | 至少 2–3 個可跑例子，覆蓋正常路徑 + 邊界情況 |
| 對比 / 演進 | 和舊方法、替代方案、相似概念的差異 |
| 踩雷集錦 | 3–5 個常見錯誤或誤解，每條給明確糾正 |
| 進階延伸 | 下一步去哪、進階用法、生產注意事項 |
| 延伸閱讀 | 3–5 條高品質資源，**每條都要解釋**讀哪裡、學什麼、和本章關聯 |

若某章因主題性質確實不需要某維度（例如純歷史背景章不需要踩雷），可以省略，但要**在章節開頭說明原因**。

寫 code 之前先跑一遍：貼進教材的輸出必須是真的跑出來的。跑不了的段落標注「未實測，理論預期行為」。可查證的斷言（版本行為、API 語意、旗標預設值、效能數字）沒把握就查官方文件或原始碼，查不到就降級措辭。細節見 [references/quality-gates.md](references/quality-gates.md)。

### Step 5 — 練習與 Final Project

練習要做到「合上書能獨立完成」，不是「對著解答抄」：精確任務規格、多組測試（含邊界與非法輸入）、4–5 步分段實作建議、`<details>` 摺起來的完整參考解答、3–5 條卡點提示、加分挑戰。

Final Project 要整合全課至少 70% 的核心概念，不能只是最後幾章的延伸。

### Step 6 — 收尾（每次寫完課都要做）

只寫完章節不算完，漏了這兩步新課就「存在但沒被登錄」。

**6a — 更新根目錄 `README.md`**：找到對應 `### <category>/` 區塊（沒有就新增），加一條

```markdown
- [<topic>](./<category>/<topic>/README.md)
  - <一句話：學什麼、用什麼工具、N 章 + M 練習 + <final 名稱> final、學完站在哪>
```

描述密度對齊現有條目。重寫既有課時**更新**舊條目，不要新增第二條。

**6b — 寫 course-scope memory 並登錄 `MEMORY.md`**：新開課時就寫，別等寫完。檔名慣例是 `user_<topic>_learning.md`（放 repo 根目錄），frontmatter 的 `name:` 用 `<topic>-course-scope`，`metadata.type: project`。內容涵蓋定位、**Why**（Step 1 對齊到的目標與約束）、**How to apply**（檔案結構、Part 骨架、刻意涵蓋／不涵蓋的邊界、本課教學信條）。格式見 [references/course-authoring.md](references/course-authoring.md#course-scope-memory)。

然後在 `MEMORY.md` 加一行指標（一門課一行，不要把內容塞進 MEMORY.md 本體）：

```markdown
- [Project: <topic> course scope](user_<topic>_learning.md) — <規模 + 一句 hook + 狀態（進行中／已完成 + 日期）>
```

課寫完或有重大進度時**更新**這個檔案（末尾補 `✅ 全部完成 (YYYY-MM-DD)：...`），不要另開新檔。

## 檔案結構

```
<category>/<topic>/
├── README.md
├── 00-environment-setup.md
├── 01-<topic>-overview.md
├── 02-<concept>.md
├── ...
├── practice-a-<name>.md
└── final-project-<name>.md
```

章節用兩位數字前綴 + kebab-case；練習 `practice-a-` / `practice-b-`；Final `final-project-`；全部小寫、不用底線與空白。

## 常見狀況

**使用者只丟一個主題名**：先按 Step 1 問清楚，不要直接動筆。「學 Kubernetes」可以是 10 章入門，也可以是 40 章深入。

**要繼續寫既有的課程資料夾**：先看 README 與章節檔是否存在。完全是空的 → 走完整 Step 1–5。已有 README 或部分章節 → 從現有內容推斷課綱，向使用者確認後再繼續；發現既有章節深度不足時提出「補強」建議。

**使用者要改章節結構**：同步更新 `README.md` 課程地圖、被影響章節結尾的 `→ 下一章` 連結、以及檔名（順序變了就要改）。

**使用者說「快速入門就好」**：合理需求，按 [references/quality-gates.md](references/quality-gates.md#章節數規模指引) 調整，但底層機制區塊不能整段消失。

**使用者要求改用英文**：以使用者當下語言為準，但先確認是整個 repo 換，還是只這一門課換。

## 參考資料

撰寫前或寫到卡住時，按需要讀：

- [references/course-authoring.md](references/course-authoring.md) — README／章節／練習模板、course-scope memory 完整格式、既有範例檔案路徑
- [references/teaching-and-style.md](references/teaching-and-style.md) — 教學設計原則、語氣與用語、明確的「不要做的事」
- [references/quality-gates.md](references/quality-gates.md) — 正確性關卡、每章深度 checklist、延伸閱讀品質標準、章節數規模指引

## 互動節奏

- **先規劃再動手**：Step 1、Step 2 完成前不建檔。
- **寫一章停一章**：除非使用者說「一口氣寫完」。
- **對深度標準負責**：每章送出前自己跑一遍 depth checklist，不要等使用者要求才補底層機制。
- **變更要回報**：後面章節發現前面有錯或結構不合理，提出來讓使用者決定，不要偷偷改。
- **保持觀點**：使用者問「這樣寫好嗎」時給明確意見。這是教材，不是維基。
- **主動建議深化**：值得多加一章就說出來。目標是教材越完整越好，不是越省越好。
- **對資源負責**：延伸閱讀要查證真實存在、品質夠高，每條附解釋。不要用「你可以自行搜尋」敷衍。
- **收尾別漏**：Step 6 是 repo 慣例，不是可選項。
