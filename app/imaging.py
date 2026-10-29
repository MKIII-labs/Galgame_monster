# -*- coding: utf-8 -*-
"""图像识别（多模态接口，双协议）"""
import json

import requests

from app.config import (effective_think_mode, get_api_conf, get_api_protocol, load_config,
                        normalize_content, thinking_payload, use_chat_for_vision)
from app.usage import record_llm_usage

# ============================================================
# 图像识别
# ============================================================

def vision_mode():
    """'chat' = 图片直发聊天模型（多模态）；'vision' = 先用视觉 API 识别成文字"""
    return "chat" if use_chat_for_vision() else "vision"


def vision_available():
    """聊天 / 视觉两条通道**任一**配了密钥就算可用（决定 PDF 扫描页要不要走识图）"""
    return bool(get_api_conf("chat")[0]) or bool(get_api_conf("vision")[0])


def chat_vision_available():
    """聊天模型是否可用于识图（设置页 / 状态接口用，不发请求）。

    配了聊天密钥即视为可用：不再按模型名猜能力（同一模型名走不同网关时
    能力并不一致，注册表判断反而会误判成「不支持」）。
    """
    try:
        return bool(get_api_conf("chat")[0])
    except Exception:                    # noqa: BLE001  读配置失败时按「不可用」处理
        return False


def build_vision_content(text, mime, b64, images=None):
    """OpenAI 兼容的多模态 content：一张或多张图片 + 文字。

    images 为 [{"mime": ..., "b64": ...}]；不传时沿用单图参数。
    """
    items = images or [{"mime": mime, "b64": b64}]
    parts = [{"type": "image_url",
              "image_url": {"url": f"data:{it['mime']};base64,{it['b64']}"}}
             for it in items]
    if (text or "").strip():
        parts.append({"type": "text", "text": text})
    return parts


def recognize_image(mime, b64, question=""):
    """按设置里的首选通道识别（聊天模型 / 视觉 API）"""
    return _recognize_with("chat" if use_chat_for_vision() else "vision", mime, b64, question)


def recognize_image_auto(mime, b64, question=""):
    """首选通道没配密钥时自动回落到另一条通道。

    用于 PDF 扫描页识图：用户不一定两条都配了，能识图就别报「未配置」。
    """
    first = "chat" if use_chat_for_vision() else "vision"
    second = "vision" if first == "chat" else "chat"
    for provider in (first, second):
        if get_api_conf(provider)[0]:
            return _recognize_with(provider, mime, b64, question)
    return "⚠️ 尚未配置「视觉模型」或「聊天模型」的 API 密钥，无法识别图片。"


def recognize_image_fallback(mime, b64, question="", prefer=None):
    """识图；首选通道**实际失败**时自动换另一条通道。

    与 recognize_image_auto 的区别：那个只看「哪条配置了 key」。
    截图回灌时首选可能是聊天模型，而纯文本聊天模型收到 image_url 会直接报错 ——
    必须按结果回落，否则截图永远识不出来。
    失败判定沿用本模块的约定：返回以 ❌ / ⚠️ 开头即视为没成功。
    """
    first = prefer if prefer in ("chat", "vision") else (
        "chat" if use_chat_for_vision() else "vision")
    second = "vision" if first == "chat" else "chat"
    out = ""
    for provider in (first, second):
        out = _recognize_with(provider, mime, b64, question)
        if not str(out).startswith(("❌", "⚠️")):
            return out
    return out


def _recognize_with(provider, mime, b64, question="", max_tokens=None):
    """按指定通道（'chat' | 'vision'）识别一张图。返回识别文本或 ⚠️/❌ 说明。"""
    use_chat = (provider == "chat")
    api_key, api_url, api_model = get_api_conf(provider)
    if not api_key:
        who = "聊天" if use_chat else "图像"
        return f"⚠️ 尚未配置{who} API 密钥，请在「设置 → {who} API」中填写。"
    # 用聊天模型识图时固定走 OpenAI 兼容的多模态格式
    protocol = "openai" if use_chat else get_api_protocol("vision")
    data_url = f"data:{mime};base64,{b64}"
    question = (question or "").strip()
    if question:
        # 带着用户的问题识图，让描述贴着他的提问走
        prompt = (f"用户对这张图片的提问：{question}\n\n"
                  "请结合图片内容回答这个提问。若图片含数学公式，请用 LaTeX（$$...$$）输出；"
                  "若含手写或印刷文字，请一并识别。请用中文回答。")
    else:
        prompt = """请分析这张图片，完成以下任务：
1. 如果图片中包含数学公式，请用 LaTeX 格式输出所有公式
2. 如果图片是物体/场景照片，请识别并描述其中的主要内容
3. 如果图片包含手写文字，请尽可能识别并输出
请用中文回答，公式部分用 $$...$$ 包裹。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if protocol == "openai":
        payload = {"model": api_model, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt}]}]}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if use_chat:
            # 聊天模型默认可能开启思考：思考 token 也计入 max_tokens，
            # 结果 content 为空、只剩 reasoning_content，识图会被误判为失败。
            # 识图只要观察结果，关掉思考后既不会空正文，响应也明显更快。
            cfg = load_config() or {}
            payload.update(thinking_payload(effective_think_mode(api_url), False,
                                            cfg.get("chat_think_level") or "high"))
    else:
        payload = {"model": api_model, "input": {"messages": [{"role": "user", "content": [
            {"image": data_url}, {"text": prompt}]}]}}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
    try:
        resp = requests.post(api_url, headers=headers,
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=60)
        resp.raise_for_status()
        result = resp.json()
        # 识图同样消耗 token：按通道与端点记账，便于用量统计覆盖所有 API
        record_llm_usage(result.get("usage") or {}, api_model, "vision", api=provider, url=api_url)
        if protocol == "openai":
            content = normalize_content(result["choices"][0]["message"]["content"])
        else:
            content = normalize_content(result["output"]["choices"][0]["message"]["content"])
        return content.strip() or "⚠️ 识别结果为空，请重试或更换图片"
    except Exception as e:
        resp = getattr(e, "response", None)
        if resp is not None:
            return f"❌ 识别出错：{e}\n响应内容：{resp.text}"
        return f"❌ 识别出错：{e}"
