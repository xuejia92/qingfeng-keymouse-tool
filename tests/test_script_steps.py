"""执行脚本（script）步骤的测试。

运行期行为在真实 cmd.exe / powershell.exe 上验证（隐藏窗口、非提权路径），
不依赖外网、不触发 UAC。覆盖：类型注册、默认参数、摘要、序列化往返、
脚本内容/文件两种来源、CMD 与 PowerShell 执行、GB2312 中文输出、ASCII 编码报错、
空内容/文件缺失报错，以及 run_script_step 写结果变量与 $变量名 引用。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params, \
    flow_from_dict, flow_to_dict
from app.script_actor import ScriptError, run_script
from app.tasks import run_script_step
class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("script"), "执行脚本")

    def test_default_params(self):
        p = default_step_params("script")
        self.assertEqual(p["script_type"], "cmd")
        self.assertEqual(p["source"], "text")
        self.assertEqual(p["encoding"], "utf-8")
        self.assertEqual(p["window_mode"], "hidden")
        self.assertFalse(p["admin"])
        self.assertEqual(p["timeout"], 120.0)
        self.assertEqual(p["result_var"], "")


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="script")
        s.params.update(params)
        return s.summary()

    def test_text_source(self):
        self.assertEqual(self._summary(source="text", script_type="cmd",
                                       result_var="out"), "执行脚本 CMD 文本内容 → out")

    def test_file_source(self):
        s = self._summary(source="file", script_type="powershell",
                          path="C:\\x\\run.ps1", result_var="r")
        self.assertIn("PowerShell", s)
        self.assertIn("run.ps1", s)
        self.assertIn("r", s)

    def test_admin_flag(self):
        self.assertIn("管理员", self._summary(source="text", admin=True, result_var="o"))

    def test_python_type(self):
        s = self._summary(source="text", script_type="python", result_var="r")
        self.assertIn("Python", s)
        self.assertIn("r", s)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="脚本流程", steps=[FlowStep(type="script", name="跑脚本", params={
            "script_type": "powershell", "source": "text", "content": "echo hi",
            "encoding": "utf-8", "window_mode": "hidden", "admin": True,
            "timeout": 30.0, "result_var": "result",
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["script_type"], "powershell")
        self.assertEqual(p["result_var"], "result")
        self.assertEqual(p["encoding"], "utf-8")
        self.assertTrue(p["admin"])


class TestActorValidation(unittest.TestCase):
    def test_empty_content(self):
        with self.assertRaises(ScriptError) as cm:
            run_script(script_type="cmd", source="text", content="   ")
        self.assertIn("为空", str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(ScriptError) as cm:
            run_script(script_type="cmd", source="file", path="Z:\\不存在\\x.bat")
        self.assertIn("不存在", str(cm.exception))

    def test_ascii_encoding_rejects_chinese(self):
        with self.assertRaises(ScriptError) as cm:
            run_script(script_type="cmd", source="text", content="echo 中文",
                       encoding="ascii")
        self.assertIn("ASCII", str(cm.exception))


class TestLauncherGeneration(unittest.TestCase):
    """启动器生成回归：keep 模式曾因中文标签 + ascii 编码崩溃。"""

    def test_keep_launcher_is_ascii_safe(self):
        from app.script_actor import _cmd_launcher
        s = _cmd_launcher("C:\\tmp\\run.bat", "C:\\tmp\\out.txt", keep=True, chcp="936")
        s.encode("ascii")   # 标签为纯 ASCII，不应抛 UnicodeEncodeError

    def test_chinese_path_launcher_encodes_gbk(self):
        from app.script_actor import _cmd_launcher
        s = _cmd_launcher("D:\\脚本\\run.bat", "C:\\tmp\\out.txt", keep=False, chcp="936")
        s.encode("gbk")     # 中文路径可用 gbk 编码，不应抛异常

    def test_keep_mode_chinese_content_runs(self):
        # 中文内容 + 保留命令窗口：启动器生成阶段不再因 ascii 编码崩溃
        # （不真正执行 keep 的 pause 阻塞，只验证启动器可生成/编码）。
        from app.script_actor import _cmd_launcher, _CMD_CHCP
        s = _cmd_launcher("C:\\tmp\\qf_script_x.bat", "C:\\tmp\\out.txt",
                          keep=True, chcp=_CMD_CHCP["gb2312"])
        s.encode("gbk")


@unittest.skipUnless(os.name == "nt", "脚本执行依赖 Windows cmd/powershell")
class TestRunScript(unittest.TestCase):
    def test_cmd_echo(self):
        r = run_script(script_type="cmd", source="text", content="echo hello\r\n",
                       window_mode="hidden")
        self.assertIn("hello", r["output"])

    def test_powershell_output(self):
        r = run_script(script_type="powershell", source="text",
                       content="Write-Output 'hi-there'\r\n", window_mode="hidden")
        self.assertIn("hi-there", r["output"])

    def test_bat_file_source(self):
        fd, path = tempfile.mkstemp(prefix="qf_test_", suffix=".bat")
        with os.fdopen(fd, "w", encoding="gbk") as f:
            f.write("@echo off\r\necho from_file\r\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        r = run_script(script_type="bat", source="file", path=path,
                       window_mode="hidden")
        self.assertIn("from_file", r["output"])

    def test_gb2312_chinese_output(self):
        r = run_script(script_type="cmd", source="text", content="echo 你好\r\n",
                       encoding="gb2312", window_mode="hidden")
        self.assertIn("你好", r["output"])

    def test_nonzero_exit_code_reported(self):
        r = run_script(script_type="cmd", source="text",
                       content="exit /b 7\r\n", window_mode="hidden")
        self.assertEqual(r["returncode"], 7)


@unittest.skipUnless(os.name == "nt", "脚本执行依赖 Windows")
class TestRunPythonScript(unittest.TestCase):
    _has_py = bool(shutil.which("python") or shutil.which("py"))
    _reason = "未找到 python 解释器"

    @unittest.skipIf(not _has_py, _reason)
    def test_python_output(self):
        r = run_script(script_type="python", source="text",
                       content="print('hello-py')\n", window_mode="hidden")
        self.assertIn("hello-py", r["output"])

    @unittest.skipIf(not _has_py, _reason)
    def test_python_chinese_utf8(self):
        r = run_script(script_type="python", source="text",
                       content="print('你好世界')\n", encoding="utf-8",
                       window_mode="hidden")
        self.assertIn("你好世界", r["output"])

    @unittest.skipIf(not _has_py, _reason)
    def test_python_gb2312_chinese(self):
        r = run_script(script_type="python", source="text",
                       content="print('中文输出')\n", encoding="gb2312",
                       window_mode="hidden")
        self.assertIn("中文输出", r["output"])

    @unittest.skipIf(not _has_py, _reason)
    def test_python_stderr_captured(self):
        r = run_script(script_type="python", source="text",
                       content="import sys; print('to-stderr', file=sys.stderr)\n",
                       window_mode="hidden")
        self.assertIn("to-stderr", r["output"])

    @unittest.skipIf(not _has_py, _reason)
    def test_python_file_source(self):
        fd, path = tempfile.mkstemp(prefix="qf_test_", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("print('from_py_file')\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        r = run_script(script_type="python", source="file", path=path,
                       window_mode="hidden")
        self.assertIn("from_py_file", r["output"])


class TestRunScriptStep(unittest.TestCase):
    def test_writes_result_var_and_resolves_refs(self):
        vars_ = {"name": "张三"}
        p = {"script_type": "cmd", "source": "text", "content": "echo hello $name\r\n",
             "result_var": "out", "window_mode": "hidden"}
        ok, why = run_script_step(p, vars_)
        self.assertTrue(ok, why)
        self.assertIn("hello", vars_["out"])
        self.assertIn("张三", vars_["out"])

    def test_empty_content_fails(self):
        ok, why = run_script_step({"source": "text", "content": "", "result_var": "a"}, {})
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_missing_file_fails(self):
        ok, why = run_script_step(
            {"source": "file", "path": "Z:\\不存在\\x.bat", "result_var": "a"}, {})
        self.assertFalse(ok)
        self.assertIn("不存在", why)


class TestFormSmoke(unittest.TestCase):
    """脚本步骤参数对话框的构建 / 回填 / 提交冒烟测试（offscreen）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="script", params=params))

    def test_build_and_fill(self):
        dlg = self._dlg(script_type="powershell", source="text",
                        content="echo hi", encoding="utf-8",
                        window_mode="keep", admin=True, timeout=30.0,
                        result_var="out")
        self.assertEqual(dlg.sc_type.currentData(), "powershell")
        self.assertTrue(dlg.sc_src_text_radio.isChecked())
        self.assertIn("echo hi", dlg.sc_content.toPlainText())
        self.assertEqual(dlg.sc_encoding.currentData(), "utf-8")
        self.assertEqual(dlg.sc_window.currentData(), "keep")
        self.assertTrue(dlg.sc_admin.isChecked())
        self.assertEqual(dlg.sc_timeout.value(), 30.0)
        self.assertEqual(dlg._combo_value(dlg.sc_result_var), "out")

    def test_apply_roundtrip(self):
        dlg = self._dlg()
        dlg.sc_type.setCurrentIndex(dlg.sc_type.findData("bat"))
        dlg.sc_src_file_radio.setChecked(True)
        dlg.sc_path.setText("D:\\x\\run.bat")
        dlg.sc_encoding.setCurrentIndex(dlg.sc_encoding.findData("ascii"))
        dlg.sc_window.setCurrentIndex(dlg.sc_window.findData("hidden"))
        dlg.sc_admin.setChecked(False)
        dlg.sc_result_var.setCurrentIndex(dlg.sc_result_var.findData("out"))
        step = FlowStep(type="script")
        dlg.apply_to(step)
        self.assertEqual(step.params["script_type"], "bat")
        self.assertEqual(step.params["source"], "file")
        self.assertEqual(step.params["path"], "D:\\x\\run.bat")
        self.assertEqual(step.params["encoding"], "ascii")
        self.assertEqual(step.params["window_mode"], "hidden")
        self.assertFalse(step.params["admin"])

    def test_source_switch_shows_correct_row(self):
        dlg = self._dlg()
        dlg.sc_src_file_radio.setChecked(True)
        self.assertTrue(dlg._sc_content_widget.isHidden())
        self.assertFalse(dlg._sc_path_widget.isHidden())
        dlg.sc_src_text_radio.setChecked(True)
        self.assertFalse(dlg._sc_content_widget.isHidden())
        self.assertTrue(dlg._sc_path_widget.isHidden())


if __name__ == "__main__":
    unittest.main()
