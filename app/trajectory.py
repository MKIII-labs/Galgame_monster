# -*- coding: utf-8 -*-
"""轨迹（Trajectory）：模型视角的追加式事件账本 + 只读投影。

对齐 DeepSeek Harness 的 Trajectory 核心：
- 事件：turn/step、request/header（系统提示 + 工具 schema）、user/message（含注入上下文）、
  assistant/message（正文 / 思考 / usage / timing）、tool/call、tool/result、
  todo/write、subagent、compact。
- 投影：turn -> group（输入 / 第 N 步）-> cell；cell 带 input/output/thinking/schema/
  tokens/duration/callId/traceId/isError。
- 系统提示对齐 DSH 的「快照 + 差异 + 分段」：request/header 记下 reason
  （initial / resume / change）、每段 hash / 偏移 / 预览；投影时把每个分段都
  单独成行（技能索引 / 系统时间 / 规则 / 工具说明…），并给出与上一版的 diff。
- 完整工具结果仍以 app.tool_trace 为准；这里只存引用与截断副本，避免重复占盘。

设计约束：
- 旁路写入：任何异常都吞掉，不影响聊天主流程。
- 与 sessions.json 解耦：旧会话没有轨迹日志时，投影回退到 session messages。
- 不参与模型请求：轨迹只读，不回灌上下文。
"""
import difflib
import hashlib
import html
import json
import os
import re
import threading
import time

MAX_BYTES = 64 * 1024 * 1024       # 单会话超过 64MB 滚动一份 .1.jsonl
DEFAULT_FIELD_CAP = 200000         # 单个字段默认保留字符数（0 = 不限）
MAX_SUBAGENTS = 20                 # 一次展开多少个子代理
MAX_SUB_TRANSCRIPT = 20000         # 子代理转录上限
MAX_PARTS = 40                     # 一次系统提示最多切多少段（超出合并成「其余 N 段」）
PART_PREVIEW = 240                 # 分段预览字符数（分段全文由投影按偏移切片）
DIFF_MAX = 20000                   # 系统提示 diff 的字符上限

# 分段标题：markdown # ~ #### 标题，或行首中文方括号标题【…】
_HEAD_RE = re.compile(r"^(?:#{1,4}\s+\S|【[^】\n]{1,60}】)")

_LOCK = threading.Lock()
_TURN_LOCK = threading.Lock()
_TURN_LAST = {}
_HEADER_LAST = {}
_CFG_CACHE = {"at": 0.0, "enabled": True, "cap": DEFAULT_FIELD_CAP}
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
_BATCH_RE = re.compile(r"ex_[A-Za-z0-9]{4,}")
_SUBAGENT_TOOLS = {"explore", "delegate"}


# ---------------------------------------------------------------- 配置 / 路径

def _cfg():
    now = time.monotonic()
    if now - _CFG_CACHE["at"] < 5.0:
        return _CFG_CACHE
    try:
        from app.config import load_config
        c = load_config() or {}
        _CFG_CACHE["enabled"] = bool(c.get("trajectory_enabled", True))
        try:
            _CFG_CACHE["cap"] = int(c.get("trajectory_max_field",
                                          DEFAULT_FIELD_CAP) or 0)
        except (TypeError, ValueError):
            _CFG_CACHE["cap"] = DEFAULT_FIELD_CAP
    except Exception:                    # noqa: BLE001  读配置失败按开启处理
        pass
    _CFG_CACHE["at"] = now
    return _CFG_CACHE


def _enabled():
    return bool(_cfg().get("enabled", True))


def _safe_sid(sid):
    s = _SID_RE.sub("_", str(sid or "").strip())
    return s[:80] or "unknown"


def _dir():
    from app.store import DATA_DIR
    d = os.path.join(DATA_DIR, "trajectory")
    os.makedirs(d, exist_ok=True)
    return d


def _path(sid):
    return os.path.join(_dir(), _safe_sid(sid) + ".jsonl")


def _old_path(sid):
    return _path(sid) + ".1"


def _clip(text, cap=None):
    t = str(text or "")
    if cap is None:
        cap = int(_cfg().get("cap") or 0)
    if cap and len(t) > cap:
        return t[:cap] + "\n…（轨迹已截断，共 %d 字）" % len(t)
    return t


def _rotate(path):
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


def _append(sid, typ, data=None):
    """追加一条事件；失败静默。返回 seq 或 0。"""
    if not _enabled():
        return 0
    try:
        path = _path(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate(path)
        seq = time.time_ns()
        item = {"v": 1, "seq": seq, "ts": int(time.time() * 1000),
                "type": str(typ), "data": data or {}}
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return seq
    except Exception as e:               # noqa: BLE001
        print("[轨迹] 写入失败（不影响主流程）：%s" % e)
        return 0


# ---------------------------------------------------------------- 事件缓存
# 45MB 的会话有 2.4 万条事件；「跟随最新」每 2.5s 全量重读会把 CPU 打满。
# 这里按 (size, mtime) 判重，只把新增字节追加进来；旧文件被滚动时整体重建。
_EV_CACHE = {}
_EV_MAX = 2
_EV_LOCK = threading.Lock()


def _stat_rev(paths):
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = [int(st.st_size), int(st.st_mtime_ns)]
        except OSError:
            out[p] = [0, 0]
    return out


def _parse_events(blob):
    out = []
    for line in blob.split(b"\n"):
        s = line.strip()
        if not s:
            continue
        try:
            item = json.loads(s.decode("utf-8", "ignore"))
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type"):
            out.append(item)
    return out


def _load_events_full(sid):
    old, cur = _old_path(sid), _path(sid)
    events = []
    pend = {}
    for p in (old, cur):
        try:
            with open(p, "rb") as f:
                blob = f.read()
        except OSError:
            pend[p] = b""
            continue
        # 末尾没有换行：可能正在写入，留给下一次增量
        parts = blob.split(b"\n")
        tail = parts.pop()
        events.extend(_parse_events(b"\n".join(parts)))
        pend[p] = tail
    events.sort(key=lambda x: int(x.get("seq") or 0))
    rev = _stat_rev([old, cur])
    return {
        "rev": rev, "events": events,
        "byseq": {int(e.get("seq") or 0): e for e in events},
        "pending": pend,
        "offset": {cur: rev[cur][0] - len(pend.get(cur) or b"")},
        "used": time.monotonic(),
    }


def _append_events(c, cur):
    """从上次偏移读当前文件的新增部分（旧文件未滚动时才安全）。"""
    off = int(c["offset"].get(cur) or 0)
    try:
        with open(cur, "rb") as f:
            f.seek(off)
            blob = f.read()
    except OSError:
        return
    if not blob:
        return
    blob = (c["pending"].get(cur) or b"") + blob
    parts = blob.split(b"\n")
    tail = parts.pop()
    consumed = len(blob) - len(tail)
    new = _parse_events(b"\n".join(parts))
    for ev in new:
        c["events"].append(ev)
        c["byseq"][int(ev.get("seq") or 0)] = ev
    c["offset"][cur] = off + consumed
    c["pending"][cur] = tail


def _iter_events(sid):
    """按时间顺序读事件（旧滚动文件在前）。带增量缓存，返回的列表只读。"""
    old, cur = _old_path(sid), _path(sid)
    rev = _stat_rev([old, cur])
    with _EV_LOCK:
        c = _EV_CACHE.get(sid)
        if c and c["rev"] == rev:
            c["used"] = time.monotonic()
            return c["events"]
        if (c and c["rev"].get(old) == rev[old]
                and rev[cur][0] >= int(c["offset"].get(cur) or 0)):
            _append_events(c, cur)
            c["rev"] = rev
            c["used"] = time.monotonic()
            return c["events"]
        c = _load_events_full(sid)
        _EV_CACHE[sid] = c
        if len(_EV_CACHE) > _EV_MAX:
            oldest = min(_EV_CACHE.items(), key=lambda kv: kv[1]["used"])[0]
            if oldest != sid:
                _EV_CACHE.pop(oldest, None)
        return c["events"]


def _event_by_seq(sid, seq):
    try:
        s = int(seq or 0)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    _iter_events(sid)                 # 确保缓存已建立
    c = _EV_CACHE.get(sid) or {}
    return (c.get("byseq") or {}).get(s)


def _drop_cache(sid):
    with _EV_LOCK:
        _EV_CACHE.pop(sid, None)
        _PROJ_CACHE.pop(sid, None)


def drop(sid):
    """删除会话时一并删除轨迹日志，避免删完会话后磁盘上还留着完整对话副本。"""
    s = str(sid or "").strip()
    if not s:
        return
    _drop_cache(s)
    base = _path(s)
    for p in (base, _old_path(s), base[:-len(".jsonl")] + ".1.jsonl"):
        try:
            os.remove(p)
        except OSError:
            pass


def _scan_last_turn(sid):
    last = 0
    for ev in _iter_events(sid):
        if ev.get("type") == "turn/start":
            try:
                last = max(last, int((ev.get("data") or {}).get("turn") or 0))
            except (TypeError, ValueError):
                pass
    return last


# ---------------------------------------------------------------- 写入接口

def ensure_turn(sid):
    """返回当前轮次；没有轮次时建一个。"""
    with _TURN_LOCK:
        n = _TURN_LAST.get(sid)
        if n is None:
            n = _scan_last_turn(sid)
        if n <= 0:
            n = 1
            _TURN_LAST[sid] = n
            _append(sid, "turn/start", {"turn": n})
        return n


def new_turn(sid):
    """开一个新轮次，返回轮次号。"""
    with _TURN_LOCK:
        n = _TURN_LAST.get(sid)
        if n is None:
            n = _scan_last_turn(sid)
        n += 1
        _TURN_LAST[sid] = n
    _append(sid, "turn/start", {"turn": n})
    return n


def last_turn(sid):
    return ensure_turn(sid)


def turn_end(sid, turn, reason="completed"):
    _append(sid, "turn/end", {"turn": int(turn or 0), "reason": str(reason or "")})


def step_start(sid, turn, step):
    _append(sid, "step/start", {"turn": int(turn or 0), "step": int(step or 0)})


def step_end(sid, turn, step):
    _append(sid, "step/end", {"turn": int(turn or 0), "step": int(step or 0)})


def _one_line(text, n=PART_PREVIEW):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def sections(text):
    """把系统提示按标题切成 [(label, start, end)]。

    识别 markdown 标题（# ~ ####）与行首中文方括号标题【…】；没有标题时整段
    作为「提示正文」。偏移用于投影时从完整系统提示里切出分段全文，避免重复存。
    """
    text = str(text or "")
    if not text.strip():
        return []
    marks, pos = [], 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        if _HEAD_RE.match(raw):
            label = re.sub(r"^#+\s*", "", raw.strip()).strip("【】 ").strip()
            marks.append((label[:60] or "段落", pos))
        pos += len(line)
    if not marks:
        return [("提示正文", 0, len(text))]
    if marks[0][1] > 0:
        marks.insert(0, ("前言", 0))
    out = []
    for i, (label, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        if end > start:
            out.append((label, start, end))
    if len(out) > MAX_PARTS:
        keep = out[:MAX_PARTS - 1]
        rest = len(out) - len(keep)
        keep.append(("其余 %d 段" % rest, keep[-1][2], len(text)))
        out = keep
    return out


def _diff_text(old, new, max_chars=DIFF_MAX):
    """两版系统提示的 unified diff（按行）。"""
    try:
        left = str(old or "").splitlines()
        right = str(new or "").splitlines()
        lines = list(difflib.unified_diff(left, right, fromfile="上一版",
                                          tofile="当前", lineterm="", n=2))
    except Exception:                    # noqa: BLE001
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（差异已截断）"
    return text


def _load_last_header(sid):
    """进程重启后从日志里恢复上一个请求头（用于判断 initial / resume / change）。"""
    for ev in reversed(_iter_events(sid)):
        if ev.get("type") != "request/header":
            continue
        data = ev.get("data") or {}
        h = data.get("header") or {}
        parts = {}
        for p in data.get("parts") or []:
            if isinstance(p, dict) and p.get("id"):
                parts[str(p["id"])] = str(p.get("hash") or "")
        return {"key": (str(h.get("systemHash") or ""),
                        str(h.get("toolsHash") or ""),
                        str(h.get("model") or "")),
                "th": str(h.get("toolsHash") or ""),
                "sh": str(h.get("systemHash") or ""),
                "parts": parts, "from_log": True}
    return None


def header(sid, turn, step, model="", provider="", system="", tools=None,
           systems=None):
    """记录一次请求头；system / tools / model 没变就跳过。

    systems 给定时（多条 system 消息）按模型顺序拼成完整系统提示；
    再把系统提示按标题切成段，记下每段 hash / 偏移 / 预览，供投影做
    「分段」「差异」，并判断这次变化来自哪几段（如技能索引 / 工具）。
    """
    try:
        raw_tools = tools if isinstance(tools, list) else []
        if isinstance(systems, list) and systems:
            system = "\n\n".join(
                str(x.get("text") if isinstance(x, dict) else x or "")
                for x in systems)
        system = str(system or "")
        sh = hashlib.sha1(system.encode("utf-8", "ignore")).hexdigest()
        th = hashlib.sha1(json.dumps(raw_tools, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8", "ignore")).hexdigest()
        key = (sh, th, str(model or ""))
        prev = _HEADER_LAST.get(sid)
        if prev is None:
            prev = _load_last_header(sid)
            if prev:
                _HEADER_LAST[sid] = prev
        if prev and prev.get("key") == key:
            return
        if prev is None:
            reason = "initial"
        elif prev.get("from_log"):
            reason = "resume"            # 进程重启：日志里有旧请求头，内存里没有
        else:
            reason = "change"
        clipped = _clip(system)
        parts, seen = [], {}
        for label, start, end in sections(system):
            if start >= len(clipped):
                break
            end = min(end, len(clipped))
            seg = clipped[start:end]
            n = seen.get(label, 0)
            seen[label] = n + 1
            pid = label if n == 0 else "%s#%d" % (label, n + 1)
            h = hashlib.sha1(seg.encode("utf-8", "ignore")).hexdigest()
            old = ((prev or {}).get("parts") or {}).get(pid)
            parts.append({"id": pid, "label": label, "chars": len(seg),
                          "hash": h, "start": start, "end": end,
                          "preview": _one_line(seg),
                          "changed": bool(prev is None or old != h)})
        _append(sid, "request/header", {
            "turn": int(turn or 0), "step": int(step or 0),
            "reason": reason,
            "changedTools": bool(prev and prev.get("th") != th),
            "parts": parts,
            "header": {
                "model": str(model or ""), "provider": str(provider or ""),
                "system": clipped, "tools": raw_tools,
                "systemHash": sh, "toolsHash": th,
            },
        })
        _HEADER_LAST[sid] = {"key": key, "th": th, "sh": sh,
                             "parts": {p["id"]: p["hash"] for p in parts}}
    except Exception as e:               # noqa: BLE001
        print("[轨迹] 请求头记录失败：%s" % e)


def user(sid, turn, content, source=None):
    _append(sid, "user/message", {
        "turn": int(turn or 0),
        "content": _clip(content),
        "source": source if isinstance(source, dict) else {"kind": "human"},
    })


def context(sid, turn, kind, content, label=""):
    """模型可见的注入上下文（记忆 / 计划 / 引用 / 文档 / 图片 / hook…）。"""
    user(sid, turn, content,
         {"kind": str(kind or "context"), "label": str(label or kind or "")})


def assistant(sid, turn, step, content="", reasoning="", usage=None, timing=None):
    _append(sid, "assistant/message", {
        "turn": int(turn or 0), "step": int(step or 0),
        "content": _clip(content), "reasoning": _clip(reasoning),
        "usage": usage if isinstance(usage, dict) else {},
        "timing": timing if isinstance(timing, dict) else {},
    })


def tool(sid, turn, step, entry, full=None):
    """记录一次工具调用与结果；subagent 工具额外记录批次。"""
    if not isinstance(entry, dict):
        return
    name = str(entry.get("tool") or "")
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    call_id = str(entry.get("trace_id") or ("t_%s_%s" % (step, time.time_ns())))
    try:
        arguments = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:                    # noqa: BLE001
        arguments = str(args)
    _append(sid, "tool/call", {
        "turn": int(turn or 0), "step": int(step or 0),
        "callId": call_id, "name": name, "arguments": _clip(arguments),
    })
    text = full if full is not None else entry.get("result")
    text = str(text or "")
    ok = bool(entry.get("ok", True))
    _append(sid, "tool/result", {
        "turn": int(turn or 0), "step": int(step or 0),
        "callId": call_id, "name": name, "ok": ok,
        "result": _clip(text), "resultChars": len(text),
        "traceId": str(entry.get("trace_id") or ""),
        "ms": int(entry.get("ms") or 0),
        "write": bool(entry.get("write")),
        "error": "" if ok else str(entry.get("error") or text[:300]),
    })
    if name in _SUBAGENT_TOOLS:
        batch = str(args.get("batch") or "")
        if not batch:
            m = _BATCH_RE.search(text) or _BATCH_RE.search(arguments)
            batch = m.group(0) if m else ""
        agents = args.get("agents") if isinstance(args.get("agents"), list) else []
        pipeline = args.get("pipeline") if isinstance(args.get("pipeline"), list) else []
        _append(sid, "subagent", {
            "turn": int(turn or 0), "step": int(step or 0),
            "callId": call_id, "name": name, "batch": batch,
            "agents": agents, "pipeline": pipeline,
            "ok": ok, "text": _clip(text),
            "tokens": int(entry.get("tokens") or 0),
        })


def todo(sid, turn, plan):
    if not isinstance(plan, dict):
        return
    _append(sid, "todo/write", {"turn": int(turn or 0), "plan": plan})


def review(sid, turn, report):
    """验收子代理的判定（独立 AI 打分 + 证据 + 差距），只读旁路。"""
    if not isinstance(report, dict):
        return
    _append(sid, "review", {"turn": int(turn or 0), "report": report})


def compact(sid, turn, before=0, after=0, chars=0):
    _append(sid, "compact", {
        "turn": int(turn or 0), "before": int(before or 0),
        "after": int(after or 0), "chars": int(chars or 0),
    })


# ---------------------------------------------------------------- 投影

def _preview(text, n=140):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def _tool_schema(header, name):
    for it in (header or {}).get("tools") or []:
        fn = it.get("function") if isinstance(it, dict) else None
        if isinstance(fn, dict) and fn.get("name") == name:
            try:
                return json.dumps(it, ensure_ascii=False)
            except Exception:            # noqa: BLE001
                return str(it)
    return ""


def _transcript(messages):
    parts = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        text = str(m.get("content") or "").strip()
        tools = m.get("tools") or []
        if role == "user" and text:
            parts.append("【用户/工具结果】\n" + text)
        elif role == "assistant":
            if text:
                parts.append("【助手】\n" + text)
            for t in tools:
                if isinstance(t, dict):
                    parts.append("【工具】%s %s" % (
                        t.get("tool") or "?", _preview(json.dumps(
                            t.get("args") or {}, ensure_ascii=False, default=str), 200)))
        if sum(len(x) for x in parts) > MAX_SUB_TRANSCRIPT:
            parts.append("…（转录已截断）")
            break
    return _clip("\n\n".join(parts), MAX_SUB_TRANSCRIPT)


def _subagent_cells(sid, batch, parent_id):
    batch = str(batch or "")
    if not _BATCH_RE.fullmatch(batch):
        return []
    from app.store import DATA_DIR
    p = os.path.join(DATA_DIR, "subagents", batch + ".jsonl")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        rec = json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return []
    items = rec.get("items") or {}
    out = []
    for key, item in list(items.items())[:MAX_SUBAGENTS]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or key)
        ask = str(item.get("ask") or "")
        msgs = item.get("messages") or []
        out.append({
            "kind": "subtool",
            "recordId": "%s:%s" % (parent_id, key),
            "inline": True,              # 子代理转录来自独立文件，直接内联
            "text": "[%s] %s" % (role, _preview(ask) or "子代理"),
            "inputDetail": ask,
            "outputDetail": _transcript(msgs),
            "source": {"kind": "subagent", "label": role},
            "batch": batch,
            "subagent": True,
            "messages": len(msgs),
            "isError": not bool(msgs),
        })
    return out


def _finalize(sid, turns, before=None, limit_turns=30, source="events"):
    """回退投影 / 导出用的收尾：编号、统计、工具排行、token 曲线。"""
    total_turns = len(turns)
    if before:
        try:
            b = int(before)
            turns = [t for t in turns if int(t.get("turn") or 0) < b]
        except (TypeError, ValueError):
            pass
    has_more = False
    try:
        lim = int(limit_turns or 0)
    except (TypeError, ValueError):
        lim = 30
    if lim > 0 and len(turns) > lim:
        has_more = True
        turns = turns[-lim:]
    counts, tools, series, marks = {}, {}, [], []
    tokens = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    errors = 0
    steps = 0
    peak = 0
    idx = 1
    for t in turns:
        st = {"cells": 0, "steps": 0, "tools": 0, "errors": 0, "peakIn": 0,
              "outTok": 0, "inTok": 0, "think": 0, "cached": 0, "secs": 0}
        t["_gi"] = None
        for g in t.get("groups") or []:
            if g.get("step"):
                steps += 1
                st["steps"] += 1
            for c in g.get("cells") or []:
                c["index"] = idx
                c["inline"] = True       # 回退投影自带全文，前端不再按 refs 取
                idx += 1
                kind = c.get("kind") or "?"
                counts[kind] = counts.get(kind, 0) + 1
                st["cells"] += 1
                if c.get("isError"):
                    errors += 1
                    st["errors"] += 1
                if kind in ("tool", "subtool"):
                    st["tools"] += 1
                    name = c.get("toolName") or ""
                    if kind == "tool" and name:
                        d = tools.setdefault(name, {"name": name, "count": 0,
                                                    "errors": 0, "ms": 0, "chars": 0})
                        d["count"] += 1
                        d["ms"] += int((c.get("timeSeconds") or 0) * 1000)
                        d["chars"] += int(c.get("resultChars") or 0)
                        if c.get("isError"):
                            d["errors"] += 1
                pin = int(c.get("input") or 0)
                if pin > peak:
                    peak = pin
                if pin > st["peakIn"]:
                    st["peakIn"] = pin
                st["inTok"] += pin
                st["outTok"] += int(c.get("output") or 0)
                st["think"] += int(c.get("think") or 0)
                st["cached"] += int(c.get("cacheRead") or 0)
                st["secs"] += float(c.get("timeSeconds") or 0)
                if kind == "message":
                    series.append([int(t.get("turn") or 0), idx, pin,
                                   int(c.get("output") or 0),
                                   int(c.get("cacheRead") or 0),
                                   int(c.get("ttftMs") or 0),
                                   round(float(c.get("timeSeconds") or 0), 2)])
                if kind == "compacted":
                    marks.append({"type": "compact", "turn": int(t.get("turn") or 0),
                                  "at": int(c.get("startedAt") or 0)})
        t.pop("_gi", None)
        t["stats"] = st
    for t in turns:
        for g in t.get("groups") or []:
            for c in g.get("cells") or []:
                tokens["prompt"] += int(c.get("input") or 0)
                tokens["completion"] += int(c.get("output") or 0)
                tokens["reasoning"] += int(c.get("think") or 0)
                tokens["cached"] += int(c.get("cacheRead") or 0)
    return {
        "sid": sid, "source": source, "turns": turns,
        "stats": {"cells": idx - 1, "turns": total_turns, "steps": steps,
                  "tools": sum(d["count"] for d in tools.values()),
                  "by_kind": counts, "errors": errors, "tokens": tokens,
                  "peakContext": peak},
        "tools": sorted(tools.values(), key=lambda d: -d["count"])[:40],
        "series": _thin(series), "marks": marks,
        "has_more": has_more, "total_turns": total_turns,
    }


def _thin(series, limit=480):
    """token 曲线抽稀：点数太多前端画不动，等距取样并保留最后一点。"""
    n = len(series)
    if n <= limit:
        return series
    step = max(1, n // (limit - 1))
    out = series[::step][:limit - 1]
    out.append(series[-1])
    return out


# ---------------------------------------------------------------- 投影
# turn -> group（输入 / 第 N 步）-> cell。
# cell 只带预览与「详情引用」（refs），正文在选中时才按需取：
# 45MB 的会话若把输入 / 输出 / schema 全塞进列表，一次要回 43MB JSON。

_PROJ_CACHE = {}
_PROJ_MAX = 2


class _Proj:
    """增量投影器：事件只追加，所以新事件继续喂给同一个实例即可。"""

    def __init__(self, sid, events):
        self.sid = sid
        self.events = events
        self.pos = 0
        self.turns = []
        self.by_turn = {}
        self.calls = {}
        self.rids = set()
        self.header = {}
        self.header_seq = 0
        self.prev_system = ""
        self.no = 0
        self.total_turns = 0
        self.kind = {}
        self.tool_stats = {}
        self.tokens = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
        self.peak = 0
        self.errors = 0
        self.steps = 0
        self.series = []
        self.marks = []
        self.started = 0

    def sync(self, events):
        if self.events is not events:
            self.__init__(self.sid, events)
        if self.pos < len(events):
            for i in range(self.pos, len(events)):
                try:
                    self._one(events[i])
                except Exception:        # noqa: BLE001 单条坏事件不影响整份
                    pass
                self.pos = i + 1

    # ------------------------------------------------------------ 结构

    def _turn(self, n, ts=0):
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            n = (self.turns[-1]["turn"] + 1) if self.turns else 1
        t = self.by_turn.get(n)
        if t is None:
            t = {"turn": n, "start": int(ts or 0), "end": None, "reason": "",
                 "groups": [], "_gi": -1,
                 "stats": {"cells": 0, "steps": 0, "tools": 0, "errors": 0,
                           "peakIn": 0, "outTok": 0, "inTok": 0, "think": 0,
                           "cached": 0, "secs": 0}}
            self.by_turn[n] = t
            self.turns.append(t)
            self.total_turns = len(self.turns)
            if ts and not self.started:
                self.started = int(ts)
        elif ts and not t.get("start"):
            t["start"] = int(ts)
        return t

    def _group(self, t, title, step=0):
        i = t["_gi"]
        if 0 <= i < len(t["groups"]) and t["groups"][i]["title"] == title:
            if step:
                t["groups"][i]["step"] = step
            return t["groups"][i]
        for j in range(len(t["groups"]) - 1, -1, -1):
            if t["groups"][j]["title"] == title:
                t["_gi"] = j
                if step:
                    t["groups"][j]["step"] = step
                return t["groups"][j]
        g = {"title": title, "step": int(step or 0), "cells": []}
        t["groups"].append(g)
        t["_gi"] = len(t["groups"]) - 1
        return g

    def _cell(self, t, title, step, cell, ts=0):
        rid = str(cell.get("recordId") or "")
        if rid and rid in self.rids:     # 子代理可能被 tool/result 与 subagent 各展开一次
            return cell
        self.no += 1
        cell["index"] = self.no
        if not rid:
            cell["recordId"] = "c_%d" % self.no
        else:
            self.rids.add(rid)
        if ts and not cell.get("startedAt"):
            cell["startedAt"] = int(ts)
        self._group(t, title, step)["cells"].append(cell)
        kind = cell.get("kind") or "?"
        self.kind[kind] = self.kind.get(kind, 0) + 1
        st = t["stats"]
        st["cells"] += 1
        if cell.get("isError"):
            st["errors"] += 1
            self.errors += 1
        if kind in ("tool", "subtool"):
            st["tools"] += 1
            name = cell.get("toolName") or ""
            if kind == "tool" and name:
                d = self.tool_stats.setdefault(
                    name, {"name": name, "count": 0, "errors": 0, "ms": 0, "chars": 0})
                d["count"] += 1
        pin = int(cell.get("input") or 0)
        if pin:
            if pin > self.peak:
                self.peak = pin
            if pin > st["peakIn"]:
                st["peakIn"] = pin
            st["inTok"] += pin
            self.tokens["prompt"] += pin
        if cell.get("output"):
            st["outTok"] += int(cell["output"])
            self.tokens["completion"] += int(cell["output"])
        if cell.get("think"):
            st["think"] += int(cell["think"])
            self.tokens["reasoning"] += int(cell["think"])
        if cell.get("cacheRead"):
            st["cached"] += int(cell["cacheRead"])
            self.tokens["cached"] += int(cell["cacheRead"])
        if cell.get("timeSeconds"):
            st["secs"] += float(cell["timeSeconds"])
        if kind == "message":
            self.series.append([int(t.get("turn") or 0), self.no, pin,
                                int(cell.get("output") or 0),
                                int(cell.get("cacheRead") or 0),
                                int(cell.get("ttftMs") or 0),
                                round(float(cell.get("timeSeconds") or 0), 2)])
        return cell

    # ------------------------------------------------------------ 事件

    def _one(self, ev):
        typ = ev.get("type")
        data = ev.get("data") or {}
        ts = int(ev.get("ts") or 0)
        seq = int(ev.get("seq") or 0)

        if typ == "turn/start":
            self._turn(data.get("turn"), ts)
            return
        if typ == "turn/end":
            t = self._turn(data.get("turn"), ts)
            t["end"] = ts
            t["reason"] = str(data.get("reason") or "")
            if t["reason"] in ("error", "failed", "aborted"):
                self.marks.append({"type": "error", "turn": t["turn"], "at": ts,
                                   "label": t["reason"]})
            return
        if typ == "step/start":
            t = self._turn(data.get("turn"), ts)
            self.steps += 1
            t["stats"]["steps"] += 1
            return
        if typ == "request/header":
            self._header(ev, data, ts, seq)
            return
        if typ == "user/message":
            self._user(data, ts, seq)
            return
        if typ == "assistant/message":
            self._assistant(data, ts, seq)
            return
        if typ == "tool/call":
            self._tool_call(data, ts, seq)
            return
        if typ == "tool/result":
            self._tool_result(data, ts, seq)
            return
        if typ == "subagent":
            t = self._turn(data.get("turn"), ts)
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "第 1 步"
            call_id = str(data.get("callId") or "")
            batch = str(data.get("batch") or "")
            if self.calls.get(call_id) is not None and batch:
                for child in _subagent_cells(self.sid, batch, call_id):
                    self._cell(t, title, step, child, ts)
            return
        if typ == "todo/write":
            t = self._turn(data.get("turn"), ts)
            plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
            steps = plan.get("steps") or []
            done = sum(1 for s in steps if isinstance(s, dict)
                       and s.get("status") == "done")
            self._cell(t, self._title(t), 0, {
                "kind": "context",
                "text": "计划 v%s · %d 步 · %d 完成" % (
                    plan.get("revision") or 1, len(steps), done),
                "refs": {"in": str(seq)}, "source": {"kind": "plan", "label": "计划"},
                "sizes": {"input": len(json.dumps(plan, ensure_ascii=False,
                                                  default=str))},
            }, ts)
            self.marks.append({"type": "plan", "turn": int(t.get("turn") or 0),
                               "at": ts, "rev": plan.get("revision") or 1,
                               "done": done, "total": len(steps)})
            return
        if typ == "review":
            t = self._turn(data.get("turn"), ts)
            rep = data.get("report") if isinstance(data.get("report"), dict) else {}
            verdict = str(rep.get("verdict") or "")
            self._cell(t, self._title(t), 0, {
                "kind": "review",
                "text": "验收 %s" % (verdict or "—"),
                "refs": {"in": str(seq)}, "source": {"kind": "review", "label": "验收"},
                "isError": verdict in ("fail", "failed", "reject"),
                "sizes": {"input": len(json.dumps(rep, ensure_ascii=False,
                                                  default=str))},
            }, ts)
            self.marks.append({"type": "review", "turn": int(t.get("turn") or 0),
                               "at": ts, "verdict": verdict})
            return
        if typ == "compact":
            t = self._turn(data.get("turn"), ts)
            self._cell(t, "输入", 0, {
                "kind": "compacted",
                "text": "压缩历史：%s → %s 条，摘要 %s 字" % (
                    data.get("before") or 0, data.get("after") or 0,
                    data.get("chars") or 0),
                "refs": {"in": str(seq)},
            }, ts)
            self.marks.append({"type": "compact", "turn": int(t.get("turn") or 0),
                               "at": ts})
            return

    def _title(self, t):
        gs = t.get("groups") or []
        if gs and gs[-1].get("step"):
            return "第 %d 步" % gs[-1]["step"]
        return "第 1 步" if gs else "输入"

    def _header(self, ev, data, ts, seq):
        t = self._turn(data.get("turn"), ts)
        self.header = data.get("header") or {}
        self.header_seq = seq
        step = int(data.get("step") or 0)
        title = ("第 %d 步" % step) if step else "输入"
        system = str(self.header.get("system") or "")
        tools = self.header.get("tools") or []
        parts = data.get("parts") if isinstance(data.get("parts"), list) else []
        reason = str(data.get("reason") or "")
        changed = [str(p.get("label") or "") for p in parts if p.get("changed")]
        if reason == "initial":
            text = "初始系统提示 · %d 字 · 工具 %d 个" % (len(system), len(tools))
        elif reason == "resume":
            text = ("系统提示（进程重启后恢复）· %d 字 · 工具 %d 个"
                    % (len(system), len(tools)))
        elif reason == "change":
            bits = []
            if changed:
                bits.append("、".join(changed[:3]) + ("…" if len(changed) > 3 else ""))
            if data.get("changedTools"):
                bits.append("工具")
            text = "系统提示更新：%s · 工具 %d 个" % ("、".join(bits) or "内容",
                                                      len(tools))
        else:
            text = "系统提示 · %d 字 · 工具 %d 个" % (len(system), len(tools))
        try:
            schema_len = len(json.dumps(tools, ensure_ascii=False, default=str))
        except Exception:                # noqa: BLE001
            schema_len = 0
        cell = {
            "kind": "system", "recordId": "sys_%s_%s" % (t["turn"], step),
            "text": text, "refs": {"sys": str(seq)},
            "sizes": {"input": len(system), "schema": schema_len},
            "parts": [{"id": str(p.get("id") or ""),
                       "label": str(p.get("label") or ""),
                       "chars": int(p.get("chars") or 0),
                       "changed": bool(p.get("changed")),
                       "recordId": "part_%s_%s_%s" % (t["turn"], step, p.get("id"))}
                      for p in parts],
            "systemReason": reason,
            "model": str(self.header.get("model") or ""),
            "provider": str(self.header.get("provider") or ""),
            "toolCount": len(tools),
            "startedAt": ts,
        }
        if self.prev_system and self.prev_system != system:
            cell["diff"] = [len(self.prev_system), len(system)]
        self._cell(t, title, step, cell, ts)
        for p in parts:
            start = int(p.get("start") or 0)
            end = int(p.get("end") or 0)
            seg = system[start:end] if 0 <= start < end <= len(system) else ""
            if not seg:
                continue
            tag = ("（变化）" if reason == "change" and p.get("changed") else "")
            self._cell(t, title, step, {
                "kind": "context",
                "recordId": "part_%s_%s_%s" % (t["turn"], step, p.get("id")),
                "text": "%s%s · %d 字" % (tag, p.get("label") or "段落", len(seg)),
                "refs": {"part": str(seq)}, "seg": [start, end],
                "sizes": {"input": len(seg)},
                "source": {"kind": "prompt_part",
                           "label": str(p.get("label") or ""),
                           "part": str(p.get("id") or ""),
                           "changed": bool(p.get("changed"))},
            }, ts)
        self.prev_system = system

    def _user(self, data, ts, seq):
        t = self._turn(data.get("turn"), ts)
        src = data.get("source") if isinstance(data.get("source"), dict) else {}
        kind = str(src.get("kind") or "human")
        content = str(data.get("content") or "")
        label = str(src.get("label") or kind)
        if kind == "human":
            self._cell(t, "输入", 0, {
                "kind": "user", "text": _preview(content) or "（空消息）",
                "refs": {"in": str(seq)}, "opensTurn": True, "source": src,
                "sizes": {"input": len(content)},
            }, ts)
        else:
            self._cell(t, "输入", 0, {
                "kind": "context", "text": "[%s] %s" % (label, _preview(content)),
                "refs": {"in": str(seq)}, "source": src,
                "sizes": {"input": len(content)},
            }, ts)

    def _assistant(self, data, ts, seq):
        t = self._turn(data.get("turn"), ts)
        step = int(data.get("step") or 0)
        title = ("第 %d 步" % step) if step else "第 1 步"
        content = str(data.get("content") or "")
        reasoning = str(data.get("reasoning") or "")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}
        start, first, end = timing.get("start"), timing.get("first"), timing.get("end")
        dur = None
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            dur = max(0.0, float(end) - float(start))
        ttft = timing.get("first_ms")
        if not isinstance(ttft, (int, float)) and isinstance(start, (int, float)) \
                and isinstance(first, (int, float)):
            ttft = max(0.0, (float(first) - float(start)) * 1000.0)
        # 事件 ts 是「写回时刻」；瀑布要的是请求起止，所以额外带上 endAt
        started = int(round(float(start) * 1000)) if isinstance(start, (int, float)) else ts
        ended = int(round(float(end) * 1000)) if isinstance(end, (int, float)) else 0
        if reasoning:
            self._cell(t, title, step, {
                "kind": "reasoning", "text": _preview(reasoning),
                "refs": {"out": str(seq)}, "sizes": {"output": len(reasoning)},
                "startedAt": started, "endAt": ended,
            }, ts)
        out_tok = int(usage.get("completion_tokens") or 0)
        speed = None
        if isinstance(dur, (int, float)) and dur > 0 and out_tok:
            speed = round(out_tok / float(dur), 1)
        details = usage.get("prompt_tokens_details") or {}
        think = int((usage.get("completion_tokens_details") or {})
                    .get("reasoning_tokens") or 0) or int(
            usage.get("reasoning_tokens") or 0)
        self._cell(t, title, step, {
            "kind": "message",
            "text": _preview(content) or ("（只有思考）" if reasoning else "（无正文）"),
            "refs": {"out": str(seq)},
            "input": int(usage.get("prompt_tokens") or 0),
            "output": out_tok, "think": think,
            "cacheRead": int(details.get("cached_tokens") or 0),
            "cacheWrite": int(details.get("cache_write_tokens")
                              or details.get("cache_creation_tokens") or 0),
            "decodeTps": speed, "timeSeconds": dur, "ttftMs": ttft,
            "sizes": {"output": len(content), "thinking": len(reasoning)},
            "startedAt": started, "endAt": ended,
        }, ts)

    def _tool_call(self, data, ts, seq):
        t = self._turn(data.get("turn"), ts)
        step = int(data.get("step") or 0)
        title = ("第 %d 步" % step) if step else "第 1 步"
        name = str(data.get("name") or "")
        call_id = str(data.get("callId") or "")
        args = str(data.get("arguments") or "")
        cell = {
            "kind": "tool", "recordId": call_id,
            "text": "%s(%s)" % (name, _preview(args, 90)),
            "refs": {"in": str(seq)}, "callId": call_id, "toolName": name,
            "schemaRef": str(self.header_seq or ""),
            "subagent": name in _SUBAGENT_TOOLS,
            "sizes": {"input": len(args)},
            "search": args[:300],
        }
        self._cell(t, title, step, cell, ts)
        if call_id:
            self.calls[call_id] = cell

    def _tool_result(self, data, ts, seq):
        t = self._turn(data.get("turn"), ts)
        step = int(data.get("step") or 0)
        title = ("第 %d 步" % step) if step else "第 1 步"
        call_id = str(data.get("callId") or "")
        cell = self.calls.get(call_id)
        if cell is None:
            name = str(data.get("name") or "")
            cell = {"kind": "tool", "recordId": call_id, "text": name,
                    "callId": call_id, "toolName": name,
                    "schemaRef": str(self.header_seq or ""),
                    "subagent": name in _SUBAGENT_TOOLS, "sizes": {}}
            self._cell(t, title, step, cell, ts)
            self.calls[call_id] = cell
        text = str(data.get("result") or "")
        ok = bool(data.get("ok", True))
        was_error = bool(cell.get("isError"))
        cell.update({
            "isError": not ok,
            "traceId": str(data.get("traceId") or ""),
            "resultChars": int(data.get("resultChars") or 0),
            "write": bool(data.get("write")),
            "timeSeconds": (int(data.get("ms") or 0) / 1000.0
                            if data.get("ms") is not None else None),
            "errorText": str(data.get("error") or "")[:300],
            "endAt": ts or None,
            "sizes": dict(cell.get("sizes") or {}, output=int(
                data.get("resultChars") or len(text))),
        })
        refs = dict(cell.get("refs") or {})
        refs["out"] = str(seq)
        cell["refs"] = refs
        if not ok and not was_error:
            t["stats"]["errors"] += 1
            self.errors += 1
        name = str(cell.get("toolName") or data.get("name") or "")
        d = self.tool_stats.get(name)
        if d is not None:
            d["ms"] += int(data.get("ms") or 0)
            d["chars"] += int(data.get("resultChars") or 0)
            if not ok:
                d["errors"] += 1
        if cell.get("timeSeconds"):
            t["stats"]["secs"] += float(cell["timeSeconds"])
        if name in _SUBAGENT_TOOLS:
            batch = str(data.get("batch") or "")
            for child in _subagent_cells(self.sid, batch, call_id):
                self._cell(t, title, step, child, ts)

    # ------------------------------------------------------------ 输出

    def render(self, before=None, limit_turns=30, source="events"):
        turns = self.turns
        total = len(turns)
        if before:
            try:
                b = int(before)
                turns = [t for t in turns if int(t.get("turn") or 0) < b]
            except (TypeError, ValueError):
                pass
        has_more = False
        try:
            lim = int(limit_turns or 0)
        except (TypeError, ValueError):
            lim = 30
        if lim > 0 and len(turns) > lim:
            has_more = True
            turns = turns[-lim:]
        out = []
        for t in turns:
            st = dict(t["stats"])
            start, end = int(t.get("start") or 0), t.get("end")
            if start and end and end > start:
                st["durMs"] = int(end - start)
            else:
                st["durMs"] = int(round((st.get("secs") or 0) * 1000))
            item = {k: v for k, v in t.items() if not k.startswith("_")}
            item["stats"] = st
            out.append(item)
        tools = sorted(self.tool_stats.values(), key=lambda d: -d["count"])[:40]
        for d in tools:
            d["avgMs"] = int(d["ms"] / d["count"]) if d["count"] else 0
        return {
            "sid": self.sid, "source": source, "turns": out,
            "stats": {"cells": self.no, "turns": self.total_turns,
                      "steps": self.steps,
                      "tools": sum(d["count"] for d in self.tool_stats.values()),
                      "by_kind": dict(self.kind), "errors": self.errors,
                      "tokens": dict(self.tokens), "peakContext": self.peak,
                      "startedAt": self.started},
            "tools": tools, "series": _thin(self.series[-2000:]),
            "marks": self.marks[-200:], "has_more": has_more,
            "total_turns": total,
        }


    def turn_index(self, before=None, limit=500):
        """全量轮次摘要：给左侧轮次导航用，不带 cells。"""
        turns = self.turns
        total = len(turns)
        if before:
            try:
                b = int(before)
                turns = [t for t in turns if int(t.get("turn") or 0) < b]
            except (TypeError, ValueError):
                pass
        has_more = False
        try:
            lim = max(0, int(limit or 0))
        except (TypeError, ValueError):
            lim = 500
        if lim and len(turns) > lim:
            has_more = True
            turns = turns[-lim:]
        out = []
        for t in turns:
            st = dict(t.get("stats") or {})
            writes = subagents = reviews = 0
            title = ""
            for g in t.get("groups") or []:
                for c in g.get("cells") or []:
                    if c.get("write"):
                        writes += 1
                    if c.get("kind") == "subtool":
                        subagents += 1
                    if c.get("kind") == "review":
                        reviews += 1
                    if not title and c.get("kind") == "user":
                        title = str(c.get("text") or "")[:80]
            start, end = int(t.get("start") or 0), t.get("end")
            if start and end and end > start:
                dur = int(end - start)
            else:
                dur = int(round((st.get("secs") or 0) * 1000))
            st.update({"writes": writes, "subagents": subagents, "reviews": reviews})
            out.append({
                "turn": int(t.get("turn") or 0), "start": start, "end": end,
                "durMs": dur, "reason": str(t.get("reason") or ""),
                "title": title or ("Turn %d" % int(t.get("turn") or 0)),
                "stats": st,
                "flags": {"errors": bool(st.get("errors")), "writes": bool(writes),
                          "subagent": bool(subagents), "review": bool(reviews)},
            })
        return {"sid": self.sid, "turns": out, "total": total,
                "has_more": has_more}


def _proj_for(sid):
    events = _iter_events(sid)
    if not events:
        with _EV_LOCK:
            _PROJ_CACHE.pop(sid, None)
        return None
    with _EV_LOCK:
        p = _PROJ_CACHE.get(sid)
        if p is None:
            p = _Proj(sid, events)
            _PROJ_CACHE[sid] = p
            if len(_PROJ_CACHE) > _PROJ_MAX:
                for k in list(_PROJ_CACHE)[:-_PROJ_MAX]:
                    if k != sid:
                        _PROJ_CACHE.pop(k, None)
    p.sync(events)
    return p


def build(sid, before=None, limit_turns=30):
    """把轨迹事件投影成 turn -> group -> cell（轻量，详情按需取）。"""
    p = _proj_for(sid)
    if p is None:
        return _build_from_session(sid, before=before, limit_turns=limit_turns)
    return p.render(before=before, limit_turns=limit_turns)


def turn_index(sid, before=None, limit=500):
    """全量轮次摘要：只读导航，不返回 cells。"""
    p = _proj_for(sid)
    if p is None:
        return {"sid": sid, "turns": [], "total": 0, "has_more": False}
    return p.turn_index(before=before, limit=limit)


_SEARCH_CACHE = {}
_SEARCH_LOCK = threading.Lock()


def _snippet(text, ql, width=100):
    i = text.lower().find(ql)
    if i < 0:
        return text[:width].replace("\n", " ")
    a = max(0, i - 40)
    b = min(len(text), i + len(ql) + 70)
    return (("…" if a else "") + text[a:b].replace("\n", " ")
            + ("…" if b < len(text) else ""))


def _search_scan(sid, q, kinds=None, tool="", errors=False, writes=False,
                 subagent=False):
    ql = q.lower()
    events = _iter_events(sid)
    p = _proj_for(sid)
    seq_cells = {}
    if p is not None:
        for t in p.turns:
            for g in t.get("groups") or []:
                for c in g.get("cells") or []:
                    for v in (c.get("refs") or {}).values():
                        try:
                            seq_cells.setdefault(int(v), []).append(c)
                        except (TypeError, ValueError):
                            pass
    kinds = set(kinds or [])
    hits = []
    for ev in events:
        typ = ev.get("type")
        data = ev.get("data") or {}
        try:
            turn = int(data.get("turn") or 0)
        except (TypeError, ValueError):
            continue
        if turn <= 0:
            continue
        try:
            step = int(data.get("step") or 0)
        except (TypeError, ValueError):
            step = 0
        try:
            seq = int(ev.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        name = ""
        items = []                       # (kind, text, ok)
        if typ == "user/message":
            src = data.get("source") if isinstance(data.get("source"), dict) else {}
            kind = "user" if str(src.get("kind") or "") == "human" else "context"
            items.append((kind, str(data.get("content") or ""), True))
        elif typ == "assistant/message":
            items.append(("message", str(data.get("content") or ""), True))
            items.append(("reasoning", str(data.get("reasoning") or ""), True))
        elif typ == "tool/call":
            name = str(data.get("name") or "")
            items.append(("tool", name + "\n" + str(data.get("arguments") or ""), True))
        elif typ == "tool/result":
            name = str(data.get("name") or "")
            items.append(("tool", name + "\n" + str(data.get("result") or ""),
                          bool(data.get("ok", True))))
        elif typ == "review":
            rep = data.get("report") if isinstance(data.get("report"), dict) else {}
            verdict = str(rep.get("verdict") or "")
            items.append(("review", json.dumps(rep, ensure_ascii=False, default=str),
                          verdict not in ("fail", "failed", "reject")))
        elif typ == "todo/write":
            items.append(("context", json.dumps(data.get("plan") or {},
                                                ensure_ascii=False, default=str), True))
        elif typ == "compact":
            items.append(("compacted", "压缩历史：%s → %s 条，摘要 %s 字" % (
                data.get("before") or 0, data.get("after") or 0,
                data.get("chars") or 0), True))
        else:
            continue
        if writes and typ != "tool/result":
            continue
        if subagent and not (typ == "tool/result" and name in _SUBAGENT_TOOLS):
            continue
        for kind, text, ok in items:
            if not text:
                continue
            if kinds and kind not in kinds:
                continue
            if tool and tool.lower() not in name.lower():
                continue
            if errors and ok:
                continue
            if typ == "tool/result" and writes and not data.get("write"):
                continue
            if ql not in text.lower():
                continue
            rid = ""
            for c in seq_cells.get(seq, []):
                if c.get("kind") == kind:
                    rid = str(c.get("recordId") or "")
                    break
            hits.append({"turn": turn, "step": step, "seq": seq, "kind": kind,
                         "tool": name, "recordId": rid,
                         "snippet": _snippet(text, ql)})
    # 新记录优先，并按 (turn, seq, kind) 去重
    seen = set()
    out = []
    for h in reversed(hits):
        key = (h["turn"], h["seq"], h["kind"], h["tool"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def search(sid, q="", kinds=None, tool="", errors=False, writes=False,
           subagent=False, limit=50, offset=0):
    """全量轨迹搜索：扫事件账本，返回可跳转的命中。"""
    q = str(q or "").strip()
    if not q:
        return {"ok": True, "hits": [], "total": 0, "has_more": False}
    rev = revision(sid)
    key = (sid, json.dumps(rev, sort_keys=True, ensure_ascii=False),
           q.lower(), tuple(sorted(kinds or [])), str(tool or ""),
           bool(errors), bool(writes), bool(subagent))
    with _SEARCH_LOCK:
        cached = _SEARCH_CACHE.get(key)
    if cached is None:
        cached = _search_scan(sid, q, kinds, tool, errors, writes, subagent)
        with _SEARCH_LOCK:
            _SEARCH_CACHE[key] = cached
            while len(_SEARCH_CACHE) > 8:
                _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
    try:
        off = max(0, int(offset or 0))
        lim = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        off, lim = 0, 50
    total = len(cached)
    return {"ok": True, "hits": cached[off:off + lim], "total": total,
            "has_more": off + lim < total, "rev": rev}


def _build_from_session(sid, before=None, limit_turns=30):
    """旧会话回退：从 message.blocks / tools / timing 近似还原轨迹。"""
    try:
        from app.sessions import load_sessions, find_session
        from app.context import TOOL_MSG_PREFIX
    except Exception:                    # noqa: BLE001
        return _finalize(sid, [], before=before, limit_turns=limit_turns,
                         source="session")
    try:
        data = load_sessions()
        s = find_session(data, sid)
    except Exception:                    # noqa: BLE001
        s = None
    if not s:
        return _finalize(sid, [], before=before, limit_turns=limit_turns,
                         source="session")
    turns = []
    cur = None

    def ensure():
        nonlocal cur
        if cur is None:
            cur = {"turn": len(turns) + 1, "start": 0, "end": None, "reason": "",
                   "groups": [{"title": "输入", "description": "", "cells": []},
                              {"title": "第 1 步", "description": "", "cells": []}],
                   "_g": None}
            turns.append(cur)
        return cur

    def add(t, title, cell):
        g = next((x for x in t["groups"] if x["title"] == title), None)
        if g is None:
            g = {"title": title, "description": "", "cells": []}
            t["groups"].append(g)
        g["cells"].append(cell)

    for m in s.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            text = str(m.get("content") or "")
            if text.startswith(TOOL_MSG_PREFIX):
                continue
            cur = None
            t = ensure()
            add(t, "输入", {"kind": "user", "text": _preview(text) or "（空消息）",
                            "inputDetail": text, "opensTurn": True,
                            "source": {"kind": "human"}})
            continue
        if role != "assistant":
            continue
        t = ensure()
        tools = m.get("tools") or []
        usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
        timing = m.get("timing") if isinstance(m.get("timing"), dict) else {}
        base = {"input": int(usage.get("prompt_tokens") or 0),
                "output": int(usage.get("completion_tokens") or 0),
                "think": int(usage.get("reasoning_tokens") or 0)}
        blocks = m.get("blocks") or []
        if blocks:
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                kind = b.get("kind")
                if kind == "reasoning" and b.get("text"):
                    add(t, "第 1 步", {"kind": "reasoning",
                                       "text": _preview(b.get("text")),
                                       "thinkingDetail": str(b.get("text"))})
                elif kind == "tool":
                    try:
                        idx = int(b.get("i") or 0)
                    except (TypeError, ValueError):
                        idx = 0
                    if 0 <= idx < len(tools) and isinstance(tools[idx], dict):
                        e = tools[idx]
                        add(t, "第 1 步", _tool_cell_from_entry(e))
                else:
                    add(t, "第 1 步", {"kind": "message",
                                       "text": _preview(m.get("content")) or "（正文）",
                                       "outputDetail": str(m.get("content") or ""),
                                       **base})
        else:
            if m.get("reasoning"):
                add(t, "第 1 步", {"kind": "reasoning",
                                   "text": _preview(m.get("reasoning")),
                                   "thinkingDetail": str(m.get("reasoning"))})
            for e in tools:
                if isinstance(e, dict):
                    add(t, "第 1 步", _tool_cell_from_entry(e))
            if m.get("content"):
                add(t, "第 1 步", {"kind": "message",
                                   "text": _preview(m.get("content")),
                                   "outputDetail": str(m.get("content") or ""),
                                   **base})
        end = timing.get("total_ms")
        if isinstance(end, (int, float)):
            t["end"] = int(end)
    return _finalize(sid, turns, before=before, limit_turns=limit_turns,
                     source="session")


def _tool_cell_from_entry(entry):
    text = str(entry.get("result") or "")
    name = str(entry.get("tool") or "?")
    try:
        args = json.dumps(entry.get("args") or {}, ensure_ascii=False,
                          default=str)
    except Exception:                    # noqa: BLE001
        args = str(entry.get("args") or "")
    return {
        "kind": "tool", "recordId": str(entry.get("trace_id") or ""),
        "text": "%s(%s)" % (name, _preview(args, 80)),
        "inputDetail": args, "outputDetail": text, "result": text,
        "traceId": str(entry.get("trace_id") or ""),
        "callId": str(entry.get("trace_id") or ""),
        "toolName": name, "subagent": name in _SUBAGENT_TOOLS,
        "isError": not bool(entry.get("ok", True)),
        "timeSeconds": (int(entry.get("ms") or 0) / 1000.0
                        if entry.get("ms") is not None else None),
    }


def revision(sid):
    """轨迹日志的轻量版本标记（大小 + mtime），供前端「跟随最新」轮询判重。"""
    out = {}
    for key, p in (("cur", _path(sid)), ("old", _old_path(sid))):
        try:
            st = os.stat(p)
            out[key] = [int(st.st_size), int(st.st_mtime_ns)]
        except OSError:
            out[key] = [0, 0]
    return out


def _json_text(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:                    # noqa: BLE001
        return str(obj)


def _header_diff(sid, ev):
    """和上一条请求头比：只给系统提示差异，省得把两份全文都塞给前端。"""
    seq = int(ev.get("seq") or 0)
    cur = ((ev.get("data") or {}).get("header") or {}).get("system") or ""
    prev = ""
    for it in _iter_events(sid):
        if int(it.get("seq") or 0) >= seq:
            break
        if it.get("type") == "request/header":
            prev = ((it.get("data") or {}).get("header") or {}).get("system") or ""
    if not prev or prev == cur:
        return ""
    return _diff_text(prev, cur)


def cell_detail(sid, refs=None, kind="", seg=None, schema_ref=0,
                tool_name="", trace_id="", record_id=""):
    """按引用取一条记录的完整正文：列表只带预览，点开才算。"""
    refs = refs if isinstance(refs, dict) else {}
    parts = {}

    def get(key):
        try:
            return _event_by_seq(sid, refs.get(key))
        except (TypeError, ValueError):
            return None

    ev = get("sys") or get("part")
    if ev is not None and ev.get("type") == "request/header":
        header = (ev.get("data") or {}).get("header") or {}
        system = str(header.get("system") or "")
        if kind == "system":
            parts["input"] = system
            parts["schema"] = _json_text(header.get("tools") or [])
            diff = _header_diff(sid, ev)
            if diff:
                parts["diff"] = diff
        else:
            try:
                s, e = int((seg or [0, 0])[0] or 0), int((seg or [0, 0])[1] or 0)
            except (TypeError, ValueError, IndexError):
                s, e = 0, 0
            s = max(0, min(s, len(system)))
            e = max(s, min(e, len(system)))
            parts["input"] = system[s:e]

    ev = get("in")
    if ev is not None:
        typ = ev.get("type")
        d = ev.get("data") or {}
        if typ == "user/message":
            parts["input"] = str(d.get("content") or "")
            src = d.get("source") if isinstance(d.get("source"), dict) else {}
            parts["label"] = "%s · %s" % (src.get("kind") or "human",
                                          src.get("label") or "")
        elif typ == "tool/call":
            parts["input"] = str(d.get("arguments") or "")
        elif typ == "todo/write":
            parts["input"] = _json_text(d.get("plan"))
        elif typ == "review":
            parts["input"] = _json_text(d.get("report"))
        elif typ == "compact":
            parts["input"] = "压缩前 %s 条 → 压缩后 %s 条\n摘要 %s 字" % (
                d.get("before") or 0, d.get("after") or 0, d.get("chars") or 0)
        elif typ == "assistant/message":
            parts["input"] = str(d.get("reasoning") or "")
            parts["output"] = str(d.get("content") or "")

    ev = get("out")
    if ev is not None:
        typ = ev.get("type")
        d = ev.get("data") or {}
        if typ == "assistant/message":
            parts["thinking"] = str(d.get("reasoning") or "")
            if kind != "reasoning":
                parts["output"] = str(d.get("content") or "")
        elif typ == "tool/result":
            text = str(d.get("result") or "")
            tid = str(trace_id or d.get("traceId") or "")
            if tid:
                try:
                    from app import tool_trace
                    full = tool_trace.load(sid, tid)
                    if isinstance(full, dict) and full.get("result") is not None:
                        text = str(full.get("result") or "")
                        parts["from"] = "trace"
                except Exception:        # noqa: BLE001
                    pass
            parts["output"] = text
            if not bool(d.get("ok", True)):
                parts["error"] = str(d.get("error") or "")

    if schema_ref and tool_name:
        try:
            hev = _event_by_seq(sid, schema_ref)
        except (TypeError, ValueError):
            hev = None
        if hev is not None:
            header = (hev.get("data") or {}).get("header") or {}
            schema = _tool_schema(header, tool_name)
            if schema:
                parts["schema"] = _json_text(json.loads(schema))
    return {"ok": True, "parts": parts, "recordId": str(record_id or "")}


def tool_detail(sid, trace_id="", call_id=""):
    """按 trace_id 取完整工具结果；找不到时回退到轨迹里存的副本。"""
    tid = str(trace_id or "")
    if tid:
        try:
            from app import tool_trace
            d = tool_trace.load(sid, tid)
            if isinstance(d, dict):
                return {"ok": True, "source": "trace",
                        "result": str(d.get("result") or ""), "entry": d}
        except Exception:                # noqa: BLE001
            pass
    cid = str(call_id or "")
    for ev in _iter_events(sid):
        if ev.get("type") != "tool/result":
            continue
        d = ev.get("data") or {}
        if cid and str(d.get("callId") or "") == cid:
            return {"ok": True, "source": "event",
                    "result": str(d.get("result") or ""), "entry": d}
    return {"ok": False, "error": "找不到该工具结果（可能已随会话滚动清理）"}


def export(sid):
    """导出完整轨迹（事件 + 投影）。"""
    events = _iter_events(sid)
    view = build(sid, limit_turns=0)
    return {"sid": sid, "version": 1, "events": events, "view": view}


_KIND_HTML = {
    "user": "输入", "context": "上下文", "system": "系统", "reasoning": "思考",
    "message": "正文", "tool": "工具", "subtool": "子代理",
    "compacted": "压缩", "review": "验收",
}


def _human_num(n):
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    a = abs(v)
    if a >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if a >= 1e6:
        return "%.2fM" % (v / 1e6)
    if a >= 1e4:
        return "%.1fK" % (v / 1e3)
    return "%d" % v


def _human_dur(ms):
    try:
        n = float(ms or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n >= 60000:
        return "%.1fm" % (n / 60000)
    if n >= 1000:
        return "%.1fs" % (n / 1000)
    return "%dms" % n


def _esc(v):
    return html.escape("" if v is None else str(v))


def export_html(sid):
    """自包含只读 HTML：轮次 / 步骤 / 记录摘要，正文收在 details 里。"""
    view = build(sid, limit_turns=0)
    turns = view.get("turns") or []
    st = view.get("stats") or {}
    tk = st.get("tokens") or {}
    esc = _esc

    head = (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>轨迹 · %s</title><style>%s</style></head><body>" % (
            esc(sid), _HTML_CSS))
    stats = ("%d 轮 · %d 步 · %d 条记录 · %d 次工具 · %d 失败 · 输出 %s token" % (
        st.get("turns") or 0, st.get("steps") or 0, st.get("cells") or 0,
        st.get("tools") or 0, st.get("errors") or 0,
        _human_num(tk.get("completion"))))
    out = [head, '<header><h1>轨迹</h1><p class="sub">%s</p><p class="sub mono">%s</p></header><main>' % (
        esc(stats), esc(sid))]

    for idx, t in enumerate(turns):
        tno = int(t.get("turn") or 0)
        tst = t.get("stats") or {}
        title = "Turn %d" % tno
        bits = []
        for g in t.get("groups") or []:
            for c in g.get("cells") or []:
                if c.get("kind") == "user":
                    title = str(c.get("text") or "")[:100] or title
                    break
            if title != "Turn %d" % tno:
                break
        if tst.get("steps"):
            bits.append("%d 步" % tst["steps"])
        bits.append("%d 条" % (tst.get("cells") or 0))
        if tst.get("tools"):
            bits.append("%d 工具" % tst["tools"])
        if tst.get("errors"):
            bits.append("%d 失败" % tst["errors"])
        if tst.get("durMs"):
            bits.append(_human_dur(tst.get("durMs")))
        if t.get("reason"):
            bits.append(str(t.get("reason")))
        is_open = " open" if idx >= len(turns) - 3 else ""
        out.append('<section class="turn"><details%s><summary>'
                   '<span class="tno">T%d</span><span class="ttitle">%s</span>'
                   '<span class="tmeta">%s</span></summary><div class="steps">' % (
                       is_open, tno, esc(title), esc(" · ".join(bits))))
        for g in t.get("groups") or []:
            cells = g.get("cells") or []
            if not cells:
                continue
            gbits = []
            gpeak = gout = 0
            gtools = gerr = 0
            gsecs = 0.0
            for c in cells:
                if c.get("kind") in ("tool", "subtool"):
                    gtools += 1
                if c.get("isError"):
                    gerr += 1
                gpeak = max(gpeak, int(c.get("input") or 0))
                gout += int(c.get("output") or 0)
                gsecs += float(c.get("timeSeconds") or 0)
            if gtools:
                gbits.append("%d 工具" % gtools)
            if gpeak or gout:
                gbits.append("%s → %s" % (_human_num(gpeak), _human_num(gout)))
            if gsecs:
                gbits.append(_human_dur(gsecs * 1000))
            if gerr:
                gbits.append("%d 失败" % gerr)
            out.append('<section class="step"><h3><span>%s</span>'
                       '<span class="smeta">%s · %d 条</span></h3>' % (
                           esc(g.get("title") or ""), esc(" · ".join(gbits)), len(cells)))
            for c in cells:
                kind = str(c.get("kind") or "")
                meta = []
                if c.get("timeSeconds"):
                    meta.append(_human_dur(float(c.get("timeSeconds") or 0) * 1000))
                if c.get("input") or c.get("output"):
                    meta.append("%s → %s" % (
                        _human_num(c.get("input")), _human_num(c.get("output"))))
                if c.get("toolName"):
                    meta.append(str(c.get("toolName")))
                if c.get("resultChars"):
                    meta.append("%s 字" % _human_num(c.get("resultChars")))
                elif (c.get("sizes") or {}).get("output"):
                    meta.append("%s 字" % _human_num((c.get("sizes") or {}).get("output")))
                body = str(c.get("text") or "")
                if c.get("errorText"):
                    body += ("\n\n[错误] " + str(c.get("errorText"))) if body else (
                        "[错误] " + str(c.get("errorText")))
                out.append('<details class="cell k-%s%s"><summary>'
                           '<span class="kind">%s</span><span class="ctitle">%s</span>'
                           '<span class="cmeta">%s</span></summary><pre>%s</pre></details>' % (
                               esc(kind), " err" if c.get("isError") else "",
                               esc(_KIND_HTML.get(kind, kind or "?")),
                               esc(c.get("text") or "（空）"),
                               esc(" · ".join(meta)), esc(body or "（无正文）")))
            out.append("</section>")
        out.append("</div></details></section>")

    if not turns:
        out.append('<p class="empty">这个会话还没有轨迹记录。</p>')
    out.append("</main></body></html>")
    return "\n".join(out)


_HTML_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfd;--fg:#1c1e21;--dim:#6b7280;--line:#e5e7eb;
--card:#fff;--accent:#4f46e5;--err:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e6e7ea;--dim:#9aa0a8;
--line:#2b2d31;--card:#1d1f23;--accent:#8b8cf0;--err:#f87171}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px}
h1{margin:0 0 4px;font-size:20px}
.sub{margin:0;color:var(--dim);font-size:12px}
.mono{font-family:ui-monospace,Consolas,monospace}
main{max-width:1100px;margin:0 auto}
.turn{border:1px solid var(--line);border-radius:8px;background:var(--card);margin:0 0 12px}
.turn>details{overflow:hidden;border-radius:8px}
summary{cursor:pointer;padding:8px 12px;display:flex;gap:10px;align-items:baseline;
background:rgba(127,127,127,.05);border-bottom:1px solid var(--line)}
.turn>details[open]>summary{border-bottom:1px solid var(--line)}
.tno{font:600 12px ui-monospace,Consolas,monospace;color:var(--dim)}
.ttitle{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tmeta{margin-left:auto;color:var(--dim);font-size:12px;white-space:nowrap}
.steps{padding:8px 12px 12px}
.step{border-left:2px solid var(--line);padding-left:10px;margin:10px 0}
.step h3{margin:0 0 6px;display:flex;gap:10px;font-size:11px;font-weight:600;
letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
.smeta{font-weight:400;letter-spacing:0;text-transform:none}
.cell{border:1px solid var(--line);border-radius:6px;margin:4px 0}
.cell>summary{padding:5px 8px;border-bottom:0;background:transparent;gap:8px;font-size:12px}
.cell[open]>summary{border-bottom:1px solid var(--line)}
.kind{flex:0 0 auto;min-width:38px;color:var(--dim)}
.ctitle{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmeta{color:var(--dim);font-size:11px;white-space:nowrap}
.cell.err .kind,.cell.err .ctitle{color:var(--err)}
pre{margin:0;padding:10px 12px;white-space:pre-wrap;word-break:break-word;
font:12px/1.65 ui-monospace,Consolas,monospace}
.empty{color:var(--dim)}
"""
