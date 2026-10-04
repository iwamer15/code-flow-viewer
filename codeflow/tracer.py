"""実行トレーサ (対象プログラムと同じプロセスで動く)。

    python tracer.py script <ファイル> [引数...]
    python tracer.py module <モジュール名> [引数...]

環境変数 CODEFLOW_ROOT / CODEFLOW_PORT / CODEFLOW_TOKEN でサーバへ実行状況を送る。
このファイルは単体で動くよう標準ライブラリだけを使う (プロジェクトの venv の Python からも直接起動される)。

集めるもの:
  * 関数ごとの呼び出し回数 … Python 3.12+ は sys.monitoring (プロジェクト外のコードは即無効化するので軽い)、
                               それ以前は sys.setprofile
  * 関数ごとの経過時間     … 10ms ごとに全スレッドのスタックをサンプリング (累計=スタック上にいた時間、自身=最内側だった時間)
  * いま実行中の位置       … メインスレッドのスタック (プロジェクト内の関数だけ)
"""
import json
import os
import runpy
import socket
import sys
import threading
import time
import traceback

_ROOT_REAL = os.path.realpath(os.environ.get("CODEFLOW_ROOT") or os.getcwd())
_ROOT_N = os.path.normcase(_ROOT_REAL)
_SELF_N = os.path.normcase(os.path.realpath(__file__))
_SKIP_PARTS = {".venv", "venv", "env", "site-packages", "dist-packages", "node_modules", "__pycache__", ".tox"}
_rel_cache = {}


def rel_of(filename):
    """プロジェクト内のファイルなら相対パス ('/' 区切り)、それ以外は ''。"""
    r = _rel_cache.get(filename)
    if r is not None:
        return r
    r = ""
    if filename and not filename.startswith("<"):
        try:
            real = os.path.realpath(filename)
            n = os.path.normcase(real)
            if n.startswith(_ROOT_N + os.sep) and n != _SELF_N:
                rel = os.path.relpath(real, _ROOT_REAL).replace(os.sep, "/")
                parts = rel.split("/")
                if not any(p in _SKIP_PARTS or p.startswith(".") for p in parts[:-1]):
                    r = rel
        except (OSError, ValueError):
            r = ""
    _rel_cache[filename] = r
    return r


calls = {}        # (rel, firstlineno, name) -> 回数
incl = {}         # -> 累計秒
selft = {}        # -> 自身の秒
_lock = threading.Lock()


def _count(code):
    rel = rel_of(code.co_filename)
    if not rel:
        return False
    k = (rel, code.co_firstlineno, code.co_name)
    calls[k] = calls.get(k, 0) + 1
    return True


def install_counter():
    mon = getattr(sys, "monitoring", None)
    if mon is not None:
        for tid in (mon.PROFILER_ID, 3, 4, 5):
            try:
                mon.use_tool_id(tid, "codeflow")
            except ValueError:
                continue

            def on_start(code, _offset, _mon=mon):
                if not _count(code):
                    return _mon.DISABLE
                return None

            mon.register_callback(tid, mon.events.PY_START, on_start)
            mon.set_events(tid, mon.events.PY_START)
            return "sys.monitoring"

    # 3.11 以前: setprofile はジェネレータの再開でも呼ばれるので、コードオブジェクト単位でキャッシュして最小限の処理にする
    keys = {}

    def prof(frame, event, _arg):
        if event != "call":
            return
        code = frame.f_code
        k = keys.get(code)
        if k is None:
            rel = rel_of(code.co_filename)
            k = keys[code] = (rel, code.co_firstlineno, code.co_name) if rel else False
        if k:
            if frame.f_lasti > 0 and code.co_flags & 0x2A0:   # 再開したジェネレータ/コルーチンは数えない
                return
            calls[k] = calls.get(k, 0) + 1

    sys.setprofile(prof)
    threading.setprofile(prof)
    return "sys.setprofile"


def stack_of(frame):
    out = []
    while frame is not None:
        code = frame.f_code
        rel = rel_of(code.co_filename)
        if rel:
            out.append([rel, code.co_firstlineno, code.co_name, frame.f_lineno])
        frame = frame.f_back
    out.reverse()
    return out


class Reporter(threading.Thread):
    def __init__(self, main_ident):
        super().__init__(daemon=True, name="codeflow-reporter")
        self.main_ident = main_ident
        self.sock = None
        self.t0 = time.perf_counter()
        self.stop_ev = threading.Event()
        self.exc = None
        self.how = ""
        port = os.environ.get("CODEFLOW_PORT")
        if port:
            try:
                self.sock = socket.create_connection(("127.0.0.1", int(port)), timeout=3)
                self.sock.sendall((os.environ.get("CODEFLOW_TOKEN", "") + "\n").encode())
            except OSError:
                self.sock = None

    def snapshot(self, done=False):
        frames = sys._current_frames()
        main = stack_of(frames.get(self.main_ident))
        others = []
        for tid, fr in frames.items():
            if tid in (self.main_ident, self.ident):
                continue
            st = stack_of(fr)
            if st:
                others.append(st)
        keys = set(calls) | set(incl)
        fn = [[k[0], k[1], k[2], calls.get(k, 0), round(incl.get(k, 0.0) * 1000, 1), round(selft.get(k, 0.0) * 1000, 1)]
              for k in keys]
        msg = {"t": round(time.perf_counter() - self.t0, 3), "stack": main, "threads": others[:8], "fn": fn,
               "done": done, "how": self.how, "py": "%d.%d" % sys.version_info[:2]}
        if self.exc:
            msg["exc"] = self.exc
        return msg

    def send(self, msg):
        if not self.sock:
            return
        try:
            self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            self.sock = None

    def run(self):
        last_send = 0.0
        last = time.perf_counter()
        while not self.stop_ev.wait(0.01):
            now = time.perf_counter()
            dt = now - last
            last = now
            try:
                for tid, fr in sys._current_frames().items():
                    if tid == self.ident:
                        continue
                    st = stack_of(fr)
                    if not st:
                        continue
                    k = tuple(st[-1][:3])
                    selft[k] = selft.get(k, 0.0) + dt
                    for key in {tuple(f[:3]) for f in st}:
                        incl[key] = incl.get(key, 0.0) + dt
                if now - last_send >= 0.15:
                    last_send = now
                    self.send(self.snapshot())
            except Exception:  # noqa: BLE001  計測の失敗で対象プログラムを止めない
                pass

    def finish(self):
        self.stop_ev.set()
        self.send(self.snapshot(done=True))
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("script", "module"):
        print("usage: tracer.py script|module <target> [args...]", file=sys.stderr)
        return 2
    mode, target, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    rep = Reporter(threading.get_ident())
    rep.start()
    # 呼び出し回数を数えるか: auto(既定)=sys.monitoring が使える 3.12 以降だけ数える / 1=常に数える / 0=数えない
    # (3.11 以前の setprofile はジェネレータの再開ごとにも呼ばれ、処理によっては数倍遅くなるため既定では使わない)
    mode_count = os.environ.get("CODEFLOW_COUNT", "auto")
    if mode_count == "1" or (mode_count == "auto" and hasattr(sys, "monitoring")):
        how = install_counter()
    else:
        how = "sampling"
    rep.how = how
    if os.environ.get("CODEFLOW_VERBOSE"):
        print(f"[codeflow] tracing with {how}", file=sys.stderr)
    code = 0
    try:
        if mode == "script":
            path = os.path.abspath(target)
            sys.argv = [path] + args
            sys.path[0] = os.path.dirname(path)
            runpy.run_path(path, run_name="__main__")
        else:
            sys.argv = [target] + args
            sys.path[0] = os.getcwd()
            runpy.run_module(target, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        c = e.code
        code = c if isinstance(c, int) else (0 if c is None else 1)
        if c is not None and not isinstance(c, int):
            print(c, file=sys.stderr)
    except BaseException as e:  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = None
        for fr in reversed(tb):
            rel = rel_of(fr.filename)
            if rel:
                where = [rel, fr.lineno]
                break
        rep.exc = {"type": type(e).__name__, "msg": str(e)[:500], "where": where}
        # 通常の python と同じ表示にする (トレーサ自身のフレームは除く)
        lines = traceback.format_exception(type(e), e, e.__traceback__)
        print("".join(l for l in lines if "tracer.py" not in l and "runpy.py" not in l), end="", file=sys.stderr)
        code = 1
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        rep.finish()
    return code


if __name__ == "__main__":
    sys.exit(main())
