# -*- coding: utf-8 -*-
"""工具元层：把高频工具收敛成少量 action 工具，降低每轮 schema 预算。

设计约束：
- 只影响「下发给模型的 schema」与「模型调用的入口」；真正的执行仍走
  agent._HANDLERS 里的旧工具名，所以审批 / 沙箱 / 写入检测 / 撤销全部复用。
- 旧工具名继续可用（alias），插件、旧会话、文本协议不受影响。
- 每个 meta 工具只暴露本组可见的 action；被 plan_mode / 懒加载隐藏的 action
  不会出现在 enum 里。
"""
import copy

# action -> (legacy_name, required_keys, arg_mapper)
# arg_mapper 为 None 时按原样透传（去掉 action 字段）。
META = {
    "read": {
        "desc": "读取与检索。action=list 列目录 / file 读文件 / files 批量读 / "
                "search 正则搜 / semantic 语义搜 / map 仓库结构。",
        "params": {
            "path": {"type": "string", "description": "文件或目录（沙箱内相对路径）"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "批量读取的文件列表（最多 16 个）"},
            "start": {"type": "integer", "description": "起始行，从 1 开始"},
            "limit": {"type": "integer", "description": "最多读取 / 返回多少行"},
            "raw": {"type": "boolean", "description": "true = 返回原文（不加行号）"},
            "pattern": {"type": "string", "description": "search 用的正则"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
            "context": {"type": "integer", "description": "命中前后各显示几行（0–5）"},
            "max_per_file": {"type": "integer", "description": "每个文件最多列几处"},
            "count_only": {"type": "boolean", "description": "只统计命中数量"},
            "query": {"type": "string", "description": "semantic 的自然语言查询"},
            "max_files": {"type": "integer", "description": "map 最多列多少个文件"},
        },
        "actions": {
            "list": ("list_dir", ["path"], None),
            "file": ("read_file", ["path"], None),
            "files": ("read_files", ["paths"], None),
            "search": ("search_files", ["pattern"], None),
            "semantic": ("search_code", ["query"], None),
            "map": ("repo_map", [], None),
        },
    },
    "edit": {
        "desc": "写文件。action=write 覆盖/新建 / patch 精确替换 / mkdir 建目录 / "
                "move 移动 / copy 复制 / delete 删除。写前必须先 read。",
        "params": {
            "path": {"type": "string", "description": "目标文件或目录"},
            "content": {"type": "string", "description": "write 的完整内容"},
            "old": {"type": "string", "description": "patch 要替换的原文（必须唯一）"},
            "new": {"type": "string", "description": "patch 的新文本"},
            "from": {"type": "string", "description": "move / copy 的源路径"},
            "to": {"type": "string", "description": "move / copy 的目标路径"},
            "overwrite": {"type": "boolean", "description": "copy 时是否覆盖，默认 false"},
        },
        "actions": {
            "write": ("write_file", ["path", "content"], None),
            "patch": ("edit_file", ["path", "old", "new"], None),
            "mkdir": ("make_dir", ["path"], None),
            "move": ("move_file", ["from", "to"], None),
            "copy": ("copy_file", ["from", "to"], None),
            "delete": ("delete_file", ["path"], None),
        },
    },
    "run": {
        "desc": "执行命令。action=exec 一次性命令（argv 数组，无管道）/ "
                "terminal 持久终端（PowerShell，支持管道 / 后台 / 交互）。",
        "params": {
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "exec 的命令与参数，如 [\"go\",\"test\",\"./...\"]"},
            "cwd": {"type": "string", "description": "工作目录，默认沙箱根"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
            "command": {"type": "string", "description": "terminal 的整条命令行"},
            "wait": {"type": "integer", "description": "terminal 等待输出秒数，默认 8"},
            "op": {"type": "string", "enum": ["run", "read", "interrupt", "close"],
                   "description": "terminal 子操作，默认 run"},
        },
        "actions": {
            "exec": ("run_command", ["argv"], None),
            "terminal": ("shell", [], "terminal"),
        },
    },
    "code": {
        "desc": "代码智能。action=diagnostics 类型检查 / definition 定义 / references 引用 / "
                "hover 签名 / symbols 符号树 / implementation 实现 / calls 调用链 / "
                "rename 语义重命名 / check 语法体检 / find 正则找符号 / impact 变更影响 / "
                "context 一次给全上下文。",
        "params": {
            "path": {"type": "string", "description": "文件或目录（沙箱内相对路径）"},
            "symbol": {"type": "string", "description": "符号名"},
            "query": {"type": "string", "description": "自然语言查询 / 符号名片段"},
            "target": {"type": "string", "description": "impact 的符号名或文件路径"},
            "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
            "new_name": {"type": "string", "description": "rename 的新名字"},
            "direction": {"type": "string", "enum": ["incoming", "outgoing"],
                          "description": "calls 的方向，默认 incoming"},
            "limit": {"type": "integer", "description": "返回条数上限"},
        },
        "actions": {
            "diagnostics": ("lsp_diagnostics", ["path"], None),
            "definition": ("lsp_definition", ["path", "line"], None),
            "references": ("lsp_references", ["path", "line"], None),
            "hover": ("lsp_hover", ["path", "line"], None),
            "symbols": ("lsp_symbols", [], None),
            "implementation": ("lsp_implementation", ["path", "line"], None),
            "calls": ("lsp_calls", ["path", "line"], None),
            "rename": ("lsp_rename", ["path", "line", "symbol", "new_name"], None),
            "check": ("check_code", [], None),
            "find": ("find_symbol", ["symbol"], "find"),
            "impact": ("impact", ["target"], None),
            "context": ("code_context", [], None),
        },
    },
    "git": {
        "desc": "Git。action=status 状态 / diff 改动 / log 历史 / commit 提交 / "
                "split 分组提交。",
        "params": {
            "path": {"type": "string", "description": "文件路径（diff 用）"},
            "staged": {"type": "boolean", "description": "diff 是否看暂存区"},
            "limit": {"type": "integer", "description": "log 条数，默认 20"},
            "message": {"type": "string", "description": "commit 提交信息"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "commit 只提交这些路径"},
            "all": {"type": "boolean", "description": "commit 前先 git add -A"},
            "groups": {"type": "array", "items": {"type": "object"},
                       "description": "split 的提交分组"},
        },
        "actions": {
            "status": ("git_status", [], None),
            "diff": ("git_diff", ["path"], None),
            "log": ("git_log", [], None),
            "commit": ("git_commit", ["message"], None),
            "split": ("split_commit", ["groups"], None),
        },
    },
    "plan": {
        "desc": "执行计划。action=update 整体更新 / patch 局部增删改。",
        "params": {
            "goal": {"type": "string"},
            "done_when": {"type": "string"},
            "milestones": {"type": "array", "items": {"type": "object"}},
            "steps": {"type": "array", "items": {"type": "object"}},
            "retro": {"type": "object"},
            "log": {"type": "object"},
            "note": {"type": "string"},
            "reason": {"type": "string"},
            "add_milestones": {"type": "array", "items": {"type": "object"}},
            "add_steps": {"type": "array", "items": {"type": "object"}},
            "update_steps": {"type": "array", "items": {"type": "object"}},
            "remove_steps": {"type": "array", "items": {"type": "string"}},
        },
        "actions": {
            "update": ("update_plan", [], None),
            "patch": ("plan_patch", [], None),
        },
    },
    "agent": {
        "desc": "子代理。action=delegate 派出并行子代理 / explore 调研 / followup 追问 / "
                "status 状态 / result 结果 / cancel 取消。",
        "params": {
            "goal": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "deliverable": {"type": "string"},
            "acceptance": {"type": "array", "items": {"type": "object"}},
            "allow_write": {"type": "boolean"},
            "research": {"type": "boolean"},
            "verify": {"type": "boolean"},
            "background": {"type": "boolean"},
            "agents": {"type": "array", "items": {"type": "object"}},
            "pipeline": {"type": "array", "items": {"type": "object"}},
            "task": {"type": "string"},
            "batch": {"type": "string"},
        },
        "actions": {
            "delegate": ("delegate", ["agents"], None),
            "explore": ("explore", ["goal", "questions"], None),
            "followup": ("explore_followup", ["batch", "questions"], None),
            "status": ("explore_status", ["batch"], None),
            "result": ("explore_result", ["batch"], None),
            "cancel": ("explore_cancel", ["batch"], None),
        },
    },
    "web": {
        "desc": "联网。action=search 搜索（复刻/还原类任务分轮补搜）/ fetch 抓网页 / "
                "note 调研记录。",
        "params": {
            "queries": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "url": {"type": "string"},
            "chars": {"type": "integer"},
            "op": {"type": "string", "description": "note 的子操作，如 add / list"},
            "gaps": {"type": "array", "items": {"type": "object"}},
            "ids": {"type": "array", "items": {"type": "string"}},
            "claims": {"type": "array", "items": {"type": "object"}},
        },
        "actions": {
            "search": ("web_search", ["queries"], None),
            "fetch": ("fetch_url", ["url"], None),
            "note": ("research_note", [], "note"),
        },
    },
    "history": {
        "desc": "写操作历史。action=list 列出 / undo 撤销 / redo 重做 / recover 恢复文件。",
        "params": {
            "token": {"type": "string", "description": "list 给出的步骤 id"},
            "path": {"type": "string", "description": "要恢复的路径"},
            "force": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
        },
        "actions": {
            "list": ("list_steps", [], None),
            "undo": ("undo_step", ["token"], None),
            "redo": ("redo_step", ["token"], None),
            "recover": ("recover_file", [], None),
        },
    },
    # ---------------- 低频组：默认懒加载 ----------------
    "visual": {
        "desc": "网页 / 游戏画面自查。action=render 运行时检查 / qa 量化视觉 / "
                "views 多视图截图 / geometry 多视图对照 / scene 场景断言。",
        "params": {
            "path": {"type": "string"}, "viewport": {"type": "string"},
            "viewports": {"type": "array", "items": {"type": "string"}},
            "screenshot": {"type": "boolean"}, "describe": {"type": "boolean"},
            "baseline": {"type": "string"}, "save_baseline": {"type": "boolean"},
            "views": {"type": "array"}, "timeout": {"type": "integer"},
            "reference": {"type": "array", "items": {"type": "string"}},
            "candidate": {"type": "array", "items": {"type": "string"}},
            "names": {"type": "array", "items": {"type": "string"}},
            "region": {"type": "array", "items": {"type": "number"}},
            "max_side": {"type": "integer"}, "min_score": {"type": "number"},
            "assertions": {"type": "array", "items": {"type": "object"}},
        },
        "actions": {
            "render": ("render_check", ["path"], None),
            "qa": ("visual_check", ["path"], None),
            "views": ("render_views", ["path"], None),
            "geometry": ("geometry_compare", ["reference", "candidate"], None),
            "scene": ("scene_assert", ["path", "assertions"], None),
        },
    },
    "image": {
        "desc": "图片理解。action=see 看图 / compare 参考图与产物对比 / analyze 尺寸与色彩分析。",
        "params": {
            "path": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}},
            "question": {"type": "string"}, "native": {"type": "boolean"},
            "reference": {"type": "string"}, "candidate": {"type": "string"},
            "region": {"type": "array", "items": {"type": "number"}},
            "max_side": {"type": "integer"}, "panels": {"type": "integer"},
        },
        "actions": {
            "see": ("see_image", [], None),
            "compare": ("image_compare", ["reference", "candidate"], None),
            "analyze": ("image_analyze", ["path"], None),
        },
    },
    "asset": {
        "desc": "素材。action=scan 扫描 / convert 转换 / atlas 图集 / inline 内联 / "
                "validate 校验 / mesh 3D 网格检查。",
        "params": {
            "path": {"type": "string"}, "out": {"type": "string"},
            "from": {"type": "string"}, "to": {"type": "string"},
            "format": {"type": "string"}, "width": {"type": "integer"},
            "height": {"type": "integer"}, "quality": {"type": "integer"},
            "images": {"type": "array", "items": {"type": "string"}},
            "map": {"type": "string"}, "max_size": {"type": "integer"},
            "padding": {"type": "integer"}, "max_kb": {"type": "integer"},
            "manifest": {"type": "string"}, "units": {"type": "string"},
        },
        "actions": {
            "scan": ("asset_scan", [], None),
            "convert": ("asset_convert", ["from", "to"], None),
            "atlas": ("asset_atlas", ["images", "out"], None),
            "inline": ("asset_inline", ["images", "out"], None),
            "validate": ("asset_validate", [], None),
            "mesh": ("mesh_inspect", ["path"], None),
        },
    },
    "audio": {
        "desc": "音频。action=probe 探测响度/时长 / compare 两段音频对比。",
        "params": {"path": {"type": "string"}, "a": {"type": "string"}, "b": {"type": "string"}},
        "actions": {
            "probe": ("audio_probe", ["path"], None),
            "compare": ("audio_compare", ["a", "b"], None),
        },
    },
    "workflow": {
        "desc": "工作流。action=run 运行 / script 脚本 / status 状态 / resume 续跑 / cancel 取消。",
        "params": {
            "workflow": {"type": "object"}, "code": {"type": "string"},
            "timeout": {"type": "integer"}, "id": {"type": "string"},
        },
        "actions": {
            "run": ("run_workflow", ["workflow"], None),
            "script": ("run_workflow_script", ["code"], None),
            "status": ("workflow_status", ["id"], None),
            "resume": ("workflow_resume", ["id"], None),
            "cancel": ("workflow_cancel", ["id"], None),
        },
    },
    "utility": {
        "desc": "杂项。action=sleep 等待 / reminder 定时提醒。",
        "params": {
            "seconds": {"type": "integer"}, "at": {"type": "string"},
            "text": {"type": "string"}, "repeat": {"type": "string"},
        },
        "actions": {
            "sleep": ("sleep", ["seconds"], None),
            "reminder": ("set_reminder", ["at", "text"], None),
        },
    },
}

# 固定顺序：核心 meta 在前，低频组在后，便于模型先看到常用能力。
META_ORDER = ["read", "edit", "run", "code", "git", "plan", "agent", "web", "history",
              "visual", "image", "asset", "audio", "workflow", "utility"]

# group -> meta 名（tool_search 目录用）
GROUP_META = {"visual": "visual", "image": "image", "asset": "asset",
              "audio": "audio", "workflow": "workflow", "utility": "utility"}


def _terminal(vals):
    out = dict(vals)
    out["action"] = out.pop("op", None) or "run"
    return out


def _note(vals):
    out = dict(vals)
    out["action"] = out.pop("op", None) or "add"
    return out


def _find(vals):
    out = dict(vals)
    out["name"] = out.pop("symbol", None) or out.get("query")
    return out


_MAPPERS = {"terminal": _terminal, "note": _note, "find": _find}


def normalize(name, args):
    """meta 调用 → (legacy_name, args)。不是 meta / action 不合法时返回 None。"""
    meta = META.get(str(name or "").strip().lower())
    if not meta or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "").strip().lower()
    if not action or action not in meta["actions"]:
        return None
    legacy, _req, mapper = meta["actions"][action]
    out = {k: v for k, v in args.items() if k != "action"}
    fn = _MAPPERS.get(mapper) if isinstance(mapper, str) else mapper
    if fn:
        out = fn(out)
    return legacy, out


def required_for(name, action):
    meta = META.get(str(name or "").strip().lower())
    if not meta:
        return []
    hit = meta["actions"].get(str(action or "").strip().lower())
    return list(hit[1]) if hit else []


def _schema(name, meta, actions):
    props = {"action": {"type": "string", "enum": actions,
                        "description": "本工具的 action；其余参数按 action 取用"}}
    props.update(copy.deepcopy(meta.get("params") or {}))
    return {"type": "function",
            "function": {"name": name, "description": meta["desc"],
                         "parameters": {"type": "object", "properties": props,
                                        "required": ["action"],
                                        "additionalProperties": False}}}


def build_schemas(visible_names):
    """按可见的旧工具名生成 meta schema；返回 (schemas, 已被吸收的旧名集合)。"""
    visible = set(visible_names or ())
    out, used = [], set()
    for name in META_ORDER:
        meta = META[name]
        actions = [a for a, (legacy, _r, _m) in meta["actions"].items()
                   if legacy in visible]
        if not actions:
            continue
        out.append(_schema(name, meta, actions))
        used.update(meta["actions"][a][0] for a in actions)
    return out, used


def catalog_line(meta_name):
    meta = META.get(meta_name)
    if not meta:
        return meta_name
    acts = "|".join(meta["actions"].keys())
    return "%s（action=%s）" % (meta_name, acts)


def group_catalog(group, legacy_names):
    """tool_search 用的一行说明：优先显示 meta 名，否则列旧名。"""
    meta_name = GROUP_META.get(group)
    if meta_name:
        return catalog_line(meta_name), [meta_name]
    return " / ".join(legacy_names), list(legacy_names)
