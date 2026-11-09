# -*- coding: utf-8 -*-
"""工具结果落盘（spill）：超长结果存文件，只回灌头尾 + 一个跨轮有效的 locator。

为什么需要：读整个文件 / 跑测试的输出可能几万到几十万字。全塞进上下文会让后续每一轮都
重传它；只给头尾又会让模型为了看细节重新读一遍。落盘后模型可以按字符区间取回，
既省上下文，又不用重跑工具。

与请求内句柄（`r1` / `r2`）的区别：那些只活一次请求，这里落盘的在重启后仍然有效。
"""
import json
import os
import re
import threading
import time
import uuid

from app.store import DATA_DIR

_LOCK = threading.Lock()
_LOC_RE = re.compile(r"^sp_[0-9a-f]{10}$")

DEFAULT_MAX = 50000          # 超过这个字符数才落盘
DEFAULT_KEEP = 400           # 最多保留多少个 spill 文件
DEFAULT_MB = 256             # 落盘总量上限（MB）


def _dir(data_dir=None):
    d = os.path.join(data_dir or DATA_DIR, "spills")
    os.makedirs(d, exist_ok=True)
    return d


def _paths(locator, data_dir=None):
    d = _dir(data_dir)
    return os.path.join(d, locator + ".txt"), os.path.join(d, locator + ".json")


def valid(locator):
    return bool(_LOC_RE.match(str(locator or "").strip()))


def save(session_id, tool, text, data_dir=None, keep=DEFAULT_KEEP, max_mb=DEFAULT_MB):
    """把一份工具结果落盘，返回 locator（形如 sp_1a2b3c4d5e）。

    写失败时返回空串，调用方应当退回到「头尾截断」路径。
    """
    try:
        body = str(text or "")
        loc = "sp_" + uuid.uuid4().hex[:10]
        p, m = _paths(loc, data_dir)
        with _LOCK:
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            with open(m, "w", encoding="utf-8") as f:
                json.dump({"tool": str(tool or ""), "chars": len(body),
                           "session": str(session_id or ""), "ts": time.time()},
                          f, ensure_ascii=False)
        _gc(data_dir, keep, max_mb)
        return loc
    except Exception as e:
        print("[spill] 落盘失败（退回头尾截断）：%s" % e)
        return ""


def load(locator, data_dir=None):
    """按 locator 取回全文；不存在 / 非法返回 None。"""
    loc = str(locator or "").strip()
    if not valid(loc):
        return None
    try:
        p, _m = _paths(loc, data_dir)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def meta(locator, data_dir=None):
    loc = str(locator or "").strip()
    if not valid(loc):
        return None
    try:
        _p, m = _paths(loc, data_dir)
        with open(m, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find_offsets(text, query, regex=False, ignore_case=False, context=2, max_hits=20):
    """在超长工具结果里按行搜索，返回 (命中总数, 命中块列表)。

    为什么按行：长文本 + 用户给的模式做整段正则可能灾难性回溯；逐行匹配天然有行号，
    也和 grep 的心智一致。每块带绝对字符偏移，模型可直接用 read_tool_result 的
    start/end 精确精读。
    """
    body = str(text or "")
    q = str(query or "")
    if not body or not q:
        return 0, []
    if len(q) > 200:
        q = q[:200]
    try:
        ctx = max(0, min(int(context or 0), 5))
    except (TypeError, ValueError):
        ctx = 2
    try:
        cap = max(1, min(int(max_hits or 0) or 20, 50))
    except (TypeError, ValueError):
        cap = 20
    pat = None
    if regex:
        try:
            pat = re.compile(q, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ValueError("正则非法：%s" % e)
    needle = q.lower() if ignore_case else q
    lines = body.splitlines(keepends=True)
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln)
    hits, total = [], 0
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        pos = -1
        if pat is not None:
            m = pat.search(line)
            pos = m.start() if m else -1
        else:
            hay = line.lower() if ignore_case else line
            pos = hay.find(needle)
        if pos < 0:
            continue
        total += 1
        if len(hits) >= cap:
            continue
        hits.append({
            "line": i + 1,
            "offset": starts[i] + pos,
            "text": line.strip()[:300],
            "before": [x.rstrip("\r\n").strip()[:200] for x in lines[max(0, i - ctx):i]],
            "after": [x.rstrip("\r\n").strip()[:200]
                      for x in lines[i + 1:i + 1 + ctx]],
        })
    return total, hits


_KEY_RE = re.compile(
    r"(?i)\b(error|exception|traceback|failed|failure|fatal|panic|"
    r"assertionerror|errno|warning|cannot|denied|not found|超时|失败|错误)\b")


def key_lines(text, limit=20, max_chars=200):
    """挑出「结论最可能在的那几行」（报错 / 失败 / 警告），给落盘预览用。

    为什么需要：长结果的头部通常是命令回显、进度条，结论在中间或尾部。
    只给头尾会让模型重新跑一遍命令；给几条关键行往往就够了。
    只做行级正则筛选，不解释内容，也不改变原文。
    """
    try:
        cap = max(1, min(int(limit or 20), 60))
    except (TypeError, ValueError):
        cap = 20
    out, seen = [], set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        if not _KEY_RE.search(line):
            continue
        seen.add(line)
        out.append(line[:max_chars])
        if len(out) >= cap:
            break
    return out


def _gc(data_dir, keep, max_mb):
    """按最旧优先清理：文件数超过 keep 或总量超过 max_mb 时删掉最老的。"""
    if keep <= 0 and max_mb <= 0:
        return
    d = _dir(data_dir)
    entries, total = [], 0
    try:
        names = [n for n in os.listdir(d) if n.endswith(".txt")]
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    entries.sort()
    while entries and ((keep > 0 and len(entries) > keep)
                       or (max_mb > 0 and total > max_mb * 1024 * 1024)):
        _mt, size, p = entries.pop(0)
        for x in (p, p[:-4] + ".json"):
            try:
                os.remove(x)
            except OSError:
                pass
        total -= size
