"""网络请求（http_request）步骤的测试。

运行期行为用一个本机临时 HTTP 服务器验证（不依赖外网、不依赖代理）：
覆盖 GET/POST、请求头/Cookie、状态码、响应头、响应 Cookie、文本/图片结果、
4xx 不判失败、空网址/超时/连接失败的报错，以及默认值与序列化。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import http_actor
from app.config import (DEFAULT_USER_AGENT, FLOW_STEP_TYPES, Flow, FlowStep,
                        default_step_params, flow_from_dict, flow_to_dict)
from app.tasks import run_http_request_step


class _Server:
    """本机临时 HTTP 服务器：自动绑定空闲端口，close() 干净停掉服务线程。"""

    def __init__(self, handler_cls):
        self._srv = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class _EchoHandler(BaseHTTPRequestHandler):
    """回显请求：返回 JSON（方法/请求体/UA/Cookie/请求头），写 Set-Cookie。"""

    def log_message(self, *a):
        pass

    def _respond(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        payload = json.dumps({
            "method": self.command,
            "body": body,
            "ua": self.headers.get("User-Agent"),
            "cookie": self.headers.get("Cookie"),
            "auth": self.headers.get("Authorization"),
        })
        self.send_response(201)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", "sid=abc123; Path=/")
        self.send_header("X-Test", "yes")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    do_GET = _respond
    do_POST = _respond


class _NotFoundHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)


class _SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        time.sleep(0.5)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


class _ImageHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(data)


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("http_request"), "网络请求")

    def test_default_params(self):
        p = default_step_params("http_request")
        self.assertEqual(p["method"], "get")
        self.assertEqual(p["result_type"], "text")
        self.assertEqual(p["timeout"], 5.0)
        self.assertTrue(p["use_proxy"])
        self.assertEqual(p["proxy"], "127.0.0.1:7897")
        self.assertEqual(p["url"], "")
        for k in ("status_var", "headers_var", "cookie_var", "text_var"):
            self.assertEqual(p[k], "")

    def test_default_user_agent(self):
        p = default_step_params("http_request")
        self.assertEqual(p["user_agent"], DEFAULT_USER_AGENT)
        self.assertIn("Chrome/112.0.0.0", p["user_agent"])


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="http_request")
        s.params.update(params)
        return s.summary()

    def test_get(self):
        self.assertEqual(self._summary(method="get", url="https://a.com"),
                         "GET https://a.com")

    def test_post(self):
        self.assertEqual(self._summary(method="post", url="https://a.com/x"),
                         "POST https://a.com/x")

    def test_without_url(self):
        self.assertIn("未填网址", self._summary(url=""))

    def test_long_url_truncated(self):
        s = self._summary(url="https://" + "x" * 100)
        self.assertIn("…", s)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="请求流程", steps=[FlowStep(type="http_request", name="拉取数据", params={
            "url": "https://a.com", "method": "post", "body": "x=1",
            "result_type": "text", "timeout": 8.0, "use_proxy": False,
        })])
        data = flow_to_dict(f)
        back = flow_from_dict(data)
        self.assertIsNotNone(back)
        step = back.steps[0]
        self.assertEqual(step.params["url"], "https://a.com")
        self.assertEqual(step.params["method"], "post")
        self.assertEqual(step.params["timeout"], 8.0)

    def test_unknown_keys_preserved(self):
        s = FlowStep(type="http_request", params={"url": "https://a.com", "future": 1})
        self.assertIn("future", s.params)


class TestParseHeaders(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(http_actor.parse_headers("A: 1\nB:2\nC: 3"),
                         {"A": "1", "B": "2", "C": "3"})

    def test_skips_invalid_lines(self):
        self.assertEqual(http_actor.parse_headers("badline\nA: 1\n\n:empty\nB:2"),
                         {"A": "1", "B": "2"})

    def test_empty(self):
        self.assertEqual(http_actor.parse_headers(""), {})


class TestRunHttpRequestStep(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qf_http_")
        self._saved_dir = http_actor.HTTP_IMAGE_DIR
        http_actor.HTTP_IMAGE_DIR = self._tmp

    def tearDown(self):
        http_actor.HTTP_IMAGE_DIR = self._saved_dir

    def _params(self, srv: _Server, **over):
        p = {"url": srv.url, "method": "get", "result_type": "text",
             "use_proxy": False, "timeout": 3.0}
        p.update(over)
        return p

    def test_get_writes_all_vars(self):
        srv = _Server(_EchoHandler)
        self.addCleanup(srv.close)
        vars_ = {}
        p = self._params(srv, headers="Authorization: Bearer t",
                         user_agent="UA-X", cookie="k=v",
                         status_var="st", headers_var="hd",
                         cookie_var="ck", text_var="tx")
        ok, why = run_http_request_step(p, vars_)
        self.assertTrue(ok, why)
        self.assertEqual(vars_["st"], 201)
        self.assertEqual(vars_["hd"].get("X-Test"), "yes")
        self.assertEqual(vars_["ck"].get("sid"), "abc123")
        echo = json.loads(vars_["tx"])
        self.assertEqual(echo["ua"], "UA-X")
        self.assertEqual(echo["cookie"], "k=v")
        self.assertEqual(echo["auth"], "Bearer t")

    def test_post_sends_body(self):
        srv = _Server(_EchoHandler)
        self.addCleanup(srv.close)
        vars_ = {}
        p = self._params(srv, method="post", body="hello world", text_var="tx")
        ok, why = run_http_request_step(p, vars_)
        self.assertTrue(ok, why)
        self.assertEqual(json.loads(vars_["tx"])["body"], "hello world")

    def test_http_404_is_not_failure(self):
        srv = _Server(_NotFoundHandler)
        self.addCleanup(srv.close)
        vars_ = {}
        p = self._params(srv, status_var="st", text_var="tx")
        ok, why = run_http_request_step(p, vars_)
        self.assertTrue(ok, why)            # 4xx 不算失败，可按状态码分支
        self.assertEqual(vars_["st"], 404)
        self.assertEqual(vars_["tx"], "not found")

    def test_image_saves_file(self):
        srv = _Server(_ImageHandler)
        self.addCleanup(srv.close)
        vars_ = {}
        p = self._params(srv, result_type="image", text_var="tx")
        ok, why = run_http_request_step(p, vars_)
        self.assertTrue(ok, why)
        self.assertTrue(os.path.isfile(vars_["tx"]))
        self.assertTrue(vars_["tx"].startswith(self._tmp))

    def test_empty_url_fails(self):
        ok, why = run_http_request_step({"url": "", "use_proxy": False}, {})
        self.assertFalse(ok)
        self.assertIn("网址", why)

    def test_timeout_fails(self):
        srv = _Server(_SlowHandler)
        self.addCleanup(srv.close)
        vars_ = {}
        p = self._params(srv, timeout=0.2)
        ok, why = run_http_request_step(p, vars_)
        self.assertFalse(ok)
        self.assertIn("超时", why)

    def test_connection_refused_fails(self):
        # 先绑定一个端口，再关闭，得到一个「无监听」的地址
        probe = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        url = f"http://127.0.0.1:{probe.server_address[1]}"
        probe.server_close()
        ok, why = run_http_request_step(
            {"url": url, "use_proxy": False, "timeout": 2.0}, {})
        self.assertFalse(ok)
        # 平台差异下可能是「连接被拒」或「超时」，但一定是网络层失败、非空原因
        self.assertTrue(why)
        self.assertIn("请求", why)

    def test_unknown_method_fails(self):
        srv = _Server(_EchoHandler)
        self.addCleanup(srv.close)
        p = self._params(srv, method="delete")
        ok, why = run_http_request_step(p, {})
        self.assertFalse(ok)
        self.assertIn("方法", why)


if __name__ == "__main__":
    unittest.main()
