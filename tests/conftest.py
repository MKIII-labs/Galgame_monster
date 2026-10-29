# -*- coding: utf-8 -*-
"""pytest 公共配置：把项目根目录挂到 sys.path，tests/ 直接 import app.*"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# 测试里可能调用 create_server：禁止自动恢复真实目标（避免误跑 / 误联网）
os.environ.setdefault("GALGAME_NO_AUTORESUME", "1")


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """测试不碰真实的 data/sessions 事件目录。

    - 事件写到 tmp：`save_sessions` 在测试里也会调 record_all；
    - 默认按 json 模式读取：不触发启动对账（对账会扫描真实事件目录）。
      需要测事件语义的用例自己 monkeypatch `_store_mode`。
    """
    try:
        from app import session_events as se
        from app import sessions
    except Exception:                       # 模块不可用时无需隔离
        return
    monkeypatch.setattr(se, "DATA_DIR", str(tmp_path / "events"))
    se._LAST.clear()
    monkeypatch.setattr(sessions, "_store_mode", lambda: "json")
    sessions.reset_merge_flag()
    yield
    se._LAST.clear()
    sessions.reset_merge_flag()


@pytest.fixture(autouse=True)
def _isolate_research(tmp_path, monkeypatch):
    """研究台账写到 tmp：否则测试里的来源 / 缺口会留在真实 data/research。"""
    try:
        from app import research
    except Exception:
        return
    monkeypatch.setattr(research, "DATA_DIR", str(tmp_path))
    research._CACHE.clear()
    yield
    research._CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_tool_trace(tmp_path, monkeypatch):
    """工具 trace 写到 tmp：否则测试会把完整结果留在真实 data/tool_trace。"""
    try:
        from app import tool_trace
    except Exception:
        return
    monkeypatch.setattr(tool_trace, "TRACE_DIR", str(tmp_path / "tool_trace"))


@pytest.fixture(autouse=True)
def _isolate_observations(request, monkeypatch):
    """除观测层自身的测试外，禁止测试往真实数据库写观测。

    数学竞技场在 answer / grade_paper 里会落观测；跑测试时如果不拦住，
    几百道测试题会写进用户的 data/assistant.db。
    """
    if getattr(request.module, "__name__", "") == "test_observe":
        return
    try:
        from app import observe
    except Exception:                       # 观测模块不可用时无需拦截
        return
    monkeypatch.setattr(observe, "record", lambda **kw: False)
    monkeypatch.setattr(observe, "record_many", lambda items=None: False)
