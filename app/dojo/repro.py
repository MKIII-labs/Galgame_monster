# -*- coding: utf-8 -*-
"""复现实验：给数据 + 半截代码，跑用户写的 solution，把 stdout 的 JSON 与论文值比对。

安全边界（本地单用户工具，风险等同本地 Jupyter）：
- 每次运行新建临时目录，数据文件复制进去，运行完删除；
- 子进程 `python -I`（忽略 PYTHONPATH / 用户 site），超时强杀；
- stdout / stderr 截断，避免刷爆接口；
- 只允许跑当前复现包里的任务，不执行任意系统命令入口。

训练定位：目标数字是公开的，用户不能靠猜；要过必须真的把数算出来。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from app import winproc
from app.store import DATA_DIR, RESOURCES_DIR

REPRO_DIRS = (
    os.path.join(RESOURCES_DIR, "dojo", "repro"),
    os.path.join(DATA_DIR, "dojo", "repro"),
)
USER_REPRO_DIR = os.path.join(DATA_DIR, "dojo", "repro")
MAX_CODE_BYTES = 200 * 1024
MAX_OUTPUT = 32 * 1024
MAX_TIMEOUT = 30
DEFAULT_TIMEOUT = 10
MAX_DATA_BYTES = 2 * 1024 * 1024
_DATA_EXTS = (".csv", ".txt", ".tsv", ".json", ".md")


class ReproError(Exception):
    pass


def _dirs():
    return [d for d in REPRO_DIRS if os.path.isdir(d)]


def _load_one(pack_dir):
    path = os.path.join(pack_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception as e:
        raise ReproError("manifest 读取失败：%s" % e) from None
    if not isinstance(m, dict):
        raise ReproError("manifest 不是对象")
    for key in ("id", "title", "question", "expected", "data", "starter"):
        if not m.get(key):
            raise ReproError("manifest 缺少 %s" % key)
    if not isinstance(m.get("data"), list) or not isinstance(m.get("expected"), dict):
        raise ReproError("manifest 的 data / expected 类型不对")
    for name in m["data"]:
        p = os.path.join(pack_dir, os.path.basename(str(name)))
        if not os.path.isfile(p):
            raise ReproError("数据文件不存在：%s" % name)
    starter = os.path.join(pack_dir, os.path.basename(str(m["starter"])))
    if not os.path.isfile(starter):
        raise ReproError("starter 文件不存在：%s" % m["starter"])
    m["_dir"] = pack_dir
    return m


def load_packs():
    out = []
    for d in _dirs():
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isdir(p) or name.startswith("."):
                continue
            try:
                out.append(_load_one(p))
            except ReproError as e:
                print("[复现] 跳过 %s：%s" % (name, e))
    return out


def get_pack(pack_id):
    for p in load_packs():
        if p["id"] == pack_id:
            return p
    raise ReproError("没有这个复现实验：%s" % pack_id)


def _read_text(path, limit=2000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def public_pack(pack):
    """题面：公开目标值、数据预览与 starter；不返回参考解。"""
    data = []
    for name in pack["data"]:
        base = os.path.basename(str(name))
        data.append({"name": base, "preview": _read_text(os.path.join(pack["_dir"], base))})
    starter_path = os.path.join(pack["_dir"], os.path.basename(str(pack["starter"])))
    return {
        "id": pack["id"],
        "title": pack.get("title") or pack["id"],
        "source": pack.get("source") or "",
        "question": pack.get("question") or "",
        "expected": pack["expected"],
        "time_limit": int(pack.get("time_limit") or DEFAULT_TIMEOUT),
        "data": data,
        "starter": _read_text(starter_path, 20000),
    }


def _parse_json_line(stdout):
    """从最后一行往前找第一行合法 JSON 对象。"""
    for line in reversed(str(stdout or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _env():
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _clip(text):
    s = str(text or "")
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + "\n…（输出已截断）"


def _execute(pack_dir, data_names, expected, time_limit, code):
    """在临时目录跑一段代码，并与 expected 逐字段比对。"""
    code = str(code or "")
    if not code.strip():
        raise ReproError("代码是空的")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ReproError("代码太大（上限 200KB）")

    limit = max(1, min(MAX_TIMEOUT, int(time_limit or DEFAULT_TIMEOUT)))
    tmp = tempfile.mkdtemp(prefix="dojo-repro-")
    started = time.time()
    timed_out = False
    stdout = stderr = ""
    rc = -1
    try:
        for name in data_names:
            base = os.path.basename(str(name))
            shutil.copyfile(os.path.join(pack_dir, base), os.path.join(tmp, base))
        with open(os.path.join(tmp, "solution.py"), "w", encoding="utf-8") as f:
            f.write(code)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "solution.py"],
                cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=limit, env=_env(),
                creationflags=winproc.no_window(),
            )
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
            stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            rc = -1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    elapsed = int((time.time() - started) * 1000)
    if timed_out:
        return {
            "passed": False, "fields": [], "stdout": _clip(stdout),
            "stderr": _clip(stderr), "returncode": rc, "elapsed_ms": elapsed,
            "timed_out": True, "error": "运行超时（%d 秒）" % limit,
        }

    parsed = _parse_json_line(stdout)
    if parsed is None:
        return {
            "passed": False, "fields": [], "stdout": _clip(stdout),
            "stderr": _clip(stderr), "returncode": rc, "elapsed_ms": elapsed,
            "timed_out": False,
            "error": "stdout 里没有找到 JSON；请用 print(json.dumps({...})) 输出结果",
        }

    fields = []
    all_ok = True
    for name, spec in expected.items():
        if isinstance(spec, dict):
            want = spec.get("value")
            tol = float(spec.get("tolerance", 1e-6) or 1e-6)
        else:
            want = spec
            tol = 1e-6
        actual = parsed.get(name)
        ok = False
        try:
            ok = abs(float(actual) - float(want)) <= abs(tol) + 1e-12
        except (TypeError, ValueError):
            ok = False
        all_ok = all_ok and ok
        fields.append({
            "name": name,
            "expected": want,
            "actual": actual,
            "tolerance": tol,
            "ok": ok,
        })
    return {
        "passed": bool(all_ok and rc == 0),
        "fields": fields,
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
        "returncode": rc,
        "elapsed_ms": elapsed,
        "timed_out": False,
        "error": "" if rc == 0 else "程序退出码非 0",
    }


def run(pack_id, code):
    pack = get_pack(pack_id)
    return _execute(pack["_dir"], pack["data"], pack["expected"],
                    pack.get("time_limit"), code)


# ---------------- 编辑器：保存前用参考解自检 ----------------

def _safe_data_name(name):
    base = os.path.basename(str(name or "").strip())
    if not base or len(base) > 80:
        raise ReproError("数据文件名不合法")
    if not re.match(r"^[\w.\- ]+$", base):
        raise ReproError("数据文件名只能包含字母、数字、下划线、点、短横线")
    ext = os.path.splitext(base)[1].lower()
    if ext not in _DATA_EXTS:
        raise ReproError("数据文件只支持 %s" % " / ".join(_DATA_EXTS))
    return base


def _new_id(title):
    base = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:24]
    digest = hashlib.sha1(str(title or "").encode("utf-8")).hexdigest()[:6]
    pid = (base + "-" if base else "repro-") + digest
    alive = {p["id"] for p in load_packs()}
    n = 2
    while pid in alive or os.path.isdir(os.path.join(USER_REPRO_DIR, pid)):
        pid = "%s-%d" % ((base or "repro")[:58], n)
        n += 1
    return pid


def create_pack(payload):
    """从编辑器保存一个个人复现包；参考解必须先跑出目标值，否则整包拒绝。"""
    if not isinstance(payload, dict):
        raise ReproError("提交内容不是对象")
    title = str(payload.get("title") or "").strip()[:60]
    question = str(payload.get("question") or "").strip()[:1000]
    source = str(payload.get("source") or "").strip()[:120]
    starter = str(payload.get("starter") or "")
    reference = str(payload.get("reference") or "")
    if not title:
        raise ReproError("标题不能为空")
    if not question:
        raise ReproError("任务描述不能为空")
    if not starter.strip():
        raise ReproError("starter 代码不能为空")
    if not reference.strip():
        raise ReproError("参考解不能为空（要用它自检目标值）")

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ReproError("至少需要一个数据文件")
    files = []
    total = 0
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise ReproError("数据文件格式不对")
        name = _safe_data_name(item.get("name"))
        content = str(item.get("content") or "")
        if not content.strip():
            raise ReproError("数据文件 %s 是空的" % name)
        if name in seen:
            raise ReproError("数据文件名重复：%s" % name)
        seen.add(name)
        total += len(content.encode("utf-8"))
        if total > MAX_DATA_BYTES:
            raise ReproError("数据总大小超过 2MB")
        files.append((name, content))

    expected_raw = payload.get("expected")
    if not isinstance(expected_raw, dict) or not expected_raw:
        raise ReproError("至少需要一个目标字段")
    expected = {}
    for name, spec in expected_raw.items():
        key = str(name or "").strip()
        if not key:
            raise ReproError("目标字段名不能为空")
        try:
            value = float(spec.get("value") if isinstance(spec, dict) else spec)
        except (TypeError, ValueError):
            raise ReproError("目标字段 %s 的值必须是数字" % key) from None
        try:
            tol = abs(float((spec or {}).get("tolerance", 1e-6)))
        except (TypeError, ValueError):
            tol = 1e-6
        expected[key] = {"value": value, "tolerance": tol or 1e-6}

    try:
        limit = int(payload.get("time_limit") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT
    limit = max(1, min(MAX_TIMEOUT, limit))

    pid = _new_id(title)
    os.makedirs(USER_REPRO_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="dojo-repro-new-", dir=USER_REPRO_DIR)
    try:
        for name, content in files:
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(content)
        with open(os.path.join(tmp, "starter.py"), "w", encoding="utf-8") as f:
            f.write(starter)
        with open(os.path.join(tmp, "reference.py"), "w", encoding="utf-8") as f:
            f.write(reference)
        manifest = {
            "id": pid,
            "title": title,
            "source": source,
            "question": question,
            "data": [name for name, _ in files],
            "starter": "starter.py",
            "time_limit": limit,
            "expected": expected,
        }
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        check = _execute(tmp, manifest["data"], expected, limit, reference)
        if not check["passed"]:
            detail = check.get("error") or "目标值没有对上"
            fields = check.get("fields") or []
            if fields:
                detail += "；" + "，".join(
                    "%s 期望 %s 实际 %s" % (x["name"], x["expected"], x["actual"])
                    for x in fields)
            raise ReproError("参考解自检没通过：" + detail)

        final = os.path.join(USER_REPRO_DIR, pid)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"id": pid, "title": title, "check": check}
