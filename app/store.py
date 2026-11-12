# -*- coding: utf-8 -*-
"""路径常量、共享锁与带 mtime 缓存的 JSON 读写"""
import collections
import json
import os
import random
import threading
import time
import uuid

APP_VERSION = "3.4"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")             # 运行时数据（整目录 gitignore）
RESOURCES_DIR = os.path.join(ROOT, "resources")   # 随项目发布的静态资源

os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PROFILE_FILE = os.path.join(DATA_DIR, "user_profile.json")
SESSION_FILE = os.path.join(DATA_DIR, "sessions.json")
LEGACY_HISTORY = os.path.join(DATA_DIR, "chat_history.txt")
USAGE_FILE = os.path.join(DATA_DIR, "token_usage.jsonl")   # 用量流水（每行一条 JSON）
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")          # 定时提醒 / 主动聊天任务
NOTIFY_DIR = os.path.join(DATA_DIR, "notify")              # 系统通知的载荷文件（中文走文件，避开命令行编码坑）

_lock = threading.Lock()
ACTIVE_STREAMS = {}  # session_id -> threading.Event
PENDING_STOPS = {}   # session_id -> 请求停止的时间戳（请求还没登记流时先记下）
_PENDING_LOCK = threading.Lock()
PENDING_STOP_TTL = 120.0


def request_stop(sid):
    """请求停止：已有流就置事件；还没登记就留下 pending stop，等登记时消费。

    返回 {"found": bool, "pending": bool}。以前找不到流也返回 ok，
    前端只能干等，长上下文压缩阶段点停止等于没点。
    """
    sid = str(sid or "").strip()
    if not sid:
        return {"found": False, "pending": False}
    with _lock:
        ev = ACTIVE_STREAMS.get(sid)
    if ev is not None:
        ev.set()
        # 已有流接管了这次停止：清掉历史 pending，避免它再停掉下一轮请求
        with _PENDING_LOCK:
            PENDING_STOPS.pop(sid, None)
        return {"found": True, "pending": False}
    with _PENDING_LOCK:
        now = time.time()
        for k in list(PENDING_STOPS):
            if now - float(PENDING_STOPS.get(k) or 0) > PENDING_STOP_TTL:
                PENDING_STOPS.pop(k, None)
        PENDING_STOPS[sid] = now
    return {"found": False, "pending": True}


def consume_stop(sid):
    """消费一次 pending stop（只生效一次）。"""
    sid = str(sid or "").strip()
    if not sid:
        return False
    with _PENDING_LOCK:
        ts = PENDING_STOPS.pop(sid, None)
    try:
        return bool(ts and time.time() - float(ts) <= PENDING_STOP_TTL)
    except (TypeError, ValueError):
        return False

_json_cache = collections.OrderedDict()   # path -> (mtime, data)
# 读缓存上限：以前无上限，任何 read_json 过的文件都会一直驻留（阅读器分块、知识库、
# 研报这类大 JSON 读一次就长期占内存）。这里按「最近最少使用」淘汰。
_JSON_CACHE_MAX = 64
# 常读大文件（sessions.json 有几十 MB）不参与淘汰，否则被清掉后一次重新解析要几百毫秒。
_json_pinned = {os.path.abspath(SESSION_FILE)}


def _remember(path, mtime, data):
    _json_cache[path] = (mtime, data)
    _json_cache.move_to_end(path)
    while len(_json_cache) > _JSON_CACHE_MAX:
        victim = next((p for p in _json_cache if os.path.abspath(p) not in _json_pinned), None)
        if victim is None:
            break
        _json_cache.pop(victim, None)


def read_json(path, default=None):
    """读取 JSON：按 mtime 缓存，文件未变则不重复解析。
    返回对象是共享的，调用方请遵循「取出→修改→写回」模式，不要跨线程长期持有。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _json_cache.pop(path, None)
        return default
    hit = _json_cache.get(path)
    if hit and hit[0] == mtime:
        _json_cache.move_to_end(path)
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[存储] 读取失败 {os.path.basename(path)}：{e}")
        return default
    _remember(path, mtime, data)
    return data


def write_json(path, obj, indent=2, remember=True):
    """写 JSON。先落临时文件再 os.replace —— 中途崩溃/断电不会把原文件写坏
    （os.replace 在同一分区上是原子的）。
    indent=None 写紧凑格式，用于「只有程序读」的文件（sessions / 用量之类）。
    remember=False：写进去的内容不进读缓存。给「写一次、极少读」的大文件用
    （会话存档快照每轮都写一份，进了缓存就是每轮泄漏几十 MB）。

    tmp 用唯一名 + replace 失败重试：sessions.json 已经到十几 MB，写入窗口里
    很容易撞上杀软扫描或另一个读句柄，Windows 会直接抛 WinError 5（拒绝访问）。
    以前共用 `path + ".tmp"` 且不重试，一次失败就丢掉整份进度快照（NOTE 43.168）。"""
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), random.randrange(1 << 30))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        last = None
        for i in range(3):
            try:
                os.replace(tmp, path)
                last = None
                break
            except OSError as e:
                last = e
                time.sleep(0.1 * (3 ** i))     # 0.1 / 0.3 / 0.9 秒
        if last is not None:
            raise last
    except OSError as e:
        print(f"[存储] 写入失败 {os.path.basename(path)}：{e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    if not remember:
        _json_cache.pop(path, None)
        return
    try:
        _remember(path, os.path.getmtime(path), obj)
    except OSError:
        _json_cache.pop(path, None)


def drop_cache(path):
    _json_cache.pop(path, None)


def clone_containers(obj):
    """复制容器、共享标量：落盘快照用的浅拷贝。

    与 copy.deepcopy 的区别：字符串 / 数字 / 布尔不复制。Python 里字符串不可变，
    共享引用就够了；会话动辄几十 MB，deepcopy 会把全部容器再分配一遍，
    每次落库多花上百毫秒、还多占几十 MB。这里只重建 dict / list 骨架。"""
    if isinstance(obj, dict):
        return {k: clone_containers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clone_containers(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(clone_containers(v) for v in obj)
    return obj


def new_id():
    return "s_" + uuid.uuid4().hex[:12]
