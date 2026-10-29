# -*- coding: utf-8 -*-
"""任务里的 LLM 步骤：让「起草 / 总结 / 分类」也能被拆进任务链。

只在任务引擎里可用（chat=False）：主对话本身就在调模型，没必要再套一层。
"""
from app import capabilities
from app.config import get_api_conf
from app.llm import _quick_chat


def _generate(args, ctx):
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return "❌ 未配置聊天 API，无法执行 LLM 步骤。"
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "❌ 缺少 prompt。"
    messages = []
    system = str(args.get("system") or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        text = _quick_chat(api_key, api_url, api_model, messages,
                           max_tokens=int(args.get("max_tokens") or 1200),
                           source="task_llm")
    except Exception as e:
        return "❌ LLM 步骤失败：%s" % e
    text = (text or "").strip()
    return text or "❌ 模型没有返回内容。"


capabilities.capability(
    "llm.generate", "用聊天模型生成一段文本（任务专用）",
    {"prompt": "str", "system": "str?", "max_tokens": "int?"},
    permission="read", chat=False)(_generate)
