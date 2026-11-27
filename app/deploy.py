# -*- coding: utf-8 -*-
"""本地部署 / 发布：把沙箱里的静态网页挂到内置静态服务器上，给别人一个可点的链接。

对齐 Trae 的「部署」体验（选择目录 → 发布 → 得到 URL → 预览卡片），但托管在本机：
- 只复制**发布用文件**到 data/deploy/sites/<id>/public，不把整个沙箱（含 .env）暴露出去；
- 服务器只做静态文件，禁目录列表、禁隐藏文件、路径越界直接 404；
- 同时给出 127.0.0.1 与局域网地址（手机同网可访问）；不涉及任何外部平台与账号。
"""
import json
import os
import re
import shutil
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_BASE_PORT = 7801
_PORT_SPAN = 60
MAX_FILES = 500
MAX_BYTES = 50 * 1024 * 1024
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
              ".idea", ".vscode", ".next", ".cache"}
_SKIP_EXT = {".map", ".pyc", ".pyo", ".log", ".tmp", ".part"}

_LOCK = threading.Lock()
_SITES = {}                  # id -> site dict（含 source/public/port/name）
_SERVERS = {}                # id -> ThreadingHTTPServer


def _data_dir():
    from app.store import DATA_DIR
    return DATA_DIR


def _deploy_dir():
    d = os.path.join(_data_dir(), "deploy")
    os.makedirs(os.path.join(d, "sites"), exist_ok=True)
    return d


def _reg_path():
    return os.path.join(_deploy_dir(), "sites.json")


def _load():
    try:
        with open(_reg_path(), "r", encoding="utf-8") as f:
            rows = json.load(f)
        for it in rows if isinstance(rows, list) else []:
            if isinstance(it, dict) and it.get("id"):
                _SITES[str(it["id"])] = it
    except (OSError, ValueError):
        pass


def _save():
    try:
        tmp = _reg_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(_SITES.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, _reg_path())
    except OSError:
        pass


def _slug(name):
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", str(name or "")).strip("-")
    return (s or "site")[:40].lower()


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""


def _free_port(preferred=0):
    if preferred:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", int(preferred)))
            return int(preferred)
        except (OSError, ValueError):
            pass
    for port in range(_BASE_PORT, _BASE_PORT + _PORT_SPAN):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    raise RuntimeError("本地端口 %d–%d 都被占用，无法启动部署服务"
                       % (_BASE_PORT, _BASE_PORT + _PORT_SPAN - 1))


def _ignore(src, names):
    out = []
    for n in names:
        if n.startswith(".") or n in _SKIP_DIRS:
            out.append(n)
        elif os.path.splitext(n)[1].lower() in _SKIP_EXT:
            out.append(n)
    return out


def _copy_public(src, dst):
    """复制发布文件；返回 (文件数, 字节数)。只保留普通文件，符号链接一律跳过。"""
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    files = 0
    total = 0
    for base, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d not in _SKIP_DIRS
                   and not os.path.islink(os.path.join(base, d))]
        rel = os.path.relpath(base, src)
        out_dir = dst if rel == "." else os.path.join(dst, rel)
        for n in names:
            if n.startswith(".") or os.path.splitext(n)[1].lower() in _SKIP_EXT:
                continue
            sp = os.path.join(base, n)
            if os.path.islink(sp) or not os.path.isfile(sp):
                continue
            dst_p = os.path.join(out_dir, n)
            os.makedirs(os.path.dirname(dst_p), exist_ok=True)
            try:
                shutil.copyfile(sp, dst_p)
            except OSError:
                continue
            files += 1
            try:
                total += os.path.getsize(dst_p)
            except OSError:
                pass
            if files > MAX_FILES or total > MAX_BYTES:
                raise ValueError("发布内容过大：最多 %d 个文件 / %d MB（含隐藏文件与地图文件已跳过）"
                                 % (MAX_FILES, MAX_BYTES // (1024 * 1024)))
    return files, total


def _handler(root):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def list_directory(self, path):
            self.send_error(404, "Not Found")
            return None

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *args):        # 发布服务器不刷控制台
            pass

    return H


def _start_server(site):
    sid = site["id"]
    srv = _SERVERS.get(sid)
    if srv is not None:
        return srv
    public = site["public"]
    if not os.path.isdir(public):
        raise RuntimeError("发布目录不存在：%s" % public)
    srv = ThreadingHTTPServer(("0.0.0.0", int(site["port"])), _handler(public))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, name="deploy-" + sid, daemon=True)
    t.start()
    _SERVERS[sid] = srv
    return srv


def _site_view(site):
    port = int(site.get("port") or 0)
    lan = _lan_ip()
    out = dict(site)
    out["running"] = site["id"] in _SERVERS
    out["url"] = "http://127.0.0.1:%d/" % port
    out["lan_url"] = ("http://%s:%d/" % (lan, port)) if lan else ""
    return out


def publish(src, name="", port=0, source_label=""):
    """发布一个目录；同一来源重复发布会原地更新（对齐 Trae 的重新部署）。"""
    src = os.path.realpath(str(src or ""))
    if not os.path.isdir(src):
        raise ValueError("来源目录不存在：%s" % src)
    with _LOCK:
        if not _SITES:
            _load()
        hit = next((s for s in _SITES.values()
                    if os.path.realpath(s.get("source") or "") == src), None)
        label = str(name or "").strip() or os.path.basename(src.rstrip("\\/")) or "site"
        sid = hit["id"] if hit else _slug(label)
        if not hit:
            n = 2
            while sid in _SITES:
                sid = "%s-%d" % (_slug(label), n)
                n += 1
        public = os.path.join(_deploy_dir(), "sites", sid, "public")
        files, total = _copy_public(src, public)
        has_index = os.path.isfile(os.path.join(public, "index.html"))
        if not files:
            raise ValueError("来源目录里没有可发布的文件（已跳过隐藏文件 / .git / node_modules）")
        now = time.time()
        site = hit or {"id": sid, "created_at": now, "source_label": source_label or src}
        # 已在运行的站点保持原端口（探测会撞上自己的监听，导致每次重发都换 URL）
        keep_port = int(site.get("port") or 0) if sid in _SERVERS else 0
        site.update({
            "name": label,
            "source": src,
            "source_label": source_label or site.get("source_label") or src,
            "public": public,
            "port": int(port) or keep_port or _free_port(int(site.get("port") or 0)),
            "updated_at": now,
            "files": files,
            "bytes": total,
            "has_index": has_index,
        })
        _SITES[sid] = site
        _save()
        # 端口换了要重启旧服务
        old = _SERVERS.pop(sid, None)
        if old is not None:
            try:
                old.shutdown()
                old.server_close()
            except Exception:            # noqa: BLE001
                pass
        _start_server(site)
        return _site_view(site)


def list_sites():
    with _LOCK:
        if not _SITES:
            _load()
        return [_site_view(s) for s in
                sorted(_SITES.values(), key=lambda x: x.get("updated_at") or 0, reverse=True)]


def stop(site_id):
    sid = str(site_id or "").strip()
    with _LOCK:
        if not _SITES:
            _load()
        if sid and sid not in _SITES:
            # 允许用名字 / 前缀指定，省得让模型背 id
            cand = [s for s in _SITES.values()
                    if s.get("id") == sid or s.get("name") == sid]
            sid = cand[0]["id"] if cand else ""
        srv = _SERVERS.pop(sid, None)
        if srv is None:
            return {"ok": False, "error": "站点没有在运行：" + (sid or "（空）")}
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:                # noqa: BLE001
            pass
        return {"ok": True, "id": sid}


def stop_all():
    for sid in list(_SERVERS):
        stop(sid)
