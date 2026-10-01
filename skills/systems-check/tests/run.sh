#!/bin/sh
# systems-check 回歸測試（零依賴、不讀真資料）：
#   1. 掃描器內建 --selftest 全過
#   2. 乾淨專案 --dry-run：rc 0、零候選
#   3. 種了互相矛盾規則的專案：兩條規則都被抽出、落在同一個主題桶（衝突判定是 agent 讀桶做的事，
#      掃描器只負責把它們放到同一張桌上；桶檔 rules_by_topic/notify.md 要同時含兩條）
# 用法：sh skills/systems-check/tests/run.sh   （PYTHON 環境變數可換直譯器）
# 三案都把 SELFCHECK_HOME 指向臨時目錄，絕不碰真的 ~/.claude。
set -u
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # CI 常是 C／POSIX locale，別讓中文輸出炸在編碼上
HERE=$(cd "$(dirname "$0")" && pwd -P)
SCAN="$HERE/../scripts/selfcheck_scan.py"
PY=${PYTHON:-python3}
TMP=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/systems-check-test.XXXXXX")" && pwd -P)
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL $*"; exit 1; }
pass() { echo "PASS $*"; }

echo "== 1. selftest =="
"$PY" "$SCAN" --selftest > "$TMP/selftest.log" 2>&1 || { grep -v "^PASS" "$TMP/selftest.log" | tail -20; fail "selftest 退出碼非 0（上面是非 PASS 的行；紅的案在 FAIL 行）"; }
set -- $(tail -1 "$TMP/selftest.log")
[ "${1:-}" = "SELFTEST" ] || fail "selftest 末行不是 SELFTEST 摘要：$*"
case "${2:-}" in */*) n=${2%/*}; t=${2#*/};; *) fail "selftest 摘要格式：$2";; esac
[ "$n" = "$t" ] && [ "$t" -gt 0 ] || fail "selftest $2"
pass "selftest $2"

HOME_T="$TMP/home"
mkdir -p "$HOME_T/.claude/projects" "$HOME_T/Library/LaunchAgents"

echo "== 2. 乾淨專案：零候選 =="
PROJ="$HOME_T/proj-clean"
mkdir -p "$PROJ/.claude"   # 專案根的判定靠 CLAUDE.md 或 .claude/ 存在；空的 .claude/ 就是最小專案
printf '# clean project\n' > "$PROJ/README.md"
SLUG0=$("$PY" -c 'import re,sys;print(re.sub(r"[^A-Za-z0-9]","-",sys.argv[1]))' "$PROJ")
mkdir -p "$HOME_T/.claude/projects/$SLUG0"   # 真的 Claude Code 專案跑過一次 session 就有這個目錄＋至少一個 transcript；沒有的話 usage 段會誠實記 partial
printf '{"type":"assistant","message":{"content":[]}}\n' > "$HOME_T/.claude/projects/$SLUG0/t.jsonl"
OUT="$TMP/out-clean"
SELFCHECK_HOME="$HOME_T" "$PY" "$SCAN" --cwd "$PROJ" --dry-run --out "$OUT" > "$TMP/clean.log" 2>&1
rc=$?
[ "$rc" -eq 0 ] || { cat "$TMP/clean.log"; [ -f "$OUT/scan_meta.json" ] && "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("incomplete"))' "$OUT/scan_meta.json"; fail "乾淨專案 rc=$rc（期望 0）"; }
[ -f "$OUT/candidates.jsonl" ] || fail "乾淨專案沒有 candidates.jsonl"
n=$("$PY" -c 'import sys;print(sum(1 for l in open(sys.argv[1]) if l.strip()))' "$OUT/candidates.jsonl")
[ "$n" -eq 0 ] || { cat "$OUT/candidates.jsonl"; fail "乾淨專案候選 $n（期望 0）"; }
pass "乾淨專案 rc=0、候選 0"

echo "== 3. 衝突專案：矛盾的兩條規則進同一桶 =="
PROJ2="$HOME_T/proj-conflict"
mkdir -p "$PROJ2"
printf '# rules\n\n## 通知\n- 🚫 一律不要用 webhook 發通知，改用檔案記錄\n' > "$PROJ2/CLAUDE.md"
SLUG=$("$PY" -c 'import re,sys;print(re.sub(r"[^A-Za-z0-9]","-",sys.argv[1]))' "$PROJ2")
MEM="$HOME_T/.claude/projects/$SLUG/memory"
mkdir -p "$MEM"
printf '# Memory Index\n- [m1](m1.md) — 一律用 webhook\n' > "$MEM/MEMORY.md"
printf '# m1\n- 一律用 webhook 發通知，最快\n' > "$MEM/m1.md"
OUT2="$TMP/out-conflict"
SELFCHECK_HOME="$HOME_T" "$PY" "$SCAN" --cwd "$PROJ2" --dry-run --out "$OUT2" > "$TMP/conflict.log" 2>&1
rc=$?
[ "$rc" -eq 0 ] || [ "$rc" -eq 1 ] || { cat "$TMP/conflict.log"; fail "衝突專案 rc=$rc（期望 0 或 1）"; }
[ -f "$OUT2/rules.jsonl" ] || fail "衝突專案沒有 rules.jsonl"
c=$("$PY" -c '
import json, sys
rules = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
a = [r for r in rules if "一律不要用 webhook" in r["text"]]
b = [r for r in rules if "一律用 webhook" in r["text"] and "不要" not in r["text"]]
print(1 if a and b and (set(a[0]["topics"]) & set(b[0]["topics"])) else 0)' "$OUT2/rules.jsonl")
[ "$c" -eq 1 ] || { cat "$OUT2/rules.jsonl"; fail "衝突專案：兩條 webhook 規則沒有都抽出、或沒落在同一主題"; }
[ -f "$OUT2/rules_by_topic/notify.md" ] || { ls "$OUT2/rules_by_topic"; fail "衝突專案沒有 rules_by_topic/notify.md"; }
grep -q "一律不要用 webhook" "$OUT2/rules_by_topic/notify.md" && grep -q "一律用 webhook" "$OUT2/rules_by_topic/notify.md" || fail "notify.md 沒同時含兩條規則"
pass "衝突專案：兩條矛盾規則同進 notify 桶"

echo "ALL PASS"
