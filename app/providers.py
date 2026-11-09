# -*- coding: utf-8 -*-
"""多服务商协议适配：OpenAI 兼容 / Anthropic 原生。

只有两件事不同：**请求体**和**流式事件**。业务侧（工具解析、记忆块、上下文压缩）
完全复用，所以这里只做最薄的一层转换。

设计取舍：
- 两种协议都下发原生 tools；Anthropic 的 tool_use / tool_result 由流式事件解析后
  收敛成项目统一的调用结构（见 anthropic_tool_event）。工具结果仍以普通 user 文本
  回灌，历史里不会留下悬空的 tool_use，所以不需要严格成对回传。
- 富内容（图片）在 _to_anthropic_content 里换算：Anthropic 不认 OpenAI 的
  image_url，必须拆成 base64 image 块；其余块降级成文本，绝不 json.dumps 成字符串。
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


def _to_anthropic_content(content):
    """OpenAI 风格 content -> Anthropic content。

    字符串原样返回；列表逐块转换：text 直接映射，image_url 的 data URL 拆成
    base64 image 块（Anthropic 不认 image_url）。认不出的块降级成文本，
    避免整条消息被 json.dumps 成「看得见但读不懂」的字符串。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        t = part.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif t == "image_url":
            src = part.get("image_url")
            url = str((src.get("url") if isinstance(src, dict) else src) or "")
            if url.startswith("data:"):
                head, _, data = url.partition(",")
                mime = (head[5:].split(";")[0] or "image/png").strip()
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": data}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif t == "image" and isinstance(part.get("source"), dict):
            blocks.append(part)
        else:
            blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)})
    return blocks


def _split_system(messages):
    """Anthropic 的 system 是独立字段，不在 messages 里；图片块要换算成原生结构。"""
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
            conv.append({"role": role, "content": _to_anthropic_content(content)})
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


def _mark_last_cacheable(conv):
    """在最后一条消息的最后一个内容块打缓存断点。

    Anthropic 的缓存按「tools → system → messages」的前缀匹配；system 打点
    已经覆盖了 tools + system，再在对话末尾打一个断点，就能让整段历史前缀
    一起复用（下一轮请求会命中这一轮的写点）。原地修改 conv，使用前已拷贝。
    """
    for m in reversed(conv):
        content = m.get("content")
        if isinstance(content, str):
            if not content.strip():
                continue
            m["content"] = [{"type": "text", "text": content,
                             "cache_control": {"type": "ephemeral"}}]
            return True
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                content[-1] = dict(last)
                content[-1]["cache_control"] = {"type": "ephemeral"}
                return True
            continue
    return False


def anthropic_body(model, messages, max_tokens=0, think=None, stream=True,
                   tools=None, tool_choice=None, temperature=None, cache=False):
    sys_text, conv = _split_system(messages)
    mt = int(max_tokens or 0) or DEFAULT_MAX_TOKENS
    body = {"model": model, "max_tokens": mt, "messages": conv}
    if temperature is not None:
        try:
            body["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    if stream:
        body["stream"] = True
    if sys_text:
        if cache:
            # 稳定段打点：命中部分约按 0.1x 计费，写入约 1.25x
            body["system"] = [{"type": "text", "text": sys_text,
                               "cache_control": {"type": "ephemeral"}}]
        else:
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
    if cache and body["messages"]:
        _mark_last_cacheable(body["messages"])
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
