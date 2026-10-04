@echo off
setlocal
set "PYTHONUTF8=1"
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Project Python not found: .venv\Scripts\python.exe
    echo Create the virtual environment and install requirements first.
    pause
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -u "reset_download_history.py"
set "reset_exit=%errorlevel%"
echo.
pause
popd
exit /b %reset_exit%
