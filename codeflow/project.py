"""ディレクトリの走査・並列解析・キャッシュ・呼び出し解決(コールグラフ構築)。"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import threading
import time
from concurrent.futures import ProcessPoolExecutor

from .languages import ACCESSORS, EXT_MAP, SPECS, analyze_generic, generic_flow
from .pyanalyzer import analyze_python, python_flow

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target", "out", "bin", "obj",
    ".idea", ".vscode", ".next", ".nuxt", ".svelte-kit", "vendor", "Pods", ".gradle", "site-packages",
    ".ipynb_checkpoints", ".cache", "coverage", "DerivedData", ".dart_tool", ".expo",
}
MAX_FILES = 30000
MAX_BYTES = 2_000_000
ENCODINGS = ("utf-8", "utf-8-sig", "cp932", "euc-jp", "latin-1")

# 受け手不明の obj.xxx() で、標準ライブラリ等と名前が衝突しやすいもの
COMMON_METHODS = frozenset("""
append extend insert pop remove clear copy get set items keys values update setdefault format join split
strip lstrip rstrip replace find index count startswith endswith lower upper encode decode read write
close open readline readlines flush seek send recv push shift unshift map filter reduce forEach then catch
finally log info warn warning error debug print toString equals hashCode add size length len contains
has delete emit on off once call apply bind next resolve reject fetch json text parse stringify render
setState sort reverse slice splice concat includes indexOf trim test exec match search wait notify
start run stop sleep sum min max abs round floor ceil isEmpty put getOrDefault subscribe dispatch
""".split())

ENTRY_STEMS = {"index", "main", "app", "server", "cli", "start", "run", "bootstrap", "program"}


# ---------------------------------------------------------------------------
# ファイル走査
# ---------------------------------------------------------------------------

def _load_gitignore(root: str) -> list[str]:
    pats = []
    try:
        with open(os.path.join(root, ".gitignore"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith(("#", "!")):
                    pats.append(line.rstrip("/").lstrip("/"))
    except OSError:
        pass
    return pats


def walk(root: str) -> list[tuple[str, str, int, int]]:
    """(rel, abs, mtime_ns, size) の一覧。"""
    ign = _load_gitignore(root)
    out = []
    stack = [""]
    while stack and len(out) < MAX_FILES:
        rel_dir = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel_dir) if rel_dir else root)
        except OSError:
            continue
        with it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if ign and any(fnmatch.fnmatch(e.name, p) or fnmatch.fnmatch(rel, p) for p in ign):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in IGNORE_DIRS or (e.name.startswith(".") and e.name not in (".github",)):
                        continue
                    stack.append(rel)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat()
                    out.append((rel, e.path, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    out.sort()
    return out


def read_text(path: str) -> tuple[str | None, str]:
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:8000]:
        return None, "binary"
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def lang_of(rel: str) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return None
    return EXT_MAP.get(name[dot:]) or EXT_MAP.get(name[dot:].lower())


def analyze_file(args) -> dict:
    """1ファイル解析 (プロセスプールから呼ばれる)。"""
    rel, path, lang = args
    try:
        src, enc = read_text(path)
        if src is None:
            return {"lang": lang, "symbols": [], "imports": [], "lines": 0, "error": "バイナリ"}
        if lang == "python":
            res = analyze_python(rel, src)
        else:
            res = analyze_generic(rel, src, SPECS[lang])
        res["lines"] = src.count("\n") + 1
        res["encoding"] = enc
        for s in res["symbols"]:
            s.pop("b", None)
        return res
    except Exception as e:  # noqa: BLE001  1ファイルの失敗で全体を止めない
        return {"lang": lang, "symbols": [], "imports": [], "lines": 0, "error": f"解析失敗: {e}"}


# ---------------------------------------------------------------------------
# import → プロジェクト内ファイルの解決
# ---------------------------------------------------------------------------

class FileIndex:
    def __init__(self, rels: list[str]):
        self.files = set(rels)
        self.stem: dict[str, set] = {}
        self.name: dict[str, set] = {}
        self.dir: dict[str, set] = {}
        for rel in rels:
            noext = rel.rsplit(".", 1)[0] if "." in rel.rsplit("/", 1)[-1] else rel
            self._add_suffixes(self.stem, noext, rel)
            self._add_suffixes(self.name, rel, rel)
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if d:
                self._add_suffixes(self.dir, d, rel)
            else:
                self.dir.setdefault("", set()).add(rel)

    @staticmethod
    def _add_suffixes(table, path, rel):
        parts = path.split("/")
        for k in range(1, len(parts) + 1):
            table.setdefault("/".join(parts[-k:]), set()).add(rel)

    def by_stem(self, key: str) -> set:
        return self.stem.get(key.strip("/"), set())

    def resolve(self, src_rel: str, imp: dict) -> list[str]:
        mod = imp["mod"]
        mode = imp.get("mode", "path")
        here = posixpath.dirname(src_rel)
        found: set = set()
        if mode == "py":
            key = mod.replace(".", "/")
            found = self.by_stem(key) | self.by_stem(key + "/__init__")
            if imp["kind"] == "from" and imp.get("orig"):
                sub = self.by_stem(f"{key}/{imp['orig']}") | self.by_stem(f"{key}/{imp['orig']}/__init__")
                if sub:
                    imp["kind"] = "module"   # from pkg import submodule
                    found = sub
            # 「from . import x」でルートより上のパッケージ名が付く場合などは末尾一致で十分
        elif mode == "path":
            m = mod.split("?")[0]
            cands = []
            if m.startswith((".", "/")) or imp.get("rel"):
                cands.append(posixpath.normpath(posixpath.join(here, m)).lstrip("/"))
            else:
                cands.append(posixpath.normpath(posixpath.join(here, m)))
                cands.append(re.sub(r"^[@~]/", "", m))
            for c in cands:
                if c in self.files:
                    found = {c}
                    break
                hit = self.by_stem(c) or self.by_stem(c + "/index") or self.name.get(c.strip("/"), set())
                if hit:
                    found = hit
                    break
        elif mode == "dotted":
            segs = [s for s in re.split(r"[.\\]", mod) if s and s != "*"]
            wildcard = mod.endswith("*")
            for k in range(len(segs), 0, -1):
                key = "/".join(segs[-k:]) if not wildcard else "/".join(segs[-k:])
                hit = self.dir.get(key, set()) if wildcard else (self.by_stem(key) or self.by_stem(key + "/init"))
                if not hit and not wildcard:
                    hit = self.dir.get(key, set()) if k == len(segs) else set()
                if hit:
                    found = hit
                    break
                if k <= max(1, len(segs) - 2):
                    break
        elif mode == "gopkg":
            segs = mod.split("/")
            for k in range(len(segs), 0, -1):
                hit = {f for f in self.dir.get("/".join(segs[-k:]), set()) if f.endswith(".go")}
                if hit:
                    found = hit
                    break
        elif mode == "rust":
            segs = [s for s in mod.split("::") if s not in ("crate", "self", "super", "")]
            for k in range(len(segs), 0, -1):
                key = "/".join(segs[:k])
                hit = self.by_stem(key) or self.by_stem(key + "/mod")
                if hit:
                    found = hit
                    break
        elif mode == "rustmod":
            stem = src_rel.rsplit(".", 1)[0]
            base = here if posixpath.basename(stem) in ("mod", "main", "lib") else stem
            for c in (f"{base}/{mod}.rs", f"{base}/{mod}/mod.rs"):
                c = c.lstrip("/")
                if c in self.files:
                    found = {c}
                    break
        found.discard(src_rel)
        return sorted(found)[:40]


# ---------------------------------------------------------------------------
# コールグラフ
# ---------------------------------------------------------------------------

def _project_entry_files(root: str, rels: set) -> dict[str, tuple[int, str]]:
    """package.json / pyproject.toml から起動ファイル・関数を拾う。"""
    out: dict[str, tuple[int, str]] = {}
    for rel in rels:
        base = rel.rsplit("/", 1)[-1]
        d = posixpath.dirname(rel)
        if base == "package.json" and rel.count("/") <= 2:
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    pj = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            targets = []
            if isinstance(pj.get("main"), str):
                targets.append((pj["main"], "package.json main"))
            b = pj.get("bin")
            if isinstance(b, str):
                targets.append((b, "package.json bin"))
            elif isinstance(b, dict):
                targets += [(v, f"package.json bin:{k}") for k, v in b.items() if isinstance(v, str)]
            for k, v in (pj.get("scripts") or {}).items():
                if isinstance(v, str):
                    for m in re.finditer(r"\b(?:node|ts-node|tsx|bun|deno run)\s+([\w./-]+\.[cm]?[jt]sx?)", v):
                        targets.append((m.group(1), f"npm run {k}"))
            for t, why in targets:
                p = posixpath.normpath(posixpath.join(d, t)).lstrip("./")
                out[p] = (95, why)
        elif base == "pyproject.toml":
            try:
                import tomllib
                with open(os.path.join(root, rel), "rb") as f:
                    pp = tomllib.load(f)
            except Exception:  # noqa: BLE001
                continue
            scripts = (pp.get("project") or {}).get("scripts") or {}
            scripts.update(((pp.get("tool") or {}).get("poetry") or {}).get("scripts") or {})
            for k, v in scripts.items():
                if isinstance(v, str) and ":" in v:
                    out["py:" + v.strip()] = (95, f"pyproject scripts: {k}")
    return out


def build_graph(root: str, results: dict[str, dict], all_rels: set) -> dict:
    idx = FileIndex([r for r, res in results.items()])
    syms: dict[str, dict] = {}
    by_file: dict[str, list] = {}
    top_by_name: dict[str, list] = {}      # 関数・クラス (メソッド以外)
    methods_by_name: dict[str, list] = {}
    members: dict[tuple, dict] = {}        # (file, class) -> {name: sym}
    classes_by_name: dict[str, list] = {}
    file_lang = {}

    for rel, res in results.items():
        lst = []
        file_lang[rel] = res.get("lang")
        for s in res["symbols"]:
            sid = f"{rel}::{s['qual']}@{s['start']}"
            s["id"] = sid
            s["file"] = rel
            syms[sid] = s
            lst.append(s)
            k = s["kind"]
            if k == "toplevel":
                continue
            if k == "method":
                methods_by_name.setdefault(s["name"], []).append(s)
                members.setdefault((rel, s["cls"]), {}).setdefault(s["name"], s)
            else:
                top_by_name.setdefault(s["name"], []).append(s)
                if k == "class":
                    classes_by_name.setdefault(s["name"], []).append(s)
        by_file[rel] = lst

    # import 解決 & 別名表
    file_imports: dict[str, list] = {}
    aliases: dict[str, dict] = {}
    for rel, res in results.items():
        targets = set()
        amap = {}
        for imp in res.get("imports", []):
            if imp.get("mode") == "gopkg" and not imp.get("local"):
                imp["local"] = imp["mod"].rsplit("/", 1)[-1]
            files = idx.resolve(rel, imp)
            targets.update(files)
            if imp.get("local"):
                amap[imp["local"]] = (imp["kind"], files, imp.get("orig"), imp["mod"])
        file_imports[rel] = sorted(targets)
        aliases[rel] = amap

    def file_symbol(files, name, allow_method=False):
        for f in files:
            for s in by_file.get(f, ()):
                if s["name"] == name and (s["kind"] in ("function", "class") or (allow_method and s["kind"] == "method")):
                    return s
        return None

    def ctor_of(cls_sym):
        lang = file_lang.get(cls_sym["file"])
        spec = SPECS.get(lang)
        cname = spec.ctor if spec else None
        if lang == "python":
            cname = "__init__"
        if cname == "@class":
            cname = cls_sym["name"]
        if cname:
            m = members.get((cls_sym["file"], cls_sym["name"]), {}).get(cname)
            if m:
                return m
        return cls_sym

    def class_of(sym):
        if sym.get("cls"):
            return sym["cls"]
        q = sym["qual"]
        return q.rsplit(".", 2)[-2] if q.count(".") >= 1 and sym["kind"] == "method" else None

    def pick(cands, rel):
        """候補が複数のとき: 同じファイル > 同じディレクトリ > import 先。3件までは曖昧エッジとして全部返す。"""
        if not cands:
            return [], 0
        if len(cands) == 1:
            return cands, 0
        same = [c for c in cands if c["file"] == rel]
        if same:
            return same[:1], 0
        imported = set(file_imports.get(rel, ()))
        imp = [c for c in cands if c["file"] in imported]
        if len(imp) == 1:
            return imp, 0
        d = posixpath.dirname(rel)
        near = [c for c in cands if posixpath.dirname(c["file"]) == d]
        if len(near) == 1:
            return near, 0
        if len(cands) <= 3:
            return cands, 1
        return [], 0

    def resolve(sym, call):
        name, recv, _line, flag = call
        rel = sym["file"]
        amap = aliases.get(rel, {})
        cls = class_of(sym)
        if recv in ACCESSORS or (recv == "" and flag != 1 and cls and (rel, cls) in members
                                 and name in members[(rel, cls)] and file_lang.get(rel) != "python"):
            if cls:
                m = members.get((rel, cls), {}).get(name)
                if m:
                    return [m], 0
            if recv in ACCESSORS:
                if flag == 2:
                    return [], 0
                same = [m for m in methods_by_name.get(name, ()) if m["file"] == rel]
                if same:
                    return same[:1], 0
                return pick(methods_by_name.get(name, []), rel) if name not in COMMON_METHODS else ([], 0)
        if recv and recv not in ("()",):
            head = recv.split(".", 1)[0]
            if recv in amap or head in amap:
                kind, files, orig, mod = amap.get(recv) or amap[head]
                if kind == "from" and files:
                    # from pkg import Klass  ->  Klass.method()
                    c = file_symbol(files, orig)
                    if c and c["kind"] == "class":
                        m = members.get((c["file"], c["name"]), {}).get(name)
                        if m:
                            return [m], 0
                if files:
                    s = file_symbol(files, name, allow_method=True)
                    if s:
                        return [ctor_of(s) if s["kind"] == "class" else s], 0
                if kind == "module":
                    return [], 0
                # import したクラスに無いメソッド (継承など) は、下の推測に任せる
            # Klass.static() / Klass::method() / Ruby の Klass.new
            if recv in classes_by_name:
                for c in classes_by_name[recv]:
                    m = members.get((c["file"], c["name"]), {}).get(name)
                    if m:
                        return [m], 0
                if name == "new":
                    return [ctor_of(classes_by_name[recv][0])], 0
            if flag == 2 or name in COMMON_METHODS:
                return [], 0
            return pick(methods_by_name.get(name, []), rel)
        # 受け手なし
        if name in amap:
            kind, files, orig, mod = amap[name]
            if kind in ("from", "default") and files:
                s = file_symbol(files, orig or name)
                if s:
                    return [ctor_of(s) if s["kind"] == "class" else s], 0
        cands = top_by_name.get(name, [])
        same = [c for c in cands if c["file"] == rel]
        if same:
            s = same[0]
            return [ctor_of(s) if s["kind"] == "class" else s], 0
        if flag == 2:
            return [], 0
        imported = set(file_imports.get(rel, ()))
        imp = [c for c in cands if c["file"] in imported]
        chosen, amb = pick(imp, rel) if imp else pick(cands, rel)
        return [ctor_of(s) if s["kind"] == "class" else s for s in chosen], amb

    edges = []
    for s in syms.values():
        seen = {}
        ext = []
        for call in s.get("calls", ()):
            targets, amb = resolve(s, call)
            if not targets:
                if call[3] != 2 and call[0] not in ext and len(ext) < 40:
                    ext.append(call[0])
                continue
            for t in targets:
                if t["id"] == s["id"] and call[3] == 2:
                    continue
                key = t["id"]
                if key in seen:
                    edges[seen[key]][4] += 1
                    continue
                flag = 2 if call[3] == 2 else amb
                seen[key] = len(edges)
                edges.append([s["id"], t["id"], call[2], flag, 1])
        s["ext"] = ext

    # エントリーポイント推定
    entries: dict[str, tuple[int, str]] = {}

    def add_entry(sid, score, why):
        if sid in syms and (sid not in entries or entries[sid][0] < score):
            entries[sid] = (score, why)

    for rel, res in results.items():
        for qual, start, score, why in res.get("hints", ()):
            add_entry(f"{rel}::{qual}@{start}", score, why)
        spec = SPECS.get(res.get("lang"))
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for s in by_file[rel]:
            if spec and s["kind"] == "function" and s["name"] in spec.main_names:
                add_entry(s["id"], 90 if s["name"] in ("main", "Main") else 80, f"{s['name']}() 関数")
            if spec and s["kind"] == "method" and s["name"] in spec.main_names and spec.name in ("java", "csharp", "kotlin", "scala"):
                add_entry(s["id"], 92, f"{s['qual']}() (static main)")
            if s["kind"] == "toplevel" and res.get("lang") != "python":
                if res.get("lang") in ("shell", "powershell"):
                    add_entry(s["id"], 50, "スクリプト本体")
                elif stem in ENTRY_STEMS:
                    add_entry(s["id"], 60, f"{stem} のトップレベル処理")
                else:
                    add_entry(s["id"], 12, "トップレベルで処理を実行")
    for key, (score, why) in _project_entry_files(root, all_rels).items():
        if key.startswith("py:"):
            mod, func = key[3:].split(":", 1)
            files = idx.by_stem(mod.replace(".", "/")) | idx.by_stem(mod.replace(".", "/") + "/__init__")
            s = file_symbol(sorted(files), func.split(".")[-1])
            if s:
                add_entry(s["id"], score, why)
        else:
            for s in by_file.get(key, ()):
                if s["kind"] == "toplevel":
                    add_entry(s["id"], score, why)
    incoming = {e[1] for e in edges}
    outgoing = {e[0] for e in edges}
    roots = [sid for sid in outgoing if sid not in incoming and sid not in entries]
    for sid in sorted(roots)[:400]:
        add_entry(sid, 5, "呼び出し元なし (起点候補)")

    # 送信用に軽量化
    out_syms = {}
    for sid, s in syms.items():
        out_syms[sid] = {"n": s["name"], "q": s["qual"], "k": s["kind"], "f": s["file"], "s": s["start"],
                         "e": s["end"], "x": s.get("ext", []), "nc": len(s.get("calls", ()))}
    files = []
    for rel in sorted(all_rels):
        res = results.get(rel)
        if res:
            files.append({"p": rel, "lang": res.get("lang"), "n": res.get("lines", 0), "err": res.get("error"),
                          "imp": file_imports.get(rel, []), "sym": [s["id"] for s in by_file.get(rel, ())]})
        else:
            files.append({"p": rel, "lang": None})
    ent = sorted(({"id": k, "score": v[0], "why": v[1]} for k, v in entries.items()),
                 key=lambda e: (-e["score"], e["id"]))
    return {"files": files, "symbols": out_syms, "edges": edges, "entries": ent}


# ---------------------------------------------------------------------------
# プロジェクト (キャッシュ付き)
# ---------------------------------------------------------------------------

class Project:
    def __init__(self):
        self.root: str | None = None
        self.cache: dict[str, tuple] = {}
        self.lock = threading.Lock()
        self.pool: ProcessPoolExecutor | None = None

    def set_root(self, root: str):
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            raise ValueError(f"フォルダが見つかりません: {root}")
        if root != self.root:
            self.root = root
            self.cache.clear()

    def safe_path(self, rel: str) -> str:
        if not self.root:
            raise ValueError("フォルダが読み込まれていません")
        p = os.path.realpath(os.path.join(self.root, rel))
        root = os.path.realpath(self.root)
        if p != root and not p.startswith(root + os.sep):
            raise PermissionError("読み込んだフォルダの外にはアクセスできません")
        return p

    def scan(self) -> dict:
        with self.lock:
            t0 = time.perf_counter()
            root = self.root
            listing = walk(root)
            t_walk = time.perf_counter()
            jobs = []
            all_rels = set()
            fresh = {}
            for rel, path, mtime, size in listing:
                all_rels.add(rel)
                lang = lang_of(rel)
                if not lang or size > MAX_BYTES:
                    continue
                c = self.cache.get(rel)
                if c and c[0] == mtime and c[1] == size:
                    fresh[rel] = c[2]
                else:
                    jobs.append((rel, path, lang, mtime, size))
            if len(jobs) >= 150 or (len(jobs) >= 4 and sum(j[4] for j in jobs) > 3_000_000):
                if self.pool is None:
                    self.pool = ProcessPoolExecutor(max_workers=min(8, (os.cpu_count() or 2)))
                args = [(r, p, l) for r, p, l, _, _ in jobs]
                # 大きいファイルから投入し、ワーカー間の偏りを減らす
                order = sorted(range(len(jobs)), key=lambda k: -jobs[k][4])
                chunk = max(1, len(jobs) // ((os.cpu_count() or 2) * 8))
                res_sorted = list(self.pool.map(analyze_file, [args[k] for k in order], chunksize=chunk))
                outs = [None] * len(jobs)
                for k, r in zip(order, res_sorted):
                    outs[k] = r
            else:
                outs = [analyze_file((r, p, l)) for r, p, l, _, _ in jobs]
            for (rel, _p, _l, mtime, size), res in zip(jobs, outs):
                self.cache[rel] = (mtime, size, res)
                fresh[rel] = res
            for rel in list(self.cache):
                if rel not in all_rels:
                    del self.cache[rel]
            t_parse = time.perf_counter()
            # build_graph は結果へ id 等を書き込むので浅いコピーを渡す
            results = {rel: {**res, "symbols": [dict(s) for s in res["symbols"]],
                             "imports": [dict(i) for i in res.get("imports", ())]} for rel, res in fresh.items()}
            g = build_graph(root, results, all_rels)
            t_end = time.perf_counter()
            by_lang: dict[str, int] = {}
            for res in fresh.values():
                by_lang[res.get("lang")] = by_lang.get(res.get("lang"), 0) + 1
            g["root"] = root
            g["stats"] = {
                "files": len(all_rels), "analyzed": len(fresh), "reparsed": len(jobs),
                "symbols": len(g["symbols"]), "edges": len(g["edges"]), "by_lang": by_lang,
                "ms": {"walk": round((t_walk - t0) * 1000), "parse": round((t_parse - t_walk) * 1000),
                       "graph": round((t_end - t_parse) * 1000), "total": round((t_end - t0) * 1000)},
            }
            return g

    def flow(self, rel: str, start: int) -> dict:
        lang = lang_of(rel)
        if not lang:
            raise ValueError("解析対象外のファイルです")
        src, _ = read_text(self.safe_path(rel))
        if src is None:
            raise ValueError("バイナリファイルです")
        items = python_flow(src, start) if lang == "python" else generic_flow(src, SPECS[lang], start)
        return {"lang": lang, "items": items}
