"""基于 pynput 的输入模拟封装（点击 / 组合键）。"""
from __future__ import annotations

import time

from pynput.mouse import Button, Controller as MouseController

from .keymap import parse_combo, to_pynput_key

_MOUSE_BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}


def click(button: str = "left", times: int = 1, x: int | None = None, y: int | None = None) -> None:
    """在当前位置（或指定坐标）点击。times=2 为双击。"""
    btn = _MOUSE_BUTTONS.get(button, Button.left)
    mouse = _get_mouse()
    if x is not None and y is not None:
        mouse.position = (int(x), int(y))
    mouse.click(btn, times)


def press_combo(keys: str, hold_sec: float = 0.0, stop=None) -> None:
    """按 keyboard 库格式的按键串，如 'space'、'ctrl+c'。

    hold_sec > 0 表示按下后**按住**这么久再松开（「键盘连按」步骤的「每次按住」靠它实现）；
    按住期间若 stop 被置位（用户点了「停止」）会立刻松开，绝不赖着不放。
    默认 0 = 按下即松开，行为与旧版一致。
    """
    mods, main = parse_combo(keys)
    kb = _get_keyboard()
    held = []
    try:
        for m in mods:
            kb.press(to_pynput_key(m))
            held.append(m)
        kb.press(to_pynput_key(main))
        if hold_sec > 0:
            if stop is not None:
                stop.wait(hold_sec)
            else:
                time.sleep(hold_sec)
        kb.release(to_pynput_key(main))
    finally:
        for m in reversed(held):
            try:
                kb.release(to_pynput_key(m))
            except Exception:
                pass


# 控制器做成模块级单例，构造一次长期复用。
# 为什么不能每次调用都 new：连点器/流程会以几十毫秒的间隔反复触发，每次都新建
# 控制器等于反复做一遍初始化开销。键盘和鼠标的处理保持一致（原来只有键盘是单例）。
# pynput 的 Controller 内部状态只有一次性的初始化，方法调用最终落到 Win32 的
# SendInput，跨任务线程共用是安全的。
_keyboard = None
_mouse = None


def _get_keyboard():
    global _keyboard
    if _keyboard is None:
        from pynput.keyboard import Controller as KeyboardController
        _keyboard = KeyboardController()
    return _keyboard


def _get_mouse():
    global _mouse
    if _mouse is None:
        _mouse = MouseController()
    return _mouse
