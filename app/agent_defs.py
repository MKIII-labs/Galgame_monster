# -*- coding: utf-8 -*-
"""子代理定义：data/agents/<id>.md（frontmatter + 提示词）。

v1 只读团队：
- 定义里只允许只读工具，写入 / 执行 / 重命名 / 提交类一律拒绝；
- 每个代理可以单独指定 tools / model / max_turns；
- 内置 researcher / reviewer / critic 三个，用户文件同 id 时覆盖内置。
"""
import os
import re

from app.skills import _parse          # 与 SKILL.md 同一套 frontmatter 语法
from app.store import DATA_DIR

AGENTS_DIR = os.path.join(DATA_DIR, "agents")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# v1 允许的工具：只读、不落地、不改仓库
READ_ONLY_TOOLS = {
    "list_dir", "read_file", "read_files", "search_files", "search_code",
    "repo_map", "code_context", "impact", "find_symbol",
    "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover",
    "lsp_symbols", "lsp_implementation", "lsp_calls", "check_code",
    "git_status", "git_diff", "git_log",
    "fetch_url", "web_search", "see_image", "image_compare", "image_analyze",
    "mesh_inspect", "asset_scan", "asset_validate",
    "render_views", "geometry_compare", "scene_assert",
    "read_tool_result",
    "research_note", "explore",
}
# 写权限单独开：permission: write 才允许，且每次写仍走主循环审批
WRITE_TOOLS = {"write_file", "edit_file", "make_dir"}
ALLOWED_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS

DEFAULT_MAX_TURNS = 6
MAX_TURNS_LIMIT = 10

BUILTINS = {
    "researcher": {
        "name": "资料调研员",
        "description": "只联网检索外部资料，结论必须带来源 URL，不碰本地代码",
        "tools": ["web_search", "fetch_url", "research_note"],
        "model": "",
        "max_turns": 6,
        "prompt": (
            "你是主代理派出的资料调研员。你只能联网检索，不能读本地文件、不能写文件、"
            "不能执行命令。目标是把外部事实查准：规格、机制、参数、数据表、接口约定、"
            "参考实现。每条结论后面必须带来源 URL；系统会核对这些 URL 是否真的来自本批"
            "搜索结果，编造链接会被打回。优先官方文档 / 规范 / 源码仓库；论坛二手转述"
            "只作补充。关键参数尽量找第二个独立来源；冲突时把两边都写出来。"
            "查不到的不要猜，写「未查清」并列出还缺什么。结论控制在 600 字以内。"
            "最后一行写：置信度: high|medium|low；未查清: ...（没有就写「无」）。"),
    },
    "reviewer": {
        "name": "代码审查员",
        "description": "只读代码，找缺陷、风险与回归点，不改文件",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "repo_map", "check_code",
                  "git_status", "git_diff", "git_log"],
        "model": "",
        "max_turns": 6,
        "prompt": (
            "你是代码审查员。只能读取与检索，不能写文件、不能执行命令。"
            "围绕分配的任务检查：逻辑错误、边界条件、并发/异常路径、明显的回归风险。"
            "每条问题都必须给出证据（文件:行号 + 关键代码片段），没有证据的猜测不要写。"
            "按严重程度排序，最多 8 条；没有问题就明确说「未发现问题」并说明检查范围。"
            "结论控制在 600 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "fidelity-reviewer": {
        "name": "还原复核员",
        "description": "对照参考物与产物，用客观指标 + 视觉检查列出最大差距，不改产物",
        "tools": ["image_compare", "see_image", "mesh_inspect", "read_file",
                  "list_dir", "search_files"],
        "model": "",
        "max_turns": 5,
        "permission": "readonly",
        "prompt": (
            "你是还原质量复核员。只读，不修改任何产物。"
            "任务是对照「参考物」与「产物」，产出可执行的差距清单。"
            "1) 先用 image_compare 对每一组参考 / 产物出客观指标"
            "（轮廓重合、宽高比、结构相似、主色分布、局部最大差、总分）；"
            "2) 有 3D 模型（.gltf / .glb / .obj）时先用 mesh_inspect 核对三角面、"
            "包围盒尺寸、材质 / 贴图引用与法线 / UV；"
            "3) 指标之外再用 see_image 看整体或局部，补充指标看不到的语义差异；"
            "4) 按「感知影响 × 差距大小」排序，最多列 5 条；每条写：现象、证据"
            "（指标名或图片位置）、建议改法；"
            "5) 不要泛泛评价「不够精致」，不要重写产物；没有明显差距就写"
            "「未发现明显差距」并说明检查范围。"
            "输出：总分 + 指标摘要 + 差距清单。最后一行写："
            "置信度: high|medium|low；未查清: ..."),
    },
    "critic": {
        "name": "反证者",
        "description": "核对其它代理给出的结论与证据，判断是否站得住",
        "tools": ["list_dir", "read_file", "search_files", "search_code",
                  "code_context", "find_symbol", "git_status", "git_diff"],
        "model": "",
        "max_turns": 5,
        "permission": "readonly",
        "prompt": (
            "你是反证者。不要重新探索整个问题，只核对其它代理给出的结论与证据。"
            "逐条检查：引用的文件 / 行号是否真的存在；那几行是否真的支持该结论；"
            "有没有反例、边界条件或另一套实现；彼此之间是否矛盾。"
            "只能读代码，不能写文件 / 执行命令。严格按格式输出，总共不超过 500 字：\n"
            "结论：支持 / 反驳 / 无法判断\n依据：文件:行号\n"
            "修正：一句话（没有就写「无」）\n冲突：无，或写清哪两条互相矛盾"),
    },
    "planner": {
        "name": "规划者",
        "description": "把任务拆成可执行步骤，指出风险与验收方式，不改文件",
        "tools": ["list_dir", "read_file", "read_files", "search_files",
                  "search_code", "code_context", "find_symbol", "repo_map",
                  "git_status", "git_diff"],
        "model": "",
        "max_turns": 4,
        "permission": "readonly",
        "prompt": (
            "你是规划者。先读必要的文件确认现状，再把任务拆成 3–6 个可独立验证的步骤。"
            "每一步写清：做什么、改哪些文件、怎么验证；标出风险与需要用户拍板的点。"
            "不要写文件、不要执行命令。结论控制在 500 字以内，按编号列表输出。"
            "最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "implementer": {
        "name": "实现者",
        "description": "在沙箱内改文件落实方案；每次写入仍要用户审批",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "impact", "repo_map",
                  "check_code", "lsp_diagnostics",
                  "git_status", "git_diff", "git_log",
                  "write_file", "edit_file", "make_dir"],
        "model": "",
        "max_turns": 8,
        "permission": "write",
        "prompt": (
            "你是实现者。按分配的任务在沙箱内改代码；每次写入都会弹审批，"
            "被拒绝就停下说明原因，不要换写法绕过。"
            "动手前先读相关文件确认现状；改完列出改了哪些文件、关键改动在哪几行，"
            "并说明你怎么验证的（跑测试 / 检查输出）。不要改与任务无关的文件。"
            "结论控制在 600 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "tester": {
        "name": "测试工程师",
        "description": "只读代码与测试，指出该补哪些用例、边界与复现步骤",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "repo_map", "check_code",
                  "lsp_diagnostics", "git_status", "git_diff"],
        "model": "",
        "max_turns": 6,
        "permission": "readonly",
        "prompt": (
            "你是测试工程师。只看代码与现有测试，不写文件、不执行命令。"
            "围绕分配的任务回答三件事：① 现有测试覆盖了什么、漏了什么；"
            "② 最值得补的用例（正常 / 边界 / 异常 / 并发），每条写清输入、预期与验证方式；"
            "③ 如果已经出问题，给出最小复现步骤。"
            "尽量指出应该改哪个测试文件、哪个断言，不要空泛地说「增加测试」。"
            "结论控制在 600 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "debugger": {
        "name": "调试员",
        "description": "根据报错或症状定位根因，给出最小修复方案，不改文件",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "impact", "repo_map",
                  "lsp_diagnostics", "lsp_definition", "lsp_references",
                  "git_status", "git_diff", "git_log"],
        "model": "",
        "max_turns": 6,
        "permission": "readonly",
        "prompt": (
            "你是调试员。只读代码，不写文件、不执行命令。"
            "按「症状 → 可能的调用链 → 证据 → 根因」的顺序排查，不要一上来就给结论。"
            "每条判断都要有文件:行号或日志证据；不确定就列出待验证的假设。"
            "最后给最小修复方案（改哪个函数、为什么、可能的副作用），不要顺手重构。"
            "结论控制在 600 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "security": {
        "name": "安全审查员",
        "description": "只读审查注入、路径穿越、密钥泄露与权限边界",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "repo_map", "git_diff", "git_log"],
        "model": "",
        "max_turns": 6,
        "permission": "readonly",
        "prompt": (
            "你是安全审查员。只读代码，不改文件、不执行命令。"
            "重点看：命令 / 路径注入、沙箱越界、密钥与凭据泄露、权限判断缺失、"
            "不可信输入直接进 shells / 文件系统 / SQL、以及错误信息泄露内部路径。"
            "只报有实际利用路径的问题，按严重程度（高 / 中 / 低）排序，"
            "每条写清触发条件与最小修复方向；不要堆砌泛泛的「建议加校验」。"
            "结论控制在 600 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "documenter": {
        "name": "文档编写员",
        "description": "读代码后更新 README / 注释 / 使用说明，可写但每次写入要审批",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "repo_map",
                  "git_status", "git_diff",
                  "write_file", "edit_file", "make_dir"],
        "model": "",
        "max_turns": 6,
        "permission": "write",
        "prompt": (
            "你是文档编写员。按分配的任务补充或更新文档、注释与使用说明；"
            "每次写入都会弹审批，被拒绝就停下说明原因。"
            "动笔前先读实现与现有文档，只写代码里能验证的事实，不编造参数和行为。"
            "优先改最小范围：补缺失段落、修过时描述、加一段可复制的示例；"
            "不要重写整篇文档，也不要改与文档无关的代码。"
            "改完列出动了哪些文件与关键段落。结论控制在 500 字以内。"
            "最后一行写：置信度: high|medium|low；未查清: ..."),
    },
    "explainer": {
        "name": "代码讲解员",
        "description": "面向新人的模块讲解：调用关系、数据流与关键设计",
        "tools": ["list_dir", "read_file", "read_files", "search_files", "search_code",
                  "code_context", "find_symbol", "repo_map",
                  "lsp_definition", "lsp_references", "lsp_hover", "lsp_symbols",
                  "git_log"],
        "model": "",
        "max_turns": 5,
        "permission": "readonly",
        "prompt": (
            "你是代码讲解员。只读代码，不写文件、不执行命令。"
            "把目标模块讲清楚：入口在哪、上下游怎么调用、数据怎么流动、"
            "关键状态与副作用是什么、哪些设计是刻意的取舍。"
            "每个结论都带文件:行号；先给一张「调用关系」文字图（几行即可），再展开细节。"
            "面向没读过这个仓库的人，术语第一次出现时解释一句。"
            "结论控制在 700 字以内。最后一行写：置信度: high|medium|low；未查清: ..."),
    },
}


class AgentDefError(ValueError):
    """可直接展示给用户的定义错误。"""


def _dir():
    os.makedirs(AGENTS_DIR, exist_ok=True)
    return AGENTS_DIR


def _path(aid):
    return os.path.join(_dir(), aid + ".md")


def _tools_list(raw):
    text = str(raw or "").strip().strip("[]")
    parts = re.split(r"[\s,，]+", text)
    out = []
    for p in parts:
        p = p.strip().strip("'\"")
        if p and p not in out:
            out.append(p)
    return out


def _render(meta, prompt):
    lines = ["---"]
    for key in ("name", "description", "tools", "model", "max_turns",
                "permission", "enabled"):
        val = meta.get(key)
        if isinstance(val, list):
            val = ", ".join(val)
        if val is None or val == "":
            continue
        lines.append("%s: %s" % (key, val))
    lines.append("---")
    lines.append("")
    lines.append(str(prompt or "").strip())
    return "\n".join(lines) + "\n"


def _normalize(aid, meta, body, builtin=False):
    if not _ID_RE.match(aid):
        raise AgentDefError("id 只能包含字母、数字、下划线和短横线（≤32）")
    name = str(meta.get("name") or aid).strip()[:40]
    desc = str(meta.get("description") or "").strip()[:200]
    tools = _tools_list(meta.get("tools"))
    if not tools:
        raise AgentDefError("%s 缺少 tools" % aid)
    permission = str(meta.get("permission") or "readonly").strip().lower()
    if permission not in ("readonly", "write"):
        raise AgentDefError("permission 只能是 readonly / write：%s" % permission)
    allowed = set(READ_ONLY_TOOLS)
    if permission == "write":
        allowed |= WRITE_TOOLS
    bad = [t for t in tools if t not in allowed]
    if bad:
        raise AgentDefError(
            "权限 %s 不允许这些工具：%s" % (permission, "、".join(bad)))
    try:
        turns = int(meta.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        turns = DEFAULT_MAX_TURNS
    turns = max(1, min(turns, MAX_TURNS_LIMIT))
    prompt = str(body or "").strip()
    if not prompt:
        raise AgentDefError("%s 缺少系统提示词（frontmatter 之后的正文）" % aid)
    enabled = str(meta.get("enabled") or "true").strip().lower() not in ("false", "0", "no")
    return {
        "id": aid,
        "name": name,
        "description": desc,
        "tools": tools,
        "model": str(meta.get("model") or "").strip()[:80],
        "max_turns": turns,
        "permission": permission,
        "enabled": enabled,
        "prompt": prompt,
        "builtin": bool(builtin),
        "path": _path(aid),
    }


def _builtin(aid):
    spec = BUILTINS[aid]
    meta = {
        "name": spec["name"], "description": spec["description"],
        "tools": spec["tools"], "model": spec["model"],
        "max_turns": spec["max_turns"],
        "permission": spec.get("permission") or "readonly",
        "enabled": "true",
    }
    obj = _normalize(aid, meta, spec["prompt"], builtin=True)
    obj["text"] = _render(meta, spec["prompt"])
    obj["path"] = ""
    return obj


def get_agent(aid):
    """按 id 取代理；用户文件覆盖内置；不存在 / 已停用返回 None。"""
    aid = str(aid or "").strip()
    if not _ID_RE.match(aid):
        return None
    path = _path(aid)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            raise AgentDefError("读取 %s 失败：%s" % (aid, e))
        meta, body = _parse(raw)
        obj = _normalize(aid, meta, body, builtin=False)
        obj["text"] = raw
        return obj if obj["enabled"] else None
    if aid in BUILTINS:
        obj = _builtin(aid)
        return obj if obj["enabled"] else None
    return None


def list_agents():
    """内置 + 用户文件；用户文件同 id 覆盖内置。读取失败的文件也会列出并带 error。"""
    rows = {}
    for aid in BUILTINS:
        rows[aid] = _builtin(aid)
    if os.path.isdir(AGENTS_DIR):
        for fn in sorted(os.listdir(AGENTS_DIR)):
            if not fn.endswith(".md"):
                continue
            aid = fn[:-3]
            if not _ID_RE.match(aid):
                continue
            try:
                with open(os.path.join(AGENTS_DIR, fn), "r", encoding="utf-8") as f:
                    raw = f.read()
                meta, body = _parse(raw)
                obj = _normalize(aid, meta, body, builtin=False)
                obj["text"] = raw
                rows[aid] = obj
            except (OSError, AgentDefError) as e:
                rows[aid] = {
                    "id": aid, "name": aid, "description": "", "tools": [],
                    "model": "", "max_turns": DEFAULT_MAX_TURNS, "enabled": False,
                    "prompt": "", "builtin": False, "text": "", "path": _path(aid),
                    "error": str(e),
                }
    return [rows[k] for k in sorted(rows)]


def save_agent(aid, text, create=False, old_id=""):
    aid = str(aid or "").strip()
    if not _ID_RE.match(aid):
        raise AgentDefError("id 只能包含字母、数字、下划线和短横线（≤32）")
    meta, body = _parse(text)
    obj = _normalize(aid, meta, body, builtin=aid in BUILTINS)
    if create and (os.path.isfile(_path(aid)) or aid in BUILTINS):
        raise AgentDefError("id 已存在：%s" % aid)
    old = str(old_id or "").strip()
    if old and old != aid and (os.path.isfile(_path(aid)) or aid in BUILTINS):
        raise AgentDefError("目标 id 已存在：%s" % aid)
    with open(_path(aid), "w", encoding="utf-8") as f:
        f.write(str(text))
    if old and old != aid and _ID_RE.match(old):
        old_path = _path(old)
        if os.path.isfile(old_path):
            os.remove(old_path)          # 改名：删掉旧的自定义文件
    obj["text"] = str(text)
    return obj


def set_enabled(aid, enabled):
    """启用 / 停用；内置代理会落一份用户文件来保存开关。"""
    aid = str(aid or "").strip()
    if not _ID_RE.match(aid):
        raise AgentDefError("id 不合法")
    path = _path(aid)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        meta, body = _parse(raw)
    elif aid in BUILTINS:
        spec = _builtin(aid)
        meta, body = _parse(spec["text"])
    else:
        raise AgentDefError("找不到子代理：%s" % aid)
    meta["enabled"] = "true" if enabled else "false"
    text = _render(meta, body)
    obj = _normalize(aid, meta, body, builtin=aid in BUILTINS)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    obj["text"] = text
    return obj


def delete_agent(aid):
    """删除用户覆盖；内置代理会回退到默认定义。"""
    aid = str(aid or "").strip()
    if not _ID_RE.match(aid):
        return False
    path = _path(aid)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def role_spec(agent):
    """转成 explore 需要的角色配置。"""
    return {"name": agent.get("name") or agent.get("id") or "",
            "tools": list(agent.get("tools") or []),
            "prompt": agent.get("prompt") or "",
            "model": agent.get("model") or "",
            "allow_write": str(agent.get("permission") or "readonly") == "write",
            "max_turns": int(agent.get("max_turns") or DEFAULT_MAX_TURNS)}
