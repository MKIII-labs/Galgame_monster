# -*- coding: utf-8 -*-
"""Code Mode：只读工具桥、越权拦截、输出收集。"""
import os

from app import code_mode


def _sandbox(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text("console.log(1)", encoding="utf-8")
    return str(tmp_path)


def test_read_tool_through_bridge(tmp_path):
    box = _sandbox(tmp_path)
    code = "import gm\nprint(gm.call('list_dir', path='.'))\n"
    text, stat = code_mode.run(box, code, timeout=20)
    assert stat["calls"] == 1
    assert "a.txt" in text or "src" in text
    assert "run_script" in text


def test_write_tool_is_denied(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import gm\n"
            "try:\n"
            "    gm.call('write_file', path='evil.txt', content='x')\n"
            "    print('NOT DENIED')\n"
            "except Exception as e:\n"
            "    print('DENIED', e)\n")
    text, stat = code_mode.run(box, code, timeout=20)
    assert "DENIED" in text
    assert not os.path.exists(os.path.join(box, "evil.txt"))
    # 被拒的调用也计入统计，模型能看到自己越权了
    assert stat["calls"] == 1


def test_print_output_and_log(tmp_path):
    box = _sandbox(tmp_path)
    logs = []
    code = ("import gm\n"
            "gm.log('开始统计')\n"
            "print('结果=42')\n")
    text, _stat = code_mode.run(box, code, timeout=20, emit=logs.append)
    assert "结果=42" in text
    assert any(x.get("type") == "script_log" for x in logs)


def test_runs_inside_sandbox(tmp_path):
    box = _sandbox(tmp_path)
    code = "import os\nprint('CWD', os.getcwd())\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert os.path.basename(box) in text


def test_parallel_reads(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import gm\n"
            "r = gm.parallel([('read_file', {'path': 'a.txt'}),"
            " ('read_file', {'path': 'src/main.js'})])\n"
            "print(len(r), 'ok' if all(x for x in r) else 'fail')\n")
    text, stat = code_mode.run(box, code, timeout=20)
    assert "2 ok" in text
    assert stat["calls"] == 2


def test_write_allowed_when_permitted(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import gm\n"
            "gm.call('write_file', path='out.txt', content='hi')\n"
            "print('WROTE')\n")
    text, _ = code_mode.run(box, code, timeout=20, allow_write=True)
    assert "WROTE" in text
    assert os.path.exists(os.path.join(box, "out.txt"))


def test_gm_tools_catalog_and_tool_error(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import gm\n"
            "names = [t['name'] for t in gm.tools()]\n"
            "print('HAS-READ', 'read_file' in names)\n"
            "try:\n"
            "    gm.call('write_file', path='x.txt', content='x')\n"
            "except gm.ToolError as e:\n"
            "    print('CODE', e.code)\n")
    text, _ = code_mode.run(box, code, timeout=20)
    assert "HAS-READ True" in text
    assert "CODE TOOL_NOT_ALLOWED" in text


def test_parallel_detail_and_call_budget(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import gm\n"
            "r = gm.parallel_detail([('read_file', {'path': 'a.txt'}),"
            " ('read_file', {'path': 'src/main.js'})])\n"
            "print('DETAIL', [x['ok'] for x in r])\n")
    text, _ = code_mode.run(box, code, timeout=20)
    assert "DETAIL [True, True]" in text

    code2 = ("import gm\n"
             "for p in ('a.txt', 'src/main.js'):\n"
             "    try:\n"
             "        gm.call('read_file', path=p)\n"
             "    except gm.ToolError as e:\n"
             "        print('LIMIT', e.code)\n")
    text2, _ = code_mode.run(box, code2, timeout=20, caps={"code_max_calls": 1})
    assert "LIMIT TOOL_BUDGET" in text2


def test_gm_is_auto_injected(tmp_path):
    """模型漏写 import gm 时也要能用（这次会话的真实故障）。"""
    box = _sandbox(tmp_path)
    code = "print(gm.call('read_file', path='a.txt'))\n"
    text, stat = code_mode.run(box, code, timeout=20)
    assert "hello" in text
    assert "Traceback" not in text
    assert not text.startswith("❌")
    assert stat["ok"] is True


def test_script_error_is_marked_failed(tmp_path):
    box = _sandbox(tmp_path)
    text, stat = code_mode.run(box, "raise RuntimeError('boom')\n", timeout=20)
    assert text.startswith("❌")
    assert "boom" in text
    assert stat["ok"] is False
    assert stat["exit_code"] != 0


# ---------------------------------------------------------------- 安全护栏（audit hook）
def test_raw_write_is_denied_by_default(tmp_path):
    """不打开 code_mode_allow_write 时，内置 open() 也写不了文件。"""
    box = _sandbox(tmp_path)
    code = "open('evil.txt', 'w', encoding='utf-8').write('x')\nprint('NOT DENIED')\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert "NOT DENIED" not in text
    assert "安全护栏" in text
    assert not os.path.exists(os.path.join(box, "evil.txt"))


def test_raw_read_outside_sandbox_is_denied(tmp_path):
    box = os.path.join(str(tmp_path), "box")
    os.makedirs(box)
    secret = tmp_path / "outside.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    code = "print(open(%r, encoding='utf-8').read())\n" % str(secret)
    text, _ = code_mode.run(box, code, timeout=20)
    assert "TOPSECRET" not in text
    assert "只能读沙箱内的文件" in text


def test_raw_read_inside_sandbox_is_allowed(tmp_path):
    box = _sandbox(tmp_path)
    code = "print('CONTENT', open('a.txt', encoding='utf-8').read())\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert "CONTENT hello" in text


def test_raw_write_allowed_only_inside_sandbox(tmp_path):
    box = _sandbox(tmp_path)
    code = "open('out.txt', 'w', encoding='utf-8').write('hi')\nprint('WROTE')\n"
    text, _ = code_mode.run(box, code, timeout=20, allow_write=True)
    assert "WROTE" in text
    assert os.path.exists(os.path.join(box, "out.txt"))
    code2 = "open('../escape.txt', 'w', encoding='utf-8').write('x')\nprint('ESCAPED')\n"
    text2, _ = code_mode.run(box, code2, timeout=20, allow_write=True)
    assert "ESCAPED" not in text2
    assert not os.path.exists(os.path.join(os.path.dirname(box), "escape.txt"))


def test_os_remove_is_denied_by_default(tmp_path):
    box = _sandbox(tmp_path)
    code = "import os\nos.remove('a.txt')\nprint('REMOVED')\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert "REMOVED" not in text
    assert os.path.exists(os.path.join(box, "a.txt"))


def test_subprocess_is_denied_by_default(tmp_path):
    box = _sandbox(tmp_path)
    code = ("import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'print(1)'])\nprint('RAN')\n")
    text, _ = code_mode.run(box, code, timeout=20)
    assert "RAN" not in text
    assert "允许脚本执行命令" in text


def test_outside_read_allowed_when_permitted(tmp_path):
    box = os.path.join(str(tmp_path), "box")
    os.makedirs(box)
    secret = tmp_path / "outside.txt"
    secret.write_text("READABLE", encoding="utf-8")
    code = "print('OUT', open(%r, encoding='utf-8').read())\n" % str(secret)
    text, _ = code_mode.run(box, code, timeout=20, allow_outside=True)
    assert "OUT READABLE" in text


def test_script_tool_call_outside_read_allowed(tmp_path):
    """脚本里 gm.call('read_file') 也要拿到 allow_outside，不能只有内置 open() 放行。"""
    box = os.path.join(str(tmp_path), "box")
    os.makedirs(box)
    outside = tmp_path / "outside.txt"
    outside.write_text("TOOL_READABLE", encoding="utf-8")
    code = "print('OUT', gm.call('read_file', path=%r))\n" % str(outside)
    text, _ = code_mode.run(box, code, timeout=20, allow_outside=True)
    assert "TOOL_READABLE" in text
    text2, _ = code_mode.run(box, code, timeout=20)
    assert "TOOL_READABLE" not in text2


def test_script_cannot_read_data_dir(tmp_path, monkeypatch):
    """沙箱里的 data/（运行数据、含 API Key）即使被沙箱包住也要拒绝。"""
    from app import store
    box = _sandbox(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"chat_api_key": "SECRET"}', encoding="utf-8")
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    code = "print(open('data/config.json', encoding='utf-8').read())\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert "SECRET" not in text
    assert "受保护" in text


def test_script_cannot_read_git_config(tmp_path):
    """沙箱内 .git/config 的 remote 可能带令牌，脚本也要拒绝。"""
    box = _sandbox(tmp_path)
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text(
        "url = https://token@github.com/x/y\n", encoding="utf-8")
    code = "print(open('.git/config', encoding='utf-8').read())\n"
    text, _ = code_mode.run(box, code, timeout=20)
    assert "token@github" not in text
    assert "受保护" in text
