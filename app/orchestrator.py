# -*- coding: utf-8 -*-
"""持久化工作流：可执行、可落盘、可恢复的编排层。

和「一次调用内的脚本编排」不同：

- 每个节点执行完立刻把状态写盘，进程重启/中断后可以 resume；
- 节点产物有名字，后续节点用 `{{id.output}}` 引用，父对话只拿最终摘要；
- 写文件 / 跑命令仍然走主循环的审批（由调用方注入 exec_node），工作流不放权；
- 预算（token / 秒）是硬闸，超了就暂停而不是硬杀。

节点类型：tool / command / script / explore / verify / final。
"""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.store import DATA_DIR

MAX_STEPS = 24
MAX_PARALLEL = 8
MAX_TEXT = 200
MAX_OUTPUT = 20000          # 单节点落盘 / 回灌上限
DEFAULT_TIMEOUT = 300
NODE_TYPES = ("tool", "command", "script", "explore", "verify", "final",
              "loop", "map", "condition")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_WF_RE = re.compile(r"^wf_[0-9a-f]{8}$")
_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.([A-Za-z0-9_]+)\s*\}\}")
_VAR_RE = re.compile(r"\{\{\s*(item|index)\s*\}\}")
_MARK = {"done": "✓", "failed": "✗", "skipped": "⏭", "running": "…",
         "pending": "○", "blocked": "!"}

_LOCK = threading.RLock()
_LIVE = {}          # wf_id -> {"pause": bool, "cancel": bool}（只对进行中的工作流有效）


# ---------------------------------------------------------------- 存储

def _dir():
    d = os.path.join(DATA_DIR, "workflows")
    os.makedirs(d, exist_ok=True)
    return d


def _path(wf_id):
    wid = str(wf_id or "").strip()
    if not _WF_RE.match(wid):
        return None
    return os.path.join(_dir(), wid + ".json")


def new_id():
    return "wf_" + uuid.uuid4().hex[:8]


def valid(wf_id):
    return bool(_WF_RE.match(str(wf_id or "").strip()))


def save(wf):
    p = _path((wf or {}).get("id"))
    if not p:
        return False
    wf["updated_at"] = time.time()
    tmp = p + ".tmp"
    try:
        with _LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False)
            os.replace(tmp, p)
        return True
    except OSError as e:
        print("[workflow] 落盘失败：%s" % e)
        return False


def load(wf_id):
    p = _path(wf_id)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            wf = json.load(f)
        return wf if isinstance(wf, dict) else None
    except (OSError, ValueError):
        return None


def _gc(keep):
    """按最旧优先清理历史工作流文件；keep<=0 不清理。"""
    if keep <= 0:
        return
    d = _dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return
    entries = []
    for n in names:
        p = os.path.join(d, n)
        try:
            entries.append((os.stat(p).st_mtime, p))
        except OSError:
            continue
    entries.sort()
    while len(entries) > keep:
        _mt, p = entries.pop(0)
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- 定义校验

def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _str_list(v, limit=12, text_limit=MAX_TEXT):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:limit]:
        s = str(x).strip()[:text_limit]
        if s and s not in out:
            out.append(s)
    return out


def _sub_steps(raw, prefix, max_n=8):
    """复合节点（loop / map / condition）里的子步骤：只支持叶子节点，防止无限嵌套。"""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for i, it in enumerate(raw[:max_n]):
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type") or "tool").strip().lower()
        if typ not in ("tool", "command", "script", "explore", "verify", "final"):
            continue
        nid = str(it.get("id") or ("%s%d" % (prefix, i + 1))).strip()
        if not _ID_RE.match(nid):
            nid = "%s%d" % (prefix, i + 1)
        node = {"id": nid, "type": typ, "status": "pending", "ms": 0, "tokens": 0,
                "output": "", "error": "", "exit_code": None, "files": [],
                "retries_used": 0, "attempts": 0,
                "retries": max(0, _int(it.get("retries"), 0)),
                "timeout": max(0, _int(it.get("timeout"), 0)),
                "always": True, "needs": []}
        if typ == "tool":
            node["tool"] = str(it.get("tool") or "").strip()
            node["args"] = it.get("args") if isinstance(it.get("args"), dict) else {}
        elif typ == "command":
            argv = it.get("argv")
            if isinstance(argv, str):
                argv = argv.split()
            node["argv"] = [str(x) for x in (argv or [])][:60]
            node["cwd"] = str(it.get("cwd") or "").strip()[:300]
        elif typ == "script":
            node["code"] = str(it.get("code") or "")[:20000]
            node["allow_write"] = bool(it.get("allow_write"))
            node["allow_cmd"] = bool(it.get("allow_cmd"))
        elif typ == "explore":
            node["goal"] = str(it.get("goal") or "").strip()[:MAX_TEXT]
            node["questions"] = _str_list(it.get("questions"), limit=3, text_limit=300)
            node["verify"] = bool(it.get("verify"))
            node["research"] = bool(it.get("research"))
        elif typ == "verify":
            checks = it.get("checks")
            if isinstance(checks, dict):
                checks = [checks]
            node["checks"] = [c for c in (checks or [])[:12] if isinstance(c, dict)]
        elif typ == "final":
            node["template"] = str(it.get("template") or "")[:8000]
        out.append(node)
    return out


def normalize(spec, max_steps=MAX_STEPS):
    """把模型给的工作流定义收敛成可执行结构；失败返回 (None, 原因)。"""
    spec = spec if isinstance(spec, dict) else {}
    raw = spec.get("steps")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None, "缺少 steps：给 1–%d 个节点" % int(max_steps or MAX_STEPS)
    steps, seen = [], set()
    for i, it in enumerate(raw[:max(1, int(max_steps or MAX_STEPS))]):
        if not isinstance(it, dict):
            return None, "第 %d 个节点不是对象" % (i + 1)
        nid = str(it.get("id") or ("s%d" % (i + 1))).strip()
        if not _ID_RE.match(nid):
            return None, "节点 id 不合法：%s（字母/数字/_/-，≤32）" % nid
        if nid in seen:
            return None, "节点 id 重复：%s" % nid
        seen.add(nid)
        typ = str(it.get("type") or "tool").strip().lower()
        if typ not in NODE_TYPES:
            return None, ("节点 %s 的 type 不支持：%s（可选 %s）"
                          % (nid, typ, " / ".join(NODE_TYPES)))
        node = {"id": nid, "type": typ, "status": "pending", "ms": 0, "tokens": 0,
                "output": "", "error": "", "exit_code": None, "files": [],
                "retries_used": 0, "attempts": 0,
                "retries": max(0, _int(it.get("retries"), 0)),
                "timeout": max(0, _int(it.get("timeout"), 0)),
                "always": bool(it.get("always"))}
        needs = it.get("needs")
        if needs is None:
            # 没写 needs 默认串到上一个节点；想要并行分支就显式写 needs: []
            node["needs"] = [steps[-1]["id"]] if steps else []
        else:
            node["needs"] = _str_list(needs, limit=12, text_limit=32)
        node["when"] = str(it.get("when") or "").strip()[:MAX_TEXT]

        if typ == "tool":
            tool = str(it.get("tool") or "").strip()
            if not tool:
                return None, "节点 %s 缺少 tool" % nid
            node["tool"] = tool
            node["args"] = it.get("args") if isinstance(it.get("args"), dict) else {}
            node["if_exists"] = str(it.get("if_exists") or "").strip().lower()
        elif typ == "command":
            argv = it.get("argv")
            if isinstance(argv, str):
                argv = argv.split()
            if not isinstance(argv, list) or not argv:
                return None, "节点 %s 缺少 argv（字符串数组）" % nid
            node["argv"] = [str(x) for x in argv][:60]
            node["cwd"] = str(it.get("cwd") or "").strip()[:300]
        elif typ == "script":
            code = str(it.get("code") or "").strip()
            if not code:
                return None, "节点 %s 缺少 code" % nid
            node["code"] = code[:20000]
            node["allow_write"] = bool(it.get("allow_write"))
            node["allow_cmd"] = bool(it.get("allow_cmd"))
        elif typ == "explore":
            node["goal"] = str(it.get("goal") or "").strip()[:MAX_TEXT]
            node["questions"] = _str_list(it.get("questions"), limit=3, text_limit=300)
            if not node["questions"]:
                return None, "节点 %s 缺少 questions" % nid
            node["verify"] = bool(it.get("verify"))
            # 资料调研节点：子代理带联网搜索，结论按来源 URL 核验
            node["research"] = bool(it.get("research"))
        elif typ == "verify":
            checks = it.get("checks")
            if isinstance(checks, dict):
                checks = [checks]
            if not isinstance(checks, list) or not checks:
                return None, "节点 %s 缺少 checks" % nid
            node["checks"] = [c for c in checks[:12] if isinstance(c, dict)]
            if not node["checks"]:
                return None, "节点 %s 的 checks 里没有可用项" % nid
        elif typ == "final":
            node["template"] = str(it.get("template") or "")[:8000]
        elif typ == "condition":
            node["cond"] = str(it.get("cond") or it.get("if") or "true")[:MAX_TEXT]
            node["then"] = _sub_steps(it.get("then") or [], nid + "_t")
            node["else"] = _sub_steps(it.get("else") or [], nid + "_e")
            if not node["then"] and not node["else"]:
                return None, "节点 %s 的 condition 需要 then 或 else" % nid
        elif typ == "loop":
            node["items"] = it.get("items")
            node["steps"] = _sub_steps(it.get("steps") or [], nid + "_l")
            node["max_iterations"] = max(1, min(
                _int(it.get("max_iterations"), 20) or 20, 100))
            node["until"] = str(it.get("until") or "")[:MAX_TEXT]
            node["on_error"] = str(it.get("on_error") or "stop").lower()
            if not node["steps"]:
                return None, "节点 %s 的 loop 缺少 steps" % nid
        elif typ == "map":
            node["items"] = it.get("items")
            node["node"] = it.get("node") if isinstance(it.get("node"), dict) else {}
            node["parallel"] = max(1, min(_int(it.get("parallel"), 4) or 4, 16))
            node["collect"] = str(it.get("collect") or "list").lower()
            if not node["node"]:
                return None, "节点 %s 的 map 缺少 node" % nid
        steps.append(node)

    err = _check_graph(steps)
    if err:
        return None, err
    budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
    return {
        "name": (str(spec.get("name") or "").strip()[:80] or "工作流"),
        "steps": steps,
        "budget": {"tokens": max(0, _int(budget.get("tokens"), 0)),
                   "seconds": max(0, _int(budget.get("seconds"), 0))},
        "fail_fast": spec.get("fail_fast", True) is not False,
    }, ""


def _check_graph(steps):
    ids = {s["id"] for s in steps}
    for s in steps:
        for n in s["needs"]:
            if n not in ids:
                return "节点 %s 依赖不存在的 %s" % (s["id"], n)
            if n == s["id"]:
                return "节点 %s 不能依赖自己" % s["id"]
    # Kahn 拓扑排序：排不完说明有环
    indeg = {s["id"]: len(s["needs"]) for s in steps}
    kids = {s["id"]: [] for s in steps}
    for s in steps:
        for n in s["needs"]:
            kids[n].append(s["id"])
    q = [k for k, v in indeg.items() if v == 0]
    seen = 0
    while q:
        k = q.pop()
        seen += 1
        for c in kids[k]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if seen != len(steps):
        return "节点依赖成环，无法确定执行顺序"
    return ""


# ---------------------------------------------------------------- 模板与条件

def _render(text, wf):
    if not isinstance(text, str) or "{{" not in text:
        return text
    v = (wf or {}).get("__vars") or {}
    if v:
        text = _VAR_RE.sub(lambda m: str(v.get(m.group(1), m.group(0))), text)
    by_id = {x.get("id"): x for x in (wf or {}).get("steps") or []}
    by_id.update((wf or {}).get("__sub") or {})

    def rep(m):
        node = by_id.get(m.group(1))
        if not node:
            return m.group(0)
        key = m.group(2)
        if key == "output":
            return str(node.get("output") or "")[:8000]
        if key in ("status", "error", "exit_code", "ms", "tokens"):
            return str(node.get(key) if node.get(key) is not None else "")
        return m.group(0)

    return _REF_RE.sub(rep, text)


def _render_value(v, wf, depth=0):
    if depth > 4:
        return v
    if isinstance(v, str):
        return _render(v, wf)
    if isinstance(v, list):
        return [_render_value(x, wf, depth + 1) for x in v]
    if isinstance(v, dict):
        return {k: _render_value(x, wf, depth + 1) for k, x in v.items()}
    return v


def _render_node(node, wf):
    out = dict(node)
    for k in ("args", "argv", "cwd", "goal", "questions", "code", "checks", "template"):
        if k in out:
            out[k] = _render_value(out[k], wf)
    return out


def _when_ok(expr, wf):
    expr = str(expr or "").strip()
    if not expr:
        return True
    e = _render(expr, wf).strip()
    if "{{" in e:                   # 引用了还没跑的节点：不满足
        return False
    if "||" in e:
        return any(_when_ok(x, wf) for x in e.split("||"))
    if "&&" in e:
        return all(_when_ok(x, wf) for x in e.split("&&"))
    if e.startswith("!"):
        return not _when_ok(e[1:], wf)
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if op in e:
            a, b = [x.strip().strip("'\"") for x in e.split(op, 1)]
            try:
                fa, fb = float(a), float(b)
            except ValueError:
                if op == "==":
                    return a == b
                if op == "!=":
                    return a != b
                return False
            return {">=": fa >= fb, "<=": fa <= fb, "==": fa == fb,
                    "!=": fa != fb, ">": fa > fb, "<": fa < fb}[op]
    if " contains " in e:
        a, b = [x.strip().strip("'\"") for x in e.split(" contains ", 1)]
        return b in a
    return e.lower() not in ("false", "0", "no", "off", "")


# ---------------------------------------------------------------- 执行

def start(session_id, spec, exec_node, emit=None, stop_event=None, cfg=None):
    cfg = cfg or {}
    wf, err = normalize(spec, _int(cfg.get("max_steps"), MAX_STEPS) or MAX_STEPS)
    if err:
        return {"ok": False, "status": "failed", "error": err, "steps": []}
    wf["id"] = new_id()
    wf["session"] = str(session_id or "")
    wf["status"] = "running"
    wf["error"] = ""
    wf["created_at"] = time.time()
    wf["used"] = {"tokens": 0}
    wf["_sandbox"] = str(cfg.get("sandbox") or "")
    save(wf)
    _gc(_int(cfg.get("keep"), 200))
    _emit(wf, emit)
    return _drive(wf, exec_node, emit, stop_event, cfg)


def resume(session_id, wf_id, exec_node, emit=None, stop_event=None, cfg=None):
    wf = load(wf_id)
    if not wf:
        return {"ok": False, "status": "failed", "id": wf_id,
                "error": "工作流 %s 不存在（可能已被清理）" % wf_id, "steps": []}
    if wf.get("status") == "completed":
        return wf
    if wf.get("status") == "cancelled":
        return wf
    wf["session"] = str(session_id or wf.get("session") or "")
    wf["status"] = "running"
    wf["error"] = ""
    wf["_sandbox"] = str((cfg or {}).get("sandbox") or wf.get("_sandbox") or "")
    for s in wf.get("steps") or []:
        if s.get("status") == "running":       # 上次中断时正在跑的节点：重跑
            s["status"] = "pending"
    save(wf)
    _emit(wf, emit)
    return _drive(wf, exec_node, emit, stop_event, cfg)


def pause(wf_id):
    return set_flag(wf_id, "pause")


def cancel(wf_id):
    return set_flag(wf_id, "cancel")


def set_flag(wf_id, name):
    wid = str(wf_id or "").strip()
    if not valid(wid) or name not in ("pause", "cancel"):
        return False
    with _LOCK:
        live = _LIVE.get(wid)
        if live is not None:
            live[name] = True
            return True
    wf = load(wid)
    if not wf:
        return False
    wf["status"] = "cancelled" if name == "cancel" else "paused"
    wf["error"] = "已取消" if name == "cancel" else "已暂停"
    save(wf)
    return True


def _emit(wf, emit):
    if not emit:
        return
    try:
        emit({"type": "workflow", "id": wf.get("id"), "name": wf.get("name"),
              "status": wf.get("status"),
              "used": dict(wf.get("used") or {}),
              "steps": [{"id": s.get("id"), "type": s.get("type"),
                         "status": s.get("status"), "ms": s.get("ms") or 0,
                         "tokens": s.get("tokens") or 0,
                         "error": (s.get("error") or "")[:120]}
                        for s in wf.get("steps") or []]})
    except Exception:
        pass


def _over_budget(wf, t0):
    b = wf.get("budget") or {}
    if b.get("tokens") and _int((wf.get("used") or {}).get("tokens")) >= b["tokens"]:
        return True
    if b.get("seconds") and (time.time() - t0) >= b["seconds"]:
        return True
    return False


def _needs_serial(node):
    """写 / 命令 / 脚本 / 子代理：串行，避免并发改树或抢审批。"""
    typ = node.get("type")
    if typ in ("command", "script", "explore", "loop", "map", "condition"):
        return True
    if typ == "tool":
        from app import agent
        name = node.get("tool")
        return (name in agent.WRITE_TOOLS or name in agent.RUN_TOOLS
                or name in agent.SHELL_TOOLS
                or name in agent.GIT_WRITE_TOOLS or name in agent.COMPUTER_TOOLS)
    return False


def _ready(wf):
    by_id = {s["id"]: s for s in wf.get("steps") or []}
    out = []
    for s in wf.get("steps") or []:
        if s.get("status") != "pending":
            continue
        deps = [by_id[n] for n in (s.get("needs") or []) if n in by_id]
        if any(d.get("status") == "failed" for d in deps) and not s.get("always"):
            s["status"] = "skipped"
            s["output"] = "（依赖失败，已跳过）"
            continue
        if any(d.get("status") not in ("done", "skipped", "failed") for d in deps):
            continue
        if not _when_ok(s.get("when"), wf):
            s["status"] = "skipped"
            s["output"] = "（条件不满足，已跳过）"
            continue
        out.append(s)
    return out


def _settle(wf):
    sts = [s.get("status") for s in wf.get("steps") or []]
    if all(x in ("done", "skipped") for x in sts):
        return "completed"
    if any(x == "failed" for x in sts):
        return "failed"
    return "blocked"


def _drive(wf, exec_node, emit, stop_event, cfg):
    cfg = cfg or {}
    _LIVE[wf["id"]] = {"pause": False, "cancel": False}
    t0 = time.time()
    max_par = max(1, _int(cfg.get("max_parallel"), MAX_PARALLEL) or MAX_PARALLEL)
    try:
        while True:
            live = _LIVE.get(wf["id"]) or {}
            if live.get("cancel"):
                wf["status"] = "cancelled"
                wf["error"] = "已取消"
                break
            if stop_event is not None and stop_event.is_set():
                # 用户点了停止 ≠ 取消：保留断点，之后可以 resume
                wf["status"] = "interrupted"
                wf["error"] = "用户已停止"
                break
            if live.get("pause"):
                wf["status"] = "paused"
                wf["error"] = "已暂停"
                break
            if _over_budget(wf, t0):
                wf["status"] = "paused"
                wf["error"] = "超出预算，已暂停"
                break
            ready = _ready(wf)
            if not ready:
                wf["status"] = _settle(wf)
                break
            batch = ready[:max_par]
            if any(_needs_serial(s) for s in batch):
                batch = batch[:1]
            for s in batch:
                s["status"] = "running"
                s["attempts"] = _int(s.get("attempts")) + 1
                s["started_at"] = time.time()
            save(wf)
            _emit(wf, emit)
            if len(batch) == 1:
                outs = [_exec_one(batch[0], wf, exec_node)]
            else:
                with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                    outs = list(pool.map(lambda n: _exec_one(n, wf, exec_node), batch))
            for s, out in zip(batch, outs):
                _apply(s, out, wf)
            save(wf)
            _emit(wf, emit)
    except Exception as e:
        wf["status"] = "failed"
        wf["error"] = "引擎异常：%s" % e
    finally:
        _LIVE.pop(wf["id"], None)
        save(wf)
        _emit(wf, emit)
    wf["ok"] = wf.get("status") == "completed"
    return wf


def _resolve_items(v, wf):
    """把 items 解析成数组：支持 JSON 字符串、模板引用、行文本。"""
    if isinstance(v, str):
        s = _render(v, wf).strip()
        try:
            data = json.loads(s)
        except ValueError:
            return [x.strip() for x in s.splitlines() if x.strip()]
        return data if isinstance(data, list) else []
    if isinstance(v, list):
        return _render_value(v, wf)
    return []


def _loop_output(children):
    try:
        return json.dumps(children, ensure_ascii=False)[:MAX_OUTPUT]
    except (TypeError, ValueError):
        return "\n".join(str(c.get("output") or "") for c in children)[:MAX_OUTPUT]


def _exec_substeps(steps, wf, exec_node):
    """按顺序执行一组子步骤；子步骤之间可用 {{id.output}} 互相引用。"""
    if not steps:
        return []
    old = wf.get("__sub")
    wf["__sub"] = {s["id"]: s for s in steps}
    try:
        for s in steps:
            _exec_one(s, wf, exec_node)
            if s.get("status") == "failed":
                break
        return steps
    finally:
        if old is None:
            wf.pop("__sub", None)
        else:
            wf["__sub"] = old


def _exec_condition(node, wf, exec_node):
    branch = (node.get("then") if _when_ok(node.get("cond") or "true", wf)
              else node.get("else"))
    steps = _exec_substeps(branch or [], wf, exec_node)
    ok = all(s.get("status") == "done" for s in steps)
    return {"ok": ok, "output": (steps[-1].get("output") if steps else ""),
            "error": "" if ok else "分支执行失败"}


def _exec_loop(node, wf, exec_node):
    """对 items 逐项执行 steps；max_iterations 上限，until 提前结束，children 支持 resume。"""
    items = _resolve_items(node.get("items"), wf)
    max_it = max(1, min(_int(node.get("max_iterations"), 20) or 20, 100))
    until = node.get("until") or ""
    children = node.setdefault("children", [])
    start_i = len(children)
    for i in range(start_i, min(len(items), max_it)):
        wf["__vars"] = {"item": items[i], "index": i}
        steps = _exec_substeps(node.get("steps") or [], wf, exec_node)
        ok = all(s.get("status") == "done" for s in steps)
        children.append({"index": i, "item": items[i], "ok": ok,
                         "output": (steps[-1].get("output") if steps else "")[:2000]})
        if not ok and node.get("on_error") != "continue":
            wf.pop("__vars", None)
            return {"ok": False, "error": "第 %d 次迭代失败" % (i + 1),
                    "output": _loop_output(children)}
        if until and _when_ok(until, wf):
            break
    wf.pop("__vars", None)
    return {"ok": True, "output": _loop_output(children)}


def _exec_map(node, wf, exec_node):
    """对 items 扇出同一个子节点；parallel 控制并发，collect=list/join。"""
    items = _resolve_items(node.get("items"), wf)
    tpl = node.get("node") or {}
    par = max(1, min(_int(node.get("parallel"), 4) or 4, 16))

    def one(i):
        sub = _sub_steps([tpl], "m%d_" % i, 1)
        if not sub:
            return {"index": i, "item": items[i], "ok": False, "output": "",
                    "error": "map.node 不是可执行节点"}
        wf2 = dict(wf)
        wf2["__vars"] = {"item": items[i], "index": i}
        s = sub[0]
        _exec_one(s, wf2, exec_node)
        return {"index": i, "item": items[i], "ok": s.get("status") == "done",
                "output": s.get("output") or "", "error": s.get("error") or ""}

    if par <= 1 or len(items) <= 1:
        recs = [one(i) for i in range(len(items))]
    else:
        with ThreadPoolExecutor(max_workers=par) as pool:
            recs = list(pool.map(one, range(len(items))))
    node["children"] = recs
    ok = all(r.get("ok") for r in recs)
    if node.get("collect") == "join":
        output = "\n".join(r.get("output") or "" for r in recs if r.get("ok"))
    else:
        output = json.dumps(recs, ensure_ascii=False)
    return {"ok": ok, "output": output,
            "error": "" if ok else "部分 map 分支失败"}


def _exec_one(node, wf, exec_node):
    started = time.time()
    tries = 1 + max(0, _int(node.get("retries")))
    out = {}
    for i in range(tries):
        node["retries_used"] = i
        try:
            if node["type"] == "verify":
                out = _verify(node, wf)
            elif node["type"] == "final":
                tpl = node.get("template") or _default_final(wf)
                out = {"ok": True, "output": _render(tpl, wf)}
            elif node["type"] == "condition":
                out = _exec_condition(node, wf, exec_node)
            elif node["type"] == "loop":
                out = _exec_loop(node, wf, exec_node)
            elif node["type"] == "map":
                out = _exec_map(node, wf, exec_node)
            else:
                out = exec_node(_render_node(node, wf)) or {
                    "ok": False, "error": "节点执行器没有返回结果"}
        except Exception as e:
            out = {"ok": False, "error": "节点异常：%s" % e}
        if out.get("ok") or out.get("no_retry"):
            break
        if i + 1 < tries:
            time.sleep(min(2.0, 0.4 * (i + 1)))
    node["ms"] = int((time.time() - started) * 1000)
    node["tokens"] = max(0, _int(out.get("tokens")))
    text = str(out.get("output") or out.get("text") or "")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n…（输出超长已截断，共 %d 字）" % len(text)
    node["output"] = text
    node["ok"] = bool(out.get("ok"))
    node["status"] = "done" if node["ok"] else "failed"
    node["error"] = "" if node["ok"] else str(out.get("error") or "失败")[:300]
    node["exit_code"] = out.get("exit_code")
    node["files"] = [str(x) for x in (out.get("files") or [])][:20]
    return node


def _apply(node, out, wf):
    """节点状态已由 _exec_one 写回 node；这里只累计整批 token。"""
    wf.setdefault("used", {})
    wf["used"]["tokens"] = _int(wf["used"].get("tokens")) + _int((out or {}).get("tokens"))


def _default_final(wf):
    lines = []
    for s in wf.get("steps") or []:
        if s.get("status") == "done" and s.get("output"):
            lines.append("[%s] %s" % (s.get("id"), s.get("output")[:1200]))
    return "\n\n".join(lines) or "（没有产出）"


def _safe_join(sandbox, rel):
    root = os.path.abspath(sandbox or ".")
    full = os.path.abspath(os.path.join(root, str(rel or "")))
    root_n = os.path.normcase(root)
    if os.path.normcase(full) != root_n and not os.path.normcase(full).startswith(root_n + os.sep):
        return None
    return full


def _verify(node, wf):
    sandbox = wf.get("_sandbox") or ""
    fails, lines = [], []
    for c in node.get("checks") or []:
        path = _render(str(c.get("path") or ""), wf)
        full = _safe_join(sandbox, path)
        if full is None:
            fails.append("%s：路径越出沙箱" % path)
            continue
        exists = os.path.exists(full)
        want = c.get("exists")
        if want is True and not exists:
            fails.append("%s：不存在" % path)
            continue
        if want is False and exists:
            fails.append("%s：不应存在" % path)
            continue
        if not exists:
            lines.append("%s：不存在（符合预期）" % path)
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(200000)
        except OSError as e:
            fails.append("%s：读取失败 %s" % (path, e))
            continue
        before = len(fails)
        for key in ("contains", "not_contains"):
            pat = _render(str(c.get(key) or ""), wf)
            if not pat:
                continue
            hit = pat in text
            if key == "contains" and not hit:
                fails.append("%s：缺少「%s」" % (path, pat[:40]))
            elif key == "not_contains" and hit:
                fails.append("%s：仍含「%s」" % (path, pat[:40]))
        rx = c.get("regex")
        if rx:
            try:
                if not re.search(str(rx), text):
                    fails.append("%s：正则未命中 %s" % (path, str(rx)[:40]))
            except re.error:
                fails.append("%s：正则非法 %s" % (path, str(rx)[:40]))
        if len(fails) == before:
            lines.append("%s：通过" % path)
    ok = not fails
    body = lines + (["✗ " + x for x in fails] if fails else [])
    return {"ok": ok, "output": "\n".join(body),
            "error": "" if ok else "；".join(fails[:5])}


# ---------------------------------------------------------------- 给模型看

def _clip(text, n):
    t = str(text or "")
    return t if len(t) <= n else t[:max(0, n - 1)] + "…"


def summary_text(wf):
    """压成给主模型的一小段：状态 + 每节点一行 + 最终产出。"""
    wf = wf or {}
    if wf.get("error") and not wf.get("steps"):
        return "【工作流】%s" % wf["error"]
    lines = ["【工作流】%s %s · %s（%d token）"
             % (wf.get("id") or "", wf.get("name") or "",
                wf.get("status") or "?", _int((wf.get("used") or {}).get("tokens")))]
    for s in wf.get("steps") or []:
        extra = []
        if s.get("ms"):
            extra.append("%.1fs" % (s["ms"] / 1000.0))
        if s.get("tokens"):
            extra.append("%dtok" % s["tokens"])
        if s.get("status") == "failed" and s.get("error"):
            extra.append(s["error"][:80])
        lines.append("[%s/%s]%s %s" % (s.get("id"), s.get("type"),
                                       _MARK.get(s.get("status"), "?"),
                                       " ".join(extra)))
    final = next((s.get("output") for s in wf.get("steps") or []
                  if s.get("type") == "final" and s.get("output")), "")
    if final:
        lines.append("最终：\n" + _clip(final, 1500))
    if wf.get("error"):
        lines.append("原因：%s" % wf["error"])
    if wf.get("status") in ("paused", "interrupted", "blocked", "failed"):
        lines.append("提示：workflow_status(\"%s\") 看节点输出；"
                     "workflow_resume(\"%s\") 从断点继续；workflow_cancel 放弃。"
                     % (wf.get("id"), wf.get("id")))
    return _clip("\n".join(lines), 4000)


def status_text(wf):
    """带节点输出的详细视图（每个节点最多 800 字）。"""
    wf = wf or {}
    if not wf:
        return "【工作流】没有找到这个工作流。"
    lines = summary_text(wf).split("\n")
    body = ["【工作流详情】%s" % (wf.get("id") or "")]
    for s in wf.get("steps") or []:
        body.append("[%s/%s]%s" % (s.get("id"), s.get("type"),
                                   _MARK.get(s.get("status"), "?")))
        if s.get("output"):
            body.append("  " + _clip(s["output"], 800).replace("\n", "\n  "))
        if s.get("error"):
            body.append("  错误：" + str(s["error"])[:300])
    return _clip("\n".join([lines[0]] + body), 8000)


# ---------------------------------------------------------------- REST

def api_status(h):
    body = h._body()
    wf = load(body.get("id") or body.get("workflow_id"))
    if not wf:
        return h._json({"ok": False, "error": "not found"}, 404)
    return h._json({"ok": True, "workflow": wf})


def api_pause(h):
    body = h._body()
    return h._json({"ok": bool(pause(body.get("id")))})


def api_cancel(h):
    body = h._body()
    return h._json({"ok": bool(cancel(body.get("id")))})


def api_resume(h):
    """前端「继续」：只告诉它该带的 id；真正的续跑在聊天请求里执行。"""
    body = h._body()
    wf = load(body.get("id"))
    if not wf:
        return h._json({"ok": False, "error": "not found"}, 404)
    return h._json({"ok": True, "need_turn": True,
                    "id": wf.get("id"), "status": wf.get("status")})
