@echo off
setlocal
set "APP_ROOT=%~dp0"
set "PYTHON_RUNTIME=%APP_ROOT%runtime\python311\pythonw.exe"
set "APP_SCRIPT=%APP_ROOT%pop_bf_lab.py"

if not exist "%PYTHON_RUNTIME%" (
    echo PoP BF Lab: Python 3.11 embedded runtime non trovato.
    echo Expected: "%PYTHON_RUNTIME%"
    pause
    exit /b 1
)

start "PoP BF Lab" /wait "%PYTHON_RUNTIME%" "%APP_SCRIPT%"
exit /b %ERRORLEVEL%
