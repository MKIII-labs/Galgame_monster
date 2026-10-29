# -*- coding: utf-8 -*-
"""IDE 的 Git 面板接口。

与 `/api/sandbox/*` 一样以「会话沙箱」为根；所有路径都是沙箱内相对路径。
读接口不产生副作用；写接口（stage / unstage / commit）只在用户点击时触发。
"""
from app import git
from app.registry import route
import os


def _guard(h):
    body = h._body()
    _s, sb, err = h._sb_guard(body)
    if err:
        return None, None, err
    return body, sb, None


def _fail(h, e):
    code = 400
    return h._json({"ok": False, "error": str(e)}, code)


@route("/api/git/status")
def _api_git_status(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.status(sb))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/diff")
def _api_git_diff(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.diff_file(sb, body.get("path"),
                                     staged=bool(body.get("staged")),
                                     context=body.get("context")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/stage")
def _api_git_stage(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        paths = body.get("paths")
        if not paths and body.get("path"):
            paths = [body.get("path")]
        return h._json(git.stage(sb, paths))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/unstage")
def _api_git_unstage(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        paths = body.get("paths")
        if not paths and body.get("path"):
            paths = [body.get("path")]
        return h._json(git.unstage(sb, paths))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/commit")
def _api_git_commit(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.commit(sb, body.get("message"),
                                  rels=body.get("paths"),
                                  add_all=bool(body.get("all"))))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/log")
def _api_git_log(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.log(sb, body.get("limit")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/patch")
def _api_git_patch(h):
    """工作区 / 暂存区的 unified diff 文本（只读）。"""
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.patch(sb, body.get("path"),
                                 staged=bool(body.get("staged"))))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/show")
def _api_git_show(h):
    """单个提交的元信息 + patch（只读）。"""
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.show_commit(sb, body.get("rev") or body.get("hash"),
                                       context=body.get("context")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/ops")
def _api_git_ops(h):
    """本会话里 AI 执行过的 git 命令（从工具时间线聚合，只读）。"""
    body, sb, err = _guard(h)
    if err:
        return err
    from app.sessions import load_sessions, find_session
    data = load_sessions()
    s = find_session(data, body.get("session_id"))
    items = []
    for mi, m in enumerate((s or {}).get("messages") or []):
        for t in (m.get("tools") or []):
            if t.get("tool") != "run_command":
                continue
            a = t.get("args") or {}
            argv = a.get("argv")
            if isinstance(argv, str):
                argv = argv.split()
            if not isinstance(argv, list) or not argv:
                cmd = str(a.get("cmd") or a.get("command") or "").strip()
                argv = cmd.split() if cmd else []
            if not argv:
                continue
            exe = os.path.basename(str(argv[0]).replace("\\", "/")).lower()
            if exe not in ("git", "git.exe"):
                continue
            sub = str(argv[1]) if len(argv) > 1 else ""
            if sub.startswith("-"):
                sub = ""
            items.append({
                "i": mi, "sub": sub or "git", "argv": [str(x) for x in argv][:20],
                "ok": bool(t.get("ok")), "ms": int(t.get("ms") or 0),
                "result": str(t.get("result") or "")[:300],
                "risk": sub in ("checkout", "restore", "reset", "clean",
                                "push", "switch", "rebase"),
            })
    return h._json({"ok": True, "items": items[-30:]})


# ============================================================
# 前端 Git 管理（批量文件 / 提交 / 分支）
# 危险操作要求前端显式传 confirm，避免误点直接毁工作区。
# ============================================================

def _rel_paths(body):
    paths = body.get("paths")
    if not paths and body.get("path"):
        paths = [body.get("path")]
    return paths if isinstance(paths, list) else []


@route("/api/git/discard")
def _api_git_discard(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.discard(sb, _rel_paths(body)))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/rm")
def _api_git_rm(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.rm_files(sb, _rel_paths(body)))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/clean")
def _api_git_clean(h):
    body, sb, err = _guard(h)
    if err:
        return err
    if body.get("confirm") not in (True, "clean", "yes"):
        return h._json({"ok": False, "error": "清理未跟踪文件不可恢复，需要二次确认"}, 400)
    try:
        return h._json(git.clean(sb, _rel_paths(body)))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/reset")
def _api_git_reset(h):
    body, sb, err = _guard(h)
    if err:
        return err
    mode = str(body.get("mode") or "soft").lower()
    if mode == "hard" and body.get("confirm") != "hard":
        return h._json({"ok": False,
                        "error": "reset --hard 会丢弃工作区改动，需要输入确认"}, 400)
    try:
        return h._json(git.reset(sb, body.get("rev") or "HEAD", mode))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/revert")
def _api_git_revert(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.revert(sb, body.get("rev") or body.get("hash")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/amend")
def _api_git_amend(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.amend(sb, body.get("message")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/branches")
def _api_git_branches(h):
    _body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.branches(sb))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/checkout")
def _api_git_checkout(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.checkout(sb, body.get("branch")))
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/branch")
def _api_git_branch(h):
    """action = create / delete / rename。"""
    body, sb, err = _guard(h)
    if err:
        return err
    action = str(body.get("action") or "").lower()
    try:
        if action == "create":
            return h._json(git.branch_create(sb, body.get("name"),
                                             body.get("start_point") or ""))
        if action == "delete":
            return h._json(git.branch_delete(sb, body.get("name"),
                                             bool(body.get("force"))))
        if action == "rename":
            return h._json(git.branch_rename(sb, body.get("old") or body.get("name"),
                                             body.get("new") or body.get("new_name")))
        return h._json({"ok": False, "error": "action 只能是 create / delete / rename"}, 400)
    except git.GitError as e:
        return _fail(h, e)


@route("/api/git/stash")
def _api_git_stash(h):
    body, sb, err = _guard(h)
    if err:
        return err
    try:
        return h._json(git.stash(sb, body.get("action") or "push",
                                 body.get("message") or ""))
    except git.GitError as e:
        return _fail(h, e)

