'use strict';

/* ============================================================
   对话里的 Skill 卡片
   ------------------------------------------------------------
   模型调用 skill.read（原生名 skill_read）时，后端会推 tool / tool_done
   事件。这里把这条工具调用渲染成一张独立卡片（和子代理卡一样留在正文外），
   而不是塞进过程折叠里——用户能直接看到「这一轮用了哪个 Skill」。
   历史消息由 appendSkillCards() 从落库的 tools 还原，外观与实时一致。
   ============================================================ */

const SKILL_TOOL_NAMES = ['skill.read', 'skill_read'];

function isSkillTool(name) {
  return SKILL_TOOL_NAMES.indexOf(String(name || '')) >= 0;
}

function skillArgName(args) {
  const a = args || {};
  return String(a.name || a.id || '').trim();
}

function skillResultParts(text) {
  const raw = String(text || '');
  const m = /^【Skill：([^】]+)】\r?\n?/.exec(raw);
  if (!m) return { name: '', body: raw, failed: /^❌/.test(raw) };
  return { name: m[1], body: raw.slice(m[0].length), failed: false };
}

function skillOpenSettings() {
  try {
    if (typeof openModal === 'function' && typeof setSettingsTab === 'function') {
      openModal();
      setSettingsTab('ext');
    }
  } catch (e) { /* 设置未加载时忽略 */ }
}

/* 建卡：head 始终可见（名字 + 状态），正文默认收起，点一下展开步骤原文 */
function buildSkillCard(toolName, args) {
  const card = document.createElement('div');
  card.className = 'skcard running';
  card._skillTool = toolName;
  const name = skillArgName(args);
  card._skillName = name;
  card.innerHTML =
    '<div class="skcard-head" role="button" tabindex="0" aria-label="展开 Skill 内容">'
      + '<span class="skcard-ico">' + icon('book') + '</span>'
      + '<span class="skcard-badge">SKILL</span>'
      + '<span class="skcard-name"></span>'
      + '<span class="skcard-state">读取中…</span>'
      + '<span class="skcard-meta"></span>'
      + '<span class="skcard-caret">' + icon('chevron') + '</span>'
    + '</div>'
    + '<div class="skcard-body">'
      + '<div class="skcard-desc">AI 已加载这套流程，并按其中的步骤执行。</div>'
      + '<pre class="skcard-text"></pre>'
      + '<div class="skcard-actions"><button type="button" class="skcard-open">在设置中查看</button></div>'
    + '</div>';
  card.querySelector('.skcard-name').textContent = name || '未知 Skill';
  const toggle = () => {
    if (!card.classList.contains('has-result')) return;
    const body = card.querySelector('.skcard-body');
    const next = !card.classList.contains('open');
    if (typeof foldTransition === 'function') {
      foldTransition(card, body, next, { cls: 'open', positive: true, ms: 180 });
    } else {
      card.classList.toggle('open', next);
    }
  };
  const head = card.querySelector('.skcard-head');
  head.addEventListener('click', toggle);
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
  card.querySelector('.skcard-open').onclick = (e) => {
    e.stopPropagation();
    skillOpenSettings();
  };
  return card;
}

/* 收尾：状态、耗时、字数；失败（Skill 不存在 / 已停用）保持红色提示 */
function finishSkillCard(card, obj) {
  if (!card) return;
  obj = obj || {};
  card.classList.remove('running');
  const ok = obj.ok !== false;
  const parts = skillResultParts(obj.result || '');
  if (parts.name) {
    card._skillName = parts.name;
    card.querySelector('.skcard-name').textContent = parts.name;
  }
  const failed = !ok || parts.failed;
  card.classList.toggle('failed', failed);
  card.classList.toggle('ok', !failed);
  const st = card.querySelector('.skcard-state');
  if (st) st.textContent = failed ? '未找到' : '已加载';
  const meta = card.querySelector('.skcard-meta');
  if (meta) {
    const bits = [];
    if (obj.ms != null) bits.push(typeof fmtToolMs === 'function' ? fmtToolMs(obj.ms) : (obj.ms + 'ms'));
    const chars = (parts.body || '').replace(/\s+/g, '').length;
    if (chars && !failed) bits.push(chars + ' 字');
    meta.textContent = bits.join(' · ');
  }
  const pre = card.querySelector('.skcard-text');
  if (pre) pre.textContent = (parts.body || '').trim();
  const desc = card.querySelector('.skcard-desc');
  if (desc) {
    desc.textContent = failed
      ? '没有加载成功（Skill 可能被删除或停用），模型会按普通流程继续。'
      : 'AI 已加载这套流程，并按其中的步骤执行。';
  }
  card.classList.add('has-result');
  if (failed) card.classList.add('open');
}

/* 历史回显：从落库的 tools 里挑出 Skill 调用，拼成常驻卡片 */
function appendSkillCards(wrap, tools) {
  if (!wrap) return;
  (tools || []).forEach((t) => {
    if (!t || !isSkillTool(t.tool)) return;
    const card = buildSkillCard(t.tool, t.args);
    finishSkillCard(card, { result: t.result, ok: t.ok, ms: t.ms });
    wrap.appendChild(card);
  });
}
