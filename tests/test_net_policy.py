# -*- coding: utf-8 -*-
"""网络域名白名单（agent_net_allow）的匹配与工具拦截。"""
import app.agent.net_policy as np
from app.agent.tools_web import _t_download_file, _t_fetch_url


def test_empty_allowlist_means_no_limit():
    assert np.check_host("anything.example.com", {"agent_net_allow": []}) == ""
    assert np.check_host("anything.example.com", {}) == ""


def test_exact_and_subdomain():
    conf = {"agent_net_allow": ["github.com"]}
    assert np.check_host("github.com", conf) == ""
    assert np.check_host("api.github.com", conf) == ""
    # raw.githubusercontent.com 是另一个域，不应被 github.com 放行
    assert "raw.githubusercontent.com" in np.check_host("raw.githubusercontent.com", conf)


def test_lookalike_domain_not_allowed():
    conf = {"agent_net_allow": ["github.com"]}
    assert np.check_host("github.com.evil.com", conf) != ""
    assert np.check_host("notgithub.com", conf) != ""


def test_entries_normalized():
    conf = {"agent_net_allow": ["*.GitHub.com", "https://raw.githubusercontent.com/a/b"]}
    allow = np.allowlist(conf)
    assert "github.com" in allow
    assert "raw.githubusercontent.com" in allow


def test_string_config_split():
    conf = {"agent_net_allow": "a.com\nb.com, c.com"}
    assert np.allowlist(conf) == ["a.com", "b.com", "c.com"]


def test_fetch_url_blocked_by_allowlist(monkeypatch):
    monkeypatch.setattr(np, "load_config", lambda: {"agent_net_allow": ["example.com"]})
    text, _undo = _t_fetch_url(".", {
        "url": "https://raw.githubusercontent.com/shadcn/improve/main/README.md"})
    assert text.startswith("❌")
    assert "白名单" in text


def test_download_file_blocked_by_allowlist(monkeypatch):
    monkeypatch.setattr(np, "load_config", lambda: {"agent_net_allow": ["example.com"]})
    text, undo = _t_download_file(".", {
        "url": "https://raw.githubusercontent.com/shadcn/improve/main/README.md",
        "path": "README.md"})
    assert text.startswith("❌")
    assert "白名单" in text
    assert undo is None
