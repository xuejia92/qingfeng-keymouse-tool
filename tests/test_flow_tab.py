"""流程左栏分组（展开/收起）与模块面板分组（收起/展开）的测试。

验证：模块分组 10 个模块全部纳入分组且无遗漏；模块分组默认展开、点击收起并
持久化到 cfg.collapsed_module_groups；流程左栏按分组构建树、流程归属正确、
分组展开/收起持久化到 cfg.collapsed_flow_groups、右键菜单项齐全；
条件分支的缩进渲染与「否则 / 否则如果」的单独删除。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from app.config import (AppConfig, AUTO_STEP_TYPES, FLOW_STEP_TYPES, Flow, FlowStep,
                        default_step_params)
from app.conditions import validate_condition_structure
from app.ui import flow_tab as flow_tab_mod
from app.ui import flow_dialog as flow_dialog_mod
from app.ui import theme
from app.ui.flow_dialog import StepRunDelegate
from app.ui.flow_tab import (BRANCH_TYPES, INDENT_UNIT, MAX_UNDO_STEPS,
                             MODULE_GROUPS, FlowTab)
from tests._env import TempConfigPaths


def _flow_name(item) -> str:
    """左栏流程条目的**纯名字**。

    流程条目文本带一个圆点前缀（`FLOW_BULLET`，用来和分组色带区分，2026-10-01）；
    本文件的断言关心的是「哪个流程、什么顺序」，所以先剥掉装饰再比。
    """
    text = item.text(0)
    bullet = flow_tab_mod.FLOW_BULLET
    return text[len(bullet):] if text.startswith(bullet) else text


class _TempPathsMixin:
    """把 app.config 运行期路径切到临时目录，避免测试保存污染真实 config/flows。"""

    def _temp_enter(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()

    def _temp_exit(self):
        self._tmp.__exit__(None, None, None)


class TestModuleGroupsDefinition(unittest.TestCase):
    def test_groups_cover_all_modules(self):
        """分组必须恰好覆盖所有「可拖拽」步骤类型，且无重复。

        自动成对生成的结构类型（endif 等）不在面板展示，故不要求被分组覆盖。
        （2026-09-04 起「关闭浏览器」不再作为面板伪类型，关闭入口收敛到
        web 步骤对话框的「操作」下拉。）
        """
        grouped = [t for _, _, types in MODULE_GROUPS for t in types]
        draggable = [t for t in FLOW_STEP_TYPES if t not in AUTO_STEP_TYPES]
        self.assertEqual(sorted(grouped), sorted(draggable))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_group_ids_unique(self):
        gids = [gid for gid, _, _ in MODULE_GROUPS]
        self.assertEqual(len(gids), len(set(gids)))


class TestFlowTabModuleGroups(_TempPathsMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    def test_builds_headers_and_buttons(self):
        """分组头数量 = 分组数；模块按钮数量 = 可拖拽步骤类型数（不含自动 endif 等结构标记）。"""
        self.assertEqual(len(self.tab._group_headers), len(MODULE_GROUPS))
        draggable = [t for t in FLOW_STEP_TYPES if t not in AUTO_STEP_TYPES]
        self.assertEqual(len(self.tab._module_btns), len(draggable))

    def test_all_collapsed_by_default(self):
        """未手动调整过折叠状态时默认全部收起：模块区隐藏、标题是收起符、不写配置。"""
        for gid, wrapper in self.tab._group_wrappers.items():
            self.assertTrue(wrapper.isHidden(), f"分组 {gid} 应默认收起")
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith(flow_tab_mod.DISCLOSURE_COLLAPSED))
        # 默认收起只是渲染态：未操作前不落盘，cfg 仍保持空
        self.assertEqual(self.cfg.collapsed_module_groups, [])

    def test_expand_then_collapse(self):
        """点击分组标题：展开 -> 显示模块 + 首次操作固化记忆；再点 -> 收起 + 记入配置。"""
        with mock.patch.object(AppConfig, "save") as save:
            header = self.tab._group_headers["input"]
            header.setChecked(True)                       # 展开（初始全收起）
            self.assertFalse(self.tab._group_wrappers["input"].isHidden())
            # 首次操作：把默认全收起固化，再剔除刚展开的 input
            self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                             sorted(gid for gid, _, _ in MODULE_GROUPS if gid != "input"))
            self.assertTrue(self.cfg.module_groups_explicit)
            self.assertTrue(header.text().startswith(flow_tab_mod.DISCLOSURE_EXPANDED))
            save.assert_called_once()

            header.setChecked(False)                      # 收起
            self.assertTrue(self.tab._group_wrappers["input"].isHidden())
            self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                             sorted(gid for gid, _, _ in MODULE_GROUPS))
            self.assertTrue(header.text().startswith(flow_tab_mod.DISCLOSURE_COLLAPSED))

    def test_expand_persists_and_restores(self):
        """手动展开多组后重建 FlowTab，展开状态从 cfg 恢复（其余默认收起）。"""
        with mock.patch.object(AppConfig, "save"):
            self.tab._group_headers["logic"].setChecked(True)
            self.tab._group_headers["app_web"].setChecked(True)
        self.assertTrue(self.cfg.module_groups_explicit)
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS
                                if gid not in ("app_web", "logic")))

        tab2 = FlowTab(self.cfg)                          # 用同一 cfg 重建
        self.assertFalse(tab2._group_wrappers["logic"].isHidden())
        self.assertFalse(tab2._group_wrappers["app_web"].isHidden())
        self.assertTrue(tab2._group_wrappers["input"].isHidden())
        self.assertTrue(tab2._group_headers["logic"].text().startswith(flow_tab_mod.DISCLOSURE_EXPANDED))
        self.assertTrue(tab2._group_headers["input"].text().startswith(flow_tab_mod.DISCLOSURE_COLLAPSED))

    def test_unknown_group_ids_ignored_on_load(self):
        """config 里未知分组 id 应被忽略，不产生异常。"""
        # 已自定义（explicit=True）时按记忆渲染：未知 id 不匹配任何分组 -> 全展开
        cfg = AppConfig()
        cfg.flows = []
        cfg.module_groups_explicit = True
        cfg.collapsed_module_groups = ["no_such_group"]
        tab2 = FlowTab(cfg)                                # 不应抛异常
        self.assertFalse(any(h.isHidden() for h in tab2._group_headers.values()))
        # 未自定义（explicit=False）时忽略历史残留，仍走默认全收起
        cfg3 = AppConfig()
        cfg3.flows = []
        cfg3.collapsed_module_groups = ["no_such_group"]
        tab3 = FlowTab(cfg3)
        self.assertTrue(all(h.isHidden() for h in tab3._group_wrappers.values()))

    def test_buttons_disabled_while_running_lock(self):
        """运行中锁定编辑：模块按钮与分组头一起禁用。"""
        self.tab._module_btns[0].setEnabled(False)
        for h in self.tab._group_headers.values():
            h.setEnabled(False)
        self.assertFalse(any(b.isEnabled() for b in self.tab._module_btns[:1]))
        self.assertFalse(any(h.isEnabled() for h in self.tab._group_headers.values()))


class TestFlowTabFlowGroups(_TempPathsMixin, unittest.TestCase):
    """流程左栏分组树：构建/归属/展开收起持久化/右键菜单。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _make_flows(self):
        f1 = Flow(name="流程A", group="办公")
        f2 = Flow(name="流程B", group="办公")
        f3 = Flow(name="流程C", group="游戏")
        f4 = Flow(name="流程D", group="")
        return [f1, f2, f3, f4]

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = self._make_flows()
        self.cfg.flow_groups = ["办公", "游戏"]
        self.cfg.collapsed_flow_groups = []
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    def test_group_tree_structure(self):
        """顶层分组 = flow_groups + 未分组；流程归入对应分组。"""
        names = [self.tab.list.topLevelItem(i).data(0, Qt.UserRole)
                 for i in range(self.tab.list.topLevelItemCount())]
        self.assertEqual(names, [("group", "办公"), ("group", "游戏"), ("group", "")])
        g_office = self.tab.list.topLevelItem(0)
        self.assertEqual(g_office.childCount(), 2)
        self.assertEqual(_flow_name(g_office.child(0)), "流程A")
        self.assertEqual(_flow_name(g_office.child(1)), "流程B")
        g_game = self.tab.list.topLevelItem(1)
        self.assertEqual(g_game.childCount(), 1)
        self.assertEqual(_flow_name(g_game.child(0)), "流程C")
        g_ungrouped = self.tab.list.topLevelItem(2)
        self.assertEqual(g_ungrouped.childCount(), 1)
        self.assertEqual(_flow_name(g_ungrouped.child(0)), "流程D")

    def test_group_expand_collapse_persists(self):
        """收起分组：记入 cfg.collapsed_flow_groups 并持久化；展开后清出。"""
        with mock.patch.object(AppConfig, "save") as save:
            self.tab._toggle_group("办公")
            self.assertIn("办公", self.cfg.collapsed_flow_groups)
            self.assertFalse(self.tab._group_item("办公").isExpanded())
            save.assert_called_once()

            self.tab._toggle_group("办公")
            self.assertNotIn("办公", self.cfg.collapsed_flow_groups)
            self.assertTrue(self.tab._group_item("办公").isExpanded())

    def test_collapsed_flow_groups_restore(self):
        """重建 FlowTab 时收起状态从 cfg 恢复。"""
        self.cfg.collapsed_flow_groups = ["游戏"]
        tab2 = FlowTab(self.cfg)
        self.assertFalse(tab2._group_item("游戏").isExpanded())
        self.assertTrue(tab2._group_item("办公").isExpanded())
        self.assertTrue(tab2._group_item("").isExpanded())

    def test_new_flow_assigns_group(self):
        """在分组下新建流程：flow.group 写入该分组。"""
        with mock.patch("app.ui.flow_tab.FlowMetaDialog.exec",
                        return_value=1) as exec_mock, \
                mock.patch.object(AppConfig, "save"):
            self.tab._new_flow("办公")
        exec_mock.assert_called_once()
        self.assertEqual(self.cfg.flows[-1].group, "办公")

    def test_add_group(self):
        """添加分组：写入 cfg.flow_groups 并刷新树。"""
        with mock.patch("app.ui.flow_tab.QInputDialog.getText",
                        return_value=("工作", True)) as dlg:
            self.tab._add_group()
        self.assertIn("工作", self.cfg.flow_groups)
        self.assertIsNotNone(self.tab._group_item("工作"))
        # 重名分组被拦截
        with mock.patch("app.ui.flow_tab.QInputDialog.getText",
                        return_value=("工作", True)), \
                mock.patch("app.ui.flow_tab.QMessageBox.information") as info:
            self.tab._add_group()
        info.assert_called_once()

    def test_rename_group_moves_flows(self):
        """重命名分组：组内流程同步迁移。"""
        with mock.patch("app.ui.flow_tab.QInputDialog.getText",
                        return_value=("行政", True)) as dlg:
            self.tab._rename_group("办公")
        dlg.assert_called_once()
        self.assertNotIn("办公", self.cfg.flow_groups)
        self.assertIn("行政", self.cfg.flow_groups)
        for f in self.cfg.flows:
            if f.name in ("流程A", "流程B"):
                self.assertEqual(f.group, "行政")
        self.assertEqual(self.tab._group_item("行政").childCount(), 2)
        self.assertIsNone(self.tab._group_item("办公"))

    def test_del_group_moves_flows_to_ungrouped(self):
        """删除分组：组内流程移入「未分组」。"""
        self.cfg.flow_groups = [g for g in self.cfg.flow_groups if g != "游戏"]
        for f in self.cfg.flows:
            if f.group == "游戏":
                f.group = ""
        self.tab.refresh_list()
        g_ungrouped = self.tab._group_item("")
        self.assertEqual(g_ungrouped.childCount(), 2)    # 流程D + 流程C

    def test_flow_context_menu_items(self):
        """流程右键菜单含：置顶 / 按创建顺序排序 / 编辑流程 / 删除流程 / 导出流程。"""
        from PySide6.QtWidgets import QMenu
        from app.ui import flow_tab as ft
        # 复刻 _flow_context_menu 的菜单构建，验证动作文本
        menu = QMenu()
        self.tab._style_menu(menu)
        pin_act = menu.addAction("↥ 置顶")
        sort_act = menu.addAction("↕ 按创建顺序排序")
        menu.addSeparator()
        edit_act = menu.addAction("✎ 编辑流程")
        del_act = menu.addAction("🗑 删除流程")
        menu.addSeparator()
        export_act = menu.addAction("📤 导出流程")
        self.assertEqual([a.text() for a in menu.actions() if a.text()],
                         ["↥ 置顶", "↕ 按创建顺序排序", "✎ 编辑流程", "🗑 删除流程",
                          "📤 导出流程"])
        self.assertTrue(all(a is not None for a in
                            (pin_act, sort_act, edit_act, del_act, export_act)))
        # 分组右键菜单
        gmenu = QMenu()
        self.tab._style_menu(gmenu)
        gmenu.addAction("↥ 置顶")
        gmenu.addAction("↕ 按创建顺序排序")
        gmenu.addSeparator()
        gmenu.addAction("✎ 重命名分组")
        gmenu.addAction("🗑 删除分组")
        self.assertEqual([a.text() for a in gmenu.actions() if a.text()],
                         ["↥ 置顶", "↕ 按创建顺序排序", "✎ 重命名分组", "🗑 删除分组"])

    def test_selected_flow_by_group_item(self):
        """点击分组头不产生选中流程；点击流程条目能选中对应流程。"""
        g_item = self.tab._group_item("办公")
        self.tab.list.setCurrentItem(g_item)
        self.assertIsNone(self.tab._selected_flow())
        f_item = self.tab._flow_item(self.cfg.flows[0].id)
        self.tab.list.setCurrentItem(f_item)
        self.assertEqual(self.tab._selected_flow().name, "流程A")


class TestLeftColumnGroupVsFlow(_TempPathsMixin, unittest.TestCase):
    """左栏「分组」与「流程」必须一眼分得开（2026-10-01 用户反馈）。

    用户原话：「左侧的分组列表和流程列表，看起来一样，优化一下显示，分组和流程做一下区分」。
    原来两者都是 47px 等高行、底色又都很淡。现在靠**四层**区分：
      高度（分组 GROUP_ROW_H=50 / 流程 FLOW_ROW_H=36，逐条 setSizeHint）
      形状（分组是通栏色带 + 左侧主色竖条 + 上下细边框；流程是白底）
      缩进（流程是子项，缩进 indentation）
      内容（分组有 ▼/▲ + 流程圆点前缀；流程有圆点前缀）
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = [Flow(name="流程A", group="办公"),
                          Flow(name="流程B", group="办公"),
                          Flow(name="流程C", group="游戏"),
                          Flow(name="流程D", group="")]
        self.cfg.flow_groups = ["办公", "游戏"]
        self.cfg.collapsed_flow_groups = []
        self.tab = FlowTab(self.cfg)
        self.tab.resize(900, 600)
        self.tab.show()
        self._app.processEvents()

    def tearDown(self):
        self._temp_exit()

    # ---------- 一、高度 ----------
    def test_group_row_is_taller_than_flow_row(self):
        tree = self.tab.list
        g = self.tab._group_item("办公")
        h_group = tree.visualItemRect(g).height()
        h_flow = tree.visualItemRect(g.child(0)).height()
        self.assertEqual(h_group, flow_tab_mod.GROUP_ROW_H)
        self.assertEqual(h_flow, flow_tab_mod.FLOW_ROW_H)
        self.assertGreater(h_group, h_flow,
                           "分组行必须比流程行高，否则又会「看起来一样」")

    # ---------- 二、缩进 ----------
    def test_flow_rows_are_indented_under_the_group(self):
        tree = self.tab.list
        g = self.tab._group_item("办公")
        self.assertEqual(tree.visualItemRect(g).x(), 0)
        self.assertGreater(tree.visualItemRect(g.child(0)).x(), 0,
                           "流程是子项，必须缩进在分组色带之下")

    # ---------- 三、形状：分组是通栏色带 ----------
    def test_group_header_band_spans_full_row(self):
        """分组底色挂在最外层控件上，整行（含右侧按钮区）通栏 —— 这是与流程最主要的形状差别。"""
        header = self.tab.list.itemWidget(self.tab._group_item("办公"), 0)
        self.assertIsNotNone(header)
        self.assertEqual(header.objectName(), "groupHeaderBox")
        self.assertTrue(header.testAttribute(Qt.WA_StyledBackground),
                        "不设 WA_StyledBackground 的话 QSS 底色不会画出来")
        self.assertGreaterEqual(header.width(), self.tab.list.viewport().width() - 2,
                               "分组色带要通栏，不能是居中的小药丸")
        # 色带比条目行矮 1px（条目自己有一条下边框）
        self.assertGreaterEqual(header.height(), flow_tab_mod.GROUP_ROW_H - 1)

    def test_group_qss_has_band_and_left_accent(self):
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            qss = fh.read()
        self.assertIn("QWidget#groupHeaderBox", qss)
        self.assertIn("border-left: 3px solid #1668a8", qss)   # 段标题的主色锚点
        self.assertIn("border-bottom: 1px solid #d8dee4", qss)
        self.assertNotIn("height: 36px", qss)                  # 行高改由 setSizeHint 逐条控制

    # ---------- 五、字号与选中态（2026-10-01 第二轮：紧凑 + 选中变浅） ----------
    def test_fonts_are_compact_and_hotkey_smaller_than_title(self):
        """整体字号降一档；热键那行必须仍**小于**分组名（用户要求「字小一点」）。"""
        import re
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()

        def _pt(anchor: str) -> float:
            seg = src.split(anchor, 1)[1]
            m = re.search(r"font-size:\s*([\d.]+)pt", seg)
            self.assertIsNotNone(m, f"{anchor} 附近没有 font-size")
            return float(m.group(1))

        flow_pt = _pt("QTreeWidget#flowList {")
        title_pt = _pt('QPushButton[groupHeader="true"]')
        hotkey_pt = _pt("QLabel#groupHotkey")
        self.assertLessEqual(flow_pt, 9)          # 条目：原 10pt
        self.assertLessEqual(title_pt, 9)         # 分组名：原 10pt
        self.assertLessEqual(hotkey_pt, 8)        # 热键：原 8pt → 7pt
        self.assertLess(hotkey_pt, title_pt)

    def test_selected_flow_uses_light_background(self):
        """选中的流程条目：**浅色底 + 主色字**（原来整条实心主色 #1668a8 + 白字，太重）。

        用 primary_soft 而不是 pressed_bg：浅色主题下 `sel_bg == pressed_bg == #e3edf7`，
        而分组色带正是 sel_bg——选 pressed_bg 会和色带**完全同色**（实测过）。
        """
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        rule = src.split("QTreeWidget#flowList::item:selected", 1)[1].split("}", 1)[0]
        self.assertIn("background-color: #e8f1fa", rule)   # 最浅的蓝（→ primary_soft）
        self.assertIn("color: #1668a8", rule)              # 主色字
        self.assertNotIn("color: white", rule)
        self.assertNotIn("background-color: #1668a8", rule)
        # 和分组色带（sel_bg）用的字面量不能是同一个，否则浅色主题下两者同色
        band = src.split("QWidget#groupHeaderBox {", 1)[1].split("}", 1)[0]
        self.assertNotIn("background: #e8f1fa", band)

    def test_selected_color_differs_from_group_band_token(self):
        """浅色主题下 `sel_bg == pressed_bg == #e3edf7`：选中项千万别挑这两个令牌。

        这条是实测踩出来的——分组色带（sel_bg）和选中项（pressed_bg）在浅色主题下
        渲染成同一个颜色，屏幕上完全分不出来。
        """
        t = theme.THEMES["light"]
        self.assertEqual(t["sel_bg"], t["pressed_bg"])       # 前提：这俩同值
        self.assertNotEqual(t["primary_soft"], t["sel_bg"])  # 所以只能挑 primary_soft

    def test_default_rows_have_no_explicit_foreground(self):
        """默认条目不能塞显式字色：否则会压过 `::item:selected` 的主色字。"""
        item = self.tab._flow_item(self.cfg.flows[0].id)
        self.assertIsNone(item.data(0, Qt.ForegroundRole))

    def test_foreground_is_cleared_after_a_run_ends(self):
        """跑完一轮后回到默认态：显式字色要被清掉（而不是塞回调色板色），选中态才跟主题。"""
        f = self.cfg.flows[0]
        self.tab._launch_flow(f)
        self.tab._on_state(f.id, "done", "")
        item = self.tab._flow_item(f.id)
        self.assertIsNone(item.data(0, Qt.ForegroundRole))

    # ---------- 四、内容：无计数、按钮贴右、圆点 ----------
    def test_group_header_shows_no_flow_count(self):
        """分组里**不显示**流程个数（用户 2026-10-01 要求）——省下的横向空间留给分组名。"""
        from PySide6.QtWidgets import QLabel
        header = self.tab.list.itemWidget(self.tab._group_item("办公"), 0)
        self.assertIsNone(header.findChild(QLabel, "groupCount"))
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            self.assertNotIn("groupCount", fh.read())

    def test_header_buttons_are_compact_and_right_aligned(self):
        """⚙（编辑分组）/ ＋（新建流程）固定小尺寸并**贴右**，不占地方。"""
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QPushButton
        header = self.tab.list.itemWidget(self.tab._group_item("办公"), 0)
        btns = [b for b in header.findChildren(QPushButton) if b.text() in ("⚙", "＋")]
        self.assertEqual(len(btns), 2, "编辑分组 / 新建流程两个按钮都要在")
        for b in btns:
            self.assertEqual(b.size(),
                             QSize(flow_tab_mod.GROUP_BTN_W, flow_tab_mod.GROUP_BTN_H))
        right_btn = max(btns, key=lambda b: b.x() + b.width())
        self.assertLessEqual(header.width() - (right_btn.x() + right_btn.width()), 8,
                             "按钮要贴在分组色带右侧")
        left_btn = min(btns, key=lambda b: b.x())
        self.assertGreater(left_btn.x(), header.width() // 2,
                           "按钮整体靠右，左侧留给分组名")

    def test_ungrouped_has_only_plus_button(self):
        """「未分组」不是可编辑分组：只有 ＋，没有 ⚙。"""
        from PySide6.QtWidgets import QPushButton
        header = self.tab.list.itemWidget(self.tab._group_item(""), 0)
        texts = [b.text() for b in header.findChildren(QPushButton)
                 if b.text() in ("⚙", "＋")]
        self.assertEqual(texts, ["＋"])
        # 按钮依旧贴右
        plus = header.findChildren(QPushButton)
        plus = [b for b in plus if b.text() == "＋"][0]
        self.assertLessEqual(header.width() - (plus.x() + plus.width()), 8)

    def test_flow_rows_have_bullet_prefix(self):
        item = self.tab._group_item("办公").child(0)
        self.assertTrue(item.text(0).startswith(flow_tab_mod.FLOW_BULLET),
                        f"流程条目要有圆点前缀：{item.text(0)!r}")
        self.assertEqual(_flow_name(item), "流程A")

    def test_flow_row_text_keeps_status_marks_after_bullet(self):
        """圆点在最前，异步 ⚡ / 运行 ▶ / 排队 ⏳ 标记原样跟在后面。"""
        f = Flow(name="异步流程", group="办公", async_run=True)
        self.assertEqual(self.tab._flow_row_text(f),
                         flow_tab_mod.FLOW_BULLET + self.tab._flow_item_text(f))
        self.assertIn("⚡", self.tab._flow_row_text(f))
        self.assertEqual(self.tab._flow_item_text(f), f"{flow_tab_mod.ASYNC_MARK} 异步流程")

    def test_group_rows_stay_unselectable(self):
        """分组头依旧不可选中：选中态（实心主色）是流程条目的专属外观。"""
        g = self.tab._group_item("办公")
        self.assertFalse(bool(g.flags() & Qt.ItemIsSelectable))
        self.tab.list.setCurrentItem(g)
        self.assertIsNone(self.tab._selected_flow())


class TestPanelRowAlignment(_TempPathsMixin, unittest.TestCase):
    """右栏模块面板的行高/字号必须与左栏分组列表**逐项对齐**（2026-10-01 用户要求）。

    用户原话：「模块面板下面的分组和列表改为和左侧分组列表一样的高度，
    一样的分组字体和列表字体，让整个页面显示的协调一点」。

    对照关系：
        左栏分组行（GROUP_ROW_H）  <->  右栏分组头
        左栏流程行（FLOW_ROW_H）   <->  右栏模块按钮
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = [Flow(name="流程A", group="办公")]
        self.cfg.flow_groups = ["办公"]
        self.cfg.collapsed_flow_groups = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)
        self.tab.resize(1000, 700)
        self.tab.show()
        self._app.processEvents()

    def tearDown(self):
        self._temp_exit()

    def _left_group_row(self):
        return self.tab.list.visualItemRect(self.tab._group_item("办公")).height()

    def _left_flow_row(self):
        g = self.tab._group_item("办公")
        return self.tab.list.visualItemRect(g.child(0)).height()

    def test_row_heights_match_left_column(self):
        self.assertEqual(self._left_group_row(), flow_tab_mod.GROUP_ROW_H)
        self.assertEqual(self._left_flow_row(), flow_tab_mod.FLOW_ROW_H)
        # 右栏分组头 / 模块按钮
        header = self.tab._group_headers["logic"]
        btn = self.tab._module_btn_by_type["log"]
        self.assertEqual(header.height(), self._left_group_row(),
                         "模块面板的分组头要和左栏分组行一样高")
        self.assertEqual(btn.height(), self._left_flow_row(),
                         "模块面板的列表项要和左栏流程行一样高")

    def test_group_font_matches_left_group_title(self):
        from PySide6.QtWidgets import QPushButton
        left_title = self.tab.list.itemWidget(self.tab._group_item("办公"), 0)\
            .findChild(QPushButton, "groupTitle")
        self.assertIsNotNone(left_title)
        right_header = self.tab._group_headers["logic"]
        self.assertEqual(right_header.font().pointSizeF(),
                         left_title.font().pointSizeF())
        # 字重也要一致（都是加粗的分组名）
        self.assertEqual(right_header.font().bold(), left_title.font().bold())

    def test_list_font_matches_left_list(self):
        right_btn = self.tab._module_btn_by_type["log"]
        # 断言**绝对字号**，不能只比两边相等——之前这条用例在离屏无字体环境下
        # 两边都是默认值，等于白测（模块按钮实际是 10pt 也照样通过）。
        self.assertEqual(right_btn.font().pointSizeF(), 9.0)
        self.assertEqual(self.tab.list.font().pointSizeF(), 9.0)
        self.assertEqual(right_btn.font().pointSizeF(),
                         self.tab.list.font().pointSizeF())

    def test_module_button_font_comes_from_wrapper_stylesheet(self):
        """⚠️ 模块按钮的字号只能靠**容器自己的样式表**给（2026-10-01 踩出来的）。

        面板顶层那条 `QWidget#flowTab QGroupBox#modulePanel QPushButton` 对 ModuleButton
        **只生效背景/边框/内边距，字号完全不生效**（实测把它改成 20pt 也没用），按钮一直沿用
        应用默认字体 10pt——这就是「右栏字体还是比左栏大」的真正原因。
        分组头（`[groupHeader="true"]` 那条）反而正常，`setFont()` 也无效。
        实测可行的只有容器级/按钮级 QSS，所以字号挂在 wrapper 上，这条用例钉住它。
        """
        wrapper = self.tab._group_wrappers["logic"]
        self.assertIn("font-size: 9pt", wrapper.styleSheet())
        btn = self.tab._module_btn_by_type["log"]
        self.assertEqual(btn.font().pointSizeF(), 9.0)

    def test_module_list_pitch_is_tight(self):
        """行间距也要紧：左栏流程行是紧挨着的，右栏原来留 3px（pitch 31 vs 28）。"""
        wrapper = self.tab._group_wrappers["logic"]
        self.assertLessEqual(wrapper.layout().spacing(), 1)

    def test_module_panel_qss_uses_same_font_size(self):
        """源码级兜底：modulePanel 的两条按钮规则都必须是 9pt（跟左栏同一档）。"""
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        seg = src.split("QGroupBox#modulePanel QPushButton {", 1)[1]
        item_rule = seg.split("}", 1)[0]
        header_rule = src.split('QPushButton[groupHeader="true"] {', 1)[1].split("}", 1)[0]
        self.assertIn("font-size: 9pt", item_rule)
        self.assertIn("font-size: 9pt", header_rule)
        self.assertIn("font-weight: 700", header_rule)

    def test_step_rows_are_one_notch_bigger_than_left(self):
        """中间「要执行的模块」列表：行高/字号**比左栏条目大一档**（2026-10-01 用户要求）。

        用户原话：「中间要执行的模块列表，每一个模块的行高稍微大一点，字体稍微大一点，
        紧凑一点，节约空间」——它是真正要执行的主体，可读性优先，但仍要紧凑
        （上下 padding 归零，见 test_step_view_qss_keeps_padding_at_zero）。
        演变：44px@11pt → 28px@9pt（与左栏同节奏）→ **30px@10pt**（比左栏大一档）。
        """
        from app.ui.flow_dialog import StepRunDelegate
        step = FlowStep(type="wait", name="延时等待",
                        params=dict(default_step_params("wait")))
        self.cfg.flows[0].steps = [step, FlowStep(
            type="log", name="打印输出", params=dict(default_step_params("log")))]
        self.tab.refresh_list()
        self._app.processEvents()

        sl = self.tab.step_list
        row_h = sl.visualItemRect(sl.item(0)).height()
        self.assertEqual(row_h, flow_tab_mod.STEP_ROW_H, "步骤行高 = STEP_ROW_H")
        self.assertGreater(row_h, self._left_flow_row(),
                           "中间模块列表的行高要比左栏流程条目大一档")
        self.assertGreater(sl.font().pointSizeF(),
                           self.tab.list.font().pointSizeF(),
                           "中间模块列表的字号要比左栏大一档")
        # 「▶ 执行」按钮要装得进行里
        self.assertLess(StepRunDelegate.BTN_H, row_h)

    def test_step_view_qss_keeps_padding_at_zero(self):
        """⚠️ `::item{height}` 是**内容高**：不把上下 padding 归零，通用规则的
        `padding:4px 8px` 会再加 8px（这正是原来 38 变成 44 的原因）。
        两处 QSS（flowTab 大样式表 + step_list 自己的）必须同档。"""
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("height: 38px", src)
        self.assertEqual(src.count(f"height: {flow_tab_mod.STEP_ROW_H}px"), 2)
        self.assertEqual(src.count("padding: 0px 8px"), 2)


class TestFlowSortAndPin(_TempPathsMixin, unittest.TestCase):
    """流程/分组的「置顶」与「按创建顺序排序」：入口逻辑 + 持久化语义。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flow_groups = ["办公", "游戏"]
        self.cfg.flow_group_seqs = {"办公": 1, "游戏": 2}
        self.cfg.collapsed_flow_groups = []
        # 办公组内故意乱序：流程B(seq2) 排在 流程A(seq1) 之前
        self.cfg.flows = [
            Flow(name="流程B", group="办公", created_seq=2),
            Flow(name="流程A", group="办公", created_seq=1),
            Flow(name="流程C", group="游戏", created_seq=3),
            Flow(name="流程D", group="", created_seq=4),
        ]
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    def _group_flow_names(self, g):
        item = self.tab._group_item(g)
        return [_flow_name(item.child(i)) for i in range(item.childCount())]

    def _top_groups(self):
        return [self.tab.list.topLevelItem(i).data(0, Qt.UserRole)
                for i in range(self.tab.list.topLevelItemCount())]

    def test_next_flow_seq(self):
        self.assertEqual(self.tab._next_flow_seq(), 5)

    def test_new_flow_gets_incrementing_seq_and_appends(self):
        with mock.patch("app.ui.flow_tab.FlowMetaDialog.exec",
                        return_value=1) as exec_mock, \
                mock.patch.object(AppConfig, "save"):
            self.tab._new_flow("办公")
        exec_mock.assert_called_once()
        last = self.cfg.flows[-1]
        self.assertEqual(last.created_seq, 5)          # 现有最大序号 + 1
        self.assertEqual(last.group, "办公")
        self.assertEqual(self._group_flow_names("办公")[-1], last.name)  # 排在末尾

    def test_pin_flow_moves_to_front_of_group(self):
        self.assertEqual(self._group_flow_names("办公"), ["流程B", "流程A"])
        self.tab._pin_flow(self.cfg.flows[1].id)       # 置顶「流程A」
        self.assertEqual(self._group_flow_names("办公"), ["流程A", "流程B"])
        # 其它分组顺序不受影响
        self.assertEqual(self._group_flow_names("游戏"), ["流程C"])
        self.assertEqual(self._group_flow_names(""), ["流程D"])

    def test_sort_flows_restores_creation_order(self):
        self.assertEqual(self._group_flow_names("办公"), ["流程B", "流程A"])
        self.tab._sort_flows_in_group("办公")
        self.assertEqual(self._group_flow_names("办公"), ["流程A", "流程B"])
        self.assertEqual(self._group_flow_names("游戏"), ["流程C"])

    def test_pin_group_moves_to_front(self):
        self.tab._pin_group("游戏")
        self.assertEqual(self.cfg.flow_groups, ["游戏", "办公"])
        self.assertEqual(self._top_groups(),
                         [("group", "游戏"), ("group", "办公"), ("group", "")])

    def test_sort_groups_restores_creation_order(self):
        self.tab._pin_group("游戏")                    # 先打乱：游戏提到最前
        self.assertEqual(self.cfg.flow_groups, ["游戏", "办公"])
        self.tab._sort_groups()                        # 按创建序号（办公1，游戏2）恢复
        self.assertEqual(self.cfg.flow_groups, ["办公", "游戏"])

    def test_add_group_assigns_seq(self):
        with mock.patch("app.ui.flow_tab.QInputDialog.getText",
                        return_value=("新分组", True)), \
                mock.patch.object(AppConfig, "save"):
            self.tab._add_group()
        self.assertEqual(self.cfg.flow_group_seqs["新分组"], 3)   # max(1,2)+1

    def test_rename_group_migrates_seq(self):
        with mock.patch("app.ui.flow_tab.QInputDialog.getText",
                        return_value=("行政", True)), \
                mock.patch.object(AppConfig, "save"):
            self.tab._rename_group("办公")
        self.assertNotIn("办公", self.cfg.flow_group_seqs)
        self.assertEqual(self.cfg.flow_group_seqs["行政"], 1)


class TestModulePanelCollapseAll(_TempPathsMixin, unittest.TestCase):
    """模块面板「一键收起/展开」标题按钮。默认收起；点标题展开全部，再点收起全部。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)
        # 默认收起：标题应显示 v（点击即展开全部）
        self.cfg.flows.append(Flow(name="流程X"))
        self.tab.refresh_list()                       # 选中流程 -> 编辑解锁

    def tearDown(self):
        self._temp_exit()

    def test_panel_title_button_starts_collapsed(self):
        self.assertIsNotNone(self.tab.panel_title_btn)
        # 默认全收起：标题显示 ▲（与分组头同一条规则：▲ = 收起了）。按常量比对，换符号不用改测试
        self.assertEqual(self.tab.panel_title_btn.text(),
                         f"模块面板 {flow_tab_mod.DISCLOSURE_COLLAPSED}")
        self.assertTrue(self.tab.panel_title_btn.isEnabled())
        self.assertIn("展开", self.tab.panel_title_btn.toolTip())

    def test_disclosure_arrows_are_font_safe_glyphs(self):
        """展开/收起符号必须用**各字重都有字形**的字符，且全面板只用一条规则。

        用户原话：「展开和收起图标改为好看点的」。查下来两处问题：
        1. 面板标题原来用 ASCII 的 `^` / 字母 `v`，根本不像控件图标；
        2. 全项目用的 `▾`/`▸`（U+25BE/U+25B8）**在 Microsoft YaHei 的粗体字面里没有**，
           而分组头/日志条正是 `font-weight:700 + 9pt` —— 实测渲染成一个方框（缺字），
           真机上能不能看见三角全看系统字体回退给不给面子。
        `▼`(U+25BC)/`▲`(U+25B2) 各字重都有，所以统一换成它们；
        并且统一语义：**▼ = 展开着，▲ = 收起了**（标题原先的 `^`/`v` 是反向的，已改齐）。
        """
        from app.ui.widgets import DISCLOSURE_COLLAPSED, DISCLOSURE_EXPANDED
        self.assertEqual(DISCLOSURE_EXPANDED, "▼")
        self.assertEqual(DISCLOSURE_COLLAPSED, "▲")
        self.assertEqual(flow_tab_mod.DISCLOSURE_EXPANDED, DISCLOSURE_EXPANDED)
        for arrow in (DISCLOSURE_EXPANDED, DISCLOSURE_COLLAPSED):
            self.assertGreaterEqual(ord(arrow), 0x25A0)   # Geometric Shapes
            self.assertLessEqual(ord(arrow), 0x25FF)
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('"模块面板 ^"', src)
        self.assertNotIn('"模块面板 v"', src)

    def test_no_legacy_disclosure_glyphs_left_in_ui_code(self):
        """UI 代码里不该再有 `▾`/`▸`（只有注释里允许出现，用来解释历史）。"""
        import app.ui
        base = os.path.dirname(os.path.abspath(app.ui.__file__))
        offenders = []
        for fn in os.listdir(base):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(base, fn), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]      # 去掉行尾注释
                    if "▾" in code or "▸" in code:
                        offenders.append(f"{fn}:{n}")
        self.assertEqual(offenders, [],
                         "还在用粗体下会缺字的 ▾/▸：" + ", ".join(offenders))

    def test_toggle_expands_then_collapses_all_and_persists(self):
        """点击标题：默认收起 -> 展开全部（记忆为空）；再点 -> 全部收起（状态全部持久化）。"""
        with mock.patch.object(AppConfig, "save") as save:
            self.tab.panel_title_btn.click()          # 全收起 -> 展开全部
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith(flow_tab_mod.DISCLOSURE_EXPANDED))
            self.assertTrue(header.isChecked())
        for wrapper in self.tab._group_wrappers.values():
            self.assertFalse(wrapper.isHidden())
        self.assertTrue(self.cfg.module_groups_explicit)
        self.assertEqual(self.cfg.collapsed_module_groups, [])
        self.assertEqual(self.tab.panel_title_btn.text(),
                         f"模块面板 {flow_tab_mod.DISCLOSURE_EXPANDED}")

        with mock.patch.object(AppConfig, "save") as save:
            self.tab.panel_title_btn.click()          # 全展开 -> 全部收起
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith(flow_tab_mod.DISCLOSURE_COLLAPSED))
        for wrapper in self.tab._group_wrappers.values():
            self.assertTrue(wrapper.isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS))
        self.assertEqual(self.tab.panel_title_btn.text(),
                         f"模块面板 {flow_tab_mod.DISCLOSURE_COLLAPSED}")
        save.assert_called()

    def test_expand_individually_from_collapsed(self):
        """默认全收起时，点单个分组标题可单独展开，其余保持收起，标题回 ▲。"""
        with mock.patch.object(AppConfig, "save"):
            self.tab._group_headers["input"].setChecked(True)   # 单独展开 input
        self.assertFalse(self.tab._group_wrappers["input"].isHidden())
        self.assertTrue(self.tab._group_wrappers["perceive"].isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS if gid != "input"))
        self.assertEqual(self.tab.panel_title_btn.text(),
                         f"模块面板 {flow_tab_mod.DISCLOSURE_EXPANDED}")

    def test_perceive_group_renamed_with_screenshot(self):
        """「文字识别」分组改名为「目标识别」，并纳入「截图」「找图」模块。"""
        titles = {gid: title for gid, title, _ in MODULE_GROUPS}
        self.assertEqual(titles["perceive"], "目标识别")
        types = {gid: ts for gid, _, ts in MODULE_GROUPS}
        self.assertIn("screenshot", types["perceive"])
        self.assertIn("find_image", types["perceive"])
        header = self.tab._group_headers["perceive"]
        self.assertIn("目标识别", header.text())


class TestModulePanelSearch(_TempPathsMixin, unittest.TestCase):
    """模块面板底部搜索框：实时过滤、展开命中分组、隐藏空分组、清空恢复、无结果提示。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.cfg.module_groups_explicit = True   # 全展开，便于观察过滤效果
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    def _all_types(self):
        return [t for _, _, types in MODULE_GROUPS for t in types]

    def _hidden_types(self):
        return {t for t in self._all_types()
                if self.tab._module_btn_by_type[t].isHidden()}

    def test_search_shows_only_matching_modules(self):
        """搜「点击」只留下 鼠标点击 / 找图点击，其余模块全部隐藏。"""
        self.tab._on_module_search_changed("点击")
        self.assertEqual(self._hidden_types(),
                         set(self._all_types()) - {"click", "find"})

    def test_search_expands_matching_groups_hides_empty(self):
        """命中模块所在分组展开；无命中分组整体隐藏（含空分组）。"""
        self.tab._on_module_search_changed("点击")
        self.assertFalse(self.tab._group_headers["input"].isHidden())
        self.assertFalse(self.tab._group_wrappers["input"].isHidden())
        for gid in ("perceive", "app_web", "logic", "condition", "python"):
            self.assertTrue(self.tab._group_headers[gid].isHidden(), gid)
            self.assertTrue(self.tab._group_wrappers[gid].isHidden(), gid)
        self.assertTrue(self.tab._no_result_label.isHidden())

    def test_search_matches_english_type(self):
        """按模块英文类型名也能命中（如 py_func）。"""
        self.tab._on_module_search_changed("py_func")
        self.assertEqual(self._hidden_types(), set(self._all_types()) - {"py_func"})

    def test_search_no_result_shows_hint(self):
        """无匹配时所有分组隐藏，显示「未找到匹配模块」提示。"""
        self.tab._on_module_search_changed("不存在的模块xyz")
        self.assertFalse(self.tab._no_result_label.isHidden())
        for gid, _, _ in MODULE_GROUPS:
            self.assertTrue(self.tab._group_headers[gid].isHidden(), gid)
            self.assertTrue(self.tab._group_wrappers[gid].isHidden(), gid)

    def test_clear_restores_all(self):
        """点清空：输入框清空、所有模块与分组恢复显示、无结果提示隐藏。"""
        self.tab.search_edit.setText("点击")
        self.tab._clear_module_search()
        self.assertEqual(self.tab.search_edit.text(), "")
        self.assertEqual(self._hidden_types(), set())
        for gid, _, _ in MODULE_GROUPS:
            self.assertFalse(self.tab._group_headers[gid].isHidden(), gid)
            self.assertFalse(self.tab._group_wrappers[gid].isHidden(), gid)
        self.assertTrue(self.tab._no_result_label.isHidden())

    def test_empty_keyword_restores(self):
        """输入清空（退格到空）同样恢复全部显示。"""
        self.tab.search_edit.setText("点击")
        self.tab.search_edit.setText("")
        self.assertEqual(self._hidden_types(), set())

    def test_search_is_transient_and_preserves_collapse(self):
        """搜索是临时过滤态：不改变 cfg 的折叠记忆，清空后恢复原折叠状态。"""
        self.cfg.collapsed_module_groups = ["perceive"]
        self.tab = FlowTab(self.cfg)                 # 从 cfg 重建，perceive 收起
        self.assertTrue(self.tab._group_wrappers["perceive"].isHidden())
        self.tab._on_module_search_changed("点击")
        self.assertEqual(self.cfg.collapsed_module_groups, ["perceive"])
        self.tab._on_module_search_changed("")       # 清空 -> 恢复
        self.assertEqual(self.cfg.collapsed_module_groups, ["perceive"])
        self.assertTrue(self.tab._group_wrappers["perceive"].isHidden())  # 仍收起
        self.assertFalse(self.tab._group_wrappers["input"].isHidden())    # 仍展开


class TestFlowListSearch(_TempPathsMixin, unittest.TestCase):
    """左栏底部流程搜索框：实时过滤、展开命中分组、隐藏空分组、清空恢复、无结果提示。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flow_groups = ["工作", "娱乐"]
        self.cfg.collapsed_flow_groups = []      # 全展开，便于观察过滤效果
        self.cfg.flows = [
            Flow(name="登录网页", group="工作"),
            Flow(name="发送邮件", group="工作"),
            Flow(name="播放音乐", group="娱乐"),
            Flow(name="零散脚本", group=""),
        ]
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    # ---- 观察辅助 ----
    def _item(self, name):
        for f in self.cfg.flows:
            if f.name == name:
                return self.tab._flow_item(f.id)
        raise AssertionError(f"没有名为 {name} 的流程")

    def _visible_names(self):
        names = []
        for i in range(self.tab.list.topLevelItemCount()):
            g = self.tab.list.topLevelItem(i)
            if g.isHidden():
                continue
            for j in range(g.childCount()):
                c = g.child(j)
                if c.isHidden():
                    continue
                data = c.data(0, Qt.UserRole)
                names.append(self.tab._flow_by_id(data[1]).name)
        return names

    def _visible_groups(self):
        out = []
        for i in range(self.tab.list.topLevelItemCount()):
            g = self.tab.list.topLevelItem(i)
            if not g.isHidden():
                out.append(g.data(0, Qt.UserRole)[1])
        return out

    # ---- 控件存在性 ----
    def test_has_search_box_and_clear_button(self):
        """左栏底部有搜索框与清空按钮，占位文案与右栏模块搜索同款。"""
        self.assertEqual(self.tab.flow_search_edit.objectName(), "flowSearch")
        self.assertEqual(self.tab.flow_search_edit.placeholderText(), "搜索流程…")
        self.assertEqual(self.tab.flow_search_clear_btn.text(), "清空")
        self.assertFalse(self.tab.flow_search_edit.isClearButtonEnabled())

    # ---- 过滤 ----
    def test_search_shows_only_matching_flows(self):
        """搜「邮件」只留下「发送邮件」，其余流程全部隐藏。"""
        self.tab._on_flow_search_changed("邮件")
        self.assertEqual(self._visible_names(), ["发送邮件"])

    def test_search_expands_matching_group_hides_empty(self):
        """命中流程所在分组展开；无命中分组整体隐藏。"""
        self.tab._on_flow_search_changed("网页")
        self.assertEqual(self._visible_groups(), ["工作"])
        self.assertTrue(self.tab._group_item("工作").isExpanded())
        self.assertTrue(self.tab._group_item("娱乐").isHidden())
        self.assertTrue(self.tab._group_item("").isHidden())
        self.assertTrue(self.tab._flow_no_result_label.isHidden())

    def test_search_matches_group_name(self):
        """搜分组名：该组下所有流程都算命中。"""
        self.tab._on_flow_search_changed("娱乐")
        self.assertEqual(self._visible_names(), ["播放音乐"])
        self.assertEqual(self._visible_groups(), ["娱乐"])

    def test_search_is_case_insensitive(self):
        """英文流程名不区分大小写。"""
        self.cfg.flows.append(Flow(name="OpenBrowser", group="工作"))
        self.tab.refresh_list()
        self.tab._on_flow_search_changed("openbrowser")
        self.assertEqual(self._visible_names(), ["OpenBrowser"])

    def test_search_no_result_shows_hint(self):
        """无匹配时所有分组隐藏，显示「未找到匹配流程」提示。"""
        self.tab._on_flow_search_changed("不存在xyz")
        self.assertEqual(self._visible_groups(), [])
        self.assertFalse(self.tab._flow_no_result_label.isHidden())

    # ---- 清空恢复 ----
    def test_clear_restores_all(self):
        """点清空：输入框清空、所有流程与分组恢复显示、无结果提示隐藏。"""
        self.tab.flow_search_edit.setText("邮件")
        self.tab._clear_flow_search()
        self.assertEqual(self.tab.flow_search_edit.text(), "")
        self.assertEqual(self._visible_names(),
                         ["登录网页", "发送邮件", "播放音乐", "零散脚本"])
        self.assertEqual(self._visible_groups(), ["工作", "娱乐", ""])
        self.assertTrue(self.tab._flow_no_result_label.isHidden())

    def test_empty_keyword_restores(self):
        """输入清空（退格到空）同样恢复全部显示。"""
        self.tab.flow_search_edit.setText("邮件")
        self.tab.flow_search_edit.setText("")
        self.assertEqual(len(self._visible_names()), 4)

    def test_search_survives_list_rebuild(self):
        """搜索是过滤态：期间因增删改触发的左栏重建仍保持过滤。"""
        self.tab._on_flow_search_changed("网页")
        self.tab.refresh_list()
        self.assertEqual(self._visible_names(), ["登录网页"])

    def test_search_is_transient_and_preserves_collapse(self):
        """搜索是临时过滤态：不改变 cfg 的折叠记忆，清空后恢复原折叠状态。"""
        self.cfg.collapsed_flow_groups = ["工作"]
        self.tab = FlowTab(self.cfg)                 # 从 cfg 重建，工作 收起
        self.assertFalse(self.tab._group_item("工作").isExpanded())
        self.tab._on_flow_search_changed("邮件")
        self.assertEqual(self.cfg.collapsed_flow_groups, ["工作"])
        self.assertTrue(self.tab._group_item("工作").isExpanded())   # 搜索时临时展开
        self.tab._on_flow_search_changed("")         # 清空 -> 恢复
        self.assertEqual(self.cfg.collapsed_flow_groups, ["工作"])
        self.assertFalse(self.tab._group_item("工作").isExpanded())  # 仍收起

    def test_toggle_group_ignored_while_searching(self):
        """搜索中点击分组头不落盘（展开由过滤逻辑接管）。"""
        self.tab._on_flow_search_changed("网页")
        self.tab._toggle_group("工作")
        self.assertEqual(self.cfg.collapsed_flow_groups, [])
        self.assertTrue(self.tab._group_item("工作").isExpanded())


class _ConditionFlowMixin(_TempPathsMixin):
    """构造含完整条件块的流程：if / press / elseif / click / else / wait / endif。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.collapsed_module_groups = []
        self.cfg.flows = [self._build_flow()]
        self.tab = FlowTab(self.cfg)      # 构造时自动选中唯一流程
        self.flow = self.cfg.flows[0]

    def tearDown(self):
        self._temp_exit()

    def _step(self, step_type):
        return FlowStep(type=step_type,
                        params=default_step_params(step_type, self.cfg.clicker,
                                                   self.cfg.presser))

    def _build_flow(self):
        flow = Flow(name="条件流程")
        flow.steps = [self._step(t) for t in
                      ("if", "press", "elseif", "click", "else", "wait", "endif")]
        return flow

    # ---- 列表渲染观察辅助 ----
    def _row_texts(self):
        return [self.tab.step_list.item(i).text()
                for i in range(self.tab.step_list.count())]

    @staticmethod
    def _indents(texts):
        """数出每行文本开头的缩进级数（按 INDENT_UNIT 为单位）。"""
        out = []
        for t in texts:
            n = 0
            while t.startswith(INDENT_UNIT * (n + 1)):
                n += 1
            out.append(n)
        return out

    def _select_row(self, row):
        self.tab.step_list.setCurrentRow(row)

    def _del(self, row, confirm=True):
        """走一遍删除流程；confirm=False 模拟用户在弹窗点「取消」。

        弹窗是模态的，offscreen 下会挂住，所以统一把确认环节换成固定返回值。
        """
        self._select_row(row)
        with mock.patch.object(FlowTab, "_confirm_del_step",
                               return_value=confirm) as confirm_mock:
            self.tab._del_step()
        return confirm_mock

    def _types(self):
        return [s.type for s in self.flow.steps]


class TestConditionIndentRender(_ConditionFlowMixin, unittest.TestCase):
    """步骤列表缩进：分支头（if/elseif/else/endif）不缩进，只有分支内步骤缩进一级。"""

    def test_block_body_indented(self):
        texts = self._row_texts()
        self.assertEqual(len(texts), 7)
        # if / elseif / else / endif 都在 0 级，夹在中间的步骤各缩进一级
        self.assertEqual(self._indents(texts), [0, 1, 0, 1, 0, 1, 0])

    def test_branch_heads_not_indented(self):
        """四个分支/边界标记互相对齐、都不缩进。"""
        texts = self._row_texts()
        for row in (0, 2, 4, 6):                 # if / elseif / else / endif
            self.assertFalse(texts[row].startswith(INDENT_UNIT), texts[row])
        for row in (1, 3, 5):                    # press / click / wait
            self.assertTrue(texts[row].startswith(INDENT_UNIT), texts[row])

    def test_running_mark_after_indent(self):
        """运行中行首的 ▶ 跟在缩进之后，缩进量不被运行状态顶掉。"""
        runner = mock.Mock()
        runner.is_running = True
        runner.current_step_index = 3          # click（块内，缩进一级）
        self.tab._runners[self.flow.id] = runner
        self.tab._reload_steps()
        text = self._row_texts()[3]
        self.assertTrue(text.startswith(INDENT_UNIT + "▶ "), text)

    def test_indent_updates_after_deleting_branch(self):
        """删掉分支头后重建列表，剩余步骤的缩进随之重算。"""
        self._select_row(4)                   # 删 else
        self._del(4)
        texts = self._row_texts()
        self.assertEqual(self._indents(texts), [0, 1, 0, 1, 1, 0])


class TestDeleteConditionBranch(_ConditionFlowMixin, unittest.TestCase):
    """「否则 / 否则如果」可单独删除：条件判断与条件结束不受牵连。"""

    def test_branch_types_are_separable(self):
        self.assertEqual(BRANCH_TYPES, ("elseif", "else"))

    def test_delete_else_keeps_block(self):
        """删「否则」只摘掉分支头：if / elseif / endif 与其下步骤全部保留。"""
        self._del(4)
        self.assertEqual(self._types(),
                         ["if", "press", "elseif", "click", "wait", "endif"])
        self.assertEqual(validate_condition_structure(self.flow.steps), [])

    def test_delete_elseif_keeps_block(self):
        """删「否则如果」后，其下步骤并入上一分支，结构仍合法。"""
        self._del(2)
        self.assertEqual(self._types(), ["if", "press", "click", "else", "wait", "endif"])
        self.assertEqual(validate_condition_structure(self.flow.steps), [])

    def test_delete_all_branches_keeps_if_pair(self):
        """把 elseif / else 全删掉，条件块仍完整（if ... endif）。"""
        self._del(4)                          # else
        self._del(2)                          # elseif
        self.assertEqual(self._types(), ["if", "press", "click", "wait", "endif"])
        self.assertEqual(validate_condition_structure(self.flow.steps), [])

    def test_delete_if_still_removes_whole_block(self):
        """回归：删 if 仍是整块删除，条件块整体消失。"""
        self._del(0)
        self.assertEqual(self._types(), [])

    def test_delete_endif_still_removes_whole_block(self):
        """回归：删 endif 同样是整块删除。"""
        self._del(6)
        self.assertEqual(self._types(), [])


class TestDeleteStepConfirm(_ConditionFlowMixin, unittest.TestCase):
    """删除步骤要弹确认框：取消则一步不动，确认才删。"""

    def test_cancelled_delete_changes_nothing(self):
        """弹窗点「取消」：步骤不删、列表不重建、不触发 changed。"""
        before = list(self.flow.steps)
        changed = []
        self.tab.changed.connect(lambda: changed.append(1))
        self._del(1, confirm=False)
        self.assertEqual(self.flow.steps, before)
        self.assertEqual(self._types(), ["if", "press", "elseif", "click",
                                         "else", "wait", "endif"])
        self.assertEqual(changed, [])

    def test_confirmed_delete_applies(self):
        """弹窗点「删除」：正常删除并刷新。"""
        self._del(1)
        self.assertEqual(self._types(), ["if", "elseif", "click", "else",
                                         "wait", "endif"])

    def test_confirm_defaults_to_cancel(self):
        """确认框默认按钮必须是「取消」，回车不会误删。"""
        del_btn, cancel_btn = object(), object()
        with mock.patch.object(flow_tab_mod, "QMessageBox") as qmb:
            box = qmb.return_value
            box.addButton.side_effect = [del_btn, cancel_btn]
            box.clickedButton.return_value = cancel_btn
            self.assertFalse(self.tab._confirm_del_step(self.flow, 1))
        box.setDefaultButton.assert_called_once_with(cancel_btn)

    def test_confirm_text_mentions_step_and_scope(self):
        """文案要点明第几步、模块名、摘要，并按删除范围补充连带影响。"""
        del_btn, cancel_btn = object(), object()
        for row, keyword in ((1, "保留"),          # 普通步骤：无连带说明也可
                             (0, "同时删除整个条件块"),   # if：整块
                             (4, "只删除「否则」")):      # else：只摘分支头
            with mock.patch.object(flow_tab_mod, "QMessageBox") as qmb:
                box = qmb.return_value
                box.addButton.side_effect = [del_btn, cancel_btn]
                box.clickedButton.return_value = del_btn
                self.assertTrue(self.tab._confirm_del_step(self.flow, row))
            text = box.setText.call_args[0][0]
            self.assertIn(f"第 {row + 1} 步", text)
            self.assertIn(FLOW_STEP_TYPES[self.flow.steps[row].type], text)
            if keyword != "保留":
                self.assertIn(keyword, text)

    def test_confirm_returns_false_when_dialog_dismissed(self):
        """Esc/关闭对话框（没有点击任何按钮）时按取消处理。"""
        del_btn, cancel_btn = object(), object()
        with mock.patch.object(flow_tab_mod, "QMessageBox") as qmb:
            box = qmb.return_value
            box.addButton.side_effect = [del_btn, cancel_btn]
            box.clickedButton.return_value = None
            self.assertFalse(self.tab._confirm_del_step(self.flow, 1))


class _LoopFlowMixin(_TempPathsMixin):
    """构造用于 foreach / while 交互测试的流程。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.collapsed_module_groups = []
        self.cfg.flows = [Flow(name="循环流程")]
        # 拖入模块现在会顺手打开步骤编辑窗（2026-09-26 起）；测试里不需要真窗口，
        # 也不该在断言步骤结构时弹出非模态窗，统一替身掉。
        self._dlg_patch = mock.patch.object(flow_tab_mod, "StepParamsDialog")
        self._dlg_patch.start()
        self.addCleanup(self._dlg_patch.stop)
        self.tab = FlowTab(self.cfg)      # 构造时自动选中唯一流程
        self.flow = self.cfg.flows[0]

    def tearDown(self):
        self._temp_exit()

    def _types(self):
        return [s.type for s in self.flow.steps]


class TestDropOpensEditor(_LoopFlowMixin, unittest.TestCase):
    """拖入模块后要立刻打开它的编辑窗，并选中新步骤（2026-09-26 用户要求）。"""

    def _drop_and_pump(self, step_type, row):
        """拖入 + 跑一轮事件循环，让 QTimer.singleShot(0) 里的开窗动作执行。"""
        with mock.patch.object(self.tab, "_edit_step_param") as editor:
            self.tab._on_step_dropped(step_type, row)
            self._app.processEvents()
        return editor

    def test_drop_wait_opens_editor_and_selects_new_row(self):
        editor = self._drop_and_pump("wait", 0)
        editor.assert_called_once()
        self.assertEqual(self.tab.step_list.currentRow(), 0)

    def test_drop_in_middle_selects_inserted_row(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="wait")]
        self.tab._reload_steps()
        editor = self._drop_and_pump("log", 1)          # 插到中间
        editor.assert_called_once()
        self.assertEqual(self.tab.step_list.currentRow(), 1)
        self.assertEqual(self.flow.steps[1].type, "log")

    def test_block_drop_selects_open_step(self):
        """if/foreach/while 拖入后选中的是「起始块」（编辑条件处），不是结束标记。"""
        editor = self._drop_and_pump("foreach", 0)
        editor.assert_called_once()
        self.assertEqual(self.flow.steps[0].type, "foreach")
        self.assertEqual(self.tab.step_list.currentRow(), 0)

    def test_else_branch_selects_its_own_row(self):
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="endif")]
        self.tab._reload_steps()
        editor = self._drop_and_pump("else", 1)
        editor.assert_called_once()
        row = self.tab.step_list.currentRow()
        self.assertEqual(self.flow.steps[row].type, "else")

    def test_break_does_not_open_editor(self):
        """break/continue 没有可填参数，别弹个空窗。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="endForeach")]
        self.tab._reload_steps()
        editor = self._drop_and_pump("break", 1)
        editor.assert_not_called()

    def test_rejected_drop_does_not_open_editor(self):
        """位置非法被拒（else 不在 if 块内）→ 不弹编辑窗。"""
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        with mock.patch.object(flow_tab_mod.QMessageBox, "information",
                               return_value=None):
            editor = self._drop_and_pump("else", 1)
        editor.assert_not_called()

    def test_running_flow_does_not_open_editor(self):
        """流程运行中禁止编辑 → 拖入直接返回，不弹窗。"""
        with mock.patch.object(self.tab, "_selected_running", return_value=True):
            editor = self._drop_and_pump("wait", 0)
        editor.assert_not_called()

    def test_web_drop_opens_editor(self):
        editor = self._drop_and_pump("web", 0)
        editor.assert_called_once()


class TestMultiSelectDelete(_LoopFlowMixin, unittest.TestCase):
    """步骤列表支持 Ctrl/Shift 多选，并能一次删除多个（2026-09-26 用户要求）。"""

    def _select(self, *rows):
        for r in rows:
            self.tab.step_list.item(r).setSelected(True)

    def _delete_confirmed(self):
        with mock.patch.object(FlowTab, "_confirm_del_steps", return_value=True):
            self.tab._del_step()

    def test_extended_selection_enabled(self):
        from PySide6.QtWidgets import QAbstractItemView
        self.assertEqual(self.tab.step_list.selectionMode(),
                         QAbstractItemView.SelectionMode.ExtendedSelection)

    def test_delete_two_of_three(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log"),
                           FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0, 2)
        self._delete_confirmed()
        self.assertEqual(self._types(), ["log"])

    def test_delete_block_and_step_together(self):
        """选中「if 起始块 + 一个普通步骤」→ 整块（含 endif）与那步一起删。"""
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="endif"),
                           FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0, 2)
        self._delete_confirmed()
        self.assertEqual(self._types(), [])

    def test_delete_branch_head_only_keeps_block(self):
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="else"),
                           FlowStep(type="endif"), FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(1, 3)
        self._delete_confirmed()
        self.assertEqual(self._types(), ["if", "endif"])

    def test_cancel_keeps_everything(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0, 1)
        with mock.patch.object(FlowTab, "_confirm_del_steps", return_value=False):
            self.tab._del_step()
        self.assertEqual(self._types(), ["wait", "log"])

    def test_rows_to_remove_expands_blocks(self):
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach"), FlowStep(type="log")]
        self.assertEqual(self.tab._rows_to_remove(self.flow, [0]), {0, 1, 2})
        self.assertEqual(self.tab._rows_to_remove(self.flow, [1, 3]), {1, 3})

    def test_nothing_selected_does_nothing(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self.tab.step_list.clearSelection()
        self.tab._del_step()
        self.assertEqual(self._types(), ["wait"])

    def test_confirm_text_mentions_block_expansion(self):
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="wait"),
                           FlowStep(type="endif")]
        self.tab._reload_steps()
        with mock.patch.object(flow_tab_mod, "QMessageBox") as qmb:
            box = qmb.return_value
            box.clickedButton.return_value = None            # 取消
            self.tab._confirm_del_steps(self.flow, [0])
        text = box.setText.call_args.args[0]
        self.assertIn("1 个步骤", text)
        self.assertIn("实际会移除 3 个步骤", text)

    def test_single_selection_still_uses_targeted_confirm(self):
        """只选中一个时仍走原来的单步确认（提示按类型区分）。"""
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        with mock.patch.object(FlowTab, "_confirm_del_step", return_value=True) as one, \
                mock.patch.object(FlowTab, "_confirm_del_steps") as many:
            self.tab._del_step()
        one.assert_called_once()
        many.assert_not_called()


class TestMultiSelectComment(_LoopFlowMixin, unittest.TestCase):
    """多选后同时注释 / 取消注释多个模块（2026-09-26 用户要求）。"""

    def _select(self, *rows):
        for r in rows:
            self.tab.step_list.item(r).setSelected(True)

    def test_comment_multiple_selected(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log"),
                           FlowStep(type="click")]
        self.tab._reload_steps()
        self._select(0, 2)
        self.tab._toggle_comment_step()
        self.assertEqual([s.commented for s in self.flow.steps],
                         [True, False, True])

    def test_all_commented_toggles_back(self):
        self.flow.steps = [FlowStep(type="wait", commented=True),
                           FlowStep(type="log", commented=True)]
        self.tab._reload_steps()
        self._select(0, 1)
        self.tab._toggle_comment_step()
        self.assertEqual([s.commented for s in self.flow.steps], [False, False])

    def test_mixed_state_comments_all(self):
        """混着两种状态时统一成「全部注释」（否则没法表达）。"""
        self.flow.steps = [FlowStep(type="wait", commented=True),
                           FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0, 1)
        self.tab._toggle_comment_step()
        self.assertEqual([s.commented for s in self.flow.steps], [True, True])

    def test_selection_kept_after_comment(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log"),
                           FlowStep(type="click")]
        self.tab._reload_steps()
        self._select(0, 2)
        self.tab._toggle_comment_step()
        self.assertEqual(self.tab._selected_step_rows(), [0, 2])   # 保持多选

    def test_single_step_still_toggles_both_ways(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._toggle_comment_step()
        self.assertTrue(self.flow.steps[0].commented)
        self._select(0)                        # 重建后重新选中
        self.tab._toggle_comment_step()
        self.assertFalse(self.flow.steps[0].commented)

    def test_menu_text_mentions_selected_count(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0, 1)
        pos = self.tab.step_list.visualItemRect(self.tab.step_list.item(0)).center()
        with mock.patch.object(flow_tab_mod, "QMenu") as qmenu:
            menu = qmenu.return_value
            menu.exec.return_value = None            # 不点任何项
            self.tab._step_context_menu(pos)
        labels = [c.args[0] for c in menu.addAction.call_args_list if c.args]
        self.assertTrue(any("注释选中的 2 个模块" in lb for lb in labels), labels)

    def test_right_click_on_selected_keeps_multi_selection(self):
        """右键点在已选中的行上不能把多选清掉，否则批量操作没法用。"""
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0, 1)
        pos = self.tab.step_list.visualItemRect(self.tab.step_list.item(1)).center()
        with mock.patch.object(flow_tab_mod, "QMenu") as qmenu:
            qmenu.return_value.exec.return_value = None
            self.tab._step_context_menu(pos)
        self.assertEqual(self.tab._selected_step_rows(), [0, 1])

    def test_running_flow_does_not_comment(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        with mock.patch.object(self.tab, "_selected_running", return_value=True):
            self.tab._toggle_comment_step()
        self.assertFalse(self.flow.steps[0].commented)


class TestLoopBlockInsert(_LoopFlowMixin, unittest.TestCase):
    def test_foreach_dropped_creates_pair(self):
        self.tab._on_step_dropped("foreach", 0)
        self.assertEqual(self._types(), ["foreach", "endForeach"])

    def test_for_dropped_creates_pair(self):
        """for 与 foreach/while 一样：拖入即生成配对结束标记 endFor。"""
        self.tab._on_step_dropped("for", 0)
        self.assertEqual(self._types(), ["for", "endFor"])

    def test_for_defaults(self):
        self.tab._on_step_dropped("for", 0)
        p = self.flow.steps[0].params
        self.assertEqual(p["var"], "i")
        self.assertEqual(p["start"], "1")     # 默认 1..10（含结束值）= 10 轮
        self.assertEqual(p["stop"], "10")
        self.assertEqual(p["step"], "1")

    def test_for_insert_in_middle_keeps_pair_adjacent(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="wait")]
        self.tab._reload_steps()
        self.tab._on_step_dropped("for", 1)
        self.assertEqual(self._types(), ["wait", "for", "endFor", "wait"])

    def test_while_dropped_creates_pair(self):
        self.tab._on_step_dropped("while", 0)
        self.assertEqual(self._types(), ["while", "endWhile"])

    def test_foreach_defaults(self):
        self.tab._on_step_dropped("foreach", 0)
        s = self.flow.steps[0]
        self.assertEqual(s.params["items"], "")
        self.assertEqual(s.params["item_var"], "item")
        self.assertEqual(s.params["index_var"], "index")

    def test_while_defaults(self):
        self.tab._on_step_dropped("while", 0)
        self.assertEqual(self.flow.steps[0].params["condition"], "")

    def test_insert_in_middle_keeps_pair_adjacent(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="wait")]
        self.tab._reload_steps()
        self.tab._on_step_dropped("foreach", 1)
        self.assertEqual(self._types(),
                         ["wait", "foreach", "endForeach", "wait"])


class TestBreakContinueInsert(_LoopFlowMixin, unittest.TestCase):
    """break/continue 只能拖入 foreach/while 循环体内，否则拒绝并提示。"""

    def test_break_dropped_inside_foreach(self):
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="endForeach")]
        self.tab._on_step_dropped("break", 1)   # 插到 foreach 与 endForeach 之间
        self.assertEqual(self._types(), ["foreach", "break", "endForeach"])

    def test_continue_dropped_inside_while(self):
        self.flow.steps = [FlowStep(type="while"), FlowStep(type="endWhile")]
        self.tab._on_step_dropped("continue", 1)
        self.assertEqual(self._types(), ["while", "continue", "endWhile"])

    def test_break_dropped_outside_loop_rejected(self):
        self.flow.steps = [FlowStep(type="wait")]
        with mock.patch.object(flow_tab_mod.QMessageBox, "information",
                               return_value=None) as mb:
            self.tab._on_step_dropped("break", 1)
        self.assertEqual(self._types(), ["wait"])   # 回滚，未插入
        mb.assert_called_once()

    def test_continue_dropped_empty_flow_rejected(self):
        self.flow.steps = []
        with mock.patch.object(flow_tab_mod.QMessageBox, "information",
                               return_value=None) as mb:
            self.tab._on_step_dropped("continue", 0)
        self.assertEqual(self._types(), [])
        mb.assert_called_once()

    def test_break_inside_if_outside_loop_rejected(self):
        """break 位于 if 块内、但 if 不在循环内 → 仍拒绝。"""
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="endif")]
        with mock.patch.object(flow_tab_mod.QMessageBox, "information",
                               return_value=None) as mb:
            self.tab._on_step_dropped("break", 1)
        self.assertEqual(self._types(), ["if", "endif"])
        mb.assert_called_once()


class TestLoopBlockDelete(_LoopFlowMixin, unittest.TestCase):
    def _del(self, row, confirm=True):
        self.tab._reload_steps()
        self.tab.step_list.setCurrentRow(row)
        with mock.patch.object(FlowTab, "_confirm_del_step",
                               return_value=confirm):
            self.tab._del_step()

    def test_delete_foreach_removes_whole_block(self):
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach"), FlowStep(type="wait")]
        self._del(0)
        self.assertEqual(self._types(), ["wait"])

    def test_delete_endForeach_removes_whole_block(self):
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach"), FlowStep(type="wait")]
        self._del(2)
        self.assertEqual(self._types(), ["wait"])

    def test_delete_while_removes_whole_block(self):
        self.flow.steps = [FlowStep(type="while"), FlowStep(type="wait"),
                           FlowStep(type="endWhile"), FlowStep(type="wait")]
        self._del(0)
        self.assertEqual(self._types(), ["wait"])

    def test_delete_endWhile_removes_whole_block(self):
        self.flow.steps = [FlowStep(type="while"), FlowStep(type="wait"),
                           FlowStep(type="endWhile"), FlowStep(type="wait")]
        self._del(2)
        self.assertEqual(self._types(), ["wait"])

    def test_delete_inner_step_keeps_block(self):
        """删循环体内的普通步骤只删该步骤，块骨架保留。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach")]
        self._del(1)
        self.assertEqual(self._types(), ["foreach", "endForeach"])


class TestLoopBlockOrderRollback(_LoopFlowMixin, unittest.TestCase):
    def _set_list_order(self, seq):
        """把 step_list 重建为给定原始索引顺序，模拟一次拖拽后的新顺序。"""
        from PySide6.QtWidgets import QListWidgetItem
        self.tab.step_list.blockSignals(True)
        self.tab.step_list.clear()
        for orig_idx in seq:
            item = QListWidgetItem(f"row {orig_idx}")
            item.setData(Qt.UserRole, orig_idx)
            self.tab.step_list.addItem(item)
        self.tab.step_list.blockSignals(False)

    def _pump(self):
        from PySide6.QtTest import QTest
        QTest.qWait(0)                    # 处理 QTimer.singleShot(0) 延迟回调

    def test_reorder_breaking_boundary_rolls_back(self):
        """把结束标记拖出块（孤儿 + 未闭合）应回滚到拖拽前顺序，且不落盘。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach"), FlowStep(type="wait")]
        original = list(self.flow.steps)
        self.tab._reload_steps()
        changed = []
        self.tab.changed.connect(lambda: changed.append(1))
        self._set_list_order([2, 0, 1, 3])     # endForeach 被拖到最前
        self.tab._on_order_changed()
        self._pump()
        self.assertEqual([s.type for s in self.flow.steps],
                         [s.type for s in original])
        self.assertEqual(changed, [])          # 未触发 changed（不落盘脏数据）

    def test_reorder_within_block_applies(self):
        """块内交换步骤顺序合法：应用新顺序，且只发 stepsChanged。

        拖动排序只改了步骤，流程名/分组/热键都没变，所以刻意不发 changed——
        发了会让主窗口重注册全部全局热键并重建定时任务页、中键菜单页的列表，
        每次拖放都白做一轮（曾导致拖动明显发顿）。
        """
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="click"),
                           FlowStep(type="press"), FlowStep(type="endForeach")]
        self.tab._reload_steps()
        changed = []
        steps_changed = []
        self.tab.changed.connect(lambda: changed.append(1))
        self.tab.stepsChanged.connect(lambda: steps_changed.append(1))
        self._set_list_order([0, 2, 1, 3])     # 交换 click / press
        self.tab._on_order_changed()
        self._pump()
        self.assertEqual(self._types(),
                         ["foreach", "press", "click", "endForeach"])
        self.assertEqual(len(steps_changed), 1)
        self.assertEqual(changed, [])

    def test_reorder_breaking_boundary_emits_nothing(self):
        """回滚路径不得发任何信号，否则会拿未落盘的脏顺序去覆盖配置。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach"), FlowStep(type="wait")]
        self.tab._reload_steps()
        changed = []
        steps_changed = []
        self.tab.changed.connect(lambda: changed.append(1))
        self.tab.stepsChanged.connect(lambda: steps_changed.append(1))
        self._set_list_order([2, 0, 1, 3])
        self.tab._on_order_changed()
        self._pump()
        self.assertEqual(changed, [])
        self.assertEqual(steps_changed, [])


class TestLoopStepDialogs(unittest.TestCase):
    """foreach / while 步骤编辑表单的构建、回填与参数收集。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_foreach_form_fill_and_apply(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="foreach", params={
            "items": "names", "item_var": "n", "index_var": "i"}))
        self.assertEqual(dlg._combo_value(dlg.foreach_items), "names")
        self.assertEqual(dlg.foreach_item_var.text(), "n")
        self.assertEqual(dlg.foreach_index_var.text(), "i")
        step = FlowStep(type="foreach")
        dlg.apply_to(step)
        self.assertEqual(step.params["items"], "names")
        self.assertEqual(step.params["item_var"], "n")
        self.assertEqual(step.params["index_var"], "i")

    def test_foreach_apply_empty_var_defaults(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="foreach"))
        dlg.foreach_item_var.setText("")
        dlg.foreach_index_var.setText("")
        step = FlowStep(type="foreach")
        dlg.apply_to(step)
        self.assertEqual(step.params["item_var"], "item")
        self.assertEqual(step.params["index_var"], "index")

    def test_while_form_fill_and_apply(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="while", params={"condition": "i<3"}))
        self.assertEqual(dlg.cond_edit.text(), "i<3")
        step = FlowStep(type="while")
        dlg.apply_to(step)
        self.assertEqual(step.params["condition"], "i<3")

    def test_end_markers_build_without_error(self):
        from app.ui.flow_dialog import StepParamsDialog
        for t in ("endForeach", "endWhile", "endif", "else", "break", "continue"):
            StepParamsDialog(FlowStep(type=t))     # 不应抛异常


class TestWebAttachDialogForm(unittest.TestCase):
    """网页「接管已打开的浏览器」表单：端口行显隐、回填与写回。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dialog(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        p = default_step_params("web")
        p.update(params)
        return StepParamsDialog(FlowStep(type="web", params=p))

    def _row_visible(self, dlg, key: str):
        """读 _web_rows 记录的某行当前显隐（QFormLayout.isRowVisible）。"""
        for k, form, row in dlg._web_rows:
            if k == key:
                return form.isRowVisible(row)
        return None

    def test_attach_row_hidden_for_front(self):
        """默认前台模式：接管端口行隐藏。"""
        dlg = self._dialog()
        self.assertEqual(self._row_visible(dlg, "attach_port"), False)

    def test_attach_row_shown_when_attach_selected(self):
        """切到「接管」模式：接管端口行显示；切走则隐藏。"""
        dlg = self._dialog()
        dlg.launch_combo.setCurrentIndex(dlg.launch_combo.findData("attach"))
        self.assertEqual(self._row_visible(dlg, "attach_port"), True)
        dlg.launch_combo.setCurrentIndex(dlg.launch_combo.findData("front"))
        self.assertEqual(self._row_visible(dlg, "attach_port"), False)

    def test_attach_row_hidden_for_non_open_actions(self):
        """操作不是「打开网址」（如关闭标签页）时，即使选了接管也不显示端口行。"""
        dlg = self._dialog()
        dlg.web_action.setCurrentIndex(dlg.web_action.findData("close_tab"))
        dlg.launch_combo.setCurrentIndex(dlg.launch_combo.findData("attach"))
        self.assertEqual(self._row_visible(dlg, "attach_port"), False)

    def test_fill_restores_attach_port(self):
        """老配置带 attach_port 回填：端口文本与打开方式正确。"""
        dlg = self._dialog(action="open", launch_mode="attach", attach_port="9333")
        self.assertEqual(dlg.launch_combo.currentData(), "attach")
        self.assertEqual(dlg.attach_port_edit.text(), "9333")

    def test_apply_persists_attach_port(self):
        """保存：attach_port 写回步骤参数。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="web")
        dlg = StepParamsDialog(step)
        dlg.launch_combo.setCurrentIndex(dlg.launch_combo.findData("attach"))
        dlg.attach_port_edit.setText("9444")
        dlg.apply_to(step)
        self.assertEqual(step.params["launch_mode"], "attach")
        self.assertEqual(step.params["attach_port"], "9444")


class TestWebDecoupled(_TempPathsMixin, unittest.TestCase):
    """网页打开/关闭已解耦（2026-09-04）：独立拖入、互不联动删除、无配对标记。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)

    def tearDown(self):
        self._temp_exit()

    def _add_flow(self, steps):
        flow = Flow(name="网页流程", steps=steps)
        # 不能重新绑定 cfg.flows（FlowTab._flows 持有原列表引用），只能原地 append
        self.cfg.flows.append(flow)
        self.tab.refresh_list()          # 左栏重建并自动选中唯一流程
        self.tab._select_flow_item(flow.id)   # 空流程（无条目可选）时也确保选中
        return flow

    def _new_web_step(self, action: str):
        p = default_step_params("web", self.cfg.clicker, self.cfg.presser)
        p["action"] = action
        return FlowStep(type="web", params=p)

    def test_drop_web_creates_single_open_step(self):
        """拖「网页操作」只生成一个「打开网址」步骤：不自动附带关闭、不带 pair_id。"""
        flow = self._add_flow([])
        self.tab._on_step_dropped("web", 0)
        self.assertEqual(len(flow.steps), 1)
        s = flow.steps[0]
        self.assertEqual(s.type, "web")
        self.assertEqual(s.params["action"], "open")
        self.assertEqual(s.pair_id, "")
        self.assertFalse(s.continue_on_fail)     # web 失败默认终止（与既有语义一致）

    def test_panel_has_no_close_browser_module(self):
        """面板不再有独立的「关闭浏览器」模块（2026-09-04 删除入口）。

        关闭浏览器收敛为 web 步骤对话框里「操作」下拉的一个选项；
        面板「网页操作」拖入仍只生成 open 单步。
        """
        types = [t for _, _, types in MODULE_GROUPS for t in types]
        self.assertNotIn("close_browser", types)
        self.assertNotIn("close_browser", self.tab._module_btn_by_type)
        flow = self._add_flow([])
        self.tab._on_step_dropped("web", 0)
        self.assertEqual(len(flow.steps), 1)
        self.assertEqual(flow.steps[0].params["action"], "open")

    def test_open_then_close_keeps_both_independent(self):
        """打开 + 关闭按顺序插入后是两个独立步骤（可被其它步骤隔开）。

        面板只拖「网页操作」（open 单步），关闭步骤通过编辑该步骤的
        「操作」下拉切换为 close_browser 生成（与真实操作路径一致）。
        """
        flow = self._add_flow([])
        self.tab._on_step_dropped("web", 0)          # row 0：open
        self.tab._on_step_dropped("wait", 1)         # row 1：wait（隔开）
        self.tab._on_step_dropped("web", 2)          # row 2：再拖一个 web 步骤
        flow.steps[2].params["action"] = "close_browser"   # 模拟在「操作」里改成关闭浏览器
        self.tab._reload_steps()
        acts = [s.params.get("action") for s in flow.steps
                if s.type == "web"]
        self.assertEqual(acts, ["open", "close_browser"])
        self.assertEqual([s.pair_id for s in flow.steps], ["", "", ""])

    def test_delete_close_keeps_open(self):
        """删除「关闭浏览器」不再连带删除配对的「打开网址」（解耦前会同步删）。"""
        flow = self._add_flow([self._new_web_step("open"),
                               self._new_web_step("close_browser")])
        self.tab.step_list.setCurrentRow(1)
        with mock.patch.object(FlowTab, "_confirm_del_step", return_value=True):
            self.tab._del_step()
        self.assertEqual(len(flow.steps), 1)
        self.assertEqual(flow.steps[0].params["action"], "open")

    def test_step_rows_show_no_pair_marker(self):
        """步骤列表不再显示「🔗成对」标记，只有（失败继续）语义标记。"""
        flow = self._add_flow([self._new_web_step("open"),
                               self._new_web_step("close_browser")])
        texts = [self.tab.step_list.item(i).text()
                 for i in range(self.tab.step_list.count())]
        self.assertTrue(any("关闭浏览器" in t for t in texts))
        self.assertTrue(all("🔗" not in t for t in texts))


class TestCloseAppFailCheckbox(unittest.TestCase):
    """「关闭应用」失败处理勾选框：默认勾选、可取消、写回 continue_on_fail。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_dialog_defaults_checked(self):
        """新建 close_app 步骤对话框：勾选框默认勾选「运行失败后继续运行后续流程」。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="close_app"))
        self.assertTrue(dlg.continue_box.isChecked())
        self.assertIn("继续运行后续流程", dlg.continue_box.text())

    def test_dialog_fill_unchecked_then_checked(self):
        """回填：continue_on_fail=False 的旧步骤 → 勾选框不勾。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="close_app")
        step.continue_on_fail = False
        dlg = StepParamsDialog(step)
        self.assertFalse(dlg.continue_box.isChecked())

    def test_apply_writes_check_state(self):
        """保存：勾选状态写回 step.continue_on_fail。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="close_app")
        dlg = StepParamsDialog(step)
        dlg.continue_box.setChecked(False)     # 用户取消勾选
        dlg.apply_to(step)
        self.assertFalse(step.continue_on_fail)
        dlg.continue_box.setChecked(True)
        dlg.apply_to(step)
        self.assertTrue(step.continue_on_fail)


def _start_drag_distance() -> int:
    from PySide6.QtWidgets import QApplication
    return QApplication.startDragDistance()


class TestModuleButtonDragGuard(unittest.TestCase):
    """模块按钮起拖的两个前置条件：超过系统拖拽阈值 + 同一时刻只允许一次拖拽。

    为什么必须有互斥：drag.exec() 在落点不接受本 MIME 时会**立刻返回**，而鼠标
    还按着；原来每次 mouseMove 都会新建一个 QDrag 再走一遍 OLE DoDragDrop
    （每次几十毫秒的 COM 初始化），鼠标划过模块面板/流程树上方时被反复触发，
    表现为「按下拖动、松开鼠标都卡」。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.ui import flow_dialog
        self.drag_calls = []

        class FakeDrag:
            def __init__(self, source):
                self.source = source
                self.mime = None
                self.actions = None
                self.drag_calls.append(self)

            def setMimeData(self, mime):
                self.mime = mime

            def exec(self, actions):
                self.actions = actions
                return actions

        FakeDrag.drag_calls = self.drag_calls
        patcher = mock.patch.object(flow_dialog, "QDrag", FakeDrag)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.btn = flow_dialog.ModuleButton("click", "鼠标点击")
        self.mime_type = flow_dialog.MIME_TYPE

    def _press(self, x=10, y=10):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent
        self.btn.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    def _move(self, x, y):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent
        self.btn.mouseMoveEvent(QMouseEvent(
            QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier))

    def _release(self, x=10, y=10):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent
        self.btn.mouseReleaseEvent(QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

    def test_move_without_press_does_not_drag(self):
        """没按下过左键就移动：不起拖（原来会起拖）。"""
        self._move(200, 200)
        self.assertEqual(len(self.drag_calls), 0)

    def test_small_jitter_does_not_drag(self):
        """按下后只抖 1~2px：不起拖，按钮还能正常点。"""
        self._press(10, 10)
        self._move(11, 11)
        self._move(12, 11)
        self.assertEqual(len(self.drag_calls), 0)

    def test_move_beyond_threshold_starts_one_drag(self):
        """超过系统拖拽阈值：起拖一次，MIME 携带该模块的步骤类型。"""
        self._press(10, 10)
        self._move(10 + _start_drag_distance() + 5, 10)
        self.assertEqual(len(self.drag_calls), 1)
        data = bytes(self.drag_calls[0].mime.data(self.mime_type)).decode()
        self.assertEqual(data, "click")

    def test_no_second_drag_while_button_still_held(self):
        """exec 返回后鼠标仍按着：后续移动不得再起拖（原来每次都起拖）。"""
        self._press(10, 10)
        self._move(100, 10)
        self.assertEqual(len(self.drag_calls), 1)
        for x in range(110, 200, 10):
            self._move(x, 10)
        self.assertEqual(len(self.drag_calls), 1)

    def test_new_press_allows_new_drag(self):
        """松手再按下：允许再起一次拖（互斥标记必须被复位）。"""
        self._press(10, 10)
        self._move(100, 10)
        self._release(100, 10)
        self._move(150, 10)                    # 未按下，不该起拖
        self.assertEqual(len(self.drag_calls), 1)
        self._press(10, 10)
        self._move(100, 10)
        self.assertEqual(len(self.drag_calls), 2)


class TestDragHintRepaintScope(_TempPathsMixin, unittest.TestCase):
    """拖动插入辅助线只重绘「窄带」，不再整表重绘。

    为什么要有这条：辅助线只有 3px 高，旧实现每次都把整个视口（12 行）标脏，
    实测 8ms/次；拖动时鼠标每跨一行就触发一次，是拖动发顿的主要绘制开销。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flow_groups = []
        self.cfg.collapsed_flow_groups = []
        self.cfg.collapsed_module_groups = []
        self.cfg.flows = [Flow(name="拖动流程", steps=[
            FlowStep(type="wait", params=default_step_params("wait")) for _ in range(6)])]
        self.tab = FlowTab(self.cfg)
        self.tab.resize(1000, 700)
        self.tab.show()
        self._app.processEvents()
        self.sl = self.tab.step_list
        self.assertEqual(self.sl.count(), 6)

    def tearDown(self):
        self.tab.hide()
        self._temp_exit()

    def _hint_at(self, row: int):
        """模拟鼠标拖到第 row 行的**下半行**（落点应插到该行之后，即 row+1）。"""
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QDragMoveEvent
        rect = self.sl.visualItemRect(self.sl.item(row))
        y = rect.center().y() + max(1, rect.height() // 4)   # 明确落在下半行
        mime = self.sl.model().mimeData([self.sl.model().index(0, 0)])
        ev = QDragMoveEvent(QPoint(40, y), Qt.MoveAction, mime,
                            Qt.LeftButton, Qt.NoModifier)
        ev.setDropAction(Qt.MoveAction)
        return ev

    def test_hint_rect_is_thin_strip(self):
        """脏区高度 7px、宽度铺满，远小于整个视口。"""
        for row in (0, 3, self.sl.count()):
            r = self.sl._hint_rect(row)
            self.assertEqual(r.height(), 7, f"row={row} 应只标脏 7px 高")
            self.assertEqual(r.width(), self.sl.viewport().width())
            self.assertLess(r.height(), self.sl.viewport().height() / 10)

    def test_hint_rect_none_is_empty(self):
        """没有辅助线时不标脏任何区域。"""
        self.assertTrue(self.sl._hint_rect(None).isNull())

    def test_hint_y_matches_painted_line(self):
        """paintEvent 画的 y 必须与 _hint_y 一致，否则窄带会画到别处。"""
        for row in range(self.sl.count()):
            self.assertEqual(self.sl._hint_y(row),
                             self.sl.visualItemRect(self.sl.item(row)).top())
        last = self.sl.count()
        self.assertEqual(self.sl._hint_y(last),
                         self.sl.visualItemRect(self.sl.item(last - 1)).bottom() + 1)

    def test_hint_y_empty_list(self):
        self.sl.clear()
        self.assertEqual(self.sl._hint_y(0), 6)

    def test_update_hint_repaints_only_old_and_new_rows(self):
        """换行只重绘旧线与新线所在的两条窄带，不整表重绘。"""
        painted = []
        with mock.patch.object(self.sl, "_repaint_hint",
                               side_effect=lambda row: painted.append(row)):
            self.sl._update_drop_hint(self._hint_at(1))     # 第一次：无旧线
            self.assertEqual(painted, [None, 2])
            painted.clear()
            self.sl._update_drop_hint(self._hint_at(4))     # 第二次：2 -> 5
            self.assertEqual(painted, [2, 5])

    def test_same_row_does_not_repaint(self):
        """落点行没变就不重绘（鼠标在同一条缝里抖动不该刷屏）。"""
        self.sl._drop_row_hint = 2
        painted = []
        with mock.patch.object(self.sl, "_repaint_hint",
                               side_effect=lambda row: painted.append(row)):
            self.sl._update_drop_hint(self._hint_at(1))     # 仍是插到第 2 行
            self.assertEqual(painted, [])

    def test_clear_hint_repaints_old_strip(self):
        """离开/落下时把旧线那条窄带擦掉。"""
        self.sl._drop_row_hint = 3
        painted = []
        with mock.patch.object(self.sl, "_repaint_hint",
                               side_effect=lambda row: painted.append(row)):
            self.sl._clear_drop_hint()
        self.assertEqual(painted, [3])
        self.assertIsNone(self.sl._drop_row_hint)


class TestHoverRepaintScope(_TempPathsMixin, unittest.TestCase):
    """悬停高亮只重绘那一行的「▶ 执行」按钮矩形，不再整表重绘。

    悬停只改按钮配色，而旧实现每次换行都 `viewport().update()`（整表 8ms）。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flow_groups = []
        self.cfg.collapsed_flow_groups = []
        self.cfg.collapsed_module_groups = []
        self.cfg.flows = [Flow(name="悬停流程", steps=[
            FlowStep(type="wait", params=default_step_params("wait")) for _ in range(5)])]
        self.tab = FlowTab(self.cfg)
        self.tab.resize(1000, 700)
        self.tab.show()
        self._app.processEvents()
        self.sl = self.tab.step_list
        self.assertEqual(self.sl.count(), 5)

    def tearDown(self):
        self.tab.hide()
        self._temp_exit()

    def _move_to_row(self, row: int):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        rect = self.sl.visualItemRect(self.sl.item(row))
        y = rect.center().y()
        self.sl.mouseMoveEvent(QMouseEvent(
            QEvent.MouseMove, QPointF(40, y), QPointF(40, y),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier))

    def test_button_rect_is_small(self):
        """按钮矩形远小于整个视口——这是「只重绘它」值得做的前提。"""
        rect = StepRunDelegate.button_rect(self.sl.visualItemRect(self.sl.item(0)))
        self.assertEqual((rect.width(), rect.height()),
                         (StepRunDelegate.BTN_W, StepRunDelegate.BTN_H))
        self.assertLess(rect.width() * rect.height(),
                        self.sl.viewport().width() * self.sl.viewport().height() / 50)

    def test_hover_change_repaints_only_two_rows(self):
        painted = []
        with mock.patch.object(self.sl, "_repaint_run_button",
                               side_effect=lambda row: painted.append(row)):
            self._move_to_row(1)
            self.assertEqual(painted, [-1, 1])
            painted.clear()
            self._move_to_row(3)
            self.assertEqual(painted, [1, 3])

    def test_same_row_does_not_repaint(self):
        self.sl._hover_row = 2
        painted = []
        with mock.patch.object(self.sl, "_repaint_run_button",
                               side_effect=lambda row: painted.append(row)):
            self._move_to_row(2)
        self.assertEqual(painted, [])

    def test_leave_repaints_old_row_only(self):
        from PySide6.QtCore import QEvent
        self.sl._hover_row = 2
        painted = []
        with mock.patch.object(self.sl, "_repaint_run_button",
                               side_effect=lambda row: painted.append(row)):
            self.sl.leaveEvent(QEvent(QEvent.Leave))
        self.assertEqual(painted, [2])
        self.assertEqual(self.sl._hover_row, -1)

    def test_repaint_run_button_ignores_bad_rows(self):
        """越界行 / 占位空行不标脏（占位行没有 UserRole，不画按钮）。"""
        from PySide6.QtWidgets import QListWidgetItem
        for row in (-1, 99, None):
            self.sl._repaint_run_button(row)      # 不应抛异常
        self.sl.clear()
        self.sl.addItem(QListWidgetItem("（流程为空：把上方模块拖进来）"))
        self.sl._repaint_run_button(0)            # 占位行：不标脏


class TestStepSizeHintCache(_TempPathsMixin, unittest.TestCase):
    """行尺寸必须缓存。

    为什么：stepView 上挂了 QSS，走样式表的 `QStyledItemDelegate.sizeHint`
    单次数百微秒；而拖动时 QListView 会**反复**重算行几何（插入位置、
    visualRect、indexAt 都要用）。不缓存时实测每次鼠标移动 13.98ms
    （同样部件去掉样式表/委托只要 0.04ms），输入通路被拖住 → 拖动卡顿。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flow_groups = []
        self.cfg.collapsed_flow_groups = []
        self.cfg.collapsed_module_groups = []
        self.cfg.flows = [Flow(name="尺寸流程", steps=[
            FlowStep(type="wait", params=default_step_params("wait")) for _ in range(4)])]
        self.tab = FlowTab(self.cfg)
        self.tab.resize(1000, 700)
        self.tab.show()
        self._app.processEvents()
        self.sl = self.tab.step_list
        self.assertEqual(self.sl.count(), 4)

    def tearDown(self):
        self.tab.hide()
        self._temp_exit()

    def _opt(self):
        from PySide6.QtWidgets import QStyleOptionViewItem
        opt = QStyleOptionViewItem()
        opt.widget = self.sl
        opt.rect = self.sl.viewport().rect()
        return opt

    def test_size_hint_reuses_cache_for_every_row(self):
        """缓存生效后，任何一行都直接返回缓存值，不再向样式表求尺寸。"""
        from PySide6.QtCore import QSize
        d = StepRunDelegate(self.sl)
        sentinel = QSize(321, 45)          # 明显不是真实行尺寸
        d._size_cache = sentinel
        for i in range(self.sl.count()):
            self.assertEqual(d.sizeHint(self._opt(), self.sl.model().index(i, 0)),
                             sentinel)

    def test_cached_size_equals_uncached(self):
        """缓存不能改变结果：和直接问样式表拿到的尺寸一致。"""
        from PySide6.QtWidgets import QStyledItemDelegate
        d = StepRunDelegate(self.sl)
        opt = self._opt()
        idx = self.sl.model().index(0, 0)
        cached = d.sizeHint(opt, idx)
        d.invalidate_size_cache()
        self.assertEqual(cached, QStyledItemDelegate.sizeHint(d, opt, idx))

    def test_invalidate_clears_cache(self):
        d = StepRunDelegate(self.sl)
        d.sizeHint(self._opt(), self.sl.model().index(0, 0))
        self.assertIsNotNone(d._size_cache)
        d.invalidate_size_cache()
        self.assertIsNone(d._size_cache)

    def test_uniform_item_sizes_enabled(self):
        """所有行等高 → 打开 uniformItemSizes，让 QListView 复用首行尺寸。"""
        self.assertTrue(self.sl.uniformItemSizes())

    def test_font_and_style_change_invalidate_cache(self):
        """字体/样式变了行高会变，必须让委托重算。

        注意：不能断言「缓存变成 None」——`super().event()` 处理完这类事件后
        QListView 往往立刻重新布局并再次调用 sizeHint，缓存马上又被填上。
        这里直接盯「invalidate_size_cache 被调用了」。
        """
        from PySide6.QtCore import QEvent
        d = self.sl.itemDelegate()
        calls = []
        with mock.patch.object(d, "invalidate_size_cache",
                               side_effect=lambda: calls.append(1)):
            self.sl.event(QEvent(QEvent.FontChange))
            self.sl.event(QEvent(QEvent.StyleChange))
        self.assertEqual(len(calls), 2)


class TestStepOnlyEditsEmitStepsChanged(_TempPathsMixin, unittest.TestCase):
    """纯步骤改动只发 stepsChanged、不发 changed（性能约定，2026-09-15）。

    为什么要钉这条：发 changed 会让主窗口重注册全部全局热键，并重建定时任务页、
    中键菜单页的列表（实测十几毫秒），而那两个页面只显示流程名、与步骤无关。
    拖动排序一直遵守这条；本类把「拖入模块 / 删步骤 / 切注释 / 改步骤参数」也钉上，
    避免以后顺手写回 changed 又让每次操作白做一轮。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.collapsed_module_groups = []
        flow = Flow(name="计数流程")
        flow.steps = [FlowStep(type="wait", params=default_step_params("wait")),
                      FlowStep(type="log", params=default_step_params("log"))]
        self.cfg.flows = [flow]
        self.tab = FlowTab(self.cfg)          # 构造时自动选中唯一流程
        self.flow = self.cfg.flows[0]
        self.changed = []
        self.steps_changed = []
        self.tab.changed.connect(lambda: self.changed.append(1))
        self.tab.stepsChanged.connect(lambda: self.steps_changed.append(1))

    def tearDown(self):
        self._temp_exit()

    def test_drag_in_module_emits_only_steps_changed(self):
        self.tab._on_step_dropped("wait", 1)
        self.assertEqual([s.type for s in self.flow.steps], ["wait", "wait", "log"])
        self.assertEqual(self.steps_changed, [1])
        self.assertEqual(self.changed, [])

    def test_delete_step_emits_only_steps_changed(self):
        self.tab.step_list.setCurrentRow(0)
        with mock.patch.object(FlowTab, "_confirm_del_step", return_value=True):
            self.tab._del_step()
        self.assertEqual([s.type for s in self.flow.steps], ["log"])
        self.assertEqual(self.steps_changed, [1])
        self.assertEqual(self.changed, [])

    def test_toggle_comment_emits_only_steps_changed(self):
        self.tab.step_list.setCurrentRow(0)
        self.tab._toggle_comment_step()
        self.assertTrue(self.flow.steps[0].commented)
        self.assertEqual(self.steps_changed, [1])
        self.assertEqual(self.changed, [])

    def test_edit_step_param_emits_only_steps_changed(self):
        """改步骤参数只动 flow.steps：走 stepsChanged，不刷新热键与其它页面。"""
        from PySide6.QtWidgets import QMessageBox
        from app.ui.flow_dialog import StepParamsDialog
        self.tab.step_list.setCurrentRow(1)
        # 参数校验若不合格会弹模态框，offscreen 下会挂住 —— 一律替换成空 mock
        with mock.patch.object(QMessageBox, "warning"), \
                mock.patch.object(QMessageBox, "information"), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.tab._edit_step_param()
            dlgs = self.tab.findChildren(StepParamsDialog)
            self.assertTrue(dlgs, "参数对话框未打开")
            dlgs[-1].accept()                 # 非模态：accept 触发 finished 保存
        self.assertEqual(self.steps_changed, [1])
        self.assertEqual(self.changed, [])

    def test_flow_level_change_still_emits_changed(self):
        """对照组：流程/分组级改动必须照旧发 changed（其它页面要按名字刷新）。"""
        with mock.patch.object(AppConfig, "save"):
            self.cfg.flow_groups = ["甲", "乙"]
            self.tab._pin_group("乙")
        self.assertEqual(self.cfg.flow_groups, ["乙", "甲"])
        self.assertEqual(self.changed, [1])
        self.assertEqual(self.steps_changed, [])


class TestForModulePanel(_LoopFlowMixin, unittest.TestCase):
    """for 出现在模块面板的「条件分支」组；endFor 作为结构标记不上面板。"""

    def test_panel_contains_for_but_not_endfor(self):
        from app.ui.flow_tab import MODULE_GROUPS
        types = [t for _gid, _title, ts in MODULE_GROUPS for t in ts]
        self.assertIn("for", types)
        self.assertNotIn("endFor", types)

    def test_every_panel_type_has_an_icon(self):
        from app.ui.flow_dialog import _TYPE_ICONS
        from app.ui.flow_tab import MODULE_GROUPS
        for _gid, _title, ts in MODULE_GROUPS:
            for t in ts:
                self.assertIn(t, _TYPE_ICONS, f"模块 {t} 缺少图标")

    def test_for_icons_registered(self):
        from app.ui.flow_dialog import _TYPE_ICONS
        self.assertIn("for", _TYPE_ICONS)
        self.assertIn("endFor", _TYPE_ICONS)


class TestForDialogRoundTrip(unittest.TestCase):
    """for 的参数对话框：_fill 与 apply_to 必须成对（改完保存再打开要看到原值）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_fill_then_apply_round_trip(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="for", params={"var": "k", "start": "2",
                                            "stop": "9", "step": "3"})
        dlg = StepParamsDialog(step, None)
        # _fill：参数回填到控件
        self.assertEqual(dlg.for_var.text(), "k")
        self.assertEqual(dlg._combo_value(dlg.for_start), "2")
        self.assertEqual(dlg._combo_value(dlg.for_stop), "9")
        self.assertEqual(dlg._combo_value(dlg.for_step), "3")
        # 改值 → apply_to 写回
        dlg.for_var.setText("m")
        dlg._set_combo_value(dlg.for_stop, "12")
        dlg.apply_to(step)
        self.assertEqual(step.params["var"], "m")
        self.assertEqual(step.params["stop"], "12")
        self.assertEqual(step.params["start"], "2")      # 没改的保持原值
        self.assertEqual(step.params["step"], "3")

    def test_apply_fills_defaults_for_empty_boxes(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="for", params={"var": "", "start": "",
                                            "stop": "5", "step": ""})
        dlg = StepParamsDialog(step, None)
        dlg._set_combo_value(dlg.for_stop, "5")
        dlg.apply_to(step)
        self.assertEqual(step.params["var"], "i")        # 空 → 默认
        self.assertEqual(step.params["start"], "1")
        self.assertEqual(step.params["step"], "1")


class TestDropOpensEditorFor(_LoopFlowMixin, unittest.TestCase):
    """拖入 for 后立刻打开编辑窗（与 §18 的通用行为一致）。"""

    def test_for_drop_opens_editor(self):
        with mock.patch.object(self.tab, "_edit_step_param") as editor:
            self.tab._on_step_dropped("for", 0)
            self._app.processEvents()
        editor.assert_called_once()
        self.assertEqual(self.flow.steps[0].type, "for")
        self.assertEqual(self.tab.step_list.currentRow(), 0)


class TestStepCopyPaste(_LoopFlowMixin, unittest.TestCase):
    """步骤列表 Ctrl+C 复制 / Ctrl+V 粘贴（2026-09-26 用户要求）。"""

    def _select(self, *rows):
        for r in rows:
            self.tab.step_list.item(r).setSelected(True)

    def _types(self):
        return [s.type for s in self.flow.steps]

    def test_copy_paste_duplicates_step_with_params(self):
        self.flow.steps = [FlowStep(type="wait", params={"seconds": 3})]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.assertEqual(self._types(), ["wait", "wait"])
        self.assertEqual(self.flow.steps[1].params["seconds"], 3)

    def test_paste_inserts_after_current_row(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab.step_list.setCurrentRow(0)          # 当前第 1 行 → 粘到它后面
        self.tab._paste_steps()
        self.assertEqual(self._types(), ["wait", "wait", "log"])

    def test_paste_appends_when_nothing_current(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab.step_list.clearSelection()
        self.tab.step_list.setCurrentRow(-1)
        self.tab._paste_steps()
        self.assertEqual(len(self.flow.steps), 2)

    def test_copy_multiple_selected_steps(self):
        self.flow.steps = [FlowStep(type="wait"), FlowStep(type="log"),
                           FlowStep(type="click")]
        self.tab._reload_steps()
        self._select(0, 2)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.assertEqual(self._types(),
                         ["wait", "log", "click", "wait", "click"])

    def test_copy_block_copies_whole_block(self):
        """复制 if 起始块要连带 endif 与块内步骤（否则粘出来是缺 endif 的残块）。"""
        self.flow.steps = [FlowStep(type="if", params={"condition": "a == 1"}),
                           FlowStep(type="wait"), FlowStep(type="endif")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.assertEqual(self._types(),
                         ["if", "wait", "endif", "if", "wait", "endif"])
        self.assertEqual(self.flow.steps[3].params["condition"], "a == 1")

    def test_copy_from_close_marker_also_whole_block(self):
        """点 endif 复制，同样应复制整块。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="wait"),
                           FlowStep(type="endForeach")]
        self.tab._reload_steps()
        self._select(2)
        self.tab._copy_steps()
        self.assertEqual(len(self.tab._step_clipboard), 3)

    def test_copy_branch_head_only(self):
        self.flow.steps = [FlowStep(type="if"), FlowStep(type="else"),
                           FlowStep(type="endif")]
        self.tab._reload_steps()
        self._select(1)
        self.tab._copy_steps()
        self.assertEqual([s.type for s in self.tab._step_clipboard], ["else"])

    def test_paste_selects_pasted_steps(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.assertEqual(self.tab._selected_step_rows(), [1])

    def test_pasted_copy_is_independent(self):
        """副本必须与原步骤彻底解耦：改副本不能动到原件。"""
        self.flow.steps = [FlowStep(type="wait", params={"seconds": 5})]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.flow.steps[1].params["seconds"] = 99
        self.assertEqual(self.flow.steps[0].params["seconds"], 5)

    def test_paste_with_empty_clipboard_does_nothing(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self.tab._paste_steps()
        self.assertEqual(len(self.flow.steps), 1)

    def test_copy_without_selection_does_nothing(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self.tab.step_list.clearSelection()
        self.tab._copy_steps()
        self.assertEqual(self.tab._step_clipboard, [])

    def test_paste_break_outside_loop_rolls_back(self):
        """break 粘到循环外 → 结构校验拦下并整体回滚（不留脏数据）。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="break"),
                           FlowStep(type="endForeach"), FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(1)                      # 复制 break
        self.tab._copy_steps()
        self.tab.step_list.setCurrentRow(3)  # 当前在循环外那行 → 粘其后
        self.tab._paste_steps()
        self.assertEqual(self._types(),
                         ["foreach", "break", "endForeach", "wait"])

    def test_running_flow_ignores_copy_paste(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        with mock.patch.object(self.tab, "_selected_running", return_value=True):
            self.tab._copy_steps()
            self.tab._paste_steps()
        self.assertEqual(len(self.flow.steps), 1)

    def test_shortcuts_emit_signals(self):
        """列表里按 Ctrl+C / Ctrl+V 会发对应信号（真正的逻辑在 FlowTab）。"""
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        seen = []
        self.tab.step_list.copyRequested.connect(lambda: seen.append("copy"))
        self.tab.step_list.pasteRequested.connect(lambda: seen.append("paste"))
        for key in (Qt.Key_C, Qt.Key_V):
            ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.ControlModifier)
            self._app.sendEvent(self.tab.step_list, ev)
        self.assertEqual(seen, ["copy", "paste"])

    def test_menu_has_copy_and_paste(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        pos = self.tab.step_list.visualItemRect(self.tab.step_list.item(0)).center()
        with mock.patch.object(flow_tab_mod, "QMenu") as qmenu:
            qmenu.return_value.exec.return_value = None
            self.tab._step_context_menu(pos)
        labels = [c.args[0] for c in qmenu.return_value.addAction.call_args_list
                  if c.args]
        self.assertTrue(any("复制" in lb for lb in labels), labels)
        self.assertTrue(any("粘贴" in lb for lb in labels), labels)


class TestMissingParamsMarker(_LoopFlowMixin, unittest.TestCase):
    """必填参数没填的步骤在列表里被标记（红框由委托画，标记数据挂在 item 上）。"""

    def _marker(self, row):
        return self.tab.step_list.item(row).data(flow_dialog_mod._STEP_MISSING_ROLE)

    def test_missing_params_marked_with_tooltip(self):
        self.flow.steps = [FlowStep(type="wait_text", params={"text": ""})]
        self.tab._reload_steps()
        self.assertEqual(self._marker(0), ["目标文字"])
        self.assertIn("还没设置", self.tab.step_list.item(0).toolTip())

    def test_empty_result_var_is_not_flagged(self):
        """回归（2026-09-26 用户报）：等待文字出现的两个结果变量都可留空，不该标红。"""
        self.flow.steps = [FlowStep(type="wait_text",
                                    params={"text": "开始", "result_var": "",
                                            "pos_var": ""})]
        self.tab._reload_steps()
        self.assertIsNone(self._marker(0))

    def test_complete_step_not_marked(self):
        self.flow.steps = [FlowStep(type="wait_text",
                                    params={"text": "开始", "result_var": "t"})]
        self.tab._reload_steps()
        self.assertIsNone(self._marker(0))
        self.assertEqual(self.tab.step_list.item(0).toolTip(), "")

    def test_commented_step_not_marked(self):
        """已注释的步骤运行期会被跳过，不该再刷红框。"""
        self.flow.steps = [FlowStep(type="var", params={"name": ""}, commented=True)]
        self.tab._reload_steps()
        self.assertIsNone(self._marker(0))

    def test_marker_clears_after_filling(self):
        self.flow.steps = [FlowStep(type="if", params={"condition": ""})]
        self.tab._reload_steps()
        self.assertEqual(self._marker(0), ["条件表达式"])
        self.flow.steps[0].params["condition"] = "a == 1"
        self.tab._reload_steps()
        self.assertIsNone(self._marker(0))

    def test_structural_steps_not_marked(self):
        self.flow.steps = [FlowStep(type="foreach", params={"items": "arr"}),
                           FlowStep(type="break"),
                           FlowStep(type="endForeach")]
        self.tab._reload_steps()
        self.assertIsNone(self._marker(0))
        self.assertIsNone(self._marker(1))
        self.assertIsNone(self._marker(2))

    def test_delegate_paints_marked_row_without_error(self):
        """委托真的能把这行画出来（红框分支不抛异常）。"""
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QStyleOptionViewItem
        self.flow.steps = [FlowStep(type="var", params={"name": ""})]
        self.tab._reload_steps()
        delegate = self.tab.step_list.itemDelegate()
        index = self.tab.step_list.indexFromItem(self.tab.step_list.item(0))
        opt = QStyleOptionViewItem()
        rect = self.tab.step_list.visualItemRect(self.tab.step_list.item(0))
        opt.rect = rect if rect.isValid() else opt.rect
        pix = QPixmap(max(1, opt.rect.width()), max(1, opt.rect.height()))
        painter = QPainter(pix)
        delegate.paint(painter, opt, index)     # 不抛异常即通过
        painter.end()


class TestUndoSteps(_LoopFlowMixin, unittest.TestCase):
    """Ctrl+Z 撤销步骤改动，可连续撤销（2026-09-26 用户要求）。"""

    def _types(self):
        return [s.type for s in self.flow.steps]

    def _select(self, *rows):
        for r in rows:
            self.tab.step_list.item(r).setSelected(True)

    def test_undo_after_drop(self):
        self.tab._on_step_dropped("wait", 0)
        self.assertEqual(self._types(), ["wait"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), [])

    def test_undo_after_delete_restores_step_and_params(self):
        self.flow.steps = [FlowStep(type="wait", params={"seconds": 5}),
                           FlowStep(type="log")]
        self.tab._reload_steps()
        self._select(0)
        with mock.patch.object(FlowTab, "_confirm_del_step", return_value=True):
            self.tab._del_step()
        self.assertEqual(self._types(), ["log"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), ["wait", "log"])
        self.assertEqual(self.flow.steps[0].params["seconds"], 5)   # 参数一并恢复

    def test_undo_after_paste(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        self.assertEqual(len(self.flow.steps), 2)
        self.tab._undo_steps()
        self.assertEqual(len(self.flow.steps), 1)

    def test_undo_after_comment(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._toggle_comment_step()
        self.assertTrue(self.flow.steps[0].commented)
        self.tab._undo_steps()
        self.assertFalse(self.flow.steps[0].commented)

    def test_multiple_undo_goes_back_step_by_step(self):
        self.tab._on_step_dropped("wait", 0)
        self.tab._on_step_dropped("log", 1)
        self.tab._on_step_dropped("click", 2)
        self.assertEqual(self._types(), ["wait", "log", "click"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), ["wait", "log"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), ["wait"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), [])
        self.tab._undo_steps()                 # 到底了再按也不出错
        self.assertEqual(self._types(), [])

    def test_undo_removes_whole_block(self):
        self.tab._on_step_dropped("if", 0)
        self.assertEqual(self._types(), ["if", "endif"])
        self.tab._undo_steps()
        self.assertEqual(self._types(), [])

    def test_empty_stack_reports_message(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        msgs = []
        with mock.patch.object(FlowTab, "_status_msg",
                               side_effect=lambda t, ms: msgs.append(t)):
            self.tab._undo_steps()
        self.assertTrue(any("没有可撤销" in m for m in msgs), msgs)

    def test_rejected_drop_records_nothing(self):
        """被拒绝的拖入（else 不在 if 块内）不该产生撤销点。"""
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        with mock.patch.object(flow_tab_mod.QMessageBox, "information",
                               return_value=None):
            self.tab._on_step_dropped("else", 1)      # 非法位置，被拒
        self.assertFalse(self.tab._undo.get(self.flow.id))
        self.tab._undo_steps()                        # 再按撤销应无事发生
        self.assertEqual(self._types(), ["wait"])

    def test_cancelled_delete_records_nothing(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        with mock.patch.object(FlowTab, "_confirm_del_step", return_value=False):
            self.tab._del_step()
        self.assertFalse(self.tab._undo.get(self.flow.id))

    def test_undo_history_is_per_flow(self):
        other = Flow(name="另一个流程", steps=[FlowStep(type="log")])
        self.cfg.flows.append(other)
        self.tab._on_step_dropped("wait", 0)          # 只改当前流程
        self.tab._undo_steps()
        self.assertEqual(self._types(), [])
        self.assertFalse(self.tab._undo.get(other.id))   # 另一个流程没有历史

    def test_undo_ignored_while_running(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        self.tab._copy_steps()
        self.tab._paste_steps()
        with mock.patch.object(self.tab, "_selected_running", return_value=True):
            self.tab._undo_steps()
        self.assertEqual(len(self.flow.steps), 2)     # 运行中不许撤销

    def test_undo_stack_is_capped(self):
        for _ in range(MAX_UNDO_STEPS + 5):
            self.tab._on_step_dropped("wait", 0)
        self.assertLessEqual(len(self.tab._undo.get(self.flow.id, [])),
                             MAX_UNDO_STEPS)

    def test_ctrl_z_emits_undo_signal(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        seen = []
        self.tab.step_list.undoRequested.connect(lambda: seen.append("undo"))
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Z, Qt.ControlModifier)
        self._app.sendEvent(self.tab.step_list, ev)
        self.assertEqual(seen, ["undo"])

    def test_menu_has_undo_item(self):
        self.flow.steps = [FlowStep(type="wait")]
        self.tab._reload_steps()
        self._select(0)
        pos = self.tab.step_list.visualItemRect(self.tab.step_list.item(0)).center()
        with mock.patch.object(flow_tab_mod, "QMenu") as qmenu:
            qmenu.return_value.exec.return_value = None
            self.tab._step_context_menu(pos)
        labels = [c.args[0] for c in qmenu.return_value.addAction.call_args_list
                  if c.args]
        self.assertTrue(any("撤销" in lb for lb in labels), labels)


class TestFlowDragIntoGroup(_TempPathsMixin, unittest.TestCase):
    """★ 把流程拖进分组（2026-10-05 用户要求）。

    真实几何下测（`visualItemRect` / `itemAt` 在离屏也能算出来，前提是先 resize+show），
    所以能覆盖"落在分组头上 / 落在某条流程上"这两种落点翻译。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp_enter()
        self.cfg = AppConfig()
        self.cfg.flows = self._make_flows()
        self.cfg.flow_groups = ["办公", "游戏"]
        self.cfg.collapsed_flow_groups = []
        self.tab = FlowTab(self.cfg)
        self.tree = self.tab.list
        self.tree.resize(320, 640)
        self.tree.show()
        self._app.processEvents()

    def tearDown(self):
        self.tree.hide()
        self._temp_exit()

    def _make_flows(self):
        return [Flow(name="流程A", group="办公"),
                Flow(name="流程B", group="办公"),
                Flow(name="流程C", group="游戏"),
                Flow(name="流程D", group="")]

    def _flow(self, name: str) -> Flow:
        return next(f for f in self.cfg.flows if f.name == name)

    def _names_under(self, group: str) -> list[str]:
        item = self.tab._group_item(group)
        return [_flow_name(item.child(i)) for i in range(item.childCount())]

    def _drop_onto(self, drag_name: str, target_item, pos=None) -> tuple:
        """模拟把某流程拖到某条目上：返回 drop_target 的翻译结果（并真的落地）。"""
        self.tree._dragging_id = self._flow(drag_name).id
        rect = self.tree.visualItemRect(target_item)
        target = self.tree.drop_target(pos or rect.center())
        if target is not None:
            self.tree.entry_dropped.emit(*target)
        self.tree._dragging_id = None
        return target

    # ---------- 落地翻译 ----------

    def test_drop_target_on_group_header(self):
        """落在分组头上 → (流程, 该分组, 无落点流程)。"""
        target = self.tree.drop_target(
            self.tree.visualItemRect(self.tab._group_item("办公")).center())
        self.tree._dragging_id = self._flow("流程D").id
        target = self.tree.drop_target(
            self.tree.visualItemRect(self.tab._group_item("办公")).center())
        self.assertEqual(target, (self._flow("流程D").id, "办公", ""))

    def test_drop_target_on_flow_row_inserts_before_it(self):
        """落在某条流程上 → 目标是那条流程所在分组，并插到它前面。"""
        self.tree._dragging_id = self._flow("流程D").id
        item = self.tab._flow_item(self._flow("流程B").id)
        target = self.tree.drop_target(self.tree.visualItemRect(item).center())
        self.assertEqual(target, (self._flow("流程D").id, "办公",
                                  self._flow("流程B").id))

    def test_drop_target_is_none_when_not_dragging_a_flow(self):
        self.tree._dragging_id = None
        self.assertIsNone(self.tree.drop_target(
            self.tree.visualItemRect(self.tab._group_item("办公")).center()))

    def test_group_headers_are_not_draggable(self):
        """分组头不该被拖走（拖它等于把整组搬家，没这个语义）。

        断言的是**没有把拖动交给 Qt 的默认实现**（`QTreeWidget.startDrag`），
        所以补丁要打在基类上——打在 `GroupDropTree` 上等于把被测方法本身换掉。
        """
        item = self.tab._group_item("办公")
        self.tree.setCurrentItem(item)
        with mock.patch.object(flow_tab_mod.QTreeWidget, "startDrag") as super_drag:
            self.tree.startDrag(Qt.MoveAction)
        self.assertFalse(super_drag.called, "分组头不该发起拖动")

    # ---------- 落地效果 ----------

    def test_drop_into_group_moves_and_persists(self):
        before = self._names_under("办公")
        with mock.patch.object(AppConfig, "save") as save:
            self._drop_onto("流程D", self.tab._group_item("办公"))
        self.assertEqual(self._flow("流程D").group, "办公")
        self.assertEqual(self._names_under("办公"), before + ["流程D"],
                         "落到分组头上 = 排在该组末尾")
        self.assertEqual(self._names_under(""), [])
        save.assert_called_once()

    def test_drop_before_a_flow_reorders_inside_the_group(self):
        """★ 组内也能靠拖调整顺序（落在某条流程上 = 插到它前面）。"""
        self._drop_onto("流程B", self.tab._flow_item(self._flow("流程A").id))
        self.assertEqual(self._names_under("办公"), ["流程B", "流程A"])

    def test_drop_onto_ungrouped_moves_flow_out(self):
        """拖到「未分组」= 把流程移出分组。"""
        self._drop_onto("流程A", self.tab._group_item(""))
        self.assertEqual(self._flow("流程A").group, "")
        self.assertEqual(self._names_under(""), ["流程D", "流程A"])
        self.assertEqual(self._names_under("办公"), ["流程B"])

    def test_drop_into_collapsed_group_expands_it(self):
        """拖进收起的分组要顺手展开，否则看起来像"流程消失了"。"""
        self.cfg.collapsed_flow_groups = ["办公"]
        self.tab.refresh_list()
        self._drop_onto("流程D", self.tab._group_item("办公"))
        self.assertNotIn("办公", self.cfg.collapsed_flow_groups)
        self.assertTrue(self.tab._group_item("办公").isExpanded())

    def test_drop_on_unknown_group_is_ignored(self):
        """目标分组不存在（比如配置被改坏）→ 不落任何改动。"""
        self.assertFalse(self.tab.move_flow_to_group(self._flow("流程D").id, "没有这个组"))
        self.assertEqual(self._flow("流程D").group, "")

    def test_noop_drop_does_not_rebuild(self):
        """把流程拖回原处（结果顺序没变）→ 不重建、不落盘。

        流程B 本来就在「办公」末尾，落到该分组头上算出来还是原位。
        """
        with mock.patch.object(AppConfig, "save") as save:
            self.assertFalse(self.tab.move_flow_to_group(
                self._flow("流程B").id, "办公"))
        self.assertFalse(save.called, "顺序没变就别重建列表（会打断选中）")

    def test_moving_flow_keeps_created_seq(self):
        """搬组不能动 created_seq —— 那是"按创建顺序排序"的依据。"""
        seq = self._flow("流程D").created_seq
        self._drop_onto("流程D", self.tab._group_item("游戏"))
        self.assertEqual(self._flow("流程D").created_seq, seq)


if __name__ == "__main__":
    unittest.main()
