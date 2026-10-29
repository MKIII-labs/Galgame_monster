# -*- coding: utf-8 -*-
"""pyright 语言服务器客户端（LSP over stdio）。

补的是「正则版轻量 LSP」的不足：`find_symbol` 只能按名字扫文本，不知道作用域、
类型、跨文件导入。这里直接挂 pyright-langserver，拿到真正的诊断 / 跳转定义 /
查找引用 / 重命名。

设计取舍：
- **一个沙箱一个会话**：LSP 是有状态的（服务端缓存文档与依赖图），每次调用重起
  进程会退化成「每次重新索引」。最多同时保留两个，超出按最久未用淘汰。
- **全量同步**：didChange 直接发整篇，不做增量。增量要维护 range 映射，出错时
  静默且难查；整篇同步在几十 KB 的文件上开销可以忽略。
- **懒启动**：第一次真正需要时才拉进程（npx 首次还要下载 pyright）。
- **失败隔离**：所有异常收敛成 Unavailable，绝不让 LSP 拖垮对话。
- **只做本机**：路径必须落在沙箱内；重命名返回「要改成什么」，写盘交给 agent
  （这样能复用撤销栈与写后自检）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

REQ_TIMEOUT = 25.0          # 单次请求超时（首次 initialize 单独放长）
START_TIMEOUT = 120.0       # npx 首次要下载 pyright，给足时间
DIAG_WAIT = 10.0            # 等 publishDiagnostics 推送
MAX_FILE_BYTES = 1024 * 1024
MAX_DIAG_FILES = 60         # 目录体检一次最多看几个文件
MAX_DIAG_ITEMS = 80
MAX_REFERENCES = 50
MAX_SESSIONS = 2

# 这些诊断是「本机没装第三方库」造成的噪音，回灌给模型只会让它去装包
_NOISE_CODES = (
    "reportMissingImports",
    "reportMissingModuleSource",
    "reportMissingTypeStubs",
)

# 首轮索引的排除项：不排掉 node_modules / venv，pyright 会在启动时扫几万个文件
_EXCLUDE = [
    "**/node_modules/**", "**/.git/**", "**/__pycache__/**",
    "**/venv/**", "**/.venv/**", "**/dist/**", "**/build/**",
]

_SESSIONS = {}
_SESS_LOCK = threading.Lock()


class Unavailable(Exception):
    """语言服务器不可用（没有 Node / 启动失败 / 请求超时 / 已熔断）。"""


# ---------------------------------------------------------------- 路径与启动

def _uri(path):
    return Path(os.path.abspath(path)).as_uri()


def _path_of(uri):
    """file:///d%3A/a/b.py -> d:\\a\\b.py"""
    try:
        u = urlparse(str(uri or ""))
    except ValueError:
        return ""
    if u.scheme != "file":
        return ""
    p = unquote(u.path or "")
    if os.name == "nt" and p.startswith("/") and len(p) > 2 and p[2] == ":":
        p = p[1:]
    return os.path.normpath(p)


def _diag_key(uri):
    """诊断缓存的键。

    pyright 会把 URI 规范化（盘符小写、冒号转义成 %3A），和我们发出去的字符串
    不一定逐字相同；所以统一落到「规范化后的绝对路径」再比对。
    """
    p = _path_of(uri)
    if not p:
        return str(uri or "")
    return os.path.normcase(os.path.abspath(p))


def _launcher():
    """返回启动命令。优先本机 pyright-langserver，否则用 npx 临时拉取。

    npm 官方源在部分网络下会 ECONNRESET，所以默认走 npmmirror；
    用户装了全局 pyright 时走本地，完全不联网。
    """
    for name in ("pyright-langserver.cmd", "pyright-langserver", "pyright-langserver.bat"):
        p = shutil.which(name)
        if p:
            return [p, "--stdio"]
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        return []
    return [npx, "--yes", "--registry=https://registry.npmmirror.com",
            "-p", "pyright", "pyright-langserver", "--stdio"]


def available():
    """有没有可用的启动方式（不代表能立刻起来：npx 首次要下载）。"""
    return bool(_launcher())


# ---------------------------------------------------------------- 会话

class Session:
    """一个 pyright-langserver 进程 + JSON-RPC 会话。"""

    def __init__(self, root, command=None):
        self.root = os.path.abspath(root)
        self.command = list(command or [])
        self.proc = None
        self._gen = 0
        self._seq = 0
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending = {}          # id -> {"ev": Event, "result": ..., "error": ...}
        self._diags = {}            # uri -> {"version": int, "items": [], "at": float}
        self._docs = {}             # abs path -> (version, text)
        self._stderr = []
        self._dead = ""
        self.last_used = time.time()

    # ---- 进程生命周期 ----

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        cmd = self.command or _launcher()
        if not cmd:
            raise Unavailable("找不到 Node / npx，无法启动 pyright-langserver")
        self._gen += 1
        gen = self._gen
        self._dead = ""
        self._stderr = []
        env = dict(os.environ)
        env.setdefault("npm_config_registry", "https://registry.npmmirror.com")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=self.root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, bufsize=0, creationflags=flags)
        except OSError as e:
            raise Unavailable("启动 pyright 失败：%s" % e)
        threading.Thread(target=self._read_loop, args=(gen,), daemon=True).start()
        threading.Thread(target=self._err_loop, args=(gen,), daemon=True).start()
        try:
            self._initialize()
        except Exception:
            self.stop()
            raise

    def stop(self):
        p = self.proc
        self.proc = None
        self._gen += 1                       # 让旧读线程自行退出
        if not p or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self._dead

    # ---- 收发 ----

    def _send(self, payload):
        if not self.proc or self.proc.poll() is not None:
            raise Unavailable("pyright 进程已退出%s" % (("：" + self._stderr[-1]) if self._stderr else ""))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        with self._write_lock:
            try:
                self.proc.stdin.write(head + body)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise Unavailable("写入 pyright 失败：%s" % e)

    def _read_loop(self, gen):
        stream = self.proc.stdout if self.proc else None
        while stream and gen == self._gen:
            try:
                msg = _read_frame(stream)
            except Exception as e:
                self._dead = "读取失败：%s" % e
                break
            if msg is None:
                self._dead = "连接已关闭"
                break
            if gen != self._gen:
                break
            self._dispatch(msg)

    def _err_loop(self, gen):
        stream = self.proc.stderr if self.proc else None
        while stream and gen == self._gen:
            try:
                line = stream.readline()
            except Exception:
                break
            if not line:
                break
            try:
                text = line.decode("utf-8", "replace").strip()
            except Exception:
                continue
            if text:
                self._stderr.append(text)
                del self._stderr[:-20]

    def _dispatch(self, msg):
        # 服务端发来的请求：必须回应，否则 pyright 会一直等
        if msg.get("method") and "id" in msg:
            method = msg.get("method")
            result = None
            if method == "workspace/configuration":
                items = (msg.get("params") or {}).get("items") or []
                result = [None] * len(items)
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except Unavailable:
                pass
            return
        if msg.get("method") == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            key = _diag_key(p.get("uri"))
            if key:
                self._diags[key] = {
                    "version": int(p.get("version") or 0),
                    "items": p.get("diagnostics") or [],
                    "at": time.time(),
                }
            return
        rid = msg.get("id")
        if rid is None:
            return                          # 其他通知（$/progress 等）直接丢
        with self._lock:
            slot = self._pending.get(rid)
        if not slot:
            return
        if "error" in msg:
            slot["error"] = msg.get("error")
        else:
            slot["result"] = msg.get("result")
        slot["ev"].set()

    def request(self, method, params, timeout=REQ_TIMEOUT):
        with self._lock:
            self._seq += 1
            rid = self._seq
            slot = {"ev": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        try:
            if not slot["ev"].wait(timeout):
                raise Unavailable("pyright 响应超时（%s，%.0fs）" % (method, timeout))
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if slot["error"]:
            raise Unavailable("pyright 返回错误（%s）：%s"
                              % (method, slot["error"].get("message") or slot["error"]))
        self.last_used = time.time()
        return slot["result"]

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- 初始化 ----

    def _initialize(self):
        root_uri = _uri(self.root)
        self.request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "galgame-monster"},
            "rootPath": self.root,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False, "didSave": False},
                    "publishDiagnostics": {"relatedInformation": True},
                    "definition": {"linkSupport": False},
                    "references": {},
                    "rename": {"prepareSupport": False},
                },
                "workspace": {"workspaceFolders": True, "applyEdit": False},
            },
            "workspaceFolders": [{
                "uri": root_uri,
                "name": os.path.basename(self.root) or "workspace",
            }],
            "initializationOptions": {
                "exclude": _EXCLUDE,
                "typeCheckingMode": "basic",
                "autoSearchPaths": True,
                "useLibraryCodeForTypes": True,
            },
        }, timeout=START_TIMEOUT)
        self.notify("initialized", {})

    # ---- 文档同步 ----

    def sync(self, abs_path):
        """确保服务端拿到的是磁盘上的最新内容，返回 (uri, version)。"""
        abs_path = os.path.abspath(abs_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_FILE_BYTES)
        except OSError as e:
            raise Unavailable("读取文件失败：%s" % e)
        uri = _uri(abs_path)
        old = self._docs.get(abs_path)
        if old is None:
            self.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": _lang_of(abs_path),
                                 "version": 1, "text": text},
            })
            self._docs[abs_path] = (1, text)
            return uri, 1
        version, prev = old
        if prev != text:
            version += 1
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],      # 全量同步
            })
            self._docs[abs_path] = (version, text)
        return uri, version

    def wait_diagnostics(self, uri, version, wait=DIAG_WAIT):
        """等一条 version >= 本次同步版本 的诊断推送。

        两种等待策略：
        - 有内容 → 立刻返回；
        - 空结果 → 再多等 0.5s，pyright 有时先推一条空诊断、分析完再推真实的。
        """
        key = _diag_key(uri)
        deadline = time.time() + wait
        quiet_until = 0.0
        while time.time() < deadline:
            item = self._diags.get(key)
            if item and item.get("version", 0) >= version:
                if item.get("items"):
                    return item
                if not quiet_until:
                    quiet_until = time.time() + 0.5
                elif time.time() >= quiet_until:
                    return item
            time.sleep(0.05)
        return self._diags.get(key) or {"version": 0, "items": [], "at": 0}


def _lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "typescriptreact",
        ".js": "javascript", ".jsx": "javascriptreact",
        ".json": "json", ".md": "markdown",
    }.get(ext, "plaintext")


def _read_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        try:
            text = line.decode("ascii", "replace").strip()
        except Exception:
            continue
        if not text:
            break
        if ":" in text:
            k, v = text.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        n = int(headers.get("content-length") or 0)
    except ValueError:
        return None
    if n <= 0 or n > 64 * 1024 * 1024:
        return None
    body = stream.read(n)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


# ---------------------------------------------------------------- 会话管理

def session(root, restart=False):
    """取（必要时创建）某个沙箱的会话。"""
    root = os.path.abspath(root or "")
    if not root or not os.path.isdir(root):
        raise Unavailable("沙箱目录不存在：%s" % (root or "（空）"))
    with _SESS_LOCK:
        s = _SESSIONS.get(root)
        if s and s.alive() and not restart:
            s.last_used = time.time()
            return s
        if s:
            s.stop()
        # 超出上限先淘汰最久未用的，避免同时挂一堆 node
        if len(_SESSIONS) >= MAX_SESSIONS:
            victim = min(_SESSIONS.values(), key=lambda x: x.last_used)
            victim.stop()
            _SESSIONS.pop(victim.root, None)
        s = Session(root)
        _SESSIONS[root] = s
    try:
        s.start()
    except Exception:
        # 起不来就不要把死会话留在表里，否则下次会反复拿到同一个坏对象
        with _SESS_LOCK:
            if _SESSIONS.get(root) is s:
                _SESSIONS.pop(root, None)
        raise
    return s


def shutdown():
    """退出时清理（测试里也用它，避免留下 node 进程）。"""
    with _SESS_LOCK:
        items = list(_SESSIONS.values())
        _SESSIONS.clear()
    for s in items:
        s.stop()


# ---------------------------------------------------------------- 对外能力

def _fmt_pos(path, line0, char0):
    return "%s:%d:%d" % (path, int(line0) + 1, int(char0) + 1)


def _rel(root, abs_path):
    try:
        return os.path.relpath(abs_path, root).replace("\\", "/")
    except ValueError:
        return abs_path


def _collect_locations(root, result):
    """definition / references 的返回值统一成 [(相对路径, 行, 列)]。

    LSP 允许三种形状：Location、Location[]、LocationLink[]。
    """
    if not result:
        return []
    if isinstance(result, dict):
        result = [result]
    out = []
    for it in result:
        if not isinstance(it, dict):
            continue
        loc = it.get("targetUri") and {"uri": it["targetUri"],
                                       "range": it.get("targetSelectionRange") or it.get("targetRange")}
        loc = loc or it
        uri = loc.get("uri")
        rng = loc.get("range") or {}
        start = rng.get("start") or {}
        abs_path = _path_of(uri)
        if not abs_path:
            continue
        out.append((_rel(root, abs_path), int(start.get("line") or 0),
                    int(start.get("character") or 0)))
    return out


def _symbol_column(text, line_no, symbol):
    """把「第几行 + 符号名」换算成 LSP 需要的 character。

    模型很难自己数列号，所以我们替它在这一行里找一次符号。
    """
    lines = text.splitlines()
    idx = max(0, min(int(line_no) - 1, len(lines) - 1))
    line = lines[idx] if lines else ""
    if symbol:
        at = line.find(symbol)
        if at >= 0:
            return idx, at
    return idx, len(line) - len(line.lstrip())


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_FILE_BYTES)
    except OSError:
        return ""


def diagnostics(root, path="", wait=DIAG_WAIT):
    """返回 (文本, None)。path 可以是文件或目录（目录最多看 MAX_DIAG_FILES 个）。"""
    base = os.path.abspath(path or root)
    if not os.path.exists(base):
        return "❌ 路径不存在：%s" % _rel(root, base), None
    targets = []
    if os.path.isfile(base):
        targets = [base]
    else:
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in
                      (".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build")]
            for fn in fns:
                if fn.endswith((".py", ".pyi")):
                    targets.append(os.path.join(dp, fn))
                if len(targets) >= MAX_DIAG_FILES:
                    break
            if len(targets) >= MAX_DIAG_FILES:
                break
    if not targets:
        return "没有可检查的 Python 文件（只做 Python 类型检查）。", None

    s = session(root)
    total_err = total_warn = 0
    blocks = []
    skipped = 0
    for abs_path in targets:
        try:
            uri, version = s.sync(abs_path)
            item = s.wait_diagnostics(uri, version, wait=wait)
        except Unavailable as e:
            return "❌ %s" % e, None
        items = []
        for d in item.get("items") or []:
            code = str(d.get("code") or d.get("rule") or "")
            if code in _NOISE_CODES:
                skipped += 1
                continue
            sev = int(d.get("severity") or 1)
            if sev > 2:                     # 只要 error / warning
                continue
            rng = (d.get("range") or {}).get("start") or {}
            items.append({
                "line": int(rng.get("line") or 0) + 1,
                "col": int(rng.get("character") or 0) + 1,
                "sev": "错误" if sev == 1 else "警告",
                "msg": str(d.get("message") or "").replace("\n", " "),
                "code": code,
            })
        errs = sum(1 for x in items if x["sev"] == "错误")
        warns = len(items) - errs
        total_err += errs
        total_warn += warns
        if items:
            blocks.append((_rel(root, abs_path), items, len(item.get("items") or []) == 0))

    if not blocks:
        tail = "（另有 %d 条「库未安装 / 缺类型存根」的提示已忽略）" % skipped if skipped else ""
        return "类型检查通过：扫描 %d 个文件，没有 error / warning%s" % (len(targets), tail), None

    lines = ["类型检查：%d 个文件里 %d 个 error、%d 个 warning%s"
             % (len(targets), total_err, total_warn,
                ("（忽略 %d 条缺库提示）" % skipped) if skipped else "")]
    for rel, items, empty in blocks:
        if empty:
            continue
        lines.append("\n【%s】" % rel)
        for x in items[:MAX_DIAG_ITEMS]:
            code = ("  [%s]" % x["code"]) if x["code"] else ""
            lines.append("  %d:%d %s %s%s" % (x["line"], x["col"], x["sev"], x["msg"], code))
    return "\n".join(lines), None


def definition(root, path, line, symbol=""):
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
        })
    except Unavailable as e:
        return "❌ %s" % e, None
    locs = _collect_locations(root, res)
    if not locs:
        return ("没有找到定义。可确认符号名是否写对，或改用 find_symbol / search_code。"), None
    lines = ["定义（%s:%d 的第 %d 个字符）→" % (_rel(root, abs_path), line0 + 1, char0 + 1)]
    for rel, l0, c0 in locs[:10]:
        snippet = ""
        full = os.path.join(root, rel.replace("/", os.sep))
        src = _read_text(full).splitlines()
        if 0 <= l0 < len(src):
            snippet = "  " + src[l0].strip()[:120]
        lines.append("  %s:%d:%d" % (rel, l0 + 1, c0 + 1))
        if snippet:
            lines.append(snippet)
    return "\n".join(lines), None


def references(root, path, line, symbol=""):
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
            "context": {"includeDeclaration": True},
        })
    except Unavailable as e:
        return "❌ %s" % e, None
    locs = _collect_locations(root, res)
    if not locs:
        return "没有找到引用。", None
    lines = ["引用 %d 处（最多列 %d 条）：" % (len(locs), MAX_REFERENCES)]
    for rel, l0, c0 in locs[:MAX_REFERENCES]:
        src = _read_text(os.path.join(root, rel.replace("/", os.sep))).splitlines()
        snippet = ("  " + src[l0].strip()[:110]) if 0 <= l0 < len(src) else ""
        lines.append("  %s:%d:%d" % (rel, l0 + 1, c0 + 1))
        if snippet:
            lines.append(snippet)
    if len(locs) > MAX_REFERENCES:
        lines.append("…（还有 %d 处未列出）" % (len(locs) - MAX_REFERENCES))
    return "\n".join(lines), None


def rename_plan(root, path, line, symbol, new_name):
    """向 pyright 要一份 WorkspaceEdit，换成「每个文件的新内容」。

    返回 ({path, old, new} 列表, 说明文本)。不写盘 —— 由 agent 决定怎么落，
    这样能复用撤销栈、写后自检与变更审阅。
    """
    new_name = str(new_name or "").strip()
    if not new_name:
        return None, "❌ 缺少 new_name"
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return None, "❌ 文件不存在：%s" % _rel(root, abs_path)
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
            "newName": new_name,
        })
    except Unavailable as e:
        return None, "❌ %s" % e
    if not res:
        return None, "pyright 没有给出重命名方案（符号可能无法重命名，或不是可重命名的标识符）"
    files, total = _workspace_edit(res, root)

    # pyright 的首次索引是后台跑的：如果只返回了当前文件、而别的文件里也确实
    # 出现过这个名字，很可能只是还没分析完。等一次再要，避免「改一半」。
    if len(files) <= 1 and _name_used_elsewhere(root, abs_path, symbol):
        time.sleep(0.9)
        try:
            res2 = s.request("textDocument/rename", {
                "textDocument": {"uri": uri},
                "position": {"line": line0, "character": char0},
                "newName": new_name,
            })
        except Unavailable:
            res2 = None
        if res2:
            files2, total2 = _workspace_edit(res2, root)
            if len(files2) > len(files):
                files, total = files2, total2

    if not files:
        return None, "重命名没有产生需要写盘的改动。"
    lines = ["重命名「%s」→「%s」：%d 个文件、%d 处改动"
             % (symbol or "?", new_name, len(files), total)]
    for f in files:
        old_lines = f["old"].count("\n") + 1
        new_lines = f["new"].count("\n") + 1
        lines.append("  %s（%d → %d 行）" % (f["rel"], old_lines, new_lines))
    return files, "\n".join(lines)


def _name_used_elsewhere(root, abs_path, symbol):
    """别的 .py 文件里是否也出现过这个标识符（用于判断重命名是否被截断）。"""
    if not symbol:
        return False
    pat = re.compile(r"\b%s\b" % re.escape(symbol))
    seen = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in
                  (".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build")]
        for fn in fns:
            full = os.path.join(dp, fn)
            if os.path.normcase(os.path.abspath(full)) == os.path.normcase(abs_path):
                continue
            if os.path.splitext(fn)[1].lower() not in (".py", ".pyi"):
                continue
            try:
                if 0 < os.path.getsize(full) <= MAX_FILE_BYTES:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        if pat.search(f.read()):
                            return True
            except OSError:
                continue
            seen += 1
            if seen >= 200:
                return False
    return False


def _workspace_edit(res, root):
    """把 WorkspaceEdit 落到「每个文件的新内容」。返回 (files, 改动处数)。"""
    edits_by_file = {}
    for uri_key, edits in (res.get("changes") or {}).items():
        edits_by_file.setdefault(uri_key, []).extend(edits or [])
    for ch in res.get("documentChanges") or []:
        td = ch.get("textDocument") or {}
        if td.get("uri"):
            edits_by_file.setdefault(td["uri"], []).extend(ch.get("edits") or [])

    root_real = os.path.realpath(root)
    files, total = [], 0
    for uri_key, edits in edits_by_file.items():
        target = _path_of(uri_key)
        if not target or not os.path.isfile(target):
            continue
        # 只允许改沙箱内的文件：WorkspaceEdit 可能包含库 / 系统路径
        if os.path.commonpath([os.path.realpath(target), root_real]) != root_real:
            continue
        src = _read_text(target)
        lines = src.splitlines(keepends=True)
        if not lines:
            continue
        # 从后往前改，前面的位置才不会被位移带偏
        ordered = sorted(edits, key=lambda e: (
            (e.get("range") or {}).get("start", {}).get("line", 0),
            (e.get("range") or {}).get("start", {}).get("character", 0)), reverse=True)
        buf = lines
        for e in ordered:
            rng = e.get("range") or {}
            st, en = rng.get("start") or {}, rng.get("end") or {}
            l1, c1 = int(st.get("line") or 0), int(st.get("character") or 0)
            l2, c2 = int(en.get("line") or 0), int(en.get("character") or 0)
            if l1 >= len(buf) or l2 >= len(buf):
                continue
            head = buf[l1][:c1]
            tail = buf[l2][c2:]
            buf[l1:l2 + 1] = [head + str(e.get("newText") or "") + tail]
            total += 1
        new_text = "".join(buf)
        if new_text != src:
            files.append({"path": target, "old": src, "new": new_text,
                          "rel": _rel(root, target)})
    return files, total


# ---------------------------------------------------------------- 语义查询：hover / 符号 / 实现 / 调用链

MAX_SYMBOLS = 120


def _markup_text(contents):
    """Hover.contents 可能是 string / MarkupContent / MarkedString[]。"""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value") or contents.get("text") or "")
    if isinstance(contents, list):
        return "\n".join(x for x in (_markup_text(c) for c in contents) if x)
    return ""


def _flat_symbols(items, depth=0, out=None):
    """DocumentSymbol 是树、SymbolInformation 是扁的，统一成 (层级, 名字, 细节, 行)。"""
    out = out if out is not None else []
    for it in items or []:
        if not isinstance(it, dict) or len(out) >= MAX_SYMBOLS:
            break
        rng = it.get("range") or (it.get("location") or {}).get("range") or {}
        line = int((rng.get("start") or {}).get("line") or 0) + 1
        out.append((depth, it.get("name") or "", it.get("detail") or "", line))
        _flat_symbols(it.get("children") or [], depth + 1, out)
    return out


def hover(root, path, line, symbol=""):
    """悬停类型 / 文档：不用读整个文件就能知道签名。"""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
        })
    except Unavailable as e:
        return "❌ %s" % e, None
    body = _markup_text((res or {}).get("contents")).strip()
    if not body:
        return "没有悬停信息（可能是空白 / 字面量，或服务端还没分析完）。", None
    return ("%s:%d:%d\n%s" % (_rel(root, abs_path), line0 + 1, char0 + 1, body[:4000])), None


def document_symbols(root, path):
    """文件内的符号树（能到方法级，比 repo_map 的顶层列表细）。"""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    except Unavailable as e:
        return "❌ %s" % e, None
    rows = _flat_symbols(res)
    if not rows:
        return "没有符号（空文件，或服务端还没分析完）。", None
    lines = ["%s 的符号（%d 个）：" % (_rel(root, abs_path), len(rows))]
    for depth, name, detail, line in rows[:MAX_SYMBOLS]:
        lines.append("  %s%s  L%d%s" % ("  " * depth, name, line,
                                        ("  " + detail) if detail else ""))
    return "\n".join(lines), None


def workspace_symbols(root, query):
    """全项目按名字找符号（比 grep 精准：只回符号，不混注释与字符串）。"""
    q = str(query or "").strip()
    if not q:
        return "请提供 query（符号名或片段）", None
    s = session(root)
    try:
        res = s.request("workspace/symbol", {"query": q})
    except Unavailable as e:
        return "❌ %s" % e, None
    rows = []
    for it in (res or [])[:MAX_SYMBOLS]:
        loc = it.get("location") or {}
        target = _path_of(loc.get("uri"))
        if not target:
            continue
        start = (loc.get("range") or {}).get("start") or {}
        rows.append((_rel(root, target), int(start.get("line") or 0) + 1,
                     it.get("name") or "", it.get("containerName") or ""))
    if not rows:
        return "没有找到符号「%s」。" % q, None
    lines = ["符号搜索「%s」命中 %d 个：" % (q, len(rows))]
    for rel, line, name, holder in rows:
        lines.append("  %s:%d  %s%s" % (rel, line, name,
                                        ("  （%s）" % holder) if holder else ""))
    return "\n".join(lines), None


def implementation(root, path, line, symbol=""):
    """谁实现了这个接口 / 抽象方法。"""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        res = s.request("textDocument/implementation", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
        })
    except Unavailable as e:
        return "❌ %s" % e, None
    locs = _collect_locations(root, res)
    if not locs:
        return "没有找到实现（该符号可能不是接口 / 抽象方法）。", None
    lines = ["实现 %d 处：" % len(locs)]
    for rel, l0, c0 in locs[:MAX_SYMBOLS]:
        src = _read_text(os.path.join(root, rel.replace("/", os.sep))).splitlines()
        snippet = ("  " + src[l0].strip()[:110]) if 0 <= l0 < len(src) else ""
        lines.append("  %s:%d:%d" % (rel, l0 + 1, c0 + 1))
        if snippet:
            lines.append(snippet)
    return "\n".join(lines), None


def call_hierarchy(root, path, line, symbol="", direction="incoming"):
    """调用链：谁调用了它（incoming）/ 它调用了谁（outgoing）。"""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % _rel(root, abs_path), None
    text = _read_text(abs_path)
    line0, char0 = _symbol_column(text, line, symbol)
    s = session(root)
    try:
        uri, _v = s.sync(abs_path)
        pre = s.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line0, "character": char0},
        })
    except Unavailable as e:
        return "❌ %s" % e, None
    if not pre:
        return "该位置不是函数 / 方法，拿不到调用层级。", None
    item = pre[0]
    outgoing = str(direction or "").strip().lower() == "outgoing"
    method = ("callHierarchy/outgoingCalls" if outgoing
              else "callHierarchy/incomingCalls")
    try:
        res = s.request(method, {"item": item})
    except Unavailable as e:
        return "❌ %s" % e, None
    rows = []
    for call in (res or [])[:MAX_SYMBOLS]:
        node = (call.get("to") if outgoing else call.get("from")) or {}
        target = _path_of(node.get("uri") or "")
        start = (node.get("selectionRange") or node.get("range") or {}).get("start") or {}
        rows.append((node.get("name") or "?", _rel(root, target) if target else "?",
                     int(start.get("line") or 0) + 1,
                     len(call.get("fromRanges") or [])))
    if not rows:
        return ("没有%s。" % ("被调用的函数" if outgoing else "调用者")), None
    lines = ["%s「%s」的%s（%d 个）："
             % (_rel(root, abs_path), symbol or (item.get("name") or "?"),
                "下游调用" if outgoing else "调用者", len(rows))]
    for name, rel, line, hits in rows:
        lines.append("  %s  %s:%d  （%d 处）" % (name, rel, line, hits))
    return "\n".join(lines), None
