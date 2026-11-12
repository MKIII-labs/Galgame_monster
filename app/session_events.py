# -*- coding: utf-8 -*-
"""append-only 会话事件日志（旁路）。

`sessions.json` 仍是唯一的读写入口；这里只是把「每次落库带来的变化」追加成事件，
用于崩溃恢复、审计，以及后续的 resume / fork。它不参与任何读取路径，
写失败也只打印一行日志，绝不影响主流程。

为什么要有它：sessions.json 是整体快照，进程写到一半崩溃时只能看到半截；
事件流是只追加的，最多丢最后一条未闭合事件，前面的内容完好。

事件格式（每行一个 JSON）：

    {"ts": 1792..., "type": "session/created", "session": {...}}
    {"ts": ..., "type": "session/meta", "fields": {"title": "..."}}
    {"ts": ..., "type": "message/append", "index": 0, "message": {...}}
    {"ts": ..., "type": "message/append_content", "index": 3, "text": "新增的正文"}
    {"ts": ..., "type": "message/append_tools", "index": 3, "tools": [...]}
    {"ts": ..., "type": "message/patch", "index": 3, "fields": {...}}
    {"ts": ..., "type": "message/replace", "index": 3, "message": {...}}
    {"ts": ..., "type": "message/truncate", "keep": 5}

落盘位置：data/sessions/<会话 id>/events.jsonl。单文件超过上限时滚动到
events.1.jsonl（只保留一份历史，更老的会被覆盖）。
"""
import json
import os
import re
import threading
import time

from app.store import DATA_DIR, clone_containers

_LOCK = threading.Lock()
# 每个会话「上一次落库时的快照」。sessions.json 的读缓存会返回同一个对象，
# 调用方是就地修改的，磁盘旧版本在内存里已经没了 —— 只能靠这份快照做 diff。
_LAST = {}
_LAST_LOCK = threading.Lock()
# 事件序号：新事件带 seq，重建时可校验单调递增；旧事件没有 seq，按 0 处理。
_SEQ = {}
_SEQ_LOCK = threading.Lock()
_ISSUES = {}          # sid -> 最近一次重建发现的序号问题
_WARNED = set()       # 已经打印过问题的会话，避免每次重建都刷日志
MAX_BYTES = 32 * 1024 * 1024      # 单文件上限：超过后滚动，避免长任务无限增长
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _next_seq(sid, path=None):
    """全局单调的事件序号。

    用纳秒时间戳而不是进程内计数器：app 与测试服务、或两个残留进程同时写同一
    会话时，计数器会各自从 0 开始，产生重复/倒退的 seq。时间戳在跨进程时仍然
    单调；万一同纳秒重复，再 +1。
    """
    n = time.time_ns()
    with _SEQ_LOCK:
        last = _SEQ.get(sid, 0)
        if n <= last:
            n = last + 1
        _SEQ[sid] = n
        return n

# 消息级字段：变化时记一条 message/patch（content / tools 单独做增量）
_MSG_FIELDS = ("blocks", "timing", "usage", "end_reason", "interrupted",
               "plan", "sources", "memory", "ctx_cut", "_progress")


def _safe_sid(sid):
    s = _SID_RE.sub("_", str(sid or "").strip())
    return s[:80]


def path_of(sid, data_dir=None):
    base = os.path.join(data_dir or DATA_DIR, "sessions", _safe_sid(sid))
    return os.path.join(base, "events.jsonl")


def _rotate(path):
    """事件文件超过上限时滚动到 events.1.jsonl（旧的一份被覆盖）。"""
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    bak = path[:-len(".jsonl")] + ".1.jsonl"
    try:
        os.replace(path, bak)
    except OSError:
        pass


def _msg_events(i, old, new):
    """一条消息从 old 变成 new 需要哪些事件（尽量小，避免长任务把日志撑爆）。"""
    out = []
    oc = str(old.get("content") or "")
    nc = str(new.get("content") or "")
    if nc.startswith(oc) and len(nc) > len(oc):
        # 流式追加：只记增量
        out.append(("message/append_content", {"index": i, "text": nc[len(oc):]}))
    elif oc != nc:
        # 正文被改写（撤回 / 编辑 / 续写重排）：整条替换，其余字段一起带上
        out.append(("message/replace", {"index": i, "message": new}))
        return out

    ot = old.get("tools") or []
    nt = new.get("tools") or []
    if len(nt) > len(ot) and nt[:len(ot)] == ot:
        out.append(("message/append_tools", {"index": i, "tools": nt[len(ot):]}))
    elif nt != ot:
        out.append(("message/patch", {"index": i, "fields": {"tools": nt}}))

    fields = {}
    for k in _MSG_FIELDS:
        if old.get(k) != new.get(k):
            fields[k] = new.get(k)
    if fields:
        out.append(("message/patch", {"index": i, "fields": fields}))
    return out


def diff(old, new):
    """算出把 old 会话变成 new 需要的事件列表。old 为 None 表示新建。"""
    if not isinstance(new, dict) or not new.get("id"):
        return []
    evs = []
    old = old if isinstance(old, dict) else None
    if not old:
        evs.append(("session/created", {"session": {
            "id": new.get("id"), "title": new.get("title"),
            "created_at": new.get("created_at"), "updated_at": new.get("updated_at"),
        }}))
        old_msgs = []
    else:
        # 记录所有顶层字段的变化（不只白名单）：事件流要能独立重建会话，
        # memory / gal_auto_story 这类字段漏记会导致重启后丢内容。
        fields = {}
        for k in set(old) | set(new):
            if k == "messages":
                continue
            if old.get(k) != new.get(k):
                fields[k] = new.get(k)      # 字段被删除时记 None
        if fields:
            evs.append(("session/meta", {"fields": fields}))
        old_msgs = old.get("messages") or []

    new_msgs = new.get("messages") or []
    if len(new_msgs) < len(old_msgs):
        evs.append(("message/truncate", {"keep": len(new_msgs)}))
    for i, m in enumerate(new_msgs):
        if i >= len(old_msgs):
            evs.append(("message/append", {"index": i, "message": m}))
        elif old_msgs[i] != m:
            evs.extend(_msg_events(i, old_msgs[i], m))
    return evs


def _sig(s):
    """廉价变化指纹：签名相同就跳过深拷贝与 diff。"""
    msgs = s.get("messages") or []
    last = msgs[-1] if msgs else {}
    return (s.get("title"), s.get("summary_upto"), len(str(s.get("summary") or "")),
            s.get("updated_at"), len(msgs),
            len(str(last.get("content") or "")), len(last.get("tools") or []))


def record(new, data_dir=None):
    """把一次落库的变化追加到事件日志。失败只打印，不抛给调用方。

    首次见到某个会话时只建立基线：如果事件文件已存在，先从事件流恢复基线；
    否则直接返回 —— 这样进程重启后不会把整份历史重复记一遍。
    代价是重启后第一次落库的那点变化不会进日志（可接受）。
    """
    try:
        if not isinstance(new, dict) or not new.get("id"):
            return
        sid = new.get("id")
        with _LAST_LOCK:
            old = _LAST.get(sid)
        if old is not None and _sig(old) == _sig(new):
            return                       # 没变化：不深拷贝、不写事件
        snap = clone_containers(new)
        with _LAST_LOCK:
            _LAST[sid] = snap
        if old is None:
            # 本进程第一次见到这个会话：sessions.json / 单会话快照在启动时已经
            # 对账过，当前内存状态就是最新状态，直接拿它当基线即可。
            # 以前这里会 rebuild（重放整份事件日志，最大的会话 50MB+ 要几百毫秒），
            # 现在只在「事件文件还不存在」时补一条完整基线。
            path = path_of(sid, data_dir)
            if os.path.exists(path) or os.path.exists(path[:-len(".jsonl")] + ".1.jsonl"):
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _rotate(path)
            with _LOCK:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": time.time(),
                                        "seq": _next_seq(sid, path),
                                        "type": "session/baseline",
                                        "session": snap}, ensure_ascii=False) + "\n")
            return
        evs = diff(old, new)
        if not evs:
            return
        path = path_of(sid, data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate(path)
        now = time.time()
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                for typ, data in evs:
                    item = {"ts": now, "seq": _next_seq(sid, path), "type": typ}
                    item.update(data)
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[事件] 旁路写入失败（不影响主流程）：%s" % e)


def record_all(rows, data_dir=None):
    """全量保存后调用：逐个会话记录变化（没变化的会被签名快速跳过）。"""
    for s in rows or []:
        record(s, data_dir)


def _tail_ts(path):
    """读事件文件最后一条的 ts（只读尾部 64KB，不解析整份文件）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0.0
    if size <= 0:
        return 0.0
    try:
        with open(path, "rb") as f:
            back = min(size, 65536)
            f.seek(size - back)
            chunk = f.read()
    except OSError:
        return 0.0
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
            return float(obj.get("ts") or 0)
        except (ValueError, TypeError, UnicodeDecodeError):
            continue           # 崩溃留下的半行：继续往前找
    return 0.0


def latest_ts(sid, data_dir=None):
    """某会话事件流最后一条的时间戳；没有事件文件时返回 0。

    用于启动对账：只有「事件比快照新」的会话才值得重放事件，
    不然每次启动都会把所有会话的 100MB+ 事件日志重建一遍。
    """
    path = path_of(sid, data_dir)
    rotated = path[:-len(".jsonl")] + ".1.jsonl"
    for p in (path, rotated):          # 先看最新的那份；滚动后新文件可能还不存在
        ts = _tail_ts(p)
        if ts:
            return ts
    return 0.0


def _apply(evt, s, messages):
    typ = evt.get("type")
    if typ == "session/baseline":
        snap = evt.get("session") or {}
        s.clear()
        s.update({k: v for k, v in snap.items() if k != "messages"})
        messages.clear()
        messages.extend(clone_containers(snap.get("messages") or []))
    elif typ == "session/created":
        s.update(evt.get("session") or {})
    elif typ == "session/meta":
        s.update(evt.get("fields") or {})
    elif typ == "message/append":
        idx = int(evt.get("index") or 0)
        while len(messages) <= idx:
            messages.append({})
        messages[idx] = evt.get("message") or {}
    elif typ == "message/append_content":
        idx = int(evt.get("index") or 0)
        if 0 <= idx < len(messages):
            messages[idx]["content"] = (messages[idx].get("content") or "") + (evt.get("text") or "")
    elif typ == "message/append_tools":
        idx = int(evt.get("index") or 0)
        if 0 <= idx < len(messages):
            messages[idx]["tools"] = (messages[idx].get("tools") or []) + (evt.get("tools") or [])
    elif typ == "message/patch":
        idx = int(evt.get("index") or 0)
        if 0 <= idx < len(messages):
            messages[idx].update(evt.get("fields") or {})
    elif typ == "message/replace":
        idx = int(evt.get("index") or 0)
        if 0 <= idx < len(messages):
            messages[idx] = evt.get("message") or {}
    elif typ == "message/truncate":
        keep = max(0, int(evt.get("keep") or 0))
        del messages[keep:]


def _check_events(events, sid):
    """检查 seq 单调；旧事件没有 seq 时跳过，不把老会话判坏。"""
    issues, last = [], 0
    for evt in events:
        try:
            seq = int(evt.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq:
            # 旧版本用 1..N 的进程内计数器，跨进程没有可比性；只校验新的时间戳序号。
            if seq > 10 ** 12:
                if seq <= last:
                    issues.append("seq 非单调：%d <= %d" % (seq, last))
                last = seq
    _ISSUES[sid] = issues
    if issues and sid not in _WARNED:
        _WARNED.add(sid)
        print("[事件] 会话 %s 序号异常（继续按宽松模式读取）：%s"
              % (sid, "；".join(issues[:3])))
    return issues


def validate(sid, data_dir=None):
    """返回某会话的事件序号问题；没有问题时返回空列表。"""
    if sid not in _ISSUES:
        rebuild(sid, data_dir)
    return list(_ISSUES.get(sid) or [])


def rebuild(sid, data_dir=None):
    """从事件流重建会话快照（测试 / 未来的 resume 用）。没有事件时返回 None。"""
    path = path_of(sid, data_dir)
    old = path[:-len(".jsonl")] + ".1.jsonl"
    files = [p for p in (old, path) if os.path.exists(p)]
    if not files:
        return None
    s, messages = {}, []
    events = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue          # 崩溃留下的半行：跳过
                    if isinstance(evt, dict):
                        events.append(evt)
                    _apply(evt, s, messages)
        except OSError:
            continue
    _check_events(events, sid)
    s["messages"] = messages
    return s


def load_all(data_dir=None):
    """扫描 data/sessions/*/events.jsonl，返回 {会话 id: 重建结果}。

    只用于启动时的对账：事件流比 sessions.json 新时以事件流为准。
    单个会话损坏不影响其它会话（跳过并打印）。
    """
    base = os.path.join(data_dir or DATA_DIR, "sessions")
    out = {}
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        p = os.path.join(base, name, "events.jsonl")
        if not os.path.isfile(p):
            continue
        try:
            s = rebuild(name, data_dir)
        except Exception as e:
            print("[事件] 会话 %s 重建失败，跳过：%s" % (name, e))
            continue
        if s and s.get("id"):
            out[s["id"]] = s
    return out
