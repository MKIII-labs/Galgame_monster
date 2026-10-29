# -*- coding: utf-8 -*-
"""完整建模案例：一个案例 = 固定顺序的一组判断步骤。

与训练单元的区别：
- 单元是「一题一判」；案例是「一条链路」，必须按顺序走完，最后给断点报告；
- 判分完全复用 primitives，不引入 LLM 评分，保证同一答案每次结果一致；
- 案例只覆盖「判断链 + 关键数值」，不生成论文，也不声称对齐官方评分标准。

目录约定：
    resources/dojo/cases/<case_id>/case.json
    data/dojo/cases/<case_id>/case.json      （用户自建，整目录 gitignore）
"""
import json
import os
import threading
import time
import uuid

from app.dojo import packs, primitives
from app.store import DATA_DIR, RESOURCES_DIR

CASE_DIRS = (
    os.path.join(RESOURCES_DIR, "dojo", "cases"),
    os.path.join(DATA_DIR, "dojo", "cases"),
)
SESSION_TTL_SEC = 2 * 3600
MAX_SESSIONS = 200
MIN_STEPS = 4
MIN_PRIMITIVES = 3
MIN_ABILITIES = 4
BREAK_SCORE = 0.6

_CACHE = {"sig": None, "cases": []}
_SESSIONS = {}
_LOCK = threading.RLock()


class CaseError(Exception):
    pass


def _signature():
    sig = []
    for root in CASE_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "case.json")
            if os.path.isfile(path):
                try:
                    sig.append((path, os.path.getmtime(path)))
                except OSError:
                    pass
    return tuple(sig)


def _source_doc(source):
    bits = [source.get("contest"), source.get("year"), source.get("problem"), source.get("topic")]
    return " ".join(str(b) for b in bits if b)


def _load_one(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            case = json.load(f)
    except Exception as e:
        raise CaseError("case.json 读取失败：%s" % e) from None
    if not isinstance(case, dict):
        raise CaseError("case.json 不是对象")
    for field in ("id", "title", "brief", "source", "steps"):
        if not case.get(field):
            raise CaseError("缺少 %s" % field)
    source = case.get("source") or {}
    if not isinstance(source, dict) or not _source_doc(source):
        raise CaseError("source 需要 contest / year / problem / topic")
    steps = case.get("steps")
    if not isinstance(steps, list) or len(steps) < MIN_STEPS:
        raise CaseError("steps 至少 %d 步" % MIN_STEPS)

    doc = _source_doc(source)
    errs = []
    seen = set()
    for step in steps:
        if not isinstance(step, dict):
            errs.append("steps 里有非对象")
            continue
        sid = str(step.get("id") or "")
        if not sid:
            errs.append("步骤缺少 id")
            continue
        if sid in seen:
            errs.append("步骤 id 重复：%s" % sid)
        seen.add(sid)
        if step.get("judge") == "L3":
            errs.append("%s 是 L3（只诊断）步骤；案例报告只用 L1/L2 计分" % sid)
        # 复用训练单元的校验：卡片的答案、陷阱、方法卡规则与单题训练完全一致
        step_errs, _warns = packs._check_unit(
            dict(step, source_ref={"doc": doc}), "official")
        errs.extend(step_errs)

    primitives_used = {s.get("primitive") for s in steps if isinstance(s, dict)}
    abilities = {s.get("ability") for s in steps if isinstance(s, dict)}
    if len(primitives_used) < MIN_PRIMITIVES:
        errs.append("至少要用 %d 种原语（当前 %d 种）" % (MIN_PRIMITIVES, len(primitives_used)))
    if len(abilities) < MIN_ABILITIES:
        errs.append("至少覆盖 %d 个能力维度（当前 %d 个）" % (MIN_ABILITIES, len(abilities)))
    if errs:
        raise CaseError("；".join(errs))

    case = dict(case)
    case["grade"] = case.get("grade") or "official"
    case["minutes"] = int(case.get("minutes") or sum(int(s.get("minutes") or 0) for s in steps))
    case["_dir"] = os.path.dirname(path)
    return case


def load_cases():
    """扫描并缓存所有案例；case.json 改动会按 mtime 自动重载。"""
    sig = _signature()
    if _CACHE["sig"] == sig:
        return _CACHE["cases"]
    loaded, seen = [], set()
    for root in CASE_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "case.json")
            if not os.path.isfile(path):
                continue
            try:
                case = _load_one(path)
            except CaseError as e:
                print("[道场] 跳过案例 %s：%s" % (name, e))
                continue
            if case["id"] in seen:
                print("[道场] 跳过重名案例 %s" % case["id"])
                continue
            seen.add(case["id"])
            loaded.append(case)
    _CACHE["sig"] = sig
    _CACHE["cases"] = loaded
    return loaded


def reset_cache():
    _CACHE["sig"] = None
    _CACHE["cases"] = []


def get_case(case_id):
    for case in load_cases():
        if case["id"] == case_id:
            return case
    raise CaseError("没有这个案例：%s" % case_id)


def public_case(case):
    """案例目录：只给步骤的能力与时长，不给题面与答案。"""
    return {
        "id": case["id"],
        "title": case.get("title") or case["id"],
        "subtitle": case.get("subtitle") or "",
        "source": case.get("source") or {},
        "brief": case.get("brief") or "",
        "minutes": case.get("minutes"),
        "grade": case.get("grade") or "official",
        "total": len(case["steps"]),
        "steps": [
            {"id": s["id"], "ability": s.get("ability"),
             "ability_name": packs.ABILITY_NAMES.get(s.get("ability"), s.get("ability")),
             "minutes": s.get("minutes")}
            for s in case["steps"]
        ],
    }


def list_cases():
    return [public_case(c) for c in load_cases()]


def _gc_locked(now):
    dead = [sid for sid, s in _SESSIONS.items() if now - s["ts"] > SESSION_TTL_SEC]
    for sid in dead:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) >= MAX_SESSIONS:
        for sid, _s in sorted(_SESSIONS.items(), key=lambda kv: kv[1]["ts"])[
                :len(_SESSIONS) - MAX_SESSIONS + 1]:
            _SESSIONS.pop(sid, None)


def start(case_id):
    try:
        case = get_case(case_id)
    except CaseError as e:
        raise CaseError(str(e)) from None
    sid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        _SESSIONS[sid] = {
            "case": case,
            "index": 0,
            "answered": {},
            "ts": now,
        }
    return {
        "session_id": sid,
        "case": public_case(case),
        "step": packs.public_unit(case["steps"][0]),
        "index": 0,
        "total": len(case["steps"]),
    }


def _get(sid):
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        s = _SESSIONS.get(str(sid or ""))
    if not s:
        raise CaseError("这个案例已经过期，重新开始一次")
    return s


def answer(session_id, step_id, value, elapsed_ms=0):
    s = _get(session_id)
    steps = s["case"]["steps"]
    index = s["index"]
    if index >= len(steps):
        raise CaseError("这个案例已经答完了")
    step = steps[index]
    if str(step_id or "") != step["id"]:
        raise CaseError("案例要按顺序作答，当前是第 %d 步" % (index + 1))
    with _LOCK:
        if step["id"] in s["answered"]:
            raise CaseError("这一步已经提交过了")
        s["answered"][step["id"]] = {"pending": True}

    try:
        result = primitives.judge(step, value)
    except primitives.JudgeError as e:
        with _LOCK:
            s["answered"].pop(step["id"], None)
        raise CaseError(str(e)) from None

    try:
        elapsed = max(0, min(3600 * 1000, int(elapsed_ms or 0)))
    except (TypeError, ValueError):
        elapsed = 0

    ability = step.get("ability")
    with _LOCK:
        s["answered"][step["id"]] = {
            "correct": bool(result["correct"]),
            "score": result["score"],
            "ability": ability,
            "elapsed_ms": elapsed,
        }
        s["index"] = index + 1
        s["ts"] = time.time()
        next_step = steps[index + 1] if index + 1 < len(steps) else None

    return {
        "case_id": s["case"]["id"],
        "step_id": step["id"],
        "correct": bool(result["correct"]),
        "scored": result["scored"],
        "score": result["score"],
        "judge": result["judge"],
        "detail": result["detail"],
        "ability": ability,
        "ability_name": packs.ABILITY_NAMES.get(ability, ability),
        "elapsed_ms": elapsed,
        "expert": step.get("expert") or {},
        "card": step.get("card") or {},
        "step_index": index,
        "total": len(steps),
        "next_step": packs.public_unit(next_step) if next_step else None,
        "finished": next_step is None,
    }


def finish(session_id):
    s = _get(session_id)
    steps = s["case"]["steps"]
    if len(s["answered"]) < len(steps):
        raise CaseError("还有 %d 步没答" % (len(steps) - len(s["answered"])))

    rows, by_ability = [], {}
    total_score = 0.0
    first_break = None
    for i, step in enumerate(steps):
        r = s["answered"].get(step["id"]) or {}
        score = float(r.get("score") or 0)
        total_score += score
        ability = step.get("ability") or "framing"
        row = by_ability.setdefault(ability, {
            "ability": ability,
            "ability_name": packs.ABILITY_NAMES.get(ability, ability),
            "score": 0.0, "total": 0,
        })
        row["score"] += score
        row["total"] += 1
        if score < BREAK_SCORE and first_break is None:
            first_break = {
                "index": i,
                "step_id": step["id"],
                "ability": ability,
                "ability_name": packs.ABILITY_NAMES.get(ability, ability),
                "score": round(score, 3),
            }
        rows.append({
            "index": i,
            "step_id": step["id"],
            "ability": ability,
            "ability_name": packs.ABILITY_NAMES.get(ability, ability),
            "correct": bool(r.get("correct")),
            "score": round(score, 3),
            "elapsed_ms": int(r.get("elapsed_ms") or 0),
        })

    total = len(steps)
    avg = total_score / total if total else 0.0
    abilities = []
    for key in sorted(by_ability):
        item = by_ability[key]
        abilities.append({
            "ability": key,
            "ability_name": item["ability_name"],
            "score": round(item["score"] / max(1, item["total"]), 3),
            "total": item["total"],
        })

    # 只说「这条链路哪里断了」，不说「你的建模能力是多少分」
    if first_break:
        verdict = "链路断在第 %d 步（%s）" % (first_break["index"] + 1, first_break["ability_name"])
        hint = "先复习这一步的方法卡，再重跑整个案例；不要只重做这一步。"
    else:
        verdict = "整条链路成立"
        hint = "可以换一个案例，或回到单题训练补你没覆盖到的能力维度。"

    return {
        "case_id": s["case"]["id"],
        "title": s["case"].get("title") or s["case"]["id"],
        "total": total,
        "avg_score": round(avg, 3),
        "steps": rows,
        "by_ability": abilities,
        "first_break": first_break,
        "verdict": verdict,
        "hint": hint,
        "elapsed_ms": sum(int((s["answered"].get(x["id"]) or {}).get("elapsed_ms") or 0)
                          for x in steps),
    }


def reset_store():
    with _LOCK:
        _SESSIONS.clear()
