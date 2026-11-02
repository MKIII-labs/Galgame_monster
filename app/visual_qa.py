# -*- coding: utf-8 -*-
"""视觉 QA：无头浏览器打开页面，抓运行时错误、WebGL 指标、帧率、截图与基线差异。

比 render_check 多出来的部分：

- 多视口截图；
- WebGL canvas / draw calls / renderer；
- rAF 采样帧率与最大掉帧；
- 图片指标：亮度、对比度、清晰度、边缘密度、空白倾向；
- 与基线截图的全局 SSIM；
- 机器可读的 `__VISUAL_METRICS__`，供 acceptance 直接断言。

只做本地渲染，不联网。截图存在 data/visual/<session>/，不写进沙箱工程。
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse

from app import webprobe
from app.store import DATA_DIR

_VQA_HEAD = r"""
<script>
(function(){
  var V = window.__VQA__ = window.__VQA__ || {errors:[],warns:[],logs:[],resource:[],draw:0,tri:0,progs:[],texs:[]};
  function push(a, x, n){ if (a.length < n) a.push(String(x).slice(0, 300)); }
  function track(a, x, n){
    if (!x || a.length >= n) return;
    for (var i = 0; i < a.length; i++) if (a[i] === x) return;
    a.push(x);
  }
  // 从 WebGL 调用本身统计三角面：不依赖 three.js / Babylon 的内部对象，
  // 所有渲染器（含自写的 gl 代码）都能算。
  function triOf(mode, count, inst){
    if (!count || count < 0) return 0;
    inst = inst || 1;
    if (mode === 4) return Math.floor(count / 3) * inst;          // TRIANGLES
    if (mode === 5 || mode === 6) return Math.max(0, count - 2) * inst;  // STRIP / FAN
    return 0;
  }
  var ce = console.error, cw = console.warn;
  console.error = function(){ push(V.errors, Array.prototype.join.call(arguments,' '), 20); return ce.apply(console, arguments); };
  console.warn  = function(){ push(V.warns,  Array.prototype.join.call(arguments,' '), 20); return cw.apply(console, arguments); };
  window.addEventListener('error', function(e){
    if (e && e.target && e.target !== window && (e.target.src || e.target.href)) {
      push(V.resource, (e.target.src || e.target.href), 20);
    } else {
      push(V.errors, (e && e.message ? e.message : 'script error'), 20);
    }
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    push(V.errors, 'unhandledrejection: ' + ((e.reason && e.reason.message) || e.reason || ''), 20);
  });
  ['drawElements','drawArrays','drawElementsInstanced','drawArraysInstanced'].forEach(function(n){
    [window.WebGLRenderingContext, window.WebGL2RenderingContext].forEach(function(C){
      if (C && C.prototype && C.prototype[n] && !C.prototype[n].__vqa) {
        var old = C.prototype[n];
        var f = function(){
          V.draw++;
          var a = arguments;
          if (n === 'drawElements') V.tri += triOf(a[0], a[1], 1);
          else if (n === 'drawElementsInstanced') V.tri += triOf(a[0], a[1], a[4]);
          else if (n === 'drawArrays') V.tri += triOf(a[0], a[2], 1);
          else V.tri += triOf(a[0], a[2], a[3]);
          return old.apply(this, a);
        };
        f.__vqa = 1; C.prototype[n] = f;
      }
    });
  });
  [window.WebGLRenderingContext, window.WebGL2RenderingContext].forEach(function(C){
    if (!C || !C.prototype) return;
    var up = C.prototype.useProgram;
    if (up && !up.__vqa) {
      var fp = function(p){ track(V.progs, p, 300); return up.apply(this, arguments); };
      fp.__vqa = 1; C.prototype.useProgram = fp;
    }
    var bt = C.prototype.bindTexture;
    if (bt && !bt.__vqa) {
      var ft = function(t, x){ track(V.texs, x, 500); return bt.apply(this, arguments); };
      ft.__vqa = 1; C.prototype.bindTexture = ft;
    }
  });
  if (window.fetch) {
    var of = window.fetch;
    window.fetch = function(){
      return of.apply(this, arguments).then(function(r){
        if (r && r.status >= 400) push(V.resource, 'fetch ' + r.status, 20);
        return r;
      }).catch(function(e){ push(V.resource, 'fetch ' + e, 20); throw e; });
    };
  }
  var ox = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__vqa_url = u; return ox.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(){
    var x = this;
    x.addEventListener('error', function(){ push(V.resource, 'xhr ' + x.__vqa_url, 20); });
    x.addEventListener('load', function(){ if (x.status >= 400) push(V.resource, 'xhr ' + x.status + ' ' + x.__vqa_url, 20); });
    return os.apply(this, arguments);
  };
})();
</script>
"""

_VQA_TAIL = r"""
<script>
(function(){
  var V = window.__VQA__ || (window.__VQA__ = {errors:[],warns:[],logs:[],resource:[],draw:0,tri:0,progs:[],texs:[]});
  var frames = 0, t0 = performance.now(), last = t0, maxGap = 0;
  function finish(){
    if (V.done) return; V.done = 1;
    var canvases = [].slice.call(document.querySelectorAll('canvas')).map(function(c){
      return {w: c.width, h: c.height, cssW: c.clientWidth, cssH: c.clientHeight};
    });
    var glInfo = null;
    try {
      var c = document.querySelector('canvas');
      var gl = c && (c.getContext('webgl2') || c.getContext('webgl'));
      if (gl) {
        var d = gl.getExtension('WEBGL_debug_renderer_info');
        glInfo = {
          vendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : '',
          renderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : ''
        };
      }
    } catch (e) { glInfo = {error: String(e)}; }
    V.result = {
      fps: frames > 1 ? Math.round(frames / ((performance.now() - t0) / 1000) * 10) / 10 : 0,
      frames: frames,
      max_frame_gap_ms: Math.round(maxGap * 10) / 10,
      console_errors: V.errors.length,
      console_warns: V.warns.length,
      resource_errors: V.resource.length,
      draw_calls: V.draw,
      triangles: Math.round(V.tri || 0),
      programs: (V.progs || []).length,
      textures_bound: (V.texs || []).length,
      canvases: canvases,
      gl: glInfo,
      title: document.title || '',
      scroll: {w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight},
      samples: {errors: V.errors.slice(0, 6), warns: V.warns.slice(0, 6), resource: V.resource.slice(0, 6)}
    };
    var pre = document.createElement('pre');
    pre.id = 'vqa'; pre.style.display = 'none';
    pre.textContent = JSON.stringify(V.result);
    document.body.appendChild(pre);
  }
  function loop(t){
    frames++;
    var g = t - last; if (g > maxGap) maxGap = g;
    last = t;
    if (t - t0 < 1200) requestAnimationFrame(loop);
    else finish();
  }
  requestAnimationFrame(loop);
  setTimeout(finish, 2600);
})();
</script>
"""


def _inject(text, base_dir):
    text = str(text or "")
    base = '<base href="%s/">' % webprobe._dir_uri(base_dir)
    if not re.search(r"<meta[^>]+charset", text, re.I):
        base += '<meta charset="utf-8">'
    m = re.search(r"<head\b[^>]*>", text, re.I)
    if m:
        text = text[:m.end()] + base + _VQA_HEAD + text[m.end():]
    else:
        text = "<head>" + base + _VQA_HEAD + "</head>" + text
    low = text.lower()
    i = low.rfind("</body>")
    if i >= 0:
        text = text[:i] + _VQA_TAIL + text[i:]
    else:
        text += _VQA_TAIL
    return text


def _inject_min(text, base_dir, tail=""):
    """只注入 <base>（相对资源能加载）与可选尾脚本，不跑 VQA 的帧率采样。"""
    text = str(text or "")
    base = '<base href="%s/">' % webprobe._dir_uri(base_dir)
    if not re.search(r"<meta[^>]+charset", text, re.I):
        base += '<meta charset="utf-8">'
    m = re.search(r"<head\b[^>]*>", text, re.I)
    if m:
        text = text[:m.end()] + base + text[m.end():]
    else:
        text = "<head>" + base + "</head>" + text
    if tail:
        low = text.lower()
        i = low.rfind("</body>")
        text = (text[:i] + tail + text[i:]) if i >= 0 else (text + tail)
    return text


def _dump(edge, url, profile, vw, vh, timeout):
    args = ["--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--hide-scrollbars", "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (vw, vh),
            "--virtual-time-budget=%d" % (webprobe.WAIT_MS + 3400),
            "--dump-dom", url]
    out = ""
    for _ in range(2):
        p = webprobe._run(edge, args, timeout)
        out = p.stdout.decode("utf-8", "replace")
        if _extract(out) is not None:
            return out
    return out


def _extract(dom):
    m = list(re.finditer(r'<pre id="vqa"[^>]*>(.*?)</pre>', dom or "", re.S))
    if not m:
        return None
    import html as htmllib
    try:
        return json.loads(htmllib.unescape(m[-1].group(1)))
    except ValueError:
        return None


def image_metrics(path):
    """截图的确定性指标。没有 Pillow 时返回空。"""
    try:
        import numpy as np
        from PIL import Image, ImageFilter
    except Exception:
        return {}
    try:
        with Image.open(path) as im:
            small = im.convert("RGB").resize((256, 256))
            gray = np.asarray(small.convert("L"), dtype=np.float32)
            edge = np.asarray(small.convert("L").filter(ImageFilter.FIND_EDGES),
                             dtype=np.float32)
            colors = small.getcolors(maxcolors=1 << 20) or []
    except Exception as e:
        return {"error": str(e)[:120]}
    contrast = float(gray.std())
    return {
        "brightness": round(float(gray.mean()), 1),
        "contrast": round(contrast, 1),
        "sharpness": round(float(edge.var()), 1),
        "edge_density": round(float((edge > 24).mean()), 4),
        "unique_colors": len(colors),
        # 越接近 1 越像空白/纯色；模型可以据此判断黑屏/白屏
        "blank_score": round(max(0.0, 1.0 - contrast / 12.0), 3),
    }


def _gray256(path):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize((256, 256)), dtype=np.float32)


def ssim(path_a, path_b):
    """全局 SSIM（单窗口）；用于基线对比，不做局部结构分析。"""
    try:
        import numpy as np
    except Exception:
        return None
    try:
        a, b = _gray256(path_a), _gray256(path_b)
    except Exception:
        return None
    if a.shape != b.shape:
        return None
    mu_a, mu_b = float(a.mean()), float(b.mean())
    va, vb = float(a.var()), float(b.var())
    cov = float(((a - mu_a) * (b - mu_b)).mean())
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2)
    if den <= 0:
        return None
    return round(float(((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / den), 4)


def _visual_dir(sid):
    d = os.path.join(DATA_DIR, "visual", re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or "unknown"))[:80])
    os.makedirs(d, exist_ok=True)
    return d


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "baseline"))[:60] or "baseline"


def check(abs_path, viewport="", sid="", screenshots=True, describe=False,
          baseline="", save_baseline=False, timeout=90, viewports=None):
    """渲染 + 指标 + 截图 + 基线对比，返回给模型看的文本。"""
    edge = webprobe.edge_path()
    if not edge:
        return ("⚠️ 没找到 Edge / Chrome，无法做视觉 QA。"
                "可以设环境变量 GAL_EDGE 指向浏览器可执行文件。")
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % abs_path
    if not abs_path.lower().endswith((".html", ".htm")):
        return "❌ visual_check 只支持 .html / .htm（当前：%s）" % os.path.basename(abs_path)
    vps = viewports if isinstance(viewports, list) and viewports else [viewport or ""]
    vps = vps[:3]
    work = tempfile.mkdtemp(prefix="gal_vqa_")
    out_dir = _visual_dir(sid)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    reports, all_metrics = [], []
    try:
        copy = os.path.join(work, "page.html")
        with open(copy, "w", encoding="utf-8") as f:
            f.write(_inject(webprobe._read_html(abs_path), os.path.dirname(abs_path)))
        url = "file:///" + copy.replace("\\", "/").lstrip("/")
        for idx, vp in enumerate(vps):
            vw, vh = webprobe.parse_viewport(vp)
            dom = _dump(edge, url, os.path.join(work, "prof%d" % idx), vw, vh, timeout)
            data = _extract(dom)
            if data is None:
                reports.append("⚠️ %d×%d：浏览器没有回传视觉探针结果" % (vw, vh))
                continue
            png = os.path.join(work, "shot_%d.png" % idx)
            b64 = webprobe._shot(edge, url, os.path.join(work, "shotprof%d" % idx),
                                 vw, vh, png, timeout) if screenshots else ""
            shot_path = ""
            if b64:
                shot_path = os.path.join(out_dir, "shots",
                                         "%s_%dx%d.png" % (stamp, vw, vh))
                os.makedirs(os.path.dirname(shot_path), exist_ok=True)
                shutil.copyfile(png, shot_path)
            met = image_metrics(png) if b64 else {}
            item = {
                "viewport": "%dx%d" % (vw, vh),
                "fps": data.get("fps"),
                "frames": data.get("frames"),
                "max_frame_gap_ms": data.get("max_frame_gap_ms"),
                "console_errors": data.get("console_errors"),
                "console_warns": data.get("console_warns"),
                "resource_errors": data.get("resource_errors"),
                "draw_calls": data.get("draw_calls"),
                "triangles": data.get("triangles"),
                "programs": data.get("programs"),
                "textures_bound": data.get("textures_bound"),
                "canvases": data.get("canvases") or [],
                "gl": data.get("gl"),
                "samples": data.get("samples") or {},
                "screenshot": shot_path,
                "baseline": "",
                "ssim": None,
            }
            item.update(met)
            if baseline:
                bname = _safe_name(baseline)
                bdir = os.path.join(out_dir, "baselines")
                bpng = os.path.join(bdir, bname + ".png")
                if save_baseline and shot_path:
                    os.makedirs(bdir, exist_ok=True)
                    shutil.copyfile(shot_path, bpng)
                    with open(os.path.join(bdir, bname + ".json"), "w",
                              encoding="utf-8") as f:
                        json.dump(item, f, ensure_ascii=False, indent=2)
                    item["baseline"] = "saved:" + bname
                elif os.path.isfile(bpng) and shot_path:
                    item["baseline"] = bname
                    item["ssim"] = ssim(shot_path, bpng)
            all_metrics.append(item)
            reports.append(_format_one(os.path.basename(abs_path), item, describe))
    except Exception as e:
        return "❌ 视觉 QA 出错：%s" % e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not reports:
        return "❌ 视觉 QA 没有拿到任何结果（页面可能拒绝加载或浏览器会话异常）。"
    metrics = all_metrics[0] if all_metrics else {}
    lines = ["\n\n".join(reports)]
    if len(all_metrics) > 1:
        lines.append("__VISUAL_ALL__ " + json.dumps(all_metrics, ensure_ascii=False))
    lines.append("__VISUAL_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    lines.append("提示：指标是确定性测量；配色是否好看、手感如何仍需人看。")
    return "\n".join(lines)


def _format_one(name, item, describe):
    lines = ["【视觉 QA】%s · 视口 %s" % (name, item["viewport"])]
    errs = int(item.get("console_errors") or 0)
    warns = int(item.get("console_warns") or 0)
    res = int(item.get("resource_errors") or 0)
    lines.append("控制台错误 %d · 警告 %d · 资源失败 %d" % (errs, warns, res))
    samples = item.get("samples") or {}
    for key, label in (("errors", "错误"), ("resource", "资源")):
        vals = samples.get(key) or []
        if vals:
            lines.append("%s样例：" % label + "；".join(str(x)[:120] for x in vals[:3]))
    cvs = item.get("canvases") or []
    gl = item.get("gl") or {}
    if cvs:
        lines.append("Canvas %d 个（%s）· draw calls %s · renderer %s"
                     % (len(cvs),
                        "、".join("%s×%s" % (c.get("w"), c.get("h")) for c in cvs[:3]),
                        item.get("draw_calls"),
                        (gl.get("renderer") or "未知")[:80]))
    else:
        lines.append("Canvas：无（如果这是 canvas / WebGL 页面，说明渲染层没起来）")
    if item.get("triangles") is not None:
        lines.append("三角面 %s · 着色器 %s · 绑定纹理 %s"
                     % (item.get("triangles"), item.get("programs"),
                        item.get("textures_bound")))
    fps = item.get("fps")
    if fps is not None:
        lines.append("帧率 %.1f FPS（采样 %s 帧，最大间隔 %sms）"
                     % (float(fps), item.get("frames"), item.get("max_frame_gap_ms")))
    if item.get("sharpness") is not None:
        lines.append("画面：亮度 %s · 对比度 %s · 清晰度 %s · 边缘密度 %s · 空白倾向 %s"
                     % (item.get("brightness"), item.get("contrast"),
                        item.get("sharpness"), item.get("edge_density"),
                        item.get("blank_score")))
    if item.get("ssim") is not None:
        lines.append("基线 %s：SSIM %s" % (item.get("baseline"), item.get("ssim")))
    elif item.get("baseline"):
        lines.append("基线 %s：%s" % (item.get("baseline"),
                                      "已保存" if str(item["baseline"]).startswith("saved:")
                                      else "无基线图可对比"))
    if item.get("screenshot"):
        lines.append("截图：" + item["screenshot"])
    if describe and item.get("screenshot"):
        try:
            from app import imaging
            b64 = ""
            with open(item["screenshot"], "rb") as f:
                import base64
                b64 = base64.b64encode(f.read()).decode("ascii")
            desc = imaging.recognize_image_fallback(
                "image/png", b64,
                "这是一个网页 / canvas 页面的渲染截图。只描述你看到的：是否黑屏/空白、"
                "主要区块是否重叠、文字是否清晰、明显视觉缺陷。150 字以内。")
            if desc:
                lines.append("【视觉模型描述】" + str(desc)[:600])
        except Exception as e:
            lines.append("（视觉描述失败：%s）" % e)
    return "\n".join(lines)


# ============================================================
# 多视图渲染（render_views）：固定视角截图，供 geometry_compare 对照
# ============================================================

DEFAULT_VIEWS = ("front", "side", "top", "iso")
MAX_VIEWS = 8


def _parse_views(views):
    """把参数收敛成 [{name, query, file}]。

    页面需要自己读 `location.search`（例如 `?view=front`）来设置相机；
    也支持显式给 query（`{"name":"45度","query":"view=iso&angle=45"}`）。
    """
    raw = views if isinstance(views, list) and views else list(DEFAULT_VIEWS)
    out, seen = [], set()
    for i, v in enumerate(raw[:MAX_VIEWS], 1):
        if isinstance(v, str):
            name, query = v.strip(), ""
        elif isinstance(v, dict):
            name = str(v.get("name") or v.get("id") or "").strip()
            query = str(v.get("query") or "").strip().lstrip("?")
        else:
            continue
        if not name:
            name = "view%d" % i
        key = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", name)[:40] or ("view%d" % i)
        while key in seen:
            key += "_"
        seen.add(key)
        if not query:
            query = "view=" + urllib.parse.quote(name)
        out.append({"name": name, "query": query, "file": key})
    return out


def render_views(abs_path, views=None, viewport="1280x720", sid="", timeout=90):
    """按多个视角截图，返回清单；页面不读 ?view= 时会提示「截图完全相同」。"""
    edge = webprobe.edge_path()
    if not edge:
        return ("⚠️ 没找到 Edge / Chrome，无法渲染多视图。"
                "可以设环境变量 GAL_EDGE 指向浏览器可执行文件。")
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % abs_path
    if not abs_path.lower().endswith((".html", ".htm")):
        return ("❌ render_views 只支持 .html / .htm（当前：%s）"
                % os.path.basename(abs_path))
    vw, vh = webprobe.parse_viewport(viewport)
    parsed = _parse_views(views)
    if not parsed:
        return "❌ views 为空：给 1–%d 个视角（如 front / side / top / iso）" % MAX_VIEWS
    work = tempfile.mkdtemp(prefix="gal_views_")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(_visual_dir(sid), "views", stamp)
    rows = []
    try:
        copy = os.path.join(work, "page.html")
        with open(copy, "w", encoding="utf-8") as f:
            f.write(_inject_min(webprobe._read_html(abs_path),
                                os.path.dirname(abs_path)))
        url_base = "file:///" + copy.replace("\\", "/").lstrip("/")
        for i, v in enumerate(parsed):
            png = os.path.join(work, "v%d.png" % i)
            b64 = webprobe._shot(
                edge, url_base + "?" + v["query"], os.path.join(work, "prof%d" % i),
                vw, vh, png, timeout)
            if not b64:
                rows.append({"name": v["name"], "query": v["query"],
                             "screenshot": "", "ok": False})
                continue
            os.makedirs(out_dir, exist_ok=True)
            dst = os.path.join(out_dir, v["file"] + ".png")
            shutil.copyfile(png, dst)
            with open(dst, "rb") as f:
                sha = hashlib.sha1(f.read()).hexdigest()
            rows.append({"name": v["name"], "query": v["query"],
                         "screenshot": dst, "sha1": sha, "ok": True})
    except Exception as e:                     # noqa: BLE001
        return "❌ 多视图渲染失败：%s" % e
    finally:
        shutil.rmtree(work, ignore_errors=True)

    ok_rows = [r for r in rows if r.get("ok")]
    if not ok_rows:
        return "❌ 多视图渲染没有拿到任何截图（页面可能拒绝加载或浏览器会话异常）。"
    identical = len(ok_rows) > 1 and len({r.get("sha1") for r in ok_rows}) == 1
    manifest = {"created": stamp, "viewport": "%dx%d" % (vw, vh),
                "page": os.path.basename(abs_path),
                "views": [{"name": r["name"], "query": r["query"],
                           "screenshot": r["screenshot"]} for r in ok_rows]}
    try:
        with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    lines = ["【多视图渲染】%s · %d 个视角 · %dx%d"
             % (os.path.basename(abs_path), len(ok_rows), vw, vh)]
    for r in rows:
        if r.get("ok"):
            lines.append("  %s（?%s）→ %s" % (r["name"], r["query"], r["screenshot"]))
        else:
            lines.append("  %s（?%s）→ 截图失败" % (r["name"], r["query"]))
    if identical:
        lines.append("⚠️ 所有视角的截图完全相同：页面大概率没有读取 `location.search` "
                     "里的 `view` 参数。给页面加一段「按 ?view= 设置相机」的逻辑后再跑；"
                     "否则多视图对照没有意义。")
    lines.append("下一步：把参考图与这些产物图按视角配对，用 geometry_compare 对照。")
    lines.append("__RENDER_VIEWS__ " + json.dumps(manifest, ensure_ascii=False))
    return "\n".join(lines)


# ============================================================
# 场景断言（scene_assert）：读页面暴露的场景事实，不靠截图猜
# ============================================================

_SCENE_TAIL = r"""
<script>
(function(){
  function pick(){
    try {
      var f = window.__VQA_SCENE_INFO__;
      var d = (typeof f === 'function') ? f() : (f || window.__VQA_SCENE__ || null);
      if (!d || typeof d !== 'object') return null;
      var objs = (d.objects || []).slice(0, 300).map(function(o){
        o = o || {};
        var b = o.bbox || null;
        return {name: String(o.name || ''), type: String(o.type || ''),
                visible: o.visible !== false,
                bbox: (b && b.min && b.max) ? {min: b.min, max: b.max} : null};
      });
      return {objects: objs, camera: d.camera || null,
              lights: (d.lights || []).slice(0, 50),
              renderer: d.renderer || null};
    } catch (e) { return {error: String(e)}; }
  }
  var tries = 0;
  function done(data){
    if (window.__VQA_SCENE_DONE__) return;
    window.__VQA_SCENE_DONE__ = 1;
    var pre = document.createElement('pre');
    pre.id = 'vqa-scene'; pre.style.display = 'none';
    pre.textContent = JSON.stringify({found: !!data && !data.error, data: data});
    document.body.appendChild(pre);
  }
  function loop(){
    var d = pick();
    if (d || ++tries > 30) return done(d);
    setTimeout(loop, 100);
  }
  setTimeout(loop, 300);
})();
</script>
"""


def _extract_scene(dom):
    m = list(re.finditer(r'<pre id="vqa-scene"[^>]*>(.*?)</pre>', dom or "", re.S))
    if not m:
        return None
    import html as htmllib
    try:
        return json.loads(htmllib.unescape(m[-1].group(1)))
    except ValueError:
        return None


def _dump_scene(edge, url, profile, vw, vh, timeout):
    args = ["--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--hide-scrollbars", "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (vw, vh),
            "--virtual-time-budget=%d" % (webprobe.WAIT_MS + 3400),
            "--dump-dom", url]
    out = ""
    for _ in range(2):
        p = webprobe._run(edge, args, timeout)
        out = p.stdout.decode("utf-8", "replace")
        if _extract_scene(out) is not None:
            return out
    return out


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def scene_checks(data, assertions):
    """纯函数：按断言检查场景事实，返回 [{id, kind, ok, got, want, note}]。"""
    data = data if isinstance(data, dict) else {}
    objs = [o for o in (data.get("objects") or []) if isinstance(o, dict)]
    names = [str(o.get("name") or "") for o in objs]
    lights = data.get("lights") or []
    cam = data.get("camera") or {}
    renderer = data.get("renderer") or {}
    tris = _fnum(renderer.get("triangles"))
    if tris is None:
        tris = _fnum(((renderer.get("info") or {}).get("render") or {}).get("triangles"))
    mn, mx, has_box = [float("inf")] * 3, [float("-inf")] * 3, False
    for o in objs:
        if not o.get("visible"):
            continue
        b = o.get("bbox") or {}
        lo, hi = b.get("min"), b.get("max")
        if not (isinstance(lo, list) and len(lo) >= 3
                and isinstance(hi, list) and len(hi) >= 3):
            continue
        for k in range(3):
            mn[k] = min(mn[k], float(lo[k]))
            mx[k] = max(mx[k], float(hi[k]))
        has_box = True
    size = [(mx[k] - mn[k]) if has_box else None for k in range(3)]

    out = []
    for i, a in enumerate((assertions or [])[:20], 1):
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind") or a.get("type") or "").strip()
        cid = str(a.get("id") or "c%d" % i)
        want = a.get("value", a.get("min", a.get("max")))
        got, ok, note = None, False, ""
        if kind == "exists":
            name = str(a.get("name") or a.get("value") or "").strip()
            got = names
            ok = bool(name) and any(name == n or name in n for n in names)
        elif kind in ("objects_min", "objects_max"):
            got = len(objs)
            w = _fnum(want, 0)
            ok = got >= w if kind == "objects_min" else got <= w
        elif kind == "hidden_max":
            got = sum(1 for o in objs if not o.get("visible"))
            ok = got <= _fnum(want, 0)
        elif kind == "lights_min":
            got = len(lights)
            ok = got >= _fnum(want, 0)
        elif kind in ("camera_fov_min", "camera_fov_max"):
            got = _fnum(cam.get("fov"))
            w = _fnum(want, 0)
            ok = got is not None and (got >= w if kind == "camera_fov_min" else got <= w)
        elif kind == "triangles_max":
            got = tris
            ok = got is not None and got <= _fnum(want, 0)
        elif kind in ("bbox_x_max", "bbox_y_max", "bbox_z_max"):
            got = size[{"bbox_x_max": 0, "bbox_y_max": 1, "bbox_z_max": 2}[kind]]
            ok = got is not None and got <= _fnum(want, 0)
        else:
            note = "未知检查类型：%s" % (kind or "（空）")
        out.append({"id": cid, "kind": kind, "ok": bool(ok),
                    "got": got, "want": want, "note": note})
    return out


def scene_assert(abs_path, assertions, viewport="1280x720", sid="", timeout=90):
    """按页面暴露的场景事实做声明式断言（对象 / 相机 / 灯光 / 三角面 / 包围盒）。"""
    edge = webprobe.edge_path()
    if not edge:
        return "⚠️ 没找到 Edge / Chrome，无法做场景断言。"
    if not os.path.isfile(abs_path):
        return "❌ 文件不存在：%s" % abs_path
    if not abs_path.lower().endswith((".html", ".htm")):
        return "❌ scene_assert 只支持 .html / .htm（当前：%s）" % os.path.basename(abs_path)
    if not isinstance(assertions, list) or not assertions:
        return "❌ scene_assert 需要 assertions 数组（1–20 条）。"
    vw, vh = webprobe.parse_viewport(viewport)
    work = tempfile.mkdtemp(prefix="gal_scene_")
    try:
        copy = os.path.join(work, "page.html")
        with open(copy, "w", encoding="utf-8") as f:
            f.write(_inject_min(webprobe._read_html(abs_path),
                                os.path.dirname(abs_path), _SCENE_TAIL))
        url = "file:///" + copy.replace("\\", "/").lstrip("/")
        dom = _dump_scene(edge, url, os.path.join(work, "prof"), vw, vh, timeout)
    except Exception as e:                     # noqa: BLE001
        return "❌ 场景断言失败：%s" % e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    payload = _extract_scene(dom)
    if payload is None:
        return "❌ 页面没有回传场景探针结果（可能加载失败或脚本报错）。"
    if not payload.get("found"):
        return ("❌ 页面没有暴露场景信息，无法断言。在页面脚本里加一段：\n"
                "```js\nwindow.__VQA_SCENE_INFO__ = function(){\n"
                "  return {objects: [{name:'ground', type:'Mesh', visible:true,\n"
                "           bbox:{min:[x,y,z], max:[x,y,z]}}],\n"
                "          camera: {fov: 50, position: [x,y,z]},\n"
                "          lights: [{type:'DirectionalLight', intensity: 1}],\n"
                "          renderer: {triangles: 1234}};\n};\n```\n"
                "数据要来自真实场景对象，不要写死常量。")
    data = payload.get("data") or {}
    checks = scene_checks(data, assertions)
    failed = [c for c in checks if not c["ok"]]
    lines = ["【场景断言】%s · 通过 %d / %d" % (os.path.basename(abs_path),
                                              len(checks) - len(failed), len(checks))]
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        detail = "期望 %s，实际 %s" % (c.get("want"), c.get("got"))
        if c["kind"] == "exists":
            detail = "对象列表：%s" % "、".join(str(x) for x in (c.get("got") or [])[:8])
        lines.append("  %s %s %s（%s）"
                     % (mark, c.get("id"), c["kind"], c.get("note") or detail))
    if failed:
        lines.append("先修未通过的断言；确认是断言写错（场景本身没问题）时再改断言。")
    lines.append("（只读页面暴露的场景事实；不渲染、不判断好不好看）")
    lines.append("__SCENE_METRICS__ " + json.dumps({
        "total": len(checks), "passed": len(checks) - len(failed),
        "failed": len(failed),
        "objects": len(data.get("objects") or []),
        "lights": len(data.get("lights") or []),
        "triangles": _fnum((data.get("renderer") or {}).get("triangles")),
    }, ensure_ascii=False))
    lines.append("__SCENE_CHECKS__ " + json.dumps(checks, ensure_ascii=False))
    return "\n".join(lines)

