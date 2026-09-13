"""中键菜单数据模型、配置持久化与运行时菜单构造的测试。

覆盖：
- MiddleMenuItem 的默认值/截断/display_label 回退链；
- middle_menu_item_from_dict 对脏数据的容错；
- AppConfig 的中键菜单字段往返持久化；
- build_menu 的条目顺序、断链跳过、分隔线插入与 action data 关联。
"""
from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import (AppConfig, Flow, FlowStep, MiddleMenuItem,
                        default_middle_menu_items, middle_menu_item_from_dict)
from app.ui.middle_menu_icons import blank_icon
from app.ui.middle_menu_tab import build_menu
from tests._env import TempConfigPaths


def _flow(name: str) -> Flow:
    return Flow(name=name, steps=[FlowStep(type="log", name="打印")])


class TestMiddleMenuItem(unittest.TestCase):
    def test_auto_id(self):
        self.assertTrue(MiddleMenuItem().id)

    def test_fields_truncated(self):
        it = MiddleMenuItem(label="x" * 80, flow_name="y" * 80)
        self.assertEqual(len(it.label), 50)
        self.assertEqual(len(it.flow_name), 50)

    def test_defaults(self):
        it = MiddleMenuItem()
        self.assertEqual(it.label, "")
        self.assertEqual(it.icon, "")          # 默认不设图标
        self.assertEqual(it.flow_id, "")
        self.assertFalse(it.separator_before)

    def test_icon_truncated(self):
        self.assertEqual(len(MiddleMenuItem(icon="x" * 40).icon), 20)

    def test_display_label_prefers_custom(self):
        it = MiddleMenuItem(label="  我的入口  ", flow_id="f1", flow_name="流程")
        self.assertEqual(it.display_label(_flow("流程")), "我的入口")

    def test_display_label_falls_back_to_flow_name_of_live_flow(self):
        """未填自定义名称时跟随流程实时名称（流程改名后菜单文字自动更新）。"""
        it = MiddleMenuItem(flow_id="f1", flow_name="旧名")
        self.assertEqual(it.display_label(_flow("新名")), "新名")

    def test_display_label_falls_back_to_redundant_name_when_flow_gone(self):
        it = MiddleMenuItem(flow_id="gone", flow_name="已删流程")
        self.assertEqual(it.display_label(None), "已删流程")

    def test_display_label_placeholder_when_everything_empty(self):
        self.assertEqual(MiddleMenuItem().display_label(None), "未命名菜单项")


class TestFromDict(unittest.TestCase):
    def test_non_dict_is_tolerated(self):
        it = middle_menu_item_from_dict(None)
        self.assertTrue(it.id)
        self.assertEqual(it.flow_id, "")

    def test_round_trip_fields(self):
        it = middle_menu_item_from_dict({
            "id": "abc123", "label": "打开", "icon": "rocket", "flow_id": "f9",
            "flow_name": "登录", "separator_before": True})
        self.assertEqual(it.id, "abc123")
        self.assertEqual(it.label, "打开")
        self.assertEqual(it.icon, "rocket")
        self.assertEqual(it.flow_id, "f9")
        self.assertEqual(it.flow_name, "登录")
        self.assertTrue(it.separator_before)

    def test_missing_icon_key_defaults_to_empty(self):
        """旧配置没有 icon 字段时应视为「无图标」，不能报错。"""
        self.assertEqual(middle_menu_item_from_dict({"flow_id": "f1"}).icon, "")

    def test_missing_id_gets_generated(self):
        self.assertTrue(middle_menu_item_from_dict({"flow_id": "f1"}).id)


class TestConfigPersistence(unittest.TestCase):
    def test_round_trip(self):
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.middle_menu_enabled = False
            cfg.middle_menu_suppress = True
            cfg.middle_menu_items = [
                MiddleMenuItem(label="甲", icon="rocket", flow_id="f1", flow_name="流程甲"),
                MiddleMenuItem(flow_id="f2", flow_name="流程乙", separator_before=True),
            ]
            cfg.save(save_flows=False)

            loaded = AppConfig.load()
            self.assertFalse(loaded.middle_menu_enabled)
            self.assertTrue(loaded.middle_menu_suppress)
            self.assertEqual(len(loaded.middle_menu_items), 2)
            self.assertEqual(loaded.middle_menu_items[0].label, "甲")
            self.assertEqual(loaded.middle_menu_items[0].icon, "rocket")
            self.assertEqual(loaded.middle_menu_items[0].flow_id, "f1")
            self.assertTrue(loaded.middle_menu_items[1].separator_before)

    def test_defaults_when_keys_absent(self):
        with TempConfigPaths():
            AppConfig().save(save_flows=False)
            loaded = AppConfig.load()
            self.assertTrue(loaded.middle_menu_enabled)     # 默认开启
            self.assertFalse(loaded.middle_menu_suppress)   # 默认不拦截中键
            self.assertEqual(loaded.middle_menu_items, [])

    def test_default_items_helper(self):
        flows = [_flow("甲"), _flow("乙")]
        items = default_middle_menu_items(flows)
        self.assertEqual([i.flow_id for i in items], [f.id for f in flows])
        self.assertEqual([i.flow_name for i in items], ["甲", "乙"])


class TestBuildMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    @staticmethod
    def _texts(menu) -> list[str]:
        return [a.text() for a in menu.actions() if not a.isSeparator()]

    def test_empty_items_returns_none(self):
        self.assertIsNone(build_menu([], []))

    def test_all_broken_returns_none(self):
        items = [MiddleMenuItem(flow_id="gone1"), MiddleMenuItem(flow_id="gone2")]
        self.assertIsNone(build_menu(items, []))

    def test_order_and_action_data(self):
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id=f2.id), MiddleMenuItem(flow_id=f1.id)]
        menu = build_menu(items, [f1, f2])
        self.assertEqual(self._texts(menu), ["乙", "甲"])          # 按菜单项顺序
        ids = [a.data() for a in menu.actions() if not a.isSeparator()]
        self.assertEqual(ids, [f2.id, f1.id])                      # action 携带流程 id

    def test_broken_link_skipped_but_others_kept(self):
        f1 = _flow("甲")
        items = [MiddleMenuItem(flow_id="gone"), MiddleMenuItem(flow_id=f1.id)]
        menu = build_menu(items, [f1])
        self.assertEqual(self._texts(menu), ["甲"])

    def test_custom_label_used(self):
        f1 = _flow("甲")
        menu = build_menu([MiddleMenuItem(label="我的入口", flow_id=f1.id)], [f1])
        self.assertEqual(self._texts(menu), ["我的入口"])

    def test_leading_separator_is_skipped(self):
        """首个条目的「上方分隔线」不生效，避免菜单顶部多一条线。"""
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id=f1.id, separator_before=True),
                 MiddleMenuItem(flow_id=f2.id, separator_before=True)]
        menu = build_menu(items, [f1, f2])
        seps = [a for a in menu.actions() if a.isSeparator()]
        self.assertEqual(len(seps), 1)
        # 分隔线必须夹在两个条目之间
        idx = menu.actions().index(seps[0])
        self.assertEqual(idx, 1)

    def test_broken_item_does_not_trigger_leading_separator(self):
        """断链条目被跳过后，其后的分隔线仍应正确插在「首个可见条目」之后。"""
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id="gone"),
                 MiddleMenuItem(flow_id=f1.id),
                 MiddleMenuItem(flow_id=f2.id, separator_before=True)]
        menu = build_menu(items, [f1, f2])
        self.assertEqual(len([a for a in menu.actions() if a.isSeparator()]), 1)

    def test_items_are_left_aligned_with_small_gap(self):
        """菜单项文字必须显式左对齐、左侧只留一点空隙。

        不显式指定样式时，Qt 会在左侧预留图标/勾选列，文字被推到约 28px 处，
        菜单又窄，看上去像居中而不是靠左。这里把「左内边距 < 右内边距」
        且「左内边距够小」钉住，防止有人把样式删掉。
        """
        flow = _flow("甲")
        menu = build_menu([MiddleMenuItem(flow_id=flow.id)], [flow])
        self.assertIsNotNone(menu)
        qss = menu.styleSheet()
        self.assertIn("QMenu::item", qss)
        match = re.search(r"padding:\s*(\d+)px\s+(\d+)px\s+(\d+)px\s+(\d+)px", qss)
        self.assertIsNotNone(match, f"未找到四值 padding 声明: {qss!r}")
        top, right, bottom, left = (int(g) for g in match.groups())
        self.assertLess(left, right, "左内边距应小于右内边距（文字靠左）")
        self.assertLessEqual(left, 14, "左侧只该留一点空隙")
        self.assertGreater(top, 0, "上下留白让菜单项有合适行高")

    def test_icon_slot_aligns_with_item_left_padding(self):
        """QMenu::icon 的 left 必须等于 QMenu::item 的 padding-left。

        两者不一致时，有图标的条目与「留空」的条目文字会错开，左边缘对不齐。
        """
        flow = _flow("甲")
        menu = build_menu([MiddleMenuItem(flow_id=flow.id, icon="rocket")], [flow])
        qss = menu.styleSheet()
        pad = re.search(
            r"QMenu::item\s*\{[^}]*padding:\s*\d+px\s+\d+px\s+\d+px\s+(\d+)px", qss)
        icon = re.search(r"QMenu::icon\s*\{[^}]*left:\s*(\d+)px", qss)
        self.assertIsNotNone(pad, "QMenu::item 缺少四值 padding")
        self.assertIsNotNone(icon, "QMenu::icon 缺少 left 定位")
        self.assertEqual(pad.group(1), icon.group(1))

    # ---------- 图标 ----------
    def test_icons_applied_to_actions(self):
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id=f1.id, icon="rocket"),
                 MiddleMenuItem(flow_id=f2.id, icon="star")]
        # 注意：QMenu 必须用变量接住。build_menu(...).actions() 这种写法里，
        # 临时 QMenu 在表达式求值后即被回收，其 QAction 一并销毁，断言时会报
        # 「Internal C++ object already deleted」。
        menu = build_menu(items, [f1, f2])
        acts = [a for a in menu.actions() if not a.isSeparator()]
        self.assertFalse(acts[0].icon().isNull())
        self.assertFalse(acts[1].icon().isNull())
        self.assertNotEqual(acts[0].icon().cacheKey(), acts[1].icon().cacheKey())

    def test_no_icon_keeps_column_empty_when_nobody_has_icon(self):
        """全部条目都没图标时不要补占位图标，否则文字会白白右移一列。"""
        flow = _flow("甲")
        menu = build_menu([MiddleMenuItem(flow_id=flow.id)], [flow])
        self.assertTrue(menu.actions()[0].icon().isNull())

    def test_iconless_item_gets_blank_placeholder_when_icons_mixed(self):
        """有图标也有无图标时，无图标的补透明占位图标，保证文字左边缘对齐。"""
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id=f1.id, icon="rocket"),
                 MiddleMenuItem(flow_id=f2.id)]              # 无图标
        menu = build_menu(items, [f1, f2])
        acts = [a for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(acts[0].icon().cacheKey() != blank_icon().cacheKey())
        self.assertEqual(acts[1].icon().cacheKey(), blank_icon().cacheKey())

    def test_unknown_icon_key_behaves_like_no_icon(self):
        """配置里写了个不认识的图标 key 时，按无图标处理，不能崩。"""
        flow = _flow("甲")
        menu = build_menu([MiddleMenuItem(flow_id=flow.id, icon="nope")], [flow])
        self.assertTrue(menu.actions()[0].icon().isNull())

    def test_unknown_key_alongside_real_icon_gets_placeholder(self):
        """真图标与未知 key 混用时：未知 key 等同于无图标，补占位图保证对齐。"""
        f1, f2 = _flow("甲"), _flow("乙")
        items = [MiddleMenuItem(flow_id=f1.id, icon="rocket"),
                 MiddleMenuItem(flow_id=f2.id, icon="nope")]
        menu = build_menu(items, [f1, f2])
        acts = [a for a in menu.actions() if not a.isSeparator()]
        self.assertFalse(acts[0].icon().isNull())
        self.assertEqual(acts[1].icon().cacheKey(), blank_icon().cacheKey())


if __name__ == "__main__":
    unittest.main()
