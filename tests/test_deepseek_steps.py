"""DeepSeek 对话（deepseek）步骤的测试。

运行期行为用一个本机临时 HTTP 服务器模拟 DeepSeek 的 OpenAI 兼容端点验证
（不依赖外网、不依赖真实 API Key），覆盖：非流式/流式解析、thinking 思考模式、
请求体组装、401 报错、空 Key / 空提问、$变量名 引用，以及默认值与序列化。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params, \
    flow_from_dict, flow_to_dict
from app.deepseek_actor import DeepSeekError, chat
from app.tasks import run_deepseek_step


class _Server:
    def __init__(self, handler_cls):
        self._srv = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class _ChatHandler(BaseHTTPRequestHandler):
    """非流式：记录请求体，返回 content（thinking 时附带 reasoning_content）。"""

    captured = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        body = json.loads(raw) if raw else {}
        type(self).captured.append(body)
        msg = {"role": "assistant", "content": "你好，世界"}
        if body.get("thinking"):
            msg["reasoning_content"] = "让我想想…"
        resp = {
            "id": "cmpl-1",
            "model": body.get("model"),
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        }
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class _StreamHandler(BaseHTTPRequestHandler):
    """流式：返回 SSE，含 reasoning_content 与多段 content。"""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)

        def ev(payload):
            return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(ev({"choices": [{"delta": {"reasoning_content": "思考1"}}]}))
        self.wfile.write(ev({"choices": [{"delta": {"content": "你好"}}]}))
        self.wfile.write(ev({"choices": [{"delta": {"content": "，世界"}}]}))
        self.wfile.write(ev({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        self.wfile.write(b"data: [DONE]\n\n")


class _ErrorHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"error": {"message": "Incorrect API key",
                                     "type": "invalid_request_error"}}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("deepseek"), "DeepSeek 对话")

    def test_default_params(self):
        p = default_step_params("deepseek")
        self.assertEqual(p["model"], "deepseek-v4-flash")
        self.assertFalse(p["thinking"])
        self.assertFalse(p["stream"])
        self.assertEqual(p["timeout"], 60.0)
        self.assertTrue(p["use_proxy"])
        self.assertEqual(p["result_var"], "")


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="deepseek")
        s.params.update(params)
        return s.summary()

    def test_basic(self):
        self.assertEqual(self._summary(model="deepseek-v4-pro", question="你好"),
                         "deepseek-v4-pro：你好")

    def test_without_question(self):
        self.assertIn("未填写提问", self._summary(question=""))

    def test_long_question_truncated(self):
        s = self._summary(question="x" * 100)
        self.assertIn("…", s)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="对话流程", steps=[FlowStep(type="deepseek", name="问一下", params={
            "model": "deepseek-v4-pro", "question": "你好", "thinking": True,
            "result_var": "answer",
        })])
        back = flow_from_dict(flow_to_dict(f))
        self.assertEqual(back.steps[0].params["model"], "deepseek-v4-pro")
        self.assertEqual(back.steps[0].params["result_var"], "answer")


class TestActorValidation(unittest.TestCase):
    def test_empty_api_key(self):
        old = __import__("os").environ.pop("DEEPSEEK_API_KEY", None)
        try:
            with self.assertRaises(DeepSeekError) as cm:
                chat(api_key="", model="m", system="", question="hi")
            self.assertIn("API Key", str(cm.exception))
        finally:
            if old is not None:
                __import__("os").environ["DEEPSEEK_API_KEY"] = old

    def test_empty_question(self):
        with self.assertRaises(DeepSeekError) as cm:
            chat(api_key="k", model="m", system="", question="  ")
        self.assertIn("提问", str(cm.exception))


class TestChat(unittest.TestCase):
    def test_non_stream_and_request_body(self):
        _ChatHandler.captured = []
        srv = _Server(_ChatHandler)
        self.addCleanup(srv.close)
        r = chat(api_key="sk-test", model="deepseek-v4-flash", system="你是助手",
                 question="你好", stream=False, timeout=5,
                 use_proxy=False, base_url=srv.url)
        self.assertEqual(r["content"], "你好，世界")
        self.assertEqual(r["reasoning"], "")
        req = _ChatHandler.captured[-1]
        self.assertEqual(req["model"], "deepseek-v4-flash")
        self.assertFalse(req["stream"])
        self.assertEqual(req["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(req["messages"][1], {"role": "user", "content": "你好"})
        self.assertNotIn("thinking", req)

    def test_thinking_enabled(self):
        _ChatHandler.captured = []
        srv = _Server(_ChatHandler)
        self.addCleanup(srv.close)
        r = chat(api_key="sk-test", model="deepseek-v4-pro", system="", question="hi",
                 thinking=True, stream=False, timeout=5,
                 use_proxy=False, base_url=srv.url)
        self.assertEqual(r["content"], "你好，世界")
        self.assertEqual(r["reasoning"], "让我想想…")
        req = _ChatHandler.captured[-1]
        self.assertEqual(req["reasoning_effort"], "high")
        self.assertEqual(req["thinking"], {"type": "enabled"})

    def test_stream_concatenates(self):
        srv = _Server(_StreamHandler)
        self.addCleanup(srv.close)
        r = chat(api_key="sk-test", model="deepseek-v4-flash", system="", question="hi",
                 stream=True, timeout=5, use_proxy=False, base_url=srv.url)
        self.assertEqual(r["content"], "你好，世界")
        self.assertEqual(r["reasoning"], "思考1")

    def test_http_error_raises(self):
        srv = _Server(_ErrorHandler)
        self.addCleanup(srv.close)
        with self.assertRaises(DeepSeekError) as cm:
            chat(api_key="bad", model="m", system="", question="hi",
                 timeout=5, use_proxy=False, base_url=srv.url)
        self.assertIn("401", str(cm.exception))
        self.assertIn("Incorrect API key", str(cm.exception))


class TestRunDeepSeekStep(unittest.TestCase):
    def test_writes_result_var_and_resolves_refs(self):
        _ChatHandler.captured = []
        srv = _Server(_ChatHandler)
        self.addCleanup(srv.close)
        vars_ = {"name": "张三"}
        p = {"api_key": "sk-test", "model": "deepseek-v4-flash", "system": "你是助手",
             "question": "请问 $name", "result_var": "answer", "stream": False,
             "timeout": 5, "use_proxy": False, "base_url": srv.url}
        ok, why = run_deepseek_step(p, vars_)
        self.assertTrue(ok, why)
        self.assertEqual(vars_["answer"], "你好，世界")
        # 提问里的 $name 已被替换
        self.assertEqual(_ChatHandler.captured[-1]["messages"][1]["content"], "请问 张三")

    def test_empty_question_fails(self):
        ok, why = run_deepseek_step({"api_key": "k", "question": "", "result_var": "a"},
                                    {})
        self.assertFalse(ok)
        self.assertIn("提问", why)


if __name__ == "__main__":
    unittest.main()
