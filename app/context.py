# -*- coding: utf-8 -*-
"""上下文容量：token 估算 / 校准 / 超限压缩 / 后台预压缩

压缩分两级：
- 预压缩：占用达到 COMPRESS_RATIO（80%）时在后台线程压缩，本轮请求不等待；
- 兜底压缩：发送前若已超上限，仍在请求线程里同步压一次，保证不把超限请求发出去。
"""

import hashlib
import json
import re
import threading
import time

from app.config import (MAX_RESERVE, get_api_conf, load_config,
                        model_context_limit)
from app.profile import load_profile, manual_block
from app.sessions import (MAX_COMPRESS_MARKS, compress_marks_for, find_session,
                          invalidate_compression, load_sessions, save_sessions,
                          trim_compress_marks)
from app.store import _lock

MSG_OVERHEAD = 4            # 每条消息的固定结构开销（近似）
COMPRESS_KEEP_MIN = 8       # 压缩时至少保留的最近原文条数
COMPRESS_KEEP_MAX = 60      # 按 token 预算保留时最多回溯多少条
COMPRESS_KEEP_RATIO = 0.25  # 保留最近约 25% 窗口的原文
COMPRESS_RATIO = 0.8        # 达到 80% 就在后台预压缩
SUMMARY_MAX_TOKENS = 2048   # 生成摘要时的最大输出 token
IMAGE_TOKENS = 1600         # 一张图片的保守估算（vision 模式下会先转成文字）
COMPRESS_MSG_CHARS = 20000  # 送进摘要的单条消息上限（防止单条超大附件再次撑爆窗口）
HISTORY_PROCESS_KEEP_TURNS = 2   # 最近几条 assistant 消息保留原文（更早的压成过程摘要）
HISTORY_PROCESS_DIGEST = 240     # 过程摘要长度：首尾各取一半
TOOL_MSG_PREFIX = "工具执行结果如下，请据此继续："   # chat.py 回灌工具结果时的固定开头

# 压缩摘要的必保项：结构化检查点，避免「摘要太短、接续时丢文件/错误/待办」
COMPRESS_PROMPT = (
    "请把下面这段较早的对话压缩成一份结构化检查点，让另一个模型能无损接续。\n"
    "【摘要】正文严格按以下 Markdown 结构输出，每节都要有，空的写「(none)」：\n\n"
    "## 主要请求与意图\n- [用户最初与变化后的目标；关键原话保留]\n\n"
    "## 关键技术概念\n- [技术、框架、模式、约定]\n\n"
    "## 文件与代码\n- [精确路径：为什么重要、关键改动/片段]\n\n"
    "## 错误与修复\n- [错误原文：如何解决、用户反馈]\n\n"
    "## 待办\n- [明确要求但还没做的]\n\n"
    "## 当前工作\n- [检查点时正在做什么]\n\n"
    "## 下一步\n- [最直接的下一步，或 (none)]\n\n"
    "## 关键上下文\n- [决定与理由、约束、用户偏好、未决问题]\n\n"
    "规则：保留精确文件路径、命令、错误串、标识符、数值、函数签名；"
    "合并【已有摘要】时保留仍有效的事实、丢弃过时的、合并成一份；"
    "不要逐句复述，不要加入原文没有的信息，不要提到这次压缩请求。\n\n"
    "最后另起一段：\n"
    "【记忆要点】\n"
    "- 值得长期记住的结论，每条一行，最多 6 条；没有就写（无）"
)

_COMPRESSING = set()        # 正在压缩的会话 id（后台预压缩与手动压缩共用）
_COMPRESS_LOCK = threading.Lock()


def context_limits(model=""):
    """读取「上下文上限」与「为回复预留」的 token 数。

    上限优先按模型名匹配 model_contexts（不同模型窗口差别很大，
    写死 128000 会让 200k 窗口的模型白白提前压缩）。

    不传 model 时取当前聊天模型：以前默认空串，`model_context_limit("")` 匹配不到
    内置表，用户把上限填成 1 亿时会被钳到硬上限 1M —— 于是进度环显示 1M（假的）、
    后台预压缩要 800k 才触发（等于永不触发），压缩全挤到请求线程里同步做。
    """
    cfg = load_config()
    if not model:
        try:
            model = (get_api_conf("chat") or ("", "", ""))[2] or ""
        except Exception:
            model = ""
    try:
        limit = int(model_context_limit(model, cfg))
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        # 不限窗口（设置里没填）：不按比例算预算，压缩逻辑全部不触发
        return 0, 0
    _res_raw = cfg.get("chat_reserve_output")
    try:
        # 0 是合法值（不预留），不能用 `or 默认值` 把它吃掉
        reserve = 16384 if _res_raw is None or _res_raw == "" else int(_res_raw)
    except (TypeError, ValueError):
        reserve = 16384
    if reserve < 0:
        reserve = 0
    # reserve 必须明显小于 limit，否则「可用窗口」会变成 0 或负数，压缩逻辑全部失效
    reserve = min(reserve, MAX_RESERVE, max(0, limit // 2))
    return limit, reserve


def effective_output_cap(limit=None, cfg=None):
    """单次请求真正生效的输出上限。

    对齐 dsh 的比例：输出最多占窗口的 `chat_output_window_ratio`（默认 25%，
    与它 1M 窗口 / 256k 输出一致）。用户把 `chat_max_tokens` 填得再大也会被夹住，
    否则「提示词 + 输出」会越过窗口，表现为上游 400 或思考后不再吐正文。
    返回 0 表示不限制（用户显式关闭了比例，且自己没填上限）。
    """
    cfg = cfg or load_config()
    if limit is None:
        limit, _ = context_limits()
    try:
        cap = int(cfg.get("chat_max_tokens") or 0)
    except (TypeError, ValueError):
        cap = 0
    try:
        ratio = float(cfg.get("chat_output_window_ratio", 0.25) or 0)
    except (TypeError, ValueError):
        ratio = 0.25
    if limit > 0 and ratio > 0:
        by_ratio = max(1024, int(limit * min(1.0, ratio)))
        cap = by_ratio if cap <= 0 else min(cap, by_ratio)
    return max(0, cap)


def request_budget(model="", cfg=None):
    """可用提示词预算：`min(窗口*压缩阈值, 窗口 - 输出上限 - headroom)`。

    这是 dsh compaction-basic 的 `floor(min(W*thresholdRatio, W-O-headroom))`。
    以前只按 `W*0.8` 触发压缩，128k 窗口 + 65k 输出时可用输入只剩 63k，
    实际提示词却常到 88k —— 上游要么 400，要么思考完就不再吐正文。
    """
    cfg = cfg or load_config()
    limit, _reserve = context_limits(model)
    if limit <= 0:
        return 0                      # 不限窗口 = 没有提示词预算，压缩不触发
    try:
        ratio = float(cfg.get("compress_ratio") or COMPRESS_RATIO)
    except (TypeError, ValueError):
        ratio = COMPRESS_RATIO
    ratio = max(0.3, min(0.95, ratio))
    out = effective_output_cap(limit, cfg)
    try:
        headroom = int(cfg.get("chat_output_headroom") or 0)
    except (TypeError, ValueError):
        headroom = 0
    by_ratio = int(limit * ratio)
    by_window = limit - out - max(0, headroom)
    # 小窗口（测试 / 本地模型）保底不能超过窗口的一半，否则预算会大于窗口本身
    floor = max(512, min(8000, limit // 2))
    return max(floor, min(by_ratio, by_window))


def estimate_tokens(text):
    """启发式 token 估算：CJK 0.6 / 英文数字 0.28 / 空白 0.1 / 标点 0.35 / 其他 0.8。
    真实值仍靠 `usage.prompt` 校准（calibrate_ratio），这里只负责给压缩阈值一个稳定基线。"""
    if not text:
        return 0
    cjk = alnum = space = punct = other = 0
    for ch in str(text):
        o = ord(ch)
        if (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7A3 or
                0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF):
            cjk += 1
        elif ch.isascii() and ch.isalnum():
            alnum += 1
        elif ch.isspace():
            space += 1
        elif ch.isascii():
            punct += 1
        else:
            other += 1
    return round(cjk * 0.6 + alnum * 0.28 + space * 0.1
                 + punct * 0.35 + other * 0.8)


def tools_tokens(tools):
    """工具 schema 的 token 估算（dsh 的 tokenMeter 也把 tools 算进请求体）。"""
    if not tools:
        return 0
    try:
        raw = json.dumps(tools, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    return estimate_tokens(raw) + len(tools) * 8


def summary_block(summary):
    """把摘要包装成注入到系统提示里的文本块"""
    return "以下是本次对话较早内容的摘要（原始消息已压缩）：\n" + (summary or "")


def _msg_text(m):
    """一条消息真正发给模型的文本：正文 + 图片描述 + 附件正文。
    估算与拼装必须共用这一个函数，否则进度条会低估附件体积。"""
    text = m.get("content") or ""
    if isinstance(text, list):
        # 多模态 content（图片 + 文字）：只取文字部分，图片按张数单独估算，
        # 不能 str(list) —— 那会把 base64 当正文算，token 估算直接爆掉。
        parts = []
        for p in text:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    parts.append("[图片]")
            elif p:
                parts.append(str(p))
        text = "\n".join(parts)
    if m.get("vision"):
        text = (text + "\n\n【图片内容】\n" + m["vision"]) if text \
               else ("【用户上传了一张图片】\n" + m["vision"])
    if m.get("doc_text"):
        fn = (m.get("doc") or {}).get("name") or "附件"
        text = (text + "\n\n【附件：" + str(fn) + "】\n" + m["doc_text"]) if text \
               else ("【用户上传了附件 " + str(fn) + "】\n" + m["doc_text"])
    return text


def _msg_tokens(m):
    n = MSG_OVERHEAD + estimate_tokens(_msg_text(m))
    content = m.get("content") if isinstance(m, dict) else None
    if isinstance(content, list):
        n += IMAGE_TOKENS * sum(1 for p in content
                                if isinstance(p, dict) and p.get("type") == "image_url")
    return n


def _context_text_tokens(system_prompt, summary, msgs):
    """估算一次请求上下文的 token 数（system + 摘要 + 消息）"""
    n = 0
    if system_prompt:
        n += MSG_OVERHEAD + estimate_tokens(system_prompt)
    if summary:
        n += MSG_OVERHEAD + estimate_tokens(summary_block(summary))
    for m in msgs:
        n += _msg_tokens(m)
    return n


def _context_parts(system_prompt, summary, msgs):
    """分项统计：系统提示 / 摘要 / 对话正文 / 附件与图片描述"""
    parts = {"system": 0, "summary": 0, "dialog": 0, "attachment": 0, "image": 0}
    if system_prompt:
        parts["system"] = MSG_OVERHEAD + estimate_tokens(system_prompt)
    if summary:
        parts["summary"] = MSG_OVERHEAD + estimate_tokens(summary_block(summary))
    for m in msgs:
        raw = m.get("content") or ""
        if isinstance(raw, list):
            base = estimate_tokens(_msg_text(m))
            extra = 0
        else:
            base = estimate_tokens(raw)
            extra = estimate_tokens(_msg_text(m)) - base
        parts["dialog"] += MSG_OVERHEAD + base
        if extra > 0:
            parts["attachment"] += extra
    return parts


def estimate_messages_tokens(messages):
    """估算一组「已拼好的」请求消息（system / user / assistant 混合）"""
    n = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("content"), str):
            n += MSG_OVERHEAD + estimate_tokens(m["content"])
        else:                       # 多模态数组：文字按字符估，图片按张数估
            n_img = 0
            for item in (m.get("content") or []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    n += estimate_tokens(item.get("text") or "")
                elif item.get("type") == "image_url":
                    n_img += 1
            n += MSG_OVERHEAD + IMAGE_TOKENS * n_img
    return n


IN_PARTS = ("rules", "tools_doc", "memory", "other_sys",
            "summary", "user", "reply", "tool_result", "schema")


def input_breakdown(messages, sys_parts=None, summary_chars=0, schema_chars=0):
    """按类型统计一次请求的输入体量（字符），用于「token 明细」看钱花在哪。

    只统计字符数：token 由调用方按该轮真实 `usage.prompt / 总字符` 缩放，
    这样各分类加起来正好等于那一轮显示的输入 token。

    sys_parts 是调用方已知的系统提示子块长度：
      rules（规则/画像/记忆说明）、tools_doc（工具说明）、memory（检索记忆/任务状态）；
    系统提示里剩下的（界面上下文 / 自动化目标 / 时钟 / 研究台账 / GAL 演出提示）
    统一算 other_sys —— 不逐个埋点，避免以后新增块要同步改这里。
    """
    sys_parts = sys_parts or {}
    out = {k: 0 for k in IN_PARTS}
    sys_total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = str(m.get("content") or "")
        n = len(text)
        role = m.get("role")
        if role == "system":
            sys_total += n
        elif role == "assistant":
            out["reply"] += n
        elif role == "user" and text.startswith(TOOL_MSG_PREFIX):
            out["tool_result"] += n
        else:
            out["user"] += n
    for k in ("rules", "tools_doc", "memory"):
        try:
            out[k] = max(0, int(sys_parts.get(k) or 0))
        except (TypeError, ValueError):
            out[k] = 0
    try:
        out["summary"] = max(0, int(summary_chars or 0))
    except (TypeError, ValueError):
        out["summary"] = 0
    known = out["rules"] + out["tools_doc"] + out["memory"] + out["summary"]
    out["other_sys"] += max(0, sys_total - known)
    try:
        out["schema"] = max(0, int(schema_chars or 0))
    except (TypeError, ValueError):
        out["schema"] = 0
    return out


def scale_parts(parts, total_tokens, total_chars):
    """把字符构成按「真实 prompt / 总字符」缩放成 token（对不齐时丢弃，宁缺毋滥）。"""
    try:
        total_tokens = int(total_tokens or 0)
        total_chars = int(total_chars or 0)
    except (TypeError, ValueError):
        return None
    if total_tokens <= 0 or total_chars <= 0 or not isinstance(parts, dict):
        return None
    ratio = float(total_tokens) / float(total_chars)
    out = {}
    for k, v in parts.items():
        try:
            n = int(round(int(v) * ratio))
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[k] = n
    return out or None


def calibrate_ratio(s, system_prompt=""):
    """用最近一条带真实 usage 的助手消息，算出「真实 / 估算」的校准因子。

    `usage.prompt` 是**整回合累计**（每轮 prompt 相加，实测一条消息 5,377,707 /
    43 轮），不能当单次请求用 —— 以前直接拿它算，ratio 会放大到 3 倍以上，
    进度条出现「149%」这种假超载，压缩也会被提前触发。
    新消息额外记 `usage.last_prompt`（最后一轮的真实 prompt）；旧数据没有这个
    字段时，用窗口粗筛掉明显是累计值的锚点。
    """
    msgs = s.get("messages", [])
    cut = int(s.get("summary_upto") or 0)
    try:
        _window = context_limits()[0]
    except Exception:
        _window = 0
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        prompt_tokens = u.get("last_prompt")
        if not prompt_tokens:
            prompt_tokens = u.get("prompt")
            # 旧数据：没有 last_prompt，只接受「像单次请求」的值（不限窗口时不做这道粗筛）
            try:
                if _window > 0 and int(prompt_tokens or 0) > _window * 1.2:
                    continue
            except (TypeError, ValueError):
                continue
        # 只有「生成该锚点时的压缩点 == 当前压缩点」才可对齐；
        # 否则（例如压缩前产生的旧锚点）其 prompt 对应的是全量历史，估算无法对上
        if not prompt_tokens or cut >= i or int(m.get("ctx_cut", 0)) != cut:
            continue
        est = _context_text_tokens(system_prompt, s.get("summary") or "", msgs[cut:i])
        if est > 0:
            ratio = int(prompt_tokens) / est
            if 0.2 <= ratio <= 5:         # 明显离群则丢弃，避免估算被带偏
                return ratio
    return 1.0


def session_context(s, system_prompt="", extra_tokens=0, tools_extra=0):
    """返回当前会话的上下文占用（估算 + 校准），供进度条与预警使用。
    extra_tokens 用于 multimodal 图片；tools_extra 用于工具 schema（不属于消息，
    但同样占请求体）。"""
    limit, reserve = context_limits()
    cut = int(s.get("summary_upto") or 0)
    summary = s.get("summary") or ""
    msgs = s.get("messages", [])[cut:]
    est = _context_text_tokens(system_prompt, summary, msgs)
    ratio = calibrate_ratio(s, system_prompt)
    used = int(est * ratio) + int(extra_tokens or 0) + int(tools_extra or 0)
    remain = max(0, limit - used) if limit else 0
    pct = round(used * 100.0 / limit, 1) if limit else 0.0
    parts = _context_parts(system_prompt, summary, msgs)
    for k in parts:
        parts[k] = int(parts[k] * ratio)
    parts["image"] = int(extra_tokens or 0)
    parts["tools"] = int(tools_extra or 0)
    return {
        "used": used, "limit": limit, "reserve": reserve, "remain": remain,
        "pct": pct, "ratio": round(ratio, 3), "compressed": bool(summary),
        "unlimited": limit <= 0,      # 前端据此隐藏进度环 / 显示「不限」
        "compress_marks": compress_marks_for(s),
        "parts": parts,
    }


def _compact_history_assistant(m, digest_chars=HISTORY_PROCESS_DIGEST):
    """把旧回合的「过程旁白」压成摘要，只保留最后一段（真正答案）。

    assistant 的 content 是所有轮次正文的拼接，过程叙述占大头；更早的回合
    保留全文只会让每轮请求都重发同一段话。没有 blocks、只有一段正文、
    或 blocks 覆盖不全（旧格式）时返回 None，保持原文。
    """
    blocks = m.get("blocks") or []
    texts = [str(b.get("text") or "").strip() for b in blocks
             if isinstance(b, dict) and b.get("kind") == "text"]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        return None
    content = str(m.get("content") or "")
    # blocks 只覆盖了部分正文（V2 旧消息）→ 不压缩，避免丢内容
    if content and sum(len(t) for t in texts) < len(content) * 0.6:
        return None
    proc = re.sub(r"\s+", " ", " ".join(texts[:-1])).strip()
    if len(proc) > digest_chars:
        half = max(40, digest_chars // 2)
        proc = proc[:half].rstrip() + "…" + proc[-half:].lstrip()
    return "【本回合过程摘要】" + proc + "\n\n" + texts[-1]


def _history_compact_cfg():
    """读取历史过程压缩配置；读不到就用默认值（不阻塞对话）。"""
    try:
        cfg = load_config() or {}
    except Exception:                      # noqa: BLE001  配置坏了不能拦住对话
        cfg = {}
    try:
        keep = int(cfg.get("history_process_keep_turns", HISTORY_PROCESS_KEEP_TURNS))
    except (TypeError, ValueError):
        keep = HISTORY_PROCESS_KEEP_TURNS
    try:
        digest = int(cfg.get("history_process_digest", HISTORY_PROCESS_DIGEST))
    except (TypeError, ValueError):
        digest = HISTORY_PROCESS_DIGEST
    return max(0, keep), max(0, digest)


def build_context_messages(s, system_prompt):
    """按「系统提示 + 摘要 + 压缩点之后的原文」拼出真正发给模型的消息列表"""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if s.get("summary"):
        block = summary_block(s["summary"])
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += "\n\n" + block
        else:
            messages.insert(0, {"role": "system", "content": block})
    cut = int(s.get("summary_upto") or 0)
    msgs = s.get("messages", [])[cut:]
    keep_turns, digest = _history_compact_cfg()
    compact_on = keep_turns > 0 and digest > 0      # 任一项为 0 = 关闭
    keep = set()
    if compact_on:
        asst = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]
        keep = set(asst[-keep_turns:])
    for i, m in enumerate(msgs):
        text = _msg_text(m)
        if compact_on and m.get("role") == "assistant" and i not in keep:
            compact = _compact_history_assistant(m, digest)
            if compact:
                text = compact
        messages.append({"role": m["role"], "content": text})
    return messages


def _is_tool_msg(m):
    return (isinstance(m, dict) and m.get("role") == "user"
            and str(m.get("content") or "").startswith(TOOL_MSG_PREFIX))


def keep_from_by_tokens(msgs, cut, limit, keep_ratio=None, calib=1.0):
    """按 token 预算决定压缩边界：从末尾往前累加，保留约 16% 输入窗口的原文。
    至少保留 COMPRESS_KEEP_MIN 条，最多回溯 COMPRESS_KEEP_MAX 条。
    边界会向前对齐到发起工具调用的 assistant，避免把「调用 + 结果」切开。
    calib 是「真实 / 估算」校准因子，和 session_context 用同一个 ratio。"""
    try:
        ratio = float(keep_ratio or COMPRESS_KEEP_RATIO)
    except (TypeError, ValueError):
        ratio = COMPRESS_KEEP_RATIO
    ratio = max(0.05, min(0.6, ratio))
    try:
        calib = float(calib or 1.0)
    except (TypeError, ValueError):
        calib = 1.0
    calib = max(0.2, min(5.0, calib))
    budget = max(1, int(limit * ratio))
    total = len(msgs)
    used = 0
    i = total - 1
    while i >= cut:
        count = total - i
        if count > COMPRESS_KEEP_MAX:
            break
        t = _msg_tokens(msgs[i]) * calib
        if count > COMPRESS_KEEP_MIN and used + t > budget:
            break
        used += t
        i -= 1
    keep_from = i + 1
    # 切点落在工具结果上：向前找到发起调用的 assistant，让它一起保留
    guard = 0
    while cut < keep_from < total and _is_tool_msg(msgs[keep_from]) and guard < 20:
        guard += 1
        j = keep_from - 1
        while j > cut and msgs[j].get("role") != "assistant":
            j -= 1
        if j <= cut:
            break
        keep_from = j
    return cut if keep_from <= cut else keep_from


def _split_summary(text):
    """把模型输出拆成（摘要, 记忆要点）。格式不符时整段当摘要。"""
    raw = (text or "").strip()
    points = []
    hit = re.search(r"【记忆要点】", raw)
    if hit:
        body = raw[hit.end():]
        raw = raw[:hit.start()]
        for line in body.splitlines():
            line = line.strip().lstrip("-*·").strip()
            if line and line not in ("（无）", "(无)", "无"):
                points.append(line[:200])
    raw = re.sub(r"^【摘要】\s*", "", raw.strip())
    return raw.strip(), points[:6]


def manual_keep_budget(msgs, cut=0, system_prompt=""):
    """手动压缩「保留最近多少」的预算（tokens）。

    有窗口时按「窗口 − 输出上限」算；不限窗口时按当前历史本身的估算量算，
    不再引入 128k 这类假想窗口。
    """
    try:
        limit = context_limits()[0]
    except Exception:                       # noqa: BLE001  读配置失败按不限处理
        limit = 0
    if limit > 0:
        return max(1, limit - effective_output_cap(limit))
    est = _context_text_tokens(system_prompt, "", (msgs or [])[int(cut or 0):])
    return max(1, est)


def compress_history(s, system_prompt=""):
    """把较早的对话总结成摘要，写入 s['summary'] / s['summary_upto']。
    成功返回 True；可压缩消息不足或调用失败返回 False。
    摘要用「轻量模型」生成（未配置 light_model 时自动沿用主模型）。

    system_prompt 会原样放在请求最前面，被压缩区间按原 role 回放：
    这样摘要请求的前缀与主请求的早期部分一致，能命中提供方的热前缀缓存
    （dsh 的 compaction-basic 也是同样做法）。"""
    msgs = s.get("messages", [])
    cut = int(s.get("summary_upto") or 0)
    # 保留预算按 dsh 口径：(窗口 - 输出上限) * keep_ratio，并保底 8k token。
    # 不限窗口时按当前历史量算（不引入假想窗口）。
    _input_window = manual_keep_budget(msgs, cut, system_prompt)
    try:
        _cfg = load_config() or {}
        _keep_ratio = float(_cfg.get("compress_keep_ratio") or COMPRESS_KEEP_RATIO)
        _sum_tokens = int(_cfg.get("compress_summary_tokens") or SUMMARY_MAX_TOKENS)
    except (TypeError, ValueError):
        _keep_ratio, _sum_tokens = COMPRESS_KEEP_RATIO, SUMMARY_MAX_TOKENS
    keep_from = keep_from_by_tokens(msgs, cut, _input_window, _keep_ratio,
                                    calibrate_ratio(s, system_prompt))
    if keep_from <= cut:
        return False                      # 可压缩的旧消息不足
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return False
    # PreCompact：钩子可以在压缩前追加「必须保住」的内容
    try:
        from app import hooks as _hooks
        _hpre = _hooks.pre_compact(str(s.get("id") or ""),
                                   str(s.get("sandbox") or ""))
    except Exception:                        # noqa: BLE001  钩子坏了不能拦住压缩
        _hpre = {}
    lines = []
    for _c in (_hpre.get("contexts") or []):
        lines.append("【hook 追加】" + _c)
    if s.get("summary"):
        lines.append("【已有摘要】" + s["summary"])
    # pin：当前任务目标 + 验收项 + 当前计划。它们可能落在压缩区间里，
    # 再显式给一次，避免结构化摘要漏掉「为什么做 / 做到什么算完成」。
    _goal = s.get("task_goal") if isinstance(s.get("task_goal"), dict) else {}
    _goal_text = str(_goal.get("text") or "").strip()
    if _goal_text:
        lines.append("【当前任务目标（pin）】" + _goal_text[:2000])
    elif cut > 0:
        _first_user = next((m for m in msgs if m.get("role") == "user"
                            and _msg_text(m).strip() and not _is_tool_msg(m)), None)
        if _first_user:
            lines.append("【用户最初请求（pin）】" + _msg_text(_first_user)[:2000])
    _acc = s.get("acceptance") if isinstance(s.get("acceptance"), dict) else {}
    _crit = [c for c in (_acc.get("criteria") or []) if isinstance(c, dict)]
    if _crit:
        lines.append("【验收项（pin）】" + "；".join(
            str(c.get("text") or "")[:80] for c in _crit[:8]))
    # 计划卡的最新版本挂在最后一条带 plan 的 assistant 消息上（s["plan"] 是旧字段，
    # 只作兜底）。以前只读 s["plan"]，但没有任何地方写它，这一块等于从未生效。
    _plan = None
    for _m in reversed(s.get("messages") or []):
        if (isinstance(_m, dict) and _m.get("role") == "assistant"
                and isinstance(_m.get("plan"), dict) and _m["plan"].get("steps")):
            _plan = _m["plan"]
            break
    if not _plan and isinstance(s.get("plan"), dict):
        _plan = s["plan"]
    if _plan:
        try:
            lines.append("【当前计划（pin）】"
                         + json.dumps(_plan, ensure_ascii=False)[:1500])
        except (TypeError, ValueError):
            pass
    # 任务状态（最近改动 / 未解决错误 / 素材 / 视觉基线 / 决策）：
    # 这些在摘要里很容易被压掉，原样 pin，压缩后仍能继续干活。
    try:
        from app import task_state
        _led = task_state.text(s, max_chars=2000)
    except Exception:
        _led = ""
    if _led:
        lines.append(_led)
    replay = []
    for m in msgs[cut:keep_from]:
        text = _msg_text(m).strip()
        if not text:
            continue
        if len(text) > COMPRESS_MSG_CHARS:
            text = (text[:COMPRESS_MSG_CHARS]
                    + "…（后略 %d 字）" % (len(text) - COMPRESS_MSG_CHARS))
        replay.append({"role": "assistant" if m.get("role") == "assistant" else "user",
                       "content": text})
    if not replay and not lines:
        return False
    req = []
    if system_prompt:
        req.append({"role": "system", "content": system_prompt})
    if lines:
        req.append({"role": "user", "content": "\n".join(lines)})
    req.extend(replay)
    req.append({"role": "user", "content": COMPRESS_PROMPT})
    try:
        from app import llm               # 延迟导入：llm 依赖 agent，避免模块级循环
        raw = llm._quick_chat(api_key, api_url, api_model, req,
                              max_tokens=max(256, _sum_tokens), source="summary")
    except Exception as e:
        print(f"[压缩] 失败：{e}")
        return False
    summary, points = _split_summary(raw)
    if not summary:
        return False
    _orig_chars = sum(len(str(m.get("content") or "")) for m in replay
                      if isinstance(m, dict))
    if _orig_chars and len(summary) > _orig_chars:
        print("[压缩] 摘要比原文还长（%d > %d 字），放弃本次压缩"
              % (len(summary), _orig_chars))
        return False
    # 先取旧标记（旧会话没有 compress_marks 时用摘要字段补一条）。
    # 必须在写 summary_* 之前取：否则 compress_marks_for 会把「本次压缩」
    # 当成旧摘要再补一条，产生两条 at 相同、位置不同的标记，
    # 前端按 at 去重后会选中靠前的那条，分隔线就画错位置了。
    marks = s.get("compress_marks")
    if not isinstance(marks, list) or not marks:
        marks = compress_marks_for(s)
    marks = list(marks)
    _now = time.time()
    s["summary"] = summary
    s["summary_upto"] = keep_from
    s["summary_at"] = _now
    s["summary_mem"] = points          # 暂存要点，落盘后由 flush_summary_memory 写入记忆
    # 每次压缩在消息流里的位置：前端据此画「历史对话已被压缩」分隔线
    marks.append({"at": int(_now * 1000), "index": len(msgs),
                  "upto": keep_from, "count": max(0, keep_from - cut)})
    s["compress_marks"] = marks
    # 统一归一：补 key、按压缩点去重、裁剪到上限（同一压缩点只留一条）
    trim_compress_marks(s)
    return True


def save_summary(sid, s):
    """把摘要字段合并到磁盘上的最新会话。
    压缩可能耗时几十秒，期间用户可能又发了消息；直接写回旧 data 会覆盖新消息，
    所以这里重新读盘，只覆盖摘要相关的四个字段。返回合并后的会话（失败为 None）。"""
    fields = {k: s.get(k) for k in
              ("summary", "summary_upto", "summary_at", "summary_mem")}
    data = load_sessions()
    cur = find_session(data, sid)
    if not cur:
        return None
    # 压缩期间用户可能编辑 / 删除了「被压缩的那一段」：此时摘要描述的是已经不存在的
    # 内容，写回去只会让模型基于错误历史回答。校验区间指纹，变了就整体作废。
    _up = int(s.get("summary_upto") or 0)
    if _up > 0 and (_range_fp(cur.get("messages") or [], _up)
                    != _range_fp(s.get("messages") or [], _up)):
        print("[压缩] 压缩期间被压缩区间发生变化，丢弃本次摘要")
        invalidate_compression(s)
        return None
    marks = s.get("compress_marks")
    if isinstance(marks, list):
        cur_marks = cur.get("compress_marks")
        if isinstance(cur_marks, list) and cur_marks:
            # 压缩期间可能又完成了一次压缩：按 at 去重，别把磁盘上的新标记覆盖掉
            seen = {str(m.get("at") or "") for m in cur_marks if isinstance(m, dict)}
            merged = list(cur_marks)
            for m in marks:
                if isinstance(m, dict) and str(m.get("at") or "") not in seen:
                    merged.append(m)
                    seen.add(str(m.get("at") or ""))
            fields["compress_marks"] = merged[-MAX_COMPRESS_MARKS:]
        else:
            fields["compress_marks"] = marks
    cur.update(fields)
    cur["summary_upto"] = min(int(cur.get("summary_upto") or 0),
                              len(cur.get("messages") or []))
    trim_compress_marks(cur)
    cur["updated_at"] = time.time()
    with _lock:
        save_sessions(data)
    return cur


def flush_summary_memory(sid):
    """把压缩时产出的「记忆要点」写进本会话记忆。
    必须在调用方 save_sessions 之后执行：apply_memory_ops 会重新读盘，
    若早于保存，外部的旧数据会把刚写入的记忆覆盖掉。"""
    data = load_sessions()
    s = find_session(data, sid)
    if not s:
        return 0
    points = s.pop("summary_mem", None) or []
    if not points:
        return 0
    allow = bool(s.get("ai_write", True))
    with _lock:
        save_sessions(data)            # 先落盘去掉暂存字段
    if not allow:                      # 会话关闭了「允许 AI 写记忆」：只清暂存，不写入
        return 0
    try:
        from app.knowledge import apply_memory_ops
        # 记忆要点是「从累积摘要里重新提炼的全量」（compress_history 会把已有摘要一起喂进去），
        # 所以这一批应当**替换**上一批，而不是追加。
        # 以前是纯 add：每压缩一次就多 6 条「历史摘要」，还会和已有条目重复（见 NOTE 43.87）。
        old = [n for n in ((s.get("memory") or {}).get("nodes") or [])
               if str(n.get("category") or "") == "历史摘要" and n.get("id")]
        ops = [{"scope": "session", "op": "delete", "id": n["id"]} for n in old]
        ops += [{"scope": "session", "op": "add",
                 "title": p[:60], "content": p, "category": "历史摘要"}
                for p in points]
        counts, _problems, _changed = apply_memory_ops(sid, [], ops)
        return sum((counts or {}).values())
    except Exception as e:
        print(f"[压缩] 写入会话记忆失败：{e}")
        return 0


def compress_busy(sid):
    """该会话是否正在压缩（后台或手动）"""
    with _COMPRESS_LOCK:
        return sid in _COMPRESSING


def begin_compress(sid):
    """登记「该会话正在压缩」。已有压缩在跑时返回 False（调用方应放弃本次压缩）。

    后台预压缩、请求内同步压缩、手动压缩共用同一个登记表：
    以前同步路径没登记，会和后台线程同时摘要同一段历史。
    """
    sid = str(sid or "")
    if not sid:
        return False
    with _COMPRESS_LOCK:
        if sid in _COMPRESSING:
            return False
        _COMPRESSING.add(sid)
    return True


def end_compress(sid):
    """释放压缩登记（必须放在 finally 里）。"""
    with _COMPRESS_LOCK:
        _COMPRESSING.discard(str(sid or ""))


def _range_fp(msgs, upto):
    """被压缩区间的内容指纹：期间用户编辑 / 删除了这一段，摘要就必须作废。"""
    h = hashlib.sha1()
    for m in list(msgs or [])[:max(0, int(upto or 0))]:
        if not isinstance(m, dict):
            h.update(str(m).encode("utf-8"))
            h.update(b"\x01")
            continue
        h.update(str(m.get("role") or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(_msg_text(m).encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def maybe_precompress(sid, system_tokens, extra_tokens=0, system_prompt=""):
    """占用达到 `request_budget()` 时在后台压缩；本轮请求不等待。

    预算同时受「窗口 * compress_ratio」和「窗口 - 输出上限 - headroom」约束
    （对齐 dsh），不再只看 80% —— 否则输出上限一大，提示词就会顶着窗口跑。
    system_prompt 会原样回放到摘要请求里，让它能命中主请求的前缀缓存。
    返回是否真的起了后台任务。"""
    if not sid:
        return False
    if not begin_compress(sid):
        return False

    def work():
        try:
            data = load_sessions()
            s = find_session(data, sid)
            if not s:
                return
            budget = request_budget()
            if budget <= 0:          # 不限窗口：不做后台预压缩
                return
            used = (session_context(s, "")["used"]
                    + int(system_tokens or 0) + int(extra_tokens or 0))
            if used < budget:
                return
            if not compress_history(s, system_prompt):
                return
            cur = save_summary(sid, s)
            if not cur:
                return
            flush_summary_memory(sid)
            ctx = session_context(cur, "")
            ctx["used"] += int(system_tokens or 0) + int(extra_tokens or 0)
            ctx["remain"] = max(0, ctx["limit"] - ctx["used"])
            ctx["pct"] = round(ctx["used"] * 100.0 / ctx["limit"], 1) if ctx["limit"] else 0.0
            ctx["parts"]["system"] += int(system_tokens or 0)
            ctx["parts"]["image"] += int(extra_tokens or 0)
            try:
                from app import events as events_bus
                events_bus.publish({"type": "context_update",
                                    "session_id": sid, "context": ctx})
            except Exception as e:
                print(f"[压缩] 推送进度失败：{e}")
        except Exception as e:
            print(f"[压缩] 后台预压缩失败：{e}")
        finally:
            end_compress(sid)

    threading.Thread(target=work, daemon=True).start()
    try:
        from app import events as events_bus
        events_bus.publish({"type": "context_compressing", "session_id": sid})
    except Exception as e:
        print(f"[压缩] 推送开始事件失败：{e}")
    return True


_TOOL_HEAD_RE = re.compile(r"^【([^】]{1,40})】")
_TARGET_PREFIXES = ("文件：", "命令：", "目录：", "URL:", "路径：", "工作目录：")


def _fold_summary(text, fold_chars=240):
    """折叠旧工具结果时保留「工具名 + 目标 + 首尾 + 关键行」，而不是只留 100 字。

    这些信息足够模型判断「这个结果里有没有我要的东西」，需要细节再用
    read_tool_result / read_file 取回；比统一截断更不容易让它重跑一遍。
    """
    body = str(text or "").replace(TOOL_MSG_PREFIX, "").strip()
    m = _TOOL_HEAD_RE.match(body)
    name = m.group(1) if m else "工具"
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if name == "web_search":
        srcs = [ln for ln in lines if re.match(r"^\[\d+\]\s", ln)][:8]
        out = "【web_search】已折叠，共 %d 字；保留来源：%s" % (
            len(text), "；".join(srcs) or "无")
    else:
        target = next((ln[:80] for ln in lines[:8]
                       if ln.startswith(_TARGET_PREFIXES)), "")
        keys = [ln for ln in lines
                if any(k in ln for k in ("错误", "失败", "退出码", "Error", "error"))][:2]
        head = " ".join(lines[:2])[:100]
        tail = " ".join(lines[-2:])[:100]
        parts = ["【%s】已折叠，共 %d 字" % (name, len(text))]
        if target:
            parts.append(target)
        if head and head not in target:
            parts.append("开头：" + head)
        if keys:
            parts.append("关键：" + "；".join(x[:70] for x in keys))
        if tail and tail != head:
            parts.append("结尾：" + tail)
        out = "；".join(parts)
    try:
        cap = int(fold_chars or 0) or 240
    except (TypeError, ValueError):
        cap = 240
    return out[:max(80, cap)]


def real_used_tokens(last_prompt, messages, since=0, calib=1.0):
    """真实 token 的近似值：上一轮真实 prompt_tokens + 之后新增内容的估算 × 校准系数。

    这是「事后真实 + 增量估算」口径：基准来自服务商返回的 usage，只有新增的那几条
    消息是估算，误差远小于拿整段历史去猜。last_prompt<=0（没拿到 usage）时返回 0，
    调用方应退回纯估算。
    """
    try:
        base = int(last_prompt or 0)
    except (TypeError, ValueError):
        base = 0
    if base <= 0:
        return 0
    try:
        since = max(0, int(since or 0))
    except (TypeError, ValueError):
        since = 0
    added = estimate_messages_tokens((messages or [])[since:]) if since < len(messages or []) else 0
    try:
        k = float(calib or 1.0)
    except (TypeError, ValueError):
        k = 1.0
    return base + int(added * max(1.0, k))


def trim_tool_results(messages, budget_tokens, keep_recent=2, fold_chars=240):
    """把较早的工具结果替换成结构化占位，直到回到预算内。

    清理单位是**单条工具结果**（对话里一条以 TOOL_MSG_PREFIX 开头的 user 消息
    就是一个结果，见 chat.py 的回灌），保留最近 `keep_recent` 条，其余折成
    `fold_chars` 以内的摘要；原文仍可通过落盘句柄 read_tool_result 取回。
    不调用模型、不改会话数据；返回被清理的条数。"""
    def _is_tool(m):
        c = m.get("content") if isinstance(m, dict) else None
        return (isinstance(m, dict) and m.get("role") == "user"
                and isinstance(c, str) and c.startswith(TOOL_MSG_PREFIX))

    idxs = [i for i, m in enumerate(messages) if _is_tool(m)]
    try:
        keep_recent = max(0, int(keep_recent or 0))
    except (TypeError, ValueError):
        keep_recent = 2
    if len(idxs) <= keep_recent:
        return 0
    total = estimate_messages_tokens(messages)
    if total <= budget_tokens:
        return 0
    dropped = 0
    for i in (idxs[:-keep_recent] if keep_recent else idxs):
        if total <= budget_tokens:
            break
        text = messages[i].get("content") or ""
        placeholder = (TOOL_MSG_PREFIX + "\n（较早的工具结果已折叠）"
                       + _fold_summary(text, fold_chars))
        total -= estimate_tokens(text) - estimate_tokens(placeholder)
        messages[i] = {"role": "user", "content": placeholder}
        dropped += 1
    return dropped


def trim_history_to_budget(messages, budget_tokens):
    """同步压缩失败时的兜底：保留 system 与最近消息，临时丢弃中间部分。
    只作用于本次请求的临时列表，不落库；返回 (新列表, 丢弃条数)。"""
    if estimate_messages_tokens(messages) <= budget_tokens:
        return messages, 0
    sys_msgs = [m for m in messages if m.get("role") == "system"][:1]
    rest = [m for m in messages if m.get("role") != "system"]
    keep = []
    used = estimate_messages_tokens(sys_msgs)
    for m in reversed(rest):
        t = _msg_tokens(m)
        if keep and used + t > budget_tokens:
            break
        keep.append(m)
        used += t
    keep.reverse()
    dropped = len(rest) - len(keep)
    if dropped <= 0:
        return messages, 0
    note = {"role": "user", "content":
            "（提示：历史过长，本轮临时省略了较早的 %d 条消息；"
            "它们仍保存在本地会话里，压缩成功后即可恢复。建议压缩历史或新建会话。）" % dropped}
    return sys_msgs + [note] + keep, dropped


def recover_overflow(messages, budget_tokens, fold_chars=240):
    """上下文超窗时的本地恢复：先折叠旧工具结果，再丢中间。
    只作用于本次请求的临时列表，不落库；返回 (新列表, 折叠条数, 丢弃条数)。"""
    folded = trim_tool_results(messages, budget_tokens, keep_recent=1,
                               fold_chars=fold_chars)
    dropped = 0
    if estimate_messages_tokens(messages) > budget_tokens:
        messages, dropped = trim_history_to_budget(messages, budget_tokens)
    return messages, folded, dropped


def summary_info(s):
    """压缩详情：摘要正文、压缩点、条数、时间"""
    msgs = s.get("messages") or []
    cut = int(s.get("summary_upto") or 0)
    text = s.get("summary") or ""
    return {
        "summary": text,
        "upto": cut,
        "compressed": cut,
        "remaining": max(0, len(msgs) - cut),
        "total": len(msgs),
        "chars": len(text),
        "tokens": estimate_tokens(text),
        "at": float(s.get("summary_at") or 0),
    }


def last_input_detail(s):
    """最近一条「有逐轮明细」的助手回复的 token 明细。

    返回 {index, at, usage, rounds, total_ms, rounds_detail, source}：
    - rounds_detail 优先用消息里落盘的 timing.rounds_detail（新消息）；
    - 旧消息没有时，按 done_at 去 token_usage.jsonl 尾部找同一回合的明细；
    - 最新一条没有明细时继续往前找，只有全都找不到才把最新一条作为 fallback。
    """
    msgs = (s or {}).get("messages") or []
    fallback = None
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        if not isinstance(u, dict) or not u:
            continue
        t = m.get("timing") or {}
        rd = t.get("rounds_detail") or []
        source = "message"
        if not rd:
            try:
                from app import usage
                rd = usage.rounds_for_turn(m.get("done_at"), t.get("rounds") or 0,
                                           session_id=(s or {}).get("id") or "")
            except Exception:
                rd = []
            source = "usage_log" if rd else "none"
        item = {
            "index": i,
            "at": float(m.get("done_at") or 0),
            "usage": u,
            "rounds": int(t.get("rounds") or 0),
            "total_ms": int(t.get("total_ms") or 0),
            "rounds_detail": rd,
            "source": source,
        }
        if rd:
            return item
        if fallback is None:
            fallback = item
    return fallback


def restore_summary(s):
    """撤销压缩：清空摘要与压缩点，让原文重新进入上下文。返回恢复的条数。"""
    if not s.get("summary") and not int(s.get("summary_upto") or 0):
        return 0
    return invalidate_compression(s)


def current_context(data):
    """当前会话的上下文占用（含系统提示），供 /api/state 与各会话接口回传。
    注意：此处只算「手动偏好」，与 _api_chat 实际发送的提示保持一致；
    记忆/画像由检索按需注入，无法在会话外复算，故不计入。"""
    cur = find_session(data, (data or {}).get("current"))
    if not cur:
        return None
    system_prompt = manual_block(load_profile())
    return session_context(cur, system_prompt)
