# -*- coding: utf-8 -*-
"""训练会话与提交。

会话只存在内存里，重启即失效（与数学竞技场的 round 一致）；
真正需要长期保留的是方法卡与能力画像，那部分由前端按
`gm.dojo.v1` 存 localStorage，换端口时和竞技场一样会读不到。
"""
import threading
import time
import uuid

from app.dojo import packs, primitives

SESSION_TTL_SEC = 2 * 3600
MAX_SESSIONS = 400

_SESSIONS = {}
_LOCK = threading.RLock()


class DojoError(Exception):
    pass


def _gc_locked(now):
    dead = [sid for sid, s in _SESSIONS.items() if now - s["ts"] > SESSION_TTL_SEC]
    for sid in dead:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) >= MAX_SESSIONS:
        # 超量时先丢最旧的：P0 只服务单机单用户，不需要更复杂的淘汰策略
        for sid, _s in sorted(_SESSIONS.items(), key=lambda kv: kv[1]["ts"])[:len(_SESSIONS) - MAX_SESSIONS + 1]:
            _SESSIONS.pop(sid, None)


def start(pack_id, unit_id=None):
    try:
        pack = packs.get_pack(pack_id)
    except packs.PackError as e:
        raise DojoError(str(e)) from None
    units = pack["units"]
    unit = None
    if unit_id:
        unit = next((u for u in units if u.get("id") == unit_id), None)
        if not unit:
            raise DojoError("这个场景包里没有该单元")
    else:
        # 无指定时按 manifest.entry.first_unit 起步，而不是按文件名撞第一个
        first = (pack["manifest"].get("entry") or {}).get("first_unit")
        unit = next((u for u in units if u.get("id") == first), None) or units[0]
    index = units.index(unit)
    sid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        _SESSIONS[sid] = {
            "pack_id": pack["manifest"]["id"],
            "unit": unit,
            "index": index,
            "answered": False,
            "ts": now,
        }
    return {
        "session_id": sid,
        "unit": packs.public_unit(unit),
        "pack": packs.public_manifest(pack),
        "index": index,
        "total": len(units),
    }


def answer(session_id, unit_id, value, elapsed_ms=0):
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        session = _SESSIONS.get(str(session_id or ""))
        if not session:
            raise DojoError("这一题已经过期，重新开始一题")
        if session["answered"]:
            raise DojoError("这道题已经提交过了")
        unit = session["unit"]
        if unit_id and unit_id != unit.get("id"):
            raise DojoError("会话与题目对不上")
        # 先占位，避免连点两次重复判分（判分本身可能很轻，但语义上只允许一次）
        session["answered"] = True

    try:
        result = primitives.judge(unit, value)
    except primitives.JudgeError as e:
        with _LOCK:
            session["answered"] = False          # 格式错误不算提交，允许改正后重试
        raise DojoError(str(e)) from None

    try:
        elapsed = max(0, min(24 * 3600, int(elapsed_ms or 0)))
    except (TypeError, ValueError):
        elapsed = 0
    with _LOCK:
        session["elapsed_ms"] = elapsed

    ability = unit.get("ability")
    return {
        "unit_id": unit.get("id"),
        "scored": result["scored"],
        "correct": result["correct"],
        "score": result["score"],
        "judge": result["judge"],
        "detail": result["detail"],
        "ability": ability,
        "ability_name": packs.ABILITY_NAMES.get(ability, ability),
        "elapsed_ms": elapsed,
        "expert": unit.get("expert") or {},
        "card": unit.get("card") or {},
    }


def reset_store():
    with _LOCK:
        _SESSIONS.clear()
