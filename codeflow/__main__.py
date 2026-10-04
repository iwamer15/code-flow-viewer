"""python -m codeflow [フォルダ] [--port 8765] [--no-browser]"""
import argparse
import multiprocessing
import os
import sys

from .server import serve


def _safe_console():
    """日本語を表示できないコンソール (cp1252 など) で print が落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _safe_console()
    ap = argparse.ArgumentParser(prog="codeflow", description="ソースコードの処理フロー可視化 + エディタ")
    ap.add_argument("root", nargs="?", default=None, help="読み込むフォルダ (省略時はカレントフォルダ)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--ask", action="store_true", help="フォルダを指定せず起動し、画面で選ぶ (前回のフォルダを復元)")
    a = ap.parse_args()
    # exe をダブルクリックしたときは、カレントフォルダ(=exe の場所)ではなく画面でフォルダを選ぶ
    frozen = getattr(sys, "frozen", False)
    root = a.root or (None if (a.ask or frozen) else os.getcwd())
    serve(root, a.port, not a.no_browser)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows の exe 化対策
    main()
