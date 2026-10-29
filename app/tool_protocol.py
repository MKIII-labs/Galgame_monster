# -*- coding: utf-8 -*-
"""工具协议：稳定错误码 + 恢复指令 + 参数校验。

dsh 的工具协议是「结构化错误 + 恢复指令 + 写前读」；这里再加两件事：

- 错误码稳定（`READ_REQUIRED` / `STALE_VERSION` / `AMBIGUOUS` ...），
  模型可以按 code 分支，评测也能直接统计失败类型；
- 失败结果带 `重试参数`，模型可以按建议直接重发，不用猜。

成功结果保持原文，不额外包装，避免破坏既有行为与 token 预算。
"""
import json

NOT_FOUND = "NOT_FOUND"
READ_REQUIRED = "READ_REQUIRED"
STALE_VERSION = "STALE_VERSION"
AMBIGUOUS = "AMBIGUOUS"
INVALID_ARG = "INVALID_ARG"
SANDBOX_DENIED = "SANDBOX_DENIED"
PERMISSION = "PERMISSION"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
CONFLICT = "CONFLICT"
REPEAT = "REPEAT"
TOOL_ERROR = "TOOL_ERROR"

_TEXT_MAP = (
    ("沙箱", SANDBOX_DENIED),
    ("不存在", NOT_FOUND),
    ("未找到", NOT_FOUND),
    ("未知工具", NOT_FOUND),
    ("缺少", INVALID_ARG),
    ("参数", INVALID_ARG),
    ("多处出现", AMBIGUOUS),
    ("目标已存在", CONFLICT),
    ("拒绝覆盖", CONFLICT),
    ("超时", TIMEOUT),
    ("已停止", CANCELLED),
    ("权限", PERMISSION),
)

_ARG_MAX_CHARS = 120      # 单个参数值在错误里的展示上限
_ARGS_MAX_CHARS = 600     # 「重试参数」整行上限

_ERR_PREFIXES = ("❌", "文件不存在", "源不存在", "目录不存在", "缺少 ",
                 "未找到", "old 在多处出现", "目标是目录", "目标已存在",
                 "拒绝覆盖", "沙箱拒绝", "工具执行出错", "未知工具")


def is_error(text):
    """工具实现没有统一的 ok 字段，这里按既有错误前缀判断。"""
    t = str(text or "").lstrip()
    return t.startswith(_ERR_PREFIXES)


def code_of(text):
    t = str(text or "")
    for key, code in _TEXT_MAP:
        if key in t:
            return code
    return TOOL_ERROR


def recovery_of(code, name="", args=None):
    args = args or {}
    p = args.get("path") or args.get("from") or ""
    if code == NOT_FOUND:
        return "先 list_dir 确认路径，再重试 %s" % (name or "工具")
    if code == READ_REQUIRED:
        return "先 read_file(path=%r)，再重试 %s" % (p, name or "写入")
    if code == STALE_VERSION:
        return ("文件在读取后已变化：重新 read_file(path=%r)，"
                "确认最新内容后再重试 %s" % (p, name or "写入"))
    if code == AMBIGUOUS:
        return "old 有多处匹配：给出更长、唯一的片段，或改用 write_file 整体覆盖"
    if code == INVALID_ARG:
        return "检查必填参数与类型后重试"
    if code == SANDBOX_DENIED:
        return ("改用沙箱内路径；确实要读外部文件时，请用户在界面开启"
                "「允许读取沙箱外」")
    if code == PERMISSION:
        return "检查文件权限，或关闭占用该文件的程序后重试"
    if code == TIMEOUT:
        return "缩小命令范围，或调大 timeout 后重试"
    if code == CONFLICT:
        return "换一个目标路径，或先删除 / 移动已有目标"
    if code == CANCELLED:
        return "用户已停止：不要再重发，先汇报当前进度"
    return "根据原因修正参数后重试；不要原样重发"


def _dump(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def brief_args(args, limit=_ARGS_MAX_CHARS):
    """把调用参数压成「可以直接重发」的一行 JSON。

    文件正文这类长值不塞进错误文本（否则一次失败反而把上下文撑爆）；
    参数太多、拼出来超过上限时宁可不给，也不给一段被截断的坏 JSON。
    """
    keep = {}
    for key, val in (args or {}).items():
        if len(_dump(val)) > _ARG_MAX_CHARS:
            continue
        keep[str(key)] = val
    if not keep:
        return ""
    try:
        out = json.dumps(keep, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return out if len(out) <= limit else ""


def error(code, message, name="", args=None, recovery=""):
    """统一的失败结果：code + 原因 + 恢复指令（+ 可重试参数）。

    保留 `❌` 前缀：调用方（chat.py / 前端日志）一直按它判断成功失败，
    结构化信息从第二行开始，旧判定零改动。
    """
    rec = recovery or recovery_of(code, name, args)
    lines = ["❌ 【工具错误】%s" % code, "原因：" + str(message or ""), "恢复：" + rec]
    brief = brief_args(args)
    if brief:
        lines.append("重试参数：" + brief)
    return "\n".join(lines)


def wrap(text, name="", args=None):
    """工具返回错误文本时补上 code / 恢复 / 重试参数；成功原样返回。"""
    if not is_error(text):
        return text
    return error(code_of(text), text, name=name, args=args)


def validate(name, args, required):
    """必填参数校验。只认 None / 缺键，不把空字符串当缺失（空文件是合法的）。"""
    for key in required or ():
        if key not in args or args.get(key) is None:
            return INVALID_ARG, "缺少必填参数：%s" % key
    return "", ""
