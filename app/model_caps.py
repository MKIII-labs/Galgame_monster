# -*- coding: utf-8 -*-
"""模型能力注册表：上下文窗口、视觉、原生工具、并行工具、输出上限。

为什么要单独一层：

- 上下文窗口不该靠一个固定的 `claude: 200000` 猜：同一个模型名可能带 `-1m`
  变体，也可能走网关换成别的窗口；
- 原生工具 / 视觉 / 并行工具支持与否，决定请求怎么组装；
- 用户可以在 config 的 `model_caps` 里按前缀覆盖，未知模型回落到默认值。

这里只做「能力判断」，不发起任何网络请求。
"""
DEFAULT = {
    "context": 128000,
    "vision": False,
    "native_tools": True,
    "parallel_tools": True,
    "max_output": 8192,
    # 服务商自带搜索（目前只有 DeepSeek：/v1/tools/search，复用聊天密钥）
    "web_search": False,
}

# 前缀 -> 能力。取最长匹配；键名小写，模型名也按小写匹配。
BUILTIN = {
    "claude": {"context": 200000, "vision": True, "native_tools": True,
               "parallel_tools": True, "max_output": 8192},
    "deepseek-flash": {"context": 1000000, "vision": False, "native_tools": True,
                       "parallel_tools": True, "max_output": 8192, "web_search": True},
    "deepseek-v4": {"context": 1000000, "vision": False, "native_tools": True,
                    "parallel_tools": True, "max_output": 8192, "web_search": True},
    "deepseek-chat": {"context": 128000, "vision": False, "native_tools": True,
                      "parallel_tools": True, "max_output": 8192, "web_search": True},
    "deepseek-reasoner": {"context": 128000, "vision": False, "native_tools": True,
                          "parallel_tools": True, "max_output": 8192, "web_search": True},
    "deepseek": {"context": 1000000, "vision": False, "native_tools": True,
                 "parallel_tools": True, "max_output": 8192, "web_search": True},
    "gpt-4o": {"context": 128000, "vision": True, "native_tools": True,
               "parallel_tools": True, "max_output": 16384},
    "gpt-4": {"context": 128000, "vision": False, "native_tools": True,
              "parallel_tools": True, "max_output": 8192},
    "gemini": {"context": 1000000, "vision": True, "native_tools": True,
               "parallel_tools": True, "max_output": 8192},
    "qwen": {"context": 131072, "vision": False, "native_tools": True,
             "parallel_tools": True, "max_output": 8192},
    "glm": {"context": 128000, "vision": False, "native_tools": True,
            "parallel_tools": True, "max_output": 8192},
    "kimi": {"context": 256000, "vision": False, "native_tools": True,
             "parallel_tools": True, "max_output": 8192},
    "llama": {"context": 131072, "vision": False, "native_tools": True,
              "parallel_tools": True, "max_output": 8192},
    "mistral": {"context": 128000, "vision": False, "native_tools": True,
                "parallel_tools": True, "max_output": 8192},
}


def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], bool):
            out[k] = bool(v)
        else:
            out[k] = v
    return out


def caps_for(model="", cfg=None):
    """按模型名返回能力字典；未知模型用 DEFAULT。"""
    name = str(model or "").strip().lower()
    cfg = cfg or {}
    table = dict(BUILTIN)
    table.update(cfg.get("model_caps") or {})
    best_key, best = "", {}
    for k, v in table.items():
        kk = str(k or "").strip().lower()
        if kk and kk in name and len(kk) > len(best_key) and isinstance(v, dict):
            best_key, best = kk, v
    cap = _merge(DEFAULT, best)
    # 1M 变体：模型名里带 1m 时按 1M 处理（用户仍可用 model_caps / chat_context_limit 收紧）
    if "1m" in name:
        cap["context"] = max(int(cap.get("context") or 0), 1000000)
    try:
        cap["context"] = int(cap.get("context") or 0) or DEFAULT["context"]
        cap["max_output"] = int(cap.get("max_output") or 0) or DEFAULT["max_output"]
    except (TypeError, ValueError):
        cap["context"] = DEFAULT["context"]
        cap["max_output"] = DEFAULT["max_output"]
    return cap
