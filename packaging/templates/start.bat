@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PRODUCT_RESEARCH_ENGINE=%~dp0
if "%XUANPIN_PORT%"=="" set XUANPIN_PORT=8800
if not exist ".venv\Scripts\python.exe" (
  echo Not installed yet. Run install.bat first.
  pause
  exit /b 1
)
echo Starting the workbench on http://127.0.0.1:%XUANPIN_PORT%  (local only; close this window to stop)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:%XUANPIN_PORT%"
".venv\Scripts\python.exe" -m research_workflow.web serve --port %XUANPIN_PORT% --insecure-cookies
echo.
echo The workbench has stopped.
pause
