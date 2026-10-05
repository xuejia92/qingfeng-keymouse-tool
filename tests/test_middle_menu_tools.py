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

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidgetAction  # noqa: E402

from app import config as config_mod  # noqa: E402
from app import tool_entries  # noqa: E402
from app.config import AppConfig, Flow, MiddleMenuItem  # noqa: E402
from app.ui import middle_menu_tools as mmt  # noqa: E402
from app.ui import middle_menu_tab as mmtab  # noqa: E402
from app.ui import main_window as mw_mod  # noqa: E402
from app.ui import theme  # noqa: E402
from tests._env import TempConfigPaths  # noqa: E402

KNOWN = "calculator"            # 内置小程序，注册表里一定有
OTHER = "startup_items"

_ESC_PATCHER = None


class _FakeEscListener:
    """替身全局 Esc 监听：show_middle_menu 会给菜单挂真的进程级键盘钩子，
    测试里没必要（详见 test_middle_menu_trigger.setUpModule 的说明）。"""

    def __init__(self, on_esc):
        self.on_esc = on_esc

    def start(self):
        return True

    def stop(self):
        pass

    def press(self):
        self.on_esc()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def setUpModule():      # noqa: N802（unittest 约定）
    global _ESC_PATCHER
    _ESC_PATCHER = mock.patch.object(mw_mod.cancel_key, "EscListener",
                                     _FakeEscListener)
    _ESC_PATCHER.start()


def tearDownModule():   # noqa: N802（unittest 约定）
    if _ESC_PATCHER is not None:
        _ESC_PATCHER.stop()


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

    def test_clean_custom_tools(self):
        """自定义程序条目的清洗：非列表/空值/脏结构都不能让加载失败。"""
        clean = config_mod.clean_custom_tools
        self.assertEqual(clean(None), [])
        self.assertEqual(clean("不是列表"), [])
        self.assertEqual(clean([None, 1, "x"]), [])
        self.assertEqual(clean([{"name": "", "path": "C:/a.exe"},
                                {"name": "甲", "path": ""}]), [])
        self.assertEqual(clean([{"name": " 甲 ", "path": " C:/a.exe "}]),
                         [{"name": "甲", "path": "C:/a.exe"}])
        # 同一路径只留一条；多余键丢掉
        self.assertEqual(clean([{"name": "甲", "path": "C:/a.exe", "x": 1},
                                {"name": "乙", "path": "C:/a.exe"}]),
                         [{"name": "甲", "path": "C:/a.exe"}])

    def test_clean_custom_tools_caps(self):
        many = [{"name": f"n{i}", "path": f"C:/p{i}.exe"}
                for i in range(config_mod.CUSTOM_TOOLS_MAX + 5)]
        self.assertEqual(len(config_mod.clean_custom_tools(many)),
                         config_mod.CUSTOM_TOOLS_MAX)

    def test_custom_tools_round_trip(self):
        """加进配置后能存能读（重启后卡片还在）。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.custom_tools = [dict(CUSTOM)]
            cfg.save(save_flows=False)
            again = AppConfig.load()
        self.assertEqual(again.custom_tools, [dict(CUSTOM)])

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

    # ---------- 拖动调位置（2026-10-05 用户要求）----------

    def test_move_slot_swaps_two_filled_cells(self):
        """★ 拖到已填的格子 = 两者交换。"""
        editor = mmt.ToolGridEditor([KNOWN, OTHER])
        seen = []
        editor.changed.connect(lambda: seen.append(1))
        self.assertTrue(editor.move_slot(0, 1))
        self.assertEqual(editor.keys(), [OTHER, KNOWN])
        self.assertEqual(seen, [1], "换位后要通知调用方写回配置")
        self.assertEqual(editor.slots()[0].text(), "启动项")
        self.assertEqual(editor.slots()[1].text(), "计算器")

    def test_move_slot_to_empty_goes_to_the_end(self):
        """拖到空格子 = 挪到末尾（宫格保持紧凑，不留空洞）。"""
        editor = mmt.ToolGridEditor([KNOWN, OTHER])
        self.assertTrue(editor.move_slot(0, 5))
        self.assertEqual(editor.keys(), [OTHER, KNOWN])
        self.assertEqual(editor.slots()[2].text(), "＋", "后面仍是空格子")

    def test_move_slot_rejects_useless_drags(self):
        editor = mmt.ToolGridEditor([KNOWN])
        seen = []
        editor.changed.connect(lambda: seen.append(1))
        self.assertFalse(editor.move_slot(0, 0), "原地不动")
        self.assertFalse(editor.move_slot(4, 0), "来源格是空的")
        self.assertFalse(editor.move_slot(-1, 1), "越界")
        self.assertFalse(editor.move_slot(0, mmt.MAX_TOOLS), "目标越界")
        self.assertEqual(editor.keys(), [KNOWN])
        self.assertEqual(seen, [], "无效拖动不该发信号")

    def test_drop_on_another_slot_reorders(self):
        """★ 真实的放置事件（拖放链路）也要能换位。"""
        from PySide6.QtGui import QDropEvent

        editor = mmt.ToolGridEditor([KNOWN, OTHER])
        data = QMimeData()
        data.setData(mmt.SLOT_MIME, b"0")
        event = QDropEvent(QPointF(1, 1), Qt.MoveAction, data,
                           Qt.LeftButton, Qt.NoModifier)
        try:
            editor.slots()[1].dropEvent(event)      # 把第 0 格拖到第 1 格
            self.assertEqual(editor.keys(), [OTHER, KNOWN])
            self.assertTrue(event.isAccepted())
        finally:
            # ⚠️ QDropEvent 不持有 QMimeData 所有权：显式保活，别让它在事件还活着时被回收
            del event
            _keep_alive = data

    def test_drop_with_foreign_mime_is_ignored(self):
        """别的来源的拖放（比如从资源管理器拖文件进来）不能被当成换位。"""
        from PySide6.QtGui import QDropEvent

        editor = mmt.ToolGridEditor([KNOWN, OTHER])
        data = QMimeData()
        data.setText("hello")
        event = QDropEvent(QPointF(1, 1), Qt.MoveAction, data,
                           Qt.LeftButton, Qt.NoModifier)
        try:
            editor.slots()[1].dropEvent(event)
            self.assertEqual(editor.keys(), [KNOWN, OTHER], "顺序不该变")
        finally:
            del event
            _keep_alive2 = data

    def test_only_filled_slots_are_draggable(self):
        """空格子没内容可搬，不该发起拖动（否则在空格上按住拖会白闪一下）。"""
        editor = mmt.ToolGridEditor([KNOWN])
        self.assertTrue(editor.slots()[0].property("slotFilled"))
        self.assertFalse(editor.slots()[1].property("slotFilled"))
        editor.remove(KNOWN)
        self.assertFalse(editor.slots()[0].property("slotFilled"))

    def test_dragging_a_filled_slot_carries_its_index(self):
        """★ 拖起一个已填格子：要带上自己的槽位号（这就是"我把哪一格挪出去"）。"""
        from PySide6.QtGui import QMouseEvent

        editor = mmt.ToolGridEditor([KNOWN, OTHER])
        button = editor.slots()[1]
        button._press_pos = QPoint(0, 0)

        class _FakeDrag:
            def __init__(self, parent=None):
                self.mime = None
                self.exec_called = False

            def setMimeData(self, mime):
                self.mime = mime

            def setPixmap(self, *_a):
                pass

            def setHotSpot(self, *_a):
                pass

            def exec(self, *_a):
                self.exec_called = True
                return None

        made = []

        def _factory(parent=None):
            drag = _FakeDrag(parent)
            made.append(drag)
            return drag

        event = QMouseEvent(QMouseEvent.MouseMove, QPointF(40, 40), Qt.NoButton,
                            Qt.LeftButton, Qt.NoModifier)
        with mock.patch.object(mmt, "QDrag", _factory):
            button.mouseMoveEvent(event)
        self.assertEqual(len(made), 1, "越过阈值后应当发起拖动")
        self.assertTrue(made[0].exec_called)
        self.assertEqual(bytes(made[0].mime.data(mmt.SLOT_MIME)), b"1")

    def test_dragging_an_empty_slot_does_nothing(self):
        from PySide6.QtGui import QMouseEvent

        editor = mmt.ToolGridEditor([KNOWN])
        button = editor.slots()[2]          # 空格子
        button._press_pos = QPoint(0, 0)
        event = QMouseEvent(QMouseEvent.MouseMove, QPointF(40, 40), Qt.NoButton,
                            Qt.LeftButton, Qt.NoModifier)
        with mock.patch.object(mmt, "QDrag") as drag:
            button.mouseMoveEvent(event)
        self.assertFalse(drag.called)


# ---------------------------------------------------------------------------
# 自定义程序也能进九宫格（2026-10-04 用户要求）
# ---------------------------------------------------------------------------
CUSTOM = {"name": "记事本", "path": r"C:\Windows\notepad.exe"}


class TestCustomToolEntries(_QtCase):
    """用户在小工具页加进来的电脑里的程序，九宫格也要能选、能显示、能打开。"""

    def test_key_is_stable_short_and_case_insensitive(self):
        """key 用哈希而不是路径：路径比 `clean_tool_keys` 的 40 字符上限长得多。

        而且 Windows 路径大小写/分隔符不敏感，`C:\\X\\a.exe` 与 `c:/x/A.exe`
        必须算同一条。
        """
        key = tool_entries.custom_key(CUSTOM["path"])
        self.assertTrue(key.startswith(tool_entries.CUSTOM_PREFIX))
        self.assertLess(len(key), 40, "必须能活着通过 clean_tool_keys 的截断")
        self.assertEqual(key, tool_entries.custom_key(CUSTOM["path"].lower()))
        self.assertEqual(key, tool_entries.custom_key(
            CUSTOM["path"].replace("\\", "/")))
        self.assertNotEqual(key, tool_entries.custom_key(r"C:\Windows\calc.exe"))

    def test_available_entries_put_custom_after_builtin(self):
        entries = mmt.available_entries([CUSTOM])
        self.assertEqual(len(entries), len(mmt.available_tools()) + 1)
        self.assertEqual(entries[-1].name, "记事本")
        self.assertTrue(entries[-1].is_custom)
        self.assertEqual(entries[-1].desc, CUSTOM["path"])

    def test_available_entries_without_custom_is_builtin_only(self):
        self.assertEqual(len(mmt.available_entries(None)),
                         len(mmt.available_tools()))
        self.assertEqual(len(mmt.available_entries([])),
                         len(mmt.available_tools()))

    def test_grid_widget_renders_and_reports_custom_key(self):
        key = tool_entries.custom_key(CUSTOM["path"])
        picked = []
        grid = mmt.ToolGridWidget([key], picked.append, custom_tools=[CUSTOM])
        self.assertEqual(grid.keys(), [key])
        button = grid.buttons()[0]
        self.assertEqual(button.text(), "记事本")
        self.assertEqual(button.toolTip(), CUSTOM["path"])
        self.assertFalse(button.icon().isNull(), "自定义程序要有图标（真文件图标）")
        grid.click_at(0)
        self.assertEqual(picked, [key])

    def test_grid_widget_drops_custom_key_that_no_longer_exists(self):
        """小工具页删掉这个程序后，留在九宫格里的 key 认不出 —— 直接跳过。"""
        key = tool_entries.custom_key(CUSTOM["path"])
        grid = mmt.ToolGridWidget([key], lambda _k: None, custom_tools=[])
        self.assertEqual(grid.keys(), [])
        self.assertEqual(grid.buttons(), [])

    def test_editor_assigns_and_shows_custom_program(self):
        editor = mmt.ToolGridEditor([], custom_tools=[CUSTOM])
        key = tool_entries.custom_key(CUSTOM["path"])
        self.assertTrue(editor.assign(key), "自定义程序也要能加进九宫格")
        self.assertEqual(editor.keys(), [key])
        slot = editor.slots()[0]
        self.assertEqual(slot.text(), "记事本")
        self.assertFalse(slot.icon().isNull())

    def test_editor_lists_custom_programs_as_candidates(self):
        editor = mmt.ToolGridEditor([], custom_tools=[CUSTOM])
        names = [entry.name for entry in editor.entries()]
        self.assertIn("记事本", names)
        self.assertIn("计算器", names, "内置小程序仍在候选里")

    def test_editor_follows_custom_tool_removal(self):
        """小工具页删掉程序后，管理页的九宫格要跟着把这一格清掉。"""
        key = tool_entries.custom_key(CUSTOM["path"])
        editor = mmt.ToolGridEditor([key, KNOWN], custom_tools=[CUSTOM])
        self.assertEqual(editor.keys(), [key, KNOWN])
        editor.set_custom_tools([])
        self.assertEqual(editor.keys(), [KNOWN])

    # ---------- 实时获取（用户反馈"动态新添加的程序，中键菜单里拿不到"）----------

    def test_editor_reads_custom_tools_live_via_provider(self):
        """★ 编辑区必须**实时**读自定义程序列表，不能只在构造时抄一份。

        用户在小工具页加完程序通常不会再进一次管理页，抄一份的话候选里永远是旧的
        （反馈原话：「动态新添加的电脑程序，中键菜单里面无法实时获取到」）。
        """
        live = {"tools": []}
        editor = mmt.ToolGridEditor([], custom_tools_provider=lambda: live["tools"])
        self.assertEqual([e.name for e in editor.entries()], [a.name for a in
                                                              mmt.available_tools()])
        live["tools"] = [dict(CUSTOM)]          # 模拟用户在工具页刚加了程序
        names = [entry.name for entry in editor.entries()]
        self.assertIn("记事本", names, "provider 变了就该立刻看到")
        key = tool_entries.custom_key(CUSTOM["path"])
        self.assertTrue(editor.assign(key), "刚加的程序也要能马上放进九宫格")

    def test_on_tools_changed_clears_slot_of_removed_program(self):
        """程序被删掉后，九宫格里那一格要清掉（别留个点不动的格子）。"""
        live = {"tools": [dict(CUSTOM)]}
        key = tool_entries.custom_key(CUSTOM["path"])
        editor = mmt.ToolGridEditor([key, KNOWN],
                                    custom_tools_provider=lambda: live["tools"])
        self.assertEqual(editor.keys(), [key, KNOWN])
        live["tools"] = []                      # 小工具页把程序删了
        editor.on_tools_changed()
        self.assertEqual(editor.keys(), [KNOWN])
        self.assertEqual(editor.slots()[1].text(), "＋")

    def test_provider_failure_does_not_break_the_editor(self):
        """provider 出异常时当作"没有自定义程序"，不能让编辑区挂掉。"""
        def boom():
            raise RuntimeError("配置读取失败")
        editor = mmt.ToolGridEditor([KNOWN], custom_tools_provider=boom)
        self.assertEqual(editor.keys(), [KNOWN])
        self.assertEqual([e.name for e in editor.entries()],
                         [a.name for a in mmt.available_tools()])

    def test_build_menu_includes_custom_tool(self):
        key = tool_entries.custom_key(CUSTOM["path"])
        menu = mmtab.build_menu([], [], tools=[key], custom_tools=[CUSTOM])
        self.assertIsNotNone(menu, "只有自定义程序时也要能弹菜单")
        widget_actions = [a for a in menu.actions()
                          if isinstance(a, QWidgetAction)]
        self.assertEqual(len(widget_actions), 1)
        self.assertEqual(widget_actions[0].defaultWidget().keys(), [key])
        menu.deleteLater()

    def test_build_menu_ignores_custom_key_without_the_program(self):
        key = tool_entries.custom_key(CUSTOM["path"])
        self.assertIsNone(mmtab.build_menu([], [], tools=[key],
                                           custom_tools=[]))


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


class _FakeWatcher:
    """替身中键监听器：show_middle_menu 会用它开/关「菜单期间吞中键」。"""

    def __init__(self):
        self.suppress_value = None
        self.menu_open_value = None

    def set_suppress(self, value):
        self.suppress_value = bool(value)

    def set_menu_open(self, value):
        self.menu_open_value = bool(value)

    def is_running(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass


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
        self.win._menu_swallow_trigger_until = 0.0
        self.watcher = _FakeWatcher()
        self.win.mouse_watcher = self.watcher
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

    def test_open_custom_tool_launches_the_program(self):
        """★ 九宫格里的自定义程序：点击 = 用系统默认方式打开它（不是开小程序窗口）。"""
        key = tool_entries.custom_key(CUSTOM["path"])
        shell = _Shell(tools=[key])
        shell.win.cfg.custom_tools = [dict(CUSTOM)]
        with mock.patch.object(tool_entries.os.path, "exists", return_value=True), \
                mock.patch.object(tool_entries.QDesktopServices, "openUrl") as open_url, \
                mock.patch("app.ui.mini_window.open_mini_app") as opener:
            open_url.return_value = True
            shell.win._open_middle_menu_tool(key)
        self.assertTrue(open_url.called, "应当交给系统打开这个程序")
        self.assertFalse(opener.called, "自定义程序不该被当成内置小程序开窗口")
        self.assertEqual(
            open_url.call_args.args[0].toLocalFile().replace("/", "\\").lower(),
            CUSTOM["path"].lower())

    def test_open_custom_tool_after_it_was_removed_reports(self):
        """小工具页把程序删掉后，九宫格里的旧 key 认不出 → 提示而不是崩。"""
        key = tool_entries.custom_key(CUSTOM["path"])
        shell = _Shell(tools=[key])
        shell.win.cfg.custom_tools = []
        with mock.patch.object(tool_entries.QDesktopServices, "openUrl") as open_url:
            shell.win._open_middle_menu_tool(key)
        self.assertFalse(open_url.called)
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
        self.assertIs(shell.watcher.menu_open_value, False,
                      "菜单收起后要把「吞中键」关掉")

    def test_nothing_configured_shows_nothing(self):
        shell = _Shell(tools=[], items=[])
        with mock.patch.object(mw_mod, "build_menu") as build, \
                mock.patch.object(mw_mod.QCursor, "pos", return_value=QPoint(5, 5)):
            shell.win.show_middle_menu()
        self.assertFalse(build.called)


if __name__ == "__main__":
    unittest.main()
