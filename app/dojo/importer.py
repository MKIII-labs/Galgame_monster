# -*- coding: utf-8 -*-
"""材料 → 训练单元草稿 → 用户确认 → 个人场景包。

防编造三道闸（P1 的底线，不要为了「生成更多」放松）：
1. 先抽取、后生成：生成只拿 parse_material 抽出的正文；
2. 只给「建议答案」：草稿用 suggested，用户确认后才变成 expert.choice / segments；
3. 出处校验：source_quote 必须能在原文里找到（去空白后 ≥8 字子串），否则整条丢弃。

材料只存在内存（2 小时），不落盘；用户确认后才写出一个 personal 包。
"""
import base64
import json
import re
import threading
import time
import uuid

from app import docparse, llm, prompts
from app.config import get_api_conf
from app.dojo import packs

MAX_MATERIAL_BYTES = 20 * 1024 * 1024
MAX_MATERIAL_CHARS = 20000
MAX_PROMPT_CHARS = 12000
MAX_UNITS = 8
MIN_QUOTE_CHARS = 8
MATERIAL_TTL_SEC = 2 * 3600
ALLOWED_PRIMITIVES = ("single_choice", "multi_select", "annotate", "short_text")
ALLOWED_ABILITIES = packs.ABILITIES

_MATERIALS = {}
_LOCK = threading.RLock()


class DojoImportError(Exception):
    """导入流程里可以直接展示给用户的错误。"""


# ---------------- 材料 ----------------

def _gc_locked(now):
    dead = [k for k, v in _MATERIALS.items() if now - v["ts"] > MATERIAL_TTL_SEC]
    for k in dead:
        _MATERIALS.pop(k, None)


def _segments(text, limit=40):
    """按空行切段。分段只用于预览与定位，不参与判分。"""
    out = []
    for i, part in enumerate(re.split(r"\n\s*\n+", text)):
        part = part.strip()
        if not part:
            continue
        out.append({"id": "p%d" % (i + 1), "text": part[:800]})
        if len(out) >= limit:
            break
    return out


def parse_material(name, b64="", text=""):
    """抽取材料正文。支持两条入口：上传文件（base64）或直接粘贴文本。

    返回 {material_id, name, chars, warn, preview, segments}。
    """
    raw_name = str(name or "材料").strip()[:200] or "材料"
    pasted = str(text or "").strip()
    if pasted:
        content, warn = pasted[:MAX_MATERIAL_CHARS], ""
        if len(pasted) > MAX_MATERIAL_CHARS:
            warn = "文本较长，只取了前 %d 字" % MAX_MATERIAL_CHARS
    else:
        data = str(b64 or "").strip()
        if not data:
            raise DojoImportError("没有收到文件内容")
        if len(data) > MAX_MATERIAL_BYTES * 1.4:
            raise DojoImportError("文件太大（上限 20MB）")
        try:
            raw = base64.b64decode(data)
        except Exception:
            raise DojoImportError("文件内容不是合法的 base64") from None
        if len(raw) > MAX_MATERIAL_BYTES:
            raise DojoImportError("文件太大（上限 20MB）")
        content, warn = docparse.extract(raw_name, raw)
        if not content:
            raise DojoImportError(warn or "没有提取到文本")
        content = content[:MAX_MATERIAL_CHARS]

    text = content.strip()
    if len(text) < 80:
        raise DojoImportError("材料太短（不足 80 字），没法生成有依据的判断题")
    mid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        _MATERIALS[mid] = {"name": raw_name, "text": text, "ts": now}
    return {
        "material_id": mid,
        "name": raw_name,
        "chars": len(text),
        "warn": warn or "",
        "preview": text[:1200],
        "segments": _segments(text),
    }


def _get_material(mid):
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        item = _MATERIALS.get(str(mid or ""))
    if not item:
        raise DojoImportError("材料已过期，请重新选择文件")
    return item


# ---------------- 生成草稿 ----------------

def _norm(text):
    return re.sub(r"\s+", "", str(text or "")).lower()


def _quote_ok(quote, material_text):
    q = _norm(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in _norm(material_text)


def _extract_array(text):
    s = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end <= start:
        raise DojoImportError("模型没有返回 JSON 数组")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        raise DojoImportError("模型返回的 JSON 无法解析") from None
    if not isinstance(data, list):
        raise DojoImportError("模型返回的不是数组")
    return data


def _options(raw):
    """选项统一成 [{key,text}]；模型给字符串数组时自动补 A/B/C。"""
    out = []
    for i, opt in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(opt, dict):
            key = str(opt.get("key") or chr(65 + i)).upper()
            text = str(opt.get("text") or "").strip()
        else:
            key = chr(65 + i)
            text = str(opt or "").strip()
        if text:
            out.append({"key": key, "text": text[:300]})
    return out


def _pick_keys(suggested, options):
    """把模型给的建议答案（key 或选项原文）统一成 key 列表。"""
    keys = [o["key"] for o in options]
    if isinstance(suggested, str):
        val = suggested.strip()
        if val.upper() in keys:
            return [val.upper()]
        for o in options:
            if _norm(o["text"]) == _norm(val):
                return [o["key"]]
        return []
    if isinstance(suggested, list):
        out = []
        for item in suggested:
            out.extend(_pick_keys(item, options))
        return list(dict.fromkeys(out))
    return []


def _validate_draft(item, material_text):
    """返回 (unit, None) 或 (None, 原因)。unit 里只有 suggested，没有正式答案。"""
    if not isinstance(item, dict):
        return None, "不是对象"
    ability = str(item.get("ability") or "").strip()
    if ability not in ALLOWED_ABILITIES:
        return None, "能力维度不认识：%s" % (ability or "空")
    primitive = str(item.get("primitive") or "").strip()
    if primitive not in ALLOWED_PRIMITIVES:
        return None, "题型不在导入支持范围：%s" % (primitive or "空")

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    situation = str(payload.get("situation") or "").strip()
    if len(situation) < 10:
        return None, "题面太短"

    expert = item.get("expert") if isinstance(item.get("expert"), dict) else {}
    why = str(expert.get("why") or "").strip()
    if len(why) < 8:
        return None, "缺少专家理由"
    signals = [str(s).strip() for s in (expert.get("signals") or []) if str(s).strip()]
    if not signals:
        return None, "缺少识别信号"
    quote = str(expert.get("source_quote") or "").strip()
    if not _quote_ok(quote, material_text):
        return None, "原文依据在材料里找不到"

    card = item.get("card") if isinstance(item.get("card"), dict) else {}
    for field in ("title", "trigger", "first_step", "pitfall"):
        if not str(card.get(field) or "").strip():
            return None, "方法卡缺少 %s" % field

    unit_payload = {"situation": situation[:2000]}
    suggested = None
    judge = "L2"
    expert_tail = {}

    if primitive in ("single_choice", "multi_select"):
        options = _options(payload.get("options"))
        if len(options) < 3:
            return None, "选项少于 3 个"
        unit_payload["options"] = options
        picked = _pick_keys(expert.get("suggested"), options)
        if primitive == "single_choice":
            if len(picked) != 1:
                return None, "建议答案是空的或不止一个"
            suggested = picked[0]
            traps = expert.get("traps") if isinstance(expert.get("traps"), dict) else {}
            keys = {o["key"] for o in options}
            bad = [k for k in keys if k != suggested and not str(traps.get(k) or "").strip()]
            if bad:
                return None, "错误选项没有给理由：%s" % ",".join(sorted(bad))
            expert_tail = {"traps": {k: str(v)[:300] for k, v in traps.items() if k in keys}}
        else:
            if not picked:
                return None, "多选建议答案是空的"
            suggested = picked

    elif primitive == "annotate":
        segs = []
        for i, seg in enumerate(payload.get("segments") or []):
            if isinstance(seg, dict):
                sid = str(seg.get("id") or "s%d" % (i + 1))
                text = str(seg.get("text") or "").strip()
            else:
                sid, text = "s%d" % (i + 1), str(seg or "").strip()
            if text:
                segs.append({"id": sid, "text": text[:300]})
        if len(segs) < 2:
            return None, "标注题至少要有两个句子"
        ids = [s["id"] for s in segs]
        picked = [str(x) for x in (expert.get("suggested_segments") or [])]
        picked = [x for x in picked if x in ids]
        if not picked:
            return None, "标注题没有给关键句"
        unit_payload["segments"] = segs
        suggested = picked

    else:                                     # short_text
        judge = "L3"
        rubric = []
        for i, r in enumerate(expert.get("rubric") or []):
            if not isinstance(r, dict):
                continue
            kws = [str(k).strip() for k in (r.get("keywords") or []) if str(k).strip()]
            if not kws:
                continue
            rubric.append({
                "id": str(r.get("id") or "r%d" % (i + 1)),
                "text": str(r.get("text") or "")[:200],
                "keywords": kws[:8],
                "weight": float(r.get("weight") or 1),
            })
        if not rubric:
            return None, "开放题缺少评分要点"
        suggested = ""
        expert_tail = {"rubric": rubric}
        sample = str(expert.get("sample") or "").strip()
        if sample:
            expert_tail["sample"] = sample[:600]

    try:
        minutes = int(item.get("minutes") or 4)
    except (TypeError, ValueError):
        minutes = 4
    minutes = max(2, min(15, minutes))

    return {
        "ability": ability,
        "primitive": primitive,
        "judge": judge,
        "minutes": minutes,
        "payload": unit_payload,
        "expert": {
            "suggested": suggested,
            "why": why[:800],
            "signals": [s[:120] for s in signals[:5]],
            "source_quote": quote[:400],
            **expert_tail,
        },
        "card": {k: str(card.get(k) or "")[:200]
                 for k in ("title", "trigger", "first_step", "pitfall")},
        "confidence": str(item.get("confidence") or "medium").lower(),
    }, None


def _build_messages(material, count, standard, dimensions):
    dims = "、".join(dimensions or []) or "未指定"
    sys = prompts.part("dojo_import", "system")
    user = prompts.part(
        "dojo_import", "user",
        count=str(count),
        standard=standard or "未指定",
        dims=dims,
        abilities="、".join("%s（%s）" % (k, v) for k, v in packs.ABILITY_NAMES.items()),
        primitives="single_choice（单选）、multi_select（多选）、annotate（标出关键句）、short_text（一句话理由）",
        text=material["text"][:MAX_PROMPT_CHARS],
    )
    if not sys or not user:
        raise DojoImportError("导入提示词模板缺失（resources/prompts/dojo_import.md）")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def draft(material_id, count=4, standard="", dimensions=None):
    """调用当前聊天模型生成草稿；通过不了出处校验的条目会被拒绝并给出原因。"""
    material = _get_material(material_id)
    if not isinstance(dimensions, list):
        dimensions = None
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(6, count))
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        raise DojoImportError("还没有配置聊天 API 密钥，无法生成草稿")
    messages = _build_messages(material, count, standard, dimensions)
    res = llm.stream_chat(api_key, api_url, api_model, messages, allow_tool=False)
    visible = str(res.get("visible") or "").strip()
    if not visible:
        raise DojoImportError("模型没有返回内容")
    items = _extract_array(visible)

    drafts, rejected = [], []
    for i, item in enumerate(items):
        unit, reason = _validate_draft(item, material["text"])
        if unit:
            unit["draft_id"] = "d%d" % (len(drafts) + 1)
            drafts.append(unit)
            if len(drafts) >= count:
                break
        else:
            rejected.append({"index": i + 1, "reason": reason})
    return {
        "material_id": material_id,
        "material_name": material["name"],
        "drafts": drafts,
        "rejected": rejected,
        "usage": res.get("usage") or {},
    }


# ---------------- 确认后落盘 ----------------

def _normalize_confirmed(unit, idx, material_name):
    """把前端确认过的草稿转成正式 unit；缺确认标记直接拒绝。"""
    if not isinstance(unit, dict):
        raise DojoImportError("单元格式不对")
    if not unit.get("confirmed"):
        raise DojoImportError("还有单元没有确认")
    primitive = str(unit.get("primitive") or "")
    ability = str(unit.get("ability") or "")
    if primitive not in ALLOWED_PRIMITIVES or ability not in ALLOWED_ABILITIES:
        raise DojoImportError("单元的能力或题型不合法")
    expert = dict(unit.get("expert") or {})
    suggested = expert.get("suggested")
    if primitive in ("single_choice", "multi_select"):
        choice = expert.get("choice") or suggested
        if not choice:
            raise DojoImportError("第 %d 条没有确认答案" % (idx + 1))
        expert["choice"] = choice
    elif primitive == "annotate":
        segs = expert.get("segments") or suggested
        if not segs:
            raise DojoImportError("第 %d 条没有确认关键句" % (idx + 1))
        expert["segments"] = segs
    for key in ("suggested", "confidence"):
        expert.pop(key, None)
    quote = str(expert.pop("source_quote", "") or "").strip()
    if not quote:
        raise DojoImportError("第 %d 条缺少原文依据" % (idx + 1))

    card = unit.get("card") or {}
    return {
        "id": "imp-%02d" % (idx + 1),
        "ability": ability,
        "primitive": primitive,
        "judge": "L3" if primitive == "short_text" else "L2",
        "minutes": max(2, min(15, int(unit.get("minutes") or 4))),
        "payload": unit.get("payload") or {},
        "expert": expert,
        "card": {k: str(card.get(k) or "") for k in ("title", "trigger", "first_step", "pitfall")},
        "source_ref": {"doc": material_name or "imported", "loc": "quote", "quote": quote[:400]},
    }


def commit(name, units, standard="", dimensions=None, material_name="", desc=""):
    name = str(name or "").strip()[:40]
    if not name:
        raise DojoImportError("给这个场景起个名字")
    if not isinstance(units, list) or not units:
        raise DojoImportError("没有可保存的单元")
    if len(units) > MAX_UNITS:
        raise DojoImportError("一次最多保存 %d 个单元" % MAX_UNITS)
    if not isinstance(dimensions, list):
        dimensions = None

    clean = [_normalize_confirmed(u, i, material_name) for i, u in enumerate(units)]
    counts = {}
    for u in clean:
        counts[u["ability"]] = counts.get(u["ability"], 0) + 1
    total = sum(counts.values())
    manifest = {
        "id": packs.pack_slug(name),
        "name": name,
        "desc": str(desc or "由本地材料导入的个人场景；内容与答案均由你确认。")[:200],
        "version": 1,
        "grade": "personal",
        "alignment": {
            "standard": str(standard or "自定义材料")[:80],
            "dimensions": [str(d)[:40] for d in (dimensions or ["自定义材料"])][:8],
        },
        "ability_weights": {k: round(v / total, 3) for k, v in counts.items()},
        "entry": {"minutes": 6, "first_unit": clean[0]["id"]},
        "sequence": [u["id"] for u in clean],
    }
    try:
        path = packs.write_pack(manifest["id"], manifest, clean)
    except packs.PackError as e:
        raise DojoImportError("保存失败：%s" % e) from None
    return {"pack_id": manifest["id"], "path": path, "unit_count": len(clean)}


def reset_materials():
    with _LOCK:
        _MATERIALS.clear()
