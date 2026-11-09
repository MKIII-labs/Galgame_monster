# -*- coding: utf-8 -*-
import hashlib
import json
import queue
import re
import threading
import time
from collections import OrderedDict

import requests
import urllib3

from app import agent, mem_tool
from app.config import (effective_think_mode, hybrid_on, is_local_url, load_config,
                        normalize_content, resolve_endpoint, thinking_payload)
from app.knowledge import (get_profile_kb, load_kb)
from app.profile import load_profile, manual_block
from app.usage import record_llm_usage

# 轻量调用统一调度：同一 prompt 短时间内直接命中缓存；并发上限避免
# 自动播放 / 多标签页在瞬时打出几十个模型请求。
_QUICK_SEM = threading.BoundedSemaphore(5)
_QUICK_CACHE = OrderedDict()
_QUICK_CACHE_LOCK = threading.Lock()
_QUICK_CACHE_TTL = 600
_QUICK_CACHE_MAX = 128


def _quick_cache_key(api_url, api_model, messages, max_tokens):
    raw = json.dumps({"u": api_url, "m": api_model, "msgs": messages,
                      "mt": int(max_tokens or 0)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _quick_cache_get(key):
    now = time.time()
    with _QUICK_CACHE_LOCK:
        item = _QUICK_CACHE.get(key)
        if not item:
            return None
        ts, text = item
        if now - ts > _QUICK_CACHE_TTL:
            _QUICK_CACHE.pop(key, None)
            return None
        _QUICK_CACHE.move_to_end(key)
        return text


def _quick_cache_put(key, text):
    if not (text or "").strip():
        return
    with _QUICK_CACHE_LOCK:
        _QUICK_CACHE[key] = (time.time(), text)
        _QUICK_CACHE.move_to_end(key)
        while len(_QUICK_CACHE) > _QUICK_CACHE_MAX:
            _QUICK_CACHE.popitem(last=False)


_SHARED_SESSION = None
_SESSION_LOCK = threading.Lock()


def _http():
    """全局复用一个 requests.Session（keep-alive）。

    以前每轮都 `requests.post(...)`，等于每轮重新建 TCP + TLS。逐轮埋点实测
    hdr（发请求到收到响应头）占回合总耗时 p50 17% / p90 36%，其中相当一部分
    就是重复握手。
    为什么不做成 thread-local：stream_chat 每轮都新建读线程，thread-local 会让
    每一轮都拿到空 Session，等于没有复用。连接池（HTTPAdapter）本身是线程安全的，
    这里只调 post、不改 Session 的 headers / cookies，可以跨线程共享。
    """
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        with _SESSION_LOCK:
            if _SHARED_SESSION is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=8,
                                                        pool_maxsize=16,
                                                        max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _SHARED_SESSION = s
    return _SHARED_SESSION


def _post_retry(url, headers, json_body, timeout, tries=2):
    """带一次重试的 POST：只对网络异常与 5xx 重试，4xx 是确定性错误、不重试。
    只用于**幂等**的轻量调用（摘要 / 记忆挑选 / GAL 判定 / 语音矫正）；
    流式主对话不在此列（重试会导致重复输出）。"""
    r = None
    for i in range(tries):
        try:
            r = _http().post(url, headers=headers, json=json_body, timeout=timeout)
            if r.status_code < 500:
                return r
        except requests.exceptions.RequestException:
            if i == tries - 1:
                raise
        if i < tries - 1:
            time.sleep(0.5 * (i + 1))
    return r


def _pick_light_model(cfg, use_light=True, source=""):
    """轻量模型选择。

    混合模式关闭时，指向本地服务的 light 不参与（等价于纯 API）；
    指向云端的小模型仍按原逻辑使用。这样「一键关闭混合」就能完全回到旧行为。

    ide-complete 默认不切本地：补全只有 350ms 防抖 + 3–5s 超时，9B 本地模型
    容易拖慢输入体验；需要时可以开 hybrid_ide_complete。
    """
    lm = (cfg.get("light_model") or "").strip() if use_light else ""
    if not lm:
        return ""
    if not hybrid_on(cfg) and is_local_url(cfg.get("light_url")):
        return ""
    if (source == "ide-complete" and is_local_url(cfg.get("light_url"))
            and not bool(cfg.get("hybrid_ide_complete"))):
        return ""
    return lm


def _quick_chat(api_key, api_url, api_model, messages, max_tokens=96, source="gal",
                timeout=(10, 60), tries=2, use_cache=True, temperature=0.2,
                use_light=True, stop_event=None):
    """一次非流式聊天调用（用于 GAL 剧情节点判定 / 摘要 / 语音矫正等轻量请求）。
    顺带把响应里的 usage 计入用量流水（按 source 标注来源）。

    `tries` / `use_cache` / `temperature` 供 IDE 内联补全这类「要快、要不打扰」的
    场景调小：补全不重试、不走缓存、温度 0。默认值与旧行为一致。
    `use_light=False` 强制走主聊天模型（阅读器翻译这类要质量的场景）。
    `stop_event`：用户点停止时不再等这个非流式请求返回（用于长上下文压缩）。
    """
    if stop_event is not None:
        # requests 的非流式调用无法从外部中断；放到后台线程里，主线程按停止信号
        # 立即返回，结果丢弃。压缩调用方拿到空串后不会 save_summary。
        if stop_event.is_set():
            return ""
        box = {}
        done = threading.Event()

        def _run():
            try:
                box["v"] = _quick_chat(
                    api_key, api_url, api_model, messages,
                    max_tokens=max_tokens, source=source, timeout=timeout,
                    tries=tries, use_cache=use_cache, temperature=temperature,
                    use_light=use_light, stop_event=None)
            except BaseException as e:      # noqa: BLE001  交给调用线程抛出
                box["e"] = e
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name="quick-chat-stop").start()
        while not done.wait(0.2):
            if stop_event.is_set():
                return ""
        if "e" in box:
            raise box["e"]
        return box.get("v", "")
    cfg = load_config() or {}
    # 分级模型调度：配置了 light_model 时，这些轻量任务改用它（可另配 light_url / light_api_key）；
    # 三项都留空 = 沿用主 chat 模型，行为与从前完全一致。
    lm = _pick_light_model(cfg, use_light, source)
    api_name = "chat"
    if lm:
        api_model = lm
        api_name = "light"
        if (cfg.get("light_url") or "").strip():
            api_url = cfg["light_url"].strip()
        if (cfg.get("light_api_key") or "").strip():
            api_key = cfg["light_api_key"].strip()
    body = {"model": api_model, "messages": messages, "stream": False,
            "max_tokens": max_tokens, "temperature": temperature}
    # 轻量调用不该「思考」：思考 token 也算进 max_tokens，会把正文挤成空
    # （踩坑记录见 docs/NOTES.md「轻量调用被思考吃光 max_tokens」）
    body.update(thinking_payload(effective_think_mode(api_url), False,
                                 cfg.get("chat_think_level") or "high"))
    cache_key = _quick_cache_key(api_url, api_model, messages, max_tokens)
    if use_cache:
        cached = _quick_cache_get(cache_key)
        if cached is not None:
            return cached
    from app import providers
    from app.config import get_api_kind
    # 选中的是 light 服务商时，协议族要按 light 的地址判断（可能是本地 Ollama）
    kind = get_api_kind("light" if api_name == "light" else "chat")
    # light 分支用的是 config 里的原始 light_url（可能是裸地址，如 Ollama 的
    # http://127.0.0.1:11434），这里补上 /v1/chat/completions；chat 分支的 URL
    # 在 get_api_conf 里已经补全，不重复处理。
    if api_name == "light" and kind == "openai":
        api_url = resolve_endpoint(api_url)
    with _QUICK_SEM:
        if kind == "anthropic":
            resp = _post_retry(
                providers.anthropic_url(api_url),
                headers=providers.anthropic_headers(api_key),
                json_body=providers.anthropic_body(api_model, messages, max_tokens,
                                                   None, stream=False),
                timeout=timeout, tries=tries)
        else:
            resp = _post_retry(
                api_url,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json; charset=utf-8"},
                json_body=body,
                timeout=timeout, tries=tries)
    resp.raise_for_status()
    data = resp.json()
    if kind == "anthropic":
        txt = providers.text_of(data)
        usage = providers.usage_of(data.get("usage"))
        _record_quick_usage(usage, api_model, source, api_name, api_url)
        finish = data.get("stop_reason") or ""
    else:
        _record_quick_usage(data.get("usage") or {}, api_model, source, api_name, api_url)
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        txt = normalize_content(msg.get("content"))
        finish = ch.get("finish_reason")
        usage = data.get("usage") or {}
    if not (txt or "").strip():
        print(f"[轻量调用] 正文为空 source={source} finish_reason={finish} "
              f"completion={usage.get('completion_tokens')}")
    else:
        _quick_cache_put(cache_key, txt)
    return txt


def _record_quick_usage(u, model, source, api="chat", url=""):
    """把一次非流式调用的 usage 记入用量流水（缺失 usage 时静默跳过）"""
    record_llm_usage(u, model, source, api=api, url=url)


def user_profile_text(limit=30):
    """用户画像文本（手动偏好 + 「用户画像」知识库），供 AI 编剧贴合用户"""
    parts = []
    try:
        p = load_profile() or {}
        manual = manual_block(p)
        if manual:
            parts.append("【用户手动偏好】\n" + manual)
    except Exception:
        pass
    try:
        kb = load_kb(get_profile_kb())
        nodes = (kb or {}).get("nodes") or []
        if nodes:
            parts.append("【用户画像】")
            for n in nodes[:limit]:
                title = str(n.get("title") or "").strip()
                if not title:
                    continue
                cat = str(n.get("category") or "其他").strip()
                content = str(n.get("content") or "").strip().replace("\n", " ")
                parts.append("- （%s）%s%s" % (cat, title, ("：" + content[:60]) if content else ""))
    except Exception:
        pass
    return "\n".join(parts)


def recent_dialog(s, limit=6, each=160):
    """把最近的对话压成简要文本（供编剧参考当前走向）"""
    msgs = (s or {}).get("messages") or []
    picked = []
    for m in msgs[-limit * 2:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = str(m.get("content") or "").replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > each:
            text = text[:each] + "…"
        picked.append(("玩家" if role == "user" else "角色") + "：" + text)
    return "\n".join(picked[-limit:])


def _strip_ctrl_markers(text):
    """兜底清理：去掉不该给用户看的控制标记（含各家的原生工具调用标记）"""
    s = str(text or "")
    for m in (mem_tool.DSML_OPEN, mem_tool.DSML_CLOSE, mem_tool.FENCE, "```tool"):
        if m:
            s = s.replace(m, "")
    # 伪工具标签一律清掉：它们落库后会作为「示例」出现在下一轮历史里，
    # 模型会照着继续吐同样的标签（自激循环）。这里用白名单规则，不再枚举写法。
    return _strip_unknown_tags(s)


_FENCED_CALLS_RE = re.compile(
    r"```(?:json|tool)?\s*(\{[\s\S]*?\"calls\"[\s\S]*?\})\s*```\s*$", re.I)

# 模型写过的「文本工具调用」标签，真实数据里出现过的全部写法：
#   <tool> / <tool调用> / <tool calls> / <tool_calls> / <tool calls/> / <tool calls">
#   <parameter name="calls"> / <invoke name="..."> / <function_calls>
# 标签只是噪音，块边界由后面能解析成调用的 JSON 决定——所以正则写宽，
# 不再按形态一个个枚举（那样只会追着模型的模板库跑）。
_CALL_TAG_RE = re.compile(
    r"<\s*/?\s*(?:tool|tool_calls?|tool\s+calls|tool调用|parameter|invoke|function_calls)"
    r"(?:\s[^<>]{0,60})?\s*/?>",
    re.I,
)
# 同源：落库前 / 前端渲染前都要清掉这些标签，否则会成为下一轮的模仿样本
_PSEUDO_TOOL_RE = _CALL_TAG_RE

# 「协议标签」的判定：名字里带 tool，或属于少数已知的调用协议名。
# 为什么不再用「不认识的名字一律删」：拿真实会话回放会吃掉正经内容 ——
# SVG 的 <linearGradient> / <ellipse>、占位符 <YYYYMMDD> / <seed> 都不是协议。
# 而模型换写法（<tool calls> / <tool_calls> / <tool calls here> / <tool调用> /
# <tool_result> …）时名字里始终带 tool，所以按「名字族」判定即可，不必枚举写法。
_PROTO_NAME_RE = re.compile(r"tool|parameter|invoke|function_calls|thinking|antml", re.I)

# 名字里允许 CJK 后缀：模型写过 `<tool调用>`（中间没有空格）。
# `<` 后必须紧跟字母或 `/字母`：这样 `a < b > c` 不会被当成标签；
# 标签体里带 `://` 的（自动链接 / URL）也不碰。
_TAG_RE = re.compile(
    r"<\s*(/?)\s*([A-Za-z][\w:.\-\u4e00-\u9fff]*)((?:\s[^<>]*?)?)\s*(/?)\s*>")

# 「动作式」伪调用：模型在原生通道失效时会照着系统提示里的 run(action=exec) 自创 XML。
# 只用于检测并提醒模型（见 chat.py 的 _MSG_PSEUDO_CALL），不参与执行、不改正文。
_ACTION_TAG_RE = re.compile(
    r"<\s*/?\s*(?:run|read|edit|code|git|plan|agent|web|history|acceptance|finish)"
    r"\b[^<>]{0,80}?\baction\s*=", re.I)
# DeepSeek 原生 DSML：全角竖线 U+FF5C（单/双/无前导）或代理转码后的 ASCII 变体。
_DSML_RE = re.compile(
    r"(?:｜\s*){0,2}DSML(?:\s*｜){1,2}"      # 完整标记（含无前导竖线）
    r"|\|\s*DSML\s*\|"                       # 代理转码后的 ASCII 变体
    r"|<\s*/?\s*｜{1,2}\s*DS",               # 流式截断残片，如 `</｜｜DS`
    re.I)
_FENCE_BLOCK_RE = re.compile(r"```[\s\S]*?```")


def pseudo_call_tagged(text):
    """正文里是否出现「看起来像工具调用、但运行时不会执行」的写法。

    覆盖三类：`<tool>` / `<invoke>` 家族、`<run action=…>` 动作式 XML、DeepSeek DSML。
    先剔掉代码围栏，避免模型在解释协议时被误判。
    """
    s = _FENCE_BLOCK_RE.sub("", str(text or ""))
    if not s:
        return False
    return bool(_PSEUDO_TOOL_RE.search(s) or _ACTION_TAG_RE.search(s) or _DSML_RE.search(s))

# 「协议标签 + 配对闭合」时，内容短于这个长度就整块删；超过则只删标签。
# 模型偶尔会把整篇答案包进标签里，不能连正文一起删掉。
_PROTO_PAIR_MAX = 2000


def _strip_unknown_tags(s):
    """删掉伪工具标签（含模型自演的工具结果）；短配对内容一并删掉。

    判定只看名字族（见 `_PROTO_NAME_RE`），与模型这次又换成了哪种写法无关。
    标签替换成换行而不是空串，避免 `</tool>## 标题` 这种标题失效。
    """
    s = str(s or "")
    if "<" not in s:
        return s
    out, pos = [], 0
    for m in _TAG_RE.finditer(s):
        if m.start() < pos:
            continue
        name = m.group(2)
        if not _PROTO_NAME_RE.search(name):
            continue
        if "://" in (m.group(3) or ""):
            continue
        out.append(s[pos:m.start()])
        out.append("\n")
        pos = m.end()
        if m.group(1) or m.group(4):
            continue                       # 闭合 / 自闭合：只丢标签本身
        close = re.compile(r"<\s*/\s*" + re.escape(name) + r"\s*>", re.I).search(s, pos)
        if close and close.start() - pos <= _PROTO_PAIR_MAX:
            pos = close.end()              # 短配对块：标签和内容一起删
    out.append(s[pos:])
    return "".join(out)

_FENCE_RE = re.compile(r"(?m)^[ \t]*(```|~~~)[ \t]*([\w+-]*)[ \t]*$")


def _fence_spans(s):
    """切出代码围栏：[(start, body_start, body_end, end, lang)]。

    未闭合的围栏一直延伸到文本末尾（流式输出被截断时很常见）。
    """
    spans, pos = [], 0
    while True:
        m = _FENCE_RE.search(s, pos)
        if not m:
            break
        mark = m.group(1)
        close = _FENCE_RE.search(s, m.end())
        while close and close.group(1) != mark:
            close = _FENCE_RE.search(s, close.end())
        if close:
            spans.append((m.start(), m.end(), close.start(), close.end(),
                          (m.group(2) or "").lower()))
            pos = close.end()
        else:
            spans.append((m.start(), m.end(), len(s), len(s),
                          (m.group(2) or "").lower()))
            break
    return spans


def _json_end(s, start):
    """从 start 处的 `{` 或 `[` 起做括号配平，返回闭合符号之后的位置；失败返回 -1。

    只区分「字符串 / 转义」，足够切出模型吐出来的那一段 JSON，
    不依赖整段文本能被 json.loads（块后面通常还有正文）。
    """
    if start >= len(s) or s[start] not in "{[":
        return -1
    stack, in_str, esc = [], False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return -1
            stack.pop()
            if not stack:
                return i + 1
    return -1


def _calls_of(s, start):
    """尝试把 start 处的 JSON 解析成工具调用；不是调用就返回 (None, -1)。"""
    end = _json_end(s, start)
    if end < 0:
        return None, -1
    return (agent.parse_calls(s[start:end]) or None), end


def _all_known(calls):
    """调用里的工具名是否都真实存在（正文里的示例 JSON 往往用虚构工具名）。"""
    return bool(calls) and all(agent.is_known_tool(c.get("tool")) for c in calls)


def _extract_text_calls(text):
    """摘掉正文里的工具调用，返回 (正文, 合并后的调用 JSON)。

    三个入口，任何位置都认：
    ① 代码围栏（```tool / ```json）：流式截断时可能没闭合；
    ② 标签块：<tool calls> / <tool_calls> / <parameter name="calls"> / <invoke> 等；
    ③ 行首裸 JSON：模型直接吐 {"calls":[...]} 或 [{"tool":...}]，工具名必须真实存在。

    标签与围栏都只是「信号」，块边界一律由 JSON 决定 —— 真实数据里模型经常
    漏写闭合标签、把调用夹在正文中间，按标签配对会把正文整段吞掉。
    """
    s = str(text or "")
    if not s:
        return s, ""
    spans = _fence_spans(s)
    in_fence = bytearray(len(s))
    for a, _bs, _be, b, _lang in spans:
        for i in range(a, min(b, len(s))):
            in_fence[i] = 1

    picked = []           # (start, end, calls)；calls=None 表示只丢标签

    # ① 代码围栏里的调用
    for a, bs, be, b, lang in spans:
        if lang not in ("", "tool", "json"):
            continue
        cand = s[bs:be].strip()
        if not cand or cand[0] not in "{[":
            continue
        calls = agent.parse_calls(cand)
        if calls and _all_known(calls):
            picked.append((a, b, calls))

    # ② 标签块（不跳过围栏：真实数据里有「``` 没闭合、调用被套在里面」的情况，
    #    标签本身就是强信号，比「这块是不是代码」可靠）
    for m in _CALL_TAG_RE.finditer(s):
        p = m.end()
        while True:                       # 允许连续标签（多种写法混用）
            while p < len(s) and s[p].isspace():
                p += 1
            nxt = _CALL_TAG_RE.match(s, p)
            if not nxt:
                break
            p = nxt.end()
        calls, end = (None, -1)
        if p < len(s) and s[p] in "{[":
            calls, end = _calls_of(s, p)
        if calls:
            picked.append((m.start(), end, calls))
        elif end > 0:
            # 标签后面确实是一段 JSON，但解析不出调用（多半是模型写坏了）：
            # 边界是确定的，整段隐去即可，但**不执行**。
            picked.append((m.start(), end, None))
        else:
            picked.append((m.start(), m.end(), None))   # 孤立标签：只丢标签

    # ③ 行首裸 JSON（工具名必须真实存在，避免把正文里的示例 JSON 当调用）
    for m in re.finditer(r"(?m)^[ \t]*([\{\[])", s):
        p = m.start(1)
        if in_fence[p]:
            continue
        calls, end = _calls_of(s, p)
        if not calls or not _all_known(calls):
            continue
        picked.append((p, end, calls))

    if not picked:
        return s, ""
    picked.sort(key=lambda x: (x[0], -(x[1] or 0)))

    out, pos, merged = [], 0, []
    for a, b, calls in picked:
        if a < pos:                       # 与已摘掉的区间重叠
            continue
        out.append(s[pos:a])
        if not calls:
            pos = b                       # 孤立标签 / 坏掉的调用块：只删不执行
            continue
        merged.extend(calls)
        pos = b
        while True:                       # 顺手吞掉紧随的闭合标签
            q = pos
            while q < len(s) and s[q].isspace():
                q += 1
            if not s.startswith("</", q):
                break
            t = _CALL_TAG_RE.match(s, q)
            if not t:
                break
            pos = t.end()
    out.append(s[pos:])
    cleaned = "".join(out)
    if cleaned == s:
        return s, ""
    block = json.dumps({"calls": merged}, ensure_ascii=False) if merged else ""
    return cleaned, block


def _extract_bare_calls(text):
    """把正文里的工具调用摘出来，返回 (正文, 控制块)。

    先走统一入口 `_extract_text_calls`（标签块 / 代码围栏 / 行首裸 JSON）；
    没命中再按老办法看末尾，兜住「正文：{"calls":...}」这种不独立成段的情况。
    """
    s = str(text or "")
    cleaned, block = _extract_text_calls(s)
    # 先解析、再清标签：未知标签里可能就包着调用 JSON（<parameter name="calls">[…]），
    # 顺序反了会把调用一起删掉。解析完剩下的标签才是纯噪音。
    cleaned = _strip_unknown_tags(cleaned)
    if block:
        return cleaned.strip(), block
    # 只清掉了标签 / 坏掉的调用块、没有可执行调用：正文也要按清理后的返回，
    # 否则这段源码会照原样落库，成为下一轮的模仿样本。
    s = cleaned
    m = _FENCED_CALLS_RE.search(s)
    if m:
        block = m.group(1)
        calls = agent.parse_calls(block)
        if calls and _all_known(calls):
            return s[:m.start()].rstrip(), block
    for i in range(len(s) - 1, -1, -1):
        if s[i] != "{":
            continue
        cand = s[i:].strip()
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("calls"), list):
            calls = agent.parse_calls(cand)
            if calls and _all_known(calls):
                return s[:i].rstrip(), cand
    return s, ""


# 公开别名：chat.py 的兜底复用同一套解析
extract_bare_calls = _extract_bare_calls

# 上游 400 的错误体里出现这些词，说明是「上下文超长」而不是「不支持 tools」。
_CONTEXT_OVERFLOW_HINTS = ("context length", "context_length", "maximum context",
                           "too many tokens", "exceeds the maximum", "context window")


def _looks_like_context_overflow(text):
    """上游 400 是不是「上下文超长」。

    这类错误以前会被当成「服务商不认原生 tools」，于是去掉 tools 把整个请求体
    再传一遍 —— 十万 token 的请求要多等一轮，界面全程静默。
    """
    t = (text or "").lower()
    return any(k in t for k in _CONTEXT_OVERFLOW_HINTS)


# 上游明确表示「余额 / 额度不足」的说法。命中后不要当普通网络错误重试：
# 同一个 Key 再发多少次都还是余额不足，只会白白多等几轮。
_BILLING_HINTS = ("insufficient balance", "insufficient_quota", "quota exceeded",
                  "exceeded your current quota", "payment required", "billing",
                  "余额不足", "欠费", "额度不足", "账户余额", "请充值")


class BillingError(RuntimeError):
    """上游余额 / 额度不足：换服务商可以，重试同一个没有意义。"""


def _looks_like_billing(text):
    t = (text or "").lower()
    return any(k in t for k in _BILLING_HINTS)


# 限流 / 过载：这类可以自动重试（退避久一点），但要告诉用户是「打得太快」，
# 而不是笼统的「调用出错」。
_RATE_HINTS = ("rate limit", "rate_limit", "too many requests", "overloaded",
               "overloaded_error", "限流", "请求过于频繁", "并发")


def _looks_like_rate_limit(text):
    t = (text or "").lower()
    return any(k in t for k in _RATE_HINTS)


def _short_error(text, n=200):
    """把上游错误压成一行：错误体有时是整页 HTML / 大 JSON。"""
    t = " ".join(str(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def is_upload_break(err):
    """请求体还没发完链路就断了（Windows 上典型文案是 write operation timed out）。

    这类失败一个字节正文都没收到，重发没有副作用，上层会给更宽的重试预算；
    和「已经吐了半句话再断」必须分开处理。
    """
    text = "%s %s" % (err, getattr(err, "args", ""))
    return "write operation timed out" in text or "Broken pipe" in text


def upload_break_message():
    return "📤 请求发送中断（上行链路没有响应），本轮还没有产生任何内容"


def _error_message(err):
    """从 {"message": ...} / {"code": ...} / 字符串里取一条能看的错误。"""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return str(err or "")


def _detail_message(text):
    """错误体优先取 JSON 里的 message，取不到再用原文。"""
    raw = str(text or "").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return _error_message(obj.get("error") or obj)
    except Exception:                    # noqa: BLE001
        pass
    return raw


class UpstreamError(RuntimeError):
    """上游明确拒绝，且已经判断出是哪一类。

    state 供上层决定要不要自动重试：auth / bad_request / not_found / context
    都是「同一个请求再发多少次都一样」的确定性错误。
    """

    def __init__(self, state, message, retry_after=0):
        super().__init__(message)
        self.state = state
        self.retry_after = max(0, int(retry_after or 0))


def _parse_retry_after(raw):
    """解析 Retry-After（秒数或 HTTP 日期）→ 秒；无法解析返回 0。"""
    txt = str(raw or "").strip()
    if not txt:
        return 0
    try:
        return max(0, int(float(txt)))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(txt)
        return max(0, int(dt.timestamp() - time.time()))
    except Exception:
        return 0


def _http_failure(status, detail_raw):
    """HTTP 非 2xx → (state, 用户能看懂的一句话)。

    以前这些全被 `raise_for_status()` 的英文串兜住，用户只看到
    「调用出错：401 Client Error: Unauthorized for url: ...」，既不知道
    是密钥错还是网络错，也看不到上游到底说了什么。
    """
    detail = _short_error(_detail_message(detail_raw))
    tail = ("：%s" % detail) if detail.strip() else "：%s" % _short_error(detail_raw)
    if status in (401, 403):
        return ("auth", "🔑 API 密钥无效或无权限（HTTP %d）%s"
                        "。请检查「设置 → 聊天 API」里的密钥" % (status, tail))
    if status == 429:
        return ("rate_limit", "🚦 请求过于频繁被限流（HTTP 429）%s，稍后自动重试" % tail)
    if status >= 500:
        return ("server", "🔥 服务商服务端故障（HTTP %d），不是本地问题%s" % (status, tail))
    if status == 404:
        return ("not_found", "🔗 接口地址不存在（HTTP 404），请检查 API 地址%s" % tail)
    if status == 400:
        return ("bad_request", "⚠️ 请求被上游拒绝（HTTP 400）%s" % tail)
    return ("http", "上游返回 HTTP %d%s" % (status, tail))


def stream_chat(api_key, api_url, api_model, messages, *, think=None, stop_event=None,
                emit=None, on_visible=None, on_flush=None, allow_tool=True,
                tools=None, timeout=(10, 300), max_tokens=None, kind=None,
                idle_timeout=None, stall_timeout=None, temperature=None):
    """一次流式聊天调用：转发 SSE 增量，并顺手摘掉 ```memory / ```tool 代码块。

    P1-step5b：原来这是 api/chat.py 里的一个闭包 `call_model`，
    捕获了 6 个外部变量；这里改成显式参数，逻辑逐字未动。

    - think:      直接并进请求体的思考参数（由 config.thinking_payload 生成）
    - tools:      原生 function calling 的工具 schema；不传则只用文本协议
    - emit(obj):  推一条 SSE 事件
    - on_visible: 处理「已摘除 memory / tool 块」的可见文本（GAL 逐段揭示走这里）
    - on_flush(): 流结束后收尾（GAL 兜底分段要把剩下的吐干净）
    - idle_timeout: 原始数据看门狗（秒）。连续这么久连一个 `data:` 行都收不到就中止；
      0 / None 按配置 `chat_stream_idle_timeout`，配 0 = 不限。
    - stall_timeout: 空转看门狗（秒）。**收到过 data 行、但连续这么久没有思考 / 正文 /
      工具 / 结束标记**就中止；0 / None 按配置 `chat_stream_stall_timeout`。
      为什么要分开：上游会发空 delta / role-only / usage-only 的 `data:` 行保活，
      原始数据看门狗会被它们续命，而界面其实一个字都没有。
    返回 {visible, mem_ops, tool_block, tool_calls, reasoning, usage, stopped,
          stream_state, first_ms, diag}。first_ms 是首包延迟（第一次收到正文 / 思考内容）；
          diag 是断流用的现场数据（响应头耗时 / 首 data / 有意义的行数 / 空行数 /
          思考字数 / 正文字数 / 前两条空 chunk 样本）。
    """
    emit = emit or (lambda obj: None)
    on_visible = on_visible or (lambda text: None)
    if on_flush is None:
        on_flush = lambda: None                                    # noqa: E731
    _cfg = load_config()
    # 原始数据看门狗：只认 `data:` 行，注释行 / 空行不算「上游还活着」。
    if idle_timeout is None:
        try:
            idle_timeout = float(_cfg.get("chat_stream_idle_timeout") or 0)
        except (TypeError, ValueError):
            idle_timeout = 0.0
    else:
        try:
            idle_timeout = float(idle_timeout or 0)
        except (TypeError, ValueError):
            idle_timeout = 0.0
    # 空转看门狗：收到 data 行，但里面没有思考 / 正文 / 工具 / 结束标记。
    # 空 delta / role-only / usage-only 会续命 socket，却不会让界面出现任何内容。
    if stall_timeout is None:
        try:
            stall_timeout = float(_cfg.get("chat_stream_stall_timeout") or 0)
        except (TypeError, ValueError):
            stall_timeout = 0.0
    else:
        try:
            stall_timeout = float(stall_timeout or 0)
        except (TypeError, ValueError):
            stall_timeout = 0.0
    # 读超时跟着看门狗走：看门狗触发后，读线程仍会阻塞在下一次 recv 上
    #（Windows 上无法从外部打断已阻塞的 recv，见下面读线程的注释）。
    # 把 socket 读超时压到看门狗之后 5 秒，残留连接最多再占 5 秒，而不是原来的 300 秒。
    if idle_timeout > 0:
        try:
            timeout = (timeout[0], min(float(timeout[1]), idle_timeout + 5.0))
        except (TypeError, ValueError, IndexError):
            pass
    t_req = time.monotonic()       # 请求发出时刻：首包延迟的基准
    # 请求发出到响应头之间没有任何正文，界面只能看到心跳。先发一条明确的「等待上游」，
    # 让前端把这段时间显示成「等待响应」而不是「卡住」；attach 重放也会带上它。
    emit({"type": "model_wait", "timeout": int(idle_timeout or 0),
          "model": api_model})
    t_data = t_req                 # 最近一次收到 data 行（含空心跳）的时刻
    t_progress = t_req             # 最近一次收到有意义内容的时刻（空转看门狗用）
    t_first = None                 # 第一次收到正文 / 思考内容的时刻

    req = {"model": api_model, "messages": messages, "stream": True,
           "stream_options": {"include_usage": True}}
    # 输出上限：不传就吃服务商默认（DeepSeek 系常见 4096，思考模式的 reasoning 也计入），
    # 长回复 + 工具块很容易在写工具块前就被截断，所以主对话显式给一个更大的值。
    try:
        _mt = int(max_tokens or 0)
    except (TypeError, ValueError):
        _mt = 0
    if _mt > 0:
        req["max_tokens"] = _mt
    # 评测需要固定随机性：显式传 0 时才下发，正常对话保持服务商默认
    if temperature is not None:
        try:
            req["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    if think:
        req.update(think)

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json; charset=utf-8"}
    # 协议族：openai（兼容）/ anthropic（原生）。Anthropic 不下发原生 tools，
    # 工具调用统一走项目的 ```tool 文本协议，避免 tool_use / tool_result 严格配对的改造。
    from app import providers
    from app.config import get_api_kind
    # kind 由调用方显式传：故障转移时备用服务商的协议可能与主服务商不同
    kind = kind or get_api_kind("chat")

    # Anthropic 提示缓存：默认开，只对「主对话流式请求」生效（轻量任务短、
    # 写入 1.25x 不划算，不缓存）。
    try:
        from app.config import load_config as _load_cfg_cache
        _cache_on = bool((_load_cfg_cache() or {}).get("anthropic_cache", True))
    except Exception:                    # noqa: BLE001
        _cache_on = True

    def _post(with_tools):
        if kind == "anthropic":
            body = providers.anthropic_body(
                api_model, messages, _mt, think, stream=True,
                tools=(tools if with_tools else None), temperature=temperature,
                cache=_cache_on)
            return _http().post(providers.anthropic_url(api_url),
                                headers=providers.anthropic_headers(api_key),
                                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                stream=True, timeout=timeout)
        body = dict(req)
        if with_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return _http().post(api_url, headers=headers,
                            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            stream=True, timeout=timeout)

    reasoning, usage_info = [], {}
    finish_reason = None
    saw_done = False              # 收到过 [DONE] 结束标记
    stream_end = None             # 流为什么结束：done / eof / stopped / timeout / error
    retry_after = 0               # 上游 Retry-After（秒）；0 = 没给 / 不可用
    native_calls = {}
    native_fell_back = False      # 服务商拒绝了原生 tools，本次已退回文本协议
    mem_mx = mem_tool.MemoryExtractor()
    tool_mx = agent.ToolExtractor() if allow_tool else None
    sent = 0
    # 读上游与解析拆到两个线程：Windows 上无法从外部打断已阻塞的 recv
    # （resp.close / sock.shutdown / sock.close / os.close 都实测无效），
    # 所以让主线程只从队列取行，每 0.2 秒检查一次停止信号 —— 点「停止」最多 0.2 秒返回。
    q = queue.Queue()
    cancelled = threading.Event()
    _resp_holder = {}

    # 诊断：用来区分「没拿到响应头」「拿到头但没有 data」「只发注释保活」。
    # 这些字段随返回值进 token_usage / app.log，下一次断流能直接定性。
    t_headers = None         # 收到响应头的时刻
    t_first_data = None      # 第一条 data: 行的时刻
    t_line = None            # 最近一条任意行（含注释）的时刻
    n_data = n_other = n_bytes = 0
    n_meaningful = n_empty = 0          # 有思考/正文/工具/结束 vs 空 data 行
    n_reason_chars = n_content_chars = 0
    empty_samples = []                  # 前两条空 chunk（截断），用于确认上游到底发了什么
    t_tool_emit = 0.0                   # 原生工具调用参数流的节流起点
    last_tool_bytes = 0
    status_code = 0

    def _reader():
        """独立线程：发请求、逐行读上游，把原始行丢进队列。"""
        nonlocal native_fell_back, t_headers, status_code
        try:
            resp = _post(bool(tools))
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                status = getattr(resp, "status_code", 0)
                _retry_after = _parse_retry_after(
                    (getattr(resp, "headers", {}) or {}).get("Retry-After"))
                # 读一次错误体（很短）用于判断该不该降级；stream=True 不影响。
                try:
                    detail_raw = (resp.text or "")[:600]
                except Exception:
                    detail_raw = ""
                detail = detail_raw.lower()
                # 402 / 余额关键词：这是「余额不足」，不是普通网络错误，也不是
                # 「不支持 tools」。必须单独抛出去，后面才能提示用户去充值。
                if status == 402 or _looks_like_billing(detail_raw):
                    resp.close()
                    raise BillingError(
                        "上游返回「%s」" % _short_error(_detail_message(detail_raw))
                        if _detail_message(detail_raw).strip()
                        else "上游 HTTP %d" % status)
                # 兼容不支持原生 tools 的 OpenAI 兼容服务：去掉 tools 再试一次，
                # 模型仍可按提示词里的 ```tool 文本协议工作。
                # 但 400 太杂（上下文超长 / 模型名不存在 / 参数非法都是 400），
                # 只有错误体确实在说 tools / function 时才降级 —— 否则会把十万 token
                # 的请求体原样再传一遍，白白多等一轮且界面全程静默。
                # 404 / 422 基本只会是「没有这个端点 / 参数不认」，直接降级。
                tools_rejected = (status in (404, 422)
                                  or (status == 400
                                      and ("tool" in detail or "function" in detail)))
                if tools and tools_rejected:
                    resp.close()
                    resp = _post(False)
                    native_fell_back = True
                    try:
                        resp.raise_for_status()
                    except requests.exceptions.HTTPError:
                        # 剥掉 tools 重发的这一次同样要分类：否则 401 / 404
                        # 会直接冒到最外层，又变回「调用出错」。
                        _st2 = getattr(resp, "status_code", 0)
                        try:
                            _raw2 = (resp.text or "")[:600]
                        except Exception:
                            _raw2 = ""
                        resp.close()
                        if _st2 == 402 or _looks_like_billing(_raw2):
                            raise BillingError("上游返回「%s」"
                                               % _short_error(_detail_message(_raw2)))
                        _state2, _msg2 = _http_failure(_st2, _raw2)
                        raise UpstreamError(_state2, _msg2, _retry_after)
                elif status == 400 and _looks_like_context_overflow(detail):
                    raise UpstreamError(
                        "context",
                        "🧹 上下文超出模型窗口（上游 400）。请先压缩历史，"
                        "或在「设置 → 上下文与压缩」里把上限调回真实窗口，"
                        "让程序提前压缩。")
                else:
                    _state, _msg = _http_failure(status, detail_raw)
                    resp.close()
                    raise UpstreamError(_state, _msg, _retry_after)
            # 走完 tools 降级判断后，这里才是真正要读的响应
            t_headers = time.monotonic()
            status_code = int(getattr(resp, "status_code", 0) or 0)
            _resp_holder["resp"] = resp      # 停止时尽力关掉，别让上游继续烧 token
            emit({"type": "model_ready", "status": status_code})
            try:
                for raw in resp.iter_lines():
                    if cancelled.is_set():
                        return
                    q.put(("line", raw))
            finally:
                resp.close()
            q.put(("end", None))
        except Exception as e:                 # 异常交给主线程，按原有分支处理
            q.put(("err", e))

    threading.Thread(target=_reader, daemon=True, name="llm-sse-reader").start()

    def _abort_stream():
        """停止 / 看门狗命中：置取消标记，并尽力关掉上游连接。

        Windows 上 close 未必能立刻打断已阻塞的 recv（读线程仍是 daemon），
        而且 close 本身可能等读锁 —— 实测直接在主线程 close 会把「点停止」
        拖到上游下一段数据到达（延迟 2~3 秒）。所以只丢给后台线程做资源释放。
        """
        cancelled.set()
        resp = _resp_holder.get("resp")
        if resp is None:
            return

        def _close():
            try:
                resp.close()
            except Exception:                # noqa: BLE001  关不掉也不能影响收尾
                pass

        threading.Thread(target=_close, daemon=True, name="llm-sse-close").start()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                _abort_stream()                # 让读线程尽快退出，并断开上游
                stream_end = "stopped"
                break
            # 看门狗：上游「连接还在、正文永远不来」时，别等 socket 读超时（默认 300s），
            # 到点直接中止并明确告诉用户是谁卡住了。
            if idle_timeout > 0 and time.monotonic() - t_data >= idle_timeout:
                _abort_stream()
                stream_end = "idle"
                emit({"type": "error", "message":
                      "⏰ 上游 %d 秒没有任何响应数据，已中止本轮"
                      "（设置 chat_stream_idle_timeout 可调，0 = 不限）"
                      % int(idle_timeout)})
                break
            # 空转：data 行还在来，但里面没有思考 / 正文 / 工具 / 结束标记。
            # 只从「收到过第一条 data」之后生效；完全没数据仍由上面的 90 秒兜底。
            # 阈值按「本轮已经产出什么」分级 —— 实测断流大头是「思考吐完几千到几万字符后，
            # 正文/工具迟迟不开始」：这段间隙上游可能长时间静默，用 45 秒会误杀。
            # - 已有正文：保持原阈值（半句话断了应尽快重试 / 续写）
            # - 已有思考、无正文：放宽到 idle 级别，给长思考留出落地时间
            # - 开了思考但上游没流式下发：同样放宽（可能在服务端思考）
            _stall_cap = stall_timeout
            if stall_timeout > 0 and n_content_chars == 0:
                if n_reason_chars > 0 or think:
                    _stall_cap = max(stall_timeout, idle_timeout or stall_timeout)
            if (stall_timeout > 0 and t_first_data is not None
                    and time.monotonic() - t_progress >= _stall_cap):
                _abort_stream()
                stream_end = "idle"
                if n_reason_chars > 0:
                    _stall_msg = ("⏰ 上游思考结束后 %d 秒仍未开始输出正文，已中止本轮"
                                  "（设置 chat_stream_stall_timeout 可调，0 = 关）")
                else:
                    _stall_msg = ("⏰ 上游 %d 秒没有思考或正文（只收到空心跳），已中止本轮"
                                  "（设置 chat_stream_stall_timeout 可调，0 = 关）")
                emit({"type": "error", "message": _stall_msg % int(_stall_cap)})
                break
            try:
                # 注意：不能叫 kind —— 那会把上面「协议族」（openai / anthropic）覆盖掉，
                # 导致 Anthropic 的流永远走 OpenAI 分支、正文全空。
                qkind, payload = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if qkind == "end":
                # 上游正常读完：既没有 [DONE] 也没有 finish_reason → 多半是被中途掐断
                stream_end = "done" if (saw_done or finish_reason) else "eof"
                break
            if qkind == "err":
                raise payload
            raw = payload
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            t_line = time.monotonic()          # 任意一行（含注释）都算「连接上还有字节」
            n_bytes += len(raw)
            if not line.startswith("data:"):
                n_other += 1                   # SSE 注释 / event: —— 连接活着，但没有正文
                continue
            chunk = line[5:].strip()
            if t_first_data is None:
                t_first_data = time.monotonic()
            n_data += 1
            t_data = time.monotonic()          # 收到 data 行 = 上游还活着（看门狗重新计时）
            if chunk == "[DONE]":
                saw_done = True
                stream_end = "done"
                break
            try:
                obj = json.loads(chunk)
            except Exception:
                n_empty += 1
                if len(empty_samples) < 2:
                    empty_samples.append(chunk[:300])
                continue
            # 有的服务商 HTTP 200，但流里发 {"error": {...}}（余额不足、限流等）。
            # 以前这种会被当成空分片，最后误报成「上游空转」。
            if isinstance(obj, dict) and obj.get("error") is not None:
                _emsg = _error_message(obj.get("error"))
                if _looks_like_billing(_emsg):
                    stream_end = "billing"
                    emit({"type": "error", "message":
                          "💳 API 余额不足：上游返回「%s」" % _short_error(_emsg)})
                elif _looks_like_rate_limit(_emsg):
                    stream_end = "rate_limit"
                    emit({"type": "error", "message":
                          "🚦 请求过于频繁被限流：%s" % _short_error(_emsg)})
                elif _looks_like_context_overflow(_emsg):
                    stream_end = "context"
                    emit({"type": "error", "message":
                          "🧹 上下文超出模型窗口：%s" % _short_error(_emsg)})
                else:
                    stream_end = "error"
                    emit({"type": "error", "message":
                          "❌ 上游返回错误：%s" % _short_error(_emsg)})
                break
            r_piece = ""
            piece = ""
            _meaningful = False
            if kind == "anthropic":
                # Anthropic 没有 [DONE]，结束标记是 message_stop。
                # 不认它的话，每个 Anthropic 回合都会被判成 eof（「上游没有发送结束标记」）。
                if providers.is_done(obj):
                    saw_done = True
                    stream_end = "done"
                    break
                # 原生 tool_use：content_block_start（工具头）+ input_json_delta（参数分片）。
                # 文本 / 思考块的 start/stop 也走同一套事件，这里只处理工具块，其余跳过，
                # 避免把每个 content_block_stop 都记成空分片。
                _tev = providers.anthropic_tool_event(obj)
                if _tev is not None:
                    _tk = _tev.get("kind")
                    _ti = int(_tev.get("index") or 0)
                    if _tk == "start":
                        native_calls[_ti] = {"id": _tev.get("id") or "",
                                             "name": _tev.get("name") or "",
                                             "arguments": ""}
                        _meaningful = True
                    elif _tk == "delta":
                        _slot = native_calls.setdefault(
                            _ti, {"id": "", "name": "", "arguments": ""})
                        _slot["arguments"] += _tev.get("json") or ""
                        _meaningful = True
                    if _tk in ("start", "delta"):
                        _tb = sum(len(s.get("arguments") or "")
                                  for s in native_calls.values())
                        _now = time.monotonic()
                        if _now - t_tool_emit >= 0.5 or _tb - last_tool_bytes >= 16384:
                            _names = [s.get("name") for s in native_calls.values()
                                      if s.get("name")]
                            emit({"type": "tool_stream",
                                  "name": _names[0] if _names else "",
                                  "bytes": _tb, "calls": len(native_calls)})
                            t_tool_emit = _now
                            last_tool_bytes = _tb
                    if _tk in ("start", "delta", "stop"):
                        if _meaningful:
                            n_meaningful += 1
                            t_progress = time.monotonic()
                        continue
                d = providers.delta(obj)
                if d is None:
                    n_empty += 1
                    if len(empty_samples) < 2:
                        empty_samples.append(chunk[:300])
                    continue
                piece, r_piece, _fr, _us = d
                r_piece = r_piece or ""
                piece = piece or ""
                if _fr:
                    finish_reason = _fr
                if _us:
                    usage_info = providers.usage_of(_us) or usage_info
                _meaningful = bool(_fr or r_piece or piece)
            else:
                if obj.get("usage"):
                    usage_info = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    n_empty += 1
                    if len(empty_samples) < 2:
                        empty_samples.append(chunk[:300])
                    continue
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                    _meaningful = True
                delta = choices[0].get("delta") or {}
                # 原生 function calling：按 index 累积 name / arguments 分片
                _has_tool = False
                for _tc in (delta.get("tool_calls") or []):
                    _has_tool = True
                    try:
                        _idx = int(_tc.get("index", 0))
                    except (TypeError, ValueError):
                        _idx = 0
                    _slot = native_calls.setdefault(
                        _idx, {"id": "", "name": "", "arguments": ""})
                    if _tc.get("id"):
                        _slot["id"] = str(_tc["id"])
                    _fn = _tc.get("function") or {}
                    if _fn.get("name"):
                        _slot["name"] = str(_fn["name"])
                    if _fn.get("arguments"):
                        _slot["arguments"] += str(_fn["arguments"])
                if _has_tool:
                    _meaningful = True
                    # 原生工具调用的参数是静默累积的：整轮结束前前端看不到任何东西。
                    # 这里节流发一条轻量进度（名字 + 参数字节数），不塞完整参数。
                    _tb = sum(len(s.get("arguments") or "")
                              for s in native_calls.values())
                    _now = time.monotonic()
                    if _now - t_tool_emit >= 0.5 or _tb - last_tool_bytes >= 16384:
                        _names = [s.get("name") for s in native_calls.values()
                                  if s.get("name")]
                        emit({"type": "tool_stream",
                              "name": _names[0] if _names else "",
                              "bytes": _tb, "calls": len(native_calls)})
                        t_tool_emit = _now
                        last_tool_bytes = _tb
                # Ollama 的 OpenAI 兼容端点把思考放在 delta.reasoning，
                # vLLM / DeepSeek 用 delta.reasoning_content：两种都要认，
                # 否则本地模型的思考过程会被整段丢掉（前端看不到「深度思考」）。
                r_piece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if not isinstance(r_piece, str):
                    r_piece = ""
                piece = delta.get("content") or ""
                if r_piece or piece:
                    _meaningful = True
            if _meaningful:
                n_meaningful += 1
                t_progress = time.monotonic()
            else:
                n_empty += 1
                if len(empty_samples) < 2:
                    empty_samples.append(chunk[:300])
            n_reason_chars += len(r_piece)
            n_content_chars += len(piece)
            if r_piece or piece:
                if t_first is None:
                    t_first = time.monotonic()   # 首包：第一段正文 / 思考内容到达
            if r_piece:
                reasoning.append(r_piece)
                emit({"type": "reasoning", "text": r_piece})
            if piece:
                vis1 = tool_mx.feed(piece) if tool_mx is not None else piece
                if vis1:
                    sent += len(vis1)
                    vis = mem_mx.feed(vis1)
                    if vis:
                        on_visible(vis)
    except BillingError as e:
        stream_end = "billing"
        emit({"type": "error", "message": "💳 API 余额不足：%s" % e})
    except UpstreamError as e:
        stream_end = e.state
        retry_after = int(getattr(e, "retry_after", 0) or 0)
        emit({"type": "error", "message": str(e)})
    except requests.exceptions.ConnectTimeout:
        stream_end = "network"
        emit({"type": "error", "message":
              "🌐 连接接口地址超时：网络不通 / 代理没开 / URL 写错"})
    except requests.exceptions.ReadTimeout:
        stream_end = "timeout"
        emit({"type": "error", "message": "⏰ 上游长时间没有返回数据，请求超时"})
    except requests.exceptions.Timeout:
        stream_end = "timeout"
        emit({"type": "error", "message": "⏰ 请求超时，请检查网络连接"})
    except requests.exceptions.SSLError as e:
        stream_end = "network"
        emit({"type": "error", "message":
              "🔒 HTTPS 证书校验失败：%s" % _short_error(e, 120)})
    except requests.exceptions.ConnectionError as e:
        if sent == 0 and n_data == 0 and is_upload_break(e):
            # 请求体还没发完就断了：重发没有副作用，交给上层的上传重试预算
            stream_end = "upload"
            emit({"type": "error", "message": upload_break_message()})
        else:
            stream_end = "network"
            emit({"type": "error", "message":
                  "🌐 连不上接口地址，请检查网络 / 代理 / API 地址（%s）"
                  % _short_error(e, 120)})
    except requests.exceptions.RequestException as e:
        if sent == 0 and n_data == 0 and is_upload_break(e):
            stream_end = "upload"
            emit({"type": "error", "message": upload_break_message()})
        elif "Connection broken" in str(e) or "IncompleteRead" in str(e):
            stream_end = "network"
            emit({"type": "error", "message":
                  "🌐 上游提前关闭了连接：%s" % _short_error(e, 120)})
        else:
            stream_end = "network"
            emit({"type": "error", "message": "🌐 请求发送失败：%s" % _short_error(e, 160)})
    except urllib3.exceptions.ProtocolError as e:
        # 读线程里 urllib3 抛的协议错误不会被 requests 重新包装（只有 _post 那一段会），
        # 过去直接落到最后的 except Exception，用户只看到「❌ 调用出错：(英文元组)」。
        # 典型场景：请求体还没发完链路就断了（Windows 文案 'The write operation timed out'）。
        _no_output = (sent == 0 and n_data == 0)
        if _no_output and is_upload_break(e):
            # 一个字节正文都没收到：重发没有副作用，上层可以给更宽的重试预算
            stream_end = "upload"
            emit({"type": "error", "message": upload_break_message()})
        else:
            stream_end = "network"
            emit({"type": "error", "message":
                  "🌐 连接被上游中断：%s" % _short_error(e, 120)})
    except urllib3.exceptions.HTTPError as e:
        # 其余 urllib3 异常（连接池 / 连接建立失败等）同样按网络问题归类，
        # 否则又会变成「调用出错」+ 英文原文。
        stream_end = "network"
        emit({"type": "error", "message": "🌐 连接失败：%s" % _short_error(e, 120)})
    except Exception as e:
        stream_end = "error"
        emit({"type": "error", "message": f"❌ 调用出错：{e}"})

    # 原生 function calling：把累积到的分片收敛成和文本协议相同的调用结构
    native_parsed = []
    for _idx in sorted(native_calls):
        _slot = native_calls[_idx]
        if not _slot["name"]:
            continue
        _call = agent.normalize_call({"function": {
            "name": _slot["name"],
            "arguments": _slot["arguments"] or "{}",
        }})
        if _call:
            if _slot["id"]:
                _call["id"] = _slot["id"]
            native_parsed.append(_call)

    tool_block, tool_calls = "", []
    # 原生 function calling 是主通道：只要本轮 tools 真被服务商接受，文本化的调用就
    # 只清理、不执行（避免「两套协议并存」把模型惯成自创标签）。
    # 只有「没下发 tools」或「服务商 400/404/422 自动降级」时才允许文本协议执行。
    text_tools_ok = (not tools) or native_fell_back
    dropped_text_calls = False
    if tool_mx is not None:
        tool_mx.finish()
        rest = tool_mx.visible_text()[sent:]           # finish 时可能吐出的残余
        if rest:
            vis = mem_mx.feed(rest)
            if vis:
                on_visible(vis)
        tool_block = tool_mx.block_text
        tool_calls = agent.parse_calls(tool_block)
        if tool_calls and not text_tools_ok:
            dropped_text_calls = True                  # 块留着，让上层提醒一次
            tool_calls = []
        # 兜底：没有 ```tool 围栏、但末尾直接吐了 {"calls": [...]} 的情况
        if not tool_calls:
            before, bare = _extract_bare_calls(mem_mx.visible_text())
            calls2 = agent.parse_calls(bare) if bare else []
            if calls2 and text_tools_ok:
                tool_block = bare
                tool_calls = calls2
            elif calls2:
                tool_block = bare                      # 不执行，但保留块体供上层提示
                dropped_text_calls = True
            # 即使没有可执行调用，只要正文变了（清掉了标签 / 坏掉的调用块）也要写回，
            # 否则前端早已流式显示的源码不会被服务端的收尾文本替换掉。
            if before != mem_mx.visible_text():
                mem_mx.visible = [before]
        # 与 memory 侧对称：未闭合的块又解析不出调用 → 当普通正文还回，别静默丢弃
        if tool_block and not tool_calls and not tool_mx.closed:
            vis = mem_mx.feed(tool_block)
            if vis:
                on_visible(vis)
    if native_parsed:                                  # 原生调用优先于文本围栏
        tool_calls = native_parsed
    leftover = mem_mx.finish()                         # 未闭合又解析不出的块当普通文本
    if leftover and not mem_mx.ops():
        mem_mx.visible.append(leftover)
        on_visible(leftover)
    on_flush()                                         # GAL：吐出兜底分段里剩下的内容
    # 兜底：模型有时只吐了标记头/尾却没给完整块（例如原生标记没闭合），
    # 这些标记不该留在会话里——否则下一轮模型会照着历史继续吐同样的格式
    raw_visible = mem_mx.visible_text()
    pseudo_tool_tag = (bool(_PSEUDO_TOOL_RE.search(raw_visible))
                       or dropped_text_calls)
    vis_final = _strip_ctrl_markers(raw_visible)
    if vis_final != raw_visible:
        mem_mx.visible = [vis_final]
    _reason_final = "".join(reasoning)
    # 失败尝试不能产生可执行动作：dsh 的失败分片不进派生历史，这里对应
    # 「不执行半截工具调用、不写记忆」。正文仍交给上层决定是否保留。
    _failed_stream = stream_end in ("idle", "network", "upload", "timeout",
                                   "error", "eof")
    _mem_ops = mem_mx.ops()
    if _failed_stream:
        tool_calls = []
        native_parsed = []
        _mem_ops = []
    # EMPTY_RESPONSE（dsh 同名分类）：正常收尾但一个内容块都没有。
    # 以前把它当「空正文」再 nudge 一次，等于多花一轮；现在交给重试策略。
    if (stream_end == "done" and not mem_mx.visible_text().strip()
            and not tool_calls and not tool_block and not _reason_final.strip()
            and not _mem_ops):
        stream_end = "empty"
    # 断流现场：other_lines > 0 = 连接上还有字节（注释保活），但一直没有 data 行；
    # other_lines = 0 且 headers_ms 有值 = 响应头之后彻底静默；headers_ms = None = 连头都没拿到。
    diag = {
        "status": status_code,
        "headers_ms": (int((t_headers - t_req) * 1000) if t_headers else None),
        "first_data_ms": (int((t_first_data - t_req) * 1000) if t_first_data else None),
        "data_lines": n_data,
        "other_lines": n_other,
        "bytes": n_bytes,
        "meaningful_lines": n_meaningful,
        "empty_lines": n_empty,
        "reasoning_chars": n_reason_chars,
        "content_chars": n_content_chars,
        "tool_names": [s.get("name") for s in native_calls.values() if s.get("name")],
        "tool_arg_bytes": sum(len(s.get("arguments") or "")
                              for s in native_calls.values()),
        "last_line_age_ms": (int((time.monotonic() - t_line) * 1000) if t_line else None),
        "last_progress_age_ms": int((time.monotonic() - t_progress) * 1000),
        # 只在「没有任何有意义的行」或「空转中止」时带样本，避免正常流也存大字符串
        "samples": (empty_samples[:2]
                    if (n_meaningful == 0 or stream_end == "idle") else []),
    }
    return {"visible": mem_mx.visible_text(), "mem_ops": _mem_ops,
            "mem_block": mem_mx.block_text, "mem_closed": bool(mem_mx.closed),
            "tool_block": tool_block, "tool_calls": tool_calls,
            "tool_closed": bool(tool_mx.closed) if tool_mx is not None else True,
            "native_tools": bool(native_parsed),
            "native_fallback": native_fell_back,
            "pseudo_tool_tag": pseudo_tool_tag,
            # 文本形式的伪调用（<run action=…> / DSML）：供上层提醒模型改用原生调用
            "pseudo_call": pseudo_call_tagged(raw_visible),
            "failed": bool(_failed_stream),
            "reasoning": _reason_final, "usage": usage_info,
            "finish_reason": finish_reason,
            "stream_state": stream_end or "eof",
            "retry_after": retry_after,
            "first_ms": (int((t_first - t_req) * 1000) if t_first else None),
            "diag": diag,
            "stopped": bool(stop_event is not None and stop_event.is_set())}
