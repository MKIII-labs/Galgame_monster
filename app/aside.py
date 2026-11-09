# -*- coding: utf-8 -*-
"""Solo 旁注提问：就一段选中文本临时问答。

和主对话的区别（也是这个模块存在的理由）：
- 主对话同一会话串行（chat.ACTIVE_STREAMS），旁注必须走独立端点；
- 旁注只读「选中片段 + 少量上下文」，不写 messages / memory / trajectory；
- 不挂任何工具，模型只解释、不执行任务。

上下文由前端冻结后传进来（主输出还在追加 token，DOM 会变），
本模块只负责裁剪、拼消息，不读 DOM、不读文件。
"""
import threading

MAX_SELECTION = 4000          # 选中片段上限（字符）
MAX_CONTEXT = 1200            # 片段前后文各自上限
MAX_QUESTION = 2000
MAX_ANSWER = 4000
MAX_HISTORY = 12              # 同一张卡片最多保留几轮追问（与 aside_store 对齐，总预算再兜底）
MAX_TOTAL = 14000             # 拼好的消息总量兜底（字符）

SYSTEM = "\n".join([
    "你是「旁注助手」，只解释用户在对话或编辑器里选中的一小段内容。",
    "规则：",
    "1. 只回答问题，不执行任务、不调用工具、不修改文件、不评价整个项目。",
    "2. 面向中文读者；原文是英文 / 术语 / 代码时，先直译关键句，再解释。",
    "3. 先给结论，再补必要背景；默认控制在 6 行以内，用户要求展开时才写长。",
    "4. 不确定就直说不知道，不要编造；不要复述整段原文。",
    "5. 选中的是代码时：先说清它在做什么、有什么坑；不要整段重写，除非用户明确要求。",
])

SYSTEM_NOTES = "\n".join([
    "你是「笔记助手」，基于给出的笔记标题、上下文和选中片段回答用户的问题。",
    "规则：",
    "1. 只回答与这段笔记相关的问题；信息不足就直说，不要编造。",
    "2. 先给结论，再补必要背景；默认简洁，用户要求展开时才写长。",
    "3. 引用笔记原文时保持原样，不要改写用户笔记。",
])

SYSTEM_READER = "\n".join([
    "你是「论文阅读助手」，基于给出的文档上下文和选中片段回答用户的问题。",
    "规则：",
    "1. 只回答与这段文档相关的问题；信息不足就直说，不要编造。",
    "2. 公式、符号、编号保持原样；术语与文档保持一致。",
    "3. 先给结论，再补必要背景；默认简洁，用户要求展开时才写长。",
])



def _clip(text, limit):
    s = str(text or "").strip()
    if limit and len(s) > limit:
        return s[:limit] + "\n…（已截断）"
    return s


def _chars(messages):
    return sum(len(str(m.get("content") or "")) for m in messages)


def _assemble(ctx, hist, question, system=SYSTEM):
    """把「片段 + 历史 + 当前问题」拼成一次独立对话。"""
    first_q = _clip((hist[0] or {}).get("q"), MAX_QUESTION) if hist else question
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": ctx + "\n\n【问题】\n" + (first_q or question)},
    ]
    for i, item in enumerate(hist):
        if i:
            hq = _clip(item.get("q"), MAX_QUESTION)
            if hq:
                msgs.append({"role": "user", "content": hq})
        ans = _clip(item.get("a"), MAX_ANSWER)
        if ans:
            msgs.append({"role": "assistant", "content": ans})
    if hist:
        msgs.append({"role": "user", "content": question})
    return msgs


def _chat_context(sel, payload):
    role = str(sel.get("role") or "")
    idx = sel.get("msg_index")
    head = "【旁注上下文】"
    if role or isinstance(idx, int):
        who = "AI 回复" if role == "ai" else ("用户消息" if role == "user" else "对话内容")
        head += "来自%s%s" % (who, (" #%s" % idx) if isinstance(idx, int) else "")

    parts = [head]
    before = _clip(sel.get("before"), MAX_CONTEXT)
    after = _clip(sel.get("after"), MAX_CONTEXT)
    if before:
        parts.append("【前文】\n" + before)
    parts.append("【选中片段】\n" + _clip(sel.get("text"), MAX_SELECTION))
    if after:
        parts.append("【后文】\n" + after)
    if payload.get("include_message"):
        full = _clip(payload.get("message_text"), MAX_SELECTION * 2)
        if full:
            parts.append("【所在消息全文】\n" + full)
    return "\n\n".join(parts)


def _code_context(sel):
    """编辑器选区：把文件位置与前后代码一起给模型，让它能判断上下文。"""
    where = "【旁注上下文】来自编辑器选区"
    path = str(sel.get("path") or "")
    start = sel.get("start_line")
    end = sel.get("end_line")
    if path:
        where += " " + path
    if isinstance(start, int) and start > 0:
        where += " 第 %s-%s 行" % (start, (end if isinstance(end, int) and end > 0 else start))
    lang = str(sel.get("language") or "")
    if lang:
        where += "（%s）" % lang

    parts = [where]
    before = _clip(sel.get("before"), MAX_CONTEXT)
    if before:
        parts.append("【选区前的代码】\n" + before)
    parts.append("【选中的代码】\n" + _clip(sel.get("text"), MAX_SELECTION))
    after = _clip(sel.get("after"), MAX_CONTEXT)
    if after:
        parts.append("【选区后的代码】\n" + after)
    return "\n\n".join(parts)


def _note_context(sel, payload):
    """笔记预览选区：标题 + 前后文 + 片段。"""
    title = str(payload.get("title") or sel.get("title") or "").strip()
    parts = ["【笔记上下文】" + (title or "（未命名笔记）")]
    prefix = _clip(sel.get("prefix"), MAX_CONTEXT)
    if prefix:
        parts.append("【前文】\n" + prefix)
    parts.append("【选中片段】\n" + _clip(sel.get("text"), MAX_SELECTION))
    suffix = _clip(sel.get("suffix"), MAX_CONTEXT)
    if suffix:
        parts.append("【后文】\n" + suffix)
    return "\n\n".join(parts)


def _reader_context(sel, payload):
    """PDF 选区：文档标题 / 页码 + 段落原文 + 片段。"""
    title = str(payload.get("title") or sel.get("title") or "").strip()
    page = sel.get("page") or payload.get("page")
    head = "【文档上下文】" + (title or "（未命名文档）")
    if page:
        head += " · 第 %s 页" % page
    parts = [head]
    context = _clip(sel.get("context") or payload.get("context"), MAX_CONTEXT)
    if context:
        parts.append("【段落原文】\n" + context)
    parts.append("【选中片段】\n" + _clip(sel.get("text"), MAX_SELECTION))
    return "\n\n".join(parts)


_SOURCE_SYSTEM = {"notes": SYSTEM_NOTES, "reader": SYSTEM_READER}


def build_messages(payload):
    """前端 payload → 模型 messages；内容不足时返回空列表（调用方回 400）。"""
    payload = payload if isinstance(payload, dict) else {}
    sel = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    text = _clip(sel.get("text"), MAX_SELECTION)
    question = _clip(payload.get("question"), MAX_QUESTION)
    if not text or not question:
        return []

    source = str(payload.get("source") or "chat")
    if source == "notes":
        ctx = _note_context(sel, payload)
    elif source == "reader":
        ctx = _reader_context(sel, payload)
    elif str(sel.get("kind") or "") == "code":
        ctx = _code_context(sel)
    else:
        ctx = _chat_context(sel, payload)

    hist = [h for h in (payload.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
    system = _SOURCE_SYSTEM.get(source, SYSTEM)
    msgs = _assemble(ctx, hist, question, system)
    # 超预算时从最老的追问开始丢，保留上下文 + 当前问题
    while _chars(msgs) > MAX_TOTAL and hist:
        hist = hist[1:]
        msgs = _assemble(ctx, hist, question, system)
    return msgs

