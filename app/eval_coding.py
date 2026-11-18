# -*- coding: utf-8 -*-
"""编码评测集：fixture 仓库 + 任务定义 + 自动判卷。

两种模式：
- mock：把 task.solution_files 写进 fixture 副本再跑测试。零 API 成本，
  用来验证「题目本身有效 + 判卷正确」，适合进日常自检。
- live：让真实 Agent 在临时 workspace 里改代码，再跑测试判卷。消耗 token，
  用来比较提示词 / 工具集 / provider 的差异。

任务 JSON（evals/coding/tasks/*.json）：
  name / fixture / prompt / test / solution_files / expect
  expect: tests_pass / pre_test_fails / changed_files / forbid_changed
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from app import agent, llm
from app.config import get_api_conf
from app.context import TOOL_MSG_PREFIX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(ROOT, "evals", "coding")
TASKS_DIR = os.path.join(EVAL_DIR, "tasks")
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIX = (".pyc", ".pyo")

# 编码任务只挂这几项工具，避免评测被大而全的工具目录干扰
CODING_TOOLS = ("list_dir", "read_file", "search_files", "search_code",
                "write_file", "edit_file", "multi_edit", "apply_patch",
                "run_command")


def _fixture_path(task):
    path = str(task.get("fixture") or "")
    return path if os.path.isabs(path) else os.path.join(EVAL_DIR, path)


def load_tasks(tasks_dir=TASKS_DIR, only=None):
    """读取并校验任务；only 是任务名集合。"""
    names = set(only or [])
    out = []
    try:
        files = sorted(os.listdir(tasks_dir))
    except OSError as e:
        raise ValueError("读不到任务目录 %s：%s" % (tasks_dir, e))
    for name in files:
        if not name.endswith(".json"):
            continue
        path = os.path.join(tasks_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                task = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError("任务无法解析 %s：%s" % (name, e))
        missing = [k for k in ("name", "fixture", "prompt", "test") if not task.get(k)]
        if missing:
            raise ValueError("任务 %s 缺少字段：%s" % (name, ", ".join(missing)))
        if names and task["name"] not in names:
            continue
        if not os.path.isdir(_fixture_path(task)):
            raise ValueError("任务 %s 的 fixture 不存在：%s" % (task["name"], task["fixture"]))
        out.append(task)
    return out


def prepare_workspace(task):
    """把 fixture 复制到临时目录，返回路径。"""
    src = _fixture_path(task)
    dst = tempfile.mkdtemp(prefix="gm-eval-")
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    return dst


def snapshot(root):
    """{相对路径: sha1}，忽略缓存目录与 .pyc。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(SKIP_SUFFIX):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as f:
                    digest = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            out[rel] = digest
    return out


def run_command(task, workspace, timeout=120):
    """跑任务里的测试命令；python 换成当前解释器，保证用项目 venv。"""
    cmd = [str(x) for x in (task.get("test") or [])]
    if not cmd:
        return {"code": -1, "output": "任务没有 test 命令"}
    if cmd[0].lower() in ("python", "python3", "py"):
        cmd[0] = sys.executable
    try:
        proc = subprocess.run(cmd, cwd=workspace, capture_output=True,
                              text=True, timeout=timeout)
        return {"code": proc.returncode,
                "output": ((proc.stdout or "") + (proc.stderr or ""))[-4000:]}
    except subprocess.TimeoutExpired:
        return {"code": -1, "output": "测试超时（%ss）" % timeout}
    except OSError as e:
        return {"code": -1, "output": "测试无法启动：%s" % e}


def apply_solution(task, workspace):
    for rel, text in (task.get("solution_files") or {}).items():
        path = os.path.join(workspace, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(text))


def grade(task, workspace, before, test_result, metrics=None):
    """判卷：测试通过 + 只改预期文件 + 没动禁止文件。"""
    expect = task.get("expect") or {}
    after = snapshot(workspace)
    changed = sorted(k for k in after if before.get(k) != after[k])
    deleted = sorted(set(before) - set(after))
    reasons = []

    if expect.get("tests_pass", True) and test_result.get("code") != 0:
        reasons.append("测试未通过（exit %s）" % test_result.get("code"))
    for path in deleted:
        reasons.append("删除了文件：%s" % path)
    forbid = [str(x) for x in (expect.get("forbid_changed") or []) if str(x)]
    for path in changed:
        if any(path.startswith(p) for p in forbid):
            reasons.append("改了禁止修改的文件：%s" % path)
    expected = [str(x) for x in (expect.get("changed_files") or []) if str(x)]
    if expected:
        unexpected = sorted(set(changed) - set(expected))
        if unexpected:
            reasons.append("改了预期之外的文件：%s" % "、".join(unexpected))
        missing = sorted(set(expected) - set(changed))
        if missing:
            reasons.append("预期修改但没改：%s" % "、".join(missing))

    return {
        "ok": not reasons,
        "reasons": reasons,
        "changed": changed,
        "deleted": deleted,
        "test_code": test_result.get("code"),
        "test_output": test_result.get("output") or "",
        "metrics": metrics or {},
    }


def _base_result(task, mode="mock"):
    return {"name": task.get("name") or "", "mode": mode, "ok": False,
            "reasons": [], "changed": [], "deleted": [], "metrics": {}}


def run_mock(task):
    """零 API 成本：用参考解验证题目与判卷本身。"""
    result = _base_result(task, "mock")
    workspace = prepare_workspace(task)
    try:
        before = snapshot(workspace)
        expect = task.get("expect") or {}
        pre = None
        if expect.get("pre_test_fails"):
            pre = run_command(task, workspace)
            if pre["code"] == 0:
                result["reasons"].append("题目无效：修复前测试已经通过")
        apply_solution(task, workspace)
        test = run_command(task, workspace)
        graded = grade(task, workspace, before, test, {"mode": "mock"})
        result.update(graded)
        result["reasons"] = result["reasons"] + graded.get("reasons", [])
        result["ok"] = not result["reasons"]
        return result
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_agent(workspace, prompt, test_cmd="", max_rounds=10, timeout=300):
    """让真实 Agent 在 workspace 里改代码；返回执行指标。"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return {"error": "尚未配置聊天 API 密钥"}
    tools = agent.native_tool_schemas(
        agent_on=True, search_ready=False, cap_domains=set(),
        allowed_tools=set(CODING_TOOLS))
    system = (
        "你是一个编码 Agent。工作目录是 %s；工具里的 path 用相对路径。\n"
        "按用户要求修改仓库，不要修改 test_*.py；结束前必须运行测试确认通过。\n"
        "测试命令：%s\n完成后用一两句话说明改了什么。"
        % (workspace, test_cmd or "（任务未指定）"))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]
    started = time.time()
    rounds = calls = tool_errors = 0
    verified = False
    usage = {"prompt": 0, "completion": 0, "total": 0}
    text = ""
    while rounds < max(1, int(max_rounds or 1)):
        left = timeout - (time.time() - started)
        if left <= 5:
            break
        try:
            rr = llm.stream_chat(api_key, api_url, api_model, messages,
                                 tools=tools,
                                 timeout=(10, max(5, min(120, int(left)))))
        except Exception as e:                     # noqa: BLE001
            return {"error": "模型调用失败：%s" % e, "rounds": rounds,
                    "tool_calls": calls, "tool_errors": tool_errors,
                    "verified": verified, "usage": usage}
        u = rr.get("usage") or {}
        for key in usage:
            try:
                usage[key] += int(u.get(key) or 0)
            except (TypeError, ValueError):
                pass
        text = rr.get("visible") or ""
        tool_calls = rr.get("tool_calls") or []
        if text:
            messages.append({"role": "assistant", "content": text})
        if not tool_calls:
            break
        results = []
        for call in tool_calls:
            name = str(call.get("tool") or "")
            args = call.get("args") or {}
            calls += 1
            if name == "run_command":
                verified = True
            try:
                out, _undo, _write = agent.run_tool(
                    workspace, name, args,
                    caps={"cmd_timeout": 120, "require_read": False})
            except Exception as e:                 # noqa: BLE001
                out = "❌ %s" % e
            if str(out).lstrip().startswith("❌"):
                tool_errors += 1
            results.append("【%s】\n%s" % (name, out))
        for item in results:
            messages.append({"role": "user",
                             "content": TOOL_MSG_PREFIX + "\n" + item})
        rounds += 1
    return {"text": text, "rounds": rounds, "tool_calls": calls,
            "tool_errors": tool_errors, "verified": verified,
            "usage": usage}


def run_live(task, max_rounds=10, timeout=300):
    """真实 Agent 跑一遍再判卷（消耗 token）。"""
    result = _base_result(task, "live")
    workspace = prepare_workspace(task)
    try:
        before = snapshot(workspace)
        expect = task.get("expect") or {}
        if expect.get("pre_test_fails"):
            pre = run_command(task, workspace)
            if pre["code"] == 0:
                result["reasons"].append("题目无效：修复前测试已经通过")
        run = run_agent(workspace, task.get("prompt") or "",
                        test_cmd=" ".join(str(x) for x in (task.get("test") or [])),
                        max_rounds=max_rounds, timeout=timeout)
        if run.get("error"):
            result["reasons"].append(run["error"])
        test = run_command(task, workspace)
        metrics = {
            "rounds": run.get("rounds", 0),
            "tool_calls": run.get("tool_calls", 0),
            "tool_errors": run.get("tool_errors", 0),
            "verified": bool(run.get("verified")),
            "tokens": (run.get("usage") or {}).get("total", 0),
        }
        graded = grade(task, workspace, before, test, metrics)
        result.update(graded)
        result["reasons"] = result["reasons"] + graded.get("reasons", [])
        result["ok"] = not result["reasons"]
        return result
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
