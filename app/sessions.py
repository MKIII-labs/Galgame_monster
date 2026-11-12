# -*- coding: utf-8 -*-
"""会话读写、精简与旧格式解析"""
import os
import re
import time

from app.store import (SESSION_FILE, LEGACY_HISTORY, DATA_DIR, read_json, write_json,
                       drop_cache, new_id)

# 「当前会话」单独存一个几十字节的小文件：切会话时只改它，
# 不再为了一个 current 字段把 20MB 的 sessions.json 全量重写（实测 230ms）。
CURRENT_FILE = os.path.join(DATA_DIR, "current_session.json")
# 切会话/首屏只回最近这么多条消息；更早的用 /api/session/messages 按需拉。
MSG_WINDOW = 80
# 每个会话最多保存多少个「提问标记」：悬浮按钮太多会挡住聊天区
MAX_QMARKS = 20
# 单会话快照目录（跟 SESSION_FILE 同目录：测试把 SESSION_FILE 指到临时目录时也隔离）
STORE_DIRNAME = "session_store"
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
_SAVED_AT_RE = re.compile(r'"saved_at"\s*:\s*([0-9]+(?:\.[0-9]+)?)')


def _safe_sid(sid):
    return _SID_RE.sub("_", str(sid or "").strip())[:80]


def _store_dir():
    return os.path.join(os.path.dirname(SESSION_FILE), STORE_DIRNAME)


def _store_path(sid):
    return os.path.join(_store_dir(), _safe_sid(sid) + ".json")


def ensure_session_kb_fields(s):
    s.setdefault("memory", {"nodes": [], "edges": []})
    s.setdefault("kb_ids", [])
    s.setdefault("ai_write", True)
    s.setdefault("use_profile", True)          # 本会话是否读取用户画像（默认开，可在知识库面板按会话关）
    s.setdefault("use_memory", True)           # 本会话是否读取会话记忆 / 绑定知识库（默认开）
    s.setdefault("agent_enabled", False)   # 是否开启「文件 Agent」模式
    s.setdefault("sandbox", "")            # Agent 的沙箱目录（绝对路径）
    s.setdefault("allow_outside_read", False)  # 允许 Agent 读取沙箱外的文件（只读）
    s.setdefault("auto_chat", False)           # 主动聊天：AI 隔一段时间主动发消息
    s.setdefault("scene", "general")           # 场景包：general | office | study | life
    # ---- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）----
    s.setdefault("gal_enabled", False)      # 该会话是否以 GAL 模式对话
    # 记忆范围：main = Solo/IDE 共用；gal = 剧场专用（独立记忆，不继承主会话）
    s.setdefault("gal_scope", "main")
    # 剧场可以从全局记忆里「读」什么（默认全关；写入始终只进本会话）
    s.setdefault("gal_use_profile", False)  # 读用户画像
    s.setdefault("gal_use_kb", False)       # 读本会话绑定的知识库
    s.setdefault("gal_use_rules", False)    # 读全局规则（角色 / 文风 / 约束）
    s.setdefault("gal_character_id", "")    # 当前角色 id
    s.setdefault("gal_bg", "")              # 当前舞台背景（相对路径）
    s.setdefault("gal_bgm", "")             # 当前舞台 BGM（相对路径）
    s.setdefault("gal_stats", [])           # 当前舞台状态参数
    s.setdefault("gal_map", {})             # 当前舞台方位（{place, marks:[{name,pos}]}）
    s.setdefault("gal_node_id", "")         # 剧情节点（P4 使用）
    s.setdefault("gal_round", 0)            # 当前节点内回合数（P4 使用）
    # ---- GAL AI 自动写剧本（每个会话独立保存 AI 生成的剧本）----
    s.setdefault("gal_auto_on", False)      # 该会话是否启用 AI 自动剧本
    s.setdefault("gal_auto_story", {})      # AI 生成的剧本（结构与 story.json 一致）
    s.setdefault("gal_auto_outline", "")    # 全剧走向摘要（注入用）
    s.setdefault("gal_auto_acts_done", 0)   # 已演出完成的幕数
    if not isinstance(s.get("memory"), dict):
        s["memory"] = {"nodes": [], "edges": []}
    s["memory"].setdefault("nodes", [])
    s["memory"].setdefault("edges", [])
    s["memory"].setdefault("families", [])
    s["memory"].setdefault("layout_mode", "stable")
    s["memory"].setdefault("layout_version", 1)
    for node in s["memory"].get("nodes") or []:
        if isinstance(node, dict):
            node.setdefault("notes", [])


MAX_COMPRESS_MARKS = 50      # 压缩标记最多保留多少条（避免会话无限膨胀）


def mark_key(m):
    """压缩标记的稳定键：一个压缩点（upto）只应存在一条标记。

    `at` 来自服务器时钟，跨进程 / 跨设备会漂移，不能当唯一键；
    `upto` 是本次压缩推进到的消息位置，天然单调递增。
    """
    if not isinstance(m, dict):
        return ""
    try:
        upto = int(m.get("upto") or 0)
    except (TypeError, ValueError):
        upto = 0
    if upto > 0:
        return "u%d" % upto
    return "a%s" % (m.get("at") or "")


def _dedupe_compress_marks(marks):
    """同一个压缩点只保留一条（按 key 去重，保留 index 更大的那条）。

    历史版本在写 summary_at 之后才补旧标记，导致首次压缩会同时留下
    「补的旧标记（index=upto）」和「真实标记（index=len(msgs)）」，
    两条 at 相同；前端按 at 去重时会选中靠前的那条，分隔线因此画在错误位置。
    现在改为按 upto 去重，同时兼容两条都没 upto 的旧数据（退回按 at）。
    """
    out, seen = [], {}
    for m in marks or []:
        if not isinstance(m, dict):
            continue
        k = mark_key(m)
        if not k:
            out.append(m)
            continue
        pos = seen.get(k)
        if pos is None:
            seen[k] = len(out)
            out.append(m)
        elif int(m.get("index") or 0) > int(out[pos].get("index") or 0):
            out[pos] = m
    return out


def invalidate_compression(s):
    """作废压缩：摘要已经无法与当前消息对齐（编辑 / 删除落在压缩区内）。

    原文一直保存在会话里，清掉摘要后模型会重新看到原文；返回被作废的压缩条数。
    """
    cut = int(s.get("summary_upto") or 0)
    s["summary"] = ""
    s["summary_upto"] = 0
    s["summary_at"] = 0
    s["summary_mem"] = []
    s["compress_marks"] = []
    return cut


def compress_marks_for(s):
    """回传压缩标记；旧会话没有 compress_marks 时用摘要字段补一条。"""
    marks = s.get("compress_marks")
    if isinstance(marks, list) and marks:
        return _dedupe_compress_marks(
            [m for m in marks[-MAX_COMPRESS_MARKS:] if isinstance(m, dict)])
    upto = int(s.get("summary_upto") or 0)
    if s.get("summary") and upto > 0:
        return [{"at": int(float(s.get("summary_at") or 0) * 1000),
                 "index": upto, "upto": upto, "count": upto}]
    return []


def trim_compress_marks(s, *, drop_after=None, shift_after=None):
    """消息截断 / 撤回后裁剪压缩标记，避免分隔线指向已删除的位置。"""
    marks = s.get("compress_marks")
    if not isinstance(marks, list):
        return
    total = len(s.get("messages") or [])
    out = []
    for m in marks:
        if not isinstance(m, dict):
            continue
        try:
            idx = int(m.get("index") or 0)
            upto = int(m.get("upto") or 0)
            at = int(m.get("at") or 0)
            count = int(m.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if drop_after is not None and idx > drop_after:
            continue
        if shift_after is not None and idx > shift_after:
            idx -= 1
        out.append({"at": at, "index": max(0, min(idx, total)),
                    "upto": max(0, min(upto, total)),
                    "count": max(0, count)})
    s["compress_marks"] = _dedupe_compress_marks(out)[-MAX_COMPRESS_MARKS:]


def trim_qmarks(s, *, drop_after=None, shift_after=None):
    """撤回 / 删除消息后同步「提问标记」的下标，避免悬浮按钮跳到错的提问。

    drop_after=X：删除下标 > X 的标记；shift_after=X：删除下标 X 本身后，
    比它大的标记整体前移一位（调用方先自行删掉 X 这一条）。
    """
    marks = s.get("qmarks")
    if not isinstance(marks, list):
        return
    out = []
    for m in marks:
        if not isinstance(m, dict):
            continue
        try:
            idx = int(m.get("idx"))
        except (TypeError, ValueError):
            continue
        if drop_after is not None and idx > drop_after:
            continue
        if shift_after is not None and idx > shift_after:
            idx -= 1
        if idx < 0:
            continue
        out.append({"idx": idx, "label": str(m.get("label") or "")[:40],
                    "text": str(m.get("text") or "")[:120],
                    "at": float(m.get("at") or 0)})
    by_idx = {}
    for m in out:
        by_idx[m["idx"]] = m          # 同一下标只留最后一条（新的覆盖旧的）
    dedup = [by_idx[k] for k in sorted(by_idx)]
    s["qmarks"] = dedup[-MAX_QMARKS:]


def parse_legacy_history(text):
    msgs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("你："):
            msgs.append({"role": "user", "content": line[2:]})
        elif line.startswith("AI："):
            msgs.append({"role": "assistant", "content": line[3:]})
    return msgs


_MERGED = False          # 本次进程是否已做过「事件流对账」


def reset_merge_flag():
    """测试用：允许重新做一次事件流对账。"""
    global _MERGED
    _MERGED = False


def _store_mode():
    """会话内容的真相源：events（默认）| json（旧行为，仅调试用）。"""
    try:
        from app.config import load_config
        return str(load_config().get("session_store") or "events")
    except Exception:
        return "json"


def _merge_events_once(data, skip=None):
    """启动后第一次加载：事件流比 sessions.json 新时，以事件流为准。

    为什么只在第一次：运行期间所有写入都会同时更新单会话快照与事件流；
    只有「上次进程崩溃 / 快照写失败」才会出现事件流领先，这时读一次即可。
    会话**列表**仍以 JSON 为准（删除会话后不会从事件流里复活）。

    skip：已经由单会话快照恢复过的会话（那份快照比事件流还新，不用再重放）。
    只有「事件流最后一条比 sessions.json 的 saved_at 新」的会话才重建 ——
    以前每个启动都要把所有会话的事件日志（实测 155MB）重建一遍，白花 1 秒多。
    """
    try:
        from app import session_events as se
    except Exception as e:
        print("[事件] 模块不可用，跳过对账：%s" % e)
        return
    skip = {str(x) for x in (skip or ())}
    try:
        agg_at = float(data.get("saved_at") or 0)
    except (TypeError, ValueError):
        agg_at = 0.0
    dirty = False
    for s in data.get("sessions") or []:
        sid = s.get("id")
        if not sid or str(sid) in skip:
            continue
        try:
            ts = se.latest_ts(sid)
        except Exception:                # noqa: BLE001  读不到就按「没有事件」处理
            ts = 0.0
        if ts <= 0:
            se.record(s)                 # 事件流缺失：补一条基线
            continue
        if ts <= agg_at:
            continue                     # 事件流没有比 sessions.json 更新的内容
        ev = se.rebuild(sid)
        if not ev:
            se.record(s)                 # 事件流缺失：补一条基线
            continue
        try:
            newer = float(ev.get("updated_at") or 0) > float(s.get("updated_at") or 0) + 1e-6
        except (TypeError, ValueError):
            newer = False
        if newer:
            _order = sid
            s.clear()
            s.update(ev)
            s["id"] = _order          # 事件流里的 id 可能被 safe 化过
            dirty = True
            print("[事件] 会话 %s 从事件流恢复（JSON 落后）" % sid)
    if dirty:
        data["saved_at"] = time.time()
        write_json(SESSION_FILE, data, indent=None)


# ---------------------------------------------------------------------------
# 单会话快照（热路径落库）
# ---------------------------------------------------------------------------

def _saved_at_of(path):
    """读快照的 saved_at：先只读文件头 256 字节（写入时 saved_at 在第一个字段），
    读不到才退回完整解析 —— 启动时不该为了比时间戳把几十 MB 的全解析一遍。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(256)
        m = _SAVED_AT_RE.search(head)
        if m:
            return float(m.group(1))
    except (OSError, ValueError):
        pass
    obj = read_json(path, None)
    drop_cache(path)
    try:
        return float((obj or {}).get("saved_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def save_session(s):
    """热路径落库：只写「这一条会话」的快照 + 事件流，不重写整份 sessions.json。

    为什么可以只写一条：sessions.json 是整份快照，写一次要把全部会话序列化
    （实测 74MB / 约 570ms，且持全局锁），而流式过程中每 15 秒就要落一次进度。
    改成每会话一个文件后只序列化变更的那一条（通常 10~150ms）。
    sessions.json 仍是权威快照，回合收尾与冷路径会写回来（`save_sessions`），
    所以外部脚本 / 备份读它是完整的，只是可能落后一个回合。
    快照里的 `saved_at` 用于启动时判断「这份快照是否比 sessions.json 新」。
    """
    if not isinstance(s, dict) or not s.get("id"):
        return
    path = _store_path(s["id"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # saved_at 必须是第一个字段：启动时靠文件头判断新旧，不解析整份文件
        write_json(path, {"saved_at": time.time(), "session": s},
                   indent=None, remember=False)
    except Exception as e:               # noqa: BLE001  快照失败不影响主流程
        print("[存储] 单会话快照失败：%s" % e)
    try:
        from app import session_events
        session_events.record(s)
    except Exception as e:               # noqa: BLE001
        print("[事件] %s" % e)


def _merge_store_snapshots(data):
    """把「比 sessions.json 新」的单会话快照并回来（进程中途退出时的热路径数据）。

    返回恢复过的会话 id 集合。过期 / 孤儿的快照顺手删掉，避免目录越积越大。
    """
    d = _store_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return set()
    try:
        agg_at = float(data.get("saved_at") or 0)
    except (TypeError, ValueError):
        agg_at = 0.0
    rows = data.get("sessions") or []
    index = {str(s.get("id") or ""): s for s in rows}
    recovered = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        ts = _saved_at_of(p)
        if ts <= agg_at:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        obj = read_json(p, None)
        drop_cache(p)                    # 快照只在启动时读一次，别常驻
        sess = (obj or {}).get("session")
        sid = str((sess or {}).get("id") or "")
        cur = index.get(sid)
        if not cur:
            # 会话列表以 sessions.json 为准：已删除的会话不因残留快照复活
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        cur.clear()
        cur.update(sess)
        recovered.add(sid)
    return recovered


def _mark_resume_pending(data):
    """把上次中断留下的 `_progress` 快照转成「可继续任务」状态。

    只在进程启动后第一次加载时做一次；正常结束的回合，收尾会删掉这条快照，
    所以这里命中的一定是「上次进程中途退出」。
    """
    dirty = False
    for s in data.get("sessions") or []:
        msgs = s.get("messages") or []
        if not msgs:
            continue
        last = msgs[-1]
        if last.get("role") == "assistant" and last.get("_progress"):
            last.pop("_progress", None)
            last["interrupted"] = True
            last["end_reason"] = {
                "kind": "resume",
                "text": "上次运行中途退出，任务未完成。可以点「继续任务」从断点接着做。",
                "action": "resume",
            }
            s["resume_pending"] = True
            dirty = True
    return dirty


def reap_orphan_progress(data, live_ids):
    """把「没有活跃流、但最后一条还是 _progress 快照」的会话转成可继续状态。

    以前只在进程启动时做；如果流线程异常退出而进程还活着，刷新页面会把
    半截快照显示成正常历史，看起来像「这一轮已经输出完成」。
    """
    live = {str(x) for x in (live_ids or ())}
    dirty = False
    for s in (data or {}).get("sessions") or []:
        sid = str(s.get("id") or "")
        if sid and sid in live:
            continue
        msgs = s.get("messages") or []
        if not msgs:
            continue
        last = msgs[-1]
        if not (isinstance(last, dict) and last.get("role") == "assistant"
                and last.get("_progress")):
            continue
        last.pop("_progress", None)
        last["interrupted"] = True
        last["end_reason"] = {
            "kind": "resume",
            "text": "后台输出已中断（连接或任务异常结束）。可以点「继续任务」接着做。",
            "action": "resume",
        }
        s["resume_pending"] = True
        dirty = True
    return dirty


def set_current(sid):
    """记住当前会话：只写一个小侧车文件（几十字节），不碰 sessions.json。"""
    sid = str(sid or "").strip()
    if not sid:
        return
    write_json(CURRENT_FILE, {"id": sid})


def _apply_current(data):
    """用侧车文件里的 current 覆盖 sessions.json 里的旧值。

    侧车文件在、且指向存在的会话时才生效；删除会话后残留的旧 id 会被忽略。
    """
    try:
        cur = str((read_json(CURRENT_FILE, {}) or {}).get("id") or "")
    except Exception:
        cur = ""
    if cur and any(s.get("id") == cur for s in (data.get("sessions") or [])):
        data["current"] = cur


def load_sessions():
    global _MERGED
    data = read_json(SESSION_FILE)
    if isinstance(data, dict) and data.get("sessions"):
        for s in data["sessions"]:
            s.setdefault("id", new_id())
            s.setdefault("title", "新对话")
            s.setdefault("updated_at", time.time())
            s.setdefault("messages", [])
            ensure_session_kb_fields(s)
        if not any(s["id"] == data.get("current") for s in data["sessions"]):
            data["current"] = data["sessions"][0]["id"]
        if not _MERGED:
            # 先置位、后执行时，任何一次瞬时 IO 失败（事件流读盘 / write_json）
            # 都会让本进程永久跳过对账；失败必须回滚标记，下次 load 再试一次。
            _MERGED = True
            try:
                # 1) 上次进程中途退出时热路径写下的单会话快照（比 sessions.json 新）
                recovered = _merge_store_snapshots(data)
                # 2) 没有快照可用的会话，再看事件流是否更新
                if _store_mode() == "events":
                    _merge_events_once(data, skip=recovered)
                if _mark_resume_pending(data):
                    # 走 save_sessions 而不是 write_json：这次变更也要进事件流
                    save_sessions(data)
            except Exception:
                _MERGED = False
                raise
        _apply_current(data)
        return data

    sessions = []
    if os.path.exists(LEGACY_HISTORY):
        try:
            with open(LEGACY_HISTORY, "r", encoding="utf-8") as f:
                legacy = f.read()
            if legacy.strip():
                sessions.append({"id": new_id(), "title": "历史记录",
                                 "updated_at": time.time(),
                                 "messages": parse_legacy_history(legacy)})
        except Exception:
            pass
    if not sessions:
        sessions.append({"id": new_id(), "title": "新对话", "updated_at": time.time(), "messages": []})
    data = {"current": sessions[0]["id"], "sessions": sessions}
    save_sessions(data)
    _apply_current(data)
    return data


def save_sessions(data):
    """冷路径落库：把整份会话快照写回 sessions.json（热路径用 `save_session`）。

    先记事件、后写快照：
    - 事件的时间戳一定早于快照里的 saved_at，启动对账时不会把刚写过的内容再重放一遍；
    - 快照写失败（Windows 占用 / 盘满）时，事件流仍能把它恢复出来。
    """
    if data is None:
        return
    # append-only 旁路事件：覆盖所有落盘路径（生成、压缩、改名、删除后的截断）。
    # 失败只打日志，绝不影响主流程；没变化的会话由签名快速跳过。
    try:
        from app import session_events
        session_events.record_all(data.get("sessions") or [])
    except Exception as e:
        print("[事件] %s" % e)
    # saved_at 标记这份快照的时间，启动时用来判断单会话快照是否更新（见 _merge_store_snapshots）
    data["saved_at"] = time.time()
    write_json(SESSION_FILE, data, indent=None)   # 只有程序读，紧凑格式写得更快、更小
    # current 同步到侧车文件（只在真的变了时写）：新建 / 删除 / 存档恢复
    # 都会改 current，不跟着同步的话下次 load 会被旧值覆盖。
    try:
        cur = str(data.get("current") or "")
        old = str((read_json(CURRENT_FILE, {}) or {}).get("id") or "")
        if cur and cur != old:
            set_current(cur)
    except Exception:
        pass


def flush_sessions(data=None):
    """把内存里的全量快照落盘一次（回合收尾 / 备份前）。

    热路径只在 `session_store/` 里写单会话快照；整份 sessions.json 靠这里跟上，
    外部脚本、备份、以及「直接打开 data/sessions.json 看」都仍然拿得到完整数据。
    """
    save_sessions(data if isinstance(data, dict) else load_sessions())


def sandbox_group(path):
    """会话列表分组用：(归一化 key, 显示名)。

    同一个目录可能被写成 D:\\Proj / d:/proj / 带尾斜杠，直接按字符串分组会裂成多组；
    Windows 上用 normcase + normpath 收敛。空路径表示「还没选目录」。
    """
    raw = str(path or "").strip()
    if not raw:
        return "", ""
    p = os.path.normpath(raw)
    return os.path.normcase(p), (os.path.basename(p) or p)


def reorder_sessions(data, ids):
    """按 ids 的顺序重排这些会话在数组中的相对位置；未列出的会话留在原位。

    只置换「已列出的这些 id 占用的槽位」——不同目录的会话、GAL 会话都不受影响，
    所以前端可以只提交当前分组里的 id，不用回传全量列表。
    返回错误文本，空串表示成功。
    """
    rows = (data or {}).get("sessions") or []
    index = {str(s.get("id") or ""): i for i, s in enumerate(rows)}
    want = []
    for x in (ids or []):
        sid = str(x or "").strip()
        if not sid or sid not in index:
            return "有会话不存在：%s" % (sid or "(空)")
        if sid in want:
            return "会话 id 重复：%s" % sid
        want.append(sid)
    if not want:
        return "缺少要排序的会话"
    slots = sorted(index[sid] for sid in want)
    picked = [rows[index[sid]] for sid in want]
    for slot, item in zip(slots, picked):
        rows[slot] = item
    return ""


def _payload_msg_window(default=MSG_WINDOW):
    """发给前端的「当前会话消息窗口」条数；0 = 全部。

    为什么可以调小：前端已支持分页 —— 服务端带 msg_total / msg_start，
    前端用 /api/session/messages 按需补更早的消息（core.js / chat.js）。
    默认 80 时，一个 80 条消息的会话会让 done 事件涨到约 780 KB，
    连接慢时最容易丢的就是这个包。
    """
    try:
        from app.config import load_config
        v = (load_config() or {}).get("chat_payload_msg_window")
        if v is None:
            return default
        return max(0, int(v))
    except Exception:                    # noqa: BLE001  读配置失败按默认
        return default


def slim_sessions(data, msg_limit=None):
    """只保留当前会话的消息，其余会话 messages 置为 None（前端切换时再按需拉取），
    避免每次进页面都把全部历史塞进 payload。

    当前会话也只回最近 `msg_limit` 条（默认取 chat_payload_msg_window），
    并带 `msg_total / msg_start`：更早的消息由 `/api/session/messages` 按需补。
    """
    if not data:
        return data
    if msg_limit is None:
        msg_limit = _payload_msg_window()
    cur = data.get("current")
    out = {"current": cur, "sessions": []}
    # 置顶会话排在前面；同组内保持原数组顺序（新会话仍在最前，更新会话不会乱跳）
    rows = sorted(data.get("sessions", []),
                  key=lambda s: 0 if s.get("pinned") else 1)
    for s in rows:
        item = {"id": s["id"], "title": s.get("title", ""), "updated_at": s.get("updated_at", 0),
                "pinned": bool(s.get("pinned"))}
        msgs = s.get("messages") or []
        item["msg_total"] = len(msgs)
        if s["id"] == cur:
            try:
                limit = int(msg_limit) if msg_limit else 0
            except (TypeError, ValueError):
                limit = MSG_WINDOW
            start = max(0, len(msgs) - limit) if limit > 0 else 0
            item["messages"] = msgs[start:]
            item["msg_start"] = start
        else:
            item["messages"] = None
            item["msg_start"] = 0
        item["kb_ids"] = s.get("kb_ids", [])
        item["ai_write"] = s.get("ai_write", True)
        item["use_profile"] = s.get("use_profile", True)
        item["use_memory"] = s.get("use_memory", True)
        item["compress_marks"] = compress_marks_for(s)
        # 提问标记：只回传仍然指向有效消息的条目（撤回后可能失效）
        qmarks = []
        for m in (s.get("qmarks") or []):
            if not isinstance(m, dict):
                continue
            try:
                qi = int(m.get("idx"))
            except (TypeError, ValueError):
                continue
            if 0 <= qi < len(msgs):
                qmarks.append({"idx": qi,
                               "label": str(m.get("label") or "")[:40],
                               "text": str(m.get("text") or "")[:120]})
        item["qmarks"] = qmarks
        item["agent_enabled"] = s.get("agent_enabled", False)
        item["sandbox"] = s.get("sandbox", "")
        item["sandbox_key"], item["sandbox_name"] = sandbox_group(s.get("sandbox"))
        item["allow_outside_read"] = s.get("allow_outside_read", False)
        # 审批档位必须回传：否则前端 applySessions 后读不到，会永远显示「手动审批」
        item["approval_mode"] = s.get("approval_mode", "ai")
        item["auto_chat"] = s.get("auto_chat", False)
        item["scene"] = s.get("scene", "general")
        item["gal_enabled"] = s.get("gal_enabled", False)
        item["gal_scope"] = s.get("gal_scope", "main")
        item["gal_use_profile"] = s.get("gal_use_profile", False)
        item["gal_use_kb"] = s.get("gal_use_kb", False)
        item["gal_use_rules"] = s.get("gal_use_rules", False)
        item["gal_character_id"] = s.get("gal_character_id", "")
        item["gal_bg"] = s.get("gal_bg", "")
        item["gal_bgm"] = s.get("gal_bgm", "")
        item["gal_stats"] = s.get("gal_stats", [])
        item["gal_map"] = s.get("gal_map") or {}
        item["gal_node_id"] = s.get("gal_node_id", "")
        item["gal_round"] = s.get("gal_round", 0)
        item["gal_auto_on"] = s.get("gal_auto_on", False)
        item["mem_count"] = len((s.get("memory") or {}).get("nodes", []))
        out["sessions"].append(item)
    return out

def find_session(data, sid):
    for s in data["sessions"]:
        if s["id"] == sid:
            return s
    return None
