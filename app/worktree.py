# -*- coding: utf-8 -*-
"""本地 git worktree 隔离：给每个可写子代理一份独立工作区。

为什么要隔离：全局 WRITE_LOCK 只保证「不并发写盘」，两个子代理先后改同一个
文件时后写者仍可能覆盖前者的改动（write_guard 只能提醒 / 拒绝，不能防止）。

做法：
1. `git worktree add --detach` 在 data/worktrees/<batch>/<agent> 建一份工作区；
2. 把**当前工作区状态**带过去（stash create 带上 staged/unstaged 改动，
   再补拷未跟踪文件），子代理看到的是你磁盘上的代码，而不是 HEAD；
3. 在 worktree 里做一个基线提交（commit-tree + reset --soft，不跑 hook、
   不动任何分支），之后的改动就是 `git diff HEAD`；
4. 跑完收集「变更文件列表 + 统一补丁」，由调用方审批后 `git apply` 回主沙箱
   并登记撤销快照；冲突（主沙箱已被别人改过）时拒绝应用、保留补丁。

边界：
- 只在本地跑 git 命令，不联网；没有外部依赖。
- 沙箱不是 git 仓库 / 仓库还没有提交 / 建 worktree 失败 → 返回错误，
  调用方退回原来的「全局写锁 + 冲突提示」，不做半套隔离。
- 本模块不碰主沙箱（除 apply / apply_check 这两个显式入口）。
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time

from app.store import DATA_DIR

GIT_TIMEOUT = 60                       # 单条 git 命令超时（秒）
PATCH_MAX = 8 * 1024 * 1024            # 补丁上限（字节），超过就要求缩小改动
UNTRACKED_FILE_MAX = 2 * 1024 * 1024   # 单个未跟踪文件最大拷贝体积
UNTRACKED_TOTAL_MAX = 32 * 1024 * 1024 # 一批未跟踪文件的总拷贝上限
KEEP_DAYS = 1.0                        # data/worktrees 下残留目录的保留天数
NAME_MAX = 40

_LOCK = threading.Lock()               # 建 / 删 worktree 串行，避免 git 索引竞争


class WorktreeError(RuntimeError):
    pass


class Worktree:
    """一份隔离工作区。ok=False 时 error 说明为什么没建成。"""

    def __init__(self):
        self.ok = False
        self.error = ""
        self.repo = ""        # 主仓库根目录
        self.rel = ""         # 主沙箱相对仓库根的路径（"" = 沙箱就是仓库根）
        self.path = ""        # worktree 根目录
        self.sandbox = ""     # worktree 里与主沙箱对应的目录
        self.base = ""        # 基线提交
        self.patch_path = ""  # 补丁落盘位置（供冲突时人工处理）
        self.batch = ""
        self.name = ""

    def __bool__(self):
        return self.ok

    def display(self):
        return "worktree:%s" % (self.name or "agent")


def _safe(name):
    """目录名：只留安全字符；非 ASCII（如中文 id）会撞名，所以尾部带内容哈希。"""
    raw = str(name or "")
    s = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-")[:NAME_MAX] or "agent"
    return "%s-%s" % (s, hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:6])


def _flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _git(repo, *args, data=None, timeout=GIT_TIMEOUT, env_extra=None):
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                "GIT_OPTIONAL_LOCKS": "0"})
    env.update(env_extra or {})
    cmd = ["git", "-C", str(repo)] + [str(a) for a in args]
    try:
        return subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, env=env,
                              creationflags=_flags())
    except (OSError, subprocess.SubprocessError) as e:
        raise WorktreeError(str(e))


def _err(proc):
    raw = (proc.stderr or b"").strip() or (proc.stdout or b"").strip()
    text = raw.decode("utf-8", "replace").strip()
    return " ".join(text.split())[:300] or ("git 退出码 %s" % proc.returncode)


def _real(path):
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def repo_info(sandbox):
    """沙箱所在的 git 仓库信息；返回 (info, 错误)。

    info: {"repo": 仓库根, "rel": 沙箱相对仓库根的 posix 路径}
    """
    root = os.path.abspath(str(sandbox or ""))
    if not root or not os.path.isdir(root):
        return None, "沙箱目录不存在"
    try:
        proc = _git(root, "rev-parse", "--show-toplevel")
    except WorktreeError as e:
        return None, "无法执行 git：%s" % e
    if proc.returncode != 0:
        return None, "沙箱不是 git 仓库"
    repo = proc.stdout.decode("utf-8", "replace").strip()
    if not repo or not os.path.isdir(repo):
        return None, "无法定位 git 仓库根目录"
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return None, "仓库还没有任何提交，无法建 worktree"
    rel = os.path.relpath(_real(root), _real(repo)).replace("\\", "/")
    if rel == ".":
        rel = ""
    if rel.startswith("../"):
        return None, "沙箱不在这个 git 仓库里"
    return {"repo": repo, "rel": rel}, ""


def _purge(repo, path):
    """清掉同名残留（上次崩溃 / 复用批次号的情形）。"""
    try:
        if os.path.isdir(path):
            _git(repo, "worktree", "remove", "--force", path)
    except WorktreeError:
        pass
    shutil.rmtree(path, ignore_errors=True)
    try:
        _git(repo, "worktree", "prune")
    except WorktreeError:
        pass


def _prune_old(keep_days=KEEP_DAYS):
    """清理 data/worktrees 下超期的残留目录（进程崩溃时来不及 remove）。"""
    base = os.path.join(DATA_DIR, "worktrees")
    if not os.path.isdir(base):
        return
    limit = time.time() - max(0.0, float(keep_days)) * 86400
    for batch in os.listdir(base):
        bpath = os.path.join(base, batch)
        if not os.path.isdir(bpath):
            continue
        try:
            old = os.path.getmtime(bpath) < limit
        except OSError:
            continue
        if not old:
            continue
        shutil.rmtree(bpath, ignore_errors=True)


def _copy_untracked(wt):
    """补拷未跟踪文件：stash create 带不上它们，但子代理常常要读。"""
    try:
        proc = _git(wt.repo, "ls-files", "--others", "--exclude-standard", "-z")
    except WorktreeError:
        return
    if proc.returncode != 0:
        return
    total = 0
    for raw in (proc.stdout or b"").split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw).replace("\\", "/")
        if wt.rel and not (rel == wt.rel or rel.startswith(wt.rel + "/")):
            continue
        src = os.path.join(wt.repo, *rel.split("/"))
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if size > UNTRACKED_FILE_MAX or total + size > UNTRACKED_TOTAL_MAX:
            continue
        dst = os.path.join(wt.path, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst) or wt.path, exist_ok=True)
            shutil.copy2(src, dst)
            total += size
        except OSError:
            continue


def _commit_baseline(wt, rev):
    """在 worktree 里做一个基线提交：之后 `git diff HEAD` 就是子代理的改动。

    用 write-tree + commit-tree + reset --soft 而不是 `git commit`：
    不触发任何 hook，也不移动任何分支（detached HEAD）。
    """
    proc = _git(wt.path, "add", "-A")
    if proc.returncode != 0:
        return "", "基线登记失败：%s" % _err(proc)
    proc = _git(wt.path, "write-tree")
    tree = proc.stdout.decode("ascii", "ignore").strip()
    if proc.returncode != 0 or not tree:
        return "", "基线登记失败：%s" % _err(proc)
    ident = {"GIT_AUTHOR_NAME": "gm-agent", "GIT_AUTHOR_EMAIL": "agent@localhost",
             "GIT_COMMITTER_NAME": "gm-agent", "GIT_COMMITTER_EMAIL": "agent@localhost"}
    proc = _git(wt.path, "commit-tree", tree, "-p", rev,
                "-m", "gm worktree base", env_extra=ident)
    base = proc.stdout.decode("ascii", "ignore").strip()
    if proc.returncode != 0 or not base:
        return "", "基线登记失败：%s" % _err(proc)
    proc = _git(wt.path, "reset", "--soft", base)
    if proc.returncode != 0:
        return "", "基线登记失败：%s" % _err(proc)
    return base, ""


def create(sandbox, name, batch=""):
    """给一个子代理建隔离工作区。失败返回 Worktree(ok=False, error=...)。"""
    wt = Worktree()
    wt.name = _safe(name)
    wt.batch = _safe(batch or "adhoc")
    with _LOCK:
        try:
            _prune_old()
            info, err = repo_info(sandbox)
            if err:
                wt.error = err
                return wt
            wt.repo, wt.rel = info["repo"], info["rel"]
            wt.path = os.path.join(DATA_DIR, "worktrees", wt.batch, wt.name)
            wt.patch_path = os.path.join(DATA_DIR, "worktrees", wt.batch,
                                         wt.name + ".diff")
            _purge(wt.repo, wt.path)          # worktree add 要求目标不存在
            rev = "HEAD"
            proc = _git(wt.repo, "stash", "create")
            if proc.returncode == 0:
                sha = proc.stdout.decode("ascii", "ignore").strip()
                if sha:
                    rev = sha              # 带上未提交改动，子代理看到的是「现在的代码」
            proc = _git(wt.repo, "worktree", "add", "--detach", "--force",
                        wt.path, rev)
            if proc.returncode != 0:
                wt.error = "创建 worktree 失败：%s" % _err(proc)
                shutil.rmtree(wt.path, ignore_errors=True)
                return wt
            rel_parts = [p for p in wt.rel.split("/") if p]
            wt.sandbox = os.path.join(wt.path, *rel_parts) if rel_parts else wt.path
            if not os.path.isdir(wt.sandbox):
                wt.error = "worktree 里找不到沙箱目录：%s" % (wt.rel or ".")
                remove(wt)
                return wt
            _copy_untracked(wt)
            base, err = _commit_baseline(wt, rev)
            if err:
                wt.error = err
                remove(wt)
                return wt
            wt.base = base
            wt.ok = True
            return wt
        except Exception as e:                # noqa: BLE001  任何意外都退回共享沙箱
            wt.error = "创建隔离工作区失败：%s" % e
            try:
                remove(wt)
            except Exception:                 # noqa: BLE001
                pass
            return wt


def _rel_to_sandbox(wt, repo_path):
    """仓库相对路径 -> 沙箱相对路径；不在沙箱内返回 None。"""
    p = str(repo_path or "").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not wt.rel:
        return p
    if p == wt.rel:
        return ""
    if p.startswith(wt.rel + "/"):
        return p[len(wt.rel) + 1:]
    return None


_STATUS = {"A": "added", "M": "modified", "D": "deleted",
           "T": "modified", "C": "added", "R": "modified"}


def changes(wt):
    """子代理在 worktree 里改了什么：按文件列出（含沙箱外的改动）。"""
    if not wt or not wt.ok:
        return []
    proc = _git(wt.path, "add", "-A")
    if proc.returncode != 0:
        raise WorktreeError("收集改动失败：%s" % _err(proc))
    num = _git(wt.path, "diff", "--cached", "--numstat", "-z",
               "--no-renames", "HEAD")
    stat = _git(wt.path, "diff", "--cached", "--name-status", "-z",
                "--no-renames", "HEAD")
    status = {}
    parts = (stat.stdout or b"").split(b"\0")
    for i in range(0, len(parts) - 1, 2):
        code = parts[i].decode("ascii", "ignore").strip()
        if not code:
            continue
        status[os.fsdecode(parts[i + 1]).replace("\\", "/")] = code[0]
    out = []
    for raw in (num.stdout or b"").split(b"\0"):
        if not raw:
            continue
        cols = raw.split(b"\t", 2)
        if len(cols) < 3:
            continue
        add_s, del_s, path_b = cols
        path = os.fsdecode(path_b).replace("\\", "/")
        try:
            add = int(add_s)
            dele = int(del_s)
        except ValueError:
            add, dele = 0, 0           # 二进制文件：numstat 给的是 "-"
        rel = _rel_to_sandbox(wt, path)
        out.append({"path": path if rel is None else rel,
                    "repo_path": path,
                    "status": _STATUS.get(status.get(path, ""), "modified"),
                    "add": add, "del": dele,
                    "binary": add_s == b"-",
                    "outside": rel is None})
    out.sort(key=lambda f: f["repo_path"])
    return out


def patch(wt):
    """生成统一补丁（含二进制）；同时落盘一份，冲突时给人看。"""
    if not wt or not wt.ok:
        return b"", "隔离工作区不存在"
    # 先刷新索引：调用方可能只调 patch 而没调 changes（仍然要拿到完整补丁）
    proc = _git(wt.path, "add", "-A")
    if proc.returncode != 0:
        return b"", "收集改动失败：%s" % _err(proc)
    proc = _git(wt.path, "diff", "--cached", "--binary", "--no-renames", "HEAD")
    if proc.returncode != 0:
        return b"", "生成补丁失败：%s" % _err(proc)
    data = proc.stdout or b""
    if not data:
        return b"", ""
    if len(data) > PATCH_MAX:
        return b"", ("补丁过大（%d 字节，上限 %d），请缩小改动范围"
                     % (len(data), PATCH_MAX))
    try:
        os.makedirs(os.path.dirname(wt.patch_path), exist_ok=True)
        with open(wt.patch_path, "wb") as f:
            f.write(data)
    except OSError:
        pass
    return data, ""


def _apply(sandbox, data, check=False):
    info, err = repo_info(sandbox)
    if err:
        return False, err
    args = ["apply", "--binary", "--whitespace=nowarn"]
    if check:
        args.insert(1, "--check")
    proc = _git(info["repo"], *args, data=data)
    if proc.returncode != 0:
        return False, _err(proc)
    return True, ""


def apply_check(sandbox, data):
    """补丁能否干净落到主沙箱（不改文件）。"""
    if not data:
        return True, ""
    return _apply(sandbox, data, check=True)


def apply(sandbox, data):
    """把补丁应用到主沙箱。调用方负责审批、WRITE_LOCK 与撤销快照。"""
    if not data:
        return True, ""
    return _apply(sandbox, data, check=False)


def remove(wt):
    """删掉 worktree（补丁文件保留，供之后人工处理）。"""
    if not wt or not wt.path:
        return
    with _LOCK:
        try:
            if wt.repo:
                _git(wt.repo, "worktree", "remove", "--force", wt.path)
        except WorktreeError:
            pass
        shutil.rmtree(wt.path, ignore_errors=True)
        try:
            if wt.repo:
                _git(wt.repo, "worktree", "prune")
        except WorktreeError:
            pass
        wt.ok = False
