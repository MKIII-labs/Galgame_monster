# -*- coding: utf-8 -*-
"""分享：产出物（自包含 HTML / .gmshare 包 / 长图 PNG），而不是「托管链接」。

旧的 `data/shares/<id>.html` + `/share/<id>` 仍然保留：它只用于**本机预览**。
对外分发一律走 `export()` 产出的文件。
"""
import json
import os
import re
import time
import uuid

from app.store import DATA_DIR

from . import package as pkg

SHARE_DIR = os.path.join(DATA_DIR, "shares")
EXPORT_DIR = os.path.join(SHARE_DIR, "exports")
IMPORT_DIR = os.path.join(SHARE_DIR, "imported")

MAX_HTML = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,40}$")
_EXPORT_RE = re.compile(r"^sh_[a-f0-9]{8,16}\.(?:html|png|gmshare)$")
_TITLE_RE = re.compile(r"<title[^>]*>([\s\S]{0,200}?)</title>", re.I)

APP_VERSION = "3.3"


# ---------------- 旧版：本机预览页 ----------------

def save(html, title=""):
    """写入一个本机预览页；内容不像 HTML 时返回 None。"""
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


# ---------------- 新版：预览 / 导出 / 管理 ----------------

def _title_of(payload):
    t = str(payload.get("title") or "").strip()
    if t:
        return t[:120]
    q = str(payload.get("question") or "").strip()
    if q:
        return q.split("\n")[0][:80]
    return "分享"


def build_document(payload, redact=True):
    """返回 (html, report, sections, title)。不落盘，预览与导出共用同一份。"""
    from . import redact as redact_mod
    from . import render
    title = _title_of(payload)
    answer = render.sanitize(payload.get("answer_html") or "")
    rep = {"secrets": 0, "kv": 0, "paths": 0, "lan": 0, "samples": []}
    if redact:
        answer, rep = redact_mod.redact_html(answer)
    else:
        _, rep = redact_mod.redact_html(answer, mask=False)
    footer = "由 Galgame Monster 分享 · " + time.strftime("%Y-%m-%d")
    html, sections = render.build_html(
        title, payload.get("question") or "", answer, footer=footer)
    return html, rep, sections, title


def preview(payload):
    html, rep, sections, title = build_document(payload)
    return {"html": html, "report": rep, "sections": sections,
            "bytes": len(html.encode("utf-8")), "title": title}


def export(payload, fmt="html"):
    """生成产物文件，返回 {ok, name, path, size, kind, report}。"""
    fmt = str(fmt or "html").lower()
    html, rep, sections, title = build_document(payload)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    sid = "sh_" + uuid.uuid4().hex[:10]
    report = {
        "secrets": rep.get("secrets", 0) + rep.get("kv", 0),
        "paths": rep.get("paths", 0), "lan": rep.get("lan", 0),
        "samples": rep.get("samples", []),
    }
    if fmt == "package":
        data = pkg.build(
            {"title": title, "question": payload.get("question") or "",
             "answer_html": payload.get("answer_html") or "",
             "options": payload.get("options") or {},
             "sections": sections},
            html, title=title, app_version=APP_VERSION,
            redacted=report, options=payload.get("options") or {})
        name = sid + ".gmshare"
        with open(os.path.join(EXPORT_DIR, name), "wb") as f:
            f.write(data)
        size = len(data)
    elif fmt == "png":
        from . import image as image_mod
        tmp_html = os.path.join(EXPORT_DIR, sid + ".src.html")
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html)
        name = sid + ".png"
        out = os.path.join(EXPORT_DIR, name)
        size = image_mod.html_to_png(tmp_html, out)
        try:
            os.remove(tmp_html)
        except OSError:
            pass
        if not size:
            return {"ok": False, "error": "没有找到 Edge/Chrome，无法生成长图；可改用 HTML"}
    else:
        name = sid + ".html"
        size = len(html.encode("utf-8"))
        with open(os.path.join(EXPORT_DIR, name), "w", encoding="utf-8") as f:
            f.write(html)
    return {"ok": True, "name": name, "path": os.path.join(EXPORT_DIR, name),
            "size": int(size or 0), "kind": fmt, "report": report}


def _kind_of(name):
    if name.endswith(".png"):
        return "png"
    if name.endswith(".gmshare"):
        return "package"
    return "html"


def list_exports(limit=100):
    out = []
    try:
        names = [n for n in os.listdir(EXPORT_DIR) if _EXPORT_RE.match(n)]
    except OSError:
        return out
    for n in names:
        p = os.path.join(EXPORT_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        title = ""
        if n.endswith(".html"):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    m = _TITLE_RE.search(f.read(4096))
                title = (m.group(1).strip() if m else "")[:80]
            except OSError:
                title = ""
        out.append({"name": n, "kind": _kind_of(n), "size": st.st_size,
                    "mtime": int(st.st_mtime), "title": title})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def delete_export(name):
    n = str(name or "")
    if not _EXPORT_RE.match(n):
        return False
    try:
        os.remove(os.path.join(EXPORT_DIR, n))
        return True
    except OSError:
        return False


def import_package(data):
    """导入 .gmshare（只读预览）：返回 {ok, html, manifest}；错误抛 ValueError。"""
    item = pkg.read(data)
    os.makedirs(IMPORT_DIR, exist_ok=True)
    sid = "sh_" + uuid.uuid4().hex[:10]
    path = os.path.join(IMPORT_DIR, sid + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(item["preview"])
    meta = item.get("manifest") or {}
    return {"ok": True, "html": item["preview"], "manifest": meta,
            "name": sid + ".html", "path": path}
