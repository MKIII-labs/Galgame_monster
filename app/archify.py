# -*- coding: utf-8 -*-
"""Archify 集成：把结构化图描述渲染成「可验证」的交互式 HTML。

`vendor/archify` 是上游 skill 的原样拷贝（v3.2.0，Apache-2.0 / MIT 双许可）。
本模块只做三件事，不重写渲染器：

- `schema`：按图型返回 schema + 官方示例（模型不需要访问 vendor 目录）；
- `finalize`：写候选文件，调用 `node bin/archify.mjs finalize`，
  依次过 validate → 原子写入 → 严格 provenance 检查 → 真实浏览器检查；
- `doctor`：环境自检（Node 版本、全部图型、浏览器）。

升级方式：替换 `vendor/archify` 并更新 `.archify-vendor.json`，本模块不用改。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, "vendor", "archify")
BIN = os.path.join(SKILL_DIR, "bin", "archify.mjs")
PACKAGE = os.path.join(SKILL_DIR, "package.json")

DIAGRAM_TYPES = ("architecture", "workflow", "sequence", "dataflow", "lifecycle",
                 "erd", "tree", "class", "timeline", "waterfall", "explainer")
MARKDOWN_TYPES = ("explainer",)          # 其余图型都用 JSON 候选

MAX_CANDIDATE = 4 * 1024 * 1024          # 候选文件上限
MAX_SCHEMA_CHARS = 26000                 # schema + 示例回灌上限（工具结果本身还会再截断）
MAX_EXAMPLE_CHARS = 12000
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 600
GATE_ORDER = ("validate", "deliver", "check", "browser-check")


def _read(path, cap=0):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if cap and len(text) > cap:
        text = text[:cap] + "\n…（已截断）"
    return text


def version():
    try:
        with open(PACKAGE, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("version") or "")
    except (OSError, ValueError):
        return ""


def _node():
    return shutil.which("node") or ""


def available():
    """返回 (ok, 原因)。缺 Node / 缺 vendor 时给出能直接照做的提示。"""
    if not os.path.isfile(BIN):
        return False, ("没有找到 vendor/archify：把上游 skill 解压到 "
                       "vendor/archify（保留 bin/、schemas/、renderers/、LICENSE）。")
    node = _node()
    if not node:
        return False, "没有找到 Node.js（Archify 需要 node >= 18）；装好 Node 后重试。"
    return True, ""


_ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
             "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL")


def _env():
    """只带 Node 运行必需的环境变量，不把 API Key 等敏感项传给子进程。"""
    e = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    e["ARCHIFY_UPDATE_CHECK_DISABLED"] = "1"      # 离线确定性：不做联网更新检查
    try:
        from app import webprobe
        edge = webprobe.edge_path()
        if edge and not e.get("ARCHIFY_CHROME"):
            e["ARCHIFY_CHROME"] = edge
    except Exception:                             # noqa: BLE001  浏览器探测失败不影响运行
        pass
    return e


def _run(args, cwd, timeout):
    node = _node()
    if not node:
        return None, "", "没有找到 Node.js（需要 >= 18）"
    try:
        p = subprocess.run(
            [node, BIN] + list(args), cwd=cwd, env=_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", "Archify 超时（%s 秒）" % timeout
    except OSError as e:
        return None, "", "无法启动 node：%s" % e
    return p.returncode, p.stdout or "", p.stderr or ""


def _json_receipt(text):
    """CLI 的 --json 会打印一行 JSON；失败时可能混有其它输出。"""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("command"):
            return obj
    i, j = str(text or "").find("{"), str(text or "").rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(text[i:j + 1])
            if isinstance(obj, dict) and obj.get("command"):
                return obj
        except ValueError:
            pass
    return None


def schema_text(kind, with_example=True):
    """返回某图型的 schema + 一份官方示例，供模型照形状写候选。"""
    kind = str(kind or "").strip().lower()
    if kind not in DIAGRAM_TYPES:
        return "❌ 不支持的图型：%s（可用：%s）" % (kind or "空", "、".join(DIAGRAM_TYPES))
    parts, budget = [], MAX_SCHEMA_CHARS
    for name in ("common.schema.json", "%s.schema.json" % kind):
        body = _read(os.path.join(SKILL_DIR, "schemas", name), cap=budget)
        if not body:
            return "❌ vendor/archify 缺少 schema：%s" % name
        parts.append("### %s\n%s" % (name, body))
        budget -= len(body)
    head = ["【Archify schema】%s · v%s" % (kind, version() or "?")]
    if kind in MARKDOWN_TYPES:
        head.append("候选是 Markdown（.archify.md），不是 JSON。")
    else:
        head.append("候选是 JSON；示例只学形状，节点与事实要来自当前任务。")
    parts = head + parts
    if with_example and budget > 1000:
        # 官方示例按文件名排序，取第一份放得下的
        for path in sorted(glob.glob(os.path.join(SKILL_DIR, "examples", "*.%s.json" % kind))):
            body = _read(path, cap=min(budget, MAX_EXAMPLE_CHARS))
            if not body:
                continue
            parts.append("### 示例 %s\n%s" % (os.path.basename(path), body))
            break
    return "\n\n".join(parts)


def _fmt_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    return ("%.0f B" % n) if n < 1024 else ("%.1f KB" % (n / 1024.0))


def _diagnostics_text(rep):
    rows = []
    for d in (rep.get("diagnostics") or [])[:6]:
        if not isinstance(d, dict):
            rows.append(str(d)[:300])
            continue
        code = str(d.get("code") or "").strip()
        msg = str(d.get("message") or "").strip()
        fixes = [str(x) for x in (d.get("supportedFixes") or [])][:2]
        line = (code + "：" if code else "") + msg[:400]
        if fixes:
            line += "\n  修复：" + "；".join(f[:200] for f in fixes)
        rows.append(line)
    return "\n".join(rows[:6])


def report_text(rep, output_path, stderr=""):
    """把 finalize 回执压成模型能读的文本 + 验收指标。"""
    rep = rep if isinstance(rep, dict) else {}
    ok = bool(rep.get("ok"))
    gates = rep.get("gates") or {}
    passed = sum(1 for k in GATE_ORDER if gates.get(k) == "pass")
    artifact = rep.get("artifact") or {}
    duration = rep.get("durationMs")
    lines = ["【Archify】%s · %s" % (rep.get("type") or "?", "通过" if ok else "未通过")]
    lines.append("门槛：" + " · ".join(
        "%s %s" % (k, "✓" if gates.get(k) == "pass" else ("—" if gates.get(k) == "not-run" else "✗"))
        for k in GATE_ORDER))
    if ok:
        lines.append("产物：%s · %s · 用时 %s ms"
                     % (artifact.get("path") or output_path,
                        _fmt_bytes(artifact.get("bytes")), duration))
        lines.append("自包含 HTML，可直接在浏览器打开；预览时用独立窗口 / iframe（含交互脚本）。")
    else:
        stage = rep.get("failedStage") or "?"
        lines.append("失败阶段：%s" % stage)
        diag = _diagnostics_text(rep)
        if diag:
            lines.append("诊断：\n" + diag)
        nxt = rep.get("nextAction") or {}
        if isinstance(nxt, dict) and nxt.get("constraint"):
            lines.append("修复约束：" + str(nxt.get("constraint"))[:300])
        tail = str(stderr or "").strip()
        if tail and not diag:
            lines.append("stderr：\n" + tail[-1200:])
        elif tail and "internal/unclassified" in diag:
            # 渲染器内部崩溃时上游不回结构化原因，stderr 是唯一线索
            lines.append("stderr：\n" + tail[-800:])
    rec = rep.get("visualReviewRecommendation") or {}
    if ok and isinstance(rec, dict) and rec.get("hints"):
        lines.append("布局提示：" + "；".join(str(x) for x in rec["hints"][:2])[:400])
    upd = rep.get("update") or {}
    if isinstance(upd, dict) and upd.get("noticeRequired") and upd.get("noticeText"):
        lines.append("更新提示：" + str(upd["noticeText"])[:300])
    metrics = {
        "ok": 1 if ok else 0,
        "gates_passed": passed,
        "artifact_bytes": int(artifact.get("bytes") or 0),
        "duration_ms": int(duration or 0),
    }
    lines.append("__ARCHIFY_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    return "\n".join(lines)


def finalize(kind, candidate_path, output_path, sandbox, quality="showcase",
             repo_root="", timeout=DEFAULT_TIMEOUT):
    """跑一次完整 finalize；返回 (给模型看的文本, 回执 dict)。"""
    ok, why = available()
    if not ok:
        return "❌ " + why, {}
    kind = str(kind or "").strip().lower()
    if kind not in DIAGRAM_TYPES:
        return "❌ 不支持的图型：%s（可用：%s）" % (kind or "空", "、".join(DIAGRAM_TYPES)), {}
    if not os.path.isfile(candidate_path):
        return "❌ 候选文件不存在：%s" % candidate_path, {}
    try:
        size = os.path.getsize(candidate_path)
    except OSError as e:
        return "❌ 读候选文件失败：%s" % e, {}
    if size > MAX_CANDIDATE:
        return ("❌ 候选文件太大（%.1f MB），上限 %d MB。"
                % (size / 1048576.0, MAX_CANDIDATE // 1048576)), {}
    q = "standard" if str(quality or "").strip().lower() == "standard" else "showcase"
    try:
        timeout = max(10, min(MAX_TIMEOUT, int(timeout or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    args = ["finalize", kind, candidate_path, output_path, "--json", "--quality", q]
    if repo_root:
        args += ["--repo-root", repo_root]
    rep = None
    for attempt in range(2):
        rc, out, err = _run(args, sandbox, timeout)
        rep = _json_receipt(out)
        if rep is None:
            tail = (out or err or "").strip()[-1500:]
            return ("❌ Archify 没有返回 JSON 回执（exit=%s）。\n%s" % (rc, tail)), {}
        if rep.get("ok"):
            break
        # 渲染器偶发内部崩溃（机器忙 / 浏览器进程残留时出现过）：只重试这一类
        codes = [str((d or {}).get("code") or "") for d in (rep.get("diagnostics") or [])
                 if isinstance(d, dict)]
        if attempt == 0 and "internal/unclassified" in codes:
            time.sleep(0.6)
            continue
        break
    return report_text(rep, output_path, err), rep


def doctor():
    ok, why = available()
    if not ok:
        return "❌ " + why
    rc, out, err = _run(["doctor"], SKILL_DIR, 120)
    body = (out or err or "").strip()
    return body[-3000:] if body else "❌ doctor 没有输出（exit=%s）" % rc


def default_output(kind, sandbox):
    """没给 output 时的默认位置：沙箱内 .archify/<type>-<时间>/<type>.html。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(kind or "diagram"))[:30]
    return os.path.join(sandbox, ".archify", "%s-%s" % (safe, stamp), "%s.html" % safe)


def prepare_inline(kind, text, sandbox):
    """把模型内联给的候选写到沙箱内，返回候选文件绝对路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(kind or "diagram"))[:30]
    folder = os.path.join(sandbox, ".archify", "%s-%s" % (safe, stamp))
    os.makedirs(folder, exist_ok=True)
    ext = ".archify.md" if kind in MARKDOWN_TYPES else ".json"
    path = os.path.join(folder, "candidate" + ext)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(text or ""))
    return path
