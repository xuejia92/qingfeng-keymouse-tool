# -*- coding: utf-8 -*-
"""「状态日志」模块 + 浮层透明控制台（2026-10-01）。

覆盖：步骤类型注册与默认参数、摘要文案、执行时把内容推到浮层（按级别）、
空内容跳过、非法级别回退；浮层按级别着色、最多行数、关闭浮层时不输出；
设置页 overlay_values 带状态日志字段。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import running_overlay
from app.config import FLOW_STEP_TYPES, AppConfig, FlowStep, default_step_params
from app.running_overlay import (LOG_DEFAULT_COLOR, LOG_ERROR_COLOR,
                                 LOG_WARN_COLOR)
from app.tasks import run_status_log_step


class TestStatusLogRegistration(unittest.TestCase):
    """步骤类型注册（缺一处就会出现「拖进去不认 / 打开报未知步骤」）。"""

    def test_registered_in_flow_step_types(self):
        self.assertIn("status_log", FLOW_STEP_TYPES)
        self.assertEqual(FLOW_STEP_TYPES["status_log"], "状态日志")

    def test_default_params(self):
        p = default_step_params("status_log")
        self.assertEqual(p.get("text"), "")
        self.assertEqual(p.get("level"), "normal")
        # 参数键要与 apply_to 写回的键一致（否则编辑后参数丢失）
        self.assertEqual(set(p), {"text", "level"})

    def test_not_an_output_step(self):
        """状态日志不产出变量：不能进 STEP_OUTPUT_FIELDS（否则变量下拉会多出假变量）。"""
        from app.config import STEP_OUTPUT_FIELDS
        self.assertNotIn("status_log", STEP_OUTPUT_FIELDS)

    def test_summary_shows_level_and_text(self):
        step = FlowStep(type="status_log", name="状态日志",
                        params={"text": "开始执行", "level": "warn"})
        text = step.summary()
        self.assertIn("警告", text)
        self.assertIn("开始执行", text)

    def test_summary_empty_text(self):
        step = FlowStep(type="status_log", name="状态日志",
                        params={"text": "", "level": "normal"})
        self.assertIn("普通", step.summary())


class TestRunStatusLogStep(unittest.TestCase):
    """执行函数：把（解析过变量的）内容按级别推到浮层。"""

    def test_pushes_resolved_text_with_level(self):
        calls: list = []
        with mock.patch.object(running_overlay, "append_status",
                               lambda t, lvl="normal": calls.append((t, lvl))):
            ok, msg = run_status_log_step(
                {"text": "计数 $n 次", "level": "error"}, {"n": 5})
        self.assertTrue(ok)
        self.assertEqual(calls, [("计数 5 次", "error")])
        self.assertIn("错误", msg)

    def test_empty_text_is_skipped(self):
        calls: list = []
        with mock.patch.object(running_overlay, "append_status",
                               lambda t, lvl="normal": calls.append((t, lvl))):
            ok, _ = run_status_log_step({"text": "   ", "level": "normal"}, {})
        self.assertTrue(ok)          # 空内容不算失败
        self.assertEqual(calls, [])

    def test_invalid_level_falls_back_to_normal(self):
        calls: list = []
        with mock.patch.object(running_overlay, "append_status",
                               lambda t, lvl="normal": calls.append((t, lvl))):
            run_status_log_step({"text": "x", "level": "nonsense"}, {})
        self.assertEqual(calls[0][1], "normal")

    def test_escapes_are_unescaped(self):
        calls: list = []
        with mock.patch.object(running_overlay, "append_status",
                               lambda t, lvl="normal": calls.append((t, lvl))):
            run_status_log_step({"text": "第一行\\n第二行", "level": "normal"}, {})
        self.assertEqual(calls[0][0], "第一行\n第二行")


class TestOverlayStatusConsole(unittest.TestCase):
    """浮层的透明控制台：按级别着色、只留最近 N 条、关开关时不输出。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        running_overlay.close()
        running_overlay._cfg = AppConfig()

    def tearDown(self):
        running_overlay.close()
        running_overlay._cfg = None

    def test_appends_with_level_color(self):
        running_overlay.append_status("普通消息", "normal")
        ov = running_overlay._status_overlay
        self.assertIsNotNone(ov)
        self.assertIn(LOG_DEFAULT_COLOR.lower(),
                      ov._log_box.toHtml().lower())
        running_overlay.append_status("出错了", "error")
        self.assertIn(LOG_ERROR_COLOR.lower(),
                      ov._log_box.toHtml().lower())
        running_overlay.append_status("注意", "warn")
        self.assertIn(LOG_WARN_COLOR.lower(),
                      ov._log_box.toHtml().lower())

    def test_level_prefix_marks(self):
        running_overlay.append_status("小心", "warn")
        self.assertIn("⚠", running_overlay._status_overlay._log_box.toPlainText())
        running_overlay.append_status("坏了", "error")
        self.assertIn("✕", running_overlay._status_overlay._log_box.toPlainText())

    def test_keeps_only_max_lines(self):
        cfg = AppConfig()
        cfg.run_overlay_log_max_lines = 3
        running_overlay._cfg = cfg
        for i in range(5):
            running_overlay.append_status(f"消息{i}")
        ov = running_overlay._status_overlay
        self.assertEqual(len(ov._log_lines), 3)
        text = ov._log_box.toHtml()
        self.assertIn("消息4", text)
        self.assertNotIn("消息1", text)

    def test_html_special_chars_escaped(self):
        running_overlay.append_status("<b>不该变粗</b>", "normal")
        self.assertIn("&lt;b&gt;", running_overlay._status_overlay._log_box.toHtml())

    def test_long_text_truncated(self):
        running_overlay.append_status("长" * 400, "normal")
        body = running_overlay._status_overlay._log_lines[0][1]
        self.assertLessEqual(len(body), running_overlay.MAX_STATUS_CHARS)

    def test_no_output_when_status_log_disabled(self):
        """状态日志是独立浮层：由 run_overlay_log_enabled 控制（与流程概览开关无关）。"""
        cfg = AppConfig()
        cfg.run_overlay_log_enabled = False
        running_overlay._cfg = cfg
        running_overlay.append_status("不该出现")
        self.assertIsNone(running_overlay._status_overlay)

    def test_messages_accumulate_across_hide_and_show(self):
        """消息只累加：浮层被关掉/自动收起过，也不清掉本次运行已输出的内容。"""
        running_overlay.append_status("一")
        sl = running_overlay._status_overlay
        sl._close_btn.click()                       # 关掉
        running_overlay.append_status("二")          # 重新显示
        sl._on_auto_hide()                          # 自动收起
        running_overlay.append_status("三")
        self.assertEqual([x[1] for x in sl._log_lines], ["一", "二", "三"])

    def test_clear_status_starts_a_new_round(self):
        """流程重新执行时调用 clear_status：消息清空、浮层收起。"""
        running_overlay.append_status("上一轮")
        sl = running_overlay._status_overlay
        self.assertTrue(sl.isVisible())
        running_overlay.clear_status()
        self.assertEqual(sl._log_lines, [])
        self.assertFalse(sl.isVisible())
        running_overlay.append_status("本轮第一条")
        self.assertEqual([x[1] for x in sl._log_lines], ["本轮第一条"])

    def test_clear_status_empties_console(self):
        running_overlay.append_status("一")
        running_overlay.append_status("二")
        running_overlay.clear_status()
        ov = running_overlay._status_overlay
        self.assertEqual(ov._log_lines, [])
        self.assertEqual(ov._log_box.toPlainText(), "")

    def test_style_follows_config(self):
        cfg = AppConfig()
        cfg.run_overlay_log_font_size = 20
        cfg.run_overlay_log_font_family = "Courier New"
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        qss = running_overlay._status_overlay.styleSheet()
        self.assertIn("QLabel#statusLog", qss)
        self.assertIn("20px", qss)
        self.assertIn("Courier New", qss)


class TestSettingsOverlayValues(unittest.TestCase):
    """设置页：overlay_values 要带上状态日志那几个字段（主窗口据此写回 cfg）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_values_include_status_log_fields(self):
        from app.ui.settings_tab import SettingsTab
        tab = SettingsTab(AppConfig())
        vals = tab.overlay_values()
        for key in ("log_font_size", "log_font_family", "log_color",
                    "log_warn_color", "log_error_color", "log_max_lines",
                    "log_enabled", "log_pos", "log_bg_color",
                    "log_max_width", "log_max_height", "log_auto_hide_sec",
                    "log_bg_transparent"):
            self.assertIn(key, vals, f"overlay_values 缺 {key}")

    def test_values_reflect_config(self):
        from app.ui.settings_tab import SettingsTab
        cfg = AppConfig()
        cfg.run_overlay_log_font_size = 18
        cfg.run_overlay_log_max_lines = 5
        tab = SettingsTab(cfg)
        vals = tab.overlay_values()
        self.assertEqual(vals["log_font_size"], 18)
        self.assertEqual(vals["log_max_lines"], 5)

    def test_defaults_are_independent_overlay(self):
        """默认：勾选展示、左下角、半透明黑背景（独立于流程概览）。"""
        from app.ui.settings_tab import SettingsTab
        from app.running_overlay import LOG_DEFAULT_BG, LOG_DEFAULT_POS
        tab = SettingsTab(AppConfig())
        vals = tab.overlay_values()
        self.assertTrue(vals["log_enabled"])
        self.assertEqual(vals["log_pos"], LOG_DEFAULT_POS)
        self.assertEqual(vals["log_bg_color"].lower(), LOG_DEFAULT_BG.lower())


class TestStatusOverlayIndependence(unittest.TestCase):
    """状态日志浮层与流程概览浮层**互相独立**（用户要求：不要放一起）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        running_overlay.close()
        running_overlay._cfg = AppConfig()

    def tearDown(self):
        running_overlay.close()
        running_overlay._cfg = None

    def test_separate_windows(self):
        running_overlay.refresh({"source": "single", "group": "",
                                 "flows": [("甲", "f6", False)]})
        running_overlay.append_status("状态一")
        ov = running_overlay._overlay
        sl = running_overlay._status_overlay
        self.assertIsNotNone(ov)
        self.assertIsNotNone(sl)
        self.assertIsNot(ov, sl)                    # 两个独立窗口
        # 状态日志不写进流程概览浮层
        self.assertFalse(hasattr(ov, "_log_box"))
        self.assertIn("状态一", sl._log_box.toPlainText())
        self.assertTrue(running_overlay.is_visible())
        self.assertTrue(running_overlay.is_status_visible())

    def test_log_overlay_survives_overview_hide(self):
        """流程全部结束时流程浮层隐藏，但状态日志浮层不受影响。"""
        running_overlay.refresh({"source": "single", "group": "",
                                 "flows": [("甲", "f6", False)]})
        running_overlay.append_status("还在跑")
        running_overlay.refresh({"source": "", "group": "", "flows": []})
        self.assertFalse(running_overlay.is_visible())        # 流程概览收起
        self.assertTrue(running_overlay.is_status_visible())  # 日志浮层仍在

    def test_log_position_independent(self):
        cfg = AppConfig()
        cfg.run_overlay_pos = "top_left"
        cfg.run_overlay_log_pos = "bottom_right"
        running_overlay._cfg = cfg
        running_overlay.refresh({"source": "single", "group": "",
                                 "flows": [("甲", "f6", False)]})
        running_overlay.append_status("x")
        self.assertEqual(running_overlay._overlay._pos_key, "top_left")
        self.assertEqual(running_overlay._status_overlay._pos_key, "bottom_right")

    def test_default_position_and_bg(self):
        from app.running_overlay import LOG_DEFAULT_BG, LOG_DEFAULT_POS
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertEqual(sl._pos_key, LOG_DEFAULT_POS)     # 默认左下角
        self.assertIn("rgba(0,0,0,", sl.styleSheet())      # 默认半透明黑背景

    def test_custom_bg_applied(self):
        cfg = AppConfig()
        cfg.run_overlay_log_bg_color = "#112233cc"
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        self.assertIn("rgba(17,34,51,204)",
                      running_overlay._status_overlay.styleSheet())

    def test_fixed_size_does_not_follow_content(self):
        """浮层尺寸固定为设置值，不随文字多少变化（2026-10-01 用户要求）。"""
        cfg = AppConfig()
        cfg.run_overlay_log_max_width = 300
        cfg.run_overlay_log_max_height = 200
        running_overlay._cfg = cfg
        running_overlay.append_status("短")
        sl = running_overlay._status_overlay
        self.assertEqual((sl.width(), sl.height()), (300, 200))
        running_overlay.append_status("很长的一句状态日志" * 30)
        self.assertEqual((sl.width(), sl.height()), (300, 200))   # 加了长消息也不变
        # 超宽仍然自动换行（QTextEdit 按控件宽度折行）
        from PySide6.QtWidgets import QTextEdit
        self.assertEqual(sl._log_box.lineWrapMode(), QTextEdit.WidgetWidth)

    def test_default_fixed_size(self):
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertEqual((sl.width(), sl.height()), (320, 180))

    def test_messages_never_auto_cleared(self):
        """内容不再自动清除：跨自动收起、跨运行都只累加。"""
        running_overlay.append_status("一")
        sl = running_overlay._status_overlay
        sl._on_auto_hide()                      # 自动收起
        running_overlay.append_status("二")
        running_overlay.reset_for_new_run()      # 下次流程运行
        running_overlay.append_status("三")
        self.assertEqual([x[1] for x in sl._log_lines], ["一", "二", "三"])

    def test_transparent_background_option(self):
        """勾选「透明背景」后浮层完全透明（忽略背景色）。"""
        cfg = AppConfig()
        cfg.run_overlay_log_bg_transparent = True
        cfg.run_overlay_log_bg_color = "#112233cc"      # 就算配了颜色也忽略
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        self.assertIn("background: transparent",
                      running_overlay._status_overlay.styleSheet())

    def test_drag_handle_clickable_and_saves_position(self):
        """拖动把手可接收鼠标（不穿透）；拖完把位置写进配置且不碰 flows。"""
        from PySide6.QtCore import Qt
        cfg = AppConfig()
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertFalse(
            sl._drag_handle.testAttribute(Qt.WA_TransparentForMouseEvents))
        saved = {}
        real_save = cfg.save
        try:
            cfg.save = lambda save_flows=True: saved.update(flows=save_flows)
            sl.move(111, 222)
            sl.on_drag_finished()
        finally:
            cfg.save = real_save
        self.assertEqual(cfg.run_overlay_log_custom_pos, "111,222")
        self.assertEqual(saved.get("flows"), False)     # save_flows=False

    def test_custom_position_overrides_grid(self):
        """手动拖过的位置优先于九宫格设置。"""
        cfg = AppConfig()
        cfg.run_overlay_log_pos = "top_left"
        cfg.run_overlay_log_custom_pos = "777,888"
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertEqual((sl.x(), sl.y()), (777, 888))

    def test_invalid_custom_position_falls_back_to_grid(self):
        """手改配置里写坏的位置：忽略，回落到九宫格定位。"""
        cfg = AppConfig()
        cfg.run_overlay_log_pos = "bottom_right"
        cfg.run_overlay_log_custom_pos = "abc"
        running_overlay._cfg = cfg
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertNotEqual((sl.x(), sl.y()), (0, 0))
        self.assertEqual(sl._pos_key, "bottom_right")

    def test_smart_scroll_follows_only_at_bottom(self):
        """智能滚动：停在底部才跟随最新；往上翻就不打扰（2026-10-01 用户要求）。"""
        from PySide6.QtCore import Qt
        cfg = AppConfig()
        cfg.run_overlay_log_max_lines = 50      # 让消息真正超出框高、出现滚动条
        running_overlay._cfg = cfg
        for i in range(30):
            running_overlay.append_status(f"消息{i}")
        sl = running_overlay._status_overlay
        for _ in range(4):
            self._app.processEvents()      # 让 QTextEdit 完成文档布局
        sb = sl._log_box.verticalScrollBar()
        self.assertGreater(sb.maximum(), 0)            # 有可滚动范围
        self.assertGreaterEqual(sb.value(), sb.maximum() - 4)   # 默认在底部
        # 往上翻到顶部后，新消息不该把它拉回底部
        sb.setValue(0)
        for _ in range(2):
            self._app.processEvents()      # 让滚动条位置先稳定下来
        running_overlay.append_status("顶部浏览时的新消息")
        for _ in range(2):
            self._app.processEvents()
        self.assertLessEqual(sb.value(), 5)
        # 滚回底部后恢复跟随
        sb.setValue(sb.maximum())
        for _ in range(2):
            self._app.processEvents()
        running_overlay.append_status("底部时的新消息")
        for _ in range(2):
            self._app.processEvents()
        self.assertGreaterEqual(sb.value(), sb.maximum() - 4)

    def test_scrollbar_as_needed_at_right_edge(self):
        """滚动条按需显示、贴最右缘（按钮已移到上方工具条）。"""
        from PySide6.QtCore import Qt
        cfg = AppConfig()
        cfg.run_overlay_log_max_lines = 50
        running_overlay._cfg = cfg
        for i in range(30):                 # 发够多，内容超出固定框高
            running_overlay.append_status(f"消息{i}")
        sl = running_overlay._status_overlay
        self.assertEqual(
            sl._log_box.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        for _ in range(3):
            self._app.processEvents()
        sb = sl._log_box.verticalScrollBar()
        self.assertGreater(sb.maximum(), 0)      # 内容超出固定框：可滚动
        # 滚动条贴消息区右缘（与上方按钮列对齐）；距浮层右缘只剩内边距 12px
        #（卡片有 10px 圆角，内容不能贴到圆角边上，12px 是合理的"最右"）
        sb_right = sl._log_box.geometry().right()   # 浮层坐标系
        self.assertLessEqual(sl.width() - sb_right, 14)

    def test_suppress_hides_overlay_when_no_status_log(self):
        """本轮流程没有状态日志模块：隐藏浮层，不显示上一轮留下的空框。"""
        running_overlay.append_status("上一轮消息")
        sl = running_overlay._status_overlay
        self.assertTrue(sl.isVisible())
        running_overlay.suppress_status()
        self.assertFalse(sl.isVisible())
        self.assertEqual(len(sl._log_lines), 1)      # 内容不清，只是不显示

    def test_manual_close_stays_hidden_until_next_run(self):
        """手动关掉后本轮不再弹出（消息照记），下次运行流程才恢复显示。"""
        running_overlay.append_status("一")
        sl = running_overlay._status_overlay
        sl._close_btn.click()
        self.assertFalse(sl.isVisible())
        running_overlay.append_status("二")          # 新消息也不再弹出
        self.assertFalse(sl.isVisible())
        self.assertEqual([x[1] for x in sl._log_lines], ["一", "二"])
        running_overlay.reset_for_new_run()          # 下次运行流程
        self.assertTrue(sl.isVisible())

    def test_close_button_clickable_others_transparent(self):
        """窗口整体穿透；消息区为接收滚轮**不穿透**；✕/☰ 可点。"""
        from PySide6.QtCore import Qt
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        self.assertTrue(sl.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertFalse(
            sl._log_box.testAttribute(Qt.WA_TransparentForMouseEvents))  # 要滚轮
        self.assertFalse(
            sl._close_btn.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertFalse(
            sl._drag_handle.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_countdown_only_in_last_10_seconds(self):
        """倒计时数字**只在剩余最后 10 秒时**才出现（用户要求）。"""
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        sl._update_countdown()                    # 默认 60 秒：还早，不显示
        self.assertEqual(sl._countdown.text(), "")
        sl._auto_hide.start(30 * 1000)            # 剩 30 秒：仍不显示
        sl._update_countdown()
        self.assertEqual(sl._countdown.text(), "")
        sl._auto_hide.start(9500)                 # 剩不到 10 秒：出现
        sl._update_countdown()
        self.assertRegex(sl._countdown.text(), r"^\d+s$")
        self.assertLessEqual(int(sl._countdown.text().rstrip("s")), 10)
        # 位置在关闭叉左边（同一工具条行内，布局顺序）
        bar = sl._toolbar
        idx_cd = bar.indexOf(sl._countdown)
        idx_close = bar.indexOf(sl._close_btn)
        self.assertLess(idx_cd, idx_close)

    def test_auto_hide_seconds_default_and_configurable(self):
        """自动隐藏秒数：默认 60，可在设置里改。"""
        running_overlay._cfg = AppConfig()
        self.assertEqual(
            running_overlay._log_live_values()["auto_hide_sec"], 60)
        cfg = AppConfig()
        cfg.run_overlay_log_auto_hide_sec = 30
        running_overlay._cfg = cfg
        self.assertEqual(
            running_overlay._log_live_values()["auto_hide_sec"], 30)
        running_overlay.append_status("x")
        self.assertLessEqual(
            running_overlay._status_overlay._auto_hide.remainingTime(), 30000)

    def test_auto_hide_keeps_messages(self):
        """自动收起只隐藏窗口：消息保留、倒计时清掉（清空只在流程重新执行时）。"""
        running_overlay.append_status("x")
        sl = running_overlay._status_overlay
        sl._on_auto_hide()
        self.assertEqual([x[1] for x in sl._log_lines], ["x"])
        self.assertEqual(sl._countdown.text(), "")
        self.assertFalse(sl.isVisible())


if __name__ == "__main__":
    unittest.main()
