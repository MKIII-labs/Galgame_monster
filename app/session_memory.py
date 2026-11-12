# -*- coding: utf-8 -*-
"""会话工作记忆（L2）：回合结束后用轻量模型把新增对话合并进一份结构化笔记。

对齐 Claude Code 的 session memory：
- 不是每条消息都更新：必须「token 增长达阈值」且「工具调用够多，或本轮自然停顿」；
- 只在后台线程跑，不阻塞回复；
- 压缩时如果这份笔记已经覆盖待压区间、没过期、区间指纹没变，就直接当摘要用，
  省掉一次摘要调用（零 API 压缩）。

它和会话记忆图谱是两回事：图谱是用户可见、可编辑的长期条目；这里只是一份
供接续用的工作笔记，存在会话字段里，不参与检索排序。
"""
import re
import threading
import time

from app import context
from app.config import get_api_conf, hybrid_on, is_local_url, load_config
from app.sessions import find_session, load_sessions, save_sessions
from app.store import _lock

MIN_TOKENS = 6000          # 距上次更新的 token 增长硬阈值
INITIAL_TOKENS = 8000      # 还没有任何工作记忆时，至少攒这么多再产出
MIN_TOOL_CALLS = 3         # 或者工具调用达到这个数
MAX_INPUT_CHARS = 24000    # 单次喂给轻量模型的新增对话上限
MAX_MEMORY_CHARS = 12000   # 工作笔记本身的长度上限
MIN_OUTPUT_CHARS = 60

_RUNNING = set()
_RUN_LOCK = threading.Lock()

PROMPT_HEAD = (
    "你是会话工作记忆维护器。把「现有工作记忆」和「新增对话」合并成一份"
    "供另一个模型无损接续的 Markdown 笔记。\n"
    "要求：\n"
    "- 只保留仍然有效的事实；旧结论被推翻就改写，不要留下矛盾的两份说法；\n"
    "- 不要编造原文没有的信息，不要大段复述对话；\n"
    "- 保留精确的文件路径、命令、错误串、数值、函数名；\n"
    "- 结构固定（每节都要有，空的写「(none)」）：\n"
    "## 目标\n## 已完成\n## 关键决定与理由\n## 文件与命令\n"
    "## 错误与修复\n## 未完成与下一步\n## 用户偏好与纠正\n"
    "- 总长不超过 6000 字。\n\n"
)


def enabled(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else (load_config() or {})
    return bool(cfg.get("session_memory_enabled", True))


def _count_tool_calls(msgs):
    n = 0
    for m in msgs or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            n += len(m.get("tools") or [])
    return n


def _last_had_tools(msgs):
    for m in reversed(msgs or []):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return bool(m.get("tools"))
    return False


def should_update(s, had_tools=None):
    """是否值得跑一次后台更新（对齐 CC 的双阈值：token 必达 + 另一个条件）。"""
    if not s or not enabled():
        return False
    msgs = s.get("messages") or []
    if not msgs:
        return False
    total = context.estimate_messages_tokens(msgs)
    if not str(s.get("session_memory") or "").strip():
        return total >= INITIAL_TOKENS
    try:
        prev = int(s.get("session_memory_tokens") or 0)
        upto = int(s.get("session_memory_upto") or 0)
    except (TypeError, ValueError):
        return False
    if upto > len(msgs) or total - prev < MIN_TOKENS:
        return False
    tools_since = _count_tool_calls(msgs[upto:])
    if had_tools is None:
        had_tools = _last_had_tools(msgs)
    return tools_since >= MIN_TOOL_CALLS or not had_tools


def _excerpt(m):
    """一条消息给记忆模型的压缩版：正文 + 本轮工具调用摘要。"""
    text = context.message_text(m).strip()
    tools = m.get("tools") if isinstance(m, dict) else None
    if tools:
        rows = []
        for t in tools[-20:]:
            if not isinstance(t, dict):
                continue
            name = str(t.get("tool") or "")
            args = str(t.get("args") or "")
            if isinstance(t.get("args"), dict):
                args = " ".join("%s=%s" % (k, v) for k, v in list(t["args"].items())[:4])
            res = str(t.get("result") or "").replace("\n", " ")[:180]
            rows.append("- %s %s → %s" % (name, args[:120], res))
        if rows:
            text = (text + "\n【本轮工具】\n" + "\n".join(rows)).strip()
    return text


def _new_dialog_text(s):
    msgs = s.get("messages") or []
    try:
        upto = int(s.get("session_memory_upto") or 0)
    except (TypeError, ValueError):
        upto = 0
    out, used = [], 0
    for m in msgs[max(0, upto):]:
        if not isinstance(m, dict):
            continue
        role = "用户" if m.get("role") == "user" else "助手"
        piece = "%s：%s" % (role, _excerpt(m)[:3000])
        if used + len(piece) > MAX_INPUT_CHARS:
            out.append("…（后面还有 %d 条，已省略）" % max(0, len(msgs) - upto - len(out)))
            break
        out.append(piece)
        used += len(piece)
    return "\n\n".join(out)


def _would_use_light():
    """当前配置下 _quick_chat(use_light=True) 是否真的会走轻量模型。

    和 llm._pick_light_model 的判定保持一致：没配 light_model、或本地 light
    在混合模式关闭时，都不算「有轻量模型」——那时主模型本来就是兜底，
    失败了再重试一次只会重复烧钱。
    """
    cfg = load_config() or {}
    lm = str(cfg.get("light_model") or "").strip()
    if not lm:
        return False
    if is_local_url(cfg.get("light_url")) and not hybrid_on(cfg):
        return False
    return True


def _call_model(api_key, api_url, api_model, messages):
    """优先轻量模型；轻量不可用 / 返回空时回退主模型（没有轻量模型则只调一次）。"""
    from app import llm
    try:
        txt = llm._quick_chat(
            api_key, api_url, api_model, messages,
            max_tokens=1400, source="session-memory",
            timeout=(10, 120), tries=1, use_light=True)
        if str(txt or "").strip():
            return txt
        raise RuntimeError("轻量模型返回空")
    except Exception as e:                # noqa: BLE001
        if not _would_use_light():
            print(f"[会话记忆] 调用失败：{e}")
            return ""
        print(f"[会话记忆] 轻量模型失败，回退主模型：{e}")
    try:
        return llm._quick_chat(
            api_key, api_url, api_model, messages,
            max_tokens=1400, source="session-memory",
            timeout=(10, 120), tries=1, use_light=False)
    except Exception as e:                # noqa: BLE001
        print(f"[会话记忆] 主模型兜底也失败：{e}")
        return ""


def _clean_output(text):
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    if len(t) < MIN_OUTPUT_CHARS:
        return ""
    if len(t) > MAX_MEMORY_CHARS:
        t = t[:MAX_MEMORY_CHARS] + "\n…（已截断）"
    return t


def build_update(s):
    """跑一次轻量模型，返回要写回会话的字段；失败返回 None（测试直接调它）。"""
    dialog = _new_dialog_text(s)
    if not dialog.strip():
        return None
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return None
    old = str(s.get("session_memory") or "").strip()
    user = (PROMPT_HEAD
            + "现有工作记忆：\n" + (old or "（暂无）")
            + "\n\n新增对话：\n" + dialog
            + "\n\n请输出合并后的完整笔记。")
    txt = _call_model(api_key, api_url, api_model,
                      [{"role": "system", "content": "你是会话工作记忆维护器，只输出 Markdown 笔记。"},
                       {"role": "user", "content": user}])
    text = _clean_output(txt)
    if not text:
        return None
    msgs = s.get("messages") or []
    return {
        "session_memory": text,
        "session_memory_upto": len(msgs),
        "session_memory_at": time.time(),
        "session_memory_tokens": context.estimate_messages_tokens(msgs),
        "session_memory_tool_calls": _count_tool_calls(msgs),
        "session_memory_fp": context.range_fingerprint(msgs, len(msgs)),
    }


def _save_fields(sid, fields):
    """只改写目标会话的工作记忆字段；与 _persist_session 共用同一把锁。"""
    with _lock:
        data = load_sessions()
        s = find_session(data, sid)
        if not s:
            return False
        cur_upto = int(s.get("session_memory_upto") or 0)
        if int(fields.get("session_memory_upto") or 0) < cur_upto:
            return False                   # 另一个更新已经跑到更后面，别回退
        s.update(fields)
        save_sessions(data)
    return True


def _run(sid, had_tools):
    try:
        s = find_session(load_sessions(), sid)
        if not s or not should_update(s, had_tools=had_tools):
            return
        fields = build_update(s)
        if not fields:
            return
        if _save_fields(sid, fields):
            print("[会话记忆] 已更新（%d 字）" % len(fields["session_memory"]))
    except Exception as e:                # noqa: BLE001
        print(f"[会话记忆] 后台更新异常：{e}")
    finally:
        with _RUN_LOCK:
            _RUNNING.discard(sid)


def maybe_update(sid, had_tools=None):
    """回合结束后调用：条件满足就在后台刷新；立即返回，不阻塞回复。"""
    sid = str(sid or "")
    if not sid or not enabled():
        return False
    with _RUN_LOCK:
        if sid in _RUNNING:
            return False
        _RUNNING.add(sid)
    threading.Thread(target=_run, args=(sid, had_tools),
                     daemon=True, name="session-memory").start()
    return True
