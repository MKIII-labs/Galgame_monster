# -*- coding: utf-8 -*-
"""任务 / 日程 / 产物的 SQLite 存储。

为什么单独一个库：
- 对话仍用 sessions.json（既有形态，不迁移，避免动到老数据）；
- 任务与日程写入频繁、需要范围查询和事务，用 SQLite 更稳。

连接策略：**每个线程一个连接**（sqlite3 连接不能跨线程共享）。
WAL 模式让读写互不阻塞；synchronous=NORMAL 在本地单机场景下兼顾安全与速度。
"""
import json
import os
import sqlite3
import threading

from app.store import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "assistant.db")

_local = threading.local()
_init_lock = threading.Lock()
_inited = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL DEFAULT '',
  scene       TEXT NOT NULL DEFAULT 'general',
  title       TEXT NOT NULL,
  intent      TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'pending',
  priority    INTEGER NOT NULL DEFAULT 0,
  tags        TEXT NOT NULL DEFAULT '',
  due_at      REAL NOT NULL DEFAULT 0,
  parent_id   TEXT NOT NULL DEFAULT '',
  depends_on  TEXT NOT NULL DEFAULT '',
  repeat_rule TEXT NOT NULL DEFAULT '',
  estimate_min INTEGER NOT NULL DEFAULT 0,
  sort_order  REAL NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  started_at  REAL NOT NULL DEFAULT 0,
  finished_at REAL NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT '',
  meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_steps (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  title       TEXT NOT NULL,
  tool        TEXT NOT NULL DEFAULT '',
  args        TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'pending',
  confirmed   INTEGER NOT NULL DEFAULT 0,
  result      TEXT NOT NULL DEFAULT '',
  error       TEXT NOT NULL DEFAULT '',
  retry       INTEGER NOT NULL DEFAULT 0,
  started_at  REAL NOT NULL DEFAULT 0,
  finished_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id, idx);

CREATE TABLE IF NOT EXISTS artifacts (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'file',
  name       TEXT NOT NULL,
  path       TEXT NOT NULL DEFAULT '',
  size       INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);

CREATE TABLE IF NOT EXISTS task_templates (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  title       TEXT NOT NULL,
  steps       TEXT NOT NULL DEFAULT '[]',
  tags        TEXT NOT NULL DEFAULT '',
  priority    INTEGER NOT NULL DEFAULT 0,
  estimate_min INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS focus_sessions (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  start_at   REAL NOT NULL,
  end_at     REAL NOT NULL,
  minutes    INTEGER NOT NULL DEFAULT 0,
  kind       TEXT NOT NULL DEFAULT 'focus',   -- focus | break
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_focus_start ON focus_sessions(start_at DESC);

CREATE TABLE IF NOT EXISTS events (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  notes         TEXT NOT NULL DEFAULT '',
  start_at      REAL NOT NULL,
  end_at        REAL NOT NULL,
  all_day       INTEGER NOT NULL DEFAULT 0,
  location      TEXT NOT NULL DEFAULT '',
  rrule         TEXT NOT NULL DEFAULT '',
  remind_before INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'active',
  source        TEXT NOT NULL DEFAULT 'calendar',   -- calendar | legacy_reminder
  source_id     TEXT NOT NULL DEFAULT '',
  session_id    TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(status, start_at);

CREATE TABLE IF NOT EXISTS event_exdates (
  event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  occurrence_at REAL NOT NULL,
  PRIMARY KEY (event_id, occurrence_at)
);

CREATE TABLE IF NOT EXISTS event_fired (
  event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  occurrence_at REAL NOT NULL,
  fired_at      REAL NOT NULL,
  PRIMARY KEY (event_id, occurrence_at)
);

CREATE TABLE IF NOT EXISTS workflows (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  trigger_type     TEXT NOT NULL DEFAULT 'manual',   -- manual | time | event
  trigger_at       TEXT NOT NULL DEFAULT '',         -- 定时："08:30"
  trigger_weekdays TEXT NOT NULL DEFAULT '',         -- "0,1,2,3,4"（周一=0）
  trigger_event    TEXT NOT NULL DEFAULT '',         -- task.done | calendar.due
  actions          TEXT NOT NULL DEFAULT '[]',       -- JSON 数组
  enabled          INTEGER NOT NULL DEFAULT 1,
  last_run         REAL NOT NULL DEFAULT 0,
  next_run         REAL NOT NULL DEFAULT 0,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflows_due ON workflows(enabled, trigger_type, next_run);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id          TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  started_at  REAL NOT NULL,
  finished_at REAL NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'running',
  detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_wf ON workflow_runs(workflow_id, started_at DESC);

CREATE TABLE IF NOT EXISTS memory_items (
  id          TEXT PRIMARY KEY,
  scope       TEXT NOT NULL DEFAULT 'global',   -- global | session:<sid> | kb:<id>
  source      TEXT NOT NULL DEFAULT 'manual',   -- manual | task | kb | session
  source_id   TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'fact',     -- fact | preference | event | task | note
  title       TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '',
  importance  REAL NOT NULL DEFAULT 0.5,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  last_used_at REAL NOT NULL DEFAULT 0,
  use_count   INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe
  ON memory_items(source, source_id, text);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- 通用观测：做过什么、结果如何。不绑定领域，角色由提示词定义。
CREATE TABLE IF NOT EXISTS observations (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'chat',     -- math_arena | math_proof | math_paper | chat
  subject     TEXT NOT NULL DEFAULT '',         -- 数学分析 / 英语 / 编程…
  kind        TEXT NOT NULL DEFAULT 'attempt',  -- attempt | note | issue | feedback
  topic       TEXT NOT NULL DEFAULT '',         -- 知识点 / 主题
  value       TEXT NOT NULL DEFAULT '{}',       -- 结构化结果（JSON）
  evidence    TEXT NOT NULL DEFAULT '',         -- 题目、你的答案、原文
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_topic ON observations(subject, topic, created_at DESC);

-- 统一批注：PDF / 文本 / 代码 / 图片共用一套记录（见 app/anno.py）
-- anchor 用 JSON 存各文档类型的定位信息；quote/prefix/suffix 是漂移后的兜底定位。
CREATE TABLE IF NOT EXISTS annotations (
  id          TEXT PRIMARY KEY,
  doc_key     TEXT NOT NULL DEFAULT '',        -- 沙箱内相对路径（统一主键的一部分）
  doc_type    TEXT NOT NULL DEFAULT '',        -- pdf | md | tex | code | docx | pptx | image
  kind        TEXT NOT NULL DEFAULT 'note',    -- highlight | note | excerpt | qa
  anchor      TEXT NOT NULL DEFAULT '{}',
  quote       TEXT NOT NULL DEFAULT '',
  prefix      TEXT NOT NULL DEFAULT '',
  suffix      TEXT NOT NULL DEFAULT '',
  color       TEXT NOT NULL DEFAULT 'yellow',
  tags        TEXT NOT NULL DEFAULT '[]',
  comment     TEXT NOT NULL DEFAULT '',
  question    TEXT NOT NULL DEFAULT '',
  answer      TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'user',    -- user | ai
  citation    TEXT NOT NULL DEFAULT '{}',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anno_doc ON annotations(doc_key, created_at);
CREATE INDEX IF NOT EXISTS idx_anno_kind ON annotations(kind, created_at DESC);

-- 文献条目（学术场景）：一篇 PDF / 稿件一条，元数据可编辑
CREATE TABLE IF NOT EXISTS documents (
  doc_key     TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  authors     TEXT NOT NULL DEFAULT '',        -- JSON 数组
  year        INTEGER NOT NULL DEFAULT 0,
  venue       TEXT NOT NULL DEFAULT '',
  doi         TEXT NOT NULL DEFAULT '',
  abstract    TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '[]',
  status      TEXT NOT NULL DEFAULT 'unread',  -- unread | reading | read
  extra       TEXT NOT NULL DEFAULT '{}',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
"""


def get_db():
    """取当前线程的连接（首次调用时创建）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        parent = os.path.dirname(DB_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(DB_FILE, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def _ensure_event_columns(conn):
    """给旧库补列（SQLite 的 ALTER TABLE 不支持 IF NOT EXISTS）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    for name, ddl in (
        ("source", "TEXT NOT NULL DEFAULT 'calendar'"),
        ("source_id", "TEXT NOT NULL DEFAULT ''"),
        ("session_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN %s %s" % (name, ddl))


def _ensure_memory_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_items)").fetchall()}
    if "title" not in cols:
        conn.execute("ALTER TABLE memory_items ADD COLUMN title TEXT NOT NULL DEFAULT ''")


def _ensure_task_columns(conn):
    """给旧库补任务字段（优先级 / 标签 / 截止 / 父子 / 依赖 / 重复 / 排序）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    added = False
    for name, ddl in (
        ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ("tags", "TEXT NOT NULL DEFAULT ''"),
        ("due_at", "REAL NOT NULL DEFAULT 0"),
        ("parent_id", "TEXT NOT NULL DEFAULT ''"),
        ("depends_on", "TEXT NOT NULL DEFAULT ''"),
        ("repeat_rule", "TEXT NOT NULL DEFAULT ''"),
        ("estimate_min", "INTEGER NOT NULL DEFAULT 0"),
        ("sort_order", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (name, ddl))
            added = True
    if added:
        # 老任务：截止时间原来只存在 meta 里，排序值用 updated_at 兜底
        rows = conn.execute("SELECT id, meta, updated_at, due_at, sort_order FROM tasks").fetchall()
        for r in rows:
            due = float(r["due_at"] or 0)
            if not due:
                try:
                    due = float((json.loads(r["meta"] or "{}") or {}).get("due_at") or 0)
                except (ValueError, TypeError):
                    due = 0
            order = float(r["sort_order"] or 0) or float(r["updated_at"] or 0)
            conn.execute("UPDATE tasks SET due_at=?, sort_order=? WHERE id=?",
                         (due, order, r["id"]))
    # 索引必须等列补齐后再建：老库执行 SCHEMA 时还没有 due_at
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at)")


def init_db():
    """建表（幂等）。进程启动时调用一次。"""
    global _inited
    with _init_lock:
        if _inited:
            return
        conn = get_db()
        conn.executescript(SCHEMA)
        _ensure_event_columns(conn)
        _ensure_memory_columns(conn)
        _ensure_task_columns(conn)
        conn.commit()
        _inited = True


def close_current():
    """关闭当前线程的连接（测试切换数据库时用）"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def configure(path):
    """把数据库指向另一个文件（仅供测试）。

    会关掉当前线程的旧连接并重跑建表；其他线程的连接不受影响，
    因此测试应在单线程里使用。
    """
    global DB_FILE, _inited
    close_current()
    DB_FILE = os.path.abspath(path)
    _inited = False
    init_db()
