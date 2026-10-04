"""ファイル横断の検索・置換と、名前変更・削除(ゴミ箱へ移動)。"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import time

from . import backups
from .project import MAX_BYTES, read_text, walk

MAX_MATCHES = 5000
TRASH = os.path.join(os.path.expanduser("~"), ".codeflow", "trash")


def compile_query(q: str, regex: bool, case: bool, word: bool) -> re.Pattern:
    if not q:
        raise ValueError("検索する文字列を入力してください")
    pat = q if regex else re.escape(q)
    if word:
        pat = r"(?<![\w])(?:" + pat + r")(?![\w])"
    try:
        return re.compile(pat, 0 if case else re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"正規表現の誤り: {e}") from e


def _included(rel: str, include: str) -> bool:
    pats = [p.strip() for p in (include or "").split(",") if p.strip()]
    if not pats:
        return True
    name = rel.rsplit("/", 1)[-1]
    for p in pats:
        neg = p.startswith("!")
        p = p.lstrip("!")
        if any(c in p for c in "*?["):
            hit = fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p)
        else:
            hit = p.strip("/") in rel
        if hit:
            return not neg
    return all(p.startswith("!") for p in pats)


def search(root: str, q: str, regex=False, case=False, word=False, include="") -> dict:
    t0 = time.perf_counter()
    pat = compile_query(q, regex, case, word)
    files = []
    total = 0
    truncated = False
    for rel, path, _mtime, size in walk(root):
        if size > MAX_BYTES or not _included(rel, include):
            continue
        try:
            text, _enc = read_text(path)
        except OSError:
            continue
        if text is None:
            continue
        matches = []
        for i, line in enumerate(text.split("\n"), 1):
            for m in pat.finditer(line):
                if m.end() == m.start():
                    continue
                a, b = m.start(), m.end()
                # 長い行は一致箇所の周りだけ切り出す
                lo = max(0, a - 60)
                hi = min(len(line), b + 100)
                pre = line[lo:hi]
                lead = len(pre) - len(pre.lstrip())
                matches.append({"l": i, "c": a, "n": b - a, "t": pre.strip(), "s": a - lo - lead,
                                "cut": lo > 0})
                total += 1
                if total >= MAX_MATCHES:
                    truncated = True
                    break
            if truncated:
                break
        if matches:
            files.append({"p": rel, "m": matches})
        if truncated:
            break
    return {"files": files, "total": total, "truncated": truncated, "ms": round((time.perf_counter() - t0) * 1000)}


def replace(root: str, safe_path, q: str, rep: str, files: list[str], regex=False, case=False, word=False) -> dict:
    pat = compile_query(q, regex, case, word)
    if regex:
        rep_t = re.sub(r"\$(\d+)", r"\\g<\1>", rep)   # $1 形式も受け付ける
        repl = rep_t
    else:
        repl = lambda _m: rep  # noqa: E731  文字列をそのまま (\ を特別扱いしない)
    changed = []
    total = 0
    for rel in files:
        path = safe_path(rel)
        text, enc = read_text(path)
        if text is None:
            continue
        new, n = pat.subn(repl, text)
        if not n or new == text:
            continue
        backups.backup(root, rel, path)
        with open(path, "w", encoding=enc, newline="") as f:
            f.write(new)
        changed.append({"p": rel, "n": n, "mtime": os.stat(path).st_mtime_ns})
        total += n
    return {"changed": changed, "total": total}


def rename(safe_path, src: str, dst: str) -> dict:
    a = safe_path(src)
    b = safe_path(dst)
    if not os.path.exists(a):
        raise ValueError(f"見つかりません: {src}")
    if os.path.exists(b):
        raise ValueError(f"移動先が既にあります: {dst}")
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.move(a, b)
    return {"ok": True}


def delete(root: str, safe_path, rel: str) -> dict:
    """完全には消さず ~/.codeflow/trash/ へ移動する (戻したいときはそこから取り出す)。"""
    p = safe_path(rel)
    if os.path.realpath(p) == os.path.realpath(root):
        raise ValueError("読み込んだフォルダ自体は削除できません")
    if not os.path.exists(p):
        raise ValueError(f"見つかりません: {rel}")
    dest = os.path.join(TRASH, backups.root_id(root), time.strftime("%Y%m%d-%H%M%S"), *rel.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(p, dest)
    return {"ok": True, "trash": dest}
