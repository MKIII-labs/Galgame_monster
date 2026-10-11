# -*- coding: utf-8 -*-
"""会话读写、精简与旧格式解析"""
import os
import time

from app.store import SESSION_FILE, LEGACY_HISTORY, read_json, write_json, new_id


def ensure_session_kb_fields(s):
    s.setdefault("memory", {"nodes": [], "edges": []})
    s.setdefault("kb_ids", [])
    s.setdefault("ai_write", True)
    s.setdefault("agent_enabled", False)   # 是否开启「文件 Agent」模式
    s.setdefault("sandbox", "")            # Agent 的沙箱目录（绝对路径）
    s.setdefault("allow_outside_read", False)  # 允许 Agent 读取沙箱外的文件（只读）
    # ---- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）----
    s.setdefault("gal_enabled", False)      # 该会话是否以 GAL 模式对话
    s.setdefault("gal_character_id", "")    # 当前角色 id
    s.setdefault("gal_node_id", "")         # 剧情节点（P4 使用）
    s.setdefault("gal_round", 0)            # 当前节点内回合数（P4 使用）
    # ---- GAL AI 自动写剧本（每个会话独立保存 AI 生成的剧本）----
    s.setdefault("gal_auto_on", False)      # 该会话是否启用 AI 自动剧本
    s.setdefault("gal_auto_story", {})      # AI 生成的剧本（结构与 story.json 一致）
    s.setdefault("gal_auto_outline", "")    # 全剧走向摘要（注入用）
    s.setdefault("gal_auto_acts_done", 0)   # 已演出完成的幕数
    if not isinstance(s.get("memory"), dict):
        s["memory"] = {"nodes": [], "edges": []}
    s["memory"].setdefault("nodes", [])
    s["memory"].setdefault("edges", [])


def parse_legacy_history(text):
    msgs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("你："):
            msgs.append({"role": "user", "content": line[2:]})
        elif line.startswith("AI："):
            msgs.append({"role": "assistant", "content": line[3:]})
    return msgs


def load_sessions():
    data = read_json(SESSION_FILE)
    if isinstance(data, dict) and data.get("sessions"):
        for s in data["sessions"]:
            s.setdefault("id", new_id())
            s.setdefault("title", "新对话")
            s.setdefault("updated_at", time.time())
            s.setdefault("messages", [])
            ensure_session_kb_fields(s)
        if not any(s["id"] == data.get("current") for s in data["sessions"]):
            data["current"] = data["sessions"][0]["id"]
        return data

    sessions = []
    if os.path.exists(LEGACY_HISTORY):
        try:
            with open(LEGACY_HISTORY, "r", encoding="utf-8") as f:
                legacy = f.read()
            if legacy.strip():
                sessions.append({"id": new_id(), "title": "历史记录",
                                 "updated_at": time.time(),
                                 "messages": parse_legacy_history(legacy)})
        except Exception:
            pass
    if not sessions:
        sessions.append({"id": new_id(), "title": "新对话", "updated_at": time.time(), "messages": []})
    data = {"current": sessions[0]["id"], "sessions": sessions}
    save_sessions(data)
    return data


def save_sessions(data):
    if data is None:
        return
    write_json(SESSION_FILE, data)


def slim_sessions(data):
    """只保留当前会话的消息，其余会话 messages 置为 None（前端切换时再按需拉取），
    避免每次进页面都把全部历史塞进 payload。"""
    if not data:
        return data
    cur = data.get("current")
    out = {"current": cur, "sessions": []}
    for s in data.get("sessions", []):
        item = {"id": s["id"], "title": s.get("title", ""), "updated_at": s.get("updated_at", 0)}
        item["messages"] = s.get("messages", []) if s["id"] == cur else None
        item["kb_ids"] = s.get("kb_ids", [])
        item["ai_write"] = s.get("ai_write", True)
        item["agent_enabled"] = s.get("agent_enabled", False)
        item["sandbox"] = s.get("sandbox", "")
        item["allow_outside_read"] = s.get("allow_outside_read", False)
        item["gal_enabled"] = s.get("gal_enabled", False)
        item["gal_character_id"] = s.get("gal_character_id", "")
        item["gal_node_id"] = s.get("gal_node_id", "")
        item["gal_round"] = s.get("gal_round", 0)
        item["gal_auto_on"] = s.get("gal_auto_on", False)
        item["mem_count"] = len((s.get("memory") or {}).get("nodes", []))
        out["sessions"].append(item)
    return out

def find_session(data, sid):
    for s in data["sessions"]:
        if s["id"] == sid:
            return s
    return None
