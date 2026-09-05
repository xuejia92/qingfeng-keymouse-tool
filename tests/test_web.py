"""web 步骤（打开关闭网页或浏览器）的测试。

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
        self.assertEqual(FLOW_STEP_TYPES.get("web"), "打开关闭网页或浏览器")

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
        self.assertIn("attach", web_actors.LAUNCH_MODES)     # 接管已打开的浏览器

    def test_default_params_have_attach_port(self):
        p = default_step_params("web")
        self.assertIn("attach_port", p)                      # 新增字段老配置平滑升级
        self.assertEqual(p["attach_port"], "")


class TestNameMigration(unittest.TestCase):
    def test_new_steps_use_new_name(self):
        """新建 web 步骤默认显示名 = 新名「打开关闭网页或浏览器」。"""
        s = FlowStep(type="web")
        self.assertEqual(s.name, "打开关闭网页或浏览器")

    def test_old_default_name_upgraded(self):
        """旧流程残留默认名「网页操作」自动纠正为新名。"""
        s = FlowStep(type="web", name="网页操作")
        self.assertEqual(s.name, "打开关闭网页或浏览器")

    def test_custom_name_kept(self):
        """用户自定义的名称不受改名影响。"""
        s = FlowStep(type="web", name="打开后台")
        self.assertEqual(s.name, "打开后台")


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

    def test_open_attach_shows_port(self):
        self.assertEqual(self._summary(action="open", url="https://a.com",
                                       launch_mode="attach", attach_port="9333",
                                       tab_target="new"),
                         "https://a.com · 接管端口9333 新标签")

    def test_open_attach_without_port_placeholder(self):
        self.assertIn("?", self._summary(action="open", url="https://a.com",
                                         launch_mode="attach"))

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


class _FakeAttachBrowser:
    """可断言的假浏览器：记录端口与 quit 调用，tabs_count/address 模拟 DrissionPage。"""

    def __init__(self, port=None):
        self.port = int(port) if port else 9222
        self.address = f"127.0.0.1:{self.port}"
        self.tabs_count = 2
        self.quit_called = 0

    def quit(self, *a, **k):
        self.quit_called += 1


class TestAttachMode(unittest.TestCase):
    """「接管已打开的浏览器」（等价 Chromium(端口)）：参数校验、会话复用/切换与关闭语义。

    刻意不启动真实浏览器：用打桩的 Chromium 类记录调用。
    """

    def setUp(self):
        web_actors.shutdown()

    def tearDown(self):
        web_actors.shutdown()

    def _stub_drission(self):
        """打桩 _import_drission：Chromium 构造等价于创建 _FakeAttachBrowser 并登记。"""
        registry = []

        def fake_import():
            class FakeChromium(_FakeAttachBrowser):
                def __init__(self, addr_or_opts=None, session_options=None):
                    super().__init__(addr_or_opts)
                    registry.append(self)

            return FakeChromium, None, ()

        original = web_actors._import_drission
        web_actors._import_drission = fake_import
        return registry, original

    # ---- 端口解析纯函数 ----
    def test_parse_attach_port_ok(self):
        self.assertEqual(web_actors._parse_attach_port("9333"), 9333)
        self.assertEqual(web_actors._parse_attach_port(9333), 9333)

    def test_parse_attach_port_rejects_invalid(self):
        for bad in ("", None, "abc", "0", "70000", "-5"):
            with self.assertRaises(ValueError):
                web_actors._parse_attach_port(bad)

    def test_address_matches_port(self):
        self.assertTrue(web_actors._address_matches_port("127.0.0.1:9333", 9333))
        self.assertFalse(web_actors._address_matches_port("127.0.0.1:9222", 9333))
        self.assertFalse(web_actors._address_matches_port("", 9333))

    # ---- open_url 前置校验：不发任何 DrissionPage ----
    def test_open_attach_without_port_fails_before_launch(self):
        ok, why = web_actors.open_url("https://a.com", mode="attach")
        self.assertFalse(ok)
        self.assertIn("端口", why)
        self.assertIsNone(web_actors._browser)

    def test_open_attach_with_invalid_port_fails_before_launch(self):
        for bad in ("abc", 0, 70000):
            ok, why = web_actors.open_url("https://a.com", mode="attach",
                                          attach_port=bad)
            self.assertFalse(ok, bad)
            self.assertIn("端口", why)

    # ---- attach 会话建立/复用/切换 ----
    def test_get_browser_attach_creates_with_port(self):
        registry, original = self._stub_drission()
        try:
            b = web_actors.get_browser("attach", "9333")
            self.assertEqual(len(registry), 1)
            self.assertIs(b, registry[0])
            self.assertEqual(registry[0].port, 9333)
            self.assertEqual(web_actors.active_mode(), "attach")
        finally:
            web_actors._import_drission = original
            web_actors._reset_browser()

    def test_get_browser_attach_reuses_same_port(self):
        registry, original = self._stub_drission()
        try:
            web_actors.get_browser("attach", "9333")
            self.assertEqual(len(registry), 1)
            # 同端口再取：沿用，不重复创建
            b2 = web_actors.get_browser("attach", "9333")
            self.assertEqual(len(registry), 1)
            self.assertIs(b2, registry[0])
        finally:
            web_actors._import_drission = original
            web_actors._reset_browser()

    def test_get_browser_attach_switches_port_keeps_window(self):
        """接管会话切到另一端口：旧 attach 浏览器不被 quit（窗口保留），连新端口。"""
        registry, original = self._stub_drission()
        try:
            web_actors.get_browser("attach", "9333")
            web_actors.get_browser("attach", "9444")
            self.assertEqual(len(registry), 2)
            self.assertEqual(registry[0].port, 9333)
            self.assertEqual(registry[1].port, 9444)
            self.assertEqual(registry[0].quit_called, 0)     # 手动开的浏览器不能被 quit
            self.assertIs(web_actors._browser, registry[1])
        finally:
            web_actors._import_drission = original
            web_actors._reset_browser()

    # ---- 关闭语义：attach 只断开，自启才 quit ----
    def test_close_browser_attach_keeps_window(self):
        b = _FakeAttachBrowser(9333)
        web_actors._browser = b
        web_actors._mode = "attach"
        try:
            self.assertTrue(web_actors.close_browser())
            self.assertEqual(b.quit_called, 0)               # 不关用户浏览器
        finally:
            web_actors._reset_browser()
        self.assertIsNone(web_actors._browser)

    def test_close_browser_self_launched_quits(self):
        b = _FakeAttachBrowser(9222)
        web_actors._browser = b
        web_actors._mode = "front"
        try:
            self.assertTrue(web_actors.close_browser())
            self.assertEqual(b.quit_called, 1)               # 自启浏览器正常退出
        finally:
            web_actors._reset_browser()
        self.assertIsNone(web_actors._browser)

    # ---- 关掉最后一个标签的收尾文案：attach 只断开，自启才退出 ----
    def _one_tab_browser(self, port=9333):
        """只有 1 个标签的假浏览器：latest_tab.close() 把 tabs_count 归零。"""

        class Tab:
            tab_id = "t1"

            def __init__(self, owner):
                self._owner = owner

            def close(self):
                self._owner.tabs_count = 0

        class OneTabBrowser(_FakeAttachBrowser):
            def __init__(self, port=None):
                super().__init__(port)
                self.tabs_count = 1

            @property
            def latest_tab(self):
                return Tab(self)

        return OneTabBrowser(port)

    def test_close_tab_last_on_attach_keeps_window(self):
        """attach 会话关掉最后一个标签：只断开接管，窗口保留、不 quit。"""
        b = self._one_tab_browser(9333)
        web_actors._browser = b
        web_actors._mode = "attach"
        try:
            ok, why = web_actors.close_tab("current")
            self.assertTrue(ok)
            self.assertIn("断开接管", why)
            self.assertIn("窗口保留", why)
            self.assertEqual(b.quit_called, 0)               # 不关用户手动开的浏览器
        finally:
            web_actors._reset_browser()
        self.assertIsNone(web_actors._browser)

    def test_close_tab_last_on_self_launched_exits(self):
        """自启浏览器关掉最后一个标签：真正退出，文案说明浏览器已退出。"""
        b = self._one_tab_browser(9222)
        web_actors._browser = b
        web_actors._mode = "front"
        try:
            ok, why = web_actors.close_tab("current")
            self.assertTrue(ok)
            self.assertIn("浏览器已退出", why)
            self.assertEqual(b.quit_called, 1)
        finally:
            web_actors._reset_browser()
        self.assertIsNone(web_actors._browser)

    def test_run_close_browser_attach_wording(self):
        """关闭接管会话时提示「窗口保留」，不再误导为浏览器已退出。"""
        web_actors._browser = _FakeAttachBrowser(9333)
        web_actors._mode = "attach"
        try:
            ok, why = run_web_step({"action": "close_browser"})
            self.assertTrue(ok)
            self.assertIn("接管", why)
            self.assertIn("保留", why)
        finally:
            web_actors._reset_browser()

    def test_run_close_browser_self_launched_wording(self):
        web_actors._browser = _FakeAttachBrowser(9222)
        web_actors._mode = "front"
        try:
            ok, why = run_web_step({"action": "close_browser"})
            self.assertTrue(ok)
            self.assertIn("已关闭", why)
        finally:
            web_actors._reset_browser()


if __name__ == "__main__":
    unittest.main()
