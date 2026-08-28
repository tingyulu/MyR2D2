#!/bin/sh
# ai-search 行為驗收矩陣 —— 自己驗一次，別只信 README。
#
# 用法：
#   sh tests/matrix.sh                    # 驗旁邊那支 ../scripts/ai-search.sh
#   SH=bash sh tests/matrix.sh            # 指定「用哪個 shell 執行受測腳本」
#   AI_SEARCH_SH=/path/to/ai-search.sh sh tests/matrix.sh
#
# 特性：
# - **不燒任何額度、不連任何網路**：後端一律用 stub 或 `cat` 模擬，不會真的呼叫 codex。
# - **不弄髒你的目錄**：所有產出寫在 mktemp 暫存區，跑完自動清掉。
# - 退出碼 0＝全過，1＝有失敗（可直接放進 CI）。
#
# MIT License — part of MyR2D2 (github.com/tingyulu/MyR2D2)

set -u

# 環境隔離：外層 shell 若 export 過這幾個變數（例如平常就把 AI_SEARCH_CMD 指到自己的後端），
# 會污染下面所有「乾淨環境」情境。測試自己要先把環境清乾淨。
unset AI_SEARCH_CMD AI_SEARCH_DIR

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
S="${AI_SEARCH_SH:-$DIR/../scripts/ai-search.sh}"
SH="${SH:-/bin/sh}"
[ -f "$S" ] || { echo "❌ 找不到受測腳本：$S" >&2; exit 1; }

W=$(mktemp -d) || { echo "❌ 建不出暫存目錄" >&2; exit 1; }
trap 'rm -rf "$W"' EXIT
STUB="$W/stub"; mkdir -p "$STUB"
pass=0; fail=0; skip=0

Q="台灣的首都是哪裡"

chk() { # chk <label> <expect_status> <expect_rc> <actual_status> <actual_rc>
	if [ "$4" = "$2" ] && [ "$5" = "$3" ]; then
		printf '  ok   %-38s status=%-24s rc=%s\n' "$1" "$4" "$5"; pass=$((pass + 1))
	else
		printf '  FAIL %-38s got status=%s rc=%s (want %s / %s)\n' "$1" "$4" "$5" "$2" "$3"; fail=$((fail + 1))
	fi
}
ok_() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
no_() { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
skip_() { printf '  skip %s\n' "$1"; skip=$((skip + 1)); }

run() { # 執行並抓狀態行；stdout 被 $(…) 捕捉＝模擬真實呼叫端
	OUT=$("$@" 2>/dev/null); RC=$?
	ST=$(printf '%s' "$OUT" | sed -n 's/^AI_SEARCH_STATUS: //p' | tail -1)
}
mkstub() { # mkstub <exit_code> <stderr_text>：假裝成 codex
	cat >"$STUB/codex" <<EOF
#!/bin/sh
[ "\$1 \$2" = "login status" ] && { echo "Logged in"; exit 0; }
echo "$2" >&2
exit $1
EOF
	chmod +x "$STUB/codex"
}

echo "受測腳本：$S"
echo "執行 shell：$SH"
echo

echo "-- A. 後端錯誤分類（stub 模擬訊息，非真實後端輸出）--"
mkstub 1 "stream error: 429 Too Many Requests (rate limit exceeded)"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "額度(429)" failed_quota 2 "$ST" "$RC"
mkstub 1 "ERROR: not logged in. Please run codex login."
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "未登入" skipped_not_logged_in 0 "$ST" "$RC"
mkstub 1 "error sending request: dns error: failed to lookup address"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "網路" failed_network 2 "$ST" "$RC"
mkstub 1 "403 Forbidden: this model is not available in your region"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "地區/政策" failed_policy 2 "$ST" "$RC"
mkstub 2 "error: unexpected argument '--ephemeral' found"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "版本不相容" failed_version 2 "$ST" "$RC"
mkstub 1 "something completely unfamiliar happened"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "無法歸類" failed_unknown 2 "$ST" "$RC"
cat >"$STUB/codex" <<'EOF'
#!/bin/sh
[ "$1 $2" = "login status" ] && { echo "Logged in"; exit 0; }
exit 0
EOF
chmod +x "$STUB/codex"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "空回覆" failed_empty 2 "$ST" "$RC"

echo "-- B. 沒有後端＝降級，不是錯誤 --"
run env PATH=/usr/bin:/bin $SH "$S" "$Q"
chk "未安裝→exit 0" skipped_not_installed 0 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "$Q" --strict
chk "未安裝+--strict→exit 3" skipped_not_installed 3 "$ST" "$RC"
mkstub 1 "stream error: 429 rate limit exceeded"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q" --soft-fail
chk "--soft-fail→failed 回 0" failed_quota 0 "$ST" "$RC"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "預設 failed 仍回 2" failed_quota 2 "$ST" "$RC"

echo "-- C. 可插拔後端 AI_SEARCH_CMD --"
run env PATH=/usr/bin:/bin AI_SEARCH_CMD='cat' $SH "$S" "$Q" --no-save
chk "自訂後端可用" ok 0 "$ST" "$RC"
run env PATH=/usr/bin:/bin AI_SEARCH_CMD='definitely_not_a_real_command_xyz' $SH "$S" "$Q"
chk "自訂後端不存在→skipped" skipped_not_installed 0 "$ST" "$RC"
run env PATH=/usr/bin:/bin AI_SEARCH_CMD='sh -c "echo not logged in >&2; exit 1"' $SH "$S" "$Q"
chk "自訂後端未登入→skipped" skipped_not_logged_in 0 "$ST" "$RC"
run env PATH=/usr/bin:/bin AI_SEARCH_CMD='false' $SH "$S" "$Q"
chk "自訂後端失敗→failed" failed_unknown 2 "$ST" "$RC"

echo "-- D. 呼叫鏈：降級不得中斷上層 --"
cat >"$W/chain.sh" <<EOF
#!/bin/sh
set -e
R=\$(env PATH=/usr/bin:/bin $SH "$S" "$Q" 2>/dev/null)
echo UPSTREAM_ALIVE
printf '%s' "\$R" | sed -n 's/^AI_SEARCH_STATUS: //p'
EOF
chmod +x "$W/chain.sh"
CHAIN=$("$W/chain.sh" 2>&1); CRC=$?
if [ "$CRC" -eq 0 ] && printf '%s' "$CHAIN" | grep -q UPSTREAM_ALIVE; then
	ok_ "set -e + \$(…) 下上層存活"
else
	no_ "set -e + \$(…) 下上層存活（rc=$CRC）"
fi
# 負對照組：真失敗（429、未帶 --soft-fail）必須讓 set -e 的上層確實中斷——
# 與上面的正面案例成對，證明「該停的時候真的會停」，不是腳本永遠回 0。
mkstub 1 "stream error: 429 Too Many Requests (rate limit exceeded)"
cat >"$W/chain2.sh" <<EOF
#!/bin/sh
set -e
R=\$(env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q" 2>/dev/null)
echo SHOULD_NOT_REACH
EOF
chmod +x "$W/chain2.sh"
CHAIN2=$("$W/chain2.sh" 2>&1); CRC2=$?
if [ "$CRC2" -ne 0 ] && ! printf '%s' "$CHAIN2" | grep -q SHOULD_NOT_REACH; then
	ok_ "負對照：真失敗在 set -e 下確實中斷上層"
else
	no_ "負對照：真失敗沒有中斷上層（rc=$CRC2）"
fi

echo "-- E. 問題輸入的各種形式 --"
# 多個位置參數接成一句問題，且真的原封送進 prompt。
OUTQ=$(env PATH=/usr/bin:/bin AI_SEARCH_CMD='cat' $SH "$S" 台北 天氣 如何 --no-save 2>/dev/null)
if printf '%s' "$OUTQ" | grep -q '台北 天氣 如何'; then ok_ "多位置參數接成一句問題"; else no_ "多位置參數接成一句問題"; fi
# stdin 問題。
ST=$(printf '什麼是 astromech droid' | env PATH=/usr/bin:/bin AI_SEARCH_CMD='cat' $SH "$S" - --no-save 2>/dev/null | sed -n 's/^AI_SEARCH_STATUS: //p'); RC=$?
chk "stdin 問題(-)" ok 0 "$ST" "$RC"
# `--` 之後即使以 - 開頭也當問題文字。
run env PATH=/usr/bin:/bin AI_SEARCH_CMD='cat' $SH "$S" --no-save -- --這是問題不是選項
chk "-- 之後當問題文字" ok 0 "$ST" "$RC"
# `-- -`：單一 `-` 經 -- 隔開＝問題文字,不可被當成 stdin 哨兵（否則互動下會卡 cat 等輸入）。
# stdin 接 /dev/null:若誤判成 stdin 會讀到空→「沒有問題」rc 1;正確當問題「-」則 ok。
ST=$(env PATH=/usr/bin:/bin AI_SEARCH_CMD='cat' $SH "$S" --no-save -- - </dev/null 2>/dev/null | sed -n 's/^AI_SEARCH_STATUS: //p'); RC=$?
chk "-- 之後的裸 - 當問題非 stdin" ok 0 "$ST" "$RC"

echo "-- F. 環境邊界 --"
SP="$W/dir with space"; mkdir -p "$SP/scripts"
cp "$S" "$SP/scripts/ai-search.sh"
run env PATH=/usr/bin:/bin $SH "$SP/scripts/ai-search.sh" "路徑含空白的問題"
chk "腳本路徑含空白" skipped_not_installed 0 "$ST" "$RC"
RO="$W/ro"; mkdir -p "$RO"; chmod 500 "$RO"
run env PATH=/usr/bin:/bin AI_SEARCH_DIR="$RO/nested" AI_SEARCH_CMD='cat' $SH "$S" "$Q"
chk "落檔目錄不可寫仍 ok" ok 0 "$ST" "$RC"
chmod 700 "$RO"

echo "-- G. 用法錯誤（全部 exit 1，不印狀態）--"
run env PATH=/usr/bin:/bin $SH "$S"
chk "沒有問題" "" 1 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "   "
chk "問題只有空白" "" 1 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "$Q" --effort banana
chk "--effort 非法值" "" 1 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "$Q" --strict --soft-fail
chk "兩開關併用" "" 1 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "$Q" --bogus-flag
chk "不認得的選項" "" 1 "$ST" "$RC"
run env PATH=/usr/bin:/bin $SH "$S" "$Q" --model
chk "--model 缺值" "" 1 "$ST" "$RC"

echo "-- H. 分類器不得把真失敗吞成 skipped（安靜的假成功）--"
mkstub 1 "authorization policy denied for this organization"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "policy 不判成未登入" failed_policy 2 "$ST" "$RC"
mkstub 1 "auth service unavailable, connection reset"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "auth 字樣實為網路" failed_network 2 "$ST" "$RC"
mkstub 1 "internal server error; request 401abc failed, retry in 401 seconds"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "401 數字不判成未登入" failed_unknown 2 "$ST" "$RC"
mkstub 1 "HTTP 401 Unauthorized: please sign in"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "帶語境 401→未登入" skipped_not_logged_in 0 "$ST" "$RC"
cat >"$STUB/codex" <<'EOF'
#!/bin/sh
[ "$1 $2" = "login status" ] && { echo "Logged in using ChatGPT (sign-in method: browser; session expires in 401 seconds)"; exit 0; }
echo "backend reached" >&2; exit 1
EOF
chmod +x "$STUB/codex"
run env PATH="$STUB:/usr/bin:/bin" $SH "$S" "$Q"
chk "已登入訊息不被誤判" failed_unknown 2 "$ST" "$RC"

echo "-- I. 落檔：不覆蓋、不外洩、不被 symlink 騙 --"
COLL="$W/collide"
env PATH=/usr/bin:/bin AI_SEARCH_DIR="$COLL" AI_SEARCH_CMD='cat' $SH "$S" "$Q" >/dev/null 2>&1
env PATH=/usr/bin:/bin AI_SEARCH_DIR="$COLL" AI_SEARCH_CMD='cat' $SH "$S" "$Q" >/dev/null 2>&1
N=$(ls "$COLL" 2>/dev/null | wc -l | tr -d ' ')
[ "$N" = "2" ] && ok_ "同秒兩次不互相覆蓋" || no_ "同秒兩次不互相覆蓋（只剩 $N 份）"
PERM=$(ls -l "$COLL"/*.md 2>/dev/null | head -1 | cut -c1-10)
[ "$PERM" = "-rw-------" ] && ok_ "落檔權限 600" || no_ "落檔權限 600（實際 $PERM）"
LEFT=$(ls -a "$COLL" 2>/dev/null | grep '^\.ai-search\.' | wc -l | tr -d ' ')
[ "$LEFT" = "0" ] && ok_ "不留暫存殘檔" || no_ "不留暫存殘檔（$LEFT 個）"
VIC="$W/victim.txt"; printf 'DO_NOT_TOUCH\n' >"$VIC"
SD="$W/symdir"; mkdir -p "$SD"; ln -s "$VIC" "$SD/.ai-search.$$.tmp" 2>/dev/null
env PATH=/usr/bin:/bin AI_SEARCH_DIR="$SD" AI_SEARCH_CMD='cat' $SH "$S" "$Q" >/dev/null 2>&1
[ "$(cat "$VIC")" = "DO_NOT_TOUCH" ] && ok_ "暫存檔 symlink 攻擊不生效" || no_ "暫存檔 symlink 攻擊寫穿了受害檔"
ODD="$W/含冒號: 與空白 的問題"
YD="$W/yaml"
env PATH=/usr/bin:/bin AI_SEARCH_DIR="$YD" AI_SEARCH_CMD='cat' $SH "$S" "$ODD" >/dev/null 2>&1
YF=$(ls "$YD"/*.md 2>/dev/null | head -1)
if [ -z "$YF" ]; then
	no_ "特殊問題字元仍落得了檔"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
	if python3 -c "
import sys,yaml,re
s=open(sys.argv[1],encoding='utf-8').read()
m=re.match(r'^---\n(.*?)\n---\n',s,re.S)
yaml.safe_load(m.group(1))
" "$YF" >/dev/null 2>&1; then
		ok_ "含冒號引號的問題 frontmatter 仍是合法 YAML"
	else
		no_ "含冒號引號的問題 frontmatter 壞了"
	fi
else
	skip_ "frontmatter YAML 驗證（本機無 python3+pyyaml）"
fi
LONGQ="這是一個很長的中文問題用來測試檔名截斷行為超過六十個字元的情況繼續加長繼續加長繼續加長是不是台灣"
LD="$W/locale"
env LC_ALL=C PATH=/usr/bin:/bin AI_SEARCH_DIR="$LD" AI_SEARCH_CMD='cat' $SH "$S" "$LONGQ" >/dev/null 2>&1
[ "$(ls "$LD" 2>/dev/null | wc -l | tr -d ' ')" = "1" ] && ok_ "非 UTF-8 locale 下長中文問題仍落檔" || no_ "非 UTF-8 locale 下長中文問題落檔失敗"
# 問題夾帶終端跳脫序列（ESC＋OSC 改視窗標題＋BEL）→ 落檔檔名不得殘留控制位元組:
# 否則腳本印「📄 已落檔：<path>」或使用者 ls 到它時,終端會執行那段跳脫序列。
ED="$W/escape"; EVILQ=$(printf 'evil\033]0;PWNED\007q')
env PATH=/usr/bin:/bin AI_SEARCH_DIR="$ED" AI_SEARCH_CMD='cat' $SH "$S" "$EVILQ" >/dev/null 2>&1
EF=""; for f in "$ED"/*.md; do [ -e "$f" ] && EF="$f"; done
BN=${EF##*/}
if [ -z "$EF" ]; then
	no_ "含 ESC/BEL 的問題仍落得了檔"
elif printf '%s' "$BN" | LC_ALL=C grep -q '[[:cntrl:]]'; then
	no_ "問題含 ESC/BEL 時檔名殘留控制位元組"
else
	ok_ "問題含 ESC/BEL 時檔名不留控制位元組"
fi

echo "-- J. 失敗時看得到原因 --"
OUT2=$(env PATH=/usr/bin:/bin AI_SEARCH_CMD='sh -c "echo hidden_reason >&2; exit 0"' $SH "$S" "$Q" 2>&1 >/dev/null)
printf '%s' "$OUT2" | grep -q hidden_reason && ok_ "空回覆會印出後端 stderr" || no_ "空回覆沒印出後端 stderr"
OUT3=$(env PATH=/usr/bin:/bin AI_SEARCH_CMD='sh -c "echo stdout_only_error; exit 1"' $SH "$S" "$Q" 2>&1 >/dev/null)
printf '%s' "$OUT3" | grep -q stdout_only_error && ok_ "錯誤寫在 stdout 也照印" || no_ "錯誤寫在 stdout 沒印出來"

printf '\n總計：%s 過 / %s 失敗' "$pass" "$fail"
[ "$skip" -gt 0 ] && printf ' / %s 略過' "$skip"
printf '\n'
[ "$fail" -eq 0 ]
