@echo off
setlocal
set PYTHONIOENCODING=utf-8
python3.13.exe "%~dp0oracle.py" %*
endlocal