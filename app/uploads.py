# -*- coding: utf-8 -*-
import base64
import os
import re
import shutil
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


def _upload_dir(sid):
    return os.path.join(UPLOAD_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", str(sid or "_")))


def _safe_ext(name):
    """从原始文件名里取一个安全的扩展名（用于 mime 认不出来时兜底）"""
    e = os.path.splitext(str(name or ""))[1].lower()
    return e if re.match(r"^\.[a-z0-9]{1,8}$", e or "") else ""


def save_upload(sid, mime, b64, name=""):
    """把 base64 内容存盘，返回 {name, mime, size}；失败返回 None"""
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
    return {"name": fname, "mime": mime or "application/octet-stream", "size": len(raw)}


def _upload_path(sid, name):
    name = os.path.basename(str(name or ""))       # 防目录穿越
    if not name:
        return None
    p = os.path.join(_upload_dir(sid), name)
    return p if os.path.isfile(p) else None


def drop_upload_files(sid, msgs):
    """删除这些消息引用的图片 / 文档附件文件（用于撤回 / 删除消息）"""
    d = _upload_dir(sid)
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        for key in ("image", "doc"):
            im = m.get(key)
            if not isinstance(im, dict):
                continue
            sel = im.get("file") or im.get("name")     # 附件用落盘名 file，图片用 name
            name = os.path.basename(str(sel or ""))
            if name:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass


def delete_session_uploads(sid):
    shutil.rmtree(_upload_dir(sid), ignore_errors=True)


def sweep_uploads():
    """启动时清理孤儿附件：会话已删除的目录整目录删除，会话还在的删掉没被任何消息引用的文件。

    测试注意：如果端到端脚本把 SESSION_FILE 指向了临时文件，必须同时设置
    ``GM_NO_SWEEP=1``（或隔离 UPLOAD_DIR），否则这里会把真实附件目录当成孤儿删掉。
    """
    if os.environ.get("GM_NO_SWEEP"):
        return
    if not os.path.isdir(UPLOAD_DIR):
        return
    alive = {}                                     # 目录名 -> 该会话仍被引用的文件名集合
    try:
        data = load_sessions()
    except Exception:
        return
    for s in (data or {}).get("sessions") or []:
        if not isinstance(s, dict):
            continue
        keep = set()
        for m in s.get("messages") or []:
            if not isinstance(m, dict):
                continue
            for key in ("image", "doc"):
                im = m.get(key)
                if not isinstance(im, dict):
                    continue
                sel = im.get("file") or im.get("name")
                nm = os.path.basename(str(sel or ""))
                if nm:
                    keep.add(nm)
        alive[os.path.basename(_upload_dir(s.get("id")))] = keep
    removed_dirs = removed_files = 0
    for dname in os.listdir(UPLOAD_DIR):
        dpath = os.path.join(UPLOAD_DIR, dname)
        if not os.path.isdir(dpath):
            continue
        if dname not in alive:
            shutil.rmtree(dpath, ignore_errors=True)
            removed_dirs += 1
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
    if removed_dirs or removed_files:
        print(f"[附件清理] 删除孤儿目录 {removed_dirs} 个、无用文件 {removed_files} 个")
