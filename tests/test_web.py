"""网页操作（DrissionPage）步骤的测试。

刻意**不启动真实浏览器**：那会让测试变慢、抢焦点，还依赖本机装了 Chrome。
这里只覆盖不需要浏览器的部分——参数默认值、摘要显示、序列化往返、
以及未启动浏览器时的幂等/报错行为。真实浏览器操作靠手工冒烟（见 README）。
"""
from __future__ import annotations

import unittest

from app import web_actors
from app.config import (FLOW_STEP_TYPES, WEB_ACTIONS, Flow, FlowStep,
                        default_step_params, flow_from_dict, flow_to_dict)
from app.tasks import run_web_step


class TestStepMetadata(unittest.TestCase):
    def test_web_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("web"), "网页操作")

    def test_actions_complete(self):
        self.assertEqual(set(WEB_ACTIONS), {"open", "close_tab", "close_browser"})

    def test_default_params(self):
        p = default_step_params("web")
        self.assertEqual(p["action"], "open")
        self.assertEqual(p["launch_mode"], "front")     # 需求：默认前台
        self.assertEqual(p["tab_target"], "reuse")
        self.assertEqual(p["url"], "")
        self.assertEqual(p["tab_scope"], "current")
        self.assertEqual(p["match_text"], "")

    def test_default_mode_is_a_known_mode(self):
        """默认值必须是 web_actors 认得的模式，否则运行时会静默走错分支。"""
        self.assertIn(default_step_params("web")["launch_mode"], web_actors.LAUNCH_MODES)

    def test_launch_modes_offer_background(self):
        self.assertIn("headless", web_actors.LAUNCH_MODES)
        self.assertIn("background", web_actors.LAUNCH_MODES)


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="web")
        s.params.update(params)
        return s.summary()

    def test_open(self):
        self.assertEqual(self._summary(action="open", url="https://a.com",
                                       launch_mode="front", tab_target="reuse"),
                         "https://a.com · 前台当前标签")

    def test_open_headless(self):
        self.assertEqual(self._summary(action="open", url="https://a.com",
                                       launch_mode="headless", tab_target="reuse"),
                         "https://a.com · 无头当前标签")

    def test_open_new_tab(self):
        self.assertEqual(self._summary(action="open", url="https://a.com",
                                       launch_mode="front", tab_target="new"),
                         "https://a.com · 前台新标签")

    def test_open_without_url(self):
        self.assertIn("未填网址", self._summary(action="open", url=""))

    def test_long_url_truncated(self):
        s = self._summary(action="open", url="https://" + "x" * 100)
        self.assertIn("…", s)
        self.assertLess(len(s), 60)

    def test_close_browser(self):
        self.assertEqual(self._summary(action="close_browser"), "关闭浏览器")

    def test_close_tab_current(self):
        self.assertEqual(self._summary(action="close_tab", tab_scope="current"),
                         "关闭当前标签")

    def test_close_tab_others(self):
        self.assertEqual(self._summary(action="close_tab", tab_scope="others"),
                         "关闭其他标签")

    def test_close_tab_match(self):
        self.assertEqual(self._summary(action="close_tab", tab_scope="match",
                                       match_text="后台"),
                         "关闭匹配「后台」的标签")

    def test_unknown_action_falls_back(self):
        """动作值损坏时返回空串，不能抛异常——否则整个流程列表都刷不出来。"""
        self.assertEqual(self._summary(action="nope"), "")


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="网页流程", steps=[FlowStep(type="web", name="打开后台", params={
            "action": "open", "url": "https://a.com",
            "launch_mode": "background", "tab_target": "new",
        })])
        data = flow_to_dict(f)
        back = flow_from_dict(data)
        self.assertIsNotNone(back)
        step = back.steps[0]
        self.assertEqual(step.params["url"], "https://a.com")
        self.assertEqual(step.params["launch_mode"], "background")

    def test_unknown_keys_preserved(self):
        """未知参数不该被丢掉：以后加字段时老流程能平滑升级。"""
        s = FlowStep(type="web", params={"action": "open", "future_field": 1})
        self.assertIn("future_field", s.params)


class TestRuntimeWithoutBrowser(unittest.TestCase):
    """未启动浏览器时的行为：必须幂等且报错清晰，不能抛异常。"""

    def setUp(self):
        web_actors.shutdown()          # 确保测试起点没有浏览器

    def tearDown(self):
        web_actors.shutdown()

    def test_close_browser_is_idempotent(self):
        self.assertFalse(web_actors.close_browser())
        self.assertFalse(web_actors.close_browser())

    def test_close_tab_without_browser(self):
        ok, why = web_actors.close_tab("current")
        self.assertTrue(ok)
        self.assertIn("未启动", why)

    def test_open_empty_url_rejected(self):
        ok, why = web_actors.open_url("")
        self.assertFalse(ok)
        self.assertIn("网址", why)

    def test_open_whitespace_url_rejected(self):
        ok, _ = web_actors.open_url("   ")
        self.assertFalse(ok)

    def test_active_mode_empty_before_launch(self):
        self.assertEqual(web_actors.active_mode(), "")

    def test_is_available_reports_reason(self):
        ok, why = web_actors.is_available()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertTrue(why)       # 不可用时必须给出原因


class TestZombieBrowser(unittest.TestCase):
    """浏览器被手动关闭/崩溃后留下的僵尸实例：必须能识别并自动重建，
    否则重复执行「打开网址」会拿断连的旧实例操作而报错（本次修复的核心场景）。"""

    class _DeadBrowser:
        """模拟已断连的 DrissionPage 实例：任何 CDP 访问都抛异常。"""

        @property
        def tabs_count(self):
            raise RuntimeError("PageDisconnectedError: 浏览器已关闭")

    def setUp(self):
        web_actors.shutdown()

    def tearDown(self):
        web_actors.shutdown()

    def test_alive_false_when_none(self):
        self.assertFalse(web_actors._browser_alive())

    def test_alive_true_for_fake_browser(self):
        """活实例（tabs_count 可访问）必须判为存活。"""

        class Fake:
            tabs_count = 1
        web_actors._browser = Fake()
        web_actors._mode = "front"
        try:
            self.assertTrue(web_actors._browser_alive())
        finally:
            web_actors._reset_browser()

    def test_alive_false_for_zombie(self):
        web_actors._browser = self._DeadBrowser()
        web_actors._mode = "front"
        try:
            self.assertFalse(web_actors._browser_alive())
        finally:
            web_actors._reset_browser()

    def test_get_browser_rebuilds_after_zombie(self):
        """僵尸实例存在时 get_browser 必须重建，而不是返回死实例。"""
        created = []

        class FakeChromium:
            def __init__(self, options):
                created.append(self)

        class FakeOptions:
            def headless(self, v):
                pass

            def set_argument(self, *a):
                pass

        original = web_actors._import_drission

        def fake_import():
            return FakeChromium, FakeOptions, ()

        web_actors._import_drission = fake_import
        web_actors._browser = self._DeadBrowser()
        web_actors._mode = "front"
        try:
            b = web_actors.get_browser("headless")
            self.assertEqual(len(created), 1)
            self.assertIs(b, created[0])
            self.assertEqual(web_actors.active_mode(), "headless")
        finally:
            web_actors._import_drission = original
            web_actors._reset_browser()

    def test_close_tab_with_zombie_is_idempotent(self):
        """对僵尸实例执行关闭标签：幂等成功，并清掉僵尸单例。"""
        web_actors._browser = self._DeadBrowser()
        web_actors._mode = "front"
        try:
            ok, why = web_actors.close_tab("current")
            self.assertTrue(ok)
            self.assertIn("未启动", why)
            self.assertIsNone(web_actors._browser)   # 僵尸单例被清掉
        finally:
            web_actors._reset_browser()

    def test_close_browser_with_zombie_returns_false(self):
        """僵尸实例没有活动浏览器可关：返回 False（显示「未启动」而非「已关闭」）。"""
        web_actors._browser = self._DeadBrowser()
        web_actors._mode = "front"
        try:
            self.assertFalse(web_actors.close_browser())
            self.assertIsNone(web_actors._browser)
        finally:
            web_actors._reset_browser()

    def test_open_url_retries_after_disconnect(self):
        """打开时连接断开（浏览器被手动关掉）→ 自动重启浏览器重试一次。"""
        from DrissionPage.errors import PageDisconnectedError
        calls = {"new": 0}

        class FakeTab:
            title = "测试页"

        class FakeBrowser:
            def new_tab(self, url):
                calls["new"] += 1
                if calls["new"] == 1:
                    raise PageDisconnectedError("连接已断开")
                return FakeTab()

        original_get = web_actors.get_browser
        web_actors.get_browser = lambda mode="front": FakeBrowser()
        try:
            ok, why = web_actors.open_url("https://a.com", new_tab=True)
        finally:
            web_actors.get_browser = original_get
            web_actors._reset_browser()
        self.assertTrue(ok, why)
        self.assertEqual(calls["new"], 2)          # 第一次失败，第二次成功


class TestLaunchOptions(unittest.TestCase):
    """启动参数：front 前台显示默认最大化；headless/background 不最大化。"""

    @staticmethod
    def _capture_options(mode: str):
        """用假 ChromiumOptions 抓取 _build_options 设置的参数，返回 dict。"""
        captured = {"headless": None, "args": []}

        class FakeChromiumOptions:
            def headless(self, v):
                captured["headless"] = v

            def set_argument(self, arg):
                captured["args"].append(arg)

        original = web_actors._import_drission
        web_actors._import_drission = lambda: (None, FakeChromiumOptions, ())
        try:
            web_actors._build_options(mode)
        finally:
            web_actors._import_drission = original
        return captured

    def test_front_is_maximized(self):
        cap = self._capture_options("front")
        self.assertIn("--start-maximized", cap["args"])
        self.assertNotIn("--window-position=-32000,-32000", cap["args"])

    def test_headless_not_maximized(self):
        cap = self._capture_options("headless")
        self.assertTrue(cap["headless"])
        self.assertNotIn("--start-maximized", cap["args"])

    def test_background_not_maximized(self):
        cap = self._capture_options("background")
        self.assertIn("--window-position=-32000,-32000", cap["args"])
        self.assertNotIn("--start-maximized", cap["args"])


class TestRunWebStep(unittest.TestCase):
    def setUp(self):
        web_actors.shutdown()

    def tearDown(self):
        web_actors.shutdown()

    def test_unknown_action(self):
        ok, why = run_web_step({"action": "teleport"})
        self.assertFalse(ok)
        self.assertIn("未知", why)

    def test_close_browser_without_instance(self):
        ok, why = run_web_step({"action": "close_browser"})
        self.assertTrue(ok)
        self.assertIn("未启动", why)

    def test_empty_url_fails_cleanly(self):
        ok, why = run_web_step({"action": "open", "url": ""})
        self.assertFalse(ok)
        self.assertIn("网址", why)

    def test_close_tab_without_instance(self):
        ok, _ = run_web_step({"action": "close_tab", "tab_scope": "current"})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
