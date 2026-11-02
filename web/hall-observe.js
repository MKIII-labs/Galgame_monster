/* ============================================================
   学习观测 —— 功能大厅入口
   ------------------------------------------------------------
   数据来自 GET /api/observe（app/observe.py 的 summary）；
   渲染逻辑仍在 web/observe.js（loadObserve / renderObserve），
   这里只负责在大厅里搭容器与时间范围切换。
   ============================================================ */
'use strict';

function observeHallMount(root) {
  root.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'observe-hall';

  const hero = document.createElement('div');
  hero.className = 'gal-hall-hero';
  const title = document.createElement('div');
  title.className = 'gal-hall-title';
  title.textContent = '学习观测';
  const desc = document.createElement('p');
  desc.className = 'hint';
  desc.textContent = '对话、数学竞技场、模拟卷、研究道场留下的学习行为都记在这里；'
    + '点某一项可只看该学科。';
  hero.appendChild(title);
  hero.appendChild(desc);

  const tabs = document.createElement('div');
  tabs.className = 'modal-tabs observe-tabs';
  [['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天']].forEach(([days, label]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tab';
    b.dataset.observeDays = days;
    b.textContent = label;
    b.onclick = () => {
      observeSubject = '';
      observeTopic = '';
      syncTabs();
      observeSetDays(Number(days)).catch((e) => toastErr('读取学习观测', e));
    };
    tabs.appendChild(b);
  });

  function syncTabs() {
    tabs.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', Number(t.dataset.observeDays) === Number(observeDays));
    });
  }

  const bar = document.createElement('div');
  bar.className = 'observe-filterbar';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = '按学科筛选时点分组卡片。';
  const clear = document.createElement('button');
  clear.id = 'observe-clear-filter';
  clear.className = 'ctx-btn hidden';
  clear.type = 'button';
  clear.textContent = '查看全部';
  clear.onclick = () => {
    observeSubject = '';
    observeTopic = '';
    loadObserve();
  };
  bar.appendChild(hint);
  bar.appendChild(clear);

  const cards = document.createElement('div');
  cards.id = 'observe-cards';
  cards.className = 'usage-cards';

  const groups = document.createElement('div');
  groups.id = 'observe-groups';
  groups.className = 'observe-groups';

  const itemsHead = document.createElement('h3');
  itemsHead.className = 'observe-h3';
  itemsHead.textContent = '明细';

  const items = document.createElement('div');
  items.id = 'observe-items';
  items.className = 'observe-items';

  wrap.appendChild(hero);
  wrap.appendChild(tabs);
  wrap.appendChild(bar);
  wrap.appendChild(cards);
  wrap.appendChild(groups);
  wrap.appendChild(itemsHead);
  wrap.appendChild(items);
  root.appendChild(wrap);

  syncTabs();
  observeSetDays(Number(observeDays) || 7).catch((e) => toastErr('读取学习观测', e));
}

hallRegister({
  id: 'observe',
  name: '学习观测',
  icon: 'i-check-square',
  tag: '数据',
  desc: '回顾对话 / 竞技场 / 模拟卷里留下的学习行为：正确率、主题分布与逐条明细。',
  meta: '近 7 / 30 / 90 天 · 按学科筛选',
  mount: observeHallMount,
});
