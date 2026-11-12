# -*- coding: utf-8 -*-
"""统一记忆：把画像 / 知识库 / 会话记忆 / 任务结果汇总成可检索的索引。

两条原则：
1. **图谱是源，索引是镜像**：知识库与会话记忆仍是原存储；`memory_items` 只是可重建的索引。
   写入时优先写回图谱（画像 / 会话 / 指定知识库），再同步索引。
2. **索引自动保鲜**：知识库或会话文件变化后，下一次搜索会自动重建，不需要手动点「重建索引」。
"""
import glob
import hashlib
import os
import re
import threading
import time
import uuid

from app import notes as noteslib
from app.capabilities import capability
from app.db import get_db
from app.knowledge import (KB_INDEX, apply_memory_ops, get_profile_kb, load_kb,
                           load_kb_index)
from app.sessions import find_session, load_sessions
from app.store import SESSION_FILE
from app.textmatch import tokens as textmatch_tokens

MAX_TEXT = 4000
MAX_ITEMS = 3000
MIRROR_SOURCES = ("kb", "session", "note")
_refresh_lock = threading.Lock()


def _now():
    return time.time()


def _clip(text, n=MAX_TEXT):
    s = str(text or "").strip()
    return s[:n]


def _first_line(text, n=60):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return str(text or "").strip()[:n]


def _tokens(query):
    """英文按词、中文按双字切分；与知识库图谱检索共用 app.textmatch。"""
    return textmatch_tokens(query)


# ------------------------------------------------------------
# 索引保鲜
# ------------------------------------------------------------
def _signature():
    """知识库 / 笔记 / 会话文件的 mtime/size 指纹（不读正文，成本很低）"""
    paths = []
    try:
        if os.path.exists(KB_INDEX):
            paths.append(KB_INDEX)
        paths.extend(glob.glob(os.path.join(os.path.dirname(KB_INDEX), "*.json")))
        paths.extend(glob.glob(os.path.join(noteslib.NOTES_DIR, "**", "*.md"),
                             recursive=True))
        if os.path.exists(SESSION_FILE):
            paths.append(SESSION_FILE)
    except OSError:
        pass
    parts = []
    for p in sorted(set(paths)):
        try:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (p, int(st.st_mtime_ns), st.st_size))
        except OSError:
            pass
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _load_sig():
    row = get_db().execute(
        "SELECT value FROM memory_meta WHERE key='index_sig'").fetchone()
    return row["value"] if row else ""


def _save_sig(sig):
    conn = get_db()
    conn.execute(
        "INSERT INTO memory_meta (key,value) VALUES ('index_sig',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sig,))
    conn.commit()


def ensure_fresh(force=False):
    """文件变了就重建索引；返回是否重建过。"""
    sig = _signature()
    if not force and _load_sig() == sig:
        return False
    with _refresh_lock:
        if not force and _load_sig() == sig:
            return False
        reindex()
        return True


# ------------------------------------------------------------
# 写入
# ------------------------------------------------------------
def remember(text, type="fact", importance=0.5, scope="global",
             source="manual", source_id="", tags=""):
    """写一条「索引级」记忆（不进入知识图谱）。任务结果与全局快速记忆用它。"""
    text = _clip(text)
    if not text:
        return None
    source = source if source in ("manual", "task", "kb", "session") else "manual"
    if not source_id:
        source_id = "m_" + uuid.uuid4().hex[:10]
    title = _first_line(text)
    now = _now()
    conn = get_db()
    conn.execute(
        "INSERT INTO memory_items (id,scope,source,source_id,type,title,text,tags,"
        "importance,created_at,updated_at,last_used_at,use_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0) "
        "ON CONFLICT(source,source_id,text) DO UPDATE SET "
        "title=excluded.title, importance=excluded.importance, "
        "tags=excluded.tags, updated_at=excluded.updated_at",
        ("m_" + uuid.uuid4().hex[:12], str(scope or "global"), source, str(source_id),
         str(type or "fact"), title, text, str(tags or ""),
         max(0.0, min(1.0, float(importance or 0.5))), now, now))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM memory_items WHERE source=? AND source_id=? AND text=?",
        (source, str(source_id), text)).fetchone()
    return dict(row) if row else None


def _resolve_target(target, type, kb_id):
    t = (target or "auto").strip()
    if t != "auto":
        return t
    if type == "preference":
        return "profile"
    if type == "note":
        return ("kb:" + kb_id) if kb_id else "session"
    return "session"


def _category_of(type, target):
    if type == "preference":
        return "喜好" if target == "profile" else "偏好"
    return {"fact": "其他", "event": "事件", "note": "笔记",
            "task": "任务"}.get(type or "fact", "其他")


def add_memory(text, type="fact", target="auto", session_id="", kb_id="",
               importance=0.6, tags=""):
    """把一条记忆写回图谱（画像 / 本会话 / 指定知识库），并同步索引。

    返回 {"target","source_name","node_id","kb_id","item","problems"}。
    """
    text = _clip(text)
    if not text:
        raise ValueError("内容不能为空")
    t = _resolve_target(target, type, kb_id)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    title = lines[0][:60] if lines else text[:60]
    content = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    category = _category_of(type, t)
    tag_list = [x.strip() for x in str(tags or "").split(",") if x.strip()]

    if t == "global":
        it = remember(text, type=type, importance=importance, tags=tags)
        return {"target": "global", "source_name": "全局记忆", "item": it,
                "node_id": "", "kb_id": "", "problems": []}

    sdata = load_sessions()
    s = find_session(sdata, session_id) if session_id else None
    if t == "session" and not s:
        t = "global"
        it = remember(text, type=type, importance=importance, tags=tags)
        return {"target": "global", "source_name": "全局记忆", "item": it,
                "node_id": "", "kb_id": "", "problems": ["没有当前会话，已写入全局记忆"]}

    if t == "profile":
        scope_key = get_profile_kb()
        allowed = [scope_key]
        source_name = "用户画像"
        kb_id = scope_key
    elif t.startswith("kb:"):
        scope_key = t[3:]
        kb = load_kb(scope_key)
        if not kb:
            raise ValueError("知识库不存在")
        allowed = [scope_key]
        source_name = kb.get("name") or "知识库"
        kb_id = scope_key
    else:
        scope_key = "session"
        allowed = [k for k in (s.get("kb_ids") or []) if k != scope_key]
        source_name = "本会话记忆"
        kb_id = ""

    counts, problems, changed = apply_memory_ops(
        session_id, allowed,
        [{"scope": scope_key, "op": "add", "title": title,
          "content": content, "category": category, "tags": tag_list}])
    if not counts.get("add") and not counts.get("update"):
        raise ValueError("写入失败：%s" % ("；".join(problems or ["未知原因"])))
    node_id = ""
    part = (changed or {}).get(scope_key) or {}
    for kind in ("node_add", "node_update"):
        ids = list(part.get(kind) or [])
        if ids:
            node_id = ids[0]
            break
    ensure_fresh(force=True)
    it = search(title, scope=("session:" + session_id) if scope_key == "session"
                and session_id else ("kb:" + scope_key if scope_key != "session" else ""),
                sources=("kb", "session"), limit=1)
    return {"target": scope_key, "source_name": source_name, "node_id": node_id,
            "kb_id": kb_id, "item": it[0] if it else None,
            "problems": problems or []}


def similar(text, target="auto", type="fact", session_id="", kb_id="", limit=3):
    """找目标范围内已有的相似条目，供「是否仍然添加」提示。"""
    t = _resolve_target(target, type, kb_id)
    toks = set(_tokens(text))
    if not toks:
        return []
    if t == "global":
        return [{"id": x["id"], "title": x.get("title") or _first_line(x["text"]),
                 "scope": x.get("scope"), "source_name": "全局记忆"}
                for x in search(text, sources=("manual", "task"), limit=limit)]

    if t == "profile":
        scope_key = get_profile_kb()
        nodes = (load_kb(scope_key) or {}).get("nodes") or []
        name = "用户画像"
    elif t.startswith("kb:"):
        scope_key = t[3:]
        kb = load_kb(scope_key) or {}
        nodes = kb.get("nodes") or []
        name = kb.get("name") or "知识库"
    else:
        s = find_session(load_sessions(), session_id) if session_id else None
        nodes = ((s or {}).get("memory") or {}).get("nodes") or []
        scope_key = "session"
        name = "本会话记忆"

    hits = []
    for n in nodes:
        body = ((n.get("title") or "") + " " + (n.get("content") or "")).lower()
        overlap = sum(1 for x in toks if x in body)
        if overlap / max(1, len(toks)) >= 0.5:
            hits.append({"id": n.get("id"), "title": n.get("title") or "",
                         "scope": scope_key, "source_name": name})
            if len(hits) >= limit:
                break
    return hits


def forget(mid):
    """只允许删除手动/任务来源；镜像条目请回原入口。"""
    row = get_db().execute(
        "SELECT source FROM memory_items WHERE id=?", (str(mid or ""),)).fetchone()
    if not row:
        return False
    if row["source"] in MIRROR_SOURCES:
        return False
    conn = get_db()
    cur = conn.execute("DELETE FROM memory_items WHERE id=?", (str(mid or ""),))
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------
# 检索
# ------------------------------------------------------------
def _score(row, toks, now, scope):
    text = (row.get("text") or "").lower()
    tags = (row.get("tags") or "").lower()
    score = float(row.get("importance") or 0) * 2.0
    for t in toks:
        score += text.count(t) * 1.6
        if t in tags:
            score += 1.0
    if scope and row.get("scope") == scope:
        score += 1.5
    age_days = max(0.0, (now - float(row.get("updated_at") or now)) / 86400)
    score += max(0.0, 1.0 - age_days / 90.0)
    score += min(int(row.get("use_count") or 0), 5) * 0.1
    return score


def _source_names():
    kbs = {k.get("id"): (k.get("name") or "知识库")
           for k in (load_kb_index().get("kbs") or [])}
    sessions = {s.get("id"): (s.get("title") or "会话")
                for s in (load_sessions().get("sessions") or [])}
    return kbs, sessions


def search(query, scope="", sources=None, limit=20):
    ensure_fresh()
    toks = _tokens(query)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM memory_items ORDER BY updated_at DESC LIMIT 800").fetchall()
    items = [dict(r) for r in rows]
    if sources:
        allow = set(sources)
        items = [x for x in items if x.get("source") in allow]
    now = _now()
    if toks:
        ql = query.lower()
        items = [x for x in items
                 if any(t in (x.get("text") or "").lower() for t in toks)
                 or ql in (x.get("text") or "").lower()]
    items.sort(key=lambda x: _score(x, toks, now, scope), reverse=True)
    top = items[:max(1, int(limit))]
    if top:
        conn.executemany(
            "UPDATE memory_items SET last_used_at=?, use_count=use_count+1 WHERE id=?",
            [(now, x["id"]) for x in top[:5]])
        conn.commit()

    kbs, sessions = _source_names()
    for x in top:
        x["title"] = x.get("title") or _first_line(x.get("text") or "")
        if x.get("source") == "kb":
            kid = (x.get("scope") or "").split(":", 1)[-1]
            x["source_name"] = kbs.get(kid, "知识库")
            x["kb_id"] = kid
        elif x.get("source") == "session":
            sid = (x.get("scope") or "").split(":", 1)[-1]
            x["source_name"] = sessions.get(sid, "本会话记忆")
            x["kb_id"] = "session"
        elif x.get("source") == "note":
            x["source_name"] = "笔记库"
            x["note_path"] = x.get("source_id") or ""
        elif x.get("source") == "task":
            x["source_name"] = "任务结果"
        else:
            x["source_name"] = "手动记忆"
    return top


def context_block(s, query):
    """给系统提示用的「长期记忆」：只取手动/任务来源，避免与知识库检索重复。"""
    sid = (s or {}).get("id") or ""
    items = search(query, scope="session:" + sid if sid else "",
                   sources=("manual", "task"), limit=3)
    if not items:
        return ""
    lines = ["【长期记忆】（来自你之前的记录，供参考，不必刻意复述）"]
    for it in items:
        lines.append("- " + (it.get("text") or "").replace("\n", " ")[:180])
    return "\n".join(lines)


# ------------------------------------------------------------
# 重建索引
# ------------------------------------------------------------
def reindex():
    """把知识库节点、笔记与会话记忆镜像进 memory_items。返回统计。"""
    conn = get_db()
    conn.execute(
        "DELETE FROM memory_items WHERE source IN ('kb','session','note')")
    count = 0
    now = _now()

    for meta in (load_kb_index().get("kbs") or []):
        kb = load_kb(meta.get("id"))
        if not kb:
            continue
        scope = "kb:" + str(kb.get("id"))
        for node in kb.get("nodes") or []:
            if count >= MAX_ITEMS:
                break
            title = str(node.get("title") or "").strip()
            content = str(node.get("content") or "").strip()
            text = _clip((title + "\n" + content).strip())
            if not text:
                continue
            tags = " ".join([str(node.get("category") or "")] +
                            [str(x) for x in (node.get("tags") or [])])
            cat = str(node.get("category") or "")
            ntype = "preference" if cat in ("喜好", "偏好") else (
                "event" if cat == "事件" else "note")
            conn.execute(
                "INSERT OR IGNORE INTO memory_items (id,scope,source,source_id,type,"
                "title,text,tags,importance,created_at,updated_at,last_used_at,use_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
                ("m_" + uuid.uuid4().hex[:12], scope, "kb",
                 str(node.get("id") or ""), ntype, title, text, tags, 0.45,
                 float(node.get("created_at") or now), now))
            count += 1

    for rel, content in (noteslib._all_notes() or {}).items():
        if count >= MAX_ITEMS:
            break
        title = noteslib.note_title(rel)
        text = _clip((title + "\n" + (content or "")).strip())
        if not text:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO memory_items (id,scope,source,source_id,type,"
            "title,text,tags,importance,created_at,updated_at,last_used_at,use_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
            ("m_" + uuid.uuid4().hex[:12], "note", "note", rel, "note",
             title, text, "笔记", 0.4, now, now))
        count += 1

    for s in (load_sessions().get("sessions") or []):
        if count >= MAX_ITEMS:
            break
        sid = s.get("id") or ""
        for node in ((s.get("memory") or {}).get("nodes") or []):
            title = str(node.get("title") or "").strip()
            content = str(node.get("content") or "").strip()
            text = _clip((title + "\n" + content).strip())
            if not text:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO memory_items (id,scope,source,source_id,type,"
                "title,text,tags,importance,created_at,updated_at,last_used_at,use_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
                ("m_" + uuid.uuid4().hex[:12], "session:" + str(sid), "session",
                 str(node.get("id") or ""),
                 "preference" if str(node.get("category") or "") in ("喜好", "偏好")
                 else ("event" if str(node.get("category") or "") == "事件" else "fact"),
                 title, text, str(node.get("category") or ""), 0.5,
                 float(node.get("created_at") or now), now))
            count += 1
    conn.commit()
    _save_sig(_signature())
    return {"mirrored": count, "total": stats()["total"]}


def stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()["n"]
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM memory_items GROUP BY source").fetchall()
    return {"total": total, "by_source": {r["source"]: r["n"] for r in rows}}


def estimate(session_id=""):
    """估算「会话记忆 / 用户画像」注入时的 token 负担。

    只读现有索引表，不触发 ensure_fresh（那里会写库，不该被一个「看一眼」的请求触发）。
    口径与 `context.estimate_tokens` 一致；两个来源的注入规则不同：
    - 用户画像：每轮固定注入最多 PROFILE_INJECT_LIMIT 条（其余靠检索）
    - 会话记忆：和知识库共用 INJECT_MAX_TOKENS 的检索预算
    """
    from app.context import estimate_tokens
    from app import knowledge
    conn = get_db()
    rows = conn.execute(
        "SELECT scope, text FROM memory_items WHERE source='session'"
    ).fetchall()
    sid = str(session_id or "").strip()
    want_scope = ("session:" + sid) if sid else ""
    out = {
        "session": {"items": 0, "chars": 0, "tokens": 0},
        "profile": {"items": 0, "chars": 0, "tokens": 0},
    }
    for r in rows:
        if want_scope and str(r["scope"] or "") != want_scope:
            continue
        text = str(r["text"] or "")
        out["session"]["items"] += 1
        out["session"]["chars"] += len(text)
        out["session"]["tokens"] += estimate_tokens(text)
    # 画像存在「用户画像」知识库里，不在 memory_items；这里只读索引，不调用 get_profile_kb
    #（那个函数找不到会**新建**一个库，一个「看一眼」的请求不该有副作用）
    try:
        idx = knowledge.load_kb_index()
        pid = ""
        for item in (idx.get("kbs") or []):
            if item.get("name") == knowledge.PROFILE_KB_NAME:
                pid = item.get("id") or ""
                break
        kb = knowledge.load_kb(pid) if pid else None
        for n in ((kb or {}).get("nodes") or []):
            text = ("%s\n%s" % (n.get("title") or "", n.get("content") or "")).strip()
            out["profile"]["items"] += 1
            out["profile"]["chars"] += len(text)
            out["profile"]["tokens"] += estimate_tokens(text)
    except Exception:                        # noqa: BLE001  画像读失败不影响会话记忆的估算
        pass
    out["profile_inject_limit"] = knowledge.PROFILE_INJECT_LIMIT
    out["inject_max_tokens"] = knowledge.inject_budget()
    return out



# ------------------------------------------------------------
# 能力
# ------------------------------------------------------------
def _cap_search(args, ctx):
    items = search(args.get("query") or "",
                   scope=args.get("scope") or "",
                   limit=args.get("limit") or 10)
    if not items:
        return "没有找到相关记忆。"
    return "\n".join("- [%s] %s" % (it.get("source_name") or it.get("type"),
                                    (it.get("text") or "")[:300]) for it in items)


def _cap_remember(args, ctx):
    sid = (ctx or {}).get("session_id") or ""
    try:
        r = add_memory(args.get("text") or "", type=args.get("type") or "fact",
                       target=args.get("target") or "session",
                       session_id=sid, importance=args.get("importance") or 0.6,
                       tags=args.get("tags") or "")
    except Exception as e:
        return "❌ 记忆写入失败：%s" % e
    return "已记住（%s）：%s" % (r.get("source_name") or r.get("target"),
                               (args.get("text") or "")[:120])


def _cap_forget(args, ctx):
    mid = args.get("id") or ""
    row = get_db().execute(
        "SELECT source FROM memory_items WHERE id=?", (mid,)).fetchone()
    if row and row["source"] in MIRROR_SOURCES:
        return "❌ 知识库/会话镜像是只读的，请到原入口删除。"
    return ("已删除记忆 %s" % mid) if forget(mid) else ("❌ 找不到记忆：%s" % mid)


capability("memory.search", "检索长期记忆（手动记录与任务结果）",
           {"query": "str", "scope": "str?", "limit": "int?"}, permission="read")(_cap_search)
capability("memory.remember", "记住一条信息，写回本会话记忆并同步索引",
           {"text": "str", "type": "str?", "importance": "0-1?",
            "tags": "str?"}, permission="write")(_cap_remember)
capability("memory.forget", "删除一条手动记忆，需要 id",
           {"id": "str"}, permission="write")(_cap_forget)
