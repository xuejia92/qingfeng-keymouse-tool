# -*- coding: utf-8 -*-
"""中键菜单「九宫格工具」的测试（2026-10-03 改版）。

菜单新结构：**上面是九宫格小工具**（最多 9 格），**下面是关联流程的菜单项**。

覆盖：
- config：`clean_tool_keys`（去空白/去重/截断到 9）与 `middle_menu_tools` 的读写往返；
- 宫格解析：认不出的 key 直接跳过（坏数据不该让菜单弹不出来）；
- 弹出菜单：有工具时首项是承载宫格的 QWidgetAction + 分隔线，没工具时与改造前完全一致；
- 管理页编辑区：固定 9 槽、增删、`changed` 信号；
- 主窗口接线：工具点击回传 → 打开小程序、且**不会**被当成流程条目。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidgetAction  # noqa: E402

from app import config as config_mod  # noqa: E402
from app.config import AppConfig, Flow, MiddleMenuItem  # noqa: E402
from app.ui import middle_menu_tools as mmt  # noqa: E402
from app.ui import middle_menu_tab as mmtab  # noqa: E402
from app.ui import main_window as mw_mod  # noqa: E402
from app.ui import theme  # noqa: E402
from tests._env import TempConfigPaths  # noqa: E402

KNOWN = "calculator"            # 内置小程序，注册表里一定有
OTHER = "startup_items"


class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self):
        theme.clear_registry()


def _flow(name: str, fid: str) -> Flow:
    return Flow(id=fid, name=name)


def _item(fid: str, **kw) -> MiddleMenuItem:
    return MiddleMenuItem(flow_id=fid, **kw)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):

    def test_clean_tool_keys(self):
        self.assertEqual(config_mod.clean_tool_keys(None), [])
        self.assertEqual(config_mod.clean_tool_keys("不是列表"), [])
        self.assertEqual(config_mod.clean_tool_keys(["a", "", "  ", "b"]), ["a", "b"])
        self.assertEqual(config_mod.clean_tool_keys(["a", "a"]), ["a"])

    def test_clean_tool_keys_caps_at_nine(self):
        keys = [f"k{i}" for i in range(20)]
        out = config_mod.clean_tool_keys(keys)
        self.assertEqual(len(out), 9)
        self.assertEqual(out, keys[:9])

    def test_max_tools_is_nine(self):
        self.assertEqual(config_mod.MIDDLE_MENU_MAX_TOOLS, 9)

    def test_round_trip(self):
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.middle_menu_tools = [KNOWN, OTHER]
            cfg.save(save_flows=False)
            with open(config_mod.CONFIG_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["middle_menu_tools"], [KNOWN, OTHER])
            self.assertEqual(AppConfig.load().middle_menu_tools, [KNOWN, OTHER])

    def test_unknown_keys_are_kept_in_config(self):
        """配置层**不**校验 key 是否存在（同图标 key 的策略）：显示端跳过即可。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.middle_menu_tools = ["不存在的工具", KNOWN]
            cfg.save(save_flows=False)
            self.assertEqual(AppConfig.load().middle_menu_tools,
                             ["不存在的工具", KNOWN])


# ---------------------------------------------------------------------------
# 宫格解析与弹出菜单
# ---------------------------------------------------------------------------
class TestResolveTools(_QtCase):

    def test_skips_unknown_keys(self):
        out = mmt.resolve_tools(["不存在的工具", KNOWN])
        self.assertEqual([k for k, _ in out], [KNOWN])

    def test_empty_input(self):
        self.assertEqual(mmt.resolve_tools(None), [])
        self.assertEqual(mmt.resolve_tools([]), [])

    def test_available_tools_are_mini_apps(self):
        keys = [app.key for app in mmt.available_tools()]
        self.assertIn(KNOWN, keys)


class TestToolGridWidget(_QtCase):

    def test_one_button_per_resolved_tool(self):
        grid = mmt.ToolGridWidget([KNOWN, OTHER, "没了"], lambda _k: None)
        self.assertEqual(grid.keys(), [KNOWN, OTHER])
        self.assertEqual(len(grid.buttons()), 2)

    def test_buttons_show_name_and_icon(self):
        grid = mmt.ToolGridWidget([KNOWN], lambda _k: None)
        button = grid.buttons()[0]
        self.assertEqual(button.text(), "计算器")
        self.assertFalse(button.icon().isNull())
        self.assertTrue(button.toolTip())

    def test_click_reports_key(self):
        picked = []
        grid = mmt.ToolGridWidget([KNOWN, OTHER], picked.append)
        grid.click_at(1)
        self.assertEqual(picked, [OTHER])

    def test_layout_is_three_columns(self):
        grid = mmt.ToolGridWidget([KNOWN, OTHER], lambda _k: None)
        layout = grid.layout()
        positions = [layout.getItemPosition(layout.indexOf(b))
                     for b in grid.buttons()]
        self.assertEqual(positions[0][:2], (0, 0))
        self.assertEqual(positions[1][:2], (0, 1))


class TestBuildMenuWithTools(_QtCase):

    def _args(self):
        flows = [_flow("甲", "f1"), _flow("乙", "f2")]
        items = [_item("f1"), _item("f2")]
        return items, flows

    def test_without_tools_menu_is_unchanged(self):
        items, flows = self._args()
        menu = mmtab.build_menu(items, flows)
        self.assertEqual(len(menu.actions()), 2)
        self.assertEqual([a.text() for a in menu.actions()], ["甲", "乙"])
        self.assertEqual([a for a in menu.actions() if a.isSeparator()], [])

    def test_tools_go_on_top_followed_by_separator(self):
        items, flows = self._args()
        menu = mmtab.build_menu(items, flows, None, tools=[KNOWN, OTHER])
        actions = menu.actions()
        self.assertIsInstance(actions[0], QWidgetAction, "宫格必须在最上面")
        self.assertTrue(actions[1].isSeparator(), "宫格与流程之间要有分隔线")
        self.assertEqual([a.text() for a in actions[2:]], ["甲", "乙"])
        self.assertEqual(len([a for a in actions if a.isSeparator()]), 1)

    def test_grid_widget_is_hosted(self):
        items, flows = self._args()
        menu = mmtab.build_menu(items, flows, None, tools=[KNOWN])
        widget_action = menu.actions()[0]
        self.assertIsInstance(widget_action.defaultWidget(), mmt.ToolGridWidget)
        self.assertEqual(widget_action.defaultWidget().keys(), [KNOWN])

    def test_tools_only_menu_still_opens(self):
        """只配了工具、一个流程菜单项都没有时也要能弹出（以前会被判成"没内容"）。"""
        menu = mmtab.build_menu([], [], None, tools=[KNOWN])
        self.assertIsNotNone(menu)
        self.assertIsInstance(menu.actions()[0], QWidgetAction)
        self.assertEqual([a for a in menu.actions() if a.isSeparator()], [])

    def test_all_unknown_tools_behave_like_no_tools(self):
        items, flows = self._args()
        menu = mmtab.build_menu(items, flows, None, tools=["没了", "也没了"])
        self.assertEqual(len(menu.actions()), 2)
        self.assertFalse(any(isinstance(a, QWidgetAction) for a in menu.actions()))

    def test_nothing_configured_returns_none(self):
        self.assertIsNone(mmtab.build_menu([], []))
        self.assertIsNone(mmtab.build_menu([], [], None, tools=[]))

    def test_menu_style_is_theme_tokenized(self):
        """菜单样式跟着主题走（以前写死浅色，深色主题下会白得刺眼）。"""
        menu = mmtab.build_menu([_item("f1")], [_flow("甲", "f1")])
        qss = menu.styleSheet()
        self.assertIn(theme.token("card_bg"), qss)
        self.assertIn(theme.token("primary"), qss)


# ---------------------------------------------------------------------------
# 管理页编辑区
# ---------------------------------------------------------------------------
class TestToolGridEditor(_QtCase):

    def test_always_nine_slots(self):
        editor = mmt.ToolGridEditor([])
        self.assertEqual(len(editor.slots()), 9)
        self.assertEqual(editor.keys(), [])

    def test_assign_and_remove(self):
        editor = mmt.ToolGridEditor([])
        seen = []
        editor.changed.connect(lambda: seen.append(1))
        self.assertTrue(editor.assign(KNOWN))
        self.assertTrue(editor.assign(OTHER))
        self.assertEqual(editor.keys(), [KNOWN, OTHER])
        self.assertEqual(seen, [1, 1])
        self.assertFalse(editor.assign(KNOWN), "重复添加应被拒绝")
        self.assertEqual(seen, [1, 1], "被拒绝时不该发信号")
        self.assertTrue(editor.remove(KNOWN))
        self.assertEqual(editor.keys(), [OTHER])

    def test_assign_rejects_unknown_key(self):
        editor = mmt.ToolGridEditor([])
        self.assertFalse(editor.assign("不存在的工具"))
        self.assertFalse(editor.assign(""))
        self.assertEqual(editor.keys(), [])

    def test_assign_stops_at_nine(self):
        editor = mmt.ToolGridEditor([])
        keys = [app.key for app in mmt.available_tools()]
        for key in keys[:9]:
            editor.assign(key)
        self.assertEqual(len(editor.keys()), min(9, len(keys)))
        self.assertFalse(editor.assign("再多一个" if len(keys) < 9 else "满了"))

    def test_slots_show_filled_and_empty(self):
        editor = mmt.ToolGridEditor([KNOWN])
        slots = editor.slots()
        self.assertEqual(slots[0].text(), "计算器")
        self.assertFalse(slots[0].icon().isNull())
        self.assertEqual(slots[1].text(), "＋")
        self.assertTrue(slots[1].icon().isNull())

    def test_set_keys_filters_and_caps(self):
        editor = mmt.ToolGridEditor([])
        editor.set_keys(["没了", KNOWN, KNOWN, OTHER])
        self.assertEqual(editor.keys(), [KNOWN, OTHER])

    def test_count_label_mentions_capacity(self):
        editor = mmt.ToolGridEditor([KNOWN])
        self.assertIn("1 / 9", editor.count_label.text())


# ---------------------------------------------------------------------------
# 主窗口接线
# ---------------------------------------------------------------------------
class _FakeMenu:
    def __init__(self, on_exec=None):
        self._on_exec = on_exec
        self.closed = False
        self.deleted = False
        self.exec_pos = None

    def exec(self, pos):
        self.exec_pos = pos
        if self._on_exec is not None:
            callback, self._on_exec = self._on_exec, None
            callback()
        return None                     # 工具点击不通过返回值回传

    def close(self):
        self.closed = True

    def deleteLater(self):
        self.deleted = True


class _Status:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, ms=0):
        self.messages.append(text)


class _Shell:
    """不跑 __init__ 的 MainWindow 壳，只装被测方法要用的属性。"""

    def __init__(self, tools=None, items=None):
        from app.ui.main_window import MainWindow
        self.win = MainWindow.__new__(MainWindow)
        self.win.cfg = AppConfig()
        self.win.cfg.flows = [_flow("甲", "f1")]
        self.win.cfg.middle_menu_items = list(items if items is not None
                                              else [_item("f1")])
        self.win.cfg.middle_menu_tools = list(tools or [])
        self.win.cfg.middle_menu_enabled = True
        self.win._middle_menu_open = False
        self.win._middle_menu = None
        self.win._pending_middle_pos = None
        self.win._pending_tool = ""
        self.status = _Status()
        self.win.statusBar = lambda: self.status
        self.opened: list[str] = []


class TestMainWindowToolWiring(_QtCase):

    def test_tool_callback_records_and_closes(self):
        shell = _Shell()
        menu = _FakeMenu()
        shell.win._middle_menu = menu
        shell.win._on_middle_menu_tool(KNOWN)
        self.assertEqual(shell.win._pending_tool, KNOWN)
        self.assertTrue(menu.closed, "点了工具就该关掉菜单")

    def test_tool_callback_without_menu_is_safe(self):
        shell = _Shell()
        shell.win._on_middle_menu_tool(KNOWN)       # 不该抛
        self.assertEqual(shell.win._pending_tool, KNOWN)

    def test_open_tool_uses_same_path_as_tools_page(self):
        shell = _Shell()
        with mock.patch("app.ui.mini_window.open_mini_app") as opener:
            shell.win._open_middle_menu_tool(KNOWN)
        opener.assert_called_once()
        self.assertEqual(opener.call_args.args[0].key, KNOWN)

    def test_open_unknown_tool_reports(self):
        shell = _Shell()
        with mock.patch("app.ui.mini_window.open_mini_app") as opener:
            shell.win._open_middle_menu_tool("没了")
        self.assertFalse(opener.called)
        self.assertTrue(shell.status.messages)

    def test_picking_tool_opens_it_and_skips_flow(self):
        """工具点击不能被当成流程条目：打开工具后本轮就结束。"""
        shell = _Shell(tools=[KNOWN])
        menu = _FakeMenu(on_exec=lambda: shell.win._on_middle_menu_tool(KNOWN))
        # 注意要打 main_window 命名空间里的 build_menu / QCursor（它是 from ... import）
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                mock.patch.object(shell.win, "_open_middle_menu_tool") as opener, \
                mock.patch.object(shell.win, "_run_flow_from_middle_menu") as runner, \
                mock.patch.object(mw_mod.QCursor, "pos", return_value=QPoint(5, 5)):
            shell.win.show_middle_menu()
        opener.assert_called_once_with(KNOWN)
        self.assertFalse(runner.called)
        self.assertTrue(menu.deleted)

    def test_menu_shows_when_only_tools_configured(self):
        """没有流程菜单项、只有九宫格工具时也要弹菜单。"""
        shell = _Shell(tools=[KNOWN], items=[])
        menu = _FakeMenu()
        with mock.patch.object(mw_mod, "build_menu", return_value=menu) as build, \
                mock.patch.object(mw_mod.QCursor, "pos", return_value=QPoint(5, 5)):
            shell.win.show_middle_menu()
        self.assertTrue(build.called)
        self.assertEqual(build.call_args.kwargs.get("tools"), [KNOWN])

    def test_nothing_configured_shows_nothing(self):
        shell = _Shell(tools=[], items=[])
        with mock.patch.object(mw_mod, "build_menu") as build, \
                mock.patch.object(mw_mod.QCursor, "pos", return_value=QPoint(5, 5)):
            shell.win.show_middle_menu()
        self.assertFalse(build.called)


if __name__ == "__main__":
    unittest.main()
