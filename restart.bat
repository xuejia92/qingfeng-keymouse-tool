@echo off
rem ============================================================
rem  restart.bat - run this tool from source (python main.py)
rem
rem  Usage: double-click this file, or run it from a terminal
rem
rem  Behavior:
rem   - starts main.py through restart_watchdog.py
rem   - press Ctrl+R inside THIS console window to restart the app
rem   - when the app quits by itself, this script exits
rem   - any error keeps this window open (pause) so you can read it
rem
rem  Env overrides:
rem   QINGFENG_PYTHON           full path of the interpreter to use
rem   QINGFENG_RESTART_HOTKEY   also register a global hotkey, e.g.
rem                             set QINGFENG_RESTART_HOTKEY=ctrl+alt+r
rem
rem  NOTE: keep this file pure ASCII + CRLF. cmd.exe reads .bat with the
rem  console code page (936 / 65001 / ...); non-ASCII bytes here have
rem  already broken this script twice (mangled commands). Messages that
rem  need Chinese live in restart_watchdog.py (Python handles encoding).
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
title QingFeng Automation - source mode (restart.bat)

set "PYCMD="
call :pick_python
if not defined PYCMD goto :no_python

rem one-shot modes of the watchdog: run once, show the result, stop.
rem (they exit 0, which would otherwise look like "restart requested")
if /i "%~1"=="--check" goto :passthru
if /i "%~1"=="--help" goto :passthru

echo.
echo   Interpreter: %PYCMD%

:restart
%PYCMD% "%~dp0restart_watchdog.py" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :restart
if "%RC%"=="1" goto :finished
if "%RC%"=="2" goto :env_error
if "%RC%"=="3" goto :app_crash
if "%RC%"=="4" goto :already_running

echo.
echo   [x] restart_watchdog.py exited with code %RC%
echo.
pause
goto :eof

:finished
echo.
echo   main.py has exited - nothing left to watch. Bye.
goto :eof

rem ---------------------------------------------------------------- helpers

:passthru
%PYCMD% "%~dp0restart_watchdog.py" %*
echo.
pause
goto :eof

:pick_python
rem 1) explicit override wins
if defined QINGFENG_PYTHON call :try_py "%QINGFENG_PYTHON%"
rem 2) project virtualenv
call :try_py "%~dp0.venv\Scripts\python.exe"
call :try_py "%~dp0venv\Scripts\python.exe"
call :try_py "%~dp0env\Scripts\python.exe"
rem 3) py launcher, then python / python3 found on PATH
if not defined PYCMD for /f "delims=" %%P in ('where py 2^>nul') do call :try_py "%%~fP" -3
if not defined PYCMD for /f "delims=" %%P in ('where python 2^>nul') do call :try_py "%%~fP"
if not defined PYCMD for /f "delims=" %%P in ('where python3 2^>nul') do call :try_py "%%~fP"
goto :eof

:try_py
if defined PYCMD goto :eof
set "CAND=%~1"
set "EXTRA=%~2"
if not exist "%CAND%" goto :eof
rem skip the Microsoft Store python stub: a 0-byte reparse point in
rem WindowsApps, executing it just opens the Store instead of running.
echo "%CAND%" | find /i "\WindowsApps\" >nul && goto :eof
for %%F in ("%CAND%") do if "%%~zF"=="0" goto :eof
set "PYCMD="%CAND%""
if defined EXTRA set "PYCMD="%CAND%" %EXTRA%"
goto :eof

:no_python
echo.
echo   [x] No usable Python interpreter was found.
echo.
echo   Looked for: QINGFENG_PYTHON, .venv\Scripts\python.exe,
echo               the "py" launcher, and python / python3 on PATH.
echo.
echo   How to fix (pick one):
echo     1) install Python 3.11+ from python.org and tick
echo        "Add python.exe to PATH", then run:
echo            python -m pip install -r requirements.txt
echo     2) point this script at an existing interpreter:
echo            set QINGFENG_PYTHON=D:\path\to\python.exe
echo     3) or use the packaged build in the dist folder
if exist "%~dp0dist" (
    echo.
    echo   Opening the dist folder for you ...
    start "" "%~dp0dist"
)
echo.
pause
goto :eof

:env_error
echo.
echo   [x] The interpreter cannot run main.py yet (see the message above).
echo       Install the runtime dependencies with:
echo           %PYCMD% -m pip install -r "%~dp0requirements.txt"
echo.
pause
goto :eof

:app_crash
echo.
echo   [x] main.py exited with an error - read the traceback above.
echo       Full log: "%~dp0app.log"
echo.
pause
goto :eof

:already_running
echo.
echo   [x] Another copy of the tool is already running (see the note above).
echo       Quit it first - tray icon / Quit - then run this script again.
echo.
pause
goto :eof
