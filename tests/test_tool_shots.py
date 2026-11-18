# -*- coding: utf-8 -*-
"""工具截图落沙箱：绝对路径改写成 .gm/screenshots 相对路径（对话里直接可见）。"""
from app.agent import tools_media


def _png(tmp_path, name="a.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
    return p


def test_mirror_shots_copies_and_rewrites(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    src = _png(tmp_path)
    out = tools_media._mirror_shots("视觉 QA\n截图：" + str(src), str(root))
    assert "截图：.gm/screenshots/qa_" in out
    files = list((root / ".gm" / "screenshots").glob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == src.read_bytes()
    # 原来的绝对路径不该再出现（前端只认沙箱相对路径）
    assert str(src) not in out


def test_mirror_shots_appends_line_for_view_list(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    src = _png(tmp_path, "front.png")
    out = tools_media._mirror_shots("front（?view=front）→ " + str(src), str(root))
    assert out.rstrip().splitlines()[-1].startswith("截图：.gm/screenshots/qa_")


def test_mirror_shots_keeps_missing_file_text(tmp_path):
    text = "截图：D:\\nope\\missing.png"
    assert tools_media._mirror_shots(text, str(tmp_path)) == text


def test_mirror_shots_dedupes_same_path(tmp_path):
    """同一张图在正文与 JSON 里各出现一次：只复制一份。"""
    root = tmp_path / "sandbox"
    root.mkdir()
    src = _png(tmp_path)
    out = tools_media._mirror_shots(
        "截图：" + str(src) + "\n" + '{"screenshot": "' + str(src) + '"}', str(root))
    assert len(list((root / ".gm" / "screenshots").glob("*.png"))) == 1
    assert out.count("截图：.gm/screenshots/qa_") == 1


def test_mirror_shots_noop_without_root(tmp_path):
    text = "截图：" + str(_png(tmp_path))
    assert tools_media._mirror_shots(text, "") == text
