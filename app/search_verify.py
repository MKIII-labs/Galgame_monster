# -*- coding: utf-8 -*-
"""引用语义校验（P2，可选）：小模型判断 claim 是否被引用证据支持。

只在设置里打开 `search_cite_nli` 时调用；默认关闭，避免每次带引用的回答
都多一次模型调用。解析失败 / 模型不可用时返回空列表（不阻断回答）。
"""
import json
import re

MAX_CLAIMS = 5
MAX_EVIDENCE = 800

_PROMPT = "\n".join([
    "你是引用核对器。给定若干条 claim 与它引用的来源原文片段，判断每条 claim 是否被原文支持。",
    "只输出 JSON 数组，不要解释，不要 Markdown 围栏：",
    '[{"i":1,"ok":true,"why":"原文直接写了..."},{"i":2,"ok":false,"why":"原文没有这个数字"}]',
    "判据：",
    "- 原文没有的数字 / 版本 / 价格 / 结论 → ok=false；",
    "- 原文是概括性表述、claim 没有歪曲 → ok=true；",
    "- 拿不准 → ok=false（宁可让模型改引，也不要放过无依据结论）。",
])


def _extract_json(text):
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    a, b = s.find("["), s.rfind("]")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(s[a:b + 1])
    except ValueError:
        return None


def parse_verdicts(text, n):
    """把模型输出解析成 [{i, ok, why}]；无法解析返回 []。"""
    arr = _extract_json(text)
    if not isinstance(arr, list):
        return []
    out = []
    for row in arr:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if i < 1 or i > n:
            continue
        ok = row.get("ok")
        if isinstance(ok, str):
            ok = ok.strip().lower() in ("true", "1", "yes", "ok", "支持")
        out.append({"i": i, "ok": bool(ok), "why": str(row.get("why") or "")[:200]})
    return out


def _default_chat(messages, max_tokens=600):
    try:
        from app.config import get_api_conf, load_config
        from app.llm import _quick_chat
        key, url, model = get_api_conf("chat", load_config())
        if not key or not url:
            return ""
        return _quick_chat(key, url, model, messages, max_tokens=max_tokens,
                           source="search-nli", timeout=(6, 20), tries=1,
                           use_cache=False, use_light=True)
    except Exception:                        # noqa: BLE001
        return ""


def verify(items, chat=None):
    """items: [{i, claim, evidence}]；返回 [{i, ok, why}]。失败返回 []。"""
    rows = []
    for it in (items or [])[:MAX_CLAIMS]:
        if not isinstance(it, dict) or not str(it.get("claim") or "").strip():
            continue
        rows.append({"i": len(rows) + 1,
                     "claim": str(it.get("claim") or "")[:400],
                     "evidence": str(it.get("evidence") or "")[:MAX_EVIDENCE]})
    if not rows:
        return []
    messages = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": json.dumps(rows, ensure_ascii=False)},
    ]
    try:
        raw = (chat or _default_chat)(messages, 600)
    except Exception:                        # noqa: BLE001
        return []
    return parse_verdicts(raw, len(rows))
