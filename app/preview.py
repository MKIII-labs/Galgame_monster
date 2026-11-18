# -*- coding: utf-8 -*-
"""IDE 内置网页预览：沙箱静态服务 + 本机 dev server 反向代理。

- 静态：把沙箱目录临时挂到 127.0.0.1:<随机端口>，只读、只监听本机；
- dev：把 http://localhost:PORT 代理到带 token 的本机端口，并在 HTML 里注入预览桥，
  这样控制台 / 元素选择对 Vite / Webpack dev server 也可用；WebSocket（HMR）做双向隧道。

为什么不用 iframe 直接读文件：iframe 请求带不了 X-Auth-Token，
而页面里的相对路径 css/js 又必须能正常解析。关掉预览面板就停掉，端口不常驻。
"""
import http.client
import io
import os
import secrets
import select
import socket
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

from app import agent

_LOCK = threading.Lock()
_SERVER = None
_THREAD = None
_ROOT = ""
_MODE = "static"          # static | dev
_DEV_TARGET = ""          # dev server 的 origin，如 http://127.0.0.1:5173
_DEV_TOKEN = ""
_DEV_PREFIX = ""          # dev server 的挂载前缀（/app 这类子路径；根为 ""）
_DEV_OPEN = "/"           # 打开代理时停在哪一页（通常是 / 或 /index.html）

# 静态预览和本机 dev server 反向代理都在 HTML 里注入桥接脚本：
# 元素选择（把 DOM 节点发回对话）与控制台错误采集都靠它。
# 公网 URL 仍是 iframe 直连，无法注入，前端会禁用这两个按钮。
_BRIDGE_PATH = "/__gm_preview_bridge.js"

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


def _bridge_tag(token=""):
    src = (("/" + token) if token else "") + _BRIDGE_PATH
    return ('<script src="%s" data-gm-preview="1"></script>' % src).encode("utf-8")


def _inject_html(body, tag=None):
    """把桥接脚本插到 </body> 前；静态服务和 dev server 代理共用。"""
    tag = tag or _bridge_tag()
    if tag in body:
        return body
    low = body.lower()
    idx = low.rfind(b"</body>")
    if idx >= 0:
        return body[:idx] + tag + body[idx:]
    return body + tag


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



def _split_target_path(path):
    """把 dev server URL 的路径拆成 (挂载前缀, 要打开的页面)。

    - 目录形式（以 / 结尾，或最后一段没有扩展名）→ 前缀=目录，打开 /
    - 文件形式（/app/index.html）→ 前缀=所在目录，打开 /index.html

    以前整段路径都被当成前缀，`http://host:8000/index.html` 会变成
    「前缀 /index.html + 请求路径」→ 上游收到 /index.html/ 直接 404；
    同时返回给前端的地址又丢掉了 /index.html，预览只能停在站点根目录。
    """
    p = urllib.parse.unquote(str(path or ""))
    if not p or p == "/":
        return "", "/"
    if p.endswith("/"):                  # 显式目录（含 /v1.2/ 这类带点的目录名）
        return p.rstrip("/"), "/"
    name = p.rsplit("/", 1)[-1]
    if "." not in name:                  # 目录（/app）
        return p, "/"
    d = p.rsplit("/", 1)[0]
    return d.rstrip("/"), "/" + name


def _parse_local_target(raw):
    """校验 dev server 地址：只允许本机 http(s)、端口 >= 1024。

    返回 (scheme, host, port, proxy_prefix, open_path, origin)。拒绝公网 / 内网地址，
    避免预览代理变成 SSRF 入口；1024 以下端口按 Trae 的 preview-proxy 策略拒绝。
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("缺少 dev server 地址")
    if "://" not in s:
        s = "http://" + s
    u = urllib.parse.urlparse(s)
    if u.scheme == "file":
        raise ValueError("url 只接受 http / https；沙箱内的本地文件请改用 path 参数"
                         "（例如 path=\"index.html\"）")
    if u.scheme not in ("http", "https"):
        raise ValueError("url 只接受 http / https")
    if u.username or u.password:
        raise ValueError("dev server 地址不能带用户名 / 密码")
    host = (u.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("只允许预览本机 dev server（localhost / 127.0.0.1 / ::1）")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise ValueError("端口不合法")
    if port < 1024:
        raise ValueError("拒绝 1024 以下的端口（当前 %d）" % port)
    prefix, open_path = _split_target_path(u.path)
    host_url = ("[%s]" % host) if ":" in host else host
    origin = "%s://%s:%d" % (u.scheme, host_url, port)
    return u.scheme, host, port, prefix, open_path, origin


def _rewrite_location(value, target_origin, proxy_base):
    v = str(value or "")
    if v.startswith(target_origin):
        return proxy_base + v[len(target_origin):]
    if v.startswith("/") and not v.startswith("//"):
        return proxy_base + v
    return v


def _proxy_handler(target, token):
    scheme, host, port, base_path, _open_path, target_origin = target

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _proxy_base(self):
            return "http://127.0.0.1:%d/%s" % (self.server.server_address[1], token)

        def _strip_token(self):
            prefix = "/" + token
            path = self.path or "/"
            if path == prefix:
                return "/"
            if path.startswith(prefix + "/"):
                return path[len(prefix):]
            return None

        def _send_bridge(self):
            body = _BRIDGE_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _upstream(self):
            if scheme == "https":
                return http.client.HTTPSConnection(
                    host, port, timeout=30,
                    context=ssl._create_unverified_context())
            return http.client.HTTPConnection(host, port, timeout=30)

        def _proxy_http(self, method):
            path = self._strip_token()
            if path is None:
                self.send_error(403, "Forbidden")
                return
            if path.split("?", 1)[0] == _BRIDGE_PATH:
                self._send_bridge()
                return
            if str(self.headers.get("Upgrade") or "").lower() == "websocket":
                self._proxy_ws(path)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(length) if length > 0 else None
            headers = {}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in ("host", "connection", "proxy-connection", "keep-alive",
                          "transfer-encoding", "upgrade", "accept-encoding",
                          "content-length"):
                    continue
                if lk == "origin":
                    v = target_origin
                elif lk == "referer" and str(v).startswith(self._proxy_base()):
                    v = target_origin + str(v)[len(self._proxy_base()):]
                headers[k] = v
            host_url = ("[%s]" % host) if ":" in host else host
            headers["Host"] = "%s:%d" % (host_url, port)
            headers["Accept-Encoding"] = "identity"
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn = self._upstream()
            try:
                conn.request(method, base_path + path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                ctype = str(resp.getheader("Content-Type") or "")
                enc = str(resp.getheader("Content-Encoding") or "")
                inject = ("text/html" in ctype.lower()
                          and (not enc or enc.lower() in ("identity",)))
                if inject:
                    data = _inject_html(data, _bridge_tag(token))
                    # dev server 自己写死的绝对地址 / HMR 地址也走代理，避免 iframe 里绕回 target
                    try:
                        proxy_base = self._proxy_base()
                        data = data.replace(target_origin.encode("utf-8"),
                                            proxy_base.encode("utf-8"))
                        t_ws = target_origin.replace("https://", "wss://").replace(
                            "http://", "ws://")
                        p_ws = proxy_base.replace("https://", "wss://").replace(
                            "http://", "ws://")
                        data = data.replace(t_ws.encode("utf-8"), p_ws.encode("utf-8"))
                    except Exception:                # noqa: BLE001
                        pass
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    lk = k.lower()
                    if lk in ("connection", "keep-alive", "proxy-connection",
                              "transfer-encoding", "content-length", "upgrade",
                              "x-frame-options"):
                        continue
                    if lk == "content-encoding" and inject:
                        continue
                    if lk == "location":
                        v = _rewrite_location(v, target_origin, self._proxy_base())
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)
            finally:
                try:
                    conn.close()
                except Exception:                    # noqa: BLE001
                    pass

        def _proxy_ws(self, path):
            """WebSocket / HMR 隧道：握手直通后做双向字节转发。"""
            target_path = base_path + path
            host_url = ("[%s]" % host) if ":" in host else host
            lines = ["%s %s HTTP/1.1" % (self.command, target_path)]
            for k, v in self.headers.items():
                lk = k.lower()
                if lk == "host":
                    continue
                if lk == "origin":
                    v = target_origin
                lines.append("%s: %s" % (k, v))
            lines.append("Host: %s:%d" % (host_url, port))
            try:
                raw = socket.create_connection((host, port), timeout=20)
                upstream = (ssl._create_unverified_context().wrap_socket(
                    raw, server_hostname=host) if scheme == "https" else raw)
            except OSError as e:
                self.send_error(502, "dev server 连接失败：%s" % e)
                return
            try:
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if b"\r\n\r\n" not in buf:
                    self.send_error(502, "dev server 没有返回 WebSocket 握手")
                    return
                head, rest = buf.split(b"\r\n\r\n", 1)
                self.wfile.write(head + b"\r\n\r\n")
                self.wfile.flush()
                if rest:
                    self.connection.sendall(rest)
                self.close_connection = True
                client = self.connection
                client.settimeout(300)
                upstream.settimeout(300)
                while True:
                    ready, _, _ = select.select([client, upstream], [], [], 60)
                    if not ready:
                        break
                    for s in ready:
                        data = s.recv(65536)
                        if not data:
                            return
                        (upstream if s is client else client).sendall(data)
            except OSError:
                return
            finally:
                try:
                    upstream.close()
                except Exception:                    # noqa: BLE001
                    pass

        def do_GET(self):
            self._proxy_http("GET")

        def do_HEAD(self):
            self._proxy_http("HEAD")

        def do_POST(self):
            self._proxy_http("POST")

        def do_PUT(self):
            self._proxy_http("PUT")

        def do_PATCH(self):
            self._proxy_http("PATCH")

        def do_DELETE(self):
            self._proxy_http("DELETE")

        def do_OPTIONS(self):
            self._proxy_http("OPTIONS")

    return H


def start_dev(raw):
    """启动（或复用）本机 dev server 反向代理，返回带 token 的 base URL。

    返回的地址已带上 URL 里的页面路径（`/index.html`），不是只有站点根；
    这样 `open_preview(url=...)` 打开的就是用户指定的那一页。
    """
    global _SERVER, _THREAD, _ROOT, _MODE, _DEV_TARGET, _DEV_TOKEN
    global _DEV_PREFIX, _DEV_OPEN
    target = _parse_local_target(raw)
    origin, prefix, open_path = target[5], target[3], target[4]
    # 先探一下端口：dev server 没起时直接给明确错误，而不是让 iframe 停在 502
    try:
        probe = socket.create_connection((target[1], target[2]), timeout=1.0)
        probe.close()
    except OSError as e:
        raise OSError("dev server 未运行或不可达（%s:%d）：%s"
                      % (target[1], target[2], e))
    with _LOCK:
        if (_MODE == "dev" and _DEV_TARGET == origin and _SERVER
                and _THREAD and _THREAD.is_alive()):
            _DEV_PREFIX, _DEV_OPEN = prefix, open_path
            return ("http://127.0.0.1:%d/%s%s"
                    % (_SERVER.server_address[1], _DEV_TOKEN,
                       urllib.parse.quote(open_path, safe="/")))
        _stop_locked()
        token = secrets.token_urlsafe(12)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _proxy_handler(target, token))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        _SERVER, _THREAD, _ROOT = srv, th, ""
        _MODE, _DEV_TARGET, _DEV_TOKEN = "dev", origin, token
        _DEV_PREFIX, _DEV_OPEN = prefix, open_path
        return ("http://127.0.0.1:%d/%s%s"
                % (srv.server_address[1], token,
                   urllib.parse.quote(open_path, safe="/")))


def start(root):
    """启动（或复用）预览服务，返回 base URL。"""
    global _SERVER, _THREAD, _ROOT, _MODE, _DEV_TARGET, _DEV_TOKEN
    root = os.path.abspath(root)
    with _LOCK:
        if (_MODE == "static" and _SERVER and _ROOT == root
                and _THREAD and _THREAD.is_alive()):
            return "http://127.0.0.1:%d/" % _SERVER.server_address[1]
        _stop_locked()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        _SERVER, _THREAD, _ROOT = srv, th, root
        _MODE, _DEV_TARGET, _DEV_TOKEN = "static", "", ""
        return "http://127.0.0.1:%d/" % srv.server_address[1]


def _stop_locked():
    global _SERVER, _THREAD, _ROOT, _MODE, _DEV_TARGET, _DEV_TOKEN
    global _DEV_PREFIX, _DEV_OPEN
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
    _MODE, _DEV_TARGET, _DEV_TOKEN = "static", "", ""
    _DEV_PREFIX, _DEV_OPEN = "", "/"


def stop():
    with _LOCK:
        _stop_locked()
