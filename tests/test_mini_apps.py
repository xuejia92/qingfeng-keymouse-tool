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

from PySide6.QtWidgets import QLabel, QSizePolicy

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

    def test_cards_flow_according_to_columns(self):
        """卡片位置由**当前列数**决定，不再是写死的三列（2026-10-04 改自适应）。"""
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(6)]):
            tab = ToolsTab()
            span = tools_tab_mod.CARD_W + tools_tab_mod.CARD_GAP
            tab._apply_width(span * 2)
            self.assertEqual(tab.columns(), 2)
            positions = [tab.grid.getItemPosition(tab.grid.indexOf(c))[:2]
                         for c in tab.cards()]
        self.assertEqual(positions, [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)])

    def test_columns_for_width(self):
        """列数换算的边界：放不下也至少 1 列，超宽则封顶。"""
        span = tools_tab_mod.CARD_W + tools_tab_mod.CARD_GAP
        self.assertEqual(ToolsTab.columns_for_width(0), 1)
        self.assertEqual(ToolsTab.columns_for_width(10), 1)
        self.assertEqual(ToolsTab.columns_for_width(span), 1)
        self.assertEqual(ToolsTab.columns_for_width(span * 2), 2)
        self.assertEqual(ToolsTab.columns_for_width(span * 3 + 1), 3)
        self.assertEqual(ToolsTab.columns_for_width(span * 100),
                         tools_tab_mod.MAX_COLUMNS)

    def test_width_drives_column_count(self):
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(8)]):
            tab = ToolsTab()
            tab._apply_width(200)
            narrow = tab.columns()
            tab._apply_width(1200)
            wide = tab.columns()
        self.assertEqual(narrow, 1)
        self.assertGreater(wide, narrow, "窗口变宽应当排下更多列")
        # 变窄以后卡片要收回来
        tab._apply_width(200)
        self.assertEqual(tab.columns(), narrow)
        self.assertEqual(
            tab.grid.getItemPosition(tab.grid.indexOf(tab.cards()[1]))[:2], (1, 0))

    def test_same_width_does_not_relayout(self):
        """列数没变时不碰任何控件：拖动窗口不该反复重排。"""
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(4)]):
            tab = ToolsTab()
            tab._apply_width(600)
            with mock.patch.object(tab, "_layout_cards") as relayout:
                tab._apply_width(600)
                tab._apply_width(600 + 1)       # 仍落在同一列数上
        self.assertFalse(relayout.called, "列数没变就不该重排")

    def test_stretch_column_follows_column_count(self):
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(4)]):
            tab = ToolsTab()
            span = tools_tab_mod.CARD_W + tools_tab_mod.CARD_GAP
            tab._apply_width(span * 2)
        self.assertEqual(tab.grid.columnStretch(2), 1)
        self.assertEqual(tab.grid.columnStretch(3), 0)
        self.assertEqual(tab.grid.columnStretch(0), 0)

    def test_holder_resize_triggers_column_recompute(self):
        """触发点绑在**卡片容器**的 resizeEvent 上（页面 resize 时它还没跟上）。

        直接投递一个 QResizeEvent：`resize()` 在离屏（控件从未 show）下不保证
        立刻派发，投事件才能确定性地验证"容器宽度变化 -> 回调"这条契约。
        """
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QResizeEvent

        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(4)]):
            tab = ToolsTab()
        calls = []
        tab.grid_holder._on_width_changed = calls.append
        tab.grid_holder.resizeEvent(
            QResizeEvent(QSize(900, 300), QSize(600, 300)))
        self.assertEqual(calls, [900], "容器宽度变化要把新宽度交给外面重算列数")

    def test_cards_are_smaller_than_before(self):
        """用户要求"每个宫格宽高都小一点"：钉住尺寸别再涨回去。"""
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app("k0", "第0项")]):
            tab = ToolsTab()
        card = tab.cards()[0]
        self.assertEqual((card.width(), card.height()),
                         (tools_tab_mod.CARD_W, tools_tab_mod.CARD_H))
        self.assertLess(tools_tab_mod.CARD_W, 168)
        self.assertLess(tools_tab_mod.CARD_H, 130)

    def test_holder_can_shrink_below_its_grid_minimum(self):
        """★ 回归：容器横向必须 Ignored，否则「列数被自己的最小宽度锁死」。

        卡片是 `setFixedSize`，于是 QGridLayout 的**最小宽度** = 当前列数 × 卡宽 + 间距；
        而 setWidgetResizable 的滚动区不会把容器压到它自己的最小宽度以下 ——
        实测页面宽 560 时容器仍是 568（= 4 列的最小宽度），回调永远算不出更少的列，
        卡片直接被裁掉。Ignored 之后容器宽度才真正跟随视口。
        """
        with mock.patch.object(mini_apps, "_APPS",
                               [_fake_app(f"k{i}", f"第{i}项") for i in range(6)]):
            tab = ToolsTab()
            tab._apply_width(1200)          # 先排成多列，把网格最小宽度撑起来
            self.assertEqual(tab.grid_holder.sizePolicy().horizontalPolicy(),
                             QSizePolicy.Ignored)
            tab._apply_width(200)
        self.assertEqual(tab.columns(), 1)

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
