# -*- coding: utf-8 -*-
"""会话存档：每轮自动留档，可从旧档「回退」或「分叉」出新线。

存档单独放在 data/saves/<会话id>/ 下，不塞进 sessions.json ——
避免把主文件撑大、拖慢每次读写。

每个存档点一个文件（<save_id>.json）+ 一份只含元信息的 index.json：
旧版把所有快照塞进一个 <会话id>.json，每轮收尾都要把整份读出来再重写一遍，
长会话能涨到 900MB+（实测 s_2809c1a88538.json = 920MB），
表现为「点停止后界面要等好几秒」。现在每轮只写一个新的快照文件。
"""
import copy
import os
import re
import threading
import time
import uuid

from app.store import DATA_DIR, drop_cache, read_json, write_json, new_id, clone_containers
from app.sessions import load_sessions, find_session, ensure_session_kb_fields

SAVES_DIR = os.path.join(DATA_DIR, "saves")
SAVE_MAX = 60          # 每个会话最多保留的存档点数量（超出丢最旧的，但保留最早一个当「开局」）

# 可被存档 / 还原的会话字段（id / updated_at 等不参与）
RESTORE_FIELDS = (
    "title", "messages", "summary", "summary_upto", "kb_ids", "ai_write", "memory",
    "use_profile", "use_memory",
    "gal_enabled", "gal_character_id", "gal_bg", "gal_bgm", "gal_stats",
    "gal_map", "gal_node_id", "gal_round",
    "gal_auto_on", "gal_auto_story", "gal_auto_outline", "gal_auto_acts_done", "gal_recap",
    "gal_recap_upto",
)

META_FIELDS = ("id", "ts", "label", "manual", "msg_count")

_SAVE_LOCK = threading.Lock()
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(sid):
    return _SID_RE.sub("_", str(sid or ""))[:80]


def _dir(sid):
    return os.path.join(SAVES_DIR, _safe(sid))


def _index_path(sid):
    return os.path.join(_dir(sid), "index.json")


def _state_path(sid, save_id):
    return os.path.join(_dir(sid), _safe(save_id) + ".json")


def _legacy_path(sid):
    """旧版：一个会话一个文件，items 里直接塞 state"""
    return os.path.join(SAVES_DIR, _safe(sid) + ".json")


def _migrate(sid):
    """把旧版单文件拆成「index + 每档一个文件」，拆完删掉旧文件。

    写 index 放在最后：中途失败时旧文件还在，下次进来重试即可。
    """
    legacy = _legacy_path(sid)
    data = read_json(legacy, {}) or {}
    items = data.get("items") or []
    metas = []
    try:
        os.makedirs(_dir(sid), exist_ok=True)
    except OSError:
        pass
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        save_id = str(it["id"])
        state = it.get("state")
        if isinstance(state, dict):
            write_json(_state_path(sid, save_id),
                       {"id": save_id, "ts": it.get("ts") or 0, "state": state},
                       indent=None, remember=False)
        metas.append({k: it.get(k) for k in META_FIELDS})
    write_json(_index_path(sid), {"session_id": sid, "items": metas}, indent=None)
    drop_cache(legacy)                 # 旧文件可能很大，解析结果别留在内存里
    try:
        os.remove(legacy)
    except OSError as e:
        print(f"[存档] 旧存档文件删除失败：{e}")
    print(f"[存档] 已迁移为分档存储：{sid}（{len(metas)} 档）")
    return {"session_id": sid, "items": metas}


def _absorb_legacy(sid, items):
    """index 已经存在、旧单文件又冒出来了（回滚跑过旧版本，或旧进程晚一步收尾）。

    把旧文件里 index 还没有的档并进来再删掉，别让 900MB 的老文件一直躺在那儿。
    """
    legacy = _legacy_path(sid)
    data = read_json(legacy, {}) or {}
    known = {it.get("id") for it in items if isinstance(it, dict)}
    added = 0
    for it in (data.get("items") or []):
        if not isinstance(it, dict) or not it.get("id") or it["id"] in known:
            continue
        save_id = str(it["id"])
        if not isinstance(it.get("state"), dict):
            continue
        write_json(_state_path(sid, save_id),
                   {"id": save_id, "ts": it.get("ts") or 0, "state": it["state"]},
                   indent=None, remember=False)
        items.append({k: it.get(k) for k in META_FIELDS})
        added += 1
    if added:
        items.sort(key=lambda x: x.get("ts") or 0)
        for old in _trim(items):
            _remove_state(sid, old.get("id"))
        _write_index(sid, items)
        print(f"[存档] 旧单文件里有 {added} 个新档，已并入分档存储：{sid}")
    drop_cache(legacy)
    try:
        os.remove(legacy)
    except OSError as e:
        print(f"[存档] 旧存档文件删除失败：{e}")


def _load_index(sid):
    idx = read_json(_index_path(sid), None)
    if isinstance(idx, dict) and isinstance(idx.get("items"), list):
        if os.path.exists(_legacy_path(sid)):
            items = list(idx["items"])
            _absorb_legacy(sid, items)
            idx = {"session_id": sid, "items": items}
        return idx
    if os.path.exists(_legacy_path(sid)):
        return _migrate(sid)
    return {"session_id": sid, "items": []}


def _write_index(sid, items):
    try:
        os.makedirs(_dir(sid), exist_ok=True)
    except OSError:
        pass
    write_json(_index_path(sid), {"session_id": sid, "items": items}, indent=None)


def _remove_state(sid, save_id):
    p = _state_path(sid, save_id)
    try:
        os.remove(p)
    except OSError:
        pass
    drop_cache(p)


def drop_saves(sid):
    """删除某会话的存档（会话被删时连带清理）"""
    for p in (_legacy_path(sid),):
        try:
            if os.path.exists(p):
                os.remove(p)
                drop_cache(p)
        except OSError as e:
            print(f"[存档] 清理失败：{e}")
    d = _dir(sid)
    try:
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    os.remove(p)
                except OSError:
                    pass
                drop_cache(p)
            os.rmdir(d)
    except OSError as e:
        print(f"[存档] 清理失败：{e}")


def _items(sid):
    return list(_load_index(sid).get("items") or [])


def list_saves(sid):
    """存档点列表（只含元信息，不含 state；最新在前）"""
    out = []
    for it in _items(sid):
        out.append({"id": it.get("id"), "ts": it.get("ts") or 0,
                    "label": it.get("label") or "", "manual": bool(it.get("manual")),
                    "msg_count": it.get("msg_count") or 0})
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


def _clip(text, n=24):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n] + "…"


def make_label(s):
    """自动摘要：GAL 取最后一段对白/旁白；否则取最后一条消息文本"""
    msgs = s.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        for seg in reversed((m.get("gal") or {}).get("dialog") or []):
            if not isinstance(seg, dict):
                continue
            txt = seg.get("speech") or seg.get("narration") or ""
            if txt:
                who = str(seg.get("speaker") or "").strip()
                return _clip((who + "：" if who else "") + txt)
        body = str(m.get("content") or "").strip()
        if body:
            return _clip(body)
    for m in reversed(msgs):
        if m.get("role") == "user" and str(m.get("content") or "").strip():
            return "你：" + _clip(m["content"])
    return "开局"


def snapshot_state(s):
    """深拷贝可恢复字段"""
    return {k: clone_containers(s[k]) for k in RESTORE_FIELDS if k in s}


def _trim(items):
    """超出上限时：保留最早的一个（开局锚点）+ 最近的 N 个。返回被丢掉的条目。"""
    if len(items) <= SAVE_MAX:
        return []
    head = items[:1]
    tail = items[-(SAVE_MAX - 1):]
    dropped = items[1:len(items) - (SAVE_MAX - 1)]
    items[:] = head + tail
    return dropped


def add_save(sid, s, label=None, manual=False):
    """生成一个存档点并落盘，返回该存档点的元信息"""
    sid = sid or s.get("id")
    with _SAVE_LOCK:
        items = _items(sid)
        save_id = "sv_" + uuid.uuid4().hex[:10]
        ts = time.time()
        meta = {
            "id": save_id,
            "ts": ts,
            "label": str(label or "").strip() or make_label(s),
            "manual": bool(manual),
            "msg_count": len(s.get("messages") or []),
        }
        try:
            os.makedirs(_dir(sid), exist_ok=True)
        except OSError:
            pass
        # 先写快照再写 index：index 里不能出现指向不存在文件的条目
        # remember=False：存档是「写一次、只在手动回退时读」的大文件，
        # 进读缓存等于每轮自动留档都泄漏几十 MB（长会话跑几十轮就是 GB 级）。
        write_json(_state_path(sid, save_id),
                   {"id": save_id, "ts": ts, "state": snapshot_state(s)},
                   indent=None, remember=False)
        items.append(meta)
        for old in _trim(items):
            _remove_state(sid, old.get("id"))
        _write_index(sid, items)
    return meta


def auto_save(sid, label=None):
    """每轮自动留档：自己 load 会话，确保拿到最新状态"""
    data = load_sessions()
    s = find_session(data, sid)
    if not s:
        return None
    ensure_session_kb_fields(s)
    return add_save(sid, s, label=label, manual=False)


def _find(sid, save_id):
    for it in _items(sid):
        if it.get("id") == save_id:
            return it
    return None


def _load_state(sid, save_id):
    p = _state_path(sid, save_id)
    obj = read_json(p, None)
    drop_cache(p)                  # 存档只在还原时读一次，没必要常驻内存
    if isinstance(obj, dict) and isinstance(obj.get("state"), dict):
        return obj["state"]
    return None


def delete_save(sid, save_id):
    with _SAVE_LOCK:
        items = [it for it in _items(sid) if it.get("id") != save_id]
        _write_index(sid, items)
        _remove_state(sid, save_id)
    return True


def restore(data, s, save_id):
    """回退（覆盖当前会话）：把存档点状态写回 s。返回 (ok, err)

    注意：快照只包含「当时真实存在的」字段（snapshot_state 按 key 存在与否拷贝）。
    快照里**没有**的可恢复字段，说明当时还没有它 —— 必须先删掉，
    否则会出现「回退之后还留着未来才产生的字段」（例如 gal_recap 里写着还没发生的提要）。
    删完之后 ensure_session_kb_fields() 会把必需字段补回默认值，所以有些字段会「重置」而非消失。
    """
    item = _find(s.get("id"), save_id)
    if not item:
        return False, "存档不存在"
    state = _load_state(s.get("id"), save_id)
    if state is None:
        return False, "存档内容缺失"
    for k in RESTORE_FIELDS:
        if k in state:
            s[k] = copy.deepcopy(state[k])
        else:
            s.pop(k, None)
    s["updated_at"] = time.time()
    ensure_session_kb_fields(s)
    return True, ""


def branch_at(data, s, upto):
    """按消息位置分叉：以当前会话的前 upto 条消息为内容复制出新会话。原会话不动。

    与 branch() 的区别：不依赖存档点。存档点是「每轮结束」留的，重新发言 / 编辑 /
    导入的历史都可能没有对应档，因此「从这条回复处分叉」必须直接按消息切片。
    """
    msgs = s.get("messages") or []
    try:
        upto = int(upto)
    except (TypeError, ValueError):
        return None, "消息位置无效"
    upto = max(0, min(upto, len(msgs)))
    if upto <= 0:
        return None, "消息位置无效"
    base = str(s.get("title") or "对话") + " · 分支"
    exist = {str(x.get("title") or "") for x in (data.get("sessions") or [])}
    n = 1
    while ("%s%d" % (base, n)) in exist:
        n += 1
    ns = {}
    for k in RESTORE_FIELDS:
        if k in s:
            ns[k] = copy.deepcopy(s[k])
    ns["messages"] = copy.deepcopy(msgs[:upto])
    cut = int(ns.get("summary_upto") or 0)
    if cut > upto:                 # 摘要覆盖了被切掉的消息 → 清空，避免摘要指向不存在的历史
        ns["summary"] = ""
        ns["summary_upto"] = 0
    else:
        ns["summary_upto"] = cut
    ns["id"] = new_id()
    ns["title"] = "%s%d" % (base, n)
    ns["updated_at"] = time.time()
    ensure_session_kb_fields(ns)
    data["sessions"].insert(0, ns)
    data["current"] = ns["id"]
    add_save(ns["id"], ns, label="分支起点", manual=True)
    return ns["id"], ""


def branch(data, s, save_id):
    """分叉：以存档点为起点复制出一条新会话并切过去，原会话不动。返回 (new_sid, err)"""
    item = _find(s.get("id"), save_id)
    if not item:
        return None, "存档不存在"
    state = _load_state(s.get("id"), save_id)
    if state is None:
        return None, "存档内容缺失"
    base = str(state.get("title") or s.get("title") or "对话") + " · 分支"
    exist = {str(x.get("title") or "") for x in (data.get("sessions") or [])}
    n = 1
    while ("%s%d" % (base, n)) in exist:
        n += 1
    ns = {"messages": []}
    for k in RESTORE_FIELDS:
        if k in state:
            ns[k] = copy.deepcopy(state[k])
    # 注意：title 也在 RESTORE_FIELDS 里，必须在拷贝之后再覆盖成本分支的标题
    ns["id"] = new_id()
    ns["title"] = "%s%d" % (base, n)
    ns["updated_at"] = time.time()
    ensure_session_kb_fields(ns)
    data["sessions"].insert(0, ns)
    data["current"] = ns["id"]
    add_save(ns["id"], ns, label="分支起点", manual=True)   # 新线也有一个起点档
    return ns["id"], ""
