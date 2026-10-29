# -*- coding: utf-8 -*-
"""路径常量、共享锁与带 mtime 缓存的 JSON 读写"""
import json
import os
import random
import threading
import time
import uuid

APP_VERSION = "3.0.0"
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

_json_cache = {}   # path -> (mtime, data)


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
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[存储] 读取失败 {os.path.basename(path)}：{e}")
        return default
    _json_cache[path] = (mtime, data)
    return data


def write_json(path, obj, indent=2):
    """写 JSON。先落临时文件再 os.replace —— 中途崩溃/断电不会把原文件写坏
    （os.replace 在同一分区上是原子的）。
    indent=None 写紧凑格式，用于「只有程序读」的文件（sessions / 用量之类）。

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
    try:
        _json_cache[path] = (os.path.getmtime(path), obj)
    except OSError:
        _json_cache.pop(path, None)


def drop_cache(path):
    _json_cache.pop(path, None)


def new_id():
    return "s_" + uuid.uuid4().hex[:12]
