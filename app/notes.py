# -*- coding: utf-8 -*-
"""内置笔记库（Obsidian 核心版）

- 存储：data/notes/ 下的普通 Markdown 文件（可用任意编辑器直接改）
- 双链：[[笔记名]] / [[笔记名|别名]] / [[笔记名#锚点]]
- 能力：文件夹树、增删改查、关系图谱、反向链接、全文搜索
"""
import os
import re

from app.store import DATA_DIR, _lock

NOTES_DIR = os.path.join(DATA_DIR, "notes")
ANNO_DIR = os.path.join(NOTES_DIR, ".annotations")

WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MD = ".md"


# ============================================================
# 基础工具
# ============================================================

def ensure_notes_dir():
    os.makedirs(NOTES_DIR, exist_ok=True)


def _safe(relpath, must_exist=False):
    """把相对路径解析到 NOTES_DIR 内部；越界或非法返回 None"""
    if not isinstance(relpath, str):
        return None
    rel = relpath.strip().replace("\\", "/").lstrip("/")
    if not rel or "\x00" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    # 拒绝含非法字符的片段（如 Windows 的 ":"），避免出现怪文件或直接抛 OSError
    if any(_INVALID.search(p) for p in parts):
        return None
    root = os.path.abspath(NOTES_DIR)
    abs_path = os.path.abspath(os.path.join(root, *parts))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        return None
    if must_exist and not os.path.exists(abs_path):
        return None
    return abs_path


def _rel(abs_path):
    return os.path.relpath(abs_path, NOTES_DIR).replace(os.sep, "/")


def clean_name(name):
    """清洗文件名/文件夹名：去掉非法字符与首尾空白/点"""
    name = _INVALID.sub("", str(name or "")).strip()
    return name.strip(". ")


def note_title(rel):
    base = rel[:-3] if rel.lower().endswith(_MD) else rel
    return base.split("/")[-1]


# ============================================================
# 文件树 / 读写
# ============================================================

def list_tree():
    ensure_notes_dir()

    def walk(d):
        out = []
        try:
            names = os.listdir(d)
        except OSError:
            return out
        names.sort(key=lambda n: (not os.path.isdir(os.path.join(d, n)), n.lower()))
        for name in names:
            if name.startswith("."):
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p):
                out.append({"type": "dir", "name": name, "path": _rel(p), "children": walk(p)})
            elif name.lower().endswith(_MD):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({"type": "file", "name": name[:-3], "path": _rel(p),
                            "size": st.st_size, "mtime": st.st_mtime})
        return out

    return walk(NOTES_DIR)


def read_note(relpath):
    p = _safe(relpath, must_exist=True)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_note(relpath, content):
    ensure_notes_dir()
    p = _safe(relpath)
    if not p or os.path.isdir(p):
        return False
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with _lock:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else "")
    except OSError as e:
        print(f"[笔记] 写入失败 {relpath}：{e}")
        return False
    return True


def create_note(relpath, content=""):
    """新建笔记；已存在则返回 (False, 已存在的路径)"""
    ensure_notes_dir()
    p = _safe(relpath)
    if not p:
        return False, "路径非法"
    if os.path.exists(p):
        return False, _rel(p)
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with _lock:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content or "")
    except OSError as e:
        return False, "创建失败：%s" % e
    return True, _rel(p)


def delete_note(relpath):
    p = _safe(relpath, must_exist=True)
    if not p or os.path.isdir(p):
        return False
    try:
        os.remove(p)
    except OSError:
        return False
    return True


def create_folder(relpath):
    ensure_notes_dir()
    p = _safe(relpath)
    if not p:
        return False, "路径非法"
    if os.path.exists(p):
        return False, "已存在"
    os.makedirs(p, exist_ok=True)
    return True, _rel(p)


def delete_folder(relpath):
    import shutil
    p = _safe(relpath, must_exist=True)
    if not p or not os.path.isdir(p):
        return False
    shutil.rmtree(p, ignore_errors=True)
    return True


def _rewrite_links(old_stem, new_stem):
    """重命名笔记后，自动更新其它笔记里的 [[旧名]] / [[旧名|别名]] / [[旧名#锚点]]（Obsidian 的行为）"""
    if not old_stem or old_stem == new_stem:
        return
    new_base = new_stem.split("/")[-1]
    pairs = [(old_stem, new_stem)]
    if "/" in old_stem and new_base != new_stem:
        pairs.append((old_stem.split("/")[-1], new_base))
    subs, used = [], set()
    for old_f, new_f in pairs:
        if not old_f or old_f.lower() in used:
            continue
        used.add(old_f.lower())
        subs.append((re.compile(r"\[\[\s*" + re.escape(old_f) + r"\s*([|#][^\[\]\n]*)?\]\]",
                                re.IGNORECASE), new_f))
    if not subs:
        return
    for rel, text in list(_all_notes().items()):
        new_text = text
        for pat, nf in subs:
            new_text = pat.sub(lambda m, n=nf: "[[" + n + (m.group(1) or "") + "]]", new_text)
        if new_text != text:
            write_note(rel, new_text)


def rename_path(relpath, new_name):
    """重命名文件或文件夹（同一目录内）；重命名笔记时自动更新其它笔记里的 [[旧名]] 链接"""
    p = _safe(relpath, must_exist=True)
    if not p:
        return False, "不存在"
    name = clean_name(new_name)
    if not name:
        return False, "名称非法"
    is_file = os.path.isfile(p)
    if is_file and not name.lower().endswith(_MD):
        name += _MD
    new_path = os.path.join(os.path.dirname(p), name)
    if os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(p):
        return False, "同名已存在"
    old_rel = _rel(p)
    old_stem = old_rel[:-3] if old_rel.lower().endswith(_MD) else old_rel
    try:
        os.rename(p, new_path)
    except OSError as e:
        return False, "重命名失败：%s" % e
    new_rel = _rel(new_path)
    if is_file:
        new_stem = new_rel[:-3] if new_rel.lower().endswith(_MD) else new_rel
        _rewrite_links(old_stem, new_stem)
    return True, new_rel


# ============================================================
# 双链 / 图谱 / 反链 / 搜索
# ============================================================

def _all_notes():
    """{相对路径: 全文}"""
    out = {}
    ensure_notes_dir()
    for root, dirs, files in os.walk(NOTES_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.startswith(".") or not fn.lower().endswith(_MD):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    out[_rel(p)] = f.read()
            except (OSError, UnicodeDecodeError):
                continue
    return out


def link_targets(text):
    """抽出 [[...]] 的目标名（去掉 |别名 与 #锚点）"""
    out = []
    for m in WIKILINK_RE.finditer(text or ""):
        raw = m.group(1).split("|")[0].split("#")[0].strip()
        if raw:
            out.append(raw)
    return out


def _name_index():
    """名称 -> 相对路径：短名与「去扩展名的完整路径」都可命中"""
    idx = {}
    for rel in _all_notes():
        stem = rel[:-3] if rel.lower().endswith(_MD) else rel
        idx.setdefault(stem.lower(), rel)
        idx.setdefault(stem.split("/")[-1].lower(), rel)
    return idx


def resolve_link(name):
    """把 [[名字]] 解析成已存在的笔记路径；找不到返回 None"""
    name = (name or "").strip()
    if not name:
        return None
    idx = _name_index()
    return idx.get(name.lower()) or idx.get(name.split("/")[-1].lower())


def graph():
    """关系图谱：节点=笔记（外加未创建的「幽灵节点」），边=双链"""
    notes = _all_notes()
    by_name = {}
    for rel in notes:
        stem = rel[:-3] if rel.lower().endswith(_MD) else rel
        by_name.setdefault(stem.lower(), rel)
        by_name.setdefault(stem.split("/")[-1].lower(), rel)

    nodes, edges, seen, ghosts = [], [], set(), {}
    for rel in notes:
        nodes.append({"id": rel, "label": note_title(rel),
                      "folder": os.path.dirname(rel) or "/", "missing": False})

    for rel, text in sorted(notes.items()):
        for t in link_targets(text):
            tgt = by_name.get(t.lower()) or by_name.get(t.split("/")[-1].lower())
            if tgt:
                if tgt == rel:
                    continue
                key = (rel, tgt)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"id": "e%d" % len(edges), "from": rel, "to": tgt})
            else:
                gid = "missing:" + t
                if gid not in ghosts:
                    ghosts[gid] = {"id": gid, "label": t, "folder": "", "missing": True}
                    nodes.append(ghosts[gid])
                key = (rel, gid)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"id": "e%d" % len(edges), "from": rel, "to": gid})

    return {"nodes": nodes, "edges": edges}


def backlinks(relpath):
    """哪些笔记链接到了这一篇"""
    name = note_title(relpath).lower()
    stem = (relpath[:-3] if relpath.lower().endswith(_MD) else relpath).lower()
    out = []
    for other, text in _all_notes().items():
        if other == relpath:
            continue
        for t in link_targets(text):
            if t.lower() in (name, stem):
                out.append({"path": other, "name": note_title(other), "snippet": _snippet(text, t)})
                break
    return out


def _snippet(text, needle):
    for line in (text or "").split("\n"):
        if needle in line:
            return line.strip()[:120]
    return ""


def search(q, limit=60):
    q = (q or "").strip()
    if not q:
        return []
    low = q.lower()
    hits = []
    for rel, text in sorted(_all_notes().items()):
        name = note_title(rel)
        if low in name.lower():
            hits.append({"path": rel, "name": name, "line": 0, "snippet": ""})
        for i, line in enumerate(text.split("\n")):
            if low in line.lower():
                hits.append({"path": rel, "name": name, "line": i + 1, "snippet": line.strip()[:120]})
                break
        if len(hits) >= limit:
            break
    return hits[:limit]


# ============================================================
# 批注：与 Markdown 正文分开存放，不改动笔记文件本身
# ============================================================

def _anno_key(rel):
    return "notes:" + str(rel or "").replace("\\", "/")


def list_annotations(rel):
    """笔记批注：统一存在 annotations 表（doc_key = notes:<相对路径>）。"""
    if not _safe(rel):
        return []
    from app import anno
    out = []
    for it in anno.list_annotations(_anno_key(rel)):
        out.append({
            "id": it.get("id"),
            "kind": it.get("kind") or "note",
            "quote": it.get("quote") or "",
            "prefix": it.get("prefix") or "",
            "suffix": it.get("suffix") or "",
            "nth": max(0, int((it.get("anchor") or {}).get("nth") or 0)),
            "comment": it.get("comment") or "",
            "question": it.get("question") or "",
            "answer": it.get("answer") or "",
            "created": it.get("created_at"),
            "updated": it.get("updated_at"),
        })
    return out


def save_annotation(rel, info):
    """新建或更新一条批注，返回 id；参数不合法返回 None。"""
    if not _safe(rel):
        return None
    info = info or {}
    quote = str(info.get("quote") or "").strip()[:500]
    comment = str(info.get("comment") or "").strip()[:2000]
    question = str(info.get("question") or "").strip()[:1000]
    answer = str(info.get("answer") or "").strip()[:12000]
    # 划词问答也走同一张 annotations 表：kind=qa + question/answer；
    # comment 仍然写一份（笔记抽屉按 comment 渲染，不用改前端）。
    if answer and not comment:
        comment = "问：%s\n\n答：%s" % (question or "（未填写问题）", answer)
    if not quote or not (comment or answer):
        return None
    from app import anno
    try:
        nth = max(0, int(info.get("nth") or 0))
    except (TypeError, ValueError):
        nth = 0
    prefix = str(info.get("prefix") or "")[:40]
    suffix = str(info.get("suffix") or "")[:40]
    kind = "qa" if (answer or info.get("kind") == "qa") else "note"
    return anno.save_annotation({
        "id": info.get("id"),
        "doc_key": _anno_key(rel),
        "kind": kind,
        "quote": quote, "prefix": prefix, "suffix": suffix,
        "comment": comment,
        "question": question, "answer": answer,
        "anchor": {"quote": quote, "nth": nth, "prefix": prefix, "suffix": suffix},
    })


def delete_annotation(rel, aid):
    if not _safe(rel):
        return False
    from app import anno
    anno.delete_annotation(str(aid or ""))
    return True
