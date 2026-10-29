# -*- coding: utf-8 -*-
"""设置页「恢复默认」依赖的不变量：默认值可深拷贝、字段齐全、接口已注册。"""

from app.config import DEFAULT_CONFIG, default_config
from app.profile import DEFAULT_PROFILE


def test_default_config_is_deep_copied():
    a = default_config()
    a["chat_context_limit"] = 1
    a["chat_presets"]["x"] = {"url": "u"}

    b = default_config()
    assert b["chat_context_limit"] == DEFAULT_CONFIG["chat_context_limit"]
    assert b["chat_presets"] == {}


def test_default_config_covers_settings_pages():
    cfg = default_config()
    for key in ("chat_url", "chat_model", "chat_context_limit", "chat_think_mode",
                "chat_round_retries", "chat_stream_stall_timeout",
                "chat_persist_reasoning", "chat_reasoning_keep_chars",
                "gal_cps", "gal_theme", "speech_polish",
                "tts_provider", "ide_theme", "agent_max_undo"):
        assert key in cfg


def test_default_profile_shape():
    assert DEFAULT_PROFILE["manual"] == ""
    assert DEFAULT_PROFILE["auto_learn_enabled"] is True
    assert DEFAULT_PROFILE["learning_prompt_custom"] == ""


def test_settings_routes_registered():
    from app.registry import snapshot
    import app.api.misc  # noqa: F401  （导入即注册）

    routes = snapshot()
    for path in ("/api/settings/defaults", "/api/app/info", "/api/app/open-data-dir"):
        assert ("POST", path) in routes


def test_obsolete_keys_are_dropped_on_save(monkeypatch):
    """死配置保存时顺手清掉，避免前端回填旧 config 又写回来。"""
    from app import config

    saved = {}
    monkeypatch.setattr(config, "write_json", lambda path, data: saved.update(data))
    config.save_config({
        "chat_url": "https://example.com",
        "hall_ai_count": 2,
        "gal_auto_script_replan": True,
        "profile_inject_enabled": False,
        "chat_protocol": "openai",
        "search_rewrite": "x",
    })
    assert saved["chat_url"] == "https://example.com"
    for key in ("hall_ai_count", "gal_auto_script_replan",
                "profile_inject_enabled", "chat_protocol", "search_rewrite"):
        assert key not in saved
