# -*- coding: utf-8 -*-
"""IDE 内置网页预览：把沙箱目录临时挂到 127.0.0.1:<随机端口>。

为什么不用 iframe 直接读文件：iframe 请求带不了 X-Auth-Token，
而页面里的相对路径 css/js 又必须能正常解析。起一个只监听本机、
只读沙箱目录的临时静态服务最省事；关掉预览面板就停掉，端口不常驻。
"""
import io
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from app import agent

_LOCK = threading.Lock()
_SERVER = None
_THREAD = None
_ROOT = ""

# 预览页由我们自己的静态服务提供，才能在 HTML 里注入桥接脚本：
# 元素选择（把 DOM 节点发回对话）与控制台错误采集都靠它。
# 外部 dev server（http://localhost:5173）不受我们控制，只能在父窗口侧降级。
_BRIDGE_PATH = "/__gm_preview_bridge.js"
_BRIDGE_TAG = b'<script src="/__gm_preview_bridge.js" data-gm-preview="1"></script>'

_BRIDGE_JS = r"""
(function () {
  if (window.__gmPreviewBridge) return;
  window.__gmPreviewBridge = true;
  var MAX = 30;
  var seen = Object.create(null);
  var queue = [];
  var pick = false;
  var last = null;
  var box = null;

  function post(type, data) {
    try { parent.postMessage({ __gmPreview: 1, type: type, data: data }, '*'); } catch (e) {}
  }
  function str(v) {
    try {
      if (v instanceof Error) return v.stack || (v.name + ': ' + v.message);
      if (typeof v === 'string') return v;
      if (typeof v === 'undefined') return 'undefined';
      if (typeof v === 'function') return '[function ' + (v.name || 'anonymous') + ']';
      if (v && typeof v === 'object') return JSON.stringify(v);
      return String(v);
    } catch (e) { return String(v); }
  }
  function report(kind, text, extra) {
    var s = String(text || '').slice(0, 4000);
    if (!s) return;
    var key = kind + '|' + s;
    if (seen[key]) return;
    seen[key] = 1;
    var item = { kind: kind, text: s, time: Date.now(), url: location.href };
    if (extra) {
      for (var k in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k)) item[k] = extra[k];
      }
    }
    queue.push(item);
    if (queue.length > MAX) queue.shift();
    post('console-batch', queue);
  }

  ['error', 'warn'].forEach(function (kind) {
    var orig = console[kind] ? console[kind].bind(console) : function () {};
    console[kind] = function () {
      var parts = [];
      for (var i = 0; i < arguments.length; i++) parts.push(str(arguments[i]));
      report(kind, parts.join(' '));
      try { orig.apply(console, arguments); } catch (e) {}
    };
  });
  window.addEventListener('error', function (e) {
    if (e && e.target && e.target !== window && e.target.tagName) {
      report('error', 'Failed to load <' + String(e.target.tagName).toLowerCase() + '> ' +
        (e.target.src || e.target.href || ''), { source: 'resource' });
      return;
    }
    var msg = (e && e.message) ? e.message : 'Script error';
    var where = (e && e.filename) ? (e.filename + ':' + (e.lineno || 0) + ':' + (e.colno || 0)) : '';
    report('error', msg + (where ? '\n  at ' + where : ''), { source: 'window.onerror' });
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    report('error', 'Unhandled promise rejection: ' + str(e && e.reason),
      { source: 'unhandledrejection' });
  });

  function overlay() {
    if (box) return box;
    box = document.createElement('div');
    box.setAttribute('data-gm-preview-overlay', '1');
    box.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;' +
      'border:2px solid #4d6bfe;background:rgba(77,107,254,.12);border-radius:2px;' +
      'display:none;box-sizing:border-box;transition:all .05s linear;';
    (document.body || document.documentElement).appendChild(box);
    return box;
  }
  function usable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el === box || (el.closest && el.closest('[data-gm-preview-overlay]'))) return false;
    var tag = String(el.tagName || '').toLowerCase();
    return tag !== 'html' && tag !== 'body';
  }
  function selectorOf(el) {
    var parts = [];
    var node = el;
    var depth = 0;
    while (node && node.nodeType === 1 && depth < 4) {
      var s = String(node.tagName).toLowerCase();
      if (node.id) { s += '#' + node.id; parts.unshift(s); break; }
      var cls = (node.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      var p = node.parentNode;
      if (p && p.children) {
        var same = 0, idx = 0;
        for (var i = 0; i < p.children.length; i++) {
          if (p.children[i].tagName === node.tagName) {
            same++;
            if (p.children[i] === node) idx = same;
          }
        }
        if (same > 1) s += ':nth-of-type(' + idx + ')';
      }
      parts.unshift(s);
      node = node.parentNode;
      depth++;
    }
    return parts.join(' > ');
  }
  function styleOf(el) {
    var c = getComputedStyle(el);
    var keys = ['display', 'position', 'width', 'height', 'color', 'background-color',
      'font-size', 'font-weight', 'line-height', 'padding', 'margin', 'border',
      'border-radius', 'flex', 'grid-template-columns', 'gap', 'opacity'];
    var out = [];
    for (var i = 0; i < keys.length; i++) {
      var v = c.getPropertyValue(keys[i]);
      if (v) out.push(keys[i] + ': ' + v);
    }
    return out.join('; ');
  }
  function infoOf(el) {
    var r = el.getBoundingClientRect();
    var html = '';
    try { html = el.outerHTML || ''; } catch (e) {}
    return {
      tag: String(el.tagName || '').toLowerCase(),
      id: el.id || '',
      classes: (el.getAttribute('class') || '').trim(),
      selector: selectorOf(el),
      text: String(el.innerText || el.textContent || '').trim().slice(0, 300),
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      styles: styleOf(el),
      html: html.slice(0, 1200),
      page: location.href,
      title: document.title || ''
    };
  }
  function setPick(on) {
    pick = !!on;
    var b = overlay();
    if (!pick) {
      b.style.display = 'none';
      last = null;
      document.documentElement.style.cursor = '';
    } else {
      document.documentElement.style.cursor = 'crosshair';
    }
    post('pick-state', { on: pick });
  }

  document.addEventListener('mousemove', function (e) {
    if (!pick) return;
    var el = e.target;
    if (!usable(el) || el === last) return;
    last = el;
    var r = el.getBoundingClientRect();
    var b = overlay();
    b.style.display = 'block';
    b.style.left = r.left + 'px';
    b.style.top = r.top + 'px';
    b.style.width = r.width + 'px';
    b.style.height = r.height + 'px';
  }, true);
  document.addEventListener('click', function (e) {
    if (!pick) return;
    var el = e.target;
    if (!usable(el)) return;
    e.preventDefault();
    e.stopPropagation();
    var data = infoOf(el);
    setPick(false);
    post('pick', data);
  }, true);
  document.addEventListener('keydown', function (e) {
    if (pick && (e.key === 'Escape' || e.keyCode === 27)) {
      e.preventDefault();
      setPick(false);
      post('pick-cancel', {});
    }
  }, true);

  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (d.__gmPreviewCmd === 'pick') setPick(!!d.on);
    else if (d.__gmPreviewCmd === 'clear') {
      queue.length = 0;
      seen = Object.create(null);
      post('console-batch', []);
    } else if (d.__gmPreviewCmd === 'state') {
      post('console-batch', queue);
      post('pick-state', { on: pick });
    }
  });

  post('ready', { url: location.href, title: document.title });
})();
"""


def _inject_html(body):
    """把桥接脚本插到 </body> 前；只处理我们自己的静态服务。"""
    if _BRIDGE_TAG in body:
        return body
    low = body.lower()
    idx = low.rfind(b"</body>")
    if idx >= 0:
        return body[:idx] + _BRIDGE_TAG + body[idx:]
    return body + _BRIDGE_TAG


def _handler(root):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, *a):       # 预览页的资源请求不要刷日志
            pass

        def end_headers(self):
            # 改完文件点「刷新」要立刻看到，禁止缓存
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _send_bridge(self):
            body = _BRIDGE_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)

        def _send_injected(self, fs):
            try:
                with open(fs, "rb") as f:
                    body = _inject_html(f.read())
            except OSError:
                return super().send_head()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)

        def send_head(self):
            if self.path.split("?", 1)[0] == _BRIDGE_PATH:
                return self._send_bridge()
            # 预览页与静态资源同源：页面脚本能 fetch 任意路径，
            # 所以 .env / 私钥 / .git 这些不能用「工具读不了」当挡箭牌——
            # 这里按 agent 的路径规则再拦一次（越界、禁止目录、密钥文件一律 403）。
            try:
                rel = os.path.relpath(self.translate_path(self.path), root)
                if not rel.startswith(".."):
                    agent.resolve_path(root, rel, for_read=True)
                    if agent.is_secret_path(rel):
                        raise agent.SandboxError("secret")
            except (agent.SandboxError, OSError):
                self.send_error(403, "Forbidden")
                return None
            fs = self.translate_path(self.path)
            if os.path.isdir(fs):
                fs = os.path.join(fs, "index.html")
            if fs.lower().endswith((".html", ".htm")) and os.path.isfile(fs):
                return self._send_injected(fs)
            return super().send_head()

    return H



def start(root):
    """启动（或复用）预览服务，返回 base URL。"""
    global _SERVER, _THREAD, _ROOT
    root = os.path.abspath(root)
    with _LOCK:
        if _SERVER and _ROOT == root and _THREAD and _THREAD.is_alive():
            return "http://127.0.0.1:%d/" % _SERVER.server_address[1]
        _stop_locked()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        _SERVER, _THREAD, _ROOT = srv, th, root
        return "http://127.0.0.1:%d/" % srv.server_address[1]


def _stop_locked():
    global _SERVER, _THREAD, _ROOT
    if _SERVER:
        try:
            _SERVER.shutdown()
        except Exception:                   # noqa: BLE001
            pass
        try:
            _SERVER.server_close()
        except Exception:                   # noqa: BLE001
            pass
    _SERVER, _THREAD, _ROOT = None, None, ""


def stop():
    with _LOCK:
        _stop_locked()
