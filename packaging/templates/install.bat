@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
echo Installing the cross-border product research workbench...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo Install FAILED with exit code %ERR%. See the messages above.
) else (
  echo.
  echo Install finished. Double-click start.bat to open the workbench.
)
rem Set XUANPIN_NO_PAUSE=1 when running unattended (e.g. from Codex) to skip the pause below.
if "%XUANPIN_NO_PAUSE%"=="" pause
exit /b %ERR%
