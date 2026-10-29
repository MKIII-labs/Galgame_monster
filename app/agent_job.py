# -*- coding: utf-8 -*-
"""无人值守的只读巡检：给一段提示词，让模型自己在沙箱里读代码并给结论。

与 api/chat.py 的主对话循环**刻意不共用**：
- 没有 SSE / GAL 演出 / TTS / 记忆写入，跑完只返回一段文本
- 工具白名单只有只读五项：不写文件、不执行命令、不调有副作用的能力
- 轮数与总时长双上限，避免后台任务把线程挂死

由 workflow 的 ``agent.run`` 动作调用；结果只进运行历史，不往会话里写消息。
"""
import os
import time

from app import agent, llm
from app.config import get_api_conf
from app.usage import record_llm_usage

READONLY_TOOLS = ("list_dir", "read_file", "search_files", "search_code", "repo_map")
NO_FINDINGS = "NO_FINDINGS"
MAX_RESULT_CHARS = 8000

_SYSTEM = """你是被定时唤醒的巡检助手，只做只读调查。规则：
1. 只能使用给定的只读工具；不要尝试修改文件、执行命令或调用其他能力；
2. 调查完直接给结论，不要写寒暄、过程旁白或「我将要做什么」；
3. 如果没有任何值得报告的问题，只输出 NO_FINDINGS 这一个词。"""


def _schemas():
    """从主 Agent 的工具表里筛出只读那几项（不自己重写 schema，避免两处漂移）。"""
    return [t for t in agent.native_tool_schemas(agent_on=True)
            if (t.get("function") or {}).get("name") in READONLY_TOOLS]


def _findings(text):
    """模型是否报告了值得一看的内容。

    NO_FINDINGS 可能被写成 ``NO_FINDINGS`` 或 ``NO_FINDINGS（今天无变化）``，
    因此只认「首行以它开头且整段很短」；长篇报告里顺带提到它不算无发现。
    """
    body = (text or "").strip()
    if not body:
        return False
    head = body.splitlines()[0].strip().upper()
    return not (head.startswith(NO_FINDINGS) and len(body) <= 80)


def _fail(msg):
    return {"ok": False, "text": msg, "findings": False, "rounds": 0,
            "tools": [], "ms": 0}


def run(prompt, sandbox="", max_rounds=6, timeout=180, model=""):
    """跑一轮只读巡检，返回 {ok, text, findings, rounds, tools, ms}。"""
    prompt = str(prompt or "").strip()
    if not prompt:
        return _fail("提示词为空")
    root = agent.clean_sandbox(sandbox)
    if not root:
        return _fail("未指定沙箱目录")
    if not os.path.isdir(root):
        return _fail("沙箱目录不存在：%s" % root)
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return _fail("尚未配置聊天 API 密钥")
    model = (model or "").strip() or api_model

    started = time.time()
    try:
        limit = max(1, int(max_rounds or 1))
    except (TypeError, ValueError):
        limit = 1
    try:
        budget = max(5, int(timeout or 180))
    except (TypeError, ValueError):
        budget = 180
    tools = _schemas()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content":
            "工作目录（沙箱根）：%s\n工具里的 path 请用相对路径。\n\n任务：\n%s"
            % (root, prompt)},
    ]
    used, rounds, text = [], 0, ""
    while rounds < limit:
        left = budget - (time.time() - started)
        if left <= 3:
            break
        rr = llm.stream_chat(api_key, api_url, model, messages, tools=tools,
                             timeout=(10, max(5, min(120, int(left)))))
        record_llm_usage(rr.get("usage") or {}, model, "automation",
                         api="chat", url=api_url)
        text = (rr.get("visible") or "").strip()
        if text:
            messages.append({"role": "assistant", "content": text})
        calls = rr.get("tool_calls") or []
        if not calls:
            break
        results = []
        for call in calls:
            name = str(call.get("tool") or "").strip()
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if name not in READONLY_TOOLS:
                # 模型偶尔会自作主张调写工具：明确拒绝并告知边界，让它改用只读手段
                results.append("【%s】\n❌ 自动化任务只允许只读工具：%s"
                               % (name, "、".join(READONLY_TOOLS)))
                continue
            out, _undo, _is_write = agent.run_tool(root, name, args)
            used.append(name)
            results.append("【%s】\n%s" % (name, out[:MAX_RESULT_CHARS]))
        messages.append({"role": "user",
                         "content": "工具执行结果如下，请据此继续：\n\n"
                                    + "\n\n".join(results)})
        rounds += 1
    else:
        # 轮数用尽仍没给结论：当有发现处理，别静默吞掉
        text = (text + "\n（已达最大轮数 %d，自动结束，未给出结论）" % limit).strip()

    ok = bool(text) or bool(used)
    return {"ok": ok, "text": text or "模型没有返回内容",
            "findings": _findings(text), "rounds": rounds,
            "tools": used, "ms": int((time.time() - started) * 1000)}
