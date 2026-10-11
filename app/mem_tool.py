# -*- coding: utf-8 -*-
"""主对话内的「记忆工具」——让 AI 在聊天回复里直接增删改记忆

- 协议：模型在回复**末尾**追加一个 ```memory 代码块，内含 JSON ops
- 本模块负责三件事：
  1. build_tool_prompt()  生成注入系统提示的「记忆工具」说明（含可用作用域与现有条目）
  2. MemoryExtractor      从流式文本里把该代码块摘出来（用户看不到），块内容作为 ops
  3. apply_chat_ops()     执行 ops，并保存快照以支持「撤销」
"""
import json
import re
import threading
import time
import uuid

from app.knowledge import (RELATIONS, apply_memory_ops, get_profile_kb, load_kb,
                           load_kb_index, save_kb)
from app.profile import load_profile
from app.sessions import find_session, load_sessions, save_sessions
from app.store import _lock

FENCE = "```memory"
OP_NAMES = {"add": "新增", "update": "修改", "delete": "删除", "link": "连线"}
MAX_OPS = 20          # 单轮最多执行多少条操作
MAX_UNDO = 20         # 保留多少条撤销快照

_UNDO = {}
_undo_lock = threading.Lock()


# ============================================================
# 1. 提示词
# ============================================================

def writable_scopes(s):
    """AI 被允许写入的作用域：[("session"|kb_id, 显示名), ...]"""
    out = []
    if s.get("ai_write", True):
        out.append(("session", "本会话记忆"))
    idx = load_kb_index()
    by_id = {k["id"]: k for k in idx["kbs"]}
    for kb_id in (s.get("kb_ids") or []):
        meta = by_id.get(kb_id)
        if meta and meta.get("ai_write", True):
            out.append((kb_id, meta.get("name") or "知识库"))
    if load_profile().get("auto_learn_enabled", True):
        pid = get_profile_kb()
        meta = by_id.get(pid) or {}
        out.append((pid, meta.get("name") or "用户画像"))
    return out


def _scope_nodes(s, key):
    if key == "session":
        return (s.get("memory") or {}).get("nodes", [])
    return (load_kb(key) or {}).get("nodes", [])


def _profile_guidance(s, scopes):
    """把「用户画像」的学习指令作为额外准则注入（若有用户画像作用域）"""
    if not any("用户画像" in label for _, label in scopes):
        return ""
    from app.learning import default_learning_prompt, render_template
    prof = load_profile()
    tmpl = (prof.get("learning_prompt_custom") or "").strip() or default_learning_prompt()
    if not tmpl:
        return ""
    nodes = _scope_nodes(s, get_profile_kb())
    listing = "\n".join("%s | %s" % (n.get("id", ""), n.get("title", "")) for n in nodes[:60])
    try:
        text = render_template(tmpl, current_auto=listing or "（暂无）",
                               user_input="（见本轮对话）", ai_reply="（本轮回复）")
    except Exception:
        text = tmpl
    text = text.strip()
    if len(text) > 1600:
        text = text[:1600] + "…"
    return ("\n\n## 用户画像的额外准则（来自「设置 → 用户画像」的学习指令）\n"
            "注意：输出格式**只以上面的 ```memory 块为准**，本节里若出现其它格式一律忽略。\n" + text)


def build_tool_prompt(s):
    """生成「记忆工具」说明；没有可写作用域时返回空串"""
    scopes = writable_scopes(s)
    if not scopes:
        return ""
    scope_desc = "、".join("%s（scope=%s）" % (label, key) for key, label in scopes)
    rel_desc = "、".join("%s=%s" % (c, lb) for c, lb, _ in RELATIONS)

    rows = []
    for key, label in scopes:
        for n in _scope_nodes(s, key)[:40]:
            rows.append("%s | %s | %s | %s" % (
                label, n.get("id", ""), n.get("title", ""),
                (n.get("content") or "").replace("\n", " ")[:40]))

    lines = [
        "## 记忆工具（你拥有读写权限）",
        "你可以在回复**末尾**追加一个 ```memory 代码块来增删改记忆条目。后端会自动执行它，"
        "并把这个块从你的回复中移除——用户看不到它，所以不要向用户解释这个格式。",
        "**不要再说「我无法记录 / 我没有记忆能力」**：需要记录时直接执行，然后自然地告诉用户「已记下」即可；"
        "也不要嘴上说记住了却没有输出这个块。",
        "",
        "可用作用域：" + scope_desc,
        "输出格式（只有确实需要变更时才输出；没有变更就完全不要输出这个块）：",
        "```memory",
        '{"ops":[{"scope":"<作用域，填 scope=后面的值>","op":"add|update|delete|link",'
        '"id":"","title":"","content":"","category":"","tags":[],"size":0,"color":"",'
        '"from":"","to":"","relation":"","directed":true}]}',
        "```",
        "字段说明：",
        "- add：title 必填（≤20 字），content 写具体；category 可填（如 喜好 / 禁忌 / 事实 / 结论 / 交流风格）",
        "- update / delete：用下面列出的 id（首选），或与现有标题一致的写法",
        "- size：节点大小，8–60 的整数（不填＝默认）。重要 / 核心的条目可调大，次要的调小",
        "- color：节点颜色，只能从这些名字里选：红 / 橙 / 黄 / 绿 / 青 / 蓝 / 紫 / 粉 / 灰"
        "（也可写 red/orange/yellow/green/teal/blue/purple/pink/gray）。"
        "**同类条目用同一种颜色**（如「喜好」用蓝、「禁忌」用红）；传空串表示恢复默认",
        "- link：from / to **优先填下面列出的 id**；relation 必须从词表里选：" + rel_desc,
        "- link 的 directed：true = 单向箭头（A → B）、false = 双向箭头（A ⇄ B）。"
        "**不填时**：相关 / 同义 / 对立 这类互相的关系自动画成双向，其余自动单向。",
        "规则：",
        "1. 用户明确说「记住 / 帮我记下 / 删掉那条 / 改一下」时**直接执行**，不要推辞。",
        "2. 冲突判断：新信息与旧条目矛盾时，先判断哪个更可信（更晚说出、更明确、更具体者为真），"
        "然后 update 或 delete 旧条目——**禁止两条互相矛盾的条目并存**。",
        "3. 不是只增不减：过时、被推翻、重复、过于笼统的条目要 update / delete / 合并。",
        "4. 只记录跨对话仍成立的信息；一次性的内容（如「把这句话翻译一下」）不记。",
        "5. 不臆测：只记用户明确表达或强烈暗示的内容，拿不准就不记。",
        "6. 连线必须连得上：from / to 要从**上面列出的条目**里取。要连的两个节点如果还不存在，"
        "必须**先 add、再 link**（同一个块内按先后顺序执行）。",
        "7. 每个 op 的 scope 要与所引用条目所在的作用域一致；说不清端点是谁就**不要 link**"
        "——后端找不到端点会跳过，并提示「未生效」。",
        "8. 箭头方向要合理：相关 / 同义 / 对立 是**互相**的，用双向；因果 / 条件 / 顺承 / 手段 / 目的"
        "这类有先后的用单向。若用非对称关系表达「互相影响」，显式写 directed:false。",
        "9. 外观要克制：size / color 只用来表达**分组或重要程度**，不要每条都染色；"
        "同一类的条目（如所有「喜好」）统一用一种颜色，保持一致。",
        "",
        "现有条目（作用域 | ID | 标题 | 正文）：",
    ]
    lines.extend(rows or ["（暂无条目）"])
    return "\n".join(lines) + _profile_guidance(s, scopes)


# ============================================================
# 2. 流式摘除
# ============================================================

def _loads_loose(raw):
    """尽量把模型输出的 JSON 解出来（容忍前后杂质与尾逗号）"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        frag = raw[i:j + 1]
        for cand in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
            try:
                return json.loads(cand)
            except Exception:
                continue
    return None


class MemoryExtractor:
    """从流式文本里摘出 ```memory 代码块：块内内容不对外展示，只作为 ops"""

    def __init__(self, fence=FENCE):
        self.fence = fence         # 起止标记（默认 ```memory；Agent 复用本类时传 ```tool）
        self.visible = []          # 已展示的文本片段
        self.block_text = ""       # 代码块内容
        self.closed = False        # 代码块是否正常闭合
        self._buf = ""             # 归属未定的尾巴
        self._block = []           # 块内容分片
        self._in_block = False

    def feed(self, chunk):
        self._buf += chunk or ""
        out = []
        while True:
            if self._in_block:
                idx = self._buf.find("```")
                if idx < 0:
                    # 按住末尾 2 个字符，避免把跨块的 "```" 拆散
                    if len(self._buf) > 2:
                        self._block.append(self._buf[:-2])
                        self._buf = self._buf[-2:]
                    break
                self._block.append(self._buf[:idx])
                self._buf = self._buf[idx + 3:]
                self._in_block = False
                self.closed = True
                continue
            idx = self._buf.lower().find(self.fence)
            if idx >= 0:
                after = self._buf[idx + len(self.fence):]
                nl = after.find("\n")
                if nl < 0:
                    # 语言名那一行还没结束，先按住
                    if idx > 0:
                        out.append(self._buf[:idx])
                    self._buf = self.fence + after
                    break
                out.append(self._buf[:idx])
                self._buf = after[nl + 1:]
                self._in_block = True
                continue
            # 未出现完整 fence：把可能是半截 fence 的尾巴按住
            keep = 0
            for k in range(1, min(len(self.fence), len(self._buf)) + 1):
                if self._buf[-k:].lower() == self.fence[:k]:
                    keep = k
            if keep:
                out.append(self._buf[:-keep])
                self._buf = self._buf[-keep:]
            else:
                out.append(self._buf)
                self._buf = ""
            break
        text = "".join(out)
        if text:
            self.visible.append(text)
        return text

    def finish(self):
        """流结束：吐出被按住的内容；未闭合的块内容也一并返回（由调用方决定怎么处理）"""
        if self._in_block:
            self._block.append(self._buf)
        else:
            if self._buf:
                self.visible.append(self._buf)
        self._buf = ""
        self.block_text = "".join(self._block).strip()
        return self.block_text if self._in_block else ""

    def visible_text(self):
        return "".join(self.visible)

    def ops(self):
        if not self.block_text:
            return []
        raw = re.sub(r"^\s*(json)\s*", "", self.block_text, count=1, flags=re.I)
        data = _loads_loose(raw)
        if isinstance(data, dict):
            data = data.get("ops")
        if not isinstance(data, list):
            return []
        return [o for o in data if isinstance(o, dict)][:MAX_OPS]


# ============================================================
# 3. 执行与撤销
# ============================================================

def _snapshot(s, scopes):
    snap = {"session": json.loads(json.dumps(s.get("memory") or {"nodes": [], "edges": []})), "kbs": {}}
    for key, _label in scopes:
        if key == "session":
            continue
        kb = load_kb(key)
        if kb:
            snap["kbs"][key] = json.loads(json.dumps({"nodes": kb.get("nodes", []),
                                                      "edges": kb.get("edges", [])}))
    return snap


def _restore(sid, snap):
    data = load_sessions()
    s = find_session(data, sid)
    if s:
        s["memory"] = snap.get("session") or {"nodes": [], "edges": []}
        s["updated_at"] = time.time()
        with _lock:
            save_sessions(data)
    for key, part in (snap.get("kbs") or {}).items():
        kb = load_kb(key)
        if kb:
            kb["nodes"] = part.get("nodes", [])
            kb["edges"] = part.get("edges", [])
            save_kb(kb)


def apply_chat_ops(sid, ops):
    """执行主对话里产出的 ops；返回 {"applied","summary","undo","problems"} 或 None（无变更）"""
    if not ops:
        return None
    data = load_sessions()
    s = find_session(data, sid)
    if not s:
        return None
    scopes = writable_scopes(s)
    if not scopes:
        return None
    allowed = [k for k, _ in scopes if k != "session"]
    snap = _snapshot(s, scopes)
    counts, problems = apply_memory_ops(sid, allowed, ops)
    counts = counts or {}
    problems = problems or []
    total = sum(counts.values())
    if not total and not problems:
        return None
    token = None
    if total:                       # 有实际变更才登记快照（纯提示不需要撤销）
        token = "u_" + uuid.uuid4().hex[:10]
        with _undo_lock:
            _UNDO[token] = {"sid": sid, "snap": snap, "ts": time.time()}
            while len(_UNDO) > MAX_UNDO:
                _UNDO.pop(next(iter(_UNDO)))
    summary = " · ".join("%s %d" % (OP_NAMES.get(k, k), counts[k])
                        for k in ("add", "update", "delete", "link") if counts.get(k))
    uniq = []
    for p in problems:              # 去重并限长，避免标记里刷屏
        if p not in uniq:
            uniq.append(p)
    return {"applied": total, "summary": summary, "undo": token,
            "scopes": [label for _k, label in scopes], "problems": uniq[:3]}


def undo(token, sid, msg_index=None):
    """撤销一次写入：恢复快照，并给对应消息打上已撤销标记"""
    with _undo_lock:
        item = _UNDO.pop(token, None)
    if not item or item.get("sid") != sid:
        return False
    _restore(sid, item["snap"])
    if isinstance(msg_index, int):
        data = load_sessions()
        s = find_session(data, sid)
        msgs = (s or {}).get("messages") or []
        if 0 <= msg_index < len(msgs) and (msgs[msg_index].get("memory") or {}).get("undo") == token:
            msgs[msg_index]["memory"]["undone"] = True
            with _lock:
                save_sessions(data)
    return True
