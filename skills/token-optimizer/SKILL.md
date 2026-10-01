---
name: token-optimizer
description: '多代理編排的 token／配額節流規則。任何要呼叫 Agent tool 或 Workflow 派工之前——不論使用者有沒有提到省 token、單一 subagent 也算——先讀本 skill 再動手。使用者說「省 token」「配額」「別燒爆額度」時也觸發。觸發詞：workflow、多代理、fan-out、subagent、派工、編排、省 token、配額、token optimizer。 English triggers: "save tokens", "quota", "don''t burn my limit", "token optimizer".'
license: MIT
---

# Token Optimizer — 多代理編排節流規則

> 改寫自 [kieiken/ultracode-token-optimization](https://github.com/kieiken/ultracode-token-optimization)（MIT），2026-07 適配全 Claude 環境並泛化。
> 核心洞察：**省 token 的重點不是壓單價，是消滅「返工循環」與「context 灌爆」**——
> 改→審→再改→再審的迴圈、以及把原始 diff/log 灌回主編排者，才是真正的大宗浪費。

## 0. 為什麼

- 訂閱制（如 Claude Max）用戶：省 token ≠ 省錢，是**省用量配額**。配額燒完＝所有 session 一起停擺，比錢痛。
- **旗艦檔模型配額燒得比中檔快得多**。這讓「模型分層」從 nice-to-have 變成鐵則。
- 本 skill 管兩個場景：① Workflow script（`agent()`/`parallel()`/`pipeline()`）② 一般 session 用 Agent tool 開 subagent。兩者同一套原則。

## 1. 模型分層表（鐵則）

| 角色 | 指定 | 理由 |
|---|---|---|
| 主編排者（session 本體） | 跟著 session 走，不動 | 只做判斷與仲裁 |
| 執行者（寫 code/改檔） | 中檔（如 `sonnet`） | 品質夠、配額友善 |
| 審查者（找碴/review） | 中檔，預設 | 高風險領域才升旗艦（見下） |
| 機械工作（摘要/格式化/grep 彙整） | 低檔（如 `haiku`）或中檔（`effort` 留 `'medium'`，🚫 別降到 `'low'`） | 不需判斷力的活別用貴模型；但省推理深度換來的是草稿品質，低檔位 ≠ 低 effort |
| 仲裁/最終判斷 | 不指定（繼承 session） | **唯一例外，必須加註解說明為什麼** |

**審查者升旗艦的門檻**（符合任一才升）：認證／金流／資料遺失風險／資安／併發／持久化／公開 API／大型重構。

**🔴 絕對規則：session 模型是旗艦檔時，每一個 `agent()`／Agent tool 呼叫都必須明確指定 `model`**（仲裁 lane 除外，且要註解）。漏指定＝整包 fan-out 全繼承貴檔，配額瞬間蒸發。這是本 skill 存在的第一理由。
> 判準是「**session 跑的比中檔貴就適用**」，不是特定型號名——新模型上市不用改這條。

**⚙️ 進階兜底（Claude Code CLI 專屬——其他工具沒有 `settings.json`／這個環境變數，直接跳過本段）**：在 `settings.json` 設 `env.CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，**漏寫 `model` 的 subagent 就落到中檔**，不再默默繼承旗艦。⚠️ 它是**地板，不是硬上限**：呼叫時明寫的 `model` 會蓋過它——指定低檔會真的降、指定旗艦也會真的燒。所以 env 只救「忘了寫」，救不了「寫錯」；正解仍是每個 `agent()`／Agent 呼叫都明寫 `model`，env 當兜底。（這個行為隨 CLI 版本變過：舊版曾實測為硬上限、近期版本為地板。「model 寫在呼叫上」是兩種行為下都對的做法，所以本 skill 只押這一條。）🚫 別拿專案 `.claude/settings.local.json` 蓋 env 當「臨時全力」開關：要全力就把旗艦檔寫在該次派工的每個呼叫上，任務結束沒有設定要回收、也不會漏關。

**📌 派工的四個硬層事實**（Claude Code 實測；其他工具對照自家機制）：

1. **effort 怎麼繼承**：Agent tool 沒有 `effort` 參數，子代理跟主 session 同 effort——主 session 開最深推理時，派出去的每一支也都是最深，估算要照這個算。Workflow 的 `agent()` 可以寫 `effort`，執行／驗證寫 `'medium'` 以上，🚫 別拿 `'low'` 當預設（省下的是推理深度，換來的是草稿品質）。
2. 🚫 **`subagent_type: 'fork'` 不當執行者**：fork 繼承父 session 整段 context、固定跑父模型（`model` 參數被忽略），肥 session＋旗艦檔＝最燒的組合。執行者一律 fresh general-purpose 或 Workflow `agent()`，並明寫 `model`。
3. **主 session context 肥時，派乾淨執行者可能反而省**：省的是新增 token（輸入＋輸出＋快取寫入）的檔位單價差與旗艦配額，快取讀取各檔位價差不大。判準：兩條路用**同一組假設**估 API 等值——自己做＝預估呼叫數 × 主 session 每次呼叫的新增量；派工＝每支固定開場成本（載入規則、記憶、工具定義，實測在數萬 tokens 量級）＋預估呼叫數 × 子代理每次呼叫量——**派工 ≤ 自己做的 1.2 倍就派**，不必等到明顯更省；估算是區間不是點，差在 ±20% 內算「差不多」。反面照舊：一個檔幾十行、有測試的小改動，開場行李比任務重，不派。
4. **別名實跑哪一版跟宿主 CLI 版本走**：`sonnet`／`opus`／`haiku` 這類別名指向哪個具體型號由 CLI 決定、改版會變；要確認實跑的是哪版，看子代理 transcript 的 `model` 欄，別看自己寫了什麼。

## 2. 執行結果壓縮再上報（鐵則）

- 子代理的回報格式在 prompt 裡就寫死：**「只回：完成/未完成＋變更檔案清單＋測試結果，三行摘要」**。
- 原始 diff、完整 log、大 JSON **絕不**灌回主編排者的 context——需要細節時，派一個低檔位代理去讀、回摘要。
- Workflow 裡用 `schema` 參數強制結構化輸出（驗證失敗會自動重試，比自由文字可靠又省來回）。
- 一般 Agent tool：prompt 結尾明講「你的最終訊息就是回傳值，只給結論與關鍵證據，不要貼過程」。

## 3. 角色鎖死（鐵則）

- **主編排者不下場實作**；細節決策下放給 lane 內的 agent，主編排者只在 phase 邊界與升級時介入。
- **審查者只出 findings**（每條要：觸發條件＋重現步驟＋行號引用），不給修法、不裁決通過與否。prompt 明寫：「你只輸出 findings 與嚴重度，核可/駁回不是你的職權」。
- **審查者輸出不直接轉發給執行者**——中間放一個 synthesis agent（中檔即可）過濾掉站不住腳的 findings、濃縮成精簡修復指令。沒證據的 finding 一律丟棄。
- **執行者不自我宣告完成**。「完成了！」不算數。

## 4. 獨立驗證（鐵則）

- 開工前先在 prompt 裡定義「done」：檔案存在、測試 exit code、commit hash——可機器驗證的證據，不是感覺。
- 完成與否由**專職驗證代理**確認：只驗證、不修東西，回結構化證據（testExit、diff 摘要）。
- 最終簽核**人工把關**：回「待使用者核可的提案」，不自動 commit、不自動發布。

## 5. 失敗三次就停（鐵則）

- 每條 retry 迴圈都要有**計數器**：同一個錯誤連續 3 次 → 停，回報現況與卡點，不再空轉。
- Workflow 迴圈加 `budget.remaining()` 守門（有設 budget 時）；沒 budget 機制的一般流程，用計數器＋「連 3 次同錯即停」。
- 執行者連續失敗時的正確反應是**換角度**（換模型檔位、換拆法、或上報卡點），不是原地重試第四次。

## 6. 結構節流（寫 script 時的預設）

- **`pipeline()` 優先於 `parallel()`**：多階段多項目的流程用 pipeline（無 barrier、單項獨立流動）；只有「下一階段真的需要上一階段全部結果」（去重、彙整、早退判斷）才用 parallel barrier。
- 一次 `agent()` 只做一件事；多目標拆成單目標 thunks。
- 所有 `agent()` 都給 `label`；prompt 裡不放揮發性資料（時間戳、隨機值），讓 `resumeFromRunId` 重跑時快取命中。
- 單次提交的 diff 上限 200–300 行；更大就先回拆分計畫。
- 並行改檔的 lanes 才加 `isolation: 'worktree'`（有成本）；唯讀 lanes 不加。

## 7. 送出前自檢（寫完 script／派工前過一遍）

- [ ] 主編排者 prompt 裡沒有原始 diff/log/大 JSON（§2）
- [ ] 每個 `agent()` 都有明確 `model`，或有「為什麼繼承 session 模型」的註解（§1）
- [ ] session 跑旗艦檔時，零例外全部指定 model（§1 絕對規則）；Workflow `agent()` 再寫 `effort`，地板 `'medium'`（§1 硬層事實 1）
- [ ] 沒有用 `subagent_type: 'fork'` 當執行者（§1 硬層事實 2）
- [ ] 主 session context 肥時做過「派工 vs 自己做」的 1.2 倍比較，不是憑感覺派（§1 硬層事實 3）
- [ ] 審查者 prompt 含「無裁決權、findings＋嚴重度＋行號引用」（§3）
- [ ] 審查者輸出經 synthesis agent 過濾才進執行者（§3）
- [ ] 完成由專職驗證代理用證據確認，非自我宣告（§4）
- [ ] retry 計數器＋連 3 次同錯即停（§5）
- [ ] 多階段多項目用 `pipeline()`，沒有多餘 barrier（§6）
- [ ] 子代理輸出用 `schema` 結構化（§2）
- [ ] 所有 `agent()` 有 `label`、prompt 無揮發性資料（§6）
- [ ] 最終產出是「待核可提案」，不自動 commit（§4）

## 8. 限制（誠實註記）

- 這是**行為守則，不是硬性 token 上限**——真要封頂得靠 Workflow 的 `budget` 機制＋腳本裡的 `budget.remaining()` 守門。
- 效果來自「把常犯的浪費模式在寫 script 前就鎖死」（diff 灌爆、輕信自我宣告、審查者越權），不保證具體省幾 %。
- 原作用 OpenAI Codex 當執行者做「異廠牌互審」；全 Claude 環境的異質性靠**模型檔位差異**（中檔執行 × 中檔審查已有獨立 context，必要時旗艦仲裁）部分替代，天生弱一點，所以 §4 的獨立驗證更重要。
