# MyR2D2 — 開發約定

MyR2D2 是**公開開源**的 Claude skillset repo（繁中本體、中英雙語觸發詞）。任何 session 在本目錄工作時，必須遵守以下約定。

> 若本機存在 `.claude/local-rules.md`（不進 repo），開工前一併讀 —— 那裡放的是不適合公開的本機守門規則。

---

## 🔒 鐵則 1：這是公開 repo，動筆前先去識別化

repo 裡的 skill 與作者本機 `~/.claude/skills/` 的同名版本**是兩份不同的文件**，不是新舊版。公開版是把私人版泛化後的產物，抽換規則如下（每一條都有既成實例，照著做就不會漏）：

| 私人版寫法 | 公開版必須改成 |
|---|---|
| 真實人名（「當 Eric 說…」） | 一律「使用者」 |
| 私人專案／品牌代號、客戶名、人選姓名 | 刪除，或抽象成 `<專案>` 佔位符 |
| 私有 CLI／服務（自製 todo、Notion、Telegram、私人腳本路徑） | 換成零依賴的通用機制，並在文末補一節「進階：接上你自己的任務系統」 |
| 私人絕對路徑（`~/Documents/...`、`~/ClaudeProjects/<私人專案>`） | 刪除；範例路徑一律用 `~` 或 `<project>` 泛型佔位符 |
| 指向作者私人設定檔某節的交叉引用 | 改寫成自包含敘述，公開版不得依賴讀者看不到的檔案 |
| 寫死的模型型號（`Fable 5`／`Opus 5`／`opus`） | 檔位語彙：**旗艦檔／中檔（如 `sonnet`）／低檔（如 `haiku`）** |
| 帶日期的本機實測記錄（「2026-07-09 三連實測」） | 刪除日期戳，只留可長期成立的結論 |
| 跨機／iCloud／特定主機的環境細節 | 刪除，公開版假設單機通用環境 |
| 中文 skill 目錄名 | 英文 kebab-case 目錄名（`flight-to-calendar`、`save-all`） |

**送出前守門（commit 前跑，不是想到才跑）**：

```bash
git grep --untracked -inE "/Users/|~/(Documents|ClaudeProjects)/|Notion|telegram"
```

三個旗標／片段都是踩過坑才加的，別省：

- **`--untracked`** —— 少了它就只掃已追蹤檔案，剛寫好還沒 `git add` 的新 SKILL.md 會被跳過，而那正是外洩最可能發生的時機。它同時自動略過 `.gitignore` 排除的路徑。
- **`-i`** —— 少了它，官方拼法的 `Telegram`／`Notion` 全部漏網（私人版原文幾乎都是首字大寫）。
- **`~/(Documents|ClaudeProjects)/`** —— 只找 `/Users/` 抓不到寫成 `~/` 的私人路徑，而上表舉的例子本身就是 `~/` 記法。

⚠️ 這條指令是**篩子不是保險絲**：它掃的是 repo 檔案樹，抓得到的只有列進 pattern 的字面。跑過不等於乾淨，仍要人工複查。

**已知的預期命中**（不是外洩，別因此忽略其他命中）：

- 本檔上面那張表裡示範「私人路徑長什麼樣」的那一列 —— 用的是 `...`／`<私人專案>` 佔位符，不指涉任何真實專案。
- 作者署名 `Eric Lu (tingyulu)`、`github.com/tingyulu`（LICENSE、`plugin.json`、`marketplace.json`）與 repo 自身連結（README 安裝指令）。
- 第三方 MIT 致謝連結 `kieiken/ultracode-token-optimization`。

除這三類以外的命中，一律當成外洩處理到查清楚為止。本機另有完整的敏感詞清單，見 `.claude/local-rules.md`（該清單本身不進 repo）。

⚠️ **本檔（CLAUDE.md）也會被 commit 進公開 repo** —— 寫規範時同樣受本節約束，別把私人專案名寫進規則裡。

---

## 📐 鐵則 2：SKILL.md 的格式規範

### frontmatter

只有兩個欄位：`name`、`description`。**不加 `version`**（版號是整包的，見鐵則 4）。
唯一例外：衍生自第三方 MIT 專案的 skill 加 `license: MIT`（目前只有 `token-optimizer`）。

🔴 **`description` 的值一律用單引號包住** —— 這不是風格偏好，是 YAML 語法硬需求：

```yaml
description: '……時觸發。 English triggers: "a", "b".'
```

本 repo 的 description 必然含有 `English triggers: `（冒號＋空白）。在**未加引號的 plain scalar** 裡，`: ` 會被 YAML 當成 mapping 分隔符 → `mapping values are not allowed here` / `Psych::SyntaxError`，整支 skill 解析失敗。`pickup` 的 `status: pending` 也是同一個雷。

- 用**單引號**（值內部的 `"` 可原樣保留）；值裡若有 `'`（如 `don't`）改寫成 `''`。
- 別為了規避而把 `English triggers: ` 的冒號拿掉 —— 那個格式是鐵則 2 的一部分，該加引號的是整個值。

**改完必驗**（兩個獨立 parser，並且要有陽性對照確認 parser 真的在檢查）：

```bash
/usr/bin/python3 -c "
import yaml,glob,re
for f in sorted(glob.glob('skills/*/SKILL.md')):
    m=re.match(r'^---\n(.*?)\n---\n',open(f).read(),re.S)
    try: print('OK  ',f,list(yaml.safe_load(m.group(1)).keys()))
    except Exception as e: print('FAIL',f,type(e).__name__)
"
```

> 📌 **事故紀錄**：`v0.1.1`（2026-07-27 發布）的 5 支 SKILL.md **全部**是無效 YAML，`npx skills add` 0/5 成功——而且從發布日起公開 repo 一直是壞的，直到 2026-07-30 沙盒實測才發現。禍首正是 v0.1.1 引入的雙語觸發詞。肉眼看檔案、或確認「檔案存在、內容看起來對」都抓不到這種錯：**唯一有效的驗證是拿真的 YAML parser 去解**。

### description 的結構（順序固定）

```
<一句話定位> <具體做什麼／不做什麼>。當使用者說「A」「B」「C」時觸發。[補充句] ⚠️ <邊界提醒>。 English triggers: "a", "b", "c".
```

- 中文觸發詞用「」逐一包住、**相鄰排列不加逗號**，句尾接「時觸發。」（`flight-to-calendar` 用「時使用。」，語意需要時可換動詞）
- 英文觸發詞**固定收尾**，另起一句 `English triggers: `，雙引號包住、**逗號分隔**
- 中英**不是逐詞對譯** —— 中文是主體清單，英文是使用者實際會講的英文說法，各自獨立
- 觸發子句與 `English triggers:` 之間可插補充句，說明**產出**（`dropoff`：「產出＝…一張交接卡」）或**適用場合**（`pickup`：「也適合 session 開場主動跑一次」）
- ⚠️ 邊界提醒為選用，但凡是「做 X 但不做 Y」的 skill 都該寫（`save-all` 明寫「不重開機器」）

> 📌 **既有不一致，改到時順手收斂**：`token-optimizer` 的 description 在「」清單之外，另插了一段頓號分隔、沒有「」包住的「觸發詞：workflow、多代理、fan-out…」。新 skill 一律用「」包住，別複製這個寫法。

### 正文結構

1. `# <name> — <中文副標>`，`<name>` 逐字用 frontmatter 的 `name`（`/` 前綴用於斜線命令型 skill：`/save-all`、`/dropoff`、`/pickup`）
   - 既有例外：`token-optimizer` 的 H1 寫成 `# Token Optimizer`（Title Case）。要美化顯示名稱可以，**詞序不變**。
2. 一句話展開定位
3. `> 🤖 R2-D2 時刻：<Star Wars 類比>` —— 品牌彩蛋，放在說明之後、步驟之前。衍生作品改放 attribution blockquote（見 `token-optimizer`）
4. 主體：`## 為什麼需要` → `## 動作` / `## 步驟` → 規則濃縮
5. **必備一個規則濃縮區塊**（`## 鐵律` 或 `## 🚦 鐵則`）—— 把整份濃縮成幾條不可違反的規則，加粗關鍵詞＋emoji 前綴。位置有彈性，照現況三種都合法：
   - 收在最後（`save-all`）
   - 收在 `## 進階：接上你自己的任務系統` **之前**（`dropoff`、`pickup`）
   - 整支 skill 本質就是規則列表時，放在**開頭**（`flight-to-calendar`，文末是 `## 注意`）
   - 衍生自第三方、原文沒有這個區塊的，維持原狀不必硬加（`token-optimizer`，文末是 `## 8. 限制`）

### 行文慣例

- **標點**：敘事／文稿類內容用全形（，。、：「」（））；指令／log／程式導向的 skill（如日誌三支）可用半形。判準＝內容性質；單檔內部一致即可。既有檔不回溯改。反引號一律半形包指令
- 步驟**預設用 markdown 有序清單**（`1. 2. 3.`）掛在 `## 動作`／`## 步驟` 底下（`dropoff`、`pickup`、`flight-to-calendar` 都是）。只有步驟多到需要拆前置動作、或想讓每步能被單獨引用時，才升級成 H3 依序編號並從 `### 0.` 開始（目前有 `save-all`、`new-mission`）
- 子項目用圈碼 ①②③④⑤，後續步驟用同一組圈碼回頭對應，不重打項目名
- emoji 語意固定：⚠️ 風險／🚫 禁止／✅ 完成條件／🔁 重複性規則／📝 紀律／🔴 絕對規則／🔒 資料界線／安全／🤖 彩蛋
- **「驗證優先於宣告」是全 repo 的主題句**：凡是寫入動作，一律配上具體驗證指令（`wc -l`／`stat`／`grep`／`cat` 回讀）＋明講「別信工具回的『成功』字面」
- 零依賴優先：預設不綁任何外部服務；要接外部系統寫進「進階」節，並註明「檔案版是零依賴的最小公倍數，不是天花板」

---

## 🔁 鐵則 3：改一支 skill 的連動清單

skill 的行為／觸發詞／依賴一改，**同一個 commit 內**掃完下表。漏改會讓 README 描述與實際行為脫鉤：

🔑 **以「標題錨點」定位，不要只認行號** —— 行號會隨任何一次插入而整批推移（2026-07-30 加 skills.sh 安裝節就推移了四列）。下表行號是當日快照，對不上時以標題為準。

| 位置（錨點） | 行號快照 | 內容 |
|---|---|---|
| `README.md` 開頭定位句 | L11 | 「**12 支** skills」計數字串 |
| `README.md` skill 總表 | L13–26 | 一句話＋R2-D2 對應 |
| `README.md` `## 這套東西怎麼開發的` | L28–32 | 失憶引言＋自我修正敘事（二審缺陷數、測項數——測試計數一變這裡也要動） |
| `README.md` `## 相容性矩陣` | L34–61 | 五欄：CLI／Cowork／Gemini／Codex／ChatGPT，含 ✅\* 分級註、trusted-folder、日誌三支資料來源、門鈴註³、ai-review 後端註⁴、未實測範圍註記 |
| `README.md` `## 安裝` 末句「裝完打…」 | L104 | 逐一點名可觸發的斜線命令 |
| `README.md` `## 更新` | L106–114 | `npx skills update`／plugin 更新法／Watch Releases 通知 |
| `README.md` `## 一組 skill、兩種語言習慣` | L116–123 | **逐字引用各 skill description 的中英觸發詞例句**，改觸發詞必同步 |
| `README.md` `## 各 skill 的依賴` | L125–141 | 依賴表（日誌三支的相依關係、門鈴選用增強、ai-review 的後端需求在此宣告；ai-review 格含**測項數**——測試計數一變這裡也要動） |
| `README.md` `## Repo 結構` tree | L151–167 | skill 目錄名＋「12 支 skill」計數＋`prompts/`／`docs/`／`.github/` 列 |
| `README.en.md` | 同上各項 | 對應英文列（**兩檔行號目前完全對齊，各 185 行**，改完要複驗仍對齊） |
| `prompts/<skill>.md`＋`.en.md` | — | **免安裝簡版**（規則類 skill 適用，damage-report 首例）：skill 的五問／規則本體一改，簡版兩檔要同步改寫，別讓簡版變舊版 |
| `docs/TEST_PLAN.md` C 段快照 | 文末表格 | 相容性結論快照——README 矩陣評級一動，這裡要同步（反之亦然，見 TEST_PLAN CROSS-07） |
| `docs/TEST_PLAN.md` D 段 | C 段之前 | 交接門鈴測項 D-01～06——dropoff/pickup 的門鈴行為一改要同步 |
| `docs/TEST_PLAN.md` E 段＋`docs/AI_REVIEW_SOURCES.md` | D 段之後 | ai-review 測項 E-01～09 與**外部前提的查證原文**（方案涵蓋、安裝／登入指令）——腳本行為或引導文字一改要同步；查證超過兩週視為過期 |
| `.claude-plugin/plugin.json` | L3 | `description` 逐一點名各 skill |
| `.claude-plugin/marketplace.json` | L11 | `plugins[0].description` 同上 |
| `adapters/openai/README.md` 可移植性總表 | L12–17 | 四列對應四組 skill |
| `adapters/openai/README.md` Codex 安裝腳本 | L40 | 備援法 `for s in ...` 的 skill 清單 |

⚠️ `adapters/openai/README.md` 的矩陣是**獨立撰寫、非複製貼上**（用字與判斷邏輯都跟 README 不同）—— 要人工比對邏輯是否仍成立，不能只靠 diff 對照。

📌 `plugin.json` 的 `keywords` 是**代表性關鍵字，不是 skill 清單**（現況本來就沒窮舉 5 支），不必逐 skill 同步 —— 別誤判成遺漏。

新增／刪除 skill 時額外要改：兩份 README 的計數字串；若可移植到 Codex，`adapters/openai/README.md` L28 的迴圈清單要加名；若有硬依賴（如需 MCP connector）而搬不動，在 adapters 矩陣標 ❌ 並寫原因（比照 `flight-to-calendar` 先例）。

---

## 🏷️ 鐵則 4：版號與發布

- **版號單一權威來源＝`.claude-plugin/plugin.json` 的 `version`。** `marketplace.json` 不重複記版號、SKILL.md 不加 per-skill version。
- **無 CHANGELOG.md** —— release note 只活在 tag 訊息與 commit body 裡。要新增 changelog 是新慣例，不是延續現狀，先問過作者。
- **release 一天最多一個**：高頻迭代期間照常 commit／push，對外以彙總版發布，不逐 commit 打版。
- 發布流程：
  0. **CI 綠燈（見 `.github/workflows/ci.yml`）為必要條件**；REG-03 安裝煙霧與發現層抽驗仍人工。本地先跑 [docs/TEST_PLAN.md](docs/TEST_PLAN.md) A 段回歸，全綠才進下一步。這道關卡防的缺陷影響**所有** npx-skills 下游 agent（v0.1.1 事故對 gemini-cli／codex 同樣 0/5 全滅），不只 Claude Code——別再把影響半徑寫窄。
  1. bump `plugin.json` 的 `version`，**與該版所有內容改動放同一個 commit**（不要「先改內容、後補版號」）
  2. commit message＝`vX.Y.Z: <繁中一句話摘要>`，內文條列改了什麼
  3. 手動建 annotated tag：`git tag -a vX.Y.Z -m "<發布公告文字>"` —— tag 訊息是**獨立撰寫的公告，不抄 commit message**
  4. `git push origin main --tags`
  5. **發版收尾：同步部署副本**——若本機環境部署了 repo 內腳本的副本（排程在跑的、複製到其他目錄的），以 repo 為真相重新同步並**以雜湊比對回讀驗證**，別信 `cp` 的沉默成功。部署清單與一鍵檢查屬本機環境，記在 `.claude/local-rules.md`（不進 repo）。此步驟的由來：v0.6.0 修了收割器、四份部署副本全忘了同步，排程照跑舊版無人發現。
- CI＝GitHub Actions（2026-08 起）；Release 仍手動。**每版建 GitHub Release**（`gh release create vX.Y.Z`，內文＝雙語更新留言；v0.1.1 起三版皆有，2026-08-07 定為慣例）。

🔴 **commit message 與 tag 訊息也要過鐵則 1** —— 它們是本 repo 唯一的 release note 載體，會原樣推上公開 GitHub，但**不在守門 grep 的掃描範圍內**（那條指令掃的是檔案樹，讀不到還沒執行的 `git commit -m`／`git tag -a -m` 參數）。動筆寫這兩處前，自己照去識別化表過一遍，別指望指令幫你擋。

> 誠實註記：上述流程是從 `v0.1.1` 這**唯一一次**實跑反推的（`v0.1.0` 從未打 tag，22 分鐘後就被 v0.1.1 的重構取代）。只有一個資料點，不是跑過多次穩定下來的規則。

---

## ⚠️ 鐵則 5：repo 版與本機安裝版，永遠不反向覆蓋

作者本機 `~/.claude/skills/` 下有與本 repo **同名**的私人客製版（`pickup`／`save-all`／`token-optimizer` 三支），底層接的是私人任務系統，與 repo 的零依賴檔案版**機制完全不相容**。（`dropoff` 於 2026-07-30 由 `handoff` 改名後已不再同名。）

- 🚫 **絕不**在作者本機把 repo 的 skill 複製進 `~/.claude/skills/` —— 會靜默覆蓋那三支，且被覆蓋後其他本機文件對它們的引用會悄悄失效。README 的手動安裝指令已改用 `cp -rn`（不覆蓋既有檔）。
- ✅ 同步方向**只有一個**：本機版驗證過的改進 → 泛化去識別化 → 回饋進 repo。反向不做。

### 要 dogfood repo 版時，走 plugin 路徑

```
/plugin marketplace add tingyulu/MyR2D2
/plugin install myr2d2@myr2d2
```

plugin skill 用 `plugin-name:skill-name` 命名空間（`myr2d2:pickup`…），**結構上不可能與其他層級衝突**。本機私人版原封不動、照常運作，兩套並存各走各的。

🔴 **不要改用「專案層安裝」當隔離手段** —— 官方文件明訂 `enterprise > personal > project`，**personal 蓋過 project**。把 repo 版放進某專案的 `.claude/skills/` 之後，同名的本機私人版**仍然勝出**：檔案沒被覆蓋（安全），但你以為在測 repo 版、實際跑的是私人版，得到一個不會報錯的錯誤結果。

---

## 🚦 鐵則總表

- 🔒 **公開優先於方便** —— 任何一行進 repo 前先過去識別化表；`CLAUDE.md` 自己也算。
- 📐 **格式跟既有 skill 對齊** —— frontmatter 兩欄、雙語觸發詞收尾、全形標點、必備鐵律／鐵則規則區塊。
- 🔁 **改 skill 必掃連動表**，同一 commit 改完。
- 🏷️ **版號只有一處**，發布靠手動 annotated tag。
- 🚫 **不反向覆蓋本機客製版**。
- ✅ **驗證優先於宣告** —— 這是 repo 的主題，寫規範時自己也遵守：改完回讀驗證，別信「成功」字面。
