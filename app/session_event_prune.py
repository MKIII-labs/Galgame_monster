# -*- coding: utf-8 -*-
"""事件日志清理：把已被快照覆盖的历史事件压成一条基线。

背景：`data/sessions/<sid>/events.jsonl` 是 append-only 的旁路日志，用于崩溃对账；
2026-11 实测 89 个会话合计 160MB（最大的单个 55MB，含 events.1.jsonl）。这些事件里
绝大多数内容早已写进 sessions.json / 单会话快照，启动时也只读文件尾部 64KB 判断新旧，
属于纯冗余。

做法：为每个会话写入一条 `session/baseline` 事件（携带当前完整快照，与
`session_events.record()` 首次落库时用的格式一致），再只保留 ts 晚于快照的增量事件。
`session_events.rebuild()` 因此仍能还原完整会话，而历史体积基本清零。

安全线：
- 默认只读预览；`--apply` 才写回，且先备份到 `data/event_prune_backup/<时间戳>/`
- 应用运行时拒绝写回（进程内 `_LAST` 基线会让后续 diff 错位，--force 可强行）
- 查不到快照的会话直接跳过，不删任何东西
- 写回前在内存里重放新事件流校验能重建出会话，校验不过就跳过
"""
import json
import os
import shutil

from app import session_events as se
from app import sessions as sm

# 小于这个体积的文件不值得重写（改写要整份读写，收益太低）
MIN_BYTES = 64 * 1024
# 新文件至少要比旧文件小这么多才动手，避免「压完没省多少」的无效写入
MIN_SAVING = 32 * 1024
BASELINE_TYPE = "session/baseline"


def _float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _rotated_of(path):
    return path[: -len(".jsonl")] + ".1.jsonl"


def _store_dir(data_dir):
    """单会话快照目录：跟 sessions.json 同目录（测试把 SESSION_FILE 指到临时目录时也隔离）。"""
    return os.path.join(data_dir or os.path.dirname(sm.SESSION_FILE), sm.STORE_DIRNAME)


def _load_snapshots(data, data_dir=None):
    """返回 {sid: (快照, 基线时间)}：sessions.json 里的会话 + 更新的单会话快照。"""
    out = {}
    agg_at = _float(data.get("saved_at"))
    for s in data.get("sessions") or []:
        if isinstance(s, dict) and s.get("id"):
            out[str(s["id"])] = (s, agg_at)
    store = _store_dir(data_dir)
    try:
        names = os.listdir(store)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(store, name), "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(snap, dict) or not snap.get("id"):
            continue
        sid = str(snap["id"])
        ts = _float(snap.get("saved_at"))
        cur = out.get(sid)
        if cur is None or ts > cur[1]:
            out[sid] = (snap, ts)
    return out


def _read_events(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue          # 崩溃留下的半行：与 rebuild 一致，跳过
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def _tail_ts(events):
    for evt in reversed(events):
        ts = _float(evt.get("ts"))
        if ts:
            return ts
    return 0.0


def _replay(events):
    """在内存里按 session_events 的规则重放，用于写回前自检。"""
    s, messages = {}, []
    for evt in events:
        se._apply(evt, s, messages)
    s["messages"] = messages
    return s


def _verify(new_events, snap, tail):
    """写回前自检：新事件流必须能重建出会话，且不丢快照里的正文。"""
    try:
        got = _replay(new_events)
    except Exception:                 # noqa: BLE001  重建失败就别写
        return False
    if not got.get("id"):
        return False
    want = [m.get("content") for m in snap.get("messages") or []]
    have = [m.get("content") for m in got.get("messages") or []]
    if not tail:
        # 没有增量时，重建结果必须与快照逐条一致
        return have == want
    # 有增量：快照期间的消息必须还在（增量可能继续往最后一条追加正文，
    # 因此只要求条数不减少、保留下来的消息以快照正文开头）
    if len(have) < len(want):
        return False
    for i, old in enumerate(want):
        new = have[i] or ""
        old = old or ""
        if new != old and not new.startswith(old):
            return False
    return True


def inspect_session(sid, snap, base_ts, data_dir=None):
    """算出一个会话的清理方案（不改文件）。返回 None 表示无需处理。"""
    path = se.path_of(sid, data_dir)
    rotated = _rotated_of(path)
    files = [p for p in (rotated, path) if os.path.isfile(p)]
    if not files:
        return None
    before = sum(os.path.getsize(p) for p in files)
    if before < MIN_BYTES:
        return None
    events = []
    for p in files:
        events.extend(_read_events(p))
    if not events:
        return None
    tail = [e for e in events if _float(e.get("ts")) > base_ts]
    new_events = [{"ts": 0, "seq": 0, "type": BASELINE_TYPE, "session": snap}] + tail
    if not _verify(new_events, snap, tail):
        return {"sid": sid, "title": snap.get("title") or "", "before": before,
                "after": before, "files": files, "tail": len(tail),
                "events": len(events), "action": "skip", "reason": "自检未通过",
                "data_dir": data_dir}
    blob = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in new_events)
    after = len(blob.encode("utf-8"))
    if before - after < MIN_SAVING:
        return None
    return {"sid": sid, "title": snap.get("title") or "", "before": before,
            "after": after, "files": files, "tail": len(tail),
            "events": len(events), "action": "rewrite", "reason": "",
            "data_dir": data_dir, "_blob": blob}


def plan(data, data_dir=None):
    """扫描全部会话，返回 (待清理, 跳过, 统计)。不改文件。"""
    snaps = _load_snapshots(data, data_dir)
    rows = []
    for sid, (snap, base_ts) in snaps.items():
        row = inspect_session(sid, snap, base_ts, data_dir)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r["before"], reverse=True)
    skipped = [r for r in rows if r["action"] == "skip"]
    todo = [r for r in rows if r["action"] == "rewrite"]
    stats = {
        "sessions": len(snaps),
        "candidates": len(todo),
        "skipped": len(skipped),
        "before": sum(r["before"] for r in todo),
        "after": sum(r["after"] for r in todo),
        "events": sum(r["events"] for r in todo),
        "tail": sum(r["tail"] for r in todo),
    }
    return todo, skipped, stats


def _sessions_root(data_dir=None):
    return os.path.join(data_dir or se.DATA_DIR, "sessions")


def scan_orphans(data, data_dir=None):
    """找出「已不在 sessions.json」的事件目录（历史删除留下的残留）。

    返回 (可删, 保留)。只有最后一条事件早于 sessions.json 的 saved_at 才算可删：
    应用可能在内存里持有一个还没写进 sessions.json 的新会话，那种目录必须留着。
    """
    snaps = _load_snapshots(data, data_dir)
    agg_at = _float(data.get("saved_at"))
    root = _sessions_root(data_dir)
    todo, keep = [], []
    try:
        names = os.listdir(root)
    except OSError:
        return todo, keep
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name in snaps:
            continue
        path = os.path.join(d, "events.jsonl")
        rotated = os.path.join(d, "events.1.jsonl")
        files = [p for p in (rotated, path) if os.path.isfile(p)]
        if not files:
            continue
        size = sum(os.path.getsize(p) for p in files)
        latest = max(_tail_ts(_read_events(p)) for p in files)
        row = {"sid": name, "dir": d, "before": size, "latest": latest, "files": files}
        if latest > agg_at:
            keep.append(row)
        else:
            todo.append(row)
    todo.sort(key=lambda r: r["before"], reverse=True)
    return todo, keep


def apply_orphan(row, backup_dir):
    """把残留事件目录整体移到备份目录（同盘移动，不复制大文件）。"""
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, row["sid"])
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.move(row["dir"], dst)
    return True


def apply_row(row, backup_dir):
    """写回单个会话：先备份，再原子替换 events.jsonl，最后删掉滚动文件。"""
    blob = row.get("_blob")
    if blob is None:
        return False
    sid = row["sid"]
    path = se.path_of(sid, row.get("data_dir"))
    os.makedirs(backup_dir, exist_ok=True)
    for src in row["files"]:
        dst = os.path.join(backup_dir, sid + "-" + os.path.basename(src))
        with open(src, "rb") as f, open(dst, "wb") as g:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                g.write(chunk)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, path)
    for p in row["files"]:
        if os.path.abspath(p) != os.path.abspath(path):
            try:
                os.remove(p)
            except OSError:
                pass
    return True
