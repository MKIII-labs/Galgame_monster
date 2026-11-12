# -*- coding: utf-8 -*-
"""正文 → passage 级证据（P0）。

网页正文前几千字常常是导航 / 版权 / 推荐位。这里按标题与空行切块，
按查询词命中数挑段落，保留原文行号，供提示词标注「第 x–y 行」，
也为后续引用数字校验提供逐字引句。
"""
import re

from app.search_rank import query_terms

_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_SENT_RE = re.compile(r"(?<=[。！？!?；;])")
_MAX_BLOCK = 700


def _split_oversize(text, line_no):
    """超长段落按句子切成 ~500 字的小块；行号沿用原行（页面通常一个段落一行）。"""
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    out, buf = [], ""
    for p in parts:
        if buf and len(buf) + len(p) > 500:
            out.append(buf)
            buf = p
        else:
            buf = (buf + " " + p).strip() if buf else p
    if buf:
        out.append(buf)
    return [{"text": x, "line_start": line_no, "line_end": line_no} for x in out]


def _blocks(text):
    """按标题 / 空行切块，返回 [{text, line_start, line_end}]。"""
    lines = str(text or "").splitlines()
    blocks, cur, start = [], [], 0

    def flush():
        nonlocal cur, start
        if not cur:
            return
        body = "\n".join(cur).strip()
        if body:
            if len(body) > _MAX_BLOCK:
                blocks.extend(_split_oversize(body, start))
            else:
                blocks.append({"text": body, "line_start": start,
                               "line_end": start + len(cur) - 1})
        cur, start = [], 0

    for i, line in enumerate(lines, 1):
        if not line.strip():
            flush()
            continue
        if _HEAD_RE.match(line) and cur:
            flush()
        if not cur:
            start = i
        cur.append(line)
    flush()
    # 不合并短块：导航块本身没有查询词，排序时会自然沉底；
    # 合并反而会把「标题 + 正文」的行号糊在一起。
    return blocks


def _score(block, terms):
    text = block["text"].lower()
    if not terms:
        return 1.0
    hit = sum(1 for t in terms if t and t in text)
    # 命中数为主，块长度做轻微加权（太短的块信息量不足）
    return hit * 10.0 + min(len(text), 900) / 900.0


def extract_evidence(text, query, max_chars=2500, max_passages=3):
    """返回 (content, evidence)；content 供提示词用，evidence 带逐字引句与行号。"""
    raw = str(text or "")
    if not raw.strip():
        return "", []
    try:
        max_chars = max(400, int(max_chars or 2500))
    except (TypeError, ValueError):
        max_chars = 2500
    try:
        max_passages = max(1, min(int(max_passages or 3), 6))
    except (TypeError, ValueError):
        max_passages = 3

    terms = query_terms(query)
    blocks = _blocks(raw)
    scored = sorted(blocks, key=lambda b: (-_score(b, terms), b["line_start"]))
    picked, used = [], 0
    for b in scored:
        if picked and used + len(b["text"]) + 2 > max_chars:
            continue
        picked.append(b)
        used += len(b["text"]) + 2
        if len(picked) >= max_passages:
            break
    if not picked:
        content = raw[:max_chars]
        return content, [{"quote": content[:600], "line_start": 1, "line_end": 1, "score": 0}]
    picked.sort(key=lambda b: b["line_start"])
    content = "\n\n".join(b["text"] for b in picked)[:max_chars]
    evidence = [{
        "quote": b["text"][:600],
        "line_start": b["line_start"],
        "line_end": b["line_end"],
        "score": round(_score(b, terms), 2),
    } for b in picked[:4]]
    return content, evidence
