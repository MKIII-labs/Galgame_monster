# -*- coding: utf-8 -*-
"""研究任务评测：把「检索-执行是否交错」变成可机检的指标。

不跑模型、不判文风，只回答四个问题：
1. 检索够不够（must_search）；
2. 写文件之后还有没有补搜（复刻类任务的核心）；
3. 正文有没有来源（URL / [n]），引用的编号是否真实存在；
4. key_facts 覆盖了多少（粗粒度关键词命中，只用于发现明显跑偏）。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_PATH = os.path.join(ROOT, "resources", "eval", "research_tasks.json")

WRITE_TOOLS = {"write_file", "edit_file", "make_dir", "move_file", "delete_file",
               "lsp_rename"}
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
_URL_RE = re.compile(r"https?://[^\s\)\]\"']+")
_NUM_LINE_RE = re.compile(r"^\[(\d{1,3})\]\s")


def load_tasks(path=None):
    with open(path or TASKS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def validate(data):
    """返回错误列表；空列表 = 结构合法。"""
    errors = []
    if not isinstance(data, dict):
        return ["评测集必须是 JSON 对象"]
    tasks = data.get("tasks")
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
        elif tid in seen:
            errors.append("id 重复：%s" % tid)
        seen.add(tid)
        if not str(t.get("prompt") or "").strip():
            errors.append("%s 缺少 prompt" % (tid or i))
        if not isinstance(t.get("key_facts"), list) or not t["key_facts"]:
            errors.append("%s 缺少 key_facts" % (tid or i))
        try:
            if int(t.get("must_search") or 0) < 1:
                errors.append("%s 的 must_search 必须 >= 1" % (tid or i))
        except (TypeError, ValueError):
            errors.append("%s 的 must_search 不是整数" % (tid or i))
        if t.get("kind") not in ("replicate", "research"):
            errors.append("%s 的 kind 只能是 replicate / research" % (tid or i))
    return errors


def _tool_calls(messages):
    """按顺序产出 (消息下标, tool 条目)。"""
    for mi, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        for t in (m.get("tools") or []):
            if isinstance(t, dict):
                yield mi, t


def _assistant_text(messages):
    return "\n".join(str(m.get("content") or "") for m in (messages or [])
                     if isinstance(m, dict) and m.get("role") == "assistant")


def _fact_terms(fact):
    """把一条 key_fact 切成可匹配的片段：中文按标点、英文按空格。"""
    raw = re.split(r"[\s,，。；;、/|（）()：:\-—]+", str(fact or ""))
    out = []
    for x in raw:
        x = x.strip().lower()
        if len(x) >= 2 and x not in out:
            out.append(x)
    return out


def _coverage(key_facts, text):
    text = str(text or "").lower()
    if not key_facts:
        return 0.0
    scores = []
    for fact in key_facts:
        terms = _fact_terms(fact)
        if not terms:
            continue
        hit = sum(1 for t in terms if t in text)
        scores.append(hit / float(len(terms)))
    return sum(scores) / len(scores) if scores else 0.0


def _extract_sources(calls):
    """从 web_search 的 result 里提取来源编号与 URL。"""
    numbered, urls = set(), set()
    for _mi, t in calls:
        if t.get("tool") != "web_search":
            continue
        text = str(t.get("result") or "")
        for line in text.splitlines():
            m = _NUM_LINE_RE.match(line.strip())
            if m:
                numbered.add(int(m.group(1)))
        for u in _URL_RE.findall(text):
            urls.add(u.rstrip(".,;"))
    return numbered, urls


def evaluate_messages(task, messages):
    """计算机检指标。task 是评测集里的一条任务。"""
    calls = list(_tool_calls(messages))
    search_idx = [i for i, (_mi, t) in enumerate(calls) if t.get("tool") == "web_search"]
    write_idx = [i for i, (_mi, t) in enumerate(calls) if t.get("tool") in WRITE_TOOLS]
    first_write = write_idx[0] if write_idx else -1
    search_after_write = any(i > first_write for i in search_idx) if first_write >= 0 else False

    notes = sum(1 for _mi, t in calls if t.get("tool") == "research_note")
    numbered, urls = _extract_sources(calls)
    text = _assistant_text(messages)
    cites = [int(x) for x in _CITE_RE.findall(text)]
    bad = sorted({n for n in cites if n not in numbered}) if numbered else []
    coverage = _coverage(task.get("key_facts") or [], text)
    flags = [s for s in (task.get("fail_signals") or []) if s and s in text]

    return {
        "search_calls": len(search_idx),
        "write_calls": len(write_idx),
        "first_write_at": first_write,
        "search_after_write": search_after_write,
        "research_notes": notes,
        "source_numbers": sorted(numbered),
        "source_urls": len(urls),
        "citations": len(cites),
        "bad_citations": bad,
        "coverage": round(coverage, 3),
        "fail_flags": flags,
    }


def score(task, metrics):
    """机检评分：不冒充 rubric 的 accuracy / actionability，只给保守的通过判定。"""
    try:
        need = int(task.get("must_search") or 0)
    except (TypeError, ValueError):
        need = 0
    kind = task.get("kind") or "research"
    search_ok = metrics["search_calls"] >= need
    interleave_ok = metrics["search_after_write"] if kind == "replicate" else True
    grounded = metrics["citations"] > 0 or metrics["source_urls"] > 0
    coverage_ok = metrics["coverage"] >= 0.5
    return {
        "search_ok": search_ok,
        "interleave_ok": interleave_ok,
        "grounded": grounded,
        "coverage_ok": coverage_ok,
        "bad_citations": len(metrics["bad_citations"]),
        "pass": bool(search_ok and interleave_ok and grounded and coverage_ok
                     and not metrics["bad_citations"]),
    }


def evaluate(task, messages):
    m = evaluate_messages(task, messages)
    return {"task": task.get("id"), "metrics": m, "score": score(task, m)}


def format_report(result):
    """给 CLI 用的简短 Markdown。"""
    t, m, s = result["task"], result["metrics"], result["score"]
    lines = [
        "## %s" % t,
        "",
        "- 检索：%d 次（写文件后补搜：%s）" % (
            m["search_calls"], "是" if m["search_after_write"] else "否"),
        "- 写操作：%d 次；research_note：%d 次" % (m["write_calls"], m["research_notes"]),
        "- 来源：%d 个 URL / %d 个编号；正文引用 %d 处；坏引用 %d 处" % (
            m["source_urls"], len(m["source_numbers"]), m["citations"],
            len(m["bad_citations"])),
        "- key_facts 覆盖：%.0f%%" % (m["coverage"] * 100),
        "- 通过：%s" % ("是" if s["pass"] else "否"),
    ]
    if m["fail_flags"]:
        lines.append("- 命中失败信号：%s" % "；".join(m["fail_flags"]))
    return "\n".join(lines)
