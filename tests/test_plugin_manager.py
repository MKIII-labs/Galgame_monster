# -*- coding: utf-8 -*-
"""插件管理器：目录包扫描 / 安装 / 卸载 / 启停 / 内置源 / zip 安全 / MCP 摘要。"""
import json
import os
import zipfile

import pytest

import app.config as config_mod
from app import capabilities, plugin_manager as pm


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    builtin = tmp_path / "builtin"
    plugins.mkdir()
    builtin.mkdir()
    cfg = {"plugins_enabled": False, "plugins_disabled": [], "mcp_enabled": False,
           "mcp_servers": []}
    monkeypatch.setattr(pm, "PLUGIN_DIR", str(plugins))
    monkeypatch.setattr(pm, "BUILTIN_DIR", str(builtin))
    monkeypatch.setattr(pm, "CATALOG_FILE", str(builtin / "catalog.json"))
    monkeypatch.setattr(pm, "load_config", lambda: cfg)
    monkeypatch.setattr(pm, "save_config", lambda c: cfg.update(c))
    monkeypatch.setattr(pm, "_mcp_reset", lambda: None)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(capabilities, "PLUGIN_DIR", str(plugins))
    monkeypatch.setattr(capabilities, "_LOADED", {})
    return type("Env", (), {"plugins": plugins, "builtin": builtin,
                            "cfg": cfg, "tmp": tmp_path})()


def _pkg(root, pid, name="示例"):
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({
        "id": pid, "name": name, "version": "1.0.0",
        "description": "说明", "entry": "plugin.py",
    }), encoding="utf-8")
    (d / "plugin.py").write_text("# demo\n", encoding="utf-8")
    return d


def test_list_installed_package_and_file(env):
    _pkg(env.plugins, "alpha", "阿尔法")
    (env.plugins / "beta.py").write_text('"""贝塔插件"""\n', encoding="utf-8")
    rows = {p["id"]: p for p in pm.list_installed()["plugins"]}
    assert rows["alpha"]["kind"] == "package"
    assert rows["alpha"]["name"] == "阿尔法"
    assert rows["alpha"]["status"] == "pending"
    assert rows["beta"]["kind"] == "file"
    assert rows["beta"]["description"] == "贝塔插件"
    assert rows["beta"]["enabled"] is True


def test_install_dir_and_collision(env):
    src = _pkg(env.tmp / "src", "alpha", "阿尔法")
    pid, err = pm.install_dir(str(src))
    assert err == "" and pid == "alpha"
    assert (env.plugins / "alpha" / "plugin.py").is_file()
    # 同名不能覆盖
    assert pm.install_dir(str(src))[1] != ""


def test_install_requires_entry(env):
    src = _pkg(env.tmp / "src", "alpha")
    (src / "plugin.py").unlink()
    assert pm.install_dir(str(src))[1] != ""


def test_uninstall(env):
    pm.install_dir(str(_pkg(env.tmp / "src", "alpha")))
    assert pm.uninstall("alpha") == ""
    assert not (env.plugins / "alpha").exists()
    assert pm.uninstall("alpha") != ""


def test_set_enabled_and_master(env):
    _pkg(env.plugins, "alpha")
    assert pm.set_enabled("alpha", False) == ""
    assert env.cfg["plugins_disabled"] == ["alpha"]
    assert pm.list_installed()["plugins"][0]["status"] == "disabled"
    assert pm.set_enabled("alpha", True) == ""
    assert env.cfg["plugins_disabled"] == []
    pm.set_master(True)
    assert env.cfg["plugins_enabled"] is True


def test_install_zip_and_slip(env):
    src = _pkg(env.tmp / "src", "alpha")
    zpath = env.tmp / "alpha.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for name in ("plugin.json", "plugin.py"):
            zf.write(src / name, "alpha/" + name)
    pid, err = pm.install_zip(str(zpath))
    assert err == "" and pid == "alpha"

    bad = env.tmp / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    assert pm.install_zip(str(bad))[1] != ""


def test_catalog_and_builtin_install(env):
    _pkg(env.builtin, "demo", "演示")
    (env.builtin / "catalog.json").write_text(json.dumps({
        "items": [{"id": "demo", "name": "演示", "description": "d"}],
    }), encoding="utf-8")
    cat = pm.catalog()
    assert len(cat) == 1 and cat[0]["installed"] is False
    pid, err = pm.install_builtin("demo")
    assert err == "" and pid == "demo"
    assert pm.catalog()[0]["installed"] is True


def test_plugin_entries_reports_bad_manifest(env):
    _pkg(env.plugins, "alpha")
    bad = env.plugins / "broken"
    bad.mkdir()
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")
    entries = {e[0]: e for e in capabilities.plugin_entries()}
    assert entries["alpha"][2] == ""
    assert entries["broken"][2] != ""


def test_capabilities_loads_dir_plugin_and_skips_disabled(env, monkeypatch):
    code = ("from app.capabilities import capability\n"
            "@capability(%r, 'test', {})\n"
            "def _h(args, ctx):\n    return 'ok'\n")
    for pid, cid in (("plugtest_on", "plugtest_on"), ("plugtest_off", "plugtest_off")):
        d = _pkg(env.plugins, pid, pid)
        (d / "plugin.py").write_text(code % cid, encoding="utf-8")
    env.cfg["plugins_enabled"] = True
    env.cfg["plugins_disabled"] = ["plugtest_off"]
    monkeypatch.setattr(capabilities, "_PLUGINS", {"loaded": False})
    ids = capabilities.ids()
    assert "plugtest_on" in ids
    assert "plugtest_off" not in ids
    loaded = capabilities.loaded_plugins()
    assert loaded["plugtest_on"]["ok"] is True
    assert loaded["plugtest_off"]["error"] == "已停用"


def test_mcp_summary_and_toggles(env):
    env.cfg["mcp_servers"] = [
        {"name": "fs", "transport": "stdio", "command": "npx", "enabled": True},
        {"name": "api", "transport": "http", "url": "https://x/mcp"},
    ]
    out = pm.mcp_summary()
    assert out["enabled"] is False
    assert [s["name"] for s in out["servers"]] == ["fs", "api"]
    assert pm.mcp_set_enabled("fs", False) == ""
    assert env.cfg["mcp_servers"][0]["enabled"] is False
    assert pm.mcp_set_master(True) == ""
    assert env.cfg["mcp_enabled"] is True
    assert pm.mcp_set_enabled("nope", True) != ""
