# -*- coding: utf-8 -*-
"""会话读写、精简与旧格式解析"""
import os
import re
import shutil
import threading
import time

from app.store import (SESSION_FILE, LEGACY_HISTORY, DATA_DIR, read_json, read_json_copy,
                       write_json, drop_cache, new_id, clone_containers, _lock)

# 「当前会话」单独存一个几十字节的小文件：切会话时只改它，
# 不再为一个 current 字段全量重写整份会话。
CURRENT_FILE = os.path.join(DATA_DIR, "current_session.json")

# 同一会话的「事件追加 + 缓存写」串行锁：不同会话互不阻塞。
# 流式落库（api/chat._persist_session）与这里的元数据 / 消息修改共用同一套锁，
# 否则改名、清空、撤回会和流式写入互相覆盖。
_SESSION_WRITE_LOCKS = {}
_SESSION_WRITE_LOCKS_GUARD = threading.Lock()


def session_write_lock(sid):
    key = str(sid or "")
    with _SESSION_WRITE_LOCKS_GUARD:
        lk = _SESSION_WRITE_LOCKS.get(key)
        if lk is None:
            lk = _SESSION_WRITE_LOCKS[key] = threading.Lock()
        return lk

# 切会话/首屏只回最近这么多条消息；更早的用 /api/session/messages 按需拉。
MSG_WINDOW = 40
# 旧逻辑只认 doc_text：小文档保留一份兼容副本，大文档不再重复存（见 session_compact）
DOC_TEXT_COMPAT_MAX = 200_000
# 单份附件正文最多内联多少字；重解析附件时也要守住，否则会重新撑大缓存
MAX_INLINE_DOC_CHARS = 2_000_000
# 每个会话最多保存多少个「提问标记」：悬浮按钮太多会挡住聊天区
MAX_QMARKS = 20
# 物化缓存目录（跟 SESSION_FILE 同目录：测试把 SESSION_FILE 指到临时目录时也隔离）
STORE_DIRNAME = "session_store"
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
_CITE_N_RE = re.compile(r"\[(\d{1,3})\]")
_SEARCH_NORM_DONE = False


def _safe_sid(sid):
    return _SID_RE.sub("_", str(sid or "").strip())[:80]


def _store_dir():
    return os.path.join(os.path.dirname(SESSION_FILE), STORE_DIRNAME)


def _store_path(sid):
    return os.path.join(_store_dir(), _safe_sid(sid) + ".json")


def _backup_corrupt_session_file():
    """sessions.json 存在但解析不出会话时，先复制一份再重建。

    以前 read_json 失败会直接返回空，load_sessions 随即新建会话并覆盖原文件，
    一次临时占用或半截写入就能把整份历史静默清空。
    """
    try:
        if not os.path.exists(SESSION_FILE) or os.path.getsize(SESSION_FILE) <= 2:
            return
        dst = "%s.corrupt-%d" % (SESSION_FILE, int(time.time()))
        shutil.copy2(SESSION_FILE, dst)
        print("[会话] sessions.json 读取失败，已备份到 %s" % os.path.basename(dst))
    except Exception as e:                # noqa: BLE001  备份失败不能影响启动
        print("[会话] 备份损坏的 sessions.json 失败：%s" % e)


def drop_session_files(sid):
    """删除会话时清理所有派生数据：物化缓存、事件流、轨迹、终端会话。

    只删列表里的条目会留下几十 MB 的孤儿文件，且磁盘上仍能被恢复出来。
    """
    s = str(sid or "").strip()
    if not s:
        return
    _TOMBSTONES.add(s)           # 索引被覆盖时也不能把已删除的会话「复活」
    safe = _safe_sid(s)
    try:
        os.remove(_store_path(s))
    except OSError:
        pass
    shutil.rmtree(os.path.join(os.path.dirname(SESSION_FILE), "sessions", safe),
                  ignore_errors=True)
    for mod, fn in (("trajectory", "drop"), ("session_events", "drop_last"),
                    ("terminal", "drop_by_sid")):
        try:
            m = __import__("app." + mod, fromlist=[fn])
            getattr(m, fn)(s)
        except Exception:                # noqa: BLE001  清理失败不影响删除结果
            pass
    try:
        drop_cache(_store_path(s))
    except Exception:                    # noqa: BLE001
        pass
    if isinstance(_AGG, dict):
        rows = [x for x in (_AGG.get("sessions") or []) if x.get("id") != s]
        if len(rows) != len(_AGG.get("sessions") or []):
            _AGG["sessions"] = rows
            _write_index(_AGG)


def ensure_session_kb_fields(s):
    s.setdefault("memory", {"nodes": [], "edges": []})
    s.setdefault("kb_ids", [])
    s.setdefault("ai_write", True)
    s.setdefault("use_profile", True)          # 本会话是否读取用户画像（默认开，可在知识库面板按会话关）
    s.setdefault("use_memory", True)           # 本会话是否读取会话记忆 / 绑定知识库（默认开）
    s.setdefault("agent_enabled", False)   # 是否开启「文件 Agent」模式
    s.setdefault("sandbox", "")            # Agent 的沙箱目录（绝对路径）
    s.setdefault("scene", "general")           # 场景包：general | office | study | life
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


# 本次进程的整份会话读模型：{"current":..., "sessions":[...]}。
# 事实源是每个会话的 append-only 事件日志（data/sessions/<id>/events.jsonl），
# 这里只是启动时从「物化缓存 + 事件流」拼出来的内存副本。
_AGG = None
# 已删除会话的墓碑：索引被误覆盖时，靠它区分「磁盘残留的缓存」与「真的删掉的会话」。
_TOMBSTONES = set()
TOMBSTONE_LIMIT = 1000
_STATS_TTL = 30.0
# 单会话事件日志超过这个体积就提示（append-only，不自动删）
EVENT_WARN_BYTES = 64 * 1024 * 1024
_STATS_CACHE = {"at": 0.0, "data": {}}


def reset_cache():
    """测试用：丢掉进程内会话缓存，模拟一次重启。"""
    global _AGG
    _AGG = None
    _TOMBSTONES.clear()
    _STATS_CACHE["at"] = 0.0
    _STATS_CACHE["data"] = {}


def reset_merge_flag():
    """兼容旧测试名：等价于 reset_cache。"""
    reset_cache()


def _sessions_root():
    return os.path.join(os.path.dirname(SESSION_FILE), "sessions")


def _index_path():
    return os.path.join(_sessions_root(), "index.json")


def _new_session(sid=None):
    s = {"id": sid or new_id(), "title": "新对话", "updated_at": time.time(), "messages": []}
    ensure_session_kb_fields(s)
    return s


def _normalize_session(s):
    """补默认字段（原地）；不是 dict 时返回 False。"""
    if not isinstance(s, dict):
        return False
    s.setdefault("id", new_id())
    s.setdefault("title", "新对话")
    s.setdefault("updated_at", time.time())
    s.setdefault("messages", [])
    if not isinstance(s.get("messages"), list):
        s["messages"] = []
    ensure_session_kb_fields(s)
    return True


def _read_cache(sid):
    """读物化缓存；返回 (session, saved_at)。只在启动加载时读一次。"""
    path = _store_path(sid)
    obj = read_json(path, None)
    drop_cache(path)                     # 大文件不常驻读缓存
    if not isinstance(obj, dict):
        return None, 0.0
    sess = obj.get("session")
    if not isinstance(sess, dict):
        return None, 0.0
    try:
        saved_at = float(obj.get("saved_at") or 0)
    except (TypeError, ValueError):
        saved_at = 0.0
    return sess, saved_at


def _write_cache(s, saved_at=None):
    """写物化缓存（可重建，不是事实源）。"""
    if not isinstance(s, dict) or not s.get("id"):
        return False
    path = _store_path(s["id"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return write_json(path, {"saved_at": saved_at or time.time(), "session": s},
                          indent=None, remember=False)
    except Exception as e:               # noqa: BLE001  缓存失败不影响主流程
        print("[存储] 会话缓存写入失败：%s" % e)
        return False


def _load_one(sid):
    """读一个会话：物化缓存优先；缓存缺失 / 事件流更新时用事件流重建。"""
    sid = str(sid or "")
    if not sid:
        return None
    sess, saved_at = _read_cache(sid)
    try:
        from app import session_events as se
        ts = se.latest_ts(sid)
    except Exception:                    # noqa: BLE001  事件模块不可用时只用缓存
        se, ts = None, 0.0
    if ts > saved_at + 1e-6:
        # 事件流比缓存新：上次进程写完事件、还没来得及写缓存就退出了
        ev = se.rebuild(sid)
        if ev and ev.get("id"):
            sess = ev
            _write_cache(sess)
    if not sess:
        if se is not None:
            ev = se.rebuild(sid)
            if ev and ev.get("id"):
                sess = ev
                _write_cache(sess)
        if not sess:
            return None
    _normalize_session(sess)
    if se is not None:
        se.seed_last(sess)               # 第一次落库要能算出增量
    return sess


def _scan_store():
    """索引丢失时按物化缓存重建会话列表（按 updated_at 倒序）。"""
    out = []
    try:
        names = os.listdir(_store_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        s = _load_one(name[:-len(".json")])
        if s:
            out.append(s)
    out.sort(key=lambda x: -(float(x.get("updated_at") or 0)))
    return out


def _write_index(data):
    order = [str(s.get("id")) for s in (data or {}).get("sessions") or []
             if isinstance(s, dict) and s.get("id")]
    path = _index_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    deleted = sorted(_TOMBSTONES)[-TOMBSTONE_LIMIT:]
    return write_json(path,
                      {"current": str((data or {}).get("current") or ""),
                       "order": order, "deleted": deleted,
                       "saved_at": time.time()},
                      indent=None, remember=False)


def _read_index():
    path = _index_path()
    obj = read_json(path, None)
    drop_cache(path)
    if not isinstance(obj, dict):
        return None
    deleted = obj.get("deleted")
    if isinstance(deleted, list):
        _TOMBSTONES.update(str(x) for x in deleted if x)
    return obj


def storage_stats(ttl=_STATS_TTL):
    """只读存储指标（带短 TTL）：会话数、缓存/事件体积、孤儿事件目录。

    只 stat，不解析内容；给 /api/health 与健康面板用。
    """
    now = time.time()
    if _STATS_CACHE["data"] and now - _STATS_CACHE["at"] < ttl:
        return dict(_STATS_CACHE["data"])
    cache_bytes = 0
    try:
        for name in os.listdir(_store_dir()):
            p = os.path.join(_store_dir(), name)
            if os.path.isfile(p):
                cache_bytes += os.path.getsize(p)
    except OSError:
        pass
    idx = _read_index() or {}
    order = [str(x) for x in (idx.get("order") or [])]
    known = set(order) | set(_TOMBSTONES)
    event_bytes = 0
    largest_event = 0
    orphans = 0
    try:
        names = os.listdir(_sessions_root())
    except OSError:
        names = []
    for name in names:
        d = os.path.join(_sessions_root(), name)
        if not os.path.isdir(d):
            continue
        cur = 0
        for fn in ("events.jsonl", "events.1.jsonl"):
            try:
                cur += os.path.getsize(os.path.join(d, fn))
            except OSError:
                pass
        event_bytes += cur
        largest_event = max(largest_event, cur)
        if name not in known:
            orphans += 1
    data = {"sessions": len(order), "current": str(idx.get("current") or ""),
            "cache_bytes": cache_bytes, "event_bytes": event_bytes,
            "largest_event_bytes": largest_event,
            "event_warn": largest_event >= EVENT_WARN_BYTES,
            "orphan_dirs": orphans, "tombstones": len(_TOMBSTONES)}
    _STATS_CACHE["at"] = now
    _STATS_CACHE["data"] = data
    return dict(data)


_LEGACY_DONE = False


def _migrate_legacy():
    """把旧 data/sessions.json 迁成「每会话事件流 + 物化缓存」，然后删掉它。

    每个会话追加一条 `session/baseline`：旧事件流可能落后于 sessions.json，
    基线能保证事件流从这一刻起与会话内容完全一致。
    """
    global _LEGACY_DONE
    if _LEGACY_DONE:
        return
    _LEGACY_DONE = True
    if not os.path.exists(SESSION_FILE):
        return
    data = read_json_copy(SESSION_FILE)
    if not (isinstance(data, dict) and isinstance(data.get("sessions"), list)):
        _backup_corrupt_session_file()
        return
    try:
        from app import session_events as se
    except Exception as e:               # noqa: BLE001  事件模块不可用，下次启动再迁
        print("[存储] 事件流不可用，暂不迁移：%s" % e)
        _LEGACY_DONE = False
        return
    try:
        agg_at = float(data.get("saved_at") or 0)
    except (TypeError, ValueError):
        agg_at = 0.0
    rows = []
    for s in data.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        snap_at = 0.0
        if sid:
            snap, snap_at = _read_cache(sid)
            if snap and snap_at > agg_at:
                s = snap                 # 崩溃前热路径写的最新快照优先
            ts = se.latest_ts(sid)
            if ts > max(agg_at, snap_at):
                ev = se.rebuild(sid)
                if ev and ev.get("id"):
                    s = ev               # 事件流比两份快照都新
        if not _normalize_session(s):
            continue
        rows.append(s)
        se.append_baseline(s)
        _write_cache(s)
    if not rows:
        print("[存储] sessions.json 里没有可迁移的会话，保留原文件")
        return
    cur = str(data.get("current") or "")
    if not any(s["id"] == cur for s in rows):
        cur = rows[0]["id"]
    _write_index({"current": cur, "sessions": rows})
    try:
        os.remove(SESSION_FILE)
        print("[存储] 已迁移到每会话事件流（%d 个会话），已删除 sessions.json" % len(rows))
    except OSError as e:
        print("[存储] 删除 sessions.json 失败：%s" % e)


def _build_aggregate():
    """启动加载：索引 + 物化缓存，缺失时用事件流重建。"""
    _migrate_legacy()
    idx = _read_index()
    sessions, order = [], []
    if idx:
        order = [str(x) for x in (idx.get("order") or [])]
        for sid in order:
            s = _load_one(sid)
            if s:
                sessions.append(s)
    # 索引存在但缓存里有「不在索引、也没有墓碑」的会话：说明索引被覆盖过（崩溃 / 旧进程），
    # 这些缓存是真数据，必须并回来。删除会话会写墓碑并删缓存，所以不会复活。
    known = {s["id"] for s in sessions}
    stray = []
    names = []
    if idx:
        try:
            names = os.listdir(_store_dir())
        except OSError:
            names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        sid = name[:-len(".json")]
        if sid in known or sid in _TOMBSTONES:
            continue
        s = _load_one(sid)
        if s and s["id"] not in known:
            stray.append(s)
            known.add(s["id"])
    if stray:
        stray.sort(key=lambda x: -(float(x.get("updated_at") or 0)))
        sessions.extend(stray)
        print("[存储] 发现 %d 个不在索引里的会话缓存，已恢复（索引可能被覆盖过）" % len(stray))
    if not sessions:
        sessions = _scan_store()
        if sessions:
            print("[存储] 会话索引缺失，已按物化缓存重建（%d 个）" % len(sessions))
    if not sessions and os.path.exists(LEGACY_HISTORY):
        try:
            with open(LEGACY_HISTORY, "r", encoding="utf-8") as f:
                legacy = f.read()
            if legacy.strip():
                s = {"id": new_id(), "title": "历史记录", "updated_at": time.time(),
                     "messages": parse_legacy_history(legacy)}
                ensure_session_kb_fields(s)
                sessions.append(s)
        except Exception:                # noqa: BLE001
            pass
    if not sessions:
        sessions = [_new_session()]
    cur = str((idx or {}).get("current") or "")
    if not any(s.get("id") == cur for s in sessions):
        cur = sessions[0]["id"]
    data = {"current": cur, "sessions": sessions}
    if not idx or not order:
        # 索引 / 缓存完全缺失：补 index 与所有缓存，下次启动不用再扫目录
        for s in sessions:
            _write_cache(s)
        _write_index(data)
    elif stray or len(order) != len(sessions):
        _write_index(data)
    return data


def _get_aggregate():
    global _AGG
    if _AGG is None:
        _AGG = _build_aggregate()
    return _AGG


# ---------------------------------------------------------------------------
# 热路径落库
# ---------------------------------------------------------------------------

def save_session(s, cache=True):
    """热路径落库：只处理这一条会话。

    顺序是「先追加事件、再写物化缓存」：事件是事实源，缓存写失败时下次启动
    会从事件流重建；反过来（先写缓存）会让事件流落后，重建时丢内容。

    `cache=False`：只追加事件、不重写物化缓存（生成中的进度快照用）。
    大会话的缓存单份已有十几 MB，每 15 秒全量重写一轮会和流式输出抢线程与磁盘；
    而进度快照只用于崩溃恢复，下次启动按事件时间戳判断缓存过期后会自动重建。
    """
    if not isinstance(s, dict) or not s.get("id"):
        return
    try:
        from app import session_events
        session_events.record(s)
    except Exception as e:               # noqa: BLE001  事件失败仍要写缓存
        print("[事件] %s" % e)
    if cache:
        _write_cache(s)


def mutate_session(sid, fn, cache=True):
    """读-改-写单个会话：只落这一条（事件 + 物化缓存）。

    fn(sess) 在 store 锁内执行，返回错误文本（空串表示成功）；磁盘写在两层锁外。
    会话写锁保证与流式落库互斥同一会话，同时不会因为改名 / 清空就重写全部会话。
    返回 (会话, 错误文本)；会话不存在时返回 (None, "not found")。
    """
    sid = str(sid or "")
    if not sid:
        return None, "not found"
    with session_write_lock(sid):
        with _lock:
            data = _get_aggregate()
            s = find_session(data, sid)
            if not s:
                return None, "not found"
            err = fn(s) or ""
        if not err:
            save_session(s, cache=cache)
    return s, err


def save_index():
    """只写索引（顺序 / current / 墓碑），不重写任何会话缓存。"""
    return _write_index(_get_aggregate())


def mutate_sessions(fn):
    """结构型修改会话列表（新建 / 删除 / 排序）：锁内深拷贝 → 改 → 原子换读模型。

    fn(data) 返回错误文本（空串表示成功）。只换内存读模型，不写任何缓存；
    调用方按需 `save_session()` / `save_index()`。
    """
    with _lock:
        data = load_sessions()
        err = fn(data) or ""
        if not err:
            cache_sessions(data)
    return data, err


def _mark_resume_pending(data, live_ids=None):
    """把上次中断留下的 `_progress` 快照转成「可继续任务」状态。

    只在进程启动时做一次（由 `startup_recover` 调用）；正常结束的回合，收尾会删掉
    这条快照，所以这里命中的一定是「上次进程中途退出」。

    live_ids：当前确实有活跃流的会话。即使被误调用（例如别的入口重新触发了恢复），
    也不能把正在生成的那条进度快照标成中断 —— 否则流式循环下一次落库会因为
    `_progress` 被清掉而新建第二条 assistant，表现为同一轮输出出现多段重复气泡。
    """
    live = {str(x) for x in (live_ids or ())}
    dirty = 0
    for s in data.get("sessions") or []:
        sid = str(s.get("id") or "")
        if sid and sid in live:
            continue
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
            dirty += 1
    if dirty:
        print("[会话] 断点恢复：%d 个会话标记为可继续" % dirty)
    return dirty > 0


def _collapse_resume_duplicates(data):
    """合并「断点恢复误标」留下的重复 assistant 消息。

    现场（2026-11-14 00:29 / 00:30）：运行中的进度快照被误标成 resume，
    流式循环下一次落库时又新建一条完整快照，于是同一轮回答变成
    「408 字 → 4498 字 → 11822 字」三条相邻 assistant，后一条包含前一条。

    只合并同时满足这些条件的相邻消息，避免误伤正常历史：
      · 两条都是 assistant，中间没有 user；
      · 前一条带 interrupted 或 end_reason.kind == 'resume'；
      · 后一条正文严格更长，且以前一条正文为前缀。
    保留后一条（内容、工具、计划、blocks 都更完整）。
    """
    changed = 0
    for s in data.get("sessions") or []:
        msgs = s.get("messages") or []
        before = len(msgs)
        i = 0
        while i + 1 < len(msgs):
            a, b = msgs[i], msgs[i + 1]
            if not (isinstance(a, dict) and isinstance(b, dict)):
                i += 1
                continue
            ac = str(a.get("content") or "")
            bc = str(b.get("content") or "")
            is_resume_dup = (
                a.get("role") == "assistant" and b.get("role") == "assistant"
                and (a.get("interrupted") or (a.get("end_reason") or {}).get("kind") == "resume")
                and ac and bc.startswith(ac) and len(bc) >= len(ac)
            )
            if is_resume_dup:
                msgs.pop(i)
                changed += 1
                continue
            i += 1
        if len(msgs) != before:
            s["messages"] = msgs
    if changed:
        print("[会话] 合并断点重复消息：%d 条" % changed)
    return changed > 0


def startup_recover(live_ids=None):
    """进程启动时的会话恢复：合并重复消息 + 标记可继续的断点。

    只在这里调用一次（`app/httpd.py` 的 create_server），不再挂在
    `load_sessions()` 里 —— 否则任意一次加载失败重试都可能在生成过程中
    把正在写的进度快照误标成中断。
    """
    data = load_sessions()
    changed = _collapse_resume_duplicates(data)
    if _mark_resume_pending(data, live_ids=live_ids):
        changed = True
    if changed:
        save_sessions(data)
    return data


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
    """记住当前会话：只写一个小侧车文件（几十字节），不碰会话存储。"""
    sid = str(sid or "").strip()
    if not sid:
        return
    write_json(CURRENT_FILE, {"id": sid})


def _apply_current(data):
    """用侧车文件里的 current 覆盖索引里的旧值。

    侧车文件在、且指向存在的会话时才生效；删除会话后残留的旧 id 会被忽略。
    """
    try:
        cur = str((read_json(CURRENT_FILE, {}) or {}).get("id") or "")
    except Exception:
        cur = ""
    if cur and any(s.get("id") == cur for s in (data.get("sessions") or [])):
        data["current"] = cur


def load_sessions(copy=True):
    """读取整份会话。`copy=False` 只给「读完就扔」的只读路径用：
    跳过容器的结构复制（单次可省约 30ms），调用方不得修改返回值。

    启动时把每个会话从「物化缓存 / 事件流」装进进程内副本；之后直接返回它，
    不再每次解析几十 MB 的整份文件。会话列表与顺序来自 index.json。
    """
    data = _get_aggregate()
    rows = data.get("sessions")
    if not isinstance(rows, list):
        rows = []
        data["sessions"] = rows
    if not rows:
        rows.append(_new_session())
    for s in rows:
        _normalize_session(s)
    if not any(s["id"] == data.get("current") for s in rows):
        data["current"] = rows[0]["id"]
    _apply_current(data)
    return clone_containers(data) if copy else data


def cache_sessions(data):
    """把热点路径刚改过的整份会话登记成进程内读模型（不落盘）。

    `_persist_session` 只写单会话缓存；后续的 `load_sessions()` 理应立刻
    看到这次修改，否则同一轮里读到的还是旧内容。
    """
    global _AGG
    if not isinstance(data, dict):
        return False
    _AGG = data
    return True


def save_sessions(data):
    """冷路径落库：写 index + 每个会话的物化缓存（热路径用 `save_session`）。

    先追加事件、后写缓存：事件时间戳不会晚于缓存的 saved_at，启动时不会因为
    「事件比缓存新」把刚写过的会话重放一遍；缓存写失败时事件流仍能恢复。

    返回是否全部写成功（失败已打日志，调用方可按需提示）。
    """
    if data is None:
        return False
    # 老消息的搜索来源可能重复 / 缺研究台账编号。冷路径写盘前顺手修一次，
    # 让磁盘上的历史逐步收敛（新消息在落库时已经规范化过）。
    global _SEARCH_NORM_DONE, _AGG
    if not _SEARCH_NORM_DONE:
        try:
            normalize_session_search(data)
            _SEARCH_NORM_DONE = True
        except Exception as e:            # noqa: BLE001  规范化失败不能挡住落库
            print("[会话] 搜索来源规范化失败：%s" % e)
    rows = data.get("sessions") or []
    try:
        from app import session_events
        session_events.record_all(rows)
    except Exception as e:
        print("[事件] %s" % e)
    data["saved_at"] = time.time()
    ok = True
    for s in rows:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        if not _write_cache(s, saved_at=data["saved_at"]):
            ok = False
    if not _write_index(data):
        ok = False
    # current 同步到侧车文件（只在真的变了时写）：新建 / 删除 / 存档恢复
    # 都会改 current，不跟着同步的话下次 load 会被旧值覆盖。
    try:
        cur = str(data.get("current") or "")
        old = str((read_json(CURRENT_FILE, {}) or {}).get("id") or "")
        if cur and cur != old:
            set_current(cur)
    except Exception:
        pass
    _AGG = data
    return ok


def flush_sessions(data=None):
    """把内存里的全量会话落盘一次（回合收尾 / 备份前）。"""
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


def _normalize_sources(srcs):
    """去重来源并复制成独立 dict（调用方可能继续改 n，不能污染读缓存）。"""
    out, seen = [], set()
    for x in srcs or []:
        if not isinstance(x, dict):
            continue
        key = str(x.get("url") or x.get("title") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(dict(x))
    return out


def _backfill_source_numbers(srcs, content):
    """老数据没有 n：正文的 [n] 都落在 1..N 内时，按来源顺序补编号。

    超出范围就不动：说明原编号来自跨轮台账，硬按顺序补会误导点击定位。
    """
    if not srcs or any(x.get("n") for x in srcs):
        return False
    if not isinstance(content, str):
        return False
    nums = set()
    for v in _CITE_N_RE.findall(content):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            nums.add(n)
    if not nums or max(nums) > len(srcs):
        return False
    for i, x in enumerate(srcs, 1):
        x["n"] = i
    return True


def normalize_session_search(data):
    """把历史会话里的搜索来源去重并回填 n（只动 sources / search.hits）。"""
    changed = False
    for s in (data or {}).get("sessions") or []:
        for m in s.get("messages") or []:
            if not isinstance(m, dict):
                continue
            srcs = m.get("sources")
            if not isinstance(srcs, list) or not srcs:
                continue
            norm = _normalize_sources(srcs)
            back = _backfill_source_numbers(norm, m.get("content"))
            search = m.get("search")
            hits_bad = (isinstance(search, dict)
                        and int(search.get("hits") or 0) != len(norm))
            if len(norm) != len(srcs) or back or hits_bad:
                m["sources"] = norm
                if isinstance(search, dict):
                    search["hits"] = len(norm)
                changed = True
    return changed


def slim_message_for_client(m, slim=False):
    """前端展示用的消息：剥掉只有服务端上下文才需要的附件正文。

    `docs[].text` / `doc_text` 可能是几十万字（一本教材），前端只用
    name / size / chars / file；预览走 /api/attach/get，正文由服务端在
    发请求时注入 —— 放进 payload 只是白白撑大首屏流量。
    `slim=True` 再剥掉过程 / 工具 / 耗时等重建过程折叠才需要的重字段，
    给分享导出这种只要正文与来源的场景用。
    """
    if not isinstance(m, dict):
        return m
    out = dict(m)
    out.pop("doc_text", None)
    docs = out.get("docs")
    if isinstance(docs, list) and docs:
        out["docs"] = [{k: v for k, v in d.items() if k != "text"}
                       if isinstance(d, dict) else d for d in docs]
    if slim:
        for k in ("blocks", "tools", "timing", "plan", "usage", "memory",
                  "reasoning", "end_reason"):
            out.pop(k, None)
    # 旧消息的来源可能重复、也没有研究台账编号：在副本上修正，别改共享缓存。
    if isinstance(out.get("sources"), list) and out["sources"]:
        norm = _normalize_sources(out["sources"])
        _backfill_source_numbers(norm, out.get("content"))
        out["sources"] = norm
        if isinstance(out.get("search"), dict):
            out["search"] = dict(out["search"])
            out["search"]["hits"] = len(norm)
    return out


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
            item["messages"] = [slim_message_for_client(x) for x in msgs[start:]]
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
        # 审批档位必须回传：否则前端 applySessions 后读不到，会永远显示「手动审批」
        item["approval_mode"] = s.get("approval_mode", "ai")
        item["scene"] = s.get("scene", "general")
        item["mem_count"] = len((s.get("memory") or {}).get("nodes", []))
        out["sessions"].append(item)
    return out

def find_session(data, sid):
    for s in data["sessions"]:
        if s["id"] == sid:
            return s
    return None


def load_session(sid, copy=True):
    """只取一个会话（不深拷贝整份会话列表）；找不到返回 None。"""
    s = find_session(_get_aggregate(), sid)
    if s is None:
        return None
    _normalize_session(s)
    return clone_containers(s) if copy else s
