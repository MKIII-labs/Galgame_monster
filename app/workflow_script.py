# -*- coding: utf-8 -*-
"""脚本化工作流：用一段 Python 脚本编排工具调用与 DAG 工作流。

与 `run_workflow` 的区别：

- `run_workflow` 是模型直接给一份 JSON DAG；
- 这里让模型写脚本，脚本里可以用循环、条件、变量、`wf.parallel` 组合多次
  工具调用与多个工作流，中间结果留在脚本变量里，只有最终输出回传模型。

脚本里 `import wf`（自动注入）：
    wf.call("read_file", path="src/main.js") -> str
    wf.parallel([("read_file", {"path": "a"}), ...]) -> [str|None]
    wf.workflow(spec) -> dict        # 同步跑一个 DAG，返回结果
    wf.workflow_text(spec) -> str    # 同上，返回人话摘要
    wf.agent("reviewer", "审查 app/api") -> str        # 单个命名子代理
    wf.agents([{"agent": "tester", "task": "..."}]) -> str   # 并行派活
    wf.pipeline(["planner", "implementer"], "任务") -> str    # 串行交接
    wf.log(...)

安全边界与 Code Mode 一致：默认只读；写/命令由调用方按权限档位打开。
子代理的写权限跟随各自定义（implementer / documenter 等），每次写仍走审批。
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

from app import code_mode

MAX_CALLS = 2000
MAX_WORKFLOWS = 16
MAX_AGENT_CALLS = 8            # 一段脚本里最多调用几次子代理
MAX_OUTPUT = 12000
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 600

_WF_BRIDGE_SRC = r'''
import itertools
import json
import os
import sys
import threading

_LOCK = threading.Lock()
_IDS = itertools.count(1)


class ToolError(RuntimeError):
    def __init__(self, message, code="TOOL_FAILED", tool=""):
        super().__init__(message)
        self.code = code
        self.tool = tool


def _send(obj):
    with _LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _wait(rid, kind):
    while True:
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("wf bridge closed")
        obj = json.loads(line)
        if obj.get("type") == kind and obj.get("id") == rid:
            return obj


def _catalog():
    try:
        p = os.path.join(os.path.dirname(__file__), "tools.json")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("tools") or []
    except Exception:
        return []


def tools():
    return _catalog()


def help(name):
    for it in _catalog():
        if str(it.get("name") or "") == str(name):
            return it
    return {}


def log(*parts):
    _send({"type": "log", "text": " ".join(str(p) for p in parts)})


def call(tool, **kwargs):
    rid = next(_IDS)
    _send({"type": "call", "id": rid, "tool": str(tool), "args": kwargs})
    obj = _wait(rid, "result")
    if not obj.get("ok"):
        raise ToolError(obj.get("error") or "tool failed",
                        obj.get("code") or "TOOL_FAILED", str(tool))
    return obj.get("result") or ""


def _parallel(calls, workers=4, detail=False):
    items = []
    for it in calls or []:
        if isinstance(it, (list, tuple)) and it:
            items.append({"tool": str(it[0]),
                          "args": it[1] if len(it) > 1 and isinstance(it[1], dict) else {}})
        elif isinstance(it, dict):
            items.append({"tool": str(it.get("tool") or ""),
                          "args": it.get("args") or {}})
    rid = next(_IDS)
    _send({"type": "parallel", "id": rid, "calls": items,
           "workers": int(workers or 4)})
    obj = _wait(rid, "results")
    rows = obj.get("items") or []
    if detail:
        return rows
    return [(it.get("result") or "") if it.get("ok") else None for it in rows]


def parallel(calls, workers=4):
    return _parallel(calls, workers=workers, detail=False)


def parallel_detail(calls, workers=4):
    return _parallel(calls, workers=workers, detail=True)


def _workflow(spec, detail=False):
    rid = next(_IDS)
    _send({"type": "workflow", "id": rid, "spec": spec})
    obj = _wait(rid, "workflow_result")
    if not obj.get("ok"):
        raise ToolError(obj.get("error") or "workflow failed",
                        obj.get("code") or "WORKFLOW_FAILED", "workflow")
    return (obj.get("data") or {}) if detail else (obj.get("text") or "")


def workflow(spec):
    return _workflow(spec, detail=True)


def workflow_text(spec):
    return _workflow(spec, detail=False)


def _agents(payload):
    rid = next(_IDS)
    _send({"type": "agents", "id": rid, "payload": payload})
    obj = _wait(rid, "agents_result")
    if not obj.get("ok"):
        raise ToolError(obj.get("error") or "agents failed",
                        obj.get("code") or "AGENTS_FAILED", "agents")
    return obj.get("text") or ""


def agent(name, task, goal="", verify=False):
    """跑一个命名子代理，返回结论文本（写权限跟随代理定义，写入仍走审批）。"""
    return _agents({"agents": [{"agent": str(name), "task": str(task)}],
                    "goal": str(goal or ""), "verify": bool(verify)})


def agents(items, goal="", verify=False):
    """并行跑多个命名子代理；items 形如 [{"agent": "reviewer", "task": "..."}]。"""
    return _agents({"agents": items, "goal": str(goal or ""),
                    "verify": bool(verify)})


def pipeline(names, task, goal=""):
    """串行交接：上一阶段的结论作为下一阶段的背景。"""
    return _agents({"pipeline": [str(x) for x in (names or [])],
                    "task": str(task), "goal": str(goal or "")})
'''


def _bridge_dir(catalog=None):
    d = os.path.join(tempfile.gettempdir(), "gm_wf_bridge")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "wf.py")
    try:
        old = ""
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                old = f.read()
        if old != _WF_BRIDGE_SRC:
            with open(p, "w", encoding="utf-8") as f:
                f.write(_WF_BRIDGE_SRC)
    except OSError:
        pass
    if catalog is not None:
        try:
            with open(os.path.join(d, "tools.json"), "w", encoding="utf-8") as f:
                json.dump({"tools": catalog}, f, ensure_ascii=False)
        except OSError:
            pass
    return d


def _wf_text(res):
    res = res if isinstance(res, dict) else {}
    steps = res.get("steps") or []
    lines = ["工作流 %s：%s" % (res.get("id") or "?", res.get("status") or "?")]
    for st in steps[:20]:
        if not isinstance(st, dict):
            continue
        out = str(st.get("output") or st.get("error") or "").replace("\n", " ")[:160]
        lines.append("- %s [%s] %s" % (st.get("id") or "?", st.get("status") or "?", out))
    if res.get("error"):
        lines.append("错误：" + str(res.get("error"))[:300])
    return "\n".join(lines)


def run(sandbox, code, caps=None, stop_event=None, emit=None, timeout=0,
        allow_tools=None, allow_write=False, allow_cmd=False, allow_outside=False):
    """执行一段工作流脚本，返回 (文本, 统计 dict)。

    与 Code Mode 用同一套安全护栏（code_mode.GUARD_SRC）：默认只读沙箱，
    写 / 命令由开关放行，且写入仍限制在沙箱内。
    """
    emit = emit or (lambda obj: None)
    # 沙箱外只读：脚本里的工具调用也要拿到同一个开关
    caps = dict(caps or {})
    caps["allow_outside"] = bool(allow_outside)
    code = str(code or "")
    if not code.strip():
        return "❌ run_workflow_script：code 为空", {"calls": 0, "workflows": 0, "agents": 0}
    if not re.search(r"(?m)^\s*(?:import\s+wf\b|from\s+wf\s+import\b)", code):
        code = "import wf  # run_workflow_script 自动注入\n" + code
    sandbox = str(sandbox or "").strip()
    if not sandbox or not os.path.isdir(sandbox):
        return "❌ run_workflow_script：需要先设置 Agent 沙箱目录", {"calls": 0, "workflows": 0, "agents": 0}
    code = code_mode.GUARD_SRC + "\n" + code
    try:
        timeout = int(timeout or 0) or DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))
    allowed = set(code_mode.ALLOWED_TOOLS if allow_tools is None else allow_tools)
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
        max_wf = int((caps or {}).get("workflow_script_max") or MAX_WORKFLOWS)
    except (TypeError, ValueError):
        max_wf = MAX_WORKFLOWS
    max_wf = max(1, max_wf)
    try:
        max_agents = int((caps or {}).get("agent_script_agent_calls")
                         if (caps or {}).get("agent_script_agent_calls") is not None
                         else MAX_AGENT_CALLS)
    except (TypeError, ValueError):
        max_agents = MAX_AGENT_CALLS
    if max_agents < 0:
        max_agents = MAX_AGENT_CALLS

    bridge = _bridge_dir(code_mode._tool_catalog(allowed))
    work = os.path.join(tempfile.gettempdir(), "gm_wf_run", uuid.uuid4().hex[:10])
    os.makedirs(work, exist_ok=True)
    script = os.path.join(work, "script.py")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
    except OSError as e:
        return "❌ run_workflow_script：写入脚本失败：%s" % e, {"calls": 0, "workflows": 0, "agents": 0}

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", script], cwd=sandbox,
            env=code_mode._clean_env(bridge, sandbox=sandbox, allow_write=allow_write,
                                     allow_cmd=allow_cmd, allow_outside=allow_outside,
                                     extra_allow=(work,)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return "❌ run_workflow_script：启动失败：%s" % e, {"calls": 0, "workflows": 0, "agents": 0}

    q = queue.Queue()
    outputs, errors, calls, workflows = [], [], [], []
    agent_calls = []
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
                        "error": "工具调用超过上限 %d 次" % max_calls})
                continue
            ok, text, code = code_mode._exec_tool(sandbox, tool, args, caps, allowed)
            calls.append((tool, ok, len(text), code_mode._clip(" ".join(text.split()), 80)))
            _reply({"type": "result", "id": obj.get("id"), "ok": ok,
                    "code": code, "result": text})
            continue
        if isinstance(obj, dict) and obj.get("type") == "parallel":
            items = obj.get("calls") if isinstance(obj.get("calls"), list) else []
            try:
                _w = max(1, min(int(obj.get("workers") or 4), code_mode.MAX_PARALLEL))
            except (TypeError, ValueError):
                _w = 4
            rows = code_mode._exec_parallel(sandbox, items, caps, allowed, _w)
            for it in rows:
                calls.append((it["tool"], it["ok"], len(it["text"]),
                              code_mode._clip(" ".join(it["text"].split()), 80)))
            _reply({"type": "results", "id": obj.get("id"), "items": rows})
            continue
        if isinstance(obj, dict) and obj.get("type") == "workflow":
            spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
            if len(workflows) >= max_wf:
                _reply({"type": "workflow_result", "id": obj.get("id"), "ok": False,
                        "code": "WORKFLOW_BUDGET",
                        "error": "工作流次数超过上限 %d" % max_wf})
                continue
            fn = (caps or {}).get("run_workflow")
            if not callable(fn):
                _reply({"type": "workflow_result", "id": obj.get("id"), "ok": False,
                        "code": "WORKFLOW_UNAVAILABLE",
                        "error": "当前环境不支持在脚本里运行工作流"})
                continue
            try:
                res = fn(spec) or {}
            except Exception as e:
                res = {"ok": False, "status": "failed", "error": str(e)}
            text = _wf_text(res)
            workflows.append((res.get("id") or "?", res.get("status") or "?",
                              len(res.get("steps") or [])))
            _reply({"type": "workflow_result", "id": obj.get("id"),
                    "ok": bool(res.get("ok")), "text": text,
                    "data": res, "code": "" if res.get("ok") else "WORKFLOW_FAILED",
                    "error": "" if res.get("ok") else str(res.get("error") or "")})
            continue
        if isinstance(obj, dict) and obj.get("type") == "agents":
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if max_agents > 0 and len(agent_calls) >= max_agents:
                _reply({"type": "agents_result", "id": obj.get("id"), "ok": False,
                        "code": "AGENT_BUDGET",
                        "error": "子代理调用超过上限 %d 次" % max_agents})
                continue
            fn = (caps or {}).get("run_agents")
            if not callable(fn):
                _reply({"type": "agents_result", "id": obj.get("id"), "ok": False,
                        "code": "AGENT_UNAVAILABLE",
                        "error": "当前环境不支持在脚本里调用子代理"
                                 "（检查设置 → 子代理 → 脚本编排）"})
                continue
            try:
                res = fn(payload) or {}
            except Exception as e:         # noqa: BLE001
                res = {"ok": False, "error": str(e)}
            ok = bool(res.get("ok"))
            text = str(res.get("text") or ("" if ok else res.get("error") or "子代理没有结果"))
            names = []
            for it in (payload.get("agents") or []):
                if isinstance(it, dict) and it.get("agent"):
                    names.append(str(it.get("agent")))
            for it in (payload.get("pipeline") or []):
                names.append(str(it))
            agent_calls.append((names, ok))
            _reply({"type": "agents_result", "id": obj.get("id"),
                    "ok": ok, "text": text,
                    "code": "" if ok else "AGENTS_FAILED",
                    "error": "" if ok else text})
            continue
        if isinstance(obj, dict) and obj.get("type") == "log":
            emit({"type": "script_log", "text": str(obj.get("text") or "")[:500]})
            continue
        outputs.append(line)

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

    out = code_mode._clip("\n".join(outputs).strip(), MAX_OUTPUT)
    err = code_mode._clip(code_mode.trim_guard_trace("\n".join(errors).strip()), 2000)
    failed = aborted["v"] or rc != 0
    status = "已中止" if aborted["v"] else ("执行失败" if failed else "执行完成")
    lines = ["%s【run_workflow_script】%s（%.1f 秒，%d 次工具调用，%d 个工作流，%d 次子代理）"
             % ("❌ " if failed else "", status, cost, len(calls), len(workflows),
                len(agent_calls))]
    if out:
        lines.append("---- 输出 ----\n" + out)
    else:
        lines.append("---- 输出 ----\n（没有 print 输出）")
    if workflows:
        lines.append("---- 工作流 ----\n" + "\n".join(
            "- %s %s（%d 步）" % w for w in workflows[:12]))
    if agent_calls:
        lines.append("---- 子代理 ----\n" + "\n".join(
            "- %s%s" % ("、".join(n) or "?", "" if ok else "（失败）")
            for n, ok in agent_calls[:12]))
    if calls:
        shown = calls[:code_mode.MAX_SUMMARY]
        lines.append("---- 工具调用 ----\n" + "\n".join(
            "- %s%s → %s" % (n, "" if ok else "（失败）", s) for n, ok, _l, s in shown))
    if err:
        lines.append("---- 错误输出 ----\n" + err)
    if aborted["v"]:
        lines.append("（%s）" % ("用户已停止" if (stop_event is not None and stop_event.is_set())
                                 else "超过 %d 秒超时" % timeout))
    return "\n\n".join(lines), {"calls": len(calls), "workflows": len(workflows),
                                "agents": len(agent_calls),
                                "seconds": cost, "aborted": aborted["v"],
                                "exit_code": rc, "ok": not failed}
