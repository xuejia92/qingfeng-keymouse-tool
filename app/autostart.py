"""开机自启管理（Windows 注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run）。

- 注册目标始终是程序 exe 的绝对路径（自动获取），绝不注册 main.py 脚本：
  - exe 运行：当前进程自己的 exe（兼容 Nuitka onefile 指向临时解压目录的情况）
  - 源码运行：自动探测 dist/ 下打包好的主程序 exe；找不到时不注册，
    并清掉历史遗留的 main.py 注册
- 每次启动调用 ensure_registered()：未注册则写入；路径变化则修正
- 注册命令自带 --autostart 参数，据此区分"开机自启"与"手动双击"两种启动来源
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

from .config import APP_NAME, compiled_original_argv0, is_compiled

AUTOSTART_ARG = "--autostart"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = APP_NAME          # 注册表值名（测试中可替换）
logger = logging.getLogger(__name__)


def _under_dir(path: str, directory: str) -> bool:
    """path 是否位于 directory 目录内（Windows 路径大小写不敏感）。"""
    try:
        d = os.path.realpath(directory).lower()
        p = os.path.realpath(path).lower()
        return os.path.commonpath([d, p]) == d
    except (ValueError, OSError):
        return False


def _is_exe(path: str) -> bool:
    return bool(path) and path.lower().endswith(".exe") and os.path.isfile(path)


def _running_exe() -> str | None:
    """编译运行时当前进程 exe 的绝对路径；源码运行返回 None。"""
    if not is_compiled():
        return None
    candidates: list[str] = []
    # Nuitka onefile：优先用记录的原始 exe 路径，避免取到临时解压目录里的二进制
    orig = compiled_original_argv0()
    if orig:
        candidates.append(os.path.abspath(str(orig)))
    candidates.append(os.path.abspath(sys.executable))
    if sys.argv and sys.argv[0]:
        candidates.append(os.path.abspath(sys.argv[0]))
    tmp = tempfile.gettempdir()
    outside = [c for c in candidates if _is_exe(c) and not _under_dir(c, tmp)]
    if outside:
        return outside[0]
    inside = [c for c in candidates if _is_exe(c)]
    return inside[0] if inside else None


def _dist_exe() -> str | None:
    """源码运行：自动探测项目 dist/ 目录里打包好的主程序 exe 绝对路径。"""
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dist = os.path.join(project, "dist")
    if not os.path.isdir(dist):
        return None
    exes: list[str] = []
    for fn in os.listdir(dist):
        p = os.path.join(dist, fn)
        if os.path.isfile(p) and fn.lower().endswith(".exe"):
            exes.append(p)                      # 单文件形态：dist/xxx.exe
        elif os.path.isdir(p):                  # 目录形态：dist/xxx/xxx.exe
            for fn2 in os.listdir(p):
                p2 = os.path.join(p, fn2)
                if os.path.isfile(p2) and fn2.lower().endswith(".exe"):
                    exes.append(p2)
    if not exes:
        return None

    def rank(p: str):
        # 多个 exe 时：优先与程序同名的，其次取最新修改的
        name = os.path.splitext(os.path.basename(p))[0]
        return (0 if name == APP_NAME else 1, -os.path.getmtime(p))

    return sorted(exes, key=rank)[0]


def current_exe_path() -> str | None:
    """程序 exe 的绝对路径：当前进程的 exe 优先，源码运行则探测 dist/ 成品。"""
    return _running_exe() or _dist_exe()


def launch_command() -> str:
    """写入 Run 键的启动命令（exe 绝对路径 + 自启标记）；找不到 exe 时为空串。"""
    exe = current_exe_path()
    return f'"{exe}" {AUTOSTART_ARG}' if exe else ""


def _path_from_command(cmd: str | None) -> str:
    """从注册表命令里取出 exe 路径部分（兼容带引号/不带引号两种写法）。"""
    cmd = (cmd or "").strip()
    if cmd.startswith('"'):
        parts = cmd.split('"', 2)
        return parts[1] if len(parts) > 1 else cmd.strip('"')
    return cmd.split(" ", 1)[0]


def _read_value() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return None


def is_registered() -> bool:
    """Run 键中是否存在本程序的值。"""
    return _read_value() is not None


def ensure_registered() -> bool:
    """确保开机自启已注册且指向程序 exe：每次启动重新检查，路径不对就改写。

    - 已注册但命令与当前 exe 不一致（程序移动/改名/换机）-> 修正
    - 已注册但指向的 exe 文件已不存在（坏路径）-> 修正或清除
    返回最终是否处于已注册状态（非 Windows 平台恒为 False）。
    """
    if sys.platform != "win32":
        logger.info("非 Windows 平台，跳过开机自启注册")
        return False
    cmd = launch_command()
    old = _read_value()
    if not cmd:
        # 源码运行且没找到打包 exe：绝不注册 main.py；
        # 已有注册若是 main.py 或指向不存在的 exe（坏路径），一并清除
        old_exe = _path_from_command(old)
        if old and ("main.py" in old or not os.path.isfile(old_exe)):
            unregister()
            logger.info("未找到程序 exe，已清除无效的自启注册：%s", old)
            return False
        logger.info("未找到程序 exe，保留现有自启注册：%s", old)
        return is_registered()
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            if old == cmd:
                return True
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        if old:
            logger.info("开机自启路径已修正：%s\n  -> %s", old, cmd)
        else:
            logger.info("开机自启已注册：%s", cmd)
        return True
    except OSError:
        logger.warning("开机自启注册失败", exc_info=True)
        return False


def unregister() -> bool:
    """移除开机自启。注意：程序每次启动都会自动恢复注册。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        logger.info("已移除开机自启")
        return True
    except OSError:
        return False


def is_autostart_launch(argv=None) -> bool:
    """本次是否为开机自启进入（命令行带 --autostart 标记）。"""
    return AUTOSTART_ARG in (sys.argv if argv is None else argv)
