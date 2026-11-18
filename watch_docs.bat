@echo off
rem All messages here are ASCII on purpose: Chinese text in a .bat gets mangled
rem under different console code pages, and the script itself prints Chinese fine.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set PY=D:\Python\python.exe
if not exist "%PY%" set PY=python
echo === Full auto: click the Tencent Docs card in WeChat, then fill from recent docs ===
echo First run: a Chrome window opens; scan the QR with WeChat once.
echo Quit: Ctrl+C in this window.
echo.
"%PY%" watch_docs.py --config watch_config.txt %*
echo.
pause
