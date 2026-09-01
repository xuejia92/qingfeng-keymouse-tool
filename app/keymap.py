"""按键名称映射：Qt 按键 <-> keyboard 库热键字符串 <-> pynput 按键对象。"""
from __future__ import annotations

import re

from pynput.keyboard import Key

# keyboard 库 / 显示共用的特殊键名 -> pynput
_NAME_TO_PYNPUT = {
    "space": Key.space, "enter": Key.enter, "esc": Key.esc, "tab": Key.tab,
    "backspace": Key.backspace, "delete": Key.delete, "insert": Key.insert,
    "home": Key.home, "end": Key.end, "pageup": Key.page_up, "pagedown": Key.page_down,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift, "win": Key.cmd,
    "capslock": Key.caps_lock, "menu": Key.menu, "printscreen": Key.print_screen,
    "scrolllock": Key.scroll_lock, "pause": Key.pause, "numlock": Key.num_lock,
}

MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")
_MODIFIER_RE = re.compile(r"^(ctrl|alt|shift|win|lctrl|rctrl|lalt|ralt|lshift|rshift|lwin|rwin)$")


def is_modifier_name(name: str) -> bool:
    return bool(_MODIFIER_RE.match(name))


def parse_combo(text: str) -> tuple[list[str], str]:
    """把 'ctrl+c' 拆成 (['ctrl'], 'c')；单键返回 ([], key)。"""
    parts = [p.strip().lower() for p in (text or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("按键为空")
    mods = [p for p in parts if is_modifier_name(p)]
    mains = [p for p in parts if not is_modifier_name(p)]
    if len(mains) != 1:
        raise ValueError("必须有且只有一个非修饰键，如 ctrl+c")
    return mods, mains[0]


def to_pynput_key(name: str):
    """keyboard 库风格的键名 -> pynput 按键对象。"""
    name = name.strip().lower()
    if name in _NAME_TO_PYNPUT:
        return _NAME_TO_PYNPUT[name]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", name):
        return getattr(Key, name)
    if name in ("add", "subtract", "multiply", "divide", "decimal"):
        return getattr(Key, name)
    if len(name) == 1:
        return name
    raise ValueError(f"不支持的按键: {name}")


def hotkey_display(text: str) -> str:
    """'ctrl+alt+q' -> 'Ctrl+Alt+Q'（仅用于显示）。"""
    if not text:
        return ""
    nice = {"esc": "Esc", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win",
            "space": "Space", "enter": "Enter", "tab": "Tab", "backspace": "Backspace",
            "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
            "pageup": "PageUp", "pagedown": "PageDown", "up": "↑", "down": "↓",
            "left": "←", "right": "→", "printscreen": "PrtSc", "capslock": "CapsLock"}
    parts = []
    for p in (text or "").split("+"):
        p = p.strip().lower()
        parts.append(nice.get(p, p.upper()))
    return "+".join(parts)


# ---------- Qt 按键事件 -> keyboard 库字符串 ----------

def qt_key_event_to_hotkey(event) -> str | None:
    """QKeyEvent -> keyboard 库热键字符串；纯修饰键返回 None（继续等待）。"""
    from PySide6.QtCore import Qt

    key = event.key()
    mods = []
    if event.modifiers() & Qt.ControlModifier:
        mods.append("ctrl")
    if event.modifiers() & Qt.AltModifier:
        mods.append("alt")
    if event.modifiers() & Qt.ShiftModifier:
        mods.append("shift")
    if event.modifiers() & Qt.MetaModifier:
        mods.append("win")

    name = _QT_SPECIAL.get(key)
    if name is None:
        text = event.text().strip().lower()
        if len(text) == 1 and text.isprintable():
            name = text
        elif Qt.Key_A <= key <= Qt.Key_Z:
            name = chr(key).lower()
        elif Qt.Key_0 <= key <= Qt.Key_9:
            name = chr(key)
        else:
            return None  # 无法识别（纯修饰键或未知键）
    return "+".join(mods + [name])


def _init_qt_special():
    from PySide6.QtCore import Qt
    mapping = {
        Qt.Key_Space: "space", Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
        Qt.Key_Escape: "esc", Qt.Key_Tab: "tab", Qt.Key_Backspace: "backspace",
        Qt.Key_Delete: "delete", Qt.Key_Insert: "insert", Qt.Key_Home: "home",
        Qt.Key_End: "end", Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown",
        Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
        Qt.Key_Minus: "-", Qt.Key_Equal: "=", Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]",
        Qt.Key_Semicolon: ";", Qt.Key_Apostrophe: "'", Qt.Key_Comma: ",",
        Qt.Key_Period: ".", Qt.Key_Slash: "/", Qt.Key_Backslash: "\\",
        Qt.Key_QuoteLeft: "`", Qt.Key_CapsLock: "capslock",
        Qt.Key_Print: "printscreen", Qt.Key_ScrollLock: "scrolllock", Qt.Key_Pause: "pause",
    }
    for i in range(1, 25):
        mapping[getattr(Qt, f"Key_F{i}")] = f"f{i}"
    return mapping


# Qt 键码 -> 本项目的按键名。必须在导入时构造：Qt 枚举只有在 PySide6 导入后才存在
_QT_SPECIAL = _init_qt_special()
