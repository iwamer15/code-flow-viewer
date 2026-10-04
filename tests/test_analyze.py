"""samples/ を解析して主要な呼び出し関係が取れているかを確認する簡易テスト。
python -m unittest discover tests
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codeflow.project import Project  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")


def edge_set(g):
    out = set()
    for a, b, *_ in g["edges"]:
        sa, sb = g["symbols"][a], g["symbols"][b]
        out.add((f"{sa['f']}:{sa['q']}", f"{sb['f']}:{sb['q']}"))
    return out


class AnalyzeSamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = Project()
        p.set_root(SAMPLES)
        cls.p = p
        cls.g = p.scan()
        cls.e = edge_set(cls.g)

    def has(self, a, b):
        self.assertIn((a, b), self.e, f"missing edge {a} -> {b}")

    def test_python(self):
        self.has("py_app/main.py:__main__", "py_app/main.py:main")
        self.has("py_app/main.py:main", "py_app/pipeline/loader.py:load_dataset")
        self.has("py_app/main.py:main", "py_app/pipeline/preprocess.py:normalize")
        self.has("py_app/main.py:main", "py_app/pipeline/preprocess.py:Augmenter.__init__")
        self.has("py_app/main.py:main", "py_app/pipeline/model.py:Trainer.__init__")
        self.has("py_app/main.py:main", "py_app/main.py:worker")  # Thread(target=worker)
        self.has("py_app/pipeline/preprocess.py:Augmenter.__init__", "py_app/pipeline/preprocess.py:Augmenter._init_rng")
        self.has("py_app/pipeline/loader.py:load_dataset", "py_app/pipeline/loader.py:_list_files")

    def test_js_ts(self):
        self.has("js_app/src/server.js:<top-level>", "js_app/src/server.js:startServer")
        self.has("js_app/src/server.js:<top-level>", "js_app/src/config.ts:loadConfig")
        self.has("js_app/src/server.js:startServer", "js_app/src/router.js:route")
        self.has("js_app/src/router.js:route", "js_app/src/handlers.js:home")
        self.has("js_app/src/router.js:route", "js_app/src/router.js:notFound")
        self.has("js_app/src/config.ts:loadConfig", "js_app/src/config.ts:parsePort")

    def test_go_java_c_rust_shell(self):
        self.has("go_app/main.go:main", "go_app/jobs.go:loadJobs")
        self.has("go_app/main.go:main", "go_app/main.go:Job.Run")
        self.has("go_app/main.go:Job.Run", "go_app/main.go:process")
        self.has("java_app/App.java:App.main", "java_app/App.java:Service.Service")
        self.has("java_app/App.java:Service.handle", "java_app/App.java:Service.format")
        self.has("c_app/main.c:main", "c_app/main.c:compute")
        self.has("c_app/main.c:compute", "c_app/util.c:add")
        self.has("c_app/main.c:main", "c_app/util.c:print_result")
        self.has("rust_app/src/main.rs:main", "rust_app/src/engine.rs:Engine.new")
        self.has("rust_app/src/main.rs:main", "rust_app/src/main.rs:report")
        self.has("rust_app/src/engine.rs:Engine.run", "rust_app/src/engine.rs:Engine.tick")
        self.has("scripts/deploy.sh:<top-level>", "scripts/deploy.sh:deploy")
        self.has("scripts/deploy.sh:deploy", "scripts/deploy.sh:build")

    def test_rescan_stable(self):
        g2 = self.p.scan()
        self.assertEqual(edge_set(g2), self.e)

    def test_python_instance_types(self):
        self.has("py_app/main.py:main", "py_app/pipeline/preprocess.py:Augmenter.apply")

    def test_entries(self):
        top = {self.g["symbols"][e["id"]]["f"] + ":" + self.g["symbols"][e["id"]]["q"]
               for e in self.g["entries"] if e["score"] >= 50}
        for want in ["py_app/main.py:__main__", "js_app/src/server.js:<top-level>", "go_app/main.go:main",
                     "java_app/App.java:App.main", "c_app/main.c:main", "rust_app/src/main.rs:main",
                     "scripts/deploy.sh:<top-level>"]:
            self.assertIn(want, top)

    def test_flow(self):
        items = self.p.flow("py_app/main.py", self.g["symbols"][next(
            k for k, s in self.g["symbols"].items() if s["q"] == "main" and s["f"] == "py_app/main.py")]["s"])["items"]
        kinds = [i["t"] for i in items]
        self.assertIn("if", kinds)
        self.assertIn("loop", kinds)
        r = self.p.flow("js_app/src/router.js", 3)["items"]
        self.assertEqual(r[0]["t"], "switch")
        self.assertEqual(len(r[0]["cases"]), 3)
        j = self.p.flow("java_app/App.java", 30)["items"]
        self.assertEqual(j[0]["t"], "try")


if __name__ == "__main__":
    unittest.main()
