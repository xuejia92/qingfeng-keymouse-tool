"""后台任务线程与公共执行函数。

run_click_step / run_press_step / run_find_step / run_var_step / run_log_step /
run_ocr_step / run_clip_set_step / run_clip_get_step / run_py_func_step 是与 UI
无关的公共执行函数，单任务（BaseTask 子类）与自动化流程（FlowRunner）共用同一套实现。
参数统一使用 dict（各字段与配置 dataclass 字段同名）。
"""
from __future__ import annotations

import inspect
import threading
import time
from dataclasses import asdict

import pyperclip

from PySide6.QtCore import QObject, Signal

from . import finder, input_actors, web_actors, win_actors
from .config import parse_region_str, resolve_template_path
from .logbus import log, log_print, log_print_raw

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
            attach_port=p.get("attach_port") or None,
        )
    elif action == "close_tab":
        ok, why = web_actors.close_tab(
            scope=p.get("tab_scope") or "current",
            match_text=p.get("match_text", ""),
        )
    elif action == "close_browser":
        attached = web_actors.active_mode() == "attach"   # 关的是接管会话还是自启会话
        closed = web_actors.close_browser()
        if closed:
            why = ("已断开接管浏览器（窗口保留，可继续手动使用）" if attached
                   else "浏览器已关闭")
        else:
            why = "浏览器未启动，无需关闭"
        ok = True
    else:
        ok, why = False, f"未知的网页动作: {action}"
    if ok and stop is not None and stop.is_set():
        return False, "已手动停止"
    return ok, why


def run_dp_browser_step(p: dict, variables: dict,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「打开浏览器」步骤：浏览器对象保存到指定变量（必填）。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_browser_step(p, variables, stop)
    except Exception as e:
        return False, f"打开浏览器失败：{type(e).__name__}: {e}"


def run_dp_element_step(p: dict, variables: dict,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「元素操作」步骤：定位元素并执行 click/input/to_upload 等操作。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_element_step(p, variables, stop)
    except Exception as e:
        return False, f"元素操作失败：{type(e).__name__}: {e}"


def run_dp_tab_step(p: dict, variables: dict,
                    stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「切换标签」步骤：按序号/标题/网址切换或新建标签。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_tab_step(p, variables, stop)
    except Exception as e:
        return False, f"切换标签失败：{type(e).__name__}: {e}"


def run_dp_listen_step(p: dict, variables: dict,
                       stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「监听网络数据」步骤：启动监听 / 等待捕获数据包 / 停止监听。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_listen_step(p, variables, stop)
    except Exception as e:
        return False, f"监听网络数据失败：{type(e).__name__}: {e}"


def run_dp_page_shot_step(p: dict, variables: dict,
                          stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「网页截图」步骤：页面截图路径写入结果变量。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_page_shot_step(p, variables, stop)
    except Exception as e:
        return False, f"网页截图失败：{type(e).__name__}: {e}"


def run_dp_ele_shot_step(p: dict, variables: dict,
                         stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「元素截图」步骤：元素截图路径写入结果变量。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_ele_shot_step(p, variables, stop)
    except Exception as e:
        return False, f"元素截图失败：{type(e).__name__}: {e}"


def run_dp_upload_step(p: dict, variables: dict,
                       stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「上传文件」步骤：点击元素触发文件选择框并上传指定文件。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_upload_step(p, variables, stop)
    except Exception as e:
        return False, f"上传文件失败：{type(e).__name__}: {e}"


def run_dp_close_browser_step(p: dict, variables: dict,
                              stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「关闭浏览器」步骤：关闭指定浏览器变量对应的浏览器。"""
    from . import dp_actors
    try:
        return dp_actors.run_dp_close_browser_step(p, variables, stop)
    except Exception as e:
        return False, f"关闭浏览器失败：{type(e).__name__}: {e}"


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
    """执行「打开应用」步骤。

    目标与启动策略：
    - use_process=True（默认「进程打开」）：process（可选，从进程列表选择/手填）
      优先——运行时若该进程已在运行，直接把它的窗口带到桌面最前端（不重复启动
      新实例）；未运行则继续走启动分支。
    - use_process=False（取消勾选）：忽略 process，直接用 path 启动。
    - path（可选）：启动用的可执行文件/文档/文件夹路径，进程未运行时用它启动
      （文件夹用默认方式打开资源管理器）。
    process 与 path 都空时报错。启动后按 wait_sec 等待加载。

    返回 (成功?, 原因)。启动失败视为失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    use_process = bool(p.get("use_process", True))
    process = (p.get("process") or "").strip() if use_process else ""
    path = (p.get("path") or "").strip()
    if not process and not path:
        return False, "未选择要打开的应用（填路径或从进程列表选择）"

    # 1) 指定了目标进程且正在运行：带出其窗口
    if process:
        hwnd = win_actors.find_process_window(process)
        if hwnd:
            if stop is not None and stop.is_set():
                return False, "已手动停止"
            win_actors.bring_to_front(hwnd)
            return True, f"「{process}」已在运行，已把窗口带到前台"

    # 2) 未在运行：走启动分支
    if not path:
        if process:
            return False, f"「{process}」未在运行，且未填写应用路径（无法启动）"
        return False, "未填写应用路径"
    ok, why = win_actors.launch_app(path)
    if not ok:
        return False, why
    wait_sec = float(p.get("wait_sec", 0) or 0)
    if wait_sec > 0 and (stop is None or not stop.wait(wait_sec)):
        pass                      # 等待期间若被停止，下面统一判定
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    return True, why


def run_http_request_step(p: dict, variables: dict,
                          stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「网络请求」步骤：用标准库 urllib 发起 GET/POST 请求。

    支持：自定义请求头、Cookie、请求体（POST）、User-Agent、超时、系统代理。
    结果写入 4 个可选结果变量：
      - status_var：HTTP 状态码（整数，含 4xx/5xx，网络层失败才判失败）
      - headers_var：响应头（dict）
      - cookie_var：响应 Cookie（dict，从 Set-Cookie 解析）
      - text_var：文本内容（result_type=text）或图片保存路径（result_type=image）
    网址/请求头/请求体/Cookie/User-Agent 均支持 $变量名 引用。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import http_actor

    url = resolve_references(str(p.get("url") or ""), variables).strip()
    if not url:
        return False, "网址为空"
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url   # 不带协议自动补 https://（与网页步骤一致）

    method = (p.get("method") or "get").strip().lower()
    headers_text = resolve_references(str(p.get("headers") or ""), variables)
    body = resolve_references(str(p.get("body") or ""), variables)
    cookie = resolve_references(str(p.get("cookie") or ""), variables)
    user_agent = resolve_references(str(p.get("user_agent") or ""), variables)
    result_type = (p.get("result_type") or "text").strip().lower()

    try:
        timeout = float(p.get("timeout") if p.get("timeout") is not None else 5)
    except (TypeError, ValueError):
        timeout = 5.0

    try:
        result = http_actor.perform_request(
            url=url,
            method=method,
            headers=http_actor.parse_headers(headers_text),
            body=body,
            cookie=cookie,
            result_type=result_type,
            user_agent=user_agent,
            timeout=timeout,
            use_proxy=bool(p.get("use_proxy", True)),
            proxy=str(p.get("proxy") or "127.0.0.1:7897"),
        )
    except http_actor.HttpError as e:
        return False, str(e)

    status_var = (p.get("status_var") or "").strip()
    headers_var = (p.get("headers_var") or "").strip()
    cookie_var = (p.get("cookie_var") or "").strip()
    text_var = (p.get("text_var") or "").strip()
    if status_var:
        variables[status_var] = result["status"]
    if headers_var:
        variables[headers_var] = result["headers"]
    if cookie_var:
        variables[cookie_var] = result["cookies"]
    if text_var:
        variables[text_var] = result["content"]

    what = "图片已保存" if result_type == "image" else "响应已获取"
    return True, f"{what}（状态码 {result['status']}）"


def run_deepseek_step(p: dict, variables: dict,
                      stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「DeepSeek 对话」步骤：调用 DeepSeek API 做一次对话补全。

    参数：model / api_key / system（角色设定）/ thinking（思考模式）/ stream（流式）/
    question（提问）/ result_var（结果变量）/ timeout / use_proxy / proxy / base_url。
    结果：把最终回答写入 result_var；思考过程（reasoning_content）打印到日志。
    提问、角色设定支持 $变量名 引用。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import deepseek_actor

    question = resolve_references(str(p.get("question") or ""), variables).strip()
    system = resolve_references(str(p.get("system") or ""), variables)
    model = (p.get("model") or "deepseek-v4-flash").strip()
    api_key = (p.get("api_key") or "").strip()
    result_var = (p.get("result_var") or "").strip()

    try:
        timeout = float(p.get("timeout") if p.get("timeout") is not None else 60)
    except (TypeError, ValueError):
        timeout = 60.0

    try:
        result = deepseek_actor.chat(
            api_key=api_key,
            model=model,
            system=system,
            question=question,
            thinking=bool(p.get("thinking")),
            stream=bool(p.get("stream")),
            timeout=timeout,
            use_proxy=bool(p.get("use_proxy", True)),
            proxy=str(p.get("proxy") or "127.0.0.1:7897"),
            base_url=str(p.get("base_url") or "https://api.deepseek.com"),
        )
    except deepseek_actor.DeepSeekError as e:
        return False, str(e)

    if result_var:
        variables[result_var] = result["content"]
    if result.get("reasoning"):
        reasoning = result["reasoning"]
        if len(reasoning) > 500:
            reasoning = reasoning[:500] + "…"
        log(f"DeepSeek 思考过程：{reasoning}")
    return True, f"已获取 DeepSeek 回答（{len(result['content'])} 字）"


def run_script_step(p: dict, variables: dict,
                    stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「执行脚本」步骤：运行 CMD / BAT / PowerShell / Python 脚本并捕获输出。

    参数：script_type（cmd/bat/powershell/python）/ source（text/file）/ content（文本内容）/
    path（脚本文件路径）/ encoding（gb2312/utf-8/utf-8-sig/ascii）/
    window_mode（hidden=隐藏窗口 / keep=完成后保留命令窗口）/ admin（管理员提权）/
    timeout（秒）/ result_var（输出变量）。
    脚本内容与文件路径支持 $变量名 引用；输出（stdout+stderr）写入 result_var。
    脚本跑完即算成功（退出码非 0 记入原因）；空内容/文件缺失/编码错误/超时/UAC 取消才算失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import script_actor

    source = (p.get("source") or "text").strip()
    script_type = (p.get("script_type") or "cmd").strip()
    content = resolve_references(str(p.get("content") or ""), variables)
    path = resolve_references(str(p.get("path") or ""), variables)
    result_var = (p.get("result_var") or "").strip()

    try:
        result = script_actor.run_script(
            script_type=script_type,
            source=source,
            content=content,
            path=path,
            encoding=str(p.get("encoding") or "utf-8"),
            window_mode=str(p.get("window_mode") or "hidden"),
            admin=bool(p.get("admin")),
            timeout=float(p.get("timeout") if p.get("timeout") is not None else 120),
        )
    except (script_actor.ScriptError, TypeError, ValueError) as e:
        return False, str(e)

    if result_var:
        variables[result_var] = result["output"]
    code = result.get("returncode") or 0
    if code:
        return True, f"脚本执行完成（退出码 {code}，输出 {len(result['output'])} 字）"
    return True, f"脚本执行完成（输出 {len(result['output'])} 字）"


def run_notify_step(p: dict, variables: dict,
                    stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「消息通知」步骤：在屏幕上弹出一条通知浮窗。

    参数：msg_type（info/success/warning/error）/ position（显示位置）/ content（消息内容，
    支持 $变量名 引用）/ duration（自动消失秒数）/ width（通知宽度，像素）。
    消息内容解析 $变量名 后为空判失败；通知展示失败判失败；通知关闭不影响步骤成败。
    通知浮窗在主线程展示（后台线程经 ui_call 调度）。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import notify_actor

    content = resolve_references(str(p.get("content") or ""), variables).strip()
    if not content:
        return False, "消息内容为空"

    msg_type = (p.get("msg_type") or "info").strip()
    position = (p.get("position") or "bottom").strip()
    try:
        duration = float(p.get("duration") if p.get("duration") is not None else 2)
    except (TypeError, ValueError):
        duration = 2.0
    try:
        width = int(p.get("width") if p.get("width") is not None else 320)
    except (TypeError, ValueError):
        width = 320

    try:
        notify_actor.show_notification(content, msg_type, duration, width, position)
    except Exception as e:
        return False, f"通知展示失败：{type(e).__name__}: {e}"

    label = notify_actor._theme(msg_type)["label"]
    return True, f"已弹出{label}通知"


def run_speech_step(p: dict, variables: dict,
                    stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「语音播报」步骤：用 pyttsx3 语音朗读文本。

    参数：content（播报内容，支持 $变量名 引用）/ wait（勾选=等播完再继续，
    默认；不勾=后台排队播报不阻塞当前流程）。
    内容解析 $变量名 后为空判失败；引擎不可用/播报异常判失败。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    from . import speech_actor

    content = resolve_references(str(p.get("content") or ""), variables).strip()
    if not content:
        return False, "播报内容为空"

    wait = bool(p.get("wait", True))
    try:
        if wait:
            ok, why = speech_actor.speak(content)
            return ok, why
        ok, why = speech_actor.speak_async(content)
        return ok, why
    except Exception as e:
        return False, f"语音播报失败：{type(e).__name__}: {e}"



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
    默认值是表达式时会按当时已存在的变量求值：支持 $变量名 引用、
    数学运算、字符串拼接、len()/int()/str() 等白名单函数（见 values.eval_var_default）。
    """
    from .values import eval_var_default
    name = (p.get("name") or "").strip()
    if not name:
        return False, "变量名为空"
    value_type = (p.get("type") or "string").strip().lower()
    default = str(p.get("default_value", "") or "")
    try:
        value = eval_var_default(value_type, default, variables)
    except ValueError as e:
        return False, str(e)
    variables[name] = value
    if variable_types is not None:
        variable_types[name] = value_type
    return True, f"已设置变量 {name}"


def _parse_log_var_item(item: str) -> tuple[str, str]:
    """解析「打印变量」单项：返回 (变量表达式, 输出后缀)。

    支持在变量表达式末尾加字面量 \\n（换行）或 \\b（空格）控制原始输出时的
    分隔符；无后缀返回空串（原始输出默认直接拼接、不换行）。
    """
    item = (item or "").strip()
    for esc, sep in (("\\n", "\n"), ("\\b", " ")):
        if item.endswith(esc):
            return item[:-len(esc)].rstrip(), sep
    return item, ""


def _unescape_log_text(text: str) -> str:
    """把「附加文本」里的字面量 \\n / \\b 转成换行 / 空格。

    只作用于用户手打的文本，在 $变量名 引用替换之前执行，避免误转变量值里
    本就含有的反斜杠序列。
    """
    return text.replace("\\n", "\n").replace("\\b", " ")


def run_log_step(p: dict, variables: dict) -> tuple[bool, str]:
    """执行「打印输出」步骤：把指定变量输出到日志控制台。

    variables 参数指定要输出的变量名（逗号分隔，支持 aaa['a'] / arr[0] 等
    Python 下标语法）；空（下拉选「无」）表示不打印任何变量。text 支持
    $变量名 占位，也支持字面量 \\n（换行）/ \\b（空格）。
    输出走「打印」通道，日志面板以蓝色字体区分。
    勾选「原始输出」（raw=True）时：不加时间戳、不自动换行、不带「变量名 =」前缀，
    多个变量默认直接拼接；变量表达式后加 \\n（换行）或 \\b（空格）控制分隔。
    勾选「显示类型」（show_type=True）时：每个解析成功的变量值后追加 Python 类型名，
    如 count = 5 (int)、name = 张三 (str)；解析失败的变量不带类型标注。
    """
    from .values import format_value, resolve_references, resolve_variable
    raw = bool(p.get("raw"))
    show_type = bool(p.get("show_type"))
    emit = log_print_raw if raw else log_print
    names = [x.strip() for x in (p.get("variables") or "").split(",") if x.strip()]
    raw_text = str(p.get("text") or "")
    text_src = _unescape_log_text(raw_text)

    if not names:
        # 没选变量（下拉「无」）：不打印任何变量；填了附加文本（哪怕只填 \n/\b 转义符）
        # 则只打印文本——判断用「原始输入是否非空白」：\n→换行、\b→空格 后再 strip 会
        # 误判成「没填」而把整条输出吞掉。
        if raw_text.strip():
            emit(resolve_references(text_src, variables))
            return True, "打印已输出"
        return True, "未打印任何内容"

    def with_type(value) -> str:
        """值文本 + 可选 Python 类型标注（如 5 (int)）。"""
        text_val = format_value(value)
        if not show_type:
            return text_val
        return f"{text_val} ({type(value).__name__})"

    text = resolve_references(text_src, variables)
    if raw:
        # 原始输出：只输出值本身，默认不换行拼接；\\n 换行、\\b 空格
        parts = []
        for name in names:
            expr, sep = _parse_log_var_item(name)
            ok, value, why = resolve_variable(expr, variables)
            parts.append((with_type(value) if ok else f"<{why}>") + sep)
        emit(text + "".join(parts) if text else "".join(parts))
        return True, f"已打印 {len(names)} 个变量"

    # 普通模式：name = value，每行一个（换行分隔）
    lines = []
    for name in names:
        ok, value, why = resolve_variable(name, variables)
        if ok:
            lines.append(f"{name} = {with_type(value)}")
        else:
            lines.append(f"{name} = <{why}>")
    if text:
        lines.insert(0, text)
    emit("\n".join(lines))
    return True, f"已打印 {len(names)} 个变量"


def run_clip_set_step(p: dict, variables: dict) -> tuple[bool, str]:
    """执行「赋值剪贴板」步骤：把变量值或自定义文本写入系统剪贴板。

    两种来源二选一（变量优先）：
      - name 指定变量（支持 aaa['a'] / arr[0] 等 Python 下标语法）：取 format_value(变量值)；
      - 否则用 text 自定义文本，支持 $变量名 引用。
    """
    from .values import format_value, resolve_references, resolve_variable
    name = (p.get("name") or "").strip()
    text = (p.get("text") or "").strip()
    if name:
        ok, val, why = resolve_variable(name, variables)
        if not ok:
            return False, why
        value = format_value(val)
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


def run_color_pick_step(p: dict, variables: dict,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「屏幕取色」步骤：把配置阶段取到的颜色字符串写入指定变量。

    颜色在编辑步骤时通过「屏幕取色…」拾取（取色遮罩实时放大 + 单击确认），
    这里只做运行时回填——把配置保存的颜色值（如 #FF0000 或 255,0,0）按
    所选格式原样写入结果变量，供后续步骤引用。

    返回 (成功?, 原因)。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    var = (p.get("variable") or "").strip()
    if not var:
        return False, "未指定结果变量"
    color = str(p.get("color") or "").strip()
    if not color:
        return False, "尚未取色（请编辑步骤点「屏幕取色…」拾取颜色）"
    variables[var] = color
    return True, f"颜色 {color} 已写入变量 {var}"


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


def run_yolo_detect_step(p: dict, variables: dict,
                         variable_types: dict | None = None,
                         stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「目标检测」步骤：YOLOv5 在屏幕 / 指定区域检测目标。

    结果写入结果变量：list[dict]，每项 {"class", "confidence", "region"}
    （region = "左上x,左上y,右下x,右下y"），按置信度从高到低；未检测到写空列表 []。
    步骤本身不因未检测到而失败（供后续步骤按变量值分支），
    模型路径无效 / 缺依赖 / 设备不可用 / 区域越界才算失败。

    附加动作（action != none）：对置信度最高的目标中心执行鼠标单击/右键/双击。
    勾选「效果预览」且有检出时，画多框红框（左上类别、右上置信度）。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    var = (p.get("variable") or "").strip()
    if not var:
        return False, "未指定结果变量"
    from . import yolo_actor
    from .values import resolve_references
    classes = resolve_references(str(p.get("classes") or ""), variables).strip()
    try:
        results = yolo_actor.detect(
            model_path=str(p.get("model_path") or ""),
            region=str(p.get("region") or ""),
            classes=classes,
            confidence=float(p.get("confidence", 0.5) or 0.5),
            device=str(p.get("device") or "cuda"),
        )
    except yolo_actor.YoloError as e:
        return False, str(e)
    except Exception as e:
        return False, f"目标检测失败：{type(e).__name__}: {e}"
    variables[var] = results
    if variable_types is not None:
        variable_types[var] = "list"
    if not results:
        return True, "未检测到目标"

    # 附加动作：对置信度最高的目标中心执行鼠标操作
    action = (p.get("action") or "none").strip()
    if action != "none":
        x1, y1, x2, y2 = (int(v) for v in results[0]["region"].split(","))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if action == "right":
            input_actors.click("right", 1, cx, cy)
        elif action == "double":
            input_actors.click("left", 2, cx, cy)
        else:
            input_actors.click("left", 1, cx, cy)

    if p.get("preview"):
        try:
            from .find_preview import show_boxes_highlight
            boxes = []
            for d in results:
                bx1, by1, bx2, by2 = (int(v) for v in d["region"].split(","))
                boxes.append(((bx1, by1, bx2, by2), d["class"],
                              f"{d['confidence']:.2f}"))
            show_boxes_highlight(boxes,
                                 float(p.get("preview_duration", 1.0) or 1.0))
        except Exception:
            pass    # 预览失败不影响检测本身（如无 Qt 环境）
    return True, f"检测到 {len(results)} 个目标 → {var}（最高置信度 {results[0]['confidence']:.2f}）"


def _py_err_text(prefix: str, e: Exception) -> str:
    """把异常格式化成简短可读的错误文本（含异常类型与信息，去掉长堆栈）。"""
    import traceback
    try:
        lines = traceback.format_exception_only(type(e), e)
        detail = "".join(lines).strip()
    except Exception:
        detail = str(e)
    return f"{prefix}: {detail}"


def run_py_func_step(p: dict, variables: dict,
                     stop: threading.Event | None = None) -> tuple[bool, str]:
    """执行「python函数」步骤：运行用户代码并调用指定函数，返回值写入指定变量。

    代码环境：
      - 提供标准 __builtins__，代码里可直接 import 常用库（datetime 等）；
      - params 中 variables 列表声明的流程变量以同名注入代码全局命名空间，
        供代码与函数体直接读取（如流程变量 x=5，代码写 def f(): return x+1 即可）。
    调用规则（func_name 必填）：
      - 代码执行完后调用该函数，取返回值作为结果；
      - 勾选变量中**与函数形参同名**的，自动以关键字实参传入——形参即取到变量值，
        例如流程变量 date_format、time_format 勾选后调用
        print_current_time(date_format=值, time_format=值)；
      - 与形参不同名的勾选变量仍注入全局环境，函数体内可直接读取；
      - 无默认值的必填形参若没有同名变量可传，返回明确缺失提示。
    结果写入 result_var 指定的流程变量（任意 Python 类型）。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    code = (p.get("code") or "").strip()
    result_var = (p.get("result_var") or "").strip()
    if not code:
        return False, "代码为空"
    if not result_var:
        return False, "未指定结果变量"
    func_name = (p.get("func_name") or "").strip()
    if not func_name:
        return False, "未填写调用函数名（python函数步骤固定调用一个函数并取返回值）"

    # 收集要注入的变量名（去重、保持声明顺序）；未定义的变量直接报错便于排查
    names: list[str] = []
    for n in p.get("variables") or []:
        n = str(n or "").strip()
        if n and n not in names:
            names.append(n)
    env: dict = {}
    for n in names:
        if n not in variables:
            return False, f"变量「{n}」未定义"
        env[n] = variables[n]

    try:
        exec(compile(code, "<python函数>", "exec"), env)
    except SyntaxError as e:
        return False, _py_err_text("代码语法错误", e)
    except Exception as e:
        return False, _py_err_text("代码执行出错", e)
    try:
        fn = env.get(func_name)
        if not callable(fn):
            return False, f"未找到函数「{func_name}」"
        # 同名形参自动传参：勾选变量里与函数形参同名的作关键字实参；其余仍注入环境
        kwargs: dict = {}
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            for pname, param in sig.parameters.items():
                if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  inspect.Parameter.KEYWORD_ONLY) and pname in env:
                    kwargs[pname] = env[pname]
            missing = [pname for pname, param in sig.parameters.items()
                       if param.default is inspect.Parameter.empty
                       and pname not in kwargs
                       and param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                          inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                          inspect.Parameter.KEYWORD_ONLY)]
            if missing:
                return False, (f"调用 {func_name} 缺少必填参数：{', '.join(missing)}；"
                               "请在变量列表勾选同名流程变量，或给形参设置默认值")
        result = fn(**kwargs)
    except Exception as e:
        return False, _py_err_text(f"函数 {func_name} 执行出错", e)

    variables[result_var] = result
    return True, f"结果已保存到变量 {result_var}"


def _coord_from_var(expr: str, variables: dict | None, default: int) -> int:
    """解析坐标轴的变量引用：expr 为变量名（或下标表达式）时取其整数值，
    未定义/非数字回退默认值。"""
    name = (expr or "").strip()
    if not name:
        return int(default)
    from .values import resolve_variable
    ok, value, why = resolve_variable(name, variables or {})
    if not ok:
        log(f"坐标变量「{name}」{why}，使用固定坐标 {int(default)}")
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
    expr 支持 aaa['a'] / arr[0] 等 Python 下标语法。
    """
    name = (expr or "").strip()
    if not name:
        return int(default_x), int(default_y)
    from .values import resolve_variable
    ok, value, why = resolve_variable(name, variables or {})
    if not ok:
        log(f"坐标变量「{name}」{why}，使用固定坐标 ({int(default_x)},{int(default_y)})")
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
