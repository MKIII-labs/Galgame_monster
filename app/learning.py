# -*- coding: utf-8 -*-
"""学习指令预设——作为「用户画像」的维护准则，注入主对话的记忆工具"""
import json
import os

from app.store import RESOURCES_DIR

# 内置学习指令预设（随项目发布，只读）
PRESETS_FILE = os.path.join(RESOURCES_DIR, "learning_presets.json")
BUILTIN_PRESET_LABEL = "默认（内置）"

# 内置预设缺失时的兜底准则（只写「怎么学」，输出格式由记忆工具统一规定）
FALLBACK_LEARNING_PROMPT = """你是用户画像的维护者：从对话中持续构建并维护关于这位用户的长期画像。
- 主动、不遗漏：偏好、禁忌、说话方式、身份背景、长期目标、常用工具与环境都值得记
- 稳定优先：只记跨对话仍成立的信息；一次性内容不记
- 不是只增不减：旧条目过时、被推翻、重复或太笼统时要更新或删除，让画像精简、准确、不矛盾
- 自己做判断：可合并同类项、把笼统的条目改具体；标准只有一条——对以后理解用户有没有帮助
- 不臆测：只记用户明确表达或强烈暗示的内容，拿不准就不记
- 冲突处理：以更晚说出、更明确、更具体者为真，更新或删除旧条目，绝不让两条矛盾条目并存

当前画像条目：
{current_auto}

本轮对话：
用户说：{user_input}
AI回复：{ai_reply}
"""
def load_builtin_learning_presets():
    """读取随项目发布的内置学习指令预设（只读）"""
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, str)}
        except Exception as e:
            print(f"[预设] 读取失败：{e}")
    return {}

def default_learning_prompt():
    """内置默认学习指令：优先用 learning_presets.json 的第一个条目，否则用兜底文案"""
    presets = load_builtin_learning_presets()
    if presets:
        return next(iter(presets.values()))
    return FALLBACK_LEARNING_PROMPT

def render_template(tmpl, **kw):
    """渲染带 {current_auto} {user_input} {ai_reply} 占位符的模板，失败时降级拼接"""
    try:
        return tmpl.format(**kw)
    except (KeyError, IndexError, ValueError):
        return (f"{tmpl}\n\n当前已知自动画像：{kw.get('current_auto', '')}\n"
                f"用户说：{kw.get('user_input', '')}\nAI回复：{kw.get('ai_reply', '')}")

