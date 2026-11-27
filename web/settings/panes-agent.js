'use strict';

/* 设置弹窗 · panes-agent.js
   Agent 执行页与执行上限
   原 web/settings.js 拆分而来：按 index.html 中的顺序加载，共享全局作用域。 */

/* ---- 把一个 pane 里的若干分区（h4.set-group 起，到下一个 h4 前）搬到另一个 pane ---- */
function _moveSections(from, to, titles) {
  Array.from(from.children).forEach((node) => {
    if (!node.matches || !node.matches('h4.set-group')) return;
    if (titles.indexOf(String(node.textContent || '').trim()) < 0) return;
    let cur = node;
    while (cur) {
      const next = cur.nextElementSibling;
      to.appendChild(cur);
      if (!next || (next.matches && next.matches('h4.set-group'))) break;
      cur = next;
    }
  });
}

/* 多个 pane 合起来当成一个查询根：拆页后仍沿用原来「按 id 找控件」的写法 */
function _paneRoot(panes) {
  return {
    querySelector: (sel) => {
      for (let i = 0; i < panes.length; i++) {
        const hit = panes[i].querySelector(sel);
        if (hit) return hit;
      }
      return null;
    },
    querySelectorAll: (sel) => panes.reduce(
      (acc, p) => acc.concat(Array.from(p.querySelectorAll(sel))), []),
    addEventListener: (type, fn) => panes.forEach((p) => p.addEventListener(type, fn)),
  };
}

/* ---- Agent 执行上限：默认值与 app/agent.py 的常量一致，这里只放最常调的 7 项 ---- */
function buildAgentPane() {
  const cfg = settingsDraft();
  const intVal = (v, d) => {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : d;
  };
  const acRaw = intVal(cfg.agent_auto_continue, -1);
  const acNow = acRaw === 0 ? 0 : -1;   // 只保留「关闭 / 无上限」两档
  const gitModeNow = ['off', 'ask', 'auto'].indexOf(String(cfg.git_commit_mode || '')) >= 0
    ? String(cfg.git_commit_mode) : 'ask';
  // max 省略（传 0）= 不写 max 属性：Agent 那几项上限由用户自己取舍，表单不再定天花板
  const numInput = (id, val, min, max) =>
    '<input id="' + id + '" class="field-input" type="number" min="' + min + '"' +
    (max ? ' max="' + max + '"' : '') + ' step="1" value="' + val + '">';
  // 0 = 不限：给个一键填入，省得记这个约定
  const limitRow = (id, val) =>
    '<div class="limit-row">' + numInput(id, val, 0, 0) +
    '<button class="limit-unli" type="button" data-unli="' + id +
    '" title="填 0 = 不限：不设上限，自己取舍">不限</button></div>';
  const acBtn = (v, icon, label, desc) =>
    '<button class="approve-opt continue-opt' + (acNow === v ? ' active' : '') +
    '" type="button" data-continue="' + v + '">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-' + icon + '"/></svg></span>' +
    '<span class="approve-opt-text"><b>' + label + '</b><i>' + desc + '</i></span>' +
    '<span class="approve-opt-check"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '</button>';
  const gitModeBtn = (v, icon, label, desc) =>
    '<button class="approve-opt gitmode-opt' + (gitModeNow === v ? ' active' : '') +
    '" type="button" data-gitmode="' + v + '">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-' + icon + '"/></svg></span>' +
    '<span class="approve-opt-text"><b>' + label + '</b><i>' + desc + '</i></span>' +
    '<span class="approve-opt-check"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '</button>';

  const pane = mkPane('agent');
  pane.innerHTML =
    hintHtml('这些是 Agent 自主执行时的「安全阀」：调大能让它一次做完更多事，但更费 token，' +
             '也更容易一口气改出大范围改动。下面这几项都能填 0 = 不限，上限由你自己取舍。',
             'agent 安全阀 上限 默认 不限 0') +
    setGroupHtml('执行节奏', '执行 节奏 轮次 循环 自动继续') +
    labelHtml('单轮最多调用轮次', '轮次 循环 上限 工具调用 不限') +
    limitRow('agent-max-rounds', intVal(cfg.agent_max_rounds, 0)) +
    hintHtml('一次提问里最多循环多少次「调工具 → 再问模型」（默认 0 = 不限）。' +
             '填 0 = 不限：它会一直做到自己收尾、你点停止，或撞上每日 token 预算。', '轮次 循环 上限 不限') +
    labelHtml('重复调用拦截', '重复 原地打转 拦截 不限') +
    limitRow('agent-repeat-limit', intVal(cfg.agent_repeat_limit, 5)) +
    hintHtml('同一工具、同一组参数连续调用到第几次就拦掉（默认 5），避免它原地打转。' +
             '中间换过一次别的调用就重新计数；填 0 = 不拦截。', '重复 原地打转 拦截 不限') +
    checkRowHtml('agent-acceptance-review',
      '完成后运行验收子代理', '验收 复核 review 子代理 收尾 finish') +
    hintHtml('开启：finish 前另起一个独立 AI 复核交付，不通过就回灌问题让它返工（默认）。'
             + '关闭：和普通聊天一样直接收尾，不做子代理验收，也不产生验收卡片与申诉裁决。',
             '验收 复核 review 子代理 收尾 finish 普通聊天') +
    '<div class="agent-approval-cap" data-search="自动续跑 继续 继续输出">' +
    '自动续跑（任务没做完时自己继续）</div>' +
    '<div class="agent-approval" data-cfg="agent_auto_continue">' +
    acBtn(0, 'alert', '关闭', '每轮结束就停，等你说「继续」') +
    acBtn(-1, 'refresh', '无上限', '一直推到做完') +
    '</div>' +
    hintHtml('模型想收尾、但计划里还有没做完的步骤（或结尾在问「要不要继续」）时，' +
             '由程序自己推它往下做。工具条里的同名选项与这里是同一份配置。',
             '自动续跑 继续 计划') +
    setGroupHtml('提问卡', '提问 策略 自主 决策 提问卡 主动提问') +
    labelHtml('遇到提问卡 / 需要拍板时', '提问 策略 自主 决策 提问卡') +
    '<select id="autonomy-question-policy" class="field-input">' +
    '<option value="ask">ask（一律问我）</option>' +
    '<option value="smart">smart（可逆自决，不可逆 / 缺外部信息问我）</option>' +
    '<option value="auto">auto（一律自决，不阻塞）</option>' +
    '</select>' +
    hintHtml('smart 只把不可逆 / 缺信息的决定摆给你，auto 能自决就自决。'
             + 'auto 也不会执行删除 / 覆盖 / 发布 / 付款 / 改凭据这类不可逆操作，'
             + '而是选可回滚的最小方案。',
             '提问 策略 自主 决策 不可逆 普通聊天') +
    setGroupHtml('通知', '通知 提示音 音量 横幅 完成 等待回答 审批 操作 出错') +
    checkRowHtml('notify-banner', '显示系统通知横幅', '通知 横幅 系统 弹窗') +
    checkRowHtml('notify-sound', '播放提示音', '通知 提示音 声音') +
    checkRowHtml('notify-done', '任务完成时提醒', '通知 完成 任务') +
    checkRowHtml('notify-ask', 'AI 等你回答 / 批准 / 动手操作时提醒',
                 '通知 等待回答 提问 待回答 审批 批准 操作') +
    checkRowHtml('notify-error', '任务失败 / 出错时提醒', '通知 失败 出错 异常 中断') +
    labelHtml('提示音音量', '通知 音量 提示音') +
    '<input id="notify-volume" class="field-input" type="range" min="0" max="100" step="5" value="60">' +
    hintHtml('横幅与声音分开控制：关掉横幅仍可只留提示音。提示音由本机合成，不需要音频文件。',
             '通知 横幅 声音 音量') +
    setGroupHtml('工具与写入', '工具 写入 shell 命令 沙箱 写前读 大厅 提示词') +
    labelHtml('Shell 自由度', 'Shell 命令 管道 重定向 白名单') +
    '<select id="agent-shell-mode" class="field-input">' +
    '<option value="strict">strict（禁管道 / 重定向）</option>' +
    '<option value="standard">standard（允许管道与沙箱内重定向）</option>' +
    '<option value="trusted">trusted（允许 ; && || 串联，仅手动会话）</option>' +
    '</select>' +
    hintHtml('默认 standard：对齐 dsh 的完整 bash，但仍逐段过命令白名单；'
             + 'strict 更保守，trusted 只建议手动会话使用。',
             'Shell 自由度 管道 重定向 白名单') +
    labelHtml('Shell 只读命令审批', 'Shell 审批 只读 白名单 免审批 自主度') +
    '<select id="agent-shell-autonomy" class="field-input">' +
    '<option value="whitelist">只读免审批（推荐）</option>' +
    '<option value="ask">每次执行都问</option>' +
    '<option value="auto">全部直接执行</option>' +
    '</select>' +
    labelHtml('只读白名单（每行一条，留空用内置）', 'Shell 白名单 只读 命令') +
    '<textarea id="agent-shell-allow" class="field-input" rows="3" spellcheck="false" ' +
    'placeholder="Get-ChildItem*&#10;git status*&#10;rg *"></textarea>' +
    labelHtml('禁止清单（优先于白名单）', 'Shell 禁止 黑名单 命令') +
    '<textarea id="agent-shell-deny" class="field-input" rows="2" spellcheck="false" ' +
    'placeholder="Get-Content *.env&#10;cat *secret*"></textarea>' +
    hintHtml('取输出 / 状态 / 关闭（read、status、close、interrupt）永远不弹审批；'
             + '执行命令只有每一段都命中只读白名单才放行。含重定向 <code>&gt;</code>、'
             + '子表达式 <code>$(...)</code>、脚本块 <code>{...}</code>、命令串联的写法一律回到审批。'
             + '白名单留空时使用内置查看类命令（Get-ChildItem / Get-Content / Select-String / git status 等）。',
             'Shell 只读 白名单 审批 重定向 子表达式') +
    labelHtml('网络域名白名单（每行一条，留空 = 不限制）',
              '网络 域名 白名单 fetch_url download 下载 联网') +
    '<textarea id="agent-net-allow" class="field-input" rows="3" spellcheck="false" ' +
    'placeholder="github.com&#10;raw.githubusercontent.com"></textarea>' +
    hintHtml('只影响 <code>fetch_url</code> 与 <code>download_file</code> 两个联网工具；'
             + '写 <code>github.com</code> 会同时放行 <code>api.github.com</code> 等子域名，'
             + '<code>raw.githubusercontent.com</code> 是另一个域，需要单独写。'
             + '留空时不做限制（默认）。',
             '网络 域名 白名单 联网 下载') +
    '<div class="agent-approval-cap" data-search="code mode 脚本 编排 run_script 写入 命令">' +
    'Code Mode（脚本批量编排）</div>' +
    checkRowHtml('code-mode-write',
      '允许脚本直接写文件（仍限沙箱内，每次写入登记可撤销快照）',
      'code mode 脚本 run_script 写入 编排') +
    checkRowHtml('code-mode-cmd',
      '允许脚本执行命令（仍过进程白名单与超时限制）',
      'code mode 脚本 run_script 命令 执行 编排') +
    hintHtml('<code>run_script</code> 让模型写一段 Python，在一次调用里循环 / 并发跑几十个工具，'
             + '只有脚本输出回到对话，长任务能省下大量上下文。'
             + '默认只读沙箱内文件（内置 <code>open()</code>、<code>os.remove</code>、'
             + '<code>subprocess</code> 都会被护栏拒绝）；上面两项打开后，'
             + '脚本才能写文件 / 执行命令，写入仍限沙箱内。脚本内部无法逐次弹审批，'
             + '所以这是全局开关 —— 只在可信任务里打开。',
             'code mode 脚本 写入 命令 沙箱 护栏 审批') +
    checkRowHtml('agent-hall-prompt',
      '注入大厅功能说明（模拟股市）', '大厅 股票 提示词 噪声') +
    hintHtml('关闭后系统提示不再介绍模拟股市与大厅界面数据，减少纯代码任务的噪声；'
             + '日历 / 日程始终注入，不受影响。',
             '大厅 股票 提示词 噪声 日历') +
    setGroupHtml('只读并行与预取', '并行 并发 读取 预取 速度') +
    labelHtml('只读工具并发数', '并发 并行 读取 预取 速度') +
    limitRow('agent-prefetch-workers', intVal(cfg.agent_prefetch_workers, 10)) +
    hintHtml('一轮里的多个只读调用会同时跑（默认 10 个并行，最多 32 个）。' +
             '填 0 = 关闭并发、全部串行。写文件与命令始终按顺序执行。',
             '并发 并行 读取 预取 速度') +
    checkRowHtml('agent-tool-parallel',
      '执行阶段只读并行（屏障之后的读也并行）', '并行 执行 阶段 只读 屏障') +
    hintHtml('与预取不同：写 / 命令 / 交互之后的独立只读调用也会并发。'
             + '例如「跑测试 → 读 B/C/D」里的三次读会一起执行；写 / 命令本身仍串行。',
             '并行 执行 阶段 只读 屏障 命令后') +
    labelHtml('执行阶段并发数', '并行 执行 阶段 并发') +
    limitRow('agent-tool-parallel-max', intVal(cfg.agent_tool_parallel_max, 10)) +
    checkRowHtml('agent-require-read',
      '覆盖已有文件前必须先读', '写前读 版本 冲突 覆盖 安全') +
    hintHtml('开启后 write_file / edit_file 覆盖本会话还没读过的文件会被拒绝，'
             + '并提示先 read_file；读后文件又被外部改动会要求重读（防止拿过期内容覆盖）。'
             + '新建文件不受影响。',
             '写前读 版本 冲突 覆盖 安全 重读') +
    labelHtml('单轮最多预取几个只读调用', '预取 上限 并发 批量') +
    limitRow('agent-prefetch-max', intVal(cfg.agent_prefetch_max, 24)) +
    hintHtml('防止模型一次发几十个读把机器打满；填 0 = 关闭预取。',
             '预取 上限 并发 关闭') +
    setGroupHtml('工具结果预算', '工具结果 裁剪 折叠 上下文 预算') +
    labelHtml('单条结果回灌上限（0 = 不截断）', '工具结果 上限 截断') +
    limitRow('tool-result-max', intVal(cfg.tool_result_max, 30000)) +
    labelHtml('回灌头部字数', '头部 开头 截断') +
    limitRow('tool-result-head', intVal(cfg.tool_result_head, 4096)) +
    labelHtml('回灌尾部字数', '尾部 结尾 错误 结论') +
    limitRow('tool-result-tail', intVal(cfg.tool_result_tail, 1024)) +
    hintHtml('错误 / 测试失败 / 命令结论通常在末尾，所以头尾都保留；'
             + '中间省略的部分可用 read_tool_result 精读。', '尾部 错误 结论 精读') +
    labelHtml('旧结果保留原文条数', '旧结果 折叠 保留') +
    limitRow('tool-result-keep-recent', intVal(cfg.tool_result_keep_recent, 2)) +
    labelHtml('旧结果折叠上限（字）', '旧结果 折叠 摘要 字数') +
    limitRow('tool-result-fold-chars', intVal(cfg.tool_result_fold_chars, 240)) +
    hintHtml('清理单位是<b>单个工具结果</b>：超出预算时把较早的结果折成「工具名 + 目标 + 首尾 + 关键行」，'
             + '最近若干条保留原文；被折叠结果的全文仍可用 read_tool_result 按区间取回。',
             '旧结果 折叠 摘要 关键行 粒度') +
    setGroupHtml('写入审批 · 自动判定', '自动审批 ai 风险 高 低 放行 审批') +
    hintHtml('按动作风险判定，不按执行次数：新建文件、建目录、普通编辑、只读命令、' +
             '跑测试与脚本都直接放行；删除 / 移动 / 语义重命名、覆盖已有文件、' +
             '安装依赖 / 联网 / 破坏性 Git（push、reset、clean）才转人工确认。' +
             '需要更细的规则可改用「自定义」档写 .gm/agent-rules.json。',
             '自动审批 风险 删除 覆盖 安装 转人工') +
    setGroupHtml('写入审批 · 全局规则', '全局 规则 预设 权限 agent-rules 标准 谨慎 只读') +
    '<div class="agent-approval">' +
    '<button class="approve-opt agent-preset" type="button" data-preset="standard">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-check"/></svg></span>' +
    '<span class="approve-opt-text"><b>标准开发</b><i>测试 / 构建 / 本地提交放行</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="cautious">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-question"/></svg></span>' +
    '<span class="approve-opt-text"><b>谨慎</b><i>写入与命令都问</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="readonly">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-alert"/></svg></span>' +
    '<span class="approve-opt-text"><b>只读审查</b><i>禁止写入与命令</i></span></button>' +
    '<button class="approve-opt agent-preset" type="button" data-preset="clear">' +
    '<span class="approve-opt-ico"><svg class="ic"><use href="#i-trash"/></svg></span>' +
    '<span class="approve-opt-text"><b>清空</b><i>删除全局规则</i></span></button>' +
    '</div>' +
    '<div class="hint" id="agent-preset-state" style="margin:6px 0 10px;color:var(--text-dim)"></div>' +
    hintHtml('预设写入 <code>data/agent-rules.json</code>，对所有项目生效；' +
             '项目里的 <code>.gm/agent-rules.json</code> 可以追加或收紧。' +
             '应用前会自动备份旧文件；只影响「自定义」档与「自动审批」的规则判定，' +
             '不会改动命令白名单与秘密文件护栏。', '全局 规则 预设 权限 agent-rules') +
    setGroupHtml('命令执行与审计', '命令 超时 run_command 秒 不限 外联 网络 审计 连接') +
    labelHtml('命令默认超时（秒）', '命令 超时 秒 不限') +
    limitRow('agent-cmd-timeout', intVal(cfg.agent_cmd_timeout, 60)) +
    hintHtml('模型没自己指定超时就用这个值（默认 60 秒）。' +
             '装依赖、跑测试这类慢命令可以调大；填 0 = 不限，直到命令自己结束或你点停止——' +
             '卡死的命令也会一直占住这一轮。', '命令 超时 秒 不限') +
    checkRowHtml('agent-net-audit',
      '记录命令访问的外部地址（只记录，不拦截）', '外联 网络 审计 连接') +
    hintHtml('命令跑完后，结果末尾会列出它和子进程连过的外部地址与端口。' +
             '只记录、不拦截，所以 pip / npm 安装不受影响；关掉后不再采集。',
             '外联 网络 审计 连接 上传') +
    setGroupHtml('并行探索（子代理）', '并行 探索 子代理 explore 侦察 交叉验证 证据 验收') +
    checkRowHtml('agent-explore-enabled',
      '允许主代理派出子代理并行调查', '并行 探索 子代理 explore') +
    hintHtml('入口不明、跨多个模块、需要交叉验证时，主代理可以一次派出 1–5 个侦察兵：' +
             '各自独立上下文，只回传 600 字结论与「文件:行号」，原文不进主对话。',
             '并行 探索 子代理 交叉验证') +
    checkRowHtml('agent-explore-verify',
      '重新核对结论里的「文件:行号」', '证据 核验 引用 行号 校验') +
    hintHtml('引用不存在会让子代理带着错误重试一次；仍对不上就判失败，避免把编造的引用当事实。',
             '证据 核验 引用 重试 失败') +
    checkRowHtml('agent-explore-fork',
      '分叉父会话上下文（真 fork）', 'fork 分叉 上下文 继承 摘要') +
    hintHtml('开启后子代理能看到父会话已完成的真实对话（按预算裁剪），不必从头猜背景；' +
             '关掉退回 2000 字摘要模式，更省 token。', 'fork 分叉 上下文 摘要 关闭') +
    labelHtml('fork 前缀预算（token）', 'fork 前缀 预算 token 上下文') +
    limitRow('agent-explore-fork-tokens', intVal(cfg.agent_explore_fork_tokens, 12000)) +
    hintHtml('填 0 = 不限；实际会与下面的「整批 token 预算」联动，取较小值。',
             'fork 预算 联动 不限') +
    checkRowHtml('agent-explore-write',
      '允许子代理在沙箱内写文件（仍走审批）', '子代理 写入 审批 修改 文件') +
    hintHtml('子代理需要模型显式要求写入才会动手；每次写都会弹审批，' +
             '同一批写入串行执行，避免并发改坏同一棵树。', '子代理 写入 审批 串行') +
    labelHtml('一批最多写几次', '子代理 写入 次数 上限') +
    limitRow('agent-explore-write-max', intVal(cfg.agent_explore_write_max, 16)) +
    hintHtml('防止审批轰炸；填 0 = 不限。', '子代理 写入 上限 不限') +
    labelHtml('一次最多几个子代理（默认 8，硬上限 12）', '并行 子代理 数量 上限') +
    limitRow('agent-explore-max', intVal(cfg.agent_explore_max, 8)) +
    labelHtml('整批 token 预算', '探索 token 预算 上限') +
    limitRow('agent-explore-budget', intVal(cfg.agent_explore_budget, 160000)) +
    hintHtml('预算用完会带着已有结论提前返回；填 0 = 不限。' +
             '子代理默认用主聊天模型，可在 config.json 里用 agent_explore_model 单独指定。',
             '探索 token 预算 模型') +
    labelHtml('子会话保留秒数（追问用）', '子会话 追问 followup 保留 秒 过期') +
    limitRow('agent-explore-ttl', intVal(cfg.agent_explore_ttl, 600)) +
    hintHtml('探索结束后，这批子代理的会话（含已读片段）会保留这么久，' +
             '期间可以用 explore_followup 追问细节，不必重新探索；填 0 = 关闭追问。',
             '子会话 追问 followup 保留 过期 0') +
    setGroupHtml('子代理 · 嵌套与后台', '嵌套 深度 后台 background 并发 取消') +
    labelHtml('子代理嵌套深度（0–5，0 = 关闭）', '嵌套 深度 子代理') +
    limitRow('agent-explore-max-depth', intVal(cfg.agent_explore_max_depth, 5)) +
    hintHtml('根层 depth=0，子代理最多再往下这么多层；嵌套层默认只读。',
             '嵌套 深度 只读') +
    labelHtml('一棵子代理树的总数上限（0 = 不限）', '子代理 总数 上限') +
    limitRow('agent-explore-max-total', intVal(cfg.agent_explore_max_total, 100)) +
    checkRowHtml('agent-explore-nested-write',
      '允许嵌套层写文件（仍走审批）', '嵌套 写入 审批') +
    hintHtml('默认关闭：嵌套层只做检索与阅读，写入留给根子代理，避免审批链随递归变复杂。',
             '嵌套 写入 关闭 审批') +
    checkRowHtml('agent-explore-background',
      '允许子代理后台运行', '后台 background 不阻塞') +
    hintHtml('后台批次启动后立即返回批次号，主代理继续做别的；' +
             '用 explore_status / explore_result / explore_cancel 取用。后台批次只读。',
             '后台 批次 不阻塞 状态 取消') +
    labelHtml('后台并发上限', '后台 并发 上限') +
    limitRow('agent-explore-background-max', intVal(cfg.agent_explore_background_max, 12)) +
    labelHtml('后台结果保留秒数', '后台 结果 保留 秒') +
    limitRow('agent-explore-background-ttl', intVal(cfg.agent_explore_background_ttl, 86400)) +
    setGroupHtml('研究闸门（复刻 / 还原类任务）', '研究 检索 缺口 台账 还原 复刻 复现') +
    checkRowHtml('agent-research-gate',
      '动笔前先要求检索（复刻 / 还原类任务）', '研究 闸门 检索 还原 复刻') +
    hintHtml('任务像「还原 / 复刻某个已有系统」时，写第一个文件前必须先用 web_search 调研；' +
             '不满足会拦一次并说明先查什么，连拦两次后放行（避免没配搜索时卡死）。',
             '研究 闸门 检索 拦截 还原 复刻') +
    labelHtml('动笔前最少检索次数', '研究 检索 次数 下限') +
    limitRow('agent-research-min-searches', intVal(cfg.agent_research_min_searches, 2)) +
    hintHtml('填 0 = 不设下限。检索结果会记进研究台账（跨轮保留、编号全局连续），' +
             '模型可以用 research_note 登记缺口与结论。', '研究 检索 次数 台账 缺口 关闭') +
    setGroupHtml('工作流（多阶段编排）', '工作流 workflow 多阶段 编排 DAG 断点 恢复') +
    checkRowHtml('workflow-enabled', '允许主代理执行多阶段工作流',
                 '工作流 workflow 多阶段 编排 DAG') +
    hintHtml('工作流把「探索 → 改文件 → 跑测试 → 校验」写成可执行节点，每步落盘，' +
             '中断后可续跑；写文件 / 命令仍然按审批档位询问。',
             '工作流 断点 续跑 审批 编排') +
    labelHtml('最多几个节点', '工作流 节点 上限') +
    limitRow('workflow-max-steps', intVal(cfg.workflow_max_steps, 48)) +
    labelHtml('最多并发几个只读节点', '工作流 并发 节点 并行') +
    limitRow('workflow-max-parallel', intVal(cfg.workflow_max_parallel, 10)) +
    labelHtml('保留多少个历史工作流', '工作流 保留 清理 历史') +
    limitRow('workflow-keep', intVal(cfg.workflow_keep, 200)) +
    hintHtml('并发只对只读节点生效；写 / 命令 / 脚本节点始终串行。填 0 = 不限。',
             '工作流 并发 串行 清理 不限') +
    setGroupHtml('变更审阅（撤销快照）', '变更 审阅 撤销 快照 保留 条数 回滚 diff 不限') +
    labelHtml('每个会话保留多少条撤销快照', '撤销 快照 保留 条数 回滚 diff 不限') +
    limitRow('agent-max-undo', intVal(cfg.agent_max_undo, 60)) +
    hintHtml('「查看变更 / 撤销」都靠这些快照。每个会话保留最近多少条<b>往期</b>写入（默认 60）。' +
             '本轮写入的快照始终保留——同一轮里写过几十个文件时，前几个的 diff 也不会打不开。' +
             '填 0 = 不限：一直留到进程退出（更占内存）。', '撤销 快照 保留 条数 diff 内存 不限') +
    setGroupHtml('电脑操作 · 浏览器（Computer Use）', 'computer 浏览器 操作 网页 白名单 站点 点击') +
    checkRowHtml('computer-enabled',
      '允许 AI 操作受控浏览器（会另开一个独立 Edge 窗口）', '浏览器 操作 开启 computer') +
    hintHtml('开启后模型多出 5 个浏览器工具：打开网页 / 看页面元素 / 点击输入 / 读 console / 截图。' +
             '这是**独立窗口**，看不到你主浏览器里已登录的站点；需要登录态时在这个窗口里登录一次，' +
             'cookie 会留在它自己的配置目录里。银行 / 支付 / 邮箱 / 密码管理器内置禁止，无法解除。',
             '浏览器 独立窗口 登录 禁止') +
    labelHtml('新站点授权方式', '授权 默认 询问 自动 批准 始终允许 站点') +
    '<select id="computer-site-mode" class="field-input">' +
    '<option value="ask">每次询问（默认）</option>' +
    '<option value="auto">自动批准，只拦可疑地址</option>' +
    '<option value="allow">始终允许（不建议）</option>' +
    '</select>' +
    hintHtml('自动批准只放行 https 公网域名；http、IP 地址、非常规端口、punycode 域名仍会先问你。' +
             '银行 / 支付 / 邮箱 / 密码管理器、设置里的禁止站点与本程序界面<b>永远拒绝</b>；' +
             '提交 / 支付 / 删除类按钮仍会先问你。', '授权 自动 可疑 危险 站点 禁区') +
    labelHtml('始终允许的站点（每行一个，支持 *.example.com）', '白名单 站点 域名') +
    '<textarea id="computer-sites" class="field-input" rows="4" spellcheck="false"></textarea>' +
    hintHtml('写进来的站点在任何档位下都直接放行；留空则按上面的授权方式处理。' +
             '本程序的端口与 web/ 目录永远不允许操作。', '白名单 站点 授权') +
    labelHtml('浏览器后端', '后端 扩展 登录态 独立窗口') +
    '<select id="computer-backend" class="field-input">' +
    '<option value="local">本地受控窗口（独立 profile，看不到登录态）</option>' +
    '<option value="extension">浏览器扩展（复用你已登录的浏览器）</option>' +
    '</select>' +
    hintHtml('扩展模式：先在浏览器里加载 <code>browser_ext/</code> 目录（chrome://extensions → 开发者模式 → 加载已解压），' +
             '点「获取连接码」把它填进扩展设置。扩展会新开一个标签页执行任务，不动你正在看的页面。',
             '扩展 连接码 复用登录态') +
    '<div class="balance-row">' +
    '<input id="computer-pair" class="field-input" readonly placeholder="点右侧按钮获取连接码">' +
    '<button type="button" class="ghost" id="computer-pair-btn">获取连接码</button></div>' +
    checkRowHtml('computer-auto-shot',
      '结构不足时自动补一张截图（canvas / 自绘界面）', '截图 自动 canvas 视觉') +
    hintHtml('页面全是画布、或桌面窗口没有可读控件时，自动截一张图交给视觉模型描述；' +
             '同一目标 30 秒内最多一次。关掉可以省 token，但自绘界面就只能靠猜了。',
             '截图 自动 canvas 视觉 token') +
    labelHtml('页面快照字数上限', '快照 长度 token') +
    numInput('computer-snapshot-chars', intVal(cfg.computer_snapshot_chars, 4000), 800, 12000) +
    hintHtml('模型「看」页面时回灌的文字上限（默认 4000）。调大看得更全，但每步都更费 token。',
             '快照 长度 token') +
    setGroupHtml('电脑操作 · 桌面（Windows UIA）', '桌面 程序 窗口 uia 急停 记事本') +
    checkRowHtml('computer-desktop-enabled',
      '允许 AI 操作桌面程序（仅白名单内的窗口）', '桌面 程序 开启 窗口') +
    hintHtml('与浏览器分开开关。开启后模型能读窗口控件树并点击 / 输入；' +
             '提权（管理员）窗口直接拒绝，密码框不读不填。' +
             '<b>急停：Ctrl+Alt+Shift+Esc</b>（按一次冻结，再按恢复）。',
             '桌面 提权 密码 急停') +
    labelHtml('允许操作的桌面程序（每行一个 exe 名，支持 *.exe）', '白名单 程序 exe') +
    '<textarea id="computer-apps" class="field-input" rows="3" spellcheck="false" ' +
    'placeholder="notepad.exe&#10;Code.exe"></textarea>' +
    hintHtml('不在这个列表里的窗口连「列出」都不会出现 —— 这是桌面的第一道闸门。留空 = 桌面功能不可用。' +
             'UWP 应用（计算器 / 设置 / 照片等）在窗口列表里显示为 <code>ApplicationFrameHost.exe</code>；' +
             '不想按 exe 限制时用下面的「无限制」。',
             '白名单 程序 闸门 uwp applicationframehost') +
    checkRowHtml('computer-desktop-unrestricted',
      '无限制：允许操作所有桌面程序（默认关闭）', '无限制 所有程序 桌面 白名单') +
    hintHtml('开启后不再检查 exe 白名单，任何可见窗口都能被列出和操作。' +
             '提权（管理员）窗口仍拒绝、密码框仍不读不填，提交 / 删除类高危动作仍会先问你。',
             '无限制 桌面 提权 密码 高危') +
    setGroupHtml('编辑器 · AI Tab 补全', 'tab 补全 ai 内联 ghost 建议 代码补全') +
    checkRowHtml('ide-ai-complete', '启用 AI Tab 补全（灰色建议，按 Tab 接受）',
      'tab 补全 ai 内联 ghost 建议 代码') +
    hintHtml('需要先在上方「轻量任务的模型」里配置 <code>light_model</code>；' +
             '没配时不会触发（避免拿主模型每 350ms 烧一次 token）。' +
             '光标停顿后自动请求，<kbd>Alt</kbd>+<kbd>\\</kbd> 手动触发，<kbd>Esc</kbd> 关闭。',
             '补全 模型 light 轻量 成本') +
    labelHtml('停顿多久后请求（毫秒）', '补全 延迟 停顿 debounce') +
    limitRow('ide-ai-delay', intVal(cfg.ide_ai_complete_delay_ms, 350)) +
    labelHtml('最多补几行', '补全 行数 长度 建议') +
    limitRow('ide-ai-max-lines', intVal(cfg.ide_ai_complete_max_lines, 8)) +
    setGroupHtml('代码工具（pyright / Git）', 'pyright lsp 类型 跳转 定义 引用 重命名 git 提交 仓库') +
    checkRowHtml('lsp-enabled',
      '启用 pyright 语言服务（类型检查 / 跳转定义 / 查找引用 / 重命名）',
      'pyright lsp 类型 检查 跳转 定义 引用 重命名') +
    hintHtml('首次调用会用 npx 拉取 pyright（约 50MB，走国内镜像），之后复用缓存。' +
             '关掉后模型退回正则版 find_symbol：只能按名字扫文本，拿不到类型检查。',
             'pyright 下载 镜像 缓存') +
    '<div class="agent-approval-cap" data-search="git 提交 commit 权限 自动">AI 提交 Git 的权限</div>' +
    '<div class="agent-approval" data-cfg="git_commit_mode">' +
    gitModeBtn('off', 'alert', '不开放', '模型只能看状态和 diff') +
    gitModeBtn('ask', 'check', '每次确认', '推荐：提交前问你一次') +
    gitModeBtn('auto', 'refresh', '完全自动', '改完就提交，不留确认') +
    '</div>' +
    hintHtml('控制模型能不能调用 git_commit。提交不会改文件内容，改的是 Git 历史，' +
             '所以单独一档、不跟写入审批混用。右侧源代码管理面板里的提交不受此限制。',
             'git 提交 commit 历史 审批') +
    setGroupHtml('外部工具（MCP）', 'mcp 外部 工具 服务 协议 stdio http') +
    checkRowHtml('mcp-enabled',
      '启用 MCP（接入外部工具服务）', 'mcp 外部 工具 开启') +
    hintHtml('<b>默认关闭。</b>stdio server 是独立进程，权限与本程序相同；远程 server 意味着数据出境。' +
             '工具默认每次执行前询问；在每行的「只读工具」里写 glob（如 <code>read_*</code>）才会免问。' +
             '工具的 description 与返回值都来自外部，可能夹带指令 —— 不要接不可信的 server。',
             'mcp 安全 进程 数据 审批 注入') +
    labelHtml('工具下发方式', 'mcp 懒注入 上下文 token') +
    '<select id="mcp-tool-mode" class="field-input">' +
    '<option value="lazy">懒注入（推荐：模型要用哪个 server 才挂它的工具）</option>' +
    '<option value="always">全量常驻（每轮都挂所有工具，更费 token）</option>' +
    '</select>' +
    labelHtml('单次调用超时（秒）', 'mcp 超时') +
    numInput('mcp-timeout', intVal(cfg.mcp_timeout, 60), 5, 600) +
    labelHtml('MCP server 列表', 'mcp server 列表 命令 url 环境变量') +
    '<div id="mcp-list" class="mcp-list"></div>' +
    '<button type="button" class="ghost" id="mcp-add">添加 server</button>' +
    '<div id="mcp-test-out" class="hint"></div>' +
    setGroupHtml('生命周期钩子（Hooks）', 'hooks 钩子 工具 前后 审计 上下文') +
    checkRowHtml('hooks-enabled',
      '启用 Hooks（工具前后 / 回合边界可插自定义逻辑）', 'hooks 钩子 开启') +
    hintHtml('默认关闭：command 钩子会执行任意命令，python 钩子会加载 <code>data/plugins</code> 里的代码。' +
             'PreToolUse 返回 <code>{"decision":"block","reason":"..."}</code> 可阻止工具执行；' +
             '返回 <code>{"context":"..."}</code> 会注入模型上下文。钩子写坏了默认放行（fail-open）。',
             'hooks 阻止 上下文 审计 放行') +
    '<textarea id="hooks-json" class="field-input" rows="6" spellcheck="false" ' +
    'placeholder=\'{"PreToolUse":[{"type":"command","command":"python hooks/audit.py","timeout":10}]}\'></textarea>' +
    '<div id="hooks-err" class="hint"></div>';

  // 这一页原本是「Agent 大杂烩」：按任务拆成 执行 / 子代理与流程 / 外部能力 三个 pane。
  // 节点是搬过去的，控件 id 与绑定逻辑不变；查找统一走 _paneRoot。
  const flowPane = mkPane('flow');
  const extPane = mkPane('ext');
  _moveSections(pane, flowPane, [
    '并行探索（子代理）', '子代理 · 嵌套与后台', '研究闸门（复刻 / 还原类任务）',
    '工作流（多阶段编排）', '变更审阅（撤销快照）',
  ]);
  _moveSections(pane, extPane, [
    '电脑操作 · 浏览器（Computer Use）', '电脑操作 · 桌面（Windows UIA）',
    '编辑器 · AI Tab 补全', '代码工具（pyright / Git）',
    '外部工具（MCP）', '生命周期钩子（Hooks）',
  ]);
  const panes = [pane, flowPane, extPane];
  const root = _paneRoot(panes);

  // ---- MCP server 列表：结构化行（动态增删，保存在 config.json 的 mcp_servers）----
  const mcpList = qIn(root, 'mcp-list');
  if (mcpList) {
    (cfg.mcp_servers || []).forEach((sp) => {
      if (sp && typeof sp === 'object') mcpList.appendChild(mcpServerRow(sp));
    });
    mcpList.addEventListener('click', (ev) => {
      const del = ev.target.closest && ev.target.closest('[data-mcp-del]');
      if (del) {
        const row = del.closest('.mcp-row');
        if (row) row.remove();
        return;
      }
      const tst = ev.target.closest && ev.target.closest('[data-mcp-test]');
      if (tst) runMcpTest(root, tst.closest('.mcp-row'));
    });
  }
  const mcpAdd = qIn(root, 'mcp-add');
  if (mcpAdd && mcpList) {
    mcpAdd.addEventListener('click', () => mcpList.appendChild(mcpServerRow({})));
  }
  const mcpMode = qIn(root, 'mcp-tool-mode');
  if (mcpMode) mcpMode.value = cfg.mcp_tool_mode === 'always' ? 'always' : 'lazy';
  const mcpOn = qIn(root, 'mcp-enabled');
  if (mcpOn) mcpOn.checked = !!cfg.mcp_enabled;
  const hooksJson = qIn(root, 'hooks-json');
  if (hooksJson) {
    const hk = cfg.hooks && typeof cfg.hooks === 'object' ? cfg.hooks : {};
    hooksJson.value = Object.keys(hk).length ? JSON.stringify(hk, null, 2) : '';
    hooksJson.addEventListener('input', () => syncHooksError(root));
  }
  const hooksOn = qIn(root, 'hooks-enabled');
  if (hooksOn) hooksOn.checked = !!cfg.hooks_enabled;

  const sitesEl = qIn(root, 'computer-sites');
  if (sitesEl) sitesEl.value = settingsList(cfg.computer_sites).join('\n');
  const compOnEl = qIn(root, 'computer-enabled');
  if (compOnEl) compOnEl.checked = !!cfg.computer_enabled;
  const modeEl = qIn(root, 'computer-site-mode');
  if (modeEl) {
    let m = String(cfg.computer_site_mode || '').toLowerCase();
    // 与后端 policy.site_mode 保持一致：旧键仍为 true 时按 allow 显示
    if (m === 'ask' && cfg.computer_browser_unrestricted) m = 'allow';
    modeEl.value = (m === 'ask' || m === 'auto' || m === 'allow')
      ? m : (cfg.computer_browser_unrestricted ? 'allow' : 'ask');
  }
  const appsEl = qIn(root, 'computer-apps');
  if (appsEl) appsEl.value = settingsList(cfg.computer_apps).join('\n');
  const deskOnEl = qIn(root, 'computer-desktop-enabled');
  if (deskOnEl) deskOnEl.checked = !!cfg.computer_desktop_enabled;
  const deskUnEl = qIn(root, 'computer-desktop-unrestricted');
  if (deskUnEl) deskUnEl.checked = !!cfg.computer_desktop_unrestricted;
  const beEl = qIn(root, 'computer-backend');
  if (beEl) beEl.value = cfg.computer_backend || 'local';
  const autoShotEl = qIn(root, 'computer-auto-shot');
  if (autoShotEl) autoShotEl.checked = cfg.computer_auto_shot !== false;
  const lspOnEl = qIn(root, 'lsp-enabled');
  if (lspOnEl) lspOnEl.checked = cfg.lsp_enabled !== false;
  const aiCompEl = qIn(root, 'ide-ai-complete');
  if (aiCompEl) aiCompEl.checked = cfg.ide_ai_complete !== false;
  // 这两项以前漏了回填：界面永远显示未勾选，保存时又按未勾选写回，
  // 表现为「并行探索开了也存不住」，还会把外联审计悄悄关掉。
  const netAuditEl = qIn(root, 'agent-net-audit');
  if (netAuditEl) netAuditEl.checked = cfg.agent_net_audit !== false;
  const exploreEl = qIn(root, 'agent-explore-enabled');
  if (exploreEl) exploreEl.checked = cfg.agent_explore_enabled !== false;
  const forkEl = qIn(root, 'agent-explore-fork');
  if (forkEl) forkEl.checked = cfg.agent_explore_fork !== false;
  const qpEl = qIn(root, 'autonomy-question-policy');
  if (qpEl) qpEl.value = String(cfg.autonomy_question_policy || 'ask');
  const accEl = qIn(root, 'agent-acceptance-review');
  if (accEl) accEl.checked = cfg.agent_acceptance_review !== false;
  ['notify-banner', 'notify-sound', 'notify-done', 'notify-ask', 'notify-error'].forEach((id) => {
    const el = qIn(root, id);
    if (el) el.checked = cfg[id.replace(/-/g, '_')] !== false;
  });
  const nvEl = qIn(root, 'notify-volume');
  if (nvEl) nvEl.value = String(cfg.notify_volume == null ? 60 : cfg.notify_volume);
  const exVerEl = qIn(root, 'agent-explore-verify');
  if (exVerEl) exVerEl.checked = cfg.agent_explore_verify !== false;
  const exWriteEl = qIn(root, 'agent-explore-write');
  if (exWriteEl) exWriteEl.checked = cfg.agent_explore_write !== false;
  const exNestWriteEl = qIn(root, 'agent-explore-nested-write');
  if (exNestWriteEl) exNestWriteEl.checked = !!cfg.agent_explore_nested_write;
  const exBgEl = qIn(root, 'agent-explore-background');
  if (exBgEl) exBgEl.checked = cfg.agent_explore_background !== false;
  const shEl = qIn(root, 'agent-shell-mode');
  if (shEl) shEl.value = cfg.agent_shell_mode || 'standard';
  const shAutoEl = qIn(root, 'agent-shell-autonomy');
  if (shAutoEl) shAutoEl.value = cfg.agent_shell_autonomy || 'whitelist';
  const shAllowEl = qIn(root, 'agent-shell-allow');
  if (shAllowEl) shAllowEl.value = settingsList(cfg.agent_shell_allow).join('\n');
  const shDenyEl = qIn(root, 'agent-shell-deny');
  if (shDenyEl) shDenyEl.value = settingsList(cfg.agent_shell_deny).join('\n');
  const netAllowEl = qIn(root, 'agent-net-allow');
  if (netAllowEl) netAllowEl.value = settingsList(cfg.agent_net_allow).join('\n');
  const cmWriteEl = qIn(root, 'code-mode-write');
  if (cmWriteEl) cmWriteEl.checked = !!cfg.code_mode_allow_write;
  const cmCmdEl = qIn(root, 'code-mode-cmd');
  if (cmCmdEl) cmCmdEl.checked = !!cfg.code_mode_allow_cmd;
  const tpEl = qIn(root, 'agent-tool-parallel');
  if (tpEl) tpEl.checked = cfg.agent_tool_parallel !== false;
  const rrEl = qIn(root, 'agent-require-read');
  if (rrEl) rrEl.checked = cfg.agent_require_read !== false;
  const hpEl = qIn(root, 'agent-hall-prompt');
  if (hpEl) hpEl.checked = cfg.agent_hall_prompt !== false;
  const rgEl = qIn(root, 'agent-research-gate');
  if (rgEl) rgEl.checked = cfg.agent_research_gate !== false;
  const wfEl = qIn(root, 'workflow-enabled');
  if (wfEl) wfEl.checked = cfg.workflow_enabled !== false;
  const pairEl = qIn(root, 'computer-pair');
  if (pairEl) pairEl.onclick = () => pairEl.select();
  const pairBtn = qIn(root, 'computer-pair-btn');
  if (pairBtn) {
    pairBtn.onclick = async () => {
      pairBtn.textContent = '读取中…';
      try {
        const r = await api('/api/computer/bridge/status');
        if (pairEl) pairEl.value = r.pairing || '';
        pairBtn.textContent = r.connected ? '扩展已连接' : '扩展未连接';
      } catch (e) {
        pairBtn.textContent = '获取失败';
      }
    };
  }

  // 续跑三档只改草稿，点「保存」才写回配置（与其它设置项一致）
  let acVal = acNow;
  root.querySelectorAll('.continue-opt').forEach((b) => {
    b.onclick = () => {
      acVal = parseInt(b.dataset.continue, 10) || 0;
      root.querySelectorAll('.continue-opt').forEach((x) => {
        x.classList.toggle('active', (parseInt(x.dataset.continue, 10) || 0) === acVal);
      });
      queueDirtyCheck();
    };
  });

  const clamp = (id, d, lo, hi) => {
    const n = parseInt((qIn(root, id) || {}).value, 10);
    return Math.max(lo, Math.min(hi, Number.isFinite(n) ? n : d));
  };
  // Agent 五项上限：0 = 不限，不能被「上限」钳掉；负数视为填错，回落默认
  const clampLimit = (id, d) => {
    const n = parseInt((qIn(root, id) || {}).value, 10);
    if (!Number.isFinite(n) || n < 0) return d;
    return n === 0 ? 0 : Math.max(1, n);
  };
  // 「不限」按钮：点一下填 0；输入框里是 0 时高亮，一眼能看出这项没设上限
  const syncUnli = () => {
    root.querySelectorAll('[data-unli]').forEach((b) => {
      const inp = qIn(root, b.dataset.unli);
      b.classList.toggle('active', !!inp && String(inp.value).trim() === '0');
    });
  };
  root.querySelectorAll('[data-unli]').forEach((b) => {
    b.onclick = () => {
      const inp = qIn(root, b.dataset.unli);
      if (!inp) return;
      inp.value = '0';
      syncUnli();
      queueDirtyCheck();
    };
  });
  root.addEventListener('input', syncUnli);
  syncUnli();
  let gitMode = gitModeNow;
  root.querySelectorAll('.gitmode-opt').forEach((b) => {
    b.onclick = () => {
      gitMode = b.dataset.gitmode || 'ask';
      root.querySelectorAll('.gitmode-opt').forEach((x) => {
        x.classList.toggle('active', x.dataset.gitmode === gitMode);
      });
      queueDirtyCheck();
    };
  });
  // 全局权限预设：立即写 data/agent-rules.json（不走「保存」草稿，覆盖前自动备份）
  const refreshPresetState = async () => {
    const el = root.querySelector('#agent-preset-state');
    if (!el) return;
    try {
      const r = await api('/api/agent/rules', {});
      if (!r || !r.ok) { el.textContent = ''; return; }
      const g = r.global_rules || {};
      const cmds = g.commands || {};
      const n = ['allow', 'ask', 'deny'].reduce((a, k) => a
        + (g[k] || []).length + (cmds[k] || []).length, 0);
      el.textContent = r.global_exists
        ? ('当前全局规则：' + n + ' 条 · ' + (r.global_path || ''))
        : '当前没有全局规则（应用预设后对所有项目生效）';
    } catch (e) { el.textContent = ''; }
  };
  root.querySelectorAll('.agent-preset').forEach((b) => {
    b.onclick = async () => {
      const preset = b.dataset.preset;
      if (preset === 'clear' && !confirm('清空全局权限规则？旧文件会先备份。')) return;
      try {
        const r = await api('/api/agent/rules/preset', { preset });
        if (!r || !r.ok) {
          toast('应用失败：' + ((r && r.error) || '未知错误'), 'error');
          return;
        }
        const label = (b.querySelector('b') || {}).textContent || preset;
        toast(preset === 'clear' ? '已清空全局规则' : ('已应用预设：' + label));
        refreshPresetState();
      } catch (e) { toast('应用失败：' + e, 'error'); }
    };
  });
  refreshPresetState();
  const collectAll = () => ({
    agent_max_rounds: clampLimit('agent-max-rounds', 0),
    agent_auto_continue: acVal,
    agent_acceptance_review: !!(qIn(root, 'agent-acceptance-review') || {}).checked,
    autonomy_question_policy: (qIn(root, 'autonomy-question-policy') || {}).value || 'ask',
    notify_banner: !!(qIn(root, 'notify-banner') || {}).checked,
    notify_sound: !!(qIn(root, 'notify-sound') || {}).checked,
    notify_done: !!(qIn(root, 'notify-done') || {}).checked,
    notify_ask: !!(qIn(root, 'notify-ask') || {}).checked,
    notify_error: !!(qIn(root, 'notify-error') || {}).checked,
    notify_volume: Math.max(0, Math.min(100,
      parseInt((qIn(root, 'notify-volume') || {}).value, 10) || 0)),
    agent_cmd_timeout: clampLimit('agent-cmd-timeout', 60),
    agent_net_audit: !!(qIn(root, 'agent-net-audit') || {}).checked,
    agent_explore_enabled: !!(qIn(root, 'agent-explore-enabled') || {}).checked,
    agent_explore_fork: !!(qIn(root, 'agent-explore-fork') || {}).checked,
    agent_explore_fork_tokens: clampLimit('agent-explore-fork-tokens', 12000),
    agent_explore_verify: !!(qIn(root, 'agent-explore-verify') || {}).checked,
    agent_explore_write: !!(qIn(root, 'agent-explore-write') || {}).checked,
    agent_explore_write_max: clampLimit('agent-explore-write-max', 16),
    agent_explore_max_depth: clampLimit('agent-explore-max-depth', 5),
    agent_explore_max_total: clampLimit('agent-explore-max-total', 100),
    agent_explore_nested_write: !!(qIn(root, 'agent-explore-nested-write') || {}).checked,
    agent_explore_background: !!(qIn(root, 'agent-explore-background') || {}).checked,
    agent_explore_background_max: clampLimit('agent-explore-background-max', 12),
    agent_explore_background_ttl: clampLimit('agent-explore-background-ttl', 86400),
    agent_research_gate: !!(qIn(root, 'agent-research-gate') || {}).checked,
    agent_research_min_searches: clampLimit('agent-research-min-searches', 2),
    agent_explore_max: clampLimit('agent-explore-max', 8),
    agent_explore_budget: clampLimit('agent-explore-budget', 160000),
    agent_explore_ttl: clampLimit('agent-explore-ttl', 600),
    workflow_enabled: !!(qIn(root, 'workflow-enabled') || {}).checked,
    workflow_max_steps: clampLimit('workflow-max-steps', 48),
    workflow_max_parallel: clampLimit('workflow-max-parallel', 10),
    workflow_keep: clampLimit('workflow-keep', 200),
    agent_repeat_limit: clampLimit('agent-repeat-limit', 5),
    agent_shell_mode: (qIn(root, 'agent-shell-mode') || {}).value || 'standard',
    agent_shell_autonomy: (qIn(root, 'agent-shell-autonomy') || {}).value || 'whitelist',
    agent_shell_allow: settingsList((qIn(root, 'agent-shell-allow') || {}).value || ''),
    agent_shell_deny: settingsList((qIn(root, 'agent-shell-deny') || {}).value || ''),
    agent_net_allow: settingsList((qIn(root, 'agent-net-allow') || {}).value || ''),
    code_mode_allow_write: !!(qIn(root, 'code-mode-write') || {}).checked,
    code_mode_allow_cmd: !!(qIn(root, 'code-mode-cmd') || {}).checked,
    agent_prefetch_workers: clampLimit('agent-prefetch-workers', 10),
    agent_prefetch_max: clampLimit('agent-prefetch-max', 24),
    agent_tool_parallel: !!(qIn(root, 'agent-tool-parallel') || {}).checked,
    agent_tool_parallel_max: clampLimit('agent-tool-parallel-max', 10),
    agent_require_read: !!(qIn(root, 'agent-require-read') || {}).checked,
    agent_hall_prompt: !!(qIn(root, 'agent-hall-prompt') || {}).checked,
    tool_result_max: clampLimit('tool-result-max', 30000),
    tool_result_head: clampLimit('tool-result-head', 4096),
    tool_result_tail: clampLimit('tool-result-tail', 1024),
    tool_result_keep_recent: clampLimit('tool-result-keep-recent', 2),
    tool_result_fold_chars: clampLimit('tool-result-fold-chars', 240),
    agent_max_undo: clampLimit('agent-max-undo', 60),
    lsp_enabled: !!(qIn(root, 'lsp-enabled') || {}).checked,
    ide_ai_complete: !!(qIn(root, 'ide-ai-complete') || {}).checked,
    ide_ai_complete_delay_ms: clampLimit('ide-ai-delay', 350),
    ide_ai_complete_max_lines: clampLimit('ide-ai-max-lines', 8),
    git_commit_mode: gitMode,
    computer_enabled: !!(qIn(root, 'computer-enabled') || {}).checked,
    computer_site_mode: (qIn(root, 'computer-site-mode') || {}).value || 'ask',
    // 旧键继续写一份，保证旧客户端 / 旧代码路径行为一致
    computer_browser_unrestricted:
      ((qIn(root, 'computer-site-mode') || {}).value || '') === 'allow',
    computer_sites: settingsList((qIn(root, 'computer-sites') || {}).value || ''),
    computer_snapshot_chars: clamp('computer-snapshot-chars', 4000, 800, 12000),
    computer_desktop_enabled: !!(qIn(root, 'computer-desktop-enabled') || {}).checked,
    computer_desktop_unrestricted: !!(qIn(root, 'computer-desktop-unrestricted') || {}).checked,
    computer_apps: settingsList((qIn(root, 'computer-apps') || {}).value || ''),
    computer_backend: (qIn(root, 'computer-backend') || {}).value || 'local',
    computer_auto_shot: !!(qIn(root, 'computer-auto-shot') || {}).checked,
    mcp_enabled: !!(qIn(root, 'mcp-enabled') || {}).checked,
    mcp_tool_mode: (qIn(root, 'mcp-tool-mode') || {}).value || 'lazy',
    mcp_timeout: clamp('mcp-timeout', 60, 5, 600),
    mcp_servers: mcpCollectRows(root),
    hooks_enabled: !!(qIn(root, 'hooks-enabled') || {}).checked,
    hooks: parseHooksJson(qIn(root, 'hooks-json') || { value: '' }),
  });

  // 三个 pane 共用同一套读取逻辑，各自只上报本页的键（「恢复本页默认」按它推导范围）
  const AGENT_KEYS = [
    'agent_max_rounds', 'agent_auto_continue', 'agent_acceptance_review',
    'autonomy_question_policy',
    'notify_banner', 'notify_sound', 'notify_done', 'notify_ask', 'notify_error',
    'notify_volume', 'agent_cmd_timeout', 'agent_net_audit', 'agent_repeat_limit',
    'agent_shell_mode', 'agent_prefetch_workers', 'agent_prefetch_max',
    'agent_tool_parallel', 'agent_tool_parallel_max', 'agent_require_read',
    'agent_hall_prompt', 'tool_result_max', 'tool_result_head', 'tool_result_tail',
    'tool_result_keep_recent', 'tool_result_fold_chars',
    'code_mode_allow_write', 'code_mode_allow_cmd',
  ];
  const FLOW_KEYS = [
    'agent_explore_enabled', 'agent_explore_fork', 'agent_explore_fork_tokens',
    'agent_explore_verify', 'agent_explore_write', 'agent_explore_write_max',
    'agent_explore_max_depth', 'agent_explore_max_total', 'agent_explore_nested_write',
    'agent_explore_background', 'agent_explore_background_max',
    'agent_explore_background_ttl', 'agent_research_gate', 'agent_research_min_searches',
    'agent_explore_max', 'agent_explore_budget', 'agent_explore_ttl',
    'workflow_enabled', 'workflow_max_steps', 'workflow_max_parallel', 'workflow_keep',
    'agent_max_undo',
  ];
  const EXT_KEYS = [
    'lsp_enabled', 'ide_ai_complete', 'ide_ai_complete_delay_ms',
    'ide_ai_complete_max_lines', 'git_commit_mode',
    'computer_enabled', 'computer_site_mode', 'computer_browser_unrestricted',
    'computer_sites', 'computer_snapshot_chars', 'computer_desktop_enabled',
    'computer_desktop_unrestricted', 'computer_apps', 'computer_backend',
    'computer_auto_shot', 'mcp_enabled', 'mcp_tool_mode', 'mcp_timeout', 'mcp_servers',
    'hooks_enabled', 'hooks',
  ];
  const pickKeys = (keys) => {
    const all = collectAll();
    const out = {};
    keys.forEach((k) => { if (k in all) out[k] = all[k]; });
    return out;
  };
  pane._collect = () => pickKeys(AGENT_KEYS);
  flowPane._collect = () => pickKeys(FLOW_KEYS);
  extPane._collect = () => pickKeys(EXT_KEYS);
  return panes;
}
