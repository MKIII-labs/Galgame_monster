# -*- coding: utf-8 -*-
"""教材大纲 → AI 研究任务草稿 → 审核 → 写入正式题库。

与「材料导入」的区别：
- 这里按教材的章/节生成完整研究任务（6 幕 + 研究结论），不是单题；
- 草稿只存在内存，审核通过后才写入 resources/dojo/inquiries/；
- 生成结果必须通过 research 的正式校验，校验不过直接拒绝。
"""
import json
import os
import re
import tempfile
import threading
import time
import uuid

from app import llm, prompts
from app.config import get_api_conf
from app.dojo import research
from app.store import DATA_DIR, RESOURCES_DIR, drop_cache, read_json, write_json

SYLLABUS_DIRS = (
    os.path.join(RESOURCES_DIR, "dojo", "syllabi"),
    os.path.join(DATA_DIR, "dojo", "syllabi"),
)
INQUIRY_ROOT = os.path.join(RESOURCES_DIR, "dojo", "inquiries")
DRAFTS_FILE = os.path.join(DATA_DIR, "dojo", "authoring_drafts.json")
DRAFT_TTL_SEC = 7 * 24 * 3600
MAX_DRAFTS = 300
SYLLABUS_ORDER = [
    "math-analysis", "advanced-algebra", "analytic-geometry", "number-theory",
    "abstract-algebra", "ode", "discrete-math", "math-modeling",
]

_CACHE = {"sig": None, "syllabi": []}
_DRAFTS = {}
_LOADED = False
_LOCK = threading.RLock()


class AuthoringError(Exception):
    pass


# ---------------- 大纲 ----------------

def _signature():
    sig = []
    for root in SYLLABUS_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                sig.append((path, os.path.getmtime(path)))
            except OSError:
                pass
    return tuple(sig)


def load_syllabi():
    sig = _signature()
    if _CACHE["sig"] == sig:
        return _CACHE["syllabi"]
    out, seen = [], set()
    for root in SYLLABUS_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print("[大纲] 跳过 %s：%s" % (name, e))
                continue
            if not isinstance(data, dict) or not data.get("id") or data["id"] in seen:
                continue
            if not isinstance(data.get("chapters"), list):
                continue
            seen.add(data["id"])
            out.append(data)
    _CACHE["sig"] = sig
    _CACHE["syllabi"] = sorted(
        out,
        key=lambda s: (SYLLABUS_ORDER.index(s["id"]) if s["id"] in SYLLABUS_ORDER else 99,
                       s.get("course") or ""),
    )
    return _CACHE["syllabi"]


def reset_cache():
    _CACHE["sig"] = None
    _CACHE["syllabi"] = []


def _find(syllabus_id):
    sid = str(syllabus_id or "")
    for s in load_syllabi():
        if s.get("id") == sid:
            return s
    raise AuthoringError("没有这份教材大纲：%s" % (sid or "（空）"))


def _chapters(syl):
    out = []
    for ch in syl.get("chapters") or []:
        secs = [{
            "id": sec.get("id") or "",
            "title": sec.get("title") or "",
        } for sec in (ch.get("sections") or [])]
        out.append({
            "id": ch.get("id") or "",
            "title": ch.get("title") or "",
            "optional": bool(ch.get("optional")),
            "sections": secs,
        })
    return out


def _chapter(syl, chapter_id):
    cid = str(chapter_id or "")
    for ch in _chapters(syl):
        if ch["id"] == cid:
            return ch
    raise AuthoringError("没有这一章：%s" % (cid or "（空）"))


def _committed_ids():
    return {q.get("id") for q in research.list_inquiries()}


def list_syllabi():
    done = _committed_ids()
    out = []
    for syl in load_syllabi():
        chapters = _chapters(syl)
        out.append({
            "id": syl.get("id"),
            "course": syl.get("course") or "",
            "textbook": syl.get("textbook") or "",
            "note": syl.get("note") or "",
            "level": syl.get("level") or "chapter",
            "chapters": len(chapters),
            "sections": sum(len(ch["sections"]) for ch in chapters),
            "committed": sum(1 for ch in chapters if _item_id(syl, ch["id"]) in done),
        })
    return out


def syllabus_detail(syllabus_id):
    syl = _find(syllabus_id)
    done = _committed_ids()
    chapters = []
    for ch in _chapters(syl):
        chapters.append({
            "id": ch["id"],
            "title": ch["title"],
            "optional": ch["optional"],
            "sections": ch["sections"],
            "committed": _item_id(syl, ch["id"]) in done,
            "draft": _draft_of_chapter(syl["id"], ch["id"]),
        })
    return {
        "id": syl.get("id"),
        "course": syl.get("course") or "",
        "textbook": syl.get("textbook") or "",
        "note": syl.get("note") or "",
        "chapters": chapters,
    }


# ---------------- 草稿 ----------------

def _item_id(syl, chapter_id):
    prefix = str(syl.get("item_prefix") or syl.get("id") or "course")
    return prefix + "-" + str(chapter_id or "").replace("_", "-")



def _extract_object(text):
    s = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise AuthoringError("模型没有返回 JSON 对象")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        raise AuthoringError("模型返回的 JSON 无法解析")
    if not isinstance(data, dict):
        raise AuthoringError("模型返回的不是对象")
    return data


def _normalize(doc, course, chapter, item_id):
    if not isinstance(doc, dict):
        raise AuthoringError("草稿不是对象")
    doc["id"] = item_id
    doc["subject"] = course
    doc["track"] = "教材"
    acts = doc.get("acts")
    if not isinstance(acts, list):
        raise AuthoringError("草稿缺少 acts")
    if len(acts) != 6:
        raise AuthoringError("草稿必须恰好 6 幕（当前 %d 幕）" % len(acts))
    for i, act in enumerate(acts):
        if isinstance(act, dict):
            act["id"] = "%s-a%d" % (item_id, i + 1)
            act.setdefault("minutes", 5)
    doc["title"] = str(doc.get("title") or chapter.get("title") or "").strip()
    return doc


def _validate(doc):
    col = {"id": "authoring", "name": "审核草稿", "standard": "教材生成"}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="inquiry_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        research._load_inquiry(tmp, col)
    except research.ResearchError as e:
        raise AuthoringError(str(e)) from None
    except Exception as e:
        raise AuthoringError("草稿校验失败：%s" % e) from None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return doc


def _build_messages(syl, ch):
    system = prompts.part("dojo_inquiry", "system")
    user = prompts.part(
        "dojo_inquiry", "user",
        course=syl.get("course") or "",
        textbook=syl.get("textbook") or "",
        chapter=ch.get("title") or "",
        sections="、".join(s["title"] for s in ch.get("sections") or []) or "（无分节信息）",
        optional="是" if ch.get("optional") else "否",
        item_id=_item_id(syl, ch["id"]),
    )
    if not system or not user:
        raise AuthoringError("生成提示词模板缺失（resources/prompts/dojo_inquiry.md）")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _ensure_loaded():
    """草稿落盘：第一次访问时从 data/dojo/authoring_drafts.json 恢复。"""
    global _LOADED
    if _LOADED:
        return
    data = read_json(DRAFTS_FILE, {"drafts": []})
    rows = data.get("drafts") if isinstance(data, dict) else []
    now = time.time()
    for d in rows or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        if now - float(d.get("created") or 0) > DRAFT_TTL_SEC:
            continue
        _DRAFTS[d["id"]] = d
    _LOADED = True


def _save_locked():
    os.makedirs(os.path.dirname(DRAFTS_FILE), exist_ok=True)
    rows = sorted(_DRAFTS.values(), key=lambda x: x["created"], reverse=True)
    write_json(DRAFTS_FILE, {"drafts": rows}, indent=None)


def _gc_locked(now):
    dead = [k for k, v in _DRAFTS.items() if now - v["created"] > DRAFT_TTL_SEC]
    for k in dead:
        _DRAFTS.pop(k, None)
    if len(_DRAFTS) > MAX_DRAFTS:
        for k in sorted(_DRAFTS, key=lambda x: _DRAFTS[x]["created"])[:len(_DRAFTS) - MAX_DRAFTS]:
            _DRAFTS.pop(k, None)


def _draft_of_chapter(syllabus_id, chapter_id):
    with _LOCK:
        _ensure_loaded()
        for d in _DRAFTS.values():
            if d["syllabus_id"] == syllabus_id and d["chapter_id"] == chapter_id:
                return d["id"]
    return None


def public_draft(draft_id):
    with _LOCK:
        _ensure_loaded()
        d = _DRAFTS.get(str(draft_id or ""))
    if not d:
        raise AuthoringError("草稿不存在或已过期")
    return {
        "id": d["id"],
        "syllabus_id": d["syllabus_id"],
        "chapter_id": d["chapter_id"],
        "chapter_title": d["chapter_title"],
        "created": int(d["created"]),
        "doc": json.loads(json.dumps(d["doc"], ensure_ascii=False)),
    }


def list_drafts():
    with _LOCK:
        _ensure_loaded()
        rows = sorted(_DRAFTS.values(), key=lambda x: x["created"], reverse=True)
    out = []
    for d in rows:
        out.append({
            "id": d["id"],
            "syllabus_id": d["syllabus_id"],
            "chapter_id": d["chapter_id"],
            "chapter_title": d["chapter_title"],
            "title": (d["doc"] or {}).get("title") or "",
            "created": int(d["created"]),
        })
    return out


def generate(syllabus_id, chapter_id):
    syl = _find(syllabus_id)
    ch = _chapter(syl, chapter_id)
    item_id = _item_id(syl, ch["id"])
    if item_id in _committed_ids():
        raise AuthoringError("这一章已经有正式主题，先不要重复生成")
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        raise AuthoringError("还没有配置聊天 API 密钥，无法生成草稿")
    messages = _build_messages(syl, ch)
    doc = None
    valid = False
    last_err = None
    for attempt in (1, 2):
        res = llm.stream_chat(api_key, api_url, api_model, messages, allow_tool=False)
        visible = str(res.get("visible") or "").strip()
        if not visible:
            last_err = AuthoringError("模型没有返回内容")
            continue
        try:
            doc = _normalize(_extract_object(visible), syl.get("course") or "", ch, item_id)
            _validate(doc)
            valid = True
            break
        except AuthoringError as e:
            last_err = e
            if attempt == 1:
                repair = [{"role": "user", "content":
                           "这份草稿未通过校验：%s\n请只修正这些问题并重新输出完整 JSON；仍然必须 6 幕，不要解释。"
                           % e}]
                if doc is not None:
                    repair.insert(0, {"role": "assistant",
                                      "content": json.dumps(doc, ensure_ascii=False)})
                messages = messages + repair
    if not valid:
        raise AuthoringError("生成失败（已自动重试一次）：%s" % last_err)
    did = "dr_" + uuid.uuid4().hex[:12]
    with _LOCK:
        _ensure_loaded()
        _gc_locked(time.time())
        _DRAFTS[did] = {
            "id": did,
            "syllabus_id": syl["id"],
            "chapter_id": ch["id"],
            "chapter_title": ch.get("title") or "",
            "doc": doc,
            "created": time.time(),
        }
        _save_locked()
    return public_draft(did)


def update(draft_id, doc):
    with _LOCK:
        _ensure_loaded()
        d = _DRAFTS.get(str(draft_id or ""))
    if not d:
        raise AuthoringError("草稿不存在或已过期")
    if not isinstance(doc, dict):
        raise AuthoringError("草稿不是对象")
    syl = _find(d["syllabus_id"])
    ch = _chapter(syl, d["chapter_id"])
    clean = _normalize(doc, syl.get("course") or "", ch, _item_id(syl, ch["id"]))
    _validate(clean)
    with _LOCK:
        d["doc"] = clean
        _save_locked()
    return public_draft(draft_id)


def reject(draft_id):
    did = str(draft_id or "")
    with _LOCK:
        _ensure_loaded()
        if did not in _DRAFTS:
            raise AuthoringError("草稿不存在或已过期")
        _DRAFTS.pop(did, None)
        _save_locked()
    return {"rejected": did}


def _collection(syl):
    return {
        "id": "course-" + syl["id"],
        "name": (syl.get("course") or syl["id"]) + " · 教材",
        "standard": syl.get("textbook") or syl.get("course") or "",
        "note": "按教材章节生成；审核通过后入库。",
        "tracks": ["教材"],
    }


def commit(draft_id):
    with _LOCK:
        _ensure_loaded()
        d = _DRAFTS.get(str(draft_id or ""))
    if not d:
        raise AuthoringError("草稿不存在或已过期")
    syl = _find(d["syllabus_id"])
    doc = _validate(d["doc"])
    col = _collection(syl)
    col_dir = os.path.join(INQUIRY_ROOT, col["id"])
    items_dir = os.path.join(col_dir, "items")
    os.makedirs(items_dir, exist_ok=True)
    col_path = os.path.join(col_dir, "collection.json")
    if not os.path.isfile(col_path):
        with open(col_path, "w", encoding="utf-8") as f:
            json.dump(col, f, ensure_ascii=False, indent=2)
    item_path = os.path.join(items_dir, doc["id"] + ".json")
    if os.path.isfile(item_path):
        raise AuthoringError("这一节已经有正式主题：%s" % doc["id"])
    with open(item_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    research.reset_cache()
    with _LOCK:
        _DRAFTS.pop(draft_id, None)
        _save_locked()
    try:
        rel_path = os.path.relpath(item_path, RESOURCES_DIR)
    except ValueError:
        rel_path = item_path
    return {
        "collection": col["id"],
        "item_id": doc["id"],
        "title": doc.get("title") or "",
        "path": rel_path,
    }


def reset_store():
    """测试用：清空内存草稿与落盘文件。"""
    global _LOADED
    with _LOCK:
        _DRAFTS.clear()
        _LOADED = True
        drop_cache(DRAFTS_FILE)
        try:
            os.remove(DRAFTS_FILE)
        except OSError:
            pass
