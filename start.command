#!/bin/bash
# Code Flow Viewer 起動 (mac) — ダブルクリックで起動し、画面でフォルダを選ぶ
cd "$(dirname "$0")"
if [ -n "$1" ]; then python3 -m codeflow "$1"; else python3 -m codeflow --ask; fi
