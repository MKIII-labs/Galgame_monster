# -*- coding: utf-8 -*-
"""Computer Use · 策略层：地址解析 / 站点授权 / 内置禁区 / 自身界面"""
import pytest

from app.computer import policy


def _cfg(sites=None, blocks=None):
    """替身：只返回策略关心的字段。"""
    return {"computer_sites": sites or [], "computer_extra_block": blocks or []}


# ---------------------------------------------------------------- 地址解析
def test_split_http_and_port():
    info = policy._split("https://example.com:8443/a/b?x=1")
    assert info["scheme"] == "https" and info["host"] == "example.com" and info["port"] == 8443
    assert info["path"] == "/a/b?x=1"


def test_split_rejects_credentials_and_weird_schemes():
    assert policy._split("https://user:pass@example.com/") is None
    assert policy._split("javascript:alert(1)")["scheme"] == "javascript"
    assert policy._split("data:text/html,x")["scheme"] == "data"
    assert policy._split("") is None


# ---------------------------------------------------------------- 内置禁区
def test_sensitive_hosts_denied(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg())
    for url in ("https://mail.qq.com/", "https://www.alipay.com/",
                "https://mybank.icbc.com.cn/", "https://1password.com/"):
        decision, why = policy.check(url)
        assert decision == "deny", url
        assert "内置禁区" in why


def test_sensitive_host_does_not_match_other_domains(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg())
    assert policy.check("https://notmail.qq.com.cn/")[0] == "ask"


# ---------------------------------------------------------------- 自身界面
def test_own_port_denied(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg(sites=["*"]))
    monkeypatch.setattr(policy, "own_port", lambda: 1234)
    decision, why = policy.check("http://127.0.0.1:1234/settings")
    assert decision == "deny" and "本程序" in why
    # 别的本机端口放行（本地开发服务器是常见场景）
    assert policy.check("http://127.0.0.1:3000/")[0] == "allow"


# ---------------------------------------------------------------- 白名单与会话授权
def test_allowlist_wildcards(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg(sites=["example.com", "*.foo.com"]))
    assert policy.check("https://example.com/x")[0] == "allow"
    assert policy.check("https://a.foo.com/")[0] == "allow"
    assert policy.check("https://foo.com/")[0] == "allow"
    assert policy.check("https://a.b.foo.com/")[0] == "allow"
    assert policy.check("https://evilfoo.com/")[0] == "ask"


def test_extra_block_wins_over_allowlist(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg(sites=["*.example.com"],
                                                    blocks=["secret.example.com"]))
    assert policy.check("https://secret.example.com/")[0] == "deny"
    assert policy.check("https://ok.example.com/")[0] == "allow"


def test_session_grant(monkeypatch):
    monkeypatch.setattr(policy, "_cfg", lambda: _cfg())
    assert policy.check("https://新站点.com/")[0] == "ask"
    policy.grant_session("新站点.com")
    try:
        assert policy.check("https://新站点.com/")[0] == "allow"
    finally:
        policy.clear_session()
    assert policy.check("https://新站点.com/")[0] == "ask"


# ---------------------------------------------------------------- file://
def test_file_requires_sandbox_and_html(tmp_path):
    page = tmp_path / "a.html"
    page.write_text("<html></html>", encoding="utf-8")
    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    assert policy.check(policy.url_of_file(page), None)[0] == "deny"
    assert policy.check(policy.url_of_file(page), str(tmp_path))[0] == "ask"
    assert policy.check(policy.url_of_file(txt), str(tmp_path))[0] == "deny"


def test_file_outside_sandbox_denied(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "evil.html"
    outside.write_text("<html></html>", encoding="utf-8")
    assert policy.check(policy.url_of_file(outside), str(box))[0] == "deny"


def test_file_inside_web_dir_denied(tmp_path, monkeypatch):
    from app.store import ROOT
    import os
    web_page = os.path.join(ROOT, "web", "index.html")
    if not os.path.isfile(web_page):
        pytest.skip("没有 web/index.html")
    assert policy.check(policy.url_of_file(web_page), ROOT)[0] == "deny"


def test_unsupported_scheme():
    assert policy.check("ftp://example.com/x")[0] == "deny"


def test_host_of():
    assert policy.host_of("https://a.example.com:8080/x") == "a.example.com"
    assert policy.host_of("file:///D:/a.html") == ""
