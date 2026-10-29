# NOTES

## [2026-10-29] 移除外卖比价模块（Deal Advisor）+ 清理遗留临时脚本

- 问题/决策：外卖比价按 `NOTE.md` 1.0 已于 2026-10-18 暂停（美团 PC 网页无点餐入口，扩展读不到 App），但代码、路由、扩展目录一直留着，属于「已废弃却仍被导入」的死代码。决定整体移除，只保留 `.bak` 存档以备移动版方案重启时复用。
- 解决方案/原因：
  - 删除前先查引用边界：后端 4 个文件、`extension/` 6 个文件、2 个测试，前端 `web/` **没有任何外卖入口**（无面板、无调用）。
  - 关键风险：这些文件在 git 里**全部未跟踪**（`git ls-files` 返回空），删除即永久丢失。因此先按 `_backup/` 既有 `.bak` 习惯存档再删。
  - `app/deal_caps.py` 的 `deal.analyze` / `deal.latest` 两个能力**未被任何启动路径注册**（`task_engine.py` 早已移除导入），删文件即等于下线能力，无需改注册点。
  - `app/deal_bridge.py` 的 `start_bridge()` 早已不被 `main.py` 调用，不占端口。
  - `extension/` 与电脑控制用的 `browser_ext/` 是**两个不同扩展**，删前者不影响 `tests/test_computer_ext_live.py`。
- 删除清单（12 个文件，均已备份为 `_backup/**/*.bak`）：
  - 后端：`app/deal.py`(39.1KB)、`app/deal_bridge.py`、`app/deal_caps.py`、`app/api/deal.py`
  - 扩展：`extension/{manifest.json,background.js,content.js,popup.html,popup.js,popup.css}`，删除后空目录一并移除
  - 测试：`tests/test_deal.py`、`tests/test_deal_bridge.py`
- 引用清理（2 处）：
  - `app/httpd.py` 第 54 行 `from app.api import deal as deal_api  # noqa: F401` 已删（该文件 579 → 578 行）
  - `app/agent.py` 的 `_CAP_NAMESPACES` 集合去掉 `"deal"`（第 426 行，集合现为 `{"bili","calendar","stock","memory","task","note","observe","skill","doc","llm","search","workflow","computer","gal","kb"}`）
- 验证结果（全部实测）：
  - `check_code`：`app/` 153 个文件、`tests/` 139 个文件，均无语法错误。
  - 导入验证：`app.httpd` / `app.agent` / `app.registry` 均 import 成功；`importlib.util.find_spec` 确认 `app.deal` / `app.deal_bridge` / `app.deal_caps` / `app.api.deal` 四个模块已不存在。
  - 路由表：`registry.ROUTES` 共 86 条，`registry.get("POST", "/api/deal/analyze")` 返回 `None`，无任何 deal 路由残留。
  - 测试收集：`python -m pytest --collect-only -q` 退出码 0，139 个测试文件全部收集成功，`test_deal*` 已从列表消失，无 ImportError。
  - 全仓搜 `deal` 仅剩无关命中：`app/interview.py:1656` 面试题英文原句 "How do you deal with negative emotions?"、`web/hall-interview.js` 的 "deal" 子串、monaco 词库。
- 相关代码/指令：
  - 回滚：把 `_backup/app/*.bak` → `app/`、`_backup/tests/*.bak` → `tests/`、`_backup/extension/*.bak` → `extension/`（去掉 `.bak` 后缀），再手动恢复 `app/httpd.py` 的导入行与 `app/agent.py` 集合里的 `"deal"`。
  - 复查命令：`python -m pytest --collect-only -q`、`search_files "deal"`。
- 顺带清理：此前通读项目时在 `.tmp_check/` 建的统计与校验脚本（`count.py`、`verify_import.py`）已全部删除，`.tmp_check/` 目录本身也已移除。
- 注意：`NOTE.md` 第 1 章（约 1.0MB 的 `NOTE.md` 开头的「外卖比价（Deal Advisor）」整节，含已暂停状态表与架构图）**未改动**，现与代码不一致（它仍描述 `app/deal.py` 保留、`app/api/deal.py` 路由注册中）。如需同步清理该章节，需要你确认——它是历史决策记录，可能想留着当存档。

## [2026-10-29] 项目地图（通读现状，供后续改动定位）

- 问题/决策：项目体量已远超「3 个 commit」的表面印象，模块多、单文件巨大，改动前需要一份「入口 → 服务 → 业务 → 数据」的对照表，否则容易改错层或在两个同名实现之间犹豫。
- 解决方案/原因（分层事实，均按代码核对）：
  - **启动器** `main.py`（517 行）：`from server import create_server` 起后端，再用 Edge/Chrome「应用模式」窗口打开（无地址栏）。落盘文件都在 `data/`：`app.log`（>8MB 启动时轮转为 `app.log.1`）、`server.json`（状态）、`app_port.txt`（端口）。无控制台时（pythonw）把 stdout/stderr 重定向进 `app.log`。端口在 8765–8774 之间尝试。
  - **服务层** `server.py`（3 行）只是兼容壳：`from app.httpd import create_server`。真正实现在 `app/httpd.py`（571 行，`ThreadingHTTPServer` + `Handler`），静态资源有 mtime+size 内存缓存，主令牌 `TOKEN = secrets.token_urlsafe(24)`。
  - **路由双轨**：`app/httpd.py` 保留老的硬编码路由表，新模块通过 `app/registry.py` 的 `@route("/api/xxx")` 自注册（默认只收 POST，同一 method+path 重复注册直接报错）。**新增接口走 `@route`，不要往老表里加**。
  - **业务层** `app/` 共 101 项（74 个 .py + 子包 `api/ computer/ dojo/ math/`）。主链路是 `app/api/chat.py`（4773 行 / 324.5KB，流式 SSE 聊天，15 秒 `: ping` 心跳）。其余大块：`app/agent.py` 317KB（文件 Agent）、`app/stock.py` 134KB、`app/math_arena.py` 147KB、`app/interview.py` 107KB、`app/gal.py` 84KB、`app/autopilot.py` 79KB。`app/api/` 下 30 个文件，各自对应一组 REST 接口。
  - **数据层两份并存**：JSON 走 `app/store.py`（`APP_VERSION="2.0"`，路径常量 + mtime 缓存 + 临时文件 `os.replace` 原子写，带 3 次退避重试防 Windows WinError 5）；结构化数据走 `app/db.py` 的 SQLite `data/assistant.db`（表 `tasks / task_steps / artifacts / events / event_exdates / event_fired`，每线程一连接、WAL）。对话仍在 `data/sessions.json`，未迁移。
  - **前端** `web/`（33 项）：`index.html` 198KB 承载全部面板，`chat.js` 126KB、`stream.js` 101KB、`settings.js` 159KB、`hall-dojo.js` 151KB、`hall-interview.js` 149KB；原生 JS + CSS，无构建步骤，`web/vendor/` 内置 katex / vis-network / xterm。
  - **随包资源** `resources/`：`prompts/*.md`（10 个提示词）、`presets/gal/{backgrounds,bgm,characters,effects}`、`dojo/`、`eval/`。运行期数据在 `data/`（整目录 gitignore）。
  - **测试** `tests/` 140 个文件，覆盖 agent / chat / hall / dojo / stock / interview / gal / lsp / workflow 等；另有 `pytest.ini`、`ruff.toml`、`scripts/dev_check.py`。
- 相关代码/指令：
  - 启动：`python main.py`（装依赖见 `requirements.txt`，需 Python 3.10+ / Windows）
  - 查结构：`git status --short --branch`、`pytest -q`、`ruff check`、`python scripts/dev_check.py`
- 现状与风险（据 `git status`，2026-10-29）：
  1. **主体代码从未入库**：分支 `main` 领先 origin 1 个 commit，只有 3 个 commit（初始版 / GAL+Agent+IDE 重构 / 根启动器）。`git status` 显示大量未跟踪条目，覆盖 `app/`、`web/`、`tests/`、`scripts/`、`desktop/` 的绝大部分（按目录折叠口径统计为 156 行未跟踪、48 行已跟踪改动、26 行已暂存新增、2 行删除；`-uall` 展开后条目数会显著变大，具体数字以当场 `git status` 为准）。本地损坏或误删无法从 git 恢复。
  2. **超大单文件**：`app/api/chat.py` 4773 行、`app/agent.py` 317KB、`web/index.html` 198KB。改动集中在这几个文件时，冲突与漏改风险高。
  3. **路由双轨**：老硬编码表 + `registry.py` 自注册并存，查某接口要先判断属于哪一套。
  4. **`sessions.json` 单体增长**：`store.py` 注释记载已到十几 MB，写入窗口易撞 WinError 5（现靠唯一临时名 + 3 次退避重试兜）。
  5. ~~**遗留面积**：外卖比价（`app/deal*.py`、`app/api/deal.py`、`extension/`）按 `NOTE.md` 1.0 已暂停（美团 PC 无点餐入口），但代码与路由仍在。~~ **已于 2026-10-29 移除**，见下方「外卖比价模块下线」条目。

## [2026-10-28] 简历 AI 访谈重构：岗位定标 + 覆盖度驱动 + 结构化事实

- 问题/决策：原「AI 引导制作」是一次问一个点、问十几题、无收敛标准、生成时直接吃大段口语原文，导致用户要答很久、成稿还是流水账。改为四件事：① 目标岗位为空时第一题强制问岗位（可跳过，跳过后按通用模板）；② 用六个板块的覆盖度分数决定还差什么、什么时候停；③ 一段经历一次问完「负责什么 → 用什么方法 → 做出什么结果」；④ 每轮同一次模型调用顺带抽出结构化事实，生成时优先用事实、访谈原文只作补充。
- 解决方案/原因：
  - 覆盖度：`_guide_coverage()` 给 basic/education/experience/projects/skills/extra 各打 0-100，`_guide_enough()` 判定收敛（basic≥60、education≥40、max(经历,项目)≥70、skills≥50）。`experience`/`projects` 有数字额外 +30，逼用户给结果。
  - 题数硬上限：`MAX_GUIDE_STEPS = 10`，`answered + 1 >= 10` 直接 done；另外 `enough and answered + 1 >= 3` 提前收。
  - 结构化事实：`guide_turn` 返回 `facts: {板块: [要点句]}`，前端渲染成可逐条删除的事实卡；`build_resume(profile, role, jd, qa, facts)` 里 facts 段排在访谈原文之前。
  - 问句硬校验：`_looks_like_question()` 要求 ≥4 字且以「？」结尾，不合格换成题库兜底，避免出现「那次数模赛……没关系。」这类假问题。注意 skip 重复题判断必须放在问句校验之前，否则非法问句先被替换，重复检测失效。
  - 可定位缺口：`build_resume` 返回 `gaps: [{section, need}]`，结果页每条带「回去补这一块」按钮，点回去把 `focus` 带给访谈。
  - 无模型降级：`_qa_facts()` 按每题记录的 `f`（板块）把答案归档，没接模型时进度条也会动、缺口也算得出来。qa 结构因此从 `{q,a}` 扩到 `{q,a,f}`。
- 相关代码/指令：
  - 后端 `app/interview.py`（`GUIDE_SECTIONS` / `MAX_GUIDE_STEPS` / `_RESUME_GUIDE_BANK` / `_guide_coverage` / `_guide_enough` / `_qa_facts` / `_normalize_facts` / `_merge_facts` / `_looks_like_question` / `guide_turn` / `build_resume` / `_local_gaps`），`app/api/hall_interview.py` 透传 facts。
  - 前端 `web/hall-interview.js`（`ivGuideMeterHtml` / `ivFactsHtml` / `ivNormalizeFacts` / `ivMergeFacts` / 岗位回写 / gaps 回跳），样式 `web/css/hall-interview.css` 的 `.iv-guide-meter` / `.iv-facts`。
  - 测试与验证：`pytest tests/test_interview.py`（56 条）、`node --check web/hall-interview.js`、`ruff check`、`python scripts/dev_check.py` 全部通过；浏览器实测第 1 题→答「数据分析」→第 2 题、事实 +1、跳过正常推进、guide 接口 3 次均 200。
  - 窄屏兜底：进度条 + 事实卡出现后，弹窗内层（`.iv-build-inner`）总高会超出 `.iv-modal-body.iv-build-flex` 的 `overflow: hidden`，输入框和「语音输入」被直接裁掉、无法点击。修法是在 `(max-width: 640px), (max-height: 720px)` 下把内层改为 `overflow-y: auto`，并给对话区限高 220px、事实卡限高 132px、输入框最小高降到 64px（`web/css/hall-interview.css` 第 609 行起）。
  - 窄屏复测（2026-10-28，376×653）：答第 1 题并展开事实卡后，`.iv-build-inner` 可滚动（527/394），滚到底「语音输入」完整可见，弹窗底部无裁切，提交/跳过/语音按钮中心均可命中；对话区另有 220px 独立滚动，形成嵌套滚动；内层滚到底后顶部「第 2 / 10 题」会滚出可视区。桌面尺寸不受影响（媒体查询不命中）。

## [2026-10-28] 双端互通方案调研与实现记录（已全部回退）

> 状态：代码已回退到本次改造前；本条目保留设计、实测结论与恢复步骤，避免下次重复踩坑。

### 目标
手机作为瘦客户端访问 PC 上的同一份数据；PC 承担全部存储与执行（“PC 即云端”）；功能不阉割。

### 调研：UU 远程 / TRAE 移动端怎么做
- UU远程：手机号账号 + 设备绑定；被控端生成「设备 ID + 动态验证码」（8–16 位），主控输入即可连；自研 NECO 协议，UDP P2P 打洞优先，失败切云中继；两端都主动外连，不要求公网 IP / 端口映射；另有图形化端口映射。
- TRAE 移动端：同一手机号三端同账号；桌面端开关「允许移动端控制当前设备」，连接有效期 180 天；手机不直连电脑，而是把任务下发到云端，云端/电脑执行；电脑离线自动切云端；云端只存对话与产物，代码库留在本地；手机断网不影响已下发任务，状态三端同步。
- 共性：账号/设备身份层 + 云端控制面 + P2P/中继数据面 + 桌面端显式授权；两端都主动外连，所以不受 NAT/防火墙/局域网限制。

### 实现过的内容（已删除）
1. Tailscale 通道
   - `app/remote.py`：探测 `tailscale status/serve status`（8 秒缓存）；主机白名单（回环 + 配置域名 + tailnet 域名 + `100.64.0.0/10`）；6 位配对码（5 分钟、一次性、单码连错 5 次作废、全局连错 10 次冷却 5 分钟）；设备表 `data/remote/devices.json`，令牌只存 sha256。
   - `app/api/remote.py`：`status / enable / code / pair / revoke / refresh`；管理接口只接受本机；`pair` 是唯一允许未配对设备调用的入口。
   - `app/httpd.py`：`_is_remote()`（Host 非回环 / `X-Forwarded-*` / `Tailscale-User-*` / 设备 Cookie）、`_host_ok()` 白名单、`_authed()` 区分主 TOKEN 与设备令牌、首页按身份注入令牌、未配对改发配对页、`/pair` 路由。
   - `web/pair.html`：自包含配对页，支持 6 位码与 `?ticket=` 自动配对。
2. 局域网直连通道
   - `app/lan.py`：主服务之外额外监听 `0.0.0.0:<主端口+1>`；端口占用退随机并写回 `lan_port`；`main.py` 启动时按配置拉起。
   - `app/remote.py`：Host 白名单允许私有网段 IPv4 字面量（域名一律拒绝，挡 DNS rebinding）；`lan_ips()` 用 `Get-NetIPAddress` 过滤 WSL/Hyper-V 虚拟网卡，优先级 192.168 > 10 > 172。
   - `app/api/remote.py`：`/api/remote/lan` 开关、`/api/remote/qr` 一次生成局域网 + Tailscale 两张码（共用一张一次性票据）。
   - `qrcode>=7.4` 生成 SVG 二维码（SVG 模式不需要 Pillow）。
3. 运维配套
   - `app/firewall.py`：检测/创建 `pythonw.exe` 入站规则，普通权限走 `Start-Process -Verb RunAs` 提权，失败回退为手动命令。
   - `app/awake.py`：`SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` 阻止系统睡眠，不强制亮屏。
   - 设置页「系统 → 手机访问」：扫码配对 / 局域网直连 / Tailscale / 已配对设备四段；显示防火墙状态、网络类型（专用/公用）、访问地址、手动命令。

### 实测结论 / 卡点
- Tailscale 本身可用：PC 与手机同 tailnet，`tailscale ping` 31ms；PC 上访问 `https://laptop-e3mf0ee4.tail34a6d7.ts.net/` 返回 200 配对页。
- 手机端 Tailscale 频繁 `offline`（系统省电杀后台 / 未保持开启），这是 Tailscale 方案的主要不稳定点。
- 局域网：`0.0.0.0:58126` 监听正常，PC 本机访问 `http://192.168.31.198:58126/` 正常；但 Windows 防火墙没有 `pythonw.exe` 入站规则，手机连不进来。本机网络类型：WLAN 192.168.31.198、以太网 2（Public）；`Get-NetConnectionProfile` 未列出 WLAN 配置文件。
- 网卡坑：直接 `connect(8.8.8.8)` 取到的是 `172.29.95.2`（WSL/Hyper-V 虚拟网卡），会生成手机打不开的地址；必须用 `Get-NetIPAddress` + 网卡名过滤。
- 语音：局域网是 HTTP，`getUserMedia` 被浏览器禁用；只有 HTTPS（Tailscale 域名）下手机语音可用。

### 回退清单（本次已执行）
- 删除：`app/remote.py`、`app/lan.py`、`app/firewall.py`、`app/awake.py`、`app/api/remote.py`、`web/pair.html`、`tests/test_remote.py`
- 还原：`app/httpd.py`（鉴权 / Host 校验 / 静态注入 / `/pair` / `_do_post`）、`app/config.py`（移除 5 个 remote/lan 配置键）、`main.py`（移除局域网监听与保持唤醒）、`web/index.html`（移除导航与面板）、`web/settings.js`（移除元信息、回填/收集与整段手机访问逻辑）、`web/css/polish.css`（移除二维码样式）、`requirements.txt`（移除 qrcode）
- 清理：`data/config.json` 中 `remote_enabled / remote_hosts / lan_enabled / lan_port / remote_keep_awake` 已删除；`data/remote/` 已删除；venv 中 `qrcode` 已卸载
- 未改：Tailscale 的 `tailscale serve` 配置仍指向 `127.0.0.1:58125`，需要时执行 `tailscale serve reset` 清除

### 恢复时的建议顺序
1. 先做局域网直连 + 防火墙规则 + 保持唤醒（同 Wi-Fi 场景，零第三方依赖）
2. 再做扫码配对（一次性票据，5 分钟）
3. 最后做外网：优先出站隧道（cloudflared / frp），不要依赖手机常驻 Tailscale
4. 数据上移 PC（client_kv）与移动端布局（mobile.css）仍是未做的阶段 2/3

### 参考
- UU远程性能实测：https://uuyc.163.com/blog/shicetuijian.html
- UU远程远程协助（设备 ID + 验证码）：https://uuyc.163.com/help/20241204/40220_1197664.html
- TRAE 移动端快速开始：https://docs.trae.cn/work_get-started-with-trae-mobile
- TRAE 三端说明：https://docs.trae.ai/ide/what-is-trae?_lang=zh

## [2026-10-28] 润色结果补上「下载 .md」
- 问题/决策：润色完成后的「改写后的全文」只有「用全文替换 / 复制全文」，没有下载入口；此前只有「AI 引导制作」的结果页能下载。
- 解决方案/原因：在润色全文区新增「下载 .md」；新增 `ivPolishedMarkdown()` 统一取全文（优先模型返回的 resume_markdown，缺失时把能对上的改写应用到当前简历再导出），复制 / 替换 / 下载三处共用同一份内容，降级模式也有东西可导出。
- 相关代码/指令：`web/hall-interview.js` 的 `ivPolishedMarkdown()` / `ivReportHtml()` / `ivBindReport()`；下载文件名 `简历-润色-YYYY-MM-DD.md`。只改前端，Ctrl+F5 即可。

## [2026-10-28] 模拟面试全面更新：简历辅助制作闭环 + 面试质量 + 缺陷修复

- 问题/决策（自查发现的硬伤）：
  1. 本地访谈死循环：填过「基本信息」后，`_local_guide` 把跳过已填栏目后的 index 当成已问轮数返回，前端存回去后每轮都跳过、每轮问同一道教育题。
  2. 降级生成的「简历」是 JSON 原文（`{"basic": ...}`），不可投递。
  3. 「先生成一版」没有推进访谈：重开弹窗会重复显示刚答过的问题。
  4. 快速填写只存内容不存步骤，重开回到第 1 栏；AI 输入框草稿关闭即丢。
  5. 面试 / 专项训练先读输入框再收麦，最后一段转写漏提交。
  6. 面试中切页签不停麦，输入框被重建后转写直接丢弃。
  7. 专项训练「换一批」无效：每类固定 8 题、请求 12 题只给 8 题。
  8. 语速用「出题到提交」的总时长，含 TTS 朗读与思考，严重失真。
  9. 复盘 prompt 不带 JD / 简历，「岗位匹配」「专业能力」无依据；面试中不能跳过。
  10. 清空全部数据后内存态（职业规划/专项/引导）残留；localStorage 配额满时静默失败。
  11. 简历每个按键同步写整份 localStorage；改了简历旧结果不提示过期；空 markdown 可清空正文。
  12. 页面文案写「四维复盘」，实际五维；触屏上 hover 才显示的「复制/撤回」不可见。
- 解决方案/原因：
  - 后端 `_local_guide` 改为「返回 index+1 作为下一轮游标」，hint 不推进；补回归测试。
  - `_local_resume_markdown` 重写为六栏结构化 Markdown（教育/经历/项目/技能/补充），访谈问答附在补充信息；模型返回空正文时回退本地版并加 warning。
  - 新增 `extract_profile()` + `/api/hall/interview/resume/import`：模型抽六栏，失败时按小标题规则切段（含「技能：Python」一行式）。
  - `guide_turn` 支持 `focus`（把分析缺口变成访谈目标）；`interview_turn` 支持 `skip`（不计分、不计语速、不追问），`_advance()` 统一推进；本地复盘排除跳过题。
  - `finish_session` 的 prompt 带上 JD + 简历摘要；题库中英各 4 类扩到 20 题，`preset_questions(shuffle=True)` 随机抽题。
  - 前端：builder 状态统一存 `resumeGuide {qa, step, current, done, formStep, draft, mode}`；生成后清空当前题，避免重开重复；关闭弹窗/切模式/切页签/提交/跳过都收麦。
  - 语速改传「录音时长」（`iv.mic.lastMs`，一次性消费），没录音则由后端按字数估算并标注 estimated。
  - 简历输入改为内存即时更新 + 300ms 防抖落盘；结果带内容指纹 `fp`，改简历后显示「可能过期 + 重新分析」。
  - 结果页：followups 逐条「补进补充信息」、有 JD 时自动跑匹配并展示覆盖/缺口、「使用并分析」、展示 used_facts。
  - localStorage 写入失败 toast 一次；清空数据调用 `ivResetRuntime()` 同步内存；Ctrl+Enter 提交；触屏按钮常显；文案改为五维。
- 相关代码/指令：
  - `app/interview.py`：`_local_guide` / `_local_resume_markdown` / `extract_profile` / `guide_turn` / `interview_turn` / `finish_session` / `preset_questions`
  - `app/api/hall_interview.py`：`/resume/import`、`/turn` 的 skip、`/questions` 的 shuffle
  - `web/hall-interview.js`：`ivSaveBuilderState` / `ivImportProfile` / `ivStartGuideFromMissing` / `ivSubmitBuilder` / `ivRenderBuildResult` / `ivSkipQuestion` / `ivMicStop`
  - 验证：`pytest tests/test_interview.py` 49 条全过；`ruff` 通过；`node --check web/hall-interview.js` 通过；`scripts/dev_check.py` 8 项全过；浏览器实测导入六栏、模式/草稿保留、跳过题目、专项训练 12 题随机均正常，控制台无应用报错。
  - 注意：后端改动需重启主程序；前端改动需 Ctrl+F5。曾因页面缓存旧 JS 误判「模式没记住」，带 `?v=2` 重载后正常。

## [2026-10-27] 简历工作台「AI 引导制作」气泡复制：自动化环境验证
- 问题/决策：自动化窗口 document.hasFocus() 恒为 false，Clipboard API 的 writeText 报 NotAllowedError（Document is not focused），需确认新加的 execCommand 兜底是否真的把文本写进剪贴板。
- 解决方案/原因：兜底生效。点击气泡「复制」后 toast 为「已复制」，用 PowerShell Get-Clipboard 读到内容与 data-copy 完全一致（先聊聊你的基本情况：你是哪所学校、什么专业、哪一届毕业的？），控制台无报错。浏览器内 readText 仍读不到（同样是窗口无焦点）。
- 相关代码/指令：web/hall-interview.js 的 ivCopy()（writeText().catch(legacy)）；web/css/hall-interview.css 的 .iv-ai-msg:hover .iv-ai-copy。验证时需先刷新页面：改完 JS 后旧页面内存里仍是旧版 ivCopy（无兜底），会误判为「复制失败」。另注意 legacy() 不检查 document.execCommand 的返回布尔值，成功提示本身不能证明写入成功。

## [2026-10-28] 「手机访问（Tailscale）」设置页与 /pair 配对页 浏览器验收
- 问题/决策：需确认新设置页字段、未安装 Tailscale 时的降级提示、生成配对码的错误反馈，以及 /pair 窄屏适配。
- 解决方案/原因：设置页标题「手机访问（Tailscale）」正常；访问开关（已关闭）、Tailscale（未安装）、提示「没有找到 tailscale 命令，请先安装 Tailscale」、访问域名（—）、HTTPS（未就绪）、Serve（未开启）、Serve 命令 `tailscale serve --bg http://127.0.0.1:49706`、已配对设备（还没有配对设备）均存在。未开启开关时点「生成配对码」会弹出可读提示「先开启「允许已配对设备访问」并保存」。干净标签页控制台无报错（旧标签残留的 55743 错误与本服务无关）。/pair 显示「连接到电脑」、6 位码输入（maxlength=6、inputmode=numeric、pattern=[0-9]*）、设备名称（可选）、配对按钮，376x653 下无横向滚动，控制台无报错。
- 已修复/复验（2026-10-28 稍后）：CSS 给按钮加 `flex: 0 0 auto` 后，376x653 下三个按钮按内容宽 77/90/117px 显示，分三行换行，文字无溢出（scrollWidth==clientWidth），页面与内容区均无横向滚动；干净标签页控制台无报错。
- 相关代码/指令：设置弹窗 `#modal` → `.settings-nav` → 手机访问；`#btn-settings`、`#btn-close-modal`；服务地址 `http://127.0.0.1:49706/` 与 `/pair`。

## [2026-10-29] 自动化模式前端可视化：实时事件推送 + 运行看板
- 问题/决策：自动化模式下前端「完全没有变化」。根因三处：① `autopilot._run_turn` 用 `_FakeHandler` 在进程内调 `chat._api_chat`，SSE 帧被吞进 `h.events` 只做统计，从没转发到 `/api/events`；② `_publish` 只推目标元信息，且只在状态切换 / 回合结束时推，回合开始那行没有任何事件；③ 前端收到后只做「toast + `selectSession` 整会话重载」，还被 `!state.streaming` 挡住。一个回合要跑几分钟，浏览器全程零反馈。
- 解决方案/原因：
  - 后端 `app/autopilot.py`：新增 `_push_live` / `_phase`，`_FakeHandler` 加 `on_frame` 钩子，把解析出的帧转成 `autopilot_live` 事件（tool / tool_done / step / warn / replace / delta）。delta 是逐字高频帧，按 300ms 合并、只留最后 500 字；工具参数经 `_slim_args` 只留 path / cmd / query 这类短字段，文件内容一律不推。阶段事件：立项 plan、执行 run、机检 verify、评审 review、决策 boss、收尾 done。踩坑：模块里原本已有 `_live(gid)`（线程存活判断），新函数必须叫 `_push_live`，否则被同名定义覆盖。
  - 前端 `web/auto.js`：新增看板模块（`apLiveOnEvent` / `apLiveFromGoal` / `apLiveFetch` / `apLiveRender` / `apLiveBodyHtml` / `apLiveBind`）。顶栏显示「阶段 + 当前工具 + 第几回合 / 计时」，展开依次是目标、完成定义（✓/✗/· 与 x/y 通过）、计划、最近回合、实时动态、正在输出。目标全量数据直接复用 `/api/autopilot` 的 status 返回，不另开接口；事件里的精简 goal 只用来先占位，再补一次完整拉取。
  - 生效范围：只在普通对话页（`uiMode === 'solo'`）且只对当前会话生效；GAL 舞台不插这条；事件 `session_id` 与当前会话不一致直接丢弃。
  - 布局：`web/css/base.css` 的 `.ap-live` 系列作为 `.chat-col` 的 flex 子项插在输入框上方（`flex: none`）；`.ap-live-act` 用 `flex: 1 + ellipsis`，工具名过长不会挤走右侧箭头；`@media (max-width: 760px)` 隐藏回合 / 计时、正文限高 200px。
- 相关代码/指令：`app/autopilot.py`（`_slim_args` / `_push_live` / `_phase` / `_FakeHandler.on_frame` / `_run_turn` 的 delta 合并 / `_loop` 各阶段点 / `_finish`）；`web/auto.js`（看板模块、`handleAutoEvent` 的 `autopilot_live` 分支、`syncAutoChatFromSession` 收尾）；`web/index.html` 的 `#ap-live`；`web/css/base.css` 的 `.ap-live-*`。
- 验证：`tests/test_autopilot.py` + `tests/test_autonomy_policy.py` 全过、`ruff` 通过、`dev_check.py` 8 项全过；用 `%TEMP%` 下的临时探针（伪造 goal + 定时 `events.publish`）在真实页面验证：1280x800 横条 986x25、展开六节齐全且不遮挡输入框；376x653 回合 / 计时隐藏、省略号生效、无横向溢出；两轮控制台无报错。

## [2026-10-29] 提问卡回到「提问位置」+ 正文不再进灰底过程折叠
- 问题/决策：两个问题同源，都出在「过程 / 答案」的旧渲染模型上。① 流式期间 `obj.type === 'ask'` 把提问卡塞进 `row._toolsBox`（追加在 `.content-wrap` 末尾），后续正文继续写进上面那个 `row._content`，卡片就被永久顶到消息最下方；历史回显走 `renderToolLog` 同样是末尾一堆。② `buildBlocksView` 用「最后一个工具之后的最后一段 text 才算答案」(`pickAnswerIndex`) 猜答案，其余正文全塞进 `.process-fold`（背景 `var(--bg-hover)`）——数学辅导这类多轮追问里，正文大头都在中间段，于是整课讲义都被灰底包住、还能被折起来。
- 解决方案/原因：
  - `web/chat.js`：`buildBlocksView` 改为返回 `{ fold, parts }`——折叠区只放 reasoning + 工具组（真正的「过程」），每段 text 各自成 `.content`（与最终答案同一套排版，无灰底），`ask_user` 的卡片按块顺序插进 parts。删除 `pickAnswerIndex` / `pickFallbackAnswerIndex` / `ABNORMAL_END_KINDS`（不再需要猜答案）。`buildAiRowFromBlocks` 按顺序追加 parts，并记 `row._secs` / `row._asks` 供源码视图与续写使用。
  - `web/stream.js`：新增 `splitAtAsk(card)`——先把当前段 `paintLive()` 落屏，空段 `display:none`，卡片与新 `.content` 段一起插在 `typing`（live-bar）之前，`c` 指向新段、`liveRendered = acc.length`（acc 仍累加全文），`liveBox/tailBox/textNode` 置空后由 `ensureLive()` 重建。`ask` 分支改调它，不再建 `_toolsBox`。收尾重排时先删掉流式期的所有 `row._secs` / `row._asks` 再按落库 blocks 重建；拿不到 blocks 时把多段并回第一段，避免重复；富媒体增强改扫整条 `row._wrap`（多段都要渲染公式）。
  - `toggleRawView`：多段消息进入源码视图时只留第一段显示全文、其余段与提问卡先 `display:none`，退出时按 `_rawSaved` 还原。
  - `web/css/base.css`：删除已不再使用的 `.process-fold .process-text` 三条规则；新增 `.content-wrap > .ask-card { margin: 12px 0 }`。
- 相关代码/指令：`web/chat.js`（`buildBlocksView` / `buildAiRowFromBlocks`）、`web/stream.js`（`splitAtAsk` / `ask` 分支 / finally 重排 / `toggleRawView`）、`web/css/base.css`。
- 验证：`node --check` 通过；`python scripts/dev_check.py` 8 项全过（99 个 py 文件编译、39 个前端 JS 语法、Markdown 回归）。浏览器实测会话 `s_2eed2dd969cf` 第 694 条（9 个 blocks / 3 个 tools）：正文为普通背景无灰底；「提问（3 个）」落在「先说明…」之后、「一、直接回答」之前，「提问（2 个）」落在「算完我们做下一步…」之后、「好的，就按你定的来」之前，结尾「先算 4 块那一步，数字发我。」之后无卡片；「过程 · 工作 14 分 28 秒」展开后只剩思考摘要与 `skill.read` 工具组。
- 未覆盖：流式过程中 `splitAtAsk` 的真机走查（需要真实 ask_user 回合，未在本次会话里跑）。旧标签页残留的两条 `SyntaxError` 与新开标签页无关（刷新不新增、新标签页不出现）。

## [2026-10-29] 非 Agent 会话不再出现「未调用工具 / 计划未完成」提示
- 问题/决策：「助手9.0」（`agent_enabled=false`，但 `sandbox` 有值）几乎每条回复都带「本轮没有执行任何工具，也没有建立计划…点『继续输出』让它真正动手」；idx672 正文被追加「系统核对：本轮没有成功执行任何工具」（误命中散文「写完了」）；idx702 收尾写成「未完成：重写项目经历表述…写 resume.html…另有 3 步」（简历任务的旧计划串进数学辅导），并逼出模型解释系统提示的 meta 文本。决策：按你选的「方案 A」——非 Agent 会话关掉所有「工具 / 计划」核对，只保留知识库写入核对；联网搜索存在时仍核对「说了要查却没查」。
- 解决方案/原因：
  - 根因：这套「交付核对」原本只对文件 Agent 成立，却从未判断会话有没有 Agent 能力；`agent_mode` 只用在 `auto_continue_reason` 的第 3 条，第 2 条（计划未完成）和第 4 条（催建计划）漏判。另 `_latest_plan` 会向前翻**全部历史**，而 `plan_can_inherit` 的 `_CONT_PREFIX_RE` 认「接着」，于是「接着补基础」把几个任务之前的计划装回来了。
  - `app/api/chat.py`：`_pending_plan_end_reason(..., agent_mode=True)` 非 Agent 直接返回 None；`_done_claim_verdict(..., agent_mode=True)` 非 Agent 只核对「声称改记忆却没有 mem ops」；`_claims_pending_tool` / `_needs_tool_claim_nudge` 加 `agent_mode`，非 Agent 用新增的 `_PENDING_CLAIM_SEARCH_RE`（只认查/搜/抓取类，不再把「先把一行加上去」当声明）；两处调用点（对话循环里的补做 nudge、收尾时的正文警告）加 `(agent_on or search_ready)` 门槛；`_plan_stagnant` 只在 agent_on 时生效；`_latest_plan` 只读**最后一条** assistant 消息；`_prev_plan` 非 Agent 不取。
  - `app/stepctx.py`：`auto_continue_reason` 的「计划还有 N 步未完成」和「多步任务还没有执行计划」两条加 `agent_mode` 门槛（非 Agent 只保留「结尾征询用户」这类与能力无关的触发）。
  - 能力澄清：非 Agent 模式并非「只有写知识库」——仍挂提问卡、web_search/fetch_url（开联网时）、set_reminder、日程/任务等能力工具、update_plan/research_note/explore；但**会落地副作用的只有 `mem_ops_all`**，所以核对只留这一项。
- 相关代码/指令：`app/api/chat.py`（`_PENDING_CLAIM_SEARCH_RE` / `_claims_pending_tool` / `_done_claim_verdict` / `_pending_plan_end_reason` / `_latest_plan` / 计划继承处 / `_plan_stagnant` 调用 / 两处收尾核对）、`app/stepctx.py`（`auto_continue_reason`）。
- 验证：`pytest` 全量通过（含新增 4 条断言：非 Agent 不催计划、非 Agent 不催建计划、非 Agent 只核对知识库、`_latest_plan` 只读最后一条）；`ruff check` 本次改动文件全过（仓库另有 2 条历史告警：`app/codeindex.py:79` E741、`app/dojo/primitives.py:101` F841，与本次无关）；`dev_check.py` 8 项全过。用 sessions.json 离线复算 `s_2eed2dd969cf` 全部 assistant 消息：666~704 原先 13 条会出「没有执行任何工具」的提示，现在全部为 0；只剩 5 条「声称改记忆但没有 mem ops」的真实核对（168/170 模型确实只说了「已改短」没输出 mem 块）。
- 未覆盖：没有真实跑一轮新对话验证（要花 token），下一轮在 助手9.0 里实测收尾是否干净可用。

## [2026-10-29] AI 输出终止通知：按结束原因出文案 + 提示音
- 问题/决策：原来 `notifyChatDone` 只写「AI 回复完成 / 中断 + 正文前 90 字」，服务端已经算好的 `end_reason`（billing / network / length / max_rounds / budget / idle / empty / plan / stopped）完全没用上；断流后由 `/api/chat/attach` 补回结果的路径（`finishRecovered` / `keepRecovering`）一条通知都不发；系统通知没有任何声音。要求：所有 AI 输出终止都通知（含意外停止），内容显示原因，加音效，手动停止不通知，仍只在后台（窗口不在前台 / 别的会话）弹。用户选定：音效覆盖所有系统通知并按事件区分，用内置单音短提示。
- 解决方案/原因：
  - `web/auto.js`：新增 `NOTIFY_TONES`（done 880 / ask 660 / warn 587 / error 392 / remind 784）、`playNotifySound(kind)`（WebAudio 单音约 0.24s，带起落包络，无音频设备不抛）、`warmNotifySound()`（在用户手势里预热 AudioContext，否则后台标签页会被浏览器禁音）；`showSystemNotify(title, text, kind)` 与 `notifyOnce(key, title, text, kind)` 增参。自动化 done / needs_human / paused|failed / retrying、提醒推送、任务 blocked / manual / done / failed 各自带 kind。
  - `web/stream.js`：`notifyChatDone(info)` 改成对象入参；`END_REASON_LABEL` 把 kind 映射成「输出完成 / API 余额不足 / 连接中断 / 输出被截断 / 工具轮数达上限 / 达到本轮预算 / 本轮没有产出 / 计划已生成 / 本轮停在这里 / 任务未完成，可继续」；正文优先 `reason.text`，其次回复摘要，最后请求异常；`billing` / `network` 用 error 音，其它有 kind 用 warn，正常完成用 done；`stopped` 或 `kind === 'stopped'` 直接返回；窗口在前台且是当前会话时不弹（保持现状）。
  - 断流补漏：finally 里 `deferNotify = !recovered && !gotDone && !recovering && isCur()` —— 任务还在服务端跑时不再提前弹「请求失败」；`waitJobThenRefresh` 返回值改为 `{ ok, reason }`（顺带解析 attach 回放的 `end_reason`），`keepRecovering(sid, err)` 与 `finishRecovered(info)` 在真正结束时各补一条带原因的通知；`stopStreaming` 的 remote 分支改读 `r.ok`（手动停止不通知）。
  - `web/work.js`：进工作台时一并 `warmNotifySound()`。
- 相关代码/指令：`web/auto.js`（`NOTIFY_TONES` / `playNotifySound` / `warmNotifySound` / `showSystemNotify` / `notifyOnce` 及各调用点）、`web/stream.js`（`END_REASON_LABEL` / `notifyChatDone` / finally 通知分支 / `finishRecovered` / `keepRecovering` / `waitJobThenRefresh` / `stopStreaming`）。
- 验证：`node --check` 三个文件通过；`python scripts/dev_check.py` 8 项全过（99 个 py、39 个前端 JS）。用 `%TEMP%` 临时探针在 Node `vm` 里加载**真实的 auto.js + stream.js**（stub `document` / `Notification` / `AudioContext`）逐条断言：正常完成=「AI 回复 · 输出完成」+880；billing=「API 余额不足」+392；network=「连接中断」+392；length=「输出被截断」+587；max_rounds=「工具轮数达上限」+587；plan=「计划已生成」；`kind=stopped` 与 `stopped=true` 均 0 通知；断流无原因=「连接中断」+392 且正文为异常信息；窗口在前台 0 通知；别的会话在前台仍通知；`notifyOnce` 同 key 只弹一次；无 AudioContext 时不抛异常。
- 未覆盖：真机听感（音量 / 音色是否合适）、系统通知权限被拒绝时的表现、真实断流恢复路径（要真跑一轮并在中途断网）。

## [2026-10-29] Token 优化 A 档：动态块后置 + 工具 schema 分层
- 问题/决策：流水 1.577B prompt 里 98.2% 是「每轮重发已有上下文」；首轮固定约 51k（历史助手正文 47%、工具 schema 28%）；55 个工具 schema 约 30k 字符，每轮都重发。决策先做零内容风险的 A 档：A1 把每轮必变的动态块移出 system；A2 低频工具组默认不下发 schema，按意图域挂载 + `tool_search` 解锁。A3（压缩 tools_doc）实测 3.7k 字里大部分是行为规则、不是与 schema 的重复，硬删会伤文本协议兜底，放弃。
- 解决方案/原因：
  - A1 `app/api/chat.py`：新增 `_attach_dyn_tail(messages, dyn, clock)`，把 `sys_dyn`（记忆检索 / 任务状态）与完整时钟贴到本轮最后一条 user 消息末尾，不再拼进 system。原因：system 一旦逐字变化，它后面的整段历史都会 miss 前缀缓存；放到请求末尾只影响最后一条消息，位置也更靠后。删掉原来「短时钟前插」的重复块；`session_context` 估算时把 `_dyn_tail` 补回，`input_breakdown` 减掉重复计入的 `sys_dyn`。
  - A2 `app/agent.py`：新增 `TOOL_GROUPS` / `TOOL_GROUP_KEYS` / `TOOL_SIG` / `hidden_tools()` / `tool_group_catalog()` / `_t_tool_search()`；`native_tool_schemas(..., allow_groups=None, unlocked=None)` 默认仍全挂，传入域集合时隐藏未命中组、并在有隐藏时追加 `tool_search`；`build_agent_prompt(..., allow_groups=None)` 在原生模式下把「未挂载工具组」目录写进提示。
  - `app/api/chat.py`：`_cap_domains_for` 合并 `agent.TOOL_GROUP_KEYS`（页面 / 游戏→visual，素材→asset，音频→audio，git→git，工作流→workflow，调研→explore，符号→symbol，脚本→code，撤销→history）；`caps["tool_unlock"] = set()`；`native_tool_schemas` 传 `allow_groups=cap_domains, unlocked=...`；工具循环里检测到解锁变化后重建 schema / `_tools_tokens` / `_schema_chars`。
- 相关代码/指令：`app/agent.py`（`TOOL_GROUPS` / `hidden_tools` / `_t_tool_search` / `native_tool_schemas` / `build_agent_prompt` / `_full_agent_prompt` / `_run_tool_impl` 的 `tool_search` 分支）、`app/api/chat.py`（`_attach_dyn_tail` / `_cap_domains_for` / `caps["tool_unlock"]` / 每轮重建 schema）。
- 验证：`python -m pytest -q` 全量 EXITCODE=0（4 skipped）；`python scripts/dev_check.py` 8 项全过。新增 3 条回归：`test_tool_schema_groups_hidden_by_default` / `test_tool_search_unlocks_group` / `test_attach_dyn_tail_keeps_system_stable`。schema 实测：全量 59 工具 / 31,364 字符 / 10,807 token → 普通任务 25 工具 / 14,179 字符 / 4,800 token（**-55%**），页面任务 28 / 15,949 / 5,436，素材任务 30 / 16,250 / 5,497；核心文件工具与 `update_plan` / `plan_patch` / `ask_user` / `acceptance` / `research_note` 保持常驻。注意：在沙箱内跑 pytest 会被 Edge 字典 / OneAuth / PowerShell 历史路径拦截（`process launch failed` / `hit restricted`）并返回 1，需要在沙箱外跑才能拿到真实 exit code。
- 未覆盖：没有真跑一轮对话（会花 token）；`tool_search` 解锁后的模型实际行为、A1 的缓存命中率提升，需要下一轮用 `token_usage.jsonl` 的 `cached / prompt` 对比确认。

## [2026-10-29] Token 优化 B 档：历史过程压缩 + 批量读取 + 空转刹车
- 问题/决策：A 档之后仍有三块可省：① 历史 assistant 的 `content` 是所有轮次正文的拼接，过程旁白占 77%，每轮都重发；② 每轮平均只有 0.91 个工具调用（51.8% 只调 1 个），看多个文件要跑多轮；③ 自动续跑后仍不执行工具的轮次有 844 个（约 95M prompt token），第二次机会基本没救回来。对应 B1/B2/B3。
- 解决方案/原因：
  - B1 `app/context.py`：新增 `_compact_history_assistant()` 与 `_history_compact_cfg()`；`build_context_messages()` 对**最近 2 条之外**的 assistant 消息只保留「过程摘要（首尾各 120 字）+ 最后一段答案」。没有 `blocks`、只有一段正文、或 `blocks` 覆盖不足 60% 的旧消息保持原文。配置 `history_process_keep_turns`（默认 2）/ `history_process_digest`（默认 240），任一项为 0 = 关闭。
  - B2 `app/agent.py` + `web/chat.js`：新增 `read_files(paths[], start?, limit?)`，一次读最多 8 个文件；加入 `TOOLS / FILE_TOOLS / PLAN_MODE_TOOLS / _REQUIRED_ARGS / _HANDLERS`，并在 `_run_tool_impl` 里为每个 path 补 `_remember_read`（否则随后 `edit_file` 会被写前读检查拦下）。提示词里把「看 3 个文件就发 3 个 read_file」改成优先 `read_files`；前端补 `TOOL_LABELS / TOOL_SUMMARY` 的「批量读取」。
  - B3 `app/api/chat.py` + `app/config.py`：空转刹车阈值从硬编码 2 改成配置 `agent_auto_idle_limit`（默认 1），即续跑后一次没执行工具就停，避免连续空转。
- 相关代码/指令：`app/context.py`（`_compact_history_assistant` / `_history_compact_cfg` / `build_context_messages`）、`app/agent.py`（`_t_read_files` / `_HANDLERS` / `native_tool_schemas` 的 read_files schema / `_run_tool_impl` 读状态登记 / `_full_agent_prompt`）、`app/api/chat.py`（`_auto_idle_limit` / `auto_idle >= _auto_idle_limit`）、`app/config.py`（`history_process_*` / `agent_auto_idle_limit`）、`web/chat.js`（`TOOL_LABELS` / `TOOL_SUMMARY`）。
- 验证：`python -m pytest -q` 全量 EXITCODE=0（4 skipped，需在沙箱外跑）；`python scripts/dev_check.py` 8 项全过；`node --check web/chat.js` 通过。离线用真实会话测 B1：`s_2809c1a88538` 413,417 → 239,743 字符（-42.0%）、`s_2eed2dd969cf` 93,502 → 79,553（-14.9%）、`s_fe95cee47c2a` 84,302 → 45,722（-45.8%）。新增回归：`test_history_process_compaction_keeps_recent_turns` / `test_history_process_compaction_skips_unreliable_messages` / `test_read_files_batches_and_keeps_errors` / `test_read_files_counts_as_read_before_write`。全量跑时 `tests/test_deal_bridge.py::test_bridge_cors_only_allows_extensions` 偶发 `WinError 10053`（本地 HTTP 桥瞬时断连），单跑与重跑均通过，与本次改动无关。
- 未覆盖：B2 的实际轮数下降要真跑任务才知道（本次未花 token）；B3 收紧后续跑行为需观察是否会把「需要两轮叙述再动手」的任务提前截断。

## [2026-10-29] 视觉 / 联网搜索默认用聊天模型，独立 API 改为兜底
- 问题/决策：原来识图固定走视觉 API（`vision_use_chat` 默认 false），联网搜索固定走博查 / 豆包 / Tavily，DeepSeek 新上线的 Search API 完全没接。要求：聊天模型有视觉 / 搜索能力就优先用它；能力不足、或用户想用自己的 API 服务时回落，并在设置里提供「优先启用视觉 API / 联网搜索 API」开关。
- 解决方案/原因：
  - `app/model_caps.py`：新增 `web_search` 能力位；DeepSeek 各模型（deepseek-flash / v4 / chat / reasoner / deepseek）标 True——Search API 是服务商级能力，复用聊天密钥，不依赖具体模型。
  - `app/search.py`：新增 `deepseek` 服务商（`https://api.deepseek.com/v1/tools/search`）；`chat_search_available()` = 聊天地址含 deepseek + 已配聊天密钥 + 模型声明 web_search；`effective_search_provider()` 默认优先 deepseek，`search_prefer_api=true` 时用配置的服务商；`get_search_conf()` 在 deepseek 模式下复用聊天密钥、从聊天地址推导 Search 地址；`search_web()` 增加 DeepSeek 响应映射（summary 优先于 snippet）。
  - `app/config.py::use_chat_for_vision()`：默认用聊天模型识图——只判断「是否配了聊天密钥」，不再按 `model_caps` 的 `vision` 位猜能力（同一模型名走不同网关时能力不一致，注册表会误判成不支持）；`vision_prefer_api=true` 时改走视觉 API；旧 `vision_use_chat` 不再作为强制项。`app/imaging.py::chat_vision_available()` 同步为「配了聊天密钥即可用」。
  - `/api/state`（`app/api/misc.py`）返回 `chat_search_available` / `chat_vision_available` / `search_effective`；`web/stream.js` 的 `hasSearchKey()` 认可「聊天模型自带搜索」；`web/init.js` 保存能力态；设置页视觉页新增「优先使用视觉 API」，联网搜索页新增「优先使用独立搜索 API」。
- 相关代码/指令：`app/search.py`（`chat_search_available` / `effective_search_provider` / `get_search_conf` / `search_web`）、`app/config.py`（`use_chat_for_vision` / `vision_prefer_api` / `search_prefer_api`）、`app/model_caps.py`（`web_search`）、`app/imaging.py`（`chat_vision_available`）、`app/api/misc.py`、`web/stream.js`、`web/init.js`、`web/settings.js`、`tests/test_search_provider.py`。
- 验证：新增 `tests/test_search_provider.py` 7 条（DeepSeek 自动优先 / prefer_api 回落 / 非 deepseek 回落 / 响应映射 / 视觉三种选择）；全量 `pytest` EXITCODE=0；`dev_check.py` 8 项全过；用真实聊天密钥调用 `POST https://api.deepseek.com/v1/tools/search` 成功返回 2 条结果（官方计费 5 元 / 1000 次成功请求）。
- 注意：搜索按次计费；当前 `search_enabled=false`，需要在输入框点亮「联网搜索」才会调用。识图默认走聊天模型（配了聊天密钥即可），只有勾选「优先使用视觉 API」才改走视觉 API；旧配置里的 `vision_use_chat` 已不再强制生效。

## [2026-10-29] 提交前检查：数据泄露点与仓库卫生
- 问题/决策：推送 GitHub 前做全量审计。密钥面是干净的：`data/`（2.78GB，`config.json` 内含 `chat_api_key` / `search_api_key`）一直被 `/data/` 挡住，3 个历史提交都没触碰过；HEAD 里扫不到 `sk-` / `ghp_` / 私钥 / 本机路径。但有 5 类本地文件处于「可跟踪」状态，另有 4 张背景图漏暂存、旧前端文件未记录删除、README 链接指向被忽略的 docs。
- 解决方案/原因：
  - `.gitignore` 新增必须项：`/NOTE.md`（1MB 开发日志，实测含 `D:\Tools\Python`、`D:\资料库\Math` 等本机路径）、`/resume.pdf`、`/resume.html`、`/_probe.pdf`、`/.tmp_audit/`、`/scripts/_tmp_*.py`；防御性补 `.env` / `.env.*` / `*.pem` / `*.key` / `*.p12` / `*.pfx` / `*.db` / `*.db-wal` / `*.db-shm` / `*.pyo` / `.mypy_cache/` / `.coverage` / `htmlcov/` / `/server.json` / `/app_port.txt` / `desktop.ini` / `/build/` / `/dist/`。明确保留 `web/vendor/` 与 `resources/presets/` 入库（项目无构建步骤，vendor 必须随仓库走）。
  - 新增 `.gitattributes`：`* text=auto`，消除 Windows 上整文件「LF will be replaced by CRLF」的伪差异。
  - `readme.md`：三处 `docs/*.md` 链接（GEO_LABEL_PERF / WORKSPACE_DESIGN / IDE_VSCODE_PARITY）改成「本地文档，不随仓库发布」，避免 GitHub 上 404；其中 IDE_VSCODE_PARITY 含本机用户名，继续保持忽略。
  - `resources/presets/CREDITS.md`：立绘表后缀 `.jpg` 改回实际的 `.png`。
  - 新增 `web/vendor/THIRD_PARTY_LICENSES.md`：katex / monaco / vis-network / xterm 均 MIT，补上原先缺失的许可声明。
  - 仓库卫生：补暂存 4 张背景图（`bg_sakura_valley/meadow/temple.jpg`、`bg_hills_green.jpg`，被 `backgrounds.json` 引用）；记录 `web/app.js`、`web/style.css` 的删除。最终暂存 806 个文件、41.3MB，最大单文件 8.7MB（`bgm_war.mp3`）。
- 相关代码/指令：`git check-ignore -v <路径>` 逐项验证忽略是否生效；`git add -A` 后用 `git diff --cached --name-only` 复核是否混入 `data/`、`NOTE.md`、`resume.*`、`.tmp_audit/`。
- 验证：`dev_check.py` 8/8 全过、`pytest` exit 0（4 skipped）；暂存区无敏感文件。未执行 commit / push（需用户确认提交信息）。

## [2026-10-29] 治「看文件就写脚本」：补读类工具表达力 + 改提示词
- 问题/决策：用户反馈 AI 想看一眼文件就写 Python 脚本。统计本地全部会话（`data/sessions/*/events.jsonl`，3480 次工具调用）后确认：read_file 939（27%）、run_command 793、run_script 仅 45（1.3%），但脚本集中爆发在少数工程会话（`s_2809c1a88538`：24 次脚本 vs 7 次 read_file），且 45 次里 18 次是 `open()` 直接读文件。结论不是"每次"，而是"需要多文件 / 多处命中 / 统计时必然退化成脚本"。
- 原因：
  1. `run_script` 的 schema 与 system prompt 原文写着「适合一次读多个文件 / 多处搜索 / 统计仓库」，等于官方推荐脚本读文件；
  2. 专用工具表达力不足：`search_files` 每个文件只报第一条命中就 break、最多 60 条、无上下文；`read_file` 只给带行号全文、256KB 截断；
  3. Code Mode 是真实 Python 子进程，`open()` 不受上限、不加行号、可 os.walk/hashlib，且 `allow_write` 只挡 `gm.call` 白名单，挡不住内置文件 IO；
  4. 历史会话里的 `write_file + run_command` 模式被上下文继承；DeepSeek 本身偏代码解法。
- 解决方案/原因：
  - `app/agent.py::_t_read_file`：新增 `raw=true`（返回不带行号的原文）；单次读取上限 256KB → 1MB（`MAX_READ_FILE_BYTES`），仍可 `start/limit` 分段。
  - `_t_read_files`：一次最多 8 → 16（`MAX_READ_FILES`），支持 `raw`。
  - `_t_search_files` 重写：每文件默认列前 5 处（`max_per_file`）、`context` 带前后 0–5 行、`count_only` 只统计每文件命中数；返回「共 N 处命中，涉及 M 个文件」。
  - 读类工具放开只读目录：`READ_ONLY_SEGMENTS = {node_modules, .venv, venv, __pycache__}`，`resolve_path(..., for_read=True)` 与 `_t_list_dir` 可读这些目录；`.git`（可能含令牌）与本项目 `data/` 仍然禁止，写入类工具仍按 `BLOCKED_SEGMENTS` 拒绝。
  - 提示词：`run_script` 描述改为「只在需要循环 / 条件分支 / 复杂计算时用」；规则 1 增加「看文件不要写脚本」；compact / 详细工具清单同步 `raw / context / count_only`。
  - `tests/test_prompt_budget.py`：Agent 提示阈值 7400 → 7600（新增读类说明）。
- 相关代码/指令：`agent.py`（`MAX_READ_FILE_BYTES` / `MAX_READ_FILES` / `MAX_SEARCH_PER_FILE` / `MAX_SEARCH_CONTEXT` / `READ_ONLY_SEGMENTS` / `_t_read_file` / `_t_read_files` / `_t_search_files` / `resolve_path` / `_full_agent_prompt` / `run_script` schema）、`tests/test_agent_guards.py`（新增 5 条）。
- 验证：`py_compile` 通过；`pytest tests/test_agent_guards.py + test_agent_intent.py + test_prompt_budget.py` 全过；`dev_check.py` 8 项全过；全量 `pytest` exit 0。
- 未覆盖 / 遗留：`run_script` 里的 `open(...,'w')` / `os.remove` / `subprocess` 当时仍不受 `code_mode_allow_write/cmd` 约束；同日下一条「Code Mode 安全护栏」已补上。

## [2026-10-29] Code Mode 安全护栏：脚本内置文件 IO 也受权限约束
- 问题/决策：`run_script` / `run_workflow_script` 是真实 Python 子进程，`allow_write` 只限制 `gm.call` 的工具白名单，脚本里的 `open(...,'w')`、`os.remove`、`shutil.*`、`subprocess` 全部绕过开关；也绕过了 `read_file` 的 256KB 上限、秘密文件拦截与「沙箱外只读」限制。决定用 audit hook 在子进程里做真正的边界（高档方案）。
- 解决方案/原因：
  - `app/code_mode.py` 新增 `GUARD_SRC`：脚本开头自动注入（在用户代码之前），变量统一 `_gm_` 前缀。规则：读只允许沙箱内 + Python 自身（`sys.prefix` / `stdlib` / `site-packages`）+ 桥接目录 + 脚本临时目录；写默认全禁，开关打开后仍只允许沙箱内；`subprocess.Popen` / `os.system` / `os.exec` 默认拒绝。拦截的 audit 事件：`open`、`os.remove`、`os.rename`、`os.mkdir`、`os.rmdir`、`os.chmod`、`os.chown`、`os.truncate`、`os.link`、`os.symlink`、`shutil.rmtree/move/copyfile/copytree/copymode`、`subprocess.Popen`、`os.system`、`os.exec`、`os.posix_spawn`。
  - `_clean_env(bridge, sandbox, allow_write, allow_cmd, allow_outside, extra_allow)`：把沙箱根与三个开关写进 `GM_GUARD_*` 环境变量；新增 `GM_GUARD_DENY`（本项目 `data/` 与沙箱内 `.git`，读和写都拒绝）；新增 `PYTHONDONTWRITEBYTECODE=1`（默认禁写时不能因 import 生成 .pyc 而失败）。
  - `code_mode.run(..., allow_outside=False)` 与 `workflow_script.run(..., allow_outside=False)` 都注入 `GUARD_SRC` 并传参；`workflow_script` 复用同一份护栏，避免从另一条路绕过去。
  - 调用方：`chat.py` 的 `caps["allow_outside"] = bool(s.get("allow_outside_read"))`，`run_script`（工具循环 / 工作流 script 节点）与 `agent._t_run_workflow_script` 都把该值传进护栏。
  - 新增 `trim_guard_trace()`：拒绝时 traceback 里 3–4 层 `_gm_*` 栈帧会被剪掉，只留用户代码位置与最终异常（省 token、不干扰定位）。
  - 文案同步：`config.py` 的 `code_mode_allow_write/cmd` 注释、`run_script` schema、system prompt 工具说明都改为「默认只读沙箱内文件，open/os.remove/subprocess 会被护栏拒绝」。
- 相关代码/指令：`app/code_mode.py`（`GUARD_SRC` / `_clean_env` / `run` / `trim_guard_trace`）、`app/workflow_script.py`（`run`）、`app/api/chat.py`（`caps["allow_outside"]` / 两处 `code_mode.run`）、`app/agent.py`（`_t_run_workflow_script`）、`tests/test_code_mode.py`（新增 9 条）。
- 验证：`test_code_mode.py` 19 条全过；`test_workflow_script.py + test_workflow.py + test_prompt_budget.py` 全过；全量 `pytest` exit 0；`dev_check.py` 8/8。真机复验：写沙箱外 / 读沙箱外 / 起子进程 / 读 `data/config.json` / 读 `.git/config` → 均被拒并给出可读原因；读沙箱内文件、`gm.call("read_file")` → 正常。
- 注意：护栏只拦文件与进程，不拦网络（`socket` 未加事件）；`allow_write` 打开后写入仍被限制在沙箱内，`os.environ` 也已被 `_clean_env` 裁成白名单，API key 不会进脚本。

## [2026-10-29] 安全审计：外部攻击面加固（防网页 / 扩展劫持本机）
- 问题/决策：全项目安全审计，用户要求优先处理「外部网络攻击劫持电脑」。审计结论：本地服务只绑 127.0.0.1 + 随机 Token + Host 校验，Electron 隔离与备份/解压/上传路径校验都合格；主要缺点击劫持防护、跨源调用放行过宽、抓正文的 SSRF 可被 30x 绕过。
- 已修（按攻击面）：
  - 点击劫持：`httpd._send` 统一加 `X-Frame-Options: SAMEORIGIN` / `CSP: frame-ancestors 'self'` / `X-Content-Type-Options: nosniff` / `Referrer-Policy: no-referrer`，外部页面再也不能 iframe 套住审批卡。
  - 跨源调用：新增 `same_origin_ok(origin, host)`，`/api/*`（bridge 除外）的 `Origin` 必须等于本页面 `Host`；扩展来源的 ACAO 只对 `/api/computer/bridge/*` 回显。网页 / 恶意扩展即使拿到 Token 也调不动本地 API。
  - SSRF：`app/search.public_url_error()` 统一公网校验；新增 `_public_get()` 用 `allow_redirects=False` 手动逐跳校验（最多 3 跳），公网页 302 到 `127.0.0.1` 不再被跟随；`agent._url_public_error` 委托同一实现，避免两套判断漂移。
  - 静态目录：`startswith(WEB_DIR)` 改为 `realpath + commonpath`（`httpd.static_file_path`），修掉 `/static/../webXXX/...` 同级目录绕过。
  - Electron：`will-navigate` 只把 `http(s)` / `mailto` 交给系统浏览器，`file:` / `ms-settings:` 等 scheme 不再转交。
- 相关代码/指令：`app/httpd.py`（`SECURITY_HEADERS` / `same_origin_ok` / `static_file_path` / `_send` / `do_GET` / `do_POST` / `_static`）、`app/search.py`（`public_url_error` / `_public_get` / `_fetch_one`）、`app/agent.py`（`_url_public_error` 委托，移除重复的 socket/ipaddress 实现）、`desktop/main.js`、`tests/test_web_hardening.py`（新增 9 条）。
- 验证：真机起服务实测 `XFO: SAMEORIGIN`、`CSP: frame-ancestors 'self'`；无 Token / 外源 Origin / 扩展源（非 bridge）→ 403，同源 → 200；`/static/../app/main.py` → 404。全量 `pytest` exit 0；`dev_check.py` 8/8。
- 未修（按用户范围暂缓，风险仍在）：① `ai` 审批档下「新建脚本 + 运行」可无提示执行任意代码；② 项目级 `.gm/agent-rules.json` 可 write allow 放宽权限；③ bridge 配对密钥走 URL query 且用 `==` 比较；④ `/api/open/path` 接受任意目录；⑤ 密钥明文落盘（可考虑 DPAPI）与第三方抓取（Jina / Edge TTS）的隐私披露。

## [2026-10-29] 修复「后台运行中」假死 / 进程关不掉
- 现象：前端停在「后台运行中…」，看不到输出、点停止无效；任务管理器关 pythonw 也无效。
- 根因（实测 `data/app.log` + 进程表 + 端口表）：
  1. 关窗时仍有 1 个流，`chat_window_keep_alive=true` 进入宽限；21:57:53 流结束、日志打印「server stopped」，但进程没退（残留非守护对象让解释器退出时一直 join）→ 变成「进程活着、socket 已关」的僵尸（`Get-NetTCPConnection` 查不到 51501 在监听）。
  2. 21:56 重开的窗口连的就是 51501；服务随后退出 → 页面停在旧 DOM 的「后台运行中」，停止 / 重开都打不到后端。
  3. 旧逻辑遇到「pid 活着但端口不通」只弹框让用户手动杀；venv 的 pythonw 是父+子两个进程，杀一个另一个还在；而退出前已 `_clear_state()`，下次启动看不到残留 → 「怎么关都关不掉」。
- 修复：
  - `main.py` 退出末尾 `os._exit(0)`（先 `shutdown()` + `server_close()`），保证窗口关掉进程一定结束。
  - 正常退出**不再删** state 文件；下次启动发现「pid 活着 + 端口不通」直接 `taskkill /PID /T /F` 收掉进程树（`_kill_stale_tree`），不再弹框要求手动处理。
  - `app/httpd.py` 增加 SSE 客户端计数 `active_clients()`；窗口关闭后若 5 秒内有新界面连上（重开 / 刷新），主循环继续服务而不退出（`_window_reconnected` / `_wait_clients_gone`），避免「新窗口指向一个刚停掉的服务」。
  - `web/stream.js::keepRecovering`：连续 3 次（约 15 秒）连不上后端就清掉 `streamingSids` 并提示「后台服务已断开，请重新启动应用」，不再空转 10 分钟；等满 10 分钟也会清掉标记。
- 相关代码/指令：`main.py`（`_kill_stale_tree` / `_window_reconnected` / `_wait_clients_gone` / 退出段）、`app/httpd.py`（`active_clients` / `_client_enter` / `_client_leave`）、`web/stream.js`（`keepRecovering`）、`tests/test_web_hardening.py`（+3 条）。
- 验证：`tests/test_web_hardening.py` 12 条全过；`dev_check.py` 8/8；全量 `pytest` exit 0。
- 遗留：本机仍有两个 21:53:48 启动的僵尸 pythonw（无监听端口），需要手动结束一次；之后由新逻辑兜底。
