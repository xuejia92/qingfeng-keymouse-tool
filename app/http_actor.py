"""网络请求步骤的执行：用标准库 urllib 发起 GET/POST 请求。

不引入第三方依赖（requests 不在运行时依赖里），urllib 已覆盖本模块全部需求：
- 自定义请求头（含 User-Agent、Cookie）
- 系统代理（http / https 走同一本地代理）
- 超时控制
- 读取响应状态码 / 响应头 / Set-Cookie / 响应体（文本或图片）

目录规则：结果类型为 image 时，把响应体保存到
<程序目录>/templates/http/（不存在自动创建），与 templates/（找图模板）同根，
都随程序目录走（打包版 = exe 同级）。
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request

from .config import TEMPLATE_DIR

# 图片下载保存目录：<程序目录>/templates/http/
HTTP_IMAGE_DIR = os.path.join(TEMPLATE_DIR, "http")

# Content-Type -> 扩展名（图片保存时据此命名，未知类型回退 .png）
_IMAGE_EXTS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/x-icon": ".ico",
    "image/tiff": ".tiff", "image/svg+xml": ".svg",
}


class HttpError(Exception):
    """网络请求步骤的可预期失败（超时、连接错误、HTTP 异常等）。"""


def parse_headers(text: str) -> dict[str, str]:
    """把「每行一条 Name: Value」的请求头文本解析成 dict；空行/非法行忽略。

    同一名字重复出现时后者覆盖前者；名字为空（缺冒号）的行直接跳过。
    """
    headers: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        if name:
            headers[name] = value
    return headers


def _response_headers(resp) -> dict:
    """把 urllib 响应头转成 dict；同名头（如 Set-Cookie）用逗号拼接保留。"""
    result: dict[str, str] = {}
    try:
        for key in resp.headers.keys():
            vals = resp.headers.get_all(key)
            result[key] = ", ".join(str(v) for v in vals) if vals else ""
    except Exception:
        # 兜底：某些非常规响应头对象不支持 get_all，退化为键值对
        try:
            for k, v in resp.headers.items():
                result[k] = v
        except Exception:
            pass
    return result


def _response_cookies(resp) -> dict:
    """从响应头的 Set-Cookie 解析出 name -> value 字典（只取每条的 name=value 部分）。"""
    cookies: dict[str, str] = {}
    try:
        raw = resp.headers.get_all("Set-Cookie") or []
    except Exception:
        raw = []
    for sc in raw:
        first = str(sc).split(";", 1)[0].strip()
        if "=" in first:
            k, _, v = first.partition("=")
            k = k.strip()
            if k:
                cookies[k] = v.strip()
    return cookies


def _decode_body(data: bytes, content_type: str) -> str:
    """按 Content-Type 的 charset 解码文本；失败依次回退 utf-8 / gb18030 / latin-1。"""
    charset = ""
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if m:
        charset = m.group(1)
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _save_image(data: bytes, content_type: str) -> str:
    """把响应体字节保存为图片文件，返回绝对路径。"""
    os.makedirs(HTTP_IMAGE_DIR, exist_ok=True)
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    ext = _IMAGE_EXTS.get(ct, ".png")
    name = f"http_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
    path = os.path.join(HTTP_IMAGE_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _build_opener(use_proxy: bool, proxy: str) -> urllib.request.OpenerDirector:
    """构造带代理（可选）的 opener。"""
    handlers: list = []
    if use_proxy:
        proxy = (proxy or "").strip()
        if proxy:
            if "://" not in proxy:
                proxy = "http://" + proxy
            handlers.append(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def perform_request(*, url: str, method: str = "get", headers: dict | None = None,
                    body: str = "", cookie: str = "", result_type: str = "text",
                    user_agent: str = "", timeout: float = 5.0,
                    use_proxy: bool = True, proxy: str = "127.0.0.1:7897") -> dict:
    """执行一次网络请求，返回 {status, headers, cookies, content}。

    content：result_type="text" 时为解码后的文本；"image" 时为图片保存路径。
    连接失败 / 超时 / HTTP 错误一律抛 HttpError（含可读原因）。
    """
    method = (method or "get").strip().lower()
    if method not in ("get", "post"):
        raise HttpError(f"不支持的请求方法: {method}")

    req_headers = dict(headers or {})
    if user_agent and "User-Agent" not in req_headers:
        req_headers["User-Agent"] = user_agent
    if cookie and "Cookie" not in req_headers:
        req_headers["Cookie"] = cookie

    data = body.encode("utf-8") if method == "post" and body else None

    req = urllib.request.Request(url, data=data, headers=req_headers,
                                 method=method.upper())
    opener = _build_opener(use_proxy, proxy)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # HTTP 错误（4xx/5xx）也有响应体与状态码，仍返回给用户按状态码分支，
        # 不当作「步骤失败」抛错；只有读不到响应才算失败。
        resp = e
    except TimeoutError as e:
        # 连接/读取超时：urllib 有时把 socket.timeout 包进 URLError.reason，
        # 有时（getresponse 阶段）直接抛出裸 TimeoutError，两种都要识别。
        raise HttpError(f"请求超时（{timeout:g} 秒）") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, TimeoutError):
            raise HttpError(f"请求超时（{timeout:g} 秒）") from e
        raise HttpError(f"请求失败：{reason}") from e
    except OSError as e:
        # 连接被拒 / DNS 解析失败 / 代理不可达等
        raise HttpError(f"请求失败：{e}") from e
    except Exception as e:
        raise HttpError(f"请求出错：{type(e).__name__}: {e}") from e

    try:
        status = int(getattr(resp, "code", 0) or 0)
        resp_headers = _response_headers(resp)
        cookies = _response_cookies(resp)
        try:
            content_type = resp.headers.get("Content-Type", "") or ""
        except Exception:
            content_type = ""
        raw = resp.read()
    except Exception as e:
        raise HttpError(f"读取响应失败：{type(e).__name__}: {e}") from e
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if result_type == "image":
        content = _save_image(raw, content_type)
    else:
        content = _decode_body(raw, content_type)

    return {"status": status, "headers": resp_headers,
            "cookies": cookies, "content": content}
