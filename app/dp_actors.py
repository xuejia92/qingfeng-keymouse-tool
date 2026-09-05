"""DrissionPage 可视化网页自动化执行器。

与 web_actors 的分工：
- web_actors：管浏览器会话单例（启动模式 / 接管端口 / 关闭语义 / 僵尸重建）；
- dp_actors：在其上实现流程模块的细粒度操作——「打开浏览器」把 Chromium
  实例保存到流程变量，后续元素操作 / 标签切换 / 监听 / 截图 / 上传 / 关闭浏览器
  都从变量里取浏览器对象，串成一条可视化链路；变量里存的就是 web_actors 的
  单例实例，「关闭浏览器」按变量定位该实例后走 web_actors 的统一关闭语义
  （自启退出 / attach 只断连、保留用户手动打开的窗口）。

定位语法、元素操作、监听、截图均对照官方文档实现：
- 定位语法：https://drissionpage.cn/browser_control/get_elements/sheet/
- 元素交互：https://drissionpage.cn/browser_control/ele_operation
- 监听网络：https://drissionpage.cn/browser_control/listen
"""
from __future__ import annotations

import os

from . import web_actors

# 元素定位方式：值 -> 显示名（对应 DrissionPage 定位语法速查表）
DP_LOCATORS = {
    "id": "id 属性（#）",
    "class": "class 属性（.）",
    "attr": "指定属性（@属性名）",
    "text": "元素文本（text）",
    "tag": "标签类型（t:）",
    "css": "CSS 选择器（css:）",
    "xpath": "XPath（xpath:）",
}

# 匹配模式：值 -> 显示名（= 精确 / : 模糊 / ^ 开头 / $ 结尾）
DP_MATCHES = {
    "=": "精确匹配",
    ":": "模糊匹配（包含）",
    "^": "匹配开头",
    "$": "匹配结尾",
}

# 元素找到后可执行的操作：值 -> 显示名（参照官方「元素交互」章节）
DP_ELE_ACTIONS = {
    "click": "点击元素",
    "click_js": "点击元素（js 方式，无视遮挡）",
    "click_right": "右键点击",
    "clear": "清空文本",
    "input": "输入内容",
    "input_enter": "输入内容并回车",
    "focus": "获取焦点",
    "hover": "鼠标悬停",
    "check": "选中（勾选框）",
    "uncheck": "取消选中",
    "set_value": "设置 value",
    "select_text": "下拉列表按文本选择",
    "select_value": "下拉列表按 value 选择",
    "select_index": "下拉列表按序号选择",
    "scroll_to_see": "滚动到元素可见",
    "drag": "拖动偏移（输入 x,y）",
    "to_upload": "点击并上传文件",
    "to_download": "点击并下载",
    "get_text": "获取元素文本 → 变量",
    "get_attr": "获取元素属性 → 变量",
    "for_new_tab": "点击并等待新标签 → 变量",
}

# 标签切换方式：值 -> 显示名
DP_TAB_MODES = {
    "index": "按序号切换标签",
    "title": "按标题切换标签",
    "url": "按网址切换标签",
    "new": "新建标签并切换",
}

# 监听动作：值 -> 显示名
DP_LISTEN_ACTIONS = {
    "start": "启动监听",
    "wait": "等待并捕获数据包",
    "stop": "停止监听",
}


class DpStepError(Exception):
    """DrissionPage 步骤失败（带人话原因）。"""


def build_locator(locator_type: str, match: str, value: str,
                  attr_name: str = "") -> str:
    """把「定位方式 + 匹配模式 + 值」合成 DrissionPage 定位符。

    语法对照速查表：# id、. class、@属性名、text、t: 标签、css:、xpath:；
    匹配模式 = 精确 / : 模糊 / ^ 开头 / $ 结尾（css、xpath、tag 不适用匹配模式）。
    """
    value = (value or "").strip()
    t = (locator_type or "id").strip()
    m = match if match in DP_MATCHES else "="
    if t == "id":
        return "#" + ("" if m == "=" else m) + value
    if t == "class":
        return "." + ("" if m == "=" else m) + value
    if t == "attr":
        name = (attr_name or "").strip() or "name"
        return f"@{name}{m}{value}"
    if t == "text":
        return f"text{m}{value}"
    if t == "tag":
        return f"t:{value}"
    if t == "css":
        return f"css:{value}"
    if t == "xpath":
        return f"xpath:{value}"
    return value


def _locator_display(p: dict) -> str:
    """步骤参数里的定位信息 -> 摘要显示文本（如「id=kw」）。"""
    t = (p.get("locator_type") or "id").strip()
    m = p.get("match") if p.get("match") in DP_MATCHES else "="
    value = (p.get("locator_value") or "").strip()
    if t == "attr":
        name = (p.get("attr_name") or "").strip() or "?"
        return f"@{name}{m}{value or '?'}"
    if t in ("css", "xpath", "tag"):
        return f"{t}:{value or '?'}"
    sym = {"id": "#", "class": "."}.get(t, "text")
    return f"{sym}{'' if m == '=' else m}{value or '?'}"


def default_shot_dir() -> str:
    """网页/元素截图的默认保存目录：程序模板目录下的 jietu/。"""
    from .config import TEMPLATE_DIR
    d = os.path.join(TEMPLATE_DIR, "jietu")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _get_browser(p: dict, variables: dict):
    """从流程变量里取浏览器对象；缺失/无效时抛 DpStepError。"""
    from .values import resolve_references
    name = resolve_references(str(p.get("browser_var") or ""), variables).strip()
    if not name:
        raise DpStepError("未指定浏览器变量")
    if name not in variables:
        raise DpStepError(f"浏览器变量「{name}」未定义（请先执行「打开浏览器」步骤）")
    browser = variables[name]
    if browser is None or not hasattr(browser, "latest_tab"):
        raise DpStepError(f"变量「{name}」不是浏览器对象")
    return name, browser


def _resolve(p: dict, key: str, variables: dict) -> str:
    """解析参数字段的 $变量名 引用。"""
    from .values import resolve_references
    return resolve_references(str(p.get(key) or ""), variables)


def _find_ele(tab, locator: str, index, timeout: float):
    """在标签页内按定位符找元素；未找到返回 None（NoneElement 视为假值）。"""
    try:
        idx = int(index or 1)
    except (TypeError, ValueError):
        idx = 1
    if idx == 0:
        idx = 1
    try:
        ele = tab.ele(locator, timeout=max(float(timeout or 0), 0.5), index=idx)
    except TypeError:
        # 老版本 ele() 不支持 index 关键字：退回先取列表再选位置
        try:
            if idx > 1:
                eles = tab.eles(locator, timeout=max(float(timeout or 0), 0.5))
                ele = eles[idx - 1] if 0 <= idx - 1 < len(eles) else None
            elif idx < 0:
                eles = tab.eles(locator, timeout=max(float(timeout or 0), 0.5))
                ele = eles[idx] if -len(eles) <= idx < 0 else None
            else:
                ele = tab.ele(locator, timeout=max(float(timeout or 0), 0.5))
        except Exception as e:
            raise DpStepError(f"查找元素失败：{type(e).__name__}: {e}")
    return ele or None


def _parse_files(text: str) -> list[str]:
    """解析文件路径列表：换行或 | 分隔；单条时也兼容。"""
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [x.strip() for x in raw.replace("|", "\n").splitlines()]
    return [x for x in parts if x]


def run_dp_browser_step(p: dict, variables: dict,
                        stop=None) -> tuple[bool, str]:
    """「打开浏览器」：启动/接管浏览器并把浏览器对象保存到指定变量（必填）。

    复用 web_actors 的会话管理：launch_mode 支持 front/headless/background/attach
    （attach 需 attach_port）；可选在打开后访问一个网址（url）。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references

    var = resolve_references(str(p.get("browser_var") or ""), variables).strip()
    if not var:
        return False, "未指定浏览器变量（必填项）"

    mode = (p.get("launch_mode") or web_actors.DEFAULT_MODE).strip()
    if mode not in web_actors.LAUNCH_MODES:
        return False, f"未知的浏览器启动模式: {mode}"
    attach_port = None
    if mode == "attach":
        try:
            attach_port = web_actors._parse_attach_port(p.get("attach_port"))
        except ValueError as e:
            return False, f"接管浏览器：{e}"

    ok, why = web_actors.is_available()
    if not ok:
        return False, why

    url = resolve_references(str(p.get("url") or ""), variables).strip()
    if url and not url.lower().startswith(("http://", "https://", "file://", "about:")):
        url = "https://" + url
    try:
        timeout = max(float(p.get("load_timeout_sec") or 20), 1.0)
    except (TypeError, ValueError):
        timeout = 20.0

    try:
        browser = web_actors.get_browser(mode, attach_port)
        if url:
            tab = browser.new_tab(url) if p.get("new_tab") else browser.latest_tab
            if not p.get("new_tab"):
                tab.get(url, timeout=timeout)
    except Exception as e:
        return False, f"浏览器启动失败：{type(e).__name__}: {e}"

    variables[var] = browser
    note = {"front": "前台", "headless": "无头", "background": "后台",
            "attach": f"接管端口{attach_port}"}.get(mode, mode)
    return True, f"浏览器对象已保存到变量 {var}（{note}）"


def _exec_ele_action(ele, action: str, input_value: str, files: list[str]):
    """对元素执行操作；返回 (成功?, 说明)。有返回值的操作返回结果值。"""
    if action == "click":
        res = ele.click()
        return (res is not False), "已点击元素"
    if action == "click_js":
        ele.click(by_js=True)
        return True, "已点击元素（js 方式）"
    if action == "click_right":
        ele.click.right()
        return True, "已右键点击元素"
    if action == "clear":
        ele.clear()
        return True, "已清空文本"
    if action == "input":
        ele.input(input_value)
        return True, "已输入内容"
    if action == "input_enter":
        ele.input(input_value + "\n")
        return True, "已输入内容并回车"
    if action == "focus":
        ele.focus()
        return True, "元素已获取焦点"
    if action == "hover":
        ele.hover()
        return True, "已悬停元素"
    if action == "check":
        ele.check()
        return True, "已选中元素"
    if action == "uncheck":
        ele.check(uncheck=True)
        return True, "已取消选中"
    if action == "set_value":
        ele.set.value(input_value)
        return True, "已设置 value"
    if action == "select_text":
        okv = ele.select.by_text(input_value)
        return bool(okv), ("已按文本选择列表项" if okv else "按文本选择列表项失败")
    if action == "select_value":
        okv = ele.select.by_value(input_value)
        return bool(okv), ("已按 value 选择列表项" if okv else "按 value 选择列表项失败")
    if action == "select_index":
        try:
            idx = int(input_value)
        except (TypeError, ValueError):
            return False, f"列表序号不是有效数字：{input_value!r}"
        okv = ele.select.by_index(idx)
        return bool(okv), ("已按序号选择列表项" if okv else "按序号选择列表项失败")
    if action == "scroll_to_see":
        ele.scroll.to_see()
        return True, "已滚动到元素可见"
    if action == "drag":
        try:
            dx, dy = (int(v.strip()) for v in input_value.split(","))
        except (ValueError, TypeError):
            return False, "拖动偏移格式应为 x,y（如 50,100）"
        ele.drag(dx, dy)
        return True, f"已拖动元素（{dx},{dy}）"
    if action == "to_upload":
        if not files:
            return False, "未填写要上传的文件路径"
        ele.click.to_upload(files if len(files) > 1 else files[0])
        return True, f"已上传 {len(files)} 个文件"
    if action == "to_download":
        if not files:
            return False, "未填写下载保存目录"
        save_path = files[0]
        try:
            os.makedirs(save_path, exist_ok=True)
        except OSError:
            return False, f"下载目录无法创建：{save_path}"
        ele.click.to_download(save_path=save_path)
        return True, f"已触发下载（保存到 {save_path}）"
    if action == "get_text":
        return True, ele.text
    if action == "get_attr":
        return True, ele.attr(input_value)
    if action == "for_new_tab":
        tab = ele.click.for_new_tab()
        return True, tab
    return False, f"未知的元素操作: {action}"


# 有返回值的元素操作（结果写入 result_var）
_RESULT_ELE_ACTIONS = {"get_text", "get_attr", "for_new_tab"}


def run_dp_element_step(p: dict, variables: dict,
                        stop=None) -> tuple[bool, str]:
    """「元素操作」：定位元素并执行 click / input / to_upload 等操作。"""
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    try:
        _, browser = _get_browser(p, variables)
        locator = build_locator(p.get("locator_type"), p.get("match"),
                                _resolve(p, "locator_value", variables),
                                p.get("attr_name") or "")
        value = (p.get("locator_value") or "").strip()
        if not value:
            return False, "未填写定位值"
        action = (p.get("action") or "click").strip()
        if action not in DP_ELE_ACTIONS:
            return False, f"未知的元素操作: {action}"
        result_var = (p.get("result_var") or "").strip()
        if action in _RESULT_ELE_ACTIONS and not result_var:
            return False, "该操作有返回值，请先设置结果变量"

        tab = browser.latest_tab
        ele = _find_ele(tab, locator, p.get("index"), float(p.get("timeout") or 10))
        if ele is None:
            return False, f"未找到元素（{locator}）"

        input_value = _resolve(p, "input_value", variables)
        files = _parse_files(_resolve(p, "file_paths", variables))
        ok, info = _exec_ele_action(ele, action, input_value, files)
        if not ok:
            return False, info

        if action in _RESULT_ELE_ACTIONS and result_var:
            if action == "for_new_tab":
                variables[result_var] = {
                    "tab_id": getattr(info, "tab_id", ""),
                    "title": getattr(info, "title", "") or "",
                    "url": getattr(info, "url", "") or "",
                }
            else:
                variables[result_var] = info
        label = DP_ELE_ACTIONS.get(action, action)
        why = label if action not in _RESULT_ELE_ACTIONS else f"{label} → {result_var}"
        return True, why
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"元素操作失败：{type(e).__name__}: {e}"


def run_dp_tab_step(p: dict, variables: dict,
                    stop=None) -> tuple[bool, str]:
    """「切换标签」：按序号/标题/网址切换标签，或新建标签并切换。

    切换后的标签信息（{tab_id, title, url}）可选写入结果变量。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    try:
        _, browser = _get_browser(p, variables)
        mode = (p.get("switch_mode") or "index").strip()
        if mode not in DP_TAB_MODES:
            return False, f"未知的切换方式: {mode}"

        def _tab_info(tab) -> dict:
            info = {"tab_id": getattr(tab, "tab_id", ""),
                    "title": getattr(tab, "title", "") or "",
                    "url": getattr(tab, "url", "") or ""}
            # title/url 是属性访问，个别对象上可能抛异常（连接中）：逐项兜底
            try:
                info["title"] = tab.title or ""
            except Exception:
                pass
            try:
                info["url"] = tab.url or ""
            except Exception:
                pass
            return info

        if mode == "new":
            url = _resolve(p, "url", variables).strip()
            if url and not url.lower().startswith(("http://", "https://", "file://", "about:")):
                url = "https://" + url
            tab = browser.new_tab(url) if url else browser.new_tab()
            result_var = (p.get("result_var") or "").strip()
            if result_var:
                variables[result_var] = _tab_info(tab)
            try:
                title = tab.title or ""
            except Exception:
                title = ""
            return True, f"已新建并切换到标签{('「' + title + '」') if title else ''}"

        value = _resolve(p, "value", variables).strip()
        if not value:
            return False, "未填写切换条件（序号/标题/网址）"
        if mode == "index":
            try:
                idx = int(value)
            except (TypeError, ValueError):
                return False, f"标签序号不是有效数字：{value!r}"
            tabs = browser.get_tabs()
            tab = tabs[idx - 1] if idx > 0 else tabs[idx]
            if tab is None:
                return False, f"没有第 {idx} 个标签（现有 {len(tabs)} 个）"
        elif mode == "title":
            tab = browser.get_tab(title=value)
            if tab is None:
                return False, f"没有标题包含「{value}」的标签"
        else:   # url
            tab = browser.get_tab(url=value)
            if tab is None:
                return False, f"没有网址包含「{value}」的标签"

        # 激活目标标签：优先 tab.activate()，失败退回 browser.to_tab()
        try:
            tab.activate()
        except Exception:
            browser.to_tab(getattr(tab, "tab_id", None))

        result_var = (p.get("result_var") or "").strip()
        info = _tab_info(tab)
        if result_var:
            variables[result_var] = info
        return True, f"已切换到标签「{info['title'] or info['url'] or info['tab_id']}」"
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"切换标签失败：{type(e).__name__}: {e}"


def run_dp_listen_step(p: dict, variables: dict,
                       stop=None) -> tuple[bool, str]:
    """「监听网络数据」：启动监听 / 等待捕获数据包 / 停止监听。

    典型用法（三步链路）：
      1. 启动监听（targets 为 URL 包含的文字，多个换行分隔；空 = 监听全部）
      2. 元素操作触发请求（如点击下一页）
      3. 等待并捕获数据包，把 url / 状态码 / 响应体写入结果变量
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    try:
        _, browser = _get_browser(p, variables)
        action = (p.get("action") or "start").strip()
        if action not in DP_LISTEN_ACTIONS:
            return False, f"未知的监听动作: {action}"
        tab = browser.latest_tab

        if action == "start":
            raw = _resolve(p, "targets", variables)
            targets = [x.strip() for x in raw.replace("|", "\n").splitlines()
                       if x.strip()]
            tab.listen.start(targets if targets else True)
            what = f"目标：{('、'.join(targets))}" if targets else "全部请求"
            return True, f"监听已启动（{what}）"

        if action == "stop":
            tab.listen.stop()
            return True, "监听已停止"

        # wait：阻塞等待一个数据包
        try:
            timeout = max(float(p.get("timeout") if p.get("timeout") is not None
                                else 10), 0.5)
        except (TypeError, ValueError):
            timeout = 10.0
        packet = tab.listen.wait(timeout=timeout)
        if not packet:
            return False, f"等待数据包超时（{timeout:g} 秒）"

        url_var = (p.get("url_var") or "").strip()
        status_var = (p.get("status_var") or "").strip()
        body_var = (p.get("body_var") or "").strip()
        url = getattr(packet, "url", "") or ""
        resp = getattr(packet, "response", None)
        status = getattr(resp, "status", None) if resp is not None else None
        body = getattr(resp, "body", None) if resp is not None else None
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        if url_var:
            variables[url_var] = url
        if status_var:
            variables[status_var] = status
        if body_var:
            variables[body_var] = body
        saved = "、".join(n for n, v in (("url", url_var), ("状态码", status_var),
                                         ("响应体", body_var)) if v)
        return True, f"已捕获数据包 {url}" + (f" → {saved}" if saved else "")
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"监听网络数据失败：{type(e).__name__}: {e}"


def run_dp_page_shot_step(p: dict, variables: dict,
                          stop=None) -> tuple[bool, str]:
    """「网页截图」：整页或视口截图，保存路径写入结果变量（必填）。"""
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    result_var = (p.get("result_var") or "").strip()
    if not result_var:
        return False, "未指定结果变量"
    try:
        _, browser = _get_browser(p, variables)
        path = _resolve(p, "path", variables).strip() or default_shot_dir()
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return False, f"保存目录无法创建：{path}"
        name = _resolve(p, "name", variables).strip() or None
        tab = browser.latest_tab
        saved = tab.get_screenshot(path=path, name=name,
                                   full_page=bool(p.get("full_page")))
        variables[result_var] = saved
        return True, f"网页截图已保存：{saved}"
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"网页截图失败：{type(e).__name__}: {e}"


def run_dp_ele_shot_step(p: dict, variables: dict,
                         stop=None) -> tuple[bool, str]:
    """「元素截图」：定位元素并截图，保存路径写入结果变量（必填）。"""
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    result_var = (p.get("result_var") or "").strip()
    if not result_var:
        return False, "未指定结果变量"
    try:
        _, browser = _get_browser(p, variables)
        locator = build_locator(p.get("locator_type"), p.get("match"),
                                _resolve(p, "locator_value", variables),
                                p.get("attr_name") or "")
        if not (p.get("locator_value") or "").strip():
            return False, "未填写定位值"
        tab = browser.latest_tab
        ele = _find_ele(tab, locator, p.get("index"), float(p.get("timeout") or 10))
        if ele is None:
            return False, f"未找到元素（{locator}）"
        path = _resolve(p, "path", variables).strip() or default_shot_dir()
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return False, f"保存目录无法创建：{path}"
        name = _resolve(p, "name", variables).strip() or None
        saved = ele.get_screenshot(path=path, name=name)
        variables[result_var] = saved
        return True, f"元素截图已保存：{saved}"
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"元素截图失败：{type(e).__name__}: {e}"


def run_dp_upload_step(p: dict, variables: dict,
                       stop=None) -> tuple[bool, str]:
    """「上传文件」：定位元素后点击触发文件选择框并填入文件路径。"""
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    try:
        _, browser = _get_browser(p, variables)
        files = _parse_files(_resolve(p, "file_paths", variables))
        if not files:
            return False, "未填写要上传的文件路径"
        for f in files:
            if not os.path.isfile(f):
                return False, f"文件不存在：{f}"
        locator = build_locator(p.get("locator_type"), p.get("match"),
                                _resolve(p, "locator_value", variables),
                                p.get("attr_name") or "")
        if not (p.get("locator_value") or "").strip():
            return False, "未填写定位值"
        tab = browser.latest_tab
        ele = _find_ele(tab, locator, p.get("index"), float(p.get("timeout") or 10))
        if ele is None:
            return False, f"未找到元素（{locator}）"
        ele.click.to_upload(files if len(files) > 1 else files[0])
        return True, f"已上传 {len(files)} 个文件"
    except DpStepError as e:
        return False, str(e)
    except Exception as e:
        return False, f"上传文件失败：{type(e).__name__}: {e}"


def run_dp_close_browser_step(p: dict, variables: dict,
                              stop=None) -> tuple[bool, str]:
    """「关闭浏览器」：关闭指定浏览器变量对应的浏览器。

    按变量定位浏览器对象后走 web_actors 的统一关闭语义：
    - 自启浏览器（front / headless / background）：退出进程，窗口一并关闭；
    - 接管浏览器（attach）：只断开连接，窗口保留，可继续手动使用；
    - 实例已失效（被手动关掉/崩溃）：只清理残留。
    关闭后该变量引用即失效，从变量字典里移除，避免后续步骤拿僵尸对象继续操作。
    """
    if stop is not None and stop.is_set():
        return False, "已手动停止"
    from .values import resolve_references
    name = resolve_references(str(p.get("browser_var") or ""), variables).strip()
    if not name:
        return False, "未指定浏览器变量"
    if name not in variables:
        return False, f"浏览器变量「{name}」未定义（请先执行「打开浏览器」步骤）"
    browser = variables[name]
    if browser is None or not hasattr(browser, "latest_tab"):
        return False, f"变量「{name}」不是浏览器对象"
    # 关闭后对象即失效：先摘掉变量，防止流程后续步骤引用僵尸实例
    variables.pop(name, None)
    if not web_actors.is_current(browser):
        # 变量指向的已不是当前活动会话（被替换/已失效的旧引用）。无法确知它
        # 当初是否 attach 接管，贸然 quit 会关掉用户手动打开的浏览器，故不动作。
        return True, (f"变量「{name}」对应的浏览器已不在活动会话"
                      f"（可能已关闭或被其它打开步骤替换），无需关闭")
    attached = web_actors.active_mode() == "attach"
    closed = web_actors.close_browser()
    if closed:
        return True, (f"已断开接管浏览器（变量 {name}，窗口保留可继续手动使用）"
                      if attached else f"浏览器已关闭（变量 {name}）")
    return True, f"浏览器（变量 {name}）未启动，无需关闭"
