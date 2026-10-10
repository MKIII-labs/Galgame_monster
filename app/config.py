# -*- coding: utf-8 -*-
"""接口配置：服务商默认值、配置读写（带缓存）、地址补全与思考参数"""
from app.store import CONFIG_FILE, read_json, write_json

API_PROVIDERS = {
    "chat": {
        "url_default": "https://api.deepseek.com/chat/completions",
        "model_default": "deepseek-flash",
        "protocol_default": "openai",
    },
    "vision": {
        "url_default": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "model_default": "qwen-vl-plus",
        "protocol_default": "native",
    },
}


def load_config():
    default = {
        "chat_api_key": "", "chat_url": "", "chat_model": "", "chat_protocol": "", "chat_presets": {},
        "chat_think_mode": "", "chat_think_enabled": False, "chat_think_level": "high",
        "chat_context_limit": 128000, "chat_reserve_output": 4096,
        "chat_balance_url": "",
        "vision_api_key": "", "vision_url": "", "vision_model": "", "vision_protocol": "", "vision_presets": {},
        "vision_use_chat": False, "vision_balance_url": "",
        "search_enabled": False, "search_provider": "bocha", "search_api_key": "",
        "search_url": "", "search_count": 5,
        "search_rewrite": True,          # 用模型把问题改写为多个检索式
        "search_fetch": True,            # 抓取命中网页的正文（仅注入提示，不入库）
        "search_fetch_count": 4,         # 抓正文的条数（1–10）
        "search_fetch_reader": "jina",   # 抓正文方式：jina（免 key）| direct
        "learning_presets": {},
        "theme": "light",
    }
    data = read_json(CONFIG_FILE)
    if not isinstance(data, dict):
        return default
    # 兼容旧版 deepseek_* / dashscope_* 键名
    for old, new in (("deepseek", "chat"), ("dashscope", "vision")):
        for suf in ("api_key", "url", "model", "protocol", "presets"):
            ok = f"{old}_{suf}"
            nk = f"{new}_{suf}"
            if ok in data and not data.get(nk):
                data[nk] = data[ok]
    for key, val in default.items():
        if key not in data:
            data[key] = val
    return data


def save_config(config):
    write_json(CONFIG_FILE, config)


def resolve_endpoint(url):
    url = (url or "").strip()
    if not url:
        return url
    base = url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"

def normalize_content(content):
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return content if content is not None else ""

def get_api_protocol(provider):
    p = API_PROVIDERS[provider]
    return (load_config().get(f"{provider}_protocol", "") or "").strip() or p["protocol_default"]

def get_api_conf(provider):
    cfg = load_config()
    p = API_PROVIDERS[provider]
    key = cfg.get(f"{provider}_api_key", "").strip()
    url = cfg.get(f"{provider}_url", "").strip() or p["url_default"]
    model = cfg.get(f"{provider}_model", "").strip() or p["model_default"]
    if get_api_protocol(provider) == "openai":
        url = resolve_endpoint(url)
    return key, url, model

def effective_think_mode(url):
    """思考参数模式：off / openai / deepseek；配置为空时按接口地址自动判断"""
    mode = (load_config().get("chat_think_mode") or "").strip()
    if mode in ("off", "openai", "deepseek"):
        return mode
    return "deepseek" if "deepseek" in (url or "").lower() else "openai"

def thinking_payload(mode, enabled, level):
    """生成思考相关请求参数。

    - off      : 不发送任何思考参数
    - openai   : 开启时发 reasoning_effort，关闭时不发（无法表达"禁用"）
    - deepseek : 始终发 thinking.type；开启时附带 reasoning_effort
    """
    level = (level or "high").strip() or "high"
    if mode == "off":
        return {}
    if mode == "openai":
        return {"reasoning_effort": level} if enabled else {}
    out = {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if enabled:
        out["reasoning_effort"] = level
    return out


def use_chat_for_vision():
    """是否改用「聊天模型」做图像识别（聊天模型自带视觉能力时）"""
    return bool(load_config().get("vision_use_chat"))
