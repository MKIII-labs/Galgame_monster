# -*- coding: utf-8 -*-
"""研究记录：研究完成后把完整报告落盘，供研究道场回看与再次导出。

与「进行中会话」的区别：
- 会话在内存里，2 小时过期；
- 研究记录是完成后的档案，写 `data/dojo/research_records.json`，重启不丢。

只保存服务端已经生成的报告，不接收前端拼装的内容。
"""
import copy
import os
import threading
import time
import uuid

from app.store import DATA_DIR, drop_cache, read_json, write_json

RECORDS_FILE = os.path.join(DATA_DIR, "dojo", "research_records.json")
MAX_RECORDS = 200
_LOCK = threading.RLock()


def _load():
    data = read_json(RECORDS_FILE, {"records": []})
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return {"records": []}
    return data


def _summary(rec):
    return {
        "id": rec.get("id") or "",
        "inquiry_id": rec.get("inquiry_id") or "",
        "title": rec.get("title") or "",
        "subject": rec.get("subject") or "",
        "track": rec.get("track") or "",
        "verdict": rec.get("verdict") or "",
        "avg_score": rec.get("avg_score") or 0,
        "coverage": rec.get("coverage") or 0,
        "finished_at": int(rec.get("finished_at") or 0),
    }


def save(report):
    """保存一份完成报告；返回记录摘要。"""
    if not isinstance(report, dict) or not report.get("inquiry_id"):
        raise ValueError("报告缺少 inquiry_id")
    artifact = report.get("artifact") or {}
    rec = {
        "id": "rr_" + uuid.uuid4().hex[:12],
        "inquiry_id": report.get("inquiry_id") or "",
        "title": report.get("title") or "",
        "subject": report.get("subject") or "",
        "track": report.get("track") or "",
        "finished_at": int(time.time()),
        "verdict": report.get("verdict") or "",
        "avg_score": report.get("avg_score") or 0,
        "coverage": artifact.get("coverage") or 0,
        "report": copy.deepcopy(report),
    }
    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)
    with _LOCK:
        data = _load()
        data["records"].insert(0, rec)
        data["records"] = data["records"][:MAX_RECORDS]
        write_json(RECORDS_FILE, data, indent=None)
    return _summary(rec)


def list_records():
    with _LOCK:
        rows = [_summary(r) for r in _load().get("records") or []]
    rows.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
    return rows


def get_record(record_id):
    rid = str(record_id or "")
    if not rid:
        raise ValueError("缺少记录 id")
    with _LOCK:
        for rec in _load().get("records") or []:
            if rec.get("id") == rid:
                return copy.deepcopy(rec.get("report") or {})
    raise ValueError("没有这条研究记录")


def delete_record(record_id):
    rid = str(record_id or "")
    if not rid:
        raise ValueError("缺少记录 id")
    with _LOCK:
        data = _load()
        keep = [r for r in data.get("records") or [] if r.get("id") != rid]
        if len(keep) == len(data.get("records") or []):
            raise ValueError("没有这条研究记录")
        data["records"] = keep
        write_json(RECORDS_FILE, data, indent=None)
    return {"deleted": rid}


def reset_store():
    """测试用：删掉记录文件与读缓存。"""
    with _LOCK:
        drop_cache(RECORDS_FILE)
        try:
            os.remove(RECORDS_FILE)
        except OSError:
            pass
