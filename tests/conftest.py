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
def _no_upload_sweep(monkeypatch):
    """测试一律关闭启动附件清理。

    live 用例会把 SESSION_FILE 指到临时文件再调 create_server；启动清理读到临时
    会话后，会把真实 data/uploads 下所有目录当成孤儿删掉（2026-11-09 实际踩过，
    多轮图片附件丢失）。GM_NO_SWEEP 是 uploads.sweep_uploads 的硬开关。
    """
    monkeypatch.setenv("GM_NO_SWEEP", "1")


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
def _no_hybrid_by_default(monkeypatch):
    """测试默认按「纯 API」跑（hybrid_mode=off）。

    本地混合 / 纯本地模式会改变压缩策略与工具挂载，且依赖本机是否装了 Ollama；
    不能让 data/config.json 里的开关影响测试结果。需要测混合模式的用例
    自己 monkeypatch `load_config` 或直接传 cfg。
    """
    import sys

    try:
        from app import config as cfg_mod
    except Exception:                       # 模块不可用时无需隔离
        return
    real = cfg_mod.load_config

    def _off(*args, **kwargs):
        cfg = real(*args, **kwargs)
        cfg["hybrid_mode"] = "off"
        return cfg

    # 各模块是 `from app.config import load_config` 早绑定的，要逐个替换引用
    for name, mod in list(sys.modules.items()):
        if not name.startswith("app"):
            continue
        if getattr(mod, "load_config", None) is real:
            monkeypatch.setattr(mod, "load_config", _off)
    monkeypatch.setattr(cfg_mod, "load_config", _off)


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
def _isolate_aside_store(tmp_path, monkeypatch):
    """旁注线程写到 tmp：否则测试建的标签页会留在真实 data/aside.json。"""
    try:
        from app import aside_store
    except Exception:                       # 模块不可用时无需隔离
        return
    monkeypatch.setattr(aside_store, "ASIDE_FILE", str(tmp_path / "aside.json"))


@pytest.fixture(autouse=True)
def _isolate_share_dir(tmp_path, monkeypatch):
    """分享页写到 tmp：否则测试生成的 HTML 会留在真实 data/shares。"""
    try:
        from app import share
    except Exception:                       # 模块不可用时无需隔离
        return
    monkeypatch.setattr(share, "SHARE_DIR", str(tmp_path / "shares"))


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
