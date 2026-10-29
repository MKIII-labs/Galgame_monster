/* ============================================================
   GAL 剧场 —— 大厅功能入口
   ------------------------------------------------------------
   剧场使用独立会话（gal_scope=gal），不和 Solo / IDE 共用历史与记忆。
   画像 / 知识库 / 全局规则默认关闭，可按场勾选「只读」。
   ============================================================ */
'use strict';

const GAL_HALL = { pick: '', flags: { profile: false, kb: false, rules: false } };

function galHallFlags(s) {
  return {
    profile: !!(s && s.gal_use_profile),
    kb: !!(s && s.gal_use_kb),
    rules: !!(s && s.gal_use_rules),
  };
}

function galHallConfigFlags() {
  const m = (state.config && state.config.gal_memory) || {};
  return { profile: !!m.profile, kb: !!m.kb, rules: !!m.rules };
}

function galHallSessions() {
  return (state.sessions.sessions || [])
    .filter((s) => (s.gal_scope || 'main') === 'gal')
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
}

function galHallSaveConfigFlags() {
  if (!state.config) state.config = {};
  state.config.gal_memory = Object.assign({}, GAL_HALL.flags);
  try { api('/api/config', { gal_memory: state.config.gal_memory }).catch(() => {}); } catch (e) { /* 忽略 */ }
}

function galHallSyncFlags() {
  const set = (id, on) => { const el = document.getElementById(id); if (el) el.checked = !!on; };
  set('gal-use-profile', GAL_HALL.flags.profile);
  set('gal-use-kb', GAL_HALL.flags.kb);
  set('gal-use-rules', GAL_HALL.flags.rules);
}

function galHallRenderList(root) {
  if (!root) return;
  const box = root.querySelector('#gal-hall-list');
  if (!box) return;
  box.innerHTML = '';
  const list = galHallSessions();
  if (!list.length) {
    const none = document.createElement('div');
    none.className = 'hall-empty';
    none.textContent = '还没有剧场记录；点「进入剧场」开一场。';
    box.appendChild(none);
    return;
  }
  list.forEach((s) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'gal-hall-session' + (s.id === GAL_HALL.pick ? ' active' : '');
    const name = document.createElement('span');
    name.className = 'gal-hall-session-name';
    name.textContent = s.title || 'GAL 剧场';
    const meta = document.createElement('span');
    meta.className = 'gal-hall-session-meta';
    const memN = Number(s.mem_count || 0);
    meta.textContent = (memN ? ('记忆 ' + memN + ' 条') : '空场') +
      (s.gal_character_id ? ' · 已有角色' : '');
    btn.appendChild(name);
    btn.appendChild(meta);
    btn.onclick = () => {
      GAL_HALL.pick = s.id;
      GAL_HALL.flags = galHallFlags(s);
      galHallSyncFlags();
      galHallRenderList(document.getElementById('gal-hall-root'));
    };
    box.appendChild(btn);
  });
}

async function galHallEnter(newOne) {
  // 先记住进场前的主会话，再创建 / 选中剧场会话（顺序反了会记成剧场自己）
  const prev = state.sessions.current;
  const prevSession = (state.sessions.sessions || []).find((s) => s.id === prev);
  if (prevSession && (prevSession.gal_scope || 'main') !== 'gal') {
    state.galReturnSession = prev;                 // 离开剧场时回到这里
  }
  let sid = newOne ? '' : (GAL_HALL.pick || (galHallSessions()[0] || {}).id || '');
  if (!sid) {
    const r = await api('/api/session/new', Object.assign({
      gal_scope: 'gal', title: 'GAL 剧场',
      gal_use_profile: GAL_HALL.flags.profile,
      gal_use_kb: GAL_HALL.flags.kb,
      gal_use_rules: GAL_HALL.flags.rules,
    }));
    applySessionsPayload(r.sessions);
    sid = r.sessions.current;
  }
  await api('/api/gal/session', Object.assign({
    id: sid, gal_enabled: true,
    gal_use_profile: GAL_HALL.flags.profile,
    gal_use_kb: GAL_HALL.flags.kb,
    gal_use_rules: GAL_HALL.flags.rules,
  }));
  await selectSession(sid);
  if (typeof setUiMode === 'function') await setUiMode('gal');
}

function galHallMount(root) {
  root.id = 'gal-hall-root';
  root.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'gal-hall';

  const hero = document.createElement('div');
  hero.className = 'gal-hall-hero';
  const t = document.createElement('div');
  t.className = 'gal-hall-title';
  t.textContent = 'GAL 剧场';
  const d = document.createElement('p');
  d.className = 'hint';
  d.textContent = '和角色演一段故事。剧场用独立会话记忆，不与 Solo / IDE 混在一起；'
    + '画像、知识库、全局规则默认不读，可按场打开（只读，写入永远只进本场记忆）。';
  hero.appendChild(t);
  hero.appendChild(d);

  const listHead = document.createElement('div');
  listHead.className = 'gal-hall-label';
  listHead.textContent = '继续上一场';
  const list = document.createElement('div');
  list.id = 'gal-hall-list';
  list.className = 'gal-hall-list';

  const memHead = document.createElement('div');
  memHead.className = 'gal-hall-label';
  memHead.textContent = '允许剧场读取';
  const opts = document.createElement('div');
  opts.className = 'gal-hall-opts';
  [['gal-use-profile', 'profile', '用户画像（只读）'],
   ['gal-use-kb', 'kb', '知识库（只读）'],
   ['gal-use-rules', 'rules', '全局规则']].forEach(([id, key, label]) => {
    const row = document.createElement('label');
    row.className = 'check-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = id;
    cb.onchange = () => {
      GAL_HALL.flags[key] = !!cb.checked;
      galHallSaveConfigFlags();
    };
    const txt = document.createElement('span');
    txt.textContent = label;
    row.appendChild(cb);
    row.appendChild(txt);
    opts.appendChild(row);
  });

  const acts = document.createElement('div');
  acts.className = 'gal-hall-actions';
  const enter = document.createElement('button');
  enter.type = 'button';
  enter.className = 'ghost wide';
  enter.textContent = '进入剧场';
  enter.onclick = () => galHallEnter(false).catch((e) => toastErr('进入剧场', e));
  const fresh = document.createElement('button');
  fresh.type = 'button';
  fresh.className = 'ghost';
  fresh.textContent = '新建一场';
  fresh.onclick = () => galHallEnter(true).catch((e) => toastErr('新建剧场', e));
  acts.appendChild(enter);
  acts.appendChild(fresh);

  wrap.appendChild(hero);
  wrap.appendChild(listHead);
  wrap.appendChild(list);
  wrap.appendChild(memHead);
  wrap.appendChild(opts);
  wrap.appendChild(acts);
  root.appendChild(wrap);

  const list0 = galHallSessions();
  GAL_HALL.pick = list0.length ? list0[0].id : '';
  GAL_HALL.flags = list0.length ? galHallFlags(list0[0]) : galHallConfigFlags();
  galHallSyncFlags();
  galHallRenderList(root);
}

hallRegister({
  id: 'gal',
  name: 'GAL 剧场',
  icon: 'i-film',
  tag: '演出',
  desc: '和角色演一段故事：独立会话记忆，可按场选择是否读取画像 / 知识库 / 全局规则。',
  meta: '独立记忆 · 角色 / 剧本 / 声音 · 图片背景',
  mount: galHallMount,
});
