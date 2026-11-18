# -*- coding: utf-8 -*-
"""会话存储压缩：清掉重复 / 二进制的附件正文。

背景：附件解析把同一篇正文同时写进 `docs[0].text`、`doc.text`、`doc_text`，
序列化时就是三份；更糟的是 MP4 等媒体被当文本解码出十几 MB 乱码。
2026-11 实测一个会话因此把 sessions.json 撑到 93MB。

这里只做「去重 + 截断 + 拒绝二进制」，不改消息顺序 / 正文 / 元信息。
"""
import os

from app.docparse import AUDIO_EXT, VIDEO_EXT

MEDIA_EXT = AUDIO_EXT | VIDEO_EXT
MAX_INLINE = 2_000_000        # 单份附件正文最多保留多少字
_BINARY_HEAD = 16384


def _binaryish(text):
    if not isinstance(text, str) or not text:
        return False
    head = text[:_BINARY_HEAD]
    bad = sum(1 for ch in head if ord(ch) < 9 or 13 < ord(ch) < 32)
    return ("\x00" in head) or bad / max(1, len(head)) > 0.05


def _slim_doc(doc):
    """doc 只保留元信息；返回释放的字符数。"""
    freed = 0
    if not isinstance(doc, dict):
        return freed
    text = doc.get("text")
    if isinstance(text, str) and text:
        freed += len(text)
    doc.pop("text", None)
    return freed


def compact_message(msg):
    """压缩一条消息。返回 (changed, freed_chars, notes)。"""
    if not isinstance(msg, dict):
        return False, 0, []
    changed = False
    freed = 0
    notes = []
    docs = msg.get("docs") if isinstance(msg.get("docs"), list) else []
    doc = msg.get("doc") if isinstance(msg.get("doc"), dict) else None
    doc_text = msg.get("doc_text")

    # 1) docs[] 里的正文：二进制直接去掉，超长截断
    for d in docs:
        if not isinstance(d, dict):
            continue
        text = d.get("text")
        if not isinstance(text, str) or not text:
            continue
        ext = os.path.splitext(str(d.get("name") or d.get("file") or ""))[1].lower()
        if ext in MEDIA_EXT or _binaryish(text):
            freed += len(text)
            d.pop("text", None)
            d["chars"] = 0
            d["warn"] = "二进制文件不支持内嵌正文，已保留文件引用"
            changed = True
            notes.append("移除二进制附件正文")
        elif len(text) > MAX_INLINE:
            freed += len(text) - MAX_INLINE
            d["text"] = text[:MAX_INLINE]
            d["chars"] = MAX_INLINE
            d["warn"] = ((str(d.get("warn") or "") + "；") if d.get("warn") else "") \
                + "正文过长，只保留前 %d 字" % MAX_INLINE
            changed = True
            notes.append("截断超长附件正文")

    # 2) doc 与 docs[0] 是同一份内容：只留元信息，不再存正文
    if doc is not None:
        t = doc.get("text")
        if isinstance(t, str) and t:
            freed += len(t)
            changed = True
            notes.append("移除 doc 重复正文")
        doc.pop("text", None)

    # 3) doc_text：docs[0].text 已存在时纯属重复；旧消息只有 doc_text 时才保留（截断）
    if isinstance(doc_text, str) and doc_text:
        canonical = docs[0].get("text") if docs and isinstance(docs[0], dict) else None
        if isinstance(canonical, str) and canonical:
            freed += len(doc_text)
            msg.pop("doc_text", None)
            changed = True
            notes.append("移除 doc_text 重复正文")
        elif _binaryish(doc_text):
            freed += len(doc_text)
            msg.pop("doc_text", None)
            changed = True
            notes.append("移除二进制 doc_text")
        elif len(doc_text) > MAX_INLINE:
            freed += len(doc_text) - MAX_INLINE
            msg["doc_text"] = doc_text[:MAX_INLINE]
            changed = True
            notes.append("截断超长 doc_text")

    return changed, freed, notes


def compact_data(data):
    """就地压缩整份 sessions 数据，返回统计。"""
    stats = {"sessions": 0, "messages": 0, "freed_chars": 0, "notes": []}
    for s in data.get("sessions") or []:
        hit = False
        for m in s.get("messages") or []:
            changed, freed, notes = compact_message(m)
            if changed:
                hit = True
                stats["messages"] += 1
                stats["freed_chars"] += freed
                stats["notes"].extend(notes)
        if hit:
            stats["sessions"] += 1
    # 只保留去重后的前 20 条提示，避免输出刷屏
    stats["notes"] = sorted(set(stats["notes"]))[:20]
    return stats
