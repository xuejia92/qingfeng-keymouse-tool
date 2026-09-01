@echo off
rem ============================================================
rem  restart.bat - run python main.py; press Ctrl+R to restart
rem
rem  Usage : double-click or run in command line
rem  Deps  : keyboard lib (already in requirements.txt)
rem
rem  Behaviors:
rem   - while main.py is running, press Ctrl+R to restart it
rem   - when main.py exits by itself, this script exits too
rem ============================================================
setlocal
cd /d "%~dp0"

:restart
echo.
echo ============================================
echo   python main.py is running ...
echo   press Ctrl+R to restart; close program to exit
echo ============================================
python restart_watchdog.py
if errorlevel 1 goto :eof
echo   Ctrl+R pressed, restarting main.py ...
goto restart
