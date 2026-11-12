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

from app import attachments
from app.config import (MAX_RESERVE, get_api_conf, hybrid_on, is_local_url,
                        light_chat_is_local, light_chat_mode, load_config,
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
HISTORY_REBUILD_STEP = 4         # 原文保留区的推进粒度（水位线，1 = 每轮推进）
TOOL_MSG_PREFIX = "工具执行结果如下，请据此继续："   # chat.py 回灌工具结果时的固定开头

# 压缩兜底（对齐 Claude Code）：
# - 摘要请求自己超长（prompt-too-long）时，按最旧丢一段再试；
# - 连续失败达到上限后熔断一段时间，避免每轮都白花一次请求。
COMPRESS_PTL_RETRIES = 3
COMPRESS_BREAKER_FAILS = 3
COMPRESS_BREAKER_SECS = 600
# 微压缩（对齐 CC 的 microcompact）：上下文到 60% 预算后，对单条过大的工具结果
# 做一次轻量摘要；只影响本次请求，原文仍在会话 / 落盘句柄里。
MICROCOMPACT_MIN_TOKENS = 3000
MICROCOMPACT_TRIGGER_RATIO = 0.6
MICROCOMPACT_KEEP_RECENT = 2
MICROCOMPACT_MAX_PER_PASS = 3
MICROCOMPACT_INPUT_CHARS = 12000
MICROCOMPACT_MAX_CHARS = 1200
_MICRO_CACHE = {}
_MICRO_CACHE_MAX = 128
_MICRO_LOCK = threading.Lock()
# 会话工作记忆复用的新鲜度上限（秒）：太旧就重新摘要，避免拿过期结论接续。
SESSION_MEMORY_MAX_AGE = 6 * 3600
SESSION_MEMORY_MIN_CHARS = 200
_COMPRESS_FAILS = {}                # sid -> [连续失败次数, 熔断截止时间]
_COMPRESS_FAILS_LOCK = threading.Lock()

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
_COMPRESS_CANCEL = {}       # sid -> Event：/api/stop 可以取消后台压缩
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
    # 仅轻量模型 + 本地服务：窗口按本地模型的实际上下文配置（默认 12k）；
    # 混合模式：用 hybrid_context_limit；关闭 / 云端轻量模型：沿用用户填的窗口。
    if light_chat_is_local(cfg):
        try:
            limit = int(cfg.get("local_context_limit") or 12000)
        except (TypeError, ValueError):
            limit = 12000
    elif hybrid_on(cfg) and not light_chat_mode(cfg):
        try:
            _hl = int(cfg.get("hybrid_context_limit") or 0)
        except (TypeError, ValueError):
            _hl = 0
        if _hl > 0:
            limit = _hl
    if limit <= 0:
        # 不限窗口（设置里没填）：不按比例算预算，压缩逻辑全部不触发
        return 0, 0
    _res_raw = (cfg.get("local_reserve_output") if light_chat_is_local(cfg)
                else cfg.get("chat_reserve_output"))
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
    真实值仍靠 `usage.prompt` 校准（calibrate_ratio），这里只负责给压缩阈值一个稳定基线。

    带结果缓存：同一个会话的同一批消息每轮会被估算好几次（1255 条消息实测 61ms/次），
    而字符串是不可变的、Python 还会把 hash 缓存在 str 对象里，重复查询接近 O(1)。
    缓存按「总字符数」封顶，避免把已经释放的大段工具结果一直拽在内存里。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    hit = _TOKEN_CACHE.get(text)
    if hit is not None:
        return hit
    cjk = alnum = space = punct = other = 0
    for ch in text:
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
    n = round(cjk * 0.6 + alnum * 0.28 + space * 0.1
              + punct * 0.35 + other * 0.8)
    _token_cache_put(text, n)
    return n


_TOKEN_CACHE = {}
_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE_MAX = 4096
_TOKEN_CACHE_CHARS = 0
_TOKEN_CACHE_CHAR_LIMIT = 8 * 1024 * 1024     # 缓存里字符串总长度上限


def _token_cache_put(key, val):
    global _TOKEN_CACHE_CHARS
    with _TOKEN_CACHE_LOCK:
        if key in _TOKEN_CACHE:
            _TOKEN_CACHE[key] = val
            return
        if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
            for k in list(_TOKEN_CACHE)[: _TOKEN_CACHE_MAX // 4]:
                _TOKEN_CACHE_CHARS -= len(k)
                _TOKEN_CACHE.pop(k, None)
        _TOKEN_CACHE[key] = val
        _TOKEN_CACHE_CHARS += len(key)
        while _TOKEN_CACHE_CHARS > _TOKEN_CACHE_CHAR_LIMIT and _TOKEN_CACHE:
            k = next(iter(_TOKEN_CACHE))
            _TOKEN_CACHE_CHARS -= len(k)
            _TOKEN_CACHE.pop(k, None)


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


def _msg_text(m, refs_handle=False):
    """一条消息真正发给模型的文本：正文 + 图片描述 + 附件正文。
    估算与拼装必须共用这一个函数，否则进度条会低估附件体积。

    refs_handle=True 时，`@文件` 引用的正文不再内联，只留一行定位信息
    （省费模式：历史里的引用正文由 read_file 按需取回）。"""
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
    # 文档附件：新消息把每份附件的正文存在 docs[].text；旧消息只有 doc_text（第一份）
    _docs = m.get("docs") if isinstance(m.get("docs"), list) else None
    if _docs:
        for _d in _docs:
            if not isinstance(_d, dict):
                continue
            fn = str(_d.get("name") or "附件")
            body = str(_d.get("text") or "")
            if body:
                text = (text + "\n\n【附件：" + fn + "】\n" + body) if text \
                       else ("【用户上传了附件 " + fn + "】\n" + body)
            elif _d.get("error"):
                note = "（未能读取：%s）" % _d.get("error")
                text = (text + "\n\n【附件：" + fn + "】" + note) if text \
                       else ("【用户上传了附件 " + fn + "】" + note)
    elif m.get("doc_text"):
        fn = (m.get("doc") or {}).get("name") or "附件"
        text = (text + "\n\n【附件：" + str(fn) + "】\n" + m["doc_text"]) if text \
               else ("【用户上传了附件 " + str(fn) + "】\n" + m["doc_text"])
    # `@文件` 引用：正文在发送时由 app/fileref.py 抽好挂在消息上。
    # 估算与拼装共用本函数，所以引用体积也会被算进上下文占用。
    for r in (m.get("refs") or []):
        if not isinstance(r, dict):
            continue
        page = (" 第 %d 页" % r["page"]) if r.get("page") else ""
        if r.get("start"):
            sym = (" · " + str(r.get("symbol"))) if r.get("symbol") else ""
            head = "【引用代码：%s:%s-%s%s】" % (
                r.get("path") or "", r.get("start"), r.get("end") or "", sym)
        else:
            head = "【引用文件：%s%s】" % (r.get("path") or "", page)
        if refs_handle or r.get("slim"):
            # 省费模式 / 低置信自动召回：正文不内联，只给「在哪、怎么取回」。
            # 信息没有丢，只是从「每轮重发」改成「按需读取」。
            line = head + "（正文未内联；需要时用 read_file 按上面的路径与行号取回）"
            text = (text + "\n\n" + line) if text else line
            continue
        body = r.get("text") or ("（未能读取：%s）" % (r.get("warn") or "未知原因"))
        text = (text + "\n\n" + head + "\n" + body) if text else (head + "\n" + body)
    # 图片 / 文档附件：给模型一行可回看的 id（正文由 attachment_read 按需取，
    # 不常驻上下文；vision 模式下识别结果已在上面注入过）
    refs = attachments.ref_line(m)
    if refs:
        text = (text + "\n" + refs) if text else refs
    return text


def message_text(m):
    """公开给会话工作记忆用的「消息真实文本」（与估算 / 拼装同一口径）。"""
    return _msg_text(m)


def _msg_tokens(m, refs_handle=False):
    n = MSG_OVERHEAD + estimate_tokens(_msg_text(m, refs_handle))
    content = m.get("content") if isinstance(m, dict) else None
    if isinstance(content, list):
        n += IMAGE_TOKENS * sum(1 for p in content
                                if isinstance(p, dict) and p.get("type") == "image_url")
    return n


def _context_text_tokens(system_prompt, summary, msgs, refs_handle_from=None):
    """估算一次请求上下文的 token 数（system + 摘要 + 消息）

    refs_handle_from：从这个下标起，引用正文才内联；更早的只算句柄。
    None = 全部内联（旧行为）。"""
    n = 0
    if system_prompt:
        n += MSG_OVERHEAD + estimate_tokens(system_prompt)
    if summary:
        n += MSG_OVERHEAD + estimate_tokens(summary_block(summary))
    for i, m in enumerate(msgs):
        n += _msg_tokens(m, refs_handle_from is not None and i < refs_handle_from)
    return n


def _context_parts(system_prompt, summary, msgs, refs_handle_from=None):
    """分项统计：系统提示 / 摘要 / 对话正文 / 附件与图片描述"""
    parts = {"system": 0, "summary": 0, "dialog": 0, "attachment": 0, "image": 0}
    if system_prompt:
        parts["system"] = MSG_OVERHEAD + estimate_tokens(system_prompt)
    if summary:
        parts["summary"] = MSG_OVERHEAD + estimate_tokens(summary_block(summary))
    for i, m in enumerate(msgs):
        handle = refs_handle_from is not None and i < refs_handle_from
        raw = m.get("content") or ""
        if isinstance(raw, list):
            base = estimate_tokens(_msg_text(m, handle))
            extra = 0
        else:
            base = estimate_tokens(raw)
            extra = estimate_tokens(_msg_text(m, handle)) - base
        parts["dialog"] += MSG_OVERHEAD + base
        if extra > 0:
            parts["attachment"] += extra
    return parts


def _refs_handle_on():
    """省费模式下，历史里的引用正文只留句柄。默认开启（与 DEFAULT_CONFIG 一致）。"""
    try:
        cfg = load_config() or {}
        return str(cfg.get("token_profile") or "balanced").lower() != "quality"
    except Exception:                    # noqa: BLE001  配置读不到就按旧行为
        return False


def _refs_inline_from(msgs):
    """最后一条「真正的用户消息」的下标：它保留引用正文，更早的用句柄。"""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if str(m.get("content") or "").startswith(TOOL_MSG_PREFIX):
            continue
        return i
    return None


def working_set_tokens():
    """省费模式下的「跨回合历史原文」预算；0 = 关闭（按窗口压缩的旧行为）。

    注意这不是上下文窗口：模型的窗口与 read_file 能力都不变，
    只是不再每轮把整段历史原文全部重发。
    """
    try:
        cfg = load_config() or {}
        if str(cfg.get("token_profile") or "balanced").lower() == "quality":
            return 0
        # 本地小模型窗口只有 8–12k，工作集对它没有意义，继续走原来的窗口比例逻辑
        if light_chat_is_local(cfg):
            return 0
        return max(0, int(cfg.get("history_working_set_tokens") or 0))
    except (TypeError, ValueError):
        return 0


def _clamp_keep_from(msgs, cut, keep_from):
    """压缩切点不能越过「当前回合」：最后一条真正的用户消息及其之后永远保留原文。"""
    _cur = _refs_inline_from(msgs)
    if _cur is not None and _cur > cut:
        return min(keep_from, _cur)
    return keep_from


def history_tokens(s):
    """当前会话「会被重发的历史原文」估算（压缩点之后的消息）。"""
    cut = int((s or {}).get("summary_upto") or 0)
    msgs = ((s or {}).get("messages") or [])[cut:]
    _rf = _refs_inline_from(msgs) if _refs_handle_on() else None
    n = _context_text_tokens("", "", msgs, _rf)
    # 历史附图是真实发给模型的，也要算进「要重发多少」
    try:
        from app import imaging
        keep = int((load_config() or {}).get("image_history_keep") or 0)
        if keep > 0 and imaging.vision_mode() == "chat":
            n += IMAGE_TOKENS * history_image_count(s, keep)
    except Exception:                    # noqa: BLE001  估算失败不影响主流程
        pass
    return n


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


def message_chars(m):
    """一条消息的「正文字符数」：多模态数组只算 text 片段，绝不把 base64 算进去。"""
    if not isinstance(m, dict):
        return 0
    raw = m.get("content")
    if isinstance(raw, list):
        return len("\n".join(str(p.get("text") or "") for p in raw
                             if isinstance(p, dict) and p.get("type") == "text"))
    return len(str(raw or ""))


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
        raw = m.get("content")
        if isinstance(raw, list):
            text = "\n".join(str(p.get("text") or "") for p in raw
                             if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = str(raw or "")
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
    _refs_handle = _refs_handle_on()      # 与真正发请求时的口径保持一致
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
        _slice = msgs[cut:i]
        _rf = _refs_inline_from(_slice) if _refs_handle else None
        est = _context_text_tokens(system_prompt, s.get("summary") or "", _slice, _rf)
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
    _rf = _refs_inline_from(msgs) if _refs_handle_on() else None
    est = _context_text_tokens(system_prompt, summary, msgs, _rf)
    ratio = calibrate_ratio(s, system_prompt)
    used = int(est * ratio) + int(extra_tokens or 0) + int(tools_extra or 0)
    remain = max(0, limit - used) if limit else 0
    pct = round(used * 100.0 / limit, 1) if limit else 0.0
    parts = _context_parts(system_prompt, summary, msgs, _rf)
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


def _history_rebuild_step():
    """原文保留区的推进粒度（水位线）；读不到配置时用默认 4。"""
    try:
        cfg = load_config() or {}
        return max(1, int(cfg.get("history_rebuild_step") or HISTORY_REBUILD_STEP))
    except (TypeError, ValueError):
        return HISTORY_REBUILD_STEP


def _assistant_keep_set(msgs, keep_turns, step):
    """计算「保留原文」的 assistant 消息下标集合（水位线推进）。

    step=1 等于旧行为：每来一条新 assistant 就有一条旧消息被压成摘要，
    请求前缀每轮都在同一个位置断一次。
    step>1 时边界只在条数跨过 step 的整数倍时整体前移一次：
    边界之间只追加新消息、不改写已发出的旧消息，前缀缓存可以连续命中；
    代价是最多临时多保留 step-1 条 assistant 原文。
    """
    asst = [i for i, m in enumerate(msgs or [])
            if isinstance(m, dict) and m.get("role") == "assistant"]
    if not asst:
        return set()
    try:
        keep = max(1, int(keep_turns))
        step = max(1, int(step))
    except (TypeError, ValueError):
        return set(asst[-2:])
    if step == 1:
        return set(asst[-keep:])
    boundary = (len(asst) // step) * step
    return set(asst[max(0, boundary - keep):])


def _history_image_msgs(msgs, keep):
    """最近 keep 条「带图片的用户消息」（排除当前回合）。

    当前回合的图片由调用方单独附图，这里排除以免重复。
    """
    if keep <= 0:
        return []
    last_user = _refs_inline_from(msgs)
    cand = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if last_user is not None and i >= last_user:
            continue
        if m.get("images") or m.get("image"):
            cand.append(m)
    return cand[-int(keep):]


def _msg_image_count(m, per_msg=2):
    imgs = m.get("images")
    if not isinstance(imgs, list) or not imgs:
        imgs = [m.get("image")] if isinstance(m.get("image"), dict) else []
    return min(max(1, int(per_msg or 1)), len(imgs))


def history_image_count(s, keep, per_msg=2):
    """历史里会被重新附图的图片数量（供上下文估算补 IMAGE_TOKENS）。"""
    params = _session_cut(s)
    return sum(_msg_image_count(m, per_msg) for m in _history_image_msgs(params, keep))


def _session_cut(s):
    cut = int((s or {}).get("summary_upto") or 0)
    return ((s or {}).get("messages") or [])[cut:]


def build_context_messages(s, system_prompt, image_loader=None, image_keep=0):
    """按「系统提示 + 摘要 + 压缩点之后的原文」拼出真正发给模型的消息列表。

    image_loader(m, text) -> content parts：省费/多模态模式下，把最近几条带图
    消息的图片重新作为多模态内容附上。否则历史里只剩一行「可回看附件」，
    模型看不到图就只能要求用户重新发送。
    """
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
    _rf = _refs_inline_from(msgs) if _refs_handle_on() else None
    keep_turns, digest = _history_compact_cfg()
    compact_on = keep_turns > 0 and digest > 0      # 任一项为 0 = 关闭
    keep = set()
    if compact_on:
        # 水位线推进：边界之间只追加、不改写旧消息（前缀缓存可连续命中）
        keep = _assistant_keep_set(msgs, keep_turns, _history_rebuild_step())
    # 最近几条带图消息：把图片重新附上（当前回合由调用方单独附图）
    _img_set = set()
    if image_loader and image_keep > 0:
        _by_msg = _history_image_msgs(msgs, image_keep)
        _img_set = {id(m) for m in _by_msg}
    for i, m in enumerate(msgs):
        text = _msg_text(m, _rf is not None and i < _rf)
        if compact_on and m.get("role") == "assistant" and i not in keep:
            compact = _compact_history_assistant(m, digest)
            if compact:
                text = compact
        if id(m) in _img_set:
            try:
                parts = image_loader(m, text)
            except Exception:            # noqa: BLE001  附图失败退回纯文本
                parts = None
            if parts:
                messages.append({"role": m["role"], "content": parts})
                continue
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
    窗口远大于当前历史时（默认窗口是 1 亿），按窗口算会让「保留 16%」变成
    “全部保留”，手动压缩永远提示可压缩内容不足；这里再以实际历史为上限。
    """
    try:
        _ws = working_set_tokens()
    except Exception:                       # noqa: BLE001
        _ws = 0
    _msgs = (msgs or [])[int(cut or 0):]
    est = _context_text_tokens(system_prompt, "", _msgs)
    if _ws > 0:
        # 省费模式：保留预算直接取工作集（历史原文上限），与窗口大小解耦。
        return max(1, min(_ws, est))
    try:
        limit = context_limits()[0]
    except Exception:                       # noqa: BLE001  读配置失败按不限处理
        limit = 0
    if limit > 0:
        budget = max(1, limit - effective_output_cap(limit))
        return max(1, min(budget, est))
    return max(1, est)


# 本地小模型的压缩系统提示：只要求结构化要点，避免它把 8k 窗口写满。
COMPRESS_SYSTEM_SMALL = (
    "你是上下文压缩器。只输出中文结构化要点，不要寒暄、不要解释过程。"
    "必须保留：文件路径、函数/命令名、错误原文、未完成事项、已做决定。"
    "信息不足时写「(none)」，不要编造。")

_ANCHOR_PATH_RE = re.compile(
    r"[A-Za-z0-9_./\\-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|md|html|css|scss|"
    r"yaml|yml|toml|ini|cfg|sh|ps1|bat|rs|go|java|kt|c|h|cpp|hpp|cs|rb|php|"
    r"lua|sql|txt)\b")
_ANCHOR_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]{3,})\s*\(")
# 正文里出现的 snake_case / camelCase 标识符（函数名、字段名）也算事实锚点
_ANCHOR_IDENT_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)\b")
_ANCHOR_ERR_RE = re.compile(
    r"(?:Error|Exception|Traceback|HTTP\s*\d{3}|退出码\s*\d+|exit code\s*\d+)[^\n]{0,100}")
_ANCHOR_STOP = {
    "if", "for", "while", "print", "len", "str", "int", "list", "dict", "set",
    "open", "range", "return", "def", "function", "async", "await", "catch",
    "then", "map", "filter", "foreach", "push", "split", "join", "replace",
    "json", "string", "number", "object", "array", "console", "window",
}


def _extract_anchors(text):
    """提取「压缩后必须还能看到」的事实锚点：路径 / 调用名 / 错误串。"""
    text = str(text or "")
    out = []
    seen = set()
    for rx in (_ANCHOR_PATH_RE, _ANCHOR_IDENT_RE, _ANCHOR_CALL_RE, _ANCHOR_ERR_RE):
        for m in rx.finditer(text):
            v = (m.group(1) if rx in (_ANCHOR_CALL_RE, _ANCHOR_IDENT_RE)
                 else m.group(0)).strip()
            if not v or v.lower() in _ANCHOR_STOP or len(v) < 4:
                continue
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
            if len(out) >= 200:
                return out
    return out


def _anchor_weight(anchor):
    """事实锚点权重：文件路径 / 错误串最贵，普通标识符只作参考。"""
    try:
        if _ANCHOR_PATH_RE.fullmatch(anchor):
            return 3
        if _ANCHOR_ERR_RE.match(anchor):
            return 3
    except Exception:                     # noqa: BLE001
        pass
    return 1


def _summary_quality_ok(replay_text, summary, pins, cfg):
    """本地摘要的事实保留闸门：加权召回不足就回退主模型。"""
    anchors = _extract_anchors(replay_text)
    if len(anchors) < 8:                   # 锚点太少，无从判断，放行
        return True
    text = str(summary or "") + "\n" + str(pins or "")
    total = sum(_anchor_weight(a) for a in anchors)
    hit = sum(_anchor_weight(a) for a in anchors if a in text)
    ratio = hit / float(total or 1)
    _need = cfg.get("hybrid_quality_gate")
    try:
        need = float(0.55 if _need is None else _need)
    except (TypeError, ValueError):
        need = 0.55
    if need <= 0:                          # 0 = 关闭闸门（只用于诊断）
        return True
    if ratio < need:
        print("[压缩] 本地摘要事实召回 %.0f%% < %.0f%%，回退主模型"
              % (ratio * 100, need * 100))
        return False
    return True


def _critical_facts(text):
    """从原文里抽「必须原样保留」的文件路径与错误串。"""
    text = str(text or "")
    paths, errs = [], []
    for m in _ANCHOR_PATH_RE.finditer(text):
        v = m.group(0).strip()
        if v and v not in paths:
            paths.append(v)
    for m in _ANCHOR_ERR_RE.finditer(text):
        v = m.group(0).strip()
        if v and v not in errs:
            errs.append(v)
    return paths[:40], errs[:10]


def _augment_summary(summary, replay_text):
    """本地摘要自动补一份「原文事实清单」：路径 / 错误串不依赖模型转述。"""
    paths, errs = _critical_facts(replay_text)
    miss_p = [p for p in paths if p not in summary]
    # 错误串后面常跟上下文（「退出码 1」等），命中前 16 字就算已保留
    miss_e = [e for e in errs
              if not (e in summary or (len(e) >= 16 and e[:16] in summary))]
    if not miss_p and not miss_e:
        return summary
    out = [str(summary or "").rstrip(), "", "## 关键文件与错误（自动提取）"]
    for p in miss_p[:30]:
        out.append("- `%s`" % p)
    for e in miss_e[:8]:
        out.append("- `%s`" % e.replace("`", ""))
    return "\n".join(out)


def _split_replay_chunks(replay, limit_tokens):
    """按 token 预算把回放消息切成若干块（本地 8k 上下文的安全线）。"""
    chunks, cur, cur_tokens = [], [], 0
    for m in replay or []:
        text = str((m or {}).get("content") or "")
        if not text:
            continue
        piece = ("【assistant】" if (m or {}).get("role") == "assistant" else "【user】") + text
        n = estimate_tokens(piece)
        if cur and cur_tokens + n > limit_tokens:
            chunks.append("\n\n".join(cur))
            cur, cur_tokens = [], 0
        if n > limit_tokens:
            # 单条就超预算：按字符硬切，保证本地不会 400
            step = max(1000, limit_tokens * 3)
            for i in range(0, len(piece), step):
                chunks.append(piece[i:i + step])
            continue
        cur.append(piece)
        cur_tokens += n
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _is_context_overflow_error(e):
    """压缩请求自己是不是被上游判成「上下文超长」。

    requests 的 HTTPError 默认 `str(e)` 只有状态码，真实原因在 `e.response.text`；
    两处都拼上看，才能和 llm 里那份 `_looks_like_context_overflow` 对齐。
    """
    try:
        from app import llm
    except Exception:                    # noqa: BLE001
        return False
    text = str(e or "")
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            text += "\n" + str(getattr(resp, "text", "") or "")
        except Exception:                # noqa: BLE001
            pass
    return llm._looks_like_context_overflow(text)


def _compress_blocked(sid):
    """该会话是否处于「连续压缩失败」的熔断期。"""
    sid = str(sid or "")
    if not sid:
        return False
    with _COMPRESS_FAILS_LOCK:
        rec = _COMPRESS_FAILS.get(sid)
        if not rec:
            return False
        if time.time() < float(rec[1] or 0):
            return True
        _COMPRESS_FAILS.pop(sid, None)   # 熔断到期：重新允许尝试
    return False


def _note_compress_result(sid, ok):
    """记录一次压缩结果：成功清零，连续失败到上限后熔断一段时间。"""
    sid = str(sid or "")
    if not sid:
        return
    with _COMPRESS_FAILS_LOCK:
        if ok:
            _COMPRESS_FAILS.pop(sid, None)
            return
        rec = _COMPRESS_FAILS.get(sid) or [0, 0.0]
        rec[0] = int(rec[0]) + 1
        if rec[0] >= COMPRESS_BREAKER_FAILS:
            rec[1] = time.time() + COMPRESS_BREAKER_SECS
            print(f"[压缩] 连续失败 {rec[0]} 次，熔断 {COMPRESS_BREAKER_SECS}s")
            rec[0] = 0
        _COMPRESS_FAILS[sid] = rec


def _api_compress(api_key, api_url, api_model, system_prompt, lines, replay,
                  sum_tokens, stop_event, use_light=True):
    """一次性摘要：主模型（或云端轻量模型）。

    对齐 Claude Code 的 PTL retry：摘要请求自己超长时，按最旧丢 20% 回放消息
    再试，最多 COMPRESS_PTL_RETRIES 次；其它错误不重试。
    """
    base = []
    if system_prompt:
        base.append({"role": "system", "content": system_prompt})
    if lines:
        base.append({"role": "user", "content": "\n".join(lines)})
    work = list(replay or [])
    last_err = None
    from app import llm                   # 延迟导入：llm 依赖 agent，避免模块级循环
    for attempt in range(COMPRESS_PTL_RETRIES + 1):
        req = list(base) + list(work) + [{"role": "user", "content": COMPRESS_PROMPT}]
        try:
            kw = {} if use_light else {"use_light": False}
            return llm._quick_chat(api_key, api_url, api_model, req,
                                   max_tokens=max(256, sum_tokens), source="summary",
                                   stop_event=stop_event, **kw)
        except Exception as e:            # noqa: BLE001
            last_err = e
            if (attempt < COMPRESS_PTL_RETRIES and len(work) > 1
                    and _is_context_overflow_error(e)):
                drop = max(1, int(len(work) * 0.2))
                work = work[drop:]
                print("[压缩] 摘要请求超长，丢弃最旧 %d 条后重试（%d/%d）"
                      % (drop, attempt + 1, COMPRESS_PTL_RETRIES))
                continue
            break
    print(f"[压缩] 失败：{last_err}")
    return ""


def _local_compress(api_key, api_url, api_model, lines, replay, cfg, stop_event):
    """本地小模型分块摘要；返回 None 表示需要回退主模型。"""
    from app import llm
    try:
        chunk_tokens = int(cfg.get("hybrid_light_chunk_tokens") or 3500)
    except (TypeError, ValueError):
        chunk_tokens = 3500
    chunk_tokens = max(800, min(6000, chunk_tokens))
    pins = "\n".join(lines)[:4000]
    chunks = _split_replay_chunks(replay, chunk_tokens)
    if not chunks:
        return None
    try:
        sum_tokens = int(cfg.get("hybrid_summary_tokens") or 800)
    except (TypeError, ValueError):
        sum_tokens = 800
    parts = []
    for ch in chunks:
        if stop_event is not None and stop_event.is_set():
            return None
        req = [
            {"role": "system", "content": COMPRESS_SYSTEM_SMALL},
            {"role": "user", "content": (pins + "\n\n【待压缩的较早对话】\n" + ch
                                         + "\n\n" + COMPRESS_PROMPT)},
        ]
        try:
            txt = llm._quick_chat(api_key, api_url, api_model, req,
                                  max_tokens=min(700, sum_tokens), source="summary",
                                  timeout=(10, 120), tries=1, use_light=True,
                                  stop_event=stop_event)
        except Exception as e:            # noqa: BLE001
            print(f"[压缩] 本地分块失败：{e}")
            return None
        txt = str(txt or "").strip()
        if not txt:
            return None
        parts.append(txt)
    raw = parts[0] if len(parts) == 1 else _merge_local_summaries(
        api_key, api_url, api_model, parts, sum_tokens, stop_event)
    if not raw:
        return None
    replay_text = "\n".join(str(m.get("content") or "") for m in replay)
    # 路径 / 错误串由代码补齐，避免小模型把 `app/x.py` 转述成 `x.py`
    raw = _augment_summary(raw, replay_text)
    if not _summary_quality_ok(replay_text, raw, pins, cfg):
        return None
    return raw


def _merge_local_summaries(api_key, api_url, api_model, parts, sum_tokens, stop_event):
    """多块摘要合并成一份；失败时退化为直接拼接（仍会过质量闸门）。"""
    from app import llm
    body = "\n\n".join(parts)
    req = [
        {"role": "system", "content": COMPRESS_SYSTEM_SMALL},
        {"role": "user", "content": ("【分块摘要】\n" + body
                                     + "\n\n请合并成一份同样结构的检查点，"
                                       "保留仍然有效的路径 / 错误 / 待办，去掉重复。\n\n"
                                     + COMPRESS_PROMPT)},
    ]
    try:
        txt = llm._quick_chat(api_key, api_url, api_model, req,
                              max_tokens=min(800, sum_tokens), source="summary",
                              timeout=(10, 120), tries=1, use_light=True,
                              stop_event=stop_event)
        txt = str(txt or "").strip()
        return txt or body
    except Exception:                     # noqa: BLE001
        return body


def _session_memory_ready(s, keep_from):
    """会话工作记忆能否直接当压缩摘要复用。

    条件：非空、覆盖待压区间（upto >= keep_from）、未过期、且被覆盖区间
    的内容指纹没变（用户编辑 / 撤回后不能拿旧结论接续）。
    """
    text = str(s.get("session_memory") or "").strip()
    if len(text) < SESSION_MEMORY_MIN_CHARS:
        return ""
    try:
        upto = int(s.get("session_memory_upto") or 0)
        at = float(s.get("session_memory_at") or 0)
    except (TypeError, ValueError):
        return ""
    if upto < int(keep_from or 0) or at <= 0:
        return ""
    if time.time() - at > SESSION_MEMORY_MAX_AGE:
        return ""
    fp = str(s.get("session_memory_fp") or "")
    if fp and fp != _range_fp(s.get("messages") or [], upto):
        print("[压缩] 会话工作记忆已过期（区间被改动），改走重新摘要")
        return ""
    return text


def _apply_summary(s, summary, keep_from, points, msgs, cut, stage="auto"):
    """把一份摘要写入会话字段，并补上「历史对话已被压缩」分隔线。"""
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
    s["summary_stage"] = str(stage or "auto")   # collapse / auto，便于排查
    s["summary_mem"] = points          # 暂存要点，落盘后由 flush_summary_memory 写入记忆
    # 每次压缩在消息流里的位置：前端据此画「历史对话已被压缩」分隔线
    marks.append({"at": int(_now * 1000), "index": len(msgs),
                  "upto": keep_from, "count": max(0, keep_from - cut)})
    s["compress_marks"] = marks
    # 统一归一：补 key、按压缩点去重、裁剪到上限（同一压缩点只留一条）
    trim_compress_marks(s)
    return True


def compress_history(s, system_prompt="", stop_event=None, prefer_local=True,
                     keep_ratio=None, stage="auto"):
    """把较早的对话总结成摘要，写入 s['summary'] / s['summary_upto']。
    成功返回 True；可压缩消息不足或调用失败返回 False。
    摘要默认用「轻量模型」生成；混合模式下的小区间可由本地模型分块完成。

    prefer_local=False：同步路径（发送前兜底压缩）强制用主模型，避免本地
    8k 分块串行导致的几十秒停顿；后台预压缩仍可用本地。

    stop_event：用户在压缩期间点停止时立即放弃，不再等模型返回。
    system_prompt 会原样放在请求最前面，被压缩区间按原 role 回放：
    这样摘要请求的前缀与主请求的早期部分一致，能命中提供方的热前缀缓存
    （dsh 的 compaction-basic 也是同样做法）。"""
    if stop_event is not None and stop_event.is_set():
        return False
    msgs = s.get("messages", [])
    cut = int(s.get("summary_upto") or 0)
    # 保留预算按 dsh 口径：(窗口 - 输出上限) * keep_ratio，并保底 8k token。
    # 不限窗口时按当前历史量算（不引入假想窗口）。
    _input_window = manual_keep_budget(msgs, cut, system_prompt)
    _cfg = load_config() or {}
    try:
        _keep_ratio = float(_cfg.get("compress_keep_ratio") or COMPRESS_KEEP_RATIO)
        _sum_tokens = int(_cfg.get("compress_summary_tokens") or SUMMARY_MAX_TOKENS)
    except (TypeError, ValueError):
        _keep_ratio, _sum_tokens = COMPRESS_KEEP_RATIO, SUMMARY_MAX_TOKENS
    if light_chat_is_local(_cfg):
        # 本地轻量模型：窗口只有 12k 左右，摘要必须更短、保留更少
        try:
            _keep_ratio = float(_cfg.get("local_keep_ratio") or 0.35)
            _sum_tokens = int(_cfg.get("local_summary_tokens") or 2048)
        except (TypeError, ValueError):
            pass
    elif hybrid_on(_cfg):
        # 混合模式的压缩更积极：保留比例与摘要上限单独配置，便于一键回退
        try:
            _keep_ratio = float(_cfg.get("hybrid_keep_ratio") or _keep_ratio)
            _sum_tokens = int(_cfg.get("hybrid_summary_tokens") or _sum_tokens)
        except (TypeError, ValueError):
            pass
    # 工作集模式：保留预算就是工作集本身，不再按窗口比例打折
    # （否则 40k 工作集会被 25% 的 keep_ratio 再砍成 10k）。
    if working_set_tokens() > 0 and not light_chat_is_local(_cfg):
        _keep_ratio = 1.0
    # 渐进折叠（L4）：显式传入更宽的保留比例，只压最旧的一段，保留更多近期原文。
    if keep_ratio is not None:
        try:
            _keep_ratio = float(keep_ratio)
        except (TypeError, ValueError):
            pass
    keep_from = keep_from_by_tokens(msgs, cut, _input_window, _keep_ratio,
                                    calibrate_ratio(s, system_prompt))
    # 当前回合（最后一条真正的用户消息及其之后）永远保留原文：
    # 回合内膨胀由工具结果折叠治理，不能用「摘要」把正在做的事压掉。
    keep_from = _clamp_keep_from(msgs, cut, keep_from)
    if keep_from <= cut:
        return False                      # 可压缩的旧消息不足
    # 会话工作记忆已覆盖这段且没过期：直接复用，省掉一次摘要调用（对齐 CC 的零成本压缩）。
    _sm = _session_memory_ready(s, keep_from)
    if _sm:
        print("[压缩] 使用会话工作记忆（零 API）")
        _ok = _apply_summary(s, _sm, keep_from, [], msgs, cut, stage=stage)
        _note_compress_result(s.get("id"), bool(_ok))
        return _ok
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
    # 本地混合模式：先用本地小模型分块摘要（省 API token），
    # 未配置 / 失败 / 事实锚点保留不足时回退主模型，保证输出质量不降级。
    _local_light = (hybrid_on(_cfg) and is_local_url(_cfg.get("light_url"))
                    and (_cfg.get("light_model") or "").strip())
    _range_tokens = sum(estimate_tokens(str(m.get("content") or "")) for m in replay)
    try:
        _local_max = int(_cfg.get(
            "local_max_range_tokens" if light_chat_is_local(_cfg)
            else "hybrid_local_max_range_tokens") or 12000)
    except (TypeError, ValueError):
        _local_max = 12000
    raw = None
    # 仅轻量模型时没有别的模型可退：区间再大也必须本地压完，否则下一轮必超窗
    # （代价是分块串行、耗时随区间线性增长）。
    _allow_local = (_range_tokens <= max(2000, _local_max)
                    or light_chat_mode(_cfg))
    if _local_light and prefer_local and _allow_local:
        raw = _local_compress(api_key, api_url, api_model, lines, replay, _cfg, stop_event)
        if raw is None and (light_chat_mode(_cfg)
                            or not bool(_cfg.get("hybrid_fallback_api", True))):
            _note_compress_result(s.get("id"), False)
            return False
    elif _local_light:
        # 区间太大时本地要串行跑很多块，延迟不可控：改用主模型一次压完（成本仍远小于省下的 prompt）
        print("[压缩] 待压缩区间约 %d tokens，超过本地摘要上限 %d，改用主模型"
              % (_range_tokens, _local_max))
        if light_chat_mode(_cfg):
            # 主对话就是 light，没有别的模型可回退：宁可这一轮不压，也不发必超上下文的请求
            _note_compress_result(s.get("id"), False)
            return False
    if raw is None:
        raw = _api_compress(api_key, api_url, api_model, system_prompt, lines, replay,
                            _sum_tokens, stop_event, use_light=not _local_light)
    if not raw:
        _note_compress_result(s.get("id"), False)
        return False
    if stop_event is not None and stop_event.is_set():
        return False
    summary, points = _split_summary(raw)
    if not summary:
        _note_compress_result(s.get("id"), False)
        return False
    _orig_chars = sum(len(str(m.get("content") or "")) for m in replay
                      if isinstance(m, dict))
    if _orig_chars and len(summary) > _orig_chars:
        print("[压缩] 摘要比原文还长（%d > %d 字），放弃本次压缩"
              % (len(summary), _orig_chars))
        _note_compress_result(s.get("id"), False)
        return False
    _ok = _apply_summary(s, summary, keep_from, points, msgs, cut, stage=stage)
    _note_compress_result(s.get("id"), bool(_ok))
    return _ok


def save_summary(sid, s):
    """把摘要字段合并到磁盘上的最新会话。
    压缩可能耗时几十秒，期间用户可能又发了消息；直接写回旧 data 会覆盖新消息，
    所以这里重新读盘，只覆盖摘要相关的四个字段。返回合并后的会话（失败为 None）。"""
    fields = {k: s.get(k) for k in
              ("summary", "summary_upto", "summary_at", "summary_stage",
               "summary_mem")}
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
        _COMPRESS_CANCEL[sid] = threading.Event()
    return True


def end_compress(sid):
    """释放压缩登记（必须放在 finally 里）。"""
    sid = str(sid or "")
    with _COMPRESS_LOCK:
        _COMPRESSING.discard(sid)
        _COMPRESS_CANCEL.pop(sid, None)


def cancel_compress(sid):
    """停止后台/同步压缩：只置事件，HTTP 请求会由 _quick_chat 丢弃。"""
    with _COMPRESS_LOCK:
        ev = _COMPRESS_CANCEL.get(str(sid or ""))
    if ev is not None:
        ev.set()
        return True
    return False


def compress_stop_event(sid):
    with _COMPRESS_LOCK:
        return _COMPRESS_CANCEL.get(str(sid or ""))


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


def range_fingerprint(msgs, upto):
    """公开给会话工作记忆用的区间指纹（与压缩校验同一套算法）。"""
    return _range_fp(msgs, upto)


def maybe_precompress(sid, system_tokens, extra_tokens=0, system_prompt="",
                      background=True):
    """占用达到 `request_budget()` 时在后台压缩；本轮请求不等待。

    预算同时受「窗口 * compress_ratio」和「窗口 - 输出上限 - headroom」约束
    （对齐 dsh），不再只看 80% —— 否则输出上限一大，提示词就会顶着窗口跑。
    system_prompt 会原样回放到摘要请求里，让它能命中主请求的前缀缓存。
    返回是否真的起了后台任务。"""
    if not sid:
        return False
    if not begin_compress(sid):
        return False

    stop_event = compress_stop_event(sid)

    def work():
        try:
            data = load_sessions()
            s = find_session(data, sid)
            if not s:
                return
            if _compress_blocked(sid):      # 连续失败熔断中：本轮不再白花请求
                return
            _cfg = load_config() or {}
            budget = request_budget()
            if budget <= 0:          # 不限窗口：不做后台预压缩
                return
            used = (session_context(s, "")["used"]
                    + int(system_tokens or 0) + int(extra_tokens or 0))
            if used < budget:
                # 渐进折叠（L4）：到窗口预算的 80% 先轻压一档，保留更多近期原文；
                # 到 100% 再走完整压缩。只影响窗口预算，不影响 40k 工作集。
                try:
                    _cr = float(_cfg.get("history_collapse_ratio") or 0)
                except (TypeError, ValueError):
                    _cr = 0.8
                if _cr <= 0 or used < budget * _cr:
                    return
                try:
                    _kr = float(_cfg.get("history_collapse_keep_ratio") or 0.32)
                except (TypeError, ValueError):
                    _kr = 0.32
                # 折叠后占用仍偏高时别每轮都再叫一次模型：同阶段 10 分钟内只折一次。
                try:
                    _last = float(s.get("summary_at") or 0)
                except (TypeError, ValueError):
                    _last = 0
                if (s.get("summary_stage") == "collapse" and _last
                        and time.time() - _last < 600):
                    return
                if not compress_history(s, system_prompt, stop_event=stop_event,
                                        keep_ratio=_kr, stage="collapse"):
                    return
            else:
                if not compress_history(s, system_prompt, stop_event=stop_event,
                                        stage="auto"):
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

    if not background:
        work()
        return True
    threading.Thread(target=work, daemon=True).start()
    try:
        from app import events as events_bus
        events_bus.publish({"type": "context_compressing", "session_id": sid})
    except Exception as e:
        print(f"[压缩] 推送开始事件失败：{e}")
    return True


def maybe_compact_working_set(sid, system_prompt="", stop_event=None):
    """省费模式：历史原文超过「工作集」时，在后台压到工作集以内。

    与 `maybe_precompress` 的区别：
    - 那个看的是**窗口预算**（1M 窗口要 80 万才触发）——只在快要爆窗时救火；
    - 这个看的是**每轮都要重发多少历史**——日常成本的主开关。

    后台执行、不阻塞本轮；当前回合的消息不会被压进摘要（见 compress_history）。
    返回是否真的起了后台任务。
    """
    ws = working_set_tokens()
    if ws <= 0 or not sid:
        return False
    if not begin_compress(sid):
        return False
    stop_event = stop_event or compress_stop_event(sid)

    def work():
        try:
            data = load_sessions()
            s = find_session(data, sid)
            if not s:
                return
            if _compress_blocked(sid):
                return
            if history_tokens(s) <= ws:
                return
            if not compress_history(s, system_prompt, stop_event=stop_event):
                return
            cur = save_summary(sid, s)
            if not cur:
                return
            flush_summary_memory(sid)
            ctx = session_context(cur, "")
            try:
                from app import events as events_bus
                events_bus.publish({"type": "context_update",
                                    "session_id": sid, "context": ctx})
            except Exception as e:            # noqa: BLE001
                print(f"[压缩] 推送进度失败：{e}")
        except Exception as e:                # noqa: BLE001
            print(f"[压缩] 工作集压缩失败：{e}")
        finally:
            end_compress(sid)

    threading.Thread(target=work, daemon=True).start()
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


FOLDED_MARK = "（较早的工具结果已折叠）"


def trim_tool_results(messages, budget_tokens, keep_recent=2, fold_chars=240,
                      target_tokens=None, start_index=0, stats=None):
    """把较早的工具结果替换成结构化占位，直到回到预算内。

    清理单位是**单条工具结果**（对话里一条以 TOOL_MSG_PREFIX 开头的 user 消息
    就是一个结果，见 chat.py 的回灌），保留最近 `keep_recent` 条，其余折成
    `fold_chars` 以内的摘要；原文仍可通过落盘句柄 read_tool_result 取回。
    不调用模型、不改会话数据；返回被清理的条数。

    缓存友好的两个参数（前缀缓存「改一个字节、后面全废」，所以折叠**次数**比深度更贵）：
    - target_tokens：低水位。给值时一次折到低水位，而不是折到刚好卡线；
      这样下一次触发要等体量再涨 `budget - target`，不会每轮都改历史中段。
    - start_index：回合内游标，只扫这个下标之后的工具消息，已处理区间不再重复扫。
      折叠点因此只前进、不回退。
    - stats：可选 dict；回填 `{"cursor": 最后一条被折消息的下标 + 1}`，供调用方保存游标。
    """
    def _is_tool(m):
        c = m.get("content") if isinstance(m, dict) else None
        return (isinstance(m, dict) and m.get("role") == "user"
                and isinstance(c, str) and c.startswith(TOOL_MSG_PREFIX))

    def _already(m):
        # 已经折过一次的占位不要再折：内容不会变，只会白白拉长扫描时间
        c = m.get("content") if isinstance(m, dict) else ""
        return isinstance(c, str) and FOLDED_MARK in c[:80]

    try:
        begin = max(0, int(start_index or 0))
    except (TypeError, ValueError):
        begin = 0
    idxs = [i for i, m in enumerate(messages) if i >= begin and _is_tool(m) and not _already(m)]
    try:
        keep_recent = max(0, int(keep_recent or 0))
    except (TypeError, ValueError):
        keep_recent = 2
    if len(idxs) <= keep_recent:
        return 0
    total = estimate_messages_tokens(messages)
    if total <= budget_tokens:
        return 0
    # 低水位缺省 = 高水位（保持老调用方的「折到刚好卡线」语义）
    target = budget_tokens
    if target_tokens:
        try:
            target = min(int(target_tokens), int(budget_tokens))
        except (TypeError, ValueError):
            target = budget_tokens
    target = max(1, target)
    dropped = 0
    last_idx = -1
    for i in (idxs[:-keep_recent] if keep_recent else idxs):
        if total <= target:
            break
        text = messages[i].get("content") or ""
        placeholder = (TOOL_MSG_PREFIX + "\n" + FOLDED_MARK
                       + _fold_summary(text, fold_chars))
        total -= estimate_tokens(text) - estimate_tokens(placeholder)
        messages[i] = {"role": "user", "content": placeholder}
        dropped += 1
        last_idx = i
    if stats is not None and last_idx >= 0:
        stats["cursor"] = last_idx + 1
    return dropped


def _micro_summarize(text, stop_event=None):
    """单条工具结果的轻量摘要；按原文 hash 缓存，避免每轮重复调用。"""
    raw = str(text or "")
    if not raw.strip():
        return ""
    key = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    with _MICRO_LOCK:
        hit = _MICRO_CACHE.get(key)
    if hit:
        return hit
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return ""
    prompt = (
        "把下面这段工具结果压缩成供另一个模型继续工作的摘要。\n"
        "只保留：错误信息、关键数值、文件路径与行号、执行过的命令、结论。\n"
        "不要寒暄、不要解释、不要编造；不超过 600 字。\n\n"
        + raw[:MICROCOMPACT_INPUT_CHARS]
    )
    try:
        from app import llm
        out = llm._quick_chat(
            api_key, api_url, api_model,
            [{"role": "user", "content": prompt}],
            max_tokens=500, source="microcompact", stop_event=stop_event,
            timeout=(8, 45), tries=1, use_light=True)
    except Exception as e:                # noqa: BLE001  失败就保留原文
        print(f"[微压缩] 摘要失败：{e}")
        return ""
    out = str(out or "").strip()
    if not out or len(out) >= len(raw):
        return ""
    if len(out) > MICROCOMPACT_MAX_CHARS:
        out = out[:MICROCOMPACT_MAX_CHARS] + "…"
    with _MICRO_LOCK:
        if len(_MICRO_CACHE) >= _MICRO_CACHE_MAX:
            _MICRO_CACHE.pop(next(iter(_MICRO_CACHE)))
        _MICRO_CACHE[key] = out
    return out


def microcompact_messages(messages, budget_tokens, cfg=None, stop_event=None):
    """对齐 CC 的 microcompact：只压「单条过大的工具结果」，不动其它消息。

    触发条件：当前上下文 >= 预算 * microcompact_trigger_ratio（默认 60%），
    且单条工具结果 >= microcompact_min_tokens（默认 3k）。每次最多压 3 条，
    保留最近 2 条工具结果原文；只改本次请求的临时列表，不落库。
    """
    cfg = cfg if isinstance(cfg, dict) else (load_config() or {})
    if cfg.get("microcompact_enabled", True) is False:
        return messages, 0
    if light_chat_is_local(cfg):          # 本地小窗口：已有确定性折叠，别再加一次模型调用
        return messages, 0
    try:
        budget = int(budget_tokens or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return messages, 0
    try:
        ratio = float(cfg.get("microcompact_trigger_ratio")
                      or MICROCOMPACT_TRIGGER_RATIO)
        min_tokens = int(cfg.get("microcompact_min_tokens")
                         or MICROCOMPACT_MIN_TOKENS)
    except (TypeError, ValueError):
        ratio, min_tokens = MICROCOMPACT_TRIGGER_RATIO, MICROCOMPACT_MIN_TOKENS
    if estimate_messages_tokens(messages) < max(1, int(budget * ratio)):
        return messages, 0

    idxs = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c.startswith(TOOL_MSG_PREFIX):
            continue
        if FOLDED_MARK in c[:80] or "【微压缩】" in c[:120]:
            continue
        idxs.append(i)
    if len(idxs) <= MICROCOMPACT_KEEP_RECENT:
        return messages, 0
    cand = []
    for i in idxs[:-MICROCOMPACT_KEEP_RECENT]:
        if stop_event is not None and stop_event.is_set():
            return messages, 0
        if estimate_tokens(messages[i].get("content") or "") >= min_tokens:
            cand.append(i)
    cand = cand[:MICROCOMPACT_MAX_PER_PASS]
    done = 0
    for i in cand:
        if stop_event is not None and stop_event.is_set():
            break
        old = messages[i].get("content") or ""
        new = _micro_summarize(old, stop_event)
        if not new:
            continue
        messages[i] = {**messages[i],
                       "content": TOOL_MSG_PREFIX + "\n【微压缩】\n" + new}
        done += 1
    if done:
        print("[微压缩] 已摘要 %d 条过大的工具结果" % done)
    return messages, done


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


def reactive_compact(messages, keep_last=4, stop_event=None, use_light=False):
    """对齐 CC 的 Reactive Compact：413 / 超窗后保留最后 4 条，其余紧急摘要。

    只作用于本次请求的临时列表，不落库；返回 (新列表, 被替换的条数)。
    找不到密钥 / 摘要失败 / 没有足够旧消息时原样返回。
    """
    msgs = list(messages or [])
    if len(msgs) <= int(keep_last or 0) + 1:
        return messages, 0
    sys_msgs = msgs[:1] if msgs and msgs[0].get("role") == "system" else []
    body = msgs[len(sys_msgs):]
    if len(body) <= int(keep_last or 0):
        return messages, 0
    cut = len(body) - int(keep_last or 0)
    # 别把 tool_use / tool_result 拆开：tail 开头若是工具结果，往前带上对应的 assistant。
    while cut > 0 and _is_tool_msg(body[cut]):
        cut -= 1
    if cut <= 0:
        return messages, 0
    old, tail = body[:cut], body[cut:]
    if sum(estimate_tokens(str(m.get("content") or "")) for m in old) < 300:
        return messages, 0
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return messages, 0
    _sys_text = str((sys_msgs[0].get("content") or "")) if sys_msgs else ""
    raw = _api_compress(api_key, api_url, api_model, _sys_text, [], old,
                        max(512, min(2048, len(old) * 8)), stop_event,
                        use_light=use_light)
    if not raw:
        return messages, 0
    summary, _points = _split_summary(raw)
    if not summary:
        return messages, 0
    note = {"role": "user", "content":
            "【上下文超窗，已紧急压缩较早的 %d 条消息；原文仍在本地会话里】\n%s"
            % (len(old), summary[:6000])}
    print("[压缩] 413 应急压缩：%d 条 → 摘要，保留最后 %d 条" % (len(old), len(tail)))
    return sys_msgs + [note] + tail, len(old)


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
