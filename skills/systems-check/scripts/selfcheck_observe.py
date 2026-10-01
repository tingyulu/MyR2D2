#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""observer.py — 外部觀測者（驗證用）：在載入受測腳本前 monkeypatch 檔案系統入口，
把每一次 open／stat／lstat／scandir／listdir／os.open／readlink／access 的路徑寫到 OBS_LOG（append）。
用法：OBS_LOG=/path/obs.log /usr/bin/python3 selfcheck_observe.py selfcheck_scan.py [args...]（例：--selftest 後 grep 真 HOME 路徑應為 0）
它不是 access.log（那是受測程式自己寫的）；判越界以本檔的紀錄為準。
"""
import builtins, functools, io, os, runpy, sys

LOG = os.environ.get("OBS_LOG", "/tmp/observer.log")
_f = open(LOG, "a", encoding="utf-8")  # 先開好再 patch，否則自己也被記


def _rec(kind, p):
    try:
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        elif isinstance(p, int):
            return  # fd 型呼叫不記
        _f.write("%s\t%s\n" % (kind, os.fspath(p)))
        _f.flush()
    except Exception:
        pass


class _W(object):
    """不可綁定的 callable：pathlib 會把 os.stat 存成類別屬性，普通函式會被當方法綁定多吃一個 self。"""

    def __init__(self, orig, kind):
        self.orig = orig
        self.kind = kind
        self.__name__ = getattr(orig, "__name__", kind)
        self.__doc__ = getattr(orig, "__doc__", None)

    def __call__(self, *a, **k):
        p = a[0] if a else k.get("path", k.get("file"))
        if p is not None:
            _rec(self.kind, p)
        return self.orig(*a, **k)


def _wrap(mod, name, kind):
    setattr(mod, name, _W(getattr(mod, name), kind))


for _n, _k in (("stat", "stat"), ("lstat", "lstat"), ("scandir", "scandir"), ("listdir", "listdir"),
               ("open", "os.open"), ("readlink", "readlink"), ("access", "access")):
    _wrap(os, _n, _k)
_wrap(builtins, "open", "open")
_wrap(io, "open", "open")

target = sys.argv[1]
sys.argv = sys.argv[1:]
_rec("observer-start", target)
rc = 0
try:
    runpy.run_path(target, run_name="__main__")
except SystemExit as e:
    rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
_rec("observer-end", "rc=%s" % rc)
sys.exit(rc)
