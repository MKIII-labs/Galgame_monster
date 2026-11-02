# -*- coding: utf-8 -*-
"""研究评测 runner：真的把模型跑一遍，再用 app.research_eval 机检打分。

和 scripts/eval_research.py 的区别：
- 那个只读「已有会话」的 messages；
- 这个会为每条任务开一个独立临时工作区、真实调用模型与工具（含 web_search），
  产出可复现的 runs 目录（env.json + 每题 JSON + summary）。

用法：
    python scripts/eval_research_run.py --list
    python scripts/eval_research_run.py --only http-cache --repeat 1
    python scripts/eval_research_run.py --repeat 3 --out eval/runs/research

注意：会真实调用模型并消耗 token；网页检索需要先在设置里配好搜索源。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import agent, bench, llm, research_eval, search as search_mod  # noqa: E402
from app.config import get_api_conf, load_config  # noqa: E402

SYSTEM = (
    "你是研究 / 复刻助手。必须遵守：\n"
    "1) 先检索再下结论；web_search 至少 {need} 次，关键事实用 fetch_url 核实；\n"
    "2) 复刻类任务先把初稿写到 solution.md，再继续检索校正；\n"
    "3) 最终回复用 [n] 标注来源，并在末尾按 [n] URL 列出；\n"
    "4) 不编造来源；引用编号只能使用检索结果里出现过的 [n]，不要自造编号；\n"
    "5) 检索到足够信息就立刻作答，不要为了凑次数反复搜索；\n"
    "6) 最多 {turns} 轮，最后一轮必须直接给出最终回复，不要再调工具。\n"
)


def _do_web_search(queries, counter, fetch_pages=1):
    """执行 web_search。agent.run_tool 不认这个工具（它由对话主循环处理），
    这里复用 app.search，返回和线上一致的「编号 + URL」参考格式。"""
    qs = []
    for q in queries or []:
        q = str(q or "").strip()
        if q and q not in qs:
            qs.append(q)
    if not qs:
        return "【web_search】缺少 queries：请给 1–3 个关键词式检索式。"
    try:
        provider, url, key, count = search_mod.get_search_conf()
    except Exception as e:                   # noqa: BLE001
        return "【web_search】联网搜索不可用：%s" % e
    if not key and provider != "deepseek":
        return "【web_search】联网搜索不可用：没有配置搜索密钥。"
    try:
        hits = search_mod.search_multi(qs, provider, url, key, count)
    except Exception as e:                   # noqa: BLE001
        return "【web_search】检索失败：%s" % e
    if not hits:
        return "【web_search】没有搜到相关结果；请如实说明没查到，不要凭记忆编造。"
    try:
        if fetch_pages > 0:
            search_mod.fetch_pages(hits, limit=min(3, int(fetch_pages)),
                                   query=" ".join(qs))
    except Exception:                        # noqa: BLE001  抓正文失败就退回摘要
        pass
    items = []
    for it in hits:
        counter["n"] += 1
        items.append(dict(it, n=counter["n"]))
    return "【web_search】\n" + search_mod.build_search_context(qs[0], items)


def _ids(raw):
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _run_one(task, model="", max_turns=6, timeout=240, keep=False, workdir=None,
             fetch_pages=1):
    """跑一条研究任务，返回 {task, messages, ...}；messages 供 research_eval 机检。"""
    tid = str(task.get("id") or "")
    cfg = dict(load_config() or {})
    api_key, api_url, api_model = get_api_conf("chat")
    model = str(model or api_model or "")
    started = time.time()
    tmp = workdir or tempfile.mkdtemp(prefix="gm_research_%s_" % tid)
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    turns_limit = max(1, int(max_turns))
    messages = [
        {"role": "system", "content": SYSTEM.format(
            need=max(1, int(task.get("must_search") or 1)), turns=turns_limit)},
        {"role": "user", "content":
            "工作目录：%s\n\n任务：\n%s" % (tmp, str(task.get("prompt") or ""))},
    ]
    tools_log, turns, final, status, error = [], 0, "", "max_turns", ""
    source_counter = {"n": 0}
    if not api_key:
        return {"task": tid, "status": "no_api_key", "ok": False,
                "messages": messages, "turns": 0, "tools": [],
                "usage": usage_total, "ms": 0, "workdir": ""}
    deadline = started + max(30, int(timeout))
    try:
        schemas = bench._schemas(())
        caps = agent.limits(cfg)
        while turns < turns_limit:
            left = deadline - time.time()
            if left <= 5:
                status = "timeout"
                break
            rr = llm.stream_chat(
                api_key, api_url, model, messages, tools=schemas,
                temperature=bench.BENCH_TEMPERATURE,
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
            entry = {"role": "assistant", "content": visible or "（发起工具调用）",
                     "tools": []}
            results = []
            finished = False
            for call in calls[:bench.MAX_CALLS_PER_ROUND]:
                name = str((call or {}).get("tool") or "").strip()
                cargs = (call or {}).get("args")
                cargs = cargs if isinstance(cargs, dict) else {}
                if name == "finish":
                    # finish 由对话主循环处理，agent.run_tool 不认它；
                    # 这里把它当「本轮结束」，summary 作为最终正文
                    final = str(cargs.get("summary") or "") or visible
                    status = "finished"
                    finished = True
                    break
                t0 = time.time()
                try:
                    if name == "web_search":
                        t_text, is_write = _do_web_search(
                            cargs.get("queries") or cargs.get("query"),
                            source_counter, fetch_pages), False
                    else:
                        t_text, _undo, is_write = agent.run_tool(
                            tmp, name, cargs, caps=caps)
                except Exception as e:       # noqa: BLE001  单个工具失败不终止任务
                    t_text, is_write = "❌ 工具执行异常：%s" % e, False
                t_text = str(t_text)
                entry["tools"].append({"tool": name, "args": cargs,
                                       "result": t_text[:bench.TOOL_RESULT_CLIP]})
                tools_log.append({
                    "tool": name, "args": cargs, "write": bool(is_write),
                    "ok": not t_text.lstrip().startswith("❌"),
                    "ms": int((time.time() - t0) * 1000),
                    "result": t_text[:500]})
                results.append("【%s】\n%s" % (name, t_text[:bench.TOOL_RESULT_CLIP]))
            messages.append(entry)
            turns += 1
            if finished:
                break
            messages.append({"role": "user", "content":
                             "工具执行结果如下，请据此继续：\n\n" + "\n\n".join(results)})
    except Exception as e:                   # noqa: BLE001  网络 / 解析异常都记为失败
        status, error = "error", str(e)[:500]
    result = research_eval.evaluate(task, messages)
    out = {
        "task": tid,
        "kind": task.get("kind"),
        "status": status,
        "ok": bool((result.get("score") or {}).get("pass")),
        "score": result.get("score") or {},
        "metrics": result.get("metrics") or {},
        "turns": turns,
        "tool_calls": len(tools_log),
        "usage": usage_total,
        "ms": int((time.time() - started) * 1000),
        "final": final[-4000:],
        "tools": tools_log,
        "model": model,
        "temperature": bench.BENCH_TEMPERATURE,
        "workdir": tmp if keep else "",
    }
    if error:
        out["error"] = error
    if not keep and not workdir:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _summary(results, meta):
    runnable = [r for r in results if r.get("status") != "no_api_key"]
    passed = [r for r in runnable if r.get("ok")]
    by_task = {}
    for r in runnable:
        g = by_task.setdefault(str(r.get("task")), {"attempts": 0, "passed": 0,
                                                    "coverage": 0.0, "search": 0})
        g["attempts"] += 1
        g["passed"] += 1 if r.get("ok") else 0
        g["coverage"] += float((r.get("metrics") or {}).get("coverage") or 0)
        g["search"] += int((r.get("metrics") or {}).get("search_calls") or 0)
    for g in by_task.values():
        n = max(1, g["attempts"])
        g["pass_rate"] = round(g["passed"] / n, 4)
        g["coverage"] = round(g["coverage"] / n, 3)
        g["search_avg"] = round(g["search"] / n, 1)
    return {
        "meta": meta,
        "runnable": len(runnable),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(runnable), 4) if runnable else 0.0,
        "tokens": sum(int((r.get("usage") or {}).get("total_tokens") or 0)
                      for r in runnable),
        "by_task": by_task,
        "tasks": [{
            "task": r.get("task"), "status": r.get("status"), "ok": bool(r.get("ok")),
            "coverage": (r.get("metrics") or {}).get("coverage"),
            "search_calls": (r.get("metrics") or {}).get("search_calls"),
            "citations": (r.get("metrics") or {}).get("citations"),
            "bad_citations": len((r.get("metrics") or {}).get("bad_citations") or []),
            "turns": r.get("turns"), "tokens": (r.get("usage") or {}).get("total_tokens"),
        } for r in results],
    }


def _summary_md(summary):
    meta = summary.get("meta") or {}
    lines = ["# 研究评测 %s" % meta.get("run_id", ""), "",
             "- 模型：%s" % meta.get("model", ""),
             "- 温度：%s · 重复：%s 次" % (bench.BENCH_TEMPERATURE, meta.get("repeat", 1)),
             "- 通过：%d / %d（%s）" % (summary.get("passed", 0),
                                       summary.get("runnable", 0),
                                       summary.get("pass_rate")),
             "- token：%d" % summary.get("tokens", 0), "",
             "| 任务 | 通过 | 次数 | 通过率 | 平均检索 | 平均覆盖 |",
             "|---|---|---|---|---|---|"]
    for key in sorted(summary.get("by_task") or {}):
        g = summary["by_task"][key]
        lines.append("| %s | %d | %d | %s | %s | %s |" % (
            key, g["passed"], g["attempts"], g["pass_rate"],
            g["search_avg"], g["coverage"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="研究评测 runner（会调用模型）")
    ap.add_argument("--list", action="store_true", help="列出任务")
    ap.add_argument("--only", default="", help="任务 id，逗号分隔")
    ap.add_argument("--model", default="", help="模型名（默认用聊天配置）")
    ap.add_argument("--repeat", type=int, default=1, help="每条任务重复次数")
    ap.add_argument("--max-turns", type=int, default=6, help="单题最大轮次")
    ap.add_argument("--timeout", type=int, default=240, help="单题最长秒数")
    ap.add_argument("--fetch-pages", type=int, default=1,
                    help="每次检索抓几篇正文（0 = 只用摘要，最快）")
    ap.add_argument("--keep", action="store_true", help="保留临时工作区")
    ap.add_argument("--out", default=os.path.join("data", "bench", "research"),
                    help="结果根目录")
    args = ap.parse_args(argv)

    data = research_eval.load_tasks()
    errs = research_eval.validate(data)
    if errs:
        print("\n".join(errs))
        return 1
    tasks = data.get("tasks") or []
    if args.list:
        for t in tasks:
            print("%-16s %-10s must_search=%d  %s" % (
                t.get("id"), t.get("kind"), int(t.get("must_search") or 0),
                str(t.get("prompt") or "")[:50]))
        return 0
    only = _ids(args.only)
    if only:
        tasks = [t for t in tasks if str(t.get("id")) in only]
    if not tasks:
        print("没有匹配的任务")
        return 1

    results = []
    repeat = max(1, int(args.repeat or 1))
    for task in tasks:
        for i in range(repeat):
            print("→ %-16s (%d/%d)" % (task.get("id"), i + 1, repeat))
            r = _run_one(task, model=args.model, max_turns=args.max_turns,
                         timeout=args.timeout, keep=args.keep,
                         fetch_pages=args.fetch_pages)
            print("   %s · %s · 检索 %s · 覆盖 %s · 引用 %s"
                  % ("✓" if r.get("ok") else "✗", r.get("status"),
                     (r.get("metrics") or {}).get("search_calls"),
                     (r.get("metrics") or {}).get("coverage"),
                     (r.get("metrics") or {}).get("citations")))
            results.append(r)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(args.out, run_id)
    os.makedirs(out, exist_ok=True)
    counts = {}
    for r in results:
        tid = str(r.get("task") or "task")
        counts[tid] = counts.get(tid, 0) + 1
        n = counts[tid]
        name = tid + (".json" if n == 1 else ".%d.json" % n)
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    meta = {"run_id": run_id, "model": args.model or "", "repeat": repeat}
    if not meta["model"]:
        meta["model"] = next((r.get("model") for r in results if r.get("model")), "")
    with open(os.path.join(out, "env.json"), "w", encoding="utf-8") as f:
        json.dump(bench.env_info(meta), f, ensure_ascii=False, indent=2)
    summary = _summary(results, meta)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(_summary_md(summary))
    print("\n已保存：%s" % out)
    print("通过 %d / %d（%s）" % (summary["passed"], summary["runnable"],
                                 summary["pass_rate"]))
    return 0 if summary["passed"] == summary["runnable"] else 1


if __name__ == "__main__":
    sys.exit(main())
