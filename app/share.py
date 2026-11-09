# -*- coding: utf-8 -*-
"""分享页：把一条问答存成独立 HTML，由 /share/<id>.html 直接打开。

前端把「已经渲染好的消息 HTML」连同精简样式一起发过来 —— 这样公式、代码块、
图片与界面上完全一致，后端不需要再实现一套 Markdown 渲染。
本模块只负责落盘、生成随机 id、按 id 读回；不写 sessions.json。
"""
import os
import re
import time
import uuid

from app.store import DATA_DIR

SHARE_DIR = os.path.join(DATA_DIR, "shares")
MAX_HTML = 400000            # 单页上限（字符）：超过基本是误传
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


def save(html, title=""):
    """写入一个分享页；内容不像 HTML 时返回 None。"""
    s = str(html or "")
    if not s.strip() or len(s) > MAX_HTML:
        return None
    low = s[:2000].lower()
    if "<html" not in low or "<body" not in low:
        return None
    os.makedirs(SHARE_DIR, exist_ok=True)
    sid = "sh_" + uuid.uuid4().hex[:12]
    path = os.path.join(SHARE_DIR, sid + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    return {"id": sid, "path": path, "title": str(title or "")[:80],
            "created_at": time.time()}


def read(name):
    """按 id（可带 .html）读回分享页字节；非法 id / 不存在返回 None。"""
    base = str(name or "")
    if base.endswith(".html"):
        base = base[:-5]
    if not _ID_RE.match(base):
        return None
    path = os.path.join(SHARE_DIR, base + ".html")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None
