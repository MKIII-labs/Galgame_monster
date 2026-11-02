# -*- coding: utf-8 -*-
"""知识库的「AI 读取成本」预估接口。

回答的是两个不同口径，界面上必须分开显示（否则很容易被理解成「每轮都要花这么多」）：
- 整库规模：库里到底有多少字 / 多少 token（不是单次成本）
- 日常每轮：对话是**检索式注入**，有 INJECT_MAX_TOKENS 上限，不会整库塞进去
- GAL 编剧：world_text 一次性全量，另有 WORLD_MAX_CHARS 上限
- 规则类（规则/能力/力量）：GAL 里每轮常驻注入，不受检索预算约束
"""
from app import knowledge, memory
from app.registry import route


@route("/api/kb/stats")
def _api_kb_stats(h):
    body = h._body()
    ids = body.get("ids")
    if not isinstance(ids, list):
        one = str(body.get("id") or "").strip()
        ids = [one] if one else []
    ids = [str(x) for x in ids if str(x).strip()]
    return h._json({"ok": True, **knowledge.kb_stats(ids)})


@route("/api/memory/estimate")
def _api_memory_estimate(h):
    """会话记忆 / 用户画像的注入量估算（只读索引表，不触发重建）。"""
    body = h._body()
    return h._json({"ok": True,
                    **memory.estimate(str(body.get("session_id") or ""))})
