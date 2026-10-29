'use strict';

/* ============================================================
   功能大厅：研究道场
   ------------------------------------------------------------
   这个文件不认识「数学」「建模」「科研」——场景的差异全在 JSON 包里。
   客户端只做三件事：
     1) 按 primitive 渲染题面
     2) 把用户的判断交给服务端判分（答案不在前端）
     3) 把能力画像、方法卡与断点存进 localStorage
   本地存档键：gm.dojo.v1（与数学竞技场一样的可迁移版本号）
   ============================================================ */

const DOJO_KEY = 'gm.dojo.v1';
const DOJO_RESEARCH_KEY = 'gm.dojo.research.v1';
const DOJO_RESEARCH_DONE_KEY = 'gm.dojo.research.done.v1';
const DOJO_RESEARCH_FILTER_KEY = 'gm.dojo.research.filter.v1';
const DOJO_REVIEW_DAYS = [0, 1, 3, 7, 15];
const DOJO_DAY_MS = 24 * 3600 * 1000;
const DOJO_HISTORY_LIMIT = 40;

const DOJO_ABILITY_NAMES = {
  framing: '定义问题',
  assumption: '建立假设',
  method: '选择方法',
  condition: '判断适用性',
  evidence: '证据',
  robustness: '稳健性',
  argument: '论证',
  reflection: '反思迁移',
};

const dojoState = {
  gen: 0,
  el: {},
  packs: [],
  pack: null,
  unit: null,
  session: null,
  index: 0,
  answered: false,
  busy: false,
  startedAt: 0,
  pick: null,
  keyHandler: null,
  store: null,
  cases: [],
  caseSession: null,
  lastCaseOut: null,
  mode: 'research',
  collections: [],
  inquiries: [],
  research: null,
  researchLastOut: null,
  researchLastValue: null,
  researchFilter: { subject: 'all', status: 'all', q: '' },
  researchRecordsView: false,
  authoringView: false,
  authoring: null,
};

/* ---------------- 基础工具 ---------------- */

function dojoQ(sel, root) {
  return (root || dojoState.el.root || document).querySelector(sel);
}

function dojoEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function dojoMsg(text, isErr) {
  const el = dojoState.el.msg;
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('err', !!isErr);
}

function dojoDefaultStore() {
  return {
    version: 1,
    pack: '',
    index: 0,
    progress: {},
    answered: {},
    abilities: {},
    cards: [],
    assessments: {},
    wrong: [],
    streak: 0,
    bestStreak: 0,
    history: [],
  };
}

function dojoLoadStore() {
  let raw = null;
  try { raw = JSON.parse(localStorage.getItem(DOJO_KEY) || 'null'); } catch (e) { raw = null; }
  const base = dojoDefaultStore();
  if (!raw || typeof raw !== 'object') return base;
  const out = Object.assign(base, raw);
  out.version = 1;
  out.index = Math.max(0, Number(out.index) || 0);
  out.progress = (out.progress && typeof out.progress === 'object') ? out.progress : {};
  out.streak = Math.max(0, Number(out.streak) || 0);
  out.bestStreak = Math.max(out.streak, Number(out.bestStreak) || 0);
  out.answered = (out.answered && typeof out.answered === 'object') ? out.answered : {};
  out.abilities = (out.abilities && typeof out.abilities === 'object') ? out.abilities : {};
  out.cards = Array.isArray(out.cards) ? out.cards : [];
  out.assessments = (out.assessments && typeof out.assessments === 'object') ? out.assessments : {};
  out.wrong = Array.isArray(out.wrong) ? out.wrong : [];
  out.history = Array.isArray(out.history) ? out.history : [];
  return out;
}

function dojoSaveStore() {
  try { localStorage.setItem(DOJO_KEY, JSON.stringify(dojoState.store)); } catch (e) { /* 隐私模式 / 配额满 */ }
}

function dojoReviewDays(stage) {
  return DOJO_REVIEW_DAYS[Math.max(0, Math.min(DOJO_REVIEW_DAYS.length - 1, stage))];
}

/* 每个包各自记进度：换场景回来时不会从第一题重来 */
function dojoSetProgress(index) {
  const store = dojoState.store;
  store.index = Math.max(0, Number(index) || 0);
  if (!store.progress) store.progress = {};
  if (dojoState.pack) store.progress[dojoState.pack.id] = store.index;
}

function dojoDueCards() {
  const now = Date.now();
  return dojoState.store.cards.filter((c) => (Number(c.next_at) || 0) <= now);
}

/* ---------------- 挂载 ---------------- */

function dojoMount(root) {
  const gen = ++dojoState.gen;
  dojoState.el = { root: root };
  dojoState.packs = [];
  dojoState.pack = null;
  dojoState.unit = null;
  dojoState.session = null;
  dojoState.index = 0;
  dojoState.answered = false;
  dojoState.busy = false;
  dojoState.pick = null;
  dojoState.store = dojoLoadStore();
  dojoState.review = null;
  dojoState.assess = null;
  dojoState.repro = [];
  dojoState.reproPack = null;
  dojoState.mode = 'research';   // 道场只保留深度研究，基础诊断已归并到数学竞技场
  dojoState.collections = [];
  dojoState.inquiries = [];
  dojoState.research = null;
  dojoState.researchLastOut = null;
  dojoState.researchFilter = dojoResearchFilterLoad();
  dojoState.paperIndex = [];
  dojoState.paperContest = 'CMC';
  dojoState.paperYear = 0;
  dojoState.paperView = false;

  root.classList.add('dojo-root');
  root.innerHTML = [
    '<div class="dojo-top">',
    '  <button type="button" class="dojo-pill dojo-pill-btn mono" id="dojo-pack" title="切换场景">载入中 ▾</button>',
    '  <span class="dojo-pill" id="dojo-progress">— / —</span>',
    '  <span class="dojo-pill" id="dojo-streak">连对 0</span>',
    '  <div class="dojo-top-right">',
    '    <button type="button" class="dojo-pill dojo-pill-btn" id="dojo-today" title="按最弱能力维度挑一道没做过的题">今日一题</button>',
    '    <button type="button" class="dojo-pill dojo-pill-btn" id="dojo-import" title="用自己的材料生成场景包">＋ 导入材料</button>',
    '    <button type="button" class="dojo-pill dojo-pill-btn hidden" id="dojo-research-exit" title="回到研究主题列表">退出研究</button>',
    '    <button type="button" class="dojo-pill dojo-pill-btn" id="dojo-review" title="按间隔重复复习方法卡">复习 0</button>',
    '    <button type="button" class="dojo-pill dojo-pill-btn" id="dojo-wrong" title="回到最早答错的题">错题 0</button>',
    '  </div>',
    '</div>',
    '<div class="dojo-pack-menu hidden" id="dojo-pack-menu"></div>',
    '<div class="dojo-layout">',
    '  <main class="dojo-main" id="dojo-main">',
    '    <div class="dojo-research hidden" id="dojo-research-panel"></div>',
    '    <div class="dojo-card" id="dojo-card">',
    '      <div class="dojo-meta">',
    '        <span class="dojo-chip ability" id="dojo-ability">—</span>',
    '        <span class="dojo-chip level" id="dojo-judge">—</span>',
    '        <span class="dojo-chip minutes" id="dojo-minutes">—</span>',
    '      </div>',
    '      <div class="dojo-situation" id="dojo-situation">正在载入今日一题……</div>',
    '      <div id="dojo-answer"></div>',
    '      <div class="dojo-actions">',
    '        <button type="button" class="dojo-btn primary" id="dojo-submit">提交判断</button>',
    '        <button type="button" class="dojo-btn" id="dojo-skip">先跳过</button>',
    '      </div>',
    '      <div class="dojo-msg" id="dojo-msg"></div>',
    '      <div class="dojo-reveal hidden" id="dojo-reveal"></div>',
    '    </div>',
    '    <div class="dojo-review hidden" id="dojo-review-panel"></div>',
    '    <div class="dojo-assess hidden" id="dojo-assess-panel"></div>',
    '    <div class="dojo-map hidden" id="dojo-map-panel"></div>',
    '    <div class="dojo-repro hidden" id="dojo-repro-panel"></div>',
    '  </main>',
    '  <aside class="dojo-side" id="dojo-side"></aside>',
    '</div>',
  ].join('');

  ['pack', 'progress', 'streak', 'review', 'import', 'today', 'wrong',
    'main', 'ability', 'judge', 'minutes',
    'situation', 'answer', 'submit', 'skip', 'msg', 'reveal', 'side']
    .forEach((k) => { dojoState.el[k] = dojoQ('#dojo-' + k); });
  dojoState.el.packMenu = dojoQ('#dojo-pack-menu');
  dojoState.el.card = dojoQ('#dojo-card');
  dojoState.el.reviewPanel = dojoQ('#dojo-review-panel');
  dojoState.el.assessPanel = dojoQ('#dojo-assess-panel');
  dojoState.el.mapPanel = dojoQ('#dojo-map-panel');
  dojoState.el.reproPanel = dojoQ('#dojo-repro-panel');
  dojoState.el.researchPanel = dojoQ('#dojo-research-panel');
  dojoState.el.researchExit = dojoQ('#dojo-research-exit');

  const scroller = root.closest('.hall-body') || document.getElementById('hall-body');
  if (scroller) scroller.scrollTop = 0;

  dojoState.el.submit.onclick = () => dojoSubmit();
  dojoState.el.skip.onclick = () => dojoNext(true);
  dojoState.el.import.onclick = () => dojoOpenImport();
  dojoState.el.researchExit.onclick = () => dojoExitResearch();
  dojoState.el.today.onclick = () => dojoToday();
  dojoState.el.wrong.onclick = () => dojoOpenWrong();
  dojoState.el.side.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-weakest]');
    if (btn) dojoPracticeAbility(dojoWeakestAbility());
  });
  dojoState.el.pack.onclick = () => dojoTogglePackMenu();
  dojoState.packDocHandler = (e) => {
    const m = dojoState.el.packMenu;
    if (!m || m.classList.contains('hidden')) return;
    if (m.contains(e.target) || (dojoState.el.pack && dojoState.el.pack.contains(e.target))) return;
    m.classList.add('hidden');
  };
  document.addEventListener('click', dojoState.packDocHandler);
  dojoState.el.review.onclick = () => dojoStartReview();

  dojoState.keyHandler = (e) => {
    if (dojoState.caseSession && e.key === 'Escape') {
      dojoExitCase();
      return;
    }
    const review = dojoState.review;
    if (review) {
      if (e.key === 'Escape') {
        dojoExitReview();
        return;
      }
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        if (!review.flipped) dojoFlipReview();
        return;
      }
      if (review.flipped && (e.key === '1' || e.key === '2')) {
        e.preventDefault();
        dojoRateReview(e.key === '2');
      }
      return;
    }
    if (dojoState.assess) {
      const a = dojoState.assess;
      const box = dojoState.el.assessPanel;
      if (e.key === 'Escape') {
        dojoExitAssess();
        return;
      }
      if (a.revealed) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          const next = box && dojoQ('#dojo-assess-next', box);
          if (next) next.click();
        }
        return;
      }
      if (/^[1-9]$/.test(e.key)) {
        const btns = box ? box.querySelectorAll('#dojo-assess-options .dojo-opt') : [];
        const btn = btns[Number(e.key) - 1];
        if (btn) {
          e.preventDefault();
          btn.click();
        }
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        dojoAssessSubmit();
      }
      return;
    }
    if (dojoState.reproPack) {
      if (e.key === 'Escape') {
        dojoExitRepro();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        dojoRunRepro();
      }
      return;
    }
    if (e.key !== 'Enter' || e.shiftKey) return;
    // 只在数值/表达式输入框里用 Enter 提交；选项与排序按钮保留自己的默认行为
    if (!e.target || e.target.tagName !== 'INPUT') return;
    if (!dojoState.answered && dojoState.unit) {
      e.preventDefault();
      dojoSubmit();
    }
  };
  document.addEventListener('keydown', dojoState.keyHandler);

  dojoRenderSide();
  dojoBoot(gen).catch((e) => {
    if (gen !== dojoState.gen) return;
    dojoMsg('载入失败：' + ((e && e.message) || e), true);
  });
}

function dojoUnmount() {
  dojoState.gen += 1;
  dojoCloseImport();
  dojoClosePackMenu();
  dojoCloseReproEditor();
  if (dojoState.packDocHandler) {
    document.removeEventListener('click', dojoState.packDocHandler);
    dojoState.packDocHandler = null;
  }
  if (dojoState.keyHandler) {
    document.removeEventListener('keydown', dojoState.keyHandler);
    dojoState.keyHandler = null;
  }
  dojoState.el = {};
  dojoState.unit = null;
  dojoState.session = null;
  dojoState.pick = null;
  dojoState.review = null;
  dojoState.assess = null;
  dojoState.reproPack = null;
  dojoState.caseSession = null;
  dojoState.lastCaseOut = null;
  dojoState.research = null;
  dojoState.researchLastOut = null;
}

/* ---------------- 启动 ---------------- */

async function dojoBoot(gen) {
  const out = await api('/api/hall/dojo/packs', {});
  if (gen !== dojoState.gen) return;
  dojoState.packs = out.packs || [];
  try {
    const r = await api('/api/hall/dojo/repro/list', {});
    if (gen === dojoState.gen) dojoState.repro = (r && r.packs) || [];
  } catch (e) {
    dojoState.repro = [];
  }
  try {
    const c = await api('/api/hall/dojo/case/list', {});
    if (gen === dojoState.gen) dojoState.cases = (c && c.cases) || [];
  } catch (e) {
    dojoState.cases = [];
  }
  try {
    const r = await api('/api/hall/dojo/research/list', {});
    if (gen === dojoState.gen) {
      dojoState.collections = (r && r.collections) || [];
      dojoState.inquiries = (r && r.inquiries) || [];
    }
  } catch (e) {
    dojoState.collections = [];
    dojoState.inquiries = [];
  }
  try {
    const p = await api('/api/hall/dojo/paper/index', {});
    if (gen === dojoState.gen) dojoState.paperIndex = (p && p.contests) || [];
  } catch (e) {
    dojoState.paperIndex = [];
  }
  // 道场只保留深度研究：进入即主题列表，不再自动开局单题诊断
  dojoState.el.card.classList.add('hidden');
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoRenderTop();
  dojoRenderResearchHome();
}

async function dojoStart(packId, unitId, gen) {
  const out = await api('/api/hall/dojo/start', { pack_id: packId, unit_id: unitId });
  if (gen != null && gen !== dojoState.gen) return;
  dojoState.session = out.session_id;
  dojoState.unit = out.unit;
  dojoState.pack = out.pack || dojoState.pack;
  dojoState.store.packName = dojoState.pack ? dojoState.pack.name : '';
  dojoState.store.packTotal = dojoState.pack ? dojoState.pack.unit_count : 0;
  dojoSaveStore();
  dojoState.index = out.index;
  dojoState.answered = false;
  dojoState.busy = false;
  dojoState.startedAt = Date.now();
  dojoState.el.reveal.classList.add('hidden');
  dojoState.el.reveal.innerHTML = '';
  dojoState.el.submit.disabled = false;
  dojoState.el.submit.classList.remove('hidden');
  dojoState.el.skip.classList.remove('hidden');
  dojoMsg('');
  dojoRenderTop();
  dojoRenderUnit();
  dojoRenderSide();
}

/* ---------------- 渲染：题面 ---------------- */

function dojoRenderTop() {
  const el = dojoState.el;
  // 道场只保留深度研究：场景/连对/今日一题/错题/导入/复习都属于已移交竞技场的诊断模式
  ['pack', 'streak', 'today', 'wrong', 'import', 'review'].forEach((k) => {
    if (el[k]) el[k].classList.add('hidden');
  });
  const rs = dojoState.research;
  const cs = dojoState.caseSession;
  if (dojoState.authoringView) {
    el.progress.textContent = '内容工坊';
    return;
  }
  if (dojoState.paperView) {
    el.progress.textContent = '真题索引';
    return;
  }
  if (dojoState.researchRecordsView) {
    el.progress.textContent = '研究记录';
    return;
  }
  if (el.researchExit) {
    el.researchExit.classList.toggle('hidden', !(rs && rs.stage !== 'done'));
  }
  if (cs) {
    el.progress.textContent = '案例 ' + Math.min(cs.index + 1, cs.total) + ' / ' + cs.total;
    return;
  }
  if (rs) {
    el.progress.textContent = rs.stage === 'acts'
      ? ('研究 ' + (rs.index + 1) + ' / ' + rs.total)
      : (rs.stage === 'done' ? '研究完成' : '研究结论');
    return;
  }
  el.progress.textContent = '研究任务';
}

function dojoRenderUnit() {
  const u = dojoState.unit;
  const el = dojoState.el;
  if (!u) return;
  const p = u.payload || {};
  el.ability.textContent = u.ability_name || DOJO_ABILITY_NAMES[u.ability] || u.ability || '训练';
  el.judge.textContent = u.judge === 'L1' ? 'L1 可验证'
    : u.judge === 'L2' ? 'L2 结构化判定' : 'L3 仅诊断 · 不计分';
  el.minutes.textContent = '约 ' + (u.minutes || 5) + ' 分钟';
  el.situation.textContent = p.situation || p.prompt || '';
  dojoRenderAnswer();
}

function dojoRenderAnswer() {
  const u = dojoState.unit;
  const p = u.payload || {};
  const box = dojoState.el.answer;
  box.innerHTML = '';
  dojoState.pick = null;
  dojoState.order = [];
  dojoState.pairs = {};

  const primitive = u.primitive;
  if (primitive === 'single_choice' || primitive === 'multi_select') {
    const multi = primitive === 'multi_select';
    const chosen = new Set();
    const wrap = document.createElement('div');
    wrap.className = 'dojo-options';
    (p.options || []).forEach((opt, i) => {
      const key = (opt && opt.key) || String.fromCharCode(65 + i);
      const text = (opt && opt.text) || opt || '';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dojo-opt';
      btn.dataset.key = key;
      btn.innerHTML = '<span class="dojo-opt-key">' + dojoEsc(key) + '</span><span>' + dojoEsc(text) + '</span>';
      btn.onclick = () => {
        if (dojoState.answered) return;
        if (multi) {
          if (chosen.has(key)) chosen.delete(key); else chosen.add(key);
          btn.classList.toggle('sel', chosen.has(key));
        } else {
          chosen.clear();
          chosen.add(key);
          wrap.querySelectorAll('.dojo-opt').forEach((b) => b.classList.toggle('sel', b.dataset.key === key));
        }
        dojoMsg('');
      };
      wrap.appendChild(btn);
    });
    box.appendChild(wrap);
    dojoState.pick = () => (multi ? Array.from(chosen) : (chosen.size ? Array.from(chosen)[0] : null));

  } else if (primitive === 'order') {
    const wrap = document.createElement('div');
    wrap.className = 'dojo-steps';
    const tip = document.createElement('div');
    tip.className = 'dojo-steps-tip';
    tip.textContent = '按你的判断依次点击，再点一次可取消。';
    (p.steps || []).forEach((step) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dojo-step';
      btn.dataset.id = step.id;
      btn.innerHTML = '<span class="dojo-step-no">·</span><span>' + dojoEsc(step.text) + '</span>';
      btn.onclick = () => {
        if (dojoState.answered) return;
        const i = dojoState.order.indexOf(step.id);
        if (i >= 0) dojoState.order.splice(i, 1); else dojoState.order.push(step.id);
        dojoRefreshOrder();
        dojoMsg('');
      };
      wrap.appendChild(btn);
    });
    box.appendChild(wrap);
    box.appendChild(tip);
    dojoState.pick = () => (dojoState.order.length === (p.steps || []).length
      ? dojoState.order.slice() : null);

  } else if (primitive === 'pair') {
    const wrap = document.createElement('div');
    wrap.className = 'dojo-pairs';
    const rights = p.right || [];
    (p.left || []).forEach((left) => {
      const row = document.createElement('div');
      row.className = 'dojo-pair-row';
      const l = document.createElement('div');
      l.className = 'dojo-pair-left';
      l.textContent = left.text;
      const arrow = document.createElement('div');
      arrow.className = 'dojo-pair-arrow';
      arrow.textContent = '→';
      const sel = document.createElement('select');
      sel.className = 'dojo-select';
      sel.dataset.left = left.id;
      sel.innerHTML = '<option value="">选择…</option>' + rights.map((r) =>
        '<option value="' + dojoEsc(r.id) + '">' + dojoEsc(r.text) + '</option>').join('');
      sel.onchange = () => {
        if (sel.value) dojoState.pairs[left.id] = sel.value;
        else delete dojoState.pairs[left.id];
        dojoMsg('');
      };
      row.appendChild(l);
      row.appendChild(arrow);
      row.appendChild(sel);
      wrap.appendChild(row);
    });
    box.appendChild(wrap);
    dojoState.pick = () => {
      const total = (p.left || []).length;
      if (Object.keys(dojoState.pairs).length < total) return null;
      return Object.assign({}, dojoState.pairs);
    };

  } else if (primitive === 'annotate') {
    const chosen = new Set();
    const wrap = document.createElement('div');
    wrap.className = 'dojo-options';
    (p.segments || []).forEach((seg) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dojo-opt';
      btn.dataset.id = seg.id;
      btn.innerHTML = '<span class="dojo-opt-key">□</span><span>' + dojoEsc(seg.text) + '</span>';
      btn.onclick = () => {
        if (dojoState.answered) return;
        if (chosen.has(seg.id)) chosen.delete(seg.id); else chosen.add(seg.id);
        btn.classList.toggle('sel', chosen.has(seg.id));
        btn.querySelector('.dojo-opt-key').textContent = chosen.has(seg.id) ? '■' : '□';
        dojoMsg('');
      };
      wrap.appendChild(btn);
    });
    box.appendChild(wrap);
    dojoState.pick = () => (chosen.size ? Array.from(chosen) : null);

  } else if (primitive === 'short_text') {
    const ta = document.createElement('textarea');
    ta.className = 'dojo-textarea';
    ta.id = 'dojo-text';
    ta.placeholder = p.placeholder || '用一句话写出你的理由……';
    ta.oninput = () => dojoMsg('');
    box.appendChild(ta);
    dojoState.pick = () => (ta.value.trim() ? ta.value.trim() : null);

  } else {
    const inp = document.createElement('input');
    inp.className = 'dojo-input';
    inp.id = primitive === 'expr' ? 'dojo-expr' : 'dojo-num';
    inp.type = 'text';
    inp.placeholder = p.placeholder || (primitive === 'expr' ? '例如 x^2 + 1' : '填一个数值');
    inp.oninput = () => dojoMsg('');
    box.appendChild(inp);
    dojoState.pick = () => (inp.value.trim() ? inp.value.trim() : null);
  }
}

function dojoRefreshOrder() {
  const box = dojoState.el.answer;
  if (!box) return;
  box.querySelectorAll('.dojo-step').forEach((btn) => {
    const i = dojoState.order.indexOf(btn.dataset.id);
    btn.classList.toggle('sel', i >= 0);
    btn.querySelector('.dojo-step-no').textContent = i >= 0 ? String(i + 1) : '·';
  });
}

/* ---------------- 提交与揭晓 ---------------- */

async function dojoSubmit() {
  if (!dojoState.unit || dojoState.answered || dojoState.busy) return;
  if (!dojoState.pick) {
    dojoMsg('先做一个判断再提交。', true);
    return;
  }
  const value = dojoState.pick();
  if (value == null) {
    dojoMsg('还有没填/没选完的部分。', true);
    return;
  }
  if (dojoState.caseSession) return dojoCaseSend(value);
  if (dojoState.mode === 'research' && dojoState.research) {
    const stage = dojoState.research.stage;
    if (stage === 'artifact') return dojoResearchArtifact(value);
    if (stage === 'revise') return dojoResearchRevise(value);
    return dojoResearchSend(value);
  }
  dojoState.busy = true;
  dojoState.el.submit.disabled = true;
  try {
    const out = await api('/api/hall/dojo/judge', {
      session_id: dojoState.session,
      unit_id: dojoState.unit.id,
      value: value,
      elapsed_ms: Date.now() - dojoState.startedAt,
    });
    dojoState.answered = true;
    dojoState.el.submit.classList.add('hidden');
    dojoApply(out);
    dojoReveal(out);
    dojoRenderTop();
    dojoRenderSide();
  } catch (e) {
    dojoState.el.submit.disabled = false;
    dojoMsg((e && e.message) || String(e), true);
  } finally {
    dojoState.busy = false;
  }
}

function dojoApply(out) {
  const store = dojoState.store;
  const unit = dojoState.unit;
  const now = Date.now();
  const packId = dojoState.pack ? dojoState.pack.id : '';

  store.answered[packId + ':' + unit.id] = {
    correct: !!out.correct, score: Number(out.score) || 0,
    judge: out.judge, at: now,
    title: (out.card && out.card.title) || '',
  };

  if (out.scored) {
    const key = out.ability || unit.ability || 'framing';
    const ab = store.abilities[key] || { seen: 0, score: 0 };
    ab.seen += 1;
    ab.score += Number(out.score) || 0;
    store.abilities[key] = ab;
    if (out.correct) {
      store.streak += 1;
      store.bestStreak = Math.max(store.bestStreak, store.streak);
    } else {
      store.streak = 0;
    }
  }

  store.history.unshift({
    pack: packId, unit: unit.id, correct: !!out.correct, score: Number(out.score) || 0,
    judge: out.judge, at: now,
  });
  store.history = store.history.slice(0, DOJO_HISTORY_LIMIT);

  if (out.card && out.card.title) dojoUpsertCard(out.card, out.correct, out.scored);
  dojoTrackWrong(packId, unit.id, out);

  const next = dojoState.index + 1;
  if (next > Number((store.progress || {})[packId] || 0)) dojoSetProgress(next);
  dojoSaveStore();
}

/* 错题队列：答错入队，答对（无论何时）出队；只用于“回去重做”，不做加权计算 */
function dojoTrackWrong(packId, unitId, out) {
  const store = dojoState.store;
  if (!store.wrong) store.wrong = [];
  const key = packId + ':' + unitId;
  const i = store.wrong.findIndex((w) => w.key === key);
  if (out.scored && !out.correct) {
    if (i < 0) {
      store.wrong.unshift({
        key: key, pack: packId, unit: unitId,
        title: (out.card && out.card.title) || '', at: Date.now(),
      });
    }
  } else if (out.correct && i >= 0) {
    store.wrong.splice(i, 1);
  }
}

function dojoWrongQueue() {
  return (dojoState.store && dojoState.store.wrong) || [];
}

function dojoUpsertCard(card, correct, scored) {
  const store = dojoState.store;
  const id = (dojoState.pack ? dojoState.pack.id : '') + ':' + card.title;
  const now = Date.now();
  let item = store.cards.find((c) => c.id === id);
  if (!item) {
    item = { id: id, pack: dojoState.pack ? dojoState.pack.id : '', ...card, stage: 0, seen: 0, next_at: now };
    store.cards.unshift(item);
  }
  item.seen = (Number(item.seen) || 0) + 1;
  item.title = card.title;
  item.trigger = card.trigger;
  item.first_step = card.first_step;
  item.pitfall = card.pitfall;
  // 答错（或不判分的题）从头复习；答对则往后推一档
  if (scored && correct) item.stage = Math.min((Number(item.stage) || 0) + 1, DOJO_REVIEW_DAYS.length - 1);
  else item.stage = 0;
  item.next_at = now + dojoReviewDays(item.stage) * DOJO_DAY_MS;
  store.cards = store.cards.slice(0, 60);
}

function dojoReveal(out) {
  const el = dojoState.el;
  const u = dojoState.unit;
  const box = el.reveal;
  const parts = [];
  const expert = out.expert || {};

  if (out.scored) {
    parts.push('<div class="dojo-verdict ' + (out.correct ? 'ok' : 'bad') + '">' +
      (out.correct ? '判断成立' : '判断不成立') +
      '<span class="dojo-verdict-note">掌握度 ' + Math.round((Number(out.score) || 0) * 100) + '%</span></div>');
  } else {
    parts.push('<div class="dojo-verdict diag">诊断（本题不计分）' +
      '<span class="dojo-verdict-note">L3 · 只给要点覆盖</span></div>');
  }

  parts.push(dojoDiagnosis(u, out));

  if (expert.why) {
    parts.push('<div class="dojo-block"><div class="dojo-block-title">专家怎么想</div>' +
      '<div class="dojo-block-body">' + dojoEsc(expert.why) + '</div></div>');
  }
  if (expert.signals && expert.signals.length) {
    parts.push('<div class="dojo-block"><div class="dojo-block-title">识别信号</div>' +
      '<ul class="dojo-signals">' + expert.signals.map((s) => '<li>' + dojoEsc(s) + '</li>').join('') +
      '</ul></div>');
  }

  // 只在你选错时指出「你踩的那个坑」，避免把没选的错误选项也塞满屏幕
  const trap = dojoChosenTrap(u, out);
  if (trap) {
    parts.push('<div class="dojo-trap">你选的那条：' + dojoEsc(trap) + '</div>');
  }
  if (expert.sample) {
    parts.push('<div class="dojo-block"><div class="dojo-block-title">参考答案</div>' +
      '<div class="dojo-block-body">' + dojoEsc(expert.sample) + '</div></div>');
  }
  if (out.card && out.card.title) {
    parts.push('<div class="dojo-card-note">' +
      '<div class="dojo-card-note-head">方法卡 · ' + dojoEsc(out.card.title) + '</div>' +
      '<dl><dt>触发</dt><dd>' + dojoEsc(out.card.trigger) + '</dd>' +
      '<dt>第一步</dt><dd>' + dojoEsc(out.card.first_step) + '</dd>' +
      '<dt>常见坑</dt><dd>' + dojoEsc(out.card.pitfall) + '</dd></dl></div>');
  }

  const nextLabel = dojoState.caseSession
    ? (out.finished ? '看案例报告' : '下一步')
    : (dojoState.mode === 'research' && dojoState.research
      ? (out.stage === 'artifact' ? '写研究结论' : '下一幕')
      : '下一题');
  const researchAsk = dojoState.mode === 'research' && dojoState.research;
  parts.push('<div class="dojo-actions">' +
    (researchAsk ? '<button type="button" class="dojo-btn" id="dojo-ask-solo">追问 Solo</button>' : '') +
    '<button type="button" class="dojo-btn primary" id="dojo-next">' + nextLabel + '</button></div>');

  box.innerHTML = parts.join('');
  box.classList.remove('hidden');
  const next = dojoQ('#dojo-next', box);
  if (next) next.onclick = () => dojoNext(false);
  const ask = dojoQ('#dojo-ask-solo', box);
  if (ask) ask.onclick = () => dojoAskSolo(dojoSoloActText());
}

function dojoDiagnosis(unit, out) {
  const d = out.detail || {};
  const primitive = unit.primitive;
  if (primitive === 'multi_select' && out.scored) {
    const bits = [];
    if ((d.hit || []).length) bits.push('命中 ' + d.hit.join('、'));
    if ((d.missed || []).length) bits.push('漏选 ' + d.missed.join('、'));
    if ((d.wrong || []).length) bits.push('错选 ' + d.wrong.join('、'));
    return bits.length ? '<div class="dojo-block"><div class="dojo-block-title">判定细节</div>' +
      '<div class="dojo-block-body">' + dojoEsc(bits.join('；')) + '</div></div>' : '';
  }
  if (primitive === 'order' && out.scored) {
    return '<div class="dojo-block"><div class="dojo-block-title">判定细节</div>' +
      '<div class="dojo-block-body">相对顺序一致 ' + (d.concordant || 0) + ' / ' + (d.pairs || 0) + ' 对</div></div>';
  }
  if (primitive === 'pair' && out.scored) {
    return '<div class="dojo-block"><div class="dojo-block-title">判定细节</div>' +
      '<div class="dojo-block-body">配对正确 ' + (d.hit || 0) + ' / ' + (d.total || 0) + '</div></div>';
  }
  if (primitive === 'annotate' && out.scored) {
    const bits = [];
    if ((d.hit || []).length) bits.push('标对 ' + d.hit.length);
    if ((d.missed || []).length) bits.push('漏标 ' + d.missed.length);
    if ((d.extra || []).length) bits.push('多标 ' + d.extra.length);
    return '<div class="dojo-block"><div class="dojo-block-title">判定细节</div>' +
      '<div class="dojo-block-body">' + dojoEsc(bits.join('；')) + '</div></div>';
  }
  if (primitive === 'short_text') {
    // rubric 只在提交后由服务端随 expert 一起返回，题面阶段拿不到
    const rubric = (out.expert && out.expert.rubric) || [];
    const byId = {};
    rubric.forEach((r) => { byId[r.id] = r; });
    const chips = [];
    (d.hits || []).forEach((id) => chips.push('<span class="dojo-hit ok">' +
      dojoEsc((byId[id] && byId[id].text) || id) + '</span>'));
    (d.missing || []).forEach((id) => chips.push('<span class="dojo-hit miss">缺：' +
      dojoEsc((byId[id] && byId[id].text) || id) + '</span>'));
    return '<div class="dojo-block"><div class="dojo-block-title">要点覆盖 ' +
      Math.round((Number(d.coverage) || 0) * 100) + '%</div>' +
      '<div class="dojo-hits">' + chips.join('') + '</div></div>';
  }
  return '';
}

function dojoChosenTrap(unit, out) {
  if (!out.scored || out.correct) return '';
  const expert = out.expert || {};
  const traps = expert.traps || {};
  const detail = out.detail || {};
  let key = detail.choice;
  if (!key && unit.primitive === 'multi_select') {
    const wrong = detail.wrong || [];
    if (wrong.length) key = wrong[0];
  }
  if (!key) return '';
  return traps[key] || '';
}

/* ---------------- 下一题 / 完成 ---------------- */

async function dojoNext(skipped) {
  if (dojoState.caseSession) return dojoCaseNext();
  if (dojoState.mode === 'research' && dojoState.research) return dojoResearchNext();
  const pack = dojoState.pack;
  if (!pack) return;
  const nextIndex = dojoState.index + 1;
  dojoState.el.reveal.classList.add('hidden');
  dojoState.el.submit.disabled = false;
  if (skipped) dojoMsg('已跳过，这题不计入统计。');

  if (nextIndex >= pack.unit_count) {
    dojoSetProgress(Math.min(nextIndex, pack.unit_count));
    dojoSaveStore();
    dojoShowDone();
    return;
  }
  const entry = pack.unit_index[nextIndex];
  try {
    await dojoStart(pack.id, entry && entry.id);
  } catch (e) {
    dojoMsg('下一题载入失败：' + ((e && e.message) || e), true);
  }
}

function dojoShowDone() {
  const pack = dojoState.pack;
  const el = dojoState.el;
  if (!pack) return;
  dojoState.answered = true;
  el.ability.textContent = '完成';
  el.judge.textContent = pack.unit_count + ' 个判断';
  el.minutes.textContent = '—';
  el.situation.textContent = '这一轮走完了。方法卡会在到期后出现在右栏；隔几天再回来，比重做一遍更有效。';
  el.answer.innerHTML = '';
  el.reveal.classList.add('hidden');
  el.submit.classList.add('hidden');
  el.skip.classList.add('hidden');
  dojoMsg('');

  const actions = document.createElement('div');
  actions.className = 'dojo-actions';
  const restart = document.createElement('button');
  restart.type = 'button';
  restart.className = 'dojo-btn primary';
  restart.textContent = '再过一轮';
  restart.onclick = () => {
    dojoState.store.pack = pack.id;
    dojoSetProgress(0);
    dojoSaveStore();
    dojoState.index = 0;
    dojoStart(pack.id, (pack.unit_index[0] || {}).id).catch((e) => toastErr('重新开始', e));
  };
  const review = document.createElement('button');
  review.type = 'button';
  review.className = 'dojo-btn';
  review.textContent = '看方法卡';
  review.onclick = () => {
    if (el.side) el.side.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
  const today = document.createElement('button');
  today.type = 'button';
  today.className = 'dojo-btn';
  today.textContent = '今日一题';
  today.onclick = () => dojoToday();
  actions.appendChild(restart);
  actions.appendChild(today);
  actions.appendChild(review);
  el.answer.appendChild(actions);

  dojoRenderTop();
  dojoRenderSide();
}

/* ---------------- 降低启动阻力：今日一题 / 错题 / 跳题 ---------------- */

function dojoUnanswered() {
  const out = [];
  (dojoState.packs || []).forEach((p) => {
    (p.unit_index || []).forEach((u) => {
      if (!dojoState.store.answered[p.id + ':' + u.id]) out.push({ pack: p, unit: u });
    });
  });
  return out;
}

function dojoAbilityRate(key) {
  const ab = dojoState.store.abilities[key];
  if (!ab || !ab.seen) return null;
  return ab.score / ab.seen;
}

function dojoWeakestAbility() {
  let best = null;
  Object.keys(DOJO_ABILITY_NAMES).forEach((key) => {
    const rate = dojoAbilityRate(key);
    if (rate == null) return;
    if (!best || rate < best.rate) best = { key: key, rate: rate };
  });
  return best ? best.key : 'framing';
}

async function dojoToday() {
  if (dojoState.review) dojoExitReview();
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.reproPack) dojoExitRepro();
  dojoCloseMap();
  const todo = dojoUnanswered();
  if (!todo.length) {
    toast('所有场景都练完了；可以看错题、复习方法卡，或跑一个建模案例');
    return;
  }
  // 先补最弱维度；该维度没有未做题时，退回当前场景，再退回任意题
  const weak = dojoWeakestAbility();
  const curId = dojoState.pack ? dojoState.pack.id : '';
  const pick = todo.find((x) => x.unit.ability === weak)
    || todo.find((x) => x.pack.id === curId)
    || todo[0];
  await dojoJumpTo(pick.pack.id, pick.unit.id);
}

async function dojoPracticeAbility(ability) {
  const cur = dojoState.pack;
  const inPack = cur && (cur.unit_index || []).find(
    (u) => u.ability === ability && !dojoState.store.answered[cur.id + ':' + u.id]);
  if (inPack) return dojoJumpTo(cur.id, inPack.id);
  const other = dojoUnanswered().find((x) => x.unit.ability === ability);
  if (other) return dojoJumpTo(other.pack.id, other.unit.id);
  toast('这个维度在当前题库里已经练完了');
}

function dojoOpenWrong() {
  const queue = dojoWrongQueue().slice().sort((a, b) => (Number(a.at) || 0) - (Number(b.at) || 0));
  if (!queue.length) {
    toast('还没有错题');
    return;
  }
  const first = queue[0];
  dojoJumpTo(first.pack, first.unit);
}

async function dojoJumpTo(packId, unitId) {
  const p = (dojoState.packs || []).find((x) => x.id === packId);
  if (!p) {
    toastErr('跳到这一题', new Error('这个场景已经不存在'));
    return;
  }
  if (dojoState.reproPack) dojoExitRepro();
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.review) dojoExitReview();
  dojoState.caseSession = null;
  dojoState.lastCaseOut = null;
  dojoCloseMap();
  dojoClosePackMenu();
  dojoState.store.pack = p.id;
  dojoState.pack = p;
  try {
    await dojoStart(packId, unitId);
    // 跳题（错题/知识地图）不能把「最远进度」往回拨，否则下次“继续”会倒退
    const curProg = Number((dojoState.store.progress || {})[packId] || 0);
    dojoSetProgress(Math.max(curProg, dojoState.index));
    dojoSaveStore();
    dojoRenderTop();
    dojoRenderSide();
  } catch (e) {
    toastErr('跳到这一题', e);
  }
}

/* ---------------- 知识地图 ---------------- */

function dojoOpenMap() {
  if (!dojoState.pack) return;
  if (dojoState.caseSession) {
    // 案例与单题使用不同的提交接口：不把案例状态留在后台，避免“返回后提交错会话”
    toast('案例进行中：先按 Esc 退出案例，再看知识地图');
    dojoClosePackMenu();
    return;
  }
  if (dojoState.review) dojoExitReview();
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.reproPack) dojoExitRepro();
  dojoClosePackMenu();
  dojoState.el.card.classList.add('hidden');
  dojoState.el.mapPanel.classList.remove('hidden');
  dojoRenderMap();
}

function dojoCloseMap() {
  const panel = dojoState.el.mapPanel;
  if (panel) {
    panel.classList.add('hidden');
    panel.innerHTML = '';
  }
  if (dojoState.el.card) dojoState.el.card.classList.remove('hidden');
}

function dojoRenderMap() {
  const panel = dojoState.el.mapPanel;
  const pack = dojoState.pack;
  const store = dojoState.store;
  if (!panel || !pack) return;

  let done = 0;
  let scoreSum = 0;
  const rows = (pack.unit_index || []).map((u, i) => {
    const rec = store.answered[pack.id + ':' + u.id];
    if (rec) {
      done += 1;
      scoreSum += Number(rec.score) || 0;
    }
    const state = !rec ? 'todo' : (rec.correct ? 'ok' : 'bad');
    const title = rec
      ? (rec.title || ('第 ' + (i + 1) + ' 题'))
      : ('第 ' + (i + 1) + ' 题 · 未练');
    const score = rec ? Math.round((Number(rec.score) || 0) * 100) + '%' : '—';
    return '<button type="button" class="dojo-map-row ' + state + '" data-unit="' + dojoEsc(u.id) + '">' +
      '<span class="dojo-map-no">' + (i + 1) + '</span>' +
      '<span class="dojo-map-title">' + dojoEsc(title) + '</span>' +
      '<span class="dojo-map-ability">' +
      dojoEsc(DOJO_ABILITY_NAMES[u.ability] || u.ability || '') + '</span>' +
      '<span class="dojo-map-score">' + score + '</span></button>';
  }).join('');

  const avg = done ? Math.round((scoreSum / done) * 100) : null;
  panel.innerHTML =
    '<div class="dojo-map-top">' +
    '<div><div class="dojo-map-head">知识地图 · ' + dojoEsc(pack.name) + '</div>' +
    '<div class="dojo-map-sub">已练 ' + done + ' / ' + pack.unit_count +
    (avg == null ? '' : ' · 平均掌握度 ' + avg + '%') + '</div></div>' +
    '<button type="button" class="dojo-btn dojo-map-exit" id="dojo-map-exit">返回</button></div>' +
    '<div class="dojo-map-list">' + (rows || '<div class="dojo-empty">这个场景还没有考点。</div>') + '</div>' +
    '<div class="dojo-map-note">点任意一行直接跳到那一道题；答过的题会显示方法卡标题与掌握度。</div>';

  const exit = dojoQ('#dojo-map-exit', panel);
  if (exit) exit.onclick = () => dojoCloseMap();
  panel.querySelectorAll('[data-unit]').forEach((btn) => {
    btn.onclick = () => dojoJumpTo(pack.id, btn.dataset.unit);
  });
}

/* ---------------- 完整建模案例 ---------------- */

function dojoCaseSourceText(cs) {
  const src = (cs && cs.source) || {};
  const bits = [];
  if (src.year) bits.push(src.year + ' 年');
  if (src.contest) bits.push(src.contest);
  if (src.problem) bits.push(src.problem + ' 题');
  const head = bits.length ? ('改编自 ' + bits.join(' ')) : '教学案例';
  return src.note ? (head + ' · ' + src.note) : head;
}

async function dojoOpenCase(caseId) {
  if (dojoState.review) dojoExitReview();
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.reproPack) dojoExitRepro();
  dojoCloseMap();
  dojoClosePackMenu();
  try {
    const out = await api('/api/hall/dojo/case/start', { id: caseId });
    if (!out.step) throw new Error('案例没有可用的步骤');
    dojoState.caseSession = {
      id: caseId,
      title: out.case.title,
      total: out.total,
      session: out.session_id,
      index: 0,
      source: out.case.source || {},
      brief: out.case.brief || '',
    };
    dojoState.lastCaseOut = null;
    dojoState.unit = out.step;
    dojoState.answered = false;
    dojoState.busy = false;
    dojoState.startedAt = Date.now();
    dojoState.el.reveal.classList.add('hidden');
    dojoState.el.reveal.innerHTML = '';
    dojoState.el.submit.disabled = false;
    dojoState.el.submit.classList.remove('hidden');
    dojoState.el.skip.classList.add('hidden');
    dojoMsg(dojoCaseSourceText(dojoState.caseSession) + '；按顺序走，不能跳步，答完六步再看断点。');
    dojoRenderTop();
    dojoRenderUnit();
    dojoRenderSide();
    const scroller = dojoState.el.root.closest('.hall-body') || document.getElementById('hall-body');
    if (scroller) scroller.scrollTop = 0;
  } catch (e) {
    toastErr('开始案例', e);
  }
}

async function dojoCaseSend(value) {
  const cs = dojoState.caseSession;
  if (!cs || dojoState.busy) return;
  dojoState.busy = true;
  dojoState.el.submit.disabled = true;
  try {
    const out = await api('/api/hall/dojo/case/answer', {
      session_id: cs.session,
      step_id: dojoState.unit.id,
      value: value,
      elapsed_ms: Date.now() - dojoState.startedAt,
    });
    dojoState.answered = true;
    dojoState.lastCaseOut = out;
    dojoState.el.submit.classList.add('hidden');
    dojoReveal(out);
    dojoRenderTop();
  } catch (e) {
    dojoState.el.submit.disabled = false;
    dojoMsg((e && e.message) || String(e), true);
  } finally {
    dojoState.busy = false;
  }
}

async function dojoCaseNext() {
  const cs = dojoState.caseSession;
  const out = dojoState.lastCaseOut;
  if (!cs || !out) return;
  if (out.next_step) {
    cs.index = Number(out.step_index || 0) + 1;
    dojoState.unit = out.next_step;
    dojoState.answered = false;
    dojoState.startedAt = Date.now();
    dojoState.el.reveal.classList.add('hidden');
    dojoState.el.reveal.innerHTML = '';
    dojoState.el.submit.disabled = false;
    dojoState.el.submit.classList.remove('hidden');
    dojoMsg('第 ' + (cs.index + 1) + ' 步 / 共 ' + cs.total + ' 步');
    dojoRenderTop();
    dojoRenderUnit();
    return;
  }
  try {
    const rep = await api('/api/hall/dojo/case/finish', { session_id: cs.session });
    dojoRenderCaseReport(rep);
  } catch (e) {
    dojoMsg((e && e.message) || String(e), true);
  }
}

function dojoRenderCaseReport(rep) {
  const el = dojoState.el;
  const caseId = dojoState.caseSession && dojoState.caseSession.id;
  dojoState.answered = true;
  el.ability.textContent = '案例报告';
  el.judge.textContent = rep.total + ' 步';
  el.minutes.textContent = '—';
  el.situation.textContent = rep.verdict;
  el.answer.innerHTML = '';
  el.submit.classList.add('hidden');
  el.skip.classList.add('hidden');
  dojoMsg('');

  const rows = (rep.steps || []).map((s) => {
    const score = Math.round((Number(s.score) || 0) * 100);
    return '<tr class="' + (score >= 60 ? 'ok' : 'bad') + '">' +
      '<td>' + (s.index + 1) + '</td>' +
      '<td>' + dojoEsc(s.ability_name) + '</td>' +
      '<td>' + score + '%</td></tr>';
  }).join('');
  const abilities = (rep.by_ability || []).map((a) =>
    '<div class="dojo-report-ability"><span>' + dojoEsc(a.ability_name) + '</span>' +
    '<b>' + Math.round((Number(a.score) || 0) * 100) + '%</b></div>').join('');

  el.reveal.innerHTML =
    '<div class="dojo-report">' +
    '<div class="dojo-report-verdict ' + (rep.first_break ? 'bad' : 'ok') + '">' +
    dojoEsc(rep.verdict) +
    '<span class="dojo-verdict-note">平均掌握度 ' +
    Math.round((Number(rep.avg_score) || 0) * 100) + '%</span></div>' +
    '<table class="dojo-assess-table"><thead><tr><th>步</th><th>能力</th><th>得分</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>' +
    '<div class="dojo-report-abilities">' + abilities + '</div>' +
    '<div class="dojo-report-hint">' + dojoEsc(rep.hint || '') + '</div>' +
    '<div class="dojo-report-note">这只反映这 ' + rep.total +
    ' 个判断，不代表完整论文水平；案例得分不计入单题能力画像。</div>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn primary" id="dojo-case-again">再走一遍</button>' +
    '<button type="button" class="dojo-btn" id="dojo-case-exit">回到研究列表</button>' +
    '</div></div>';
  el.reveal.classList.remove('hidden');

  const again = dojoQ('#dojo-case-again', el.reveal);
  if (again) again.onclick = () => dojoOpenCase(caseId);
  const exit = dojoQ('#dojo-case-exit', el.reveal);
  if (exit) exit.onclick = () => dojoExitCase();
  dojoRenderTop();
}

async function dojoExitCase() {
  dojoState.caseSession = null;
  dojoState.lastCaseOut = null;
  try {
    await dojoBoot(dojoState.gen);
  } catch (e) {
    toastErr('回到研究列表', e);
  }
}

/* ---------------- 深度研究 ---------------- */

function dojoSavedResearch() {
  try {
    const raw = JSON.parse(localStorage.getItem(DOJO_RESEARCH_KEY) || 'null');
    return raw && typeof raw === 'object' && raw.sessionId ? raw : null;
  } catch (e) {
    return null;
  }
}

function dojoSaveResearchSession() {
  const r = dojoState.research;
  if (!r || !r.inquiry) return;
  try {
    localStorage.setItem(DOJO_RESEARCH_KEY, JSON.stringify({
      sessionId: r.sessionId,
      inquiryId: r.inquiry.id,
      title: r.inquiry.title,
      subject: r.inquiry.subject,
      index: r.index,
      total: r.total,
      stage: r.stage,
      savedAt: Date.now(),
    }));
  } catch (e) { /* 配额满 / 隐私模式 */ }
}

function dojoClearResearchSession() {
  try { localStorage.removeItem(DOJO_RESEARCH_KEY); } catch (e) { /* ignore */ }
}

function dojoResearchDone() {
  try {
    const raw = JSON.parse(localStorage.getItem(DOJO_RESEARCH_DONE_KEY) || 'null');
    return raw && typeof raw === 'object' ? raw : {};
  } catch (e) {
    return {};
  }
}

function dojoSaveResearchDone(report) {
  const map = dojoResearchDone();
  map[report.inquiry_id] = {
    at: Date.now(),
    verdict: report.verdict || '',
    coverage: report.artifact ? report.artifact.coverage : 0,
  };
  try { localStorage.setItem(DOJO_RESEARCH_DONE_KEY, JSON.stringify(map)); } catch (e) { /* ignore */ }
}

function dojoResearchFilterLoad() {
  try {
    const raw = JSON.parse(localStorage.getItem(DOJO_RESEARCH_FILTER_KEY) || 'null');
    if (!raw || typeof raw !== 'object') throw new Error('empty');
    return {
      subject: String(raw.subject || 'all'),
      status: String(raw.status || 'all'),
      q: String(raw.q || ''),
    };
  } catch (e) {
    return { subject: 'all', status: 'all', q: '' };
  }
}

function dojoResearchFilterSave() {
  try {
    localStorage.setItem(DOJO_RESEARCH_FILTER_KEY, JSON.stringify(dojoState.researchFilter));
  } catch (e) { /* 隐私模式 / 配额满 */ }
}

function dojoResearchStateOf(q, saved, done) {
  if (done && done[q.id]) return 'done';
  if (saved && saved.inquiryId === q.id) return 'doing';
  return 'todo';
}

function dojoResearchMatch(q, saved, done) {
  const f = dojoState.researchFilter || {};
  if (f.subject && f.subject !== 'all' && q.subject !== f.subject) return false;
  if (f.status && f.status !== 'all' && dojoResearchStateOf(q, saved, done) !== f.status) return false;
  const kw = String(f.q || '').trim().toLowerCase();
  if (kw) {
    const blob = [q.title, q.subject, q.brief, (q.exam && q.exam.topic) || '',
      (q.prerequisites || []).join(' ')].join(' ').toLowerCase();
    if (blob.indexOf(kw) < 0) return false;
  }
  return true;
}

function dojoResearchSubjects(inquiries) {
  const out = [];
  (inquiries || []).forEach((q) => {
    const s = String(q.subject || '').trim();
    if (s && out.indexOf(s) < 0) out.push(s);
  });
  return out;
}

function dojoResearchFilterHtml(inquiries) {
  const f = dojoState.researchFilter || { subject: 'all', status: 'all', q: '' };
  const chip = (attr, val, text, active) =>
    '<button type="button" class="dojo-filter-chip' + (active ? ' active' : '') +
    '" data-' + attr + '="' + dojoEsc(val) + '">' + dojoEsc(text) + '</button>';
  const subjects = dojoResearchSubjects(inquiries);
  return '<div class="dojo-filter">' +
    '<div class="dojo-filter-row">' +
    chip('fsubject', 'all', '全部学科', f.subject === 'all') +
    subjects.map((s) => chip('fsubject', s, s, f.subject === s)).join('') +
    '</div>' +
    '<div class="dojo-filter-row">' +
    chip('fstatus', 'all', '全部状态', f.status === 'all') +
    chip('fstatus', 'todo', '未开始', f.status === 'todo') +
    chip('fstatus', 'doing', '进行中', f.status === 'doing') +
    chip('fstatus', 'done', '已完成', f.status === 'done') +
    '<input type="search" class="dojo-filter-search" id="dojo-inquiry-search"' +
    ' placeholder="搜索主题 / 知识点 / 考试方向" value="' + dojoEsc(f.q) + '">' +
    '</div>' +
    '<div class="dojo-filter-count" id="dojo-filter-count"></div>' +
    '</div>';
}

function dojoResearchCard(q, done, saved) {
  const isDone = done && done[q.id];
  const doing = !isDone && saved && saved.inquiryId === q.id;
  return '<button type="button" class="dojo-inquiry" data-inquiry="' + dojoEsc(q.id) + '">' +
    '<span class="dojo-inquiry-top">' +
    '<span class="dojo-inquiry-subject">' + dojoEsc(q.subject) +
    (q.track ? (' · ' + dojoEsc(q.track)) : '') + '</span>' +
    '<span class="dojo-inquiry-min">约 ' + q.minutes + ' 分钟</span></span>' +
    '<span class="dojo-inquiry-title">' + dojoEsc(q.title) + '</span>' +
    '<span class="dojo-inquiry-meta">' + q.total + ' 幕 · 考试对接：' +
    dojoEsc((q.exam && q.exam.topic) || '—') + '</span>' +
    '<span class="dojo-inquiry-state' + (isDone ? ' done' : '') + '">' +
    (isDone ? ('已完成 · ' + dojoEsc(isDone.verdict || ''))
      : (doing ? '进行中' : '未开始')) + '</span>' +
    '</button>';
}

function dojoRenderInquiryList(panel) {
  const box = dojoQ('#dojo-inquiry-list', panel);
  if (!box) return;
  const all = dojoState.inquiries || [];
  const saved = dojoSavedResearch();
  const done = dojoResearchDone();
  const list = all.filter((q) => dojoResearchMatch(q, saved, done));
  box.innerHTML = list.length
    ? list.map((q) => dojoResearchCard(q, done, saved)).join('')
    : '<div class="dojo-empty">没有匹配的研究主题。换个关键词，或点上面的「全部学科 / 全部状态」。</div>';
  box.querySelectorAll('[data-inquiry]').forEach((btn) => {
    btn.onclick = () => dojoStartResearch(btn.dataset.inquiry);
  });
  const count = dojoQ('#dojo-filter-count', panel);
  if (count) count.textContent = '显示 ' + list.length + ' / ' + all.length + ' 个主题';
}

function dojoWireResearchFilter(panel) {
  const refresh = () => {
    const f = dojoState.researchFilter;
    panel.querySelectorAll('[data-fsubject]').forEach((b) => {
      b.classList.toggle('active', b.dataset.fsubject === f.subject);
    });
    panel.querySelectorAll('[data-fstatus]').forEach((b) => {
      b.classList.toggle('active', b.dataset.fstatus === f.status);
    });
    dojoRenderInquiryList(panel);
  };
  panel.querySelectorAll('[data-fsubject]').forEach((btn) => {
    btn.onclick = () => {
      dojoState.researchFilter.subject = btn.dataset.fsubject;
      dojoResearchFilterSave();
      refresh();
    };
  });
  panel.querySelectorAll('[data-fstatus]').forEach((btn) => {
    btn.onclick = () => {
      dojoState.researchFilter.status = btn.dataset.fstatus;
      dojoResearchFilterSave();
      refresh();
    };
  });
  const search = dojoQ('#dojo-inquiry-search', panel);
  if (search) {
    search.oninput = () => {
      dojoState.researchFilter.q = search.value;
      dojoResearchFilterSave();
      dojoRenderInquiryList(panel);
    };
  }
}

function dojoRenderResearchHome() {
  const panel = dojoState.el.researchPanel;
  if (!panel) return;
  // 回到主题列表就不再持有活动会话；断点信息从 localStorage + /state 恢复
  dojoState.research = null;
  dojoState.researchLastOut = null;
  dojoState.researchRecordsView = false;
  dojoState.authoringView = false;
  dojoState.paperView = false;
  const col = (dojoState.collections || [])[0] || {};
  const saved = dojoSavedResearch();
  const inquiries = dojoState.inquiries || [];

  const resume = saved
    ? '<button type="button" class="dojo-resume" id="dojo-resume-research">' +
      '<span class="dojo-resume-tag">继续上次</span>' +
      '<b>' + dojoEsc(saved.title || saved.inquiryId) + '</b>' +
      '<span>' + dojoEsc(saved.subject || '') + ' · 第 ' +
      Math.min(Number(saved.index || 0) + 1, Number(saved.total || 0) || 1) + ' / ' +
      (saved.total || '?') + ' 幕</span></button>'
    : '';

  const cases = dojoState.cases || [];
  const caseHtml = cases.length
    ? '<div class="dojo-case-block"><div class="dojo-pack-section">完整建模 · CUMCM 改编</div>' +
      cases.map((c) =>
        '<button type="button" class="dojo-case-item" data-case="' + dojoEsc(c.id) + '">' +
        '<span class="dojo-case-name">' + dojoEsc(c.title) + '</span>' +
        '<span class="dojo-case-meta">' + c.total + ' 步 · 约 ' + c.minutes + ' 分钟</span></button>').join('') +
      '</div>'
    : '';

  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">深度研究</div>' +
    '<div class="dojo-research-sub">一个任务只研究一个问题：先看它怎么来的，再构造反例、重建证明，最后落到考试。</div></div>' +
    '<div class="dojo-research-head-right">' +
    '<button type="button" class="dojo-btn dojo-research-records" id="dojo-paper-index">真题索引</button>' +
    '<button type="button" class="dojo-btn dojo-research-authoring" id="dojo-research-authoring">内容工坊</button>' +
    '<button type="button" class="dojo-btn dojo-research-records" id="dojo-research-records">研究记录</button>' +
    '<div class="dojo-research-count">' + inquiries.length + ' 个主题</div></div></div>' +
    (col.id
      ? '<div class="dojo-research-col"><b>' + dojoEsc(col.name) + '</b>' +
        '<span>' + dojoEsc(col.note || col.standard || '') + '</span></div>'
      : '') +
    resume +
    dojoResearchFilterHtml(inquiries) +
    '<div class="dojo-inquiry-list" id="dojo-inquiry-list"></div>' +
    caseHtml +
    '<div class="dojo-research-note">研究结论只做论证要素诊断，不计分、也不冒充证明检查；判断幕照常计分。</div>';

  const resumeBtn = dojoQ('#dojo-resume-research', panel);
  if (resumeBtn) resumeBtn.onclick = () => dojoResumeResearch();
  const recordsBtn = dojoQ('#dojo-research-records', panel);
  if (recordsBtn) recordsBtn.onclick = () => dojoOpenResearchRecords();
  const authoringBtn = dojoQ('#dojo-research-authoring', panel);
  if (authoringBtn) authoringBtn.onclick = () => dojoOpenAuthoring();
  const papersBtn = dojoQ('#dojo-paper-index', panel);
  if (papersBtn) papersBtn.onclick = () => dojoRenderPaperIndex();
  dojoWireResearchFilter(panel);
  dojoRenderInquiryList(panel);
  panel.querySelectorAll('[data-case]').forEach((btn) => {
    btn.onclick = () => dojoOpenCase(btn.dataset.case);
  });
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoState.el.card.classList.add('hidden');
  dojoRenderTop();
  dojoRenderSide();
}

function dojoPaperContest() {
  const list = dojoState.paperIndex || [];
  return list.find((c) => c.id === dojoState.paperContest) || list[0] || null;
}

function dojoRenderPaperIndex() {
  const panel = dojoState.el.researchPanel;
  if (!panel) return;
  dojoState.paperView = true;
  dojoState.research = null;
  const list = dojoState.paperIndex || [];
  if (!list.length) {
    panel.innerHTML = '<div class="dojo-empty">真题索引还没载入，回大厅再进来一次。</div>';
    dojoRenderTop();
    return;
  }
  const cur = dojoPaperContest();
  const year = Number(dojoState.paperYear || 0);
  const papers = (cur.papers || []).filter((p) => !year || p.year === year);

  const contestChip = (c) =>
    '<button type="button" class="dojo-filter-chip' + (c.id === cur.id ? ' active' : '') +
    '" data-paper-contest="' + dojoEsc(c.id) + '">' + dojoEsc(c.name) +
    '<i>' + c.adapted + '/' + c.total + '</i></button>';
  const yearChip = (y, text) =>
    '<button type="button" class="dojo-filter-chip' + (Number(y) === year ? ' active' : '') +
    '" data-paper-year="' + y + '">' + dojoEsc(text) + '</button>';

  const body = papers.map((p) => {
    const head = p.edition ? ('第 ' + p.edition + ' 届 · ' + p.paper) : p.paper;
    const rows = (p.problems || []).map((q) => {
      const noText = typeof q.no === 'number' ? ('第 ' + q.no + ' 题') : (q.no + ' 题');
      const meta = [q.points ? (q.points + ' 分') : '', q.subject || ''].filter(Boolean).join(' · ');
      const label = q.title || q.topic || '';
      const right = q.item
        ? '<button type="button" class="dojo-paper-go" data-paper-item="' + dojoEsc(q.item) + '">已改编 ›</button>'
        : '<span class="dojo-paper-todo">待改编</span>';
      return '<div class="dojo-paper-row">' +
        '<span class="dojo-paper-no">' + dojoEsc(noText) + '</span>' +
        '<span class="dojo-paper-main"><b>' + dojoEsc(label) + '</b>' +
        (q.head && q.head !== label ? '<em>' + dojoEsc(q.head) + '</em>' : '') + '</span>' +
        '<span class="dojo-paper-meta">' + dojoEsc(meta) + '</span>' + right + '</div>';
    }).join('');
    return '<div class="dojo-paper-block"><div class="dojo-paper-head"><b>' +
      p.year + ' 年 · ' + dojoEsc(head) + '</b>' +
      (p.url ? '<a href="' + dojoEsc(p.url) + '" target="_blank" rel="noreferrer">官方题目 ↗</a>' : '') +
      (p.note ? '<span>' + dojoEsc(p.note) + '</span>' : '') + '</div>' + rows + '</div>';
  }).join('');

  panel.innerHTML =
    '<div class="dojo-research-head"><div><div class="dojo-research-title">真题索引</div>' +
    '<div class="dojo-research-sub">' + dojoEsc(cur.standard || '') + '</div></div>' +
    '<div class="dojo-research-head-right">' +
    '<button type="button" class="dojo-btn" id="dojo-paper-back">返回主题</button>' +
    '<div class="dojo-research-count">' + cur.adapted + ' / ' + cur.total + ' 已改编</div></div></div>' +
    '<div class="dojo-filter"><div class="dojo-filter-row">' + list.map(contestChip).join('') + '</div>' +
    '<div class="dojo-filter-row">' + yearChip(0, '全部年份') +
    (cur.years || []).map((y) => yearChip(y, y + ' 年')).join('') + '</div></div>' +
    (cur.note ? '<div class="dojo-paper-note">' + dojoEsc(cur.note) + '</div>' : '') +
    '<div class="dojo-paper-list">' +
    (body || '<div class="dojo-empty">这一年没有登记题目。</div>') + '</div>' +
    '<div class="dojo-research-note">索引只登记年份、卷别、题号与考点；题面与分值以官方 PDF 为准。点「已改编」进入本站的 6 幕研究。</div>';

  const back = dojoQ('#dojo-paper-back', panel);
  if (back) back.onclick = () => dojoRenderResearchHome();
  panel.querySelectorAll('[data-paper-contest]').forEach((b) => {
    b.onclick = () => {
      dojoState.paperContest = b.dataset.paperContest;
      dojoState.paperYear = 0;
      dojoRenderPaperIndex();
    };
  });
  panel.querySelectorAll('[data-paper-year]').forEach((b) => {
    b.onclick = () => {
      dojoState.paperYear = Number(b.dataset.paperYear) || 0;
      dojoRenderPaperIndex();
    };
  });
  panel.querySelectorAll('[data-paper-item]').forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.paperItem;
      if (id.indexOf('cumcm-') === 0) dojoOpenCase(id);
      else dojoStartResearch(id);
    };
  });
  panel.classList.remove('hidden');
  dojoState.el.card.classList.add('hidden');
  dojoRenderTop();
  dojoRenderSide();
}

async function dojoStartResearch(inquiryId) {
  try {
    const out = await api('/api/hall/dojo/research/start', { id: inquiryId });
    dojoState.research = {
      sessionId: out.session_id,
      inquiry: out.inquiry,
      stage: 'acts',
      index: out.index,
      total: out.total,
      act: out.act,
      answered: [],
      artifact: null,
      revisions: 0,
    };
    dojoState.researchLastOut = null;
    dojoSaveResearchSession();
    dojoRenderResearchAct();
  } catch (e) {
    toastErr('开始研究', e);
  }
}

async function dojoResumeResearch() {
  const saved = dojoSavedResearch();
  if (!saved) {
    dojoRenderResearchHome();
    return;
  }
  try {
    const out = await api('/api/hall/dojo/research/state', { session_id: saved.sessionId });
    dojoState.research = {
      sessionId: saved.sessionId,
      inquiry: out.inquiry,
      stage: out.finished ? 'done' : out.stage,
      index: out.index,
      total: out.total,
      act: out.act,
      answered: out.answered || [],
      artifact: out.artifact,
      revisions: out.revisions || 0,
    };
    if (out.finished) {
      dojoResearchFinish();
      return;
    }
    if (out.stage === 'acts') dojoRenderResearchAct();
    else if (out.artifact) dojoRenderResearchDiagnosis();
    else dojoRenderResearchArtifact();
  } catch (e) {
    dojoClearResearchSession();
    toast('上次的研究会话已过期，重新开始一个主题');
    dojoRenderResearchHome();
  }
}

function dojoScrollTop() {
  const scroller = dojoState.el.root.closest('.hall-body') || document.getElementById('hall-body');
  if (scroller) scroller.scrollTop = 0;
}

function dojoRenderResearchAct() {
  const r = dojoState.research;
  if (!r || !r.act) return;
  const el = dojoState.el;
  el.researchPanel.classList.add('hidden');
  el.card.classList.remove('hidden');
  dojoState.unit = r.act;
  dojoState.answered = false;
  dojoState.busy = false;
  dojoState.startedAt = Date.now();
  el.reveal.classList.add('hidden');
  el.reveal.innerHTML = '';
  el.submit.textContent = '提交判断';
  el.submit.disabled = false;
  el.submit.classList.remove('hidden');
  el.skip.classList.add('hidden');
  dojoState.researchLastValue = null;
  dojoRenderUnit();
  el.ability.textContent = r.act.label || '研究';
  el.judge.textContent = (r.act.judge === 'L1' ? 'L1 可验证' : 'L2 结构化判定') +
    ' · ' + (r.act.ability_name || '');
  el.minutes.textContent = '约 ' + (r.act.minutes || 5) + ' 分钟';
  dojoMsg('第 ' + (r.index + 1) + ' 幕 / 共 ' + r.total + ' 幕');
  dojoRenderTop();
  dojoRenderSide();
  dojoScrollTop();
}

async function dojoResearchSend(value) {
  const r = dojoState.research;
  if (!r || r.stage !== 'acts' || dojoState.busy) return;
  dojoState.busy = true;
  dojoState.el.submit.disabled = true;
  try {
    const out = await api('/api/hall/dojo/research/answer', {
      session_id: r.sessionId,
      act_id: dojoState.unit.id,
      value: value,
      elapsed_ms: Date.now() - dojoState.startedAt,
    });
    dojoState.answered = true;
    dojoState.researchLastOut = out;
    dojoState.researchLastValue = value;
    dojoState.el.submit.classList.add('hidden');
    dojoReveal(out);
    dojoRenderTop();
    dojoSaveResearchSession();
  } catch (e) {
    dojoState.el.submit.disabled = false;
    dojoMsg((e && e.message) || String(e), true);
  } finally {
    dojoState.busy = false;
  }
}

function dojoResearchNext() {
  const r = dojoState.research;
  const out = dojoState.researchLastOut;
  if (!r || !out) return;
  if (out.next_act) {
    r.index = Number(out.index || 0) + 1;
    r.act = out.next_act;
    r.stage = 'acts';
    dojoSaveResearchSession();
    dojoRenderResearchAct();
    return;
  }
  r.index = r.total;
  r.stage = 'artifact';
  dojoSaveResearchSession();
  dojoRenderResearchArtifact();
}

function dojoRenderResearchArtifact() {
  const r = dojoState.research;
  if (!r) return;
  const el = dojoState.el;
  r.stage = 'artifact';
  dojoSaveResearchSession();
  el.researchPanel.classList.add('hidden');
  el.card.classList.remove('hidden');
  dojoState.unit = {
    id: 'research-artifact',
    primitive: 'short_text',
    judge: 'L3',
    ability: 'argument',
    ability_name: '研究结论',
    minutes: 8,
    payload: {
      situation: (r.inquiry.artifact && r.inquiry.artifact.prompt) +
        '\n\n（不计分：只检查论证要素是否出现；至少 40 字）',
      placeholder: '写 150–300 字：反例、条件、结论',
    },
  };
  dojoState.answered = false;
  dojoState.busy = false;
  dojoState.startedAt = Date.now();
  el.reveal.classList.add('hidden');
  el.reveal.innerHTML = '';
  el.submit.textContent = '提交研究结论';
  el.submit.disabled = false;
  el.submit.classList.remove('hidden');
  el.skip.classList.add('hidden');
  dojoRenderUnit();
  el.ability.textContent = '研究结论';
  el.judge.textContent = 'L3 · 仅诊断';
  el.minutes.textContent = '约 8 分钟';
  dojoMsg('提交后可以对照专家范例修改一次；结论不计分。');
  dojoRenderTop();
  dojoRenderSide();
  dojoScrollTop();
}

async function dojoResearchArtifact(value) {
  const r = dojoState.research;
  if (!r) return;
  const text = String(value == null ? '' : value).trim();
  if (text.length < 40) {
    dojoMsg('至少写 40 字：反例、条件、结论各说一句。', true);
    return;
  }
  dojoState.busy = true;
  dojoState.el.submit.disabled = true;
  try {
    const out = await api('/api/hall/dojo/research/artifact', {
      session_id: r.sessionId, text: text,
    });
    r.artifact = out;
    r.stage = 'diagnosis';
    dojoSaveResearchSession();
    dojoRenderResearchDiagnosis();
  } catch (e) {
    dojoState.el.submit.disabled = false;
    dojoMsg((e && e.message) || String(e), true);
  } finally {
    dojoState.busy = false;
  }
}

function dojoRenderResearchDiagnosis() {
  const r = dojoState.research;
  const el = dojoState.el;
  if (!r || !r.artifact) return;
  const art = r.artifact;
  el.researchPanel.classList.add('hidden');
  el.card.classList.remove('hidden');
  el.ability.textContent = '研究结论';
  el.judge.textContent = 'L3 · 仅诊断';
  el.minutes.textContent = '—';
  el.situation.textContent = '论证要素覆盖 ' + Math.round((Number(art.coverage) || 0) * 100) + '%';
  el.answer.innerHTML = '';
  el.submit.classList.add('hidden');
  el.skip.classList.add('hidden');
  dojoMsg('');

  const hits = (art.hits || []).map((x) =>
    '<span class="dojo-hit ok">' + dojoEsc(x.text) + '</span>').join('');
  const miss = (art.missing || []).map((x) =>
    '<span class="dojo-hit miss">缺：' + dojoEsc(x.text) + '</span>').join('');
  const canRevise = Number(art.revisions_left || 0) > 0;

  el.reveal.innerHTML =
    '<div class="dojo-report">' +
    '<div class="dojo-report-verdict ' + (art.coverage >= 0.6 ? 'ok' : 'bad') + '">' +
    '论证要素覆盖 ' + Math.round((Number(art.coverage) || 0) * 100) + '%' +
    '<span class="dojo-verdict-note">不计分</span></div>' +
    '<div class="dojo-hits">' + (hits + miss || '<span class="dojo-empty">没有可诊断的要素。</span>') + '</div>' +
    (art.expert_sample
      ? '<div class="dojo-block"><div class="dojo-block-title">专家示范</div>' +
        '<div class="dojo-block-body">' + dojoEsc(art.expert_sample) + '</div></div>'
      : '') +
    '<div class="dojo-report-note">只检查论证要素是否出现，不判断证明是否正确；这也是它不计分的原因。</div>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn" id="dojo-research-ask">追问 Solo</button>' +
    (canRevise ? '<button type="button" class="dojo-btn" id="dojo-research-revise">改一次</button>' : '') +
    '<button type="button" class="dojo-btn primary" id="dojo-research-finish">完成研究</button>' +
    '</div></div>';
  el.reveal.classList.remove('hidden');

  const ask = dojoQ('#dojo-research-ask', el.reveal);
  if (ask) ask.onclick = () => dojoAskSolo(dojoSoloArtifactText());
  const rev = dojoQ('#dojo-research-revise', el.reveal);
  if (rev) rev.onclick = () => dojoResearchReviseStart();
  const fin = dojoQ('#dojo-research-finish', el.reveal);
  if (fin) fin.onclick = () => dojoResearchFinish();
  r.stage = 'diagnosis';
  dojoRenderTop();
  dojoRenderSide();
}

function dojoResearchReviseStart() {
  const r = dojoState.research;
  if (!r || !r.artifact) return;
  const el = dojoState.el;
  r.stage = 'revise';
  dojoSaveResearchSession();
  el.reveal.classList.add('hidden');
  el.reveal.innerHTML = '';
  dojoState.unit = {
    id: 'research-revise',
    primitive: 'short_text',
    judge: 'L3',
    ability: 'argument',
    ability_name: '研究结论',
    minutes: 5,
    payload: {
      situation: '修改你的研究结论（只能改一次）。补上缺失的论证要素，不要改判断幕的分数。',
      placeholder: '在原文基础上补反例、条件或结论',
    },
  };
  dojoState.answered = false;
  dojoState.busy = false;
  el.submit.textContent = '提交修改';
  el.submit.disabled = false;
  el.submit.classList.remove('hidden');
  el.skip.classList.add('hidden');
  dojoRenderUnit();
  el.ability.textContent = '研究结论 · 修订';
  el.judge.textContent = 'L3 · 仅诊断';
  el.minutes.textContent = '约 5 分钟';
  const ta = dojoQ('#dojo-text');
  if (ta) ta.value = r.artifact.text || '';
  dojoMsg('这次修改不会影响已答幕的分数。');
  dojoRenderTop();
}

async function dojoResearchRevise(value) {
  const r = dojoState.research;
  if (!r) return;
  const text = String(value == null ? '' : value).trim();
  if (text.length < 40) {
    dojoMsg('至少写 40 字再提交修改。', true);
    return;
  }
  dojoState.busy = true;
  dojoState.el.submit.disabled = true;
  try {
    const out = await api('/api/hall/dojo/research/revise', {
      session_id: r.sessionId, text: text,
    });
    r.artifact = out;
    r.stage = 'diagnosis';
    dojoSaveResearchSession();
    dojoRenderResearchDiagnosis();
  } catch (e) {
    dojoState.el.submit.disabled = false;
    dojoMsg((e && e.message) || String(e), true);
  } finally {
    dojoState.busy = false;
  }
}

async function dojoResearchFinish() {
  const r = dojoState.research;
  if (!r) return;
  try {
    const report = await api('/api/hall/dojo/research/finish', { session_id: r.sessionId });
    r.report = report;
    r.stage = 'done';
    dojoSaveResearchDone(report);
    dojoClearResearchSession();
    dojoRenderResearchArchive(report);
  } catch (e) {
    toastErr('结算研究', e);
  }
}

function dojoResearchMd(report) {
  const lines = [];
  lines.push('# 研究档案：' + (report.title || ''));
  lines.push('');
  lines.push('- 学科：' + (report.subject || ''));
  lines.push('- 赛道：' + (report.track || 'A/B'));
  lines.push('- 结论：' + (report.verdict || ''));
  lines.push('- 判断幕平均分：' + Math.round((Number(report.avg_score) || 0) * 100) + '%');
  lines.push('');
  lines.push('## 六幕判断');
  (report.acts || []).forEach((a) => {
    lines.push('- 第 ' + (a.index + 1) + ' 幕 · ' + (a.label || a.ability_name) +
      '：' + Math.round((Number(a.score) || 0) * 100) + '%');
  });
  lines.push('');
  lines.push('## 我的研究结论');
  lines.push(((report.artifact && report.artifact.text) || '').trim() || '（未填写）');
  lines.push('');
  lines.push('## 论证要素诊断（不计分）');
  lines.push('- 覆盖：' + Math.round((Number(report.artifact && report.artifact.coverage) || 0) * 100) + '%');
  const hits = (report.artifact && report.artifact.hits) || [];
  const miss = (report.artifact && report.artifact.missing) || [];
  if (hits.length) lines.push('- 已覆盖：' + hits.map((x) => x.text).join('；'));
  if (miss.length) lines.push('- 缺失：' + miss.map((x) => x.text).join('；'));
  lines.push('');
  lines.push('## 专家示范');
  lines.push(((report.artifact && report.artifact.expert_sample) || '').trim());
  lines.push('');
  lines.push('> 由研究道场生成；诊断只检查要素是否出现，不判断证明是否正确。');
  return lines.join('\n');
}

function dojoExportResearch(report) {
  try {
    const blob = new Blob([dojoResearchMd(report)], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (report.title || 'research') + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) {
    toastErr('导出研究档案', e);
  }
}

function dojoRenderResearchArchive(report, opts) {
  const panel = dojoState.el.researchPanel;
  if (!panel) return;
  const fromRecords = !!(opts && opts.fromRecords);
  dojoState.researchRecordsView = fromRecords;
  dojoState.el.card.classList.add('hidden');
  panel.classList.remove('hidden');
  const rows = (report.acts || []).map((a) =>
    '<tr><td>' + (a.index + 1) + '</td><td>' + dojoEsc(a.label || a.ability_name) +
    '</td><td>' + Math.round((Number(a.score) || 0) * 100) + '%</td></tr>').join('');
  const abilities = (report.by_ability || []).map((a) =>
    '<div class="dojo-report-ability"><span>' + dojoEsc(a.ability_name) + '</span><b>' +
    Math.round((Number(a.score) || 0) * 100) + '%</b></div>').join('');
  const art = report.artifact || {};
  const miss = (art.missing || []).map((x) =>
    '<span class="dojo-hit miss">缺：' + dojoEsc(x.text) + '</span>').join('');

  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">' + dojoEsc(report.title) + '</div>' +
    '<div class="dojo-research-sub">' + dojoEsc(report.subject || '') + ' · ' +
    dojoEsc(report.track || 'A/B') + ' 类 · 研究档案</div></div>' +
    '<button type="button" class="dojo-btn dojo-research-back" id="dojo-research-back">' +
    (fromRecords ? '返回记录' : '返回列表') + '</button></div>' +
    '<div class="dojo-report">' +
    '<div class="dojo-report-verdict ' + (report.first_break ? 'bad' : 'ok') + '">' +
    dojoEsc(report.verdict || '') +
    '<span class="dojo-verdict-note">判断幕平均 ' +
    Math.round((Number(report.avg_score) || 0) * 100) + '%</span></div>' +
    '<table class="dojo-assess-table"><thead><tr><th>幕</th><th>研究动作</th><th>得分</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>' +
    '<div class="dojo-report-abilities">' + abilities + '</div>' +
    '<div class="dojo-block"><div class="dojo-block-title">我的研究结论</div>' +
    '<div class="dojo-block-body">' + dojoEsc(art.text || '') + '</div></div>' +
    '<div class="dojo-block"><div class="dojo-block-title">论证要素诊断（不计分，覆盖 ' +
    Math.round((Number(art.coverage) || 0) * 100) + '%）</div>' +
    (miss || '<div class="dojo-block-body">关键要素都已出现；这仍不等于证明正确。</div>') + '</div>' +
    (art.expert_sample
      ? '<div class="dojo-block"><div class="dojo-block-title">专家示范</div>' +
        '<div class="dojo-block-body">' + dojoEsc(art.expert_sample) + '</div></div>'
      : '') +
    '<div class="dojo-report-note">研究结论不计分；档案可以导出 Markdown，也可以在「研究记录」里回看。</div>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn" id="dojo-research-ask">追问 Solo</button>' +
    '<button type="button" class="dojo-btn primary" id="dojo-research-export">导出 Markdown</button>' +
    '<button type="button" class="dojo-btn" id="dojo-research-again">再走一遍</button>' +
    '</div></div>';

  const back = dojoQ('#dojo-research-back', panel);
  if (back) back.onclick = () => {
    dojoState.research = null;
    if (fromRecords) dojoOpenResearchRecords();
    else dojoRenderResearchHome();
  };
  const ask = dojoQ('#dojo-research-ask', panel);
  if (ask) ask.onclick = () => dojoAskSolo(dojoSoloReportText(report));
  const exp = dojoQ('#dojo-research-export', panel);
  if (exp) exp.onclick = () => dojoExportResearch(report);
  const again = dojoQ('#dojo-research-again', panel);
  if (again) again.onclick = () => dojoStartResearch(report.inquiry_id);
  dojoRenderTop();
  dojoRenderSide();
}

async function dojoExitResearch() {
  if (dojoState.research && dojoState.research.stage !== 'done') dojoSaveResearchSession();
  dojoState.el.card.classList.add('hidden');
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoRenderResearchHome();
}

/* ---------------- 研究记录 ---------------- */

function dojoFormatTime(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  if (isNaN(d.getTime()) || !Number(ts)) return '';
  const p = (n) => (n < 10 ? '0' : '') + n;
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function dojoOpenResearchRecords() {
  try {
    const out = await api('/api/hall/dojo/research/records/list', {});
    dojoState.research = null;
    dojoState.researchLastOut = null;
    dojoState.researchLastValue = null;
    dojoState.researchRecordsView = true;
    dojoRenderResearchRecords(out.records || []);
  } catch (e) {
    toastErr('加载研究记录', e);
  }
}

function dojoRenderResearchRecords(rows) {
  const panel = dojoState.el.researchPanel;
  if (!panel) return;
  const list = rows.length
    ? rows.map((r) => {
      const date = dojoFormatTime(r.finished_at);
      return '<div class="dojo-record">' +
        '<button type="button" class="dojo-record-main" data-record="' + dojoEsc(r.id) + '">' +
        '<span class="dojo-record-title">' + dojoEsc(r.title || r.inquiry_id) + '</span>' +
        '<span class="dojo-record-meta">' + dojoEsc(r.subject || '') +
        (date ? ' · ' + dojoEsc(date) : '') + '</span>' +
        '<span class="dojo-record-verdict">' + dojoEsc(r.verdict || '') + '</span>' +
        '<span class="dojo-record-score">覆盖 ' +
        Math.round((Number(r.coverage) || 0) * 100) + '% · 判断幕 ' +
        Math.round((Number(r.avg_score) || 0) * 100) + '%</span>' +
        '</button>' +
        '<button type="button" class="dojo-record-del" data-del="' + dojoEsc(r.id) + '">删除</button>' +
        '</div>';
    }).join('')
    : '<div class="dojo-empty">还没有研究记录。完成一次研究后会自动保存到这里。</div>';

  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">研究记录</div>' +
    '<div class="dojo-research-sub">完成研究后自动保存；可以回看、再次导出或删除。</div></div>' +
    '<button type="button" class="dojo-btn dojo-research-back" id="dojo-records-back">返回研究</button>' +
    '</div>' +
    '<div class="dojo-record-list">' + list + '</div>';

  const back = dojoQ('#dojo-records-back', panel);
  if (back) back.onclick = () => dojoRenderResearchHome();
  panel.querySelectorAll('[data-record]').forEach((btn) => {
    btn.onclick = () => dojoOpenResearchRecord(btn.dataset.record);
  });
  panel.querySelectorAll('[data-del]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('删除这条研究记录？删除后无法恢复。')) return;
      try {
        await api('/api/hall/dojo/research/records/delete', { id: btn.dataset.del });
        toast('研究记录已删除');
        dojoOpenResearchRecords();
      } catch (e) {
        toastErr('删除研究记录', e);
      }
    };
  });
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoState.el.card.classList.add('hidden');
  dojoRenderTop();
  dojoRenderSide();
}

async function dojoOpenResearchRecord(recordId) {
  try {
    const out = await api('/api/hall/dojo/research/records/get', { id: recordId });
    dojoRenderResearchArchive(out.record, { fromRecords: true });
  } catch (e) {
    toastErr('打开研究记录', e);
  }
}

/* ---------------- 内容工坊：教材大纲 → 草稿审核 ---------------- */

async function dojoOpenAuthoring() {
  try {
    const out = await api('/api/hall/dojo/authoring/syllabi', {});
    const list = out.syllabi || [];
    if (!list.length) {
      toast('还没有教材大纲，先放一份到 resources/dojo/syllabi/', 'error');
      return;
    }
    dojoState.authoringView = true;
    dojoState.research = null;
    dojoState.researchRecordsView = false;
    dojoState.authoring = { syllabi: list, current: list[0].id, detail: null, drafts: [] };
    await dojoAuthoringLoad(list[0].id);
  } catch (e) {
    toastErr('打开内容工坊', e);
  }
}

async function dojoAuthoringLoad(syllabusId) {
  const a = dojoState.authoring;
  if (!a) return;
  const [detail, pending] = await Promise.all([
    api('/api/hall/dojo/authoring/syllabus', { id: syllabusId }),
    api('/api/hall/dojo/authoring/drafts', {}),
  ]);
  a.current = syllabusId;
  a.detail = detail.syllabus;
  a.drafts = pending.drafts || [];
  dojoRenderAuthoring();
}

function dojoRenderAuthoring() {
  const panel = dojoState.el.researchPanel;
  const a = dojoState.authoring;
  if (!panel || !a || !a.detail) return;
  const d = a.detail;
  const chapters = d.chapters || [];
  const done = chapters.filter((c) => c.committed).length;
  const pending = chapters.filter((c) => !c.committed && c.draft).length;

  const draftRows = a.drafts.length
    ? '<div class="dojo-draft-box"><div class="dojo-side-title">待审草稿<span>' +
      a.drafts.length + ' 份</span></div>' +
      a.drafts.map((x) =>
        '<button type="button" class="dojo-draft-row" data-draft="' + dojoEsc(x.id) + '">' +
        '<span>' + dojoEsc(x.chapter_title || x.chapter_id) + '</span>' +
        '<b>' + dojoEsc(x.title || '未命名') + '</b></button>').join('') + '</div>'
    : '';

  const chapterRows = chapters.map((ch) => {
    let state = '未生成';
    let action = '<button type="button" class="dojo-mini" data-gen="' + dojoEsc(ch.id) + '">生成草稿</button>';
    if (ch.committed) {
      state = '已入库';
      action = '<span class="dojo-section-done">已入库</span>';
    } else if (ch.draft) {
      state = '草稿待审';
      action = '<button type="button" class="dojo-mini" data-draft="' + dojoEsc(ch.draft) + '">查看草稿</button>';
    } else if (ch.optional) {
      state = '选学';
    }
    return '<div class="dojo-chapter">' +
      '<div class="dojo-chapter-head"><b>' + dojoEsc(ch.title) + '</b>' +
      '<span>' + (ch.sections || []).length + ' 节 · ' + dojoEsc(state) + '</span></div>' +
      '<div class="dojo-chapter-sections">' +
      (ch.sections || []).map((s) => dojoEsc(s.title)).join(' · ') + '</div>' +
      '<div class="dojo-chapter-action">' + action + '</div></div>';
  }).join('');

  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">内容工坊</div>' +
    '<div class="dojo-research-sub">按教材整章生成 6 幕研究任务；草稿必须审核通过才写入正式题库。</div></div>' +
    '<button type="button" class="dojo-btn dojo-research-back" id="dojo-authoring-back">返回研究</button>' +
    '</div>' +
    '<div class="dojo-authoring-meta"><b>' + dojoEsc(d.course) + '</b> · ' +
    dojoEsc(d.textbook || '') + '<span>已入库 ' + done + ' / ' + chapters.length +
    ' 章 · 待审 ' + pending + ' 份</span></div>' +
    (a.syllabi.length > 1
      ? '<div class="dojo-filter-row">' + a.syllabi.map((s) =>
        '<button type="button" class="dojo-filter-chip' + (s.id === a.current ? ' active' : '') +
        '" data-syllabus="' + dojoEsc(s.id) + '">' + dojoEsc(s.course || s.id) + '</button>').join('') +
        '</div>'
      : '') +
    draftRows +
    '<div class="dojo-chapter-list">' + chapterRows + '</div>';

  const back = dojoQ('#dojo-authoring-back', panel);
  if (back) back.onclick = () => dojoRenderResearchHome();
  panel.querySelectorAll('[data-syllabus]').forEach((btn) => {
    btn.onclick = () => dojoAuthoringLoad(btn.dataset.syllabus);
  });
  panel.querySelectorAll('[data-draft]').forEach((btn) => {
    btn.onclick = () => dojoOpenAuthoringDraft(btn.dataset.draft);
  });
  panel.querySelectorAll('[data-gen]').forEach((btn) => {
    btn.onclick = () => dojoAuthoringGenerate(btn.dataset.gen, btn);
  });
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoState.el.card.classList.add('hidden');
  dojoRenderTop();
  dojoRenderSide();
}

async function dojoAuthoringGenerate(chapterId, btn) {
  const a = dojoState.authoring;
  if (!a || !a.current) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '生成中…';
  }
  try {
    toast('正在生成草稿，可能需要几十秒');
    const out = await api('/api/hall/dojo/authoring/generate', {
      syllabus_id: a.current, chapter_id: chapterId,
    });
    await dojoAuthoringLoad(a.current);
    dojoRenderAuthoringDraft(out.draft);
  } catch (e) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '生成草稿';
    }
    toastErr('生成草稿', e);
  }
}

async function dojoOpenAuthoringDraft(draftId) {
  try {
    const out = await api('/api/hall/dojo/authoring/draft', { id: draftId });
    dojoRenderAuthoringDraft(out.draft);
  } catch (e) {
    toastErr('打开草稿', e);
  }
}

function dojoRenderAuthoringDraft(draft) {
  const panel = dojoState.el.researchPanel;
  if (!panel || !draft) return;
  const doc = draft.doc || {};
  const acts = (doc.acts || []).map((a, i) => {
    const p = a.payload || {};
    const text = p.situation || p.prompt || '';
    return '<div class="dojo-draft-act">' +
      '<div class="dojo-draft-act-head"><b>第 ' + (i + 1) + ' 幕 · ' + dojoEsc(a.label || '') + '</b>' +
      '<span>' + dojoEsc(a.primitive || '') + ' · ' + dojoEsc(a.ability_name || a.ability || '') +
      ' · ' + dojoEsc(a.judge || '') + '</span></div>' +
      '<div class="dojo-draft-act-body">' + dojoEsc(String(text).slice(0, 220)) + '</div></div>';
  }).join('');
  const rubric = ((doc.artifact || {}).rubric || []).map((r) =>
    '<li>' + dojoEsc(r.text || r.id) + '</li>').join('');

  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">草稿审核</div>' +
    '<div class="dojo-research-sub">' + dojoEsc(draft.chapter_title || '') +
    ' · 通过后写入正式题库；退回则删除草稿。</div></div>' +
    '<button type="button" class="dojo-btn dojo-research-back" id="dojo-draft-back">返回章列表</button>' +
    '</div>' +
    '<div class="dojo-draft-head"><b>' + dojoEsc(doc.title || '未命名') + '</b>' +
    '<span>' + dojoEsc(doc.subject || '') + ' · 约 ' + (doc.minutes || 30) + ' 分钟 · ' +
    (doc.acts || []).length + ' 幕</span></div>' +
    '<div class="dojo-draft-acts">' + acts + '</div>' +
    '<div class="dojo-block"><div class="dojo-block-title">研究结论 rubric</div>' +
    '<ul class="dojo-draft-rubric">' + rubric + '</ul></div>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn primary" id="dojo-draft-commit">通过入库</button>' +
    '<button type="button" class="dojo-btn" id="dojo-draft-edit">编辑 JSON</button>' +
    '<button type="button" class="dojo-btn" id="dojo-draft-reject">退回</button>' +
    '</div>';

  const back = dojoQ('#dojo-draft-back', panel);
  if (back) back.onclick = () => dojoRenderAuthoring();
  const commit = dojoQ('#dojo-draft-commit', panel);
  if (commit) commit.onclick = () => dojoCommitAuthoringDraft(draft.id);
  const reject = dojoQ('#dojo-draft-reject', panel);
  if (reject) reject.onclick = () => dojoRejectAuthoringDraft(draft.id);
  const edit = dojoQ('#dojo-draft-edit', panel);
  if (edit) edit.onclick = () => dojoRenderAuthoringEdit(draft);
  dojoState.el.researchPanel.classList.remove('hidden');
  dojoState.el.card.classList.add('hidden');
  dojoRenderTop();
  dojoRenderSide();
}

function dojoRenderAuthoringEdit(draft) {
  const panel = dojoState.el.researchPanel;
  if (!panel || !draft) return;
  panel.innerHTML =
    '<div class="dojo-research-head">' +
    '<div><div class="dojo-research-title">编辑草稿 JSON</div>' +
    '<div class="dojo-research-sub">保存时会重新走一遍正式校验；校验不过不会覆盖草稿。</div></div>' +
    '<button type="button" class="dojo-btn dojo-research-back" id="dojo-edit-back">返回预览</button>' +
    '</div>' +
    '<textarea class="dojo-authoring-json" id="dojo-authoring-json">' +
    dojoEsc(JSON.stringify(draft.doc || {}, null, 2)) + '</textarea>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn primary" id="dojo-edit-save">保存并校验</button>' +
    '<button type="button" class="dojo-btn" id="dojo-edit-cancel">取消</button>' +
    '</div>';

  const back = dojoQ('#dojo-edit-back', panel);
  if (back) back.onclick = () => dojoRenderAuthoringDraft(draft);
  const cancel = dojoQ('#dojo-edit-cancel', panel);
  if (cancel) cancel.onclick = () => dojoRenderAuthoringDraft(draft);
  const save = dojoQ('#dojo-edit-save', panel);
  if (save) {
    save.onclick = async () => {
      const ta = dojoQ('#dojo-authoring-json', panel);
      let doc;
      try {
        doc = JSON.parse(ta.value);
      } catch (e) {
        toast('JSON 语法不对：' + e.message, 'error');
        return;
      }
      try {
        const out = await api('/api/hall/dojo/authoring/update', { id: draft.id, doc: doc });
        toast('草稿已更新并通过校验');
        dojoRenderAuthoringDraft(out.draft);
      } catch (e) {
        toastErr('保存草稿', e);
      }
    };
  }
  dojoRenderTop();
}

async function dojoCommitAuthoringDraft(draftId) {
  try {
    const out = await api('/api/hall/dojo/authoring/commit', { id: draftId });
    toast('已入库：' + (out.title || out.item_id));
    dojoState.authoring.drafts = (dojoState.authoring.drafts || [])
      .filter((d) => d.id !== draftId);
    await dojoAuthoringLoad(dojoState.authoring.current);
  } catch (e) {
    toastErr('草稿入库', e);
  }
}

async function dojoRejectAuthoringDraft(draftId) {
  if (!confirm('退回并删除这份草稿？')) return;
  try {
    await api('/api/hall/dojo/authoring/reject', { id: draftId });
    toast('草稿已退回');
    await dojoAuthoringLoad(dojoState.authoring.current);
  } catch (e) {
    toastErr('退回草稿', e);
  }
}

/* ---------------- 追问 Solo ---------------- */

function dojoOptionLines(unit) {
  const p = unit.payload || {};
  if (!Array.isArray(p.options) || !p.options.length) return '';
  return p.options.map((o, i) => {
    const key = (o && o.key) || String.fromCharCode(65 + i);
    return key + '. ' + ((o && o.text) || o || '');
  }).join('\n');
}

function dojoStepLines(unit) {
  const p = unit.payload || {};
  if (!Array.isArray(p.steps) || !p.steps.length) return '';
  return p.steps.map((s, i) => String.fromCharCode(65 + i) + '. ' + (s.text || s.id)).join('\n');
}

function dojoAnswerText(unit, value) {
  if (value == null) return '（未记录）';
  if (unit.primitive === 'single_choice') return String(value);
  if (unit.primitive === 'multi_select') {
    return Array.isArray(value) ? value.join('、') : String(value);
  }
  if (unit.primitive === 'order') {
    const p = unit.payload || {};
    const map = {};
    (p.steps || []).forEach((s, i) => { map[s.id] = String.fromCharCode(65 + i) + '. ' + (s.text || s.id); });
    return (Array.isArray(value) ? value : []).map((id) => map[id] || id).join('\n');
  }
  return String(value);
}

function dojoReferenceText(unit, expert) {
  const e = expert || {};
  if (unit.primitive === 'single_choice') return String(e.choice || '');
  if (unit.primitive === 'multi_select') return (e.choice || []).join('、');
  if (unit.primitive === 'order') {
    const p = unit.payload || {};
    const map = {};
    (p.steps || []).forEach((s, i) => { map[s.id] = String.fromCharCode(65 + i) + '. ' + (s.text || s.id); });
    return (e.order || []).map((id) => map[id] || id).join('\n');
  }
  if (unit.primitive === 'numeric') return String(e.value);
  if (unit.primitive === 'expr') return String(e.expr || '');
  return '';
}

function dojoSoloActText() {
  const r = dojoState.research;
  const u = dojoState.unit;
  if (!r || !u) return '';
  const q = r.inquiry || {};
  const out = dojoState.researchLastOut || {};
  const expert = out.expert || {};
  const lines = ['【研究道场 · 第 ' + ((Number(r.index) || 0) + 1) + ' 幕追问】', ''];
  if (q.title) lines.push('研究主题：' + q.title);
  if (q.subject) lines.push('学科：' + q.subject);
  if (u.label) lines.push('这一幕：' + u.label + (u.ability_name ? '（' + u.ability_name + '）' : ''));
  lines.push('', '题干：', (u.payload && u.payload.situation) || '');
  const opts = dojoOptionLines(u);
  if (opts) lines.push('', '选项：', opts);
  const steps = dojoStepLines(u);
  if (steps) lines.push('', '步骤：', steps);
  lines.push('', '我的答案：', dojoAnswerText(u, dojoState.researchLastValue));
  const ref = dojoReferenceText(u, expert);
  if (ref) lines.push('', '参考判断：', ref);
  if (out.correct != null) lines.push('结果：' + (out.correct ? '判断成立' : '判断不成立'));
  if (expert.why) lines.push('', '专家为什么这样想：', expert.why);
  if (expert.signals && expert.signals.length) {
    lines.push('', '识别信号：', expert.signals.map((s) => '- ' + s).join('\n'));
  }
  const card = out.card || {};
  if (card.title) {
    lines.push('', '方法卡：' + card.title,
      '触发：' + (card.trigger || ''),
      '第一步：' + (card.first_step || ''),
      '常见坑：' + (card.pitfall || ''));
  }
  lines.push('', '追问：');
  return lines.join('\n');
}

function dojoSoloArtifactText() {
  const r = dojoState.research;
  if (!r || !r.artifact) return '';
  const art = r.artifact;
  const q = r.inquiry || {};
  const lines = ['【研究道场 · 研究结论追问】', ''];
  if (q.title) lines.push('研究主题：' + q.title);
  if (q.subject) lines.push('学科：' + q.subject);
  const prompt = (q.artifact && q.artifact.prompt) || '';
  if (prompt) lines.push('', '研究问题：', prompt);
  lines.push('', '我的研究结论：', (art.text || '').trim() || '（未填写）');
  lines.push('', '论证要素覆盖：' + Math.round((Number(art.coverage) || 0) * 100) + '%');
  if ((art.hits || []).length) lines.push('已覆盖：' + art.hits.map((x) => x.text).join('；'));
  if ((art.missing || []).length) lines.push('缺失：' + art.missing.map((x) => x.text).join('；'));
  if (art.expert_sample) lines.push('', '专家示范：', art.expert_sample);
  lines.push('', '追问：');
  return lines.join('\n');
}

function dojoSoloReportText(report) {
  const rep = report || {};
  const art = rep.artifact || {};
  const lines = ['【研究道场 · 研究档案追问】', ''];
  if (rep.title) lines.push('研究主题：' + rep.title);
  if (rep.subject) lines.push('学科：' + rep.subject);
  if (rep.verdict) lines.push('结论：' + rep.verdict);
  lines.push('判断幕平均：' + Math.round((Number(rep.avg_score) || 0) * 100) + '%');
  lines.push('', '六幕判断：');
  (rep.acts || []).forEach((a) => {
    lines.push((Number(a.index) + 1) + '. ' + (a.label || a.ability_name || '') +
      '：' + Math.round((Number(a.score) || 0) * 100) + '%');
  });
  if (art.prompt) lines.push('', '研究问题：', art.prompt);
  lines.push('', '我的研究结论：', (art.text || '').trim() || '（未填写）');
  lines.push('', '论证要素覆盖：' + Math.round((Number(art.coverage) || 0) * 100) + '%');
  if ((art.missing || []).length) lines.push('缺失：' + art.missing.map((x) => x.text).join('；'));
  if (art.expert_sample) lines.push('', '专家示范：', art.expert_sample);
  lines.push('', '追问：');
  return lines.join('\n');
}

function dojoAskSolo(text) {
  if (!text) return;
  if (typeof setUiMode !== 'function') {
    toast('当前版本没有 Solo 模式，无法追问', 'error');
    return;
  }
  try {
    if (typeof state !== 'undefined' && state.streaming) {
      toast('Solo 正在生成回答，等结束再追问', 'error');
      return;
    }
  } catch (e) { /* state 不可用时继续 */ }
  const input = document.querySelector('#input');
  if (!input) {
    toast('找不到 Solo 输入框，请手动切到 Solo', 'error');
    return;
  }
  if (input.value.trim() && !confirm('Solo 输入框已有内容，要替换成这次追问吗？')) return;
  setUiMode('solo');
  input.value = text;
  if (typeof input.setSelectionRange === 'function') {
    const end = input.value.length;
    input.setSelectionRange(end, end);
  }
  if (typeof autoGrow === 'function') autoGrow();
  input.focus();
  toast('已带到 Solo，请在「追问：」后写下你的问题');
}

function dojoRenderSide() {
  const side = dojoState.el.side;
  if (!side) return;
  const blocks = [];
  const rs = dojoState.research;
  const cs = dojoState.caseSession;

  if (cs) {
    blocks.push('<div class="dojo-side-block"><div class="dojo-side-title">这个案例<span>' +
      cs.total + ' 步</span></div>' +
      '<div class="dojo-align-line">来源：<b>' + dojoEsc(dojoCaseSourceText(cs)) + '</b></div>' +
      '<div class="dojo-align-line">案例得分不计入单题统计；六步走完再看断点。</div></div>');
  } else if (rs) {
    const q = rs.inquiry || {};
    blocks.push('<div class="dojo-side-block"><div class="dojo-side-title">这项研究<span>' +
      (rs.stage === 'done' ? '已完成' : ('第 ' + Math.min(rs.index + 1, rs.total) + ' / ' +
        rs.total + ' 幕')) + '</span></div>' +
      '<div class="dojo-align-line">学科：<b>' + dojoEsc(q.subject || '') + '</b></div>' +
      '<div class="dojo-align-line">考试对接：' +
      dojoEsc((q.exam && q.exam.topic) || '—') + '</div>' +
      '<div class="dojo-align-line">判断幕计分；研究结论只做要素诊断。</div></div>');
  } else {
    blocks.push('<div class="dojo-side-block"><div class="dojo-side-title">怎么研究<span>6 幕</span></div>' +
      '<div class="dojo-align-line">1. 它从哪来：回到问题源头</div>' +
      '<div class="dojo-align-line">2. 先猜：提出可检验的猜想</div>' +
      '<div class="dojo-align-line">3. 再破：构造反例或边界</div>' +
      '<div class="dojo-align-line">4. 后立：重建证明骨架</div>' +
      '<div class="dojo-align-line">5. 看条件：适用范围与失效点</div>' +
      '<div class="dojo-align-line">6. 落地：考试怎么考</div></div>');
    blocks.push('<div class="dojo-side-block"><div class="dojo-side-title">计分说明</div>' +
      '<div class="dojo-align-line">判断幕按 L1/L2 计分；研究结论只做论证要素覆盖，不计分。</div></div>');
  }

  side.innerHTML = blocks.join('');
}

/* ---------------- 复习模式（间隔重复） ---------------- */

function dojoPackName(packId) {
  const p = (dojoState.packs || []).find((x) => x.id === packId);
  return p ? p.name : '已移除的场景';
}

function dojoStartReview() {
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.reproPack) dojoExitRepro();
  const due = dojoDueCards();
  if (!due.length) {
    toast('还没有到期的方法卡；先做几题，卡片会按 1/3/7/15 天回来');
    return;
  }
  due.sort((a, b) => (Number(a.next_at) || 0) - (Number(b.next_at) || 0));
  dojoState.review = { queue: due, i: 0, flipped: false, remembered: 0, forgot: 0 };
  dojoState.el.card.classList.add('hidden');
  dojoState.el.reviewPanel.classList.remove('hidden');
  const scroller = dojoState.el.root.closest('.hall-body') || document.getElementById('hall-body');
  if (scroller) scroller.scrollTop = 0;
  dojoRenderReview();
}

function dojoExitReview() {
  dojoState.review = null;
  if (dojoState.el.reviewPanel) {
    dojoState.el.reviewPanel.classList.add('hidden');
    dojoState.el.reviewPanel.innerHTML = '';
  }
  if (dojoState.el.card) dojoState.el.card.classList.remove('hidden');
  dojoRenderTop();
}

function dojoFlipReview() {
  const r = dojoState.review;
  if (!r || r.flipped) return;
  r.flipped = true;
  dojoRenderReview();
}

function dojoRateReview(remembered) {
  const r = dojoState.review;
  if (!r || !r.flipped) return;
  const c = r.queue[r.i];
  if (!c) return;
  if (remembered) {
    c.stage = Math.min((Number(c.stage) || 0) + 1, DOJO_REVIEW_DAYS.length - 1);
    c.next_at = Date.now() + dojoReviewDays(c.stage) * DOJO_DAY_MS;
    r.remembered += 1;
  } else {
    c.stage = 0;
    // 10 分钟后再来：本轮不重复，但今天还能再遇到一次
    c.next_at = Date.now() + 10 * 60 * 1000;
    r.forgot += 1;
  }
  c.seen = (Number(c.seen) || 0) + 1;
  dojoSaveStore();
  r.i += 1;
  r.flipped = false;
  dojoRenderReview();
  dojoRenderSide();
}

function dojoRenderReview() {
  const r = dojoState.review;
  const box = dojoState.el.reviewPanel;
  if (!r || !box) return;
  if (r.i >= r.queue.length) {
    dojoRenderReviewDone();
    return;
  }
  const c = r.queue[r.i];
  const top = '<div class="dojo-review-top">' +
    '<span class="dojo-pill accent">复习 ' + (r.i + 1) + ' / ' + r.queue.length + '</span>' +
    '<span class="dojo-pill mono">' + dojoEsc(dojoPackName(c.pack)) + '</span>' +
    '<button type="button" class="dojo-btn dojo-review-exit" id="dojo-review-exit">退出复习</button>' +
    '</div>';

  if (!r.flipped) {
    box.innerHTML = top +
      '<div class="dojo-review-card">' +
      '<div class="dojo-review-tag">触发条件</div>' +
      '<div class="dojo-review-trigger">' + dojoEsc(c.trigger || c.title) + '</div>' +
      '<div class="dojo-review-prompt">看到这个信号时，你的第一步是什么？</div>' +
      '<div class="dojo-actions"><button type="button" class="dojo-btn primary" id="dojo-review-flip">翻面看答案</button></div>' +
      '<div class="dojo-review-hint">空格翻面 · Esc 退出</div>' +
      '</div>';
    dojoQ('#dojo-review-exit', box).onclick = dojoExitReview;
    dojoQ('#dojo-review-flip', box).onclick = dojoFlipReview;
    return;
  }

  box.innerHTML = top +
    '<div class="dojo-review-card">' +
    '<div class="dojo-card-note-head">方法卡 · ' + dojoEsc(c.title) + '</div>' +
    '<dl class="dojo-review-dl">' +
    '<dt>触发</dt><dd>' + dojoEsc(c.trigger) + '</dd>' +
    '<dt>第一步</dt><dd>' + dojoEsc(c.first_step) + '</dd>' +
    '<dt>常见坑</dt><dd>' + dojoEsc(c.pitfall) + '</dd>' +
    '</dl>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn" data-remember="0">忘了</button>' +
    '<button type="button" class="dojo-btn primary" data-remember="1">记得</button>' +
    '</div><div class="dojo-review-hint">1 忘了 · 2 记得 · Esc 退出</div>' +
    '</div>';
  dojoQ('#dojo-review-exit', box).onclick = dojoExitReview;
  box.querySelectorAll('[data-remember]').forEach((btn) => {
    btn.onclick = () => dojoRateReview(btn.dataset.remember === '1');
  });
}

function dojoRenderReviewDone() {
  const r = dojoState.review;
  const box = dojoState.el.reviewPanel;
  const total = r.remembered + r.forgot;
  const tail = r.forgot > 0
    ? '忘了的卡已回到第一档，10 分钟后会再出现。'
    : '记得的卡会按 1 / 3 / 7 / 15 天回来。';
  box.innerHTML = '<div class="dojo-review-card">' +
    '<div class="dojo-review-tag">本轮完成</div>' +
    '<div class="dojo-review-trigger">过了 ' + total + ' 张</div>' +
    '<div class="dojo-review-prompt">记得 ' + r.remembered + ' · 忘了 ' + r.forgot +
    '<br>' + tail + '</div>' +
    '<div class="dojo-actions"><button type="button" class="dojo-btn primary" id="dojo-review-done">回到训练</button></div>' +
    '</div>';
  dojoQ('#dojo-review-done', box).onclick = dojoExitReview;
  dojoRenderTop();
}

/* ---------------- 测评：前测 / 后测 ---------------- */

function dojoAssessHide() {
  if (dojoState.el.assessPanel) {
    dojoState.el.assessPanel.classList.add('hidden');
    dojoState.el.assessPanel.innerHTML = '';
  }
  if (dojoState.el.card) dojoState.el.card.classList.remove('hidden');
  dojoState.assess = null;
}

async function dojoStartAssess(phase) {
  const pack = dojoState.pack;
  if (!pack || !pack.assessment) return;
  dojoClosePackMenu();
  if (dojoState.review) dojoExitReview();
  if (dojoState.reproPack) dojoExitRepro();
  try {
    const out = await api('/api/hall/dojo/assess/start', { pack_id: pack.id, phase: phase });
    dojoState.assess = {
      session: out.session_id,
      phase: out.phase,
      packId: out.pack_id,
      packName: out.pack_name,
      units: out.units || [],
      i: 0,
      results: [],
      revealed: false,
      busy: false,
      qStartedAt: Date.now(),
    };
    dojoState.el.card.classList.add('hidden');
    dojoState.el.assessPanel.classList.remove('hidden');
    const scroller = dojoState.el.root.closest('.hall-body') || document.getElementById('hall-body');
    if (scroller) scroller.scrollTop = 0;
    dojoRenderAssess();
  } catch (e) {
    toastErr('开始测评', e);
  }
}

function dojoExitAssess() {
  dojoAssessHide();
  dojoRenderTop();
  dojoRenderSide();
}

function dojoRenderAssess() {
  const a = dojoState.assess;
  const box = dojoState.el.assessPanel;
  if (!a || !box) return;
  if (a.i >= a.units.length) {
    dojoFinishAssess();
    return;
  }
  const unit = a.units[a.i];
  const p = unit.payload || {};
  const phaseName = a.phase === 'pre' ? '前测' : '后测';
  const multi = unit.primitive === 'multi_select';
  a.pick = new Set();
  a.revealed = false;
  a.qStartedAt = Date.now();

  const opts = (p.options || []).map((o, idx) => {
    const key = (o && o.key) || String.fromCharCode(65 + idx);
    const text = (o && o.text) || o || '';
    return '<button type="button" class="dojo-opt" data-key="' + dojoEsc(key) + '">' +
      '<span class="dojo-opt-key">' + dojoEsc(key) + '</span><span>' + dojoEsc(text) + '</span></button>';
  }).join('');

  box.innerHTML = '<div class="dojo-review-top">' +
    '<span class="dojo-pill accent">' + phaseName + ' ' + (a.i + 1) + ' / ' + a.units.length + '</span>' +
    '<span class="dojo-pill mono">' + dojoEsc(a.packName) + '</span>' +
    '<button type="button" class="dojo-btn dojo-review-exit" id="dojo-assess-exit">退出测评</button>' +
    '</div>' +
    '<div class="dojo-review-card">' +
    '<div class="dojo-assess-meta"><span class="dojo-chip ability">' +
    dojoEsc(unit.ability_name || '') + '</span>' +
    '<span class="dojo-chip">' + (multi ? '多选' : '单选') + '</span></div>' +
    '<div class="dojo-assess-situation">' + dojoEsc(p.situation || '') + '</div>' +
    '<div class="dojo-options" id="dojo-assess-options">' + opts + '</div>' +
    '<div class="dojo-actions"><button type="button" class="dojo-btn primary" id="dojo-assess-submit">提交</button></div>' +
    '<div class="dojo-msg" id="dojo-assess-msg"></div>' +
    '<div id="dojo-assess-reveal"></div>' +
    '</div>';

  dojoQ('#dojo-assess-exit', box).onclick = dojoExitAssess;
  const wrap = dojoQ('#dojo-assess-options', box);
  wrap.querySelectorAll('.dojo-opt').forEach((btn) => {
    btn.onclick = () => {
      if (a.revealed) return;
      const key = btn.dataset.key;
      if (multi) {
        if (a.pick.has(key)) a.pick.delete(key); else a.pick.add(key);
        btn.classList.toggle('sel', a.pick.has(key));
      } else {
        a.pick.clear();
        a.pick.add(key);
        wrap.querySelectorAll('.dojo-opt').forEach((b) => b.classList.toggle('sel', b.dataset.key === key));
      }
      dojoQ('#dojo-assess-msg', box).textContent = '';
    };
  });
  dojoQ('#dojo-assess-submit', box).onclick = () => dojoAssessSubmit();
}

async function dojoAssessSubmit() {
  const a = dojoState.assess;
  if (!a || a.busy || a.revealed) return;
  const box = dojoState.el.assessPanel;
  const unit = a.units[a.i];
  const chosen = Array.from(a.pick || []);
  const value = unit.primitive === 'multi_select' ? chosen : (chosen[0] || '');
  if (!value || (Array.isArray(value) && !value.length)) {
    dojoQ('#dojo-assess-msg', box).textContent = '先选一个答案';
    return;
  }
  a.busy = true;
  const btn = dojoQ('#dojo-assess-submit', box);
  btn.disabled = true;
  try {
    const out = await api('/api/hall/dojo/assess/answer', {
      session_id: a.session,
      unit_id: unit.id,
      value: value,
      elapsed_ms: Date.now() - a.qStartedAt,
    });
    a.results.push(out);
    a.revealed = true;
    dojoRenderAssessReveal(out, chosen);
  } catch (e) {
    a.busy = false;
    btn.disabled = false;
    dojoQ('#dojo-assess-msg', box).textContent = (e && e.message) || String(e);
  }
}

function dojoRenderAssessReveal(out, chosen) {
  const a = dojoState.assess;
  const box = dojoState.el.assessPanel;
  const unit = a.units[a.i];
  const wrap = dojoQ('#dojo-assess-options', box);
  const detail = out.detail || {};
  const correctKey = detail.choice;                 // 单选
  wrap.querySelectorAll('.dojo-opt').forEach((btn) => {
    const key = btn.dataset.key;
    btn.classList.add('resolved');
    if (unit.primitive === 'single_choice' && key === correctKey) btn.classList.add('ok');
    if (unit.primitive === 'single_choice' && key !== correctKey && chosen.indexOf(key) >= 0) btn.classList.add('bad');
    if (unit.primitive === 'multi_select') {
      // 多选：服务端没有回传标准答案，只标出用户的选择对错
      if (chosen.indexOf(key) >= 0) btn.classList.add(out.correct ? 'ok' : 'bad');
    }
  });
  const submit = dojoQ('#dojo-assess-submit', box);
  submit.classList.add('hidden');
  const reveal = dojoQ('#dojo-assess-reveal', box);
  reveal.innerHTML = '<div class="dojo-verdict ' + (out.correct ? 'ok' : 'bad') + '">' +
    (out.correct ? '答对了' : '答错了') +
    '<span class="dojo-verdict-note">掌握度 ' + Math.round((Number(out.score) || 0) * 100) + '%</span></div>' +
    (out.expert && out.expert.why
      ? '<div class="dojo-block"><div class="dojo-block-title">为什么</div>' +
        '<div class="dojo-block-body">' + dojoEsc(out.expert.why) + '</div></div>'
      : '') +
    '<div class="dojo-actions"><button type="button" class="dojo-btn primary" id="dojo-assess-next">' +
    (a.i + 1 >= a.units.length ? '看结果' : '下一题') + '</button></div>';
  dojoQ('#dojo-assess-next', box).onclick = () => {
    a.i += 1;
    a.busy = false;
    a.revealed = false;
    dojoRenderAssess();
  };
  dojoQ('#dojo-assess-msg', box).textContent = '';
}

async function dojoFinishAssess() {
  const a = dojoState.assess;
  const box = dojoState.el.assessPanel;
  if (!a || !box) return;
  try {
    const out = await api('/api/hall/dojo/assess/finish', { session_id: a.session });
    const store = dojoState.store;
    store.assessments = store.assessments || {};
    const perPack = store.assessments[a.packId] || {};
    perPack[out.phase] = {
      at: Date.now(),
      total: out.total,
      correct: out.correct,
      rate: out.rate,
      elapsed_ms: out.elapsed_ms,
      per_minute: out.per_minute,
      by_ability: out.by_ability || [],
    };
    store.assessments[a.packId] = perPack;
    dojoSaveStore();
    dojoRenderAssessResult(out);
  } catch (e) {
    box.innerHTML = '<div class="dojo-review-card"><div class="dojo-review-tag">结算失败</div>' +
      '<div class="dojo-review-prompt">' + dojoEsc((e && e.message) || String(e)) + '</div>' +
      '<div class="dojo-actions"><button type="button" class="dojo-btn" id="dojo-assess-exit2">退出</button></div></div>';
    dojoQ('#dojo-assess-exit2', box).onclick = dojoExitAssess;
  }
}

function dojoAssessRate(rec) {
  return rec ? Math.round((Number(rec.rate) || 0) * 100) : null;
}

function dojoRenderAssessResult(out) {
  const a = dojoState.assess;
  const box = dojoState.el.assessPanel;
  const phaseName = out.phase === 'pre' ? '前测' : '后测';
  const perPack = (dojoState.store.assessments || {})[a.packId] || {};
  const pre = perPack.pre;
  let compare = '';
  if (out.phase === 'post' && pre) {
    const delta = Math.round((out.rate - pre.rate) * 100);
    compare = '<div class="dojo-assess-delta ' + (delta >= 0 ? 'up' : 'down') + '">' +
      (delta >= 0 ? '+' : '') + delta + ' 个百分点（相对前测）</div>';
  } else if (out.phase === 'pre') {
    compare = '<div class="dojo-assess-note">练一段时间后回来做后测，才有可比较的数据。</div>';
  } else {
    compare = '<div class="dojo-assess-note">没有前测记录，这次只能看绝对值。</div>';
  }
  box.innerHTML = '<div class="dojo-review-top">' +
    '<span class="dojo-pill accent">' + phaseName + '完成</span>' +
    '<span class="dojo-pill mono">' + dojoEsc(a.packName) + '</span>' +
    '</div>' +
    '<div class="dojo-review-card">' +
    '<div class="dojo-assess-big">' + out.correct + ' / ' + out.total + '</div>' +
    '<div class="dojo-assess-sub">正确率 ' + Math.round(out.rate * 100) + '% · 每分钟答对 ' +
    out.per_minute + ' 题 · 用时 ' + Math.round(out.elapsed_ms / 1000) + ' 秒</div>' +
    compare +
    '<div class="dojo-assess-note">同考点、不同表述的前后测，没有对照组；每题只出现一次，' +
    '样本也小。只能作为个人参考，不能当作因果结论。</div>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn primary" id="dojo-assess-done">回到训练</button>' +
    '<button type="button" class="dojo-btn" id="dojo-assess-report">看报告</button>' +
    '</div></div>';
  dojoQ('#dojo-assess-done', box).onclick = dojoExitAssess;
  dojoQ('#dojo-assess-report', box).onclick = () => dojoShowAssessReport();
  dojoRenderTop();
  dojoRenderSide();
}

function dojoFmtDay(ts) {
  const d = new Date(Number(ts) || Date.now());
  const p = (n) => (n < 10 ? '0' + n : String(n));
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function dojoShowAssessReport() {
  const pack = dojoState.pack;
  const box = dojoState.el.assessPanel;
  if (!pack || !box) return;
  dojoClosePackMenu();
  if (dojoState.review) dojoExitReview();
  const perPack = (dojoState.store.assessments || {})[pack.id] || {};
  const pre = perPack.pre;
  const post = perPack.post;
  let body = '';
  if (!pre && !post) {
    body = '<div class="dojo-review-prompt">还没有测评记录。先做一次前测，练一段时间后再做后测。</div>';
  } else {
    const row = (label, rec) => rec
      ? '<tr><td>' + label + '</td><td>' + dojoFmtDay(rec.at) + '</td>' +
        '<td>' + rec.correct + ' / ' + rec.total + '</td>' +
        '<td>' + dojoAssessRate(rec) + '%</td>' +
        '<td>' + rec.per_minute + ' 题/分</td></tr>'
      : '<tr><td>' + label + '</td><td colspan="4" class="dojo-assess-muted">未测</td></tr>';
    let delta = '';
    if (pre && post) {
      const d = Math.round((post.rate - pre.rate) * 100);
      delta = '<div class="dojo-assess-delta ' + (d >= 0 ? 'up' : 'down') + '">' +
        (d >= 0 ? '+' : '') + d + ' 个百分点</div>';
    }
    // 能力维度对比
    const abilities = {};
    (pre && pre.by_ability || []).forEach((x) => { abilities[x.ability] = { name: x.ability_name, pre: x }; });
    (post && post.by_ability || []).forEach((x) => {
      abilities[x.ability] = Object.assign({ name: x.ability_name }, abilities[x.ability] || {}, { post: x });
    });
    const abRows = Object.keys(abilities).map((k) => {
      const it = abilities[k];
      const fmt = (x) => x ? (x.correct + '/' + x.total) : '—';
      return '<tr><td>' + dojoEsc(it.name || k) + '</td><td>' + fmt(it.pre) + '</td><td>' + fmt(it.post) + '</td></tr>';
    }).join('');
    body = '<table class="dojo-assess-table"><thead><tr>' +
      '<th>阶段</th><th>日期</th><th>正确</th><th>正确率</th><th>速度</th></tr></thead><tbody>' +
      row('前测', pre) + row('后测', post) + '</tbody></table>' +
      delta +
      (abRows ? '<div class="dojo-assess-sub">按能力维度</div>' +
        '<table class="dojo-assess-table"><thead><tr><th>维度</th><th>前测</th><th>后测</th></tr></thead>' +
        '<tbody>' + abRows + '</tbody></table>' : '');
  }
  box.innerHTML = '<div class="dojo-review-top">' +
    '<span class="dojo-pill accent">测评报告</span>' +
    '<span class="dojo-pill mono">' + dojoEsc(pack.name) + '</span>' +
    '<button type="button" class="dojo-btn dojo-review-exit" id="dojo-report-exit">关闭</button>' +
    '</div>' +
    '<div class="dojo-review-card">' + body +
    '<div class="dojo-assess-note">同考点、不同表述的前后测，没有对照组；每题只出现一次，' +
    '样本也小。这里的数字只能作为个人参考，不能当作「这个模块让你提了多少分」的证据。</div>' +
    '</div>';
  dojoState.el.card.classList.add('hidden');
  box.classList.remove('hidden');
  dojoQ('#dojo-report-exit', box).onclick = dojoExitAssess;
}

/* ---------------- 复现实验：数据 + 半截代码 + 数值校验 ---------------- */

function dojoExitRepro() {
  dojoState.reproPack = null;
  if (dojoState.el.reproPanel) {
    dojoState.el.reproPanel.classList.add('hidden');
    dojoState.el.reproPanel.innerHTML = '';
  }
  if (dojoState.el.card) dojoState.el.card.classList.remove('hidden');
}

async function dojoOpenRepro(packId) {
  const list = dojoState.repro || [];
  const target = packId || (list[0] && list[0].id);
  if (!target) {
    toast('还没有复现实验');
    return;
  }
  dojoClosePackMenu();
  if (dojoState.review) dojoExitReview();
  if (dojoState.assess) dojoAssessHide();
  try {
    const out = await api('/api/hall/dojo/repro/get', { id: target });
    dojoState.reproPack = out.pack;
    dojoState.el.card.classList.add('hidden');
    dojoState.el.reproPanel.classList.remove('hidden');
    const scroller = dojoState.el.root.closest('.hall-body') || document.getElementById('hall-body');
    if (scroller) scroller.scrollTop = 0;
    dojoRenderRepro(out.pack);
  } catch (e) {
    toastErr('打开复现实验', e);
  }
}

function dojoReproTargets(expected) {
  return Object.keys(expected || {}).map((name) => {
    const spec = expected[name] || {};
    const value = spec && typeof spec === 'object' ? spec.value : spec;
    const tol = spec && typeof spec === 'object' ? spec.tolerance : null;
    return '<div class="dojo-repro-target-line"><b>' + dojoEsc(name) + '</b> = ' +
      dojoEsc(value) + (tol != null ? ' <span>（容差 ±' + dojoEsc(tol) + '）</span>' : '') + '</div>';
  }).join('');
}

function dojoRenderRepro(pack) {
  const box = dojoState.el.reproPanel;
  const data = (pack.data || []).map((d) =>
    '<div class="dojo-repro-file"><div class="dojo-repro-file-name">' + dojoEsc(d.name) + '</div>' +
    '<pre class="dojo-repro-data">' + dojoEsc(d.preview) + '</pre></div>').join('');
  box.innerHTML = '<div class="dojo-review-top">' +
    '<span class="dojo-pill accent">复现实验</span>' +
    '<span class="dojo-pill mono">' + dojoEsc(pack.title) + '</span>' +
    '<button type="button" class="dojo-btn dojo-review-exit" id="dojo-repro-exit">退出</button>' +
    '</div>' +
    '<div class="dojo-repro-grid">' +
    '<div class="dojo-repro-left">' +
    '<div class="dojo-repro-question">' + dojoEsc(pack.question) + '</div>' +
    (pack.source ? '<div class="dojo-repro-source">来源：' + dojoEsc(pack.source) + '</div>' : '') +
    '<div class="dojo-repro-targets"><div class="dojo-block-title">目标值</div>' +
    dojoReproTargets(pack.expected) + '</div>' +
    data +
    '</div>' +
    '<div class="dojo-repro-right">' +
    '<textarea class="dojo-repro-code" id="dojo-repro-code" spellcheck="false"></textarea>' +
    '<div class="dojo-actions">' +
    '<button type="button" class="dojo-btn primary" id="dojo-repro-run">运行并校验</button>' +
    '<span class="dojo-repro-hint">Ctrl+Enter 运行</span>' +
    '</div>' +
    '<div class="dojo-repro-note">会在本机临时目录执行你写的 Python（超时 ' +
    (pack.time_limit || 10) + ' 秒）。只运行你自己写的代码，不要粘贴来源不明的脚本。</div>' +
    '<div id="dojo-repro-result"></div>' +
    '</div></div>';

  dojoQ('#dojo-repro-exit', box).onclick = dojoExitRepro;
  dojoQ('#dojo-repro-run', box).onclick = () => dojoRunRepro();
  const code = dojoQ('#dojo-repro-code', box);
  code.value = pack.starter || '';
  code.focus();
}

async function dojoRunRepro() {
  const pack = dojoState.reproPack;
  const box = dojoState.el.reproPanel;
  if (!pack || !box) return;
  const codeEl = dojoQ('#dojo-repro-code', box);
  const btn = dojoQ('#dojo-repro-run', box);
  const result = dojoQ('#dojo-repro-result', box);
  if (!codeEl || !btn || !result) return;
  btn.disabled = true;
  btn.textContent = '运行中…';
  result.innerHTML = '<div class="dojo-msg">正在临时目录执行…</div>';
  try {
    const out = await api('/api/hall/dojo/repro/run', { id: pack.id, code: codeEl.value });
    dojoRenderReproResult(out);
  } catch (e) {
    result.innerHTML = '<div class="dojo-msg err">' + dojoEsc((e && e.message) || String(e)) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = '运行并校验';
  }
}

function dojoRenderReproResult(out) {
  const box = dojoState.el.reproPanel;
  const result = box && dojoQ('#dojo-repro-result', box);
  if (!result) return;
  const rows = (out.fields || []).map((f) =>
    '<tr><td>' + dojoEsc(f.name) + '</td><td>' + dojoEsc(f.expected) + '</td>' +
    '<td>' + dojoEsc(f.actual) + '</td>' +
    '<td class="' + (f.ok ? 'dojo-repro-ok' : 'dojo-repro-bad') + '">' +
    (f.ok ? '通过' : '不符') + '</td></tr>').join('');
  result.innerHTML =
    '<div class="dojo-verdict ' + (out.passed ? 'ok' : 'bad') + '">' +
    (out.passed ? '复现通过' : '还没对上') +
    '<span class="dojo-verdict-note">' + out.elapsed_ms + ' ms</span></div>' +
    (out.error ? '<div class="dojo-msg err">' + dojoEsc(out.error) + '</div>' : '') +
    (rows ? '<table class="dojo-assess-table"><thead><tr><th>字段</th><th>论文值</th>' +
      '<th>你的结果</th><th>判定</th></tr></thead><tbody>' + rows + '</tbody></table>' : '') +
    (out.stdout ? '<details class="dojo-repro-out"><summary>stdout</summary><pre>' +
      dojoEsc(out.stdout) + '</pre></details>' : '') +
    (out.stderr ? '<details class="dojo-repro-out" open><summary>stderr</summary><pre>' +
      dojoEsc(out.stderr) + '</pre></details>' : '');
}

/* ---------------- 复现包编辑器 ---------------- */

function dojoCloseReproEditor() {
  const layer = document.querySelector('.dojo-repro-editor');
  if (layer) layer.remove();
  dojoState.reproEditor = null;
}

function dojoOpenReproEditor() {
  dojoClosePackMenu();
  dojoCloseReproEditor();
  const layer = document.createElement('div');
  layer.className = 'dojo-repro-editor';
  layer.innerHTML = '<div class="dojo-import-card">' +
    '<div class="dojo-import-head"><div>' +
    '<div class="dojo-import-title">新建复现包</div>' +
    '<div class="dojo-import-sub">保存前会用参考解跑一遍；跑不出目标值就不允许保存。</div>' +
    '</div><button type="button" class="dojo-import-x" id="dre-x">✕</button></div>' +
    '<div class="dojo-import-body" id="dre-body"></div>' +
    '<div class="dojo-import-foot">' +
    '<span class="dojo-imp-adopted" id="dre-msg"></span>' +
    '<button type="button" class="dojo-btn" id="dre-cancel">取消</button>' +
    '<button type="button" class="dojo-btn primary" id="dre-save">保存并自检</button>' +
    '</div></div>';
  document.body.appendChild(layer);
  dojoState.reproEditor = {
    title: '',
    source: '',
    question: '',
    files: [{ name: 'data.csv', content: '' }],
    targets: [{ name: 'value', value: '0', tolerance: '0.01' }],
    starter: '# 读取数据，把结果打印成一行 JSON\n',
    reference: '',
    limit: '10',
  };
  layer.querySelector('#dre-x').onclick = dojoCloseReproEditor;
  layer.querySelector('#dre-cancel').onclick = dojoCloseReproEditor;
  layer.querySelector('#dre-save').onclick = () => dojoSaveReproEditor();
  layer.onclick = (e) => { if (e.target === layer) dojoCloseReproEditor(); };
  layer.addEventListener('keydown', (e) => { if (e.key === 'Escape') dojoCloseReproEditor(); });
  dojoRenderReproEditor();
  layer.querySelector('#dre-title').focus();
}

function dojoReproEditorSync() {
  const s = dojoState.reproEditor;
  if (!s || !document.querySelector('#dre-body')) return;
  const q = (sel) => document.querySelector(sel);
  s.title = q('#dre-title').value;
  s.source = q('#dre-source').value;
  s.question = q('#dre-question').value;
  s.starter = q('#dre-starter').value;
  s.reference = q('#dre-reference').value;
  s.limit = q('#dre-limit').value;
  s.files = Array.from(document.querySelectorAll('.dre-file')).map((row) => ({
    name: row.querySelector('.dre-file-name').value,
    content: row.querySelector('.dre-file-content').value,
  }));
  s.targets = Array.from(document.querySelectorAll('.dre-target')).map((row) => ({
    name: row.querySelector('.dre-target-name').value,
    value: row.querySelector('.dre-target-value').value,
    tolerance: row.querySelector('.dre-target-tol').value,
  }));
}

function dojoRenderReproEditor() {
  const s = dojoState.reproEditor;
  const body = document.querySelector('#dre-body');
  if (!s || !body) return;
  const esc = dojoEsc;
  const fileRows = s.files.map((f, i) =>
    '<div class="dre-file">' +
    '<div class="dre-row">' +
    '<input class="dojo-input dre-file-name" value="' + esc(f.name) + '" placeholder="data.csv">' +
    '<button type="button" class="dojo-btn dre-remove" data-kind="file" data-idx="' + i + '">移除</button>' +
    '</div>' +
    '<textarea class="dojo-repro-code dre-file-content" spellcheck="false" ' +
    'placeholder="粘贴文件内容（CSV / TXT / JSON / MD）">' + esc(f.content) + '</textarea>' +
    '</div>').join('');
  const targetRows = s.targets.map((t, i) =>
    '<div class="dre-target">' +
    '<input class="dojo-input dre-target-name" value="' + esc(t.name) + '" placeholder="slope">' +
    '<input class="dojo-input dre-target-value" value="' + esc(t.value) + '" placeholder="2.003">' +
    '<input class="dojo-input dre-target-tol" value="' + esc(t.tolerance) + '" placeholder="0.01">' +
    '<button type="button" class="dojo-btn dre-remove" data-kind="target" data-idx="' + i + '">移除</button>' +
    '</div>').join('');

  body.innerHTML =
    '<div class="dre-row">' +
    '<input class="dojo-input" id="dre-title" maxlength="60" placeholder="标题，例如：复现最小二乘斜率" value="' + esc(s.title) + '">' +
    '<input class="dojo-input" id="dre-source" maxlength="120" placeholder="来源（可选），例如：论文表 1" value="' + esc(s.source) + '">' +
    '</div>' +
    '<textarea class="dojo-textarea" id="dre-question" maxlength="1000" ' +
    'placeholder="任务描述：要算什么、输出什么 JSON">' + esc(s.question) + '</textarea>' +
    '<div class="dre-section">数据文件</div>' + fileRows +
    '<div class="dre-hint">支持 .csv / .txt / .tsv / .json / .md；文件名会成为代码里的相对路径。</div>' +
    '<div class="dojo-actions"><button type="button" class="dojo-btn" id="dre-add-file">＋ 数据文件</button></div>' +
    '<div class="dre-section">目标字段（论文报告值）</div>' + targetRows +
    '<div class="dojo-actions"><button type="button" class="dojo-btn" id="dre-add-target">＋ 目标字段</button></div>' +
    '<div class="dre-section">starter（给用户的半截代码）</div>' +
    '<textarea class="dojo-repro-code" id="dre-starter" spellcheck="false">' + esc(s.starter) + '</textarea>' +
    '<div class="dre-section">参考解（只用于自检，不返回训练界面）</div>' +
    '<textarea class="dojo-repro-code" id="dre-reference" spellcheck="false" ' +
    'placeholder="必须能跑出上面的目标值">' + esc(s.reference) + '</textarea>' +
    '<div class="dre-row"><span class="dre-hint">超时（秒）</span>' +
    '<input class="dojo-input dre-limit" id="dre-limit" value="' + esc(s.limit) + '"></div>';

  body.querySelector('#dre-add-file').onclick = () => {
    dojoReproEditorSync();
    const n = dojoState.reproEditor.files.length + 1;
    dojoState.reproEditor.files.push({ name: 'data' + n + '.csv', content: '' });
    dojoRenderReproEditor();
  };
  body.querySelector('#dre-add-target').onclick = () => {
    dojoReproEditorSync();
    const n = dojoState.reproEditor.targets.length + 1;
    dojoState.reproEditor.targets.push({ name: 'value' + n, value: '0', tolerance: '0.01' });
    dojoRenderReproEditor();
  };
  body.querySelectorAll('.dre-remove').forEach((btn) => {
    btn.onclick = () => {
      dojoReproEditorSync();
      const i = Number(btn.dataset.idx);
      if (btn.dataset.kind === 'file') dojoState.reproEditor.files.splice(i, 1);
      else dojoState.reproEditor.targets.splice(i, 1);
      if (!dojoState.reproEditor.files.length) {
        dojoState.reproEditor.files.push({ name: 'data.csv', content: '' });
      }
      if (!dojoState.reproEditor.targets.length) {
        dojoState.reproEditor.targets.push({ name: 'value', value: '0', tolerance: '0.01' });
      }
      dojoRenderReproEditor();
    };
  });
}

async function dojoSaveReproEditor() {
  dojoReproEditorSync();
  const s = dojoState.reproEditor;
  if (!s) return;
  const msg = document.querySelector('#dre-msg');
  const save = document.querySelector('#dre-save');
  const expected = {};
  s.targets.forEach((t) => {
    const key = t.name.trim();
    if (!key) return;
    expected[key] = {
      value: Number(t.value),
      tolerance: Number(t.tolerance) || 1e-6,
    };
  });
  const payload = {
    title: s.title.trim(),
    source: s.source.trim(),
    question: s.question.trim(),
    starter: s.starter,
    reference: s.reference,
    time_limit: Number(s.limit) || 10,
    data: s.files
      .filter((f) => f.name.trim() && f.content.trim())
      .map((f) => ({ name: f.name.trim(), content: f.content })),
    expected: expected,
  };
  save.disabled = true;
  save.textContent = '自检并保存…';
  msg.textContent = '正在用参考解自检，可能要几秒…';
  msg.classList.remove('err');
  try {
    const out = await api('/api/hall/dojo/repro/create', payload);
    dojoCloseReproEditor();
    const r = await api('/api/hall/dojo/repro/list', {});
    dojoState.repro = (r && r.packs) || [];
    toast('已保存复现包：' + out.title);
    await dojoOpenRepro(out.id);
  } catch (e) {
    msg.textContent = (e && e.message) || String(e);
    msg.classList.add('err');
    save.disabled = false;
    save.textContent = '保存并自检';
  }
}

/* ---------------- 场景切换与个人包管理 ---------------- */

function dojoClosePackMenu() {
  const m = dojoState.el.packMenu;
  if (m) m.classList.add('hidden');
}

function dojoTogglePackMenu() {
  const m = dojoState.el.packMenu;
  if (!m) return;
  if (!m.classList.contains('hidden')) {
    m.classList.add('hidden');
    return;
  }
  dojoRenderPackMenu();
  m.classList.remove('hidden');
}

function dojoPackMsg(text, err) {
  const m = dojoState.el.packMenu;
  const el = m && m.querySelector('#dojo-pack-msg');
  if (!el) {
    if (text) toast(text, err ? 'error' : undefined);
    return;
  }
  el.textContent = text || '';
  el.classList.toggle('err', !!err);
}

function dojoRenderPackMenu() {
  const m = dojoState.el.packMenu;
  if (!m) return;
  const cur = dojoState.pack;
  const packs = dojoState.packs || [];
  const items = packs.map((p) => {
    const tag = p.grade === 'official' ? '内置' : (p.grade === 'verified' ? '已验证' : '个人');
    return '<button type="button" class="dojo-pack-item' +
      (cur && p.id === cur.id ? ' current' : '') + '" data-pack="' + dojoEsc(p.id) + '">' +
      '<span class="dojo-pack-name">' + dojoEsc(p.name) + '</span>' +
      '<span class="dojo-pack-meta">' + p.unit_count + ' 单元 · ' + tag + '</span></button>';
  }).join('');
  // 内置包只允许导出；个人包才有重命名 / 删除
  const personal = cur && cur.grade !== 'official' && cur.grade !== 'verified';
  m.innerHTML = '<div class="dojo-pack-section">基础诊断 · 场景包</div>' +
    '<div class="dojo-pack-list">' + (items || '<div class="dojo-empty">还没有场景包</div>') + '</div>' +
    '<div class="dojo-pack-ops">' +
    '<label class="dojo-btn dojo-pack-op">导入 JSON<input type="file" id="dojo-pack-file" accept=".json,application/json" hidden></label>' +
    '<button type="button" class="dojo-btn dojo-pack-op" data-op="export">导出当前</button>' +
    '<button type="button" class="dojo-btn dojo-pack-op" data-op="map">知识地图</button>' +
    (personal ? '<button type="button" class="dojo-btn dojo-pack-op" data-op="rename">重命名</button>' +
      '<button type="button" class="dojo-btn dojo-pack-op" data-op="delete">删除</button>' : '') +
    '</div>' +
    (cur && cur.assessment
      ? '<div class="dojo-pack-ops">' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-assess="pre">前测 ' + cur.assessment.pre + ' 题</button>' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-assess="post">后测 ' + cur.assessment.post + ' 题</button>' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-assess="report">测评报告</button>' +
        '</div>'
      : '') +
    (dojoState.repro && dojoState.repro.length
      ? '<div class="dojo-pack-ops">' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-repro="open">复现实验（' +
        dojoState.repro.length + '）</button>' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-repro="new">＋ 新建复现包</button></div>'
      : '<div class="dojo-pack-ops">' +
        '<button type="button" class="dojo-btn dojo-pack-op" data-repro="new">＋ 新建复现包</button></div>') +
    (dojoState.cases && dojoState.cases.length
      ? '<div class="dojo-case-block"><div class="dojo-pack-section">完整建模 · CUMCM 改编</div>' +
        dojoState.cases.map((c) =>
          '<button type="button" class="dojo-case-item" data-case="' + dojoEsc(c.id) + '">' +
          '<span class="dojo-case-name">' + dojoEsc(c.title) + '</span>' +
          '<span class="dojo-case-meta">' + c.total + ' 步 · 约 ' + c.minutes + ' 分钟</span></button>').join('') +
        '</div>'
      : '') +
    '<div class="dojo-pack-msg" id="dojo-pack-msg"></div>';

  m.querySelectorAll('.dojo-pack-item').forEach((btn) => {
    btn.onclick = () => dojoSwitchPack(btn.dataset.pack);
  });
  const file = m.querySelector('#dojo-pack-file');
  if (file) file.onchange = () => dojoImportPackFile(file.files && file.files[0]);
  m.querySelectorAll('[data-op]').forEach((btn) => {
    if (btn.dataset.op === 'export') btn.onclick = () => dojoExportPack();
    if (btn.dataset.op === 'map') btn.onclick = () => dojoOpenMap();
    if (btn.dataset.op === 'rename') btn.onclick = () => dojoRenamePack();
    if (btn.dataset.op === 'delete') btn.onclick = () => dojoDeletePack(btn);
  });
  m.querySelectorAll('[data-case]').forEach((btn) => {
    btn.onclick = () => dojoOpenCase(btn.dataset.case);
  });
  m.querySelectorAll('[data-assess]').forEach((btn) => {
    btn.onclick = () => {
      const kind = btn.dataset.assess;
      if (kind === 'report') dojoShowAssessReport();
      else dojoStartAssess(kind);
    };
  });
  m.querySelectorAll('[data-repro]').forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.repro === 'new') dojoOpenReproEditor();
      else dojoOpenRepro();
    };
  });
}

async function dojoSwitchPack(packId) {
  const p = (dojoState.packs || []).find((x) => x.id === packId);
  if (!p) return;
  if (dojoState.reproPack) dojoExitRepro();
  if (dojoState.assess) dojoAssessHide();
  if (dojoState.review) dojoExitReview();
  if (dojoState.caseSession) {
    dojoState.caseSession = null;
    dojoState.lastCaseOut = null;
  }
  dojoCloseMap();
  if (dojoState.pack && p.id === dojoState.pack.id) {
    dojoClosePackMenu();
    return;
  }
  dojoState.store.pack = p.id;
  dojoState.pack = p;
  const prog = Number((dojoState.store.progress || {})[p.id] || 0);
  dojoState.index = Math.min(prog, p.unit_count);
  dojoSetProgress(dojoState.index);
  dojoSaveStore();
  dojoClosePackMenu();
  if (dojoState.index >= p.unit_count) {
    dojoRenderTop();
    dojoRenderSide();
    dojoShowDone();
    return;
  }
  try {
    await dojoStart(p.id, (p.unit_index[dojoState.index] || {}).id);
  } catch (e) {
    toastErr('切换场景', e);
  }
}

function dojoRenamePack() {
  const m = dojoState.el.packMenu;
  const cur = dojoState.pack;
  if (!m || !cur) return;
  const row = m.querySelector('.dojo-pack-item.current');
  if (!row) return;
  const input = document.createElement('input');
  input.className = 'dojo-input dojo-pack-rename';
  input.value = cur.name;
  input.maxLength = 40;
  row.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const name = input.value.trim();
    if (!name || name === cur.name) {
      dojoRenderPackMenu();
      return;
    }
    try {
      const out = await api('/api/hall/dojo/packs/rename', { pack_id: cur.id, name: name });
      dojoState.packs = out.packs || [];
      dojoState.pack = (out.packs || []).find((p) => p.id === cur.id) || dojoState.pack;
      dojoRenderTop();
      dojoRenderPackMenu();
    } catch (e) {
      dojoPackMsg((e && e.message) || String(e), true);
    }
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commit();
    } else if (e.key === 'Escape') {
      dojoRenderPackMenu();
    }
  };
}

async function dojoDeletePack(btn) {
  const cur = dojoState.pack;
  if (!cur) return;
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = '确认删除';
    setTimeout(() => {
      if (btn.isConnected && btn.dataset.armed === '1') {
        btn.dataset.armed = '';
        btn.textContent = '删除';
      }
    }, 6000);
    return;
  }
  try {
    const out = await api('/api/hall/dojo/packs/delete', { pack_id: cur.id });
    dojoState.packs = out.packs || [];
    const store = dojoState.store;
    if (store.progress) delete store.progress[cur.id];
    store.cards = (store.cards || []).filter((c) => c.pack !== cur.id);
    Object.keys(store.answered || {}).forEach((k) => {
      if (k.indexOf(cur.id + ':') === 0) delete store.answered[k];
    });
    store.history = (store.history || []).filter((h) => h.pack !== cur.id);
    store.pack = '';
    store.index = 0;
    dojoSaveStore();
    dojoClosePackMenu();
    toast('已删除场景：' + cur.name);
    dojoState.pack = null;
    await dojoBoot(dojoState.gen);
  } catch (e) {
    dojoPackMsg((e && e.message) || String(e), true);
  }
}

async function dojoExportPack() {
  const cur = dojoState.pack;
  if (!cur) return;
  try {
    const out = await api('/api/hall/dojo/packs/export', { pack_id: cur.id });
    const blob = new Blob([JSON.stringify(out.pack, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (cur.name || 'dojo-pack').replace(/[\\/:*?"<>|]/g, '_') + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    dojoPackMsg('已导出：' + a.download);
  } catch (e) {
    dojoPackMsg((e && e.message) || String(e), true);
  }
}

async function dojoImportPackFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const payload = JSON.parse(text);
    const out = await api('/api/hall/dojo/packs/import', { pack: payload });
    dojoState.packs = out.packs || [];
    toast('已导入场景：' + out.pack.name);
    dojoClosePackMenu();
    await dojoSwitchPack(out.pack.id);
  } catch (e) {
    dojoPackMsg('导入失败：' + ((e && e.message) || e), true);
  }
}

/* ---------------- 导入材料（P1） ---------------- */

function dojoCloseImport() {
  const layer = document.querySelector('.dojo-import');
  if (layer) layer.remove();
  dojoState.imp = null;
}

function dojoOpenImport() {
  dojoCloseImport();
  const layer = document.createElement('div');
  layer.className = 'dojo-import';
  layer.setAttribute('role', 'dialog');
  layer.setAttribute('aria-modal', 'true');
  layer.innerHTML = [
    '<div class="dojo-import-card">',
    '  <div class="dojo-import-head">',
    '    <div>',
    '      <div class="dojo-import-title">导入材料，生成自己的场景包</div>',
    '      <div class="dojo-import-sub">材料只在本机读取；模型只给建议，答案要你逐条确认。</div>',
    '    </div>',
    '    <button type="button" class="dojo-import-x" id="dojo-imp-x" title="关闭">✕</button>',
    '  </div>',
    '  <div class="dojo-import-body">',
    '    <div class="dojo-imp-row">',
    '      <label class="dojo-btn dojo-imp-pick">选择文件（md / txt / pdf / docx）',
    '        <input type="file" id="dojo-imp-file" accept=".md,.markdown,.txt,.pdf,.docx,.xlsx" hidden>',
    '      </label>',
    '      <span class="dojo-imp-filename" id="dojo-imp-filename">未选择</span>',
    '    </div>',
    '    <div class="dojo-imp-row">',
    '      <input class="dojo-input" id="dojo-imp-name" maxlength="40" placeholder="场景名，例如：我的论文方法课">',
    '      <input class="dojo-input" id="dojo-imp-standard" maxlength="80" placeholder="对齐标准（可选），例如：论文方法复盘">',
    '      <select class="dojo-select dojo-imp-count" id="dojo-imp-count" title="生成多少条">',
    '        <option value="3">3 条</option><option value="4" selected>4 条</option><option value="6">6 条</option>',
    '      </select>',
    '    </div>',
    '    <textarea class="dojo-textarea dojo-imp-paste" id="dojo-imp-paste" placeholder="或者直接粘贴一段材料（≥80 字）"></textarea>',
    '    <div class="dojo-actions">',
    '      <button type="button" class="dojo-btn" id="dojo-imp-parse">读取文件</button>',
    '      <button type="button" class="dojo-btn" id="dojo-imp-parse-text">读取粘贴内容</button>',
    '      <button type="button" class="dojo-btn primary" id="dojo-imp-draft" disabled>生成草稿</button>',
    '    </div>',
    '    <div class="dojo-imp-preview" id="dojo-imp-preview"></div>',
    '    <div class="dojo-imp-drafts" id="dojo-imp-drafts"></div>',
    '    <div class="dojo-msg" id="dojo-imp-msg"></div>',
    '  </div>',
    '  <div class="dojo-import-foot">',
    '    <span class="dojo-imp-adopted" id="dojo-imp-adopted">已采纳 0 条</span>',
    '    <button type="button" class="dojo-btn primary" id="dojo-imp-save" disabled>保存为场景包</button>',
    '  </div>',
    '</div>',
  ].join('');
  document.body.appendChild(layer);

  dojoState.imp = { file: null, materialId: '', materialName: '', drafts: [], adopted: new Set(), busy: false };

  const q = (id) => layer.querySelector('#' + id);
  const fileInput = q('dojo-imp-file');
  const nameInput = q('dojo-imp-name');
  const standardInput = q('dojo-imp-standard');
  const countSel = q('dojo-imp-count');
  const paste = q('dojo-imp-paste');
  const preview = q('dojo-imp-preview');
  const draftBox = q('dojo-imp-drafts');
  const msg = q('dojo-imp-msg');
  const parseBtn = q('dojo-imp-parse');
  const parseTextBtn = q('dojo-imp-parse-text');
  const draftBtn = q('dojo-imp-draft');
  const saveBtn = q('dojo-imp-save');
  const adoptedEl = q('dojo-imp-adopted');
  dojoState.imp.el = { nameInput, standardInput, countSel, paste, preview, draftBox, msg,
    parseBtn, parseTextBtn, draftBtn, saveBtn, adoptedEl };

  const say = (text, err) => {
    msg.textContent = text || '';
    msg.classList.toggle('err', !!err);
  };

  q('dojo-imp-x').onclick = dojoCloseImport;
  layer.onclick = (e) => { if (e.target === layer) dojoCloseImport(); };
  layer.addEventListener('keydown', (e) => { if (e.key === 'Escape') dojoCloseImport(); });

  fileInput.onchange = () => {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    if (f.size > 20 * 1024 * 1024) { say('文件太大（上限 20MB）', true); return; }
    dojoState.imp.file = f;
    q('dojo-imp-filename').textContent = f.name + ' · ' + Math.max(1, Math.round(f.size / 1024)) + ' KB';
    if (!nameInput.value.trim()) {
      nameInput.value = f.name.replace(/\.[^.]+$/, '').slice(0, 24);
    }
    say('');
  };

  parseBtn.onclick = () => dojoImpParse();
  parseTextBtn.onclick = () => dojoImpParseText();
  draftBtn.onclick = () => dojoImpDraft();
  saveBtn.onclick = () => dojoImpSave();
  nameInput.focus();
}

function dojoImpApplyParse(out) {
  const imp = dojoState.imp;
  if (!imp) return;
  imp.materialId = out.material_id;
  imp.materialName = out.name;
  imp.el.preview.innerHTML = '<div class="dojo-imp-meta">' + dojoEsc(out.name) + ' · ' + out.chars + ' 字 · ' +
    (out.segments || []).length + ' 段' + (out.warn ? ' · ' + dojoEsc(out.warn) : '') + '</div>' +
    '<pre class="dojo-imp-text">' + dojoEsc(out.preview) + '</pre>';
  imp.el.draftBtn.disabled = false;
}

async function dojoImpParseText() {
  const imp = dojoState.imp;
  if (!imp) return;
  const text = imp.el.paste.value.trim();
  if (text.length < 80) {
    imp.el.msg.textContent = '至少粘贴 80 字，才能生成有依据的判断题';
    imp.el.msg.classList.add('err');
    return;
  }
  const btn = imp.el.parseTextBtn;
  btn.disabled = true;
  btn.textContent = '读取中…';
  imp.el.msg.textContent = '';
  imp.el.msg.classList.remove('err');
  try {
    const out = await api('/api/hall/dojo/import/parse', { name: '粘贴文本', text: text });
    dojoImpApplyParse(out);
  } catch (e) {
    imp.el.msg.textContent = (e && e.message) || String(e);
    imp.el.msg.classList.add('err');
  } finally {
    btn.disabled = false;
    btn.textContent = '读取粘贴内容';
  }
}

async function dojoImpParse() {
  const imp = dojoState.imp;
  if (!imp) return;
  const { msg, parseBtn } = imp.el;
  if (!imp.file) { msg.textContent = '先选一个文件'; msg.classList.add('err'); return; }
  imp.busy = true;
  parseBtn.disabled = true;
  parseBtn.textContent = '读取中…';
  msg.textContent = '';
  msg.classList.remove('err');
  try {
    const b64 = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result || '').split(',')[1] || '');
      r.onerror = () => reject(new Error('读取文件失败'));
      r.readAsDataURL(imp.file);
    });
    const out = await api('/api/hall/dojo/import/parse', { name: imp.file.name, data: b64 });
    dojoImpApplyParse(out);
  } catch (e) {
    msg.textContent = (e && e.message) || String(e);
    msg.classList.add('err');
  } finally {
    imp.busy = false;
    parseBtn.disabled = false;
    parseBtn.textContent = '读取材料';
  }
}

async function dojoImpDraft() {
  const imp = dojoState.imp;
  if (!imp || !imp.materialId) return;
  const { msg, draftBtn, preview } = imp.el;
  imp.busy = true;
  draftBtn.disabled = true;
  draftBtn.textContent = '生成中…（可能要半分钟）';
  msg.textContent = '';
  msg.classList.remove('err');
  try {
    const out = await api('/api/hall/dojo/import/draft', {
      material_id: imp.materialId,
      count: Number(imp.el.countSel.value) || 4,
      standard: imp.el.standardInput.value.trim(),
      dimensions: [],
    });
    imp.drafts = out.drafts || [];
    imp.adopted = new Set();
    dojoImpRenderDrafts(out.rejected || []);
    if (!imp.drafts.length) {
      msg.textContent = '没有生成可用单元：' +
        (out.rejected || []).map((r) => '第 ' + r.index + ' 条 ' + r.reason).join('；');
      msg.classList.add('err');
    }
    void preview;
  } catch (e) {
    msg.textContent = (e && e.message) || String(e);
    msg.classList.add('err');
  } finally {
    imp.busy = false;
    draftBtn.disabled = false;
    draftBtn.textContent = '重新生成';
  }
}

function dojoImpRenderDrafts(rejected) {
  const imp = dojoState.imp;
  const box = imp.el.draftBox;
  if (!imp.drafts.length) {
    box.innerHTML = rejected.length
      ? '<div class="dojo-imp-reject">被丢弃的 ' + rejected.length + ' 条：' +
        rejected.map((r) => '#' + r.index + ' ' + dojoEsc(r.reason)).join('；') + '</div>'
      : '';
    dojoImpRefreshAdopted();
    return;
  }
  box.innerHTML = imp.drafts.map((d, i) => {
    const opts = (d.payload && d.payload.options) || [];
    const segs = (d.payload && d.payload.segments) || [];
    const suggest = d.expert && d.expert.suggested;
    let answer = '';
    if (opts.length) {
      answer = '<div class="dojo-imp-options">' + opts.map((o) => {
        const hit = Array.isArray(suggest) ? suggest.indexOf(o.key) >= 0 : suggest === o.key;
        return '<div class="dojo-imp-opt' + (hit ? ' hit' : '') + '"><b>' + dojoEsc(o.key) + '</b>' +
          dojoEsc(o.text) + (hit ? '<span class="dojo-imp-tag">建议</span>' : '') + '</div>';
      }).join('') + '</div>';
    } else if (segs.length) {
      answer = '<div class="dojo-imp-options">' + segs.map((s) => {
        const hit = (suggest || []).indexOf(s.id) >= 0;
        return '<div class="dojo-imp-opt' + (hit ? ' hit' : '') + '">' + dojoEsc(s.text) +
          (hit ? '<span class="dojo-imp-tag">建议</span>' : '') + '</div>';
      }).join('') + '</div>';
    } else if (d.expert && d.expert.rubric) {
      answer = '<div class="dojo-imp-rubric">要点：' +
        d.expert.rubric.map((r) => dojoEsc(r.text)).join('；') + '</div>';
    }
    return '<div class="dojo-imp-draft" data-idx="' + i + '">' +
      '<div class="dojo-imp-draft-head">' +
      '<span class="dojo-chip ability">' + dojoEsc(d.ability_name || DOJO_ABILITY_NAMES[d.ability] || d.ability) + '</span>' +
      '<span class="dojo-chip">' + dojoEsc(dojoPrimitiveName(d.primitive)) + '</span>' +
      '<span class="dojo-chip level">' + dojoEsc(d.confidence || 'medium') + '</span>' +
      '</div>' +
      '<div class="dojo-imp-situation">' + dojoEsc((d.payload && d.payload.situation) || '') + '</div>' +
      answer +
      '<div class="dojo-imp-quote">原文：' + dojoEsc((d.expert && d.expert.source_quote) || '') + '</div>' +
      '<div class="dojo-imp-actions">' +
      '<button type="button" class="dojo-btn" data-act="adopt">采纳</button>' +
      '<button type="button" class="dojo-btn" data-act="drop">丢弃</button>' +
      '</div></div>';
  }).join('');

  box.querySelectorAll('.dojo-imp-draft').forEach((card) => {
    const idx = Number(card.dataset.idx);
    card.querySelector('[data-act="adopt"]').onclick = () => dojoImpAdopt(idx, true);
    card.querySelector('[data-act="drop"]').onclick = () => dojoImpAdopt(idx, false);
  });
  dojoImpRefreshAdopted();
}

function dojoPrimitiveName(p) {
  return ({ single_choice: '单选', multi_select: '多选', annotate: '标关键句', short_text: '一句话理由' })[p] || p;
}

function dojoImpAdopt(idx, adopt) {
  const imp = dojoState.imp;
  if (!imp) return;
  if (adopt) imp.adopted.add(idx); else imp.adopted.delete(idx);
  const card = imp.el.draftBox.querySelector('.dojo-imp-draft[data-idx="' + idx + '"]');
  if (card) {
    card.classList.toggle('adopted', adopt);
    const a = card.querySelector('[data-act="adopt"]');
    const d = card.querySelector('[data-act="drop"]');
    if (a) a.textContent = adopt ? '已采纳' : '采纳';
    if (d) d.textContent = adopt ? '取消' : '丢弃';
  }
  dojoImpRefreshAdopted();
}

function dojoImpRefreshAdopted() {
  const imp = dojoState.imp;
  if (!imp) return;
  const n = imp.adopted.size;
  imp.el.adoptedEl.textContent = '已采纳 ' + n + ' 条';
  imp.el.saveBtn.disabled = n === 0;
}

async function dojoImpSave() {
  const imp = dojoState.imp;
  if (!imp || !imp.adopted.size) return;
  const name = imp.el.nameInput.value.trim();
  if (!name) {
    imp.el.msg.textContent = '先给这个场景起个名字';
    imp.el.msg.classList.add('err');
    imp.el.nameInput.focus();
    return;
  }
  const units = Array.from(imp.adopted).sort((a, b) => a - b).map((i) => {
    const d = imp.drafts[i];
    return Object.assign({}, d, { confirmed: true });
  });
  imp.el.saveBtn.disabled = true;
  imp.el.saveBtn.textContent = '保存中…';
  try {
    const out = await api('/api/hall/dojo/import/commit', {
      name: name,
      units: units,
      standard: imp.el.standardInput.value.trim(),
      dimensions: [],
      material_name: imp.materialName,
    });
    dojoCloseImport();
    dojoState.store.pack = out.pack_id;
    if (!dojoState.store.progress) dojoState.store.progress = {};
    dojoState.store.progress[out.pack_id] = 0;
    dojoState.store.index = 0;
    dojoSaveStore();
    toast('已保存场景包：' + name + '（' + out.unit_count + ' 个单元）');
    await dojoBoot(dojoState.gen);
  } catch (e) {
    imp.el.msg.textContent = (e && e.message) || String(e);
    imp.el.msg.classList.add('err');
    imp.el.saveBtn.disabled = false;
    imp.el.saveBtn.textContent = '保存为场景包';
  }
}

/* ---------------- 注册 ---------------- */

/* 大厅首页渲染时道场还没 mount，只能直接读本地存档来显示“继续上次” */
function dojoHallResearch() {
  try {
    const raw = JSON.parse(localStorage.getItem(DOJO_RESEARCH_KEY) || 'null');
    return raw && typeof raw === 'object' && raw.sessionId ? raw : null;
  } catch (e) {
    return null;
  }
}

function dojoHallMeta() {
  const r = dojoHallResearch();
  if (r) {
    return '继续研究：' + (r.title || r.inquiryId) + ' · 第 ' +
      Math.min(Number(r.index || 0) + 1, Number(r.total || 0) || 1) + ' / ' +
      (r.total || '?') + ' 幕';
  }
  return '深度研究 · 本科 7 科 + 数学建模 · 判断幕计分';
}

function dojoHallCta() {
  return dojoHallResearch() ? '继续研究 ›' : '开始研究 ›';
}

hallRegister({
  id: 'dojo',
  name: '研究道场',
  icon: 'i-book',
  tag: '研究',
  desc: '一个任务只研究一个问题：看它怎么来的、构造反例、重建证明，最后落到考试。',
  meta: dojoHallMeta,
  cta: dojoHallCta,
  mount: dojoMount,
  unmount: dojoUnmount,
});
