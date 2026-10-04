@echo off
rem Code Flow Viewer launcher (Windows)
rem  - Double click: choose a folder in the browser (restores the last folder)
rem  - Drag and drop a folder onto this file: open that folder
rem  (ASCII only on purpose: cmd.exe misreads UTF-8 Japanese text in .bat files)
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if "%~1"=="" (
  %PY% -m codeflow --ask
) else (
  %PY% -m codeflow "%~1"
)
if errorlevel 1 pause
