# -*- coding: utf-8 -*-
"""本地 worktree 隔离：创建 / 带脏状态 / 补丁回写 / 冲突 / 非 git 回退。"""
import os
import subprocess

import pytest

from app import agent, explore, worktree


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个带提交 + 未提交改动 + 未跟踪文件的临时 git 仓库。"""
    monkeypatch.setattr(worktree, "DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    os.makedirs(root / "sub")
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "a@b.c")
    _git(str(root), "config", "user.name", "t")
    (root / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
    _git(str(root), "add", ".")
    _git(str(root), "commit", "-qm", "init")
    (root / "sub" / "a.txt").write_text("a\ndirty\n", encoding="utf-8")
    (root / "sub" / "u.txt").write_text("untracked\n", encoding="utf-8")
    return root


def test_create_carries_dirty_and_untracked(repo):
    wt = worktree.create(str(repo), "a1", batch="t")
    assert wt.ok, wt.error
    assert wt.rel == ""
    assert (repo / "sub" / "a.txt").read_text(encoding="utf-8") == "a\ndirty\n"
    # 子代理看到的是「现在的代码」：未提交改动 + 未跟踪文件都在
    assert open(os.path.join(wt.sandbox, "sub", "a.txt"), encoding="utf-8").read() \
        == "a\ndirty\n"
    assert open(os.path.join(wt.sandbox, "sub", "u.txt"), encoding="utf-8").read() \
        == "untracked\n"

    # 基线之后只算子代理的改动
    with open(os.path.join(wt.sandbox, "sub", "a.txt"), "a", encoding="utf-8") as f:
        f.write("agent\n")
    os.makedirs(os.path.join(wt.sandbox, "new"), exist_ok=True)
    with open(os.path.join(wt.sandbox, "new", "n.txt"), "w", encoding="utf-8") as f:
        f.write("n\n")
    files = worktree.changes(wt)
    assert [(f["path"], f["status"]) for f in files] == [
        ("new/n.txt", "added"), ("sub/a.txt", "modified")]
    data, err = worktree.patch(wt)
    assert err == "" and data
    assert os.path.isfile(wt.patch_path)

    ok, cerr = worktree.apply_check(str(repo), data)
    assert ok, cerr
    ok, aerr = worktree.apply(str(repo), data)
    assert ok, aerr
    assert (repo / "sub" / "a.txt").read_text(encoding="utf-8") == "a\ndirty\nagent\n"
    assert (repo / "new" / "n.txt").read_text(encoding="utf-8") == "n\n"

    worktree.remove(wt)
    assert not os.path.isdir(wt.path)


def test_apply_conflict_is_detected(repo):
    wt = worktree.create(str(repo), "a1", batch="t")
    with open(os.path.join(wt.sandbox, "sub", "a.txt"), "a", encoding="utf-8") as f:
        f.write("agent\n")
    data, _ = worktree.patch(wt)
    # 主沙箱在子代理运行期间又被改了一次：补丁不能干净落地
    (repo / "sub" / "a.txt").write_text("user edit\n", encoding="utf-8")
    ok, cerr = worktree.apply_check(str(repo), data)
    assert not ok and cerr
    worktree.remove(wt)


def test_non_git_and_empty_repo_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "DATA_DIR", str(tmp_path / "data"))
    plain = tmp_path / "plain"
    os.makedirs(plain)
    bad = worktree.create(str(plain), "x", batch="t")
    assert not bad.ok and "不是 git 仓库" in bad.error

    empty = tmp_path / "empty"
    os.makedirs(empty)
    _git(str(empty), "init", "-q")
    bad2 = worktree.create(str(empty), "x", batch="t")
    assert not bad2.ok and "还没有任何提交" in bad2.error


def test_subdir_sandbox_maps_back(repo):
    wt = worktree.create(str(repo / "sub"), "a1", batch="t")
    assert wt.ok, wt.error
    assert wt.rel == "sub"
    assert os.path.isdir(wt.sandbox)
    with open(os.path.join(wt.sandbox, "a.txt"), "a", encoding="utf-8") as f:
        f.write("agent\n")
    files = worktree.changes(wt)
    assert [(f["path"], f["status"]) for f in files] == [("a.txt", "modified")]
    assert files[0]["repo_path"] == "sub/a.txt"
    worktree.remove(wt)


def test_worktree_enabled_modes():
    hooks = {"worktree_write_tool": lambda sb: None,
             "worktree_apply": lambda info: None, "sandbox": "x"}
    off = {"worktree_isolation": "off"}
    auto = dict(hooks, worktree_isolation="auto")
    always = dict(hooks, worktree_isolation="always")
    assert explore._worktree_enabled(off, 3) is False
    assert explore._worktree_enabled(auto, 1) is False     # 单写者没冲突
    assert explore._worktree_enabled(auto, 2) is True
    assert explore._worktree_enabled(always, 1) is True
    # 没有回调（非 chat 链路 / 测试环境）时不隔离
    assert explore._worktree_enabled({"worktree_isolation": "always"}, 2) is False


def test_can_write_matches_roles():
    ctx = {"allow_write": True}
    assert explore._can_write({"role": "worker"}, ctx) is True
    assert explore._can_write({"role": "reader"}, ctx) is False
    ctx = {"allow_write": False, "roles": {"w": {"tools": ["write_file"],
                                                 "allow_write": True}}}
    assert explore._can_write({"role": "w"}, ctx) is True


def test_isolated_agents_write_their_own_copy_and_apply(repo):
    """两个可写子代理各写一份 worktree：主沙箱在回写前不变，回写后两边改动都在。"""
    seen = []

    def chat_fn(ctx, messages, schemas, stop_event, model=None):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "【工具结果】" not in text and "工具执行结果" not in text:
            name = "mine_%s.txt" % ("1" if "一号" in text else "2")
            return {"visible": "", "tool_calls": [
                {"tool": "write_file", "args": {"path": name, "content": "hi\n"}}]}
        return {"visible": "完成（%s:1）" % ("./mine_1.txt" if "一号" in text
                                            else "./mine_2.txt"),
                "tool_calls": []}

    def write_factory(sb):
        def _w(name, args):
            out, _undo, _wr = agent.run_tool(sb, name, args)
            return not out.lstrip().startswith("❌"), out
        return _w

    def apply_fn(info):
        seen.append([f["path"] for f in info["files"]])
        ok, err = worktree.apply(info["sandbox"], info["patch"])
        return {"ok": ok, "applied": ok, "undo": "t"} if ok else {
            "ok": False, "applied": False, "error": err}

    base = {
        "sandbox": str(repo), "api_key": "k", "api_url": "http://x", "model": "m",
        "goal": "各写一个文件", "chat_fn": chat_fn, "roles": {},
        "schemas": [], "allow_write": True, "write_tool": write_factory(str(repo)),
        "worktree_write_tool": write_factory, "worktree_apply": apply_fn,
        "worktree_enabled": True, "verify_evidence": False,
        "write_count": [0], "write_conflicts": [0], "write_batch": "b1",
        "emit": lambda o: None,
    }
    rs = [explore._run_one({"id": q, "role": "worker", "ask": q, "scope": [],
                            "hint_files": []}, base, explore.ReadCache(str(repo)),
                           explore.Budget(0), lambda o: None, None)
          for q in ("一号", "二号")]
    explore._settle_worktrees(rs, base, lambda o: None)
    assert all(r["ok"] for r in rs), [r.get("error") for r in rs]
    assert all(r.get("worktree", {}).get("applied") for r in rs)
    assert sorted(p for row in seen for p in row) == ["mine_1.txt", "mine_2.txt"]
    assert (repo / "mine_1.txt").read_text(encoding="utf-8") == "hi\n"
    assert (repo / "mine_2.txt").read_text(encoding="utf-8") == "hi\n"
    # 工作区用完即删：批次目录下只剩补丁文件，没有残留工作区
    batch_dir = os.path.join(worktree.DATA_DIR, "worktrees", worktree._safe("b1"))
    left = [n for n in os.listdir(batch_dir) if os.path.isdir(os.path.join(batch_dir, n))]
    assert left == []
