# -*- coding: utf-8 -*-
"""「🧰 小工具」功能的测试。

覆盖：
- **注册表**（app/mini_apps）：内置计算器已登记、key 唯一、find_app、同 key 覆盖；
- **九宫格页**（ToolsTab）：卡片数 = 小程序数、卡片文案/提示/图标、点击 -> 打开窗口、
  reload 后能看到新登记的小程序；
- **独立窗口**（mini_window）：无 Qt 父对象（真正的顶层窗口）、同 key 只开一个、
  关闭后可从登记表摘除并可重开、close_all 收尾；
- **计算器**（示例小程序）：四则运算 / 除零 / 百分号 / 正负号 / 退格 / 键盘映射；
- **接线契约**（源码级）：主窗口里「小工具」标签在「设置」左边、shutdown 会关掉
  遗留的小程序窗口。

注册表是模块级全局状态，涉及增删的用例一律用 mock.patch 换成局部列表，
避免污染同一进程里其它测试文件。
"""
from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel

from app import mini_apps
from app.mini_apps import MiniApp
from app.ui import mini_window, tools_tab as tools_tab_mod
from app.ui import theme
from app.ui.main_window import MainWindow
from app.ui.mini_window import MiniAppWindow, close_all, open_mini_app, open_apps
from app.ui.tools_tab import ToolsTab


def _fake_app(key: str = "fake", name: str = "假小程序") -> MiniApp:
    return MiniApp(key=key, name=name, icon="🧪", desc="测试用",
                   factory=QLabel)


class _QtCase(unittest.TestCase):
    """需要 QApplication 的用例的公共基类（离屏）。

    ⚠️ 碰 Qt 图形对象（QPixmap/QToolButton）之前必须先有 QApplication，
    否则不是抛异常而是进程直接 abort。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self):
        close_all()                     # 别把窗口留给下一个用例
        theme.clear_registry()          # 少留一堆样式登记，免得拖慢别的用例


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class TestMiniAppRegistry(_QtCase):

    def test_builtin_calculator_registered(self):
        app = mini_apps.find_app("calculator")
        self.assertIsNotNone(app, "内置计算器应当已登记到注册表")
        self.assertEqual(app.name, "计算器")
        self.assertTrue(app.icon)
        self.assertTrue(app.desc)

    def test_keys_are_unique(self):
        keys = [a.key for a in mini_apps.all_apps()]
        self.assertEqual(len(keys), len(set(keys)), f"key 重复：{keys}")

    def test_find_unknown_key_returns_none(self):
        self.assertIsNone(mini_apps.find_app("不存在的key"))

    def test_all_apps_returns_copy(self):
        """返回副本：外部乱改不该影响注册表本身。"""
        snapshot = mini_apps.all_apps()
        snapshot.clear()
        self.assertTrue(mini_apps.all_apps())

    def test_register_replaces_same_key_in_place(self):
        with mock.patch.object(mini_apps, "_APPS", []):
            mini_apps.register(_fake_app("a", "甲"))
            mini_apps.register(_fake_app("b", "乙"))
            mini_apps.register(_fake_app("a", "甲改"))
            self.assertEqual([a.key for a in mini_apps.all_apps()], ["a", "b"])
            self.assertEqual(mini_apps.find_app("a").name, "甲改")

    def test_factory_creates_new_widget_each_call(self):
        app = mini_apps.find_app("calculator")
        first, second = app.create(), app.create()
        self.assertIsNot(first, second)


# ---------------------------------------------------------------------------
# 九宫格页
# ---------------------------------------------------------------------------
class TestToolsTab(_QtCase):

    def test_card_per_registered_app(self):
        tab = ToolsTab()
        apps = mini_apps.all_apps()
        self.assertEqual(len(tab.cards()), len(apps))
        self.assertEqual([c.text() for c in tab.cards()], [a.name for a in apps])

    def test_card_shows_icon_and_tooltip(self):
        tab = ToolsTab()
        card = tab.cards()[0]
        self.assertFalse(card.icon().isNull(), "卡片应当有图标")
        self.assertIn(mini_apps.all_apps()[0].desc, card.toolTip())
        self.assertEqual(card.iconSize().width(), tools_tab_mod.CARD_ICON)

    def test_grid_is_three_columns(self):
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(4)]):
            tab = ToolsTab()
            grid = tab.grid
            positions = [grid.getItemPosition(grid.indexOf(c))
                         for c in tab.cards()]
        self.assertEqual(positions[0][:2], (0, 0))
        self.assertEqual(positions[1][:2], (0, 1))
        self.assertEqual(positions[2][:2], (0, 2))
        self.assertEqual(positions[3][:2], (1, 0), "第四张卡片应当换行")

    def test_click_opens_mini_window(self):
        tab = ToolsTab()
        card = tab.cards()[0]
        with mock.patch.object(tools_tab_mod, "open_mini_app") as opener:
            card.click()
        opener.assert_called_once()
        self.assertIs(opener.call_args.args[0], mini_apps.all_apps()[0])

    def test_reload_picks_up_new_app(self):
        with mock.patch.object(mini_apps, "_APPS", [_fake_app("only", "唯一")]):
            tab = ToolsTab()
            self.assertEqual(len(tab.cards()), 1)
            mini_apps.register(_fake_app("extra", "新增"))
            tab.reload()
            self.assertEqual([c.text() for c in tab.cards()], ["唯一", "新增"])

    def test_empty_registry_shows_hint(self):
        with mock.patch.object(mini_apps, "_APPS", []):
            tab = ToolsTab()
            self.assertEqual(tab.cards(), [])
            self.assertTrue(tab._empty_label.isVisibleTo(tab))


# ---------------------------------------------------------------------------
# 独立窗口
# ---------------------------------------------------------------------------
class TestMiniAppWindow(_QtCase):

    def test_window_is_independent_toplevel(self):
        """与主程序相互独立：没有 Qt 父对象，自己就是顶层窗口。"""
        win = open_mini_app(mini_apps.find_app("calculator"))
        self.assertIsNone(win.parent())
        self.assertTrue(win.isWindow())
        self.assertIs(win.window(), win)
        self.assertEqual(win.windowTitle(), "计算器")

    def test_same_key_reuses_one_window(self):
        app = mini_apps.find_app("calculator")
        first = open_mini_app(app)
        second = open_mini_app(app)
        self.assertIs(first, second)
        self.assertEqual(len(open_apps()), 1)

    def test_different_keys_open_separate_windows(self):
        with mock.patch.object(mini_apps, "_APPS", []):
            a, b = open_mini_app(_fake_app("a", "甲")), open_mini_app(_fake_app("b", "乙"))
        self.assertIsNot(a, b)
        self.assertEqual(len(open_apps()), 2)

    def test_close_removes_from_registry_and_allows_reopen(self):
        app = mini_apps.find_app("calculator")
        first = open_mini_app(app)
        old_content = first.content
        first.close()
        self.assertEqual(open_apps(), [])
        second = open_mini_app(app)
        self.assertIsNot(second, first)
        self.assertIsNot(second.content, old_content,
                         "重开应当是全新实例（工厂语义）")

    def test_close_all_closes_everything(self):
        with mock.patch.object(mini_apps, "_APPS", []):
            open_mini_app(_fake_app("a", "甲"))
            open_mini_app(_fake_app("b", "乙"))
        close_all()
        self.assertEqual(open_apps(), [])

    def test_window_hosts_app_widget(self):
        win = open_mini_app(mini_apps.find_app("calculator"))
        self.assertIsInstance(win, MiniAppWindow)
        self.assertEqual(win.app_key, "calculator")
        # 计算器主控件应当在窗口里（而不是被塞进主窗口的某个标签页）
        from app.mini_apps.calculator import CalculatorWidget
        self.assertIsInstance(win.content, CalculatorWidget)

    def test_window_respects_content_minimum_size(self):
        """★ 回归：宿主窗口不能只看 sizeHint。

        QTableWidget 这类控件的 sizeHint 很小（启动项页实测会开出 517x335，
        比内容下限还小、把界面挤变形），所以内容显式设的 minimumSize 必须被尊重。
        """
        from PySide6.QtWidgets import QWidget

        class _Big(QWidget):
            def __init__(self):
                super().__init__()
                self.setMinimumSize(640, 400)

        win = open_mini_app(MiniApp(key="big", name="大块头", icon="📐",
                                    desc="", factory=_Big))
        self.assertGreaterEqual(win.width(), 640)
        self.assertGreaterEqual(win.height(), 400)
        self.assertGreaterEqual(win.minimumWidth(), 640)

    def test_place_without_screen_is_noop(self):
        """拿不到屏幕（极端环境）时不该抛异常。"""
        with mock.patch.object(mini_window.QApplication, "primaryScreen",
                               return_value=None):
            win = mini_window.MiniAppWindow(_fake_app())
            mini_window._place(win, None)
            win.close()


# ---------------------------------------------------------------------------
# 示例小程序：计算器
# ---------------------------------------------------------------------------
class TestCalculator(_QtCase):

    def _calc(self):
        from app.mini_apps.calculator import CalculatorWidget
        return CalculatorWidget()

    def _seq(self, calc, *keys):
        for k in keys:
            calc.press(k)
        return calc.display_text

    def test_basic_arithmetic(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "1", "+", "2", "="), "3")
        self.assertEqual(self._seq(calc, "C", "9", "×", "8", "="), "72")
        self.assertEqual(self._seq(calc, "C", "9", "−", "4", "="), "5")
        self.assertEqual(self._seq(calc, "C", "8", "÷", "2", "="), "4")

    def test_minus_button_is_not_the_ascii_hyphen(self):
        """回归：减号是显示字符 −(U+2212)，内部运算符表也必须用它。

        曾经的 bug：按钮发的是 −、运算符表里却是 ASCII -，按下减号被静默忽略，
        9 × 8 − 4 算成了 9 × 84 = 756。
        """
        from app.mini_apps.calculator import _BINARY_OPS
        self.assertIn("−", _BINARY_OPS)
        self.assertNotIn("-", _BINARY_OPS)
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "9", "×", "8", "−", "4", "="), "68")

    def test_chained_operators_evaluate_immediately(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "1", "+", "2", "+", "3", "="), "6")

    def test_repeated_operator_does_not_recompute(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "1", "+", "+", "2", "="), "3")

    def test_divide_by_zero_shows_error(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "9", "÷", "0", "="), "错误")

    def test_error_clears_on_next_digit(self):
        calc = self._calc()
        self._seq(calc, "C", "9", "÷", "0", "=")
        self.assertEqual(self._seq(calc, "7"), "7")

    def test_decimal_point_only_once(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "1", ".", ".", "5"), "1.5")

    def test_percent_and_negate_and_backspace(self):
        calc = self._calc()
        self.assertEqual(self._seq(calc, "C", "5", "0", "%"), "0.5")
        self.assertEqual(self._seq(calc, "C", "1", "2", "3", "±"), "-123")
        self.assertEqual(self._seq(calc, "←", "←"), "-1")
        self.assertEqual(self._seq(calc, "C", "0", "±"), "0")   # 0 取反没意义，保持 0

    def test_clear_resets_everything(self):
        calc = self._calc()
        self._seq(calc, "C", "1", "+", "2")
        self.assertEqual(self._seq(calc, "C"), "0")

    def test_keyboard_mapping(self):
        """键盘走的是同一个 press()：* -> ×、/ -> ÷、ASCII - -> −。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication

        calc = self._calc()
        for text, key in [("9", Qt.Key_9), ("*", Qt.Key_Asterisk),
                          ("8", Qt.Key_8), ("-", Qt.Key_Minus),
                          ("4", Qt.Key_4)]:
            calc.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier, text))
        calc.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return,
                                     Qt.NoModifier, "\r"))
        QApplication.processEvents()
        self.assertEqual(calc.display_text, "68")
        calc.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape,
                                     Qt.NoModifier, ""))
        self.assertEqual(calc.display_text, "0")

    def test_unknown_key_is_ignored(self):
        calc = self._calc()
        self._seq(calc, "C", "1")
        self.assertEqual(self._seq(calc, "无此键"), "1")


# ---------------------------------------------------------------------------
# 接线契约（源码级：构造整个 MainWindow 太重，钉住写法即可）
# ---------------------------------------------------------------------------
class TestMainWindowWiring(unittest.TestCase):

    def test_tools_tab_sits_left_of_settings(self):
        """「设置」现在只剩标签栏最右侧的齿轮图标，但页签顺序仍在最后（小工具在它左边）。"""
        src = inspect.getsource(MainWindow.__init__)
        tools = src.index('tabs.addTab(self.tools_tab, "🧰 小工具")')
        settings = src.index('tabs.addTab(self.settings_tab, "")')
        self.assertLess(tools, settings, "「小工具」标签必须在「设置」左边")
        self.assertIn("self.tools_tab = ToolsTab()", src)

    def test_shutdown_closes_mini_windows(self):
        src = inspect.getsource(MainWindow.shutdown)
        self.assertIn("close_mini_windows", src)


if __name__ == "__main__":
    unittest.main()
