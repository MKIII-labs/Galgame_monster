# -*- coding: utf-8 -*-
"""秘密文件护栏：读不到、搜不到、命令点名也跑不了。"""
import json

from app import agent


def test_is_secret_path_patterns():
    assert agent.is_secret_path(".env")
    assert agent.is_secret_path(".env.local")
    assert agent.is_secret_path("config/.env")
    assert agent.is_secret_path("certs/server.pem")
    assert agent.is_secret_path("keys/id_rsa")
    assert agent.is_secret_path("C:/Users/x/.ssh/config")
    assert agent.is_secret_path("credentials.json")
    assert agent.is_secret_path("id_rsa.pub") == ""      # 公钥允许读
    assert agent.is_secret_path("app/main.py") == ""
    assert agent.is_secret_path("config/env.txt") == ""


def test_read_file_denies_secret_and_rules_can_allow(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    text, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": ".env"})
    assert text.lstrip().startswith("❌") and "密钥" in text

    gm = tmp_path / ".gm"
    gm.mkdir()
    (gm / "agent-rules.json").write_text(json.dumps({"allow": [".env"]}),
                                         encoding="utf-8")
    text2, _u2, _w2 = agent.run_tool(str(tmp_path), "read_file", {"path": ".env"})
    assert "SECRET=1" in text2


def test_search_files_skips_secret_content(tmp_path):
    (tmp_path / ".env").write_text("TOKEN_XYZ=1", encoding="utf-8")
    (tmp_path / "a.txt").write_text("TOKEN_XYZ=2", encoding="utf-8")
    text, _u, _w = agent.run_tool(str(tmp_path), "search_files",
                                  {"pattern": "TOKEN_XYZ"})
    assert "a.txt" in text
    assert ".env" not in text


def test_run_command_blocks_secret_argument(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    text, _u, _w = agent.run_tool(str(tmp_path), "run_command",
                                  {"argv": ["python", ".env"]}, unrestricted=True)
    assert text.lstrip().startswith("❌") and "密钥" in text
