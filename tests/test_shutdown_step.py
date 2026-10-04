# -*- coding: utf-8 -*-
"""「定时关机」（shutdown）步骤的测试。

这个模块的特殊之处：**它是真会关机的**。所以整个测试文件遵守一条铁律——
任何用例都不得让真实关机命令发出去：

· 执行逻辑层：一律 patch `tasks._run_power_command`（单点封装，不碰全局
  subprocess），只断言「该发什么命令」；`_run_power_command` 自身用假的
  `_power_run` 覆盖成功 / 非零返回码 / 命令缺失三条分支。
· 等待时间：靠 `_wait_for_seconds` 注入 + 把 `SHUTDOWN_WAIT_TICK_SEC` /
  `SHUTDOWN_WARN_TICK_SEC` 调到 0.01，把倒计时压到 0.1~0.3 秒。
  绝不 patch 全局 time.sleep —— 那会让其它仍在跑的后台线程睡了个寂寞。

覆盖：类型注册 / 默认参数 / 摘要 / 对话框行显隐与校验往返 / 三种触发方式
（指定时间点、倒计时、条件触发）的判定与失败分支 / 停止取消语义 / 电源命令构建 /
提醒倒计时期间的屏幕红色倒计时浮层（浮层本体另见 test_power_overlay.py）/
提醒期「按 Esc 键取消」与取消闸门（监听本体另见 test_cancel_key.py）。
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.cancel_key as cancel_key_mod
import app.config as config_mod
import app.flows as flows_mod
import app.ocr as ocr_mod
import app.tasks as tasks_mod
from app.config import (FLOW_STEP_TYPES, Flow, FlowStep, POWER_ACTIONS,
                        SHUTDOWN_TRIGGER_MODES, default_step_params,
                        format_duration, parse_at_time,
                        shutdown_countdown_seconds)
from tests._env import TempConfigPaths


def _params(**overrides) -> dict:
    """一份「写完就 dry_run + 提醒 0 秒」的安全配置：默认不会碰真机关机。"""
    p = dict(default_step_params("shutdown"))
    p.update({"trigger_mode": "countdown", "count_hours": 0, "count_minutes": 0,
              "count_seconds": 0.1, "warn_sec": 0, "dry_run": True})
    p.update(overrides)
    return p


class _ShutdownTestBase(unittest.TestCase):
    """统一把倒计时轮询压短 + 隔离 config/flows/log 到临时目录。

    并且**统一把 Esc 监听换成替身**：真监听是进程级键盘钩子，只要有真实 Esc 按键
    落在提醒倒计时那零点几秒里，倒计时就会被判成「用户取消」，于是
    `test_warn_window_waits_before_firing`（断言命令发出去了）和
    `test_shows_with_action_label_and_font_size`（断言 ok）会**随机失败**。
    实测确认过机制：另起一个进程注入 Esc，`_warn_before_power` 当场返回
    「已取消重启（按下 Esc 键）」（2026-10-04 全量套件里中过一次，单跑怎么都过）。
    Esc 取消本身由 TestEscCancel 用替身确定性地覆盖，这里换成替身不丢覆盖。
    """

    def setUp(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        for name, value in (("SHUTDOWN_WAIT_TICK_SEC", 0.01),
                            ("SHUTDOWN_WARN_TICK_SEC", 0.01)):
            patcher = mock.patch.object(tasks_mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # _FakeEscListener 定义在文件后面（运行时才解析，没问题）；
        # _EscTestBase 会再打一层自己的补丁，后起的生效、LIFO 回滚，互不干扰。
        patcher = mock.patch.object(cancel_key_mod, "EscListener",
                                    lambda on_esc: _FakeEscListener(on_esc))
        patcher.start()
        self.addCleanup(patcher.stop)


class _FakeDatetime:
    """只替换 `now()` 的假 datetime；replace / 加减仍走真实 datetime。"""

    fixed: datetime = datetime(2026, 9, 18, 12, 0, 0)

    @classmethod
    def now(cls):
        return cls.fixed


# ---------------- 元信息：注册 / 默认参数 / 摘要 ----------------


class TestRegistration(_ShutdownTestBase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("shutdown"), "定时关机")

    def test_in_app_web_group(self):
        from app.ui.flow_tab import MODULE_GROUPS
        groups = {gid: types for gid, _, types in MODULE_GROUPS}
        self.assertIn("shutdown", groups["app_web"])

    def test_has_icon(self):
        from app.ui.flow_dialog import _TYPE_ICONS
        self.assertTrue(_TYPE_ICONS.get("shutdown"))

    def test_option_tables(self):
        self.assertEqual(list(SHUTDOWN_TRIGGER_MODES), ["at_time", "countdown", "condition"])
        self.assertEqual(list(POWER_ACTIONS), ["shutdown", "restart", "sleep", "lock"])

    def test_default_params_cover_three_modes(self):
        p = default_step_params("shutdown")
        # 触发方式与各自的配置项都在
        self.assertIn(p["trigger_mode"], SHUTDOWN_TRIGGER_MODES)
        for key in ("at_time", "at_time_if_passed", "count_hours", "count_minutes",
                    "count_seconds", "cond_mode", "image", "image_path", "confidence",
                    "text", "tolerance", "region", "interval_sec", "cond_timeout_sec",
                    "hold_sec", "power_action", "warn_sec", "force_close_apps", "dry_run"):
            self.assertIn(key, p)
        # 默认值：不动手就不会真关机（有提醒窗口）；且默认不演练
        self.assertEqual(p["power_action"], "shutdown")
        self.assertGreater(p["warn_sec"], 0)
        self.assertFalse(p["dry_run"])
        self.assertTrue(p["force_close_apps"])

    def test_step_constructs_with_defaults(self):
        s = FlowStep(type="shutdown")
        self.assertEqual(s.name, "定时关机")
        self.assertEqual(s.params["trigger_mode"], "countdown")


class TestAtTimeParsing(_ShutdownTestBase):
    def test_valid(self):
        self.assertEqual(parse_at_time("23:30"), (23, 30))
        self.assertEqual(parse_at_time("7:05"), (7, 5))
        self.assertEqual(parse_at_time("2359"), (23, 59))
        self.assertEqual(parse_at_time(" 00:00 "), (0, 0))

    def test_invalid(self):
        for bad in ("", "   ", "24:00", "23:60", "23", "12:34:56", "abc", "-1:00", None):
            self.assertIsNone(parse_at_time(bad), bad)


class TestDurationHelpers(_ShutdownTestBase):
    def test_countdown_seconds(self):
        self.assertEqual(shutdown_countdown_seconds(
            {"count_hours": 1, "count_minutes": 30, "count_seconds": 5}), 5405)
        self.assertEqual(shutdown_countdown_seconds({}), 0)
        # 非法值按 0 计、负数归零，不抛异常
        self.assertEqual(shutdown_countdown_seconds(
            {"count_hours": "x", "count_minutes": -5, "count_seconds": 2.5}), 2.5)

    def test_format_duration(self):
        self.assertEqual(format_duration(5405), "1 小时 30 分 5 秒")
        self.assertEqual(format_duration(60), "1 分")
        self.assertEqual(format_duration(0), "0 秒")
        self.assertEqual(format_duration("bad"), "0 秒")


class TestSummary(_ShutdownTestBase):
    def _summary(self, **params):
        s = FlowStep(type="shutdown")
        s.params.update(params)
        return s.summary()

    def test_countdown(self):
        txt = self._summary(trigger_mode="countdown", count_minutes=30,
                            count_hours=0, count_seconds=0)
        self.assertEqual(txt, "倒计时 30 分 → 关机")

    def test_countdown_unset(self):
        txt = self._summary(trigger_mode="countdown", count_hours=0,
                            count_minutes=0, count_seconds=0)
        self.assertIn("未设置", txt)

    def test_at_time(self):
        txt = self._summary(trigger_mode="at_time", at_time="23:30")
        self.assertIn("23:30", txt)
        self.assertIn("顺延次日", txt)

    def test_at_time_fail_policy(self):
        txt = self._summary(trigger_mode="at_time", at_time="23:30",
                            at_time_if_passed="fail")
        self.assertIn("判失败", txt)

    def test_condition_text_and_image(self):
        txt = self._summary(trigger_mode="condition", cond_mode="text", text="下载完成")
        self.assertEqual(txt, "出现文字「下载完成」→ 关机")
        txt2 = self._summary(trigger_mode="condition", cond_mode="image",
                             image="tpl_1.png")
        self.assertEqual(txt2, "出现图片 tpl_1.png → 关机")

    def test_long_text_truncated(self):
        txt = self._summary(trigger_mode="condition", cond_mode="text", text="字" * 40)
        self.assertLess(len(txt), 30)

    def test_action_and_dry_run_marked(self):
        txt = self._summary(trigger_mode="countdown", count_minutes=1,
                            power_action="restart")
        self.assertIn("重启", txt)
        txt2 = self._summary(trigger_mode="countdown", count_minutes=1, dry_run=True)
        self.assertTrue(txt2.startswith("[演练]"))


# ---------------- 电源命令构建与执行 ----------------


class TestPowerCommand(_ShutdownTestBase):
    def test_shutdown_force(self):
        self.assertEqual(tasks_mod.power_command("shutdown", True, 0),
                         ["shutdown", "/s", "/t", "0", "/f"])

    def test_shutdown_without_force_and_delay(self):
        self.assertEqual(tasks_mod.power_command("shutdown", False, 60),
                         ["shutdown", "/s", "/t", "60"])

    def test_restart(self):
        self.assertEqual(tasks_mod.power_command("restart", True, 0),
                         ["shutdown", "/r", "/t", "0", "/f"])

    def test_sleep_and_lock_ignore_force(self):
        self.assertEqual(tasks_mod.power_command("sleep", True, 30),
                         ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        self.assertEqual(tasks_mod.power_command("lock", False, 30),
                         ["rundll32.exe", "user32.dll,LockWorkStation"])

    def test_unknown_action_returns_none(self):
        self.assertIsNone(tasks_mod.power_command("explode", True, 0))

    def test_negative_delay_clamped(self):
        self.assertEqual(tasks_mod.power_command("shutdown", False, -5),
                         ["shutdown", "/s", "/t", "0"])


class TestRunPowerCommand(_ShutdownTestBase):
    """`_run_power_command` 自身：只 patch `_power_run`，绝不真跑 shutdown。"""

    def _proc(self, code: int = 0, out: str = "", err: str = ""):
        return mock.Mock(returncode=code, stdout=out, stderr=err)

    def test_resolves_absolute_shutdown_exe(self):
        """argv[0] 必须换成 System32\\shutdown.exe 绝对路径，不靠 PATH 搜索。"""
        with mock.patch.object(tasks_mod, "_power_run",
                               return_value=self._proc()) as run:
            ok, _ = tasks_mod._run_power_command(["shutdown", "/s", "/t", "0", "/f"])
        self.assertTrue(ok)
        argv = run.call_args[0][0]
        self.assertTrue(argv[0].lower().endswith("shutdown.exe"), argv[0])
        self.assertIn("system32", argv[0].lower())
        self.assertEqual(argv[1:], ["/s", "/t", "0", "/f"])

    def test_non_shutdown_argv_untouched(self):
        with mock.patch.object(tasks_mod, "_power_run",
                               return_value=self._proc()) as run:
            tasks_mod._run_power_command(["rundll32.exe", "user32.dll,LockWorkStation"])
        self.assertEqual(run.call_args[0][0],
                         ["rundll32.exe", "user32.dll,LockWorkStation"])

    def test_nonzero_returncode_is_failure_with_message(self):
        with mock.patch.object(tasks_mod, "_power_run",
                               return_value=self._proc(5, err="拒绝访问。(5)")):
            ok, why = tasks_mod._run_power_command(["shutdown", "/s"])
        self.assertFalse(ok)
        self.assertIn("5", why)
        self.assertIn("拒绝访问", why)

    def test_missing_binary(self):
        with mock.patch.object(tasks_mod, "_power_run",
                               side_effect=FileNotFoundError):
            ok, why = tasks_mod._run_power_command(["shutdown", "/s"])
        self.assertFalse(ok)
        self.assertIn("找不到命令", why)

    def test_timeout(self):
        with mock.patch.object(tasks_mod, "_power_run",
                               side_effect=tasks_mod.subprocess.TimeoutExpired("x", 1)):
            ok, why = tasks_mod._run_power_command(["shutdown", "/s"])
        self.assertFalse(ok)
        self.assertIn("超时", why)


# ---------------- 触发方式 ② 倒计时 ----------------


class TestCountdownTrigger(_ShutdownTestBase):
    def test_zero_duration_fails_fast(self):
        ok, why = tasks_mod.run_shutdown_step(
            _params(count_hours=0, count_minutes=0, count_seconds=0), {})
        self.assertFalse(ok)
        self.assertIn("必须大于 0", why)

    def test_waits_then_fires(self):
        """倒计时 0.15 秒：等到之后才走到执行；dry_run 下只记日志。"""
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            t0 = time.monotonic()
            ok, why = tasks_mod.run_shutdown_step(
                _params(count_seconds=0.15), {})
            elapsed = time.monotonic() - t0
        self.assertTrue(ok, why)
        self.assertIn("演练", why)
        self.assertGreaterEqual(elapsed, 0.1)
        run.assert_not_called()          # 演练模式：命令一次都不许发

    def test_real_run_builds_expected_command(self):
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(dry_run=False, warn_sec=0, force_close_apps=True), {})
        self.assertTrue(ok, why)
        run.assert_called_once_with(["shutdown", "/s", "/t", "0", "/f"])

    def test_force_off_drops_f_flag(self):
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            tasks_mod.run_shutdown_step(
                _params(dry_run=False, warn_sec=0, force_close_apps=False), {})
        run.assert_called_once_with(["shutdown", "/s", "/t", "0"])

    def test_command_failure_reported(self):
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(False, "返回码 5：拒绝访问")):
            ok, why = tasks_mod.run_shutdown_step(
                _params(dry_run=False, warn_sec=0), {})
        self.assertFalse(ok)
        self.assertIn("关机失败", why)

    def test_unknown_power_action_rejected(self):
        ok, why = tasks_mod.run_shutdown_step(_params(power_action="explode"), {})
        self.assertFalse(ok)
        self.assertIn("未知的电源动作", why)

    def test_unknown_trigger_mode_rejected(self):
        ok, why = tasks_mod.run_shutdown_step(_params(trigger_mode="telepathy"), {})
        self.assertFalse(ok)
        self.assertIn("未知的触发方式", why)


# ---------------- 触发方式 ① 指定时间点 ----------------


class TestAtTimeTrigger(_ShutdownTestBase):
    def _run(self, params, **patch):
        with mock.patch.object(tasks_mod, "datetime", _FakeDatetime):
            return tasks_mod.run_shutdown_step(params, {}, patch.get("stop"))

    def test_invalid_format_fails(self):
        for bad in ("", "25:00", "abc"):
            ok, why = tasks_mod.run_shutdown_step(
                _params(trigger_mode="at_time", at_time=bad), {})
            self.assertFalse(ok, bad)
            self.assertIn("格式无效", why)

    def test_past_and_fail_policy(self):
        _FakeDatetime.fixed = datetime(2026, 9, 18, 12, 0, 0)
        ok, why = self._run(_params(trigger_mode="at_time", at_time="08:00",
                                    at_time_if_passed="fail"))
        self.assertFalse(ok)
        self.assertIn("已过", why)

    def test_past_rolls_to_next_day(self):
        """已过 + 顺延：等待秒数应约等于一整天（只验秒数，不真的等）。"""
        _FakeDatetime.fixed = datetime(2026, 9, 18, 12, 0, 0)
        seen = {}

        def fake_wait(sec, stop_):
            seen["sec"] = sec
            return ""

        with mock.patch.object(tasks_mod, "_wait_for_seconds", fake_wait), \
                mock.patch.object(tasks_mod, "_run_power_command",
                                  return_value=(True, "已下发")) as run:
            ok, why = self._run(_params(trigger_mode="at_time", at_time="11:00",
                                        dry_run=False, warn_sec=0))
        self.assertTrue(ok, why)
        self.assertAlmostEqual(seen["sec"], 24 * 3600 - 3600, delta=2)
        run.assert_called_once()          # 时间点模式同样走真实执行

    def test_future_waits_then_fires(self):
        """未过：等待秒数 = 距该时刻的秒数（只验等待时长，不真等）。"""
        _FakeDatetime.fixed = datetime(2026, 9, 18, 12, 0, 30)
        seen = {}

        def fake_wait(sec, stop_):
            seen["sec"] = sec
            return ""

        with mock.patch.object(tasks_mod, "_wait_for_seconds", fake_wait):
            ok, why = self._run(_params(trigger_mode="at_time", at_time="12:01"))
        self.assertTrue(ok, why)
        self.assertAlmostEqual(seen["sec"], 30, delta=2)

    def test_reports_target_time_in_result(self):
        _FakeDatetime.fixed = datetime(2026, 9, 18, 12, 0, 0)
        with mock.patch.object(tasks_mod, "_wait_for_seconds", lambda s, e: ""):
            ok, why = self._run(_params(trigger_mode="at_time", at_time="13:45"))
        self.assertTrue(ok, why)
        self.assertIn("13:45", why)


# ---------------- 触发方式 ③ 条件触发 ----------------


class TestConditionTextTrigger(_ShutdownTestBase):
    def _p(self, **kw):
        base = {"trigger_mode": "condition", "cond_mode": "text", "text": "下载完成",
                "interval_sec": 0.05, "cond_timeout_sec": 0.0, "hold_sec": 0.0}
        base.update(kw)
        return _params(**base)

    def test_empty_text_fails_fast(self):
        ok, why = tasks_mod.run_shutdown_step(self._p(text="   "), {})
        self.assertFalse(ok)
        self.assertIn("目标文字为空", why)

    def test_hit_fires(self):
        hit = {"x": 100, "y": 200, "text": "下载完成", "score": 0.9}
        with mock.patch.object(ocr_mod, "find_text", return_value=(True, hit, "")), \
                mock.patch.object(tasks_mod, "_run_power_command"):
            ok, why = tasks_mod.run_shutdown_step(self._p(), {})
        self.assertTrue(ok, why)
        self.assertIn("下载完成", why)

    def test_variable_reference_resolved(self):
        """目标文字支持 $变量名：运行时取变量里的文字去匹配。"""
        seen = {}

        def fake_find_text(region="", text="", lang="ch", tolerance=None):
            seen["text"] = text
            return True, {"x": 1, "y": 2, "text": text, "score": 0.9}, ""

        with mock.patch.object(ocr_mod, "find_text", side_effect=fake_find_text), \
                mock.patch.object(tasks_mod, "_run_power_command"):
            tasks_mod.run_shutdown_step(self._p(text="$keyword"), {"keyword": "安装完成"})
        self.assertEqual(seen["text"], "安装完成")

    def test_timeout_when_never_appears(self):
        with mock.patch.object(ocr_mod, "find_text", return_value=(True, None, "")), \
                mock.patch.object(tasks_mod, "_run_power_command") as run:
            ok, why = tasks_mod.run_shutdown_step(
                self._p(cond_timeout_sec=0.2, interval_sec=0.05), {})
        self.assertFalse(ok)
        self.assertIn("超时", why)
        run.assert_not_called()

    def test_ocr_failure_fails_not_hangs(self):
        """OCR 用不了必须判失败：否则会被当成「条件一直没满足」永久挂着。"""
        with mock.patch.object(ocr_mod, "find_text",
                               return_value=(False, None, "OCR 不可用")):
            ok, why = tasks_mod.run_shutdown_step(self._p(), {})
        self.assertFalse(ok)
        self.assertIn("OCR 不可用", why)

    def test_hold_requires_continuous_match(self):
        """条件须连续保持：命中后立刻消失不应触发，稳定命中才触发。"""
        with mock.patch.object(ocr_mod, "find_text", return_value=(True, None, "")), \
                mock.patch.object(tasks_mod, "_run_power_command") as run:
            ok, why = tasks_mod.run_shutdown_step(
                self._p(hold_sec=5, cond_timeout_sec=0.2), {})
        self.assertFalse(ok)
        run.assert_not_called()

        hit = {"x": 1, "y": 2, "text": "下载完成", "score": 0.9}
        with mock.patch.object(ocr_mod, "find_text", return_value=(True, hit, "")), \
                mock.patch.object(tasks_mod, "_run_power_command"):
            ok2, why2 = tasks_mod.run_shutdown_step(self._p(hold_sec=0.1), {})
        self.assertTrue(ok2, why2)
        self.assertIn("连续保持", why2)


class TestConditionImageTrigger(_ShutdownTestBase):
    def _p(self, **kw):
        base = {"trigger_mode": "condition", "cond_mode": "image",
                "image": "tpl_x.png", "interval_sec": 0.05}
        base.update(kw)
        return _params(**base)

    def test_template_load_failure(self):
        with mock.patch.object(tasks_mod.finder, "load_template", return_value=None):
            ok, why = tasks_mod.run_shutdown_step(self._p(), {})
        self.assertFalse(ok)
        self.assertIn("模板图加载失败", why)

    def test_hit_fires(self):
        import numpy as np
        tpl = np.zeros((4, 4, 3), dtype="uint8")
        with mock.patch.object(tasks_mod.finder, "load_template", return_value=tpl), \
                mock.patch.object(tasks_mod.finder, "grab_full_screen",
                                  return_value=np.zeros((10, 10, 3), dtype="uint8")), \
                mock.patch.object(tasks_mod.finder, "locate",
                                  return_value=(30, 40, 0.97)), \
                mock.patch.object(tasks_mod, "_run_power_command"):
            ok, why = tasks_mod.run_shutdown_step(self._p(), {})
        self.assertTrue(ok, why)
        self.assertIn("30,40", why)

    def test_region_uses_locate_in_region(self):
        import numpy as np
        tpl = np.zeros((4, 4, 3), dtype="uint8")
        screen = np.zeros((100, 100, 3), dtype="uint8")
        with mock.patch.object(tasks_mod.finder, "load_template", return_value=tpl), \
                mock.patch.object(tasks_mod.finder, "grab_full_screen",
                                  return_value=screen), \
                mock.patch.object(tasks_mod.finder, "locate_in_region",
                                  return_value=(11, 22, 0.9)) as in_region, \
                mock.patch.object(tasks_mod.finder, "locate") as locate, \
                mock.patch.object(tasks_mod, "_run_power_command"):
            ok, _ = tasks_mod.run_shutdown_step(self._p(region="10,20,50,50"), {})
        self.assertTrue(ok)
        self.assertTrue(in_region.called)
        locate.assert_not_called()

    def test_grab_exception_fails(self):
        import numpy as np
        tpl = np.zeros((4, 4, 3), dtype="uint8")
        with mock.patch.object(tasks_mod.finder, "load_template", return_value=tpl), \
                mock.patch.object(tasks_mod.finder, "grab_full_screen",
                                  side_effect=RuntimeError("抓屏失败")):
            ok, why = tasks_mod.run_shutdown_step(self._p(), {})
        self.assertFalse(ok)
        self.assertIn("找图失败", why)


# ---------------- 停止 / 取消语义 ----------------


class TestCancelSemantics(_ShutdownTestBase):
    def test_already_stopped_returns_immediately(self):
        stop = threading.Event()
        stop.set()
        ok, why = tasks_mod.run_shutdown_step(_params(count_seconds=5), {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")

    def test_stop_during_wait_aborts_before_any_command(self):
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(count_seconds=5, dry_run=False, warn_sec=0), {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")
        run.assert_not_called()

    def test_stop_during_warn_window_cancels(self):
        """提醒倒计时内停止 = 取消执行，且命令一次都没发出去。"""
        stop = threading.Event()
        threading.Timer(0.25, stop.set).start()
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=5, dry_run=False), {}, stop)
        self.assertFalse(ok)
        self.assertIn("已取消", why)
        run.assert_not_called()

    def test_warn_zero_fires_immediately(self):
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            ok, _ = tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=0, dry_run=False), {})
        self.assertTrue(ok)
        run.assert_called_once()

    def test_warn_window_waits_before_firing(self):
        """提醒 0.2 秒：命令必须等倒计时走完才发（给用户留取消机会）。"""
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            t0 = time.monotonic()
            tasks_mod.run_shutdown_step(
                _params(warn_sec=0.2, dry_run=False), {})
            fired_at = time.monotonic()
        self.assertTrue(run.called)
        self.assertGreaterEqual(fired_at - t0, 0.15)


# ---------------- 提醒倒计时期间的屏幕浮层 ----------------


class TestWarnOverlay(_ShutdownTestBase):
    """提醒倒计时期间屏幕下方的大号红色倒计时浮层。

    这里只验「**什么时候显示、显示什么、什么时候收**」——把 `app.power_overlay`
    的两个入口整个 patch 掉，一个真窗口都不建（浮层本体在 test_power_overlay.py 里测）。
    """

    def setUp(self):
        super().setUp()
        # Esc 监听已由 _ShutdownTestBase 统一换成替身（见那里的说明）
        import app.power_overlay as power_overlay_mod
        self.overlay = power_overlay_mod
        patcher = mock.patch.object(power_overlay_mod, "show_countdown",
                                    return_value=object())
        self.show = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(power_overlay_mod, "hide_countdown")
        self.hide = patcher.start()
        self.addCleanup(patcher.stop)

    def _remain_values(self):
        """每次调用浮层时的「剩余秒数」序列（show_countdown 的首个位置参数）。"""
        return [c[0][0] for c in self.show.call_args_list]

    def test_shows_with_action_label_and_font_size(self):
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")):
            ok, _ = tasks_mod.run_shutdown_step(
                _params(warn_sec=0.3, dry_run=False, power_action="restart",
                        warn_overlay_font_size=72), {})
        self.assertTrue(ok)
        self.assertTrue(self.show.called)
        args, kwargs = self.show.call_args_list[0]
        self.assertGreater(args[0], 0)                 # 剩余秒数
        self.assertEqual(args[1], "重启")               # 即将执行的动作
        self.assertEqual(kwargs.get("font_size"), 72)  # 配置的字号传到了浮层

    def test_font_size_defaults_to_overlay_default(self):
        tasks_mod.run_shutdown_step(_params(warn_sec=0.2), {})
        self.assertEqual(self.show.call_args_list[0][1].get("font_size"),
                         self.overlay.DEFAULT_FONT_SIZE)

    def test_font_size_is_clamped(self):
        """越界字号收进浮层的安全范围，别让一个手改的 config 撑爆屏幕。"""
        tasks_mod.run_shutdown_step(_params(warn_sec=0.2, warn_overlay_font_size=9999), {})
        size = self.show.call_args_list[0][1].get("font_size")
        self.assertLessEqual(size, self.overlay.MAX_FONT_SIZE)
        self.assertGreaterEqual(size, self.overlay.MIN_FONT_SIZE)

    def test_hidden_after_normal_finish(self):
        tasks_mod.run_shutdown_step(_params(warn_sec=0.2), {})
        self.assertTrue(self.hide.called)

    def test_hidden_when_cancelled_in_warn_window(self):
        """提醒期内被「停止」打断：浮层必须收起，不能留在桌面上骗人。"""
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(warn_sec=5, dry_run=False), {}, stop)
        self.assertFalse(ok)
        self.assertIn("已取消", why)
        run.assert_not_called()
        self.assertTrue(self.hide.called)

    def test_disabled_by_flag(self):
        tasks_mod.run_shutdown_step(_params(warn_sec=0.2, warn_overlay=False), {})
        self.show.assert_not_called()
        self.hide.assert_not_called()

    def test_no_overlay_when_warn_is_zero(self):
        """提醒 0 秒 = 立即执行，压根没有提醒期，不该弹浮层。"""
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")):
            tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=0, dry_run=False), {})
        self.show.assert_not_called()

    def test_overlay_failure_does_not_fail_the_step(self):
        """浮层画不出来（无 Qt 实例等）只能降级成「只有日志」，绝不能拖垮关机。"""
        self.show.side_effect = RuntimeError("没有 Qt 应用实例")
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=0.2, dry_run=False), {})
        self.assertTrue(ok, why)
        run.assert_called_once()

    def test_overlay_failure_stops_retrying_but_still_hides(self):
        """第一次就失败后不再每秒重试（省掉无意义的跨线程调度），收尾照样收一次。"""
        self.show.side_effect = RuntimeError("炸了")
        tasks_mod.run_shutdown_step(_params(warn_sec=0.3), {})
        self.assertEqual(self.show.call_count, 1)
        self.assertTrue(self.hide.called)

    def test_countdown_text_ticks_down_and_is_throttled(self):
        """文案随剩余秒数递减刷新；同一秒内不重复刷（否则浮层宽度会反复跳）。"""
        stop = threading.Event()
        threading.Timer(0.25, stop.set).start()
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=1.05, dry_run=False), {}, stop)
        run.assert_not_called()
        shown = self._remain_values()
        self.assertGreaterEqual(len(shown), 2)
        self.assertGreater(shown[0], shown[-1])

    def test_sub_second_warn_repaints_once(self):
        """不足 1 秒的提醒（ceil 后恒为 1 秒）只画一次。"""
        tasks_mod.run_shutdown_step(_params(warn_sec=0.4), {})
        self.assertEqual(self.show.call_count, 1)

    def test_first_paint_uses_ceil_so_it_never_shows_zero(self):
        """首帧就用「向上取整」的秒数：提醒 0.3 秒显示 1 秒，而不是刺眼的 0 秒。"""
        tasks_mod.run_shutdown_step(_params(warn_sec=0.3), {})
        self.assertEqual(self._remain_values(), [0.3])
        self.assertEqual(self.overlay.format_remain(0.3), "1 秒")


# ---------------- 提醒期内按 Esc 键取消 ----------------


class _FakeEscListener:
    """替身 Esc 监听器：能让测试「按一下 Esc」，并记录启停。"""

    def __init__(self, on_esc):
        self.on_esc = on_esc
        self.started = False
        self.stopped = False
        self.ok = True
        self.ready = threading.Event()

    def start(self) -> bool:
        self.started = True
        self.ready.set()
        return self.ok

    def stop(self) -> None:
        self.stopped = True

    def press(self) -> None:
        """模拟按下 Esc（真的会调到闸门的 `press_esc`）。"""
        self.on_esc()

    def __enter__(self) -> bool:
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


class _EscTestBase(_ShutdownTestBase):
    """替身 Esc 监听 + 替身浮层；其余全真（真走提醒倒计时、真判取消）。"""

    def setUp(self):
        super().setUp()
        self.listeners: list[_FakeEscListener] = []
        self.listen_ok = True

        def factory(on_esc):
            obj = _FakeEscListener(on_esc)
            obj.ok = self.listen_ok
            self.listeners.append(obj)
            return obj

        patcher = mock.patch.object(cancel_key_mod, "EscListener", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

        import app.power_overlay as power_overlay_mod
        self.overlay = power_overlay_mod
        patcher = mock.patch.object(power_overlay_mod, "show_countdown",
                                    return_value=object())
        self.show = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(power_overlay_mod, "hide_countdown")
        self.hide = patcher.start()
        self.addCleanup(patcher.stop)

    def _listen(self, timeout: float = 2.0) -> _FakeEscListener:
        """等提醒期真的挂上监听（监听是在进入提醒期那一刻才挂的）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.listeners:
                return self.listeners[0]
            time.sleep(0.01)
        self.fail("提醒期内始终没有挂上 Esc 监听")

    def _spawn(self, params: dict, stop=None):
        """后台线程跑一步，返回 (线程, 结果盒)。"""
        out: dict = {}

        def work():
            out["r"] = tasks_mod.run_shutdown_step(params, {}, stop)

        th = threading.Thread(target=work, daemon=True)
        th.start()
        return th, out


class TestCancelGate(_EscTestBase):
    """`_CancelGate`：把「流程停止」与「按 Esc」合成一个可等待对象（鸭子类型 Event）。"""

    def test_source_tracks_both_sources(self):
        stop = threading.Event()
        gate = tasks_mod._CancelGate(stop)
        self.assertEqual(gate.source(), "")
        self.assertFalse(gate.is_set())
        gate.press_esc()
        self.assertEqual(gate.source(), "esc")
        self.assertTrue(gate.is_set())

    def test_source_prefers_stop(self):
        """两个都触发时原因记在「停止」上：流程正在收摊，别再写「按了 Esc」。"""
        stop = threading.Event()
        stop.set()
        gate = tasks_mod._CancelGate(stop)
        gate.press_esc()
        self.assertEqual(gate.source(), "stop")

    def test_is_set_without_stop_event(self):
        """没有流程停止事件（stop=None）时只看 Esc。"""
        gate = tasks_mod._CancelGate(None)
        self.assertFalse(gate.is_set())
        gate.press_esc()
        self.assertTrue(gate.is_set())

    def test_wait_returns_true_when_esc_pressed(self):
        gate = tasks_mod._CancelGate(None)
        threading.Timer(0.05, gate.press_esc).start()
        t0 = time.monotonic()
        self.assertTrue(gate.wait(2.0))
        self.assertLess(time.monotonic() - t0, 1.0)   # 是被按醒的，不是等满 2 秒

    def test_wait_returns_true_when_stop_set(self):
        stop = threading.Event()
        gate = tasks_mod._CancelGate(stop)
        threading.Timer(0.05, stop.set).start()
        t0 = time.monotonic()
        self.assertTrue(gate.wait(2.0))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_wait_times_out(self):
        gate = tasks_mod._CancelGate(None)
        self.assertFalse(gate.wait(0.05))
        self.assertFalse(gate.wait(0))
        self.assertFalse(gate.wait(-1))

    def test_wait_survives_garbage_timeout(self):
        """配置被手改坏了也别抛：非法等待时长按 0 处理（立刻返回）。"""
        gate = tasks_mod._CancelGate(None)
        self.assertFalse(gate.wait("一会儿"))


class TestEscCancel(_EscTestBase):
    """提醒期内按 Esc 键取消（需求：「按下 Esc 按键，就取消执行动作」）。

    作用范围也一并钉住：**只在提醒期挂监听**——长倒计时 / 等时间点 / 等条件会持续
    几十分钟甚至几小时，用户在别的程序里随手按 Esc 就把任务无声取消掉，太容易误伤。
    """

    def test_esc_during_warn_window_cancels(self):
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            th, out = self._spawn(_params(count_seconds=0.05, warn_sec=5,
                                          dry_run=False))
            self._listen().press()
            th.join(5)
        self.assertFalse(th.is_alive(), "按了 Esc 还在等，说明闸门没起作用")
        ok, why = out["r"]
        self.assertFalse(ok)
        self.assertIn("已取消", why)
        self.assertIn("Esc", why)
        run.assert_not_called()             # 取消必须发生在发命令之前
        self.assertTrue(self.hide.called)   # 浮层不能留在桌面上骗人

    def test_listener_is_stopped_after_cancel(self):
        with mock.patch.object(tasks_mod, "_run_power_command") as run:
            th, out = self._spawn(_params(count_seconds=0.05, warn_sec=5,
                                          dry_run=False))
            listener = self._listen()
            listener.press()
            th.join(5)
        self.assertTrue(listener.started)
        self.assertTrue(listener.stopped)   # 用完就摘，别在系统里留全局钩子
        self.assertFalse(out["r"][0])
        run.assert_not_called()

    def test_esc_is_not_listened_during_trigger_wait(self):
        """等触发阶段（本用例是倒计时）不挂监听：那会儿按 Esc 不算数。"""
        th, out = self._spawn(_params(count_seconds=0.4, warn_sec=0))
        time.sleep(0.15)
        self.assertEqual(self.listeners, [], "触发等待阶段就挂了 Esc 监听")
        th.join(5)
        self.assertTrue(out["r"][0], out["r"][1])   # 没被打断，正常触发

    def test_listener_unregistered_after_normal_finish(self):
        tasks_mod.run_shutdown_step(_params(warn_sec=0.2), {})
        self.assertEqual(len(self.listeners), 1)
        self.assertTrue(self.listeners[0].stopped)

    def test_no_listener_when_warn_is_zero(self):
        """提醒 0 秒 = 立即执行，没有提醒期，也就没有「按 Esc 反悔」的机会。"""
        tasks_mod.run_shutdown_step(_params(count_seconds=0.05, warn_sec=0), {})
        self.assertEqual(self.listeners, [])
        self.show.assert_not_called()

    def test_overlay_note_tells_the_user_to_press_esc(self):
        """屏幕上的提示必须写明按 Esc（用户就是要求「添加一个提示」）。"""
        tasks_mod.run_shutdown_step(_params(warn_sec=0.3), {})
        note = self.show.call_args_list[0][1].get("note")
        self.assertIn("Esc", note)

    def test_note_falls_back_when_listener_unavailable(self):
        """监听挂不上（没装库 / 没权限）就不写「按 Esc 取消」，但关机流程照跑。"""
        self.listen_ok = False
        with mock.patch.object(tasks_mod, "_run_power_command",
                               return_value=(True, "已下发")) as run:
            ok, why = tasks_mod.run_shutdown_step(
                _params(count_seconds=0.05, warn_sec=0.2, dry_run=False), {})
        self.assertTrue(ok, why)
        run.assert_called_once()
        self.assertEqual(self.show.call_args_list[0][1].get("note"),
                         self.overlay.NOTE_NO_ESC)
        self.assertTrue(self.listeners[0].stopped)

    def test_stop_still_wins_in_warn_window(self):
        """提醒期内点「停止」的措辞不该被 Esc 抢走：两种取消方式的文案要能区分。"""
        stop = threading.Event()
        th, out = self._spawn(_params(count_seconds=0.05, warn_sec=5,
                                      dry_run=False), stop)
        self._listen()
        stop.set()
        th.join(5)
        ok, why = out["r"]
        self.assertFalse(ok)
        self.assertIn("停止指令", why)
        self.assertNotIn("Esc", why)


# ---------------- 流程分发（FlowRunner） ----------------


class TestFlowDispatch(_ShutdownTestBase):
    def _runner(self, flow):
        runner = flows_mod.FlowRunner(flow)
        runner.vars = flows_mod.FlowVariableStore(flow)
        return runner

    def test_runner_dispatches_shutdown_step(self):
        flow = Flow(name="f", steps=[FlowStep(type="shutdown", params=_params())])
        runner = self._runner(flow)
        with mock.patch.object(flows_mod, "run_shutdown_step",
                               return_value=(True, "ok")) as step_fn:
            reason = runner._run_once()
        self.assertIsNone(reason)
        step_fn.assert_called_once()
        # 参数与变量容器都按约定透传
        args = step_fn.call_args[0]
        self.assertIn("trigger_mode", args[0])
        self.assertIsInstance(args[1], dict)

    def test_step_failure_marks_flow_failed(self):
        flow = Flow(name="f", steps=[FlowStep(type="shutdown", params=_params())])
        runner = self._runner(flow)
        with mock.patch.object(flows_mod, "run_shutdown_step",
                               return_value=(False, "关机失败：拒绝访问")):
            reason = runner._run_once()
        self.assertIsNotNone(reason)
        self.assertIn("关机失败", reason)


# ---------------- 参数对话框 ----------------


class TestDialog(_ShutdownTestBase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="shutdown", params=_params(**params))
        return StepParamsDialog(step), step

    def test_rows_follow_trigger_mode(self):
        dlg, _ = self._dlg()
        dlg.sd_mode.setCurrentIndex(dlg.sd_mode.findData("at_time"))
        rows = dlg._sd_form
        self.assertTrue(rows.isRowVisible(dlg._sd_time_rows[0]))
        self.assertFalse(rows.isRowVisible(dlg._sd_count_rows[0]))
        self.assertFalse(rows.isRowVisible(dlg._sd_cond_rows[0]))

        dlg.sd_mode.setCurrentIndex(dlg.sd_mode.findData("countdown"))
        self.assertFalse(rows.isRowVisible(dlg._sd_time_rows[0]))
        self.assertTrue(rows.isRowVisible(dlg._sd_count_rows[0]))

        dlg.sd_mode.setCurrentIndex(dlg.sd_mode.findData("condition"))
        self.assertTrue(rows.isRowVisible(dlg._sd_cond_rows[0]))
        self.assertTrue(rows.isRowVisible(dlg._sd_common_rows[0]))

    def test_condition_submode_toggles_image_and_text_rows(self):
        dlg, _ = self._dlg(trigger_mode="condition", cond_mode="image")
        self.assertTrue(dlg._sd_form.isRowVisible(dlg._sd_img_rows[0]))
        self.assertFalse(dlg._sd_form.isRowVisible(dlg._sd_text_rows[0]))
        dlg.sd_cond_mode.setCurrentIndex(dlg.sd_cond_mode.findData("text"))
        self.assertFalse(dlg._sd_form.isRowVisible(dlg._sd_img_rows[0]))
        self.assertTrue(dlg._sd_form.isRowVisible(dlg._sd_text_rows[0]))

    def test_fill_and_apply_roundtrip(self):
        dlg, step = self._dlg(trigger_mode="countdown", count_hours=2,
                              count_minutes=15, count_seconds=30,
                              power_action="restart", warn_sec=60,
                              force_close_apps=False, dry_run=True)
        self.assertEqual(dlg.sd_hours.value(), 2)
        self.assertEqual(dlg.sd_minutes.value(), 15)
        self.assertEqual(dlg.sd_action.currentData(), "restart")
        self.assertEqual(dlg.sd_warn.value(), 60)
        self.assertFalse(dlg.sd_force.isChecked())
        dlg.apply_to(step)
        self.assertEqual(step.params["trigger_mode"], "countdown")
        self.assertEqual(step.params["count_hours"], 2)
        self.assertEqual(step.params["power_action"], "restart")
        self.assertEqual(step.params["warn_sec"], 60)
        self.assertFalse(step.params["force_close_apps"])
        self.assertTrue(step.params["dry_run"])
        self.assertIn("2 小时 15 分 30 秒", step.summary())

    def test_apply_keeps_all_three_modes(self):
        """切到某一触发方式保存时，另外两种方式已填的内容不能丢。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="shutdown", params=_params(
            trigger_mode="at_time", at_time="23:30", text="装好了"))
        dlg = StepParamsDialog(step)
        dlg.sd_mode.setCurrentIndex(dlg.sd_mode.findData("countdown"))
        dlg.sd_hours.setValue(1)
        dlg.apply_to(step)
        self.assertEqual(step.params["at_time"], "23:30")
        self.assertEqual(step.params["text"], "装好了")
        self.assertEqual(step.params["count_hours"], 1)

    def test_overlay_rows_default_and_roundtrip(self):
        dlg, step = self._dlg(warn_sec=30, warn_overlay=True,
                              warn_overlay_font_size=64)
        self.assertTrue(dlg.sd_overlay.isChecked())
        self.assertEqual(dlg.sd_overlay_size.value(), 64)
        dlg.apply_to(step)
        self.assertTrue(step.params["warn_overlay"])
        self.assertEqual(step.params["warn_overlay_font_size"], 64)

    def test_overlay_defaults_when_params_missing(self):
        """旧流程没有这两个键：回填成「显示浮层 + 默认字号」，不能回填成空。"""
        from app.power_overlay import DEFAULT_FONT_SIZE
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="shutdown", params={
            "trigger_mode": "countdown", "count_minutes": 5, "warn_sec": 30})
        dlg = StepParamsDialog(step)
        self.assertTrue(dlg.sd_overlay.isChecked())
        self.assertEqual(dlg.sd_overlay_size.value(), DEFAULT_FONT_SIZE)
        dlg.apply_to(step)
        self.assertTrue(step.params["warn_overlay"])
        self.assertEqual(step.params["warn_overlay_font_size"], DEFAULT_FONT_SIZE)

    def test_overlay_disabled_when_warn_is_zero(self):
        """提醒 0 秒 = 立即执行，没有提醒期，浮层整组置灰。"""
        dlg, _ = self._dlg(warn_sec=0)
        self.assertFalse(dlg.sd_overlay.isEnabled())
        self.assertFalse(dlg.sd_overlay_size.isEnabled())
        dlg.sd_warn.setValue(10)
        self.assertTrue(dlg.sd_overlay.isEnabled())

    def test_font_size_locked_until_overlay_checked(self):
        dlg, _ = self._dlg(warn_sec=10, warn_overlay=False)
        self.assertFalse(dlg.sd_overlay_size.isEnabled())
        dlg.sd_overlay.setChecked(True)
        self.assertTrue(dlg.sd_overlay_size.isEnabled())

    def test_hint_describes_the_overlay(self):
        dlg, _ = self._dlg(warn_sec=30, warn_overlay=True, warn_overlay_font_size=64)
        hint = dlg.sd_hint.text()
        self.assertIn("30 秒后关机", hint)
        self.assertIn("64 pt", hint)
        dlg.sd_overlay.setChecked(False)
        self.assertNotIn("大号红色倒计时", dlg.sd_hint.text())

    def test_hint_mentions_esc_cancel_only_when_there_is_a_warn_window(self):
        """底部说明要写明「按 Esc 取消」，但只在真有提醒期时——0 秒时没有这种机会。"""
        dlg, _ = self._dlg(warn_sec=30)
        self.assertIn("Esc", dlg.sd_hint.text())
        dlg.sd_warn.setValue(0)
        self.assertNotIn("Esc", dlg.sd_hint.text())
        self.assertIn("停止", dlg.sd_hint.text())

    def _accept(self, dlg):
        """点「确定」并返回被 patch 的 QMessageBox，用于断言是否弹了拦截提示。"""
        with mock.patch("app.ui.flow_dialog.QMessageBox") as qmb:
            dlg.accept()
        return qmb

    def test_accept_rejects_bad_time(self):
        dlg, _ = self._dlg(trigger_mode="at_time", at_time="")
        qmb = self._accept(dlg)
        self.assertTrue(qmb.warning.called)
        self.assertIn("关机时间", qmb.warning.call_args[0][1])

    def test_accept_rejects_zero_countdown(self):
        dlg, _ = self._dlg(trigger_mode="countdown", count_hours=0,
                           count_minutes=0, count_seconds=0)
        qmb = self._accept(dlg)
        self.assertTrue(qmb.warning.called)
        self.assertIn("倒计时", qmb.warning.call_args[0][1])

    def test_accept_rejects_condition_without_image(self):
        dlg, _ = self._dlg(trigger_mode="condition", cond_mode="image", image="")
        dlg._image = ""
        dlg._image_path = ""
        qmb = self._accept(dlg)
        self.assertTrue(qmb.warning.called)
        self.assertIn("目标图片", qmb.warning.call_args[0][1])

    def test_accept_rejects_condition_without_text(self):
        dlg, _ = self._dlg(trigger_mode="condition", cond_mode="text", text="")
        qmb = self._accept(dlg)
        self.assertTrue(qmb.warning.called)
        self.assertIn("目标文字", qmb.warning.call_args[0][1])

    def test_accept_passes_when_valid(self):
        dlg, _ = self._dlg(trigger_mode="at_time", at_time="23:30")
        qmb = self._accept(dlg)
        self.assertFalse(qmb.warning.called)


class TestConfigSerialization(_ShutdownTestBase):
    def test_flow_roundtrip_keeps_shutdown_params(self):
        from app.config import flow_from_dict, flow_to_dict
        flow = Flow(name="f", steps=[FlowStep(type="shutdown", params=_params(
            trigger_mode="at_time", at_time="23:30", power_action="lock"))])
        back = flow_from_dict(flow_to_dict(flow))
        p = back.steps[0].params
        self.assertEqual(p["trigger_mode"], "at_time")
        self.assertEqual(p["at_time"], "23:30")
        self.assertEqual(p["power_action"], "lock")
        # 缺失字段用默认补齐（旧流程文件里没有 shutdown 的字段时也能跑）
        self.assertIn("dry_run", p)


if __name__ == "__main__":
    unittest.main()
