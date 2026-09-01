---
name: save-all
description: '重開機／關機／收工前的「落地同步」收尾檢查。在每個還開著的對話 session 裡各跑一次，把只活在這個對話裡、還沒寫進磁碟的東西全部外部化並驗證真的落地，然後回報「安全可重開 / 還有 X 沒同步」。當使用者說「要重開機了」「save-all」「/save-all」「準備重開」「關機前收尾」「reboot 前」「今天收工」時觸發。⚠️ 本 skill 只做落地＋回報，不重開機器。 English triggers: "save-all", "about to reboot", "shutting down", "wrapping up for today", "before restart".'
---

# /save-all — 收工前的落地收尾

重開機（或單純收工）前，在**每個還開著的對話 session** 裡各跑一次。

## 為什麼需要

- Session 一關，**對話 context 就沒了**。「只活在這個對話裡、還沒寫進檔案的東西」—— 學到的教訓、剛冒出來的待辦、做到一半的工作狀態 —— 全部蒸發。
- 落地 = 把它們外部化成磁碟上的檔案。
- 而且**寫入可能靜默失敗**（工具回「成功」但檔案沒變的情況真實存在）→ **落地後一定要驗證真的寫進磁碟**。這是本 skill 的鐵則，不是可選步驟。

> 🤖 R2-D2 時刻：Death Star 圖紙如果只存在 Leia 的腦子裡，Tantive IV 被攔截時就全劇終了。
> 存進 R2、彈射逃生艙 —— 資料落地，故事才能繼續。

## 動作（依序做完、逐項回報）

### 0. 校時，並把時間報成回覆第一行

**⏰ 回覆的第一行固定是** `⏰ <YYYY-MM-DD（週幾）HH:MM> <IANA 時區>` —— 例 `⏰ 2026-03-09（一）10:12 Europe/Berlin`。落地紀錄的時間別憑印象寫；時區也不可省，人與機器不在同一時區時，沒有時區的時間讀不出是哪裡的幾點。

零依賴取法（macOS／Linux 通用，不需 python），順手把**起點**落成檔案 —— 第 5 步要讀它算耗時：

```bash
tz=${TZ:-$(readlink -f /etc/localtime 2>/dev/null || readlink /etc/localtime 2>/dev/null)}
case $tz in */zoneinfo/*) tz=${tz#*/zoneinfo/} ;; /*) tz=$(cat /etc/timezone 2>/dev/null) ;; esac
case $(date +%u) in 1) w=一;; 2) w=二;; 3) w=三;; 4) w=四;; 5) w=五;; 6) w=六;; 7) w=日;; esac
printf '⏰ %s（%s）%s %s\n' "$(date +%F)" "$w" "$(date +%H:%M)" "${tz:-時區未知}"
M=/tmp/save-all-$(date +%Y%m%d-%H%M%S).start; date +%s > "$M"; echo "起點檔=$M"
```

🚫 **別用 `date '+%A'` 拿星期**（給的是 locale 決定的 `Tuesday`／`星期二`，都不是「（二）」），也別拿 `date +%Z` 當時區（那是 `CST` 這類縮寫、不是 IANA 名稱）；上面用 `%u` 對照表與 locale 無關。`readlink` 的 `-f` 與那行 `case` 也不能省：有些發行版的 `/etc/localtime` 是**多層**符號連結，只解一層會把中繼路徑當成時區印出來，而「有值」正好躲過 `${tz:-時區未知}` 的防呆——格式合格、內容是錯的，最難發現。切不出 IANA 名稱時寧可印「時區未知」。⚠️ 已知限制：`$TZ` 是 `PST8PDT` 這類舊式 POSIX 字串時會照原樣印出，🚫 別改印系統時區去頂替（會印出跟實際時間對不上的假名稱）。

**記住輸出的起點檔路徑**，第 5 步要用。回覆語言不是中文時，週幾寫該語言慣用形式即可，不可省的是時區。

跑不了 shell 的環境（網頁版、無終端機）：用當回合系統給的時刻並自己補時區標籤；拿不到完整值就寫「時間取不到完整值」，🚫 別包裝成看起來合格的時間行 —— 這種環境也沒有起點檔，第 5 步的耗時照實寫「未知」。

### 1. 盤點本 session 的 in-flight

逐項自問「session 關掉後這個還在嗎？不在就落地」：

- **① 學到的教訓／決策／意外發現** → 該進長期記憶的（memory 目錄、專案筆記）
- **② 本 session 新長出的待辦** → 該進你的任務清單的
- **③ 要交給別專案／未來 session 的事** → 該寫交接卡的（→ `/dropoff`）
- **④ 改過的系統性設置**（裝/改/停排程、改自動化腳本、改設定檔）→ 該記進專案文件的
- **⑤ 進行到一半的工作敘事** → 該寫 HANDOFF／進度筆記的

若本 session 閒置、什麼 in-flight 都沒有 → 直接跳到第 5 步回報「無 in-flight、安全可重開」，**別硬湊**。

### 2. 逐項落地

- ①④⑤ → 寫進對應的筆記／memory／文件檔。
- ② → 進你的任務系統（沒有就寫進專案 TODO 檔）。
- ③ → 跑 `/dropoff` 寫交接卡（有這個 skill 就用它，別在這裡重寫邏輯）。

**每寫一項立刻驗證**：`wc -l` 看行數、`stat` 看 mtime 是剛剛、`grep` 新內容有命中 → 確認真落地。沒落地就重寫，**別信工具回的「成功」字面**。

### 3. flush git（若本 session 動過 git 管理的目錄）

有 auto-sync 的目錄也一樣 —— 重開可能發生在下次自動同步之前，手動推一次別等：

```bash
git add -A && git commit -m "pre-reboot flush $(date +%m%d-%H%M)" && git push
```

（commit 前瞄一眼 `git status`，確認沒把不該進 repo 的東西帶進去。）

### 4. 統計本次 save-all 消費的 token（Claude Code CLI 限定，選跑）

> Cowork／claude.ai 環境沒有本機 transcript，這一步直接跳過，不影響其他步驟。

讀本 session transcript 的真實 `usage`（不要自估 —— 模型無法可靠內省自身用量）：

```bash
tx=$(find ~/.claude/projects -name "${CLAUDE_CODE_SESSION_ID}.jsonl" 2>/dev/null | head -1)
[ -n "$tx" ] && python3 - "$tx" <<'PY'
import json,sys
i=o=cc=cr=n=0
for line in open(sys.argv[1]):
    try: u=json.loads(line).get('message',{}).get('usage')
    except: continue
    if not isinstance(u,dict): continue
    i+=u.get('input_tokens',0); o+=u.get('output_tokens',0)
    cc+=u.get('cache_creation_input_tokens',0); cr+=u.get('cache_read_input_tokens',0); n+=1
print(f"session 累計 {n} turns: input={i:,} output={o:,} cache_creation={cc:,} cache_read={cr:,}")
PY
```

### 5. 回報 go/no-go

逐項複驗後給明確信號：

- ✅ **全部落地、無 in-flight 殘留** → 「安全可重開」＋列出這次落地了什麼、寫進哪個檔。
- ⚠️ **還有 X 沒同步** → 列出未落地項目與原因，「建議先處理 X 再重開，或你接受風險重開（會掉 X）」，讓使用者決定。

**末尾必附時間行**（第 4 步有跑的話，token 統計行接在它後面）：

```bash
M=/tmp/save-all-YYYYmmdd-HHMMSS.start   # ← 換成第 0 步印出的那個路徑
tz=${TZ:-$(readlink -f /etc/localtime 2>/dev/null || readlink /etc/localtime 2>/dev/null)}
case $tz in */zoneinfo/*) tz=${tz#*/zoneinfo/} ;; /*) tz=$(cat /etc/timezone 2>/dev/null) ;; esac
case $(date +%u) in 1) w=一;; 2) w=二;; 3) w=三;; 4) w=四;; 5) w=五;; 6) w=六;; 7) w=日;; esac
s=$(cat "$M" 2>/dev/null)
case $s in ''|*[!0-9]*) e="耗時未知" ;; *) e="距起點 $(( ( $(date +%s) - s ) / 60 )) 分" ;; esac
printf '⏰ %s（%s）%s %s（%s）\n' "$(date +%F)" "$w" "$(date +%H:%M)" "${tz:-時區未知}" "$e"
```

🔴 **這一行要現跑，不可沿用第 0 步那一行** —— 這支常跑二三十分鐘，沿用就是報一個過期的時間。**耗時交給 shell 從起點檔算**，不要自己心算。起點檔讀不到或內容壞掉時，上面的 `case` 會直接印「耗時未知」——照那個輸出貼，🚫 不要自己補一個看起來合理的數字。

⚠️ 誠實界定：耗時是機器算出來的、可信；但「這一行確實是現跑的」**沒有機械證據**能自證 —— 別聲稱有，也別用頭尾差值當證明（同一分鐘收工差值本來就是 0，而差值本身照樣可以用寫的）。這條規則靠的是紀律，不是防呆。

## 鐵律

- 🚫 **本 skill 不重開機器**，只做落地＋回報；重開由使用者自己按。
- ⏰ **頭尾都報時間**（含 IANA 時區）：結尾那行現跑、耗時由機器從起點檔算，不沿用開頭那行、不心算。
- ✅ **落地一定驗證真的寫進去**。
- 🔁 **每個開著的 session 各跑一次** —— 各 session 只知道自己的 in-flight，別想在一個 session 裡代跑別的。
- 📝 **隨做隨記、逐項回報**，別等最後批次補。
