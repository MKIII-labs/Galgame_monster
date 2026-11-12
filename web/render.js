'use strict';

/* ============================================================
   对话渲染辅助（消息行构建仍在 chat.js）
   - messagesSignature：轻量签名，用于判断「窗口与内容都没变」时跳过整块重排
   ============================================================ */
window.GM_RENDER = {
  messagesSignature(msgs, start, total) {
    let h = total + '#' + start;
    for (let i = start; i < total; i++) {
      const m = msgs[i] || {};
      const c = String(m.content == null ? '' : m.content);
      h += '|' + i + ':' + (m.role || '') + ':' + c.length
        + ':' + c.slice(0, 12) + c.slice(-12)
        + ':' + (m.done_at ? 1 : 0)
        + ':' + ((m.blocks || []).length)
        + ':' + String(m.reasoning || '').length
        + ':' + ((m.tools || []).length)
        + ':' + ((m.references || []).length)
        + ':' + (((m.plan || {}).steps || []).length)
        + ':' + (m.end_reason ? 1 : 0);
    }
    return h;
  },
};
