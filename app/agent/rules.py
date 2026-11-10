# -*- coding: utf-8 -*-

import json
import os
from app.store import DATA_DIR



RULES_FILES = ("AGENTS.md", "CLAUDE.md")   # 项目规则文件（按顺序读取）


RULES_MAX_CHARS = 6000                     # 规则注入上限，避免每轮 prompt 被撑大
# 规则文件缓存：键 = 绝对路径，值 = (mtime_ns, size, 文本)。
# 每轮重读同一份文件既费 I/O，也容易因为读到的时机不同让 system 产生无谓差异；
# 文件变了（mtime/size 变）才重新读，保证「没改动就逐字节一致」。


_RULES_CACHE = {}


_RULES_CACHE_MAX = 32


RULES_REL = ".gm/agent-rules.json"          # 自定义审批规则（相对沙箱根目录）


def _read_rules_text_cached(p):
    """读一个规则文件（AGENTS.md / CLAUDE.md）；mtime/size 没变就复用上次结果。

    每轮由 build_agent_prompt 调用。缓存只影响 I/O，不影响文本：
    文件一改（mtime_ns 或 size 变）就重读，所以「没改就逐字节一致」。
    注意别和下面的 `_read_rules_file`（读权限 JSON）混了。
    """
    try:
        st = os.stat(p)
    except OSError:
        _RULES_CACHE.pop(p, None)
        return ""
    key = (st.st_mtime_ns, st.st_size)
    hit = _RULES_CACHE.get(p)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read(RULES_MAX_CHARS + 1).strip()
    except OSError:
        return ""
    if len(txt) > RULES_MAX_CHARS:
        txt = txt[:RULES_MAX_CHARS] + "\n…（规则文件过长，已截断）"
    if len(_RULES_CACHE) >= _RULES_CACHE_MAX:
        _RULES_CACHE.pop(next(iter(_RULES_CACHE)), None)
    _RULES_CACHE[p] = (key, txt)
    return txt


def project_rules(sandbox):
    """读取沙箱根目录的 AGENTS.md / CLAUDE.md，拼成注入用文本。

    每轮都由 build_agent_prompt 重新取 → 上下文压缩后也不会被摘要吃掉。
    文件读取有 mtime 缓存（见 _read_rules_text_cached），内容没变时结果逐字节一致。
    """
    from .sandbox import clean_sandbox
    root = clean_sandbox(sandbox)
    if not root or not os.path.isdir(root):
        return ""
    parts = []
    for fn in RULES_FILES:
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        txt = _read_rules_text_cached(p)
        if not txt:
            continue
        parts.append("## 项目规则（%s，必须遵守）\n%s" % (fn, txt))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 工具协议
# 必填参数：进实现之前先校验，缺了直接 INVALID_ARG —— 比各工具里的自然语言报错更早、更稳定。


def rules_path(sandbox):
    """项目级规则文件的绝对路径；沙箱未设置时返回 ""。"""
    from .sandbox import clean_sandbox
    root = clean_sandbox(sandbox)
    return os.path.join(root, *RULES_REL.split("/")) if root else ""


def global_rules_path():
    """全局规则文件：写在 data/ 下，对所有项目生效。"""
    return os.path.join(DATA_DIR, "agent-rules.json")


def _read_rules_file(p):
    """读一份规则文件；返回 (rules, error)。文件不存在 → ({}, "")。"""
    if not p or not os.path.isfile(p):
        return {}, ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        return {}, "规则文件无法解析：%s" % e
    if not isinstance(raw, dict):
        return {}, "规则文件必须是 JSON 对象（allow / ask / deny 三个数组）"
    out = {}
    for key in ("allow", "ask", "deny"):
        vals = raw.get(key)
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            vals = []
        out[key] = [str(v).strip().replace("\\", "/") for v in vals if str(v).strip()]
    # 命令规则：{ "commands": { "allow": ["python -m pytest*"], "deny": [...] } }
    cmds = raw.get("commands")
    norm = {}
    if isinstance(cmds, dict):
        for key in ("allow", "ask", "deny"):
            vals = cmds.get(key)
            if isinstance(vals, str):
                vals = [vals]
            if not isinstance(vals, list):
                vals = []
            norm[key] = [str(v).strip() for v in vals if str(v).strip()]
    out["commands"] = norm
    # 工具规则：{ "tools": { "allow": ["mcp__fs__read_*"], "deny": ["mcp__*__delete*"] } }
    # `permissions` 是等价别名，方便照 MCP / Claude Code 的写法直接抄。
    tools_raw = raw.get("tools") if isinstance(raw.get("tools"), dict) else raw.get("permissions")
    tnorm = {}
    if isinstance(tools_raw, dict):
        for key in ("allow", "ask", "deny"):
            vals = tools_raw.get(key)
            if isinstance(vals, str):
                vals = [vals]
            if not isinstance(vals, list):
                vals = []
            tnorm[key] = [str(v).strip() for v in vals if str(v).strip()]
    out["tools"] = tnorm
    return out, ""


def load_rules(sandbox):
    """读沙箱根目录下的 .gm/agent-rules.json（只管项目级）。

    返回 (rules, error)。rules 形如 {"allow": [...], "ask": [...], "deny": [...]}，
    路径 pattern 用 glob（如 "docs/**"、"*.md"），相对沙箱根目录。
    文件不存在 → ({}, "")；解析失败 → ({}, 错误说明)，调用方一律按「必须询问」兜底，
    规则写坏了绝不能变成放行。
    """
    return _read_rules_file(rules_path(sandbox))


def load_global_rules():
    """读 data/agent-rules.json（全局规则，对所有项目生效）。"""
    return _read_rules_file(global_rules_path())


def load_effective_rules(sandbox):
    """项目规则 + 全局规则的合并结果。

    两份都解析失败时返回 ({}, 错误)，调用方按「必须询问」兜底。
    合并只是把三档列表拼起来：判定仍然是 deny > ask > allow，
    所以任何一边写了 deny 都拦得住，项目规则也能收紧全局的 allow。
    """
    proj, perr = load_rules(sandbox)
    glob, gerr = load_global_rules()
    if perr or gerr:
        return {}, (perr or gerr)
    out = {}
    for key in ("allow", "ask", "deny"):
        out[key] = list(glob.get(key) or []) + list(proj.get(key) or [])
    cmds = {}
    for key in ("allow", "ask", "deny"):
        cmds[key] = (list((glob.get("commands") or {}).get(key) or [])
                     + list((proj.get("commands") or {}).get(key) or []))
    out["commands"] = cmds
    tools = {}
    for key in ("allow", "ask", "deny"):
        tools[key] = (list((glob.get("tools") or {}).get(key) or [])
                      + list((proj.get("tools") or {}).get(key) or []))
    out["tools"] = tools
    return out, ""
