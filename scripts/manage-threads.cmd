@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
set "ENTRY=%PROJECT_ROOT%\manage-threads.pyw"
set "PYTHONW="

if defined PROGRESS_PYTHONW if exist "%PROGRESS_PYTHONW%" set "PYTHONW=%PROGRESS_PYTHONW%"
if not defined PYTHONW if exist "%LOCALAPPDATA%\CodexProgressToolbox\Python\pythonw.exe" set "PYTHONW=%LOCALAPPDATA%\CodexProgressToolbox\Python\pythonw.exe"
if not defined PYTHONW if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe" set "PYTHONW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if not defined PYTHONW for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-*") do call :consider_runtime "%%~fD"
if not defined PYTHONW for /f "delims=" %%P in ('py.exe -3 -c "import sys; print(sys.executable)" 2^>nul') do call :consider_python "%%P"
if not defined PYTHONW for /f "delims=" %%P in ('where pythonw.exe 2^>nul ^| findstr /v /i "\WindowsApps\"') do if not defined PYTHONW set "PYTHONW=%%P"

if not defined PYTHONW (
    echo Python windowed runtime was not found.
    echo Run scripts\install.ps1 or set PROGRESS_PYTHONW to pythonw.exe.
    pause
    exit /b 1
)
if not exist "%ENTRY%" (
    echo Thread manager entry point was not found.
    pause
    exit /b 1
)

start "" "%PYTHONW%" "%ENTRY%"
exit /b 0

:consider_runtime
if defined PYTHONW exit /b 0
if not exist "%~1\python.exe" exit /b 0
if not exist "%~1\pythonw.exe" exit /b 0
"%~1\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHONW=%~1\pythonw.exe"
exit /b 0

:consider_python
if defined PYTHONW exit /b 0
if not exist "%~dp1pythonw.exe" exit /b 0
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHONW=%~dp1pythonw.exe"
exit /b 0
