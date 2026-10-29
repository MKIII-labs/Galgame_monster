# -*- coding: utf-8 -*-
"""深度研究：一个研究任务 = 一组按顺序展开的「幕」+ 一份研究结论。

与单题训练 / 案例的区别：
- 单题训练是「一次判断」；案例是「固定顺序的判断链」；
- 研究任务要求用户先走完「发生 → 猜想 → 反例 → 证明骨架 → 边界 → 应用」，
  最后写一段自己的研究结论；
- 客观幕照常计分；研究结论只做 rubric 覆盖诊断，不计分、不冒充证明检查。

目录约定：
    resources/dojo/inquiries/<collection>/collection.json
    resources/dojo/inquiries/<collection>/items/<inquiry>.json
    data/dojo/inquiries/…                      （用户自建，整目录 gitignore）
"""
import copy
import json
import os
import threading
import time
import uuid

from app.dojo import packs, primitives
from app.store import DATA_DIR, RESOURCES_DIR

INQUIRY_DIRS = (
    os.path.join(RESOURCES_DIR, "dojo", "inquiries"),
    os.path.join(DATA_DIR, "dojo", "inquiries"),
)
INDEX_DIRS = (
    os.path.join(RESOURCES_DIR, "dojo", "index"),
    os.path.join(DATA_DIR, "dojo", "index"),
)
SESSION_TTL_SEC = 2 * 3600
MAX_SESSIONS = 200
MIN_ACTS = 4
MIN_PRIMITIVES = 3
MIN_ABILITIES = 4
BREAK_SCORE = 0.6
ARTIFACT_WEAK = 0.6
MAX_ARTIFACT_CHARS = 2000

_CACHE = {"sig": None, "collections": []}
_INDEX_CACHE = {"sig": None, "data": []}
_SESSIONS = {}
_LOCK = threading.RLock()


class ResearchError(Exception):
    pass


def _item_dir(collection_dir):
    return os.path.join(collection_dir, "items")


def _signature():
    sig = []
    for root in INQUIRY_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            col = os.path.join(root, name)
            if not os.path.isdir(col):
                continue
            paths = [os.path.join(col, "collection.json")]
            items_dir = _item_dir(col)
            if os.path.isdir(items_dir):
                paths.extend(
                    os.path.join(items_dir, n)
                    for n in sorted(os.listdir(items_dir))
                    if n.endswith(".json"))
            for path in paths:
                try:
                    sig.append((path, os.path.getmtime(path)))
                except OSError:
                    pass
    return tuple(sig)


def _load_collection(col_dir):
    path = os.path.join(col_dir, "collection.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            col = json.load(f)
    except Exception as e:
        raise ResearchError("collection.json 读取失败：%s" % e) from None
    if not isinstance(col, dict):
        raise ResearchError("collection.json 不是对象")
    for field in ("id", "name", "standard"):
        if not col.get(field):
            raise ResearchError("collection 缺少 %s" % field)
    col = dict(col)
    col["_dir"] = col_dir
    col["items"] = []
    items_dir = _item_dir(col_dir)
    if not os.path.isdir(items_dir):
        return col
    for name in sorted(os.listdir(items_dir)):
        if not name.endswith(".json"):
            continue
        try:
            col["items"].append(_load_inquiry(os.path.join(items_dir, name), col))
        except ResearchError as e:
            print("[研究] 跳过 %s：%s" % (name, e))
    return col


def _load_inquiry(path, col):
    try:
        with open(path, "r", encoding="utf-8") as f:
            q = json.load(f)
    except Exception as e:
        raise ResearchError("inquiry 读取失败：%s" % e) from None
    if not isinstance(q, dict):
        raise ResearchError("inquiry 不是对象")
    for field in ("id", "title", "subject", "brief", "acts", "artifact"):
        if not q.get(field):
            raise ResearchError("缺少 %s" % field)
    acts = q.get("acts")
    if not isinstance(acts, list) or len(acts) < MIN_ACTS:
        raise ResearchError("acts 至少 %d 幕" % MIN_ACTS)

    doc = "%s · %s" % (col.get("standard"), q.get("title"))
    errs, seen = [], set()
    for act in acts:
        if not isinstance(act, dict):
            errs.append("acts 里有非对象")
            continue
        aid = str(act.get("id") or "")
        if not aid:
            errs.append("一幕缺少 id")
            continue
        if aid in seen:
            errs.append("幕 id 重复：%s" % aid)
        seen.add(aid)
        if act.get("judge") == "L3":
            errs.append("%s 是 L3；研究任务的分幕只收 L1/L2" % aid)
        act_errs, _warns = packs._check_unit(
            dict(act, source_ref={"doc": doc}), "official")
        errs.extend(act_errs)

    prims = {a.get("primitive") for a in acts if isinstance(a, dict)}
    abilities = {a.get("ability") for a in acts if isinstance(a, dict)}
    if len(prims) < MIN_PRIMITIVES:
        errs.append("至少要用 %d 种原语（当前 %d 种）" % (MIN_PRIMITIVES, len(prims)))
    if len(abilities) < MIN_ABILITIES:
        errs.append("至少覆盖 %d 个能力维度（当前 %d 个）" % (MIN_ABILITIES, len(abilities)))

    artifact = q.get("artifact") or {}
    for field in ("prompt", "rubric", "expert_sample"):
        if not artifact.get(field):
            errs.append("artifact 缺少 %s" % field)
    rubric = artifact.get("rubric") or []
    if len(rubric) < 3:
        errs.append("artifact.rubric 至少 3 条（否则诊断没有意义）")
    for item in rubric:
        if not item.get("id") or not item.get("text") or not (item.get("keywords") or []):
            errs.append("artifact.rubric 每项都要有 id / text / keywords")

    if errs:
        raise ResearchError("；".join(errs))

    q = dict(q)
    q["collection"] = col["id"]
    q["collection_name"] = col.get("name") or col["id"]
    q["minutes"] = int(q.get("minutes") or sum(int(a.get("minutes") or 0) for a in acts))
    q["_path"] = path
    return q


def load_collections():
    sig = _signature()
    if _CACHE["sig"] == sig:
        return _CACHE["collections"]
    loaded, seen = [], set()
    for root in INQUIRY_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            col_dir = os.path.join(root, name)
            if not os.path.isdir(col_dir):
                continue
            try:
                col = _load_collection(col_dir)
            except ResearchError as e:
                print("[研究] 跳过 %s：%s" % (name, e))
                continue
            if col["id"] in seen:
                print("[研究] 跳过重名 collection %s" % col["id"])
                continue
            seen.add(col["id"])
            loaded.append(col)
    _CACHE["sig"] = sig
    _CACHE["collections"] = loaded
    return loaded


def reset_cache():
    _CACHE["sig"] = None
    _CACHE["collections"] = []
    _INDEX_CACHE["sig"] = None
    _INDEX_CACHE["data"] = []


def _find(inquiry_id):
    for col in load_collections():
        for q in col["items"]:
            if q["id"] == inquiry_id:
                return col, q
    raise ResearchError("没有这个研究任务：%s" % inquiry_id)


def get_inquiry(inquiry_id):
    _col, q = _find(inquiry_id)
    return q


def public_collection(col):
    return {
        "id": col["id"],
        "name": col.get("name") or col["id"],
        "standard": col.get("standard") or "",
        "note": col.get("note") or "",
        "tracks": col.get("tracks") or [],
        "count": len(col["items"]),
    }


def public_inquiry(q, with_acts=True):
    out = {
        "id": q["id"],
        "collection": q.get("collection"),
        "collection_name": q.get("collection_name"),
        "title": q.get("title"),
        "subject": q.get("subject") or "",
        "track": q.get("track") or "",
        "minutes": q.get("minutes"),
        "exam": q.get("exam") or {},
        "source": q.get("source") or {},
        "prerequisites": q.get("prerequisites") or [],
        "brief": q.get("brief") or "",
        "total": len(q["acts"]),
        "artifact": {"prompt": (q.get("artifact") or {}).get("prompt") or ""},
    }
    if with_acts:
        out["acts"] = [
            {"id": a["id"], "label": a.get("label") or "",
             "ability": a.get("ability"),
             "ability_name": packs.ABILITY_NAMES.get(a.get("ability"), a.get("ability")),
             "primitive": a.get("primitive"), "judge": a.get("judge"),
             "minutes": a.get("minutes")}
            for a in q["acts"]
        ]
    return out


def list_collections():
    return [public_collection(c) for c in load_collections() if c["items"]]


def list_inquiries(collection_id=None):
    out = []
    for col in load_collections():
        if collection_id and col["id"] != collection_id:
            continue
        out.extend(public_inquiry(q) for q in col["items"])
    # 与 CMC 初赛的分值顺序一致：数分 → 高代 → 解析几何
    order = {"数学分析": 0, "高等代数": 1, "解析几何": 2}
    out.sort(key=lambda q: (order.get(q.get("subject"), 9), q.get("title") or ""))
    return out


# ---------------- 真题索引 ----------------
# 索引只描述「哪一年、哪一卷、第几题、考什么」，不改写题面；
# 是否已改编由 inquiries / cases 里的 source 字段反查得到，避免两处手工维护。

def _index_signature():
    parts = []
    for root in INDEX_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                parts.append("%s:%s" % (path, os.path.getmtime(path)))
            except OSError:
                continue
    return "|".join(parts)


def load_indexes():
    sig = _index_signature()
    if _INDEX_CACHE["sig"] == sig:
        return _INDEX_CACHE["data"]
    loaded, seen = [], set()
    for root in INDEX_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8") as f:
                    idx = json.load(f)
            except Exception as e:
                print("[索引] 跳过 %s：%s" % (name, e))
                continue
            if not isinstance(idx, dict) or not idx.get("contest"):
                continue
            if idx["contest"] in seen:
                continue
            seen.add(idx["contest"])
            loaded.append(idx)
    _INDEX_CACHE["sig"] = sig
    _INDEX_CACHE["data"] = loaded
    return loaded


def _source_parts(src):
    try:
        year = int(src.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    return (str(src.get("contest") or ""), year,
            str(src.get("paper") or ""), str(src.get("problem") or ""))


def _adapted_maps():
    """已改编题：带卷别用精确键，不带卷别（旧案例）用宽松键兜底。"""
    from app.dojo import cases  # 局部导入，避免模块级循环依赖

    exact, loose = {}, {}
    for col in load_collections():
        for q in col["items"]:
            contest, year, paper, problem = _source_parts(q.get("source") or {})
            if not (contest and year and problem):
                continue
            if paper:
                exact.setdefault((contest, year, paper, problem), q["id"])
            else:
                loose.setdefault((contest, year, problem), q["id"])
    for case in cases.load_cases():
        contest, year, paper, problem = _source_parts(case.get("source") or {})
        if not (contest and year and problem):
            continue
        if paper:
            exact.setdefault((contest, year, paper, problem), case["id"])
        else:
            loose.setdefault((contest, year, problem), case["id"])
    return exact, loose


def list_paper_index():
    """按赛事返回全量真题索引，并标出每个题位是否已有改编题。"""
    exact, loose = _adapted_maps()
    out = []
    for idx in load_indexes():
        contest = idx.get("contest") or ""
        papers, total, done = [], 0, 0
        for p in idx.get("papers") or []:
            paper = p.get("paper") or ""
            try:
                year = int(p.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            problems = []
            for q in p.get("problems") or []:
                no = str(q.get("no") or "")
                total += 1
                item = (exact.get((contest, year, paper, no))
                        or loose.get((contest, year, no)))
                if item:
                    done += 1
                problems.append({
                    "no": q.get("no"),
                    "points": q.get("points"),
                    "subject": q.get("subject") or "",
                    "topic": q.get("topic") or "",
                    "title": q.get("title") or "",
                    "head": q.get("head") or "",
                    "item": item,
                    "adapted": bool(item),
                })
            papers.append({
                "id": p.get("id") or "",
                "year": year,
                "edition": p.get("edition"),
                "paper": paper,
                "url": p.get("url") or "",
                "note": p.get("note") or "",
                "problems": problems,
            })
        out.append({
            "id": contest,
            "name": idx.get("name") or contest,
            "standard": idx.get("standard") or "",
            "note": idx.get("note") or "",
            "source_url": idx.get("source_url") or "",
            "years": sorted({p["year"] for p in papers if p["year"]}, reverse=True),
            "papers": papers,
            "total": total,
            "adapted": done,
        })
    return out


def _get(sid):
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        s = _SESSIONS.get(str(sid or ""))
    if not s:
        raise ResearchError("这个研究会话已经过期，重新开始一次")
    return s


def _gc_locked(now):
    dead = [sid for sid, s in _SESSIONS.items() if now - s["ts"] > SESSION_TTL_SEC]
    for sid in dead:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) >= MAX_SESSIONS:
        for sid, _s in sorted(_SESSIONS.items(), key=lambda kv: kv[1]["ts"])[
                :len(_SESSIONS) - MAX_SESSIONS + 1]:
            _SESSIONS.pop(sid, None)


def _act_public(act):
    out = packs.public_unit(act)
    out["label"] = act.get("label") or ""
    return out


def start(inquiry_id):
    try:
        _col, q = _find(inquiry_id)
    except ResearchError as e:
        raise ResearchError(str(e)) from None
    sid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        _SESSIONS[sid] = {
            "inquiry": q,
            "index": 0,
            "answers": {},
            "artifact": None,
            "revisions": 0,
            "finished": False,
            "ts": now,
        }
    return {
        "session_id": sid,
        "inquiry": public_inquiry(q, with_acts=True),
        "act": _act_public(q["acts"][0]),
        "index": 0,
        "total": len(q["acts"]),
        "stage": "acts",
    }


def state(session_id):
    s = _get(session_id)
    q = s["inquiry"]
    total = len(q["acts"])
    idx = s["index"]
    act = None
    stage = "artifact" if idx >= total else "acts"
    if idx < total:
        act = _act_public(q["acts"][idx])
    answered = []
    for i, a in enumerate(q["acts"]):
        r = s["answers"].get(a["id"])
        if r:
            answered.append({
                "index": i, "act_id": a["id"], "label": a.get("label") or "",
                "correct": bool(r.get("correct")), "score": r.get("score"),
                "ability": a.get("ability"),
                "ability_name": packs.ABILITY_NAMES.get(a.get("ability"), a.get("ability")),
            })
    return {
        "session_id": str(session_id),
        "inquiry": public_inquiry(q, with_acts=True),
        "index": idx,
        "total": total,
        "stage": stage,
        "act": act,
        "answered": answered,
        "artifact": copy.deepcopy(s["artifact"]),
        "revisions": s["revisions"],
        "finished": bool(s.get("finished")),
    }


def answer(session_id, act_id, value, elapsed_ms=0):
    s = _get(session_id)
    q = s["inquiry"]
    acts = q["acts"]
    idx = s["index"]
    if s.get("finished"):
        raise ResearchError("这项研究已经完成；要再走一遍请重新开始")
    if idx >= len(acts):
        raise ResearchError("这一轮的研究判断已经答完，去写研究结论")
    act = acts[idx]
    if str(act_id or "") != act["id"]:
        raise ResearchError("研究任务要按顺序推进，当前是第 %d 幕" % (idx + 1))
    with _LOCK:
        if act["id"] in s["answers"]:
            raise ResearchError("这一幕已经提交过了")
        s["answers"][act["id"]] = {"pending": True}
    try:
        result = primitives.judge(act, value)
    except primitives.JudgeError as e:
        with _LOCK:
            s["answers"].pop(act["id"], None)
        raise ResearchError(str(e)) from None
    try:
        elapsed = max(0, min(3600 * 1000, int(elapsed_ms or 0)))
    except (TypeError, ValueError):
        elapsed = 0
    ability = act.get("ability")
    with _LOCK:
        s["answers"][act["id"]] = {
            "correct": bool(result["correct"]), "score": result["score"],
            "ability": ability, "elapsed_ms": elapsed,
        }
        s["index"] = idx + 1
        s["ts"] = time.time()
        next_act = acts[idx + 1] if idx + 1 < len(acts) else None

    return {
        "inquiry_id": q["id"],
        "act_id": act["id"],
        "label": act.get("label") or "",
        "correct": bool(result["correct"]),
        "scored": result["scored"],
        "score": result["score"],
        "judge": result["judge"],
        "detail": result["detail"],
        "ability": ability,
        "ability_name": packs.ABILITY_NAMES.get(ability, ability),
        "expert": act.get("expert") or {},
        "card": act.get("card") or {},
        "index": idx,
        "total": len(acts),
        "next_act": _act_public(next_act) if next_act else None,
        "stage": "acts" if next_act else "artifact",
    }


def _diagnose(artifact, text):
    """只用关键词覆盖做诊断；绝不把它说成「证明正确」。"""
    unit = {
        "primitive": "short_text",
        "judge": "L3",
        "payload": {},
        "expert": {"rubric": artifact.get("rubric") or []},
    }
    result = primitives.judge(unit, text)
    by_id = {str(r.get("id")): r for r in (artifact.get("rubric") or [])}
    d = result["detail"]
    hits = [{"id": i, "text": (by_id.get(str(i)) or {}).get("text") or str(i)}
            for i in d.get("hits") or []]
    missing = [{"id": i, "text": (by_id.get(str(i)) or {}).get("text") or str(i)}
               for i in d.get("missing") or []]
    return {
        "text": text,
        "coverage": round(float(d.get("coverage") or 0), 3),
        "hits": hits,
        "missing": missing,
        "scored": False,
        "note": "只检查论证要素是否出现，不判断证明是否正确。",
    }


def submit_artifact(session_id, text):
    s = _get(session_id)
    q = s["inquiry"]
    if s.get("finished"):
        raise ResearchError("这项研究已经完成")
    if len(s["answers"]) < len(q["acts"]):
        raise ResearchError("先把 %d 幕的判断走完" % len(q["acts"]))
    if s["artifact"] is not None:
        raise ResearchError("研究结论已经提交过；需要修改请用「改一次」")
    text = str(text or "").strip()
    if len(text) < 40:
        raise ResearchError("研究结论至少写 40 字，把反例、条件和结论说清楚")
    text = text[:MAX_ARTIFACT_CHARS]
    art = q["artifact"]
    out = _diagnose(art, text)
    out["expert_sample"] = art.get("expert_sample") or ""
    out["revisions_left"] = 1
    with _LOCK:
        s["artifact"] = out
        s["ts"] = time.time()
    return out


def revise_artifact(session_id, text):
    s = _get(session_id)
    if s.get("finished"):
        raise ResearchError("这项研究已经完成")
    if s["artifact"] is None:
        raise ResearchError("先提交研究结论，再修改")
    if s["revisions"] >= 1:
        raise ResearchError("只能修改一次")
    text = str(text or "").strip()
    if len(text) < 40:
        raise ResearchError("修改后的结论至少写 40 字")
    text = text[:MAX_ARTIFACT_CHARS]
    art = s["inquiry"]["artifact"]
    out = _diagnose(art, text)
    out["expert_sample"] = art.get("expert_sample") or ""
    out["revisions_left"] = 0
    with _LOCK:
        s["artifact"] = out
        s["revisions"] += 1
        s["ts"] = time.time()
    return out


def finish(session_id):
    s = _get(session_id)
    q = s["inquiry"]
    acts = q["acts"]
    if len(s["answers"]) < len(acts):
        raise ResearchError("还有 %d 幕没答" % (len(acts) - len(s["answers"])))
    if s["artifact"] is None:
        raise ResearchError("先把研究结论写出来")

    rows, by_ability = [], {}
    total_score = 0.0
    first_break = None
    for i, a in enumerate(acts):
        r = s["answers"].get(a["id"]) or {}
        score = float(r.get("score") or 0)
        total_score += score
        ab = a.get("ability") or "framing"
        item = by_ability.setdefault(ab, {
            "ability": ab,
            "ability_name": packs.ABILITY_NAMES.get(ab, ab),
            "score": 0.0, "total": 0,
        })
        item["score"] += score
        item["total"] += 1
        if score < BREAK_SCORE and first_break is None:
            first_break = {
                "index": i, "act_id": a["id"], "label": a.get("label") or "",
                "ability": ab,
                "ability_name": packs.ABILITY_NAMES.get(ab, ab),
                "score": round(score, 3),
            }
        rows.append({
            "index": i, "act_id": a["id"], "label": a.get("label") or "",
            "ability": ab,
            "ability_name": packs.ABILITY_NAMES.get(ab, ab),
            "correct": bool(r.get("correct")), "score": round(score, 3),
            "elapsed_ms": int(r.get("elapsed_ms") or 0),
        })

    abilities = [{
        "ability": k,
        "ability_name": v["ability_name"],
        "score": round(v["score"] / max(1, v["total"]), 3),
        "total": v["total"],
    } for k, v in sorted(by_ability.items())]

    coverage = float((s["artifact"] or {}).get("coverage") or 0)
    if first_break:
        verdict = "第 %d 幕（%s）还没站稳" % (first_break["index"] + 1, first_break["ability_name"])
        hint = "先回到这一幕的方法卡，再重走整个研究；不要只背结论。"
    elif coverage < ARTIFACT_WEAK:
        verdict = "判断链走完，但研究结论还缺关键要素"
        hint = "对照缺失项补一次：反例、条件、结论各说清楚了吗？"
    else:
        verdict = "研究链完整"
        hint = "换一个主题，或把这个结论讲给别人听一遍。"

    with _LOCK:
        s["finished"] = True
        s["ts"] = time.time()

    return {
        "inquiry_id": q["id"],
        "title": q.get("title") or q["id"],
        "subject": q.get("subject") or "",
        "track": q.get("track") or "",
        "total": len(acts),
        "avg_score": round(total_score / max(1, len(acts)), 3),
        "acts": rows,
        "by_ability": abilities,
        "first_break": first_break,
        "artifact": {
            "coverage": round(coverage, 3),
            "hits": (s["artifact"] or {}).get("hits") or [],
            "missing": (s["artifact"] or {}).get("missing") or [],
            "text": (s["artifact"] or {}).get("text") or "",
            "expert_sample": (s["artifact"] or {}).get("expert_sample") or "",
            "prompt": (q.get("artifact") or {}).get("prompt") or "",
            "revisions": s["revisions"],
            "scored": False,
        },
        "verdict": verdict,
        "hint": hint,
        "elapsed_ms": sum(int((s["answers"].get(a["id"]) or {}).get("elapsed_ms") or 0)
                          for a in acts),
    }


def reset_store():
    with _LOCK:
        _SESSIONS.clear()
