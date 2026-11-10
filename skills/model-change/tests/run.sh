#!/bin/sh
# model-change 回歸測試（零依賴、不連網、不讀真資料）：
#   1. 掃描器內建 --selftest 全過
#   2. 預設範圍：把 HOME 指到臨時目錄跑一次，rc 0；選用的 memory 根目錄不存在不算錯
#   3. 自訂範圍＋--json：rc 0、報告是合法 JSON、權限 0600、被掃的檔一個位元組都沒變
#   4. 根目錄不存在 → rc 3；roots 檔壞掉 → rc 2；--json 寫不出去 → rc 4
# 用法：sh skills/model-change/tests/run.sh   （PYTHON 環境變數可換直譯器）
set -u
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # CI 常是 C／POSIX locale，別讓中文輸出炸在編碼上
HERE=$(cd "$(dirname "$0")" && pwd -P)
SCAN="$HERE/../scripts/model_audit.py"
PY=${PYTHON:-python3}
TMP=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/model-change-test.XXXXXX")" && pwd -P)
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL $*"; exit 1; }
pass() { echo "PASS $*"; }

echo "== 1. selftest =="
"$PY" "$SCAN" --selftest > "$TMP/selftest.log" 2>&1 || { grep -v "^\[PASS\]" "$TMP/selftest.log" | tail -20; fail "selftest 退出碼非 0（上面是非 PASS 的行）"; }
set -- $(tail -1 "$TMP/selftest.log")
[ "${1:-}" = "selftest" ] || fail "selftest 末行不是摘要：$*"
case "${2:-}" in */*) n=${2%/*}; t=${2#*/};; *) fail "selftest 摘要格式：${2:-}";; esac
[ "$n" = "$t" ] && [ "$t" -gt 0 ] || fail "selftest $2"
grep -q "^\[SKIP\]" "$TMP/selftest.log" && echo "NOTE $(grep -c '^\[SKIP\]' "$TMP/selftest.log") 條被跳過（見下）" && grep "^\[SKIP\]" "$TMP/selftest.log"
pass "selftest $2"

HOME_T="$TMP/home"
PROJ="$TMP/proj"
mkdir -p "$HOME_T/.claude/hooks" "$PROJ/bin"
printf '#!/bin/sh\nclaude -p "nightly" --model claude-old-1\n' > "$HOME_T/.claude/hooks/nightly.sh"
printf '#!/bin/sh\nclaude -p "hi"\n' > "$PROJ/bin/bare.sh"
printf 'cmd = ["claude", "-p", prompt, "--model", "claude-new-2", "--effort", "high"]\n' > "$PROJ/bin/good.py"

echo "== 2. 預設範圍（HOME 指到臨時目錄）=="
( cd "$PROJ" && HOME="$HOME_T" USERPROFILE="$HOME_T" "$PY" "$SCAN" --current claude-new-2 --legacy claude-old-1 ) > "$TMP/default.log" 2>&1
rc=$?
[ "$rc" -eq 0 ] || { cat "$TMP/default.log"; fail "預設範圍 rc=$rc（期望 0）"; }
grep -q "claude-memory" "$TMP/default.log" && grep -q "選用" "$TMP/default.log" || { cat "$TMP/default.log"; fail "選用的 memory 根目錄沒有照「選用」顯示"; }
grep -q "確定 3 處；沒寫 --model 1 處、有 model 沒寫 --effort 1 處、旗標在後面才補要看條件 0 處；不確定 0 處" "$TMP/default.log" || { cat "$TMP/default.log"; fail "預設範圍的 D 段計數不對"; }
pass "預設範圍 rc=0、D 段 3 處（1 沒寫 model、1 沒寫 effort）"

echo "== 3. 自訂範圍＋--json，被掃的檔不變 =="
printf '{"roots": [{"name": "proj", "path": "%s", "maxdepth": 3}]}\n' "$PROJ" > "$TMP/roots.json"
sum() { "$PY" -c 'import hashlib,os,sys
h=hashlib.sha256()
for d,_,fs in sorted(os.walk(sys.argv[1])):
    for f in sorted(fs):
        p=os.path.join(d,f); h.update(p.encode()); h.update(open(p,"rb").read()); h.update(str(os.stat(p).st_mtime_ns).encode())
print(h.hexdigest())' "$1"; }
before=$(sum "$PROJ")
"$PY" "$SCAN" --current claude-new-2 --roots-file "$TMP/roots.json" --json "$TMP/report.json" > "$TMP/custom.log" 2>&1
rc=$?
[ "$rc" -eq 0 ] || { cat "$TMP/custom.log"; fail "自訂範圍 rc=$rc（期望 0）"; }
[ "$(sum "$PROJ")" = "$before" ] || fail "被掃的目錄有變動（內容或修改時間）"
"$PY" -c 'import json,os,sys
r=json.load(open(sys.argv[1],encoding="utf-8"))
calls=[h for h in r["headless"] if h["status"]=="call"]
assert len(calls)==2, calls
assert sorted(h["model_kind"] or "none" for h in calls)==["literal","none"], calls
assert not r["bad_roots"] and not r["incomplete_roots"]
if os.name=="posix":
    assert (os.stat(sys.argv[1]).st_mode & 0o777)==0o600, oct(os.stat(sys.argv[1]).st_mode)
' "$TMP/report.json" || fail "report.json 內容或權限不對"
pass "自訂範圍 rc=0、JSON 合法、權限 0600、被掃的檔沒變"

echo "== 4. 結束碼 =="
printf '{"roots": [{"name": "gone", "path": "%s/nope"}]}\n' "$TMP" > "$TMP/roots-missing.json"
"$PY" "$SCAN" --roots-file "$TMP/roots-missing.json" > "$TMP/missing.log" 2>&1; rc=$?
[ "$rc" -eq 3 ] || { cat "$TMP/missing.log"; fail "根目錄不存在 rc=$rc（期望 3）"; }
printf '{"roots": "oops"}\n' > "$TMP/roots-bad.json"
"$PY" "$SCAN" --roots-file "$TMP/roots-bad.json" > "$TMP/bad.log" 2>&1; rc=$?
[ "$rc" -eq 2 ] || { cat "$TMP/bad.log"; fail "roots 檔格式錯 rc=$rc（期望 2）"; }
"$PY" "$SCAN" --current claude-new-2 --roots-file "$TMP/roots.json" --json "$TMP/no-such-dir/report.json" > "$TMP/nojson.log" 2>&1; rc=$?
[ "$rc" -eq 4 ] || { cat "$TMP/nojson.log"; fail "--json 寫不出去 rc=$rc（期望 4）"; }
pass "結束碼：根目錄不存在 3、roots 檔壞掉 2、--json 寫不出去 4"

echo "ALL PASS"
