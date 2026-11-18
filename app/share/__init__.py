# -*- coding: utf-8 -*-
"""分享：产出物（自包含 HTML / .gmshare 包 / 长图 PNG），而不是「托管链接」。

旧的 `data/shares/<id>.html` + `/share/<id>` 仍然保留：它只用于**本机预览**。
对外分发一律走 `export()` 产出的文件。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from app import winproc
from app.store import DATA_DIR

from . import package as pkg

SHARE_DIR = os.path.join(DATA_DIR, "shares")          # 内部预览页（不面向用户）
IMPORT_DIR = os.path.join(SHARE_DIR, "imported")
_LEGACY_EXPORT_DIR = os.path.join(SHARE_DIR, "exports")

_DOWNLOADS_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _downloads_dir():
    """用户「下载」文件夹：优先读注册表（用户可能把它移到别的盘）。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            raw, _type = winreg.QueryValueEx(k, _DOWNLOADS_GUID)
        path = os.path.expandvars(str(raw or ""))
        if path and os.path.isdir(path):
            return path
    except (OSError, ImportError, ValueError):
        pass
    home = os.path.expanduser("~")
    for name in ("Downloads", "下载"):
        cand = os.path.join(home, name)
        if os.path.isdir(cand):
            return cand
    return home


# 导出产物放在「下载」里，不塞进项目目录：用户点开就能找到。
DEFAULT_EXPORT_DIR = os.path.join(_downloads_dir(), "Galgame Monster 分享")
EXPORT_DIR = DEFAULT_EXPORT_DIR

MAX_HTML = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,40}$")
_EXPORT_RE = re.compile(r"^sh_[a-f0-9]{8,16}\.(?:html|png|gmshare)$")
_TITLE_RE = re.compile(r"<title[^>]*>([\s\S]{0,200}?)</title>", re.I)

APP_VERSION = "3.5"


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


def _migrate_legacy_exports():
    """老版本把导出放在 data/shares/exports；搬到下载目录，避免散落在项目里。"""
    if os.path.abspath(EXPORT_DIR) != os.path.abspath(DEFAULT_EXPORT_DIR):
        return                      # 测试 / 自定义目录不迁移
    try:
        names = os.listdir(_LEGACY_EXPORT_DIR)
    except OSError:
        return
    for n in names:
        if not _EXPORT_RE.match(n):
            continue
        dst = os.path.join(EXPORT_DIR, n)
        if os.path.exists(dst):
            continue
        try:
            # 项目在 D:、下载常在 C:，os.replace 跨盘会失败，必须用 shutil.move
            shutil.move(os.path.join(_LEGACY_EXPORT_DIR, n), dst)
        except OSError:
            pass
    try:
        os.rmdir(_LEGACY_EXPORT_DIR)        # 只剩空目录时顺手清掉
    except OSError:
        pass


def ensure_export_dir():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    _migrate_legacy_exports()
    return EXPORT_DIR


def export_path(name):
    """按导出名返回绝对路径；非法名 / 文件不存在返回空串。"""
    n = str(name or "")
    if not _EXPORT_RE.match(n):
        return ""
    path = os.path.join(EXPORT_DIR, n)
    return path if os.path.isfile(path) else ""


_EXT_OF = {"html": ".html", "png": ".png", "package": ".gmshare"}
_FILETYPES_OF = {
    "html": [["HTML 文件", "*.html"]],
    "png": [["PNG 图片", "*.png"]],
    "package": [["分享包", "*.gmshare"]],
}
_PICK_MARK = "__GM_PICK__"
_PICK_LOCK = threading.Lock()

# 与「选择 Agent 沙箱目录」同一套做法：独立 Python 子进程 + 先声明 per-monitor DPI
# 感知，窗口才不会在缩放屏上发虚；也不经过 PowerShell，不会触发系统安全警告。
_SAVE_PICKER_CODE = (
    "import sys, json, ctypes\n"
    "try: ctypes.windll.shcore.SetProcessDpiAwareness(2)\n"
    "except Exception: pass\n"
    "try: ctypes.windll.user32.SetProcessDPIAware()\n"
    "except Exception: pass\n"
    "import tkinter as tk\n"
    "from tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw()\n"
    "try: r.attributes('-topmost', True)\n"
    "except Exception: pass\n"
    "conf = json.loads(sys.argv[3])\n"
    "p = filedialog.asksaveasfilename(initialdir=sys.argv[1] or None,\n"
    "    initialfile=sys.argv[2] or '', title=conf['title'],\n"
    "    defaultextension=conf['ext'], filetypes=conf['types']) or ''\n"
    "sys.stdout.write('\\n" + _PICK_MARK + "' + p + '\\n')\n"
    "sys.stdout.flush()\n"
)


def default_name(fmt="html"):
    return "分享-%s%s" % (time.strftime("%Y%m%d-%H%M%S"), _EXT_OF.get(fmt, ".html"))


def ask_save_path(default_file, fmt="html"):
    """弹出系统「另存为」窗口（与选沙箱目录同一个 Tk 选择框）。

    返回：选中路径 / ""（用户取消或已有窗口开着）/ None（窗口没打开成功，调用方走默认目录）。
    """
    if not _PICK_LOCK.acquire(blocking=False):
        return ""                       # 已经有一个选择框开着，别叠第二个
    try:
        start = EXPORT_DIR if os.path.isdir(EXPORT_DIR) else _downloads_dir()
        conf = json.dumps({"title": "保存分享文件",
                           "ext": _EXT_OF.get(fmt, ".html"),
                           "types": _FILETYPES_OF.get(fmt, _FILETYPES_OF["html"])},
                          ensure_ascii=False)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _SAVE_PICKER_CODE, start, default_file, conf],
                capture_output=True, text=True, timeout=600,
                creationflags=winproc.no_window())
        except subprocess.TimeoutExpired:
            print("[分享] 另存为窗口超时，按取消处理")
            return ""
        except Exception as e:          # noqa: BLE001
            print("[分享] 另存为窗口打开失败：%s" % e)
            return None
        # 本机有些软件会往子进程 stdout 打日志：只认带标记的那一行
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith(_PICK_MARK):
                path = line[len(_PICK_MARK):].strip().strip('"').strip("'")
                return os.path.abspath(path) if path else ""
        return None
    finally:
        _PICK_LOCK.release()


def _save_target(path, fmt):
    """补全扩展名并确保目录存在；返回绝对路径。"""
    target = os.path.abspath(str(path))
    ext = _EXT_OF.get(fmt, ".html")
    if not target.lower().endswith(ext):
        target += ext
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return target


def export(payload, fmt="html", save_path=""):
    """生成产物文件，返回 {ok, name, path, size, kind, report, saved_to?}。

    save_path 非空时在生成后再复制一份到用户选择的位置（导出记录仍保留在导出目录）。
    """
    fmt = str(fmt or "html").lower()
    html, rep, sections, title = build_document(payload)
    ensure_export_dir()
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
    source = os.path.join(EXPORT_DIR, name)
    result = {"ok": True, "name": name, "path": source,
              "size": int(size or 0), "kind": fmt, "report": report}
    if save_path:
        try:
            target = _save_target(save_path, fmt)
            shutil.copy2(source, target)
            result["saved_to"] = target
        except OSError as e:
            result["saved_to"] = ""
            result["save_error"] = "复制到你选择的位置失败：%s" % e
    return result


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
