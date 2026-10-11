# -*- coding: utf-8 -*-
"""GAL 台词朗读（TTS）：极简适配器

支持两种后端（见 docs/GAL_MODE_DESIGN.md §7）：
- openai     ：任意 OpenAI 兼容的 `POST {base}/v1/audio/speech`
- gpt-sovits ：本地 GPT-SoVITS HTTP（`POST {base}/tts`）

只做「文本 → 整段音频字节」，不做流式；失败由调用方处理。
"""
import re

import requests


def config(cfg=None, override=None):
    """读取 TTS 配置（可传入已加载的 config 以复用；override 用于角色的配音预设）"""
    if cfg is None:
        from app.config import load_config
        cfg = load_config() or {}
    try:
        speed = float(cfg.get("tts_speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        max_len = int(cfg.get("tts_max_len") or 80)
    except (TypeError, ValueError):
        max_len = 80
    base = {
        "enabled": bool(cfg.get("tts_enabled")),
        "provider": str(cfg.get("tts_provider") or "edge").strip().lower(),
        "url": str(cfg.get("tts_url") or "").strip(),
        "api_key": str(cfg.get("tts_api_key") or "").strip(),
        "model": str(cfg.get("tts_model") or "").strip() or "tts-1",
        "voice": str(cfg.get("tts_voice") or "").strip(),
        "speed": min(4.0, max(0.25, speed)),
        "max_len": min(400, max(20, max_len)),
        # GPT-SoVITS 角色音色克隆：绝对路径 + 参考文本 + 参考语言
        "ref_audio_path": "",
        "prompt_text": "",
        "prompt_lang": "",
        # GPT-SoVITS 角色微调权重（可选；需本地服务，走 set_*_weights 切换）
        "gpt_weights_path": "",
        "sovits_weights_path": "",
    }
    if isinstance(override, dict):
        for k in ("provider", "voice", "model", "url"):
            v = str(override.get(k) or "").strip()
            if v:
                base[k] = v.lower() if k == "provider" else v
        for k in ("ref_audio_path", "prompt_text", "prompt_lang",
                  "gpt_weights_path", "sovits_weights_path"):
            v = str(override.get(k) or "").strip()
            if v:
                base[k] = v
        try:
            sp = float(override.get("speed") or 0)
            if sp > 0:
                base["speed"] = min(4.0, max(0.25, sp))
        except (TypeError, ValueError):
            pass
    return base


# 已切换的权重缓存：同一路径不重复切换，避免每次合成都重载模型
_weight_cache = {"gpt": "", "sovits": ""}


def _switch_weights(root, cfg):
    """按需切换 GPT-SoVITS 的 GPT / SoVITS 微调权重（api_v2 的 set_*_weights）。

    权重没变就不重复切换，避免每次合成都重新加载模型。
    """
    root = str(root or "http://127.0.0.1:9880").rstrip("/")
    for key, ep in (("gpt", "/set_gpt_weights"), ("sovits", "/set_sovits_weights")):
        path = str(cfg.get("%s_weights_path" % key) or "").strip()
        if path == _weight_cache[key]:
            continue
        if not path:
            _weight_cache[key] = ""
            continue
        try:
            r = requests.get(root + ep, params={"weights_path": path}, timeout=180)
            r.raise_for_status()
            _weight_cache[key] = path
        except Exception as e:
            raise ValueError("切换 %s 权重失败：%s" % (key, e))


def split_sentences(text, max_len=80):
    """按中文标点切句；过长再硬切，保证每次合成不至于太长"""
    text = str(text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;…])|\n+", text)
    out = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        while len(p) > max_len:
            out.append(p[:max_len])
            p = p[max_len:]
        if p:
            out.append(p)
    return out


def synthesize(cfg, text, voice=""):
    """合成一段文本。返回 (音频字节, mime)；失败抛异常。"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("文本为空")
    provider = cfg.get("provider") or "edge"
    base = (cfg.get("url") or "").rstrip("/")

    if provider == "edge":
        # 微软 Edge 在线语音合成：免费、无需密钥（需本机可访问微软服务）
        return _edge_synthesize(cfg, text, voice)

    if provider == "gpt-sovits":
        root = base or "http://127.0.0.1:9880"
        endpoint = root + "/tts"
        _switch_weights(root, cfg)          # 需要时切到角色专属权重（api_v2 的 set_*_weights）
        lang = str(cfg.get("prompt_lang") or "").strip() or "zh"
        body = {"text": text, "text_lang": lang, "media_type": "wav"}
        ref = str(cfg.get("ref_audio_path") or "").strip()
        if ref:
            # 参考音频 → 克隆角色音色（api_v2 字段；同时带旧版 api.py 的别名，服务端会忽略多余字段）
            body["ref_audio_path"] = ref
            body["prompt_text"] = str(cfg.get("prompt_text") or "")
            body["prompt_lang"] = lang
            body["refer_wav_path"] = ref
            body["prompt_language"] = lang
        r = requests.post(endpoint, json=body, timeout=180)
        r.raise_for_status()
        return r.content, "audio/wav"

    # OpenAI 兼容
    endpoint = base or "https://api.openai.com"
    if "/audio/speech" not in endpoint:
        endpoint = endpoint + "/v1/audio/speech"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    body = {
        "model": cfg.get("model") or "tts-1",
        "voice": voice or cfg.get("voice") or "alloy",
        "input": text,
        "response_format": "mp3",
    }
    speed = cfg.get("speed") or 1.0
    if abs(speed - 1.0) > 1e-6:
        body["speed"] = speed
    r = requests.post(endpoint, headers=headers, json=body, timeout=180)
    r.raise_for_status()
    return r.content, "audio/mpeg"


# ------------------------------------------------------------
# Edge TTS（免费、无需密钥）
# ------------------------------------------------------------
EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def edge_voices():
    """列出 Edge 中文音色（供设置界面提示用）；失败返回 []"""
    try:
        import asyncio
        import edge_tts

        async def _list():
            vs = await edge_tts.list_voices()
            return [{"short_name": v.get("ShortName"), "gender": v.get("Gender"),
                     "locale": v.get("Locale")} for v in vs
                    if str(v.get("Locale", "")).startswith("zh")]
        return asyncio.run(_list())
    except Exception:
        return []


def _edge_synthesize(cfg, text, voice=""):
    try:
        import asyncio
        import edge_tts
    except ImportError:
        raise ValueError("未安装 edge-tts（在项目目录执行：pip install edge-tts）")
    name = (voice or cfg.get("voice") or EDGE_DEFAULT_VOICE).strip()
    rate = int(round((float(cfg.get("speed") or 1.0) - 1.0) * 100))
    rate_s = ("+" if rate >= 0 else "") + str(rate) + "%"

    async def _run():
        comm = edge_tts.Communicate(text, name, rate=rate_s)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.extend(chunk["data"])
        return bytes(buf)

    try:
        data = asyncio.run(_run())
    except Exception as e:
        raise ValueError("Edge TTS 调用失败：%s" % e)
    if not data:
        raise ValueError("Edge TTS 未返回音频（音色名是否正确？）")
    return data, "audio/mpeg"
