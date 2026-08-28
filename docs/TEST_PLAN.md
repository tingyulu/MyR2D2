# MyR2D2 測試計畫

> 本計畫的存在理由：v0.1.1 帶著 5 支無效 YAML 上線三天無人發現（`npx skills add` 對**所有** agent 0/5 全滅，不只 Claude Code）。教訓＝**發布關卡必須擋在 push 之前，且驗證範圍要涵蓋所有宣稱相容的工具**。
>
> 各項標註可測性：🟢 本機純機械可測／🟡 需對應 agent CLI 在場／✋ 需人工判讀。

## 層次定義（跨工具相容的三層，逐層驗、不可混談）

1. **安裝層**：`npx skills add` 能把檔案裝進該 agent 的 skills 目錄。
2. **發現層**：該 agent 本體真的列出／載入這些 skill（安裝成功 ≠ 看得到，見 CROSS-02 的 trusted-folder 教訓）。
3. **執行層**：agent 收到觸發詞後真的照 SKILL.md 內文正確行事。

---

## A. 發布前回歸（每次 release tag 前必跑，全綠才可 push）

> **一鍵版**（與 CI `.github/workflows/ci.yml` 等價的機械關卡；兩邊漂移時以 ci.yml 為準）：
>
> ```bash
> set -e
> python3 - <<'PY'
> import yaml, glob, re, sys
> fails = 0
> files = sorted(glob.glob('skills/*/SKILL.md'))
> assert len(files) == 12, f'skill 數 {len(files)} != 12'
> for f in files:
>     m = re.match(r'^---\n(.*?)\n---\n', open(f, encoding='utf-8').read(), re.S)
>     try: yaml.safe_load(m.group(1))
>     except Exception as e: print('FAIL', f, type(e).__name__); fails += 1
> try:
>     yaml.safe_load('description: bad: colon'); print('FAIL 陽性對照'); fails += 1
> except yaml.YAMLError: pass
> sys.exit(1 if fails else 0)
> PY
> for d in skills/*/; do npx --yes skills-ref validate "$d"; done
> # 守門 grep：照 CLAUDE.md 鐵則 1 那條跑（CI 版含白名單，見 ci.yml 第 3 步；不在此重抄一份）
> test "$(wc -l < README.md)" -eq "$(wc -l < README.en.md)"
> for s in sh dash bash ksh zsh; do SH=$s sh skills/ai-review/tests/matrix.sh; done
> for s in sh dash bash ksh zsh; do SH=$s sh skills/ai-search/tests/matrix.sh; done
> python3 skills/mission-log/tests/harvest_test.py
> LC_ALL=C python3 skills/mission-log/tests/harvest_test.py
> ```

### REG-01 🟢 YAML frontmatter 雙 parser 驗證

```bash
python3 -c "
import yaml,glob,re
for f in sorted(glob.glob('skills/*/SKILL.md')):
    m=re.match(r'^---\n(.*?)\n---\n',open(f).read(),re.S)
    try: print('OK  ',f,list(yaml.safe_load(m.group(1)).keys()))
    except Exception as e: print('FAIL',f,type(e).__name__)
"
```

**通過**：全部 skill（現為 12 支）全 `OK`。有第二個 parser（ruby psych／js-yaml）就交叉驗。這是 v0.1.1 事故的直接回歸項。
⚠️ 驗證器自己也要驗：跑一次**故意壞掉的 YAML**（如 `description: bad: colon`）確認它真的會 FAIL，否則你可能在看一個永遠說 OK 的空轉腳本。

### REG-02 🟢 agentskills.io 官方 validator

```bash
for d in skills/*/; do npx --yes skills-ref validate "$d"; done
```

**通過**：全數（現為 12 支）全過。驗 name 格式（小寫/連字號/=目錄名）、description ≤1024 字等規格硬約束。

### REG-03 🟢 本地安裝煙霧測試

```bash
cd "$(mktemp -d)" && git init -q . && npx --yes skills@latest add <repo根目錄> -y
```

**通過**：回報 `Installed 12 skills`（與 repo 現有支數一致）、0 個 Skipped。

### REG-04 🟢 公開內容守門

跑 repo `CLAUDE.md` 鐵則 1 的守門 grep（含 `--untracked` 與 `-i`），除已知預期命中外 0 命中。

### REG-05 🟢 文件連動掃描

依 `CLAUDE.md` 鐵則 3 連動清單逐項核對（README 雙檔／plugin.json／marketplace.json／adapters 矩陣）。

---

## B. 跨工具相容（宣稱相容的每個工具，逐層驗證）

### CROSS-01 🟢 多 agent 安裝層矩陣

```bash
for a in gemini-cli codex cursor github-copilot; do
  (cd "$(mktemp -d)" && git init -q . && npx --yes skills@latest add <repo根目錄> --agent $a -y)
done
```

**通過**：每個 agent 都回報 Installed 支數=repo 現有支數、0 Skipped。
狀態（2026-07-30）：gemini-cli／codex 已實測通過；cursor／github-copilot 未實測。

### CROSS-02 🟡 Gemini CLI 發現層（trusted-folder 關卡）

```bash
gemini skills list --all   # 分別在「未信任」與「已信任」的專案目錄各跑一次
```

**通過**：未信任時輸出含 `Skipping project agents due to untrusted folder`（skill 不出現＝**預期行為**，不是 bug）；信任該資料夾後全數 skill 列出並標 `[Enabled]`。
⚠️ 這道關卡是無聲的（不報錯），文件必須揭露，否則使用者會以為安裝失敗。建議用隔離 `HOME` 測「已信任」情境，避免動到真實 `~/.gemini/trustedFolders.json`。
狀態（2026-07-30，gemini 0.40.0）：未信任情境已實測吻合；已信任情境未實測。

### CROSS-03 🟡 Codex CLI 發現層（原生注入驗證）

```bash
# 於已用 --agent codex 裝好的專案目錄：
codex debug prompt-input "test" | grep -E "dropoff|pickup|save-all|token-optimizer|flight-to-calendar"
```

**通過**:全數 skill 的 name＋description 均出現在 model-visible prompt 的 skills 區塊。
狀態（2026-07-30，codex-cli 0.145.0）：已實測通過——Codex 有原生 skill 機制（`~/.codex/skills/.system/`），**不需**手動併入 AGENTS.md。

### CROSS-04 🟢 ChatGPT 消費版陰性對照

```bash
npx --yes skills@latest add <repo根目錄> --agent chatgpt -y
```

**通過**：CLI 回報 `Invalid agents: chatgpt`（**預期失敗**）。ChatGPT 消費版無檔案系統／無 CLI，唯一路徑是 `adapters/openai/` 的手動貼入法。此項用來持續確認該定位仍準確。

### CROSS-05 ✋ 執行層端到端（每個 release 至少抽測一支）

在隔離目錄以各 agent 非互動模式觸發低風險 skill（建議 `dropoff`）：

```bash
codex exec "觸發 dropoff：幫示範任務寫一張交接卡"     # 或 gemini -p "..."
```

**通過**：真的產出交接卡檔案，frontmatter（status/from/to/created）齊全、內容符合 SKILL.md 步驟。需人工核對格式，「有產出檔案」不算過。
狀態（2026-07-30）：未實測（gemini／codex 皆在場可測）。

### CROSS-06 🟢 事故回歸（YAML × 真實安裝）

REG-01 ＋ REG-03 合跑。此缺陷影響**所有** npx-skills 下游 agent，不是 Claude Code 特有——發布關卡的涵蓋範圍要與此對齊。

### CROSS-07 ✋ 矩陣宣稱 × 實測交叉稽核

README 相容性矩陣與 adapters 上的每個 ✅／⚠️／❌，都要能對應到一次實際指令輸出佐證（本項曾抓到 adapters 對 Codex 的描述整段過時）。有落差→下個 release tag 前修文件或重測。

---

## J. 日誌三支（mission-log／daily-debrief／weekly-debrief，v0.3.0 起）

### J-01 🟢 收割器零 token 實跑

```bash
python3 skills/mission-log/scripts/harvest.py --date <近 7 天內某日>
```

**通過**：列出該日活躍 session（時間段／專案名／turns／tokens／工具／原話），全程無模型呼叫；中文專案名正確顯示（cwd 欄位路徑，非目錄編碼的 `-----`）。
狀態（2026-08-02~03）：✅ 已實測（端到端）。

### J-02 🟢 跨機收割（ssh 餵單檔）

```bash
ssh <主機> "python3 - --date <日期>" < skills/mission-log/scripts/harvest.py
```

**通過**：遠端輸出自帶主機名；⚠️ 別加 `ssh -n`（stdin 會被接到 /dev/null，腳本餵不進去——實測踩過）。
狀態（2026-08-02~03）：✅ 已實測（端到端，跨機餵單檔）。

### J-03 ✋ 日期參數解析

`/daily-debrief`（今天）／`yesterday`／`YYYY-MM-DD`；`/weekly-debrief`／`last`／`YYYY-Www`／任一日期落到該週。**通過**：各形式都收割到正確日期範圍。
狀態（2026-08-02~03 試用期）：⚠️ 部分實測（未逐項驗全）。

### J-04 ✋ daily 冪等與誠實

同一天重跑 → 覆蓋更新不疊加；該日無記錄 → 檔案照寫、內容明講原因；summary 只含骨架撐得起的敘述（抽查比對原話）。
狀態（2026-08-02~03 試用期）：⚠️ 部分實測（未逐項驗全）。

### J-05 ✋ weekly 缺口自癒

刪掉該週某天的日報 md 再跑 weekly → 自動補生成；把日期設到保留期外 → 週報標「無記錄」而非報錯。
狀態：❓ 從未跑過（首次真實使用即為驗證）。

### J-06 🟢 歸檔搬移

放一個假日期（>30 天前）的日報檔再跑 daily → 檔案被移入 `journal/archive/YYYY-MM/`，且 weekly 不讀 archive。
狀態（2026-08-02~03）：✅ 已實測（端到端）。

---

## D. 交接門鈴（dropoff/pickup 的跨 session 即時通知，v0.4.0 起）

> 底層＝Claude Code v2.1.224+ 的 cross-session messaging（`ListAgents`＋`SendMessage`；桌面 app 為 session 管理工具變體，以 sessionId 定址）。官方支援 macOS／Linux；送往 bypass-permissions session 的訊息會被暫留待人工核准（`crossSessionInbound`）。版本與行為敘述已對照官方 changelog 與 docs 查證（2026-08-09）。設計原則：門鈴只是通知，卡片檔案才是真相——以下任何一項失敗都不得影響交接成立。

### D-01 ✋ 去程：閒置 session 被門鈴喚醒並開工

發訊給一個閒置（非執行中）的 session，內含卡片路徑與接手指示。
**通過**：對面無需人工介入即開始處理（讀卡、認領）。
狀態（2026-08-09）：✅ 已實測（桌面變體）——閒置 session 39 秒內完成「收訊→執行指令→落檔」。

### D-02 ✋ 回程：完成回訊送達來源 session

接手方完成後回訊來源 session（from 位址）。
**通過**：來源 session 收到完成訊息並被觸發。
狀態（2026-08-09）：✅ 已實測（桌面變體，雙向閉環成立）。

### D-03 ✋ CLI 原生變體（terminal 間、名稱定址）

兩個 terminal `claude` session 間以 `ListAgents`＋`SendMessage` 完成 D-01/D-02 同款流程。
**通過**：同 D-01/D-02。
狀態（2026-08-09）：✅ 已實測（首次真實交接即驗證）——發送端為 headless `-p` session，`ListAgents` 以名稱定址找到 tmux 內的互動 session、`SendMessage` 送達（回執含 msg_id）；接收端互動 session 無人工介入即開工跑 pickup 流程。附帶發現：headless `-p` **能發不能收**（官方文件僅載明不能收）；名稱定址有時要求帶短識別碼（裸名被拒、`名稱 [ref]` 成功）。後續同日：互動 terminal 當發送端亦經真實回訊驗證，且該回訊**跨機**送達另一台機器的桌面 session（經雲端 bridge 定址；單次觀察、機制歸因未確認，勿當保證）。

### D-04 ✋ 靜默檔（「不用即時通知」）

dropoff 時使用者說「不用即時通知」→ 不發訊、卡上 `notify: silent`；後續 /pickup 掃卡仍撈得到。
**通過**：無訊息送出且卡片欄位正確。
狀態（2026-08-09）：❓ 未實測（規則層，首次真實使用即為驗證）。

### D-05 ✋ 無能力環境降級

在無跨 session 傳訊工具的環境（Gemini CLI／Codex／Cowork）跑 dropoff。
**通過**：門鈴步驟被跳過、無報錯、交接卡照常成立。
狀態（2026-08-09）：❓ 未實測。

### D-06 ✋ 誤喚醒防呆（新鮮度三分支＋路由記憶）

dropoff 篩目標專案候選（新鮮＝7 天內活躍）的三種情況，逐一驗：
**通過**：恰好一個新鮮候選 → 自動發訊；超過一個 → 列出問使用者挑，且選擇被記進 `.claude/handoffs/routing.md`（下次同目標直接用，該 session 消失／過期則重問並更新）；一個都沒有（全過期）→ 仍問使用者（硬按過期的 or 只留卡），不自動喚醒沉睡 session。
狀態（2026-08-09）：❓ 未實測（規則層；由真實誤喚醒事故反推立規，首次觸發即驗證）。

---

## E. ai-review（跨模型二審，v0.5.0 起）

> 腳本＝`skills/ai-review/scripts/ai-review.sh`（單檔 POSIX shell）。設計原則：**狀態走 stdout、
> 退出碼只分真失敗** —— 沒裝後端是預期中的降級，不得中斷上層流程。

### E-01 🟢 多 shell 語法與行為矩陣

```bash
for s in /bin/sh /bin/dash /bin/bash /bin/ksh /bin/zsh; do $s -n skills/ai-review/scripts/ai-review.sh; done
```

行為矩陣**已隨 skill 出貨**＝`skills/ai-review/tests/matrix.sh`（41 項，stub 後端不燒額度、
產出寫暫存區不弄髒目錄、開頭自動 `unset` 外部 `AI_REVIEW_*` 變數以隔離環境、全過回 0 可進 CI）：

```bash
for s in /bin/sh /bin/dash /bin/bash /bin/ksh /bin/zsh; do SH=$s sh skills/ai-review/tests/matrix.sh; done
```

**通過**：語法全過；每個 shell 皆 41/41（缺 `python3`+`pyyaml` 時 40 過 1 略過）。
狀態（2026-08-21）：✅ 已實測，macOS 27 上 5 shell 皆 41/41；外部 `export AI_REVIEW_CMD` 污染下結果不變（隔離生效）；`npx skills add` 後 `tests/` 隨 skill 一起裝出（2026-08-20 驗）。

### E-02 🟡 真實後端 ok 路徑

```bash
skills/ai-review/scripts/ai-review.sh <某檔> --rubric code
```

**通過**：回 `AI_REVIEW_STATUS: ok`、退出碼 0、意見內容確實依 rubric 分項（不是錯誤頁）。
狀態（2026-08-20）：✅ 已實測（Codex CLI 0.148.0，真的抓出示範檔的邏輯錯誤）。

### E-03 ✋ 降級不中斷上層

在 `set -e` 的 wrapper 內、且輸出被 `$(…)` 捕捉的情況下，對「沒有後端」的環境呼叫。
**通過**：wrapper 繼續往下跑、退出碼 0、狀態為 `skipped_not_installed`。
狀態（2026-08-20）：✅ 已實測（含 `--strict` 反向確認會回 3）。

### E-04 ✋ 未登入偵測

登出後端後呼叫（或以 stub 模擬 `codex login status` 回「not logged in」）。
**通過**：狀態為 `skipped_not_logged_in`、退出碼 0，且引導文字講的是**登入**不是重裝。
狀態（2026-08-20）：⚠️ **僅以 stub 實測**；沒有真的把帳號登出驗過（會影響使用中的環境）。

### E-06 🟢 跨模型二審抓到的缺陷回歸（v0.5.1）

v0.5.0 出貨後把「SKILL.md＋腳本」整包送 GPT 與 Gemini 各審一次，兩邊獨立指出同一批缺陷。
逐項回歸（納入 E-01 的矩陣，每個 shell 都跑）：

1. `AI_REVIEW_CMD` 指到不存在的命令（exit 127）→ 必須 `skipped_not_installed`＋exit 0
   （原本落進 `failed_unknown`＋exit 2，**直接違反「沒有後端不得中斷上層流程」的硬需求**）。
2. 自訂後端回「not logged in」→ `skipped_not_logged_in`＋exit 0（原本被強制轉成 `failed_unknown`）。
3. `authorization policy denied`／`auth service unavailable` 這類**真失敗不得被吞成 skipped**
   （原本分類器有一條模糊的 `*auth*`，會讓真失敗安靜地回 exit 0）。
4. `--effort` 非白名單值 → exit 1（原本會送進後端，最後被誤判成「版本不相容」並給錯引導；
   值也會被插進 `-c model_reasoning_effort="…"`）。
5. 一次給兩份來源檔 → exit 1（原本靜默只審最後一份）。
6. 同一秒跑兩次 → 落檔不互相覆蓋（檔名加 PID）。
7. 檔名含冒號／空白 → frontmatter 仍是合法 YAML（值加引號並跳脫）。
8. 後端 exit 0 但回空 → 照印原始 stderr（SKILL.md 宣稱「失敗時一律照印」，原本空回覆這條沒做到）。

9. `--soft-fail` → `failed_*` 也回 exit 0（狀態字串照印）；不加時維持回 2。

狀態（2026-08-20）：✅ 全數實測通過（5 shell × 28 項）。

### E-07 🟢 第二輪跨模型二審的回歸（v0.5.3）

v0.5.1／v0.5.2 之後再送一次 GPT＋Gemini。這輪抓到的是**修法本身帶出來的新問題**：

1. `--strict` 與 `--soft-fail` 併用語意衝突（一個要把「沒審到」變失敗、一個要把失敗變沒事）→ 直接報錯 exit 1。
2. 自訂 rubric 是路徑時（`--rubric ../shared/x.md`），路徑會被塞進落檔檔名 → 帶著 `/`、`..` 寫到別的目錄。改成內建三份保留原名、其餘一律 `custom`。
3. 後端把錯誤寫到 **stdout 後回非零**：原本只看 stderr，分類拿到空字串、畫面印「原始錯誤」卻沒東西 → 改成 stderr＋stdout 都納入分類並照印。
4. `--` 沒有真正停止解析選項 → 改成 `--` 之後全部當來源檔。
5. 落檔非原子、且會跟隨既有 symlink → 暫存檔改建在**目標目錄內**再 `mv`（同檔案系統的 rename 才是原子）。
6. **上一輪的修法自己帶出的風險**：登入偵測加了裸的 `*expired*`／`*sign in*`，但「已登入」訊息也可能含 `sign-in method`／`session expires` → 會把正常登入誤判成沒登入而白白略過二審。改成只收 `token expired`／`please sign in` 這種明確片語。
7. `cut -c` 在非 UTF-8 locale 下切的是 bytes，長中文檔名會被切出殘缺位元組 → 依 locale 分流（UTF-8 保留原名，其餘降級成 ASCII 安全字元）。

狀態（2026-08-20）：✅ 全數實測通過（5 shell × 35 項；含 `LC_ALL=C` 下的長中文檔名落檔）。

### E-08 🟡 第三方環境獨立複驗（v0.5.3）

在**另一個專案目錄**以 `npx skills add` 裝好後，用同一份行為矩陣獨立再跑一次，
並實測 skill 層（發現、觸發、有沒有真的去跑腳本、有沒有消化意見）。
**通過**：矩陣同樣全過；skill 被列出；觸發後真的執行腳本並依 `AI_REVIEW_STATUS` 定調。
狀態（2026-08-20）：✅ 已由另一個 session 在獨立沙盒複驗（35/35、真實後端 ok、降級 rc=0、
skill 層三項行為皆正確、damage-report 整合節可達）。

⚠️ 該次複驗抓到**兩層命名碰撞**，是本專案兩輪跨模型二審都沒看到的角度：

1. **skill 觸發詞碰撞**：使用者若已有同義的個人 skill（例如另一支也宣稱「二審／cross-model review」
   的 skill），**personal 層優先於 project 層** —— 喊觸發詞會叫到那一支，測起來像本 skill 壞了。
   測試時用明確指名（Skill 工具指定名稱、或直接讀本 skill 的 SKILL.md）繞過。
2. **PATH 執行檔碰撞**：`ai-review` 是很容易撞名的命令名。若使用者 `PATH` 上有另一支同名執行檔，
   打**裸命令名**會叫到那一支，而且它可能看起來也在做二審 —— 不會報錯，只會安靜地測錯對象。
   ⇒ 本 skill 與 damage-report 的文件一律用**完整路徑**呼叫腳本，這是刻意的。

### E-09 🟢 第三輪跨模型二審的回歸（v0.5.4）

第三輪只有 GPT 一腿（Gemini 免費額度當日用盡），但抓到的全是**第二輪修法自己帶出來的**：

1. **可預測的暫存檔名**（`.ai-review.<pid>.tmp`）：別人可在共用落檔目錄先放同名 symlink，
   `>` 會跟著它把別的檔案截斷 → 改用 `mktemp` 產生不可預測檔名並 `chmod 600`。
   回歸測試直接放一個惡意 symlink，確認受害檔案沒被寫穿。
2. **群組寫入吞掉中途失敗**：`{ …; cat X; printf '\n'; } > f` 只要最後一個 `printf` 成功，
   整組就回 0 —— 於是「送出殘缺 prompt」與「存下被截斷的審閱結果」都會被判成成功。
   改成群組內 `&&` 串接，任一步失敗即中止。
3. **裸的 `401` 比對**（登入偵測與後端分類器各一份）：`session expires in 401 seconds`、
   request id 含 401 都會命中 → 真失敗被判成 skipped（exit 0）。收窄成
   `http 401`／`status 401`／`401 unauthorized` 等帶語境寫法。
   ⚠️ 第一次修還修錯：改成 `*"401 "*` 仍會命中「401 seconds」，是**回歸測試自己抓出來的**。
4. 契約對齊：`dump_backend_output` 只印尾 20 行，文件原本寫「都照印」→ 改成「各印尾 20 行」，
   並警告該輸出可能含 secrets。落檔改為 600 並在文件說明。

狀態（2026-08-20）：✅ 全數實測通過（5 shell × 40 項，含 symlink 攻擊與落檔權限 600 檢查）。

### E-05 ✋ 平台與方案覆蓋

>（編號跳序＝歷史演進痕跡，刻意保留：E-05 性質是收尾總查，物理位置固定在 E-09 之後，編號不重排。）

**通過**：README 宣稱支援的平台與帳號方案都實跑過。
狀態（2026-08-21）：✅ **Linux 經 CI（ubuntu-latest）**——矩陣 5 shell × 41 項與 harvest 測試隨每次 push 實跑；
❌ **Windows 未實測**；❌ **免費方案帳號未實測** ——
官方用量限制表不含免費方案（見 `AI_REVIEW_SOURCES.md`），因此無法宣稱免費帳號開箱即用。
README 與 SKILL.md 均未作此宣稱。要宣稱前先補這兩格。

---

## F. ai-search（帶引用即時查證）

> 腳本＝`skills/ai-search/scripts/ai-search.sh`（單檔 POSIX shell，與 ai-review 同架構）。
> 設計原則相同：**狀態走 stdout、退出碼只分真失敗**，沒裝後端是預期降級、不得中斷上層。
> 🔴 誠實註記：ai-search **尚未**經過 ai-review 那樣的多輪跨「模型家族」二審 —— 但已過一輪
> 三視角對抗式自審（shell 正確性／去識別化／連動完整性，抓到並修掉 2 個真缺陷：檔名控制字元
> 注入、`-- -` stdin 邊界）。真實後端 ok 路徑已**單次**實測（F-02／F-04，2026-08-24），尚未逐項回歸。

### F-01 🟢 多 shell 語法與行為矩陣

```bash
for s in /bin/sh /bin/dash /bin/bash /bin/ksh /bin/zsh; do $s -n skills/ai-search/scripts/ai-search.sh; done
for s in /bin/sh /bin/dash /bin/bash /bin/ksh /bin/zsh; do SH=$s sh skills/ai-search/tests/matrix.sh; done
```

行為矩陣隨 skill 出貨＝`skills/ai-search/tests/matrix.sh`（43 項，stub 後端**不燒額度、不連網**、
產出寫暫存區、開頭自動 `unset` 外部 `AI_SEARCH_*` 變數以隔離環境）。涵蓋：後端錯誤分類、
`--strict`／`--soft-fail`、可插拔後端、`set -e`＋`$(…)` 呼叫鏈（含負對照：真失敗必須中斷上層）、
問題輸入多形式（位置參數接句／stdin `-`／`--` 之後當文字）、分類器不把真失敗吞成 skipped、
落檔不覆蓋／600／防 symlink、含冒號引號的問題 frontmatter 仍是合法 YAML、非 UTF-8 locale 長中文落檔。
**通過**：語法全過；每個 shell 皆 43/43（缺 `python3`+`pyyaml` 時 42 過 1 略過）。
狀態（2026-08-24）：✅ 已於 macOS 27 五 shell 實測 42 過 1 略（本機無 pyyaml；frontmatter 另以
`/usr/bin/python3`+yaml 6.0.3 獨立驗過為合法）；Linux 由 CI（ubuntu-latest）每次 push 實跑。

### F-02 🟡 真實後端 ok 路徑

```bash
skills/ai-search/scripts/ai-search.sh "某個需要查證現況的問題"
```

**通過**：回 `AI_SEARCH_STATUS: ok`、退出碼 0、答案結論先行且附**可點的來源連結**（不是錯誤頁），
內容反映當前網路資訊而非訓練知識填答。
狀態（2026-08-24）：✅ **已單次實測**（Codex CLI，真實 `web_search` 後端）——問「今天日期＋Codex CLI
最新穩定版」，回 `AI_SEARCH_STATUS: ok`／rc 0、結論先行、附 3 個來源連結（官方 GitHub release／npm／
timeanddate）並自行分級官方vs二手，且答出當日 release `0.149.1`（新過本機安裝版＝確實查了即時網路、
非訓練知識）；預設落檔的 frontmatter 正確（含 `'` 的問題在 YAML 內跳脫無誤）。尚未逐項回歸。

### F-03 ✋ 降級不中斷上層

在 `set -e` 的 wrapper 內、輸出被 `$(…)` 捕捉時，對「沒有後端」的環境呼叫。
**通過**：wrapper 繼續往下跑、退出碼 0、狀態 `skipped_not_installed`；`--strict` 反向確認回 3。
狀態（2026-08-24）：✅ 已由矩陣 B／D 段涵蓋（stub 實測，5 shell）。

### F-04 🟡 web_search 旗標實際生效

真實後端下，確認腳本送的 `-c tools.web_search=true` 真的讓後端上網（而非拿舊知識答）。
**通過**：答案含**當下**才查得到的事實與來源連結。
狀態（2026-08-24）：✅ 已實測（**間接證據**）——同一次 F-02 run 的答案含當日 release 版本與可點來源，非舊知識填答。⚠️ 單次陽性、未做「移除旗標」的負對照，「是這個旗標使然」屬推論（後端也可能預設開搜尋）；補負對照時歸本項。

---

## C. 相容性結論快照（安裝層 2026-08-21 重驗；發現層仍為 2026-07-30 快照，過期重驗）

| 工具 | 安裝層 | 發現層 | 執行層 |
|---|---|---|---|
| Claude Code CLI | ✅ 實測（npx／plugin 雙路徑） | ✅ | ✅（日常使用） |
| Gemini CLI 0.40.0 | ✅ 實測 10/10（2026-08-21） | ⚠️ 需先信任資料夾（無聲關卡） | ❓ 未測 |
| Codex CLI 0.148.0 | ✅ 實測 10/10（2026-08-21） | ✅ 實測（原生注入 prompt；於 0.145.0 實測） | ❓ 未測 |
| ChatGPT 消費版 | ❌ 無安裝路徑（產品限制） | ❌ 無 skill 概念 | 僅手動貼入，網頁端人工驗 |
| Cursor／Copilot 等 | ❓ `npx skills` 支援但未實測 | ❓ | ❓ |

註（2026-08-09）：v0.4.0 的交接門鈴（D 段）為 Claude Code 限定的選用增強，各工具評級不因此變動——非 Claude Code 環境自動降級純檔案交接（見 README 矩陣註³）。

註（2026-08-24）：新增 ai-search（第 11 支）。當日重跑 REG-03 通用安裝煙霧＝**Installed 11 skills**（`npx skills add` 同時裝進 Claude Code／Codex／Gemini CLI／Copilot 等目標，ai-search 含 `tests/` 一起裝出）。表內各 CLI 版本的 **10/10 是 ai-search 之前的逐一 `--agent` 快照**（2026-08-21），per-agent 11 支的 CROSS-01 重驗待補；評級不因新增一支而變動。

註（2026-08-28）：新增 new-mission（第 12 支，純規則零依賴，比照 damage-report 無專屬測試段；prompts/ 另有免安裝簡版兩檔）。當日重跑 REG-03 通用安裝煙霧＝**Installed 12 skills**、new-mission 在列、0 Skipped。per-agent 逐一 `--agent` 的 CROSS-01 重驗仍待補（同上註）；評級不因新增而變動。
