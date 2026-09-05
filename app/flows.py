"""自动化流程执行引擎：按步骤顺序执行，支持循环轮数与失败策略。

FlowRunner 运行在独立线程，逐步骤调用 tasks.py 的公共执行函数；
信号 stepStarted / stepFinished / stepProgress / stateChanged 驱动 UI。
失败策略：找图超时等步骤失败时，默认终止整个流程；步骤勾选
continue_on_fail 时跳过继续。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from .conditions import (MAX_WHILE_ITERATIONS, build_control_flow,
                         build_loop_flow, eval_condition)
from .config import FLOW_STEP_TYPES, Flow
from .logbus import log, log_print
from .tasks import (run_app_step, run_click_step, run_clip_get_step,
                    run_clip_set_step, run_close_app_step, run_color_pick_step,
                    run_deepseek_step,
                    run_dp_browser_step, run_dp_close_browser_step,
                    run_dp_ele_shot_step,
                    run_dp_element_step, run_dp_listen_step,
                    run_dp_page_shot_step, run_dp_tab_step, run_dp_upload_step,
                    run_find_image_step,
                    run_find_step, run_http_request_step, run_log_step,
                    run_notify_step,
                    run_ocr_step, run_press_step,
                    run_py_func_step, run_script_step, run_screenshot_step,
                    run_speech_step,
                    run_text_find_step,
                    run_var_step, run_web_step, run_yolo_detect_step)
from .values import eval_expression_value, format_value, resolve_variable


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
        self._last_condition_truthy = False  # 最近一次 if/elseif 条件的真假

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
        ok = (reason == "已手动停止" or reason.startswith("已完成")
              or reason.startswith("已退出流程"))
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
        """执行一轮全部步骤（含 if 分支跳转与 foreach/while 循环）。

        返回 None 表示整轮成功，否则为流程结束原因。分支语义与编程语言一致：
        - if / elseif 条件成立则进入其后的步骤体，否则跳到下一个分支头；
        - else 表示「上方条件都不成立」时进入；endif 为结构标记，不执行不判成败。
        用 false_jump / block_end 两张表 + pending_ends 栈（记录已命中分支的块结束
        位置）实现嵌套条件块的正确定向与整块跳过。

        循环语义（loop_stack 栈，与 pending_ends 正交）：
        - foreach：对 items 每个元素写 item_var/index_var 后执行循环体，空列表跳过；
        - while：进入前求值条件，不成立跳过整块；到达 endWhile 无条件跳回 while 重求值，
          靠 iterations 计数做死循环保护；
        - endForeach/endWhile 为结构标记，不执行不判成败。
        """
        vars = getattr(self, "vars", FlowVariableStore(self.flow))
        steps = self.flow.steps
        false_jump, block_end = build_control_flow(steps)
        loop_ends = build_loop_flow(steps)   # {foreach/while 索引: 结束标记索引}
        n = len(steps)
        idx = 0
        pending_ends: list[int] = []   # 已命中分支的块结束索引（栈，内层在后）
        loop_stack: list[dict] = []    # 循环帧栈（foreach/while，内层在后）
        arrived_by_jump = False        # 当前步骤是否经「假跳转」落到分支头
        while idx < n:
            if self._stop.is_set():
                return "已手动停止"
            step = steps[idx]
            self.current_step_index = idx
            t = step.type

            # 被注释的步骤：运行期跳过不执行。idx 线性推进、不重算控制流表，
            # 结构标记的配对关系不受影响（注释整块/单步都能正确跳过）。
            if step.commented:
                idx += 1
                continue

            # 线性到达 elseif/else/endif：说明上一分支已命中，跳过整块剩余
            if not arrived_by_jump and t in ("elseif", "else", "endif") and pending_ends:
                idx = pending_ends.pop()
                continue

            if t in ("if", "elseif"):
                self.stepStarted.emit(idx, step.name)
                ok, why = self._eval_condition_step(step, vars)
                self.last_step_ok, self.last_step_reason = ok, why
                self.stepFinished.emit(idx, ok, why)
                log(f"流程「{self.flow.name}」步骤 {idx + 1}/{n}：{step.name} · {why}")
                if not ok:
                    if self._stop.is_set():
                        return "已手动停止"
                    return f"第 {idx + 1} 步「{step.name}」失败：{why}"
                if self._last_condition_truthy:
                    pending_ends.append(block_end.get(idx, n))
                    idx += 1
                    arrived_by_jump = False
                else:
                    target = false_jump.get(idx, n)
                    # 跳到 endif+1（=block_end）表示「整块结束」，应视为线性到达；
                    # 跳到下一个分支头（elseif/else）才是需要求值的「分支跳转」。
                    arrived_by_jump = (target != block_end.get(idx, n))
                    idx = target
                continue

            if t == "else":
                # 经假跳转到达（上方条件都不成立）→ 进入 else 分支体
                pending_ends.append(block_end.get(idx, n))
                idx += 1
                arrived_by_jump = False
                continue

            if t == "endif":
                # 经假跳转落到 endif（无 else）→ 直接跳过整块
                idx += 1
                arrived_by_jump = False
                continue

            if t == "while":
                # 进入前求值一次条件：不成立跳过整块，成立进入循环体
                end = loop_ends.get(idx, n)
                self.stepStarted.emit(idx, step.name)
                ok, why = self._eval_condition_step(step, vars)
                self.last_step_ok, self.last_step_reason = ok, why
                self.stepFinished.emit(idx, ok, why)
                log(f"流程「{self.flow.name}」步骤 {idx + 1}/{n}：{step.name} · {why}")
                if not ok:
                    if self._stop.is_set():
                        return "已手动停止"
                    return f"第 {idx + 1} 步「{step.name}」失败：{why}"
                if self._last_condition_truthy:
                    # 只压一次帧：后续迭代由 endWhile 重新求值后跳回 body_start，
                    # 不再次经过本 while 步骤，避免反复压帧导致栈无限增长。
                    loop_stack.append({"type": "while", "while_idx": idx,
                                       "body_start": idx + 1, "end": end,
                                       "iterations": 0})
                    idx += 1
                else:
                    idx = end + 1
                arrived_by_jump = False
                continue

            if t == "foreach":
                # 取数据源列表；空列表直接跳过，否则写首项变量后进入循环体
                end = loop_ends.get(idx, n)
                self.stepStarted.emit(idx, step.name)
                ok, why, items_list, keys_list = self._prepare_foreach(step, vars)
                self.last_step_ok, self.last_step_reason = ok, why
                self.stepFinished.emit(idx, ok, why)
                log(f"流程「{self.flow.name}」步骤 {idx + 1}/{n}：{step.name} · {why}")
                if not ok:
                    if self._stop.is_set():
                        return "已手动停止"
                    return f"第 {idx + 1} 步「{step.name}」失败：{why}"
                if not items_list:
                    idx = end + 1
                else:
                    item_var = (step.params.get("item_var") or "").strip() or "item"
                    index_var = (step.params.get("index_var") or "").strip() or "index"
                    loop_stack.append({"type": "foreach", "items": items_list,
                                       "keys": keys_list, "i": 0,
                                       "body_start": idx + 1, "end": end,
                                       "item_var": item_var, "index_var": index_var})
                    self._write_foreach_vars(vars, loop_stack[-1])
                    idx += 1
                arrived_by_jump = False
                continue

            if t == "endForeach":
                # 下一项未越界则回 body_start，越界弹栈结束循环
                if loop_stack and loop_stack[-1]["type"] == "foreach":
                    frame = loop_stack[-1]
                    frame["i"] += 1
                    if frame["i"] < len(frame["items"]):
                        self._write_foreach_vars(vars, frame)
                        idx = frame["body_start"]
                    else:
                        loop_stack.pop()
                        idx += 1
                else:
                    idx += 1   # 孤儿 endForeach：跳过
                arrived_by_jump = False
                continue

            if t == "endWhile":
                # 迭代计数 +1，超限报错；否则重新求值条件：成立回 body_start，不成立弹栈退出
                if loop_stack and loop_stack[-1]["type"] == "while":
                    frame = loop_stack[-1]
                    frame["iterations"] += 1
                    if frame["iterations"] >= MAX_WHILE_ITERATIONS:
                        return (f"第 {idx + 1} 步「{step.name}」超过 "
                                f"{MAX_WHILE_ITERATIONS} 次迭代，疑似死循环，已终止")
                    while_step = steps[frame["while_idx"]]
                    ok, why = self._eval_condition_step(while_step, vars)
                    if not ok:
                        return (f"第 {frame['while_idx'] + 1} 步"
                                f"「{while_step.name}」失败：{why}")
                    if self._last_condition_truthy:
                        idx = frame["body_start"]
                    else:
                        loop_stack.pop()
                        idx = frame["end"] + 1
                else:
                    idx += 1   # 孤儿 endWhile：跳过
                arrived_by_jump = False
                continue

            if t == "break":
                # 立即跳出最内层循环：弹掉栈顶循环帧，跳到该循环结束标记之后。
                # loop_stack 只装 foreach/while 帧，if 不入栈，因此 break 天然只针对循环。
                if loop_stack:
                    frame = loop_stack.pop()
                    idx = frame["end"] + 1
                else:
                    return f"第 {idx + 1} 步「{step.name}」不在任何循环体内"
                arrived_by_jump = False
                continue

            if t == "continue":
                # 跳到最内层循环的结束标记，由 endForeach/endWhile 处理下一次迭代。
                if loop_stack:
                    idx = loop_stack[-1]["end"]
                else:
                    return f"第 {idx + 1} 步「{step.name}」不在任何循环体内"
                arrived_by_jump = False
                continue

            if t == "exit":
                # 立即终止整个流程：可选打印某变量值后返回结束原因（不再执行后续步骤）。
                self.stepStarted.emit(idx, step.name)
                var = (step.params.get("variable") or "").strip()
                if var:
                    ok, val, why = resolve_variable(var, vars.values)
                    if ok:
                        log_print(f"退出流程：变量 {var} = {format_value(val)}")
                    else:
                        log_print(f"退出流程：变量「{var}」{why}")
                log(f"流程「{self.flow.name}」在第 {idx + 1} 步「{step.name}」处退出")
                self.last_step_ok, self.last_step_reason = True, "已退出流程"
                self.stepFinished.emit(idx, True, "已退出流程")
                return "已退出流程"

            self.stepStarted.emit(idx, step.name)
            log(f"流程「{self.flow.name}」步骤 {idx + 1}/{n}：{step.name}")
            ok, why = self._exec_step(idx, step, vars)
            self.last_step_ok, self.last_step_reason = ok, why
            self.stepFinished.emit(idx, ok, why)
            if not ok:
                log(f"步骤 {idx + 1} 未成功：{why}")
            if self._stop.is_set():
                return "已手动停止"  # 手动停止优先于失败判定，不弹失败提示
            if not ok:
                if step.continue_on_fail:
                    idx += 1
                    continue
                return f"第 {idx + 1} 步「{step.name}」失败：{why}"
            idx += 1
        self.current_step_index = -1
        return None

    @staticmethod
    def _prepare_foreach(step, vars) -> tuple[bool, str, list, list | None]:
        """准备 foreach 数据源：求值表达式并归一化，返回 (成功?, 说明, 元素列表, 键列表)。

        元素列表写 item_var，键列表写 index_var（列表/字符串键列表为 None，表示用
        数字下标）。字典按「值→item、键→index」遍历；列表/元组/字符串按元素遍历。
        数据源支持表达式：裸变量名 arr、下标 arr[0]、$引用与函数
        （range(0, 3)、slice(0, $k)、sorted($arr) 等，见 values.eval_expression_value）。
        """
        items_text = (step.params.get("items") or "").strip()
        if not items_text:
            return False, "未填写数据源", [], None
        ok, val, why = eval_expression_value(items_text, vars.values)
        if not ok:
            return False, why, [], None
        if isinstance(val, dict):
            items_list = list(val.values())
            keys_list = list(val.keys())
        elif isinstance(val, str):
            items_list = list(val)
            keys_list = None
        elif isinstance(val, slice):
            return False, (f"数据源结果是 slice 切片对象，不能直接遍历；"
                           f"可配合下标切片如 $arr[slice(开始,结束,步长)]，"
                           f"或改用 range(开始,结束,步长)"), [], None
        elif hasattr(val, "__iter__"):
            # list/tuple/range/enumerate/zip/set… 统一转列表
            try:
                items_list = list(val)
            except Exception as e:
                return False, f"数据源无法转为列表：{type(e).__name__}: {e}", [], None
            keys_list = None
        else:
            return False, (f"数据源「{items_text}」不可遍历"
                           f"（类型 {type(val).__name__}）"), [], None
        return True, f"遍历 {items_text}（{len(items_list)} 项）", items_list, keys_list

    @staticmethod
    def _write_foreach_vars(vars, frame: dict) -> None:
        """把当前遍历元素/下标写入运行时变量。

        字典：item_var = 值，index_var = 键；列表/字符串：item_var = 元素，
        index_var = 数字下标（0 起）。
        """
        element = frame["items"][frame["i"]]
        keys = frame.get("keys")
        index_value = keys[frame["i"]] if keys is not None else frame["i"]
        item_var = frame.get("item_var") or ""
        index_var = frame.get("index_var") or ""
        if item_var:
            vars.values[item_var] = element
            vars.types[item_var] = FlowRunner._python_type_name(element)
        if index_var:
            vars.values[index_var] = index_value
            vars.types[index_var] = FlowRunner._python_type_name(index_value)

    @staticmethod
    def _python_type_name(value) -> str:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "float"
        if isinstance(value, (list, tuple)):
            return "list"
        if isinstance(value, dict):
            return "dict"
        return "string"

    def _eval_condition_step(self, step, vars) -> tuple[bool, str]:
        """求值 if/elseif 条件，返回 (成功?, 说明)；真实结果写入 _last_condition_truthy。"""
        expr = (step.params.get("condition") or "").strip()
        self._last_condition_truthy = False
        if not expr:
            return False, "条件表达式为空"
        ok, result, why = eval_condition(expr, vars.values)
        if not ok:
            return False, why
        self._last_condition_truthy = bool(result)
        return True, why

    def _exec_step(self, idx: int, step, vars) -> tuple[bool, str]:
        """执行单个步骤，返回 (成功?, 原因)。"""
        progress = lambda done, elapsed: self.stepProgress.emit(idx, done, elapsed)
        if step.type == "var":
            return run_var_step(step.params, vars.values, vars.types)
        elif step.type == "log":
            return run_log_step(step.params, vars.values)
        elif step.type == "ocr":
            return run_ocr_step(step.params, vars.values, self._stop)
        elif step.type == "text_find":
            return run_text_find_step(step.params, vars.values, self._stop)
        elif step.type == "clip_set":
            return run_clip_set_step(step.params, vars.values)
        elif step.type == "clip_get":
            return run_clip_get_step(step.params, vars.values, vars.types)
        elif step.type == "screenshot":
            return run_screenshot_step(step.params, vars.values, self._stop)
        elif step.type == "speech":
            return run_speech_step(step.params, vars.values, self._stop)
        elif step.type == "find_image":
            return run_find_image_step(step.params, vars.values, self._stop)
        elif step.type == "yolo_detect":
            return run_yolo_detect_step(step.params, vars.values, vars.types,
                                        self._stop)
        elif step.type == "color_pick":
            return run_color_pick_step(step.params, vars.values, self._stop)
        elif step.type == "py_func":
            return run_py_func_step(step.params, vars.values, self._stop)
        elif step.type == "dp_browser":
            return run_dp_browser_step(step.params, vars.values, self._stop)
        elif step.type == "dp_element":
            return run_dp_element_step(step.params, vars.values, self._stop)
        elif step.type == "dp_tab":
            return run_dp_tab_step(step.params, vars.values, self._stop)
        elif step.type == "dp_listen":
            return run_dp_listen_step(step.params, vars.values, self._stop)
        elif step.type == "dp_page_shot":
            return run_dp_page_shot_step(step.params, vars.values, self._stop)
        elif step.type == "dp_ele_shot":
            return run_dp_ele_shot_step(step.params, vars.values, self._stop)
        elif step.type == "dp_upload":
            return run_dp_upload_step(step.params, vars.values, self._stop)
        elif step.type == "dp_close_browser":
            return run_dp_close_browser_step(step.params, vars.values, self._stop)
        if step.type == "click":
            reason = run_click_step(step.params, self._stop, progress, vars.values)
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
        elif step.type == "http_request":
            return run_http_request_step(step.params, vars.values, self._stop)
        elif step.type == "deepseek":
            return run_deepseek_step(step.params, vars.values, self._stop)
        elif step.type == "script":
            return run_script_step(step.params, vars.values, self._stop)
        elif step.type == "notify":
            return run_notify_step(step.params, vars.values, self._stop)
        elif step.type == "app":
            return run_app_step(step.params, self._stop)  # 自带成败判定
        elif step.type == "close_app":
            return run_close_app_step(step.params, self._stop)  # 自带成败判定
        else:
            reason = f"未知步骤类型: {step.type}（应为 {'/'.join(FLOW_STEP_TYPES)}）"
        ok = reason not in ("已手动停止",) and not reason.startswith(("等待目标超时", "模板图", "未设置", "按键无效", "出错"))
        return ok, reason
