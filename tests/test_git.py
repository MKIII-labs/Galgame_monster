# -*- coding: utf-8 -*-
"""Git 封装：状态解析、相对路径、diff 形状、暂存 / 提交。"""
import subprocess

import pytest

from app import git


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false"] + list(args),
        cwd=str(root), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@pytest.fixture()
def repo(tmp_path):
    if not _git_available():
        pytest.skip("本机没有 git")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _git_available():
    try:
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def test_status_on_plain_dir(tmp_path):
    r = git.status(str(tmp_path))
    assert r["ok"] is False
    assert "不是 Git 仓库" in r["reason"] or r["reason"]


def test_status_lists_modified_and_untracked(repo):
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")
    r = git.status(str(repo))
    assert r["ok"] is True
    by_path = {f["path"]: f for f in r["files"]}
    assert by_path["a.txt"]["unstaged"] is True
    assert by_path["a.txt"]["staged"] is False
    assert by_path["new.txt"]["untracked"] is True


def test_diff_file_shape(repo):
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    d = git.diff_file(str(repo), "a.txt")
    assert d["ok"] is True and d["binary"] is False
    assert d["adds"] == 1 and d["dels"] == 1
    # Windows 下工作区是 CRLF，git show 可能给 LF；比较前统一换行
    assert d["old"].replace("\r\n", "\n").startswith("one\ntwo")
    assert d["new"].replace("\r\n", "\n").startswith("one\ntwo changed")
    # 与 /api/sandbox/diff 同构：前端可以直接复用 ideDiffDoc
    for key in ("hunks", "old_total", "new_total", "gap_after", "path"):
        assert key in d


def test_stage_and_unstage(repo):
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    git.stage(str(repo), ["a.txt"])
    f = {x["path"]: x for x in git.status(str(repo))["files"]}["a.txt"]
    assert f["staged"] is True and f["unstaged"] is False
    git.unstage(str(repo), ["a.txt"])
    f = {x["path"]: x for x in git.status(str(repo))["files"]}["a.txt"]
    assert f["staged"] is False and f["unstaged"] is True


def test_commit_and_log(repo):
    (repo / "a.txt").write_text("one\ntwo more\n", encoding="utf-8")
    git.stage(str(repo), ["a.txt"])
    r = git.commit(str(repo), "改第二行")
    assert r["ok"] is True and r["hash"]
    log = git.log(str(repo), 5)
    assert log["ok"] is True
    assert log["items"][0]["subject"] == "改第二行"
    assert git.status(str(repo))["files"] == []


def test_commit_requires_message(repo):
    with pytest.raises(git.GitError):
        git.commit(str(repo), "   ")


def test_patch_tracked_and_untracked(repo):
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    p = git.patch(str(repo), "a.txt")
    assert p["ok"] is True and p["binary"] is False
    assert "-two" in p["patch"] and "+two changed" in p["patch"]

    # 未跟踪文件：git diff 不输出，需要手工生成「新文件」补丁
    (repo / "new.txt").write_text("hello\nworld\n", encoding="utf-8")
    p2 = git.patch(str(repo), "new.txt")
    assert p2["ok"] is True
    assert "new file mode" in p2["patch"]
    assert "+hello" in p2["patch"] and "+world" in p2["patch"]

    # 暂存区
    git.stage(str(repo), ["a.txt"])
    p3 = git.patch(str(repo), "a.txt", staged=True)
    assert "+two changed" in p3["patch"]


def test_show_commit(repo):
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    r = git.show_commit(str(repo), head)
    assert r["ok"] is True
    assert r["short"] and r["subject"] == "init"
    assert "a.txt" in r["patch"]
    assert r["adds"] >= 2
    # 只接受十六进制提交号，避免把任意参数传给 git
    with pytest.raises(git.GitError):
        git.show_commit(str(repo), "HEAD --upload-pack=evil")


def test_diff_text_work_includes_untracked(repo):
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")
    d = git.diff_text(str(repo), "work")
    assert d["ok"] is True and d["scope"] == "work"
    assert "-two" in d["text"] and "+two changed" in d["text"]
    assert "new.txt" in d["files"] and "+hello" in d["text"]


def test_diff_text_commit_and_branch(repo):
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    d = git.diff_text(str(repo), "commit", rev=head)
    assert d["ok"] is True and "a.txt" in d["text"]

    cur = git.branches(str(repo))["current"]
    git.branch_create(str(repo), "feat")
    git.checkout(str(repo), "feat")
    (repo / "a.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "--", "a.txt")
    _git(repo, "commit", "-q", "-m", "feat")
    # 切回主分支后比较：base 用分支名，避免把仓库状态搞乱
    assert git.checkout(str(repo), cur)["ok"] is True
    b = git.diff_text(str(repo), "branch", base=cur, head="feat")
    assert b["ok"] is True and "feature" in b["text"]
    assert b["label"] == "%s...feat" % cur


def test_diff_text_rejects_bad_rev(repo):
    with pytest.raises(git.GitError):
        git.diff_text(str(repo), "commit", rev="HEAD --upload-pack=evil")
    with pytest.raises(git.GitError):
        git.diff_text(str(repo), "branch", base="--upload-pack=evil")


def test_commit_patch_prefers_staged(repo):
    (repo / "a.txt").write_text("one\ntwo staged\n", encoding="utf-8")
    git.stage(str(repo), ["a.txt"])
    d = git.commit_patch(str(repo))
    assert d["ok"] is True and d["staged"] is True
    assert "+two staged" in d["text"]
    assert d["truncated"] is False


def test_sync_push_fetch_pull_and_upstream(repo, tmp_path_factory):
    bare = tmp_path_factory.mktemp("remote") / "origin.git"
    _git(repo, "init", "--bare", "-q", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    cur = git.branches(str(repo))["current"]

    # 第一次 push 没有上游：sync 会自动补 -u
    assert git.sync(str(repo), "push")["ok"] is True
    assert git.status(str(repo))["upstream"] == "origin/" + cur
    assert git.sync(str(repo), "fetch")["ok"] is True
    assert git.sync(str(repo), "pull")["ok"] is True
    assert git.status(str(repo))["remotes"] == ["origin"]


def test_sync_rejects_unknown_action(repo):
    with pytest.raises(git.GitError):
        git.sync(str(repo), "delete")


def test_tags_create_list_delete(repo):
    assert git.tag_create(str(repo), "v1")["ok"] is True
    items = git.tags(str(repo))["items"]
    assert [t["name"] for t in items] == ["v1"]
    assert git.tag_delete(str(repo), "v1")["ok"] is True
    assert git.tags(str(repo))["items"] == []
    with pytest.raises(git.GitError):
        git.tag_create(str(repo), "--upload-pack=evil")


def test_graph_log_has_graph_column(repo):
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "--", "a.txt")
    _git(repo, "commit", "-q", "-m", "second")
    r = git.graph_log(str(repo), 10)
    assert r["ok"] is True and len(r["items"]) >= 2
    assert r["items"][0]["subject"] == "second"
    assert all("graph" in it for it in r["items"])
    assert "*" in "".join(it["graph"] for it in r["items"])



# ---------------------------------------------------------------- 前端管理操作

def test_discard_tracked_and_untracked(repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    r = git.discard(str(repo), ["a.txt", "new.txt"])
    assert r["ok"] is True and r["tracked"] == 1 and r["untracked"] == 1
    assert (repo / "a.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "one\ntwo\n"
    assert not (repo / "new.txt").exists()


def test_rm_files_stages_deletion(repo):
    assert git.rm_files(str(repo), ["a.txt"])["ok"] is True
    assert not (repo / "a.txt").exists()
    st = git.status(str(repo))
    assert any(f["path"] == "a.txt" and f["staged"] for f in st["files"])


def test_clean_removes_only_untracked(repo):
    (repo / "junk.txt").write_text("x", encoding="utf-8")
    assert git.clean(str(repo))["ok"] is True
    assert not (repo / "junk.txt").exists()
    assert (repo / "a.txt").exists()


def test_reset_soft_keeps_worktree(repo):
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "--", "a.txt")
    _git(repo, "commit", "-q", "-m", "second")
    assert git.reset(str(repo), "HEAD~1", "soft")["ok"] is True
    assert (repo / "a.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "two\n"
    st = git.status(str(repo))
    assert any(f["path"] == "a.txt" and f["staged"] for f in st["files"])


def test_reset_rejects_bad_rev(repo):
    with pytest.raises(git.GitError):
        git.reset(str(repo), "--hard; rm -rf /", "soft")


def test_revert_creates_reverse_commit(repo):
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "--", "a.txt")
    _git(repo, "commit", "-q", "-m", "second")
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True).strip()
    assert git.revert(str(repo), head)["ok"] is True
    assert "one" in (repo / "a.txt").read_text(encoding="utf-8")


def test_amend_changes_message(repo):
    assert git.amend(str(repo), "init amended")["ok"] is True
    assert git.log(str(repo), 1)["items"][0]["subject"] == "init amended"


def test_branch_create_checkout_rename_delete(repo):
    cur = git.branches(str(repo))["current"]
    assert git.branch_create(str(repo), "feat")["ok"] is True
    assert any(b["name"] == "feat" for b in git.branches(str(repo))["items"])
    assert git.checkout(str(repo), "feat")["ok"] is True
    assert git.branches(str(repo))["current"] == "feat"
    assert git.branch_rename(str(repo), "feat", "feat2")["ok"] is True
    assert git.checkout(str(repo), cur)["ok"] is True
    assert git.branch_delete(str(repo), "feat2")["ok"] is True


def test_branch_rejects_unsafe_name(repo):
    with pytest.raises(git.GitError):
        git.branch_create(str(repo), "-D")
    with pytest.raises(git.GitError):
        git.branch_create(str(repo), "../evil")


def test_stash_roundtrip(repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    assert git.stash(str(repo), "push", "wip")["ok"] is True
    assert "one" in (repo / "a.txt").read_text(encoding="utf-8")
    assert git.stash(str(repo), "pop")["ok"] is True
    assert "changed" in (repo / "a.txt").read_text(encoding="utf-8")


def test_path_escape_rejected(repo):
    with pytest.raises(git.GitError):
        git.diff_file(str(repo), "../outside.txt")
    with pytest.raises(git.GitError):
        git.stage(str(repo), ["/etc/passwd"])


def test_subdir_sandbox_reports_relative_paths(repo):
    """沙箱只是仓库子目录时，路径要按沙箱出，不能按仓库根出。"""
    sub = repo / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("x\n", encoding="utf-8")
    r = git.status(str(sub))
    assert r["ok"] is True
    assert [f["path"] for f in r["files"]] == ["b.txt"]


def test_binary_file_not_diffed(repo):
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    git.stage(str(repo), ["bin.dat"])
    d = git.diff_file(str(repo), "bin.dat", staged=True)
    assert d["binary"] is True
    assert d["hunks"] == []


class _FakeHandler:
    """只实现 /api/git/* 用到的那几个方法。"""

    def __init__(self, body, sandbox):
        self._in = body
        self._sandbox = sandbox
        self.calls = []

    def _body(self):
        return self._in

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def _sb_guard(self, body):
        return {"id": "s1"}, self._sandbox, None


def test_api_routes_registered_and_status(repo):
    import app.api.git as git_api          # noqa: F401  导入即注册
    from app.registry import snapshot

    routes = snapshot()
    for path in ("/api/git/status", "/api/git/diff", "/api/git/stage",
                 "/api/git/unstage", "/api/git/commit", "/api/git/log",
                 "/api/git/message", "/api/git/sync", "/api/git/tag",
                 "/api/git/graph"):
        assert ("POST", path) in routes

    (repo / "a.txt").write_text("one\ntwo api\n", encoding="utf-8")
    h = _FakeHandler({"session_id": "s1"}, str(repo))
    out = git_api._api_git_status(h)
    assert out["ok"] is True
    assert [f["path"] for f in out["files"]] == ["a.txt"]


def test_api_ide_review_git_scope(repo, monkeypatch):
    """Git 范围审查走同一条 /api/ide/review，只回摘要不写文件。"""
    import app.api.sandbox as sb_api
    from app import ide_review

    monkeypatch.setattr(ide_review, "get_api_conf", lambda kind: ("k", "u", "m"))
    monkeypatch.setattr(ide_review, "_quick_chat",
                        lambda *a, **kw: '{"summary": "改了 a.txt", "risks": [], "flow": ""}')
    (repo / "a.txt").write_text("one\ntwo changed\n", encoding="utf-8")
    h = _FakeHandler({"session_id": "s1", "scope": "git", "mode": "work"}, str(repo))
    out = sb_api._api_ide_review(h)
    assert out["ok"] is True and out["summary"] == "改了 a.txt"
    assert "a.txt" in out["files"]
    assert (repo / "a.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == \
        "one\ntwo changed\n"

