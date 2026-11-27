# -*- coding: utf-8 -*-
"""append-only 会话事件日志（会话内容的唯一事实源）。

每个会话一个 `data/sessions/<会话 id>/events.jsonl`：只追加、不滚动、不覆盖，
永久保留。`data/session_store/<会话 id>.json` 只是从它物化出来的读缓存，
可以随时删除并重建。

为什么用事件流：整体快照写到一半崩溃时只能看到半截；事件流是只追加的，
最多丢最后一条未闭合事件，前面的内容完好。用户显式删除会话时会删掉整个目录。

事件格式（每行一个 JSON）：

    {"ts": 1792..., "type": "session/baseline", "session": {...}}
    {"ts": 1792..., "type": "session/created", "session": {...}}
    {"ts": ..., "type": "session/meta", "fields": {"title": "..."}}
    {"ts": ..., "type": "message/append", "index": 0, "message": {...}}
    {"ts": ..., "type": "message/append_content", "index": 3, "text": "新增的正文"}
    {"ts": ..., "type": "message/append_tools", "index": 3, "tools": [...]}
    {"ts": ..., "type": "message/append_blocks", "index": 3, "blocks": [...]}
    {"ts": ..., "type": "message/append_block_text", "index": 3, "bi": 0, "text": "增量"}
    {"ts": ..., "type": "message/patch", "index": 3, "fields": {...}}
    {"ts": ..., "type": "message/replace", "index": 3, "message": {...}}
    {"ts": ..., "type": "message/truncate", "keep": 5}
"""
import collections
import json
import os
import re
import threading
import time

from app.store import DATA_DIR, clone_containers

# 每会话一把写锁：不同会话可以并行追加；同一会话必须串行（事件靠 diff）。
_WRITE_LOCKS = {}
_WRITE_LOCKS_GUARD = threading.Lock()
# 单会话事件文件超过这个体积就滚动成 .1.jsonl（重建时会按 .1 → 当前 的顺序读）。
MAX_BYTES = 64 * 1024 * 1024


def _write_lock(sid):
    key = str(sid or "")
    with _WRITE_LOCKS_GUARD:
        lk = _WRITE_LOCKS.get(key)
        if lk is None:
            lk = _WRITE_LOCKS[key] = threading.Lock()
        return lk


def _rotate(path):
    """超过阈值就把当前文件滚到 .1.jsonl；旧 .1 直接覆盖（只保留两份）。"""
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    bak = path[:-len(".jsonl")] + ".1.jsonl"
    try:
        os.replace(path, bak)
    except OSError as e:
        print("[事件] 滚动失败（继续追加原文件）：%s" % e)

# 每个会话「上一次落库时的快照」。读取时会登记物化缓存里的状态，
# 这样进程启动后的第一次落库也能算出正确增量。
# 用 OrderedDict + 上限：会话多时不能把每一份完整会话都常驻内存。
_LAST = collections.OrderedDict()
_LAST_LOCK = threading.Lock()
LAST_CAP = 64
# 事件序号：新事件带 seq，重建时可校验单调递增；旧事件没有 seq，按 0 处理。
_SEQ = {}
_SEQ_LOCK = threading.Lock()
_ISSUES = {}          # sid -> 最近一次重建发现的序号问题
_WARNED = set()       # 已经打印过问题的会话，避免每次重建都刷日志
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def drop_last(sid):
    """删除会话时清掉进程内缓存，避免 _LAST 无限增长。"""
    s = str(sid or "").strip()
    if not s:
        return
    with _LAST_LOCK:
        _LAST.pop(s, None)
    with _SEQ_LOCK:
        _SEQ.pop(s, None)
    _ISSUES.pop(s, None)
    _WARNED.discard(s)


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


def _blocks_events(i, old_blocks, new_blocks):
    """blocks 的增量事件；表达不了时返回 None（调用方整条替换）。

    取消思考上限后，一次进度落库再整段重写 blocks 会让日志迅速膨胀：
    - 流式思考只增长最后一块 → 只记文本增量；
    - 每轮新增工具 / 文本块 → 只记新增的块。
    """
    ob = list(old_blocks or [])
    nb = list(new_blocks or [])
    n = min(len(ob), len(nb))
    if all(ob[k] == nb[k] for k in range(n)):
        if len(nb) > len(ob):
            return [("message/append_blocks", {"index": i, "blocks": nb[len(ob):]})]
        return [] if len(nb) == len(ob) else None
    if len(ob) == len(nb) and n >= 1 and all(ob[k] == nb[k] for k in range(n - 1)):
        a, b = ob[-1], nb[-1]
        at = str(a.get("text") or "") if isinstance(a, dict) else ""
        bt = str(b.get("text") or "") if isinstance(b, dict) else ""
        if (isinstance(a, dict) and isinstance(b, dict) and set(a) == set(b)
                and a.get("kind") == b.get("kind")
                and bt.startswith(at) and len(bt) > len(at)):
            return [("message/append_block_text",
                     {"index": i, "bi": n - 1, "text": bt[len(at):]})]
    return None


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
        if k == "blocks":
            continue                      # blocks 单独做增量
        if old.get(k) != new.get(k):
            fields[k] = new.get(k)
    if fields:
        out.append(("message/patch", {"index": i, "fields": fields}))
    if old.get("blocks") != new.get("blocks"):
        evs = _blocks_events(i, old.get("blocks"), new.get("blocks"))
        if evs is None:
            # 增量表达不了（编辑 / 撤回 / 重排）：整条替换，前面的增量被覆盖
            out.append(("message/replace", {"index": i, "message": new}))
        else:
            out.extend(evs)
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
    blocks = last.get("blocks") or []
    # 思考 / 工具块的增长也要进签名：否则进度落库时只有 blocks 变化会被整条跳过
    return (s.get("title"), s.get("summary_upto"), len(str(s.get("summary") or "")),
            s.get("updated_at"), len(msgs),
            len(str(last.get("content") or "")), len(last.get("tools") or []),
            len(blocks), len(str(blocks[-1].get("text") or "")) if blocks else 0)


def _set_last(sid, snap):
    """登记会话快照，并按最近使用淘汰，避免常驻内存随会话数无限增长。"""
    with _LAST_LOCK:
        _LAST[sid] = snap
        _LAST.move_to_end(sid)
        while len(_LAST) > LAST_CAP:
            _LAST.popitem(last=False)


def seed_last(s):
    """登记当前内存快照：进程启动加载后调用，第一次落库才能算出正确增量。"""
    if not isinstance(s, dict) or not s.get("id"):
        return
    _set_last(s.get("id"), clone_containers(s))


def append_baseline(s, data_dir=None):
    """无条件追加一条完整基线（迁移 / 缓存丢失后对账用）；返回事件 ts。

    与 `record` 的区别：不做增量 diff，整份会话作为 `session/baseline` 写入，
    重建时从这一条开始覆盖之前的内容。
    """
    if not isinstance(s, dict) or not s.get("id"):
        return 0
    sid = s.get("id")
    snap = clone_containers(s)
    _set_last(sid, clone_containers(snap))
    path = path_of(sid, data_dir)
    ts = time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"ts": ts, "seq": _next_seq(sid, path),
                           "type": "session/baseline", "session": snap},
                          ensure_ascii=False) + "\n"
        with _write_lock(sid):
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return ts
    except OSError as e:
        print("[事件] 基线写入失败：%s" % e)
        return 0


def record(new, data_dir=None):
    """把一次落库的变化追加到事件日志；返回最后一条事件的 ts（没写返回 0）。

    首次见到某个会话时只建立基线：如果事件文件已存在，说明启动加载时已经
    登记过快照（见 `seed_last`），直接返回 —— 这样进程重启后不会把整份历史
    重复记一遍。
    """
    try:
        if not isinstance(new, dict) or not new.get("id"):
            return 0
        sid = new.get("id")
        with _LAST_LOCK:
            old = _LAST.get(sid)
        if old is not None and _sig(old) == _sig(new):
            return 0                     # 没变化：不深拷贝、不写事件
        snap = clone_containers(new)
        _set_last(sid, snap)
        if old is None:
            path = path_of(sid, data_dir)
            if os.path.exists(path) or os.path.exists(path[:-len(".jsonl")] + ".1.jsonl"):
                return 0
            return append_baseline(snap, data_dir)
        evs = diff(old, new)
        if not evs:
            return 0
        path = path_of(sid, data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = time.time()
        # 先序列化（可能几 MB），再进会话写锁；锁内只做 rotate + append。
        lines = []
        for typ, data in evs:
            item = {"ts": now, "seq": _next_seq(sid, path), "type": typ}
            item.update(data)
            lines.append(json.dumps(item, ensure_ascii=False) + "\n")
        payload = "".join(lines)
        with _write_lock(sid):
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(payload)
        return now
    except Exception as e:
        print("[事件] 写入失败（不影响主流程）：%s" % e)
        return 0


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
    elif typ == "message/append_blocks":
        idx = int(evt.get("index") or 0)
        if 0 <= idx < len(messages):
            messages[idx]["blocks"] = (messages[idx].get("blocks") or []) + (evt.get("blocks") or [])
    elif typ == "message/append_block_text":
        idx = int(evt.get("index") or 0)
        bi = int(evt.get("bi") or 0)
        if 0 <= idx < len(messages):
            blocks = messages[idx].get("blocks") or []
            if 0 <= bi < len(blocks) and isinstance(blocks[bi], dict):
                blocks[bi]["text"] = str(blocks[bi].get("text") or "") + (evt.get("text") or "")
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

    用于判断事件流是否比物化缓存新。
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
