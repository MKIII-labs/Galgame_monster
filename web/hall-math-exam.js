/* 数学竞技场：模拟考试、学情诊断、速查、冲刺、证明训练（从 hall-math.js 拆出） */

/* ---------------- 期末冲刺 ----------------
   模拟卷 / 学情诊断 / 考前速查 / 7 天冲刺 / 证明训练。
   试卷由后端生成，交卷前不返回答案；证明评分是 AI 步骤点评，不是形式化验证。 */

const MATH_EXAM_CAUSES = ['概念不清', '计算失误', '审题错误', '时间不够', '完全不会'];

function mathExamPrefsLoad() {
  const out = { course: '', skill: '', level: 3, count: 15, duration: 90, drillCount: 5 };
  const raw = mathReadRaw(MATH_EXAM_PREFS_KEY);
  if (!raw || typeof raw !== 'object') return out;
  if (typeof raw.course === 'string' && MATH_COURSES[raw.course]) out.course = raw.course;
  if (typeof raw.skill === 'string') out.skill = raw.skill;
  const lv = Number(raw.level);
  if (lv >= 1 && lv <= 5) out.level = Math.round(lv);
  const cnt = Number(raw.count);
  if (cnt >= 5 && cnt <= 30) out.count = Math.round(cnt);
  const dur = Number(raw.duration);
  if (dur >= 10 && dur <= 240) out.duration = Math.round(dur);
  const drill = Number(raw.drillCount);
  if (drill >= 5 && drill <= 30) out.drillCount = Math.round(drill);
  return out;
}

function mathExamPrefsSave(st) {
  if (!st) return;
  try {
    localStorage.setItem(MATH_EXAM_PREFS_KEY, JSON.stringify({
      course: st.course, skill: st.skill, level: st.level,
      count: st.examCount, duration: st.duration, drillCount: st.drillCount,
    }));
  } catch (e) { /* 忽略 */ }
}

function mathExamEsc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function mathExamState() {
  if (!math.exam) {
    const prefs = mathExamPrefsLoad();
    math.exam = {
      view: 'home', meta: null, loading: false,
      course: prefs.course || ((math.selected && math.selected[0]) || MATH_COURSE_ORDER[0]),
      count: prefs.count, examCount: prefs.count, drillCount: prefs.drillCount,
      duration: prefs.duration, level: prefs.level, skill: prefs.skill, mode: 'exam',
      paper: null, answers: {}, flags: {}, index: 0,
      startedAt: 0, remaining: 0, lastTick: 0, timer: null,
      report: null, wrongAdded: false, submitting: false,
      diagnosisDays: 90,
      sprintDate: (math.store.sprint && math.store.sprint.date) || '',
      cheatSheet: '',
      proof: { list: null, course: '', current: null, text: '', result: null, loading: false },
    };
  }
  return math.exam;
}

function mathExamStopTimer() {
  if (math.exam && math.exam.timer) {
    clearInterval(math.exam.timer);
    math.exam.timer = null;
  }
}

function mathOpenExam() {
  if (!math.store) return;
  mathClosePicker();
  mathOpenOverlay('模拟考试', '整卷、专项、学情诊断、考前速查、7 天冲刺和证明训练都在这里。');
  const st = mathExamState();
  mathExamRender();
  if (!st.meta && !st.loading) mathExamLoadMeta();
  else if (st.view === 'room' && st.paper && st.remaining > 0) mathExamStartTimer();
}

async function mathExamLoadMeta() {
  const st = mathExamState();
  st.loading = true;
  mathExamRender();
  try {
    const r = await api('/api/hall/math/paper/meta', {});
    st.meta = r;
    if (!st.meta.courses.some((c) => c.id === st.course)) {
      st.course = st.meta.courses[0].id;
      mathExamPrefsSave(st);
    }
  } catch (e) {
    toast('加载课程失败：' + (e.message || e), 'error');
  } finally {
    st.loading = false;
    mathExamRender();
  }
}

function mathExamSetView(view) {
  const st = mathExamState();
  st.view = view;
  if (view !== 'room') mathExamStopTimer();
  mathExamRender();
}

function mathExamRender() {
  const ov = math.overlay;
  if (!ov || !math.exam) return;
  ov.body.innerHTML = '';
  ov.foot.innerHTML = '';
  const st = math.exam;
  if (st.loading) {
    ov.body.innerHTML = '<div class="math-loading"><span class="math-spinner"></span>'
      + '正在准备期末复习数据…</div>';
    return;
  }
  if (st.view === 'setup') mathExamRenderSetup();
  else if (st.view === 'room') mathExamRenderRoom();
  else if (st.view === 'report') mathExamRenderReport();
  else if (st.view === 'diagnosis') mathExamRenderDiagnosis();
  else if (st.view === 'cheatsheet') mathExamRenderCheatSheet();
  else if (st.view === 'sprint') mathExamRenderSprint();
  else if (st.view === 'proof') mathExamRenderProof();
  else mathExamRenderHome();
}

function mathExamRenderHome() {
  const ov = math.overlay;
  const st = math.exam;
  const exams = math.store.exams || [];
  const last = exams[0];
  ov.body.innerHTML = '<div class="math-exam-hero">'
    + '<div class="math-exam-hero-title">模拟考试</div>'
    + '<div class="math-exam-hero-sub">先做一套整卷，再看薄弱知识点，最后用速查和冲刺计划收口。</div>'
    + (last ? '<div class="math-exam-last">上次：' + mathExamEsc(last.course_name || '')
      + ' · ' + last.score + ' 分 · ' + last.correct + '/' + last.total + ' 题</div>' : '')
    + '</div>'
    + '<div class="math-exam-grid">'
    + '<button type="button" class="math-exam-card" data-view="setup" data-mode="exam"><b>期末模拟卷</b>'
    + '<span>单门课程整卷，计时、答题卡、交卷后统一批改</span></button>'
    + '<button type="button" class="math-exam-card" data-view="setup" data-mode="drill"><b>知识点专项</b>'
    + '<span>按课程 + 知识点练 5 / 10 题，不计时</span></button>'
    + '<button type="button" class="math-exam-card" data-view="diagnosis"><b>学情诊断</b>'
    + '<span>按知识点看正确率，找出考前最该补的 5 个点</span></button>'
    + '<button type="button" class="math-exam-card" data-view="cheatsheet"><b>考前速查</b>'
    + '<span>把薄弱知识点和错题整理成一份 Markdown</span></button>'
    + '<button type="button" class="math-exam-card" data-view="sprint"><b>7 天冲刺</b>'
    + '<span>按考试日期生成每日任务，勾选完成</span></button>'
    + '<button type="button" class="math-exam-card" data-view="proof"><b>证明训练</b>'
    + '<span>写证明，AI 按步骤点评；不是形式化验证</span></button>'
    + '</div>'
    + '<div class="math-exam-note">模拟卷只从单门课程出题；答案与解析在交卷后才返回。'
    + '职业/考试建议仅供参考。</div>';
  ov.body.querySelectorAll('[data-view]').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.mode) {
        st.mode = b.dataset.mode;
        st.count = st.mode === 'drill' ? st.drillCount : st.examCount;
      }
      mathExamSetView(b.dataset.view);
    };
  });
}

function mathExamCourseSkills(course) {
  const st = mathExamState();
  if (!st.meta) return [];
  const c = st.meta.courses.find((x) => x.id === course);
  return c ? c.skills : [];
}

function mathExamRenderSetup() {
  const ov = math.overlay;
  const st = math.exam;
  if (!st.meta) {
    ov.body.innerHTML = '<div class="math-loading"><span class="math-spinner"></span>正在加载课程…</div>';
    return;
  }
  const skills = mathExamCourseSkills(st.course);
  if (st.skill && skills.indexOf(st.skill) < 0) st.skill = '';
  const courseOpts = st.meta.courses.map((c) => '<option value="' + c.id + '"'
    + (c.id === st.course ? ' selected' : '') + '>' + mathExamEsc(c.name) + '</option>').join('');
  const skillOpts = ['<option value="">全部知识点</option>'].concat(
    skills.map((s) => '<option value="' + mathExamEsc(s) + '"'
      + (s === st.skill ? ' selected' : '') + '>' + mathExamEsc(s) + '</option>')
  ).join('');
  ov.body.innerHTML = '<div class="math-exam-form">'
    + '<div class="math-exam-form-title">' + (st.mode === 'drill' ? '知识点专项' : '期末模拟卷') + '</div>'
    + '<label class="math-exam-field"><span>课程</span><select id="math-exam-course">'
    + courseOpts + '</select></label>'
    + '<label class="math-exam-field"><span>知识点</span><select id="math-exam-skill">'
    + skillOpts + '</select></label>'
    + '<label class="math-exam-field"><span>题量</span><select id="math-exam-count">'
    + [5, 10, 15, 20].map((n) => '<option value="' + n + '"'
      + (n === st.count ? ' selected' : '') + '>' + n + ' 题</option>').join('')
    + '</select></label>'
    + (st.mode === 'drill' ? '' :
      '<label class="math-exam-field"><span>时长</span><select id="math-exam-duration">'
      + [60, 90, 120].map((n) => '<option value="' + n + '"'
        + (n === st.duration ? ' selected' : '') + '>' + n + ' 分钟</option>').join('')
      + '</select></label>')
    + '<label class="math-exam-field"><span>难度</span><select id="math-exam-level">'
    + [1, 2, 3, 4, 5].map((n) => '<option value="' + n + '"'
      + (n === st.level ? ' selected' : '') + '>'
      + '★'.repeat(n) + (n === 3 ? ' 标准' : '') + '</option>').join('')
    + '</select></label>'
    + '<div class="math-exam-note">' + (st.mode === 'drill'
      ? '专项训练不计时，题量建议 5 或 10。'
      : '交卷后才会看到答案与解析；中途退出会保留当前答案和剩余时间。') + '</div>'
    + '</div>';
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn primary" id="math-exam-start">'
    + (st.mode === 'drill' ? '开始专项' : '开始模拟卷') + '</button>';
  ov.body.querySelector('#math-exam-course').onchange = (e) => {
    st.course = e.target.value;
    st.skill = '';
    mathExamPrefsSave(st);
    mathExamRenderSetup();
  };
  ov.body.querySelector('#math-exam-skill').onchange = (e) => {
    st.skill = e.target.value;
    mathExamPrefsSave(st);
  };
  ov.body.querySelector('#math-exam-count').onchange = (e) => {
    st.count = Number(e.target.value) || 15;
    if (st.mode === 'drill') st.drillCount = st.count;
    else st.examCount = st.count;
    mathExamPrefsSave(st);
  };
  const dur = ov.body.querySelector('#math-exam-duration');
  if (dur) dur.onchange = (e) => {
    st.duration = Number(e.target.value) || 90;
    mathExamPrefsSave(st);
  };
  ov.body.querySelector('#math-exam-level').onchange = (e) => {
    st.level = Number(e.target.value) || 3;
    mathExamPrefsSave(st);
  };
  ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('home');
  ov.foot.querySelector('#math-exam-start').onclick = mathExamStart;
}

async function mathExamStart() {
  const st = mathExamState();
  if (st.loading) return;
  mathExamPrefsSave(st);
  st.loading = true;
  mathExamRender();
  try {
    const r = await api('/api/hall/math/paper/new', {
      course: st.course, count: st.count,
      duration: st.mode === 'drill' ? 10 : st.duration,
      level: st.level, skill: st.skill,
    });
    st.paper = r;
    st.answers = {};
    st.flags = {};
    st.index = 0;
    st.report = null;
    st.wrongAdded = false;
    st.startedAt = Date.now();
    st.remaining = st.mode === 'drill' ? 0 : r.duration * 60000;
    st.lastTick = Date.now();
    st.loading = false;
    st.view = 'room';
    mathExamRender();
    mathExamStartTimer();
    if (r.warnings && r.warnings.length) toast(r.warnings[0]);
  } catch (e) {
    st.loading = false;
    toast('生成试卷失败：' + (e.message || e), 'error');
    mathExamRender();
  }
}

function mathExamStartTimer() {
  mathExamStopTimer();
  const st = mathExamState();
  if (st.mode === 'drill' || !st.remaining) return;
  st.lastTick = Date.now();
  st.timer = setInterval(() => {
    const now = Date.now();
    st.remaining = Math.max(0, st.remaining - (now - st.lastTick));
    st.lastTick = now;
    const el = document.getElementById('math-exam-time');
    if (el) el.textContent = mathExamTimeText(st.remaining);
    if (st.remaining <= 0) {
      mathExamStopTimer();
      toast('时间到，自动交卷');
      mathExamSubmit(true);
    }
  }, 1000);
}

function mathExamTimeText(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

function mathExamCurrentQuestion() {
  const st = mathExamState();
  return st.paper && st.paper.questions ? st.paper.questions[st.index] : null;
}

function mathExamRenderRoom() {
  const ov = math.overlay;
  const st = math.exam;
  if (!st.paper || !st.paper.questions || !st.paper.questions.length) {
    ov.body.innerHTML = '<div class="math-panel-note">试卷为空，请返回重新生成。</div>';
    ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>';
    ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('setup');
    return;
  }
  const q = mathExamCurrentQuestion();
  const qid = q.id;
  const ans = st.answers[qid];
  const value = ans ? ans.value : null;
  let answerHtml = '';
  if (q.kind === 'calc') {
    answerHtml = '<input id="math-exam-input" class="math-exam-input" type="text"'
      + ' placeholder="输入数值答案" value="' + mathExamEsc(value == null ? '' : value) + '">';
  } else if (q.kind === 'tf') {
    answerHtml = '<div class="math-exam-options">'
      + ['true', 'false'].map((v) => '<button type="button" class="math-exam-opt'
        + (String(value) === v ? ' sel' : '') + '" data-value="' + v + '">'
        + (v === 'true' ? '正确' : '错误') + '</button>').join('') + '</div>';
  } else {
    answerHtml = '<div class="math-exam-options">'
      + (q.options || []).map((o) => '<button type="button" class="math-exam-opt'
        + (String(value) === String(o.id) ? ' sel' : '')
        + '" data-value="' + mathExamEsc(o.id) + '">'
        + mathExamEsc(String(o.id).toUpperCase()) + '. ' + mathExamEsc(o.text) + '</button>').join('')
      + '</div>';
  }
  const answered = st.paper.questions.filter((x) => st.answers[x.id]
    && st.answers[x.id].value !== null && st.answers[x.id].value !== '').length;
  ov.body.innerHTML = '<div class="math-exam-room-head">'
    + '<div class="math-exam-room-title">' + mathExamEsc(st.paper.course_name)
    + ' · ' + (st.mode === 'drill' ? '知识点专项' : '模拟卷') + '</div>'
    + '<div class="math-exam-room-meta">第 ' + (st.index + 1) + ' / ' + st.paper.questions.length
    + ' 题 · 已答 ' + answered + '</div>'
    + '<div class="math-exam-time" id="math-exam-time">'
    + (st.mode === 'drill' ? '不限时' : mathExamTimeText(st.remaining)) + '</div></div>'
    + '<div class="math-exam-card-body">'
    + '<div class="math-exam-q-head"><span class="math-chip kind">'
    + (q.kind === 'calc' ? '计算' : (q.kind === 'tf' ? '真假判断' : '反例选择'))
    + '</span><span class="math-chip skill">' + mathExamEsc(q.skill || '') + '</span>'
    + '<span class="math-chip">难度 ' + '★'.repeat(Math.max(1, q.difficulty || 2)) + '</span></div>'
    + '<div class="math-exam-prompt" id="math-exam-prompt"></div>'
    + answerHtml
    + '</div>'
    + '<div class="math-exam-palette" id="math-exam-palette"></div>';
  mathRichText(ov.body.querySelector('#math-exam-prompt'), q.prompt || '');
  const input = ov.body.querySelector('#math-exam-input');
  if (input) {
    input.addEventListener('input', () => {
      mathExamSetAnswer(qid, input.value);
    });
    input.focus();
  }
  ov.body.querySelectorAll('.math-exam-opt').forEach((b) => {
    b.onclick = () => {
      mathExamSetAnswer(qid, b.dataset.value);
      mathExamRenderRoom();
    };
  });
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-prev"'
    + (st.index <= 0 ? ' disabled' : '') + '>上一题</button>'
    + '<button type="button" class="math-btn" id="math-exam-flag">'
    + (st.flags[qid] ? '取消标记' : '标记') + '</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn" id="math-exam-next"'
    + (st.index >= st.paper.questions.length - 1 ? ' disabled' : '') + '>下一题</button>'
    + '<button type="button" class="math-btn primary" id="math-exam-submit">交卷</button>';
  ov.foot.querySelector('#math-exam-prev').onclick = () => mathExamNav(-1);
  ov.foot.querySelector('#math-exam-next').onclick = () => mathExamNav(1);
  ov.foot.querySelector('#math-exam-flag').onclick = () => {
    st.flags[qid] = !st.flags[qid];
    mathExamRenderRoom();
  };
  ov.foot.querySelector('#math-exam-submit').onclick = () => mathExamSubmit(false);
  mathExamUpdatePalette();
}

function mathExamSetAnswer(qid, value) {
  const st = mathExamState();
  st.answers[qid] = { value: value, elapsed_ms: Date.now() - (st.startedAt || Date.now()) };
  mathExamUpdatePalette();
}

function mathExamUpdatePalette() {
  const st = mathExamState();
  const box = document.getElementById('math-exam-palette');
  if (!box || !st.paper) return;
  box.innerHTML = '';
  st.paper.questions.forEach((q, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'math-exam-pal' + (i === st.index ? ' current' : '')
      + (st.answers[q.id] && st.answers[q.id].value !== null && st.answers[q.id].value !== '' ? ' done' : '')
      + (st.flags[q.id] ? ' flagged' : '');
    b.textContent = String(i + 1);
    b.onclick = () => { st.index = i; mathExamRenderRoom(); };
    box.appendChild(b);
  });
}

function mathExamNav(d) {
  const st = mathExamState();
  if (!st.paper) return;
  st.index = Math.max(0, Math.min(st.paper.questions.length - 1, st.index + d));
  mathExamRenderRoom();
}

async function mathExamSubmit(auto) {
  const st = mathExamState();
  if (!st.paper || st.submitting) return;
  const unanswered = st.paper.questions.filter((q) => {
    const a = st.answers[q.id];
    return !a || a.value === null || a.value === '';
  }).length;
  if (!auto && unanswered && !confirm('还有 ' + unanswered + ' 题没答，确定交卷吗？')) return;
  st.submitting = true;
  mathExamStopTimer();
  const used = st.mode === 'drill' ? 0 : Math.max(0, st.paper.duration * 60000 - st.remaining);
  const answers = st.paper.questions.map((q) => {
    const a = st.answers[q.id];
    return { qid: q.id, value: a ? a.value : null, elapsed_ms: a ? a.elapsed_ms : 0 };
  });
  const ov = math.overlay;
  if (ov) {
    ov.body.innerHTML = '<div class="math-loading"><span class="math-spinner"></span>正在批改…</div>';
    ov.foot.innerHTML = '';
  }
  try {
    const r = await api('/api/hall/math/paper/grade', {
      paper_id: st.paper.paper_id, answers: answers, time_ms: used,
    });
    st.report = r;
    st.view = 'report';
    st.submitting = false;
    const rec = {
      at: Date.now(), course: st.paper.course, course_name: st.paper.course_name,
      score: r.score, correct: r.correct, total: r.total,
      duration: st.paper.duration, time_ms: r.time_ms,
      by_skill: r.by_skill || [],
      questions: (r.questions || []).map((q) => ({
        prompt: q.prompt, skill: q.skill, kind: q.kind, correct: q.correct,
        score: q.score, value: q.value, answer: q.answer,
        explain: q.explain, cause: '',
      })),
    };
    math.store.exams = [rec].concat(math.store.exams || []).slice(0, MATH_EXAM_LIMIT);
    mathSaveStore();
    mathExamRender();
    toast('交卷完成：' + r.score + ' 分');
  } catch (e) {
    st.submitting = false;
    toast('批改失败：' + (e.message || e), 'error');
    st.view = 'room';
    mathExamRender();
    mathExamStartTimer();
  }
}

function mathExamRenderReport() {
  const ov = math.overlay;
  const st = math.exam;
  const r = st.report;
  if (!r) { mathExamSetView('home'); return; }
  const bySkill = (r.by_skill || []).slice().sort((a, b) => a.accuracy - b.accuracy);
  const byAbility = r.by_ability || [];
  const abilityHtml = byAbility.length
    ? '<div class="math-exam-section">过程能力</div><div class="math-exam-skill-list">'
      + byAbility.map((a) => '<div class="math-exam-skill"><b>'
        + mathExamEsc(a.ability_name || mathAbilityName(a.ability)) + '</b><span>'
        + a.correct + '/' + a.attempts + ' · ' + a.accuracy + '%'
        + (a.attempts < 5 ? ' · 样本不足' : '') + '</span><i style="--p:' + a.accuracy + '%"></i></div>').join('')
      + '</div>'
    : '';
  ov.body.innerHTML = '<div class="math-exam-report-head">'
    + '<div class="math-exam-score">' + r.score + '<span>分</span></div>'
    + '<div class="math-exam-report-meta">答对 ' + r.correct + ' / ' + r.total
    + ' · 正确率 ' + r.accuracy + '% · 用时 '
    + (r.time_ms ? mathExamTimeText(r.time_ms) : '未计时') + '</div></div>'
    + '<div class="math-exam-section">知识点表现</div>'
    + '<div class="math-exam-skill-list">' + bySkill.map((s) => '<div class="math-exam-skill">'
      + '<b>' + mathExamEsc(s.skill) + '</b><span>' + s.correct + '/' + s.attempts
      + ' · ' + s.accuracy + '%</span><i style="--p:' + s.accuracy + '%"></i></div>').join('')
    + '</div>'
    + abilityHtml
    + '<div class="math-exam-section">逐题回顾</div>'
    + '<div class="math-exam-review">' + (r.questions || []).map((q, i) => '<div class="math-exam-review-item '
      + (q.correct ? 'ok' : 'bad') + '"><div class="math-exam-review-head"><b>' + (i + 1) + '. '
      + mathExamEsc(q.prompt) + '</b><span>' + (q.correct ? '正确' : '错误') + '</span></div>'
      + '<div class="math-exam-review-line">你的答案：' + mathExamEsc(mathFmtAnswer(q, q.value))
      + ' · 正确答案：' + mathExamEsc(q.answer == null ? '' : q.answer) + '</div>'
      + (q.explain ? '<div class="math-exam-review-explain">' + mathExamEsc(q.explain) + '</div>' : '')
      + (q.steps && q.steps.length ? '<ol class="math-steps">' + q.steps.map((s) => '<li>'
        + mathExamEsc(s) + '</li>').join('') + '</ol>' : '')
      + (q.exam_tip ? '<div class="math-exam-review-tip">考试套路：' + mathExamEsc(q.exam_tip) + '</div>' : '')
      + '</div>').join('') + '</div>';
  const wrongCount = (r.questions || []).filter((q) => !q.correct).length;
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-home">返回首页</button>'
    + '<span class="math-modal-spacer"></span>'
    + (wrongCount ? '<button type="button" class="math-btn" id="math-exam-addwrong">'
      + (st.wrongAdded ? '已加入错题本' : '错题加入错题本') + '</button>' : '')
    + '<button type="button" class="math-btn primary" id="math-exam-retry">再考一套</button>';
  ov.foot.querySelector('#math-exam-home').onclick = () => mathExamSetView('home');
  ov.foot.querySelector('#math-exam-retry').onclick = () => {
    st.report = null; st.paper = null; st.view = 'setup'; mathExamRender();
  };
  const add = ov.foot.querySelector('#math-exam-addwrong');
  if (add) add.onclick = () => mathExamAddWrongs();
}

function mathExamAddWrongs() {
  const st = mathExamState();
  if (!st.report || st.wrongAdded) return;
  const store = math.store;
  let added = 0;
  (st.report.questions || []).forEach((q) => {
    if (q.correct) return;
    const course = st.paper.course;
    const rec = {
      subject: course, subject_name: st.paper.course_name,
      course: course, course_name: st.paper.course_name,
      skill: q.skill || '', prompt: q.prompt, kind: q.kind,
      options: Array.isArray(q.options) ? q.options : [],
      unit: '', difficulty: q.difficulty || 2, hints: [],
      scene: q.scene || '', answer: String(q.answer == null ? '' : q.answer),
      my_answer: mathFmtAnswer(q, q.value),
      answer_display: q.answer, answer_value: q.answer_value,
      explain: q.explain || '', steps: Array.isArray(q.steps) ? q.steps : [],
      exam_tip: q.exam_tip || '', box: 0, due: Date.now(),
      reps: 0, lapses: 1, last: 0, legacy: false, at: Date.now(), cause: '',
    };
    const old = store.wrong.find((x) => x && x.prompt === rec.prompt);
    store.wrong = store.wrong.filter((x) => !x || x.prompt !== rec.prompt);
    if (old) { rec.reps = Number(old.reps) || 0; rec.lapses = (Number(old.lapses) || 0) + 1; }
    store.wrong.unshift(rec);
    added += 1;
  });
  if (store.wrong.length > 120) store.wrong.length = 120;
  st.wrongAdded = true;
  mathSaveStore();
  mathRenderTop();
  toast(added ? ('已加入 ' + added + ' 道错题') : '没有需要加入的错题');
  mathExamRenderReport();
}

function mathExamDiagnosisData(days) {
  const cutoff = days ? Date.now() - days * MATH_DAY_MS : 0;
  const map = {};
  const add = (course, courseName, skill, correct, at) => {
    if (!skill) return;
    if (cutoff && at && at < cutoff) return;
    const key = (course || '') + '|' + skill;
    const s = map[key] || (map[key] = {
      course: course || '', course_name: courseName || '', skill: skill,
      attempts: 0, correct: 0,
    });
    s.attempts += 1;
    if (correct) s.correct += 1;
  };
  (math.store.history || []).forEach((h) => {
    (h.items || []).forEach((it) => add(it.course, it.course_name, it.skill, it.correct, h.at));
  });
  (math.store.exams || []).forEach((e) => {
    (e.questions || []).forEach((q) => add(e.course, e.course_name, q.skill, q.correct, e.at));
  });
  (math.store.wrong || []).forEach((w) => add(w.course, w.course_name, w.skill, false, w.at));
  const skills = Object.keys(map).map((k) => {
    const s = map[k];
    s.accuracy = Math.round(100 * s.correct / Math.max(1, s.attempts));
    return s;
  });
  skills.sort((a, b) => a.accuracy - b.accuracy || b.attempts - a.attempts);
  let weak = skills.filter((s) => s.attempts >= 2 && s.accuracy < 75).slice(0, 5);
  if (!weak.length) weak = skills.slice(0, 5);
  const causes = {};
  (math.store.wrong || []).forEach((w) => {
    if (!w.cause) return;
    if (cutoff && w.at && w.at < cutoff) return;
    causes[w.cause] = (causes[w.cause] || 0) + 1;
  });
  return { skills: skills, weak: weak, causes: causes };
}

function mathExamRenderDiagnosis() {
  const ov = math.overlay;
  const st = math.exam;
  const data = mathExamDiagnosisData(st.diagnosisDays);
  const causeRows = MATH_EXAM_CAUSES.map((c) => [c, data.causes[c] || 0])
    .filter((x) => x[1] > 0);
  const abilityRows = mathAbilityLocal()
    .filter((a) => a.attempts > 0)
    .sort((a, b) => a.accuracy - b.accuracy || b.attempts - a.attempts)
    .slice(0, 3);
  const abilityHtml = abilityRows.length
    ? '<div class="math-exam-section">过程能力（最该练）</div><div class="math-exam-weak-list">'
      + abilityRows.map((a) => '<div class="math-exam-weak"><div class="math-exam-weak-head"><b>'
        + mathExamEsc(mathAbilityName(a.ability)) + '</b><span>' + a.accuracy + '% · '
        + a.correct + '/' + a.attempts + (a.attempts < 5 ? ' · 样本不足' : '')
        + '</span></div><div class="math-exam-weak-bar"><i style="--p:' + a.accuracy
        + '%"></i></div><div class="math-exam-weak-act">'
        + (a.attempts < 5 ? '样本还不够，先多练几题再判断' : '换题/换科目时，这个动作最容易掉链子')
        + '</div></div>').join('') + '</div>'
    : '';
  ov.body.innerHTML = '<div class="math-exam-diagnosis-head">'
    + '<div class="math-exam-diagnosis-title">学情诊断</div>'
    + '<div class="math-exam-seg">'
    + '<button type="button" class="' + (st.diagnosisDays ? 'active' : '')
    + '" data-days="90">最近 90 天</button>'
    + '<button type="button" class="' + (!st.diagnosisDays ? 'active' : '')
    + '" data-days="0">全部历史</button></div></div>'
    + '<div class="math-exam-section">考前最该补的知识点</div>'
    + (data.weak.length ? '<div class="math-exam-weak-list">' + data.weak.map((s) => {
      const action = s.accuracy < 50 ? '先看概念与例题，再做 5 道同类题'
        : (s.accuracy < 75 ? '做 10 道同类题，重点看错因' : '保持手感，隔天复习一次');
      return '<div class="math-exam-weak"><div class="math-exam-weak-head"><b>'
        + mathExamEsc(s.skill) + '</b><span>' + s.accuracy + '% · ' + s.correct + '/'
        + s.attempts + '</span></div><div class="math-exam-weak-bar"><i style="--p:'
        + s.accuracy + '%"></i></div><div class="math-exam-weak-act">'
        + mathExamEsc((s.course_name ? s.course_name + ' · ' : '') + action)
        + '</div></div>';
    }).join('') + '</div>' : '<div class="math-panel-note">还没有足够数据。先做一局或一套模拟卷。</div>')
    + abilityHtml
    + (causeRows.length ? '<div class="math-exam-section">错因分布</div><div class="math-exam-causes">'
      + causeRows.map((x) => '<span class="math-exam-cause">' + mathExamEsc(x[0])
        + ' <b>' + x[1] + '</b></span>').join('') + '</div>' : '')
    + '<div class="math-exam-note">统计'
    + (st.diagnosisDays ? '最近 90 天' : '全部历史')
    + '的对局、模拟卷和错题；不代表学校官方考纲。</div>';
  ov.body.querySelectorAll('[data-days]').forEach((b) => {
    b.onclick = () => {
      st.diagnosisDays = Number(b.dataset.days) || 0;
      mathExamRenderDiagnosis();
    };
  });
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn" id="math-exam-to-drill">去专项训练</button>'
    + '<button type="button" class="math-btn primary" id="math-exam-to-cheat">生成考前速查</button>';
  ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('home');
  ov.foot.querySelector('#math-exam-to-drill').onclick = () => {
    st.mode = 'drill';
    st.count = 5;
    st.skill = '';
    mathExamSetView('setup');
  };
  ov.foot.querySelector('#math-exam-to-cheat').onclick = () => {
    st.cheatSheet = mathExamBuildCheatSheet(data, st.diagnosisDays);
    mathExamSetView('cheatsheet');
  };
}

function mathExamBuildCheatSheet(data, days) {
  const span = days === 0 ? 0 : (days || 90);
  const diag = data || mathExamDiagnosisData(span);
  const cutoff = span ? Date.now() - span * MATH_DAY_MS : 0;
  const weak = diag.weak.length ? diag.weak : diag.skills.slice(0, 5);
  const lines = ['# 数学期末考前速查', '',
    '> 基于' + (span ? '最近 ' + span + ' 天' : '全部历史')
    + '的练习、模拟卷与错题自动整理；不是官方考纲。', ''];
  if (!weak.length) {
    lines.push('还没有足够数据。先做几道题或一套模拟卷，再生成速查。');
    return lines.join('\n');
  }
  weak.forEach((s, i) => {
    lines.push('## ' + (i + 1) + '. ' + (s.course_name || '') + ' · ' + s.skill, '',
      '- 近期正确率：' + s.accuracy + '%（' + s.correct + '/' + s.attempts + '）');
    const ws = (math.store.wrong || []).filter((w) => w.skill === s.skill
      && (!s.course || w.course === s.course)
      && (!cutoff || !w.at || w.at >= cutoff)).slice(0, 3);
    if (ws.length) {
      ws.forEach((w) => {
        lines.push('', '### 错题', '', '**题目**：' + w.prompt,
          '', '**正确答案**：' + w.answer, '', '**解析**：' + (w.explain || '暂无'));
        if (w.exam_tip) lines.push('', '**考试套路**：' + w.exam_tip);
        if (w.cause) lines.push('', '**上次错因**：' + w.cause);
      });
    } else {
      lines.push('', '- 暂无对应错题，建议做 5 道同类题。');
    }
    lines.push('');
  });
  return lines.join('\n');
}

function mathExamRenderCheatSheet() {
  const ov = math.overlay;
  const st = math.exam;
  const text = st.cheatSheet || mathExamBuildCheatSheet(null, st.diagnosisDays);
  st.cheatSheet = text;
  ov.body.innerHTML = '<div class="math-exam-section">考前速查</div>'
    + '<div class="math-note-preview rendered" id="math-exam-cheat"></div>';
  const box = ov.body.querySelector('#math-exam-cheat');
  if (typeof safeRenderMarkdown === 'function') box.innerHTML = safeRenderMarkdown(text);
  else box.textContent = text;
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn" id="math-exam-cheat-copy">复制 Markdown</button>'
    + '<button type="button" class="math-btn primary" id="math-exam-cheat-download">下载 .md</button>';
  ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('home');
  ov.foot.querySelector('#math-exam-cheat-copy').onclick = () => mathCopyText(text);
  ov.foot.querySelector('#math-exam-cheat-download').onclick = () => {
    mathDownloadNotes(text, '数学期末速查-' + mathNoteStamp() + '.md');
  };
}

function mathExamRenderSprint() {
  const ov = math.overlay;
  const st = math.exam;
  const sprint = math.store.sprint;
  let daysLeft = null;
  if (sprint && sprint.date) {
    const exam = new Date(sprint.date + 'T00:00:00');
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    if (!isNaN(exam.getTime())) daysLeft = Math.ceil((exam - today) / MATH_DAY_MS);
  }
  ov.body.innerHTML = '<div class="math-exam-diagnosis-head">'
    + '<div class="math-exam-diagnosis-title">7 天冲刺</div>'
    + '<div class="math-exam-seg"><input type="date" id="math-exam-date" value="'
    + mathExamEsc(st.sprintDate || (sprint && sprint.date) || '') + '"></div></div>'
    + (daysLeft == null ? '<div class="math-exam-note">选择考试日期后生成计划。</div>'
      : '<div class="math-exam-note">距离考试还有 <b>' + Math.max(0, daysLeft)
        + '</b> 天' + (daysLeft < 0 ? '（日期已过）' : '') + '。</div>')
    + (sprint && sprint.tasks && sprint.tasks.length
      ? '<div class="math-exam-sprint-list">' + sprint.tasks.map((t, i) => '<label class="math-exam-task">'
        + '<input type="checkbox" data-task="' + i + '"' + (t.done ? ' checked' : '') + '>'
        + '<span class="math-exam-task-day">Day ' + t.day + '</span>'
        + '<span class="math-exam-task-body"><b>' + mathExamEsc(t.title) + '</b>'
        + '<em>' + mathExamEsc(t.detail) + '</em></span></label>').join('') + '</div>'
      : '<div class="math-panel-note">还没有冲刺计划。</div>');
  ov.body.querySelectorAll('[data-task]').forEach((box) => {
    box.onchange = () => {
      const i = Number(box.dataset.task);
      if (math.store.sprint && math.store.sprint.tasks[i]) {
        math.store.sprint.tasks[i].done = box.checked;
        math.store.sprint.updatedAt = Date.now();
        mathSaveStore();
      }
    };
  });
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn" id="math-exam-sprint-clear">清除计划</button>'
    + '<button type="button" class="math-btn primary" id="math-exam-sprint-gen">生成 7 天计划</button>';
  ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('home');
  ov.foot.querySelector('#math-exam-sprint-gen').onclick = () => {
    const input = document.getElementById('math-exam-date');
    const date = input ? input.value : '';
    if (!date) { toast('先选择考试日期', 'error'); return; }
    st.sprintDate = date;
    const diag = mathExamDiagnosisData(90);
    const weak = diag.weak.slice(0, 3).map((s) => s.skill || '薄弱知识点');
    const tasks = [
      { day: 1, title: '薄弱点 1：' + (weak[0] || '基础概念'), detail: '看 2 道概念题 + 做 10 道同类题', done: false },
      { day: 2, title: '薄弱点 2：' + (weak[1] || '计算基础'), detail: '做 10 道计算题，限时完成', done: false },
      { day: 3, title: '薄弱点 3：' + (weak[2] || '错题回顾'), detail: '错题本复习 10 道，标出错因', done: false },
      { day: 4, title: '计算速度训练', detail: '做 15 道计算题，记录每题用时', done: false },
      { day: 5, title: '整套模拟卷', detail: '按期末时长完成一套模拟卷', done: false },
      { day: 6, title: '错题重练', detail: '把模拟卷错题加入错题本并重做', done: false },
      { day: 7, title: '考前速查 + 轻量复习', detail: '看速查笔记，做 5 道基础题保持手感', done: false },
    ];
    math.store.sprint = { date: date, tasks: tasks, updatedAt: Date.now() };
    mathSaveStore();
    mathExamRenderSprint();
    toast('已生成 7 天冲刺计划');
  };
  ov.foot.querySelector('#math-exam-sprint-clear').onclick = () => {
    if (!confirm('清除当前冲刺计划？')) return;
    math.store.sprint = null;
    st.sprintDate = '';
    mathSaveStore();
    mathExamRenderSprint();
  };
}

/* ---------------- 证明训练 ---------------- */

async function mathExamLoadProofs(course) {
  const st = mathExamState();
  st.proof.loading = true;
  st.proof.course = course || '';
  mathExamRender();
  try {
    const r = await api('/api/hall/math/proofs', { course: st.proof.course });
    st.proof.list = r.proofs || [];
  } catch (e) {
    toast('加载证明题失败：' + (e.message || e), 'error');
    st.proof.list = [];
  } finally {
    st.proof.loading = false;
    mathExamRender();
  }
}

function mathExamRenderProof() {
  const ov = math.overlay;
  const st = math.exam;
  const pf = st.proof;
  if (pf.current) {
    mathExamRenderProofDetail();
    return;
  }
  const courses = (st.meta && st.meta.courses) || [];
  const opts = ['<option value="">全部课程</option>'].concat(courses.map((c) =>
    '<option value="' + c.id + '"' + (c.id === pf.course ? ' selected' : '') + '>'
    + mathExamEsc(c.name) + '</option>')).join('');
  ov.body.innerHTML = '<div class="math-exam-diagnosis-head">'
    + '<div class="math-exam-diagnosis-title">证明训练</div>'
    + '<div class="math-exam-seg"><select id="math-exam-proof-course">' + opts
    + '</select></div></div>'
    + '<div class="math-exam-note">AI 按步骤点评，只评价你实际写出的内容；'
    + '不是形式化验证，也不能替代老师阅卷。</div>'
    + (pf.loading ? '<div class="math-loading"><span class="math-spinner"></span>加载证明题…</div>'
      : (pf.list && pf.list.length ? '<div class="math-exam-proof-list">'
        + pf.list.map((p) => '<button type="button" class="math-exam-proof" data-proof="'
          + mathExamEsc(p.id) + '"><b>' + mathExamEsc(p.prompt) + '</b><span>'
          + mathExamEsc(p.course_name || '') + ' · ' + mathExamEsc(p.skill || '')
          + ' · 难度 ' + '★'.repeat(Math.max(1, p.difficulty || 2)) + '</span></button>').join('')
        + '</div>' : '<div class="math-panel-note">没有这个课程的证明题。</div>'));
  const sel = ov.body.querySelector('#math-exam-proof-course');
  if (sel) sel.onchange = () => mathExamLoadProofs(sel.value);
  ov.body.querySelectorAll('[data-proof]').forEach((b) => {
    b.onclick = () => {
      pf.current = (pf.list || []).find((x) => x.id === b.dataset.proof) || null;
      pf.text = '';
      pf.result = null;
      mathExamRenderProofDetail();
    };
  });
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-back">返回</button>';
  ov.foot.querySelector('#math-exam-back').onclick = () => mathExamSetView('home');
  if (pf.list == null && !pf.loading) mathExamLoadProofs(pf.course);
}

function mathExamRenderProofDetail() {
  const ov = math.overlay;
  const st = math.exam;
  const pf = st.proof;
  const p = pf.current;
  if (!p) { pf.current = null; mathExamRenderProof(); return; }
  if (pf.result) {
    const r = pf.result;
    ov.body.innerHTML = '<div class="math-exam-proof-prompt">' + mathExamEsc(p.prompt) + '</div>'
      + '<div class="math-exam-proof-result-head"><b>' + (r.score == null ? '未评分' : r.score + ' 分')
      + '</b><span>' + mathExamEsc(r.verdict || '') + ' · 置信度 ' + mathExamEsc(r.confidence || '')
      + '</span></div>'
      + (r.steps && r.steps.length ? '<div class="math-exam-section">步骤点评</div>'
        + '<div class="math-exam-proof-steps">' + r.steps.map((s) => '<div class="math-exam-proof-step '
          + (s.status === '有' ? 'ok' : (s.status === '部分' ? 'part' : 'miss')) + '">'
          + '<b>' + mathExamEsc(s.point) + '</b><span>' + mathExamEsc(s.status) + '</span>'
          + '<em>' + mathExamEsc(s.comment) + '</em></div>').join('') + '</div>' : '')
      + (r.missing && r.missing.length ? '<div class="math-exam-section">缺失的关键点</div>'
        + '<ul class="iv-list">' + r.missing.map((x) => '<li>' + mathExamEsc(x) + '</li>').join('') + '</ul>' : '')
      + (r.suggestions && r.suggestions.length ? '<div class="math-exam-section">建议</div>'
        + '<ul class="iv-list">' + r.suggestions.map((x) => '<li>' + mathExamEsc(x) + '</li>').join('') + '</ul>' : '')
      + (r.outline && r.outline.length ? '<div class="math-exam-section">标准步骤</div>'
        + '<ol class="math-steps">' + r.outline.map((x) => '<li>' + mathExamEsc(x) + '</li>').join('') + '</ol>' : '')
      + '<div class="math-exam-note">' + mathExamEsc(r.disclaimer || '') + '</div>';
    ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-proof-back">返回题目列表</button>'
      + '<span class="math-modal-spacer"></span>'
      + '<button type="button" class="math-btn primary" id="math-exam-proof-again">修改后重评</button>';
    ov.foot.querySelector('#math-exam-proof-back').onclick = () => {
      pf.current = null; pf.result = null; pf.text = ''; mathExamRenderProof();
    };
    ov.foot.querySelector('#math-exam-proof-again').onclick = () => {
      pf.result = null; mathExamRenderProofDetail();
    };
    return;
  }
  ov.body.innerHTML = '<div class="math-exam-proof-prompt">' + mathExamEsc(p.prompt) + '</div>'
    + (p.hints && p.hints.length ? '<div class="math-exam-proof-hints">'
      + p.hints.map((x) => '<span>' + mathExamEsc(x) + '</span>').join('') + '</div>' : '')
    + '<textarea id="math-exam-proof-text" class="math-textarea math-exam-proof-text"'
    + ' spellcheck="false" placeholder="写下你的证明过程，尽量写清每一步的依据…">'
    + mathExamEsc(pf.text || '') + '</textarea>'
    + '<div class="math-exam-note">AI 只根据你写出的内容点评；没写的步骤不会替你补。</div>';
  const ta = ov.body.querySelector('#math-exam-proof-text');
  if (ta) ta.addEventListener('input', () => { pf.text = ta.value; });
  ov.foot.innerHTML = '<button type="button" class="math-btn" id="math-exam-proof-back">返回列表</button>'
    + '<span class="math-modal-spacer"></span>'
    + '<button type="button" class="math-btn primary" id="math-exam-proof-submit">提交点评</button>';
  ov.foot.querySelector('#math-exam-proof-back').onclick = () => {
    pf.current = null; mathExamRenderProof();
  };
  ov.foot.querySelector('#math-exam-proof-submit').onclick = mathExamProofSubmit;
}

async function mathExamProofSubmit() {
  const st = mathExamState();
  const pf = st.proof;
  if (!pf.current || pf.loading) return;
  const ta = document.getElementById('math-exam-proof-text');
  const text = ta ? ta.value.trim() : (pf.text || '').trim();
  if (text.length < 20) { toast('证明太短，先写下关键步骤', 'error'); return; }
  pf.text = text;
  pf.loading = true;
  mathExamRenderProofDetail();
  try {
    const r = await api('/api/hall/math/proof/grade', {
      proof_id: pf.current.id, text: text,
    });
    pf.result = r;
  } catch (e) {
    toast('点评失败：' + (e.message || e), 'error');
  } finally {
    pf.loading = false;
    mathExamRenderProofDetail();
  }
}

