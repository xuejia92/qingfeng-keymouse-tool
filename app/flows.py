"""自动化流程执行引擎：按步骤顺序执行，支持循环轮数与失败策略。

FlowRunner 运行在独立线程，逐步骤调用 tasks.py 的公共执行函数；
信号 stepStarted / stepFinished / stepProgress / stateChanged 驱动 UI。
失败策略：找图超时等步骤失败时，默认终止整个流程；步骤勾选
continue_on_fail 时跳过继续。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from .config import FLOW_STEP_TYPES, Flow
from .logbus import log
from .tasks import (run_app_step, run_click_step, run_close_app_step,
                    run_find_step, run_log_step, run_ocr_step, run_press_step,
                    run_var_step, run_web_step)


class FlowVariableStore:
    """流程运行期变量容器。

    每次流程启动时按变量声明初始化；同一轮/多轮循环之间保持值，
    直到流程结束。变量步骤可创建/覆盖变量。
    """

    def __init__(self, flow: Flow):
        self._values: dict = {}
        self._types: dict = {}
        self._init_from_decl(flow)

    def _init_from_decl(self, flow: Flow) -> None:
        for v in getattr(flow, "variables", []) or []:
            name = (v.name or "").strip()
            if not name:
                continue
            try:
                self._values[name] = v.parse_value()
            except Exception:
                self._values[name] = None
            self._types[name] = v.type

    @property
    def values(self) -> dict:
        return self._values

    @property
    def types(self) -> dict:
        return self._types


class FlowRunner(QObject):
    stepStarted = Signal(int, str)         # 步骤索引, 步骤名
    stepFinished = Signal(int, bool, str)  # 步骤索引, 是否成功, 原因
    stepProgress = Signal(int, int, float)  # 步骤索引, 已执行次数, 已用时秒
    stateChanged = Signal(str, str, bool)  # ("running"/"stopped", 结束原因, 是否正常)

    def __init__(self, flow: Flow):
        super().__init__()
        self.flow = flow
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_running = False
        self.current_step_index = -1  # 运行中当前步骤（-1=未开始/轮次间）
        self.last_step_ok = True      # 最近一次执行的步骤结果（单步执行判定成败用）
        self.last_step_reason = ""

    def start(self) -> bool:
        if self.is_running or not self.flow.steps:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"流程:{self.flow.name}")
        self.is_running = True
        self.stateChanged.emit("running", "", True)
        loops = "无限循环" if self.flow.loops == 0 else f"{self.flow.loops} 轮"
        log(f"流程「{self.flow.name}」开始运行（{len(self.flow.steps)} 步 · {loops}）")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # ---------- 执行 ----------
    def _run(self) -> None:
        try:
            reason = self._run_loops()
        except Exception as e:  # 保护线程不静默死亡
            reason = f"出错: {e}"
        self.is_running = False
        ok = reason == "已手动停止" or reason.startswith("已完成")
        self.stateChanged.emit("stopped", reason, ok)
        log(f"流程「{self.flow.name}」结束：{reason}")

    def _run_loops(self) -> str:
        self.vars = FlowVariableStore(self.flow)
        loops = self.flow.loops if self.flow.loops > 0 else -1  # -1 = 无限
        loop_no = 0
        while loops < 0 or loop_no < loops:
            if self._stop.is_set():
                return "已手动停止"
            loop_no += 1
            if self.flow.loops != 1:
                self.stepStarted.emit(-1, f"第 {loop_no} 轮"
                                      + (f"/{self.flow.loops}" if self.flow.loops > 0 else ""))
            reason = self._run_once()
            if reason is not None:
                return reason
        return f"已完成 {self.flow.loops} 轮"

    def _run_once(self) -> str | None:
        """执行一轮全部步骤。返回 None 表示整轮成功，否则为流程结束原因。"""
        vars = getattr(self, "vars", FlowVariableStore(self.flow))
        for idx, step in enumerate(self.flow.steps):
            if self._stop.is_set():
                return "已手动停止"
            self.current_step_index = idx
            self.stepStarted.emit(idx, step.name)
            log(f"流程「{self.flow.name}」步骤 {idx + 1}/{len(self.flow.steps)}：{step.name}")
            ok, why = self._exec_step(idx, step, vars)
            self.last_step_ok, self.last_step_reason = ok, why
            self.stepFinished.emit(idx, ok, why)
            if not ok:
                log(f"步骤 {idx + 1} 未成功：{why}")
            if self._stop.is_set():
                return "已手动停止"  # 手动停止优先于失败判定，不弹失败提示
            if not ok:
                if step.continue_on_fail:
                    continue
                return f"第 {idx + 1} 步「{step.name}」失败：{why}"
        self.current_step_index = -1
        return None

    def _exec_step(self, idx: int, step, vars) -> tuple[bool, str]:
        """执行单个步骤，返回 (成功?, 原因)。"""
        progress = lambda done, elapsed: self.stepProgress.emit(idx, done, elapsed)
        if step.type == "var":
            return run_var_step(step.params, vars.values, vars.types)
        elif step.type == "log":
            return run_log_step(step.params, vars.values)
        elif step.type == "ocr":
            return run_ocr_step(step.params, vars.values, self._stop)
        if step.type == "click":
            reason = run_click_step(step.params, self._stop, progress)
        elif step.type == "press":
            reason = run_press_step(step.params, self._stop, progress)
        elif step.type == "find":
            reason = run_find_step(step.params, self._stop, progress)
        elif step.type == "wait":
            seconds = float(step.params.get("seconds", 1) or 0)
            self._stop.wait(max(seconds, 0.0))
            reason = "已手动停止" if self._stop.is_set() else "等待完成"
        elif step.type == "web":
            return run_web_step(step.params, self._stop)  # 自带成败判定
        elif step.type == "app":
            return run_app_step(step.params, self._stop)  # 自带成败判定
        elif step.type == "close_app":
            return run_close_app_step(step.params, self._stop)  # 自带成败判定
        else:
            reason = f"未知步骤类型: {step.type}（应为 {'/'.join(FLOW_STEP_TYPES)}）"
        ok = reason not in ("已手动停止",) and not reason.startswith(("等待目标超时", "模板图", "未设置", "按键无效", "出错"))
        return ok, reason
