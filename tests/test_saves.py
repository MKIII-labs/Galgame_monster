# -*- coding: utf-8 -*-
"""会话存档：回退 / 分叉 / 上限。
防回归：branch() 先拷贝 RESTORE_FIELDS（含 title）会覆盖分叉标题 —— 必须「先拷贝、后写 id/title」。"""
import pytest

from app import saves


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(saves, "SAVES_DIR", str(tmp_path))
    return tmp_path


def _sess():
    return {"id": "s1", "title": "原会话",
            "messages": [{"role": "user", "content": "hi"}],
            "kb_ids": [], "gal_bg": "", "gal_recap": "", "gal_enabled": True}


def test_save_then_restore_roundtrip(isolate):
    s = _sess()
    it = saves.add_save("s1", s, label="开局", manual=True)
    assert it["id"].startswith("sv_")

    # 改动会话，再回退：快照里存在的字段都应还原
    s["gal_bg"] = "bg_x.jpg"
    s["messages"].append({"role": "assistant", "content": "yo"})
    s["gal_recap"] = "后来的提要"

    ok, err = saves.restore({}, s, it["id"])
    assert ok and err == ""
    assert s["gal_bg"] == ""
    assert s["gal_recap"] == ""
    assert len(s["messages"]) == 1


def test_restore_clears_field_absent_from_snapshot(isolate):
    """防回归：快照里没有的字段，回退后不能残留「未来才产生的」值。

    两种收场：不在 ensure_session_kb_fields 的 setdefault 列表里 → 字段直接消失；
    在列表里 → 被重置成默认值。两者都不该留着未来值。
    """
    s = _sess()
    s.pop("gal_recap")                    # 快照时还没有这个字段
    it = saves.add_save("s1", s, label="开局", manual=True)
    s["gal_recap"] = "快照之后才有的"
    s["gal_auto_story"] = {"start": "a1", "nodes": [{"id": "a1"}], "title": "未来的剧本"}
    saves.restore({}, s, it["id"])
    assert "gal_recap" not in s
    assert s.get("gal_auto_story") == {}          # 重置为默认空剧本，未来值不残留


def test_branch_title_not_overwritten(isolate):
    data = {"sessions": [], "current": ""}
    s = _sess()
    data["sessions"].append(s)
    it = saves.add_save("s1", s, label="开局", manual=True)

    new_sid, err = saves.branch(data, s, it["id"])
    assert err == "" and new_sid and new_sid != "s1"
    ns = next(x for x in data["sessions"] if x["id"] == new_sid)
    assert ns["title"] != "原会话", "分叉标题被快照里的 title 覆盖了"
    assert "分支" in ns["title"]
    assert data["current"] == new_sid
    # 新线要有自己的「分支起点」档
    assert [x["label"] for x in saves.list_saves(new_sid)] == ["分支起点"]


def test_restore_unknown_id(isolate):
    ok, err = saves.restore({}, _sess(), "not-exist")
    assert not ok and err


def test_list_saves_sorted_desc(isolate):
    s = _sess()
    a = saves.add_save("s1", s, label="a", manual=True)
    b = saves.add_save("s1", s, label="b", manual=True)
    ids = [x["id"] for x in saves.list_saves("s1")]
    assert ids[0] == b["id"] and ids[-1] == a["id"]


def test_trim_keeps_earliest_anchor(isolate, monkeypatch):
    monkeypatch.setattr(saves, "SAVE_MAX", 4)
    s = _sess()
    first = saves.add_save("s1", s, label="开局", manual=True)
    for i in range(6):
        saves.add_save("s1", s, label="第%d" % i)
    items = saves.list_saves("s1")
    assert len(items) == 4
    assert first["id"] in [x["id"] for x in items], "最早一个要留作「开局」锚点"


def test_delete_save(isolate):
    s = _sess()
    it = saves.add_save("s1", s, label="x", manual=True)
    saves.delete_save("s1", it["id"])
    assert saves.list_saves("s1") == []


def test_branch_at_slices_messages(isolate):
    """按消息位置分叉：只保留前 upto 条，原会话不受影响。"""
    data = {"sessions": [], "current": ""}
    s = _sess()
    s["messages"] = [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"},
                     {"role": "user", "content": "q2"}]
    data["sessions"].append(s)

    new_sid, err = saves.branch_at(data, s, 2)
    assert err == "" and new_sid and new_sid != "s1"
    ns = next(x for x in data["sessions"] if x["id"] == new_sid)
    assert [m["content"] for m in ns["messages"]] == ["q1", "a1"]
    assert "分支" in ns["title"]
    assert data["current"] == new_sid
    assert [x["label"] for x in saves.list_saves(new_sid)] == ["分支起点"]
    assert len(s["messages"]) == 3          # 原会话原样保留


def test_branch_at_rejects_bad_upto(isolate):
    data = {"sessions": [], "current": ""}
    s = _sess()
    data["sessions"].append(s)
    assert saves.branch_at(data, s, 0)[0] is None
    assert saves.branch_at(data, s, "x")[0] is None
    assert saves.branch_at(data, s, 999)[0] is not None   # 超界按总长度处理


def test_branch_at_clears_summary_beyond_cut(isolate):
    """摘要点超过切片长度时要清空，否则新线会引用不存在的历史。"""
    data = {"sessions": [], "current": ""}
    s = _sess()
    s["messages"] = [{"role": "user", "content": "q1"},
                     {"role": "assistant", "content": "a1"}]
    s["summary"] = "旧摘要"
    s["summary_upto"] = 2
    data["sessions"].append(s)

    new_sid, _ = saves.branch_at(data, s, 1)
    ns = next(x for x in data["sessions"] if x["id"] == new_sid)
    assert ns["summary"] == "" and ns["summary_upto"] == 0


def test_save_writes_per_savepoint_file(isolate):
    """防回归：不能再写「一个会话一个大文件」，否则每轮收尾都要重写全量快照。"""
    s = _sess()
    it = saves.add_save("s1", s, label="开局", manual=True)
    assert (isolate / "s1.json").exists() is False
    assert (isolate / "s1" / "index.json").exists()
    assert (isolate / "s1" / ("%s.json" % it["id"])).exists()


def test_trim_removes_state_files(isolate, monkeypatch):
    monkeypatch.setattr(saves, "SAVE_MAX", 3)
    s = _sess()
    first = saves.add_save("s1", s, label="开局", manual=True)
    old = saves.add_save("s1", s, label="a")
    saves.add_save("s1", s, label="b")
    newest = saves.add_save("s1", s, label="c")
    kept = [x["id"] for x in saves.list_saves("s1")]
    assert len(kept) == 3
    assert first["id"] in kept and newest["id"] in kept
    assert not (isolate / "s1" / ("%s.json" % old["id"])).exists()


def test_legacy_file_reappearing_is_absorbed(isolate):
    """旧版本进程晚一步收尾、又把单文件写回来时：新档要并进来，旧文件删掉。"""
    import json
    s = _sess()
    saves.add_save("s1", s, label="新存储的档", manual=True)
    (isolate / "s1.json").write_text(json.dumps({"session_id": "s1", "items": [
        {"id": "sv_late", "ts": 2, "label": "旧进程补的", "manual": False, "msg_count": 1,
         "state": {"title": "旧档", "messages": []}},
    ]}, ensure_ascii=False), encoding="utf-8")

    labels = [x["label"] for x in saves.list_saves("s1")]
    assert "旧进程补的" in labels
    assert not (isolate / "s1.json").exists()
    assert (isolate / "s1" / "sv_late.json").exists()


def test_legacy_single_file_migrated(isolate):
    """旧版单文件要能自动拆开，且拆完后仍可回退。"""
    import json
    legacy = isolate / "s1.json"
    legacy.write_text(json.dumps({"session_id": "s1", "items": [
        {"id": "sv_old", "ts": 1, "label": "开局", "manual": True, "msg_count": 1,
         "state": {"title": "旧档", "messages": [{"role": "user", "content": "hi"}]}},
    ]}, ensure_ascii=False), encoding="utf-8")

    items = saves.list_saves("s1")
    assert [x["id"] for x in items] == ["sv_old"]
    assert not legacy.exists()                      # 迁移后旧文件删掉，避免再被整份重写
    assert (isolate / "s1" / "sv_old.json").exists()

    s = _sess()
    s["title"] = "改过了"
    ok, err = saves.restore({}, s, "sv_old")
    assert ok and s["title"] == "旧档"
