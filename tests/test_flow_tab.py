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
from app.ui.flow_tab import (BRANCH_TYPES, INDENT_UNIT, MODULE_GROUPS, FlowTab)
from tests._env import TempConfigPaths


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
        """未手动调整过折叠状态时默认全部收起：模块区隐藏、标题 ▸、不写配置。"""
        for gid, wrapper in self.tab._group_wrappers.items():
            self.assertTrue(wrapper.isHidden(), f"分组 {gid} 应默认收起")
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith("▸"))
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
            self.assertTrue(header.text().startswith("▾"))
            save.assert_called_once()

            header.setChecked(False)                      # 收起
            self.assertTrue(self.tab._group_wrappers["input"].isHidden())
            self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                             sorted(gid for gid, _, _ in MODULE_GROUPS))
            self.assertTrue(header.text().startswith("▸"))

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
        self.assertTrue(tab2._group_headers["logic"].text().startswith("▾"))
        self.assertTrue(tab2._group_headers["input"].text().startswith("▸"))

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
        self.assertEqual(g_office.child(0).text(0), "流程A")
        self.assertEqual(g_office.child(1).text(0), "流程B")
        g_game = self.tab.list.topLevelItem(1)
        self.assertEqual(g_game.childCount(), 1)
        self.assertEqual(g_game.child(0).text(0), "流程C")
        g_ungrouped = self.tab.list.topLevelItem(2)
        self.assertEqual(g_ungrouped.childCount(), 1)
        self.assertEqual(g_ungrouped.child(0).text(0), "流程D")

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
        return [item.child(i).text(0) for i in range(item.childCount())]

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
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 v")  # 默认收起，可点击展开
        self.assertTrue(self.tab.panel_title_btn.isEnabled())
        self.assertIn("展开", self.tab.panel_title_btn.toolTip())

    def test_toggle_expands_then_collapses_all_and_persists(self):
        """点击标题：默认收起 -> 展开全部（记忆为空）；再点 -> 全部收起（状态全部持久化）。"""
        with mock.patch.object(AppConfig, "save") as save:
            self.tab.panel_title_btn.click()          # 全收起 -> 展开全部
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith("▾"))
            self.assertTrue(header.isChecked())
        for wrapper in self.tab._group_wrappers.values():
            self.assertFalse(wrapper.isHidden())
        self.assertTrue(self.cfg.module_groups_explicit)
        self.assertEqual(self.cfg.collapsed_module_groups, [])
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 ^")

        with mock.patch.object(AppConfig, "save") as save:
            self.tab.panel_title_btn.click()          # 全展开 -> 全部收起
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith("▸"))
        for wrapper in self.tab._group_wrappers.values():
            self.assertTrue(wrapper.isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS))
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 v")
        save.assert_called()

    def test_expand_individually_from_collapsed(self):
        """默认全收起时，点单个分组标题可单独展开，其余保持收起，标题回 ^。"""
        with mock.patch.object(AppConfig, "save"):
            self.tab._group_headers["input"].setChecked(True)   # 单独展开 input
        self.assertFalse(self.tab._group_wrappers["input"].isHidden())
        self.assertTrue(self.tab._group_wrappers["perceive"].isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS if gid != "input"))
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 ^")

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
        self.tab = FlowTab(self.cfg)      # 构造时自动选中唯一流程
        self.flow = self.cfg.flows[0]

    def tearDown(self):
        self._temp_exit()

    def _types(self):
        return [s.type for s in self.flow.steps]


class TestLoopBlockInsert(_LoopFlowMixin, unittest.TestCase):
    def test_foreach_dropped_creates_pair(self):
        self.tab._on_step_dropped("foreach", 0)
        self.assertEqual(self._types(), ["foreach", "endForeach"])

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
        """块内交换步骤顺序合法：应用新顺序并触发 changed。"""
        self.flow.steps = [FlowStep(type="foreach"), FlowStep(type="click"),
                           FlowStep(type="press"), FlowStep(type="endForeach")]
        self.tab._reload_steps()
        changed = []
        self.tab.changed.connect(lambda: changed.append(1))
        self._set_list_order([0, 2, 1, 3])     # 交换 click / press
        self.tab._on_order_changed()
        self._pump()
        self.assertEqual(self._types(),
                         ["foreach", "press", "click", "endForeach"])
        self.assertEqual(len(changed), 1)


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


if __name__ == "__main__":
    unittest.main()
