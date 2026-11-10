# -*- coding: utf-8 -*-

import threading
from app import computer
from app import tool_meta



PLAN_MODE_TOOLS = frozenset((
    "list_dir", "read_file", "read_files", "search_files", "search_code", "repo_map",
    "code_context", "impact", "find_symbol", "check_code",
    "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover",
    "lsp_symbols", "lsp_implementation", "lsp_calls",
    "git_status", "git_diff", "git_log", "list_steps",
    "see_image", "image_compare", "image_analyze", "attachment_read", "read_tool_result",
    "mesh_inspect", "asset_scan", "asset_validate",
    "visual_check", "render_views", "scene_assert", "geometry_compare",
    "fetch_url", "web_search",
    "acceptance", "update_plan", "plan_patch", "research_note",
    "explore", "explore_followup", "explore_status", "explore_result",
    "explore_cancel", "ask_user", "mcp_list_servers", "mcp_list_tools",
    "sleep",
))


BLOCKED_SEGMENTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                    ".browser_profile", ".idea", ".vscode"}

# ---- 秘密文件护栏 ----
# 多数「外传」不是 AI 主动偷，而是它顺手读了 .env / 密钥再写进别的地方。
# 这些文件默认不读、不参与内容检索；确需读取时可在 .gm/agent-rules.json 的
# allow 里显式放行（规则文件优先于这里的默认拒绝）。


SEE_MAX_IMAGES = 4                    # see_image 一次最多看几张（多图同请求做对照）


SEE_MAX_TOTAL = 24 * 1024 * 1024      # 多图合计上限


TOOLS = ("list_dir", "read_file", "read_files", "search_files", "search_code", "repo_map", "write_file", "edit_file",
         "code_context", "impact",
         "make_dir", "move_file", "copy_file", "delete_file", "run_command", "shell",
         "fetch_url", "sleep", "render_check",
         "visual_check", "render_views", "scene_assert", "geometry_compare",
         "asset_scan", "asset_convert", "asset_atlas", "asset_inline", "asset_validate",
         "mesh_inspect",
         "audio_probe", "audio_compare", "see_image", "image_compare",
         "image_analyze", "attachment_read", "read_tool_result", "archify",
         "find_symbol", "check_code", "lsp_diagnostics", "lsp_definition",
         "lsp_references", "lsp_rename", "lsp_hover", "lsp_symbols",
         "lsp_implementation", "lsp_calls",
         "git_status", "git_diff", "git_log", "git_commit", "split_commit",
         "list_steps", "undo_step", "redo_step", "recover_file",
         "acceptance",
         "update_plan", "plan_patch",
         "run_workflow_script",
         "research_note",
         "run_workflow", "workflow_status", "workflow_resume", "workflow_cancel",
         "explore",
         "explore_followup",
         "explore_status", "explore_result", "explore_cancel",
         "ask_user",
         "computer_open", "computer_see", "computer_act", "computer_read", "computer_shot",
         "computer_windows",
         "mcp_list_servers", "mcp_list_tools")


TOOLS = TOOLS + tuple(tool_meta.META)


COMPUTER_TOOLS = computer.TOOLS


WRITE_TOOLS = ("write_file", "edit_file", "make_dir", "move_file", "copy_file", "delete_file",
               "lsp_rename", "recover_file",
               "asset_convert", "asset_atlas", "asset_inline")


LSP_TOOLS = {"lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_rename",
             "lsp_hover", "lsp_symbols", "lsp_implementation", "lsp_calls"}


def writes_files(name, args=None):
    """这次调用会不会在沙箱里落文件（决定是否走写入审批）。

    `archify` 只有 finalize 会写产物；schema / doctor 是只读的，不该每次弹审批。
    """
    n = str(name or "").strip()
    if n in WRITE_TOOLS:
        return True
    if n == "archify":
        a = args if isinstance(args, dict) else {}
        return str(a.get("action") or "finalize").strip().lower() == "finalize"
    return False


RUN_TOOLS = ("run_command",)          # 执行类：不走写入审批，走 judge_command
# 持久 shell：等于任意命令执行，无法按可执行白名单分类，审批单独走 judge_shell


GIT_WRITE_TOOLS = ("git_commit", "split_commit")
# 允许「读取沙箱外文件」的工具（仅在用户开启授权后生效；写入类工具永远只在沙箱内）


OUTSIDE_READ_TOOLS = ("list_dir", "read_file", "read_files", "see_image",
                      "image_compare", "image_analyze", "geometry_compare")


_PLUGIN_TOOLS = {}


_PLUGIN_TOOLS_LOCK = threading.Lock()


FILE_TOOLS = {"list_dir", "read_file", "read_files", "search_files", "search_code", "repo_map",
              "code_context", "impact", "write_file",
              "edit_file", "make_dir", "move_file", "copy_file", "delete_file",
              # 下面这些同样以沙箱为根，归到同一类：normalize_tool_name 与
              # 「没开 Agent 就不能用」的判定都靠它
              "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_rename",
              "lsp_hover", "lsp_symbols", "lsp_implementation", "lsp_calls",
               "git_status", "git_diff", "git_log", "git_commit", "split_commit",
              "list_steps", "undo_step", "redo_step", "recover_file",
              "visual_check", "asset_scan", "asset_convert", "asset_atlas",
              "asset_inline", "asset_validate", "run_workflow_script"}

# 模型常见的「同义词 / 简写 / OpenAI 风格下划线名」→ 本项目工具名。
# 这里只做**保守**映射：不确定的别名不映射，避免把正常工具改错。


TOOL_ALIASES = {
    "read": "read_file", "cat": "read_file", "open_file": "read_file",
    "readfile": "read_file", "read_text": "read_file", "view_file": "read_file",
    "ls": "list_dir", "list": "list_dir", "dir": "list_dir",
    "listdir": "list_dir", "list_files": "list_dir", "list_directory": "list_dir",
    "grep": "search_files", "find_in_files": "search_files",
    "searchfiles": "search_files",
    "write": "write_file", "create_file": "write_file", "save_file": "write_file",
    "writefile": "write_file",
    "edit": "edit_file", "replace": "edit_file", "patch": "edit_file",
    "editfile": "edit_file",
    "mkdir": "make_dir", "create_dir": "make_dir", "createdir": "make_dir",
    "move": "move_file", "rename": "move_file", "mv": "move_file",
    "movefile": "move_file",
    "deletefile": "delete_file",
    "run": "run_command", "exec": "run_command", "shell": "run_command",
    "bash": "run_command", "terminal": "run_command", "exec_command": "run_command",
    "run_shell": "run_command", "shell_command": "run_command",
    "render": "render_check", "screenshot": "render_check", "render_page": "render_check",
    "rendercheck": "render_check", "preview_page": "render_check",
    "visual": "visual_check", "visual_qa": "visual_check", "visualcheck": "visual_check",
    "multi_view": "render_views", "multi_views": "render_views", "views": "render_views",
    "render_view": "render_views", "screenshots": "render_views",
    "compare_views": "geometry_compare", "geometry_diff": "geometry_compare",
    "view_compare": "geometry_compare",
    "scene_check": "scene_assert", "assert_scene": "scene_assert",
    "scene_validate": "scene_assert",
    "diagram": "archify", "architecture_diagram": "archify", "flowchart": "archify",
    "sequence_diagram": "archify", "erd": "archify", "explainer": "archify",
    "scan_assets": "asset_scan", "assets": "asset_scan",
    "convert_asset": "asset_convert", "pack_atlas": "asset_atlas",
    "inline_assets": "asset_inline", "validate_assets": "asset_validate",
    "inspect_mesh": "mesh_inspect", "model_inspect": "mesh_inspect",
    "mesh": "mesh_inspect", "mesh_check": "mesh_inspect",
    "3d_inspect": "mesh_inspect", "gltf_validate": "mesh_inspect",
    "listen": "audio_probe", "hear": "audio_probe", "probe_audio": "audio_probe",
    "audio": "audio_probe", "audio_check": "audio_probe", "sound_check": "audio_probe",
    "audio_diff": "audio_compare", "compare_audio": "audio_compare",
    "audio_ab": "audio_compare", "ab_audio": "audio_compare",
    "look": "see_image", "view_image": "see_image", "read_image": "see_image",
    "see": "see_image", "image": "see_image", "describe_image": "see_image",
    "compare_images": "image_compare", "image_diff": "image_compare",
    "analyze_image": "image_analyze", "image_measure": "image_analyze",
    "browser_open": "computer_open", "open_url": "computer_open", "browser_see": "computer_see",
    "browser_snapshot": "computer_see", "browser_act": "computer_act",
    "browser_click": "computer_act", "browser_type": "computer_act",
    "browser_read": "computer_read", "browser_screenshot": "computer_shot",
    "plan": "update_plan", "todo": "update_plan", "update_todo": "update_plan",
    "plan_patch": "plan_patch", "patch_plan": "plan_patch", "plan_update": "plan_patch",
    "todo_patch": "plan_patch",
    "todo_write": "update_plan", "set_plan": "update_plan",
    "search_web": "web_search", "web": "web_search", "google": "web_search",
    "browse": "web_search",
    "remind": "set_reminder", "reminder": "set_reminder", "alarm": "set_reminder",
    "calendar_create": "calendar.create", "create_event": "calendar.create",
    "calendar_list": "calendar.list", "calendar_update": "calendar.update",
    "calendar_delete": "calendar.delete",
    "memory_search": "memory.search", "recall": "memory.search",
    "remember": "memory.remember", "forget": "memory.forget",
    "task_create": "task.create", "task_list": "task.list", "task_get": "task.get",
    "bili": "bili.analyze", "bilibili": "bili.analyze",
    "bili_analyze": "bili.analyze", "bili_up": "bili.up",
    "bili_search": "bili.up", "bili_comments": "bili.comments",
    "bili_danmaku": "bili.danmaku", "bili_transcript": "bili.transcript",
    "bili_timeline": "bili.timeline",
    "ask": "ask_user", "question": "ask_user",
    # 显式收尾：模型常写成 done / complete / task_done（Trae 的叫法）
    "done": "finish", "complete": "finish", "finished": "finish",
    "task_done": "finish", "task_complete": "finish", "finish_task": "finish",
}


def _capability_ids():
    try:
        from app import capabilities
        return set(capabilities.ids())
    except Exception:
        return set()


_CAP_NAMESPACES = {"bili", "calendar", "stock", "memory", "task", "note",
                   "observe", "skill", "doc", "llm", "search",
                   "workflow", "computer", "gal", "kb", "image"}


def is_known_tool(name):
    """这个工具名是不是真的存在。

    只用于「正文里裸写的 JSON 要不要当调用」这一判断：模型偶尔直接吐
    {"calls":[...]}，而正文里也可能有示例 JSON；要求工具名真实存在，
    可以把「讲解格式」和「真的想调工具」区分开。
    """
    from .dispatch import normalize_tool_name
    raw = str(name or "").strip()
    if not raw:
        return False
    if raw.lower() in tool_meta.META:
        return True
    try:
        n = normalize_tool_name(raw)
    except Exception:
        return False
    if n in TOOLS or n in COMPUTER_TOOLS:
        return True
    if n.startswith("mcp__") or n in _PLUGIN_TOOLS:
        return True
    if n in ("web_search", "set_reminder", "delegate"):
        return True
    if "." in n:
        return n.split(".", 1)[0].lower() in _CAP_NAMESPACES
    return n in _capability_ids()


# ============================================================
# 原生 function calling 的工具 schema
# ============================================================


def _json_type(spec):
    """把能力表里的简写类型（str? / int? / num?）转成 JSON Schema 类型。"""
    s = str(spec or "").strip().lower().rstrip("?")
    if s in ("int", "integer"):
        return "integer"
    if s in ("num", "number", "float"):
        return "number"
    if s in ("bool", "boolean"):
        return "boolean"
    if s in ("list", "array"):
        return "array"
    if s in ("dict", "object"):
        return "object"
    return "string"


TOOL_GROUPS = {
    "visual": ("render_check", "visual_check", "render_views", "scene_assert",
               "geometry_compare"),
    "image": ("see_image", "image_compare", "image_analyze"),
    "asset": ("asset_scan", "asset_convert", "asset_atlas",
              "asset_inline", "asset_validate", "mesh_inspect"),
    "audio": ("audio_probe", "audio_compare"),
    "workflow": ("run_workflow", "run_workflow_script", "workflow_status",
                 "workflow_resume", "workflow_cancel"),
    "utility": ("sleep", "set_reminder"),
    "diagram": ("archify",),
}

# 意图关键词：用户这轮消息里出现这些词，就整组挂载（宁可多挂，不要该用却看不见）


TOOL_GROUP_KEYS = {
    "asset": ("素材", "贴图", "图集", "纹理", "图片", "图标", "sprite", "atlas", "png", "jpg",
              "3d", "模型", "mesh", "glb", "gltf", "obj", "fbx", "建模", "场景"),
    "visual": ("页面", "网页", "html", "css", "canvas", "画布", "游戏", "渲染",
               "截图", "界面", "前端", "视觉", "布局", "ui",
               "视角", "多视图", "相机", "灯光", "场景", "3d", "模型", "几何"),
    "image": ("图片", "看图", "截图", "参考图", "对比", "尺寸", "色彩", "png", "jpg",
              "webp", "视觉", "图像"),
    "audio": ("音频", "音效", "bgm", "语音", "响度", "混音"),
    "diagram": ("架构图", "流程图", "时序图", "数据流", "状态图", "状态机", "ERD", "类图",
                "时间线", "瀑布图", "diagram", "archify", "画图", "可视化",
                "一页", "explainer", "架构", "结构图", "系统图", "模块图", "关系图",
                "示意图", "脑图"),
    "workflow": ("工作流", "workflow", "自动化流程"),
    "utility": ("等待", "睡眠", "sleep", "提醒", "闹钟", "定时"),
}

# tool_search 的目录由 tool_meta 按 action 生成，不再维护第二份参数速查表。


def hidden_tools(allow_groups=None, unlocked=None):
    """本轮不下发 schema 的工具名集合；allow_groups=None 表示全挂。"""
    if allow_groups is None:
        return set()
    hide = set()
    for group, names in TOOL_GROUPS.items():
        if group not in allow_groups:
            hide.update(names)
    for item in (unlocked or ()):
        if item in TOOL_GROUPS:
            hide.difference_update(TOOL_GROUPS[item])
        else:
            hide.discard(str(item))
    return hide


def tool_group_catalog(hidden=None):
    """一行一组的工具目录：告诉模型「还有哪些组可以用 tool_search 解锁」。"""
    lines = ["未挂载的工具组（原生 schema 里没有；需要时先调 "
             "tool_search(query=\"组名或关键词\") 解锁，下一轮生效）："]
    for group, names in TOOL_GROUPS.items():
        if hidden is not None and not (set(names) & set(hidden)):
            continue
        label, _shown = tool_meta.group_catalog(group, names)
        lines.append("- %s：%s" % (group, label))
    return "\n".join(lines)


READ_ONLY_SEGMENTS = {"node_modules", ".venv", "venv", "__pycache__"}


_REQUIRED_ARGS = {
    "list_dir": ("path",),
    "read_file": ("path",),
    "read_files": ("paths",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "old", "new"),
    "make_dir": ("path",),
    "move_file": ("from", "to"),
    "copy_file": ("from", "to"),
    "delete_file": ("path",),
    "fetch_url": ("url",),
    "sleep": ("seconds",),
}
