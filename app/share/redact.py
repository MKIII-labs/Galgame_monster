# -*- coding: utf-8 -*-
"""分享产物的脱敏：只在 HTML 的**文本片段**上替换，绝不碰标签与属性。

为什么不用正则直接扫整段 HTML：`<a href="...">` 里的 URL、`<img src>` 里的
base64 都含 `://` / 长串，整段替换会把页面结构改坏。这里按 `<...>` 切分，
只处理标签之外的文本。
"""
import re

# 高置信度密钥形态：命中即替换
_SECRET_RES = [
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
]
# 键值对形态：保留键名与分隔符，只打码值
_KV_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)"
    r"\s*[:=]\s*)([\"']?)([^\s\"'<>]{6,})\2")
# 绝对路径：只保留盘符 / 用户名占位
_WIN_USER_RE = re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s\"'<>]+")
_POSIX_USER_RE = re.compile(r"/(?:Users|home)/[^/\s\"'<>]+")
# 内网地址：只统计，不强制替换（替换会破坏「本机调试」这类正常内容）
_LAN_RE = re.compile(r"\b(?:127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?")

_TAG_SPLIT = re.compile(r"(<[^>]*>)")

SAMPLE_MAX = 3


def _new_report():
    return {"secrets": 0, "kv": 0, "paths": 0, "lan": 0, "samples": []}


def _note(rep, kind, text):
    if len(rep["samples"]) < SAMPLE_MAX * 2:
        s = str(text)
        rep["samples"].append({"kind": kind, "text": s[:120]})


def redact_text(text, rep, mask=True):
    """对一段纯文本做替换；mask=False 时只统计不替换。"""
    out = str(text)
    for rx in _SECRET_RES:
        def _sub(m):
            rep["secrets"] += 1
            _note(rep, "secret", m.group(0))
            return "***"
        out = rx.sub(_sub, out)

    def _kv(m):
        rep["kv"] += 1
        _note(rep, "secret", m.group(0))
        return m.group(1) + (m.group(2) or "") + "***" + (m.group(2) or "")
    out = _KV_RE.sub(_kv, out)

    def _path(m):
        rep["paths"] += 1
        _note(rep, "path", m.group(0))
        raw = m.group(0)
        return ("C:\\Users\\***" if raw[:1].isalpha() and ":" in raw[:3]
                else ("~/***" if raw.startswith("/") else raw))
    out = _WIN_USER_RE.sub(_path, out)
    out = _POSIX_USER_RE.sub(_path, out)

    def _lan(m):
        rep["lan"] += 1
        _note(rep, "lan", m.group(0))
        return m.group(0)          # 内网地址默认保留，只在报告里提示
    out = _LAN_RE.sub(_lan, out)
    return out if mask else text


def redact_html(html, mask=True):
    """返回 (处理后的 HTML, 报告)。报告里带命中样例，供前端逐条确认。"""
    rep = _new_report()
    parts = _TAG_SPLIT.split(str(html or ""))
    for i, seg in enumerate(parts):
        if not seg or seg.startswith("<"):
            continue
        parts[i] = redact_text(seg, rep, mask=mask)
    return "".join(parts), rep
