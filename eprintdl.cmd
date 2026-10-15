@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONPATH=%CD%"

set "PYEXE="
if exist "%CD%\.venv\Scripts\python.exe" set "PYEXE=%CD%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%CD%\.venv\bin\python" set "PYEXE=%CD%\.venv\bin\python"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "C:\Python313\python.exe" set "PYEXE=C:\Python313\python.exe"
if not defined PYEXE if exist "C:\Python312\python.exe" set "PYEXE=C:\Python312\python.exe"
if not defined PYEXE for /f "usebackq delims=" %%i in (`where python 2^>nul`) do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE for /f "usebackq delims=" %%i in (`where py 2^>nul`) do if not defined PYEXE set "PYEXE=%%i"

if not defined PYEXE (
  echo eprintdl: Python not found. Install Python 3.10 or newer. 1>&2
  exit /b 2
)

"%PYEXE%" -X utf8 -m eprintdl %*
exit /b %ERRORLEVEL%
