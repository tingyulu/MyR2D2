# MyR2D2 速查小抄（Cheatsheet）

一張表找齊 12 支 skill 的「什麼時候用、怎麼喊」。安裝與相容性見 [README](../README.md)；觸發詞為節選，完整清單在各 `skills/*/SKILL.md` 的 description。

## 收工與交接

| Skill | 什麼時候用 | 中文觸發 | English |
|---|---|---|---|
| `/save-all` | 重開機／收工前，把只活在對話裡的東西落地並驗證 | 「save-all」「要重開機了」「關機前收尾」 | "save-all", "wrapping up for today" |
| `/dropoff` | 把一件事連同脈絡交給另一個專案／未來 session（推球） | 「交接給 X」「推球給 X」 | "hand this off to X" |
| `/pickup` | 讀交接卡並接手（接球）；session 開場跑一次防漏接 | 「接手」「pickup」「看交接」 | "pickup", "anything handed off to me?" |

## 工作日誌

| Skill | 什麼時候用 | 中文觸發 | English |
|---|---|---|---|
| `/mission-log` | 零 token 收割某天的 session 活動骨架（只讀不寫） | 「今天做了什麼」「mission log」 | "what did I work on today" |
| `/daily-debrief` | 由骨架產某天日報（需 mission-log） | 「日報」「daily debrief」 | "daily summary", "write up my day" |
| `/weekly-debrief` | 彙整 7 份日報成週報（需 daily-debrief＋mission-log） | 「週報」「這週做了什麼」 | "weekly debrief", "wrap up my week" |

## 開工與收尾品質

| Skill | 什麼時候用 | 中文觸發 | English |
|---|---|---|---|
| `/new-mission` | 三步以上或做錯難回頭的新任務：先問→計畫→點頭才動手→收尾報告 | 「新任務」「開工簡報」「先問我再做」 | "mission brief", "plan before doing" |
| `/damage-report` | 開發／研究收尾寫回報前的五問自檢 | 「收尾自檢」「跑五問」 | "damage report", "self-review" |
| `/ai-review` | 產出送另一個模型二審，消化意見再回報 | 「送二審」「跨模型 review」 | "second opinion", "cross-model review" |
| `/ai-search` | 帶引用的即時網路查證，查不到就說查不到 | 「查證」「上網查一下」「這是不是真的」 | "fact-check this", "what is the latest" |

## 節流與生活

| Skill | 什麼時候用 | 中文觸發 | English |
|---|---|---|---|
| `token-optimizer` | 呼叫 Agent／Workflow 派工前必讀的節流規則（自動觸發型） | 「省 token」「配額」 | "save tokens", "don't burn my limit" |
| `flight-to-calendar` | 把已訂航班加進 Google Calendar，跨時區正確、傍晚／清晨航段標日落日出座位 | 「把航班加到行事曆」 | "add my flights to the calendar" |

> 🤖 同內容的 4:5 圖卡：[cheatsheet.png](cheatsheet.png)（存進手機相簿隨時翻）。
