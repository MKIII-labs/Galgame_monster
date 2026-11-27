# -*- coding: utf-8 -*-
"""内置笔记库（Obsidian 核心版）

- 存储：data/notes/ 下的普通 Markdown 文件（可用任意编辑器直接改）
- 双链：[[笔记名]] / [[笔记名|别名]] / [[笔记名#锚点]]
- 能力：文件夹树、增删改查、关系图谱、反向链接、全文搜索
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

from app.store import DATA_DIR, _lock

NOTES_DIR = os.path.join(DATA_DIR, "notes")
ANNO_DIR = os.path.join(NOTES_DIR, ".annotations")
LINK_FILE = os.path.join(DATA_DIR, "note_links.json")

WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MD = ".md"

# 笔记索引：rel -> {mtime_ns, size, text, title, stem, links}
# 搜索 / 反链 / 图谱 / 改名每次都遍历全部笔记，没有索引就得反复读盘。
# 这里只按目录列表 + stat 判断文件是否变过，没变过就复用上次读到的内容。
# 本应用内的写入走 _invalidate_index() 精确失效，外部编辑器改动靠 mtime+size 发现。
_INDEX = {}
_INDEX_LOCK = threading.RLock()
_INDEX_ROOT = None


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


def _invalidate_index(rel=None, is_dir=False):
    """让索引条目失效（rel=None 清空整表）。只删缓存，不读盘。"""
    with _INDEX_LOCK:
        if rel is None:
            _INDEX.clear()
        elif is_dir:
            pre = rel + "/"
            for k in [k for k in _INDEX if k == rel or k.startswith(pre)]:
                _INDEX.pop(k, None)
        else:
            _INDEX.pop(rel, None)


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


def _atomic_write(p, text):
    """同目录临时文件 + os.replace：中途崩溃/断电不会留下半截笔记。

    临时文件名以 "." 开头，索引与文件树本来就会跳过隐藏文件。
    """
    d = os.path.dirname(p) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".gm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
            _atomic_write(p, content if isinstance(content, str) else "")
    except OSError as e:
        print(f"[笔记] 写入失败 {relpath}：{e}")
        return False
    _invalidate_index(_rel(p))
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
            _atomic_write(p, content or "")
    except OSError as e:
        return False, "创建失败：%s" % e
    _invalidate_index(_rel(p))
    return True, _rel(p)


def delete_note(relpath):
    p = _safe(relpath, must_exist=True)
    if not p or os.path.isdir(p):
        return False
    rel = _rel(p)
    _prepare_annotations()
    try:
        os.remove(p)
    except OSError:
        return False
    _invalidate_index(rel)
    _drop_links(rel)
    _drop_annotations(rel)
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
    rel = _rel(p)
    _prepare_annotations()
    shutil.rmtree(p, ignore_errors=True)
    _invalidate_index(rel, is_dir=True)
    _drop_links(rel, is_dir=True)
    _drop_annotations(rel, is_dir=True)
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
    for rel, meta in list(_notes_meta().items()):
        text = meta["text"]
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
    _prepare_annotations()
    try:
        os.rename(p, new_path)
    except OSError as e:
        return False, "重命名失败：%s" % e
    new_rel = _rel(new_path)
    _invalidate_index(old_rel, is_dir=not is_file)
    _invalidate_index(new_rel, is_dir=not is_file)
    _remap_link_paths(old_rel, new_rel, is_dir=not is_file)
    _remap_annotations(old_rel, new_rel, is_dir=not is_file)
    if is_file:
        new_stem = new_rel[:-3] if new_rel.lower().endswith(_MD) else new_rel
        _rewrite_links(old_stem, new_stem)
    return True, new_rel


def move_note(relpath, target_dir):
    """把笔记移动到另一个目录（target_dir="" 表示根目录）。

    双链按「笔记名」解析，移动不会断链，因此不需要改写其它笔记。
    """
    p = _safe(relpath, must_exist=True)
    if not p or not os.path.isfile(p):
        return False, "笔记不存在"
    tdir = str(target_dir or "").strip().replace("\\", "/").strip("/")
    if tdir:
        d = _safe(tdir)
        if not d or not os.path.isdir(d):
            return False, "目标文件夹不存在"
    else:
        d = NOTES_DIR
    new_path = os.path.join(d, os.path.basename(p))
    if os.path.abspath(new_path) == os.path.abspath(p):
        return True, _rel(p)
    if os.path.exists(new_path):
        return False, "目标位置已有同名笔记"
    old_rel = _rel(p)
    _prepare_annotations()
    try:
        os.rename(p, new_path)
    except OSError as e:
        return False, "移动失败：%s" % e
    new_rel = _rel(new_path)
    _invalidate_index(old_rel)
    _invalidate_index(new_rel)
    _remap_link_paths(old_rel, new_rel)
    _remap_annotations(old_rel, new_rel)
    return True, new_rel


def move_path(relpath, target_dir):
    """移动笔记或文件夹（Obsidian 文件树拖拽用，target_dir="" 表示根目录）。"""
    p = _safe(relpath, must_exist=True)
    if not p:
        return False, "不存在"
    if not os.path.isdir(p):
        return move_note(relpath, target_dir)
    tdir = str(target_dir or "").strip().replace("\\", "/").strip("/")
    if tdir:
        d = _safe(tdir)
        if not d or not os.path.isdir(d):
            return False, "目标文件夹不存在"
    else:
        d = NOTES_DIR
    src = os.path.abspath(p)
    dst = os.path.abspath(os.path.join(d, os.path.basename(p)))
    tdir_abs = os.path.abspath(d)
    if src == dst:
        return True, _rel(p)
    if tdir_abs == src or tdir_abs.startswith(src + os.sep):
        return False, "不能移动到自身或子目录"
    if os.path.exists(dst):
        return False, "目标位置已有同名文件夹"
    old_rel = _rel(p)
    _prepare_annotations()
    try:
        os.rename(p, dst)
    except OSError as e:
        return False, "移动失败：%s" % e
    new_rel = _rel(dst)
    _invalidate_index(old_rel, is_dir=True)
    _invalidate_index(new_rel, is_dir=True)
    _remap_link_paths(old_rel, new_rel, is_dir=True)
    _remap_annotations(old_rel, new_rel, is_dir=True)
    return True, new_rel


# ============================================================
# 双链 / 图谱 / 反链 / 搜索
# ============================================================

def _refresh_index():
    """把磁盘上的笔记同步进 _INDEX：只遍历目录 + stat，内容没变就不重读。"""
    global _INDEX_ROOT
    root = os.path.abspath(NOTES_DIR)
    with _INDEX_LOCK:
        if _INDEX_ROOT != root:
            # 测试或运行期换过笔记根目录：旧缓存全部作废
            _INDEX.clear()
            _INDEX_ROOT = root
        ensure_notes_dir()
        alive = set()
        for dirpath, dirs, files in os.walk(NOTES_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith(".") or not fn.lower().endswith(_MD):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rel = _rel(p)
                alive.add(rel)
                hit = _INDEX.get(rel)
                if hit and hit["mtime_ns"] == st.st_mtime_ns and hit["size"] == st.st_size:
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    _INDEX.pop(rel, None)
                    alive.discard(rel)
                    continue
                stem = rel[:-3] if rel.lower().endswith(_MD) else rel
                _INDEX[rel] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "text": text,
                               "title": note_title(rel), "stem": stem,
                               "links": link_targets(text)}
        for rel in [r for r in _INDEX if r not in alive]:
            _INDEX.pop(rel, None)
        return _INDEX


def _notes_meta():
    """{相对路径: 索引项}：返回浅拷贝，调用方遍历时不受并发刷新影响"""
    with _INDEX_LOCK:
        return dict(_refresh_index())


def _all_notes():
    """{相对路径: 全文}（走索引，未改动的文件不重复读盘）"""
    return {rel: m["text"] for rel, m in _notes_meta().items()}


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
    for rel, meta in _notes_meta().items():
        stem = meta["stem"]
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
    notes = _notes_meta()
    by_name = {}
    for rel, meta in notes.items():
        stem = meta["stem"]
        by_name.setdefault(stem.lower(), rel)
        by_name.setdefault(stem.split("/")[-1].lower(), rel)

    nodes, edges, seen, ghosts = [], [], set(), {}
    for rel, meta in notes.items():
        nodes.append({"id": rel, "label": meta["title"],
                      "folder": os.path.dirname(rel) or "/", "missing": False})

    for rel, meta in sorted(notes.items()):
        for t in meta["links"]:
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
    for other, meta in _notes_meta().items():
        if other == relpath:
            continue
        for t in meta["links"]:
            if t.lower() in (name, stem):
                out.append({"path": other, "name": meta["title"],
                            "snippet": _snippet(meta["text"], t)})
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
    for rel, meta in sorted(_notes_meta().items()):
        name = meta["title"]
        if low in name.lower():
            hits.append({"path": rel, "name": name, "line": 0, "snippet": ""})
        text = meta["text"]
        if low in text.lower():
            for i, line in enumerate(text.split("\n")):
                if low in line.lower():
                    hits.append({"path": rel, "name": name, "line": i + 1,
                                 "snippet": line.strip()[:120]})
                    break
        if len(hits) >= limit:
            break
    return hits[:limit]


# ============================================================
# 对话 → 笔记：显式捕获 + 来源索引
# ------------------------------------------------------------
# 只记录「哪条对话被记进了哪篇笔记」，不复制会话数据，也不改会话本身。
# 笔记仍是普通 Markdown；删除笔记时只清理索引，不影响对话。
# ============================================================

def _load_links():
    try:
        with open(LINK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_links(items):
    d = os.path.dirname(LINK_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = LINK_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump((items or [])[-5000:], f, ensure_ascii=False)
        os.replace(tmp, LINK_FILE)


def _remap_link_paths(old_rel, new_rel, is_dir=False):
    old_rel = str(old_rel or "").replace("\\", "/")
    new_rel = str(new_rel or "").replace("\\", "/")
    if not old_rel or not new_rel or old_rel == new_rel:
        return
    items = _load_links()
    changed = False
    for it in items:
        p = str(it.get("note_path") or "")
        if p == old_rel:
            it["note_path"] = new_rel
        elif is_dir and p.startswith(old_rel + "/"):
            it["note_path"] = new_rel + p[len(old_rel):]
        else:
            continue
        it["note_title"] = note_title(it["note_path"])
        changed = True
    if changed:
        _save_links(items)


def _drop_links(rel, is_dir=False):
    rel = str(rel or "").replace("\\", "/")
    if not rel:
        return
    items = _load_links()
    kept = [it for it in items
            if not (it.get("note_path") == rel
                    or (is_dir and str(it.get("note_path") or "").startswith(rel + "/")))]
    if len(kept) != len(items):
        _save_links(kept)


# 批注的 doc_key 是 `notes:<相对路径>`。旧版 JSON 批注要先导入统一表，
# 否则改名后仍会按旧路径迁移，留下孤儿。
def _prepare_annotations():
    try:
        from app import anno
        anno.migrate_legacy()
    except Exception as e:               # noqa: BLE001  批注失败不影响笔记操作
        print("[批注] 迁移旧数据失败：%s" % e)


def _remap_annotations(old_rel, new_rel, is_dir=False):
    try:
        from app import anno
        anno.remap_note_keys(old_rel, new_rel, is_dir=is_dir)
    except Exception as e:               # noqa: BLE001
        print("[批注] 迁移失败：%s" % e)


def _drop_annotations(rel, is_dir=False):
    try:
        from app import anno
        anno.drop_note_keys(rel, is_dir=is_dir)
    except Exception as e:               # noqa: BLE001
        print("[批注] 清理失败：%s" % e)


def _capture_block(text, info, stamp):
    """生成追加到笔记末尾的 Markdown 块。"""
    role = str(info.get("role") or "")
    role_label = "我的提问" if role == "user" else ("AI 回复" if role == "ai" else "对话")
    midx = info.get("msg_index")
    sid = str(info.get("session_id") or "")
    lines = ["## 对话摘录 · %s" % stamp, ""]
    lines.extend("> " + ln for ln in text.split("\n"))
    lines.append("")
    source = "来源：%s" % role_label
    if midx is not None:
        source += " · #%s" % midx
    stitle = str(info.get("session_title") or "").strip()
    if stitle:
        source += " · 《%s》" % stitle[:60]
    if sid and midx is not None:
        from urllib.parse import quote as urlquote
        source += " · [回到对话 #%s](/__gm/chat?session=%s&msg=%s)" % (
            midx, urlquote(sid, safe=""), midx)
    lines.append(source)
    thought = str(info.get("note") or "").strip()
    if thought:
        lines.extend(["", "我的想法：" + thought])
    return "\n".join(lines) + "\n"


def capture_from_chat(info):
    """把一段对话摘录显式写入笔记。

    target: current（当前笔记）/ inbox（今日学习收件箱）/ new（新建笔记）
    返回 (True, {path, title, deduped}) 或 (False, 错误信息)。
    """
    info = info or {}
    text = str(info.get("text") or "").strip()
    if not text:
        return False, "内容为空"
    text = text[:12000]
    try:
        midx = int(info.get("msg_index"))
    except (TypeError, ValueError):
        midx = None
    role = str(info.get("role") or "")
    if role not in ("user", "ai"):
        role = ""
    sid = str(info.get("session_id") or "").strip()
    info = dict(info, msg_index=midx, role=role, session_id=sid)

    target = str(info.get("target") or "inbox").strip()
    if target == "current":
        rel = str(info.get("path") or "").strip()
        p = _safe(rel, must_exist=True)
        if not p or not os.path.isfile(p):
            return False, "当前笔记不存在"
        rel = _rel(p)
    elif target == "new":
        name = clean_name(info.get("title") or "")
        if not name:
            return False, "请填写笔记标题"
        if not name.lower().endswith(_MD):
            name += _MD
        rel = name
        p = _safe(rel)
        if not p:
            return False, "路径非法"
        if os.path.exists(p):
            return False, "同名笔记已存在"
        create_note(rel, "# " + note_title(rel) + "\n")
    else:
        rel = "学习收件箱/%s.md" % time.strftime("%Y-%m-%d")
        p = _safe(rel)
        if not p:
            return False, "路径非法"
        if not os.path.exists(p):
            create_note(rel, "# %s 学习收件箱\n" % time.strftime("%Y-%m-%d"))
    title = note_title(rel)

    text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
    items = _load_links()
    for it in items:
        if (it.get("session_id") == sid and it.get("msg_index") == midx
                and it.get("note_path") == rel and it.get("text_hash") == text_hash):
            return True, {"path": rel, "title": title, "deduped": True}

    block = _capture_block(text, info, time.strftime("%H:%M"))
    old = read_note(rel) or ""
    body = old.rstrip("\n")
    write_note(rel, (body + "\n\n" + block) if body else block)

    items.append({
        "session_id": sid, "msg_index": midx, "role": role,
        "note_path": rel, "note_title": title,
        "quote": text[:300], "text_hash": text_hash,
        "at": time.time(),
    })
    _save_links(items)
    return True, {"path": rel, "title": title, "deduped": False}


def list_chat_captures(session_id):
    """某条会话已经被记进笔记的消息（新→旧）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    items = [it for it in _load_links() if it.get("session_id") == sid]
    items.sort(key=lambda it: it.get("at") or 0, reverse=True)
    return items


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
