#!/usr/bin/env python3
"""model_audit.py — 換模型時的盤點掃描器：列出哪些檔案寫死了模型 ID、哪些無頭呼叫沒寫模型或 effort。
不改任何被掃的檔；只有給 --json 時會寫出那一份報告檔。純標準庫，不連網。

用法：
  model_audit.py --current ID[,ID…] [--legacy ID[,ID…]] [--roots-file roots.json] [--json out.json]
                 [--follow-links] [--no-text]
  model_audit.py --selftest

  --current       官方文件列為現役的完整模型 ID（自己從原文抄；這支工具不內建任何模型清單）
  --legacy        官方文件列為 legacy、仍可用的完整 ID
  --roots-file    要掃的根目錄清單（JSON）；不給就掃 ~/.claude（不含 projects 底下的對話紀錄）、
                  ~/.claude/projects 各專案的 memory 筆記、目前目錄
  --json          把完整結果另存一份 JSON（先寫暫存檔再換名，權限 0600）
  --follow-links  連結指到根目錄外時也跟過去讀（預設不跟，只計數）
  --no-text       報告不帶指令原文片段（要把報告貼到別處、存成 CI 產物時用）

roots.json 長這樣（path 可用 ~，也可以是單一檔案；maxdepth 預設 8；only 是選填的路徑 regex，
比對時路徑分隔符一律換成 /，只掃符合的檔；每個 root 可另加 "skip_dirs"（只對它生效）與
"optional": true（不存在或 0 個檔時不算錯））：
  {"roots": [{"name": "runners", "path": "~/.my-runner", "maxdepth": 2},
             {"name": "notes", "path": "~/notes", "maxdepth": 3, "only": "/models/[^/]+\\\\.md$"}],
   "skip_dirs": ["my-big-data-dir"]}
給了 --roots-file 就只掃檔裡列的；預設範圍（設定目錄、memory 筆記、目前目錄）要保留的話自己列進去。

輸出五段：A 根目錄與檔數｜B 各區出現的完整 ID（現役／legacy／不認得）｜C 程式與設定檔裡的非現役 ID
          ｜D 無頭呼叫（claude -p）有沒有寫 --model 與 --effort｜E 常駐啟動指令（claude --model …）
結束碼：0 正常｜2 參數錯誤｜3 掃描不完整（根目錄不存在、掃到 0 個檔、有檔案或目錄讀不到），結果不可信
        ｜4 掃描完整但 --json 寫不出去（掃描不完整又寫不出去時回 3，stderr 會講報告沒寫成）

設計上的自我檢查：
  1. 每個根目錄都印出掃了幾個檔、略過了什麼；不存在、0 個檔或讀不到就回 3，不讓「沒掃到」看起來像「沒問題」。
  2. 不內建模型清單：現役與 legacy 由呼叫的人從官方原文給，工具不會拿過期的清單替你判斷。
  3. 「不認得」獨立一類照樣列出：新模型、打錯的 ID、雲端平台包裝過的 ID、網址片段都會落在這裡，由人判斷。
  4. D 段把一條指令當成一個單位來看（續行、括號、清單都先組起來），旗標不會借到隔壁那條指令的。
     看不出來的（字串裡的指令、執行檔名稱認不得、引號沒對上）列在「不確定」，不歸為安全。

已知限制（都會讓結果偏掉，報告看起來卻正常，所以寫在這裡）：
  - 帶日期尾碼的 ID 是去掉尾碼後比對的，日期打錯看不出來。
  - 只認完整 ID，設定裡寫別名（檔位名稱）的地方不在 B、C 段，要另外看。
  - D、E 段只判讀程式與設定檔；.md 與 .txt 只計「哪幾行提到 claude -p」，不判讀。
  - 指令先存進變數再補旗標的，只往後找同名變數的 += 與 extend／append／push，而且不看條件成不成立；
    其他組法會被報成「沒寫 model」。YAML 一行一個參數的清單寫法認不到。
  - JS 的 regex 字面值、template 與 python f-string 裡的運算式沒有另外處理，裡面藏的呼叫可能漏掉。
  - 太大的檔（超過 1.5 MB）不讀、不是 UTF-8 的檔替換壞字元後照讀：A 段有計數與路徑，但不影響結束碼。
  - 遮機密靠樣式比對（長串英數、token=… 這類鍵值、網址帳密），不保證遮乾淨；不放心就加 --no-text。
  - 沒有防「掃描途中檔案被換成連結」這種刻意攻擊；請掃自己信得過的目錄。
"""
import argparse
import bisect
import json
import os
import re
import shutil
import sys
import tempfile

# 兩種命名都認：claude-<家族>-<版本…> 與 claude-<版本>-<家族>-<日期>（版本在前的舊式寫法）
ID_RE = re.compile(r"(?<![A-Za-z0-9_-])claude-(?:[a-z]+-[0-9]|[0-9]+-[0-9a-z])[A-Za-z0-9.\-]*")
# 報告裡的指令片段可能帶到金鑰：24 個字元以上的連續英數一律遮掉
SECRETISH_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
DATE_SUFFIX_RE = re.compile(r"-20[0-9]{6}$")
MODEL_FLAG_RE = re.compile(r"""--model(?:=|["',\s]+)\s*["']?([A-Za-z0-9.\-_\[\]${}:]+)""")
MODEL_ANY_RE = re.compile(r"--model\b")
EFFORT_FLAG_RE = re.compile(r"--effort\b")
LITERAL_MODEL_RE = re.compile(r"[A-Za-z0-9.\-]+(\[1m\])?")
# claude 執行檔：裸字 claude／CLAUDE（前後不接字元、不是 .claude 目錄、不是 claude.ai 這類名稱），或 …claude_bin／CLAUDE_CMD 這類變數與函式
TOK_RE = re.compile(
    r"(?:(?<![\w.-])(?:claude|CLAUDE)(?![\w-])(?!\.(?!exe\b|cmd\b)[A-Za-z])(?!/)"
    r"|[A-Za-z_.]*(?:claude|CLAUDE)_(?:bin|cmd|path|exe|cli|BIN|CMD|PATH|EXE|CLI)\b(?:\(\))?)")
P_RE = re.compile(r"(?<![\w-])(?:-p|--print)(?![\w-])")
LAUNCH_FLAG_RE = re.compile(r"--(?:model|name|remote-control|continue|resume)\b")
# 沒看到執行檔名稱時，用這些旗標猜「這大概是在叫 claude」
CLAUDE_FLAG_RE = re.compile(r"--(?:model|effort|allowedTools|disallowedTools|output-format|permission-mode|append-system-prompt|mcp-config|max-turns|dangerously-skip-permissions)\b")
# 字串前面是這些，字串內容多半會被拿去執行
EXEC_HEAD_RE = re.compile(r"\b(?:bash|sh|zsh|dash|ksh|eval|ssh|tmux|nohup|sudo|xargs|osascript|watch|screen|timeout|launchctl)\b"
                          r"|\b(?:system|popen|Popen|subprocess|exec\w*|spawn\w*|run|call|check_output|check_call)\s*[.(]")
# 字串前面是這些，字串內容多半只是訊息
TALK_HEAD_RE = re.compile(r"(?:^|[\s;&|({])(?:echo|printf|print|println|log\w*|warn\w*|die|fail\w*|err\w*|info|debug|notify\w*|say|raise|throw|assert\w*"
                          r"|console\.\w+|logger\.\w+|logging\.\w+|re\.\w+|compile|description|help|usage)\b[^;&|]*$")
CMD_PREFIX_RE = re.compile(r"""\s*(?:(?:exec|nohup|time|command|env|caffeinate(?:\s+-\w+)*|timeout\s+\S+)\s+)*(?:[A-Za-z_]\w*=\S*\s+)*["']?[\w$/{}.~-]*""")
# 設定檔裡，這些欄位的字串值會被拿去執行
CFG_KEY_RE = re.compile(r"""(?:command|cmd|run|exec\w*|shell|entrypoint|program\w*|args?)["']?\s*[:=]\s*(?:\[\s*)?["']$""", re.I)
# 指令先存進變數（cmd = [...]、cmd=(...)），後面幾行可能才補旗標
ASSIGN_RE = re.compile(r"""(?:^|\n)[ \t]*(?:(?:local|declare|readonly|export|const|let|var)\s+(?:-\w+\s+)*)?([A-Za-z_][\w.]*)\s*(?::[^=\n]+)?\+?=\s*[\[(]?\s*["']?\$?\{?$""")
# 執行檔是變數、名稱看不出是不是 claude（"$AI_CLI" -p …；[cli, "-p", …]）。
# 變數與 -p 之間只准有旗標與它們的值；中間夾著子命令的（"$TMUX" capture-pane -p）不是在叫 claude，不列
VAR_CMD_RE = re.compile(r"""(?:^|[;&|(`]|\b(?:then|do|else|exec|nohup|command)\s)\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:timeout\s+\S+\s+)?"?\$\{?[A-Za-z_]\w*\}?"?(?:\s+(?:-[\w-]+(?:=\S*)?|"[^"]*"|'[^']*'|\$\{?\w+\}?))*\s+$""")
VAR_ARGV_RE = re.compile(r"""^\[\s*[A-Za-z_][\w.]*\s*,[^\[\]]*["']$""")
SECRET_KV_RE = re.compile(r"""(?i)((?:token|secret|password|passwd|api[_-]?key|authorization|bearer)\w*["']?\s*[:=]\s*["']?|bearer\s+)[^\s"']+""")
URL_CRED_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HEREDOC_RE = re.compile(r"""<<(-?)[ \t]*(?:(['"])([A-Za-z_]\w*)\2|\\?([A-Za-z_]\w*))""")
HEREDOC_SH_RE = re.compile(r"\b(?:bash|sh|zsh|dash|ksh|ssh|sudo|tmux)\b")
HEREDOC_PY_RE = re.compile(r"\bpython[0-9.]*\b")
HEREDOC_JS_RE = re.compile(r"\b(?:node|deno|bun)\b")
# 引號沒對上時退回的單行比對（只用來把東西列進「不確定」）
FALLBACK_RE = re.compile(r"""(?:\bclaude\b|CLAUDE_BIN|claude_bin|claude_cmd)[^\n#]{0,160}?(?:(?<![\w-])-p(?![\w-])|--print\b)""")

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".build", "build", "dist", "logs", "log",
             "site-packages", "vendor", "target", "coverage", ".next", "DerivedData", "Pods",
             "shell-snapshots", "file-history", "statsig", "paste-cache", "telemetry", "todos", "cache", "Caches"}
# 副檔名 → 語法家族（決定引號、註解、括號怎麼讀）；"" 是沒有副檔名的檔（看 shebang，沒有就當 shell）
FAM = {".sh": "sh", ".zsh": "sh", ".bash": "sh", "": "sh", ".py": "py", ".js": "js", ".mjs": "js", ".ts": "js", ".swift": "js",
       ".json": "cfg", ".toml": "cfg", ".yaml": "cfg", ".yml": "cfg", ".cfg": "cfg", ".conf": "cfg",
       ".service": "cfg", ".timer": "cfg", ".plist": "plist"}
TEXT_EXT = set(FAM) | {".md", ".txt"}
SKIP_SUFFIX = (".jsonl", ".log", ".csv", ".lock", ".min.js", ".map")
BRACKETS = {"sh": "(", "py": "([{", "js": "([", "cfg": "([{", "plist": "([{"}
CLOSERS = {")": "(", "]": "[", "}": "{"}
MAX_BYTES = 1_500_000
TAIL_CAP = 4000
SELF = os.path.realpath(__file__)
# 預設範圍：設定目錄（不含 projects，那裡是對話紀錄與跑過的產物，不是現役設定）、各專案的 memory 筆記、目前目錄
DEFAULT_ROOTS = [{"name": "claude-config", "path": "~/.claude", "maxdepth": 5, "skip_dirs": ["projects"]},
                 {"name": "claude-memory", "path": "~/.claude/projects", "maxdepth": 3, "only": r"/memory/[^/]+\.md$", "optional": True},
                 {"name": "cwd", "path": ".", "maxdepth": 6}]
C, S, Q, CM, N, H = "C", "S", "Q", "#", "N", "H"   # 程式｜字串內容｜引號｜註解｜指令結束的換行｜heredoc 純文字


def mask(s):
    """報告要印出來的片段：遮掉看起來像機密的值、長串英數、網址裡的帳密，控制字元換成問號（免得檔名或內容操縱終端機）。"""
    s = URL_CRED_RE.sub("://…@", s)
    s = SECRET_KV_RE.sub(lambda m: m.group(1) + "…", s)
    return CTRL_RE.sub("?", SECRETISH_RE.sub("…", s))


def norm_path(p, sep=os.sep):
    """比對 only 用：路徑分隔符一律換成 /，同一條規則在各平台都對得到。"""
    return p.replace(sep, "/") if sep != "/" else p


def inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:   # 不同磁碟
        return False


def short_path(p, home):
    return CTRL_RE.sub("?", "~" + p[len(home):] if home and (p == home or p.startswith(home + os.sep)) else p)


def walk(root, maxdepth, only, skip_dirs, follow, info):
    if os.path.isfile(root):
        yield root
        return
    root_real = os.path.realpath(root)
    seen_dirs = {root_real}

    def onerr(_exc):
        info["walk_errors"] += 1

    base = root.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(root, followlinks=follow, onerror=onerr):
        keep = []
        for x in dirs:
            if x in skip_dirs:
                continue
            p = os.path.join(d, x)
            if os.path.islink(p):
                rp = os.path.realpath(p)
                if not follow:
                    if not inside(rp, root_real):   # 指回根目錄裡面的，本尊會被掃到，不算漏
                        info["links_skipped"] += 1
                    continue
                if rp in seen_dirs:
                    continue
                seen_dirs.add(rp)
            keep.append(x)
        dirs[:] = keep
        if d.count(os.sep) - base >= maxdepth:
            dirs[:] = []
        for f in files:
            if os.path.splitext(f)[1].lower() not in TEXT_EXT or f.endswith(SKIP_SUFFIX):
                continue
            p = os.path.join(d, f)
            if only and not only.search(norm_path(p)):
                continue
            if not follow and os.path.islink(p) and not inside(os.path.realpath(p), root_real):
                info["links_skipped"] += 1
                continue
            yield p


def classify(mid, current, legacy):
    base = DATE_SUFFIX_RE.sub("", mid)
    if mid in current or base in current:
        return "current"
    if mid in legacy or base in legacy:
        return "legacy"
    return "unknown"


def plist_to_argv(text):
    """plist 的 <array><string>…</string></array> 換成清單寫法，後面就能用同一套讀法。行數不變。"""
    text = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    text = re.sub(r"<string>(.*?)</string>", lambda m: '"' + m.group(1).replace("\\", "\\\\").replace('"', '\\"') + '",', text)
    return text.replace("<array>", "[").replace("</array>", "]")


def lex(text, fam):
    """逐字元標記。回傳 (cls, depth, regions, unreliable)：
    cls[i] 是 C/S/Q/#/N/H 之一；depth[i] 是括號層數；regions 是 heredoc 內嵌別種語言的區段；
    unreliable 表示引號或 heredoc 沒對上，這個檔的判讀不可靠。"""
    n = len(text)
    cls = [C] * n
    depth = [0] * n
    regions = []
    unreliable = False
    stack = []          # ("s", 引號, 可跨行) ｜ ("(", "sub"/"arr") ｜ ("[",) ｜ ("{",) ｜ ("`",)
    nb = 0
    pending = []        # 這一行宣告的 heredoc：(結束字, 去 tab, 那一行的文字)
    line_start = 0
    opens = BRACKETS[fam]
    triple = fam in ("py", "js")
    i = 0
    while i < n:
        ch = text[i]
        top = stack[-1] if stack else None
        if top is not None and top[0] == "s":
            delim, multi = top[1], top[2]
            depth[i] = nb
            if ch == "\\" and not (fam == "sh" and delim == "'") and i + 1 < n:
                cls[i] = cls[i + 1] = S
                depth[i + 1] = nb
                i += 2
                continue
            if text.startswith(delim, i):
                for k in range(len(delim)):
                    cls[i + k] = Q
                    depth[i + k] = nb
                stack.pop()
                i += len(delim)
                continue
            if fam == "sh" and delim == '"':
                if text.startswith("$(", i):
                    nb += 1
                    stack.append(("(", "sub"))
                    depth[i] = depth[i + 1] = nb
                    i += 2
                    continue
                if ch == "`":
                    nb += 1
                    stack.append(("`",))
                    depth[i] = nb
                    i += 1
                    continue
            if ch == "\n" and not multi:   # 單行字串沒收尾：在行尾結束它，換行交給下面當程式處理
                stack.pop()
                continue
            cls[i] = S
            i += 1
            continue
        depth[i] = nb
        if ch == "\n":
            if fam == "sh":
                ends = not (top is not None and top[0] == "(" and top[1] == "arr")
            else:
                ends = nb == 0
            if ends:
                cls[i] = N
            i += 1
            if pending:
                for delim, dash, cmdline in pending:
                    j, found = i, None
                    while j <= n:
                        e = text.find("\n", j)
                        e = n if e < 0 else e
                        ln = text[j:e]
                        if (ln.lstrip("\t") if dash else ln) == delim:
                            found = (j, e)
                            break
                        if e >= n:
                            break
                        j = e + 1
                    if not found:          # 找不到結束行：後面怎麼讀都不可靠了
                        unreliable = True
                        continue
                    body_end, term_end = found
                    sub = "py" if HEREDOC_PY_RE.search(cmdline) else ("js" if HEREDOC_JS_RE.search(cmdline) else None)
                    if sub:                # 餵給 python／node 的內文：照那個語言讀
                        scls, sdep, _r, sun = lex(text[i:body_end], sub)
                        cls[i:body_end] = scls
                        depth[i:body_end] = [x + nb for x in sdep]
                        regions.append((i, body_end, sub))
                        unreliable = unreliable or sun
                    elif HEREDOC_SH_RE.search(cmdline):   # 餵給 shell／ssh 的內文：就是指令，照常往下讀
                        continue
                    else:                  # 其他（cat、tee、餵給 claude 當 prompt…）：純文字，不判讀
                        for k in range(i, body_end):
                            cls[k] = H
                            depth[k] = nb
                    for k in range(body_end, term_end):
                        cls[k] = H
                        depth[k] = nb
                    i = term_end
                pending = []
            line_start = i
            continue
        if ch == "\\" and i + 1 < n:     # 跳脫；shell 行尾的反斜線就是續行，兩個字元都留在程式層
            depth[i + 1] = nb
            i += 2
            continue
        if fam in ("sh", "cfg", "py") and ch == "#" and (fam == "py" or i == 0 or text[i - 1] in " \t\n;(|&"):
            e = text.find("\n", i)
            e = n if e < 0 else e
            for k in range(i, e):
                cls[k] = CM
                depth[k] = nb
            i = e
            continue
        if fam == "js" and ch == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                e = text.find("\n", i)
                e = n if e < 0 else e
            else:
                e = text.find("*/", i + 2)
                unreliable = unreliable or e < 0
                e = n if e < 0 else e + 2
            for k in range(i, e):
                cls[k] = CM
                depth[k] = nb
            i = e
            continue
        if ch in "\"'" or (ch == "`" and fam == "js"):
            if triple and ch != "`" and text.startswith(ch * 3, i):
                stack.append(("s", ch * 3, True))
                for k in range(3):
                    cls[i + k] = Q
                    depth[i + k] = nb
                i += 3
                continue
            stack.append(("s", ch, fam == "sh" or ch == "`"))
            cls[i] = Q
            i += 1
            continue
        if fam == "sh":
            if ch == "`":
                if top is not None and top[0] == "`":
                    stack.pop()
                    nb -= 1
                else:
                    nb += 1
                    stack.append(("`",))
                    depth[i] = nb
                i += 1
                continue
            if ch == "<" and text.startswith("<<", i):
                if text.startswith("<<<", i):
                    i += 3
                    continue
                arith = len(stack) >= 2 and stack[-1][0] == "(" and stack[-2][0] == "("   # $(( 1 << n )) 是位移不是 heredoc
                m = None if arith else HEREDOC_RE.match(text, i)
                if m:
                    e = text.find("\n", i)
                    pending.append((m.group(3) or m.group(4), bool(m.group(1)), text[line_start:n if e < 0 else e]))
                    i = m.end()
                    continue
        if ch in opens:
            nb += 1
            if ch == "(":
                stack.append(("(", "arr" if fam == "sh" and i > 0 and text[i - 1] == "=" else "sub"))
            else:
                stack.append((ch,))
            depth[i] = nb
            i += 1
            continue
        if ch in CLOSERS and top is not None and top[0] == CLOSERS[ch]:
            stack.pop()
            nb -= 1     # 收尾括號自己還算在裡面那一層（depth[i] 已經是收之前的層數）
        i += 1
    if stack:           # 檔案讀完還有沒收尾的引號、括號或反引號
        unreliable = True
    return cls, depth, regions, unreliable


def visible(text, cls):
    """程式層原樣；沒有空白的字串（"claude"、"-p"、"$BIN"）當成一個字留著；有空白的字串、註解、heredoc 文字遮掉。
    回傳 (看得到的文字, 被遮掉的字串區段清單)。長度與原文相同，位置可以直接對回去。"""
    out = list(text)
    runs = []
    n = len(text)
    i = 0
    while i < n:
        c = cls[i]
        if c == S:
            j = i
            while j < n and cls[j] == S:
                j += 1
            seg = text[i:j]
            if any(x.isspace() for x in seg):
                runs.append((i, j))
                for k in range(i, j):
                    out[k] = "\x00"
            i = j
            continue
        if c == CM:
            out[i] = " "
        elif c == H:
            out[i] = "\x00" if text[i] != "\n" else "\n"
        i += 1
    return "".join(out), runs


def model_of(seg, fam):
    """回傳 (值, 種類)：種類是 literal／variable／unparsed；沒寫 --model 回 (None, None)。"""
    m = MODEL_FLAG_RE.search(seg)
    if not m:
        return ("?", "unparsed") if MODEL_ANY_RE.search(seg) else (None, None)
    val = m.group(1)
    quoted = m.start(1) > 0 and seg[m.start(1) - 1] in "\"'"
    if any(x in val for x in "${}:") or not LITERAL_MODEL_RE.fullmatch(val):
        return val, "variable"
    if fam in ("py", "js") and not quoted:   # 清單寫法裡沒加引號的是變數名
        return val, "variable"
    return val, "literal"


def analyze(text, fam):
    """回傳 (無頭呼叫, 啟動指令, 只是提到的次數)。無頭呼叫每筆帶 status：call（確定）或 uncertain（不確定，附 why）。"""
    if fam == "plist":
        text = plist_to_argv(text)
    cls, depth, regions, unreliable = lex(text, fam)
    vis, runs = visible(text, cls)
    n = len(text)
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    line_of = lambda pos: bisect.bisect_right(starts, pos)

    def fam_at(pos):
        for a, b, f in regions:
            if a <= pos < b:
                return f
        return fam

    def snippet(pos):
        ln = line_of(pos)
        raw = text[starts[ln - 1]:starts[ln] if ln < len(starts) else n]
        return mask(raw.strip())[:110]

    def tail_end(pos):
        d0 = depth[pos - 1]
        f = fam_at(pos - 1)
        limit = min(n, pos + TAIL_CAP)
        i = pos
        while i < limit:
            d = depth[i]
            if d < d0:
                return i, False
            if d == d0:
                c = cls[i]
                if c == N:
                    return i, False
                if c == C:
                    ch = text[i]
                    if ch == ";" and f != "cfg":
                        return i, False
                    if f == "sh" and (ch == "|" or (ch == "&" and text[i - 1] != ">" and not text.startswith(">", i + 1))):
                        return i, False
            i += 1
        return limit, limit < n

    def entry(pos, seg, f, status="call", why=None):
        val, kind = model_of(seg, f)
        return {"line": line_of(pos), "status": status, "why": why, "model": val, "model_kind": kind,
                "effort": bool(EFFORT_FLAG_RE.search(seg)), "effort_later": False, "text": snippet(pos)}

    def later_flags(e, tok_start, end):
        """指令存在變數裡時，往後找 NAME += […]／NAME.extend(…)／NAME+=(…) 有沒有補上 --model、--effort。"""
        back = tok_start
        while back > 0 and tok_start - back < 400 and cls[back - 1] != N:
            back -= 1
        am = ASSIGN_RE.search(vis[back:tok_start])
        if not am:
            return
        grow = re.compile(r"(?<![\w.])" + re.escape(am.group(1)) + r"\s*(?:\+=|\.(?:extend|append|push|insert|unshift)\s*\()[^\n]*")
        for gm in grow.finditer(vis, end, min(n, end + 3000)):
            if e["model_kind"] is None and MODEL_ANY_RE.search(gm.group(0)):
                e["model"], e["model_kind"] = "?", "later"
            if not e["effort"] and EFFORT_FLAG_RE.search(gm.group(0)):
                e["effort_later"] = True

    headless, launchers, covered = [], [], []
    mentions = []
    # 1. 程式層：執行檔與 -p 在同一條指令裡
    last = -1
    for m in TOK_RE.finditer(vis):
        if m.start() < last:
            continue
        end, cut = tail_end(m.end())
        seg = vis[m.end():end]
        if P_RE.search(seg):
            e = entry(m.start(), seg, fam_at(m.start()), "uncertain" if cut else "call", "指令太長或括號沒對上，沒看完" if cut else None)
            later_flags(e, m.start(), end)
            headless.append(e)
            covered.append((m.start(), end))
            last = end
        elif LAUNCH_FLAG_RE.search(seg[:600]):
            e = entry(m.start(), seg, fam_at(m.start()))
            launchers.append({k: e[k] for k in ("line", "model", "model_kind", "effort", "text")})
            covered.append((m.start(), end))
            last = end
    # 2. 字串裡的指令：看字串前面是誰在用它
    for a, b in runs:
        run = text[a:b]
        if "claude" not in run.lower():
            continue
        back = a
        while back > 0 and a - back < 400 and cls[back - 1] != N:
            back -= 1
        head = vis[back:a]
        multiline = "\n" in run
        is_exec = bool(EXEC_HEAD_RE.search(head))
        runs_it = is_exec or fam == "plist" or (fam == "cfg" and bool(CFG_KEY_RE.search(vis[back:a])))
        # python／js 清單裡的一個元素（前面是逗號或左中括號）多半是資料，不是要執行的指令
        in_list = fam_at(a) in ("py", "js") and head.rstrip().rstrip("\"'rfbRFB").rstrip()[-1:] in (",", "[")
        pieces, p0 = [], 0
        for sm in re.finditer(r";|&&|\|\|?|\n", run):
            pieces.append((p0, run[p0:sm.start()]))
            p0 = sm.end()
        pieces.append((p0, run[p0:]))
        for off, piece in pieces:
            tm = next((t for t in TOK_RE.finditer(piece) if CMD_PREFIX_RE.fullmatch(piece[:t.start()])), None)
            if tm is None:
                pm = TOK_RE.search(piece)
                if pm and P_RE.search(piece):
                    mentions.append(line_of(a + off + pm.start()))
                continue
            seg = piece[tm.end():]
            is_p = bool(P_RE.search(seg))
            if not is_p and not LAUNCH_FLAG_RE.search(seg):
                continue
            pos = a + off + tm.start()
            if runs_it:
                e = entry(pos, seg, "sh")
                if is_p:
                    headless.append(e)
                else:
                    launchers.append({k: e[k] for k in ("line", "model", "model_kind", "effort", "text")})
            elif not is_p:
                continue
            elif fam == "cfg":
                headless.append(entry(pos, seg, "sh", "uncertain", "設定檔字串裡的指令，看不出這個欄位會不會被執行"))
            elif multiline or in_list or TALK_HEAD_RE.search(head):
                mentions.append(line_of(pos))
            else:
                headless.append(entry(pos, seg, "sh", "uncertain", "字串裡的指令，看不出有沒有被拿去執行"))
    # 3. 有 -p 也有 claude 才有的旗標，但看不到執行檔名稱
    for m in P_RE.finditer(vis):
        pos = m.start()
        if any(x <= pos < y for x, y in covered):
            continue
        d0 = depth[pos]
        lo = pos
        while lo > 0 and pos - lo < TAIL_CAP and depth[lo - 1] >= d0 and not (depth[lo - 1] == d0 and cls[lo - 1] == N):
            lo -= 1
        hi = pos
        while hi < n and hi - pos < TAIL_CAP and depth[hi] >= d0 and not (depth[hi] == d0 and cls[hi] == N):
            hi += 1
        if any(x < hi and lo < y for x, y in covered):
            continue
        seg = vis[lo:hi]
        f = fam_at(pos)
        before = vis[lo:pos]
        var_exec = bool(VAR_CMD_RE.search(before)) if f == "sh" else (f in ("py", "js", "plist", "cfg") and bool(VAR_ARGV_RE.search(before)))
        if CLAUDE_FLAG_RE.search(seg):
            headless.append(entry(pos, seg, f, "uncertain", "有 -p 與 claude 的旗標，但這一段看不到執行檔名稱"))
            covered.append((lo, hi))
        elif var_exec:
            headless.append(entry(pos, seg, f, "uncertain", "執行檔是變數，名稱看不出是不是 claude"))
            covered.append((lo, hi))
    # 4. 引號沒對上：這個檔的判讀都不可靠，全部降成不確定，再用單行比對補漏
    if unreliable:
        for h in headless:
            if h["status"] == "call":
                h["status"], h["why"] = "uncertain", "這個檔的引號或 heredoc 沒對上，判讀不可靠"
        have = {h["line"] for h in headless}
        for i, ln in enumerate(text.split("\n")):
            if (i + 1) not in have and FALLBACK_RE.search(ln) and not ln.lstrip().startswith("#"):
                val, kind = model_of(ln, "sh")
                headless.append({"line": i + 1, "status": "uncertain", "why": "這個檔的引號或 heredoc 沒對上，只做了單行比對",
                                 "model": val, "model_kind": kind, "effort": bool(EFFORT_FLAG_RE.search(ln)), "effort_later": False,
                                 "text": mask(ln.strip())[:110]})
    headless.sort(key=lambda h: h["line"])
    launchers.sort(key=lambda h: h["line"])
    return headless, launchers, mentions


def family_of(path, data):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".txt"):
        return None
    if ext == "" and data[:2] == b"#!":
        first = data[:120].split(b"\n", 1)[0]
        if b"python" in first:
            return "py"
        if b"node" in first or b"deno" in first or b"bun" in first:
            return "js"
    return FAM.get(ext)


def scan(roots, current, legacy, skip_dirs, follow=False):
    home = os.path.expanduser("~")
    res = {"roots": [], "ids": {}, "stale_in_code": [], "headless": [], "launchers": [], "mentions": [], "doc_mentions": [], "too_large": []}
    seen_real = set()
    for r in roots:
        path = os.path.abspath(os.path.expanduser(r["path"]))
        info = {"name": r["name"], "path": short_path(path, home), "exists": os.path.exists(path), "optional": bool(r.get("optional")), "files": 0, "dup": 0,
                "unreadable": 0, "walk_errors": 0, "too_large": 0, "binary": 0, "decode_replaced": 0, "links_skipped": 0}
        res["roots"].append(info)
        if not info["exists"]:
            continue
        only = re.compile(r["only"]) if r.get("only") else None
        ids = res["ids"].setdefault(r["name"], {})
        for p in walk(path, int(r.get("maxdepth", 8)), only, skip_dirs | set(r.get("skip_dirs", [])), follow, info):
            rp = os.path.realpath(p)
            if rp in seen_real:        # 跟前面的根目錄重疊：這個根目錄有掃到它，但內容只算一次
                info["dup"] += 1
                continue
            try:
                if os.path.getsize(p) > MAX_BYTES:
                    info["too_large"] += 1
                    res["too_large"].append(short_path(p, home))
                    continue
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                info["unreadable"] += 1
                continue
            seen_real.add(rp)
            if b"\x00" in data[:4096]:
                info["binary"] += 1
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
                info["decode_replaced"] += 1
            text = text.replace("\r\n", "\n")   # CRLF 檔：行數不變，續行與 heredoc 結束行才對得上
            info["files"] += 1
            fam = family_of(p, data)
            sp = short_path(p, home)
            if "claude-" in text:
                for i, ln in enumerate(text.split("\n")):
                    for m in ID_RE.findall(ln):
                        mid = m.rstrip(".-")
                        kind = classify(mid, current, legacy)
                        ids.setdefault(kind, {}).setdefault(mid, 0)
                        ids[kind][mid] += 1
                        if fam and kind != "current":
                            res["stale_in_code"].append({"file": sp, "line": i + 1, "id": mid, "kind": kind})
            if not fam:        # .md 與 .txt：文件不判讀，只記哪幾行提到，讓人自己看
                if "claude" in text:
                    res["doc_mentions"] += [{"file": sp, "line": i + 1} for i, ln in enumerate(text.split("\n")) if FALLBACK_RE.search(ln)]
                continue
            if rp == SELF:
                continue
            # 先粗篩，免得每個檔都逐字元讀：有 -p／--print 的都要看（執行檔可能是變數），啟動指令則要有 claude 字樣
            if not (P_RE.search(text) or ("claude" in text.lower() and LAUNCH_FLAG_RE.search(text))):
                continue
            hl, la, mentions = analyze(text, fam)
            res["headless"] += [dict(h, file=sp) for h in hl]
            res["launchers"] += [dict(h, file=sp) for h in la]
            res["mentions"] += [{"file": sp, "line": x} for x in mentions]
    res["bad_roots"] = [r["name"] for r in res["roots"] if not r["optional"] and (not r["exists"] or r["files"] + r["dup"] == 0)]
    res["incomplete_roots"] = [r["name"] for r in res["roots"] if r["unreadable"] or r["walk_errors"]]
    return res


def show_model(h):
    if h["model_kind"] is None:
        return None
    if h["model_kind"] == "literal":
        return h["model"]
    if h["model_kind"] == "later":
        return "（--model 在後面幾行才加上去，要看條件）"
    return "（變數，要看它的來源）" if h["model_kind"] == "variable" else "（--model 的值解析不了，看原始碼）"


def render(res, have_current):
    out = ["== A. 根目錄與檔數"]
    notes = (("dup", "與前面的根目錄重複"), ("unreadable", "🔴 讀不到"), ("walk_errors", "🔴 目錄列不出來"), ("too_large", "🟡 太大略過"),
             ("decode_replaced", "🟡 不是 UTF-8、有字元被替換"), ("links_skipped", "🟡 連結指到根目錄外、沒跟"), ("binary", "二進位略過"))
    for r in res["roots"]:
        mark = "不存在" if not r["exists"] else ("掃到 0 個檔" if r["files"] + r["dup"] == 0 else "")
        mark = mark and (f"（選用，這裡{mark}）" if r["optional"] else "🔴 " + mark)
        extra = "".join(f"（{lab} {r[k]}）" for k, lab in notes if r.get(k))
        out.append(f"   {r['name']:18s} {r['files']:6d} 檔{extra}  {r['path']}  {mark}")
    for pth in res.get("too_large", [])[:8]:
        out.append(f"      太大沒讀：{pth}")
    out.append("\n== B. 各區出現的完整 ID" + ("" if have_current else "（沒給 --current，全部歸在「不認得」或 legacy）"))
    label = {"current": "現役", "legacy": "legacy", "unknown": "不認得"}
    for name, kinds in res["ids"].items():
        if not kinds:
            continue
        out.append(f"   [{name}]")
        for k in ("current", "legacy", "unknown"):
            if kinds.get(k):
                out.append(f"      {label[k]}：" + "、".join(f"{m}×{n}" for m, n in sorted(kinds[k].items(), key=lambda x: -x[1])[:14]))
    out.append(f"\n== C. 程式與設定檔裡的非現役 ID（{len(res['stale_in_code'])} 處；.md 與 .txt 不算）")
    agg = {}
    for s in res["stale_in_code"]:
        a = agg.setdefault(s["file"], {"lines": [], "ids": set()})
        a["lines"].append(s["line"])
        a["ids"].add(("" if s["kind"] == "legacy" else "？") + s["id"])
    for f, a in agg.items():
        ls = ",".join(str(x) for x in a["lines"][:8]) + ("…" if len(a["lines"]) > 8 else "")
        out.append(f"   {f}  行 {ls}  {'、'.join(sorted(a['ids']))}")
    calls = [h for h in res["headless"] if h["status"] == "call"]
    unsure = [h for h in res["headless"] if h["status"] != "call"]
    nomodel = [h for h in calls if h["model_kind"] is None]
    noeff = [h for h in calls if h["model_kind"] is not None and not h["effort"] and not h["effort_later"]]
    later = [h for h in calls if h["model_kind"] == "later" or h["effort_later"]]
    out.append(f"\n== D. 無頭呼叫（claude -p）：確定 {len(calls)} 處；沒寫 --model {len(nomodel)} 處、有 model 沒寫 --effort {len(noeff)} 處、"
               f"旗標在後面才補要看條件 {len(later)} 處；不確定 {len(unsure)} 處")

    def flag(h):
        shown = show_model(h)
        eff = "｜effort 有寫" if h["effort"] else ("｜effort 在後面幾行才加，要看條件" if h["effort_later"] else "｜🟡 effort 沒寫（吃該模型預設）")
        return "🔴 沒寫 model（吃預設）" if shown is None else shown + eff

    for h in calls:
        out.append(f"   {h['file']}:{h['line']}  [{flag(h)}]  {h['text'][:70]}")
    if unsure:
        out.append("   -- 不確定（工具看不出來，請人看）")
        for h in unsure:
            out.append(f"   {h['file']}:{h['line']}  [❓ {h['why']}｜{flag(h)}]  {h['text'][:70]}")
    if res.get("mentions"):
        out.append(f"   （另有 {len(res['mentions'])} 處判成只是訊息、說明文字或資料裡提到 claude -p，沒列；位置在 --json 的 mentions）")
    if res.get("doc_mentions"):
        out.append(f"   （.md 與 .txt 不判讀：另有 {len(res['doc_mentions'])} 行提到 claude -p，位置在 --json 的 doc_mentions；會被照著執行的指令文件要自己看）")
    out.append(f"\n== E. 常駐啟動指令：共 {len(res['launchers'])} 處")
    for h in res["launchers"]:
        out.append(f"   {h['file']}:{h['line']}  [{show_model(h) or '沒寫 model'}{'｜effort 有寫' if h['effort'] else ''}]")
    if not have_current:
        out.append("\n🟡 沒給 --current：分不出哪些 ID 是現役，C 段等於列出全部。先從官方文件抄現役清單再跑一次。")
    if res["bad_roots"]:
        out.append(f"\n🔴 這些根目錄不存在或掃到 0 個檔，結果不可信：{'、'.join(res['bad_roots'])}（先確認路徑）")
    if res["incomplete_roots"]:
        out.append(f"\n🔴 這些根目錄裡有檔案或目錄讀不到，結果不完整：{'、'.join(res['incomplete_roots'])}（看 A 段的數字）")
    return "\n".join(out)


def write_json(path, res):
    """先寫同目錄的暫存檔（建立時就是 0600）再換名：不會留下寫到一半的報告，也不會被別的使用者讀到。"""
    path = os.path.abspath(os.path.expanduser(path))
    fd, tmp = tempfile.mkstemp(prefix=".model-audit-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_roots(path):
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        cfg = json.load(fh)
    roots = cfg["roots"]
    if not isinstance(roots, list) or not roots:
        raise ValueError("roots 要是非空清單")
    names = [r.get("name") for r in roots if isinstance(r, dict)]
    if len(names) != len(roots) or not all(names) or len(set(names)) != len(names) or not all(r.get("path") for r in roots):
        raise ValueError("每個 root 都要有不重複的 name 與 path")
    for r in roots:
        if r.get("only"):
            re.compile(r["only"])
        if not isinstance(r.get("maxdepth", 8), int) or isinstance(r.get("maxdepth", 8), bool) or r.get("maxdepth", 8) < 0:
            raise ValueError(f"{r['name']} 的 maxdepth 要是非負整數")
        rs = r.get("skip_dirs", [])
        if not isinstance(rs, list) or not all(isinstance(x, str) for x in rs):
            raise ValueError(f"{r['name']} 的 skip_dirs 要是字串清單")
    skip = cfg.get("skip_dirs", [])
    if not isinstance(skip, list) or not all(isinstance(x, str) for x in skip):
        raise ValueError("skip_dirs 要是字串清單")
    return roots, set(skip)


def selftest():
    root = tempfile.mkdtemp(prefix="model-audit-selftest-")
    ok = []
    add = lambda name, good: ok.append((name, good))
    try:
        for d in ("runners/a", "docs", "empty", "outside", "linked", "broken"):
            os.makedirs(os.path.join(root, d))

        def w(rel, s):
            with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                fh.write(s)

        # 假機密在執行時才拼出來：原始碼裡不留長得像金鑰的字面，免得金鑰掃描器（含同包的 systems-check）每次都報
        fake = "abcdefghijklm" + "nopqrstuvwxyz" + "0123456789"
        w("runners/a/run.sh", '#!/bin/sh\nOUT="$(claude -p "$PROMPT" \\\n  --model claude-old-1 \\\n  --allowedTools "$T")"\n# claude -p 這行是註解\n')
        w("runners/a/bare.sh", '#!/bin/sh\nclaude -p "hi" --output-format json\n')
        w("runners/a/good.py", 'cmd = ["claude", "-p", prompt, "--model", "claude-new-2", "--effort", "high"]\n')
        w("runners/a/launch.sh", '#!/bin/zsh\nclaude --model claude-new-2-20300101 --effort high --name x --remote-control\n')
        w("runners/a/typo.json", '{"model": "claude-nwe-2"}\n')
        w("runners/a/talk.sh", '#!/bin/sh\necho "claude -p 逾時了"\nlog "會跑 claude -p /usage"\nrun_it || echo "claude -p failed"\n')
        w("runners/a/argv.py", 'argv = base + [\n    "--model", m,\n    "-p", build(x),\n    "--tools", "Read",\n]\n')
        w("runners/a/oldstyle.json", '{"model": "claude-3-5-sample-20200101", "k": "s' + 'k-' + fake + '"}\n')
        w("runners/a/leak.sh", '#!/bin/sh\nTOK' + 'EN=' + fake + ' claude -p "x" --model claude-new-2\n')
        w("runners/a/split.sh", '#!/bin/sh\n"$CLAUDE_BIN" \\\n  -p "$prompt" \\\n  --model "$MODEL"\n')
        w("runners/a/borrow.py", 'import subprocess\nrun(["claude", "-p", prompt])\nrun(["other", "--model", "x", "--effort", "high"])\n')
        w("runners/a/wrapped.py", 'print(subprocess.run(["claude", "-p", prompt]))\n')
        w("runners/a/notclaude.py", 'subprocess.run(["mkdir", "-p", path])\nsubprocess.run(["grep",\n  "-p",\n  pat])\n')
        w("runners/a/assign.sh", '#!/bin/sh\nCMD="claude -p --model claude-old-1"\n')
        w("runners/a/dashc.sh", '#!/bin/sh\nbash -c \'cd /tmp && claude -p hi\'\n')
        w("runners/a/heredoc.sh", "#!/bin/sh\ncat > p.txt <<EOF\nDon't run claude -p here, it's only a prompt.\nEOF\nclaude -p \"$(cat p.txt)\" --model claude-new-2 --effort low\n")
        w("runners/a/pyhere.sh", "#!/bin/sh\npython3 - <<'PY'\nimport subprocess\nsubprocess.run(['claude', '-p', 'x',\n                '--model', 'claude-new-2'])\nPY\n")
        w("runners/a/prompt.py", 'PROMPT = """\nStep 1: run claude -p "summarize" and wait.\n"""\n')
        w("runners/a/concat.py", 'cmd = ["claude", "-p", prompt, "--model=" + model]\n')
        w("runners/a/agent.plist", '<plist><dict><key>ProgramArguments</key><array>\n<string>/usr/local/bin/claude</string>\n<string>-p</string>\n<string>hi</string>\n</array></dict></plist>\n')
        w("runners/a/tmux.sh", '#!/bin/sh\ntmux new-session -d -s main "claude --model claude-new-2 --name main"\n')
        w("runners/a/hook.json", '{"hooks": [{"command": "cd /srv && claude -p \\"nightly\\" --model claude-old-1"}]}\n')
        w("runners/a/later.py", 'cmd = [\n    "claude",\n    "-p", query,\n]\nif model:\n    cmd.extend(["--model", model])\n')
        w("runners/a/later.sh", '#!/bin/sh\nargs=("$CLAUDE_BIN" -p "$x")\nargs+=(--model "$M" --effort high)\n')
        w("runners/a/listdata.py", 'WORDS = ["runner", "headless", "claude -p", "cron"]\n')
        w("runners/a/meta.json", '{"description": "claude -p wrapper for nightly jobs"}\n')
        w("runners/a/varexec.sh", '#!/bin/sh\n"$AI_CLI" -p "$prompt"\nmkdir -p "$OUT"\n"$TMUX_BIN" -L "$SOCK" capture-pane -p -t x\n')
        w("runners/a/varargv.py", 'subprocess.run([cli, "-p", prompt])\n')
        w("runners/a/shift.sh", '#!/bin/sh\nx=$(( 1 << n ))\nclaude -p "$x" --model claude-new-2 --effort low\n')
        w("runners/a/creds.sh", '#!/bin/sh\nAPI_K' + 'EY=ab.cd/ef claude -p "see https://bob' + ':hunter2@example.test/x" --model claude-new-2\n')
        w("docs/howto.md", "跑這行：\n\n    claude -p \"$PROMPT\"\n")
        with open(os.path.join(root, "runners", "a", "crlf.sh"), "wb") as fh:
            fh.write(b'#!/bin/sh\r\nclaude \\\r\n  -p "$x" \\\r\n  --model claude-new-2\r\n')
        w("broken/odd.sh", "#!/bin/sh\nclaude -p \"$x\" --model claude-new-2\necho 'never closed\n")
        w("docs/notes.md", "舊筆記提到 claude-old-1 與 claude-new-2。\n")
        w("outside/secret.sh", '#!/bin/sh\nclaude -p "outside"\n')
        can_link = True
        try:
            os.symlink(os.path.join(root, "outside", "secret.sh"), os.path.join(root, "linked", "out.sh"))
            os.symlink(os.path.join(root, "outside"), os.path.join(root, "linked", "outdir"))
        except (OSError, NotImplementedError, AttributeError):
            can_link = False
        w("linked/in.sh", '#!/bin/sh\nclaude -p "inside" --model claude-new-2 --effort high\n')
        roots = [{"name": "runners", "path": os.path.join(root, "runners"), "maxdepth": 3},
                 {"name": "docs", "path": os.path.join(root, "docs")},
                 {"name": "empty", "path": os.path.join(root, "empty")},
                 {"name": "missing", "path": os.path.join(root, "nope")}]
        res = scan(roots, {"claude-new-2"}, {"claude-old-1"}, SKIP_DIRS)
        by = {}
        for h in res["headless"]:
            by.setdefault(os.path.basename(h["file"]), []).append(h)
        one = lambda f: by[f][0] if len(by.get(f, [])) == 1 else {}
        st = {(os.path.basename(s["file"]), s["id"], s["kind"]) for s in res["stale_in_code"]}
        la = {os.path.basename(h["file"]): h for h in res["launchers"]}
        add("無頭呼叫：跨行的 --model 讀得到、註解行不算", one("run.sh").get("model") == "claude-old-1" and one("run.sh").get("status") == "call")
        add("無頭呼叫：沒寫 model 的標成沒寫", one("bare.sh").get("status") == "call" and one("bare.sh").get("model_kind", 0) is None)
        add("無頭呼叫：清單寫法的 model 與 effort 都讀得到", one("good.py").get("model") == "claude-new-2" and one("good.py").get("model_kind") == "literal" and one("good.py").get("effort") is True)
        add("印訊息、記 log 裡的 claude -p 不算呼叫，另計提到次數", "talk.sh" not in by and len([x for x in res["mentions"] if x["file"].endswith("talk.sh")]) == 3)
        add("看不到執行檔名稱的清單（有 -p 與 --model）列為不確定，model 認成變數", one("argv.py").get("status") == "uncertain" and one("argv.py").get("model_kind") == "variable")
        add("舊式命名（版本在前）的 ID 也抓得到", ("oldstyle.json", "claude-3-5-sample-20200101", "unknown") in st)
        add("報告裡的指令片段遮掉長串英數", fake not in one("leak.sh").get("text", fake))
        add("run.sh 有 model 沒 effort", one("run.sh").get("effort") is False)
        add("常駐啟動指令：帶日期尾碼的現役 ID 算現役", la.get("launch.sh", {}).get("effort") is True and "claude-new-2-20300101" in res["ids"]["runners"].get("current", {}))
        add("程式檔裡的 legacy 被列出", ("run.sh", "claude-old-1", "legacy") in st)
        add("打錯的 ID 歸不認得並列出", ("typo.json", "claude-nwe-2", "unknown") in st)
        add(".md 裡的舊 ID 只計數、不列進 C 段", not any(f == "notes.md" for f, _, _ in st) and res["ids"]["docs"]["legacy"]["claude-old-1"] == 1)
        add("空目錄與不存在的目錄都進 bad_roots", set(res["bad_roots"]) == {"empty", "missing"})
        text = render(res, True)
        add("報告含五段標題、不確定小節、文件提示與紅燈提示", all(k in text for k in ("== A.", "== B.", "== C.", "== D.", "== E.", "不確定（工具看不出來", "doc_mentions", "旗標在後面才補要看條件 2 處", "結果不可信")))
        add("執行檔與 -p 分在兩行（反斜線續行）也算同一條指令，變數 model 標成變數", one("split.sh").get("status") == "call" and one("split.sh").get("model_kind") == "variable")
        add("旗標不借隔壁指令的：前一條沒寫 model 就是沒寫", one("borrow.py").get("status") == "call" and one("borrow.py").get("model_kind", 0) is None and one("borrow.py").get("effort") is False)
        add("包在 print(...) 裡的真呼叫照樣算", one("wrapped.py").get("status") == "call")
        add("mkdir -p、grep -p 這類清單不算、也不列不確定", "notclaude.py" not in by)
        add("字串賦值裡的指令列為不確定，不算確定也不當沒事", one("assign.sh").get("status") == "uncertain" and one("assign.sh").get("model") == "claude-old-1")
        add("bash -c '…' 裡的呼叫算確定", one("dashc.sh").get("status") == "call" and one("dashc.sh").get("model_kind", 0) is None)
        add("heredoc 純文字（含撇號）不判讀，後面的真呼叫照樣讀到", one("heredoc.sh").get("status") == "call" and one("heredoc.sh").get("line") == 5 and one("heredoc.sh").get("effort") is True)
        add("餵給 python 的 heredoc 照 python 讀，跨行清單的 model 讀得到", one("pyhere.sh").get("status") == "call" and one("pyhere.sh").get("model") == "claude-new-2")
        add("多行 prompt 字串裡提到 claude -p 不算呼叫", "prompt.py" not in by)
        add("--model 的值解析不了時標成解析不了，不當成沒寫", one("concat.py").get("model_kind") == "unparsed")
        add("plist 的 ProgramArguments 認得到", one("agent.plist").get("status") == "call" and one("agent.plist").get("line") == 2)
        add("tmux 字串裡的啟動指令列進 E 段", la.get("tmux.sh", {}).get("model") == "claude-new-2")
        add("設定檔字串裡的指令算確定，model 讀得到", one("hook.json").get("status") == "call" and one("hook.json").get("model") == "claude-old-1")
        add("旗標在後面幾行才補（extend、+=）：標成後面才加，不算沒寫", one("later.py").get("model_kind") == "later" and one("later.sh").get("model_kind") == "later" and one("later.sh").get("effort_later") is True)
        add("python 清單裡的字串元素當資料，不列", "listdata.py" not in by and any(x["file"].endswith("listdata.py") for x in res["mentions"]))
        add("設定檔裡不是指令欄位的字串列為不確定", one("meta.json").get("status") == "uncertain")
        add("執行檔是變數（名稱看不出是不是 claude）加 -p：列為不確定；mkdir -p 與帶子命令的不列", [h.get("status") for h in by.get("varexec.sh", [])] == ["uncertain"] and one("varexec.sh").get("line") == 2 and one("varargv.py").get("status") == "uncertain")
        add("$(( 1 << n )) 不當成 heredoc，後面的呼叫照樣確定", one("shift.sh").get("status") == "call" and one("shift.sh").get("effort") is True)
        add("片段遮掉鍵值型機密與網址帳密", "ab.cd/ef" not in one("creds.sh").get("text", "ab.cd/ef") and "hunter2" not in one("creds.sh").get("text", "hunter2"))
        add(".md 不判讀但記下提到的行", [x["line"] for x in res["doc_mentions"] if x["file"].endswith("howto.md")] == [3] and "howto.md" not in by)
        add("控制字元不會原樣進報告", "\x1b" not in mask("a\x1b[31mb") and short_path("/x/a\x1b[2Jb", "/home/z") == "/x/a?[2Jb")
        add("CRLF 換行的檔：反斜線續行照樣組成一條指令", one("crlf.sh").get("status") == "call" and one("crlf.sh").get("model") == "claude-new-2" and one("crlf.sh").get("line") == 2)
        optres = scan([{"name": "opt", "path": os.path.join(root, "nope"), "optional": True}, roots[1]], set(), set(), SKIP_DIRS)
        add("選用的根目錄不存在不算錯", not optres["bad_roots"])
        skipres = scan([{"name": "r", "path": os.path.join(root, "runners"), "skip_dirs": ["a"]}, roots[1]], set(), set(), SKIP_DIRS)
        add("單一根目錄的 skip_dirs 有效（整個子目錄沒掃）", skipres["roots"][0]["files"] == 0 and not skipres["headless"])
        res2 = scan(roots[:2], set(), set(), SKIP_DIRS)
        add("沒給清單時全部歸不認得", not res2["ids"]["runners"].get("current") and "claude-new-2" in res2["ids"]["runners"]["unknown"] and not res2["bad_roots"])
        res3 = scan([{"name": "broken", "path": os.path.join(root, "broken")}], {"claude-new-2"}, set(), SKIP_DIRS)
        add("引號沒對上的檔：找到的呼叫全部降成不確定", len(res3["headless"]) == 1 and res3["headless"][0]["status"] == "uncertain")
        res4 = scan([{"name": "all", "path": os.path.join(root, "runners")}, {"name": "again", "path": os.path.join(root, "runners", "a")}], set(), set(), SKIP_DIRS)
        add("重疊的根目錄：後面那個不算 0 個檔，內容只算一次", not res4["bad_roots"] and res4["roots"][1]["files"] == 0 and res4["roots"][1]["dup"] > 0
            and len([h for h in res4["headless"] if h["file"].endswith("bare.sh")]) == 1)
        if can_link:
            res5 = scan([{"name": "linked", "path": os.path.join(root, "linked")}], {"claude-new-2"}, set(), SKIP_DIRS)
            f5 = {os.path.basename(h["file"]) for h in res5["headless"]}
            add("指到根目錄外的檔案與目錄連結預設不跟、有計數", f5 == {"in.sh"} and res5["roots"][0]["links_skipped"] == 2)
            res6 = scan([{"name": "linked", "path": os.path.join(root, "linked")}], {"claude-new-2"}, set(), SKIP_DIRS, follow=True)
            add("--follow-links 會跟過去讀，同一個檔不重複算", len([h for h in res6["headless"] if "outside" in h["text"]]) == 1 and res6["roots"][0]["links_skipped"] == 0)
        else:
            print("[SKIP] 這個環境建不了符號連結，兩條連結測試沒跑")
        out_json = os.path.join(root, "report.json")
        write_json(out_json, res)
        with open(out_json, encoding="utf-8") as fh:
            back = json.load(fh)
        mode_ok = os.name != "posix" or (os.stat(out_json).st_mode & 0o777) == 0o600
        add("--json：內容讀得回來、沒留暫存檔、權限 0600（非 POSIX 只驗前兩項）", back["bad_roots"] == res["bad_roots"] and mode_ok
            and not [f for f in os.listdir(root) if f.startswith(".model-audit-")])
        try:
            write_json(os.path.join(root, "no-such-dir", "x.json"), res)
            add("--json 寫不出去會丟錯，不會默默成功", False)
        except OSError:
            add("--json 寫不出去會丟錯，不會默默成功", True)
        add("only 比對前把路徑分隔符換成 /（Windows 路徑也對得到）", re.search(r"/models/[^/]+\.md$", norm_path("C:\\notes\\models\\a.md", "\\")) is not None)
        hp = lambda *parts: os.sep + os.sep.join(parts)
        add("家目錄縮寫不會把 /home/al 套到 /home/alice", short_path(hp("home", "alice", "x"), hp("home", "al")) == hp("home", "alice", "x") and short_path(hp("home", "al", "x"), hp("home", "al")) == "~" + os.sep + "x")
        bad_cfg = os.path.join(root, "bad-roots.json")
        w("bad-roots.json", '{"roots": [{"name": "a", "path": "x"}, {"name": "a", "path": "y"}]}')
        try:
            load_roots(bad_cfg)
            add("roots 檔名稱重複會被拒絕", False)
        except ValueError:
            add("roots 檔名稱重複會被拒絕", True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    add("自測用的暫存夾清掉了", not os.path.exists(root))
    for name, good in ok:
        print(f"[{'PASS' if good else 'FAIL'}] {name}")
    bad = [n for n, g in ok if not g]
    print(f"selftest {len(ok) - len(bad)}/{len(ok)}")
    return 1 if bad else 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):   # 終端機不是 UTF-8 時（常見於 Windows）別讓中文與符號炸在編碼上
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(add_help=True, description="換模型時的盤點掃描器（不改被掃的檔）。完整說明見檔頭。")
    ap.add_argument("--current", default="")
    ap.add_argument("--legacy", default="")
    ap.add_argument("--roots-file")
    ap.add_argument("--json")
    ap.add_argument("--follow-links", action="store_true")
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    roots, skip = DEFAULT_ROOTS, set(SKIP_DIRS)
    if a.roots_file:
        try:
            roots, extra = load_roots(a.roots_file)
            skip |= extra
        except Exception as exc:  # 設定檔讀不了就不掃，別退回預設值假裝掃過
            print(f"🔴 roots 檔讀不了或格式不對：{exc}", file=sys.stderr)
            return 2
    split = lambda s: {x.strip() for x in s.split(",") if x.strip()}
    current, legacy = split(a.current), split(a.legacy)
    res = scan(roots, current, legacy, skip, a.follow_links)
    if a.no_text:
        for h in res["headless"] + res["launchers"]:
            h["text"] = ""
    print(render(res, bool(current)))
    rc = 3 if res["bad_roots"] or res["incomplete_roots"] else 0
    if a.json:
        try:
            write_json(a.json, res)
        except OSError as exc:
            print(f"🔴 --json 寫不出去：{exc}（掃描有跑完，文字報告在上面）", file=sys.stderr)
            return rc or 4
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
