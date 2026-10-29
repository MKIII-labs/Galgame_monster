/* ============================================================
   功能大厅：模拟面试
   ------------------------------------------------------------
   四个页签：
     - 简历工作台：分析 / AI 引导制作 / 润色 / JD 匹配
     - 模拟面试：浏览器 TTS 提问 + 本地流式语音识别作答 + 复盘报告
     - 专项训练：本地题库单题练习，AI 点评
     - 复盘记录：最近 20 场面试的评分与逐题记录

   数据只存在本机 localStorage（gm.interview.v1）：
   简历、引导问答、面试记录、设置都在这里，可在「设置」里一键清空。
   面试会话本身在服务端进程内（app/interview.py），刷新页面会丢，
   但结束后生成的报告会立刻写入本地记录。
   ============================================================ */
'use strict';

const IV_KEY = 'gm.interview.v1';
const IV_HISTORY_LIMIT = 20;

const IV_TYPES = { hr: 'HR 行为面', technical: '专业技术面', stress: '压力面', mixed: '综合面' };
const IV_LANGS = { zh: '中文', en: 'English' };
const IV_POLISH_MODES = {
  impact: '强化成果量化',
  concise: '精简去冗余',
  professional: '专业书面表达',
  star: '按 STAR 展开',
};
const IV_BUILDER_STEPS = [
  { key: 'basic', title: '基本信息', hint: '姓名、城市、联系方式、求职方向。没有的信息可以留空。',
    ph: '例：张三，上海，138xxxx，求职方向：前端开发' },
  { key: 'education', title: '教育背景', hint: '学校、专业、时间、GPA / 排名、相关课程。',
    ph: '例：XX 大学 计算机科学与技术 2021.09-2025.06，GPA 3.7/4.0' },
  { key: 'experience', title: '实习 / 工作经历', hint: '公司、岗位、时间；每段做了什么、怎么做、结果数字。',
    ph: '例：XX 公司 前端实习生 2024.07-2024.10，负责……，把加载时间从 3s 降到 1.2s' },
  { key: 'projects', title: '项目经历', hint: '项目背景、你的角色、技术 / 方法、结果。',
    ph: '例：校园二手交易平台，独立完成前端，日活 800+' },
  { key: 'skills', title: '技能与证书', hint: '语言、工具、证书、奖项。',
    ph: '例：JavaScript / Vue / Git；CET-6；校级一等奖学金' },
  { key: 'extra', title: '补充信息', hint: '追问的答案、临时想到的事实、证书细节都可以放这里；内容会自动保存在本机。',
    ph: '例：项目起止时间 2024.03-2024.06；首屏优化用了路由懒加载和图片压缩' },
];

// 跳过的固定答句：后端 app/interview.py 的 _SKIP_ANSWER 用它识别「待补项」
const IV_SKIP_ANSWER = '（跳过这题）';

/* 悬浮说明：给应届生解释专业名词与每个按钮的作用。
   文案集中在这里，改文案不用动各处 HTML。 */
const IV_HELP = {
  heroStats: '简历评分、已完成面试场次、历史最佳复盘分。',
  settings: '语音、默认面试参数与本地数据管理。',
  tabResume: '简历分析、AI 引导制作、润色和 JD 匹配。',
  tabInterview: '配置岗位后开始一场语音 / 文字模拟面试。',
  tabCareer: '职业方向探索、方向对比和 3/6/12 个月行动计划。',
  tabDrill: '从本地题库挑一道题单独练习。',
  tabHistory: '查看自动保存的历史面试复盘。',
  role: '你准备投的职位名称。AI 会按这个岗位评估简历、生成面试题。',
  company: '目标公司，可不填。填了之后面试官会结合公司业务提问。',
  upload: '支持 txt / md / docx / 文本型 PDF。扫描版 PDF 读不出文字，请直接粘贴简历内容。',
  resume: '先写事实：学校、经历、项目、你做了什么、结果数字。AI 只改表达，不会替你编造。',
  jd: 'JD = Job Description，岗位描述 / 招聘要求。把招聘网站上的职位详情复制进来，用于匹配度分析和针对性出题。',
  analyze: '从完整度、结构清晰、成果量化、岗位匹配、表达专业五个维度打分，并列出可定位的问题。',
  build: '用 5 个问题引导你把真实经历整理成简历；信息不够会继续追问，不会编造公司或数字。',
  polishMode: '选择改写风格：量化成果、精简、专业书面表达，或按 STAR 结构展开。',
  polish: '逐条改写并说明理由，你可以只接受其中几条，不会整篇覆盖。',
  jdMatch: '对照招聘要求，看简历覆盖了哪些关键词、还缺什么、面试可能问什么。',
  clear: '删除本机保存的简历与分析结果，不影响已经生成的面试记录。',
  viewEdit: '编辑简历原文（Markdown 源码）。',
  viewPreview: '渲染后的阅读视图：标题、列表、加粗和公式按排版显示。',
  builderSave: '引导问答的每一步都会自动保存在本机，关闭弹窗后再次打开仍在。',
  builderReset: '清空本次访谈的问答与已填栏目，重新从第一个问题开始；不影响简历正文、分析结果和面试记录。',
  builderAi: 'AI 一次问一个问题，帮你想起来有哪些可以写进简历的经历。',
  builderForm: '自己按固定栏目填写，适合已经想好要写什么的人。',
  aiHint: '让 AI 给回忆线索和例子，不会替你编造经历。',
  aiSkip: '跳过这题，AI 会继续问下一类经历；跳过的题目会列进生成结果的「还需要你补充」，也可以把鼠标移到那条回答上点「撤回」重新答。',
  builderMic: '语音作答：点一下开始说话，边说边出字；再点一下停止并转写。用的是主界面同一套本地识别。',
  importProfile: '把简历原文按栏目整理进六个栏目，接着用「快速填写」或「AI 访谈」补全；原文里没有的栏目不会被覆盖。',
  stale: '简历正文、目标岗位或 JD 改过了，下面的结果还是按旧内容算的。',
  careerFit: '匹配度由 AI 根据你提供的信息估算，不是正式测评，建议用实习或小项目验证。',
  careerVerify: '低成本验证：先做一件小事，再决定要不要投入更多。',
  careerLink: '把选定方向填入目标岗位，直接开始模拟面试。',
  careerExport: '把方向、对比和行动计划导出为 Markdown。',
  careerRedo: '清空本轮盘点和规划，重新开始。',
  score: '综合评分，由五个维度加权得到。',
  severity: '问题严重程度：高 = 影响筛选，中 = 明显减分，低 = 可优化。',
  followup: 'AI 认为还缺的关键信息。补进「补充信息」后可以重新生成简历。',
  warning: 'AI 对事实完整性的提醒，例如缺少时间、数字或某项岗位要求。',
  buildRegen: '把补充信息一起交给 AI，重新整理一版简历。',
  buildUse: '把这份简历放回工作台，不会自动发起分析。',
  buildUseAnalyze: '放回简历工作台，并立刻按新内容重新分析一次。',
  followupFill: '把这条追问写进「补充信息」，你只要在后面补答案。',
  gapResume: '回到 AI 访谈，针对这一块继续问一题，补完再重新生成。',
  importWarn: '这些栏目在原文里没找到，可以手动补。',
  extraInfo: '补充信息会自动保存在本机，重新生成时会一起发给 AI。',
  copy: '复制到剪贴板。',
  download: '下载为 Markdown 文件，可在笔记软件中继续编辑。',

  type: 'HR 行为面看经历与动机；专业技术面深挖技能；压力面故意质疑；综合面混合出题。',
  lang: '面试官提问和点评使用的语言；英文模式会同时切换英文题库。',
  difficulty: '1–5，越高问题越深、追问越紧。应届生建议从 2–3 开始。',
  count: '主问题数量。面试官可能追加追问，追问不占用主问题名额。',
  useResume: '勾选后问题会围绕你的简历和项目经历，更接近真实面试。',
  tts: '用浏览器自带语音朗读题目，不需要额外配置；不支持时会自动降级为字幕。',
  speechCheck: '检查本地语音识别模型是否就绪；未就绪时可以直接打字作答。',
  start: '开始一场面试。开始后可以随时结束并生成复盘报告。',
  progress: '当前主问题进度。AI 的追问不占用主问题名额，所以进度可能暂时不变。',
  timer: '本场面试已经进行的时间。',
  end: '结束本场面试并生成复盘报告，报告会保存到「复盘记录」。',
  replay: '再朗读一遍当前题目。',
  hint: '给出答题结构建议，不会直接给标准答案。',
  mic: '点一下开始录音，再点一下停止并转写；模型未就绪时可打字。',
  skipQuestion: '这题先跳过：不计分、不计语速，直接进入下一题。',
  answer: '先给结论，再用 STAR 展开：情境、任务、行动、结果。语音作答会自动转成文字。',
  submit: '提交回答后，AI 会给本题分数和现场反馈，并决定是否追问。',
  feedback: '本题的即时反馈与得分，不代表最终复盘总分。',
  overallScore: '本场面试的综合评分，由五个维度汇总。',
  expression: '语速 = 每分钟字数；口头禅 = 嗯 / 那个 / 然后等；中文参考 180–300 字/分。',
  perQuestion: '逐题回顾：本题得分、点评，以及可参考的回答结构。',
  reportCopy: '复制 Markdown 格式的复盘报告。',
  reportDownload: '下载 .md 文件，可在笔记软件中继续编辑。',
  reportAgain: '保留历史记录，回到面试设置再来一场。',

  drill: '从题库挑一道题单独练，适合碎片时间；提交后 AI 会点评并给参考回答。',
  refresh: '重新加载这一批题目。',
  drillItem: '点击开始单题练习，计时从进入题目开始。',
  back: '返回上一级。',
  again: '不换题，重新作答一次。',
  next: '进入下一题；练完这一批会自动换新题。',

  history: '自动保存最近 20 场面试的复盘报告，只存在本机。',
  trend: '每根柱代表一场面试的总分，从左到右按时间排列。',
  historyScore: '该场面试的综合评分。',
  expand: '展开查看完整复盘：维度评分、表达力、逐题点评。',
  export: '把本地数据导出为 JSON 文件，便于备份。',
  clearData: '删除本机保存的数据，操作不可撤销。',
  delete: '删除这一条记录，操作不可撤销。',

  rate: '面试官朗读速度，1.0 为正常速度。',
  speechStatus: '显示本地流式语音识别模型是否已下载并可加载。',
  data: '本机目前保存的简历字数和面试场次数。',
};

/* 维度名 → 说明 */
const IV_DIM_HELP = {
  '完整度': '简历板块是否齐全：教育、经历、项目、技能、联系方式。',
  '结构清晰': '板块顺序、要点句、排版是否让招聘方一眼看到重点。',
  '成果量化': '经历里是否有可核实的数字：规模、比例、时长、结果。',
  '岗位匹配': '简历关键词与目标岗位 / JD 的贴合程度。',
  '表达专业': '是否用动词开头、书面表达，是否有空话。',
  '表达清晰': '语速、口头禅、吐字与信息完整度，是否让人听明白。',
  '逻辑结构': '回答是否有结论、分层和因果，常用 STAR 结构。',
  '专业能力': '回答里的知识、方法、工具和项目细节是否扎实。',
  '应变抗压': '面对追问、质疑和压力问题时的反应与稳定度。',
};

function ivTip(key) {
  const text = IV_HELP[key];
  return text ? ' data-iv-tip="' + ivEsc(text) + '"' : '';
}


const iv = {
  gen: 0,
  el: {},
  tab: 'resume',
  store: null,
  busy: false,
  resumeView: 'edit',        // edit | preview
  saveTimer: null,
  analysis: null,
  polish: null,
  jdMatch: null,
  builder: null,
  guideFocus: '',
  session: null,
  reportView: null,
  finishing: false,
  overlay: null,
  roomTimer: null,
  speakTimer: null,
  onDocClick: null,
  onScroll: null,
  drill: { items: [], type: 'mixed', lang: 'zh', current: null, result: null, startedAt: 0 },
  career: null,
  mic: { active: false, inputId: '', pending: [], text: '', sid: '', sending: false },
};

hallRegister({
  id: 'interview',
  name: '模拟面试',
  icon: 'i-chat',
  tag: '求职',
  desc: '简历分析、AI 引导制作与润色，再用真实语音做一场模拟面试，结束后拿到五维复盘报告。',
  meta: '简历工坊 · 语音面试 · JD 匹配 · 专项训练 · 复盘',
  mount: interviewMount,
  unmount: interviewUnmount,
});

/* ---------------- 本地存档 ---------------- */

function ivDefaultStore() {
  return {
    version: 1,
    resume: { text: '', role: '', jd: '', analysis: null, polish: null, jdMatch: null, updatedAt: 0 },
    profile: { basic: '', education: '', experience: '', projects: '', skills: '', extra: '' },
    resumeGuide: { qa: [], step: 0, current: '', done: false, formStep: 0,
      draft: '', mode: 'ai', facts: {}, coverage: {}, field: '', updatedAt: 0 },
    career: { qa: [], step: 0, current: '', plan: null, selected: '', draft: '', updatedAt: 0 },
    settings: { tts: true, rate: 1, lang: 'zh', type: 'mixed', count: 5, difficulty: 3, useResume: true },
    history: [],
  };
}

function ivNormalizeQa(list) {
  const out = [];
  (Array.isArray(list) ? list : []).slice(-30).forEach((it) => {
    if (!it || typeof it !== 'object') return;
    const q = String(it.q || it.question || '').slice(0, 500);
    const a = String(it.a || it.answer || '').slice(0, 2000);
    const f = String(it.f || it.field || '').slice(0, 20);
    if (q || a) out.push({ q: q, a: a, f: f, at: Number(it.at) || 0 });
  });
  return out;
}

/* AI 访谈的五个进度板块：和 app/interview.py 的 GUIDE_SECTIONS 对应 */
const IV_GUIDE_SECTIONS = ['basic', 'education', 'experience', 'projects', 'skills'];
const IV_GUIDE_LABELS = {
  basic: '基本', education: '教育', experience: '经历',
  projects: '项目', skills: '技能', extra: '其他',
};

function ivNormalizeFacts(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  Object.keys(raw).forEach((k) => {
    const arr = Array.isArray(raw[k]) ? raw[k] : [raw[k]];
    const vals = [];
    arr.slice(0, 8).forEach((it) => {
      const s = String(it && typeof it === 'object'
        ? (it.text || it.content || '') : (it == null ? '' : it)).trim().slice(0, 300);
      if (s && vals.indexOf(s) < 0) vals.push(s);
    });
    if (vals.length) out[k] = vals;
  });
  return out;
}

function ivMergeFacts(a, b) {
  const out = {};
  [ivNormalizeFacts(a), ivNormalizeFacts(b)].forEach((part) => {
    Object.keys(part).forEach((k) => {
      const vals = out[k] || (out[k] = []);
      part[k].forEach((s) => {
        if (vals.indexOf(s) < 0 && vals.length < 8) vals.push(s);
      });
    });
  });
  return out;
}

function ivNormalizeCoverage(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  Object.keys(raw).forEach((k) => {
    const n = Number(raw[k]);
    if (isFinite(n)) out[k] = Math.max(0, Math.min(100, Math.round(n)));
  });
  return out;
}

function ivNormalizeGuide(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  return {
    qa: ivNormalizeQa(r.qa),
    step: Math.max(0, Number(r.step) || 0),
    current: String(r.current || ''),
    done: !!r.done,
    formStep: Math.max(0, Number(r.formStep) || 0),
    draft: String(r.draft || ''),
    mode: r.mode === 'form' ? 'form' : 'ai',
    facts: ivNormalizeFacts(r.facts),
    coverage: ivNormalizeCoverage(r.coverage),
    field: String(r.field || ''),
    updatedAt: Number(r.updatedAt) || 0,
  };
}

function ivNormalizeCareer(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  return {
    qa: ivNormalizeQa(r.qa),
    step: Math.max(0, Number(r.step) || 0),
    current: String(r.current || ''),
    plan: r.plan && typeof r.plan === 'object' ? r.plan : null,
    selected: String(r.selected || ''),
    draft: String(r.draft || ''),
    updatedAt: Number(r.updatedAt) || 0,
  };
}

function ivNormalizeStore(raw) {
  const def = ivDefaultStore();
  if (!raw || typeof raw !== 'object') return def;
  const r = raw.resume && typeof raw.resume === 'object' ? raw.resume : {};
  const p = raw.profile && typeof raw.profile === 'object' ? raw.profile : {};
  const s = raw.settings && typeof raw.settings === 'object' ? raw.settings : {};
  const hist = Array.isArray(raw.history) ? raw.history.filter((h) => h && typeof h === 'object') : [];
  return {
    version: 1,
    resume: {
      text: String(r.text || ''),
      role: String(r.role || ''),
      jd: String(r.jd || ''),
      analysis: r.analysis && typeof r.analysis === 'object' ? r.analysis : null,
      polish: r.polish && typeof r.polish === 'object' ? r.polish : null,
      jdMatch: r.jdMatch && typeof r.jdMatch === 'object' ? r.jdMatch : null,
      updatedAt: Number(r.updatedAt) || 0,
    },
    profile: {
      basic: String(p.basic || ''), education: String(p.education || ''),
      experience: String(p.experience || ''), projects: String(p.projects || ''),
      skills: String(p.skills || ''), extra: String(p.extra || ''),
    },
    resumeGuide: ivNormalizeGuide(raw.resumeGuide),
    career: ivNormalizeCareer(raw.career),
    settings: {
      tts: s.tts !== false,
      rate: Math.max(0.6, Math.min(1.6, Number(s.rate) || 1)),
      lang: IV_LANGS[s.lang] ? s.lang : 'zh',
      type: IV_TYPES[s.type] ? s.type : 'mixed',
      count: [3, 5, 7, 10].indexOf(Number(s.count)) >= 0 ? Number(s.count) : 5,
      difficulty: Math.max(1, Math.min(5, Number(s.difficulty) || 3)),
      useResume: s.useResume !== false,
    },
    history: hist.slice(0, IV_HISTORY_LIMIT),
  };
}

function ivLoadStore() {
  try {
    const raw = JSON.parse(localStorage.getItem(IV_KEY) || 'null');
    return ivNormalizeStore(raw);
  } catch (e) {
    return ivDefaultStore();
  }
}

let ivSaveWarned = false;

function ivSaveStore() {
  try {
    localStorage.setItem(IV_KEY, JSON.stringify(iv.store));
    ivSaveWarned = false;
  } catch (e) {
    // 配额满 / 隐私模式：不能静默失败，否则用户以为已经存下来了
    if (!ivSaveWarned && typeof toast === 'function') {
      ivSaveWarned = true;
      toast('本机存储空间不足，最近的修改可能没有保存；可在设置里导出后清空旧记录', 'error');
    }
  }
}

/* 高频输入（引导问答 / 补充信息）用 300ms 防抖落盘，避免每个字符都写 localStorage */
function ivQueueSave() {
  if (iv.saveTimer) clearTimeout(iv.saveTimer);
  iv.saveTimer = setTimeout(() => {
    iv.saveTimer = null;
    if (iv.store) ivSaveStore();
  }, 300);
}

/* immediate=false 用于输入事件：只更新内存，落盘走 300ms 防抖，避免每敲一个字就写整份简历 */
function ivSaveResumeFields(immediate) {
  const text = document.getElementById('iv-resume-text');
  const role = document.getElementById('iv-resume-role');
  const jd = document.getElementById('iv-resume-jd');
  if (text) iv.store.resume.text = text.value;
  if (role) iv.store.resume.role = role.value.trim();
  if (jd) iv.store.resume.jd = jd.value;
  iv.store.resume.updatedAt = Date.now();
  if (immediate === false) ivQueueSave();
  else ivSaveStore();
}

/* 简历内容指纹：用来判断分析 / 润色 / JD 匹配结果是不是已经过期 */
function ivFp() {
  const r = iv.store.resume;
  const s = (r.text || '') + '\u0000' + (r.role || '') + '\u0000' + (r.jd || '');
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h.toString(36) + ':' + s.length;
}

function ivHasStale() {
  const fp = ivFp();
  return !!((iv.analysis && iv.analysis.fp && iv.analysis.fp !== fp)
    || (iv.polish && iv.polish.fp && iv.polish.fp !== fp)
    || (iv.jdMatch && iv.jdMatch.fp && iv.jdMatch.fp !== fp));
}

/* ---------------- 通用小工具 ---------------- */

function ivEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function ivMd(src) {
  if (typeof safeRenderMarkdown === 'function') return safeRenderMarkdown(String(src || ''));
  return '<pre class="iv-md-plain">' + ivEsc(src) + '</pre>';
}

function ivDate(ts) {
  const d = new Date(Number(ts) || Date.now());
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function ivDuration(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const p = (n) => String(n).padStart(2, '0');
  return p(Math.floor(s / 60)) + ':' + p(s % 60);
}

function ivCopy(text) {
  const t = String(text || '');
  // 老式兜底：Clipboard API 被拒（窗口没焦点 / 权限）时退回 execCommand
  const legacy = () => new Promise((resolve, reject) => {
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); resolve(); } catch (e) { reject(e); }
    ta.remove();
  });
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(t).catch(legacy);
  }
  return legacy();
}

/* Ctrl/Cmd + Enter 直接触发提交类按钮 */
function ivHotkey(el, fn) {
  if (!el) return;
  el.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      fn();
    }
  });
}

function ivDownload(name, text) {
  try {
    const blob = new Blob([String(text || '')], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    toast('下载失败：' + (e.message || e), 'error');
  }
}

/* ---------------- 悬浮提示 ---------------- */

let ivTipBox = null;
let ivTipTarget = null;

function ivTipShow(target) {
  const text = target && target.getAttribute('data-iv-tip');
  if (!text) return;
  if (!ivTipBox) {
    ivTipBox = document.createElement('div');
    ivTipBox.className = 'iv-tooltip';
    ivTipBox.setAttribute('role', 'tooltip');
    document.body.appendChild(ivTipBox);
  }
  ivTipTarget = target;
  ivTipBox.textContent = text;
  ivTipBox.classList.add('show');
  ivTipPlace(target);
}

function ivTipPlace(target) {
  if (!ivTipBox) return;
  const r = target.getBoundingClientRect();
  const gap = 8;
  const maxW = Math.max(180, Math.min(300, window.innerWidth - 24));
  ivTipBox.style.maxWidth = maxW + 'px';
  const tr = ivTipBox.getBoundingClientRect();
  let left = r.left + r.width / 2 - tr.width / 2;
  left = Math.max(10, Math.min(window.innerWidth - tr.width - 10, left));
  let top = r.top - tr.height - gap;
  let below = false;
  if (top < 10) {
    top = r.bottom + gap;
    below = true;
  }
  ivTipBox.style.left = Math.round(left) + 'px';
  ivTipBox.style.top = Math.round(top) + 'px';
  ivTipBox.classList.toggle('below', below);
}

function ivTipHide() {
  if (ivTipBox) ivTipBox.classList.remove('show');
  ivTipTarget = null;
}

function ivTipBind(root) {
  if (!root || root.dataset.ivTipBound) return;
  root.dataset.ivTipBound = '1';
  root.addEventListener('mouseover', (e) => {
    const t = e.target.closest && e.target.closest('[data-iv-tip]');
    if (t && root.contains(t)) ivTipShow(t);
  });
  root.addEventListener('mouseout', (e) => {
    const t = e.target.closest && e.target.closest('[data-iv-tip]');
    if (!t) return;
    if (e.relatedTarget && t.contains(e.relatedTarget)) return;
    ivTipHide();
  });
  root.addEventListener('focusin', (e) => {
    const t = e.target.closest && e.target.closest('[data-iv-tip]');
    if (t && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) ivTipShow(t);
  });
  root.addEventListener('focusout', () => ivTipHide());
  // 手机 / 触屏：点一下显示，再点一下或点空白关闭（输入框不弹，避免挡住打字）
  root.addEventListener('click', (e) => {
    if (!window.matchMedia || !window.matchMedia('(hover: none)').matches) return;
    const t = e.target.closest && e.target.closest('[data-iv-tip]');
    if (!t || /^(INPUT|TEXTAREA)$/.test(t.tagName)) return;
    // 放到下一拍：按钮可能触发重渲染，元素已被替换时就不再弹
    setTimeout(() => {
      if (!t.isConnected) return;
      if (ivTipTarget === t) ivTipHide();
      else ivTipShow(t);
    }, 0);
  });
}

/* ---------------- 弹窗 ---------------- */

function ivOpenOverlay(title, sub) {
  ivCloseOverlay();
  const wrap = document.createElement('div');
  wrap.className = 'iv-modal';
  const card = document.createElement('div');
  card.className = 'iv-modal-card';
  card.innerHTML = '<div class="iv-modal-head"><div class="iv-modal-title">' + ivEsc(title) + '</div>'
    + '<button type="button" class="iv-modal-x" title="关闭">×</button></div>'
    + (sub ? '<div class="iv-modal-sub">' + ivEsc(sub) + '</div>' : '')
    + '<div class="iv-modal-body"></div><div class="iv-modal-foot"></div>';
  wrap.appendChild(card);
  wrap.addEventListener('click', (e) => { if (e.target === wrap) ivCloseOverlay(); });
  card.querySelector('.iv-modal-x').onclick = ivCloseOverlay;
  document.body.appendChild(wrap);
  ivTipBind(card);
  iv.overlay = { wrap: wrap, card: card,
    body: card.querySelector('.iv-modal-body'), foot: card.querySelector('.iv-modal-foot') };
  return iv.overlay;
}

function ivCloseOverlay() {
  if (!iv.overlay) return;
  // 弹窗关掉时把挂在引导制作输入框上的录音一起收掉，不然会继续录、继续写
  if (typeof micStopIf === 'function') micStopIf('iv-ai-answer');
  // 关掉之前把草稿 / 进度落盘，重开才能接着写
  if (iv.builder) ivSaveBuilderState();
  try { iv.overlay.wrap.remove(); } catch (e) { /* 忽略 */ }
  iv.overlay = null;
  iv.builder = null;
  iv.guideFocus = '';
}

/* ---------------- 挂载 / 卸载 ---------------- */

function interviewMount(root) {
  const gen = ++iv.gen;
  iv.el = { root: root };
  iv.store = ivLoadStore();
  iv.tab = 'resume';
  iv.resumeView = 'edit';
  iv.session = null;
  iv.reportView = null;
  iv.finishing = false;
  iv.analysis = iv.store.resume.analysis;
  iv.polish = iv.store.resume.polish;
  iv.jdMatch = iv.store.resume.jdMatch;
  iv.builder = null;
  iv.drill = { items: [], type: iv.store.settings.type, lang: iv.store.settings.lang,
    current: null, result: null, startedAt: 0 };
  const c = iv.store.career || {};
  iv.career = {
    qa: ivNormalizeQa(c.qa), step: Number(c.step) || 0,
    current: String(c.current || ''),
    plan: c.plan || null, selected: String(c.selected || ''),
    input: String(c.draft || ''), busy: false,
    guide: { question: String(c.current || ''), why: '', examples: [], hint: '',
      done: false, summary: '', loading: false },
  };

  root.innerHTML = ''
    + '<div class="iv-root">'
    + '  <div class="iv-hero">'
    + '    <div class="iv-hero-main"><div class="iv-hero-title">模拟面试</div>'
    + '      <div class="iv-hero-sub">先把简历讲清楚，再把回答说出来。</div></div>'
    + '    <div class="iv-hero-stats iv-tip-able"' + ivTip('heroStats') + ' id="iv-hero-stats"></div>'
    + '    <button type="button" class="iv-btn ghost small" id="iv-settings"'
    + ivTip('settings') + '>设置</button>'
    + '  </div>'
    + '  <div class="iv-tabs" id="iv-tabs">'
    + '    <button type="button" data-tab="resume"' + ivTip('tabResume') + '>简历工作台</button>'
    + '    <button type="button" data-tab="interview"' + ivTip('tabInterview') + '>模拟面试</button>'
    + '    <button type="button" data-tab="career"' + ivTip('tabCareer') + '>职业规划</button>'
    + '    <button type="button" data-tab="drill"' + ivTip('tabDrill') + '>专项训练</button>'
    + '    <button type="button" data-tab="history"' + ivTip('tabHistory') + '>复盘记录</button>'
    + '  </div>'
    + '  <div class="iv-panel" id="iv-panel"></div>'
    + '</div>';

  const tabs = root.querySelector('#iv-tabs');
  tabs.querySelectorAll('button').forEach((b) => {
    b.onclick = () => ivSetTab(b.dataset.tab);
  });
  root.querySelector('#iv-settings').onclick = ivOpenSettings;
  ivTipBind(root);
  iv.onDocClick = (e) => {
    if (!e.target.closest || !e.target.closest('[data-iv-tip]')) ivTipHide();
  };
  iv.onScroll = () => ivTipHide();
  document.addEventListener('click', iv.onDocClick);
  window.addEventListener('scroll', iv.onScroll, true);
  window.addEventListener('resize', iv.onScroll);
  ivRenderHero();
  ivSetTab('resume');
  // 语音合成音色列表是异步加载的，提前触发一次
  try { if (window.speechSynthesis) window.speechSynthesis.getVoices(); } catch (e) { /* 忽略 */ }
  if (gen !== iv.gen) return;
}

function interviewUnmount() {
  iv.gen += 1;
  ivClearRoomTimer();
  if (iv.saveTimer) { clearTimeout(iv.saveTimer); iv.saveTimer = null; ivSaveStore(); }
  ivMicStop(true);
  ivStopSpeak();
  ivCloseOverlay();
  ivTipHide();
  if (iv.onDocClick) {
    document.removeEventListener('click', iv.onDocClick);
    iv.onDocClick = null;
  }
  if (iv.onScroll) {
    window.removeEventListener('scroll', iv.onScroll, true);
    window.removeEventListener('resize', iv.onScroll);
    iv.onScroll = null;
  }
  iv.session = null;
  iv.el = {};
}

function ivClearRoomTimer() {
  if (iv.roomTimer) { clearInterval(iv.roomTimer); iv.roomTimer = null; }
}

function ivRenderHero() {
  const box = document.getElementById('iv-hero-stats');
  if (!box) return;
  const score = iv.analysis && iv.analysis.overall ? iv.analysis.overall : '—';
  const sessions = (iv.store.history || []).length;
  const best = (iv.store.history || []).reduce((m, h) => Math.max(m, Number(h.score) || 0), 0);
  box.innerHTML = ''
    + '<span class="iv-stat"><b>' + ivEsc(score) + '</b><i>简历评分</i></span>'
    + '<span class="iv-stat"><b>' + sessions + '</b><i>面试场次</i></span>'
    + '<span class="iv-stat"><b>' + (best || '—') + '</b><i>最佳复盘</i></span>';
}

function ivSetTab(tab) {
  if (['resume', 'interview', 'career', 'drill', 'history'].indexOf(tab) < 0) tab = 'resume';
  if (iv.tab !== tab) {
    ivStopSpeak();
    // 切走之前先收麦：输入框马上会被重建，继续录只会把转写结果丢掉
    if (iv.mic.active) {
      const stopping = ivMicStop(true);
      // 这段录音不属于任何一题，别让下一题的语速统计把它算进去
      if (stopping && stopping.then) stopping.then(() => { iv.mic.lastMs = 0; });
    }
  }
  ivTipHide();
  iv.tab = tab;
  const wrap = document.getElementById('iv-tabs');
  if (wrap) {
    wrap.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
  }
  ivRenderPanel();
}

function ivRenderPanel() {
  const panel = document.getElementById('iv-panel');
  if (!panel) return;
  ivTipHide();
  ivClearRoomTimer();
  if (iv.tab === 'resume') ivRenderResume(panel);
  else if (iv.tab === 'interview') ivRenderInterview(panel);
  else if (iv.tab === 'career') ivRenderCareer(panel);
  else if (iv.tab === 'drill') ivRenderDrill(panel);
  else ivRenderHistory(panel);
  ivRenderHero();
}

/* ============================================================
   一、简历工作台
   ============================================================ */

function ivRenderResume(panel) {
  const r = iv.store.resume;
  panel.innerHTML = ''
    + '<div class="iv-grid">'
    + '  <section class="iv-card iv-editor">'
    + '    <div class="iv-card-head"><span>简历原文</span>'
    + '      <span class="iv-view-switch">'
    + '        <button type="button" class="iv-vs-btn' + (iv.resumeView !== 'preview' ? ' active' : '')
    + '" data-view="edit"' + ivTip('viewEdit') + '>编辑</button>'
    + '        <button type="button" class="iv-vs-btn' + (iv.resumeView === 'preview' ? ' active' : '')
    + '" data-view="preview"' + ivTip('viewPreview') + '>预览</button>'
    + '      </span>'
    + '      <span class="iv-muted" id="iv-char">' + r.text.length + ' 字</span></div>'
    + '    <div class="iv-fields">'
    + '      <label class="iv-field iv-tip-able"' + ivTip('role') + '><span>目标岗位</span>'
    + '        <input id="iv-resume-role" type="text" placeholder="例：前端开发工程师" value="'
    + ivEsc(r.role) + '"></label>'
    + '      <label class="iv-field iv-file-field iv-tip-able"' + ivTip('upload') + '><span>上传简历</span>'
    + '        <input id="iv-resume-file" type="file" accept=".txt,.md,.markdown,.docx,.pdf">'
    + '        <em>支持 txt / md / docx / 文本型 PDF</em></label>'
    + '    </div>'
    + (iv.resumeView === 'preview'
      ? '<div class="iv-resume-preview iv-md">' + (r.text.trim()
        ? ivMd(r.text)
        : '<div class="iv-preview-empty">还没有内容。切回「编辑」粘贴或上传简历。</div>') + '</div>'
      : '<textarea id="iv-resume-text" class="iv-textarea iv-resume-text" spellcheck="false"'
        + ivTip('resume')
        + '      placeholder="把简历粘贴到这里，或上传文件。先写事实，再让 AI 帮你改表达。">'
        + ivEsc(r.text) + '</textarea>')
    + '    <details class="iv-jd iv-tip-able"' + ivTip('jd') + ' ' + (r.jd ? 'open' : '') + '>'
    + '      <summary>岗位 JD（可选，用于匹配度分析与针对性出题）</summary>'
    + '      <textarea id="iv-resume-jd" class="iv-textarea iv-jd-text" spellcheck="false"'
    + '        placeholder="粘贴岗位 JD…">' + ivEsc(r.jd) + '</textarea>'
    + '    </details>'
    + '    <div class="iv-actions iv-wrap">'
    + '      <button type="button" class="iv-btn primary" id="iv-analyze"' + ivTip('analyze') + '>分析简历</button>'
    + '      <button type="button" class="iv-btn" id="iv-build"' + ivTip('build') + '>AI 引导制作</button>'
    + '      <button type="button" class="iv-btn" id="iv-import-profile"' + ivTip('importProfile')
    + '>从原文导入栏目</button>'
    + '      <select id="iv-polish-mode" class="iv-select" title="润色模式"' + ivTip('polishMode') + '>'
    + Object.keys(IV_POLISH_MODES).map((k) => '<option value="' + k + '"'
        + (k === 'impact' ? ' selected' : '') + '>' + IV_POLISH_MODES[k] + '</option>').join('')
    + '      </select>'
    + '      <button type="button" class="iv-btn" id="iv-polish"' + ivTip('polish') + '>润色</button>'
    + '      <button type="button" class="iv-btn" id="iv-jd-match"' + ivTip('jdMatch') + '>JD 匹配</button>'
    + '      <button type="button" class="iv-btn ghost" id="iv-clear"' + ivTip('clear') + '>清空</button>'
    + '    </div>'
    + '    <div class="iv-hintline">简历只保存在本机浏览器；点击 AI 按钮时才会把文本发送给你配置的聊天模型。</div>'
    + '    <div class="iv-hintline warn hidden" id="iv-stale"' + ivTip('stale')
    + '>简历已修改，下面的结果可能过期。'
    + '<button type="button" class="iv-btn small ghost" id="iv-stale-run">重新分析</button></div>'
    + '  </section>'
    + '  <section class="iv-card iv-report" id="iv-report">' + ivReportHtml() + '</section>'
    + '</div>';

  const staleBox = panel.querySelector('#iv-stale');
  const tickStale = () => {
    if (staleBox) staleBox.classList.toggle('hidden', !ivHasStale());
  };
  const staleRun = panel.querySelector('#iv-stale-run');
  if (staleRun) staleRun.onclick = ivAnalyzeResume;
  tickStale();

  const text = panel.querySelector('#iv-resume-text');
  if (text) {
    text.addEventListener('input', () => {
      const c = panel.querySelector('#iv-char');
      if (c) c.textContent = text.value.length + ' 字';
      ivSaveResumeFields(false);
      tickStale();
    });
    ivHotkey(text, () => ivAnalyzeResume());
  }
  panel.querySelectorAll('.iv-vs-btn').forEach((b) => {
    b.onclick = () => {
      if (iv.resumeView === b.dataset.view) return;
      ivSaveResumeFields();
      iv.resumeView = b.dataset.view;
      ivRenderPanel();
    };
  });
  panel.querySelector('#iv-resume-role').addEventListener('input', () => {
    ivSaveResumeFields(false);
    tickStale();
  });
  panel.querySelector('#iv-resume-jd').addEventListener('input', () => {
    ivSaveResumeFields(false);
    tickStale();
  });
  panel.querySelector('#iv-resume-file').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) ivUploadResume(f);
    e.target.value = '';
  });
  panel.querySelector('#iv-analyze').onclick = ivAnalyzeResume;
  panel.querySelector('#iv-build').onclick = () => ivOpenBuilder();
  panel.querySelector('#iv-import-profile').onclick = ivImportProfile;
  panel.querySelector('#iv-polish').onclick = ivPolishResume;
  panel.querySelector('#iv-jd-match').onclick = ivMatchJd;
  panel.querySelector('#iv-clear').onclick = () => {
    if (!confirm('清空当前简历，以及分析、润色和 JD 匹配结果？（本机保存的这份内容会一并删除）')) return;
    iv.store.resume = ivDefaultStore().resume;
    iv.analysis = null;
    iv.polish = null;
    iv.jdMatch = null;
    ivSaveStore();
    ivRenderPanel();
  };
  ivBindReport(panel);
}

function ivBusyButton(btn, busy, text) {
  if (!btn) return;
  if (busy) {
    btn.dataset.old = btn.textContent;
    btn.disabled = true;
    btn.textContent = text || '处理中…';
  } else {
    btn.disabled = false;
    if (btn.dataset.old) {
      btn.textContent = btn.dataset.old;
      btn.dataset.old = '';
    }
  }
}

function ivReadFileB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      const s = String(fr.result || '');
      const i = s.indexOf(',');
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    fr.onerror = () => reject(new Error('读取文件失败'));
    fr.readAsDataURL(file);
  });
}

async function ivUploadResume(file) {
  if (file.size > 8 * 1024 * 1024) { toast('文件超过 8 MB，请压缩后再试', 'error'); return; }
  toast('正在解析 ' + file.name + '…');
  try {
    const b64 = await ivReadFileB64(file);
    const r = await api('/api/hall/interview/resume/parse', { name: file.name, data: b64 });
    if (!r || !r.ok) throw new Error((r && r.error) || '解析失败');
    iv.store.resume.text = r.text || '';
    ivSaveStore();
    ivRenderPanel();
    toast('已解析 ' + (r.chars || 0) + ' 字' + (r.warn ? '（' + r.warn + '）' : ''), 'success');
  } catch (e) {
    toast('解析失败：' + (e.message || e), 'error');
  }
}

/* 把已有简历拆进六个栏目：导入后直接打开「快速填写」让用户检查 */
async function ivImportProfile() {
  const btn = document.getElementById('iv-import-profile');
  if (iv.busy) return;
  ivSaveResumeFields();
  const text = iv.store.resume.text.trim();
  if (text.length < 50) { toast('先粘贴或上传一份简历（至少 50 字）', 'error'); return; }
  if (!confirm('把简历原文按栏目整理出来？会用整理结果填充六个栏目，原文里没有的栏目保持原样。')) return;
  iv.busy = true;
  ivBusyButton(btn, true, '整理中…');
  try {
    const r = await api('/api/hall/interview/resume/import', { text: text });
    const p = (r && r.profile) || {};
    // 以现有栏目为底，只覆盖原文里确实抽到的栏目
    const next = Object.assign({}, ivDefaultStore().profile, iv.store.profile || {});
    Object.keys(next).forEach((k) => {
      const v = String(p[k] || '').trim();
      if (v) next[k] = v;
    });
    iv.store.profile = next;
    ivSaveStore();
    iv.guideFocus = '';
    ivOpenBuilder('form');
    toast(r.degraded ? '已按小标题整理到栏目，请检查一遍' : '已整理到六个栏目，请检查后继续', 'success');
  } catch (e) {
    toast('整理失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
    ivBusyButton(btn, false);
  }
}

async function ivAnalyzeResume() {
  const btn = document.getElementById('iv-analyze');
  if (iv.busy) return;
  ivSaveResumeFields();
  const text = iv.store.resume.text.trim();
  if (text.length < 50) { toast('请先粘贴至少 50 个字符的简历', 'error'); return; }
  iv.busy = true;
  ivBusyButton(btn, true, '分析中…');
  try {
    const r = await api('/api/hall/interview/resume/analyze', {
      text: text, role: iv.store.resume.role, jd: iv.store.resume.jd,
    });
    r.fp = ivFp();
    iv.analysis = r;
    iv.store.resume.analysis = r;
    ivSaveStore();
    ivRenderPanel();
    toast(r.degraded ? '已完成（本地规则版）' : '简历分析完成', 'success');
  } catch (e) {
    toast('分析失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
    ivBusyButton(btn, false);
  }
}

async function ivPolishResume() {
  const btn = document.getElementById('iv-polish');
  if (iv.busy) return;
  ivSaveResumeFields();
  const text = iv.store.resume.text.trim();
  if (text.length < 30) { toast('请先粘贴简历内容', 'error'); return; }
  const mode = (document.getElementById('iv-polish-mode') || {}).value || 'impact';
  iv.busy = true;
  ivBusyButton(btn, true, '润色中…');
  try {
    const r = await api('/api/hall/interview/resume/polish', {
      text: text, role: iv.store.resume.role, mode: mode,
    });
    r.fp = ivFp();
    iv.polish = r;
    iv.store.resume.polish = r;
    ivSaveStore();
    ivRenderPanel();
    toast(r.degraded ? '已生成本地润色建议' : '润色完成', 'success');
  } catch (e) {
    toast('润色失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
    ivBusyButton(btn, false);
  }
}

async function ivMatchJd() {
  const btn = document.getElementById('iv-jd-match');
  if (iv.busy) return;
  ivSaveResumeFields();
  if (iv.store.resume.text.trim().length < 50) { toast('请先粘贴简历', 'error'); return; }
  if (iv.store.resume.jd.trim().length < 20) { toast('请先在「岗位 JD」里粘贴完整 JD', 'error'); return; }
  iv.busy = true;
  ivBusyButton(btn, true, '匹配中…');
  try {
    const r = await api('/api/hall/interview/jd/match', {
      text: iv.store.resume.text, jd: iv.store.resume.jd, role: iv.store.resume.role,
    });
    r.fp = ivFp();
    iv.jdMatch = r;
    iv.store.resume.jdMatch = r;
    ivSaveStore();
    ivRenderPanel();
    toast('JD 匹配完成', 'success');
  } catch (e) {
    toast('匹配失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
    ivBusyButton(btn, false);
  }
}

function ivRing(score, label) {
  const v = Math.max(0, Math.min(100, Number(score) || 0));
  const key = label === '综合' ? 'score'
    : (label === '匹配' ? 'jdMatch' : (label === '本题' ? 'feedback' : 'overallScore'));
  return '<div class="iv-ring iv-tip-able"' + ivTip(key) + ' style="--v:' + v + '"><span>'
    + v + '</span></div>'
    + (label ? '<div class="iv-ring-label">' + ivEsc(label) + '</div>' : '');
}

function ivDimsHtml(dims) {
  return '<div class="iv-dims">' + (dims || []).map((d) => {
    const v = Math.max(0, Math.min(100, Number(d.score) || 0));
    const help = IV_DIM_HELP[d.name];
    return '<div class="iv-dim"><div class="iv-dim-top"><span class="iv-tip-able"'
      + (help ? ' data-iv-tip="' + ivEsc(help) + '"' : '') + '>' + ivEsc(d.name)
      + '</span><b>' + v + '</b></div>'
      + '<div class="iv-bar"><i style="width:' + v + '%"></i></div>'
      + (d.comment ? '<div class="iv-dim-note">' + ivEsc(d.comment) + '</div>' : '')
      + '</div>';
  }).join('') + '</div>';
}

/* 润色后的完整简历：优先用模型给的全文；没给全文就把能对上的改写应用到当前简历，
   这样「复制 / 下载」拿到的是同一份结果，降级时也有内容可导出。 */
function ivPolishedMarkdown() {
  const p = iv.polish || {};
  if (p.resume_markdown) return p.resume_markdown;
  let text = String((iv.store && iv.store.resume && iv.store.resume.text) || '');
  let n = 0;
  (p.bullets || []).forEach((b) => {
    if (b.rewritten && b.original && text.indexOf(b.original) >= 0) {
      text = text.replace(b.original, b.rewritten);
      n += 1;
    }
  });
  return n ? text : '';
}

function ivReportHtml() {
  const a = iv.analysis;
  if (!a && !iv.polish && !iv.jdMatch) {
    return '<div class="iv-empty"><div class="iv-empty-ico">📄</div>'
      + '<div class="iv-empty-title">还没有分析结果</div>'
      + '<div class="iv-empty-sub">粘贴简历后点「分析简历」；也可以先让 AI 引导你把经历整理成事实。</div></div>';
  }
  let h = '<div class="iv-report-scroll">';
  if (a) {
    h += '<div class="iv-score-row">' + ivRing(a.overall, '综合') + '<div>'
      + '<div class="iv-score-title iv-tip-able"' + ivTip('score') + '>简历评分</div>'
      + '<div class="iv-summary">' + ivEsc(a.summary || '') + '</div>'
      + (a.degraded ? '<div class="iv-warnchip">本地规则版：连接聊天模型后可得逐条 AI 建议</div>' : '')
      + '</div></div>';
    h += ivDimsHtml(a.dimensions);
    if (a.highlights && a.highlights.length) {
      h += '<div class="iv-chips">' + a.highlights.map((x) =>
        '<span class="iv-chip">' + ivEsc(x) + '</span>').join('') + '</div>';
    }
    if (a.issues && a.issues.length) {
      h += '<div class="iv-sec-title">问题清单</div>' + a.issues.map((it) => {
        const sev = it.severity === 'high' ? '高' : (it.severity === 'low' ? '低' : '中');
        return '<div class="iv-issue sev-' + ivEsc(it.severity || 'medium') + '">'
          + '<div class="iv-issue-head"><span class="iv-sev iv-tip-able"' + ivTip('severity')
          + '>' + sev + '</span>'
          + '<span class="iv-issue-sec">' + ivEsc(it.section || '整体') + '</span></div>'
          + (it.quote ? '<blockquote>' + ivEsc(it.quote) + '</blockquote>' : '')
          + (it.problem ? '<div class="iv-issue-p">' + ivEsc(it.problem) + '</div>' : '')
          + (it.fix ? '<div class="iv-issue-fix"><b>怎么改</b>' + ivEsc(it.fix) + '</div>' : '')
          + '</div>';
      }).join('');
    }
    if (a.missing && a.missing.length) {
      h += '<div class="iv-sec-title iv-tip-able"' + ivTip('analyzeMissing') + '>还缺的信息'
        + '<button type="button" class="iv-btn small ghost" id="iv-fill-missing">就这些继续访谈</button></div>'
        + '<ul class="iv-list">'
        + a.missing.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>';
    }
  }
  if (iv.polish) {
    h += '<div class="iv-sec-title iv-tip-able"' + ivTip('polish') + '>润色建议'
      + '<button type="button" class="iv-btn small ghost" id="iv-apply-all">全部应用</button></div>';
    h += '<div class="iv-polish-summary">' + ivEsc(iv.polish.summary || '') + '</div>';
    if (iv.polish.warnings && iv.polish.warnings.length) {
      h += '<div class="iv-warnbox">' + iv.polish.warnings.map((x) => ivEsc(x)).join('<br>') + '</div>';
    }
    h += (iv.polish.bullets || []).map((b, i) => '<div class="iv-bullet">'
      + '<div class="iv-bullet-orig">' + ivEsc(b.original) + '</div>'
      + (b.rewritten ? '<div class="iv-bullet-new">' + ivEsc(b.rewritten) + '</div>' : '')
      + (b.reason ? '<div class="iv-bullet-reason">' + ivEsc(b.reason) + '</div>' : '')
      + '<div class="iv-bullet-actions">'
      + (b.rewritten ? '<button type="button" class="iv-btn small primary" data-apply="' + i + '">应用改写</button>' : '')
      + '</div></div>').join('');
    const full = ivPolishedMarkdown();
    if (full) {
      h += '<div class="iv-sec-title">改写后的全文</div>'
        + '<div class="iv-md">' + ivMd(full) + '</div>'
        + '<div class="iv-actions"><button type="button" class="iv-btn" id="iv-use-polished">用全文替换简历</button>'
        + '<button type="button" class="iv-btn ghost" id="iv-copy-polished">复制全文</button>'
        + '<button type="button" class="iv-btn ghost" id="iv-download-polished"'
        + ivTip('download') + '>下载 .md</button></div>';
    }
  }
  if (iv.jdMatch) {
    const j = iv.jdMatch;
    h += '<div class="iv-sec-title iv-tip-able"' + ivTip('jdMatch') + '>JD 匹配度</div>'
      + '<div class="iv-jd-row">' + ivRing(j.match_score, '匹配') + '<div class="iv-summary">'
      + ivEsc(j.summary || '') + '</div></div>';
    if (j.covered && j.covered.length) {
      h += '<div class="iv-jd-sub">已覆盖</div><div class="iv-chips">'
        + j.covered.map((x) => '<span class="iv-chip ok">' + ivEsc(x.keyword) + '</span>').join('')
        + '</div>';
    }
    if (j.missing && j.missing.length) {
      h += '<div class="iv-jd-sub">缺口</div>' + j.missing.map((x) => '<div class="iv-miss">'
        + '<b>' + ivEsc(x.keyword) + '</b><span>' + ivEsc(x.suggestion || '') + '</span></div>').join('');
    }
    if (j.questions && j.questions.length) {
      h += '<div class="iv-jd-sub">可能被问到</div><ul class="iv-list">'
        + j.questions.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>';
    }
  }
  h += '</div>';
  return h;
}

function ivBindReport(panel) {
  const box = panel.querySelector('#iv-report');
  if (!box) return;
  const fillMissing = box.querySelector('#iv-fill-missing');
  if (fillMissing) fillMissing.onclick = ivStartGuideFromMissing;
  box.querySelectorAll('[data-apply]').forEach((btn) => {
    btn.onclick = () => {
      const i = Number(btn.dataset.apply);
      const b = (iv.polish && iv.polish.bullets || [])[i];
      if (!b || !b.rewritten) return;
      const cur = iv.store.resume.text;
      if (cur.indexOf(b.original) < 0) {
        toast('原文已变化，找不到这一条，请重新分析', 'error');
        return;
      }
      iv.store.resume.text = cur.replace(b.original, b.rewritten);
      ivSaveStore();
      ivRenderPanel();
      toast('已应用这条改写', 'success');
    };
  });
  const all = box.querySelector('#iv-apply-all');
  if (all) {
    all.onclick = () => {
      const list = (iv.polish && iv.polish.bullets) || [];
      let text = iv.store.resume.text;
      let n = 0;
      list.forEach((b) => {
        if (b.rewritten && text.indexOf(b.original) >= 0) {
          text = text.replace(b.original, b.rewritten);
          n += 1;
        }
      });
      iv.store.resume.text = text;
      ivSaveStore();
      ivRenderPanel();
      toast(n ? ('已应用 ' + n + ' 条改写') : '没有可应用的条目');
    };
  }
  const useAll = box.querySelector('#iv-use-polished');
  if (useAll) {
    useAll.onclick = () => {
      const md = ivPolishedMarkdown();
      if (!md) return;
      if (!confirm('用改写后的全文替换当前简历？')) return;
      iv.store.resume.text = md;
      ivSaveStore();
      ivRenderPanel();
      toast('已替换简历全文', 'success');
    };
  }
  const copyAll = box.querySelector('#iv-copy-polished');
  if (copyAll) {
    copyAll.onclick = () => {
      const md = ivPolishedMarkdown();
      if (!md) { toast('没有可复制的全文', 'error'); return; }
      ivCopy(md).then(() => toast('已复制', 'success')).catch(() => toast('复制失败', 'error'));
    };
  }
  const downloadAll = box.querySelector('#iv-download-polished');
  if (downloadAll) {
    downloadAll.onclick = () => {
      const md = ivPolishedMarkdown();
      if (!md) { toast('没有可下载的全文', 'error'); return; }
      ivDownload('简历-润色-' + new Date().toISOString().slice(0, 10) + '.md', md);
    };
  }
}

/* 分析出的缺口直接变成下一轮访谈的目标：AI 会优先问这些 */
function ivStartGuideFromMissing() {
  const a = iv.analysis || {};
  const parts = (a.missing || []).slice(0, 6);
  (a.issues || []).filter((x) => x.severity === 'high').forEach((x) => {
    const s = x.fix || x.problem;
    if (s && parts.indexOf(s) < 0) parts.push(s);
  });
  if (!parts.length) { toast('这份简历没有明显缺口，可以直接开始模拟面试', 'success'); return; }
  iv.guideFocus = parts.join('；').slice(0, 800);
  ivOpenBuilder('ai');
}

/* ---------------- AI 引导制作简历 ---------------- */

function ivOpenBuilder(mode) {
  ivSaveResumeFields();
  const ov = ivOpenOverlay('AI 引导制作简历',
    'AI 会一次问一个问题，帮你想起来可以写什么；也可以切换到「快速填写」。');
  const g = iv.store.resumeGuide || {};
  const formStep = Math.max(0, Math.min(IV_BUILDER_STEPS.length - 1,
    Number(g.formStep) || 0));
  const openMode = mode === 'form' || mode === 'ai'
    ? mode : (g.mode === 'form' ? 'form' : 'ai');
  iv.builder = {
    mode: openMode,
    step: formStep,
    data: Object.assign({}, iv.store.profile),
    result: null,
    qa: ivNormalizeQa(g.qa),
    guideStep: Number(g.step) || 0,
    input: String(g.draft || ''),
    focus: iv.guideFocus || '',
    facts: ivNormalizeFacts(g.facts),
    coverage: ivNormalizeCoverage(g.coverage),
    guide: {
      question: String(g.current || ''),
      why: '', examples: [], hint: '', done: !!g.done,
      summary: '', loading: false, field: String(g.field || ''),
    },
  };
  ivRenderBuilder(ov.body, ov.foot);
  if (iv.builder.mode === 'ai' && !iv.builder.guide.question && !iv.builder.guide.done) {
    ivAiGuideNext(false);
  }
}

function ivBuilderModeSwitch(mode) {
  return '<div class="iv-build-modes">'
    + '<button type="button" class="iv-mode-btn' + (mode === 'ai' ? ' active' : '')
    + '" data-mode="ai"' + ivTip('builderAi') + '>AI 访谈</button>'
    + '<button type="button" class="iv-mode-btn' + (mode === 'form' ? ' active' : '')
    + '" data-mode="form"' + ivTip('builderForm') + '>快速填写</button>'
    + '</div>';
}

function ivRenderBuilder(body, foot) {
  if (!iv.builder) return;
  if (iv.builder.result) {
    ivRenderBuildResult(body, foot);
    return;
  }
  // 只让对话区滚动，输入框和操作按钮固定在可视区内（否则小窗口下按钮会被挡在折叠线以下）
  body.classList.add('iv-build-flex');
  body.innerHTML = ivBuilderModeSwitch(iv.builder.mode);
  const inner = document.createElement('div');
  inner.className = 'iv-build-inner';
  body.appendChild(inner);
  body.querySelectorAll('.iv-mode-btn').forEach((b) => {
    b.onclick = () => {
      if (iv.builder.mode === b.dataset.mode) return;
      ivSaveBuilderState();
      // 离开 AI 访谈时先把挂在它输入框上的录音收掉
      if (iv.builder.mode === 'ai' && typeof micStopIf === 'function') micStopIf('iv-ai-answer');
      iv.builder.mode = b.dataset.mode;
      if (b.dataset.mode === 'form') {
        iv.builder.step = Math.max(0, Math.min(IV_BUILDER_STEPS.length - 1,
          Number((iv.store.resumeGuide || {}).formStep) || 0));
      }
      ivRenderBuilder(body, foot);
      if (iv.builder.mode === 'ai'
        && !iv.builder.guide.question && !iv.builder.guide.done) {
        ivAiGuideNext(false);
      }
    };
  });
  if (iv.builder.mode === 'ai') ivRenderAiBuilder(inner, body, foot);
  else ivRenderFormBuilder(inner, body, foot);
}

function ivRenderFormBuilder(inner, body, foot) {
  const step = IV_BUILDER_STEPS[iv.builder.step];
  const total = IV_BUILDER_STEPS.length;
  inner.innerHTML = '<div class="iv-build-progress">'
    + IV_BUILDER_STEPS.map((s, i) => '<span class="' + (i === iv.builder.step ? 'on' : '')
      + (i < iv.builder.step ? ' done' : '') + '">' + (i + 1) + '</span>').join('')
    + '</div>'
    + '<div class="iv-build-title">' + (iv.builder.step + 1) + '/' + total + ' · ' + ivEsc(step.title) + '</div>'
    + '<div class="iv-build-hint">' + ivEsc(step.hint) + '</div>'
    + '<textarea id="iv-build-input" class="iv-textarea iv-build-input" spellcheck="false"'
    + ' placeholder="' + ivEsc(step.ph) + '">'
    + ivEsc(iv.builder.data[step.key] || '') + '</textarea>'
    + '<div class="iv-build-note iv-tip-able"' + ivTip('builderSave')
    + '>没有的信息可以留空或写「暂无」；填写内容会自动保存在本机，关闭后再打开仍在。</div>';
  foot.innerHTML = '<button type="button" class="iv-btn ghost" id="iv-build-reset"' + ivTip('builderReset')
    + '>重新开始</button>'
    + '<span class="iv-bar-spacer"></span>'
    + '<button type="button" class="iv-btn ghost" id="iv-build-prev"'
    + (iv.builder.step === 0 ? ' disabled' : '') + '>上一步</button>'
    + '<button type="button" class="iv-btn" id="iv-build-skip">跳过</button>'
    + '<button type="button" class="iv-btn primary" id="iv-build-next">'
    + (iv.builder.step === total - 1 ? '生成简历' : '下一步') + '</button>';
  foot.querySelector('#iv-build-reset').onclick = ivBuilderReset;
  const go = (n) => {
    ivStoreBuilderInput();
    iv.builder.step = n;
    ivSaveBuilderState();
    ivRenderBuilder(body, foot);
  };
  foot.querySelector('#iv-build-prev').onclick = () => go(Math.max(0, iv.builder.step - 1));
  foot.querySelector('#iv-build-skip').onclick = () => {
    if (iv.builder.step >= IV_BUILDER_STEPS.length - 1) {
      ivStoreBuilderInput();
      ivSubmitBuilder(body, foot, false);
    } else {
      go(iv.builder.step + 1);
    }
  };
  foot.querySelector('#iv-build-next').onclick = () => {
    if (iv.builder.step >= IV_BUILDER_STEPS.length - 1) {
      ivStoreBuilderInput();
      ivSubmitBuilder(body, foot, false);
    } else {
      go(iv.builder.step + 1);
    }
  };
  const input = inner.querySelector('#iv-build-input');
  if (input) {
    input.addEventListener('input', () => {
      if (!iv.builder) return;
      iv.builder.data[step.key] = input.value;
      iv.store.profile = Object.assign({}, iv.store.profile, iv.builder.data);
      ivQueueSave();
    });
    // 快速填写也能 Ctrl+Enter 走到下一步 / 生成
    ivHotkey(input, () => foot.querySelector('#iv-build-next').click());
    input.focus();
  }
}

function ivStoreBuilderInput() {
  const b = iv.builder;
  if (!b) return;
  const input = document.getElementById('iv-build-input');
  if (input) {
    const step = IV_BUILDER_STEPS[b.step];
    if (step) b.data[step.key] = input.value;
    iv.store.profile = Object.assign({}, iv.store.profile, b.data);
  }
  ivSaveBuilderState();
}

/* 引导制作的进度都存这里：问答、当前题、快速填写停在第几栏、AI 输入框草稿 */
function ivSaveBuilderState() {
  const b = iv.builder;
  if (!b || !iv.store) return;
  const prev = iv.store.resumeGuide || {};
  iv.store.resumeGuide = {
    qa: (b.qa || []).slice(-30),
    step: Number(b.guideStep) || 0,
    current: b.guide.question || '',
    done: !!b.guide.done,
    formStep: Number(b.step) || 0,
    draft: b.mode === 'ai' ? String(b.input || '') : String(prev.draft || ''),
    mode: b.mode,
    facts: b.facts || {},
    coverage: b.coverage || {},
    field: b.guide.field || '',
    updatedAt: Date.now(),
  };
  ivSaveStore();
}

/* 重新开始：清掉访谈问答与已填栏目，回到第一个问题。
   只动引导制作自己的数据，简历正文 / 分析结果 / 面试记录都不受影响。 */
async function ivBuilderReset() {
  const b = iv.builder;
  if (!b || !iv.overlay || iv.busy || b.guide.loading) return;
  if (!confirm('清空本次访谈的问答和已填栏目，重新开始？'
    + '（不影响简历正文、分析结果和面试记录）')) return;
  // 先收麦，等转写落进输入框再清，避免转写结果在重置后才写回来
  if (typeof micStopIf === 'function') {
    const stopping = micStopIf('iv-ai-answer');
    if (stopping) await stopping;
  }
  // 收麦期间弹窗可能被关掉 / 换了状态
  if (iv.builder !== b || !iv.overlay) return;
  b.step = 0;
  b.data = ivDefaultStore().profile;
  b.result = null;
  b.qa = [];
  b.guideStep = 0;
  b.input = '';
  b.focus = '';
  b.facts = {};
  b.coverage = {};
  iv.guideFocus = '';
  b.guide = { question: '', why: '', examples: [], hint: '', done: false,
    summary: '', loading: false, field: '' };
  iv.store.profile = Object.assign({}, b.data);
  iv.store.resumeGuide = { qa: [], step: 0, current: '', done: false,
    formStep: 0, draft: '', mode: 'ai', facts: {}, coverage: {}, field: '',
    updatedAt: Date.now() };
  ivSaveStore();
  ivRenderBuilder(iv.overlay.body, iv.overlay.foot);
  if (b.mode === 'ai') ivAiGuideNext(false);
}

/* 撤回某条回答：和 Solo 的消息撤回一致——连同它后面的问答一起撤掉，回到这一题重答 */
function ivWithdrawAnswer(idx) {
  const b = iv.builder;
  if (!b || !iv.overlay || b.busy || b.guide.loading) return;
  const item = b.qa[idx];
  if (!item) return;
  if (!confirm('撤回这条回答，以及它后面的问答？\n撤回后回到这一题重新回答。')) return;
  if (typeof micStopIf === 'function') micStopIf('iv-ai-answer');
  const q = item.q || '';
  b.qa = b.qa.slice(0, idx);                       // 这条及之后都撤掉，跟 Solo 撤回一致
  b.guideStep = Math.min(Number(b.guideStep) || 0, b.qa.length);
  b.input = '';
  b.guide = { question: q, why: '已撤回这条回答，重新回答一次。', examples: [],
    hint: '', done: false, summary: '', loading: false };
  ivAiGuideSave();
  ivRenderBuilder(iv.overlay.body, iv.overlay.foot);
  if (!q) ivAiGuideNext(false);
}

/* 语音输入按钮状态：由主界面那套麦克风回调驱动（是否录音 / 秒数 / 是否在转写） */
function ivBuilderMicUI(on, secs, busy) {
  const btn = document.getElementById('iv-ai-mic');
  if (!btn) return;
  btn.classList.toggle('recording', !!on);
  btn.textContent = on ? ('停止并转写 ' + (secs || 0) + 's') : (busy ? '转写中…' : '语音输入');
  const sub = document.getElementById('iv-ai-submit');
  if (sub) sub.disabled = !!on || !!busy || !!(iv.builder && iv.builder.guide.loading);
}

function ivAiGuideSave() {
  ivSaveBuilderState();
}

async function ivAiGuideNext(hint, skip) {
  const b = iv.builder;
  if (!b || b.mode !== 'ai' || b.guide.loading) return;
  if (!iv.overlay) return;
  const body = iv.overlay.body;
  const foot = iv.overlay.foot;
  b.guide.loading = true;
  b.guide.hint = '';
  ivRenderBuilder(body, foot);
  try {
    const r = await api('/api/hall/interview/resume/guide', {
      qa: b.qa, profile: b.data, role: iv.store.resume.role,
      jd: iv.store.resume.jd, step: b.guideStep || 0,
      current_question: b.guide.question || '', current_field: b.guide.field || '',
      hint: !!hint, skip: !!skip, facts: b.facts || {},
      focus: b.focus || '',
    });
    if (!iv.builder || iv.builder !== b) return;
    b.guide.loading = false;
    b.guide.question = (r && r.question) || (hint ? b.guide.question : '');
    b.guide.why = (r && r.why) || '';
    b.guide.examples = (r && r.examples) || [];
    b.guide.hint = (r && r.hint) || '';
    b.guide.field = (r && r.field) || (hint ? b.guide.field : '');
    if (r && r.facts) b.facts = ivMergeFacts(b.facts, r.facts);
    if (r && r.coverage) b.coverage = ivNormalizeCoverage(r.coverage);
    if (hint && !b.guide.hint && b.guide.examples.length) {
      b.guide.hint = '可以从这些里挑一个，或者只说你记得的部分：'
        + b.guide.examples.join('；');
    }
    b.guide.done = !!(r && r.done) && !hint;
    b.guide.summary = (r && r.summary) || '';
    if (r && typeof r.step === 'number') b.guideStep = r.step;
    if (b.guide.done) b.guide.question = '';
    // 访谈结束会收掉输入框，先把录音停掉，避免声音继续录却写不进去
    if (b.guide.done && typeof micStopIf === 'function') micStopIf('iv-ai-answer');
    ivAiGuideSave();
    ivRenderBuilder(body, foot);
  } catch (e) {
    if (!iv.builder || iv.builder !== b) return;
    b.guide.loading = false;
    if (!b.guide.question) {
      b.guide.done = true;
      b.guide.summary = '访谈暂时不可用，可以先切换到「快速填写」或直接生成。';
    }
    toast('访谈失败：' + (e.message || e), 'error');
    ivRenderBuilder(body, foot);
  }
}

function ivAiGuideInput() {
  const ta = document.getElementById('iv-ai-answer');
  return ta ? ta : null;
}

async function ivAiGuideSubmit(skip) {
  const b = iv.builder;
  if (!b || b.mode !== 'ai' || b.guide.loading) return;
  // 录音中直接提交：先收麦，等转写写回输入框再读内容
  if (typeof micStopIf === 'function') {
    const stopping = micStopIf('iv-ai-answer');
    if (stopping) await stopping;
  }
  const ta = ivAiGuideInput();
  const ans = skip ? IV_SKIP_ANSWER : (ta ? ta.value.trim() : '');
  if (!ans) { toast('先写下回答，或点「跳过这题」', 'error'); return; }
  const field = b.guide.field || '';
  // 第一题问的是岗位：答完直接写回简历抬头，后面所有问题都会围着它问
  if (!skip && field === 'role') {
    const role = ans.slice(0, 60);
    iv.store.resume.role = role;
    const roleInput = document.getElementById('iv-resume-role');
    if (roleInput) roleInput.value = role;
    ivSaveStore();
    b.guide.field = '';
  }
  if (b.guide.question) b.qa.push({ q: b.guide.question, a: ans, f: field, at: Date.now() });
  b.input = '';
  b.guide.hint = '';
  ivAiGuideSave();
  ivAiGuideNext(false, skip);
}

function ivAiGuideHint() {
  const b = iv.builder;
  if (!b || b.guide.loading) return;
  const ta = ivAiGuideInput();
  if (ta) b.input = ta.value;
  ivAiGuideNext(true);
}

function ivAiGenerate() {
  const b = iv.builder;
  if (!b || !iv.overlay) return;
  if (b.guide.loading) { toast('AI 正在出下一题，稍等一下', 'error'); return; }
  // 当前输入框里还没提交的内容由 ivSubmitBuilder 统一收进 qa，避免重复记录
  ivSubmitBuilder(iv.overlay.body, iv.overlay.foot, true);
}

/* ai=true 是 AI 提问；带 idx 的是用户回答，悬停时可撤回（同 Solo 的消息撤回）。
   每个气泡都带「复制」，原样复制纯文本，和 Solo 消息的复制一致。 */
function ivAiBubble(item, ai, idx) {
  const text = String(item == null ? '' : item);
  let acts = '<button type="button" class="iv-ai-copy" data-copy="' + ivEsc(text)
    + '" title="复制这段文字">复制</button>';
  if (typeof idx === 'number') {
    acts += '<button type="button" class="iv-ai-withdraw" data-qa="' + idx
      + '" title="撤回这条回答，回到这一题重新回答">撤回</button>';
  }
  return '<div class="iv-ai-msg ' + (ai ? 'ai' : 'user') + '">'
    + '<div class="iv-ai-label">' + (ai ? 'AI 访谈' : '我') + acts + '</div>'
    + '<div class="iv-ai-text">' + ivEsc(text) + '</div></div>';
}

/* 气泡上的「复制」按钮：文案是渲染时写进 data-copy 的纯文本 */
function ivBindBubbleCopy(root) {
  root.querySelectorAll('.iv-ai-copy').forEach((btn) => {
    btn.onclick = () => {
      const text = btn.dataset.copy || '';
      if (!text) return;
      ivCopy(text).then(() => toast('已复制', 'success')).catch(() => toast('复制失败', 'error'));
    };
  });
}

function ivFactCount(facts) {
  const f = ivNormalizeFacts(facts);
  return Object.keys(f).reduce((n, k) => n + f[k].length, 0);
}

/* 访谈进度：五个板块的覆盖度，替代「不知道还要问多少题」 */
function ivGuideMeterHtml(b) {
  const total = 10;
  const done = b.qa.length;
  const step = b.guide.done ? total : Math.min(done + 1, total);
  const bars = IV_GUIDE_SECTIONS.map((k) => {
    const v = Number((b.coverage || {})[k]) || 0;
    return '<div class="iv-guide-bar" title="' + IV_GUIDE_LABELS[k] + ' ' + v + '%">'
      + '<span class="iv-guide-name">' + IV_GUIDE_LABELS[k] + '</span>'
      + '<div class="iv-guide-track"><i style="width:' + v + '%"></i></div></div>';
  }).join('');
  return '<div class="iv-guide-meter">'
    + '<div class="iv-guide-step">第 ' + step + ' / ' + total + ' 题'
    + '<span class="iv-muted">已记录 ' + ivFactCount(b.facts) + ' 条事实</span></div>'
    + '<div class="iv-guide-bars">' + bars + '</div></div>';
}

/* 每题抽出的结构化事实：生成简历时优先用这些，而不是大段口语原文 */
function ivFactsHtml(facts) {
  const f = ivNormalizeFacts(facts);
  const keys = Object.keys(f);
  if (!keys.length) return '';
  return '<details class="iv-facts" open><summary>已记录的事实（'
    + ivFactCount(f) + ' 条，生成时优先使用）</summary>'
    + keys.map((k) => '<div class="iv-fact-group"><b>' + (IV_GUIDE_LABELS[k] || k) + '</b>'
      + f[k].map((x, i) => '<div class="iv-fact-item"><span>' + ivEsc(x) + '</span>'
        + '<button type="button" class="iv-fact-del" data-sec="' + ivEsc(k)
        + '" data-idx="' + i + '">删</button></div>').join('')
      + '</div>').join('')
    + '</details>';
}

function ivRenderAiBuilder(inner, body, foot) {
  const b = iv.builder;
  const g = b.guide;
  let chat = '';
  b.qa.forEach((it, i) => {
    if (it.q) chat += ivAiBubble(it.q, true);
    chat += ivAiBubble(it.a || '（未回答）', false, i);
  });
  if (g.loading) {
    chat += '<div class="iv-ai-thinking"><span class="iv-spinner"></span>AI 正在想下一个问题…</div>';
  } else if (g.done) {
    chat += ivAiBubble(g.summary || '信息已经比较完整，可以生成简历了。', true);
  } else if (g.question) {
    chat += ivAiBubble(g.question, true);
    if (g.why) chat += '<div class="iv-ai-why">为什么问：' + ivEsc(g.why) + '</div>';
    if (g.examples && g.examples.length) {
      chat += '<div class="iv-ai-examples">'
        + g.examples.map((x) => '<button type="button" class="iv-ai-example">'
          + ivEsc(x) + '</button>').join('') + '</div>';
    }
  } else {
    chat += '<div class="iv-ai-thinking">还没有开始访谈。</div>';
  }
  if (g.hint) chat += '<div class="iv-ai-hintbox">' + ivEsc(g.hint) + '</div>';

  inner.innerHTML = ivGuideMeterHtml(b)
    + '<div class="iv-ai-chat" id="iv-ai-chat">' + chat + '</div>'
    + ivFactsHtml(b.facts)
    + (g.done ? '' : '<textarea id="iv-ai-answer" class="iv-textarea iv-ai-input"'
      + ' spellcheck="false" placeholder="用你自己的话回答，记得多少写多少；想不起来就点下面的提示。">'
      + ivEsc(b.input || '') + '</textarea>'
      + '<div class="iv-actions iv-wrap">'
      + '<button type="button" class="iv-btn primary" id="iv-ai-submit"'
      + (g.loading ? ' disabled' : '') + '>提交回答</button>'
      + '<button type="button" class="iv-btn" id="iv-ai-hint"' + ivTip('aiHint')
      + (g.loading ? ' disabled' : '') + '>不知道，给我点提示</button>'
      + '<button type="button" class="iv-btn ghost" id="iv-ai-skip"' + ivTip('aiSkip')
      + (g.loading ? ' disabled' : '') + '>跳过这题</button>'
      + '<button type="button" class="iv-btn ghost" id="iv-ai-mic"' + ivTip('builderMic')
      + (g.loading ? ' disabled' : '') + '>语音输入</button></div>');

  foot.innerHTML = '<span class="iv-muted">已答 ' + b.qa.length + ' 题</span>'
    + '<span class="iv-bar-spacer"></span>'
    + '<button type="button" class="iv-btn ghost" id="iv-ai-reset"' + ivTip('builderReset')
    + '>重新开始</button>'
    + '<button type="button" class="iv-btn primary" id="iv-ai-generate"'
    + (g.loading ? ' disabled' : '') + '>'
    + (b.qa.length >= 3 ? '信息够了，生成简历' : '先生成一版') + '</button>';

  const ta = inner.querySelector('#iv-ai-answer');
  if (ta) {
    ta.addEventListener('input', () => {
      if (!iv.builder) return;
      iv.builder.input = ta.value;
      // 草稿只放内存，落盘走防抖：关掉弹窗再打开还能接着写
      if (iv.store.resumeGuide) iv.store.resumeGuide.draft = ta.value;
      ivQueueSave();
    });
    ivHotkey(ta, () => ivAiGuideSubmit(false));
  }
  inner.querySelectorAll('.iv-ai-example').forEach((btn) => {
    btn.onclick = () => {
      if (!ta) return;
      ta.value = btn.textContent;
      if (iv.builder) {
        iv.builder.input = ta.value;
        if (iv.store.resumeGuide) iv.store.resumeGuide.draft = ta.value;
        ivQueueSave();
      }
      ta.focus();
    };
  });
  inner.querySelectorAll('.iv-ai-withdraw').forEach((btn) => {
    btn.onclick = () => ivWithdrawAnswer(Number(btn.dataset.qa));
  });
  // 事实卡可以逐条删除：AI 抽错了不能硬塞进简历
  inner.querySelectorAll('.iv-fact-del').forEach((btn) => {
    btn.onclick = () => {
      const b2 = iv.builder;
      if (!b2) return;
      const sec = btn.dataset.sec;
      const idx = Number(btn.dataset.idx);
      const list = (b2.facts || {})[sec] || [];
      list.splice(idx, 1);
      if (!list.length) delete b2.facts[sec];
      ivSaveBuilderState();
      ivRenderBuilder(body, foot);
    };
  });
  ivBindBubbleCopy(inner);
  const s = inner.querySelector('#iv-ai-submit');
  if (s) s.onclick = () => ivAiGuideSubmit(false);
  const h = inner.querySelector('#iv-ai-hint');
  if (h) h.onclick = ivAiGuideHint;
  const sk = inner.querySelector('#iv-ai-skip');
  if (sk) sk.onclick = () => ivAiGuideSubmit(true);
  const m = inner.querySelector('#iv-ai-mic');
  if (m) m.onclick = () => toggleMic('iv-ai-answer', 'iv-ai-mic', ivBuilderMicUI);
  const gen = foot.querySelector('#iv-ai-generate');
  if (gen) gen.onclick = ivAiGenerate;
  const rs = foot.querySelector('#iv-ai-reset');
  if (rs) rs.onclick = ivBuilderReset;
  const chatBox = inner.querySelector('#iv-ai-chat');
  if (chatBox) chatBox.scrollTop = chatBox.scrollHeight;
  if (ta) ta.focus();
}

async function ivSubmitBuilder(body, foot, fromAI) {
  const b = iv.builder;
  if (!b || iv.busy) return;
  // 先占住 busy，避免收麦 / 请求期间被重复点击
  iv.busy = true;
  try {
    if (fromAI && typeof micStopIf === 'function') {
      // 还在录音就先收麦，把最后一段转写并进输入框
      const stopping = micStopIf('iv-ai-answer');
      if (stopping) await stopping;
      if (iv.builder !== b) return;        // 收麦期间弹窗被关掉了
    }
    if (fromAI) {
      const ta = ivAiGuideInput();
      if (ta && ta.value.trim() && b.guide.question) {
        b.qa.push({ q: b.guide.question, a: ta.value.trim(),
          f: b.guide.field || '', at: Date.now() });
        b.guide.field = '';
        // 生成之后这一题就算答过了：清掉当前题，重开弹窗不能再问一遍
        b.guide.question = '';
        b.guide.why = '';
        b.guide.examples = [];
        b.guide.hint = '';
        b.guide.done = false;
        b.input = '';
        ivAiGuideSave();
      }
    } else {
      ivStoreBuilderInput();
    }
    const facts = b.data;
    const joined = (Object.keys(facts).map((k) => String(facts[k] || '')).join('')
      + b.qa.map((it) => it.a || '').join('')).replace(/（跳过这题）/g, '').trim();
    if (joined.length < 20) {
      toast('至少填写或聊出一些真实信息，再生成简历', 'error');
      return;
    }
    body.innerHTML = '<div class="iv-loading"><span class="iv-spinner"></span>正在根据你提供的事实撰写简历…</div>';
    foot.innerHTML = '';
    const payload = Object.assign({}, facts);
    const r = await api('/api/hall/interview/resume/build', {
      profile: payload, role: iv.store.resume.role, jd: iv.store.resume.jd, qa: b.qa,
      facts: b.facts || {},
    });
    if (iv.builder !== b) return;
    if (!r || !String(r.resume_markdown || '').trim()) {
      toast('这一版没有生成正文，补充一点信息再试', 'error');
      ivRenderBuilder(body, foot);
      return;
    }
    b.result = r;
    ivRenderBuildResult(body, foot);
    // 有 JD 时顺手跑一次匹配，结果页直接显示覆盖与缺口
    ivAutoJdMatch(b, r);
  } catch (e) {
    if (iv.builder === b && iv.overlay) {
      toast('生成失败：' + (e.message || e), 'error');
      ivRenderBuilder(body, foot);
    }
  } finally {
    iv.busy = false;
  }
}

async function ivAutoJdMatch(b, r) {
  const jd = String(iv.store.resume.jd || '').trim();
  if (!jd || jd.length < 20) return;
  try {
    const m = await api('/api/hall/interview/jd/match', {
      text: r.resume_markdown, jd: jd, role: iv.store.resume.role,
    });
    if (iv.builder !== b || !b.result || !iv.overlay) return;
    b.jdMatch = m;
    ivRenderBuildResult(iv.overlay.body, iv.overlay.foot);
  } catch (e) { /* 匹配失败不影响已经生成的简历 */ }
}

function ivRenderBuildResult(body, foot) {
  const b = iv.builder;
  const r = b && b.result;
  if (!r) return;
  // 结果页整页滚动，不用「对话区固定高度」那套布局
  body.classList.remove('iv-build-flex');
  let html = '';
  if (r.warnings && r.warnings.length) {
    html += '<div class="iv-warnbox iv-tip-able"' + ivTip('warning') + '>'
      + r.warnings.map((x) => ivEsc(x)).join('<br>') + '</div>';
  }
  html += '<div class="iv-sec-title">简历预览</div>'
    + '<div class="iv-md iv-build-md">' + ivMd(r.resume_markdown || '') + '</div>';

  // JD 匹配：生成后自动跑一次，覆盖 / 缺口直接在这里看
  const jm = b.jdMatch;
  if (jm) {
    html += '<div class="iv-sec-title iv-tip-able"' + ivTip('jdMatch') + '>JD 匹配'
      + '<span class="iv-muted">' + (jm.match_score || 0) + ' 分</span></div>'
      + (jm.summary ? '<div class="iv-summary">' + ivEsc(jm.summary) + '</div>' : '');
    if (jm.covered && jm.covered.length) {
      html += '<div class="iv-jd-sub">已覆盖</div><div class="iv-chips">'
        + jm.covered.map((x) => '<span class="iv-chip ok">' + ivEsc(x.keyword) + '</span>').join('')
        + '</div>';
    }
    if (jm.missing && jm.missing.length) {
      html += '<div class="iv-jd-sub">缺口</div>'
        + jm.missing.map((x) => '<div class="iv-miss"><b>' + ivEsc(x.keyword) + '</b><span>'
          + ivEsc(x.suggestion || '') + '</span></div>').join('');
    }
    if (jm.degraded) html += '<div class="iv-hintline">JD 匹配是本地关键词比对版。</div>';
  }

  const gaps = Array.isArray(r.gaps) ? r.gaps : [];
  if (gaps.length) {
    html += '<div class="iv-sec-title iv-tip-able"' + ivTip('followup') + '>还需要你补充</div>'
      + '<ul class="iv-list iv-followups">'
      + gaps.map((x, i) => '<li><span>' + ivEsc((IV_GUIDE_LABELS[x.section] || '其他') + '：'
        + (x.need || '')) + '</span>'
        + '<button type="button" class="iv-btn small ghost" data-gap="' + i + '"'
        + ivTip('gapResume') + '>回去补这一块</button></li>').join('')
      + '</ul>';
  } else if (r.followups && r.followups.length) {
    html += '<div class="iv-sec-title iv-tip-able"' + ivTip('followup') + '>还需要你补充</div>'
      + '<ul class="iv-list iv-followups">'
      + r.followups.map((x, i) => '<li><span>' + ivEsc(x) + '</span>'
        + '<button type="button" class="iv-btn small ghost" data-fill="' + i + '"'
        + ivTip('followupFill') + '>补进补充信息</button></li>').join('')
      + '</ul>';
  }
  if (r.used_facts && r.used_facts.length) {
    html += '<details class="iv-used-facts"><summary>这一版用到了你提供的这些信息</summary>'
      + '<ul class="iv-list">'
      + r.used_facts.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul></details>';
  }
  html += '<div class="iv-sec-title iv-tip-able"' + ivTip('extraInfo') + '>补充信息（可选）</div>'
    + '<textarea id="iv-build-extra" class="iv-textarea iv-build-extra" spellcheck="false"'
    + ' placeholder="回答上面的追问，或补充任何事实，然后重新生成。">'
    + ivEsc(b.data.extra || '') + '</textarea>';
  body.innerHTML = html;

  foot.innerHTML = '<button type="button" class="iv-btn ghost" id="iv-build-back"'
    + ivTip('back') + '>返回修改</button>'
    + '<button type="button" class="iv-btn" id="iv-build-regen"' + ivTip('buildRegen')
    + '>带着补充重新生成</button>'
    + '<button type="button" class="iv-btn" id="iv-build-copy"' + ivTip('copy') + '>复制</button>'
    + '<button type="button" class="iv-btn" id="iv-build-download"' + ivTip('download') + '>下载 .md</button>'
    + '<button type="button" class="iv-btn" id="iv-build-use-analyze"' + ivTip('buildUseAnalyze')
    + '>使用并分析</button>'
    + '<button type="button" class="iv-btn primary" id="iv-build-use"' + ivTip('buildUse')
    + '>使用这份简历</button>';

  const back = () => {
    if (!iv.builder) return;
    iv.builder.result = null;
    iv.builder.jdMatch = null;
    iv.builder.step = IV_BUILDER_STEPS.length - 1;
    ivRenderBuilder(body, foot);
    // AI 模式下刚才那题已经算答过：回到对话时接着问下一题，不能重复上一题
    const bb = iv.builder;
    if (bb && bb.mode === 'ai' && !bb.guide.question && !bb.guide.done && !bb.guide.loading) {
      ivAiGuideNext(false);
    }
  };
  foot.querySelector('#iv-build-back').onclick = back;
  foot.querySelector('#iv-build-regen').onclick = () => {
    const ex = document.getElementById('iv-build-extra');
    if (ex && iv.builder) {
      iv.builder.data.extra = ex.value;
      iv.store.profile = Object.assign({}, iv.store.profile, iv.builder.data);
      iv.builder.jdMatch = null;
      ivSaveStore();
    }
    ivSubmitBuilder(body, foot);
  };
  foot.querySelector('#iv-build-copy').onclick = () => {
    ivCopy(r.resume_markdown || '').then(() => toast('已复制', 'success'))
      .catch(() => toast('复制失败', 'error'));
  };
  foot.querySelector('#iv-build-download').onclick = () => {
    ivDownload('简历-' + new Date().toISOString().slice(0, 10) + '.md', r.resume_markdown || '');
  };
  foot.querySelector('#iv-build-use').onclick = () => {
    iv.store.resume.text = r.resume_markdown || '';
    ivSaveStore();
    ivCloseOverlay();
    ivRenderPanel();
    toast('已放入简历工作台', 'success');
  };
  foot.querySelector('#iv-build-use-analyze').onclick = () => {
    iv.store.resume.text = r.resume_markdown || '';
    ivSaveStore();
    ivCloseOverlay();
    ivRenderPanel();
    toast('已放入简历工作台，正在按新内容分析', 'success');
    ivAnalyzeResume();
  };

  // 追问逐条补进「补充信息」，用户只要在后面写答案
  body.querySelectorAll('[data-fill]').forEach((btn) => {
    btn.onclick = () => {
      const text = (r.followups || [])[Number(btn.dataset.fill)];
      const ex = body.querySelector('#iv-build-extra');
      if (!text || !ex || !iv.builder) return;
      const head = ex.value.replace(/\s*$/, '');
      ex.value = (head ? head + '\n' : '') + '- ' + text + '：';
      ex.focus();
      try { ex.setSelectionRange(ex.value.length, ex.value.length); } catch (e) { /* 忽略 */ }
      iv.builder.data.extra = ex.value;
      iv.store.profile = Object.assign({}, iv.store.profile, iv.builder.data);
      ivQueueSave();
    };
  });

  // 缺口直接回到访谈继续问这一块，比让用户自己想补充什么更省事
  body.querySelectorAll('[data-gap]').forEach((btn) => {
    btn.onclick = () => {
      const g2 = gaps[Number(btn.dataset.gap)];
      const b2 = iv.builder;
      if (!g2 || !b2) return;
      b2.result = null;
      b2.jdMatch = null;
      b2.focus = ((IV_GUIDE_LABELS[g2.section] || '其他') + '：'
        + (g2.need || '')).slice(0, 800);
      b2.guide = { question: '', why: '', examples: [], hint: '', done: false,
        summary: '', loading: false, field: '' };
      b2.mode = 'ai';
      ivRenderBuilder(body, foot);
      ivAiGuideNext(false);
    };
  });

  // 补充信息边写边存：关闭弹窗、切去别的页签都不会丢
  const extraBox = body.querySelector('#iv-build-extra');
  if (extraBox) {
    extraBox.addEventListener('input', () => {
      if (!iv.builder) return;
      iv.builder.data.extra = extraBox.value;
      iv.store.profile = Object.assign({}, iv.store.profile, iv.builder.data);
      ivQueueSave();
    });
  }
}

/* ============================================================
   二、模拟面试
   ============================================================ */

function ivRenderInterview(panel) {
  ivClearRoomTimer();
  if (iv.finishing) {
    panel.innerHTML = '<div class="iv-card iv-loading-card"><span class="iv-spinner"></span>'
      + '<div>正在整理这场面试，生成复盘报告…</div></div>';
    return;
  }
  if (iv.reportView) { ivRenderReportView(panel); return; }
  if (iv.session) { ivRenderRoom(panel); return; }
  ivRenderInterviewConfig(panel);
}

function ivRenderInterviewConfig(panel) {
  const s = iv.store.settings;
  const role = iv.store.resume.role || '';
  panel.innerHTML = '<div class="iv-grid iv-interview-grid">'
    + '<section class="iv-card">'
    + '  <div class="iv-card-head"><span>面试设置</span><span class="iv-muted">面试官由你配置的聊天模型扮演</span></div>'
    + '  <div class="iv-fields iv-fields-2">'
    + '    <label class="iv-field iv-tip-able"' + ivTip('role') + '><span>目标岗位 *</span>'
    + '<input id="iv-cfg-role" type="text" value="'
    + ivEsc(role) + '" placeholder="例：产品经理"></label>'
    + '    <label class="iv-field iv-tip-able"' + ivTip('company') + '><span>公司（可选）</span>'
    + '<input id="iv-cfg-company" type="text" placeholder="例：字节跳动"></label>'
    + '    <label class="iv-field iv-tip-able"' + ivTip('type') + '><span>面试类型</span>'
    + '<select id="iv-cfg-type" class="iv-select">'
    + Object.keys(IV_TYPES).map((k) => '<option value="' + k + '"'
        + (k === s.type ? ' selected' : '') + '>' + IV_TYPES[k] + '</option>').join('')
    + '    </select></label>'
    + '    <label class="iv-field iv-tip-able"' + ivTip('lang') + '><span>语言</span>'
    + '<select id="iv-cfg-lang" class="iv-select">'
    + Object.keys(IV_LANGS).map((k) => '<option value="' + k + '"'
        + (k === s.lang ? ' selected' : '') + '>' + IV_LANGS[k] + '</option>').join('')
    + '    </select></label>'
    + '    <label class="iv-field iv-tip-able"' + ivTip('difficulty') + '><span>难度 <b id="iv-diff-val">'
    + s.difficulty + '</b>/5</span>'
    + '      <input id="iv-cfg-diff" type="range" min="1" max="5" step="1" value="' + s.difficulty + '"></label>'
    + '    <label class="iv-field iv-tip-able"' + ivTip('count') + '><span>题目数量</span>'
    + '<select id="iv-cfg-count" class="iv-select">'
    + [3, 5, 7, 10].map((n) => '<option value="' + n + '"'
        + (n === s.count ? ' selected' : '') + '>' + n + ' 题</option>').join('')
    + '    </select></label>'
    + '  </div>'
    + '  <label class="iv-check iv-tip-able"' + ivTip('useResume')
    + '><input type="checkbox" id="iv-cfg-use-resume"'
    + (s.useResume ? ' checked' : '') + '> 结合简历摘要出题（更有针对性）</label>'
    + '  <label class="iv-check iv-tip-able"' + ivTip('tts')
    + '><input type="checkbox" id="iv-cfg-tts"'
    + (s.tts ? ' checked' : '') + '> 面试官语音朗读（浏览器 TTS）</label>'
    + '  <div class="iv-actions iv-wrap"><button type="button" class="iv-btn primary" id="iv-start"'
    + ivTip('start') + '>开始面试</button>'
    + '    <button type="button" class="iv-btn ghost" id="iv-speech-check"'
    + ivTip('speechCheck') + '>检测语音识别</button></div>'
    + '  <div class="iv-hintline" id="iv-speech-line">语音作答依赖本地识别模型；未就绪时可以直接打字。</div>'
    + '</section>'
    + '<aside class="iv-card iv-side-note">'
    + '  <div class="iv-card-head"><span>怎么练更有效</span></div>'
    + '  <ol class="iv-list iv-list-num">'
    + '    <li>先把简历事实写清楚，面试官会围绕它追问。</li>'
    + '    <li>每题用「结论 → 情境 → 行动 → 结果」作答。</li>'
    + '    <li>答完看复盘里的参考结构，再重录一遍。</li>'
    + '    <li>专项训练适合碎片时间单题突破。</li>'
    + '  </ol>'
    + '</aside></div>';

  const q = (id) => panel.querySelector(id);
  q('#iv-cfg-diff').addEventListener('input', (e) => {
    q('#iv-diff-val').textContent = e.target.value;
    iv.store.settings.difficulty = Number(e.target.value) || 3;
    ivSaveStore();
  });
  q('#iv-cfg-type').addEventListener('change', (e) => {
    iv.store.settings.type = e.target.value;
    ivSaveStore();
  });
  q('#iv-cfg-lang').addEventListener('change', (e) => {
    iv.store.settings.lang = e.target.value;
    ivSaveStore();
  });
  q('#iv-cfg-count').addEventListener('change', (e) => {
    iv.store.settings.count = Number(e.target.value) || 5;
    ivSaveStore();
  });
  q('#iv-cfg-use-resume').addEventListener('change', (e) => {
    iv.store.settings.useResume = !!e.target.checked;
    ivSaveStore();
  });
  q('#iv-cfg-tts').addEventListener('change', (e) => {
    iv.store.settings.tts = !!e.target.checked;
    ivSaveStore();
    if (!e.target.checked) ivStopSpeak();
  });
  q('#iv-start').onclick = () => ivStartInterview(panel);
  q('#iv-speech-check').onclick = () => ivCheckSpeech(q('#iv-speech-line'));
  ivCheckSpeech(q('#iv-speech-line'), true);
}

async function ivCheckSpeech(line, silent) {
  if (!line) return;
  if (!silent) line.textContent = '正在检测语音识别模型…';
  try {
    const res = await fetch('/api/stt/status', { headers: { 'X-Auth-Token': TOKEN } });
    const j = await res.json();
    const sp = j && j.speech ? j.speech : {};
    if (sp.stream) {
      line.textContent = '语音识别已就绪：点麦克风即可作答。';
      line.className = 'iv-hintline ok';
    } else {
      line.textContent = '语音识别模型未下载：先运行 scripts/fetch_speech_model.py streaming，或直接打字作答。';
      line.className = 'iv-hintline warn';
    }
  } catch (e) {
    if (!silent) line.textContent = '检测失败：' + (e.message || e);
  }
}

async function ivStartInterview(panel) {
  if (iv.busy) return;
  const role = (document.getElementById('iv-cfg-role') || {}).value || '';
  if (!role.trim()) { toast('请先填写目标岗位', 'error'); return; }
  const cfg = {
    role: role.trim(),
    company: ((document.getElementById('iv-cfg-company') || {}).value || '').trim(),
    type: ((document.getElementById('iv-cfg-type') || {}).value) || 'mixed',
    lang: ((document.getElementById('iv-cfg-lang') || {}).value) || 'zh',
    difficulty: Number((document.getElementById('iv-cfg-diff') || {}).value) || 3,
    count: Number((document.getElementById('iv-cfg-count') || {}).value) || 5,
    resume_summary: '',
    jd: iv.store.resume.jd || '',
  };
  if (iv.store.settings.useResume && iv.store.resume.text) {
    cfg.resume_summary = iv.store.resume.text.slice(0, 2000);
  }
  iv.store.resume.role = cfg.role;
  iv.store.settings.type = cfg.type;
  iv.store.settings.lang = cfg.lang;
  iv.store.settings.difficulty = cfg.difficulty;
  iv.store.settings.count = cfg.count;
  ivSaveStore();
  const btn = document.getElementById('iv-start');
  iv.busy = true;
  ivBusyButton(btn, true, '准备面试…');
  try {
    const r = await api('/api/hall/interview/start', { config: cfg });
    iv.reportView = null;
    iv.mic.lastMs = 0;                 // 新一场从零开始算说话时长
    iv.session = {
      id: r.session_id,
      question: r.question,
      tip: r.tip || '',
      index: r.index || 0,
      total: r.total || cfg.count,
      config: r.config || cfg,
      transcript: [{ role: 'interviewer', text: r.question }],
      startedAt: Date.now(),
      qStart: Date.now(),
      draft: '',
      lastFeedback: null,
      done: false,
      showTip: false,
      speakMs: 0,
      degraded: !!r.degraded,
    };
    ivRenderPanel();
    ivSpeak(r.question);
    if (r.degraded) toast('未配置聊天模型：本次使用本地题库，评分会简化', 'error');
  } catch (e) {
    toast('开始失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
    ivBusyButton(btn, false);
  }
}

function ivTranscriptHtml(list) {
  return (list || []).map((t) => {
    if (t.role === 'interviewer') {
      return '<div class="iv-bubble interviewer"><div class="iv-bubble-label">面试官</div>'
        + '<div class="iv-bubble-text">' + ivEsc(t.text) + '</div></div>';
    }
    if (t.skipped) {
      return '<div class="iv-bubble candidate skipped"><div class="iv-bubble-label">你 · 跳过</div>'
        + '<div class="iv-bubble-text">这题先跳过</div></div>';
    }
    return '<div class="iv-bubble candidate"><div class="iv-bubble-label">你'
      + (t.score != null ? ' · ' + t.score + ' 分' : '') + '</div>'
      + '<div class="iv-bubble-text">' + ivEsc(t.text) + '</div></div>';
  }).join('');
}

function ivRenderRoom(panel) {
  const s = iv.session;
  if (!s) return;
  const fb = s.lastFeedback;
  panel.innerHTML = '<div class="iv-room">'
    + '<div class="iv-room-bar">'
    + '  <span class="iv-chip iv-tip-able"' + ivTip('progress') + '>第 '
    + Math.min(s.index + 1, s.total) + '/' + s.total + ' 题</span>'
    + '  <span class="iv-chip iv-timer iv-tip-able"' + ivTip('timer') + ' id="iv-room-timer">00:00</span>'
    + '  <span class="iv-chip iv-tip-able"' + ivTip('type') + '>'
    + ivEsc(IV_TYPES[s.config.type] || '') + ' · 难度 '
    + (s.config.difficulty || 3) + '/5</span>'
    + '  <span class="iv-chip">' + (s.config.lang === 'en' ? 'English' : '中文') + '</span>'
    + '  <span class="iv-bar-spacer"></span>'
    + '  <button type="button" class="iv-btn ghost small" id="iv-end"' + ivTip('end')
    + '>结束并看报告</button>'
    + '</div>'
    + '<div class="iv-question-card">'
    + '  <div class="iv-question-label">面试官提问</div>'
    + '  <div class="iv-question-text" id="iv-question">' + ivEsc(s.question || '') + '</div>'
    + '  <div class="iv-actions">'
    + '    <button type="button" class="iv-btn small" id="iv-replay"' + ivTip('replay')
    + '>重听</button>'
    + '    <button type="button" class="iv-btn small ghost" id="iv-hint"' + ivTip('hint')
    + '>提示</button>'
    + '  </div>'
    + '  <div class="iv-hintbox' + (s.showTip ? '' : ' hidden') + '" id="iv-tip">'
    + ivEsc(s.tip || '先说结论，再用 STAR 展开：情境、任务、行动、结果。') + '</div>'
    + '</div>'
    + '<div class="iv-transcript" id="iv-transcript">' + ivTranscriptHtml(s.transcript) + '</div>'
    + '<div class="iv-answer-card">'
    + '  <div class="iv-answer-head"><span>你的回答</span>'
    + '    <span class="iv-muted" id="iv-mic-state">' + (iv.mic.active ? '录音中…' : '') + '</span></div>'
    + '  <textarea id="iv-answer" class="iv-textarea iv-answer-text" spellcheck="false"'
    + ivTip('answer')
    + '    placeholder="点麦克风说话，或直接输入回答…">' + ivEsc(s.draft || '') + '</textarea>'
    + '  <div class="iv-actions iv-wrap">'
    + '    <button type="button" class="iv-btn iv-mic-btn" id="iv-mic"' + ivTip('mic')
    + '>语音作答</button>'
    + '    <button type="button" class="iv-btn ghost" id="iv-skip"' + ivTip('skipQuestion')
    + '>跳过这题</button>'
    + '    <button type="button" class="iv-btn primary" id="iv-submit"' + ivTip('submit')
    + '>提交回答</button>'
    + '  </div>'
    + '  <div class="iv-feedback iv-tip-able' + (fb ? '' : ' hidden') + '" id="iv-feedback"'
    + ivTip('feedback') + '>'
    + (fb ? (fb.score != null ? '<b>' + fb.score + ' 分</b>' : '')
      + '<span>' + ivEsc(fb.feedback || '') + '</span>' : '')
    + '</div>'
    + '</div>'
    + '</div>';

  const q = (id) => panel.querySelector(id);
  const ans = q('#iv-answer');
  ans.addEventListener('input', () => { if (iv.session) iv.session.draft = ans.value; });
  q('#iv-replay').onclick = () => ivSpeak(s.question);
  q('#iv-hint').onclick = () => {
    s.showTip = !s.showTip;
    q('#iv-tip').classList.toggle('hidden', !s.showTip);
  };
  q('#iv-mic').onclick = () => ivMicToggle('iv-answer');
  q('#iv-skip').onclick = () => ivSkipQuestion(panel);
  q('#iv-submit').onclick = () => ivSubmitAnswer(panel);
  ivHotkey(ans, () => ivSubmitAnswer(panel));
  q('#iv-end').onclick = () => ivFinishInterview(true);
  ivPaintMic();
  ans.focus();
  const tr = q('#iv-transcript');
  if (tr) tr.scrollTop = tr.scrollHeight;
  ivStartRoomTimer(panel);
}

function ivStartRoomTimer(panel) {
  ivClearRoomTimer();
  const tick = () => {
    if (!iv.session || !panel.isConnected) { ivClearRoomTimer(); return; }
    const el = panel.querySelector('#iv-room-timer');
    if (el) el.textContent = ivDuration(Date.now() - iv.session.startedAt);
  };
  tick();
  iv.roomTimer = setInterval(tick, 1000);
}

async function ivSubmitAnswer(panel) {
  const s = iv.session;
  if (!s || iv.busy) return;
  if (iv.mic.active) {
    // 先收麦再读输入框：最后的尾包和精修文本是在收麦之后才写回来的
    await ivMicStop(false);
    if (!iv.session || iv.session !== s) return;
  }
  const box = panel.querySelector('#iv-answer');
  const answer = box ? box.value.trim() : '';
  if (!answer) { toast('先写下或说出你的回答', 'error'); return; }
  const spoke = Number(iv.mic.lastMs) || 0;
  iv.mic.lastMs = 0;                       // 只算一次，别把上一题的录音时长重复记进来
  if (spoke) s.speakMs = (s.speakMs || 0) + spoke;
  // 语速只认「说话时长」：读题、思考和打字的时间都不算
  const duration = spoke;
  const btn = panel.querySelector('#iv-submit');
  iv.busy = true;
  ivBusyButton(btn, true, '提交中…');
  try {
    const r = await api('/api/hall/interview/turn', {
      session_id: s.id, answer: answer, duration_ms: duration,
    });
    s.transcript.push({ role: 'candidate', text: answer, score: r.score, feedback: r.feedback });
    if (r.question) s.transcript.push({ role: 'interviewer', text: r.question });
    s.question = r.question || '';
    s.tip = r.tip || '';
    s.index = r.index || 0;
    s.total = r.total || s.total;
    s.lastFeedback = { score: r.score, feedback: r.feedback };
    s.draft = '';
    s.showTip = false;
    s.qStart = Date.now();
    s.done = !!r.done;
    ivRenderPanel();
    if (r.done) {
      toast('这一场答完了，正在生成复盘…');
      setTimeout(() => ivFinishInterview(false), 500);
    } else {
      ivSpeak(r.question);
    }
  } catch (e) {
    toast('提交失败：' + (e.message || e), 'error');
    ivBusyButton(btn, false);
  } finally {
    iv.busy = false;
  }
}

/* 跳过本题：不计分、不计语速、不追问，直接推进到下一题 */
async function ivSkipQuestion(panel) {
  const s = iv.session;
  if (!s || iv.busy) return;
  if (iv.mic.active) {
    await ivMicStop(true);
    iv.mic.lastMs = 0;
    if (!iv.session || iv.session !== s) return;
  }
  iv.busy = true;
  try {
    const r = await api('/api/hall/interview/turn', { session_id: s.id, skip: true });
    s.transcript.push({ role: 'candidate', text: '（跳过）', skipped: true });
    if (r.question) s.transcript.push({ role: 'interviewer', text: r.question });
    s.question = r.question || '';
    s.tip = r.tip || '';
    s.index = r.index || 0;
    s.total = r.total || s.total;
    s.lastFeedback = { score: null, feedback: r.feedback || '这题先跳过。' };
    s.draft = '';
    s.showTip = false;
    s.qStart = Date.now();
    s.done = !!r.done;
    ivRenderPanel();
    if (r.done) {
      toast('这一场答完了，正在生成复盘…');
      setTimeout(() => ivFinishInterview(false), 500);
    } else {
      ivSpeak(r.question);
    }
  } catch (e) {
    toast('跳过失败：' + (e.message || e), 'error');
  } finally {
    iv.busy = false;
  }
}

async function ivFinishInterview(manual) {
  const s = iv.session;
  if (!s || iv.finishing) return;
  const answered = s.transcript.filter((t) => t.role === 'candidate' && !t.skipped);
  if (!answered.length) {
    if (manual) { toast('还没有作答，先回答一题吧', 'error'); return; }
    // 全部跳过：没有可复盘的内容，别生成一份 0 分报告存进记录
    iv.session = null;
    iv.reportView = null;
    ivRenderPanel();
    toast('这一场没有有效作答，没有生成复盘', 'error');
    return;
  }
  if (manual && !confirm('结束面试并生成复盘报告？已答 ' + answered.length + ' 题。')) return;
  ivMicStop(true);
  ivStopSpeak();
  iv.finishing = true;
  ivRenderPanel();
  const meta = { role: s.config.role, company: s.config.company,
    type: s.config.type, lang: s.config.lang, id: s.id };
  try {
    const r = await api('/api/hall/interview/report', { session_id: s.id });
    iv.reportView = {
      report: r.report || {},
      expression: r.expression || {},
      transcript: r.transcript || s.transcript,
      meta: meta,
    };
    ivStoreHistory(meta, iv.reportView);
    iv.session = null;
    iv.finishing = false;
    ivRenderPanel();
    toast('复盘报告已生成', 'success');
  } catch (e) {
    iv.finishing = false;
    ivRenderPanel();
    toast('生成报告失败：' + (e.message || e), 'error');
  }
}

/* ---------------- 浏览器 TTS ---------------- */

function ivPickVoice(lang) {
  if (!window.speechSynthesis) return null;
  const list = window.speechSynthesis.getVoices() || [];
  const re = lang === 'en' ? /^en([-_]|$)/i : /^zh([-_]|$)/i;
  const cands = list.filter((v) => re.test(v.lang || ''));
  const prefer = cands.find((v) => /Xiaoxiao|Yunxi|Yunyang|Huihui|Tingting|Meijia|Google/i
    .test(v.name || ''));
  return prefer || cands[0] || null;
}

function ivSpeak(text) {
  if (!iv.store || !iv.store.settings.tts || !text) return;
  if (!window.speechSynthesis || typeof SpeechSynthesisUtterance === 'undefined') return;
  try {
    window.speechSynthesis.cancel();
    const lang = (iv.session && iv.session.config.lang) || iv.store.settings.lang || 'zh';
    const u = new SpeechSynthesisUtterance(String(text).slice(0, 600));
    const v = ivPickVoice(lang);
    if (v) u.voice = v;
    u.lang = v ? v.lang : (lang === 'en' ? 'en-US' : 'zh-CN');
    u.rate = Number(iv.store.settings.rate) || 1;
    window.speechSynthesis.speak(u);
  } catch (e) { /* 浏览器不支持 / 被拦截 */ }
}

function ivStopSpeak() {
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) { /* 忽略 */ }
}

/* ---------------- 本地语音识别（复用 /api/stt/*） ---------------- */

function ivResample(input, inRate, outRate) {
  if (!input.length || inRate === outRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

function ivEncodePcm16(f32) {
  const buf = new ArrayBuffer(f32.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return buf;
}

function ivConcatF32(chunks) {
  let total = 0;
  chunks.forEach((c) => { total += c.length; });
  const all = new Float32Array(total);
  let off = 0;
  chunks.forEach((c) => { all.set(c, off); off += c.length; });
  return all;
}

function ivPaintMic() {
  document.querySelectorAll('.iv-mic-btn').forEach((btn) => {
    btn.classList.toggle('recording', !!iv.mic.active);
    btn.textContent = iv.mic.active ? '停止并转写' : '语音作答';
  });
}

async function ivMicToggle(inputId) {
  if (iv.mic.active) return ivMicStop(false);
  return ivMicStart(inputId);
}

async function ivMicStart(inputId) {
  if (iv.mic.active) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('当前浏览器不支持麦克风采集', 'error');
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    toast('无法访问麦克风（请检查系统或浏览器权限）', 'error');
    return;
  }
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx;
  try { ctx = new AC(); await ctx.resume(); } catch (e) { toast('音频初始化失败', 'error'); return; }
  if (!ctx.createScriptProcessor) {
    try { stream.getTracks().forEach((t) => t.stop()); ctx.close(); } catch (e) { /* 忽略 */ }
    toast('当前浏览器不支持录音采集方式', 'error');
    return;
  }
  const source = ctx.createMediaStreamSource(stream);
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const sink = ctx.createGain();
  sink.gain.value = 0;
  iv.mic = {
    active: true, inputId: inputId, pending: [], text: '', sid: '', sending: false,
    stream: stream, ctx: ctx, source: source, node: node, sink: sink,
    sampleRate: ctx.sampleRate || 48000, startedAt: Date.now(),
    timer: null, uiTimer: null, lastMs: 0,
  };
  node.onaudioprocess = (e) => {
    if (iv.mic.active) iv.mic.pending.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };
  source.connect(node); node.connect(sink); sink.connect(ctx.destination);
  iv.mic.timer = setInterval(ivMicPush, 500);
  iv.mic.uiTimer = setInterval(ivMicTick, 500);
  ivPaintMic();
  ivMicTick();
  api('/api/stt/start', { denoise: false }).then((r) => {
    if (iv.mic.active && r && r.ok) iv.mic.sid = r.sid;
  }).catch((e) => {
    if (iv.mic.active) { toast(e.message || '无法开始识别', 'error'); ivMicStop(true); }
  });
}

async function ivMicPush() {
  if (!iv.mic.active || !iv.mic.sid || iv.mic.sending) return;
  const pend = iv.mic.pending;
  iv.mic.pending = [];
  if (!pend.length) return;
  const pcm = ivEncodePcm16(ivResample(ivConcatF32(pend), iv.mic.sampleRate, 16000));
  iv.mic.sending = true;
  try {
    const res = await fetch('/api/stt/chunk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Auth-Token': TOKEN,
        'X-Stt-Sid': iv.mic.sid },
      body: pcm,
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok && data.text && iv.mic.active) {
      iv.mic.text = data.text;
      ivMicWrite();
    }
  } catch (e) { /* 单次失败忽略，下一轮继续 */ }
  finally { iv.mic.sending = false; }
}

function ivMicTick() {
  if (!iv.mic.active) return;
  ivPaintMic();
  const el = document.getElementById('iv-mic-state');
  if (!el) return;
  const sec = Math.round((Date.now() - iv.mic.startedAt) / 1000);
  el.textContent = '录音中 ' + sec + 's' + (iv.mic.text ? ' · ' + iv.mic.text.slice(-18) : '');
}

function ivMicWrite() {
  const el = document.getElementById(iv.mic.inputId || '');
  if (!el) return;
  const prefix = el.dataset.micPrefix != null ? el.dataset.micPrefix : el.value.replace(/\s*$/, '');
  el.dataset.micPrefix = prefix;
  el.value = (prefix ? prefix + ' ' : '') + (iv.mic.text || '');
  // 让宿主（面试 / 专项训练）同步草稿，重渲染时不至于丢掉刚转写的内容
  el.dispatchEvent(new Event('input', { bubbles: true }));
  if (typeof autoGrow === 'function') autoGrow();
}

async function ivMicStop(silent) {
  if (!iv.mic.active) return '';
  const m = iv.mic;
  m.active = false;
  if (m.timer) clearInterval(m.timer);
  if (m.uiTimer) clearInterval(m.uiTimer);
  try { m.node.onaudioprocess = null; } catch (e) { /* 忽略 */ }
  try { m.source.disconnect(); m.node.disconnect(); m.sink.disconnect(); } catch (e) { /* 忽略 */ }
  try { m.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
  const rate = m.sampleRate;
  try { m.ctx.close(); } catch (e) { /* 忽略 */ }
  const pend = m.pending || [];
  const sid = m.sid;
  const spokeMs = Math.max(0, Date.now() - (m.startedAt || Date.now()));
  iv.mic = { active: false, inputId: '', pending: [], text: '', sid: '',
    sending: false, lastMs: spokeMs };
  ivPaintMic();
  const state = document.getElementById('iv-mic-state');
  if (state) state.textContent = '转写中…';
  let text = m.text || '';
  try {
    if (pend.length && sid) {
      const pcm = ivEncodePcm16(ivResample(ivConcatF32(pend), rate, 16000));
      const res = await fetch('/api/stt/chunk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', 'X-Auth-Token': TOKEN,
          'X-Stt-Sid': sid },
        body: pcm,
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok && data.text) text = data.text;
    }
    if (sid) {
      const r = await api('/api/stt/finish', { sid: sid });
      if (r && r.ok && r.text) text = r.text;
    }
  } catch (e) { /* 用已有文本 */ }
  if (text) {
    const el = document.getElementById(m.inputId || '');
    if (el) {
      const prefix = el.value.replace(/\s*$/, '');
      el.value = (prefix ? prefix + ' ' : '') + text;
      iv.mic.text = '';
      if (iv.session) iv.session.draft = el.value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      if (typeof autoGrow === 'function') autoGrow();
    }
  } else if (!silent) {
    toast('没听清，请再说一次或直接打字', 'error');
  }
  if (state) state.textContent = '';
  ivPaintMic();
  return text;
}

/* ---------------- 复盘报告 ---------------- */

function ivCheckScore(score) {
  const v = Number(score) || 0;
  return v >= 80 ? 'good' : (v >= 60 ? 'mid' : 'bad');
}

function ivExprHtml(e) {
  if (!e || !e.answers || !e.answers.length) return '';
  return '<div class="iv-expr iv-tip-able"' + ivTip('expression') + '>'
    + '<div class="iv-expr-item"><b>' + (e.speed || 0) + '</b><i>' + ivEsc(e.unit || '字/分') + '</i></div>'
    + '<div class="iv-expr-item"><b>' + ivEsc(e.speed_level || '—') + '</b><i>语速</i></div>'
    + '<div class="iv-expr-item"><b>' + (e.fillers || 0) + '</b><i>口头禅</i></div>'
    + '<div class="iv-expr-item"><b>' + (e.filler_rate || 0) + '%</b><i>口头禅占比</i></div>'
    + '<div class="iv-expr-item"><b>' + (e.seconds || 0) + 's</b><i>总作答时长</i></div>'
    + '</div>'
    + '<div class="iv-hintline">' + (e.estimated ? '部分时长按字数估算；' : '')
    + '语速参考区间 ' + (e.ideal ? e.ideal.min + '-' + e.ideal.max : '—') + ' ' + ivEsc(e.unit || '')
    + '。</div>';
}

function ivReportBodyHtml(report, expression) {
  const r = report || {};
  let h = '<div class="iv-score-row">' + ivRing(r.overall, '总分') + '<div>'
    + '<div class="iv-score-title">面试复盘</div>'
    + '<div class="iv-summary">' + ivEsc(r.summary || '') + '</div>'
    + (r.degraded ? '<div class="iv-warnchip">本地规则版：连接聊天模型后可获得逐题参考答案</div>' : '')
    + '</div></div>';
  h += ivDimsHtml(r.dimensions);
  h += ivExprHtml(expression);
  if (r.strengths && r.strengths.length) {
    h += '<div class="iv-sec-title">做得好的</div><div class="iv-chips">'
      + r.strengths.map((x) => '<span class="iv-chip ok">' + ivEsc(x) + '</span>').join('') + '</div>';
  }
  if (r.improvements && r.improvements.length) {
    h += '<div class="iv-sec-title">优先改进</div>' + r.improvements.map((it) => '<div class="iv-improve">'
      + '<div class="iv-improve-title">' + ivEsc(it.issue || '') + '</div>'
      + (it.evidence ? '<div class="iv-improve-ev">证据：' + ivEsc(it.evidence) + '</div>' : '')
      + (it.action ? '<div class="iv-improve-act">行动：' + ivEsc(it.action) + '</div>' : '')
      + '</div>').join('');
  }
  if (r.per_question && r.per_question.length) {
    h += '<div class="iv-sec-title iv-tip-able"' + ivTip('perQuestion') + '>逐题回顾</div>'
      + r.per_question.map((q, i) =>
      '<details class="iv-pq"><summary><span class="iv-pq-no">' + (i + 1) + '</span>'
      + '<span class="iv-pq-q">' + ivEsc(q.question || '') + '</span>'
      + '<span class="iv-pq-score ' + ivCheckScore(q.score) + '">' + (q.score || 0) + '</span></summary>'
      + (q.comment ? '<div class="iv-pq-comment">' + ivEsc(q.comment) + '</div>' : '')
      + (q.better_answer ? '<div class="iv-pq-better"><b>参考结构</b>' + ivMd(q.better_answer) + '</div>' : '')
      + '</details>').join('');
  }
  if (r.next_steps && r.next_steps.length) {
    h += '<div class="iv-sec-title">下一步</div><ul class="iv-list">'
      + r.next_steps.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>';
  }
  return h;
}

function ivRenderReportView(panel) {
  const d = iv.reportView;
  if (!d) { ivRenderInterviewConfig(panel); return; }
  const meta = d.meta || {};
  panel.innerHTML = '<div class="iv-card iv-report-card">'
    + '<div class="iv-room-bar">'
    + '  <span class="iv-chip">' + ivEsc(meta.role || '') + '</span>'
    + (meta.company ? '<span class="iv-chip">' + ivEsc(meta.company) + '</span>' : '')
    + '  <span class="iv-chip">' + ivEsc(IV_TYPES[meta.type] || '') + '</span>'
    + '  <span class="iv-bar-spacer"></span>'
    + '  <button type="button" class="iv-btn small ghost" id="iv-report-copy"'
    + ivTip('reportCopy') + '>复制报告</button>'
    + '  <button type="button" class="iv-btn small ghost" id="iv-report-download"'
    + ivTip('reportDownload') + '>下载 .md</button>'
    + '</div>'
    + '<div class="iv-report-scroll">' + ivReportBodyHtml(d.report, d.expression) + '</div>'
    + '<div class="iv-actions">'
    + '  <button type="button" class="iv-btn primary" id="iv-report-again"'
    + ivTip('reportAgain') + '>再来一场</button>'
    + '  <button type="button" class="iv-btn" id="iv-report-history"'
    + ivTip('history') + '>查看复盘记录</button>'
    + '</div></div>';
  const md = () => ivReportMarkdown(d.report, d.expression, meta);
  panel.querySelector('#iv-report-copy').onclick = () => {
    ivCopy(md()).then(() => toast('报告已复制', 'success')).catch(() => toast('复制失败', 'error'));
  };
  panel.querySelector('#iv-report-download').onclick = () => {
    ivDownload('面试复盘-' + new Date().toISOString().slice(0, 10) + '.md', md());
  };
  panel.querySelector('#iv-report-again').onclick = () => {
    iv.reportView = null;
    ivRenderPanel();
  };
  panel.querySelector('#iv-report-history').onclick = () => ivSetTab('history');
}

function ivReportMarkdown(report, expression, meta) {
  const r = report || {};
  const m = meta || {};
  const lines = ['# 面试复盘', ''];
  if (m.role) lines.push('- 岗位：' + m.role + (m.company ? '（' + m.company + '）' : ''));
  if (m.type) lines.push('- 类型：' + (IV_TYPES[m.type] || m.type));
  lines.push('- 时间：' + ivDate(Date.now()));
  lines.push('');
  lines.push('## 总分 ' + (r.overall || 0));
  if (r.summary) lines.push('', r.summary);
  if (r.dimensions) {
    lines.push('', '## 维度评分');
    r.dimensions.forEach((d) => lines.push('- ' + d.name + '：' + d.score + (d.comment ? '（' + d.comment + '）' : '')));
  }
  if (expression && expression.answers && expression.answers.length) {
    lines.push('', '## 表达力');
    lines.push('- 语速：' + (expression.speed || 0) + ' ' + (expression.unit || ''));
    lines.push('- 语速评价：' + (expression.speed_level || '—'));
    lines.push('- 口头禅：' + (expression.fillers || 0) + ' 次（占比 ' + (expression.filler_rate || 0) + '%）');
  }
  if (r.strengths && r.strengths.length) {
    lines.push('', '## 做得好的');
    r.strengths.forEach((x) => lines.push('- ' + x));
  }
  if (r.improvements && r.improvements.length) {
    lines.push('', '## 优先改进');
    r.improvements.forEach((x) => {
      lines.push('- **' + (x.issue || '') + '**');
      if (x.evidence) lines.push('  - 证据：' + x.evidence);
      if (x.action) lines.push('  - 行动：' + x.action);
    });
  }
  if (r.per_question && r.per_question.length) {
    lines.push('', '## 逐题回顾');
    r.per_question.forEach((q, i) => {
      lines.push('', '### ' + (i + 1) + '. ' + (q.question || ''));
      lines.push('- 得分：' + (q.score || 0));
      if (q.comment) lines.push('- 点评：' + q.comment);
      if (q.better_answer) lines.push('', '参考结构：', '', q.better_answer);
    });
  }
  if (r.next_steps && r.next_steps.length) {
    lines.push('', '## 下一步');
    r.next_steps.forEach((x) => lines.push('- ' + x));
  }
  return lines.join('\n');
}

function ivStoreHistory(meta, data) {
  if (!meta || !meta.id) return;
  if ((iv.store.history || []).some((h) => h && h.sessionId === meta.id)) return;
  const item = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    sessionId: meta.id,
    at: Date.now(),
    role: meta.role || '',
    company: meta.company || '',
    type: meta.type || 'mixed',
    lang: meta.lang || 'zh',
    score: Number((data.report && data.report.overall) || 0),
    summary: (data.report && data.report.summary) || '',
    report: data.report || {},
    expression: data.expression || {},
    transcript: (data.transcript || []).slice(-40),
  };
  iv.store.history.unshift(item);
  if (iv.store.history.length > IV_HISTORY_LIMIT) iv.store.history.length = IV_HISTORY_LIMIT;
  ivSaveStore();
}

/* ============================================================
   三、职业规划
   ============================================================ */

function ivStoreCareer() {
  const c = iv.career;
  if (!c) return;
  iv.store.career = {
    qa: c.qa.slice(-30), step: c.step || 0,
    current: c.guide.question || '', plan: c.plan || null,
    selected: c.selected || '', draft: c.input || '', updatedAt: Date.now(),
  };
  ivSaveStore();
}

function ivRenderCareer(panel) {
  const c = iv.career;
  if (!c) return;
  if (c.loadingPlan) {
    panel.innerHTML = '<div class="iv-card iv-loading-card"><span class="iv-spinner"></span>'
      + '<div>正在整理方向、对比和行动计划…</div></div>';
    return;
  }
  if (c.plan) { ivRenderCareerPlan(panel); return; }
  ivRenderCareerIntake(panel);
}

function ivRenderCareerIntake(panel) {
  const c = iv.career;
  const g = c.guide;
  let chat = '';
  c.qa.forEach((it) => {
    if (it.q) chat += ivAiBubble(it.q, true);
    chat += ivAiBubble(it.a || '（未回答）', false);
  });
  if (g.loading) {
    chat += '<div class="iv-ai-thinking"><span class="iv-spinner"></span>正在想下一个问题…</div>';
  } else if (g.done) {
    chat += ivAiBubble(g.summary || '信息差不多了，可以生成职业规划。', true);
  } else if (g.question) {
    chat += ivAiBubble(g.question, true);
    if (g.why) chat += '<div class="iv-ai-why">为什么问：' + ivEsc(g.why) + '</div>';
    if (g.examples && g.examples.length) {
      chat += '<div class="iv-ai-examples">'
        + g.examples.map((x) => '<button type="button" class="iv-ai-example">'
          + ivEsc(x) + '</button>').join('') + '</div>';
    }
  } else {
    chat += '<div class="iv-ai-thinking">还没有开始盘点。</div>';
  }
  if (g.hint) chat += '<div class="iv-ai-hintbox">' + ivEsc(g.hint) + '</div>';

  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-card-head"><span class="iv-tip-able"' + ivTip('tabCareer')
    + '>职业规划 · 自我盘点</span><span class="iv-muted">'
    + (c.qa.length ? '已聊 ' + c.qa.length + ' 轮' : '先花 3 分钟做盘点') + '</span></div>'
    + '<div class="iv-hintline">不知道该投什么很正常。先回答几个问题，AI 会结合你的回答和简历给出方向；'
    + '这些回答只保存在本机。</div>'
    + '<div class="iv-ai-chat iv-career-chat" id="iv-career-chat">' + chat + '</div>'
    + (g.done ? '' : '<textarea id="iv-career-answer" class="iv-textarea iv-ai-input"'
      + ' spellcheck="false" placeholder="用你自己的话回答，记得多少写多少…">'
      + ivEsc(c.input || '') + '</textarea>'
      + '<div class="iv-actions iv-wrap">'
      + '<button type="button" class="iv-btn primary" id="iv-career-submit"'
      + (g.loading ? ' disabled' : '') + '>提交回答</button>'
      + '<button type="button" class="iv-btn" id="iv-career-hint"' + ivTip('aiHint')
      + (g.loading ? ' disabled' : '') + '>不知道，给我点提示</button>'
      + '<button type="button" class="iv-btn ghost" id="iv-career-skip"' + ivTip('aiSkip')
      + (g.loading ? ' disabled' : '') + '>跳过这题</button></div>')
    + '<div class="iv-actions iv-career-actions">'
    + '<button type="button" class="iv-btn primary" id="iv-career-generate">生成职业规划</button>'
    + '<button type="button" class="iv-btn ghost" id="iv-career-reset"' + ivTip('careerRedo')
    + '>重新开始</button>'
    + '<span class="iv-muted">会结合简历和上面的回答；简历可在简历工作台填写</span></div>'
    + '</div>';

  const ta = panel.querySelector('#iv-career-answer');
  if (ta) {
    ta.addEventListener('input', () => {
      if (!iv.career) return;
      iv.career.input = ta.value;
      iv.store.career.draft = ta.value;     // 草稿防抖落盘，切页签也不丢
      ivQueueSave();
    });
    ivHotkey(ta, () => ivCareerGuideSubmit(false));
  }
  panel.querySelectorAll('.iv-ai-example').forEach((btn) => {
    btn.onclick = () => {
      if (!ta) return;
      ta.value = btn.textContent;
      if (iv.career) {
        iv.career.input = ta.value;
        iv.store.career.draft = ta.value;
        ivQueueSave();
      }
      ta.focus();
    };
  });
  const submit = panel.querySelector('#iv-career-submit');
  if (submit) submit.onclick = () => ivCareerGuideSubmit(false);
  const hint = panel.querySelector('#iv-career-hint');
  if (hint) hint.onclick = ivCareerGuideHint;
  const skip = panel.querySelector('#iv-career-skip');
  if (skip) skip.onclick = () => ivCareerGuideSubmit(true);
  ivBindBubbleCopy(panel);
  panel.querySelector('#iv-career-generate').onclick = () => ivCareerGenerate('');
  panel.querySelector('#iv-career-reset').onclick = ivCareerReset;
  const chatBox = panel.querySelector('#iv-career-chat');
  if (chatBox) chatBox.scrollTop = chatBox.scrollHeight;
  if (ta) ta.focus();
  if (!g.question && !g.done && !g.loading) ivCareerGuideNext(false);
}

async function ivCareerGuideNext(hint, skip) {
  const c = iv.career;
  if (!c || c.busy || c.guide.loading) return;
  c.guide.loading = true;
  c.guide.hint = '';
  ivRenderPanel();
  try {
    const r = await api('/api/hall/interview/career/guide', {
      qa: c.qa, step: c.step || 0, current_question: c.guide.question || '',
      hint: !!hint, skip: !!skip,
    });
    if (!iv.career || iv.career !== c) return;
    c.guide.loading = false;
    c.guide.question = (r && r.question) || (hint ? c.guide.question : '');
    c.guide.why = (r && r.why) || '';
    c.guide.examples = (r && r.examples) || [];
    c.guide.hint = (r && r.hint) || '';
    if (hint && !c.guide.hint && c.guide.examples.length) {
      c.guide.hint = '可以从这些里挑一个，或者只说你记得的部分：'
        + c.guide.examples.join('；');
    }
    c.guide.done = !!(r && r.done) && !hint;
    c.guide.summary = (r && r.summary) || '';
    if (r && typeof r.step === 'number') c.step = r.step;
    if (c.guide.done) c.guide.question = '';
    ivStoreCareer();
    ivRenderPanel();
  } catch (e) {
    if (!iv.career || iv.career !== c) return;
    c.guide.loading = false;
    c.guide.done = true;
    c.guide.summary = '盘点暂时不可用，可以先写几句自己的情况再生成规划。';
    toast('盘点失败：' + (e.message || e), 'error');
    ivRenderPanel();
  }
}

function ivCareerGuideSubmit(skip) {
  const c = iv.career;
  if (!c || c.busy || c.guide.loading) return;
  const ta = document.getElementById('iv-career-answer');
  const ans = skip ? IV_SKIP_ANSWER : (ta ? ta.value.trim() : '');
  if (!ans) { toast('先写下回答，或点「跳过这题」', 'error'); return; }
  if (c.guide.question) c.qa.push({ q: c.guide.question, a: ans, at: Date.now() });
  c.input = '';
  c.guide.hint = '';
  ivStoreCareer();
  ivCareerGuideNext(false, skip);
}

function ivCareerGuideHint() {
  const c = iv.career;
  if (!c || c.guide.loading) return;
  const ta = document.getElementById('iv-career-answer');
  if (ta) c.input = ta.value;
  ivCareerGuideNext(true);
}

async function ivCareerGenerate(selectedName) {
  const c = iv.career;
  if (!c || c.busy) return;
  if (!c.qa.length && (iv.store.resume.text || '').trim().length < 20) {
    toast('至少回答一个问题，或先在简历工作台填写简历', 'error');
    return;
  }
  c.busy = true;
  c.loadingPlan = true;
  c.selected = selectedName || c.selected || '';
  ivRenderPanel();
  try {
    const r = await api('/api/hall/interview/career/plan', {
      qa: c.qa, resume: iv.store.resume.text, selected: c.selected,
    });
    if (!iv.career || iv.career !== c) return;
    c.plan = r;
    c.selected = (r && r.selected && r.selected.name) || c.selected;
    c.loadingPlan = false;
    c.busy = false;
    ivStoreCareer();
    ivRenderPanel();
    toast(r.degraded ? '已生成本地规则版规划' : '职业规划已生成', 'success');
  } catch (e) {
    c.loadingPlan = false;
    c.busy = false;
    ivRenderPanel();
    toast('生成失败：' + (e.message || e), 'error');
  }
}

function ivCareerReset() {
  const c = iv.career;
  if (!c) return;
  if (!confirm('清空本轮盘点和规划，重新开始？')) return;
  c.qa = [];
  c.step = 0;
  c.current = '';
  c.plan = null;
  c.selected = '';
  c.input = '';
  c.busy = false;
  c.loadingPlan = false;
  c.guide = { question: '', why: '', examples: [], hint: '',
    done: false, summary: '', loading: false };
  ivStoreCareer();
  ivRenderPanel();
}

function ivCareerPlanHtml(plan) {
  const p = plan || {};
  let h = '<div class="iv-score-row">'
    + '<div class="iv-ring iv-tip-able" style="--v:' + (p.degraded ? 55 : 80) + '">'
    + '<span>' + (p.degraded ? '≈' : 'AI') + '</span></div><div>'
    + '<div class="iv-score-title">职业规划</div>'
    + '<div class="iv-summary">' + ivEsc(p.summary || '') + '</div>'
    + '<div class="iv-warnchip">方向基于你提供的信息；薪资、招聘量等外部数据请在招聘平台自行核实</div>'
    + '</div></div>';

  h += '<div class="iv-sec-title">方向对比</div>'
    + '<div class="iv-compare-wrap"><table class="iv-compare"><thead><tr>'
    + '<th>方向</th><th>匹配</th><th>入门难度</th><th>成长</th><th>缺口</th><th>验证成本</th>'
    + '</tr></thead><tbody>'
    + (p.comparison || []).map((it) => '<tr><td>' + ivEsc(it.name || '') + '</td>'
      + '<td>' + (it.fit || 0) + '</td><td>' + ivEsc(it.entry_difficulty || '') + '</td>'
      + '<td>' + ivEsc(it.growth || '') + '</td><td>' + ivEsc(it.gap || '') + '</td>'
      + '<td>' + ivEsc(it.verify_cost || '') + '</td></tr>').join('')
    + '</tbody></table></div>';

  h += '<div class="iv-sec-title">可选方向</div><div class="iv-career-grid">'
    + (p.directions || []).map((d) => '<div class="iv-career-card">'
      + '<div class="iv-career-head"><b>' + ivEsc(d.name || '') + '</b>'
      + '<span class="iv-career-fit iv-tip-able"' + ivTip('careerFit') + '>匹配 '
      + (d.fit || 0) + '</span></div>'
      + '<div class="iv-career-line">' + ivEsc(d.one_liner || '') + '</div>'
      + (d.why && d.why.length ? '<div class="iv-career-sec">为什么适合你</div><ul class="iv-list">'
        + d.why.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>' : '')
      + (d.entry_roles && d.entry_roles.length
        ? '<div class="iv-career-sec">典型入门岗位</div><div class="iv-chips">'
          + d.entry_roles.map((x) => '<span class="iv-chip">' + ivEsc(x) + '</span>').join('') + '</div>' : '')
      + (d.skills && d.skills.length
        ? '<div class="iv-career-sec">需要补的能力</div><div class="iv-chips">'
          + d.skills.map((x) => '<span class="iv-chip">' + ivEsc(x) + '</span>').join('') + '</div>' : '')
      + (d.path && d.path.length ? '<div class="iv-career-sec">成长路径</div>'
        + '<div class="iv-career-path">' + d.path.map((x) => '<div class="iv-career-step">'
          + '<b>' + ivEsc(x.stage || '') + '</b><span>' + ivEsc(x.focus || '') + '</span>'
          + (x.milestones && x.milestones.length ? '<em>' + ivEsc(x.milestones.join(' / ')) + '</em>' : '')
          + '</div>').join('') + '</div>' : '')
      + (d.risks && d.risks.length ? '<div class="iv-career-sec">风险 / 可能不适合</div><ul class="iv-list">'
        + d.risks.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>' : '')
      + (d.verify_actions && d.verify_actions.length
        ? '<div class="iv-career-sec iv-tip-able"' + ivTip('careerVerify') + '>本周就能验证</div>'
          + '<ul class="iv-list">' + d.verify_actions.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>' : '')
      + '<div class="iv-career-card-actions"><button type="button" class="iv-btn small primary"'
      + ' data-pick="' + ivEsc(d.name || '') + '">按这个方向做计划</button></div>'
      + '</div>').join('') + '</div>';

  const sel = p.selected || {};
  if (sel.name) {
    h += '<div class="iv-sec-title">选定方向：' + ivEsc(sel.name) + '</div>';
    if (sel.gaps && sel.gaps.length) {
      h += '<div class="iv-gap-list">' + sel.gaps.map((g) => '<div class="iv-gap">'
        + '<div><b>已有</b>' + ivEsc(g.have || '') + '</div>'
        + '<div><b>需要</b>' + ivEsc(g.need || '') + '</div>'
        + '<div class="iv-gap-act"><b>行动</b>' + ivEsc(g.action || '') + '</div>'
        + '</div>').join('') + '</div>';
    }
    if (sel.plan && sel.plan.length) {
      h += '<div class="iv-plan-grid">' + sel.plan.map((x) => '<div class="iv-plan-card">'
        + '<div class="iv-plan-period">' + ivEsc(x.period || '') + '</div>'
        + (x.goals && x.goals.length ? '<div class="iv-career-sec">目标</div><ul class="iv-list">'
          + x.goals.map((v) => '<li>' + ivEsc(v) + '</li>').join('') + '</ul>' : '')
        + (x.outputs && x.outputs.length ? '<div class="iv-career-sec">产出</div><ul class="iv-list">'
          + x.outputs.map((v) => '<li>' + ivEsc(v) + '</li>').join('') + '</ul>' : '')
        + (x.checks && x.checks.length ? '<div class="iv-career-sec">验收</div><ul class="iv-list">'
          + x.checks.map((v) => '<li>' + ivEsc(v) + '</li>').join('') + '</ul>' : '')
        + '</div>').join('') + '</div>';
    }
    if (sel.search_keywords && sel.search_keywords.length) {
      h += '<div class="iv-career-sec">可以去招聘平台搜</div><div class="iv-chips">'
        + sel.search_keywords.map((x) => '<span class="iv-chip">' + ivEsc(x) + '</span>').join('')
        + '</div>';
    }
  }
  if (p.questions_for_user && p.questions_for_user.length) {
    h += '<div class="iv-sec-title">还需要你想清楚</div><ul class="iv-list">'
      + p.questions_for_user.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>';
  }
  return h;
}

function ivRenderCareerPlan(panel) {
  const c = iv.career;
  const p = c.plan || {};
  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-card-head"><span class="iv-tip-able"' + ivTip('tabCareer')
    + '>职业规划</span><span class="iv-muted">方向不是终身决定，先用小成本验证</span>'
    + '<span class="iv-bar-spacer"></span>'
    + '<button type="button" class="iv-btn small ghost" id="iv-career-export"'
    + ivTip('careerExport') + '>导出 Markdown</button>'
    + '<button type="button" class="iv-btn small ghost" id="iv-career-redo"'
    + ivTip('careerRedo') + '>重新盘点</button></div>'
    + '<div class="iv-report-scroll">' + ivCareerPlanHtml(p) + '</div>'
    + '<div class="iv-actions"><button type="button" class="iv-btn primary" id="iv-career-interview"'
    + ivTip('careerLink') + '>用这个方向开始模拟面试</button></div>'
    + '</div>';

  panel.querySelector('#iv-career-export').onclick = () => {
    ivDownload('职业规划-' + new Date().toISOString().slice(0, 10) + '.md',
      ivCareerMarkdown(p));
  };
  panel.querySelector('#iv-career-redo').onclick = ivCareerReset;
  panel.querySelectorAll('[data-pick]').forEach((b) => {
    b.onclick = () => ivCareerGenerate(b.dataset.pick || '');
  });
  panel.querySelector('#iv-career-interview').onclick = () => {
    const name = (p.selected && p.selected.name) || (p.directions && p.directions[0]
      && p.directions[0].name) || '';
    if (!name) return;
    iv.store.resume.role = name;
    ivSaveStore();
    ivSetTab('interview');
    toast('已把「' + name + '」填入面试目标岗位');
  };
}

function ivCareerMarkdown(plan) {
  const p = plan || {};
  const lines = ['# 职业规划', '', p.summary || '', '',
    '> 方向基于个人提供的信息；薪资、招聘量等外部数据请自行核实。', ''];
  lines.push('## 方向对比', '', '| 方向 | 匹配 | 入门难度 | 成长 | 缺口 | 验证成本 |',
    '|---|---|---|---|---|---|');
  (p.comparison || []).forEach((x) => {
    lines.push('| ' + (x.name || '') + ' | ' + (x.fit || 0) + ' | ' + (x.entry_difficulty || '')
      + ' | ' + (x.growth || '') + ' | ' + (x.gap || '') + ' | ' + (x.verify_cost || '') + ' |');
  });
  lines.push('', '## 可选方向');
  (p.directions || []).forEach((d) => {
    lines.push('', '### ' + (d.name || '') + '（匹配 ' + (d.fit || 0) + '）', '', d.one_liner || '');
    if (d.why && d.why.length) lines.push('', '**为什么适合你**', ...d.why.map((x) => '- ' + x));
    if (d.entry_roles && d.entry_roles.length) lines.push('', '**入门岗位**：' + d.entry_roles.join('、'));
    if (d.skills && d.skills.length) lines.push('**需要补**：' + d.skills.join('、'));
    if (d.path && d.path.length) {
      lines.push('', '**成长路径**');
      d.path.forEach((x) => lines.push('- ' + (x.stage || '') + '：' + (x.focus || '')));
    }
    if (d.risks && d.risks.length) lines.push('', '**风险**', ...d.risks.map((x) => '- ' + x));
    if (d.verify_actions && d.verify_actions.length) {
      lines.push('', '**本周验证**', ...d.verify_actions.map((x) => '- ' + x));
    }
  });
  const sel = p.selected || {};
  if (sel.name) {
    lines.push('', '## 选定方向：' + sel.name);
    (sel.gaps || []).forEach((g) => {
      lines.push('', '- 已有：' + (g.have || '') + '；需要：' + (g.need || '') + '；行动：' + (g.action || ''));
    });
    (sel.plan || []).forEach((x) => {
      lines.push('', '### ' + (x.period || ''));
      if (x.goals && x.goals.length) lines.push('**目标**', ...x.goals.map((v) => '- ' + v));
      if (x.outputs && x.outputs.length) lines.push('**产出**', ...x.outputs.map((v) => '- ' + v));
      if (x.checks && x.checks.length) lines.push('**验收**', ...x.checks.map((v) => '- ' + v));
    });
    if (sel.search_keywords && sel.search_keywords.length) {
      lines.push('', '**搜索关键词**：' + sel.search_keywords.join('、'));
    }
  }
  if (p.questions_for_user && p.questions_for_user.length) {
    lines.push('', '## 还需要想清楚', ...p.questions_for_user.map((x) => '- ' + x));
  }
  return lines.join('\n');
}

/* ============================================================
   四、专项训练
   ============================================================ */

function ivRenderDrill(panel) {
  if (iv.drill.current && iv.drill.result) { ivRenderDrillResult(panel); return; }
  if (iv.drill.current) { ivRenderDrillPractice(panel); return; }
  if (!iv.drill.items.length && !iv.drill.loading) ivLoadDrillQuestions();
  const type = iv.drill.type;
  const lang = iv.drill.lang;
  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-card-head"><span class="iv-tip-able"' + ivTip('drill')
    + '>专项训练</span><span class="iv-muted">'
    + (iv.drill.loading ? '加载题库…' : iv.drill.items.length + ' 道题') + '</span></div>'
    + '<div class="iv-actions iv-wrap iv-drill-filters">'
    + Object.keys(IV_TYPES).map((k) => '<button type="button" class="iv-filter'
      + (k === type ? ' active' : '') + '" data-type="' + k + '"' + ivTip('type') + '>'
      + IV_TYPES[k] + '</button>').join('')
    + '<span class="iv-bar-spacer"></span>'
    + '<select id="iv-drill-lang" class="iv-select"' + ivTip('lang') + '><option value="zh"'
    + (lang === 'zh' ? ' selected' : '') + '>中文</option><option value="en"'
    + (lang === 'en' ? ' selected' : '') + '>English</option></select>'
    + '<button type="button" class="iv-btn small ghost" id="iv-drill-refresh"'
    + ivTip('refresh') + '>换一批</button>'
    + '</div>'
    + (iv.drill.loading
      ? '<div class="iv-loading"><span class="iv-spinner"></span>正在准备题目…</div>'
      : (iv.drill.items.length
        ? '<div class="iv-drill-grid">' + iv.drill.items.map((it, i) => '<button type="button"'
          + ' class="iv-drill-item" data-i="' + i + '"' + ivTip('drillItem')
          + '><span class="iv-drill-no">' + (i + 1) + '</span>'
          + '<span class="iv-drill-q">' + ivEsc(it.question) + '</span>'
          + '<span class="iv-drill-tip">' + ivEsc(it.tip || '') + '</span></button>').join('')
          + '</div>'
        : '<div class="iv-empty"><div class="iv-empty-title">题库加载失败</div>'
          + '<div class="iv-empty-sub">点击「换一批」重试。</div></div>'))
    + '</div>';

  panel.querySelectorAll('.iv-filter').forEach((b) => {
    b.onclick = () => {
      iv.drill.type = b.dataset.type;
      iv.drill.items = [];
      ivRenderPanel();
    };
  });
  const langSel = panel.querySelector('#iv-drill-lang');
  if (langSel) langSel.onchange = () => {
    iv.drill.lang = langSel.value;
    iv.drill.items = [];
    ivRenderPanel();
  };
  panel.querySelector('#iv-drill-refresh').onclick = () => {
    iv.drill.items = [];
    ivRenderPanel();
  };
  panel.querySelectorAll('.iv-drill-item').forEach((b) => {
    b.onclick = () => {
      iv.drill.current = iv.drill.items[Number(b.dataset.i)] || null;
      iv.drill.result = null;
      iv.drill.startedAt = Date.now();
      iv.drill.draft = '';
      ivRenderPanel();
      if (iv.drill.current) ivSpeak(iv.drill.current.question);
    };
  });
}

async function ivLoadDrillQuestions() {
  if (iv.drill.loading) return;
  iv.drill.loading = true;
  const gen = iv.gen;
  try {
    const r = await api('/api/hall/interview/questions', {
      type: iv.drill.type, lang: iv.drill.lang, n: 12, shuffle: true,
    });
    if (gen !== iv.gen) return;
    iv.drill.items = (r && r.items) || [];
  } catch (e) {
    if (gen === iv.gen) iv.drill.items = [];
    toast('题库加载失败：' + (e.message || e), 'error');
  } finally {
    iv.drill.loading = false;
    if (gen === iv.gen && iv.tab === 'drill') ivRenderPanel();
  }
}

function ivRenderDrillPractice(panel) {
  const it = iv.drill.current;
  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-room-bar">'
    + '  <button type="button" class="iv-btn small ghost" id="iv-drill-back"' + ivTip('back')
    + '>‹ 返回题库</button>'
    + '  <span class="iv-chip iv-tip-able"' + ivTip('type') + '>' + ivEsc(it.category || '') + '</span>'
    + '  <span class="iv-chip iv-timer iv-tip-able"' + ivTip('timer') + ' id="iv-drill-timer">00:00</span>'
    + '  <span class="iv-bar-spacer"></span>'
    + '  <button type="button" class="iv-btn small" id="iv-drill-replay"' + ivTip('replay')
    + '>重听题目</button>'
    + '</div>'
    + '<div class="iv-question-card">'
    + '  <div class="iv-question-label">练习题</div>'
    + '  <div class="iv-question-text">' + ivEsc(it.question) + '</div>'
    + '  <details class="iv-drill-hint iv-tip-able"' + ivTip('hint') + '><summary>提示</summary><div>'
    + ivEsc(it.tip || '用 STAR 结构：情境、任务、行动、结果。') + '</div></details>'
    + '</div>'
    + '<div class="iv-answer-card">'
    + '  <div class="iv-answer-head"><span>你的回答</span>'
    + '    <span class="iv-muted" id="iv-mic-state">' + (iv.mic.active ? '录音中…' : '') + '</span></div>'
    + '  <textarea id="iv-drill-answer" class="iv-textarea iv-answer-text" spellcheck="false"'
    + ivTip('answer')
    + '    placeholder="输入或说出你的回答…"></textarea>'
    + '  <div class="iv-actions">'
    + '    <button type="button" class="iv-btn iv-mic-btn" id="iv-drill-mic"' + ivTip('mic')
    + '>语音作答</button>'
    + '    <button type="button" class="iv-btn primary" id="iv-drill-submit"' + ivTip('submit')
    + '>提交点评</button>'
    + '  </div>'
    + '</div></div>';
  const q = (id) => panel.querySelector(id);
  q('#iv-drill-back').onclick = () => {
    ivMicStop(true);
    iv.drill.current = null;
    iv.drill.result = null;
    ivClearRoomTimer();
    ivRenderPanel();
  };
  q('#iv-drill-replay').onclick = () => ivSpeak(it.question);
  q('#iv-drill-mic').onclick = () => ivMicToggle('iv-drill-answer');
  q('#iv-drill-submit').onclick = () => ivDrillSubmit(panel);
  ivHotkey(q('#iv-drill-answer'), () => ivDrillSubmit(panel));
  ivPaintMic();
  q('#iv-drill-answer').focus();
  ivClearRoomTimer();
  const tick = () => {
    const el = panel.querySelector('#iv-drill-timer');
    if (el) el.textContent = ivDuration(Date.now() - iv.drill.startedAt);
  };
  tick();
  iv.roomTimer = setInterval(tick, 1000);
}

async function ivDrillSubmit(panel) {
  const it = iv.drill.current;
  if (!it) return;
  // 先收麦再读输入框，避免最后一段转写漏掉
  if (iv.mic.active) {
    await ivMicStop(false);
    iv.mic.lastMs = 0;
  }
  const box = panel.querySelector('#iv-drill-answer');
  const answer = box ? box.value.trim() : '';
  if (answer.length < 10) { toast('回答太短，至少写 10 个字符', 'error'); return; }
  const btn = panel.querySelector('#iv-drill-submit');
  ivBusyButton(btn, true, '点评中…');
  try {
    const r = await api('/api/hall/interview/drill', {
      question: it.question, answer: answer, lang: iv.drill.lang, tip: it.tip,
    });
    iv.drill.result = r;
    iv.drill.answer = answer;
    ivRenderPanel();
  } catch (e) {
    toast('点评失败：' + (e.message || e), 'error');
    ivBusyButton(btn, false);
  }
}

function ivRenderDrillResult(panel) {
  const r = iv.drill.result || {};
  const it = iv.drill.current || {};
  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-room-bar">'
    + '  <button type="button" class="iv-btn small ghost" id="iv-drill-back"' + ivTip('back')
    + '>‹ 返回题库</button>'
    + '  <span class="iv-chip iv-tip-able"' + ivTip('type') + '>' + ivEsc(it.category || '') + '</span>'
    + '  <span class="iv-bar-spacer"></span>'
    + '  <button type="button" class="iv-btn small" id="iv-drill-again"' + ivTip('again')
    + '>重新作答</button>'
    + '  <button type="button" class="iv-btn small primary" id="iv-drill-next"' + ivTip('next')
    + '>下一题</button>'
    + '</div>'
    + '<div class="iv-score-row">' + ivRing(r.score, '本题') + '<div>'
    + '<div class="iv-score-title">' + ivEsc(it.question || '') + '</div>'
    + '<div class="iv-summary">' + ivEsc(r.comment || '') + '</div>'
    + (r.degraded ? '<div class="iv-warnchip">本地规则版点评：连接聊天模型后可得到参考回答</div>' : '')
    + '</div></div>'
    + (r.strengths && r.strengths.length
      ? '<div class="iv-sec-title">做得好的</div><div class="iv-chips">'
        + r.strengths.map((x) => '<span class="iv-chip ok">' + ivEsc(x) + '</span>').join('') + '</div>' : '')
    + (r.improvements && r.improvements.length
      ? '<div class="iv-sec-title">可以更好</div><ul class="iv-list">'
        + r.improvements.map((x) => '<li>' + ivEsc(x) + '</li>').join('') + '</ul>' : '')
    + (r.better_answer
      ? '<div class="iv-sec-title">参考回答</div><div class="iv-md">' + ivMd(r.better_answer) + '</div>' : '')
    + '<div class="iv-sec-title">我的回答</div><div class="iv-my-answer">'
    + ivEsc(iv.drill.answer || '') + '</div>'
    + '</div>';
  const q = (id) => panel.querySelector(id);
  q('#iv-drill-back').onclick = () => {
    iv.drill.current = null;
    iv.drill.result = null;
    ivClearRoomTimer();
    ivRenderPanel();
  };
  q('#iv-drill-again').onclick = () => {
    iv.drill.result = null;
    iv.drill.startedAt = Date.now();
    ivRenderPanel();
  };
  q('#iv-drill-next').onclick = () => {
    const idx = iv.drill.items.indexOf(iv.drill.current);
    if (idx >= 0 && idx < iv.drill.items.length - 1) {
      iv.drill.current = iv.drill.items[idx + 1];
      iv.drill.result = null;
      iv.drill.startedAt = Date.now();
      ivRenderPanel();
      ivSpeak(iv.drill.current.question);
    } else {
      iv.drill.current = null;
      iv.drill.result = null;
      iv.drill.items = [];
      ivRenderPanel();
      toast('这一批练完了，已换一批新题');
    }
  };
}

/* ============================================================
   四、复盘记录
   ============================================================ */

function ivRenderHistory(panel) {
  const list = iv.store.history || [];
  if (!list.length) {
    panel.innerHTML = '<div class="iv-empty"><div class="iv-empty-ico">🗂</div>'
      + '<div class="iv-empty-title">还没有面试记录</div>'
      + '<div class="iv-empty-sub">完成一场模拟面试后，复盘报告会自动保存在这里（最近 '
      + IV_HISTORY_LIMIT + ' 场）。</div>'
      + '<button type="button" class="iv-btn primary" id="iv-history-start">去面试</button></div>';
    const b = panel.querySelector('#iv-history-start');
    if (b) b.onclick = () => ivSetTab('interview');
    return;
  }
  const trend = list.slice(0, 10).reverse();
  panel.innerHTML = '<div class="iv-card">'
    + '<div class="iv-card-head"><span class="iv-tip-able"' + ivTip('history')
    + '>复盘记录</span><span class="iv-muted">共 ' + list.length + ' 场</span>'
    + '<span class="iv-bar-spacer"></span>'
    + '<button type="button" class="iv-btn small ghost" id="iv-history-export"'
    + ivTip('export') + '>导出 JSON</button>'
    + '<button type="button" class="iv-btn small ghost" id="iv-history-clear"'
    + ivTip('clearData') + '>清空记录</button></div>'
    + '<div class="iv-trend iv-tip-able"' + ivTip('trend') + '>'
    + trend.map((h) => '<div class="iv-trend-col" title="'
      + ivEsc(h.role + ' · ' + (h.score || 0) + ' 分') + '">'
      + '<span class="iv-trend-score">' + (h.score || 0) + '</span>'
      + '<i style="height:' + Math.max(8, Math.min(100, Number(h.score) || 0)) + '%"></i></div>').join('')
    + '</div>'
    + '<div class="iv-history-list">' + list.map((h, i) => '<div class="iv-history-item">'
      + '<button type="button" class="iv-history-head" data-i="' + i + '"' + ivTip('expand') + '>'
      + '<span class="iv-history-when">' + ivDate(h.at) + '</span>'
      + '<span class="iv-history-role">' + ivEsc(h.role || '') + '</span>'
      + '<span class="iv-chip">' + ivEsc(IV_TYPES[h.type] || h.type || '') + '</span>'
      + '<span class="iv-history-score ' + ivCheckScore(h.score) + ' iv-tip-able"'
      + ivTip('historyScore') + '>'
      + (h.score || 0) + '</span>'
      + '<span class="iv-history-open">展开</span></button>'
      + '<div class="iv-history-body hidden"></div>'
      + '<div class="iv-history-actions"><button type="button" class="iv-btn small ghost" data-del="'
      + i + '"' + ivTip('delete') + '>删除</button></div>'
      + '</div>').join('') + '</div>'
    + '</div>';

  panel.querySelector('#iv-history-export').onclick = () => {
    ivDownload('面试记录-' + new Date().toISOString().slice(0, 10) + '.json',
      JSON.stringify(list, null, 2));
  };
  panel.querySelector('#iv-history-clear').onclick = () => {
    if (!confirm('清空全部复盘记录？此操作不可撤销。')) return;
    iv.store.history = [];
    ivSaveStore();
    ivRenderPanel();
    toast('已清空记录');
  };
  panel.querySelectorAll('.iv-history-head').forEach((b) => {
    b.onclick = () => {
      const item = list[Number(b.dataset.i)];
      const body = b.parentElement.querySelector('.iv-history-body');
      if (!body || !item) return;
      const open = !body.classList.contains('hidden');
      body.classList.toggle('hidden', open);
      b.querySelector('.iv-history-open').textContent = open ? '展开' : '收起';
      if (!open && !body.dataset.done) {
        body.innerHTML = '<div class="iv-report-scroll">'
          + ivReportBodyHtml(item.report, item.expression) + '</div>';
        body.dataset.done = '1';
      }
    };
  });
  panel.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = () => {
      const i = Number(b.dataset.del);
      if (!confirm('删除这条记录？')) return;
      iv.store.history.splice(i, 1);
      ivSaveStore();
      ivRenderPanel();
      toast('已删除');
    };
  });
}

/* ============================================================
   五、设置
   ============================================================ */

/* 清空本机数据后，内存里的临时状态也要一起清，否则切到别的页签还能看到旧内容 */
function ivResetRuntime() {
  iv.analysis = null;
  iv.polish = null;
  iv.jdMatch = null;
  iv.session = null;
  iv.reportView = null;
  iv.finishing = false;
  iv.builder = null;
  iv.guideFocus = '';
  iv.career = {
    qa: [], step: 0, current: '', plan: null, selected: '', input: '',
    busy: false, loadingPlan: false,
    guide: { question: '', why: '', examples: [], hint: '', done: false,
      summary: '', loading: false },
  };
  iv.drill = { items: [], type: iv.store.settings.type, lang: iv.store.settings.lang,
    current: null, result: null, startedAt: 0 };
}

function ivOpenSettings() {
  const s = iv.store.settings;
  const ov = ivOpenOverlay('设置', '面试官语音、默认面试参数与本地数据。');
  ov.body.innerHTML = ''
    + '<label class="iv-check iv-tip-able"' + ivTip('tts')
    + '><input type="checkbox" id="iv-set-tts"' + (s.tts ? ' checked' : '')
    + '> 面试官语音朗读（浏览器 TTS）</label>'
    + '<div class="iv-field iv-tip-able"' + ivTip('rate') + '><span>语速 <b id="iv-set-rate-val">'
    + s.rate.toFixed(1) + '</b>×</span>'
    + '<input type="range" id="iv-set-rate" min="0.6" max="1.6" step="0.1" value="' + s.rate + '"></div>'
    + '<div class="iv-fields iv-fields-2">'
    + '<label class="iv-field iv-tip-able"' + ivTip('lang') + '><span>默认语言</span>'
    + '<select id="iv-set-lang" class="iv-select">'
    + Object.keys(IV_LANGS).map((k) => '<option value="' + k + '"'
      + (k === s.lang ? ' selected' : '') + '>' + IV_LANGS[k] + '</option>').join('') + '</select></label>'
    + '<label class="iv-field iv-tip-able"' + ivTip('type') + '><span>默认面试类型</span>'
    + '<select id="iv-set-type" class="iv-select">'
    + Object.keys(IV_TYPES).map((k) => '<option value="' + k + '"'
      + (k === s.type ? ' selected' : '') + '>' + IV_TYPES[k] + '</option>').join('') + '</select></label>'
    + '</div>'
    + '<div class="iv-set-status iv-tip-able"' + ivTip('speechStatus')
    + ' id="iv-set-speech">语音识别状态检测中…</div>'
    + '<div class="iv-set-data iv-tip-able"' + ivTip('data') + '>本地数据：简历 '
    + (iv.store.resume.text || '').length + ' 字 · 面试记录 '
    + (iv.store.history || []).length + ' 场</div>'
    + '<div class="iv-actions"><button type="button" class="iv-btn ghost" id="iv-set-export"'
    + ivTip('export') + '>导出全部数据</button>'
    + '<button type="button" class="iv-btn danger" id="iv-set-clear"' + ivTip('clearData')
    + '>清空全部数据</button></div>';
  ov.foot.innerHTML = '<button type="button" class="iv-btn primary" id="iv-set-done">完成</button>';

  const rate = ov.body.querySelector('#iv-set-rate');
  rate.addEventListener('input', () => {
    ov.body.querySelector('#iv-set-rate-val').textContent = Number(rate.value).toFixed(1);
    iv.store.settings.rate = Number(rate.value) || 1;
    ivSaveStore();
  });
  ov.body.querySelector('#iv-set-tts').addEventListener('change', (e) => {
    iv.store.settings.tts = !!e.target.checked;
    ivSaveStore();
    if (!e.target.checked) ivStopSpeak();
  });
  ov.body.querySelector('#iv-set-lang').addEventListener('change', (e) => {
    iv.store.settings.lang = e.target.value;
    iv.drill.lang = e.target.value;
    ivSaveStore();
  });
  ov.body.querySelector('#iv-set-type').addEventListener('change', (e) => {
    iv.store.settings.type = e.target.value;
    iv.drill.type = e.target.value;
    ivSaveStore();
  });
  ov.body.querySelector('#iv-set-export').onclick = () => {
    ivDownload('模拟面试数据-' + new Date().toISOString().slice(0, 10) + '.json',
      JSON.stringify(iv.store, null, 2));
  };
  ov.body.querySelector('#iv-set-clear').onclick = () => {
    if (!confirm('清空简历、引导问答、面试记录与设置？此操作不可撤销。')) return;
    iv.store = ivDefaultStore();
    ivResetRuntime();
    ivSaveStore();
    ivCloseOverlay();
    ivRenderPanel();
    toast('已清空本地数据');
  };
  ov.foot.querySelector('#iv-set-done').onclick = ivCloseOverlay;
  ivRefreshSpeechStatus(ov.body.querySelector('#iv-set-speech'));
}

async function ivRefreshSpeechStatus(el) {
  if (!el) return;
  try {
    const res = await fetch('/api/stt/status', { headers: { 'X-Auth-Token': TOKEN } });
    const j = await res.json();
    const sp = (j && j.speech) || {};
    if (sp.stream) {
      el.textContent = '语音识别：已就绪（流式模型已加载路径 ' + (sp.stream_dir || '默认目录') + '）';
      el.className = 'iv-set-status ok';
    } else {
      el.textContent = '语音识别：未就绪。运行 scripts/fetch_speech_model.py streaming 后可语音作答。';
      el.className = 'iv-set-status warn';
    }
  } catch (e) {
    el.textContent = '语音识别：状态获取失败（' + (e.message || e) + '）';
    el.className = 'iv-set-status warn';
  }
}

