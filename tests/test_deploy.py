# -*- coding: utf-8 -*-
"""deploy_site / app.deploy：本地静态发布。"""
import urllib.error
import urllib.request

import pytest

from app import agent, deploy, store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path / "data"))
    deploy._SITES.clear()
    deploy.stop_all()
    deploy._SERVERS.clear()
    yield
    deploy.stop_all()
    deploy._SITES.clear()


def _site(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    return root


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def test_publish_serves_index_and_skips_secrets(tmp_path):
    _site(tmp_path)
    text, undo, _ = agent.run_tool(str(tmp_path), "deploy_site", {"path": "web", "name": "demo"})
    assert "已发布" in text and "GM_PREVIEW_URL:" in text
    rows = deploy.list_sites()
    assert len(rows) == 1
    site = rows[0]
    assert site["name"] == "demo" and site["running"]
    if not site.get("lan_url"):
        # 无网卡的 CI 环境只校验本机地址
        assert site["url"].startswith("http://127.0.0.1:")
    code, body = _get(site["url"])
    assert code == 200 and "hello" in body
    # 隐藏文件与 node_modules 不进发布目录
    with pytest.raises(urllib.error.HTTPError):
        _get(site["url"] + ".env")
    with pytest.raises(urllib.error.HTTPError):
        _get(site["url"] + "node_modules/x.js")


def test_redeploy_reuses_site(tmp_path):
    root = _site(tmp_path)
    agent.run_tool(str(tmp_path), "deploy_site", {"path": "web"})
    (root / "index.html").write_text("<h1>v2</h1>", encoding="utf-8")
    agent.run_tool(str(tmp_path), "deploy_site", {"path": "web"})
    rows = deploy.list_sites()
    assert len(rows) == 1
    assert "v2" in _get(rows[0]["url"])[1]


def test_list_and_stop(tmp_path):
    _site(tmp_path)
    agent.run_tool(str(tmp_path), "deploy_site", {"path": "web"})
    text, _, _ = agent.run_tool(str(tmp_path), "deploy_site", {"action": "list"})
    assert "已发布站点" in text
    sid = deploy.list_sites()[0]["id"]
    text, _, _ = agent.run_tool(str(tmp_path), "deploy_site",
                             {"action": "stop", "site_id": sid})
    assert "已停止" in text
    assert not deploy.list_sites()[0]["running"]


def test_missing_dir_rejected(tmp_path):
    text, _, _ = agent.run_tool(str(tmp_path), "deploy_site", {"path": "nope"})
    assert text.startswith("❌")
