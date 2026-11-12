# -*- coding: utf-8 -*-

import threading



_LOADED_SIDS = set()            # 已经从磁盘并回内存的会话（避免重复加载）


def _ensure_loaded(sid):
    """第一次碰到某个会话时，把磁盘上的撤销栈并回内存。"""
    from .undo import _UNDO
    if not sid or sid in _LOADED_SIDS:
        return
    # 标记必须放在加载**成功之后**：以前是先 add 再 load，一旦 load 因瞬时 IO
    # 失败（文件被占用 / 杀软扫描）抛异常，异常被吞掉、标记却已置位，这个会话
    # 的撤销栈在本进程内就再也不会并回来 —— 表现为「数据在，界面说没有」。
    try:
        from app import snapstore
        items = snapstore.load(sid)
    except Exception as e:               # noqa: BLE001
        print("[撤销] 会话 %s 的撤销栈加载失败，下次访问重试：%s" % (sid, e))
        return
    _LOADED_SIDS.add(sid)
    for token, item in items.items():
        if token not in _UNDO:
            item["sid"] = sid
            _UNDO[token] = item


_TURNS = {}                 # sid -> {"tokens": [...], "ts": float}


_turns_lock = threading.Lock()
