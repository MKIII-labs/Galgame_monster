# -*- coding: utf-8 -*-
"""网页渲染自检（render_check）：用无头 Edge 真的打开沙箱里的 HTML，
把「看得见的问题」变成纯文本回灌模型。

为什么需要它（NOTE 43.54 / 43.78）：模型能跑 node / pytest，但看不见画面。
历史事故是 canvas 0×0、变量未定义导致首帧 ReferenceError —— 桩环境（自造 DOM）
能抓后者，抓不到前者，因为桩没有排版引擎。

探针只回答**可判定**的问题：运行时错误、溢出首屏、被遮挡、字号过小、
对比度不足、零尺寸容器、点击区过小。主观美观交给可选的截图 + 视觉模型。

链路：HTML 副本（注入探针）→ msedge --headless=new --dump-dom → 读 <pre id="__probe">
"""
import base64
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import tempfile

from app import imaging

# 常见安装位置；GAL_EDGE 环境变量可覆盖（自定义路径 / 换 Chrome）
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
DEFAULT_VIEWPORT = (1280, 800)
MIN_W, MIN_H = 320, 240          # 比手机还窄的视口没有参考价值
MAX_W, MAX_H = 2560, 1600
WAIT_MS = 400                    # 页面 load 之后再等这么久，让首屏动画 / 异步初始化跑一遍
MAX_ELEMENTS = 3000              # 只扫前这么多节点，避免大页面把自检拖到几十秒
MAX_LOGS = 10
DESC_MAX = 1200                  # 视觉描述回灌上限

PROBE_ID = "__probe"

# ---------------------------------------------------------------- 探针脚本
# 注入在 <head> 之后：尽早接管 console/error，才能抓到页面自己的报错。
_PROBE_HEAD = r"""
<script>
(function () {
  var P = window.__galProbe = { logs: [] };
  function add(kind, msg) {
    if (P.logs.length < 40) P.logs.push(kind + ": " + String(msg).slice(0, 200));
  }
  window.addEventListener("error", function (e) {
    var where = e.filename ? " @ " + String(e.filename).split("/").pop() + ":" + e.lineno : "";
    add("error", (e.message || "脚本错误") + where);
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    add("promise", (r && (r.message || r)) || "未处理的 Promise 拒绝");
  });
  ["error", "warn"].forEach(function (k) {
    var old = console[k];
    console[k] = function () {
      try { add(k, [].slice.call(arguments).map(String).join(" ")); } catch (_) {}
      return old.apply(console, arguments);
    };
  });
})();
</script>
"""

# 注入在 </body> 之前：load 之后统一采集，结果序列化进 DOM（--dump-dom 才读得到）。
_PROBE_TAIL = r"""
<script>
(function () {
  var MSG = window.__galProbe || { logs: [] };
  function txt(el) {
    var t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.length > 24 ? t.slice(0, 24) + "…" : t;
  }
  function sel(el) {
    if (!el || el.nodeType !== 1) return "?";
    var s = el.tagName.toLowerCase();
    if (el.id) return s + "#" + el.id;
    var cls = (typeof el.className === "string" ? el.className : "").trim();
    if (cls) s += "." + cls.split(/\s+/).slice(0, 2).join(".");
    return s;
  }
  function parseRgb(v) {
    var m = String(v || "").match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    var p = m[1].split(",").map(function (x) { return parseFloat(x); });
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum(c) {
    function f(x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function ratio(a, b) {
    var la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  // 向上找第一个不透明的背景色；遇到背景图就放弃（算不准就别误报）
  function bgOf(el) {
    var n = el;
    while (n && n.nodeType === 1) {
      var st = getComputedStyle(n);
      if (st.backgroundImage && st.backgroundImage !== "none") return null;
      var c = parseRgb(st.backgroundColor);
      if (c && c.a > 0.5) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }
  function push(arr, item, cap) { if (arr.length < (cap || 6)) arr.push(item); }

  function run() {
    var vw = innerWidth, vh = innerHeight;
    var out = { vw: vw, vh: vh, logs: MSG.logs || [], issues: [], counts: {} };
    var all = document.body ? document.body.getElementsByTagName("*") : [];
    out.counts.elements = all.length;
    out.counts.scrollHeight = document.documentElement.scrollHeight;
    out.counts.done = 0;
    var zero = [], over = [], cover = [], small = [], low = [], tiny = [];
    var INTER = /^(button|a|input|select|textarea)$/i;
    var n = Math.min(all.length, """ + str(MAX_ELEMENTS) + r""");
    for (var i = 0; i < n; i++) {
      var el = all[i];
      if (el.id === """ + json.dumps(PROBE_ID) + r""") continue;
      var st = getComputedStyle(el);
      if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) continue;
      var r = el.getBoundingClientRect();
      var tag = el.tagName.toUpperCase();
      out.counts.done = i + 1;
      // 画布 / 视频 / iframe 零尺寸：绘图全部落在不可见区域（历史黑屏根因）
      if ((tag === "CANVAS" || tag === "VIDEO" || tag === "IFRAME") && (r.width < 2 || r.height < 2)) {
        push(zero, sel(el) + " " + Math.round(r.width) + "×" + Math.round(r.height));
        continue;
      }
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.right > vw + 2 || r.bottom > vh + 2) {
        if (r.top < vh && r.left < vw) {
          push(over, sel(el) + " 右/下 " + Math.round(r.right) + "/" + Math.round(r.bottom));
        }
      }
      var inter = INTER.test(tag) || el.getAttribute("role") === "button" || st.cursor === "pointer";
      if (inter) {
        if (r.width < 24 || r.height < 24) {
          push(tiny, sel(el) + " " + Math.round(r.width) + "×" + Math.round(r.height));
        }
        if (r.top >= 0 && r.top < vh && r.left >= 0 && r.left < vw) {
          var cx = Math.min(vw - 1, r.left + r.width / 2);
          var cy = Math.min(vh - 1, r.top + r.height / 2);
          var hit = document.elementFromPoint(cx, cy);
          if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
            push(cover, sel(el) + " ← 被 " + sel(hit) + " 挡住");
          }
        }
      }
      var hasText = false;
      for (var k = 0; k < el.childNodes.length; k++) {
        var nd = el.childNodes[k];
        if (nd.nodeType === 3 && nd.nodeValue.trim()) { hasText = true; break; }
      }
      if (!hasText) continue;
      var fs = parseFloat(st.fontSize) || 0;
      if (fs > 0 && fs < 12) push(small, Math.round(fs) + "px「" + txt(el) + "」");
      var fg = parseRgb(st.color), bg = bgOf(el);
      if (fg && bg && fg.a > 0.5) {
        var cr = ratio(fg, bg);
        if (cr < 3.0) push(low, cr.toFixed(1) + "「" + txt(el) + "」");
      }
    }
    out.issues = [
      { k: "零尺寸容器", v: zero },
      { k: "溢出首屏", v: over },
      { k: "被遮挡", v: cover },
      { k: "字号过小", v: small },
      { k: "对比度不足", v: low },
      { k: "点击区过小", v: tiny }
    ];
    var pre = document.createElement("pre");
    pre.id = """ + json.dumps(PROBE_ID) + r""";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function go() { setTimeout(run, """ + str(WAIT_MS) + r"""); }
  if (document.readyState === "complete") go();
  else window.addEventListener("load", go);
})();
</script>
"""


# ============================================================
# 1. 环境与参数
# ============================================================

def edge_path():
    """找一个可用的 Chromium 内核浏览器；找不到返回空串。"""
    env = (os.environ.get("GAL_EDGE") or "").strip()
    for p in ([env] if env else []) + list(EDGE_CANDIDATES):
        if p and os.path.isfile(p):
            return p
    return ""


def parse_viewport(spec):
    """把 "1280x800" / "1280,800" / None 收敛成 (w, h)，并夹到安全范围。"""
    m = re.match(r"^\s*(\d{2,5})\s*[xX,，*]\s*(\d{2,5})\s*$", str(spec or ""))
    if not m:
        return DEFAULT_VIEWPORT
    w = max(MIN_W, min(MAX_W, int(m.group(1))))
    h = max(MIN_H, min(MAX_H, int(m.group(2))))
    return w, h


# ============================================================
# 2. HTML 注入
# ============================================================

def _read_html(path):
    """读源码并统一成 UTF-8：dump-dom 的序列化结果按 UTF-8 解码。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gbk", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def inject_probe(text, base_dir):
    """注入 <base>（副本在临时目录，相对资源要指回原目录）与探针脚本。"""
    text = str(text or "")
    base = '<base href="%s/">' % _dir_uri(base_dir)
    if not re.search(r"<meta[^>]+charset", text, re.I):
        base += '<meta charset="utf-8">'
    m = re.search(r"<head\b[^>]*>", text, re.I)
    if m:
        text = text[:m.end()] + base + _PROBE_HEAD + text[m.end():]
    else:
        text = "<head>" + base + _PROBE_HEAD + "</head>" + text
    low = text.lower()
    i = low.rfind("</body>")
    if i >= 0:
        text = text[:i] + _PROBE_TAIL + text[i:]
    else:
        text += _PROBE_TAIL
    return text


def _dir_uri(path):
    """目录 → file:/// URI（处理盘符、空格与非 ASCII）。"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    out = []
    for ch in p:
        if ch.isalnum() or ch in "/:-._~":
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "file://" + "".join(out)


# ============================================================
# 3. 启动浏览器 / 读回结果
# ============================================================

def _run(edge, args, timeout):
    """跑一次浏览器；Windows 下不弹控制台窗口。"""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([edge] + args, capture_output=True, timeout=timeout,
                          creationflags=flags)


def extract_probe(dom):
    """从 dump-dom 结果里取出探针 JSON；没有就返回 None。

    取**最后**一个同 id 的元素：探针是追加到 body 末尾的，页面自己若也用了
    这个 id，先出现的那个不是我们的结果。
    """
    hits = list(re.finditer(r'<pre id="%s">(.*?)</pre>' % PROBE_ID, dom or "", re.S))
    if not hits:
        return None
    try:
        return json.loads(htmllib.unescape(hits[-1].group(1)))
    except ValueError:
        return None


def _dump_dom(edge, url, profile, vw, vh, timeout):
    """--dump-dom 取 DOM。NOTE 43.58 记录过它偶发空输出，所以失败重试一次（换 profile）。"""
    args = ["--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--hide-scrollbars", "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (vw, vh),
            "--virtual-time-budget=%d" % (WAIT_MS + 2100),
            "--dump-dom", url]
    for _ in range(2):
        p = _run(edge, args, timeout)
        out = p.stdout.decode("utf-8", "replace")
        if extract_probe(out) is not None:
            return out, p.stderr.decode("utf-8", "replace")
    return out, p.stderr.decode("utf-8", "replace")


def _shot(edge, url, profile, vw, vh, png, timeout):
    args = ["--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (vw, vh),
            "--virtual-time-budget=%d" % (WAIT_MS + 2100),
            "--screenshot=" + png, url]
    _run(edge, args, timeout)
    if os.path.isfile(png) and os.path.getsize(png) > 0:
        with open(png, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    return ""


# ============================================================
# 4. 报告
# ============================================================

def format_report(data, name, viewport, extra=""):
    """把探针 JSON 翻成人话。纯函数，便于测试。"""
    d = data or {}
    vw, vh = viewport
    cnt = d.get("counts") or {}
    lines = ["【渲染自检】%s · 视口 %d×%d · DOM %d 节点 · 文档高 %dpx"
             % (name, vw, vh, int(cnt.get("elements") or 0), int(cnt.get("scrollHeight") or 0))]
    logs = [str(x) for x in (d.get("logs") or [])][:MAX_LOGS]
    if logs:
        lines.append("运行时 / 控制台（%d 条，最多列 %d）：" % (len(d.get("logs") or []), MAX_LOGS))
        lines += ["  - " + x for x in logs]
    else:
        lines.append("运行时错误：无")
    total = 0
    for item in d.get("issues") or []:
        vals = item.get("v") or []
        if not vals:
            continue
        total += len(vals)
        lines.append("%s（%d）：" % (item.get("k") or "问题", len(vals)))
        lines += ["  - " + str(v) for v in vals]
    if total == 0:
        lines.append("布局：未发现溢出 / 遮挡 / 字号 / 对比度 / 点击区问题")
    if int(cnt.get("elements") or 0) > MAX_ELEMENTS:
        lines.append("（只检查了前 %d 个元素）" % MAX_ELEMENTS)
    if extra:
        lines.append(extra)
    lines.append("提示：这是无头浏览器的确定性检查；配色是否好看、动效手感仍需人看。")
    return "\n".join(lines)


def _describe_shot(b64):
    """截图交给视觉模型；chat 通道不支持图片时自动回落 vision 通道。"""
    q = ("这是一个网页 / 小游戏的渲染截图。只描述你**看到**的，不要猜功能："
         "① 画面是否正常显示，有没有黑屏、空白、只显示部分区域；"
         "② 主要区块的位置关系，有没有重叠或错位；"
         "③ 文字是否清晰可读，配色是否过淡；④ 明显视觉缺陷。200 字以内。")
    try:
        text = imaging.recognize_image_fallback("image/png", b64, q)
    except Exception as e:                       # 视觉链路任何异常都不该让自检失败
        return "⚠️ 视觉描述失败：%s" % e
    text = (text or "").strip()
    return text[:DESC_MAX]


# ============================================================
# 5. 对外入口
# ============================================================

def check(abs_path, viewport=None, screenshot=False, timeout=90):
    """对沙箱里的一个 HTML 做渲染自检，返回给模型看的文本。"""
    edge = edge_path()
    if not edge:
        return ("⚠️ 没找到 Edge / Chrome，无法做渲染自检。"
                "可以设环境变量 GAL_EDGE 指向浏览器可执行文件。")
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % abs_path
    if not abs_path.lower().endswith((".html", ".htm")):
        return "❌ render_check 只支持 .html / .htm（当前：%s）" % os.path.basename(abs_path)
    vw, vh = parse_viewport(viewport)
    name = os.path.basename(abs_path)
    work = tempfile.mkdtemp(prefix="gal_probe_")
    try:
        copy = os.path.join(work, "page.html")
        with open(copy, "w", encoding="utf-8") as f:
            f.write(inject_probe(_read_html(abs_path), os.path.dirname(abs_path)))
        url = "file:///" + copy.replace("\\", "/").lstrip("/")
        dom, err = _dump_dom(edge, url, os.path.join(work, "prof"), vw, vh, timeout)
        data = extract_probe(dom)
        if data is None:
            tail = (err or "").strip().splitlines()[-3:]
            return ("⚠️ 浏览器没有回传探针结果（页面可能拒绝加载或浏览器会话异常）。\n"
                    "浏览器 stderr：%s\n"
                    "可先自己用浏览器打开 %s 看是否正常，或稍后重试。"
                    % (" / ".join(tail) or "（空）", name))
        report = format_report(data, name, (vw, vh))
        if screenshot:
            b64 = _shot(edge, url, os.path.join(work, "prof2"), vw, vh,
                        os.path.join(work, "shot.png"), timeout)
            if b64:
                report += "\n\n【视觉模型看到的画面】\n" + _describe_shot(b64)
            else:
                report += "\n\n【截图】⚠️ 浏览器没有产出截图文件（本机无头会话偶发失效，见 NOTE 43.58）"
        return report
    except subprocess.TimeoutExpired:
        return "❌ 渲染超时（%d 秒）：页面可能有死循环或一直在等网络资源。" % timeout
    except Exception as e:
        return "❌ 渲染自检出错：%s" % e
    finally:
        shutil.rmtree(work, ignore_errors=True)
