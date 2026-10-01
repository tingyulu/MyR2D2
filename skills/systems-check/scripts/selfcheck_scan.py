#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck_scan.py — systems-check 掃描器。

python3.9 stdlib、零第三方依賴、不修改被掃來源。
只產機械層事實與候選；判讀與後續處置由 SKILL.md 的模型端流程負責。

硬規則：
- 所有讀檔一律走 safe_open／safe_open_bytes／safe_lines／safe_stat（唯一的 open 點在 _open_checked）。
- 所有產出一律走 emit／emit_jsonl／emit_text（access.log 例外走 emit_raw）。
- 掃描器不做任何外部副作用呼叫（不開卡、不上傳、不發通知、不連網）；
  允許的本機唯讀子程序只有 git ls-files、git rev-parse、launchctl print。
"""
from __future__ import annotations

import argparse
import ast
import glob as globmod
import hashlib
import io
import json
import os
import plistlib
import re
import shlex
import tokenize
from urllib.parse import unquote
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "1.0.0"

# --------------------------------------------------------------------------
# 全域狀態（每次掃描前 reset_state()）
# --------------------------------------------------------------------------
_ACCESS = []          # access.log 的行
_EMITTED = []         # 本次寫出的產出檔
GLOBAL_LAYER = [False]  # --global 旗標；manifest 的 global_layer 亦可開啟


def human_owner() -> str:
    """需要人來判斷的候選由誰負責。可用 SELFCHECK_HUMAN_OWNER 覆寫。"""
    return os.environ.get("SELFCHECK_HUMAN_OWNER") or "使用者"
_KNOWN_NAMES = set()  # 本次盤點到的自家名稱（memory stem／skill 目錄／bin／hook 檔名），遮罩時原樣保留


def reset_state():
    GLOBAL_LAYER[0] = False
    del _ACCESS[:]
    del _EMITTED[:]
    _KNOWN_NAMES.clear()


class ScopeError(Exception):
    pass


class ManifestError(Exception):
    pass


# --------------------------------------------------------------------------
# 環境
# --------------------------------------------------------------------------
def home() -> Path:
    v = os.environ.get("SELFCHECK_HOME")
    if v:
        return Path(v).expanduser().resolve()
    return Path.home().resolve()


def now() -> datetime:
    v = os.environ.get("SELFCHECK_NOW")
    if v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return datetime.now()


def slug_of(path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _under(p: Path, root: Path) -> bool:
    """p 等於 root 或在 root 底下（realpath 祖先關係，不用字串 startswith）。"""
    try:
        return p == root or p.is_relative_to(root)
    except AttributeError:  # pragma: no cover - py<3.9
        return p == root or str(p).startswith(str(root) + os.sep)
    except Exception:
        return False


def expand_tilde(s: str, h: Path) -> Path:
    s = str(s)
    if s == "~":
        return h
    if s.startswith("~/"):
        return h / s[2:]
    return Path(s)


# --------------------------------------------------------------------------
# 資料界線
# --------------------------------------------------------------------------
_RE_BEARER = re.compile(r"Bearer\s+([A-Za-z0-9\-_\.]{8,})")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 電話遮罩：E.164 國際格式（+ 開頭 8〜15 碼）＋一組在地範例（台灣門號）；其他在地寫法不遮，見 SKILL 已知限制
_RE_TWPHONE = re.compile(r"(?:\+886[\-\s]?\d{1,2}[\-\s]?\d{3,4}[\-\s]?\d{3,4}|\b09\d{8}\b|\+\d{8,15}\b)")
_RE_URL = re.compile(r"https?://[^\s'\"<>)\]]+")
_RE_LONGTOK = re.compile(r"[A-Za-z0-9_\-]{20,}")


def _mask_if_secretlike(m):
    """長字串只在「像密鑰」時才遮——≥3 個數字，或 ≥40 字且含數字；純字母加連字號的識別字（log_glob_in_claude_projects、token-optimizer-skill）不遮。"""
    tok = m.group(0)
    if re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tok):
        return tok  # 32 hex 的頁面 id／UUID 是引用證據不是密鑰，遮了就無法回溯
    digits = sum(c.isdigit() for c in tok)
    if digits >= 3 or (len(tok) >= 40 and digits >= 1):
        return mask_value(tok)
    return tok


def mask_value(tok: str) -> str:
    """前 4 字＋…＋長度。原值永不出現。"""
    tok = str(tok)
    return "%s…(%d)" % (tok[:4], len(tok))


def _strip_query(m):
    u = m.group(0)
    i = u.find("?")
    return (u[:i] + "?…") if i >= 0 else u


_RE_URLUSER = re.compile(r"://[^/\s@]+@")  # URL userinfo（user:pass@）
_PATHISH = re.compile(r"^[~/]|/")


_RE_NAMEISH = re.compile(r"[A-Za-z0-9_\-\.]{4,}")
_KNOWN_PREFIXES = ("memory:", "skill:", "bin:", "hook:")


def _redact_keep_known(tok: str) -> str:
    """token 內若出現本次盤點到的已知名稱就原樣保留，其餘片段照舊遮。

    邊界：token 含 `://`（URL，query 要整段剝）或名稱緊鄰 `@`（email 局部）時整段照舊遮，不走保留路徑。
    """
    if not _KNOWN_NAMES or "://" in tok:
        return redact(tok)
    spans = []
    for m in _RE_NAMEISH.finditer(tok):
        prev = tok[m.start() - 1] if m.start() else ""
        nxt = tok[m.end()] if m.end() < len(tok) else ""
        if prev == "@" or nxt == "@":
            continue
        core = m.group(0)
        for pre in _KNOWN_PREFIXES:
            if core.startswith(pre):
                core = core[len(pre):]
        core = core.rstrip(_TRAIL)
        if core and core in _KNOWN_NAMES:
            spans.append((m.start(), m.end()))
    if not spans:
        return redact(tok)
    out = []
    last = 0
    for s, e in spans:
        out.append(redact(tok[last:s]))
        out.append(tok[s:e])
        last = e
    out.append(redact(tok[last:]))
    return "".join(out)


def redact_text(text):
    """自由文字欄位（title／proposal／reason／sig／program_args…）：逐 token 判斷，像路徑的用 redact_path，其餘用 redact。"""
    if not isinstance(text, str) or not text:
        return text
    out = []
    for tok in re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
        elif _PATHISH.search(tok) and not re.search(r"[=:@]", tok):
            out.append(redact_path(tok))
        else:
            out.append(_redact_keep_known(tok))
    return "".join(out)


def redact(text):
    if not isinstance(text, str) or not text:
        return text
    t = _RE_URLUSER.sub("://…@", text)
    t = _RE_BEARER.sub(lambda m: "Bearer " + mask_value(m.group(1)), t)
    t = _RE_EMAIL.sub(lambda m: mask_value(m.group(0)), t)
    t = _RE_TWPHONE.sub("09…(遮罩)", t)
    t = _RE_URL.sub(_strip_query, t)
    t = _RE_LONGTOK.sub(_mask_if_secretlike, t)
    return t


def _mtime_or_zero(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def redact_path(text):
    """路徑類欄位：仍遮 Bearer／email／電話／URL query，但不遮長字串（路徑不是祕密）。"""
    if not isinstance(text, str) or not text:
        return text
    t = _RE_URLUSER.sub("://…@", text)
    t = _RE_BEARER.sub(lambda m: "Bearer " + mask_value(m.group(1)), t)
    t = _RE_EMAIL.sub(lambda m: mask_value(m.group(0)), t)
    t = _RE_TWPHONE.sub("09…(遮罩)", t)
    t = _RE_URL.sub(_strip_query, t)
    return t


PATH_KEYS = frozenset([
    "path", "file", "files", "path_rel", "ref", "out_dir", "stdout", "stderr",
    "target", "source_file",  # inject／import 列的路徑欄位
    "log_paths", "allowed_roots", "allowed_files", "reference_files", "list_only_dirs",
    "project_root", "memory_dir", "transcript_dir", "launch_agents", "code_roots",
    "topic_files", "log_globs", "exclude", "slug", "run_id",
])
TEXT_KEYS = frozenset([  # 自由文字欄位走 redact_text（token 級：像路徑的保留、其餘照遮）
    "title", "proposal", "reason", "always_on_reason", "label", "runner", "runner_labels", "name", "sig", "program_args",
])


def clip(s: str, n: int) -> str:
    if not isinstance(s, str):
        return s
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
@dataclass
class Scope:
    mode: str
    project_root: Path
    slug: str
    memory_dir: Path
    transcript_dir: Path
    allowed_roots: list
    reference_files: list
    manifest: dict
    refused: list
    slug_collisions: list
    home: Path
    launch_agents: Path
    allowed_files: list = field(default_factory=list)   # 個別檔案白名單（reference／log_globs／plist 宣告）
    list_only_dirs: list = field(default_factory=list)  # 只准列名不准讀內容
    incomplete: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # 不影響 status 的掃描備註（例：姊妹根 symlink 未列名）
    global_imports: list = field(default_factory=list)  # 全局 CLAUDE.md 用 @ 匯入、且住在 ~/.claude 底下的檔（跟它一樣每個 session 開場載入）
    ctx_files: list = field(default_factory=list)   # inject／import 目標（不進 log 自動撿拾）
    owner_project: str = ""


def rel_home(scope: Scope, p) -> str:
    p = Path(p)
    try:
        return "~/" + str(p.relative_to(scope.home))
    except ValueError:
        return str(p)


def record_refused(scope: Scope, path, reason: str):
    entry = {"path": rel_home(scope, path), "reason": reason}
    if entry not in scope.refused:
        scope.refused.append(entry)


def record_incomplete(scope: Scope, section: str, reason: str):
    entry = {"section": section, "reason": reason}
    if entry not in scope.incomplete:
        scope.incomplete.append(entry)


def _hard_denied(scope: Scope, rp: Path):
    """回 reason 字串代表硬禁區（連 stat 都不做，或只准列名）。"""
    c = scope.home / ".claude"
    projects = c / "projects"
    if rp == c / ".credentials.json":
        return "credentials_stat_only"
    if rp.parent == c and rp.suffix == ".jsonl":
        return "claude_root_jsonl"
    if rp == projects:
        return None if rp in scope.list_only_dirs else "claude_projects_root"
    if _under(rp, projects):
        td = scope.transcript_dir
        if not _under(rp, td):
            return "other_project"
    return None


def in_scope(scope: Scope, path) -> bool:
    try:
        rp = Path(path).resolve()
    except OSError:
        return False
    if _hard_denied(scope, rp):
        return False
    for f in scope.allowed_files:
        if rp == f:
            return True
    for r in scope.allowed_roots:
        if _under(rp, r):
            return True
    return False


def _stat_only(scope: Scope, rp: Path):
    """只准 stat、不准讀內容的檔。"""
    if rp == scope.home / ".claude" / ".credentials.json":
        return "credentials"
    if _under(rp, scope.home / ".config"):
        return "config_dir"
    if _is_cred_name(rp.name):
        return "sensitive_filename"
    return None


# --------------------------------------------------------------------------
# 唯一的讀檔入口
# --------------------------------------------------------------------------
def _log_access(kind: str, path: Path, nbytes):
    _ACCESS.append("%s\t%s\t%s\t%s" % (now().isoformat(timespec="seconds"), kind, str(path), nbytes))


def _open_checked(scope: Scope, rp: Path, mode: str):
    """本檔唯一呼叫內建 open 的地方。"""
    return open(rp, mode)  # noqa: SIM115


def _guard(scope: Scope, path, need_content=True):
    p = Path(path)
    try:
        rp = p.resolve()
    except (OSError, RuntimeError):  # symlink 迴圈是 RuntimeError，原本只接 OSError 會讓整支掃描器當掉
        record_refused(scope, p, "resolve_error")
        return None
    if not in_scope(scope, rp):
        record_refused(scope, p, "out_of_scope")
        return None
    if not rp.exists():
        return None
    if need_content:
        so = _stat_only(scope, rp)
        if so:
            record_refused(scope, p, "stat_only:" + so)
            return None
    return rp


def safe_open_bytes(scope: Scope, path, max_bytes=None, tail=False):
    rp = _guard(scope, path)
    if rp is None:
        return None
    try:
        size = rp.stat().st_size
    except OSError:
        record_refused(scope, path, "stat_error")
        return None
    try:
        with _open_checked(scope, rp, "rb") as f:
            if tail and max_bytes and size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丟掉可能被切一半的第一行
                data = f.read()
            elif max_bytes:
                data = f.read(max_bytes)
            else:
                data = f.read()
    except OSError:
        record_refused(scope, path, "read_error")
        return None
    _log_access("read", rp, len(data))
    return data


def safe_open(scope: Scope, path, max_bytes=None, tail=False):
    data = safe_open_bytes(scope, path, max_bytes=max_bytes, tail=tail)
    if data is None:
        return None
    if b"\x00" in data[:4096]:
        record_refused(scope, path, "decode:binary")
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if max_bytes and len(data) >= max_bytes:
            return data.decode("utf-8", errors="ignore")  # 截斷讀取切在多位元組字元中間不算壞檔
        record_refused(scope, path, "decode:utf8")
        return None


def safe_lines(scope: Scope, path, max_bytes=None, tail=False):
    """串流讀行（大檔用）。回 None 代表被擋；否則回 list[str]。"""
    data = safe_open_bytes(scope, path, max_bytes=max_bytes, tail=tail)
    if data is None:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        record_refused(scope, path, "decode:utf8")
        return None
    return text.splitlines()


def safe_stat(scope: Scope, path):
    p = Path(path)
    try:
        rp = p.resolve()
    except (OSError, RuntimeError):  # symlink 迴圈是 RuntimeError，原本只接 OSError 會讓整支掃描器當掉
        record_refused(scope, p, "resolve_error")
        return None
    if _hard_denied(scope, rp):
        record_refused(scope, p, "out_of_scope")
        return None
    ok = in_scope(scope, rp) or _under(rp, scope.home / ".config") or rp == scope.home / ".claude" / ".credentials.json"
    if not ok:
        record_refused(scope, p, "out_of_scope")
        return None
    try:
        st = rp.stat()
    except OSError:
        record_refused(scope, p, "stat_error")
        return None
    _log_access("stat", rp, st.st_size)
    return st


# --------------------------------------------------------------------------
# 中央輸出
# --------------------------------------------------------------------------
def _sanitize(obj, maxlen, mode=None):
    if isinstance(obj, str):
        fn = redact_path if mode == "path" else (redact_text if mode == "text" else redact)
        return clip(fn(obj), maxlen)
    if isinstance(obj, dict):
        return dict((k, _sanitize(v, maxlen, mode or ("path" if k in PATH_KEYS else ("text" if k in TEXT_KEYS else None))))
                    for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, maxlen, mode) for v in obj]
    return obj


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:  # noqa: SIM115
        f.write(text)
    _EMITTED.append(path)
    _log_access("write", path, len(text.encode("utf-8")))


def emit(path: Path, obj, maxlen=200):
    _write(path, json.dumps(_sanitize(obj, maxlen), ensure_ascii=False, indent=2) + "\n")


def emit_jsonl(path: Path, rows, maxlen=200):
    out = []
    for row in rows:
        out.append(json.dumps(_sanitize(row, maxlen), ensure_ascii=False))
    _write(path, ("\n".join(out) + "\n") if out else "")


def emit_text(path: Path, text: str):
    _write(path, redact(text))


def emit_raw(path: Path, text: str):
    """access.log 專用：路徑不遮罩，但永不含檔案內容。"""
    _write(path, text)


# --------------------------------------------------------------------------
# 範圍解析
# --------------------------------------------------------------------------
def find_project_root(cwd: Path, h: Path):
    """往上找含 CLAUDE.md 或 .claude/ 的目錄；到 home 或 / 停；home 本身與 ~/.claude 不算。"""
    claude_dir = h / ".claude"
    try:
        cur = Path(cwd).resolve()
    except OSError:
        cur = Path(cwd)
    while True:
        if cur == h or _under(cur, claude_dir):
            return None
        if (cur / "CLAUDE.md").exists() or (cur / ".claude").is_dir():
            return cur
        nxt = cur.parent
        if nxt == cur:
            return None
        cur = nxt


def _validate_manifest_root(raw: str, h: Path, project_root: Path = None) -> Path:
    if ".." in Path(str(raw)).parts:
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）含 .. 片段：%s" % raw)
    p = expand_tilde(raw, h)
    if not p.is_absolute() and project_root is not None:
        p = project_root / p  # 相對路徑對專案根解析，不對 cwd
    if not p.exists():
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）不存在：%s" % raw)
    rp = p.resolve()
    if not _under(rp, h):
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）不在 ~ 底下：%s" % raw)
    if rp == h:
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）不得是 ~ 本身：%s" % raw)
    if rp == h / ".claude":
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）不得是 ~/.claude：%s" % raw)
    if _under(rp, h / ".claude" / "projects"):
        raise ManifestError("manifest 路徑（code_roots／sibling_roots）不得落在 ~/.claude/projects/：%s" % raw)
    return rp


def _log_file_violation(rp: Path, h: Path):
    """log_globs 展開後的單檔檢查；回 reason 或 None。允許 /tmp、/private/tmp 第一層。"""
    tmp_ok = rp.parent in (Path("/tmp"), Path("/private/tmp"))  # 只豁免 /tmp 第一層的檔（如 /tmp/<某工具>.log），且只豁免「必須在 ~ 底下」這一條
    if not tmp_ok and not _under(rp, h):
        return "log_glob_not_under_home"
    if rp == h / ".claude":
        return "log_glob_is_claude_dir"
    if _under(rp, h / ".claude" / "projects"):
        return "log_glob_in_claude_projects"
    return None


def _audit_table_raw(manifest: dict):
    out = []
    for a in (manifest.get("audits") or []):
        out.append((a.get("name") or "", a.get("path") or None,
                    a.get("cycle") or "日", int(a.get("stale_days") or 2)))
    return out


def load_manifest(project_root: Path, h: Path) -> dict:
    path = project_root / ".claude" / "systems-check.json"
    try:
        if not _under(path.resolve(), project_root.resolve()):
            raise ManifestError("manifest 經 symlink 逃出專案根：%s" % path)
    except OSError:
        raise ManifestError("manifest 路徑解析失敗：%s" % path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:  # noqa: SIM115
            raw = json.load(f)
    except Exception as exc:
        raise ManifestError("manifest 解析失敗：%s" % type(exc).__name__)
    if not isinstance(raw, dict):
        raise ManifestError("manifest 不是 JSON 物件")
    _log_access("read", path.resolve(), path.stat().st_size)
    def _strlist(key):  # 型別驗證，字串會被 list() 拆成單字元、其他型別會炸未包裝例外
        v = raw.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ManifestError("manifest 欄位 %s 必須是字串陣列" % key)
        return list(v)
    tpj = raw.get("owner_project") or ""
    if not isinstance(tpj, str):
        raise ManifestError("manifest 欄位 owner_project 必須是字串")
    sa = raw.get("security_allow") or []
    if not isinstance(sa, list) or not all(isinstance(x, dict) for x in sa):
        raise ManifestError("manifest 欄位 security_allow 必須是物件陣列")
    bad = [x for x in sa if not (str(x.get("file") or "") or str(x.get("pattern") or ""))]
    if bad:
        raise ManifestError("manifest security_allow 有 %d 條沒有 file 也沒有 pattern（會放行一切，拒絕）" % len(bad))
    for x in sa:  # 內容錨與說明欄位只驗型別，不驗語意
        for k in ("snippet_contains", "reason", "by", "at"):
            if k in x and not isinstance(x[k], str):
                raise ManifestError("manifest security_allow 的 %s 必須是字串" % k)
        if "snippet_contains" in x and not str(x["snippet_contains"]).strip():
            raise ManifestError("manifest security_allow 的 snippet_contains 不可為空字串（等同沒錨卻看起來有錨）")
    mn = _strlist("machine_names")
    au = raw.get("audits") or []
    if not isinstance(au, list) or not all(isinstance(x, dict) for x in au):
        raise ManifestError("manifest 欄位 audits 必須是物件陣列")
    gl = raw.get("global_layer", False)
    if not isinstance(gl, bool):
        raise ManifestError("manifest 欄位 global_layer 必須是 true／false")
    tp = raw.get("topics") or {}
    if not isinstance(tp, dict) or not all(isinstance(k, str) and isinstance(v, list) for k, v in tp.items()):
        raise ManifestError("manifest 欄位 topics 必須是 {主題: [關鍵字]}")
    out = {
        "owner_project": tpj,
        "global_layer": gl,
        "audits": list(au),
        "machine_names": mn,
        "code_roots": [],
        "code_roots_raw": _strlist("code_roots"),
        "sibling_roots": [],
        "sibling_roots_raw": _strlist("sibling_roots"),
        "log_globs": _strlist("log_globs"),
        "exclude": _strlist("exclude"),
        "security_allow": list(sa),
        "topics": dict(tp),
    }
    for r in out["code_roots_raw"]:
        out["code_roots"].append(_validate_manifest_root(r, h, project_root))
    for r in out["sibling_roots_raw"]:
        out["sibling_roots"].append(_validate_manifest_root(r, h, project_root))
    return out


def _expand_log_globs(scope: Scope):
    h = scope.home
    files = []
    for pat in scope.manifest.get("log_globs") or []:
        raw = str(pat)
        if ".." in Path(raw).parts:
            record_refused(scope, raw, "log_glob_dotdot")
            continue
        expanded_p = expand_tilde(raw, h)
        if not expanded_p.is_absolute():
            expanded_p = scope.project_root / expanded_p  # 相對 glob 對專案根
        expanded = str(expanded_p)
        # glob 之前先驗 pattern——只准 ~ 底下，或 /tmp、/private/tmp 第一層；禁 **。不然 glob 本身就是範圍外的存在性探測
        pat_ok = expanded.startswith(str(h) + "/") or Path(expanded).parent in (Path("/tmp"), Path("/private/tmp"))
        if not pat_ok or "**" in expanded:
            record_refused(scope, raw, "log_glob_pattern_out_of_bounds")
            continue
        hits = [Path(x) for x in sorted(globmod.glob(expanded))]
        if not hits:
            record_incomplete(scope, "logs", "manifest log_glob 0 命中：%s" % raw)
            continue
        for p in hits:
            try:
                rp = p.resolve()
            except OSError:
                record_refused(scope, p, "resolve_error")
                continue
            why = _log_file_violation(rp, h)
            if why:
                record_refused(scope, p, why)
                continue
            if not rp.is_file():
                continue
            files.append(rp)
    return files


def _slug_collisions(scope: Scope):
    parent = scope.project_root.parent
    out = []
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return out
    for n in names:
        d = parent / n
        if d == scope.project_root:
            continue
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        if slug_of(d) == scope.slug:
            out.append(n)
    return out


def resolve_scope(cwd) -> Scope:
    h = home()
    root = find_project_root(Path(cwd), h)
    if root is None:
        raise ScopeError("範圍解析失敗：從 %s 往上找不到專案根（home 本身與 ~/.claude 底下不算）" % cwd)
    manifest = load_manifest(root, h)
    # 全局層模式預設關；要開就在 manifest 寫 {"global_layer": true} 或帶 --global。
    mode = "global" if (manifest.get("global_layer") or GLOBAL_LAYER[0]) else "project"
    slug = slug_of(root)
    tdir = h / ".claude" / "projects" / slug
    mdir = tdir / "memory"
    la_env = os.environ.get("SELFCHECK_LAUNCHAGENTS")
    la = Path(la_env) if la_env else (h / "Library" / "LaunchAgents")
    try:
        la = la.resolve()
    except OSError:
        pass

    c = h / ".claude"
    allowed = [root, tdir, la]
    allowed.extend(manifest.get("code_roots") or [])
    reference = []
    list_only = []
    allowed_extra_files = []
    if mode == "global":
        allowed.extend([
            c / "CLAUDE.md", c / "skills", c / "hooks", c / "bin",
            c / "settings.json", c / ".gitignore",
        ])
        try:
            allowed.extend(sorted(c.glob("*.sh")))
        except OSError:
            pass
        list_only.append(c / "projects")
        for _n, _tpl, _cy, _d in _audit_table_raw(manifest):
            if _tpl:
                allowed_extra_files.append(expand_tilde(_tpl, h))
    else:
        reference.append(c / "CLAUDE.md")
        try:
            reference.extend(sorted(c.glob("skills/*/SKILL.md")))
        except OSError:
            pass
        reference = [p for p in reference if p.exists()]

    scope = Scope(
        mode=mode, project_root=root, slug=slug, memory_dir=mdir, transcript_dir=tdir,
        allowed_roots=[p for p in allowed if p is not None],
        reference_files=reference, manifest=manifest, refused=[], slug_collisions=[],
        home=h, launch_agents=la,
        allowed_files=list(reference) + allowed_extra_files, list_only_dirs=list_only,
    )
    scope.owner_project = manifest.get("owner_project") or ""
    scope.global_imports = _global_imports(scope)
    if mode == "global":
        scope.allowed_roots.extend(scope.global_imports)
    else:
        scope.reference_files.extend(scope.global_imports)
        scope.allowed_files.extend(scope.global_imports)
    scope.allowed_files.extend(_expand_log_globs(scope))
    scope.slug_collisions = _slug_collisions(scope)
    return scope


def _global_imports(scope: Scope):
    """全局 CLAUDE.md 用 @ 匯入的檔：只收解析後仍在 ~/.claude 底下、而且存在的檔。
    這些檔跟全局 CLAUDE.md 一樣每個 session 開場載入，所以專案模式當對照層、全局層模式當受審檔；
    不寫死任何目錄名，使用者沒有這種慣例就是空清單。"""
    c = scope.home / ".claude"
    f = c / "CLAUDE.md"
    out = []
    try:
        if not f.is_file():
            return out
    except OSError:
        return out
    for line in safe_lines(scope, f, max_bytes=512 * 1024):
        for tok in _ctx_import_tokens(line):
            if tok.startswith("~/"):
                p = scope.home / tok[2:]
            elif tok.startswith("/"):
                p = Path(tok)
            else:
                p = c / tok
            p = Path(os.path.normpath(str(p)))
            try:
                if _under(p, c) and p.is_file() and p not in out:
                    out.append(p)
            except OSError:
                continue
    return out


def scope_dict(scope: Scope) -> dict:
    m = scope.manifest
    return {
        "scanner_version": VERSION,
        "mode": scope.mode,
        "scan_coverage": (
            "本專案根＋專案 memory／transcript＋manifest 宣告的 code_roots／log_globs＋"
            + ("（全局層模式）~/.claude 的 CLAUDE.md、它 @匯入的檔、skills／hooks／bin／settings.json／*.sh"
               if scope.mode == "global"
               else "（專案）全局對照：~/.claude/CLAUDE.md、它 @匯入的檔、skill frontmatter")
        ),
        "project_root": rel_home(scope, scope.project_root),
        "slug": scope.slug,
        "memory_dir": rel_home(scope, scope.memory_dir),
        "transcript_dir": rel_home(scope, scope.transcript_dir),
        "launch_agents": rel_home(scope, scope.launch_agents),
        "allowed_roots": [rel_home(scope, p) for p in scope.allowed_roots],
        "allowed_files": [rel_home(scope, p) for p in scope.allowed_files],
        "reference_files": [rel_home(scope, p) for p in scope.reference_files],
        "list_only_dirs": [rel_home(scope, p) for p in scope.list_only_dirs],
        "refused": scope.refused,
        "slug_collisions": scope.slug_collisions,
        "owner_project": scope.owner_project,
        "manifest": {
            "present": bool(m),
            "owner_project": m.get("owner_project", ""),
            "code_roots": [rel_home(scope, p) for p in (m.get("code_roots") or [])],
            "log_globs": list(m.get("log_globs") or []),
            "exclude": list(m.get("exclude") or []),
            "security_allow_count": len(m.get("security_allow") or []),
            "extra_topics": sorted((m.get("topics") or {}).keys()),
        },
    }


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------
SKIP_DIR_PARTS = ("_retired", "_backup", ".git", "node_modules", "__pycache__")


def _excluded(scope: Scope, p: Path) -> bool:
    s = str(p)
    for part in Path(s).parts:
        if part in SKIP_DIR_PARTS or part.startswith(".bak"):
            return True
    if Path(s).name.startswith(".bak") or ".bak" in Path(s).suffixes:
        return True
    for pat in scope.manifest.get("exclude") or []:
        if Path(s).match(str(pat)):
            return True
        if re.search(re.escape(str(pat).strip("*").strip("/")), s) and str(pat).startswith("**/"):
            return True
    return False


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _est_tokens(text: str) -> int:
    cjk = 0
    other = 0
    for ch in text:
        if "　" <= ch <= "鿿" or "＀" <= ch <= "￯":
            cjk += 1
        else:
            other += 1
    return int(cjk + other / 4.0)


def _sorted_glob(base: Path, pattern: str):
    try:
        return sorted(base.glob(pattern))
    except OSError:
        return []


def iter_sources(scope: Scope):
    """yield (path, layer, role, frontmatter_only)"""
    root = scope.project_root
    c = scope.home / ".claude"
    out = []
    out.append((root / "CLAUDE.md", "project", "audited", False))
    for p in _sorted_glob(root / ".claude", "settings*.json"):
        out.append((p, "settings", "audited", False))
    for p in _sorted_glob(root / ".claude", "hooks/*"):
        if p.is_file():
            out.append((p, "hook", "audited", False))
    for p in _sorted_glob(root / ".claude", "skills/*/SKILL.md"):
        out.append((p, "skill", "audited", False))
    for p in _sorted_glob(scope.memory_dir, "*.md"):
        out.append((p, "memory", "audited", False))
    if scope.mode == "global":
        out.append((c / "CLAUDE.md", "global", "audited", False))
        for p in scope.global_imports:
            out.append((p, "global", "audited", False))
        for p in _sorted_glob(c, "skills/*/SKILL.md"):
            out.append((p, "skill", "audited", False))
        for p in _sorted_glob(c, "hooks/*"):
            if p.is_file():
                out.append((p, "hook", "audited", False))
        for p in _sorted_glob(c, "*.sh"):
            out.append((p, "hook", "audited", False))
        out.append((c / "settings.json", "settings", "audited", False))
        for p in _sorted_glob(c, "bin/*"):
            if p.is_file():
                out.append((p, "bin", "audited", False))
    else:
        out.append((c / "CLAUDE.md", "global", "reference", False))
        for p in scope.global_imports:
            out.append((p, "global", "reference", False))
        for p in _sorted_glob(c, "skills/*/SKILL.md"):
            out.append((p, "skill", "reference", True))
    seen = set()
    for p, layer, role, fm in out:
        try:
            if not p.exists() or not p.is_file():
                continue
        except OSError:
            continue
        if _excluded(scope, p):
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        yield p, layer, role, fm


def _skill_description(text: str) -> str:
    """SKILL.md 每場開場注入的是 frontmatter 的 description 欄，不是前 30 行（量前 30 行會把正文算進常駐，嚴重高估）。
    只認 `---` 圍起來的 frontmatter；description 支援單行、引號、以及 YAML `>`／`|` 區塊（接續的縮排行）。
    抽不到 description 就退回整個 frontmatter；連 frontmatter 都沒有才退回前 30 行（並由呼叫端記 reason）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    fm = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        fm.append(ln)
    else:
        return ""
    desc = None
    i = 0
    while i < len(fm):
        ln = fm[i]
        if ln.startswith("description:"):
            val = ln[len("description:"):].strip()
            if val in (">", "|", ">-", "|-"):
                block = []
                i += 1
                while i < len(fm) and (fm[i].startswith(" ") or fm[i].startswith("\t") or fm[i].strip() == ""):
                    block.append(fm[i].strip())
                    i += 1
                desc = " ".join(b for b in block if b)
            else:
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                desc = val
            break
        i += 1
    if desc is None:
        return "\n".join(fm)
    return desc


def _always_on(scope: Scope, p: Path, layer: str):
    c = scope.home / ".claude"
    if p == scope.project_root / "CLAUDE.md":
        return True, "專案 CLAUDE.md：每個 session 開場載入"
    if p == c / "CLAUDE.md":
        return True, "全局 CLAUDE.md：每個 session 開場載入"
    if p in scope.global_imports:
        return True, "由全局 CLAUDE.md @匯入，每個 session 載入"
    if p == scope.memory_dir / "MEMORY.md":
        return True, "SessionStart 注入記憶索引"
    if layer == "skill" and p.name == "SKILL.md":
        return True, "skill description 常駐工具清單（只計 frontmatter description 欄）"
    return False, ""


def build_inventory(scope: Scope):
    inv = []
    for p, layer, role, fm_only in iter_sources(scope):
        text = safe_open(scope, p, max_bytes=(16384 if fm_only else 4 * 1024 * 1024))
        if text is None:
            continue
        if fm_only:
            text = "\n".join(text.splitlines()[:30])  # 對照層 skill 只讀 frontmatter（前 30 行），不讀正文
        measured = text
        if fm_only or (layer == "skill" and p.name == "SKILL.md"):
            head = "\n".join(text.splitlines()[:30])
            if fm_only:
                measured = head
        ao, reason = _always_on(scope, p, layer)
        if ao and layer == "skill":
            desc = _skill_description(text)
            if desc:
                measured = desc
            else:
                measured = "\n".join(text.splitlines()[:30])
                reason = reason + "（無 frontmatter，退回前 30 行）"
        inv.append({
            "path_rel": rel_home(scope, p),
            "layer": layer,
            "role": role,
            "bytes": len(text.encode("utf-8")),
            "utf16_units": _utf16_units(measured),
            "est_tokens": _est_tokens(measured),
            "always_on": ao,
            "always_on_reason": reason,
        })
    return inv


# --------------------------------------------------------------------------
# inject（hook 注入）與 import（CLAUDE.md @匯入）常駐列
#
# 🔴 最重要的判準：目標路徑先做「純字面包含判定」，
#    字面不在允許根底下＝不 resolve、不 stat、不 open、不 exists()。
#    （Path.resolve() 本身會逐段 lstat，所以字面在外的路徑連 resolve 都不准。）
# --------------------------------------------------------------------------
CTX_EVENTS = ("SessionStart", "UserPromptSubmit")
CTX_MAX_IMPORT_DEPTH = 5
_RE_SEG_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_RE_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_RE_IMPORT_INLINE = re.compile(r"(?:^|\s)@(~/[^\s]+|\./[^\s]+|\.\./[^\s]+)")
_RE_REDIR = re.compile(r"^\d*[<>]")
_RE_REDIR_BARE = re.compile(r"\d*[<>]{1,2}")
_CTX_READ_CMDS = ("cat", "head", "tail", "sed")
_CTX_SCRIPT_CMDS = ("bash", "sh", "zsh", "python3", "python")


def _ctx_roots(scope: Scope):
    """ALLOWED_CTX_ROOTS：專案根、~/.claude、manifest 宣告的姊妹根（全部先 resolve）。"""
    out = []
    for p in [scope.project_root, scope.home / ".claude"] + list(scope.manifest.get("sibling_roots") or []):
        try:
            out.append(Path(p).resolve())
        except OSError:
            out.append(Path(p))
    return out


def _ctx_literal_inside(target, roots) -> bool:
    """純字面包含判定：os.path.normpath 折疊 ..，🚫 完全不碰檔案系統。"""
    np = os.path.normpath(str(target))
    if not os.path.isabs(np):
        return False
    for r in roots:
        rs = os.path.normpath(str(r))
        if np == rs or np.startswith(rs + os.sep):
            return True
    return False


def _ctx_followable(scope: Scope, target) -> bool:
    """只有這幾處的腳本才追讀一層：<專案>/.claude/hooks/、~/.claude/hooks/、~/.claude/*.sh、~/.claude/bin/。"""
    p = os.path.normpath(str(target))
    c = os.path.normpath(str(scope.home / ".claude"))
    roots = [os.path.normpath(str(scope.project_root / ".claude" / "hooks")),
             os.path.join(c, "hooks"), os.path.join(c, "bin")]
    for r in roots:
        if p == r or p.startswith(r + os.sep):
            return True
    return os.path.dirname(p) == c and p.endswith(".sh")


def _ctx_expand(scope: Scope, s, vars_):
    """引數展開：$P／$CLAUDE_PROJECT_DIR／${CLAUDE_PROJECT_DIR:-$(pwd)}／$(pwd)→專案根；$HOME／~→home。"""
    s = str(s)
    proj = str(scope.project_root)
    h = str(scope.home)
    s = s.replace("${CLAUDE_PROJECT_DIR:-$(pwd)}", proj)
    s = s.replace("${CLAUDE_PROJECT_DIR}", proj).replace("$CLAUDE_PROJECT_DIR", proj)
    s = s.replace("$(pwd)", proj).replace("`pwd`", proj)
    s = s.replace("${HOME}", h).replace("$HOME", h)
    for k, v in (vars_ or {}).items():
        s = re.sub(r"\$\{%s\}|\$%s(?![A-Za-z0-9_])" % (re.escape(k), re.escape(k)), lambda _m, _v=v: _v, s)
    if s == "~":
        return h
    if s.startswith("~/"):
        return h + s[1:]
    return s


def _ctx_dynamic(s: str) -> bool:
    return ("$" in s) or ("`" in s)


def _ctx_row(target_disp, layer, reason, source_rel, **flags):
    row = {
        "path_rel": target_disp,
        "target": target_disp,
        "layer": layer,
        "role": "audited",
        "bytes": 0,
        "utf16_units": 0,
        "est_tokens": 0,
        "always_on": True,
        "always_on_reason": reason,
        "source_file": source_rel,
    }
    row.update(flags)
    return row


def _ctx_push(rows, seen, row):
    key = (row["layer"], row["path_rel"])
    if key in seen:
        return
    seen[key] = True
    rows.append(row)


def _ctx_open_once(scope: Scope, rp):
    """已通過字面＋resolve 兩道允許根判定的檔，單次暫時放行→讀→撤回。
    _ctx_read_target 與 _ctx_walk_imports 的遞迴都走這裡；否則遞迴再開同一檔會被 _guard 記成 out_of_scope，
    inventory 有數字、scope.refused 卻說沒讀＝兩處說法對不上。"""
    added = rp not in scope.allowed_files
    if added:
        scope.allowed_files.append(rp)
    try:
        return safe_open(scope, rp, max_bytes=4 * 1024 * 1024)
    finally:
        if added:
            try:
                scope.allowed_files.remove(rp)
            except ValueError:
                pass


def _ctx_read_target(scope: Scope, roots, expanded, layer, reason, source_rel, extra=None):
    """回 (row, resolved_path 或 None)。不在允許根＝只記 outside，一步都不碰檔案系統。"""
    extra = dict(extra or {})
    disp = rel_home(scope, os.path.normpath(str(expanded)))  # 純字串折疊，不碰檔案系統
    if not _ctx_literal_inside(expanded, roots):
        row = _ctx_row(disp, layer, reason, source_rel, outside=True)
        row.update(extra)
        return row, None
    try:
        rp = Path(expanded).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):  # 別把程式錯誤偽裝成 unreadable
        row = _ctx_row(disp, layer, reason, source_rel, unreadable=True)
        row.update(extra)
        return row, None
    if not any(_under(rp, r) for r in roots):
        # symlink 指出樹＝視同 outside，不讀
        row = _ctx_row(disp, layer, reason, source_rel, outside=True)
        row.update(extra)
        return row, None
    if rp not in scope.ctx_files:
        scope.ctx_files.append(rp)
    # 單次呼叫內暫時放行→讀→撤回，不讓 shell 啟發式永久擴大 allowed_files
    text = _ctx_open_once(scope, rp)
    if text is None:
        row = _ctx_row(rel_home(scope, rp), layer, reason, source_rel, unreadable=True)
        row.update(extra)
        return row, rp
    row = _ctx_row(rel_home(scope, rp), layer, reason, source_rel)
    row["bytes"] = len(text.encode("utf-8"))
    row["utf16_units"] = _utf16_units(text)
    row["est_tokens"] = _est_tokens(text)
    row.update(extra)
    return row, rp


def _ctx_split_cmdsubs(text: str, max_subs: int = 50):
    """把 $( … ) 抽出來單獨解析，外層以佔位符取代。

    切段規則只按 ;／&&／||／| 切，但實際見過的 SessionStart hook 長這樣
    `jq --arg inbox "$(cat "$P/INBOX.md" | head -40)"`：不抽出來，
    內層引號會把外層切壞，真正的 cat 目標就整個看不見（它必須被量到）。
    """
    out = []
    subs = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("$(", i) and len(subs) < max_subs:
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                subs.append(text[i + 2:j - 1])
                out.append("__CMDSUB__")
                i = j
                continue
        out.append(text[i])
        i += 1
    return "".join(out), subs


_RE_PATHISH = re.compile(r"[~$@{}A-Za-z0-9_./\-]+")


def _ctx_emit(rows, seen, row, strict, tok=None):
    """strict＝在被追讀的腳本內：量到的照收，沒量到的只收「長得像路徑」的 token。

    腳本內容是用 shell 啟發式硬解的（連 .py 都可能被 python3 追讀），
    切錯產生的碎片是純噪音——一次就能長出幾十條（`=`／`over`／`"): continue`…）。
    這些碎片本來就沒碰過檔案系統，丟掉不會鬆邊界。
    """
    if strict and any(row.get(k) for k in ("dynamic", "outside", "unreadable", "unfollowed")):
        s = str(tok or "")
        if "/" not in s or not _RE_PATHISH.fullmatch(s):
            return
    _ctx_push(rows, seen, row)


def _ctx_seg_files(tokens):
    """回 ("files", [...])／("script", path)／None。只認 cat／head／tail／sed 與 bash|sh|zsh|python3 SCRIPT。"""
    if not tokens:
        return None
    cmd = os.path.basename(tokens[0])
    args = list(tokens[1:])
    if cmd in _CTX_READ_CMDS:
        nonflag = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if _RE_REDIR.match(a):
                if _RE_REDIR_BARE.fullmatch(a):
                    skip_next = True  # `> file` 這種：檔名歸重導向，不是要讀的檔
                continue
            if a.startswith("-"):
                if cmd in ("head", "tail") and a == "-n":
                    skip_next = True
                elif cmd == "sed" and a in ("-e", "-f"):
                    skip_next = True
                continue
            nonflag.append(a)
        if cmd == "sed":
            has_e = any(a in ("-e", "-f") for a in args)
            files = nonflag if has_e else nonflag[1:]
        else:
            files = nonflag
        return ("files", files) if files else None
    if cmd in _CTX_SCRIPT_CMDS:
        if "-c" in args:
            return None
        for a in args:
            if a.startswith("-"):
                continue
            return ("script", a)
        return None
    return None


def _ctx_parse_command(scope: Scope, roots, cmd_text, reason, source_rel, rows, seen, depth, init_vars=None):
    """把一段 hook 命令切段解析；切錯只會造成 dynamic，不會越界。"""
    vars_ = dict(init_vars or {})
    strict = depth >= 1
    text0 = str(cmd_text)
    proj0 = str(scope.project_root)
    text0 = text0.replace("${CLAUDE_PROJECT_DIR:-$(pwd)}", proj0).replace("$(pwd)", proj0).replace("`pwd`", proj0)
    text0, cmdsubs = _ctx_split_cmdsubs(text0)
    for seg in _RE_SEG_SPLIT.split(text0):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            _ctx_emit(rows, seen, _ctx_row(clip(seg, 120), "inject", reason, source_rel, dynamic=True), strict, None)
            continue
        while toks:
            m = _RE_ASSIGN.match(toks[0])
            if not m:
                break
            vars_[m.group(1)] = _ctx_expand(scope, m.group(2), vars_)
            toks = toks[1:]
        got = _ctx_seg_files(toks)
        if not got:
            continue
        kind, items = got
        if kind == "files":
            for a in items:
                ex = _ctx_expand(scope, a, vars_)
                if _ctx_dynamic(ex):
                    _ctx_emit(rows, seen, _ctx_row(a, "inject", reason, source_rel, dynamic=True), strict, a)
                    continue
                row, _rp = _ctx_read_target(scope, roots, ex, "inject", reason, source_rel)
                _ctx_emit(rows, seen, row, strict, a)
            continue
        # kind == "script"
        a = items
        ex = _ctx_expand(scope, a, vars_)
        if _ctx_dynamic(ex):
            _ctx_emit(rows, seen, _ctx_row(a, "inject", reason, source_rel, dynamic=True), strict, a)
            continue
        if depth >= 1 or not _ctx_followable(scope, ex):
            row = _ctx_row(rel_home(scope, ex), "inject", reason, source_rel, unfollowed=True)
            if not _ctx_literal_inside(ex, roots):
                row["outside"] = True
            _ctx_emit(rows, seen, row, strict, a)
            continue
        try:
            rp = Path(ex).resolve(strict=False)
        except Exception:
            _ctx_emit(rows, seen, _ctx_row(rel_home(scope, ex), "inject", reason, source_rel,
                                           unfollowed=True, unreadable=True), strict, a)
            continue
        if not any(_under(rp, r) for r in roots):
            _ctx_emit(rows, seen, _ctx_row(rel_home(scope, ex), "inject", reason, source_rel,
                                           unfollowed=True, outside=True), strict, a)
            continue
        if rp not in scope.allowed_files:
            scope.allowed_files.append(rp)
        if rp not in scope.ctx_files:
            scope.ctx_files.append(rp)
        text = safe_open(scope, rp, max_bytes=4 * 1024 * 1024)
        if text is None:
            _ctx_emit(rows, seen, _ctx_row(rel_home(scope, rp), "inject", reason, source_rel,
                                           unfollowed=True, unreadable=True), strict, a)
            continue
        sub_reason = reason[:-1] + "；經 " + rp.name + "）" if reason.endswith("）") else reason
        _ctx_parse_command(scope, roots, text, sub_reason, source_rel, rows, seen, depth + 1, dict(vars_))
    for sub in cmdsubs:
        _ctx_parse_command(scope, roots, sub, reason, source_rel, rows, seen, depth, vars_)


def _collect_hook_commands(node, out):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str):
            out.append(cmd)
        for v in node.values():
            _collect_hook_commands(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_hook_commands(v, out)


def scan_inject(scope: Scope, roots, rows, seen):
    for sp in _settings_paths(scope):
        text = safe_open(scope, sp, max_bytes=MAX_FILE_BYTES)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            record_incomplete(scope, "inject", "settings 解析失敗，hook 注入未盤點：%s" % rel_home(scope, sp))  # 別安靜變空
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        src = rel_home(scope, sp)
        for ev in CTX_EVENTS:
            cmds = []
            _collect_hook_commands(hooks.get(ev), cmds)
            for c in cmds:
                _ctx_parse_command(scope, roots, c, "%s hook 注入（%s）" % (ev, src), src, rows, seen, 0)
    return rows


def _ctx_import_tokens(line: str):
    out = []
    s = line.strip()
    if s.startswith("@"):
        rest = s[1:].split()
        tok = rest[0] if rest else ""
        if tok and ("/" in tok or "." in tok):
            out.append(tok)
        return out
    for m in _RE_IMPORT_INLINE.finditer(line):
        out.append(m.group(1))
    return out


def _ctx_walk_imports(scope: Scope, roots, src_path, rows, seen, visited, depth):
    if depth > CTX_MAX_IMPORT_DEPTH:
        return
    key = str(src_path)
    if key in visited:
        return
    visited.add(key)
    # depth 0 的 CLAUDE.md 本來就在範圍內；depth ≥1 的是剛經 _ctx_read_target 兩道判定讀過的檔，同樣單次放行
    text = _ctx_open_once(scope, Path(src_path)) if depth > 0 else safe_open(scope, src_path, max_bytes=4 * 1024 * 1024)
    if text is None:
        return
    src_rel = rel_home(scope, src_path)
    base = Path(src_path).parent
    for ln_i, line in enumerate(text.splitlines(), 1):
        for tok in _ctx_import_tokens(line):
            reason = "CLAUDE.md @匯入（%s:%d）" % (src_rel, ln_i)
            if tok == "~" or tok.startswith("~/"):
                ex = str(expand_tilde(tok, scope.home))
            elif os.path.isabs(tok):
                ex = tok
            else:
                ex = os.path.join(str(base), tok)
            row, rp = _ctx_read_target(scope, roots, ex, "import", reason, src_rel)
            _ctx_push(rows, seen, row)
            if rp is not None and not row.get("unreadable") and not row.get("outside"):
                _ctx_walk_imports(scope, roots, rp, rows, seen, visited, depth + 1)


def scan_imports(scope: Scope, roots, inventory, rows, seen):
    visited = set()
    for item in inventory:
        if item.get("layer") in ("project", "global") and Path(item["path_rel"]).name == "CLAUDE.md":
            _ctx_walk_imports(scope, roots, expand_tilde(item["path_rel"], scope.home),
                              rows, seen, visited, 0)
    return rows


def augment_context_inventory(scope: Scope, inventory):
    """把 inject／import 常駐列併進 inventory；與既有列同路徑時只補 always_on_reason，不重複計。"""
    roots = _ctx_roots(scope)
    rows = []
    seen = {}
    scan_inject(scope, roots, rows, seen)
    scan_imports(scope, roots, inventory, rows, seen)
    by_path = {}
    for it in inventory:
        by_path.setdefault(it["path_rel"], it)
    add = []
    for r in rows:
        flagged = r.get("outside") or r.get("dynamic") or r.get("unreadable") or r.get("unfollowed")
        ex = by_path.get(r["path_rel"])
        if ex is not None and not flagged:
            note = "；亦由 @匯入" if r["layer"] == "import" else "；亦由 hook 注入"
            cur = ex.get("always_on_reason") or ""
            if note not in cur:
                ex["always_on_reason"] = cur + note
            continue
        add.append(r)
    inventory.extend(add)
    return inventory


# --------------------------------------------------------------------------
# 規則抽取
# --------------------------------------------------------------------------
RULE_MARKERS_CJK = [
    "🚫", "🔴", "⚠️", "🔒", "一律", "必須", "不要", "別", "禁止", "絕不", "永不",
    "不准", "不可", "只准", "只能", "鐵則",
]  # 不收「優先／預設／必」與 only／always，噪音太大
RULE_MARKERS_EN = ["never", "must", "do not", "don't"]
HIGH_MARKERS = ["🔴", "🚫", "🔒", "絕不", "永不", "禁止", "不准", "never"]

TOPIC_TABLE = {
    "notify": ["通知", "notify", "notification", "推播", "訊息", "告警", "提醒", "webhook"],
    "docs": ["文件", "docs", "README", "資料庫", "頁面", "知識庫", "wiki"],
    "git": ["git", "commit", "push", "merge", "rebase", "pathspec", "repo", "分支", "branch", "clone"],
    "todo": ["待辦", "todo", "卡", "issue", "ticket", "handoff", "交接", "接手", "pickup"],
    "memory": ["memory", "MEMORY.md", "記憶", "lessons", "索引", "記錄"],
    "time": ["時間", "校時", "date", "時戳", "timestamp", "幾點", "時區", "timezone", "分鐘", "小時", "時段", "今天"],
    "model": ["模型", "model", "opus", "sonnet", "haiku", "檔位", "tier", "token", "context", "配額", "quota", "effort"],
    "dispatch": ["派工", "dispatch", "Workflow", "subagent", "fan-out", "agent", "審查"],
    "credential": ["憑證", "token", "密碼", "OAuth", "憑據", "secret", "key", "登入", "授權", "外洩"],
    "browser": ["Chrome", "CDP", "瀏覽器", "browser", "playwright", "截圖", "selector", "DOM", "分頁", "tab"],
    "launchd": ["launchd", "plist", "cron", "runner", "headless", "claude -p", "tmux", "session", "schedule", "排程器"],
    "review": ["審", "review", "收尾", "回報", "報告", "驗證", "證據", "自檢"],
    "files": ["雲端同步", "路徑", "目錄", "TCC", "repo", "檔案", "備份", "symlink", "scratch", "磁碟", "path"],
}

_RE_MD_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*|#{1,6}\s*)+")
_RE_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*)$")


def _topics_for(text: str, extra: dict):
    low = text.lower()
    hit = []
    table = dict(TOPIC_TABLE)
    for k, v in (extra or {}).items():
        table.setdefault(str(k), [])
        table[str(k)] = list(table[str(k)]) + [str(x) for x in (v or [])]
    for topic, kws in table.items():
        for kw in kws:
            k = str(kw)
            if (k.lower() in low) if k.isascii() else (k in text):
                hit.append(topic)
                break
    return hit or ["other"]


def _is_rule_line(text: str) -> bool:
    if len(text) < 8:
        return False
    low = text.lower()
    for m in RULE_MARKERS_CJK:
        if m in text:
            return True
    for m in RULE_MARKERS_EN:
        if m in low:
            return True
    return False


def extract_rules(scope: Scope, inventory):
    rules = []
    extra_topics = scope.manifest.get("topics") or {}
    for item in inventory:
        layer = item["layer"]
        role = item["role"]
        if layer in ("bin", "settings", "inject", "import"):
            continue  # settings 只取鍵名，內容不進規則文字；inject／import 不是規則檔
        if role == "reference" and layer == "skill":
            continue  # frontmatter 只量大小，不抽規則
        p = expand_tilde(item["path_rel"], scope.home)
        text = safe_open(scope, p, max_bytes=4 * 1024 * 1024)
        if text is None:
            continue
        heading = ""
        lines = text.splitlines()
        last_idx = None
        for i, raw in enumerate(lines, 1):
            hm = _RE_HEADING.match(raw)
            if hm:
                heading = clip(hm.group(2).strip(), 60)
                last_idx = None
                continue
            body = _RE_MD_PREFIX.sub("", raw).strip()
            if not body:
                last_idx = None
                continue
            indented = raw[:1] in (" ", "\t")
            if indented and last_idx is not None and rules[last_idx]["_cont"] < 3:
                rules[last_idx]["text"] = clip(rules[last_idx]["text"] + " " + body, 300)
                rules[last_idx]["_cont"] += 1
                continue
            if not _is_rule_line(body):
                last_idx = None
                continue
            file_field = "%s:%s" % (layer, item["path_rel"])
            rid = hashlib.sha1(("%s|%s|%s" % (item["path_rel"], i, body)).encode("utf-8")).hexdigest()[:12]
            prio = "high" if any(m in body or m.lower() in body.lower() for m in HIGH_MARKERS) else "normal"
            rules.append({
                "id": rid, "file": file_field, "line": i, "heading": heading,
                "text": clip(body, 300), "topics": _topics_for(body, extra_topics),
                "priority": prio, "role": role, "layer": layer,
                "_cont": 0,
            })
            last_idx = len(rules) - 1
    for r in rules:
        r.pop("_cont", None)
    return rules


def write_topic_buckets(rules, out: Path, max_buckets=32, max_chars=40000):
    # 桶不再併進 other（設桶數上限會把小桶全塞進 other，反而讓它膨脹）；finder 派工按檔案分組，見 SKILL.md 附錄 A
    buckets = {}
    for r in rules:
        for t in r.get("topics") or ["other"]:
            buckets.setdefault(t, []).append(r)
    if len(buckets) > max_buckets:
        ordered = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
        keep = [k for k, v in ordered if k != "other"][: max_buckets - 1]
        merged = {}
        other = list(buckets.get("other") or [])
        for k, v in buckets.items():
            if k in keep:
                merged[k] = v
            elif k != "other":
                other.extend(v)
        merged["other"] = other
        buckets = merged
    written = {}
    d = out / "rules_by_topic"
    for topic in sorted(buckets):
        rows = buckets[topic]
        lines = []
        for r in rows:
            prefix = "[REF] " if r.get("role") == "reference" else ""
            lines.append("- %s[%s] %s:%s（%s）%s" % (prefix, r["id"], r["file"], r["line"], r["priority"], r["text"]))
        chunks = []
        cur = []
        cur_len = 0
        for ln in lines:
            if cur and cur_len + len(ln) + 1 > max_chars:
                chunks.append(cur)
                cur = []
                cur_len = 0
            cur.append(ln)
            cur_len += len(ln) + 1
        if cur:
            chunks.append(cur)
        names = []
        for i, ch in enumerate(chunks):
            name = "%s.md" % topic if i == 0 else "%s.part%d.md" % (topic, i + 1)
            header = "# 主題桶：%s（%d 條，第 %d/%d 份）\n\n" % (topic, len(rows), i + 1, len(chunks))
            emit_text(d / name, header + "\n".join(ch) + "\n")
            names.append(name)
        written[topic] = names
    return written


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------
_RE_CMDNAME = re.compile(r"<command-name>\s*/?([^<\s][^<]{0,80}?)\s*</command-name>")
_RE_MEMREF = re.compile(r"/memory/([A-Za-z0-9_\-\.一-鿿]+)\.md")


def _known_skill_names(scope: Scope):
    names = set()
    for p in _sorted_glob(scope.project_root / ".claude", "skills/*/SKILL.md"):
        names.add(p.parent.name)
    if scope.mode == "global":
        for p in _sorted_glob(scope.home / ".claude", "skills/*/SKILL.md"):
            names.add(p.parent.name)
    return names


def _known_bin_names(scope: Scope):
    names = {}
    if scope.mode != "global":
        return names
    for p in _sorted_glob(scope.home / ".claude", "bin/*"):
        if p.is_file():
            names[p.name] = p
            names.setdefault(p.stem, p)
    return names


def _bump(d, key, date_str):
    e = d.setdefault(key, {"count": 0, "last_seen": None})
    e["count"] += 1
    if date_str and (e["last_seen"] is None or date_str > e["last_seen"]):
        e["last_seen"] = date_str


def scan_usage(scope: Scope, usage_days: int) -> dict:
    tdir = scope.transcript_dir
    skills = {}
    memory = {}
    binhits = {}
    cli_cmds = {}
    parse_errors = 0
    scanned = 0
    cutoff = (now() - timedelta(days=usage_days)).timestamp()
    skill_names = _known_skill_names(scope)
    bin_names = _known_bin_names(scope)
    if not tdir.exists():
        record_incomplete(scope, "usage", "transcript 目錄不存在：%s" % rel_home(scope, tdir))
        return {"transcripts_scanned": 0, "window_days": usage_days, "parse_errors": 0,
                "skills": {}, "memory": {}, "bin": {}}
    try:
        files = sorted([p for p in tdir.iterdir() if p.is_file() and p.suffix == ".jsonl"])
    except OSError:
        record_incomplete(scope, "usage", "transcript 目錄讀不到")
        files = []
    files = [p for p in files if not p.name.startswith("agent-")]
    if not files:
        record_incomplete(scope, "usage", "transcript 目錄 0 個 jsonl：%s" % rel_home(scope, tdir))
    for p in files:
        st = safe_stat(scope, p)
        if st is None or st.st_mtime < cutoff:
            continue
        lines = safe_lines(scope, p, max_bytes=256 * 1024 * 1024)  # 單一 transcript 可能很大
        if lines is None:
            record_incomplete(scope, "usage", "transcript 讀不到（%s）" % rel_home(scope, p))
            continue
        scanned += 1
        fdate = datetime.fromtimestamp(st.st_mtime).date().isoformat()
        for line in lines:
            if not line.strip():
                continue
            for m in _RE_CMDNAME.finditer(line):
                nm = m.group(1).strip().lstrip("/")
                if nm and nm in skill_names:
                    _bump(skills, nm, fdate)
                elif nm:
                    _bump(cli_cmds, nm, fdate)  # /clear、/compact 這類內建指令另計，不混進 skill
            try:
                obj = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            ts = obj.get("timestamp")
            d = fdate
            if isinstance(ts, str) and len(ts) >= 10:
                d = ts[:10]
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                nm = c.get("name")
                inp = c.get("input") or {}
                if not isinstance(inp, dict):
                    continue
                if nm == "Skill":
                    sk = inp.get("skill")
                    if isinstance(sk, str) and sk:
                        _bump(skills, sk.lstrip("/"), d)
                elif nm == "Read":
                    fp = inp.get("file_path")
                    if isinstance(fp, str):
                        mm = _RE_MEMREF.search(fp)
                        if mm:
                            _bump(memory, mm.group(1), d)
                elif nm == "Bash":
                    cmd = inp.get("command")
                    if not isinstance(cmd, str):
                        continue
                    for mm in _RE_MEMREF.finditer(cmd):
                        _bump(memory, mm.group(1), d)
                    for sname in skill_names:
                        if sname in cmd:
                            _bump(skills, sname, d)
                    for bname in bin_names:
                        if re.search(r"\b%s\b" % re.escape(bname), cmd):
                            _bump(binhits, bname, d)
    return {"transcripts_scanned": scanned, "window_days": usage_days,
            "parse_errors": parse_errors, "skills": skills, "memory": memory, "bin": binhits,
            "cli_commands": cli_cmds,
            "signal_note": "訊號＝%d 天 transcript 內 Skill 呼叫／command-name／Read／Bash 引用；描述匹配觸發不留痕，故只標疑似" % usage_days}


# --------------------------------------------------------------------------
# dead refs
# --------------------------------------------------------------------------
_RE_PATHTOK = re.compile(r"(~/[^\s`'\"，。、；：）)\]】>,]+|/Users/[^\s`'\"，。、；：）)\]】>,]+)")  # 半形逗號也切
_RE_BACKTICK_FILE = re.compile(r"`([^`\s*]+\.(?:py|sh|js|plist))`")  # 裸 .md 檔名太雜（筆記庫文件），不判；含 * 的是 glob
# 跨機標記：預設只認通用說法；自家機器代稱在 manifest 寫 {"machine_names": ["boxA", "boxB"]}
_RE_CROSS_MACHINE = re.compile(r"另一台|\bssh\s|\banother (?:machine|host|box)\b|\bremote (?:machine|host)\b", re.I)  # 整行有跨機標記＝本機驗不了


def _cross_machine_re(scope: Scope):
    """通用跨機說法＋manifest 宣告的自家機器代稱（詞邊界比對，studios 不會被 studio 命中）。"""
    names = [str(x) for x in (scope.manifest.get("machine_names") or []) if str(x).strip()]
    if not names:
        return _RE_CROSS_MACHINE
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z])(%s)(?![A-Za-z])|另一台|\bssh\s" % alt)
_TRAIL = "。，、；：）)]】>.,:;!?'\"`"
# 全形標點是說明文字併進路徑的接縫；判定改「階梯裁切」，任一前綴存在就不判死。
_CJK_PUNCT = "「」『』（）〈〉《》【】、。，；：！？／｜　;("  # 半形 ; ( 也是常見接縫（`~/.local/bin;真身`），階梯先試整段再裁，含 ( 的真路徑不受影響


def _claude_territory(scope: Scope, p) -> bool:
    """字面判定路徑是否在本工具管得到的地盤（專案根、~/.claude、manifest 宣告的姊妹根），不碰檔案系統。"""
    roots = [scope.project_root, scope.home / ".claude"]
    roots.extend(scope.manifest.get("sibling_roots") or [])
    return any(_under(p, r) for r in roots)


def _ladder_prefixes(ref: str):
    """回傳 [整個 token] ＋ [在每個 CJK 標點處裁短的前綴，由長到短]，前綴尾端的標點一律去掉。"""
    out = [ref]
    for i in sorted((i for i, ch in enumerate(ref) if ch in _CJK_PUNCT), reverse=True):
        pre = ref[:i].rstrip(_CJK_PUNCT)
        if pre and pre not in out:
            out.append(pre)
    return out


# 這些檔名滿地都是，就算在姊妹專案找到也不構成「其實還在」的證據。
COMMON_BASENAMES = frozenset([
    "README.md", "CLAUDE.md", "SKILL.md", "settings.json", "settings.local.json",
    "config.json", "package.json", "requirements.txt", "Makefile", "main.py",
    "setup.py", "app.py", "test.py", "tests.py", "utils.py", "run.sh", "install.sh",
    "build.sh", "index.js", "index.html", ".env", "notes.md", "HANDOFF.md",
    "INBOX.md", "MEMORY.md", "INDEX.md", "TODO.md",
])
SISTER_WALK_DEPTH = 3
SISTER_WALK_SECONDS = 20


def _basename_index(scope: Scope, max_depth=4):
    """回傳 {檔名: 首次找到的根標籤}；標籤＝scope／probe／sister:<根 rel_home>。只列名不讀內容。"""
    idx = {}

    def _add(name, label):
        if name not in idx:
            idx[name] = label

    for root in scope.allowed_roots:
        try:
            if not root.exists():
                continue
            if root.is_file():
                _add(root.name, "scope")
                continue
        except OSError:
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            if len(d.parts) - base_depth >= max_depth:
                del dirnames[:]
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIR_PARTS]
            for fn in filenames:
                _add(fn, "scope")
    # 裸檔名可能住在範圍外的常見位置，只列名不讀內容（list-only）
    probes = [scope.home / ".local" / "bin", scope.home / ".claude" / "bin", scope.home / ".claude" / "hooks"]
    # manifest 宣告的 code_roots 也當裸檔名的探測位置（姊妹根不放這裡，那是下面獨立的降級索引）
    probes += [d for d in (scope.manifest.get("code_roots") or []) if d.is_dir() and not d.is_symlink()]
    for root in probes:
        try:
            if not root.is_dir() or root.is_symlink():
                continue
            base_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if len(Path(dirpath).parts) - base_depth >= 2:
                    del dirnames[:]
                dirnames[:] = [x for x in dirnames if x not in SKIP_DIR_PARTS]
                for fn in filenames:
                    _add(fn, "probe")
        except OSError:
            continue
    try:
        for f in scope.home.iterdir():
            if f.is_file():
                _add(f.name, "probe")
    except OSError:
        pass
    # 姊妹專案根：manifest 的 sibling_roots，只列名，用來把「檔在別的專案」降級成 auto。
    # 🔴 沒宣告＝正常情況（多數使用者不需要），不記 incomplete，否則第一次跑就會是 partial。
    index_incomplete = []
    sisters = []
    for sister_base in (scope.manifest.get("sibling_roots") or []):
        try:
            # 不跟 symlink——`roots/ext -> /Volumes/private` 若當 os.walk 起點，followlinks=False 擋不住
            sisters.extend(sorted([d for d in sister_base.iterdir() if d.is_dir() and not d.is_symlink()]))
            skipped = [d.name for d in sister_base.iterdir() if d.is_symlink()]
            if skipped:
                scope.notes.append("dead_refs｜姊妹根有 symlink 未列名（刻意不跟）：%s" % "、".join(sorted(skipped)[:5]))  # 不算 incomplete：這是刻意省略，不是掃不到
        except OSError:
            record_incomplete(scope, "dead_refs", "姊妹根列不到：%s" % rel_home(scope, sister_base))
    for root in sisters:
        label = "sister:" + rel_home(scope, root)
        local = {}
        t0 = time.monotonic()  # 牆鐘倒退不該讓逾時失效
        timed_out = False
        try:
            base_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if (time.monotonic() - t0) > SISTER_WALK_SECONDS:
                    timed_out = True
                    break
                if len(Path(dirpath).parts) - base_depth >= SISTER_WALK_DEPTH:
                    del dirnames[:]
                dirnames[:] = sorted(x for x in dirnames if x not in SKIP_DIR_PARTS)
                for fn in sorted(filenames):
                    local.setdefault(fn, label)
        except OSError:
            timed_out = True
        if timed_out:
            index_incomplete.append(label)
            record_incomplete(scope, "dead_refs", "姊妹根列名未完成（逾時或 OSError），不用於降級：%s" % rel_home(scope, root))
            continue  # 該根不用於降級
        for fn, lb in local.items():
            _add(fn, lb)
    return idx


def find_dead_refs(scope: Scope, rules, extra_texts):
    idx = _basename_index(scope)
    out = []
    seen = set()
    cross_re = _cross_machine_re(scope)

    def check(file_label, line_no, text):
        cross = bool(cross_re.search(text))  # 整行有跨機標記＝本機驗不了
        for m in _RE_PATHTOK.finditer(text):
            ref = unquote(m.group(1).rstrip(_TRAIL)).replace("\\ ", " ").rstrip("\\")
            if len(ref) < 4:
                continue
            if any(ch in ref for ch in "*{}<>…") or "xxx" in ref.lower() or "路徑" in ref:
                continue  # glob／佔位／省略號不是路徑
            alive = False
            checked = []
            # `~/Library/Application Support/x` 這種被空白切斷的路徑，接回後面最多 3 個詞用 exists() 試，
            # 取代對任意目錄列名的啟發式（列名只准在 Claude 地盤）
            tail_words = text[m.end():].split()[:3]
            joined = ref
            for w in tail_words:
                joined = joined + " " + w.rstrip(_TRAIL)
                for cand in _ladder_prefixes(joined):
                    pj = expand_tilde(cand, scope.home) if cand.startswith("~") else Path(cand)
                    if not _under(pj, scope.home):
                        continue
                    try:
                        if pj.exists():
                            alive = True
                            break
                    except OSError:
                        continue
                if alive:
                    break
            if alive:
                continue
            for cand in _ladder_prefixes(ref):
                if len(cand) < 4:
                    continue
                p = expand_tilde(cand, scope.home) if cand.startswith("~") else Path(cand)
                if not _under(p, scope.home):
                    continue  # 只探測 ~ 底下的路徑，不當 HOME 外的存在性 oracle
                checked.append(cand)
                try:
                    if p.exists():
                        alive = True
                        break
                    if (_claude_territory(scope, p.parent) or p.parent == scope.home) and p.parent.exists() and any(
                            c.name.startswith(p.name) for c in p.parent.iterdir()):  # 列名啟發式只在 Claude 地盤或 home 本層（home 檔名本來就進 basename 索引），別對 ~/Library 之類列目錄
                        alive = True  # `.../Projects/My` 這種被空白切斷的前綴（原名含空白，例如 My Project），同層有以它開頭的目錄＝不判死
                        break
                except OSError:
                    continue
            if not alive and checked:
                ref_out = checked[-1]  # 最短前綴（不含標點）
                key = (file_label, ref_out)
                if key in seen:
                    continue
                seen.add(key)
                rec = {"file": file_label, "line": line_no, "ref": ref_out, "kind": "path"}
                if cross:
                    rec["cross_machine"] = True
                out.append(rec)
        for m in _RE_BACKTICK_FILE.finditer(text):
            fn = m.group(1)
            if "/" in fn or "~" in fn:
                continue
            label = idx.get(fn)
            if label and not label.startswith("sister:"):
                continue  # 在 scope／probe 找到＝維持現狀，不判死
            key = (file_label, fn)
            if key in seen:
                continue
            seen.add(key)
            rec = {"file": file_label, "line": line_no, "ref": fn, "kind": "filename"}
            if label and fn not in COMMON_BASENAMES:
                rec["exists_elsewhere"] = label  # 只在姊妹專案找到＝降級不丟掉
            if cross:
                rec["cross_machine"] = True
            out.append(rec)

    for r in rules:
        if r.get("role") == "reference":
            continue  # [REF] 只當衝突的另一端，不判死路徑
        check(r["file"], r["line"], r["text"])
    for label, text in extra_texts:
        for i, line in enumerate(text.splitlines(), 1):
            check(label, i, line)
    return out


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------
_RE_LASTEXIT = re.compile(r"last exit code\s*=\s*(-?\d+)")
_RE_STATE = re.compile(r"state\s*=\s*(\w+)")


def _launchctl(label: str):
    if os.environ.get("SELFCHECK_NO_LAUNCHCTL") == "1":
        return None, None
    try:
        uid = os.getuid()
        r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (uid, label)],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        return None, None
    if r.returncode != 0:
        return False, None
    txt = r.stdout.decode("utf-8", errors="replace")
    m = _RE_LASTEXIT.search(txt)
    last_exit = int(m.group(1)) if m else None
    sm = _RE_STATE.search(txt)
    loaded = True if sm else True
    return loaded, last_exit


def scan_runners(scope: Scope) -> dict:
    la = scope.launch_agents
    runners = []
    no_log = []
    if not la.exists():
        if sys.platform != "darwin" and not os.environ.get("SELFCHECK_LAUNCHAGENTS"):
            # runner 偵測只支援 macOS launchd；別的平台沒有這個目錄是正常，不算 incomplete
            scope.notes.append("runner 偵測只支援 macOS launchd（LaunchAgents）；本平台略過")
            return {"runners": [], "no_log": [], "plists_seen": 0}
        record_incomplete(scope, "runners", "LaunchAgents 目錄不存在：%s" % rel_home(scope, la))
        return {"runners": [], "no_log": [], "plists_seen": 0}
    plists = _sorted_glob(la, "*.plist")
    for p in plists:
        data = safe_open_bytes(scope, p, max_bytes=2 * 1024 * 1024)
        if data is None:
            record_incomplete(scope, "runners", "plist 讀不到：%s" % rel_home(scope, p))
            continue
        try:
            pl = plistlib.loads(data)
        except Exception as exc:
            record_incomplete(scope, "runners", "plist 解析失敗（%s）：%s" % (type(exc).__name__, rel_home(scope, p)))
            continue
        label = str(pl.get("Label") or p.stem)
        args = [str(x) for x in (pl.get("ProgramArguments") or [])]
        if pl.get("Program"):
            args = [str(pl.get("Program"))] + args
        wd = pl.get("WorkingDirectory")
        envs = pl.get("EnvironmentVariables") or {}
        cand = list(args)
        if wd:
            cand.append(str(wd))
        for v in envs.values():
            cand.append(str(v))
        owned = False
        for tok in cand:
            tk = tok.strip()
            if not tk or not (tk.startswith("/") or tk.startswith("~")):
                continue
            try:
                rp = expand_tilde(tk, scope.home).resolve()
            except OSError:
                continue
            for root in scope.allowed_roots:
                if _under(rp, root):
                    owned = True
                    break
            if not owned and scope.mode == "global" and _under(rp, scope.home / ".claude"):
                owned = True
            if owned:
                break
        if not owned:
            continue
        sched = {}
        if pl.get("StartCalendarInterval") is not None:
            sci = pl.get("StartCalendarInterval")
            sched["StartCalendarInterval"] = sci if isinstance(sci, list) else [sci]
        if pl.get("StartInterval") is not None:
            sched["StartInterval"] = pl.get("StartInterval")
        if pl.get("RunAtLoad") is not None:
            sched["RunAtLoad"] = bool(pl.get("RunAtLoad"))
        stdout = pl.get("StandardOutPath")
        stderr = pl.get("StandardErrorPath")
        logs = []
        for lp in (stdout, stderr):
            if not lp:
                continue
            rp = expand_tilde(str(lp), scope.home)
            try:
                rr = rp.resolve()
            except OSError:
                continue
            if _hard_denied(scope, rr) or _under(rr, scope.home / ".config"):
                record_refused(scope, rp, "runner_log_forbidden")
                continue
            if rr not in logs:
                logs.append(rr)
        loaded, last_exit = _launchctl(label)
        if loaded is None and os.environ.get("SELFCHECK_NO_LAUNCHCTL") != "1":
            record_incomplete(scope, "runners", "launchctl print 解析不到狀態：%s" % label)
        runners.append({
            "label": label,
            "program_args": [redact_text(a) for a in args],
            "schedule": sched,
            "stdout": rel_home(scope, stdout) if stdout else None,
            "stderr": rel_home(scope, stderr) if stderr else None,
            "loaded": loaded,
            "last_exit": last_exit,
            "log_paths": [rel_home(scope, x) for x in logs],
            "_logs": logs,
        })
        if not logs:
            no_log.append(label)
    if no_log:
        record_incomplete(scope, "runners", "%d 支 runner 沒有 log 路徑，只能靠 manifest" % len(no_log))
    return {"runners": runners, "no_log": no_log, "plists_seen": len(plists)}


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
LOG_TAIL_BYTES = 5 * 1024 * 1024
_RE_TS1 = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
_RE_TS2 = re.compile(r"^(\d{4}/\d{2}/\d{2}) (\d{2}:\d{2})")
_RE_TS3 = re.compile(r"^(\w{3}) +(\d+) (\d{2}:\d{2})")
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

ERR_PATTERNS = [
    r"traceback", r"\berror\b", r"exception", r"exit(?:ed)? (?:code )?[1-9]\d*",
    r"\bfailed\b", r"command not found", r"no such file", r"permission denied",
    r"time[d]? ?out", r"killed", r"segfault",
]
_RE_ERR = re.compile("|".join(ERR_PATTERNS), re.I)
# 不再有「整行短路」的排除；成功片段只會被刪掉，殘餘文字仍命中錯誤特徵就算錯誤
# （`{"ok": true, "failed": 3}`、`error=0; upload failed`、`no error initially; final exception` 都要留）。
_RE_OK_FRAGS = re.compile(r"\bfail(?:ed|ures?)?\s*[=:]\s*0\b|\berrors?\s*[=:]\s*0\b|\bno errors?\b|(?<![=:])\b0 errors?\b(?!\s*[=:])|\bERROR_OK\b|\"ok\":\s*true", re.I)
_RE_NOTIFY = re.compile(r"notify|notification|sendmessage|send_message|webhook|alert", re.I)
# 成功摘要行裡的 "failed": 0 不是錯誤。只認「有失敗計數且全為零」，
# 🚫 不因為出現 0 就整行放行（那等於再造一次 ERROR_OK 免死金牌）。
_RE_ZERO_KEY = re.compile(r"failed|failures?|fail_count|errors?|error_count|exceptions?|n_errors", re.I)
_RE_BAD_STR = re.compile(r"failed|error|exception|traceback", re.I)
# 剝掉 fail…=0 之後用的殘留偵測：刻意比 _RE_ERR 寬（多認複數形），寬＝少排除＝保守。
_RE_ERR_RESIDUAL = re.compile("|".join(ERR_PATTERNS + [r"\berrors\b", r"\bfailures?\b", r"\bexceptions\b"]), re.I)


_RE_OKFLAG_KEY = re.compile(r"(?i)(success|succeeded|ok)")
_RE_EXIT_KEY = re.compile(r"(?i)(exit_?code|return_?code|rc|status_?code)")
_RE_STATUS_KEY = re.compile(r"(?i)(status|state|error|errors|err|reason|level|outcome|result_?status)")  # 只有這類鍵的字串值算「提到失敗」，訊息內文（text／message）不算


def _flatten_pairs(obj, depth=0, out=None):
    """遞迴攤平 dict／list 的 (鍵, 值)，最多 6 層（原本只看一層）。"""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append((k, v))
            _flatten_pairs(v, depth + 1, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_pairs(v, depth + 1, out)
    return out


def _json_summary_ok(obj) -> bool:
    """成功摘要＝有至少一個失敗計數鍵且全為 0／False／空、沒有字串值提到失敗、
    success／ok 類旗標不是 False、exit_code／rc 類為 0、數值 status 不是 4xx／5xx（後補的三條）。"""
    if not isinstance(obj, dict):
        return False
    pairs = _flatten_pairs(obj)
    zeros = [v for k, v in pairs if isinstance(k, str) and _RE_ZERO_KEY.fullmatch(k)]
    top_ok = any(isinstance(k, str) and _RE_OKFLAG_KEY.fullmatch(k) and v is True for k, v in obj.items())
    if not zeros and not top_ok:
        return False  # 沒有失敗計數鍵、也沒有頂層 ok／success=true 旗標＝不能當成功摘要（通知 API 的 {"ok":true,…} 走旗標這條）
    for v in zeros:
        if v is True:
            return False
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v == 0:
            continue
        if isinstance(v, (list, dict)) and len(v) == 0:
            continue
        return False
    for k, v in pairs:
        if not isinstance(k, str):
            continue
        if isinstance(v, str) and _RE_STATUS_KEY.fullmatch(k) and _RE_BAD_STR.search(v):
            return False  # 通知 API 成功回應的 text 裡提到「failed」是內容不是狀態，不能因此判成錯誤
        if _RE_OKFLAG_KEY.fullmatch(k) and v is False:
            return False
        if _RE_EXIT_KEY.fullmatch(k) and isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0:
            if k.lower().startswith("status") and 200 <= v < 400:
                continue
            return False
        if k.lower() == "status" and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 400:
            return False
    return True


def _zero_summary_line(body: str) -> bool:
    """這一行是不是「失敗數為零」的摘要行；是就不當 log error。"""
    s = body.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except Exception:
            obj = None
        if obj is not None:
            return _json_summary_ok(obj)  # JSON 走 JSON 的判準，不再落到字串規則
    return _RE_ERR_RESIDUAL.search(_RE_OK_FRAGS.sub("", body)) is None


def _parse_ts(line: str, year: int):
    m = _RE_TS1.match(line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1) + "T" + m.group(2) + ":00")
        except ValueError:
            return None
    m = _RE_TS2.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1) + " " + m.group(2), "%Y/%m/%d %H:%M")
        except ValueError:
            return None
    m = _RE_TS3.match(line)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            try:
                return datetime(year, mon, int(m.group(2)),
                                int(m.group(3).split(":")[0]), int(m.group(3).split(":")[1]))
            except ValueError:
                return None
    return None


_RE_SIG_TS = re.compile(r"^\[?\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}(:\d{2})?\]?|^\w{3} +\d+ \d{2}:\d{2}(:\d{2})?")
_RE_SIG_HEX = re.compile(r"\b[0-9a-f]{6,}\b", re.I)
_RE_SIG_NUM = re.compile(r"\d+")
_RE_SIG_PATH = re.compile(r"(/[^\s/]+){3,}")


def normalize_sig(line: str) -> str:
    s = _RE_SIG_TS.sub("", line).strip().lower()
    s = _RE_SIG_PATH.sub(lambda m: "/" + "/".join(m.group(0).strip("/").split("/")[-2:]), s)
    s = _RE_SIG_HEX.sub("#", s)
    s = _RE_SIG_NUM.sub("#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def scan_logs(scope: Scope, log_entries, days: int):
    """log_entries: list of (path, runner_label|None)."""
    groups = {}
    rows = []
    sec_lines = []
    cutoff = now() - timedelta(days=days)
    year = now().year
    for path, label in log_entries:
        lines = safe_lines(scope, path, max_bytes=LOG_TAIL_BYTES, tail=True)
        if lines is None:
            record_incomplete(scope, "logs", "log 讀不到：%s" % rel_home(scope, path))
            continue
        try:
            mtime = datetime.fromtimestamp(Path(path).stat().st_mtime)
        except OSError:
            mtime = now()
        # ts 三態 own／carried／unknown——traceback 的續行沿用同檔前一則自己的戳，
        # 🚫 不往後套第一個戳（那會把舊錯誤搬到今天）；整檔零戳才退回 mtime。
        parsed = []
        any_ts = False
        last_own = None
        for i, ln in enumerate(lines):
            ts = _parse_ts(ln, year)
            if ts:
                any_ts = True
                last_own = ts
                parsed.append((i, ln, ts, "own"))
            elif last_own is not None:
                parsed.append((i, ln, last_own, "carried"))
            else:
                parsed.append((i, ln, None, "unknown"))
        if not any_ts:
            parsed = [(i, ln, mtime, "mtime") for i, ln, _, _ in parsed]
        bodies = dict((i, _RE_SIG_TS.sub("", ln).strip()) for i, ln, _, _ in parsed)
        notify_idx = [i for i, ln, _, _ in parsed if _RE_NOTIFY.search(bodies[i])]
        notify_ts = [(i, t) for i, ln, t, _ in parsed if _RE_NOTIFY.search(bodies[i])]
        file_has_notify = bool(notify_idx)
        for i, ln, ts, src in parsed:
            if not ln.strip():
                continue
            sec_lines.append((path, i + 1, ln))
            body = bodies.get(i, ln)
            if not _RE_ERR.search(body):
                continue
            if _zero_summary_line(body):
                continue
            if ts and ts < cutoff:
                continue
            sig = normalize_sig(ln)
            if not sig:
                continue
            g = groups.get(sig)
            if g is None:
                g = {"sig": sig, "count": 0, "first_seen": None, "last_seen": None,
                     "_days": set(), "_srcs": set(), "unknown_day_count": 0,
                     "files": [], "runner_labels": [], "sample": clip(redact(ln.strip()), 200),
                     "notified_any": False, "file_has_notify_path": False, "ts_source": "unknown"}
                groups[sig] = g
            g["count"] += 1
            g["_srcs"].add(src)
            if src == "unknown":
                g["unknown_day_count"] += 1  # 檔頭尚無戳：只計數，不進日期集合
                dstr = None
            else:
                dstr = (ts or mtime).date().isoformat()
                g["_days"].add(dstr)
                if g["first_seen"] is None or dstr < g["first_seen"]:
                    g["first_seen"] = dstr
                if g["last_seen"] is None or dstr > g["last_seen"]:
                    g["last_seen"] = dstr
            fr = rel_home(scope, path)
            if fr not in g["files"]:
                g["files"].append(fr)
            if label and label not in g["runner_labels"]:
                g["runner_labels"].append(label)
            if file_has_notify:
                g["file_has_notify_path"] = True
            notified = False
            if any_ts and ts:
                for _, nt in notify_ts:
                    if nt and abs((nt - ts).total_seconds()) <= 600:
                        notified = True
                        break
            else:
                for ni in notify_idx:
                    if abs(ni - i) <= 50:
                        notified = True
                        break
            if notified:
                g["notified_any"] = True
            rows.append({"file": fr, "line": i + 1, "ts": dstr or "", "ts_source": src, "sig": sig,
                         "sample": clip(redact(ln.strip()), 200), "runner": label or ""})
    out = []
    for sig, g in groups.items():
        g["days_distinct"] = len(g["_days"])
        srcs = g.pop("_srcs", set())
        if "own" in srcs:
            g["ts_source"] = "line"
        elif "mtime" in srcs:
            g["ts_source"] = "mtime"
        elif "carried" in srcs:
            g["ts_source"] = "carried"
        else:
            g["ts_source"] = "unknown"
        g.pop("_days", None)
        # 照定義：重複出現（≥3 個不同日）而且看不到通知痕跡＝repeated_unnotified；
        # log 裡完全沒有通知路徑也算「看不到」，不因此降級（file_has_notify_path 只是資訊欄）
        if g["days_distinct"] >= 3 and not g["notified_any"]:
            g["classification"] = "repeated_unnotified"
        else:
            g["classification"] = "single_or_notified"
        out.append(g)
    out.sort(key=lambda x: (-x["count"], x["sig"]))
    summary = {
        "files_scanned": len(set(rel_home(scope, p) for p, _ in log_entries)),
        "window_days": days,
        "groups": len(out),
        "repeated_unnotified": len([g for g in out if g["classification"] == "repeated_unnotified"]),
        "single_or_notified": len([g for g in out if g["classification"] == "single_or_notified"]),
        "scope_note": "同一個錯誤簽名跨 log 檔合併計數；通知偵測只看這些 log 檔本身，其他管道的通知看不到；log 裡完全沒有通知路徑的重複錯誤一樣算 repeated_unnotified",
    }
    return rows, summary, out, sec_lines


# --------------------------------------------------------------------------
# security
# --------------------------------------------------------------------------
SECRET_PATTERNS = [
    ("generic_assign", re.compile(r"(?i)(api[_-]?key|secret|token|passwd|password|authorization)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{16,})"), 2),
    ("bearer", re.compile(r"Bearer\s+([A-Za-z0-9\-_\.]{20,})"), 1),
    ("openai", re.compile(r"(sk-[A-Za-z0-9]{20,})"), 1),
    ("github", re.compile(r"(gh[pousr]_[A-Za-z0-9]{30,})"), 1),
    ("slack", re.compile(r"(xox[abpr]-[A-Za-z0-9\-]{10,})"), 1),
    ("google", re.compile(r"(AIza[0-9A-Za-z\-_]{30,})"), 1),
    ("telegram_bot", re.compile(r"\b(\d{8,10}:[A-Za-z0-9_\-]{35})\b"), 1),
    ("pem", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)"), 1),
    ("fb_token", re.compile(r"(EAA[A-Za-z0-9]{40,})"), 1),
]
PLACEHOLDER = re.compile(r"xxx|<your|example|replace|dummy|placeholder|changeme|canary", re.I)
_RE_IDENTISH = re.compile(r"^(os|sys|self|load|get|read|fetch|environ|input|none|true|false|str|int|json|open|path|config|settings|token|secret|api|key|value|data|args|kwargs)[\w.]*$", re.I)


def _plausible_secret(ln: str, m, raw: str) -> bool:
    """generic_assign 只認「引號包住的值」或「至少 2 個數字且不是識別字鏈」；`TOKEN = os.path.join(` 這種不算。"""
    seg = ln[m.start():m.end()]
    quoted = bool(re.search(r"[:=]\s*['\"]", seg))
    nxt = ln[m.end():m.end() + 1]
    if nxt == "(":
        return False
    if quoted:
        return not _RE_IDENTISH.match(raw)
    if "." in raw or _RE_IDENTISH.match(raw):
        return False
    return sum(c.isdigit() for c in raw) >= 2
CRED_NAME = re.compile(r"(^|[._-])(credentials?|token|secrets?|oauth[^/]*|client_secret[^/]*)\.(json|txt|yaml|yml|toml)$|\.(pem|key|p12|token)$|^\.env(\.|$)|^token$|^credentials?$", re.I)
CRED_NAME_EXCLUDE = re.compile(r"\.(example|sample|template|dist)$|\.(py|sh|md|plist|log|out|zsh|bash|js|ts|html|css)$", re.I)


def _is_cred_name(name: str) -> bool:
    """只認憑證檔型（token.json／.env／*.pem…），不再用「檔名含 token」一竿子打翻 .py／.sh／.md／.plist。"""
    return bool(CRED_NAME.search(name)) and not CRED_NAME_EXCLUDE.search(name)
SHELL_HAZARDS = [
    ("curl_pipe_sh", re.compile(r"curl[^|\n]*\|\s*(?:ba|z)?sh")),
    ("eval", re.compile(r"\beval\b")),
    ("rm_rf_var", re.compile(r"rm -rf\s+\"?\$")),
    ("chmod_777", re.compile(r"chmod 777")),
    ("sudo", re.compile(r"\bsudo\b")),
]
_CLOUD_SYNC_SEGMENTS = ("Mobile Documents", "Dropbox", "OneDrive", "Google Drive", "GoogleDrive")  # 各家雲端同步容器的路徑片段
SCAN_SUFFIXES = (".sh", ".py", ".json", ".plist", ".md")  # 讀內容的五種副檔名；其餘只做檔名／權限檢查


def _scan_content(f: Path) -> bool:
    """要不要讀內容做 secret／shell 掃描：上面五種副檔名，或 hooks／bin 底下沒有副檔名的腳本。"""
    if f.suffix.lower() in SCAN_SUFFIXES:
        return True
    if f.suffix == "":
        parts = f.parts
        return ("hooks" in parts or "bin" in parts) and ".venv" not in parts
    return False
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SECURITY_FILES = 6000


def _git_tracked(scope: Scope, root: Path):
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if r.returncode != 0:
            return None, []
        top = Path(r.stdout.decode("utf-8", "replace").strip())
        if not top or not _under(top.resolve(), root.resolve()):
            ok = any(_under(top.resolve(), a) for a in scope.allowed_roots)
            if not ok and scope.mode == "global" and top.resolve() == (scope.home / ".claude").resolve():
                ok = True  # 全局層 repo 頂層＝~/.claude，只拿來 ls-files 列名；每個檔讀前仍過 in_scope
            if not ok:
                record_refused(scope, top, "git_toplevel_out_of_scope")
                return None, []
        r2 = subprocess.run(["git", "-C", str(top), "ls-files", "-z"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        if r2.returncode != 0:
            return top, []
        names = [x for x in r2.stdout.decode("utf-8", "replace").split("\0") if x]
        return top, [top / n for n in names]
    except Exception:
        return None, []


def _walk_files(scope: Scope, root: Path, max_depth=4):
    out = []
    try:
        if root.is_file():
            return [root]
        if not root.exists():
            return []
    except OSError:
        return []
    base = len(root.resolve().parts)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            if e.name in SKIP_DIR_PARTS or e.name.startswith(".bak"):
                continue
            try:
                rp = p.resolve()
            except (OSError, RuntimeError):  # symlink 迴圈丟的是 RuntimeError 不是 OSError；只接這兩種，別吞其他例外
                record_refused(scope, p, "resolve_error")
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:  # Python 3.14 起 symlink 迴圈在 is_dir 就丟 ELOOP（3.13 以前 resolve 先丟）；一律記 refused 不炸整輪
                record_refused(scope, p, "resolve_error")
                continue
            if is_dir:
                if rp in [x.resolve() for x in scope.list_only_dirs]:
                    stack.append(p)
                    continue
                if not in_scope(scope, rp):
                    record_refused(scope, p, "out_of_scope")
                    continue
                if _under(rp, scope.transcript_dir) and not _under(rp, scope.memory_dir) and rp != scope.transcript_dir:
                    continue
                if len(rp.parts) - base >= max_depth:
                    continue
                stack.append(p)
            else:
                if not in_scope(scope, rp):
                    record_refused(scope, p, "out_of_scope")
                    continue
                if _excluded(scope, rp):
                    continue
                if _under(rp, scope.transcript_dir) and not _under(rp, scope.memory_dir):
                    continue  # transcript 與它的 tool-results 側車只產計數，不進 security 讀取
                out.append(rp)
    return out


def _hazard_skip_lines(f: Path, text: str):
    """回傳 .py 檔中「註解＋docstring」的行號集合；非 .py 回 None（由呼叫端逐行看 #）。

    只用來擋 SHELL_HAZARDS；SECRET_PATTERNS 照掃註解（註解裡的 token 一樣是外洩）。
    tokenize 或 ast 任一失敗就回空集合＝寧可多報。
    """
    if f.suffix.lower() != ".py":
        return None
    skip = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                skip.add(tok.start[0])
    except Exception:
        return set()
    try:
        tree = ast.parse(text)
    except Exception:
        return set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        val = getattr(first, "value", None)
        if isinstance(first, ast.Expr) and isinstance(val, ast.Constant) and isinstance(val.value, str):
            start = getattr(first, "lineno", 0)
            end = getattr(first, "end_lineno", start) or start
            for i in range(start, end + 1):
                skip.add(i)
    return skip


def _sec_allowed(scope: Scope, rel: str, pattern: str, line_text: str = "") -> bool:
    for a in scope.manifest.get("security_allow") or []:
        if not isinstance(a, dict):
            continue
        f = str(a.get("file") or "")
        pt = str(a.get("pattern") or "")
        if not f and not pt:
            continue  # 防禦性略過；正常情況 load_manifest 已用 ManifestError 擋掉全空條目（exit 2）
        if pt and pt != pattern:
            continue
        if f and not (rel.endswith(f) or Path(rel).match(f)):
            continue
        sc = str(a.get("snippet_contains") or "")
        if sc and sc not in (line_text or ""):
            continue  # 內容錨——放行綁在那一行的文字上，檔案改了就自動失效
        return True
    return False


def scan_security(scope: Scope, log_lines, runners_info):
    hits = []
    tracked = set()
    files = []
    seen = set()
    for root in list(scope.allowed_roots) + list(scope.list_only_dirs):
        try:
            if not root.exists():
                continue
        except OSError:
            continue
        top, tfiles = (None, [])
        if root.is_dir() and root not in scope.list_only_dirs:
            top, tfiles = _git_tracked(scope, root)
        if tfiles:
            for f in tfiles:
                try:
                    rp = f.resolve()
                except OSError:
                    continue
                tracked.add(str(rp))
                if not in_scope(scope, rp):
                    continue
                if str(rp) not in seen:
                    seen.add(str(rp))
                    files.append(rp)
        for f in _walk_files(scope, root):
            if str(f) not in seen:
                seen.add(str(f))
                files.append(f)
    if len(files) > MAX_SECURITY_FILES:
        record_incomplete(scope, "security", "檔案數 %d 超過上限 %d，只掃前 %d 個" % (len(files), MAX_SECURITY_FILES, MAX_SECURITY_FILES))
        files = files[:MAX_SECURITY_FILES]

    def add(entry):
        hits.append(entry)

    for f in files:
        rel = rel_home(scope, f)
        st = safe_stat(scope, f)
        if st is None:
            continue
        if _is_cred_name(f.name):
            if st.st_mode & 0o077:
                add({"kind": "perm", "file": rel, "line": 0, "pattern": "mode",
                     "mode": oct(st.st_mode & 0o777), "severity": "medium"})
            s = str(f)
            if any(seg in s for seg in _CLOUD_SYNC_SEGMENTS):  # 憑證檔住在雲端同步資料夾＝會被同步到別台機器
                add({"kind": "cloud_synced_credential", "file": rel, "line": 0, "pattern": "cloud-sync", "severity": "medium"})
            if str(f) in tracked:
                add({"kind": "tracked_credential", "file": rel, "line": 0, "pattern": "git", "severity": "high"})
        if not _scan_content(f):
            continue  # 圖片／影片／log 等非目標型別不進內容掃描（先過型別再談大小，否則 incomplete 會被圖檔洗版）
        if st.st_size > MAX_FILE_BYTES:
            record_incomplete(scope, "security", "檔案 >2MB 跳過：%s" % rel)
            continue
        text = safe_open(scope, f, max_bytes=MAX_FILE_BYTES)
        if text is None:
            continue
        is_shellish = f.suffix.lower() in (".sh", ".zsh", ".bash") or "/hooks/" in str(f) or "/bin/" in str(f)
        py_skip = _hazard_skip_lines(f, text) if is_shellish else None
        for i, ln in enumerate(text.splitlines(), 1):
            for name, rx, gi in SECRET_PATTERNS:
                m = rx.search(ln)
                if not m:
                    continue
                raw = m.group(gi)
                if PLACEHOLDER.search(raw) or PLACEHOLDER.search(ln[m.end():m.end() + 40]):
                    continue  # 只看值本身與值後 40 字（註解），不看整行
                if name == "generic_assign" and not _plausible_secret(ln, m, raw):
                    continue
                if _sec_allowed(scope, rel, name, ln):
                    continue
                add({"kind": "secret", "file": rel, "line": i, "pattern": name,
                     "masked": mask_value(raw),
                     "tracked_in_git": str(f) in tracked,
                     "severity": "high" if str(f) in tracked else "medium"})
            if is_shellish:
                in_comment = (i in py_skip) if py_skip is not None else ln.lstrip().startswith("#")
                if not in_comment:
                    for name, rx in SHELL_HAZARDS:
                        if rx.search(ln) and not _sec_allowed(scope, rel, name, ln):
                            add({"kind": "shell_hazard", "file": rel, "line": i, "pattern": name,
                                 "snippet": clip(redact(ln.strip()), 120), "severity": "low"})

    for path, lineno, ln in log_lines:
        rel = rel_home(scope, path)
        for name, rx, gi in SECRET_PATTERNS:
            m = rx.search(ln)
            if not m:
                continue
            raw = m.group(gi)
            if PLACEHOLDER.search(raw) or _sec_allowed(scope, rel, name, ln):
                continue
            if name == "generic_assign" and not _plausible_secret(ln, m, raw):
                continue
            add({"kind": "secret", "file": rel, "line": lineno, "pattern": name,
                 "masked": mask_value(raw), "tracked_in_git": False, "severity": "high"})

    for r in runners_info.get("runners") or []:
        for i, a in enumerate(r.get("program_args") or []):
            for name, rx in SHELL_HAZARDS:
                if rx.search(a) and not _sec_allowed(scope, "plist:" + r["label"], name, a):
                    add({"kind": "shell_hazard", "file": "plist:" + r["label"], "line": i,
                         "pattern": name, "snippet": clip(a, 120), "severity": "low"})

    for sp in _settings_paths(scope):
        text = safe_open(scope, sp, max_bytes=MAX_FILE_BYTES)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except Exception as exc:
            record_incomplete(scope, "security", "settings 解析失敗（%s）：%s" % (type(exc).__name__, rel_home(scope, sp)))
            continue
        rel = rel_home(scope, sp)
        perms = (data.get("permissions") or {}) if isinstance(data, dict) else {}
        for entry in (perms.get("allow") or []):
            e = str(entry)
            if "(*)" in e or "/**)" in e or e.startswith("Bash(*"):
                add({"kind": "wide_permission", "file": rel, "line": 0, "pattern": "allow",
                     "entry": e, "severity": "low"})
        for key in ("defaultMode", "bypassPermissions", "dangerouslySkipPermissions"):
            if isinstance(perms, dict) and key in perms:
                add({"kind": "permission_mode", "file": rel, "line": 0, "pattern": key,
                     "entry": key + "=" + str(perms.get(key)), "severity": "low"})
            if isinstance(data, dict) and key in data:
                add({"kind": "permission_mode", "file": rel, "line": 0, "pattern": key,
                     "entry": key, "severity": "low"})
    return hits


def _settings_paths(scope: Scope):
    out = _sorted_glob(scope.project_root / ".claude", "settings*.json")
    if scope.mode == "global":
        sp = scope.home / ".claude" / "settings.json"
        if sp.exists():
            out = list(out) + [sp]
    return out


# --------------------------------------------------------------------------
# existing audits（global 模式）
# --------------------------------------------------------------------------
# 預設空：本工具不預設你裝了哪些稽核器。要檢查自己的排程稽核器有沒有停跑，
# 在 manifest 寫 {"audits": [{"name": "...", "path": "~/...json", "cycle": "日", "stale_days": 2}]}。
def _audit_table(scope: Scope):
    return _audit_table_raw(scope.manifest)


def _threshold_from_plist(scope: Scope, name: str, default_days: int):
    la = scope.launch_agents
    for p in _sorted_glob(la, "*.plist"):
        if name not in p.name:
            continue
        data = safe_open_bytes(scope, p, max_bytes=1024 * 1024)
        if data is None:
            continue
        try:
            pl = plistlib.loads(data)
        except Exception:
            continue
        sci = pl.get("StartCalendarInterval")
        if sci is None:
            continue
        items = sci if isinstance(sci, list) else [sci]
        weekly = any(isinstance(x, dict) and "Weekday" in x for x in items)
        return (8 if weekly else 2), "plist"
    return default_days, "default"


def scan_existing_audits(scope: Scope) -> dict:
    if scope.mode != "global":
        return {"mode": scope.mode, "checked": False, "items": []}
    items = []
    for name, path_tpl, cycle, default_days in _audit_table(scope):
        thr, thr_src = _threshold_from_plist(scope, name, default_days)
        rel = path_tpl
        if rel is None:
            items.append({"name": name, "exists": False, "path": None, "path_unknown": True,
                          "cycle": cycle, "threshold_days": thr, "threshold_source": thr_src,
                          "mtime": None, "age_days": None, "stale": None, "summary_keys": []})
            record_incomplete(scope, "existing_audits", "%s：產出路徑未知（path_unknown）" % name)
            continue
        p = expand_tilde(rel, scope.home)
        st = safe_stat(scope, p) if p.exists() else None
        if st is None:
            items.append({"name": name, "exists": False, "path": rel, "path_unknown": False,
                          "cycle": cycle, "threshold_days": thr, "threshold_source": thr_src,
                          "mtime": None, "age_days": None, "stale": None, "summary_keys": []})
            record_incomplete(scope, "existing_audits", "%s：產出不存在（%s）" % (name, rel))
            continue
        mtime = datetime.fromtimestamp(st.st_mtime)
        age = (now() - mtime).total_seconds() / 86400.0
        stale = age > thr
        keys = []
        text = safe_open(scope, p, max_bytes=1024 * 1024)
        if text is not None:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    keys = [{"key": k, "value_type": type(v).__name__} for k, v in data.items()
                            if "/" not in str(k) and len(str(k)) <= 40][:10]  # 鍵名若是路徑（別專案的 memory 目錄）不列
            except Exception as exc:
                record_incomplete(scope, "existing_audits", "%s：產出 JSON 解析失敗（%s）" % (name, type(exc).__name__))
        items.append({"name": name, "exists": True, "path": rel, "path_unknown": False,
                      "cycle": cycle, "threshold_days": thr, "threshold_source": thr_src,
                      "mtime": mtime.isoformat(timespec="seconds"), "age_days": round(age, 2),
                      "stale": stale, "summary_keys": keys})
        if stale:
            record_incomplete(scope, "existing_audits", "%s 過期（%.1f 天 > %d）" % (name, age, thr))
    return {"mode": scope.mode, "checked": True, "items": items,
            "summary_note": "summary 只列頂層鍵名與值型別，不含值（避免既有稽核器內容外流）"}


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------
HOTSPOT_MIN_TOKENS = 500
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def _fp(dimension: str, first_file: str, title: str) -> str:
    norm = re.sub(r"\d+", "", title.lower())
    norm = re.sub(r"\s+", "", norm)
    return hashlib.sha1(("%s|%s|%s" % (dimension, first_file, norm)).encode("utf-8")).hexdigest()[:12]


def _cand(dimension, severity, confidence, title, evidence, proposal, owner_project, owner="agent",
          triage=None, auto_reason=None):
    """triage／auto_reason 為選填：缺＝human（要人逐條核）；"auto"＝機械降級，人核只看附錄計數與抽樣。"""
    ev = []
    for e in evidence[:5]:
        ev.append({"file": e.get("file", ""), "line": int(e.get("line") or 0),
                   "snippet": clip(redact(str(e.get("snippet") or "")), 120)})
    if not ev:
        ev = [{"file": "", "line": 0, "snippet": ""}]
    c = {"fp": _fp(dimension, ev[0]["file"], title), "dimension": dimension,
         "severity": severity, "confidence": confidence, "title": clip(title, 200),
         "evidence": ev, "proposal": clip(proposal, 200), "owner_project": owner_project,
         "owner": owner, "source": "scanner"}
    if triage == "auto":
        c["triage"] = "auto"
        c["auto_reason"] = clip(str(auto_reason or ""), 200)
    return c


def _owner_project(scope: Scope) -> str:
    """發現歸屬哪個專案：manifest 的 owner_project 優先，否則用專案根目錄名。"""
    if scope.owner_project:
        return scope.owner_project
    return scope.project_root.name


def _too_young(scope, path, win):
    """檔案比 usage 窗還新＝窗內零引用不構成訊號；回 (是否降級, 檔齡天數)。"""
    st = safe_stat(scope, path)
    if st is None:
        return False, None
    birth = getattr(st, "st_birthtime", None)
    if birth is None:
        # 沒有 birthtime 的平台／檔案系統（多數 Linux）：用 mtime 與 ctime 較早者估建立時間——
        # ctime 不可能早於建立、mtime 可能被回溯，取小的最接近；拿不到就不降級（維持 human）
        try:
            birth = min(st.st_mtime, st.st_ctime)
        except AttributeError:
            return False, None
    try:
        age = (now() - datetime.fromtimestamp(birth)).days
    except (ValueError, OverflowError, OSError):
        return False, None
    return (age < win), age


def build_candidates(scope, inventory, rules, usage, dead_refs, runners, log_groups, security, audits):
    out = []
    op = _owner_project(scope)
    too_young = {"memory": [], "skills": [], "bin": []}

    # scope：slug 碰撞
    if scope.slug_collisions:
        out.append(_cand("security", "medium", "confirmed",
                         "專案 slug 碰撞：memory／transcript 目錄共用",
                         [{"file": rel_home(scope, scope.project_root), "line": 0,
                           "snippet": "同 slug 目錄：" + "、".join(scope.slug_collisions[:5])}],
                         "把其中一個專案目錄改名，讓 slug 不再相同；改名前先確認 memory 與 transcript 的搬遷方式",
                         op, human_owner()))

    # unused：memory
    win = usage.get("window_days", 30)
    mem_usage = usage.get("memory") or {}
    for item in inventory:
        if item["layer"] != "memory" or item["role"] != "audited":
            continue
        name = Path(item["path_rel"]).stem
        if name == "MEMORY":
            continue
        if (mem_usage.get(name) or {}).get("count", 0) == 0:
            young, age = _too_young(scope, expand_tilde(item["path_rel"], scope.home), win)
            if young:
                too_young["memory"].append(name)
            out.append(_cand("unused", "low", "suspected",
                             "疑似未使用（%d天零引用）：memory:%s" % (win, name),
                             [{"file": item["path_rel"], "line": 0, "snippet": "transcript 窗內 0 次 Read／Bash 引用"}],
                             "確認是否仍需保留；要留就在 MEMORY.md 索引補鉤子，要退役就刪掉或移到你慣用的封存位置", op,
                             triage=("auto" if young else None),
                             auto_reason=("窗內新檔：檔齡 %d 天 < 窗 %d 天，零引用不可判" % (age, win) if young else None)))
    # unused：skill
    sk_usage = usage.get("skills") or {}
    for item in inventory:
        if item["layer"] != "skill" or item["role"] != "audited":
            continue
        name = Path(item["path_rel"]).parent.name
        if (sk_usage.get(name) or {}).get("count", 0) == 0:
            young, age = _too_young(scope, expand_tilde(item["path_rel"], scope.home), win)
            if young:
                too_young["skills"].append(name)
            out.append(_cand("unused", "low", "suspected",
                             "疑似未使用（%d天零引用）：skill:%s" % (win, name),
                             [{"file": item["path_rel"], "line": 0, "snippet": "transcript 窗內 0 次 Skill／command-name／Bash 引用"}],
                             "skill 可能靠 description 匹配觸發、不留呼叫痕跡；先看用途再決定退役", op,
                             triage=("auto" if young else None),
                             auto_reason=("窗內新檔：檔齡 %d 天 < 窗 %d 天，零引用不可判" % (age, win) if young else None)))
    # unused：bin（global）
    if scope.mode == "global":
        bin_usage = usage.get("bin") or {}
        plist_tokens = " ".join(" ".join(r.get("program_args") or []) for r in (runners.get("runners") or []))
        hook_text = ""
        for sp in _settings_paths(scope):
            t = safe_open(scope, sp, max_bytes=MAX_FILE_BYTES)
            if t:
                hook_text += t
        for p in _sorted_glob(scope.home / ".claude", "bin/*"):
            if not p.is_file():
                continue
            nm = p.name
            if (bin_usage.get(nm) or {}).get("count", 0) or (bin_usage.get(p.stem) or {}).get("count", 0):
                continue
            if nm in plist_tokens or p.stem in plist_tokens or nm in hook_text:
                continue
            young, age = _too_young(scope, p, win)
            if young:
                too_young["bin"].append(nm)
            out.append(_cand("unused", "low", "suspected",
                             "疑似未使用（%d天零引用）：bin:%s" % (win, nm),
                             [{"file": rel_home(scope, p), "line": 0, "snippet": "transcript 窗內 0 次引用、不在 plist／settings"}],
                             "確認是否還有別台機器或別的 session 在用；沒有就登記退役並更新你的工具清單文件", op,
                             triage=("auto" if young else None),
                             auto_reason=("窗內新檔：檔齡 %d 天 < 窗 %d 天，零引用不可判" % (age, win) if young else None)))

    # dead refs
    for d in dead_refs:
        tri, why = None, None
        if d.get("exists_elsewhere"):
            tri, why = "auto", "範圍外同名檔存在：%s" % d["exists_elsewhere"]
        elif d.get("cross_machine"):
            tri, why = "auto", "行內有跨機標記，無法在本機驗"
        out.append(_cand("scope", "medium", "suspected",
                         "死路徑引用：%s ← %s" % (d["ref"], d["file"]),
                         [{"file": d["file"], "line": d["line"], "snippet": "%s（%s）不存在" % (d["ref"], d["kind"])}],
                         "改成現行路徑，或把已退役的說明整段刪掉", op,
                         triage=tri, auto_reason=why))

    # log errors
    for g in log_groups:
        sev = "high" if g["classification"] == "repeated_unnotified" else ("medium" if g["count"] >= 5 else "low")
        who = (g["runner_labels"] or g["files"] or [""])[0]
        out.append(_cand("log_error", sev,
                         "confirmed" if g["classification"] == "repeated_unnotified" else "suspected",
                         "log error（%s，%d 次／%d 天）：%s｜%s" % (g["classification"], g["count"], g["days_distinct"], who, clip(g["sig"], 60)),
                         [{"file": g["files"][0] if g["files"] else "", "line": 0, "snippet": g["sample"]}],
                         "查這支 runner 的失敗原因；重複且無通知痕跡的要補告警路徑", op))

    # security
    for h in security:
        detail = h.get("pattern") or h.get("mode") or ""
        if h["kind"] == "perm":
            detail = h.get("mode", "")
        out.append(_cand("security", h.get("severity", "low"),
                         "confirmed" if h["kind"] in ("perm", "wide_permission", "permission_mode", "cloud_synced_credential", "tracked_credential") else "suspected",
                         "security（%s，%s）：%s:%s｜%s" % (h["kind"], h.get("severity", "low"), h["file"], h.get("line", 0), detail),
                         [{"file": h["file"], "line": h.get("line", 0),
                           "snippet": h.get("snippet") or h.get("entry") or h.get("masked") or detail}],
                         "開檔看上下文；真的是憑證就換掉並移出 repo／雲端同步資料夾，佔位值可加進 security_allow", op,
                         human_owner() if h.get("severity") == "high" else "agent"))

    # existing audits stale
    for a in (audits.get("items") or []):
        if a.get("stale"):
            out.append(_cand("optimize", "medium", "confirmed",
                             "既有稽核器過期：%s 上次 %s" % (a["name"], (a.get("mtime") or "")[:10]),
                             [{"file": a.get("path") or a["name"], "line": 0,
                               "snippet": "age %.1f 天 > 門檻 %d 天（門檻來源 %s）" % (a.get("age_days") or 0, a.get("threshold_days") or 0, a.get("threshold_source"))}],
                             "確認那支稽核器還在跑嗎；沒在跑就修排程或正式退役", op, human_owner()))

    # optimize：always_on 熱點
    hot = [i for i in inventory if i["always_on"] and i["role"] == "audited" and i["est_tokens"] >= HOTSPOT_MIN_TOKENS]
    hot.sort(key=lambda x: -x["est_tokens"])
    for i in hot[:5]:
        out.append(_cand("optimize", "low", "suspected",
                         "常駐脈絡熱點：%s（約 %d tokens）" % (i["path_rel"], i["est_tokens"]),
                         [{"file": i["path_rel"], "line": 0, "snippet": i["always_on_reason"]}],
                         "壓縮、搬到 on-demand 或合併重複段落；估可省的單位寫進報告第 4 段", op))
    # optimize：索引單行過長／skill description 過長
    for i in inventory:
        if i["role"] != "audited":
            continue
        if i["layer"] in ("inject", "import"):
            continue  # 常駐列只進熱點段，不進索引行長度／skill description 檢查
        if Path(i["path_rel"]).name == "MEMORY.md":  # 記憶索引每個 session 載入，單行太長就是常駐成本
            p = expand_tilde(i["path_rel"], scope.home)
            text = safe_open(scope, p, max_bytes=MAX_FILE_BYTES)
            if text:
                for ln_i, ln in enumerate(text.splitlines(), 1):
                    if _utf16_units(ln) > 300:
                        out.append(_cand("optimize", "low", "confirmed",
                                         "索引單行過長：%s:%d（%d 單位 > 300）" % (i["path_rel"], ln_i, _utf16_units(ln)),
                                         [{"file": i["path_rel"], "line": ln_i, "snippet": clip(ln, 120)}],
                                         "把細節搬進該條目的 .md，索引行只留一句鉤子", op))
        if i["layer"] == "skill" and i["utf16_units"] > 400:
            out.append(_cand("optimize", "low", "suspected",
                             "skill description 過長：%s（%d 單位 > 400）" % (i["path_rel"], i["utf16_units"]),
                             [{"file": i["path_rel"], "line": 0, "snippet": "frontmatter 常駐於每個 session"}],
                             "把觸發詞收斂、把說明搬進 SKILL.md 正文", op))

    usage["too_young"] = too_young  # 把「太新所以不可判」留在事實層，不只留在候選欄位
    usage["too_young_count"] = sum(len(v) for v in too_young.values())

    dedup = {}
    for c in out:
        prev = dedup.get(c["fp"])
        if prev is None or SEV_ORDER[c["severity"]] < SEV_ORDER[prev["severity"]]:
            dedup[c["fp"]] = c
    res = list(dedup.values())
    res.sort(key=lambda c: (SEV_ORDER[c["severity"]], c["dimension"], c["title"]))
    return res


# --------------------------------------------------------------------------
# run_scan
# --------------------------------------------------------------------------
def _collect_extra_texts(scope: Scope):
    out = []
    roots = [scope.project_root / ".claude"]
    if scope.mode == "global":
        roots.append(scope.home / ".claude")
    seen = set()
    for base in roots:
        for pat in ("skills/*/SKILL.md", "hooks/*"):
            for p in _sorted_glob(base, pat):
                if not p.is_file() or str(p) in seen:
                    continue
                seen.add(str(p))
                t = safe_open(scope, p, max_bytes=1024 * 1024)
                if t is not None:
                    out.append(("%s:%s" % ("skill" if p.name == "SKILL.md" else "hook", rel_home(scope, p)), t))
    return out


_RE_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]*$")


def _name_safe_to_keep(name: str) -> bool:
    """白名單來自受掃專案的檔名，不可信——形似 token 的檔名不得進白名單。
    要求：只含 [A-Za-z0-9_-.]、不命中任何 secret 特徵、以 -_. 切開後至少兩段是 ≥3 字的純字母（slug 形狀）。"""
    if not name or not _RE_NAME_OK.match(name):
        return False
    for _n, rx, _gi in SECRET_PATTERNS:
        if rx.search(name):
            return False
    segs = [s for s in re.split(r"[-_.]", name) if s]
    alpha = [s for s in segs if s.isalpha() and len(s) >= 3]
    return len(alpha) >= 2


def _fill_known_names(inventory):
    """把 inventory 的自家名稱收進 _KNOWN_NAMES（memory 檔名 stem／skill 目錄名／bin 檔名／hook 檔名）；
    只收 slug 形狀的名字（_name_safe_to_keep），形似 token 的檔名照舊遮。"""
    cands = set()
    for item in inventory:
        p = Path(item["path_rel"])
        layer = item.get("layer")
        if layer == "memory":
            cands.add(p.stem)
        elif layer == "skill":
            cands.add(p.parent.name)
        elif layer in ("bin", "hook"):
            cands.add(p.name)
            cands.add(p.stem)
    for nm in cands:
        if _name_safe_to_keep(nm):
            _KNOWN_NAMES.add(nm)
    _KNOWN_NAMES.discard("")


def run_scan(args) -> int:
    reset_state()
    try:
        scope = resolve_scope(args.cwd)
    except (ScopeError, ManifestError) as exc:
        sys.stderr.write("[selfcheck] 致命：%s\n" % exc)
        return 2

    if getattr(args, "list_scope", False):
        sys.stdout.write(json.dumps(_sanitize(scope_dict(scope), 400), ensure_ascii=False, indent=2) + "\n")
        return 0

    ts = now()
    date_s = ts.strftime("%Y-%m-%d")
    run_id = "%s-%s" % (date_s, ts.strftime("%H%M"))
    if args.out:
        out = Path(str(args.out)).expanduser()
        try:
            rp = out.resolve()
        except OSError:
            rp = out
        if args.dry_run:
            if _under(rp, scope.project_root):
                sys.stderr.write("[selfcheck] 致命：--dry-run 的 --out 不得落在專案根底下（%s）\n" % out)
                return 2
        else:
            # 硬規則：正式跑只准寫 <專案根>/.claude/systems-check/ 底下（latest 也只會指到這裡）
            base = scope.project_root / ".claude" / "systems-check"
            if rp == base or not _under(rp, base):
                sys.stderr.write("[selfcheck] 致命：--out 只准落在 %s 底下的子目錄（%s）\n" % (base, out))
                return 2
    elif args.dry_run:
        out = Path(tempfile.gettempdir()) / ("selfcheck-%s-%d" % (date_s, os.getpid()))
    else:
        out = scope.project_root / ".claude" / "systems-check" / run_id
    out.mkdir(parents=True, exist_ok=True)

    started = ts.isoformat(timespec="seconds")
    inventory = build_inventory(scope)
    augment_context_inventory(scope, inventory)  # inject／import 常駐列
    _fill_known_names(inventory)  # 自家名稱不再被當成密鑰遮掉
    rules = extract_rules(scope, inventory)
    buckets = write_topic_buckets(rules, out)
    usage = scan_usage(scope, args.usage_days)
    runners = scan_runners(scope)

    log_entries = []
    seen_logs = set()
    for r in runners.get("runners") or []:
        for lp in r.get("_logs") or []:
            if str(lp) in seen_logs:
                continue
            seen_logs.add(str(lp))
            if lp not in scope.allowed_files:
                scope.allowed_files.append(lp)
            if lp.exists():
                log_entries.append((lp, r["label"]))
            else:
                record_incomplete(scope, "logs", "runner %s 宣告的 log 不存在：%s" % (r["label"], rel_home(scope, lp)))
    for lp in scope.allowed_files:
        if str(lp) in seen_logs or lp in scope.ctx_files:
            continue
        if lp.suffix in (".log", ".out") or ".out" in lp.name:
            seen_logs.add(str(lp))
            log_entries.append((lp, None))
    # code_roots 底下的 logs/*.log 與 *.log 自動納入（錯誤掃描＋secret 掃描），上限 200 檔
    extra_logs = []
    for root in scope.allowed_roots:
        if root in (scope.project_root, scope.memory_dir, scope.transcript_dir, scope.launch_agents):
            continue
        for pat in ("logs/*.log", "logs/*.out", "*.log"):
            for lp in _sorted_glob(root, pat):
                if lp.is_file() and str(lp) not in seen_logs:
                    seen_logs.add(str(lp))
                    extra_logs.append(lp)
    if len(extra_logs) > 200:
        record_incomplete(scope, "logs", "code_roots 底下 log 超過 200 檔，只掃最新 200")
        extra_logs = sorted(extra_logs, key=lambda x: _mtime_or_zero(x), reverse=True)[:200]
    for lp in extra_logs:
        if lp not in scope.allowed_files:
            scope.allowed_files.append(lp)
        log_entries.append((lp, None))

    log_rows, log_summary, log_groups, sec_log_lines = scan_logs(scope, log_entries, args.days)
    security = scan_security(scope, sec_log_lines, runners)
    audits = scan_existing_audits(scope)
    dead = find_dead_refs(scope, rules, _collect_extra_texts(scope))
    candidates = build_candidates(scope, inventory, rules, usage, dead, runners, log_groups, security, audits)

    if scope.refused:
        record_incomplete(scope, "scope", "refused 非空（%d 筆），部分路徑未掃" % len(scope.refused))

    runners_public = {"runners": [dict((k, v) for k, v in r.items() if not k.startswith("_")) for r in (runners.get("runners") or [])],
                      "no_log": runners.get("no_log") or [], "plists_seen": runners.get("plists_seen", 0)}

    emit(out / "scope.json", scope_dict(scope), maxlen=400)
    emit(out / "inventory.json", inventory, maxlen=400)
    emit_jsonl(out / "rules.jsonl", rules, maxlen=300)
    emit(out / "usage.json", usage, maxlen=300)
    emit_jsonl(out / "dead_refs.jsonl", dead, maxlen=300)
    emit(out / "runners.json", runners_public, maxlen=300)
    emit_jsonl(out / "logs.jsonl", log_rows, maxlen=200)
    emit(out / "logs_summary.json", {"summary": log_summary, "groups": log_groups}, maxlen=200)
    emit_jsonl(out / "security.jsonl", security, maxlen=200)
    emit(out / "existing_audits.json", audits, maxlen=200)
    emit_jsonl(out / "candidates.jsonl", candidates, maxlen=200)

    status = "partial" if scope.incomplete else "complete"
    meta = {
        "version": VERSION, "mode": scope.mode, "run_id": run_id,
        "run_mode": ("dry-run" if args.dry_run else ("report-only" if getattr(args, "report_only", False) else ("auto" if getattr(args, "auto", False) else "interactive"))),
        "status": status, "incomplete": scope.incomplete, "notes": list(scope.notes),
        "started_at": started, "finished_at": now().isoformat(timespec="seconds"),
        "out_dir": str(out),
        "counts": {
            "inventory": len(inventory), "rules": len(rules), "topics": len(buckets),
            "usage_transcripts": usage.get("transcripts_scanned", 0),
            "dead_refs": len(dead), "runners": len(runners_public["runners"]),
            "log_groups": len(log_groups), "security": len(security),
            "candidates": len(candidates), "refused": len(scope.refused),
            "candidates_auto": len([c for c in candidates if c.get("triage") == "auto"]),
            "candidates_human": len([c for c in candidates if c.get("triage") != "auto"]),
            "candidates_by_severity": {
                "high": len([c for c in candidates if c["severity"] == "high"]),
                "medium": len([c for c in candidates if c["severity"] == "medium"]),
                "low": len([c for c in candidates if c["severity"] == "low"]),
            },
        },
        "topic_files": buckets,
    }
    emit(out / "scan_meta.json", meta, maxlen=400)
    emit_raw(out / "access.log", "\n".join(_ACCESS) + "\n")

    if not args.dry_run:
        base = scope.project_root / ".claude" / "systems-check"
        link = base / "latest"
        try:
            if link.is_symlink() or link.exists():
                if link.is_symlink():
                    link.unlink()
            os.symlink(str(out), str(link))
        except OSError:
            emit_text(base / "latest.txt", run_id + "\n")

    sys.stdout.write("[selfcheck] mode=%s status=%s out=%s candidates=%d（high %d／medium %d／low %d）refused=%d\n" % (
        scope.mode, status, out, len(candidates),
        meta["counts"]["candidates_by_severity"]["high"],
        meta["counts"]["candidates_by_severity"]["medium"],
        meta["counts"]["candidates_by_severity"]["low"], len(scope.refused)))
    if args.verbose:
        for it in scope.incomplete:
            sys.stdout.write("  incomplete: %s｜%s\n" % (it["section"], it["reason"]))
    return 1 if status == "partial" else 0


def build_parser():
    ap = argparse.ArgumentParser(prog="selfcheck_scan.py", description="systems-check 掃描器：對目前這個專案做一次唯讀體檢")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--usage-days", dest="usage_days", type=int, default=30)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--report-only", dest="report_only", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list-scope", dest="list_scope", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--global", dest="global_layer", action="store_true",
                    help="把 ~/.claude 全局層一併納入掃描（預設關；等同 manifest 的 global_layer）")
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    GLOBAL_LAYER[0] = bool(getattr(args, "global_layer", False))
    if args.selftest:
        return selftest()
    if args.cwd is None:
        args.cwd = os.getcwd()
    return run_scan(args)


# --------------------------------------------------------------------------
# selftest（fixture 全部由程式碼生成，不放靜態檔）
# --------------------------------------------------------------------------
_FAKE_SECRET = "abcd1234" + "efgh5678" + "ijkl9012"  # 拆開寫，免得掃描器掃到自己的 fixture
_FAKE_GHP_NAME = "ghp_" + "abcdefghij1234567890" + "abcdefghij123456"  # 同理，掃全局層時抓到自己這行（high）
_FX_SECRET_A = "mnop3456" + "qrst7890" + "uvwx1234"  # 案 28 用，同樣拆開寫
_FX_SECRET_B = "mnop3456" + "qrst7890" + "uvwx5678"
CANARIES_CONTENT = ["CANARY_B_7f3a", "CANARY_B_MEM", "CANARY_FN_5b2e", "CANARY_ENV_2b8f",
                    "CANARY_GIT_3a91", "CANARY_LOG_8d4c", "CANARY_AUD_4e7b", "CANARY_EXC",
                    _FAKE_SECRET]


def _w(path: Path, text: str, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(str(path), mode)
    return path


def _build_fixture():
    tmp = Path(tempfile.mkdtemp(prefix="selfcheck-fixture-")).resolve()
    h = tmp
    proj = h / "Projects"
    A = proj / "ProjA"
    B = proj / "ProjB"
    GL = proj / "GlobalProj"
    CLEAN = proj / "Clean"
    SPACE = proj / "含 空白 專案"
    JIA = proj / "甲乙丙"
    DING = proj / "丁戊己"
    repoA = h / "code" / "repoA"
    c = h / ".claude"

    slugA = slug_of(A)
    slugB = slug_of(B)
    slugGL = slug_of(GL)
    slugClean = slug_of(CLEAN)
    slugSpace = slug_of(SPACE)
    slugJia = slug_of(JIA)

    _w(A / "CLAUDE.md",
       "# 專案 A 規則\n\n## 通知\n"
       "- 🚫 一律不要用 webhook 發通知，改用檔案記錄\n"
       "- 🔴 必須先跑測試再宣告完成\n"
       "- 一律先跑 `~/.claude/bin/ghost.sh` 再開工\n"
       "- 一律讀 ~/.claude/CLAUDE.md「通知」節\n"            # 20：階梯裁切要救回來
       "- 一律別碰 ~/.claude/NOPE.md「x」\n"               # 20：裁到最短仍不存在＝真死
       "- 真身在 ~/.claude/CLAUDE.md;真身 那段\n"            # 20：半形 ; 接縫也要救回來
       "- 設定檔在 ~/Library/Application Support/appx/cfg.txt 裡\n"  # 20：空白切斷的路徑接回後面的詞救回
       "- studio 上的 ~/nowhere/bar.sh 一律先看\n"           # 22：跨機標記
       "- studios ~/nowhere/baz.sh 一律先看\n"              # 22：studios 不算跨機
       "@./docs/rules.md\n"                                # 31：專案內 @匯入（rules→deeper→rules 循環）
       "@../ProjB/shared.md\n"                             # 31：允許根外，只記 outside、不 stat 不 open
       "@~/.claude/lessons/INDEX.md\n"                     # 31：允許根內（~/.claude）
       "@./docs/link.md\n"                                 # 32：symlink 指出樹
       "@./docs/a.md\n")                                   # 32：symlink 迴圈
    (A / "sub" / "dir").mkdir(parents=True, exist_ok=True)
    _w(A / ".claude" / "settings.json",
       json.dumps({"permissions": {"allow": ["Bash(*)", "Read(~/x/**)"]},
                   "hooks": {"SessionStart": [{"hooks": [
                       {"type": "command",
                        "command": ('P="${CLAUDE_PROJECT_DIR:-$(pwd)}"; cat "$P/INBOX.md"; '
                                    'bash "$P/.claude/hooks/inj.sh"; cat $DYN_FILE; bash ~/evil.sh')}]}]}},
                  ensure_ascii=False))
    # 30：hook 注入的三種結局——可量／dynamic／不追讀
    _w(A / ".claude" / "hooks" / "inj.sh", '#!/bin/sh\ncat "$P/NOTES.md"\n')
    _w(A / "INBOX.md", "# INBOX\n收件匣：這行每個 session 都被 cat 進脈絡\n")
    _w(A / "NOTES.md", "# NOTES\n經 inj.sh 間接注入的第二層內容\n")
    _w(h / "evil.sh", "#!/bin/sh\necho 不該被追讀\n")
    # 31／32：@匯入的目標
    _w(A / "docs" / "rules.md", "# rules\n@./deeper.md\n- 一律照 rules 走\n")
    _w(A / "docs" / "deeper.md", "# deeper\n@./rules.md\n- 一律照 deeper 走\n")
    _w(B / "shared.md", "# shared\nCANARY_SHARED_6c4e\n")
    _w(B / "secret.md", "# secret\nCANARY_SYMLINK_1a2b\n")
    for _src, _dst in (("../../ProjB/secret.md", A / "docs" / "link.md"),
                       ("b.md", A / "docs" / "a.md"), ("a.md", A / "docs" / "b.md")):
        try:
            os.symlink(_src, str(_dst))
        except OSError:
            pass
    _w(A / ".claude" / "settings.local.json", "{ broken json CANARY_EXC_BODY")
    _w(A / ".claude" / "systems-check.json", json.dumps({
        "code_roots": ["~/code/repoA"],
        "sibling_roots": ["~/sisters"],
        "machine_names": ["studio", "laptop"],
        "log_globs": ["~/logs/*.log"],
        "exclude": [], "security_allow": []}, ensure_ascii=False))
    for sk in ("skillA", "skillB", "skillC"):
        _w(A / ".claude" / "skills" / sk / "SKILL.md",
           "---\nname: %s\ndescription: 測試用 skill %s\n---\n\n# %s\n- 一律照步驟做\n" % (sk, sk, sk))
    try:
        os.symlink(str(c / "projects" / slugB), str(A / ".claude" / "link_out"))
    except OSError:
        pass

    # 21：姊妹根＝manifest 宣告的 ~/sisters/*
    projs = h / "sisters" / "ProjS"
    _w(projs / "tools" / "機械掃.py", "# 姊妹專案的工具\n")
    _w(projs / "main.py", "# 姊妹專案的進入點\n")

    # 27：註解與 docstring 不算 shell_hazard，語法壞掉的 .py 仍要抓
    _w(A / ".claude" / "hooks" / "hz.py",
       '"""模組 docstring：sudo rm -rf 只是文件範例\n第二行也在 docstring 裡\n"""\n'
       "# eval(x) 這是註解\n"
       "import sys\n\n\n"
       "def go(x):\n"
       '    """函式 docstring：sudo rm 也在這裡"""\n'
       "    return eval(x)\n")
    _w(A / ".claude" / "hooks" / "hz.sh", '#!/bin/sh\n# rm -rf "$X" 這是註解\nrm -rf "$X"\n')
    _w(A / ".claude" / "hooks" / "hzbad.py", "def broken(:\n    eval(x)\n")

    _w(B / "CLAUDE.md", "# 專案 B\nCANARY_B_7f3a\n- 一律不要外流\n")
    _w(B / "CANARY_FN_5b2e.md", "b 專案私有內容\n")
    _w(B / "CANARY_EXC.json", "{ broken")

    _w(GL / "CLAUDE.md", "# GlobalProj\n- 🔴 一律先盤點再動手\n")
    _w(CLEAN / "CLAUDE.md", "# 乾淨專案\n- 一律先讀規格再動手\n")
    _w(SPACE / "CLAUDE.md", "# 含空白\n- 一律先確認再動手\n")
    _w(JIA / "CLAUDE.md", "# 甲\n- 一律先確認\n")
    _w(DING / "CLAUDE.md", "# 丁\n- 一律先確認\n")

    _w(c / "CLAUDE.md", "# 全局規則\n\nCANARY_G_9c1d 這行只是標記\n\n- 🔒 密碼與金鑰一律不進版本控制\n\n@lessons/INDEX.md\n@~/outside_import.md\n")
    _w(h / "outside_import.md", "# 在 ~/.claude 外\nCANARY_GIMP_2d4f\n")  # 38：全局 @匯入指到 ~/.claude 外＝不進對照層、不讀
    _w(c / "lessons" / "INDEX.md", "# 索引\n- 效能 — 一律先量測再最佳化\n")
    for sk in ("skillA", "skillB", "skillC"):
        _w(c / "skills" / sk / "SKILL.md",
           "---\nname: %s\ndescription: 全局測試 skill %s\n---\n\n# %s\n- 一律照步驟做\n" % (sk, sk, sk))
    _w(c / ".example-audit.state.json",
       json.dumps({"last_run": now().isoformat(timespec="seconds"), "note": "CANARY_AUD_4e7b"}, ensure_ascii=False))

    mem = c / "projects" / slugA / "memory"
    _w(mem / "MEMORY.md", "# Memory Index\n- [m1](m1.md) — 一律用 webhook\n")
    _w(mem / "m1.md", "# m1\n- 一律用 webhook 發通知，最快\n")
    _w(mem / "m2.md", "# m2\n- 🔴 必須先跑測試再宣告完成\n")
    _w(mem / "m3.md", "# m3\n- 一律留著備查\n")
    _w(mem / "m4.md", "# m4\n- 一律留著備查\n")
    # 21：裸檔名只在姊妹專案找得到 → 降級；ghost2.py 哪裡都沒有 → human；main.py 是常見名 → human
    _w(mem / "m5.md",
       "# m5\n- 一律先跑 `機械掃.py` 再交稿\n- 一律別用 `ghost2.py`\n- 一律用 `main.py` 當進入點\n")
    # 24：已知名稱不該被當密鑰遮掉
    _w(mem / "export-format-v101-keep-original-layout.md", "# 匯出格式\n- 一律照原格式\n")
    _w(mem / (_FAKE_GHP_NAME + ".md"), "# 形似 token 的檔名\n- 一律留著\n")  # 24：不得進白名單
    _w(h / "Library" / "Application Support" / "appx" / "cfg.txt", "k=v\n")  # 20
    _w(mem / "m6.md", "# m6\n- 一律先跑 `leak_only.py` 再開工\n")  # 33：只在 symlink 姊妹根有＝不得 exists_elsewhere
    (h / "outside_sister").mkdir(parents=True, exist_ok=True)
    _w(h / "outside_sister" / "leak_only.py", "x = 1\n")
    try:
        os.symlink(str(h / "outside_sister"), str(h / "sisters" / "ext"))
    except OSError:
        pass
    tj = c / "projects" / slugA / "t.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-09-05T10:00:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "skillA"}}]}},
        {"type": "assistant", "timestamp": "2026-09-05T10:05:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Read",
                                  "input": {"file_path": str(mem / "m4.md")}}]}},
        {"type": "user", "timestamp": "2026-09-05T10:06:00Z",
         "message": {"content": "<command-name>/skillB</command-name>"}},
    ]
    _w(tj, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    _w(c / "projects" / slugA / "bad.jsonl", "{ this is not json CANARY_EXC_LINE\n")
    _w(c / "projects" / slugB / "memory" / "x.md", "# x\nCANARY_B_MEM\n- 一律不要外流\n")
    for sl in (slugGL, slugClean, slugSpace, slugJia):
        _w(c / "projects" / sl / "t.jsonl",
           json.dumps({"type": "assistant", "message": {"content": []}}, ensure_ascii=False) + "\n")

    _w(repoA / "run.sh", "#!/bin/sh\necho hi\n", 0o755)
    _w(repoA / "settings_sample.py", 'API_KEY = "%s"\n# CANARY_GIT_3a91\n' % _FAKE_SECRET)
    _w(repoA / "token.json", "{}\n", 0o644)
    try:
        subprocess.run(["git", "init", "-q", str(repoA)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(["git", "-C", str(repoA), "add", "-A"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass

    _w(h / "logs" / "a.log",
       "2026-09-01 03:00 Traceback (most recent call last): RuntimeError boom in /opt/app/run.py\n"
       "2026-09-01 12:00 webhook sent ok CANARY_LOG_8d4c\n"
       "2026-09-02 03:00 Traceback (most recent call last): RuntimeError boom in /opt/app/run.py\n"
       "2026-09-03 03:00 Traceback (most recent call last): RuntimeError boom in /opt/app/run.py\n")
    _w(h / "logs" / "b.log",
       "2026-09-04 10:00 ERROR upload failed\n"
       "2026-09-04 10:02 notify: webhook sent\n")
    # 25：carry-forward 三態
    _w(h / "logs" / "c.log",
       "2026-09-04 10:00 INFO ok\n"
       "Traceback (most recent call last): boom-c\n"
       "RuntimeError: boom\n"
       "2026-09-06 09:00 INFO ok\n")
    _w(h / "logs" / "d.log",
       "Traceback (most recent call last): boom-d\n"
       "2026-09-06 09:00 INFO ok\n")
    _w(h / "logs" / "e.log",
       "2026-08-01 00:00 x\n"
       "Traceback (most recent call last): boom-e\n")
    # 26：failed=0 摘要
    _w(h / "logs" / "f.log",
       '{"csv": 5, "failed": 0, "status_cleared": 2}\n'
       '{"csv": 5, "failed": 2}\n'
       '{"status": "failed", "count": 0}\n'
       "upload failed=0 errors=3\n"
       "retry failed=0\n"
       '{"ok": true, "failed": 3}\n'                     # 成功旗標不是免死金牌
       "error=0; upload failed\n"                         # 刪掉成功片段後殘餘仍是錯誤
       "no error initially; final exception\n"
       '{"ok":true,"result":{"text":"job exit 1 handled"}}\n'  # 通知 API 型成功回應（有 exit 1 字樣）不算 error
       '{"ok":true,"result":{"text":"upload failed yesterday"}}\n')  # 內文提到 failed 也不算（狀態鍵才算）
    # 24 負對照：不在已知名稱裡的長字串仍要遮
    _w(h / "logs" / "g.log", "ERROR token zq-9f8e7d6c5b4a3f2e1d0c-key77 failed\n")
    # 39：整個 log 沒有任何通知路徑，同一錯誤跨三天＝repeated_unnotified
    _w(h / "logs" / "h.log",
       "2026-09-01 03:00 Traceback (most recent call last): RuntimeError boom-h\n"
       "2026-09-02 03:00 Traceback (most recent call last): RuntimeError boom-h\n"
       "2026-09-03 03:00 Traceback (most recent call last): RuntimeError boom-h\n")

    la = h / "LaunchAgents"
    pl = {"Label": "com.test.a",
          "ProgramArguments": ["/bin/sh", str(repoA / "run.sh")],
          "StandardOutPath": str(h / "logs" / "a.log"),
          "StandardErrorPath": str(h / "logs" / "a.log"),
          "StartCalendarInterval": {"Hour": 3, "Minute": 0},
          "EnvironmentVariables": {"CANARY": "CANARY_ENV_2b8f"}}
    la.mkdir(parents=True, exist_ok=True)
    with open(str(la / "com.test.a.plist"), "wb") as f:  # noqa: SIM115
        plistlib.dump(pl, f)

    os.environ["SELFCHECK_HOME"] = str(h)
    os.environ["SELFCHECK_NOW"] = "2026-09-06T01:00"
    os.environ["SELFCHECK_NO_LAUNCHCTL"] = "1"
    os.environ["SELFCHECK_LAUNCHAGENTS"] = str(la)
    _w(GL / ".claude" / "systems-check.json", json.dumps({"global_layer": True, "audits": [
        {"name": "demo-audit-fresh", "path": "~/.claude/.example-audit.state.json", "cycle": "日", "stale_days": 2},
        {"name": "demo-audit-stale", "path": "~/.claude/.example-audit-old.json", "cycle": "日", "stale_days": 2}]}, ensure_ascii=False))
    _old = c / ".example-audit-old.json"
    _w(_old, json.dumps({"last_run": "2000-01-01T00:00:00"}))
    os.utime(_old, (now().timestamp() - 10 * 86400, now().timestamp() - 10 * 86400))
    return {"home": h, "A": A, "B": B, "GL": GL, "CLEAN": CLEAN, "SPACE": SPACE,
            "JIA": JIA, "DING": DING, "repoA": repoA, "c": c,
            "slugA": slugA, "slugB": slugB, "slugGL": slugGL}


class _Args(object):
    def __init__(self, **kw):
        self.cwd = kw.get("cwd")
        self.out = kw.get("out")
        self.days = kw.get("days", 7)
        self.usage_days = kw.get("usage_days", 30)
        self.dry_run = kw.get("dry_run", True)
        self.report_only = kw.get("report_only", False)
        self.auto = kw.get("auto", False)
        self.list_scope = kw.get("list_scope", False)
        self.verbose = kw.get("verbose", False)


def _run(cwd, **kw):
    import contextlib
    so, se = [], []
    outdir = kw.pop("out", None)
    if outdir is None:
        outdir = Path(tempfile.mkdtemp(prefix="selfcheck-out-"))
    args = _Args(cwd=str(cwd), out=str(outdir), **kw)
    sout, serr = __import__("io").StringIO(), __import__("io").StringIO()
    with contextlib.redirect_stdout(sout), contextlib.redirect_stderr(serr):
        rc = run_scan(args)
    so = sout.getvalue()
    se = serr.getvalue()
    return {"rc": rc, "out": Path(outdir), "stdout": so, "stderr": se}


def _load(res, name):
    p = res["out"] / name
    if not p.exists():
        return None
    txt = p.read_text(encoding="utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(x) for x in txt.splitlines() if x.strip()]
    if name.endswith(".json"):
        return json.loads(txt)
    return txt


def _all_output_text(res):
    parts = [res["stdout"], res["stderr"]]
    for p in sorted(res["out"].rglob("*")):
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(parts)


def selftest() -> int:
    fx = _build_fixture()
    h, A, B, GL = fx["home"], fx["A"], fx["B"], fx["GL"]
    results = []

    def check(name, fn):
        try:
            ok = bool(fn())
            err = ""
        except Exception as exc:
            ok = False
            err = "%s: %s" % (type(exc).__name__, exc)
        results.append((name, ok, err))
        sys.stdout.write("%s  %s%s\n" % ("PASS" if ok else "FAIL", name, ("｜" + err) if err else ""))

    rA = _run(A)
    rGL = _run(GL)
    rClean = _run(fx["CLEAN"])
    rSpace = _run(fx["SPACE"])
    rJia = _run(fx["JIA"])

    def c1():
        s1 = resolve_scope(A)
        s2 = resolve_scope(A / "sub" / "dir")
        return s1.project_root == s2.project_root == A.resolve() and s1.mode == "project"
    check("01 專案根解析（ProjA 與 sub/dir 同根、mode=project）", c1)

    def c2():
        a = _run(h)["rc"]
        b = _run(h / "nowhere")["rc"]
        return a == 2 and b == 2
    check("02 home 與無標記目錄執行 → exit 2", c2)

    def c3():
        txt = _all_output_text(rA)
        no_canary = ("CANARY_B_7f3a" not in txt) and ("CANARY_B_MEM" not in txt) and ("CANARY_FN_5b2e" not in txt)
        acc = (rA["out"] / "access.log").read_text(encoding="utf-8")
        no_b = (fx["slugB"] not in acc) and (str(B) not in acc)
        refused = _load(rA, "scope.json")["refused"]
        has_link = any("link_out" in r["path"] for r in refused)
        return no_canary and no_b and has_link
    check("03 project 模式不外洩 ProjB／slugB，symlink 進 refused", c3)

    def c4():
        rules = _load(rA, "rules.jsonl")
        ref = [r for r in rules if r["role"] == "reference" and r["file"].startswith("global:")]
        acc = (rA["out"] / "access.log").read_text(encoding="utf-8")
        return len(ref) >= 1 and str((fx["c"] / "CLAUDE.md").resolve()) in acc
    check("04 project 模式讀得到全局對照檔且標 role=reference", c4)

    def c5():
        sc = _load(rGL, "scope.json")
        under_claude = any(r.startswith("~/.claude") for r in sc["allowed_roots"])
        refused_b = any(fx["slugB"] in r["path"] for r in sc["refused"])
        return sc["mode"] == "global" and under_claude and refused_b
    check("05 global 模式含全局層、別的專案目錄仍被 refuse", c5)

    def c6():
        rules = _load(rA, "rules.jsonl")
        a = [r for r in rules if "一律不要用 webhook" in r["text"]]
        b = [r for r in rules if "一律用 webhook" in r["text"] and "不要" not in r["text"]]
        if not a or not b:
            return False
        return bool(set(a[0]["topics"]) & set(b[0]["topics"]))
    check("06 衝突素材兩條都在且同桶", c6)

    def c7():
        rules = _load(rA, "rules.jsonl")
        same = [r for r in rules if "必須先跑測試再宣告完成" in r["text"]]
        files = set(r["file"] for r in same)
        return len(same) >= 2 and len(files) >= 2
    check("07 重複素材兩處各有條目", c7)

    def c8():
        u = _load(rA, "usage.json")
        cands = _load(rA, "candidates.jsonl")
        titles = " ".join(c["title"] for c in cands)
        ok_sk = (u["skills"].get("skillA", {}).get("count") == 1
                 and u["skills"].get("skillB", {}).get("count") == 1
                 and u["skills"].get("skillC", {}).get("count", 0) == 0)
        return (ok_sk and "skill:skillC" in titles
                and "memory:m3" in titles and "memory:m4" not in titles)
    check("08 usage 計數與未使用候選（skillC／m3 命中、m4 不中）", c8)

    def c9():
        dead = _load(rA, "dead_refs.jsonl")
        return any("ghost.sh" in d["ref"] for d in dead)
    check("09 dead_refs 抓到 ghost.sh", c9)

    def c10():
        rn = _load(rA, "runners.json")
        r = [x for x in rn["runners"] if x["label"] == "com.test.a"]
        return bool(r) and any("a.log" in p for p in r[0]["log_paths"])
    check("10 runner 歸屬與 log 路徑", c10)

    def c11():
        g = _load(rA, "logs_summary.json")["groups"]
        a = [x for x in g if "traceback" in x["sig"]]
        b = [x for x in g if "upload" in x["sig"]]
        return (bool(a) and a[0]["classification"] == "repeated_unnotified"
                and bool(b) and b[0]["classification"] == "single_or_notified" and b[0]["notified_any"])
    check("11 log 分類（重複無通知 vs 單次已通知）", c11)

    def c12():
        sec = _load(rA, "security.jsonl")
        txt = _all_output_text(rA)
        has_secret = any(s["kind"] == "secret" and s["pattern"] == "generic_assign" for s in sec)
        no_raw = _FAKE_SECRET not in txt
        has_perm = any(s["kind"] == "perm" and "token.json" in s["file"] for s in sec)
        has_wide = any(s["kind"] == "wide_permission" and "Bash(*)" in (s.get("entry") or "") for s in sec)
        return has_secret and no_raw and has_perm and has_wide
    check("12 security：secret／perm／wide_permission 命中且原值不外洩", c12)

    def c13():
        mf = A / ".claude" / "systems-check.json"
        orig = mf.read_text(encoding="utf-8")
        try:
            mf.write_text(json.dumps({"code_roots": ["../.."]}), encoding="utf-8")
            a = _run(A)["rc"]
            mf.write_text(json.dumps({"code_roots": ["~/.claude"]}), encoding="utf-8")
            b = _run(A)["rc"]
            mf.write_text(json.dumps({"log_globs": ["~/.claude/projects/*/memory/*.md"]}), encoding="utf-8")
            r = _run(A)
            refused = _load(r, "scope.json")["refused"]
            hits = [x for x in refused if x["reason"] == "log_glob_in_claude_projects"]
            return a == 2 and b == 2 and len(hits) >= 5
        finally:
            mf.write_text(orig, encoding="utf-8")
    check("13 manifest 越界（.. / ~/.claude → exit 2；log_glob 越界 → refused）", c13)

    def c14():
        d = A / ".claude" / "systems-check"
        before = d.exists()
        args = _Args(cwd=str(A), out=None, dry_run=True)
        import contextlib
        with contextlib.redirect_stdout(__import__("io").StringIO()), contextlib.redirect_stderr(__import__("io").StringIO()):
            run_scan(args)
        after = d.exists()
        bad = _Args(cwd=str(A), out=str(A / ".claude" / "x"), dry_run=True)
        with contextlib.redirect_stdout(__import__("io").StringIO()), contextlib.redirect_stderr(__import__("io").StringIO()):
            rc = run_scan(bad)
        return (before is False) and (after is False) and rc == 2
    check("14 dry-run 不落專案目錄；--out 落專案根 → exit 2", c14)

    def c15():
        cands = _load(rClean, "candidates.jsonl")
        meta = _load(rClean, "scan_meta.json")
        return cands == [] and rClean["rc"] == 0 and meta["status"] == "complete"
    check("15 乾淨專案：候選為空、exit 0、不得 partial", c15)

    def c16():
        sp = fx["repoA"] / "settings_sample.py"
        orig_sp = sp.read_text(encoding="utf-8")
        al = fx["home"] / "logs" / "a.log"
        orig_al = al.read_text(encoding="utf-8")
        try:
            sp.write_text("# CANARY_GIT_3a91\n", encoding="utf-8")
            al.write_text("2026-09-01 03:00 Traceback (most recent call last): RuntimeError boom in /opt/app/run.py\n"
                          "2026-09-01 12:00 webhook sent ok CANARY_LOG_8d4c\n", encoding="utf-8")
            r = _run(A)
            sec = _load(r, "security.jsonl")
            n_secret = len([s for s in sec if s["kind"] == "secret"])
            g = _load(r, "logs_summary.json")["groups"]
            a = [x for x in g if "traceback" in x["sig"] and "boom-h" not in x["sig"]]  # 排除案 39 的 h.log 群組
            return n_secret == 0 and bool(a) and a[0]["classification"] == "single_or_notified"
        finally:
            sp.write_text(orig_sp, encoding="utf-8")
            al.write_text(orig_al, encoding="utf-8")
    check("16 分辨力：拿掉 secret／縮成一天，結論跟著變", c16)

    def c17():
        sc = _load(rJia, "scope.json")
        return "丁戊己" in sc["slug_collisions"]
    check("17 slug 碰撞偵測", c17)

    def c18():
        return rSpace["rc"] == 0
    check("18 中文＋空白路徑專案可解析、exit 0", c18)

    def c19():
        ta = _all_output_text(rA)
        tt = _all_output_text(rGL)
        bad = []
        for cn in CANARIES_CONTENT:
            if cn in ta:
                bad.append("A:" + cn)
            if cn in tt:
                bad.append("GL:" + cn)
        if fx["slugB"] in ta or str(B) in ta:
            bad.append("A:slugB")
        if bad:
            raise AssertionError("外洩：" + "、".join(bad))
        return True
    check("19 全類 canary 與 secret 原值在產出／stdout／stderr 皆為 0", c19)

    # ---------------- 後期新增案 ----------------
    def c20():
        dead = _load(rA, "dead_refs.jsonl")
        refs = [d["ref"] for d in dead]
        alive = not any(r.startswith("~/.claude/CLAUDE.md") for r in refs)
        alive2 = not any(r.startswith("~/Library/Application") for r in refs)
        return alive and alive2 and ("~/.claude/NOPE.md" in refs)
    check("20 CJK 階梯裁切（CLAUDE.md「通知」不判死、NOPE.md 判死且 ref 乾淨）", c20)

    def c21():
        dead = _load(rA, "dead_refs.jsonl")
        cands = _load(rA, "candidates.jsonl")

        def cand(ref):
            return [c for c in cands if c["dimension"] == "scope" and ref in c["title"]]
        ji = [d for d in dead if d["ref"] == "機械掃.py"]
        ok_rec = bool(ji) and str(ji[0].get("exists_elsewhere") or "").startswith("sister:")
        cj, cg, cm = cand("機械掃.py"), cand("ghost2.py"), cand("main.py")
        return (ok_rec and bool(cj) and cj[0].get("triage") == "auto"
                and bool(cg) and "triage" not in cg[0]
                and bool(cm) and "triage" not in cm[0])
    check("21 姊妹根 exists_elsewhere 降級（常見名 main.py 不降級）", c21)

    def c22():
        dead = _load(rA, "dead_refs.jsonl")
        cands = _load(rA, "candidates.jsonl")
        bar = [d for d in dead if d["ref"].endswith("bar.sh")]
        baz = [d for d in dead if d["ref"].endswith("baz.sh")]
        cb = [c for c in cands if "bar.sh" in c["title"]]
        return (bool(bar) and bar[0].get("cross_machine") is True
                and bool(baz) and "cross_machine" not in baz[0]
                and bool(cb) and cb[0].get("triage") == "auto")
    check("22 跨機標記（studio 命中、studios 不命中）", c22)

    def c33():
        dead = _load(rA, "dead_refs.jsonl")
        cands = _load(rA, "candidates.jsonl")
        rec = [d for d in dead if d["ref"] == "leak_only.py"]
        cc = [c for c in cands if c["dimension"] == "scope" and "leak_only.py" in c["title"]]
        notes = " ".join(_load(rA, "scan_meta.json").get("notes", []))
        return (bool(rec) and "exists_elsewhere" not in rec[0]
                and bool(cc) and "triage" not in cc[0]
                and "symlink" in notes)
    check("33 姊妹根 symlink 不跟：只在 symlink 根有的檔不得 exists_elsewhere，且 scan_meta.notes 留痕", c33)

    def c35():
        d = h / ".claude" / "skills" / "desc_probe"
        d.mkdir(parents=True, exist_ok=True)
        desc = "短短一句觸發說明，含中文與 ASCII 123"
        body = "\n".join(["# desc_probe", ""] + ["這是正文第 %d 行，很長很長很長很長很長很長很長很長很長很長。" % k for k in range(60)])
        (d / "SKILL.md").write_text("---\nname: desc_probe\ndescription: %s\n---\n%s\n" % (desc, body), encoding="utf-8")
        d2 = h / ".claude" / "skills" / "desc_block"
        d2.mkdir(parents=True, exist_ok=True)
        (d2 / "SKILL.md").write_text("---\nname: desc_block\ndescription: >\n  第一段\n  第二段\n---\n" + body + "\n", encoding="utf-8")
        r = _run(GL)
        inv = _load(r, "inventory.json")
        a = [i for i in inv if i["layer"] == "skill" and i["path_rel"].endswith("desc_probe/SKILL.md")]
        b = [i for i in inv if i["layer"] == "skill" and i["path_rel"].endswith("desc_block/SKILL.md")]
        want_a = _utf16_units(desc)
        want_b = _utf16_units("第一段 第二段")
        return (bool(a) and a[0]["always_on"] and a[0]["utf16_units"] == want_a and a[0]["bytes"] > 2000
                and bool(b) and b[0]["utf16_units"] == want_b)
    check("35 skill 常駐量只計 description（長正文不算；YAML > 區塊會合併）", c35)

    def c23():
        old_now = os.environ.get("SELFCHECK_NOW")
        try:
            os.environ["SELFCHECK_NOW"] = (datetime.now() + timedelta(days=10)).isoformat(timespec="seconds")
            r_old = _run(A, usage_days=5)
            os.environ["SELFCHECK_NOW"] = datetime.now().isoformat(timespec="seconds")
            r_new = _run(A, usage_days=30)
        finally:
            if old_now is not None:
                os.environ["SELFCHECK_NOW"] = old_now
        a = [c for c in _load(r_old, "candidates.jsonl") if "memory:m3" in c["title"]]
        b = [c for c in _load(r_new, "candidates.jsonl") if "memory:m3" in c["title"]]
        u = _load(r_new, "usage.json")
        return (bool(a) and "triage" not in a[0]
                and bool(b) and b[0].get("triage") == "auto"
                and "窗內新檔" in (b[0].get("auto_reason") or "")
                and "m3" in ((u.get("too_young") or {}).get("memory") or []))
    check("23 too_young 分辨力（檔齡 > 窗＝human；窗內新檔＝auto）", c23)

    def c24():
        titles = " ".join(c["title"] for c in _load(rA, "candidates.jsonl"))
        groups = _load(rA, "logs_summary.json")["groups"]
        leak = any("zq-9f8e7d6c5b4a3f2e1d0c-key77" in (g.get("sample") or "") for g in groups)
        ghp_leak = _FAKE_GHP_NAME in titles
        return ("memory:export-format-v101-keep-original-layout" in titles) and not leak and not ghp_leak
    check("24 已知名稱不遮、未知長字串仍遮、形似 token 的檔名不進白名單", c24)

    def c25():
        g = _load(rA, "logs_summary.json")["groups"]

        def one(key):
            hit = [x for x in g if key in x["sig"]]
            return hit[0] if hit else None
        cc, dd, ee = one("boom-c"), one("boom-d"), one("boom-e")
        return (cc is not None and cc["ts_source"] == "carried" and cc["first_seen"] == "2026-09-04"
                and dd is not None and dd["ts_source"] == "unknown"
                and dd["unknown_day_count"] == 1 and dd["first_seen"] is None
                and ee is None)
    check("25 log 戳 carry-forward（carried／unknown／舊戳被 cutoff 濾掉）", c25)

    def c26():
        sigs = [x["sig"] for x in _load(rA, "logs_summary.json")["groups"]]
        joined = "｜".join(sigs)
        return ("status_cleared" not in joined
                and any('"failed": #}' in s for s in sigs)
                and any('"status": "failed"' in s for s in sigs)
                and any(s.startswith("upload failed") for s in sigs)
                and not any(s.startswith("retry") for s in sigs)
                and any('"ok": true' in s for s in sigs)
                and any(s.startswith("error=#; upload failed") for s in sigs)
                and any("final exception" in s for s in sigs)
                and not any('"ok":true,"result"' in s for s in sigs))
    check("26 failed=0 摘要行不算 error（JSON 與純文字兩路）", c26)

    def c27():
        sec = _load(rA, "security.jsonl")

        def haz(suffix):
            return [s for s in sec if s["kind"] == "shell_hazard" and s["file"].endswith(suffix)]
        py, sh, bad = haz("/hz.py"), haz("/hz.sh"), haz("/hzbad.py")
        return (len(py) == 1 and py[0]["pattern"] == "eval"
                and len(sh) == 1 and sh[0]["pattern"] == "rm_rf_var"
                and any(s["pattern"] == "eval" for s in bad))
    check("27 註解／docstring 不掃 shell_hazard，語法壞掉仍寧多報", c27)

    def c28():
        mf = A / ".claude" / "systems-check.json"
        orig = mf.read_text(encoding="utf-8")
        fxpy = A / "fx.py"
        try:
            _w(fxpy, 'PASSWORD = "%s"  # FIXTURE\nPASSWORD = "%s"\n' % (_FX_SECRET_A, _FX_SECRET_B))
            data = json.loads(orig)
            data["security_allow"] = [{"file": "fx.py", "pattern": "generic_assign",
                                       "snippet_contains": "FIXTURE"}]
            mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            r = _run(A)
            hits = [s for s in _load(r, "security.jsonl")
                    if s["kind"] == "secret" and s["file"].endswith("/fx.py")]
            txt = _all_output_text(r)
            return (len(hits) == 1 and hits[0]["line"] == 2
                    and _FX_SECRET_A not in txt and _FX_SECRET_B not in txt)
        finally:
            mf.write_text(orig, encoding="utf-8")
            try:
                fxpy.unlink()
            except OSError:
                pass
    check("28 security_allow 的 snippet_contains 只放行帶錨那一行", c28)

    def c29():
        meta = _load(rA, "scan_meta.json")["counts"]
        cands = _load(rA, "candidates.jsonl")
        return (meta["candidates_auto"] + meta["candidates_human"] == meta["candidates"] == len(cands)
                and all(("triage" not in c) or c["triage"] == "auto" for c in cands))
    check("29 counts 的 auto＋human＝candidates，triage 只有缺或 auto", c29)

    def c30():
        inv = _load(rA, "inventory.json")
        inj = [i for i in inv if i.get("layer") == "inject"]
        ib = [i for i in inj if i["path_rel"].endswith("/INBOX.md")]
        nt = [i for i in inj if i["path_rel"].endswith("/NOTES.md")]
        dyn = [i for i in inj if i.get("dynamic")]
        evil = [i for i in inj if i["path_rel"].endswith("evil.sh")]
        al = _load(rA, "access.log") or ""
        return (len(ib) == 1 and len(nt) == 1 and ib[0]["always_on"] and nt[0]["always_on"]
                and ib[0]["utf16_units"] == _utf16_units((A / "INBOX.md").read_text(encoding="utf-8"))
                and nt[0]["utf16_units"] == _utf16_units((A / "NOTES.md").read_text(encoding="utf-8"))
                and "經 inj.sh" in nt[0]["always_on_reason"]
                and len(dyn) == 1 and dyn[0]["path_rel"] == "$DYN_FILE" and dyn[0]["utf16_units"] == 0
                and len(evil) == 1 and evil[0].get("unfollowed") is True
                and "evil.sh" not in al)
    check("30 inject：cat 目標可量、經腳本追讀一層、dynamic 不量、允許根外不追讀", c30)

    def c31():
        inv = _load(rA, "inventory.json")
        imp = [i for i in inv if i.get("layer") == "import"]
        rl = [i for i in imp if i["path_rel"].endswith("docs/rules.md")]
        dp = [i for i in imp if i["path_rel"].endswith("docs/deeper.md")]
        sh = [i for i in imp if i["path_rel"].endswith("ProjB/shared.md")]
        idx_imp = [i for i in imp if i["path_rel"].endswith("lessons/INDEX.md")]
        idx_inv = [i for i in inv if i["path_rel"] == "~/.claude/lessons/INDEX.md"]
        # 「同路徑不重複計」與「被 @匯入的檔也另有 import 列」兩種設計都說得通：
        # 本實作併進既有列（只補 always_on_reason），故兩種結局都算通過。
        idx_ok = (bool(idx_imp) and idx_imp[0]["utf16_units"] > 0) or (
            bool(idx_inv) and "亦由 @匯入" in (idx_inv[0].get("always_on_reason") or ""))
        al = _load(rA, "access.log") or ""
        refused = _load(rA, "scope.json")["refused"]
        return (len(rl) == 1
                and rl[0]["utf16_units"] == _utf16_units((A / "docs" / "rules.md").read_text(encoding="utf-8"))
                and len(dp) == 1 and dp[0]["utf16_units"] > 0
                and idx_ok
                and len(sh) == 1 and sh[0].get("outside") is True and sh[0]["utf16_units"] == 0
                and "shared.md" not in al
                and not any("shared.md" in x["path"] for x in refused)
                and "CANARY_SHARED_6c4e" not in _all_output_text(rA))
    check("31 import：遞迴＋循環不炸，允許根外只記 outside（不 stat 不 open）", c31)

    def c31b():
        sc = _load(rA, "scope.json")
        refused = " ".join(r.get("path", "") for r in sc.get("refused", []))
        inv = _load(rA, "inventory.json")
        imported = [i["path_rel"] for i in inv if i.get("layer") == "import" and (i.get("utf16_units") or 0) > 0]
        return bool(imported) and not any(Path(p).name in refused for p in imported)
    check("31b import 讀過的檔不得同時出現在 refused", c31b)

    def c32():
        inv = _load(rA, "inventory.json")
        imp = [i for i in inv if i.get("layer") == "import"]
        lk = [i for i in imp if i["path_rel"].endswith("docs/link.md")]
        aa = [i for i in imp if i["path_rel"].endswith("docs/a.md")]
        return (len(lk) == 1 and bool(lk[0].get("outside") or lk[0].get("unreadable"))
                and lk[0]["utf16_units"] == 0
                and len(aa) == 1 and bool(aa[0].get("unreadable") or aa[0].get("outside"))
                and "CANARY_SYMLINK_1a2b" not in _all_output_text(rA)
                and rA["rc"] in (0, 1))
    check("32 邊界：symlink 出樹＝outside、symlink 迴圈＝unreadable，canary 不外洩", c32)

    def c34():
        au = _load(rGL, "existing_audits.json") or {}
        items = {i["name"]: i for i in au.get("items", [])}
        meta = _load(rGL, "scan_meta.json") or {}
        fresh, stale = items.get("demo-audit-fresh"), items.get("demo-audit-stale")
        return (au.get("checked") is True and bool(fresh) and bool(stale)
                and fresh["exists"] is True and fresh["stale"] is False
                and stale["exists"] is True and stale["stale"] is True
                and any("demo-audit-stale" in x.get("reason", "") for x in meta.get("incomplete", []))
                and "CANARY_AUD_4e7b" not in json.dumps(au, ensure_ascii=False))
    check("34 manifest audits：新鮮的不算過期、過期的標 stale 並記 incomplete、只列鍵名不列值", c34)

    def c37():
        import contextlib
        import shutil
        base = A / ".claude" / "systems-check"
        outside = Path(tempfile.mkdtemp(prefix="selfcheck-outside-")) / "run"
        quiet = (contextlib.redirect_stdout(__import__("io").StringIO()), contextlib.redirect_stderr(__import__("io").StringIO()))
        with quiet[0], quiet[1]:
            rc_out = run_scan(_Args(cwd=str(A), out=str(outside), dry_run=False))
        with quiet[0], quiet[1]:
            rc_base = run_scan(_Args(cwd=str(A), out=str(base), dry_run=False))
        inside = base / "custom-run"
        with quiet[0], quiet[1]:
            rc_in = run_scan(_Args(cwd=str(A), out=str(inside), dry_run=False))
        link = base / "latest"
        link_ok = link.is_symlink() and Path(os.readlink(str(link))).resolve() == inside.resolve()
        ok = (rc_out == 2 and not outside.exists()
              and rc_base == 2
              and rc_in in (0, 1) and (inside / "scan_meta.json").exists() and link_ok)
        shutil.rmtree(str(base), ignore_errors=True)  # 還原 fixture，別影響其他案
        return ok
    check("37 正式跑的 --out 只准落在 .claude/systems-check/ 底下（外面、base 本身都 exit 2；裡面可跑且 latest 指過去）", c37)

    def c38():
        sc = _load(rA, "scope.json")
        refs = sc.get("reference_files") or []
        inv = _load(rA, "inventory.json")
        idx = [i for i in inv if i["path_rel"] == "~/.claude/lessons/INDEX.md"]
        return ("~/.claude/lessons/INDEX.md" in refs
                and not any(r.endswith("outside_import.md") for r in refs)
                and bool(idx) and idx[0].get("always_on") is True
                and "@匯入" in (idx[0].get("always_on_reason") or "")
                and "CANARY_GIMP_2d4f" not in _all_output_text(rA))
    check("38 對照層由全局 CLAUDE.md 的 @匯入決定：~/.claude 內的檔進 reference 且常駐；指到外面的不收、canary 不外洩", c38)

    def c39():
        g = _load(rA, "logs_summary.json")["groups"]
        a = [x for x in g if "boom-h" in x["sig"]]
        return (bool(a) and a[0]["classification"] == "repeated_unnotified"
                and a[0]["file_has_notify_path"] is False and a[0]["days_distinct"] >= 3)
    check("39 log 完全沒有通知路徑、同一錯誤跨 3 天＝repeated_unnotified（不因沒通知路徑而降級）", c39)

    passed = len([1 for _, ok, _ in results if ok])
    total = len(results)
    sys.stdout.write("SELFTEST %d/%d\n" % (passed, total))
    return 0 if passed == total else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
