"""HTTP API のテスト: 保存時の衝突検知・バックアップ・不正リクエスト拒否・実行トレース。"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codeflow import backups, server  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.proj = os.path.join(cls.tmp, "proj")
        shutil.copytree(os.path.join(ROOT, "samples", "py_app"), cls.proj)
        os.makedirs(os.path.join(cls.proj, "data"), exist_ok=True)
        with open(os.path.join(cls.proj, "data", "a.csv"), "w", encoding="utf-8") as f:
            f.write("Alpha\nBeta\n")
        backups.BASE = os.path.join(cls.tmp, "backups")
        server.PROJECT.set_root(cls.proj)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        server.RUNS.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_save_conflict_and_backup(self):
        st, d = self.req("/api/file?path=pipeline/model.py")
        self.assertEqual(st, 200)
        base = d["mtime"]
        # 外部で変更
        time.sleep(0.01)
        p = os.path.join(self.proj, "pipeline", "model.py")
        with open(p, "a", encoding="utf-8") as f:
            f.write("# external\n")
        st, r = self.req("/api/file", {"path": "pipeline/model.py", "content": "mine\n", "base_mtime": base})
        self.assertEqual(st, 409)
        self.assertTrue(r["conflict"])
        self.assertIn("# external", r["disk"])
        with open(p, encoding="utf-8") as f:
            self.assertIn("# external", f.read())      # 上書きされていない
        # 強制保存 → バックアップに外部変更版が残る
        st, r = self.req("/api/file", {"path": "pipeline/model.py", "content": "mine\n", "force": True})
        self.assertEqual(st, 200)
        st, lst = self.req("/api/backups?path=pipeline/model.py")
        self.assertGreaterEqual(len(lst["items"]), 1)
        st, b = self.req(f"/api/backup?path=pipeline/model.py&id={lst['items'][0]['id']}")
        self.assertIn("# external", b["content"])
        # 正しい base_mtime なら普通に保存できる
        st, r2 = self.req("/api/file", {"path": "pipeline/model.py", "content": "mine2\n", "base_mtime": r["mtime"]})
        self.assertEqual(st, 200)

    def test_search_replace_rename_delete(self):
        st, r = self.req("/api/search", {"q": "DEF LOAD_DATASET", "include": "*.py"})
        self.assertEqual(st, 200, r)
        hits = {f["p"]: f["m"] for f in r["files"]}
        self.assertIn("pipeline/loader.py", hits)
        m = hits["pipeline/loader.py"][0]
        self.assertEqual(m["t"][m["s"]:m["s"] + m["n"]], "def load_dataset")
        st, r = self.req("/api/search", {"q": "load_dataset", "include": "pipeline/, !*.csv"})
        self.assertEqual({f["p"] for f in r["files"]}, {"pipeline/loader.py"})
        st, r = self.req("/api/search", {"q": "trainer", "case": True, "word": True})
        self.assertTrue(all(f["p"] == "main.py" for f in r["files"]))
        st, r = self.req("/api/search", {"q": "(", "regex": True})
        self.assertEqual(st, 400)
        # 置換 (正規表現の $1 形式) → バックアップされる
        st, r = self.req("/api/replace", {"q": r"def (_init_rng)", "rep": "def $1_v2", "regex": True,
                                          "files": ["pipeline/preprocess.py"]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["total"], 1)
        with open(os.path.join(self.proj, "pipeline", "preprocess.py"), encoding="utf-8") as f:
            self.assertIn("def _init_rng_v2", f.read())
        st, b = self.req("/api/backups?path=pipeline/preprocess.py")
        self.assertEqual(len(b["items"]), 1)
        # 名前変更 → 削除(ゴミ箱へ)
        with open(os.path.join(self.proj, "tmp_a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        st, r = self.req("/api/rename", {"path": "tmp_a.py", "to": "sub/tmp_b.py"})
        self.assertEqual(st, 200, r)
        self.assertTrue(os.path.isfile(os.path.join(self.proj, "sub", "tmp_b.py")))
        st, r = self.req("/api/rename", {"path": "sub/tmp_b.py", "to": "main.py"})
        self.assertEqual(st, 400)                                   # 既存ファイルは上書きしない
        from codeflow import fileops
        fileops.TRASH = os.path.join(self.tmp, "trash")
        st, r = self.req("/api/delete", {"path": "sub"})
        self.assertEqual(st, 200, r)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "sub")))
        self.assertTrue(os.path.isfile(os.path.join(r["trash"], "tmp_b.py")))
        st, r = self.req("/api/delete", {"path": "."})
        self.assertEqual(st, 400)

    def test_guard(self):
        st, _ = self.req("/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)
        st, _ = self.req("/api/run/stop", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run/stop", data=b"a=1",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 403)
        st, _ = self.req("/api/file?path=../../etc/passwd")
        self.assertEqual(st, 403)

    def test_run_traced(self):
        st, r = self.req("/api/run/start", {"mode": "python", "target": "main.py", "args": "--epochs 2", "count": True})
        self.assertEqual(st, 200, r)
        snap = None
        for _ in range(100):
            time.sleep(0.1)
            st, s = self.req("/api/run/status?since=0")
            if not s["running"] and s.get("snapshot") and s["snapshot"].get("done"):
                snap = s["snapshot"]
                break
        self.assertIsNotNone(snap, s)
        self.assertEqual(s["code"], 0, s["text"])
        names = {(f[0], f[2]): f[3] for f in snap["fn"]}
        self.assertEqual(names.get(("main.py", "main")), 1)
        self.assertEqual(names.get(("pipeline/model.py", "step")), 2)      # --epochs 2
        self.assertGreaterEqual(names.get(("pipeline/loader.py", "load_dataset"), 0), 1)
        self.assertEqual(names.get(("pipeline/loader.py", "read_csv")), 1)
        self.assertGreaterEqual(names.get(("pipeline/preprocess.py", "flip"), 0), 0)

    def test_run_exception_and_stdin(self):
        with open(os.path.join(self.proj, "boom.py"), "w", encoding="utf-8") as f:
            f.write("def ask():\n    return input()\n\ndef boom(x):\n    raise ValueError(x)\n\nboom(ask())\n")
        st, r = self.req("/api/run/start", {"mode": "python", "target": "boom.py"})
        self.assertEqual(st, 200, r)
        time.sleep(0.5)
        self.req("/api/run/input", {"text": "こんにちは"})
        for _ in range(100):
            time.sleep(0.1)
            st, s = self.req("/api/run/status?since=0")
            if not s["running"] and (s.get("snapshot") or {}).get("done"):
                break
        self.assertEqual(s["code"], 1)
        exc = s["snapshot"]["exc"]
        self.assertEqual(exc["type"], "ValueError")
        self.assertEqual(exc["msg"], "こんにちは")
        self.assertEqual(exc["where"], ["boom.py", 5])
        self.assertIn("ValueError: こんにちは", s["text"])
        self.assertNotIn("tracer.py", s["text"])

    def test_run_stop(self):
        with open(os.path.join(self.proj, "loop.py"), "w", encoding="utf-8") as f:
            f.write("import time\n\ndef work():\n    time.sleep(0.05)\n\nwhile True:\n    work()\n")
        while self.req("/api/run/status?since=0")[1].get("running"):
            time.sleep(0.1)
        st, r = self.req("/api/run/start", {"mode": "python", "target": "loop.py"})
        self.assertEqual(st, 200, r)
        time.sleep(1.0)
        st, s = self.req("/api/run/status?since=0")
        self.assertTrue(s["running"])
        stack = s["snapshot"]["stack"]
        self.assertEqual(stack[-1][2], "work")           # いま work() の中
        self.req("/api/run/stop", {})
        for _ in range(50):
            time.sleep(0.1)
            if not self.req("/api/run/status?since=0")[1]["running"]:
                break
        self.assertFalse(self.req("/api/run/status?since=0")[1]["running"])


if __name__ == "__main__":
    unittest.main()
