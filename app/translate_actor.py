"""谷歌翻译客户端（免 API Key 的网页接口）。

思路参考 https://github.com/poemdistance/google-translate 的「截图 → OCR → 翻译」，
但不复用它的代码，原因有两点：

1. 它默认的 `translate.google.cn` 域名**已经停用**（实测返回 404），
   直接用会必然失败；`translate.google.com` 与 `translate.googleapis.com` 仍可用。
2. 它依赖 googletrans / PySocks / sysv_ipc 等第三方库（sysv_ipc 还是 Linux 专有），
   本项目要能打进 PyInstaller exe，依赖越少越好。

所以这里只用标准库 urllib 实现，零新增依赖。

接口行为（本机实测）：
- GET `/translate_a/single?client=gtx&dt=t&sl=auto&tl=zh-CN&q=<文本>`
- 返回 `[[[译文分段, 原文分段, ...], ...], null, 检测到的源语言, ...]`
- 每个分段自带尾随换行符，因此 `"".join(分段译文)` 能原样保留原文的换行结构
  （OCR 的多行结果直接送进来，译文行数与原文一致）
- 单次 GET 实测 4400+ 字符正常；再长则按行切分多次请求后拼接
- 语言代码非法时接口不报错，而是原样返回输入（故语言用下拉框限定，避免手填错）
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import (TRANSLATE_LANGUAGES, TRANSLATE_TARGET_LANGUAGES)

# 语言代码 -> 界面显示名（表定义在 config.py，与其它步骤的选项表放在一起）
LANGUAGES = TRANSLATE_LANGUAGES
TARGET_LANGUAGES = TRANSLATE_TARGET_LANGUAGES

DEFAULT_SOURCE = "auto"
DEFAULT_TARGET = "zh-CN"

# 多域名回退：前一个不可用（超时/连接失败/被墙）时依次尝试后面的。
# 注意 translate.google.cn 已停用，不要加回来。
ENDPOINTS: tuple[str, ...] = (
    "https://translate.googleapis.com/translate_a/single",
    "https://translate.google.com/translate_a/single",
)

# 单次请求的文本上限（字符）。实测 4400+ 正常，留出余量避免 URL 过长被拒。
CHUNK_LIMIT = 3500

# 整轮重试前的等待秒数（第一轮所有域名都失败且错误可重试时）
RETRY_DELAY = 0.8

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class TranslateError(Exception):
    """翻译过程中的可预期错误（网络不可用、返回格式异常等）。

    retryable 标记该错误是否值得重试（网络抖动/超时/429/5xx）。
    语言代码写错之类的错误重试也没用，标记为 False 避免白等。
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def lang_name(code: str) -> str:
    """语言代码转显示名；未知代码原样返回。"""
    return LANGUAGES.get((code or "").strip(), (code or "").strip())


def normalize_proxy(proxy: str) -> str:
    """规范化代理地址：`127.0.0.1:7890` -> `http://127.0.0.1:7890`。

    空串表示不使用代理。带协议前缀的原样返回（含 socks5://，由 urllib 的
    ProxyHandler 决定是否支持；不支持时会在请求阶段报错并给出提示）。
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    return "http://" + proxy


def translate(text: str, source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET,
              timeout: float = 10.0, proxy: str = "") -> tuple[bool, dict | None, str]:
    """把 text 翻译成 target 语言。

    返回 (成功?, 结果, 说明)。结果格式：
      {"text": 译文, "source_lang": 检测到的源语言代码,
       "target_lang": 目标语言代码, "requests": 实际请求次数}

    多行文本的换行结构会被保留（译文行数与原文一致）。
    文本为空/纯空白视为失败（调用方通常应在此之前就拦截）。
    本模块只用标准库，没有「依赖没装」这种情况，所以不做可用性预检——
    网络不通会在请求阶段报出可读原因。
    """
    raw = text if isinstance(text, str) else str(text or "")
    if not raw.strip():
        return False, None, "待翻译文本为空"

    src = (source or DEFAULT_SOURCE).strip() or DEFAULT_SOURCE
    dst = (target or DEFAULT_TARGET).strip() or DEFAULT_TARGET
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 10.0
    if timeout <= 0:
        timeout = 10.0
    proxy_url = normalize_proxy(proxy)

    parts: list[str] = []
    detected = ""
    used = 0
    try:
        for chunk in split_chunks(raw):
            piece, lang = _translate_chunk(chunk, src, dst, timeout, proxy_url)
            parts.append(piece)
            detected = detected or lang
            used += 1
    except TranslateError as e:
        return False, None, str(e)
    except Exception as e:  # 兜底：不让未预期异常冒泡到流程引擎
        return False, None, f"翻译失败：{type(e).__name__}: {e}"

    result = {
        "text": "".join(parts),
        "source_lang": detected or src,
        "target_lang": dst,
        "requests": used,
    }
    return True, result, f"已翻译 {len(raw)} 字符（{lang_name(detected or src)} → {lang_name(dst)}）"


def split_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """把长文本按行切分成若干块，每块不超过 limit 字符。

    按行切而不是硬切：整句送进接口才能译得准，硬切会把词/句截断。
    单行本身就超限时只能硬切（罕见，OCR 一般不会出现这么长的单行）。
    空文本返回空列表。
    """
    text = text or ""
    if not text.strip():
        return []
    if limit <= 0:
        limit = CHUNK_LIMIT
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        # 单行超限：先把已攒的块收掉，再把这行硬切成若干段
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks


def _translate_chunk(chunk: str, source: str, target: str, timeout: float,
                     proxy_url: str) -> tuple[str, str]:
    """翻译单个文本块，返回 (译文, 检测到的源语言)。

    依次尝试 ENDPOINTS 里的域名；全部失败且失败原因属于「网络抖动」时，
    等待 RETRY_DELAY 秒后把整轮再来一遍（自动化流程里因一次抖动就终止流程
    代价太大）。两轮都失败才抛 TranslateError。
    """
    params = {
        "client": "gtx",
        "dt": "t",
        "sl": source,
        "tl": target,
        "q": chunk,
    }
    query = urllib.parse.urlencode(params)
    last_err = ""
    last_retryable = False

    for round_no in range(2):
        for endpoint in ENDPOINTS:
            url = f"{endpoint}?{query}"
            try:
                body = _http_get(url, timeout, proxy_url)
            except TranslateError as e:
                last_err, last_retryable = str(e), e.retryable
                continue
            try:
                data = json.loads(body)
            except (ValueError, TypeError) as e:
                last_err = f"返回内容不是合法 JSON（{e}）"
                last_retryable = True
                continue
            text, detected = _parse_response(data)
            if text is None:
                last_err = "返回结构不符合预期"
                last_retryable = True
                continue
            return text, detected
        # 第一轮全部失败：只有「值得重试」的错误才再等一次
        if round_no == 0 and last_retryable:
            time.sleep(RETRY_DELAY)
            continue
        break

    raise TranslateError(last_err or "谷歌翻译请求失败", retryable=last_retryable)


def _http_get(url: str, timeout: float, proxy_url: str) -> str:
    """发 GET 请求并返回响应文本；失败抛 TranslateError（附可读原因）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url,
                                                     "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        # 429（限流）与 5xx（服务端故障）属于临时性问题，值得重试
        retryable = e.code == 429 or e.code >= 500
        raise TranslateError(f"谷歌翻译返回 HTTP {e.code}（{e.reason}）",
                             retryable=retryable) from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if proxy_url:
            hint = f"（已配置代理 {proxy_url}，请确认代理在运行）"
        else:
            hint = "（若本机无法直连谷歌，请在步骤里填写代理）"
        raise TranslateError(f"无法连接谷歌翻译：{reason}{hint}",
                             retryable=True) from e
    except TimeoutError as e:
        raise TranslateError(f"谷歌翻译请求超时（{timeout:g} 秒）",
                             retryable=True) from e
    except Exception as e:
        raise TranslateError(f"请求失败：{type(e).__name__}: {e}") from e


def _parse_response(data) -> tuple[str | None, str]:
    """解析接口返回，返回 (译文, 检测到的源语言)。

    结构：[[[译文, 原文, ...], ...], null, "检测到的源语言", ...]
    每个分段自带尾随换行，直接拼接即可保留原文的行结构。

    解析不出任何文本（结构不符预期、或分段列表为空）时返回 (None, "")，
    由调用方换下一个域名重试——空响应往往就是某个域名出了问题。
    待翻译文本为空的情况在 translate() 里已提前拦截，所以「解析出空文本」
    不可能是正常结果。
    """
    if not isinstance(data, (list, tuple)) or not data:
        return None, ""
    segments = data[0]
    if not isinstance(segments, (list, tuple)):
        return None, ""
    buf: list[str] = []
    for seg in segments:
        if not isinstance(seg, (list, tuple)) or not seg:
            continue
        piece = seg[0]
        if isinstance(piece, str):
            buf.append(piece)
    if not buf:
        return None, ""
    detected = data[2] if len(data) > 2 and isinstance(data[2], str) else ""
    return "".join(buf), detected
