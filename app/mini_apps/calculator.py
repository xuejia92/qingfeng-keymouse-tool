# -*- coding: utf-8 -*-
"""内置小程序：计算器（同时充当「小工具」功能的完整演示）。

为什么拿计算器当示例：它**自带全部状态、不依赖主程序**（不读配置、不碰流程、
不装热键），正好用来演示「点开卡片 -> 在独立窗口里跑起来」这条完整链路；
又足够简单，一眼就能看出窗口是不是真的独立（能单独最小化、能被别的窗口盖住、
关掉它主程序毫发无损）。

键盘也能用：数字 / `+ - * /` / 回车(=) / 退格 / Esc(清空) / `%`。
"""
from __future__ import annotations

import operator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGridLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..ui import theme
from . import MiniApp, register

# 单键尺寸与间距（窗口尺寸由布局自己算出来，不写死窗口大小）
KEY_W, KEY_H = 66, 50
KEY_GAP = 6
DISPLAY_H = 54
PAGE_MARGIN = 12

# 运算符一律用**显示字符**做键（− 是 U+2212 减号，不是 ASCII 连字符）：
# 内部键与按钮文本、键盘映射三处必须是同一个字符，否则会出现「按了减号但
# 运算符没换」这类静默错误（2026-10-03 开发时踩过）。
_BINARY_OPS = {
    "+": operator.add,
    "−": operator.sub,
    "×": operator.mul,
    "÷": operator.truediv,
}

# (显示文本, 按键类别, 工具提示)；类别只影响配色，不影响行为
_KEYS: list[tuple[str, str, str]] = [
    ("C", "func", "清空"),
    ("←", "func", "退格"),
    ("%", "func", "百分号"),
    ("÷", "op", "除"),
    ("7", "digit", ""), ("8", "digit", ""), ("9", "digit", ""),
    ("×", "op", "乘"),
    ("4", "digit", ""), ("5", "digit", ""), ("6", "digit", ""),
    ("−", "op", "减"),
    ("1", "digit", ""), ("2", "digit", ""), ("3", "digit", ""),
    ("+", "op", "加"),
    ("±", "func", "正负号"), ("0", "digit", ""), (".", "digit", ""),
    ("=", "eq", "等于"),
]
_COLUMNS = 4


def _fmt(value: float) -> str:
    """把计算结果格式化成人类看得懂的一行文本。

    - 整数结果不带小数点（12 而不是 12.0）；
    - 其余用 `%.10g`，避免 0.1+0.2 这类二进制浮点尾巴糊满一整屏。
    """
    if value != value or value in (float("inf"), float("-inf")):
        return ""
    if abs(value) < 1e15 and abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.10g}"


def _key_qss(kind: str) -> str:
    """按键配色（控件级内联样式，颜色全部走主题令牌 -> 切主题自动跟随）。

    ⚠️ 主色底上的文字必须写 `white` **关键字**而不是 `#ffffff`：主题引擎的
    颜色映射会把十六进制白色当成 card_bg 之类的令牌，深色主题下白字会变成
    深字压深底（见 theme.map_colors 的说明）。
    """
    t = theme.token
    if kind == "eq":
        return (f"QPushButton{{background:{t('primary')};color:white;"
                f"border:1px solid {t('primary_border')};}}"
                f"QPushButton:hover{{background:{t('primary_hover')};color:white;}}"
                f"QPushButton:pressed{{background:{t('primary_pressed')};color:white;}}")
    if kind == "op":
        return (f"QPushButton{{background:{t('primary_soft')};color:{t('primary')};"
                f"border:1px solid {t('border_light')};}}"
                f"QPushButton:hover{{border-color:{t('primary')};}}"
                f"QPushButton:pressed{{background:{t('pressed_bg')};}}")
    if kind == "func":
        return (f"QPushButton{{background:{t('hover_bg')};color:{t('text_dim')};"
                f"border:1px solid {t('border_light')};}}"
                f"QPushButton:hover{{border-color:{t('primary')};color:{t('primary')};}}"
                f"QPushButton:pressed{{background:{t('pressed_bg')};}}")
    return (f"QPushButton{{background:{t('card_bg')};color:{t('text')};"
            f"border:1px solid {t('input_border')};}}"
            f"QPushButton:hover{{background:{t('primary_soft')};"
            f"border-color:{t('primary')};color:{t('primary')};}}"
            f"QPushButton:pressed{{background:{t('pressed_bg')};}}")


class CalculatorWidget(QWidget):
    """一个能用的计算器（单击 + 键盘）。

    运算语义是「即时执行」型：`1 + 2 + 3` 每按一次运算符就把前面算掉，
    与 Windows 自带计算器的标准模式一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._acc: float | None = None      # 左操作数（已累积）
        self._op: str | None = None         # 待执行的运算符
        self._entry = "0"                   # 当前正在输入 / 显示的数
        self._fresh = True                  # True = 下一个数字另起一个新数
        self._error = False                 # 出错态（除零），显示「错误」
        self._expr = ""                     # 上方小字：算式过程
        self.setFocusPolicy(Qt.StrongFocus)
        self._build_ui()
        self._refresh()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root.setSpacing(6)

        self._expr_label = QLabel("")
        self._expr_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._expr_label.setFixedHeight(18)
        # 小字算式行用次要文字色；用行内样式保持与主题一致
        self._expr_label.setStyleSheet(
            f"color:{theme.token('text_muted')};font-size:9pt;")
        root.addWidget(self._expr_label)

        self._display = QLineEdit("0")
        self._display.setReadOnly(True)
        self._display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._display.setFixedHeight(DISPLAY_H)
        self._display.setFocusPolicy(Qt.NoFocus)   # 键盘事件统一由本控件处理
        self._display.setStyleSheet(
            f"QLineEdit{{background:{theme.token('input_bg')};"
            f"color:{theme.token('text')};"
            f"border:1px solid {theme.token('input_border')};"
            f"border-radius:8px;padding:2px 12px;font-size:20pt;}}")
        root.addWidget(self._display)

        grid = QGridLayout()
        grid.setSpacing(KEY_GAP)
        for idx, (text, kind, tip) in enumerate(_KEYS):
            btn = QPushButton(text)
            btn.setFixedSize(KEY_W, KEY_H)
            btn.setFocusPolicy(Qt.NoFocus)      # 别把键盘焦点从本控件抢走
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{border-radius:8px;font-size:12pt;"
                f"padding:0px;}}" + _key_qss(kind))
            if tip:
                btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, k=text: self.press(k))
            grid.addWidget(btn, idx // _COLUMNS, idx % _COLUMNS)
        root.addLayout(grid)

    # ---------- 状态刷新 ----------
    def _refresh(self) -> None:
        self._display.setText("错误" if self._error else self._entry)
        self._expr_label.setText(self._expr)

    @property
    def display_text(self) -> str:
        """当前显示的文本（测试与调试用）。"""
        return self._display.text()

    def _reset(self) -> None:
        self._acc = None
        self._op = None
        self._entry = "0"
        self._fresh = True
        self._error = False
        self._expr = ""

    def _clear_error(self) -> None:
        if self._error:
            self._reset()

    # ---------- 运算 ----------
    def _apply(self, left: float, right: float, op: str) -> float | None:
        """执行一次二元运算；除零返回 None 并置错误态。"""
        try:
            result = _BINARY_OPS[op](left, right)
        except ZeroDivisionError:
            return None
        except (KeyError, OverflowError):
            return None
        if result != result or result in (float("inf"), float("-inf")):
            return None
        return result

    def _fail(self) -> None:
        self._error = True
        self._acc = None
        self._op = None
        self._entry = "0"
        self._fresh = True
        self._expr = "不能除以 0"

    # ---------- 对外唯一入口 ----------
    def press(self, key: str) -> None:
        """按下一个键（界面按钮与键盘都走这里，保证行为完全一致）。"""
        if key.isdigit():
            self._input_digit(key)
        elif key == ".":
            self._input_dot()
        elif key in _BINARY_OPS:
            self._input_operator(key)
        elif key == "=":
            self._equals()
        elif key == "C":
            self._reset()
        elif key == "←":
            self._backspace()
        elif key == "±":
            self._negate()
        elif key == "%":
            self._percent()
        else:
            return
        self._refresh()

    def _input_digit(self, digit: str) -> None:
        self._clear_error()
        if self._fresh:
            self._entry = digit
            self._fresh = False
        elif self._entry == "0":
            self._entry = digit
        else:
            self._entry += digit

    def _input_dot(self) -> None:
        self._clear_error()
        if self._fresh:
            self._entry = "0."
            self._fresh = False
        elif "." not in self._entry:
            self._entry += "."

    def _input_operator(self, op: str) -> None:
        self._clear_error()
        try:
            current = float(self._entry)
        except ValueError:
            return
        if self._acc is None:
            self._acc = current
        elif not self._fresh:
            merged = self._apply(self._acc, current, self._op)
            if merged is None:
                self._fail()
                return
            self._acc = merged
        # _fresh 为真 = 用户连按了两个运算符：只换运算符，不重复计算
        self._op = op
        self._fresh = True
        self._entry = _fmt(self._acc)
        self._expr = f"{_fmt(self._acc)} {op}"

    def _equals(self) -> None:
        self._clear_error()
        if self._op is None:
            self._fresh = True
            return
        try:
            current = float(self._entry)
        except ValueError:
            return
        assert self._acc is not None
        merged = self._apply(self._acc, current, self._op)
        if merged is None:
            self._fail()
            return
        self._expr = f"{_fmt(self._acc)} {self._op} {_fmt(current)} ="
        self._entry = _fmt(merged)
        self._acc = None
        self._op = None
        self._fresh = True

    def _backspace(self) -> None:
        self._clear_error()
        if self._fresh:
            self._entry = "0"
            return
        trimmed = self._entry[:-1]
        self._entry = trimmed if trimmed not in ("", "-") else "0"

    def _negate(self) -> None:
        if self._error:
            return
        if self._entry in ("0", "0."):
            return
        self._entry = self._entry[1:] if self._entry.startswith("-") \
            else "-" + self._entry

    def _percent(self) -> None:
        if self._error:
            return
        try:
            value = float(self._entry)
        except ValueError:
            return
        self._entry = _fmt(value / 100.0)
        self._fresh = True

    # ---------- 键盘 ----------
    def keyPressEvent(self, event) -> None:     # noqa: N802（Qt 命名）
        key = event.key()
        text = event.text()
        if text.isdigit():
            self.press(text)
        elif text in (".", "%"):
            # ⚠️ 必须用元组判断，不能写 `text in ".%"`：空文本（Esc/退格等特殊键
            # 的 text() 为空）是任何字符串的子串，会把那些键全部误吞（踩过）。
            self.press(text)
        elif text == "+":
            self.press("+")
        elif text == "-":
            self.press("−")                 # ASCII 连字符映射到显示用的减号
        elif text == "*":
            self.press("×")
        elif text == "/":
            self.press("÷")
        elif key in (Qt.Key_Return, Qt.Key_Enter) or text == "=":
            self.press("=")
        elif key == Qt.Key_Backspace:
            self.press("←")
        elif key == Qt.Key_Escape:
            self.press("C")
        else:
            super().keyPressEvent(event)        # 其余交给默认处理
            return
        event.accept()

    def showEvent(self, event) -> None:         # noqa: N802（Qt 命名）
        super().showEvent(event)
        self.setFocus()                         # 打开即可直接敲键盘


# 登记到「小工具」页（key 一旦发布不要再改）
register(MiniApp(
    key="calculator",
    name="计算器",
    icon="🧮",
    desc="四则运算 / 百分号 / 正负号，支持键盘输入",
    factory=CalculatorWidget,
))
