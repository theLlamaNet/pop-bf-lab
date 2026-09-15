@echo off
setlocal
set "APP_ROOT=%~dp0"
set "PYTHON_RUNTIME=%APP_ROOT%runtime\python311\python.exe"
set "APP_SCRIPT=%APP_ROOT%pop_bf_lab.py"
set "APP_LOG=%APP_ROOT%pop_bf_lab.log"

if not exist "%PYTHON_RUNTIME%" (
    echo PoP BF Lab: Python 3.11 embedded runtime not found.
    echo Expected: "%PYTHON_RUNTIME%"
    pause
    exit /b 1
)

pushd "%APP_ROOT%"
if errorlevel 1 (
    echo PoP BF Lab: Cannot open the program folder.
    pause
    exit /b 1
)
"%PYTHON_RUNTIME%" "%APP_SCRIPT%" > "%APP_LOG%" 2>&1
set "APP_EXIT=%ERRORLEVEL%"
popd
if not "%APP_EXIT%"=="0" (
    echo PoP BF Lab could not run. Error details:
    type "%APP_LOG%"
    echo.
    echo Log: "%APP_LOG%"
    pause
)
exit /b %APP_EXIT%
