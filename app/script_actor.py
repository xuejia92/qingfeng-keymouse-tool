"""执行脚本步骤：运行 CMD / BAT / PowerShell / Python 脚本并捕获输出。

设计要点：
- 只用标准库 subprocess，不引入第三方依赖。
- 脚本来源两种：
    · 文本内容（source=text）：按所选编码写入临时 .bat/.ps1/.py 后执行；
    · 脚本文件（source=file）：原样执行本地文件，保留 %~dp0 与相对路径，
      不复制改写用户文件。
- 输出统一捕获：
    · 隐藏窗口 / 保留命令窗口 / 管理员权限三种运行方式，都把 stdout+stderr
      重定向到一个临时文件再读回，避免「提权跨进程拿不到管道输出」的问题。
- 隐藏窗口（hidden）：CREATE_NO_WINDOW 不弹黑框，同步等待脚本结束；
- 保留命令窗口（keep）：新开可见控制台（CREATE_NEW_CONSOLE），脚本跑完后
  打印输出并 pause，用户关闭窗口后流程继续；
- 管理员权限（admin）：用 PowerShell `Start-Process -Verb RunAs -Wait` 触发
  UAC 提权，提权进程仍把输出写到同一临时文件（同一用户令牌可写 %TEMP%）。

编码（utf-8 无 BOM 默认 / gb2312 / utf-8-sig / ascii）：
- 文本来源：控制脚本文件的写出编码；文件来源：控制脚本输出的解读编码。
- CMD/BAT 走 cmd.exe，执行前按所选编码显式 `chcp`（936/65001/437），
  脚本与输出编码确定、不依赖系统默认 OEM 代码页（否则中文会乱码）；
- PowerShell 用 `Out-File -Encoding` 显式控制输出文件编码，与所选编码对齐；
- Python 用 `PYTHONIOENCODING` 强制 stdout/stderr 编码；GB2312 编码的源码会在
  文件头补 PEP 263 声明 `# -*- coding: gbk -*-`，否则会被按 UTF-8 误读；
- 输出解码失败一律 errors="replace" 兜底，不因编码问题中断流程。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

# 编码：值 -> Python codec
ENCODINGS = {
    "gb2312": "gb2312",
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "ascii": "ascii",
}
ENCODING_LABELS = {
    "utf-8": "UTF-8 无 BOM（默认）",
    "utf-8-sig": "UTF-8 有 BOM",
    "gb2312": "GB2312",
    "ascii": "ASCII",
}

# PowerShell Out-File -Encoding 参数（与所选编码对齐）
_PS_ENCODING = {
    "gb2312": "Default",   # ANSI，中文系统即 gbk
    "utf-8": "UTF8",
    "utf-8-sig": "UTF8BOM",
    "ascii": "ASCII",
}

# cmd.exe 代码页：按所选编码显式 chcp，保证脚本与输出编码确定，
# 不依赖系统默认 OEM 代码页（否则 UTF-8 默认代码页的系统上中文会乱码）。
_CMD_CHCP = {
    "gb2312": "936",
    "utf-8": "65001",
    "utf-8-sig": "65001",
    "ascii": "437",
}

# Python 的 PYTHONIOENCODING：强制 Python stdout/stderr 按所选编码写出，
# 否则 Windows 上管道输出默认走 locale（中文系统 cp936），与所选编码不一致会乱码。
_PY_IO_ENCODING = {
    "gb2312": "gbk",
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8",
    "ascii": "ascii",
}

# PEP 263 源码编码声明（首行或 shebang 之后的第二行）
_CODING_DECL_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")


class ScriptError(Exception):
    """执行脚本步骤的可预期失败（内容为空 / 文件缺失 / 编码错误 / 超时 / UAC 取消等）。"""


def _codec(encoding: str) -> str:
    return ENCODINGS.get((encoding or "utf-8").strip().lower(), "utf-8")


def _kind(script_type: str) -> str:
    t = (script_type or "cmd").strip().lower()
    if t in ("powershell", "ps1", "ps"):
        return "powershell"
    if t in ("python", "py", "py3"):
        return "python"
    return "cmd"   # cmd / bat 都走 cmd.exe


def _write_temp(prefix: str, suffix: str, content: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _bat_launcher(command: str, out_path: str, keep: bool, chcp: str,
                  setup_lines: tuple[str, ...] = ()) -> str:
    """生成 cmd 启动器：setup 行（如 set 环境变量）+ chcp + 执行命令重定向输出到文件；
    keep 时打印输出并 pause 保持窗口。标签用纯 ASCII，避免启动器编码出岔。"""
    lines = ["@echo off", *setup_lines]
    if chcp:
        lines.append(f"chcp {chcp} >nul")   # 显式代码页，脚本与输出编码确定
    lines.append(f'{command} > "{out_path}" 2>&1')
    if keep:
        lines += ["echo.", "echo ---- output ----",
                  f'type "{out_path}"', "echo.", "pause"]
    return "\r\n".join(lines) + "\r\n"


def _cmd_launcher(script_path: str, out_path: str, keep: bool, chcp: str) -> str:
    """生成 cmd 启动器（CMD/BAT）：先按编码切代码页，再 call 脚本并重定向输出。"""
    return _bat_launcher(f'call "{script_path}"', out_path, keep, chcp)


def _py_launcher(py_exe: str, script_path: str, out_path: str, keep: bool,
                 chcp: str, io_encoding: str) -> str:
    """生成 Python 执行启动器（cmd 包装）：set PYTHONIOENCODING 后运行 python 脚本。"""
    return _bat_launcher(f'"{py_exe}" "{script_path}"', out_path, keep, chcp,
                         setup_lines=(f"set PYTHONIOENCODING={io_encoding}",))


def _has_coding_decl(text: str) -> bool:
    """内容前两行是否已含 PEP 263 编码声明（避免重复追加）。"""
    for line in (text or "").splitlines()[:2]:
        if _CODING_DECL_RE.match(line):
            return True
    return False


def _ps_launcher(script_path: str, out_path: str, keep: bool, ps_enc: str) -> str:
    """生成 PowerShell 启动器：合并所有输出流写到文件；keep 时回显并 Read-Host。"""
    lines = [f'& "{script_path}" 2>&1 | Out-String | '
             f'Out-File -FilePath "{out_path}" -Encoding {ps_enc}']
    if keep:
        lines += [f'Get-Content "{out_path}" -Encoding {ps_enc}',
                  'Read-Host "Press Enter to close"']
    return "\r\n".join(lines) + "\r\n"


def _read_output(path: str, codec: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    return data.decode(codec, errors="replace")


def _run_command(cmd: list[str], keep: bool, timeout: float | None,
                 capture: bool = False):
    """执行一条命令；keep 时新开可见控制台且不设超时（等用户关窗）。"""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NEW_CONSOLE if keep else _CREATE_NO_WINDOW
    if not keep and timeout is not None:
        kwargs["timeout"] = timeout
    if capture:
        kwargs["capture_output"] = True
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise ScriptError(f"脚本执行超时（{timeout:g} 秒）") from None
    except OSError as e:
        raise ScriptError(f"启动解释器失败：{e}") from e


def _run_admin(target: str, args: list[str] | None, timeout: float | None) -> int:
    """用 PowerShell Start-Process -Verb RunAs 提权执行，返回外层退出码。

    用户取消 UAC 授权时 Start-Process 抛错，这里用 try/catch 捕获并返回 1603，
    供调用方区分「已取消授权」与正常完成。
    """
    if args:
        arg_list = ",".join(f"'{a}'" for a in args)
        expr = (f"try {{ Start-Process -FilePath '{target}' "
                f"-ArgumentList {arg_list} -Verb RunAs -Wait | Out-Null; exit 0 }} "
                f"catch {{ Write-Error $_; exit 1603 }}")
    else:
        expr = (f"try {{ Start-Process -FilePath '{target}' "
                f"-Verb RunAs -Wait | Out-Null; exit 0 }} "
                f"catch {{ Write-Error $_; exit 1603 }}")
    cmd = ["powershell", "-NoProfile", "-Command", expr]
    kwargs: dict = {"capture_output": True}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        r = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise ScriptError(f"脚本执行超时（{timeout:g} 秒）") from None
    except OSError as e:
        raise ScriptError(f"无法提权执行：{e}") from e
    if r.returncode == 1603:
        raise ScriptError("已取消管理员权限授权（UAC）")
    if r.returncode != 0:
        err = (r.stderr or b"").decode("gbk", errors="replace").strip()
        raise ScriptError(f"提权执行失败：{err or r.returncode}")
    return 0


def run_script(*, script_type: str = "cmd", source: str = "text",
               content: str = "", path: str = "", encoding: str = "utf-8",
               window_mode: str = "hidden", admin: bool = False,
               timeout: float = 120.0) -> dict:
    """执行一次脚本，返回 {"returncode": int, "output": str}；失败抛 ScriptError。

    returncode：隐藏/保留窗口（非提权）模式为脚本解释器的退出码；
    提权模式下无法可靠取得脚本自身退出码，统一返回 0（失败以异常抛出）。
    """
    kind = _kind(script_type)
    codec = _codec(encoding)
    chcp = _CMD_CHCP.get(codec, "936")
    keep = (window_mode or "hidden").strip() == "keep"
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 120.0
    if timeout <= 0:
        timeout = 120.0

    # 1. 确定脚本文件
    if (source or "text").strip() == "file":
        path = (path or "").strip()
        if not path:
            raise ScriptError("未指定脚本文件路径")
        if not os.path.isfile(path):
            raise ScriptError(f"脚本文件不存在：{path}")
        script_path = os.path.abspath(path)
        script_is_temp = False
    else:
        content = content or ""
        if not content.strip():
            raise ScriptError("脚本内容为空")
        if kind == "powershell":
            ext = ".ps1"
        elif kind == "python":
            ext = ".py"
        else:
            ext = ".bat"
        try:
            data = content.encode(codec)
        except UnicodeEncodeError as e:
            label = ENCODING_LABELS.get(encoding, encoding or "UTF-8 无 BOM")
            raise ScriptError(f"脚本内容无法按 {label} 编码：{e}") from e
        # Python 默认按 UTF-8 读源码；GB2312 编码需在文件头加 PEP 263 声明，
        # 否则中文源码会被按 UTF-8 误读导致 SyntaxError。
        if kind == "python" and codec == "gb2312" and not _has_coding_decl(content):
            data = b"# -*- coding: gbk -*-\n" + data
        script_path = _write_temp("qf_script_", ext, data)
        script_is_temp = True

    # 2. 输出临时文件
    fd, out_path = tempfile.mkstemp(prefix="qf_script_out_", suffix=".txt")
    os.close(fd)

    tmp_files: list[str] = [out_path]
    returncode = 0
    try:
        if kind == "powershell":
            ps_enc = _PS_ENCODING.get(codec, "Default")
            launcher = _write_temp(
                "qf_ps_launcher_", ".ps1",
                _ps_launcher(script_path, out_path, keep, ps_enc).encode("utf-8-sig"))
            tmp_files.append(launcher)
            if admin:
                returncode = _run_admin(
                    "powershell.exe",
                    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher],
                    None if keep else timeout)
            else:
                r = _run_command(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", launcher], keep, None if keep else timeout)
                returncode = r.returncode
        else:
            # cmd / python 都走 cmd.exe 启动器。.bat 启动器不能带 BOM（cmd 会把 BOM
            # 当成首行内容导致首条命令失效），故 utf-8-sig 落为 utf-8；其余与脚本编码
            # 一致，保证 chcp 后中文路径可被正确读取。
            if kind == "python":
                py = shutil.which("python") or shutil.which("py")
                if not py:
                    raise ScriptError("未找到 Python 解释器（请安装 Python 并加入 PATH）")
                io_encoding = _PY_IO_ENCODING.get(codec, "utf-8")
                launcher_text = _py_launcher(
                    py, script_path, out_path, keep, chcp, io_encoding)
                prefix = "qf_py_launcher_"
            else:
                launcher_text = _cmd_launcher(script_path, out_path, keep, chcp)
                prefix = "qf_cmd_launcher_"
            launcher_codec = "utf-8" if codec == "utf-8-sig" else codec
            try:
                launcher_bytes = launcher_text.encode(launcher_codec)
            except UnicodeEncodeError as e:
                label = ENCODING_LABELS.get(encoding, encoding or "UTF-8 无 BOM")
                raise ScriptError(
                    f"脚本路径无法按 {label} 编码（路径含该编码不支持的字符）：{e}") from e
            launcher = _write_temp(prefix, ".bat", launcher_bytes)
            tmp_files.append(launcher)
            if admin:
                returncode = _run_admin(launcher, None, None if keep else timeout)
            else:
                r = _run_command(["cmd", "/c", launcher], keep,
                                 None if keep else timeout)
                returncode = r.returncode

        output = _read_output(out_path, codec)
    finally:
        if script_is_temp:
            _safe_remove(script_path)
        for f in tmp_files:
            _safe_remove(f)

    return {"returncode": returncode, "output": output}
