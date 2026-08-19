@echo off
setlocal
set PYTHONIOENCODING=utf-8
python3.13.exe "%~dp0advice.py" %*
endlocal
