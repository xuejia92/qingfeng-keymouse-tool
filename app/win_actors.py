"""Windows 窗口与应用执行器：句柄识别、后台键鼠、启动应用。

分三层能力：
1. 窗口句柄识别：WindowFromPoint（点坐标取句柄）、按标题找窗口、取窗口标题，
   供「打开应用」模块绑定窗口、鼠标/键盘后台操作指定目标窗口。
2. 后台键鼠：PostMessage 向指定窗口句柄发送鼠标/键盘消息，不抢前台焦点。
3. 启动应用：os.startfile / subprocess.Popen 打开本地程序。

为什么后台操作用 PostMessage 而不是 pynput：
pynput 的 SendInput 是「系统级注入」，永远作用于当前前台窗口，无法定向到
后台窗口；PostMessage 直接投递到目标窗口的消息队列，窗口不在前台也能收到。
代价：部分应用（尤其用 DirectInput/游戏引擎自绘的）不处理窗口消息，后台
键鼠对它们无效——这是 Windows 消息机制的通病，如实说明即可。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# ---- 窗口样式常量（窗口识别遮罩穿透自身用）----
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020

# subprocess 调用 taskkill 等控制台程序时隐藏黑框。程序打包成无控制台的
# windowed exe 后，subprocess 默认会给子进程新建一个控制台窗口，导致每次
# 执行「关闭应用」步骤都弹出一个 cmd 黑框一闪而逝（循环/多任务触发时就是
# 一连串黑框）。加 CREATE_NO_WINDOW 后彻底不弹。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- Win32 消息常量 ----
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102

MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

# 鼠标按键 -> (down, up, dblclk, mk 标志)
_BUTTON_MSGS = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, MK_MBUTTON),
}

# 常用键名 -> 虚拟键码（VK）。字符键在 key_to_vk 里单独处理。
_VK_MAP = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "capslock": 0x14, "printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13,
    "numlock": 0x90,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}


def _make_lparam(x: int, y: int) -> int:
    """打包客户区坐标到 lParam（低 16 位 x，高 16 位 y）。"""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


# ---------- 窗口句柄识别 ----------

def window_at_point(x: int, y: int) -> int:
    """屏幕坐标下的窗口句柄（0 = 未找到）。"""
    hwnd = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return int(hwnd or 0)


def _root_window(hwnd: int) -> int:
    """向上取顶层窗口（WindowFromPoint 可能返回子控件，取其根窗口）。"""
    try:
        root = _user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT = 2
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def window_title(hwnd: int) -> str:
    """取窗口标题；取不到返回空串。"""
    if not hwnd:
        return ""
    n = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def window_exists(hwnd: int) -> bool:
    """窗口句柄当前是否仍有效。"""
    return bool(hwnd) and bool(_user32.IsWindow(wintypes.HWND(hwnd)))


def find_window_by_title(title: str) -> int:
    """按标题精确匹配窗口，返回句柄；找不到返回 0。"""
    title = (title or "").strip()
    if not title:
        return 0
    hwnd = _user32.FindWindowW(None, title)
    return int(hwnd or 0)


def pick_window_at_point(x: int, y: int) -> tuple[int, str]:
    """屏幕坐标点取窗口：返回 (顶层句柄, 标题)。"""
    hwnd = _root_window(window_at_point(x, y))
    return hwnd, window_title(hwnd)


def cursor_pos() -> tuple[int, int]:
    """当前鼠标的屏幕物理坐标。"""
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def set_cursor_pos(x: int, y: int) -> bool:
    """把系统鼠标移动到指定屏幕物理坐标；成功返回 True。"""
    return bool(_user32.SetCursorPos(int(x), int(y)))


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口的屏幕物理矩形 (x, y, w, h)；无效返回 None。"""
    if not hwnd or not window_exists(hwnd):
        return None
    r = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def window_class(hwnd: int) -> str:
    """窗口类名；取不到返回空串。"""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value if n else ""


def cursor_window_info(skip_hwnd: int = 0) -> tuple[int, str, tuple[int, int, int, int] | None]:
    """当前鼠标下的顶层窗口信息：(句柄, 标题, 物理矩形)。

    skip_hwnd：需要跳过的窗口句柄（如识别遮罩自身）——查询前临时给它加
    WS_EX_TRANSPARENT 让 WindowFromPoint 穿透，查完恢复。
    """
    x, y = cursor_pos()
    if skip_hwnd:
        ex = _user32.GetWindowLongW(wintypes.HWND(skip_hwnd), _GWL_EXSTYLE)
        _user32.SetWindowLongW(wintypes.HWND(skip_hwnd), _GWL_EXSTYLE,
                               ex | _WS_EX_TRANSPARENT)
        try:
            hwnd = _root_window(window_at_point(x, y))
        finally:
            _user32.SetWindowLongW(wintypes.HWND(skip_hwnd), _GWL_EXSTYLE, ex)
    else:
        hwnd = _root_window(window_at_point(x, y))
    return hwnd, window_title(hwnd), window_rect(hwnd)


# ---------- 后台键鼠 ----------

def background_click(hwnd: int, button: str = "left", times: int = 1,
                     x: int | None = None, y: int | None = None) -> bool:
    """向指定窗口后台发送鼠标点击。x/y 为**屏幕坐标**（None = 当前鼠标位置）。

    lParam 需要客户区坐标，所以内部用 ScreenToClient 转换（旧实现把屏幕
    坐标直接当客户区坐标，导致点击整体偏移「边框+标题栏」，看起来像没生效）。
    点击前先发 WM_MOUSEMOVE，让目标窗口更新内部鼠标状态。

    times=2 时发 down/up 两次并夹一次 dblclk。返回是否成功投递。
    """
    if not hwnd or not window_exists(hwnd):
        return False
    down, up, dbl, mk = _BUTTON_MSGS.get(button, _BUTTON_MSGS["left"])
    hw = wintypes.HWND(hwnd)
    # 屏幕坐标 -> 客户区坐标
    if x is None or y is None:
        sx, sy = cursor_pos()
    else:
        sx, sy = int(x), int(y)
    pt = wintypes.POINT(sx, sy)
    _user32.ScreenToClient(hw, ctypes.byref(pt))
    lp = _make_lparam(pt.x, pt.y)
    for i in range(max(1, times)):
        _user32.PostMessageW(hw, WM_MOUSEMOVE, mk, lp)
        _user32.PostMessageW(hw, down, mk, lp)
        if times == 2 and i == 0:
            _user32.PostMessageW(hw, dbl, mk, lp)
        _user32.PostMessageW(hw, up, 0, lp)
    return True


def _key_lparam(vk: int, up: bool = False) -> int:
    """构造 WM_KEYDOWN/WM_KEYUP 的 lParam（含扫描码与释放标志）。

    很多程序会检查 lParam 里的扫描码，置 0 会导致它们忽略该按键。
    """
    sc = int(_user32.MapVirtualKeyW(vk, 0)) & 0xFF   # VK -> 扫描码
    lp = (sc << 16) | 1                              # bits16-23 扫描码 + 重复计数 1
    if up:
        lp |= (1 << 30) | (1 << 31)                  # 前一状态 + 转换状态(释放)
    return lp


def key_to_vk(name: str) -> int:
    """keyboard 库风格键名 -> 虚拟键码；无法转换抛 ValueError。"""
    name = (name or "").strip().lower()
    if name in _VK_MAP:
        return _VK_MAP[name]
    if len(name) == 1 and name.isprintable():
        ch = name
        vk = _user32.VkKeyScanW(ch)       # W 版本接受单字符，返回低 8 位 VK
        if vk != -1:
            return int(vk & 0xFF)
        # VkKeyScan 受键盘布局/输入法影响可能返回 -1，对 ASCII 做确定性兜底
        code = ord(ch)
        if "a" <= ch <= "z":
            return code - 32              # 字母 VK 与 ASCII 大写码一致（A=0x41）
        if "0" <= ch <= "9":
            return code                   # 数字 VK 与 ASCII 码一致（0=0x30）
    raise ValueError(f"不支持的按键: {name}")


def background_press(hwnd: int, keys: str) -> bool:
    """向指定窗口后台发送按键（keyboard 库格式，如 'space'、'ctrl+c'）。

    只处理单个非修饰键 + 可选修饰键组合；返回是否成功投递。
    """
    if not hwnd or not window_exists(hwnd):
        return False
    from .keymap import parse_combo
    mods, main = parse_combo(keys)
    hw = wintypes.HWND(hwnd)
    try:
        vk = key_to_vk(main)
    except ValueError:
        return False
    # 按下修饰键
    held = []
    for m in mods:
        try:
            mvk = key_to_vk(m)
        except ValueError:
            continue
        _user32.PostMessageW(hw, WM_KEYDOWN, mvk, _key_lparam(mvk))
        held.append(mvk)
    # 主键按下 + 字符 + 抬起
    _user32.PostMessageW(hw, WM_KEYDOWN, vk, _key_lparam(vk))
    try:
        ch = ord(main) if len(main) == 1 else 0
    except (TypeError, ValueError):
        ch = 0
    if ch:
        _user32.PostMessageW(hw, WM_CHAR, ch, 0)
    _user32.PostMessageW(hw, WM_KEYUP, vk, _key_lparam(vk, up=True))
    # 释放修饰键
    for mvk in reversed(held):
        _user32.PostMessageW(hw, WM_KEYUP, mvk, _key_lparam(mvk, up=True))
    return True


# ---------- 临时激活目标窗口（配合 SendInput 的后台方案） ----------
#
# PostMessage 是「真后台、不抢焦点」，但 UWP / Chrome / 游戏等现代应用不
# 处理窗口消息，投递了也不生效。要对它们生效，唯一可靠的纯用户态办法是：
# 短暂把目标窗口带到前台，用 SendInput（pynput）注入，再恢复原前台窗口。
# 用 AttachThreadInput 把当前线程与目标窗口线程绑定，可以绕过 Windows 对
# SetForegroundWindow 的「后台进程不得抢前台」限制，让激活几乎必定成功。

_prev_foreground = 0
_attached_tids: list[int] = []


def activate_window(hwnd: int) -> bool:
    """临时把目标窗口设为前台（AttachThreadInput 绕过抢焦点限制）。

    记录原前台窗口，配合 restore_foreground() 使用。返回是否成功。
    """
    global _prev_foreground, _attached_tids
    if not hwnd or not window_exists(hwnd):
        return False
    hw = wintypes.HWND(hwnd)
    _prev_foreground = int(_user32.GetForegroundWindow() or 0)
    target_tid = int(_user32.GetWindowThreadProcessId(hw, None) or 0)
    cur_tid = int(_kernel32.GetCurrentThreadId())
    _attached_tids = []
    if target_tid and target_tid != cur_tid:
        _user32.AttachThreadInput(cur_tid, target_tid, True)
        _attached_tids.append(target_tid)
    if _prev_foreground and _prev_foreground != hwnd:
        fg_tid = int(_user32.GetWindowThreadProcessId(
            wintypes.HWND(_prev_foreground), None) or 0)
        if fg_tid and fg_tid != cur_tid and fg_tid != target_tid:
            _user32.AttachThreadInput(cur_tid, fg_tid, True)
            _attached_tids.append(fg_tid)
    _user32.SetForegroundWindow(hw)
    _user32.SetFocus(hw)
    return True


def bring_to_front(hwnd: int) -> bool:
    """把窗口带到前台并保持（「打开应用」带出已运行实例用）。

    与 activate_window 的差别：activate_window 是后台键鼠的「临时置顶」——
    记录原前台、保留线程绑定，之后由 restore_foreground() 还原；本函数置顶后
    立即解除线程绑定、不记录还原，前台就停留在目标窗口。
    """
    if not hwnd or not window_exists(hwnd):
        return False
    SW_RESTORE = 9
    hw = wintypes.HWND(hwnd)
    # 最小化的窗口先还原再置顶（SetForegroundWindow 对最小化窗口无效）
    _user32.ShowWindow(hw, SW_RESTORE)
    target_tid = int(_user32.GetWindowThreadProcessId(hw, None) or 0)
    cur_tid = int(_kernel32.GetCurrentThreadId())
    attached: list[int] = []
    if target_tid and target_tid != cur_tid:
        if _user32.AttachThreadInput(cur_tid, target_tid, True):
            attached.append(target_tid)
    _user32.SetForegroundWindow(hw)
    _user32.SetFocus(hw)
    for tid in attached:
        try:
            _user32.AttachThreadInput(cur_tid, tid, False)
        except Exception:
            pass
    return True


def restore_foreground() -> None:
    """恢复 activate_window() 之前的前台窗口，并断开线程绑定。"""
    global _prev_foreground, _attached_tids
    if _prev_foreground:
        try:
            _user32.SetForegroundWindow(wintypes.HWND(_prev_foreground))
        except Exception:
            pass
    cur_tid = int(_kernel32.GetCurrentThreadId())
    for tid in _attached_tids:
        try:
            _user32.AttachThreadInput(cur_tid, tid, False)
        except Exception:
            pass
    _attached_tids = []
    _prev_foreground = 0


# ---------- 启动应用 ----------

def launch_app(path: str) -> tuple[bool, str]:
    """打开本地程序/文件/文件夹。返回 (成功?, 结果描述)。

    支持：绝对/相对路径、PATH 里的命令名（如 notepad.exe）、文档/快捷方式；
    目录路径用系统默认方式打开（资源管理器）。
    """
    path = (path or "").strip()
    if not path:
        return False, "未填写应用路径"
    if not os.path.exists(path):
        # 可能是 PATH 里的命令名（如 notepad.exe、calc.exe），用 shutil.which 解析
        resolved = shutil.which(path)
        if not resolved:
            return False, f"路径不存在：{path}"
        path = resolved
    try:
        os.startfile(path)               # 关联默认程序打开（exe/快捷方式/文档都行）
    except OSError:
        try:
            subprocess.Popen([path],
                             creationflags=_CREATE_NO_WINDOW)  # startfile 失败时兜底运行
        except OSError as e:
            return False, f"启动失败：{e}"
    return True, f"已启动 {os.path.basename(path)}"


# ---- 进程枚举（psapi + version，全 Unicode API，杜绝中文乱码） ----

_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_version32 = ctypes.WinDLL("version", use_last_error=True)
_version32.VerQueryValueW.argtypes = [
    ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _enum_pids() -> list[int]:
    """枚举所有进程 PID。"""
    n = 2048
    buf = (ctypes.c_uint * n)()
    needed = ctypes.c_uint()
    if not _psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        return []
    count = min(needed.value // ctypes.sizeof(ctypes.c_uint), n)
    return [int(buf[i]) for i in range(count)]


def _process_path(pid: int) -> str:
    """取进程的可执行文件完整路径（Unicode，无乱码）；失败返回空串。"""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint(4096)
        buf = ctypes.create_unicode_buffer(4096)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def _file_description(path: str) -> str:
    """从 exe 版本信息里取 FileDescription（如「Google Chrome」「记事本」）。

    遍历所有语言块（中文/英文等），取第一个非空描述；取不到返回空串。
    """
    try:
        size = _version32.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not _version32.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        trans_ptr = ctypes.c_void_p()
        trans_len = ctypes.c_uint()
        if not _version32.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                         ctypes.byref(trans_ptr),
                                         ctypes.byref(trans_len)):
            return ""
        for i in range(trans_len.value // 4):
            pair = ctypes.cast(trans_ptr.value + i * 4,
                               ctypes.POINTER(ctypes.c_ushort * 2)).contents
            lang, cp = int(pair[0]), int(pair[1])
            key = f"\\StringFileInfo\\{lang:04X}{cp:04X}\\FileDescription"
            desc_ptr = ctypes.c_void_p()
            desc_len = ctypes.c_uint()
            if _version32.VerQueryValueW(buf, key, ctypes.byref(desc_ptr),
                                         ctypes.byref(desc_len)):
                desc = ctypes.wstring_at(desc_ptr.value)
                if desc.strip():
                    return desc.strip()
    except Exception:
        pass
    return ""


def _windows_by_pid() -> dict[int, list[str]]:
    """可见窗口 -> 按 PID 分组收集窗口标题（用于区分多开实例）。"""
    result: dict[int, list[str]] = {}
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n <= 0:
            return True
        pid = ctypes.c_uint()
        _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
        result.setdefault(int(pid.value), []).append(buf.value)
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return result


def list_processes() -> list[dict]:
    """列出当前**有可见窗口**的运行中进程，供「关闭应用」选择。

    只保留有主窗口的进程，过滤掉无窗口的后台子进程 / 系统服务
    （如 svchost.exe、chrome 的 GPU/网络子进程、crashpad 等），
    列表就是「正在运行的应用」。窗口标题用于区分多开实例。

    每个进程返回：{pid, name(进程名如 chrome.exe), app_name(应用名),
    title(窗口标题)}。全部走 Unicode API，中文不乱码。
    """
    windows = _windows_by_pid()
    items: list[dict] = []
    seen: set[int] = set()
    for pid in _enum_pids():
        if pid <= 0 or pid in seen:
            continue
        titles = windows.get(pid)
        if not titles:
            continue                      # 无可见窗口 = 后台子进程/服务，跳过
        path = _process_path(pid)
        if not path:
            continue
        name = os.path.basename(path)
        if not name.lower().endswith(".exe"):
            continue
        seen.add(pid)
        items.append({
            "pid": pid,
            "name": name,
            "path": path,
            "app_name": _file_description(path),
            "title": titles[0],
        })
    items.sort(key=lambda x: (x["app_name"] or x["name"]).lower())
    return items


def find_process_window(name: str) -> int:
    """按进程名找其第一个有可见窗口的主窗口句柄（「打开应用」带出已运行实例用）。

    进程名不区分大小写；填完整路径自动取文件名；不带 .exe 自动补。
    目标进程未运行、或没有可见窗口时返回 0。
    """
    raw = (name or "").strip().replace("/", os.sep)
    base = raw.rsplit(os.sep, 1)[-1]
    if not base:
        return 0
    if not base.lower().endswith(".exe"):
        base += ".exe"
    target = base.lower()

    # 先收集匹配进程名的 PID（避免对每个窗口做一次进程路径查询）
    pids: set[int] = set()
    for pid in _enum_pids():
        path = _process_path(pid)
        if path and os.path.basename(path).lower() == target:
            pids.add(pid)
    if not pids:
        return 0

    found = [0]
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if found[0]:
            return False                          # 已找到，停止枚举
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindowTextLengthW(hwnd) <= 0:
            return True                           # 无标题的透明/工具窗口跳过
        pid = ctypes.c_uint()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids:
            found[0] = int(hwnd)
            return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found[0]


def close_app(target: str) -> tuple[bool, str]:
    """关闭指定应用（按进程名匹配，结束所有同名进程）。

    target 可以填进程名（notepad.exe）或完整路径（自动取文件名）。
    用系统自带的 taskkill /IM，不需要额外依赖；/T 连它的子进程一起结束。
    """
    target = (target or "").strip()
    if not target:
        return False, "未填写要关闭的应用"
    # 填完整路径时取文件名（同时兼容 / 与 \ 两种分隔符）
    name = target.replace("/", os.sep).rsplit(os.sep, 1)[-1]
    if not name.lower().endswith(".exe"):
        name += ".exe"
    try:
        r = subprocess.run(["taskkill", "/IM", name, "/F", "/T"],
                           capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"关闭失败：{e}"
    if r.returncode == 0:
        return True, f"已关闭 {name}"
    return False, f"未找到正在运行的 {name}（或没有权限结束它）"


def wait_window(hwnd: int, timeout: float = 10.0) -> bool:
    """等待窗口句柄出现/有效。hwnd=0 时立即返回 True（不等待）。"""
    if not hwnd:
        return True
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        if window_exists(hwnd):
            return True
        time.sleep(0.1)
    return window_exists(hwnd)


def find_window_like(title: str) -> int:
    """按标题**包含**匹配窗口，返回第一个可见匹配窗口的句柄；找不到返回 0。

    用于兜底：绑定窗口时记的是当时的完整标题，应用重启后标题可能带上了
    动态部分（如记事本的「无标题 - 记事本」变成「文档1.txt - 记事本」），
    精确匹配会落空，包含匹配能兜住。
    """
    title = (title or "").strip()
    if not title:
        return 0
    found = [0]

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
        if title.lower() in buf.value.lower():
            found[0] = int(hwnd)
            return False          # 找到即停
        return True

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found[0]


def wait_window_by_title(title: str, timeout: float = 10.0) -> int:
    """按窗口标题轮询等待窗口出现，返回句柄；超时返回 0。

    先用精确匹配，落空再尝试包含匹配（见 find_window_like）。title 为空
    时立即返回 0（不等待）。
    """
    title = (title or "").strip()
    if not title:
        return 0
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        hwnd = find_window_by_title(title) or find_window_like(title)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return find_window_by_title(title) or find_window_like(title)
