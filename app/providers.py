# -*- coding: utf-8 -*-
"""多服务商协议适配：OpenAI 兼容 / Anthropic 原生。

只有两件事不同：**请求体**和**流式事件**。业务侧（工具解析、记忆块、上下文压缩）
完全复用，所以这里只做最薄的一层转换。

设计取舍：
- Anthropic 分支**不下发原生 tools**，强制走项目自己的 ```tool 文本协议。
  原因：原生 tool_use / tool_result 必须严格成对回传，而本项目的历史消息里
  工具结果是以普通 user 文本追加的，改造面很大且容易 400。
  文本协议在所有服务商上行为一致，是更稳的选择。
- Anthropic 的 usage 字段与 OpenAI 不同，这里统一映射，避免用量统计错位。
- 失败一律抛给调用方处理，不在这里吞异常。
"""
import json

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def kind_of(url, cfg_kind=""):
    """判定协议类型：优先看显式配置，其次按地址猜。"""
    k = str(cfg_kind or "").strip().lower()
    if k in ("openai", "anthropic"):
        return k
    return "anthropic" if "anthropic" in str(url or "").lower() else "openai"


def anthropic_url(url):
    """补全 Anthropic 的 /v1/messages 端点。"""
    base = str(url or "").strip().rstrip("/")
    if not base:
        base = "https://api.anthropic.com"
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def anthropic_headers(api_key):
    return {
        "x-api-key": api_key or "",
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json; charset=utf-8",
    }


def _split_system(messages):
    """Anthropic 的 system 是独立字段，不在 messages 里。"""
    sys_text, conv = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            text = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False)
            if text.strip():
                sys_text.append(text)
        elif role in ("user", "assistant"):
            conv.append({"role": role, "content": content if isinstance(content, str)
                         else json.dumps(content, ensure_ascii=False)})
    return "\n\n".join(sys_text), conv


def _thinking(think, max_tokens):
    """把项目里的思考参数映射成 Anthropic 的 thinking 结构。"""
    if not think:
        return None
    enabled = False
    if isinstance(think, dict):
        t = think.get("thinking") or {}
        enabled = (t.get("type") == "enabled") or bool(think.get("reasoning_effort"))
    if not enabled:
        return None
    budget = max(1024, min(int(max_tokens or DEFAULT_MAX_TOKENS) // 2, 8192))
    return {"type": "enabled", "budget_tokens": budget}


def anthropic_body(model, messages, max_tokens=0, think=None, stream=True,
                   tools=None, tool_choice=None):
    sys_text, conv = _split_system(messages)
    mt = int(max_tokens or 0) or DEFAULT_MAX_TOKENS
    body = {"model": model, "max_tokens": mt, "messages": conv}
    if stream:
        body["stream"] = True
    if sys_text:
        body["system"] = sys_text
    th = _thinking(think, mt)
    if th:
        body["thinking"] = th
    if tools:
        _tools = [t for t in (_to_anthropic_tool(x) for x in tools) if t]
        if _tools:
            body["tools"] = _tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    return body


def _to_anthropic_tool(schema):
    """OpenAI 风格 function schema -> Anthropic tools 条目。

    项目里的 schema 统一是 {type:"function", function:{name, description, parameters}}；
    Anthropic 要的是 {name, description, input_schema}。
    """
    if not isinstance(schema, dict):
        return None
    fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    params = fn.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": str(fn.get("description") or ""),
        "input_schema": params,
    }


def anthropic_tool_event(obj):
    """Anthropic 流式 tool_use 事件 -> 统一增量。

    返回 {"kind": "start"|"delta"|"stop", "index": int, "id": str,
          "name": str, "json": str}；不是工具事件返回 None。
    """
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    try:
        idx = int(obj.get("index") or 0)
    except (TypeError, ValueError):
        idx = 0
    if t == "content_block_start":
        block = obj.get("content_block") or {}
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return None
        return {"kind": "start", "index": idx,
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""), "json": ""}
    if t == "content_block_delta":
        d = obj.get("delta") or {}
        if not isinstance(d, dict) or d.get("type") != "input_json_delta":
            return None
        return {"kind": "delta", "index": idx, "id": "", "name": "",
                "json": str(d.get("partial_json") or "")}
    if t == "content_block_stop":
        # content_block_stop 也用于 text/thinking 块；由调用方按 index 是否在
        # native_calls 里决定是否处理，这里统一返回 stop。
        return {"kind": "stop", "index": idx, "id": "", "name": "", "json": ""}
    return None


def is_done(obj):
    return isinstance(obj, dict) and obj.get("type") == "message_stop"


def delta(obj):
    """把一条 Anthropic 流事件转成 (text, reasoning, finish_reason, usage)。

    不相关的事件返回 None，调用方直接跳过。
    """
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "content_block_delta":
        d = obj.get("delta") or {}
        dt = d.get("type")
        if dt == "text_delta":
            return (d.get("text") or "", "", None, None)
        if dt == "thinking_delta":
            return ("", d.get("thinking") or "", None, None)
        return None
    if t == "message_delta":
        d = obj.get("delta") or {}
        fr = d.get("stop_reason") or ""
        mapped = {"max_tokens": "length", "end_turn": "stop",
                  "tool_use": "tool_calls"}.get(fr, fr or None)
        return ("", "", mapped, obj.get("usage"))
    if t == "message_start":
        return ("", "", None, ((obj.get("message") or {}).get("usage")))
    return None


def usage_of(raw, prompt_hint=0):
    """Anthropic usage -> OpenAI 风格，保证用量统计口径一致。"""
    if not isinstance(raw, dict):
        return {}
    pin = int(raw.get("input_tokens") or raw.get("prompt_tokens") or prompt_hint or 0)
    pout = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
    if not pin and not pout:
        return {}
    return {"prompt_tokens": pin, "completion_tokens": pout,
            "total_tokens": pin + pout}


def text_of(data):
    """非流式响应 -> 纯文本。"""
    if not isinstance(data, dict):
        return ""
    out = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "".join(out)
