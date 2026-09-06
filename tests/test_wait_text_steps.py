"""「等待文字出现」（wait_text）步骤的测试。

覆盖：类型注册/默认参数/摘要/序列化、ocr._fuzzy_match 近似匹配、ocr.find_text 的
tolerance 命中与 None 回归、run_wait_text_step（循环检测、命中写变量、超时、停止、
空文字、OCR 失败）、参数对话框构建/回填/校验/区域框选回填。
真实 OCR 全部规避（mock _grab_region_with_offset + _get_ocr / mock find_text）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import (FLOW_STEP_TYPES, STEP_OUTPUT_FIELDS, Flow, FlowStep,
                        default_step_params, flow_from_dict, flow_to_dict)


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("wait_text"), "等待文字出现")

    def test_default_params(self):
        p = default_step_params("wait_text")
        self.assertEqual(p["text"], "")
        self.assertEqual(p["region"], "")
        self.assertEqual(p["interval_sec"], 1.0)
        self.assertEqual(p["tolerance"], 0.8)
        self.assertEqual(p["timeout_sec"], 0.0)
        self.assertEqual(p["result_var"], "")
        self.assertEqual(p["pos_var"], "")

    def test_output_fields_registered(self):
        self.assertEqual(STEP_OUTPUT_FIELDS["wait_text"], ("result_var", "pos_var"))


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="wait_text")
        s.params.update(params)
        return s.summary()

    def test_shows_target(self):
        self.assertIn("等待文字", self._summary(text="登录"))
        self.assertIn("登录", self._summary(text="登录"))

    def test_empty_target(self):
        self.assertIn("未填文字", self._summary(text="   "))


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="等待流程", steps=[FlowStep(type="wait_text", name="等文字", params={
            "text": "$kw", "region": "10,20,100,50", "interval_sec": 0.5,
            "tolerance": 0.9, "timeout_sec": 30.0,
            "result_var": "txt", "pos_var": "xy",
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["text"], "$kw")
        self.assertEqual(p["region"], "10,20,100,50")
        self.assertEqual(p["tolerance"], 0.9)
        self.assertEqual(p["timeout_sec"], 30.0)


# ---------- ocr._fuzzy_match ----------

class TestFuzzyMatch(unittest.TestCase):
    def _f(self, keyword, line, tolerance):
        from app.ocr import _fuzzy_match
        return _fuzzy_match(keyword, line, tolerance)

    def test_exact_substring(self):
        self.assertTrue(self._f("确认", "点击确认按钮", 0.8))

    def test_near_match_tolerated(self):
        # "确队" vs "确认" 错一字，ratio = 0.5
        self.assertTrue(self._f("确认", "确队", 0.4))
        self.assertFalse(self._f("确认", "确队", 0.6))

    def test_sliding_window_within_long_line(self):
        # 目标词混在长行里，整行 ratio 被相邻字拉低，但窗口匹配仍命中
        self.assertTrue(self._f("登录", "欢迎使用登入系统继续", 0.5))

    def test_empty_inputs(self):
        self.assertFalse(self._f("", "abc", 0.8))
        self.assertFalse(self._f("abc", "", 0.8))

    def test_zero_tolerance_matches_any(self):
        self.assertTrue(self._f("登录", "完全不相关", 0.0))

    def test_substring_lower(self):
        # _fuzzy_match 接收 find_text 已 lower 的文本；大小写不敏感由 find_text 层保证
        self.assertTrue(self._f("ok", "点击 ok 按钮", 0.8))


# ---------- ocr.find_text tolerance ----------

class TestFindTextTolerance(unittest.TestCase):
    def _patch(self, texts):
        """mock 抓屏 + OCR，返回指定文本行（带简单 box）。"""
        items = []
        for i, t in enumerate(texts):
            items.append(([[i, 0], [i + 10, 0], [i + 10, 10], [i, 10]], t, 0.9))
        from app import ocr as ocr_actor

        def fake_grab(region):
            return object(), (5, 7)      # (img, offset)

        fake_ocr = mock.Mock()
        fake_ocr.return_value = (items, None)
        return (mock.patch.object(ocr_actor, "_grab_region_with_offset", side_effect=fake_grab),
                mock.patch.object(ocr_actor, "_get_ocr", return_value=fake_ocr),
                mock.patch.object(ocr_actor, "is_available", return_value=(True, "")))

    def test_tolerance_hits_near_match(self):
        from app import ocr as ocr_actor
        with self._patch(["点击确队按钮"])[0], self._patch(["点击确队按钮"])[1], \
             self._patch(["点击确队按钮"])[2]:
            ok, value, _ = ocr_actor.find_text(region="", text="确认", tolerance=0.5)
        self.assertTrue(ok)
        self.assertIsNotNone(value)
        self.assertEqual(value["text"], "点击确队按钮")

    def test_tolerance_none_keeps_substring_match(self):
        """tolerance=None 时保持「包含匹配」：确队不含「确认」→ 未找到（回归保护）。"""
        from app import ocr as ocr_actor
        with self._patch(["点击确队按钮"])[0], self._patch(["点击确队按钮"])[1], \
             self._patch(["点击确队按钮"])[2]:
            ok, value, _ = ocr_actor.find_text(region="", text="确认")
        self.assertTrue(ok)
        self.assertIsNone(value)

    def test_case_insensitive(self):
        """find_text 大小写不敏感（内部先 lower）。"""
        from app import ocr as ocr_actor
        with self._patch(["点击 ok 按钮"])[0], self._patch(["点击 ok 按钮"])[1], \
             self._patch(["点击 ok 按钮"])[2]:
            ok, value, _ = ocr_actor.find_text(region="", text="OK")
        self.assertTrue(ok)
        self.assertIsNotNone(value)


# ---------- run_wait_text_step ----------

class TestRunWaitTextStep(unittest.TestCase):
    def test_found_after_retry_writes_variables(self):
        from app.tasks import run_wait_text_step
        state = {"n": 0}

        def fake_find(region=None, text=None, tolerance=None):
            state["n"] += 1
            if state["n"] == 1:
                return True, None, "未找到文字"
            return True, {"x": 100, "y": 200, "text": "确认按钮", "score": 0.9}, "找到"

        with mock.patch("app.ocr.find_text", side_effect=fake_find):
            ok, why = run_wait_text_step(
                {"text": "确认", "interval_sec": 0.05,
                 "result_var": "r", "pos_var": "p"}, {})
        self.assertTrue(ok, why)
        self.assertEqual(state["n"], 2)          # 第二次命中
        self.assertIn("已出现", why)

    def test_writes_variables_on_hit(self):
        from app.tasks import run_wait_text_step
        vars_ = {}
        with mock.patch("app.ocr.find_text",
                        return_value=(True, {"x": 33, "y": 44, "text": "登录", "score": 1.0},
                                      "找到")):
            ok, _ = run_wait_text_step(
                {"text": "登录", "result_var": "txt", "pos_var": "xy"}, vars_)
        self.assertTrue(ok)
        self.assertEqual(vars_["txt"], "登录")
        self.assertEqual(vars_["xy"], "33,44")

    def test_timeout_fails(self):
        from app.tasks import run_wait_text_step
        with mock.patch("app.ocr.find_text", return_value=(True, None, "未找到")):
            ok, why = run_wait_text_step(
                {"text": "确认", "interval_sec": 0.05, "timeout_sec": 0.06}, {})
        self.assertFalse(ok)
        self.assertIn("超时", why)

    def test_stopped(self):
        import threading
        from app.tasks import run_wait_text_step
        stop = threading.Event()
        stop.set()
        with mock.patch("app.ocr.find_text") as ft:
            ok, why = run_wait_text_step({"text": "确认"}, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")
        ft.assert_not_called()

    def test_empty_text_fails(self):
        from app.tasks import run_wait_text_step
        with mock.patch("app.ocr.find_text") as ft:
            ok, why = run_wait_text_step({"text": "  "}, {})
        self.assertFalse(ok)
        self.assertIn("目标文字为空", why)
        ft.assert_not_called()

    def test_ocr_unavailable_fails(self):
        from app.tasks import run_wait_text_step
        with mock.patch("app.ocr.find_text",
                        return_value=(False, None, "缺少 RapidOCR 库")):
            ok, why = run_wait_text_step({"text": "确认"}, {})
        self.assertFalse(ok)
        self.assertIn("RapidOCR", why)

    def test_resolves_variable_in_text(self):
        from app.tasks import run_wait_text_step
        with mock.patch("app.ocr.find_text",
                        return_value=(True, {"x": 1, "y": 2, "text": "确认", "score": 1.0},
                                      "找到")) as ft:
            ok, _ = run_wait_text_step({"text": "$kw"}, {"kw": "确认"})
        self.assertTrue(ok)
        self.assertEqual(ft.call_args.kwargs["text"], "确认")


# ---------- 对话框 ----------

class TestWaitTextDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="wait_text", params=params))

    def test_defaults(self):
        dlg = self._open({})
        self.assertEqual(dlg.wt_text.text(), "")
        self.assertEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        self.assertAlmostEqual(dlg.wt_interval.value(), 1.0)
        self.assertAlmostEqual(dlg.wt_tolerance.value(), 0.8)
        self.assertAlmostEqual(dlg.wt_timeout.value(), 0.0)

    def test_apply_roundtrip(self):
        dlg = self._open({})
        dlg.wt_text.setText("登录")
        dlg._set_region_text("10,20,100,50")
        dlg.wt_interval.setValue(0.5)
        dlg.wt_tolerance.setValue(0.9)
        dlg.wt_timeout.setValue(30.0)
        dlg._set_combo_value(dlg.wt_result_var, "txt")
        dlg._set_combo_value(dlg.wt_pos_var, "xy")
        step = FlowStep(type="wait_text")
        dlg.apply_to(step)
        p = step.params
        self.assertEqual(p["text"], "登录")
        self.assertEqual(p["region"], "10,20,100,50")
        self.assertAlmostEqual(p["interval_sec"], 0.5)
        self.assertAlmostEqual(p["tolerance"], 0.9)
        self.assertAlmostEqual(p["timeout_sec"], 30.0)
        self.assertEqual(p["result_var"], "txt")
        self.assertEqual(p["pos_var"], "xy")

    def test_accept_requires_text(self):
        dlg = self._open({})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("目标文字", warn.call_args.args[1])

    def test_accept_passes_with_text(self):
        dlg = self._open({"text": "登录"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()

    def test_region_clear_restores_full_screen(self):
        dlg = self._open({"region": "10,20,100,50"})
        self.assertNotEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        dlg._set_region_text(None)
        self.assertEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        step = FlowStep(type="wait_text")
        dlg.apply_to(step)
        self.assertEqual(step.params["region"], "")


if __name__ == "__main__":
    unittest.main()
