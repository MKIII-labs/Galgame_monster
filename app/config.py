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
        "speech_model_dir": "",          # 本地语音模型目录；留空则用 data/models 下的默认目录
        "speech_polish": False,          # 语音输入结束后是否自动做一次 AI 矫正
        "speech_denoise": False,         # 语音输入是否开启流式降噪（DPDFNet）
        "speech_denoise_model": "",      # 降噪模型路径；留空则用 data/models/dpdfnet_baseline.onnx
        "speech_final_model_dir": "",    # 两遍识别的精修模型目录；留空则用 data/models 下的默认目录
        "ui_mode": "solo",               # 界面模式：solo（纯对话）| ide（左侧工作区 + 右侧对话）
        "theme": "light",                # 默认浅色（VS Code Light+）
        # ---- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）----
        "gal_emotion_multi": False,      # 情绪字段是否用多标签数组（默认单值）
        "gal_options_instant": True,     # 点选项是否立即发送（否则填回输入框）
        "gal_tts_priority": "sprite",    # 台词发声优先级：sprite=绑定音频优先 | tts
        # ---- GAL 演出皮肤 / 节奏 / 特效 ----
        "gal_theme": "sakura",           # 演出主题：sakura 樱粉 | night 夜蓝 | washi 和风纸
        "gal_typewriter": True,          # 打字机逐字显示
        "gal_cps": 12,                   # 打字机速度（字符/秒）
        "gal_seg_mode": "single",         # 对白展示：single 单行推进（默认，= 新世界观感）| append 逐段追加
        "gal_auto": False,               # 单行推进时：语音播完自动切下一段
        "gal_min_show": 0.6,             # 每段最短展示秒数（自动推进用）
        "gal_fx": True,                  # 关键词特效（*闪光* / *震动* 等）
        "gal_show_think": False,         # 是否显示 AI 思考过程（关闭时只显示「思考中…」，更不破坏沉浸感）
        "gal_show_exec": True,           # 是否显示 AI 行为（搜索/工具等，以「幕间」呈现）
        "gal_dialog_max": 46,            # 对白框最大高度（占舞台高度百分比，30–70）
        "gal_sprite_fx": True,           # 立绘进出场 / 换人过渡动画
        "gal_bar_auto": True,            # 顶部工具条沉浸式淡出（鼠标移入才实显）
        # ---- GAL AI 自动写剧本 ----
        "gal_auto_script": False,        # 总开关：由 AI 依据角色设定与用户画像自己编剧情并推进
        "gal_auto_script_acts": 5,       # 大纲幕数（1–12）
        "gal_auto_script_rounds": 4,     # 每幕最多回合数（1–12）
        "gal_auto_script_replan": True,  # 允许在剧情跑偏时重新规划
        # ---- GAL 台词朗读（TTS）----
        "tts_enabled": False,            # 是否朗读台词
        "tts_provider": "edge",           # edge（免费·免密钥）| openai（兼容 /v1/audio/speech）| gpt-sovits（本地 HTTP）
        "tts_url": "",                   # edge 忽略；openai 留空用 api.openai.com；gpt-sovits 留空用 127.0.0.1:9880
        "tts_api_key": "",
        "tts_model": "",                 # openai 用，如 tts-1 / gpt-4o-mini-tts
        "tts_voice": "",                 # edge: zh-CN-XiaoxiaoNeural 等；openai: alloy 等；留空用各自默认
        "tts_speed": 1.0,
        "tts_max_len": 80,               # 单句最大字数（按标点切句）
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
