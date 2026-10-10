# -*- coding: utf-8 -*-
"""图像识别（多模态接口，双协议）"""
import json

import requests

from app.config import get_api_conf, get_api_protocol, normalize_content, use_chat_for_vision

# ============================================================
# 图像识别
# ============================================================

def recognize_image(mime, b64):
    # 开关开启时改用「聊天模型」识图（要求聊天模型自带视觉能力）
    use_chat = use_chat_for_vision()
    provider = "chat" if use_chat else "vision"
    api_key, api_url, api_model = get_api_conf(provider)
    if not api_key:
        who = "聊天" if use_chat else "图像"
        return f"⚠️ 尚未配置{who} API 密钥，请在「设置 → {who} API」中填写。"
    # 用聊天模型识图时固定走 OpenAI 兼容的多模态格式
    protocol = "openai" if use_chat else get_api_protocol("vision")
    data_url = f"data:{mime};base64,{b64}"
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
    else:
        payload = {"model": api_model, "input": {"messages": [{"role": "user", "content": [
            {"image": data_url}, {"text": prompt}]}]}}
    try:
        resp = requests.post(api_url, headers=headers,
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=60)
        resp.raise_for_status()
        result = resp.json()
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
