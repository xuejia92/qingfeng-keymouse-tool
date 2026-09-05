"""语音播报（speech）步骤的测试。

覆盖：类型注册/默认参数/摘要/序列化、speech_actor（fake pyttsx3 全链路：
引擎惰性启动于专用播放线程、say 调用、结果带回、空文本/初始化失败）、
run_speech_step 的 $变量名 解析与 wait 分支、参数对话框构建/回填/校验。
真实发声在测试里完全规避（用假引擎替换 pyttsx3）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params, \
    flow_from_dict, flow_to_dict


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("speech"), "语音播报")

    def test_default_params(self):
        p = default_step_params("speech")
        self.assertEqual(p["content"], "")
        self.assertEqual(p["wait"], True)


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="speech")
        s.params.update(params)
        return s.summary()

    def test_content_shown(self):
        self.assertIn("语音播报", self._summary(content="你好"))

    def test_empty_content(self):
        self.assertIn("空内容", self._summary(content="   "))

    def test_wait_false_marked(self):
        self.assertIn("后台播放", self._summary(content="你好", wait=False))

    def test_wait_true_no_mark(self):
        s = self._summary(content="你好", wait=True)
        self.assertNotIn("后台播放", s)

    def test_long_content_truncated(self):
        s = self._summary(content="这是一段非常非常非常非常非常非常非常非常长的播报内容")
        self.assertLessEqual(len(s), 30)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="播报流程", steps=[FlowStep(type="speech", name="说一句", params={
            "content": "流程已完成 $name", "wait": False,
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["content"], "流程已完成 $name")
        self.assertEqual(p["wait"], False)


# ---------- speech_actor（假 pyttsx3 全链路） ----------

class _FakeEngine:
    """假 pyttsx3 引擎：记录 say，runAndWait 立即返回（不真发声）。"""

    def __init__(self):
        self.said = []
        self.voice = None

    def setProperty(self, name, value):
        if name == "voice":
            self.voice = value

    def getProperty(self, name):
        return [] if name == "voices" else None

    def say(self, text):
        self.said.append(text)

    def runAndWait(self):
        pass


class _FakePyttsx3:
    def __init__(self, engine):
        self._engine = engine

    def init(self, *a, **kw):
        return self._engine


class TestSpeechActor(unittest.TestCase):
    def setUp(self):
        from app import speech_actor
        self._actor = speech_actor
        self._actor._reset_for_test()

    def test_speak_empty_fails(self):
        ok, why = self._actor.speak("   ")
        self.assertFalse(ok)
        self.assertIn("空", why)

    def test_speak_async_empty_fails(self):
        ok, why = self._actor.speak_async("")
        self.assertFalse(ok)
        self.assertIn("空", why)

    def test_speak_runs_in_worker_with_fake_engine(self):
        """替换 pyttsx3 为假引擎：worker 线程消费任务、say 收到文本、结果带回。"""
        engine = _FakeEngine()
        sys.modules["pyttsx3"] = _FakePyttsx3(engine)
        try:
            ok, why = self._actor.speak("你好，世界")
            self.assertTrue(ok, why)
            self.assertEqual(engine.said, ["你好，世界"])
        finally:
            sys.modules.pop("pyttsx3", None)

    def test_speak_init_failure_reported(self):
        """pyttsx3.init 抛异常：speak 返回失败并带原因，不崩 worker。"""
        class _Bad:
            def init(self, *a, **kw):
                raise RuntimeError("no audio device")
        sys.modules["pyttsx3"] = _Bad()
        try:
            ok, why = self._actor.speak("测试")
            self.assertFalse(ok)
            self.assertIn("语音引擎", why)
        finally:
            sys.modules.pop("pyttsx3", None)

    def test_speak_twice_delivers_both(self):
        """回归：连续两次播报都必须真正投递（引擎不复用，规避 pyttsx3
        第二次 runAndWait 提前返回的 bug）。"""
        engine = _FakeEngine()

        class _Counting:
            def __init__(self):
                self.inits = 0

            def init(self, *a, **kw):
                self.inits += 1
                return engine
        fake = _Counting()
        sys.modules["pyttsx3"] = fake
        try:
            ok1, _ = self._actor.speak("第一段")
            ok2, _ = self._actor.speak("第二段")
            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertEqual(fake.inits, 2)            # 每段都新建引擎
            self.assertEqual(engine.said, ["第一段", "第二段"])
        finally:
            sys.modules.pop("pyttsx3", None)


# ---------- run_speech_step ----------

class TestRunSpeechStep(unittest.TestCase):
    def test_wait_true_calls_speak(self):
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak", return_value=(True, "语音播报完成")) as sp:
            ok, why = run_speech_step({"content": "你好", "wait": True}, {"name": "张三"})
        self.assertTrue(ok)
        sp.assert_called_once_with("你好")

    def test_resolves_variable(self):
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak", return_value=(True, "语音播报完成")) as sp:
            ok, _ = run_speech_step({"content": "$name 你好", "wait": True},
                                    {"name": "张三"})
        self.assertTrue(ok)
        sp.assert_called_once_with("张三 你好")

    def test_wait_false_calls_speak_async(self):
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak_async",
                        return_value=(True, "已提交后台语音播报（不等待）")) as sa:
            ok, why = run_speech_step({"content": "你好", "wait": False}, {})
        self.assertTrue(ok)
        sa.assert_called_once_with("你好")
        self.assertIn("不等待", why)

    def test_speak_failure_propagates(self):
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak",
                        return_value=(False, "语音引擎初始化失败")):
            ok, why = run_speech_step({"content": "你好", "wait": True}, {})
        self.assertFalse(ok)
        self.assertIn("语音引擎", why)

    def test_empty_content_fails(self):
        """内容解析 $变量名 后为空（变量值为空）：失败且不调用引擎。"""
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak") as sp:
            ok, why = run_speech_step({"content": "$name", "wait": True},
                                      {"name": "   "})
        self.assertFalse(ok)
        self.assertIn("空", why)
        sp.assert_not_called()

    def test_unresolved_reference_preserved(self):
        """变量未定义时保留 $变量名 原文朗读（不静默失败，内容非空即可播）。"""
        from app.tasks import run_speech_step
        with mock.patch("app.speech_actor.speak", return_value=(True, "语音播报完成")) as sp:
            ok, _ = run_speech_step({"content": "结果是 $unknown", "wait": True}, {})
        self.assertTrue(ok)
        sp.assert_called_once_with("结果是 $unknown")

    def test_stopped(self):
        import threading
        from app.tasks import run_speech_step
        stop = threading.Event()
        stop.set()
        with mock.patch("app.speech_actor.speak") as sp:
            ok, why = run_speech_step({"content": "你好", "wait": True}, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")
        sp.assert_not_called()


# ---------- 对话框 ----------

class TestSpeechDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="speech", params=params))

    def test_defaults(self):
        dlg = self._open({"content": "", "wait": True})
        self.assertTrue(dlg.sp_wait.isChecked())
        self.assertEqual(dlg.sp_content.toPlainText(), "")

    def test_apply_roundtrip(self):
        dlg = self._open({"content": "流程已完成 $name", "wait": False})
        self.assertEqual(dlg.sp_content.toPlainText(), "流程已完成 $name")
        self.assertFalse(dlg.sp_wait.isChecked())
        step = FlowStep(type="speech")
        dlg.apply_to(step)
        self.assertEqual(step.params["content"], "流程已完成 $name")
        self.assertEqual(step.params["wait"], False)

    def test_accept_requires_content(self):
        dlg = self._open({"content": "", "wait": True})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("播报内容", warn.call_args.args[1])

    def test_accept_passes_with_content(self):
        dlg = self._open({"content": "你好", "wait": True})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()

    def test_insert_var_disabled_when_no_flow_vars(self):
        """流程中没有变量步骤时「插入变量」下拉禁用并有引导文案。"""
        dlg = self._open({"content": "", "wait": True})
        self.assertFalse(dlg.sp_var.isEnabled())
        self.assertIn("暂无变量", dlg.sp_var.currentText())


if __name__ == "__main__":
    unittest.main()
