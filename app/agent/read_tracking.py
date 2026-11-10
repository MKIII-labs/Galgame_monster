# -*- coding: utf-8 -*-

import os
import threading
import time
from app import tool_protocol



_READ_STATE = {}                 # (沙箱根, 绝对路径) -> (mtime_ns, size)


_READ_STATE_MAX = 4096           # 进程内最多记多少条，长跑进程不会无限涨
# 「本回合已经把这个文件的全文给了模型」：同会话 + 同回合 + 版本未变时不再回灌。
# 与 _READ_STATE 分开：那个是「写前必须读过」的守卫，进程级；这个只用于省 token，
# 必须按会话/回合隔离，否则新会话会误以为模型已经看过内容。


_READ_SEEN = {}                  # (sid, 沙箱根, 绝对路径) -> (版本戳, turn_key)


_READ_SEEN_MAX = 2048


_read_lock = threading.Lock()
# 只有「覆盖已有内容」才需要先读；新建文件、建目录、删除都不拦（与 dsh 一致）


_READ_BEFORE_WRITE = ("write_file", "edit_file")

# 插件注册的工具（data/plugins/*.py 用 @agent_tool 注册）：name -> 元数据。
# 与内置工具分开存，TOOLS 仍是一份稳定的元组，插件加载失败不会影响内置工具。


def _file_ver(p):
    """文件的版本戳；不存在 / 拿不到 stat 时返回 None。"""
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _remember_read(sandbox, p):
    """记下「这个文件本会话读过了，版本是 X」。"""
    from .sandbox import _real
    ver = _file_ver(p)
    if ver is None:
        return
    key = (_real(sandbox), p)       # 沙箱根用 realpath，避免尾斜杠 / 大小写造成两套键
    with _read_lock:
        _READ_STATE.pop(key, None)          # 重新插入，保证「最近使用」排在最后
        _READ_STATE[key] = ver
        while len(_READ_STATE) > _READ_STATE_MAX:
            _READ_STATE.pop(next(iter(_READ_STATE)))


def _turn_key(caps):
    """本回合的标识：同一回合内重复读同一文件才允许省略正文。"""
    return str((caps or {}).get("turn_key") or "")


def _seen_key(sandbox, p, caps):
    from .sandbox import _real
    return (str((caps or {}).get("sid") or ""), _real(sandbox), p)


def _note_seen(sandbox, p, caps):
    """记下「这个文件的全文已经给过模型了（本回合内有效）」。"""
    tk = _turn_key(caps)
    if not tk:
        return
    ver = _file_ver(p)
    if ver is None:
        return
    key = _seen_key(sandbox, p, caps)
    with _read_lock:
        _READ_SEEN.pop(key, None)
        _READ_SEEN[key] = (ver, tk)
        while len(_READ_SEEN) > _READ_SEEN_MAX:
            _READ_SEEN.pop(next(iter(_READ_SEEN)))


def _repeat_read_stub(sandbox, args, caps, allow_outside=False):
    """同会话同回合、文件版本没变、且上次给过全文 → 只回一行，不再回灌全文。

    只要模型显式带了 start / limit / raw，就当作「要别的区间」，照常执行。
    返回空串表示照常读取。
    """
    from .io_utils import _fmt_size
    from .sandbox import (SandboxError, _rel, resolve_path)
    try:
        if not (caps or {}).get("read_dedupe", True):
            return ""
        if args.get("start") or args.get("limit") or args.get("raw"):
            return ""
        tk = _turn_key(caps)
        if not tk:
            return ""
        p = resolve_path(sandbox, args.get("path"), allow_outside, for_read=True)
        if not os.path.isfile(p):
            return ""
        ver = _file_ver(p)
        if ver is None:
            return ""
        key = _seen_key(sandbox, p, caps)
        with _read_lock:
            known = _READ_SEEN.get(key)
            if known != (ver, tk):
                return ""
            _READ_SEEN.pop(key, None)       # 刷新 LRU
            _READ_SEEN[key] = known
        _remember_read(sandbox, p)           # 顺带刷新「写前读」守卫的 LRU
        return ("文件未变化：%s（%s，本回合已读过，正文仍在上下文里）。"
                "要看别的区间请带 start / limit；要无行号原文请加 raw=true。"
                % (_rel(sandbox, p), _fmt_size(ver[1])))
    except (SandboxError, OSError, TypeError, ValueError):
        return ""


def _check_read_before_write(sandbox, name, args):
    """覆盖已有文件前必须读过；读过但文件变了则要求重读。返回错误文本或空串。"""
    from .sandbox import (SandboxError, _real, _rel, resolve_path)
    if name not in _READ_BEFORE_WRITE:
        return ""
    try:
        p = resolve_path(sandbox, args.get("path"))
    except SandboxError:
        return ""                 # 路径越界交给工具自己报，别在这里抢答
    if not os.path.isfile(p):
        return ""                 # 新建 / 目标是目录：不拦
    with _read_lock:
        known = _READ_STATE.get((_real(sandbox), p))
    rel = _rel(sandbox, p)
    if known is None:
        return tool_protocol.error(
            tool_protocol.READ_REQUIRED,
            "目标文件已存在，但本会话还没读过它：%s" % rel,
            name=name, args=args)
    if known != _file_ver(p):
        return tool_protocol.error(
            tool_protocol.STALE_VERSION,
            "文件在读取后已变化：%s" % rel,
            name=name, args=args)
    return ""


def turn_begin(sid):
    """每轮对话开始时调用：清空该会话本轮已登记的 token。"""
    from .base import MAX_TURNS
    from .state import (_TURNS, _turns_lock)
    with _turns_lock:
        _TURNS[sid] = {"tokens": [], "ts": time.time()}
        if len(_TURNS) > MAX_TURNS:
            oldest = sorted(_TURNS.items(), key=lambda kv: kv[1]["ts"])[0][0]
            if oldest != sid:
                _TURNS.pop(oldest, None)


def turn_note(sid, token):
    """把一次写操作的 undo token 记到本轮（供「回滚本轮」反向重放）。"""
    from .state import (_TURNS, _turns_lock)
    if not token:
        return
    with _turns_lock:
        item = _TURNS.get(sid)
        if item is not None:
            item["tokens"].append(token)
