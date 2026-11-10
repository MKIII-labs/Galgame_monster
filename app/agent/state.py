# -*- coding: utf-8 -*-

import threading



_LOADED_SIDS = set()            # 已经从磁盘并回内存的会话（避免重复加载）


def _ensure_loaded(sid):
    """第一次碰到某个会话时，把磁盘上的撤销栈并回内存。"""
    from .undo import _UNDO
    if not sid or sid in _LOADED_SIDS:
        return
    _LOADED_SIDS.add(sid)
    try:
        from app import snapstore
        for token, item in snapstore.load(sid).items():
            if token not in _UNDO:
                item["sid"] = sid
                _UNDO[token] = item
    except Exception:                    # noqa: BLE001
        pass


_TURNS = {}                 # sid -> {"tokens": [...], "ts": float}


_turns_lock = threading.Lock()
