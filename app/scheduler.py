"""定时任务调度引擎：cron 解析、下次运行时间计算、预览与后台调度线程。

纯计算部分（parse_cron / cron_next / next_run_time / next_run_times /
describe_schedule）不依赖 Qt，便于单元测试；ScheduleRunner 在独立线程轮询，
到期后通过 Qt 信号（跨线程自动排队）通知主线程触发流程。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal

from .config import WEEKDAY_NAMES, ScheduleTask

# 时间字符串格式（last_run / next_run 持久化用）
TIME_FMT = "%Y-%m-%d %H:%M:%S"

_MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _floor_sec(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _token_int(tok: str, names: dict | None) -> int:
    tok = tok.strip().lower()
    if names and tok in names:
        return names[tok]
    return int(tok)


def _parse_cron_field(field: str, lo: int, hi: int, names: dict | None = None) -> set:
    """解析单个 cron 字段（支持 * / */n / a-b / a-b/n / 逗号列表 / 名称），返回取值集合。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip().lower()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
        if step <= 0:
            raise ValueError(f"cron 步长需大于 0：{part}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _token_int(a, names), _token_int(b, names)
        else:
            start = end = _token_int(base, names)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron 字段取值越界：{part}")
        for v in range(start, end + 1, step):
            values.add(v)
    if not values:
        raise ValueError("cron 字段为空")
    return values


def parse_cron(expr: str) -> dict | None:
    """解析 cron 表达式（5 段「分 时 日 月 周」或 6 段「秒 分 时 日 月 周」）。

    返回字段取值集合与元信息；非法表达式返回 None。
    """
    expr = (expr or "").strip()
    if not expr:
        return None
    parts = expr.split()
    if len(parts) not in (5, 6):
        return None
    if len(parts) == 6:
        sec_s, min_s, hour_s, dom_s, month_s, dow_s = parts
    else:
        sec_s = "0"
        min_s, hour_s, dom_s, month_s, dow_s = parts
    try:
        return {
            "has_sec": len(parts) == 6,
            "sec": _parse_cron_field(sec_s, 0, 59),
            "min": _parse_cron_field(min_s, 0, 59),
            "hour": _parse_cron_field(hour_s, 0, 23),
            "dom": _parse_cron_field(dom_s, 1, 31),
            "month": _parse_cron_field(month_s, 1, 12, _MONTH_NAMES),
            "dow": _parse_cron_field(dow_s, 0, 7, _DOW_NAMES),
            "dom_raw": dom_s,
            "dow_raw": dow_s,
        }
    except (ValueError, IndexError):
        return None


def _dow_matches(dt: datetime, dow_set: set) -> bool:
    """cron 星期匹配：0/7 都表示周日（ISO 7）。"""
    iso = dt.isoweekday()
    if iso == 7:
        return 0 in dow_set or 7 in dow_set
    return iso in dow_set


def _day_matches(f: dict, day: datetime) -> bool:
    dom_restricted = f["dom_raw"] != "*"
    dow_restricted = f["dow_raw"] != "*"
    if dom_restricted and dow_restricted:
        return day.day in f["dom"] or _dow_matches(day, f["dow"])
    if dom_restricted:
        return day.day in f["dom"]
    if dow_restricted:
        return _dow_matches(day, f["dow"])
    return True


def cron_next(expr: str, after: datetime) -> datetime | None:
    """返回严格晚于 after 的下一次 cron 触发时间；表达式非法或无匹配返回 None。"""
    f = parse_cron(expr)
    if f is None:
        return None
    after = _floor_sec(after)
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(366 * 6):   # 6 年上限，避免死循环
        if day.month in f["month"] and _day_matches(f, day):
            for h in sorted(f["hour"]):
                for m in sorted(f["min"]):
                    if f["has_sec"]:
                        for s in sorted(f["sec"]):
                            t = day.replace(hour=h, minute=m, second=s)
                            if t > after:
                                return t
                    else:
                        t = day.replace(hour=h, minute=m, second=0)
                        if t > after:
                            return t
        day += timedelta(days=1)
    return None


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    try:
        h, m = str(s or "").split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except (ValueError, AttributeError):
        return None


def _parse_once(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _at_time(task: ScheduleTask) -> tuple[int, int]:
    p = _parse_hhmm(task.at_time)
    return p if p else (9, 0)


def next_run_time(task: ScheduleTask, after: datetime) -> datetime | None:
    """返回严格晚于 after 的下一次触发时间；无法计算（如一次性任务已过期）返回 None。"""
    after = _floor_sec(after)
    mode = task.mode
    if mode == "second":
        return after + timedelta(seconds=max(1, int(task.interval or 1)))
    if mode == "minute":
        n = max(1, int(task.interval or 1))
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while t.minute % n != 0:
            t += timedelta(minutes=1)
        return t
    if mode == "hour":
        n = max(1, int(task.interval or 1))
        t = after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while t.hour % n != 0:
            t += timedelta(hours=1)
        return t
    if mode == "day":
        hh, mm = _at_time(task)
        n = max(1, int(task.interval or 1))
        t = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= after:
            t += timedelta(days=1)
        anchor = datetime(2020, 1, 1)
        while (t.date() - anchor.date()).days % n != 0:
            t += timedelta(days=1)
        return t
    if mode == "week":
        hh, mm = _at_time(task)
        days = [d for d in (task.weekdays or []) if 1 <= d <= 7]
        if not days:
            return None
        t = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for _ in range(8):
            if t.isoweekday() in days and t > after:
                return t
            t += timedelta(days=1)
        return None
    if mode == "month":
        hh, mm = _at_time(task)
        mdays = [d for d in (task.monthdays or []) if 1 <= d <= 31]
        if not mdays:
            return None
        t = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for _ in range(33):
            if t.day in mdays and t > after:
                return t
            t += timedelta(days=1)
        return None
    if mode == "once":
        dt = _parse_once(task.once_at)
        return dt if dt is not None and dt > after else None
    if mode == "cron":
        return cron_next(task.cron, after)
    return None


def next_run_times(task: ScheduleTask, after: datetime, n: int = 5) -> list[datetime]:
    """预览接下来 n 次运行时间（不足 n 次时返回实际数量）。"""
    out: list[datetime] = []
    t = after
    for _ in range(n):
        t = next_run_time(task, t)
        if t is None:
            break
        out.append(t)
    return out


def describe_schedule(task: ScheduleTask) -> str:
    """把调度规则转成一句话描述，供列表/详情展示。"""
    mode = task.mode
    if mode == "second":
        return f"每 {max(1, int(task.interval or 1))} 秒"
    if mode == "minute":
        return f"每 {max(1, int(task.interval or 1))} 分钟"
    if mode == "hour":
        return f"每 {max(1, int(task.interval or 1))} 小时"
    if mode == "day":
        n = max(1, int(task.interval or 1))
        s = "每天" if n == 1 else f"每 {n} 天"
        return f"{s} {task.at_time or '09:00'}"
    if mode == "week":
        days = [WEEKDAY_NAMES[d] for d in (task.weekdays or []) if 1 <= d <= 7]
        ds = "、".join(days) if days else "未选星期"
        return f"每周 {ds} {task.at_time or '09:00'}"
    if mode == "month":
        mdays = [d for d in (task.monthdays or []) if 1 <= d <= 31]
        ds = "、".join(f"{d}日" for d in mdays) if mdays else "未选日期"
        return f"每月 {ds} {task.at_time or '09:00'}"
    if mode == "once":
        return f"指定时间 {task.once_at or '未设置'}"
    if mode == "cron":
        return f"Cron {task.cron}"
    return "未设置"


def format_dt(dt: datetime | None) -> str:
    return dt.strftime(TIME_FMT) if dt else ""


class ScheduleRunner(QObject):
    """后台调度线程：每 0.5 秒轮询一次启用的任务，到期发射 due 信号。

    通过 Qt 信号（自动排队到主线程）通知触发，避免在非主线程操作控件。
    任务改动通过「规则签名」比对自动感知，无需显式失效通知。
    """

    due = Signal(str, str)   # (task_id, flow_id)

    def __init__(self, tasks_provider, parent=None):
        super().__init__(parent)
        self._provider = tasks_provider     # callable -> list[ScheduleTask]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, tuple[tuple, datetime | None]] = {}

    @staticmethod
    def _signature(task: ScheduleTask) -> tuple:
        return (task.mode, task.cron, task.interval, task.at_time,
                tuple(task.weekdays or []), tuple(task.monthdays or []),
                task.once_at)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="定时调度")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def invalidate(self, task_id: str) -> None:
        self._state.pop(task_id, None)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logging.getLogger(__name__).warning("定时调度循环异常", exc_info=True)
            self._stop.wait(0.5)

    def _tick(self) -> None:
        now = datetime.now()
        # 快照当前任务列表，避免主线程增删任务时迭代到半截的列表
        for task in list(self._provider()):
            if not task.enabled:
                self._state.pop(task.id, None)
                continue
            sig = self._signature(task)
            cached = self._state.get(task.id)
            if cached is None or cached[0] != sig:
                self._state[task.id] = (sig, next_run_time(task, now))
                continue
            nt = cached[1]
            if nt is not None and nt <= now:
                self.due.emit(task.id, task.flow_id)
                self._state[task.id] = (sig, next_run_time(task, now))
