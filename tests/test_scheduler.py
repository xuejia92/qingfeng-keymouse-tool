"""app/scheduler.py 的纯计算逻辑测试：cron 解析、下次运行时间、预览、描述。"""
from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
