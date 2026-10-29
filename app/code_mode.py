# -*- coding: utf-8 -*-
"""Code Mode：让模型写一段 Python 脚本，在一次调用里编排多次只读工具。

为什么需要：无状态接口每轮都要重传全部历史。长任务里「读 20 个文件 → 统计 →
再读 5 个」如果按工具一轮一轮跑，上下文会线性膨胀（实测单轮 prompt p90 14 万 token）。
Code Mode 把这些操作放进一段脚本，只有脚本输出与调用摘要进模型上下文。

安全边界：
- 默认只能调用只读工具；完全放权或设置里打开 code_mode_allow_write /
  code_mode_allow_cmd 后，才允许脚本直接写文件 / 执行命令。
- 子进程 cwd = 会话沙箱；环境变量只传最小集合（不传 API key / 配置）。
- 总时长、输出长度、工具调用次数都有上限；用户点停止会直接终止子进程。
- 脚本里的写入同样登记撤销快照（caps 带 sid 时）。

协议：脚本里 `import gm`，通过 stdout/stdin 的逐行 JSON 与父进程通信：
    gm.call("read_file", path="src/main.js") -> str
    gm.log("正在统计", n)                     -> 实时推到前端（可选）
父进程收到 call 后执行 agent.run_tool，把结果写回同一行协议。
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# 脚本里可以调用的工具：全部只读、无副作用
ALLOWED_TOOLS = {
    "list_dir", "read_file", "search_files", "search_code", "repo_map",
    "code_context", "impact", "check_code", "find_symbol",
    "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover",
    "lsp_symbols", "lsp_implementation", "lsp_calls",
    "git_status", "git_diff", "git_log", "list_steps",
}

MAX_CALLS = 2000                # 单次脚本最多调用多少次工具（可由配置覆盖）
MAX_PARALLEL = 16               # 单次 parallel 最多并发（可由配置覆盖）
MAX_OUTPUT = 12000              # 回灌给模型的输出上限（字符）
MAX_SUMMARY = 60                # 调用摘要最多列几条
DEFAULT_TIMEOUT = 120           # 秒
MAX_TIMEOUT = 600

_BRIDGE_SRC = r'''
import itertools
import json
import os
import sys
import threading

_LOCK = threading.Lock()
_IDS = itertools.count(1)


class ToolError(RuntimeError):
    """脚本里的工具失败：带稳定的 code，便于按类型处理。"""
    def __init__(self, message, code="TOOL_FAILED", tool=""):
        super().__init__(message)
        self.code = code
        self.tool = tool


def _send(obj):
    with _LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _recv():
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("gm bridge closed")
    return json.loads(line)


def _catalog():
    try:
        p = os.path.join(os.path.dirname(__file__), "tools.json")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("tools") or []
    except Exception:
        return []


def tools():
    """当前脚本可调用的工具目录（name / description / parameters）。"""
    return _catalog()


def help(name):
    """取一个工具的 schema；找不到返回空 dict。"""
    for it in _catalog():
        if str(it.get("name") or "") == str(name):
            return it
    return {}


def call(tool, **kwargs):
    """调用一个工具，返回结果文本。失败抛 ToolError（带 code / tool）。"""
    rid = next(_IDS)
    with _LOCK:
        sys.stdout.write(json.dumps(
            {"type": "call", "id": rid, "tool": str(tool), "args": kwargs},
            ensure_ascii=False) + "\n")
        sys.stdout.flush()
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("gm bridge closed")
            obj = json.loads(line)
            if obj.get("type") == "result" and obj.get("id") == rid:
                break
    if not obj.get("ok"):
        raise ToolError(obj.get("error") or "tool failed",
                        obj.get("code") or "TOOL_FAILED", str(tool))
    return obj.get("result") or ""


def log(*parts):
    """实时日志：推到界面，不计入最终输出。"""
    _send({"type": "log", "text": " ".join(str(p) for p in parts)})


def _parallel(calls, workers=4, detail=False):
    items = []
    for it in calls or []:
        if isinstance(it, (list, tuple)) and it:
            tool = str(it[0])
            args = it[1] if len(it) > 1 and isinstance(it[1], dict) else {}
        elif isinstance(it, dict):
            tool = str(it.get("tool") or "")
            args = it.get("args") or {}
        else:
            continue
        items.append({"tool": tool, "args": args})
    rid = next(_IDS)
    with _LOCK:
        sys.stdout.write(json.dumps(
            {"type": "parallel", "id": rid, "calls": items,
             "workers": int(workers or 4)}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("gm bridge closed")
            obj = json.loads(line)
            if obj.get("type") == "results" and obj.get("id") == rid:
                break
    rows = obj.get("items") or []
    if detail:
        return rows
    return [(it.get("result") or "") if it.get("ok") else None for it in rows]


def parallel(calls, workers=4):
    """并发跑一批调用，返回结果列表（失败的位置为 None）。"""
    return _parallel(calls, workers=workers, detail=False)


def parallel_detail(calls, workers=4):
    """并发跑一批调用，返回 [{tool, ok, result, error, code}, ...]。"""
    return _parallel(calls, workers=workers, detail=True)
'''


# ============================================================
# 安全护栏：装进子进程的 audit hook
# ------------------------------------------------------------
# 光限制 gm.call 的白名单不够：脚本是真实 Python 进程，`open()` / `os.remove` /
# `subprocess` 都能绕过「默认只能读」。这里在脚本开头注入一个 audit hook：
#   - 读：默认只允许沙箱内 + Python 自身（stdlib / site-packages）+ 桥接目录；
#     「允许读取沙箱外」打开后才放开；
#   - 写：默认全禁，打开 code_mode_allow_write 后只允许写沙箱内；
#   - 命令：默认全禁，打开 code_mode_allow_cmd 后才允许。
# 变量统一用 _gm_ 前缀，避免和模型生成的代码撞名。
# ============================================================
GUARD_SRC = r'''
# ===== Code Mode 安全护栏（自动注入，勿删）=====
import os as _gm_os
import sys as _gm_sys

_gm_root = _gm_os.path.realpath(_gm_os.environ.get("GM_GUARD_ROOT") or ".")
_gm_write = _gm_os.environ.get("GM_GUARD_WRITE") == "1"
_gm_cmd = _gm_os.environ.get("GM_GUARD_CMD") == "1"
_gm_outside = _gm_os.environ.get("GM_GUARD_OUTSIDE") == "1"
_gm_allow = []
for _gm_p in (_gm_os.environ.get("GM_GUARD_ALLOW") or "").split(_gm_os.pathsep):
    if _gm_p:
        try:
            _gm_allow.append(_gm_os.path.realpath(_gm_p))
        except OSError:
            pass
# 永远禁止的路径：本项目 data/（含密钥）与沙箱内 .git（remote 可能带令牌）
_gm_protected = []
for _gm_p in (_gm_os.environ.get("GM_GUARD_DENY") or "").split(_gm_os.pathsep):
    if _gm_p:
        try:
            _gm_protected.append(_gm_os.path.realpath(_gm_p))
        except OSError:
            pass
# Python 自己的目录永远可读（import 会打开 stdlib / site-packages 里的文件）
for _gm_p in (_gm_sys.prefix, getattr(_gm_sys, "base_prefix", ""),
              _gm_os.path.dirname(_gm_os.__file__)):
    if _gm_p:
        try:
            _gm_allow.append(_gm_os.path.realpath(_gm_p))
        except OSError:
            pass
try:
    import site as _gm_site
    for _gm_p in list(_gm_site.getsitepackages()) + [_gm_site.getusersitepackages()]:
        if _gm_p:
            _gm_allow.append(_gm_os.path.realpath(_gm_p))
except Exception:
    pass


def _gm_real(p):
    try:
        return _gm_os.path.realpath(_gm_os.fsdecode(p))
    except (TypeError, ValueError, OSError):
        return ""


def _gm_inside(p, root):
    return bool(root) and (p == root or p.startswith(root + _gm_os.sep))


def _gm_deny(what):
    raise PermissionError("Code Mode 安全护栏拒绝：" + what)


def _gm_check_open(path, mode, flags):
    if path is None or isinstance(path, int):
        return
    p = _gm_real(path)
    if not p:
        return
    for _gm_d in _gm_protected:
        if _gm_inside(p, _gm_d):
            _gm_deny("该路径受保护（运行数据 / .git）：" + p)
    for _gm_a in _gm_allow:
        if _gm_inside(p, _gm_a):
            return
    writing = False
    if isinstance(mode, str) and mode:
        writing = any(_gm_c in mode for _gm_c in "wax+")
    elif isinstance(flags, int):
        writing = bool(flags & (_gm_os.O_WRONLY | _gm_os.O_RDWR | _gm_os.O_CREAT
                                | _gm_os.O_APPEND | _gm_os.O_TRUNC))
    if writing:
        if not _gm_write:
            _gm_deny("脚本内写文件需要开启「允许脚本写入」（code_mode_allow_write）")
        if not _gm_inside(p, _gm_root):
            _gm_deny("只能写沙箱内的文件：" + p)
    elif not _gm_outside and not _gm_inside(p, _gm_root):
        _gm_deny("只能读沙箱内的文件：" + p)


def _gm_check_fs(event, args):
    if not _gm_write:
        _gm_deny("%s 需要开启「允许脚本写入」（code_mode_allow_write）" % event)
    for _gm_a in args:
        if isinstance(_gm_a, (str, bytes, _gm_os.PathLike)):
            _gm_p = _gm_real(_gm_a)
            if not _gm_p:
                continue
            for _gm_d in _gm_protected:
                if _gm_inside(_gm_p, _gm_d):
                    _gm_deny("该路径受保护（运行数据 / .git）：" + _gm_p)
            if not _gm_inside(_gm_p, _gm_root):
                _gm_deny("只能修改沙箱内的路径：" + _gm_p)


def _gm_hook(event, args):
    if event == "open":
        _gm_check_open(args[0], args[1], args[2])
    elif event in ("os.remove", "os.rename", "os.mkdir", "os.rmdir",
                   "os.chmod", "os.chown", "os.truncate", "os.link",
                   "os.symlink", "shutil.rmtree", "shutil.move",
                   "shutil.copyfile", "shutil.copytree", "shutil.copymode"):
        _gm_check_fs(event, args)
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        if not _gm_cmd:
            _gm_deny("脚本内执行命令需要开启「允许脚本执行命令」（code_mode_allow_cmd）")


_gm_sys.addaudithook(_gm_hook)
# ===== 安全护栏结束 =====
'''


def _bridge_dir(catalog=None):
    d = os.path.join(tempfile.gettempdir(), "gm_code_bridge")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "gm.py")
    try:
        old = ""
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                old = f.read()
        if old != _BRIDGE_SRC:
            with open(p, "w", encoding="utf-8") as f:
                f.write(_BRIDGE_SRC)
    except OSError:
        pass
    if catalog is not None:
        try:
            with open(os.path.join(d, "tools.json"), "w", encoding="utf-8") as f:
                json.dump({"tools": catalog}, f, ensure_ascii=False)
        except OSError:
            pass
    return d


def _clean_env(bridge, sandbox="", allow_write=False, allow_cmd=False,
               allow_outside=False, extra_allow=()):
    """只传最小环境变量：不把 API key / 配置带进模型生成的代码。

    同时写入安全护栏的运行参数：沙箱根、写 / 命令 / 沙箱外读三个开关，
    以及「永远可读」的路径（桥接目录、脚本临时目录）。
    """
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
            "PYTHONIOENCODING", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONPATH"] = bridge + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # 默认禁止写文件，导入本地模块时不该因为生成 .pyc 而失败
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GM_GUARD_ROOT"] = os.path.realpath(sandbox) if sandbox else ""
    env["GM_GUARD_WRITE"] = "1" if allow_write else "0"
    env["GM_GUARD_CMD"] = "1" if allow_cmd else "0"
    env["GM_GUARD_OUTSIDE"] = "1" if allow_outside else "0"
    allow = [bridge] + [p for p in extra_allow if p]
    env["GM_GUARD_ALLOW"] = os.pathsep.join(
        os.path.realpath(p) for p in allow if p)
    # 永远禁止：本项目 data/（密钥等运行数据）与沙箱内 .git（remote 可能带令牌）
    deny = []
    try:
        from app.store import DATA_DIR
        deny.append(DATA_DIR)
    except Exception:
        pass
    if sandbox:
        deny.append(os.path.join(sandbox, ".git"))
    env["GM_GUARD_DENY"] = os.pathsep.join(
        os.path.realpath(p) for p in deny if p)
    return env


def _clip(text, n):
    t = str(text or "")
    if len(t) <= n:
        return t
    return t[:int(n * 0.75)] + "\n…（中间省略 %d 字）…\n" % (len(t) - n) + t[-int(n * 0.25):]


def trim_guard_trace(text):
    """去掉 traceback 里安全护栏自身的栈帧，只留用户代码位置与最终异常。

    护栏在脚本里是普通函数，拒绝时会产生 3–4 层 `_gm_*` 栈帧；
    原样回灌给模型既费 token 也干扰定位。
    """
    lines = str(text or "").splitlines()
    out = []
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if head.startswith('File "') and (", in _gm_" in head or "_gm_" in nxt):
            # 只吃掉这一帧（File 行 + 缩进的源码 / 箭头行），
            # 后面的最终异常行（无缩进）必须保留
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _tool_catalog(allowed):
    """把允许的工具整理成 gm.tools() 可读的目录。"""
    try:
        from app import agent
        schemas = agent.native_tool_schemas(
            agent_on=True, search_ready=True, cap_domains=None,
            computer_on=True, lsp_on=True, git_commit_mode="ask")
        out = []
        for s in schemas:
            fn = s.get("function") if isinstance(s, dict) else None
            name = str((fn or {}).get("name") or "")
            if name in allowed:
                out.append({"name": name,
                            "description": str((fn or {}).get("description") or ""),
                            "parameters": (fn or {}).get("parameters") or {}})
        have = {x["name"] for x in out}
        for n in sorted(set(allowed) - have):
            out.append({"name": n, "description": "", "parameters": {}})
        return out
    except Exception:
        return [{"name": n, "description": "", "parameters": {}} for n in sorted(allowed)]


def run(sandbox, code, caps=None, stop_event=None, emit=None, timeout=0,
        allow_tools=None, allow_write=False, allow_cmd=False, allow_outside=False):
    """执行一段 Python 脚本。返回 (结果文本, 统计 dict)。

    allow_write / allow_cmd / allow_outside 由调用方按权限档位决定。
    脚本开头会注入 audit hook（GUARD_SRC）：默认只读沙箱内文件，
    写文件与执行命令分别由上面两个开关放行；打开后写入仍限制在沙箱内。
    emit(obj) 用来把 script_log 推给前端（可选）。
    """
    emit = emit or (lambda obj: None)
    code = str(code or "")
    if not code.strip():
        return "❌ run_script：code 为空", {"calls": 0}
    # gm 桥接模块由运行环境提供，模型不必自己 import（漏写是这次会话里的真实故障）
    if not re.search(r"(?m)^\s*(?:import\s+gm\b|from\s+gm\s+import\b)", code):
        code = "import gm  # run_script 自动注入\n" + code
    sandbox = str(sandbox or "").strip()
    if not sandbox or not os.path.isdir(sandbox):
        return "❌ run_script：需要先设置 Agent 沙箱目录", {"calls": 0}
    # 安全护栏必须在用户代码之前执行
    code = GUARD_SRC + "\n" + code
    try:
        timeout = int(timeout or 0) or DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))
    allowed = set(ALLOWED_TOOLS if allow_tools is None else allow_tools)
    if allow_write or allow_cmd:
        from app import agent as _agent
        if allow_write:
            allowed |= set(_agent.WRITE_TOOLS)
        if allow_cmd:
            allowed |= set(_agent.RUN_TOOLS)

    try:
        max_calls = int((caps or {}).get("code_max_calls") or MAX_CALLS)
    except (TypeError, ValueError):
        max_calls = MAX_CALLS
    if max_calls < 0:
        max_calls = MAX_CALLS
    try:
        max_parallel = int((caps or {}).get("code_parallel") or MAX_PARALLEL)
    except (TypeError, ValueError):
        max_parallel = MAX_PARALLEL
    max_parallel = max(1, min(max_parallel, MAX_PARALLEL))

    bridge = _bridge_dir(_tool_catalog(allowed))
    work = os.path.join(tempfile.gettempdir(), "gm_code_run", uuid.uuid4().hex[:10])
    os.makedirs(work, exist_ok=True)
    script = os.path.join(work, "script.py")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
    except OSError as e:
        return "❌ run_script：写入脚本失败：%s" % e, {"calls": 0}

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", script],
            cwd=sandbox,
            env=_clean_env(bridge, sandbox=sandbox, allow_write=allow_write,
                           allow_cmd=allow_cmd, allow_outside=allow_outside,
                           extra_allow=(work,)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return "❌ run_script：启动失败：%s" % e, {"calls": 0}

    q = queue.Queue()
    outputs, errors = [], []
    calls = []                      # [(tool, ok, 结果长度, 摘要)]
    t0 = time.time()
    aborted = {"v": False}

    def _reader(stream, kind):
        try:
            for line in stream:
                q.put((kind, line.rstrip("\n")))
        except Exception as e:
            q.put((kind, "⚠ 读取失败：%s" % e))
        finally:
            q.put((kind, None))

    threading.Thread(target=_reader, args=(proc.stdout, "out"), daemon=True).start()
    threading.Thread(target=_reader, args=(proc.stderr, "err"), daemon=True).start()

    def _reply(obj):
        try:
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception:
            pass

    done_streams = 0
    while done_streams < 2:
        if stop_event is not None and stop_event.is_set():
            aborted["v"] = True
            break
        if time.time() - t0 > timeout:
            aborted["v"] = True
            break
        try:
            kind, line = q.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None:
                # 进程已退出但队列还没读干净：再等一轮
                continue
            continue
        if line is None:
            done_streams += 1
            continue
        if kind == "err":
            if line.strip():
                errors.append(line)
            continue
        if not line.strip():
            continue
        obj = None
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
        if isinstance(obj, dict) and obj.get("type") == "call":
            tool = str(obj.get("tool") or "")
            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            if max_calls and len(calls) >= max_calls:
                _reply({"type": "result", "id": obj.get("id"), "ok": False,
                        "code": "TOOL_BUDGET",
                        "error": "工具调用超过上限 %d 次，请拆小一点" % max_calls})
                continue
            ok, text, code = _exec_tool(sandbox, tool, args, caps, allowed)
            calls.append((tool, ok, len(text), _clip(" ".join(text.split()), 80)))
            _reply({"type": "result", "id": obj.get("id"), "ok": ok,
                    "code": code, "result": text})
            continue
        if isinstance(obj, dict) and obj.get("type") == "parallel":
            items = obj.get("calls") if isinstance(obj.get("calls"), list) else []
            try:
                _w = max(1, min(int(obj.get("workers") or 4), max_parallel))
            except (TypeError, ValueError):
                _w = min(4, max_parallel)
            results = _exec_parallel(sandbox, items, caps, allowed, _w)
            for it in results:
                calls.append((it["tool"], it["ok"], len(it["text"]),
                              _clip(" ".join(it["text"].split()), 80)))
            _reply({"type": "results", "id": obj.get("id"), "items": results})
            continue
        if isinstance(obj, dict) and obj.get("type") == "log":
            emit({"type": "script_log", "text": str(obj.get("text") or "")[:500]})
            continue
        outputs.append(line)

    # 收尾：先给进程一点时间自然退出，再强制
    try:
        proc.wait(timeout=1)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.stdin.close()
    except Exception:
        pass
    rc = proc.poll()
    if rc is None:
        rc = -1
    cost = time.time() - t0
    shutil.rmtree(work, ignore_errors=True)

    out = _clip("\n".join(outputs).strip(), MAX_OUTPUT)
    err = _clip(trim_guard_trace("\n".join(errors).strip()), 2000)
    failed = aborted["v"] or rc != 0
    status = "已中止" if aborted["v"] else ("执行失败" if failed else "执行完成")
    lines = ["%s【run_script】%s（%.1f 秒，%d 次工具调用）"
             % ("❌ " if failed else "", status, cost, len(calls))]
    if out:
        lines.append("---- 输出 ----\n" + out)
    else:
        lines.append("---- 输出 ----\n（没有 print 输出）")
    if calls:
        shown = calls[:MAX_SUMMARY]
        lines.append("---- 工具调用 ----\n" + "\n".join(
            "- %s%s → %s" % (n, "" if ok else "（失败）", s) for n, ok, _l, s in shown))
        if len(calls) > len(shown):
            lines.append("…（共 %d 次，只列前 %d 条）" % (len(calls), len(shown)))
    if err:
        lines.append("---- 错误输出 ----\n" + err)
        if "NameError: name 'gm' is not defined" in err:
            lines.append("提示：gm 已自动注入；如果脚本用了 exec / 自定义入口，请保留首行 import gm。")
    if aborted["v"]:
        lines.append("（%s）" % ("用户已停止" if (stop_event is not None and stop_event.is_set())
                                 else "超过 %d 秒超时" % timeout))
    return "\n\n".join(lines), {"calls": len(calls), "seconds": cost,
                                "aborted": aborted["v"], "exit_code": rc,
                                "ok": not failed}


def _exec_parallel(sandbox, items, caps, allowed, workers):
    """批量执行：只读调用并发，含写 / 命令时强制串行（避免并发写同一棵树）。"""
    def one(it):
        tool = str(it.get("tool") or "")
        args = it.get("args") if isinstance(it.get("args"), dict) else {}
        ok, text, code = _exec_tool(sandbox, tool, args, caps, allowed)
        return {"tool": tool, "ok": ok, "text": text, "result": text,
                "code": code, "error": "" if ok else text}

    if not items:
        return []
    # 只要有一个不是「只读白名单」里的工具，就串行执行
    if any(str(it.get("tool") or "") not in ALLOWED_TOOLS for it in items):
        return [one(it) for it in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(max(1, workers), len(items),
                                            MAX_PARALLEL)) as pool:
        return list(pool.map(one, items))


def _exec_tool(sandbox, tool, args, caps, allowed):
    """执行一次工具调用；越权 / 异常都转成 (ok, 文本, 稳定错误码)。"""
    if tool not in allowed:
        return False, ("❌ 不允许在脚本里调用 %s（本次脚本允许：%s）"
                       % (tool, "、".join(sorted(allowed)))), "TOOL_NOT_ALLOWED"
    try:
        from app import agent
        text, undo, is_write = agent.run_tool(sandbox, tool, args, caps=caps)
        # 脚本内写入也要登记撤销：否则 Code Mode 打开写权限后无法回退
        if is_write and undo and (caps or {}).get("sid"):
            try:
                agent.register_undo(caps["sid"], undo, tool)
            except Exception:
                pass
        ok = not str(text).lstrip().startswith("❌")
        return ok, text, ("" if ok else "TOOL_FAILED")
    except Exception as e:
        return False, "❌ %s 执行失败：%s" % (tool, e), "TOOL_ERROR"
