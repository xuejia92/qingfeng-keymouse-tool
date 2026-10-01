"""DrissionPage 可视化网页自动化步骤（dp_*）的测试。

覆盖：config 层默认参数与摘要、dp_actors 层定位符合成与执行语义
（浏览器变量必填 / 元素操作 / 上传）、模块面板 DrissionPage 分组覆盖、
步骤编辑对话框表单（构建 / 回填 / 收集 / 必填校验）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import (AUTO_STEP_TYPES, FLOW_STEP_TYPES, FlowStep,
                        default_step_params)
from app.dp_actors import (DP_ELE_ACTIONS, DP_LISTEN_ACTIONS, DP_LOCATORS,
                           DP_MATCHES, DP_TAB_MODES, build_locator,
                           run_dp_browser_step, run_dp_close_browser_step,
                           run_dp_element_step,
                           run_dp_upload_step)
from app.ui.flow_tab import MODULE_GROUPS

DP_TYPES = ["dp_browser", "dp_element", "dp_tab", "dp_listen",
            "dp_page_shot", "dp_ele_shot", "dp_upload",
            "dp_close_browser"]


def _params(t, **over):
    p = default_step_params(t)
    p.update(over)
    return p


class _FakeEle:
    """可断言的假元素：记录 click/input 调用，返回预设文本/属性。"""

    def __init__(self, text="hello", attr_val="v"):
        self.text = text
        self.attr_val = attr_val
        self.clicked = False
        self.input_val = None

    def click(self):
        self.clicked = True
        return True

    def input(self, value):
        self.input_val = value

    def attr(self, name):
        return self.attr_val


class _FakeTab:
    def __init__(self, ele=None):
        self._ele = ele or _FakeEle()

    def ele(self, locator, timeout=None, index=1):
        return self._ele


class _FakeBrowser:
    def __init__(self, tab=None):
        self.latest_tab = tab or _FakeTab()


class TestMetadata(unittest.TestCase):
    def test_registered_as_step_types(self):
        self.assertEqual(FLOW_STEP_TYPES["dp_browser"], "打开浏览器")
        self.assertEqual(FLOW_STEP_TYPES["dp_element"], "元素操作")
        self.assertEqual(FLOW_STEP_TYPES["dp_tab"], "切换标签")
        self.assertEqual(FLOW_STEP_TYPES["dp_listen"], "监听网络数据")
        self.assertEqual(FLOW_STEP_TYPES["dp_page_shot"], "网页截图")
        self.assertEqual(FLOW_STEP_TYPES["dp_ele_shot"], "元素截图")
        self.assertEqual(FLOW_STEP_TYPES["dp_upload"], "上传文件")
        self.assertEqual(FLOW_STEP_TYPES["dp_close_browser"], "关闭浏览器")

    def test_default_params(self):
        self.assertEqual(_params("dp_browser")["browser_var"], "")
        self.assertEqual(_params("dp_browser")["launch_mode"], "front")
        self.assertEqual(_params("dp_element")["action"], "click")
        self.assertEqual(_params("dp_element")["locator_type"], "id")
        self.assertEqual(_params("dp_element")["match"], "=")
        self.assertEqual(_params("dp_tab")["switch_mode"], "index")
        self.assertEqual(_params("dp_listen")["action"], "start")
        self.assertEqual(_params("dp_close_browser")["browser_var"], "")

    def test_drission_group_covers_all_dp_modules(self):
        groups = {gid: types for gid, _, types in MODULE_GROUPS}
        self.assertIn("drission", groups)
        self.assertEqual(sorted(groups["drission"]), sorted(DP_TYPES))

    def test_dp_types_draggable(self):
        """dp_* 全部可拖拽（不在自动成对生成的结构标记里）。"""
        for t in DP_TYPES:
            self.assertNotIn(t, AUTO_STEP_TYPES)


class TestBuildLocator(unittest.TestCase):
    def test_id_exact(self):
        self.assertEqual(build_locator("id", "=", "kw"), "#kw")

    def test_id_fuzzy(self):
        self.assertEqual(build_locator("id", ":", "kw"), "#:kw")

    def test_class_prefix(self):
        self.assertEqual(build_locator("class", "^", "btn"), ".^btn")

    def test_attr(self):
        self.assertEqual(build_locator("attr", "=", "v", attr_name="name"), "@name=v")

    def test_text_suffix(self):
        self.assertEqual(build_locator("text", "$", "首页"), "text$首页")

    def test_tag(self):
        self.assertEqual(build_locator("tag", "=", "div"), "t:div")

    def test_css(self):
        self.assertEqual(build_locator("css", "=", ".cls"), "css:.cls")

    def test_xpath(self):
        self.assertEqual(build_locator("xpath", "=", "//div"), "xpath://div")


class TestSummary(unittest.TestCase):
    def _summary(self, t, **params):
        s = FlowStep(type=t)
        s.params.update(params)
        return s.summary()

    def test_browser(self):
        self.assertIn("browser", self._summary("dp_browser", browser_var="browser"))
        self.assertIn("浏览器", self._summary("dp_browser", browser_var="browser"))

    def test_element_action_label(self):
        self.assertIn("点击元素", self._summary(
            "dp_element", action="click", locator_value="kw"))

    def test_tab(self):
        self.assertIn("按序号", self._summary("dp_tab", switch_mode="index", value="1"))

    def test_page_shot_result_var(self):
        self.assertIn("out", self._summary("dp_page_shot", result_var="out"))

    def test_close_browser_var_in_summary(self):
        self.assertIn("b", self._summary("dp_close_browser", browser_var="b"))
        self.assertIn("浏览器", self._summary("dp_close_browser", browser_var="b"))
        self.assertIn("未指定变量", self._summary("dp_close_browser"))


class TestExecution(unittest.TestCase):
    def test_browser_step_requires_var(self):
        ok, why = run_dp_browser_step({"browser_var": ""}, {})
        self.assertFalse(ok)
        self.assertIn("浏览器变量", why)

    def test_element_step_requires_browser_var(self):
        ok, why = run_dp_element_step({"browser_var": ""}, {})
        self.assertFalse(ok)
        self.assertIn("浏览器变量", why)

    def test_element_step_undefined_browser_var(self):
        ok, why = run_dp_element_step({"browser_var": "b"}, {})
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_element_click(self):
        browser = _FakeBrowser()
        ok, why = run_dp_element_step(_params(
            "dp_element", browser_var="b", locator_type="id", match="=",
            locator_value="kw", action="click"), {"b": browser})
        self.assertTrue(ok, why)
        self.assertTrue(browser.latest_tab._ele.clicked)

    def test_element_get_text_writes_result_var(self):
        browser = _FakeBrowser()
        vars_ = {"b": browser}
        ok, why = run_dp_element_step(_params(
            "dp_element", browser_var="b", locator_type="id",
            locator_value="kw", action="get_text", result_var="txt"), vars_)
        self.assertTrue(ok, why)
        self.assertEqual(vars_["txt"], "hello")

    def test_upload_requires_files(self):
        browser = _FakeBrowser()
        ok, why = run_dp_upload_step(_params(
            "dp_upload", browser_var="b", locator_value="f", file_paths=""), {"b": browser})
        self.assertFalse(ok)
        self.assertIn("文件", why)

    # ---- 「关闭浏览器」----
    def test_close_step_requires_var(self):
        ok, why = run_dp_close_browser_step({"browser_var": ""}, {})
        self.assertFalse(ok)
        self.assertIn("浏览器变量", why)

    def test_close_step_undefined_browser_var(self):
        ok, why = run_dp_close_browser_step({"browser_var": "b"}, {})
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_close_step_not_browser_value(self):
        ok, why = run_dp_close_browser_step({"browser_var": "b"}, {"b": "not-a-browser"})
        self.assertFalse(ok)
        self.assertIn("不是浏览器对象", why)

    def test_close_self_started_browser(self):
        """自启会话：真正退出浏览器并把变量从运行期容器摘除。"""
        browser = _FakeBrowser()
        vars_ = {"b": browser}
        with mock.patch("app.web_actors.is_current", return_value=True), \
             mock.patch("app.web_actors.active_mode", return_value="front"), \
             mock.patch("app.web_actors.close_browser", return_value=True) as cb:
            ok, why = run_dp_close_browser_step(
                _params("dp_close_browser", browser_var="b"), vars_)
        self.assertTrue(ok, why)
        self.assertIn("已关闭", why)
        cb.assert_called_once()
        self.assertNotIn("b", vars_)          # 关闭后引用失效，防止误用僵尸对象

    def test_close_attach_browser_disconnects(self):
        """接管（attach）会话：只断连，不退出用户手动打开的窗口。"""
        browser = _FakeBrowser()
        vars_ = {"b": browser}
        with mock.patch("app.web_actors.is_current", return_value=True), \
             mock.patch("app.web_actors.active_mode", return_value="attach"), \
             mock.patch("app.web_actors.close_browser", return_value=True):
            ok, why = run_dp_close_browser_step(
                _params("dp_close_browser", browser_var="b"), vars_)
        self.assertTrue(ok, why)
        self.assertIn("接管", why)
        self.assertIn("窗口保留", why)

    def test_close_stale_ref_is_idempotent(self):
        """变量指向已非活动会话（旧引用）时不动手，幂等成功。"""
        browser = _FakeBrowser()
        vars_ = {"b": browser}
        with mock.patch("app.web_actors.is_current", return_value=False), \
             mock.patch("app.web_actors.close_browser") as cb:
            ok, why = run_dp_close_browser_step(
                _params("dp_close_browser", browser_var="b"), vars_)
        self.assertTrue(ok, why)
        cb.assert_not_called()
        self.assertNotIn("b", vars_)          # 引用不再有效，同样摘除


class TestDialogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_all_dp_forms_build(self):
        from app.ui.flow_dialog import StepParamsDialog
        for t in DP_TYPES:
            dlg = StepParamsDialog(FlowStep(type=t))
            self.assertIsNotNone(dlg)
            dlg.close()

    def test_element_fill_and_apply_roundtrip(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="dp_element", params=_params(
            "dp_element", browser_var="b", locator_type="css", match="=",
            locator_value=".btn", index=2, action="input", input_value="hi",
            timeout=5.0, result_var="r"))
        dlg = StepParamsDialog(step)
        self.assertEqual(dlg.dpe_locator.currentData(), "css")
        self.assertEqual(dlg.dpe_action.currentData(), "input")
        self.assertEqual(dlg._combo_value(dlg.dpe_browser), "b")
        out = FlowStep(type="dp_element")
        dlg.apply_to(out)
        self.assertEqual(out.params["locator_type"], "css")
        self.assertEqual(out.params["index"], 2)
        self.assertEqual(out.params["action"], "input")
        self.assertEqual(out.params["input_value"], "hi")
        self.assertEqual(out.params["browser_var"], "b")
        dlg.close()

    def test_browser_form_rejects_empty_var(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="dp_browser"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请填写浏览器变量")
        dlg.close()

    def test_element_result_action_rejects_missing_var(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="dp_element", params=_params(
            "dp_element", browser_var="b", action="get_text", locator_value="kw"))
        dlg = StepParamsDialog(step)
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请设置结果变量")
        dlg.close()

    def test_close_browser_form_fill_and_apply_roundtrip(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="dp_close_browser",
                        params=_params("dp_close_browser", browser_var="b"))
        dlg = StepParamsDialog(step)
        self.assertEqual(dlg._combo_value(dlg.dpc_browser), "b")
        out = FlowStep(type="dp_close_browser")
        dlg.apply_to(out)
        self.assertEqual(out.params["browser_var"], "b")
        dlg.close()

    def test_close_browser_form_rejects_empty_var(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="dp_close_browser"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请选择浏览器变量")
        dlg.close()


class TestBrowserDebugPort(unittest.TestCase):
    """「打开浏览器」的调试端口（等价 --remote-debugging-port，默认 9333）。

    2026-09-27 用户要求：端口默认 9333，且自启的浏览器也带上它
    （这样之后别的步骤/流程能接管，也方便调试）；留空则不带端口（兼容旧流程）。
    """

    def test_default_port_is_9333(self):
        self.assertEqual(default_step_params("dp_browser")["attach_port"], "9333")

    def test_build_options_sets_local_port(self):
        calls = []

        class _FakeCO:
            def set_local_port(self, port):
                calls.append(port)

            def set_argument(self, _a):
                pass

            def headless(self, _v):
                pass

        from app import web_actors
        with mock.patch("app.web_actors._import_drission",
                        return_value=(object, _FakeCO, ())):
            web_actors._build_options("front", local_port=9333)
            self.assertEqual(calls, [9333])
            calls.clear()
            web_actors._build_options("front")         # 不传端口 → 不设
            self.assertEqual(calls, [])

    def test_get_browser_passes_port_for_self_launch(self):
        """front/headless/background（自启）也要把端口传下去。"""
        from app import web_actors
        seen = {}

        def _fake_build(mode, local_port=None):
            seen["mode"], seen["port"] = mode, local_port
            return "OPT"

        web_actors._reset_browser()
        with mock.patch("app.web_actors._import_drission",
                        return_value=(lambda *_a: "BROWSER", object, ())), \
                mock.patch("app.web_actors._build_options", side_effect=_fake_build):
            web_actors.get_browser("front", 9333)
        self.assertEqual(seen, {"mode": "front", "port": 9333})
        web_actors._reset_browser()

    def test_step_passes_default_port(self):
        captured = {}

        def _fake_get_browser(mode, port):
            captured.update(mode=mode, port=port)
            return _FakeBrowser()

        step = FlowStep(type="dp_browser", params={"browser_var": "b"})
        with mock.patch("app.web_actors.is_available", return_value=(True, "")), \
                mock.patch("app.web_actors.get_browser", side_effect=_fake_get_browser):
            ok, why = run_dp_browser_step(step.params, {})
        self.assertTrue(ok, why)
        self.assertEqual(captured["port"], 9333)
        self.assertEqual(captured["mode"], "front")

    def test_empty_port_keeps_old_behaviour(self):
        captured = {}

        def _fake_get_browser(mode, port):
            captured.update(mode=mode, port=port)
            return _FakeBrowser()

        step = FlowStep(type="dp_browser",
                        params={"browser_var": "b", "attach_port": ""})
        with mock.patch("app.web_actors.is_available", return_value=(True, "")), \
                mock.patch("app.web_actors.get_browser", side_effect=_fake_get_browser):
            ok, _why = run_dp_browser_step(step.params, {})
        self.assertTrue(ok)
        self.assertIsNone(captured["port"])          # 留空 = 不带端口

    def test_attach_without_port_fails(self):
        step = FlowStep(type="dp_browser",
                        params={"browser_var": "b", "launch_mode": "attach",
                                "attach_port": ""})
        ok, why = run_dp_browser_step(step.params, {})
        self.assertFalse(ok)
        self.assertIn("未填写接管端口", why)

    def test_invalid_port_reported(self):
        for bad in ("abc", "70000", "-1"):
            step = FlowStep(type="dp_browser",
                            params={"browser_var": "b", "attach_port": bad})
            ok, why = run_dp_browser_step(step.params, {})
            self.assertFalse(ok, bad)
            self.assertIn("调试端口", why)


class TestBrowserPortDialog(unittest.TestCase):
    """「打开浏览器」表单：端口默认 9333、始终可见、非法值拦下。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_port_filled_with_default(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="dp_browser"))
        self.assertEqual(dlg.dpb_port.text(), "9333")
        dlg.close()

    def test_port_row_visible_for_all_modes(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="dp_browser"))
        for mode in ("front", "headless", "background", "attach"):
            idx = dlg.dpb_mode.findData(mode)
            dlg.dpb_mode.setCurrentIndex(idx)
            dlg._sync_dpb_rows()
            form, row = dlg._dpb_port_row
            self.assertTrue(form.isRowVisible(row), mode)
        dlg.close()

    def test_invalid_port_rejected_on_accept(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="dp_browser"))
        dlg.dpb_var.setText("b")
        dlg.dpb_port.setText("abc")
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "调试端口不合法")
        dlg.close()

    def test_empty_port_accepted(self):
        """清空端口 = 不带端口，应该允许保存。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="dp_browser", params={"browser_var": "b"})
        dlg = StepParamsDialog(step)
        dlg.dpb_port.setText("")
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertFalse(warn.called)
        dlg.close()


class TestAttachExistingOnly(unittest.TestCase):
    """接管模式：端口没有浏览器时必须报错，绝不静默自启新浏览器（2026-09-27 修的 bug）。

    现场：用户桌面 Chrome 快捷方式加了 --remote-debugging-port=9333，但之前开着普通 Chrome，
    单实例机制让带端口的新进程秒退 → 9333 端口实际没监听 → DrissionPage 的 Chromium(端口)
    会「自动启动一个新的」，用户看到的就是"接管却开了新窗口"。
    """

    def test_port_probe(self):
        from app import web_actors
        with mock.patch("socket.create_connection", side_effect=OSError("refused")):
            self.assertFalse(web_actors._port_has_browser(9333))
        with mock.patch("socket.create_connection"):
            self.assertTrue(web_actors._port_has_browser(9333))

    def test_attach_without_browser_raises_with_clear_tip(self):
        from app import web_actors
        web_actors._reset_browser()
        launched = []

        class _FakeCO:
            def set_local_port(self, _p):
                return self

            def existing_only(self, _on):
                return self

        def _fake_import():
            return (lambda co: launched.append(co), _FakeCO, ())

        with mock.patch.object(web_actors, "_port_has_browser", return_value=False), \
                mock.patch.object(web_actors, "_import_drission",
                                  side_effect=_fake_import):
            with self.assertRaises(ValueError) as cm:
                web_actors.get_browser("attach", 9333)
        self.assertIn("接管失败", str(cm.exception))
        self.assertIn("单实例", str(cm.exception))     # 教用户怎么自救
        self.assertEqual(launched, [])                 # 没有真去起浏览器
        web_actors._reset_browser()

    def test_attach_with_browser_uses_existing_only(self):
        from app import web_actors
        web_actors._reset_browser()
        seen = {}

        class _FakeCO:
            def set_local_port(self, port):
                seen["port"] = port
                return self

            def existing_only(self, on):
                seen["existing_only"] = on
                return self

        def _fake_import():
            return (lambda co: "BROWSER", _FakeCO, ())

        with mock.patch.object(web_actors, "_port_has_browser", return_value=True), \
                mock.patch.object(web_actors, "_import_drission",
                                  side_effect=_fake_import):
            b = web_actors.get_browser("attach", 9333)
        self.assertEqual(b, "BROWSER")
        self.assertEqual(seen["port"], 9333)
        self.assertTrue(seen["existing_only"])        # 兜底：DrissionPage 也不会自启
        web_actors._reset_browser()

    def test_dp_browser_attach_failure_reports_clear_reason(self):
        step = FlowStep(type="dp_browser",
                        params={"browser_var": "b", "launch_mode": "attach",
                                "attach_port": "9333"})
        with mock.patch("app.web_actors.is_available", return_value=(True, "")), \
                mock.patch("app.web_actors.get_browser",
                           side_effect=ValueError("接管失败：端口 9333 上没有正在运行的浏览器")):
            ok, why = run_dp_browser_step(step.params, {})
        self.assertFalse(ok)
        self.assertIn("接管失败", why)
        self.assertNotIn("ValueError", why)           # 别把异常类名糊到文案上


if __name__ == "__main__":
    unittest.main()
