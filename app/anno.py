# -*- coding: utf-8 -*-
"""统一批注 / 文献条目存储。

为什么从 JSON 换成 SQLite
------------------------
笔记批注（`data/notes/.annotations`）和阅读器批注（`data/reader/<doc>/annotations.json`）
原来是两套互不相干的 JSON。面向学术用户时，需要跨文档检索、按标签聚合、导出引用，
JSON 每文档一份做不到；这里统一到 `annotations` / `documents` 两张表。

文档主键（doc_key）
------------------
- 沙箱文件：沙箱内相对路径，如 `out/main.pdf`
- 笔记库：`notes:<笔记相对路径>`，与沙箱路径区分开

锚点（anchor）
--------------
按文档类型存不同定位信息，`quote + prefix/suffix` 作为漂移后的兜底：
- PDF 阅读流：`{block_id, page, side}`
- 文本 / 代码：`{line_start, col_start, line_end, col_end}`
- 笔记库：`{quote, nth, prefix, suffix}`
"""
import hashlib
import json
import os
import secrets
import time

from app import db

COLORS = {"yellow", "green", "blue", "pink", "purple"}
KINDS = {"highlight", "note", "excerpt", "qa"}

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"}
_CODE_EXT = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".lua",
    ".kt", ".swift", ".css", ".html", ".htm", ".xml", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".conf",
}


def norm_key(path):
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")[:500]


def doc_type_of(doc_key):
    key = str(doc_key or "")
    if key.startswith("notes:"):
        return "md"
    ext = os.path.splitext(key)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".md", ".markdown"):
        return "md"
    if ext == ".tex":
        return "tex"
    if ext in _IMAGE_EXT:
        return "image"
    if ext == ".docx":
        return "docx"
    if ext == ".pptx":
        return "pptx"
    if ext in _CODE_EXT:
        return "code"
    return "text"


def _loads(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except Exception:                    # noqa: BLE001
        return default


def _row(r):
    d = dict(r)
    d["anchor"] = _loads(d.get("anchor"), {})
    d["tags"] = _loads(d.get("tags"), [])
    d["citation"] = _loads(d.get("citation"), {})
    # 常用锚点字段摊平：前端（阅读器 / 编辑器）不必再解一层 anchor
    for k in ("block_id", "page", "side", "line_start", "line_end",
              "col_start", "col_end", "nth", "rects"):
        if k in d["anchor"]:
            d[k] = d["anchor"][k]
    return d


def list_annotations(doc_key=None, q=None, kind=None, tag=None, limit=500):
    migrate_legacy()
    conn = db.get_db()
    where, params = [], []
    if doc_key:
        where.append("doc_key=?")
        params.append(norm_key(doc_key))
    if kind and kind in KINDS:
        where.append("kind=?")
        params.append(kind)
    if tag:
        where.append("tags LIKE ?")
        params.append('%"' + str(tag).replace('"', "") + '"%')
    if q:
        like = "%" + str(q).strip() + "%"
        where.append("(quote LIKE ? OR comment LIKE ? OR question LIKE ? "
                     "OR answer LIKE ? OR tags LIKE ?)")
        params.extend([like] * 5)
    sql = "SELECT * FROM annotations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(max(1, min(int(limit or 500), 5000)))
    rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def get_annotation(aid):
    row = db.get_db().execute("SELECT * FROM annotations WHERE id=?", (str(aid or ""),)).fetchone()
    return _row(row) if row else None


def counts(doc_key):
    rows = db.get_db().execute(
        "SELECT kind, COUNT(*) AS n FROM annotations WHERE doc_key=? GROUP BY kind",
        (norm_key(doc_key),)).fetchall()
    out = {"total": 0, "highlight": 0, "note": 0, "qa": 0, "excerpt": 0}
    for r in rows:
        k = str(r["kind"] or "note")
        out[k] = int(r["n"])
        out["total"] += int(r["n"])
    return out


def save_annotation(info):
    """新建或更新一条批注；返回 id，参数不合法返回 None。"""
    info = info or {}
    doc_key = norm_key(info.get("doc_key") or info.get("path"))
    if not doc_key:
        return None
    quote = str(info.get("quote") or "").strip()[:4000]
    comment = str(info.get("comment") or "").strip()[:8000]
    answer = str(info.get("answer") or "").strip()[:12000]
    question = str(info.get("question") or "").strip()[:1000]
    if not (quote or comment or answer or question):
        return None
    kind = str(info.get("kind") or "note")
    if kind not in KINDS:
        kind = "note"
    color = str(info.get("color") or "yellow")
    if color not in COLORS:
        color = "yellow"
    anchor = info.get("anchor")
    if not isinstance(anchor, dict):
        anchor = {}
        for k in ("block_id", "page", "side", "line_start", "line_end",
                  "col_start", "col_end", "nth", "rects"):
            if info.get(k) is not None:
                anchor[k] = info[k]
    tags = info.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip()[:40] for t in tags if str(t).strip()][:12]
    citation = info.get("citation")
    if not isinstance(citation, dict):
        citation = {}
    now = time.time()
    aid = str(info.get("id") or "").strip()
    conn = db.get_db()
    if aid and conn.execute("SELECT 1 FROM annotations WHERE id=?", (aid,)).fetchone():
        conn.execute(
            "UPDATE annotations SET kind=?, anchor=?, quote=?, prefix=?, suffix=?, "
            "color=?, tags=?, comment=?, question=?, answer=?, source=?, citation=?, "
            "updated_at=? WHERE id=?",
            (kind, json.dumps(anchor, ensure_ascii=False), quote,
             str(info.get("prefix") or "")[:200], str(info.get("suffix") or "")[:200],
             color, json.dumps(tags, ensure_ascii=False), comment, question, answer,
             "ai" if info.get("source") == "ai" else "user",
             json.dumps(citation, ensure_ascii=False), now, aid))
        conn.commit()
        return aid
    aid = "a_" + secrets.token_hex(6)
    try:
        created = float(info.get("created") or 0) or now
    except (TypeError, ValueError):
        created = now
    conn.execute(
        "INSERT INTO annotations (id, doc_key, doc_type, kind, anchor, quote, prefix, "
        "suffix, color, tags, comment, question, answer, source, citation, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, doc_key, doc_type_of(doc_key), kind,
         json.dumps(anchor, ensure_ascii=False), quote,
         str(info.get("prefix") or "")[:200], str(info.get("suffix") or "")[:200],
         color, json.dumps(tags, ensure_ascii=False), comment, question, answer,
         "ai" if info.get("source") == "ai" else "user",
         json.dumps(citation, ensure_ascii=False), created, now))
    conn.commit()
    return aid


def delete_annotation(aid):
    conn = db.get_db()
    conn.execute("DELETE FROM annotations WHERE id=?", (str(aid or ""),))
    conn.commit()
    return True


# ---------------------------------------------------------------- 文献条目

def get_document(doc_key):
    row = db.get_db().execute("SELECT * FROM documents WHERE doc_key=?",
                              (norm_key(doc_key),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["authors"] = _loads(d.get("authors"), [])
    d["tags"] = _loads(d.get("tags"), [])
    d["extra"] = _loads(d.get("extra"), {})
    return d


def save_document(doc_key, info):
    key = norm_key(doc_key)
    if not key:
        return None
    info = info or {}
    old = get_document(key) or {}
    now = time.time()
    authors = info.get("authors", old.get("authors") or [])
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.replace("；", ";").split(";") if a.strip()]
    tags = info.get("tags", old.get("tags") or [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    try:
        year = int(info.get("year", old.get("year") or 0) or 0)
    except (TypeError, ValueError):
        year = 0
    conn = db.get_db()
    conn.execute(
        "INSERT INTO documents (doc_key, title, authors, year, venue, doi, abstract, "
        "tags, status, extra, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_key) DO UPDATE SET title=excluded.title, authors=excluded.authors, "
        "year=excluded.year, venue=excluded.venue, doi=excluded.doi, "
        "abstract=excluded.abstract, tags=excluded.tags, status=excluded.status, "
        "extra=excluded.extra, updated_at=excluded.updated_at",
        (key, str(info.get("title", old.get("title") or ""))[:400],
         json.dumps(authors, ensure_ascii=False), year,
         str(info.get("venue", old.get("venue") or ""))[:200],
         str(info.get("doi", old.get("doi") or ""))[:200],
         str(info.get("abstract", old.get("abstract") or ""))[:4000],
         json.dumps([str(t) for t in tags][:20], ensure_ascii=False),
         str(info.get("status", old.get("status") or "unread"))[:20],
         json.dumps(info.get("extra", old.get("extra") or {}), ensure_ascii=False)[:8000],
         old.get("created_at") or now, now))
    conn.commit()
    return get_document(key)


def list_documents(q=None, limit=200):
    conn = db.get_db()
    if q:
        like = "%" + str(q).strip() + "%"
        rows = conn.execute(
            "SELECT * FROM documents WHERE title LIKE ? OR authors LIKE ? OR venue LIKE ? "
            "OR tags LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (like, like, like, like, max(1, min(int(limit or 200), 1000)))).fetchall()
    else:
        rows = conn.execute("SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?",
                            (max(1, min(int(limit or 200), 1000)),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["authors"] = _loads(d.get("authors"), [])
        d["tags"] = _loads(d.get("tags"), [])
        d["extra"] = _loads(d.get("extra"), {})
        out.append(d)
    return out


# ---------------------------------------------------------------- 旧数据迁移

def _migrate_notes():
    """把 `data/notes/.annotations/*.json` 导入 annotations 表（只跑一次）。"""
    from app import notes as noteslib

    base = os.path.join(noteslib.NOTES_DIR, ".annotations")
    if not os.path.isdir(base):
        return 0
    marker = os.path.join(base, ".migrated_sqlite")
    if os.path.isfile(marker):
        return 0
    # 反查：sha1(相对路径) -> 相对路径（笔记文件可能已被删，只在现有文件里找）
    by_hash = {}
    for dirpath, dirnames, files in os.walk(noteslib.NOTES_DIR):
        dirnames[:] = [d for d in dirnames if d != ".annotations"]
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, noteslib.NOTES_DIR).replace(os.sep, "/")
            by_hash[hashlib.sha1(rel.encode("utf-8")).hexdigest()] = rel
    n = 0
    for fn in os.listdir(base):
        if not fn.endswith(".json"):
            continue
        rel = by_hash.get(fn[:-5])
        if not rel:
            continue
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:                # noqa: BLE001
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            save_annotation({
                "doc_key": "notes:" + rel, "kind": "note", "quote": it.get("quote"),
                "prefix": it.get("prefix"), "suffix": it.get("suffix"),
                "anchor": {"quote": it.get("quote"), "nth": it.get("nth"),
                           "prefix": it.get("prefix"), "suffix": it.get("suffix")},
                "comment": it.get("comment"), "created": it.get("created"),
            })
            n += 1
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass
    return n


_migrated = False


def migrate_legacy():
    global _migrated
    if _migrated:
        return
    try:
        _migrate_notes()
    except Exception:                    # noqa: BLE001  迁移失败不影响新功能
        pass
    _migrated = True
