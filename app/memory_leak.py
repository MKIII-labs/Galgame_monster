# -*- coding: utf-8 -*-
"""清理已落库的 <memory> 控制块（模型自创协议写法的存量泄漏）。

背景：文本协议只认 ```memory 围栏。模型改写成 <memory>…</memory> 时，旧版本
会把整块 JSON 原样落库：前端把源码显示给用户，下一轮又作为「示例」被模仿。
运行时已补三层兜底（流式摘除 / 落库前清洗 / 前端渲染），这里是存量清理。
"""
from app.mem_tool import strip_memory_tags


def clean_message(msg):
    """清洗一条消息的内容；返回 (是否改动, 腾出的字符数)。"""
    if not isinstance(msg, dict):
        return False, 0
    text = msg.get("content")
    if not isinstance(text, str) or not text:
        return False, 0
    cleaned = strip_memory_tags(text)
    if cleaned == text:
        return False, 0
    msg["content"] = cleaned
    return True, len(text) - len(cleaned)


def clean_sessions_data(data):
    """就地清洗 sessions.json 结构（也支持单会话快照）；返回统计。"""
    stats = {"sessions": 0, "messages": 0, "chars": 0}
    if not isinstance(data, dict):
        return stats
    buckets = data.get("sessions") if isinstance(data.get("sessions"), list) else [data]
    for s in buckets:
        if not isinstance(s, dict):
            continue
        hit = 0
        for m in s.get("messages") or []:
            changed, freed = clean_message(m)
            if changed:
                hit += 1
                stats["chars"] += freed
        if hit:
            stats["sessions"] += 1
            stats["messages"] += hit
    return stats
