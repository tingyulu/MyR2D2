#!/bin/sh
# model-change 回歸測試（零依賴、不連網、不讀真資料）：
#   1. 掃描器內建 --selftest 全過
#   2. 預設範圍：把 HOME 指到臨時目錄跑一次，rc 0；選用的 memory 根目錄不存在不算錯；有預設範圍提醒
#   2b. 範圍裡放一份掃描器副本與這支測試檔：報告一個字都不變（它們不享有豁免，靠的是原始碼裡沒有會被判讀的字面）
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
# 測試資料裡的執行檔名稱與模型 ID 用變數拼：這支檔被掃描器掃到時，不該有任何一行被判讀成呼叫或模型 ID
C=claude
NEW="$C-new-2"
OLD="$C-old-1"
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
printf '#!/bin/sh\n%s -p "nightly" --model %s\n' "$C" "$OLD" > "$HOME_T/.claude/hooks/nightly.sh"
printf '#!/bin/sh\n%s -p "hi"\n' "$C" > "$PROJ/bin/bare.sh"
printf 'cmd = ["%s", "-p", prompt, "--model", "%s", "--effort", "high"]\n' "$C" "$NEW" > "$PROJ/bin/good.py"

echo "== 2. 預設範圍（HOME 指到臨時目錄）=="
( cd "$PROJ" && HOME="$HOME_T" USERPROFILE="$HOME_T" "$PY" "$SCAN" --current "$NEW" --legacy "$OLD" ) > "$TMP/default.log" 2>&1
rc=$?
[ "$rc" -eq 0 ] || { cat "$TMP/default.log"; fail "預設範圍 rc=$rc（期望 0）"; }
grep -q "claude-memory" "$TMP/default.log" && grep -q "選用" "$TMP/default.log" || { cat "$TMP/default.log"; fail "選用的 memory 根目錄沒有照「選用」顯示"; }
grep -q "確定 3 處；沒寫 --model 1 處、有 model 沒寫 --effort 1 處、旗標在後面才補要看條件 0 處；不確定 0 處" "$TMP/default.log" || { cat "$TMP/default.log"; fail "預設範圍的 D 段計數不對"; }
grep -q "預設範圍" "$TMP/default.log" || { cat "$TMP/default.log"; fail "用預設範圍時沒有提醒它看不到別處的部署副本"; }
pass "預設範圍 rc=0、D 段 3 處（1 沒寫 model、1 沒寫 effort）、有預設範圍提醒"

echo "== 2b. 掃描範圍裡有掃描器的副本與這支測試檔：報告不變 =="
mkdir -p "$PROJ/tools"
cp "$SCAN" "$PROJ/tools/model_audit.py"
cp "$HERE/run.sh" "$PROJ/tools/run.sh"
( cd "$PROJ" && HOME="$HOME_T" USERPROFILE="$HOME_T" "$PY" "$SCAN" --current "$NEW" --legacy "$OLD" --json "$TMP/copy.json" ) > "$TMP/copy.log" 2>&1
rc=$?
rm -rf "$PROJ/tools"
[ "$rc" -eq 0 ] || { cat "$TMP/copy.log"; fail "含副本的預設範圍 rc=$rc（期望 0）"; }
# 正控：多掃到的兩個檔真的有被讀進去（檔數 +2），不是因為沒掃到才乾淨
"$PY" -c 'import json,sys
r=json.load(open(sys.argv[1],encoding="utf-8"))
cwd=[x for x in r["roots"] if x["name"]=="cwd"][0]
assert cwd["files"]==4, cwd
hits=[h["file"] for h in r["headless"]+r["launchers"]]+[x["file"] for x in r["stale_in_code"]]
bad=[f for f in hits if "tools" in f]
assert not bad, bad
' "$TMP/copy.json" || { cat "$TMP/copy.log"; fail "掃描器的副本或測試檔被列進 C、D、E 段，或根本沒被讀到"; }
# 除了 A 段的檔數與「只是提到」的計數，報告其餘每一行都要跟沒有副本時一樣
strip() { grep -v "^   cwd \|判成只是訊息" "$1"; }
[ "$(strip "$TMP/copy.log")" = "$(strip "$TMP/default.log")" ] || { diff "$TMP/default.log" "$TMP/copy.log"; fail "加了副本之後報告變了"; }
pass "掃描器副本與測試檔有被讀到（檔數 +2），B、C、D、E 段一行都沒變"

echo "== 3. 自訂範圍＋--json，被掃的檔不變 =="
printf '{"roots": [{"name": "proj", "path": "%s", "maxdepth": 3}]}\n' "$PROJ" > "$TMP/roots.json"
sum() { "$PY" -c 'import hashlib,os,sys
h=hashlib.sha256()
for d,_,fs in sorted(os.walk(sys.argv[1])):
    for f in sorted(fs):
        p=os.path.join(d,f); h.update(p.encode()); h.update(open(p,"rb").read()); h.update(str(os.stat(p).st_mtime_ns).encode())
print(h.hexdigest())' "$1"; }
before=$(sum "$PROJ")
"$PY" "$SCAN" --current "$NEW" --roots-file "$TMP/roots.json" --json "$TMP/report.json" > "$TMP/custom.log" 2>&1
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
"$PY" "$SCAN" --current "$NEW" --roots-file "$TMP/roots.json" --json "$TMP/no-such-dir/report.json" > "$TMP/nojson.log" 2>&1; rc=$?
[ "$rc" -eq 4 ] || { cat "$TMP/nojson.log"; fail "--json 寫不出去 rc=$rc（期望 4）"; }
pass "結束碼：根目錄不存在 3、roots 檔壞掉 2、--json 寫不出去 4"

echo "ALL PASS"
