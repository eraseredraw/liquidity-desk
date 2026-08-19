@echo off
rem Recalibrate per-symbol strategy params: grid search + OOS check
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python3.13.exe grid_backtest.py
if %errorlevel% neq 0 (
  echo Grid search failed. Check network.
  pause
  exit /b 1
)
python3.13.exe backtest.py
pause