# -*- coding: utf-8 -*-

import json
import re
import time
from app import langs
from app import tool_meta
from app import tool_protocol
from app.mem_tool import _loads_loose

from .base import _t_split_commit
from .tools_computer import (_t_computer_act, _t_computer_open, _t_computer_read, _t_computer_see, _t_computer_shot, _t_computer_windows)
from .tools_exec import (_t_run_command, _t_run_workflow_script, _t_shell)
from .tools_files import (_t_attachment_read, _t_check_code, _t_code_context, _t_copy_file, _t_delete_file, _t_edit_file, _t_find_symbol, _t_impact, _t_list_dir, _t_list_steps, _t_make_dir, _t_move_file, _t_read_file, _t_read_files, _t_read_tool_result, _t_recover_file, _t_redo_step, _t_repo_map, _t_search_code, _t_search_files, _t_sleep, _t_undo_step, _t_write_file)
from .tools_git import (_t_git_commit, _t_git_diff, _t_git_log, _t_git_status)
from .tools_lsp import (_t_lsp_calls, _t_lsp_definition, _t_lsp_diagnostics, _t_lsp_hover, _t_lsp_implementation, _t_lsp_references, _t_lsp_rename, _t_lsp_symbols)
from .tools_mcp import (_t_mcp_list_servers, _t_mcp_list_tools)
from .tools_media import (_t_archify, _t_asset_atlas, _t_asset_convert, _t_asset_inline, _t_asset_scan, _t_asset_validate, _t_audio_compare, _t_audio_probe, _t_geometry_compare, _t_image_analyze, _t_image_compare, _t_mesh_inspect, _t_render_check, _t_render_views, _t_scene_assert, _t_see_image, _t_visual_check)
from .tools_web import _t_fetch_url


def normalize_tool_name(name):
    """把模型写的工具名收敛到注册表里的规范名；认不出就原样返回。"""
    from .groups import (COMPUTER_TOOLS, FILE_TOOLS, TOOL_ALIASES, _capability_ids)
    raw = str(name or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("mcp__"):
        # MCP 工具名由索引表精确匹配，不能被点号 / 下划线规则改写
        return raw
    if raw.lower() in ("mcp_list_servers", "mcp_list_tools"):
        return raw.lower()
    low = raw.lower().replace("-", "_").replace(" ", "_")
    if low in TOOL_ALIASES:
        return TOOL_ALIASES[low]
    if raw in FILE_TOOLS or raw in ("run_command", "render_check", "web_search",
                                    "audio_probe", "audio_compare", "see_image",
                                    "image_compare", "image_analyze",
                                    "read_tool_result",
                                    "find_symbol", "check_code",
                                    "set_reminder", "ask_user") \
            or raw in COMPUTER_TOOLS:
        return raw
    caps = _capability_ids()
    if "." in raw and not raw.startswith("."):
        if raw in caps:
            return raw
        if raw.lower() in caps:
            return raw.lower()
        return raw
    if raw in caps:
        return raw
    if low in caps:
        return low
    dotted = low.replace("_", ".")
    if dotted in caps:
        return dotted
    if low.endswith("_tool"):
        cand = low[:-5]
        if cand in caps or cand in FILE_TOOLS:
            return cand
    return raw


# 能力工具的命名空间（id 形如 calendar.create）。能力是懒注册的：
# 模块没被导入时 _capability_ids() 是空的，不能用它来判断「这个工具不存在」。


def normalize_args(tool, args):
    """兼容常见参数别名与字符串形式的 arguments（OpenAI 风格）。"""
    if isinstance(args, str):
        args = _loads_loose(args)
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, val in args.items():
        k = str(key)
        lk = k.lower()
        if lk in ("file", "filename", "file_name", "filepath", "file_path"):
            k = "path"
        elif lk in ("dir", "directory", "folder") and tool in ("list_dir", "make_dir"):
            k = "path"
        elif lk in ("q", "query", "keyword", "keywords"):
            if tool == "web_search":
                k = "queries"
            elif tool == "search_files":
                k = "pattern"
            else:
                k = lk          # memory.search 用 query，bili.up 用 keyword，保持原样
        elif lk in ("old_string", "old_text", "oldtext", "find"):
            k = "old"
        elif lk in ("new_string", "new_text", "newtext"):
            k = "new"
        elif lk in ("src", "source", "from_path"):
            k = "from"
        elif lk in ("dst", "dest", "destination", "to_path"):
            k = "to"
        elif lk in ("text", "body", "data") and tool == "write_file":
            k = "content"
        elif lk in ("start_line", "offset") and tool == "read_file":
            k = "start"
        out[k] = val

    if tool == "web_search":
        q = out.get("queries")
        if isinstance(q, str):
            out["queries"] = [q]
        elif isinstance(q, (list, tuple)):
            out["queries"] = [str(x) for x in q if str(x).strip()]
    if tool == "ask_user" and isinstance(out.get("questions"), dict):
        out["questions"] = [out["questions"]]
    if tool == "list_dir" and not out.get("path"):
        out["path"] = "."
    if tool == "set_reminder" and not out.get("text") and out.get("content"):
        out["text"] = out["content"]

    for k in ("start", "limit", "pages", "qty", "count", "remind_before"):
        if k in out:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                pass
    return out


def normalize_call(item, default_tool=""):
    """把一条调用统一成 {"tool": 规范名, "args": dict}。

    兼容：
    - {"tool":"read_file","args":{...}}（本项目）
    - {"name":"read_file","arguments":"{...}"}（OpenAI 风格）
    - {"function":{"name":"read_file","arguments":"{...}"}}
    """
    from .base import _first_arg
    if not isinstance(item, dict):
        return None
    tool = item.get("tool") or item.get("name") or default_tool
    args = _first_arg(item, ("args", "arguments", "parameters"))
    fn = item.get("function")
    if isinstance(fn, dict):
        tool = fn.get("name") or tool
        args = _first_arg(fn, ("args", "arguments", "parameters")) or args
    if not tool:
        return None
    args_obj = args if isinstance(args, dict) else (
        _loads_loose(args) if isinstance(args, str) else {})
    # 工具元层优先：read/edit/run/... 带 action 时先映射回旧工具名，
    # 这样后面的审批 / 沙箱 / 写前必读 / 撤销全部按旧名生效。
    _meta = tool_meta.normalize(str(tool), args_obj)
    if _meta:
        tool, args_obj = _meta
        # 这里的旧名已是规范名，不能再过 alias（例如 shell 会被别名成 run_command）
        return {"tool": tool, "args": normalize_args(tool, args_obj)}
    low_tool = str(tool).strip().lower()
    # 少量「同名但语义不同」的工具按参数消歧
    if low_tool == "search":
        if isinstance(args_obj, dict) and (
                "queries" in args_obj
                or ("query" in args_obj and "pattern" not in args_obj
                    and "glob" not in args_obj)):
            tool = "web_search"
        else:
            tool = "search_files"
    elif low_tool in ("delete", "remove", "rm"):
        has_path = isinstance(args_obj, dict) and any(
            k in args_obj for k in ("path", "file", "filename", "file_path"))
        if isinstance(args_obj, dict) and "id" in args_obj and not has_path:
            tool = "calendar.delete"
        else:
            tool = "delete_file"
    tool = normalize_tool_name(tool)
    return {"tool": tool, "args": normalize_args(tool, args)}


def parse_calls(block_text):
    """把控制块内容解析成 [{"tool": 名, "args": {...}}, ...]

    兼容：{"calls":[...]}、{"tool_calls":[...]}、单个 {"tool":...}、
    OpenAI 的 {"function":{...}}，以及一行一个对象的 NDJSON。
    解析后统一做工具名/参数别名收敛。
    """
    from .base import (MAX_CALLS_PER_ROUND, _ndjson_objects)
    raw = re.sub(r"^\s*(json|tool)\s*", "", (block_text or "").strip(),
                 count=1, flags=re.I).strip()
    if raw.startswith("<"):                     # 去掉残留的原生标记头
        raw = re.sub(r"^<[^>]*>", "", raw).strip()
    data = _loads_loose(raw)
    if isinstance(data, dict):
        if isinstance(data.get("calls"), list):
            data = data["calls"]
        elif isinstance(data.get("tool_calls"), list):
            data = data["tool_calls"]
        elif isinstance(data.get("function_call"), dict):
            data = [data["function_call"]]
        elif isinstance(data.get("function"), dict):
            data = [data]
        else:
            data = [data]
    elif data is None:
        data = _ndjson_objects(raw)             # NDJSON 兜底
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for item in data[:MAX_CALLS_PER_ROUND]:
        call = normalize_call(item)
        if not call:
            continue
        key = json.dumps(call, ensure_ascii=False, sort_keys=True)
        if key in seen:                         # 同一轮重复的同名同参调用只执行一次
            continue
        seen.add(key)
        out.append(call)
    return out


# ============================================================
# 1. 沙箱路径解析与安全校验
# ============================================================


_HANDLERS = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "read_files": _t_read_files,
    "search_files": _t_search_files,
    "search_code": _t_search_code,
    "repo_map": _t_repo_map,
    "code_context": _t_code_context,
    "impact": _t_impact,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "make_dir": _t_make_dir,
    "move_file": _t_move_file,
    "delete_file": _t_delete_file,
    "render_check": _t_render_check,
    "visual_check": _t_visual_check,
    "render_views": _t_render_views,
    "geometry_compare": _t_geometry_compare,
    "scene_assert": _t_scene_assert,
    "archify": _t_archify,
    "asset_scan": _t_asset_scan,
    "asset_convert": _t_asset_convert,
    "asset_atlas": _t_asset_atlas,
    "asset_inline": _t_asset_inline,
    "asset_validate": _t_asset_validate,
    "mesh_inspect": _t_mesh_inspect,
    "run_workflow_script": _t_run_workflow_script,
    "audio_probe": _t_audio_probe,
    "audio_compare": _t_audio_compare,
    "see_image": _t_see_image,
    "attachment_read": _t_attachment_read,
    "image_compare": _t_image_compare,
    "image_analyze": _t_image_analyze,
    "read_tool_result": _t_read_tool_result,
    "find_symbol": _t_find_symbol,
    "check_code": _t_check_code,
    "lsp_diagnostics": _t_lsp_diagnostics,
    "lsp_definition": _t_lsp_definition,
    "lsp_references": _t_lsp_references,
    "lsp_rename": _t_lsp_rename,
    "lsp_hover": _t_lsp_hover,
    "lsp_symbols": _t_lsp_symbols,
    "lsp_implementation": _t_lsp_implementation,
    "lsp_calls": _t_lsp_calls,
    "git_status": _t_git_status,
    "git_diff": _t_git_diff,
    "git_log": _t_git_log,
    "git_commit": _t_git_commit,
    "split_commit": _t_split_commit,
    "list_steps": _t_list_steps,
    "undo_step": _t_undo_step,
    "redo_step": _t_redo_step,
    "recover_file": _t_recover_file,
    "computer_open": _t_computer_open,
    "computer_see": _t_computer_see,
    "computer_act": _t_computer_act,
    "computer_read": _t_computer_read,
    "computer_shot": _t_computer_shot,
    "computer_windows": _t_computer_windows,
    "mcp_list_servers": _t_mcp_list_servers,
    "mcp_list_tools": _t_mcp_list_tools,
}


def _validate_args(name, args):
    """必填参数校验；run_command 允许 argv / command 二选一。"""
    from .groups import _REQUIRED_ARGS
    if name == "run_command":
        if not (args.get("argv") or args.get("command")):
            return tool_protocol.error(
                tool_protocol.INVALID_ARG, "缺少必填参数：argv（或 command）",
                name=name, args=args)
        return ""
    code, msg = tool_protocol.validate(name, args, _REQUIRED_ARGS.get(name))
    if not code:
        return ""
    return tool_protocol.error(code, msg, name=name, args=args)


def run_tool(sandbox, name, args, allow_outside=False, caps=None, stop_event=None,
             unrestricted=False):
    """执行一个工具，并经过生命周期钩子。

    PreToolUse 可以 block（理由回灌给模型）；PostToolUse 只观察与注入上下文，
    不改执行结果。钩子本身出问题一律吞掉 —— 审计挂了不能拖垮工具。
    """
    from .groups import (GIT_WRITE_TOOLS, writes_files)
    from .io_utils import (MAX_RESULT_CHARS, WRITE_LOCK)
    name = (name or "").strip()
    args = args if isinstance(args, dict) else {}
    # 防御性归一：直接调用 meta 工具（脚本 / 工作流 / 外部集成）也能落到旧实现上。
    _meta = tool_meta.normalize(name, args)
    if _meta:
        name, args = _meta
    sid = str((caps or {}).get("sid") or "")
    safe_args = args if isinstance(args, dict) else {}
    try:
        from app import hooks
        pre = hooks.before_tool(name, safe_args, sandbox, sid)
    except Exception:                        # noqa: BLE001
        pre = {}
    if pre.get("decision") == "block":
        return tool_protocol.error(
            tool_protocol.PERMISSION,
            "被 hook 阻止：%s" % (pre.get("reason") or "未说明原因"),
            name=name, args=safe_args), None, False
    # 写类工具串行化：见 WRITE_LOCK 的说明（并发子代理丢改动）
    if writes_files(name, args) or name in GIT_WRITE_TOOLS:
        with WRITE_LOCK:
            text, undo, is_write = _run_tool_impl(
                sandbox, name, args, allow_outside=allow_outside, caps=caps,
                stop_event=stop_event, unrestricted=unrestricted)
    else:
        text, undo, is_write = _run_tool_impl(
            sandbox, name, args, allow_outside=allow_outside, caps=caps,
            stop_event=stop_event, unrestricted=unrestricted)
    _ctx = list(pre.get("contexts") or [])
    try:
        from app import hooks
        post = hooks.after_tool(name, safe_args,
                                not str(text).lstrip().startswith("❌"),
                                text, sandbox, sid)
        _ctx += list(post.get("contexts") or [])
    except Exception:                        # noqa: BLE001
        pass
    if _ctx:
        # hook 想追加的内容跟着工具结果一起回灌，模型下一轮就能看到
        text = str(text) + "\n\n【hook】" + "\n".join(_ctx)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n…（结果过长已截断）"
    return text, undo, is_write


def _run_tool_impl(sandbox, name, args, allow_outside=False, caps=None, stop_event=None,
                   unrestricted=False):
    """（内部）执行一个工具。返回 (结果文本, 撤销操作列表或 None, 是否写操作)

    allow_outside=True 时，读取类工具（list_dir / read_file）可用绝对路径读取沙箱外文件。
    caps 是 limits() 的结果：给 run_command 补默认超时，require_read 决定是否强制写前读。
    stop_event 只影响 run_command：用户点停止时立刻结束正在跑的命令。
    """
    from .base import (_limit_or_default, post_write_check)
    from .groups import (GIT_WRITE_TOOLS, LSP_TOOLS, OUTSIDE_READ_TOOLS, TOOLS, writes_files)
    from .io_utils import MAX_RESULT_CHARS
    from .read_tracking import (_READ_BEFORE_WRITE, _check_read_before_write, _note_seen, _remember_read, _repeat_read_stub)
    from .sandbox import (SandboxError, clean_sandbox, invalidate_tree, resolve_path)
    from .tools_exec import (CMD_DEFAULT_TIMEOUT, SHELL_DEFAULT, _t_run_command)
    from .tools_files import _t_tool_search
    from .tools_media import _append_blank_image_warning
    name = (name or "").strip()
    if name.startswith("mcp__"):
        # 外部工具：参数校验交给 server 的 inputSchema，这里只做超时与错误包装
        from app import mcp
        _margs = args if isinstance(args, dict) else {}
        _mtext = mcp.call(name, _margs, timeout=(caps or {}).get("mcp_timeout"))
        return tool_protocol.wrap(_mtext, name=name, args=_margs), None, False
    if name == "tool_search":
        # 元工具：不解锁沙箱，只登记「下一轮要挂哪些工具组」
        return _t_tool_search(args, caps), None, False
    if name in ("update_plan", "plan_patch"):
        # 计划状态属于对话会话，不在工具层：由调用方通过 caps 注入回调。
        # 主循环、工作流节点、脚本共用同一个回调，避免出现两套计划状态。
        fn = (caps or {}).get("plan_tool")
        if not callable(fn):
            return ("❌ %s 需要对话会话（脚本 / 独立工具调用里没有计划上下文）。"
                    % name, None, False)
        try:
            _pok, _ptext = fn(name, args if isinstance(args, dict) else {})
        except Exception as e:            # noqa: BLE001
            return "❌ %s 执行失败：%s" % (name, e), None, False
        return str(_ptext), None, False
    if name == "acceptance":
        fn = (caps or {}).get("acceptance_tool")
        if not callable(fn):
            return ("❌ acceptance 需要对话会话（脚本 / 独立工具调用里没有验收上下文）。",
                    None, False)
        try:
            _aok, _atext = fn(args if isinstance(args, dict) else {})
        except Exception as e:            # noqa: BLE001
            return "❌ acceptance 执行失败：%s" % e, None, False
        return str(_atext), None, False
    if name in LSP_TOOLS:
        _lp = str((args or {}).get("path") or "").strip()
        if _lp and not langs.lsp_kind(_lp):
            return langs.lsp_fallback(_lp), None, False
    if name not in _HANDLERS:
        return tool_protocol.wrap(
            "未知工具：%s（可用：%s）" % (name, "、".join(TOOLS)), name=name), None, False
    sandbox = clean_sandbox(sandbox)      # 统一清洗，保证各工具算相对路径时用的是干净根目录
    args = args if isinstance(args, dict) else {}
    bad = _validate_args(name, args)
    if bad:
        return bad, None, False
    if (caps or {}).get("require_read", True):
        blocked = _check_read_before_write(sandbox, name, args)
        if blocked:
            return blocked, None, False
    # archify（流程图 / 架构图）只在 Agent 模式可用：非 Agent 没有沙箱，也不该生成
    # 可交互产物。只有调用方显式传了 agent_on=False 才拦（脚本 / 子代理不传，保持兼容）。
    if name == "archify" and (caps or {}).get("agent_on") is False:
        return tool_protocol.error(
            tool_protocol.PERMISSION,
            "archify（流程图 / 架构图 / 时序图）只在 Agent 模式下可用。"
            "当前会话不是 Agent：请改用内联 svg / mermaid / chart，"
            "或告诉用户「开启 Agent 模式后可以生成可交互页面」。",
            name=name, args=args), None, False
    # 同一回合内重复读同一个没变的文件：只回一行，不再把全文重发一遍。
    # 判断在真正读盘之前，所以连 IO 都省了。
    if name == "read_file":
        _stub = _repeat_read_stub(sandbox, args, caps, allow_outside)
        if _stub:
            return _stub, None, False
        if (caps or {}).get("read_outline"):
            # 大文件先给符号大纲（默认关闭，由 read_outline_mode 控制）
            args = dict(args, _outline=1,
                        _outline_min=int((caps or {}).get("read_outline_min") or 600))
    if name == "run_command" and not args.get("timeout"):
        _raw = (caps or {}).get("cmd_timeout")
        if _raw is not None:
            # 0 = 不限：传 -1 明确告诉工具「别自动掐断」，而不是回落成默认的 60 秒
            _ct = _limit_or_default(_raw, CMD_DEFAULT_TIMEOUT)
            args = dict(args, timeout=(-1 if _ct <= 0 else _ct))
    try:
        if name == "run_command":
            _cmd_t0 = time.time()
            _fn = _HANDLERS.get(name)
            if _fn is _t_run_command:
                text, undo_ops = _t_run_command(
                    sandbox, args, stop_event,
                    (caps or {}).get("shell_mode") or SHELL_DEFAULT,
                    unrestricted)
            else:
                # 测试 / 插件替换过实现：按原签名调用，不要把 shell_mode 塞进去
                text, undo_ops = _fn(sandbox, args)
            text = _append_blank_image_warning(sandbox, _cmd_t0, text)
        elif name == "shell":
            text, undo_ops = _HANDLERS[name](sandbox, args, caps, stop_event)
        elif name in ("fetch_url", "sleep"):
            text, undo_ops = _HANDLERS[name](sandbox, args, stop_event)
        elif name in ("list_steps", "undo_step", "redo_step", "recover_file",
                      "visual_check", "render_views", "scene_assert",
                      "attachment_read", "run_workflow_script", "archify"):
            # 这几个要拿会话 id / caps 才能定位写后快照、视觉产物、对话附件或工作流执行器
            text, undo_ops = _HANDLERS[name](sandbox, args, caps)
        elif allow_outside and name in OUTSIDE_READ_TOOLS:
            text, undo_ops = _HANDLERS[name](sandbox, args, True)
        else:
            text, undo_ops = _HANDLERS[name](sandbox, args)
    except SandboxError as e:
        return tool_protocol.error(tool_protocol.SANDBOX_DENIED, str(e),
                                   name=name, args=args), None, False
    except PermissionError as e:
        return tool_protocol.error(tool_protocol.PERMISSION, str(e),
                                   name=name, args=args), None, False
    except FileNotFoundError as e:
        return tool_protocol.error(tool_protocol.NOT_FOUND, str(e),
                                   name=name, args=args), None, False
    except TimeoutError as e:
        return tool_protocol.error(tool_protocol.TIMEOUT, str(e),
                                   name=name, args=args), None, False
    except ValueError as e:
        return tool_protocol.error(tool_protocol.INVALID_ARG, str(e),
                                   name=name, args=args), None, False
    except Exception as e:
        return tool_protocol.error(tool_protocol.TOOL_ERROR, "工具执行出错：%s" % e,
                                   name=name, args=args), None, False
    # 失败结果补 code / 恢复 / 可重发参数；成功结果原样返回，不占额外 token
    text = tool_protocol.wrap(text, name=name, args=args)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…（结果过长已截断）"
    if writes_files(name, args):
        invalidate_tree(sandbox)             # 结构可能变了，下次读树重新遍历
    # 写后自检：语法错误直接回灌给模型（成功时零额外 token）
    target = ""
    if undo_ops and name in ("write_file", "edit_file", "move_file"):
        rel = args.get("to") if name == "move_file" else args.get("path")
        if rel:
            try:
                target = resolve_path(sandbox, rel)
            except SandboxError:
                target = ""
        err = post_write_check(target)
        if err:
            text += "\n\n【自检】文件存在语法错误，请修正后重试：\n" + err
        elif target and name in ("write_file", "edit_file"):
            # 语法没问题才格式化：格式化器可能把坏文件改得更坏
            try:
                from app import formatter
                changed, ferr = formatter.format_file(
                    target, (caps or {}).get("formatter") or "off")
            except Exception:                    # noqa: BLE001  格式化绝不能拖垮写入
                changed, ferr = False, ""
            if changed:
                err2 = post_write_check(target)
                text += "\n\n（已按项目风格自动格式化）"
                if err2:
                    text += "\n【自检】格式化后出现语法错误，请重试：\n" + err2
            elif ferr:
                text += "\n\n【格式化】" + ferr
    if target and name in _READ_BEFORE_WRITE:
        # 自己刚写完的版本记下来：同一回合继续 edit 不必再读一遍
        _remember_read(sandbox, target)
    elif name == "read_file" and not tool_protocol.is_error(text):
        try:
            _rp = resolve_path(sandbox, args.get("path"), allow_outside)
            _remember_read(sandbox, _rp)
            # 只有「整篇正文给过」才算 seen；分段读和大纲都不算
            if (not (args.get("start") or args.get("limit") or args.get("raw"))
                    and not str(text).startswith("文件大纲：")):
                _note_seen(sandbox, _rp, caps)
        except SandboxError:
            pass
    elif name == "read_files" and not tool_protocol.is_error(text):
        # 批量读也要记「读过了」，否则随后 edit_file 会被写前读检查拦下
        _paths = args.get("paths")
        if isinstance(_paths, str):
            _paths = [_paths]
        for _p in list(_paths or [])[:8]:
            try:
                _remember_read(sandbox, resolve_path(sandbox, _p, allow_outside))
            except SandboxError:
                pass
    return text, undo_ops, (writes_files(name, args) or name in GIT_WRITE_TOOLS)


# ============================================================
# 3. 撤销
# ============================================================


def limits(cfg=None):
    """把设置里的 Agent 上限收敛成一份运行时配置。

    缺键、类型不对回落默认常量；0 = 不限；比最小值还小的正数抬到最小值。
    每次请求开始算一次，循环里用局部变量——中途改设置不会让同一次请求前后不一致。
    """
    from .base import (LIMIT_MINIMUMS, MAX_ROUNDS, REPEAT_LIMIT, _limit_or_default)
    from .tools_exec import (CMD_DEFAULT_TIMEOUT, normalize_shell_mode)
    cfg = cfg if isinstance(cfg, dict) else {}
    pairs = (
        ("max_rounds", "agent_max_rounds", MAX_ROUNDS),
        ("repeat_limit", "agent_repeat_limit", REPEAT_LIMIT),
        ("cmd_timeout", "agent_cmd_timeout", CMD_DEFAULT_TIMEOUT),
    )
    out = {}
    for key, cfg_key, default in pairs:
        val = _limit_or_default(cfg.get(cfg_key), default)
        if val < 0:
            val = default              # 负数没有意义，别悄悄当成「不限」
        elif val > 0:
            val = max(LIMIT_MINIMUMS[key], val)
        out[key] = val
    # Shell 自由度与写后格式化是字符串档位，不做数值钳制
    out["shell_mode"] = normalize_shell_mode(cfg.get("agent_shell_mode"))
    out["formatter"] = str(cfg.get("agent_formatter") or "off").strip().lower()
    # Code Mode 预算：0 = 不限调用次数；并发至少 1
    for key, cfg_key, default in (("code_max_calls", "code_mode_max_calls", 2000),
                                  ("code_parallel", "code_mode_parallel", 16)):
        val = _limit_or_default(cfg.get(cfg_key), default)
        out[key] = 0 if val == 0 else max(1, val)
    # Code Mode 写入/命令是布尔开关：脚本内部不再逐次弹审批
    out["code_allow_write"] = cfg.get("code_mode_allow_write") is True
    out["code_allow_cmd"] = cfg.get("code_mode_allow_cmd") is True
    # 写前读：覆盖已有文件前必须先在会话里读过（dsh 的 fs-policy 默认行为）
    out["require_read"] = cfg.get("agent_require_read") is not False
    # 同回合重复读去重：文件没变就只回一行，不再重发全文
    out["read_dedupe"] = cfg.get("agent_read_dedupe") is not False
    # 大文件默认给符号大纲（默认关闭：会改变 read_file 的返回形态，需先跑 bench）
    out["read_outline"] = cfg.get("read_outline_mode") is True
    try:
        out["read_outline_min"] = max(0, int(cfg.get("read_outline_min_lines") or 600))
    except (TypeError, ValueError):
        out["read_outline_min"] = 600
    return out


def needs_sandbox(name):
    """这个工具是否依赖沙箱目录（Agent 未开启时就不能用）。

    MCP 工具跑在外部进程里、Computer 有独立开关，都不受「Agent + 沙箱」限制。
    """
    from .groups import COMPUTER_TOOLS
    n = str(name or "").strip()
    if n.startswith("mcp__") or n in ("mcp_list_servers", "mcp_list_tools"):
        return False
    # 这几个不碰沙箱：抓网页 / 等待 / 画图（archify 落 data/archify/<sid>/）、
    # 工具组的元工具（非 Agent 模式也要能用 tool_search 解锁），没开 Agent 也能用
    if n in ("fetch_url", "sleep", "archify", "tool_search"):
        return False
    return n not in COMPUTER_TOOLS


_HANDLERS["run_command"] = _t_run_command


_HANDLERS["copy_file"] = _t_copy_file


_HANDLERS["fetch_url"] = _t_fetch_url


_HANDLERS["sleep"] = _t_sleep


_HANDLERS["shell"] = _t_shell
