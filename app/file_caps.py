# -*- coding: utf-8 -*-
"""把已有的文件工具包装成「能力」，供任务引擎调用。

为什么不在对话里也走这层：
文件工具的准入条件是「Agent 已开启 + 已选沙箱」，这条规则在 chat.py 里。
这里注册的能力标记为 chat=False，只给任务引擎用，避免绕过那个开关。
"""
from app import agent, capabilities
from app.sessions import find_session, load_sessions

_FILE_TOOLS = {
    "list_dir": ("列出目录内容", {"path": "str"}, "read"),
    "read_file": ("读取文本文件（带行号）", {"path": "str", "start": "int?",
                                            "limit": "int?"}, "read"),
    "search_files": ("按内容或文件名搜索", {"pattern": "str", "glob": "str?"}, "read"),
    "write_file": ("覆盖或创建文件", {"path": "str", "content": "str"}, "write"),
    "edit_file": ("精确替换文件内容", {"path": "str", "old": "str", "new": "str"}, "write"),
    "make_dir": ("创建目录", {"path": "str"}, "write"),
    "move_file": ("移动 / 重命名", {"from": "str", "to": "str"}, "write"),
    "delete_file": ("删除文件或空目录", {"path": "str"}, "write"),
}


def _sandbox_of(ctx):
    sid = (ctx or {}).get("session_id") or ""
    if not sid:
        return ""
    s = find_session(load_sessions(), sid)
    return agent.clean_sandbox((s or {}).get("sandbox"))


def _make(name):
    def _run(args, ctx):
        sandbox = _sandbox_of(ctx)
        if not sandbox:
            return "❌ 该会话还没有设置工作区，文件工具不可用。"
        text, _undo, _is_write = agent.run_tool(sandbox, name, args or {})
        return text
    return _run


for _name, (_desc, _schema, _perm) in _FILE_TOOLS.items():
    capabilities.capability(_name, _desc, _schema, permission=_perm,
                            chat=False)(_make(_name))
