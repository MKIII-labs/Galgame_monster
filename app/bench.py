# -*- coding: utf-8 -*-
"""评测跑分：把「同一个任务」交给 Agent 跑一遍，再用机器可检的断言打分。

设计边界（重要）：
- 评测任务只存在于 `resources/eval/bench_tasks.json`，普通对话不会读取；
- 跑分使用独立临时工作区与独立系统提示，不写 `data/config.json`、不写会话；
- 断言语义是通用的（文件 / 命令 / 视觉指标 / 工具轨迹），任务特有指标由任务自己声明；
- 消融只过滤工具与临时配置，不改产品行为。

驱动方式：内部循环（llm + agent.run_tool），不经过 HTTP / SSE。
与线上链路的差异：没有审批弹窗、没有历史压缩、没有记忆与画像 —— 这些都不属于
「模型能不能完成任务」的核心变量，评测时固定住反而更容易复现。
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch

from app import agent, llm
from app.config import get_api_conf, load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_PATH = os.path.join(ROOT, "resources", "eval", "bench_tasks.json")
FIXTURES_DIR = os.path.join(ROOT, "resources", "eval", "fixtures")
RUNS_DIR = os.path.join(ROOT, "data", "bench", "runs")

TEXT_LIMIT = 2 * 1024 * 1024        # 断言读文件的上限
TOOL_RESULT_CLIP = 8000             # 回灌给模型的单条工具结果上限
MAX_CALLS_PER_ROUND = 8

# 消融：只影响「模型能看到哪些工具」，不改产品配置
ABLATION_TOOLS = {
    "no_visual": {"visual_check", "render_check", "see_image",
                  "audio_probe", "audio_compare"},
    "no_assets": {"asset_scan", "asset_convert", "asset_atlas",
                  "asset_inline", "asset_validate"},
    "no_code_mode": {"run_script", "run_workflow_script"},
    "no_acceptance": {"acceptance", "update_plan", "research_note"},
    "no_subagent": {"explore", "explore_followup", "explore_status",
                    "explore_result", "explore_cancel"},
}
ABLATIONS = tuple(ABLATION_TOOLS) + ("no_mcp", "no_hooks")

_FALLBACK_SYSTEM = (
    "你是一个能在沙箱里读写文件、执行命令的编程助手。"
    "路径一律用相对沙箱根的相对路径；不要编造工具结果；任务做完再给结论。")


# ------------------------------------------------------------------ 任务集

def load_tasks(path=None):
    with open(path or TASKS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [t for t in (data.get("tasks") or []) if isinstance(t, dict)]


def get_task(task_id, path=None):
    tid = str(task_id or "").strip()
    for t in load_tasks(path):
        if str(t.get("id") or "") == tid:
            return t
    return None


def validate(data=None):
    """校验任务集结构；返回错误列表（空 = 合法）。"""
    errors = []
    if data is None:
        try:
            with open(TASKS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return ["任务集无法读取：%s" % e]
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return ["缺少 tasks 数组"]
    seen = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errors.append("tasks[%d] 不是对象" % i)
            continue
        tid = str(t.get("id") or "").strip()
        if not tid:
            errors.append("tasks[%d] 缺少 id" % i)
            continue
        if tid in seen:
            errors.append("id 重复：%s" % tid)
        seen.add(tid)
        if not str(t.get("prompt") or "").strip():
            errors.append("%s 缺少 prompt" % tid)
        fixture = str(t.get("fixture") or tid)
        if not os.path.isdir(os.path.join(FIXTURES_DIR, fixture)):
            errors.append("%s 的 fixture 目录不存在：%s" % (tid, fixture))
        exp = t.get("expect")
        if not isinstance(exp, dict) or not exp:
            errors.append("%s 缺少 expect" % tid)
        for key in ("max_turns", "timeout"):
            try:
                if int(t.get(key) or 0) <= 0:
                    errors.append("%s 的 %s 必须 > 0" % (tid, key))
            except (TypeError, ValueError):
                errors.append("%s 的 %s 不是整数" % (tid, key))
    return errors


def dry_run(path=None):
    """只做结构 / fixture 校验，不调用模型。"""
    tasks = load_tasks(path)
    problems = validate({"tasks": tasks} if path else None)
    out = []
    for t in tasks:
        tid = t.get("id")
        local = [p for p in problems if str(p).startswith(str(tid))]
        fixture = str(t.get("fixture") or tid)
        src = os.path.join(FIXTURES_DIR, fixture)
        files = []
        for base, _dirs, names in os.walk(src):
            for n in names:
                files.append(os.path.relpath(os.path.join(base, n), src).replace("\\", "/"))
        out.append({"task": tid, "ok": not local, "problems": local,
                    "fixture": fixture, "fixture_files": sorted(files)})
    return out


# ------------------------------------------------------------------ 跑分

def ablation_disable_tools(ablations):
    disabled = set()
    for name in ablations or []:
        disabled |= ABLATION_TOOLS.get(str(name), set())
    return disabled


def _prepare_workdir(task, workdir):
    shutil.copytree(os.path.join(FIXTURES_DIR, str(task.get("fixture") or task["id"])),
                    workdir, dirs_exist_ok=True)


def _system_prompt(workdir):
    s = {"id": "bench", "messages": [], "agent_enabled": True, "sandbox": workdir}
    try:
        tool_text = agent.build_agent_prompt(
            s, search_ready=False, native_tools=True, cap_domains=set())
        from app import prompt_assembly as pa
        fixed, dyn, _n = pa.base_system_parts(
            s, "", tool_text=tool_text, include_kbs=False, skip_profile=True)
        text = "\n\n".join(x for x in (fixed, dyn) if x).strip()
        if text:
            return text
    except Exception:                        # noqa: BLE001  装配失败就用兜底提示
        pass
    return _FALLBACK_SYSTEM


def _schemas(ablations):
    disabled = ablation_disable_tools(ablations)
    out = []
    for item in agent.native_tool_schemas(agent_on=True, search_ready=True,
                                          cap_domains=set()):
        name = str((item.get("function") or {}).get("name") or "")
        if name and name not in disabled:
            out.append(item)
    return out


def _ablation_stack(ablations, cfg):
    """no_mcp / no_hooks 需要改运行期读配置的函数；用上下文管理器保证恢复。"""
    stack = ExitStack()
    if "no_mcp" in (ablations or []):
        from app import mcp
        frozen = dict(cfg, mcp_enabled=False)
        stack.enter_context(patch.object(mcp, "_cfg", lambda: frozen))
    if "no_hooks" in (ablations or []):
        from app import hooks
        stack.enter_context(patch.object(hooks, "_cfg", lambda: {"hooks_enabled": False}))
    return stack


def run_task(task, model="", cfg=None, ablations=(), max_turns=None, timeout=None,
             keep=False, workdir=None):
    """跑一个任务；返回结果 dict（可直接 JSON 序列化）。"""
    task = task if isinstance(task, dict) else get_task(task)
    if not task:
        return {"task": str(task), "status": "error", "ok": False,
                "checks": [], "error": "找不到任务"}
    tid = str(task.get("id") or "")
    cfg = dict(cfg or load_config() or {})
    limit = max(1, int(max_turns or task.get("max_turns") or 12))
    budget = max(30, int(timeout or task.get("timeout") or 300))
    api_key, api_url, api_model = get_api_conf("chat")
    model = str(model or api_model or "")
    started = time.time()
    base = {"task": tid, "title": task.get("title") or tid,
            "model": model, "ablations": list(ablations or []),
            "status": "error", "ok": False, "checks": [], "turns": 0,
            "tool_calls": 0, "usage": {}, "ms": 0, "final": "", "tools": []}
    if not api_key:
        return dict(base, status="no_api_key", error="尚未配置聊天 API 密钥")

    tmp = workdir or tempfile.mkdtemp(prefix="gm_bench_%s_" % (tid or "task"))
    _prepare_workdir(task, tmp)
    caps = agent.limits(cfg)
    messages = [
        {"role": "system", "content": _system_prompt(tmp)},
        {"role": "user", "content":
            "工作目录（沙箱根）：%s\n工具里的 path 请用相对路径。\n\n任务：\n%s"
            % (tmp, str(task.get("prompt") or ""))},
    ]
    tools_log, turns, final, error = [], 0, "", ""
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    status = "max_turns"
    deadline = started + budget
    try:
        with _ablation_stack(ablations, cfg):
            schemas = _schemas(ablations)
            while turns < limit:
                left = deadline - time.time()
                if left <= 5:
                    status = "timeout"
                    break
                rr = llm.stream_chat(
                    api_key, api_url, model, messages, tools=schemas,
                    timeout=(10, max(5, min(120, int(left)))))
                usage = rr.get("usage") or {}
                for k in usage_total:
                    try:
                        usage_total[k] += int(usage.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
                visible = str(rr.get("visible") or "")
                calls = rr.get("tool_calls") or []
                if not calls:
                    final = visible
                    state = str(rr.get("stream_state") or "")
                    status = "error" if state in ("error", "timeout", "idle") else "finished"
                    if status == "error":
                        error = str(rr.get("diag") or state)[:500]
                    break
                messages.append({"role": "assistant",
                                 "content": visible or "（发起工具调用）"})
                results = []
                for call in calls[:MAX_CALLS_PER_ROUND]:
                    name = str((call or {}).get("tool") or "").strip()
                    cargs = (call or {}).get("args")
                    cargs = cargs if isinstance(cargs, dict) else {}
                    t0 = time.time()
                    try:
                        t_text, _undo, is_write = agent.run_tool(
                            tmp, name, cargs, caps=caps)
                    except Exception as e:       # noqa: BLE001  单个工具失败不终止任务
                        t_text, is_write = "❌ 工具执行异常：%s" % e, False
                    t_text = str(t_text)
                    tools_log.append({
                        "tool": name, "args": cargs,
                        "ok": not t_text.lstrip().startswith("❌"),
                        "write": bool(is_write),
                        "ms": int((time.time() - t0) * 1000),
                        "result": t_text[:500]})
                    results.append("【%s】\n%s" % (name, t_text[:TOOL_RESULT_CLIP]))
                messages.append({"role": "user",
                                 "content": "工具执行结果如下，请据此继续：\n\n"
                                            + "\n\n".join(results)})
                turns += 1
    except Exception as e:                   # noqa: BLE001  网络 / 解析异常都记为失败
        status, error = "error", str(e)[:500]
    checks = evaluate(task, tmp, {"tools": tools_log, "final": final})
    ok = all(c["ok"] for c in checks if not c.get("skipped"))
    ms = int((time.time() - started) * 1000)
    out = dict(base, status=status, ok=ok, checks=checks, turns=turns,
               tool_calls=len(tools_log), usage=usage_total, ms=ms,
               final=final[-4000:], tools=tools_log,
               workdir=tmp if keep else "")
    if error:
        out["error"] = error
    if not keep and not workdir:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ------------------------------------------------------------------ 断言

def _safe_join(root, rel):
    p = os.path.abspath(os.path.join(root, str(rel or "")))
    root_abs = os.path.abspath(root)
    if p != root_abs and not p.startswith(root_abs + os.sep):
        raise ValueError("路径越界：%s" % rel)
    return p


def _fail(name, detail, skipped=False):
    return {"name": name, "ok": False, "skipped": bool(skipped), "detail": detail}


def _ok(name, detail=""):
    return {"name": name, "ok": True, "skipped": False, "detail": detail}


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _check_file(workdir, spec):
    rel = str(spec.get("path") or "")
    name = "file:%s" % rel
    try:
        p = _safe_join(workdir, rel)
    except ValueError as e:
        return _fail(name, str(e))
    if not os.path.isfile(p):
        return _fail(name, "文件不存在")
    size = os.path.getsize(p)
    if "min_bytes" in spec and size < int(spec["min_bytes"]):
        return _fail(name, "只有 %d 字节（要求 ≥ %s）" % (size, spec["min_bytes"]))
    text = ""
    if size <= TEXT_LIMIT:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return _fail(name, "读取失败：%s" % e)
    for want in _as_list(spec.get("text_contains")):
        if str(want) not in text:
            return _fail(name, "缺少 %r" % want)
    for bad in _as_list(spec.get("text_not_contains")):
        if str(bad) in text:
            return _fail(name, "仍包含 %r" % bad)
    if "json_keys" in spec:
        try:
            data = json.loads(text)
        except ValueError:
            return _fail(name, "不是合法 JSON")
        if not isinstance(data, dict):
            return _fail(name, "JSON 顶层不是对象")
        if len(data) < int(spec["json_keys"]):
            return _fail(name, "只有 %d 个键（要求 ≥ %s）" % (len(data), spec["json_keys"]))
    img = spec.get("image")
    if isinstance(img, dict):
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
                mode = im.mode
        except Exception as e:               # noqa: BLE001
            return _fail(name, "图片打不开：%s" % e)
        if "width_min" in img and w < int(img["width_min"]):
            return _fail(name, "宽度 %d（要求 ≥ %s）" % (w, img["width_min"]))
        if "height_min" in img and h < int(img["height_min"]):
            return _fail(name, "高度 %d（要求 ≥ %s）" % (h, img["height_min"]))
        if img.get("alpha") and mode not in ("RGBA", "LA", "P"):
            return _fail(name, "没有透明通道（mode=%s）" % mode)
    return _ok(name, "%d 字节" % size)


def _check_command(workdir, spec):
    argv = [str(x) for x in (spec.get("argv") or [])]
    name = "cmd:%s" % " ".join(argv)
    if not argv:
        return _fail(name, "argv 为空")
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable          # 用当前解释器，避免 PATH 里是另一个环境
    try:
        p = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=int(spec.get("timeout") or 120))
    except subprocess.TimeoutExpired:
        return _fail(name, "命令超时")
    except OSError as e:
        return _fail(name, "命令启动失败：%s" % e)
    out = ((p.stdout or "") + (p.stderr or ""))[-1500:]
    want = spec.get("exit")
    if want is not None and p.returncode != int(want):
        return _fail(name, "退出码 %d（期望 %s）\n%s" % (p.returncode, want, out))
    for text in _as_list(spec.get("contains")):
        if str(text) not in out:
            return _fail(name, "输出缺少 %r" % text)
    for text in _as_list(spec.get("not_contains")):
        if str(text) in out:
            return _fail(name, "输出仍包含 %r" % text)
    return _ok(name, "退出码 %d" % p.returncode)


def _metrics(text):
    """与 chat.py 的 _acceptance_metrics 同一约定：marker 开头的单行 JSON。"""
    for line in str(text or "").splitlines():
        for marker in ("__VISUAL_METRICS__", "__ASSET_METRICS__"):
            if line.startswith(marker):
                try:
                    obj = json.loads(line[len(marker):].strip())
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    return obj
    return {}


_VISUAL_CHECKS = (
    ("fps_min", "fps", ">="),
    ("fps_max", "fps", "<="),
    ("console_errors_max", "console_errors", "<="),
    ("resource_errors_max", "resource_errors", "<="),
    ("draw_calls_max", "draw_calls", "<="),
    ("draw_calls_min", "draw_calls", ">="),
    ("sharpness_min", "sharpness", ">="),
    ("contrast_min", "contrast", ">="),
    ("blank_score_max", "blank_score", "<="),
)


def _check_visual(workdir, spec):
    rel = str(spec.get("path") or "")
    name = "visual:%s" % rel
    try:
        _safe_join(workdir, rel)
    except ValueError as e:
        return _fail(name, str(e))
    args = {"path": rel, "screenshot": True}
    if spec.get("viewport"):
        args["viewport"] = str(spec["viewport"])
    text, _undo, _w = agent.run_tool(workdir, "visual_check", args)
    text = str(text)
    if text.lstrip().startswith("❌"):
        # 没有可用的无头浏览器 / 浏览器启动失败：标记为跳过，不算模型失败
        return _fail(name, text[:300], skipped=True)
    metrics = _metrics(text)
    for key, metric, op in _VISUAL_CHECKS:
        if key not in spec:
            continue
        if metric not in metrics or metrics.get(metric) is None:
            return _fail(name, "没有指标 %s" % metric)
        try:
            want = float(spec[key])
            got = float(metrics[metric])
        except (TypeError, ValueError):
            return _fail(name, "%s 不是数字" % key)
        if (op == ">=" and got < want) or (op == "<=" and got > want):
            return _fail(name, "%s=%s 未达 %s %s" % (metric, metrics[metric], op, spec[key]))
    return _ok(name, json.dumps(metrics, ensure_ascii=False)[:300])


def _check_tools(spec, result):
    used = [str(t.get("tool") or "") for t in (result.get("tools") or [])]
    name = "tools"
    missing = [x for x in (spec.get("require") or []) if x not in used]
    want_any = list(spec.get("require_any") or [])
    if missing:
        return _fail(name, "没有调用：%s（实际：%s）" % ("、".join(missing), "、".join(used) or "无"))
    if want_any and not any(x in used for x in want_any):
        return _fail(name, "至少需要调用其中之一：%s" % "、".join(want_any))
    if "min" in spec and len(used) < int(spec["min"]):
        return _fail(name, "只调用 %d 次（要求 ≥ %s）" % (len(used), spec["min"]))
    return _ok(name, "调用 %d 次" % len(used))


def _check_final(spec, result):
    text = str(result.get("final") or "")
    name = "final"
    for want in _as_list(spec.get("contains")):
        if str(want) not in text:
            return _fail(name, "收尾正文缺少 %r" % want)
    for bad in _as_list(spec.get("not_contains")):
        if str(bad) in text:
            return _fail(name, "收尾正文仍包含 %r" % bad)
    if spec.get("regex"):
        try:
            if not re.search(str(spec["regex"]), text):
                return _fail(name, "正则未命中")
        except re.error:
            return _fail(name, "正则非法")
    return _ok(name)


def evaluate(task, workdir, result):
    exp = task.get("expect") if isinstance(task, dict) else {}
    exp = exp if isinstance(exp, dict) else {}
    checks = []
    for spec in exp.get("files") or []:
        if isinstance(spec, dict):
            checks.append(_check_file(workdir, spec))
    for spec in _as_list(exp.get("command")):
        if isinstance(spec, dict):
            checks.append(_check_command(workdir, spec))
    if isinstance(exp.get("visual"), dict):
        checks.append(_check_visual(workdir, exp["visual"]))
    if isinstance(exp.get("tool_calls"), dict):
        checks.append(_check_tools(exp["tool_calls"], result))
    if isinstance(exp.get("final"), dict):
        checks.append(_check_final(exp["final"], result))
    return checks


# ------------------------------------------------------------------ 汇总 / 落盘

def summarize(results, meta=None):
    results = [r for r in (results or []) if isinstance(r, dict)]
    total = len(results)
    runnable = [r for r in results if r.get("status") != "no_api_key"]
    passed = [r for r in runnable if r.get("ok")]
    skipped = sum(1 for r in runnable for c in (r.get("checks") or []) if c.get("skipped"))
    # 消融对比：同一批任务按「基线 / 单消融」分组，直接回答哪一层在起作用
    by_ablation = {}
    for r in runnable:
        key = ",".join(r.get("ablations") or []) or "baseline"
        g = by_ablation.setdefault(key, {"total": 0, "passed": 0})
        g["total"] += 1
        if r.get("ok"):
            g["passed"] += 1
    for g in by_ablation.values():
        g["pass_rate"] = round(g["passed"] / g["total"], 4) if g["total"] else 0.0
    return {
        "meta": dict(meta or {}),
        "total": total,
        "runnable": len(runnable),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(runnable), 4) if runnable else 0.0,
        "skipped_checks": skipped,
        "tokens": sum(int((r.get("usage") or {}).get("total_tokens") or 0)
                      for r in runnable),
        "ms": sum(int(r.get("ms") or 0) for r in runnable),
        "by_ablation": by_ablation,
        "tasks": [{
            "task": r.get("task"), "status": r.get("status"), "ok": bool(r.get("ok")),
            "turns": r.get("turns"), "tool_calls": r.get("tool_calls"),
            "tokens": int((r.get("usage") or {}).get("total_tokens") or 0),
            "ms": r.get("ms"),
            "failed_checks": [c.get("name") for c in (r.get("checks") or [])
                              if not c.get("ok")],
            "ablations": r.get("ablations") or [],
        } for r in results],
    }


def save_run(results, meta=None, root=None):
    run_id = str((meta or {}).get("run_id") or time.strftime("%Y%m%d-%H%M%S"))
    d = os.path.join(root or RUNS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    for r in results or []:
        tid = str(r.get("task") or "task")
        with open(os.path.join(d, tid + ".json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    summary = summarize(results, dict(meta or {}, run_id=run_id))
    with open(os.path.join(d, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
        f.write(_summary_md(summary))
    return d, summary


def _summary_md(summary):
    lines = ["# 评测结果 %s" % (summary.get("meta") or {}).get("run_id", ""), "",
             "- 模型：%s" % (summary.get("meta") or {}).get("model", ""),
             "- 通过：%d / %d（%s）" % (summary.get("passed", 0),
                                       summary.get("runnable", 0),
                                       summary.get("pass_rate")),
             "- token：%d · 耗时：%.1fs" % (summary.get("tokens", 0),
                                            (summary.get("ms", 0) or 0) / 1000.0),
             "", "| 任务 | 结果 | 轮次 | 工具 | token | 未通过项 |",
             "|---|---|---|---|---|---|"]
    for t in summary.get("tasks") or []:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            t.get("task"), "✓" if t.get("ok") else t.get("status"),
            t.get("turns"), t.get("tool_calls"), t.get("tokens"),
            "、".join(t.get("failed_checks") or []) or "-"))
    groups = summary.get("by_ablation") or {}
    if len(groups) > 1:
        lines += ["", "## 消融对比", "", "| 组 | 通过 | 总数 | 通过率 |",
                  "|---|---|---|---|"]
        for key in sorted(groups):
            g = groups[key]
            lines.append("| %s | %d | %d | %s |"
                         % (key, g["passed"], g["total"], g["pass_rate"]))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 盲评

def _load_run(path):
    data = {}
    for name in os.listdir(path):
        if not name.endswith(".json") or name == "summary.json":
            continue
        try:
            with open(os.path.join(path, name), "r", encoding="utf-8") as f:
                item = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("task"):
            data[str(item["task"])] = item
    return data


def render_review(run_a, run_b, out_path, seed=7):
    """把两次运行匿名成 A/B，输出自包含 HTML 评分页。"""
    a, b = _load_run(run_a), _load_run(run_b)
    tasks = sorted(set(a) | set(b))
    rng = random.Random(seed)
    cards = []
    for tid in tasks:
        left, right = ("A", "B") if rng.random() < 0.5 else ("B", "A")
        pair = {"A": a.get(tid) or {}, "B": b.get(tid) or {}}
        cards.append(_review_card(tid, left, pair[left], right, pair[right]))
    html = _review_html(cards)
    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def _review_card(tid, a_label, a, b_label, b):
    def block(label, item):
        checks = item.get("checks") or []
        bad = [c.get("name") for c in checks if not c.get("ok")]
        return (
            '<div class="side"><h3>方案 %s</h3>'
            '<p class="meta">%s · 轮次 %s · 工具 %s · token %s</p>'
            '<p class="verdict">%s</p>'
            '<pre class="final">%s</pre>'
            '<details><summary>工具轨迹</summary><pre>%s</pre></details>'
            '<p class="bad">%s</p></div>'
            % (label, "通过" if item.get("ok") else item.get("status") or "未运行",
               item.get("turns") or "-", item.get("tool_calls") or "-",
               int((item.get("usage") or {}).get("total_tokens") or 0),
               "✓ 机检通过" if item.get("ok") else "✗ " + ("、".join(bad) or "未通过"),
               _esc(str(item.get("final") or "")[:2000]),
               _esc("\n".join("%s(%s) %s" % (t.get("tool"),
                                             "ok" if t.get("ok") else "fail",
                                             json.dumps(t.get("args") or {},
                                                        ensure_ascii=False)[:120])
                              for t in (item.get("tools") or [])[:40])),
               _esc("、".join(bad) or "-")))
    return ('<section class="task"><h2>%s</h2><div class="pair">%s%s</div>'
            '<div class="score"><label>A 更好 <input type="radio" name="%s" value="A"></label>'
            '<label>平手 <input type="radio" name="%s" value="tie"></label>'
            '<label>B 更好 <input type="radio" name="%s" value="B"></label></div>'
            '<textarea placeholder="评语（可选）"></textarea></section>'
            % (_esc(tid), block(a_label, a), block(b_label, b), tid, tid, tid))


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _review_html(cards):
    tpl = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>盲评对比</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1100px;margin:32px auto;padding:0 18px;background:#0f1115;color:#e6e6e6}
h1{font-size:22px}h2{font-size:17px;margin:0 0 10px}
.task{border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:0 0 18px;background:#151922}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.side{background:#10141b;border:1px solid #232936;border-radius:10px;padding:12px}
.side h3{margin:0 0 6px;font-size:15px}
.meta{color:#8b93a1;font-size:12px;margin:0 0 6px}
.verdict{font-weight:600;margin:0 0 8px}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#0b0e13;border-radius:8px;padding:10px;max-height:260px;overflow:auto}
.bad{color:#ff9a9a;font-size:12px}
.score{margin-top:12px;display:flex;gap:18px;font-size:13px}
textarea{width:100%;margin-top:8px;min-height:48px;background:#0b0e13;color:#e6e6e6;border:1px solid #2a2f3a;border-radius:8px}
</style></head><body>
<h1>盲评对比（A/B 顺序已隐藏）</h1>
<p class="meta">只根据产物与过程判断哪个更好；需要时再揭晓 A/B 对应哪次运行。</p>
__CARDS__
</body></html>"""
    return tpl.replace("__CARDS__", "\n".join(cards))
