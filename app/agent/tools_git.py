# -*- coding: utf-8 -*-

import re
import subprocess
import sys
from app import winproc
from app.config import load_config



def _t_git_status(root, args):
    from app import git as gitlib
    try:
        st = gitlib.status(root)
    except gitlib.GitError as e:
        return "❌ %s" % e, None
    if not st.get("ok"):
        return "❌ %s" % st.get("reason"), None
    lines = ["分支：%s" % st.get("branch")]
    if st.get("ahead") or st.get("behind"):
        lines.append("与上游：领先 %d、落后 %d" % (st.get("ahead") or 0, st.get("behind") or 0))
    files = st.get("files") or []
    if not files:
        lines.append("工作区干净，没有改动。")
    else:
        lines.append("改动 %d 个文件：" % len(files))
        for f in files[:80]:
            tag = "未跟踪" if f.get("untracked") else (
                "已暂存" if f.get("staged") and not f.get("unstaged") else "未暂存")
            lines.append("  [%s] %s" % (tag, f.get("path")))
    return "\n".join(lines), None


def _t_git_diff(root, args):
    from app import git as gitlib
    rel = str(args.get("path") or "").strip()
    if not rel:
        return "请提供 path：要看哪个文件（沙箱内相对路径）", None
    try:
        d = gitlib.diff_file(root, rel, staged=bool(args.get("staged")))
    except gitlib.GitError as e:
        return "❌ %s" % e, None
    if d.get("binary"):
        return "%s：二进制文件，不做文本 diff" % rel, None
    if not d.get("hunks"):
        return "%s：没有可见改动（可能没改，或只在空白上不同）" % rel, None
    out = ["%s  +%d -%d" % (rel, d.get("adds") or 0, d.get("dels") or 0)]
    shown = 0
    for h in d["hunks"]:
        if shown >= 400:
            out.append("…（diff 过长，已截断）")
            break
        out.append("@@ -%d +%d @@（前 %d 行已折叠）"
                   % (h.get("old_start") or 1, h.get("new_start") or 1,
                      h.get("gap_before") or 0))
        for i, line in enumerate(h.get("old") or []):
            out.append(("-%s" if (h.get("old_kinds") or [])[i] == "del" else " %s") % line)
            shown += 1
        for i, line in enumerate(h.get("new") or []):
            out.append(("+%s" if (h.get("new_kinds") or [])[i] == "ins" else " %s") % line)
            shown += 1
    return "\n".join(out), None


def _t_git_log(root, args):
    from app import git as gitlib
    r = gitlib.log(root, args.get("limit"))
    if not r.get("ok"):
        return "❌ %s" % r.get("error"), None
    if not r["items"]:
        return "还没有任何提交。", None
    return "\n".join("%s  %s  %s  %s" % (x["hash"], x["date"], x["author"], x["subject"])
                     for x in r["items"]), None


# ---- 提交前验证：只跑「受影响」的测试，不跑全量 ----


def _commit_targets(gitlib, root, paths, add_all):
    """这次提交会带上哪些文件（用于反查受影响测试）。"""
    if paths:
        return list(paths)
    try:
        st = gitlib.status(root)
    except Exception:                            # noqa: BLE001
        return []
    files = [f for f in (st.get("files") or []) if f.get("path")]
    if add_all:
        return [f["path"] for f in files]
    return [f["path"] for f in files if f.get("staged")]


def verify_before_commit(root, rels):
    """用符号图找相关测试并只跑它们。返回 (ok, 说明)。

    这是「提交工作流」的核心：对方只做 undo/redo，不保证提交质量；
    这里把「改了什么 → 该验什么」交给代码，而不是让模型自己声明测过了。
    """
    from .base import (VERIFY_MAX_TESTS, VERIFY_TIMEOUT)
    from .tools_exec import (_clean_output, _cmd_env)
    from app import codesym
    tests = []
    for rel in rels or []:
        for t in codesym.tests_for(root, rel):
            if t not in tests:
                tests.append(t)
        if len(tests) >= VERIFY_MAX_TESTS:
            break
    tests = tests[:VERIFY_MAX_TESTS]
    if not tests:
        return True, "没有找到直接相关的测试（跳过验证）"
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header"] + tests,
            cwd=root, capture_output=True, timeout=VERIFY_TIMEOUT,
            env=_cmd_env(), creationflags=winproc.no_window())
    except subprocess.TimeoutExpired:
        return False, "验证超时（%ds）：%s" % (VERIFY_TIMEOUT, " ".join(tests))
    except OSError as e:
        return True, "测试无法启动，跳过验证（%s）" % e
    if p.returncode == 0:
        return True, "已通过：" + "、".join(tests)
    out = _clean_output(p.stdout or b"") or _clean_output(p.stderr or b"")
    tail = "\n".join(out.splitlines()[-25:])
    return False, "测试失败（%s）：\n%s" % (" ".join(tests), tail)


def _t_git_commit(root, args):
    from app import git as gitlib
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = None
    message = str(args.get("message") or "").strip()
    if not message:
        return "缺少 message：提交信息要写「为什么改」，不能为空", None
    add_all = bool(args.get("all"))
    rels = _commit_targets(gitlib, root, paths, add_all)
    verify_mode = str(load_config().get("git_verify_before_commit") or "auto").strip().lower()
    verify_note = ""
    if verify_mode != "off" and rels:
        ok, verify_note = verify_before_commit(root, rels)
        if not ok:
            return ("❌ 提交被拦下（提交前验证没通过）。\n%s\n\n"
                    "先修好再提交；确实要跳过，可在设置里把「提交前验证」改为关闭。"
                    % verify_note), None
        if verify_note.startswith("已通过"):
            message += "\n\nVerified-by: " + verify_note.replace("已通过：", "")
    try:
        r = gitlib.commit(root, message, rels=paths, add_all=add_all)
    except gitlib.GitError as e:
        return "❌ %s" % e, None
    head = "已提交 %s\n%s" % (r.get("hash") or "", r.get("summary") or "")
    if verify_note:
        head += "\n验证：" + verify_note
    return head, None


def judge_git_commit(mode, unrestricted=False):
    """git_commit 的三档策略：off = 不给用，ask = 每次问，auto = 不问。

    与写入审批分开：提交不改文件内容，改的是 Git 历史，不能混进 _UNDO。
    unrestricted（完全放权）时直接放行，不再看设置里的策略。
    """
    if unrestricted:
        return "allow", ""
    m = str(mode or "off").strip().lower()
    if m == "off":
        return "deny", "设置里关闭了「允许 AI 提交」"
    if m == "auto":
        return "allow", ""
    return "ask", "提交会写入 Git 历史（设置里可改为自动或关闭）"


# ---------------------------------------------------------------- 命令执行
# 定位：给模型一个「能跑起来看结果」的入口，**不是安全沙箱**。
# Windows 上没有 Seatbelt / Landlock 这类 OS 级隔离，这里只做：
#   可执行白名单 + 禁止串联/重定向 + cwd 锁沙箱 + 环境变量清洗 + 四档审批。


def _dangerous_git(plan):
    """完全放权时仍需确认的高危 git 命令（覆盖工作区 / 改写历史）。

    典型事故：`git checkout HEAD -- .` 会把整个工作区恢复成 HEAD，
    用户清空后的状态被一次性覆盖。这里不依赖审批档位。
    """
    from .tools_exec import (_cmd_kind, _exe_name)
    if not isinstance(plan, dict):
        return False
    argvs = []
    if plan.get("argv"):
        argvs.append([str(x) for x in plan["argv"]])
    cmd = str(plan.get("cmd") or "")
    if cmd:
        for seg in re.split(r"(?:&&|\|\||;|\|)", cmd):
            toks = seg.strip().split()
            if toks:
                argvs.append(toks)
    for argv in argvs:
        if argv and _exe_name(argv[0]) == "git" and _cmd_kind(argv) == "high":
            return True
    return False
