# MyR2D2 → ChatGPT / Codex 移植包
# MyR2D2 → ChatGPT / Codex adapter

*中文為主,English below per section.*

SKILL.md 本體就是純 Markdown 指令,任何能讀指令的 LLM 都吃得下。差別在「觸發機制」與「工具依賴」。本移植包告訴你哪些能直接搬、哪些要改、哪些搬不動。

The SKILL.md files are plain Markdown instructions — any instruction-following LLM can consume them. What differs is the *trigger mechanism* and *tool dependencies*. This adapter tells you what ports as-is, what needs edits, and what doesn't port.

## 可移植性總表 | Portability matrix

| Skill | Codex CLI | ChatGPT | 說明 Notes |
|---|---|---|---|
| save-all | ✅ | ⚠️ | Codex:git/檔案操作全通,token 統計那步刪掉(那是讀 Claude Code transcript 的)。ChatGPT:無本機檔案系統,只能當「收工檢查清單」用。<br>Codex: git/file ops all work; delete the token-count step (it reads Claude Code transcripts). ChatGPT: no local filesystem — usable only as a wrap-up checklist. |
| dropoff / pickup | ✅ | ❌ | 交接卡=磁碟上的 Markdown 檔,Codex CLI 完全可用。「即時門鈴」步驟(v0.4.0 起)是 Claude Code 專屬的 cross-session messaging,Codex 端自動跳過、純檔案交接不受影響。ChatGPT 沒有跨 session 共用磁碟,搬不動。<br>Cards are Markdown files on disk — fully portable to Codex CLI. The "instant doorbell" step (v0.4.0+) uses Claude Code-only cross-session messaging and auto-skips on Codex; file-based handoff is unaffected. ChatGPT has no cross-session shared disk. |
| token-optimizer | ⚠️ 原則通用 | ⚠️ 原則通用 | 五鐵則(分層/壓縮上報/角色鎖死/獨立驗證/失敗三停)通用;§1 模型名換成你家的檔位(如 o4-mini vs o3)。⚠️ Workflow 專屬語彙**不只 §6**——`agent()`/`schema`/`budget.remaining()`/`label` 散布在 §1 絕對規則、§2、§5、§6、§7 自檢、§8:移植時把這些讀成「你的多代理派工機制」的代稱、規則要意譯(例:「每次派工都明確指定模型」),別照抄語法。§1「進階兜底」段(settings.json/env)是 Claude Code 專屬,跳過。原作 kieiken/ultracode-token-optimization 就是 Codex 環境寫的,等於「移植回老家」。<br>The five iron rules are universal; swap §1 model names for your vendor's tiers. ⚠️ Workflow-specific syntax is **not limited to §6** — `agent()`/`schema`/`budget.remaining()`/`label` appear in §1's red rule, §2, §5, §6, the §7 checklist, and §8: read them as stand-ins for *your* multi-agent dispatch mechanism and port the rules by meaning ("always pin the model when dispatching"), not by syntax. Skip §1's advanced-backstop paragraph (settings.json/env — Claude Code only). Fun fact: the upstream (kieiken) was written FOR Codex — porting it back is going home. |
| mission-log / daily-debrief / weekly-debrief | ❌ | ❌ | 資料來源是 Claude Code 自家 transcript(~/.claude/projects),其他工具沒有這份資料——skill 指令可讀,但無料可收。想移植得把收割器改讀你家 agent 的 session 檔(如 Codex 的 ~/.codex/sessions)。<br>Data source is Claude Code's own transcripts — other tools don't have it. Porting means pointing the harvester at your agent's session files (e.g. Codex's ~/.codex/sessions). |
| new-mission | ✅ | ✅ | 純規則零依賴,「先查再問(最多五題)+計畫骨架+等明確的『做』才動手+收尾報告五格」原封搬;進階節的 ai-review 送審換成你自己的審稿工具即可。**ChatGPT 免動手:`prompts/new-mission.md` 有現成簡版,貼進 custom instructions 即用。**<br>Pure rules, zero deps — "look first, ask at most five, plan skeleton, wait for an explicit go, close with a five-cell wrap-up report" ports verbatim; swap the advanced section's ai-review hookup for your own reviewer. ChatGPT: paste the ready-made `prompts/new-mission.en.md` into custom instructions. |
| damage-report | ✅ | ✅ | 純規則零依賴,五問+「寫無」原封搬;第 5 問的 /dropoff 引用換成你的交接慣例即可。最適合直接併進 AGENTS.md 常駐。**ChatGPT 免動手:`prompts/damage-report.md` 有現成簡版,整段貼進 custom instructions 即用。**<br>Pure rules, zero deps — the five questions and the "write none" rule port verbatim; swap the /dropoff mention in Q5 for your own handoff habit. Ideal for merging straight into AGENTS.md. |
| ai-review | ✅ | ⚠️ | Codex CLI 環境**本來就有後端**,腳本(單檔 POSIX shell)直接可跑;不想用 codex 就設 `AI_REVIEW_CMD` 換後端。ChatGPT 網頁版沒有 shell:改用 `prompts/ai-review.md`,把它貼進**另一個** AI(不是寫這份東西的那個)就是二審。<br>Codex environments already have the backend — the single-file POSIX shell script runs as-is; set `AI_REVIEW_CMD` to swap it. ChatGPT web has no shell: use `prompts/ai-review.md` and paste it into *another* AI (not the one that wrote the work). |
| ai-search | ✅ | ⚠️ | Codex CLI 本來就有後端,腳本(單檔 POSIX shell)直接可跑——它要的是**會上網搜尋**的後端,codex 內建 `web_search` 剛好是;換後端(`AI_SEARCH_CMD`)也得挑一個會搜尋的,純 LLM 只會拿舊知識填答。ChatGPT 網頁版沒有 shell,但**本身有 browsing**:把 SKILL.md 那幾條「結論先行、每個事實附來源、查不到就說查不到」的要求直接說給它即可,毋須本腳本。<br>Codex already has the backend — the single-file POSIX shell script runs as-is; what it needs is a backend that *searches the web*, and codex's built-in `web_search` is exactly that. A swapped backend (`AI_SEARCH_CMD`) must also search — a plain LLM just fills from stale knowledge. ChatGPT web has no shell but *does* have browsing: give it the SKILL.md rules (conclusion first, cite every fact, say when not found) — no script needed. |
| flight-to-calendar | ❌ | ⚠️ | 硬依賴 Google Calendar 寫入工具。Codex CLI 無;ChatGPT 需自備 Calendar 的 Action/connector 才能用,規則本身(時區換算/一段一事件/夕陽座位)全通用。<br>Hard dependency on a Google Calendar write tool. Codex CLI: none. ChatGPT: needs a Calendar Action/connector; the rules themselves (timezone math, one-leg-one-event, sunset seats) are universal. |

## Codex CLI 安裝法 | Codex CLI setup

**首選:新版 Codex CLI 已有原生 skill 機制**(0.145 實測,2026-07;內建 `~/.codex/skills/.system/`),直接裝:

**Preferred: recent Codex CLI has native skill support** (verified on 0.145, 2026-07; ships `~/.codex/skills/.system/`) — just install:

```bash
npx skills add tingyulu/MyR2D2 --agent codex
```

裝進 `.agents/skills/` 後,skill 的 name+description 會注入 Codex 的 model prompt(用 `codex debug prompt-input "test"` 可驗證),觸發詞自動路由,毋須手動搬運。

Installed into `.agents/skills/`, each skill's name+description is injected into Codex's model-visible prompt (verify with `codex debug prompt-input "test"`) — triggers auto-route, no manual copying.

**備援**(舊版 Codex、或無原生 skill 機制的環境):Codex 讀 `AGENTS.md`(專案根目錄或 `~/.codex/AGENTS.md`),把規則直接併進去:

**Fallback** (older Codex, or environments without native skills): Codex reads `AGENTS.md` (project root or `~/.codex/AGENTS.md`) — merge the rules in directly:

```bash
# 把要用的 skill 內文(去掉 YAML frontmatter)接進 AGENTS.md
# Append the skill bodies (minus YAML frontmatter) into AGENTS.md
for s in save-all dropoff pickup new-mission damage-report ai-review ai-search token-optimizer; do
  echo -e "\n\n<!-- MyR2D2: $s -->" >> AGENTS.md
  sed '1,/^---$/d' ../../skills/$s/SKILL.md | sed '1,/^---$/d' >> AGENTS.md
done
```

或更省 context 的做法:AGENTS.md 只放一句路由 ——

Or the context-cheaper route — AGENTS.md carries one routing line:

```markdown
When the user says "save-all" / "dropoff" / "pickup", read and follow
docs/myr2d2/<name>.md before acting.
```

再把 skill 檔複製到 `docs/myr2d2/`。/ …and copy the skill files into `docs/myr2d2/`.

## 移植時的三個坑 | Three porting gotchas

1. **觸發詞不會自己生效(備援法/ChatGPT 適用)** — Claude Code 與新版 Codex 靠 description 自動路由;ChatGPT 或走 AGENTS.md 備援法時,要嘛使用者手動說「照 save-all 流程走」,要嘛靠上面那句路由指令。<br>*Triggers don't fire by themselves (fallback path / ChatGPT) — Claude Code and recent Codex auto-route on descriptions; on ChatGPT or the AGENTS.md fallback, the user invokes by name or you add the routing line.*
2. **「驗證落地」規則照搬** — 寫入靜默失敗不是 Claude 特有的,任何 agent 環境都該回讀驗證。這是全包最值得帶走的一條。<br>*Keep the "verify the write" rule — silent write failures aren't Claude-specific. It's the single most portable rule in this pack.*
3. **R2-D2 註解可刪** — 那是給人看的調味,不影響行為。<br>*The R2-D2 asides are seasoning for humans; deleting them changes nothing.*
