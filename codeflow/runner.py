"""プログラムの実行管理 (起動・出力の取得・入力・停止・トレーサからの実行状況の受信)。

同時に動かすのは1つだけ。画面は /api/run/status をポーリングして出力と実行状況を受け取る。
"""
from __future__ import annotations

import codecs
import json
import locale
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

TRACER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracer.py")
MAX_CHUNKS = 4000
IS_WIN = sys.platform == "win32"


def find_python(root: str) -> str:
    """プロジェクト内の仮想環境があればその Python、なければこのツールを動かしている Python。"""
    for d in (".venv", "venv", "env"):
        for p in (("Scripts", "python.exe"), ("bin", "python3"), ("bin", "python")):
            cand = os.path.join(root, d, *p)
            if os.path.isfile(cand):
                return cand
    return sys.executable


class Run:
    def __init__(self, root: str, spec: dict):
        self.root = root
        self.spec = spec
        self.chunks: list[str] = []
        self.base = 0            # chunks から捨てた数 (seq の補正用)
        self.snapshot: dict | None = None
        self.code: int | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.traced = spec.get("mode") == "python"
        self.listener: socket.socket | None = None
        self.proc: subprocess.Popen | None = None

    # ---- 起動 ----
    def start(self):
        spec = self.spec
        cwd = os.path.realpath(os.path.join(self.root, spec.get("cwd") or ""))
        if not os.path.isdir(cwd) or not (os.path.normcase(cwd) + os.sep).startswith(os.path.normcase(os.path.realpath(self.root)) + os.sep):
            raise ValueError("作業フォルダは読み込んだフォルダの中を指定してください")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        shell = False
        if self.traced:
            py = spec.get("python") or find_python(self.root)
            args = shlex.split(spec.get("args") or "", posix=not IS_WIN)
            if spec.get("module"):
                cmd = [py, TRACER, "module", spec["module"], *args]
            else:
                target = os.path.realpath(os.path.join(self.root, spec["target"]))
                if not os.path.isfile(target):
                    raise ValueError(f"ファイルが見つかりません: {spec['target']}")
                cmd = [py, TRACER, "script", target, *args]
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            env["CODEFLOW_ROOT"] = self.root
            env["CODEFLOW_PORT"] = str(self.listener.getsockname()[1])
            env["CODEFLOW_TOKEN"] = self.token
            threading.Thread(target=self._accept, daemon=True).start()
            enc = "utf-8"
        else:
            cmd = spec.get("command") or ""
            if not cmd.strip():
                raise ValueError("コマンドを入力してください")
            shell = True
            enc = "utf-8" if not IS_WIN else (locale.getpreferredencoding(False) or "utf-8")
        self.decoder = codecs.getincrementaldecoder(enc)(errors="replace")
        kw = {}
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        self.cmdline = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, **kw)
        threading.Thread(target=self._read_output, daemon=True).start()

    def _accept(self):
        try:
            self.listener.settimeout(60)
            conn, _ = self.listener.accept()
        except OSError:
            return
        finally:
            try:
                self.listener.close()
            except OSError:
                pass
        f = conn.makefile("rb")
        try:
            if f.readline().decode().strip() != self.token:
                return
            for line in f:
                try:
                    snap = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                with self.lock:
                    self.snapshot = snap
        except OSError:
            pass
        finally:
            conn.close()

    def _read_output(self):
        out = self.proc.stdout
        while True:
            try:
                data = out.read(8192)
            except OSError:
                break
            if not data:
                break
            self._append(self.decoder.decode(data))
        self._append(self.decoder.decode(b"", final=True))
        self.code = self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        # トレーサの最終報告 (done) が届くのを少しだけ待つ
        for _ in range(20):
            with self.lock:
                if not self.traced or (self.snapshot or {}).get("done"):
                    break
            time.sleep(0.05)
        self.ended = time.time()

    def _append(self, text):
        if not text:
            return
        with self.lock:
            self.chunks.append(text)
            if len(self.chunks) > MAX_CHUNKS:
                drop = len(self.chunks) - MAX_CHUNKS
                del self.chunks[:drop]
                self.base += drop

    # ---- 操作 ----
    def status(self, since: int) -> dict:
        with self.lock:
            start = max(0, since - self.base)
            text = "".join(self.chunks[start:])
            seq = self.base + len(self.chunks)
            snap = self.snapshot
        running = self.ended is None
        return {
            "running": running, "code": self.code, "seq": seq, "text": text,
            "truncated": since < self.base, "elapsed": round((self.ended or time.time()) - self.started, 2),
            "traced": self.traced, "snapshot": snap, "cmd": self.cmdline,
        }

    def send_input(self, text: str):
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            try:
                self.proc.stdin.write((text + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                pass

    def stop(self):
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_WIN:
                # 子プロセスごと終了させる
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(2)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                p.kill()
            except OSError:
                pass


class RunManager:
    def __init__(self):
        self.current: Run | None = None
        self.lock = threading.Lock()

    def start(self, root: str, spec: dict) -> dict:
        with self.lock:
            if self.current and self.current.ended is None:
                raise ValueError("実行中のプログラムがあります。停止してから実行してください")
            run = Run(root, spec)
            run.start()
            self.current = run
            return {"ok": True, "cmd": run.cmdline}

    def status(self, since: int) -> dict:
        if not self.current:
            return {"running": False, "none": True}
        return self.current.status(since)

    def stop(self):
        if self.current:
            self.current.stop()

    def send_input(self, text: str):
        if self.current:
            self.current.send_input(text)
