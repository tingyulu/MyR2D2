# MyR2D2 Cheat Sheet

All 12 skills on one page: when to use each one, and what to say to trigger it. For install and compatibility, see the [README](../README.en.md). Triggers are a sample; the full lists live in each `skills/*/SKILL.md` description.

## Wrap-up & Handoff

| Skill | When to use | English | 中文 |
|---|---|---|---|
| `/save-all` | Before a reboot or at the end of the day: save what only lives in the chat, and verify it landed | "save-all", "about to reboot", "wrapping up for today" | 「save-all」「要重開機了」「關機前收尾」 |
| `/dropoff` | Hand a task and its full context to another project or a future session | "hand this off to X", "pass this to the next session" | 「交接給 X」「推球給 X」 |
| `/pickup` | Read handoff cards and take over; run it once at session start so nothing slips | "pickup", "anything handed off to me?" | 「接手」「pickup」「看交接」 |

## Work Journal

| Skill | When to use | English | 中文 |
|---|---|---|---|
| `/mission-log` | Zero-token harvest of a day's session activity (read-only) | "what did I work on today", "mission log" | 「今天做了什麼」「mission log」 |
| `/daily-debrief` | Turn that harvest into a daily report (needs mission-log) | "daily debrief", "write up my day" | 「日報」「daily debrief」 |
| `/weekly-debrief` | Roll 7 dailies into a weekly report (needs daily-debrief + mission-log) | "weekly debrief", "wrap up my week" | 「週報」「這週做了什麼」 |

## Kickoff & Quality

| Skill | When to use | English | 中文 |
|---|---|---|---|
| `/new-mission` | A new task with 3+ steps, or hard to undo: ask → plan → act only on your go → final report | "mission brief", "plan before doing" | 「新任務」「開工簡報」「先問我再做」 |
| `/damage-report` | Five self-review questions before reporting back on dev or research work | "damage report", "self-review" | 「收尾自檢」「跑五問」 |
| `/ai-review` | Get a second opinion from another model, then digest it before reporting | "second opinion", "cross-model review" | 「送二審」「跨模型 review」 |
| `/ai-search` | Live web answers with citations; says so when nothing is found | "fact-check this", "what is the latest" | 「查證」「上網查一下」「這是不是真的」 |

## Budget & Life

| Skill | When to use | English | 中文 |
|---|---|---|---|
| `token-optimizer` | Budget rules to read before dispatching Agent / Workflow jobs (auto-triggers) | "save tokens", "don't burn my limit" | 「省 token」「配額」 |
| `flight-to-calendar` | Add booked flights to Google Calendar with correct time zones; on dusk and dawn legs, flags the sunset or sunrise seat side | "add my flights to the calendar" | 「把航班加到行事曆」 |

> 🤖 Same content as a 4:5 card: [cheatsheet.en.png](cheatsheet.en.png) (save it to your phone's photos). The card is rendered from [cheatsheet.en.html](cheatsheet.en.html); the render command is in a comment at the top of that file. 中文版：[cheatsheet.md](cheatsheet.md).
