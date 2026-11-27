'use strict';

/* ============================================================
   对话渲染辅助（消息行构建仍在 chat.js）
   - messageSig：单条消息的轻量签名，用于按 idx 判断哪一条真的变了
   - messagesSignature：整窗签名，用于判断「窗口与内容都没变」时跳过整块重排
   ============================================================ */
(function () {
  function messageSig(m) {
    const it = m || {};
    const c = String(it.content == null ? '' : it.content);
    return (it.role || '') + ':' + c.length
      + ':' + c.slice(0, 12) + c.slice(-12)
      + ':' + (it.done_at ? 1 : 0)
      + ':' + ((it.blocks || []).length)
      + ':' + String(it.reasoning || '').length
      + ':' + ((it.tools || []).length)
      + ':' + ((it.references || []).length)
      + ':' + (((it.plan || {}).steps || []).length)
      + ':' + (it.end_reason ? 1 : 0);
  }

  window.GM_RENDER = {
    messageSig: messageSig,
    messagesSignature(msgs, start, total) {
      let h = total + '#' + start;
      for (let i = start; i < total; i++) h += '|' + i + ':' + messageSig(msgs[i]);
      return h;
    },
  };
})();
