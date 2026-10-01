# -*- coding: utf-8 -*-
"""步骤「必填参数」判定（config.step_missing_required）的测试。

用途：流程列表给「必填参数没填」的步骤画红框。判定**以各 run_xxx_step / dp_actors
里的实际判定为准**（"没填就 return False"），不能凭感觉加。

2026-09-26 用户报的误报：等待文字出现的 result_var / pos_var 其实是**可选输出**
（不填只是不写变量，步骤照样成功），早期凭猜测当必填 → 列表误标红框。
本文件里 TestOptionalOutputVars 就是钉这个坑的回归测试。

另外三条规则表完整性契约防拼写错误：类型要在 FLOW_STEP_TYPES 里、字段名要真的
存在于该类型的默认参数里（拼错会让 _blank 永远为真 → 把所有步骤刷红）。
"""
from __future__ import annotations

import unittest

from app.config import (FLOW_STEP_TYPES, STEP_REQUIRED_PARAMS, FlowStep,
                        default_step_params, step_missing_required)


def _step(t: str, **params) -> FlowStep:
    return FlowStep(type=t, params=params)


class TestMissingRequired(unittest.TestCase):
    def test_blank_variants_all_count_as_missing(self):
        for bad in (None, "", "   "):
            self.assertEqual(step_missing_required(_step("var", name=bad)), ["变量名"])

    def test_filled_step_reports_nothing(self):
        self.assertEqual(step_missing_required(_step("var", name="x")), [])

    def test_py_func_requires_all_three(self):
        """python 函数的 code / func_name / result_var 都是 run_py_func_step 里的硬校验。"""
        got = step_missing_required(_step("py_func", code="def f(): pass",
                                          func_name="f"))
        self.assertEqual(got, ["结果变量"])
        self.assertEqual(
            step_missing_required(_step("py_func", code="def f(): pass",
                                        func_name="f", result_var="r")), [])

    def test_wait_text_only_requires_text(self):
        """等待文字出现：只有目标文字必填（结果变量可选，见 TestOptionalOutputVars）。"""
        self.assertEqual(step_missing_required(_step("wait_text", text="")),
                         ["目标文字"])
        self.assertEqual(step_missing_required(_step("wait_text", text="开始")), [])

    def test_condition_blocks_require_expression(self):
        for t in ("if", "elseif", "while"):
            self.assertEqual(step_missing_required(_step(t)), ["条件表达式"])
            self.assertEqual(step_missing_required(_step(t, condition="a == 1")), [])

    def test_for_requires_stop_only(self):
        self.assertEqual(step_missing_required(_step("for", stop="")), ["结束值"])
        self.assertEqual(step_missing_required(_step("for", stop="5")), [])

    def test_foreach_requires_items(self):
        self.assertEqual(step_missing_required(_step("foreach", items="")), ["数据源"])

    def test_qq_mail_requires_sender_credentials_and_recipient(self):
        got = step_missing_required(_step("qq_mail"))
        self.assertEqual(got, ["发送人邮箱", "邮箱授权码", "收件人邮箱"])
        self.assertEqual(
            step_missing_required(_step("qq_mail", mail_user="a@qq.com",
                                        mail_auth_code="x", mail_to="b@qq.com")), [])

    def test_structural_and_empty_steps_never_flagged(self):
        for t in ("endif", "else", "endForeach", "endFor", "endWhile",
                  "break", "continue"):
            self.assertEqual(step_missing_required(_step(t)), [], t)

    def test_unknown_type_returns_empty(self):
        class _Fake:
            type = "nosuch_type"
            params: dict = {}

        self.assertEqual(step_missing_required(_Fake()), [])

    def test_non_string_value_treated_as_filled(self):
        self.assertEqual(
            step_missing_required(
                _step("shutdown", trigger_mode="countdown", count_hours=3)), [])


class TestOptionalOutputVars(unittest.TestCase):
    """结果变量类参数是否必填，一律以运行期判定为准（2026-09-26 修的误报）。

    这些步骤里写的是 `if result_var: variables[...] = ...`，留空只是"不写变量"，
    步骤本身照样成功 —— 列表不该给它们标红框。
    """

    def test_wait_text_result_vars_are_optional(self):
        """用户报的场景：两个结果变量都可留空。"""
        self.assertEqual(
            step_missing_required(_step("wait_text", text="开始",
                                        result_var="", pos_var="")), [])

    def test_wait_image_result_vars_are_optional(self):
        self.assertEqual(
            step_missing_required(_step("wait_image", image="t.png",
                                        result_var="", pos_var="")), [])

    def test_text_find_variable_is_optional(self):
        self.assertEqual(
            step_missing_required(_step("text_find", text="首页", variable="")), [])

    def test_manual_shot_has_no_required_field(self):
        self.assertEqual(step_missing_required(_step("manual_shot")), [])

    def test_ocr_variable_is_required(self):
        """反向确认：文字识别的结果变量是真必填（run_ocr_step 会失败）。"""
        self.assertEqual(step_missing_required(_step("ocr", variable="")),
                         ["结果变量"])

    def test_screenshot_and_color_pick_variable_required(self):
        for t in ("screenshot", "color_pick", "clip_get", "shot_translate"):
            want = ["译文变量"] if t == "shot_translate" else ["结果变量"]
            self.assertEqual(step_missing_required(_step(t, variable="")), want, t)

    def test_find_image_variable_required(self):
        """找图的结果变量必填（run_find_image_step 会 "未指定结果变量"）；
        模板图在条件规则里判，这里填上以免混进结果。"""
        self.assertEqual(
            step_missing_required(_step("find_image", variable="", image="t.png")),
            ["结果变量"])


class TestConditionalRequired(unittest.TestCase):
    def test_web_url_only_required_for_open(self):
        self.assertEqual(step_missing_required(_step("web", action="open")),
                         ["网址"])
        self.assertEqual(
            step_missing_required(_step("web", action="open", url="https://x")), [])
        self.assertEqual(
            step_missing_required(_step("web", action="close_browser")), [])

    def test_script_content_or_path_by_source(self):
        self.assertEqual(step_missing_required(_step("script", source="text")),
                         ["脚本内容"])
        self.assertEqual(step_missing_required(_step("script", source="file")),
                         ["脚本文件路径"])
        self.assertEqual(
            step_missing_required(_step("script", source="text", content="echo hi")), [])

    def test_click_fixed_position_needs_coords_or_var(self):
        self.assertEqual(
            step_missing_required(_step("click", fixed_position=True)),
            ["固定坐标 x/y（或坐标变量）"])
        self.assertEqual(
            step_missing_required(_step("click", fixed_position=True,
                                        pos_x=10, pos_y=20)), [])
        self.assertEqual(
            step_missing_required(_step("click", fixed_position=True,
                                        pos_var="p")), [])
        self.assertEqual(step_missing_required(_step("click", fixed_position=False)), [])

    def test_background_mode_needs_window_title(self):
        """click 与 press 的后台模式都要绑窗口（否则运行期报「未绑定目标窗口」）。"""
        self.assertEqual(
            step_missing_required(_step("click", fixed_position=False,
                                        background=True)), ["目标窗口标题"])
        self.assertEqual(
            step_missing_required(_step("press", keys="a", background=True)),
            ["目标窗口标题"])

    def test_find_like_steps_need_template(self):
        for t in ("find_image", "wait_image", "find"):
            extra = {"variable": "v"} if t == "find_image" else {}
            self.assertIn("模板图",
                          step_missing_required(_step(t, image="", image_path="",
                                                      **extra)), t)
            self.assertEqual(
                step_missing_required(_step(t, image="t.png", **extra)), [], t)
            # 只填绝对路径也算（load_template 两者都看）
            self.assertEqual(
                step_missing_required(_step(t, image="", image_path="C:/t.png",
                                            **extra)), [], t)

    def test_app_needs_path_or_process(self):
        self.assertEqual(step_missing_required(_step("app")),
                         ["程序路径（或从进程列表选择）"])
        self.assertEqual(step_missing_required(_step("app", path="notepad.exe")), [])
        self.assertEqual(step_missing_required(_step("app", process="notepad.exe")), [])

    def test_clip_set_needs_name_or_text(self):
        self.assertEqual(step_missing_required(_step("clip_set")), ["变量名或文本"])
        self.assertEqual(step_missing_required(_step("clip_set", name="x")), [])
        self.assertEqual(step_missing_required(_step("clip_set", text="hello")), [])

    def test_dp_tab_needs_switch_value(self):
        self.assertEqual(step_missing_required(_step("dp_tab", browser_var="b",
                                                     value="")),
                         ["切换条件（序号/标题/网址）"])
        self.assertEqual(
            step_missing_required(_step("dp_tab", browser_var="b", value="1")), [])

    def test_dp_shots_need_result_var(self):
        self.assertEqual(
            step_missing_required(_step("dp_page_shot", browser_var="b")),
            ["结果变量"])
        self.assertEqual(
            step_missing_required(_step("dp_ele_shot", browser_var="b",
                                        locator_value="#a")), ["结果变量"])

    def test_dp_element_requires_result_var_only_for_value_actions(self):
        base = dict(browser_var="b", locator_value="#a")
        self.assertEqual(
            step_missing_required(_step("dp_element", action="click", **base)), [])
        self.assertEqual(
            step_missing_required(_step("dp_element", action="get_text", **base)),
            ["结果变量"])
        self.assertEqual(
            step_missing_required(_step("dp_element", action="get_text",
                                        result_var="r", **base)), [])

    def test_shutdown_by_trigger_mode(self):
        self.assertEqual(
            step_missing_required(_step("shutdown", trigger_mode="at_time")),
            ["触发时间"])
        self.assertEqual(
            step_missing_required(_step("shutdown", trigger_mode="countdown",
                                        count_hours=0, count_minutes=0,
                                        count_seconds=0)),
            ["倒计时时长"])
        self.assertEqual(
            step_missing_required(_step("shutdown", trigger_mode="countdown",
                                        count_minutes=5)), [])
        self.assertEqual(
            step_missing_required(_step("shutdown", trigger_mode="condition",
                                        cond_mode="text")), ["触发文字"])
        self.assertEqual(
            step_missing_required(_step("shutdown", trigger_mode="condition",
                                        cond_mode="image", image="t.png")), [])

    def test_close_app_needs_target_or_process(self):
        self.assertEqual(step_missing_required(_step("close_app")), ["目标进程"])
        self.assertEqual(step_missing_required(_step("close_app", target="notepad")), [])


class TestRuleTableIntegrity(unittest.TestCase):
    """规则表本身别写错：类型与字段名都必须真实存在。"""

    def test_table_types_are_known(self):
        for t in STEP_REQUIRED_PARAMS:
            self.assertIn(t, FLOW_STEP_TYPES, f"规则表里的未知类型 {t}")

    def test_required_field_names_are_real(self):
        for t, items in STEP_REQUIRED_PARAMS.items():
            keys = set(default_step_params(t))
            for key, _label in items:
                self.assertIn(key, keys, f"{t} 的必填字段 {key} 不在默认参数里")

    def test_conditional_field_names_are_real(self):
        used = {
            "find_image": ("image", "image_path"),
            "wait_image": ("image", "image_path"),
            "find": ("image",),        # image_path 是运行期兼容字段，不在默认参数里
            "click": ("fixed_position", "pos_var", "pos_x", "pos_y",
                      "background", "window_title"),
            "press": ("background", "window_title"),
            "web": ("action", "url"),
            "script": ("source", "path", "content"),
            "float_image": ("source_mode", "address", "image", "image_path"),
            "app": ("path", "process"),
            "close_app": ("target", "process"),
            "clip_set": ("name", "text"),
            "dp_tab": ("value",),
            "dp_element": ("action", "result_var"),
            "shutdown": ("trigger_mode", "at_time", "cond_mode", "text", "image",
                         "image_path", "count_hours", "count_minutes",
                         "count_seconds"),
        }
        for t, fields in used.items():
            keys = set(default_step_params(t))
            for f in fields:
                self.assertIn(f, keys, f"{t} 的条件规则引用了不存在的字段 {f}")

    def test_every_label_is_non_empty(self):
        for t, items in STEP_REQUIRED_PARAMS.items():
            for key, label in items:
                self.assertTrue(label.strip(), f"{t}.{key} 缺少提示文案")

    def test_no_optional_output_var_is_declared_required(self):
        """防止再犯：结果变量类字段只允许出现在确有硬校验的类型上。

        名单来自逐一核对 run_xxx_step 的结论（这些类型里确实会 "未指定结果变量"
        直接失败）：ocr / shot_translate / screenshot / color_pick / clip_get /
        find_image / yolo_detect / py_func / dp_page_shot / dp_ele_shot。
        """
        allowed = {"ocr", "shot_translate", "screenshot", "color_pick", "clip_get",
                   "find_image", "yolo_detect", "py_func", "dp_page_shot",
                   "dp_ele_shot", "dp_element"}
        for t, items in STEP_REQUIRED_PARAMS.items():
            keys = {k for k, _ in items}
            for field in ("result_var", "pos_var", "variable"):
                if field in keys:
                    self.assertIn(t, allowed,
                                  f"{t} 把可选的输出变量 {field} 当成了必填")


if __name__ == "__main__":
    unittest.main()
