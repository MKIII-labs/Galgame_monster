# -*- coding: utf-8 -*-
"""上下文容量：token 估算 / 校准 / 超限压缩"""
import json
import time

import requests

from app.config import load_config, get_api_conf, normalize_content
from app.profile import load_profile
from app.sessions import find_session

MSG_OVERHEAD = 4            # 每条消息的固定结构开销（近似）
COMPRESS_KEEP = 8           # 压缩时保留最近的原文条数
SUMMARY_MAX_TOKENS = 1024   # 生成摘要时的最大输出 token


def context_limits():
    """读取配置里的「上下文上限」与「为回复预留」的 token 数（非法值回落默认）"""
    cfg = load_config()
    try:
        limit = int(cfg.get("chat_context_limit") or 128000)
    except (TypeError, ValueError):
        limit = 128000
    try:
        reserve = int(cfg.get("chat_reserve_output") or 4096)
    except (TypeError, ValueError):
        reserve = 4096
    if limit <= 0:
        limit = 128000
    if reserve < 0:
        reserve = 0
    return limit, reserve


def estimate_tokens(text):
    """启发式 token 估算：CJK 约 0.6 token/字，其余约 0.3 token/字。
    仅用于进度提示，误差靠真实 usage.prompt 校准。"""
    if not text:
        return 0
    cjk = 0
    total = 0
    for ch in str(text):
        total += 1
        o = ord(ch)
        if (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7A3 or
                0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF):
            cjk += 1
    return round(cjk * 0.6 + (total - cjk) * 0.3)


def summary_block(summary):
    """把摘要包装成注入到系统提示里的文本块"""
    return "以下是本次对话较早内容的摘要（原始消息已压缩）：\n" + (summary or "")


def _context_text_tokens(system_prompt, summary, msgs):
    """估算一次请求上下文的 token 数（system + 摘要 + 消息）"""
    n = 0
    if system_prompt:
        n += MSG_OVERHEAD + estimate_tokens(system_prompt)
    if summary:
        n += MSG_OVERHEAD + estimate_tokens(summary_block(summary))
    for m in msgs:
        n += MSG_OVERHEAD + estimate_tokens(m.get("content") or "")
    return n


def calibrate_ratio(s, system_prompt=""):
    """用最近一条带真实 usage.prompt 的助手消息，算出「真实 / 估算」的校准因子"""
    msgs = s.get("messages", [])
    cut = int(s.get("summary_upto") or 0)
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "assistant":
            continue
        prompt_tokens = (m.get("usage") or {}).get("prompt")
        # 只有「生成该锚点时的压缩点 == 当前压缩点」才可对齐；
        # 否则（例如压缩前产生的旧锚点）其 prompt 对应的是全量历史，估算无法对上
        if not prompt_tokens or cut >= i or int(m.get("ctx_cut", 0)) != cut:
            continue
        est = _context_text_tokens(system_prompt, s.get("summary") or "", msgs[cut:i])
        if est > 0:
            ratio = int(prompt_tokens) / est
            if 0.2 <= ratio <= 5:         # 明显离群则丢弃，避免估算被带偏
                return ratio
    return 1.0


def session_context(s, system_prompt=""):
    """返回当前会话的上下文占用（估算 + 校准），供进度条与预警使用"""
    limit, reserve = context_limits()
    cut = int(s.get("summary_upto") or 0)
    summary = s.get("summary") or ""
    msgs = s.get("messages", [])
    est = _context_text_tokens(system_prompt, summary, msgs[cut:])
    ratio = calibrate_ratio(s, system_prompt)
    used = int(est * ratio)
    remain = max(0, limit - used)
    pct = round(used * 100.0 / limit, 1) if limit else 0.0
    return {
        "used": used, "limit": limit, "reserve": reserve, "remain": remain,
        "pct": pct, "ratio": round(ratio, 3), "compressed": bool(summary),
    }


def build_context_messages(s, system_prompt):
    """按「系统提示 + 摘要 + 压缩点之后的原文」拼出真正发给模型的消息列表"""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if s.get("summary"):
        block = summary_block(s["summary"])
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += "\n\n" + block
        else:
            messages.insert(0, {"role": "system", "content": block})
    cut = int(s.get("summary_upto") or 0)
    for m in s.get("messages", [])[cut:]:
        messages.append({"role": m["role"], "content": m.get("content") or ""})
    return messages


def compress_history(s):
    """把较早的对话总结成摘要，写入 s['summary'] / s['summary_upto']。
    成功返回 True；可压缩消息不足或调用失败返回 False。"""
    msgs = s.get("messages", [])
    cut = int(s.get("summary_upto") or 0)
    keep_from = len(msgs) - COMPRESS_KEEP
    if keep_from <= cut:
        return False                      # 可压缩的旧消息不足
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return False
    lines = []
    if s.get("summary"):
        lines.append("【已有摘要】" + s["summary"])
    for m in msgs[cut:keep_from]:
        who = "用户" if m.get("role") == "user" else "AI"
        lines.append(f"{who}：{(m.get('content') or '').strip()}")
    prompt = (
        "请把下面这段较早的对话压缩成一段简洁的摘要，保留关键事实、用户的偏好与需求、"
        "未完成的事项以及必要的上下文；不要逐句复述，不要加入原文没有的信息。"
        "只输出摘要正文。\n\n" + "\n".join(lines))
    try:
        resp = requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8"},
            json={"model": api_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False, "max_tokens": SUMMARY_MAX_TOKENS},
            timeout=(10, 120))
        resp.raise_for_status()
        obj = resp.json()
        choice = (obj.get("choices") or [{}])[0]
        summary = normalize_content((choice.get("message") or {}).get("content"))
    except Exception as e:
        print(f"[压缩] 失败：{e}")
        return False
    summary = (summary or "").strip()
    if not summary:
        return False
    s["summary"] = summary
    s["summary_upto"] = keep_from
    return True


def current_context(data):
    """当前会话的上下文占用（含系统提示），供 /api/state 与各会话接口回传。
    注意：此处只算「手动偏好」，与 _api_chat 实际发送的提示保持一致；
    记忆/画像由检索按需注入，无法在会话外复算，故不计入。"""
    cur = find_session(data, (data or {}).get("current"))
    if not cur:
        return None
    system_prompt = (load_profile().get("manual", "") or "").strip()
    return session_context(cur, system_prompt)
