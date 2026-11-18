'use strict';

/* ============================================================
   从某条 AI 回复处创建分支
   ------------------------------------------------------------
   按消息位置精确切片：以当前会话的前 N 条消息复制出一条新会话，
   原会话原样保留。不依赖任何存档快照文件。
   ============================================================ */

async function forkFromMessage(index) {
  const s = curSession();
  if (!s || typeof index !== 'number') return;
  if (!window.confirm('以这条回复为起点，复制出一条新的会话线？\n（当前会话原样保留）')) return;
  try {
    const r = await api('/api/save/branch', { session_id: s.id, upto: index + 1 });
    if (!r || !r.ok) throw new Error((r && r.error) || '失败');
    applySessions(r, { keepCurrent: false });   // 分叉：服务端把 current 指向新会话
    toast('已分叉出新会话');
    renderMessages();
  } catch (e) { toast('创建分支失败：' + e); }
}
