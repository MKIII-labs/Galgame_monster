# -*- coding: utf-8 -*-
"""Computer Use · 页面快照

「结构化优先」的观测通道：把可交互元素编成 [1][2][3]，模型用编号寻址，
比截图猜坐标准，也比整页 HTML 便宜一个数量级（一页约 800–2000 字）。

编号由页面内的 window.__gm 维护：同一次页面生命周期里编号稳定，
导航后重新开始编号（旧编号直接失效）。
"""

# 采集脚本：返回 JSON，由 Python 侧排版。
# 只收「可交互元素 + 画布 + 正文摘要」，不收整棵 DOM —— 那是 token 黑洞。
_JS_COLLECT = r"""
(function (start, limit) {
  var G = window.__gm;
  if (!G) { G = window.__gm = { seq: 0, els: {}, cursor: 0 }; }
  function vis(el) {
    var st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    var r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  }
  function tick(el) {
    for (var k in G.els) { if (G.els[k] === el) return k; }
    var n = String(++G.seq);
    G.els[n] = el;
    return n;
  }
  function label(el) {
    var t = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    if (!t) {
      var tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        t = el.getAttribute('placeholder') || el.value || '';
      } else {
        t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      }
    }
    return t.slice(0, 48);
  }
  var SEL = 'a,button,input,select,textarea,summary,[role="button"],[role="link"],'
          + '[role="tab"],[role="checkbox"],[role="menuitem"],[role="switch"],'
          + '[onclick],[contenteditable="true"]';
  var nodes = [].slice.call(document.querySelectorAll(SEL)).filter(vis);
  var all = nodes.map(function (el) {
    var r = el.getBoundingClientRect();
    var id = el.id ? '#' + el.id : '';
    return {
      ref: +tick(el),
      tag: el.tagName.toLowerCase() + id,
      role: el.getAttribute('role') || '',
      name: label(el),
      type: (el.type || '').toString().toLowerCase(),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      pass: (el.type || '').toLowerCase() === 'password',
      checked: (typeof el.checked === 'boolean') ? el.checked : null,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height)
    };
  });
  var canvases = [].slice.call(document.querySelectorAll('canvas')).filter(vis).map(function (c) {
    var r = c.getBoundingClientRect();
    return { id: c.id || '', w: Math.round(r.width), h: Math.round(r.height) };
  });
  var body = document.body ? (document.body.innerText || '') : '';
  return {
    url: location.href,
    title: document.title,
    vw: innerWidth, vh: innerHeight,
    scrollY: Math.round(window.scrollY || 0),
    scrollMax: Math.max(0, (document.documentElement.scrollHeight || 0) - innerHeight),
    count: all.length,
    start: start,
    items: all.slice(start, start + limit),
    canvases: canvases.slice(0, 6),
    text: body.replace(/\n{3,}/g, '\n\n').slice(0, 1200)
  };
})(%d, %d)
"""


def collect(start=0, limit=40):
    """取一页结构化快照（start 用于翻页）。"""
    from app.computer import browser
    return browser.evaluate(_JS_COLLECT % (max(0, int(start)), max(1, min(80, int(limit)))),
                            timeout=25)


def _one(it):
    tag = it.get("tag") or "?"
    name = (it.get("name") or "").replace("\n", " ").strip()
    bits = ["[%s]" % it.get("ref"), tag]
    if it.get("role"):
        bits.append("role=%s" % it["role"])
    if name:
        bits.append("「%s」" % name)
    if it.get("type") and it["type"] not in ("", "text"):
        bits.append(it["type"])
    if it.get("checked") is True:
        bits.append("已勾选")
    if it.get("disabled"):
        bits.append("禁用")
    if it.get("pass"):
        bits.append("← 密码框，禁止读取/填写")
    bits.append("(%s,%s)" % (it.get("x"), it.get("y")))
    return " ".join(bits)


def render(data, max_chars=4000):
    """把采集结果排版成给模型看的文本。纯函数，便于测试。"""
    d = data or {}
    lines = ["# %s · %s" % (d.get("title") or "(无标题)", d.get("url") or ""),
             "视口 %sx%s · 滚动 %s/%s · 可交互元素 %s 个"
             % (d.get("vw"), d.get("vh"), d.get("scrollY"), d.get("scrollMax"),
                d.get("count") or 0)]
    items = d.get("items") or []
    for it in items:
        lines.append(_one(it))
    for c in (d.get("canvases") or []):
        lines.append("[canvas%s] %sx%s  ← 自绘内容，需要时用 computer_shot"
                     % (("#" + c["id"]) if c.get("id") else "", c.get("w"), c.get("h")))
    text = (d.get("text") or "").strip()
    if text:
        lines.append("")
        lines.append("正文摘要：")
        lines.append(text[:600] + ("…" if len(text) > 600 else ""))
    start, count = int(d.get("start") or 0), int(d.get("count") or 0)
    shown = start + len(items)
    if count > shown:
        lines.append("")
        lines.append("（共 %d 个可交互元素，已显示 %d–%d；继续看用 start=%d）"
                     % (count, start + 1, shown, shown))
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…（快照过长已截断；可用 start 翻页或调小 limit）"
    return out


def element(ref):
    """按编号取元素信息（给电脑操作层用）。"""
    from app.computer import browser
    js = r"""
    (function (ref) {
      var G = window.__gm;
      var el = G && G.els[ref];
      if (!el) return null;
      el.scrollIntoView({ block: 'center', inline: 'center' });
      var r = el.getBoundingClientRect();
      var opts = null;
      if (el.tagName === 'SELECT') {
        opts = [].slice.call(el.options).map(function (o) {
          return { v: o.value, t: (o.text || '').slice(0, 40) };
        }).slice(0, 40);
      }
      return {
        tag: el.tagName.toLowerCase(),
        type: (el.type || '').toLowerCase(),
        name: (el.getAttribute('aria-label') || el.innerText || el.value || '')
               .replace(/\s+/g, ' ').trim().slice(0, 60),
        pass: (el.type || '').toLowerCase() === 'password',
        disabled: !!el.disabled,
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
        w: Math.round(r.width), h: Math.round(r.height),
        options: opts
      };
    })(%d)
    """ % int(ref)
    return browser.evaluate(js, timeout=10)
