'use strict';

/* ============================================================
   快捷键「完整说明」数据层
   ------------------------------------------------------------
   这一层只有数据，不碰 DOM、不碰 localStorage，可被 Node 直接 require
   （scripts/hotkeys_test.js 会校验它和 HK_DEFS 是否一一对应）。

   为什么单独一张表：
   - 旧版「完整说明」是 index.html 里手写的 6 张表，改键 / 新增功能后经常忘了同步，
     已经和实际判定脱节（输入框缺 Ctrl+Enter、全局缺 Ctrl+F 等）。
   - 现在可改键的条目只写 `def: '注册表 id'`，按键名由 hotkeys.js 从 HK_DEFS 现取，
     改键后说明自动跟着变；不可改键的条目才写死 `keys`（展示名）。
   - 新增快捷键时：能进注册表的进 HK_DEFS；不能改键的补到这里，缺一条会被测试拦住。

   字段：
   - def     HK_DEFS 里的 id（按键与名称都从这里取）
   - keys    不可改键条目的展示名数组（按键符号直接用最终展示文本）
   - name    不可改键条目的作用
   - cond    生效条件（这一列回答「为什么我按了没反应」）
   - note    补充说明，可选
   - search  额外的搜索关键词，可选
   ============================================================ */

const HK_DOC_GROUPS = [
  {
    id: 'global',
    title: '全局与模式',
    search: '全局 模式切换 会话排序 关闭 浮层 esc 查找 渲染 源码 快捷键清单',
    rows: [
      { def: 'global.help',
        cond: '焦点不在编辑器时（编辑器里 Ctrl+/ 归 Monaco 的行注释）',
        note: '输入框内只认 Ctrl / ⌘ 组合，正常打「?」不会误触。' },
      { def: 'global.find',
        cond: 'Solo / IDE 模式；设置弹窗未打开；编辑器聚焦时让给编辑器内的查找' },
      { def: 'global.render',
        cond: 'Solo 对话或笔记打开时；IDE 模式下交给 Monaco 的 Markdown 预览' },
      { keys: ['Ctrl+Alt+1', 'Ctrl+Alt+2', 'Ctrl+Alt+3', 'Ctrl+Alt+4'],
        name: '切换 Solo / IDE / 大厅 / 工作台',
        cond: '任意模式',
        note: '不依赖侧栏，也不需要先回到对应模式。' },
      { keys: ['Alt+↑', 'Alt+↓'],
        name: '上移 / 下移当前会话',
        cond: '焦点在左侧会话条目上',
        note: '拖拽排序的键盘等价入口。' },
      { keys: ['Esc'],
        name: '关闭最上层浮层 / 弹窗',
        cond: '设置弹窗未打开时',
        note: '按层关闭：存档 → 用量 → 笔记 → 知识库 → GAL 编辑器 → 开场卡 → 抽屉菜单 → 背景 / BGM → 快速克隆 → 剧本 → 回顾 → 日志。设置弹窗自己的 Esc 见「设置面板」。' },
    ],
  },
  {
    id: 'composer',
    title: '对话与输入框',
    search: '输入框 发送 换行 润色 清空 历史 指令 补全 菜单 提问卡',
    rows: [
      { def: 'cmp.sendEnter',
        cond: '输入框聚焦、未按 Shift、输入法未在组词' },
      { def: 'cmp.send',
        cond: '输入框聚焦' },
      { keys: ['Shift+Enter'],
        name: '换行（不发送）',
        cond: '输入框聚焦',
        note: '固定键，不参与改键。' },
      { def: 'cmp.polish',
        cond: '输入框有内容' },
      { def: 'cmp.clear',
        cond: '输入框有内容；正在修改历史消息时先退出修改状态' },
      { def: 'cmp.history',
        cond: '输入框为空时' },
      { keys: ['↑', '↓', 'Tab', 'Enter', 'Esc'],
        name: '指令 / 补全菜单：选择、采纳、关闭',
        cond: '菜单展开时',
        note: '↑↓ 移动、Tab 或 Enter 采纳、Esc 关闭。' },
      { keys: ['Enter', 'Esc'],
        name: '提交 / 关闭 Agent 提问卡',
        cond: '提问卡打开时',
        note: '输入法组词期间不触发；提问卡与 Agent 无关，不选沙箱也能用。' },
    ],
  },
  {
    id: 'gal',
    title: 'GAL 演出',
    search: 'gal 推进 快进 自动播放 回顾 选项 舞台 角色面板',
    rows: [
      { def: 'gal.advance',
        cond: 'GAL 模式、焦点不在输入框或按钮上、没有弹窗；出现选项时先做选择' },
      { def: 'gal.fast',
        cond: '按住时；焦点不在输入框' },
      { def: 'gal.auto',
        cond: 'GAL 模式、无弹窗' },
      { def: 'gal.review',
        cond: '回顾面板尚未打开' },
      { def: 'gal.option',
        cond: '选项浮层出现时',
        note: '↑ / ↓ 移动，Enter / 空格确认，数字键 1–9 按显示顺序直接选。' },
      { keys: ['点击舞台'],
        name: '推进对话',
        cond: '不点在按钮或面板上' },
      { def: 'gal.esc',
        cond: 'GAL 角色 / 背景 / BGM 面板的搜索框聚焦时',
        note: 'Esc 只关当前面板；其它 GAL 浮层由「全局」里的 Esc 按层关闭。' },
    ],
  },
  {
    id: 'ide',
    title: 'IDE 工作区',
    search: 'ide 保存 快速打开 命令面板 文件管理区 侧边栏 终端 审阅 保留 撤销 调试 断点 F5 F10 F11',
    rows: [
      { def: 'ide.save', cond: 'IDE 模式' },
      { def: 'ide.open', cond: 'IDE 模式' },
      { def: 'ide.palette', cond: 'IDE 模式' },
      { def: 'ide.sidebar', cond: 'IDE 模式' },
      { def: 'ide.terminal', cond: 'IDE 模式' },
      { def: 'ide.debugStart', cond: 'IDE 模式、焦点不在输入框 / 编辑器文本区' },
      { def: 'ide.debugNext', cond: '调试暂停、焦点不在输入框 / 编辑器文本区' },
      { def: 'ide.debugIn', cond: '调试暂停、焦点不在输入框 / 编辑器文本区' },
      { def: 'ide.debugOut', cond: '调试暂停、焦点不在输入框 / 编辑器文本区' },
      { def: 'ide.debugStop', cond: '调试进行中、焦点不在输入框 / 编辑器文本区' },
      { keys: ['↑', '↓', 'Enter', 'Esc'],
        name: '命令面板 / 快速打开：选择、执行、关闭',
        cond: '对应面板打开时' },
      { keys: ['Enter'],
        name: '在沙箱内搜索',
        cond: '焦点在搜索框时' },
      { keys: ['Ctrl+Enter'],
        name: '保留本轮全部改动（变更审阅）',
        cond: '审阅视图打开、焦点不在聊天输入框' },
      { keys: ['Ctrl+Backspace'],
        name: '撤销本轮全部改动（变更审阅）',
        cond: '审阅视图打开、焦点不在聊天输入框' },
      { keys: ['Esc'],
        name: '关闭弹窗式审阅',
        cond: '审阅以弹窗打开时' },
    ],
  },
  {
    id: 'editor',
    title: '编辑器（VS Code / Monaco）',
    search: 'monaco vscode 编辑器 撤销 重做 注释 删除行 移动行 复制行 插入行 缩进 多光标 查找 替换 跳转行',
    rows: [
      { def: 'ed.selectLine', cond: '编辑器聚焦时' },
      { def: 'ed.undo', cond: '编辑器聚焦时' },
      { def: 'ed.redo', cond: '编辑器聚焦时' },
      { def: 'ed.comment', cond: '编辑器聚焦时' },
      { def: 'ed.delLine', cond: '编辑器聚焦时' },
      { def: 'ed.moveUp', cond: '编辑器聚焦时' },
      { def: 'ed.moveDown', cond: '编辑器聚焦时' },
      { def: 'ed.copyUp', cond: '编辑器聚焦时' },
      { def: 'ed.copyDown', cond: '编辑器聚焦时' },
      { def: 'ed.insertBelow', cond: '编辑器聚焦时' },
      { def: 'ed.insertAbove', cond: '编辑器聚焦时' },
      { def: 'ed.selNext', cond: '编辑器聚焦时' },
      { def: 'ed.indent', cond: '编辑器聚焦时' },
      { def: 'ed.outdent', cond: '编辑器聚焦时' },
      { def: 'ed.find', cond: '编辑器聚焦时' },
      { def: 'ed.replace', cond: '编辑器聚焦时' },
      { def: 'ed.gotoLine', cond: '编辑器聚焦时' },
    ],
  },
  {
    id: 'editorTools',
    title: '编辑器进阶（导航 / AI / 运行）',
    search: '转到定义 引用 重命名 大纲 问题 ai 改写 预览 latex 运行 格式化 批注',
    rows: [
      { def: 'ed.gotoDef', cond: '编辑器聚焦时' },
      { def: 'ed.gotoRefs', cond: '编辑器聚焦时' },
      { def: 'ed.renameSymbol', cond: '编辑器聚焦时' },
      { def: 'ed.outline', cond: '编辑器聚焦时' },
      { def: 'ed.problems', cond: '编辑器聚焦时' },
      { def: 'ed.inlineEdit', cond: '编辑器聚焦且有选区' },
      { def: 'ed.inlineAsk', cond: '编辑器聚焦且有选区' },
      { def: 'ed.mdPreview', cond: '编辑器聚焦、当前文件是 Markdown' },
      { def: 'ed.texBuild', cond: '编辑器聚焦、当前文件是 LaTeX' },
      { def: 'ed.runFile', cond: '编辑器聚焦' },
      { def: 'ed.formatDoc', cond: '编辑器聚焦' },
      { def: 'ed.addAnno', cond: '编辑器聚焦且有选区' },
    ],
  },
  {
    id: 'ideTools',
    title: 'IDE 进阶（AI 补全）',
    search: 'ide ai 补全 候选 采纳 收起',
    rows: [
      { keys: ['Alt+\\'],
        name: '手动触发 AI 补全',
        cond: '编辑器聚焦、已配置「轻量任务的模型」',
        note: '自动模式停顿时也会补全；这个键用来手动催一次。' },
      { keys: ['Alt+]', 'Alt+['],
        name: '下一个 / 上一个 AI 补全候选',
        cond: '补全候选浮层出现时' },
      { keys: ['Tab', 'Esc'],
        name: '采纳 / 收起 AI 补全',
        cond: '补全建议显示时' },
    ],
  },
  {
    id: 'settings',
    title: '设置面板（本页）',
    search: '设置 关闭 保存 分类 搜索 筛选',
    rows: [
      { keys: ['Esc'],
        name: '关闭设置；搜索框有内容时先清空',
        cond: '设置弹窗打开时',
        note: '「功能说明」的筛选框有内容时，Esc 先清空筛选，不会顺手关掉设置。' },
      { keys: ['Ctrl+S'],
        name: '保存并关闭',
        cond: '设置弹窗打开时',
        note: '设置内的 Ctrl+P / Ctrl+Shift+P 会被拦下，避免叠出编辑器面板。' },
      { keys: ['←', '→'],
        name: '切换左侧分类',
        cond: '焦点不在输入框 / 下拉 / 文本域' },
      { keys: ['↑', '↓', 'Enter', 'Esc'],
        name: '搜索结果：选择、跳转、清空',
        cond: '焦点在左上角搜索框时' },
    ],
  },
];

// Node 单元测试用（浏览器里 module 未定义，这段不执行）
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { HK_DOC_GROUPS };
}
