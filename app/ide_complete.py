# -*- coding: utf-8 -*-
"""IDE 内联补全（Tab 补全）：上下文裁剪、提示构造、结果清洗与限流。

和主对话解耦：短超时、不重试、不走缓存，配置了 light_model 时自动走轻量模型。
补全只是一段「建议文本」，永远不写文件 —— 是否接受由用户在编辑器里按 Tab 决定。
"""
import re
import threading
import time

from app.config import get_api_conf, load_config
from app.llm import _quick_chat

PREFIX_MAX = 6000          # 光标前最多带给模型多少字符
SUFFIX_MAX = 2000          # 光标后最多带给模型多少字符
MAX_LINES = 8
MAX_CHARS = 600
MIN_INTERVAL = 0.8         # 同一会话两次请求的最小间隔（秒）
FAIL_LIMIT = 5             # 连续失败几次进入冷却
COOLDOWN = 60.0

_LOCK = threading.Lock()
_LAST = {}                 # session_id -> 上次请求时间
_FAILS = {}                # session_id -> 连续失败次数
_COOLDOWN_UNTIL = {}       # session_id -> 冷却结束时间


def _trim_prefix(text, limit=PREFIX_MAX):
    """保留光标前的最后 limit 个字符（越靠近光标越重要）。"""
    s = str(text or "")
    return s[-limit:] if limit and len(s) > limit else s


def _trim_suffix(text, limit=SUFFIX_MAX):
    s = str(text or "")
    return s[:limit] if limit and len(s) > limit else s


def build_messages(path, language, prefix, suffix, max_lines=MAX_LINES):
    """构造 FIM 提示：光标前文 + 后文，让模型只补中间那段。"""
    pre = _trim_prefix(prefix)
    post = _trim_suffix(suffix)
    sys = (
        "你是代码补全引擎。只输出「光标处应当插入的文本」，不要解释、不要 Markdown "
        "代码块、不要重复光标前后已有的内容。补全要短且能直接接在前文之后、"
        "与后文自然衔接；最多 %d 行。没有把握或不需要补全时输出空字符串。"
        % int(max_lines or MAX_LINES)
    )
    user = (
        "文件：%s（%s）\n\n--- 光标前 ---\n%s\n--- 光标后 ---\n%s"
        % (str(path or ""), str(language or "plaintext"), pre, post)
    )
    return [{"role": "system", "content": sys},
            {"role": "user", "content": user}]


def clean_completion(text, suffix="", max_lines=MAX_LINES, max_chars=MAX_CHARS):
    """把模型输出收拾成「能直接插进光标处」的一段文本。

    · 去整体包裹的 ``` 围栏；
    · 去掉与光标后文重复的部分（模型常把后文也重写一遍）；
    · 限制行数与字符数（补全只是提示，长了既慢又容易错）。
    """
    t = str(text or "").replace("\r\n", "\n").strip("\n")
    if not t.strip():
        return ""
    if t.lstrip().startswith("```"):
        head = t.lstrip()
        nl = head.find("\n")
        t = head[nl + 1:] if nl >= 0 else ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    t = t.rstrip()
    if not t:
        return ""
    # 与后文开头重叠：取最长公共前缀，超过 8 个字符才裁（避免「)」这类正常重复）
    post = str(suffix or "")
    if post:
        k = min(len(t), len(post), 200)
        while k > 8 and t[-k:] != post[:k]:
            k -= 1
        if k >= 8:
            t = t[:-k].rstrip()
    if not t:
        return ""
    lines = t.split("\n")
    try:
        limit = int(max_lines or 0)
    except (TypeError, ValueError):
        limit = MAX_LINES
    if limit > 0 and len(lines) > limit:
        lines = lines[:limit]
        t = "\n".join(lines)
    try:
        cap = int(max_chars or 0)
    except (TypeError, ValueError):
        cap = MAX_CHARS
    if cap > 0 and len(t) > cap:
        cut = t[:cap]
        nl = cut.rfind("\n")
        t = cut[:nl] if nl > 0 else cut
    return t.rstrip()


def allow(sid, now=None):
    """该会话现在能不能再发一次补全请求（间隔 + 冷却）。"""
    now = time.time() if now is None else now
    key = str(sid or "")
    with _LOCK:
        if _COOLDOWN_UNTIL.get(key, 0) > now:
            return False
        if now - _LAST.get(key, 0) < MIN_INTERVAL:
            return False
        _LAST[key] = now
    return True


def note_result(sid, ok, now=None):
    """记录结果：连续失败到阈值就冷却一段时间，避免网络坏时每 350ms 空打一次。"""
    now = time.time() if now is None else now
    key = str(sid or "")
    with _LOCK:
        if ok:
            _FAILS.pop(key, None)
            return
        n = _FAILS.get(key, 0) + 1
        if n >= FAIL_LIMIT:
            _COOLDOWN_UNTIL[key] = now + COOLDOWN
            _FAILS.pop(key, None)
        else:
            _FAILS[key] = n


def reset_throttle():
    """清空限流状态（测试用）。"""
    with _LOCK:
        _LAST.clear()
        _FAILS.clear()
        _COOLDOWN_UNTIL.clear()


def complete(path, language, prefix, suffix, max_lines=None):
    """请求一次补全，返回 (text, error)。text 为空表示「没有建议」。"""
    cfg = load_config() or {}
    if cfg.get("ide_ai_complete") is False:
        return "", "已关闭 AI 补全"
    if not (cfg.get("light_model") or "").strip():
        return "", "未配置轻量模型"
    if max_lines is None:
        try:
            max_lines = int(cfg.get("ide_ai_complete_max_lines") or MAX_LINES)
        except (TypeError, ValueError):
            max_lines = MAX_LINES
    max_lines = max(1, min(40, int(max_lines or MAX_LINES)))
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return "", "未配置接口密钥"
    msgs = build_messages(path, language, prefix, suffix, max_lines)
    try:
        tok = max(32, min(256, max_lines * 24))
    except (TypeError, ValueError):
        tok = 96
    try:
        raw = _quick_chat(api_key, api_url, api_model, msgs, max_tokens=tok,
                          source="ide-complete", timeout=(3, 5), tries=1,
                          use_cache=False, temperature=0.0)
    except Exception as e:
        return "", str(e)[:160]
    return clean_completion(raw, suffix, max_lines), ""
