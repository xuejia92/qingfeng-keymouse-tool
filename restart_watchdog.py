"""restart_watchdog.py —— 源码模式热重启看门狗（配合 restart.bat 使用）。

行为
----
启动 `python main.py`，并监听「重启请求」。请求到达时先请主程序**优雅退出**
（广播退出消息 → 主程序 aboutToQuit → 关掉残留浏览器、正常收尾），超时才强制结束；
随后本进程返回退出码 0，由 restart.bat 重新拉起。

重启请求来源（默认只用控制台按键，不抢系统热键）
- **控制台内按 Ctrl+R（默认）**：用 msvcrt 直接读控制台按键，不开全局键盘钩子，
  因此不会抢浏览器 / 编辑器的 Ctrl+R，也不会和主程序自己的全局热键（shift+f1 等）打架。
- **全局热键（可选）**：设环境变量 QINGFENG_RESTART_HOTKEY=ctrl+alt+r（keyboard 库
  支持任意组合）后额外注册全局热键；触发时只在「控制台 / 本程序窗口处于前台」时才
  重启，避免在别处按组合键误伤（QINGFENG_RESTART_HOTKEY_GLOBAL=1 可关掉这个前台
  限制，恢复成"任何程序里按都生效"）。

退出码（restart.bat 依赖，不可随意改）
- 0  收到重启请求 → bat 重新启动
- 1  主程序自己退出了（用户点退出/关窗口）→ bat 结束
- 2  环境不可用（解释器缺依赖 / 找不到 main.py）→ bat 打印帮助后结束
- 3  主程序异常退出（非 0 退出码）→ bat 打印日志提示后结束
- 4  主程序启动后立刻退出（多半已有实例在跑）→ bat 把提示留在屏幕上再结束
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(SCRIPT_DIR, "main.py")

# 主程序启动时**必然**要导入的运行时依赖（缺一个 main.py 就跑不起来）
REQUIRED_MODULES = ("PySide6", "keyboard", "mss", "cv2", "numpy", "PIL", "DrissionPage")

EXIT_RESTART = 0
EXIT_STOP = 1
EXIT_ENV_ERROR = 2
EXIT_CRASH = 3
EXIT_ALREADY_RUNNING = 4

CONSOLE_KEY_CHAR = "\x12"          # 控制台里 Ctrl+R 读到的是控制字符 0x12
ENV_GLOBAL_HOTKEY = "QINGFENG_RESTART_HOTKEY"
ENV_GLOBAL_NO_GATE = "QINGFENG_RESTART_HOTKEY_GLOBAL"

GRACEFUL_QUIT_TIMEOUT = 5.0        # 等主程序优雅退出的上限
FORCE_KILL_TIMEOUT = 3.0           # terminate / kill 后的等待上限
FAST_EXIT_SEC = 3.0                # 启动后这么快就退出且退出码 0 → 多半是"已有实例在跑"

QUIT_MESSAGE_NAME = "QingFengKeyMouseTool_Quit"   # 必须与 app/instance_lock.py 同名


def say(text: str = "") -> None:
    """控制台输出：控制台代码页可能编不出某些字符，退化成 ascii 也别抛异常。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------- 纯逻辑（可测）

def is_store_alias(path: str) -> bool:
    """是否 Windows 应用商店的 python 占位符（WindowsApps 下的 0 字节 reparse point）。

    这种"python.exe"被执行时会直接弹出应用商店，用来探测依赖是灾难性的，
    所以探测前必须先把它排掉（bat 里也做了一次同样的判断）。
    """
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    if "windowsapps" in p:
        return True
    try:
        return os.path.isfile(p) and os.path.getsize(p) == 0
    except OSError:
        return False


def probe_modules(python_exe: str, modules=REQUIRED_MODULES) -> str | None:
    """在指定解释器里逐个试导入 modules。

    返回第一个缺失的模块名；全都可导入返回 None；解释器本身跑不起来返回描述串。
    """
    if is_store_alias(python_exe):
        return "<Windows 应用商店的 python 占位符，不是真的解释器>"
    code = (
        "import importlib, sys\n"
        "missing = ''\n"
        "for m in {mods!r}:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        missing = m\n"
        "        break\n"
        "sys.stdout.write(missing)\n"
    ).format(mods=list(modules))
    try:
        r = subprocess.run([python_exe, "-c", code],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "<解释器无响应>"
    except OSError as e:
        return f"<无法启动解释器: {e}>"
    if r.returncode != 0:
        return "<解释器运行失败>"
    return r.stdout.strip() or None


def exit_code_for(restart_requested: bool, child_rc: int | None) -> int:
    """由「是否收到重启请求」和「子进程退出码」推出看门狗自己的退出码。"""
    if restart_requested:
        return EXIT_RESTART
    if child_rc not in (0, None):
        return EXIT_CRASH
    return EXIT_STOP


def hotkey_scope(env=None) -> str:
    """决定热键方案：只有控制台按键（console）/ 控制台 + 全局热键（global）。"""
    env = os.environ if env is None else env
    return "global" if (env.get(ENV_GLOBAL_HOTKEY) or "").strip() else "console"


def gate_enabled(env=None) -> bool:
    """全局热键是否启用"前台限制"（默认启用）。"""
    env = os.environ if env is None else env
    return (env.get(ENV_GLOBAL_NO_GATE) or "").strip().lower() not in ("1", "true", "yes", "on")


def startup_verdict(restart_requested: bool, child_rc: int | None, ran_sec: float,
                    fast_exit_sec: float = FAST_EXIT_SEC) -> tuple[int, str]:
    """返回 (退出码, 给用户看的一句话说明)。"""
    if restart_requested:
        return EXIT_RESTART, "已收到重启请求，重新拉起 main.py"
    if child_rc not in (0, None):
        return EXIT_CRASH, f"main.py 异常退出（退出码 {child_rc}），看看上面的报错再重跑"
    if ran_sec < fast_exit_sec:
        return EXIT_ALREADY_RUNNING, (
            "main.py 启动后立刻退出且未报错 —— 通常是**已经有一个实例在跑**"
            "（单实例保护会把已有窗口置前，新实例直接退出）。"
            "要重启就先退出那个实例（托盘图标 → 退出）。")
    return EXIT_STOP, "main.py 已正常退出"


# ---------------------------------------------------------------- 进程 / 窗口工具

def _process_parent_map() -> dict[int, int]:
    """{pid: 父pid}（toolhelp32 快照）。取不到返回空 dict。"""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    TH32CS_SNAPPROCESS = 0x2
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    out: dict[int, int] = {}
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def parent_chain(pid: int, depth: int) -> set[int]:
    """pid 往上 depth 层的父进程 pid 集合（用于判断"前台窗口是不是我的控制台宿主"）。"""
    if depth <= 0:
        return set()
    try:
        parents = _process_parent_map()
    except Exception:
        return set()
    out: set[int] = set()
    cur = pid
    for _ in range(depth):
        cur = parents.get(cur, 0)
        if not cur or cur in out:
            break
        out.add(cur)
    return out


def _console_window_pid() -> int:
    """本进程所连控制台窗口的宿主进程 pid（conhost/OpenConsole），取不到返回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetConsoleWindow.restype = ctypes.c_void_p
        hwnd = user32.GetConsoleWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def allowed_pids(child_pid: int) -> set[int]:
    """允许"按热键即重启"的进程集合：看门狗自己、主程序、控制台宿主、两层父进程。

    父进程链是为了覆盖 Windows Terminal / VS Code 内置终端这类"控制台窗口其实属于
    终端程序"的情况 —— 此时前台窗口是终端进程，而不是 conhost。
    """
    pids = {os.getpid(), int(child_pid or 0)}
    pids |= parent_chain(os.getpid(), 2)
    pids.add(_console_window_pid())
    pids.discard(0)
    return pids


def foreground_is_ours(child_pid: int) -> bool:
    """前台窗口是否属于「控制台 / 本看门狗 / 主程序」。

    ⚠️ 判断不出来（异常、拿不到窗口）一律**放行**：宁可偶尔多重启一次，
    也不能把"按了 Ctrl+R 却没反应"这种更坏的结果留给用户。
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetConsoleWindow.restype = ctypes.c_void_p
        fg = user32.GetForegroundWindow()
        if not fg:
            return True
        if fg == user32.GetConsoleWindow():
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(pid))
        return int(pid.value) in allowed_pids(child_pid)
    except Exception:
        return True


# ---------------------------------------------------------------- 重启请求来源

class RestartRequest:
    """线程安全的重启请求标志（热键回调 / 控制台线程只置位，主线程消费）。"""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def request(self) -> None:
        self._flag.set()

    @property
    def requested(self) -> bool:
        return self._flag.is_set()

    def wait(self, timeout: float) -> bool:
        return self._flag.wait(timeout)


def start_console_hotkey(req: RestartRequest) -> bool:
    """后台线程读控制台按键：Ctrl+R → 请求重启。可用返回 True。"""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        msvcrt.kbhit()          # 没有控制台时这里就会失败，提前暴露
    except OSError:
        return False

    def loop() -> None:
        while not req.requested:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == CONSOLE_KEY_CHAR:
                        say("")
                        say("  [Ctrl+R] 收到重启请求")
                        req.request()
                        return
            except OSError:
                return
            except Exception:
                return
            time.sleep(0.03)

    threading.Thread(target=loop, daemon=True, name="console-hotkey").start()
    return True


def start_global_hotkey(req: RestartRequest, child_pid: int) -> str | None:
    """按环境变量注册全局热键，成功返回组合键名。"""
    spec = (os.environ.get(ENV_GLOBAL_HOTKEY) or "").strip().lower()
    if not spec:
        return None
    try:
        import keyboard
    except Exception as e:
        say(f"  [!] 全局热键不可用（keyboard 库导入失败：{e}），只能用控制台 Ctrl+R")
        return None
    gate = gate_enabled()

    def on_hotkey() -> None:
        if gate and not foreground_is_ours(child_pid):
            return               # 在别的程序里按的，不当重启
        say("")
        say(f"  [{spec}] 收到重启请求")
        req.request()

    try:
        keyboard.add_hotkey(spec, on_hotkey)
    except Exception as e:
        say(f"  [!] 注册全局热键 {spec} 失败：{e}")
        return None
    return spec


# ---------------------------------------------------------------- 关停子进程

def request_graceful_quit() -> bool:
    """广播「退出」消息给主程序（同名消息在不同进程注册会得到同一个 id）。

    这里刻意不 import app.instance_lock：那个模块会拉起 PySide6，而看门狗要能在
    "依赖刚好齐全"的最小环境里跑。消息名保持一致即可。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.RegisterWindowMessageW.restype = ctypes.c_uint
        user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_void_p]
        msg = int(user32.RegisterWindowMessageW(QUIT_MESSAGE_NAME))
        if not msg:
            return False
        return bool(user32.PostMessageW(ctypes.c_void_p(0xFFFF), msg, None, None))
    except Exception:
        return False


def wait_exit(child, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if child.poll() is not None:
            return True
        time.sleep(0.1)
    return child.poll() is not None


def _force_kill(child) -> None:
    if child.poll() is not None:
        return
    for kill in (child.terminate, child.kill):
        try:
            kill()
            child.wait(timeout=FORCE_KILL_TIMEOUT)
            return
        except Exception:
            continue


def shutdown_child(child, graceful: bool = True) -> None:
    """结束子进程：先请它优雅退出，超时才强杀。"""
    if child.poll() is not None:
        return
    if graceful and request_graceful_quit():
        say("  正在请求主程序退出（先关掉残留浏览器、正常收尾）…")
        if wait_exit(child, GRACEFUL_QUIT_TIMEOUT):
            return
        say(f"  主程序 {GRACEFUL_QUIT_TIMEOUT:.0f} 秒内没退出，改为强制结束")
    _force_kill(child)


# ---------------------------------------------------------------- 入口

def self_check() -> int:
    """--check：只做环境体检，不启动主程序（restart.bat 用它做前置校验）。"""
    say(f"  解释器   : {sys.executable}")
    say(f"  Python   : {sys.version.split()[0]}")
    say(f"  程序目录 : {SCRIPT_DIR}")
    ok = True
    if not os.path.isfile(ENTRY):
        say(f"  [x] 找不到主程序入口：{ENTRY}")
        ok = False
    missing = probe_modules(sys.executable)
    if missing:
        if missing.startswith("<"):
            say(f"  [x] 解释器无法用来跑主程序：{missing}")
        else:
            say(f"  [x] 缺少依赖模块：{missing}")
        say(f"      安装依赖：\"{sys.executable}\" -m pip install -r \"{os.path.join(SCRIPT_DIR, 'requirements.txt')}\"")
        ok = False
    if ok:
        say("  [√] 环境检查通过，可以启动 main.py")
    return 0 if ok else EXIT_ENV_ERROR


def _print_banner(hotkey_spec: str | None, console_ok: bool) -> None:
    say("")
    say("=" * 52)
    say("  清风自动化键鼠工具 · 源码模式")
    if console_ok:
        say("  在本控制台窗口按 Ctrl+R 重启主程序")
    if hotkey_spec:
        say(f"  全局热键 {hotkey_spec} 同样可以重启"
            + ("" if gate_enabled() else "（无前台限制）"))
    if not console_ok and not hotkey_spec:
        say("  [!] 没有可用的重启热键，只能关掉主程序来结束")
    say("  按 Ctrl+C 结束主程序并退出本脚本")
    say("=" * 52)


def main() -> int:
    argv = sys.argv[1:]
    if "--check" in argv:
        return self_check()
    if "-h" in argv or "--help" in argv:
        say(__doc__ or "")
        return 0
    passthrough = [a for a in argv if a != "--"]

    if not os.path.isfile(ENTRY):
        say(f"[x] 找不到主程序入口：{ENTRY}")
        return EXIT_ENV_ERROR
    missing = probe_modules(sys.executable)
    if missing:
        say(f"[x] 当前解释器缺少依赖（{missing}）：{sys.executable}")
        say(f"    请先执行：\"{sys.executable}\" -m pip install -r "
            f"\"{os.path.join(SCRIPT_DIR, 'requirements.txt')}\"")
        return EXIT_ENV_ERROR

    child = subprocess.Popen([sys.executable, ENTRY, *passthrough], cwd=SCRIPT_DIR)
    req = RestartRequest()
    console_ok = start_console_hotkey(req)
    hotkey_spec = start_global_hotkey(req, child.pid)
    _print_banner(hotkey_spec, console_ok)
    started = time.monotonic()

    try:
        try:
            while child.poll() is None:
                if req.wait(0.2):
                    shutdown_child(child)
                    break
        except KeyboardInterrupt:
            say("")
            say("  [Ctrl+C] 结束主程序…")
            shutdown_child(child)
            return EXIT_STOP
    finally:
        # 兜底：无论走哪条路（含 Ctrl+C 期间再按一次 Ctrl+C）都不能把主程序
        # 丢下——否则控制台没了、热键也没了，只剩一个没人管的实例在后台跑。
        if child.poll() is None:
            _force_kill(child)

    child_rc = child.poll()
    ran = time.monotonic() - started
    code, message = startup_verdict(req.requested, child_rc, ran)
    say(f"  {message}")
    return code


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = EXIT_STOP
    except BaseException:
        import traceback
        traceback.print_exc()
        rc = EXIT_CRASH
    # 热键钩子（keyboard 的监听线程）可能拖住解释器退出，这里直接落盘退出码
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
