# -*- coding: utf-8 -*-
"""设置中心：校验、保存、历史、回滚与旧配置迁移。"""
import json

import pytest

from app import settings_center as sc


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    from app import config

    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    monkeypatch.setattr(sc, "CONFIG_FILE", str(path))
    monkeypatch.setattr(sc, "HISTORY_DIR", str(tmp_path / "history"))
    return path


def _write_cfg(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_cfg(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_validate_patch_accepts_valid_and_unknown(cfg_env):
    assert sc.validate_patch({"chat_round_retries": 15, "settings_level": "basic"}) == []
    assert sc.validate_patch({"unknown_future_key": {"a": 1}}) == []
    # 兼容旧客户端：布尔 / 数字用字符串传
    assert sc.validate_patch({"ui_process_follow": "false"}) == []
    assert sc.validate_patch({"chat_round_retries": "15"}) == []


def test_validate_patch_reports_range_choice_and_type(cfg_env):
    errors = sc.validate_patch({"chat_round_retries": 999})
    assert errors and errors[0]["key"] == "chat_round_retries"

    errors = sc.validate_patch({"settings_level": "root"})
    assert errors and errors[0]["key"] == "settings_level"

    errors = sc.validate_patch({"ui_process_follow": "maybe"})
    assert errors and errors[0]["key"] == "ui_process_follow"

    errors = sc.validate_patch({"chat_round_retries": "abc"})
    assert errors and errors[0]["key"] == "chat_round_retries"


def test_validate_patch_accepts_all_default_keys(cfg_env):
    """默认配置里的每个键都必须能通过校验（新增设置项不会把老配置卡死）。"""
    from app.config import default_config
    assert sc.validate_patch(default_config()) == []


def test_unknown_keys_survive_normal_save(cfg_env):
    _write_cfg(cfg_env, {"unknown_vendor_key": {"x": 1}, "chat_round_retries": 6})
    assert sc.save_patch({"chat_round_retries": 15})["ok"] is True
    cfg = _read_cfg(cfg_env)
    assert cfg["unknown_vendor_key"] == {"x": 1}
    assert cfg["chat_round_retries"] == 15


def test_history_redacts_nested_preset_keys(cfg_env):
    """chat_presets 这类嵌套结构里的 api_key 也不能进历史。"""
    _write_cfg(cfg_env, {"chat_presets": {"p": {"api_key": "sk-secret", "model": "m"}}})
    hid = sc.save_patch({"chat_round_retries": 15})["history_id"]
    snap = sc.load_version(hid)
    assert snap["config"]["chat_presets"]["p"]["api_key"] == "***"
    assert snap["config"]["chat_presets"]["p"]["model"] == "m"


def test_rollback_restores_nested_current_secret(cfg_env):
    _write_cfg(cfg_env, {"chat_presets": {"p": {"api_key": "old"}}, "chat_round_retries": 6})
    hid = sc.save_patch({"chat_round_retries": 15})["history_id"]
    sc.save_patch({"chat_presets": {"p": {"api_key": "new"}}})

    assert sc.rollback(hid)["ok"] is True
    cfg = _read_cfg(cfg_env)
    assert cfg["chat_round_retries"] == 6
    assert cfg["chat_presets"]["p"]["api_key"] == "new"    # 当前密钥不被历史覆盖


def test_save_patch_writes_config_and_history(cfg_env):
    _write_cfg(cfg_env, {"chat_api_key": "secret-1"})
    res = sc.save_patch({"chat_round_retries": 15})
    assert res["ok"] is True
    assert res["changed"] == ["chat_round_retries"]
    assert _read_cfg(cfg_env)["chat_round_retries"] == 15

    items = sc.history()
    assert len(items) == 1
    assert items[0]["changed"] == ["chat_round_retries"]
    snap = sc.load_version(items[0]["id"])
    assert snap["config"]["chat_round_retries"] == sc.DEFAULT_CONFIG["chat_round_retries"]   # 保存前的值
    assert snap["config"]["chat_api_key"] == "***"                    # 历史里脱敏


def test_save_patch_rejects_invalid_without_writing(cfg_env):
    _write_cfg(cfg_env, {"chat_round_retries": 20})
    res = sc.save_patch({"chat_round_retries": 999})
    assert res["ok"] is False and res["errors"]
    assert _read_cfg(cfg_env)["chat_round_retries"] == 20
    assert sc.history() == []


def test_no_history_without_change(cfg_env):
    _write_cfg(cfg_env, {"chat_round_retries": 20})
    res = sc.save_patch({"chat_round_retries": 20})
    assert res["ok"] is True and res["history_id"] == ""
    assert sc.history() == []


def test_history_is_retained_to_limit(cfg_env, monkeypatch):
    monkeypatch.setattr(sc, "HISTORY_LIMIT", 3)
    _write_cfg(cfg_env, {"chat_round_retries": 6})
    for i in range(5):
        assert sc.save_patch({"chat_round_retries": 10 + i})["ok"] is True
    items = sc.history(limit=10)
    assert len(items) == 3


def test_diff_redacts_secrets(cfg_env):
    _write_cfg(cfg_env, {"chat_api_key": "secret-1", "chat_round_retries": 6})
    hid = sc.save_patch({"chat_round_retries": 15})["history_id"]
    data = sc.diff(hid)
    assert data and data["changed"]
    row = next(x for x in data["changed"] if x["key"] == "chat_round_retries")
    assert row["before"] == 6 and row["after"] == 15


def test_rollback_keeps_current_secrets(cfg_env):
    _write_cfg(cfg_env, {"chat_api_key": "secret-1", "chat_round_retries": 6})
    first = sc.save_patch({"chat_round_retries": 15})["history_id"]
    sc.save_patch({"chat_api_key": "secret-2", "chat_round_retries": 18})

    res = sc.rollback(first)
    assert res["ok"] is True
    cfg = _read_cfg(cfg_env)
    assert cfg["chat_round_retries"] == 6                 # 回到保存前
    assert cfg["chat_api_key"] == "secret-2"    # 密钥不被历史覆盖

    # 回滚本身也留下一条可撤销记录
    assert sc.history()


def test_rollback_unknown_version(cfg_env):
    res = sc.rollback("not-exist")
    assert res["ok"] is False and res["errors"]


def test_ensure_migrated_backs_up_and_preserves_unknown_keys(cfg_env):
    _write_cfg(cfg_env, {"chat_url": "https://x", "deepseek_api_key": "legacy",
                         "unknown_key": 1})
    res = sc.ensure_migrated()
    assert res["migrated"] is True and res["backup"]
    backed = json.loads(open(res["backup"], "r", encoding="utf-8").read())
    assert backed["unknown_key"] == 1 and backed["chat_url"] == "https://x"

    cfg = _read_cfg(cfg_env)
    assert cfg["config_version"] == sc.SETTINGS_VERSION
    assert cfg["unknown_key"] == 1
    assert cfg["chat_api_key"] == "legacy"      # 旧键已改名
    assert cfg["settings_level"] == "advanced"  # 默认值已补齐

    again = sc.ensure_migrated()
    assert again["migrated"] is False


def test_ensure_migrated_creates_missing_config(cfg_env):
    res = sc.ensure_migrated()
    assert res["created"] is True
    assert _read_cfg(cfg_env)["config_version"] == sc.SETTINGS_VERSION


def test_api_settings_roundtrip(cfg_env):
    from app.api import settings_center as api
    from app.registry import get as get_route

    assert get_route("POST", "/api/settings") is not None

    class H:
        def __init__(self, body):
            self.body = body

        def _body(self):
            return self.body

        def _json(self, obj, code=200):
            obj = dict(obj)
            obj["_code"] = code
            return obj

    _write_cfg(cfg_env, {"chat_round_retries": 6})
    saved = api._api_settings(H({"op": "save", "patch": {"chat_round_retries": 16}}))
    assert saved["ok"] is True and saved["_code"] == 200

    bad = api._api_settings(H({"op": "save", "patch": {"chat_round_retries": 999}}))
    assert bad["ok"] is False and bad["_code"] == 400 and bad["errors"]

    hist = api._api_settings(H({"op": "history"}))
    assert hist["ok"] and hist["items"]

    diff = api._api_settings(H({"op": "diff", "id": hist["items"][0]["id"]}))
    assert diff["ok"] and diff["diff"]["changed"]

    back = api._api_settings(H({"op": "rollback", "id": hist["items"][0]["id"]}))
    assert back["ok"] and back["config"]["chat_round_retries"] == 6

    cur = api._api_settings(H({"op": "current"}))
    assert cur["ok"] and cur["config"]["chat_round_retries"] == 6


def test_api_config_uses_settings_center(cfg_env):
    from app.api import misc

    class H:
        def __init__(self, body):
            self.body = body

        def _body(self):
            return self.body

        def _json(self, obj, code=200):
            obj = dict(obj)
            obj["_code"] = code
            return obj

    _write_cfg(cfg_env, {"chat_round_retries": 6})
    ok = misc._api_save_config(H({"chat_round_retries": 15}))
    assert ok["ok"] is True and ok["_code"] == 200 and ok["changed"] == ["chat_round_retries"]

    bad = misc._api_save_config(H({"chat_round_retries": 999}))
    assert bad["ok"] is False and bad["_code"] == 400
    assert _read_cfg(cfg_env)["chat_round_retries"] == 15
