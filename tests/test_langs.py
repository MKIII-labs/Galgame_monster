# -*- coding: utf-8 -*-
"""跨语言：项目探测、语法自检、LSP 降级与符号搜索。"""
from app import agent
from app import langs


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    info = langs.detect(str(tmp_path))
    assert "Python" in info["languages"]
    assert info["package_manager"] == "uv"
    assert any("pytest" in x for x in info["test"])
    assert "语言" in langs.profile_line(str(tmp_path))


def test_detect_node(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"vitest","build":"vite build"}}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    info = langs.detect(str(tmp_path))
    assert "Node" in info["languages"]
    assert info["package_manager"] == "pnpm"
    assert "pnpm test" in info["test"]
    assert "pnpm run build" in info["build"]


def test_detect_go_and_rust(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert "Go" in langs.detect(str(tmp_path))["languages"]
    other = tmp_path / "rust"
    other.mkdir()
    (other / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    info = langs.detect(str(other))
    assert "Rust" in info["languages"]
    assert "cargo test" in info["test"]


def test_syntax_check_python_and_json(tmp_path):
    bad = tmp_path / "a.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    assert langs.syntax_check(str(bad))
    good = tmp_path / "b.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert langs.syntax_check(str(good)) == ""
    broken = tmp_path / "c.json"
    broken.write_text('{"a":', encoding="utf-8")
    assert langs.syntax_check(str(broken))


def test_lsp_kind_and_fallback(tmp_path):
    assert langs.lsp_kind("a.py") == "python"
    assert langs.lsp_kind("a.go") == ""
    assert "暂无内置 LSP" in langs.lsp_fallback("a.go")


def test_find_symbol_works_for_go(tmp_path):
    (tmp_path / "main.go").write_text(
        "package main\nfunc BuildThing() {}\n", encoding="utf-8")
    text, _undo = agent._t_find_symbol(str(tmp_path), {"name": "BuildThing"})
    assert "main.go" in text and "定义 1 处" in text


def test_check_code_covers_go(tmp_path):
    (tmp_path / "main.go").write_text("package main\nfunc main() {\n", encoding="utf-8")
    text, _undo = agent._t_check_code(str(tmp_path), {})
    # 没装 gofmt 时静默跳过，但文件必须被纳入检查范围
    assert "检查了 1 个文件" in text or "main.go" in text


def test_command_whitelist_allows_polyglot(tmp_path):
    for argv in (["go", "test", "./..."], ["cargo", "build"],
                 ["mvn", "-q", "test"], ["dotnet", "build"], ["gcc", "-c", "a.c"]):
        _argv, err = agent._cmd_argv({"argv": argv}, str(tmp_path))
        assert not err, (argv, err)
    for argv in (["bash", "-lc", "echo hi"], ["curl", "http://x"]):
        _argv, err = agent._cmd_argv({"argv": argv}, str(tmp_path))
        assert err
