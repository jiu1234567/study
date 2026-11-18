@echo off
rem ASCII only on purpose (see watch_docs.bat).
chcp 65001 >nul
cd /d "%~dp0"
set PY=D:\Python\python.exe
if not exist "%PY%" set PY=python
echo === WeChat watcher: detect a Tencent Docs card and click it open ===
echo     Use watch_docs.bat for the full flow (this one only clicks).
echo Quit: Ctrl+C in this window.
echo.
"%PY%" watch_link.py --config watch_config.txt %*
echo.
pause
