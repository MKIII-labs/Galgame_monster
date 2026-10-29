# -*- coding: utf-8 -*-
"""全量数据备份 / 预检 / 恢复。

设计取舍：
- 备份是一个 ZIP：manifest.json + payload/ 下的用户数据；
- 只打包用户数据，不打包 config.json（含 API Key）、浏览器 profile、vendor；
- SQLite 用 sqlite3.backup() 生成一致性快照，避免直接复制 WAL 状态；
- 恢复前先生成一份安全备份，预检通过后才覆盖；
- 预检与恢复都校验 manifest 中的文件大小和 SHA-256。
"""
import glob
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile

from app import knowledge, store
from app.db import DB_FILE, get_db

APP_NAME = "Galgame_monster"
FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 512 * 1024 * 1024       # 解压后总大小上限，防止 zip bomb

_FILE_NAMES = ("sessions.json", "tasks.json", "user_profile.json",
               "chat_history.txt", "token_usage.jsonl")
_DIR_NAMES = ("knowledge", "notes", "uploads", "saves")


def _root(data_dir=None):
    return os.path.abspath(data_dir or store.DATA_DIR)


def _db_path(root, db_file=None):
    return os.path.abspath(db_file or os.path.join(root, "assistant.db"))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _json_count(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    value = data.get(key)
    return len(value) if isinstance(value, list) else 0


def _kb_count(root):
    index = os.path.join(root, "knowledge", "index.json")
    try:
        with open(index, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("kbs"), list):
            return len(data["kbs"])
    except Exception:
        pass
    return len([p for p in glob.glob(os.path.join(root, "knowledge", "*.json"))
                if os.path.basename(p) != "index.json"])


def _counts(root):
    sessions = _json_count(os.path.join(root, "sessions.json"), "sessions")
    tasks = _json_count(os.path.join(root, "tasks.json"), "items")
    return {
        "sessions": sessions,
        "tasks": tasks,
        "knowledge": _kb_count(root),
        "notes": len(glob.glob(os.path.join(root, "notes", "**", "*.md"), recursive=True)),
        "uploads": sum(1 for _ in glob.iglob(os.path.join(root, "uploads", "**", "*"),
                                             recursive=True)
                       if os.path.isfile(_)),
        "saves": (len(glob.glob(os.path.join(root, "saves", "*.json")))
                  + len(glob.glob(os.path.join(root, "saves", "*", "index.json")))),
    }


def _sqlite_snapshot(db_file):
    """生成 SQLite 一致性快照；数据库不存在时返回 None。"""
    if not os.path.isfile(db_file):
        return None
    tmp = tempfile.NamedTemporaryFile(prefix="gm-backup-", suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return _read_bytes(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _zip_add(zf, path, data, files):
    info = zipfile.ZipInfo(path, time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)
    files.append({"path": path, "size": len(data), "sha256": _sha256(data)})


def export_backup(data_dir=None, db_file=None):
    """返回 (zip bytes, 文件名)。"""
    root = _root(data_dir)
    dbf = _db_path(root, db_file)
    out = io.BytesIO()
    files = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _FILE_NAMES:
            data = _read_bytes(os.path.join(root, name))
            if data is not None:
                _zip_add(zf, "payload/" + name, data, files)
        for name in _DIR_NAMES:
            src_dir = os.path.join(root, name)
            if not os.path.isdir(src_dir):
                continue
            # 空目录也写入，恢复时才能正确清空
            _zip_add(zf, "payload/%s/" % name, b"", files)
            for dirpath, dirnames, filenames in os.walk(src_dir):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                for fn in sorted(filenames):
                    if fn.endswith(".tmp"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    data = _read_bytes(full)
                    if data is not None:
                        _zip_add(zf, "payload/" + rel, data, files)
        db_data = _sqlite_snapshot(dbf)
        if db_data is not None:
            _zip_add(zf, "payload/assistant.db", db_data, files)
        manifest = {
            "app": APP_NAME,
            "format": FORMAT_VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "counts": _counts(root),
            "files": files,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    name = "galgame-backup-%s.zip" % time.strftime("%Y%m%d-%H%M%S")
    return out.getvalue(), name


def inspect_backup(data):
    """只读预检；返回 manifest 与统计。校验失败抛 ValueError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise ValueError("不是有效的 ZIP 备份：%s" % e)
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as e:
            raise ValueError("备份缺少 manifest.json：%s" % e)
        if not isinstance(manifest, dict) or manifest.get("app") != APP_NAME:
            raise ValueError("不是本程序的备份文件")
        if int(manifest.get("format") or 0) != FORMAT_VERSION:
            raise ValueError("备份格式版本不支持")
        entries = {i.filename: i for i in zf.infolist()}
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_BACKUP_BYTES:
            raise ValueError("备份解压后超过 %d MB，已拒绝" % (MAX_BACKUP_BYTES // 1024 // 1024))
        files = manifest.get("files") or []
        if not isinstance(files, list):
            raise ValueError("manifest.files 格式错误")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("manifest.files 条目格式错误")
            path = str(item.get("path") or "")
            if not path.startswith("payload/") or ".." in path.split("/"):
                raise ValueError("备份路径不合法：%s" % path)
            info = entries.get(path)
            if info is None:
                raise ValueError("备份缺少文件：%s" % path)
            raw = zf.read(path)
            if len(raw) != int(item.get("size") or 0) or _sha256(raw) != item.get("sha256"):
                raise ValueError("文件校验失败：%s" % path)
        return {
            "manifest": manifest,
            "summary": manifest.get("counts") or {},
            "files": len(files),
        }


def _safe_extract(data, root):
    """把 payload/ 解到临时目录；返回临时目录路径。"""
    tmp = tempfile.mkdtemp(prefix=".restore-", dir=root)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith("payload/"):
                continue
            rel = name[len("payload/"):]
            if not rel:
                continue
            parts = [p for p in rel.split("/") if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                raise ValueError("备份路径不合法：%s" % name)
            target = os.path.join(tmp, *parts)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return tmp


def _replace_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".restore.tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _replace_dir(src, dst):
    if not os.path.isdir(src):
        return
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    elif os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)


def _clear_caches(root):
    for name in ("sessions.json", "tasks.json", "user_profile.json"):
        store.drop_cache(os.path.join(root, name))
    for path in glob.glob(os.path.join(root, "knowledge", "*.json")):
        try:
            knowledge.drop_cache(path)
        except Exception:
            pass


def restore_backup(data, data_dir=None, db_file=None, make_safety=True):
    """预检后覆盖恢复；返回预检摘要与安全备份路径。"""
    info = inspect_backup(data)
    root = _root(data_dir)
    dbf = _db_path(root, db_file)
    safety = ""
    if make_safety:
        blob, _name = export_backup(root, dbf)
        os.makedirs(os.path.join(root, "backups"), exist_ok=True)
        safety = os.path.join(root, "backups", "pre-restore-%s.zip"
                              % time.strftime("%Y%m%d-%H%M%S"))
        with open(safety, "wb") as f:
            f.write(blob)
    tmp = _safe_extract(data, root)
    try:
        for name in _FILE_NAMES:
            src = os.path.join(tmp, name)
            if os.path.isfile(src):
                _replace_file(src, os.path.join(root, name))
        for name in _DIR_NAMES:
            _replace_dir(os.path.join(tmp, name), os.path.join(root, name))
        db_src_path = os.path.join(tmp, "assistant.db")
        if os.path.isfile(db_src_path):
            if db_file:
                dst = sqlite3.connect(dbf)
            else:
                dst = get_db()
            src = sqlite3.connect(db_src_path)
            try:
                src.backup(dst)
                try:
                    dst.commit()
                except Exception:
                    pass
            finally:
                src.close()
                if db_file:
                    dst.close()
        _clear_caches(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "summary": info["summary"], "safety": safety}
