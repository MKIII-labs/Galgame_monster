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


def _iter_events(sid):
    """按时间顺序读事件（旧滚动文件在前）。坏行跳过。"""
    out = []
    for p in (_old_path(sid), _path(sid)):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get("type"):
                        out.append(item)
        except OSError:
            continue
    out.sort(key=lambda x: int(x.get("seq") or 0))
    return out


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
    counts = {}
    tokens = {"prompt": 0, "completion": 0, "reasoning": 0}
    errors = 0
    idx = 1
    for t in turns:
        t.pop("_g", None)
        for g in t.get("groups") or []:
            for c in g.get("cells") or []:
                c["index"] = idx
                idx += 1
                counts[c.get("kind") or "?"] = counts.get(c.get("kind") or "?", 0) + 1
                if c.get("isError"):
                    errors += 1
                for k in tokens:
                    try:
                        tokens[k] += int(c.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
    return {
        "sid": sid, "source": source, "turns": turns,
        "stats": {"cells": idx - 1, "turns": total_turns,
                  "by_kind": counts, "errors": errors, "tokens": tokens},
        "has_more": has_more,
    }


def build(sid, before=None, limit_turns=30):
    """把轨迹事件投影成 turn -> group -> cell。没有轨迹日志时回退到会话消息。"""
    events = _iter_events(sid)
    if not events:
        return _build_from_session(sid, before=before, limit_turns=limit_turns)

    turns, by_turn, calls = [], {}, {}
    header = {}
    prev_system = ""

    def ensure_turn(n, ts=0):
        n = int(n or 0)
        if n <= 0:
            n = (turns[-1]["turn"] + 1) if turns else 1
        t = by_turn.get(n)
        if t is None:
            t = {"turn": n, "start": int(ts or 0), "end": None, "reason": "",
                 "groups": [], "_g": None}
            by_turn[n] = t
            turns.append(t)
        return t

    def group_of(t, title, desc=""):
        g = t.get("_g")
        if g is not None and g.get("title") == title:
            return g
        g = {"title": title, "description": desc, "cells": []}
        t["groups"].append(g)
        t["_g"] = g
        return g

    def add(t, title, cell):
        group_of(t, title)["cells"].append(cell)

    for ev in events:
        typ = ev.get("type")
        data = ev.get("data") or {}
        ts = int(ev.get("ts") or 0)
        if typ == "turn/start":
            ensure_turn(data.get("turn"), ts)
        elif typ == "turn/end":
            t = ensure_turn(data.get("turn"), ts)
            t["end"] = ts
            t["reason"] = str(data.get("reason") or "")
        elif typ == "request/header":
            t = ensure_turn(data.get("turn"), ts)
            header = data.get("header") or {}
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "输入"
            system = str(header.get("system") or "")
            tools = header.get("tools") or []
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
                    bits.append("、".join(changed[:3]) + ("…" if len(changed) > 3
                                                          else ""))
                if data.get("changedTools"):
                    bits.append("工具")
                text = "系统提示更新：%s · 工具 %d 个" % ("、".join(bits) or "内容",
                                                          len(tools))
            else:                          # 旧事件没有 reason / parts：保持原样
                text = "系统提示 · %d 字 · 工具 %d 个" % (len(system), len(tools))
            cell = {
                "kind": "system",
                "recordId": "sys_%s_%s" % (t["turn"], step),
                "text": text,
                "inputDetail": system,
                "schemaDetail": json.dumps(tools, ensure_ascii=False, default=str),
                "parts": [{"id": str(p.get("id") or ""),
                           "label": str(p.get("label") or ""),
                           "chars": int(p.get("chars") or 0),
                           "changed": bool(p.get("changed")),
                           "recordId": "part_%s_%s_%s"
                                       % (t["turn"], step, p.get("id"))}
                          for p in parts],
                "systemReason": reason,
                "model": str(header.get("model") or ""),
                "provider": str(header.get("provider") or ""),
                "toolCount": len(tools),
                "startedAt": ts,
            }
            if prev_system and prev_system != system:
                cell["promptDiff"] = _diff_text(prev_system, system)
            add(t, title, cell)
            # 每个分段单独成行（对齐 DSH 的「注入项逐条可见」）：
            # 技能索引 / 系统时间 / Agent 工具说明…都在这里能找到原文。
            for p in parts:
                start = int(p.get("start") or 0)
                end = int(p.get("end") or 0)
                seg = system[start:end] if 0 <= start < end <= len(system) else ""
                if not seg:
                    continue
                tag = ("（变化）" if reason == "change" and p.get("changed")
                       else "")
                add(t, title, {
                    "kind": "context",
                    "recordId": "part_%s_%s_%s" % (t["turn"], step, p.get("id")),
                    "text": "%s%s · %d 字" % (tag, p.get("label") or "段落",
                                              len(seg)),
                    "inputDetail": seg,
                    "source": {"kind": "prompt_part",
                               "label": str(p.get("label") or ""),
                               "part": str(p.get("id") or ""),
                               "changed": bool(p.get("changed"))},
                    "startedAt": ts,
                })
            prev_system = system
        elif typ == "user/message":
            t = ensure_turn(data.get("turn"), ts)
            src = data.get("source") if isinstance(data.get("source"), dict) else {}
            kind = str(src.get("kind") or "human")
            content = str(data.get("content") or "")
            label = str(src.get("label") or kind)
            if kind == "human":
                add(t, "输入", {
                    "kind": "user", "text": _preview(content) or "（空消息）",
                    "inputDetail": content, "opensTurn": True,
                    "source": src, "startedAt": ts,
                })
            else:
                add(t, "输入", {
                    "kind": "context", "text": "[%s] %s" % (label, _preview(content)),
                    "inputDetail": content, "source": src, "startedAt": ts,
                })
        elif typ == "assistant/message":
            t = ensure_turn(data.get("turn"), ts)
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "第 1 步"
            content = str(data.get("content") or "")
            reasoning = str(data.get("reasoning") or "")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}
            start = timing.get("start")
            first = timing.get("first")
            end = timing.get("end")
            dur = None
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                dur = max(0.0, float(end) - float(start))
            ttft = timing.get("first_ms")
            if not isinstance(ttft, (int, float)) and isinstance(start, (int, float)) \
                    and isinstance(first, (int, float)):
                ttft = max(0.0, (float(first) - float(start)) * 1000.0)
            if reasoning:
                add(t, title, {
                    "kind": "reasoning", "text": _preview(reasoning),
                    "thinkingDetail": reasoning, "startedAt": ts,
                })
            out_tok = int(usage.get("completion_tokens") or 0)
            speed = None
            if isinstance(dur, (int, float)) and dur > 0 and out_tok:
                speed = round(out_tok / float(dur), 1)     # decode tok/s
            details = usage.get("prompt_tokens_details") or {}
            add(t, title, {
                "kind": "message",
                "text": _preview(content) or ("（只有思考）" if reasoning else "（无正文）"),
                "outputDetail": content,
                "input": int(usage.get("prompt_tokens") or 0),
                "output": out_tok,
                "think": int((usage.get("completion_tokens_details") or {})
                             .get("reasoning_tokens") or 0)
                         or int(usage.get("reasoning_tokens") or 0),
                "cacheRead": int(details.get("cached_tokens") or 0),
                "cacheWrite": int(details.get("cache_write_tokens")
                                  or details.get("cache_creation_tokens") or 0),
                "decodeTps": speed,
                "timeSeconds": dur, "ttftMs": ttft,
                "startedAt": ts,
            })
        elif typ == "tool/call":
            t = ensure_turn(data.get("turn"), ts)
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "第 1 步"
            name = str(data.get("name") or "")
            call_id = str(data.get("callId") or "")
            args = str(data.get("arguments") or "")
            cell = {
                "kind": "tool", "recordId": call_id,
                "text": "%s(%s)" % (name, _preview(args, 80)),
                "inputDetail": args, "callId": call_id, "toolName": name,
                "schemaDetail": _tool_schema(header, name),
                "subagent": name in _SUBAGENT_TOOLS,
                "startedAt": ts,
            }
            add(t, title, cell)
            calls[call_id] = cell
        elif typ == "tool/result":
            t = ensure_turn(data.get("turn"), ts)
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "第 1 步"
            call_id = str(data.get("callId") or "")
            cell = calls.get(call_id)
            if cell is None:
                name = str(data.get("name") or "")
                cell = {"kind": "tool", "recordId": call_id,
                        "text": name, "callId": call_id, "toolName": name,
                        "subagent": name in _SUBAGENT_TOOLS, "startedAt": ts}
                add(t, title, cell)
                calls[call_id] = cell
            text = str(data.get("result") or "")
            cell.update({
                "result": text, "outputDetail": text,
                "isError": not bool(data.get("ok", True)),
                "traceId": str(data.get("traceId") or ""),
                "resultChars": int(data.get("resultChars") or 0),
                "write": bool(data.get("write")),
                "errorText": str(data.get("error") or ""),
                "timeSeconds": (int(data.get("ms") or 0) / 1000.0
                                if data.get("ms") is not None else None),
            })
            name = str(cell.get("toolName") or data.get("name") or "")
            if name in _SUBAGENT_TOOLS:
                batch = str(data.get("batch") or "")
                for child in _subagent_cells(sid, batch, call_id):
                    add(t, title, child)
        elif typ == "subagent":
            t = ensure_turn(data.get("turn"), ts)
            step = int(data.get("step") or 0)
            title = ("第 %d 步" % step) if step else "第 1 步"
            call_id = str(data.get("callId") or "")
            cell = calls.get(call_id)
            batch = str(data.get("batch") or "")
            if cell is not None and batch:
                for child in _subagent_cells(sid, batch, call_id):
                    add(t, title, child)
        elif typ == "todo/write":
            t = ensure_turn(data.get("turn"), ts)
            plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
            steps = plan.get("steps") or []
            done = sum(1 for s in steps if isinstance(s, dict)
                       and s.get("status") == "done")
            add(t, "第 1 步" if not t["groups"] else t["groups"][-1]["title"], {
                "kind": "context",
                "text": "计划 v%s · %d 步 · %d 完成" % (
                    plan.get("revision") or 1, len(steps), done),
                "inputDetail": json.dumps(plan, ensure_ascii=False, default=str),
                "source": {"kind": "plan", "label": "计划"},
                "startedAt": ts,
            })
        elif typ == "compact":
            t = ensure_turn(data.get("turn"), ts)
            add(t, "输入", {
                "kind": "compacted",
                "text": "压缩历史：%s → %s 条，摘要 %s 字" % (
                    data.get("before") or 0, data.get("after") or 0,
                    data.get("chars") or 0),
                "startedAt": ts,
            })
    return _finalize(sid, turns, before=before, limit_turns=limit_turns)


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
