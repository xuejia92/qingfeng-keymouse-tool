"""网页自动化执行器：基于 DrissionPage 的浏览器会话管理。

为什么用浏览器级 Chromium 对象，而不是 ChromiumPage（实测踩过的坑）：
- ChromiumPage 只代表单个标签。一旦做多标签操作就报
  「该标签页已有非MixTab版本」，必须改 Settings 才能绕。
- get_tab(n) 的序号是**倒序**（1 = 最新创建的那个），拿它做
  「按序号关闭标签」必然关错，所以本模块不提供按序号关闭。
- 关掉当前标签后 page 对象直接断连，后续操作全是 PageDisconnectedError。
改用 Chromium（浏览器）+ MixTab（标签）两层模型后，实测
打开 / 新标签 / 关当前 / 关其他 / 退出 全部正常。

会话为什么是单例：一个流程里「打开网址 → 找图点击 → 关闭浏览器」必须操作
同一个浏览器。每步各起一个既慢，又会丢掉登录态和 Cookie。

启动模式只在**首次启动浏览器**时生效：浏览器已经开着时再选别的模式，
沿用现有实例并在日志里说明，而不是偷偷把浏览器杀掉重建（那样会丢状态）。
"""
from __future__ import annotations

import threading

_LOCK = threading.RLock()
_browser = None            # Chromium 实例
_mode = ""                 # 当前实例的启动模式
_import_error = ""         # DrissionPage 不可用的原因（只记一次）

# 启动/连接模式：值 -> 显示名
LAUNCH_MODES = {
    "front": "前台显示",
    "headless": "无头模式（不显示窗口）",
    "background": "后台静默（窗口移出屏幕）",
    "attach": "接管已打开的浏览器（端口）",
}
DEFAULT_MODE = "front"

# 关闭标签的作用范围：值 -> 显示名
TAB_SCOPES = {
    "current": "关闭当前标签",
    "others": "关闭除当前外的其他标签",
    "match": "关闭网址/标题包含指定文字的标签",
}


def _import_drission():
    """惰性导入：DrissionPage 体积不小，不该拖慢程序启动。

    返回 (Chromium, ChromiumOptions, 连接断开类异常元组) 或抛 ImportError。
    连接断开类异常（PageDisconnectedError 等）用于识别「浏览器被手动关掉/崩溃」
    导致的僵尸实例——这是重复打开网址报错的主要根因。
    """
    from DrissionPage import Chromium, ChromiumOptions
    from DrissionPage.errors import (BrowserConnectError, ContextLostError,
                                     PageDisconnectedError, TargetNotFoundError)
    return (Chromium, ChromiumOptions,
            (PageDisconnectedError, BrowserConnectError, ContextLostError,
             TargetNotFoundError))


def _conn_errors() -> tuple:
    """连接断开类异常元组；DrissionPage 不可用时返回空元组。"""
    try:
        return _import_drission()[2]
    except Exception:
        return ()


def is_available() -> tuple[bool, str]:
    """DrissionPage 是否可用。返回 (可用?, 不可用时的人类可读原因)。"""
    global _import_error
    try:
        _import_drission()
    except ImportError as e:
        _import_error = str(e)
        return False, f"缺少 DrissionPage 库：{e}"
    except Exception as e:                       # 打包后可能缺子模块或数据文件
        _import_error = str(e)
        return False, f"DrissionPage 加载失败：{type(e).__name__}: {e}"
    _import_error = ""
    return True, ""


def _build_options(mode: str):
    """按启动模式构造 ChromiumOptions。

    front（前台显示）：默认最大化窗口，方便用户直接看到页面；
    headless / background：不干扰屏幕，也不最大化（最大化对无头无意义）。
    attach 模式不在此构造（它直接 Chromium(端口) 接管，见 get_browser）。
    """
    _, ChromiumOptions, _ = _import_drission()
    co = ChromiumOptions()
    if mode == "headless":
        co.headless(True)
    elif mode == "background":
        # 不开无头：保留真实浏览器（很多站点会检测无头），只把窗口挪到屏幕外
        co.set_argument("--window-position=-32000,-32000")
    else:
        # front 前台显示：默认最大化，用户可手动调整窗口大小
        co.set_argument("--start-maximized")
    return co


def _parse_attach_port(attach_port) -> int:
    """解析接管端口：合法返回 int，否则抛 ValueError（带人话原因）。"""
    if attach_port in (None, ""):
        raise ValueError("未填写接管端口")
    try:
        port = int(str(attach_port).strip())
    except (TypeError, ValueError):
        raise ValueError(f"接管端口不是有效数字：{attach_port}")
    if not (0 < port <= 65535):
        raise ValueError(f"接管端口超出范围（1~65535）：{port}")
    return port


def _address_matches_port(address: str, port: int) -> bool:
    """Chromium.address（形如 '127.0.0.1:9333'）是否就是目标端口。"""
    try:
        return str(address or "").rsplit(":", 1)[-1] == str(port)
    except Exception:
        return False


def _browser_alive() -> bool:
    """当前单例是否仍然可操作。

    判断标准：访问 tabs_count（走 CDP）不抛异常。用户手动关掉浏览器窗口、
    浏览器崩溃、或调试端口被回收后，DrissionPage 对象还在但内部连接已断，
    任何操作都会抛 PageDisconnectedError——这种「僵尸实例」必须被识别出来，
    否则重复执行「打开网址」会拿旧实例继续操作而报错。
    """
    global _browser
    if _browser is None:
        return False
    try:
        _browser.tabs_count
        return True
    except Exception:
        return False


def _reset_browser() -> None:
    """清空浏览器单例（不尝试 quit：实例可能已经死了，quit 反而抛异常）。"""
    global _browser, _mode
    _browser, _mode = None, ""


def get_browser(mode: str = DEFAULT_MODE, attach_port=None):
    """取浏览器实例；没有或已失效就按 mode 建立。线程安全。

    mode 只在首次建立时生效，已有实例时沿用（见模块 docstring）：
    - front/headless/background：用 ChromiumOptions 启动新浏览器；
    - attach：接管「已用 --remote-debugging-port=N 手动打开」的浏览器，
      等价于 Chromium(N)（文档：端口空闲时也会在该端口自动启动一个）。
      接管中的实例仍允许被之后的 open 步骤沿用（不重复开进程）。
    已有实例但已失效（被手动关闭/崩溃）时，清掉后重新建立，避免继续用僵尸实例。
    已是 attach 会话但目标端口不同 → 断开旧接管后连接新端口（用户手动开的浏览器
    窗口保留不退出）；非 attach 会话切到 attach → 先关闭自启浏览器再接管。
    """
    global _browser, _mode
    with _LOCK:
        port = _parse_attach_port(attach_port) if mode == "attach" else None
        if _browser is not None and not _browser_alive():
            _reset_browser()
        if _browser is not None:
            if mode == "attach" and port is not None \
                    and not _address_matches_port(_browser.address, port):
                # 切到另一个端口的接管目标：先退出当前会话再连新的。
                # attach 会话只断开（不关用户浏览器）；自启会话才真正 quit。
                if _mode != "attach":
                    try:
                        _browser.quit()
                    except Exception:
                        pass
                _reset_browser()
            else:
                return _browser
        Chromium, _, _ = _import_drission()
        if mode == "attach":
            _browser = Chromium(port)   # 接管该端口已有浏览器；空闲则自动启动
            _mode = "attach"
            return _browser
        _browser = Chromium(_build_options(mode))
        _mode = mode
        return _browser


def active_mode() -> str:
    """当前浏览器实例的启动模式（未启动时返回空串）。"""
    with _LOCK:
        return _mode if _browser is not None else ""


def is_current(browser) -> bool:
    """browser 是否就是当前活动会话的实例。

    供「按浏览器变量关闭」判断：流程变量里存的浏览器对象与 web_actors 单例
    是同一实例时，可直接复用 close_browser() 的关闭语义（attach 只断连 /
    自启才退出 / 僵尸清理）；否则属于已被替换或已失效的旧引用。
    """
    with _LOCK:
        return _browser is not None and browser is _browser


def close_browser() -> bool:
    """结束浏览器会话并清空单例。返回是否真的结束了一个活动会话。

    对 attach（接管）会话只断开连接、保留浏览器窗口——那是用户手动打开的浏览器，
    quit 会把用户正用着的窗口一起关掉；文档亦言明程序结束不应关闭被接管的浏览器。
    对已失效的僵尸实例（浏览器被手动关掉/崩溃）只清空单例、返回 False——
    没有活动实例可关，调用方应显示「浏览器未启动，无需关闭」而非「已关闭」。
    """
    global _browser, _mode
    with _LOCK:
        if _browser is None:
            return False
        alive = _browser_alive()
        if alive and _mode != "attach":
            try:
                _browser.quit()
            except Exception:                        # 浏览器可能已被用户手工关掉
                pass
        _browser, _mode = None, ""
        return alive


def open_url(url: str, mode: str = DEFAULT_MODE, new_tab: bool = False,
             timeout: float = 20.0, wait_after: float = 0.0,
             attach_port=None) -> tuple[bool, str]:
    """打开网址。返回 (成功?, 结果描述)。

    url 为空时只启动浏览器不导航；缺少 http(s):// 前缀时自动补 https://。
    mode=attach 时 attach_port 必填：接管该端口已打开的浏览器（见 get_browser）。
    """
    url = (url or "").strip()
    if not url:
        return False, "未填写网址"
    if not url.lower().startswith(("http://", "https://", "file://", "about:")):
        url = "https://" + url

    if mode == "attach":
        try:
            attach_port = _parse_attach_port(attach_port)
        except ValueError as e:
            return False, f"接管浏览器：{e}"

    with _LOCK:
        ok, why = is_available()
        if not ok:
            return False, why
        before_mode = active_mode()      # "" = 还没启动，本次会按 mode 新建
        try:
            browser = (get_browser(mode) if mode != "attach"
                       else get_browser(mode, attach_port))
        except Exception as e:
            return False, f"浏览器启动失败：{type(e).__name__}: {e}"

        reused = bool(before_mode) and before_mode != mode
        if mode == "attach":
            note = (f"（接管端口 {attach_port} 的浏览器）" if not before_mode
                    else f"（沿用端口 {attach_port} 的浏览器会话）")
        elif reused:
            note = (f"（沿用已启动的浏览器，{LAUNCH_MODES.get(active_mode(), '?')}）")
        else:
            note = ""
        try:
            if new_tab:
                tab = browser.new_tab(url)
            else:
                tab = browser.latest_tab
                tab.get(url, timeout=max(float(timeout or 0), 1.0))
        except _conn_errors():
            # 旧实例连接已断开（浏览器被手动关闭/崩溃）：清掉单例重新建立再试一次
            _reset_browser()
            try:
                browser = (get_browser(mode) if mode != "attach"
                           else get_browser(mode, attach_port))
                if new_tab:
                    tab = browser.new_tab(url)
                else:
                    tab = browser.latest_tab
                    tab.get(url, timeout=max(float(timeout or 0), 1.0))
            except Exception as e:
                return False, f"打开失败：{type(e).__name__}: {e}"
        except Exception as e:
            return False, f"打开失败：{type(e).__name__}: {e}"

        if wait_after and wait_after > 0:
            import time
            time.sleep(min(wait_after, 60.0))

        try:
            title = (tab.title or "").strip()
        except Exception:
            title = ""
        where = "新标签" if new_tab else "当前标签"
        return True, f"{where}已打开 {url}" + (f" · {title}" if title else "") + note


def close_tab(scope: str = "current", match_text: str = "") -> tuple[bool, str]:
    """关闭标签页。返回 (成功?, 结果描述)。

    scope: current / others / match（见 TAB_SCOPES）。
    """
    with _LOCK:
        if _browser is None or not _browser_alive():
            # 浏览器未启动或已失效（被手动关闭）：幂等返回成功，并清掉僵尸单例
            if _browser is not None:
                _reset_browser()
            return True, "浏览器未启动，无需关闭"
        try:
            total = _browser.tabs_count
            if total <= 0:
                attached = active_mode() == "attach"
                close_browser()
                tail = "已断开接管浏览器（窗口保留）" if attached else "已退出浏览器"
                return True, f"没有可关闭的标签，{tail}"

            if scope == "others":
                cur = _browser.latest_tab
                _browser.close_tabs(cur.tab_id, others=True)
                return True, f"已关闭其他标签（{total} → {_browser.tabs_count}）"

            if scope == "match":
                text = (match_text or "").strip()
                if not text:
                    return False, "未填写匹配文字"
                targets = _match_tabs(text)
                if not targets:
                    return True, f"没有网址/标题包含「{text}」的标签"
                _browser.close_tabs(targets)
                return True, f"已关闭 {len(targets)} 个匹配「{text}」的标签"

            # current
            cur = _browser.latest_tab
            cur.close()
            left = _browser.tabs_count
            if left <= 0:
                attached = active_mode() == "attach"
                close_browser()
                tail = "已断开接管浏览器（窗口保留）" if attached else "浏览器已退出"
                return True, f"已关闭当前标签（这是最后一个，{tail}）"
            return True, f"已关闭当前标签（{total} → {left}）"
        except Exception as e:
            return False, f"关闭标签失败：{type(e).__name__}: {e}"


def _match_tabs(text: str) -> list:
    """按网址或标题包含 text 匹配标签（大小写不敏感），返回标签对象列表。"""
    needle = (text or "").strip().lower()
    if not needle:
        return []
    out = []
    for t in _browser.get_tabs():
        try:
            hay = f"{t.url or ''} {t.title or ''}".lower()
        except Exception:
            continue
        if needle in hay:
            out.append(t)
    return out


def shutdown() -> None:
    """程序退出时收尾：关掉还开着的浏览器。"""
    try:
        close_browser()
    except Exception:
        pass
