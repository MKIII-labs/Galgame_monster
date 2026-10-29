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
import json
import os
import re
import shutil
import tempfile
import time

from app import webprobe
from app.store import DATA_DIR

_VQA_HEAD = r"""
<script>
(function(){
  var V = window.__VQA__ = window.__VQA__ || {errors:[],warns:[],logs:[],resource:[],draw:0};
  function push(a, x, n){ if (a.length < n) a.push(String(x).slice(0, 300)); }
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
        var f = function(){ V.draw++; return old.apply(this, arguments); };
        f.__vqa = 1; C.prototype[n] = f;
      }
    });
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
  var V = window.__VQA__ || (window.__VQA__ = {errors:[],warns:[],logs:[],resource:[],draw:0});
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
