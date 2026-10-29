# -*- coding: utf-8 -*-
"""Agent 意图路由与工具调用格式收敛。

目标：用一套可回归的本地基准，保证「明确指令 → 正确工具」的准确率达到 95% 以上，
并覆盖模型常见的别名 / OpenAI 风格参数写法。
"""
import pytest

from app import agent


# 明确指令：每条都应命中预期工具；None 表示不应该硬塞工具。
INTENT_CASES = [
    # ---- B 站 ----
    ("帮我看看这个视频 https://www.bilibili.com/video/BV1xx411c7mD", "bili.analyze"),
    ("BV1xx411c7mD 讲了什么", "bili.analyze"),
    ("这个视频的弹幕 https://www.bilibili.com/video/BV1xx411c7mD", "bili.danmaku"),
    ("把评论区读完 https://www.bilibili.com/video/BV1xx411c7mD", "bili.comments"),
    ("要完整字幕 https://www.bilibili.com/video/BV1xx411c7mD", "bili.transcript"),
    ("做个时间轴 https://www.bilibili.com/video/BV1xx411c7mD", "bili.timeline"),
    ("B站 UP主 可可味 有哪些投稿", "bili.up"),
    ("查一下 bilibili 上 老番茄 的投稿", "bili.up"),
    # ---- 联网 ----
    ("帮我查一下今天的天气", "web_search"),
    ("搜一下 OpenAI 最新模型", "web_search"),
    ("最新 iPhone 价格", "web_search"),
    ("今天有什么新闻", "web_search"),
    ("美元兑人民币汇率", "web_search"),
    ("昨晚的比赛比分是多少", "web_search"),
    ("帮我在网上搜一下 Trae IDE 的更新公告", "web_search"),
    ("联网查一下最新政策", "web_search"),
    # ---- 提醒 ----
    ("明天早上8点提醒我交作业", "set_reminder"),
    ("今晚9点提醒我吃药", "set_reminder"),
    ("帮我设个闹钟，7:30", "set_reminder"),
    ("2026-10-20 09:00 提醒我开会", "set_reminder"),
    ("三天后提醒我复盘", "set_reminder"),
    ("一个小时后再提醒我", "set_reminder"),
    ("提醒我明天下午三点给妈妈打电话", "set_reminder"),
    ("每周一提醒我开周会", "set_reminder"),
    # ---- 日程 ----
    ("帮我安排明天上午10点复习数学", "calendar.create"),
    ("把明天的面试加到工作台", "calendar.create"),
    ("我明天有什么安排", "calendar.list"),
    ("查看一下这周的日程", "calendar.list"),
    ("创建一个日程：周五 14:00 组会", "calendar.create"),
    ("2026-10-21 09:00 安排牙医", "calendar.create"),
    ("列出下周的日程", "calendar.list"),
    ("schedule a meeting tomorrow", "calendar.create"),
    # ---- 记忆 ----
    ("记住：我对花生过敏", "memory.remember"),
    ("帮我记一下，周五交报告", "memory.remember"),
    ("你还记得我上次说的模型吗", "memory.search"),
    ("回忆一下我之前说过的偏好", "memory.search"),
    ("remember that I like dark mode", "memory.remember"),
    ("我之前说过什么关于数学的事", "memory.search"),
    # ---- 读文件 ----
    ("读一下 app/agent.py", "read_file"),
    ("打开 README.md", "read_file"),
    ("看看 C:\\project\\a.py 里写了什么", "read_file"),
    ("查看 src/main.js 的第 10 行", "read_file"),
    ("read package.json", "read_file"),
    ("帮我打开 ./config.yaml", "read_file"),
    ("看一下 tests/test_stock.py", "read_file"),
    ("读一下这个文件", None),
    # ---- 列目录 ----
    ("看看 app 目录里有什么", "list_dir"),
    ("列出项目文件", "list_dir"),
    ("列出当前目录", "list_dir"),
    ("ls src", "list_dir"),
    ("看看沙箱里有什么", "list_dir"),
    ("打开 app 文件夹", "list_dir"),
    # ---- 搜文件 ----
    ("在项目里搜索 TODO", "search_files"),
    ("搜索一下 app 目录里有没有 fetch(", "search_files"),
    ("grep read_file app/agent.py", "search_files"),
    ("找一下 agent.py 里的 parse_calls", "search_files"),
    ("搜索所有 *.py 文件里的 TODO", "search_files"),
    ("项目里有没有 use strict", "search_files"),
    # ---- 写 / 改 / 删 / 移动 ----
    ("新建文件 notes/todo.md，内容写今天要做的事", "write_file"),
    ("创建一个 README.md", "write_file"),
    ("把 app/agent.py 里的 read 改成 read_file", "edit_file"),
    ("替换 config.json 里的 debug 为 false", "edit_file"),
    ("删除 temp.log", "delete_file"),
    ("把 a.txt 重命名为 b.txt", "move_file"),
    ("移动 src/a.py 到 src/b.py", "move_file"),
    # ---- 不该硬塞工具 ----
    ("你好", None),
    ("帮我写一个 Python 快速排序", None),
    ("解释一下什么是递归", None),
    ("今天几号", None),
    ("这个安排不合理", None),
    ("提醒功能怎么用", None),
    ("提醒我一下这个功能", None),
    ("帮我查一下这个函数", None),
    ("帮我搜一下这个报错", None),
    ("搜索算法怎么写", None),
    ("我明天有考试", None),
    ("把这段代码改短一点", None),
    ("帮我总结一下这篇文章", None),
]


@pytest.mark.parametrize("text,expected", INTENT_CASES)
def test_intent_tool(text, expected):
    hint = agent.intent_hint(text)
    got = (hint or {}).get("tool")
    assert got == expected, "输入：%r\n期望：%r\n实际：%r\nhint=%r" % (
        text, expected, got, hint)


def test_intent_args_extraction():
    h = agent.intent_hint("读一下 app/agent.py")
    assert h["tool"] == "read_file"
    assert h["args"]["path"] == "app/agent.py"

    h = agent.intent_hint("看看 app 目录里有什么")
    assert h["tool"] == "list_dir"
    assert h["args"]["path"] == "app"

    h = agent.intent_hint("2026-10-20 09:00 提醒我开会")
    assert h["tool"] == "set_reminder"
    assert h["args"]["at"] == "2026-10-20 09:00"

    h = agent.intent_hint("把 a.txt 重命名为 b.txt")
    assert h["tool"] == "move_file"
    assert h["args"] == {"from": "a.txt", "to": "b.txt"}

    h = agent.intent_hint("查一下 bilibili 上 老番茄 的投稿")
    assert h["tool"] == "bili.up"
    assert h["args"]["name"] == "老番茄"


# ------------------------------------------------------------
# 工具名 / 参数格式收敛
# ------------------------------------------------------------

def test_parse_calls_openai_tool_calls_format():
    raw = ('{"tool_calls":[{"type":"function","function":{'
           '"name":"read_file","arguments":"{\\"file\\":\\"a.py\\"}"}}]}')
    calls = agent.parse_calls(raw)
    assert calls == [{"tool": "read_file", "args": {"path": "a.py"}}]


def test_parse_calls_openai_single_function_format():
    raw = '{"function":{"name":"read","arguments":{"path":"a.py"}}}'
    assert agent.parse_calls(raw) == [
        {"tool": "read_file", "args": {"path": "a.py"}}]


def test_parse_calls_aliases_and_arg_names():
    raw = '{"name":"ls","arguments":{"dir":"src"}}'
    assert agent.parse_calls(raw) == [
        {"tool": "list_dir", "args": {"path": "src"}}]

    raw = '{"tool":"calendar_create","args":{"title":"x","start_at":"2026-10-20 09:00"}}'
    assert agent.parse_calls(raw)[0]["tool"] == "calendar.create"

    raw = '{"tool":"web_search","args":{"query":"天气"}}'
    assert agent.parse_calls(raw)[0]["args"]["queries"] == ["天气"]


def test_parse_calls_edit_aliases():
    raw = ('{"tool_calls":[{"function":{"name":"edit","arguments":'
           '"{\\"file\\":\\"a.py\\",\\"old_string\\":\\"x\\",\\"new_string\\":\\"y\\"}"}}]}')
    assert agent.parse_calls(raw) == [
        {"tool": "edit_file", "args": {"path": "a.py", "old": "x", "new": "y"}}]


def test_parse_calls_keeps_unknown_dotted_tool():
    raw = '{"tool":"foo.bar","args":{}}'
    assert agent.parse_calls(raw) == [{"tool": "foo.bar", "args": {}}]


def test_parse_calls_search_disambiguation():
    assert agent.parse_calls(
        '{"tool":"search","args":{"queries":["天气"]}}')[0]["tool"] == "web_search"
    assert agent.parse_calls(
        '{"tool":"search","args":{"pattern":"TODO"}}')[0]["tool"] == "search_files"


def test_parse_calls_delete_disambiguation():
    assert agent.parse_calls(
        '{"tool":"delete","args":{"path":"a.txt"}}')[0]["tool"] == "delete_file"
    assert agent.parse_calls(
        '{"tool":"delete","args":{"id":"ev_1"}}')[0]["tool"] == "calendar.delete"


def test_parse_calls_keeps_tool_specific_arg_names():
    assert agent.parse_calls(
        '{"tool":"bili.up","args":{"keyword":"攻略"}}')[0]["args"] == {"keyword": "攻略"}
    assert agent.parse_calls(
        '{"tool":"memory.search","args":{"query":"偏好"}}')[0]["args"] == {"query": "偏好"}


def test_parse_calls_deduplicates_identical_calls():
    raw = ('{"calls":[{"tool":"read_file","args":{"path":"a.py"}},'
           '{"tool":"read_file","args":{"path":"a.py"}}]}')
    assert len(agent.parse_calls(raw)) == 1


def test_intent_hint_prompt_mentions_tool_and_args():
    h = agent.intent_hint("读一下 app/agent.py")
    p = agent.intent_hint_prompt(h)
    assert "read_file" in p and "app/agent.py" in p


def test_agent_prompt_has_tool_decision_table():
    p = agent.build_agent_prompt({}, search_ready=False)
    assert "工具选择决策表" in p
    assert "缺少必需参数" in p
    assert "先调用工具再下结论" in p or "工具返回后先核对" in p


def test_needs_intent_nudge():
    from app.api.chat import _needs_intent_nudge

    hint = {"tool": "read_file", "confidence": 0.95}
    assert _needs_intent_nudge("我来读取这个文件", hint, False, False) is True
    assert _needs_intent_nudge("我来读取这个文件", hint, True, False) is False
    assert _needs_intent_nudge("我来读取这个文件", hint, False, True) is False
    assert _needs_intent_nudge("我来读取这个文件", {"tool": "read_file",
                                                  "confidence": 0.8},
                               False, False) is False
    assert _needs_intent_nudge("请问文件路径是什么？", hint, False, False) is False
    assert _needs_intent_nudge("", hint, False, False) is False


def test_config_has_hall_ai_enabled_default():
    from app.config import load_config
    cfg = load_config()
    assert isinstance(cfg.get("hall_ai_enabled"), dict)
    assert 1 <= int(cfg.get("hall_stock_ai_count") or 3) <= 3
