# -*- coding: utf-8 -*-
"""划词问答（旁注 / 笔记 / PDF）的持久化线程。

线程按「作用域」分组：
- `session:<sid>`：对话里的旁注，跟着主会话走；
- `doc:<doc_key>`：笔记 / PDF 的划词问答，跟着文档走（换会话也能接着问）。

每个作用域可以有多个线程（前端显示为标签页）。线程只保存「选中的片段 + 问答对」，
不写 sessions.json / memory / trajectory。

兼容：旧调用传裸 sid（如 `create_thread("s1", ...)`）会按 `session:s1` 处理；
旧的 `by_session` 结构在首次读取时自动迁移到 `by_scope`。
"""
import os
import time
import uuid

from app.store import DATA_DIR, _lock, read_json, write_json

ASIDE_FILE = os.path.join(DATA_DIR, "aside.json")
MAX_THREADS = 20          # 每个作用域最多保留多少个线程
MAX_HISTORY = 12          # 每个线程最多保留多少轮问答
MAX_TITLE = 40


def norm_scope(key):
    s = str(key or "").strip()
    if not s:
        return "session:_"
    if s.startswith("session:") or s.startswith("doc:"):
        return s
    return "session:" + s


def _load():
    data = read_json(ASIDE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    by = data.get("by_scope")
    if not isinstance(by, dict):
        by = {}
    legacy = data.pop("by_session", None)
    if isinstance(legacy, dict):
        for sid, items in legacy.items():
            if isinstance(items, list):
                by.setdefault("session:" + str(sid), items)
    data["by_scope"] = by
    return data


def _save(data):
    with _lock:
        write_json(ASIDE_FILE, data)


def list_threads(scope):
    items = (_load().get("by_scope") or {}).get(norm_scope(scope)) or []
    return items[-MAX_THREADS:]


def get_thread(scope, tid):
    for t in list_threads(scope):
        if t.get("id") == str(tid or ""):
            return t
    return None


def create_thread(scope, selection, include_message=False, message_text="", title="",
                  source="chat", anchor="", doc_key=""):
    scope = norm_scope(scope)
    data = _load()
    by = data["by_scope"].setdefault(scope, [])
    now = time.time()
    text = str((selection or {}).get("text") or "").strip()
    t = {
        "id": "as_" + uuid.uuid4().hex[:10],
        "title": (str(title or "").strip() or text.replace("\n", " ")[:MAX_TITLE]
                  or "新问答")[:MAX_TITLE],
        "source": str(source or "chat"),
        "anchor": str(anchor or text)[:600],
        "doc_key": str(doc_key or "")[:500],
        "selection": selection or {},
        "include_message": bool(include_message),
        "message_text": str(message_text or "")[:8000],
        "history": [],
        "created_at": now,
        "updated_at": now,
    }
    by.append(t)
    if len(by) > MAX_THREADS:
        data["by_scope"][scope] = by[-MAX_THREADS:]
    _save(data)
    return t


def append_qa(scope, tid, question, answer):
    """把一轮问答写进线程；线程不存在返回 None。"""
    scope = norm_scope(scope)
    data = _load()
    by = data["by_scope"].setdefault(scope, [])
    for t in by:
        if t.get("id") != str(tid or ""):
            continue
        t.setdefault("history", []).append({
            "q": str(question or "")[:2000],
            "a": str(answer or "")[:4000],
            "at": time.time(),
        })
        t["history"] = t["history"][-MAX_HISTORY:]
        t["updated_at"] = time.time()
        _save(data)
        return t
    return None


def rename_thread(scope, tid, title):
    scope = norm_scope(scope)
    data = _load()
    for t in (data["by_scope"].get(scope) or []):
        if t.get("id") == str(tid or ""):
            t["title"] = str(title or "").strip()[:MAX_TITLE] or t["title"]
            t["updated_at"] = time.time()
            _save(data)
            return t
    return None


def delete_thread(scope, tid):
    scope = norm_scope(scope)
    data = _load()
    items = data["by_scope"].get(scope) or []
    left = [t for t in items if t.get("id") != str(tid or "")]
    if len(left) == len(items):
        return False
    data["by_scope"][scope] = left
    _save(data)
    return True
