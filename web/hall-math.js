/* ============================================================
   数学竞技场 —— 功能大厅的数学学习版「图寻」
   ------------------------------------------------------------
   一局 5 题：3 道计算 + 1 道真假判断 + 1 道反例选择，四科轮换。
   题目与答案都由后端 app/math_arena.py 生成 / 校验，题面接口不下发答案，
   只有提交后才返回对错、解析与三位 AI 的作答理由。

   学习记录全部存在 localStorage：
     - 段位（累计 XP，决定下一局 AI 的强度，形成「自适应陪练」）
     - 各科掌握度（做对 / 做过）
     - 错题本（题干 + 正确答案 + 解析）
     - 连胜与最高分
   ============================================================ */
'use strict';

const MATH_KEY = 'gm.math.arena.v4';
const MATH_KEY_V3 = 'gm.math.arena.v3';
const MATH_KEY_V2 = 'gm.math.arena.v2';
const MATH_KEY_V1 = 'gm.math.arena.v1';
const MATH_KEEP_KEY = 'gm.math.arena.keep.v4';       // 旧档保留一份，迁移失败时可回退
const MATH_HISTORY_LIMIT = 40;
const MATH_AI_COUNT_KEY = 'gm.math.arena.ai_count.v1';
const MATH_COURSES_KEY = 'gm.math.arena.courses.v3';
const MATH_SUBJECTS_KEY_V2 = 'gm.math.arena.subjects.v2';
const MATH_EXAM_PREFS_KEY = 'gm.math.arena.exam.prefs.v1';

/* 大学科口径：用于「总计」掌握度，保持旧数据不丢 */
const MATH_SUBJECTS = {
  analysis: '数学分析',
  algebra: '高等代数',
  number: '初等数论',
  abstract: '抽象代数',
  discrete: '离散数学',
  probability: '概率论',
  statistics: '数理统计',
  ode: '常微分方程',
  complex: '复变函数',
  geometry: '解析几何',
  numerical: '数值分析',
  modeling: '数学建模',
};
const MATH_SUBJECT_ORDER = Object.keys(MATH_SUBJECTS);

/* 选课粒度：数学分析拆 3 学期、高等代数拆 2 学期 */
const MATH_COURSES = {
  analysis1: '数学分析（一）',
  analysis2: '数学分析（二）',
  analysis3: '数学分析（三）',
  algebra1: '高等代数（一）',
  algebra2: '高等代数（二）',
  number: '初等数论',
  abstract: '抽象代数',
  discrete: '离散数学',
  probability: '概率论',
  statistics: '数理统计',
  ode: '常微分方程',
  complex: '复变函数',
  geometry: '解析几何',
  numerical: '数值分析',
  modeling: '数学建模',
};
const MATH_COURSE_ORDER = Object.keys(MATH_COURSES);
const MATH_COURSE_PARENT = {
  analysis1: 'analysis', analysis2: 'analysis', analysis3: 'analysis',
  algebra1: 'algebra', algebra2: 'algebra',
};
MATH_COURSE_ORDER.forEach((c) => { if (!MATH_COURSE_PARENT[c]) MATH_COURSE_PARENT[c] = c; });
const MATH_COURSE_SHORT = {
  analysis1: '数分一', analysis2: '数分二', analysis3: '数分三',
  algebra1: '高代一', algebra2: '高代二',
};

/* 选课面板按学习路径分组 */
const MATH_GROUPS = [
  { name: '数学分析', ids: ['analysis1', 'analysis2', 'analysis3'] },
  { name: '高等代数', ids: ['algebra1', 'algebra2'] },
  { name: '其他核心', ids: ['number', 'abstract', 'discrete'] },
  { name: '概率统计', ids: ['probability', 'statistics'] },
  { name: '方程与复分析', ids: ['ode', 'complex'] },
  { name: '几何与计算', ids: ['geometry', 'numerical'] },
  { name: '应用建模', ids: ['modeling'] },
];

function mathCourseOf(subject, skill) {
  if (subject === 'analysis') {
    if (['数列极限', '连续与可导', '导数应用', '极限', '导数'].indexOf(skill) >= 0) {
      return 'analysis1';
    }
    if (['级数', '黎曼积分', '定积分', '泰勒展开'].indexOf(skill) >= 0) {
      return 'analysis2';
    }
    return 'analysis3';
  }
  if (subject === 'algebra') {
    if (['矩阵运算', '行列式', '线性方程组', '矩阵的秩', '零空间', '多项式']
      .indexOf(skill) >= 0) {
      return 'algebra1';
    }
    return 'algebra2';
  }
  return MATH_COURSES[subject] ? subject : null;
}

/* 段位阈值：累计 XP 达到阈值即升段。AI 的准确率随段位 1→5 提高。 */
const MATH_LEVEL_XP = [0, 120, 320, 640, 1100];
const MATH_LEVEL_NAMES = ['入门', '熟练', '进阶', '精通', '大师'];
const MATH_RANK_BONUS = [30, 15, 5, 0];

/* Leitner 间隔（天）：box 0 表示刚错/待复习，box 4 表示已长期掌握 */
const MATH_REVIEW_DAYS = [0, 1, 3, 7, 15];
const MATH_DAY_MS = 24 * 3600 * 1000;
const MATH_REVIEW_LIMIT = 10;
const MATH_EXAM_LIMIT = 30;

const math = {
  gen: 0,               // 挂载代次：切走后的异步响应一律丢弃
  el: {},
  store: null,
  round: null,
  index: 0,
  q: null,              // 当前题（公开题面）
  qStart: 0,
  elapsedMs: 0,
  timer: null,
  busy: false,
  revealed: false,
  aiRows: [],
  qResults: [],         // 每题的结果，结算时写进本地存档
  result: null,
  no: 0,
  aiCount: 3,
  selected: [],         // 当前题库范围（课程 id）
  mode: 'round',        // home / round / review
  abilityMeta: null,    // 8 维名称 + 知识点映射（/api/hall/math/abilities）
  hintsShown: 0,
  lastValue: null,
  lastOut: null,
  reviewQueue: [],
  reviewIndex: 0,
  reviewCorrect: 0,
  reviewXp: 0,
  reviewResults: [],
  picker: null,
  overlay: null,        // 错题本 / 对局记录 / 笔记弹窗
  wrongFilter: { course: 'all', status: 'all' },
  exam: null,           // 期末冲刺状态
  katexTimer: null,
  keyHandler: null,
};

hallRegister({
  id: 'math',
  name: '数学竞技场',
  icon: 'i-book',
  tag: '训练',
  desc: '训练首页：快速对局、单科模拟卷、薄弱点专项；同时看知识点掌握度与 8 维过程能力。',
  meta: '15 门课程 · 模拟卷 · 过程能力 · 错题复习',
  mount: mathMount,
  unmount: mathUnmount,
});

/* ---------------- 本地存档 ---------------- */

function mathDefaultStore() {
  const subjects = {};
  MATH_SUBJECT_ORDER.forEach((k) => { subjects[k] = { seen: 0, correct: 0 }; });
  const courseStats = {};
  MATH_COURSE_ORDER.forEach((k) => { courseStats[k] = { seen: 0, correct: 0 }; });
  return {
    version: 4, xp: 0, rounds: 0, streak: 0, bestStreak: 0, bestScore: 0,
    subjects: subjects, courseStats: courseStats, wrong: [], history: [],
    exams: [], sprint: null,
  };
}

function mathReadRaw(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
}

/* v1 的错题只有可读答案，没有完整题面快照 → 标成 legacy：能看，但不能进入复习 */
function mathNormalizeWrong(items) {
  const list = Array.isArray(items) ? items.slice(0, 120) : [];
  return list.filter((w) => w && typeof w === 'object').map((w) => {
    const kind = (w.kind === 'calc' || w.kind === 'tf' || w.kind === 'counter')
      ? w.kind : null;
    const hasValue = Object.prototype.hasOwnProperty.call(w, 'answer_value');
    const course = (w.course && MATH_COURSES[w.course]) ? w.course
      : mathCourseOf(w.subject, w.skill || '');
    const rec = Object.assign({}, w, {
      kind: kind,
      course: course,
      course_name: course ? MATH_COURSES[course] : (w.subject_name || ''),
      legacy: !kind || !hasValue,
      box: Math.max(0, Math.min(MATH_REVIEW_DAYS.length - 1, Number(w.box) || 0)),
      reps: Math.max(0, Number(w.reps) || 0),
      lapses: Math.max(0, Number(w.lapses) || 0),
      due: Number(w.due) || 0,
      last: Number(w.last) || 0,
      options: Array.isArray(w.options) ? w.options : [],
      steps: Array.isArray(w.steps) ? w.steps : [],
      cause: String(w.cause || ''),
    });
    if (rec.legacy) rec.due = 0;
    return rec;
  });
}

/* 对局记录：只保存展示需要的字段，最多 40 局 */
function mathNormalizeHistory(items) {
  const list = Array.isArray(items) ? items.slice(0, MATH_HISTORY_LIMIT) : [];
  return list.filter((h) => h && typeof h === 'object').map((h) => {
    const courses = Array.isArray(h.courses)
      ? h.courses.filter((c) => MATH_COURSES[c]) : [];
    const players = Array.isArray(h.players) ? h.players.slice(0, 4).map((p) => ({
      name: String((p && p.name) || ''),
      kind: p && p.kind === 'ai' ? 'ai' : 'human',
      score: Math.max(0, Number(p && p.score) || 0),
      correct: Math.max(0, Number(p && p.correct) || 0),
      rank: Math.max(0, Number(p && p.rank) || 0),
    })) : [];
    const questions = Array.isArray(h.items) ? h.items.slice(0, 10).map((q) => ({
      course: q && MATH_COURSES[q.course] ? q.course : null,
      course_name: String((q && q.course_name) || ''),
      skill: String((q && q.skill) || ''),
      kind: String((q && q.kind) || ''),
      correct: !!(q && q.correct),
      score: Math.max(0, Number(q && q.score) || 0),
      prompt: String((q && q.prompt) || ''),
    })) : [];
    return {
      at: Math.max(0, Number(h.at) || 0),
      score: Math.max(0, Number(h.score) || 0),
      correct: Math.max(0, Number(h.correct) || 0),
      total: Math.max(0, Number(h.total) || questions.length),
      rank: Math.max(0, Number(h.rank) || 0),
      xp_gain: Math.max(0, Number(h.xp_gain) || 0),
      level: Math.max(1, Number(h.level) || 1),
      courses: courses,
      players: players,
      items: questions,
    };
  });
}

function mathNormalizeExams(items) {
  const list = Array.isArray(items) ? items.slice(0, MATH_EXAM_LIMIT) : [];
  return list.filter((e) => e && typeof e === 'object').map((e) => ({
    at: Math.max(0, Number(e.at) || 0),
    course: e.course && MATH_COURSES[e.course] ? e.course : null,
    course_name: String(e.course_name || ''),
    score: Math.max(0, Number(e.score) || 0),
    correct: Math.max(0, Number(e.correct) || 0),
    total: Math.max(0, Number(e.total) || 0),
    duration: Math.max(0, Number(e.duration) || 0),
    time_ms: Math.max(0, Number(e.time_ms) || 0),
    by_skill: (Array.isArray(e.by_skill) ? e.by_skill.slice(0, 30) : []).map((s) => ({
      skill: String((s && s.skill) || ''),
      attempts: Math.max(0, Number(s && s.attempts) || 0),
      correct: Math.max(0, Number(s && s.correct) || 0),
      accuracy: Math.max(0, Number(s && s.accuracy) || 0),
      time_ms: Math.max(0, Number(s && s.time_ms) || 0),
    })),
    questions: (Array.isArray(e.questions) ? e.questions.slice(0, 30) : []).map((q) => ({
      prompt: String((q && q.prompt) || ''),
      skill: String((q && q.skill) || ''),
      kind: String((q && q.kind) || ''),
      correct: !!(q && q.correct),
      score: Math.max(0, Number(q && q.score) || 0),
      value: q ? q.value : null,
      answer: q ? q.answer : null,
      explain: String((q && q.explain) || ''),
      cause: String((q && q.cause) || ''),
    })),
  }));
}

function mathNormalizeSprint(raw) {
  if (!raw || typeof raw !== 'object' || !raw.date) return null;
  return {
    date: String(raw.date || ''),
    tasks: (Array.isArray(raw.tasks) ? raw.tasks.slice(0, 14) : []).map((t, i) => ({
      day: Math.max(1, Number(t && t.day) || (i + 1)),
      title: String((t && t.title) || ''),
      detail: String((t && t.detail) || ''),
      done: !!(t && t.done),
    })),
    updatedAt: Math.max(0, Number(raw.updatedAt) || 0),
  };
}

function mathLoadStore() {
  const base = mathDefaultStore();
  let raw = mathReadRaw(MATH_KEY);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    // v4 不存在时依次回退 v3、v2、v1；旧统计保留在「总计」，学期小计从 0 开始
    const candidates = [mathReadRaw(MATH_KEY_V3), mathReadRaw(MATH_KEY_V2), mathReadRaw(MATH_KEY_V1)];
    for (let i = 0; i < candidates.length; i += 1) {
      const old = candidates[i];
      if (old && typeof old === 'object' && !Array.isArray(old)) {
        raw = old;
        try { localStorage.setItem(MATH_KEEP_KEY, JSON.stringify(old)); } catch (e) { /* 忽略 */ }
        break;
      }
    }
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return base;

  const store = Object.assign(base, raw);
  store.subjects = Object.assign({}, base.subjects);
  const oldSub = raw.subjects && typeof raw.subjects === 'object' ? raw.subjects : {};
  MATH_SUBJECT_ORDER.forEach((k) => {
    const s = oldSub[k] || {};
    store.subjects[k] = {
      seen: Math.max(0, Number(s.seen) || 0),
      correct: Math.max(0, Number(s.correct) || 0),
    };
  });
  store.courseStats = Object.assign({}, base.courseStats);
  const oldCourse = raw.courseStats && typeof raw.courseStats === 'object'
    ? raw.courseStats : {};
  MATH_COURSE_ORDER.forEach((k) => {
    const s = oldCourse[k] || {};
    store.courseStats[k] = {
      seen: Math.max(0, Number(s.seen) || 0),
      correct: Math.max(0, Number(s.correct) || 0),
    };
  });
  store.xp = Math.max(0, Number(store.xp) || 0);
  store.rounds = Math.max(0, Number(store.rounds) || 0);
  store.streak = Math.max(0, Number(store.streak) || 0);
  store.bestStreak = Math.max(store.streak, Number(store.bestStreak) || 0);
  store.bestScore = Math.max(0, Number(store.bestScore) || 0);
  store.wrong = mathNormalizeWrong(store.wrong);
  store.history = mathNormalizeHistory(store.history);
  store.exams = mathNormalizeExams(store.exams);
  store.sprint = mathNormalizeSprint(store.sprint);
  store.version = 4;
  try { localStorage.setItem(MATH_KEY, JSON.stringify(store)); } catch (e) { /* 隐私模式 */ }
  return store;
}

function mathSaveStore() {
  try { localStorage.setItem(MATH_KEY, JSON.stringify(math.store)); } catch (e) { /* 隐私模式 / 配额满 */ }
}

function mathLevelOf(xp) {
  let lv = 1;
  MATH_LEVEL_XP.forEach((t, i) => { if (xp >= t) lv = i + 1; });
  return Math.min(5, lv);
}

function mathNextLevelXp(level) {
  return level >= 5 ? null : MATH_LEVEL_XP[level];
}

function mathAiCountLoad() {
  try {
    const raw = localStorage.getItem(MATH_AI_COUNT_KEY);
    if (raw == null || raw === '') return 3;   // 首次进入默认 3 位，不能把 null 当成 0
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 && n <= 3 ? Math.round(n) : 3;
  } catch (e) {
    return 3;
  }
}

function mathExpandSubject(s) {
  if (s === 'analysis') return ['analysis1', 'analysis2', 'analysis3'];
  if (s === 'algebra') return ['algebra1', 'algebra2'];
  return MATH_COURSES[s] ? [s] : [];
}

function mathSelectedLoad() {
  try {
    const rawText = localStorage.getItem(MATH_COURSES_KEY);
    if (rawText != null) {
      const raw = JSON.parse(rawText);
      if (Array.isArray(raw)) {
        const out = [];
        raw.forEach((c) => {
          if (MATH_COURSES[c] && out.indexOf(c) < 0) out.push(c);
        });
        return out;
      }
    }
    // v2 的选课按大学科保存：数学分析展开成 1/2/3，高等代数展开成 1/2
    const oldText = localStorage.getItem(MATH_SUBJECTS_KEY_V2);
    if (oldText != null) {
      const old = JSON.parse(oldText);
      if (Array.isArray(old)) {
        const out = [];
        old.forEach((s) => {
          mathExpandSubject(s).forEach((c) => {
            if (out.indexOf(c) < 0) out.push(c);
          });
        });
        return out;
      }
    }
    return MATH_COURSE_ORDER.slice();   // 首次进入：全选
  } catch (e) {
    return MATH_COURSE_ORDER.slice();
  }
}

function mathSelectedSave() {
  try { localStorage.setItem(MATH_COURSES_KEY, JSON.stringify(math.selected)); } catch (e) { /* 忽略 */ }
}

/* ---------------- 错题间隔重复（Leitner） ---------------- */

function mathReviewUpdate(rec, correct, now) {
  now = now || Date.now();
  rec.legacy = false;
  rec.reps = (Number(rec.reps) || 0) + 1;
  rec.last = now;
  if (correct) {
    rec.box = Math.min(MATH_REVIEW_DAYS.length - 1, (Number(rec.box) || 0) + 1);
    rec.due = now + MATH_REVIEW_DAYS[rec.box] * MATH_DAY_MS;
  } else {
    rec.lapses = (Number(rec.lapses) || 0) + 1;
    rec.box = 0;
    rec.due = now;
  }
  return rec;
}

function mathDueRecords(now) {
  now = now || Date.now();
  return (math.store.wrong || [])
    .filter((r) => r && !r.legacy && r.due && r.due <= now)
    .sort((a, b) => a.due - b.due);
}

function mathNextDueText() {
  const now = Date.now();
  const times = (math.store.wrong || [])
    .filter((r) => r && !r.legacy && r.due > now)
    .map((r) => r.due);
  if (!times.length) return '';
  const days = Math.ceil((Math.min.apply(null, times) - now) / MATH_DAY_MS);
  return '下次复习约 ' + Math.max(1, days) + ' 天后';
}

function mathAddXp(n) {
  if (!(n > 0)) return;
  math.store.xp += n;
  mathSaveStore();
}

/* ---------------- 挂载 / 卸载 ---------------- */

function mathMount(root) {
  const gen = ++math.gen;
  math.el = { root: root };
  math.store = mathLoadStore();
  math.round = null;
  math.index = 0;
  math.q = null;
  math.result = null;
  math.qResults = [];
  math.revealed = false;
  math.busy = false;
  math.no = 0;
  math.aiCount = mathAiCountLoad();
  math.selected = mathSelectedLoad();
  math.mode = 'home';
  math.hintsShown = 0;
  math.lastValue = null;
  math.lastOut = null;
  math.reviewQueue = [];
  math.reviewIndex = 0;
  math.reviewCorrect = 0;
  math.reviewXp = 0;
  math.picker = null;
  mathCloseOverlay();
  math.wrongFilter = { course: 'all', status: 'all' };
  mathTimerStop();

  root.classList.add('math-root');
  root.innerHTML = [
    '<div class="math-top">',
    '  <div class="math-top-group">',
    '    <button type="button" class="math-pill math-pill-btn accent" id="math-home-btn">训练首页</button>',
    '    <span class="math-pill hidden" id="math-no">第 1 局</span>',
    '    <span class="math-pill hidden" id="math-mode">错题复习</span>',
    '    <span class="math-pill" id="math-level"></span>',
    '    <span class="math-pill" id="math-streak"></span>',
    '  </div>',
    '  <div class="math-top-group math-top-tools">',
    '    <button type="button" class="math-pill math-pill-btn" id="math-bank"></button>',
    '    <button type="button" class="math-pill math-pill-btn accent" id="math-exam">模拟考试</button>',
    '    <button type="button" class="math-pill math-pill-btn" id="math-review"></button>',
    '    <button type="button" class="math-pill math-pill-btn" id="math-archive">学习档案</button>',
    '  </div>',
    '  <div class="math-top-group math-top-right">',
    '    <span class="math-pill math-ai-pill"><span class="math-ai-label">AI 陪练</span>',
    '      <select id="math-ai-count" class="math-select" title="AI 对手人数（下一局生效）">',
    '      <option value="0">关闭</option><option value="1">1 位</option>',
    '      <option value="2">2 位</option><option value="3">3 位</option>',
    '    </select></span>',
    '  </div>',
    '</div>',
    '<div class="math-layout">',
    '  <div class="math-main">',
    '    <div class="math-home hidden" id="math-home"></div>',
    '    <div class="math-card" id="math-card">',
    '      <div class="math-card-meta">',
    '        <div class="math-progress" id="math-progress"></div>',
    '        <span class="math-pill math-timer" id="math-timer">0:00</span>',
    '      </div>',
    '      <div class="math-card-head" id="math-head"></div>',
    '      <div class="math-prompt" id="math-prompt"></div>',
    '      <div class="math-answer" id="math-answer"></div>',
    '      <div class="math-hints hidden" id="math-hints"></div>',
    '      <div class="math-reveal hidden" id="math-reveal"></div>',
    '      <div class="math-msg" id="math-msg"></div>',
    '      <div class="math-actions" id="math-actions"></div>',
    '    </div>',
    '    <div class="math-result hidden" id="math-result"></div>',
    '  </div>',
    '  <aside class="math-side" id="math-side"></aside>',
    '</div>',
    '<div class="math-picker hidden" id="math-picker"></div>',
  ].join('');

  ['no', 'mode', 'level', 'streak', 'bank', 'review', 'archive', 'home', 'homeBtn',
    'exam', 'aiCount', 'timer', 'progress', 'card', 'head', 'prompt', 'answer', 'hints', 'reveal',
    'msg', 'actions', 'result', 'side', 'picker']
    .forEach((k) => {
      math.el[k] = root.querySelector('#math-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()));
    });

  // 从大厅卡片进来时，滚动容器可能停在卡片位置；回到顶部再展示题目
  const scroller = root.closest('.hall-body') || document.getElementById('hall-body');
  if (scroller) scroller.scrollTop = 0;

  math.el.aiCount.value = String(math.aiCount);
  math.el.aiCount.onchange = () => {
    math.aiCount = Math.max(0, Math.min(3, Number(math.el.aiCount.value) || 0));
    try { localStorage.setItem(MATH_AI_COUNT_KEY, String(math.aiCount)); } catch (e) { /* 忽略 */ }
    toast(math.aiCount ? ('AI 陪练已设为 ' + math.aiCount + ' 位（下一局生效）')
      : 'AI 陪练已关闭（下一局生效）');
  };
  math.el.bank.onclick = () => mathOpenPicker(math.mode === 'review');
  math.el.review.onclick = () => mathStartReview();
  math.el.archive.onclick = mathOpenArchive;
  math.el.homeBtn.onclick = () => mathGoHome();
  math.el.exam.onclick = mathOpenExam;
  math.el.picker.onclick = (e) => {
    if (e.target === math.el.picker) mathClosePicker();
  };
  math.keyHandler = mathOnKey;
  document.addEventListener('keydown', math.keyHandler);

  mathRenderTop();
  mathRenderSide();
  mathLoadAbilityMeta(gen);
  if (!math.selected.length) {
    // 用户上次把题库清空了：先选课，不擅自替他抽题
    mathRenderHome();
    math.el.prompt.textContent = '先选择要练的课程，再开始试炼。';
    mathOpenPicker(false);
    return;
  }
  mathRenderHome();
}

function mathUnmount() {
  math.gen += 1;
  if (math.keyHandler) {
    document.removeEventListener('keydown', math.keyHandler);
    math.keyHandler = null;
  }
  mathTimerStop();
  mathExamStopTimer();
  mathClosePicker();
  mathCloseOverlay();
  math.exam = null;
  if (math.katexTimer) {
    clearInterval(math.katexTimer);
    math.katexTimer = null;
  }
  math.round = null;
  math.q = null;
  math.result = null;
  math.qResults = [];
  math.picker = null;
  math.abilityMeta = null;
  math.el = {};
}

/* ---------------- 训练首页 ---------------- */

const MATH_ABILITY_FALLBACK = {
  framing: '定义问题', assumption: '建立假设', method: '选择方法', condition: '判断适用性',
  evidence: '证据', robustness: '稳健性', argument: '论证', reflection: '反思迁移',
};

function mathAbilityIds() {
  if (math.abilityMeta && Array.isArray(math.abilityMeta.abilities) && math.abilityMeta.abilities.length) {
    return math.abilityMeta.abilities.map((a) => a.id);
  }
  return Object.keys(MATH_ABILITY_FALLBACK);
}

function mathAbilityName(id) {
  if (math.abilityMeta && Array.isArray(math.abilityMeta.abilities)) {
    const hit = math.abilityMeta.abilities.find((a) => a.id === id);
    if (hit) return hit.name;
  }
  return MATH_ABILITY_FALLBACK[id] || id;
}

function mathAbilityOfSkill(skill) {
  if (math.abilityMeta && math.abilityMeta.skills && math.abilityMeta.skills[skill]) {
    return math.abilityMeta.skills[skill];
  }
  return 'method';
}

/* 本地历史（对局 + 模拟卷）聚合；错题本已包含在这些记录里，不重复计 */
function mathAbilityLocal() {
  const by = {};
  const add = (skill, correct, score) => {
    const key = mathAbilityOfSkill(skill || '');
    const a = by[key] || (by[key] = { ability: key, attempts: 0, correct: 0, score: 0 });
    a.attempts += 1;
    if (correct) a.correct += 1;
    a.score += Math.max(0, Number(score) || 0);
  };
  (math.store.history || []).forEach((h) => {
    (h.items || []).forEach((it) => add(it.skill, it.correct, it.score));
  });
  (math.store.exams || []).forEach((e) => {
    (e.questions || []).forEach((q) => add(q.skill, q.correct, q.score));
  });
  return Object.keys(by).map((k) => {
    const a = by[k];
    a.accuracy = Math.round((100 * a.correct) / Math.max(1, a.attempts));
    return a;
  });
}

function mathRenderAbilityBlock(rows, note) {
  const box = document.createElement('div');
  box.className = 'math-ability';
  const title = document.createElement('div');
  title.className = 'math-panel-title';
  title.textContent = '过程能力';
  box.appendChild(title);

  const map = {};
  (rows || []).forEach((r) => { map[r.ability] = r; });
  mathAbilityIds().forEach((id) => {
    const r = map[id];
    const row = document.createElement('div');
    row.className = 'math-ability-row';
    const name = document.createElement('span');
    name.className = 'math-ability-name';
    name.textContent = mathAbilityName(id);
    const bar = document.createElement('span');
    bar.className = 'math-ability-bar';
    const fill = document.createElement('i');
    const pct = r ? Math.max(0, Math.min(100, Number(r.accuracy) || 0)) : 0;
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    const text = document.createElement('span');
    text.className = 'math-ability-text';
    if (!r || !r.attempts) {
      text.textContent = '—';
    } else if (r.attempts < 5) {
      text.textContent = r.accuracy + '% · 样本不足';
      text.title = '只答了 ' + r.attempts + ' 题，暂不适合横向比较';
    } else {
      text.textContent = r.accuracy + '% · ' + r.attempts + ' 题';
    }
    row.appendChild(name);
    row.appendChild(bar);
    row.appendChild(text);
    box.appendChild(row);
  });
  if (note) {
    const n = document.createElement('div');
    n.className = 'math-panel-note';
    n.textContent = note;
    box.appendChild(n);
  }
  return box;
}

function mathLoadAbilityMeta(gen) {
  api('/api/hall/math/abilities', {}).then((r) => {
    if (gen !== math.gen) return;
    math.abilityMeta = r;
    if (math.mode === 'home') mathRenderHome();
    else mathRenderSide();
  }).catch(() => { /* 能力层失败不影响对局 */ });
}

function mathWeakestSkill() {
  const data = mathExamDiagnosisData(0);
  const hit = (data.weak || [])[0];
  return hit || null;
}

function mathHomeLastText() {
  const last = (math.store.history || [])[0];
  const exam = (math.store.exams || [])[0];
  if (exam && (!last || (Number(exam.at) || 0) > (Number(last.at) || 0))) {
    return '模拟卷 ' + exam.score + ' 分 · ' + (exam.course_name || '');
  }
  if (last) {
    return '快速对局 ' + last.score + ' 分 · 第 ' + last.rank + ' 名';
  }
  return '还没有训练记录';
}

function mathRenderHome() {
  const home = math.el.home;
  if (!home) return;
  math.mode = 'home';
  mathTimerStop();
  math.el.card.classList.add('hidden');
  math.el.result.classList.add('hidden');
  home.classList.remove('hidden');

  const due = mathDueRecords().length;
  const wrong = (math.store.wrong || []).length;
  const weak = mathWeakestSkill();
  const last = mathHomeLastText();

  home.innerHTML =
    '<div class="math-home-hero">' +
    '<div class="math-home-title">今天练什么</div>' +
    '<div class="math-home-sub">选一条路径：快速对局 5 题，模拟卷按课程计时，专项只练最该补的点。</div>' +
    '</div>' +
    '<div class="math-home-grid">' +
    '<button type="button" class="math-home-main" id="math-home-round">' +
    '<span class="math-home-kicker">快速对局 · 5 题</span>' +
    '<b>和 AI 比一局</b>' +
    '<span>约 5 分钟 · 立刻揭晓 · 错题自动进复习</span>' +
    '<em>上次：' + mathExamEsc(last) + '</em></button>' +
    '<button type="button" class="math-home-card" id="math-home-exam">' +
    '<span class="math-home-kicker">模拟考试</span><b>单科整卷</b>' +
    '<span>按课程计时，交卷后统一批改</span></button>' +
    '<button type="button" class="math-home-card" id="math-home-drill">' +
    '<span class="math-home-kicker">薄弱点专项</span><b>' +
    (weak ? ('补：' + mathExamEsc(weak.skill)) : '只练最该补的') + '</b>' +
    '<span>5 / 10 题，不计时，带解析</span></button>' +
    '</div>' +
    '<div class="math-home-stats">' +
    '<button type="button" class="math-pill math-pill-btn" id="math-home-review">今日复习 ' + due + '</button>' +
    '<button type="button" class="math-pill math-pill-btn" id="math-home-wrong">错题 ' + wrong + '</button>' +
    '<span class="math-home-last">' + mathExamEsc(last) + '</span>' +
    '</div>' +
    '<div class="math-home-ability" id="math-home-ability"></div>' +
    '<div class="math-home-note">能力按知识点主映射聚合；样本少于 5 题只标“样本不足”，不做横向比较。</div>';

  const abilityBox = home.querySelector('#math-home-ability');
  if (abilityBox) abilityBox.appendChild(mathRenderAbilityBlock(mathAbilityLocal(), ''));
  home.querySelector('#math-home-round').onclick = () => mathNewRound();
  home.querySelector('#math-home-exam').onclick = () => mathOpenExam();
  home.querySelector('#math-home-drill').onclick = () => mathOpenDrill();
  home.querySelector('#math-home-review').onclick = () => mathStartReview();
  home.querySelector('#math-home-wrong').onclick = () => mathOpenWrongBook();

  mathRenderTop();
  mathRenderSide();
}

function mathGoHome() {
  mathTimerStop();
  math.reviewQueue = [];
  math.reviewIndex = 0;
  math.reviewResults = [];
  mathRenderHome();
}

function mathOpenDrill() {
  mathOpenExam();
  const st = mathExamState();
  const weak = mathWeakestSkill();
  st.mode = 'drill';
  st.count = 10;
  st.drillCount = 10;
  if (weak) {
    st.skill = weak.skill || '';
    if (weak.course) st.course = weak.course;
  } else {
    st.skill = '';
  }
  st.view = 'setup';
  mathExamRender();
}

function mathOpenArchive() {
  const ov = mathOpenOverlay('学习档案', '错题本、对局记录和可复制笔记。');
  const wrong = (math.store.wrong || []).length;
  const hist = (math.store.history || []).length;
  ov.body.innerHTML = '<div class="math-archive-grid">' +
    '<button type="button" class="math-archive-card" data-archive="wrong">' +
    '<b>错题本 ' + wrong + '</b><span>按 Leitner 间隔复习；可筛选、补错因、整理成笔记</span></button>' +
    '<button type="button" class="math-archive-card" data-archive="history">' +
    '<b>对局记录 ' + hist + '</b><span>每局成绩、排名与逐题明细</span></button>' +
    '<button type="button" class="math-archive-card" data-archive="notes">' +
    '<b>错题笔记</b><span>把错题按课程整理成 Markdown，可复制或下载</span></button>' +
    '</div>' +
    '<div class="math-exam-note">复习到期和错题数会同步到首页与顶部。</div>';
  ov.body.querySelectorAll('[data-archive]').forEach((btn) => {
    btn.onclick = () => {
      const kind = btn.dataset.archive;
      mathCloseOverlay();
      if (kind === 'wrong') mathOpenWrongBook();
      else if (kind === 'history') mathOpenHistory();
      else mathOpenNotes();
    };
  });
}

/* ---------------- 题库范围（选课） ---------------- */

function mathOpenPicker() {
  const box = math.el.picker;
  if (!box) return;
  math.picker = { sel: (math.selected || []).slice() };
  box.innerHTML = '';
  box.classList.remove('hidden');

  const card = document.createElement('div');
  card.className = 'math-picker-card';
  const title = document.createElement('div');
  title.className = 'math-picker-title';
  title.textContent = '选择题库范围';
  const sub = document.createElement('div');
  sub.className = 'math-picker-sub';
  sub.textContent = '只从选中的课程里抽 5 道题；至少选 1 门。选择会保存到本地。';
  card.appendChild(title);
  card.appendChild(sub);

  const groups = document.createElement('div');
  groups.className = 'math-picker-groups';
  MATH_GROUPS.forEach((g) => {
    const group = document.createElement('div');
    group.className = 'math-picker-group';
    const label = document.createElement('div');
    label.className = 'math-picker-group-name';
    label.textContent = g.name;
    group.appendChild(label);
    const list = document.createElement('div');
    list.className = 'math-picker-list';
    g.ids.forEach((id) => {
      if (!MATH_COURSES[id]) return;
      const item = document.createElement('label');
      item.className = 'math-course';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = id;
      cb.checked = math.picker.sel.indexOf(id) >= 0;
      cb.onchange = () => {
        const at = math.picker.sel.indexOf(id);
        if (cb.checked && at < 0) math.picker.sel.push(id);
        if (!cb.checked && at >= 0) math.picker.sel.splice(at, 1);
        math.selected = math.picker.sel.slice();
        mathSelectedSave();
        mathRenderTop();
        mathRenderPickerCount();
      };
      const name = document.createElement('span');
      name.textContent = MATH_COURSES[id];
      item.appendChild(cb);
      item.appendChild(name);
      list.appendChild(item);
    });
    group.appendChild(list);
    groups.appendChild(group);
  });
  card.appendChild(groups);

  const foot = document.createElement('div');
  foot.className = 'math-picker-foot';
  const count = document.createElement('span');
  count.className = 'math-picker-count';
  count.id = 'math-picker-count';
  foot.appendChild(count);
  const all = document.createElement('button');
  all.type = 'button';
  all.className = 'math-btn';
  all.textContent = '全选';
  all.onclick = () => {
    math.picker.sel = MATH_COURSE_ORDER.slice();
    card.querySelectorAll('input[type=checkbox]').forEach((cb) => { cb.checked = true; });
    math.selected = math.picker.sel.slice();
    mathSelectedSave();
    mathRenderTop();
    mathRenderPickerCount();
  };
  const none = document.createElement('button');
  none.type = 'button';
  none.className = 'math-btn';
  none.textContent = '清空';
  none.onclick = () => {
    math.picker.sel = [];
    card.querySelectorAll('input[type=checkbox]').forEach((cb) => { cb.checked = false; });
    math.selected = [];
    mathSelectedSave();
    mathRenderTop();
    mathRenderPickerCount();
  };
  foot.appendChild(all);
  foot.appendChild(none);
  const spacer = document.createElement('span');
  spacer.className = 'math-picker-spacer';
  foot.appendChild(spacer);
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'math-btn ghost';
  cancel.textContent = '取消';
  cancel.onclick = mathClosePicker;
  const start = document.createElement('button');
  start.type = 'button';
  start.className = 'math-btn primary';
  start.textContent = '开始试炼';
  start.id = 'math-picker-start';
  start.onclick = mathStartFromPicker;
  foot.appendChild(cancel);
  foot.appendChild(start);
  card.appendChild(foot);
  box.appendChild(card);
  mathRenderPickerCount();
}

function mathRenderPickerCount() {
  const el = document.getElementById('math-picker-count');
  const btn = document.getElementById('math-picker-start');
  if (!el || !math.picker) return;
  const n = math.picker.sel.length;
  el.textContent = '已选 ' + n + ' / ' + MATH_COURSE_ORDER.length + ' 门';
  if (btn) btn.disabled = n === 0;
}

function mathStartFromPicker() {
  if (!math.picker || !math.picker.sel.length) {
    toast('至少选择 1 门课程', 'error');
    return;
  }
  math.selected = math.picker.sel.slice();
  mathSelectedSave();
  mathClosePicker();
  if (math.mode === 'review') {
    math.mode = 'round';
    math.el.mode.classList.add('hidden');
    mathNewRound();
    return;
  }
  if (math.mode === 'home') {
    mathRenderHome();
    return;
  }
  mathNewRound();
}

function mathClosePicker() {
  if (math.el.picker) {
    math.el.picker.classList.add('hidden');
    math.el.picker.innerHTML = '';
  }
  math.picker = null;
}

/* ---------------- 开局与出题 ---------------- */

async function mathNewRound() {
  if (math.busy) return;
  if (!math.selected.length) {
    math.el.prompt.textContent = '先选择要练的课程，再开始试炼。';
    mathOpenPicker();
    return;
  }
  const gen = math.gen;
  math.busy = true;
  mathTimerStop();
  math.mode = 'round';
  if (math.el.mode) math.el.mode.classList.add('hidden');
  if (math.el.home) math.el.home.classList.add('hidden');
  math.no += 1;
  math.round = null;
  math.index = 0;
  math.q = null;
  math.result = null;
  math.qResults = [];
  math.revealed = false;
  math.aiRows = [];
  math.hintsShown = 0;
  math.lastValue = null;
  math.lastOut = null;
  math.el.result.classList.add('hidden');
  math.el.result.innerHTML = '';
  math.el.card.classList.remove('hidden');
  math.el.prompt.textContent = '正在出题…';
  math.el.head.innerHTML = '';
  math.el.answer.innerHTML = '';
  math.el.hints.classList.add('hidden');
  math.el.hints.innerHTML = '';
  math.el.reveal.classList.add('hidden');
  math.el.reveal.innerHTML = '';
  mathMsg('');
  math.el.actions.innerHTML = '';
  mathRenderTop();
  mathRenderSide();

  let data;
  try {
    data = await api('/api/hall/math/new', {
      level: mathLevelOf(math.store.xp),
      ai_count: math.aiCount,
      courses: math.selected,
    });
  } catch (e) {
    if (gen !== math.gen) return;
    math.busy = false;
    math.el.prompt.textContent = '出题失败，请重试。';
    mathMsg((e && e.message) || String(e), 'err');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'math-btn primary';
    retry.textContent = '重新开局';
    retry.onclick = () => mathNewRound();
    math.el.actions.innerHTML = '';
    math.el.actions.appendChild(retry);
    return;
  }
  if (gen !== math.gen) return;
  math.busy = false;
  math.round = data.round;
  mathRenderQuestion();
}

function mathRenderQuestion() {
  const q = math.round.questions[math.index];
  math.q = q;
  math.revealed = false;
  math.aiRows = [];
  math.hintsShown = 0;
  math.lastValue = null;
  math.lastOut = null;
  math.el.result.classList.add('hidden');
  math.el.card.classList.remove('hidden');
  if (math.el.home) math.el.home.classList.add('hidden');
  math.el.reveal.classList.add('hidden');
  math.el.reveal.innerHTML = '';
  mathMsg('');
  mathRenderHead(q);
  mathRichText(math.el.prompt, q.prompt);
  mathRenderAnswer(q);
  mathRenderHints(q);
  mathRenderActions(q);
  mathRenderSide();
  mathTimerStart();
  if (q.kind === 'calc') {
    const input = math.el.answer.querySelector('input');
    if (input) setTimeout(() => { try { input.focus(); } catch (e) { /* 忽略 */ } }, 0);
  }
}

function mathRenderHead(q) {
  const box = math.el.head;
  box.innerHTML = '';
  const chips = [
    ['math-chip subject', q.course_name || q.subject_name],
    ['math-chip kind', q.kind === 'calc' ? '计算' : (q.kind === 'tf' ? '真假判断' : '反例选择')],
    ['math-chip', '难度 ' + '★'.repeat(Math.max(1, Math.min(5, q.difficulty || 1)))],
    ['math-chip skill', q.skill || ''],
  ];
  chips.forEach(([cls, text]) => {
    if (!text) return;
    const s = document.createElement('span');
    s.className = cls;
    s.textContent = text;
    box.appendChild(s);
  });
  const no = document.createElement('span');
  no.className = 'math-chip index';
  const inReview = math.mode === 'review';
  const total = inReview
    ? (math.reviewQueue.length || 1)
    : (math.round ? math.round.questions.length : 5);
  const idx = inReview ? math.reviewIndex : math.index;
  no.textContent = (inReview ? '复习 ' : '第 ') + (idx + 1) + ' / ' + total + ' 题';
  box.appendChild(no);
}

function mathRenderAnswer(q) {
  const box = math.el.answer;
  box.innerHTML = '';
  if (q.kind === 'calc') {
    const wrap = document.createElement('div');
    wrap.className = 'math-calc';
    const input = document.createElement('input');
    input.type = 'text';
    input.inputMode = 'decimal';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.className = 'math-input';
    input.placeholder = '输入数值答案';
    input.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); mathSubmitFromInput(); }
    };
    wrap.appendChild(input);
    if (q.unit) {
      const unit = document.createElement('span');
      unit.className = 'math-unit';
      unit.textContent = q.unit;
      wrap.appendChild(unit);
    }
    const hint = document.createElement('div');
    hint.className = 'math-input-hint';
    hint.textContent = '整数答案需精确；小数答案允许 1% 误差。答得越快得分越高。';
    box.appendChild(wrap);
    box.appendChild(hint);
    return;
  }
  const opts = document.createElement('div');
  opts.className = q.kind === 'tf' ? 'math-tf' : 'math-options';
  (q.options || []).forEach((o) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = q.kind === 'tf'
      ? 'math-tf-btn ' + (o.id === 'true' ? 'yes' : 'no')
      : 'math-option';
    if (q.kind === 'counter') {
      const id = document.createElement('span');
      id.className = 'math-option-id';
      id.textContent = String(o.id).toUpperCase();
      b.appendChild(id);
    }
    const t = document.createElement('span');
    mathRichText(t, o.text);
    b.appendChild(t);
    b.onclick = () => mathSubmit(o.id, b);
    opts.appendChild(b);
  });
  box.appendChild(opts);
  const keyhint = document.createElement('div');
  keyhint.className = 'math-input-hint';
  keyhint.textContent = q.kind === 'tf'
    ? '快捷键：1 正确 · 2 错误' : '快捷键：A / B / C / D';
  box.appendChild(keyhint);
}

function mathRenderHints(q) {
  const box = math.el.hints;
  if (!box) return;
  box.innerHTML = '';
  const hints = Array.isArray(q.hints) ? q.hints : [];
  if (!hints.length) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  math.hintsShown = 0;

  const list = document.createElement('div');
  list.className = 'math-hint-list';
  box.appendChild(list);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'math-hint-btn';
  const sync = () => {
    const left = hints.length - math.hintsShown;
    btn.textContent = left > 0
      ? ('提示 ' + (math.hintsShown + 1) + ' / ' + hints.length)
      : '提示已全部展开';
    btn.disabled = left <= 0;
  };
  btn.onclick = () => {
    if (math.hintsShown >= hints.length) return;
    const item = document.createElement('div');
    item.className = 'math-hint-item';
    mathRichText(item, String(hints[math.hintsShown]));
    list.appendChild(item);
    math.hintsShown += 1;
    sync();
    // 新提示插在按钮上方，小屏上可能正好在视口外；滚到可见位置
    if (typeof item.scrollIntoView === 'function') {
      try { item.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { /* 忽略 */ }
    }
  };
  sync();
  box.appendChild(btn);
}

function mathRenderActions(q) {
  const box = math.el.actions;
  box.innerHTML = '';
  if (q.kind === 'calc') {
    const ok = document.createElement('button');
    ok.type = 'button';
    ok.className = 'math-btn primary';
    ok.textContent = '提交答案';
    ok.onclick = mathSubmitFromInput;
    math.el.submit = ok;
    box.appendChild(ok);
  } else {
    math.el.submit = null;
  }
  const skip = document.createElement('button');
  skip.type = 'button';
  skip.className = 'math-btn ghost';
  skip.textContent = '跳过';
  skip.title = '跳过按答错计 0 分';
  skip.onclick = () => mathSubmit(null);
  math.el.skip = skip;
  box.appendChild(skip);

  const next = document.createElement('button');
  next.type = 'button';
  next.className = 'math-btn primary hidden';
  next.textContent = '下一题';
  next.onclick = mathNext;
  math.el.next = next;
  box.appendChild(next);
}

/* ---------------- 作答 ---------------- */

/* 判断题 1/2、反例题 A–D；输入框 / 下拉框 / 弹窗打开时不拦截 */
function mathOnKey(e) {
  if (!math.q || math.revealed || math.busy) return;
  if (math.overlay || math.picker) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  const tag = t && t.tagName ? t.tagName : '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
      || (t && t.isContentEditable)) return;
  if (math.mode !== 'review' && !math.round) return;
  const q = math.q;
  if (q.kind === 'tf') {
    if (e.key === '1') { e.preventDefault(); mathSubmit(true); }
    else if (e.key === '2') { e.preventDefault(); mathSubmit(false); }
    return;
  }
  if (q.kind === 'counter') {
    const idx = ['KeyA', 'KeyB', 'KeyC', 'KeyD'].indexOf(e.code);
    if (idx < 0) return;
    const opt = (q.options || [])[idx];
    if (!opt) return;
    e.preventDefault();
    mathSubmit(opt.id);
  }
}

function mathRevealIntoView() {
  const box = math.el.reveal;
  if (!box || box.classList.contains('hidden')) return;
  const head = box.querySelector('.math-reveal-head');
  if (!head) return;
  const r = head.getBoundingClientRect();
  if (r.top < 0 || r.bottom > window.innerHeight) {
    try { head.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { /* 忽略 */ }
  }
}

function mathSubmitFromInput() {
  const input = math.el.answer.querySelector('input');
  if (!input) return;
  const raw = String(input.value || '').trim();
  if (!raw) { mathMsg('先输入一个数值答案，或点「跳过」。', 'err'); return; }
  const n = Number(raw.replace(/，/g, ',').replace(/,/g, ''));
  if (!Number.isFinite(n)) { mathMsg('这看起来不是一个数值，请只输入数字（可用小数点与负号）。', 'err'); return; }
  mathSubmit(n);
}

async function mathSubmit(value, btn) {
  if (!math.q || math.revealed || math.busy) return;
  if (math.mode === 'review') { mathSubmitReview(value, btn); return; }
  if (!math.round) return;
  const gen = math.gen;
  const q = math.q;
  math.busy = true;
  math.lastValue = value;
  mathMaskAnswers(true, btn);
  if (math.el.submit) math.el.submit.disabled = true;
  if (math.el.skip) math.el.skip.disabled = true;
  mathMsg('提交中…');

  mathTimerStop();
  math.elapsedMs = Math.max(0, Date.now() - math.qStart);

  let out;
  try {
    out = await api('/api/hall/math/answer', {
      round_id: math.round.id, qid: q.id, value: value, elapsed_ms: math.elapsedMs,
    });
  } catch (e) {
    if (gen !== math.gen) return;
    math.busy = false;
    mathMaskAnswers(false, btn);
    if (math.el.submit) math.el.submit.disabled = false;
    if (math.el.skip) math.el.skip.disabled = false;
    mathMsg('提交失败：' + ((e && e.message) || e), 'err');
    mathTimerStart(false);
    return;
  }
  if (gen !== math.gen) return;

  math.busy = false;
  math.revealed = true;
  math.aiRows = Array.isArray(out.ai) ? out.ai : [];
  math.lastOut = out;
  math.qResults[math.index] = { q: q, out: out, value: value };
  mathMsg('');
  // 提交后保留提示，方便对照解析回看
  mathRenderReveal(q, out);
  mathRenderSide();
  mathTimerTick(true);

  if (math.el.skip) math.el.skip.classList.add('hidden');
  if (math.el.submit) math.el.submit.classList.add('hidden');
  if (math.el.next) {
    math.el.next.classList.remove('hidden');
    math.el.next.textContent = math.index >= math.round.questions.length - 1
      ? '查看本局结算' : '下一题 ›';
    math.el.next.focus();
  }
  mathRevealIntoView();
}

function mathMaskAnswers(locked, picked) {
  const buttons = math.el.answer.querySelectorAll('button');
  buttons.forEach((b) => {
    b.disabled = locked;
    if (locked && picked && b === picked) b.classList.add('picked');
    if (!locked) b.classList.remove('picked');
  });
  const input = math.el.answer.querySelector('input');
  if (input) input.disabled = locked;
}

function mathRenderReveal(q, out) {
  const box = math.el.reveal;
  box.innerHTML = '';
  box.classList.remove('hidden');

  const head = document.createElement('div');
  head.className = 'math-reveal-head';
  const mark = document.createElement('span');
  mark.className = 'math-mark ' + (out.correct ? 'ok' : 'bad');
  mark.textContent = out.correct ? '正确' : '错误';
  head.appendChild(mark);
  const score = document.createElement('span');
  score.className = 'math-reveal-score';
  score.textContent = '+' + out.score + ' 分';
  head.appendChild(score);
  const time = document.createElement('span');
  time.className = 'math-reveal-time';
  time.textContent = '用时 ' + (math.elapsedMs / 1000).toFixed(1) + ' 秒';
  head.appendChild(time);
  box.appendChild(head);

  const ans = document.createElement('div');
  ans.className = 'math-correct';
  const label = document.createElement('span');
  label.className = 'math-correct-label';
  label.textContent = '正确答案';
  const val = document.createElement('span');
  val.className = 'math-correct-value';
  mathRichText(val, mathFmtAnswer(q, out.answer));
  ans.appendChild(label);
  ans.appendChild(val);
  box.appendChild(ans);

  if (out.explain) {
    const ex = document.createElement('div');
    ex.className = 'math-explain';
    mathRichText(ex, out.explain);
    box.appendChild(ex);
  }

  const steps = Array.isArray(out.steps) ? out.steps : [];
  if (steps.length) {
    const sol = document.createElement('div');
    sol.className = 'math-solution';
    const t = document.createElement('div');
    t.className = 'math-sol-title';
    t.textContent = '完整解';
    sol.appendChild(t);
    const ol = document.createElement('ol');
    ol.className = 'math-steps';
    steps.forEach((s) => {
      const li = document.createElement('li');
      mathRichText(li, String(s));
      ol.appendChild(li);
    });
    sol.appendChild(ol);
    box.appendChild(sol);
  }

  if (out.exam_tip) {
    const tip = document.createElement('div');
    tip.className = 'math-tip';
    const t = document.createElement('span');
    t.className = 'math-tip-tag';
    t.textContent = '考试套路';
    const v = document.createElement('span');
    mathRichText(v, out.exam_tip);
    tip.appendChild(t);
    tip.appendChild(v);
    box.appendChild(tip);
  }

  if (out.scene) {
    const scene = document.createElement('div');
    scene.className = 'math-scene';
    const t = document.createElement('span');
    t.className = 'math-scene-tag';
    t.textContent = '用在哪';
    const v = document.createElement('span');
    mathRichText(v, out.scene);
    scene.appendChild(t);
    scene.appendChild(v);
    box.appendChild(scene);
  }

  const acts = document.createElement('div');
  acts.className = 'math-reveal-actions';
  const solo = document.createElement('button');
  solo.type = 'button';
  solo.className = 'math-btn';
  solo.textContent = '追问 Solo AI';
  solo.title = '把整道题、你的答案与官方解析带到 Solo，在「追问：」后继续问';
  solo.onclick = mathAskSolo;
  acts.appendChild(solo);
  const note = document.createElement('span');
  note.className = 'math-reveal-note';
  note.textContent = 'AI 讲解可能有误，以人工解析为准';
  acts.appendChild(note);
  box.appendChild(acts);
}

/* 把答案格式化成题面语境下看得懂的样子 */
function mathFmtAnswer(q, value) {
  if (value == null) return '未作答';
  if (q.kind === 'tf') return (value === true || value === 'true') ? '正确' : '错误';
  if (q.kind === 'counter') {
    const o = (q.options || []).find((x) => x.id === value);
    return o ? o.text : String(value);
  }
  return mathNum(value) + (q.unit || '');
}

function mathNum(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const r = Math.round(n);
  if (Math.abs(n - r) < 1e-9) return String(r);
  return String(Number(n.toFixed(6)));
}

/* ---------------- 公式排版 ----------------
   题库里的公式是 Unicode 纯文本（P(A∩B)=...、x^(n-1)），这里把连续的
   公式片段交给 KaTeX 排版，中文说明仍按普通文本显示。识别或渲染失败时
   自动回退原文，避免把题目变成一行红字。 */
const MATH_RICH_EXTRA = '≤≥≠≈≡∈∉⊂⊆⊃⊇∪∩→←↔⇒⇔∫∑∏√∞±×÷·πσμλθφΔΩ∂∇²³¹⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉';
const MATH_FORMULA_MARK = /[\^_=<>|\[\]{}≤≥≠≈≡∈∉⊂⊆⊃⊇∪∩→←↔⇒⇔∫∑∏√∞±×÷·πσμλθφΔΩ∂∇²³¹⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉]/;

function mathRichChar(ch) {
  if (!ch) return false;
  if (/[A-Za-z0-9+\-*\/^_=<>|()\[\]{}.,!'\s]/.test(ch)) return true;
  return MATH_RICH_EXTRA.indexOf(ch) >= 0;
}

function mathIsFormula(text) {
  const t = String(text).trim();
  if (t.length < 2 || t.length > 120) return false;
  if (!MATH_FORMULA_MARK.test(t)) return false;
  return !/[\u4e00-\u9fff]/.test(t);
}

function mathRichSplit(text) {
  const parts = [];
  let textBuf = '';
  let formulaBuf = '';
  const flushText = () => {
    if (textBuf) { parts.push({ math: false, text: textBuf }); textBuf = ''; }
  };
  const flushFormula = () => {
    if (!formulaBuf) return;
    parts.push({ math: mathIsFormula(formulaBuf), text: formulaBuf });
    formulaBuf = '';
  };
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (mathRichChar(ch)) {
      flushText();
      formulaBuf += ch;
    } else {
      flushFormula();
      textBuf += ch;
    }
  }
  flushFormula();
  flushText();
  return parts;
}

function mathGroupEnd(s, start, open, close) {
  let depth = 0;
  for (let i = start; i < s.length; i += 1) {
    if (s[i] === open) depth += 1;
    else if (s[i] === close) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/* x^(n-1) -> x^{n-1}，Z_n -> Z_{n}；已经是 {…} 的保持原样 */
function mathFixScripts(s, mark) {
  let out = '';
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (ch !== mark) { out += ch; continue; }
    const next = s[i + 1];
    if (next === '{') {
      const end = mathGroupEnd(s, i + 1, '{', '}');
      if (end > i) {
        out += mark + s.slice(i + 1, end + 1);
        i = end;
        continue;
      }
    }
    if (next === '(') {
      const end = mathGroupEnd(s, i + 1, '(', ')');
      if (end > i) {
        out += mark + '{' + s.slice(i + 2, end) + '}';
        i = end;
        continue;
      }
    }
    let j = i + 1;
    let token = '';
    if (s[j] === '-' || s[j] === '+') { token += s[j]; j += 1; }
    while (j < s.length && /[A-Za-z0-9.]/.test(s[j])) { token += s[j]; j += 1; }
    if (token) {
      out += mark + '{' + token + '}';
      i = j - 1;
      continue;
    }
    if (next && next !== ' ' && next !== mark) {
      out += mark + '{' + next + '}';
      i += 1;
      continue;
    }
    out += ch;
  }
  return out;
}

function mathTexify(text) {
  let s = String(text).trim();
  // KaTeX 对部分 Unicode 符号不认，先换成等价命令
  s = s.replace(/√\(([^()]*)\)/g, '\\sqrt{$1}');
  s = s.replace(/√([A-Za-z0-9]+)/g, '\\sqrt{$1}');
  s = s.replace(/√/g, '\\surd');
  s = s.replace(/[−–]/g, '-');
  s = s.replace(/°/g, '^{\\circ}');
  s = s.replace(/ℝ/g, '\\mathbb{R}');
  s = s.replace(/ℕ/g, '\\mathbb{N}');
  s = s.replace(/ℤ/g, '\\mathbb{Z}');
  s = s.replace(/ℚ/g, '\\mathbb{Q}');
  s = s.replace(/ℂ/g, '\\mathbb{C}');
  s = s.replace(/∅/g, '\\emptyset');
  s = s.replace(/[#$&%]/g, '\\$&');
  s = mathFixScripts(s, '^');
  s = mathFixScripts(s, '_');
  s = s.replace(/\bmod\b/g, '\\bmod');
  s = s.replace(/\b(gcd|det|max|min|log|ln|exp|sin|cos|tan|lim)\b/g, '\\operatorname{$1}');
  return s;
}

function mathUpgradeRich() {
  if (!window.katex) return;
  const nodes = document.querySelectorAll('[data-math-rich]');
  for (let i = 0; i < nodes.length; i += 1) {
    const el = nodes[i];
    const raw = el.getAttribute('data-math-rich');
    if (raw == null) continue;
    el.removeAttribute('data-math-rich');
    mathRichText(el, raw);
  }
}

function mathEnsureKatex() {
  if (window.katex || math.katexTimer) return;
  let tries = 0;
  math.katexTimer = setInterval(() => {
    tries += 1;
    if (window.katex) {
      clearInterval(math.katexTimer);
      math.katexTimer = null;
      mathUpgradeRich();
      return;
    }
    if (tries >= 40) {
      clearInterval(math.katexTimer);
      math.katexTimer = null;
    }
  }, 250);
}

function mathAppendText(el, text) {
  if (text) el.appendChild(document.createTextNode(text));
}

function mathRichText(el, text) {
  if (!el) return;
  const raw = String(text == null ? '' : text);
  el.textContent = '';
  if (!raw) return;
  if (!window.katex) {
    el.textContent = raw;
    el.setAttribute('data-math-rich', raw);
    mathEnsureKatex();
    return;
  }
  el.removeAttribute('data-math-rich');
  const parts = mathRichSplit(raw);
  parts.forEach((part) => {
    if (!part.math) { mathAppendText(el, part.text); return; }
    const leading = (part.text.match(/^\s*/) || [''])[0];
    const trailing = (part.text.match(/\s*$/) || [''])[0];
    const core = part.text.slice(leading.length, part.text.length - trailing.length);
    mathAppendText(el, leading);
    if (!core) {
      mathAppendText(el, trailing);
      return;
    }
    const tex = mathTexify(core);
    let html = '';
    try {
      html = window.katex.renderToString(tex, {
        throwOnError: false, strict: 'ignore', trust: false,
      });
    } catch (e) {
      html = '';
    }
    if (!html || html.indexOf('katex-error') >= 0) {
      mathAppendText(el, core);
    } else {
      const span = document.createElement('span');
      span.className = 'math-formula';
      span.innerHTML = html;
      el.appendChild(span);
    }
    mathAppendText(el, trailing);
  });
}

/* ---------------- 追问 Solo AI ----------------
   把整道题的信息（题干、选项、你的答案、正确答案、解析、完整解、
   考试套路、应用场景）一起带到 Solo；末尾留「追问：」，问题由用户自己写。
   预填后不自动发送，避免悄悄消耗 token。 */
function mathAskSolo() {
  const q = math.q;
  if (!q) return;
  if (typeof setUiMode !== 'function') {
    toast('当前版本没有 Solo 模式，无法跳转', 'error');
    return;
  }
  try {
    if (typeof state !== 'undefined' && state.streaming) {
      toast('当前正在生成回答，等结束再追问', 'error');
      return;
    }
  } catch (e) { /* state 不可用时继续 */ }

  const out = math.lastOut || {};
  const row = (Array.isArray(math.qResults) && math.qResults[math.index]) || {};
  const userValue = (math.lastValue !== null && math.lastValue !== undefined)
    ? math.lastValue
    : (row.value != null ? row.value : undefined);
  const kindText = q.kind === 'calc' ? '计算' : (q.kind === 'tf' ? '真假判断' : '反例选择');
  const course = q.course_name || q.subject_name || '';
  const subject = q.subject_name || '';
  const lines = ['【数学竞技场 · 题目追问】', ''];

  if (course) lines.push('课程：' + course);
  if (subject && subject !== course) lines.push('学科：' + subject);
  if (q.skill) lines.push('知识点：' + q.skill);
  lines.push('题型：' + kindText);
  if (q.difficulty) {
    lines.push('难度：' + '★'.repeat(Math.max(1, Math.min(5, q.difficulty))));
  }
  lines.push('', '题目：', q.prompt);

  if (Array.isArray(q.options) && q.options.length) {
    if (q.kind === 'counter') {
      lines.push('', '选项：');
      q.options.forEach((o) => {
        lines.push(String(o.id).toUpperCase() + '. ' + o.text);
      });
    } else if (q.kind === 'tf') {
      lines.push('', '选项：正确 / 错误');
    }
  }

  lines.push('', '我的答案：' + mathFmtAnswer(q, userValue));
  if (out.answer != null) lines.push('正确答案：' + mathFmtAnswer(q, out.answer));
  if (out.correct != null) lines.push('结果：' + (out.correct ? '回答正确' : '回答错误'));
  if (out.score != null) lines.push('得分：' + out.score);

  if (out.explain) lines.push('', '官方解析：', String(out.explain));
  const steps = Array.isArray(out.steps) ? out.steps : [];
  if (steps.length) {
    lines.push('', '完整解：');
    steps.forEach((s, i) => lines.push((i + 1) + '. ' + String(s)));
  }
  if (out.exam_tip) lines.push('', '考试套路：', String(out.exam_tip));
  if (out.scene) lines.push('', '应用场景：', String(out.scene));

  lines.push('', '追问：');

  const input = document.querySelector('#input');
  if (!input) {
    toast('找不到对话输入框，请手动切到 Solo', 'error');
    return;
  }
  if (input.value.trim() && !confirm('Solo 输入框已有内容，要替换成这道题吗？')) return;

  setUiMode('solo');
  input.value = lines.join('\n');
  if (typeof input.setSelectionRange === 'function') {
    const end = input.value.length;
    input.setSelectionRange(end, end);
  }
  if (typeof autoGrow === 'function') autoGrow();
  input.focus();
  toast('整道题已带到 Solo，请在「追问：」后写下你的问题');
}

function mathNext() {
  if (!math.revealed || math.busy) return;
  if (math.mode === 'review') { mathNextReview(); return; }
  if (!math.round) return;
  if (math.index >= math.round.questions.length - 1) { mathFinish(); return; }
  math.index += 1;
  mathRenderQuestion();
  mathScrollCardIntoView();
}

function mathScrollCardIntoView() {
  const card = math.el.card;
  if (!card || card.classList.contains('hidden')) return;
  if (card.getBoundingClientRect().top < 0) {
    try { card.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (e) { /* 忽略 */ }
  }
}

/* ---------------- 错题复习（本地判分，不调用后端 / AI） ---------------- */

function mathBeginReview(queue) {
  if (math.busy) return;
  if (!queue || !queue.length) {
    toast('没有可复习的错题');
    return;
  }
  const gen = math.gen;
  math.mode = 'review';
  math.reviewQueue = queue.slice(0, MATH_REVIEW_LIMIT);
  math.reviewIndex = 0;
  math.reviewCorrect = 0;
  math.reviewXp = 0;
  math.reviewResults = [];
  math.revealed = false;
  math.busy = false;
  math.result = null;
  math.el.result.classList.add('hidden');
  math.el.result.innerHTML = '';
  math.el.mode.classList.remove('hidden');
  math.el.no.textContent = '错题复习';
  mathRenderTop();
  mathRenderReviewQuestion();
  if (gen !== math.gen) return;
}

function mathStartReview() {
  if (math.busy) return;
  const due = mathDueRecords();
  if (!due.length) {
    const next = mathNextDueText();
    toast(next ? ('今天没有到期错题；' + next) : '还没有可复习的错题，先做几道题吧');
    return;
  }
  mathBeginReview(due);
}

function mathReviewQuestion() {
  const rec = math.reviewQueue[math.reviewIndex];
  if (!rec) return null;
  const kind = rec.kind;
  let options = Array.isArray(rec.options) ? rec.options : [];
  if (kind === 'tf' && !options.length) {
    options = [{ id: 'true', text: '正确' }, { id: 'false', text: '错误' }];
  }
  return {
    id: 'review-' + math.reviewIndex,
    subject: rec.subject,
    subject_name: rec.subject_name || MATH_SUBJECTS[rec.subject] || rec.subject,
    course: rec.course || rec.subject,
    course_name: rec.course_name || MATH_COURSES[rec.course] || rec.subject_name || '',
    kind: kind,
    prompt: rec.prompt,
    options: options,
    unit: rec.unit || '',
    skill: rec.skill || '',
    difficulty: rec.difficulty || 2,
    hints: Array.isArray(rec.hints) ? rec.hints : [],
    scene: rec.scene || '',
    answer_value: rec.answer_value,
  };
}

function mathRenderReviewQuestion() {
  const q = mathReviewQuestion();
  if (!q) { mathRenderReviewSummary(); return; }
  math.q = q;
  math.revealed = false;
  math.hintsShown = 0;
  math.lastValue = null;
  math.lastOut = null;
  math.el.card.classList.remove('hidden');
  math.el.result.classList.add('hidden');
  math.el.result.innerHTML = '';
  if (math.el.home) math.el.home.classList.add('hidden');
  math.el.reveal.classList.add('hidden');
  math.el.reveal.innerHTML = '';
  mathMsg('');
  mathRenderHead(q);
  mathRichText(math.el.prompt, q.prompt);
  mathRenderAnswer(q);
  mathRenderHints(q);
  mathRenderActions(q);
  if (math.el.skip) math.el.skip.textContent = '记错并跳过';
  mathRenderSide();
  mathTimerStart();
  if (q.kind === 'calc') {
    const input = math.el.answer.querySelector('input');
    if (input) setTimeout(() => { try { input.focus(); } catch (e) { /* 忽略 */ } }, 0);
  }
}

function mathGradeLocal(q, value) {
  if (value == null) return false;
  if (q.kind === 'calc') {
    const ans = Number(q.answer_value);
    if (!Number.isFinite(ans)) return false;
    if (Math.abs(ans - Math.round(ans)) < 1e-9) return Math.abs(value - ans) <= 1e-6;
    return Math.abs(value - ans) <= Math.max(1e-9, Math.abs(ans) * 0.01);
  }
  if (q.kind === 'tf') {
    const want = q.answer_value === true || q.answer_value === 'true';
    const got = value === true || value === 'true';
    return want === got;
  }
  return value === q.answer_value;
}

function mathSubmitReview(value, btn) {
  if (!math.q || math.revealed || math.busy) return;
  const rec = math.reviewQueue[math.reviewIndex];
  if (!rec) return;
  math.busy = true;
  math.lastValue = value;
  mathMaskAnswers(true, btn);
  if (math.el.submit) math.el.submit.disabled = true;
  if (math.el.skip) math.el.skip.disabled = true;
  mathTimerStop();
  math.elapsedMs = Math.max(0, Date.now() - math.qStart);

  const q = math.q;
  const correct = mathGradeLocal(q, value);
  mathReviewUpdate(rec, correct);
  math.reviewResults[math.reviewIndex] = correct;
  if (correct) {
    math.reviewCorrect += 1;
    math.reviewXp += 3;
    mathAddXp(3);
  }
  mathSaveStore();

  const out = {
    correct: correct, score: correct ? 100 : 0,
    answer: rec.answer_display,
    answer_value: rec.answer_value,
    explain: rec.explain || '',
    steps: Array.isArray(rec.steps) ? rec.steps : [],
    exam_tip: rec.exam_tip || '',
    scene: rec.scene || '',
    ai: [], answered: 0, total: 0,
  };
  math.lastOut = out;
  math.revealed = true;
  math.busy = false;
  mathMsg('');
  // 复习提交后同样保留提示
  mathRenderReveal(q, out);
  mathRenderTop();
  mathRenderSide();
  mathTimerTick(true);

  if (math.el.skip) math.el.skip.classList.add('hidden');
  if (math.el.submit) math.el.submit.classList.add('hidden');
  if (math.el.next) {
    math.el.next.classList.remove('hidden');
    math.el.next.textContent = math.reviewIndex >= math.reviewQueue.length - 1
      ? '完成复习' : '下一题 ›';
    math.el.next.focus();
  }
  mathRevealIntoView();
}

function mathNextReview() {
  if (math.reviewIndex >= math.reviewQueue.length - 1) {
    mathRenderReviewSummary();
    return;
  }
  math.reviewIndex += 1;
  mathRenderReviewQuestion();
  mathScrollCardIntoView();
}

function mathRenderReviewSummary() {
  const box = math.el.result;
  if (!box) return;
  box.innerHTML = '';
  box.classList.remove('hidden');
  math.el.card.classList.add('hidden');
  mathTimerStop();

  const title = document.createElement('div');
  title.className = 'math-result-title';
  title.textContent = '错题复习完成';
  box.appendChild(title);

  const hero = document.createElement('div');
  hero.className = 'math-result-hero';
  const rank = document.createElement('div');
  rank.className = 'math-result-rank win';
  rank.textContent = math.reviewCorrect + ' / ' + math.reviewQueue.length;
  const nums = document.createElement('div');
  nums.className = 'math-result-nums';
  nums.textContent = '复习答对 · +' + math.reviewXp + ' XP';
  hero.appendChild(rank);
  hero.appendChild(nums);
  box.appendChild(hero);

  const note = document.createElement('div');
  note.className = 'math-panel-note';
  const dueLeft = mathDueRecords().length;
  note.textContent = dueLeft
    ? ('还有 ' + dueLeft + ' 道到期错题，可以再来一轮。')
    : ('这一轮已清空到期错题。' + (mathNextDueText() ? mathNextDueText() + '。' : ''));
  box.appendChild(note);

  const acts = document.createElement('div');
  acts.className = 'math-result-actions';
  if (dueLeft) {
    const again = document.createElement('button');
    again.type = 'button';
    again.className = 'math-btn';
    again.textContent = '再复习一轮';
    again.onclick = () => mathStartReview();
    acts.appendChild(again);
  }
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'math-btn primary';
  back.textContent = '返回试炼';
  back.onclick = () => {
    math.mode = 'round';
    math.el.mode.classList.add('hidden');
    mathNewRound();
  };
  acts.appendChild(back);
  box.appendChild(acts);
}

/* ---------------- 结算 ---------------- */

async function mathFinish() {
  if (!math.round || math.busy) return;
  const gen = math.gen;
  math.busy = true;
  mathMsg('正在结算…');
  let out;
  try {
    out = await api('/api/hall/math/finish', { round_id: math.round.id });
  } catch (e) {
    if (gen !== math.gen) return;
    math.busy = false;
    mathMsg('结算失败：' + ((e && e.message) || e), 'err');
    return;
  }
  if (gen !== math.gen) return;
  math.busy = false;
  math.result = out;
  mathStoreRound(out);
  mathRenderResult(out);
  mathRenderTop();
  mathRenderSide();
}

function mathStoreRound(out) {
  const store = math.store;
  const beforeLevel = mathLevelOf(store.xp);

  let xpGain = out.correct * 18 + (MATH_RANK_BONUS[out.rank - 1] || 0);

  math.qResults.forEach((row) => {
    if (!row || !row.q) return;
    const sub = store.subjects[row.q.subject];
    if (!sub) return;
    sub.seen += 1;
    if (row.out.correct) sub.correct += 1;
    const courseKey = row.q.course || mathCourseOf(row.q.subject, row.q.skill || '');
    const cs = store.courseStats[courseKey];
    if (cs) {
      cs.seen += 1;
      if (row.out.correct) cs.correct += 1;
    }
    if (row.out.correct) {
      // 在新的一局里做对旧错题，也算一次成功复习：把间隔往后推
      const old = store.wrong.find((x) => x && !x.legacy && x.prompt === row.q.prompt);
      if (old) mathReviewUpdate(old, true);
    }
    if (!row.out.correct) {
      const rec = {
        subject: row.q.subject,
        subject_name: row.q.subject_name,
        course: courseKey,
        course_name: row.q.course_name || MATH_COURSES[courseKey] || row.q.subject_name,
        skill: row.q.skill || '',
        prompt: row.q.prompt,
        kind: row.q.kind,
        options: Array.isArray(row.q.options) ? row.q.options : [],
        unit: row.q.unit || '',
        difficulty: row.q.difficulty || 2,
        hints: Array.isArray(row.q.hints) ? row.q.hints : [],
        scene: row.out.scene || '',
        answer: mathFmtAnswer(row.q, row.out.answer),
        my_answer: mathFmtAnswer(row.q, row.value),
        answer_display: row.out.answer,
        answer_value: row.out.answer_value,
        explain: row.out.explain || '',
        steps: Array.isArray(row.out.steps) ? row.out.steps : [],
        exam_tip: row.out.exam_tip || '',
        box: 0,
        due: Date.now(),
        reps: 0,
        lapses: 1,
        last: 0,
        legacy: false,
        at: Date.now(),
        cause: '',
      };
      const old = store.wrong.find((x) => x && x.prompt === rec.prompt);
      if (old) {
        rec.reps = Number(old.reps) || 0;
        rec.lapses = (Number(old.lapses) || 0) + 1;
      }
      store.wrong = store.wrong.filter((x) => !x || x.prompt !== rec.prompt);
      store.wrong.unshift(rec);
    }
  });
  if (store.wrong.length > 120) store.wrong.length = 120;

  store.xp += xpGain;
  store.rounds += 1;
  store.bestScore = Math.max(store.bestScore, out.score || 0);
  if (out.rank === 1) {
    store.streak += 1;
    store.bestStreak = Math.max(store.bestStreak, store.streak);
  } else {
    store.streak = 0;
  }
  const afterLevel = mathLevelOf(store.xp);

  // 对局记录：汇总 + 逐题明细，供「对局记录」弹窗查看
  const historyItems = math.qResults.filter(Boolean).map((row) => ({
    course: row.q.course || mathCourseOf(row.q.subject, row.q.skill || ''),
    course_name: row.q.course_name || '',
    skill: row.q.skill || '',
    kind: row.q.kind || '',
    correct: !!(row.out && row.out.correct),
    score: Math.max(0, Number(row.out && row.out.score) || 0),
    prompt: row.q.prompt || '',
  }));
  store.history.unshift({
    at: Date.now(),
    score: Math.max(0, Number(out.score) || 0),
    correct: Math.max(0, Number(out.correct) || 0),
    total: Math.max(0, Number(out.total) || historyItems.length),
    rank: Math.max(0, Number(out.rank) || 0),
    xp_gain: xpGain,
    level: afterLevel,
    courses: (math.selected || []).slice(),
    players: (out.players || []).map((p) => ({
      name: p.name || '',
      kind: p.kind || 'ai',
      score: Math.max(0, Number(p.score) || 0),
      correct: Math.max(0, Number(p.correct) || 0),
      rank: Math.max(0, Number(p.rank) || 0),
    })),
    items: historyItems,
  });
  if (store.history.length > MATH_HISTORY_LIMIT) store.history.length = MATH_HISTORY_LIMIT;

  mathSaveStore();
  out.xp_gain = xpGain;
  out.level_before = beforeLevel;
  out.level_after = afterLevel;
}

function mathRenderResult(out) {
  const box = math.el.result;
  box.innerHTML = '';
  box.classList.remove('hidden');
  math.el.card.classList.add('hidden');

  const title = document.createElement('div');
  title.className = 'math-result-title';
  title.textContent = '本局结算';
  box.appendChild(title);

  const hero = document.createElement('div');
  hero.className = 'math-result-hero';
  const rank = document.createElement('div');
  rank.className = 'math-result-rank' + (out.rank === 1 ? ' win' : '');
  rank.textContent = '第 ' + out.rank + ' 名';
  hero.appendChild(rank);
  const nums = document.createElement('div');
  nums.className = 'math-result-nums';
  const accuracy = out.total ? Math.round((out.correct / out.total) * 100) : 0;
  nums.textContent = out.score + ' 分 · 答对 ' + out.correct + ' / ' + out.total
    + ' · 正确率 ' + accuracy + '%';
  hero.appendChild(nums);
  box.appendChild(hero);

  const list = document.createElement('div');
  list.className = 'math-rank-list';
  (out.players || []).forEach((p) => {
    const row = document.createElement('div');
    row.className = 'math-rank-row' + (p.kind === 'human' ? ' me' : '');
    const place = document.createElement('span');
    place.className = 'math-rank-place';
    place.textContent = String(p.rank);
    const name = document.createElement('span');
    name.className = 'math-rank-name';
    name.textContent = p.name + (p.kind === 'ai' ? ' · AI' : '');
    const sc = document.createElement('span');
    sc.className = 'math-rank-score';
    sc.textContent = p.score + ' 分 · ' + p.correct + ' 对';
    row.appendChild(place);
    row.appendChild(name);
    row.appendChild(sc);
    list.appendChild(row);
  });
  box.appendChild(list);

  const wrongN = Math.max(0, (out.total || 0) - (out.correct || 0));
  const note = document.createElement('div');
  note.className = 'math-result-note';
  note.textContent = wrongN
    ? (wrongN + ' 道错题已自动加入错题本，可在「今日复习」里重做。')
    : '全部答对，保持手感。';
  box.appendChild(note);

  const xp = document.createElement('div');
  xp.className = 'math-xp';
  const gain = document.createElement('span');
  gain.className = 'math-xp-gain';
  gain.textContent = '+' + out.xp_gain + ' XP';
  xp.appendChild(gain);

  if (out.level_after > out.level_before) {
    const up = document.createElement('span');
    up.className = 'math-levelup';
    up.textContent = '段位提升：Lv.' + out.level_after + ' ' + MATH_LEVEL_NAMES[out.level_after - 1];
    xp.appendChild(up);
  }

  const lv = mathLevelOf(math.store.xp);
  const nextXp = mathNextLevelXp(lv);
  const bar = document.createElement('div');
  bar.className = 'math-xp-bar';
  const fill = document.createElement('i');
  const low = MATH_LEVEL_XP[lv - 1];
  fill.style.width = nextXp == null ? '100%'
    : Math.max(0, Math.min(100, ((math.store.xp - low) / (nextXp - low)) * 100)) + '%';
  bar.appendChild(fill);
  xp.appendChild(bar);

  const lvText = document.createElement('span');
  lvText.className = 'math-xp-text';
  lvText.textContent = 'Lv.' + lv + ' ' + MATH_LEVEL_NAMES[lv - 1]
    + (nextXp == null ? ' · 已满段' : ' · 距下一段 ' + (nextXp - math.store.xp) + ' XP');
  xp.appendChild(lvText);
  box.appendChild(xp);

  const ability = mathRenderAbilityBlock(out.by_ability || [],
    '本局只答 5 题；单个维度少于 5 题时只标样本不足，不做横向比较。');
  ability.classList.add('math-result-ability');
  box.appendChild(ability);

  const acts = document.createElement('div');
  acts.className = 'math-result-actions';
  const again = document.createElement('button');
  again.type = 'button';
  again.className = 'math-btn primary';
  again.textContent = '再来一局';
  again.onclick = () => mathNewRound();
  acts.appendChild(again);
  const home = document.createElement('button');
  home.type = 'button';
  home.className = 'math-btn';
  home.textContent = '回训练首页';
  home.onclick = () => mathGoHome();
  acts.appendChild(home);
  if (mathDueRecords().length) {
    const rev = document.createElement('button');
    rev.type = 'button';
    rev.className = 'math-btn';
    rev.textContent = '今日复习 ' + mathDueRecords().length;
    rev.onclick = () => mathStartReview();
    acts.appendChild(rev);
  }
  const bank = document.createElement('button');
  bank.type = 'button';
  bank.className = 'math-btn ghost';
  bank.textContent = '换题库';
  bank.onclick = () => mathOpenPicker();
  acts.appendChild(bank);
  box.appendChild(acts);
}

/* ---------------- 侧栏 ---------------- */

function mathRenderProgress() {
  const box = math.el.progress;
  if (!box) return;
  box.innerHTML = '';
  const inReview = math.mode === 'review';
  const total = inReview
    ? (math.reviewQueue.length || 1)
    : (math.round ? math.round.questions.length : 5);
  for (let i = 0; i < total; i += 1) {
    const seg = document.createElement('span');
    if (inReview) {
      const r = math.reviewResults[i];
      if (r === true) seg.className = 'ok';
      else if (r === false) seg.className = 'bad';
      if (i === math.reviewIndex && !math.revealed) seg.className = 'current';
      seg.title = '复习 ' + (i + 1);
    } else {
      const r = math.qResults[i];
      if (r) seg.className = r.out.correct ? 'ok' : 'bad';
      else if (math.round && i === math.index && !math.result) seg.className = 'current';
      seg.title = '第 ' + (i + 1) + ' 题';
    }
    box.appendChild(seg);
  }
}

function mathRenderSideHome() {
  const side = math.el.side;
  if (!side) return;
  side.innerHTML = '';

  const lv = mathLevelOf(math.store.xp);
  const nextXp = mathNextLevelXp(lv);
  const box = document.createElement('div');
  box.className = 'math-panel';
  const title = document.createElement('div');
  title.className = 'math-panel-title';
  title.textContent = '训练状态';
  box.appendChild(title);
  const line = document.createElement('div');
  line.className = 'math-panel-note';
  line.textContent = 'Lv.' + lv + ' ' + MATH_LEVEL_NAMES[lv - 1]
    + (nextXp == null ? ' · 已满段' : ' · 距下一段 ' + (nextXp - math.store.xp) + ' XP');
  box.appendChild(line);
  const stats = document.createElement('div');
  stats.className = 'math-panel-note';
  stats.textContent = '已练 ' + math.store.rounds + ' 局 · 最高分 ' + math.store.bestScore
    + ' · 错题 ' + (math.store.wrong || []).length;
  box.appendChild(stats);
  side.appendChild(box);

  const ability = mathRenderAbilityBlock(mathAbilityLocal(), '来自对局与模拟卷；少于 5 题只标样本不足。');
  ability.classList.add('math-panel');
  side.appendChild(ability);

  const diag = mathExamDiagnosisData(0);
  const weak = (diag.weak || []).slice(0, 3);
  if (weak.length) {
    const wp = document.createElement('div');
    wp.className = 'math-panel';
    const wt = document.createElement('div');
    wt.className = 'math-panel-title';
    wt.textContent = '最该补的知识点';
    wp.appendChild(wt);
    weak.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'math-panel-note';
      row.textContent = (s.course_name ? s.course_name + ' · ' : '') + s.skill
        + ' · ' + s.accuracy + '%（' + s.correct + '/' + s.attempts + '）';
      wp.appendChild(row);
    });
    side.appendChild(wp);
  }

  side.appendChild(mathReviewInfoPanel());
}

function mathRenderSide() {
  const side = math.el.side;
  if (!side) return;
  if (math.mode === 'home') {
    mathRenderSideHome();
    return;
  }
  side.innerHTML = '';
  mathRenderProgress();

  const progress = document.createElement('div');
  progress.className = 'math-panel';
  const inReview = math.mode === 'review';
  const pt = document.createElement('div');
  pt.className = 'math-panel-title';
  pt.textContent = inReview ? '复习进度' : '本局进度';
  progress.appendChild(pt);
  const dots = document.createElement('div');
  dots.className = 'math-dots';
  const total = inReview
    ? (math.reviewQueue.length || 1)
    : (math.round ? math.round.questions.length : 5);
  for (let i = 0; i < total; i += 1) {
    const d = document.createElement('span');
    d.className = 'math-dot';
    if (inReview) {
      const r = math.reviewResults[i];
      if (r === true) d.classList.add('ok');
      else if (r === false) d.classList.add('bad');
      if (i === math.reviewIndex && !math.revealed) d.classList.add('current');
      d.title = '复习 ' + (i + 1);
    } else {
      const r = math.qResults[i];
      if (r) d.classList.add(r.out.correct ? 'ok' : 'bad');
      if (math.round && i === math.index && !math.result) d.classList.add('current');
      d.title = '第 ' + (i + 1) + ' 题';
    }
    dots.appendChild(d);
  }
  progress.appendChild(dots);
  const sum = document.createElement('div');
  sum.className = 'math-panel-note';
  if (inReview) {
    const done = math.reviewResults.filter((x) => x === true || x === false).length;
    sum.textContent = '已复习 ' + done + ' / ' + total + ' · 答对 ' + math.reviewCorrect;
  } else {
    const done = math.qResults.filter(Boolean).length;
    const right = math.qResults.filter((x) => x && x.out.correct).length;
    sum.textContent = '已答 ' + done + ' / ' + total + ' · 答对 ' + right;
  }
  progress.appendChild(sum);
  side.appendChild(progress);

  side.appendChild(inReview ? mathReviewInfoPanel() : mathAiPanel());

  const mastery = document.createElement('div');
  mastery.className = 'math-panel';
  const mt = document.createElement('div');
  mt.className = 'math-panel-title';
  mt.textContent = '学科掌握度';
  mastery.appendChild(mt);
  Object.keys(MATH_SUBJECTS).forEach((k) => {
    const s = math.store.subjects[k] || { seen: 0, correct: 0 };
    const row = document.createElement('div');
    row.className = 'math-mastery';
    const name = document.createElement('span');
    name.className = 'math-mastery-name';
    name.textContent = MATH_SUBJECTS[k];
    const bar = document.createElement('span');
    bar.className = 'math-mastery-bar';
    const fill = document.createElement('i');
    fill.style.width = (s.seen ? Math.round((s.correct / s.seen) * 100) : 0) + '%';
    bar.appendChild(fill);
    const text = document.createElement('span');
    text.className = 'math-mastery-text';
    text.textContent = s.seen ? (s.correct + '/' + s.seen) : '—';
    row.appendChild(name);
    row.appendChild(bar);
    row.appendChild(text);
    mastery.appendChild(row);
  });

  const termBox = document.createElement('details');
  termBox.className = 'math-term-mastery';
  const termSum = document.createElement('summary');
  termSum.textContent = '学期掌握度';
  termBox.appendChild(termSum);
  ['analysis1', 'analysis2', 'analysis3', 'algebra1', 'algebra2'].forEach((k) => {
    const s = math.store.courseStats[k] || { seen: 0, correct: 0 };
    const row = document.createElement('div');
    row.className = 'math-mastery';
    const name = document.createElement('span');
    name.className = 'math-mastery-name';
    name.textContent = MATH_COURSE_SHORT[k] || MATH_COURSES[k];
    name.title = MATH_COURSES[k];
    const bar = document.createElement('span');
    bar.className = 'math-mastery-bar';
    const fill = document.createElement('i');
    fill.style.width = (s.seen ? Math.round((s.correct / s.seen) * 100) : 0) + '%';
    bar.appendChild(fill);
    const text = document.createElement('span');
    text.className = 'math-mastery-text';
    text.textContent = s.seen ? (s.correct + '/' + s.seen) : '—';
    row.appendChild(name);
    row.appendChild(bar);
    row.appendChild(text);
    termBox.appendChild(row);
  });
  mastery.appendChild(termBox);
  mastery.appendChild(mathWrongPanel());
  side.appendChild(mastery);
}

function mathReviewInfoPanel() {
  const panel = document.createElement('div');
  panel.className = 'math-panel';
  const title = document.createElement('div');
  title.className = 'math-panel-title';
  title.textContent = '错题复习';
  panel.appendChild(title);
  const note = document.createElement('div');
  note.className = 'math-panel-note';
  note.textContent = '本地判分，不调用 AI。答对会拉长复习间隔，答错回到第 0 盒。';
  panel.appendChild(note);
  const due = mathDueRecords().length;
  const stat = document.createElement('div');
  stat.className = 'math-review-stat';
  stat.textContent = '到期 ' + due + ' 道' + (mathNextDueText() ? ' · ' + mathNextDueText() : '');
  panel.appendChild(stat);
  return panel;
}

function mathAiPanel() {
  const panel = document.createElement('div');
  panel.className = 'math-panel';
  const title = document.createElement('div');
  title.className = 'math-panel-title';
  title.textContent = 'AI 陪练';
  panel.appendChild(title);

  if (!math.round) {
    const loading = document.createElement('div');
    loading.className = 'math-panel-note';
    loading.textContent = '正在准备开局…';
    panel.appendChild(loading);
    return panel;
  }
  if (!(math.round.players || []).some((p) => p.kind === 'ai')) {
    const none = document.createElement('div');
    none.className = 'math-panel-note';
    none.textContent = '本局未开启 AI 陪练。';
    panel.appendChild(none);
    return panel;
  }

  (math.round.players || []).filter((p) => p.kind === 'ai').forEach((p) => {
    const row = math.aiRows.find((a) => a.player_id === p.id);
    const wrap = document.createElement('div');
    wrap.className = 'math-ai-row';

    const head = document.createElement('div');
    head.className = 'math-ai-head';
    const name = document.createElement('span');
    name.className = 'math-ai-name';
    name.textContent = p.name;
    head.appendChild(name);

    const state = document.createElement('span');
    state.className = 'math-ai-state';
    if (!math.q || math.revealed) {
      if (row) {
        state.classList.add(row.correct ? 'ok' : 'bad');
        state.textContent = row.correct ? '答对' : '答错';
      }
    } else {
      state.textContent = '待你作答';
    }
    head.appendChild(state);
    wrap.appendChild(head);

    if (row) {
      const ans = document.createElement('div');
      ans.className = 'math-ai-answer';
      mathRichText(ans, '答案：' + mathFmtAnswer(math.q, row.value) + ' · +' + row.score + ' 分');
      wrap.appendChild(ans);
      if (row.reason) {
        const reason = document.createElement('div');
        reason.className = 'math-ai-reason';
        mathRichText(reason, row.reason);
        wrap.appendChild(reason);
      }
    }
    panel.appendChild(wrap);
  });
  return panel;
}

function mathWrongPanel() {
  const box = document.createElement('div');
  box.className = 'math-wrong';
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = '错题本（' + math.store.wrong.length + '）';
  details.appendChild(summary);

  if (!math.store.wrong.length) {
    const empty = document.createElement('div');
    empty.className = 'math-panel-note';
    empty.textContent = '还没有错题，继续保持。';
    details.appendChild(empty);
  } else {
    math.store.wrong.slice(0, 5).forEach((w) => {
      const item = document.createElement('div');
      item.className = 'math-wrong-item';
      const head = document.createElement('div');
      head.className = 'math-wrong-head';
      head.textContent = (w.course_name || w.subject_name || '')
        + (w.skill ? ' · ' + w.skill : '')
        + ' · ' + mathReviewLabel(w);
      const prompt = document.createElement('div');
      prompt.className = 'math-wrong-prompt';
      mathRichText(prompt, w.prompt);
      const ans = document.createElement('div');
      ans.className = 'math-wrong-answer';
      mathRichText(ans, '正确答案：' + (w.answer || '—'));
      item.appendChild(head);
      item.appendChild(prompt);
      item.appendChild(ans);
      if (w.explain) {
        const ex = document.createElement('div');
        ex.className = 'math-wrong-explain';
        mathRichText(ex, w.explain);
        item.appendChild(ex);
      }
      details.appendChild(item);
    });
    const acts = document.createElement('div');
    acts.className = 'math-wrong-actions';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'math-btn';
    open.textContent = '打开错题本';
    open.onclick = mathOpenWrongBook;
    const notes = document.createElement('button');
    notes.type = 'button';
    notes.className = 'math-btn';
    notes.textContent = '整理成笔记';
    notes.onclick = mathOpenNotes;
    acts.appendChild(open);
    acts.appendChild(notes);
    details.appendChild(acts);
  }
  box.appendChild(details);
  return box;
}

function mathRenderTop() {
  if (!math.el.no) return;
  const inReview = math.mode === 'review';
  const inHome = math.mode === 'home';
  math.el.no.textContent = '第 ' + math.no + ' 局';
  math.el.no.classList.toggle('hidden', inHome || inReview);
  if (math.el.mode) math.el.mode.classList.toggle('hidden', !inReview);
  if (math.el.homeBtn) math.el.homeBtn.classList.toggle('active', inHome);

  const lv = mathLevelOf(math.store.xp);
  math.el.level.textContent = '段位 Lv.' + lv + ' ' + MATH_LEVEL_NAMES[lv - 1];
  math.el.level.title = '累计 ' + math.store.xp + ' XP，段位越高 AI 陪练越强';
  math.el.streak.textContent = math.store.streak > 0
    ? '连胜 ' + math.store.streak : ('最高连胜 ' + math.store.bestStreak);
  math.el.streak.title = '每局第一名才算连胜';

  const n = (math.selected || []).length;
  math.el.bank.textContent = n === 0 ? '题库：未选择'
    : (n === MATH_COURSE_ORDER.length ? '题库：全部' : ('题库：' + n + ' 门'));
  math.el.bank.title = '选择题库范围（只从选中的课程抽题）';

  const due = mathDueRecords().length;
  math.el.review.textContent = due ? ('今日复习 ' + due) : '今日复习';
  math.el.review.title = due ? ('有 ' + due + ' 道到期错题')
    : (mathNextDueText() || '还没有可复习的错题');
  math.el.review.disabled = inReview;

  if (math.el.archive) {
    const nw = (math.store.wrong || []).length;
    const nh = (math.store.history || []).length;
    math.el.archive.title = '错题 ' + nw + ' 道 · 对局记录 ' + nh + ' 条 · 笔记';
  }
}

function mathTimerStart(reset) {
  if (reset !== false) {
    math.qStart = Date.now();
    math.elapsedMs = 0;
  }
  mathTimerStop();
  mathTimerTick(false);
  math.timer = setInterval(() => mathTimerTick(false), 250);
}

function mathTimerStop() {
  if (math.timer) { clearInterval(math.timer); math.timer = null; }
}

function mathTimerTick(frozen) {
  if (!math.el.timer) return;
  const ms = frozen ? math.elapsedMs : Math.max(0, Date.now() - math.qStart);
  const sec = Math.floor(ms / 1000);
  const mm = Math.floor(sec / 60);
  const ss = sec % 60;
  math.el.timer.textContent = mm + ':' + (ss < 10 ? '0' : '') + ss;
  math.el.timer.title = '本题用时 ' + (ms / 1000).toFixed(1) + ' 秒';
}

function mathMsg(text, kind) {
  const el = math.el.msg;
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('err', kind === 'err');
}
