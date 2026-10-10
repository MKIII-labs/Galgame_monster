# -*- coding: utf-8 -*-
"""路径常量、共享锁与带 mtime 缓存的 JSON 读写"""
import json
import os
import threading
import uuid

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


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        _json_cache[path] = (os.path.getmtime(path), obj)
    except OSError:
        _json_cache.pop(path, None)


def drop_cache(path):
    _json_cache.pop(path, None)


def new_id():
    return "s_" + uuid.uuid4().hex[:12]
