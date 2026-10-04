"""オフライン用: 画面で使う JS ライブラリを static/vendor/ に取得する。

    python -m codeflow.vendor

ネットに繋がる PC で一度実行し、フォルダごと持っていけば社内 PC 等でもオフラインで動く。
"""
import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "static", "vendor")
FILES = {
    "cytoscape.min.js": "https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js",
    "dagre.min.js": "https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js",
    "cytoscape-dagre.js": "https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js",
    "mermaid.min.js": "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js",
}
MONACO_TGZ = "https://registry.npmjs.org/monaco-editor/-/monaco-editor-0.52.2.tgz"


def fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except urllib.error.URLError as e:
        if "CERTIFICATE" not in str(e).upper():
            raise
    # python.org 版 Python の証明書未設定や社内プロキシ対策: certifi → OS の curl の順に試す
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=120, context=ctx) as r:
            return r.read()
    except ImportError:
        pass
    curl = shutil.which("curl")
    if not curl:
        raise SystemExit("SSL 証明書を検証できません。certifi を入れるか curl を使える環境で実行してください")
    return subprocess.run([curl, "-fsSL", url], check=True, capture_output=True, timeout=300).stdout


def main():
    for stream in (sys.stdout, sys.stderr):   # 日本語を表示できないコンソールでも落ちないように
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    os.makedirs(VENDOR, exist_ok=True)
    for name, url in FILES.items():
        print("download", name)
        with open(os.path.join(VENDOR, name), "wb") as f:
            f.write(fetch(url))
    print("download monaco-editor (min/vs)")
    data = fetch(MONACO_TGZ)
    prefix = "package/min/vs/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            rel = m.name[len(prefix):]
            dest = os.path.join(VENDOR, "monaco", "vs", *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with tf.extractfile(m) as src, open(dest, "wb") as out:
                out.write(src.read())
    print("完了:", VENDOR)


if __name__ == "__main__":
    main()
