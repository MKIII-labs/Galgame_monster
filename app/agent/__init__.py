# -*- coding: utf-8 -*-
"""包入口：保持 ``from app.agent import X`` 的旧路径不变。"""

from .approval import (
    APPROVAL_ALIASES, APPROVAL_DEFAULT, APPROVAL_MODES, APPROVAL_TIMEOUT, SECRET_NAMES,
    SECRET_SEGMENTS, SECRET_SUFFIXES, _APPROVALS, _APPROVE_REMEMBER, _approvals_lock,
    _match_command_rules, _match_rules, _match_tool_rules, _secret_allowed, _secret_deny_text,
    _tool_arg_brief, approval_remember, describe_call, get_approval, is_secret_path,
    judge_command, judge_shell, judge_tool_rule, judge_write, new_approval, normalize_approval,
    remember_approval_rule, requires_approval, resolve_approval, wait_approval,
)
from .base import (
    BLANK_RANGE_MIN, BLANK_SCAN_MAX, BLANK_STD_MIN, BUILD_LOCK, CHECK_MAX_FILES,
    CODE_CONTEXT_CHARS, CODE_CONTEXT_REFS, CODE_IDENT_RE, FENCE, FETCH_URL_CHARS,
    FETCH_URL_MAX_CHARS, LIMIT_MINIMUMS, LOOP_EXEMPT, LOOP_WINDOW, LOOP_WINDOW_LIMIT,
    LOOP_WINDOW_LIMIT_CMD, MAX_CALLS_PER_ROUND, MAX_CHECK_BYTES, MAX_LIST_ENTRIES,
    MAX_READ_FILES, MAX_READ_FILE_BYTES, MAX_ROUNDS, MAX_SEARCH_CONTEXT, MAX_SEARCH_FILES,
    MAX_SEARCH_HITS, MAX_SEARCH_PER_FILE, MAX_TURNS, REPEAT_LIMIT, SLEEP_MAX, SYMBOL_EXTS,
    SYMBOL_MAX_BYTES, SYMBOL_MAX_FILES, SYMBOL_PER_FILE_REFS, SYMBOL_REF_LIMIT, ToolExtractor,
    VERIFY_MAX_TESTS, VERIFY_TIMEOUT, VISION_NATIVE_MAX, _CMD_NOISE_PREFIXES, _FAIL_KEYS,
    _PATH_EXT, _PATH_RE, _PICK_LOCK, _PLAN_REVIEW_ACTIONS, _SHELL_LOCK, _SHELL_OFF,
    _TREE_CACHE, _TREE_LOCK, _first_arg, _glob_to_regex, _limit_or_default, _ndjson_objects,
    _normalize_options, _symbol_def_re, _t_split_commit, failure_signature, failure_streak,
    intent_hint_prompt, normalize_auto_continue, post_write_check, rename_entry, repeat_guard,
    reset_loop_window, result_ok, window_hits,
)
from .dispatch import (
    _HANDLERS, _run_tool_impl, _validate_args, limits, needs_sandbox, normalize_args,
    normalize_call, normalize_tool_name, parse_calls, run_tool,
)
from .groups import (
    BLOCKED_SEGMENTS, COMPUTER_TOOLS, FILE_TOOLS, GIT_WRITE_TOOLS, LSP_TOOLS,
    OUTSIDE_READ_TOOLS, PLAN_MODE_TOOLS, READ_ONLY_SEGMENTS, RUN_TOOLS, SEE_MAX_IMAGES,
    SEE_MAX_TOTAL, TOOLS, TOOL_ALIASES, TOOL_GROUPS, TOOL_GROUP_KEYS, WRITE_TOOLS,
    _CAP_NAMESPACES, _PLUGIN_TOOLS, _PLUGIN_TOOLS_LOCK, _REQUIRED_ARGS, _capability_ids,
    _json_type, hidden_tools, is_known_tool, tool_group_catalog, writes_files,
)
from .io_utils import (
    IMAGE_MIME, IMG_EXTS, IMG_MAX_BYTES, IMG_MIME, MAX_IMAGE_BYTES, MAX_READ_BYTES,
    MAX_RESULT_CHARS, WRITE_LOCK, _fmt_size, _looks_binary, _preview_display, _preview_paths,
    _read_doc_for_agent, _read_text_file, _write_enc, image_mime, read_bytes, read_text,
    write_text,
)
from .plan import (
    ASK_TIMEOUT, MAX_ASKS, PLAN_LOG_MAX, PLAN_MAX_ACCEPT, PLAN_MAX_MILESTONES, PLAN_MAX_RETRO,
    PLAN_MAX_STEPS, PLAN_MAX_TEXT, PLAN_MAX_VERIFY, PLAN_REVIEW_TIMEOUT, PLAN_STATUSES, _ASKS,
    _PLAN_REVIEWS, _PLAN_REVIEWS_LOCK, _asks_lock, _plan_accept, _plan_deps, _plan_done_when,
    _plan_log, _plan_status, _plan_token, _plan_txt, accept, accept_tokens, apply_plan_patch,
    ask_meta, dismiss_ask, get_plan_review, new_asks, new_plan_review, normalize_plan,
    pick_directory, plan_brief, resolve_plan_review, submit_answers, wait_answers,
    wait_plan_review,
)
from .prompt import (
    _ASK_PROMPT, _CAPABILITY_BOUNDARY, _COMPUTER_PROMPT, _FINISH_PROMPT, _META_ALIAS_PROMPT,
    _NO_SEARCH_PROMPT, _PICKER_CODE, _PICK_MARK, _PLAN_MODE_PROMPT, _PLAN_PROMPT,
    _REMINDER_PROMPT, _RESEARCH_PROMPT, _RESEARCH_PROMPT_SHORT, _SEARCH_PROMPT,
    _VISUAL_BITMAP_NO, _VISUAL_BITMAP_OK, _VISUAL_DIAGRAM_OFF, _VISUAL_DIAGRAM_READY,
    _VISUAL_HEAD, _VISUAL_TAIL, _bili_hint, _calendar_hint, _capability_prompt, _file_hint,
    _full_agent_prompt, _line_cid, _memory_hint, _proto_block, _reminder_hint, _stock_hint,
    _tool_decision_prompt, _visual_prompt, _web_hint, build_agent_prompt, intent_hint,
    proto_block,
)
from .read_tracking import (
    _READ_BEFORE_WRITE, _READ_SEEN, _READ_SEEN_MAX, _READ_STATE, _READ_STATE_MAX,
    _check_read_before_write, _file_ver, _note_seen, _read_lock, _remember_read,
    _repeat_read_stub, _seen_key, _turn_key, turn_begin, turn_note,
)
from .rules import (
    RULES_FILES, RULES_MAX_CHARS, RULES_REL, _RULES_CACHE, _RULES_CACHE_MAX, _read_rules_file,
    _read_rules_text_cached, global_rules_path, load_effective_rules, load_global_rules,
    load_rules, project_rules, rules_path,
)
from .sandbox import (
    SandboxError, TREE_TTL, _assert_not_blocked, _build_tree, _extract_dir, _extract_path,
    _extract_paths, _extract_pattern, _looks_like_path, _norm_rel, _real, _recover_rel, _rel,
    _sandbox_root, _tree, _walk, _write_rel_paths, clean_cwd, clean_sandbox, delete_entry,
    ensure_entry, invalidate_tree, resolve_path, sandbox_warnings, tree,
)
from .schemas import _fn_tool, _tool_search_schema, agent_tool, native_tool_schemas
from .state import _LOADED_SIDS, _TURNS, _ensure_loaded, _turns_lock
from .tools_computer import (
    _computer_text, _t_computer_act, _t_computer_open, _t_computer_read, _t_computer_see,
    _t_computer_shot, _t_computer_windows, judge_computer,
)
from .tools_exec import (
    CMD_ALLOW_EXES, CMD_ALLOW_MODULES, CMD_DEFAULT_TIMEOUT, CMD_DENY_EXES, CMD_ENV_DROP,
    CMD_GIT_MEDIUM, CMD_GIT_READONLY, CMD_INLINE_FLAGS, CMD_INLINE_PREFIX, CMD_MAX_OUTPUT,
    CMD_MAX_TIMEOUT, CMD_META_CHARS, CMD_NPM_HIGH, CMD_SCRIPT_EXTS, CMD_VERSION_FLAGS,
    SHELL_DEFAULT, SHELL_MODES, SHELL_TOOLS, SHELL_WAIT_DEFAULT, SHELL_WAIT_MAX, _clean_output,
    _cmd_argv, _cmd_env, _cmd_inline_error, _cmd_kind, _cmd_plan, _cmd_plan_unrestricted,
    _cmd_script_error, _cmd_secret_hit, _exe_name, _kill_tree, _pipe_plan, _resolve_argv,
    _script_stamp, _shell_idle, _shell_plan, _strip_cmd_noise, _t_run_command,
    _t_run_workflow_script, _t_shell, call_signature, normalize_shell_mode,
)
from .tools_files import (
    _recover_candidates, _t_attachment_read, _t_check_code, _t_code_context, _t_copy_file,
    _t_delete_file, _t_edit_file, _t_find_symbol, _t_impact, _t_list_dir, _t_list_steps,
    _t_make_dir, _t_move_file, _t_read_file, _t_read_files, _t_read_tool_result,
    _t_recover_file, _t_redo_step, _t_repo_map, _t_search_code, _t_search_files, _t_sleep,
    _t_tool_search, _t_undo_step, _t_write_file, missing_written_files,
)
from .tools_git import (
    _commit_targets, _dangerous_git, _t_git_commit, _t_git_diff, _t_git_log, _t_git_status,
    judge_git_commit, verify_before_commit,
)
from .tools_lsp import (
    _lsp_line, _t_lsp_calls, _t_lsp_definition, _t_lsp_diagnostics, _t_lsp_hover,
    _t_lsp_implementation, _t_lsp_references, _t_lsp_rename, _t_lsp_symbols,
)
from .tools_mcp import _t_mcp_list_servers, _t_mcp_list_tools
from .tools_media import (
    _append_blank_image_warning, _b64_for_vision, _read_vision_images, _t_archify,
    _t_asset_atlas, _t_asset_convert, _t_asset_inline, _t_asset_scan, _t_asset_validate,
    _t_audio_compare, _t_audio_probe, _t_geometry_compare, _t_image_analyze, _t_image_compare,
    _t_mesh_inspect, _t_render_check, _t_render_views, _t_scene_assert, _t_see_image,
    _t_visual_check, vision_attachments,
)
from .tools_web import (
    _ABS_TIME_RE, _BV_RE, _URL_RE, _extract_abs_time, _extract_url, _t_fetch_url,
    _url_public_error,
)
from .undo import (
    MAX_UNDO, SNAP_AFTER_MAX, _UNDO, _WRITE_LABEL, _capture_after, _persist_undo, _snap,
    _trim_undo, _undo_lock, ensure_undo_loaded, list_steps, preview_undo, redo, register_undo,
    turn_undo, undo, undo_limit, undo_old_content, undo_tokens,
)

# 兼容旧的模块级命名空间（外部/测试会直接读 app.agent.xxx）
import base64
import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from app import docparse
from app import computer
from app import webprobe
from app import langs
from app import tool_meta
from app import visual_qa
from app import assets
from app import audio
from app import mesh
from app import winproc
from app import tool_protocol
from app.config import light_chat_is_local, load_config
from app.mem_tool import DSML_CLOSE, DSML_OPEN, MemoryExtractor, _loads_loose
from app.store import DATA_DIR
