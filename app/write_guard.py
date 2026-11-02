# -*- coding: utf-8 -*-
"""并行写隔离：批次内按路径登记写者，冲突时告警或拒绝。

全局写锁只保证「不并发写盘」；两个代理先后改同一文件时，后写者仍可能
在语义上覆盖前者的改动。这里给每次写入登记归属：

- serial（默认）：照常执行，结果里带「该文件刚由 X 修改」的提醒；
- strict：同一批次同一路径只允许第一个写者，后来的直接拒绝；
- off：不登记。

只在进程内生效（主循环 / 子代理 / 脚本都在同一进程里）。
"""
import os
import threading
import time

MAX_BATCHES = 64          # 最多保留多少个批次的写者表
RECENT_TTL = 120.0        # 「最近写者」的有效期（秒）

_LOCK = threading.Lock()
_OWNERS = {}              # batch -> {path: {"owner": str, "ts": float, "all": set}}
_RECENT = {}              # path -> {"owner": str, "ts": float}


def _norm(path):
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        return ""
    p = os.path.normpath(p).replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def paths_of(tool, args, sandbox=""):
    """这次写入会动到哪些路径（只取能确定的目标，目录类不参与冲突）。"""
    name = str(tool or "")
    args = args if isinstance(args, dict) else {}
    if name in ("make_dir", "asset_convert", "asset_inline"):
        return []
    pairs = []
    if name in ("move_file", "copy_file"):
        for k in ("from", "to", "src", "dst", "path", "source", "target"):
            v = args.get(k)
            if v:
                pairs.append(str(v))
    else:
        v = args.get("path") or args.get("file")
        if v:
            pairs.append(str(v))
    out = []
    for p in pairs:
        n = _norm(p)
        if n and n not in out:
            out.append(n)
    return out


def _gc(now):
    if len(_OWNERS) <= MAX_BATCHES:
        return
    rows = sorted(_OWNERS.items(),
                  key=lambda kv: max((r.get("ts") or 0) for r in kv[1].values())
                  if kv[1] else 0)
    for k, _ in rows[:max(0, len(_OWNERS) - MAX_BATCHES)]:
        _OWNERS.pop(k, None)


def claim(batch, path, owner, mode="serial"):
    """登记一次写入。

    返回 (ok, prev_owner, recent_owner)：
    - strict 模式下同一路径已被别人写过 → ok=False；
    - prev_owner 是**本批次**里的前一个写者（不含自己）；
    - recent_owner 是最近 RECENT_TTL 秒内写过该路径的别的代理（跨批次）。
    """
    mode = str(mode or "serial").strip().lower()
    rel = _norm(path)
    if mode == "off" or not batch or not rel:
        return True, "", ""
    owner = str(owner or "")
    now = time.time()
    with _LOCK:
        _gc(now)
        rows = _OWNERS.setdefault(batch, {})
        prev = rows.get(rel) or {}
        prev_owner = str(prev.get("owner") or "")
        recent = _RECENT.get(rel) or {}
        recent_owner = ""
        if recent and (now - float(recent.get("ts") or 0)) <= RECENT_TTL:
            recent_owner = str(recent.get("owner") or "")
        if mode == "strict" and prev_owner and prev_owner != owner:
            return False, prev_owner, recent_owner
        seen = set(prev.get("all") or ())
        seen.add(owner)
        rows[rel] = {"owner": owner, "ts": now, "all": seen}
        _RECENT[rel] = {"owner": owner, "ts": now}
        return True, (prev_owner if prev_owner and prev_owner != owner else ""), \
            (recent_owner if recent_owner and recent_owner != owner else "")


def conflicts(batch):
    """同一批次里被多个代理写过的路径：[(path, [owner...]), ...]。"""
    with _LOCK:
        rows = dict(_OWNERS.get(str(batch or "")) or {})
    out = []
    for path, rec in rows.items():
        owners = sorted(set(rec.get("all") or ()))
        if len(owners) > 1:
            out.append((path, owners))
    return sorted(out, key=lambda x: x[0])


def forget(batch):
    """批次结束后释放写者表（可选的清理，防止长期占用）。"""
    with _LOCK:
        _OWNERS.pop(str(batch or ""), None)
