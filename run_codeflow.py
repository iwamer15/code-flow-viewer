"""exe (PyInstaller) 用の入口。python -m codeflow と同じ動きをする。"""
import multiprocessing

from codeflow.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()   # 並列解析のワーカーを exe から起動するため
    main()
