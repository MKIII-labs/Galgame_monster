# -*- coding: utf-8 -*-
"""统一批注 API：PDF / 文本 / 代码 / 图片共用。

阅读器与笔记库原来的批注端点保留（内部已改为调用同一张表），这里提供给
编辑器、文献视图与导出使用。
"""
import os

from app import anno
from app.registry import route


def _guard(h):
    body = h._body()
    s, sb = h._sb(body)
    if not s:
        return body, None, h._json({"ok": False, "error": "会话不存在"}, 404)
    return body, sb, None


@route("/api/anno/list")
def _api_anno_list(h):
    body, sb, err = _guard(h)
    if err:
        return err
    items = anno.list_annotations(
        body.get("path"), q=body.get("q"), kind=body.get("kind"), tag=body.get("tag"))
    out = {"ok": True, "items": items}
    if body.get("path"):
        out["counts"] = anno.counts(body.get("path"))
    return h._json(out)


@route("/api/anno/save")
def _api_anno_save(h):
    body, sb, err = _guard(h)
    if err:
        return err
    info = dict(body)
    info.setdefault("doc_key", body.get("path"))
    aid = anno.save_annotation(info)
    if not aid:
        return h._json({"ok": False, "error": "批注内容不能为空"}, 400)
    return h._json({"ok": True, "id": aid,
                    "items": anno.list_annotations(info.get("doc_key")),
                    "counts": anno.counts(info.get("doc_key"))})


@route("/api/anno/delete")
def _api_anno_delete(h):
    body, sb, err = _guard(h)
    if err:
        return err
    anno.delete_annotation(str(body.get("id") or ""))
    out = {"ok": True}
    if body.get("path"):
        out["items"] = anno.list_annotations(body.get("path"))
        out["counts"] = anno.counts(body.get("path"))
    return h._json(out)


def _cite_line(meta, page=0):
    """把文献元数据拼成一行可读引用（作者 (年份). 标题. 出处, p.X）。"""
    meta = meta or {}
    authors = meta.get("authors") or []
    if isinstance(authors, list):
        who = ", ".join(str(a) for a in authors[:6])
        if len(authors) > 6:
            who += " et al."
    else:
        who = str(authors)
    year = meta.get("year") or ""
    title = str(meta.get("title") or "").strip()
    venue = str(meta.get("venue") or "").strip()
    bits = []
    if who:
        bits.append(who)
    if year:
        bits.append("(%s)" % year)
    head = " ".join(bits)
    tail = ". ".join(x for x in (title, venue) if x)
    line = ". ".join(x for x in (head, tail) if x)
    if page:
        line += ", p.%s" % page
    return line.strip() or "(未命名文献)"


def _md_escape(s):
    return str(s or "").replace("\r\n", "\n").strip()


@route("/api/anno/export")
def _api_anno_export(h):
    body, sb, err = _guard(h)
    if err:
        return err
    fmt = str(body.get("format") or "md").lower()
    path = body.get("path")
    items = anno.list_annotations(path)
    doc = anno.get_document(path) if path else None
    if fmt == "bibtex":
        key = os.path.splitext(os.path.basename(str(path or "ref")))[0]
        key = "".join(c for c in key if c.isalnum() or c in "-_") or "ref"
        meta = doc or {}
        authors = meta.get("authors") or []
        author = " and ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors)
        lines = ["@misc{%s," % key]
        if author:
            lines.append("  author = {%s}," % author)
        if meta.get("title"):
            lines.append("  title = {%s}," % meta["title"])
        if meta.get("year"):
            lines.append("  year = {%s}," % meta["year"])
        if meta.get("venue"):
            lines.append("  howpublished = {%s}," % meta["venue"])
        if meta.get("doi"):
            lines.append("  doi = {%s}," % meta["doi"])
        lines.append("}")
        text = "\n".join(lines)
        return h._json({"ok": True, "format": "bibtex", "text": text,
                        "name": key + ".bib"})
    title = (doc or {}).get("title") or os.path.basename(str(path or "批注"))
    out = ["# %s" % title, ""]
    if doc:
        out.append("> %s" % _cite_line(doc))
        out.append("")
    for it in items:
        head = "- [%s]" % (it.get("kind") or "note")
        if it.get("page"):
            head += " p.%s" % it.get("page")
        if it.get("tags"):
            head += " · " + ", ".join("#" + str(t) for t in it.get("tags"))
        out.append(head)
        if it.get("quote"):
            out.append("  > " + _md_escape(it["quote"]).replace("\n", "\n  > "))
        if it.get("comment"):
            out.append("  " + _md_escape(it["comment"]).replace("\n", "\n  "))
        if it.get("question"):
            out.append("  **Q：** " + _md_escape(it["question"]))
        if it.get("answer"):
            out.append("  **A：** " + _md_escape(it["answer"]).replace("\n", "\n  "))
        out.append("")
    name = "".join(c for c in title if c not in '\\/:*?"<>|')[:60] + "-批注.md"
    return h._json({"ok": True, "format": "md", "text": "\n".join(out), "name": name})


@route("/api/anno/excerpt")
def _api_anno_excerpt(h):
    """把选中段落摘录进笔记库，并留一条 excerpt 批注（带页码与引用）。"""
    body, sb, err = _guard(h)
    if err:
        return err
    import time

    from app import notes as noteslib
    doc_key = anno.norm_key(body.get("path"))
    quote = str(body.get("quote") or "").strip()
    if not doc_key or not quote:
        return h._json({"ok": False, "error": "没有可摘录的内容"}, 400)
    comment = str(body.get("comment") or "").strip()
    try:
        page = int(body.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    doc = anno.get_document(doc_key) or {}
    title = str(doc.get("title") or os.path.splitext(os.path.basename(doc_key))[0])[:80]
    note_rel = "文献摘录/" + (noteslib.clean_name(title) or "未命名") + ".md"
    content = noteslib.read_note(note_rel)
    if content is None:
        ok, created = noteslib.create_note(note_rel)
        note_rel = created if ok else note_rel
        content = ""
    head = ""
    if not (content or "").strip():
        head = "# %s\n\n- 来源：`%s`\n- 引用：%s\n" % (title, doc_key, _cite_line(doc, 0))
    block = "\n## p.%s · %s\n\n> %s\n\n%s\n" % (
        page or "?", time.strftime("%Y-%m-%d"),
        quote.replace("\n", "\n> "), comment)
    if not noteslib.write_note(note_rel, (content or "") + head + block):
        return h._json({"ok": False, "error": "写入笔记失败"}, 400)
    aid = anno.save_annotation({
        "doc_key": doc_key, "kind": "excerpt",
        "anchor": {"page": page, "block_id": str(body.get("block_id") or ""),
                   "side": "dst" if body.get("side") == "dst" else "src"},
        "quote": quote, "comment": comment,
        "citation": {"page": page, "title": title, "authors": doc.get("authors") or [],
                     "year": doc.get("year") or 0},
    })
    return h._json({"ok": True, "id": aid, "note": note_rel,
                    "items": anno.list_annotations(doc_key),
                    "counts": anno.counts(doc_key)})


@route("/api/doc/get")
def _api_doc_get(h):
    body, sb, err = _guard(h)
    if err:
        return err
    doc = anno.get_document(body.get("path"))
    return h._json({"ok": True, "doc": doc})


@route("/api/doc/save")
def _api_doc_save(h):
    body, sb, err = _guard(h)
    if err:
        return err
    doc = anno.save_document(body.get("path"), body)
    if not doc:
        return h._json({"ok": False, "error": "路径为空"}, 400)
    return h._json({"ok": True, "doc": doc})


@route("/api/doc/list")
def _api_doc_list(h):
    body, sb, err = _guard(h)
    if err:
        return err
    return h._json({"ok": True, "items": anno.list_documents(body.get("q"))})
