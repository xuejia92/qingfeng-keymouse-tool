"""DeepSeek 对话步骤：调用 DeepSeek API（OpenAI 兼容）做一次对话补全。

复用 app/http_actor.py 的标准库 urllib 传输层（代理 / 超时 / 错误处理），
本模块只负责拼 OpenAI 格式的请求体、解析响应（含流式 SSE 与 thinking 思考模式）。
不依赖 openai 第三方 SDK，避免打包体积膨胀。

参考（等价实现）：
    client = OpenAI(api_key=..., base_url="https://api.deepseek.com")
    client.chat.completions.create(model=..., messages=[...], stream=False,
        reasoning_effort="high", extra_body={"thinking": {"type": "enabled"}})
"""
from __future__ import annotations

import json
import os

from . import http_actor

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
# 下拉默认候选（用户仍可手动输入其它模型名）
MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]


class DeepSeekError(Exception):
    """DeepSeek 对话步骤的可预期失败（缺 Key、空提问、API 报错、网络失败）。"""


def _build_messages(system: str, question: str) -> list[dict]:
    """组装 messages：system（可选）+ user（必填）。"""
    msgs: list[dict] = []
    if system and system.strip():
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": question})
    return msgs


def _parse_non_stream(data: dict) -> dict:
    """解析非流式响应：取 message.content（回答）与 message.reasoning_content（思考）。"""
    content, reasoning = "", ""
    try:
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message", {}) or {}
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
    except (IndexError, AttributeError):
        pass
    return {"content": content, "reasoning": reasoning}


def _parse_stream(text: str) -> dict:
    """解析流式（SSE）响应：累加 delta.content 与 delta.reasoning_content。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        try:
            choices = obj.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
        except (IndexError, AttributeError):
            continue
        if delta.get("content"):
            content_parts.append(delta["content"])
        if delta.get("reasoning_content"):
            reasoning_parts.append(delta["reasoning_content"])
    return {"content": "".join(content_parts),
            "reasoning": "".join(reasoning_parts)}


def chat(*, api_key: str, model: str, system: str, question: str,
         thinking: bool = False, stream: bool = False, timeout: float = 60.0,
         use_proxy: bool = True, proxy: str = "127.0.0.1:7897",
         base_url: str = DEFAULT_BASE_URL) -> dict:
    """执行一次 DeepSeek 对话，返回 {"content": 回答, "reasoning": 思考过程}。

    失败抛 DeepSeekError（含可读原因）。
    """
    if not api_key or not api_key.strip():
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DeepSeekError(
            "未设置 API Key（请在步骤里填写，或设置环境变量 DEEPSEEK_API_KEY）")
    if not question or not question.strip():
        raise DeepSeekError("提问内容为空")

    model = (model or DEFAULT_MODEL).strip()
    body: dict = {
        "model": model,
        "messages": _build_messages(system, question),
        "stream": bool(stream),
    }
    if thinking:
        # 与参考代码一致：思考模式同时下发 thinking.enabled 与 reasoning_effort=high
        body["reasoning_effort"] = "high"
        body["thinking"] = {"type": "enabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"

    try:
        result = http_actor.perform_request(
            url=url, method="post", headers=headers,
            body=json.dumps(body, ensure_ascii=False),
            result_type="text", user_agent="",
            timeout=timeout, use_proxy=use_proxy, proxy=proxy)
    except http_actor.HttpError as e:
        raise DeepSeekError(str(e)) from e

    if result["status"] >= 400:
        detail = result["content"]
        try:
            obj = json.loads(detail)
            err = obj.get("error", {}) or {}
            detail = err.get("message") or err.get("type") or detail
        except (ValueError, AttributeError):
            pass
        raise DeepSeekError(f"API 错误（{result['status']}）：{detail[:300]}")

    if stream:
        return _parse_stream(result["content"])
    try:
        data = json.loads(result["content"])
    except ValueError as e:
        raise DeepSeekError(f"响应不是有效 JSON：{result['content'][:200]}") from e
    return _parse_non_stream(data)
