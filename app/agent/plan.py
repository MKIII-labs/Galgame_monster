# -*- coding: utf-8 -*-

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from app import winproc

from .base import (_PICK_LOCK, _PLAN_REVIEW_ACTIONS, _normalize_options)
from .prompt import (_PICKER_CODE, _PICK_MARK)
from .state import (_TURNS, _turns_lock)
from .undo import (_UNDO, _persist_undo, _undo_lock)


ASK_TIMEOUT = 600           # 等待用户回答提问的超时（秒）


MAX_ASKS = 6                # 一次提问最多包含几个问题


PLAN_MAX_STEPS = 24         # 执行计划最多几步（v2：可拆成多个里程碑）


PLAN_MAX_MILESTONES = 6     # 里程碑上限（每个 2–6 步）


PLAN_MAX_TEXT = 120         # 单步描述最长字符


PLAN_MAX_VERIFY = 200       # 验收方式最长字符


PLAN_MAX_RETRO = 600        # 收尾复盘最长字符（结果 / 偏差 / 经验）


PLAN_MAX_ACCEPT = 6         # 单步最多挂几个验收项 id


PLAN_LOG_MAX = 20           # 决策 / 发现日志各保留多少条


PLAN_STATUSES = ("pending", "doing", "done", "blocked")
# 规划模式（/plan）只读：连写文件、跑命令、Git 提交都不给。


_ASKS = {}


_asks_lock = threading.Lock()

# 写前读状态：dsh 的 fs-policy 要求「覆盖已有文件前先读过」；
# 这里额外记版本戳（mtime_ns + size），读后文件被外部改动就要求重读，
# 避免模型拿过期内容整体覆盖。


def accept(token):
    """接纳一次写操作：丢弃 undo 快照，之后不能再撤回。

    与 undo 的区别：文件保持现状，只是销毁「反悔」的凭据。
    返回 False 表示 token 不存在（已撤销 / 已接纳 / 超出保留条数）。
    """
    with _undo_lock:
        item = _UNDO.pop(token, None)
    if not item:
        return False
    sid = item.get("sid")
    if sid:
        with _turns_lock:
            turn = _TURNS.get(sid)
            if turn and token in turn["tokens"]:
                turn["tokens"].remove(token)
        _persist_undo(sid)
    return True


def accept_tokens(tokens):
    """批量接纳；返回到真正接纳的条数。"""
    return sum(1 for t in (tokens or []) if accept(t))


PLAN_REVIEW_TIMEOUT = 1800              # 等用户审批计划的最长时间（秒）


_PLAN_REVIEWS = {}


_PLAN_REVIEWS_LOCK = threading.Lock()


def new_plan_review(sid, plan):
    """登记一次「等你批准计划」，返回 rid。侧栏会显示「待批准」。"""
    rid = "pr_" + uuid.uuid4().hex[:10]
    with _PLAN_REVIEWS_LOCK:
        _PLAN_REVIEWS[rid] = {"event": threading.Event(), "sid": sid,
                              "plan": plan if isinstance(plan, dict) else {},
                              "action": "", "comment": "", "steps": None,
                              "ts": time.time()}
    from app import session_status
    session_status.mark(sid, "approval")
    return rid


def get_plan_review(rid):
    """只读地取一条待审批计划（调试 / 恢复用）。"""
    with _PLAN_REVIEWS_LOCK:
        item = _PLAN_REVIEWS.get(rid)
    if not item:
        return None
    return {"sid": item.get("sid") or "", "plan": item.get("plan") or {},
            "action": item.get("action") or ""}


def resolve_plan_review(rid, action, comment="", steps=None):
    """前端回传审批结果。同一条记录只接受第一次回答。"""
    a = str(action or "").strip().lower()
    if a not in _PLAN_REVIEW_ACTIONS:
        return False
    with _PLAN_REVIEWS_LOCK:
        item = _PLAN_REVIEWS.get(rid)
        if not item or item["event"].is_set():
            return False
        item["action"] = a
        item["comment"] = str(comment or "").strip()[:4000]
        item["steps"] = steps if isinstance(steps, list) else None
        item["event"].set()
    return True


def wait_plan_review(rid, timeout=PLAN_REVIEW_TIMEOUT, stop_event=None):
    """阻塞直到用户回答 / 超时 / 停止。返回 dict 或 None。"""
    with _PLAN_REVIEWS_LOCK:
        item = _PLAN_REVIEWS.get(rid)
    if not item:
        return None
    deadline = time.time() + max(0.05, timeout)
    while time.time() < deadline:
        if item["event"].wait(0.2):
            break
        if stop_event is not None and stop_event.is_set():
            break
    with _PLAN_REVIEWS_LOCK:
        _PLAN_REVIEWS.pop(rid, None)
    from app import session_status
    session_status.clear(item.get("sid"), "approval")
    if not item["event"].is_set() or not item.get("action"):
        return None
    return {"action": item["action"], "comment": item.get("comment") or "",
            "steps": item.get("steps")}


# ============================================================
# 3.4 重复调用守卫（doom loop）
# ============================================================


def _plan_txt(v, limit=PLAN_MAX_TEXT):
    t = str(v or "").strip()
    return (t[:limit] + "…") if len(t) > limit else t


def _plan_status(v):
    s = str(v or "pending").strip().lower()
    return s if s in PLAN_STATUSES else "pending"


def _plan_token(raw, fallback, limit=16):
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:limit]
    return s or fallback


def _plan_deps(v):
    if isinstance(v, str):
        v = [x for x in re.split(r"[,，;；\s]+", v) if x]
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:8]:
        s = re.sub(r"[^A-Za-z0-9_-]", "", str(x or ""))[:16]
        if s and s not in out:
            out.append(s)
    return out


def _plan_accept(v):
    """步骤挂的验收项 id（对应 acceptance 工具的 criteria.id）。"""
    if isinstance(v, str):
        v = [x for x in re.split(r"[,，;；\s]+", v) if x]
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:PLAN_MAX_ACCEPT]:
        s = re.sub(r"[^A-Za-z0-9_-]", "", str(x or ""))[:24]
        if s and s not in out:
            out.append(s)
    return out


def _plan_done_when(v):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:8]:
        t = _plan_txt(x, PLAN_MAX_VERIFY)
        if t:
            out.append(t)
    return out


def _plan_log(base_log, extra):
    """合并决策 / 发现日志：各保留最近 PLAN_LOG_MAX 条。"""
    out = {"decisions": [], "discoveries": []}
    if isinstance(base_log, dict):
        for k in ("decisions", "discoveries"):
            for it in (base_log.get(k) or [])[-PLAN_LOG_MAX:]:
                if isinstance(it, dict) and it.get("text"):
                    out[k].append({"at": int(it.get("at") or 0),
                                   "text": _plan_txt(it.get("text"), PLAN_MAX_VERIFY)})
    items = []
    if isinstance(extra, list):
        items = extra
    elif isinstance(extra, dict):
        # 字典形式：{"decisions": "文本", "discoveries": "文本"}——键是 kind，值是文本。
        # 不能写 dict(v, kind=k)：v 是字符串，dict("文字") 会按字符拆键值对，
        # 抛 "dictionary update sequence element #0 has length 1"（历史崩溃点）。
        items = [{"kind": k, "text": v} for k, v in extra.items() if isinstance(v, str)]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "decision").strip().lower()
        # 字典形式常用复数键（decisions / discoveries），单复数都要认，
        # 否则两桶都会被并进 decisions。
        if kind in ("decision", "decisions"):
            kind = "decision"
        elif kind in ("discovery", "discoveries"):
            kind = "discovery"
        else:
            kind = "decision"
        text = _plan_txt(it.get("text") or it.get("note"), PLAN_MAX_VERIFY)
        if text:
            out["decisions" if kind == "decision" else "discoveries"].append(
                {"at": int(time.time()), "text": text})
    for k in out:
        out[k] = out[k][-PLAN_LOG_MAX:]
    return out


def normalize_plan(args, base=None):
    """把模型给的 plan 参数收敛成计划对象（v2：里程碑 + 活文档）。

    结构：
      {goal, done_when, revision, milestones:[{id,title,done_when}],
       steps:[{id,text,status,milestone,verify,deps}], log:{decisions,discoveries}, note}

    `steps` 仍然是**扁平列表**（状态只存一份），老代码（_pending_steps /
    plan_stale / 自动续跑 / 前端旧渲染）都不用改；里程碑只是分组元数据。

    返回 (plan, "") 或 (None, 错误说明)。
    """
    args = args if isinstance(args, dict) else {}
    base = base if isinstance(base, dict) else {}
    milestones_in = args.get("milestones")
    steps_in = args.get("steps")
    if isinstance(steps_in, dict):
        steps_in = [steps_in]
    has_ms = isinstance(milestones_in, list) and bool(milestones_in)
    if not has_ms and not steps_in:
        return None, ("缺少 steps 或 milestones：简单任务给 1–8 步，"
                      "大型任务给 2–%d 个里程碑（每个 2–6 步）。" % PLAN_MAX_MILESTONES)

    milestones = []
    flat = []
    if has_ms:
        for m in milestones_in[:PLAN_MAX_MILESTONES]:
            if not isinstance(m, dict):
                continue
            title = _plan_txt(m.get("title") or m.get("text"))
            if not title:
                continue
            mid = _plan_token(m.get("id"), "M%d" % (len(milestones) + 1), 12)
            if any(x["id"] == mid for x in milestones):
                mid = "M%d" % (len(milestones) + 1)
            milestones.append({"id": mid, "title": title,
                               "done_when": _plan_txt(m.get("done_when"), PLAN_MAX_VERIFY)})
            for si, st in enumerate(m.get("steps") or [], 1):
                if isinstance(st, str):
                    st = {"text": st}
                if not isinstance(st, dict):
                    continue
                text = _plan_txt(st.get("text") or st.get("title"))
                if not text:
                    continue
                flat.append({
                    "id": _plan_token(st.get("id"), "%sS%d" % (mid, si)),
                    "text": text,
                    "status": _plan_status(st.get("status")),
                    "milestone": mid,
                    "verify": _plan_txt(st.get("verify") or st.get("check"), PLAN_MAX_VERIFY),
                    "deps": _plan_deps(st.get("deps")),
                    "ref": _plan_txt(st.get("ref"), 80),
                    "accept": _plan_accept(st.get("accept")),
                })
    for si, st in enumerate(steps_in or [], 1):
        if isinstance(st, str):
            st = {"text": st}
        if not isinstance(st, dict):
            continue
        text = _plan_txt(st.get("text") or st.get("title") or st.get("step"))
        if not text:
            continue
        mid = str(st.get("milestone") or "").strip()
        if not mid and len(milestones) == 1:
            mid = milestones[0]["id"]
        flat.append({
            "id": _plan_token(st.get("id"), "S%d" % si),
            "text": text,
            "status": _plan_status(st.get("status")),
            "milestone": mid,
            "verify": _plan_txt(st.get("verify") or st.get("check"), PLAN_MAX_VERIFY),
            "deps": _plan_deps(st.get("deps")),
            "ref": _plan_txt(st.get("ref"), 80),
            "accept": _plan_accept(st.get("accept")),
        })
    if not flat:
        return None, "steps / milestones 里没有可用的步骤文本。"
    flat = flat[:PLAN_MAX_STEPS]
    # id 去重：patch 只靠 id 定位，重复会导致改错步骤
    seen = set()
    for i, s in enumerate(flat, 1):
        if s["id"] in seen:
            s["id"] = "S%d" % i
        seen.add(s["id"])
    # 引用了不存在的里程碑 → 归到第一个（或清空）
    mids = {m["id"] for m in milestones}
    for s in flat:
        if s["milestone"] and s["milestone"] not in mids:
            s["milestone"] = (milestones[0]["id"] if len(milestones) == 1 else "")
    try:
        revision = int(base.get("revision") or 0) + 1
    except (TypeError, ValueError):
        revision = 1
    return {
        "goal": _plan_txt(args.get("goal") or base.get("goal"), 200),
        "done_when": _plan_done_when(args.get("done_when") or base.get("done_when")),
        "revision": revision,
        "milestones": milestones,
        "steps": flat,
        "log": _plan_log(base.get("log"), args.get("log")),
        "retro": _plan_txt(args.get("retro") or base.get("retro"), PLAN_MAX_RETRO),
        "note": _plan_txt(args.get("note"), PLAN_MAX_TEXT),
    }, ""


def apply_plan_patch(plan, args):
    """在不重发整份计划的前提下改计划（活文档）。

    返回 (new_plan, err, summary)。没有计划时拒绝：先 update_plan 建卡。
    """
    if not isinstance(plan, dict) or not (plan.get("steps")):
        return None, "还没有计划：先调用 update_plan 建立计划，再用 plan_patch 调整。", ""
    args = args if isinstance(args, dict) else {}
    milestones = [dict(m) for m in (plan.get("milestones") or []) if isinstance(m, dict)]
    mids = {m.get("id") for m in milestones}
    steps = [dict(s) for s in (plan.get("steps") or []) if isinstance(s, dict)]
    by_id = {s.get("id"): s for s in steps}
    changed = 0

    for m in args.get("add_milestones") or []:
        if not isinstance(m, dict):
            continue
        title = _plan_txt(m.get("title"))
        if not title:
            continue
        mid = _plan_token(m.get("id"), "M%d" % (len(milestones) + 1), 12)
        if mid in mids:
            mid = "M%d" % (len(milestones) + 1)
        milestones.append({"id": mid, "title": title,
                           "done_when": _plan_txt(m.get("done_when"), PLAN_MAX_VERIFY)})
        mids.add(mid)
        changed += 1
        for si, st in enumerate(m.get("steps") or [], 1):
            if not isinstance(st, dict):
                continue
            text = _plan_txt(st.get("text"))
            if not text:
                continue
            sid = _plan_token(st.get("id"), "%sS%d" % (mid, si))
            if sid in by_id:
                sid = "%sS%d" % (mid, si)
            item = {"id": sid, "text": text, "status": _plan_status(st.get("status")),
                    "milestone": mid,
                    "verify": _plan_txt(st.get("verify"), PLAN_MAX_VERIFY),
                    "deps": _plan_deps(st.get("deps")),
                    "ref": _plan_txt(st.get("ref"), 80),
                    "accept": _plan_accept(st.get("accept"))}
            steps.append(item)
            by_id[sid] = item
            changed += 1

    default_mid = ""
    doing = next((s for s in steps if s.get("status") == "doing"), None)
    if doing:
        default_mid = str(doing.get("milestone") or "")
    if not default_mid and milestones:
        default_mid = milestones[0]["id"]
    for st in args.get("add_steps") or []:
        if not isinstance(st, dict):
            continue
        text = _plan_txt(st.get("text"))
        if not text:
            continue
        mid = str(st.get("milestone") or default_mid or "")
        if mid and mid not in mids:
            mid = default_mid
        sid = "S%d" % (len(steps) + 1)
        while sid in by_id:
            sid += "x"
        item = {"id": sid, "text": text, "status": _plan_status(st.get("status")),
                "milestone": mid,
                "verify": _plan_txt(st.get("verify"), PLAN_MAX_VERIFY),
                "deps": _plan_deps(st.get("deps")),
                "ref": _plan_txt(st.get("ref"), 80),
                "accept": _plan_accept(st.get("accept"))}
        steps.append(item)
        by_id[sid] = item
        changed += 1

    for u in args.get("update_steps") or []:
        if not isinstance(u, dict):
            continue
        sid = str(u.get("id") or "").strip()
        item = by_id.get(sid)
        if not item:
            continue
        if "text" in u:
            item["text"] = _plan_txt(u.get("text")) or item.get("text")
        if "status" in u:
            item["status"] = _plan_status(u.get("status"))
        if "verify" in u:
            item["verify"] = _plan_txt(u.get("verify"), PLAN_MAX_VERIFY)
        if "milestone" in u:
            mid = str(u.get("milestone") or "")
            item["milestone"] = mid if (not mid or mid in mids) else item.get("milestone", "")
        if "deps" in u:
            item["deps"] = _plan_deps(u.get("deps"))
        if "ref" in u:
            item["ref"] = _plan_txt(u.get("ref"), 80)
        if "accept" in u:
            item["accept"] = _plan_accept(u.get("accept"))
        changed += 1

    drop = {str(x) for x in (args.get("remove_steps") or []) if str(x).strip()}
    if drop:
        steps = [s for s in steps if str(s.get("id")) not in drop]
        changed += len(drop)

    if not changed and not args.get("log") and not args.get("reason") \
            and not args.get("goal") and not args.get("done_when") \
            and not args.get("retro"):
        return None, "plan_patch 没有可执行的改动：给 add_steps / update_steps / remove_steps / log / retro / reason 之一。", ""

    payload = {
        "goal": args.get("goal") or plan.get("goal") or "",
        "done_when": args.get("done_when") or plan.get("done_when") or [],
        "milestones": milestones,
        "steps": steps,
        "log": args.get("log") or [],
        "retro": args.get("retro") or plan.get("retro") or "",
        "note": args.get("note") or plan.get("note") or "",
    }
    newp, err = normalize_plan(payload, base=plan)
    if err:
        return None, err, ""
    reason = _plan_txt(args.get("reason"), PLAN_MAX_VERIFY)
    if reason:
        newp["log"]["decisions"] = (newp["log"]["decisions"]
                                    + [{"at": int(time.time()), "text": reason}])[-PLAN_LOG_MAX:]
    summary = ("已更新计划 v%d：%d 个里程碑 / %d 步"
               % (newp.get("revision") or 1, len(newp.get("milestones") or []),
                  len(newp.get("steps") or [])))
    if reason:
        summary += "；变更原因：" + reason
    return newp, "", summary


def plan_brief(plan):
    """给模型看的一行计划摘要。

    贴在本轮「工具执行结果」末尾，利用近因效应：长对话里计划不会沉到中段
    被模型忽略（Lost in the Middle）。
    """
    steps = (plan or {}).get("steps") or []
    if not steps:
        return ""
    done = sum(1 for s in steps if s.get("status") == "done")
    doing = next((s.get("text") for s in steps if s.get("status") == "doing"), "")
    todo = [s.get("text") for s in steps
            if s.get("status") not in ("done", "blocked")]
    blocked = [s.get("text") for s in steps if s.get("status") == "blocked"]
    out = "【当前计划】%d/%d" % (done, len(steps))
    try:
        rev = int((plan or {}).get("revision") or 1)
    except (TypeError, ValueError):
        rev = 1
    if rev > 1:
        out += " v%d" % rev
    if done + len(blocked) >= len(steps):
        return out + (" · 已全部完成" if not blocked else " · 有 %d 步受阻" % len(blocked))
    if doing:
        out += " · 正在：" + str(doing)
    elif todo:
        out += " · 下一步：" + str(todo[0])
    if len(todo) > 1:
        out += " · 还剩 %d 步" % len(todo)
    return out


# ============================================================
# 4. 向用户提问（原地等待回答）
# ============================================================


def new_asks(sid, questions, timeout=None):
    """一次登记一组提问（可含多个问题）。返回 (qid, 规范化后的问题列表)。

    每个问题形如 {"question": str, "options": [{label, desc, recommended}], "multiple": bool}；
    options 可为空，也兼容纯字符串（会自动补成只带 label 的对象）。
    multiple 为真时题目末尾自动补「（多选）」，用户可同时选多个。
    action 为真时是「等待用户操作」：question 是标题，detail 是说明，
    items 是操作清单（可选），前端只给「我已完成 / 无法完成」两个按钮。
    最多 MAX_ASKS 个，超出的丢弃。

    timeout 是该组提问的等待上限（秒），0 = 永不超时；超时后 wait_answers 返回 None，
    调用方按「未回答」继续。
    """
    qid = "q_" + uuid.uuid4().hex[:10]
    try:
        tmo = int(timeout if timeout is not None else ASK_TIMEOUT)
    except (TypeError, ValueError):
        tmo = ASK_TIMEOUT
    tmo = max(0, min(3600, tmo))
    now = time.time()
    clean = []
    for it in (questions or []):
        if isinstance(it, str):
            it = {"question": it}
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        if not q:
            continue
        opts = _normalize_options(it.get("options"))
        multiple = (it.get("multiple") is True
                    or str(it.get("multiple")).strip().lower() in ("1", "true", "yes", "on"))
        # 多选标记直接写进题面：前端、历史回显、模型拿到的回答里都能看到，
        # 不用再各维护一份「是否多选」的展示逻辑。已经写了就不重复补。
        if multiple and "多选" not in q:
            q = q + "（多选）"
        item = {"question": q, "options": opts, "multiple": multiple}
        # action=true：这件「事」要用户在本机亲手做完（登录 / 装依赖 / 点系统弹窗），
        # 前端渲染成「等待你操作」卡，只有「我已完成 / 无法完成」两个出口。
        if it.get("action") is True or str(it.get("action")).strip().lower() in ("1", "true", "yes", "on"):
            raw = it.get("items")
            if isinstance(raw, str):
                raw = [raw]
            steps = [str(x).strip()[:200] for x in (raw if isinstance(raw, list) else [])
                     if str(x).strip()][:8]
            item.update({"action": True,
                         "detail": str(it.get("detail") or "").strip()[:400],
                         "items": steps,
                         "options": []})        # 操作卡用固定按钮，不再给选项
        clean.append(item)
        if len(clean) >= MAX_ASKS:
            break
    with _asks_lock:
        _ASKS[qid] = {"event": threading.Event(), "answers": None, "sid": sid,
                      "questions": clean, "created_at": now,
                      "timeout": tmo, "expires_at": (now + tmo) if tmo > 0 else 0}
    from app import session_status
    session_status.mark(sid, "ask")          # 侧栏会话行显示「待回答」
    return qid, clean


def ask_meta(qid):
    """提问的元信息（截止时间 / 问题列表）；不存在返回 None。"""
    with _asks_lock:
        item = _ASKS.get(str(qid or ""))
        if not item:
            return None
        return {"qid": str(qid), "sid": item.get("sid"),
                "questions": list(item.get("questions") or []),
                "created_at": item.get("created_at"),
                "timeout": item.get("timeout"),
                "expires_at": item.get("expires_at")}


def dismiss_ask(qid, note="（由 AI 自行决定）"):
    """用户点「让 AI 自己决定」：按给定说明唤醒等待线程。"""
    with _asks_lock:
        item = _ASKS.get(str(qid or ""))
        if not item:
            return False
        n = max(1, len(item.get("questions") or []))
        item["answers"] = [str(note or "（由 AI 自行决定）")] * n
        item["event"].set()
    return True


def submit_answers(qid, answers):
    """提交整组回答（与问题一一对应；未答的传空串）。也可只传一个字符串。"""
    with _asks_lock:
        item = _ASKS.get(qid)
    if not item:
        return False
    if answers is None or isinstance(answers, str):
        answers = [answers]
    if not isinstance(answers, (list, tuple)):
        answers = [answers]
    item["answers"] = ["" if a is None else str(a) for a in answers]
    item["event"].set()
    return True


def wait_answers(qid, timeout=None, stop_event=None):
    """阻塞等待用户回答整组提问；返回答案列表，超时/被停止返回 None

    timeout 为 None 时用提问登记时的有效期（new_asks(timeout=...)）；0 = 永不超时，
    只被回答 / 停止打断。
    """
    with _asks_lock:
        item = _ASKS.get(qid)
    if not item:
        return None
    if timeout is None:
        try:
            timeout = float(item.get("timeout") or 0)
        except (TypeError, ValueError):
            timeout = 0
    deadline = (time.time() + timeout) if timeout > 0 else None
    last_touch = time.time()
    from app import session_status
    while deadline is None or time.time() < deadline:
        if item["event"].wait(0.3):
            break
        if stop_event is not None and stop_event.is_set():
            break
        # 提问可能永久等待：定期续期，别让侧栏「待回答」被 TTL 清掉
        if time.time() - last_touch > 30:
            session_status.refresh(item.get("sid"), "ask")
            last_touch = time.time()
    with _asks_lock:
        _ASKS.pop(qid, None)
    session_status.clear(item.get("sid"), "ask")
    return item.get("answers")


# ============================================================
# 5. 提示词
# ============================================================


def pick_directory(initial=""):
    """弹出系统文件夹选择框。

    返回 `(path, busy)`：
      - `busy=True` 表示**已经有一个选择框开着**，本次不再弹第二个窗口（path 为空串）；
      - `busy=False` + 空 path 表示用户取消，或弹窗过程失败。

    为什么要互斥：服务端是 ThreadingHTTPServer，而前端存在两条会触发它的路径
    （手动点 📁、开启 Agent 时自动弹窗），叠加起来就会起两个 tkinter 子进程，
    屏幕上出现**多个文件夹窗口**。这里用**非阻塞**互斥挡掉第二个请求 ——
    刻意不排队，否则用户连点几次后仍会连着弹出好几个。

    另外只认带 __GM_PICK__ 标记的那一行——本机某些软件（如联想 AI Agent）会往
    Python 子进程的 stdout 里打日志，直接取整个 stdout 会把这些日志混进路径。
    """
    if not _PICK_LOCK.acquire(blocking=False):
        return "", True
    try:
        start = initial if (initial and os.path.isdir(initial)) else os.path.expanduser("~")
        try:
            r = subprocess.run([sys.executable, "-c", _PICKER_CODE, start],
                               capture_output=True, text=True, timeout=600,
                               creationflags=winproc.no_window())
        except Exception as e:
            print("[Agent] 目录选择框打开失败：%s" % e)
            return "", False
        for line in reversed((r.stdout or "").splitlines()):
            if line.startswith(_PICK_MARK):
                path = line[len(_PICK_MARK):].strip().strip('"').strip("'")
                return (path if (path and os.path.isdir(path)) else ""), False
        return "", False
    finally:
        _PICK_LOCK.release()


# ============================================================
# 7. IDE / 工作区：目录树与文件读写（沙箱内，复用同一套安全闸）
# ============================================================
