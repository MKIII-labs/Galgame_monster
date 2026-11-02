# -*- coding: utf-8 -*-
"""工具协议 v2：错误码 / 恢复指令 / 写前读 / 版本冲突 / 参数校验。"""
from app import agent
from app import tool_protocol as tp


# ---------------------------------------------------------------- 错误码
def test_code_of_legacy_text():
    assert tp.code_of("❌ 沙箱拒绝：路径越出沙箱范围") == tp.SANDBOX_DENIED
    assert tp.code_of("文件不存在：a.py") == tp.NOT_FOUND
    assert tp.code_of("old 在多处出现（2 处），请给出更长的唯一片段") == tp.AMBIGUOUS
    assert tp.code_of("目标已存在，拒绝覆盖：b.py") == tp.CONFLICT
    assert tp.code_of("命令执行超时（60 秒）") == tp.TIMEOUT


def test_error_text_is_structured_and_keeps_cross_prefix():
    text = tp.error(tp.READ_REQUIRED, "还没读过 a.py", name="write_file",
                    args={"path": "a.py", "content": "x" * 500})
    assert text.startswith("❌ 【工具错误】READ_REQUIRED")
    assert "原因：" in text and "恢复：" in text
    assert "read_file" in text
    # 长内容（文件正文）不塞进错误，避免失败时反而撑大上下文
    assert "重试参数：" in text and "xxx" not in text
    assert '"path": "a.py"' in text


def test_brief_args_limits():
    assert tp.brief_args({}) == ""
    assert tp.brief_args({"path": "a.py", "content": "y" * 400}) == '{"path": "a.py"}'
    assert tp.brief_args({"n": 1, "flag": True}) == '{"n": 1, "flag": true}'


def test_wrap_only_touches_errors():
    ok = "已创建 a.py（1 行，3 字节）"
    assert tp.wrap(ok) == ok
    bad = tp.wrap("文件不存在：a.py", name="edit_file", args={"path": "a.py"})
    assert bad.startswith("❌ 【工具错误】NOT_FOUND")
    assert tp.is_error(bad)


# ---------------------------------------------------------------- 参数校验
def test_missing_required_arg(tmp_path):
    text, undo, write = agent.run_tool(str(tmp_path), "read_file", {})
    assert text.startswith("❌ 【工具错误】INVALID_ARG")
    assert "path" in text
    assert undo is None and write is False


def test_run_command_accepts_argv_or_command(tmp_path):
    text, _u, _w = agent.run_tool(str(tmp_path), "run_command", {})
    assert text.startswith("❌ 【工具错误】INVALID_ARG")
    assert "argv" in text


def test_unknown_tool_is_coded(tmp_path):
    text, _u, _w = agent.run_tool(str(tmp_path), "no_such_tool", {})
    assert text.startswith("❌ 【工具错误】NOT_FOUND")
    assert "未知工具" in text


# ---------------------------------------------------------------- 写前读
def test_overwrite_existing_requires_read(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("old\n", encoding="utf-8")
    text, undo, write = agent.run_tool(
        str(tmp_path), "write_file", {"path": "a.txt", "content": "new\n"})
    assert text.startswith("❌ 【工具错误】READ_REQUIRED")
    assert undo is None and write is False
    assert p.read_text(encoding="utf-8") == "old\n"


def test_read_then_write_passes(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("old\n", encoding="utf-8")
    read_text, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.txt"})
    assert "old" in read_text
    text, undo, write = agent.run_tool(
        str(tmp_path), "write_file", {"path": "a.txt", "content": "new\n"})
    assert not tp.is_error(text)
    assert write and undo
    assert p.read_text(encoding="utf-8") == "new\n"


def test_new_file_needs_no_read(tmp_path):
    text, undo, write = agent.run_tool(
        str(tmp_path), "write_file", {"path": "fresh.txt", "content": "hi\n"})
    assert text.startswith("已创建")
    assert write and undo
    assert "【工具错误】" not in text


def test_external_change_makes_version_stale(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("v1\n", encoding="utf-8")
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.txt"})
    p.write_text("v2-longer\n", encoding="utf-8")       # 会话外改动：size 变化
    text, undo, _w = agent.run_tool(
        str(tmp_path), "write_file", {"path": "a.txt", "content": "v3\n"})
    assert text.startswith("❌ 【工具错误】STALE_VERSION")
    assert undo is None
    assert p.read_text(encoding="utf-8") == "v2-longer\n"


def test_edit_requires_read(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    text, undo, _w = agent.run_tool(
        str(tmp_path), "edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"})
    assert text.startswith("❌ 【工具错误】READ_REQUIRED")
    assert undo is None
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"})
    text2, undo2, _w2 = agent.run_tool(
        str(tmp_path), "edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"})
    assert not tp.is_error(text2)
    assert undo2
    assert p.read_text(encoding="utf-8") == "x = 2\n"


def test_require_read_can_be_disabled(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("old\n", encoding="utf-8")
    text, _u, write = agent.run_tool(
        str(tmp_path), "write_file", {"path": "a.txt", "content": "new\n"},
        caps={"require_read": False})
    assert not tp.is_error(text)
    assert write
    assert p.read_text(encoding="utf-8") == "new\n"


# ---------------------------------------------------------------- schema / 配置 / 提示词
def test_native_schema_is_strict_and_mentions_read():
    schemas = agent.native_tool_schemas(agent_on=True)
    by_name = {s["function"]["name"]: s["function"] for s in schemas}
    for fn in by_name.values():
        assert fn["parameters"]["additionalProperties"] is False
    assert "action=" in by_name["edit"]["description"]
    assert "action=" in by_name["read"]["description"]


def test_limits_require_read():
    assert agent.limits({})["require_read"] is True
    assert agent.limits({"agent_require_read": False})["require_read"] is False
