# -*- coding: utf-8 -*-
"""知识库 / 记忆库：存储、检索注入与 AI 回写"""
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid

import requests

from app.store import DATA_DIR, read_json, write_json, drop_cache, _lock
from app.config import (get_api_conf, light_chat_is_local, load_config,
                        normalize_content)
from app.context import estimate_tokens
from app.profile import load_profile, save_profile
from app.sessions import load_sessions, save_sessions, find_session, ensure_session_kb_fields
from app.textmatch import tokens as _text_tokens

# ============================================================
# 知识库 / 记忆库（分层记忆系统）
# ============================================================

KB_DIR = os.path.join(DATA_DIR, "knowledge")
KB_INDEX = os.path.join(KB_DIR, "index.json")
PROFILE_KB_NAME = "用户画像"
MAX_KB_PROMPT = 2000      # 单个知识库自定义提示词上限（注入系统提示用）
PICK_THRESHOLD = 40     # 候选条目超过此数、且分数没有明显断层时，才让模型挑选
PICK_LIMIT = 12         # 本地直接注入（或模型挑选）的上限条数
CAND_LIMIT = 60         # 交给候选池的上限
PROFILE_INJECT_LIMIT = 30  # 用户画像固定注入的上限条数
NEIGHBOR_LIMIT = 4      # 命中节点额外带上的一跳邻居上限
MATCH_KEEP = PICK_LIMIT - NEIGHBOR_LIMIT   # 直接命中的保底条数，其余名额与邻居同池竞争
INJECT_NODE_CHARS = 400 # 单条节点注入正文上限
INJECT_MAX_TOKENS = 3000  # 相关记忆整块注入的 token 预算（可被 memory_inject_tokens 覆盖）
WORLD_MAX_CHARS = 9000    # world_text（整库喂给编剧）的字符上限
# 记忆索引（L3，对齐 CC 的 MEMORY.md）：常驻标题清单按「行数 + 字节」双上限截断。
MEMORY_INDEX_LINES = 200
MEMORY_INDEX_BYTES = 25000
# 记忆挑选结果缓存（进程内）：key -> (时间戳, 下标列表)。
# 挑选是一次非流式模型往返且位于首字节前，同一问话/同一批候选重复触发时可直接复用。
_PICK_CACHE = {}
_PICK_CACHE_MAX = 64
_PICK_TTL = 120.0       # 秒

# 「用模型挑记忆」的总开关（配置 memory_llm_pick，默认关）。
# 每次 retrieve_memory 都读一次文件不划算，缓存 1 秒；设置保存后最多 1 秒生效。
_PICK_CFG = {"t": 0.0, "v": False}


def _llm_pick_enabled():
    now = time.time()
    if now - _PICK_CFG["t"] < 1.0:
        return _PICK_CFG["v"]
    try:
        from app.config import load_config
        v = bool(load_config().get("memory_llm_pick", False))
    except Exception:
        v = False
    _PICK_CFG.update({"t": now, "v": v})
    return v


def inject_budget():
    """每轮「相关记忆」整块的 token 预算（默认 3000，可配置）。"""
    try:
        v = int((load_config() or {}).get("memory_inject_tokens") or INJECT_MAX_TOKENS)
    except (TypeError, ValueError):
        v = INJECT_MAX_TOKENS
    return max(300, min(50000, v))

# 「规则型」类别：这些节点承载的是世界规则 / 能力规则 / 力量体系之类的**硬约束**，
# 必须**每轮常驻注入**，不能只靠关键词检索命中——否则模型会把规则当成「可选参考」而忽略。
# （按「类别名」匹配，与具体世界/角色无关；某些知识库不用这些类别名时，这里自然什么都不常驻。）
PINNED_CATEGORIES = ("规则", "能力", "力量")

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
    ("means",     "手段",   "通过 A 达成 B"),
    ("purpose",   "目的",   "A 是为了 B"),
]
# 已下线的旧关系码：新连线不再提供，但老数据里的边要照旧显示出原来的标签，
# 不能因为词表收缩就把历史图谱里的边全部显示成「相关」。
LEGACY_RELATION_LABEL = {"sequence": "顺承", "turn": "转折"}
RELATION_LABEL = {c: lb for c, lb, _ in RELATIONS}
RELATION_LABEL.update(LEGACY_RELATION_LABEL)
RELATION_DESC = {c: d for c, _, d in RELATIONS}
RELATION_CODES = [c for c, _, _ in RELATIONS]

# 天然对称（互相）的关系：连线默认画双向箭头
SYMMETRIC_RELATIONS = {"related", "same", "opposite"}


def is_symmetric(code):
    return (code or "") in SYMMETRIC_RELATIONS


# ------------------------------------------------------------
# 节点外观（大小 / 颜色）与图谱力学默认值
# ------------------------------------------------------------
NODE_SIZE_MIN, NODE_SIZE_MAX = 8, 60

# AI 只能用这些命名色（名称 -> 固定 hex），避免写出看不清的颜色
NODE_PALETTE = {
    "red": "#e5484d", "orange": "#e8890c", "yellow": "#d9a406",
    "green": "#30a46c", "teal": "#12a594", "blue": "#4d6bfe",
    "purple": "#8e4ec6", "pink": "#d6409f", "gray": "#8b8d98",
}
_PALETTE_ALIASES = {
    "红": "red", "橙": "orange", "黄": "yellow", "绿": "green",
    "青": "teal", "蓝": "blue", "紫": "purple", "粉": "pink", "灰": "gray",
}


def normalize_color(raw):
    """把颜色归一成 hex；无法识别返回 None（表示不设颜色 / 恢复默认）"""
    if raw is None:
        return None
    s = str(raw).strip().lower().lstrip("#")
    if not s:
        return None
    if s in NODE_PALETTE:
        return NODE_PALETTE[s]
    if s in _PALETTE_ALIASES:
        return NODE_PALETTE[_PALETTE_ALIASES[s]]
    for k, v in _PALETTE_ALIASES.items():
        if k in s:
            return NODE_PALETTE[v]
    if re.match(r"^[0-9a-f]{6}$", s):
        return "#" + s
    if re.match(r"^[0-9a-f]{3}$", s):
        return "#" + "".join(ch * 2 for ch in s)
    return None


def normalize_size(raw):
    """把节点大小夹紧到 [8,60] 的整数；非法 / 空返回 None"""
    if raw is None or raw == "":
        return None
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return max(NODE_SIZE_MIN, min(NODE_SIZE_MAX, n))


def default_graph():
    """图谱力学默认值（四项，对齐 Obsidian 力导向：斥力 / 中心引力 / 弹簧强度 / 线段长度）
    repel 为正数，前端取负作为 vis 的 gravitationalConstant。"""
    return {"repel": 9000, "center": 0.3, "link": 0.04, "distance": 130}


def _apply_appearance(node, op):
    """把 op 里的 size / color 应用到节点；color 传空串表示清除（恢复默认）"""
    if "size" in op:
        sz = normalize_size(op.get("size"))
        if sz is None:
            node.pop("size", None)
        else:
            node["size"] = sz
    if "color" in op:
        col = normalize_color(op.get("color"))
        if col:
            node["color"] = col
        else:
            node.pop("color", None)

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
    # 「顺承 / 转折」已从词表下线：新边退回 related，避免模型继续产出会破坏结构的时间顺序噪声；
    # 老数据里 relation=sequence/turn 的边不受影响（它们不经过归一化，标签由 LEGACY_RELATION_LABEL 兜底）。
    "顺承": "related", "先于": "related", "然后": "related", "接着": "related", "sequence": "related",
    "手段": "means", "方法": "means", "方式": "means", "通过": "means", "means": "means",
    "目的": "purpose", "为了": "purpose", "purpose": "purpose",
    "转折": "related", "但是": "related", "让步": "related", "turn": "related",
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
    """给前端用的关系词表（sym=true 表示天然对称，连线默认双向）"""
    return [{"code": c, "label": lb, "desc": d, "sym": c in SYMMETRIC_RELATIONS}
            for c, lb, d in RELATIONS]


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
    kb.setdefault("graph", default_graph())
    kb.setdefault("prompt", "")
    kb.setdefault("families", [])
    kb.setdefault("layout_mode", "stable")
    kb.setdefault("layout_version", 1)
    for node in kb.get("nodes") or []:
        if isinstance(node, dict):
            node.setdefault("notes", [])
            node.setdefault("aliases", [])
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
          "prompt": "", "families": [], "layout_mode": "stable", "layout_version": 1,
          "nodes": [], "edges": [], "graph": default_graph(),
          "created_at": now, "updated_at": now}
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


def _clean_aliases(v):
    """别名：列表或逗号分隔字符串；去重、限长、最多 8 个。"""
    if isinstance(v, str):
        v = re.split(r"[,，、;；\n]", v)
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        s = str(x or "").strip()[:40]
        if s and s not in out:
            out.append(s)
    return out[:8]


def make_node(title, content="", category="其他", tags=None, source=None, size=None,
              color=None, aliases=None):
    now = time.time()
    node = {"id": _node_id(), "title": (title or "").strip()[:120], "content": content or "",
            "category": category or "其他", "tags": tags or [], "aliases": _clean_aliases(aliases),
            "locked": False, "notes": [], "x": None, "y": None,
            "created_at": now, "updated_at": now, "source": source}
    if size is not None:
        node["size"] = size
    if color:
        node["color"] = color
    return node


def append_node_note(node, content, source="user"):
    """给节点追加一条时间线补充；不改原文。返回 note 或 None。"""
    text = str(content or "").strip()
    if not node or not text or node.get("locked"):
        return None
    note = {"id": "note_" + uuid.uuid4().hex[:10], "content": text[:4000],
            "created_at": time.time(), "source": source or "user"}
    node.setdefault("notes", []).append(note)
    node["updated_at"] = time.time()
    return note


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


def tokenize(text, limit=256):
    """英文按词、中文按双字 + 整段；与统一索引共用 app.textmatch。"""
    return set(_text_tokens(text, limit=limit))


def _node_fields(node):
    """把节点拆成字段级 token 集合，供加权评分复用。"""
    aliases = set()
    for a in (node.get("aliases") or []):
        aliases |= tokenize(a)
    tags = set()
    for t in (node.get("tags") or []):
        tags |= tokenize(t)
    return {
        "title": tokenize(node.get("title") or ""),
        "aliases": aliases,
        "tags": tags,
        "category": tokenize(node.get("category") or ""),
        "body": tokenize(node.get("content") or ""),
    }


def _score_fields(fields, q_tokens, df=None, total=0):
    """字段加权 + IDF + 长度归一化。
    标题/别名权重最高，其次标签；IDF 压掉「的/我」这类人人都有、没有区分度的词；
    长度归一化避免内容堆得多的节点仅凭字数霸榜。"""
    score = 0.0
    for t in q_tokens:
        w = 1.0
        if df is not None and total:
            w = math.log((total + 1.0) / (df.get(t, 0) + 1.0)) + 1.0
        if t in fields["title"] or t in fields["aliases"]:
            score += 3.0 * w
        if t in fields["tags"]:
            score += 2.0 * w
        if t in fields["category"]:
            score += 1.5 * w
        if t in fields["body"]:
            score += 1.0 * w
    if score <= 0:
        return 0.0
    size = len(fields["title"]) + len(fields["body"])
    return score / (1.0 + math.log(1.0 + size / 200.0))


def score_node(node, q_tokens, df=None, total=0):
    """兼容旧调用：单节点评分（每次重新切分字段）。"""
    return _score_fields(_node_fields(node), q_tokens, df, total)


def _clear_winner(cand):
    """前 12 名的分数是否已经明显断层：是就不必再花一次模型调用去挑。"""
    if len(cand) <= PICK_LIMIT:
        return True
    return cand[0][0] >= cand[PICK_LIMIT][0] * 2.0


# ---- 向量召回（可选）----
# 复用 codeindex 的本地 embedding（bge-small-zh-v1.5）：关键词检索遇到「换个说法」
# 会直接漏掉，向量能补上这类同义召回。模型不可用时完全回退到原来的 IDF 排序。
_VEC_LOCK = threading.Lock()
_VEC_CACHE = {"sig": "", "mat": None}
VEC_MIN = 0.35        # 余弦低于此视为不相关（只在关键词也 0 分时用于过滤）
VEC_WEIGHT = 0.55     # 混合分里向量的权重，其余给关键词


def _vec_text(node):
    """参与向量化的文本：标题 + 别名 + 分类 + 正文前 300 字。"""
    parts = [str(node.get("title") or ""), str(node.get("category") or "")]
    parts += [str(a) for a in (node.get("aliases") or [])]
    parts.append(str(node.get("content") or "")[:300])
    return "\n".join(p for p in parts if p).strip()


def _vec_matrix(items):
    """候选节点的向量矩阵（按内容签名缓存）。模型不可用返回 None。"""
    try:
        from app import codeindex
        if not codeindex.model_ready():
            return None
        h = hashlib.sha1()
        for it in items:
            n = it["node"]
            h.update(str(n.get("id")).encode("utf-8", "ignore"))
            h.update(str(n.get("updated_at") or 0).encode("utf-8", "ignore"))
            h.update(str(len(str(n.get("content") or ""))).encode("utf-8", "ignore"))
        sig = h.hexdigest()
        with _VEC_LOCK:
            if _VEC_CACHE["sig"] == sig and _VEC_CACHE["mat"] is not None:
                return _VEC_CACHE["mat"]
        mat = codeindex.embed_texts([_vec_text(it["node"]) for it in items])
        if mat is None:
            return None
        with _VEC_LOCK:
            _VEC_CACHE.update({"sig": sig, "mat": mat})
        return mat
    except Exception as e:
        print("[记忆] 向量化失败：%s" % e)
        return None


def _vec_scores(items, query):
    """{node_id: 余弦相似度}；模型不可用返回 None。"""
    mat = _vec_matrix(items)
    if mat is None:
        return None
    try:
        import numpy as np

        from app import codeindex
        qv = codeindex.embed_texts([query])
        if qv is None or len(qv) == 0:
            return None
        a = qv[0]
        denom = np.linalg.norm(mat, axis=1) * (np.linalg.norm(a) + 1e-9) + 1e-9
        sims = mat.dot(a) / denom
        out = {}
        for it, s in zip(items, sims):
            nid = it["node"].get("id")
            if nid:
                out[nid] = float(s)
        return out
    except Exception as e:
        print("[记忆] 向量打分失败：%s" % e)
        return None


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


def _pick_cache_key(nodes, user_text):
    import hashlib
    h = hashlib.sha1()
    h.update((user_text or "").encode("utf-8", "ignore"))
    for n in nodes:
        h.update(str(n.get("id") or n.get("title") or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def pick_relevant(nodes, user_text):
    """让模型从候选里挑相关条目，返回下标列表；失败返回 None

    带进程内短 TTL 缓存：同一句问话 + 同一批候选直接复用结果。
    记忆挑选是一次**非流式模型往返**，且在「首字节之前」，命中缓存可省掉这段阻塞。
    """
    key = _pick_cache_key(nodes, user_text)
    hit = _PICK_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _PICK_TTL:
        return hit[1]
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
    _PICK_CACHE[key] = (time.time(), out)
    if len(_PICK_CACHE) > _PICK_CACHE_MAX:
        _PICK_CACHE.pop(next(iter(_PICK_CACHE)))
    return out


def _collect_items(s, include_kbs=True, exclude_pinned=False, include_session=True):
    """收集可检索节点，同时保留各来源的节点表与边（供邻居扩展）。
    返回 (items, edges_map, nodes_map)。"""
    def _skip(n):
        return exclude_pinned and str(n.get("category") or "") in PINNED_CATEGORIES

    pkb_id = get_profile_kb()
    scopes = [("session", "会话记忆", s)] if include_session else []
    if include_kbs:
        for kb_id in (s.get("kb_ids") or []):
            if kb_id == pkb_id:       # 画像条目标题已固定注入，不再参与相关记忆
                continue
            kb = load_kb(kb_id)
            if kb:
                scopes.append((kb_id, kb.get("name") or "知识库", kb))

    items, edges_map, nodes_map = [], {}, {}
    seen = set()
    for key, name, obj in scopes:
        if key == "session":
            mem = obj.get("memory") or {}
            nodes, edges = mem.get("nodes") or [], mem.get("edges") or []
        else:
            nodes, edges = obj.get("nodes") or [], obj.get("edges") or []
        nodes_map[key] = {n.get("id"): n for n in nodes if isinstance(n, dict)}
        # S2：失效边保留在文件里，但不参与检索 / 邻居扩展
        edges_map[key] = [e for e in edges if isinstance(e, dict) and edge_is_valid(e)]
        for n in nodes:
            if not isinstance(n, dict):
                continue
            nid = n.get("id")
            if nid in seen or _skip(n):
                continue
            seen.add(nid)
            items.append({"node": n, "scope": name, "scope_key": key})
    return items, edges_map, nodes_map


def _rank_items(items, query, allow_model=True):
    """字段加权 + IDF 排序；必要时才让模型挑选。
    返回 (chosen[(score, item)], q_tokens, 是否触发了模型挑选)。

    向量模型可用时，排序改成「关键词 + 向量余弦」混合（同义召回）；
    不可用时与旧行为完全一致，不会因为没装 fastembed 退化出错。

    allow_model=False 时只用本地排序：模型挑选是一次非流式往返，且在**首字节之前**，
    实测把「提示词装配」这一段拖到 p90 1.9s / 最坏 7s。默认由 retrieve_memory 按配置传入。
    """
    q = set(tokenize(query, limit=24))
    if not q or not items:
        return [], q, False
    rows = [(_node_fields(it["node"]), it) for it in items]
    df = {}
    for fields, _it in rows:
        for t in set().union(*fields.values()):
            df[t] = df.get(t, 0) + 1
    total = len(rows)
    kw = [_score_fields(fields, q, df, total) for fields, _it in rows]
    vec = _vec_scores(items, query)
    scored = []
    if vec is None:                       # 回退：原来的纯关键词排序
        scored = [(sc, it) for sc, (_f, it) in zip(kw, rows) if sc > 0]
    else:
        top = max(kw) or 1.0
        for sc, (_f, it) in zip(kw, rows):
            v = max(0.0, min(1.0, vec.get(it["node"].get("id"), 0.0)))
            if sc <= 0 and v < VEC_MIN:   # 关键词没命中、向量也不像 → 丢弃
                continue
            mixed = (1.0 - VEC_WEIGHT) * (sc / top) + VEC_WEIGHT * v
            scored.append((mixed * 100.0, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    cand = scored[:CAND_LIMIT]
    if not cand:
        return [], q, False
    triggered = bool(allow_model) and len(cand) > PICK_THRESHOLD and not _clear_winner(cand)
    if triggered:
        idxs = pick_relevant([c[1]["node"] for c in cand], query)
        chosen = [cand[i] for i in idxs] if idxs is not None else cand[:PICK_LIMIT]
    else:
        chosen = cand[:PICK_LIMIT]
    return chosen, q, triggered


def _expand_neighbors(chosen, edges_map, nodes_map, limit=NEIGHBOR_LIMIT):
    """给命中节点各补一跳邻居（入边/出边都算），最多 limit 条。"""
    out, seen = [], {it["node"].get("id") for _score, it in chosen}
    for score, it in chosen:
        if len(out) >= limit:
            break
        nid, key = it["node"].get("id"), it["scope_key"]
        for e in edges_map.get(key) or []:
            if len(out) >= limit:
                break
            if e.get("from") == nid:
                oid = e.get("to")
            elif e.get("to") == nid:
                oid = e.get("from")
            else:
                continue
            if not oid or oid in seen:
                continue
            node = (nodes_map.get(key) or {}).get(oid)
            if not node:
                continue
            seen.add(oid)
            rel = e.get("label") or relation_label(e.get("relation"))
            fact = normalize_edge_fact(e.get("fact"))[:80]
            if fact:                      # 连线的「为什么」比关系名更有信息量，优先带给模型
                rel = rel + "：" + fact
            out.append((score * 0.6,
                        {"node": node, "scope": it["scope"], "scope_key": key},
                        rel))
    return out


def _merge_ranked(chosen, neighbors, limit=PICK_LIMIT, match_keep=MATCH_KEEP):
    """S4a：把直接命中与一跳邻居放进同一个排序池，返回 [(score, item, relation, kind)]。

    规则：先按分数保底保留 `match_keep` 条直接命中，剩余名额由「低分命中 + 邻居」按分数竞争。
    这样既不会让低分邻居把命中全挤掉，也不再让邻居永远排在所有命中之后（原来的做法
    是命中占满 PICK_LIMIT 后再硬塞 4 条邻居，等于 16 条都要付费）。
    """
    ordered = [(sc, it, "", "match") for sc, it in chosen[:match_keep]]
    rest = [(sc, it, "", "match") for sc, it in chosen[match_keep:]]
    rest += [(sc, it, rel, "neighbor") for sc, it, rel in neighbors]
    rest.sort(key=lambda x: x[0], reverse=True)
    return (ordered + rest)[:limit]


def _inject_line(node, score=None, relation=""):
    """把节点压成一行注入文本：正文截断，保留分类与关系信息。"""
    content = (node.get("content") or "").strip()
    if len(content) > INJECT_NODE_CHARS:
        content = content[:INJECT_NODE_CHARS] + "…"
    cat = node.get("category", "其他")
    title = node.get("title", "")
    if relation:
        head = f"- （关联·{relation}·{cat}）{title}"
    else:
        head = f"- （{round(score or 0, 2)}·{cat}）{title}"
    return head + (f"：{content}" if content else "")


def retrieve_memory(s, user_text, exclude_pinned=False, skip_profile=False, include_kbs=True,
                    recent_text="", budget_tokens=None, include_session=True):
    """检索记忆：用户画像固定注入 + 其余来源两段式召回。返回 (文本块, 条数)

    exclude_pinned=True 时跳过「规则型」节点——它们由 pinned_block 常驻注入，
    避免同一批规则既进检索又进常驻、内容重复。
    ※ 只有确实会调用 pinned_block 的场景（GAL 模式）才该传 True；
      普通聊天传 False，否则这些记忆会被排除后**没有任何东西替代**。

    skip_profile=True 时不再单独列出画像条目标题——此时记忆工具说明（tool_prompt）
    已经按 scope 列出了同一批条目（含 id + 正文摘要），重复注入纯属浪费 token。

    recent_text 用于查询扩展：用户只说「继续 / 这个」时，拼上一轮的话再检索。
    """
    if budget_tokens is None:
        budget_tokens = inject_budget()
    else:
        try:
            budget_tokens = max(1, int(budget_tokens))
        except (TypeError, ValueError):
            budget_tokens = inject_budget()
    if not recent_text:
        msgs = [m for m in (s.get("messages") or [])
                if m.get("role") == "user" and (m.get("content") or "").strip()]
        if len(msgs) >= 2:
            recent_text = str(msgs[-2].get("content") or "")[:500]
    parts, count = [], 0
    # 画像注入按会话开关（知识库面板可单独关掉某个会话），默认开：
    # 关掉后内容仍保留在本地，只是不再进提示词。
    _profile_on = (s or {}).get("use_profile") is not False
    pkb_id = get_profile_kb() if _profile_on else None
    if pkb_id:
        kb = load_kb(pkb_id)
        if kb and kb.get("nodes"):
            if not skip_profile:          # id / 标题已由 tool_prompt 列出 → 不重复注入
                parts.append("【用户画像】（长期偏好，供参考）")
                for n in kb["nodes"][:PROFILE_INJECT_LIMIT]:
                    parts.append(f"- （{n.get('category', '其他')}）{n.get('title', '')}")
                    count += 1

    items, edges_map, nodes_map = _collect_items(
        s, include_kbs=include_kbs, exclude_pinned=exclude_pinned,
        include_session=include_session)
    query = user_text or ""
    if len(set(tokenize(query, limit=24))) <= 2 and recent_text:
        query = (query + "\n" + recent_text).strip()
    chosen, _q, _used_model = _rank_items(items, query, allow_model=_llm_pick_enabled())
    neighbors = _expand_neighbors(chosen, edges_map, nodes_map)
    ranked = _merge_ranked(chosen, neighbors)

    lines, nlines, used = [], [], 0
    for score, it, rel, kind in ranked:
        line = (_inject_line(it["node"], relation=rel) if kind == "neighbor"
                else _inject_line(it["node"], score=score))
        t = estimate_tokens(line)
        if (lines or nlines) and used + t > budget_tokens:
            break
        (nlines if kind == "neighbor" else lines).append(line)
        used += t
        count += 1

    if lines:
        parts.append("【相关记忆】（来自知识库/会话记忆，供参考，不必刻意复述）")
        parts.extend(lines)
    if nlines:
        parts.append("【关联记忆】（与上面条目一跳相连；「关系：陈述」中的陈述就是两者的具体依据，可直接采用）")
        parts.extend(nlines)
    if not parts:
        return "", 0
    return "\n".join(parts), count


def recall_detail(s, user_text, recent_text="", include_kbs=True, include_session=True):
    """检索调试：返回本轮会注入的条目、分数与来源，不拼接文本。"""
    if not recent_text:
        msgs = [m for m in (s.get("messages") or [])
                if m.get("role") == "user" and (m.get("content") or "").strip()]
        if len(msgs) >= 2:
            recent_text = str(msgs[-2].get("content") or "")[:500]
    items, edges_map, nodes_map = _collect_items(
        s, include_kbs=include_kbs, include_session=include_session)
    query = user_text or ""
    if len(set(tokenize(query, limit=24))) <= 2 and recent_text:
        query = (query + "\n" + recent_text).strip()
    chosen, q, used_model = _rank_items(items, query)
    neighbors = _expand_neighbors(chosen, edges_map, nodes_map)
    out = []
    for score, it, rel, kind in _merge_ranked(chosen, neighbors):
        n = it["node"]
        out.append({"node_id": n.get("id"), "title": n.get("title") or "",
                    "category": n.get("category") or "其他", "scope": it["scope"],
                    "scope_key": it["scope_key"], "score": round(score, 2),
                    "kind": kind, "relation": rel})
    return {"query": query, "tokens": sorted(q), "items": out,
            "used_model": used_model, "candidates": len(items),
            "budget": inject_budget()}


# ------------------------------------------------------------
# 有界记忆索引（L3，对齐 Claude Code 的 MEMORY.md）
# ------------------------------------------------------------
_MEM_INDEX_CACHE = {}
_MEM_INDEX_CACHE_MAX = 32
_MEM_INDEX_LOCK = threading.Lock()


def _profile_kb_id():
    """只查索引、不创建：提示词装配不该顺手新建一个「用户画像」库。"""
    for item in (load_kb_index().get("kbs") or []):
        if item.get("name") == PROFILE_KB_NAME:
            return item.get("id") or ""
    return ""


def memory_index_block(s, include_profile=True, include_kbs=True, include_session=True,
                       max_lines=None, max_bytes=None):
    """长期记忆的常驻标题清单（CC MEMORY.md 的对齐版）。

    - 只列「作用域 · 分类 · 标题 — 一行钩子」，正文仍由检索 / memory.search 按需取；
    - 行数 / 字节双上限（默认 200 行 / 25KB），超限截断并标注；
    - 按节点 updated_at 倒序；同一 id 只出现一次；
    - 按会话缓存：记忆没变就直接复用，变了才重建（CC 是每会话只读一次，这里更及时一点）；
    - 本地轻量模型窗口太小，直接不注入。
    """
    cfg = load_config() or {}
    if cfg.get("memory_index_enabled", True) is False:
        return ""
    try:
        if light_chat_is_local(cfg):
            return ""
    except Exception:                     # noqa: BLE001  模式判断失败不影响索引
        pass
    if max_lines is None:
        try:
            max_lines = int(cfg.get("memory_index_max_lines", MEMORY_INDEX_LINES) or 0)
        except (TypeError, ValueError):
            max_lines = MEMORY_INDEX_LINES
    if max_bytes is None:
        try:
            max_bytes = int(cfg.get("memory_index_max_bytes", MEMORY_INDEX_BYTES) or 0)
        except (TypeError, ValueError):
            max_bytes = MEMORY_INDEX_BYTES

    pkb_id = ""
    if include_profile and (s or {}).get("use_profile") is not False:
        pkb_id = _profile_kb_id()
    scopes = []
    if pkb_id:
        kb = load_kb(pkb_id)
        if kb:
            scopes.append((pkb_id, kb.get("name") or PROFILE_KB_NAME, kb.get("nodes") or []))
    if include_kbs:
        for kb_id in ((s or {}).get("kb_ids") or []):
            kb_id = str(kb_id or "")
            if not kb_id or kb_id == pkb_id:
                continue
            kb = load_kb(kb_id)
            if kb:
                scopes.append((kb_id, kb.get("name") or "知识库", kb.get("nodes") or []))
    if include_session:
        scopes.append(("session", "会话记忆",
                       (((s or {}).get("memory") or {}).get("nodes") or [])))

    rows, seen = [], set()
    for key, name, nodes in scopes:
        for n in nodes:
            if not isinstance(n, dict):
                continue
            # 「历史摘要」节点已经由 summary 块单独注入，索引里再列一遍纯属重复。
            if key == "session" and str(n.get("category") or "") == "历史摘要":
                continue
            nid = str(n.get("id") or n.get("title") or "")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            rows.append((key, name, n))

    h = hashlib.sha1()
    h.update(("cfg|%s|%s\n" % (max_lines, max_bytes)).encode("utf-8", "ignore"))
    for key, _name, n in rows:
        h.update(("%s|%s|%s|%s|%s\n" % (
            key, n.get("id") or "", n.get("updated_at") or 0,
            n.get("title") or "", len(str(n.get("content") or "")))).encode(
                "utf-8", "ignore"))
    sig = h.hexdigest()
    sid = str((s or {}).get("id") or "")
    if sid:
        with _MEM_INDEX_LOCK:
            hit = _MEM_INDEX_CACHE.get(sid)
        if hit and hit[0] == sig:
            return hit[1]

    rows.sort(key=lambda x: float(x[2].get("updated_at")
                                  or x[2].get("created_at") or 0), reverse=True)
    total = len(rows)
    truncated = bool(max_lines and total > max_lines)
    picked = rows[:max_lines] if max_lines else rows
    lines = []
    for _key, name, n in picked:
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        cat = str(n.get("category") or "其他").strip() or "其他"
        hook = ""
        for ln in str(n.get("content") or "").splitlines():
            if ln.strip():
                hook = ln.strip()
                break
        if len(hook) > 80:
            hook = hook[:80] + "…"
        lines.append("- [%s·%s] %s%s" % (name, cat, title,
                                         (" — " + hook) if hook else ""))

    body = "\n".join(lines)
    if max_bytes and len(body.encode("utf-8")) > max_bytes:
        cut = body.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        body = cut.rsplit("\n", 1)[0]
        truncated = True
    if body:
        head = ("【记忆索引】（长期记忆标题清单，共 %d 条；正文请用 memory.search "
                "或检索按需取，不要逐条复述）" % total)
        if truncated:
            head += "（已按行数 / 字节上限截断）"
        out = head + "\n" + body
    else:
        out = ""
    if sid:
        with _MEM_INDEX_LOCK:
            if len(_MEM_INDEX_CACHE) >= _MEM_INDEX_CACHE_MAX:
                _MEM_INDEX_CACHE.pop(next(iter(_MEM_INDEX_CACHE)))
            _MEM_INDEX_CACHE[sid] = (sig, out)
    return out


# ------------------------------------------------------------
# 规则常驻注入（不靠检索）
# ------------------------------------------------------------
def profile_read_block(limit=30):
    """只读画像块：给 GAL 等「需要理解用户、但不该写入画像」的场景。"""
    kb = load_kb(get_profile_kb())
    nodes = (kb or {}).get("nodes") or []
    if not nodes:
        return ""
    lines = ["【用户画像（只读；供理解用户，不要修改）】"]
    for n in nodes[:max(1, int(limit or 30))]:
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        cat = str(n.get("category") or "其他")
        content = str(n.get("content") or "").replace("\n", " ")[:80]
        lines.append("- （%s）%s%s" % (cat, title, ("：" + content) if content else ""))
    return "\n".join(lines) if len(lines) > 1 else ""


def pinned_nodes(s, include_kbs=True):
    """收集需要「每轮常驻注入」的规则型节点（会话记忆 + 绑定的知识库）"""
    out, seen = [], set()
    srcs = [((s.get("memory") or {}).get("nodes") or [])]
    if include_kbs:
        for kb_id in (s.get("kb_ids") or []):
            kb = load_kb(kb_id)
            if kb:
                srcs.append(kb.get("nodes") or [])
    for nodes in srcs:
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if str(n.get("category") or "") not in PINNED_CATEGORIES:
                continue
            nid = n.get("id") or n.get("title")
            if nid in seen:
                continue
            seen.add(nid)
            out.append(n)
    return out


def pinned_block(s, include_kbs=True):
    """把常驻规则打包成注入文本；返回 (文本, 条数)"""
    nodes = pinned_nodes(s, include_kbs=include_kbs)
    if not nodes:
        return "", 0
    parts = ["【硬性规则｜世界设定，必须严格遵守，不得违背】",
             "以下是这个世界的规则与机制。**每一条都是硬约束**：不得违背、不得忽略、不得自行发明替代规则。"]
    for n in nodes:
        content = (n.get("content") or "").strip()
        parts.append("- " + (n.get("title") or "") + ("：" + content if content else ""))
    return "\n".join(parts), len(nodes)


def world_text(s, max_chars=9000):
    """把绑定的知识库整库拼成「世界设定」文本（供编剧阶段一次性喂给模型）。
    与 pinned_block 不同：这里追求**全**，用于大纲编写，不是每轮注入。"""
    pkb_id = get_profile_kb()
    parts, total = [], 0
    for kb_id in (s.get("kb_ids") or []):
        if kb_id == pkb_id:
            continue
        kb = load_kb(kb_id)
        if not kb:
            continue
        for n in (kb.get("nodes") or []):
            line = "- 【%s】%s：%s" % (n.get("category") or "其他", n.get("title") or "",
                                      (n.get("content") or "").strip())
            if total + len(line) > max_chars:
                return "\n".join(parts)
            parts.append(line)
            total += len(line)
    return "\n".join(parts)


def kb_stats(kb_ids):
    """估算「AI 读取这些知识库」要花多少输入 token（两个口径分开给）。

    - 全量：整库拼进提示词时的量（GAL 编剧走 world_text，另有 WORLD_MAX_CHARS 上限）
    - 每轮：日常对话是**检索式注入**，受 INJECT_MAX_TOKENS 约束；
      「规则 / 能力 / 力量」类在 GAL 里走 pinned_block 每轮常驻，不占检索预算。

    口径与 `context.estimate_tokens` 一致（中英分别折算），不是精确 tokenizer。
    """
    from app.context import estimate_tokens      # 延迟导入，避免循环依赖
    items = []
    for kid in (kb_ids or []):
        kid = str(kid or "").strip()
        if not kid:
            continue
        kb = load_kb(kid)
        if not kb:
            continue
        chars = tokens = 0
        pin_n = pin_chars = pin_tokens = 0
        for n in (kb.get("nodes") or []):
            text = ("%s\n%s" % (n.get("title") or "", n.get("content") or "")).strip()
            c = len(text)
            t = estimate_tokens(text)
            chars += c
            tokens += t
            if str(n.get("category") or "").strip() in PINNED_CATEGORIES:
                pin_n += 1
                pin_chars += c
                pin_tokens += t
        items.append({"id": kid, "name": kb.get("name") or kid,
                      "nodes": len(kb.get("nodes") or []),
                      "chars": chars, "tokens": tokens,
                      "pinned_nodes": pin_n, "pinned_chars": pin_chars,
                      "pinned_tokens": pin_tokens})
    keys = ("nodes", "chars", "tokens", "pinned_nodes", "pinned_chars", "pinned_tokens")
    total = {k: sum(i[k] for i in items) for k in keys}
    total["libraries"] = len(items)
    cfg = load_config() or {}
    try:
        idx_lines = int(cfg.get("memory_index_max_lines", MEMORY_INDEX_LINES) or 0)
    except (TypeError, ValueError):
        idx_lines = MEMORY_INDEX_LINES
    try:
        idx_bytes = int(cfg.get("memory_index_max_bytes", MEMORY_INDEX_BYTES) or 0)
    except (TypeError, ValueError):
        idx_bytes = MEMORY_INDEX_BYTES
    return {"items": items, "total": total,
            "inject_max_tokens": inject_budget(),
            "world_max_chars": WORLD_MAX_CHARS,
            "memory_index_enabled": cfg.get("memory_index_enabled", True) is not False,
            "memory_index_max_lines": idx_lines,
            "memory_index_max_bytes": idx_bytes}


def _norm_text(s):
    """去掉所有空白并转小写，用于宽松匹配"""
    return re.sub(r"\s+", "", (s or "")).lower()


def _alias_matches(node, t, nt=None):
    for a in (node.get("aliases") or []):
        sa = str(a or "").strip()
        if not sa:
            continue
        if sa == t:
            return True
        if nt is not None and _norm_text(sa) == nt:
            return True
    return False


def _find_node(nodes, nid=None, title=None, fuzzy=False):
    """按 id / 标题 / 别名定位节点。
    fuzzy=True 时逐级放宽：精确 → 归一化（去空白/大小写）→ 唯一子串；
    出现多个候选（有歧义）时直接放弃，避免连错 / 改错节点。"""
    if nid:
        for n in nodes:
            if n.get("id") == nid:
                return n
    if not title:
        return None
    t = str(title).strip()
    for n in nodes:
        if (n.get("title") or "").strip() == t or _alias_matches(n, t):
            return n
    if not fuzzy:
        return None
    nt = _norm_text(t)
    if len(nt) < 2:
        return None
    for n in nodes:                                   # 去空白/大小写后完全一致
        if _norm_text(n.get("title")) == nt or _alias_matches(n, "", nt):
            return n
    hit = [n for n in nodes if nt in _norm_text(n.get("title"))
           or any(nt in _norm_text(a) for a in (n.get("aliases") or []))]
    return hit[0] if len(hit) == 1 else None


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on", "单向", "有向", "directed", "forward")


def _edge_directed(op, rel):
    """边是单向还是双向：显式字段优先（directed / bidirectional），
    都没给时按关系是否天然对称判断（相关 / 同义 / 对立 -> 双向）。"""
    if "bidirectional" in op:
        return not _truthy(op.get("bidirectional"))
    if "directed" in op:
        return _truthy(op.get("directed"))
    return not is_symmetric(rel)


def _same_edge(edge, a_id, b_id, rel, directed):
    """已存在的边与新边是否重复；无向边「反着连」也算同一条，避免出现 A⇄B / B⇄A 两条重影"""
    if (edge.get("relation") or "related") != rel:
        return False
    ef, et = edge.get("from"), edge.get("to")
    if ef == a_id and et == b_id:
        return True
    if not directed and ef == b_id and et == a_id:
        return True
    return False


EDGE_FACT_MAX = 200


def normalize_edge_fact(raw):
    """连线的「为什么」：压成一行；空字符串表示没写。"""
    s = " ".join(str(raw or "").split())
    return s[:EDGE_FACT_MAX]


def edge_is_valid(edge):
    """边是否仍然有效：invalid_at 有值 = 已被新结论替代，留作历史，不参与检索与导出。"""
    return not (isinstance(edge, dict) and edge.get("invalid_at"))


def invalidate_edge(edge, at=None):
    """把边标记为失效（不删除，保留可追溯的历史）。返回是否真的改了。"""
    if not isinstance(edge, dict) or edge.get("invalid_at"):
        return False
    edge["invalid_at"] = float(at or time.time())
    return True


def _find_edge(edges, edge_id=None, a_id=None, b_id=None, rel=None):
    """按 id 优先、否则按端点 + 关系找一条**有效**的边。"""
    for e in edges or []:
        if not isinstance(e, dict) or not edge_is_valid(e):
            continue
        if edge_id and e.get("id") == edge_id:
            return e
    if not (a_id and b_id):
        return None
    directed = not is_symmetric(rel) if rel else None
    for e in edges or []:
        if not isinstance(e, dict) or not edge_is_valid(e):
            continue
        if rel and (e.get("relation") or "related") != rel:
            continue
        ef, et = e.get("from"), e.get("to")
        if ef == a_id and et == b_id:
            return e
        if directed is False and ef == b_id and et == a_id:
            return e
    return None


def anchor_changed(changed, session, msg_index):
    """给本轮写入的节点 / 边打上「原话锚点」source_msg（消息下标），供 UI 跳回出处。

    为什么分两步：apply_memory_ops 执行时 assistant 消息还没落库
    （续写会复用上一条、进度消息会被 pop），最终下标要到落库那一刻才知道。
    返回被打上锚点的条目数。
    """
    if not isinstance(changed, dict) or not isinstance(msg_index, int) or msg_index < 0:
        return 0
    kbs, n = {}, 0
    for scope, part in changed.items():
        if not isinstance(part, dict):
            continue
        ids = set()
        for kind in ("node_add", "node_update", "edge_add", "edge_update"):
            ids.update(str(x) for x in (part.get(kind) or []))
        if not ids:
            continue
        if scope == "session":
            mem = (session or {}).get("memory") or {}
            targets = list(mem.get("nodes") or []) + list(mem.get("edges") or [])
        else:
            kb = kbs.get(scope)
            if kb is None:
                kb = load_kb(scope)
                if not kb:
                    continue
                kbs[scope] = kb
            targets = list(kb.get("nodes") or []) + list(kb.get("edges") or [])
        for it in targets:
            if isinstance(it, dict) and str(it.get("id")) in ids:
                it["source_msg"] = msg_index
                n += 1
    for kb in kbs.values():
        save_kb(kb)
    return n


def apply_memory_ops(session_id, allowed_kb_ids, ops):
    """把 AI 产出的操作指令应用到会话记忆与允许的知识库。
    返回 (counts, problems)：counts 是各类成功计数；problems 是未生效的原因说明。"""
    problems = []
    sdata = load_sessions()
    s = find_session(sdata, session_id)
    if not s:
        return {}, ["会话不存在"], {}
    ensure_session_kb_fields(s)
    kb_cache, s_changed, touched = {}, False, set()
    counts = {}
    changed = {}          # scope_key -> {"node_add": set(), "node_update": set(), "edge_add": set()}

    def mark(scope_key, kind, item_id):
        """记录本 op 产生的变更 id，供前端在图谱上高亮「新内容 / 改过的内容」"""
        if not item_id:
            return
        changed.setdefault(scope_key, {}).setdefault(kind, set()).add(str(item_id))

    all_keys = ["session"] + [k for k in (allowed_kb_ids or []) if k != "session"]

    def scope_store(scope_key):
        if scope_key == "session":
            return s["memory"]["nodes"], s["memory"]["edges"]
        if scope_key not in all_keys:
            return None, None
        if scope_key not in kb_cache:
            kb_cache[scope_key] = load_kb(scope_key)
        kb = kb_cache.get(scope_key)
        return (kb["nodes"], kb["edges"]) if kb else (None, None)

    def locate(ref):
        """先在本 op 的作用域找端点，找不到再到其它可写作用域找；返回 (作用域, 节点)"""
        if not ref:
            return None, None
        ref = str(ref)
        for k in [key] + [x for x in all_keys if x != key]:
            nodes, _edges = scope_store(k)
            if not nodes:
                continue
            n = _find_node(nodes, ref, ref, fuzzy=True)
            if n:
                return k, n
        return None, None

    for op in ops:
        if not isinstance(op, dict):
            continue
        key = str(op.get("scope") or "session").strip()
        # 容错：模型有时会把提示词里的「scope=」前缀一起抄进字段值，
        # 例如 "scope=session"；这里剥掉前缀，否则会被误判为不可写作用域。
        low = key.lower()
        if low.startswith("scope="):
            key = key.split("=", 1)[1].strip()
        elif low.startswith("scope:"):
            key = key.split(":", 1)[1].strip()
        elif low.startswith("scope："):
            key = key.split("：", 1)[1].strip()
        key = key or "session"
        kind = (op.get("op") or "").lower()
        if key != "session" and key not in all_keys:
            problems.append("作用域 %s 不可写" % key)
            continue
        nodes, edges = scope_store(key)
        if nodes is None:
            problems.append("作用域 %s 不存在" % key)
            continue
        did = False
        try:
            if kind == "add":
                title = (op.get("title") or "").strip()
                if not title:
                    continue
                exist = _find_node(nodes, title=title, fuzzy=True)
                if exist and not exist.get("locked"):
                    if op.get("content"):
                        exist["content"] = op["content"]
                    exist["category"] = op.get("category") or exist.get("category", "其他")
                    if "aliases" in op:
                        merged = list(exist.get("aliases") or [])
                        merged += _clean_aliases(op.get("aliases"))
                        exist["aliases"] = _clean_aliases(merged)
                    _apply_appearance(exist, op)
                    exist["updated_at"] = time.time()
                    did = True
                    counts["update"] = counts.get("update", 0) + 1
                    mark(key, "node_update", exist.get("id"))
                elif not exist:
                    new_node = make_node(title, op.get("content") or "",
                                         op.get("category") or "其他",
                                         op.get("tags") or [], session_id,
                                         size=normalize_size(op.get("size")),
                                         color=normalize_color(op.get("color")),
                                         aliases=op.get("aliases"))
                    nodes.append(new_node)
                    did = True
                    counts["add"] = counts.get("add", 0) + 1
                    mark(key, "node_add", new_node.get("id"))
            elif kind == "update":
                n = _find_node(nodes, op.get("id"), op.get("title"), fuzzy=True)
                if n and not n.get("locked"):
                    for f in ("content", "title", "category", "tags"):
                        if op.get(f):
                            n[f] = op[f]
                    if "aliases" in op:           # 允许用空列表清空别名
                        n["aliases"] = _clean_aliases(op.get("aliases"))
                    _apply_appearance(n, op)
                    n["updated_at"] = time.time()
                    did = True
                    counts["update"] = counts.get("update", 0) + 1
                    mark(key, "node_update", n.get("id"))
                elif not n:
                    problems.append("找不到条目「%s」" % (op.get("title") or op.get("id") or ""))
            elif kind == "delete":
                n = _find_node(nodes, op.get("id"), op.get("title"), fuzzy=True)
                if n and not n.get("locked"):
                    nid = n["id"]
                    nodes.remove(n)
                    edges[:] = [e for e in edges if e.get("from") != nid and e.get("to") != nid]
                    did = True
                    counts["delete"] = counts.get("delete", 0) + 1
                elif not n:
                    problems.append("找不到条目「%s」" % (op.get("title") or op.get("id") or ""))
            elif kind == "append_note":
                n = _find_node(nodes, op.get("id"), op.get("title"), fuzzy=True)
                if n and not n.get("locked"):
                    note = append_node_note(n, op.get("content") or "", source="ai")
                    if note:
                        did = True
                        counts["append_note"] = counts.get("append_note", 0) + 1
                        mark(key, "node_update", n.get("id"))
                    else:
                        problems.append("追加内容不能为空")
                elif not n:
                    problems.append("找不到条目「%s」" % (op.get("title") or op.get("id") or ""))
            elif kind == "link":
                rel = normalize_relation(op.get("relation") or op.get("label"))
                directed = _edge_directed(op, rel)
                fact = normalize_edge_fact(op.get("fact") or op.get("why") or op.get("reason"))
                a_scope, a = locate(op.get("from"))
                b_scope, b = locate(op.get("to"))
                if not a or not b:
                    miss = [str(x) for x, n in ((op.get("from"), a), (op.get("to"), b)) if not n]
                    problems.append("找不到节点「%s」" % "」「".join(miss))
                    continue
                if a["id"] == b["id"]:
                    problems.append("两端是同一节点「%s」" % (a.get("title") or ""))
                    continue
                # 边写进 from 端点实际所在的作用域（端点可能在别的可写库里）
                tkey = a_scope
                _tn, tedges = scope_store(tkey)
                if tedges is None:
                    problems.append("作用域 %s 不存在" % tkey)
                    continue
                # S1：连线必须带一句话陈述（为什么成立 / 依据是什么），否则这条边没有信息量
                if not fact:
                    problems.append("连线必须用 fact 写一句话说明依据：%s → %s"
                                    % (a.get("title") or "", b.get("title") or ""))
                    continue
                # (from,to,relation) 唯一：同一对节点同一关系只保留一条有效边
                old = next((e for e in tedges
                            if _same_edge(e, a["id"], b["id"], rel, directed)), None)
                fact_updated = False
                if old is not None:
                    old_fact = normalize_edge_fact(old.get("fact"))
                    if old_fact == fact:
                        continue                      # 完全重复：幂等跳过
                    if old_fact:
                        invalidate_edge(old)          # S2：旧陈述被新陈述替代，标失效不删除
                        mark(tkey, "edge_update", old.get("id"))
                    else:                             # 老数据没写陈述 → 补上，不算替代
                        old["fact"] = fact
                        old.setdefault("valid_at", time.time())
                        fact_updated = True
                        counts["link"] = counts.get("link", 0) + 1
                        did = True
                        key = tkey
                        mark(tkey, "edge_update", old.get("id"))
                if not fact_updated:
                    label = (op.get("label") or "").strip() or relation_label(rel)
                    new_edge_id = _edge_id()
                    tedges.append({"id": new_edge_id, "from": a["id"], "to": b["id"],
                                   "label": label, "relation": rel, "directed": directed,
                                   "type": "relation", "fact": fact, "valid_at": time.time()})
                    counts["link"] = counts.get("link", 0) + 1
                    did = True
                    key = tkey
                    mark(tkey, "edge_add", new_edge_id)
            elif kind == "unlink":
                # S2：让一条已有关系失效（旧结论被推翻），保留在历史里可追溯
                raw_rel = op.get("relation") or op.get("label")
                rel = normalize_relation(raw_rel) if raw_rel else None
                eid = str(op.get("edge_id") or op.get("id") or "").strip()
                a_scope, a = locate(op.get("from")) if op.get("from") else (None, None)
                b_scope, b = locate(op.get("to")) if op.get("to") else (None, None)
                if not eid and not (a and b):
                    problems.append("unlink 需要 edge_id，或 from / to 两端")
                    continue
                tkey = a_scope or key
                _tn, tedges = scope_store(tkey)
                if tedges is None:
                    problems.append("作用域 %s 不存在" % tkey)
                    continue
                e = _find_edge(tedges, eid or None,
                               a.get("id") if a else None, b.get("id") if b else None, rel)
                if not e:
                    problems.append("找不到要失效的关系")
                    continue
                if invalidate_edge(e):
                    counts["unlink"] = counts.get("unlink", 0) + 1
                    did = True
                    key = tkey
                    mark(tkey, "edge_update", e.get("id"))
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
    changed_out = {k: {kk: sorted(v) for kk, v in vv.items() if v} for k, vv in changed.items()}
    return counts, problems, changed_out