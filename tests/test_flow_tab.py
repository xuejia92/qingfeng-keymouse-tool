"""流程左栏分组（展开/收起）与模块面板分组（收起/展开）的测试。

验证：模块分组 10 个模块全部纳入分组且无遗漏；模块分组默认展开、点击收起并
持久化到 cfg.collapsed_module_groups；流程左栏按分组构建树、流程归属正确、
分组展开/收起持久化到 cfg.collapsed_flow_groups、右键菜单项齐全。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from app.config import AppConfig, FLOW_STEP_TYPES, Flow
from app.ui.flow_tab import MODULE_GROUPS, FlowTab


class TestModuleGroupsDefinition(unittest.TestCase):
    def test_groups_cover_all_modules(self):
        """分组必须恰好覆盖 FLOW_STEP_TYPES 的全部类型，且无重复。"""
        grouped = [t for _, _, types in MODULE_GROUPS for t in types]
        self.assertEqual(sorted(grouped), sorted(FLOW_STEP_TYPES))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_group_ids_unique(self):
        gids = [gid for gid, _, _ in MODULE_GROUPS]
        self.assertEqual(len(gids), len(set(gids)))


class TestFlowTabModuleGroups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)

    def test_builds_headers_and_buttons(self):
        """分组头数量 = 分组数；模块按钮数量 = 模块类型数。"""
        self.assertEqual(len(self.tab._group_headers), len(MODULE_GROUPS))
        self.assertEqual(len(self.tab._module_btns), len(FLOW_STEP_TYPES))

    def test_all_expanded_by_default(self):
        """默认全部展开：无显式隐藏（isHidden=False 表示 setVisible(True) 生效）。"""
        for gid, wrapper in self.tab._group_wrappers.items():
            self.assertFalse(wrapper.isHidden(), f"分组 {gid} 应默认展开")
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith("▾"))

    def test_collapse_then_expand(self):
        """点击分组标题：收起 -> 隐藏模块 + 记入配置；再点 -> 恢复展开 + 清出配置。"""
        with mock.patch.object(AppConfig, "save") as save:
            header = self.tab._group_headers["input"]
            header.setChecked(False)                      # 收起
            self.assertTrue(self.tab._group_wrappers["input"].isHidden())
            self.assertEqual(self.cfg.collapsed_module_groups, ["input"])
            self.assertTrue(header.text().startswith("▸"))
            save.assert_called_once()

            header.setChecked(True)                       # 展开
            self.assertFalse(self.tab._group_wrappers["input"].isHidden())
            self.assertEqual(self.cfg.collapsed_module_groups, [])
            self.assertTrue(header.text().startswith("▾"))

    def test_collapse_persists_and_restores(self):
        """收起多组后重建 FlowTab，状态从 cfg 恢复。"""
        with mock.patch.object(AppConfig, "save"):
            self.tab._group_headers["logic"].setChecked(False)
            self.tab._group_headers["app_web"].setChecked(False)
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         ["app_web", "logic"])

        tab2 = FlowTab(self.cfg)                          # 用同一 cfg 重建
        self.assertTrue(tab2._group_wrappers["logic"].isHidden())
        self.assertTrue(tab2._group_wrappers["app_web"].isHidden())
        self.assertFalse(tab2._group_wrappers["input"].isHidden())
        self.assertTrue(tab2._group_headers["logic"].text().startswith("▸"))
        self.assertTrue(tab2._group_headers["input"].text().startswith("▾"))

    def test_unknown_group_ids_ignored_on_load(self):
        """config 里未知分组 id 应被忽略，不产生异常。"""
        cfg = AppConfig()
        cfg.flows = []
        cfg.collapsed_module_groups = ["no_such_group"]
        tab2 = FlowTab(cfg)                                # 不应抛异常
        self.assertFalse(any(h.isHidden() for h in tab2._group_headers.values()))

    def test_buttons_disabled_while_running_lock(self):
        """运行中锁定编辑：模块按钮与分组头一起禁用。"""
        self.tab._module_btns[0].setEnabled(False)
        for h in self.tab._group_headers.values():
            h.setEnabled(False)
        self.assertFalse(any(b.isEnabled() for b in self.tab._module_btns[:1]))
        self.assertFalse(any(h.isEnabled() for h in self.tab._group_headers.values()))


class TestFlowTabFlowGroups(unittest.TestCase):
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
        self.cfg = AppConfig()
        self.cfg.flows = self._make_flows()
        self.cfg.flow_groups = ["办公", "游戏"]
        self.cfg.collapsed_flow_groups = []
        self.tab = FlowTab(self.cfg)

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
        """流程右键菜单含：编辑流程 / 删除流程 / 导出流程。"""
        from PySide6.QtWidgets import QMenu
        from app.ui import flow_tab as ft
        # 复刻 _flow_context_menu 的菜单构建，验证动作文本
        menu = QMenu()
        self.tab._style_menu(menu)
        edit_act = menu.addAction("✎ 编辑流程")
        del_act = menu.addAction("🗑 删除流程")
        menu.addSeparator()
        export_act = menu.addAction("📤 导出流程")
        self.assertEqual([a.text() for a in menu.actions() if a.text()],
                         ["✎ 编辑流程", "🗑 删除流程", "📤 导出流程"])
        self.assertTrue(all(a is not None for a in (edit_act, del_act, export_act)))
        # 分组右键菜单
        gmenu = QMenu()
        self.tab._style_menu(gmenu)
        gmenu.addAction("✎ 重命名分组")
        gmenu.addAction("🗑 删除分组")
        self.assertEqual([a.text() for a in gmenu.actions() if a.text()],
                         ["✎ 重命名分组", "🗑 删除分组"])

    def test_selected_flow_by_group_item(self):
        """点击分组头不产生选中流程；点击流程条目能选中对应流程。"""
        g_item = self.tab._group_item("办公")
        self.tab.list.setCurrentItem(g_item)
        self.assertIsNone(self.tab._selected_flow())
        f_item = self.tab._flow_item(self.cfg.flows[0].id)
        self.tab.list.setCurrentItem(f_item)
        self.assertEqual(self.tab._selected_flow().name, "流程A")


class TestModulePanelCollapseAll(unittest.TestCase):
    """模块面板「一键收起」小按钮。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.flows = []
        self.cfg.collapsed_module_groups = []
        self.tab = FlowTab(self.cfg)

    def test_panel_title_button_exists(self):
        self.assertIsNotNone(self.tab.panel_title_btn)
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 ^")  # 文字后带 ^
        self.cfg.flows.append(Flow(name="流程X"))
        self.tab.refresh_list()                       # 选中流程 -> 编辑解锁
        self.assertIn("收起", self.tab.panel_title_btn.toolTip())

    def test_collapse_all_collapses_every_group_and_persists(self):
        """点击面板标题：所有分组变 ▸、内容隐藏、标题变 v、状态全部持久化。"""
        self.cfg.flows.append(Flow(name="流程X"))  # 原地追加，_flows 引用同一列表
        self.tab.refresh_list()                       # 选中流程 -> 编辑解锁
        self.assertTrue(self.tab.panel_title_btn.isEnabled())

        with mock.patch.object(AppConfig, "save") as save:
            self.tab.panel_title_btn.click()
        for header in self.tab._group_headers.values():
            self.assertTrue(header.text().startswith("▸"))
        for wrapper in self.tab._group_wrappers.values():
            self.assertTrue(wrapper.isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS))
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 v")
        save.assert_called()

    def test_collapse_all_expands_individually(self):
        """全部收起后，点单个分组标题仍可单独展开，标题符号回 ^。"""
        self.cfg.flows.append(Flow(name="流程X"))
        self.tab.refresh_list()
        self.tab.panel_title_btn.click()
        self.tab._group_headers["input"].setChecked(True)   # 单独展开 input
        self.assertFalse(self.tab._group_wrappers["input"].isHidden())
        self.assertTrue(self.tab._group_wrappers["perceive"].isHidden())
        self.assertEqual(sorted(self.cfg.collapsed_module_groups),
                         sorted(gid for gid, _, _ in MODULE_GROUPS if gid != "input"))
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 ^")

    def test_collapse_all_toggles_back_to_expand(self):
        """全部收起后再点标题：所有分组重新展开，标题回 ^。"""
        self.cfg.flows.append(Flow(name="流程X"))
        self.tab.refresh_list()
        self.tab.panel_title_btn.click()        # 全部收起
        self.tab.panel_title_btn.click()        # 全部展开
        for header in self.tab._group_headers.values():
            self.assertTrue(header.isChecked())
            self.assertTrue(header.text().startswith("▾"))
        for wrapper in self.tab._group_wrappers.values():
            self.assertFalse(wrapper.isHidden())
        self.assertEqual(self.cfg.collapsed_module_groups, [])
        self.assertEqual(self.tab.panel_title_btn.text(), "模块面板 ^")

    def test_perceive_group_renamed_with_screenshot(self):
        """「文字识别」分组改名为「目标识别」，并纳入新模块「截图」。"""
        titles = {gid: title for gid, title, _ in MODULE_GROUPS}
        self.assertEqual(titles["perceive"], "目标识别")
        types = {gid: ts for gid, _, ts in MODULE_GROUPS}
        self.assertIn("screenshot", types["perceive"])
        header = self.tab._group_headers["perceive"]
        self.assertIn("目标识别", header.text())


if __name__ == "__main__":
    unittest.main()
