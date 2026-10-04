"""保存前の自動バックアップ。

プロジェクトフォルダを汚さないよう ~/.codeflow/backups/<フォルダごとのID>/<相対パス>/ に置き、
1ファイルあたり新しい順に KEEP 個まで残す。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time

BASE = os.path.join(os.path.expanduser("~"), ".codeflow", "backups")
KEEP = 30


def _dir_for(root: str, rel: str) -> str:
    rid = hashlib.sha1(os.path.realpath(root).encode("utf-8")).hexdigest()[:12]
    return os.path.join(BASE, rid, *rel.split("/"))


def backup(root: str, rel: str, path: str) -> str | None:
    """path の現在の内容を退避する。ファイルが無ければ何もしない。"""
    if not os.path.isfile(path):
        return None
    d = _dir_for(root, rel)
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    dest = os.path.join(d, stamp + ".bak")
    shutil.copy2(path, dest)
    olds = sorted(f for f in os.listdir(d) if f.endswith(".bak"))
    for f in olds[:-KEEP]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return stamp


def list_backups(root: str, rel: str) -> list[dict]:
    d = _dir_for(root, rel)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted((f for f in os.listdir(d) if f.endswith(".bak")), reverse=True):
        p = os.path.join(d, f)
        bid = f[:-4]
        t = bid[:15]
        out.append({"id": bid, "time": f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}:{t[13:15]}",
                    "size": os.path.getsize(p)})
    return out


def backup_path(root: str, rel: str, bid: str) -> str:
    if not bid or any(c in bid for c in "/\\.") :
        raise ValueError("不正なバックアップIDです")
    p = os.path.join(_dir_for(root, rel), bid + ".bak")
    if not os.path.isfile(p):
        raise ValueError("バックアップが見つかりません")
    return p
