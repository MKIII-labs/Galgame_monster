# -*- coding: utf-8 -*-
"""影子 git 仓库：AI 写操作的检查点。

设计参考 Trae：每个工作区一个独立于项目 .git 的裸仓库（bare），
每次工具写入后把「写前 / 写后」内容写成 blob，并打一个 tag：

    refs/tags/toolcall-<turn>-<token>
    refs/tags/before-chat-turn-<turn>
    refs/tags/after-chat-turn-<turn>

仓库位置：``data/snapshots/<sha1(工作区)>/``（bare 仓库，没有工作区）。
内容只包含被写入过的文件，git object 天然去重；不依赖项目本身的版本控制。

取舍：
- 只是**附加**存储：撤销仍然走 ``undo.py`` 的内存 / 磁盘快照，
  这里负责持久化、去重与可追溯（git log / 查看历史），失败不影响对话。
- 机器没装 git 时整体降级：``available()`` 返回 False，调用方直接跳过。
"""
import hashlib
import json
import os
import subprocess
import threading
import time

from app.store import DATA_DIR

SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
AUTHOR = ("Galgame Monster", "undo@local")
_WARNED = set()
_LOCK = threading.Lock()
_GIT_OK = None          # None = 还没探测；True / False = 结果


def _git_bin():
    return "git"


def available():
    """本机是否有可用的 git（结果缓存）。"""
    global _GIT_OK
    if _GIT_OK is not None:
        return _GIT_OK
    try:
        r = subprocess.run([_git_bin(), "--version"], capture_output=True,
                           text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _GIT_OK = r.returncode == 0
    except Exception:
        _GIT_OK = False
    return _GIT_OK


def _warn_once(key, msg):
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(msg)


def repo_dir(root):
    """某个工作区对应的影子仓库目录。"""
    key = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(SNAP_DIR, key)


def _env(repo):
    env = dict(os.environ)
    env["GIT_DIR"] = repo
    env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = AUTHOR
    env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = AUTHOR
    env["GIT_CONFIG_NOSYSTEM"] = "1"          # 不读系统级 git 配置，避免被全局钩子/模板干扰
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def _run(repo, args, data=None, env_extra=None):
    env = _env(repo)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([_git_bin()] + args, input=data, capture_output=True,
                          env=env, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _ensure_repo(root):
    repo = repo_dir(root)
    if os.path.isdir(os.path.join(repo, "objects")):
        return repo
    os.makedirs(repo, exist_ok=True)
    r = _run(repo, ["init", "--bare", "-q"])
    if r.returncode != 0:
        _warn_once("init", "[快照] 初始化影子仓库失败：%s"
                   % (r.stderr or b"").decode("utf-8", "replace")[:200])
        return ""
    return repo


def _hash_blob(repo, data):
    r = _run(repo, ["hash-object", "-w", "--stdin"], data=data or b"")
    if r.returncode != 0:
        return ""
    return r.stdout.decode("ascii", "replace").strip()


def _resolve(repo, ref):
    r = _run(repo, ["rev-parse", "--verify", "-q", ref])
    if r.returncode != 0:
        return ""
    return r.stdout.decode("ascii", "replace").strip()


def _rel(root, path):
    """绝对路径 → 仓库内相对路径；不在工作区内返回空串（这类文件不进快照）。"""
    try:
        base = os.path.normcase(os.path.abspath(root).rstrip("\\/")) + os.sep
        full = os.path.abspath(path)
        if not os.path.normcase(full).startswith(base):
            return ""
        rel = os.path.relpath(full, os.path.abspath(root))
        return rel.replace("\\", "/")
    except (OSError, ValueError):
        return ""


def checkpoint(root, tag, turn="", label="", files=None):
    """为一个检查点落一次提交并打 tag。

    files: [{"path": 绝对路径, "before": bytes|None, "after": bytes|None}]
    before/after 为 None 表示「当时不存在」（新建 / 删除）。

    返回 commit id；任何失败都返回空串，不抛异常。
    """
    if not root or not tag:
        return ""
    if not available():
        return ""
    repo = _ensure_repo(root)
    if not repo:
        return ""
    entries, before_added, after_added = [], [], []
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = _rel(root, item.get("path") or "")
        if not rel:
            continue
        before, after = item.get("before"), item.get("after")
        entry = {"path": rel,
                 "before": _hash_blob(repo, before) if before is not None else "",
                 "after": _hash_blob(repo, after) if after is not None else ""}
        entries.append(entry)
        if entry["before"]:
            before_added.append((rel, entry["before"]))
        if entry["after"]:
            after_added.append((rel, entry["after"]))
    meta = json.dumps({"tag": tag, "turn": turn, "label": str(label or "")[:200],
                       "ts": int(time.time()), "files": entries},
                      ensure_ascii=False).encode("utf-8")
    meta_sha = _hash_blob(repo, meta)
    if not meta_sha:
        return ""

    with _LOCK:
        try:
            index = os.path.join(repo, "index.checkpoint")
            try:
                os.remove(index)
            except OSError:
                pass
            env_extra = {"GIT_INDEX_FILE": index}
            for rel, sha in before_added:
                r = _run(repo, ["update-index", "--add", "--cacheinfo",
                                "100644,%s,before/%s" % (sha, rel)], env_extra=env_extra)
                if r.returncode != 0:
                    raise OSError((r.stderr or b"").decode("utf-8", "replace")[:200])
            for rel, sha in after_added:
                r = _run(repo, ["update-index", "--add", "--cacheinfo",
                                "100644,%s,content/%s" % (sha, rel)], env_extra=env_extra)
                if r.returncode != 0:
                    raise OSError((r.stderr or b"").decode("utf-8", "replace")[:200])
            r = _run(repo, ["update-index", "--add", "--cacheinfo",
                            "100644,%s,meta.json" % meta_sha], env_extra=env_extra)
            if r.returncode != 0:
                raise OSError((r.stderr or b"").decode("utf-8", "replace")[:200])
            r = _run(repo, ["write-tree"], env_extra=env_extra)
            if r.returncode != 0:
                raise OSError((r.stderr or b"").decode("utf-8", "replace")[:200])
            tree = r.stdout.decode("ascii", "replace").strip()
            parent = _resolve(repo, "refs/heads/main")
            args = ["commit-tree", tree]
            if parent:
                args += ["-p", parent]
            args += ["-m", tag]
            r = _run(repo, args)
            if r.returncode != 0:
                raise OSError((r.stderr or b"").decode("utf-8", "replace")[:200])
            commit = r.stdout.decode("ascii", "replace").strip()
            _run(repo, ["update-ref", "refs/tags/" + tag, commit])
            _run(repo, ["update-ref", "refs/heads/main", commit])
            try:
                os.remove(index)
            except OSError:
                pass
            return commit
        except Exception as e:                    # noqa: BLE001 快照失败不能影响对话
            _warn_once("checkpoint", "[快照] 写入检查点失败（%s）：%s" % (tag, e))
            return ""


def list_tags(root):
    """列出某个工作区的检查点 tag（测试 / 排查用）。"""
    if not root or not available():
        return []
    repo = repo_dir(root)
    if not os.path.isdir(os.path.join(repo, "objects")):
        return []
    r = _run(repo, ["tag", "-l"])
    if r.returncode != 0:
        return []
    return [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x]


def read_file(root, tag, rel, which="before"):
    """读取某个检查点里某文件的原始字节（which=before|content）；读不到返回 None。"""
    if not root or not tag or not rel or not available():
        return None
    repo = repo_dir(root)
    sub = "content" if which == "content" else "before"
    r = _run(repo, ["show", "%s:%s/%s" % (tag, sub, str(rel).replace("\\", "/"))])
    if r.returncode != 0:
        return None
    return r.stdout
