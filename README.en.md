# MyR2D2 🤖

### Your everyday astromech droid — a Claude skillset (zh-TW body, bilingual triggers)

**[繁體中文 (primary)](README.md) | English (this page)**

---

R2-D2 was never the protagonist, but every episode runs on him: smuggling out the Death Star plans, rolling across a desert to find Obi-Wan, quietly fixing the ship and managing power from the back of an X-wing.

That's MyR2D2's job description — 12 skills covering things that "won't kill you if skipped, but keep the whole workflow alive when done":

| Skill | One-liner | R2-D2 parallel |
|---|---|---|
| **save-all** | Before wrap-up/reboot: land everything that lives only in the conversation, and **verify** it hit disk | Plans stored in R2, escape pod away |
| **dropoff** | Write a task + full context into a handoff card for another session; rings the doorbell if the target session is live | Leia recording "Help me, Obi-Wan" |
| **pickup** | New session fetches the cards (or gets doorbell-woken), reads in full, claims, starts, reports back when done | R2 finds Obi-Wan, plays the hologram |
| **mission-log** | Zero-token harvest of any day's session activity (the transcripts were always recording — you just need a reader) | The flight recorder never sleeps |
| **daily-debrief** | Daily wrap-up: what happened + reflection, landed before transcripts evaporate (30-day retention) | The post-mission debrief |
| **weekly-debrief** | Weekly wrap-up: 7 dailies condensed into storylines and trends | Campaigns reveal supply-line problems; single sorties don't |
| **new-mission** | Kickoff brief: look first, ask at most five questions, draft the plan, review it, wait for an explicit "go" — and mint a reusable task prompt on the way | R2 projects the Death Star plans; the squadron flies the trench only after the briefing |
| **damage-report** | Five wrap-up questions run against the original ask before you report; the suggestions field says "none" when there's nothing real | Ship repaired, R2 runs its own diagnostics and beeps the damage report — without waiting for Luke to ask |
| **ai-review** | Send the work to **another model** for a second opinion, digest it, then write the report; says "self-review only" when no backend is there | R2 and C-3PO bicker for six films — each covering the other's blind half |
| **ai-search** | Ask once, get a **cited, checkable** live answer; says "not found" instead of filling from stale training data | R2 jacks into an Imperial terminal — reading live station data, not stale intel from memory |
| **token-optimizer** | Iron rules before multi-agent dispatch: model tiering, compressed reporting, stop after 3 failures | Power allocation — don't let shields drain the engines |
| **flight-to-calendar** | Booked flights → Google Calendar: timezone-correct, one leg per event, sunset seats | Navigation — the astromech's actual day job |

## How this pack gets built

Claude sessions are **amnesiac**: close the conversation and everything not written to disk evaporates. The common theme here is fighting that amnesia — every skill in the table covers one link of the amnesia chain. All of it was iterated out of real daily-driver usage, not theory.

The development process eats the same rules: **before every release, the work goes to a different model family for review** (asking the same model to "check again" mostly re-confirms what it already believed). `ai-review` itself was built this way — three rounds of cross-model review caught 21 defects, 13 of which were introduced by the previous round's own fixes, and 41 regression tests ship in the box. All of it is checkable: method and evidence in [docs/TEST_PLAN.md](docs/TEST_PLAN.md), per-version fixes in [Releases](https://github.com/tingyulu/MyR2D2/releases).

## Compatibility matrix

Start here — check which skills your tool can run:

| Skill | Claude Code CLI | Cowork / claude.ai | Gemini CLI | Codex CLI | ChatGPT (manual paste only) |
|---|---|---|---|---|---|
| save-all | ✅ | ✅ (token-count step auto-skips) | ✅\* (same) | ✅\* (drop the token-count step) | ⚠️ checklist only |
| dropoff / pickup³ | ✅ | ✅ | ✅\* | ✅\* | ❌ (no shared disk) |
| mission-log / daily-debrief / weekly-debrief | ✅ | ❌ (no local transcripts) | ❌² | ❌² | ❌² |
| new-mission | ✅ | ✅ (rules-only, zero tool deps) | ✅ (rules-only) | ✅ (rules-only) | ⚠️ paste as a kickoff protocol |
| damage-report | ✅ | ✅ (rules-only, zero tool deps) | ✅ (rules-only) | ✅ (rules-only) | ⚠️ paste as a wrap-up checklist |
| ai-review | ✅ (needs a review backend⁴) | ⚠️ rules work; the script needs a shell | ⚠️ same | ⚠️ same | ⚠️ use prompts/ in another AI |
| ai-search | ✅ (needs a search backend⁵) | ⚠️ rules work; the script needs a shell | ⚠️ same | ⚠️ same | ⚠️ use prompts/ with built-in browsing |
| token-optimizer | ✅ | ✅ (rules-only, no tool deps) | ⚠️ principles port¹ | ⚠️ principles port¹ | ⚠️ principles port¹ |
| flight-to-calendar | ✅ (needs Calendar connector) | ✅ (needs Calendar connector) | ⚠️ bring your own Calendar MCP (untested) | ❌ no Calendar tool | ⚠️ needs an Action |

\* = install/discovery layers verified; execution layer is rules-based inference (see [docs/TEST_PLAN.md](docs/TEST_PLAN.md) CROSS-05).
¹ The five iron rules port; swap model names for your vendor's tiers. §1's "advanced backstop" (settings.json / env) only works in Claude Code — skip it elsewhere.
² The journal trio reads **Claude Code's own transcripts** (`~/.claude/projects/`) — the skill format installs elsewhere, but the data isn't there, hence ❌.
³ The "instant doorbell" (messaging the target session right after a dropoff) is an optional enhancement that needs Claude Code v2.1.224+ cross-session messaging (officially macOS/Linux; messages to bypass-permissions sessions are held for manual approval); other tools skip it automatically — file-based handoff is unaffected.
⁴ `ai-review` needs a review backend (Codex CLI by default, swappable via `AI_REVIEW_CMD`) plus a POSIX shell. No backend or not signed in → `skipped_*` and it still **exits 0**, so it never breaks your flow (automation should parse the final `AI_REVIEW_STATUS:` line on stdout to tell "skipped" from "reviewed"); quota/network failures exit 2 by default, and `--soft-fail` makes those exit 0 too. It deliberately pins no model (pinned names go stale); if the backend's default model is outside your plan, pass `--model`. Verified on macOS under `sh`/`dash`/`bash`/`ksh`/`zsh`, and on Linux via CI (ubuntu-latest) on every push; **Windows and free-tier accounts remain untested**.
⁵ `ai-search` shares ai-review's architecture (single-file POSIX shell, status on stdout, exit codes only mark real failures, and it ships its own 43-item behavior matrix that runs in CI on every push); the difference is it needs a backend that **actually searches the web** (Codex CLI's built-in `web_search` by default, swappable via `AI_SEARCH_CMD` — but the replacement must also search; a plain LLM just fills from stale knowledge). No backend or not signed in → `skipped_*` and exit 0 — automation that only checks exit codes reads "skipped" as success; parse the final `AI_SEARCH_STATUS:` line on stdout to tell them apart. ChatGPT web has its own browsing — use the lite prompt in `prompts/ai-search.md`; the script isn't needed there.

- **Gemini CLI / Codex CLI**: install & discovery layers verified — including Gemini's trusted-folder gate (if skills don't show up, trust the project folder first); execution layer untested.
- **ChatGPT**: no CLI / no filesystem — manual paste is the only path (see adapters).
- Other `npx skills` targets (Cursor, Copilot, …): untested.

Porting guide for ChatGPT / Codex (preferred `npx skills` path, AGENTS.md fallback, three gotchas): **[adapters/openai/](adapters/openai/README.md)**.

## Install

**Pick your lane**: CLI user → npx or Plugin | want manual control → manual copy | chat-only → no-install lite prompts | claude.ai Cowork → last section.

### skills.sh (`npx skills`) — recommended, one command

[![skills.sh](https://skills.sh/b/tingyulu/MyR2D2)](https://skills.sh/tingyulu/MyR2D2) [![CI](https://github.com/tingyulu/MyR2D2/actions/workflows/ci.yml/badge.svg)](https://github.com/tingyulu/MyR2D2/actions/workflows/ci.yml)
(The skills.sh badge counts cumulative installs, not the number of skills.)

```bash
npx skills add tingyulu/MyR2D2
```

[`npx skills`](https://github.com/vercel-labs/skills) supports Claude Code and many other agents (`gemini-cli`, `codex`, `cursor`, … — full list in the upstream README). This repo has verified the install layer for gemini-cli / codex (method & evidence in [docs/TEST_PLAN.md](docs/TEST_PLAN.md)); other targets are untested. It installs to the **project scope** `./.claude/skills/` by default; add `-g` for a global install. Use `--skill` to pick individual skills.

### Claude Code CLI — Plugin (deep integration)

```
/plugin marketplace add tingyulu/MyR2D2
/plugin install myr2d2@myr2d2
```

Skills land under the `myr2d2:` namespace (`/myr2d2:dropoff`, …) — structurally conflict-free with any same-name skills you already have, and centrally updatable via the marketplace.

### Claude Code CLI — manual copy

```bash
git clone https://github.com/tingyulu/MyR2D2.git
cp -rn MyR2D2/skills/* ~/.claude/skills/
```

⚠️ Note the `-n` (no-clobber): if `~/.claude/skills/` already has folders with these names, plain `cp -r` **overwrites them silently**. Diff first if you're updating an existing install.

### Chat-only? No-install lite prompts

No CLI, nothing to install: [prompts/](prompts/) has paste-ready lite versions — `new-mission` ([zh-TW](prompts/new-mission.md) | [EN](prompts/new-mission.en.md) — paste into persistent instructions so it **asks, plans, and waits for your go before acting**), `damage-report` ([zh-TW](prompts/damage-report.md) | [EN](prompts/damage-report.en.md); a 1,260-char [minimal version](prompts/damage-report.lite.en.md) fits narrow fields like ChatGPT Free), `ai-review` ([zh-TW](prompts/ai-review.md) | [EN](prompts/ai-review.en.md) — paste into **another** AI for a cross-model review) and `ai-search` ([zh-TW](prompts/ai-search.md) | [EN](prompts/ai-search.en.md) — paste into an AI **with browsing** for cited, real-time verification).

### Cowork / claude.ai

First get the repo via the manual-copy `git clone` (or **Code → Download ZIP** on the GitHub page), then add the skill folders you want (`skills/<name>/`) to your Cowork project skills (or the project's `.claude/skills/`).

Then trigger with `/save-all`, `/dropoff`, `/pickup`, `/daily-debrief`, `/new-mission`, `/damage-report`, `/ai-review`, `/ai-search`, etc., or natural language in either language.

## Updating

Installed skills are point-in-time snapshots — **new releases won't notify you**. To update:

```bash
npx skills update
```

One command updates every installed skill (sources are recorded in the install-time lock file; `-g`/`-p` scopes to global/project). For plugin installs, update the marketplace via the `/plugin` UI. To get notified on new releases: **Watch → Custom → Releases** on this repo.

## One skill set, two language habits

The skill bodies are written in Traditional Chinese (single source of truth — no parallel translations to maintain). **Trigger phrases come in matched zh/en pairs** in each skill's description:

- 中文習慣:「要重開機了」「交接給 X」「有沒有交接給我的」
- English habit: "about to reboot", "hand this off to X", "anything handed off to me?"

Claude follows the zh-TW instructions and replies in whatever language you speak — English users lose nothing, and there's only ever one copy of each skill to maintain.

## Per-skill dependencies

| Skill | Dependencies |
|---|---|
| save-all | None (the token-count step is Claude Code CLI-only and optional) |
| dropoff / pickup | None — cards are Markdown files under the project's `.claude/handoffs/`; the instant doorbell is an optional enhancement (Claude Code v2.1.224+), auto-skipped where unavailable |
| mission-log | None — the harvester is a stdlib-only python3 script, zero tokens. Ships tests (`tests/harvest_test.py`, synthetic fixtures — never reads your real data) |
| daily-debrief | **Requires mission-log** (the harvester lives there) |
| weekly-debrief | **Requires daily-debrief and mission-log** (missing dailies are auto-backfilled) |
| new-mission | None (pure rules; the `ai-review` hookup in step 3's advanced section is an optional cross-reference) |
| damage-report | None (pure rules; the `/dropoff` mention in Q5 and the `ai-review` upgrade section are optional cross-references) |
| ai-review | **A review backend** (Codex CLI by default; `AI_REVIEW_CMD` swaps in any command that reads stdin and writes stdout) plus a POSIX shell. No extra packages: no npm module, no brew formula, no API key of your own. Ships 41 regression tests (`tests/matrix.sh`, no quota burned) |
| ai-search | **A web-searching backend** (Codex CLI's built-in `web_search` by default; `AI_SEARCH_CMD` swaps it, but the replacement must also search) plus a POSIX shell. No extra packages. Ships 43 regression tests (`tests/matrix.sh`, no quota burned, no network) |
| token-optimizer | None (rules-only; Workflow-specific items need the Workflow tool — Workflow is Claude Code's multi-agent orchestration feature; §1's advanced backstop is Claude Code CLI-only) |
| flight-to-calendar | **Google Calendar MCP connector** (hard dependency) |

dropoff/pickup default to the zero-dependency file-based version; if you run your own task system (CLI todo, Notion, Linear…), each SKILL.md includes a "plug in your own task system" section.

## Design principles

1. **Verification over declaration** — writes get read back, completion needs evidence, literal success messages are not trusted.
2. **Self-contained context** — handoff cards assume the reader knows nothing.
3. **Zero-dependency lowest common denominator** — file-based by default, external systems are the upgrade path.
4. **Quota is a shared resource** — thrift is the default for multi-agent dispatch; full power is an explicit switch.
5. **Single source of truth** — one copy per skill (zh-TW); language habits are handled by paired bilingual triggers, not parallel translations.

## Repo layout

```
MyR2D2/
├── .claude-plugin/                    ← plugin.json + marketplace.json (single plugin)
├── .github/workflows/                 ← CI (YAML validation, content gate, behavior matrix, harvest tests)
├── skills/                            ← 12 skills (zh-TW body, bilingual triggers)
│   ├── save-all/  ├── dropoff/  ├── pickup/
│   ├── mission-log/  ├── daily-debrief/  ├── weekly-debrief/
│   ├── new-mission/  ├── damage-report/  ├── ai-review/
│   ├── ai-search/  ├── token-optimizer/  └── flight-to-calendar/
├── prompts/                           ← no-install lite prompts (paste into any chat)
├── docs/                              ← test plan + verification notes for external claims
├── adapters/openai/                   ← ChatGPT / Codex porting kit
├── README.md                          ← zh-TW (primary)
└── README.en.md                       ← this page
```

## Attribution

- `token-optimizer` is adapted from [kieiken/ultracode-token-optimization](https://github.com/kieiken/ultracode-token-optimization) (MIT), generalized for all-Claude environments.

## Author

Eric Lu ("Uncle Eric") — 30 years in product, now a product consultant, headhunter, and career coach. These skills are my actual daily workflow; once a pain point got solved, open-sourcing it was the easy part.

- Long-form writing: [uncleric.com](https://www.uncleric.com) (zh-TW)
- Find me: [Threads @tingyulu](https://www.threads.com/@tingyulu) | [LinkedIn](https://www.linkedin.com/in/uncleeric/)
- Weekly jobs newsletter: [大叔的人生相談室](https://www.linkedin.com/newsletters/7484182774178803713/) (zh-TW)

## License

MIT — see [LICENSE](LICENSE).

*MyR2D2 is fan-tribute naming, unaffiliated with Lucasfilm / Disney; R2-D2 and Star Wars are trademarks of their respective owners.*
