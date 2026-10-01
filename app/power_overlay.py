"""定时关机：屏幕下方的大号红色倒计时浮层。

为什么单独一个模块：`run_shutdown_step` 跑在流程的后台线程里，而 QWidget 只能在
主线程创建 / 更新，所以和「图片悬浮」「消息通知」一样，经 `screenshot_actor.ui_call`
把窗口操作调度到主线程（调用方见 `tasks._warn_before_power`）。

浮层的几条硬约束：

- **鼠标穿透**（`Qt.WindowTransparentForInput` + `WA_TransparentForMouseEvents`）：
  它只是个闹钟，绝不能挡住用户在本步倒计时期间的操作，也不能抢焦点
  （`WA_ShowWithoutActivating`）——否则用户正在打的字会跑到浮层上去。
- **不参与「最后一个窗口关闭就退出」**（`WA_QuitOnClose=False`）：收起浮层时若被
  Qt 当成「最后一个窗口关了」，主线程的事件循环会直接退出，而后台步骤线程还卡在
  `ui_call` 里等回复 —— 僵住。见下面 `PowerCountdown.__init__` 里的注释。
- **置顶无边框**：无边框、Tool 窗口（不进任务栏）、始终在最前。
- **位置固定在主屏下方居中**，压在任务栏之上；字号变化后重新居中。
- **单例**：反复「就地更新」同一个窗口，而不是每帧弹一个新窗；
  `hide_countdown()` 把它收起。
- 只在**「提醒倒计时」（warn_sec）**期间显示。长倒计时 / 条件等待阶段不显示，
  免得几十分钟一直糊在屏幕上挡着用户干活。

颜色刻意写死成红色（用户要求「字大一点，红色」），字号可配。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QVBoxLayout,
                               QWidget)

from .config import format_duration

# 主文字号范围与默认值（pt）。默认值要「一眼就看见」，但别大到把屏幕糊满。
DEFAULT_FONT_SIZE = 48
MIN_FONT_SIZE = 12
MAX_FONT_SIZE = 200

# 浮层距屏幕底边的留白（像素）——避让任务栏与输入法候选框
BOTTOM_MARGIN = 72

# 主文字（红）与副文字（暗红）颜色
MAIN_COLOR = "#d81e06"
SUB_COLOR = "#b33226"

# 副行固定文案：告诉用户「想反悔按哪里」——按 Esc 最快，所以写在前头
NOTE = "按 Esc 键取消（或点流程「停止」）"
# Esc 监听没挂上时的退路文案：宁可少提示一句，也别提示一句做不到的
NOTE_NO_ESC = "点流程「停止」可取消"


def clamp_font_size(size) -> int:
    """字号收进 [MIN_FONT_SIZE, MAX_FONT_SIZE]；非法值退回默认字号。"""
    try:
        value = int(round(float(size)))
    except (TypeError, ValueError):
        value = DEFAULT_FONT_SIZE
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, value))


def format_remain(remain_sec) -> str:
    """剩余秒数 -> 倒计时文案里的时长（**向上取整**）。

    取整方向很关键：还剩 0.4 秒时若按四舍五入会显示「0 秒后关机」，
    看着像已经到点了；向上取整则显示「1 秒」，与直觉一致。
    """
    try:
        secs = int(math.ceil(float(remain_sec)))
    except (TypeError, ValueError):
        secs = 0
    return format_duration(max(0, secs))


def countdown_text(remain_sec, action_label: str = "") -> str:
    """主行文案，如「15 秒后关机」「1 分 30 秒后重启」。"""
    return f"{format_remain(remain_sec)}后{action_label or '关机'}"


class PowerCountdown(QWidget):
    """屏幕下方居中、无边框置顶、鼠标穿透的大号倒计时浮层。"""

    def __init__(self, font_size: int = DEFAULT_FONT_SIZE):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool
                         | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)      # 展示时不抢焦点
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # 双保险：连子控件一起穿透
        # 关键：倒计时结束收起浮层时，别让 Qt 把它当成「最后一个窗口关了」而退出
        # 事件循环。真跑起来时主窗口一般在，但脚本化调用（没有主窗口）会踩中：
        # 主线程一退出事件循环，后台线程还在 ui_call 里等回复 —— 直接僵住。
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("powerCountdown")
        self.setWindowTitle("关机倒计时")

        self._card = QFrame(self)
        self._card.setObjectName("powerCountdownCard")
        # 颜色走样式表（QFont 没有颜色），字号走 QFont（样式表不写 font-size，
        # 免得两边打架时以样式表为准，程序里改的字号反而不生效）。
        self._card.setStyleSheet(
            f"QFrame#powerCountdownCard {{ background: rgba(255, 255, 255, 0.94);"
            f" border: 3px solid {MAIN_COLOR}; border-radius: 16px; }}"
            f"QLabel#powerCountdownMain {{ color: {MAIN_COLOR}; }}"
            f"QLabel#powerCountdownSub {{ color: {SUB_COLOR}; }}")

        self.main_label = QLabel(self._card)
        self.main_label.setObjectName("powerCountdownMain")
        self.main_label.setAlignment(Qt.AlignCenter)
        self.sub_label = QLabel(self._card)
        self.sub_label.setObjectName("powerCountdownSub")
        self.sub_label.setAlignment(Qt.AlignCenter)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(38, 18, 38, 18)
        card_lay.setSpacing(4)
        card_lay.addWidget(self.main_label, 0, Qt.AlignHCenter)
        card_lay.addWidget(self.sub_label, 0, Qt.AlignHCenter)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._card)

        self._font_size = clamp_font_size(font_size)
        self._apply_font()
        self.set_countdown(0, "关机", NOTE)

    # ---------- 内容 ----------
    @property
    def font_size(self) -> int:
        """当前主文字号（pt）。"""
        return self._font_size

    def _apply_font(self) -> None:
        """把字号铺到两个标签上：主行加粗，副行约为主行的 40%。"""
        main = QFont(self.font())
        main.setPointSize(self._font_size)
        main.setBold(True)
        self.main_label.setFont(main)

        sub = QFont(self.font())
        sub.setPointSize(max(MIN_FONT_SIZE, int(round(self._font_size * 0.4))))
        self.sub_label.setFont(sub)

    def set_font_size(self, size) -> None:
        """改字号（会重新排版并重新居中）。"""
        size = clamp_font_size(size)
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_font()
        self.adjustSize()
        self.place()

    def set_countdown(self, remain_sec, action_label: str = "关机",
                      note: str = NOTE) -> None:
        """刷新倒计时文案（剩余秒数 + 即将执行的动作），并保持底部居中。"""
        self.main_label.setText(countdown_text(remain_sec, action_label))
        self.sub_label.setText(note or "")
        self.sub_label.setVisible(bool(note))
        self.adjustSize()
        self.place()

    # ---------- 定位 ----------
    def place(self) -> None:
        """贴主屏**下方居中**（按可用区域对齐，自动避开任务栏）。

        浮层宽到超过屏幕时不再居中而是贴左边缘：倒计时的数字排在最前面，
        居中会把数字截在屏幕外，只剩「秒后关机」这种没用的半截。
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.left() + (geo.width() - self.width()) // 2
        y = geo.bottom() - self.height() - BOTTOM_MARGIN + 1
        self.move(max(geo.left(), x), max(geo.top(), y))


# ---------------------------------------------------------------------------
# 对外接口（任意线程可调用，内部调度到主线程）
# ---------------------------------------------------------------------------

_current: "PowerCountdown | None" = None


def current() -> "PowerCountdown | None":
    """当前浮层对象（未创建过则 None）。测试与状态栏用。"""
    return _current


def _alive(win) -> bool:
    """窗口的 C++ 对象还在不在（程序退出后被销毁则返回 False）。"""
    try:
        win.isVisible()
        return True
    except RuntimeError:
        return False


def show_countdown(remain_sec, action_label: str = "关机",
                   font_size: int = DEFAULT_FONT_SIZE,
                   note: str = NOTE) -> "PowerCountdown | None":
    """显示（或就地更新）屏幕下方的红色倒计时浮层，返回浮层对象。

    同一个浮层会被复用；`remain_sec` 向上取整后显示。
    浮层只是提醒，创建失败（没有 QApplication / 平台不支持）返回 None，
    由调用方决定是否忽略——**不应因此打断流程**。
    """
    def _do() -> "PowerCountdown":
        # 没有 QApplication 时**绝不能**去构造 QWidget：那不是抛异常，是直接
        # abort 掉整个进程（同 QPixmap 的老坑）。这里主动抛，由外层吞掉返回 None。
        if QApplication.instance() is None:
            raise RuntimeError("没有 Qt 应用实例，无法显示倒计时浮层")
        global _current
        win = _current
        if win is None or not _alive(win):
            win = PowerCountdown(font_size)
            _current = win
        win.set_font_size(font_size)
        win.set_countdown(remain_sec, action_label, note)
        if not win.isVisible():
            win.show()
        win.raise_()
        return win

    try:
        from .screenshot_actor import ui_call
        return ui_call(_do)
    except Exception:
        return None


def hide_countdown() -> None:
    """收起倒计时浮层（没显示过 / 已销毁都静默返回）。"""
    def _do() -> None:
        win = _current
        if win is None:
            return
        try:
            win.hide()
        except RuntimeError:
            pass

    try:
        from .screenshot_actor import ui_call
        ui_call(_do)
    except Exception:
        pass


def active_count() -> int:
    """当前可见的浮层数量（0 或 1）——单例窗口，测试用。"""
    win = _current
    return 1 if (win is not None and _alive(win) and win.isVisible()) else 0


def close_all() -> None:
    """销毁浮层（程序退出时调用）。"""
    global _current
    win, _current = _current, None
    if win is None:
        return
    try:
        win.close()
    except RuntimeError:
        pass
