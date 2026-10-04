@echo off
rem Code Flow Viewer 起動 (Windows)
rem   ・ダブルクリック: 画面でフォルダを選ぶ (前回のフォルダを復元)
rem   ・フォルダをこの bat にドラッグ&ドロップ: そのフォルダを読み込む
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if "%~1"=="" (
  %PY% -m codeflow --ask
) else (
  %PY% -m codeflow "%~1"
)
if errorlevel 1 pause
