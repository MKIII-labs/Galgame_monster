'use strict';

/* ============================================================
   自动跟随（对话区）
   ------------------------------------------------------------
   参考实现：
   · stackblitz-labs/use-stick-to-bottom：ResizeObserver 检测内容增删、
     用「用户是否逃离（escaped）」而不是「当前是否贴底」决定跟随、
     自己发起的滚动要记账忽略、Spring 动画；
   · opencode / Claude Code 的 transcript 语义：只有用户主动上翻才
     解除吸附，新输出本身永远不会解除。
   本项目的核心修复：
   1) 跟随意图（pinned）与位置测量解耦。内容变高导致 scrollTop 变化时，
      scroll 事件先被「自己写入的滚动」和「刚发生尺寸变化」两道闸门过滤，
      绝不会被当成「用户上翻」而关掉跟随。
   2) 高度突变（图片加载 / 代码高亮 / 工具卡展开 / 字体回退）由
      ResizeObserver 直接触发重新对齐，不再依赖下一次 delta。
   3) 距离近时按时间常数缓动追平；超过一屏直接瞬移，避免跨区域跳转时
      长时间追赶；任何用户输入立即取消动画。
   ============================================================ */

// ---- 兼容旧全局：其他脚本（stream-core / chat）直接读写 ----
let _followBottom = true;
const FOLLOW_SLOP = 40;          // 「贴着底」的容差（向上翻多少算离开）

const FOLLOW_DEFAULTS = {
  atBottomSlop: 24,      // 判定「在底部」的像素容差
  resumeSlop: 12,        // 用户向下滚到离底多少像素内，重新吸附
  snapScreens: 1.15,     // 离底超过多少屏：直接瞬移
  tau: 55,               // 缓动时间常数（ms）；越小越快
  quietMs: 72,           // 尺寸变化后忽略 scroll 事件的窗口
  maxStep: 2200,         // 单帧最大位移，防止切回标签页时一步飞出去
};

/* ------------------------------------------------------------
   通用跟随核心：只负责「位置 / 意图 / 动画」，不认任何业务选择器。
   由 bind() 负责挂 ResizeObserver / MutationObserver。
   ------------------------------------------------------------ */
function gmFollowCreate(options) {
  const opt = Object.assign({}, FOLLOW_DEFAULTS, options || {});
  const state = {
    pinned: opt.pinned !== false,
    selecting: false,
    lastTop: 0,
    lastMaxTop: 0,         // 上一次看到的最大 scrollTop：用来识别「内容变矮导致的回弹」
    expected: [],          // 最近几次程序写入后的 scrollTop（scroll 事件异步回来时比对）
    quietUntil: 0,
    raf: 0,
    lastTs: 0,
    destroyed: false,
    bound: false,
  };

  const box = () => (typeof opt.scrollEl === 'function' ? opt.scrollEl() : opt.scrollEl);
  const maxTop = (el) => Math.max(0, el.scrollHeight - el.clientHeight);

  function notify(extra) {
    if (!opt.onChange) return;
    try { opt.onChange(Object.assign({ pinned: state.pinned, state }, extra)); } catch (e) { /* 忽略 */ }
  }

  /* 程序滚动统一出口：记账 expectedTop，并把 scroll-behavior 临时改成 auto。
     等 scroll 事件异步回来时，先与记账值比对，避免「自己滚的」被判成用户操作。 */
  function writeTop(top) {
    const el = box();
    if (!el) return;
    const max = maxTop(el);
    const next = Math.max(0, Math.min(max, top));
    const style = getComputedStyle(el);
    const smooth = style.scrollBehavior && style.scrollBehavior !== 'auto';
    if (smooth) el.style.scrollBehavior = 'auto';
    el.scrollTop = next;
    if (smooth) el.style.scrollBehavior = '';
    state.expected.push(el.scrollTop);
    if (state.expected.length > 4) state.expected.shift();
    state.lastTop = el.scrollTop;
    state.lastMaxTop = max;
  }

  function stop() {
    if (state.raf) { cancelAnimationFrame(state.raf); state.raf = 0; }
    state.lastTs = 0;
  }

  function cancel() { stop(); }

  function tick(ts) {
    state.raf = 0;
    const el = box();
    if (!el || state.destroyed) return;
    if (!state.pinned || state.selecting) return;
    const max = maxTop(el);
    state.lastMaxTop = max;
    const dist = max - el.scrollTop;
    if (dist <= 0.5) {
      if (el.scrollTop > max) writeTop(max);
      state.lastTop = el.scrollTop;
      return;
    }
    const dt = state.lastTs ? Math.min(64, ts - state.lastTs) : 16.7;
    state.lastTs = ts;
    if (dist > opt.snapScreens * el.clientHeight) {
      writeTop(max);                       // 跨屏跳转：瞬移，不做长距离动画
    } else {
      const k = 1 - Math.exp(-dt / opt.tau);
      const step = Math.min(dist, Math.max(0.5, dist * k), opt.maxStep);
      writeTop(el.scrollTop + step);
    }
    state.lastTop = el.scrollTop;
    if (state.pinned && maxTop(el) - el.scrollTop > 0.5) {
      state.raf = requestAnimationFrame(tick);
    } else {
      state.lastTs = 0;
    }
  }

  function follow() {
    if (state.destroyed || !state.pinned || state.selecting) return;
    if (state.raf) return;
    state.lastTs = 0;
    state.raf = requestAnimationFrame(tick);
  }

  function setPinned(on, reason) {
    const next = !!on;
    if (state.pinned === next) {
      if (next) follow();
      return;
    }
    state.pinned = next;
    if (next) follow(); else stop();
    notify({ reason: reason || '' });
  }

  /* 用户明确上翻：立即停动画，锁定跟随，不和用户抢滚动条 */
  function unpin(reason) {
    stop();
    setPinned(false, reason || 'user');
  }

  /* 尺寸变化：先记一个静默窗口（这段时间的 scroll 事件不算用户意图），
     再把已经越过底部的 scrollTop 拉回来；跟随中则重新对齐目标。 */
  function noteResize(opts) {
    const el = box();
    if (!el) return;
    // 静默窗口只服务于「跟随中自己写 scrollTop」；未跟随时没有程序滚动，
    // 不能因为流式内容不断长高就一直刷新窗口，否则用户滚回底部的操作会被忽略。
    if (state.pinned) state.quietUntil = performance.now() + opt.quietMs;
    const max = maxTop(el);
    state.lastMaxTop = max;
    if (el.scrollTop > max) writeTop(max);
    state.lastTop = el.scrollTop;
    if (!opts || opts.follow !== false) follow();
  }

  function noteMutation() {
    // 结构 / 文本变化不设静默窗口：否则流式期间每个 token 都会刷新窗口，
    // 用户拖滚动条 / 键盘翻页产生的 scroll 事件会被一直忽略。
    // 程序滚动靠 expected 记账过滤，浏览器锚定在跟随中已关闭（#chat.following）。
    if (state.selecting) { notify({ reason: 'selection' }); return; }   // 选区中：只刷新按钮状态
    follow();
  }

  function report(top) {
    if (!opt.onScroll) return;
    try { opt.onScroll(top, { pinned: state.pinned }); } catch (e) { /* 忽略 */ }
  }

  function onScroll() {
    const el = box();
    if (!el) return;
    const top = el.scrollTop;
    // 1) 自己写入的滚动（scroll 事件异步派发，可能落后一帧）
    for (let i = 0; i < state.expected.length; i++) {
      if (Math.abs(top - state.expected[i]) <= 1.5) {
        state.expected.splice(0, i + 1);
        state.lastTop = top;
        report(top);
        return;
      }
    }
    if (state.expected.length && top < state.expected[0] - 1.5) state.expected.length = 0;
    if (state.selecting) { state.lastTop = top; report(top); return; }
    if (performance.now() < state.quietUntil) {   // 内容尺寸变化导致的滚动
      state.lastTop = top;
      report(top);
      return;
    }
    const prev = state.lastTop;
    state.lastTop = top;
    const max = maxTop(el);
    // 内容变矮（折叠 / 占位移除 / 图片重新排版）会让浏览器把 scrollTop 回弹到新的底部。
    // 这不是用户上翻：跟随时不能因此解除吸附，否则后续输出会停在半路。
    const shrank = max < state.lastMaxTop - 1;
    state.lastMaxTop = max;
    const nearBottom = max - top <= opt.resumeSlop;
    // 只有「位置仍贴着新的底部」才像浏览器夹紧；如果用户同时往上翻了很远，
    // 仍然按用户意图解除跟随。
    const clampLike = shrank && nearBottom;
    if (top < prev - 1) {
      if (state.pinned && !clampLike) unpin('scroll-up');
    } else if (top > prev + 1 && nearBottom && !state.pinned) {
      setPinned(true, 'scroll-down');
    }
    if (clampLike && state.pinned) follow();
    report(top);
  }

  // 内层还有能滚的容器（过程折叠区）：这次滚动属于它，不影响主窗口跟随。
  function innerScroller(target, root) {
    let node = target;
    while (node && node !== root) {
      if (node.nodeType === 1) {
        const oy = getComputedStyle(node).overflowY;
        if ((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight + 1) {
          return node;
        }
      }
      node = node.parentElement;
    }
    return null;
  }

  function onWheel(e) {
    if (e.deltaY >= 0 || !state.pinned) return;
    if (innerScroller(e.target, box())) return;
    unpin('wheel-up');
  }

  let touchY = 0;
  function onTouchStart(e) {
    touchY = e.touches && e.touches[0] ? e.touches[0].clientY : 0;
  }
  function onTouchMove(e) {
    const y = e.touches && e.touches[0] ? e.touches[0].clientY : touchY;
    const up = y - touchY > 4;                 // 手指下移 = 内容上翻
    touchY = y;
    if (up && state.pinned && !innerScroller(e.target, box())) unpin('touch-up');
  }

  function selectionActive() {
    const sel = window.getSelection && window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
    const el = box();
    const node = sel.anchorNode;
    return !!(el && node && el.contains(node));
  }

  function onSelectionChange() {
    const active = selectionActive();
    if (active === state.selecting) return;
    state.selecting = active;
    if (active) stop();
    else if (state.pinned) follow();
    // 选区期间位置可能离底很远：让业务侧刷新「回到底部」按钮，但不要把内层步骤拽回底部
    notify({ reason: 'selection' });
  }

  function bind() {
    if (state.bound) return;
    const el = box();
    if (!el) return;
    state.bound = true;
    state.lastTop = el.scrollTop;
    state.lastMaxTop = maxTop(el);
    el.addEventListener('scroll', onScroll, { passive: true });
    el.addEventListener('wheel', onWheel, { passive: true });
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchmove', onTouchMove, { passive: true });
    document.addEventListener('selectionchange', onSelectionChange);
  }

  function destroy() {
    state.destroyed = true;
    stop();
    const el = box();
    if (el && state.bound) {
      el.removeEventListener('scroll', onScroll);
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
    }
    document.removeEventListener('selectionchange', onSelectionChange);
  }

  return {
    get state() { return state; },
    bind,
    destroy,
    follow,
    cancel,
    pin() { setPinned(true, 'api'); },
    unpin,
    snap() {
      const el = box();
      if (!el) return;
      stop();
      setPinned(true, 'snap');
      writeTop(maxTop(el));
    },
    /* 平滑回到底部（点「回到底部」按钮时用）：
       - 按距离定 220–520ms，easeOutCubic；跨屏也不会动画太久；
       - 用户上翻 / 触摸上滑 / 选中文字会走 unpin → stop()，动画立即取消；
       - 系统开启「减少动态效果」时直接到顶，不做动画。 */
    glide(ms) {
      const el = box();
      if (!el) return;
      stop();
      setPinned(true, 'glide');
      // setPinned 可能顺带启动 55ms 的跟随缓动；先停掉，统一由下面的 glide 接管
      stop();
      const start = el.scrollTop;
      const end = maxTop(el);
      const dist = end - start;
      const reduced = !!(window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      if (dist <= 1 || reduced) {
        writeTop(end);
        return;
      }
      const dur = Math.max(220, Math.min(520, Number(ms) || (180 + dist * 0.12)));
      const t0 = performance.now();
      const step = (now) => {
        const p = Math.min(1, (now - t0) / dur);
        const k = 1 - Math.pow(1 - p, 3);        // easeOutCubic
        writeTop(start + dist * k);
        if (p < 1) {
          state.raf = requestAnimationFrame(step);
        } else {
          state.raf = 0;
          state.lastTs = 0;
          if (state.pinned) follow();           // 动画期间内容又变高：继续贴底
        }
      };
      state.raf = requestAnimationFrame(step);
    },
    toBottom(force) { if (force) { this.snap(); } else follow(); },
    isPinned() { return state.pinned; },
    isAtBottom(px) {
      const el = box();
      if (!el) return true;
      return maxTop(el) - el.scrollTop <= (px == null ? opt.atBottomSlop : px);
    },
    distance() {
      const el = box();
      return el ? maxTop(el) - el.scrollTop : 0;
    },
    maxTop() { const el = box(); return el ? maxTop(el) : 0; },
    writeTop,
    noteResize,
    noteMutation,
    suspend(ms) { state.quietUntil = performance.now() + Math.max(0, ms || 0); },
    setTop(top) { writeTop(top); },
    setPinned,
  };
}

/* ------------------------------------------------------------
   绑定 DOM：ResizeObserver（所有直接子节点）+ MutationObserver（结构 / 文本）
   两者只负责「发现变化」，是否跟随仍由核心的 pinned 状态决定。
   ------------------------------------------------------------ */
function gmFollowBind(options) {
  const opt = options || {};
  const core = gmFollowCreate(opt);
  const box = () => (typeof opt.scrollEl === 'function' ? opt.scrollEl() : opt.scrollEl);
  let ro = null;
  let mo = null;
  let isBound = false;
  const observed = new Set();

  function syncChildren() {
    const el = box();
    if (!el || !ro) return;
    // targets 由业务方提供（聊天区 = 容器 + 所有直接子节点 + 正在跑的过程折叠），
    // 一次 ResizeObserver 同时覆盖「内容高度突变」和「折叠区内部高度突变」，
    // 同一批 entries 里能看出是折叠自身在变，还是整行在变。
    const want = new Set(typeof opt.targets === 'function' ? opt.targets(el) : []);
    if (!want.size) {
      (el.children ? Array.from(el.children) : []).forEach((n) => want.add(n));
      want.add(el);
    }
    observed.forEach((node) => {
      if (!want.has(node) || !node.isConnected) { ro.unobserve(node); observed.delete(node); }
    });
    want.forEach((node) => {
      if (!observed.has(node)) { ro.observe(node); observed.add(node); }
    });
  }

  function bind() {
    if (isBound) return true;
    const el = box();
    if (!el) return false;
    isBound = true;
    core.bind();
    if (window.ResizeObserver) {
      ro = new ResizeObserver((entries) => {
        const handled = opt.onResize ? opt.onResize(entries, core) === true : false;
        if (!handled) core.noteResize();
        else core.noteResize({ follow: false });
      });
      if (opt.observeChildren !== false) syncChildren();
      else if (opt.contentEl) {
        const content = typeof opt.contentEl === 'function' ? opt.contentEl() : opt.contentEl;
        if (content) { ro.observe(content); observed.add(content); }
        ro.observe(el);
        observed.add(el);
      }
    }
    if (window.MutationObserver) {
      let syncTimer = 0;
      const scheduleSync = () => {
        if (syncTimer) return;
        syncTimer = setTimeout(() => { syncTimer = 0; syncChildren(); }, 120);
      };
      mo = new MutationObserver((records) => {
        // 逐 token 的 characterData 不重建观察列表：长会话里每 token 扫一遍子节点太贵。
        // 只有结构变化（新消息 / 新折叠）才需要重新同步 targets。
        if (records.some((r) => r.type === 'childList')) scheduleSync();
        const handled = opt.onMutation ? opt.onMutation(records, core) === true : false;
        if (handled) core.noteResize({ follow: false });
        else core.noteMutation();
      });
      mo.observe(el, { childList: true, subtree: true, characterData: true });
    }
    return true;
  }

  function destroy() {
    isBound = false;
    core.destroy();
    if (ro) { ro.disconnect(); ro = null; }
    if (mo) { mo.disconnect(); mo = null; }
    observed.clear();
  }

  const api = Object.assign({}, core, { bind, destroy, core, syncChildren });
  bind();                      // 创建即绑定，调用方不需要记得再调一次
  return api;
}

window.GmFollow = { create: gmFollowCreate, bind: gmFollowBind };

/* ============================================================
   对话区接入
   ============================================================ */
let CHAT_FOLLOW = null;
let _restoreScrollUntil = 0;

// 会话阅读位置：切走时记住每个会话滚到哪，切回来还原（只放内存）。
const _chatScrollMem = new Map();

function chatBox() { return $('#chat'); }

function processFollowEnabled() {
  return !(state.config && state.config.ui_process_follow === false);
}

function followPinned() {
  return CHAT_FOLLOW ? CHAT_FOLLOW.isPinned() : !!_followBottom;
}

let _bottomBtnRaf = 0;
/* 内容长高但没触发 scroll 事件时，按钮状态也要更新（流式中用户上翻后
   距离会被推远）。按帧合并，避免每个 token 都强制读一次 scrollHeight。 */
function scheduleBottomBtnSync() {
  if (_bottomBtnRaf) return;
  _bottomBtnRaf = requestAnimationFrame(() => {
    _bottomBtnRaf = 0;
    syncBottomBtn();
  });
}

function syncBottomBtn() {
  const btn = $('#btn-bottom');
  if (!btn) return;
  const empty = !!$('#empty');
  // 未跟随（用户上翻 / 主动暂停）时始终给出口：即使离底很近，也需要一个
  // 「重新吸附」的按钮；跟随时才按「贴底」隐藏。
  btn.classList.toggle('off', empty || (followPinned() && nearBottom()));
}

function rememberChatScroll() {
  const c = chatBox();
  const sid = state.sessions.current;
  if (!c || !sid) return;
  _chatScrollMem.set(String(sid), c.scrollTop);
}

function isAtBottom(px) {
  if (CHAT_FOLLOW) return CHAT_FOLLOW.isAtBottom(px);
  const c = chatBox();
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < (px == null ? 24 : px);
}

function nearBottom() { return isAtBottom(120); }

function setFollowBottom(on) {
  if (CHAT_FOLLOW) {
    if (on) CHAT_FOLLOW.pin(); else CHAT_FOLLOW.unpin('api');
  } else {
    _followBottom = !!on;
  }
}

function scrollToBottom(force) {
  if (!CHAT_FOLLOW) {
    const c = chatBox();
    if (c && force) c.scrollTop = c.scrollHeight;
    return;
  }
  if (force) CHAT_FOLLOW.snap(); else CHAT_FOLLOW.follow();
}

function followScroll() {
  if (CHAT_FOLLOW) CHAT_FOLLOW.follow();
}

function scrollToBottomSmooth() {
  resumeProcessFollow();                    // 点「回到底部」= 也想看到最新步骤
  if (CHAT_FOLLOW) { CHAT_FOLLOW.glide(); return; }
  // 还没初始化跟随核心：优先用浏览器原生平滑滚动，不支持就退回瞬移
  const c = chatBox();
  if (!c) return;
  const reduced = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  if (!reduced && c.scrollTo) {
    try { c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' }); return; }
    catch (e) { /* 老浏览器不支持对象参数 → 走瞬移 */ }
  }
  scrollToBottom(true);
}

/* 迟到的布局变化（5MB 字体、富媒体、收尾折叠动画）会把「已经到底」的视野留在半截。
   在仍然处于跟随状态时分几拍确认底部；用户一旦上翻 pinned=false，后续拍子自动跳过，
   不会把正在读历史的用户拽回底部。 */
function settleChatBottom(delays) {
  const marks = Array.isArray(delays) ? delays : [0, 120, 320, 800, 1600];
  marks.forEach((d) => setTimeout(() => {
    if (!CHAT_FOLLOW || !CHAT_FOLLOW.isPinned()) return;
    if (CHAT_FOLLOW.distance() > 2) CHAT_FOLLOW.snap();
  }, d));
}
window.settleChatBottom = settleChatBottom;

function restoreChatScroll(sid) {  const c = chatBox();
  if (!c) return;
  const y = _chatScrollMem.get(String(sid || ''));
  if (!CHAT_FOLLOW) {
    // 初始化早期（还没 bind）：退化为直接定位，避免停在顶部
    if (typeof y === 'number') c.scrollTop = y;
    else if ($('#empty')) c.scrollTop = 0;
    else c.scrollTop = c.scrollHeight;
    return;
  }
  if (typeof y === 'number') {
    _restoreScrollUntil = Date.now() + 320;
    CHAT_FOLLOW.suspend(320);
    CHAT_FOLLOW.setTop(y);
    const atBottom = CHAT_FOLLOW.isAtBottom(FOLLOW_SLOP);
    _followBottom = atBottom;
    CHAT_FOLLOW.setPinned(atBottom, 'restore');
  } else if ($('#empty')) {
    _followBottom = true;
    CHAT_FOLLOW.setPinned(true, 'restore');
    c.scrollTop = 0;
  } else {
    _followBottom = true;
    CHAT_FOLLOW.snap();
  }
  syncBottomBtn();
}

/* ---------------- 过程折叠区跟随 ---------------- */
// 每个折叠区的内层滚动状态：用户在内层上翻只暂停内层跟随，不影响主窗口。
const _procFollow = new WeakMap();
const _procExpected = new WeakMap();
let _procFollowRaf = 0;

function procSetScroll(body, top) {
  if (!body) return;
  body.scrollTop = top;
  // 记一串期望值：连续两步的写入可能合并成一次 scroll 事件，只记一个会误判
  const arr = _procExpected.get(body) || [];
  arr.push(body.scrollTop);
  if (arr.length > 4) arr.shift();
  _procExpected.set(body, arr);
}

function processNearBottom(body) {
  if (!body) return true;
  return body.scrollHeight - body.scrollTop - body.clientHeight < 24;
}

/* 重新贴底时把内层过程区也放回「跟随」：
   用户点「回到底部」或自己滚回底部，期望看到的是最新步骤，而不是停在旧位置。 */
function resumeProcessFollow() {
  const box = chatBox();
  if (!box) return;
  const folds = box.querySelectorAll('.process-fold:not(.collapsed)');
  if (!folds.length) return;
  const fold = folds[folds.length - 1];
  const body = fold.querySelector('.process-fold-body');
  if (!body) return;
  _procFollow.set(body, true);
  if (body.scrollHeight > body.clientHeight + 1) {
    procSetScroll(body, body.scrollHeight);      // 立刻停到最新一步
  }
}

/* 内层「用户意图」判定：只有真的在过程折叠区上滚过（滚轮 / 触摸 / 拖滚动条 / 键盘），
   之后的滚动才允许暂停跟随。折叠动画、滚动锚定、内容增删也会改 scrollTop，
   以前一律当成「用户上翻」，一暂停就再也回不来（表现为步骤不再跟随）。 */
const PROC_INPUT_MS = 600;

function markProcInput(e) {
  const t = e.target;
  const body = t && t.closest ? t.closest('.process-fold-body') : null;
  if (!body) return;
  // 按在步骤内容上（展开卡片等）不算滚动意图；只有按在滚动条上（target 就是 body）才算
  if ((e.type === 'mousedown' || e.type === 'pointerdown') && t !== body) return;
  body._procInputAt = performance.now();
  if (e.type === 'wheel' && e.deltaY < 0
      && body.scrollTop > 0                   // 已经在顶部：别再暂停，让滚动链回主窗口
      && body.scrollHeight > body.clientHeight + 1) {
    _procFollow.set(body, false);
  }
  if (e.type === 'keydown' && /^(ArrowUp|PageUp|Home)$/.test(e.key || '')) {
    _procFollow.set(body, false);
  }
}

function watchProcessScroll() {
  const box = chatBox();
  if (!box || box._procScrollBound) return;
  box._procScrollBound = true;
  box.addEventListener('scroll', (e) => {
    const body = e.target;
    if (!body || !body.classList || !body.classList.contains('process-fold-body')) return;
    const exp = _procExpected.get(body);
    if (exp && exp.length) {
      const hit = exp.findIndex((v) => Math.abs(body.scrollTop - v) <= 1.5);
      if (hit >= 0) {
        exp.splice(0, hit + 1);
        return;                                          // 自己滚的
      }
    }
    // 没有近期用户输入 → 是布局变化（折叠动画 / 锚定）带的位移，不改跟随意图
    const prevTop = body._procLastTop == null ? body.scrollTop : body._procLastTop;
    body._procLastTop = body.scrollTop;
    if (performance.now() - (body._procInputAt || -1e9) < PROC_INPUT_MS) {
      // 主动往下滚 = 想继续看最新步骤：直接恢复；往上滚才按「是否贴底」暂停
      if (body.scrollTop > prevTop + 1) _procFollow.set(body, true);
      else _procFollow.set(body, processNearBottom(body));
    }
  }, true);
  ['wheel', 'touchstart', 'touchmove', 'pointerdown', 'mousedown', 'keydown']
    .forEach((type) => {
      box.addEventListener(type, markProcInput,
                           { capture: true, passive: type !== 'keydown' });
    });
}

function isRunningFold(node) {
  return !!(node && node.nodeType === 1 && node.classList
    && node.classList.contains('process-fold') && node.classList.contains('running')
    && !node.classList.contains('collapsed'));
}

function lastProcessAnchor(fold) {
  const box = chatBox();
  if (!box) return null;
  if (!fold) {
    const folds = box.querySelectorAll('.process-fold:not(.collapsed)');
    fold = folds.length ? folds[folds.length - 1] : null;
  }
  if (!fold || fold.classList.contains('collapsed')) return null;
  const cur = fold.querySelectorAll('.tool-card.is-current');
  if (cur.length) return cur[cur.length - 1];
  const rows = fold.querySelectorAll('.chat-flow > *');
  return rows.length ? rows[rows.length - 1] : null;
}

function lastSubagentAnchor() {
  const box = chatBox();
  if (!box) return null;
  const cards = box.querySelectorAll('.subagent-card');
  const card = cards.length ? cards[cards.length - 1] : null;
  if (!card) return null;
  const rows = card.querySelectorAll('.subagent-row');
  return rows.length ? rows[rows.length - 1] : card;
}

// 主窗口只负责「折叠区底边被推出视口时补最小差值」，不强行贴底。
function ensureFoldVisible(fold, box) {
  if (!fold || !box || !CHAT_FOLLOW) return;
  const fr = fold.getBoundingClientRect();
  const br = box.getBoundingClientRect();
  const pad = 24;
  if (fr.bottom <= br.bottom - pad) return;
  CHAT_FOLLOW.writeTop(box.scrollTop + (fr.bottom - (br.bottom - pad)));
}

function followProcessStep(el) {
  if (!el) { scrollToBottom(); return; }
  if (!processFollowEnabled()) {
    scrollToBottom();
    return;
  }
  // 先停掉主窗口的贴底动画，避免和步骤跟随抢滚动条。
  if (CHAT_FOLLOW) CHAT_FOLLOW.cancel();
  if (_procFollowRaf) return;
  _procFollowRaf = requestAnimationFrame(() => {
    _procFollowRaf = 0;
    const box = chatBox();
    if (!box || !el.isConnected) return;
    const fold = el.closest('.process-fold');
    if (fold && fold.classList.contains('collapsed')) return;
    if (fold && !fold.classList.contains('running')) return;
    const body = fold ? fold.querySelector('.process-fold-body') : null;
    if (body) {
      // 内层跟随独立于主窗口：用户只是把外层往上翻了一下，不等于
      // 不想再看最新步骤；内层自己的上翻暂停由 _procFollow 控制。
      if (_procFollow.get(body) !== false) {
        const br = body.getBoundingClientRect();
        const er = el.getBoundingClientRect();
        const pad = 16;
        let delta = 0;
        if (er.bottom > br.bottom - pad) delta = er.bottom - (br.bottom - pad);
        else if (er.top < br.top + pad) delta = er.top - (br.top + pad);
        if (delta) procSetScroll(body, body.scrollTop + delta);
      }
      if (_followBottom) {
        ensureFoldVisible(fold, box);
        // 步骤跟随只保证折叠底边可见；跟随时仍要把主窗口收到底（否则会停在
        // 离底几像素的位置，下次内容变化前都不会再动）。
        CHAT_FOLLOW.follow();
      }
      return;
    }
    if (!_followBottom) return;
    const br = box.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    const pad = 24;
    if (er.bottom <= br.bottom - pad) return;
    CHAT_FOLLOW.writeTop(box.scrollTop + (er.bottom - (br.bottom - pad)));
    if (_followBottom) CHAT_FOLLOW.follow();
  });
}

// ResizeObserver 统一挂在 bindChatFollow 里（行 + 正在跑的折叠一起观察）；
// 这里只负责在折叠刚插入时立刻补一次观察，避免第一帧的高度变化漏掉。
function observeProcessFold() {
  if (CHAT_FOLLOW) CHAT_FOLLOW.syncChildren();
}

/* ---------------- 聊天区绑定 ---------------- */
function chatFollowTargets(box) {
  const out = [box];
  if (box.children) Array.from(box.children).forEach((n) => out.push(n));
  // 所有过程折叠都观察：折叠可能先插入、随后才被加上 running 类
  box.querySelectorAll('.process-fold').forEach((n) => out.push(n));
  return out;
}

function bindChatFollow() {
  const box = chatBox();
  if (!box || CHAT_FOLLOW) return;
  CHAT_FOLLOW = gmFollowBind({    scrollEl: box,
    pinned: _followBottom,
    observeChildren: true,
    targets: chatFollowTargets,
    onChange({ pinned, reason }) {
      _followBottom = pinned;
      if (pinned && reason !== 'selection') resumeProcessFollow();   // 重新贴底：内层步骤也回到跟随
      box.classList.toggle('following', pinned);
      syncBottomBtn();
    },
    onScroll() {
      if (Date.now() < _restoreScrollUntil) return;
      rememberChatScroll();
      syncBottomBtn();
    },
    // 尺寸变化：同一批 entries 里有正在跑的过程折叠，就交给步骤跟随；
    // 否则（答案区图片 / 代码高亮 / 窗口缩放）由主窗口贴底。
    onResize(entries) {
      syncBottomBtn();                 // 内容长高可能把「离底距离」推远，按钮要出现
      if (!processFollowEnabled()) return false;
      let fold = null;
      for (const e of entries) {
        if (isRunningFold(e.target)) { fold = e.target; break; }
      }
      if (!fold || fold._foldAnim) return false;
      const anchor = lastProcessAnchor(fold);
      if (!anchor) return false;
      // 内层跟随不依赖主窗口是否贴底：外层被上翻时，最新步骤仍然要带进视野。
      followProcessStep(anchor);
      return true;
    },
    // 结构 / 文本变化：过程折叠、探索小队走锚点跟随，其余贴底
    onMutation(records) {
      if (!_followBottom) scheduleBottomBtnSync();
      let procFold = null;
      let sub = false;
      for (const r of records) {
        const added = Array.from(r.addedNodes || []);
        const host = r.target && r.target.nodeType === 1 ? r.target : r.target.parentElement;
        let fold = host && host.closest ? host.closest('.process-fold') : null;
        if (!fold) {
          for (const n of added) {
            if (n.nodeType === 1 && n.classList && n.classList.contains('process-fold')) {
              fold = n;
              break;
            }
          }
        }
        if (fold) {
          if (processFollowEnabled() && fold.classList.contains('running')) procFold = fold;
        } else if ((host && host.closest && host.closest('.subagent-card'))
                   || added.some((n) => n.nodeType === 1 && n.classList
                     && n.classList.contains('subagent-card'))) {
          sub = true;
        }
      }
      if (procFold || sub) {
        const anchor = procFold ? lastProcessAnchor(procFold) : lastSubagentAnchor();
        if (anchor) followProcessStep(anchor);
        return true;
      }
      return !_followBottom;
    },
  });
  CHAT_FOLLOW.bind();
  watchProcessScroll();
  const btn = $('#btn-bottom');
  if (btn) btn.onclick = () => scrollToBottomSmooth();
  if ($('#empty')) {
    box.scrollTop = 0;                       // 新会话首屏从顶部开始，别把标题滚出去
  } else if (_followBottom) {
    CHAT_FOLLOW.snap();
  }
  box.classList.toggle('following', _followBottom);
  syncBottomBtn();
  // 5MB 的 Semibold 字体迟到会让整段文字重排：就绪后（非首屏）再确认一次底部
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      if (!$('#empty')) settleChatBottom([0, 160, 500, 1200]);
    }).catch(() => {});
  }
}
