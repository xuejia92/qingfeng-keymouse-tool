"""app/hotkey_policy.py 的热键冲突检测测试。

集中校验所有「可设置快捷键」的地方不会重复设置同一个组合键：
show_hide / stop_all / clicker / presser / find_task:<id> / flow:<id>。
"""
from __future__ import annotations

import unittest

from app import hotkey_policy
from app.config import AppConfig, ClickerConfig, FindTask, Flow, PresserConfig


def _build_cfg() -> AppConfig:
    """构造一个带满各类热键的配置，便于验证收集与冲突判定。"""
    cfg = AppConfig()
    cfg.show_hide_hotkey = "shift+f1"
    cfg.stop_all_hotkey = "shift+f2"
    cfg.clicker = ClickerConfig(hotkey="f6")
    cfg.presser = PresserConfig(hotkey="f7")
    cfg.find_tasks = [FindTask(name="找图A", hotkey="f8")]
    cfg.flows = [Flow(name="流程B", hotkey="f9")]
    return cfg


class TestCollectHotkeys(unittest.TestCase):
    def test_collects_all_fixed_slots(self):
        cfg = _build_cfg()
        slots = {s: hk for s, _label, hk in hotkey_policy.collect_hotkeys(cfg)}
        # 槽位 -> 热键
        self.assertEqual(slots["show_hide"], "shift+f1")
        self.assertEqual(slots["stop_all"], "shift+f2")
        self.assertEqual(slots["clicker"], "f6")
        self.assertEqual(slots["presser"], "f7")

    def test_collects_find_task_and_flow_slots(self):
        cfg = _build_cfg()
        by_slot = {s: (label, hk) for s, label, hk in
                   hotkey_policy.collect_hotkeys(cfg)}
        find_slot = next(s for s in by_slot if s.startswith("find_task:"))
        flow_slot = next(s for s in by_slot if s.startswith("flow:"))
        self.assertEqual(by_slot[find_slot][1], "f8")
        self.assertEqual(by_slot[flow_slot][1], "f9")
        self.assertIn("找图A", by_slot[find_slot][0])
        self.assertIn("流程B", by_slot[flow_slot][0])

    def test_skips_empty_hotkeys(self):
        cfg = AppConfig()  # flows 默认空
        cfg.show_hide_hotkey = ""
        cfg.stop_all_hotkey = ""
        cfg.clicker = ClickerConfig(hotkey="")
        cfg.presser = PresserConfig(hotkey="")
        cfg.flows = [Flow(name="无热键流程", hotkey="")]
        cfg.find_tasks = [FindTask(name="无热键找图", hotkey="")]
        self.assertEqual(hotkey_policy.collect_hotkeys(cfg), [])

    def test_normalizes_case_and_space(self):
        cfg = AppConfig()
        cfg.clicker = ClickerConfig(hotkey="  Ctrl+F6 ")
        slots = {s: hk for s, _label, hk in hotkey_policy.collect_hotkeys(cfg)}
        self.assertEqual(slots["clicker"], "ctrl+f6")


class TestFindHotkeyConflict(unittest.TestCase):
    def test_no_conflict(self):
        cfg = _build_cfg()
        self.assertIsNone(hotkey_policy.find_hotkey_conflict("f10", cfg))

    def test_conflict_returns_owner_label(self):
        cfg = _build_cfg()
        self.assertEqual(hotkey_policy.find_hotkey_conflict("f6", cfg), "鼠标连点")

    def test_conflict_with_show_hide(self):
        cfg = _build_cfg()
        self.assertEqual(
            hotkey_policy.find_hotkey_conflict("shift+f1", cfg), "显示/隐藏窗口")

    def test_conflict_case_insensitive(self):
        cfg = _build_cfg()
        self.assertEqual(
            hotkey_policy.find_hotkey_conflict("Shift+F1", cfg), "显示/隐藏窗口")

    def test_empty_candidate_no_conflict(self):
        cfg = _build_cfg()
        self.assertIsNone(hotkey_policy.find_hotkey_conflict("", cfg))
        self.assertIsNone(hotkey_policy.find_hotkey_conflict(None, cfg))

    def test_exclude_self_slot_avoids_false_positive(self):
        """编辑自身时，旧值不算冲突（允许原地确认同一个键）。"""
        cfg = _build_cfg()
        self.assertIsNone(
            hotkey_policy.find_hotkey_conflict("f6", cfg, exclude_slot="clicker"))

    def test_exclude_slot_still_detects_other_owner(self):
        cfg = _build_cfg()
        # clicker 编辑时输入 f7（presser 的键），仍应报冲突
        self.assertEqual(
            hotkey_policy.find_hotkey_conflict("f7", cfg, exclude_slot="clicker"),
            "键盘连按")


class TestCheck(unittest.TestCase):
    def setUp(self):
        hotkey_policy.set_config(None)

    def tearDown(self):
        hotkey_policy.set_config(None)

    def test_no_config_injected_returns_none(self):
        self.assertIsNone(hotkey_policy.check("f6"))

    def test_detects_conflict_after_set_config(self):
        hotkey_policy.set_config(_build_cfg())
        self.assertEqual(hotkey_policy.check("f6"), "鼠标连点")

    def test_respects_exclude_slot(self):
        hotkey_policy.set_config(_build_cfg())
        self.assertIsNone(hotkey_policy.check("f6", exclude_slot="clicker"))

    def test_clear_config_disables_check(self):
        hotkey_policy.set_config(_build_cfg())
        self.assertIsNotNone(hotkey_policy.check("f6"))
        hotkey_policy.set_config(None)
        self.assertIsNone(hotkey_policy.check("f6"))


if __name__ == "__main__":
    unittest.main()
