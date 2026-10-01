#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck_state.py — systems-check 的指紋去重與卡對應。

state.json schema：
  {"<fp>": {"card_id": "1234", "first_seen": "2026-09-06", "last_seen": "2026-09-06",
            "seen_count": 3, "title": "...",
            "status": "accepted", "reason": "...", "by": "<誰接受的>", "at": "2026-09-06"}}
  （status／reason／by／at 只有「已接受例外」才有。）

子命令：
  plan   --state S --findings F            印 {"new":[], "repeat":[], "resolved":[], "accepted":[],
                                              "accepted_resolved":[], "counts":{}}（new 已照 severity→confidence→fp 排好）
  commit --state S --findings F --cards fp=id[,fp=id...]
  accept --state S <fp> --reason "..." [--by <誰接受的>] [--title "..."]

寫入一律 tmp＋rename（原子），並用 fcntl.flock 鎖 state.json：多張卡逐張 commit 時合併不覆蓋。
本工具只讀寫 state.json 與 findings 檔，不呼叫任何外部服務，也不連網。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"


def _today() -> str:
    v = os.environ.get("SELFCHECK_NOW")
    if v:
        try:
            return datetime.fromisoformat(v).date().isoformat()
        except ValueError:
            pass
    return datetime.now().date().isoformat()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:  # noqa: SIM115
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_findings(path: Path):
    with open(path, "r", encoding="utf-8") as f:  # noqa: SIM115
        text = f.read()
    text_s = text.strip()
    if not text_s:
        return []
    if text_s[0] == "[":
        data = json.loads(text_s)
    elif text_s[0] == "{" and "\n" not in text_s.strip("\n"):
        data = [json.loads(text_s)]
    else:
        try:
            data = json.loads(text_s)
        except Exception:
            data = [json.loads(x) for x in text_s.splitlines() if x.strip()]
    if isinstance(data, dict):
        data = data.get("findings") or []
    out = []
    for row in data:
        if isinstance(row, dict) and row.get("fp"):
            out.append(row)
    return out


def _atomic_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _Lock(object):
    """對 state.json 旁的 .lock 上排他鎖；逐張 commit 時合併不覆蓋。"""

    def __init__(self, path: Path):
        self.lockpath = Path(str(path) + ".lock")
        self.fh = None

    def __enter__(self):
        self.lockpath.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(str(self.lockpath), "a+")  # noqa: SIM115
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_CONF_RANK = {"confirmed": 0, "suspected": 1}


def cmd_plan(args) -> int:
    state = load_state(Path(args.state))
    findings = load_findings(Path(args.findings))
    seen = set()
    new_items, repeat, accepted = [], [], []
    for f in findings:
        fp = f["fp"]
        seen.add(fp)
        ent = state.get(fp)
        if ent and ent.get("status") == "accepted":
            accepted.append({"fp": fp, "reason": ent.get("reason", ""), "by": ent.get("by", ""),
                             "at": ent.get("at", ""), "title": f.get("title") or ent.get("title", "")})
        elif ent:
            repeat.append({"fp": fp, "card_id": ent.get("card_id"),
                           "seen_count": int(ent.get("seen_count") or 0) + 1,
                           "title": f.get("title") or ent.get("title", "")})
        else:
            new_items.append(f)
    # 開卡順序固定：severity high→medium→low，同級 confirmed 先於 suspected，再依 fp；超過上限時先開的是前面的
    new_items.sort(key=lambda f: (_SEV_RANK.get(str(f.get("severity")), 9),
                                  _CONF_RANK.get(str(f.get("confidence")), 9), str(f["fp"])))
    new = [f["fp"] for f in new_items]
    resolved = [fp for fp, ent in state.items()
                if fp not in seen and ent.get("status") != "accepted"]
    accepted_resolved = [fp for fp, ent in state.items()
                         if fp not in seen and ent.get("status") == "accepted"]  # 已接受的例外消失了也要看得到
    out = {"new": new, "repeat": repeat, "resolved": sorted(resolved), "accepted": accepted,
           "accepted_resolved": sorted(accepted_resolved),
           "counts": {"new": len(new), "repeat": len(repeat), "resolved": len(resolved),
                      "accepted": len(accepted), "accepted_resolved": len(accepted_resolved)}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


def _parse_cards(spec: str):
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit("--cards 格式錯：需要 fp=id，收到 %r" % part)
        fp, cid = part.split("=", 1)
        out[fp.strip()] = cid.strip()
    return out


def cmd_commit(args) -> int:
    spath = Path(args.state)
    findings = load_findings(Path(args.findings))
    by_fp = dict((f["fp"], f) for f in findings)
    cards = _parse_cards(args.cards)
    today = _today()
    with _Lock(spath):
        state = load_state(spath)
        touched = []
        for fp, cid in cards.items():
            f = by_fp.get(fp)
            ent = state.get(fp)
            if ent is None:
                ent = {"card_id": cid, "first_seen": today, "last_seen": today,
                       "seen_count": 1, "title": (f or {}).get("title", "")}
            else:
                ent = dict(ent)
                ent["card_id"] = cid or ent.get("card_id")
                ent["last_seen"] = today
                ent["seen_count"] = int(ent.get("seen_count") or 0) + 1
                if f and f.get("title"):
                    ent["title"] = f["title"]
            state[fp] = ent
            touched.append(fp)
        if args.touch_repeats:
            for f in findings:
                fp = f["fp"]
                if fp in cards or fp not in state:
                    continue
                ent = dict(state[fp])
                if ent.get("status") == "accepted":
                    continue
                ent["last_seen"] = today
                ent["seen_count"] = int(ent.get("seen_count") or 0) + 1
                state[fp] = ent
                touched.append(fp)
        _atomic_write(spath, state)
    sys.stdout.write(json.dumps({"committed": sorted(set(touched)), "state": str(spath)},
                                ensure_ascii=False) + "\n")
    return 0


def cmd_accept(args) -> int:
    spath = Path(args.state)
    today = _today()
    with _Lock(spath):
        state = load_state(spath)
        ent = dict(state.get(args.fp) or {})
        ent.setdefault("first_seen", today)
        ent["last_seen"] = today
        ent.setdefault("seen_count", 1)
        if args.title:
            ent["title"] = args.title
        ent["status"] = "accepted"
        ent["reason"] = args.reason
        ent["by"] = args.by
        ent["at"] = today
        state[args.fp] = ent
        _atomic_write(spath, state)
    sys.stdout.write(json.dumps({"accepted": args.fp, "reason": args.reason, "by": args.by,
                                 "at": today}, ensure_ascii=False) + "\n")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="selfcheck_state.py", description="系統自檢指紋 state 工具")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("plan")
    p.add_argument("--state", required=True)
    p.add_argument("--findings", required=True)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("commit")
    p.add_argument("--state", required=True)
    p.add_argument("--findings", required=True)
    p.add_argument("--cards", required=True, help="fp=id[,fp=id...]")
    p.add_argument("--touch-repeats", action="store_true",
                   help="同時把本次再次命中的舊指紋 last_seen／seen_count 更新")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("accept")
    p.add_argument("fp")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default=os.environ.get("SELFCHECK_HUMAN_OWNER") or "使用者")
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_accept)
    return ap


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
