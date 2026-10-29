# -*- coding: utf-8 -*-
"""被抽出的模块级函数：**实际调用一次**，防止「自动搬移漏 import → 调用时才 NameError」。
（import 期不会暴露这类问题，必须真调用。）"""
import os

from app import export_md, llm, uploads, vscode


def test_uploads_helpers_callable(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", str(tmp_path))
    d = uploads._upload_dir("s/1 a")
    assert "s_1_a" in d                       # 非法字符被替换
    assert uploads._safe_ext("x.PNG") == ".png"
    assert uploads._safe_ext("noext") == ""
    # _upload_path：文件不存在 → None；存在 → 绝对路径；且防目录穿越
    assert uploads._upload_path("sid", "a.png") is None
    os.makedirs(uploads._upload_dir("sid"), exist_ok=True)
    target = os.path.join(uploads._upload_dir("sid"), "a.png")
    open(target, "w", encoding="utf-8").close()
    assert uploads._upload_path("sid", "a.png") == target
    assert uploads._upload_path("sid", "../escape.png") is None


def test_export_markdown_callable():
    md = export_md.session_markdown({"title": "测试会话", "messages": [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
    ]})
    assert isinstance(md, str) and "测试会话" in md and "你好" in md
    assert export_md._md_name('a/b:c*d?', "fb") == "a_b_c_d_"


def test_llm_helpers_callable():
    assert isinstance(llm.user_profile_text(), str)
    s = {"messages": [{"role": "user", "content": "第一句"},
                      {"role": "assistant", "content": "第二句"}]}
    out = llm.recent_dialog(s, limit=6, each=160)
    assert "第一句" in out and "第二句" in out
    assert isinstance(llm._record_quick_usage.__name__, str)


def test_vscode_helpers_callable():
    assert vscode._strip_jsonc('{"a": 1, // 注释\n "b": 2, /* 块注释 */ }').strip().startswith("{")
    assert vscode.find_vscode_cli() is None or isinstance(vscode.find_vscode_cli(), str)


def test_picker_code_is_dpi_aware_and_compiles():
    """高分屏下 Tk 目录框发虚：弹窗前必须先声明 DPI 感知。"""
    from app import agent
    compile(agent._PICKER_CODE, "<picker>", "exec")
    assert "SetProcessDpiAwareness" in agent._PICKER_CODE
    assert "askdirectory" in agent._PICKER_CODE
