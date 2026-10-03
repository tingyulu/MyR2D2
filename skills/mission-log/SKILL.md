---
name: mission-log
description: '零 token 的 session 活動收割器。從 Claude Code 本來就在寫的 transcript(~/.claude/projects)抽出指定日期的活動骨架:哪些專案、幾點到幾點、說過什麼、用了哪些工具、花多少 token。當使用者說「今天做了什麼」「昨天的 session 記錄」「收割某天的活動」「mission log」時觸發。⚠️ 只讀不寫、不呼叫模型;日報/週報請用 /daily-debrief、/weekly-debrief(兩者以本 skill 為底層)。 English triggers: "what did I work on today", "harvest my sessions", "mission log", "show session activity".'
---

# /mission-log — session 活動收割

把「這一天所有 Claude session 做了什麼」的骨架抽出來 —— 不靠任何背景程序,因為 **Claude Code 本來就在全程記錄**(transcript 落在 `~/.claude/projects/`),本 skill 只是收割器。

> 🤖 R2-D2 時刻:astromech 的飛行記錄器從不休息 —— X-wing 落地後,技師才把記錄拉出來看。
> 記錄一直都在,你需要的是讀取器。

## 用法

`/mission-log [日期]` —— 不帶參數＝今天;`yesterday`＝昨天;`YYYY-MM-DD`＝指定日。

## 動作

1. **跑收割器**(本 skill 附帶的腳本,純標準庫、零 token):

   ```bash
   python3 <本skill目錄>/scripts/harvest.py --date <日期>
   ```

   輸出每個活躍 session 的:時間段、專案@分支、turns、token 消耗、模型、工具直方圖、使用者原話(前 5 句)。機器可讀版加 `--format jsonl`。
   口徑:turns＝API 回應數、token＝新增 in+out+cache_creation(不含 cache 讀取)。🔴 **一則回應只算一次**:transcript 把同一則回應拆成多行(每個 content block 一行)、每行都帶同一份 usage,收割器按 `message.id` 併成一則、四欄取最大——逐行相加會把 turns 與 tokens 灌水約 2 倍,別自己另寫一份逐行加。

2. **呈現**:把骨架整理給使用者看;若使用者追問某個 session 細節,再視需要深讀該 transcript(骨架裡有 session id 前綴可定位),**深讀前先告知會消耗較多 context**。

## 鐵律

- 🚫 **只讀不寫**:本 skill 不落任何檔、不改任何狀態;落檔是 /daily-debrief 的事。
- ✅ **骨架說什麼就是什麼**:不腦補骨架裡沒有的活動;抽不到資料就回報「該日無記錄」。
- ⚠️ **用量矛盾不硬加**:同一則回應出現兩組不同用量、或對到兩個 requestId 時,該則 turns 照算、tokens 不計,列上標 `usage_conflict`、stderr 警告。看到就去查 transcript,別手動補數字。
- ⚠️ **回溯上限＝transcript 保留期**:Claude Code 預設 30 天後刪 transcript(`cleanupPeriodDays` 可調大);超過保留期的日期無料可收,直說。

## 進階:跨機器收割

腳本是單檔自包含,可直接經 ssh 餵給另一台機器的 python3(該機零安裝):

```bash
ssh <你的主機> "python3 - --date <日期>" < <本skill目錄>/scripts/harvest.py
```

輸出自帶主機名,多台結果可直接並列。
