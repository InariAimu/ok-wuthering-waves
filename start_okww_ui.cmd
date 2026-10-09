@echo off
setlocal

rem Always run from the repository root, including when double-clicked.
cd /d "%~dp0"

if not exist "%~dp0main.py" (
    echo [OK-WW] Cannot find main.py in "%~dp0".
    pause
    exit /b 2
)

set "OKWW_PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "OKWW_PYTHON=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0.venv\bin\python.exe" (
    set "OKWW_PYTHON=%~dp0.venv\bin\python.exe"
) else if exist "%~dp0.venv\bin\python" (
    set "OKWW_PYTHON=%~dp0.venv\bin\python"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [OK-WW] No repository .venv or global Python was found.
        echo [OK-WW] Create .venv and install the project dependencies first.
        pause
        exit /b 3
    )
    set "OKWW_PYTHON=python"
)

if /i "%OKWW_LAUNCH_DRY_RUN%"=="1" (
    echo [OK-WW] Working directory: %CD%
    echo [OK-WW] Python: %OKWW_PYTHON%
    echo [OK-WW] Entry: %~dp0main.py
    exit /b 0
)

echo [OK-WW] Starting UI with "%OKWW_PYTHON%"...
"%OKWW_PYTHON%" "%~dp0main.py" %*
set "OKWW_EXIT_CODE=%ERRORLEVEL%"

if not "%OKWW_EXIT_CODE%"=="0" (
    echo.
    echo [OK-WW] UI exited with code %OKWW_EXIT_CODE%.
    pause
)

exit /b %OKWW_EXIT_CODE%
