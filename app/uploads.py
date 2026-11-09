# -*- coding: utf-8 -*-
import base64
import hashlib
import os
import re
import shutil
import time
import uuid
from app.store import DATA_DIR
from app.sessions import load_sessions

# ============================================================
# 聊天图片附件：落盘到 data/uploads/<会话>/，消息里只存引用
# ============================================================
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

_UPLOAD_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}
_MIME_BY_EXT = {v: k for k, v in _UPLOAD_EXT.items()}
_MIME_BY_EXT[".jpeg"] = "image/jpeg"
# 文档附件回传时也要给对 Content-Type：PDF 用错类型，浏览器会直接下载而不是内嵌预览
_MIME_BY_EXT.update({
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})


def _upload_dir(sid):
    return os.path.join(UPLOAD_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", str(sid or "_")))


def _safe_ext(name):
    """从原始文件名里取一个安全的扩展名（用于 mime 认不出来时兜底）"""
    e = os.path.splitext(str(name or ""))[1].lower()
    return e if re.match(r"^\.[a-z0-9]{1,8}$", e or "") else ""


def att_id(name):
    """附件 id：由落盘文件名派生。

    这样「旧会话里没有 id 字段」也能直接算出来，不需要迁移消息；
    同一会话内落盘名是 uuid，冲突概率可忽略。非 ASCII 文件名（旧附件只有原名时）
    退回短哈希，避免全被压成同一个 id。
    """
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    clean = re.sub(r"[^A-Za-z0-9]", "", stem)
    if len(clean) >= 6:
        return "att_" + clean[:12]
    return "att_" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:12]


def save_upload(sid, mime, b64, name=""):
    """把 base64 内容存盘，返回 {id, name, mime, size}；失败返回 None"""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    ext = _UPLOAD_EXT.get((mime or "").lower()) or _safe_ext(name)
    if not ext:
        ext = ".png" if str(mime or "").startswith("image/") else ".bin"
    fname = uuid.uuid4().hex + ext
    d = _upload_dir(sid)
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "wb") as f:
            f.write(raw)
    except OSError:
        return None
    return {"id": att_id(fname), "name": fname,
            "mime": mime or "application/octet-stream", "size": len(raw)}


def _msg_images(m):
    """一条消息里的图片列表：新结构 images（多图）优先，旧结构 image 兜底。"""
    if not isinstance(m, dict):
        return []
    ims = m.get("images")
    if isinstance(ims, list) and ims:
        return [x for x in ims if isinstance(x, dict)]
    im = m.get("image")
    return [im] if isinstance(im, dict) else []


def read_message_images(sid, msg, limit=4):
    """把某条消息已留存的图片读回内存（base64），用于重发 / 继续时重新附图。"""
    d = _upload_dir(sid)
    out = []
    for im in _msg_images(msg):
        name = os.path.basename(str(im.get("file") or im.get("name") or ""))
        if not name:
            continue
        try:
            with open(os.path.join(d, name), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if not raw:
            continue
        ext = os.path.splitext(name)[1].lower()
        out.append({"mime": im.get("mime") or _MIME_BY_EXT.get(ext, "image/png"),
                    "data": base64.b64encode(raw).decode("ascii")})
        if len(out) >= max(1, int(limit or 4)):
            break
    return out



def _upload_path(sid, name):
    name = os.path.basename(str(name or ""))       # 防目录穿越
    if not name:
        return None
    p = os.path.join(_upload_dir(sid), name)
    return p if os.path.isfile(p) else None


def _referenced_files(m):
    """一条消息引用到的落盘文件名（多图 images 全部算上，文档用 docs / doc 的 file）。"""
    names = []
    for im in _msg_images(m):
        sel = im.get("file") or im.get("name")      # 附件用落盘名 file，图片用 name
        nm = os.path.basename(str(sel or ""))
        if nm:
            names.append(nm)
    docs = m.get("docs") if isinstance(m, dict) else None
    if isinstance(docs, list) and docs:
        for d in docs:
            if not isinstance(d, dict):
                continue
            nm = os.path.basename(str(d.get("file") or d.get("name") or ""))
            if nm:
                names.append(nm)
    else:
        doc = m.get("doc") if isinstance(m, dict) else None
        if isinstance(doc, dict):
            nm = os.path.basename(str(doc.get("file") or doc.get("name") or ""))
            if nm:
                names.append(nm)
    return names


def drop_upload_files(sid, msgs):
    """删除这些消息引用的图片 / 文档附件文件（用于撤回 / 删除消息）"""
    d = _upload_dir(sid)
    for m in msgs or []:
        for name in _referenced_files(m):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


def delete_session_uploads(sid):
    shutil.rmtree(_upload_dir(sid), ignore_errors=True)


QUARANTINE_DIR = ".orphan"
QUARANTINE_KEEP = 3          # 保留最近几批隔离目录，超出再清掉


def _quarantine_dir(dpath):
    """把不认识的会话目录移进 .orphan/<时间戳>/，不要直接 rmtree。

    为什么不再直接删：测试 / 脚本如果把 ``SESSION_FILE`` 指到临时文件再起服务，
    启动清理读到的会话列表就是临时数据；此时真实 uploads 下的目录全都会被当成
    「孤儿」删掉（2026-11-09 实际发生过，多轮图片附件全部丢失）。
    移到隔离区至少还能人工找回来。
    """
    try:
        batch = os.path.join(UPLOAD_DIR, QUARANTINE_DIR, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(batch, exist_ok=True)
        dst = os.path.join(batch, os.path.basename(dpath))
        if os.path.exists(dst):
            dst += "-" + uuid.uuid4().hex[:6]
        shutil.move(dpath, dst)
        return True
    except OSError:
        return False


def _prune_quarantine():
    root = os.path.join(UPLOAD_DIR, QUARANTINE_DIR)
    if not os.path.isdir(root):
        return
    try:
        batches = sorted(d for d in os.listdir(root)
                         if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return
    for name in batches[:-QUARANTINE_KEEP] if QUARANTINE_KEEP > 0 else batches:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _session_file_is_default():
    """当前读的会话文件是不是真实 data/sessions.json。

    测试 / 脚本常把 ``sessions.SESSION_FILE`` 改到临时目录；此时去清理真实
    ``UPLOAD_DIR`` 必然把全部目录误判成孤儿。路径不一致就直接放弃清理。
    """
    try:
        import app.sessions as _s
        from app.store import SESSION_FILE as _default
        return (os.path.realpath(os.path.abspath(_s.SESSION_FILE))
                == os.path.realpath(os.path.abspath(_default)))
    except Exception:
        return False


def sweep_uploads():
    """启动时清理孤儿附件：会话已删除的目录移入隔离区，会话还在的删掉没被引用的文件。

    安全开关（任意一个命中就完全不清理）：
    - ``GM_NO_SWEEP=1``：测试 / 脚本显式关闭；
    - ``PYTEST_CURRENT_TEST``：pytest 里默认关闭（live 用例会临时改 SESSION_FILE）；
    - 会话文件不是默认 ``data/sessions.json``：说明被指到了临时文件，直接跳过；
    - 会话列表为空：宁可留着，也不要在读不到数据时把附件全清掉。
    """
    if os.environ.get("GM_NO_SWEEP"):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not _session_file_is_default():
        print("[附件清理] 会话文件不是默认路径，跳过清理（避免误删）")
        return
    if not os.path.isdir(UPLOAD_DIR):
        return
    try:
        data = load_sessions()
    except Exception:
        return
    rows = (data or {}).get("sessions") or []
    if not rows:
        print("[附件清理] 会话列表为空，跳过清理（避免误删）")
        return
    alive = {}                                     # 目录名 -> 该会话仍被引用的文件名集合
    for s in rows:
        if not isinstance(s, dict):
            continue
        keep = set()
        for m in s.get("messages") or []:
            if not isinstance(m, dict):
                continue
            keep.update(_referenced_files(m))
        alive[os.path.basename(_upload_dir(s.get("id")))] = keep
    removed_files = quarantined = 0
    for dname in os.listdir(UPLOAD_DIR):
        if dname.startswith("."):                   # .orphan 等内部目录
            continue
        dpath = os.path.join(UPLOAD_DIR, dname)
        if not os.path.isdir(dpath):
            continue
        if dname not in alive:
            # 会话列表里没有这个目录：先隔离，不删除
            if _quarantine_dir(dpath):
                quarantined += 1
            continue
        keep = alive[dname]
        for fname in os.listdir(dpath):
            if fname in keep:
                continue
            try:
                os.remove(os.path.join(dpath, fname))
                removed_files += 1
            except OSError:
                pass
    _prune_quarantine()
    if quarantined or removed_files:
        print(f"[附件清理] 隔离孤儿目录 {quarantined} 个、删除无用文件 {removed_files} 个")
