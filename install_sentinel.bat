@echo off
setlocal
set PY="C:\Users\h04x\AppData\Local\Microsoft\WindowsApps\pythonw3.13.exe"
set SCRIPT="%~dp0sentinel.py"
set TASK=TradingSentinel

schtasks /Create /F /TN %TASK% /TR "%PY% %SCRIPT%" /SC ONLOGON /RL LIMITED /IT
if %errorlevel%==0 (
    echo [OK] Task %TASK% installed. Sentinel will start at every logon, no console window.
    echo      Run now: python3.13.exe "%~dp0sentinel.py"
    echo      Remove:   schtasks /Delete /TN %TASK% /F
) else (
    echo [FAIL] Could not install task. Run as admin if needed.
)
endlocal