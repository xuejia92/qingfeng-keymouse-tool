"""「截图谷歌翻译」步骤 + 谷歌翻译客户端的测试。

刻意**不发起真实网络请求**：那会让测试变慢、依赖外网可达性，结果还不稳定。
网络层统一 mock（translate_actor._http_get），只验证解析、分片、回退、重试、
变量写入、展示开关等逻辑。真实联网翻译靠手工冒烟：跑一遍步骤，在遮罩上框住
英文文字，看变量里是否拿到中文译文。
OCR 同样 mock，不加载 RapidOCR 模型；屏幕遮罩/框选也 mock（select_region）。

注意：截图区域**不在编辑期预设**（2026-09-15 起改为运行时由用户框选），
所以每个用例都必须打桩 screenshot_actor.select_region，否则会真的弹屏幕遮罩。
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.screenshot_actor as shot_actor
from app import translate_actor
from app.config import (FLOW_STEP_TYPES, STEP_OUTPUT_FIELDS,
                        TRANSLATE_LANGUAGES, TRANSLATE_TARGET_LANGUAGES,
                        Flow, FlowStep, default_step_params, flow_from_dict,
                        flow_to_dict)
from app.tasks import run_shot_translate_step


def _gtx_response(segments, detected="en"):
    """按谷歌 /translate_a/single 的真实结构造返回体。

    segments 为 [(译文, 原文), ...]；每个分段自带尾随换行（接口行为）。
    """
    body = [[[t, s, None, None, 5] for t, s in segments], None, detected,
            None, None, None, 1, [], [[detected]]]
    return json.dumps(body)


# ---------- 纯逻辑：分片 / 代理 / 语言表 ----------

class TestSplitChunks(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(translate_actor.split_chunks("abc"), ["abc"])

    def test_empty_text_no_chunk(self):
        self.assertEqual(translate_actor.split_chunks(""), [])
        self.assertEqual(translate_actor.split_chunks("   \n  "), [])

    def test_splits_on_line_boundaries(self):
        text = "".join(f"Line number {i} of text.\n" for i in range(300))
        chunks = translate_actor.split_chunks(text, limit=500)
        self.assertGreater(len(chunks), 1)
        # 不丢内容：拼回去与原文一致
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            self.assertLessEqual(len(c), 500)

    def test_single_oversized_line_hard_split(self):
        chunks = translate_actor.split_chunks("x" * 9000, limit=3500)
        self.assertEqual([len(c) for c in chunks], [3500, 3500, 2000])

    def test_non_positive_limit_falls_back_to_default(self):
        chunks = translate_actor.split_chunks("x" * 10, limit=0)
        self.assertEqual(chunks, ["x" * 10])


class TestNormalizeProxy(unittest.TestCase):
    def test_adds_http_scheme(self):
        self.assertEqual(translate_actor.normalize_proxy("127.0.0.1:7890"),
                         "http://127.0.0.1:7890")

    def test_keeps_existing_scheme(self):
        self.assertEqual(translate_actor.normalize_proxy("socks5://a:1"),
                         "socks5://a:1")

    def test_empty_means_direct(self):
        self.assertEqual(translate_actor.normalize_proxy(""), "")
        self.assertEqual(translate_actor.normalize_proxy(None), "")


class TestLanguageTables(unittest.TestCase):
    def test_auto_only_on_source_side(self):
        self.assertIn("auto", TRANSLATE_LANGUAGES)
        self.assertNotIn("auto", TRANSLATE_TARGET_LANGUAGES)

    def test_target_is_subset_of_languages(self):
        for k in TRANSLATE_TARGET_LANGUAGES:
            self.assertIn(k, TRANSLATE_LANGUAGES)

    def test_lang_name_known_and_unknown(self):
        self.assertEqual(translate_actor.lang_name("zh-CN"), "中文（简体）")
        self.assertEqual(translate_actor.lang_name("zz"), "zz")
        self.assertEqual(translate_actor.lang_name(""), "")


class TestParseResponse(unittest.TestCase):
    def test_joins_segments_keeping_newlines(self):
        data = json.loads(_gtx_response([("你好世界\n", "Hello world\n"),
                                         ("早上好", "Good morning")]))
        text, detected = translate_actor._parse_response(data)
        self.assertEqual(text, "你好世界\n早上好")
        self.assertEqual(detected, "en")

    def test_malformed_shapes_return_none(self):
        for bad in (None, [], "str", [[]], [None], [[[]]], 0):
            with self.subTest(bad=bad):
                self.assertEqual(translate_actor._parse_response(bad), (None, ""))

    def test_missing_detected_language(self):
        text, detected = translate_actor._parse_response([[["a", "b"]]])
        self.assertEqual(text, "a")
        self.assertEqual(detected, "")


class TestTranslateErrorFlags(unittest.TestCase):
    def test_default_not_retryable(self):
        self.assertFalse(translate_actor.TranslateError("x").retryable)

    def test_retryable_flag(self):
        self.assertTrue(translate_actor.TranslateError("x", retryable=True).retryable)


# ---------- 翻译主流程（mock 网络） ----------

class TestTranslateWithMockedHttp(unittest.TestCase):
    def test_empty_text_fails_without_request(self):
        with mock.patch.object(translate_actor, "_http_get") as get:
            ok, res, why = translate_actor.translate("   ")
        self.assertFalse(ok)
        self.assertIsNone(res)
        self.assertIn("为空", why)
        get.assert_not_called()

    def test_success_returns_text_and_detected_lang(self):
        body = _gtx_response([("你好世界", "Hello world")])
        with mock.patch.object(translate_actor, "_http_get", return_value=body):
            ok, res, why = translate_actor.translate("Hello world")
        self.assertTrue(ok)
        self.assertEqual(res["text"], "你好世界")
        self.assertEqual(res["source_lang"], "en")
        self.assertEqual(res["target_lang"], "zh-CN")
        self.assertEqual(res["requests"], 1)
        self.assertIn("英语 → 中文（简体）", why)

    def test_explicit_source_lang_is_used(self):
        body = _gtx_response([("大家好", "Bonjour")], detected="fr")
        with mock.patch.object(translate_actor, "_http_get", return_value=body) as get:
            ok, res, _ = translate_actor.translate("Bonjour", source="fr", target="zh-CN")
        self.assertTrue(ok)
        self.assertIn("sl=fr", get.call_args[0][0])
        self.assertEqual(res["source_lang"], "fr")

    def test_multi_chunk_makes_multiple_requests_and_joins(self):
        body = _gtx_response([("译文\n", "source\n")])
        with mock.patch.object(translate_actor, "_http_get", return_value=body) as get:
            ok, res, _ = translate_actor.translate("x" * 9000)
        self.assertTrue(ok)
        self.assertEqual(res["requests"], 3)          # 3500+3500+2000
        self.assertEqual(get.call_count, 3)
        self.assertEqual(res["text"], "译文\n译文\n译文\n")

    def test_falls_back_to_second_endpoint(self):
        calls = []

        def fake_get(url, timeout, proxy_url):
            calls.append(url)
            if translate_actor.ENDPOINTS[0] in url:
                raise translate_actor.TranslateError("第一个域名失败", retryable=False)
            return _gtx_response([("好", "good")])

        with mock.patch.object(translate_actor, "_http_get", side_effect=fake_get):
            ok, res, _ = translate_actor.translate("good")
        self.assertTrue(ok)
        self.assertEqual(res["text"], "好")
        # 每个域名只试一次：不可重试的错误不会触发整轮重试
        self.assertEqual(len(calls), 2)

    def test_all_endpoints_failed_reports_reason(self):
        err = translate_actor.TranslateError("网络不可达", retryable=False)
        with mock.patch.object(translate_actor, "_http_get", side_effect=err):
            ok, res, why = translate_actor.translate("hello")
        self.assertFalse(ok)
        self.assertIsNone(res)
        self.assertIn("网络不可达", why)

    def test_retryable_error_retries_whole_round(self):
        with mock.patch.object(translate_actor, "_http_get",
                               side_effect=translate_actor.TranslateError(
                                   "超时", retryable=True)) as get, \
             mock.patch.object(translate_actor, "RETRY_DELAY", 0):
            ok, _, _ = translate_actor.translate("hello")
        self.assertFalse(ok)
        # 2 个域名 × 2 轮
        self.assertEqual(get.call_count, 2 * len(translate_actor.ENDPOINTS))

    def test_non_retryable_error_does_not_retry(self):
        with mock.patch.object(translate_actor, "_http_get",
                               side_effect=translate_actor.TranslateError(
                                   "格式错", retryable=False)) as get, \
             mock.patch.object(translate_actor, "RETRY_DELAY", 0):
            ok, _, _ = translate_actor.translate("hello")
        self.assertFalse(ok)
        self.assertEqual(get.call_count, len(translate_actor.ENDPOINTS))

    def test_invalid_json_is_retried_then_fails(self):
        with mock.patch.object(translate_actor, "_http_get",
                               return_value="<html>not json</html>") as get, \
             mock.patch.object(translate_actor, "RETRY_DELAY", 0):
            ok, _, why = translate_actor.translate("hello")
        self.assertFalse(ok)
        self.assertIn("JSON", why)
        self.assertEqual(get.call_count, 2 * len(translate_actor.ENDPOINTS))

    def test_timeout_argument_coerced(self):
        body = _gtx_response([("好", "good")])
        with mock.patch.object(translate_actor, "_http_get", return_value=body) as get:
            translate_actor.translate("good", timeout="bad")
        self.assertEqual(get.call_args[0][1], 10.0)   # 非法值回落到默认

    def test_dead_google_cn_domain_not_used(self):
        """translate.google.cn 已停用（实测 404），不能再作为回退域名。"""
        for ep in translate_actor.ENDPOINTS:
            self.assertNotIn("translate.google.cn", ep)


# ---------- run_shot_translate_step ----------

class TestShotTranslateStep(unittest.TestCase):
    """区域改运行时框选后，每个用例都要把框选打桩（见模块 docstring）。"""

    def setUp(self):
        p_ui = mock.patch.object(shot_actor, "ui_call",
                                 side_effect=lambda fn: fn())
        p_region = mock.patch.object(shot_actor, "select_region",
                                     return_value=(10, 20, 300, 80))
        self.ui_call = p_ui.start()
        self.select_region = p_region.start()
        self.addCleanup(p_ui.stop)
        self.addCleanup(p_region.stop)

    def _patch_ocr(self, lines, ok=True, why="识别到 2 行文字"):
        return mock.patch("app.ocr.recognize", return_value=(ok, lines, why))

    def _patch_translate(self, text="你好世界", source="en"):
        return mock.patch.object(
            translate_actor, "translate",
            return_value=(True, {"text": text, "source_lang": source,
                                 "target_lang": "zh-CN", "requests": 1},
                          "已翻译 11 字符"))

    def test_missing_variable_fails(self):
        ok, why = run_shot_translate_step({"variable": ""}, {})
        self.assertFalse(ok)
        self.assertIn("未指定译文变量", why)

    def test_stop_before_run(self):
        stop = threading.Event()
        stop.set()
        ok, why = run_shot_translate_step({"variable": "v"}, {}, stop)
        self.assertFalse(ok)
        self.assertIn("已手动停止", why)

    def test_ocr_unavailable_fails(self):
        with self._patch_ocr(None, ok=False, why="缺少 RapidOCR 库"):
            ok, why = run_shot_translate_step({"variable": "v"}, {})
        self.assertFalse(ok)
        self.assertIn("RapidOCR", why)

    def test_no_text_recognized_fails(self):
        with self._patch_ocr([]):
            ok, why = run_shot_translate_step({"variable": "v"}, {})
        self.assertFalse(ok)
        self.assertIn("未在截图区域识别到文字", why)

    def test_blank_lines_treated_as_empty(self):
        with self._patch_ocr(["  ", "", "\t"]):
            ok, why = run_shot_translate_step({"variable": "v"}, {})
        self.assertFalse(ok)
        self.assertIn("未在截图区域识别到文字", why)

    def test_success_writes_translation_variable(self):
        vars_: dict = {}
        with self._patch_ocr(["Hello world", "Good morning"]), \
             self._patch_translate():
            ok, why = run_shot_translate_step({"variable": "译文"}, vars_)
        self.assertTrue(ok)
        self.assertEqual(vars_["译文"], "你好世界")
        self.assertIn("译文", why)

    def test_source_var_written_only_when_set(self):
        vars_: dict = {}
        with self._patch_ocr(["Hello", "World"]), self._patch_translate():
            run_shot_translate_step({"variable": "t"}, vars_)
        self.assertNotIn("src", vars_)
        self.assertEqual(list(vars_), ["t"])

    def test_keeps_line_breaks_by_default(self):
        captured = {}

        def fake_translate(text, **kw):
            captured["text"] = text
            return True, {"text": "译文", "source_lang": "en"}, "ok"

        with self._patch_ocr(["Line one", "Line two"]), \
             mock.patch.object(translate_actor, "translate", side_effect=fake_translate):
            run_shot_translate_step({"variable": "t", "source_var": "src"}, {})
        self.assertEqual(captured["text"], "Line one\nLine two")

    def test_merge_lines_joins_with_space(self):
        captured = {}
        vars_: dict = {}

        def fake_translate(text, **kw):
            captured["text"] = text
            return True, {"text": "译文", "source_lang": "en"}, "ok"

        with self._patch_ocr(["Line one", "Line two"]), \
             mock.patch.object(translate_actor, "translate", side_effect=fake_translate):
            run_shot_translate_step({"variable": "t", "source_var": "src",
                                     "merge_lines": True}, vars_)
        self.assertEqual(captured["text"], "Line one Line two")
        self.assertEqual(vars_["src"], "Line one Line two")   # 原文变量同样按合并后存

    def test_translate_failure_fails_and_writes_nothing(self):
        vars_: dict = {}
        with self._patch_ocr(["Hello"]), \
             mock.patch.object(translate_actor, "translate",
                               return_value=(False, None, "无法连接谷歌翻译")):
            ok, why = run_shot_translate_step({"variable": "t", "source_var": "s"}, vars_)
        self.assertFalse(ok)
        self.assertIn("无法连接", why)
        self.assertEqual(vars_, {})

    def test_empty_translation_fails(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(text="   "):
            ok, why = run_shot_translate_step({"variable": "t"}, {})
        self.assertFalse(ok)
        self.assertIn("翻译结果为空", why)

    def test_languages_timeout_proxy_and_region_passed_through(self):
        """语言/超时/代理照旧透传；区域来自**运行时框选**（不再是 params 里的值）。"""
        seen = {}

        def fake_translate(text, source=None, target=None, timeout=None, proxy=None):
            seen.update(source=source, target=target, timeout=timeout, proxy=proxy)
            return True, {"text": "ok", "source_lang": "en"}, "ok"

        with self._patch_ocr(["Hello"]) as ocr_mock, \
             mock.patch.object(translate_actor, "translate", side_effect=fake_translate):
            run_shot_translate_step({"variable": "t",
                                     "source_lang": "en", "target_lang": "ja",
                                     "timeout_sec": 7.5, "proxy": "127.0.0.1:7890"}, {})
        self.assertEqual(ocr_mock.call_args.kwargs["region"], "10,20,300,80")
        self.assertEqual(seen["source"], "en")
        self.assertEqual(seen["target"], "ja")
        self.assertEqual(seen["timeout"], 7.5)
        self.assertEqual(seen["proxy"], "127.0.0.1:7890")

    # ---------- 运行时框选（区域不再预设）----------
    def test_selection_goes_through_ui_call(self):
        """遮罩是 QWidget：必须经 ui_call 调度到主线程，不能在后台线程直接建。"""
        with self._patch_ocr(["Hello"]), self._patch_translate():
            run_shot_translate_step({"variable": "t"}, {})
        self.ui_call.assert_called_once_with(shot_actor.select_region)
        self.select_region.assert_called_once_with()

    def test_legacy_region_param_is_ignored(self):
        """旧流程里残留的 region 必须被忽略，实际用的是用户这次框选的区域。"""
        with self._patch_ocr(["Hello"]) as ocr_mock, self._patch_translate():
            ok, _ = run_shot_translate_step(
                {"variable": "t", "region": "0,0,9999,9999"}, {})
        self.assertTrue(ok)
        self.assertEqual(ocr_mock.call_args.kwargs["region"], "10,20,300,80")

    def test_cancel_selection_fails_without_ocr(self):
        """Esc 取消框选即判失败，且不去做 OCR / 翻译（不静默跳过继续跑）。"""
        self.select_region.return_value = None
        with self._patch_ocr(["Hello"]) as ocr_mock, self._patch_translate() as tr:
            ok, why = run_shot_translate_step({"variable": "t"}, {})
        self.assertFalse(ok)
        self.assertIn("已取消框选", why)
        ocr_mock.assert_not_called()
        tr.assert_not_called()

    def test_tiny_selection_fails(self):
        self.select_region.return_value = (10, 20, 0, 50)
        with self._patch_ocr(["Hello"]) as ocr_mock:
            ok, why = run_shot_translate_step({"variable": "t"}, {})
        self.assertFalse(ok)
        self.assertIn("框选区域过小", why)
        ocr_mock.assert_not_called()

    def test_stop_after_selection_aborts_before_ocr(self):
        """框选完才发现被停止：及时退出，不再继续识别。"""
        stop = threading.Event()

        def fake_select():
            stop.set()
            return (1, 2, 30, 40)

        self.select_region.side_effect = fake_select
        with self._patch_ocr(["Hello"]) as ocr_mock:
            ok, why = run_shot_translate_step({"variable": "t"}, {}, stop)
        self.assertFalse(ok)
        self.assertIn("已手动停止", why)
        ocr_mock.assert_not_called()

    def test_copy_clipboard(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.tasks.pyperclip.copy") as cp:
            ok, why = run_shot_translate_step({"variable": "t",
                                               "copy_clipboard": True}, {})
        self.assertTrue(ok)
        cp.assert_called_once_with("你好世界")

    def test_clipboard_failure_does_not_fail_step(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.tasks.pyperclip.copy",
                        side_effect=RuntimeError("剪贴板被占用")):
            ok, why = run_shot_translate_step({"variable": "t",
                                               "copy_clipboard": True}, {})
        self.assertTrue(ok)          # 剪贴板是附带动作，失败不影响步骤
        self.assertIn("剪贴板失败", why)

    def test_clipboard_not_touched_when_unchecked(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.tasks.pyperclip.copy") as cp:
            run_shot_translate_step({"variable": "t"}, {})
        cp.assert_not_called()

    def test_notify_shown_with_duration(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification") as note:
            ok, _ = run_shot_translate_step({"variable": "t", "show_notify": True,
                                             "notify_seconds": 3}, {})
        self.assertTrue(ok)
        self.assertEqual(note.call_args[0][0], "你好世界")     # 没填原文变量时只显示译文
        self.assertEqual(note.call_args[0][2], 3.0)

    def test_notify_zero_seconds_means_manual_close(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification") as note:
            run_shot_translate_step({"variable": "t", "show_notify": True,
                                     "notify_seconds": 0}, {})
        self.assertEqual(note.call_args[0][2], 0.0)

    def test_notify_includes_source_when_source_var_set(self):
        with self._patch_ocr(["Hello world"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification") as note:
            run_shot_translate_step({"variable": "t", "source_var": "s",
                                     "show_notify": True}, {})
        body = note.call_args[0][0]
        self.assertIn("Hello world", body)
        self.assertIn("你好世界", body)

    def test_notify_failure_does_not_fail_step(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification", return_value=None):
            ok, why = run_shot_translate_step({"variable": "t",
                                               "show_notify": True}, {})
        self.assertTrue(ok)
        self.assertIn("未能显示", why)

    def test_notify_not_called_when_unchecked(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification") as note:
            run_shot_translate_step({"variable": "t"}, {})
        note.assert_not_called()

    def test_bad_notify_seconds_coerced(self):
        with self._patch_ocr(["Hello"]), self._patch_translate(), \
             mock.patch("app.notify_actor.show_notification") as note:
            ok, _ = run_shot_translate_step({"variable": "t", "show_notify": True,
                                             "notify_seconds": "abc"}, {})
        self.assertTrue(ok)
        self.assertEqual(note.call_args[0][2], 0.0)


# ---------- 参数对话框表单（区域改为运行时框选）----------

class TestShotTranslateDialogForm(unittest.TestCase):
    """「截图谷歌翻译」表单：不再有区域预设行；保存时清掉旧流程残留的 region。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dialog(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        p = default_step_params("shot_translate")
        p.update(params)
        return StepParamsDialog(FlowStep(type="shot_translate", params=p))

    def test_no_region_row(self):
        """表单里不该再有区域输入框（区域由用户运行时框选）。"""
        dlg = self._dialog()
        self.assertFalse(hasattr(dlg, "region_edit"))

    def test_form_explains_runtime_selection(self):
        """要有说明文字，否则用户会以为是漏了设置区域的地方。"""
        from PySide6.QtWidgets import QLabel
        dlg = self._dialog()
        texts = [w.text() for w in dlg.findChildren(QLabel)]
        self.assertTrue(any("框选" in t for t in texts), texts)

    def test_apply_drops_legacy_region(self):
        """旧流程带 region：保存时清掉，别留个看不懂的死参数。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="shot_translate", params={"region": "1,2,3,4"})
        StepParamsDialog(step).apply_to(step)
        self.assertNotIn("region", step.params)

    def test_apply_keeps_other_params(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="shot_translate")
        dlg = StepParamsDialog(step)
        dlg.st_source.setCurrentIndex(dlg.st_source.findData("en"))
        dlg.apply_to(step)
        self.assertEqual(step.params["source_lang"], "en")


# ---------- 配置层 / 模块面板 / 变量校验 ----------

class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("shot_translate"), "截图谷歌翻译")

    def test_default_params(self):
        p = default_step_params("shot_translate")
        # 区域改为运行时框选：默认参数里不该再有 region（2026-09-15）
        self.assertNotIn("region", p)
        self.assertEqual(p["source_lang"], "auto")
        self.assertEqual(p["target_lang"], "zh-CN")
        self.assertEqual(p["variable"], "")
        self.assertEqual(p["source_var"], "")
        self.assertFalse(p["merge_lines"])
        self.assertFalse(p["show_notify"])
        self.assertFalse(p["copy_clipboard"])
        self.assertEqual(p["notify_seconds"], 0.0)
        self.assertEqual(p["timeout_sec"], 10.0)
        self.assertEqual(p["proxy"], "")

    def test_output_fields(self):
        self.assertEqual(STEP_OUTPUT_FIELDS["shot_translate"], ("variable", "source_var"))

    def test_summary(self):
        s = FlowStep(type="shot_translate", params={
            "source_lang": "en", "target_lang": "zh-CN", "variable": "译文"})
        self.assertEqual(s.summary(), "运行时框选截屏 · 英语→中文（简体） → 译文")

    def test_summary_defaults_when_params_missing(self):
        s = FlowStep(type="shot_translate", params={})
        self.assertEqual(s.summary(), "运行时框选截屏 · 自动检测→中文（简体） → 未指定变量")

    def test_summary_ignores_legacy_region(self):
        """旧流程残留的 region 不再进摘要（免得看起来还以为有预设区域）。"""
        s = FlowStep(type="shot_translate",
                     params={"region": "10,20,300,80", "variable": "译文"})
        self.assertNotIn("10,20,300,80", s.summary())

    def test_serialization_roundtrip(self):
        flow = Flow(name="翻译流程", steps=[
            FlowStep(type="shot_translate", params={
                "source_lang": "ja", "target_lang": "zh-CN",
                "variable": "译文", "source_var": "原文", "merge_lines": True,
                "show_notify": True, "notify_seconds": 5.0, "copy_clipboard": True,
                "timeout_sec": 15.0, "proxy": "http://127.0.0.1:7890"}),
        ])
        back = flow_from_dict(flow_to_dict(flow))
        self.assertEqual(back.steps[0].type, "shot_translate")
        self.assertEqual(back.steps[0].params["proxy"], "http://127.0.0.1:7890")
        self.assertTrue(back.steps[0].params["merge_lines"])
        self.assertEqual(back.steps[0].params["target_lang"], "zh-CN")

    def test_step_name_defaults_to_display_name(self):
        self.assertEqual(FlowStep(type="shot_translate").name, "截图谷歌翻译")

    def test_in_module_panel_group(self):
        from app.ui.flow_tab import MODULE_GROUPS
        groups = {t for _, _, types in MODULE_GROUPS for t in types}
        self.assertIn("shot_translate", groups)

    def test_has_icon(self):
        from app.ui.flow_dialog import _TYPE_ICONS
        self.assertIn("shot_translate", _TYPE_ICONS)

    def test_module_group_covers_all_panel_types(self):
        """模块面板里的每个类型都必须有显示名和图标，否则按钮会崩。"""
        from app.ui.flow_dialog import _TYPE_ICONS
        from app.ui.flow_tab import MODULE_GROUPS
        for _, _, types in MODULE_GROUPS:
            for t in types:
                with self.subTest(step_type=t):
                    self.assertIn(t, FLOW_STEP_TYPES)
                    self.assertIn(t, _TYPE_ICONS)


class TestDefinedVariables(unittest.TestCase):
    def test_step_defines_both_variables(self):
        from app.conditions import _step_defined_variables
        self.assertEqual(
            _step_defined_variables("shot_translate", {"variable": "t", "source_var": "s"}),
            ["t", "s"])

    def test_source_var_optional(self):
        from app.conditions import _step_defined_variables
        self.assertEqual(
            _step_defined_variables("shot_translate", {"variable": "t"}), ["t"])
        self.assertEqual(_step_defined_variables("shot_translate", {}), [])

    def test_existing_types_unchanged(self):
        """重构后原有步骤类型的产出变量不能变。"""
        from app.conditions import _step_defined_variables
        self.assertEqual(_step_defined_variables("ocr", {"variable": "a"}), ["a"])
        self.assertEqual(_step_defined_variables("var", {"name": "b"}), ["b"])
        self.assertEqual(_step_defined_variables("var", {}), [])
        self.assertEqual(_step_defined_variables("foreach", {"item_var": "i",
                                                             "index_var": "n"}), ["i", "n"])
        self.assertEqual(_step_defined_variables("foreach", {"item_var": "i",
                                                             "index_var": "i"}), ["i"])
        self.assertEqual(_step_defined_variables("wait", {"seconds": 1}), [])


# ---------- 步骤参数对话框 ----------

class TestStepDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="shot_translate", params=params))

    def test_form_roundtrip(self):
        dlg = self._open({"region": "10,20,300,80", "source_lang": "ja",
                          "target_lang": "en", "variable": "译文",
                          "source_var": "原文", "merge_lines": True,
                          "show_notify": True, "notify_seconds": 4.5,
                          "copy_clipboard": True, "timeout_sec": 20.0,
                          "proxy": "127.0.0.1:7890"})
        self.assertEqual(dlg.st_source.currentData(), "ja")
        self.assertEqual(dlg.st_target.currentData(), "en")
        self.assertTrue(dlg.st_merge.isChecked())
        self.assertTrue(dlg.st_notify.isChecked())
        self.assertTrue(dlg.st_notify_sec.isEnabled())      # 勾了显示才可调时长
        self.assertEqual(dlg.st_notify_sec.value(), 4.5)
        self.assertTrue(dlg.st_clip.isChecked())
        self.assertEqual(dlg.st_timeout.value(), 20.0)
        self.assertEqual(dlg.st_proxy.text(), "127.0.0.1:7890")

        step = FlowStep(type="shot_translate")
        dlg.apply_to(step)
        self.assertEqual(step.params["source_lang"], "ja")
        self.assertEqual(step.params["target_lang"], "en")
        self.assertEqual(step.params["variable"], "译文")
        self.assertEqual(step.params["source_var"], "原文")
        self.assertTrue(step.params["merge_lines"])
        self.assertEqual(step.params["notify_seconds"], 4.5)
        self.assertEqual(step.params["timeout_sec"], 20.0)
        self.assertEqual(step.params["proxy"], "127.0.0.1:7890")

    def test_notify_seconds_disabled_until_checked(self):
        dlg = self._open({"show_notify": False})
        self.assertFalse(dlg.st_notify_sec.isEnabled())
        dlg.st_notify.setChecked(True)
        self.assertTrue(dlg.st_notify_sec.isEnabled())
        dlg.st_notify.setChecked(False)
        self.assertFalse(dlg.st_notify_sec.isEnabled())

    def test_defaults_filled_for_empty_params(self):
        dlg = self._open({})
        self.assertEqual(dlg.st_source.currentData(), "auto")
        self.assertEqual(dlg.st_target.currentData(), "zh-CN")
        self.assertEqual(dlg.st_timeout.value(), 10.0)

    def test_has_continue_on_fail_checkbox(self):
        dlg = self._open({})
        self.assertTrue(hasattr(dlg, "continue_box"))
        self.assertFalse(dlg.continue_box.isChecked())      # 默认失败终止流程

    def test_continue_on_fail_saved(self):
        dlg = self._open({})
        dlg.continue_box.setChecked(True)
        step = FlowStep(type="shot_translate")
        dlg.apply_to(step)
        self.assertTrue(step.continue_on_fail)

    def test_language_combos_not_editable(self):
        """语言用固定下拉，避免手填错误代码被接口静默忽略。"""
        dlg = self._open({})
        self.assertFalse(dlg.st_source.isEditable())
        self.assertFalse(dlg.st_target.isEditable())


if __name__ == "__main__":
    unittest.main()
