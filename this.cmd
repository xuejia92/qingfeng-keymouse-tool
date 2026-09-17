@echo off
rem ============================================================
rem  this.cmd - 把当前工作区提交并推送到 GitHub (origin)
rem
rem    this.cmd              提交并推送，提交信息固定 qingfeng
rem    this.cmd "提交说明"    用自定义提交信息
rem    this.cmd --check      只做环境自检，不提交、不推送
rem    this.cmd --help       显示本帮助
rem ============================================================
chcp 936 >nul
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "BRANCH="
set "MSG=qingfeng"
set "CHECKONLY="
set "NETOK="
set "REMOTE_HAS="
rem  显式用 System32 的工具：Git 自带的 usr\bin 里也有 find/findstr 同名程序，
rem  一旦 PATH 里排前面，命令行参数含义完全不同（会去遍历整个磁盘）
set "FIND=%SystemRoot%\System32\find.exe"
set "FINDSTR=%SystemRoot%\System32\findstr.exe"

if "%~1"=="" goto :args_done
if /i "%~1"=="--check" goto :arg_check
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="/?" goto :usage
set "MSG=%~1"
goto :args_done
:arg_check
set "CHECKONLY=1"
:args_done

rem ---------- 0. 找 git ----------
rem  机器 PATH 里已装了 Git，这里再兜一层，免得 PATH 被改坏后脚本失效
set "PATH=%PATH%;D:\Program Files\Git\cmd;C:\Program Files\Git\cmd"
where git >nul 2>&1
if errorlevel 1 goto :no_git

rem ---------- 1. 权限自检 ----------
rem  本目录属主不是当前 Windows 用户时，git 2.35+ 会拒绝一切操作，报
rem  "fatal: detected dubious ownership in repository"。
rem  这里用 -c 只在本次命令内放行，不改动你的全局 git 配置。
set "GIT=git -c "safe.directory=%REPO%""

%GIT% rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto :no_access

rem  还没提交过的分支只有 symbolic-ref 拿得到名字
for /f "delims=" %%b in ('%GIT% symbolic-ref --short HEAD 2^>nul') do set "BRANCH=%%b"
if not defined BRANCH for /f "delims=" %%b in ('%GIT% rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if not defined BRANCH goto :no_access

if defined CHECKONLY goto :do_check

rem ---------- 2. 提交身份自检 ----------
rem  本机没有全局 user.name / user.email，不补上的话 commit 会直接失败
for /f "delims=" %%e in ('%GIT% config user.email 2^>nul') do set "EMAIL=%%e"
if defined EMAIL goto :identity_ok
echo [提示] 本机没有配置 git 提交身份，按本仓库历史提交的身份写入（只影响本仓库）。
%GIT% config --local user.name "dusy"
%GIT% config --local user.email "1922884595@qq.com"
if errorlevel 1 goto :err_identity
for /f "delims=" %%e in ('%GIT% config user.email 2^>nul') do set "EMAIL=%%e"
:identity_ok
for /f "delims=" %%n in ('%GIT% config user.name 2^>nul') do set "UNAME=%%n"

call :header
echo.

rem ---------- 3. 同步远端 ----------
echo --- 同步远端 ---
%GIT% fetch origin %BRANCH%
if errorlevel 1 goto :fetch_failed
set "NETOK=1"
echo     已获取远端最新状态。

rem ---------- 4. 暂存 ----------
:stage
echo --- 本次改动 ---
%GIT% status --short
echo --- 暂存 ---
%GIT% add -A
if errorlevel 1 goto :err_add

%GIT% diff --cached --quiet
set "RC=!errorlevel!"
if "!RC!"=="0" goto :nothing_commit
if "!RC!"=="1" goto :has_commit
goto :err_add

:nothing_commit
echo     没有需要提交的改动，跳过提交。
goto :after_commit

:has_commit
echo --- 提交 ---
%GIT% commit -m "%MSG%"
if errorlevel 1 goto :err_commit

rem ---------- 5. 合并远端更新（只允许快进） ----------
:after_commit
if not defined NETOK goto :do_push
echo --- 合并远端更新 ---
%GIT% merge --ff-only origin/%BRANCH%
if errorlevel 1 goto :diverged
echo     已是最新。

rem ---------- 6. 推送 ----------
:do_push
echo --- 推送到 GitHub ---
%GIT% push origin %BRANCH%
if errorlevel 1 goto :err_push

for /f "delims=" %%h in ('%GIT% rev-parse --short HEAD 2^>nul') do set "NEWREV=%%h"
echo.
echo [完成] %BRANCH% @ %NEWREV% 已推送到 origin
echo.
pause
exit /b 0

rem ----- fetch 失败：分清「连不上」和「远端还没这个分支」 -----
:fetch_failed
set "QFLS=%TEMP%\qf_ls_remote.txt"
%GIT% ls-remote --heads origin > "%QFLS%" 2>nul
if errorlevel 1 goto :fetch_net
"%FINDSTR%" /c:"refs/heads/%BRANCH%" "%QFLS%" >nul 2>&1
if errorlevel 1 goto :fetch_newbranch
goto :fetch_net

:fetch_newbranch
del "%QFLS%" >nul 2>&1
echo [提示] 远端还没有 %BRANCH% 分支（首次推送），跳过同步。
echo.
goto :stage

:fetch_net
del "%QFLS%" >nul 2>&1
echo [警告] 连不上 GitHub，跳过同步：网络或代理问题。
echo        本地会照常提交，稍后重新运行本脚本即可补推。
echo        若需要走代理，执行一次：
echo          git config --global http.proxy http://127.0.0.1:7897
echo.
goto :stage

rem ============================ 分支 ============================

:do_check
for /f "delims=" %%e in ('%GIT% config user.email 2^>nul') do set "EMAIL=%%e"
for /f "delims=" %%n in ('%GIT% config user.name 2^>nul') do set "UNAME=%%n"
if defined EMAIL goto :check_header
set "UNAME=(未配置，正式运行时会自动补上)"
set "EMAIL=(未配置)"
:check_header
call :header
echo --- 远端地址 ---
%GIT% remote -v
echo --- 最近 3 次提交 ---
%GIT% log --oneline -3
echo --- 本地领先 / 远端领先的提交数 ---
for /f "delims=" %%a in ('%GIT% rev-list --count origin/%BRANCH%..%BRANCH% 2^>nul') do echo     待推送: %%a 条
for /f "delims=" %%b in ('%GIT% rev-list --count %BRANCH%..origin/%BRANCH% 2^>nul') do echo     待合并: %%b 条
echo --- 改动文件数 ---
for /f %%c in ('%GIT% status --porcelain ^| "%FIND%" /c /v ""') do echo     共 %%c 项
echo.
echo [自检完成] 未做任何提交或推送。
echo.
pause
exit /b 0

:header
echo.
echo ============================================================
echo  仓库 : %REPO%
echo  分支 : %BRANCH%   (远端 origin)
echo  身份 : %UNAME% ^<%EMAIL%^>
echo  说明 : %MSG%
echo ============================================================
exit /b 0

rem ============================ 出错 ============================

:no_git
echo [错误] 没有找到 git 命令。
echo        请安装 Git for Windows，或把它的 cmd 目录加入 PATH。
echo        常见位置：D:\Program Files\Git\cmd
goto :halt

:no_access
echo [错误] git 拒绝访问本目录，仓库属主不是当前 Windows 用户。
echo        本脚本已自动用 -c safe.directory 放行，仍失败说明路径对不上。
echo        手动执行一次可永久解决：
echo          git config --global --add safe.directory "%REPO%"
goto :halt

:err_identity
echo [错误] 写入 git 提交身份失败。
goto :halt

:err_add
echo [错误] git add 失败，请看上面的输出。
goto :halt

:err_commit
echo [错误] git commit 失败，请看上面的输出。
goto :halt

:diverged
echo [错误] 远端有本地没有的提交，无法自动快进合并。
echo        请手动处理后再运行本脚本：
echo          git pull --rebase origin %BRANCH%
goto :halt

:err_push
echo [错误] 推送失败。常见原因：
echo        - 网络或代理不通，可先执行 git config --global http.proxy http://127.0.0.1:7897
echo        - GitHub 登录凭据过期，需要重新登录
echo        - 远端有新提交，先执行 git pull --rebase origin %BRANCH%
goto :halt

:usage
echo.
echo   this.cmd - 把当前工作区提交并推送到 GitHub
echo.
echo     this.cmd                 提交并推送，提交信息 qingfeng
echo     this.cmd "提交说明"       用自定义提交信息
echo     this.cmd --check         只做环境自检，不提交、不推送
echo     this.cmd --help          显示本帮助
echo.
echo   脚本会自动处理三件事：脚本目录的中文路径、目录属主权限，
echo   以及本机没有配置 git 提交身份。
echo.
pause
exit /b 0

:halt
echo.
pause
exit /b 1
