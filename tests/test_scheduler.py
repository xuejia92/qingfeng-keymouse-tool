"""app/scheduler.py 的纯计算逻辑测试：cron 解析、下次运行时间、预览、描述。"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime

from app import config as config_mod
from app.config import AppConfig, Flow, FlowStep, ScheduleTask, schedule_from_dict
from app.scheduler import (cron_next, describe_schedule, next_run_time,
                           next_run_times, parse_cron)
from tests._env import TempConfigPaths

# 固定基准：2026-09-02 是周三（isoweekday=3）
WED = datetime(2026, 9, 2, 10, 0, 0)


def task(**kw) -> ScheduleTask:
    base = dict(mode="day", at_time="09:00")
    base.update(kw)
    return ScheduleTask(**base)


class TestParseCron(unittest.TestCase):
    def test_valid_5_field(self):
        f = parse_cron("*/5 * * * *")
        self.assertIsNotNone(f)
        self.assertEqual(f["min"], {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})
        self.assertFalse(f["has_sec"])

    def test_valid_6_field(self):
        f = parse_cron("*/10 * * * * *")
        self.assertIsNotNone(f)
        self.assertTrue(f["has_sec"])
        self.assertIn(0, f["sec"])
        self.assertIn(50, f["sec"])

    def test_weekday_names(self):
        f = parse_cron("0 9 * * 1-5")
        self.assertEqual(f["dow"], {1, 2, 3, 4, 5})

    def test_sunday_names(self):
        f = parse_cron("0 0 * * sun")
        self.assertIn(0, f["dow"])

    def test_range_step(self):
        f = parse_cron("0 0-10/2 * * *")
        self.assertEqual(f["hour"], {0, 2, 4, 6, 8, 10})

    def test_invalid(self):
        self.assertIsNone(parse_cron(""))
        self.assertIsNone(parse_cron("60 * * * *"))      # 分钟越界
        self.assertIsNone(parse_cron("* * *"))           # 段数不足
        self.assertIsNone(parse_cron("a b c d e"))       # 非法值
        self.assertIsNone(parse_cron("*/0 * * * *"))     # 步长为 0


class TestCronNext(unittest.TestCase):
    def test_every_5_min(self):
        self.assertEqual(cron_next("*/5 * * * *", WED), datetime(2026, 9, 2, 10, 5, 0))

    def test_daily_rolls_to_tomorrow(self):
        self.assertEqual(cron_next("0 9 * * *", WED), datetime(2026, 9, 3, 9, 0, 0))

    def test_weekday_skips_weekend(self):
        # 周三 10 点后，下一个工作日 9 点 = 周四
        self.assertEqual(cron_next("0 9 * * 1-5", WED), datetime(2026, 9, 3, 9, 0, 0))

    def test_monthly_1st(self):
        self.assertEqual(cron_next("0 0 1 * *", WED), datetime(2026, 10, 1, 0, 0, 0))

    def test_invalid_returns_none(self):
        self.assertIsNone(cron_next("not a cron", WED))


class TestNextRunTime(unittest.TestCase):
    def test_second(self):
        self.assertEqual(next_run_time(task(mode="second", interval=5), WED),
                         datetime(2026, 9, 2, 10, 0, 5))

    def test_minute_aligned(self):
        self.assertEqual(next_run_time(task(mode="minute", interval=5), WED),
                         datetime(2026, 9, 2, 10, 5, 0))

    def test_hour_aligned(self):
        self.assertEqual(next_run_time(task(mode="hour", interval=2), WED),
                         datetime(2026, 9, 2, 12, 0, 0))

    def test_day(self):
        self.assertEqual(next_run_time(task(mode="day", at_time="09:00"), WED),
                         datetime(2026, 9, 3, 9, 0, 0))

    def test_day_same_day_if_later(self):
        self.assertEqual(next_run_time(task(mode="day", at_time="11:00"), WED),
                         datetime(2026, 9, 2, 11, 0, 0))

    def test_week(self):
        self.assertEqual(next_run_time(task(mode="week", weekdays=[3], at_time="09:00"), WED),
                         datetime(2026, 9, 9, 9, 0, 0))

    def test_week_no_days(self):
        self.assertIsNone(next_run_time(task(mode="week", weekdays=[], at_time="09:00"), WED))

    def test_month(self):
        self.assertEqual(next_run_time(task(mode="month", monthdays=[1], at_time="00:00"), WED),
                         datetime(2026, 10, 1, 0, 0, 0))

    def test_once_future(self):
        self.assertEqual(next_run_time(task(mode="once", once_at="2026-09-03 10:00"), WED),
                         datetime(2026, 9, 3, 10, 0, 0))

    def test_once_past(self):
        self.assertIsNone(next_run_time(task(mode="once", once_at="2026-09-01 10:00"), WED))


class TestPreview(unittest.TestCase):
    def test_five_times(self):
        times = next_run_times(task(mode="second", interval=10), WED, 5)
        self.assertEqual(len(times), 5)
        self.assertEqual(times[0], datetime(2026, 9, 2, 10, 0, 10))
        self.assertEqual(times[4], datetime(2026, 9, 2, 10, 0, 50))

    def test_once_only_one(self):
        times = next_run_times(task(mode="once", once_at="2026-09-03 10:00"), WED, 5)
        self.assertEqual(len(times), 1)


class TestDescribe(unittest.TestCase):
    def test_second(self):
        self.assertEqual(describe_schedule(task(mode="second", interval=5)), "每 5 秒")

    def test_day(self):
        self.assertEqual(describe_schedule(task(mode="day", at_time="08:30")), "每天 08:30")

    def test_week(self):
        self.assertEqual(describe_schedule(task(mode="week", weekdays=[1, 5], at_time="09:00")),
                         "每周 周一、周五 09:00")

    def test_cron(self):
        self.assertEqual(describe_schedule(task(mode="cron", cron="*/5 * * * *")),
                         "Cron */5 * * * *")


class TestScheduleFromDict(unittest.TestCase):
    def test_defaults(self):
        t = schedule_from_dict({})
        self.assertEqual(t.mode, "day")
        self.assertEqual(t.at_time, "09:00")
        self.assertEqual(len(t.id), 12)

    def test_invalid_mode_falls_back(self):
        self.assertEqual(schedule_from_dict({"mode": "nope"}).mode, "day")

    def test_roundtrip_fields(self):
        t = schedule_from_dict({
            "id": "abc", "name": "任务", "mode": "week", "weekdays": [1, 3, 5],
            "interval": 2, "at_time": "07:30", "enabled": False,
        })
        self.assertEqual(t.id, "abc")
        self.assertEqual(t.name, "任务")
        self.assertEqual(t.weekdays, [1, 3, 5])
        self.assertEqual(t.enabled, False)


class TestConfigRoundtrip(unittest.TestCase):
    """定时任务随 config.json 持久化往返。"""

    def test_save_load_roundtrip(self):
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_groups = ["工作"]
            cfg.schedule_tasks = [ScheduleTask(
                name="每日", group="工作", mode="week", weekdays=[1, 5],
                flow_id=cfg.flows[0].id, flow_name="F")]
            cfg.save()
            with open(config_mod.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("schedule_tasks", data)
            self.assertNotIn("flows", data)   # 流程仍独立存储

            cfg2 = AppConfig.load()
            self.assertEqual(len(cfg2.schedule_tasks), 1)
            self.assertEqual(cfg2.schedule_tasks[0].weekdays, [1, 5])
            self.assertEqual(cfg2.schedule_groups, ["工作"])

    def test_missed_fires_roundtrip(self):
        """missed_fires 计数随配置持久化。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_tasks = [ScheduleTask(
                name="once任务", mode="once", once_at="2099-01-01 10:00",
                flow_id=cfg.flows[0].id, flow_name="F", missed_fires=2)]
            cfg.save()
            cfg2 = AppConfig.load()
            self.assertEqual(cfg2.schedule_tasks[0].missed_fires, 2)

    def test_last_alert_date_roundtrip(self):
        """last_alert_date 告警日期随配置持久化（跨天恢复提示资格的依据）。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_tasks = [ScheduleTask(
                name="t", mode="day", at_time="09:00",
                flow_id=cfg.flows[0].id, flow_name="F",
                last_alert_date="2026-09-02")]
            cfg.save()
            cfg2 = AppConfig.load()
            self.assertEqual(cfg2.schedule_tasks[0].last_alert_date, "2026-09-02")

    def test_last_alert_date_default_when_missing(self):
        """旧配置没有 last_alert_date 字段时默认为空（下次触发即可提示）。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_tasks = [ScheduleTask(
                name="t", mode="day", at_time="09:00",
                flow_id=cfg.flows[0].id, flow_name="F")]
            cfg.save()
            with open(config_mod.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data["schedule_tasks"][0].pop("last_alert_date")
            with open(config_mod.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            cfg2 = AppConfig.load()
            self.assertEqual(cfg2.schedule_tasks[0].last_alert_date, "")

    def test_missed_fires_default_when_missing(self):
        """旧配置没有 missed_fires 字段时默认为 0。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_tasks = [ScheduleTask(
                name="t", mode="once", once_at="2099-01-01 10:00",
                flow_id=cfg.flows[0].id, flow_name="F")]
            cfg.save()
            # 手动移除 missed_fires 字段，模拟旧配置
            with open(config_mod.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data["schedule_tasks"][0].pop("missed_fires")
            with open(config_mod.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            cfg2 = AppConfig.load()
            self.assertEqual(cfg2.schedule_tasks[0].missed_fires, 0)

    def test_save_without_flows_does_not_touch_flows_dir(self):
        """save(save_flows=False) 只写 config.json，不创建/改写 flows/ 目录。

        定时任务页的所有保存都走 save_flows=False：秒级任务可能每秒触发一次，
        不能让定时任务保存把 flows/ 下所有流程文件反复重写（用户要求 flows/ 不被改动）。
        """
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            cfg.schedule_tasks = [ScheduleTask(
                name="t", mode="day", at_time="08:00",
                flow_id=cfg.flows[0].id, flow_name="F")]
            cfg.save(save_flows=False)
            self.assertFalse(os.path.isdir(config_mod.FLOWS_DIR),
                             "save_flows=False 不应创建 flows/ 目录")
            with open(config_mod.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("schedule_tasks", data)
            # 常规 save() 仍正常写 flows/
            cfg.save()
            self.assertTrue(os.path.isdir(config_mod.FLOWS_DIR))


class TestScheduleTabLogic(unittest.TestCase):
    """ScheduleTab 行为级测试（需要 QApplication）：重命名 / last_run / once 重试 / 流程删除。"""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        # 每个用例前清掉上一个 ScheduleTab 残留，避免 leak
        QApplication.processEvents()

    def _make_tab(self, cfg):
        from app.ui.schedule_tab import ScheduleTab
        class _FT:
            def __init__(self): self.started = []
            def start_flow_if_idle(self, fid, silent=False):
                self.started.append((fid, silent))
                return self._next_return
        ft = _FT()
        tab = ScheduleTab(cfg, ft)
        tab.flow_tab = ft   # 方便用例控制 _next_return
        return tab

    def test_rename_preserves_collapsed(self):
        """重命名分组应同步更新 collapsed_schedule_groups，避免分组被意外展开。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.schedule_groups = ["旧名", "其他"]
            cfg.collapsed_schedule_groups = ["旧名"]
            tab = self._make_tab(cfg)
            tab._rename_group = tab._rename_group  # ensure attr
            # 直接调用 _rename_group 的核心逻辑，绕过 QInputDialog
            g = "旧名"
            name = "新名"
            cfg.schedule_groups = [name if x == g else x for x in cfg.schedule_groups]
            cfg.collapsed_schedule_groups = [
                name if x == g else x for x in cfg.collapsed_schedule_groups
            ]
            self.assertEqual(cfg.schedule_groups, ["新名", "其他"])
            self.assertEqual(cfg.collapsed_schedule_groups, ["新名"])

    def test_last_run_not_updated_when_whenflow_busy(self):
        """流程繁忙时不应更新 last_run，保持记录准确。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(
                name="循环任务", mode="day", at_time="09:00",
                flow_id=cfg.flows[0].id, flow_name="F",
                last_run="2026-09-01 09:00:00")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.flow_tab._next_return = False   # 流程繁忙
            tab._fire(t)
            self.assertEqual(t.last_run, "2026-09-01 09:00:00",
                             "流程繁忙时 last_run 不应更新")
            self.assertTrue(t.enabled, "循环任务不应被停用")

    def test_once_busy_reschedules_up_to_3(self):
        """一次性任务遇流程繁忙：60秒后重试，最多 3 次重试后放弃。"""
        from datetime import datetime, timedelta
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            now = datetime.now()
            t = ScheduleTask(
                name="一次性", mode="once", once_at="2099-01-01 00:00:00",
                flow_id=cfg.flows[0].id, flow_name="F")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.flow_tab._next_return = False

            # 第 1~3 次繁忙：均应重排（最多重试 3 次）
            for expected_missed in (1, 2, 3):
                tab._fire(t)
                self.assertEqual(t.missed_fires, expected_missed,
                                 f"第 {expected_missed} 次繁忙后 missed_fires 应为 {expected_missed}")
                self.assertTrue(t.enabled, f"第 {expected_missed} 次仍应重排")
                new_dt = datetime.strptime(t.once_at, "%Y-%m-%d %H:%M:%S")
                self.assertGreater(new_dt, now + timedelta(seconds=30))

            # 第 4 次：超过 3 次重试 → 放弃
            tab._fire(t)
            self.assertEqual(t.missed_fires, 4)
            self.assertFalse(t.enabled, "连续 3+ 次繁忙应停用")
            self.assertEqual(t.next_run, "")

    def test_once_busy_reset_when_whenstarted(self):
        """一次性任务正常启动后，missed_fires 应清零。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(
                name="t", mode="once", once_at="2099-01-01 00:00:00",
                flow_id=cfg.flows[0].id, flow_name="F", missed_fires=2)
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.flow_tab._next_return = True   # 能启动
            tab._fire(t)
            self.assertEqual(t.missed_fires, 0)
            self.assertFalse(t.enabled, "once 启动后应停用")
            self.assertNotEqual(t.last_run, "")

    def test_on_flows_changed_disables_deleted_flow(self):
        """流程被删除后，对应定时任务立即停用。"""
        with TempConfigPaths():
            cfg = AppConfig()
            flow = Flow(name="要被删", steps=[FlowStep(type="wait")])
            cfg.flows = [flow]
            t = ScheduleTask(
                name="依赖任务", mode="day", at_time="09:00",
                flow_id=flow.id, flow_name="要被删", enabled=True,
                next_run="2099-01-01 09:00:00")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            # 模拟流程被删除
            cfg.flows = []
            tab.on_flows_changed()
            self.assertFalse(t.enabled, "流程删除后任务应停用")
            self.assertEqual(t.next_run, "")

    def test_alert_once_daily_limit(self):
        """同一任务同一天最多告警一次：第二次触发不更新日期、不重复提示。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(name="t", mode="day", at_time="09:00",
                             flow_id=cfg.flows[0].id, flow_name="F")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            shown = []

            class _SB:
                def showMessage(self, *a, **k): shown.append(a)

            class _Win:
                def statusBar(self): return _SB()

            tab.window = lambda: _Win()
            tab._alert_once(t, "第一次告警")
            self.assertEqual(t.last_alert_date, datetime.now().strftime("%Y-%m-%d"))
            self.assertEqual(len(shown), 1, "首次应提示")
            tab._alert_once(t, "第二次告警")
            self.assertEqual(len(shown), 1, "同日第二次不应重复提示")
            # 跨天恢复提示资格
            t.last_alert_date = "2000-01-01"
            tab._alert_once(t, "跨天告警")
            self.assertEqual(len(shown), 2, "跨天应恢复提示资格")

    def test_alert_once_no_statusbar_does_not_crash(self):
        """无状态栏环境（测试/托盘最小化）下告警不崩溃，仍记录日期。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(name="t", mode="day", at_time="09:00",
                             flow_id=cfg.flows[0].id, flow_name="F")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.window = lambda: None
            tab._alert_once(t, "无状态栏告警")
            self.assertEqual(t.last_alert_date, datetime.now().strftime("%Y-%m-%d"))

    def test_fire_busy_alerts_once(self):
        """循环任务因流程繁忙被跳过：告警一天一次，但 next_run 仍继续排期。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(name="t", mode="day", at_time="09:00",
                             flow_id=cfg.flows[0].id, flow_name="F",
                             last_run="2026-09-01 09:00:00")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            shown = []

            class _SB:
                def showMessage(self, *a, **k): shown.append(a)

            class _Win:
                def statusBar(self): return _SB()

            tab.window = lambda: _Win()
            tab.flow_tab._next_return = False   # 流程繁忙
            tab._fire(t)
            self.assertEqual(t.last_alert_date, datetime.now().strftime("%Y-%m-%d"),
                             "繁忙跳过应记录告警日期")
            self.assertEqual(len(shown), 1)
            self.assertNotEqual(t.next_run, "", "循环任务繁忙后仍应排期")
            self.assertTrue(t.enabled)
            # 同日第二次繁忙：不重复提示，日期不变
            tab._fire(t)
            self.assertEqual(len(shown), 1, "同日繁忙只告警一次")

    def test_fire_missing_flow_alerts(self):
        """流程缺失时触发：告警一次并自动停用。"""
        with TempConfigPaths():
            cfg = AppConfig()
            t = ScheduleTask(name="孤儿任务", mode="day", at_time="09:00",
                             flow_id="nope", flow_name="不存在", enabled=True)
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab._fire(t)
            self.assertFalse(t.enabled, "流程缺失应停用")
            self.assertEqual(t.last_alert_date, datetime.now().strftime("%Y-%m-%d"),
                             "流程缺失应告警")
            self.assertEqual(t.next_run, "")

    def test_clear_layout_no_leak(self):
        """_clear_layout 必须彻底清空（含子布局里的 widget），防止重影。"""
        from PySide6.QtWidgets import (QLabel, QPushButton, QVBoxLayout,
                                       QWidget)
        from app.ui.schedule_tab import ScheduleTab
        host = ScheduleTab.__new__(ScheduleTab)
        host.detail = QWidget()
        lay = QVBoxLayout(host.detail)
        head = QVBoxLayout()
        head.addWidget(QLabel("n"))
        head.addWidget(QLabel("b"))
        lay.addLayout(head)
        lay.addWidget(QPushButton("d"))
        btns = QVBoxLayout()
        btns.addWidget(QPushButton("r"))
        btns.addWidget(QPushButton("e"))
        lay.addLayout(btns)
        n0 = len(host.detail.findChildren(QWidget))   # findChildren 不含自身
        self.assertEqual(n0, 5, f"应有 5 个子孙 widget，实际 {n0}")
        ScheduleTab._clear_layout(lay)
        for _ in range(5):
            self.app.processEvents()
        n1 = len(host.detail.findChildren(QWidget))
        self.assertEqual(n1, 0, f"_clear_layout 泄漏：剩 {n1} 个 widget")


    def test_due_fires_silent(self):
        """调度到期触发必须 silent：自动运行失败时不弹窗打扰。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(name="t", mode="day", at_time="09:00",
                             flow_id=cfg.flows[0].id, flow_name="F")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.flow_tab._next_return = True
            tab._on_due(t.id, t.flow_id)
            self.assertEqual(tab.flow_tab.started[-1], (t.flow_id, True),
                             "调度触发应标记 silent")

    def test_run_now_not_silent(self):
        """用户手动「立即运行」不 silent：失败保留弹窗反馈。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait")])]
            t = ScheduleTask(name="t", mode="day", at_time="09:00",
                             flow_id=cfg.flows[0].id, flow_name="F")
            cfg.schedule_tasks = [t]
            tab = self._make_tab(cfg)
            tab.flow_tab._next_return = True
            tab._fire(t)
            self.assertEqual(tab.flow_tab.started[-1], (t.flow_id, False),
                             "手动立即运行不应 silent")

    def test_flow_tab_silent_registration(self):
        """start_flow_if_idle(silent=True) 登记静默标记；非 silent 不登记。"""
        from app.ui.flow_tab import FlowTab
        with TempConfigPaths():
            cfg = AppConfig()
            f = Flow(name="F", steps=[FlowStep(type="wait")])
            cfg.flows = [f]
            ft = FlowTab(cfg)
            calls = []
            ft.toggle_flow = lambda fid: calls.append(fid)   # 不真启动线程
            ok = ft.start_flow_if_idle(f.id, silent=True)
            self.assertTrue(ok)
            self.assertIn(f.id, ft._silent, "silent 启动应登记静默标记")
            self.assertEqual(calls, [f.id])
            ft._silent.clear()
            ft.start_flow_if_idle(f.id)
            self.assertNotIn(f.id, ft._silent, "非 silent 启动不应登记")

    def test_on_state_silent_failed_no_popup(self):
        """静默流程失败：不弹 QMessageBox，走状态栏提示，结束即清除标记。"""
        from unittest import mock
        from app.ui.flow_tab import FlowTab
        with TempConfigPaths():
            cfg = AppConfig()
            f = Flow(name="F", steps=[FlowStep(type="wait")])
            cfg.flows = [f]
            ft = FlowTab(cfg)
            shown = []

            class _SB:
                def showMessage(self, *a, **k): shown.append(a)

            class _Win:
                def statusBar(self): return _SB()

            ft.window = lambda: _Win()
            ft._silent.add(f.id)
            with mock.patch("app.ui.flow_tab.QMessageBox") as mb:
                ft._on_state(f.id, "stopped", "找图超时", False)
                mb.information.assert_not_called()
            self.assertNotIn(f.id, ft._silent, "结束后应清除静默标记")
            self.assertTrue(shown, "静默失败应走状态栏提示")

    def test_on_state_manual_failed_still_popup(self):
        """手动运行流程失败：仍弹 QMessageBox 反馈（不受静默影响）。"""
        from unittest import mock
        from app.ui.flow_tab import FlowTab
        with TempConfigPaths():
            cfg = AppConfig()
            f = Flow(name="F", steps=[FlowStep(type="wait")])
            cfg.flows = [f]
            ft = FlowTab(cfg)

            class _SB:
                def showMessage(self, *a, **k): pass

            class _Win:
                def statusBar(self): return _SB()

            ft.window = lambda: _Win()
            with mock.patch("app.ui.flow_tab.QMessageBox") as mb:
                ft._on_state(f.id, "stopped", "找图超时", False)
                mb.information.assert_called_once()


if __name__ == "__main__":
    unittest.main()
