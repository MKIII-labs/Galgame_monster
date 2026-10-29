# -*- coding: utf-8 -*-
"""会话级任务状态（Task Ledger）。

压缩摘要会丢掉细节；但「为什么做、做到什么算完成、哪些文件刚改过、
哪些错误还没解决」不能丢。这里把这些字段单独存进会话，压缩时原样 pin，
每轮再以一段紧凑文本注入系统提示。

只存摘要，不存全文：素材清单/视觉基线存路径与关键指标，避免把会话撑大。
"""
import time

_MAX_FILES = 12
_MAX_ERRORS = 6
_MAX_DECISIONS = 8


def get(s):
    if not isinstance(s, dict):
        return {}
    led = s.get("task_ledger")
    if not isinstance(led, dict):
        led = {}
        s["task_ledger"] = led
    return led


def _trim(lst, n):
    if isinstance(lst, list) and len(lst) > n:
        del lst[:-n]


def set_goal(s, text):
    led = get(s)
    txt = str(text or "").strip()
    if txt:
        led["goal"] = txt[:2000]
        led["goal_at"] = time.time()


def set_acceptance(s, criteria):
    led = get(s)
    items = []
    for c in criteria or []:
        if isinstance(c, dict):
            txt = str(c.get("text") or "").strip()
            if txt:
                items.append(txt[:200])
        elif str(c or "").strip():
            items.append(str(c).strip()[:200])
    led["acceptance"] = items[:20]


def set_plan(s, plan):
    led = get(s)
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        steps = []
        for it in plan["steps"][:30]:
            if isinstance(it, dict):
                steps.append({
                    "text": str(it.get("text") or "")[:200],
                    "status": str(it.get("status") or ""),
                })
        led["plan"] = steps


def note_write(s, path):
    led = get(s)
    p = str(path or "").strip()
    if not p:
        return
    files = led.get("recent_files")
    if not isinstance(files, list):
        files = []
    files = [x for x in files if x != p]
    files.append(p)
    led["recent_files"] = files[-_MAX_FILES:]


def note_error(s, text):
    led = get(s)
    t = str(text or "").strip()
    if not t:
        return
    errs = led.get("open_errors")
    if not isinstance(errs, list):
        errs = []
    errs = [x for x in errs if x != t]
    errs.append(t[:300])
    led["open_errors"] = errs[-_MAX_ERRORS:]


def clear_error(s, text=""):
    led = get(s)
    if not text:
        led["open_errors"] = []
        return
    t = str(text).strip()
    led["open_errors"] = [x for x in (led.get("open_errors") or []) if x != t]


def note_decision(s, text):
    led = get(s)
    t = str(text or "").strip()
    if not t:
        return
    ds = led.get("decisions")
    if not isinstance(ds, list):
        ds = []
    ds = [x for x in ds if x != t]
    ds.append(t[:300])
    led["decisions"] = ds[-_MAX_DECISIONS:]


def note_asset_manifest(s, summary):
    led = get(s)
    t = str(summary or "").strip()
    if t:
        led["asset_manifest"] = t[:2000]
        led["asset_at"] = time.time()


def note_visual_baseline(s, path, metrics=None):
    led = get(s)
    p = str(path or "").strip()
    if not p:
        return
    items = led.get("visual_baselines")
    if not isinstance(items, list):
        items = []
    items = [x for x in items if not (isinstance(x, dict) and x.get("path") == p)]
    items.append({"path": p, "metrics": dict(metrics or {}), "at": time.time()})
    led["visual_baselines"] = items[-8:]


def text(s, max_chars=2000):
    """给模型看的紧凑任务状态；没有内容时返回空串。"""
    led = get(s)
    lines = []
    if led.get("goal"):
        lines.append("目标：" + str(led["goal"])[:600])
    acc = led.get("acceptance") or []
    if acc:
        lines.append("验收：" + "；".join(str(x) for x in acc[:8]))
    plan = led.get("plan") or []
    if plan:
        items = []
        for it in plan[:10]:
            if isinstance(it, dict):
                mark = {"done": "✓", "doing": "…", "pending": "○"}.get(
                    str(it.get("status") or ""), "○")
                items.append("%s %s" % (mark, str(it.get("text") or "")))
        if items:
            lines.append("计划：" + "；".join(items))
    files = led.get("recent_files") or []
    if files:
        lines.append("最近改动：" + "、".join(str(x) for x in files[-6:]))
    errs = led.get("open_errors") or []
    if errs:
        lines.append("未解决：" + "；".join(str(x) for x in errs[-4:]))
    if led.get("asset_manifest"):
        lines.append("素材：" + str(led["asset_manifest"])[:500])
    vb = led.get("visual_baselines") or []
    if vb:
        names = []
        for it in vb[-3:]:
            if isinstance(it, dict):
                names.append(str(it.get("path") or ""))
        if names:
            lines.append("视觉基线：" + "、".join(n for n in names if n))
    ds = led.get("decisions") or []
    if ds:
        lines.append("关键决策：" + "；".join(str(x) for x in ds[-4:]))
    if not lines:
        return ""
    out = "【任务状态】\n" + "\n".join("- " + ln for ln in lines)
    return out[:max_chars]
