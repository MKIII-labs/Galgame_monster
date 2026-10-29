# -*- coding: utf-8 -*-
"""场景测评：前测 / 后测。

只回答一个问题：练过之后，在「没见过的题」上正确率有没有变化。
因此：
- 测评题与训练题分开（unit.test = pre/post），不进训练序列，也不进方法卡；
- 前测与后测同考点、不同表述；
- 报告只给正确率与单位时间正确率，不给「提分 X%」这种因果结论。
"""
import threading
import time
import uuid

from app.dojo import packs, primitives

SESSION_TTL_SEC = 2 * 3600
MAX_SESSIONS = 200
PHASES = ("pre", "post")
PHASE_NAMES = {"pre": "前测", "post": "后测"}

_SESSIONS = {}
_LOCK = threading.RLock()


class AssessError(Exception):
    pass


def _gc_locked(now):
    dead = [k for k, v in _SESSIONS.items() if now - v["ts"] > SESSION_TTL_SEC]
    for k in dead:
        _SESSIONS.pop(k, None)
    if len(_SESSIONS) >= MAX_SESSIONS:
        for sid, _s in sorted(_SESSIONS.items(), key=lambda kv: kv[1]["ts"])[:len(_SESSIONS) - MAX_SESSIONS + 1]:
            _SESSIONS.pop(sid, None)


def _units_for(pack, phase):
    ids = ((pack["manifest"].get("assessment") or {}).get(phase) or [])
    by_id = {u["id"]: u for u in pack["units"]}
    units = [by_id[str(i)] for i in ids if str(i) in by_id]
    if not units:
        raise AssessError("这个场景还没有%s题" % PHASE_NAMES[phase])
    return units


def start(pack_id, phase):
    phase = str(phase or "")
    if phase not in PHASES:
        raise AssessError("phase 只能是 pre 或 post")
    try:
        pack = packs.get_pack(pack_id)
    except packs.PackError as e:
        raise AssessError(str(e)) from None
    units = _units_for(pack, phase)
    sid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        _SESSIONS[sid] = {
            "pack_id": pack["manifest"]["id"],
            "phase": phase,
            "units": units,
            "answered": {},
            "ts": now,
        }
    return {
        "session_id": sid,
        "pack_id": pack["manifest"]["id"],
        "pack_name": pack["manifest"].get("name") or pack["manifest"]["id"],
        "phase": phase,
        "total": len(units),
        "units": [packs.public_unit(u) for u in units],
    }


def _get(sid):
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        s = _SESSIONS.get(str(sid or ""))
    if not s:
        raise AssessError("测评会话已过期，请重新开始")
    return s


def answer(session_id, unit_id, value, elapsed_ms=0):
    s = _get(session_id)
    unit = next((u for u in s["units"] if u.get("id") == unit_id), None)
    if not unit:
        raise AssessError("这道题不属于本次测评")
    with _LOCK:
        if unit_id in s["answered"]:
            raise AssessError("这道题已经提交过了")
        s["answered"][unit_id] = {"pending": True}
    try:
        result = primitives.judge(unit, value)
    except primitives.JudgeError as e:
        with _LOCK:
            s["answered"].pop(unit_id, None)
        raise AssessError(str(e)) from None
    if not result["scored"]:
        with _LOCK:
            s["answered"].pop(unit_id, None)
        raise AssessError("测评题必须是 L1/L2，不能是只诊断的开放题")
    try:
        elapsed = max(0, min(3600 * 1000, int(elapsed_ms or 0)))
    except (TypeError, ValueError):
        elapsed = 0
    ability = unit.get("ability")
    with _LOCK:
        s["answered"][unit_id] = {
            "correct": bool(result["correct"]),
            "score": result["score"],
            "ability": ability,
            "elapsed_ms": elapsed,
        }
    return {
        "unit_id": unit_id,
        "correct": bool(result["correct"]),
        "score": result["score"],
        "detail": result["detail"],
        "ability": ability,
        "ability_name": packs.ABILITY_NAMES.get(ability, ability),
        "elapsed_ms": elapsed,
        "expert": {"why": unit["expert"].get("why") or "",
                   "signals": unit["expert"].get("signals") or []},
    }


def finish(session_id):
    s = _get(session_id)
    if len(s["answered"]) < len(s["units"]):
        raise AssessError("还有题没答完")
    total = len(s["units"])
    correct = 0
    elapsed = 0
    by_ability = {}
    details = []
    for unit in s["units"]:
        r = s["answered"].get(unit["id"]) or {}
        ok = bool(r.get("correct"))
        correct += 1 if ok else 0
        elapsed += int(r.get("elapsed_ms") or 0)
        ab = unit.get("ability") or "framing"
        row = by_ability.setdefault(ab, {"ability": ab,
                                         "ability_name": packs.ABILITY_NAMES.get(ab, ab),
                                         "correct": 0, "total": 0})
        row["total"] += 1
        if ok:
            row["correct"] += 1
        details.append({"unit_id": unit["id"], "correct": ok,
                        "ability": ab, "elapsed_ms": int(r.get("elapsed_ms") or 0)})
    minutes = max(elapsed / 60000.0, 1 / 60.0)
    return {
        "pack_id": s["pack_id"],
        "phase": s["phase"],
        "total": total,
        "correct": correct,
        "rate": round(correct / total, 3),
        "elapsed_ms": elapsed,
        "per_minute": round(correct / minutes, 2),
        "by_ability": [by_ability[k] for k in sorted(by_ability)],
        "details": details,
    }


def reset_store():
    with _LOCK:
        _SESSIONS.clear()
