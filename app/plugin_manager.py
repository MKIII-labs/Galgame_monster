# -*- coding: utf-8 -*-
"""插件管理器：Python 工具插件的安装 / 卸载 / 启停，以及 MCP 服务器的摘要与开关。

插件形态（都放在 data/plugins/）：
- 目录包：`data/plugins/<id>/plugin.json` + `entry`（默认 plugin.py）
- 老式单文件：`data/plugins/<id>.py`

安全边界：
- 插件是任意 Python 代码，安装/启用都必须由用户在界面上确认；默认总开关关闭。
- 安装只接受：内置源、本地目录、zip、https Git 仓库；不执行任何安装脚本，
  也不跟随 zip 里的软链 / 越界路径。
- 启停与安装都只写文件 + config，运行时加载需要重启进程。
"""
import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from app import capabilities
from app.config import load_config, save_config
from app.store import RESOURCES_DIR, ROOT

PLUGIN_DIR = capabilities.PLUGIN_DIR
BUILTIN_DIR = os.path.join(RESOURCES_DIR, "plugins")
CATALOG_FILE = os.path.join(BUILTIN_DIR, "catalog.json")

MAX_FILES = 300
MAX_BYTES = 20 * 1024 * 1024
README_LIMIT = 8000
GIT_TIMEOUT = 90

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _rel(path):
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path


def _safe_id(pid):
    s = str(pid or "").strip()
    return s if _ID_RE.match(s) else ""


def _inside_plugin_dir(path):
    root = os.path.normpath(PLUGIN_DIR)
    p = os.path.normpath(os.path.abspath(path))
    return p == root or p.startswith(root + os.sep)


def _read_manifest(dir_path):
    path = os.path.join(dir_path, "plugin.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f) or {}
    except Exception:                        # noqa: BLE001
        return None
    return meta if isinstance(meta, dict) else None


def _doc_summary(path):
    """单文件插件没有 manifest：用模块 docstring 第一行当描述，不执行代码。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(20000))
        doc = ast.get_docstring(tree) or ""
        return doc.strip().splitlines()[0][:120] if doc.strip() else ""
    except Exception:                        # noqa: BLE001
        return ""


def _entry_of(dir_path, meta):
    rel = str((meta or {}).get("entry") or "plugin.py").replace("\\", "/").lstrip("/")
    root = os.path.normpath(dir_path)
    target = os.path.normpath(os.path.join(dir_path, rel))
    if not target.startswith(root + os.sep) or not target.lower().endswith(".py"):
        return ""
    return target if os.path.isfile(target) else ""


def _item(pid, path, meta, loaded, disabled):
    meta = meta or {}
    enabled = pid not in disabled
    if not enabled:
        status, note = "disabled", "已停用"
    elif loaded.get(pid, {}).get("ok"):
        status, note = "loaded", "已加载"
    elif loaded.get(pid):
        status, note = "error", loaded[pid].get("error") or "加载失败"
    else:
        status, note = "pending", "未加载（重启后生效）"
    entry = _entry_of(path, meta) if os.path.isdir(path) else path
    return {
        "id": pid,
        "name": str(meta.get("name") or pid),
        "description": str(meta.get("description") or ("" if os.path.isdir(path) else _doc_summary(path))),
        "version": str(meta.get("version") or ""),
        "author": str(meta.get("author") or ""),
        "homepage": str(meta.get("homepage") or ""),
        "permissions": [str(x) for x in (meta.get("permissions") or [])][:12],
        "enabled": enabled,
        "status": status,
        "status_text": note,
        "path": _rel(path),
        "entry": _rel(entry) if entry else "",
        "kind": "package" if os.path.isdir(path) else "file",
    }


def list_installed():
    cfg = load_config()
    master = bool(cfg.get("plugins_enabled", False))
    disabled = set(cfg.get("plugins_disabled") or [])
    if master:
        try:
            capabilities.ids()               # 触发一次加载，拿到本次进程的真实状态
        except Exception:                    # noqa: BLE001
            pass
    loaded = capabilities.loaded_plugins()
    rows = []
    if not os.path.isdir(PLUGIN_DIR):
        return {"enabled": master, "plugins": [], "dir": PLUGIN_DIR}
    for entry in sorted(os.listdir(PLUGIN_DIR)):
        if entry.startswith("_"):
            continue
        full = os.path.join(PLUGIN_DIR, entry)
        if os.path.isfile(full) and entry.lower().endswith(".py"):
            pid = os.path.splitext(entry)[0]
            if not _safe_id(pid):
                continue
            rows.append(_item(pid, full, {}, loaded, disabled))
            continue
        if not os.path.isdir(full):
            continue
        meta = _read_manifest(full)
        if meta is None:
            continue
        pid = _safe_id(entry)
        if not pid:
            continue
        rows.append(_item(pid, full, meta, loaded, disabled))
    return {"enabled": master, "plugins": rows, "dir": PLUGIN_DIR}


def detail(pid):
    s = _safe_id(pid)
    if not s:
        return None
    folder = os.path.join(PLUGIN_DIR, s)
    file_path = os.path.join(PLUGIN_DIR, s + ".py")
    if os.path.isdir(folder):
        base, meta = folder, _read_manifest(folder) or {}
    elif os.path.isfile(file_path):
        base, meta = file_path, {}
    else:
        return None
    item = _item(s, base, meta, capabilities.loaded_plugins(),
                 set(load_config().get("plugins_disabled") or []))
    readme = ""
    if os.path.isdir(base):
        for name in ("README.md", "readme.md", "README.txt"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        readme = f.read(README_LIMIT)
                except OSError:
                    readme = ""
                break
        try:
            item["files"] = sorted(os.listdir(base))[:40]
        except OSError:
            item["files"] = []
    item["readme"] = readme
    return item


def set_enabled(pid, on):
    s = _safe_id(pid)
    if not s:
        return "插件 id 不合法"
    cfg = load_config()
    off = set(cfg.get("plugins_disabled") or [])
    if on:
        off.discard(s)
    else:
        off.add(s)
    cfg["plugins_disabled"] = sorted(off)
    save_config(cfg)
    return ""


def set_master(on):
    cfg = load_config()
    cfg["plugins_enabled"] = bool(on)
    save_config(cfg)
    return ""


def _scan(src):
    files = total = 0
    for dirpath, _dirs, names in os.walk(src):
        for fn in names:
            p = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(p)
            except OSError:
                continue
            files += 1
            if files > MAX_FILES:
                return "文件太多（超过 %d 个）" % MAX_FILES
            if total > MAX_BYTES:
                return "体积太大（超过 %d MB）" % (MAX_BYTES // 1024 // 1024)
    return ""


def _package_root(src):
    """找到真正的插件根：允许 zip 里多包一层目录。"""
    if os.path.isfile(os.path.join(src, "plugin.json")):
        return src
    try:
        subs = [os.path.join(src, d) for d in os.listdir(src)
                if os.path.isdir(os.path.join(src, d)) and not d.startswith(".")]
    except OSError:
        return ""
    subs = [d for d in subs if os.path.isfile(os.path.join(d, "plugin.json"))]
    return subs[0] if len(subs) == 1 else ""


def install_dir(src):
    """从本地目录安装一个目录包。返回 (id, 错误)。"""
    root = _package_root(os.path.abspath(str(src or "")))
    if not root:
        return None, "目录里没有 plugin.json"
    meta = _read_manifest(root)
    if meta is None:
        return None, "plugin.json 解析失败"
    err = _scan(root)
    if err:
        return None, err
    if not _entry_of(root, meta):
        return None, "plugin.json 的 entry 指向的文件不存在"
    pid = _safe_id(meta.get("id") or os.path.basename(root))
    if not pid:
        return None, "插件 id 不合法（只能用字母 / 数字 / _ / -）"
    dst = os.path.join(PLUGIN_DIR, pid)
    if os.path.exists(dst) or os.path.exists(dst + ".py"):
        return None, "已经安装过同名插件：%s" % pid
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        shutil.copytree(root, dst)
    except OSError as e:
        return None, "安装失败：%s" % e
    _enable_after_install(pid)
    return pid, ""


def install_file(path):
    """安装老式单文件插件 data/plugins/<id>.py。"""
    src = os.path.abspath(str(path or ""))
    if not os.path.isfile(src) or not src.lower().endswith(".py"):
        return None, "只支持 .py 单文件插件"
    pid = _safe_id(os.path.splitext(os.path.basename(src))[0])
    if not pid:
        return None, "插件文件名不合法（只能用字母 / 数字 / _ / -）"
    dst = os.path.join(PLUGIN_DIR, pid + ".py")
    if os.path.exists(dst) or os.path.exists(os.path.join(PLUGIN_DIR, pid)):
        return None, "已经安装过同名插件：%s" % pid
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        return None, "安装失败：%s" % e
    _enable_after_install(pid)
    return pid, ""


def _enable_after_install(pid):
    cfg = load_config()
    off = [x for x in (cfg.get("plugins_disabled") or []) if x != pid]
    cfg["plugins_disabled"] = off
    save_config(cfg)


def install_zip(path):
    src = os.path.abspath(str(path or ""))
    if not os.path.isfile(src) or not src.lower().endswith(".zip"):
        return None, "只支持 .zip 插件包"
    tmp = tempfile.mkdtemp(prefix="gm-plugin-")
    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES:
                return None, "压缩包文件太多（超过 %d 个）" % MAX_FILES
            total = sum(int(i.file_size or 0) for i in infos)
            if total > MAX_BYTES:
                return None, "压缩包解压后太大（超过 %d MB）" % (MAX_BYTES // 1024 // 1024)
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    return None, "压缩包里有越界路径：%s" % info.filename
                if info.is_dir():
                    continue
                dest = os.path.join(tmp, *name.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as rf, open(dest, "wb") as wf:
                    shutil.copyfileobj(rf, wf)
        return install_dir(tmp)
    except (zipfile.BadZipFile, OSError) as e:
        return None, "解压失败：%s" % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_git(url):
    raw = str(url or "").strip()
    if not re.match(r"^https://[A-Za-z0-9.-]+/", raw) or "@" in raw.split("://", 1)[-1].split("/")[0]:
        return None, "只支持 https:// 的公开 Git 地址"
    tmp = tempfile.mkdtemp(prefix="gm-plugin-git-")
    try:
        p = subprocess.run(["git", "clone", "--depth", "1", "--", raw, tmp],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=GIT_TIMEOUT)
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()[:300]
            return None, "git clone 失败：%s" % (err or p.returncode)
        return install_dir(tmp)
    except subprocess.TimeoutExpired:
        return None, "git clone 超时"
    except OSError as e:
        return None, "git 不可用：%s" % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_builtin(pid):
    s = _safe_id(pid)
    if not s:
        return None, "插件 id 不合法"
    src = os.path.join(BUILTIN_DIR, s)
    if not os.path.isfile(os.path.join(src, "plugin.json")):
        return None, "内置源里没有这个插件"
    return install_dir(src)


def uninstall(pid):
    s = _safe_id(pid)
    if not s:
        return "插件 id 不合法"
    folder = os.path.join(PLUGIN_DIR, s)
    file_path = os.path.join(PLUGIN_DIR, s + ".py")
    target = folder if os.path.isdir(folder) else (file_path if os.path.isfile(file_path) else "")
    if not target:
        return "没有安装这个插件"
    if not _inside_plugin_dir(target):
        return "拒绝删除：路径不在插件目录下"
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    except OSError as e:
        return "删除失败：%s" % e
    cfg = load_config()
    cfg["plugins_disabled"] = [x for x in (cfg.get("plugins_disabled") or []) if x != s]
    save_config(cfg)
    return ""


def catalog():
    data = {}
    if os.path.isfile(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:                    # noqa: BLE001
            data = {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    installed = {p["id"] for p in list_installed()["plugins"]}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = _safe_id(it.get("id"))
        if not pid:
            continue
        src = os.path.join(BUILTIN_DIR, pid)
        if not os.path.isfile(os.path.join(src, "plugin.json")):
            continue
        out.append({
            "id": pid,
            "name": str(it.get("name") or pid),
            "description": str(it.get("description") or ""),
            "version": str(it.get("version") or ""),
            "author": str(it.get("author") or ""),
            "permissions": [str(x) for x in (it.get("permissions") or [])][:12],
            "installed": pid in installed,
        })
    return out


# ------------------------------------------------------------
# MCP（只读摘要 + 开关；新增 / 编辑仍在设置里）
# ------------------------------------------------------------
def mcp_summary():
    cfg = load_config()
    servers = []
    for s in cfg.get("mcp_servers") or []:
        if not isinstance(s, dict):
            continue
        servers.append({
            "name": str(s.get("name") or ""),
            "transport": str(s.get("transport") or "stdio"),
            "command": str(s.get("command") or ""),
            "url": str(s.get("url") or ""),
            "enabled": s.get("enabled", True) is not False,
        })
    return {"enabled": bool(cfg.get("mcp_enabled", False)),
            "tool_mode": str(cfg.get("mcp_tool_mode") or "lazy"),
            "servers": servers}


def mcp_set_master(on):
    cfg = load_config()
    cfg["mcp_enabled"] = bool(on)
    save_config(cfg)
    _mcp_reset()
    return ""


def mcp_set_enabled(name, on):
    cfg = load_config()
    hit = False
    for s in cfg.get("mcp_servers") or []:
        if isinstance(s, dict) and str(s.get("name") or "") == str(name):
            s["enabled"] = bool(on)
            hit = True
    if not hit:
        return "没有这个 MCP 服务器"
    save_config(cfg)
    _mcp_reset()
    return ""


def _mcp_reset():
    try:
        from app import mcp
        mcp.reset()
    except Exception:                        # noqa: BLE001
        pass
