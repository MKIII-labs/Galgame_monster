# -*- coding: utf-8 -*-
"""工具执行日志：记录「准备执行」与「执行完成」，用于崩溃后判断结果是否未知。

为什么单独放在 events.jsonl 之外：

- 会话事件流记录的是「消息变化」，工具执行到一半崩溃时消息还没变化，
  事件流里看不到任何痕迹；
- 这里在工具真正执行前先落一条 intent，执行后再落 outcome；
- 重启时 intent 没有对应 outcome 的，就是 `TOOL_OUTCOME_UNKNOWN`，
  必须让模型先核实外部状态，不能盲目重试有副作用的操作。

只用于本地恢复，不进入模型上下文；写失败不影响主流程。
"""
import hashlib
import json
import os
import threading
import time

from app.store import DATA_DIR

_LOCK = threading.Lock()
_SID_RE = __import__("re").compile(r"[^A-Za-z0-9_-]")


def _safe_sid(sid):
    return _SID_RE.sub("_", str(sid or "").strip())[:80]


def path_of(sid, data_dir=None):
    base = os.path.join(data_dir or DATA_DIR, "sessions", _safe_sid(sid))
    return os.path.join(base, "tool_journal.jsonl")


def _append(sid, obj, data_dir=None):
    try:
        p = path_of(sid, data_dir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[工具日志] 写入失败（不影响主流程）：%s" % e)


def _args_hash(args):
    try:
        raw = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        raw = str(args)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def side_effecting(tool):
    """这个工具是否可能改变外部状态。读类工具不算。"""
    try:
        from app import agent
        return (tool in agent.WRITE_TOOLS or tool in agent.RUN_TOOLS
                or tool in agent.SHELL_TOOLS
                or tool in agent.GIT_WRITE_TOOLS)
    except Exception:
        return tool in ("write_file", "edit_file", "make_dir", "move_file",
                        "copy_file", "delete_file", "run_command", "shell",
                        "git_commit", "split_commit",
                        "recover_file", "lsp_rename")


def intent(sid, call_id, tool, args=None, data_dir=None):
    """工具执行前调用。call_id 为空时用时间戳生成，保证同一次执行能配对。"""
    cid = str(call_id or "").strip() or ("c_%d" % time.time_ns())
    _append(sid, {
        "ts": time.time(),
        "type": "intent",
        "call_id": cid,
        "tool": str(tool or ""),
        "args_hash": _args_hash(args),
        "side_effecting": bool(side_effecting(tool)),
    }, data_dir)
    return cid


def outcome(sid, call_id, ok=True, error="", result_chars=0, data_dir=None):
    _append(sid, {
        "ts": time.time(),
        "type": "outcome",
        "call_id": str(call_id or ""),
        "ok": bool(ok),
        "error": str(error or "")[:500],
        "result_chars": int(result_chars or 0),
    }, data_dir)


def load(sid, data_dir=None):
    """读回本会话的工具日志；坏行跳过，不影响恢复。"""
    p = path_of(sid, data_dir)
    out = []
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
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        pass
    return out


def pending(sid, data_dir=None, side_effect_only=True):
    """返回「有 intent、没有 outcome」的调用。

    side_effect_only=True 时只返回可能有副作用的调用：读类工具即使结果丢了，
    重试也不会造成外部影响，没必要打扰模型。
    """
    intents = {}
    done = set()
    for item in load(sid, data_dir):
        typ = item.get("type")
        cid = str(item.get("call_id") or "")
        if typ == "intent" and cid:
            intents[cid] = item
        elif typ == "outcome" and cid:
            done.add(cid)
    out = []
    for cid, item in intents.items():
        if cid in done:
            continue
        if side_effect_only and not item.get("side_effecting"):
            continue
        out.append(item)
    out.sort(key=lambda x: float(x.get("ts") or 0))
    return out


def pending_text(sid, data_dir=None):
    """给模型看的一段恢复提示；没有未知结果时返回空串。"""
    items = pending(sid, data_dir)
    if not items:
        return ""
    names = []
    for it in items[:8]:
        names.append("%s（%s）" % (it.get("tool") or "工具",
                                    it.get("call_id") or "?"))
    more = "" if len(items) <= 8 else "，另有 %d 个" % (len(items) - 8)
    return (
        "【工具结果未知】上次运行在以下工具执行期间中断，无法确认是否已经生效：\n"
        + "\n".join("- " + n for n in names) + more + "\n"
        "这些调用可能有副作用。不要直接重试；先读文件 / 查状态 / 看输出，"
        "确认外部状态后再决定是否重做。"
    )
