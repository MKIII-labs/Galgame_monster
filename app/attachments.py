# -*- coding: utf-8 -*-
"""会话附件留存：把消息里的图片 / 文档统一成可寻址的附件（att_xxx）。

- 消息结构保持原样（images / image / docs / doc），附件 id 由落盘文件名派生，
  所以旧会话不需要迁移也能解析；
- 只用于「模型回看」：读文件时限定在当前会话的 uploads 目录内，
  不走沙箱路径解析，也不接受任意绝对路径。
"""
import os

from app.sessions import find_session, load_sessions
from app.uploads import _MIME_BY_EXT, _upload_path, att_id

IMAGE = "image"
DOC = "doc"

_NAME_MAX = 60          # 回看引用里文件名最多显示多少字符


def _entry(kind, file, name, mime, size, msg_index, text="", **extra):
    item = {
        "id": att_id(file),
        "kind": kind,
        "file": os.path.basename(str(file)),
        "name": str(name or os.path.basename(str(file))),
        "mime": str(mime or ""),
        "size": int(size or 0),
        "msg_index": int(msg_index or 0),
        "text": str(text or ""),
    }
    item.update(extra)
    return item


def from_message(msg, index=0):
    """一条 user 消息里登记过的附件（没有则返回空列表）。"""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return []
    out = []
    imgs = msg.get("images")
    if not isinstance(imgs, list) or not imgs:
        imgs = [msg.get("image")] if isinstance(msg.get("image"), dict) else []
    for im in imgs:
        if not isinstance(im, dict):
            continue
        f = im.get("file") or im.get("name")
        if not f:
            continue
        ext = os.path.splitext(str(f))[1].lower()
        out.append(_entry(IMAGE, f, im.get("name") or f,
                          im.get("mime") or _MIME_BY_EXT.get(ext, "image/png"),
                          im.get("size"), index))
    docs = msg.get("docs")
    if isinstance(docs, list) and docs:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            f = doc.get("file") or doc.get("name")
            if not f:
                continue
            out.append(_entry(DOC, f, doc.get("name") or f, doc.get("mime"),
                              doc.get("size"), index,
                              text=doc.get("text") or "",
                              pages=doc.get("pages") or [],
                              warn=doc.get("warn") or "", error=doc.get("error") or "",
                              chars=int(doc.get("chars") or 0)))
    else:
        # 旧会话只有单个 doc 字段
        doc = msg.get("doc")
        if isinstance(doc, dict) and (doc.get("file") or doc.get("name")):
            f = doc.get("file") or doc.get("name")
            out.append(_entry(DOC, f, doc.get("name") or f, doc.get("mime"),
                              doc.get("size"), index, text=msg.get("doc_text") or "",
                              pages=doc.get("pages") or [],
                              warn=doc.get("warn") or "", error=doc.get("error") or "",
                              chars=int(doc.get("chars") or 0)))
    return out


def list_for_session(sid):
    """按消息顺序列出本会话的全部附件。"""
    s = find_session(load_sessions(), sid)
    if not s:
        return []
    out = []
    for i, m in enumerate(s.get("messages") or []):
        out.extend(from_message(m, i))
    return out


def find(sid, key):
    """按 id / 落盘名 / 原文件名找附件（大小写不敏感；唯一后缀也算命中）。"""
    k = str(key or "").strip()
    if not k:
        return None
    items = list_for_session(sid)
    kl = k.lower()
    for it in items:
        if it["id"].lower() == kl:
            return it
    for it in items:
        if it["file"].lower() == kl or it["name"].lower() == kl:
            return it
    tail = [it for it in items
            if it["file"].lower().endswith(kl) or it["name"].lower().endswith(kl)]
    return tail[0] if len(tail) == 1 else None


def read_bytes(sid, item):
    """读附件原始字节；只认当前会话目录里的落盘名。"""
    name = item.get("file") if isinstance(item, dict) else item
    p = _upload_path(sid, name)
    if not p:
        return b""
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return b""


def ref_line(msg):
    """历史消息里的一行紧凑引用，告诉模型「这些附件可以回看」。

    只给 id / 类型 / 文件名，不占正文体积；模型真正需要内容时再调
    attachment_read。
    """
    items = from_message(msg)
    if not items:
        return ""
    parts = []
    for it in items:
        name = it["name"]
        if len(name) > _NAME_MAX:
            name = name[: _NAME_MAX - 1] + "…"
        parts.append("%s｜%s｜%s" % (it["id"], "图片" if it["kind"] == IMAGE else "文档", name))
    return "【可回看附件】" + "；".join(parts)
