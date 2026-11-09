# -*- coding: utf-8 -*-
"""前缀漂移观测（批 1：只观测，不修改任何消息）。

为什么需要：DeepSeek 的硬盘缓存只认**从第 0 个 token 起**的完整前缀；
system 里只要有一个字符变了，它后面的整段历史都会 miss。这里按消息粒度
记录「与上一次请求的公共前缀长度」和「首次分歧位置」，用来回答一个
以前只能靠猜的问题：缓存为什么没命中。

只做统计，不参与任何装配决策；任何异常都被吞掉，不影响对话。
"""

import hashlib
import threading
from collections import OrderedDict

from app.context import estimate_tokens

_MAX_SESSIONS = 64          # 只保留最近这么多个会话的指纹，避免内存无限增长
_LOCK = threading.Lock()
_LAST = OrderedDict()       # sid -> {"hashes": [...], "tokens": [...], "sys": str, "tools": str}


def _text_of(m):
    """把消息内容压成纯文本（兼容 vision 的 list content）。"""
    if not isinstance(m, dict):
        return str(m or "")
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif p.get("type") in ("image_url", "input_image"):
                    parts.append("[图片]")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return "" if c is None else str(c)


def _hash_msg(m):
    key = (_text_of(m).encode("utf-8", "ignore") +
           b"\x00" + str(m.get("role") or "").encode("utf-8", "ignore"))
    return hashlib.sha1(key).hexdigest()[:16]


def _hash_tools(tools):
    if not tools:
        return ""
    try:
        import json
        raw = json.dumps(tools, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(tools)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _system_of(messages):
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "system":
            return _hash_msg(m)
    return ""


def _remember(sid, entry):
    with _LOCK:
        _LAST[sid] = entry
        _LAST.move_to_end(sid)
        while len(_LAST) > _MAX_SESSIONS:
            _LAST.popitem(last=False)


def reset():
    """清空历史指纹（测试用）。"""
    with _LOCK:
        _LAST.clear()


def observe(sid, messages, tools=None):
    """计算本次请求相对「同会话上一次请求」的前缀复用情况。

    返回字段全部是只读统计；首次请求返回 cold=True。
    """
    try:
        messages = messages or []
        cur_hashes = [_hash_msg(m) for m in messages]
        cur_tokens = [estimate_tokens(_text_of(m)) for m in messages]
        sys_hash = _system_of(messages)
        tools_hash = _hash_tools(tools)

        with _LOCK:
            prev = _LAST.get(str(sid or ""))
            if prev is not None:
                _LAST.move_to_end(str(sid or ""))

        out = {
            "cold": prev is None,
            "prev_msgs": len(prev["hashes"]) if prev else 0,
            "cur_msgs": len(cur_hashes),
            "prefix_msgs": 0,
            "prefix_ratio": 0.0,
            "prefix_tokens": 0,
            "cur_tokens": sum(cur_tokens),
            "reuse_ratio": 0.0,
            "sys_changed": False,
            "tools_changed": False,
            "first_divergence": None,
        }
        if prev is not None:
            old = prev["hashes"]
            n = min(len(old), len(cur_hashes))
            i = 0
            while i < n and old[i] == cur_hashes[i]:
                i += 1
            prefix_tokens = sum(cur_tokens[:i])
            out.update({
                "prefix_msgs": i,
                "prefix_ratio": round(i * 1.0 / max(1, min(len(old), len(cur_hashes))), 3),
                "prefix_tokens": prefix_tokens,
                "reuse_ratio": round(prefix_tokens * 1.0 / max(1, sum(cur_tokens)), 3),
                "sys_changed": bool(sys_hash and prev.get("sys") and sys_hash != prev["sys"]),
                "tools_changed": bool(tools_hash and prev.get("tools")
                                      and tools_hash != prev["tools"]),
                "first_divergence": (i if i < n else None),
            })

        _remember(str(sid or ""), {"hashes": cur_hashes, "tokens": cur_tokens,
                                   "sys": sys_hash, "tools": tools_hash})
        return out
    except Exception:                    # noqa: BLE001  观测失败不能拦住请求
        return {"cold": True, "error": 1}
