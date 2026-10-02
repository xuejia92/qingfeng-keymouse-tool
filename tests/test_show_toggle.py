# -*- coding: utf-8 -*-
"""全局热键「显示/隐藏主窗口」的置顶链契约（2026-10-02）。

**用户反馈**：「如果客户端 app 在没有置顶桌面显示（被其它窗口遮挡），这时候按下
快捷键应该是置顶显示」。

两条规则钉死：

1. `toggle_show_hide` 的三分支：隐藏/最小化 -> 显示并置顶；可见且在前台 -> 隐藏；
   **可见但被遮挡（非前台）-> 同样显示并置顶**（不能判成「隐藏」）。
2. `_force_foreground` 的 TOPMOST **不能定时取消**：`SetForegroundWindow` 受
   Windows 前台锁定限制，被拒绝时窗口必须在 TOPMOST 下保持可见（点一下即可激活），
   由失焦事件解除——否则窗口闪 300ms 就掉回被遮挡处，等于没置顶。

测试用 `MainWindow.__new__` 轻量替身（widget 方法全部 mock），不构造完整窗口；
offscreen 平台下 Win32 调用一律走 try/except 兜底，不会真置顶任何东西。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui import main_window as mw_mod
from app.ui.main_window import MainWindow


def _src() -> str:
    return Path(mw_mod.__file__).read_text(encoding="utf-8")


def _body(name: str) -> str:
    """截出 MainWindow 某方法的函数体（到下一个同级 def 为止）。"""
    text = _src()
    return text.split(f"def {name}")[1].split("\n    def ")[0]


class ToggleCase(unittest.TestCase):
    def _win(self) -> MainWindow:
        """不跑 __init__ 的 MainWindow：widget 方法全部换成 mock。"""
        win = MainWindow.__new__(MainWindow)
        win.cfg = mock.Mock(version="1.0.0")
        win.isVisible = mock.Mock(return_value=False)
        win.isMinimized = mock.Mock(return_value=False)
        win.isActiveWindow = mock.Mock(return_value=False)
        win.show_window = mock.Mock()
        win.hide_window = mock.Mock()
        return win


class TestToggleBranches(ToggleCase):
    def test_hidden_window_shows_and_pins(self):
        win = self._win()
        win.toggle_show_hide()
        win.show_window.assert_called_once()
        win.hide_window.assert_not_called()

    def test_minimized_window_shows_and_pins(self):
        win = self._win()
        win.isVisible.return_value = True
        win.isMinimized.return_value = True
        win.toggle_show_hide()
        win.show_window.assert_called_once()

    def test_front_window_hides(self):
        """可见且在前台：再按一次 = 隐藏。"""
        win = self._win()
        win.isVisible.return_value = True
        win.isActiveWindow.return_value = True
        win.toggle_show_hide()
        win.hide_window.assert_called_once()
        win.show_window.assert_not_called()

    def test_visible_but_covered_pins_to_front(self):
        """★ 用户报的场景：可见但被遮挡（非前台）-> 必须置顶显示，不许判成隐藏。"""
        win = self._win()
        win.isVisible.return_value = True
        win.isMinimized.return_value = False
        win.isActiveWindow.return_value = False     # 被别的窗口盖住了
        win.toggle_show_hide()
        win.show_window.assert_called_once()
        win.hide_window.assert_not_called()


class TestPinLifecycle(ToggleCase):
    def test_show_window_calls_force_foreground(self):
        win = MainWindow.__new__(MainWindow)
        win.isMinimized = mock.Mock(return_value=False)
        win.show = mock.Mock()
        win.raise_ = mock.Mock()
        win.activateWindow = mock.Mock()
        win._force_foreground = mock.Mock()
        win.show_window()
        win._force_foreground.assert_called_once()

    def test_hide_window_clears_topmost_flag(self):
        win = MainWindow.__new__(MainWindow)
        win._pinned_topmost = True
        win.isVisible = mock.Mock(return_value=False)   # 隐藏中：clear 早退，不碰 winId
        win.hide = mock.Mock()
        win.hide_window()
        self.assertFalse(win._pinned_topmost, "隐藏后还挂着置顶标志")
        win.hide.assert_called_once()

    def test_clear_topmost_ignores_hidden_window(self):
        """窗口不可见时 _clear_topmost 不得碰 winId()（会强制创建原生窗口）。"""
        win = MainWindow.__new__(MainWindow)
        win._pinned_topmost = True
        win.isVisible = mock.Mock(return_value=False)
        win.winId = mock.Mock(side_effect=AssertionError("不可见时不该碰 winId"))
        win._clear_topmost()
        self.assertFalse(win._pinned_topmost)

    def test_activation_lost_clears_topmost(self):
        """置顶后失焦 -> 解除；置顶后激活/未置顶 -> 不动。"""
        win = MainWindow.__new__(MainWindow)
        win._pinned_topmost = True
        win._clear_topmost = mock.Mock()
        win._on_activation_changed(False)
        win._clear_topmost.assert_called_once()

        win._clear_topmost.reset_mock()
        win._pinned_topmost = True
        win._on_activation_changed(True)            # 还在前台：保持置顶
        win._clear_topmost.assert_not_called()

        win._pinned_topmost = False                 # 根本没置顶过：不动作
        win._on_activation_changed(False)
        win._clear_topmost.assert_not_called()


class TestSourceContract(unittest.TestCase):
    """源码级兜底：TOPMOST 的生命周期不再依赖定时器。"""

    def test_force_foreground_has_no_scheduled_unpin(self):
        """`SetForegroundWindow` 可能被系统拒绝：TOPMOST 定时取消会让窗口
        掉回被遮挡处（用户看到「闪一下又没了」）。这条曾经就是 bug。"""
        body = _body("_force_foreground")
        self.assertNotIn("singleShot", body,
                         "TOPMOST 不能用定时器取消——被拒绝时窗口会掉回去")
        self.assertIn("_pinned_topmost = True", body)

    def test_unpin_wired_everywhere(self):
        """解除置顶必须接在：隐藏、失焦两处。"""
        self.assertIn("_clear_topmost", _body("hide_window"))
        self.assertIn("_clear_topmost", _body("_on_activation_changed"))

    def test_win32_calls_use_void_p(self):
        """★ 本轮真根因：SetWindowPos 必须用 c_void_p 传句柄。

        裸 int 会把 HWND_TOPMOST(-1) 截断成 32 位 0xFFFFFFFF（不是 64 位的
        0xFFFFFFFFFFFFFFFF），SetWindowPos 拿到错误句柄导致 TOPMOST 从未生效。
        """
        body = _body("_force_foreground")
        self.assertIn("SetWindowPos.argtypes", body,
                      "Win32 调用必须设置 argtypes，否则句柄被当 32 位截断")
        self.assertIn("c_void_p", body)
        self.assertNotIn("HWND_TOPMOST = -1", body,
                         "别再退回裸 int 传 -1 的写法")

    def test_toggle_keeps_pin_branch(self):
        """「可见但非前台 -> 置顶」这个分支不许被删。"""
        body = _body("toggle_show_hide")
        self.assertIn("isActiveWindow", body)
        self.assertIn("show_window", body)


if __name__ == "__main__":
    unittest.main()
