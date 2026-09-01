"""后台任务线程与公共执行函数。

run_click_step / run_press_step / run_find_step / run_var_step / run_log_step /
run_ocr_step / run_clip_set_step / run_clip_get_step 是与 UI 无关的公共执行函数，
单任务（BaseTask 子类）与自动化流程（FlowRunner）共用同一套实现。
参数统一使用 dict（各字段与配置 dataclass 字段同名）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict

import pyperclip

from PySide6.QtCore import QObject, Signal

from . import finder, input_actors, web_actors, win_actors
from .config import parse_region_str, resolve_template_path
from .logbus import log

# 找图循环两次抓屏之间的间隔（秒）。
# 原来是固定 0.05（20 Hz），在 4K 屏上意味着每秒 20 次全屏 matchTemplate，
# 单核长期吃满、笔记本风扇狂转。改成按抓屏面积动态退避，见 grab_interval()。
_GRAB_INTERVAL_MIN = 0.05   # 小块区域：最快 20 Hz
_GRAB_INTERVAL_MAX = 0.20   # 4K 全屏：退到 5 Hz
SEARCH_GRAB_INTERVAL = _GRAB_INTERVAL_MIN   # 兼容旧引用：间隔下限

_screen_area_cache: int | None = None


def _virtual_screen_area() -> int:
    """虚拟桌面总像素数（物理像素）。屏幕配置很少变，缓存一次即可。"""
    global _screen_area_cache
    if _screen_area_cache is None:
        try:
            import mss
            with mss.mss() as sct:
                m = sct.monitors[0]
                _screen_area_cache = max(int(m["width"]) * int(m["height"]), 1)
        except Exception:
            _screen_area_cache = 1920 * 1080   # 取不到就按 1080p 估算
    return _screen_area_cache


def grab_interval(region: tuple | None = None) -> float:
    """按本次抓屏面积返回等待秒数：约每 100 万像素增加 0.05 秒。

    参考值：1MP → 0.05s(20Hz)、1080p 约 2MP → 0.10s(10Hz)、
    4K 约 8MP → 封顶 0.20s(5Hz)。
    找图命中会慢几十毫秒，但 CPU 从「吃满一个核」降到可接受水平。
    """
    if region is not None:
        try:
            area = max(int(region[2]) * int(region[3]), 1)
        except (TypeError, ValueError, IndexError):
            area = _virtual_screen_area()
    else:
        area = _virtual_screen_area()
    interval = _GRAB_INTERVAL_MIN * (area / 1_000_000.0)
    return min(_GRAB_INTERVAL_MAX, max(_GRAB_INTERVAL_MIN, interval))


def run_web_step(p: dict, stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行网页操作（DrissionPage）步骤，返回 (成功?, 原因)。

    网页步骤不像点击/连按那样有循环次数，成败由 web_actors 直接判定；
    失败原因五花八门（没填网址、浏览器起不来、导航超时…），塞进其它步骤
    共用的「按前缀识别失败」列表里反而脆弱，所以这里把 ok 一起返回。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    action = (p.get("action") or "open").strip()
    if action == "open":
        ok, why = web_actors.open_url(
            url=p.get("url", ""),
            mode=p.get("launch_mode") or web_actors.DEFAULT_MODE,
            new_tab=p.get("tab_target") == "new",
            timeout=float(p.get("load_timeout_sec") or 20),
            wait_after=float(p.get("wait_after_sec") or 0),
        )
    elif action == "close_tab":
        ok, why = web_actors.close_tab(
            scope=p.get("tab_scope") or "current",
            match_text=p.get("match_text", ""),
        )
    elif action == "close_browser":
        closed = web_actors.close_browser()
        ok, why = True, ("浏览器已关闭" if closed else "浏览器未启动，无需关闭")
    else:
        ok, why = False, f"未知的网页动作: {action}"
    if ok and stop is not None and stop.is_set():
        return False, "已手动停止"
    return ok, why


def _limit_reason(stop: threading.Event, done: int, t0: float,
                  max_count: int, duration: float) -> str | None:
    """通用停止条件判定（次数 0=无限、时长 0=不限，任一满足即停）。"""
    if stop.is_set():
        return "已手动停止"
    if max_count > 0 and done >= max_count:
        return f"已完成 {done} 次"
    if duration > 0 and time.monotonic() - t0 >= duration:
        return "已到设定时长"
    return None


def run_app_step(p: dict, stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「打开应用」步骤：启动本地程序，按需等待若干秒让其加载完成。

    返回 (成功?, 原因)。启动失败视为失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    ok, why = win_actors.launch_app(p.get("path", ""))
    if not ok:
        return False, why
    wait_sec = float(p.get("wait_sec", 0) or 0)
    if wait_sec > 0 and (stop is None or not stop.wait(wait_sec)):
        pass                      # 等待期间若被停止，下面统一判定
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    return True, why


def run_close_app_step(p: dict, stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「关闭应用」步骤：按进程名结束应用。返回 (成功?, 原因)。

    process 是运行时用的进程名（如 chrome.exe）；旧数据/手动输入只有
    target 时回退到 target。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    process = (p.get("process") or p.get("target") or "").strip()
    ok, why = win_actors.close_app(process)
    if not ok:
        return False, why
    wait_sec = float(p.get("wait_sec", 0) or 0)
    if wait_sec > 0 and stop is not None:
        stop.wait(wait_sec)
    return True, why


def run_var_step(p: dict, variables: dict, variable_types: dict | None = None) -> tuple[bool, str]:
    """执行「变量」步骤：把默认值按类型解析后写入运行时变量。

    variables: 运行时变量存储 dict；variable_types: 变量名 -> 类型。
    变量步骤相当于声明并赋值，遇到同名变量会覆盖。
    """
    from .values import parse_value
    name = (p.get("name") or "").strip()
    if not name:
        return False, "变量名为空"
    value_type = (p.get("type") or "string").strip().lower()
    default = p.get("default_value", "")
    try:
        value = parse_value(value_type, str(default or ""))
    except ValueError as e:
        return False, str(e)
    variables[name] = value
    if variable_types is not None:
        variable_types[name] = value_type
    return True, f"已设置变量 {name}"


def run_log_step(p: dict, variables: dict) -> tuple[bool, str]:
    """执行「日志输出」步骤：把指定变量/全部变量输出到日志控制台。

    variables 参数指定要输出的变量名（逗号分隔）；空表示输出全部变量。
    text 支持 $变量名 占位。
    """
    from .values import format_value, resolve_references
    names = [x.strip() for x in (p.get("variables") or "").split(",") if x.strip()]
    if not names and (p.get("text") or "").strip():
        # 只填了 text：把它当作普通日志输出
        text = resolve_references(str(p.get("text") or ""), variables)
        log(text)
        return True, "日志已输出"
    if not names:
        names = list(variables.keys())
    if not names:
        return False, "没有可输出的变量"
    lines = []
    for name in names:
        if name in variables:
            lines.append(f"{name} = {format_value(variables[name])}")
        else:
            lines.append(f"{name} = <未定义>")
    text = resolve_references(str(p.get("text") or ""), variables)
    if text:
        lines.insert(0, text)
    log("\n".join(lines))
    return True, f"已输出 {len(names)} 个变量"


def run_clip_set_step(p: dict, variables: dict) -> tuple[bool, str]:
    """执行「赋值剪贴板」步骤：把变量值或自定义文本写入系统剪贴板。

    两种来源二选一（变量优先）：
      - name 指定变量：取 format_value(变量值)；
      - 否则用 text 自定义文本，支持 $变量名 引用。
    """
    from .values import format_value, resolve_references
    name = (p.get("name") or "").strip()
    text = (p.get("text") or "").strip()
    if name:
        if name not in variables:
            return False, f"变量「{name}」未定义"
        value = format_value(variables[name])
        source = f"变量 {name}"
    elif text:
        value = resolve_references(text, variables)
        source = "自定义文本"
    else:
        return False, "请选择变量或填写文本"
    try:
        pyperclip.copy(value)
    except Exception as e:
        return False, f"写入剪贴板失败: {e}"
    return True, f"已把{source}写入剪贴板"


def run_clip_get_step(p: dict, variables: dict,
                      variable_types: dict | None = None) -> tuple[bool, str]:
    """执行「获取剪贴板内容」步骤：读取系统剪贴板文本，赋值给指定变量。"""
    name = (p.get("variable") or "").strip()
    if not name:
        return False, "未指定变量名"
    try:
        text = pyperclip.paste()
    except Exception as e:
        return False, f"读取剪贴板失败: {e}"
    variables[name] = text
    if variable_types is not None:
        variable_types[name] = "string"
    return True, f"已把剪贴板内容赋值给变量 {name}"


def run_ocr_step(p: dict, variables: dict, stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「文字识别」步骤：RapidOCR 识别屏幕区域，结果写入指定变量。

    返回 (成功?, 原因)。结果变量 absent 时识别成功但写变量失败定义为失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    var = (p.get("variable") or "").strip()
    if not var:
        return False, "未指定结果变量"
    from . import ocr as ocr_actor
    ok, value, why = ocr_actor.recognize(
        region=str(p.get("region") or ""),
        lang=str(p.get("lang") or "ch"),
        multi_ocr=bool(p.get("multi_ocr", True)),
    )
    if not ok:
        return False, why
    variables[var] = value
    return True, f"已识别文字到变量 {var}（{why}）"


def run_text_find_step(p: dict, variables: dict,
                       stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「文字查找」步骤：OCR 在屏幕/区域查找指定文字。

    找到：
      - 勾选点击 -> 鼠标左/右键点击该文字中心
      - 未勾选   -> 把坐标 "x,y" 写入结果变量
    未找到：把 false 写入结果变量。步骤本身不因未找到而失败，
    以便后续步骤根据变量值分支。OCR 本身不可用/异常才算失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import ocr as ocr_actor
    keyword = resolve_references(str(p.get("text") or ""), variables).strip()
    if not keyword:
        return False, "查找文字为空"
    var = (p.get("variable") or "").strip()
    ok, value, why = ocr_actor.find_text(
        region=str(p.get("region") or ""),
        text=keyword,
    )
    if not ok:
        return False, why
    if value is None:
        if var:
            variables[var] = False
        return True, f"未找到文字「{keyword}」"
    x, y = int(value["x"]), int(value["y"])
    if p.get("click"):
        button = "right" if p.get("click_button") == "right" else "left"
        input_actors.click(button, 1, x, y)
        return True, f"已点击文字「{keyword}」（{x}, {y}）"
    if var:
        variables[var] = f"{x},{y}"
    return True, f"找到文字「{keyword}」（{x}, {y}）"


def run_screenshot_step(p: dict, variables: dict,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「截图」步骤：按指定区域截图，保存到文件。

    返回 (成功?, 原因)。保存方式：
      - variable（默认保存）：保存到 <程序目录>/templates/jietu/（不存在自动创建）；
      - choose（自选保存）：弹「另存为」对话框由用户选择保存位置，取消视为失败。
    默认保存必须指定结果变量；自选保存可选（选了也会把绝对路径写入变量）。

    「另存为对话框」需要主线程 UI，通过 screenshot_actor.ui_call 调度到主线程执行
    （后台线程阻塞等待结果，主线程用嵌套事件循环处理交互）。
    """
    from . import screenshot_actor
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    save_mode = (p.get("save_mode") or "variable").strip()
    var = (p.get("variable") or "").strip()
    region = str(p.get("region") or "")

    # 默认保存必须指定结果变量（自选保存可选），在抓图前校验避免无谓抓屏
    if save_mode != "choose" and not var:
        return False, "未指定结果变量"

    import cv2  # 与 finder/capture_overlay 同一依赖，仅本步骤用到
    try:
        # 固定「指定区域」截图；region 为空时回退全屏（兼容旧版全屏配置）
        img = screenshot_actor.grab_image("region", region)

        if save_mode == "choose":
            default_name = f"截图_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path = screenshot_actor.ui_call(
                lambda: screenshot_actor.ask_save_path(default_name))
            if not path:
                return False, "已取消保存"
            cv2.imwrite(path, img)
        else:
            path = screenshot_actor.save_jietu(img)
    except Exception as e:
        return False, f"截图失败：{type(e).__name__}: {e}"

    if var:
        variables[var] = path
    return True, f"截图已保存：{path}"


def run_find_image_step(p: dict, variables: dict,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「找图」步骤：模板匹配在屏幕 / 指定区域找图。

    找到：把目标矩形区域 "左上x,左上y,右下x,右下y" 写入结果变量；
    未找到：把 false 写入结果变量。
    步骤本身不因未找到而失败（供后续步骤按变量值分支），
    模板图加载失败 / 未指定结果变量才算失败。
    勾选「效果预览」且命中时，在被找到的区域画红框（默认 1 秒，可设时长）。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    var = (p.get("variable") or "").strip()
    if not var:
        return False, "未指定结果变量"
    template = finder.load_template(
        resolve_template_path(p.get("image", ""), p.get("image_path", "")) or "")
    if template is None:
        return False, "模板图加载失败"
    region = parse_region_str(str(p.get("region", "") or ""))
    confidence = float(p.get("confidence", 0.85) or 0.85)
    try:
        screen = finder.grab_full_screen()
        if region is not None:
            hit = finder.locate_in_region(template, screen, confidence, region)
        else:
            hit = finder.locate(template, screen, confidence)
    except Exception as e:
        return False, f"找图失败：{type(e).__name__}: {e}"
    if hit is None:
        variables[var] = False
        return True, "未找到目标"
    cx, cy, score = hit
    th, tw = template.shape[:2]
    left = int(cx) - tw // 2
    top = int(cy) - th // 2
    right = left + tw
    bottom = top + th
    variables[var] = f"{left},{top},{right},{bottom}"
    if p.get("preview"):
        try:
            from .find_preview import show_find_highlight
            show_find_highlight((left, top, right, bottom),
                                float(p.get("preview_duration", 1.0) or 1.0))
        except Exception:
            pass    # 预览失败不影响找图本身（如无 Qt 环境）
    return True, f"找到目标（区域 {left},{top},{right},{bottom}，置信度 {score:.2f}）"


def _coord_from_var(expr: str, variables: dict | None, default: int) -> int:
    """解析坐标轴的变量引用：expr 为变量名时取其整数值，未定义/非数字回退默认值。"""
    name = (expr or "").strip()
    if not name:
        return int(default)
    value = (variables or {}).get(name)
    if value is None:
        log(f"坐标变量「{name}」未定义，使用固定坐标 {int(default)}")
        return int(default)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        log(f"坐标变量「{name}」的值不是数字（{value!r}），使用固定坐标 {int(default)}")
        return int(default)


def _coord_from_pos_var(expr: str, variables: dict | None,
                        default_x: int, default_y: int) -> tuple[int, int]:
    """解析坐标变量：取变量的中心坐标。

    支持两种格式：
      - 坐标 "x,y"（如 "64,63"）→ 直接用该点；
      - 矩形区域 "x1,y1,x2,y2"（左上角+右下角，找图模块结果）→ 取区域中心点。
    也容忍列表/元组 [x, y] 或 [x1, y1, x2, y2]。未定义、格式不对时回退固定坐标。
    """
    name = (expr or "").strip()
    if not name:
        return int(default_x), int(default_y)
    value = (variables or {}).get(name)
    if value is None:
        log(f"坐标变量「{name}」未定义，使用固定坐标 ({int(default_x)},{int(default_y)})")
        return int(default_x), int(default_y)
    try:
        if isinstance(value, (list, tuple)):
            parts = [int(float(v)) for v in value]
        else:
            parts = [int(float(v.strip())) for v in str(value).strip().split(",")]
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 4:
            x1, y1, x2, y2 = parts
            return (x1 + x2) // 2, (y1 + y2) // 2
        raise ValueError
    except (TypeError, ValueError):
        log(f"坐标变量「{name}」的值不是 \"x,y\" 或 \"x1,y1,x2,y2\" 格式（{value!r}），"
            f"使用固定坐标 ({int(default_x)},{int(default_y)})")
        return int(default_x), int(default_y)


def run_click_step(p: dict, stop: threading.Event, progress, variables: dict | None = None) -> str:
    """公共鼠标点击循环。返回结束原因。

    variables: 流程运行期变量（可选）。固定坐标模式下 pos_var 非空时，
    坐标优先取变量值（"x,y" 坐标，或 "x1,y1,x2,y2" 区域取中心），
    未定义或格式不对回退 pos_x / pos_y。
    """
    button = p.get("mouse_button", "left")
    times = 2 if p.get("click_type") == "double" else 1
    interval = max(int(p.get("interval_ms", 100) or 100), 20) / 1000.0
    count = int(p.get("count", 1) or 0)
    duration = float(p.get("duration_sec", 0) or 0)
    if count <= 0 and duration <= 0:
        count = 1  # 防呆：流程中"无限"步骤按 1 次执行，避免流程卡死
    background = bool(p.get("background"))
    window_title = (p.get("window_title") or "").strip()
    if background and not window_title:
        return "未绑定目标窗口"
    # 后台模式：按窗口标题动态查找句柄（句柄每次重启都变，不能存死），
    # 整个循环期间激活目标窗口一次，SendInput 才能落到它上面
    # （PostMessage 对 UWP/Chrome 等现代应用无效，见 win_actors 说明）。
    if background:
        hwnd = win_actors.find_window_by_title(window_title) or \
            win_actors.find_window_like(window_title)
        if not hwnd:
            return "目标窗口不存在"
        win_actors.activate_window(hwnd)
    done, t0 = 0, time.monotonic()
    try:
        while True:
            if p.get("fixed_position"):
                if p.get("pos_var"):
                    x, y = _coord_from_pos_var(p.get("pos_var"), variables,
                                               p.get("pos_x", 0), p.get("pos_y", 0))
                else:
                    # 兼容旧配置：pos_x_var / pos_y_var 各自独立
                    x = _coord_from_var(p.get("pos_x_var"), variables, p.get("pos_x", 0))
                    y = _coord_from_var(p.get("pos_y_var"), variables, p.get("pos_y", 0))
                input_actors.click(button, times, x, y)
            else:
                input_actors.click(button, times)
            done += 1
            progress(done, time.monotonic() - t0)
            reason = _limit_reason(stop, done, t0, count, duration)
            if reason:
                return reason
            stop.wait(interval)
    finally:
        if background:
            win_actors.restore_foreground()


def run_press_step(p: dict, stop: threading.Event, progress) -> str:
    """公共键盘连按循环。返回结束原因。"""
    keys = (p.get("keys") or "").strip()
    if not keys:
        return "未设置按键"
    interval = max(int(p.get("interval_ms", 100) or 100), 20) / 1000.0
    count = int(p.get("count", 1) or 0)
    duration = float(p.get("duration_sec", 0) or 0)
    if count <= 0 and duration <= 0:
        count = 1
    background = bool(p.get("background"))
    window_title = (p.get("window_title") or "").strip()
    if background and not window_title:
        return "未绑定目标窗口"
    # 后台模式：按窗口标题动态查找句柄，整个循环期间激活目标窗口一次。
    if background:
        hwnd = win_actors.find_window_by_title(window_title) or \
            win_actors.find_window_like(window_title)
        if not hwnd:
            return "目标窗口不存在"
        win_actors.activate_window(hwnd)
    done, t0 = 0, time.monotonic()
    try:
        while True:
            input_actors.press_combo(keys)
            done += 1
            progress(done, time.monotonic() - t0)
            reason = _limit_reason(stop, done, t0, count, duration)
            if reason:
                return reason
            stop.wait(interval)
    except ValueError as e:
        return f"按键无效: {e}"
    finally:
        if background:
            win_actors.restore_foreground()


def _wait_hit(template, confidence: float, timeout: float, region,
              stop: threading.Event):
    """在超时窗口内循环抓屏匹配；timeout<=0 表示一直等到找到。"""
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    while True:
        if stop.is_set():
            return None
        screen = finder.grab_full_screen()
        if region is not None:
            hit = finder.locate_in_region(template, screen, confidence, region)
        else:
            hit = finder.locate(template, screen, confidence)
        if hit:
            return hit
        if deadline and time.monotonic() >= deadline:
            return None
        stop.wait(grab_interval(region))


def run_find_step(p: dict, stop: threading.Event, progress) -> str:
    """公共找图点击循环。返回结束原因（"等待目标超时" 视为步骤失败）。"""
    template = finder.load_template(
        resolve_template_path(p.get("image", ""), p.get("image_path", "")) or "")
    if template is None:
        return "模板图加载失败"
    region = parse_region_str(str(p.get("region", "") or ""))
    interval = max(int(p.get("interval_ms", 500) or 500), 50) / 1000.0
    timeout = float(p.get("search_timeout_sec", 10) or 0)
    count = int(p.get("count", 1) or 0)
    duration = float(p.get("duration_sec", 0) or 0)
    if count <= 0 and duration <= 0:
        count = 1
    done, t0 = 0, time.monotonic()
    while True:
        hit = _wait_hit(template, float(p.get("confidence", 0.85)), timeout, region, stop)
        if hit is None:
            return "已手动停止" if stop.is_set() else "等待目标超时"
        cx, cy, _score = hit
        x, y = cx + int(p.get("offset_x", 0) or 0), cy + int(p.get("offset_y", 0) or 0)
        click_type = p.get("click_type", "single")
        if click_type == "right":
            input_actors.click("right", 1, x, y)
        elif click_type == "double":
            input_actors.click("left", 2, x, y)
        else:
            input_actors.click("left", 1, x, y)
        done += 1
        progress(done, time.monotonic() - t0)
        reason = _limit_reason(stop, done, t0, count, duration)
        if reason:
            return reason
        stop.wait(interval)


class BaseTask(QObject):
    stateChanged = Signal(str, str)   # ("running"/"stopped", 结束原因)
    progress = Signal(int, float)     # 已执行次数, 已用时(秒)

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_running = False

    def start(self) -> bool:
        if self.is_running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_wrapper, daemon=True, name=self.name)
        self.is_running = True
        self.stateChanged.emit("running", "")
        log(f"{self.name}：已启动")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run_wrapper(self) -> None:
        try:
            reason = self.work()
        except Exception as e:  # 保护线程不静默死亡
            reason = f"出错: {e}"
        self.is_running = False
        self.stateChanged.emit("stopped", reason)
        log(f"{self.name}：已停止（{reason}）")

    def work(self) -> str:
        raise NotImplementedError


class ClickTask(BaseTask):
    """鼠标连点。get_config 由主线程提供（返回当前配置快照）。"""

    def __init__(self):
        super().__init__("鼠标连点")
        self.get_config = lambda: None

    def work(self) -> str:
        cfg = self.get_config()
        if cfg is None:
            return "未配置"
        return run_click_step(asdict(cfg), self._stop,
                              lambda d, e: self.progress.emit(d, e))


class PressTask(BaseTask):
    """键盘连按。"""

    def __init__(self):
        super().__init__("键盘连按")
        self.get_config = lambda: None

    def work(self) -> str:
        cfg = self.get_config()
        if cfg is None:
            return "未配置"
        return run_press_step(asdict(cfg), self._stop,
                              lambda d, e: self.progress.emit(d, e))


class FindTaskRunner(BaseTask):
    """单个找图点击任务。task 为启动时主线程写入的配置副本。"""

    def __init__(self, task):
        super().__init__(f"找图:{task.name}")
        self.task = task

    def work(self) -> str:
        return run_find_step(asdict(self.task), self._stop,
                             lambda d, e: self.progress.emit(d, e))
