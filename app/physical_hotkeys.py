"""基于 pynput 低级键盘钩子的全局热键引擎：只认**物理按键**。

为什么不用 keyboard.add_hotkey（2026-09-22 教训）：
1. 它的回调拿不到 LLKHF_INJECTED 标志，无法区分「用户真按」与「本程序合成的按键」。
   游戏自动化流程会持续发送技能键（比如每 0.2s 发一个 q），用户把分组热键设成
   ctrl+q 时，流程发送的 q 落在用户还没松开的 ctrl 上 → 热键再次触发 → 流程被
   自己的技能键停掉（用户视角：按热键启动后"自己"就停了 / 再按停止没反应）。
2. Alt+字母 组合在注入路径下字母会被系统转译（实测 alt+a 变 alt+m），匹配失灵。

这里的做法：pynput.Listener 的 win32_event_filter 直接读 KBDLLHOOKSTRUCT.flags，
**injected 事件一律跳过**（不更新按下状态、不触发、也不拦截）；只对物理事件做
「修饰键集合 + 主键」的组合匹配。热键名沿用 keyboard 库风格（ctrl+alt+q、f6、space…）。

suppress 语义与旧版一致：注册时 suppress=True 的热键触发后，这次按键会被吞掉，
不传给前台程序（单键热键如 f6 需要，避免漏进游戏）。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

LLKHF_INJECTED = 0x10
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

_MODIFIERS = ("ctrl", "alt", "shift", "win")

# vkCode -> keyboard 库风格的键名。字母/数字直接小写；F 键、方向键、修饰键、
# 常用 OEM 键（美式布局字符）逐个列出；没 listed 的键不参与热键匹配。
_VK_TO_NAME: dict[int, str] = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x13: "pause",
    0x14: "capslock", 0x1B: "esc", 0x20: "space", 0x2C: "printscreen",
    0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2D: "insert", 0x2E: "delete",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "a", 0x42: "b", 0x43: "c", 0x44: "d", 0x45: "e", 0x46: "f",
    0x47: "g", 0x48: "h", 0x49: "i", 0x4A: "j", 0x4B: "k", 0x4C: "l",
    0x4D: "m", 0x4E: "n", 0x4F: "o", 0x50: "p", 0x51: "q", 0x52: "r",
    0x53: "s", 0x54: "t", 0x55: "u", 0x56: "v", 0x57: "w", 0x58: "x",
    0x59: "y", 0x5A: "z",
    0x5B: "win", 0x5C: "win",
    0x60: "0", 0x61: "1", 0x62: "2", 0x63: "3", 0x64: "4",
    0x65: "5", 0x66: "6", 0x67: "7", 0x68: "8", 0x69: "9",
    0x6A: "*", 0x6B: "+", 0x6D: "-", 0x6E: ".", 0x6F: "/",
    0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4", 0x74: "f5", 0x75: "f6",
    0x76: "f7", 0x77: "f8", 0x78: "f9", 0x79: "f10", 0x7A: "f11", 0x7B: "f12",
    0x7C: "f13", 0x7D: "f14", 0x7E: "f15", 0x7F: "f16", 0x80: "f17",
    0x81: "f18", 0x82: "f19", 0x83: "f20", 0x84: "f21", 0x85: "f22",
    0x86: "f23", 0x87: "f24",
    0x90: "capslock", 0x91: "scrolllock",
    0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl",
    0xA4: "alt", 0xA5: "alt",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


class PhysicalHotkeyEngine(QObject):
    """全局热键引擎（进程内单例）。只匹配物理按键，程序合成的按键一律忽略。"""

    fired = Signal(str)      # 匹配到的热键（keyboard 库小写格式）

    def __init__(self):
        super().__init__()
        self._handlers: dict[str, list[tuple[int, object, bool]]] = {}
        self._pressed: set[str] = set()
        self._listener = None
        self._paused = False        # 拖拽期间临时卸钩子（见 pause）
        self._lock = threading.Lock()
        self._next_id = 0

    # ---------- 注册 / 注销 ----------
    def register(self, hotkey: str, callback, suppress: bool = False) -> bool:
        """注册热键（keyboard 库小写格式）；无效键名返回 False。"""
        hk = (hotkey or "").strip().lower()
        if not hk:
            return False
        for part in hk.split("+"):
            if part not in _VK_TO_NAME.values():
                log.warning("热键含无法识别的键名: %s（%r）", hk, part)
                return False
        with self._lock:
            self._next_id += 1
            self._handlers.setdefault(hk, []).append(
                (self._next_id, callback, bool(suppress)))
            # 暂停期间注册热键**不要**把钩子装回来（拖拽还没结束）
            need_start = self._listener is None and not self._paused
        if need_start:
            self._start()
        return True

    def unregister(self, hotkey: str) -> None:
        hk = (hotkey or "").strip().lower()
        with self._lock:
            self._handlers.pop(hk, None)
            empty = not self._handlers
        if empty:
            self._stop()

    def unregister_all(self) -> None:
        with self._lock:
            self._handlers.clear()
        self._stop()

    # ---------- 监听 ----------
    def _start(self) -> None:
        if self._listener is not None:
            return
        try:
            from pynput.keyboard import Listener

            self._listener = Listener(
                win32_event_filter=self._win32_event_filter)
            self._listener.start()
            log.info("物理热键引擎已启动（只认物理按键）")
        except Exception:
            log.exception("物理热键引擎启动失败")
            self._listener = None

    def _stop(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception:
            log.exception("物理热键引擎停止失败")
        self._listener = None

    # ---------- 拖拽期间临时让位（与 mouse_menu.pause 对称） ----------
    def pause(self) -> None:
        """拖拽期间**真的卸掉**键盘钩子（配对 unpause）。

        为什么必须有：低级键盘钩子的回调也是 Python 回调，必须抢到 GIL 才能返回，
        而 Windows 会**同步等**它返回才继续投递键盘输入。本程序自己的拖拽
        （OLE 的 DoDragDrop）会让**主线程连握 GIL 待在原生模态循环里**，
        钩子线程拿不到 GIL；`LowLevelHooksTimeout` 到点后 Windows 会
        **静默摘掉**钩子——不抛异常、不回调通知，pynput 也不会告诉你，
        `_start()` 之后也不会被重新调用（`_listener` 还非 None）。
        结果就是**所有全局热键永久失效**，直到重启程序。
        （鼠标钩子那边早就做了同样的让位，见 mouse_menu.pause。）

        这里必须**真的停 listener**：只是「忽略事件」没用——超时被摘掉的是
        系统钩子本身。注意与 `suspend()`（无热键时提前返回）不同，这里
        无论有没有热键都要把状态记上，unpause 时再决定装不装。
        """
        with self._lock:
            if self._paused:
                return
            self._paused = True
        self._stop()

    def unpause(self) -> None:
        """与 pause 配对：装回钩子（期间热键已全部注销则不再装）。"""
        with self._lock:
            if not self._paused:
                return
            self._paused = False
            if not self._handlers:
                return
        self._start()

    # ---------- 事件处理（都在 pynput 监听线程） ----------
    def _win32_event_filter(self, msg, data):
        """低级钩子：injected 事件直接放行忽略；物理事件维护状态并匹配热键。

        返回 False 会把这次按键吞掉（用于 suppress 热键）。
        """
        try:
            if data.flags & LLKHF_INJECTED:
                return True                     # 程序合成的按键：与热键无关
            name = _VK_TO_NAME.get(data.vkCode)
            if name is None:
                return True
            is_down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if is_down:
                # ⚠️ 已经在按下状态又收到 KEYDOWN = 系统的**自动重复**（按住不放）。
                # 不过滤的话按住热键会每秒触发十几次：中键菜单会被"关掉又立刻重开"，
                # 看起来就是**怎么都关不掉**（2026-10-04 用户反馈）。
                # 真正的连按是 down/up/down，中间有 up 把它从 _pressed 里摘掉，不受影响。
                repeat = name in self._pressed
                self._pressed.add(name)
            else:
                repeat = False
                self._pressed.discard(name)
            if not is_down or name in _MODIFIERS or repeat:
                return True                     # 松开 / 修饰键 / 自动重复都不触发

            candidate = self._candidate(name)
            with self._lock:
                reg = list(self._handlers.get(candidate) or [])
            if not reg:
                return True
            suppress_any = any(s for _, _cb, s in reg)
            log.info("物理热键触发: %s", candidate)
            for _hid, cb, _s in reg:
                try:
                    cb()
                except Exception:
                    log.exception("热键回调异常: %s", candidate)
            # emit 信号只是给 HotkeyManager 做日志/联动，真正分发走上面的 callback
            self.fired.emit(candidate)
            return not suppress_any             # suppress=True → 吞掉这次按键
        except Exception:
            log.exception("热键引擎处理事件异常")
            return True

    def _candidate(self, main: str) -> str:
        """当前按下的修饰键 + 主键 → 录入时的键序（ctrl,alt,shift,win,主键）。"""
        mods = [m for m in _MODIFIERS if m in self._pressed]
        return "+".join(mods + [main])


# 进程内单例：低级键盘钩子全局只能装一份
engine = PhysicalHotkeyEngine()


def pause() -> None:
    """拖拽期间临时卸掉全局键盘钩子（配对 unpause）。

    流程编排区拖动步骤/拖入模块时调用（见 ui/flow_dialog.StepList.startDrag）：
    那段时间主线程握着 GIL 待在 OLE 原生模态循环里，钩子线程拿不到 GIL，
    Windows 超时后会**静默摘掉**钩子且不通知 → 全局热键永久失效。
    鼠标钩子的对应让位见 mouse_menu.pause()。
    """
    engine.pause()


def unpause() -> None:
    """与 pause 配对：装回键盘钩子。"""
    engine.unpause()
