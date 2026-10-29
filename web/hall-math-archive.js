/* 数学竞技场：错题本、对局记录、笔记（从 hall-math.js 拆出） */

/* ---------------- 错题本 / 对局记录 / 笔记 ---------------- */

function mathTimeText(ts) {
  const d = new Date(Number(ts) || 0);
  if (isNaN(d.getTime())) return '未知时间';
  const p = (n) => (n < 10 ? '0' + n : String(n));
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function mathNoteStamp() {
  const d = new Date();
  const p = (n) => (n < 10 ? '0' + n : String(n));
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function mathOpenOverlay(title, sub) {
  mathCloseOverlay();
  const ov = document.createElement('div');
  ov.className = 'math-modal';
  const card = document.createElement('div');
  card.className = 'math-modal-card';
  const head = document.createElement('div');
  head.className = 'math-modal-head';
  const t = document.createElement('div');
  t.className = 'math-modal-title';
  t.textContent = title;
  const x = document.createElement('button');
  x.type = 'button';
  x.className = 'math-modal-x';
  x.textContent = '×';
  x.title = '关闭';
  x.onclick = mathCloseOverlay;
  head.appendChild(t);
  head.appendChild(x);
  card.appendChild(head);
  if (sub) {
    const s = document.createElement('div');
    s.className = 'math-modal-sub';
    s.textContent = sub;
    card.appendChild(s);
  }
  const body = document.createElement('div');
  body.className = 'math-modal-body';
  const foot = document.createElement('div');
  foot.className = 'math-modal-foot';
  card.appendChild(body);
  card.appendChild(foot);
  ov.appendChild(card);
  ov.onclick = (e) => { if (e.target === ov) mathCloseOverlay(); };
  document.body.appendChild(ov);
  math.overlay = { el: ov, card: card, body: body, foot: foot };
  return math.overlay;
}

function mathCloseOverlay() {
  if (math.exam && math.exam.timer) mathExamStopTimer();
  if (math.overlay && math.overlay.el && math.overlay.el.parentNode) {
    math.overlay.el.parentNode.removeChild(math.overlay.el);
  }
  math.overlay = null;
}

function mathWrongStatus(w) {
  if (!w || w.legacy) return { id: 'legacy', label: '旧记录' };
  const now = Date.now();
  if (!w.due || w.due <= now) return { id: 'due', label: mathReviewLabel(w) };
  return { id: 'later', label: mathReviewLabel(w) };
}

function mathWrongCard(w) {
  const item = document.createElement('div');
  item.className = 'math-note-item';
  const head = document.createElement('div');
  head.className = 'math-note-head';
  const meta = document.createElement('span');
  meta.className = 'math-note-meta';
  meta.textContent = (w.course_name || w.subject_name || '未分类')
    + (w.skill ? ' · ' + w.skill : '')
    + ' · ' + mathWrongStatus(w).label;
  head.appendChild(meta);
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'math-note-del';
  del.textContent = '删除';
  del.title = '从错题本删除这道题';
  del.onclick = () => {
    if (!confirm('从错题本删除这道题？')) return;
    math.store.wrong = math.store.wrong.filter((x) => x !== w);
    mathSaveStore();
    mathRenderTop();
    mathRenderSide();
    mathOpenWrongBook();
  };
  head.appendChild(del);
  item.appendChild(head);

  const prompt = document.createElement('div');
  prompt.className = 'math-note-prompt';
  mathRichText(prompt, w.prompt || '');
  item.appendChild(prompt);

  const ans = document.createElement('div');
  ans.className = 'math-note-answer';
  mathRichText(ans, '正确答案：' + (w.answer || '—'));
  item.appendChild(ans);
  if (w.my_answer) {
    const mine = document.createElement('div');
    mine.className = 'math-note-mine';
    mathRichText(mine, '我的答案：' + w.my_answer);
    item.appendChild(mine);
  }

  const causeRow = document.createElement('label');
  causeRow.className = 'math-note-cause';
  const causeLabel = document.createElement('span');
  causeLabel.textContent = '错因';
  causeRow.appendChild(causeLabel);
  const causeSel = document.createElement('select');
  causeSel.className = 'math-select';
  ['', ...MATH_EXAM_CAUSES].forEach((c) => {
    const o = document.createElement('option');
    o.value = c;
    o.textContent = c || '未标注';
    if ((w.cause || '') === c) o.selected = true;
    causeSel.appendChild(o);
  });
  causeSel.onchange = () => {
    w.cause = causeSel.value;
    mathSaveStore();
    toast(w.cause ? ('错因已标为「' + w.cause + '」') : '已清除错因');
  };
  causeRow.appendChild(causeSel);
  item.appendChild(causeRow);

  const det = document.createElement('details');
  det.className = 'math-note-details';
  const sum = document.createElement('summary');
  sum.textContent = '解析 / 完整解';
  det.appendChild(sum);
  if (w.explain) {
    const p = document.createElement('div');
    p.className = 'math-note-explain';
    mathRichText(p, w.explain);
    det.appendChild(p);
  }
  const steps = Array.isArray(w.steps) ? w.steps : [];
  if (steps.length) {
    const ol = document.createElement('ol');
    ol.className = 'math-steps';
    steps.forEach((s) => {
      const li = document.createElement('li');
      mathRichText(li, String(s));
      ol.appendChild(li);
    });
    det.appendChild(ol);
  }
  if (w.exam_tip) {
    const p = document.createElement('div');
    p.className = 'math-note-tip';
    mathRichText(p, '考试套路：' + w.exam_tip);
    det.appendChild(p);
  }
  if (w.scene) {
    const p = document.createElement('div');
    p.className = 'math-note-scene';
    mathRichText(p, '用在哪：' + w.scene);
    det.appendChild(p);
  }
  item.appendChild(det);

  if (!w.legacy) {
    const acts = document.createElement('div');
    acts.className = 'math-note-actions';
    const one = document.createElement('button');
    one.type = 'button';
    one.className = 'math-btn';
    one.textContent = '练这道';
    one.onclick = () => { mathCloseOverlay(); mathBeginReview([w]); };
    acts.appendChild(one);
    item.appendChild(acts);
  }
  return item;
}

function mathOpenWrongBook() {
  if (!math.store) return;
  mathClosePicker();
  const list = math.store.wrong || [];
  const due = mathDueRecords().length;
  const ov = mathOpenOverlay('错题本', '共 ' + list.length + ' 道错题'
    + (due ? ' · 今日到期 ' + due + ' 道' : ''));
  const filters = document.createElement('div');
  filters.className = 'math-filters';

  const courseSel = document.createElement('select');
  courseSel.className = 'math-filter';
  const courseIds = [];
  list.forEach((w) => {
    if (w.course && MATH_COURSES[w.course] && courseIds.indexOf(w.course) < 0) {
      courseIds.push(w.course);
    }
  });
  courseIds.sort((a, b) => MATH_COURSE_ORDER.indexOf(a) - MATH_COURSE_ORDER.indexOf(b));
  courseSel.appendChild(new Option('全部课程', 'all'));
  courseIds.forEach((c) => courseSel.appendChild(new Option(MATH_COURSES[c], c)));
  const wanted = (MATH_COURSES[math.wrongFilter.course]
    && courseIds.indexOf(math.wrongFilter.course) >= 0) ? math.wrongFilter.course : 'all';
  math.wrongFilter.course = wanted;
  courseSel.value = wanted;
  courseSel.onchange = () => {
    math.wrongFilter.course = courseSel.value;
    mathOpenWrongBook();
  };

  const statusSel = document.createElement('select');
  statusSel.className = 'math-filter';
  [['all', '全部状态'], ['due', '待复习'], ['later', '已排期'], ['legacy', '旧记录']]
    .forEach((pair) => statusSel.appendChild(new Option(pair[1], pair[0])));
  statusSel.value = math.wrongFilter.status;
  statusSel.onchange = () => {
    math.wrongFilter.status = statusSel.value;
    mathOpenWrongBook();
  };

  filters.appendChild(courseSel);
  filters.appendChild(statusSel);
  ov.body.appendChild(filters);

  const shown = list.filter((w) => {
    if (math.wrongFilter.course !== 'all' && w.course !== math.wrongFilter.course) return false;
    if (math.wrongFilter.status !== 'all'
      && mathWrongStatus(w).id !== math.wrongFilter.status) return false;
    return true;
  });
  const count = document.createElement('div');
  count.className = 'math-panel-note';
  count.textContent = '显示 ' + shown.length + ' / ' + list.length + ' 道';
  ov.body.appendChild(count);

  if (!shown.length) {
    const empty = document.createElement('div');
    empty.className = 'math-panel-note math-note-empty';
    empty.textContent = list.length ? '没有符合筛选条件的错题。' : '还没有错题，先做几道题吧。';
    ov.body.appendChild(empty);
  } else {
    shown.forEach((w) => ov.body.appendChild(mathWrongCard(w)));
  }

  const review = document.createElement('button');
  review.type = 'button';
  review.className = 'math-btn primary';
  review.textContent = due ? ('开始复习 ' + due) : '今日无到期错题';
  review.disabled = !due;
  review.onclick = () => { mathCloseOverlay(); mathStartReview(); };
  const notes = document.createElement('button');
  notes.type = 'button';
  notes.className = 'math-btn';
  notes.textContent = '整理成笔记';
  notes.disabled = !list.length;
  notes.onclick = mathOpenNotes;
  const clear = document.createElement('button');
  clear.type = 'button';
  clear.className = 'math-clear';
  clear.textContent = '清空错题本';
  clear.disabled = !list.length || math.mode === 'review';
  if (clear.disabled) clear.title = math.mode === 'review' ? '复习进行中，结束后再清空' : '没有可清空的错题';
  clear.onclick = () => {
    if (!confirm('清空全部错题？这个操作不可恢复。')) return;
    math.store.wrong = [];
    mathSaveStore();
    mathRenderTop();
    mathRenderSide();
    mathOpenWrongBook();
  };
  ov.foot.appendChild(review);
  ov.foot.appendChild(notes);
  const spacer = document.createElement('span');
  spacer.className = 'math-modal-spacer';
  ov.foot.appendChild(spacer);
  ov.foot.appendChild(clear);
}

function mathBuildNotes() {
  const list = (math.store && math.store.wrong) ? math.store.wrong.slice() : [];
  if (!list.length) return '';
  const md = (text) => {
    const raw = String(text == null ? '' : text);
    return mathRichSplit(raw).map((part) => {
      if (!part.math) return part.text;
      const leading = (part.text.match(/^\s*/) || [''])[0];
      const trailing = (part.text.match(/\s*$/) || [''])[0];
      const core = part.text.slice(leading.length, part.text.length - trailing.length);
      if (!core) return part.text;
      return leading + '$' + mathTexify(core) + '$' + trailing;
    }).join('');
  };
  const groups = {};
  list.forEach((w) => {
    const key = (w.course && MATH_COURSES[w.course]) ? w.course : (w.subject || 'other');
    if (!groups[key]) groups[key] = [];
    groups[key].push(w);
  });
  const keys = Object.keys(groups).sort((a, b) => {
    const ia = MATH_COURSE_ORDER.indexOf(a);
    const ib = MATH_COURSE_ORDER.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
  });
  const lines = [];
  lines.push('# 数学错题笔记');
  lines.push('');
  lines.push('> 生成时间：' + mathTimeText(Date.now()) + ' · 共 ' + list.length + ' 道错题');
  lines.push('');
  keys.forEach((key) => {
    const items = groups[key];
    const name = MATH_COURSES[key] || items[0].course_name
      || items[0].subject_name || '未分类';
    lines.push('## ' + name);
    lines.push('');
    const skills = [];
    items.forEach((w) => {
      const s = w.skill || '未标注';
      if (skills.indexOf(s) < 0) skills.push(s);
    });
    skills.forEach((skill) => {
      lines.push('### ' + skill);
      lines.push('');
      const group = items.filter((w) => (w.skill || '未标注') === skill);
      group.forEach((w, i) => {
        lines.push((i + 1) + '. **题目**：' + md(String(w.prompt || '').replace(/\n/g, ' ')));
        lines.push('   - **正确答案**：' + md(w.answer || '—'));
        if (w.my_answer) lines.push('   - **我的答案**：' + md(w.my_answer));
        if (w.explain) {
          lines.push('   - **解析**：' + md(String(w.explain).replace(/\n/g, ' ')));
        }
        const steps = Array.isArray(w.steps) ? w.steps : [];
        if (steps.length) {
          lines.push('   - **完整解**：');
          steps.forEach((s, k) => {
            lines.push('     ' + (k + 1) + '. ' + md(String(s).replace(/\n/g, ' ')));
          });
        }
        if (w.exam_tip) lines.push('   - **考试套路**：' + md(w.exam_tip));
        if (w.scene) lines.push('   - **用在哪**：' + md(w.scene));
        lines.push('   - **复习状态**：' + mathReviewLabel(w));
        lines.push('');
      });
    });
  });
  return lines.join('\n').trim() + '\n';
}

function mathCopyFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  toast(ok ? 'Markdown 已复制' : '复制失败，请手动选择预览内容', ok ? undefined : 'error');
}

function mathCopyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => toast('Markdown 已复制'))
      .catch(() => mathCopyFallback(text));
    return;
  }
  mathCopyFallback(text);
}

function mathDownloadNotes(text, filename) {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || ('数学错题笔记-' + mathNoteStamp() + '.md');
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => { try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ } }, 1500);
  toast('笔记已开始下载');
}

function mathOpenNotes() {
  const text = mathBuildNotes();
  if (!text) { toast('还没有错题，先做几道题吧'); return; }
  mathClosePicker();
  const ov = mathOpenOverlay('错题笔记', '已按「课程 → 知识点」自动整理，可复制或下载 Markdown');
  const tools = document.createElement('div');
  tools.className = 'math-note-tools';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'math-btn primary';
  copy.textContent = '复制 Markdown';
  copy.onclick = () => mathCopyText(text);
  const dl = document.createElement('button');
  dl.type = 'button';
  dl.className = 'math-btn';
  dl.textContent = '下载 .md';
  dl.onclick = () => mathDownloadNotes(text);
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'math-btn ghost';
  back.textContent = '返回错题本';
  back.onclick = mathOpenWrongBook;
  tools.appendChild(copy);
  tools.appendChild(dl);
  tools.appendChild(back);
  ov.body.appendChild(tools);
  const pre = document.createElement('div');
  pre.className = 'math-note-preview rendered';
  if (typeof safeRenderMarkdown === 'function') {
    pre.innerHTML = safeRenderMarkdown(text);
  } else {
    pre.textContent = text;
  }
  ov.body.appendChild(pre);
}

function mathHistoryCard(h) {
  const item = document.createElement('div');
  item.className = 'math-history-item';
  const head = document.createElement('div');
  head.className = 'math-history-head';
  const when = document.createElement('span');
  when.className = 'math-history-when';
  when.textContent = mathTimeText(h.at);
  const rank = document.createElement('span');
  rank.className = 'math-history-rank' + (h.rank === 1 ? ' win' : '');
  rank.textContent = '第 ' + (h.rank || '—') + ' 名';
  const score = document.createElement('span');
  score.className = 'math-history-score';
  score.textContent = h.score + ' 分 · 答对 ' + h.correct + ' / ' + h.total;
  const xp = document.createElement('span');
  xp.className = 'math-history-xp';
  xp.textContent = '+' + h.xp_gain + ' XP';
  head.appendChild(when);
  head.appendChild(rank);
  head.appendChild(score);
  head.appendChild(xp);
  item.appendChild(head);

  const meta = document.createElement('div');
  meta.className = 'math-history-meta';
  const names = (h.courses || []).map((c) => MATH_COURSES[c] || c);
  meta.textContent = names.length === MATH_COURSE_ORDER.length
    ? '课程：全部' : ('课程：' + (names.join('、') || '—'));
  item.appendChild(meta);

  if (h.players && h.players.length) {
    const players = document.createElement('div');
    players.className = 'math-history-players';
    players.textContent = h.players.map((p) => '#' + p.rank + ' ' + p.name
      + (p.kind === 'ai' ? ' · AI' : '') + ' ' + p.score + ' 分').join(' ｜ ');
    item.appendChild(players);
  }

  const det = document.createElement('details');
  det.className = 'math-note-details';
  const sum = document.createElement('summary');
  sum.textContent = '逐题明细';
  det.appendChild(sum);
  (h.items || []).forEach((q, i) => {
    const row = document.createElement('div');
    row.className = 'math-history-q';
    const mark = document.createElement('span');
    mark.className = 'math-history-q-mark ' + (q.correct ? 'ok' : 'bad');
    mark.textContent = q.correct ? '✓' : '✗';
    const text = document.createElement('span');
    text.className = 'math-history-q-text';
    mathRichText(text, (i + 1) + '. ' + (q.course_name || '')
      + (q.skill ? ' · ' + q.skill : '') + '：' + (q.prompt || ''));
    row.appendChild(mark);
    row.appendChild(text);
    det.appendChild(row);
  });
  item.appendChild(det);
  return item;
}

function mathOpenHistory() {
  if (!math.store) return;
  mathClosePicker();
  const list = math.store.history || [];
  const ov = mathOpenOverlay('对局记录', list.length
    ? ('最近 ' + list.length + ' 局 · 最高分 ' + math.store.bestScore)
    : '还没有对局记录');
  if (!list.length) {
    const empty = document.createElement('div');
    empty.className = 'math-panel-note math-note-empty';
    empty.textContent = '打完一局后，这里会保存分数、名次、XP 和逐题结果。';
    ov.body.appendChild(empty);
  } else {
    list.forEach((h) => ov.body.appendChild(mathHistoryCard(h)));
  }
  const clear = document.createElement('button');
  clear.type = 'button';
  clear.className = 'math-clear';
  clear.textContent = '清空对局记录';
  clear.disabled = !list.length;
  clear.onclick = () => {
    if (!confirm('清空全部对局记录？')) return;
    math.store.history = [];
    mathSaveStore();
    mathRenderTop();
    mathOpenHistory();
  };
  const spacer = document.createElement('span');
  spacer.className = 'math-modal-spacer';
  ov.foot.appendChild(spacer);
  ov.foot.appendChild(clear);
}

function mathReviewLabel(w) {
  if (!w || w.legacy) return '旧记录';
  const box = Math.max(0, Math.min(MATH_REVIEW_DAYS.length - 1, Number(w.box) || 0));
  const now = Date.now();
  if (!w.due || w.due <= now) return box ? ('第 ' + box + ' 盒 · 待复习') : '待复习';
  const days = Math.max(1, Math.ceil((w.due - now) / MATH_DAY_MS));
  return '第 ' + box + ' 盒 · ' + days + ' 天后';
}

