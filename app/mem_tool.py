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

from app.config import load_config
from app.knowledge import (MAX_KB_PROMPT, RELATIONS, apply_memory_ops, edge_is_valid,
                           get_profile_kb, load_kb, load_kb_index, save_kb)
from app.profile import load_profile
from app.sessions import find_session, load_sessions, save_sessions
from app.store import _lock

FENCE = "```memory"
# DeepSeek 原生工具调用标记（U+FF5C 全角竖线）：模型有时会吐这个而不是 ```tool 围栏，
# 不认它就会「整块标记直接显示给用户 + 工具不执行 + 本轮静默结束」，表现为「输出中断」。
DSML_OPEN = "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
DSML_CLOSE = "</\uff5c\uff5cDSML\uff5c\uff5c>"
OP_NAMES = {"add": "新增", "update": "修改", "delete": "删除",
            "link": "连线", "unlink": "断开", "append_note": "追加"}
MAX_OPS = 20          # 单轮最多执行多少条操作
MAX_UNDO = 20         # 保留多少条撤销快照

_UNDO = {}
_undo_lock = threading.Lock()


# ============================================================
# 1. 提示词
# ============================================================

def writable_scopes(s, include_profile=True, include_kb=True, include_session=True):
    """AI 被允许写入的作用域：[("session"|kb_id, 显示名), ...]

    include_profile / include_kb 供 GAL 剧场使用：剧场默认只能写本会话记忆，
    避免把演出内容写进用户画像或知识库。
    include_session=False 时连本会话记忆也不列（用于会话级「读取记忆」关闭）。
    """
    out = []
    if include_session and s.get("ai_write", True):
        out.append(("session", "本会话记忆"))
    idx = load_kb_index()
    by_id = {k["id"]: k for k in idx["kbs"]}
    if include_kb:
        for kb_id in (s.get("kb_ids") or []):
            meta = by_id.get(kb_id)
            if meta and meta.get("ai_write", True):
                out.append((kb_id, meta.get("name") or "知识库"))
    if include_profile and load_profile().get("auto_learn_enabled", True):
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
    text += ("\n注意：画像是长期事实，不是任务工作区；一次性的任务进展写本会话记忆，"
             "不要写进画像。")
    return ("\n\n## 用户画像的额外准则（来自「设置 → 用户画像」的学习指令）\n"
            "注意：输出格式**只以上面的 ```memory 块为准**，本节里若出现其它格式一律忽略。\n" + text)


def _kb_guidance(scopes):
    """每个可写知识库的自定义写入规则（叠加在通用记忆规则 / 学习指令之上）。"""
    blocks = []
    for key, label in scopes:
        if key == "session":
            continue
        kb = load_kb(key) or {}
        rule = str(kb.get("prompt") or "").strip()
        if not rule:
            continue
        rule = rule[:MAX_KB_PROMPT]
        blocks.append(
            "## 知识库写入规则：%s（ID：%s）\n"
            "往这个库写内容时，除上面的通用规则外还必须遵守：\n%s"
            % (label, key, rule))
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""


def _guidance_lists_profile(s, scopes):
    """画像准则模板是否会用 {current_auto} 把画像条目列出来。

    列了就不在「现有条目」里再列一遍——同一批标题每轮要付两次 token。
    判不出来时返回 False（保守：保留原来的重复，不丢信息）。
    """
    if not any("用户画像" in label for _, label in scopes):
        return False
    try:
        from app.learning import default_learning_prompt
        prof = load_profile()
        tmpl = (prof.get("learning_prompt_custom") or "").strip() or default_learning_prompt()
    except Exception:
        return False
    return "{current_auto}" in (tmpl or "")


def build_tool_prompt(s, include_profile=True, include_kb=True, include_session=True):
    """生成「记忆工具」说明；没有可写作用域时返回空串"""
    scopes = writable_scopes(s, include_profile=include_profile, include_kb=include_kb,
                             include_session=include_session)
    if not scopes:
        return ""
    cfg = load_config() or {}
    # 0 是合法值（= 不设软上限 / 用默认列表长度），不能用 `or` 兜底吃掉
    try:
        _soft_raw = cfg.get("memory_soft_limit")
        soft_limit = 60 if _soft_raw is None or _soft_raw == "" else int(_soft_raw)
    except (TypeError, ValueError):
        soft_limit = 60
    try:
        _recent_raw = cfg.get("memory_list_recent")
        list_recent = 40 if _recent_raw is None or _recent_raw == "" else int(_recent_raw)
    except (TypeError, ValueError):
        list_recent = 40
    list_recent = max(12, min(80, list_recent))
    session_nodes = (s.get("memory") or {}).get("nodes") or []
    # 只算有效关系：失效边留作历史，不该让模型以为「关系已经够了」
    session_edges = [e for e in ((s.get("memory") or {}).get("edges") or [])
                     if edge_is_valid(e)]
    over_limit = soft_limit > 0 and len(session_nodes) >= soft_limit
    # 超限时多列一些，模型才能看到同主题的旧条目并合并；平时仍只列 12 条省 token。
    recent_n = list_recent if over_limit else 12
    scope_desc = "、".join("%s（ID：%s）" % (label, key) for key, label in scopes)
    rel_desc = "、".join("%s=%s" % (c, lb) for c, lb, _ in RELATIONS)

    # 画像条目已由「额外准则」列全时，不再在这里重复列一遍
    skip_profile_rows = _guidance_lists_profile(s, scopes)
    profile_kb = get_profile_kb()
    rows = []
    for key, label in scopes:
        if skip_profile_rows and key == profile_kb:
            continue
        # 只列 id + 标题：正文由记忆检索按需注入，这里列全文纯属每轮重复付费。
        # 取「最近更新的 N 条」而不是数组前 N 条：节点是 append 追加的，取前 N 条
        # 等于永远只给模型看最旧的，它看不见自己刚写的 → 下一轮又 add 一条同主题的，
        # 同一个话题能堆出 5 个版本（见 NOTE 43.87）。
        # N 平时是 12；超过 memory_soft_limit 时放宽到 memory_list_recent，方便合并。
        recent = sorted(_scope_nodes(s, key),
                        key=lambda n: (n.get("updated_at") or n.get("created_at") or 0),
                        reverse=True)[:recent_n]
        for n in recent:
            rows.append("%s | %s | %s" % (label, n.get("id", ""), n.get("title", "")))

    lines = [
        "## 记忆工具（你拥有读写权限）",
        "你可以在回复**末尾**追加一个 ```memory 代码块来增删改记忆条目。后端会自动执行它，"
        "并把这个块从你的回复中移除——用户看不到它，所以不要向用户解释这个格式。",
        "**不要再说「我无法记录 / 我没有记忆能力」**：需要记录时直接执行，然后自然地告诉用户「已记下」；"
        "也不要嘴上说记住了却没有输出这个块。",
        "",
        "## 作用域选择（先读这一节）",
        "- **默认写本会话记忆**（session）：任务目标与进展、已确认的决定、文件路径 / 参数 / 结论、"
        "未完成事项、用户纠正过的点。即使只对本会话成立，也必须写 session。",
        "- **用户画像**只放跨会话稳定成立的偏好 / 身份 / 长期目标；不要把任务进展写进去。**本会话记忆不受此限**。",
        "- **知识库**只在内容明确属于该库主题时写；拿不准就先写本会话记忆，需要长期保留再同步。",
        "- 对话历史随时可能被压缩。后面还要用到的信息，现在就写进本会话记忆，不要依赖聊天记录。",
        "",
        "可用作用域：" + scope_desc,
        "本会话记忆现有 %d 条、%d 条关系。" % (len(session_nodes), len(session_edges)),
        "输出格式（只有确实需要变更时才输出；没有变更就完全不要输出这个块）：",
        "```memory",
        '{"ops":[{"scope":"<作用域ID>","op":"add|update|delete|link|unlink|append_note",'
        '"id":"","title":"","content":"","category":"","tags":[],"aliases":[],"size":0,"color":"",'
        '"from":"","to":"","relation":"","fact":"","directed":true}]}',
        "```",
        "字段说明：",
        "- scope：只填作用域 ID（如 `session` 或 `kb:xxx`），**不要带 `scope=` 前缀**",
        "- add：title 必填（≤20 字），content 写具体；category 可选（喜好 / 禁忌 / 事实 / 结论 / 交流风格）",
        "- update / delete：用下面列出的 id（首选），或与现有标题一致的写法",
        "- append_note：给已有条目追加一条时间线补充，**保留原文与历史**；用 id / title 定位，content 必填",
        "- aliases：可选，别名 / 简称（≤8 个），提升检索命中；不要拿它代替 title",
        "- size：8–60 的整数；color：红 / 橙 / 黄 / 绿 / 青 / 蓝 / 紫 / 粉 / 灰。"
        "两者只表达分组与重要程度，同类条目用同一种颜色，不要每条都染色",
        "- link：from / to 从下面列出的条目里取，relation 从词表选：" + rel_desc +
        "；directed 不填时，相关 / 同义 / 对立 自动双向，其余单向。**fact 必填**："
        "用一句话写清这条关系为什么成立（依据 / 具体表现），只写「有关系」等于没连——"
        "fact 会被随关系一起注入后续对话，是这条边唯一有用的部分；"
        "同一对节点同一关系只保留一条有效边，陈述变了会自动替代旧边（旧边标失效、不删除），"
        "所以**不需要**先 unlink 再 link 同一对节点；",
        "- unlink：让一条已有关系失效（旧结论被推翻 / 不再成立），用 edge_id 或 from+to 指定；"
        "边不会被删除，只是不再参与检索——需要时再 link 一条新的",
        "规则：",
        "1. 用户明确说「记住 / 帮我记下 / 删掉那条 / 改一下」时**直接执行**，不要推辞。",
        "2. 冲突判断：新信息与旧条目矛盾时，以更晚、更明确、更具体者为真，"
        "然后 update 或 delete 旧条目——**禁止两条互相矛盾的条目并存**。",
        "3. 不是只增不减：过时、被推翻、重复、过于笼统的条目要 update / delete / 合并。"
        "同一主题有多条时，保留信息最全的一条，用 update 把其余内容合并进去（别名也并进来），"
        "再 delete 其余条目；**不要靠换一个标题 add 一条新的来绕过合并**。",
        "4. 不臆测：只记用户明确表达或强烈暗示的内容，拿不准就不记。",
        "5. **追加要写增量**：同一件事有新进展时优先 append_note，正文写清「什么时候 / 发生了什么 / 结果如何」，"
        "不要写「同上」「已更新」这种没有信息量的补充；旧结论被推翻或原文写错时才用 update，"
        "**不要用 add 覆盖同名条目来假装追加**。",
        "6. **事件必须连关系**：本会话记忆里的「事件 / 进展 / 结论 / 卡点」类条目，"
        "新增或更新时，只要能在已有条目里找到相关目标、前置步骤、原因或结果，"
        "就必须输出至少一条 link（优先 purpose=目的 / cause=因果 / condition=条件 / means=手段），"
        "并写清 fact；旧结论被推翻时：同一对节点同一关系直接 link 新陈述（自动替代），"
        "关系本身不再成立时用 unlink。"
        "确实找不到相关条目才跳过，不要因为「关系不明确」就一条都不连。"
        "from / to 必须已存在；要连的节点若还没有，先 add 再 link（同一块内按顺序执行）。",
    ]
    if over_limit:
        lines += [
            "",
            "## 本会话记忆需要整理（%d 条 / %d 条关系）" % (len(session_nodes),
                                                              len(session_edges)),
            "- 本轮新增前先整理：合并同主题重复（保留信息最全的一条，update 合并正文/别名，其余 delete）；",
            "- 删除过时、被推翻、过于笼统的条目；",
            "- 给关键事件补关系：新进展至少连一条 purpose / cause / condition / means，并写清 fact；",
            "- 整理目标：≤ %d 条。" % soft_limit,
        ]
    extra = (_profile_guidance(s, scopes) + _kb_guidance(scopes)).strip()
    if extra:
        lines += ["", extra]
    lines += ["", "现有条目（作用域 | ID | 标题；每个作用域最多列最近更新的 %d 条，"
                  "正文按需检索，不在这里列）：" % recent_n]
    lines.extend(rows or ["（暂无条目）"])
    return "\n".join(lines)


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


def _fence_at_line_start(buf, i, prev_char="\n"):
    """标记是否位于行首（前面只允许空白）—— Markdown 围栏语义。
    正文里提到的 ```memory / ```tool（如向用户讲解协议时）前面有别的文字，
    不算控制块起点；不判断行首会把后半段回答当控制块吞掉。
    prev_char：buf 起点前一个「已输出」字符（流开头视为换行）—— 增量切分后 buf 开头
    不一定真是行首，必须用它补上下文，否则被按住的半截标记会「看起来像行首」。"""
    seg = buf[:i]
    nl = seg.rfind("\n")
    if nl >= 0:
        # 只看「当前行」的缩进：上一行有正文并不影响本行行首
        return not seg[nl + 1:].strip()
    return (not seg.strip()) and prev_char == "\n"


class MemoryExtractor:
    """从流式文本里摘出 ```memory 代码块：块内内容不对外展示，只作为 ops"""

    def __init__(self, fence=FENCE, openers=None):
        # 起止候选：默认只用 `fence`；Agent 复用本类时会额外认 DeepSeek 原生工具标记
        self.openers = list(openers) if openers else [(fence, "```")]
        self.visible = []          # 已展示的文本片段
        self.block_text = ""       # 控制块内容
        self.closed = False        # 控制块是否正常闭合
        self._prev_char = "\n"     # 已输出文本的末字符（初始视为行首）
        self._buf = ""             # 归属未定的尾巴
        self._block = []           # 块内容分片
        self._in_block = False
        self._closer = "```"       # 当前块用哪个收尾标记

    def feed(self, chunk):
        self._buf += chunk or ""
        out = []

        def emit(s):
            if not s:
                return
            out.append(s)
            self._prev_char = s[-1]      # 记住已输出文本的末字符，供「行首」判定

        while True:
            if self._in_block:
                idx = self._buf.find(self._closer)
                if idx < 0:
                    # 按住末尾 len(closer)-1 个字符，避免把跨块的收尾标记拆散
                    keep = len(self._closer) - 1
                    if len(self._buf) > keep:
                        self._block.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    break
                self._block.append(self._buf[:idx])
                self._buf = self._buf[idx + len(self._closer):]
                self._in_block = False
                self.closed = True
                continue
            hit = None                              # (位置, 起始标记, 收尾标记)
            low = self._buf.lower()
            for op, cl in self.openers:
                # ``` 围栏必须在行首；否则是正文里的字面量，跳过它继续找。
                # DSML 这类原生标记不做行首约束，避免漏检（回归）。
                line_only = op.startswith("```")
                i = low.find(op.lower())
                while i >= 0 and line_only and not _fence_at_line_start(self._buf, i, self._prev_char):
                    i = low.find(op.lower(), i + 1)
                if i >= 0 and (hit is None or i < hit[0]):
                    hit = (i, op, cl)
            if hit:
                i, op, cl = hit
                after = self._buf[i + len(op):]
                nl = after.find("\n")
                if nl < 0:
                    # 语言名那一行还没结束，先按住
                    if i > 0:
                        emit(self._buf[:i])
                    self._buf = self._buf[i:]
                    break
                emit(self._buf[:i])
                self._buf = after[nl + 1:]
                self._in_block = True
                self._closer = cl
                continue
            # 未出现完整起止标记：把可能是半截标记的尾巴按住
            keep = 0
            for op, _cl in self.openers:
                for k in range(1, min(len(op), len(self._buf)) + 1):
                    if self._buf[-k:].lower() == op[:k].lower():
                        keep = max(keep, k)
            if keep:
                emit(self._buf[:-keep])
                self._buf = self._buf[-keep:]
            else:
                emit(self._buf)
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
    # 剧场会话只允许写本会话记忆：即使模型误写了画像/知识库作用域，这里也拒绝
    if (s.get("gal_scope") or "main") == "gal":
        scopes = writable_scopes(s, include_profile=False, include_kb=False)
    else:
        scopes = writable_scopes(s)
    if not scopes:
        return None
    allowed = [k for k, _ in scopes if k != "session"]
    snap = _snapshot(s, scopes)
    counts, problems, changed = apply_memory_ops(sid, allowed, ops)
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
                        for k in ("add", "update", "delete", "link", "unlink", "append_note")
                        if counts.get(k))
    uniq = []
    for p in problems:              # 去重并限长，避免标记里刷屏
        if p not in uniq:
            uniq.append(p)
    return {"applied": total, "summary": summary, "undo": token,
            "scopes": [label for _k, label in scopes], "problems": uniq[:3],
            "changed": changed or {}}


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
