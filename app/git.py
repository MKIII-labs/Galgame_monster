# -*- coding: utf-8 -*-
"""Git 的薄封装：状态、差异、暂存、提交、日志。

三条约束决定了这里的写法：
1. **一律带 `--relative`**：沙箱可能只是仓库的一个子目录，git 默认输出仓库根
   相对路径，那和 IDE 目录树里的路径对不上；`--relative` 让它按 cwd 出路径。
2. **一律带 `-- .` 限定范围**：只报告沙箱内的改动，不把仓库别处的东西拉进来。
3. **不允许交互**：`GIT_TERMINAL_PROMPT=0`，否则缺凭据时进程会卡住等输入。
"""
import os
import re
import subprocess
from app import diffview

TIMEOUT = 20
MAX_FILES = 500
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_CONTEXT = 200


class GitError(Exception):
    """git 不可用 / 不是仓库 / 命令失败。"""


def _run(root, args, timeout=TIMEOUT, stdin_bytes=None):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        p = subprocess.run(
            ["git", "-c", "core.quotepath=false"] + list(args),
            cwd=root, input=stdin_bytes, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except FileNotFoundError:
        raise GitError("找不到 git 命令，请先安装 Git 并加入 PATH")
    except subprocess.TimeoutExpired:
        raise GitError("git 命令超时（%ds）：%s" % (timeout, " ".join(args)))
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace").strip()


def _text(raw):
    if len(raw) > MAX_TEXT_BYTES:
        raw = raw[:MAX_TEXT_BYTES]
    if b"\x00" in raw:
        return None                      # 二进制文件不按文本 diff
    return raw.decode("utf-8", "replace")


def _safe_rel(root, rel):
    """把用户/模型给的相对路径收敛到沙箱内；越界直接拒绝。"""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or os.path.isabs(rel):
        raise GitError("路径必须是沙箱内的相对路径：%r" % rel)
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([target, root_real]) != root_real:
        raise GitError("路径越界：%s" % rel)
    if target == root_real:
        raise GitError("这是仓库根目录，不是文件")
    return rel


def repo_info(root):
    """返回 {ok, root, reason}。root 是仓库根（可能与沙箱不同）。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "root": "", "reason": "沙箱目录不存在"}
    code, out, err = _run(root, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return {"ok": False, "root": "", "reason": err or "当前目录不是 Git 仓库"}
    return {"ok": True, "root": out.decode("utf-8", "replace").strip(), "reason": ""}


def _remap(root, toplevel, path):
    """git 输出的路径是「仓库根相对」的，这里是「沙箱相对」的。

    `git status` 不支持 `--relative`（只有 diff / log 有），所以只能自己换算。
    """
    if not path:
        return path
    if os.path.normcase(os.path.abspath(toplevel)) == os.path.normcase(os.path.abspath(root)):
        return path
    full = os.path.join(toplevel, path.replace("/", os.sep))
    return os.path.relpath(full, root).replace("\\", "/")


def status(root):
    """解析 `--porcelain=v2 -z --branch`，返回分支信息与文件列表。"""
    info = repo_info(root)
    if not info["ok"]:
        return {"ok": False, "repo": False, "reason": info["reason"], "files": [],
                "branch": "", "ahead": 0, "behind": 0}
    toplevel = info["root"]
    code, out, err = _run(root, ["status", "--porcelain=v2", "-z", "--branch",
                                 "--untracked-files=all", "--", "."])
    if code != 0:
        raise GitError(err or "git status 失败")
    branch, ahead, behind = "", 0, 0
    files = []
    parts = out.split(b"\x00")
    i = 0
    while i < len(parts):
        raw = parts[i]
        i += 1
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
            continue
        if line.startswith("# branch.ab "):
            try:
                ab = line[len("# branch.ab "):].split()
                ahead = int(ab[0].lstrip("+"))
                behind = int(ab[1].lstrip("-"))
            except (ValueError, IndexError):
                pass
            continue
        if line.startswith("#"):
            continue
        if line.startswith("1 "):
            seg = line.split(" ", 8)
            if len(seg) < 9:
                continue
            xy, path = seg[1], _remap(root, toplevel, seg[8])
            files.append(_entry(path, xy))
        elif line.startswith("2 "):
            seg = line.split(" ", 9)
            if len(seg) < 10:
                continue
            xy, path = seg[1], _remap(root, toplevel, seg[9])
            entry = _entry(path, xy)
            entry["renamed"] = True
            # -z 模式下原名单独一段
            if i < len(parts):
                entry["orig"] = _remap(root, toplevel,
                                       parts[i].decode("utf-8", "replace"))
                i += 1
            files.append(entry)
        elif line.startswith("u "):
            seg = line.split(" ", 10)
            if len(seg) >= 11:
                files.append(_entry(_remap(root, toplevel, seg[10]), "UU"))
        elif line.startswith("? "):
            path = _remap(root, toplevel, line[2:])
            files.append({"path": path, "x": "?", "y": "?",
                          "staged": False, "untracked": True, "status": "U"})
        if len(files) >= MAX_FILES:
            break
    return {"ok": True, "repo": True, "reason": "", "files": files,
            "branch": branch or "(detached)", "ahead": ahead, "behind": behind,
            "remotes": _remotes(root), "upstream": _upstream(root),
            "capped": len(files) >= MAX_FILES}


def _remotes(root):
    code, out, _err = _run(root, ["remote"])
    if code != 0:
        return []
    return [x.strip() for x in out.decode("utf-8", "replace").splitlines() if x.strip()]


def _upstream(root):
    code, out, _err = _run(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return out.decode("utf-8", "replace").strip() if code == 0 else ""


def _entry(path, xy):
    x = xy[0] if len(xy) > 0 else " "
    y = xy[1] if len(xy) > 1 else " "
    # porcelain v2 里「没变」用 '.' 表示（v1 才是空格），两种都要当空
    staged = x not in (" ", ".", "?", "!")
    unstaged = y not in (" ", ".", "?", "!")
    return {
        "path": path, "x": x, "y": y,
        "staged": staged, "unstaged": unstaged, "untracked": False,
        "status": _label(x if staged else y),
    }


def _label(flag):
    return {"M": "M", "A": "A", "D": "D", "R": "R", "C": "C", "U": "U",
            "T": "T"}.get(flag, "?")


def _show(root, spec):
    """git show 的容错版本：不存在就当空文件。"""
    code, out, _err = _run(root, ["show", spec])
    if code != 0:
        return ""
    return _text(out)


def diff_file(root, rel, staged=False, context=3):
    """返回与 /api/sandbox/diff 同构的 diff。

    - staged=True：HEAD → 暂存区
    - staged=False：暂存区（没有就用 HEAD）→ 工作区
    未跟踪文件：空 → 工作区。
    """
    rel = _safe_rel(root, rel)
    try:
        context = max(0, min(int(context or 3), MAX_CONTEXT))
    except (TypeError, ValueError):
        context = 3
    abs_path = os.path.join(root, rel.replace("/", os.sep))
    work = ""
    work_binary = False
    if os.path.isfile(abs_path):
        with open(abs_path, "rb") as f:
            work = _text(f.read())
        work_binary = work is None
    if staged:
        old = _show(root, "HEAD:./" + rel)
        new = _show(root, ":./" + rel)
    else:
        old = _show(root, ":./" + rel)
        if not old:
            old = _show(root, "HEAD:./" + rel)
        new = work if work is not None else ""
    if old is None or new is None or work_binary:
        return {"ok": True, "path": rel, "binary": True, "old": "", "new": "",
                "hunks": [], "adds": 0, "dels": 0, "old_total": 0, "new_total": 0,
                "gap_after": 0}
    d = diffview.build(old, new, context)
    return {"ok": True, "path": rel, "binary": False, "old": old, "new": new,
            "hunks": d["hunks"], "adds": d["adds"], "dels": d["dels"],
            "old_total": d["old_total"], "new_total": d["new_total"],
            "gap_after": d["gap_after"]}


def _paths(root, rels):
    out = []
    for r in rels or []:
        out.append(_safe_rel(root, r))
    return out


def stage(root, rels):
    paths = _paths(root, rels)
    if not paths:
        raise GitError("缺少要暂存的文件")
    code, _out, err = _run(root, ["add", "--"] + paths)
    if code != 0:
        raise GitError(err or "git add 失败")
    return {"ok": True, "count": len(paths)}


def unstage(root, rels):
    paths = _paths(root, rels)
    if not paths:
        raise GitError("缺少要取消暂存的文件")
    code, _out, err = _run(root, ["restore", "--staged", "--"] + paths)
    if code != 0:
        # 老版本 git 没有 restore --staged
        code, _out, err = _run(root, ["reset", "-q", "HEAD", "--"] + paths)
    if code != 0:
        raise GitError(err or "取消暂存失败")
    return {"ok": True, "count": len(paths)}


def commit(root, message, rels=None, add_all=False):
    msg = str(message or "").strip()
    if not msg:
        raise GitError("提交信息不能为空")
    if len(msg) > 4000:
        msg = msg[:4000]
    if add_all:
        code, _out, err = _run(root, ["add", "-A", "--", "."])
        if code != 0:
            raise GitError(err or "git add -A 失败")
    elif rels:
        stage(root, rels)
    code, out, err = _run(root, ["commit", "-F", "-"], stdin_bytes=msg.encode("utf-8"))
    if code != 0:
        raise GitError(err or "git commit 失败（可能没有已暂存的改动）")
    hcode, hout, _herr = _run(root, ["rev-parse", "--short", "HEAD"])
    summary = out.decode("utf-8", "replace").strip()
    return {"ok": True,
            "hash": hout.decode("utf-8", "replace").strip() if hcode == 0 else "",
            "summary": summary[-800:]}


def _ref(name, default="HEAD"):
    """分支名 / HEAD 系列 / 十六进制提交号：只接受这三种形态。"""
    name = str(name or default).strip() or default
    if re.match(r"^(HEAD([~^]\d*)*|[0-9a-fA-F]{4,40})$", name):
        return name
    return _clean_branch(name)


def _cap(text, max_chars):
    try:
        cap = max(1000, min(int(max_chars or 0), 400000))
    except (TypeError, ValueError):
        cap = 60000
    text = str(text or "")
    if len(text) <= cap:
        return text, False
    return text[:cap] + "\n…（diff 已截断）", True


def _untracked_patches(root):
    """未跟踪文件 git diff 不输出，逐个手工生成「新文件」补丁。"""
    out = []
    try:
        st = status(root)
    except GitError:
        return out
    for f in st.get("files") or []:
        if not f.get("untracked"):
            continue
        try:
            p = patch(root, f["path"])
        except GitError:
            continue
        if p.get("patch"):
            out.append(p["patch"])
    return out


def diff_text(root, scope="work", rev="", base="", head="HEAD", max_chars=60000):
    """给 AI 审阅用的 diff 文本。

    scope = work   未提交的改动（含未跟踪文件）
    scope = commit 单个提交的 patch
    scope = branch base...head 的分支差异（三点 = 以 merge-base 为基准）
    """
    scope = str(scope or "work").lower()
    if scope == "commit":
        rev = str(rev or "").strip()
        if not re.match(r"^[0-9a-fA-F]{4,40}$", rev):
            raise GitError("提交号不合法：%r" % rev)
        code, out, err = _run(root, ["show", "--no-color", "--relative",
                                     "--unified=3", "--format=", rev, "--", "."])
        label = "提交 " + rev[:12]
        if code != 0:
            raise GitError(err or "读取提交失败")
        text = out.decode("utf-8", "replace")
    elif scope == "branch":
        b = _ref(base, "")
        h = _ref(head, "HEAD")
        code, out, err = _run(root, ["diff", "--no-color", "--relative",
                                     "--unified=3", "%s...%s" % (b, h), "--", "."])
        label = "%s...%s" % (b, h)
        if code != 0:
            raise GitError(err or "读取分支差异失败")
        text = out.decode("utf-8", "replace")
    else:
        scope = "work"
        code, out, err = _run(root, ["diff", "--no-color", "--relative",
                                     "--unified=3", "HEAD", "--", "."])
        if code != 0:
            raise GitError(err or "读取未提交改动失败")
        text = out.decode("utf-8", "replace")
        extra = _untracked_patches(root)
        if extra:
            text = (text.rstrip() + "\n" + "\n".join(extra)).strip()
        label = "未提交的改动"
    files = sorted(set(re.findall(r"^diff --git a/(\S+)", text, re.M)))
    text, truncated = _cap(text, max_chars)
    return {"ok": True, "scope": scope, "label": label, "text": text,
            "files": files, "truncated": truncated}


def commit_patch(root, max_chars=40000):
    """生成提交信息用的 diff：优先暂存区，没有暂存改动就用工作区。"""
    code, out, err = _run(root, ["diff", "--cached", "--no-color", "--relative",
                                 "--unified=3", "--", "."])
    if code != 0:
        raise GitError(err or "读取暂存改动失败")
    text = out.decode("utf-8", "replace")
    staged = bool(text.strip())
    if not staged:
        code, out, err = _run(root, ["diff", "--no-color", "--relative",
                                     "--unified=3", "HEAD", "--", "."])
        if code != 0:
            raise GitError(err or "读取工作区改动失败")
        text = out.decode("utf-8", "replace")
        extra = _untracked_patches(root)
        if extra:
            text = (text.rstrip() + "\n" + "\n".join(extra)).strip()
    text, truncated = _cap(text, max_chars)
    return {"ok": True, "text": text, "staged": staged, "truncated": truncated}


def log(root, limit=20):
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    code, out, err = _run(root, ["log", "-n", str(limit),
                                 "--pretty=format:%h%x09%ad%x09%an%x09%s",
                                 "--date=short"])
    if code != 0:
        return {"ok": False, "error": err or "读取提交历史失败", "items": []}
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        seg = line.split("\t")
        if len(seg) >= 4:
            items.append({"hash": seg[0], "date": seg[1], "author": seg[2],
                          "subject": seg[3]})
    return {"ok": True, "error": "", "items": items}


def last_commit_message(root):
    """给模型写提交信息时参考（最近 10 条 subject）。"""
    r = log(root, 10)
    if not r.get("ok"):
        return ""
    return "\n".join("- " + x["subject"] for x in r["items"])


def patch(root, rel, staged=False):
    """工作区 / 暂存区的 unified diff 文本（只读，供前端预览）。

    未跟踪文件 git diff 不会输出，这里手工生成一个「新文件」补丁。
    """
    rel = _safe_rel(root, rel)
    args = ["diff", "--no-color", "--unified=3"]
    if staged:
        args.append("--cached")
    args += ["--", rel]
    code, out, err = _run(root, args)
    if code != 0:
        raise GitError(err or "读取补丁失败")
    text = out.decode("utf-8", "replace")
    if text.strip():
        return {"ok": True, "path": rel, "binary": False, "patch": text}
    abs_path = os.path.join(root, rel.replace("/", os.sep))
    if not staged and os.path.isfile(abs_path):
        with open(abs_path, "rb") as f:
            raw = f.read()
        body = _text(raw)
        if body is None:
            return {"ok": True, "path": rel, "binary": True, "patch": ""}
        lines = body.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        head = ("diff --git a/%s b/%s\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n"
                % (rel, rel, rel, len(lines)))
        return {"ok": True, "path": rel, "binary": False,
                "patch": head + "\n".join("+" + ln for ln in lines) + "\n"}
    return {"ok": True, "path": rel, "binary": False, "patch": ""}


def show_commit(root, rev, context=3):
    """单个提交的元信息 + patch（供历史列表点开查看）。"""
    rev = str(rev or "").strip()
    if not re.match(r"^[0-9a-fA-F]{4,40}$", rev):
        raise GitError("提交号不合法：%r" % rev)
    try:
        context = max(0, min(int(context or 3), MAX_CONTEXT))
    except (TypeError, ValueError):
        context = 3
    fmt = "%H%x09%h%x09%an%x09%ad%x09%s"
    code, out, err = _run(root, ["show", "--no-color", "--date=short",
                                 "--pretty=format:" + fmt,
                                 "--unified=%d" % context, rev])
    if code != 0:
        raise GitError(err or "读取提交失败")
    text = out.decode("utf-8", "replace")
    head, _, body = text.partition("\n")
    seg = head.split("\t")
    adds = sum(1 for ln in body.splitlines()
               if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in body.splitlines()
               if ln.startswith("-") and not ln.startswith("---"))
    return {
        "ok": True,
        "hash": seg[0] if seg else rev,
        "short": seg[1] if len(seg) > 1 else rev[:7],
        "author": seg[2] if len(seg) > 2 else "",
        "date": seg[3] if len(seg) > 3 else "",
        "subject": seg[4] if len(seg) > 4 else "",
        "patch": body, "adds": adds, "dels": dels,
    }


# ============================================================
# 前端管理：丢弃改动、删除、回退、分支、stash
# 所有写操作都只作用于会话沙箱；危险操作由 API 层做二次确认。
# ============================================================

def _clean_branch(name):
    """分支名只接受保守字符集，避免被拼成命令行选项或越界引用。"""
    name = str(name or "").strip()
    if not name or len(name) > 120:
        raise GitError("分支名不能为空且不能超过 120 字符")
    if name.startswith("-") or ".." in name or not re.match(r"^[A-Za-z0-9._/\-]+$", name):
        raise GitError("分支名不合法：%r" % name)
    return name


def _clean_rev(rev):
    """只接受 HEAD 系列或十六进制提交号；不接受任意 rev 表达式。"""
    rev = str(rev or "HEAD").strip()
    if not re.match(r"^(HEAD([~^]\d*)*|[0-9a-fA-F]{4,40})$", rev):
        raise GitError("提交号不合法：%r" % rev)
    return rev


def discard(root, rels):
    """丢弃工作区改动：已跟踪文件 restore，未跟踪文件直接清理。"""
    paths = _paths(root, rels)
    if not paths:
        raise GitError("缺少要丢弃改动的文件")
    code, out, _err = _run(root, ["ls-files", "--"] + paths)
    tracked_set = set(out.decode("utf-8", "replace").splitlines())
    tracked = [p for p in paths if p in tracked_set]
    untracked = [p for p in paths if p not in tracked_set]
    if tracked:
        code, _out, err = _run(root, ["restore", "--worktree", "--"] + tracked)
        if code != 0:
            raise GitError(err or "撤销改动失败")
    if untracked:
        code, _out, err = _run(root, ["clean", "-f", "--"] + untracked)
        if code != 0:
            raise GitError(err or "删除未跟踪文件失败")
    return {"ok": True, "count": len(paths),
            "tracked": len(tracked), "untracked": len(untracked)}


def rm_files(root, rels):
    """删除已跟踪文件（git rm），删除动作会进入暂存区。"""
    paths = _paths(root, rels)
    if not paths:
        raise GitError("缺少要删除的文件")
    code, _out, err = _run(root, ["rm", "-f", "--"] + paths)
    if code != 0:
        raise GitError(err or "删除文件失败")
    return {"ok": True, "count": len(paths)}


def clean(root, rels=None):
    """清理未跟踪文件 / 目录（git clean -fd）。rels 为空时清理整个沙箱。"""
    paths = _paths(root, rels) if rels else []
    args = ["clean", "-f", "-d"]
    if paths:
        args += ["--"] + paths
    code, out, err = _run(root, args)
    if code != 0:
        raise GitError(err or "清理未跟踪文件失败")
    return {"ok": True, "count": len(paths),
            "output": out.decode("utf-8", "replace")[-2000:]}


def reset(root, rev="HEAD", mode="soft"):
    """回退到某个提交。hard 会丢工作区改动，调用方必须先确认。"""
    mode = str(mode or "soft").lower()
    if mode not in ("soft", "mixed", "hard"):
        raise GitError("reset 模式只能是 soft / mixed / hard")
    rev = _clean_rev(rev)
    code, _out, err = _run(root, ["reset", "--" + mode, rev])
    if code != 0:
        raise GitError(err or "git reset 失败")
    return {"ok": True, "mode": mode, "rev": rev}


def revert(root, rev):
    """反向提交某个历史提交（不改写历史，最安全）。"""
    rev = str(rev or "").strip()
    if not re.match(r"^[0-9a-fA-F]{4,40}$", rev):
        raise GitError("提交号不合法：%r" % rev)
    code, _out, err = _run(root, ["revert", "--no-edit", rev])
    if code != 0:
        raise GitError(err or "撤回提交失败（可能有冲突，请先解决）")
    return {"ok": True, "rev": rev}


def amend(root, message):
    """修改最后一次提交的信息（不动文件内容）。"""
    msg = str(message or "").strip()
    if not msg:
        raise GitError("提交信息不能为空")
    if len(msg) > 4000:
        msg = msg[:4000]
    code, out, err = _run(root, ["commit", "--amend", "-F", "-"],
                          stdin_bytes=msg.encode("utf-8"))
    if code != 0:
        raise GitError(err or "修改提交信息失败")
    hcode, hout, _herr = _run(root, ["rev-parse", "--short", "HEAD"])
    return {"ok": True,
            "hash": hout.decode("utf-8", "replace").strip() if hcode == 0 else "",
            "summary": out.decode("utf-8", "replace").strip()[-800:]}


def branches(root):
    """本地分支列表（含当前分支与上游）。"""
    code, out, err = _run(root, ["branch",
                                 "--format=%(refname:short)%09%(HEAD)%09%(upstream:short)"])
    if code != 0:
        raise GitError(err or "读取分支失败")
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        seg = line.split("\t")
        if not seg or not seg[0]:
            continue
        items.append({"name": seg[0],
                      "current": len(seg) > 1 and seg[1].strip() == "*",
                      "upstream": seg[2] if len(seg) > 2 else ""})
    cur = next((b["name"] for b in items if b["current"]), "")
    return {"ok": True, "items": items, "current": cur}


def checkout(root, name):
    """切换分支（优先 switch，老版本 git 回落 checkout）。"""
    name = _clean_branch(name)
    code, _out, err = _run(root, ["switch", name])
    if code != 0:
        code, _out, err = _run(root, ["checkout", name])
    if code != 0:
        raise GitError(err or "切换分支失败")
    return {"ok": True, "branch": name}


def branch_create(root, name, start_point=""):
    name = _clean_branch(name)
    args = ["branch", name]
    if start_point:
        args.append(_clean_rev(start_point))
    code, _out, err = _run(root, args)
    if code != 0:
        raise GitError(err or "新建分支失败")
    return {"ok": True, "branch": name}


def branch_delete(root, name, force=False):
    """删除本地分支。未合并的分支必须显式 force。"""
    name = _clean_branch(name)
    code, _out, err = _run(root, ["branch", "-D" if force else "-d", name])
    if code != 0:
        if not force and "not fully merged" in err.lower():
            raise GitError("分支还没合并；确认要删除请用强制删除")
        raise GitError(err or "删除分支失败")
    return {"ok": True, "branch": name}


def branch_rename(root, old, new):
    old = _clean_branch(old)
    new = _clean_branch(new)
    code, _out, err = _run(root, ["branch", "-m", old, new])
    if code != 0:
        raise GitError(err or "重命名分支失败")
    return {"ok": True, "from": old, "to": new}


def stash(root, action="push", message=""):
    """stash / stash pop / stash list / stash drop。"""
    action = str(action or "push").lower()
    if action == "push":
        args = ["stash", "push", "-u"]
        if message:
            args += ["-m", str(message)[:200]]
    elif action == "pop":
        args = ["stash", "pop"]
    elif action == "list":
        args = ["stash", "list"]
    elif action == "drop":
        args = ["stash", "drop"]
    else:
        raise GitError("stash 动作只能是 push / pop / list / drop")
    code, out, err = _run(root, args)
    if code != 0:
        raise GitError(err or "stash 失败")
    return {"ok": True, "action": action,
            "output": out.decode("utf-8", "replace")[-2000:]}


# ============================================================
# 远端同步 / 标签 / 分支图
# ============================================================

def sync(root, action="fetch", branch="", force=False, timeout=180):
    """fetch / pull / push。

    `GIT_TERMINAL_PROMPT=0` 保证缺凭据时立刻失败并返回 stderr，
    而不是把进程挂在交互提示上。
    """
    action = str(action or "fetch").lower()
    if action not in ("fetch", "pull", "push"):
        raise GitError("同步动作只能是 fetch / pull / push")
    if action == "fetch":
        code, out, err = _run(root, ["fetch", "--all", "--prune"], timeout=timeout)
        label = "拉取远端信息"
    elif action == "pull":
        code, out, err = _run(root, ["pull", "--no-edit"], timeout=timeout)
        label = "拉取并合并"
    else:
        b = str(branch or "").strip()
        if not b:
            c, o, _e = _run(root, ["symbolic-ref", "--short", "HEAD"])
            b = o.decode("utf-8", "replace").strip() if c == 0 else ""
        if b:
            b = _clean_branch(b)
        # 没有上游时直接 -u：`git push origin <b>` 不会自动建立跟踪关系
        has_upstream = bool(_upstream(root))
        args = ["push"]
        if force:
            args.append("--force-with-lease")
        if b:
            args += ([] if has_upstream else ["-u"]) + ["origin", b]
        code, out, err = _run(root, args, timeout=timeout)
        label = "推送到远端"
    if code != 0:
        raise GitError(err or (label + "失败"))
    text = (out.decode("utf-8", "replace") + "\n" + err).strip()
    return {"ok": True, "action": action, "output": text[-4000:]}


def tags(root):
    """本地标签列表（按创建时间倒序）。"""
    code, out, err = _run(root, [
        "for-each-ref", "--sort=-creatordate",
        "--format=%(refname:short)%09%(objectname:short)%09%(creatordate:short)",
        "refs/tags"])
    if code != 0:
        raise GitError(err or "读取标签失败")
    items = []
    for line in out.decode("utf-8", "replace").splitlines():
        seg = line.split("\t")
        if seg and seg[0]:
            items.append({"name": seg[0],
                          "hash": seg[1] if len(seg) > 1 else "",
                          "date": seg[2] if len(seg) > 2 else ""})
    return {"ok": True, "items": items}


def tag_create(root, name, rev="", message=""):
    name = _clean_branch(name)
    rev = _clean_rev(rev) if str(rev or "").strip() else ""
    msg = str(message or "").strip()
    if msg:
        args = ["tag", "-a", name, "-m", msg[:500]] + ([rev] if rev else [])
    else:
        args = ["tag", name] + ([rev] if rev else [])
    code, _out, err = _run(root, args)
    if code != 0:
        raise GitError(err or "创建标签失败")
    return {"ok": True, "name": name}


def tag_delete(root, name):
    name = _clean_branch(name)
    code, _out, err = _run(root, ["tag", "-d", name])
    if code != 0:
        raise GitError(err or "删除标签失败")
    return {"ok": True, "name": name}


def graph_log(root, limit=60):
    """带分支图的提交历史（ASCII 图由 git 生成，前端只按列展示）。"""
    try:
        limit = max(1, min(int(limit or 60), 200))
    except (TypeError, ValueError):
        limit = 60
    code, out, err = _run(root, [
        "log", "--graph", "--all", "--no-color", "--date=short",
        "-n", str(limit),
        # \x1f 作为分隔符：--graph 会把 "* " 前缀拼在第一个字段前面，
        # 不能按第一个 tab 切（否则提交号会被并进图里）。
        # 不用 \x1e：Python 的 splitlines() 会把它当换行，行会被切碎。
        "--pretty=format:%x1f%h%x09%ad%x09%an%x09%s%x09%d"])
    if code != 0:
        return {"ok": False, "error": err or "读取提交历史失败", "items": []}
    items = []
    for line in out.decode("utf-8", "replace").split("\n"):
        if "\x1f" not in line:
            continue
        graph, rest = line.split("\x1f", 1)
        seg = rest.split("\t")
        if len(seg) < 4:
            continue
        items.append({
            "graph": graph.rstrip(),
            "hash": seg[0], "date": seg[1], "author": seg[2],
            "subject": seg[3],
            "refs": seg[4].strip() if len(seg) > 4 else "",
        })
    return {"ok": True, "error": "", "items": items}
