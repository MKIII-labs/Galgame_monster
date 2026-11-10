# -*- coding: utf-8 -*-

import os
import threading
import time
import uuid
from app.config import load_config



MAX_UNDO = 60               # 每个会话保留多少条「往期」撤销快照（本轮登记的永不淘汰）
# 以前的坑：这是全局 20 条 FIFO —— 一个长回合写几十次文件，就会把「变更审阅」里
# 前几个文件的 baseToken 挤掉（表现为「查看变更」报「快照已过期」），
# 别的会话写入也会把你的条目整批挤出去。


_UNDO = {}


_undo_lock = threading.Lock()


SNAP_AFTER_MAX = 1024 * 1024    # 单个文件「写后快照」上限（redo 用）

# 全局写锁：主循环与子代理（explore 并发）可能同时改同一个文件 ——
# A 读到旧内容、B 也读到旧内容，先后写回去时后写的会冲掉前一个的改动，且不报错。
# 写操作（WRITE_TOOLS / GIT_WRITE_TOOLS）以及 IDE 的「全局替换 / 重命名」都过这把锁；
# 用 RLock：工具实现内部再触发写也不会自锁。


def _snap(path):
    """记录文件原状，供撤销用"""
    from .sandbox import SandboxError
    if os.path.isdir(path):
        return {"kind": "restore_dir", "path": path, "existed": True}
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                return {"kind": "restore", "path": path, "existed": True, "data": f.read()}
        except OSError as e:
            raise SandboxError("无法读取原文件，已放弃修改：" + str(e))
    return {"kind": "restore", "path": path, "existed": False, "data": b""}


def preview_undo(sid, tokens=None, scope=None, root=""):
    """预览一次回滚会改到哪些文件（只读，不落盘）。

    与 undo() 的执行顺序一致：token 从后往前、每个 token 的 ops 也从后往前；
    同一路径第一次命中的状态就是最终状态（后面的操作先被撤销）。
    """
    from .io_utils import (_preview_display, _preview_paths)
    from .state import (_TURNS, _turns_lock)
    ensure_undo_loaded(sid)
    picked = list(tokens or [])
    if scope == "turn":
        with _turns_lock:
            item = _TURNS.get(sid)
            picked = list(item.get("tokens") or []) if item else []
    files, seen = [], set()
    skipped = 0
    for token in reversed(picked):
        with _undo_lock:
            item = _UNDO.get(token)
        if not item or item.get("undone"):
            skipped += 1                 # 已撤销 / 快照过期：实际回滚也会跳过
            continue
        for op in reversed(item.get("ops") or []):
            for p, action, kind in _preview_paths(op):
                key = os.path.normcase(os.path.abspath(p))
                if key in seen:
                    continue
                seen.add(key)
                files.append({"path": _preview_display(p, root),
                              "action": action, "kind": kind})
    return {"ok": True, "files": files, "skipped": skipped, "total": len(picked)}


def list_steps(sid):
    """结构化版本（供前端步骤时间线），按时间正序。"""
    from .state import _ensure_loaded
    _ensure_loaded(sid)
    with _undo_lock:
        items = [(t, v) for t, v in _UNDO.items()
                 if sid and v.get("sid") == sid]
    items.sort(key=lambda kv: kv[1].get("ts") or 0)
    out = []
    for token, item in items:
        files = []
        for op in item.get("ops") or []:
            p = op.get("path")
            if p:
                files.append(p)
        out.append({"token": token,
                    "label": item.get("label") or "",
                    "ts": item.get("ts") or 0,
                    "undone": bool(item.get("undone")),
                    "files": files})
    return out


def _capture_after(ops):
    """写操作完成后记录「写后的样子」——redo 需要它，只有 before 是重做不了的。"""
    for op in ops or []:
        if op.get("kind") != "restore" or not op.get("path"):
            continue
        try:
            with open(op["path"], "rb") as f:
                op["after"] = f.read(SNAP_AFTER_MAX)
            op["after_existed"] = True
        except OSError:
            op["after"] = b""
            op["after_existed"] = False


def _persist_undo(sid):
    """把某个会话的撤销栈落盘（失败静默，不影响内存撤销）。"""
    if not sid:
        return
    try:
        from app import snapstore
        with _undo_lock:
            items = {t: v for t, v in _UNDO.items() if v.get("sid") == sid}
        snapstore.save(sid, items)
    except Exception:                    # noqa: BLE001  持久化绝不能拖垮写入
        pass


def ensure_undo_loaded(sid):
    """确保该会话的磁盘快照已并回内存（看 diff / 撤销 / 保留之前调用）。

    重启后内存是空的，而 _ensure_loaded 以前只在「写入」时触发 ——
    于是重启之后，看旧变更、撤销旧变更都会失败，磁盘上明明还有快照。
    """
    from .state import _ensure_loaded
    _ensure_loaded(sid)


def undo_limit(cfg=None):
    """每个会话保留多少条「往期」撤销快照（设置项 agent_max_undo）。

    0 = 不限；缺键 / 非数字 / 负数 → 默认常量。本轮登记的 token 不参与淘汰，
    所以这里管的是「跨回合还能回看多远」。
    """
    from .base import _limit_or_default
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:                    # noqa: BLE001
            cfg = {}
    val = _limit_or_default((cfg or {}).get("agent_max_undo"), MAX_UNDO)
    if val < 0:
        return MAX_UNDO          # 负数没有意义，别悄悄当成「不限」
    return val                   # 0 = 不限


def _trim_undo(sid):
    """超出每会话上限时从最旧的开始淘汰，但**本轮登记的 token 永不淘汰**。

    「变更审阅」用的是每个文件首次写入的 baseToken：只要它被挤掉，
    面板就会显示「快照已过期」。所以本轮的凭据必须留下，
    代价是极端长的回合会暂时超出上限（宁可多占一点内存，也不能让审阅失效）。
    """
    from .state import (_TURNS, _turns_lock)
    limit = undo_limit()
    if limit <= 0:
        return                             # 0 = 不限
    with _turns_lock:
        turn = _TURNS.get(sid)
        protected = set(turn.get("tokens") or []) if turn else set()
    with _undo_lock:
        mine = [t for t, v in _UNDO.items() if v.get("sid") == sid]
        while len(mine) > limit:
            victim = next((t for t in mine if t not in protected), None)
            if victim is None:
                break
            _UNDO.pop(victim, None)
            mine.remove(victim)


def register_undo(sid, undo_ops, label):
    """把一次写操作的撤销信息登记进内存，返回 token"""
    from .read_tracking import turn_note
    from .state import _ensure_loaded
    if not undo_ops:
        return None
    _ensure_loaded(sid)
    _capture_after(undo_ops)
    token = "a_" + uuid.uuid4().hex[:10]
    with _undo_lock:
        _UNDO[token] = {"sid": sid, "ops": undo_ops, "label": label,
                        "ts": time.time(), "undone": False}
    turn_note(sid, token)            # 先记进本轮：本轮 token 不参与淘汰
    _trim_undo(sid)
    _persist_undo(sid)
    return token


def undo_old_content(token, abs_path):
    """取某次写操作前的文件内容（供 IDE 的 diff 预览）。

    返回 None 表示拿不到快照：token 不存在（已被撤销 / 超出 20 条上限），
    或这次操作没有改到这个文件。只读，不影响撤销本身。
    """
    target = os.path.normcase(os.path.abspath(abs_path))
    with _undo_lock:
        item = _UNDO.get(token)
        if not item:
            return None
        ops = list(item.get("ops") or [])
    for op in ops:
        if op.get("kind") != "restore":
            continue
        p = op.get("path")
        if not p:
            continue
        if os.path.normcase(os.path.abspath(p)) != target:
            continue
        data = op.get("data") or b""
        if isinstance(data, str):
            return data
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "replace")
    return None


def undo(token):
    """撤回一次写操作：按相反顺序还原文件。

    不再 pop：保留条目并标记 undone，才能支持 redo；
    重复调用同一条会返回 False，不会二次还原。
    """
    from .sandbox import invalidate_tree
    with _undo_lock:
        item = _UNDO.get(token)
        if not item or item.get("undone"):
            return False
        item["undone"] = True
    for op in reversed(item.get("ops") or []):
        kind = op.get("kind")
        try:
            if kind == "restore":
                if op.get("existed"):
                    parent = os.path.dirname(op["path"])
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(op["path"], "wb") as f:
                        f.write(op.get("data") or b"")
                elif os.path.exists(op["path"]):
                    os.remove(op["path"])
            elif kind == "restore_dir":
                if not os.path.exists(op["path"]):
                    os.makedirs(op["path"], exist_ok=True)
            elif kind == "rmdir":
                if os.path.isdir(op["path"]) and not os.listdir(op["path"]):
                    os.rmdir(op["path"])
            elif kind == "mkdir":
                if not os.path.exists(op["path"]):
                    os.makedirs(op["path"], exist_ok=True)
            elif kind == "move_back":
                if os.path.exists(op["dst"]) and not os.path.exists(op["src"]):
                    parent = os.path.dirname(op["src"])
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    os.replace(op["dst"], op["src"])
        except OSError:
            pass
    invalidate_tree()                        # 撤销可能重建 / 删除文件
    _persist_undo(item.get("sid"))
    return True


def redo(token):
    """把一次已撤销的写操作重新落盘（需要写后快照）。"""
    from .sandbox import invalidate_tree
    with _undo_lock:
        item = _UNDO.get(token)
        if not item or not item.get("undone"):
            return False
        ops = list(item.get("ops") or [])
    touched = False
    for op in ops:
        if op.get("kind") != "restore" or "after" not in op:
            continue
        p = op.get("path")
        if not p:
            continue
        try:
            if op.get("after_existed"):
                parent = os.path.dirname(p)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(p, "wb") as f:
                    f.write(op.get("after") or b"")
            elif os.path.exists(p):
                os.remove(p)
            touched = True
        except OSError:
            pass
    if not touched:
        return False
    with _undo_lock:
        item["undone"] = False
    invalidate_tree()
    _persist_undo(item.get("sid"))
    return True


def undo_tokens(tokens):
    """按相反顺序批量撤销（一轮里对同一文件的多步修改）。

    返回成功撤销的条数；已过期 / 已被接纳的 token 会计为失败。
    """
    done = 0
    for t in reversed(list(tokens or [])):
        if undo(t):
            done += 1
    return done


# ============================================================
# 3.2 整轮检查点：把同一轮里的写操作串起来，支持「回滚本轮」
# ============================================================


def turn_undo(sid):
    """按相反顺序撤销本轮所有写操作。返回 (成功条数, 失败条数)。"""
    from .state import (_TURNS, _turns_lock)
    with _turns_lock:
        item = _TURNS.get(sid)
        tokens = list(item["tokens"]) if item else []
        if item is not None:
            item["tokens"] = []
    if not tokens:
        return 0, 0
    ok = fail = 0
    for token in reversed(tokens):
        if undo(token):
            ok += 1
        else:
            fail += 1                        # token 过期（超过 MAX_UNDO）或已撤过
    return ok, fail


# ============================================================
# 3.3 写入审批：需要用户点头时挂起工具调用，等前端决定
# ============================================================


_WRITE_LABEL = {
    "write_file": "新建或覆盖文件",
    "edit_file": "修改已有文件",
    "make_dir": "新建目录",
    "move_file": "移动或重命名",
    "delete_file": "删除文件",
    "lsp_rename": "语义重命名（可能改多个文件）",
    "recover_file": "从写后快照恢复文件",
}
