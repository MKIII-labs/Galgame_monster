# -*- coding: utf-8 -*-
"""完整工具结果 trace：会话级 JSONL，供事后复盘 / 审计。

为什么单独存：
- `sessions.json` 里的工具结果只保留前 500 字，事后无法还原模型当时看到了什么；
- trace 只追加、不进模型上下文，按会话滚动，失败静默，不影响主流程。
"""
import json
import os
import threading
import time
import uuid

from app.store import DATA_DIR

TRACE_DIR = os.path.join(DATA_DIR, "tool_trace")
MAX_BYTES = 32 * 1024 * 1024       # 单会话超过 32MB 滚动一份 .1.jsonl
_lock = threading.Lock()


def _safe_sid(sid):
    s = "".join(ch for ch in str(sid or "") if ch.isalnum() or ch in "-_")
    return s or "unknown"


def _path(sid):
    return os.path.join(TRACE_DIR, _safe_sid(sid) + ".jsonl")


def _old_path(sid):
    return _path(sid) + ".1"


def record(sid, tool, args, result, ok=True, ms=0):
    """追加一条完整结果，返回 trace_id；失败返回空串。"""
    text = str(result or "")
    tid = uuid.uuid4().hex[:16]
    entry = {"ts": time.time(), "trace_id": tid, "tool": str(tool or ""),
             "args": args if isinstance(args, dict) else {},
             "ok": bool(ok), "ms": int(ms or 0),
             "result": text, "result_chars": len(text)}
    try:
        with _lock:
            os.makedirs(TRACE_DIR, exist_ok=True)
            p = _path(sid)
            try:
                if os.path.getsize(p) >= MAX_BYTES:
                    old = _old_path(sid)
                    if os.path.exists(old):
                        os.remove(old)
                    os.replace(p, old)
            except OSError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return tid
    except Exception:
        return ""


def load(sid, trace_id):
    """按 trace_id 取回完整记录（当前文件 + 上一份滚动文件）。"""
    tid = str(trace_id or "")
    if not tid:
        return None
    for p in (_path(sid), _old_path(sid)):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("trace_id") == tid:
                        return d
        except OSError:
            continue
    return None
