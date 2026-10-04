"""標準ライブラリだけで動く HTTP サーバ (127.0.0.1 限定)。"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import string
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import backups, fileops
from .languages import monaco_lang
from .project import ENCODINGS, Project, read_text, walk
from .runner import RunManager, find_python

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PROJECT = Project()
RUNS = RunManager()
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _browse(path: str | None) -> dict:
    if not path:
        path = PROJECT.root or os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    dirs = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir() and not e.name.startswith("."):
                    dirs.append(e.name)
    except OSError as e:
        return {"path": path, "parent": os.path.dirname(path), "dirs": [], "error": str(e)}
    drives = []
    if sys.platform == "win32":
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None, "dirs": sorted(dirs, key=str.lower),
            "drives": drives, "home": os.path.expanduser("~")}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodeFlow/1.0"

    def log_message(self, fmt, *args):  # アクセスログは出さない
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _guard(self, post: bool = False) -> bool:
        """別サイトからの操作を拒否する (ファイルの読み書きとプログラム実行を守る)。

        * Host が 127.0.0.1 / localhost 以外 → DNS リバインディング対策で拒否
        * Origin 付きで Host と違う → 他サイトからの fetch / フォーム送信を拒否
        * POST は JSON のみ (フォームからの単純リクエストでは送れない形式)
        """
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        origin = self.headers.get("Origin")
        bad = hostname not in LOCAL_HOSTS or (origin and urlparse(origin).netloc != host)
        if post and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            bad = True
        if bad:
            _json(self, {"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path.startswith("/api/"):
                if not self._guard():
                    return
                return self._api_get(u.path, q)
            return self._static(u.path)
        except PermissionError as e:
            _json(self, {"error": str(e)}, 403)
        except (ValueError, KeyError) as e:
            _json(self, {"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _json(self, {"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if not self._guard(post=True):
                return
            data = self._body()
            if u.path == "/api/scan":
                if data.get("root"):
                    PROJECT.set_root(data["root"])
                if not PROJECT.root:
                    return _json(self, {"error": "フォルダを指定してください"}, 400)
                return _json(self, PROJECT.scan())
            if u.path == "/api/file":
                rel = data["path"]
                path = PROJECT.safe_path(rel)
                enc = data.get("encoding") or "utf-8"
                if enc not in ENCODINGS:
                    enc = "utf-8"
                if data.get("create") and os.path.exists(path):
                    return _json(self, {"error": "同名のファイルが既にあります"}, 409)
                exists = os.path.isfile(path)
                base = data.get("base_mtime")
                if exists and not data.get("create") and not data.get("force"):
                    cur = os.stat(path).st_mtime_ns
                    # 開いた後に外部で変更されていたら上書きせずに知らせる
                    if base is None or int(base) != cur:
                        disk, _enc = read_text(path)
                        return _json(self, {"error": "ファイルが外部で変更されています", "conflict": True,
                                            "disk": disk, "mtime": cur}, 409)
                bid = backups.backup(PROJECT.root, rel, path) if exists else None
                os.makedirs(os.path.dirname(path), exist_ok=True)
                content = data.get("content", "")
                with open(path, "w", encoding=enc, newline="") as f:
                    f.write(content)
                return _json(self, {"ok": True, "mtime": os.stat(path).st_mtime_ns, "backup": bid})
            if u.path == "/api/search":
                if not PROJECT.root:
                    return _json(self, {"error": "フォルダを読み込んでください"}, 400)
                return _json(self, fileops.search(PROJECT.root, data.get("q", ""), bool(data.get("regex")),
                                                  bool(data.get("case")), bool(data.get("word")), data.get("include", "")))
            if u.path == "/api/replace":
                return _json(self, fileops.replace(PROJECT.root, PROJECT.safe_path, data.get("q", ""), data.get("rep", ""),
                                                   list(data.get("files") or []), bool(data.get("regex")),
                                                   bool(data.get("case")), bool(data.get("word"))))
            if u.path == "/api/rename":
                return _json(self, fileops.rename(PROJECT.safe_path, data["path"], data["to"]))
            if u.path == "/api/delete":
                return _json(self, fileops.delete(PROJECT.root, PROJECT.safe_path, data["path"]))
            if u.path == "/api/run/start":
                if not PROJECT.root:
                    return _json(self, {"error": "フォルダを読み込んでください"}, 400)
                return _json(self, RUNS.start(PROJECT.root, data))
            if u.path == "/api/run/stop":
                RUNS.stop()
                return _json(self, {"ok": True})
            if u.path == "/api/run/input":
                RUNS.send_input(str(data.get("text", "")))
                return _json(self, {"ok": True})
            _json(self, {"error": "not found"}, 404)
        except PermissionError as e:
            _json(self, {"error": str(e)}, 403)
        except (ValueError, KeyError) as e:
            _json(self, {"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _json(self, {"error": str(e)}, 500)

    def _api_get(self, path, q):
        if path == "/api/state":
            return _json(self, {"root": PROJECT.root, "sep": os.sep, "platform": sys.platform})
        if path == "/api/browse":
            return _json(self, _browse(q.get("path")))
        if path == "/api/file":
            p = PROJECT.safe_path(q["path"])
            src, enc = read_text(p)
            if src is None:
                return _json(self, {"error": "バイナリファイルは開けません"}, 415)
            return _json(self, {"content": src, "encoding": enc, "lang": monaco_lang(q["path"]),
                                "mtime": os.stat(p).st_mtime_ns})
        if path == "/api/backups":
            PROJECT.safe_path(q["path"])
            return _json(self, {"items": backups.list_backups(PROJECT.root, q["path"])})
        if path == "/api/backup":
            PROJECT.safe_path(q["path"])
            src, enc = read_text(backups.backup_path(PROJECT.root, q["path"], q["id"]))
            return _json(self, {"content": src, "encoding": enc})
        if path == "/api/run/status":
            return _json(self, RUNS.status(int(q.get("since", 0))))
        if path == "/api/run/config":
            if not PROJECT.root:
                return _json(self, {"python": sys.executable})
            py = find_python(PROJECT.root)
            return _json(self, {"python": py, "venv": py != sys.executable, "node": shutil.which("node") or ""})
        if path == "/api/flow":
            return _json(self, PROJECT.flow(q["path"], int(q["line"])))
        if path == "/api/stamp":
            # 外部エディタでの変更検知用 (ファイル一覧と更新時刻のハッシュ)
            if not PROJECT.root:
                return _json(self, {"stamp": None})
            h = 0
            for rel, _p, mtime, size in walk(PROJECT.root):
                h = (h * 1000003 + hash((rel, mtime, size))) & 0xFFFFFFFFFFFF
            return _json(self, {"stamp": h})
        _json(self, {"error": "not found"}, 404)

    def do_HEAD(self):
        self._static(urlparse(self.path).path, head=True)

    def _static(self, path, head=False):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC)) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head:
            self.wfile.write(data)


def serve(root: str | None, port: int, open_browser: bool):
    if root:
        PROJECT.set_root(root)
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("空いているポートが見つかりません")
    url = f"http://127.0.0.1:{port}/"
    print(f"Code Flow Viewer: {url}")
    if PROJECT.root:
        print(f"  対象フォルダ: {PROJECT.root}")
    print("  終了するには Ctrl+C")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        RUNS.stop()
        if PROJECT.pool:
            PROJECT.pool.shutdown(cancel_futures=True)
