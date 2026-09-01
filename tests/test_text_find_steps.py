"""文字查找步骤 + 鼠标点击坐标变量的测试。

覆盖：ocr.find_text（mock RapidOCR 返回带坐标结果）、tasks 层 run_text_find_step
（找到点击 / 返回坐标 / 未找到写 false / 变量引用）、run_click_step 坐标变量解析、
config 默认参数与摘要、步骤编辑对话框表单与只读变量下拉。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import ocr as ocr_actor
from app.config import FlowStep, default_step_params
from app.tasks import run_click_step, run_text_find_step


# ---------- ocr.find_text ----------

class TestOcrFindText(unittest.TestCase):
    """ocr.find_text 的坐标定位与匹配逻辑（mock RapidOCR 返回）。"""

    def _fake_result(self):
        # 两条文本：第一条不含关键词，第二条含
        return [
            ([[0, 0], [100, 0], [100, 20], [0, 20]], "Hello World", 0.95),
            ([[200, 40], [300, 40], [300, 60], [200, 60]], "确认按钮", 0.92),
        ]

    def _patch_grab(self, offset=(0, 0)):
        return mock.patch("app.ocr._grab_region_with_offset",
                          return_value=(object(), offset))

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_finds_text_and_center(self, _av):
        with self._patch_grab(), \
             mock.patch.object(ocr_actor, "_get_ocr") as get_ocr:
            get_ocr.return_value = mock.Mock(return_value=(self._fake_result(), 0.1))
            ok, value, why = ocr_actor.find_text(region="0,0,800,600", text="确认")
        self.assertTrue(ok)
        self.assertEqual(value["x"], 250)
        self.assertEqual(value["y"], 50)
        self.assertEqual(value["text"], "确认按钮")
        self.assertAlmostEqual(value["score"], 0.92)
        self.assertIn("找到", why)

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_case_insensitive_english(self, _av):
        with self._patch_grab(), \
             mock.patch.object(ocr_actor, "_get_ocr") as get_ocr:
            get_ocr.return_value = mock.Mock(return_value=(self._fake_result(), 0.1))
            ok, value, _ = ocr_actor.find_text(text="hello")
        self.assertTrue(ok)
        self.assertEqual(value["x"], 50)

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_region_offset_added_to_center(self, _av):
        """区域查找：OCR 相对坐标要加上区域左上角偏移，转成屏幕绝对坐标。"""
        with self._patch_grab(offset=(100, 50)), \
             mock.patch.object(ocr_actor, "_get_ocr") as get_ocr:
            get_ocr.return_value = mock.Mock(return_value=(self._fake_result(), 0.1))
            ok, value, why = ocr_actor.find_text(region="100,50,800,600", text="确认")
        self.assertTrue(ok)
        self.assertEqual(value["x"], 350)          # 250 + 100
        self.assertEqual(value["y"], 100)          # 50 + 50
        self.assertIn("350, 100", why)

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_fullscreen_negative_offset(self, _av):
        """全屏且虚拟桌面带负偏移（多显示器）：坐标同样转为屏幕绝对坐标。"""
        with self._patch_grab(offset=(-1920, 0)), \
             mock.patch.object(ocr_actor, "_get_ocr") as get_ocr:
            get_ocr.return_value = mock.Mock(return_value=(self._fake_result(), 0.1))
            ok, value, _ = ocr_actor.find_text(text="确认")
        self.assertTrue(ok)
        self.assertEqual(value["x"], -1670)        # 250 - 1920
        self.assertEqual(value["y"], 50)

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_not_found_returns_none(self, _av):
        with self._patch_grab(), \
             mock.patch.object(ocr_actor, "_get_ocr") as get_ocr:
            get_ocr.return_value = mock.Mock(return_value=(self._fake_result(), 0.1))
            ok, value, why = ocr_actor.find_text(text="不存在的文字")
        self.assertTrue(ok)
        self.assertIsNone(value)
        self.assertIn("未找到", why)

    @mock.patch("app.ocr.is_available", return_value=(True, ""))
    def test_empty_keyword_fails(self, _av):
        ok, value, why = ocr_actor.find_text(text="   ")
        self.assertFalse(ok)
        self.assertIn("为空", why)

    @mock.patch("app.ocr.is_available", return_value=(False, "缺少 RapidOCR"))
    def test_ocr_unavailable(self, _av):
        ok, _, why = ocr_actor.find_text(text="x")
        self.assertFalse(ok)
        self.assertIn("RapidOCR", why)


# ---------- tasks.run_text_find_step ----------

class TestRunTextFindStep(unittest.TestCase):
    """步骤执行：找到点击 / 返回坐标 / 未找到 false / 变量引用。"""

    def _patch_find(self, value):
        return mock.patch("app.ocr.find_text", return_value=(True, value, "ok"))

    def test_found_click_left_no_var_write(self):
        vars_store = {}
        with self._patch_find({"x": 100, "y": 200, "text": "确认", "score": 0.9}), \
             mock.patch("app.tasks.input_actors.click") as click:
            ok, why = run_text_find_step(
                {"text": "确认", "click": True, "click_button": "left",
                 "variable": "v"}, vars_store)
        self.assertTrue(ok)
        click.assert_called_once_with("left", 1, 100, 200)
        self.assertEqual(vars_store, {})          # 点击模式不写坐标

    def test_found_click_right(self):
        with self._patch_find({"x": 1, "y": 2, "text": "OK", "score": 0.8}), \
             mock.patch("app.tasks.input_actors.click") as click:
            ok, _ = run_text_find_step(
                {"text": "OK", "click": True, "click_button": "right", "variable": ""}, {})
        self.assertTrue(ok)
        click.assert_called_once_with("right", 1, 1, 2)

    def test_found_returns_coords_to_var(self):
        vars_store = {}
        with self._patch_find({"x": 30, "y": 40, "text": "OK", "score": 0.8}):
            ok, why = run_text_find_step(
                {"text": "OK", "click": False, "variable": "pos"}, vars_store)
        self.assertTrue(ok)
        self.assertEqual(vars_store["pos"], "30,40")
        self.assertIn("30, 40", why)

    def test_not_found_writes_false_and_not_fail(self):
        vars_store = {}
        with self._patch_find(None):
            ok, why = run_text_find_step(
                {"text": "x", "click": False, "variable": "hit"}, vars_store)
        self.assertTrue(ok)                       # 未找到不视为失败
        self.assertIs(vars_store["hit"], False)
        self.assertIn("未找到", why)

    def test_variable_reference_in_text(self):
        with self._patch_find(None) as find:
            run_text_find_step({"text": "$kw", "variable": "v"}, {"kw": "目标文字"})
        self.assertEqual(find.call_args.kwargs["text"], "目标文字")

    def test_empty_text_fails(self):
        ok, why = run_text_find_step({"text": "  "}, {})
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_ocr_failure_is_step_failure(self):
        with mock.patch("app.ocr.find_text", return_value=(False, None, "OCR 挂了")):
            ok, why = run_text_find_step({"text": "x", "variable": "v"}, {})
        self.assertFalse(ok)
        self.assertIn("OCR", why)

    def test_variable_optional_no_write(self):
        """结果变量可不设置：找到/未找到时都不写变量，步骤正常成功。"""
        vars_store = {}
        with self._patch_find({"x": 10, "y": 20, "text": "OK", "score": 0.8}):
            ok, why = run_text_find_step({"text": "OK", "click": False, "variable": ""},
                                         vars_store)
        self.assertTrue(ok)
        self.assertEqual(vars_store, {})          # 未设变量，不写任何东西
        with self._patch_find(None):
            ok, why = run_text_find_step({"text": "OK", "click": False, "variable": ""},
                                         vars_store)
        self.assertTrue(ok)                       # 未找到也不失败
        self.assertIn("未找到", why)
        self.assertEqual(vars_store, {})


# ---------- tasks.run_click_step 坐标变量 ----------

class TestClickStepVariableCoords(unittest.TestCase):
    """固定坐标模式下 pos_var（"x,y" 字符串）的解析。"""

    def _params(self, **over):
        p = {"fixed_position": True, "pos_x": 1, "pos_y": 2,
             "pos_var": "",
             "mouse_button": "left", "click_type": "single",
             "interval_ms": 100, "count": 1, "duration_sec": 0}
        p.update(over)
        return p

    def _run(self, p, variables):
        stop = threading.Event()
        with mock.patch("app.tasks.input_actors.click") as click:
            reason = run_click_step(p, stop, lambda d, e: None, variables)
        return click, reason

    def test_uses_variable_value(self):
        click, reason = self._run(
            self._params(pos_var="pos"), {"pos": "64,63"})
        self.assertIn("已完成", reason)
        click.assert_called_once_with("left", 1, 64, 63)

    def test_uses_variable_value_with_spaces(self):
        click, _ = self._run(
            self._params(pos_var="pos"), {"pos": " 100, 200 "})
        click.assert_called_once_with("left", 1, 100, 200)

    def test_uses_variable_value_float(self):
        click, _ = self._run(
            self._params(pos_var="pos"), {"pos": "12.5,600"})
        click.assert_called_once_with("left", 1, 12, 600)

    def test_undefined_var_falls_back_to_numeric(self):
        click, reason = self._run(self._params(pos_var="pos"), {})
        self.assertIn("已完成", reason)
        click.assert_called_once_with("left", 1, 1, 2)

    def test_bad_format_falls_back_to_numeric(self):
        click, _ = self._run(self._params(pos_var="pos"), {"pos": "abc"})
        click.assert_called_once_with("left", 1, 1, 2)

    def test_no_variables_uses_numeric(self):
        click, _ = self._run(self._params(), None)
        click.assert_called_once_with("left", 1, 1, 2)

    def test_uses_region_value_takes_center(self):
        """区域 \"x1,y1,x2,y2\"（找图模块结果）→ 取中心点。"""
        click, reason = self._run(
            self._params(pos_var="pos"), {"pos": "100,200,400,500"})
        self.assertIn("已完成", reason)
        click.assert_called_once_with("left", 1, 250, 350)

    def test_uses_region_value_odd_center(self):
        """奇数宽高的区域中心向下取整。"""
        click, _ = self._run(
            self._params(pos_var="pos"), {"pos": "10,20,31,41"})
        click.assert_called_once_with("left", 1, 20, 30)

    def test_uses_region_value_from_list(self):
        """列表 [x1,y1,x2,y2] 同样取中心。"""
        click, _ = self._run(
            self._params(pos_var="pos"), {"pos": [100, 200, 400, 500]})
        click.assert_called_once_with("left", 1, 250, 350)

    def test_three_parts_falls_back_to_numeric(self):
        """3 个数字（非法格式）回退固定坐标。"""
        click, _ = self._run(self._params(pos_var="pos"), {"pos": "1,2,3"})
        click.assert_called_once_with("left", 1, 1, 2)


# ---------- config ----------

class TestTextFindConfig(unittest.TestCase):
    """config 层的默认参数 / 步骤校验 / 摘要。"""

    def test_default_params(self):
        p = default_step_params("text_find")
        self.assertEqual(set(p), {"text", "region", "click", "click_button", "variable"})
        self.assertFalse(p["click"])

    def test_click_has_var_fields(self):
        p = default_step_params("click")
        self.assertIn("pos_var", p)
        self.assertNotIn("pos_x_var", p)

    def test_flowstep_roundtrip(self):
        s = FlowStep(type="text_find", params={"text": "确认"})
        self.assertEqual(s.name, "文字查找")
        self.assertIn("确认", s.summary())
        self.assertIn("返回坐标", s.summary())
        s2 = FlowStep(type="text_find", params={"text": "确认", "click": True})
        self.assertIn("点击", s2.summary())

    def test_click_summary_shows_var(self):
        s = FlowStep(type="click", params={"fixed_position": True,
                                           "pos_var": "pos"})
        self.assertIn("pos", s.summary())
        s2 = FlowStep(type="click", params={"fixed_position": True})
        self.assertNotIn("坐标", s2.summary())

    def test_click_summary_legacy_var_fields(self):
        # 兼容旧配置：pos_x_var / pos_y_var 仍显示
        s = FlowStep(type="click", params={"fixed_position": True,
                                           "pos_x_var": "px", "pos_y_var": "py"})
        self.assertIn("px", s.summary())
        self.assertIn("py", s.summary())


# ---------- 对话框 ----------

class TestTextFindDialog(unittest.TestCase):
    """步骤编辑对话框：文字查找表单构建/回填/收集、变量下拉只读。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, step_type: str, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type=step_type, params=params))

    def test_text_find_form_roundtrip(self):
        dlg = self._open("text_find", {"text": "确认", "region": "10,20,100,50",
                                       "click": True, "click_button": "right",
                                       "variable": ""})
        self.assertEqual(dlg.tf_text.text(), "确认")
        self.assertTrue(dlg.tf_click.isChecked())
        self.assertEqual(dlg.tf_button.currentData(), "right")
        self.assertTrue(dlg.tf_button.isEnabled())     # 勾选点击后按键可选
        step = FlowStep(type="text_find")
        dlg.apply_to(step)
        self.assertEqual(step.params["text"], "确认")
        self.assertEqual(step.params["click_button"], "right")
        self.assertTrue(step.params["click"])

    def test_text_find_click_disables_button(self):
        dlg = self._open("text_find", {"text": "x", "click": False})
        self.assertFalse(dlg.tf_button.isEnabled())
        dlg.tf_click.setChecked(True)
        self.assertTrue(dlg.tf_button.isEnabled())

    def test_click_var_coords_roundtrip(self):
        dlg = self._open("click", {"fixed_position": True, "pos_var": "pos"})
        self.assertTrue(dlg.var_radio.isChecked())
        self.assertFalse(dlg.fixed_radio.isChecked())
        self.assertEqual(dlg.pos_var.currentText(), "pos")
        step = FlowStep(type="click")
        dlg.apply_to(step)
        self.assertTrue(step.params["fixed_position"])
        self.assertEqual(step.params["pos_var"], "pos")

    def test_click_fixed_coords_roundtrip(self):
        dlg = self._open("click", {"fixed_position": True, "pos_var": ""})
        self.assertTrue(dlg.fixed_radio.isChecked())
        self.assertFalse(dlg.var_radio.isChecked())
        step = FlowStep(type="click")
        dlg.apply_to(step)
        self.assertTrue(step.params["fixed_position"])
        self.assertEqual(step.params["pos_var"], "")

    def test_click_follow_roundtrip(self):
        dlg = self._open("click", {"fixed_position": False})
        self.assertTrue(dlg.follow_radio.isChecked())
        step = FlowStep(type="click")
        dlg.apply_to(step)
        self.assertFalse(step.params["fixed_position"])
        self.assertEqual(step.params["pos_var"], "")

    def test_var_coords_requires_variable(self):
        """选中「变量坐标」但没选变量时，确定被拦截并提示。"""
        dlg = self._open("click", {"fixed_position": True, "pos_var": ""})
        dlg.var_radio.setChecked(True)      # 切到变量坐标但不选变量
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertTrue(dlg.var_radio.isChecked())   # 对话框未关闭

    def test_pos_rows_visibility_switch(self):
        """三选一联动：切换单选时对应的控件行显示/隐藏。"""
        dlg = self._open("click", {"fixed_position": True, "pos_var": ""})
        self.assertFalse(dlg._fixed_pos_widget.isHidden())
        self.assertTrue(dlg._var_pos_widget.isHidden())
        dlg.var_radio.setChecked(True)
        self.assertTrue(dlg._fixed_pos_widget.isHidden())
        self.assertFalse(dlg._var_pos_widget.isHidden())
        dlg.follow_radio.setChecked(True)
        self.assertTrue(dlg._fixed_pos_widget.isHidden())
        self.assertTrue(dlg._var_pos_widget.isHidden())

    def test_all_var_combos_readonly(self):
        """所有变量编辑下拉均不可编辑，只能下拉选择。"""
        for st, attr in (("log", "log_vars"), ("clip_set", "clip_name"),
                         ("clip_get", "clip_variable"), ("ocr", "ocr_variable"),
                         ("text_find", "tf_variable"), ("click", "pos_var")):
            dlg = self._open(st, {})
            combo = getattr(dlg, attr)
            self.assertFalse(combo.isEditable(), f"{st}.{attr} 应不可编辑")


if __name__ == "__main__":
    unittest.main()
