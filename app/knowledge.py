# -*- coding: utf-8 -*-
"""知识库 / 记忆库：存储、检索注入与 AI 回写"""
import json
import os
import re
import time
import uuid

import requests

from app.store import ROOT, DATA_DIR, read_json, write_json, drop_cache, _lock
from app.config import get_api_conf, normalize_content
from app.profile import load_profile, save_profile
from app.sessions import load_sessions, save_sessions, find_session, ensure_session_kb_fields

# ============================================================
# 知识库 / 记忆库（分层记忆系统）
# ============================================================

KB_DIR = os.path.join(DATA_DIR, "knowledge")
KB_INDEX = os.path.join(KB_DIR, "index.json")
PROFILE_KB_NAME = "用户画像"
PICK_THRESHOLD = 20     # 候选条目超过此数才让模型挑选
PICK_LIMIT = 12         # 本地直接注入（或模型挑选）的上限条数
CAND_LIMIT = 60         # 交给候选池的上限
PROFILE_INJECT_LIMIT = 30  # 用户画像固定注入的上限条数

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")

# ============================================================
# 关系类型词表（融合「关系图谱」的语义关系与「事理图谱」的事件逻辑关系）
# 边是有向的：from -> to 读作「from <关系> to」
# ============================================================
RELATIONS = [
    ("related",   "相关",   "一般关联（默认）"),
    ("isa",       "上下位", "A 属于 / 是 B 的一种"),
    ("partof",    "组成",   "A 是 B 的组成部分"),
    ("attribute", "属性",   "B 是 A 的特征 / 属性"),
    ("same",      "同义",   "A 与 B 等价 / 同指"),
    ("opposite",  "对立",   "A 与 B 相反 / 互斥"),
    ("instance",  "例证",   "B 是 A 的实例 / 例子"),
    ("cause",     "因果",   "A 导致 / 引起 B"),
    ("condition", "条件",   "A 是 B 的前提条件"),
    ("sequence",  "顺承",   "A 先于 B 发生"),
    ("means",     "手段",   "通过 A 达成 B"),
    ("purpose",   "目的",   "A 是为了 B"),
    ("turn",      "转折",   "A 与 B 形成转折 / 让步"),
]
RELATION_LABEL = {c: lb for c, lb, _ in RELATIONS}
RELATION_DESC = {c: d for c, _, d in RELATIONS}
RELATION_CODES = [c for c, _, _ in RELATIONS]

# 归一化：把 AI 输出的自由说法映射回词表
_REL_ALIASES = {
    "相关": "related", "关联": "related", "有关": "related", "related": "related",
    "上下位": "isa", "属于": "isa", "是一": "isa", "包含": "isa", "isa": "isa", "subclass": "isa",
    "组成": "partof", "部分": "partof", "组成关系": "partof", "partof": "partof", "part_of": "partof",
    "属性": "attribute", "特征": "attribute", "具有": "attribute", "attribute": "attribute",
    "同义": "same", "等价": "same", "相同": "same", "同指": "same", "same": "same",
    "对立": "opposite", "相反": "opposite", "互斥": "opposite", "矛盾": "opposite", "opposite": "opposite",
    "例证": "instance", "例子": "instance", "实例": "instance", "例如": "instance", "instance": "instance",
    "因果": "cause", "导致": "cause", "引起": "cause", "因为": "cause", "cause": "cause", "causes": "cause",
    "条件": "condition", "前提": "condition", "condition": "condition",
    "顺承": "sequence", "先于": "sequence", "然后": "sequence", "接着": "sequence", "sequence": "sequence",
    "手段": "means", "方法": "means", "方式": "means", "通过": "means", "means": "means",
    "目的": "purpose", "为了": "purpose", "purpose": "purpose",
    "转折": "turn", "但是": "turn", "让步": "turn", "turn": "turn",
}


def normalize_relation(raw):
    """把自由文本 / AI 输出的关系名归一成词表里的 code；无法识别时退回 related"""
    if not raw:
        return "related"
    s = str(raw).strip().lower().replace(" ", "").replace("-", "_")
    if s in RELATION_CODES:
        return s
    if s in _REL_ALIASES:
        return _REL_ALIASES[s]
    for k, v in _REL_ALIASES.items():      # 包含匹配，如「因果关系」
        if k in s:
            return v
    return "related"


def relation_label(code):
    return RELATION_LABEL.get(code, "相关")


def relations_payload():
    """给前端用的关系词表"""
    return [{"code": c, "label": lb, "desc": d} for c, lb, d in RELATIONS]


def _kb_id():
    return "kb_" + uuid.uuid4().hex[:10]


def _node_id():
    return "n_" + uuid.uuid4().hex[:10]


def _edge_id():
    return "e_" + uuid.uuid4().hex[:10]


_kb_dir_ready = False


def _ensure_kb_dir():
    global _kb_dir_ready
    if not _kb_dir_ready:
        os.makedirs(KB_DIR, exist_ok=True)
        _kb_dir_ready = True


def load_kb_index():
    _ensure_kb_dir()
    data = read_json(KB_INDEX)
    if not isinstance(data, dict):
        return {"kbs": []}
    data.setdefault("kbs", [])
    return data


def save_kb_index(index):
    _ensure_kb_dir()
    write_json(KB_INDEX, index)


def is_profile_kb(kb_id):
    item = next((k for k in load_kb_index()["kbs"] if k["id"] == kb_id), None)
    return bool(item and item.get("name") == PROFILE_KB_NAME)


def _kb_path(kb_id):
    return os.path.join(KB_DIR, str(kb_id) + ".json")


def load_kb(kb_id):
    if not kb_id:
        return None
    path = _kb_path(kb_id)
    if not os.path.exists(path):
        return None
    kb = read_json(path)
    if not isinstance(kb, dict):
        return None
    kb.setdefault("nodes", [])
    kb.setdefault("edges", [])
    return kb


def save_kb(kb):
    _ensure_kb_dir()
    kb["updated_at"] = time.time()
    write_json(_kb_path(kb["id"]), kb)
    idx = load_kb_index()
    for item in idx["kbs"]:
        if item["id"] == kb["id"]:
            item["name"] = kb.get("name", item.get("name"))
            item["updated_at"] = kb["updated_at"]
            item["node_count"] = len(kb.get("nodes", []))
            break
    save_kb_index(idx)


def new_kb(name):
    now = time.time()
    kb = {"id": _kb_id(), "name": (name or "未命名知识库").strip() or "未命名知识库",
          "nodes": [], "edges": [], "created_at": now, "updated_at": now}
    save_kb(kb)
    idx = load_kb_index()
    idx["kbs"].append({"id": kb["id"], "name": kb["name"], "ai_write": True,
                       "created_at": now, "updated_at": now, "node_count": 0})
    save_kb_index(idx)
    return kb


def delete_kb(kb_id):
    """删除知识库并清理各会话的绑定；「用户画像」为系统库，禁止删除。成功返回 True"""
    if is_profile_kb(kb_id):
        return False
    idx = load_kb_index()
    idx["kbs"] = [k for k in idx["kbs"] if k["id"] != kb_id]
    save_kb_index(idx)
    path = _kb_path(kb_id)
    if os.path.exists(path):
        os.remove(path)
    drop_cache(path)
    # 清理会话里指向该库的悬空绑定
    sdata = load_sessions()
    changed = False
    for s in sdata.get("sessions", []):
        if kb_id in (s.get("kb_ids") or []):
            s["kb_ids"] = [k for k in s["kb_ids"] if k != kb_id]
            changed = True
    if changed:
        with _lock:
            save_sessions(sdata)
    return True


def set_kb_write(kb_id, enabled):
    idx = load_kb_index()
    for item in idx["kbs"]:
        if item["id"] == kb_id:
            item["ai_write"] = bool(enabled)
            break
    save_kb_index(idx)
    return idx


def get_profile_kb():
    """返回「用户画像」知识库的 id（不存在则创建）"""
    idx = load_kb_index()
    for item in idx["kbs"]:
        if item.get("name") == PROFILE_KB_NAME:
            return item["id"]
    return new_kb(PROFILE_KB_NAME)["id"]


def make_node(title, content="", category="其他", tags=None, source=None):
    now = time.time()
    return {"id": _node_id(), "title": (title or "").strip()[:120], "content": content or "",
            "category": category or "其他", "tags": tags or [], "locked": False,
            "x": None, "y": None, "created_at": now, "updated_at": now, "source": source}


def migrate_profile_to_kb():
    """一次性迁移：把 user_profile.auto 的旧条目导入「用户画像」知识库，之后由知识库接管"""
    profile = load_profile()
    if profile.get("_kb_migrated"):
        return
    auto = (profile.get("auto") or "").strip()
    kb_id = get_profile_kb()
    kb = load_kb(kb_id)
    if auto and kb is not None and not kb.get("nodes"):
        for ln in auto.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            cat, title = "其他", ln
            for sep in ("｜", "|"):
                if sep in ln:
                    a, b = ln.split(sep, 1)
                    cat, title = (a.strip() or "其他"), (b.strip() or ln)
                    break
            kb["nodes"].append(make_node(title, category=cat, source="user_profile"))
        save_kb(kb)
    profile["_kb_migrated"] = True
    with _lock:
        save_profile(profile)


def tokenize(text):
    return set(_TOKEN_RE.findall((text or "").lower()))


def score_node(node, q_tokens):
    text = " ".join([node.get("title", ""), node.get("content", ""),
                     node.get("category", ""), " ".join(node.get("tags") or [])])
    n_tokens = tokenize(text)
    if not n_tokens or not q_tokens:
        return 0
    return len(n_tokens & q_tokens)


def _extract_json(text):
    """从模型回复里尽量解析出 JSON（兼容 ```json 包裹与前后废话）"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = t.find(a), t.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _chat_json(prompt, max_tokens=800, temperature=0.2):
    """调用聊天 API 并解析 JSON；失败返回 None"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return None
    try:
        resp = requests.post(api_url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8"},
            json={"model": api_model,
                  "messages": [{"role": "system", "content": "你是记忆整理助手，只输出 JSON，不要解释。"},
                               {"role": "user", "content": prompt}],
                  "stream": False, "max_tokens": max_tokens, "temperature": temperature},
            timeout=(10, 90))
        resp.raise_for_status()
        obj = resp.json()
        text = normalize_content(((obj.get("choices") or [{}])[0].get("message") or {}).get("content"))
        return _extract_json(text)
    except Exception as e:
        print(f"[记忆] 调用失败：{e}")
        return None


def pick_relevant(nodes, user_text):
    """让模型从候选里挑相关条目，返回下标列表；失败返回 None"""
    lines = []
    for i, n in enumerate(nodes):
        lines.append(f"{i}. [{n.get('category', '其他')}] {n.get('title', '')}"
                     f" —— {(n.get('content') or '')[:60]}")
    prompt = ("下面是候选记忆条目（编号. 分类 标题 —— 摘要）。\n"
              "用户这句话是：\n" + user_text + "\n\n"
              "请挑出真正相关、有助于回答这句话的编号，只输出 JSON 整数数组（如 [0,3]）；"
              "没有相关项就输出 []。\n\n" + "\n".join(lines))
    data = _chat_json(prompt, max_tokens=200)
    if isinstance(data, dict):
        data = data.get("ids") or data.get("indices") or []
    if not isinstance(data, list):
        return None
    out = []
    for x in data:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(nodes):
            out.append(i)
    return out


def retrieve_memory(s, user_text):
    """检索记忆：用户画像固定注入 + 其余来源两段式召回。返回 (文本块, 条数)"""
    parts, count = [], 0
    pkb_id = get_profile_kb()
    profile_nodes = []
    if pkb_id:
        kb = load_kb(pkb_id)
        if kb and kb.get("nodes"):
            profile_nodes = kb["nodes"]
            parts.append("【用户画像】（长期偏好，供参考）")
            for n in profile_nodes[:PROFILE_INJECT_LIMIT]:
                parts.append(f"- （{n.get('category', '其他')}）{n.get('title', '')}")
                count += 1

    seen = set(n.get("id") for n in profile_nodes)
    items = []
    for n in (s.get("memory") or {}).get("nodes", []):
        if n.get("id") not in seen:
            seen.add(n.get("id"))
            items.append((n, "会话记忆"))
    for kb_id in (s.get("kb_ids") or []):
        if kb_id == pkb_id:
            continue
        kb = load_kb(kb_id)
        if not kb:
            continue
        for n in kb.get("nodes", []):
            if n.get("id") not in seen:
                seen.add(n.get("id"))
                items.append((n, kb.get("name") or "知识库"))

    q = tokenize(user_text)
    scored = [(score_node(n, q), n, sc) for n, sc in items]
    scored = [t for t in scored if t[0] > 0]
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        cand = scored[:CAND_LIMIT]
        if len(cand) > PICK_THRESHOLD:
            idxs = pick_relevant([c[1] for c in cand], user_text)
            chosen = [cand[i] for i in idxs] if idxs is not None else cand[:PICK_LIMIT]
        else:
            chosen = cand[:PICK_LIMIT]
        if chosen:
            parts.append("【相关记忆】（来自知识库/会话记忆，供参考，不必刻意复述）")
            for _, n, sc in chosen:
                content = (n.get("content") or "").strip()
                parts.append(f"- （{sc}·{n.get('category', '其他')}）{n.get('title', '')}"
                             + (f"：{content}" if content else ""))
                count += 1

    if not parts:
        return "", 0
    return "\n".join(parts), count


def _find_node(nodes, nid=None, title=None):
    if nid:
        for n in nodes:
            if n.get("id") == nid:
                return n
    if title:
        t = title.strip()
        for n in nodes:
            if (n.get("title") or "").strip() == t:
                return n
    return None


def apply_memory_ops(session_id, allowed_kb_ids, ops):
    """把 AI 产出的操作指令应用到会话记忆与允许的知识库"""
    sdata = load_sessions()
    s = find_session(sdata, session_id)
    if not s:
        return
    ensure_session_kb_fields(s)
    kb_cache, s_changed, touched = {}, False, set()
    counts = {}

    def scope_store(key):
        if key == "session":
            return s["memory"]["nodes"], s["memory"]["edges"]
        kb = kb_cache.get(key)
        return (kb["nodes"], kb["edges"]) if kb else (None, None)

    for op in ops:
        if not isinstance(op, dict):
            continue
        key = op.get("scope") or "session"
        if key != "session":
            if key not in allowed_kb_ids:
                continue
            if key not in kb_cache:
                kb_cache[key] = load_kb(key)
        nodes, edges = scope_store(key)
        if nodes is None:
            continue
        kind = (op.get("op") or "").lower()
        did = False
        try:
            if kind == "add":
                title = (op.get("title") or "").strip()
                if not title:
                    continue
                exist = _find_node(nodes, title=title)
                if exist and not exist.get("locked"):
                    if op.get("content"):
                        exist["content"] = op["content"]
                    exist["category"] = op.get("category") or exist.get("category", "其他")
                    exist["updated_at"] = time.time()
                    did = True
                    counts["update"] = counts.get("update", 0) + 1
                elif not exist:
                    nodes.append(make_node(title, op.get("content") or "",
                                           op.get("category") or "其他",
                                           op.get("tags") or [], session_id))
                    did = True
                    counts["add"] = counts.get("add", 0) + 1
            elif kind == "update":
                n = _find_node(nodes, op.get("id"), op.get("title"))
                if n and not n.get("locked"):
                    for f in ("content", "title", "category", "tags"):
                        if op.get(f):
                            n[f] = op[f]
                    n["updated_at"] = time.time()
                    did = True
                    counts["update"] = counts.get("update", 0) + 1
            elif kind == "delete":
                n = _find_node(nodes, op.get("id"), op.get("title"))
                if n and not n.get("locked"):
                    nid = n["id"]
                    nodes.remove(n)
                    edges[:] = [e for e in edges if e.get("from") != nid and e.get("to") != nid]
                    did = True
                    counts["delete"] = counts.get("delete", 0) + 1
            elif kind == "link":
                a = _find_node(nodes, op.get("from"), op.get("from"))
                b = _find_node(nodes, op.get("to"), op.get("to"))
                if a and b and a["id"] != b["id"]:
                    rel = normalize_relation(op.get("relation") or op.get("label"))
                    label = (op.get("label") or "").strip() or relation_label(rel)
                    # 同一对节点允许存在不同的关系，完全重复才跳过
                    dup = any(e.get("from") == a["id"] and e.get("to") == b["id"]
                              and (e.get("relation") or "related") == rel for e in edges)
                    if not dup:
                        edges.append({"id": _edge_id(), "from": a["id"], "to": b["id"],
                                      "label": label, "relation": rel, "type": "relation"})
                        did = True
                        counts["link"] = counts.get("link", 0) + 1
        except Exception:
            continue
        if not did:
            continue
        if key == "session":
            s_changed = True
        else:
            touched.add(key)

    if s_changed:
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(sdata)
    for key in touched:
        if kb_cache.get(key):
            save_kb(kb_cache[key])
    return counts