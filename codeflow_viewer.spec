# PyInstaller 設定:  pyinstaller codeflow_viewer.spec --noconfirm --clean
#   → dist/CodeFlowViewer/ フォルダ (中の CodeFlowViewer.exe を起動)
# 事前に python -m codeflow.vendor を実行しておくと、画面のライブラリも同梱され完全オフラインで動く。
#
# exe 1個 (onefile) にしないのは、起動のたびに一時フォルダへ展開されウイルス対策ソフトの検査が毎回走り、
# 起動に約10秒かかったため (フォルダ形式なら2回目以降 0.3 秒)。
datas = [
    ("codeflow/static", "codeflow/static"),
    ("codeflow/tracer.py", "codeflow"),        # ユーザーの Python で実行するので .py のまま同梱
    ("codeflow/tracer_node.js", "codeflow"),
]

a = Analysis(["run_codeflow.py"], pathex=["."], datas=datas, hiddenimports=[], excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CodeFlowViewer", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CodeFlowViewer", upx=False)
